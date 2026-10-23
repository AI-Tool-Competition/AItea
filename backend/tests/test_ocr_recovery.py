import base64
from copy import deepcopy
import hashlib

import pytest

from app import ingestion_quality, parsing
from test_ingestion_quality import make_pdf


def uncertain_page(native, text):
    data = make_pdf([(native, True)])
    from app.ingestion import parse_file

    page = parse_file("scan.pdf", data)["pages"][0]
    page.update(
        status="ocr_uncertain",
        text=text,
        ocr={"uncertain_spans": [], "image_blob": {"saved": "full-page"}, "model": "old-model"},
    )
    ingestion_quality.validate_ocr_page(page)
    return data, page


def test_amount_spacing_is_compared_without_truncating_or_changing_digits():
    _, page = uncertain_page(
        "Rp 508.156.210 .535,00\nRp 61.0 62.37 6.76 4,00", "Rp 508.156.210.535,00\nRp 61.062.376.764,00"
    )
    assert not page["ocr"]["validation_warnings"]
    assert page["status"] == "ocr_complete"


def test_unknown_native_amount_digit_still_needs_pixel_evidence():
    _, page = uncertain_page("Rp 11.234.000.000,00", "Rp 1.234.000.000,00")
    assert page["status"] == "ocr_uncertain"
    assert page["ocr"]["validation_warnings"]


def test_year_in_broken_reference_matches_same_year_in_complete_reference():
    _, page = uncertain_page("Nomor24 Tahun\n2000", "Nomor 24 Tahun 2000")
    assert page["status"] == "ocr_complete"
    _, conflict = uncertain_page("Nomor24 Tahun\n2000", "Nomor 24 Tahun 2001")
    assert conflict["status"] == "ocr_uncertain"


def test_number_crop_has_margin_for_inexact_native_character_positions():
    data, page = uncertain_page("Rp 11.234.000.000,00", "Rp 1.234.000.000,00")
    task = parsing.plan_pdf_ocr_recovery_bounded(data, page)[0]
    # The numeric token starts near x=70. Five points clipped trailing
    # digits of the real scan's damaged character coordinates.
    assert task["region_bbox"][0] <= 55
    assert task["verify_bbox"][2] > task["region_bbox"][2]


def test_region_render_keeps_original_binding_and_pixel_limit():
    render = getattr(parsing, "render_pdf_region_bounded", None)
    assert callable(render), "bounded PDF region rendering is missing"
    data = make_pdf([("NOMOR 19 TAHUN 2025", True)])
    result = render(data, 1, [35, 40, 250, 90])
    assert result["source_hash"] == hashlib.sha256(data).hexdigest()
    assert result["region_bbox"] == [35, 40, 250, 90]
    assert result["dpi"] == 400
    assert result["width"] * result["height"] <= 6_000_000
    assert hashlib.sha256(base64.b64decode(result["image_base64"])).hexdigest() == result["image_sha256"]


@pytest.mark.parametrize("box", [[-1, 0, 5, 10], [1, 1, 1, 5], [0, 0, float("nan"), 3], [0, 0, 25000, 10]])
def test_invalid_region_rejected_before_render(box):
    render = getattr(parsing, "render_pdf_region_bounded", None)
    assert callable(render), "bounded PDF region rendering is missing"
    with pytest.raises(ValueError):
        render(make_pdf([("original", True)]), 1, box)


def test_recovery_uses_document_geometry_instead_of_sample_coordinates():
    plan = getattr(parsing, "plan_pdf_ocr_recovery_bounded", None)
    assert callable(plan), "automatic region plan is missing"
    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023")
    tasks = plan(data, page)
    assert len(tasks) == 1 and tasks[0]["kind"] == "number"
    assert tasks[0]["target"]["field"] == "law_number_year"
    # This fixture places its line near the top; the real sample lies lower.
    assert tasks[0]["region_bbox"][1] < 100
    assert tasks[0]["region_bbox"][2] < 500


