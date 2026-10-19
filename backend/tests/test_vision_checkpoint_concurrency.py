"""Real bounded PDF rendering; provider and operator failures are simulated."""

import base64
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier, Event, Lock

import httpx
import pytest

from app import parsing, vision
from app.ingestion import parse_file
from test_ingestion_quality import make_pdf
from test_vision_ocr import payload


@pytest.fixture(params=[False, True], ids=["fixed64", "adaptive64"])
def maximum_capacity(monkeypatch, request):
    adaptive = request.param
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("VISION_OCR_WORKERS", "64")
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", str(adaptive))
    monkeypatch.setattr(parsing, "_SLOTS", parsing._ParserSlots())
    clock = [0.0]
    slots = parsing._VisionSlots(clock=lambda: clock[0])
    monkeypatch.setattr(parsing, "_OCR_SLOTS", slots)
    assert slots.acquire(timeout=0)
    slots.release()
    while slots.snapshot()["target"] < 64:
        # Simulated healthy responses and time grow the adaptive target. Actual
        # HTTP overlap below uses MockTransport, never provider throughput.
        clock[0] += 30
        for _ in range(max(16, slots.snapshot()["target"])):
            slots.report_provider_result(status_code=200)
    return slots, adaptive


@pytest.mark.parametrize("failure_type", [KeyboardInterrupt, RuntimeError])
def test_interrupted_batch_keeps_successful_inflight_page_and_returns_all_slots(
    monkeypatch, maximum_capacity, failure_type
):
    slots, _ = maximum_capacity

    content = make_pdf([("First visible source", True), ("Second visible source", True)])
    saved = parse_file("scan.pdf", content)
    barrier, shutdown_entered, lock = Barrier(2, timeout=10), Event(), Lock()
    requests, images = [], {}
    interrupted = True

    class ObservedExecutor(ThreadPoolExecutor):
        def shutdown(self, *args, **kwargs):
            shutdown_entered.set()
            return super().shutdown(*args, **kwargs)

    monkeypatch.setattr(parsing, "ThreadPoolExecutor", ObservedExecutor)
    original_client = httpx.Client

    def provider(request):
        body = json.loads(request.content)
        image_url = body["messages"][1]["content"][0]["image_url"]["url"]
        image_hash = hashlib.sha256(base64.b64decode(image_url.split(",", 1)[1])).hexdigest()
        with lock:
            page_number = images[image_hash]
            hint = ["First visible source", "Second visible source"][page_number - 1]
            requests.append(hint)
        if interrupted:
            barrier.wait()
            if hint == "First visible source":
                raise failure_type("simulated interrupted page")
            assert shutdown_entered.wait(10), "successful sibling must finish during batch shutdown"
        return httpx.Response(200, json=payload(text=hint))

    monkeypatch.setattr(
        vision.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs),
    )

    def checkpoint(page):
        with lock:
            saved["pages"][page["page"] - 1] = deepcopy(page)

    def save_image(rendered):
        with lock:
            images[rendered["image_sha256"]] = rendered["page"]
        return {"content_hash": rendered["image_sha256"]}

    with pytest.raises(failure_type, match="simulated interrupted page"):
        parsing.ocr_bounded(
            "scan.pdf", content, "application/pdf", saved, on_page=checkpoint, on_image=save_image
        )
    assert slots.snapshot()["active"] == 0
    assert not any(thread.name.startswith("page-ocr") for thread in threading.enumerate())
    assert saved["pages"][0]["status"] == "pending_ocr"
    assert saved["pages"][0]["ocr"]["attempts"] == 1
    assert saved["pages"][1]["status"] == "ocr_complete"
    completed = deepcopy(saved["pages"][1])
    assert len(requests) == 2
    assert all(slots.acquire(timeout=0) for _ in range(64))
    assert not slots.acquire(timeout=0)
    for _ in range(64):
        slots.release()

    interrupted = False
    result = parsing.ocr_bounded(
        "scan.pdf", content, "application/pdf", saved, on_page=checkpoint, on_image=save_image
    )
    assert not result["quality"]["complete"]
    assert result["pages"][0]["ocr"]["request_outcome"] == "unknown_after_interruption"
    assert result["pages"][1] == completed
    assert requests.count("Second visible source") == 1
    assert requests.count("First visible source") == 1
    assert slots.snapshot()["active"] == 0


