"""Real local child-process capacity, cleanup and durable scheduling up to 32 jobs."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Lock

import pytest

from app import archive_parsing, ingestion, parsing
from app.db import now
from app.service import Service
from test_archive_parsing import pdf_file


class _ObservedConnection:
    def __init__(self, connection, operation):
        self.connection, self.operation = connection, operation

    def send_bytes(self, payload):
        import resource

        Path(os.environ["TEA_TEST_PARSE_OBSERVATIONS"], f"{os.getpid()}.done").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "operation": self.operation,
                    "memory_limit": resource.getrlimit(resource.RLIMIT_AS),
                    "cpu_limit": resource.getrlimit(resource.RLIMIT_CPU),
                    "core_limit": resource.getrlimit(resource.RLIMIT_CORE),
                    "result_bytes": len(payload),
                }
            )
        )
        self.connection.send_bytes(payload)

    def close(self):
        self.connection.close()


def _observe(connection, operation, worker, args):
    directory = Path(os.environ["TEA_TEST_PARSE_OBSERVATIONS"])
    (directory / f"{os.getpid()}.ready").write_text(operation)
    deadline = time.monotonic() + 25
    while not (directory / "release").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("Test barrier did not release its own child.")
        time.sleep(0.01)
    worker(_ObservedConnection(connection, operation), *args)


def _observed_parse_worker(connection, *args):
    _observe(connection, "basic", parsing._parse_worker, args)


def _observed_archive_worker(connection, *args):
    _observe(connection, "archive-native", archive_parsing._archive_pdf_worker, args)


def _observed_archive_auxiliary(connection, operation, payload):
    _observe(
        connection,
        "archive-" + operation,
        archive_parsing._archive_auxiliary_worker,
        (operation, payload),
    )


def _exit_worker(connection, mode, marker):
    Path(marker).write_text(str(os.getpid()))
    if mode == "crash":
        connection.close()
        os._exit(7)
    if mode == "timeout":
        time.sleep(10)
    elif mode == "error":
        connection.send_bytes(json.dumps({"error": "Known parser failure"}).encode())
    else:
        connection.send_bytes(json.dumps({"result": {"pid": os.getpid()}}).encode())
    connection.close()


@pytest.fixture
def parser_slots(monkeypatch):
    monkeypatch.delenv("TEA_PARSE_WORKERS", raising=False)
    slots = parsing._ParserSlots()
    monkeypatch.setattr(parsing, "_SLOTS", slots)
    return slots


def test_default_capacity_remains_two_and_setup_can_precede_first_use(parser_slots, monkeypatch):
    assert parsing.parse_worker_limit() == 2
    monkeypatch.setenv("TEA_PARSE_WORKERS", "128")
    assert all(parser_slots.acquire(timeout=0) for _ in range(128))
    assert not parser_slots.acquire(timeout=0)
    for _ in range(128):
        parser_slots.release()


@pytest.mark.parametrize("limit", range(1, 129))
def test_explicit_parse_capacities_are_bounded(parser_slots, monkeypatch, limit):
    monkeypatch.setenv("TEA_PARSE_WORKERS", str(limit))
    assert all(parser_slots.acquire(timeout=0) for _ in range(limit))
    assert not parser_slots.acquire(timeout=0)
    for _ in range(limit):
        parser_slots.release()


@pytest.mark.parametrize("value", ["0", "129", "256", "-1", "bad", "2.5", ""])
def test_invalid_capacity_fails_before_any_child_starts(parser_slots, monkeypatch, value):
    monkeypatch.setenv("TEA_PARSE_WORKERS", value)
    monkeypatch.setattr(multiprocessing, "get_context", lambda *_: pytest.fail("child started"))
    with pytest.raises(ValueError, match="TEA_PARSE_WORKERS must be between 1 and 128"):
        parsing.parse_bounded("law.txt", b"Original text", "text/plain")


def test_active_capacity_cannot_be_replaced_and_acquired_slots_can_return(parser_slots, monkeypatch):
    assert parser_slots.acquire(timeout=0)
    monkeypatch.setenv("TEA_PARSE_WORKERS", "32")
    with pytest.raises(ValueError, match="restart this worker"):
        parser_slots.acquire(timeout=0)
    parser_slots.release()  # Releasing does not reread a changed configuration.
    monkeypatch.setenv("TEA_PARSE_WORKERS", "2")
    assert parser_slots.acquire(timeout=0) and parser_slots.acquire(timeout=0)
    assert not parser_slots.acquire(timeout=0)
    parser_slots.release()
    parser_slots.release()
    with pytest.raises(ValueError):
        parser_slots.release()


@pytest.mark.parametrize("capacity", [8, 16, 32])
def test_real_mixed_children_share_capacity_and_next_child_defers(
    parser_slots, tmp_path, monkeypatch, capacity
):
    observations = tmp_path / "observations"
    observations.mkdir()
    monkeypatch.setenv("TEA_TEST_PARSE_OBSERVATIONS", str(observations))
    originals = []
    for index in range(capacity):
        directory = tmp_path / str(index)
        directory.mkdir()
        originals.append(pdf_file(directory, [f"Visible source {index}.\nTea import condition."]))
    checkpoints = [
        archive_parsing.parse_archive_pdf_bounded(path, source_hash=digest, size=size, filename="law.pdf")
        for path, digest, size in originals[6:]
    ]
    # Replace only the test's fresh pool; production never replaces active slots.
    monkeypatch.setenv("TEA_PARSE_WORKERS", str(capacity))
    monkeypatch.setattr(parsing, "_SLOTS", parsing._ParserSlots())
    monkeypatch.setattr(parsing, "_parse_worker", _observed_parse_worker)
    monkeypatch.setattr(archive_parsing, "_archive_pdf_worker", _observed_archive_worker)
    monkeypatch.setattr(archive_parsing, "_archive_auxiliary_worker", _observed_archive_auxiliary)

    def invoke(index):
        path, digest, size = originals[index % capacity]
        if index < 2 or index == capacity:
            return parsing.parse_bounded("law.pdf", path.read_bytes(), "application/pdf")
        if index < 4:
            return archive_parsing.parse_archive_pdf_bounded(
                path, source_hash=digest, size=size, filename="law.pdf"
            )
        if index < 6:
            return archive_parsing.render_archive_page_bounded(
                path, source_hash=digest, size=size, page_number=1
            )
        return archive_parsing.archive_rebuild_bounded(checkpoints[index - 6])

    with ThreadPoolExecutor(max_workers=capacity + 1) as pool:
        first = [pool.submit(invoke, index) for index in range(capacity)]
        try:
            deadline = time.monotonic() + 10
            while len(list(observations.glob("*.ready"))) < capacity and time.monotonic() < deadline:
                time.sleep(0.01)
            ready = list(observations.glob("*.ready"))
            assert len(ready) == capacity
            assert len({item.stem for item in ready}) == capacity
            assert {item.read_text() for item in ready} == {
                "basic",
                "archive-native",
                "archive-render",
                "archive-rebuild",
            }
            next_child = pool.submit(invoke, capacity)
            with pytest.raises(parsing.ProcessingDeferred) as caught:
                next_child.result(timeout=6)
            assert caught.value.retryable and caught.value.retry_after == 5
            assert len(list(observations.glob("*.ready"))) == capacity
        finally:
            (observations / "release").touch()
        results = [future.result(timeout=12) for future in first]
    records = [json.loads(path.read_text()) for path in observations.glob("*.done")]
    assert len(records) == capacity
    assert all(record["memory_limit"] == [768 * 1024 * 1024] * 2 for record in records)
    assert all(record["cpu_limit"] == [35, 35] and record["core_limit"] == [0, 0] for record in records)
    assert all(record["result_bytes"] <= 40 * 1024 * 1024 for record in records)
    assert all(result["source_hash"] == originals[index][1] for index, result in enumerate(results))
    assert results[4]["image_sha256"] and results[5]["image_sha256"]
    own_pids = {record["pid"] for record in records}
    assert not own_pids.intersection(child.pid for child in multiprocessing.active_children())
    assert parsing._OCR_SLOTS.acquire(timeout=0) and parsing._OCR_SLOTS.acquire(timeout=0)
    try:
        assert not parsing._OCR_SLOTS.acquire(timeout=0)  # Vision capacity remains two.
    finally:
        parsing._OCR_SLOTS.release()
        parsing._OCR_SLOTS.release()
    assert ingestion._EMBEDDING_HTTP_SLOTS.acquire(timeout=0)
    assert ingestion._EMBEDDING_HTTP_SLOTS.acquire(timeout=0)
    try:
        assert not ingestion._EMBEDDING_HTTP_SLOTS.acquire(timeout=0)
    finally:
        ingestion._EMBEDDING_HTTP_SLOTS.release()
        ingestion._EMBEDDING_HTTP_SLOTS.release()


@pytest.mark.parametrize("mode", ["crash", "timeout", "error"])
@pytest.mark.parametrize("capacity", [1, 32])
def test_failed_children_are_reaped_before_capacity_is_reused(
    parser_slots, monkeypatch, tmp_path, mode, capacity
):
    monkeypatch.setenv("TEA_PARSE_WORKERS", str(capacity))
    assert all(parser_slots.acquire(timeout=0) for _ in range(capacity - 1))
    marker = tmp_path / "pid"
    expected = "进程中断" if mode == "crash" else "超时" if mode == "timeout" else "Known parser failure"
    with pytest.raises(ValueError, match=expected):
        parsing._run_bounded(_exit_worker, (mode, str(marker)), 1 if mode == "timeout" else 5)
    failed_pid = int(marker.read_text())
    assert failed_pid not in {child.pid for child in multiprocessing.active_children()}
    healthy = parsing._run_bounded(_exit_worker, ("healthy", str(marker)), 5)
    assert healthy["pid"] != failed_pid
    assert healthy["pid"] not in {child.pid for child in multiprocessing.active_children()}
    assert parser_slots.acquire(timeout=0)
    assert not parser_slots.acquire(timeout=0)
    for _ in range(capacity):
        parser_slots.release()


def test_concurrent_starts_and_cleanup_never_poll_the_same_child_together(parser_slots, monkeypatch):
    from multiprocessing.popen_fork import Popen

    monkeypatch.setenv("TEA_PARSE_WORKERS", "32")
    original_poll = Popen.poll
    counter_lock = Lock()
    polling = {}
    maximum = {}

    def observed_poll(process, *args, **kwargs):
        assert parsing._PROCESS_LIFECYCLE_LOCK.locked()
        with counter_lock:
            polling[process.pid] = polling.get(process.pid, 0) + 1
            maximum[process.pid] = max(maximum.get(process.pid, 0), polling[process.pid])
        try:
            time.sleep(0.003)  # Make start/_cleanup vs owner waitpid overlap observable.
            return original_poll(process, *args, **kwargs)
        finally:
            with counter_lock:
                polling[process.pid] -= 1

    monkeypatch.setattr(Popen, "poll", observed_poll)
    contents = [f"Immutable native text {index}.".encode() for index in range(32)]
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(lambda content: parsing.parse_bounded("law.txt", content), contents))
    assert len(maximum) == 32 and max(maximum.values()) == 1
    assert all(
        result["source_hash"] == hashlib.sha256(content).hexdigest()
        for result, content in zip(results, contents)
    )


def test_cleanup_failure_keeps_the_unconfirmed_child_slot(parser_slots, monkeypatch):
    monkeypatch.setenv("TEA_PARSE_WORKERS", "1")

    class Pipe:
        def close(self):
            pass

        def poll(self, timeout):
            return True

        def recv_bytes(self, limit):
            return b'{"result": {}}'

    class UnreapedProcess:
        pid = 42

        def start(self):
            pass

        def join(self, timeout):
            pass

        def is_alive(self):
            return True

        def terminate(self):
            pass

        def kill(self):
            pass

        def close(self):
            raise ValueError("Child termination could not be confirmed")

    class Context:
        def Pipe(self, **kwargs):
            return Pipe(), Pipe()

        def Process(self, **kwargs):
            return UnreapedProcess()

    monkeypatch.setattr(multiprocessing, "get_context", lambda *_: Context())
    with pytest.raises(ValueError, match="termination could not be confirmed"):
        parsing._run_bounded(_exit_worker, (), 1)
    assert not parser_slots.acquire(timeout=0)


def test_pipe_close_error_still_reaps_owned_child_before_returning_slot(parser_slots, monkeypatch):
    monkeypatch.setenv("TEA_PARSE_WORKERS", "1")
    events = []

    class Pipe:
        def __init__(self, name):
            self.name = name

        def close(self):
            events.append("close-" + self.name)
            if self.name == "receive":
                raise OSError("Owned receive pipe close failed")

        def poll(self, timeout):
            return True

        def recv_bytes(self, limit):
            return b'{"result": {}}'

    class OwnedProcess:
        pid = 42

        def start(self):
            events.append("start")

        def join(self, timeout):
            events.append("join")

        def is_alive(self):
            return False

        def close(self):
            assert not parser_slots.acquire(timeout=0)
            events.append("close-child")

    class Context:
        def Pipe(self, **kwargs):
            return Pipe("receive"), Pipe("send")

        def Process(self, **kwargs):
            return OwnedProcess()

    monkeypatch.setattr(multiprocessing, "get_context", lambda *_: Context())
    with pytest.raises(OSError, match="Owned receive pipe close failed"):
        parsing._run_bounded(_exit_worker, (), 1)
    assert events[-3:] == ["close-send", "join", "close-child"]
    assert parser_slots.acquire(timeout=0)
    parser_slots.release()


def _store_text(service, index, *, shared=None, level="primary"):
    content = f"Tea import original {index}.".encode() if shared is None else shared
    blob = service.archive.blobs.put_bytes("owner", content)
    with service.db.transaction() as conn:
        return dict(
            service.archive.store_original(
                conn,
                "owner",
                blob,
                "law.txt",
                "text/plain",
                f"https://official.invalid/{index}.txt"
                if shared is None
                else "https://official.invalid/shared.txt",
                collection="country:ID",
                level=level,
                tags=["country:ID"],
                identity_key=str(index),
            )
        )


@pytest.fixture(params=[8, 16, 32])
def concurrent_job_service(tmp_path, monkeypatch, request):
    monkeypatch.setenv("TEA_PROCESS_WORKERS", str(request.param))
    service = Service(tmp_path / "owned")
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("owner", "owner", None, None, 1, now()))
    return service


@pytest.mark.parametrize("first_phase", ["parse", "embed"])
def test_job_leases_remain_shared_and_phase_fair(concurrent_job_service, first_phase):
    service = concurrent_job_service
    capacity = service.collection.processing_workers
    docs = [_store_text(service, index) for index in range(capacity + 4)]
    second_phase = "embed" if first_phase == "parse" else "parse"
    with service.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET phase=?,updated_at='2000'", (first_phase,))
        conn.execute(
            "UPDATE processing_jobs SET phase=?,updated_at='2100' WHERE document_id=?",
            (second_phase, docs[-1]["id"]),
        )
    first = service.collection.claim_processing(owner_id="owner", document_ids=[docs[0]["id"]])
    assert first["phase"] == first_phase
    collections = [Service(service.db.path.parent).collection for _ in range(3)]
    assert collections[0].claim_processing()["phase"] == second_phase
    with ThreadPoolExecutor(max_workers=capacity + 4) as pool:
        picked = list(pool.map(lambda index: collections[index % 3].claim_processing(), range(capacity + 4)))
    # Embedding has two provider slots. Waiting embed jobs keep their retry
    # budgets, while parse work may use the remaining processing capacity.
    expected_active = capacity if first_phase == "parse" else 3
    assert sum(claim is not None for claim in picked) == expected_active - 2
    assert all(collection.claim_processing() is None for collection in collections)
    with closing(service.db.connect()) as conn:
        assert (
            conn.execute("SELECT count(*) FROM processing_jobs WHERE status='processing'").fetchone()[0]
            == expected_active
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM processing_jobs WHERE status='queued' AND attempts!=0"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM processing_jobs WHERE status='processing' AND phase='embed'"
            ).fetchone()[0]
            <= 2
        )


def test_jobs_keep_same_source_serial_and_expired_lease_recoverable(concurrent_job_service):
    service = concurrent_job_service
    capacity = service.collection.processing_workers
    shared = b"The identical immutable source original."
    a, b = [_store_text(service, index, shared=shared) for index in range(2)]
    other = [_store_text(service, index) for index in range(2, capacity + 2)]
    picked = service.collection.claim_processing(owner_id="owner", document_ids=[a["id"]])
    assert picked["document_id"] == a["id"]
    with ThreadPoolExecutor(max_workers=capacity + 2) as pool:
        claims = list(pool.map(lambda _: service.collection.claim_processing(), range(capacity + 2)))
    active = [claim for claim in claims if claim]
    assert len(active) == capacity - 1 and b["id"] not in {claim["document_id"] for claim in active}
    assert all(claim["document_id"] in {doc["id"] for doc in other} for claim in active)
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE processing_jobs SET lease_until=? WHERE document_id=?", (time.time() - 1, a["id"])
        )
    assert (
        service.collection.claim_processing(owner_id="owner", document_ids=[b["id"]])["document_id"]
        == b["id"]
    )
    with closing(service.db.connect()) as conn:
        assert not service.collection._valid_claim(conn, picked, True)


def test_job_configuration_keeps_independent_secondary_zero_vectors(concurrent_job_service, parser_slots):
    service = concurrent_job_service
    doc = _store_text(service, 1, level="secondary")
    claim = service.collection.claim_processing()
    assert claim and service.collection.processing_workers in {8, 16, 32}
    service.collection.process_item(claim)
    with closing(service.db.connect()) as conn:
        assert not conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0]
        assert not service.archive.can_index(conn, doc["id"])
        row = conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()
        assert json.loads(row["manifest"])["chunks"]
        assert row["content_hash"] == hashlib.sha256(b"Tea import original 1.").hexdigest()


@pytest.mark.parametrize("value", ["0", "129", "256", "-1"])
def test_invalid_process_capacity_is_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TEA_PROCESS_WORKERS", value)
    with pytest.raises(ValueError, match="TEA_PROCESS_WORKERS must be between 1 and 128"):
        Service(tmp_path / "owned")


def test_default_process_capacity_remains_two(tmp_path, monkeypatch):
    monkeypatch.delenv("TEA_PROCESS_WORKERS", raising=False)
    assert Service(tmp_path / "owned").collection.processing_workers == 2


def test_processing_capacity_128_keeps_embedding_limit_independent(tmp_path, monkeypatch):
    monkeypatch.setenv("TEA_PROCESS_WORKERS", "128")
    monkeypatch.setenv("TEA_PARSE_WORKERS", "128")
    monkeypatch.setenv("TEA_EMBED_WORKERS", "32")
    collection = Service(tmp_path / "owned").collection
    assert collection.processing_workers == 128
    assert collection.embedding_workers == 32


def test_processing_32_with_parse_two_defers_without_consuming_attempts(tmp_path, monkeypatch, parser_slots):
    monkeypatch.setenv("TEA_PROCESS_WORKERS", "32")
    monkeypatch.setenv("TEA_PARSE_WORKERS", "2")
    service = Service(tmp_path / "owned")
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("owner", "owner", None, None, 1, now()))
    doc = _store_text(service, 1)
    claim = service.collection.claim_processing()
    assert claim and service.collection.processing_workers == 32
    deferred_at = None
    original_failure = service.collection._failure

    def record_failure(owned_claim, error, *, processing):
        nonlocal deferred_at
        deferred_at = time.time()
        return original_failure(owned_claim, error, processing=processing)

    monkeypatch.setattr(service.collection, "_failure", record_failure)
    assert parser_slots.acquire(timeout=0) and parser_slots.acquire(timeout=0)
    try:
        service.collection.process_item(claim)
    finally:
        parser_slots.release()
        parser_slots.release()
    assert deferred_at is not None
    with closing(service.db.connect()) as conn:
        job = conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (doc["id"],)).fetchone()
        assert job["status"] == "queued" and job["attempts"] == 0
        assert job["lease_token"] is None and job["lease_until"] is None
        assert job["available_at"] >= deferred_at + 4.5
        current = conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()
        assert current["content_hash"] == doc["content_hash"] and current["version"] == doc["version"]
        assert Path(current["storage_path"]).read_bytes() == b"Tea import original 1."
        assert not conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0]
