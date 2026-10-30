"""Prepare a derived attachment; make it visible only with the chat transaction."""

import base64
import hashlib
import json
from pathlib import Path

from app.contexts.documents.application.archive import family_key, sanitize_filename
from app.contexts.documents.infrastructure.long_image_pdf import MAX_BYTES, convert_image
from app.shared.domain.errors import fail
from app.shared.domain.values import dump, now, uid


def prepare_pdf(service, owner, source, options, *, timeout):
    if source["mime_type"] not in ("image/png", "image/jpeg", "image/webp"):
        fail(422, "image_required", "请选择 PNG、JPEG 或 WebP 图片")
    path = Path(source["storage_path"]).resolve()
    if not path.is_relative_to(service.db.files_dir.resolve()) or not 0 < source["size"] <= MAX_BYTES:
        fail(422, "image_unavailable", "图片原件大小或位置无效")
    with path.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if hashlib.sha256(content).hexdigest() != source["content_hash"]:
        fail(409, "image_changed", "原图校验不一致")
    try:
        result = convert_image(content, options, timeout=timeout)
    except ValueError as exc:
        fail(422, "image_conversion_failed", str(exc))
    data = base64.b64decode(result.pop("pdf_base64"), validate=True)
    blob = service.archive.blobs.put_bytes(owner, data)
    filename, _ = sanitize_filename(Path(source["filename"]).stem + "-分页.pdf", fallback="分页.pdf")
    return {
        "id": uid(),
        "source_id": source["id"],
        "source_version": source["version"],
        "filename": filename,
        "storage_path": str(blob.path),
        "content_hash": blob.digest,
        "size": blob.size,
        "pagination": {**result, "options": options},
    }


def commit_pdf(service, conn, owner, case_id, pending):
    source = service.library.own_document(conn, owner, pending["source_id"])
    if (
        source["case_id"] != case_id
        or source["version"] != pending["source_version"]
        or source["role"] != "primary"
    ):
        fail(409, "image_context_changed", "原图范围或版本已变化，分页文件未保存")
    filename = pending["filename"]
    family = family_key(
        owner,
        filename,
        case_id=case_id,
        role="secondary",
        parent_id=source["id"],
        identity_key=dump(["image_pdf", source["id"], pending["pagination"]["options"]]),
    )
    previous = conn.execute(
        "SELECT * FROM documents WHERE owner_id=? AND family_id=? ORDER BY version DESC LIMIT 1",
        (owner, family),
    ).fetchone()
    if previous:
        metadata = json.loads(previous["manifest"]).get("image_pagination", {})
        if metadata.get("options") == pending["pagination"]["options"]:
            return {"id": previous["id"], "filename": previous["filename"]}
    conn.execute(
        """INSERT INTO documents(
            id,owner_id,case_id,parent_id,title,filename,role,status,mime_type,size,version,
            content_hash,storage_path,tags,source_url,error,manifest,created_at,family_id
        ) VALUES(?,?,?,?,?,?,'secondary','stored','application/pdf',?,?,?,?,?,NULL,NULL,?,?,?)""",
        (
            pending["id"],
            owner,
            case_id,
            source["id"],
            Path(filename).stem,
            filename,
            pending["size"],
            previous["version"] + 1 if previous else 1,
            pending["content_hash"],
            pending["storage_path"],
            dump([]),
            dump({"elements": [], "chunks": [], "image_pagination": pending["pagination"]}),
            now(),
            family,
        ),
    )
    service.touch(conn, case_id)
    service.event(conn, case_id, "document_uploaded", "保存长图分页 PDF", filename)
    return {"id": pending["id"], "filename": filename}
