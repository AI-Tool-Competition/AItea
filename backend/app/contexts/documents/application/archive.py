"""Immutable original versions and explicit, collection-scoped legal relationships."""

import hashlib
import json
import re
from contextlib import closing
from pathlib import Path
from urllib.parse import urldefrag

from app.contexts.documents.application.system_library import (
    SYSTEM_OWNER_ID,
    public_document_sql,
    register_source_guard,
    require_admission,
)
from app.shared.domain.countries import country_from_collection, document_country, resolve_country
from app.shared.domain.values import dump, now, uid
from app.shared.infrastructure.persistence.blob_store import BlobStore


def sanitize_filename(filename, *, fallback):
    """Return a bounded display name and the legacy name used for family identity."""
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", Path(filename.replace("\\", "/")).name)
    legacy = cleaned[:180] or fallback
    suffix = Path(cleaned).suffix
    if len(cleaned) > 180 and 0 < len(suffix) < 180:
        return cleaned[: 180 - len(suffix)] + suffix, legacy
    return legacy, legacy


def family_key(
    owner,
    filename,
    source_url=None,
    case_id=None,
    role="primary",
    parent_id=None,
    identity_key=None,
    country=None,
):
    identity = (
        ("key", identity_key)
        if identity_key
        else (("url", urldefrag(source_url)[0]) if source_url else ("filename", filename))
    )
    base = hashlib.sha256(dump([owner, case_id, role, parent_id, identity]).encode()).hexdigest()
    return _country_family(base, country) if country else base


def _country_family(base, country):
    country_from_collection(f"country:{country}")
    return hashlib.sha256(dump(["country", country, base]).encode()).hexdigest()


