"""Bounded single-node polling; source due times/leases belong to the durable store."""

from __future__ import annotations

import logging
from threading import Event, Lock, Thread
from typing import Protocol

from app.contexts.documents.application.system_library import BackgroundWorkerScope

logger = logging.getLogger(__name__)


class SourceService(Protocol):
    def due_sources(self, *, worker_scope=BackgroundWorkerScope()) -> list[dict]: ...

    def fetch_source(self, owner_id: str, source_id: str) -> object: ...


class ScheduledCollector:
    def __init__(
        self,
        service: SourceService,
        *,
        interval_seconds: float = 60,
        batch_size: int = 10,
        worker_scope=BackgroundWorkerScope(),
    ):
        if interval_seconds <= 0 or batch_size < 1:
            raise ValueError("The polling interval and batch size must be positive")
        self.service = service
        worker_scope.sql()
        self.worker_scope = worker_scope
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self._stop = Event()
        self._cycle_lock = Lock()
        self._thread: Thread | None = None
        self.last_recovery: dict[str, int] = {}

    def run_once(self) -> dict[str, int]:
        result = {"succeeded": 0, "failed": 0}
        if self._stop.is_set() or not self._cycle_lock.acquire(blocking=False):
            return result
        try:
            collection = getattr(self.service, "collection", None)
            recovery = getattr(collection, "recovery", None)
            if recovery is not None:
                try:
                    self.last_recovery = recovery.run_due(
                        limit=min(self.batch_size, 32), **self.worker_scope.kwargs()
                    )
                except Exception:
                    result["failed"] += 1
                    logger.exception("Could not resume due collection failures")
            try:
                sources = self.service.due_sources(**self.worker_scope.kwargs())[: self.batch_size]
            except Exception:
                logger.exception("Could not read due document sources")
                return {"succeeded": 0, "failed": 1}
            for source in sources:
                if self._stop.is_set():
                    break
                try:
                    if collection is not None:
                        collection.enqueue_scheduled(source["owner_id"], source["id"])
                    else:
                        self.service.fetch_source(source["owner_id"], source["id"])
                except Exception:
                    result["failed"] += 1
                    logger.exception("Scheduled source collection failed (source_id=%s)", source.get("id"))
                else:
                    result["succeeded"] += 1
            return result
        finally:
            self._cycle_lock.release()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval_seconds)

    def start(self) -> ScheduledCollector:
        if self._thread is not None:
            return self
        self._thread = Thread(target=self._run, name="tea-source-collector", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)


def start_scheduler(
    service: SourceService, *, enabled: bool = True, worker_scope=BackgroundWorkerScope()
) -> ScheduledCollector | None:
    return ScheduledCollector(service, worker_scope=worker_scope).start() if enabled else None


def stop_scheduler(handle: ScheduledCollector | None) -> None:
    if handle is not None:
        handle.stop()
