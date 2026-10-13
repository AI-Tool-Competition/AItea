"""Provider batch tuning preserves input order and durable processing checkpoints."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BoundedSemaphore, Event, Lock

import httpx
import pytest

from app import ingestion


@pytest.fixture
def configured(monkeypatch):
    for key, value in {"BASE_URL": "https://embed.example/v1", "MODEL": "embed", "API_KEY": "test"}.items():
        monkeypatch.setenv("EMBEDDING_" + key, value)
    monkeypatch.setattr(ingestion, "_EMBEDDING_PROFILES", {})
    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", BoundedSemaphore(2))
    monkeypatch.delenv("EMBEDDING_REQUEST_CONCURRENCY", raising=False)


def provider(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))


def response(values, dimension=2):
    return httpx.Response(
        200,
        json={
            "data": [
                {"index": i, "embedding": [float(value), *([1.0] * (dimension - 1))]}
                for i, value in reversed(list(enumerate(values)))
            ]
        },
    )


def test_configured_small_batches_preserve_response_order(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    requests = []

    def handler(request):
        values = json.loads(request.content)["input"]
        requests.append(values)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [float(value), 1.0]}
                    for i, value in reversed(list(enumerate(values)))
                ]
            },
        )

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert ingestion.embed_texts(["1", "2", "3", "4", "5"]) == [[float(i), 1.0] for i in range(1, 6)]
    assert requests == [["1", "2"], ["3", "4"], ["5"]]


@pytest.mark.parametrize("size", ["0", "65", "invalid"])
def test_invalid_batch_configuration_never_calls_provider(configured, monkeypatch, size):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", size)
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("invalid configuration called provider"))
    with pytest.raises(ValueError, match="EMBEDDING_BATCH_SIZE"):
        ingestion.embed_texts(["text"])


def test_timeouts_learn_provider_ceiling_and_preserve_all_inputs(configured, monkeypatch):
    requests = []

    def handler(request):
        values = json.loads(request.content)["input"]
        requests.append(list(values))
        if len(values) > 4:
            raise httpx.ReadTimeout("secret provider details", request=request)
        return response(values)

    provider(monkeypatch, handler)
    values = [str(index) for index in range(1, 66)]
    assert ingestion.embed_texts(values) == [[float(value), 1.0] for value in values]
    assert [len(batch) for batch in requests[:5]] == [64, 32, 16, 8, 4]
    assert [value for batch in requests[4:] for value in batch] == values
    assert ingestion.embedding_batch_size() == 4
    requests.clear()
    assert ingestion.embed_texts(["66", "67", "68", "69", "70"])
    assert [len(batch) for batch in requests] == [4, 1]


def test_request_size_rejection_adapts_without_retrying_same_payload(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "3")
    requests = []

    def handler(request):
        values = json.loads(request.content)["input"]
        requests.append(values)
        return httpx.Response(413) if len(values) > 1 else response(values)

    provider(monkeypatch, handler)
    assert ingestion.embed_texts(["1", "2", "3", "4"]) == [[float(i), 1.0] for i in range(1, 5)]
    assert requests == [["1", "2", "3"], ["1"], ["2"], ["3"], ["4"]]
    assert ingestion.embedding_batch_size() == 1


def test_unavailable_provider_has_bounded_adaptive_attempts(configured, monkeypatch):
    requests = []
    monkeypatch.setattr(ingestion.time, "sleep", lambda seconds: None)

    def handler(request):
        requests.append(len(json.loads(request.content)["input"]))
        raise httpx.ReadTimeout("SECRET credential in provider error", request=request)

    provider(monkeypatch, handler)
    with pytest.raises(ingestion.EmbeddingServiceError) as error:
        ingestion.embed_texts(["1"] * 64)
    assert requests == [64, 32, 16, 8, 4, 2, 1, 1]
    assert error.value.retryable
    assert "SECRET" not in str(error.value)
    assert error.value.__suppress_context__


def test_retry_after_defers_sibling_jobs_without_another_request(configured, monkeypatch):
    clock, requests = [100.0], []
    monkeypatch.setattr(ingestion.time, "monotonic", lambda: clock[0])

    def handler(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "9999"})

    provider(monkeypatch, handler)
    with pytest.raises(ingestion.EmbeddingServiceError) as first:
        ingestion.embed_texts(["1"])
    assert first.value.retry_after == 300
    clock[0] += 30
    with pytest.raises(ingestion.EmbeddingProviderDeferred) as deferred:
        ingestion.embed_texts(["2"])
    assert deferred.value.retry_after == 270
    assert len(requests) == 1
    clock[0] += 270
    with pytest.raises(ingestion.EmbeddingServiceError):
        ingestion.embed_texts(["2"])
    assert len(requests) == 2


def test_short_transient_failure_retries_without_changing_batch(configured, monkeypatch):
    requests, sleeps = [], []
    monkeypatch.setattr(ingestion.time, "sleep", sleeps.append)

    def handler(request):
        values = json.loads(request.content)["input"]
        requests.append(values)
        return httpx.Response(503) if len(requests) < 3 else response(values)

    provider(monkeypatch, handler)
    assert ingestion.embed_texts(["1", "2"]) == [[1.0, 1.0], [2.0, 1.0]]
    assert requests == [["1", "2"]] * 3
    assert sleeps == [0.25, 0.5]
    assert ingestion.embedding_batch_size() == 64


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_deterministic_provider_failure_never_retries(configured, monkeypatch, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, json={"error": "secret document or credentials"})

    provider(monkeypatch, handler)
    with pytest.raises(ingestion.EmbeddingServiceError) as error:
        ingestion.embed_texts(["1", "2"])
    assert not error.value.retryable
    assert len(requests) == 1
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("index", [True, 0.0, "0", None])
def test_provider_index_must_be_an_exact_integer(configured, monkeypatch, index):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": [{"index": index, "embedding": [1.0, 2.0]}]})

    provider(monkeypatch, handler)
    with pytest.raises(ValueError, match="索引"):
        ingestion.embed_texts(["1"])
    assert len(requests) == 1


def test_adaptive_requests_still_reject_dimension_changes(configured, monkeypatch):
    requests = []

    def handler(request):
        values = json.loads(request.content)["input"]
        requests.append(values)
        if len(values) > 2:
            return httpx.Response(413)
        return response(values, dimension=2 if len(requests) == 2 else 3)

    provider(monkeypatch, handler)
    with pytest.raises(ValueError, match="维度"):
        ingestion.embed_texts(["1", "2", "3", "4"])
    assert requests == [["1", "2", "3", "4"], ["1", "2"], ["3", "4"]]


def test_profile_is_bounded_and_separates_credentials_and_models(configured, monkeypatch):
    key = ingestion._embedding_profile_key()
    ingestion._embedding_profile(key, size=4, cooldown=30)
    monkeypatch.setenv("EMBEDDING_API_KEY", "another-secret-key")
    assert ingestion.embedding_batch_size() == 64
    monkeypatch.setenv("EMBEDDING_API_KEY", "test")
    monkeypatch.setenv("EMBEDDING_MODEL", "another-model")
    assert ingestion.embedding_batch_size() == 64
    for number in range(40):
        monkeypatch.setenv("EMBEDDING_MODEL", f"model-{number}")
        ingestion._embedding_profile(ingestion._embedding_profile_key(), size=2)
    assert len(ingestion._EMBEDDING_PROFILES) == 32
    assert all(len(profile_key) == 64 for profile_key in ingestion._EMBEDDING_PROFILES)
    assert "another-secret-key" not in repr(ingestion._EMBEDDING_PROFILES)


def test_complete_call_deadline_prevents_retrying_a_slow_response(configured, monkeypatch):
    clock, requests = [0.0], []
    monkeypatch.setattr(ingestion.time, "monotonic", lambda: clock[0])

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] = 301
            yield b'{"data": []}'

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=SlowBody())

    provider(monkeypatch, handler)
    with pytest.raises(ingestion.EmbeddingServiceError):
        ingestion.embed_texts(["1", "2"])
    assert len(requests) == 1


def test_two_workers_share_downward_profile_without_mixing_input_order(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "8")
    barrier, lock, requests = Barrier(2, timeout=5), Lock(), []

    def handler(request):
        values = json.loads(request.content)["input"]
        with lock:
            requests.append(values)
        if len(values) > 4:
            # Both jobs may already have acquired a larger batch before either
            # provider response arrives. They must converge safely afterwards.
            barrier.wait()
            raise httpx.ReadTimeout("oversized concurrent request", request=request)
        return response(values)

    provider(monkeypatch, handler)
    inputs = [[str(index) for index in range(start, start + 16)] for start in (1, 101)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(ingestion.embed_texts, inputs))
    assert outputs == [[[float(value), 1.0] for value in values] for values in inputs]
    assert sorted(len(values) for values in requests) == [4] * 8 + [8] * 2
    assert ingestion.embedding_batch_size() == 4


@pytest.mark.parametrize("concurrency", ["0", "3", "invalid", "1.0", "01"])
def test_invalid_request_concurrency_never_calls_provider(configured, monkeypatch, concurrency):
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", concurrency)
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("invalid configuration called provider"))
    with pytest.raises(ValueError, match="EMBEDDING_REQUEST_CONCURRENCY"):
        ingestion.embed_texts(["text"])


def test_claim_size_separates_request_ceiling_and_respects_learned_profile(configured, monkeypatch):
    assert ingestion.embedding_claim_size() == 64
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "4")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "2")
    assert ingestion.embedding_batch_size() == 4
    assert ingestion.embedding_claim_size() == 8
    ingestion._embedding_profile(ingestion._embedding_profile_key(), size=2)
    assert ingestion.embedding_claim_size() == 4


def test_parallel_batches_overlap_and_return_original_order(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "2")
    barrier, completed, lock = Barrier(2, timeout=5), [], Lock()

    def handler(request):
        values = json.loads(request.content)["input"]
        barrier.wait()
        if int(values[0]) % 4 == 1:
            time.sleep(0.03)
        with lock:
            completed.append(values)
        return response(values)

    provider(monkeypatch, handler)
    values = [str(index) for index in range(1, 9)]
    assert ingestion.embed_texts(values) == [[float(value), 1.0] for value in values]
    assert completed == [["3", "4"], ["1", "2"], ["7", "8"], ["5", "6"]]
    assert not any(thread.name.startswith("tea-embedding") for thread in threading.enumerate())


def test_process_slots_bound_all_document_and_query_callers(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "2")
    lock, active, peak, requests = Lock(), [0], [0], []

    def handler(request):
        values = json.loads(request.content)["input"]
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            requests.append(values)
        try:
            time.sleep(0.025)
            return response(values)
        finally:
            with lock:
                active[0] -= 1

    provider(monkeypatch, handler)
    inputs = [["1", "2", "3", "4"], ["101", "102", "103", "104"], ["201"]]
    with ThreadPoolExecutor(max_workers=3) as pool:
        outputs = list(pool.map(ingestion.embed_texts, inputs))
    assert peak == [2]
    assert active == [0]
    assert len(requests) == 5
    assert outputs == [[[float(value), 1.0] for value in values] for values in inputs]


def test_two_healthy_slow_documents_wait_for_shared_slots_without_losing_vectors(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "4")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "2")
    both_started, lock, requests = Event(), Lock(), []
    active, peak = [0], [0]
    original_response = ingestion._embedding_response

    def scheduled_response(client, batch, base, key, model, call):
        # Exercise the observed production schedule: each document has one
        # healthy request in flight while its other request awaits capacity.
        if batch[0] in {"5", "105"}:
            assert both_started.wait(timeout=5)
        return original_response(client, batch, base, key, model, call)

    monkeypatch.setattr(ingestion, "_embedding_response", scheduled_response)

    def handler(request):
        values = json.loads(request.content)["input"]
        with lock:
            requests.append(values)
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            if len(requests) == 2:
                both_started.set()
        try:
            assert both_started.wait(timeout=5)
            # A normal provider latency longer than the old five-second local
            # slot wait must not consume queue attempts or discard success.
            time.sleep(10)
            return response(values)
        finally:
            with lock:
                active[0] -= 1

    provider(monkeypatch, handler)
    inputs = [[str(index) for index in range(start, start + 8)] for start in (1, 101)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        outputs = list(pool.map(ingestion.embed_texts, inputs))
    assert outputs == [[[float(value), 1.0] for value in values] for values in inputs]
    assert peak == [2]
    assert active == [0]
    assert sorted(tuple(values) for values in requests) == sorted(
        tuple(values[offset : offset + 4]) for values in inputs for offset in (0, 4)
    )
    assert not any(thread.name.startswith("tea-embedding") for thread in threading.enumerate())


@pytest.mark.parametrize("status", [401, 429])
def test_parallel_failure_drains_only_existing_wave_and_keeps_safe_error(configured, monkeypatch, status):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "2")
    barrier, requests, lock = Barrier(2, timeout=5), [], Lock()

    def handler(request):
        values = json.loads(request.content)["input"]
        with lock:
            requests.append(values)
        barrier.wait()
        if values[0] == "1":
            return httpx.Response(status, json={"secret": "payload/key"}, headers={"Retry-After": "7"})
        time.sleep(0.025)
        return response(values)

    provider(monkeypatch, handler)
    with pytest.raises(ingestion.EmbeddingServiceError) as error:
        ingestion.embed_texts([str(index) for index in range(1, 9)])
    assert len(requests) == 2
    assert error.value.retryable is (status == 429)
    assert error.value.retry_after == (7 if status == 429 else None)
    assert "payload" not in str(error.value)
    assert error.value.__suppress_context__
    assert not any(thread.name.startswith("tea-embedding") for thread in threading.enumerate())
    assert ingestion._EMBEDDING_HTTP_SLOTS.acquire(blocking=False)
    assert ingestion._EMBEDDING_HTTP_SLOTS.acquire(blocking=False)
    ingestion._EMBEDDING_HTTP_SLOTS.release()
    ingestion._EMBEDDING_HTTP_SLOTS.release()
    if status == 429:
        with pytest.raises(ingestion.EmbeddingProviderDeferred):
            ingestion.embed_texts(["9"])
        assert len(requests) == 2


def test_parallel_batches_reject_dimension_change_between_lanes(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "2")
    barrier, requests = Barrier(2, timeout=5), []

    def handler(request):
        values = json.loads(request.content)["input"]
        requests.append(values)
        barrier.wait()
        return response(values, dimension=2 if values[0] == "1" else 3)

    provider(monkeypatch, handler)
    with pytest.raises(ValueError, match="维度"):
        ingestion.embed_texts([str(index) for index in range(1, 9)])
    assert len(requests) == 2


def test_parallel_adaptation_keeps_order_and_limits_the_complete_failure_budget(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "2")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "64")
    monkeypatch.setattr(ingestion.time, "sleep", lambda seconds: None)
    requests, lock, first_wave = [], Lock(), Barrier(2, timeout=5)

    def handler(request):
        values = json.loads(request.content)["input"]
        with lock:
            requests.append(values)
        if len(values) == 64:
            first_wave.wait()
        raise httpx.ReadTimeout("secret credential", request=request)

    provider(monkeypatch, handler)
    with pytest.raises(ingestion.EmbeddingServiceError) as error:
        ingestion.embed_texts(["1"] * 128)
    assert len(requests) == 8
    assert "secret" not in str(error.value)
    assert not any(thread.name.startswith("tea-embedding") for thread in threading.enumerate())


def test_parallel_adaptive_requests_preserve_every_input_once_successful(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "2")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "8")
    first_wave, successful, lock = Barrier(2, timeout=5), [], Lock()

    def handler(request):
        values = json.loads(request.content)["input"]
        if len(values) == 8:
            first_wave.wait()
            return httpx.Response(413)
        with lock:
            successful.extend(values)
        return response(values)

    provider(monkeypatch, handler)
    values = [str(index) for index in range(1, 33)]
    assert ingestion.embed_texts(values) == [[float(value), 1.0] for value in values]
    assert sorted(successful, key=int) == values
    assert ingestion.embedding_batch_size() == 4


def test_slot_wait_is_clipped_by_call_deadline_and_never_issues_http(configured, monkeypatch):
    clock, waits, released = [0.0], [], []
    monkeypatch.setattr(ingestion.time, "monotonic", lambda: clock[0])

    class OccupiedSlots:
        def acquire(self, *, timeout):
            waits.append(timeout)
            clock[0] += timeout
            return False

        def release(self):
            released.append(True)

    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", OccupiedSlots())
    provider(monkeypatch, lambda request: pytest.fail("no slot must mean no HTTP"))
    original = ingestion._EmbeddingCall.__init__

    def near_deadline(self, profile_key):
        original(self, profile_key)
        clock[0] = 299.9

    monkeypatch.setattr(ingestion._EmbeddingCall, "__init__", near_deadline)
    with pytest.raises(ingestion.EmbeddingServiceError) as error:
        ingestion.embed_texts(["1"])
    assert not isinstance(error.value, ingestion.EmbeddingProviderDeferred)
    assert waits == pytest.approx([0.1])
    assert not released


def test_occupied_slots_wait_within_existing_deadline_without_provider_requests(configured, monkeypatch):
    clock, waits = [0.0], []
    monkeypatch.setattr(ingestion.time, "monotonic", lambda: clock[0])

    class OccupiedSlots:
        def acquire(self, *, timeout):
            waits.append(timeout)
            clock[0] += timeout
            return False

    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", OccupiedSlots())
    provider(monkeypatch, lambda request: pytest.fail("occupied slots must issue no HTTP"))
    with pytest.raises(ingestion.EmbeddingServiceError) as error:
        ingestion.embed_texts(["1"])
    assert not isinstance(error.value, ingestion.EmbeddingProviderDeferred)
    assert error.value.retry_after is None
    assert sum(waits) == pytest.approx(300)
    assert max(waits) == 0.25


def test_waiting_http_slots_recheck_provider_cooldown_without_issuing_requests(configured, monkeypatch):
    waits = []

    class CoolingProviderSlots:
        def acquire(self, *, timeout):
            waits.append(timeout)
            ingestion._embedding_profile(ingestion._embedding_profile_key(), cooldown=10)
            return False

    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", CoolingProviderSlots())
    provider(monkeypatch, lambda request: pytest.fail("cooled provider must issue no HTTP"))
    with pytest.raises(ingestion.EmbeddingProviderDeferred) as error:
        ingestion.embed_texts(["1"])
    assert waits == [0.25]
    assert 0 < error.value.retry_after <= 10


def test_waiting_http_slot_observes_sibling_cancellation_before_next_poll(configured, monkeypatch):
    call, waits = ingestion._EmbeddingCall(ingestion._embedding_profile_key()), []

    class CancelledSlots:
        def acquire(self, *, timeout):
            waits.append(timeout)
            call.stop.set()
            return False

    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", CancelledSlots())
    with pytest.raises(ingestion._EmbeddingCallCancelled):
        ingestion._embedding_response(None, ["1"], "https://embed.example/v1", "test", "embed", call)
    assert waits == [0.25]
    assert call.requests == 0


def test_request_timeout_accounts_for_slot_wait(configured, monkeypatch):
    clock, timeouts = [0.0], []
    monkeypatch.setattr(ingestion.time, "monotonic", lambda: clock[0])

    class DelayedSlots:
        def acquire(self, *, timeout):
            clock[0] = 299.5
            return True

        def release(self):
            pass

    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", DelayedSlots())

    def handler(request):
        timeouts.append(request.extensions["timeout"])
        return response(["1"])

    provider(monkeypatch, handler)
    assert ingestion.embed_texts(["1"]) == [[1.0, 1.0]]
    assert all(value == 0.5 for value in timeouts[0].values())


def test_cooldown_is_rechecked_after_waiting_for_http_slot(configured, monkeypatch):
    released = []

    class NewlyAvailableSlots:
        def acquire(self, *, timeout):
            ingestion._embedding_profile(ingestion._embedding_profile_key(), cooldown=10)
            return True

        def release(self):
            released.append(True)

    monkeypatch.setattr(ingestion, "_EMBEDDING_HTTP_SLOTS", NewlyAvailableSlots())
    provider(monkeypatch, lambda request: pytest.fail("cooldown after slot wait must issue no HTTP"))
    with pytest.raises(ingestion.EmbeddingProviderDeferred):
        ingestion.embed_texts(["1"])
    assert released == [True]


def test_parallel_requests_share_call_deadline_and_stop_future_waves(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "2")
    clock, requests, first_wave = [0.0], [], Barrier(2, timeout=5)
    monkeypatch.setattr(ingestion.time, "monotonic", lambda: clock[0])

    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            clock[0] = 301
            yield b'{"data": []}'

    def handler(request):
        requests.append(request)
        first_wave.wait()
        return httpx.Response(200, stream=SlowBody())

    provider(monkeypatch, handler)
    with pytest.raises(ingestion.EmbeddingServiceError):
        ingestion.embed_texts([str(index) for index in range(1, 9)])
    assert len(requests) == 2
    assert not any(thread.name.startswith("tea-embedding") for thread in threading.enumerate())


def test_cooldown_after_partial_success_does_not_claim_zero_http(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "1")
    requests = []

    def handler(request):
        requests.append(request)
        ingestion._embedding_profile(ingestion._embedding_profile_key(), cooldown=10)
        return response(["1"])

    provider(monkeypatch, handler)
    with pytest.raises(ingestion.EmbeddingServiceError) as error:
        ingestion.embed_texts(["1", "2"])
    assert not isinstance(error.value, ingestion.EmbeddingProviderDeferred)
    assert error.value.retryable
    assert len(requests) == 1


def test_operator_interrupt_stops_retries_before_draining_workers(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "2")
    barrier, requests = Barrier(2, timeout=5), []

    def handler(request):
        values = json.loads(request.content)["input"]
        requests.append(values)
        barrier.wait()
        if values[0] == "1":
            raise KeyboardInterrupt()
        time.sleep(0.025)
        raise httpx.ReadTimeout("in-flight request interrupted", request=request)

    provider(monkeypatch, handler)
    with pytest.raises(KeyboardInterrupt):
        ingestion.embed_texts([str(index) for index in range(1, 9)])
    assert len(requests) == 2
    assert not any(thread.name.startswith("tea-embedding") for thread in threading.enumerate())
