"""RAGFlow owns vectors and ranking; SQLite owns source identity and access.

Imports are durable and resumable. A timed-out chunk creation is reconciled by
reading the remote document before another paid embedding can be attempted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.shared.domain.values import dump, now
from app.shared.infrastructure.persistence.blob_store import _verify_existing

SCHEMA_VERSION = 21
RECIPE = "mineru-ragflow-structured-v1"
_ID = re.compile(r"[a-zA-Z0-9_-]{1,128}\Z")
MAX_RECONCILE_INTENTS = 500


def _digest(value):
    return hashlib.sha256(dump(value).encode()).hexdigest()


def configured():
    return all(
        os.getenv(key, "").strip()
        for key in ("RAGFLOW_BASE_URL", "RAGFLOW_API_KEY", "RAGFLOW_EMBEDDING_MODEL")
    )


def profile():
    if not configured():
        return None
    return _digest(
        [
            RECIPE,
            os.environ["RAGFLOW_BASE_URL"].rstrip("/"),
            os.environ["RAGFLOW_EMBEDDING_MODEL"],
            os.getenv("EMBEDDING_BASE_URL"),
            os.getenv("EMBEDDING_MODEL"),
        ]
    )


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("RAGFlow 返回的资源标识无效。")
    return value


class RAGFlowError(ValueError):
    def __init__(self, code, *, retry_after=30, unknown=False):
        self.code, self.retry_after, self.unknown = str(code), retry_after, unknown
        self.request_outcome_unknown = self.code == "embedding_outcome_unknown"
        self.retryable = not self.request_outcome_unknown and (
            unknown
            or self.code == "http_429"
            or self.code.startswith("http_5")
            or self.code
            in {
                "api_500",
                "ReadTimeout",
                "WriteTimeout",
                "ConnectTimeout",
                "PoolTimeout",
                "ConnectError",
                "ReadError",
                "WriteError",
                "RemoteProtocolError",
            }
        )
        super().__init__(f"RAGFlow 暂不能继续（{self.code}），原件和索引进度已保留。")


class RAGFlowClient:
    def __init__(self):
        if not configured():
            raise RAGFlowError("not_configured")
        self.base = os.environ["RAGFLOW_BASE_URL"].rstrip("/")
        parsed = urlsplit(self.base)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise RAGFlowError("invalid_endpoint")
        # Plain HTTP is only for this host's private RAGFlow service.
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise RAGFlowError("https_required")
        self.key = os.environ["RAGFLOW_API_KEY"]

    def call(self, method, path, *, payload=None, params=None, files=None, timeout=None):
        request_timeout = 120 if timeout is None else min(30, max(1, float(timeout)))
        try:
            with httpx.Client(
                timeout=httpx.Timeout(request_timeout, connect=min(5, request_timeout)),
                trust_env=False,
                follow_redirects=False,
            ) as client:
                with client.stream(
                    method,
                    self.base + "/api/v1" + path,
                    headers={"Authorization": "Bearer " + self.key},
                    json=payload,
                    params=params,
                    files=files,
                ) as response:
                    status, retry = response.status_code, response.headers.get("Retry-After")
                    raw = bytearray()
                    for block in response.iter_bytes():
                        raw.extend(block)
                        if len(raw) > 16 * 1024 * 1024:
                            raise RAGFlowError("response_size", unknown=method == "POST")
            if status != 200:
                from app.shared.infrastructure.http.download import _retry_after

                raise RAGFlowError(
                    f"http_{status}",
                    retry_after=min(3600, max(15, _retry_after(retry) or 30)),
                    unknown=method == "POST" and status >= 500,
                )
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("code") != 0:
                code = (
                    value.get("code", "invalid_response") if isinstance(value, dict) else "invalid_response"
                )
                # Never put provider response bodies, model credentials or private
                # source text in job errors or the application's public API.
                raise RAGFlowError(f"api_{code}")
            return value.get("data")
        except httpx.HTTPError as exc:
            raise RAGFlowError(type(exc).__name__, unknown=method == "POST") from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise RAGFlowError("invalid_json", unknown=method == "POST") from None


def chunk_content(chunk):
    # The source ordinal distinguishes equal passages at different locations;
    # RAGFlow derives a chunk ID from content and remote document ID.
    locator = json.loads(chunk["locator"])
    page = locator.get("physical_page", locator.get("page"))
    origin = f"来源页码：{page}；" if page is not None else ""
    return chunk["retrieval_text"].strip() + f"\n\n{origin}段落序号：{chunk['ordinal'] + 1}"


class RAGFlowIndex:
    def __init__(self, service):
        self.service, self.db = service, service.db
        with self.db.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)
            ).fetchone():
                conn.execute("""CREATE TABLE ragflow_datasets(
                    owner_id TEXT NOT NULL REFERENCES users(id),profile TEXT NOT NULL,
                    name TEXT NOT NULL UNIQUE,remote_id TEXT,created_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id,profile))""")
                conn.execute("""CREATE TABLE ragflow_documents(
                    generation TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES documents(id),
                    owner_id TEXT NOT NULL REFERENCES users(id),profile TEXT NOT NULL,
                    source_hash TEXT NOT NULL,source_version INTEGER NOT NULL,index_revision INTEGER NOT NULL,
                    dataset_id TEXT,remote_id TEXT,name TEXT NOT NULL,state TEXT NOT NULL,
                    chunk_count INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
                conn.execute(
                    "CREATE INDEX ragflow_document_source ON ragflow_documents(document_id,profile,source_hash,source_version,state)"
                )
                conn.execute("""CREATE TABLE ragflow_chunks(
                    generation TEXT NOT NULL REFERENCES ragflow_documents(generation),
                    local_id TEXT NOT NULL,input_hash TEXT NOT NULL,content_hash TEXT NOT NULL,
                    remote_id TEXT,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                    intent_at REAL,next_at REAL NOT NULL DEFAULT 0,error_code TEXT,
                    PRIMARY KEY(generation,local_id))""")
                conn.execute("CREATE INDEX ragflow_chunk_remote ON ragflow_chunks(remote_id,generation)")
                conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (SCHEMA_VERSION, now()))
            conn.execute("SELECT owner_id,profile,name,remote_id FROM ragflow_datasets LIMIT 0")
            conn.execute(
                "SELECT generation,document_id,source_hash,source_version,index_revision,state,remote_id FROM ragflow_documents LIMIT 0"
            )
            conn.execute(
                "SELECT generation,local_id,input_hash,content_hash,remote_id,state,intent_at,next_at FROM ragflow_chunks LIMIT 0"
            )

    def binding(self, conn, document_id, fingerprint, *, include_chunks=True):
        row = conn.execute(
            """SELECT d.id,d.owner_id,d.role,d.version,d.content_hash,d.title,d.filename,
            d.size,d.storage_path,d.mime_type,r.complete,r.chunk_count,r.revision index_revision
            FROM documents d JOIN retrieval_documents r ON r.document_id=d.id WHERE d.id=?""",
            (document_id,),
        ).fetchone()
        if row is None or row["role"] != "primary" or not row["complete"]:
            return None
        chunks = (
            [
                dict(c)
                for c in conn.execute(
                    "SELECT * FROM retrieval_chunks WHERE document_id=? ORDER BY ordinal,rowid",
                    (document_id,),
                )
            ]
            if include_chunks
            else None
        )
        generation = _digest(
            [
                RECIPE,
                document_id,
                row["owner_id"],
                row["content_hash"],
                row["version"],
                fingerprint,
                row["index_revision"],
            ]
        )
        return dict(row), chunks, generation

    def pending(self, conn, document_id, fingerprint, limit=1):
        bound = self.binding(conn, document_id, fingerprint, include_chunks=False)
        if bound is None or not bound[0]["chunk_count"]:
            return []
        row = conn.execute("SELECT state FROM ragflow_documents WHERE generation=?", (bound[2],)).fetchone()
        return [{"generation": bound[2]}] if not row or row[0] != "ready" else []

    def retry_delay(self, conn, document_id, fingerprint):
        bound = self.binding(conn, document_id, fingerprint, include_chunks=False)
        if not bound:
            return 0
        next_at = conn.execute(
            "SELECT min(next_at) FROM ragflow_chunks WHERE generation=? AND state!='done'", (bound[2],)
        ).fetchone()[0]
        return max(0, (next_at or 0) - time.time())

    def reconcile_pending_intents(self, document_id, owner_id):
        """Read remote receipts for an exhausted generation without creating chunks."""
        fingerprint = profile()
        if not fingerprint:
            raise RAGFlowError("not_configured")
        with closing(self.db.connect()) as conn:
            bound = self.binding(conn, document_id, fingerprint, include_chunks=False)
            if not bound or bound[0]["owner_id"] != owner_id:
                return None
            doc, _chunks, generation = bound
            remote = conn.execute(
                """SELECT dataset_id,remote_id,state FROM ragflow_documents
                WHERE generation=? AND owner_id=? AND profile=?""",
                (generation, owner_id, fingerprint),
            ).fetchone()
            intents = {
                row["local_id"]: row["content_hash"]
                for row in conn.execute(
                    """SELECT local_id,content_hash FROM ragflow_chunks
                    WHERE generation=? AND state='intent' ORDER BY local_id LIMIT ?""",
                    (generation, MAX_RECONCILE_INTENTS + 1),
                )
            }
            local_chunks = {
                chunk["chunk_id"]: dict(chunk)
                for chunk in conn.execute(
                    """SELECT x.* FROM retrieval_chunks x JOIN ragflow_chunks c
                    ON c.local_id=x.chunk_id AND c.generation=? AND c.state='intent'
                    WHERE x.document_id=? ORDER BY x.chunk_id LIMIT ?""",
                    (generation, document_id, MAX_RECONCILE_INTENTS + 1),
                )
            }
        if len(intents) > MAX_RECONCILE_INTENTS:
            raise RAGFlowError("reconcile_intent_limit")
        if not remote or not remote["dataset_id"] or not remote["remote_id"] or not intents:
            return {
                "generation": generation,
                "source_hash": doc["content_hash"],
                "source_version": doc["version"],
                "index_revision": doc["index_revision"],
                "dataset_id": remote["dataset_id"] if remote else None,
                "remote_id": remote["remote_id"] if remote else None,
                "intents": intents,
                "matches": {},
            }
        client = RAGFlowClient()
        path = f"/datasets/{_identifier(remote['dataset_id'])}/documents/{_identifier(remote['remote_id'])}/chunks"
        wanted = {chunk_content(chunk): local_id for local_id, chunk in local_chunks.items()}
        matches = {}
        pages = 0
        remote_chunk_count = 0
        deadline = time.monotonic() + 15
        for page in range(1, 42):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RAGFlowError("reconcile_deadline")
            data = client.call(
                "GET",
                path,
                params={"page": page, "page_size": 500},
                timeout=min(5, remaining),
            )
            items = data.get("chunks", [])
            pages += 1
            remote_chunk_count += len(items)
            for item in items:
                if not isinstance(item, dict):
                    continue
                content = item.get("content_with_weight") or item.get("content")
                identifier = item.get("chunk_id") or item.get("id")
                local_id = wanted.get(content) if isinstance(content, str) else None
                if local_id and identifier:
                    matches[local_id] = {
                        "remote_id": _identifier(identifier),
                        "content_hash": intents[local_id],
                    }
            if len(matches) == len(wanted):
                break
            if len(items) < 500:
                break
        else:
            raise RAGFlowError("reconcile_limit")
        return {
            "generation": generation,
            "source_hash": doc["content_hash"],
            "source_version": doc["version"],
            "index_revision": doc["index_revision"],
            "dataset_id": remote["dataset_id"],
            "remote_id": remote["remote_id"],
            "intents": intents,
            "matches": matches,
            "remote_chunk_count": remote_chunk_count,
            "pages": pages,
        }

    def _guard(self, conn, doc, claim, generation, fingerprint):
        if not self.service.collection._valid_claim(conn, claim, True):
            raise RAGFlowError("lease_changed")
        if profile() != fingerprint or not self.service.archive.can_index(conn, doc["id"]):
            raise RAGFlowError("scope_changed")
        bound = self.binding(conn, doc["id"], fingerprint, include_chunks=False)
        if not bound or bound[2] != generation:
            raise RAGFlowError("source_changed")

    def _dataset(self, client, doc, claim, generation, fingerprint):
        name = "aitea-" + _digest([str(self.db.data_dir.resolve()), doc["owner_id"], fingerprint])[:48]
        with self.db.transaction() as conn:
            self._guard(conn, doc, claim, generation, fingerprint)
            conn.execute(
                "INSERT OR IGNORE INTO ragflow_datasets(owner_id,profile,name,created_at) VALUES(?,?,?,?)",
                (doc["owner_id"], fingerprint, name, now()),
            )
            row = conn.execute(
                "SELECT * FROM ragflow_datasets WHERE owner_id=? AND profile=?",
                (doc["owner_id"], fingerprint),
            ).fetchone()
        if row["remote_id"]:
            return _identifier(row["remote_id"])
        found = client.call("GET", "/datasets", params={"name": name, "page_size": 2})
        found = found if isinstance(found, list) else found.get("datasets", [])
        found = [item for item in found if item.get("name") == name]
        if len(found) > 1:
            raise RAGFlowError("ambiguous_dataset")
        result = (
            found[0]
            if found
            else client.call(
                "POST",
                "/datasets",
                payload={
                    "name": name,
                    "embedding_model": os.environ["RAGFLOW_EMBEDDING_MODEL"],
                    "permission": "me",
                    "parse_type": 1,
                    "parser_id": "general",
                    "language": "English",
                },
            )
        )
        remote_id = _identifier(result["id"])
        with self.db.transaction() as conn:
            self._guard(conn, doc, claim, generation, fingerprint)
            conn.execute(
                "UPDATE ragflow_datasets SET remote_id=? WHERE owner_id=? AND profile=?",
                (remote_id, doc["owner_id"], fingerprint),
            )
        return remote_id

    def step(self, doc, claim):
        fingerprint = profile()
        if not fingerprint:
            raise RAGFlowError("not_configured")
        with self.db.transaction() as conn:
            self.service.index.sync_document(conn, doc["id"])
            bound = self.binding(conn, doc["id"], fingerprint)
            if not bound or not bound[1]:
                return 0
            doc, chunks, generation = bound
            self._guard(conn, doc, claim, generation, fingerprint)
            conn.execute(
                "UPDATE processing_jobs SET lease_until=? WHERE document_id=? AND lease_token=?",
                (time.time() + 300, claim["document_id"], claim["lease_token"]),
            )
            name = "aitea-" + generation + Path(doc["filename"]).suffix.lower()
            conn.execute(
                """INSERT OR IGNORE INTO ragflow_documents(generation,document_id,owner_id,profile,source_hash,source_version,index_revision,name,state,chunk_count,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,'planned',?,?,?)""",
                (
                    generation,
                    doc["id"],
                    doc["owner_id"],
                    fingerprint,
                    doc["content_hash"],
                    doc["version"],
                    doc["index_revision"],
                    name,
                    len(chunks),
                    now(),
                    now(),
                ),
            )
            for chunk in chunks:
                conn.execute(
                    "INSERT OR IGNORE INTO ragflow_chunks(generation,local_id,input_hash,content_hash,state) VALUES(?,?,?,?,'planned')",
                    (generation, chunk["chunk_id"], chunk["input_hash"], chunk["content_hash"]),
                )
            remote = dict(
                conn.execute("SELECT * FROM ragflow_documents WHERE generation=?", (generation,)).fetchone()
            )
        client = RAGFlowClient()
        dataset = remote["dataset_id"] or self._dataset(client, doc, claim, generation, fingerprint)
        dataset = _identifier(dataset)
        if not remote["remote_id"]:
            result = client.call(
                "GET", f"/datasets/{dataset}/documents", params={"keywords": name, "page_size": 2}
            )
            found = [item for item in result.get("docs", []) if item.get("name") == name]
            if len(found) > 1:
                raise RAGFlowError("ambiguous_document")
            if found:
                remote_id = _identifier(found[0]["id"])
            else:
                _verify_existing(Path(doc["storage_path"]), doc["content_hash"], doc["size"])
                with open(doc["storage_path"], "rb") as source:
                    uploaded = client.call(
                        "POST",
                        f"/datasets/{dataset}/documents",
                        files={"file": (name, source, doc["mime_type"])},
                    )
                if not isinstance(uploaded, list) or len(uploaded) != 1:
                    raise RAGFlowError("upload_receipt")
                remote_id = _identifier(uploaded[0]["id"])
            with self.db.transaction() as conn:
                self._guard(conn, doc, claim, generation, fingerprint)
                conn.execute(
                    "UPDATE ragflow_documents SET dataset_id=?,remote_id=?,state='indexing',updated_at=? WHERE generation=?",
                    (dataset, remote_id, now(), generation),
                )
            # Never invoke RAGFlow's PDF parser: MinerU already parsed this file.
            return 1
        remote_id = _identifier(remote["remote_id"])
        path = f"/datasets/{dataset}/documents/{remote_id}/chunks"
        with closing(self.db.connect()) as conn:
            states = {
                row["local_id"]: dict(row)
                for row in conn.execute(
                    "SELECT * FROM ragflow_chunks WHERE generation=? AND state!='done'", (generation,)
                )
            }
        due = [
            chunk
            for chunk in chunks
            if chunk["chunk_id"] in states and states[chunk["chunk_id"]]["next_at"] <= time.time()
        ]
        # Only uncertain writes require a remote read. Page all bounded chunks
        # so a request lost after commit cannot pay for the same chunk again.
        unknown = any(states[c["chunk_id"]]["state"] == "intent" for c in due)
        existing = {}
        if unknown:
            wanted = {
                chunk_content(chunk): chunk["chunk_id"]
                for chunk in due
                if states[chunk["chunk_id"]]["state"] == "intent"
            }
            for page in range(1, 42):
                data = client.call("GET", path, params={"page": page, "page_size": 500})
                items = data.get("chunks", [])
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    # RAGFlow's chunks endpoint exposes indexed text as
                    # content_with_weight; accept content too for compatible
                    # versions and test doubles that return the create shape.
                    content = item.get("content_with_weight") or item.get("content")
                    if isinstance(content, str) and content in wanted:
                        existing[content] = _identifier(item.get("id") or item.get("chunk_id"))
                if len(existing) == len(wanted):
                    break
                if len(items) < 500:
                    break
            else:
                raise RAGFlowError("reconcile_limit")
        concurrency = int(os.getenv("RAGFLOW_CHUNK_CONCURRENCY", "8"))
        if not 1 <= concurrency <= 32:
            raise ValueError("RAGFLOW_CHUNK_CONCURRENCY 必须在 1 至 32 之间。")

        def add(chunk):
            state = states[chunk["chunk_id"]]
            content = chunk_content(chunk)
            known = existing.get(content)
            if state["state"] == "intent" and not known:
                # Ten minutes covers RAGFlow's documented server insertion
                # timeout. Absence still cannot prove the embedding was unbilled.
                age = time.time() - state["intent_at"]
                if age >= 900:
                    raise RAGFlowError("embedding_outcome_unknown", retry_after=3600)
                return
            with self.db.transaction() as conn:
                self._guard(conn, doc, claim, generation, fingerprint)
                if not known:
                    conn.execute(
                        "UPDATE ragflow_chunks SET state='intent',intent_at=?,next_at=?,attempts=attempts+1 WHERE generation=? AND local_id=?",
                        (time.time(), time.time() + 60, generation, chunk["chunk_id"]),
                    )
            if not known:
                result = client.call(
                    "POST",
                    path,
                    payload={
                        "content": content,
                        "important_keywords": [],
                        "tag_kwd": ["mineru", "source:" + doc["content_hash"]],
                        "user_id": hashlib.sha256(doc["owner_id"].encode()).hexdigest(),
                    },
                )
                known = _identifier(result["chunk"]["id"])
            with self.db.transaction() as conn:
                self._guard(conn, doc, claim, generation, fingerprint)
                conn.execute(
                    "UPDATE ragflow_chunks SET remote_id=?,state='done',error_code=NULL,next_at=0 WHERE generation=? AND local_id=?",
                    (known, generation, chunk["chunk_id"]),
                )

        # One bounded batch per lease yields fairly to parsing and other files.
        errors = []
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(add, chunk) for chunk in due[:concurrency]]
            for future in futures:
                try:
                    future.result()
                except Exception as exc:
                    errors.append(exc)
        if errors:
            raise errors[0]
        with self.db.transaction() as conn:
            self._guard(conn, doc, claim, generation, fingerprint)
            remaining = conn.execute(
                "SELECT count(*) FROM ragflow_chunks WHERE generation=? AND state!='done'", (generation,)
            ).fetchone()[0]
            if not remaining:
                conn.execute(
                    "UPDATE ragflow_documents SET state='ready',updated_at=? WHERE generation=?",
                    (now(), generation),
                )
        return remaining

    def cache_signature(self, conn, owner, case):
        """Invalidate analyses when this case gains a usable remote generation."""
        from app.contexts.documents.application.system_library import (
            public_document_sql,
            register_source_guard,
        )

        register_source_guard(conn)
        countries = set(self.service.legal.allowed_countries(case["template"]))
        for stage in case["template"].get("stages", []):
            countries.update(self.service.legal.allowed_countries(case["template"], stage))
        shared = "0"
        if countries:
            shared = "(" + public_document_sql("d") + " AND dc.country IN (SELECT value FROM json_each(?)))"
        args = [profile(), owner, case["id"]] + ([dump(sorted(countries))] if countries else [])
        digest = hashlib.sha256()
        for row in conn.execute(
            """SELECT remote.document_id,remote.generation,remote.remote_id FROM ragflow_documents remote
            JOIN documents d ON d.id=remote.document_id
            JOIN retrieval_documents catalog ON catalog.document_id=d.id
            LEFT JOIN document_countries dc ON dc.document_id=d.id
            WHERE remote.profile=? AND remote.state='ready' AND remote.source_hash=d.content_hash
            AND remote.source_version=d.version AND remote.index_revision=catalog.revision
            AND ((d.owner_id=? AND (d.case_id IS NULL OR d.case_id=?)) OR """
            + shared
            + ") ORDER BY remote.document_id,remote.generation",
            args,
        ):
            digest.update(dump(tuple(row)).encode())
        return digest.hexdigest()

    def retrieve(self, query, candidates, *, limit):
        """candidates are already filtered by the application's legal SQL gate."""
        if not candidates:
            return []
        if len(candidates) > 20000:
            raise RAGFlowError("narrow_query_scope")
        datasets = list(dict.fromkeys(row["dataset_id"] for row in candidates))
        documents = list(dict.fromkeys(row["remote_id"] for row in candidates))
        if not datasets or not documents:
            return []  # Empty document_ids would mean all remote documents.
        rerank = os.getenv("RAGFLOW_RERANK_MODEL", "").strip()
        data = RAGFlowClient().call(
            "POST",
            "/retrieval",
            payload={
                "question": query,
                "dataset_ids": datasets,
                "document_ids": documents,
                "page": 1,
                "page_size": min(100, max(48, limit)),
                "similarity_threshold": 0.15,
                "keywords_similarity_weight": 0.5,
                "knn_top_k": 512,
                "knn_num_candidates": 1024,
                "rerank_candidates_count": 48,
                "highlight": False,
                "use_kg": False,
                **({"rerank_id": rerank} if rerank else {}),
            },
        )
        chunks = data.get("chunks", [])
        if not isinstance(chunks, list) or len(chunks) > 100:
            raise RAGFlowError("retrieval_response")
        allowed = {row["remote_id"] for row in candidates}
        return [chunk for chunk in chunks if chunk.get("document_id") in allowed]
