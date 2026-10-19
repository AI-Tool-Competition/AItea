"""Real multi-process SQLite/CAS validation with explicitly simulated HTTPS transport.

The transport calls the production physical-connection limiter, but makes no
external request. Parsing, durable claims, CAS writes and recovery are real.
Set TEA_QUEUE_VALIDATION_REPORT_DIR to retain machine-readable measurements.
"""

import hashlib
import json
import logging
import multiprocessing
import os
import sqlite3
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
from pathlib import Path
from threading import Event, Lock
from urllib.parse import urlsplit

import pytest

from app import collection as module
from app import ingestion
from app.collection import CollectionWorkers
from app.db import now
from app.service import Service


OWNER = "parallel-validation-owner"
HOSTS = 8


def original(url):
    return (f"Validation original for {url}\nArticle 1: retain the verified original.\n" * 4).encode()


def prepare(path, monkeypatch, *, workers=16):
    monkeypatch.setenv("TEA_COLLECT_WORKERS", str(workers))
    monkeypatch.setenv("TEA_COLLECT_PER_HOST", "2")
    monkeypatch.setenv("TEA_PROCESS_WORKERS", "4")
    monkeypatch.setenv("TEA_PARSE_WORKERS", "4")
    service = Service(path)
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (OWNER, OWNER, None, None, 1, now()))
    source = service.save_source(
        OWNER,
        {
            "name": "Controlled concurrency validation",
            "url": "https://host-0.example/law-0.txt",
            "enabled": False,
            "interval_hours": 24,
            "tags": [],
            "plan": {"collection": "country:ID", "discover_attachments": False, "max_items": 500},
        },
    )
    return service, source


def enqueue(service, source, urls, key="validation"):
    return service.collection.enqueue(
        OWNER, source["id"], {"client_request_id": key, "entries": [{"url": url} for url in urls]}
    )


def counts(service):
    with closing(service.db.connect()) as conn:
        items = dict(conn.execute("SELECT status,COUNT(*) FROM collection_items GROUP BY status"))
        jobs = dict(conn.execute("SELECT status,COUNT(*) FROM processing_jobs GROUP BY status"))
        return {
            "items": items,
            "jobs": jobs,
            "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            "network_leases": conn.execute("SELECT COUNT(*) FROM collection_network_leases").fetchone()[0],
        }


def wait_for(predicate, *, seconds=30, message="Expected queue transition did not occur"):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.02)


