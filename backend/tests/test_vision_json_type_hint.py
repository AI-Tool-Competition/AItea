"""A known provider format hint cannot relax the page transcription contract."""

import base64
import json

import pytest

from app import parsing, vision
from app.ingestion import parse_file
from test_ingestion_quality import make_pdf
from test_vision_ocr import fake_provider, payload


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "test-vision-model")


def hinted_response(**changes):
    response = payload(text="Pasal 1\nTeks lengkap.")
    value = json.loads(response["choices"][0]["message"]["content"])
    value.update(type="json_object", **changes)
    response["choices"][0]["message"]["content"] = json.dumps(value)
    return response


def test_exact_top_level_format_hint_preserves_page_reading(monkeypatch):
    response = hinted_response()
    requests = fake_provider(monkeypatch, response)
    result = vision.transcribe_page({"image_base64": "eA=="})
    assert len(requests) == 1
    assert result["text"] == "Pasal 1\nTeks lengkap."
    assert result["printed_page"] == "11"
    assert result["complete"] is True and result["uncertain_spans"] == []
    assert result["finish_reason"] == "stop" and result["review_status"] == "unreviewed"
    assert result["model"] == "provider-model-revision"


@pytest.mark.parametrize(
    "change",
    [
        {"type": "object"},
        {"type": "JSON_OBJECT"},
        {"type": None},
        {"type": {"value": "json_object"}},
        {"extra": "ignored would be unsafe"},
        {"printed_page": 11},
        {"complete": "true"},
        {"uncertain_spans": "none"},
        {"is_blank": 0},
    ],
)
def test_hint_does_not_accept_unknown_fields_or_invalid_types(monkeypatch, change):
    response = hinted_response()
    value = json.loads(response["choices"][0]["message"]["content"])
    value.update(change)
    raw = json.dumps(value)
    response["choices"][0]["message"]["content"] = raw
    fake_provider(monkeypatch, response)
    with pytest.raises(vision.OCRResponseError) as caught:
        vision.transcribe_page({"image_base64": "eA=="})
    evidence = caught.value.response_evidence
    assert evidence["error_code"] == "invalid_transcription_schema"
    assert base64.b64decode(evidence["response_base64"]) == raw.encode()


@pytest.mark.parametrize("wrapped", [False, True])
def test_hint_does_not_supply_missing_fields_or_unwrap_payloads(monkeypatch, wrapped):
    response = hinted_response()
    value = json.loads(response["choices"][0]["message"]["content"])
    if wrapped:
        value = {"type": "json_object", "content": value}
    else:
        value.pop("printed_page")
    response["choices"][0]["message"]["content"] = json.dumps(value)
    fake_provider(monkeypatch, response)
    with pytest.raises(vision.OCRResponseError, match="结构"):
        vision.transcribe_page({"image_base64": "eA=="})


def test_hint_preserves_incomplete_and_uncertain_flags(monkeypatch):
    fake_provider(monkeypatch, hinted_response(complete=False, uncertain_spans=["stamp unreadable"]))
    result = vision.transcribe_page({"image_base64": "eA=="})
    assert result["complete"] is False
    assert result["uncertain_spans"] == ["stamp unreadable", "model_reported_incomplete"]


def test_hint_preserves_original_error_bytes_for_overlong_text(monkeypatch):
    response = hinted_response(text="x" * (vision.MAX_PAGE_TEXT + 1))
    raw = response["choices"][0]["message"]["content"]
    fake_provider(monkeypatch, response)
    with pytest.raises(vision.OCRResponseError) as caught:
        vision.transcribe_page({"image_base64": "eA=="})
    assert base64.b64decode(caught.value.response_evidence["response_base64"]) == raw.encode()


def test_hint_does_not_bypass_legal_number_validation_in_bounded_pipeline(monkeypatch):
    fake_provider(monkeypatch, hinted_response(text="NOMOR 19 TAHUN 2023"))
    content = make_pdf([("NOMOR 19 TAHUN 2025", True)])
    manifest = parse_file("scan.pdf", content)
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", manifest)
    page = result["pages"][0]
    assert page["status"] == "ocr_uncertain"
    assert "native_ocr_law_number_year_mismatch" in page["ocr"]["uncertain_spans"]
    assert not result["quality"]["complete"]
