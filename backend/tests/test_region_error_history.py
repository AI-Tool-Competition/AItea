"""Crop failures keep the exact request, body and durable retry fence."""

import base64
from copy import deepcopy
import json

import pytest

from app import ocr_recovery, vision
from app.ocr_error_evidence import persist_response_evidence, validate_response_binding
from app.blob_store import BlobStore
from test_ocr_recovery import uncertain_page
from test_vision_ocr import fake_provider, payload


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-region-key")
    monkeypatch.setenv("LLM_MODEL", "test-region-model")


def test_region_body_binding_wait_and_history_survive_retry(enabled, monkeypatch, tmp_path):
    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023")
    clock, calls, checkpoints, evidence = [100.0], [], [], []
    monkeypatch.setattr(ocr_recovery.time, "time", lambda: clock[0])
    blobs = BlobStore(tmp_path / "files", max_bytes=2 * vision.MAX_ERROR_RESPONSE_BYTES)

    def read(_):
        calls.append(True)
        if len(calls) == 1:
            raise vision._response_error(
                "busy",
                {},
                b"busy response",
                error_code="http_429",
                payload_kind="provider_http_error",
                retryable=True,
                retry_after=75,
                http_status=429,
            )
        return {"text": "NOMOR 19 TAHUN 2025", "uncertain_spans": [], "graphic_marks": []}

    def save(value):
        evidence.append(deepcopy(value))
        return persist_response_evidence(
            blobs, "owner", value, source_hash=page["source_hash"], request_page=checkpoints[-1]
        )

    monkeypatch.setattr(vision, "transcribe_region", read)
    options = {
        "on_region": lambda r: {"content_hash": r["image_sha256"]},
        "on_page": checkpoints.append,
        "on_response_error": save,
    }
    failed = ocr_recovery.recover_page_step(data, page, **options)
    region = failed["ocr"]["recovery"]["regions"][0]
    assert evidence[0]["request_kind"] == "region"
    assert evidence[0]["request_id"] == region["request_id"]
    assert base64.b64decode(evidence[0]["response_base64"]) == b"busy response"
    assert failed["ocr"]["recovery"]["retry_after_seconds"] == 75
    for key, value in (
        ("region_id", "wrong"),
        ("pass_number", 1),
        ("region_bbox", [0, 0, 1, 1]),
        ("request_id", "wrong"),
    ):
        with pytest.raises(ValueError):
            validate_response_binding({**evidence[0], key: value}, page["source_hash"], checkpoints[-1])
    deferred = ocr_recovery.recover_page_step(data, failed, **options)
    assert len(calls) == 1
    assert deferred["ocr"]["recovery"]["regions"][0] == region
    clock[0] += 75
    retried = ocr_recovery.recover_page_step(data, deferred, **options)
    updated = retried["ocr"]["recovery"]["regions"][0]
    assert len(calls) == 2 and updated["attempts"] == 2
    assert updated["attempt_history"][0]["previous_region"] == region
    assert updated["request_id"] != region["request_id"]
    assert "error_response" not in updated
    assert (
        json.loads((tmp_path / region["error_response"]["blob"]["storage_path"]).read_text()) == evidence[0]
    )


@pytest.mark.parametrize("raw", ["not json", '{"text":"missing required fields"}'])
def test_region_invalid_payload_retains_raw_response(enabled, monkeypatch, raw):
    response = payload()
    response["choices"][0]["message"]["content"] = raw
    fake_provider(monkeypatch, response)
    with pytest.raises(vision.OCRResponseError) as caught:
        vision.transcribe_region({"image_base64": "AAAA"})
    assert base64.b64decode(caught.value.response_evidence["response_base64"]) == raw.encode()


@pytest.mark.parametrize(
    "damage", [None, "outer_key", "inner_key", "outer_type", "inner_type", "content_type"]
)
def test_exact_nested_page_wrapper_preserves_strict_schema(enabled, monkeypatch, damage):
    response = payload()
    inner = json.loads(response["choices"][0]["message"]["content"])
    wrapped = {"type": "json_object", "content": inner}
    if damage == "outer_key":
        wrapped["unknown"] = True
    elif damage == "inner_key":
        inner["unknown"] = True
    elif damage == "outer_type":
        wrapped["type"] = "anything"
    elif damage == "inner_type":
        inner["complete"] = "true"
    elif damage == "content_type":
        wrapped["content"] = json.dumps(inner)
    raw = json.dumps(wrapped)
    response["choices"][0]["message"]["content"] = raw
    fake_provider(monkeypatch, response)
    if damage:
        with pytest.raises(vision.OCRResponseError) as caught:
            vision.transcribe_page({"image_base64": "AAAA", "native_text": ""})
        assert base64.b64decode(caught.value.response_evidence["response_base64"]) == raw.encode()
    else:
        result = vision.transcribe_page({"image_base64": "AAAA", "native_text": ""})
        assert result["text"] == inner["text"] and result["complete"] is True