def record(name, **metrics):
    result = {"scenario": name, "transport": "simulated; no external HTTPS throughput measured", **metrics}
    directory = os.getenv("TEA_QUEUE_VALIDATION_REPORT_DIR")
    if directory:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True)
        (path / f"{name}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print("QUEUE_VALIDATION " + json.dumps(result, ensure_ascii=False))


def verify_originals(service, expected_urls):
    with closing(service.db.connect()) as conn:
        docs = [dict(row) for row in conn.execute("SELECT * FROM documents")]
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert list(conn.execute("PRAGMA foreign_key_check")) == []
        duplicates = conn.execute(
            "SELECT COUNT(*) FROM (SELECT family_id,content_hash FROM documents "
            "GROUP BY family_id,content_hash HAVING COUNT(*)>1)"
        ).fetchone()[0]
        assert duplicates == 0
    assert {doc["source_url"] for doc in docs} == set(expected_urls)
    assert len(docs) == len(set(expected_urls))
    for doc in docs:
        content = Path(doc["storage_path"]).read_bytes()
        assert content == original(doc["source_url"])
        assert doc["content_hash"] == hashlib.sha256(content).hexdigest()
        assert doc["version"] == 1
    assert not list(service.archive.blobs.staging_dir.iterdir())
    assert len([path for path in service.archive.blobs.blobs_dir.rglob("*") if path.is_file()]) == len(docs)
    return len(docs)


class Errors(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)
        self.messages = []

    def emit(self, record):
        error = record.exc_info[1] if record.exc_info else None
        self.messages.append(
            {
                "message": self.format(record),
                "error_type": type(error).__name__ if error else None,
                "sqlite_errorcode": getattr(error, "sqlite_errorcode", None),
            }
        )


def process_worker(path, output, ready, stop, parse_ready, release, saturated, counters, metric_lock, limit):
    """Every process owns its own Service and actual CollectionWorkers coordinator."""
    handler = Errors()
    logging.getLogger("app.collection").addHandler(handler)
    calls = []
    worker = None
    try:
        service = Service(path)
        claim_processing = service.collection.claim_processing

        def paused_processing(**kwargs):
            return claim_processing(**kwargs) if parse_ready.is_set() else None

        service.collection.claim_processing = paused_processing

        def download(url, *, consume, connection_slot, **kwargs):
            host = urlsplit(url).hostname
            host_index = int(host.split("-", 1)[1].split(".", 1)[0])
            with connection_slot(host):
                with metric_lock:
                    counters[0] += 1
                    counters[1] = max(counters[1], counters[0])
                    counters[2 + host_index] += 1
                    counters[2 + HOSTS + host_index] = max(
                        counters[2 + HOSTS + host_index], counters[2 + host_index]
                    )
                    if counters[0] == limit:
                        saturated.set()
                    calls.append(url)
                try:
                    if not release.wait(30):
                        raise TimeoutError("Parent did not release the simulated transport")
                    time.sleep(0.025)
                    return {
                        "url": url,
                        "filename": url.rsplit("/", 1)[-1],
                        "mime_type": "text/plain",
                        "blob": consume([original(url)]),
                        "etag": '"validation-original"',
                    }
                finally:
                    with metric_lock:
                        counters[0] -= 1
                        counters[2 + host_index] -= 1

        ingestion._download = download
        ready.wait(timeout=30)
        worker = CollectionWorkers(service.collection).start()
        if not stop.wait(180):
            raise TimeoutError("Parent did not stop validation workers")
    finally:
        if worker is not None:
            worker.stop(timeout=30)
            if worker.thread.is_alive():
                handler.messages.append(
                    {"message": "Collection worker failed to stop", "sqlite_errorcode": None}
                )
        Path(output).write_text(json.dumps({"calls": calls, "errors": handler.messages}))


@pytest.mark.parametrize("limit", [8, 16])
def test_four_processes_share_limits_and_resume_after_real_backlog(tmp_path, monkeypatch, limit):
    service, source = prepare(tmp_path / "data", monkeypatch, workers=limit)
    urls = [f"https://host-{number % HOSTS}.example/law-{number}.txt" for number in range(256)]
    for repeat in range(2):
        for batch in range(2):
            enqueue(service, source, urls[batch * 128 : (batch + 1) * 128], key=f"{repeat}-{batch}")
    context = multiprocessing.get_context("spawn")
    ready = context.Barrier(5)
    stop, parse_ready, release, saturated = [context.Event() for _ in range(4)]
    counters, metric_lock = context.Array("i", 2 + HOSTS * 2, lock=False), context.Lock()
    processes = [
        context.Process(
            target=process_worker,
            args=(
                str(service.db.data_dir),
                str(tmp_path / f"worker-{index}.json"),
                ready,
                stop,
                parse_ready,
                release,
                saturated,
                counters,
                metric_lock,
                limit,
            ),
        )
        for index in range(4)
    ]
    started = time.monotonic()
    for process in processes:
        process.start()
    try:
        ready.wait(timeout=30)
        assert saturated.wait(30), counts(service)
        first_wave = counts(service)
        assert first_wave["items"].get("fetching") == first_wave["network_leases"] == limit
        release.set()

        def paused():
            state = counts(service)
            return (
                state["jobs"].get("queued", 0) >= 200
                and state["items"].get("fetching", 0) == 0
                and state["items"].get("queued", 0) > 0
            )

        wait_for(paused, seconds=60, message="Download queue did not pause at its processing backlog limit")
        backlog = counts(service)
        assert 200 <= backlog["documents"] <= 200 + limit - 1
        time.sleep(0.4)
        assert counts(service) == backlog, (
            "Downloads continued despite a full, deliberately paused parse queue"
        )
        parse_ready.set()

        def completed():
            state = counts(service)
            return state["items"] == {"stored": 512} and state["jobs"] == {"completed": 256}

        wait_for(
            completed, seconds=90, message="Queue did not drain after the actual bounded parsers resumed"
        )
    finally:
        release.set()
        parse_ready.set()
        stop.set()
        for process in processes:
            process.join(timeout=10)
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert [process.exitcode for process in processes] == [0] * 4
    outputs = [json.loads((tmp_path / f"worker-{index}.json").read_text()) for index in range(4)]
    calls = Counter(url for output in outputs for url in output["calls"])
    assert calls == Counter({url: 2 for url in urls}), "A queue item was downloaded twice or lost"
    errors = [error for output in outputs for error in output["errors"]]
    assert all(
        isinstance(error["sqlite_errorcode"], int)
        and error["sqlite_errorcode"] & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
        for error in errors
    ), errors
    assert counters[0] == 0 and counters[1] == limit
    assert max(counters[2 + HOSTS :]) <= 2
    assert counts(service)["network_leases"] == 0
    assert verify_originals(service, urls) == 256
    with closing(service.db.connect()) as conn:
        assert dict(conn.execute("SELECT status,COUNT(*) FROM collection_runs GROUP BY status")) == {
            "completed": 4
        }
        assert dict(conn.execute("SELECT attempts,COUNT(*) FROM collection_items GROUP BY attempts")) == {
            1: 512
        }
    record(
        f"four-process-limit-{limit}",
        processes=4,
        configured_download_workers=limit,
        peak_physical_connections=counters[1],
        peak_per_host_connections=max(counters[2 + HOSTS :]),
        completed_items=512,
        completed_originals=256,
        duplicate_originals=0,
        recovered_database_errors=len(errors),
        unexpected_errors=0,
        processing_backlog_at_pause=backlog["documents"],
        pending_items_at_pause=backlog["items"]["queued"],
        pending_items_after_drain=0,
        remaining_network_leases=0,
        remaining_staging_files=0,
        elapsed_seconds=round(time.monotonic() - started, 3),
        child_exitcodes=[process.exitcode for process in processes],
    )


def hold_database_writer(path, pipe):
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        pipe.send("locked")
        if not pipe.poll(15):
            raise TimeoutError("Parent did not release the validation SQLite writer")
        assert pipe.recv() == "release"
        conn.rollback()
    pipe.close()


def test_coordinator_recovers_after_real_external_sqlite_writer(tmp_path, monkeypatch):
    from app import parsing

    service, source = prepare(tmp_path / "data", monkeypatch, workers=8)
    monkeypatch.setattr(parsing, "_SLOTS", parsing._ParserSlots())
    url = "https://host-0.example/recovered.txt"
    enqueue(service, source, [url])
    real_connect = service.db.connect

    def quick_connect():
        conn = real_connect()
        conn.execute("PRAGMA busy_timeout=40")
        return conn

    monkeypatch.setattr(service.db, "connect", quick_connect)

    def download(url, *, consume, connection_slot, **kwargs):
        with connection_slot(urlsplit(url).hostname):
            return {
                "url": url,
                "filename": "recovered.txt",
                "mime_type": "text/plain",
                "blob": consume([original(url)]),
            }

    monkeypatch.setattr(ingestion, "_download", download)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=hold_database_writer, args=(str(service.db.path), child))
    handler = Errors()
    logger = logging.getLogger("app.collection")
    logger.addHandler(handler)
    worker = None
    started = time.monotonic()
    process.start()
    child.close()
    try:
        assert parent.poll(15) and parent.recv() == "locked"
        worker = CollectionWorkers(service.collection).start()
        wait_for(lambda: handler.messages, seconds=10, message="Expected real SQLITE_BUSY was not observed")
        assert service.collection.worker_health["last_error_at"]
        parent.send("release")
        process.join(timeout=5)
        assert process.exitcode == 0
        wait_for(lambda: counts(service)["jobs"] == {"completed": 1}, seconds=15)
        assert counts(service)["items"] == {"stored": 1}
        assert verify_originals(service, [url]) == 1
        with closing(service.db.connect()) as conn:
            assert conn.execute("SELECT attempts FROM collection_items").fetchone()[0] == 1
        assert all(
            error["error_type"] == "OperationalError"
            and error["sqlite_errorcode"] & 0xFF == sqlite3.SQLITE_BUSY
            for error in handler.messages
        ), handler.messages
        record(
            "external-writer-recovery",
            deliberately_recovered_database_errors=len(handler.messages),
            completed_items=1,
            completed_originals=1,
            duplicate_originals=0,
            download_attempts=1,
            busy_timeout_ms=40,
            production_busy_timeout_changed=False,
            elapsed_seconds=round(time.monotonic() - started, 3),
        )
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        if worker:
            worker.stop(timeout=10)
            assert not worker.thread.is_alive()
        logger.removeHandler(handler)


