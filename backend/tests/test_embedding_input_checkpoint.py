"""Successful provider batches survive sibling failures, restart and overlap."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import runpy
import threading
import time

import pytest

from app import embedding_reuse, ingestion
from app.collection import CollectionService
from test_embedding_batches import provider, response
from test_embedding_reuse import register, seed_vectors, process, stored, vectors, FP
from test_embedding_reuse import service as _service_fixture

service = _service_fixture


def checkpoint(tmp_path, **scope):
    return embedding_reuse.InputCheckpointCache(
        tmp_path / "successful.sqlite3",
        {"owner": "one", "fingerprint": "model", "source_plan": "fixed", **scope},
    )


def test_successful_partial_batch_is_reused_after_failure_and_restart(tmp_path):
    cache = checkpoint(tmp_path)

    def fails(texts, *, on_batch):
        assert texts == ["a", "b"]
        on_batch(["a"], [[3.0, 4.0]])
        raise ValueError("second HTTP failed")

    with pytest.raises(ValueError, match="second HTTP"):
        cache.embed(["a", "b"], fails)
    restarted = checkpoint(tmp_path)

    def succeeds(texts, *, on_batch):
        assert texts == ["b"]
        on_batch(texts, [[0.0, 1.0]])
        return [[0.0, 1.0]]

    result = restarted.embed(["a", "b", "a"], succeeds)
    assert result[0] == result[2] and result[1] == [0.0, 1.0]
    assert restarted.snapshot()["saved_inputs"] == 2
    assert restarted.embed(["a"], lambda *_: pytest.fail("paid twice")) == [result[0]]


def test_overlap_has_single_cold_payment_per_input(tmp_path):
    cache = checkpoint(tmp_path)
    calls = []
    entered = threading.Event()

    def slow(texts, *, on_batch):
        calls.append(texts)
        entered.set()
        time.sleep(0.05)
        values = [[1.0, 0.0] for _ in texts]
        on_batch(texts, values)
        return values

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.embed, ["a", "b"], slow)
        assert entered.wait(1)
        second = pool.submit(cache.embed, ["b", "c"], slow)
        assert first.result() == second.result() == [[1.0, 0.0], [1.0, 0.0]]
    assert calls == [["a", "b"], ["c"]]
    assert cache.snapshot()["inflight_inputs"] == 0


@pytest.mark.parametrize("scope", [{"owner": "two"}, {"fingerprint": "other"}, {"source_plan": "changed"}])
def test_checkpoints_never_cross_owner_model_or_source_plan(tmp_path, scope):
    checkpoint(tmp_path)
    with pytest.raises(ValueError, match="不一致"):
        checkpoint(tmp_path, **scope)


def test_invalid_receipt_never_becomes_a_cache_hit(tmp_path):
    cache = checkpoint(tmp_path)
    with pytest.raises(ValueError):
        cache.save(["a"], [[float("nan"), 1.0]])
    assert cache.snapshot()["saved_inputs"] == 0


@pytest.mark.parametrize("mutation", ["metadata", "remove_classification"])
def test_runner_metadata_guard_allows_append_but_rejects_old_row_changes(service, mutation):
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "complete_embeddings.py"))
    original = register(service, "original")
    guard = {"rows": runner["metadata_rows"](service)}
    register(service, "newly-imported")
    assert runner["existing_metadata_intact"](service, guard)
    with service.db.transaction() as conn:
        if mutation == "metadata":
            conn.execute(
                "UPDATE document_metadata SET revision=revision+1 WHERE document_id=?", (original["id"],)
            )
        else:
            conn.execute("DELETE FROM legal_classifications WHERE document_id=?", (original["id"],))
    assert not runner["existing_metadata_intact"](service, guard)


def test_same_input_keeps_first_valid_vector_and_repeated_rows_pay_once(tmp_path):
    cache = checkpoint(tmp_path)
    calls = []

    def provider(texts, *, on_batch):
        calls.append(texts)
        assert texts == ["same"]
        on_batch(texts, [[1.0, 0.0]])
        # A repeated receipt cannot overwrite already committed canonical bytes.
        on_batch(texts, [[0.0, 1.0]])
        return [[0.0, 1.0]]

    assert cache.embed(["same", "same"], provider) == [[1.0, 0.0], [1.0, 0.0]]
    assert calls == [["same"]]
    assert checkpoint(tmp_path).embed(["same"], lambda *_: pytest.fail("paid twice")) == [[1.0, 0.0]]
    with pytest.raises(ValueError, match="维度"):
        cache.save(["same"], [[1.0, 0.0, 0.0]])


def test_invalid_sibling_does_not_commit_any_receipt(tmp_path):
    cache = checkpoint(tmp_path)
    with pytest.raises(ValueError):
        cache.save(["good", "invalid"], [[1.0, 0.0], [float("nan"), 1.0]])
    assert cache.snapshot()["saved_inputs"] == 0


def test_opt_in_reuses_exact_input_between_distinct_originals(service, monkeypatch):
    monkeypatch.setenv("EMBEDDING_INPUT_CACHE", "true")
    donor = register(service, "donor", url="https://one.invalid/a", content=b"one")
    target = register(service, "target", country="SG", url="https://two.invalid/b", content=b"two")
    seed_vectors(service, donor)
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET status='processing' WHERE id=?", (target["id"],))
    process(service, target)
    proof = stored(service, target)["manifest"]["embedding_reuse"]
    assert len(vectors(service, target)) == 2
    assert proof["protocol"] == embedding_reuse.INPUT_PROTOCOL
    assert proof["last_batch"]["provider_input_chunks"] == 0
    assert all(r["match_policy"] == "exact_input" for r in proof["records"].values())


def test_input_cache_still_excludes_other_owner(service, monkeypatch):
    monkeypatch.setenv("EMBEDDING_INPUT_CACHE", "true")
    donor = register(service, "donor", owner="other")
    target = register(service, "target")
    seed_vectors(service, donor)
    with service.db.transaction() as conn:
        batch = service.index.pending(conn, target["id"], FP)
        plan = embedding_reuse.prepare_batch(service, conn, target, FP, batch)
    assert not plan["hits"]


def test_input_cache_still_excludes_other_model(service, monkeypatch):
    monkeypatch.setenv("EMBEDDING_INPUT_CACHE", "true")
    donor = register(service, "donor")
    target = register(service, "target")
    seed_vectors(service, donor, fingerprint="previous-model")
    with service.db.transaction() as conn:
        batch = service.index.pending(conn, target["id"], FP)
        plan = embedding_reuse.prepare_batch(service, conn, target, FP, batch)
    assert not plan["hits"]


@pytest.mark.parametrize("http_workers", [4, 8, 16, 32, 64])
def test_real_parallel_callers_share_configured_slots(monkeypatch, http_workers):
    for k, v in {
        "BASE_URL": "https://embed.example/v1",
        "MODEL": "embed",
        "API_KEY": "test",
        "HTTP_WORKERS": str(http_workers),
        "BATCH_SIZE": "1",
        "REQUEST_CONCURRENCY": "2",
    }.items():
        monkeypatch.setenv("EMBEDDING_" + k, v)
    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", ingestion._EmbeddingSlots())
    monkeypatch.setattr(ingestion, "_EMBEDDING_PROFILES", {})
    barrier = threading.Barrier(http_workers)
    lock = threading.Lock()
    active = peak = 0

    def handler(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(timeout=10)
        with lock:
            active -= 1
        return response(json.loads(request.content)["input"])

    provider(monkeypatch, handler)
    with ThreadPoolExecutor(max_workers=http_workers // 2) as pool:
        futures = [
            pool.submit(ingestion.embed_texts, [str(n), str(n + 1)]) for n in range(1, http_workers + 1, 2)
        ]
        results = [value for future in futures for value in future.result()]
    assert results == [[float(n), 1.0] for n in range(1, http_workers + 1)]
    assert peak == http_workers and active == 0
    slots = ingestion._EMBEDDING_HTTP_SLOTS
    for _ in range(http_workers):
        assert slots.acquire(blocking=False)
    assert not slots.acquire(blocking=False)
    monkeypatch.setenv("EMBEDDING_HTTP_WORKERS", "2")
    for _ in range(http_workers):
        slots.release()
    with pytest.raises(ValueError, match="重启"):
        slots.acquire(blocking=False)


@pytest.mark.parametrize("limit", [4, 8, 16, 32])
def test_document_lease_capacity_is_shared_and_released(service, monkeypatch, limit):
    monkeypatch.setenv("TEA_PROCESS_WORKERS", "32")
    monkeypatch.setenv("TEA_EMBED_WORKERS", str(limit))
    for n in range(limit + 2):
        register(service, "source-" + str(n))
    services = [CollectionService(service) for _ in range(3)]
    with ThreadPoolExecutor(max_workers=limit + 2) as pool:
        claims = list(pool.map(lambda n: services[n % 3].claim_processing(), range(limit + 2)))
    active = [claim for claim in claims if claim]
    assert len(active) == limit
    with service.db.transaction() as conn:
        queued = conn.execute("SELECT attempts FROM processing_jobs WHERE status='queued'").fetchall()
        assert len(queued) == 2 and all(row[0] == 0 for row in queued)
    services[0]._failure(active[0], ValueError("provider failure"), processing=True)
    replacement = services[1].claim_processing()
    assert replacement and replacement["document_id"] != active[0]["document_id"]
    assert services[2].claim_processing() is None


@pytest.mark.parametrize("value", ["0", "65", "2.5", "invalid"])
def test_invalid_http_capacity_never_starts_a_request(monkeypatch, value):
    monkeypatch.setenv("EMBEDDING_HTTP_WORKERS", value)
    with pytest.raises(ValueError, match="EMBEDDING_HTTP_WORKERS"):
        ingestion._EmbeddingSlots().acquire(blocking=False)


def test_explicit_timeout_preserves_call_budget_and_can_disable_ambiguous_retries(monkeypatch):
    import httpx

    for k, v in {
        "BASE_URL": "https://embed.example/v1",
        "MODEL": "embed",
        "API_KEY": "test",
        "HTTP_WORKERS": "2",
        "HTTP_TIMEOUT_SECONDS": "60",
        "RETRY_AMBIGUOUS_TIMEOUTS": "false",
        "BATCH_SIZE": "6",
        "REQUEST_CONCURRENCY": "1",
    }.items():
        monkeypatch.setenv("EMBEDDING_" + k, v)
    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", ingestion._EmbeddingSlots())
    monkeypatch.setattr(ingestion, "_EMBEDDING_PROFILES", {})
    calls = []

    def timeout(request):
        calls.append(request)
        assert request.extensions["timeout"]["read"] == 60
        assert request.extensions["timeout"]["connect"] == 10
        raise httpx.ReadTimeout("unknown provider outcome")

    provider(monkeypatch, timeout)
    with pytest.raises(ingestion.EmbeddingServiceError) as failure:
        ingestion.embed_texts(["1", "2", "3", "4", "5", "6"])
    assert failure.value.retry_after == 60 and len(calls) == 1
    assert ingestion._EMBEDDING_HTTP_SLOTS.acquire(blocking=False)
    assert ingestion._EMBEDDING_HTTP_SLOTS.acquire(blocking=False)
    ingestion._EMBEDDING_HTTP_SLOTS.release()
    ingestion._EMBEDDING_HTTP_SLOTS.release()


def test_http_callback_preserves_success_before_later_failure(monkeypatch):
    import httpx

    for k, v in {
        "BASE_URL": "https://embed.example/v1",
        "MODEL": "embed",
        "API_KEY": "test",
        "HTTP_WORKERS": "2",
        "BATCH_SIZE": "1",
        "REQUEST_CONCURRENCY": "1",
    }.items():
        monkeypatch.setenv("EMBEDDING_" + k, v)
    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", ingestion._EmbeddingSlots())
    monkeypatch.setattr(ingestion, "_EMBEDDING_PROFILES", {})
    provider(
        monkeypatch,
        lambda req: response(["1"]) if json.loads(req.content)["input"] == ["1"] else httpx.Response(400),
    )
    saved = []
    with pytest.raises(ingestion.EmbeddingServiceError):
        ingestion.embed_texts(["1", "2"], on_batch=lambda texts, values: saved.append((texts, values)))
    assert saved == [(["1"], [[1.0, 1.0]])]
    assert ingestion._EMBEDDING_HTTP_SLOTS.acquire(blocking=False)
    assert ingestion._EMBEDDING_HTTP_SLOTS.acquire(blocking=False)
    ingestion._EMBEDDING_HTTP_SLOTS.release()
    ingestion._EMBEDDING_HTTP_SLOTS.release()


def test_http_callback_preserves_inflight_success_after_peer_429(monkeypatch, tmp_path):
    import httpx

    for k, v in {
        "BASE_URL": "https://embed.example/v1",
        "MODEL": "embed",
        "API_KEY": "test",
        "HTTP_WORKERS": "2",
        "BATCH_SIZE": "1",
        "REQUEST_CONCURRENCY": "2",
    }.items():
        monkeypatch.setenv("EMBEDDING_" + k, v)
    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", ingestion._EmbeddingSlots())
    monkeypatch.setattr(ingestion, "_EMBEDDING_PROFILES", {})
    original_init = ingestion._EmbeddingCall.__init__
    calls = []

    def capture(self, profile):
        original_init(self, profile)
        calls.append(self)

    monkeypatch.setattr(ingestion._EmbeddingCall, "__init__", capture)
    barrier = threading.Barrier(2)
    recovering = False
    resumed_inputs = []

    class AfterFailure(httpx.SyncByteStream):
        def __iter__(self):
            assert calls[0].stop.wait(3)
            yield json.dumps({"data": [{"index": 0, "embedding": [2.0, 1.0]}]}).encode()

    def handler(request):
        if recovering:
            values = json.loads(request.content)["input"]
            resumed_inputs.extend(values)
            return response(values)
        barrier.wait(3)
        if json.loads(request.content)["input"] == ["1"]:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, stream=AfterFailure())

    provider(monkeypatch, handler)
    cache = checkpoint(tmp_path)
    with pytest.raises(ingestion.EmbeddingServiceError):
        cache.embed(["1", "2"], ingestion.embed_texts)
    assert cache.snapshot()["saved_inputs"] == 1
    assert calls[0].requests == 2
    recovering = True
    monkeypatch.setattr(ingestion, "_EMBEDDING_PROFILES", {})
    restarted = checkpoint(tmp_path)
    result = restarted.embed(["1", "2"], ingestion.embed_texts)
    assert resumed_inputs == ["1"] and calls[1].requests == 1
    assert result[1] == pytest.approx([2 / 5**0.5, 1 / 5**0.5])
    assert restarted.snapshot()["saved_inputs"] == 2
