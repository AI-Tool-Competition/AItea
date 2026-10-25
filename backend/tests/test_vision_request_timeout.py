import pytest

from app.contexts.documents.infrastructure.providers import vision


def test_vision_request_timeout_defaults_to_180_seconds(monkeypatch):
    monkeypatch.delenv("VISION_OCR_REQUEST_TIMEOUT_SECONDS", raising=False)

    assert vision.vision_ocr_request_timeout() == 180


@pytest.mark.parametrize("value", ["10", "180", "600"])
def test_vision_request_timeout_accepts_configured_bounds(monkeypatch, value):
    monkeypatch.setenv("VISION_OCR_REQUEST_TIMEOUT_SECONDS", value)

    assert vision.vision_ocr_request_timeout() == int(value)


@pytest.mark.parametrize("value", ["", "fast", "9", "601"])
def test_vision_request_timeout_rejects_invalid_values(monkeypatch, value):
    monkeypatch.setenv("VISION_OCR_REQUEST_TIMEOUT_SECONDS", value)

    with pytest.raises(ValueError, match="VISION_OCR_REQUEST_TIMEOUT_SECONDS"):
        vision.vision_ocr_request_timeout()


def test_deepseek_ocr_requests_disable_thinking_to_reserve_output_for_transcription(monkeypatch):
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://aerisun.top/sub2api/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4.1-flash")
    requests = []

    def invoke(body):
        requests.append(body)
        if body["messages"][0]["content"] == vision._REGION_PROMPT:
            raw = '{"text":"visible text","uncertain_spans":[],"graphic_marks":[]}'
        elif body["messages"][0]["content"] == vision._IMAGE_PROMPT:
            raw = '{"complete":true,"text":"visible text","uncertain_spans":[],"is_blank":false}'
        else:
            raw = '{"complete":true,"printed_page":null,"text":"visible text","uncertain_spans":[],"is_blank":false}'
        return {"model": body["model"], "choices": [{"message": {"content": raw}}]}, raw, "stop"

    monkeypatch.setattr(vision, "_invoke", invoke)
    rendered = {"image_base64": "aW1hZ2U=", "native_text": ""}
    vision.transcribe_page(rendered)
    vision.transcribe_region(rendered)
    vision.transcribe_image(rendered)

    assert len(requests) == 3
    assert all(request["thinking"] == {"type": "disabled"} for request in requests)
    assert "reasoning_effort" not in requests[1]
    assert requests[0]["max_tokens"] == 16384


def test_non_deepseek_ocr_keeps_provider_default_reasoning(monkeypatch):
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://aerisun.top/sub2api/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "other-vision-model")
    captured = {}
    raw = '{"complete":true,"printed_page":null,"text":"visible text","uncertain_spans":[],"is_blank":false}'

    def invoke(body):
        captured.update(body)
        return {"model": body["model"], "choices": [{"message": {"content": raw}}]}, raw, "stop"

    monkeypatch.setattr(vision, "_invoke", invoke)
    vision.transcribe_page({"image_base64": "aW1hZ2U=", "native_text": ""})

    assert "thinking" not in captured


def test_table_quality_retry_gets_blind_row_boundary_recheck(monkeypatch):
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://aerisun.top/sub2api/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4.1-flash")
    requests = []
    raw = '{"complete":true,"printed_page":null,"text":"table","uncertain_spans":[],"is_blank":false}'

    def invoke(body):
        requests.append(body)
        return {"model": body["model"], "choices": [{"message": {"content": raw}}]}, raw, "stop"

    monkeypatch.setattr(vision, "_invoke", invoke)
    vision.transcribe_page({"image_base64": "aW1hZ2U=", "native_text": ""})
    retried = vision.transcribe_page(
        {
            "image_base64": "aW1hZ2U=",
            "native_text": "",
            "quality_retry": "table_layout_not_preserved",
        }
    )

    first_text = requests[0]["messages"][1]["content"][1]["text"]
    retry_text = requests[1]["messages"][1]["content"][1]["text"]
    assert "previous visual transcription" not in first_text
    assert "previous visual transcription reported table_layout_not_preserved" in retry_text
    assert "its transcript is intentionally omitted" in retry_text
    assert retried["prompt_version"] == "legal-page-transcription-v5"