def crashed_claim_holder(path, pipe):
    service = Service(path)
    module.LEASE_SECONDS = 0.5
    with ExitStack() as stack:
        claims = []
        while claim := service.collection.claim_fetch():
            stack.enter_context(service.collection.network_slot(claim["host"], time.monotonic() + 0.5))
            claims.append(claim)
        pipe.send(claims)
        Event().wait(30)


def test_killed_process_recovers_real_leases_and_rejects_old_tokens(tmp_path, monkeypatch):
    service, source = prepare(tmp_path / "data", monkeypatch, workers=8)
    urls = [f"https://host-{number}.example/law-{number}.txt" for number in range(8)]
    enqueue(service, source, urls)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=crashed_claim_holder, args=(str(service.db.data_dir), child))
    process.start()
    child.close()
    started = time.monotonic()
    try:
        assert parent.poll(30)
        expired = parent.recv()
        assert len(expired) == 8
        process.terminate()
        process.join(timeout=5)
        assert process.exitcode is not None and process.exitcode != 0
        time.sleep(0.6)
        restarted = Service(service.db.data_dir)
        recovered = [restarted.collection.claim_fetch() for _ in range(8)]
        assert all(recovered)
        old_tokens = {claim["lease_token"] for claim in expired}
        assert not old_tokens.intersection(claim["lease_token"] for claim in recovered)
        with closing(restarted.db.connect()) as conn:
            before = [dict(row) for row in conn.execute("SELECT * FROM collection_items ORDER BY id")]
        restarted.collection.heartbeat([(False, claim) for claim in expired])
        for claim in expired:
            restarted.collection.fetch_item(claim)
            restarted.collection._failure(claim, ValueError("obsolete claim"), processing=False)
        with closing(restarted.db.connect()) as conn:
            assert [dict(row) for row in conn.execute("SELECT * FROM collection_items ORDER BY id")] == before
        assert counts(restarted)["documents"] == 0

        def download(url, *, consume, connection_slot, **kwargs):
            with connection_slot(urlsplit(url).hostname):
                return {
                    "url": url,
                    "filename": url.rsplit("/", 1)[-1],
                    "mime_type": "text/plain",
                    "blob": consume([original(url)]),
                }

        monkeypatch.setattr(ingestion, "_download", download)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(restarted.collection.fetch_item, recovered))
        assert counts(restarted)["items"] == {"stored": 8}
        assert counts(restarted)["network_leases"] == 0
        assert verify_originals(restarted, urls) == 8
        record(
            "killed-process-recovery",
            killed_process_exitcode=process.exitcode,
            recovered_items=8,
            stale_tokens_rejected=8,
            duplicate_originals=0,
            remaining_network_leases=0,
            elapsed_seconds=round(time.monotonic() - started, 3),
            expiry="real elapsed time; shortened test claim TTL; production physical lease grace retained",
        )
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def test_cancel_retry_at_sixteen_connections_preserves_physical_limits(tmp_path, monkeypatch):
    service, source = prepare(tmp_path / "data", monkeypatch)
    urls = [f"https://host-{number % HOSTS}.example/law-{number}.txt" for number in range(16)]
    run = enqueue(service, source, urls)
    first_ready, release, retry_waiting = Event(), Event(), Event()
    lock = Lock()
    active = peak = calls = 0

    def download(url, *, consume, connection_slot, **kwargs):
        nonlocal active, peak, calls
        with lock:
            calls += 1
            call = calls
            if calls == 32:
                retry_waiting.set()
        with connection_slot(urlsplit(url).hostname):
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 16:
                    first_ready.set()
            try:
                if call <= 16:
                    assert release.wait(15)
                return {
                    "url": url,
                    "filename": url.rsplit("/", 1)[-1],
                    "mime_type": "text/plain",
                    "blob": consume([original(url)]),
                }
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(ingestion, "_download", download)
    other = Service(service.db.data_dir)
    claims = [service.collection.claim_fetch() for _ in range(16)]
    assert all(claims)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=32) as pool:
        first = [pool.submit(service.collection.fetch_item, claim) for claim in claims]
        try:
            assert first_ready.wait(15)
            cancelled = other.collection.control(OWNER, run["id"], "cancel", run["revision"])
            assert counts(service)["network_leases"] == 16
            other.collection.control(OWNER, run["id"], "retry", cancelled["revision"])
            retried = [other.collection.claim_fetch() for _ in range(16)]
            assert all(retried)
            second = [pool.submit(other.collection.fetch_item, claim) for claim in retried]
            assert retry_waiting.wait(15)
            assert counts(service)["documents"] == 0
            assert counts(service)["network_leases"] == 16
        finally:
            release.set()
        for future in [*first, *second]:
            future.result(timeout=15)
    assert active == 0 and peak == 16 and calls == 32
    assert counts(service)["items"] == {"stored": 16}
    assert counts(service)["network_leases"] == 0
    assert verify_originals(service, urls) == 16
    record(
        "cancel-retry-sixteen",
        peak_physical_connections=peak,
        download_attempts=calls,
        completed_originals=16,
        stale_publications=0,
        duplicate_originals=0,
        remaining_network_leases=0,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )


