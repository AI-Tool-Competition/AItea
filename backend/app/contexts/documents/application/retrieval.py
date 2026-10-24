"""Local source/citation catalog and authorization gate for RAGFlow retrieval.

Legacy vector tables are retained for historical backup compatibility. They are
not queried by the current retrieval or ingestion pipeline.
"""

from __future__ import annotations

import hashlib
import json
import math
from contextlib import closing
from datetime import datetime, timezone

from app.contexts.documents.domain.text import _terms
from app.contexts.documents.infrastructure.parsers.ingestion_quality import text_complete
from app.shared.domain.values import dump, now

EMBEDDING_RECIPE = "context-float32-v1"
CANDIDATES = 48
BACKFILL_BATCH = 20

_SCHEMA = (
    """CREATE TABLE retrieval_documents(
        document_id TEXT PRIMARY KEY REFERENCES documents(id), source_hash TEXT NOT NULL,
        complete INTEGER NOT NULL, chunk_count INTEGER NOT NULL, revision INTEGER NOT NULL)""",
    """CREATE TABLE retrieval_dirty(document_id TEXT PRIMARY KEY REFERENCES documents(id))""",
    """CREATE TABLE retrieval_chunks(
        rowid INTEGER PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id),
        chunk_id TEXT NOT NULL, ordinal INTEGER NOT NULL, text TEXT NOT NULL,
        retrieval_text TEXT NOT NULL, tokens TEXT NOT NULL, locator TEXT NOT NULL,
        content_hash TEXT NOT NULL, input_hash TEXT NOT NULL,
        UNIQUE(document_id,chunk_id))""",
    """CREATE TABLE retrieval_models(fingerprint TEXT PRIMARY KEY, dimension INTEGER NOT NULL
        CHECK(dimension BETWEEN 1 AND 65536))""",
    """CREATE TABLE retrieval_vectors(
        chunk_rowid INTEGER PRIMARY KEY REFERENCES retrieval_chunks(rowid) ON DELETE CASCADE,
        fingerprint TEXT NOT NULL REFERENCES retrieval_models(fingerprint), input_hash TEXT NOT NULL,
        dimension INTEGER NOT NULL, vector BLOB NOT NULL,
        CHECK(dimension BETWEEN 1 AND 65536 AND length(vector)=dimension*4))""",
    "CREATE INDEX retrieval_vectors_model ON retrieval_vectors(fingerprint,chunk_rowid)",
    """CREATE VIRTUAL TABLE retrieval_fts USING fts5(tokens,
        content='retrieval_chunks',content_rowid='rowid',tokenize='unicode61')""",
    """CREATE TRIGGER retrieval_chunk_insert AFTER INSERT ON retrieval_chunks BEGIN
        INSERT INTO retrieval_fts(rowid,tokens) VALUES(new.rowid,new.tokens); END""",
    """CREATE TRIGGER retrieval_chunk_delete AFTER DELETE ON retrieval_chunks BEGIN
        INSERT INTO retrieval_fts(retrieval_fts,rowid,tokens) VALUES('delete',old.rowid,old.tokens); END""",
    """CREATE TRIGGER retrieval_chunk_update AFTER UPDATE OF tokens ON retrieval_chunks BEGIN
        INSERT INTO retrieval_fts(retrieval_fts,rowid,tokens) VALUES('delete',old.rowid,old.tokens);
        INSERT INTO retrieval_fts(rowid,tokens) VALUES(new.rowid,new.tokens); END""",
    """CREATE TRIGGER retrieval_document_insert AFTER INSERT ON documents BEGIN
        INSERT OR IGNORE INTO retrieval_dirty VALUES(new.id); END""",
    """CREATE TRIGGER retrieval_document_update AFTER UPDATE OF manifest,content_hash,role ON documents
        BEGIN INSERT OR IGNORE INTO retrieval_dirty VALUES(new.id); END""",
)