def test_consensus_changes_exact_span_keeps_history_and_saves_regions_before_http(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023\nOther year 2023.")
    original = deepcopy(page)
    sent = []
    saved = []

    def persist(rendered):
        saved.append(deepcopy(rendered))
        return {"content_hash": rendered["image_sha256"], "asset_id": "r" + str(len(saved))}

    def read(rendered):
        assert len(saved) > len(sent)
        assert "native_text" not in rendered and "candidate" not in rendered
        sent.append(rendered["image_sha256"])
        return {
            "text": "NOMOR 19 TAHUN 2025",
            "uncertain_spans": [],
            "graphic_marks": [],
            "prompt_version": "test-region",
        }

    monkeypatch.setattr(vision, "transcribe_region", read)
    first = ocr_recovery.recover_page_step(data, page, on_region=persist)
    assert first["status"] == "pending_ocr" and len(sent) == 1
    final = ocr_recovery.recover_page_step(data, first, on_region=persist)
    assert len(sent) == 2
    assert final["status"] == "ocr_complete"
    assert final["text"] == "NOMOR 19 TAHUN 2025\nOther year 2023."
    assert final["ocr"]["image_blob"] == original["ocr"]["image_blob"]
    assert final["ocr"]["recovery_history"][0]["text"] == original["text"]
    assert final["ocr"]["recovery_history"][0]["ocr"] == original["ocr"]
    assert (
        final["ocr"]["recovery_history"][0]["text_hash"]
        == hashlib.sha256(original["text"].encode()).hexdigest()
    )
    assert len(final["ocr"]["recovery"]["regions"]) == 2
    assert "image_base64" not in str(final["ocr"]["recovery"])
    ingestion_quality.validate_ocr_page(final)
    assert final["status"] == "ocr_complete"


def test_two_region_readings_disagree_preserves_uncertainty_and_text(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023")
    values = iter(["NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2024"])
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {"text": next(values), "uncertain_spans": [], "graphic_marks": []},
    )

    def save(r):
        return {"content_hash": r["image_sha256"]}

    result = ocr_recovery.recover_page_step(data, page, on_region=save)
    result = ocr_recovery.recover_page_step(data, result, on_region=save)
    assert result["status"] == "ocr_uncertain"
    assert result["text"] == page["text"]
    assert result["ocr"]["recovery"]["status"] == "conflict"
    monkeypatch.setattr(vision, "transcribe_region", lambda _: pytest.fail("terminal recovery resent"))
    assert ocr_recovery.recover_page_step(data, result, on_region=save)["text"] == page["text"]


def test_parser_recipe_upgrade_reuses_verified_numeric_work_without_new_requests(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023")
    task = parsing.plan_pdf_ocr_recovery_bounded(data, page)[0]
    monkeypatch.setattr(
        parsing,
        "plan_pdf_ocr_recovery_bounded",
        lambda *a, **kw: [
            deepcopy(task),
            {"kind": "unresolved", "reason": "old_framing", "status": "pending"},
        ],
    )
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {"text": "NOMOR 19 TAHUN 2025", "uncertain_spans": [], "graphic_marks": []},
    )

    def save(rendered):
        return {"content_hash": rendered["image_sha256"]}

    old = page
    for _ in range(3):
        old = ocr_recovery.recover_page_step(data, old, on_region=save)
    assert old["status"] == "ocr_uncertain" and old["text"] == page["text"]
    old["ocr"]["recovery"]["version"] = "previous-parser-recipe"
    monkeypatch.setattr(parsing, "plan_pdf_ocr_recovery_bounded", lambda *a, **kw: [deepcopy(task)])
    monkeypatch.setattr(
        vision, "transcribe_region", lambda _: pytest.fail("verified numeric region was sent again")
    )
    final = ocr_recovery.recover_page_step(data, old, on_region=save)
    assert final["status"] == "ocr_complete" and "2025" in final["text"]
    assert final["ocr"]["recovery"]["reused_tasks"] == 1
    assert final["ocr"]["recovery_history"][-1]["text"] == page["text"]


def test_transient_region_failures_keep_attempts_then_stop(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023")
    calls = []
    clock = [100.0]
    monkeypatch.setattr(ocr_recovery.time, "time", lambda: clock[0])

    def fail(_):
        calls.append(True)
        raise vision.OCRResponseError("transient", retryable=True)

    monkeypatch.setattr(vision, "transcribe_region", fail)

    def save(r):
        return {"content_hash": r["image_sha256"]}

    result = page
    for attempt in range(1, 4):
        result = ocr_recovery.recover_page_step(data, result, on_region=save)
        assert result["ocr"]["recovery"]["tasks"][0]["attempts"][0] == attempt
        assert result["status"] == ("pending_ocr" if attempt < 3 else "ocr_uncertain")
        if attempt < 3:
            assert result["ocr"]["recovery"]["retry_after_seconds"] == 5 * (2 ** (attempt - 1))
            clock[0] += result["ocr"]["recovery"]["retry_after_seconds"]
    ocr_recovery.recover_page_step(data, result, on_region=save)
    assert len(calls) == 3


def test_region_checkpoint_failure_propagates_without_provider_call(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023")
    monkeypatch.setattr(vision, "transcribe_region", lambda _: pytest.fail("unsaved region sent"))

    def fail(_):
        raise ValueError("CAS unavailable")

    with pytest.raises(ValueError, match="CAS unavailable"):
        ocr_recovery.recover_page_step(data, page, on_region=fail)


def test_recovery_checkpoints_attempt_and_region_before_external_request(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023")
    checkpoints = []

    def read(_):
        assert checkpoints[-1]["ocr"]["recovery"]["tasks"][0]["attempts"][0] == 1
        assert checkpoints[-1]["ocr"]["recovery"]["regions"][0]["image_blob"]
        return {"text": "NOMOR 19 TAHUN 2025", "uncertain_spans": [], "graphic_marks": []}

    monkeypatch.setattr(vision, "transcribe_region", read)
    ocr_recovery.recover_page_step(
        data, page, on_region=lambda r: {"hash": r["image_sha256"]}, on_page=checkpoints.append
    )


def test_ocr_pipeline_continues_recovery_without_resending_whole_page(monkeypatch):
    from app import vision
    from app.ingestion import parse_file

    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "test")
    data = make_pdf([("NOMOR 19 TAHUN 2025", True)])
    full_calls = []

    def full(_):
        full_calls.append(True)
        return {
            "text": "NOMOR 19 TAHUN 2023",
            "printed_page": None,
            "uncertain_spans": [],
            "is_blank": False,
            "complete": True,
            "model": "test",
        }

    monkeypatch.setattr(vision, "transcribe_page", full)
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {"text": "NOMOR 19 TAHUN 2025", "uncertain_spans": [], "graphic_marks": []},
    )

    def save(r):
        return {"content_hash": r["image_sha256"]}

    manifest = parsing.ocr_bounded(
        "scan.pdf", data, "application/pdf", parse_file("scan.pdf", data), on_region=save
    )
    assert manifest["pages"][0]["status"] == "pending_ocr"
    assert manifest["pages"][0]["ocr"]["phase"] == "recovery"
    for _ in range(2):
        manifest = parsing.ocr_bounded("scan.pdf", data, "application/pdf", manifest, on_region=save)
    assert len(full_calls) == 1 and manifest["quality"]["complete"]
    assert "2025" in manifest["chunks"][0]["text"] and "2023" not in manifest["chunks"][0]["text"]


def test_native_object_order_cannot_shift_correction_to_another_law():
    data, page = uncertain_page(
        "NOMOR 5 TAHUN 2026\nNomor 12 Tahun 2019\nNomor 77 Tahun 2020",
        "NOMOR 5 TAHUN 2023\nNomor 12 Tahun 2019\nNomor 77 Tahun 2020",
    )
    page["native_text"] = "Nomor 77 Tahun 2020\nNomor 12 Tahun 2019\nNOMOR 5 TAHUN 2026"
    tasks = parsing.plan_pdf_ocr_recovery_bounded(data, page)
    assert len(tasks) == 1 and tasks[0]["target"]["candidate_text"] == "NOMOR 5 TAHUN 2023"


def test_duplicate_dates_have_separate_actual_rectangles():
    native = "pada tanggal 29 Oktober 2025\npada tanggal 29 Oktober 2025"
    data, page = uncertain_page(native, native.replace("2025", "2024"))
    tasks = parsing.plan_pdf_ocr_recovery_bounded(data, page)
    assert len(tasks) == 2 and all(task["kind"] == "number" for task in tasks)
    assert tasks[0]["region_bbox"] != tasks[1]["region_bbox"]


def test_native_table_order_cannot_replace_an_unrelated_amount():
    native = "11.234.000.000,00\n8.043.000.000,00\n351.514.160.000,00"
    data, page = uncertain_page(native, "8.043.000.000,00\n351.514.160.000,00\n1.234.000.000,00")
    tasks = parsing.plan_pdf_ocr_recovery_bounded(data, page)
    assert len(tasks) == 1 and tasks[0]["target"]["candidate_text"] == "1.234.000.000,00"


def test_interrupted_last_attempt_cannot_restart_with_fourth_call(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023")
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {"text": "NOMOR 19 TAHUN 2025", "uncertain_spans": [], "graphic_marks": []},
    )
    checkpoints = []
    ocr_recovery.recover_page_step(
        data, page, on_region=lambda r: {"hash": r["image_sha256"]}, on_page=checkpoints.append
    )
    result = checkpoints[-1]  # persisted before the third HTTP attempt got a response
    result["ocr"]["recovery"]["tasks"][0]["attempts"][0] = 3
    monkeypatch.setattr(vision, "transcribe_region", lambda _: pytest.fail("fourth attempt"))
    result = ocr_recovery.recover_page_step(data, result, on_region=lambda r: {"hash": r["image_sha256"]})
    assert result["status"] == "ocr_uncertain" and result["ocr"]["recovery"]["status"] == "failed"


def test_complete_saved_region_is_reused_for_same_geometry(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023")
    calls = []

    def read(_):
        calls.append(True)
        return {"text": "NOMOR 19 TAHUN 2025", "uncertain_spans": [], "graphic_marks": []}

    monkeypatch.setattr(vision, "transcribe_region", read)

    def save(r):
        return {"content_hash": r["image_sha256"]}

    result = ocr_recovery.recover_page_step(data, page, on_region=save)
    task = result["ocr"]["recovery"]["tasks"][0]
    task["reading_ids"] = []
    resumed = ocr_recovery.recover_page_step(data, result, on_region=save)
    assert len(calls) == 1 and len(resumed["ocr"]["recovery"]["tasks"][0]["reading_ids"]) == 1


def test_same_image_twice_is_not_independent_numeric_evidence(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page("11.234.000.000,00", "1.234.000.000,00")
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {"text": "1.234.000.000,00", "uncertain_spans": [], "graphic_marks": []},
    )

    def save(r):
        return {"content_hash": r["image_sha256"]}

    result = ocr_recovery.recover_page_step(data, page, on_region=save)
    result = ocr_recovery.recover_page_step(data, result, on_region=save)
    assert result["status"] == "ocr_complete"
    regions = result["ocr"]["recovery"]["regions"]
    regions[1]["image_sha256"] = regions[0]["image_sha256"]
    ingestion_quality.validate_ocr_page(result)
    assert result["status"] == "ocr_uncertain"


def test_missing_native_layer_uses_bounded_general_regions():
    plan = getattr(parsing, "plan_pdf_ocr_recovery_bounded")
    from app.ingestion import parse_file

    data = make_pdf([("", True)])
    page = parse_file("scan.pdf", data)["pages"][0]
    page.update(
        status="ocr_uncertain",
        text="[illegible]dia Silvanna Djaman",
        ocr={"uncertain_spans": ["name unreadable"]},
    )
    tasks = plan(data, page)
    assert 2 <= len(tasks) <= 24 and all(task["kind"] == "search_printed" for task in tasks)
    assert all(0 <= task["region_bbox"][0] < task["region_bbox"][2] <= 612 for task in tasks)


def test_region_model_protocol_is_blind_and_strict(monkeypatch):
    import json
    import httpx
    from app import vision

    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setenv("LLM_MODEL", "test")
    requests = []
    original = httpx.Client

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"text": "NOMOR 19 TAHUN 2025", "uncertain_spans": [], "graphic_marks": []}
                            )
                        },
                    }
                ]
            },
        )

    monkeypatch.setattr(
        vision.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(respond), **kw)
    )
    result = vision.transcribe_region({"image_base64": "eA==", "native_text": "do not send this hint"})
    assert result["text"] == "NOMOR 19 TAHUN 2025" and result["native_hint_chars"] == 0
    assert "do not send this hint" not in str(requests)
    assert requests[0]["messages"][1]["content"][0]["image_url"]["detail"] == "high"


