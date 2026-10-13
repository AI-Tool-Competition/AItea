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


def _render_pdf_page(content: bytes, page_number: int) -> dict:
    from importlib.metadata import version
    import pypdfium2 as pdfium

    with pdfium.PdfDocument(content) as document:
        if not 1 <= page_number <= len(document) <= 300:
            raise ValueError("PDF 页码或总页数超过限制。")
        page = document[page_number - 1]
        width, height = page.get_size()
        if not all(math.isfinite(value) and 0 < value <= 20000 for value in (width, height)):
            raise ValueError("PDF 页面尺寸无效。")
        scale = min(200 / 72, 4000 / max(width, height), math.sqrt(5_990_000 / (width * height)))
        bitmap = page.render(scale=scale, may_draw_forms=False)
        try:
            image = bitmap.to_pil()
            if image.width * image.height > 6_000_000:
                raise ValueError("PDF 渲染像素超过限制。")
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            data = buffer.getvalue()
            if len(data) > _MAX_IMAGE_BYTES:
                raise ValueError("PDF 单页图像超过 8 MB。")
            return {
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
                "render_version": "bounded-page-png-v1",
            }
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


def _ocr_one_page(content: bytes, page: dict, on_image: Callable[[dict], dict] | None = None) -> dict:
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
        updated.pop("error", None)
    finally:
        _OCR_SLOTS.release()
    return updated


def ocr_bounded(
    filename: str,
    content: bytes,
    mime_type: str,
    manifest: dict,
    *,
    on_page: Callable[[dict], None] | None = None,
    on_image: Callable[[dict], dict] | None = None,
    max_pages: int = 4,
) -> dict:
    """One bounded batch. Checkpoint each terminal page; completed pages are reused.

    Callbacks are persistence boundaries: exceptions propagate, never turn into
    model failures. The queue owns repeated batches, explicit retry and access.
    """
    from .vision import vision_ocr_configured

    if type(max_pages) is not int or not 1 <= max_pages <= 20:
        raise ValueError("单批图片识别页数必须在 1 至 20 之间。")
    if (
        not content
        or len(content) > _MAX_BYTES
        or manifest.get("source_hash") != hashlib.sha256(content).hexdigest()
    ):
        raise ValueError("识别原件 hash 不匹配或大小超过限制。")
    result = deepcopy(manifest)
    pages = result.get("pages", [])
    if (
        not pages
        or len(pages) > 300
        or [page.get("page") for page in pages] != list(range(1, len(pages) + 1))
        or any(page.get("source_hash") != result["source_hash"] for page in pages)
    ):
        raise ValueError("PDF 页映射不完整或原件 hash 不匹配。")
    pending = [page for page in pages if page["status"] == "pending_ocr"][:max_pages]
    if pending and vision_ocr_configured():
        executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="page-ocr")
        futures = [executor.submit(_ocr_one_page, content, page, on_image) for page in pending]
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
    return _run_bounded(_auxiliary_worker, ("assemble", result), 45)