def embedding_text(chunk):
    return str(chunk.get("retrieval_text") or chunk.get("text") or "")


def input_hash(text):
    return hashlib.sha256((EMBEDDING_RECIPE + "\0" + text).encode()).hexdigest()


def normalized_vector(vector):
    import numpy as np

    if not isinstance(vector, (list, tuple, np.ndarray)) or not 1 <= len(vector) <= 65536:
        raise ValueError("向量维度无效")
    if any(isinstance(value, (bool, np.bool_)) for value in vector):
        raise ValueError("向量必须为有限数值")
    try:
        with np.errstate(over="raise", invalid="raise"):
            values = np.asarray(vector, dtype="<f4")
        if values.ndim != 1 or not np.isfinite(values).all():
            raise ValueError("向量必须为有限数值")
        norm = math.sqrt(float(np.einsum("i,i->", values, values, dtype=np.float64, optimize=False)))
        if not norm or not math.isfinite(norm):
            raise ValueError("向量不能为零")
        return np.asarray(values / norm, dtype="<f4")
    except (TypeError, OverflowError, FloatingPointError) as exc:
        raise ValueError("向量必须为有限数值") from exc


def stored_vector(vector, dimension, expected_dimension):
    """Decode only a bounded, finite unit float32 checkpoint for numerical reuse."""
    import numpy as np

    if (
        type(dimension) is not int
        or not 1 <= dimension <= 65536
        or type(expected_dimension) is not int
        or not 1 <= expected_dimension <= 65536
        or dimension != expected_dimension
        or not isinstance(vector, bytes)
        or len(vector) != dimension * 4
    ):
        raise ValueError("缓存向量维度或长度无效")
    values = np.frombuffer(vector, dtype="<f4")
    norm = float(np.einsum("i,i->", values, values, dtype=np.float64, optimize=False))
    if not np.isfinite(values).all() or not math.isfinite(norm) or abs(norm - 1) > 0.0001:
        raise ValueError("缓存向量不是有限单位向量")
    return values.tolist()