def test_image_only_uncertainty_search_requires_matching_printed_context(monkeypatch):
    from app import ocr_recovery, vision
    from app.ingestion import parse_file

    data = make_pdf([("", True)])
    page = parse_file("scan.pdf", data)["pages"][0]
    page.update(
        status="ocr_uncertain",
        text="[illegible]dia Silvanna Djaman",
        ocr={"uncertain_spans": ["[illegible]dia Silvanna Djaman"]},
    )

    def read(rendered):
        if rendered["region_bbox"][1] < 190:
            return {"text": "unrelated printed text", "uncertain_spans": [], "graphic_marks": []}
        return {
            "text": "Lydia Silvanna Djaman",
            "uncertain_spans": [],
            "graphic_marks": ["handwritten signature"],
        }

    monkeypatch.setattr(vision, "transcribe_region", read)
    for _ in range(8):
        page = ocr_recovery.recover_page_step(
            data, page, on_region=lambda r: {"content_hash": r["image_sha256"]}
        )
        if page["status"] != "pending_ocr":
            break
    assert page["status"] == "ocr_complete" and page["text"] == "Lydia Silvanna Djaman"
    assert any(task["status"] == "skipped" for task in page["ocr"]["recovery"]["tasks"])


def test_region_budget_rejection_is_terminal_and_checkpointable():
    from app.ingestion import parse_file

    data = make_pdf([("", True)])
    page = parse_file("scan.pdf", data)["pages"][0]
    page.update(
        status="ocr_uncertain",
        text="\n".join("[illegible] text" + str(i) for i in range(7)),
        ocr={"uncertain_spans": ["unreadable"]},
    )
    tasks = parsing.plan_pdf_ocr_recovery_bounded(data, page)
    assert len(tasks) == 1 and tasks[0]["reason"] == "region_budget_exceeded"
    assert tasks[0]["status"] == "pending" and tasks[0]["attempts"] == [0, 0]


