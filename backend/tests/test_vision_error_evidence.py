"""Failed paid responses remain bounded, isolated diagnostics, never searchable text."""

from __future__ import annotations

import base64
import hashlib
import json
from copy import deepcopy

import httpx
import pytest

from app import parsing, vision
from app.blob_store import BlobStore
from app.ingestion import parse_file
from test_ingestion_quality import make_pdf
from test_vision_ocr import fake_provider, payload


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(parsing, "_OCR_SLOTS", parsing._VisionSlots())
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "private-diagnostic-test-key")
    monkeypatch.setenv("LLM_MODEL", "test-vision-model")


def raw_provider(monkeypatch, content, *, status=200):
    original = httpx.Client
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, content=content)

    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    return requests


def save_callback(tmp_path, owner, evidence):
    blobs = BlobStore(tmp_path / "files", max_bytes=2 * vision.MAX_ERROR_RESPONSE_BYTES)

    def save(value):
        body = base64.b64decode(value["response_base64"], validate=True)
        assert len(body) == value["size"] <= vision.MAX_ERROR_RESPONSE_BYTES
        assert hashlib.sha256(body).hexdigest() == value["content_hash"]
        packet = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        blob = blobs.put_bytes(owner, packet)
        evidence.append(deepcopy(value))
        return {
            "content_hash": blob.digest,
            "size": blob.size,
            "storage_path": str(blob.path.relative_to(tmp_path)),
            "mime_type": "application/json",
        }

    return save


def recognize(content, **kwargs):
    return parsing.ocr_bounded(
        "scan.pdf", content, "application/pdf", parse_file("scan.pdf", content), **kwargs
    )


