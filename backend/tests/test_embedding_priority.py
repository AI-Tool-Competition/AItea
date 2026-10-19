"""One host-wide admission budget preserves interactive and long-document work."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
import sqlite3
import threading
import time

import httpx
import pytest

from app import ingestion
from app.embedding_admission import SharedEmbeddingBudget, SharedEmbeddingCooldown, model_scope
from test_embedding_batches import provider, response
from test_retrieval_index import body, seed, search
from test_retrieval_index import workspace as _workspace_fixture

workspace = _workspace_fixture


def wait_for(predicate, timeout=15):
    until = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= until:
            pytest.fail("Timed out waiting for bounded admission evidence")
        time.sleep(0.02)


def configure(path, base="https://embed.example/v1", limit=64):
    os.environ.update(
        EMBEDDING_BASE_URL=base,
        EMBEDDING_MODEL="test-model",
        EMBEDDING_API_KEY="not-a-real-key",
        EMBEDDING_HTTP_TIMEOUT_SECONDS="60",
        EMBEDDING_HTTP_WORKERS=str(limit),
        EMBEDDING_BATCH_SIZE="1",
        EMBEDDING_REQUEST_CONCURRENCY="1",
        EMBEDDING_ADMISSION_PATH=str(path),
        EMBEDDING_RETRY_AMBIGUOUS_TIMEOUTS="false",
    )
    ingestion._EMBEDDING_HTTP_SLOTS = ingestion._EmbeddingSlots()
    ingestion._EMBEDDING_PROFILES = {}


def http_process(path, base, lane, count, results):
    configure(path, base)
    # Prepare real clients before the barrier; simultaneous CA-store loading is
    # unrelated to HTTP admission and can dominate this local concurrency test.
    original = httpx.Client
    clients = [original(trust_env=False, follow_redirects=False) for _ in range(count)]
    available = list(clients)
    httpx.Client = lambda **_: available.pop()

    def execute(index):
        text = f"{lane}:{index}" + ("x" * 15000 if lane == "bulk" else "")
        with ingestion.embedding_priority(lane):
            return ingestion.embed_texts([text])

    try:
        with ThreadPoolExecutor(max_workers=count) as pool:
            values = list(pool.map(execute, range(count)))
        results.put((lane, len(values), None))
    except Exception as error:
        results.put((lane, 0, type(error).__name__))
    finally:
        httpx.Client = original
        for client in clients:
            client.close()


def initialize_process(path, gate, results):
    gate.wait(10)
    try:
        budget = SharedEmbeddingBudget(path, "simultaneous", 64)
        results.put(budget.snapshot()["limit"])
    except Exception as error:
        results.put(type(error).__name__)


def abandoned_process(path, lane, ready):
    budget = SharedEmbeddingBudget(path, "abandoned", 1)
    assert budget.try_acquire("active", lane)
    assert not budget.try_acquire("waiting", "bulk" if lane == "query" else "query")
    ready.set()
    threading.Event().wait(30)


def forked_process(results):
    slots = ingestion._EMBEDDING_HTTP_SLOTS
    results.put((slots._shared().pid, slots.acquire(blocking=False)))


def test_independent_http_processes_share_64_and_queries_get_reserved_slots(tmp_path):
    context = multiprocessing.get_context("spawn")
    gate, lock = threading.Event(), threading.Lock()
    state = {"active": 0, "peak": 0, "bulk": 0, "query": 0, "texts": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            text = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["input"][0]
            lane = text.split(":", 1)[0]
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                state[lane] += 1
                state["texts"].append(text)
            gate.wait(30)
            payload = json.dumps({"data": [{"index": 0, "embedding": [1.0, 0.0]}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            with lock:
                state["active"] -= 1

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        request_queue_size = 128

    server = Server(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    base, path = f"http://127.0.0.1:{server.server_port}/v1", tmp_path / "runtime.sqlite3"
    results = context.Queue()
    bulk = context.Process(target=http_process, args=(path, base, "bulk", 64, results))
    query = context.Process(target=http_process, args=(path, base, "query", 4, results))
    processes = [bulk, query]
    try:
        bulk.start()
        wait_for(lambda: state["bulk"] == 60, timeout=25)
        budget = SharedEmbeddingBudget(path, model_scope(base, "test-model"), 64)
        assert budget.snapshot()["active"] == {"bulk": 60, "query": 0}
        query.start()
        wait_for(lambda: state["query"] == 4)
        assert state["active"] == state["peak"] == 64
        assert budget.snapshot()["waiting"]["bulk"] == 4
        gate.set()
        for child in processes:
            child.join(20)
            assert child.exitcode == 0
        assert sorted(results.get(timeout=2) for _ in processes) == [("bulk", 64, None), ("query", 4, None)]
        assert state["peak"] == 64 and state["active"] == 0
        assert len(state["texts"]) == len(set(state["texts"])) == 68
        assert all(len(text) > 15000 for text in state["texts"] if text.startswith("bulk:"))
        assert budget.snapshot()["active"] == budget.snapshot()["waiting"] == {"query": 0, "bulk": 0}
    finally:
        gate.set()
        for child in processes:
            if child.pid and child.is_alive():
                child.terminate()
                child.join(5)
        server.shutdown()
        server.server_close()


def test_first_api_and_runner_initialization_is_atomic(tmp_path):
    context = multiprocessing.get_context("spawn")
    gate, results = context.Event(), context.Queue()
    path = tmp_path / "simultaneous.sqlite3"
    children = [context.Process(target=initialize_process, args=(path, gate, results)) for _ in range(4)]
    try:
        for child in children:
            child.start()
        gate.set()
        for child in children:
            child.join(15)
            assert child.exitcode == 0
        assert [results.get(timeout=2) for _ in children] == [64] * 4
        assert not list(tmp_path.glob(".embedding-admission-*"))
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(5)


@pytest.mark.parametrize("lane", ["query", "bulk"])
def test_dead_process_reclaims_active_and_waiting_roles(tmp_path, lane):
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    path = tmp_path / "abandoned.sqlite3"
    child = context.Process(target=abandoned_process, args=(path, lane, ready))
    try:
        child.start()
        assert ready.wait(10)
        budget = SharedEmbeddingBudget(path, "abandoned", 1)
        assert sum(budget.snapshot()["active"].values()) == sum(budget.snapshot()["waiting"].values()) == 1
        child.terminate()
        child.join(5)
        assert budget.snapshot()["reclaimed"] == 2
        assert budget.try_acquire("replacement", "bulk")
        budget.release("replacement")
    finally:
        if child.is_alive():
            child.terminate()
            child.join(5)


def test_fork_does_not_reuse_parent_process_lease_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    configure(tmp_path / "fork.sqlite3", limit=1)
    slots = ingestion._EMBEDDING_HTTP_SLOTS
    assert slots.acquire(blocking=False)
    context = multiprocessing.get_context("fork")
    results = context.Queue()
    child = context.Process(target=forked_process, args=(results,))
    child.start()
    child.join(5)
    try:
        assert child.exitcode == 0
        assert results.get(timeout=2) == (child.pid, False)
        assert slots.snapshot()["active"]["bulk"] == 1
    finally:
        slots.release()


@pytest.mark.parametrize("kind", ["business", "empty", "plain", "symlink", "hardlink", "directory"])
def test_wrong_database_path_is_rejected_without_modifying_original(tmp_path, kind):
    source = tmp_path / "tea.sqlite3"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("CREATE TABLE documents(id TEXT)")
        conn.commit()
    source.chmod(0o640)
    path = source
    if kind == "empty":
        path = tmp_path / "empty.sqlite3"
        path.touch()
    elif kind == "plain":
        path = tmp_path / "plain"
        path.write_text("not a database")
    elif kind == "symlink":
        path = tmp_path / "alias.sqlite3"
        path.symlink_to(source)
    elif kind == "hardlink":
        path = tmp_path / "hard.sqlite3"
        os.link(source, path)
    elif kind == "directory":
        path = tmp_path / "directory"
        path.mkdir()
    before = hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mode
    with pytest.raises(ValueError):
        SharedEmbeddingBudget(path, "model", 64)
    assert before == (hashlib.sha256(source.read_bytes()).hexdigest(), source.stat().st_mode)
    with closing(sqlite3.connect(source)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [
            ("documents",)
        ]


def test_query_priority_and_bulk_floor_with_both_lanes_waiting(tmp_path):
    budget = SharedEmbeddingBudget(tmp_path / "fair.sqlite3", "model", 64)
    for index in range(16):
        assert budget.try_acquire(f"bulk-{index}", "bulk")
    for index in range(48):
        assert budget.try_acquire(f"query-{index}", "query")
    assert not budget.try_acquire("new-bulk", "bulk")
    assert not budget.try_acquire("new-query", "query")
    budget.release("query-0")
    assert not budget.try_acquire("new-bulk", "bulk")
    assert budget.try_acquire("new-query", "query")
    assert not budget.try_acquire("newer-query", "query")
    budget.release("bulk-0")
    assert not budget.try_acquire("newer-query", "query")
    assert budget.try_acquire("new-bulk", "bulk")
    assert budget.snapshot()["active"] == {"query": 48, "bulk": 16}


def test_real_rag_query_uses_priority_and_restores_default_lane(workspace, monkeypatch):
    manifest = body("permit original")
    manifest["embedding_fingerprint"] = "model"
    manifest["chunks"][0]["embedding"] = [1.0, 0.0]
    with workspace.db.transaction() as conn:
        seed(conn, "priority-query", manifest)
    monkeypatch.setattr(workspace, "embedding_fingerprint", lambda: "model")
    captured = []

    def embed(texts):
        captured.append((texts, ingestion._EMBEDDING_PRIORITY.get()))
        return [[1.0, 0.0]]

    monkeypatch.setattr(ingestion, "embed_texts", embed)
    result = search(workspace, "permit")
    assert result["mode"] == "hybrid"
    assert captured == [(["permit"], "query")]
    assert ingestion._EMBEDDING_PRIORITY.get() == "bulk"


def test_live_capacity_change_is_rejected_until_both_processes_drain(tmp_path):
    path = tmp_path / "capacity.sqlite3"
    original = SharedEmbeddingBudget(path, "model", 32)
    assert original.try_acquire("active", "bulk")
    with pytest.raises(ValueError, match="drain"):
        SharedEmbeddingBudget(path, "model", 64)
    original.release("active")
    upgraded = SharedEmbeddingBudget(path, "model", 64)
    assert upgraded.snapshot()["limit"] == 64
    with pytest.raises(ValueError, match="restart"):
        original.try_acquire("old-client", "bulk")


def test_cancelled_waiter_does_not_block_next_bulk_job(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    configure(tmp_path / "cancel.sqlite3", limit=1)
    slots = ingestion._EMBEDDING_HTTP_SLOTS
    assert slots.acquire(blocking=False)
    with pytest.raises(RuntimeError, match="cancel"):
        with ingestion._embedding_wait("query"):
            assert not slots.acquire(timeout=0.01)
            assert slots.snapshot()["waiting"] == {"query": 1, "bulk": 0}
            raise RuntimeError("cancel")
    assert slots.snapshot()["waiting"] == {"query": 0, "bulk": 0}
    slots.release()
    assert slots.acquire(blocking=False)
    slots.release()


def test_shared_provider_cooldown_and_model_boundary(tmp_path):
    path = tmp_path / "cooldown.sqlite3"
    first = SharedEmbeddingBudget(path, "same-model", 64)
    second = SharedEmbeddingBudget(path, "same-model", 64)
    first.cooldown(10)
    with pytest.raises(SharedEmbeddingCooldown) as error:
        second.try_acquire("delayed", "query")
    assert 0 < error.value.retry_after <= 10
    assert second.snapshot()["waiting"] == {"query": 0, "bulk": 0}
    other = SharedEmbeddingBudget(path, "other-model", 64)
    assert other.try_acquire("different", "query")
    other.release("different")


@pytest.mark.parametrize("failure", [429, 504, "read", "connect"])
def test_provider_failure_preserves_evidence_and_releases_all_slots(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    configure(tmp_path / "failure.sqlite3")

    def handler(request):
        if failure == "read":
            raise httpx.ReadTimeout("private detail", request=request)
        if failure == "connect":
            raise httpx.ConnectError("private detail", request=request)
        return httpx.Response(failure, headers={"Retry-After": "1"} if failure == 429 else {})

    provider(monkeypatch, handler)
    with pytest.raises(ingestion.EmbeddingServiceError) as error:
        ingestion.embed_texts(["1"])
    assert error.value.http_status == (failure if isinstance(failure, int) else None)
    assert error.value.request_outcome_unknown is (failure in {504, "read"})
    assert error.value.request_sent is (failure != "connect")
    assert "private" not in str(error.value)
    snapshot = ingestion._EMBEDDING_HTTP_SLOTS.snapshot()
    assert snapshot["active"] == snapshot["waiting"] == {"query": 0, "bulk": 0}


def test_paid_success_survives_local_release_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ.copy())
    configure(tmp_path / "paid.sqlite3")
    budget = ingestion._EMBEDDING_HTTP_SLOTS._shared()
    original = budget.transaction

    def unavailable():
        raise sqlite3.OperationalError("database is locked")

    def paid(request):
        monkeypatch.setattr(budget, "transaction", unavailable)
        return response(json.loads(request.content)["input"])

    provider(monkeypatch, paid)
    saved = []
    assert ingestion.embed_texts(["1"], on_batch=lambda texts, values: saved.extend(values)) == [[1.0, 1.0]]
    assert saved == [[1.0, 1.0]]
    monkeypatch.setattr(budget, "transaction", original)
    assert budget.snapshot()["active"]["bulk"] == 1  # Conservative lease, never over-admit.
