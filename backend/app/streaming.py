"""Bounded, cancellable transport for the existing synchronous workflow."""

import asyncio
import logging
import threading
from concurrent.futures import TimeoutError as FutureTimeout

from .models import DebugStreamEvent, MessageStreamEvent

logger = logging.getLogger(__name__)


class StreamCancelled(Exception):
    pass


async def message_events(service, owner, case_id, payload):
    async for event in workflow_events(
        lambda emit, check: service.post_message(owner, case_id, payload, emit=emit, check_cancelled=check),
        MessageStreamEvent,
        "正在读取业务资料…",
    ):
        yield event


async def debug_events(service, owner, payload):
    async for event in workflow_events(
        lambda emit, check: service.studio.debug(owner, payload, emit=emit, check_cancelled=check),
        DebugStreamEvent,
        "正在准备测试样例…",
    ):
        yield event


async def workflow_events(execute, envelope, initial_label):
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
                result = execute(emit, check_cancelled)
                emit("done", result)
            except AppError as error:
                emit("error", {"code": error.code, "message": error.message})
            except StreamCancelled:
                raise
            except Exception:
                logger.exception("workflow_stream_failed")
                emit("error", {"code": "stream_failed", "message": "回复未能完成，请重试。"})
        except StreamCancelled:
            pass

    worker = asyncio.create_task(asyncio.to_thread(work))
    try:
        yield envelope(event="status", data={"label": initial_label}).encode()
        while True:
            try:
                event, data = await asyncio.wait_for(queue.get(), timeout=10)
            except TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield envelope(event=event, data=data).encode()
            if event in ("done", "error"):
                break
    finally:
        cancelled.set()
        # The provider's cancellation watcher closes its async socket. Any
        # synchronous retrieval stops at the next workflow checkpoint.
        worker.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
