"""Account-scoped file copies, committed with the successful assistant message."""

import base64
import hashlib
from pathlib import Path

from app.contexts.documents.application.archive import family_key, sanitize_filename
from app.contexts.documents.infrastructure.document_files import MAX_BYTES, MIMES, operate
from app.shared.domain.errors import fail
from app.shared.domain.values import dump, now, uid


def source_bytes(service, source):
    path = Path(source["storage_path"]).resolve()
    if not path.is_relative_to(service.db.files_dir.resolve()) or not 0 < source["size"] <= MAX_BYTES:
        fail(422, "file_unavailable", "文件原件大小或位置无效")
    with path.open("rb") as stream:
        content = stream.read(MAX_BYTES + 1)
    if len(content) != source["size"] or hashlib.sha256(content).hexdigest() != source["content_hash"]:
        fail(409, "file_changed", "文件原件校验不一致")
    return content


def file_format(source):
    format = Path(source["filename"]).suffix.lower().lstrip(".")
    if format not in MIMES:
        fail(422, "unsupported_file", "支持DOCX、XLSX、PPTX、PDF及UTF-8的MD、TXT、CSV文件")
    return format


def inspect_file(service, source, *, offset, limit, timeout):
    try:
        return operate(
            "inspect",
            {"format": file_format(source), "offset": offset, "limit": limit},
            source_bytes(service, source),
            timeout=timeout,
        )
    except ValueError as exc:
        fail(422, "file_inspection_failed", str(exc))


def prepare_file(service, owner, filename, options, *, source=None, timeout):
    format = options["format"]
    filename, _ = sanitize_filename(filename, fallback=f"文档.{format}")
    if Path(filename).suffix.lower() != "." + format:
        fail(422, "file_extension", "文件扩展名须与目标格式一致")
    if source and filename == source["filename"]:
        filename = str(Path(filename).with_stem(Path(filename).stem + "-修改副本"))
    try:
        result = operate(
            "edit" if source else "create",
            options,
            source_bytes(service, source) if source else None,
            timeout=timeout,
        )
    except ValueError as exc:
        fail(422, "file_operation_failed", str(exc))
    data = base64.b64decode(result.pop("file_base64"), validate=True)
    blob = service.archive.blobs.put_bytes(owner, data)
    return {
        "id": uid(),
        "filename": filename,
        "format": format,
        "storage_path": str(blob.path),
        "content_hash": blob.digest,
        "size": blob.size,
        "source_id": source["id"] if source else None,
        "source_version": source["version"] if source else None,
        "source_hash": source["content_hash"] if source else None,
        "validation": result["validation"],
    }


def commit_file(service, conn, owner, case_id, pending):
    if pending["source_id"]:
        source = service.library.own_document(conn, owner, pending["source_id"])
        if (
            source["case_id"] != case_id
            or source["version"] != pending["source_version"]
            or source["content_hash"] != pending["source_hash"]
        ):
            fail(409, "file_context_changed", "源文件范围或版本已变化，修改副本未保存")
    filename = pending["filename"]
    family = family_key(
        owner, filename, case_id=case_id, identity_key=dump(["document_operations", pending["id"]])
    )
    conn.execute(
        """INSERT INTO documents(
            id,owner_id,case_id,parent_id,title,filename,role,status,mime_type,size,version,
            content_hash,storage_path,tags,source_url,error,manifest,created_at,family_id
        ) VALUES(?,?,?,NULL,?,?,'primary','stored',?,?,1,?,?,?,NULL,NULL,?,?,?)""",
        (
            pending["id"],
            owner,
            case_id,
            Path(filename).stem,
            filename,
            MIMES[pending["format"]],
            pending["size"],
            pending["content_hash"],
            pending["storage_path"],
            dump([]),
            dump(
                {
                    "elements": [],
                    "chunks": [],
                    "document_operations": {
                        "source_id": pending["source_id"],
                        "source_version": pending["source_version"],
                        "validation": pending["validation"],
                    },
                }
            ),
            now(),
            family,
        ),
    )
    service.touch(conn, case_id)
    service.event(conn, case_id, "document_uploaded", "保存文档副本", filename)
    return {"id": pending["id"], "filename": filename}
