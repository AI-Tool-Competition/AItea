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


@pytest.mark.parametrize(
    "http,lanes,requested,expected", [(64, 2, 12, 12), (64, 2, None, 32), (24, 2, None, 12), (64, 1, 12, 12)]
)
def test_background_can_reduce_without_changing_shared_query_ceiling(http, lanes, requested, expected):
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "complete_embeddings.py"))
    assert runner["document_worker_limit"](http, lanes, requested) == expected


@pytest.mark.parametrize("http,lanes,requested", [(64, 2, 33), (24, 2, 13), (64, 1, None), (64, 2, 0)])
def test_background_cannot_bypass_global_or_processing_ceiling(http, lanes, requested):
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "complete_embeddings.py"))
    with pytest.raises(ValueError):
        runner["document_worker_limit"](http, lanes, requested)


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

    def execute(n):
        lane = "query" if n <= min(4, http_workers // 16) else "bulk"
        with ingestion.embedding_priority(lane):
            return ingestion.embed_texts([str(n), str(n + 1)])

    with ThreadPoolExecutor(max_workers=http_workers // 2) as pool:
        futures = [pool.submit(execute, n) for n in range(1, http_workers + 1, 2)]
        results = [value for future in futures for value in future.result()]
    assert results == [[float(n), 1.0] for n in range(1, http_workers + 1)]
    assert peak == http_workers and active == 0
    slots = ingestion._EMBEDDING_HTTP_SLOTS
    with ingestion.embedding_priority("query"):
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


def test_unresolved_intents_reconcile_each_attempt_and_preserve_legacy(tmp_path):
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "complete_embeddings.py"))
    intents = [
        {"request_id": key, "http_started": True, "input_hashes": [value]}
        for key, value in [("a1", "a"), ("a2", "a"), ("b", "b"), ("c", "c"), ("e", "e")]
    ]
    results = [
        {**intents[1], "http_status": 429},
        {**intents[2], "http_status": 200},
        {**intents[4], "http_status": 429},
        {"http_started": True, "input_hashes": ["legacy"], "http_status": 200},
        {"http_started": True, "input_hashes": ["rejected"], "http_status": 400},
        {"http_started": False, "input_hashes": ["not_sent"]},
    ]
    for name, rows in [("issued-requests.jsonl", intents), ("requests.jsonl", results)]:
        (tmp_path / name).write_text("".join(json.dumps(row) + "\n" for row in rows))
    assert runner["unresolved_issued_inputs"](tmp_path, {"c"}) == {"a", "b", "legacy"}
    assert runner["unresolved_issued_inputs"](tmp_path, {"a", "b", "c", "legacy"}) == set()
    # A torn journal fails closed instead of treating unrecorded outcomes as free.
    with (tmp_path / "requests.jsonl").open("a") as stream:
        stream.write('{"request_id":')
    with pytest.raises(json.JSONDecodeError):
        runner["unresolved_issued_inputs"](tmp_path, set())


@pytest.mark.parametrize(
    "change",
    [
        {"owner": "other"},
        {"fingerprint": "other"},
        {"source_plan_sha256": "0" * 64},
        {"protocol": "unknown"},
        {"unexpected": True},
        {"input_hashes": []},
        {"input_hashes": "a" * 64},
        {"input_hashes": ["A" * 64]},
        {"input_hashes": ["a" * 63]},
        {"input_hashes": [None]},
        {"input_hashes": ["a" * 64, "a" * 64]},
        {"input_hashes": ["b" * 64]},
    ],
)
def test_unresolved_selection_rejects_scope_drift_and_invalid_inputs(tmp_path, change):
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "complete_embeddings.py"))
    plan = {"owner": "owner", "fingerprint": FP, "documents": [{"id": "fixed"}]}
    selection = {
        "protocol": "fixed-embedding-unresolved-recovery-v1",
        "owner": "owner",
        "fingerprint": FP,
        "source_plan_sha256": runner["digest"](plan),
        "input_hashes": ["a" * 64],
        **change,
    }
    path = tmp_path / "selection.json"
    path.write_text(json.dumps(selection))
    with pytest.raises(ValueError, match="Unresolved recovery selection"):
        runner["read_unresolved_selection"](path, plan, {"a" * 64})


