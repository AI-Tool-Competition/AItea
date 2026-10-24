"""Same-file writers queue locally; SQLite still arbitrates other processes."""

import multiprocessing
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier, Event

import pytest
from app.bootstrap import Service
from app.shared.domain.values import now
from app.shared.infrastructure import db as module
from app.shared.infrastructure.db import Database


def quick_connections(database, monkeypatch):
    real = database.connect

    def connect():
        conn = real()
        conn.execute("PRAGMA busy_timeout=40")
        return conn

    monkeypatch.setattr(database, "connect", connect)


def wait_for(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Writer did not reach the expected queue boundary")
        time.sleep(0.005)


def queued(gate):
    with gate._condition:
        return len(gate._queue)


@pytest.fixture
def database(tmp_path):
    value = Database(tmp_path / "target")
    with value.transaction() as conn:
        conn.execute("CREATE TABLE probe(value INTEGER)")
    return value


def values(database):
    with closing(database.connect()) as conn:
        return [row[0] for row in conn.execute("SELECT value FROM probe ORDER BY rowid")]


def test_same_path_instances_commit_in_waiting_order(database, monkeypatch):
    other = Database(database.path.parent / ".." / database.path.parent.name)
    for item in (database, other):
        quick_connections(item, monkeypatch)
    gate = module._writer_gate(database.path)
    assert gate is module._writer_gate(other.path)

    def write(number):
        with (database if number % 2 else other).transaction() as conn:
            conn.execute("INSERT INTO probe VALUES(?)", (number,))

    with ThreadPoolExecutor(max_workers=16) as pool:
        with database.transaction():
            futures = []
            for number in range(16):
                futures.append(pool.submit(write, number))
                wait_for(lambda: queued(gate) == number + 1)
            # Far beyond the test connection's busy deadline: local waiters
            # must not race BEGIN IMMEDIATE before their turn.
            time.sleep(0.1)
        for future in futures:
            future.result(timeout=5)
    assert values(database) == list(range(16))


def test_same_service_32_failure_writes_release_every_lease(tmp_path, monkeypatch):
    service = Service(tmp_path / "service")
    checkpoint = (
        '{"pages":[{"page":1,"status":"pending_ocr","ocr":{"attempts":2}}],"elements":[],"chunks":[]}'
    )
    claims = []
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("owner", "Test", None, None, 1, now()))
        for number in range(32):
            ident, token = "document-" + str(number), "lease-" + str(number)
            conn.execute(
                """INSERT INTO documents(id,owner_id,title,filename,role,status,mime_type,size,version,
                   content_hash,storage_path,tags,manifest,created_at,family_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ident,
                    "owner",
                    ident,
                    "source.txt",
                    "primary",
                    "processing",
                    "text/plain",
                    1,
                    1,
                    "original-" + str(number),
                    "unused-original",
                    "[]",
                    checkpoint,
                    now(),
                    ident,
                ),
            )
            conn.execute(
                """INSERT INTO processing_jobs(document_id,owner_id,phase,status,attempts,lease_token,lease_until,updated_at)
                   VALUES(?,?,'parse','processing',1,?,?,?)""",
                (ident, "owner", token, time.time() + 120, now()),
            )
            claims.append(
                {
                    "document_id": ident,
                    "owner_id": "owner",
                    "phase": "parse",
                    "attempts": 1,
                    "lease_token": token,
                }
            )
    quick_connections(service.db, monkeypatch)
    gate = module._writer_gate(service.db.path)
    barrier = Barrier(33)

    def release(claim):
        barrier.wait(timeout=5)
        service.collection._failure(claim, RuntimeError("temporary failure"), processing=True)

    with ThreadPoolExecutor(max_workers=32) as pool:
        with service.db.transaction():
            futures = [pool.submit(release, claim) for claim in claims]
            barrier.wait(timeout=5)
            wait_for(lambda: queued(gate) == 32)
            time.sleep(0.1)
        for future in futures:
            future.result(timeout=5)
    with closing(service.db.connect()) as conn:
        jobs = conn.execute("SELECT * FROM processing_jobs").fetchall()
        assert len(jobs) == 32
        assert all(row["status"] == "queued" and row["attempts"] == 1 for row in jobs)
        assert all(row["lease_token"] is row["lease_until"] is None for row in jobs)
        assert {row[0] for row in conn.execute("SELECT manifest FROM documents")} == {checkpoint}


@pytest.mark.parametrize("failure", ["body", "connect", "commit"])
def test_failed_transaction_releases_queue_and_rolls_back(database, monkeypatch, failure):
    real = database.connect

    class FailedCommit:
        def __init__(self, conn):
            self.conn = conn

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def commit(self):
            raise RuntimeError("injected commit failure")

    def connect():
        if failure == "connect":
            raise RuntimeError("injected connection failure")
        return FailedCommit(real()) if failure == "commit" else real()

    monkeypatch.setattr(database, "connect", connect)
    with pytest.raises(RuntimeError, match="injected"):
        with database.transaction() as conn:
            conn.execute("INSERT INTO probe VALUES(1)")
            if failure == "body":
                raise RuntimeError("injected body failure")
    monkeypatch.setattr(database, "connect", real)
    with ThreadPoolExecutor(max_workers=1) as pool:

        def write():
            with database.transaction() as conn:
                conn.execute("INSERT INTO probe VALUES(2)")

        pool.submit(write).result(timeout=3)
    assert values(database) == [2]


def test_same_thread_nested_same_database_is_rejected_without_partial_commit(database):
    other = Database(database.path.parent)
    with database.transaction() as conn:
        conn.execute("INSERT INTO probe VALUES(1)")
        with pytest.raises(RuntimeError, match="existing connection"):
            with other.transaction():
                pytest.fail("Nested connection acquired its own transaction")
        conn.execute("INSERT INTO probe VALUES(2)")
    assert values(database) == [1, 2]


def test_other_database_and_reads_are_not_blocked(database, tmp_path):
    other = Database(tmp_path / "independent")
    event = Event()

    def write():
        with other.transaction() as conn:
            conn.execute("CREATE TABLE independent(value INTEGER)")
            conn.execute("INSERT INTO independent VALUES(7)")
        # A read connection to the busy database also stays outside the queue.
        assert values(database) == []
        event.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with database.transaction() as conn:
            conn.execute("INSERT INTO probe VALUES(1)")
            future = pool.submit(write)
            assert event.wait(timeout=3)
            future.result(timeout=3)
    assert values(database) == [1]


def hold_external_writer(path, pipe):
    with closing(sqlite3.connect(path, isolation_level=None)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        pipe.send("locked")
        if not pipe.poll(10):
            raise TimeoutError("Parent did not release the external writer")
        assert pipe.recv() == "release"
        conn.rollback()
    pipe.close()


def test_other_process_busy_is_real_failure_and_local_queue_recovers(database, monkeypatch):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=hold_external_writer, args=(str(database.path), child))
    process.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv() == "locked"
        quick_connections(database, monkeypatch)
        with pytest.raises(sqlite3.OperationalError) as error:
            with database.transaction():
                pytest.fail("External SQLite writer was silently ignored")
        assert error.value.sqlite_errorcode & 0xFF == sqlite3.SQLITE_BUSY
        parent.send("release")
        process.join(timeout=5)
        assert process.exitcode == 0
        with database.transaction() as conn:
            conn.execute("INSERT INTO probe VALUES(9)")
        assert values(database) == [9]
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)


def inherited_database_write(database, pipe):
    try:
        with database.transaction() as conn:
            conn.execute("INSERT INTO probe VALUES(8)")
        pipe.send("written")
    finally:
        pipe.close()


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="No fork available")
def test_fork_child_does_not_inherit_local_writer_queue(database):
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    gate = module._writer_gate(database.path)
    with gate.hold():
        process = context.Process(target=inherited_database_write, args=(database, child))
        process.start()
        child.close()
        try:
            assert parent.poll(5) and parent.recv() == "written"
            process.join(timeout=5)
            assert process.exitcode == 0
        finally:
            parent.close()
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
    assert values(database) == [8]
