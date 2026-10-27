"""Multipart image routing, strict receipts and the shared 48-request ceiling."""

import asyncio
import base64
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from app.contexts.conversations.infrastructure.agent_transport import gateway_tool
from app.contexts.documents.infrastructure.providers import vision

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII="
)
URL = "https://aerisun.top/sub2api/v1/tools/image_recognition"


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setenv("VISION_OCR_PROVIDER", "sub2api")
    monkeypatch.setenv("VISION_OCR_URL", URL)
    monkeypatch.setenv("VISION_OCR_API_KEY", "image-only-fixture-key")
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("VISION_OCR_WORKERS", "48")
    monkeypatch.setenv("VISION_OCR_REQUEST_WORKERS", "48")
    monkeypatch.setenv("TEA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_BASE_URL", "https://old-provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "old-chat-key")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4.1-flash")
    monkeypatch.setattr(vision, "_VISION_REQUEST_SLOT", vision._VisionRequestSlot())
    return tmp_path


def mock_http(monkeypatch, handler):
    sync_client, async_client = httpx.Client, httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "Client", lambda **kw: sync_client(transport=transport, **kw))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: async_client(transport=transport, **kw))


@pytest.mark.parametrize("kind", ["page", "image", "region"])
def test_all_ocr_reads_use_one_multipart_image_without_chat_model_fallback(configured, monkeypatch, kind):
    requests = []
    text = {"text": "VISIBLE 250", "uncertain_spans": []}
    if kind == "region":
        text["graphic_marks"] = []
    else:
        text.update(complete=True, is_blank=False)
        if kind == "page":
            text["printed_page"] = None

    def handler(request):
        requests.append(request)
        assert str(request.url) == URL
        assert request.headers["authorization"] == "Bearer image-only-fixture-key"
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert PNG in request.content
        assert b'name="prompt"' in request.content
        assert b'name="image"; filename="image.png"' in request.content
        assert b"deepseek" not in request.content
        assert b"never" in request.content.lower()
        return httpx.Response(
            200,
            json={"name": "image_recognition", "status": "completed", "output": {"text": json.dumps(text)}},
        )

    mock_http(monkeypatch, handler)
    rendered = {"image_base64": base64.b64encode(PNG).decode(), "native_text": ""}
    result = getattr(vision, "transcribe_" + kind)(rendered)
    assert result["text"] == "VISIBLE 250"
    assert result["requested_model"] == "sub2api/image_recognition"
    assert result["review_status"] == "unreviewed"
    assert len(requests) == 1


def test_chat_images_use_the_same_endpoint_and_multipart_contract(configured, monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == URL
        assert request.headers["authorization"] == "Bearer image-only-fixture-key"
        assert PNG in request.content and b"describe original text" in request.content
        assert b"deepseek" not in request.content
        return httpx.Response(
            200, json={"name": "image_recognition", "status": "completed", "output": {"text": "VISIBLE 250"}}
        )

    mock_http(monkeypatch, handler)
    result = asyncio.run(
        gateway_tool(
            "image_recognition",
            {
                "image": "data:image/png;base64," + base64.b64encode(PNG).decode(),
                "prompt": "describe original text",
            },
            lambda: None,
            timeout=5,
        )
    )
    assert result["output"]["text"] == "VISIBLE 250" and len(seen) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"status": "running", "output": {"text": "partial"}},
        {"status": "completed", "output": {"text": ""}},
        {"status": "completed", "error": "failure", "output": {"text": "partial"}},
    ],
)
def test_tool_partial_or_failed_receipts_never_become_complete_ocr(configured, monkeypatch, response):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=response)

    mock_http(monkeypatch, handler)
    with pytest.raises(vision.OCRResponseError) as raised:
        vision.transcribe_image({"image_base64": base64.b64encode(PNG).decode()})
    assert raised.value.request_outcome_unknown
    assert len(calls) == 1


def test_shared_file_slots_allow_48_and_block_the_49th_across_slot_instances(configured):
    held, release = threading.Barrier(49), threading.Event()

    def hold(_):
        # Independent objects still share the canonical instance's file locks.
        slot = vision._VisionRequestSlot()
        assert slot.acquire(timeout=2, data_root=configured)
        try:
            held.wait(timeout=10)
            release.wait(timeout=10)
        finally:
            slot.release()

    with ThreadPoolExecutor(max_workers=48) as pool:
        tasks = [pool.submit(hold, index) for index in range(48)]
        try:
            held.wait(timeout=10)
            extra = vision._VisionRequestSlot()
            assert not extra.acquire(timeout=0, data_root=configured)
            child = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from app.contexts.documents.infrastructure.providers.vision import _VisionRequestSlot; sys.exit(1 if _VisionRequestSlot().acquire(timeout=0, data_root=sys.argv[1]) else 0)",
                    str(configured),
                ],
                capture_output=True,
                cwd=Path(__file__).resolve().parents[1],
                timeout=15,
                check=False,
            )
            assert child.returncode == 0, child.stderr.decode()
        finally:
            release.set()
        for task in tasks:
            task.result(timeout=10)
    assert extra.acquire(timeout=0, data_root=configured)
    extra.release()


def test_new_provider_profile_does_not_reuse_deepseek_transcription_identity(configured):
    from app.contexts.documents.infrastructure.recovery.pdf_ocr_reuse import current_profile

    profile = current_profile()
    assert profile["base_url"] == URL
    assert profile["requested_model"] == "sub2api/image_recognition"


def test_image_api_key_echo_is_never_saved_as_provider_error_evidence(configured, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, content=b"rejected image-only-fixture-key")

    mock_http(monkeypatch, handler)
    with pytest.raises(vision.OCRResponseError) as raised:
        vision.transcribe_image({"image_base64": base64.b64encode(PNG).decode()})
    assert raised.value.http_status == 401
    assert raised.value.response_evidence is None
    assert "image-only-fixture-key" not in str(raised.value)
    assert len(calls) == 1
