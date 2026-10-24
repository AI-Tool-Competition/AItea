"""Bounded file-backed DOCX mapping; uploads retain their separate 20 MiB limit."""

import hashlib
import json
import os

from app.contexts.documents.infrastructure.parsers.archive_parsing import (
    _open_original,
    _validate_original_request,
    _verify_hash,
    _verify_identity,
)
from app.contexts.documents.infrastructure.parsers.docx_parsing import parse_docx
from app.contexts.documents.infrastructure.parsers.document_parser import _MAX_RESULT_BYTES, _run_bounded

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
PROTOCOL = "file-source-docx-v1"


def read_docx_source(path, *, source_hash, size):
    """Read one immutable source in a child, never fetch an external relationship."""
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
        raise ValueError("采集 DOCX 原件大小必须为 1 至 64 MiB。")
    path = _validate_original_request(path, source_hash, size)
    try:
        stream, info = _open_original(path, size)
        with stream:
            _verify_hash(stream, source_hash, size)
            content = stream.read(size + 1)
            if len(content) != size or hashlib.sha256(content).hexdigest() != source_hash:
                raise ValueError("DOCX 原件在读取时发生变化。")
            _verify_hash(stream, source_hash, size)
            _verify_identity(path, info, size)
            after = os.fstat(stream.fileno())
            if (after.st_mtime_ns, after.st_ctime_ns) != (info.st_mtime_ns, info.st_ctime_ns):
                raise ValueError("DOCX 原件在读取时发生变化。")
            return content
    except ValueError as exc:
        raise ValueError(str(exc).replace("PDF", "DOCX")) from None


def _worker(connection, path, source_hash, size, filename):
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (35,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        stream, info = _open_original(path, size)
        with stream:
            content = read_docx_source(path, source_hash=source_hash, size=size)
            result = parse_docx(content, filename=filename, max_bytes=MAX_ARCHIVE_BYTES)
            result["archive_docx"] = {"version": PROTOCOL, "size": size, "source_hash": source_hash}
            _verify_hash(stream, source_hash, size)
            _verify_identity(path, info, size)
            after = os.fstat(stream.fileno())
            if (after.st_mtime_ns, after.st_ctime_ns) != (info.st_mtime_ns, info.st_ctime_ns):
                raise ValueError("DOCX 原件在解析时发生变化。")
        encoded = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > _MAX_RESULT_BYTES:
            raise ValueError("DOCX 解析结果超过处理限制。")
        connection.send_bytes(encoded)
    except (MemoryError, OSError):
        connection.send_bytes(json.dumps({"error": "DOCX 原件读取失败或超过资源限制。"}).encode())
    except Exception as exc:
        message = str(exc)[:500] if isinstance(exc, ValueError) else "DOCX 原件解析失败。"
        connection.send_bytes(json.dumps({"error": message}, ensure_ascii=False).encode())
    finally:
        connection.close()


def parse_archive_docx_bounded(path, *, source_hash, size, filename="document.docx"):
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES:
        raise ValueError("采集 DOCX 原件大小必须为 1 至 64 MiB。")
    path = _validate_original_request(path, source_hash, size)
    return _run_bounded(_worker, (path, source_hash, size, filename), 45)
