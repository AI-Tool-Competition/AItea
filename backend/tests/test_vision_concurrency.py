"""Configured vision capacity across real bounded renders and mocked HTTP requests."""

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
def enabled(monkeypatch):
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "test-vision-model")


@pytest.fixture
def slots(monkeypatch):
    monkeypatch.delenv("VISION_OCR_WORKERS", raising=False)
    value = parsing._ParserSlots(limit_reader=vision.vision_ocr_worker_limit, setting="VISION_OCR_WORKERS")
    monkeypatch.setattr(parsing, "_OCR_SLOTS", value)
    return value


@pytest.mark.parametrize("limit", [1, 2, 8, 16])
def test_explicit_capacity_and_default_preserve_bounded_permits(slots, monkeypatch, limit):
    assert vision.vision_ocr_worker_limit() == 2
    monkeypatch.setenv("VISION_OCR_WORKERS", str(limit))
    assert all(slots.acquire(timeout=0) for _ in range(limit))
    assert not slots.acquire(timeout=0)
    for _ in range(limit):
        slots.release()
    with pytest.raises(ValueError):
        slots.release()


@pytest.mark.parametrize("value", ["0", "17", "32", "-1", "bad", "2.5", ""])
def test_invalid_configuration_fails_before_render_or_request(enabled, slots, monkeypatch, value):
    monkeypatch.setenv("VISION_OCR_WORKERS", value)
    monkeypatch.setattr(parsing, "render_pdf_page_bounded", lambda *_: pytest.fail("render started"))
    monkeypatch.setattr(vision, "_invoke", lambda *_: pytest.fail("provider called"))
    with pytest.raises(ValueError, match="VISION_OCR_WORKERS must be between 1 and 16"):
        parsing._ocr_one_page(b"source", {"page": 1, "status": "pending_ocr"})


def test_live_capacity_cannot_be_replaced_but_old_permit_can_return(slots, monkeypatch):
    monkeypatch.setenv("VISION_OCR_WORKERS", "8")
    assert slots.acquire(timeout=0)
    monkeypatch.setenv("VISION_OCR_WORKERS", "16")
    with pytest.raises(ValueError, match="VISION_OCR_WORKERS changed; restart"):
        slots.acquire(timeout=0)
    monkeypatch.setenv("VISION_OCR_WORKERS", "bad")
    slots.release()  # Release never rereads invalid or changed configuration.
    monkeypatch.setenv("VISION_OCR_WORKERS", "8")
    assert all(slots.acquire(timeout=0) for _ in range(8))
    assert not slots.acquire(timeout=0)
    for _ in range(8):
        slots.release()


@pytest.mark.parametrize(
    "capacity, document_count, expected_peak", [(2, 8, 2), (8, 8, 8), (16, 8, 16), (8, 1, 4)]
)
def test_document_batches_fill_shared_capacity_without_expanding_four_page_batch(
    enabled, slots, monkeypatch, capacity, document_count, expected_peak
):
    import json

    monkeypatch.setenv("VISION_OCR_WORKERS", str(capacity))
    lock, reached, release = Lock(), Event(), Event()
    active = peak = calls = 0
    original_client = httpx.Client

    def handler(request):
        nonlocal active, peak, calls
        body = json.loads(request.content)
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == expected_peak:
                reached.set()
        try:
            assert release.wait(15), "Mock provider barrier was not released"
            native = json.loads(body["messages"][1]["content"][1]["text"].rsplit("\n", 1)[-1])
            return httpx.Response(200, json=payload(text=native or "Pasal 1\nVisible source."))
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(handler), **kw)
    )
    documents = []
    for index in range(document_count):
        content = make_pdf([(f"Pasal 1\nOriginal source {index}.", True)] * 5)
        documents.append((content, parse_file(f"source-{index}.pdf", content)))
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(parsing.ocr_bounded, "scan.pdf", content, "application/pdf", manifest)
            for content, manifest in documents
        ]
        try:
            assert reached.wait(12), f"Expected {expected_peak} concurrent requests were not reached"
            if expected_peak == capacity:
                assert not slots.acquire(timeout=0)
            with lock:
                assert active == calls == peak == expected_peak
        finally:
            release.set()
        results = [future.result(timeout=30) for future in futures]
    assert peak == expected_peak and active == 0 and calls <= 4 * document_count
    attempted = 0
    for result, (_, old) in zip(results, documents, strict=True):
        assert result["pages"][4] == old["pages"][4]
        for page in result["pages"][:4]:
            if page.get("ocr", {}).get("attempts") == 1:
                assert page["status"] == "ocr_complete"
                attempted += 1
            else:
                assert page["status"] == "pending_ocr"
                assert page["deferred_reason"] in {"ocr_capacity", "render_capacity"}
                assert not page.get("error")
    assert calls == attempted
    assert all(slots.acquire(timeout=0) for _ in range(capacity))
    assert not slots.acquire(timeout=0)
    for _ in range(capacity):
        slots.release()


