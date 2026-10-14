import base64
import hashlib
import json

import httpx
import pytest

from app import parsing
from app.ingestion import parse_file
from test_ingestion_quality import make_pdf


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "test-vision-model")


def fake_provider(monkeypatch, response):
    from app import vision

    original = httpx.Client
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)
    )
    return requests


def payload(*, text="Pasal 1\nKetentuan lengkap.", uncertain=None, finish="stop"):
    return {
        "model": "provider-model-revision",
        "choices": [
            {
                "finish_reason": finish,
                "message": {
                    "content": json.dumps(
                        {
                            "complete": True,
                            "printed_page": "11",
                            "text": text,
                            "uncertain_spans": uncertain or [],
                            "is_blank": False,
                        }
                    )
                },
            }
        ],
    }


def test_scan_ocr_is_opt_in_and_no_network_when_disabled(monkeypatch):
    from app import vision

    monkeypatch.delenv("VISION_OCR_ENABLED", raising=False)
    monkeypatch.setattr(vision.httpx, "Client", lambda **_: pytest.fail("OCR called without opt-in"))
    content = make_pdf([("bad text layer", True)])
    original = parse_file("scan.pdf", content)
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", original)
    assert result["quality"]["pending_pages"] == [1]
    assert not vision.vision_ocr_configured()


def test_transient_provider_failures_retry_three_times_without_looping(enabled, monkeypatch):
    from app import vision

    original = httpx.Client
    calls = []

    def unavailable(request):
        calls.append(True)
        raise httpx.ReadTimeout("test transport failure", request=request)

    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(unavailable), **kw)
    )
    content = make_pdf([("original text", True)])
    result = parse_file("scan.pdf", content)
    for attempt in range(1, 4):
        result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", result)
        assert result["pages"][0]["ocr"]["attempts"] == attempt
        assert result["pages"][0]["status"] == ("pending_ocr" if attempt < 3 else "ocr_failed")
        assert result["quality"]["complete"] is False
    parsing.ocr_bounded("scan.pdf", content, "application/pdf", result)
    assert len(calls) == 3


def test_authentication_errors_do_not_automatically_retry(enabled, monkeypatch):
    from app import vision

    original = httpx.Client
    monkeypatch.setattr(
        vision.httpx,
        "Client",
        lambda **kw: original(transport=httpx.MockTransport(lambda _: httpx.Response(401)), **kw),
    )
    content = make_pdf([("original text", True)])
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", parse_file("scan.pdf", content))
    assert result["pages"][0]["status"] == "ocr_failed"


@pytest.mark.parametrize(
    "native,transcribed,field",
    [
        ("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023", "law_number_year"),
        ("Nomor 24 Tahun 2022", "Nomor 23 Tahun 2022", "law_number_year"),
        ("pada tanggal 29 Oktober 2025", "pada tanggal 29 Oktober 2024", "date"),
        ("TAHUN 2025 NOMOR 181", "TAHUN 2024 NOMOR 181", "publication"),
        ("Pasal 14\nKetentuan penutup.", "Pasal 4\nKetentuan penutup.", "article"),
        ("Jumlah 508.156.210.535,00", "Jumlah 508.156.210.533,00", "amount"),
    ],
)
def test_model_complete_cannot_hide_changed_legal_numbers(enabled, monkeypatch, native, transcribed, field):
    fake_provider(monkeypatch, payload(text=transcribed))
    content = make_pdf([(native, True)])
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", parse_file("scan.pdf", content))
    page = result["pages"][0]
    assert page["native_text"] == native and page["text"] == transcribed
    assert page["status"] == "ocr_uncertain"
    assert "native_ocr_" + field + "_mismatch" in page["ocr"]["uncertain_spans"]
    assert result["quality"]["review_pages"] == [1]
    assert not result["quality"]["complete"]