def test_region_render_error_keeps_original_transcript_as_terminal_review(monkeypatch):
    from app import ocr_recovery

    data, page = uncertain_page("NOMOR 19 TAHUN 2025", "NOMOR 19 TAHUN 2023")

    def fail(*_):
        raise ValueError("document region rendering failed")

    monkeypatch.setattr(parsing, "render_pdf_region_bounded", fail)
    result = ocr_recovery.recover_page_step(
        data, page, on_region=lambda _: pytest.fail("invalid region saved")
    )
    assert result["status"] == "ocr_uncertain" and result["text"] == page["text"]
    assert result["ocr"]["recovery"]["status"] == "failed"


def test_file_ocr_never_reads_original_bytes_in_parent(tmp_path, monkeypatch):
    from pathlib import Path
    from app import vision
    from app.archive_parsing import parse_archive_pdf_bounded

    data = make_pdf([("NOMOR 19 TAHUN 2025", True)])
    path = tmp_path / "large-archive.pdf"
    path.write_bytes(data)
    manifest = parse_archive_pdf_bounded(path, source_hash=hashlib.sha256(data).hexdigest(), size=len(data))
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setenv("LLM_MODEL", "test")
    monkeypatch.setattr(
        vision,
        "transcribe_page",
        lambda _: {
            "text": "NOMOR 19 TAHUN 2023",
            "printed_page": None,
            "uncertain_spans": [],
            "is_blank": False,
            "complete": True,
        },
    )
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {"text": "NOMOR 19 TAHUN 2025", "uncertain_spans": [], "graphic_marks": []},
    )
    monkeypatch.setattr(Path, "read_bytes", lambda _: pytest.fail("parent materialized full archive"))

    def save(r):
        return {"content_hash": r["image_sha256"]}

    for _ in range(3):
        manifest = parsing.ocr_bounded(
            "scan.pdf",
            None,
            "application/pdf",
            manifest,
            archive_path=path,
            archive_size=len(data),
            on_region=save,
        )
    assert manifest["quality"]["complete"] and "2025" in manifest["chunks"][0]["text"]