def test_pdf_native_image_and_recovery_defer_from_the_same_occupied_pool(enabled, slots, monkeypatch):
    monkeypatch.setenv("VISION_OCR_WORKERS", "1")
    assert slots.acquire(timeout=0)
    acquire = slots.acquire
    monkeypatch.setattr(slots, "acquire", lambda **_: acquire(timeout=0))
    monkeypatch.setattr(vision, "_invoke", lambda *_: pytest.fail("full pool invoked provider"))
    monkeypatch.setattr(ocr_recovery, "recover_page_step", lambda *_args, **_kw: pytest.fail("recovery ran"))
    content = make_pdf([("Visible source", True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    recovered = deepcopy(page)
    recovered.update(status="ocr_uncertain", ocr={"phase": "recovery"})
    image_content, image_manifest = docx()
    asset = native_asset_ocr.pending_assets(image_manifest)[0]
    results = [
        parsing._ocr_one_page(content, page),
        parsing._ocr_one_page(content, recovered, on_region=lambda _: {}),
        native_asset_ocr._one(image_content, image_manifest, asset, None, None),
    ]
    assert all(r["deferred_reason"] == "ocr_capacity" and not r.get("ocr") for r in (results[0], results[2]))
    assert results[1]["ocr"] == {"phase": "recovery"}
    assert results[2]["asset_status"] == "pending_vision"
    slots.release()
    assert slots.acquire(timeout=0)
    slots.release()


@pytest.mark.parametrize("kind", ["page", "image", "recovery"])
def test_checkpoint_and_recovery_exceptions_return_owned_capacity(enabled, slots, monkeypatch, kind):
    monkeypatch.setenv("VISION_OCR_WORKERS", "1")
    monkeypatch.setattr(vision, "_invoke", lambda *_: pytest.fail("checkpoint failure made request"))

    def fail(*_args, **_kwargs):
        assert not slots.acquire(timeout=0), "Operation did not hold the shared permit"
        raise RuntimeError("checkpoint unavailable")

    content = make_pdf([("Visible source", True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        if kind == "image":
            image_content, manifest = docx()
            native_asset_ocr._one(
                image_content, manifest, native_asset_ocr.pending_assets(manifest)[0], None, fail
            )
        elif kind == "recovery":
            page.update(status="ocr_uncertain", ocr={"phase": "recovery"})
            monkeypatch.setattr(ocr_recovery, "recover_page_step", fail)
            parsing._ocr_one_page(content, page, on_region=lambda _: {})
        else:
            parsing._ocr_one_page(content, page, on_page=fail)
    assert slots.acquire(timeout=0)
    assert not slots.acquire(timeout=0)
    slots.release()


def test_provider_timeout_keeps_attempt_and_releases_capacity_for_next_request(enabled, slots, monkeypatch):
    monkeypatch.setenv("VISION_OCR_WORKERS", "1")
    original_client = httpx.Client

    def timeout(request):
        assert not slots.acquire(timeout=0)
        raise httpx.ReadTimeout("mock timeout", request=request)

    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(timeout), **kw)
    )
    content = make_pdf([("Visible source", True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    result = parsing._ocr_one_page(content, page)
    assert result["status"] == "pending_ocr" and result["deferred_reason"] == "provider_retry"
    assert result["ocr"]["attempts"] == 1
    assert slots.acquire(timeout=0)
    assert not slots.acquire(timeout=0)
    slots.release()
