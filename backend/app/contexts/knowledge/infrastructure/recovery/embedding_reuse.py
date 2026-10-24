"""Bounded numerical reuse within one owner, model and validated input.

Warm reuse defaults to identical legal originals; explicit input-cache mode
also accepts exact retrieval inputs from other verified originals. An operator
may keep durable HTTP receipts for a frozen plan. Neither path copies approval,
source text or locators. The caller retains its live target lease/transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from threading import Condition, Lock

from app.contexts.knowledge.application.retrieval import input_hash, stored_vector
from app.contexts.knowledge.infrastructure.parsing.ingestion_quality import text_complete
from app.shared.domain.values import dump, now
from app.shared.infrastructure.blob_store import _verify_existing

PROTOCOL = "same-owner-original-embedding-reuse-v1"
INPUT_PROTOCOL = "same-owner-input-embedding-reuse-v1"
MAX_DONORS = 8
MAX_BATCH = 128
MAX_MANIFEST_BYTES = 40 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024
MAX_PROVENANCE_BYTES = 4 * 1024 * 1024
MAX_PROVENANCE_RECORDS = 4096


class InputCheckpointCache:
    """Private operator-run receipts, scoped to a frozen owner/model/source plan.

    A successful HTTP batch is fsynced independently of the application's claim.
    A later failed sibling can therefore resume without paying for that result
    again. This cache transfers numbers only, never authorization or citations.
    """

    def __init__(self, path, scope):
        self.path = Path(path)
        self.condition = Condition(Lock())
        self.inflight = set()
        self.hits = self.saved = 0
        if not isinstance(scope, dict) or not all(
            scope.get(k) for k in ("owner", "fingerprint", "source_plan")
        ):
            raise ValueError("向量检查点必须限定账号、模型和原件计划")
        encoded = dump(scope)
        with closing(self._connect()) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS scope(version INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS inputs(input_hash TEXT PRIMARY KEY,text_sha256 TEXT NOT NULL,"
                "dimension INTEGER NOT NULL,vector BLOB NOT NULL,vector_sha256 TEXT NOT NULL)"
            )
            rows = conn.execute("SELECT version,payload FROM scope").fetchall()
            if rows and rows != [(1, encoded)]:
                raise ValueError("向量检查点账号、模型或原件计划不一致")
            conn.execute("INSERT OR IGNORE INTO scope VALUES(1,?)", (encoded,))
            conn.commit()
        self.path.chmod(0o600)

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _read(self, conn, text):
        row = conn.execute(
            "SELECT text_sha256,dimension,vector,vector_sha256 FROM inputs WHERE input_hash=?",
            (input_hash(text),),
        ).fetchone()
        if row is None:
            return None
        text_sha, dimension, vector, vector_sha = row
        if text_sha != _hash(text.encode()) or vector_sha != _hash(vector):
            raise ValueError("向量检查点正文或向量校验失败")
        return stored_vector(vector, dimension, dimension)

    def save(self, texts, vectors):
        from app.contexts.knowledge.application.retrieval import normalized_vector

        if len(texts) != len(vectors) or not 1 <= len(texts) <= 128:
            raise ValueError("向量检查点批次无效")
        prepared = [normalized_vector(v) for v in vectors]
        if len({len(v) for v in prepared}) != 1:
            raise ValueError("向量检查点批次维度不一致")
        with self.condition, closing(self._connect()) as conn, conn:
            existing = conn.execute("SELECT dimension FROM inputs LIMIT 1").fetchone()
            if existing and existing[0] != len(prepared[0]):
                raise ValueError("向量检查点模型维度发生变化")
            for text, vector in zip(texts, prepared, strict=True):
                if self._read(conn, text) is not None:
                    continue
                raw = vector.tobytes()
                conn.execute(
                    "INSERT INTO inputs VALUES(?,?,?,?,?)",
                    (input_hash(text), _hash(text.encode()), len(vector), raw, _hash(raw)),
                )
                self.saved += 1

    def embed(self, texts, provider):
        if not texts:
            return []
        if len(texts) > 128 or any(
            not isinstance(t, str) or len(t.encode()) > MAX_INPUT_BYTES for t in texts
        ):
            raise ValueError("向量检查点输入超过限制")
        unique = dict(zip((input_hash(t) for t in texts), texts, strict=True))
        with self.condition:
            self.condition.wait_for(lambda: not self.inflight.intersection(unique))
            with closing(self._connect()) as conn:
                resolved = {key: self._read(conn, text) for key, text in unique.items()}
            missing = {key: unique[key] for key, result in resolved.items() if result is None}
            self.hits += len(texts) - len(missing)
            self.inflight.update(missing)
        try:
            if missing:
                # The provider checkpoints every validated HTTP batch, not only
                # the aggregate return from multiple potentially failing lanes.
                values = provider(list(missing.values()), on_batch=self.save)
                if values is None or len(values) != len(missing):
                    raise ValueError("向量检查点未收到完整结果")
                self.save(list(missing.values()), values)
            with closing(self._connect()) as conn:
                result = [self._read(conn, text) for text in texts]
            if any(value is None for value in result):
                raise ValueError("成功响应缺少持久向量检查点")
            return result
        finally:
            with self.condition:
                self.inflight.difference_update(missing)
                self.condition.notify_all()

    def snapshot(self):
        with self.condition, closing(self._connect()) as conn:
            return {
                "saved_inputs": conn.execute("SELECT COUNT(*) FROM inputs").fetchone()[0],
                "cache_hits_this_process": self.hits,
                "new_saved_this_process": self.saved,
                "inflight_inputs": len(self.inflight),
            }


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _document(service, conn, document_id, owner, *, input_cache=False):
    row = conn.execute(
        """SELECT id,owner_id,role,version,family_id,content_hash,source_url,size,
        storage_path,status,CASE WHEN length(CAST(manifest AS BLOB))<=? THEN manifest END manifest
        FROM documents WHERE id=? AND owner_id=?""",
        (MAX_MANIFEST_BYTES, document_id, owner),
    ).fetchone()
    if row is None or row["manifest"] is None:
        raise ValueError("向量处理资料或正文检查点无效")
    metadata = conn.execute(
        """SELECT revision,CASE WHEN length(CAST(payload AS BLOB))<=65536 THEN payload END payload
        FROM document_metadata WHERE document_id=?""",
        (document_id,),
    ).fetchone()
    classes = [
        dict(r)
        for r in conn.execute(
            """SELECT collection,level,revision,CASE WHEN length(CAST(tags AS BLOB))<=65536 THEN tags END tags
            FROM legal_classifications WHERE document_id=? ORDER BY collection LIMIT 65""",
            (document_id,),
        )
    ]
    if (
        len(classes) > 64
        or any(r["tags"] is None for r in classes)
        or (metadata and metadata["payload"] is None)
    ):
        raise ValueError("向量复用资料元数据超过限制")
    country = conn.execute(
        "SELECT country FROM document_countries WHERE document_id=?", (document_id,)
    ).fetchone()
    index = conn.execute(
        "SELECT source_hash,complete,revision FROM retrieval_documents WHERE document_id=?",
        (document_id,),
    ).fetchone()
    dirty = conn.execute("SELECT 1 FROM retrieval_dirty WHERE document_id=?", (document_id,)).fetchone()
    latest = conn.execute(
        """SELECT id FROM documents INDEXED BY documents_family WHERE owner_id=? AND family_id=?
        ORDER BY version DESC,created_at DESC,id DESC LIMIT 1""",
        (owner, row["family_id"]),
    ).fetchone()
    manifest = json.loads(row["manifest"])
    eligible = (
        row["role"] == "primary"
        and row["status"] in ({"ready", "processing"} if input_cache else {"ready"})
        and bool(country)
        and any(
            r["collection"] == "country:" + country["country"] and r["level"] == "primary" for r in classes
        )
        and service.archive.can_index(conn, document_id)
        and index is not None
        and index["source_hash"] == row["content_hash"]
        and index["complete"] == 1
        and not dirty
        and latest is not None
        and latest["id"] == document_id
        and manifest.get("source_hash") == row["content_hash"]
        and text_complete(manifest)
    )
    return {
        **{k: row[k] for k in row.keys() if k != "manifest"},
        "manifest_sha256": _hash(row["manifest"].encode()),
        "metadata": dict(metadata) if metadata else None,
        "classifications": classes,
        "country": country["country"] if country else None,
        "index": dict(index) if index else None,
        "dirty": bool(dirty),
        "latest_document_id": latest["id"] if latest else None,
        "eligible": bool(eligible),
    }


def _chunk(conn, document_id, chunk_id):
    row = conn.execute(
        """SELECT rowid,chunk_id,ordinal,text,retrieval_text,locator,content_hash,input_hash
        FROM retrieval_chunks WHERE document_id=? AND chunk_id=?""",
        (document_id, chunk_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if (
        not isinstance(result["retrieval_text"], str)
        or len(result["retrieval_text"].encode()) > MAX_INPUT_BYTES
        or len(result["text"].encode()) > MAX_INPUT_BYTES
        or result["input_hash"] != input_hash(result["retrieval_text"])
        or result["content_hash"] != _hash(result["text"].encode())
    ):
        raise ValueError("向量分块输入校验失败")
    return result


def prepare_batch(service, conn, target, fingerprint, batch):
    """Capture narrow candidates in a short transaction; perform no raw-file I/O."""
    if not isinstance(batch, list) or not 1 <= len(batch) <= MAX_BATCH:
        raise ValueError("向量复用批次数量超过限制")
    if not isinstance(fingerprint, str) or not 0 < len(fingerprint) <= 200:
        raise ValueError("向量复用模型标识无效")
    target = dict(target)
    setting = os.getenv("EMBEDDING_INPUT_CACHE", "false").strip().lower()
    if setting not in {"true", "false"}:
        raise ValueError("EMBEDDING_INPUT_CACHE 必须为 true 或 false。")
    input_cache = setting == "true"
    current = _document(service, conn, target["id"], target["owner_id"], input_cache=input_cache)
    if any(
        current[k] != target[k]
        for k in ("owner_id", "role", "version", "content_hash", "source_url", "size", "storage_path")
    ):
        raise ValueError("向量处理原件版本已变更")
    chunks = []
    for requested in batch:
        actual = _chunk(conn, target["id"], requested["chunk_id"])
        if actual is None or any(
            actual[k] != requested[k] for k in ("rowid", "input_hash", "retrieval_text")
        ):
            raise ValueError("向量分块内容已变更")
        chunks.append(actual)
    if len({c["rowid"] for c in chunks}) != len(chunks):
        raise ValueError("向量分块重复")
    plan = {
        "target": current,
        "fingerprint": fingerprint,
        "chunks": chunks,
        "hits": {},
        "misses": list(range(len(chunks))),
        "files": [],
        "raw_verified": False,
        "input_cache": input_cache,
    }
    if not current["eligible"]:
        return plan
    if input_cache:
        return _prepare_input_hits(service, conn, plan)
    rows = conn.execute(
        """SELECT id FROM documents INDEXED BY documents_source
        WHERE owner_id=? AND source_url IS ? AND content_hash=? AND version=? AND role='primary'
        AND id!=? ORDER BY rowid LIMIT ?""",
        (
            current["owner_id"],
            current["source_url"],
            current["content_hash"],
            current["version"],
            current["id"],
            MAX_DONORS,
        ),
    ).fetchall()
    dimension = conn.execute(
        "SELECT dimension FROM retrieval_models WHERE fingerprint=?", (fingerprint,)
    ).fetchone()
    donors = []
    for row in rows:
        donor = _document(service, conn, row["id"], current["owner_id"])
        if donor["eligible"] and donor["size"] == current["size"]:
            donors.append(donor)
    for n, chunk in enumerate(chunks):
        for donor in donors:
            dc = _chunk(conn, donor["id"], chunk["chunk_id"])
            if dc is None or any(
                dc[k] != chunk[k] for k in ("input_hash", "retrieval_text", "text", "content_hash")
            ):
                continue
            vector = conn.execute(
                "SELECT fingerprint,input_hash,dimension,vector FROM retrieval_vectors WHERE chunk_rowid=?",
                (dc["rowid"],),
            ).fetchone()
            if (
                vector is None
                or vector["fingerprint"] != fingerprint
                or vector["input_hash"] != chunk["input_hash"]
            ):
                continue
            if dimension is None:
                raise ValueError("缓存向量模型维度不存在")
            values = stored_vector(vector["vector"], vector["dimension"], dimension["dimension"])
            plan["hits"][n] = {
                "donor": donor,
                "donor_chunk": dc,
                "dimension": vector["dimension"],
                "donor_vector_sha256": _hash(vector["vector"]),
                "values": values,
            }
            break
    plan["misses"] = [n for n in range(len(chunks)) if n not in plan["hits"]]
    return plan


def _prepare_input_hits(service, conn, plan):
    """Copy only numerical inputs within an owner, retaining both source fences.

    Scan the current model once for this bounded batch, and return at most eight
    source candidates per input. No source text, locator or approval is copied.
    """
    current, chunks, fingerprint = plan["target"], plan["chunks"], plan["fingerprint"]
    hashes = list(dict.fromkeys(c["input_hash"] for c in chunks))
    placeholders = ",".join("?" for _ in hashes)
    rows = conn.execute(
        f"""SELECT input_hash,document_id,chunk_id FROM (
          SELECT v.input_hash,c.document_id,c.chunk_id,
            ROW_NUMBER() OVER(PARTITION BY v.input_hash ORDER BY c.document_id,c.rowid) candidate
          FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid
          JOIN documents d ON d.id=c.document_id
          WHERE v.fingerprint=? AND v.input_hash IN ({placeholders})
            AND v.input_hash=c.input_hash AND d.owner_id=? AND d.role='primary'
        ) WHERE candidate<=?""",
        (fingerprint, *hashes, current["owner_id"], MAX_DONORS),
    ).fetchall()
    candidates, documents = {}, {}
    dimension = conn.execute(
        "SELECT dimension FROM retrieval_models WHERE fingerprint=?", (fingerprint,)
    ).fetchone()
    for row in rows:
        candidates.setdefault(row["input_hash"], []).append(row)
    for n, chunk in enumerate(chunks):
        for row in candidates.get(chunk["input_hash"], []):
            ident = row["document_id"]
            if ident not in documents:
                documents[ident] = _document(service, conn, ident, current["owner_id"], input_cache=True)
            donor = documents[ident]
            if not donor["eligible"]:
                continue
            dc = _chunk(conn, ident, row["chunk_id"])
            if dc is None or any(dc[k] != chunk[k] for k in ("input_hash", "retrieval_text")):
                continue
            vector = conn.execute(
                "SELECT fingerprint,input_hash,dimension,vector FROM retrieval_vectors WHERE chunk_rowid=?",
                (dc["rowid"],),
            ).fetchone()
            if (
                vector is None
                or vector["fingerprint"] != fingerprint
                or vector["input_hash"] != chunk["input_hash"]
            ):
                continue
            if dimension is None:
                raise ValueError("缓存向量模型维度不存在")
            values = stored_vector(vector["vector"], vector["dimension"], dimension["dimension"])
            plan["hits"][n] = {
                "donor": donor,
                "donor_chunk": dc,
                "dimension": vector["dimension"],
                "donor_vector_sha256": _hash(vector["vector"]),
                "values": values,
            }
            break
    plan["misses"] = [n for n in range(len(chunks)) if n not in plan["hits"]]
    return plan


def verify_originals(service, plan):
    """Verify shared immutable account CAS once, outside the SQLite write lock."""
    if not plan["target"]["eligible"]:
        return
    identities = [plan["target"], *(h["donor"] for h in plan["hits"].values())]
    checked = set()
    for row in identities:
        digest = row["content_hash"]
        owner_hash = _hash(row["owner_id"].encode())
        expected = service.archive.blobs.blobs_dir / owner_hash / digest[:2] / digest[2:4] / digest
        if (
            not re_hash(digest)
            or type(row["size"]) is not int
            or not 0 < row["size"] <= 128 * 1024 * 1024
            or Path(row["storage_path"]) != expected
        ):
            raise ValueError("向量处理原件不在账号内容存储内")
        if str(expected) in checked:
            continue
        before = _file_state(expected, service.db.files_dir.resolve())
        _verify_existing(expected, digest, row["size"])
        after = _file_state(expected, service.db.files_dir.resolve())
        if before != after:
            raise ValueError("向量处理原件核验时发生变化")
        plan["files"].append({"path": str(expected), "state": after})
        checked.add(str(expected))
    plan["raw_verified"] = True


def re_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _file_state(path, root):
    result = []
    for current in [path, *path.parents]:
        info = current.lstat()
        if current == path:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("向量处理原件必须是普通文件")
            result.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("向量处理原件路径无效")
        else:
            result.append((info.st_dev, info.st_ino))
        if current == root:
            return result
    raise ValueError("向量处理原件路径越界")


def assert_current(service, conn, plan):
    """The caller rechecks its live lease before this transaction-bound fence."""
    target = plan["target"]
    if target["eligible"] and (not plan["raw_verified"] or not plan["files"]):
        raise ValueError("向量处理原件尚未完成核验")
    if (
        service.embedding_fingerprint() != plan["fingerprint"]
        or _document(service, conn, target["id"], target["owner_id"], input_cache=plan["input_cache"])
        != target
    ):
        raise ValueError("向量处理资料、模型或索引已变更")
    for file in plan["files"]:
        if _file_state(Path(file["path"]), service.db.files_dir.resolve()) != file["state"]:
            raise ValueError("向量处理原件已变更")
    for chunk in plan["chunks"]:
        if _chunk(conn, target["id"], chunk["chunk_id"]) != chunk:
            raise ValueError("向量处理分块已变更")
    checked_donors = set()
    for hit in plan["hits"].values():
        donor = hit["donor"]
        if donor["id"] not in checked_donors:
            if (
                _document(service, conn, donor["id"], donor["owner_id"], input_cache=plan["input_cache"])
                != donor
            ):
                raise ValueError("复用来源资料或索引已变更")
            checked_donors.add(donor["id"])
        if _chunk(conn, donor["id"], hit["donor_chunk"]["chunk_id"]) != hit["donor_chunk"]:
            raise ValueError("复用来源分块已变更")
        row = conn.execute(
            "SELECT fingerprint,input_hash,dimension,vector FROM retrieval_vectors WHERE chunk_rowid=?",
            (hit["donor_chunk"]["rowid"],),
        ).fetchone()
        if (
            row is None
            or row["fingerprint"] != plan["fingerprint"]
            or row["input_hash"] != hit["donor_chunk"]["input_hash"]
            or row["dimension"] != hit["dimension"]
            or _hash(row["vector"]) != hit["donor_vector_sha256"]
        ):
            raise ValueError("复用来源向量已变更")
        stored_vector(row["vector"], row["dimension"], hit["dimension"])


def record_commit(conn, plan, manifest):
    """Record actual normalized target bytes after existing store_vectors succeeds."""
    previous = manifest.get("embedding_reuse")
    if previous is not None and (
        not isinstance(previous, dict) or previous.get("protocol") not in {PROTOCOL, INPUT_PROTOCOL}
    ):
        raise ValueError("向量复用检查点无效")
    records = (
        dict(previous.get("records", {}))
        if previous and previous.get("fingerprint") == plan["fingerprint"]
        else {}
    )
    if len(records) > MAX_PROVENANCE_RECORDS:
        raise ValueError("向量复用检查点超过限制")
    target = plan["target"]
    for n, chunk in enumerate(plan["chunks"]):
        if n not in plan["hits"]:
            records.pop(chunk["chunk_id"], None)
            continue
        hit = plan["hits"][n]
        row = conn.execute(
            "SELECT vector FROM retrieval_vectors WHERE chunk_rowid=?", (chunk["rowid"],)
        ).fetchone()
        donor = hit["donor"]
        records[chunk["chunk_id"]] = {
            "match_policy": "exact_input" if plan["input_cache"] else "same_original",
            "source_document_id": donor["id"],
            "source_document_version": donor["version"],
            "source_hash": donor["content_hash"],
            "source_url": donor["source_url"],
            "source_index_revision": donor["index"]["revision"],
            "source_metadata_revision": donor["metadata"]["revision"] if donor["metadata"] else 0,
            "source_classifications_sha256": _hash(dump(donor["classifications"]).encode()),
            "target_document_id": target["id"],
            "target_document_version": target["version"],
            "target_source_hash": target["content_hash"],
            "target_country": target["country"],
            "target_index_revision_before": target["index"]["revision"],
            "target_metadata_revision": target["metadata"]["revision"] if target["metadata"] else 0,
            "target_classifications_sha256": _hash(dump(target["classifications"]).encode()),
            "chunk_id": chunk["chunk_id"],
            "input_hash": chunk["input_hash"],
            "retrieval_text_sha256": _hash(chunk["retrieval_text"].encode()),
            "fingerprint": plan["fingerprint"],
            "dimension": hit["dimension"],
            "donor_vector_sha256": hit["donor_vector_sha256"],
            "target_vector_sha256": _hash(row["vector"]),
            "store_vectors_renormalized": True,
            "bytes_identical_after_normalization": _hash(row["vector"]) == hit["donor_vector_sha256"],
            "target_locators_or_approval_copied": False,
        }
    checkpoint = {
        "protocol": INPUT_PROTOCOL if plan["input_cache"] else PROTOCOL,
        "fingerprint": plan["fingerprint"],
        "records": records,
        "current_reused_vector_records": len(records),
        "last_batch": {
            "committed_at": now(),
            "input_chunks": len(plan["chunks"]),
            "reused_vectors": len(plan["hits"]),
            "provider_input_chunks": len(plan["misses"]),
            "embed_texts_invocations": int(bool(plan["misses"])),
            "external_wire_calls_measured": False,
        },
    }
    if len(records) > MAX_PROVENANCE_RECORDS or len(dump(checkpoint).encode()) > MAX_PROVENANCE_BYTES:
        raise ValueError("向量复用检查点超过限制")
    return checkpoint