def test_legal_number_validation_preserves_layout_variants_without_guessing(enabled, monkeypatch):
    fake_provider(
        monkeypatch,
        payload(text="Nomor 19 Tahun 2025\npada tanggal 29 oktober 2025\nPasal 14\nRp 1.234.000,00"),
    )
    native = "NOMOR 19\nTAHUN 2025\npada tanggal 29 Oktober 2025\nPasal 14\nRp. 1.234.000,00"
    content = make_pdf([(native, True)])
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", parse_file("scan.pdf", content))
    assert result["quality"]["complete"]
    assert result["pages"][0]["ocr"]["validation_version"]


def test_broken_native_anchor_does_not_invalidate_additional_readable_numbers(enabled, monkeypatch):
    fake_provider(monkeypatch, payload(text="Nomor 19 Tahun 2025\nNomor 24 Tahun 2000\nPasal 1\nPasal 2"))
    native = "NOMOR l9 TAHUN 2O25\nNomor 24 Tahun 2000\nPasal I\nPasal 2"
    content = make_pdf([(native, True)])
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", parse_file("scan.pdf", content))
    assert result["quality"]["complete"]


@pytest.mark.parametrize("scanned", [False, True])
def test_model_receives_high_detail_image_and_visibility_aware_native_hint(enabled, monkeypatch, scanned):
    requests = fake_provider(monkeypatch, payload())
    content = make_pdf([("native layer", scanned)])
    page = parse_file("scan.pdf", content)["pages"][0]
    parsing._ocr_one_page(content, page)
    parts = requests[0]["messages"][1]["content"]
    assert parts[0]["image_url"]["detail"] == "high"
    if (page.get("native_visibility") or {}).get("requires_visual_review"):
        assert "native layer" not in parts[1]["text"] and parts[1]["text"].endswith('""')
    else:
        assert "native layer" in parts[1]["text"]
    assert "untrusted" in parts[1]["text"]


def test_resumed_complete_page_rechecks_numbers_without_model_or_text_replacement(enabled, monkeypatch):
    from app import vision

    content = make_pdf([("NOMOR 19 TAHUN 2025", True)])
    manifest = parse_file("scan.pdf", content)
    page = manifest["pages"][0]
    page.update(status="ocr_complete", text="NOMOR 19 TAHUN 2023", ocr={"uncertain_spans": []})
    manifest["quality"]["complete"] = True
    monkeypatch.setattr(vision, "transcribe_page", lambda *_: pytest.fail("completed page resent"))
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", manifest)
    assert result["pages"][0]["text"] == page["text"]
    assert result["quality"]["review_pages"] == [1]
    assert not result["quality"]["complete"]


def test_page_render_uses_bounded_process_and_binds_source():
    content = make_pdf([("Source image", False)])
    rendered = parsing.render_pdf_page_bounded(content, 1)
    image = base64.b64decode(rendered["image_base64"])
    assert image.startswith(b"\x89PNG")
    assert rendered["image_sha256"] == hashlib.sha256(image).hexdigest()
    assert rendered["source_hash"] == hashlib.sha256(content).hexdigest()
    assert rendered["page"] == 1
    assert rendered["width"] * rendered["height"] <= 6_000_000
    assert rendered["renderer_version"]


def test_ocr_retains_full_transcript_and_checkpoint_without_resending_success(enabled, monkeypatch):
    requests = fake_provider(monkeypatch, payload())
    content = make_pdf([("broken native", True), ("second native", True)])
    checkpoints = []
    result = parsing.ocr_bounded(
        "scan.pdf",
        content,
        "application/pdf",
        parse_file("scan.pdf", content),
        on_page=checkpoints.append,
        max_pages=1,
    )
    assert len(checkpoints) == 2
    assert checkpoints[0]["status"] == "pending_ocr" and checkpoints[0]["ocr"]["attempts"] == 1
    page = checkpoints[1]
    assert page["page"] == 1 and page["printed_page"] == "11"
    assert page["native_text"] == "broken native"
    assert page["text"] == "Pasal 1\nKetentuan lengkap."
    assert page["ocr"]["finish_reason"] == "stop"
    assert page["ocr"]["requested_model"] == "test-vision-model"
    assert page["ocr"]["model"] == "provider-model-revision"
    assert result["quality"]["completed_pages"] == [1]
    assert result["quality"]["pending_pages"] == [2]
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", result, max_pages=1)
    assert len(requests) == 2
    assert result["quality"]["complete"]
    assert {row["locator"]["page"] for row in result["chunks"]} == {1, 2}
    assert all("broken native" not in row["text"] for row in result["chunks"])
    assert all(
        request["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/png;base64,")
        for request in requests
    )