def test_unresolved_selection_rejects_oversized_file_and_conflicting_cli_flags(tmp_path, monkeypatch):
    import sys

    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "complete_embeddings.py"))
    path = tmp_path / "oversized.json"
    path.write_bytes(b" " * (4 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="4 MiB"):
        runner["read_unresolved_selection"](path, {}, set())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "complete_embeddings",
            "--plan",
            "plan",
            "--output",
            "out",
            "--models-env",
            "env",
            "--retry-unresolved",
            "--retry-unresolved-selection",
            str(path),
        ],
    )
    with pytest.raises(SystemExit) as error:
        runner["main"]()
    assert error.value.code == 2


@pytest.mark.parametrize("unknown_again", [False, True])
def test_runner_recovers_only_selected_unknowns_and_never_repays_saved_inputs(
    service, tmp_path, monkeypatch, unknown_again
):
    from contextlib import closing
    import os
    import signal
    import sys

    from app import service as service_module
    from app.retrieval import input_hash

    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "complete_embeddings.py"))
    import verify_ingestion

    docs = [register(service, name, context=name, count=1) for name in ("allowed", "saved", "new-unknown")]
    texts = {doc["id"]: json.loads(doc["manifest"])["chunks"][0]["retrieval_text"] for doc in docs}
    hashes = {ident: input_hash(text) for ident, text in texts.items()}
    plan = {
        "protocol": "test-fixed-plan",
        "owner": "owner",
        "fingerprint": FP,
        "data_dir": str(service.db.data_dir),
        "documents": [
            {key: doc[key] for key in ("id", "version", "content_hash", "source_url", "storage_path", "size")}
            for doc in docs
        ],
    }
    plan_path, selection_path = tmp_path / "plan.json", tmp_path / "recover.json"
    plan_path.write_text(json.dumps(plan))
    selection_path.write_text(
        json.dumps(
            {
                "protocol": "fixed-embedding-unresolved-recovery-v1",
                "owner": "owner",
                "fingerprint": FP,
                "source_plan_sha256": runner["digest"](plan),
                "input_hashes": [hashes[doc["id"]] for doc in docs[:2]],
            }
        )
    )
    output = tmp_path / "run"
    output.mkdir()
    journal = "".join(
        json.dumps({"http_started": True, "http_status": 504, "input_hashes": [value]}) + "\n"
        for value in hashes.values()
    )
    (output / "requests.jsonl").write_text(journal)
    cache = embedding_reuse.InputCheckpointCache(
        output / "successful-inputs.sqlite3",
        {"owner": "owner", "fingerprint": FP, "source_plan": runner["digest"](plan)},
    )
    cache.save([texts[docs[1]["id"]]], [[0.0, 1.0]])
    called = []
    observers = []

    def embed(values, *, on_batch):
        called.extend(values)
        if unknown_again:
            observer = observers[-1]
            observer.stop.set()
            observer.failure = "issued_request_result_unknown"
            with (output / "requests.jsonl").open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "run_id": observer.run_id,
                            "request_id": "new-unknown",
                            "http_started": True,
                            "http_status": 504,
                            "input_hashes": [input_hash(text) for text in values],
                        }
                    )
                    + "\n"
                )
            raise ingestion.EmbeddingServiceError(
                retryable=True, http_status=504, request_outcome_unknown=True, request_sent=True
            )
        result = [[1.0, 0.0] for _ in values]
        on_batch(values, result)
        return result

    monkeypatch.setattr(os, "environ", dict(os.environ))
    for key, value in {"BASE_URL": "https://embed.example/v1", "MODEL": "embed", "API_KEY": "test"}.items():
        monkeypatch.setenv("EMBEDDING_" + key, value)
    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", ingestion._EmbeddingSlots())
    monkeypatch.setattr(service_module, "Service", lambda _: service)
    monkeypatch.setattr(verify_ingestion, "configure_models", lambda _: None)
    monkeypatch.setattr(runner["Observations"], "install", lambda observer: observers.append(observer))
    monkeypatch.setattr(signal, "signal", lambda *_: None)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "complete_embeddings",
            "--plan",
            str(plan_path),
            "--output",
            str(output),
            "--models-env",
            "unused",
            "--admission-path",
            str(tmp_path / "runtime" / "admission.sqlite3"),
            "--retry-unresolved-selection",
            str(selection_path),
            "--apply",
        ],
    )
    monkeypatch.setattr(ingestion, "embed_texts", embed)
    assert runner["main"]() == (3 if unknown_again else 0)
    assert called == [texts[docs[0]["id"]]]
    report = json.loads((output / "report.json").read_text())
    assert report["completed_documents"] == (1 if unknown_again else 2)
    assert report["missing_rows"] == (2 if unknown_again else 1)
    assert report["explicitly_allowed_unresolved_inputs"] == 1 and report["quarantined_inputs"] == 1
    assert report["deferred_documents"] == 1 and all(report["integrity"].values())
    snapshot = json.loads((output / ("unresolved-" + report["run_id"] + ".json")).read_text())
    assert snapshot["allowed_input_hashes"] == [hashes[docs[0]["id"]]]
    assert snapshot["quarantined_input_hashes"] == [hashes[docs[2]["id"]]]
    assert (
        snapshot["recovery_selection"]["sha256"]
        == report["configuration"]["retry_unresolved_selection_sha256"]
    )
    before = {doc["id"]: vectors(service, doc) for doc in docs}
    retained_journal = (output / "requests.jsonl").read_text()
    monkeypatch.setattr(
        ingestion, "embed_texts", lambda *_: pytest.fail("restarted selection repaid a successful input")
    )
    assert runner["main"]() == (3 if unknown_again else 0)
    assert before == {doc["id"]: vectors(service, doc) for doc in docs}
    assert (output / "requests.jsonl").read_text() == retained_journal
    assert retained_journal.startswith(journal)
    with closing(service.db.connect()) as conn:
        assert (
            conn.execute(
                "SELECT status FROM processing_jobs WHERE document_id=?", (docs[2]["id"],)
            ).fetchone()[0]
            == "queued"
        )
    report = json.loads((output / "report.json").read_text())
    assert report["explicitly_allowed_unresolved_inputs"] == 0
    assert report["quarantined_inputs"] == (2 if unknown_again else 1)
    assert report["consumed_recovery_inputs"] == int(unknown_again)


