"""File-backed, resumable native parsing for large collected PDF originals.

The archive source remains one immutable version. Windows describe absolute
physical pages of that source; no split PDF or whole-file parent buffer exists.
User uploads keep their separate 20 MB limit in ``parsing.parse_bounded``.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

from app.contexts.documents.infrastructure.parsers.document_parser import (
    _MAX_RESULT_BYTES,
    ProcessingDeferred,
    _run_bounded,
)
from app.contexts.documents.infrastructure.parsers.pdf_limits import ARCHIVE_MAX_PAGES

_MAX_BYTES = 300 * 1024 * 1024
_MAX_PAGES = ARCHIVE_MAX_PAGES
_MAX_WINDOW_PAGES = 64
_MAX_PAGE_STREAM_BYTES = 16 * 1024 * 1024
_MAX_TEXT = 4_000_000
_MAX_ELEMENTS = 20_000
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_HASH_BLOCK_BYTES = 1024 * 1024
_KNOWN_STATUSES = {"pending_parse", "native", "pending_ocr", "ocr_complete", "ocr_failed", "ocr_uncertain"}
_NATIVE_WARNING = "PDF 原生文本不保证扫描页或复杂表格准确；页状态保留待识别和待核对项。"


def needs_native(page):
    from app.contexts.documents.infrastructure.parsers.pdf_tables import VERSION
    from app.contexts.documents.infrastructure.parsers.pdf_text_visibility import (
        VERSION as VISIBILITY_VERSION,
    )

    return (
        (
            page.get("status") == "native"
            and not page.get("ocr")
            and page.get("native_visibility", {}).get("version") != VISIBILITY_VERSION
        )
        or page.get("status") == "pending_parse"
        or (
            page.get("status") == "pending_ocr"
            and page.get("reasons") == ["table_layout"]
            and not page.get("ocr", {}).get("image_blob")
            and page.get("native_grid_attempt") != VERSION
        )
    )


def parse_archive_pdf_bounded(
    path: str | os.PathLike[str],
    *,
    source_hash: str,
    size: int,
    filename: str = "",
    manifest: dict | None = None,
    max_pages: int = _MAX_WINDOW_PAGES,
) -> dict:
    """Parse at most one window and return a persistable full-source checkpoint.

    ``pending_parse`` pages must be processed before OCR/indexing is considered
    complete. On subsequent calls pass the persisted ``manifest``; every native
    or OCR checkpoint is reused without extracting that page again. The child
    verifies source size/hash before and after the bounded extraction window.
    """
    file_path = _validate_original_request(path, source_hash, size)
    if type(max_pages) is not int or not 1 <= max_pages <= _MAX_WINDOW_PAGES:
        raise ValueError("PDF 解析窗口必须为 1 至 64 页。")
    if not isinstance(filename, str) or len(filename) > 1024:
        raise ValueError("PDF 文件名超过处理限制。")
    checkpoint = _checkpoint_payload(manifest, source_hash)
    return _run_archive_bounded(
        _archive_pdf_worker,
        (file_path, source_hash, size, filename, checkpoint, max_pages, _MAX_PAGES),
    )


def parse_archive_pdf_pages_bounded(
    path: str | os.PathLike[str],
    *,
    source_hash: str,
    size: int,
    total_pages: int,
    first_page: int,
    max_pages: int = _MAX_WINDOW_PAGES,
) -> dict:
    """Extract only an absolute physical-page window, without derived chunks.

    The returned pages are evidence for a separate, resumable page archive.
    They are deliberately not a complete document manifest or an index input.
    Existing aggregate text/transport and per-process limits still apply to
    this operation; no previous window is sent to or rebuilt by the child.
    """
    file_path = _validate_original_request(path, source_hash, size)
    if type(total_pages) is not int or not 0 < total_pages <= _MAX_PAGES:
        raise ValueError(f"采集 PDF 页数必须为 1 至 {_MAX_PAGES} 页。")
    if type(first_page) is not int or not 1 <= first_page <= total_pages:
        raise ValueError("PDF 窗口起始物理页超出原件范围。")
    if type(max_pages) is not int or not 1 <= max_pages <= _MAX_WINDOW_PAGES:
        raise ValueError("PDF 解析窗口必须为 1 至 64 页。")
    return _run_archive_bounded(
        _archive_auxiliary_worker,
        ("pages", (file_path, source_hash, size, total_pages, first_page, max_pages, _MAX_PAGES)),
    )


def inspect_archive_pdf_bounded(path: str | os.PathLike[str], *, source_hash: str, size: int) -> dict:
    """Verify actual page count without extracting or replacing saved text."""
    file_path = _validate_original_request(path, source_hash, size)
    return _run_archive_bounded(_archive_auxiliary_worker, ("inspect", (file_path, source_hash, size)))


def render_archive_page_bounded(
    path: str | os.PathLike[str],
    *,
    source_hash: str,
    size: int,
    page_number: int,
    region_bbox: list | None = None,
    dpi: int = 200,
) -> dict:
    """Render one original physical page/region using a verified seekable file.

    Region coordinates use the original PDF page's top-left origin, in canvas
    units, and are independent of pixel scale: ``[left, top, right, bottom]``.
    The requested 200/400 DPI can be reduced by the existing pixel/edge limits.
    """
    file_path = _validate_original_request(path, source_hash, size)
    if type(page_number) is not int or not 1 <= page_number <= _MAX_PAGES:
        raise ValueError(f"采集 PDF 物理页码必须为 1 至 {_MAX_PAGES} 页。")
    if type(dpi) is not int or dpi not in {200, 400}:
        raise ValueError("PDF 渲染 DPI 必须为 200 或 400。")
    if region_bbox is not None:
        if (
            not isinstance(region_bbox, (list, tuple))
            or len(region_bbox) != 4
            or any(
                type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 20_000
                for value in region_bbox
            )
            or region_bbox[0] >= region_bbox[2]
            or region_bbox[1] >= region_bbox[3]
        ):
            raise ValueError("PDF 局部区域无效。")
        region_bbox = list(region_bbox)
    return _run_archive_bounded(
        _archive_auxiliary_worker,
        ("render", (file_path, source_hash, size, page_number, region_bbox, dpi, _MAX_PAGES)),
    )


def archive_rebuild_bounded(manifest: dict) -> dict:
    """Reassemble bounded saved page checkpoints, without opening the source.

    The trusted archive checkpoint already binds full-source size and page
    count. ``pending_parse`` remains incomplete even if OCR pages are finished.
    """
    if not isinstance(manifest, dict):
        raise ValueError("PDF 归档检查点格式无效。")
    source_hash = manifest.get("source_hash")
    metadata = manifest.get("archive_pdf")
    if not isinstance(metadata, dict) or metadata.get("version") != 1:
        raise ValueError("PDF 重组装需要完整原件归档检查点。")
    size, count = metadata.get("source_size"), metadata.get("total_pages")
    _validate_original_request("archive-checkpoint.pdf", source_hash, size)
    if type(count) is not int or not 0 < count <= _MAX_PAGES:
        raise ValueError(f"采集 PDF 页数必须为 1 至 {_MAX_PAGES} 页。")
    pages = manifest.get("pages")
    if not isinstance(pages, list) or len(pages) != count:
        raise ValueError("PDF 重组装检查点必须覆盖完整原件物理页。")
    checkpoint = _checkpoint_payload(manifest, source_hash)
    return _run_archive_bounded(_archive_auxiliary_worker, ("rebuild", (checkpoint, count)))


def _validate_original_request(path, source_hash, size) -> str:
    if type(size) is not int or not 0 < size <= _MAX_BYTES:
        raise ValueError("采集 PDF 原件不能为空且不能超过 300 MiB。")
    if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        raise ValueError("采集 PDF 必须绑定有效的 SHA-256 source hash。")
    try:
        file_path = os.fspath(path)
    except TypeError as exc:
        raise ValueError("PDF 原件路径无效。") from exc
    if not isinstance(file_path, str) or not file_path or "\0" in file_path:
        raise ValueError("PDF 原件路径无效。")
    return file_path


def _run_archive_bounded(worker, args: tuple) -> dict:
    try:
        return _run_bounded(worker, args, 45)
    except ProcessingDeferred as exc:
        raise ProcessingDeferred("大型 PDF 处理队列繁忙，已延后处理。") from exc


def _checkpoint_payload(manifest: dict | None, source_hash: str) -> dict | None:
    if manifest is None:
        return None
    if not isinstance(manifest, dict) or manifest.get("source_hash") != source_hash:
        raise ValueError("PDF 检查点 source hash 与完整原件不匹配。")
    if "pipeline_version" in manifest and (
        not isinstance(manifest["pipeline_version"], str) or len(manifest["pipeline_version"]) > 1024
    ):
        raise ValueError("PDF 检查点处理版本无效。")
    # Already derived chunks/elements are rebuilt from pages inside the worker;
    # carrying both would duplicate large text buffers through multiprocessing.
    checkpoint = {
        key: manifest[key]
        for key in ("source_hash", "pipeline_version", "title", "warnings", "pages", "archive_pdf")
        if key in manifest
    }
    try:
        encoded_size = len(json.dumps(checkpoint, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("PDF 检查点格式无效。") from exc
    if encoded_size > _MAX_RESULT_BYTES:
        raise ValueError("PDF 检查点超过 40 MB 处理限制。")
    return checkpoint


def _archive_pdf_worker(connection, path, source_hash, size, filename, checkpoint, max_pages, page_limit):
    try:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (35,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except ImportError:
            pass
        result = _parse_archive_window(path, source_hash, size, filename, checkpoint, max_pages, page_limit)
        encoded = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_RESULT_BYTES:
            raise ValueError("PDF 聚合解析结果超过 40 MB 处理限制。")
        connection.send_bytes(encoded)
    except (MemoryError, OSError):
        connection.send_bytes(json.dumps({"error": "PDF 原件无法读取或窗口超过资源限制。"}).encode())
    except Exception as exc:
        message = str(exc)[:500] if isinstance(exc, ValueError) else "PDF 无法读取，请检查文件是否损坏。"
        connection.send_bytes(json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"))
    finally:
        connection.close()


def _archive_auxiliary_worker(connection, operation, payload):
    try:
        try:
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
            resource.setrlimit(resource.RLIMIT_CPU, (35,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except ImportError:
            pass
        if operation == "render":
            result = _render_archive_page(*payload)
        elif operation == "pages":
            result = _parse_archive_page_window(*payload)
        elif operation == "inspect":
            result = _inspect_archive_pdf(*payload)
        elif operation == "rebuild":
            from app.contexts.documents.infrastructure.parsers.ingestion_quality import (
                OCR_VALIDATION_VERSION,
                PDF_VISIBILITY_PIPELINES,
            )

            checkpoint, count = payload
            metadata = checkpoint["archive_pdf"]
            pages = _restore_pages(checkpoint, checkpoint["source_hash"], metadata["source_size"], count)
            visibility_required = str(checkpoint.get("pipeline_version", "")).split("+", 1)[
                0
            ] in PDF_VISIBILITY_PIPELINES or any("native_visibility" in page for page in pages)
            result = _assemble_archive_manifest(
                pages,
                checkpoint["source_hash"],
                metadata["source_size"],
                count,
                metadata.get("window_size", _MAX_WINDOW_PAGES),
                checkpoint,
                checkpoint.get("title", ""),
                f"archive-pdf-pages-v{3 if visibility_required else 2}"
                f"+vision-ocr-v2+{OCR_VALIDATION_VERSION}",
            )
        else:
            raise ValueError("未知 PDF 归档处理操作。")
        encoded = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_RESULT_BYTES:
            raise ValueError("PDF 聚合处理结果超过 40 MB 处理限制。")
        connection.send_bytes(encoded)
    except (MemoryError, OSError):
        connection.send_bytes(json.dumps({"error": "PDF 原件无法读取或页面超过资源限制。"}).encode())
    except Exception as exc:
        message = str(exc)[:500] if isinstance(exc, ValueError) else "PDF 页面处理失败，请检查原件。"
        connection.send_bytes(json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"))
    finally:
        connection.close()


def _render_archive_page(path, source_hash, size, page_number, region_bbox, dpi, page_limit):
    from importlib.metadata import version

    import pypdfium2 as pdfium

    stream, info = _open_original(path, size)
    with stream:
        _verify_hash(stream, source_hash, size)
        # BinaryIO input keeps the verified descriptor open. Passing the path
        # would reopen it and permit a replacement/symlink race after validation.
        with pdfium.PdfDocument(stream, autoclose=False) as document:
            if not 1 <= page_number <= len(document) <= page_limit:
                raise ValueError("PDF 物理页码或总页数超过限制。")
            page = document[page_number - 1]
            bitmap = image = None
            try:
                width, height = page.get_size()
                if not all(math.isfinite(value) and 0 < value <= 20_000 for value in (width, height)):
                    raise ValueError("PDF 页面尺寸无效。")
                crop = (0, 0, 0, 0)
                canvas_width, canvas_height = width, height
                if region_bbox is not None:
                    left, top, right, bottom = region_bbox
                    if not (0 <= left < right <= width and 0 <= top < bottom <= height):
                        raise ValueError("PDF 局部区域超出页面。")
                    canvas_width, canvas_height = right - left, bottom - top
                    crop = (left, height - bottom, width - right, top)
                scale = min(
                    dpi / 72,
                    4000 / max(canvas_width, canvas_height),
                    math.sqrt(5_990_000 / (canvas_width * canvas_height)),
                )
                bitmap = page.render(scale=scale, crop=crop, may_draw_forms=False, limit_image_cache=True)
                image = bitmap.to_pil()
                if image.width * image.height > 6_000_000:
                    raise ValueError("PDF 渲染像素超过限制。")
                with io.BytesIO() as buffer:
                    image.save(buffer, format="PNG")
                    data = buffer.getvalue()
                if len(data) > _MAX_IMAGE_BYTES:
                    raise ValueError("PDF 单页图像超过 8 MB。")
                result = {
                    "source_hash": source_hash,
                    "page": page_number,
                    "image_sha256": hashlib.sha256(data).hexdigest(),
                    "image_base64": base64.b64encode(data).decode("ascii"),
                    "mime_type": "image/png",
                    "width": image.width,
                    "height": image.height,
                    "dpi": round(scale * 72, 4),
                    "renderer": "pypdfium2",
                    "renderer_version": version("pypdfium2"),
                    "pdfium_version": str(pdfium.PDFIUM_INFO),
                    "render_version": "bounded-region-png-v1"
                    if region_bbox is not None
                    else "bounded-page-png-v1",
                }
                if region_bbox is not None:
                    result["region_bbox"] = region_bbox
                    result["region_id"] = hashlib.sha256(
                        json.dumps(
                            [source_hash, page_number, region_bbox, result["render_version"]],
                            separators=(",", ":"),
                        ).encode()
                    ).hexdigest()
            finally:
                if image is not None:
                    image.close()
                if bitmap is not None:
                    bitmap.close()
                page.close()
        _verify_hash(stream, source_hash, size)
        _verify_identity(path, info, size)
        return result


def _open_original(path: str, size: int):
    # O_NONBLOCK ensures a substituted FIFO cannot stall before fstat rejects it.
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError("PDF 原件必须是大小匹配的普通文件。")
        return os.fdopen(descriptor, "rb"), info
    except BaseException:
        os.close(descriptor)
        raise


def _verify_hash(stream, source_hash: str, size: int) -> None:
    stream.seek(0)
    digest = hashlib.sha256()
    consumed = 0
    while block := stream.read(_HASH_BLOCK_BYTES):
        consumed += len(block)
        if consumed > size:
            raise ValueError("PDF 原件大小发生变化。")
        digest.update(block)
    if consumed != size or digest.hexdigest() != source_hash:
        raise ValueError("PDF 原件 source hash 校验失败。")
    stream.seek(0)


def _verify_identity(path, info, size) -> None:
    current = os.stat(path, follow_symlinks=False)
    if (current.st_dev, current.st_ino, current.st_size) != (info.st_dev, info.st_ino, size):
        raise ValueError("PDF 原件路径在解析过程中发生变化。")


def _parse_archive_window(path, source_hash, size, filename, checkpoint, max_pages, page_limit):
    from pypdf import PdfReader

    stream, info = _open_original(path, size)
    with stream:
        _verify_hash(stream, source_hash, size)
        reader = PdfReader(stream, strict=False)
        if reader.is_encrypted:
            raise ValueError("不支持加密 PDF，请先提供解密后的文档。")
        count = len(reader.pages)
        if not 0 < count <= page_limit:
            raise ValueError(f"采集 PDF 页数必须为 1 至 {page_limit} 页。")
        pages = _restore_pages(checkpoint, source_hash, size, count)
        pending = [page for page in pages if needs_native(page)][:max_pages]
        characters = sum(max(len(page["native_text"]), len(page["text"])) for page in pages)
        for page in _extract_native_pages(
            reader, stream, path, source_hash, size, pending, page_limit, characters=characters
        ):
            pages[page["page"] - 1] = page
        # CAS files are immutable by design. Recheck bytes as well as identity
        # to reject in-place mutations or path replacement during extraction.
        _verify_hash(stream, source_hash, size)
        _verify_identity(path, info, size)
    return _assemble_archive_manifest(
        pages,
        source_hash,
        size,
        count,
        max_pages,
        checkpoint,
        checkpoint.get("title", "") if checkpoint else Path(filename or path).stem,
        "archive-pdf-pages-v3",
    )


def _inspect_archive_pdf(path, source_hash, size):
    from pypdf import PdfReader

    stream, info = _open_original(path, size)
    with stream:
        _verify_hash(stream, source_hash, size)
        reader = PdfReader(stream, strict=False)
        if reader.is_encrypted:
            raise ValueError("不支持加密 PDF，请先提供解密后的文档。")
        count = len(reader.pages)
        if not 0 < count <= _MAX_PAGES:
            raise ValueError(f"采集 PDF 页数必须为 1 至 {_MAX_PAGES} 页。")
        _verify_hash(stream, source_hash, size)
        _verify_identity(path, info, size)
    return {"source_hash": source_hash, "source_size": size, "total_pages": count}


def _parse_archive_page_window(path, source_hash, size, total_pages, first_page, max_pages, page_limit):
    import time

    from pypdf import PdfReader

    started = time.monotonic_ns()
    stream, info = _open_original(path, size)
    with stream:
        _verify_hash(stream, source_hash, size)
        reader = PdfReader(stream, strict=False)
        if reader.is_encrypted:
            raise ValueError("不支持加密 PDF，请先提供解密后的文档。")
        count = len(reader.pages)
        if count != total_pages or not 0 < count <= page_limit:
            raise ValueError("PDF 原件物理页数与窗口绑定不匹配。")
        pending = [
            {"page": number, "native_text": "", "text": ""}
            for number in range(first_page, min(count + 1, first_page + max_pages))
        ]
        pages = _extract_native_pages(reader, stream, path, source_hash, size, pending, page_limit)
        _verify_hash(stream, source_hash, size)
        _verify_identity(path, info, size)
    return {
        "protocol": "archive-pdf-native-window-v1",
        "source_hash": source_hash,
        "source_size": size,
        "total_pages": count,
        "first_page": pages[0]["page"],
        "last_page": pages[-1]["page"],
        "pages": pages,
        "quality": {"complete": False, "status": "native_evidence_only"},
        "derived_status": "not_generated",
        "index_status": "not_generated",
        "worker_stats": {
            "pid": os.getpid(),
            "started_monotonic_ns": started,
            "finished_monotonic_ns": time.monotonic_ns(),
        },
    }


def _extract_native_pages(reader, stream, path, source_hash, size, pending, page_limit, *, characters=0):
    """Shared extraction/audit, independent of whole-document assembly."""
    from app.contexts.documents.infrastructure.parsers.native_parser import (
        _audit_native_pdf_page,
        _pdf_large_raster,
        _pdf_table_layout,
    )

    pages = []
    for previous in pending:
        number = previous["page"]
        page = reader.pages[number - 1]
        content = page.get_contents()
        if content is not None and len(content.get_data()) > _MAX_PAGE_STREAM_BYTES:
            raise ValueError(f"PDF 第 {number} 页内容流超过 16 MB 处理限制。")
        text = (page.extract_text() or "").strip()
        characters += len(text) - max(len(previous["native_text"]), len(previous["text"]))
        if characters > _MAX_TEXT:
            raise ValueError("PDF 聚合正文超过 4,000,000 字符处理限制。")
        reasons = [] if text else ["empty_text"]
        if _pdf_large_raster(page):
            reasons.append("large_raster")
        if _pdf_table_layout(content, page.get("/Resources")):
            reasons.append("table_layout")
        pages.append(
            {
                "page": number,
                "printed_page": None,
                "native_text": text,
                "text": text,
                "status": "pending_ocr" if reasons else "native",
                "ocr_required": bool(reasons),
                "reasons": reasons,
                "source_hash": source_hash,
            }
        )
    if pages:
        import pypdfium2 as pdfium

        stream.seek(0)
        with pdfium.PdfDocument(stream) as document:
            for page in pages:
                pdfpage = document[page["page"] - 1]
                try:
                    _audit_native_pdf_page(
                        page,
                        pdfpage,
                        optional_content="/OCProperties" in reader.trailer["/Root"],
                        render_blank=lambda number: _render_archive_page(
                            path, source_hash, size, number, None, 200, page_limit
                        ),
                    )
                finally:
                    pdfpage.close()
    return pages


def _assemble_archive_manifest(pages, source_hash, size, count, max_pages, checkpoint, title, pipeline):
    from importlib.metadata import version

    from app.contexts.documents.infrastructure.parsers.ingestion_quality import validate_ocr_page
    from app.contexts.documents.infrastructure.parsers.native_parser import _parsed_result, _pdf_elements
    from app.shared.infrastructure.vendor.geo_parser.document_ingestion import ParsedDocument

    if sum(max(len(page["native_text"]), len(page["text"])) for page in pages) > _MAX_TEXT:
        raise ValueError("PDF 聚合正文超过 4,000,000 字符处理限制。")
    for page in pages:
        validate_ocr_page(page)
    elements = _pdf_elements(pages)
    if len(elements) > _MAX_ELEMENTS:
        raise ValueError("PDF 聚合结构超过 20,000 个元素处理限制。")
    warnings = [_NATIVE_WARNING]
    if checkpoint is not None:
        warnings = list(dict.fromkeys([*warnings, *checkpoint.get("warnings", [])]))
    result = _parsed_result(
        ParsedDocument("pypdf", version("pypdf"), pipeline, elements, [], warnings, pages),
        source_hash,
        title,
    )
    pending_numbers = [page["page"] for page in pages if needs_native(page)]
    result["archive_pdf"] = {
        "version": 1,
        "source_size": size,
        "total_pages": count,
        "parsed_pages": [page["page"] for page in pages if page["status"] != "pending_parse"],
        "pending_pages": pending_numbers,
        "complete": not pending_numbers,
        "window_size": max_pages,
    }
    result["quality"]["pending_parse_pages"] = pending_numbers
    if pending_numbers:
        result["quality"].update(status="needs_parse", complete=False)
    return result


def _restore_pages(checkpoint: dict | None, source_hash: str, size: int, count: int) -> list[dict]:
    restored = {}
    if checkpoint is not None:
        previous = checkpoint.get("archive_pdf", {})
        if not isinstance(previous, Mapping) or (
            previous and (previous.get("source_size") != size or previous.get("total_pages") != count)
        ):
            raise ValueError("PDF 检查点原件大小或页数不匹配。")
        existing = checkpoint.get("pages")
        if not isinstance(existing, list) or len(existing) > count:
            raise ValueError("PDF 页面检查点格式无效。")
        for page in existing:
            if not isinstance(page, dict):
                raise ValueError("PDF 页面检查点格式无效。")
            number = page.get("page")
            if type(number) is not int or not 1 <= number <= count or number in restored:
                raise ValueError("PDF 检查点物理页码不唯一或超出原件范围。")
            if page.get("source_hash") != source_hash or page.get("status") not in _KNOWN_STATUSES:
                raise ValueError("PDF 页面检查点 source hash 或状态不匹配。")
            if not all(isinstance(page.get(key), str) for key in ("native_text", "text")):
                raise ValueError("PDF 页面检查点正文无效。")
            if page["status"] == "pending_parse" and (page["text"] or page["native_text"]):
                raise ValueError("待解析 PDF 页面不能伪装成完整正文。")
            restored[number] = deepcopy(page)
        if not isinstance(checkpoint.get("title", ""), str) or len(checkpoint.get("title", "")) > 4096:
            raise ValueError("PDF 检查点标题超过限制。")
        warnings = checkpoint.get("warnings", [])
        if (
            not isinstance(warnings, list)
            or len(warnings) > 1000
            or any(not isinstance(item, str) or len(item) > 1000 for item in warnings)
        ):
            raise ValueError("PDF 检查点提示超过限制。")
    if sum(max(len(page["native_text"]), len(page["text"])) for page in restored.values()) > _MAX_TEXT:
        raise ValueError("PDF 聚合正文超过 4,000,000 字符处理限制。")
    return [
        restored.get(number)
        or {
            "page": number,
            "printed_page": None,
            "native_text": "",
            "text": "",
            "status": "pending_parse",
            "ocr_required": False,
            "reasons": [],
            "source_hash": source_hash,
        }
        for number in range(1, count + 1)
    ]
