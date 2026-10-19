"""Provider backoff survives restart without dropping paid request evidence."""

import base64
from copy import deepcopy
import hashlib
import json

import httpx
import pytest

from app import parsing, vision
from app.ingestion import parse_file
from test_ingestion_quality import make_pdf
from test_vision_error_evidence import save_callback
from test_vision_ocr import payload


def test_restart_can_resume_last_reduced_capacity(configured, monkeypatch):
    monkeypatch.setenv("VISION_OCR_WORKERS", "4")
    slots = parsing._VisionSlots(initial_target=1)
    assert slots.acquire(timeout=0)
    assert not slots.acquire(timeout=0)
    assert slots.snapshot()["maximum"] == 4 and slots.snapshot()["target"] == 1
    slots.release()
    with pytest.raises(ValueError):
        parsing._VisionSlots(initial_target=5).acquire(timeout=0)


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "LLM_BASE_URL": "https://provider.invalid/v1",
        "LLM_API_KEY": "private-ocr-test-key",
        "LLM_MODEL": "test-vision",
        "VISION_OCR_ENABLED": "true",
        "VISION_OCR_WORKERS": "2",
        "VISION_OCR_ADAPTIVE": "true",
    }.items():
        monkeypatch.setenv(key, value)
    clock = [100.0]
    slots = parsing._VisionSlots(clock=lambda: clock[0])
    monkeypatch.setattr(parsing, "_OCR_SLOTS", slots)
    monkeypatch.setattr(parsing.time, "time", lambda: clock[0])
    return clock, slots


def provider(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )


@pytest.mark.parametrize("wait", ["75", "9999"])
def test_http_limit_preserves_bounded_body_and_full_retry_after(configured, monkeypatch, wait):
    clock, slots = configured
    assert slots.acquire(timeout=0)
    slots.release()
    raw = b'{"error":"provider busy"}'
    provider(
        monkeypatch,
        lambda _: httpx.Response(
            429, content=raw, headers={"Retry-After": wait, "x-secret": "not persisted"}
        ),
    )
    with pytest.raises(vision.OCRResponseError) as caught:
        vision._invoke({"messages": []})
    error = caught.value
    assert error.retryable and error.retry_after == float(wait) and error.http_status == 429
    assert base64.b64decode(error.response_evidence["response_base64"]) == raw
    assert error.response_evidence["http_status"] == 429
    assert "not persisted" not in json.dumps(error.response_evidence)
    assert slots.snapshot()["target"] == slots.snapshot()["floor"] == 1
    assert not slots.acquire(timeout=0)
    clock[0] += float(wait)
    assert slots.acquire(timeout=0)
    assert not slots.acquire(timeout=0)
    slots.release()


def test_error_retry_after_date_reuses_existing_rfc_parser(configured, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    delay = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=180), usegmt=True)
    provider(monkeypatch, lambda _: httpx.Response(503, content=b"busy", headers={"Retry-After": delay}))
    with pytest.raises(vision.OCRResponseError) as caught:
        vision._invoke({})
    assert 178 <= caught.value.retry_after <= 181
    assert caught.value.response_evidence["retry_after_seconds"] == caught.value.retry_after