def test_redirect_host_cooldown_defers_without_consuming_failure_budget(tmp_path, monkeypatch):
    service, source = prepare(tmp_path / "data", monkeypatch, workers=8)
    blocked = [f"https://host-{number}.example/redirect.txt" for number in range(8)]
    healthy = "https://host-0.example/healthy.txt"
    enqueue(service, source, [*blocked, healthy])
    with service.db.transaction() as conn:
        conn.execute("UPDATE collection_items SET attempts=2 WHERE url<>?", (healthy,))
        conn.execute(
            "INSERT INTO collection_host_cooldowns VALUES(?,?)", ("limited.example", time.time() + 180)
        )
    attempted_targets = []

    def download(url, *, consume, connection_slot, **kwargs):
        if url in blocked:
            # The short test deadline exposes a cooldown wait without delaying
            # the test for the production 90-second absolute download budget.
            with service.collection.network_slot("limited.example", time.monotonic() + 0.15):
                attempted_targets.append("limited.example")
                raise AssertionError("A cooled redirect target received a request")
        with connection_slot(urlsplit(url).hostname):
            attempted_targets.append("healthy.example")
            return {
                "url": url,
                "filename": "healthy.txt",
                "mime_type": "text/plain",
                "blob": consume([original(url)]),
            }

    monkeypatch.setattr(ingestion, "_download", download)
    claims = [service.collection.claim_fetch() for _ in range(8)]
    assert all(claims) and {claim["url"] for claim in claims} == set(blocked)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(service.collection.fetch_item, claims))
    with closing(service.db.connect()) as conn:
        deferred = [
            dict(row) for row in conn.execute("SELECT * FROM collection_items WHERE url<>?", (healthy,))
        ]
    assert all(item["status"] == "queued" and item["attempts"] == 2 for item in deferred)
    assert all(item["available_at"] >= time.time() + 170 for item in deferred)
    next_claim = service.collection.claim_fetch()
    assert next_claim and next_claim["url"] == healthy
    service.collection.fetch_item(next_claim)
    assert attempted_targets == ["healthy.example"]
    assert counts(service)["items"] == {"queued": 8, "stored": 1}
    record(
        "redirect-cooldown-yields-capacity",
        deferred_items=8,
        preserved_failure_attempts=2,
        cooled_target_requests=0,
        healthy_originals_stored=1,
        elapsed_seconds=round(time.monotonic() - started, 3),
    )
