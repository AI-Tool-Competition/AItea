import hashlib
import json
import logging
import mimetypes
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.contexts.knowledge.application.system_library import (
    public_document_sql,
    register_source_guard,
    source_scope,
)
from app.contexts.knowledge.domain.documents import DOCUMENT_FIELDS, DOCUMENT_SUMMARY, MAX_UPLOAD
from app.contexts.knowledge.domain.metadata import DocumentMetadata
from app.shared.domain.errors import AppError, fail
from app.shared.domain.values import dump, now, uid

logger = logging.getLogger(__name__)


class DocumentService:
    def __init__(self, app):
        self.app = app

    def own_document(self, conn, owner, id):
        doc = conn.execute("SELECT * FROM documents WHERE id=? AND owner_id=?", (id, owner)).fetchone()
        if doc is None:
            fail(404, "not_found", "未找到资料")
        return doc

    def read_document(self, conn, owner, id):
        """Shared reads never grant permission to mutate a system original."""
        register_source_guard(conn)
        doc = conn.execute(
            f"SELECT * FROM documents WHERE id=? AND (owner_id=? OR {public_document_sql()})",
            (id, owner),
        ).fetchone()
        if doc is None:
            fail(404, "not_found", "未找到资料")
        return doc

    def document_dict(self, row, conn=None):
        if conn is None:
            with closing(self.app.db.connect()) as conn:
                return self.app.document_dict(row, conn=conn)
        from app.contexts.knowledge.application.document_processing import processing_details

        doc = self.app.document_base(row)
        doc["processing_detail"] = processing_details(conn, [row["id"]]).get(row["id"])
        return self.app.archive.enrich(self.app.catalog.enrich_document(doc, conn=conn), conn=conn)

    def document_base(self, row):
        doc = {key: row[key] for key in DOCUMENT_FIELDS}
        doc["source_scope"] = source_scope(row["owner_id"])
        doc["tags"] = json.loads(row["tags"])
        doc["chunk_count"] = (
            row["chunk_count"]
            if "chunk_count" in row.keys()
            else len(json.loads(row["manifest"]).get("chunks", []))
        )
        return doc

    def document_summaries(self, rows, conn):
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        from app.contexts.knowledge.application.document_processing import processing_details

        progress = processing_details(conn, ids)
        placeholders = ",".join("?" for _ in ids)
        metadata = {
            row["document_id"]: row
            for row in conn.execute(
                f"SELECT * FROM document_metadata WHERE document_id IN ({placeholders})", ids
            )
        }
        classifications = {}
        countries = {
            row["document_id"]: row["country"]
            for row in conn.execute(
                f"""SELECT d.id document_id,COALESCE(dc.country,pc.country) country FROM documents d
                    LEFT JOIN document_countries dc ON dc.document_id=d.id
                    LEFT JOIN document_countries pc ON pc.document_id=d.parent_id WHERE d.id IN ({placeholders})""",
                ids,
            )
        }
        for row in conn.execute(
            f"SELECT * FROM legal_classifications WHERE document_id IN ({placeholders}) ORDER BY collection",
            ids,
        ):
            classifications.setdefault(row["document_id"], []).append(row)
        result = []
        for row in rows:
            doc = self.app.document_base(row)
            doc["processing_detail"] = progress.get(row["id"])
            doc["country"] = countries.get(row["id"])
            saved = metadata.get(row["id"])
            if saved:
                doc.update(
                    metadata=json.loads(saved["payload"]),
                    metadata_revision=saved["revision"],
                    reviewed_at=saved["reviewed_at"],
                )
            else:
                default = (
                    DocumentMetadata(kind="law", review_status="draft")
                    if row["source_url"]
                    else DocumentMetadata()
                )
                doc.update(metadata=default.model_dump(mode="json"), metadata_revision=0, reviewed_at=None)
            result.append(self.app.archive.with_classifications(doc, classifications.get(row["id"], [])))
        return result

    def metadata_changed(self, conn, document):
        if not document["case_id"]:
            return
        states = conn.execute(
            "SELECT task_id,evidence_ids FROM task_states WHERE case_id=?", (document["case_id"],)
        ).fetchall()
        for state in states:
            if document["id"] in json.loads(state["evidence_ids"]):
                self.app.invalidate_task(
                    conn,
                    document["case_id"],
                    state["task_id"],
                    reason="引用材料的审核或适用范围已变更，请重新核验。",
                )
        self.app.touch(conn, document["case_id"])
        self.app.event(
            conn, document["case_id"], "document_review_updated", "更新资料审核信息", document["filename"]
        )

    def store_document(
        self,
        owner,
        filename,
        content,
        mime_type="",
        *,
        case_id=None,
        role="primary",
        parent_id=None,
        tags=None,
        source_url=None,
    ):
        from app.contexts.knowledge.infrastructure.parsing.ingestion_quality import (
            has_mapped_source_objects,
            quality_warning,
            text_complete,
        )
        from app.contexts.knowledge.infrastructure.parsing.parsing import parse_bounded

        if len(content) > MAX_UPLOAD:
            fail(413, "file_too_large", "文件不能超过 20 MB")
        if not content:
            fail(422, "empty_file", "上传文件不能为空")
        from app.contexts.knowledge.application.archive import family_key, sanitize_filename

        filename, identity_filename = sanitize_filename(filename, fallback="document.bin")
        if role not in ("primary", "secondary"):
            fail(422, "invalid_role", "文件角色无效")
        if (
            not isinstance(tags or [], list)
            or len(tags or []) > 20
            or any(not isinstance(t, str) or len(t) > 80 for t in tags or [])
        ):
            fail(422, "invalid_tags", "标签需为最多 20 项的短文本数组")
        with closing(self.app.db.connect()) as conn:
            if case_id:
                self.app.own_case(conn, owner, case_id)
            if role == "secondary":
                if not parent_id:
                    fail(422, "parent_required", "关联附件必须指定主文件")
                parent = self.app.own_document(conn, owner, parent_id)
                if parent["case_id"] != case_id or parent["role"] != "primary":
                    fail(422, "invalid_parent", "附件与主文件必须属于同一资料范围")
            elif parent_id:
                fail(422, "invalid_parent", "主文件不能指定父文件")
        mime_type = (mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream").split(";")[
            0
        ]
        content_hash = hashlib.sha256(content).hexdigest()
        family = family_key(owner, identity_filename, source_url, case_id, role, parent_id)
        with closing(self.app.db.connect()) as conn:
            previous = conn.execute(
                "SELECT * FROM documents WHERE owner_id=? AND family_id=? ORDER BY version DESC LIMIT 1",
                (owner, family),
            ).fetchone()
            if (
                previous
                and previous["content_hash"] == content_hash
                and previous["status"] in ("ready", "stored")
            ):
                manifest = json.loads(previous["manifest"])
                fingerprint = self.app.embedding_fingerprint()
                pending = bool(
                    fingerprint
                    and (
                        manifest.get("embedding_fingerprint") != fingerprint
                        or self.app.index.pending(conn, previous["id"], fingerprint, limit=1)
                        or conn.execute(
                            "SELECT 1 FROM retrieval_dirty WHERE document_id=?", (previous["id"],)
                        ).fetchone()
                    )
                )
                if role == "primary" and pending and text_complete(manifest) and manifest.get("chunks"):
                    # Retry the existing immutable original instead of creating a
                    # new legal version merely because its vector service failed.
                    with self.app.db.transaction() as write:
                        current = write.execute(
                            "SELECT * FROM documents WHERE owner_id=? AND family_id=? ORDER BY version DESC LIMIT 1",
                            (owner, family),
                        ).fetchone()
                        if (
                            current
                            and current["id"] == previous["id"]
                            and self.app.archive.can_index(write, current["id"])
                        ):
                            self.app.collection.queue_processing(write, current["id"], owner)
                            return self.app.document_dict(current, conn=write)
                if role == "secondary" or (not previous["error"] and not pending):
                    return self.app.document_dict(previous, conn=conn)
        blob = self.app.archive.blobs.put_bytes(owner, content)
        manifest, error = {"elements": [], "chunks": []}, None
        status = "stored" if role == "secondary" else "ready"
        if role == "primary":
            try:
                manifest = parse_bounded(filename, content, mime_type, parser="mineru")
                if (
                    not manifest.get("elements")
                    and not manifest.get("pages")
                    and not has_mapped_source_objects(manifest)
                    and not manifest.get("remote_parse")
                ):
                    raise ValueError("文件没有可提取的正文；扫描件可能需要 OCR")
                if manifest.get("warnings"):
                    error = "解析提示：" + "；".join(str(item) for item in manifest["warnings"])[:450]
                if not text_complete(manifest):
                    error = quality_warning(manifest)
                    if manifest.get("remote_parse", {}).get("state") == "planned":
                        status = "processing"
                    else:
                        status = "failed"
            except Exception as exc:
                logger.info("document_parse_failed", extra={"parser_error": type(exc).__name__})
                status, error = "failed", str(exc)[:500]
        id, storage_path = uid(), blob.path
        try:
            with self.app.db.transaction() as conn:
                if case_id:
                    self.app.own_case(conn, owner, case_id)
                previous = conn.execute(
                    "SELECT * FROM documents WHERE owner_id=? AND family_id=? ORDER BY version DESC LIMIT 1",
                    (owner, family),
                ).fetchone()
                if (
                    previous
                    and previous["content_hash"] == content_hash
                    and previous["status"] == status
                    and previous["error"] == error
                    and json.loads(previous["manifest"]).get("embedding_fingerprint")
                    == manifest.get("embedding_fingerprint")
                ):
                    return self.app.document_dict(previous, conn=conn)
                version = previous["version"] + 1 if previous else 1
                conn.execute(
                    "INSERT INTO documents(id,owner_id,case_id,parent_id,title,filename,role,status,mime_type,size,version,content_hash,storage_path,tags,source_url,error,manifest,created_at,family_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        id,
                        owner,
                        case_id,
                        parent_id,
                        Path(filename).stem,
                        filename,
                        role,
                        status,
                        mime_type,
                        len(content),
                        version,
                        content_hash,
                        str(storage_path),
                        dump(tags or []),
                        source_url,
                        error,
                        dump(manifest),
                        now(),
                        family,
                    ),
                )
                if status == "processing":
                    self.app.collection.queue_processing(conn, id, owner)
                conn.execute("SAVEPOINT upload_index")
                try:
                    # Bad vectors may be skipped when importing old data, but a
                    # new upload must never advertise a silently incomplete index.
                    self.app.index.sync_document(conn, id, manifest, strict_vectors=True)
                except ValueError as exc:
                    conn.execute("ROLLBACK TO upload_index")
                    for chunk in manifest.get("chunks", []):
                        chunk.pop("embedding", None)
                    manifest.pop("embedding_fingerprint", None)
                    error = "向量索引未完成：" + str(exc)[:350]
                    manifest["embedding_error"] = error
                    conn.execute(
                        "UPDATE documents SET manifest=?,error=? WHERE id=?", (dump(manifest), error, id)
                    )
                    self.app.index.sync_document(conn, id, manifest)
                finally:
                    conn.execute("RELEASE upload_index")
                fingerprint = self.app.embedding_fingerprint()
                if (
                    role == "primary"
                    and status == "ready"
                    and text_complete(manifest)
                    and fingerprint
                    and self.app.index.pending(conn, id, fingerprint, limit=1)
                ):
                    error = error or "向量索引未完成，已排队重试。"
                    conn.execute(
                        "UPDATE documents SET manifest=json_remove(manifest,'$.embedding_fingerprint'),error=? WHERE id=?",
                        (error, id),
                    )
                    conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (id,))
                    self.app.collection.queue_processing(conn, id, owner)
                if case_id:
                    if previous:
                        for state in conn.execute(
                            "SELECT task_id,evidence_ids FROM task_states WHERE case_id=?", (case_id,)
                        ).fetchall():
                            if previous["id"] in json.loads(state["evidence_ids"]):
                                self.app.invalidate_task(
                                    conn,
                                    case_id,
                                    state["task_id"],
                                    reason=f"材料「{filename}」上传了新版本，请重新关联和核验。",
                                )
                    self.app.touch(conn, case_id)
                    self.app.event(
                        conn,
                        case_id,
                        "document_uploaded",
                        "上传办理材料",
                        f"{filename} · 第 {version} 版 · {status}",
                    )
                return self.app.document_dict(self.app.own_document(conn, owner, id), conn=conn)
        except BaseException:
            # Immutable blobs may already be shared by another committed document.
            raise

    def documents(
        self,
        owner,
        case_id=None,
        limit=100,
        offset=0,
        *,
        role=None,
        parent_id=None,
        q=None,
        legal_level=None,
        collection=None,
        country=None,
        source_scope=None,
        library_only=False,
    ):
        from app.shared.domain.countries import country_from_collection, document_country, resolve_country

        if library_only and case_id:
            fail(422, "invalid_document_scope", "业务资料与库外资料范围不能同时选择")
        scope = country_from_collection(collection) if collection else None
        if country and scope and country != scope:
            fail(422, "country_mismatch", "采集组与所选国家不一致")
        country = country or scope
        with closing(self.app.db.connect()) as conn:
            if case_id:
                case = self.app.own_case(conn, owner, case_id)
                destination = resolve_country(json.loads(case["template"])["destination"])
                if destination:
                    if country and country != destination:
                        fail(422, "country_mismatch", "业务只能选择目标国家的法规")
                    country = destination
            register_source_guard(conn)
            parent = self.app.read_document(conn, owner, parent_id) if parent_id else None
            if parent and country is None:
                country = document_country(conn, parent_id)
            shared = public_document_sql() if country and country != "unassigned" else "0"
            if source_scope == "personal":
                shared = "0"
            # An explicit personal directory is the account's upload inventory,
            # including case files. Case views and legacy callers keep their
            # existing scope; this does not widen the evidence search gate.
            account_inventory = source_scope == "personal" and not case_id and not library_only
            personal = "owner_id=?" if account_inventory else "owner_id=? AND case_id IS ?"
            conditions = [f"(({personal}) OR {shared})"]
            parameters = [owner] if account_inventory else [owner, case_id]
            if source_scope == "system":
                conditions.append(shared)
            if country and country != "unassigned":
                scoped_business = (
                    """OR (documents.case_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM document_countries dc
                    WHERE dc.document_id=COALESCE(documents.parent_id,documents.id)))"""
                    if case_id
                    else ""
                )
                conditions.append(
                    f"""(EXISTS(SELECT 1 FROM document_countries dc WHERE dc.document_id=COALESCE(documents.parent_id,documents.id) AND dc.country=?)
                    {scoped_business})"""
                )
                parameters.append(country)
            elif country == "unassigned" or source_scope != "personal":
                conditions.append(
                    "NOT EXISTS(SELECT 1 FROM document_countries dc WHERE dc.document_id=COALESCE(documents.parent_id,documents.id))"
                )
            if parent_id:
                account_directory = account_inventory and parent["owner_id"] == owner
                if (parent["case_id"] != case_id and not account_directory) or parent["role"] != "primary":
                    fail(404, "not_found", "当前资料库中未找到一级资料")
                if role == "primary":
                    fail(422, "invalid_role", "一级资料下只能查询二级附件")
                conditions.extend(["parent_id=?", "role='secondary'"])
                parameters.append(parent_id)
            elif role:
                conditions.append("role=?")
                parameters.append(role)
            if collection:
                conditions.append(
                    "EXISTS(SELECT 1 FROM legal_classifications lc WHERE lc.document_id=documents.id AND lc.collection=?)"
                )
                parameters.append(collection)
            if legal_level == "attachment":
                conditions.append("role='secondary'")
            elif legal_level:
                conditions.append("role='primary'")
                primary = """COALESCE((SELECT lc.level='primary' FROM legal_classifications lc
                    JOIN document_countries dc ON dc.document_id=lc.document_id
                    WHERE lc.document_id=documents.id AND lc.collection='country:'||dc.country),
                    EXISTS(SELECT 1 FROM legal_classifications lc WHERE lc.document_id=documents.id AND lc.level='primary')
                    OR NOT EXISTS(SELECT 1 FROM legal_classifications lc WHERE lc.document_id=documents.id))"""
                conditions.append(f"({primary})" if legal_level == "primary" else f"NOT ({primary})")
            if q and q.strip():
                pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                conditions.append("(title LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\')")
                parameters.extend([pattern, pattern])
            where = " AND ".join(conditions)
            rows = conn.execute(
                f"SELECT {DOCUMENT_SUMMARY} FROM documents WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            total = conn.execute(f"SELECT COUNT(*) n FROM documents WHERE {where}", parameters).fetchone()[
                "n"
            ]
            return {"items": self.app.document_summaries(rows, conn), "total": total}

    def document_detail(
        self, owner, document_id, *, element_offset=0, chunk_offset=0, limit=100, include_preview=True
    ):
        from app.contexts.knowledge.application.document_preview import preview

        with closing(self.app.db.connect()) as conn:
            row = self.app.read_document(conn, owner, document_id)
            manifest = json.loads(row["manifest"])
            elements, chunks = manifest.get("elements", []), manifest.get("chunks", [])
            return {
                "document": self.app.document_dict(row, conn=conn),
                "elements": elements[element_offset : element_offset + limit],
                "chunks": [
                    {key: value for key, value in chunk.items() if key != "embedding"}
                    for chunk in chunks[chunk_offset : chunk_offset + limit]
                ],
                "element_total": len(elements),
                "chunk_total": len(chunks),
                "element_offset": element_offset,
                "chunk_offset": chunk_offset,
                "limit": limit,
                "parser": manifest.get("parser"),
                "warnings": manifest.get("warnings", []),
                "quality": manifest.get("quality"),
                **({"preview": preview(manifest, element_offset, limit)} if include_preview else {}),
            }

    def document_image(self, owner, document_id, image_id):
        from app.contexts.knowledge.application.document_preview import image_bytes

        with closing(self.app.db.connect()) as conn:
            row = self.app.read_document(conn, owner, document_id)
        try:
            return image_bytes(self.app, row, json.loads(row["manifest"]), image_id)
        except (ValueError, OSError, KeyError) as exc:
            raise AppError(422, "image_unavailable", "图片暂时无法读取，请重试或下载原件。") from exc

    def search(
        self,
        owner,
        query,
        case_id=None,
        limit=5,
        as_of=None,
        collection=None,
        country=None,
        document_ids=None,
        *,
        source_scope=None,
    ):
        historical = as_of is not None
        as_of = as_of or datetime.now(timezone.utc).date()
        with closing(self.app.db.connect()) as conn:
            from app.shared.domain.countries import country_from_collection, resolve_country

            scope = country_from_collection(collection) if collection else None
            if country and scope and country != scope:
                fail(422, "country_mismatch", "检索国家与采集组不一致")
            country = country or scope
            if case_id:
                case = self.app.own_case(conn, owner, case_id)
                destination = resolve_country(json.loads(case["template"])["destination"])
                if destination:
                    if country and country != destination:
                        fail(422, "country_mismatch", "业务只能检索目标国家的法规")
                    country = destination
        return self.app.search_eligible(
            owner,
            query,
            case_id,
            limit,
            as_of,
            collection,
            country,
            document_ids,
            historical=historical,
            source_scope=source_scope,
        )

    def search_eligible(
        self,
        owner,
        query,
        case_id,
        limit,
        as_of,
        collection=None,
        country=None,
        document_ids=None,
        *,
        historical=False,
        source_scope=None,
    ):
        """Shared eligible-document search; callers resolve their allowed country scope first."""
        with closing(self.app.db.connect()) as conn:
            if case_id:
                self.app.own_case(conn, owner, case_id)
        return self.app.index.search(
            owner,
            query,
            case_id,
            limit,
            as_of,
            collection,
            None if country == "unassigned" else country,
            document_ids,
            historical,
            source_scope=source_scope,
        )

    def download(self, owner, document_id):
        with closing(self.app.db.connect()) as conn:
            return dict(self.read_document(conn, owner, document_id))

    def patch_metadata(self, owner, document_id, payload):
        self.app.catalog.patch_document(owner, document_id, payload, on_changed=self.metadata_changed)
        with closing(self.app.db.connect()) as conn:
            return self.document_dict(self.own_document(conn, owner, document_id), conn=conn)