def test_full_64_http_wave_drains_rate_limit_and_timeout_without_new_requests(monkeypatch, maximum_capacity):
    slots, adaptive = maximum_capacity
    lock, reached = Lock(), Event()
    rate_release, timeout_release, success_release = Event(), Event(), Event()
    rate_done, timeout_done = Event(), Event()
    active = peak = calls = 0
    original_client = httpx.Client

    def provider(request):
        nonlocal active, peak, calls
        index = json.loads(request.content)["test_index"]
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 64:
                reached.set()
        try:
            if index == 0:
                assert rate_release.wait(10)
                return httpx.Response(429)
            if index == 1:
                assert timeout_release.wait(10)
                raise httpx.ReadTimeout("simulated provider timeout", request=request)
            assert success_release.wait(10)
            return httpx.Response(200, json=payload())
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(
        vision.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs),
    )

    def invoke(index):
        assert slots.acquire(timeout=5)
        try:
            return vision._invoke({"test_index": index})
        except vision.OCRResponseError as error:
            return error
        finally:
            slots.release()
            if index == 0:
                rate_done.set()
            elif index == 1:
                timeout_done.set()

    with ThreadPoolExecutor(max_workers=64) as executor:
        futures = [executor.submit(invoke, index) for index in range(64)]
        try:
            assert reached.wait(10)
            assert slots.snapshot()["active"] == peak == calls == 64
            assert not slots.acquire(timeout=0)
            rate_release.set()
            assert rate_done.wait(5)
            assert slots.snapshot()["target"] == (32 if adaptive else 64)
            timeout_release.set()
            assert timeout_done.wait(5)
            state = slots.snapshot()
            assert state["target"] == (16 if adaptive else 64)
            assert state["active"] == 62
            assert state["capacity_failures"] == 2
            if adaptive:
                assert not slots.acquire(timeout=0)
                assert state["cooldown_remaining_seconds"] == 30
            else:
                # A fixed capacity still respects the provider's minimum wait.
                assert state["provider_wait_remaining_seconds"] == 30
                assert not slots.acquire(timeout=0)
        finally:
            rate_release.set()
            timeout_release.set()
            success_release.set()
        results = [future.result(timeout=10) for future in futures]
    assert all(isinstance(result, vision.OCRResponseError) for result in results[:2])
    assert results[0].retryable and not results[1].retryable
    assert results[1].request_outcome_unknown
    assert all(isinstance(result, tuple) for result in results[2:])
    assert active == slots.snapshot()["active"] == 0
    assert calls == peak == slots.snapshot()["peak"] == 64
    assert slots.snapshot()["target"] == (16 if adaptive else 64)


@pytest.mark.parametrize("failure", ["lease expired", "checkpoint unavailable"])
def test_terminal_checkpoint_rejection_preserves_attempt_and_returns_capacity(
    monkeypatch, maximum_capacity, failure
):
    slots, _ = maximum_capacity
    content = make_pdf([("Visible source", True), ("Visible source", True)])
    saved = parse_file("scan.pdf", content)
    barrier, lock = Barrier(2, timeout=10), Lock()
    original_client = httpx.Client
    responses = []
    rejected = []

    def provider(request):
        barrier.wait()
        with lock:
            responses.append(True)
        return httpx.Response(200, json=payload(text="Visible source"))

    monkeypatch.setattr(
        vision.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs),
    )

    def checkpoint(page):
        with lock:
            if page["status"] == "ocr_complete":
                rejected.append(page["page"])
                raise ValueError(failure)
            saved["pages"][page["page"] - 1] = deepcopy(page)

    with pytest.raises(ValueError, match=failure):
        parsing.ocr_bounded("scan.pdf", content, "application/pdf", saved, on_page=checkpoint)
    assert len(responses) == 2 and sorted(rejected) == [1, 2]
    assert all(page["status"] == "pending_ocr" and page["ocr"]["attempts"] == 1 for page in saved["pages"])
    assert slots.snapshot()["active"] == 0
    assert not any(thread.name.startswith("page-ocr") for thread in threading.enumerate())