def test_retry_delay_and_previous_failure_survive_page_checkpoint_restart(configured, monkeypatch, tmp_path):
    clock, slots = configured
    calls = []

    def respond(request):
        calls.append(request)
        return (
            httpx.Response(429, content=b'{"error":"rate limited"}', headers={"Retry-After": "75"})
            if len(calls) == 1
            else httpx.Response(200, json=payload(text="Pasal 1\nValid source."))
        )

    provider(monkeypatch, respond)
    content = make_pdf([("Pasal 1\nValid source.", True)])
    evidence, checkpoints = [], []
    callback = save_callback(tmp_path, "owner", evidence)
    initial = parse_file("scan.pdf", content)
    result = parsing.ocr_bounded(
        "scan.pdf",
        content,
        "application/pdf",
        initial,
        on_page=checkpoints.append,
        on_response_error=callback,
    )
    failed = deepcopy(result["pages"][0])
    assert failed["ocr"]["retry_not_before"] == 175
    assert result["quality"]["retry_after_seconds"] == 75
    assert len(calls) == len(evidence) == 1
    assert failed["ocr"]["error_response"]["http_status"] == 429
    # A restarted process has no in-memory cooldown; the durable page still fences it.
    monkeypatch.setattr(parsing, "_OCR_SLOTS", parsing._VisionSlots(clock=lambda: clock[0]))
    paused = parsing.ocr_bounded(
        "scan.pdf", content, "application/pdf", result, on_page=checkpoints.append, on_response_error=callback
    )
    assert len(calls) == 1 and paused["pages"][0] == failed
    clock[0] = 176
    completed = parsing.ocr_bounded(
        "scan.pdf", content, "application/pdf", paused, on_page=checkpoints.append, on_response_error=callback
    )
    page = completed["pages"][0]
    assert page["status"] == "ocr_complete" and page["ocr"]["attempts"] == 2
    assert len(calls) == 2 and completed["quality"]["complete"]
    history = page["ocr_attempt_history"]
    assert len(history) == 1 and history[0]["previous_page"] == failed
    raw = json.dumps(failed, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert history[0]["previous_page_sha256"] == hashlib.sha256(raw).hexdigest()
    saved = json.loads((tmp_path / failed["ocr"]["error_response"]["blob"]["storage_path"]).read_text())
    assert saved == evidence[0]
    parsing.ocr_bounded(
        "scan.pdf",
        content,
        "application/pdf",
        completed,
        on_page=checkpoints.append,
        on_response_error=callback,
    )
    assert len(calls) == 2


@pytest.mark.parametrize("saved_transport_evidence", [False, True])
def test_interrupted_request_intent_cannot_automatically_issue_a_second_call(
    configured, monkeypatch, saved_transport_evidence
):
    content = make_pdf([("Original", True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    page["ocr"] = {
        "attempts": 1,
        "request_id": "persisted-before-interruption",
        "source_hash": page["source_hash"],
        "page": 1,
    }
    if saved_transport_evidence:
        page["ocr"]["error_response"] = {
            "request_outcome": "unknown_transport_result",
            "blob": {"content_hash": "old"},
        }
    previous = deepcopy(page)
    monkeypatch.setattr(vision, "transcribe_page", lambda *_: pytest.fail("unknown paid request resent"))
    monkeypatch.setattr(
        parsing, "render_pdf_page_bounded", lambda *_: pytest.fail("unknown request rerendered")
    )
    result = parsing._ocr_one_page(content, page, None)
    assert result["status"] == "ocr_failed"
    assert result["ocr"]["request_outcome"] == "unknown_after_interruption"
    assert result["ocr"]["request_id"] == previous["ocr"]["request_id"]
    assert result["ocr"]["attempts"] == 1
    assert result["ocr_attempt_history"][0]["previous_page"] == previous


def test_retry_render_failure_retains_previous_request_and_error_reference(configured, monkeypatch):
    content = make_pdf([("Original", True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    page["ocr"] = {
        "attempts": 1,
        "request_id": "old-request",
        "error_response": {"blob": {"content_hash": "old"}},
    }
    page["error"] = "original provider failure"
    previous = deepcopy(page)
    monkeypatch.setattr(
        parsing, "render_pdf_page_bounded", lambda *_: (_ for _ in ()).throw(ValueError("render failed"))
    )
    monkeypatch.setattr(vision, "transcribe_page", lambda _: pytest.fail("render failure called provider"))
    result = parsing._ocr_one_page(content, page)
    assert result["ocr_attempt_history"][0]["previous_page"] == previous
    assert result["ocr"]["attempts"] == 1 and result["status"] == "ocr_failed"
