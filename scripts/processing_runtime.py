"""Shared bounded coordination for the operator processing runners."""

from __future__ import annotations

import sqlite3
import time


def retry_local_database(operation, *, on_wait):
    """Retry a rolled-back local transaction, never a model/download operation.

    The database's existing 15-second busy timeout applies to each of at most
    three attempts. The additional delay totals 0.6 seconds, below the existing
    120-second processing lease; callers retain stop/drain responsibility.
    """
    from app.contexts.ingestion.application.collection_jobs import _storage_busy

    for attempt in range(3):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not _storage_busy(exc) or attempt == 2:
                raise
            delay = 0.2 * (2**attempt)
            on_wait(attempt=attempt + 1, retry_delay_seconds=delay)
            time.sleep(delay)
