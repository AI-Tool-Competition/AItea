"""Resource-bounded parser process, independent of HTTP and persistence."""

from __future__ import annotations

import json
import multiprocessing
import os
from threading import BoundedSemaphore

_SLOTS = BoundedSemaphore(2)
_MAX_BYTES = 20 * 1024 * 1024
_MAX_RESULT_BYTES = 40 * 1024 * 1024


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
    if not _SLOTS.acquire(timeout=5):
        raise ValueError("文档处理队列繁忙，请稍后重试。")
    receive = send = process = None
    try:
        context = multiprocessing.get_context("spawn")
        receive, send = context.Pipe(duplex=False)
        process = context.Process(
            target=_parse_worker, args=(send, filename, content, mime_type, parser), daemon=True
        )
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