@pytest.mark.parametrize(
    "finish,uncertain,status", [("length", [], "ocr_failed"), ("stop", ["numeral unclear"], "ocr_uncertain")]
)
def test_truncated_and_uncertain_responses_are_terminal_unindexable_pages(
    enabled, monkeypatch, finish, uncertain, status
):
    requests = fake_provider(monkeypatch, payload(finish=finish, uncertain=uncertain))
    content = make_pdf([("preserved native", True)])
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", parse_file("scan.pdf", content))
    assert result["pages"][0]["status"] == status
    assert not result["quality"]["complete"]
    assert not result["quality"]["pending_pages"]
    assert result["pages"][0]["ocr"]["finish_reason"] == finish
    if status == "ocr_failed":
        assert result["pages"][0]["text"] == "preserved native"
    parsing.ocr_bounded("scan.pdf", content, "application/pdf", result)
    assert len(requests) == 1


def test_checkpoint_failure_propagates_to_job_runner(enabled, monkeypatch):
    fake_provider(monkeypatch, payload())
    content = make_pdf([("native", True)])

    def fail(_page):
        raise RuntimeError("checkpoint unavailable")

    with pytest.raises(RuntimeError, match="checkpoint unavailable"):
        parsing.ocr_bounded(
            "scan.pdf", content, "application/pdf", parse_file("scan.pdf", content), on_page=fail
        )


def test_wrong_original_hash_cannot_reuse_transcript(enabled):
    content = make_pdf([("first", True)])
    manifest = parse_file("first.pdf", content)
    with pytest.raises(ValueError, match="hash|原件"):
        parsing.ocr_bounded("other.pdf", make_pdf([("other", True)]), "application/pdf", manifest)


def test_rendered_image_is_saved_before_provider_call(enabled, monkeypatch):
    requests = fake_provider(monkeypatch, payload())
    content = make_pdf([("native", True)])
    image_records = []

    def save(rendered):
        assert not requests
        assert (
            hashlib.sha256(base64.b64decode(rendered["image_base64"])).hexdigest() == rendered["image_sha256"]
        )
        image_records.append(rendered)
        return {"sha256": rendered["image_sha256"], "asset_id": "saved-image"}

    result = parsing.ocr_bounded(
        "scan.pdf", content, "application/pdf", parse_file("scan.pdf", content), on_image=save
    )
    assert len(image_records) == 1
    assert result["pages"][0]["ocr"]["image_blob"]["asset_id"] == "saved-image"
    assert "image_base64" not in result["pages"][0]["ocr"]


def test_image_checkpoint_error_is_not_swallowed_or_sent_to_model(enabled, monkeypatch):
    requests = fake_provider(monkeypatch, payload())
    content = make_pdf([("native", True)])

    def fail(_image):
        raise ValueError("image CAS unavailable")

    with pytest.raises(ValueError, match="image CAS unavailable"):
        parsing.ocr_bounded(
            "scan.pdf", content, "application/pdf", parse_file("scan.pdf", content), on_image=fail
        )
    assert requests == []


def test_blank_response_conflicting_with_text_layer_needs_review(enabled, monkeypatch):
    response = payload(text="")
    response["choices"][0]["message"]["content"] = json.dumps(
        {"text": "", "complete": True, "is_blank": True, "printed_page": None, "uncertain_spans": []}
    )
    fake_provider(monkeypatch, response)
    content = make_pdf([("Pasal 1\nVisible text in native layer", True)])
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", parse_file("scan.pdf", content))
    assert result["quality"]["review_pages"] == [1]
    assert not result["quality"]["complete"]


