"""Resource-bounded parser process, independent of HTTP and persistence."""

from __future__ import annotations

import json
import base64
import hashlib
import io
import math
import multiprocessing
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from threading import BoundedSemaphore

_SLOTS = BoundedSemaphore(2)
_MAX_BYTES = 20 * 1024 * 1024
_MAX_RESULT_BYTES = 40 * 1024 * 1024
_OCR_SLOTS = BoundedSemaphore(2)
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ProcessingDeferred(ValueError):
    """Temporary local capacity exhaustion, never a document/provider failure."""

    retryable = True
    retry_after = 5


def _parse_worker(connection, filename: str, content: bytes, mime_type: str, parser: str) -> None:
    try:
        try:
            import resource

            memory_mb = 4096 if parser == "docling" else 768
            memory_limit = memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
            resource.setrlimit(resource.RLIMIT_CPU, (110 if parser == "docling" else 35,) * 2)
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except ImportError:
            pass  # Windows still has an enforced wall clock timeout.
        from .ingestion import parse_file

        result = parse_file(filename, content, mime_type, parser=parser)
        encoded = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > _MAX_RESULT_BYTES:
            raise ValueError("解析结果过大，请拆分文档后上传。")
        connection.send_bytes(encoded)
    except (MemoryError, OSError):
        connection.send_bytes(json.dumps({"error": "文档解析超过资源限制，请拆分文件或更换格式。"}).encode())
    except Exception as exc:
        # A parser message, never a traceback or source bytes, crosses the boundary.
        message = (
            str(exc)[:500] if isinstance(exc, ValueError) else "文档解析失败，请检查文件格式或解析器配置。"
        )
        connection.send_bytes(json.dumps({"error": message}, ensure_ascii=False).encode("utf-8"))
    finally:
        connection.close()


def parse_bounded(
    filename: str, content: bytes, mime_type: str = "", *, role: str = "primary", parser: str = "basic"
) -> dict:
    if role != "primary":
        raise ValueError("secondary 文档仅保存原件，禁止解析或建立索引。")
    if not content or len(content) > _MAX_BYTES:
        raise ValueError("文档不能为空且不能超过 20 MB。")
    timeout = float(os.getenv("TEA_PARSE_TIMEOUT", "120" if parser == "docling" else "45"))
    if not 0 < timeout <= 300:
        raise ValueError("解析超时配置必须在 0 至 300 秒之间。")
    return _run_bounded(_parse_worker, (filename, content, mime_type, parser), timeout)


def _run_bounded(worker: Callable, args: tuple, timeout: float) -> dict:
    if not _SLOTS.acquire(timeout=5):
        raise ProcessingDeferred("文档处理队列繁忙，已延后处理。")
    receive = send = process = None
    try:
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(target=worker, args=(send, *args), daemon=True)
        process.start()
        send.close()
        if not receive.poll(timeout):
            raise ValueError("文档解析超时，原件已保留；请拆分文件或更换格式后重试。")
        try:
            payload = json.loads(receive.recv_bytes(_MAX_RESULT_BYTES))
        except (EOFError, OSError, ValueError) as exc:
            raise ValueError("文档解析进程中断或结果超过限制，请检查文件。") from exc
        if "error" in payload:
            raise ValueError(payload["error"])
        return payload["result"]
    finally:
        try:
            if receive is not None:
                receive.close()
            if send is not None:
                send.close()
            if process is not None and process.pid is not None:
                process.join(timeout=0.5)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)
                process.close()
        finally:
            _SLOTS.release()