class RetrievalIndex:
    def __init__(self, service):
        self.service, self.db = service, service.db
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=10").fetchone():
                for statement in _SCHEMA:
                    conn.execute(statement)
                # Upgrade is metadata-only. Each old document is backfilled once, on demand.
                conn.execute("INSERT INTO retrieval_dirty SELECT id FROM documents")
                conn.execute("INSERT INTO schema_migrations VALUES(10,?)", (now(),))
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=13").fetchone():
                # Eligibility was tightened for damaged decoded PDF characters.
                # Recheck old derived flags once, including published versions,
                # without editing their text, review history or model vectors.
                conn.execute("INSERT OR IGNORE INTO retrieval_dirty SELECT id FROM documents")
                conn.execute("INSERT INTO schema_migrations VALUES(13,?)", (now(),))
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=14").fetchone():
                # DOCX v1 retained only body paragraphs. Revalidate derived
                # completeness without rewriting published source or reviews.
                conn.execute("INSERT OR IGNORE INTO retrieval_dirty SELECT id FROM documents")
                conn.execute("INSERT INTO schema_migrations VALUES(14,?)", (now(),))
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=15").fetchone():
                # HTML v2 omitted source images; source PDF placeholders cannot
                # be repaired through OCR. Recheck derived completeness only.
                conn.execute("INSERT OR IGNORE INTO retrieval_dirty SELECT id FROM documents")
                conn.execute("INSERT INTO schema_migrations VALUES(15,?)", (now(),))

    def ensure_current(
        self,
        owner,
        *,
        case_id=None,
        collection=None,
        as_of=None,
        historical=False,
        country=None,
        document_ids=None,
        limit=BACKFILL_BATCH,
        source_scope=None,
    ):
        """Backfill at most one batch in the query's actual legal/account scope."""
        with closing(self.db.connect()) as conn:
            sql, parameters = self.service.archive.eligibility_sql(
                conn,
                owner,
                case_id,
                collection,
                as_of or datetime.now(timezone.utc).date(),
                historical,
                country,
                document_ids,
                require_current=False,
                source_scope=source_scope,
            )
        return self._backfill(sql, parameters, limit=limit)

    def _backfill(self, sql, parameters, *, limit=BACKFILL_BATCH):
        if not 0 <= limit <= BACKFILL_BATCH:
            raise ValueError("索引回填批次数量超过限制")
        with closing(self.db.connect()) as conn:
            ids = [
                row[0]
                for row in conn.execute(
                    "SELECT q.document_id FROM retrieval_dirty q JOIN ("
                    + sql
                    + ") e ON e.id=q.document_id ORDER BY q.document_id LIMIT ?",
                    (*parameters, limit),
                )
            ]
        processed = 0
        for document_id in ids:
            with self.db.transaction() as conn:
                # Recheck permission/review changes after acquiring the write transaction.
                if conn.execute(
                    "SELECT 1 FROM retrieval_dirty q JOIN ("
                    + sql
                    + ") e ON e.id=q.document_id WHERE q.document_id=?",
                    (*parameters, document_id),
                ).fetchone():
                    self.sync_document(conn, document_id)
                    processed += 1
        with closing(self.db.connect()) as conn:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM retrieval_dirty q JOIN (" + sql + ") e ON e.id=q.document_id",
                parameters,
            ).fetchone()[0]
        return {"processed": processed, "remaining": remaining}

    def prepare_scopes(self, owner, countries, *, case_id=None, historical=False, document_ids=None):
        """Share one preparation budget across an analysis's allowed countries."""
        # Legal search falls back to the unassigned scope when no configured
        # country resolves; prepare that same scope before taking its snapshot.
        countries = list(dict.fromkeys(countries)) or [None]
        if len(countries) > 22:
            raise ValueError("准备索引的国家范围超过限制")
        processed, remaining = 0, 0
        for country in countries:
            progress = self.ensure_current(
                owner,
                case_id=case_id,
                country=country,
                historical=historical,
                document_ids=document_ids,
                limit=BACKFILL_BATCH - processed,
            )
            processed += progress["processed"]
            remaining += progress["remaining"]
        return {"processed": processed, "remaining": remaining}

    def rebuild(self, owner, *, restart=False):
        """Explicit repair, one resumable batch per call; original versions stay intact.

        Set restart=True once to invalidate this owner's derived index, then call
        again with restart=False until remaining is zero. No model calls occur.
        """
        if restart:
            with self.db.transaction() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO retrieval_dirty SELECT id FROM documents WHERE owner_id=?",
                    (owner,),
                )
        return self._backfill("SELECT id FROM documents WHERE owner_id=?", (owner,))

    def sync_document(self, conn, document_id, manifest=None, *, strict_vectors=False):
        document = conn.execute(
            "SELECT id,role,content_hash,manifest FROM documents WHERE id=?", (document_id,)
        ).fetchone()
        if document is None:
            raise ValueError("资料不存在")
        manifest = manifest if manifest is not None else json.loads(document["manifest"])
        complete = int(
            document["role"] == "primary"
            and text_complete(manifest)
            and (not manifest.get("remote_parse") or manifest.get("source_hash") == document["content_hash"])
        )
        chunks = manifest.get("chunks", []) if document["role"] == "primary" else []
        previous = conn.execute(
            "SELECT * FROM retrieval_documents WHERE document_id=?", (document_id,)
        ).fetchone()
        changed = (
            not previous
            or previous["source_hash"] != document["content_hash"]
            or previous["complete"] != complete
        )
        conn.execute(
            """INSERT INTO retrieval_documents VALUES(?,?,?,0,0) ON CONFLICT(document_id) DO NOTHING""",
            (document_id, document["content_hash"], complete),
        )
        if previous and previous["source_hash"] != document["content_hash"]:
            conn.execute(
                "DELETE FROM retrieval_vectors WHERE chunk_rowid IN (SELECT rowid FROM retrieval_chunks WHERE document_id=?)",
                (document_id,),
            )
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS retrieval_seen(rowid INTEGER PRIMARY KEY)")
        conn.execute("DELETE FROM retrieval_seen")
        seen = set()
        removed_inline_vectors = False
        fingerprint = manifest.get("embedding_fingerprint")
        for ordinal, chunk in enumerate(chunks):
            text = chunk.get("text", "")
            if not isinstance(text, str) or not text.strip():
                continue  # Empty table/structural records remain in the manifest, never in retrieval.
            chunk_id = str(chunk.get("id") or f"chunk-{ordinal}")
            if chunk_id in seen:
                raise ValueError("分块标识重复，不能建立可靠索引")
            seen.add(chunk_id)
            context = embedding_text(chunk)
            hashed = input_hash(context)
            content_hash = hashlib.sha256(text.encode()).hexdigest()
            cursor = conn.execute(
                """INSERT INTO retrieval_chunks(document_id,chunk_id,ordinal,text,retrieval_text,tokens,locator,content_hash,input_hash)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(document_id,chunk_id) DO UPDATE SET
                ordinal=excluded.ordinal,text=excluded.text,retrieval_text=excluded.retrieval_text,tokens=excluded.tokens,
                locator=excluded.locator,content_hash=excluded.content_hash,input_hash=excluded.input_hash
                WHERE retrieval_chunks.ordinal!=excluded.ordinal OR retrieval_chunks.input_hash!=excluded.input_hash
                OR retrieval_chunks.content_hash!=excluded.content_hash OR retrieval_chunks.locator!=excluded.locator""",
                (
                    document_id,
                    chunk_id,
                    ordinal,
                    text,
                    context,
                    " ".join(_terms(context)),
                    dump(chunk.get("locator") or {}),
                    content_hash,
                    hashed,
                ),
            )
            changed |= bool(cursor.rowcount)
            row = conn.execute(
                "SELECT rowid,input_hash FROM retrieval_chunks WHERE document_id=? AND chunk_id=?",
                (document_id, chunk_id),
            ).fetchone()
            conn.execute("INSERT INTO retrieval_seen VALUES(?)", (row["rowid"],))
            deleted = conn.execute(
                "DELETE FROM retrieval_vectors WHERE chunk_rowid=? AND input_hash!=?", (row["rowid"], hashed)
            )
            changed |= bool(deleted.rowcount)
            if "embedding" in chunk:
                vector = chunk.pop("embedding")
                removed_inline_vectors = True
                if (
                    fingerprint
                    and complete
                    and self.service.archive.can_index(conn, document_id)
                    and chunk.get("embedding_input_hash", hashed) == hashed
                ):
                    try:
                        changed |= self.store_vectors(
                            conn, document_id, fingerprint, [dict(row)], [vector], touch=False
                        )
                    except ValueError:
                        if strict_vectors:
                            raise
                        # Legacy invalid vectors do not prevent text migration or enter the new index.
                        pass
        changed |= bool(
            conn.execute(
                """DELETE FROM retrieval_chunks WHERE document_id=?
            AND rowid NOT IN (SELECT rowid FROM retrieval_seen)""",
                (document_id,),
            ).rowcount
        )
        conn.execute(
            """UPDATE retrieval_documents SET source_hash=?,complete=?,chunk_count=?,revision=revision+?
            WHERE document_id=?""",
            (document["content_hash"], complete, len(seen), int(bool(changed)), document_id),
        )
        if removed_inline_vectors:
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), document_id))
        conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (document_id,))

    def pending(self, conn, document_id, fingerprint, limit=64):
        return self.service.ragflow.pending(conn, document_id, fingerprint, limit)

    def store_vectors(self, conn, document_id, fingerprint, chunks, vectors, *, touch=True):
        if len(chunks) != len(vectors):
            raise ValueError("向量数量与分块不一致")
        prepared = [normalized_vector(vector) for vector in vectors]
        dimensions = {len(vector) for vector in prepared}
        if not dimensions:
            return False
        if len(dimensions) != 1:
            raise ValueError("向量维度不一致")
        dimension = dimensions.pop()
        stored = conn.execute(
            "SELECT dimension FROM retrieval_models WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        if stored and stored["dimension"] != dimension:
            raise ValueError("当前模型的向量维度发生变化，请配置新模型标识后重建")
        # Check every input before changing any committed vector in this batch.
        for chunk in chunks:
            if not conn.execute(
                "SELECT 1 FROM retrieval_chunks WHERE rowid=? AND document_id=? AND input_hash=?",
                (chunk["rowid"], document_id, chunk["input_hash"]),
            ).fetchone():
                raise ValueError("分块内容已变更，请重新处理")
        conn.execute("INSERT OR IGNORE INTO retrieval_models VALUES(?,?)", (fingerprint, dimension))
        changed = False
        for chunk, vector in zip(chunks, prepared, strict=True):
            changed |= bool(
                conn.execute(
                    """INSERT INTO retrieval_vectors VALUES(?,?,?,?,?) ON CONFLICT(chunk_rowid) DO UPDATE SET
                fingerprint=excluded.fingerprint,input_hash=excluded.input_hash,dimension=excluded.dimension,vector=excluded.vector
                WHERE retrieval_vectors.fingerprint!=excluded.fingerprint OR retrieval_vectors.input_hash!=excluded.input_hash
                OR retrieval_vectors.vector!=excluded.vector""",
                    (chunk["rowid"], fingerprint, chunk["input_hash"], dimension, vector.tobytes()),
                ).rowcount
            )
        if touch and changed:
            conn.execute(
                "UPDATE retrieval_documents SET revision=revision+1 WHERE document_id=?", (document_id,)
            )
        return changed

    def search(
        self,
        owner,
        query,
        case_id,
        limit,
        as_of,
        collection,
        country,
        document_ids,
        historical,
        *,
        source_scope=None,
    ):
        from app.contexts.documents.infrastructure.providers.ragflow import RAGFlowError, configured

        if not isinstance(query, str) or len(query) > 4000 or not 1 <= limit <= 50:
            raise ValueError("检索问题或数量超过限制")
        mode = "ragflow" if configured() else "unavailable"
        result = {"items": [], "mode": mode, "reranked": False}
        if not configured():
            return result | {"warning": "RAGFlow 尚未配置，当前没有执行资料检索。"}
        progress = self.ensure_current(
            owner,
            case_id=case_id,
            collection=collection,
            as_of=as_of,
            historical=historical,
            country=country,
            document_ids=document_ids,
            source_scope=source_scope,
        )
        fingerprint = self.service.embedding_fingerprint()
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            if case_id:
                self.service.own_case(conn, owner, case_id)
            sql, parameters = self.service.archive.eligibility_sql(
                conn,
                owner,
                case_id,
                collection,
                as_of,
                historical,
                country,
                document_ids,
                source_scope=source_scope,
            )
            # Remote datasets never decide authorization or applicable law.
            conn.execute("CREATE TEMP TABLE retrieval_eligible AS " + sql, parameters)
            candidates = [
                dict(row)
                for row in conn.execute(
                    """SELECT r.*,e.metadata_revision,e.index_revision FROM ragflow_documents r
                JOIN retrieval_eligible e ON e.id=r.document_id
                WHERE r.state='ready' AND r.profile=? AND r.source_hash=e.content_hash
                AND r.source_version=e.version AND r.index_revision=e.index_revision ORDER BY r.document_id,r.generation LIMIT 20001""",
                    (fingerprint,),
                )
            ]
        if not candidates:
            return result | {"warning": "当前范围尚无已完成 RAGFlow 索引且通过资料审核的内容。"}
        try:
            remote = self.service.ragflow.retrieve(query, candidates, limit=limit)
        except RAGFlowError:
            return result | {"warning": "RAGFlow 检索暂不可用，请稍后重试。"}
        generations = {row["generation"]: row for row in candidates}
        wanted = [(str(hit.get("id", "")), str(hit.get("document_id", ""))) for hit in remote]
        hits = []
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            if case_id:
                self.service.own_case(conn, owner, case_id)
            sql, parameters = self.service.archive.eligibility_sql(
                conn,
                owner,
                case_id,
                collection,
                as_of,
                historical,
                country,
                document_ids,
                source_scope=source_scope,
            )
            conn.execute("CREATE TEMP TABLE retrieval_eligible AS " + sql, parameters)
            if wanted:
                rows = conn.execute(
                    """WITH wanted AS (
                        SELECT json_extract(value,'$[0]') remote_chunk,
                            json_extract(value,'$[1]') remote_document,key rank FROM json_each(?)
                    ) SELECT c.*,e.owner_id,e.title,e.source_url,e.version,e.metadata_payload,
                    e.metadata_revision,e.index_revision,r.generation FROM wanted w
                    JOIN ragflow_chunks m ON m.remote_id=w.remote_chunk
                    JOIN ragflow_documents r ON r.generation=m.generation AND r.remote_id=w.remote_document
                    JOIN retrieval_chunks c ON c.document_id=r.document_id AND c.chunk_id=m.local_id
                    JOIN retrieval_eligible e ON e.id=c.document_id
                    WHERE r.profile=? AND r.state='ready' AND m.state='done'
                    AND m.input_hash=c.input_hash AND m.content_hash=c.content_hash
                    AND r.source_hash=e.content_hash AND r.source_version=e.version
                    AND r.index_revision=e.index_revision ORDER BY w.rank""",
                    (dump(wanted), fingerprint),
                ).fetchall()
                seen = set()
                for row in rows:
                    original = generations.get(row["generation"])
                    identity = (row["document_id"], row["chunk_id"])
                    if (
                        identity not in seen
                        and original
                        and all(original[key] == row[key] for key in ("metadata_revision", "index_revision"))
                    ):
                        hits.append(dict(row))
                        seen.add(identity)
        result["items"] = [self.citation(hit, query) for hit in hits[:limit]]
        if len(hits) < len(wanted) or progress["remaining"]:
            result["warning"] = "部分资料仍在索引或检索期间状态发生变更，已排除未核实引用。"
        return result

    @staticmethod
    def citation(hit, query):
        from app.contexts.documents.application.system_library import source_scope

        text = hit["text"]
        # Keep ordinary legal chunks complete. Exceptionally long imported chunks
        # get a verbatim window around the first matched term, with explicit offsets.
        start, end = 0, len(text)
        if len(text) > 5000:
            lower = text.casefold()
            positions = [lower.find(term) for term in _terms(query) if term in lower]
            start = max(0, (min(positions) if positions else 0) - 1000)
            end = min(len(text), start + 5000)
        locator = json.loads(hit["locator"]) | {
            "chunk_id": hit["chunk_id"],
            "chunk_hash": hit["content_hash"],
        }
        if start or end != len(text):
            locator |= {"excerpt_char_start": start, "excerpt_char_end": end}
        return {
            "source_scope": source_scope(hit["owner_id"]),
            "document_id": hit["document_id"],
            "title": hit["title"],
            "excerpt": text[start:end],
            "locator": locator,
            "source_url": hit["source_url"],
            "document_version": hit["version"],
            "metadata": json.loads(hit["metadata_payload"]),
            "metadata_revision": hit["metadata_revision"],
            "chunk_id": hit["chunk_id"],
            "chunk_hash": hit["content_hash"],
        }