def test_file_ocr_rejects_wrong_original_hash_before_model(tmp_path, monkeypatch):
    from app import vision
    from app.ingestion import parse_file

    data = make_pdf([("NOMOR 19 TAHUN 2025", True)])
    path = tmp_path / "archive.pdf"
    path.write_bytes(data)
    manifest = parse_file("scan.pdf", data)
    manifest["source_hash"] = "0" * 64
    monkeypatch.setattr(vision, "transcribe_page", lambda _: pytest.fail("wrong original sent"))
    with pytest.raises(ValueError, match="hash"):
        parsing.ocr_bounded(
            "scan.pdf", None, "application/pdf", manifest, archive_path=path, archive_size=len(data)
        )


def test_printed_recovery_must_preserve_all_already_visible_text(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page(
        "prefix Lydia Silvanna Djaman long preserved ending",
        "prefix [illegible]dia Silvanna Djaman long preserved ending",
    )
    page["ocr"]["uncertain_spans"] = ["[illegible]"]
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {"text": "Lydia Silvanna Djaman", "uncertain_spans": [], "graphic_marks": ["signature"]},
    )

    def save(r):
        return {"content_hash": r["image_sha256"]}

    result = ocr_recovery.recover_page_step(data, page, on_region=save)
    result = ocr_recovery.recover_page_step(data, result, on_region=save)
    assert result["status"] == "ocr_uncertain" and result["text"] == page["text"]


