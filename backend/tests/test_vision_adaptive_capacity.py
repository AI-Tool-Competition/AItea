"""Shared permits and mocked HTTP feedback, without provider throughput claims."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event, Lock

import httpx
import pytest

from app import native_asset_ocr, ocr_recovery, parsing, vision
from app.ingestion import parse_file
from test_ingestion_quality import make_pdf
from test_native_asset_ocr import docx
from test_vision_ocr import payload


@pytest.fixture
def adaptive(monkeypatch):
    monkeypatch.setenv("VISION_OCR_WORKERS", "64")
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", "true")
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    clock = [0.0]
    slots = parsing._VisionSlots(clock=lambda: clock[0])
    monkeypatch.setattr(parsing, "_OCR_SLOTS", slots)
    return slots, clock


def initialize(slots):
    assert slots.acquire(timeout=0)
    slots.release()


def grow(slots, clock):
    clock[0] += 30
    for _ in range(max(16, slots.snapshot()["target"])):
        slots.report_provider_result(status_code=200)


@pytest.mark.parametrize("maximum", [1, 2, 8, 12, 16, 32, 64])
@pytest.mark.parametrize("enabled", [False, True])
def test_initial_target_and_floor_preserve_configured_maximum(adaptive, monkeypatch, maximum, enabled):
    slots, _ = adaptive
    monkeypatch.setenv("VISION_OCR_WORKERS", str(maximum))
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", str(enabled))
    target = min(maximum, 16) if enabled else maximum
    assert all(slots.acquire(timeout=0) for _ in range(target))
    assert not slots.acquire(timeout=0)
    state = slots.snapshot()
    assert (state["maximum"], state["target"], state["floor"]) == (maximum, target, 1)
    slots.report_provider_result(status_code=503)
    assert slots.snapshot()["target"] == (max(1, target // 2) if enabled else target)
    for _ in range(target):
        slots.release()
    with pytest.raises(ValueError, match="not acquired"):
        slots.release()


def test_healthy_growth_requires_time_and_responses_each_step_and_stops_at_maximum(adaptive):
    slots, clock = adaptive
    initialize(slots)
    for _ in range(32):
        slots.report_provider_result(status_code=200)
    assert slots.snapshot()["target"] == 16
    clock[0] = 29.999
    slots.report_provider_result(status_code=200)
    assert slots.snapshot()["target"] == 16
    clock[0] = 30
    slots.report_provider_result(status_code=200)
    assert slots.snapshot()["target"] == 24
    clock[0] = 60
    for _ in range(23):
        slots.report_provider_result(status_code=200)
    assert slots.snapshot()["target"] == 24
    slots.report_provider_result(status_code=200)
    assert slots.snapshot()["target"] == 32
    for expected in [40, 48, 56, 64, 64]:
        grow(slots, clock)
        assert slots.snapshot()["target"] == expected
    assert slots.snapshot()["increases"] == 6


@pytest.mark.parametrize(
    "feedback, reason",
    [
        ({"status_code": 429}, "HTTP 429"),
        ({"status_code": 503}, "HTTP 503"),
        ({"status_code": 504}, "HTTP 504"),
        ({"status_code": 599}, "HTTP 599"),
        ({"timeout": "ReadTimeout"}, "ReadTimeout"),
        ({"timeout": "ConnectTimeout"}, "ConnectTimeout"),
    ],
)
def test_failure_halves_and_cools_down_without_replaying_requests(adaptive, feedback, reason):
    slots, clock = adaptive
    initialize(slots)
    grow(slots, clock)
    grow(slots, clock)
    assert slots.snapshot()["target"] == 32
    slots.report_provider_result(**feedback)
    assert slots.snapshot()["target"] == 16
    assert slots.snapshot()["last_failure"] == reason
    for _ in range(32):
        slots.report_provider_result(status_code=200)
    clock[0] += 29.999
    slots.report_provider_result(status_code=200)
    assert slots.snapshot()["target"] == 16
    clock[0] += 0.001
    slots.report_provider_result(status_code=200)
    assert slots.snapshot()["target"] == 24
    assert slots.snapshot()["capacity_failures"] == 1


def test_existing_permits_drain_and_floor_failure_restarts_cooldown(adaptive):
    slots, clock = adaptive
    initialize(slots)
    grow(slots, clock)
    grow(slots, clock)
    assert all(slots.acquire(timeout=0) for _ in range(32))
    slots.report_provider_result(status_code=503)
    assert slots.snapshot()["active"] == 32 and slots.snapshot()["target"] == 16
    assert not slots.acquire(timeout=0)
    for _ in range(16):
        slots.release()
    assert not slots.acquire(timeout=0)
    slots.release()
    assert slots.acquire(timeout=0)
    assert not slots.acquire(timeout=0)
    for _ in range(16):
        slots.release()
    slots.report_provider_result(status_code=429)
    assert slots.snapshot()["target"] == 8
    clock[0] += 29
    slots.report_provider_result(status_code=503)
    assert slots.snapshot()["cooldown_remaining_seconds"] == 30
    assert slots.snapshot()["decreases"] == 3
    assert slots.snapshot()["peak"] == 32 and slots.snapshot()["active"] == 0


def test_healthy_target_growth_wakes_waiting_acquire_without_canceling_existing_work(adaptive, monkeypatch):
    slots, clock = adaptive
    assert all(slots.acquire(timeout=0) for _ in range(16))
    waiting = Event()
    wait = slots._condition.wait

    def observed_wait(timeout):
        waiting.set()
        return wait(timeout)

    monkeypatch.setattr(slots._condition, "wait", observed_wait)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(slots.acquire, timeout=5)
        assert waiting.wait(2)
        assert slots.snapshot()["active"] == 16
        grow(slots, clock)
        assert future.result(timeout=2)
    assert slots.snapshot()["active"] == 17 and slots.snapshot()["target"] == 24
    for _ in range(17):
        slots.release()


@pytest.mark.parametrize("value", ["", "bad", "2", "2.5"])
def test_invalid_adaptive_configuration_does_not_initialize(adaptive, monkeypatch, value):
    slots, _ = adaptive
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", value)
    with pytest.raises(ValueError, match="VISION_OCR_ADAPTIVE must be true or false"):
        slots.acquire(timeout=0)
    assert not slots.snapshot()["initialized"]


def test_live_configuration_immutable_release_and_metrics_ignore_environment(adaptive, monkeypatch):
    slots, _ = adaptive
    assert slots.acquire(timeout=0)
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", "false")
    with pytest.raises(ValueError, match="VISION_OCR_ADAPTIVE changed; restart"):
        slots.acquire(timeout=0)
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", "true")
    monkeypatch.setenv("VISION_OCR_WORKERS", "32")
    with pytest.raises(ValueError, match="VISION_OCR_WORKERS changed; restart"):
        slots.acquire(timeout=0)
    monkeypatch.setenv("VISION_OCR_WORKERS", "bad")
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", "bad")
    slots.release()
    state = vision.vision_ocr_capacity_state()
    assert state["maximum"] == 64 and state["target"] == 16 and state["active"] == 0
    state["target"] = 64
    assert slots.snapshot()["target"] == 16


def test_shared_target_bounds_actual_mock_http_peak(adaptive, monkeypatch):
    slots, _ = adaptive
    lock, reached, release = Lock(), Event(), Event()
    active = peak = calls = 0
    original = httpx.Client

    def handler(request):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 16:
                reached.set()
        try:
            assert release.wait(10)
            return httpx.Response(200, json=payload())
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )

    def request():
        assert slots.acquire(timeout=5)
        try:
            return vision._invoke({})
        finally:
            slots.release()

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(request) for _ in range(32)]
        try:
            assert reached.wait(5)
            assert slots.snapshot()["active"] == 16
            assert not slots.acquire(timeout=0)
        finally:
            release.set()
        assert all(future.result(timeout=10) for future in futures)
    assert calls == 32 and peak == 16 and active == 0
    assert slots.snapshot()["successful_responses"] == 32
    assert slots.snapshot()["target"] == 16


@pytest.mark.parametrize(
    "response", [429, 503, "ReadTimeout", "ConnectTimeout", "bad_json", "truncated", 401]
)
def test_invoke_reports_transport_once_and_separates_quality_failures(adaptive, monkeypatch, response):
    slots, _ = adaptive
    initialize(slots)
    original, calls = httpx.Client, []

    def handler(request):
        calls.append(request)
        if response in {"ReadTimeout", "ConnectTimeout"}:
            raise getattr(httpx, response)("mock timeout", request=request)
        if response == "bad_json":
            return httpx.Response(200, content=b"invalid json")
        if response == "truncated":
            return httpx.Response(200, json=payload(finish="length"))
        return httpx.Response(response)

    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    with pytest.raises(vision.OCRResponseError):
        vision._invoke({})
    state = slots.snapshot()
    assert len(calls) == 1
    failed = response in {429, 503, "ReadTimeout", "ConnectTimeout"}
    assert state["target"] == (8 if failed else 16)
    assert state["capacity_failures"] == int(failed)
    assert state["successful_responses"] == int(response in {"bad_json", "truncated"})


def test_partial_http_200_body_timeout_is_failure_and_never_counted_as_healthy(adaptive, monkeypatch):
    slots, _ = adaptive
    initialize(slots)
    original, calls = httpx.Client, []

    class FailingBody(httpx.SyncByteStream):
        def __iter__(self):
            yield b'{"choices":'
            raise httpx.ReadTimeout("partial-body timeout")

    def handler(request):
        calls.append(request)
        return httpx.Response(200, stream=FailingBody())

    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    with pytest.raises(vision.OCRResponseError) as error:
        vision._invoke({})
    state = slots.snapshot()
    assert not error.value.retryable and error.value.request_outcome_unknown and len(calls) == 1
    assert state["target"] == 8 and state["capacity_failures"] == 1
    assert state["successful_responses"] == 0


def test_pdf_image_and_recovery_share_drop_and_defer_without_attempt(adaptive, monkeypatch):
    slots, _ = adaptive
    assert all(slots.acquire(timeout=0) for _ in range(16))
    slots.report_provider_result(status_code=503)
    acquire = slots.acquire
    monkeypatch.setattr(slots, "acquire", lambda **_: acquire(timeout=0))
    monkeypatch.setattr(vision, "_invoke", lambda *_: pytest.fail("occupied pool called provider"))
    monkeypatch.setattr(ocr_recovery, "recover_page_step", lambda *_args, **_kw: pytest.fail("recovery ran"))
    content = make_pdf([("Visible source", True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    recovering = deepcopy(page)
    recovering.update(status="ocr_uncertain", ocr={"phase": "recovery", "attempts": 2})
    image_content, manifest = docx()
    results = [
        parsing._ocr_one_page(content, page),
        parsing._ocr_one_page(content, recovering, on_region=lambda _: {}),
        native_asset_ocr._one(
            image_content, manifest, native_asset_ocr.pending_assets(manifest)[0], None, None
        ),
    ]
    assert all(result["deferred_reason"] == "ocr_capacity" for result in results)
    assert not results[0].get("ocr") and not results[2].get("ocr")
    assert results[1]["ocr"] == recovering["ocr"]
    for _ in range(16):
        slots.release()
    assert slots.snapshot()["active"] == 0


def test_uninitialized_feedback_and_getter_do_not_freeze_configuration(adaptive, monkeypatch):
    slots, _ = adaptive
    slots.report_provider_result(status_code=503)
    assert not vision.vision_ocr_capacity_state()["initialized"]
    monkeypatch.setenv("VISION_OCR_WORKERS", "32")
    initialize(slots)
    assert slots.snapshot()["maximum"] == 32 and slots.snapshot()["capacity_failures"] == 0
