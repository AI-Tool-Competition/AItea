from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TypeVar

InputT = TypeVar("InputT")
ResultT = TypeVar("ResultT")


def bounded_ordered_map(
    items: Sequence[InputT],
    worker: Callable[[InputT], ResultT],
    *,
    max_concurrency: int,
    thread_name_prefix: str,
    progress_callback: Callable[[int, int, int], None] | None = None,
) -> list[ResultT]:
    """Run a bounded sliding window and restore results to input order."""
    if max_concurrency < 1:
        raise ValueError("max_concurrency must be positive")
    if not items:
        return []
    if max_concurrency == 1 or len(items) == 1:
        results: list[ResultT] = []
        for index, item in enumerate(items):
            results.append(worker(item))
            if progress_callback:
                remaining_active = 1 if index + 1 < len(items) else 0
                progress_callback(index + 1, len(items), remaining_active)
        return results

    worker_count = min(max_concurrency, len(items))
    executor = ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix=thread_name_prefix,
    )
    pending: dict[Future[ResultT], int] = {}
    completed: dict[int, ResultT] = {}
    next_index = 0

    def submit(index: int) -> None:
        pending[executor.submit(worker, items[index])] = index

    try:
        while next_index < worker_count:
            submit(next_index)
            next_index += 1

        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            completed_batch: list[tuple[int, ResultT]] = []
            for future in done:
                index = pending.pop(future)
                completed_batch.append((index, future.result()))
            completed.update(completed_batch)

            for _ in completed_batch:
                if next_index >= len(items):
                    break
                submit(next_index)
                next_index += 1
            if progress_callback:
                progress_callback(len(completed), len(items), len(pending))
    except Exception:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)

    return [completed[index] for index in range(len(items))]