def test_modified_saved_numeric_evidence_does_not_waive_native_conflict(monkeypatch):
    from app import ocr_recovery, vision

    data, page = uncertain_page("11.234.000.000,00", "1.234.000.000,00")
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {"text": "1.234.000.000,00", "uncertain_spans": [], "graphic_marks": []},
    )

    def save(r):
        return {"content_hash": r["image_sha256"]}

    result = ocr_recovery.recover_page_step(data, page, on_region=save)
    result = ocr_recovery.recover_page_step(data, result, on_region=save)
    assert result["status"] == "ocr_complete"
    for region in result["ocr"]["recovery"]["regions"]:
        region["reading"]["text"] += "\nmodified after persistence"
    ingestion_quality.validate_ocr_page(result)
    assert result["status"] == "ocr_uncertain"


def literal_page(native="salmon (Hucho hucho)", text="prefix (*Huchohucho*) trailing", literal="Huchohucho"):
    data, page = uncertain_page(native, text)
    page.update(status="ocr_uncertain")
    page["ocr"].update(
        complete=True,
        text=text,
        uncertain_spans=[literal],
        image_sha256="e" * 64,
    )
    return data, page


def save_literal_region(rendered):
    assert hashlib.sha256(base64.b64decode(rendered["image_base64"])).hexdigest() == rendered["image_sha256"]
    return {"content_hash": rendered["image_sha256"], "storage_path": "regions/" + rendered["region_id"]}


def test_literal_locator_uses_unique_whitespace_insensitive_native_character_boxes():
    data, page = literal_page()
    tasks = parsing.plan_pdf_ocr_recovery_bounded(data, page)
    assert len(tasks) == 1 and tasks[0]["kind"] == "literal"
    target = tasks[0]["target"]
    start, end = target["candidate_span"]
    assert page["text"][start:end] == "Huchohucho"
    assert target["uncertainty_exact"] == "Huchohucho"
    assert target["source_hash"] == page["source_hash"]
    assert target["page"] == 1
    assert tasks[0]["region_bbox"] != tasks[0]["verify_bbox"]
    assert tasks[0]["region_bbox"][1] < 100
    assert tasks[0]["region_bbox"][2] < 300


def test_literal_locator_matches_superscript_ocr_against_baseline_pdf_glyph():
    data, page = literal_page(native="Net weight: 490g e6", text="Net weight: 490g e⁶", literal="490g e⁶")
    tasks = parsing.plan_pdf_ocr_recovery_bounded(data, page)
    assert len(tasks) == 1 and tasks[0]["kind"] == "literal"
    assert tasks[0]["target"]["uncertainty_exact"] == "490g e⁶"
    assert tasks[0]["target"]["native_locator_span"]


@pytest.mark.parametrize(
    "native,text,reason",
    [
        ("Hucho hucho\nHuchohucho", "Huchohucho", "ambiguous_literal_location"),
        ("Hucho hucho", "Huchohucho and Huchohucho", "ambiguous_literal_alignment"),
        ("prefixHuchohuchoSuffix", "Huchohucho", "literal_location_unavailable"),
    ],
)
def test_literal_multiple_occurrences_or_inside_word_location_are_unresolved(native, text, reason):
    data, page = literal_page(native, text)
    tasks = parsing.plan_pdf_ocr_recovery_bounded(data, page)
    assert len(tasks) == 1 and tasks[0]["kind"] == "unresolved"
    assert tasks[0]["reason"] == reason


def test_literal_ocr_substring_does_not_become_a_recoverable_exact_token():
    data, page = literal_page("Hucho hucho", "prefixHuchohuchoSuffix")
    assert not parsing.plan_pdf_ocr_recovery_bounded(data, page)


