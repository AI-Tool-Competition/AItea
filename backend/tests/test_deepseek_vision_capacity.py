"""DeepSeek image fallback is opt-in and single-flight across local workers."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import fcntl
import json
import multiprocessing
import os
import time

import httpx
import pytest

from app import ingestion, native_asset_ocr, ocr_recovery, parsing, vision
from app.ingestion import parse_file
from test_ingestion_quality import make_pdf
from test_native_asset_ocr import docx
from test_vision_ocr import fake_provider, payload


@pytest.fixture
def deepseek(monkeypatch, tmp_path):
    monkeypatch.setenv("TEA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4.1-flash")
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("VISION_OCR_WORKERS", "64")
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", "true")
    gate = vision._DeepSeekImageSlot()
    monkeypatch.setattr(vision, "_DEEPSEEK_IMAGE_SLOT", gate)
    monkeypatch.setattr(parsing, "_OCR_SLOTS", parsing._VisionSlots(initial_target=16))
    return gate, tmp_path


def _mock_image_worker(data, active, peak, counter, start):
    os.environ.update(
        TEA_DATA_DIR=data,
        LLM_BASE_URL="https://provider.invalid/v1",
        LLM_API_KEY="test-secret",
        LLM_MODEL="deepseek-v4.1-flash",
    )

    def handler(_request):
        with active.get_lock():
            active.value += 1
            peak.value = max(peak.value, active.value)
        try:
            time.sleep(0.03)
            with counter.get_lock():
                counter.value += 1
            return httpx.Response(200, json=payload())
        finally:
            with active.get_lock():
                active.value -= 1

    original = httpx.Client
    vision.httpx.Client = lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    assert start.wait(5)
    for _ in range(3):
        vision._invoke({"model": "deepseek-v4.1-flash"})


def _hold_image_slot(data, ready):
    os.environ["TEA_DATA_DIR"] = data
    slot = vision._DeepSeekImageSlot()
    assert slot.acquire(timeout=2)
    ready.set()
    time.sleep(20)


@pytest.mark.parametrize("model", ["deepseek-v4.1-flash", "DeepSeek-V3", "vendor/deepseek-r1"])
def test_deepseek_aliases_cap_only_vision_and_preserve_embedding_64(deepseek, monkeypatch, model):
    monkeypatch.setenv("LLM_MODEL", model)
    monkeypatch.setenv("EMBEDDING_HTTP_WORKERS", "64")
    assert vision.vision_ocr_worker_limit() == 1
    assert ingestion.embedding_http_worker_limit() == 64
    slots = parsing._OCR_SLOTS
    assert slots.acquire(timeout=0)
    assert slots.snapshot()["maximum"] == slots.snapshot()["target"] == 1
    assert not slots.acquire(timeout=0)
    slots.release()


def test_non_deepseek_vision_keeps_existing_parallel_capacity(deepseek, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "qwen2.5-vl")
    assert vision.vision_ocr_worker_limit() == 64
    slots = parsing._OCR_SLOTS
    assert slots.acquire(timeout=0) and slots.acquire(timeout=0)
    assert slots.snapshot()["active"] == 2
    slots.release()
    slots.release()


def test_actual_mock_http_is_single_flight_across_processes_and_data_alias(deepseek):
    _, data = deepseek
    alias = data / "same-data"
    alias.symlink_to(data, target_is_directory=True)
    ctx = multiprocessing.get_context("spawn")
    active, peak, counter, start = ctx.Value("i", 0), ctx.Value("i", 0), ctx.Value("i", 0), ctx.Event()
    processes = [
        ctx.Process(target=_mock_image_worker, args=(str(root), active, peak, counter, start))
        for root in (data, alias)
    ]
    try:
        for process in processes:
            process.start()
        start.set()
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
        assert counter.value == 6 and peak.value == 1 and active.value == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.kill()
                process.join(5)


def test_killed_process_releases_shared_image_lock(deepseek):
    gate, data = deepseek
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    process = ctx.Process(target=_hold_image_slot, args=(str(data), ready))
    try:
        process.start()
        assert ready.wait(5)
        assert not gate.acquire(timeout=0.02)
        process.kill()
        process.join(5)
        assert gate.acquire(timeout=1)
        gate.release()
    finally:
        if process.is_alive():
            process.kill()
            process.join(5)


def test_busy_global_slot_defers_pdf_native_and_region_before_any_request_intent(deepseek, monkeypatch):
    gate, data = deepseek
    runtime = data / ".runtime"
    runtime.mkdir()
    acquire = gate.acquire
    monkeypatch.setattr(gate, "acquire", lambda **_: acquire(timeout=0))
    monkeypatch.setattr(vision.httpx, "Client", lambda **_: pytest.fail("busy gate called HTTP"))
    content = make_pdf([("Visible original", True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    recovering = deepcopy(page)
    recovering.update(status="ocr_uncertain", ocr={"phase": "recovery", "attempts": 2})
    native_content, manifest = docx()
    asset = native_asset_ocr.pending_assets(manifest)[0]
    with (runtime / "deepseek-vision.lock").open("wb") as blocker:
        fcntl.flock(blocker, fcntl.LOCK_EX)
        results = [
            (parsing._ocr_one_page(content, page), page),
            (parsing._ocr_one_page(content, recovering, on_region=lambda _: {}), recovering),
            (native_asset_ocr._one(native_content, manifest, asset, None, None), asset),
            (ocr_recovery.recover_page_step(content, recovering, on_region=lambda _: {}), recovering),
        ]
        for result, original in results:
            assert result["deferred_reason"] == "ocr_capacity"
            assert result.get("ocr") == original.get("ocr")
            assert not (result.get("ocr") or {}).get("request_outcome")
        with pytest.raises(vision.OCRCapacityDeferred) as error:
            vision._invoke({"model": "deepseek-v4.1-flash"})
        assert error.value.request_sent is False and not error.value.request_outcome_unknown
    assert not parsing._OCR_SLOTS.snapshot()["initialized"]


@pytest.mark.parametrize("terminal", ["complete", "conflict", "failed"])
def test_busy_slot_does_not_downgrade_terminal_region_checkpoint(deepseek, monkeypatch, terminal):
    gate, _ = deepseek
    monkeypatch.setattr(gate, "acquire", lambda **_: pytest.fail("terminal cache requested a slot"))
    content = make_pdf([("Verified source", True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    page.update(
        status="ocr_complete" if terminal == "complete" else "ocr_uncertain",
        text="Saved transcription",
        ocr={"attempts": 2, "recovery": {"version": ocr_recovery.RECOVERY_VERSION, "status": terminal}},
    )
    result = ocr_recovery.recover_page_step(
        content, page, on_region=lambda _: pytest.fail("terminal cache rendered a region")
    )
    assert result == page


def test_native_text_and_completed_pdf_cache_make_no_additional_image_requests(deepseek, monkeypatch):
    calls = fake_provider(monkeypatch, payload())
    native = make_pdf([("Complete native legal text with all terms and conditions.", False)])
    original = parse_file("native.pdf", native)
    assert original["pages"][0]["status"] == "native"
    rebuilt = parsing.ocr_bounded("native.pdf", native, "application/pdf", original)
    assert rebuilt["pages"][0]["status"] == "native" and not calls
    content = make_pdf([("Pasal 1\nKetentuan lengkap.", True)])
    first = parsing.ocr_bounded("scan.pdf", content, "application/pdf", parse_file("scan.pdf", content))
    assert first["pages"][0]["status"] == "ocr_complete" and len(calls) == 1
    second = parsing.ocr_bounded("scan.pdf", content, "application/pdf", first)
    assert second["pages"][0]["ocr"] == first["pages"][0]["ocr"] and len(calls) == 1


def test_completed_native_image_cache_is_reused_without_another_request(deepseek, monkeypatch):
    response = payload()
    text = json.loads(response["choices"][0]["message"]["content"])
    text.pop("printed_page")
    response["choices"][0]["message"]["content"] = json.dumps(text)
    calls = fake_provider(monkeypatch, response)
    content, manifest = docx()
    first = native_asset_ocr.ocr_native_assets_bounded(content, manifest)
    assert native_asset_ocr.assets(first)[0]["asset_status"] == "ocr_complete" and len(calls) == 1
    second = native_asset_ocr.ocr_native_assets_bounded(content, first)
    assert native_asset_ocr.assets(second)[0]["ocr"] == native_asset_ocr.assets(first)[0]["ocr"]
    assert len(calls) == 1


def test_direct_image_calls_and_outer_slots_are_reentrant_without_deadlock(deepseek, monkeypatch):
    calls = fake_provider(monkeypatch, payload())
    slots = parsing._OCR_SLOTS
    assert slots.acquire(timeout=0)
    try:
        assert vision.transcribe_page({"image_base64": "eA=="})["complete"]
    finally:
        slots.release()
    assert len(calls) == 1
    with ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(lambda _: vision._invoke({"model": "deepseek-v4.1-flash"}), range(3)))
    assert len(calls) == 4
