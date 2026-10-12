"""Bounded, cancellable transport for the existing synchronous workflow."""

import asyncio
import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeout

from .models import MessageStreamEvent

logger = logging.getLogger(__name__)


class StreamCancelled(Exception):
    pass


async def message_events(service, owner, case_id, payload):
    from .service import AppError

    loop = asyncio.get_running_loop()
    queue = asyncio.Queue(maxsize=16)
    cancelled = threading.Event()

    def check_cancelled():
        if cancelled.is_set():
            raise StreamCancelled()

    def emit(event, data):
        check_cancelled()
        future = asyncio.run_coroutine_threadsafe(queue.put((event, data)), loop)
        try:
            while True:
                try:
                    future.result(timeout=0.25)
                    return
                except FutureTimeout:
                    check_cancelled()
        finally:
            if not future.done():
                future.cancel()

    def work():
        try:
            try:
                result = service.post_message(
                    owner, case_id, payload, emit=emit, check_cancelled=check_cancelled
                )
                emit("done", result)
            except AppError as error:
                emit("error", {"code": error.code, "message": error.message})
            except StreamCancelled:
                raise
            except Exception:
                logger.exception("chat_stream_failed")
                emit("error", {"code": "stream_failed", "message": "回复未能完成，请重试。"})
        except StreamCancelled:
            pass

    worker = asyncio.create_task(asyncio.to_thread(work))
    try:
        yield MessageStreamEvent(event="status", data={"label": "正在读取业务资料…"}).encode()
        while True:
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout=10)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield MessageStreamEvent(event=event, data=data).encode()
            if event in ("done", "error"):
                break
    finally:
        cancelled.set()
        # The provider's cancellation watcher closes its async socket. Any
        # synchronous retrieval stops at the next workflow checkpoint.
        worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