def test_blind_literal_agreement_replaces_only_exact_span_and_keeps_all_previous_evidence(monkeypatch):
    from app import ocr_recovery, vision

    data, page = literal_page()
    original = deepcopy(page)
    requests = []
    readings = iter(["salmon (Hucho hucho)", "(Hucho hucho)\nOther visible text"])

    def read(rendered):
        assert not ({"native_text", "candidate", "target", "context", "uncertainty_exact"} & rendered.keys())
        assert "Huchohucho" not in str(
            {key: value for key, value in rendered.items() if key != "image_base64"}
        )
        requests.append(deepcopy(rendered))
        return {"text": next(readings), "uncertain_spans": [], "graphic_marks": [], "native_hint_chars": 0}

    monkeypatch.setattr(vision, "transcribe_region", read)
    first = ocr_recovery.recover_page_step(data, page, on_region=save_literal_region)
    assert first["text"] == original["text"] and first["status"] == "pending_ocr"
    result = ocr_recovery.recover_page_step(data, first, on_region=save_literal_region)
    assert result["status"] == "ocr_complete"
    assert result["text"] == "prefix (*Hucho hucho*) trailing"
    assert not result["ocr"]["uncertain_spans"]
    assert result["source_hash"] == original["source_hash"]
    assert result["native_text"] == original["native_text"]
    assert result["ocr"]["text"] == original["ocr"]["text"]
    assert result["ocr"]["image_sha256"] == original["ocr"]["image_sha256"]
    assert result["ocr"]["image_blob"] == original["ocr"]["image_blob"]
    assert result["ocr"]["recovery_history"][0]["ocr"] == original["ocr"]
    assert (
        result["ocr"]["recovery_history"][0]["text_hash"]
        == hashlib.sha256(original["text"].encode()).hexdigest()
    )
    assert len(requests) == 2 and requests[0]["region_bbox"] != requests[1]["region_bbox"]
    assert requests[0]["image_sha256"] != requests[1]["image_sha256"]
    proof = result["ocr"]["recovery"]["verifications"][0]
    assert proof["field"] == "literal" and proof["uncertainty_exact"] == "Huchohucho"
    assert proof["verified_text"] == "Hucho hucho" and len(set(proof["region_ids"])) == 2
    for region in result["ocr"]["recovery"]["regions"]:
        assert region["reading_hash"] == ocr_recovery._digest(region["reading"])
        assert region["image_blob"]["content_hash"] == region["image_sha256"]
    assert page == original


@pytest.mark.parametrize(
    "second,uncertainties",
    [
        ("Huchohuco", []),
        ("HuchoHucho", []),
        ("Huchohucho", ["unsure about this visible name"]),
        ("Huchohucho [illegible]", []),
        ("prefixHuchohuchoSuffix", []),
        ("Huchohucho and Hucho hucho", []),
    ],
)
def test_literal_disagreement_uncertain_reads_and_substrings_cannot_clear_flag(
    monkeypatch, second, uncertainties
):
    from app import ocr_recovery, vision

    data, page = literal_page()
    values = iter(
        [
            {"text": "Huchohucho", "uncertain_spans": [], "graphic_marks": []},
            {"text": second, "uncertain_spans": uncertainties, "graphic_marks": []},
        ]
    )
    monkeypatch.setattr(vision, "transcribe_region", lambda _: next(values))
    result = ocr_recovery.recover_page_step(data, page, on_region=save_literal_region)
    result = ocr_recovery.recover_page_step(data, result, on_region=save_literal_region)
    assert result["status"] == "ocr_uncertain" and result["text"] == page["text"]
    assert result["ocr"]["uncertain_spans"] == ["Huchohucho"]
    assert result["ocr"]["recovery"]["status"] == "conflict"


@pytest.mark.parametrize(
    "flag", ["model_reported_incomplete", "table_layout_not_preserved", "independent_name_issue"]
)
def test_literal_proof_does_not_clear_independent_completeness_uncertainties(monkeypatch, flag):
    from app import ocr_recovery, vision

    data, page = literal_page()
    page["ocr"]["uncertain_spans"].append(flag)
    if flag == "model_reported_incomplete":
        page["ocr"]["complete"] = False
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {
            "text": "Huchohucho",
            "uncertain_spans": [],
            "graphic_marks": [],
        },
    )
    result = ocr_recovery.recover_page_step(data, page, on_region=save_literal_region)
    result = ocr_recovery.recover_page_step(data, result, on_region=save_literal_region)
    assert result["ocr"]["uncertain_spans"] == [flag]
    assert result["status"] == "ocr_uncertain"
    assert result["ocr"]["complete"] == page["ocr"]["complete"]
    assert result["ocr"]["recovery"]["tasks"][0]["status"] == "complete"