@pytest.mark.parametrize(
    "finish,raw",
    [("stop", '```json\n{"text":"PAID_INVALID_JSON",\n```'), ("length", '{"text":"PAID_TRUNCATED')],
)
def test_invalid_and_truncated_response_is_saved_with_request_and_physical_page(
    enabled, monkeypatch, tmp_path, finish, raw
):
    response = payload(finish=finish)
    response["choices"][0]["message"]["content"] = raw
    requests = fake_provider(monkeypatch, response)
    content = make_pdf([("preserved native", True)])
    evidence, checkpoints = [], []
    callback = save_callback(tmp_path, "owner-a", evidence)
    result = recognize(content, on_page=checkpoints.append, on_response_error=callback)
    assert len(requests) == len(evidence) == 1
    item = evidence[0]
    page = result["pages"][0]
    record = page["ocr"]["error_response"]
    assert base64.b64decode(item["response_base64"]) == raw.encode()
    assert item["payload_kind"] == "assistant_message"
    assert item["source_hash"] == hashlib.sha256(content).hexdigest()
    assert item["page"] == 1 and item["attempt"] == 1
    assert item["request_id"] == checkpoints[0]["ocr"]["request_id"]
    assert len(item["request_id"]) == 32
    assert (
        item["request_sha256"]
        == hashlib.sha256(
            json.dumps(requests[0], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )
    assert item["image_sha256"] == page["ocr"]["image_sha256"]
    assert "error_response" not in checkpoints[0]["ocr"]
    assert record == checkpoints[-1]["ocr"]["error_response"]
    saved = json.loads((tmp_path / record["blob"]["storage_path"]).read_bytes())
    assert saved == item
    assert base64.b64decode(saved["response_base64"]) == raw.encode()
    assert record["blob"]["content_hash"] != record["content_hash"]
    assert page["status"] == "ocr_failed" and page["text"] == "preserved native"
    assert page["ocr"]["finish_reason"] == finish
    assert not result["quality"]["complete"]
    assert "PAID_" not in json.dumps(result)
    assert "response_base64" not in json.dumps(result)
    parsing.ocr_bounded("scan.pdf", content, "application/pdf", result, on_response_error=callback)
    assert len(requests) == len(evidence) == 1


def test_overlong_transcription_keeps_only_explicitly_truncated_diagnostic_prefix(
    enabled, monkeypatch, tmp_path
):
    response = payload(text="x" * (vision.MAX_ERROR_RESPONSE_BYTES + 100))
    raw = response["choices"][0]["message"]["content"].encode()
    requests = fake_provider(monkeypatch, response)
    evidence = []
    result = recognize(
        make_pdf([("native", True)]), on_response_error=save_callback(tmp_path, "owner", evidence)
    )
    item = evidence[0]
    assert len(requests) == 1
    assert item["error_code"] == "invalid_transcription_schema"
    assert item["size"] == vision.MAX_ERROR_RESPONSE_BYTES
    assert item["observed_bytes"] == len(raw)
    assert item["capture_truncated"] and item["response_complete"]
    assert base64.b64decode(item["response_base64"]) == raw[: vision.MAX_ERROR_RESPONSE_BYTES]
    assert not result["quality"]["complete"]
    assert result["pages"][0]["status"] == "ocr_failed"


@pytest.mark.parametrize("oversized", [False, True])
def test_malformed_provider_json_or_oversized_successful_response_retains_bounded_bytes(
    enabled, monkeypatch, tmp_path, oversized
):
    raw = b'{"choices":' + (b"x" * vision.MAX_RESPONSE_BYTES if oversized else b"")
    requests = raw_provider(monkeypatch, raw)
    evidence = []
    result = recognize(
        make_pdf([("native", True)]), on_response_error=save_callback(tmp_path, "owner", evidence)
    )
    item = evidence[0]
    assert len(requests) == 1
    assert item["payload_kind"] == "provider_response"
    assert item["error_code"] == ("response_limit" if oversized else "invalid_provider_json")
    assert item["response_complete"] is not oversized
    assert item["capture_truncated"] is oversized
    assert base64.b64decode(item["response_base64"]) == raw[: vision.MAX_ERROR_RESPONSE_BYTES]
    assert result["pages"][0]["status"] == "ocr_failed"
    assert not result["quality"]["complete"]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_http_error_bodies_and_authorization_are_not_diagnostic_evidence(enabled, monkeypatch, status):
    raw = b"Authorization: Bearer private-diagnostic-test-key\nPRIVATE_HTTP_ERROR_BODY"
    requests = raw_provider(monkeypatch, raw, status=status)
    result = recognize(
        make_pdf([("native", True)]),
        on_response_error=lambda _: pytest.fail("HTTP error body was passed to storage"),
    )
    assert len(requests) == 1
    serialized = json.dumps(result)
    for secret in ("private-diagnostic-test-key", "PRIVATE_HTTP_ERROR_BODY", "Bearer ", "Authorization"):
        assert secret not in serialized
    assert "error_response" not in result["pages"][0]["ocr"]
    assert not result["quality"]["complete"]


def test_credential_echo_in_successful_response_is_not_persisted(enabled, monkeypatch):
    response = payload()
    response["choices"][0]["message"]["content"] = "not JSON private-diagnostic-test-key"
    fake_provider(monkeypatch, response)
    result = recognize(
        make_pdf([("native", True)]),
        on_response_error=lambda _: pytest.fail("credential echo was passed to storage"),
    )
    assert "private-diagnostic-test-key" not in json.dumps(result)
    assert result["pages"][0]["status"] == "ocr_failed"


def test_empty_successful_http_response_retains_nonempty_bound_diagnostic_packet(
    enabled, monkeypatch, tmp_path
):
    requests = raw_provider(monkeypatch, b"")
    evidence = []
    result = recognize(
        make_pdf([("native", True)]), on_response_error=save_callback(tmp_path, "owner", evidence)
    )
    assert len(requests) == len(evidence) == 1
    assert evidence[0]["size"] == 0 and evidence[0]["response_base64"] == ""
    record = result["pages"][0]["ocr"]["error_response"]
    assert record["blob"]["size"] > 0
    saved = json.loads((tmp_path / record["blob"]["storage_path"]).read_bytes())
    assert saved["request_id"] == record["request_id"]
    assert saved["source_hash"] == result["source_hash"]
    assert result["pages"][0]["status"] == "ocr_failed"


def test_success_does_not_write_error_response_package(enabled, monkeypatch):
    fake_provider(monkeypatch, payload(text="Pasal 1\nValid text."))
    result = recognize(
        make_pdf([("Pasal 1\nValid text.", True)]),
        on_response_error=lambda _: pytest.fail("successful response was duplicated"),
    )
    assert result["pages"][0]["status"] == "ocr_complete"
    assert "error_response" not in result["pages"][0]["ocr"]


def test_diagnostic_storage_failure_propagates_after_paid_call_without_second_request(enabled, monkeypatch):
    response = payload()
    response["choices"][0]["message"]["content"] = "invalid json"
    requests = fake_provider(monkeypatch, response)
    checkpoints = []

    def fail(_evidence):
        raise ValueError("diagnostic CAS unavailable")

    with pytest.raises(ValueError, match="diagnostic CAS unavailable"):
        recognize(make_pdf([("native", True)]), on_page=checkpoints.append, on_response_error=fail)
    assert len(requests) == len(checkpoints) == 1
    assert checkpoints[0]["ocr"]["attempts"] == 1
    assert checkpoints[0]["ocr"]["request_id"]


def test_identical_error_payload_has_separate_owner_cas_and_distinct_request_identity(
    enabled, monkeypatch, tmp_path
):
    response = payload()
    response["choices"][0]["message"]["content"] = "PAID_INVALID_PRIVATE_RESPONSE"
    requests = fake_provider(monkeypatch, response)
    content = make_pdf([("native", True)])
    evidence_a, evidence_b = [], []
    a = recognize(content, on_response_error=save_callback(tmp_path, "owner-a", evidence_a))
    b = recognize(content, on_response_error=save_callback(tmp_path, "owner-b", evidence_b))
    ra = a["pages"][0]["ocr"]["error_response"]
    rb = b["pages"][0]["ocr"]["error_response"]
    assert len(requests) == 2
    assert ra["content_hash"] == rb["content_hash"]
    assert ra["request_id"] != rb["request_id"]
    assert ra["blob"]["storage_path"] != rb["blob"]["storage_path"]
    assert hashlib.sha256(b"owner-a").hexdigest() in ra["blob"]["storage_path"]
    assert hashlib.sha256(b"owner-b").hexdigest() in rb["blob"]["storage_path"]
    assert "PAID_INVALID_PRIVATE_RESPONSE" not in json.dumps(a) + json.dumps(b)
