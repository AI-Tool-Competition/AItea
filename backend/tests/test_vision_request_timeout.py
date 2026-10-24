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
