"""Durable original-GET intents and conservative transfer budgets for one schedule."""

from __future__ import annotations

from contextlib import closing, contextmanager
from pathlib import Path
import re
import sqlite3
import time
import uuid
from urllib.parse import urlsplit


class BudgetBlocked(ValueError):
    """No request was sent; a persisted acquisition bound requires review."""


def initialize(root: Path):
    path = root / "transfers.sqlite3"
    if path.exists() or path.is_symlink():
        raise ValueError("transfer budget already exists")
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript("""
            PRAGMA journal_mode=DELETE;
            PRAGMA synchronous=FULL;
            PRAGMA user_version=1;
            CREATE TABLE requests (
                sequence INTEGER PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                cycle TEXT NOT NULL, url TEXT NOT NULL, reserved_bytes INTEGER NOT NULL,
                status TEXT NOT NULL, started_at REAL NOT NULL, finished_at REAL,
                actual_bytes INTEGER, sha256 TEXT, http_status INTEGER, error_type TEXT);
            CREATE INDEX requests_url ON requests(url, sequence);
            CREATE INDEX requests_cycle ON requests(cycle);
        """)
    path.chmod(0o600)


def connection(root):
    path = root / "transfers.sqlite3"
    if path.is_symlink() or not path.is_file():
        raise ValueError("persistent transfer budget is missing; cannot reset it")
    conn = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    if conn.execute("PRAGMA user_version").fetchone()[0] != 1 or {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    } != {"requests"}:
        conn.close()
        raise ValueError("invalid transfer-budget database")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def summary(root):
    with closing(connection(root)) as conn:
        return dict(
            conn.execute("""SELECT count(*) AS requests,
            coalesce(sum(reserved_bytes),0) AS charged_bytes,
            coalesce(sum(status IN ('intent','body_received')),0) AS unknown_requests,
            coalesce(sum(status='success'),0) AS successful_requests FROM requests""").fetchone()
        )


def reserve(root, config, cycle, url, maximum):
    with closing(connection(root)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute(
            "SELECT 1 FROM requests WHERE url=? AND status='permanent_failure' LIMIT 1", (url,)
        ).fetchone():
            raise BudgetBlocked("original URL has a terminal HTTP failure; metadata changes cannot reset it")
        failures = conn.execute(
            """SELECT count(*) FROM requests WHERE url=? AND sequence >
            coalesce((SELECT max(sequence) FROM requests WHERE url=? AND status='success'),0)""",
            (url, url),
        ).fetchone()[0]
        if failures >= config["max_original_attempts"]:
            raise BudgetBlocked("original dispatch attempts exhausted, including interrupted requests")
        total, current = conn.execute(
            """SELECT coalesce(sum(reserved_bytes),0),
            coalesce(sum(CASE WHEN cycle=? THEN reserved_bytes ELSE 0 END),0) FROM requests""",
            (cycle,),
        ).fetchone()
        allowance = min(
            maximum, config["original_byte_budget"] - current, config["total_original_byte_budget"] - total
        )
        if allowance <= 0:
            raise BudgetBlocked("original transfer byte budget exhausted")
        request_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO requests(request_id,cycle,url,reserved_bytes,status,started_at) VALUES(?,?,?,?,?,?)",
            (request_id, cycle, url, allowance, "intent", time.time()),
        )
        return request_id, allowance


def finish(root, request_id, *, response=None, error=None):
    with closing(connection(root)) as conn, conn:
        if error is None:
            blob = response["blob"]
            row = conn.execute(
                "SELECT reserved_bytes FROM requests WHERE request_id=? AND status='intent'", (request_id,)
            ).fetchone()
            if row is None or not 0 < blob.size <= row[0]:
                raise ValueError("download exceeded its durable original reservation")
            conn.execute(
                """UPDATE requests SET status='body_received',reserved_bytes=?,actual_bytes=?,sha256=?,
                finished_at=? WHERE request_id=? AND status='intent'""",
                (blob.size, blob.size, blob.digest, time.time(), request_id),
            )
        else:
            matched = re.search(r"HTTP (\d{3})(?!\d)", str(error))
            status = int(matched[1]) if matched else None
            outcome = "permanent_failure" if status in {400, 401, 403, 404, 405, 410, 451} else "failed"
            # Failed/unknown transfers retain their full reservation: the
            # downloader does not report how many partial body bytes arrived.
            conn.execute(
                """UPDATE requests SET status=?,http_status=?,error_type=?,finished_at=?
                WHERE request_id=? AND status='intent'""",
                (outcome, status, type(error).__name__, time.time(), request_id),
            )


def confirm_receipts(root, cycle, originals):
    """Only a verified collector receipt closes an intent, not a returned HTTP body."""
    with closing(connection(root)) as conn, conn:
        for result in originals:
            for item in result["assets"]:
                conn.execute(
                    """UPDATE requests SET status='success' WHERE cycle=? AND url=?
                    AND sha256=? AND actual_bytes=? AND status='body_received'""",
                    (cycle, item["url"], item["sha256"], item["size"]),
                )


@contextmanager
def guarded_transport(module, config, cycle):
    """Install only in this isolated worker, preserving the existing downloader."""
    original = module._download
    root = cycle.parent.parent

    def download(url, **options):
        parsed = urlsplit(url)
        is_original = parsed.hostname == "assets.egazette.gov.sg" or (
            parsed.hostname == "jdih.setneg.go.id" and parsed.path == "/api/hukumproduk/pdf"
        )
        if not is_original:
            return original(url, **options)
        request_id, allowance = reserve(root, config, cycle.name, url, options["max_bytes"])
        try:
            response = original(url, **{**options, "max_bytes": allowance})
        except Exception as exc:
            finish(root, request_id, error=exc)
            raise
        finish(root, request_id, response=response)
        return response

    module._download = download
    try:
        yield
    finally:
        module._download = original