def test_independent_validation_warning_and_model_incomplete_boolean_still_block_literal_page(monkeypatch):
    from app import ocr_recovery, vision

    data, page = literal_page()
    page["ocr"].update(complete=False, validation_warnings=["independent_table_warning"])
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {
            "text": "Huchohucho",
            "uncertain_spans": [],
            "graphic_marks": [],
        },
    )
    result = ocr_recovery.recover_page_step(data, page, on_region=save_literal_region)
    result = ocr_recovery.recover_page_step(data, result, on_region=save_literal_region)
    assert result["status"] == "ocr_uncertain" and result["ocr"]["complete"] is False
    assert result["ocr"]["uncertain_spans"] == ["model_reported_incomplete", "independent_table_warning"]
    assert result["ocr"]["validation_warnings"] == ["independent_table_warning"]


def test_structural_flag_cannot_be_proved_as_a_literal_even_if_printed_verbatim():
    data, page = literal_page(
        "model_reported_incomplete", "model_reported_incomplete", "model_reported_incomplete"
    )
    assert not parsing.plan_pdf_ocr_recovery_bounded(data, page)


def test_corrupted_first_literal_reading_cannot_be_used_after_second_frame(monkeypatch):
    from app import ocr_recovery, vision

    data, page = literal_page()
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {
            "text": "Huchohucho",
            "uncertain_spans": [],
            "graphic_marks": [],
        },
    )
    result = ocr_recovery.recover_page_step(data, page, on_region=save_literal_region)
    result["ocr"]["recovery"]["regions"][0]["reading"]["text"] += " modified after persistence"
    result = ocr_recovery.recover_page_step(data, result, on_region=save_literal_region)
    assert result["status"] == "ocr_uncertain" and result["ocr"]["uncertain_spans"] == ["Huchohucho"]


def test_compatible_literal_proof_is_reused_across_recovery_version_upgrade(monkeypatch):
    from app import ocr_recovery, vision

    data, page = literal_page()
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {
            "text": "Huchohucho",
            "uncertain_spans": [],
            "graphic_marks": [],
        },
    )
    result = ocr_recovery.recover_page_step(data, page, on_region=save_literal_region)
    result = ocr_recovery.recover_page_step(data, result, on_region=save_literal_region)
    previous_regions = deepcopy(result["ocr"]["recovery"]["regions"])
    result["ocr"]["recovery"]["version"] = "previous-compatible-region-recipe"
    result["ocr"]["uncertain_spans"] = ["Huchohucho"]
    result["status"] = "ocr_uncertain"
    monkeypatch.setattr(
        vision, "transcribe_region", lambda _: pytest.fail("compatible proof resent to model")
    )
    final = ocr_recovery.recover_page_step(data, result, on_region=save_literal_region)
    assert final["status"] == "ocr_complete"
    assert final["ocr"]["recovery"]["reused_tasks"] == 1
    assert final["ocr"]["recovery"]["regions"] == previous_regions


@pytest.mark.parametrize("modified", ["reading", "image_hash", "correction", "proof"])
def test_final_multi_literal_checkpoint_rechecks_earlier_proof_before_clearing(monkeypatch, modified):
    from app import ocr_recovery, vision

    data, page = literal_page("Hucho hucho\nSalmo salar", "Huchohucho\nSalmosalar")
    page["ocr"]["uncertain_spans"] = ["Huchohucho", "Salmosalar"]
    readings = iter(["Huchohucho", "Huchohucho", "Salmosalar", "Salmosalar"])
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {
            "text": next(readings),
            "uncertain_spans": [],
            "graphic_marks": [],
        },
    )
    result = page
    for _ in range(2):
        result = ocr_recovery.recover_page_step(data, result, on_region=save_literal_region)
    assert result["ocr"]["recovery"]["tasks"][0]["status"] == "complete"
    assert result["status"] == "pending_ocr"
    recovery = result["ocr"]["recovery"]
    if modified == "reading":
        recovery["regions"][0]["reading"]["text"] += " modified after checkpoint"
    elif modified == "image_hash":
        recovery["regions"][0]["image_blob"]["content_hash"] = "a" * 64
    elif modified == "correction":
        recovery["corrections"][0]["after"] = "unverified replacement"
    else:
        recovery["verifications"][0]["uncertainty_exact"] = "another uncertainty"
    for _ in range(2):
        result = ocr_recovery.recover_page_step(data, result, on_region=save_literal_region)
    assert result["status"] == "ocr_uncertain"
    assert result["text"] == page["text"]
    assert result["ocr"]["uncertain_spans"] == ["Huchohucho", "Salmosalar"]