def test_runner_drains_paid_peer_after_local_future_failure(tmp_path):
    from concurrent.futures import Future
    import sqlite3

    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "complete_embeddings.py"))
    observations = runner["Observations"](tmp_path)
    first, second = Future(), Future()
    first.set_exception(sqlite3.OperationalError("database is locked"))
    active = {first: {"document_id": "first"}, second: {"document_id": "second"}}
    assert runner["finish_claims"](active, observations) == 1
    assert observations.stop.is_set() and list(active) == [second]
    cache = checkpoint(tmp_path)
    cache.save(["paid"], [[1.0, 0.0]])
    second.set_result(None)
    assert runner["finish_claims"](active, observations) == 1 and not active
    assert checkpoint(tmp_path).embed(["paid"], lambda *_: pytest.fail("paid twice")) == [[1.0, 0.0]]
    audit = json.loads((tmp_path / "runtime-errors.jsonl").read_text())
    assert audit["operation"] == "processing_result" and audit["error_type"] == "OperationalError"


def test_runner_recovers_only_expired_fixed_owner_embedding_leases(service):
    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "complete_embeddings.py"))
    expired, active, delayed, parse = [
        register(service, key) for key in ["expired", "active", "delayed", "parse"]
    ]
    register(service, "outside-fixed-plan")
    foreign = register(service, "foreign", owner="other")
    old_claim = service.collection.claim_processing(owner_id="owner", document_ids=[expired["id"]])
    assert old_claim
    with service.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET lease_until=0 WHERE document_id=?", (expired["id"],))
        conn.execute(
            "UPDATE processing_jobs SET status='processing',lease_until=? WHERE document_id=?",
            (time.time() + 120, active["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET available_at=? WHERE document_id=?",
            (time.time() + 120, delayed["id"]),
        )
        conn.execute("UPDATE processing_jobs SET phase='parse' WHERE document_id=?", (parse["id"],))
    ids = [d["id"] for d in [expired, active, delayed, parse, foreign]]
    assert runner["admission_candidates"](service, "owner", ids, 32) == [expired["id"]]
    renewed = service.collection.claim_processing(owner_id="owner", document_ids=[expired["id"]])
    assert renewed and renewed["lease_token"] != old_claim["lease_token"]
    assert runner["admission_candidates"](service, "owner", ids, 32) == []


@pytest.mark.parametrize("crash,status", [(False, 429), (False, 503), (False, 504), (True, 429)])
def test_request_intent_is_durable_before_transport_and_survives_crash(monkeypatch, tmp_path, crash, status):
    import httpx
    import multiprocessing
    import os

    from app import parsing, vision
    from app.retrieval import input_hash

    runner = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "complete_embeddings.py"))
    for k, v in {
        "BASE_URL": "https://embed.example/v1",
        "MODEL": "embed",
        "API_KEY": "test",
        "HTTP_WORKERS": "1",
        "BATCH_SIZE": "1",
        "REQUEST_CONCURRENCY": "1",
        "RETRY_AMBIGUOUS_TIMEOUTS": "true",
    }.items():
        monkeypatch.setenv("EMBEDDING_" + k, v)
    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", ingestion._EmbeddingSlots())
    monkeypatch.setattr(ingestion, "_EMBEDDING_PROFILES", {})
    for module, names in [
        (ingestion, ("_embedding_response", "_download")),
        (parsing, ("parse_bounded", "ocr_bounded")),
        (vision, ("transcribe_page", "transcribe_image")),
    ]:
        for name in names:
            monkeypatch.setattr(module, name, getattr(module, name))
    fsynced = []
    original_fsync = os.fsync

    def fsync(fd):
        original_fsync(fd)
        fsynced.append(fd)

    monkeypatch.setattr(os, "fsync", fsync)

    def handler(request):
        assert fsynced
        intent = json.loads((tmp_path / "issued-requests.jsonl").read_text())
        assert intent["input_hashes"] == [input_hash("1")]
        assert len(intent["request_id"]) == 32
        assert (tmp_path / "issued-requests.jsonl").stat().st_mode & 0o777 == 0o600
        if crash:
            os._exit(23)
        return httpx.Response(status, headers={"Retry-After": "3"} if status == 429 else {})

    provider(monkeypatch, handler)
    observations = runner["Observations"](tmp_path)
    observations.run_id = "test"
    observations.install()
    if crash:
        child = multiprocessing.get_context("fork").Process(target=ingestion.embed_texts, args=(["1"],))
        child.start()
        child.join(5)
        assert child.exitcode == 23
        assert not (tmp_path / "requests.jsonl").exists()
        assert runner["unresolved_issued_inputs"](tmp_path, set()) == {input_hash("1")}
    else:
        with pytest.raises(ingestion.EmbeddingServiceError):
            ingestion.embed_texts(["1"])
        results = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
        issued = [row for row in results if row["http_started"]]
        assert len(issued) == 1  # Stop also blocks an old provider lane's internal retry.
        result = issued[0]
        intent = json.loads((tmp_path / "issued-requests.jsonl").read_text())
        assert result["request_id"] == intent["request_id"]
        assert result["retry_after_seconds"] == (3 if status == 429 else None)
        assert observations.stop.is_set()
        assert observations.failure == ("provider_rate_limited" if status == 429 else "provider_unavailable")
        assert observations.active == 0
        assert runner["unresolved_issued_inputs"](tmp_path, set()) == (
            set() if status == 429 else {input_hash("1")}
        )


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504])
def test_ambiguous_gateway_failure_is_not_shrunk_or_reissued(monkeypatch, status):
    import httpx

    for k, v in {
        "BASE_URL": "https://embed.example/v1",
        "MODEL": "embed",
        "API_KEY": "test",
        "HTTP_WORKERS": "1",
        "BATCH_SIZE": "2",
        "REQUEST_CONCURRENCY": "1",
        "RETRY_AMBIGUOUS_TIMEOUTS": "false",
    }.items():
        monkeypatch.setenv("EMBEDDING_" + k, v)
    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", ingestion._EmbeddingSlots())
    monkeypatch.setattr(ingestion, "_EMBEDDING_PROFILES", {})
    sent = []

    def handler(request):
        sent.append(json.loads(request.content)["input"])
        return httpx.Response(status)

    provider(monkeypatch, handler)
    with pytest.raises(ingestion.EmbeddingServiceError) as failure:
        ingestion.embed_texts(["1", "2"])
    assert sent == [["1", "2"]]
    assert failure.value.retry_after == 60
    assert ingestion._EMBEDDING_HTTP_SLOTS.acquire(blocking=False)
    ingestion._EMBEDDING_HTTP_SLOTS.release()
