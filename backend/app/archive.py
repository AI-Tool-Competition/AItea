"""Immutable original versions and explicit, collection-scoped legal relationships."""

import hashlib
import json
import re
from contextlib import closing
from pathlib import Path
from urllib.parse import urldefrag

from .blob_store import BlobStore
from .db import dump, now, uid


def family_key(
    owner, filename, source_url=None, case_id=None, role="primary", parent_id=None, identity_key=None
):
    identity = (
        ("key", identity_key)
        if identity_key
        else (("url", urldefrag(source_url)[0]) if source_url else ("filename", filename))
    )
    return hashlib.sha256(dump([owner, case_id, role, parent_id, identity]).encode()).hexdigest()


class LegalArchive:
    def __init__(self, service):
        self.service, self.db = service, service.db
        self.blobs = BlobStore(self.db.files_dir)
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=4").fetchone():
                return
            conn.execute("ALTER TABLE documents ADD COLUMN family_id TEXT")
            cursor = ""
            while rows := conn.execute(
                "SELECT id,owner_id,filename,source_url,case_id,role,parent_id FROM documents WHERE id>? ORDER BY id LIMIT 500",
                (cursor,),
            ).fetchall():
                for row in rows:
                    family = family_key(
                        row["owner_id"],
                        row["filename"],
                        row["source_url"],
                        row["case_id"],
                        row["role"],
                        row["parent_id"],
                    )
                    conn.execute("UPDATE documents SET family_id=? WHERE id=?", (family, row["id"]))
                cursor = rows[-1]["id"]
            conn.execute("CREATE INDEX documents_family ON documents(owner_id,family_id,version DESC)")
            conn.execute("CREATE INDEX documents_family_hash ON documents(family_id,content_hash)")
            conn.execute("""CREATE TABLE legal_classifications(
                document_id TEXT NOT NULL REFERENCES documents(id), collection TEXT NOT NULL,
                level TEXT NOT NULL CHECK(level IN ('primary','secondary')), tags TEXT NOT NULL,
                revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(document_id,collection))""")
            conn.execute(
                "CREATE INDEX classifications_collection ON legal_classifications(collection,level,document_id)"
            )
            conn.execute("""CREATE TABLE legal_classification_history(
                document_id TEXT NOT NULL REFERENCES documents(id), collection TEXT NOT NULL,
                revision INTEGER NOT NULL, payload TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL, PRIMARY KEY(document_id,collection,revision))""")
            conn.execute("""CREATE TABLE legal_relations(
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), collection TEXT NOT NULL,
                source_document_id TEXT NOT NULL REFERENCES documents(id), target_document_id TEXT NOT NULL REFERENCES documents(id),
                relation TEXT NOT NULL CHECK(relation IN ('cites','related')), locator TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(collection,source_document_id,target_document_id,relation,locator))""")
            conn.execute(
                "CREATE INDEX relations_source ON legal_relations(owner_id,source_document_id,created_at,id)"
            )
            conn.execute(
                "CREATE INDEX relations_target ON legal_relations(owner_id,target_document_id,created_at,id)"
            )
            conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (4, now()))

    def enrich(self, document, conn=None):
        if conn is None:
            with closing(self.db.connect()) as conn:
                return self.enrich(document, conn)
        rows = conn.execute(
            "SELECT collection,level,tags,revision FROM legal_classifications WHERE document_id=? ORDER BY collection",
            (document["id"],),
        ).fetchall()
        return self.with_classifications(document, rows)

    def with_classifications(self, document, rows):
        classifications = [
            {key: row[key] for key in ("collection", "level", "revision")} | {"tags": json.loads(row["tags"])}
            for row in rows
        ]
        level = (
            "attachment"
            if document["role"] == "secondary"
            else ("secondary" if rows and all(row["level"] == "secondary" for row in rows) else "primary")
        )
        tags = (
            list(dict.fromkeys(tag for item in classifications for tag in item["tags"]))
            if classifications
            else document["tags"]
        )
        return document | {"classifications": classifications, "legal_level": level, "tags": tags}

    def can_index(self, conn, document_id, collection=None):
        rows = conn.execute(
            "SELECT collection,level FROM legal_classifications WHERE document_id=?", (document_id,)
        ).fetchall()
        if collection:
            return any(row["collection"] == collection and row["level"] == "primary" for row in rows)
        return not rows or any(row["level"] == "primary" for row in rows)

    def classify(self, conn, owner, document, collection, level, tags, *, expected_revision=None):
        from .service import fail

        if document["role"] != "primary":
            fail(422, "attachment_classification", "普通附件不能设为法规，请单独导入法规原件")
        previous = conn.execute(
            "SELECT * FROM legal_classifications WHERE document_id=? AND collection=?",
            (document["id"], collection),
        ).fetchone()
        was_indexable = self.can_index(conn, document["id"])
        revision = previous["revision"] if previous else 0
        if expected_revision is not None and expected_revision != revision:
            fail(409, "stale_classification", "资料标签或分级已有更新，请刷新后保存")
        if expected_revision is None and previous:
            if previous["level"] != level:
                fail(409, "classification_conflict", "同一采集组已指定不同分级，请先确认资料分级")
            tags = list(dict.fromkeys([*json.loads(previous["tags"]), *tags]))
        tags = list(dict.fromkeys(tags))
        if len(tags) > 20:
            fail(422, "too_many_tags", "合并后的标签超过 20 项，请整理标签后重试")
        if previous and previous["level"] == level and json.loads(previous["tags"]) == tags:
            return
        if (
            not previous
            and conn.execute(
                "SELECT COUNT(*) FROM legal_classifications WHERE document_id=?", (document["id"],)
            ).fetchone()[0]
            >= 20
        ):
            fail(422, "too_many_collections", "每份资料最多加入 20 个采集组")
        revision += 1
        at = now()
        conn.execute(
            """INSERT INTO legal_classifications VALUES(?,?,?,?,?,?)
            ON CONFLICT(document_id,collection) DO UPDATE SET level=excluded.level,tags=excluded.tags,
            revision=excluded.revision,updated_at=excluded.updated_at""",
            (document["id"], collection, level, dump(tags), revision, at),
        )
        conn.execute(
            "INSERT INTO legal_classification_history VALUES(?,?,?,?,?,?)",
            (
                document["id"],
                collection,
                revision,
                dump({"level": level, "tags": tags}),
                owner,
                at,
            ),
        )
        # All classification entry points, including relation anchors, share this transition.
        if not was_indexable and self.can_index(conn, document["id"]):
            self.service.collection.queue_processing(conn, document["id"], owner)

    def patch_classification(self, owner, document_id, payload):
        with self.db.transaction() as conn:
            doc = self.service.own_document(conn, owner, document_id)
            self.classify(conn, owner, doc, **payload)
            if self.can_index(conn, document_id):
                self.service.collection.queue_processing(conn, document_id, owner)
            self.service.metadata_changed(conn, doc)
            return self.service.document_dict(doc, conn=conn)

    def versions(self, owner, document_id, limit=20, offset=0):
        from .service import DOCUMENT_SUMMARY

        with closing(self.db.connect()) as conn:
            doc = self.service.own_document(conn, owner, document_id)
            rows = conn.execute(
                f"SELECT {DOCUMENT_SUMMARY} FROM documents WHERE owner_id=? AND family_id=? ORDER BY version DESC,id DESC LIMIT ? OFFSET ?",
                (owner, doc["family_id"], limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM documents WHERE owner_id=? AND family_id=?", (owner, doc["family_id"])
            ).fetchone()[0]
            return {"items": self.service.document_summaries(rows, conn), "total": total}

    def add_relation(self, owner, source_id, payload, conn=None):
        from .service import fail

        if conn is None:
            with self.db.transaction() as conn:
                return self.add_relation(owner, source_id, payload, conn)
        source = self.service.own_document(conn, owner, source_id)
        target = self.service.own_document(conn, owner, payload["target_document_id"])
        if (
            source["id"] == target["id"]
            or source["role"] != "primary"
            or target["role"] != "primary"
            or source["case_id"] != target["case_id"]
        ):
            fail(422, "invalid_relation", "关联法规必须是同一资料范围内的两份独立正文")
        collection = payload["collection"]
        # Explicitly selecting an uploaded primary as an anchor adds it to the collection.
        self.classify(conn, owner, source, collection, "primary", json.loads(source["tags"]))
        if not conn.execute(
            "SELECT 1 FROM legal_classifications WHERE document_id=? AND collection=?",
            (target["id"], collection),
        ).fetchone():
            fail(422, "different_collection", "关联资料需先加入同一采集组")
        relation, locator = payload.get("relation", "related"), payload.get("locator", "")
        conn.execute(
            "INSERT OR IGNORE INTO legal_relations VALUES(?,?,?,?,?,?,?,?)",
            (
                uid(),
                owner,
                collection,
                source_id,
                target["id"],
                relation,
                locator,
                now(),
            ),
        )
        row = conn.execute(
            "SELECT * FROM legal_relations WHERE collection=? AND source_document_id=? AND target_document_id=? AND relation=? AND locator=?",
            (collection, source_id, target["id"], relation, locator),
        ).fetchone()
        return dict(row) | {
            "source_title": source["title"],
            "target_title": target["title"],
            "source_version": source["version"],
            "target_version": target["version"],
        }

    def relations(self, owner, document_id, limit=20, offset=0):
        with closing(self.db.connect()) as conn:
            self.service.own_document(conn, owner, document_id)
            params = (owner, document_id, document_id)
            where = "r.owner_id=? AND (r.source_document_id=? OR r.target_document_id=?)"
            rows = conn.execute(
                f"""SELECT r.*,s.title source_title,t.title target_title,s.version source_version,t.version target_version
                FROM legal_relations r JOIN documents s ON s.id=r.source_document_id JOIN documents t ON t.id=r.target_document_id
                WHERE {where} ORDER BY r.created_at DESC,r.id DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM legal_relations r WHERE {where}", params).fetchone()[
                0
            ]
            return {"items": [dict(row) for row in rows], "total": total}

    def store_original(
        self,
        conn,
        owner,
        blob,
        filename,
        mime_type,
        source_url,
        *,
        collection,
        level,
        tags,
        identity_key=None,
        parent_id=None,
        primary_document_ids=(),
    ):
        filename = (
            re.sub(r"[\x00-\x1f\x7f]", "", Path(filename.replace("\\", "/")).name)[:180] or "source.bin"
        )
        role = "secondary" if level == "attachment" else "primary"
        family = family_key(
            owner, filename, source_url, role=role, parent_id=parent_id, identity_key=identity_key
        )
        previous = conn.execute(
            "SELECT * FROM documents WHERE owner_id=? AND family_id=? ORDER BY version DESC LIMIT 1",
            (owner, family),
        ).fetchone()
        unchanged = previous and previous["content_hash"] == blob.digest
        if unchanged:
            doc = previous
        else:
            version = conn.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM documents WHERE owner_id=? AND family_id=?",
                (owner, family),
            ).fetchone()[0]
            id = uid()
            conn.execute(
                """INSERT INTO documents(id,owner_id,case_id,parent_id,title,filename,role,status,mime_type,size,version,
                content_hash,storage_path,tags,source_url,error,manifest,created_at,family_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    id,
                    owner,
                    None,
                    parent_id,
                    Path(filename).stem,
                    filename,
                    role,
                    "stored" if level == "attachment" else "processing",
                    mime_type,
                    blob.size,
                    version,
                    blob.digest,
                    str(blob.path),
                    dump(tags),
                    source_url,
                    None,
                    dump({"elements": [], "chunks": []}),
                    now(),
                    family,
                ),
            )
            doc = self.service.own_document(conn, owner, id)
        if level != "attachment":
            was_indexable = self.can_index(conn, doc["id"])
            self.classify(conn, owner, doc, collection, level, tags)
            fingerprint = self.service.embedding_fingerprint()
            manifest = json.loads(doc["manifest"])
            needs_index = (
                self.can_index(conn, doc["id"])
                and fingerprint
                and (
                    manifest.get("embedding_fingerprint") != fingerprint
                    or any(not chunk.get("embedding") for chunk in manifest.get("chunks", []))
                )
            )
            if not unchanged or (self.can_index(conn, doc["id"]) and not was_indexable) or needs_index:
                self.service.collection.queue_processing(conn, doc["id"], owner)
            for primary in primary_document_ids:
                self.add_relation(
                    owner,
                    primary,
                    {
                        "target_document_id": doc["id"],
                        "collection": collection,
                        "relation": "related",
                        "locator": "",
                    },
                    conn,
                )
        return doc

    def retrieval_rows(self, conn, owner, case_id, collection, as_of, historical):
        """Choose applicable versions before loading bounded manifests; lists stay body-free."""
        from .catalog import DocumentMetadata

        business = dump(DocumentMetadata().model_dump(mode="json"))
        law = dump(DocumentMetadata(kind="law", review_status="draft").model_dump(mode="json"))
        membership = """(NOT EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=v.id)
            OR EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=v.id AND c.level='primary'))"""
        parameters = [business, law, owner, case_id]
        if collection:
            membership = "EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=v.id AND c.collection=? AND c.level='primary')"
            parameters.append(collection)
        period = """json_extract(v.metadata_payload,'$.review_status')='published'
            AND (json_extract(v.metadata_payload,'$.effective_from') IS NULL OR json_extract(v.metadata_payload,'$.effective_from')<=?)
            AND (json_extract(v.metadata_payload,'$.effective_to') IS NULL OR json_extract(v.metadata_payload,'$.effective_to')>=?)"""
        parameters.extend([as_of.isoformat(), as_of.isoformat()])
        history = "v.family_rank=1"
        if historical:
            history = """(json_extract(v.metadata_payload,'$.kind')!='law' OR
                (json_extract(v.metadata_payload,'$.effective_from') IS NOT NULL
                AND (v.family_rank=1 OR json_extract(v.metadata_payload,'$.effective_to') IS NOT NULL)))"""
        candidates = conn.execute(
            f"""WITH versions AS (
            SELECT d.id,d.family_id,d.version,d.title,d.source_url,d.status,d.created_at,length(d.manifest) manifest_size,
                COALESCE(m.payload,CASE WHEN d.source_url IS NULL THEN ? ELSE ? END) metadata_payload,
                COALESCE(m.revision,0) metadata_revision,m.reviewed_at,
                ROW_NUMBER() OVER(PARTITION BY d.family_id ORDER BY d.version DESC,d.created_at DESC,d.id DESC) family_rank
            FROM documents d LEFT JOIN document_metadata m ON m.document_id=d.id
            WHERE d.owner_id=? AND (d.case_id IS NULL OR d.case_id=?) AND d.role='primary'
        ), applicable AS (
            SELECT v.*,ROW_NUMBER() OVER(PARTITION BY v.family_id ORDER BY v.version DESC,v.id DESC) selected
            FROM versions v WHERE {membership} AND {period} AND {history} AND v.status='ready'
        ) SELECT * FROM applicable WHERE selected=1 ORDER BY created_at DESC,id DESC LIMIT 501""",
            parameters,
        ).fetchall()
        selected, size = [], 0
        limited = len(candidates) > 500
        for row in candidates[:500]:
            if size + row["manifest_size"] > 64 * 1024 * 1024:
                limited = True
                continue
            selected.append(row)
            size += row["manifest_size"]
        if not selected:
            return [], {}, limited
        placeholders = ",".join("?" for _ in selected)
        manifests = {
            row["id"]: row["manifest"]
            for row in conn.execute(
                f"SELECT id,manifest FROM documents WHERE id IN ({placeholders})",
                [row["id"] for row in selected],
            )
        }
        rows = [dict(row) | {"manifest": manifests[row["id"]]} for row in selected]
        metadata = {
            row["id"]: {
                "metadata": json.loads(row["metadata_payload"]),
                "metadata_revision": row["metadata_revision"],
                "reviewed_at": row["reviewed_at"],
            }
            for row in selected
        }
        return rows, metadata, limited