def _auxiliary_worker(connection, operation: str, payload) -> None:
    """Rendering/reassembly get their own per-page CPU and memory deadline."""
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (35,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if operation == "render":
            result = _render_pdf_page(*payload)
        elif operation == "render_region":
            result = _render_pdf_page(*payload)
        elif operation == "plan_recovery":
            from .ocr_recovery import plan_regions

            result = plan_regions(*payload)
        elif operation == "validate_archive":
            from .archive_parsing import _open_original, _verify_hash, _verify_identity

            path, source_hash, size = payload
            stream, info = _open_original(path, size)
            with stream:
                _verify_hash(stream, source_hash, size)
                _verify_identity(path, info, size)
            result = {"source_hash": source_hash, "size": size}
        else:
            from .ingestion import rebuild_pdf_manifest

            result = rebuild_pdf_manifest(payload)
        encoded = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > _MAX_RESULT_BYTES:
            raise ValueError("识别结果超过处理限制。")
        connection.send_bytes(encoded)
    except Exception as exc:
        message = str(exc)[:500] if isinstance(exc, ValueError) else "页面处理失败或超过资源限制。"
        connection.send_bytes(json.dumps({"error": message}, ensure_ascii=False).encode())
    finally:
        connection.close()


def _render_pdf_page(content: bytes, page_number: int, region_bbox: list | None = None) -> dict:
    from importlib.metadata import version
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(content) as document:
        if not 1 <= page_number <= len(document) <= 300:
            raise ValueError("PDF 页码或总页数超过限制。")
        page = document[page_number - 1]
        width, height = page.get_size()
        if not all(math.isfinite(value) and 0 < value <= 20000 for value in (width, height)):
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
            (400 if region_bbox is not None else 200) / 72,
            4000 / max(canvas_width, canvas_height),
            math.sqrt(5_990_000 / (canvas_width * canvas_height)),
        )
        bitmap = page.render(scale=scale, crop=crop, may_draw_forms=False)
        try:
            image = bitmap.to_pil()
            if image.width * image.height > 6_000_000:
                raise ValueError("PDF 渲染像素超过限制。")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            data = buffer.getvalue()
            if len(data) > _MAX_IMAGE_BYTES:
                raise ValueError("PDF 单页图像超过 8 MB。")
            result = {
                "source_hash": hashlib.sha256(content).hexdigest(),
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
                        [result["source_hash"], page_number, region_bbox, result["render_version"]],
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
            return result
        finally:
            bitmap.close()
            page.close()


def render_pdf_page_bounded(content: bytes, page_number: int) -> dict:
    if (
        not content
        or len(content) > _MAX_BYTES
        or type(page_number) is not int
        or not 1 <= page_number <= 300
    ):
        raise ValueError("PDF 大小或页码超过限制。")
    return _run_bounded(_auxiliary_worker, ("render", (content, page_number)), 45)


def render_pdf_region_bounded(content: bytes, page_number: int, region_bbox: list) -> dict:
    if (
        not content
        or len(content) > _MAX_BYTES
        or type(page_number) is not int
        or not 1 <= page_number <= 300
        or not isinstance(region_bbox, (list, tuple))
        or len(region_bbox) != 4
        or any(
            type(value) not in {int, float} or not math.isfinite(value) or not 0 <= value <= 20000
            for value in region_bbox
        )
        or region_bbox[0] >= region_bbox[2]
        or region_bbox[1] >= region_bbox[3]
    ):
        raise ValueError("PDF 局部区域、大小或页码无效。")
    return _run_bounded(_auxiliary_worker, ("render_region", (content, page_number, list(region_bbox))), 45)


def _archive_reference(path, source_hash: str, size: int) -> tuple[str, str, int]:
    try:
        file_path = os.fspath(path)
    except TypeError as exc:
        raise ValueError("采集原件路径无效。") from exc
    if (
        not isinstance(file_path, str)
        or not file_path
        or "\0" in file_path
        or not isinstance(source_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
        or type(size) is not int
        or not 0 < size <= 128 * 1024 * 1024
    ):
        raise ValueError("采集原件路径、hash 或大小无效。")
    return file_path, source_hash, size


def plan_pdf_ocr_recovery_bounded(
    content: bytes | None, page: dict, *, archive_path=None, archive_size: int | None = None
) -> list[dict]:
    if content is None:
        file_path, _, size = _archive_reference(archive_path, page.get("source_hash"), archive_size)
        return _run_bounded(_auxiliary_worker, ("plan_recovery", (None, page, file_path, size)), 45)
    if (
        archive_path is not None
        or not content
        or len(content) > _MAX_BYTES
        or page.get("source_hash") != hashlib.sha256(content).hexdigest()
    ):
        raise ValueError("局部补救原件 hash 不匹配或大小超过限制。")
    return _run_bounded(_auxiliary_worker, ("plan_recovery", (content, page)), 45)


def _ocr_one_page(
    content: bytes | None,
    page: dict,
    on_image: Callable[[dict], dict] | None = None,
    on_region: Callable[[dict], dict] | None = None,
    on_page: Callable[[dict], None] | None = None,
    archive_path=None,
    archive_size: int | None = None,
) -> dict:
    from .ingestion_quality import validate_ocr_page
    from .vision import OCRResponseError, PROMPT_VERSION, transcribe_page

    updated = deepcopy(page)

    def defer(reason: str) -> dict:
        updated.update(status="pending_ocr", deferred_reason=reason)
        updated.pop("error", None)
        return updated

    if not _OCR_SLOTS.acquire(timeout=5):
        return defer("ocr_capacity")
    updated.pop("deferred_reason", None)
    if on_region is not None and (
        page.get("status") == "ocr_uncertain" or (page.get("ocr") or {}).get("phase") == "recovery"
    ):
        try:
            from .ocr_recovery import recover_page_step

            return recover_page_step(
                content,
                updated,
                on_region=on_region,
                on_page=on_page,
                archive_path=archive_path,
                archive_size=archive_size,
            )
        except ProcessingDeferred:
            return defer("render_capacity")
        finally:
            _OCR_SLOTS.release()
    previous_attempts = (page.get("ocr") or {}).get("attempts", 0)
    metadata = {
        "prompt_version": PROMPT_VERSION,
        "requested_model": os.getenv("LLM_MODEL", "")[:200],
        "source_hash": page["source_hash"],
        "page": page["page"],
        "attempts": previous_attempts
        if type(previous_attempts) is int and 0 <= previous_attempts <= 3
        else 0,
    }

    def failure(exc: ValueError | OSError) -> dict:
        if isinstance(exc, OCRResponseError):
            metadata["finish_reason"] = exc.finish_reason
        retryable = isinstance(exc, OCRResponseError) and exc.retryable and metadata["attempts"] < 3
        updated.update(
            status="pending_ocr" if retryable else "ocr_failed",
            ocr=metadata,
            error=str(exc)[:500] if isinstance(exc, ValueError) else "页面识别失败，请稍后重试。",
        )
        if retryable:
            updated["deferred_reason"] = "provider_retry"
        return updated

    try:
        try:
            if content is None:
                from .archive_parsing import render_archive_page_bounded

                rendered = render_archive_page_bounded(
                    archive_path, source_hash=page["source_hash"], size=archive_size, page_number=page["page"]
                )
            else:
                rendered = render_pdf_page_bounded(content, page["page"])
        except ProcessingDeferred:
            return defer("render_capacity")
        except (ValueError, OSError) as exc:
            return failure(exc)
        metadata.update({key: value for key, value in rendered.items() if key != "image_base64"})
        # Persist exactly the sent pixels before the external request. A storage
        # callback failure must stop the job, not be reclassified as model error.
        if on_image is not None:
            reference = on_image(deepcopy(rendered))
            if not isinstance(reference, dict):
                raise ValueError("图像存储回调必须返回持久引用。")
            metadata["image_blob"] = reference
        try:
            metadata["attempts"] += 1
            transcript = transcribe_page({**rendered, "native_text": page["native_text"]})
        except (ValueError, OSError) as exc:
            return failure(exc)
        if transcript["is_blank"] and page["native_text"].strip():
            transcript["uncertain_spans"].append("blank_response_conflicts_with_native_text")
        elif len(page["native_text"]) > 200 and len(transcript["text"]) < len(page["native_text"]) * 0.5:
            transcript["uncertain_spans"].append("transcription_much_shorter_than_native_text")
        if "table_layout" in page.get("reasons", []) and not re.search(
            r"(?m)^\s*\|(?:[ \t]*:?-{2,}:?[ \t]*\|)+[ \t]*$", transcript["text"]
        ):
            transcript["uncertain_spans"].append("table_layout_not_preserved")
        metadata.update(transcript)
        updated.update(
            text=transcript["text"],
            printed_page=transcript["printed_page"],
            ocr=metadata,
            status="ocr_uncertain" if transcript["uncertain_spans"] else "ocr_complete",
        )
        validate_ocr_page(updated)
        if on_region is not None and updated["status"] == "ocr_uncertain":
            # Checkpoint the original full transcription first. The following
            # queue step initializes a separately audited recovery operation.
            updated["ocr"]["phase"] = "recovery"
            updated["status"] = "pending_ocr"
        updated.pop("error", None)
    finally:
        _OCR_SLOTS.release()
    return updated


def rebuild_pdf_bounded(manifest: dict) -> dict:
    """Revalidate saved page text and rebuild derived content in a bounded worker."""
    if manifest.get("archive_pdf"):
        from .archive_parsing import archive_rebuild_bounded

        return archive_rebuild_bounded(manifest)
    return _run_bounded(_auxiliary_worker, ("assemble", manifest), 45)


def ocr_bounded(
    filename: str,
    content: bytes | None,
    mime_type: str,
    manifest: dict,
    *,
    on_page: Callable[[dict], None] | None = None,
    on_image: Callable[[dict], dict] | None = None,
    on_region: Callable[[dict], dict] | None = None,
    max_pages: int = 4,
    archive_path=None,
    archive_size: int | None = None,
) -> dict:
    """One bounded batch. Checkpoint each terminal page; completed pages are reused.

    Callbacks are persistence boundaries: exceptions propagate, never turn into
    model failures. The queue owns repeated batches, explicit retry and access.
    """
    from .vision import vision_ocr_configured

    if type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise ValueError("单批图片识别页数必须在 1 至 20 之间。")
    if content is None:
        reference = _archive_reference(archive_path, manifest.get("source_hash"), archive_size)
        _run_bounded(_auxiliary_worker, ("validate_archive", reference), 45)
    elif archive_path is not None or (
        not content
        or len(content) > _MAX_BYTES
        or manifest.get("source_hash") != hashlib.sha256(content).hexdigest()
    ):
        raise ValueError("识别原件 hash 不匹配或大小超过限制。")
    result = deepcopy(manifest)
    pages = result.get("pages", [])
    from .ocr_recovery import RECOVERY_VERSION
    from .ingestion_quality import pdf_page_status

    if (
        not pages
        or len(pages) > (5000 if content is None else 300)
        or [page.get("page") for page in pages] != list(range(1, len(pages) + 1))
        or any(page.get("source_hash") != result["source_hash"] for page in pages)
    ):
        raise ValueError("PDF 页映射不完整或原件 hash 不匹配。")
    pending = [
        page
        for page in pages
        if pdf_page_status(page) == "pending_ocr"
        or (
            on_region is not None
            and pdf_page_status(page) == "ocr_uncertain"
            and (
                (page.get("ocr") or {}).get("recovery", {}).get("version") != RECOVERY_VERSION
                or (page.get("ocr") or {}).get("recovery", {}).get("status")
                not in {"complete", "conflict", "failed"}
            )
        )
    ][:max_pages]
    if pending and vision_ocr_configured():
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="page-ocr")
        futures = [
            executor.submit(
                _ocr_one_page, content, page, on_image, on_region, on_page, archive_path, archive_size
            )
            for page in pending
        ]
        try:
            for future in as_completed(futures):
                page = future.result()
                if on_page is not None:
                    on_page(deepcopy(page))
                pages[page["page"] - 1] = page
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    # Also rebuild when resuming after the very last page checkpoint, or when
    # no pages are pending. Cached chunks may precede the checkpointed text.
    return rebuild_pdf_bounded(result)