class LegalArchive:
    def __init__(self, service):
        self.service, self.db = service, service.db
        self.blobs = BlobStore(self.db.files_dir, max_bytes=300 * 1024 * 1024)
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
        country = document_country(conn, document["id"])
        if not country and document["role"] == "secondary" and document.get("parent_id"):
            country = document_country(conn, document["parent_id"])
        return self.with_classifications(document | {"country": country}, rows)

    def with_classifications(self, document, rows):
        classifications = [
            {key: row[key] for key in ("collection", "level", "revision")} | {"tags": json.loads(row["tags"])}
            for row in rows
        ]
        national = [
            row
            for row in rows
            if document.get("country") and row["collection"] == f"country:{document['country']}"
        ]
        effective_rows = national or rows
        level = (
            "attachment"
            if document["role"] == "secondary"
            else (
                "secondary"
                if effective_rows and all(row["level"] == "secondary" for row in effective_rows)
                else "primary"
            )
        )
        tags = (
            list(dict.fromkeys(tag for item in classifications for tag in item["tags"]))
            if classifications
            else document["tags"]
        )
        return document | {"classifications": classifications, "legal_level": level, "tags": tags}

    def can_index(self, conn, document_id, collection=None):
        requested_country = country_from_collection(collection)
        country = document_country(conn, document_id)
        if requested_country and requested_country != country:
            return False
        rows = conn.execute(
            "SELECT collection,level FROM legal_classifications WHERE document_id=?", (document_id,)
        ).fetchall()
        national = [row for row in rows if country and row["collection"] == f"country:{country}"]
        if national and national[0]["level"] != "primary":
            return False
        if collection:
            return any(row["collection"] == collection and row["level"] == "primary" for row in rows)
        return not rows or any(row["level"] == "primary" for row in rows)

    def _bind_country(self, conn, owner, document, country, *, family_prepared=False, kind_hint=None):
        """Bind the complete immutable version family, without moving raw files."""
        from app.shared.domain.errors import fail

        if kind_hint is not None and kind_hint not in {"law", "guidance", "case"}:
            fail(422, "invalid_document_kind", "国别资料类型必须为法规、指引或案例")
        if document["case_id"]:
            case = self.service.own_case(conn, owner, document["case_id"])
            destination = resolve_country(json.loads(case["template"]).get("destination"))
            if destination and destination != country:
                fail(422, "country_mismatch", "业务资料只能归入该业务目标国家")
        current = document_country(conn, document["id"])
        if current:
            if current != country:
                fail(422, "country_mismatch", "资料国别已固定，不能改为其他国家")
            return
        family = document["family_id"]
        if conn.execute(
            """SELECT 1 FROM documents d JOIN document_countries c ON c.document_id=d.id
            WHERE d.owner_id=? AND d.family_id=? AND c.country<>? LIMIT 1""",
            (owner, family, country),
        ).fetchone():
            fail(422, "country_mismatch", "同一法规的历史版本不能跨国家混用")
        if conn.execute(
            """SELECT 1 FROM legal_relations r
            JOIN documents s ON s.id=r.source_document_id JOIN documents t ON t.id=r.target_document_id
            LEFT JOIN document_countries sc ON sc.document_id=s.id
            LEFT JOIN document_countries tc ON tc.document_id=t.id
            WHERE r.owner_id=? AND (
                (s.family_id=? AND t.family_id<>? AND (tc.country IS NULL OR tc.country<>?)) OR
                (t.family_id=? AND s.family_id<>? AND (sc.country IS NULL OR sc.country<>?))) LIMIT 1""",
            (owner, family, family, country, family, family, country),
        ).fetchone():
            fail(
                422,
                "country_relation_conflict",
                "资料已有未定国或其他国家的关联，请保留历史关系并单独导入国别原件",
            )
        # A file uploaded before country scoping has a legacy family key. Move
        # every version together so subsequent unscoped uploads cannot replace it.
        if not family_prepared:
            scoped_family = _country_family(family, country)
            if conn.execute(
                "SELECT 1 FROM documents WHERE owner_id=? AND family_id=? LIMIT 1", (owner, scoped_family)
            ).fetchone():
                fail(422, "country_family_conflict", "该国已有同一法规家族，请使用已有国别原件")
            conn.execute(
                "UPDATE documents SET family_id=? WHERE owner_id=? AND family_id=?",
                (scoped_family, owner, family),
            )
            family = scoped_family
        # Country assignment changes the legal applicability boundary. A normal
        # upload's default business/published state is not legal review evidence.
        # Only newly bound versions are reset; repeats retain an explicit review.
        from app.contexts.documents.domain.metadata import DocumentMetadata

        cursor = ""
        while rows := conn.execute(
            """SELECT d.id,d.case_id,d.filename,d.source_url,m.payload,m.revision
            FROM documents d LEFT JOIN document_metadata m ON m.document_id=d.id
            WHERE d.owner_id=? AND d.family_id=? AND d.role='primary' AND d.id>?
                AND NOT EXISTS(SELECT 1 FROM document_countries c WHERE c.document_id=d.id)
            ORDER BY d.id LIMIT 200""",
            (owner, family, cursor),
        ).fetchall():
            for row in rows:
                metadata = (
                    json.loads(row["payload"])
                    if row["payload"]
                    else DocumentMetadata().model_dump(mode="json")
                )
                metadata.update(
                    kind=(kind_hint or "law")
                    if metadata["kind"] in {"business", "demo"}
                    else metadata["kind"],
                    jurisdiction=country,
                    review_status="draft",
                )
                payload = dump(DocumentMetadata.model_validate(metadata).model_dump(mode="json"))
                revision, at = (row["revision"] or 0) + 1, now()
                conn.execute(
                    """INSERT INTO document_metadata VALUES(?,?,?,?,?,?)
                    ON CONFLICT(document_id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,
                    reviewed_by=NULL,reviewed_at=NULL,updated_at=excluded.updated_at""",
                    (row["id"], payload, revision, None, None, at),
                )
                conn.execute(
                    "INSERT INTO document_metadata_history VALUES(?,?,?,?,?)",
                    (row["id"], revision, payload, owner, at),
                )
                if row["id"] != document["id"]:
                    # The classification entry point invalidates the selected
                    # version; older references in this family also need review.
                    self.service.metadata_changed(conn, row)
            cursor = rows[-1]["id"]
        conn.execute(
            """INSERT OR IGNORE INTO document_countries(document_id,country)
            SELECT id,? FROM documents WHERE owner_id=? AND family_id=?""",
            (country, owner, family),
        )

    def classify(self, conn, owner, document, collection, level, tags, *, expected_revision=None):
        from app.shared.domain.errors import fail

        country = country_from_collection(collection)
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
            if country:
                self._bind_country(conn, owner, document, country)
            return
        if (
            not previous
            and conn.execute(
                "SELECT COUNT(*) FROM legal_classifications WHERE document_id=?", (document["id"],)
            ).fetchone()[0]
            >= 20
        ):
            fail(422, "too_many_collections", "每份资料最多加入 20 个采集组")
        if country:
            self._bind_country(conn, owner, document, country)
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
            return self.service.document_dict(self.service.own_document(conn, owner, document_id), conn=conn)

    def versions(self, owner, document_id, limit=20, offset=0):
        from app.contexts.documents.domain.documents import DOCUMENT_SUMMARY

        with closing(self.db.connect()) as conn:
            doc = self.service.read_document(conn, owner, document_id)
            actual_owner = doc["owner_id"]
            scope = public_document_sql() if actual_owner == SYSTEM_OWNER_ID else "1"
            rows = conn.execute(
                f"SELECT {DOCUMENT_SUMMARY} FROM documents WHERE owner_id=? AND family_id=? AND {scope} ORDER BY version DESC,id DESC LIMIT ? OFFSET ?",
                (actual_owner, doc["family_id"], limit, offset),
            ).fetchall()
            total = conn.execute(
                f"SELECT COUNT(*) FROM documents WHERE owner_id=? AND family_id=? AND {scope}",
                (actual_owner, doc["family_id"]),
            ).fetchone()[0]
            return {"items": self.service.document_summaries(rows, conn), "total": total}

    def add_relation(self, owner, source_id, payload, conn=None):
        from app.shared.domain.errors import fail

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
        country = country_from_collection(collection)
        source_country, target_country = (
            document_country(conn, source_id),
            document_country(conn, target["id"]),
        )
        if source_country != target_country or (country and source_country != country):
            fail(422, "country_mismatch", "关联法规必须已归属同一国家，不能通过其他采集组跨国关联")
        # Associations describe exact originals without changing an existing
        # legal tier. Independent secondary regulations also cite and amend
        # one another; linking them must not grant indexing eligibility.
        if not conn.execute(
            "SELECT 1 FROM legal_classifications WHERE document_id=? AND collection=?",
            (source_id, collection),
        ).fetchone():
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
            doc = self.service.read_document(conn, owner, document_id)
            params = (doc["owner_id"], document_id, document_id)
            where = "r.owner_id=? AND (r.source_document_id=? OR r.target_document_id=?)"
            if doc["owner_id"] == SYSTEM_OWNER_ID:
                where += f" AND {public_document_sql('s')} AND {public_document_sql('t')}"
                where += """ AND (SELECT country FROM document_countries WHERE document_id=COALESCE(s.parent_id,s.id))
                    =(SELECT country FROM document_countries WHERE document_id=COALESCE(t.parent_id,t.id))"""
            rows = conn.execute(
                f"""SELECT r.*,s.title source_title,t.title target_title,s.version source_version,t.version target_version
                FROM legal_relations r JOIN documents s ON s.id=r.source_document_id JOIN documents t ON t.id=r.target_document_id
                WHERE {where} ORDER BY r.created_at DESC,r.id DESC LIMIT ? OFFSET ?""",
                (*params, limit, offset),
            ).fetchall()
            total = conn.execute(
                f"""SELECT COUNT(*) FROM legal_relations r
                JOIN documents s ON s.id=r.source_document_id JOIN documents t ON t.id=r.target_document_id
                WHERE {where}""",
                params,
            ).fetchone()[0]
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
        kind_hint=None,
        source_response=None,
    ):
        from app.shared.domain.errors import fail

        country = country_from_collection(collection)
        require_admission(owner, source_url, country)
        filename, identity_filename = sanitize_filename(filename, fallback="source.bin")
        role = "secondary" if level == "attachment" else "primary"
        if parent_id:
            parent = self.service.own_document(conn, owner, parent_id)
            if document_country(conn, parent["id"]) != country:
                fail(422, "country_mismatch", "附件与主文件必须属于同一国家范围")
        family = family_key(
            owner,
            identity_filename,
            source_url,
            role=role,
            parent_id=parent_id,
            identity_key=identity_key,
            country=country,
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
            manifest = {"elements": [], "chunks": []}
            if source_response is not None:
                manifest["source_response"] = source_response
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
                    dump(manifest),
                    now(),
                    family,
                ),
            )
            doc = self.service.own_document(conn, owner, id)
        if country:
            self._bind_country(conn, owner, doc, country, family_prepared=True, kind_hint=kind_hint)
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
                    or self.service.index.pending(conn, doc["id"], fingerprint, limit=1)
                    or conn.execute(
                        "SELECT 1 FROM retrieval_dirty WHERE document_id=?", (doc["id"],)
                    ).fetchone()
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

    def eligibility_sql(
        self,
        conn,
        owner,
        case_id,
        collection,
        as_of,
        historical,
        country=None,
        document_ids=None,
        *,
        require_current=True,
        source_scope=None,
    ):
        """One scope/version gate shared by lexical and dense persistent retrieval."""
        from app.contexts.documents.domain.metadata import DocumentMetadata

        register_source_guard(conn)

        business = dump(DocumentMetadata().model_dump(mode="json"))
        law = dump(DocumentMetadata(kind="law", review_status="draft").model_dump(mode="json"))
        collection_country = country_from_collection(collection)
        if country:
            country_from_collection(f"country:{country}")
            if collection_country and collection_country != country:
                from app.shared.domain.errors import fail

                fail(422, "country_mismatch", "检索国家与采集组不一致")
        membership = """(NOT EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=v.id)
            OR EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=v.id AND c.level='primary'))"""
        shared = public_document_sql("d") if country and source_scope != "personal" else "0"
        ownership = f"AND d.owner_id='{SYSTEM_OWNER_ID}'" if source_scope == "system" else ""
        parameters = [business, law, owner, case_id]
        business_material = """(v.country IS NULL AND v.case_id IS NOT NULL
            AND json_extract(v.metadata_payload,'$.kind')='business')"""
        scope = """v.country IS NULL AND NOT EXISTS(
            SELECT 1 FROM legal_classifications c WHERE c.document_id=v.id AND c.collection LIKE 'country:%')"""
        if country:
            scope = f"(v.country=? OR {business_material})"
            parameters.append(country)
            membership = f"""(EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=v.id
                AND c.collection='country:'||v.country AND c.level='primary') OR
                (NOT EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=v.id
                    AND c.collection='country:'||v.country) AND {membership}))"""
        if collection:
            requested_membership = "EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=v.id AND c.collection=? AND c.level='primary')"
            membership = f"({membership} AND {requested_membership})" if country else requested_membership
            parameters.append(collection)
        if country:
            membership = f"({business_material} OR {membership})"
        period = """json_extract(v.metadata_payload,'$.review_status')='published'
            AND (json_extract(v.metadata_payload,'$.effective_from') IS NULL OR json_extract(v.metadata_payload,'$.effective_from')<=?)
            AND (json_extract(v.metadata_payload,'$.effective_to') IS NULL OR json_extract(v.metadata_payload,'$.effective_to')>=?)"""
        parameters.extend([as_of.isoformat(), as_of.isoformat()])
        system_review = f"""(v.owner_id<>'{SYSTEM_OWNER_ID}' OR
            (v.reviewed_by IS NOT NULL AND v.reviewed_by<>'{SYSTEM_OWNER_ID}' AND v.reviewed_at IS NOT NULL
             AND EXISTS(SELECT 1 FROM users reviewer WHERE reviewer.id=v.reviewed_by AND reviewer.is_demo=0)
             AND json_extract(v.metadata_payload,'$.effective_from') IS NOT NULL))"""
        history = "v.family_rank=1"
        if historical:
            history = """(json_extract(v.metadata_payload,'$.kind')!='law' OR
                (json_extract(v.metadata_payload,'$.effective_from') IS NOT NULL
                AND (v.family_rank=1 OR json_extract(v.metadata_payload,'$.effective_to') IS NOT NULL)))"""
        selected_scope = ""
        if document_ids is not None:
            if not document_ids:
                selected_scope = " AND 0"
            else:
                selected_scope = " AND id IN (" + ",".join("?" for _ in document_ids) + ")"
            parameters.extend(document_ids)
        index_gate = "AND text_complete=1 AND index_current=1" if require_current else ""
        query = f"""WITH versions AS (
            SELECT d.id,d.owner_id,d.family_id,d.version,d.title,d.source_url,d.status,d.created_at,d.case_id,d.content_hash,dc.country,
                COALESCE(m.payload,CASE WHEN d.source_url IS NULL THEN ? ELSE ? END) metadata_payload,
                COALESCE(m.revision,0) metadata_revision,m.reviewed_at,m.reviewed_by,
                COALESCE(rx.complete,0) text_complete,COALESCE(rx.revision,0) index_revision,
                (rx.source_hash=d.content_hash AND NOT EXISTS(
                    SELECT 1 FROM retrieval_dirty q WHERE q.document_id=d.id)) index_current,
                ROW_NUMBER() OVER(PARTITION BY d.family_id ORDER BY d.version DESC,d.created_at DESC,d.id DESC) family_rank
            FROM documents d LEFT JOIN document_metadata m ON m.document_id=d.id
            LEFT JOIN document_countries dc ON dc.document_id=d.id
            LEFT JOIN retrieval_documents rx ON rx.document_id=d.id
            WHERE ((d.owner_id=? AND (d.case_id IS NULL OR d.case_id=?)) OR {shared}) AND d.role='primary' {ownership}
        ), applicable AS (
            SELECT v.*,ROW_NUMBER() OVER(PARTITION BY v.family_id ORDER BY v.version DESC,v.id DESC) selected
            FROM versions v WHERE {scope} AND {membership} AND {period} AND {system_review} AND {history}
                AND v.status='ready'
        ) SELECT * FROM applicable WHERE selected=1 {index_gate} {selected_scope} ORDER BY created_at DESC,id DESC"""
        return query, parameters

    def retrieval_rows(
        self, conn, owner, case_id, collection, as_of, historical, country=None, document_ids=None
    ):
        """Compatibility metadata listing; retrieval itself joins this scope inside SQLite."""
        query, parameters = self.eligibility_sql(
            conn, owner, case_id, collection, as_of, historical, country, document_ids
        )
        rows = conn.execute(query, parameters).fetchall()
        metadata = {
            row["id"]: {
                "metadata": json.loads(row["metadata_payload"]),
                "metadata_revision": row["metadata_revision"],
                "reviewed_at": row["reviewed_at"],
            }
            for row in rows
        }
        return rows, metadata, False