@pytest.mark.parametrize(
    "prefix,suffix,valid",
    [
        ("```json\n", "\n```", True),
        ("```\n", "\n```", True),
        ("Here is the transcription:\n```json\n", "\n```", False),
        ("```json\n", "\n```\nDone", False),
    ],
)
def test_only_single_complete_json_fence_is_accepted(enabled, monkeypatch, prefix, suffix, valid):
    from app.vision import OCRResponseError, transcribe_page

    response = payload()
    message = response["choices"][0]["message"]
    message["content"] = prefix + message["content"] + suffix
    fake_provider(monkeypatch, response)
    if valid:
        assert transcribe_page({"image_base64": "eA=="})["text"].startswith("Pasal 1")
    else:
        with pytest.raises(OCRResponseError, match="JSON"):
            transcribe_page({"image_base64": "eA=="})


def test_table_page_without_table_structure_requires_review(enabled, monkeypatch):
    fake_provider(monkeypatch, payload(text="Header A Header B Cell A Cell B"))
    content = make_pdf([("Header A Header B Cell A Cell B", True)])
    manifest = parse_file("table.pdf", content)
    manifest["pages"][0]["reasons"].append("table_layout")
    result = parsing.ocr_bounded("table.pdf", content, "application/pdf", manifest)
    assert result["quality"]["review_pages"] == [1]
    assert "table_layout_not_preserved" in result["pages"][0]["ocr"]["uncertain_spans"]


class _NoCapacity:
    def acquire(self, **_kwargs):
        return False

    def release(self):
        pytest.fail("unacquired capacity was released")


def test_busy_ocr_capacity_preserves_pending_pages_and_resumes_without_failure(enabled, monkeypatch):
    requests = fake_provider(monkeypatch, payload())
    content = make_pdf([("native", True)] * 5)
    manifest = parse_file("scan.pdf", content)
    slots = parsing._OCR_SLOTS
    monkeypatch.setattr(parsing, "_OCR_SLOTS", _NoCapacity())
    checkpoints = []
    result = parsing.ocr_bounded("scan.pdf", content, "application/pdf", manifest, on_page=checkpoints.append)
    assert result["quality"]["failed_pages"] == []
    assert result["quality"]["pending_pages"] == [1, 2, 3, 4, 5]
    assert result["quality"]["deferred_pages"] == [1, 2, 3, 4]
    assert result["quality"]["retry_after_seconds"] > 0
    assert len(checkpoints) == 4 and requests == []
    assert all("error" not in page for page in checkpoints)
    monkeypatch.setattr(parsing, "_OCR_SLOTS", slots)
    resumed = parsing.ocr_bounded("scan.pdf", content, "application/pdf", result, max_pages=1)
    assert resumed["pages"][0]["status"] == "ocr_complete"
    assert "deferred_reason" not in resumed["pages"][0]
    assert resumed["quality"]["deferred_pages"] == [2, 3, 4]
    assert len(requests) == 1


def test_render_capacity_wait_is_not_a_failed_transcription(enabled, monkeypatch):
    requests = fake_provider(monkeypatch, payload())
    content = make_pdf([("native", True)])
    monkeypatch.setattr(parsing, "_SLOTS", _NoCapacity())
    result = parsing._ocr_one_page(content, parse_file("scan.pdf", content)["pages"][0])
    assert result["status"] == "pending_ocr"
    assert result["deferred_reason"] == "render_capacity"
    assert "error" not in result and requests == []


def test_busy_worker_capacity_has_a_retryable_deferred_error(monkeypatch):
    monkeypatch.setattr(parsing, "_SLOTS", _NoCapacity())
    with pytest.raises(ValueError) as exc:
        parsing.parse_bounded("law.txt", b"native text")
    assert getattr(exc.value, "retryable", False)
    assert getattr(exc.value, "retry_after", 0) > 0
    assert isinstance(exc.value, parsing.ProcessingDeferred)
