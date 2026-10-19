"""Actual PDF evidence distinguishes off-page amounts from printed conflicts."""

from copy import deepcopy
import hashlib

import pytest

from app.ingestion_quality import number_anchors, validate_ocr_page
from app.native_anchor_evidence import filter_offpage_amount_anchors
from app.ocr_recovery import plan_regions, recover_page_step
from test_pdf_text_visibility import inspect, original

AMOUNT = b"132.030.206.894.000,00"
VISIBLE = b"BT /F1 12 Tf 20 250 Td (Budget) Tj ET "
OFFPAGE = b"BT /F1 12 Tf 350 100 Td (" + AMOUNT + b") Tj ET"


def fixture(stream=VISIBLE + OFFPAGE, *, text="Budget", **options):
    content = original(stream, **options)
    native, audit = inspect(content)
    source_hash = hashlib.sha256(content).hexdigest()
    return content, {
        "page": 1,
        "source_hash": source_hash,
        "native_text": native,
        "native_visibility": audit,
        "text": text,
        "status": "ocr_complete",
        "ocr": {
            "source_hash": source_hash,
            "page": 1,
            "request_id": "saved-paid-request",
            "attempts": 1,
            "text": text,
            "complete": True,
            "uncertain_spans": [],
        },
    }


def test_unique_wholly_offpage_amount_has_bound_proof_without_changing_source():
    _, page = fixture()
    before = deepcopy(page)
    kept, proof = filter_offpage_amount_anchors(page, number_anchors(page["native_text"]))
    assert kept == [] and page == before
    assert proof["source_hash"] == page["source_hash"] and proof["page"] == 1
    assert proof["native_text_sha256"] == hashlib.sha256(page["native_text"].encode()).hexdigest()
    assert proof["excluded_amounts"][0]["native_anchor"]["text"] == AMOUNT.decode()
    validate_ocr_page(page)
    assert page["status"] == "ocr_complete"
    assert page["native_text"] == before["native_text"]
    assert page["native_visibility"] == before["native_visibility"]
    assert page["ocr"]["request_id"] == before["ocr"]["request_id"]


@pytest.mark.parametrize(
    "damage",
    [
        "source",
        "page",
        "native_hash",
        "native_text",
        "audit_version",
        "unknown",
        "missing_evidence",
        "object_hash",
        "object_order",
        "object_active",
        "object_clip",
        "object_cover",
        "matrix",
        "page_size",
        "geometry",
        "duplicate_object",
        "ocr_source",
        "ocr_page",
        "ocr_incomplete",
        "extra_nan",
    ],
)
def test_missing_stale_or_ambiguous_visibility_evidence_keeps_the_numeric_gate(damage):
    _, page = fixture()
    audit = page["native_visibility"]
    item = audit["evidence"][0]
    if damage == "source":
        audit["source_hash"] = "0" * 64
    elif damage == "page":
        audit["page"] = 2
    elif damage == "native_hash":
        audit["native_text_sha256"] = "0" * 64
    elif damage == "native_text":
        page["native_text"] += " changed"
    elif damage == "audit_version":
        audit["version"] = "unknown-version"
    elif damage == "unknown":
        audit["unknown_reasons"] = ["native_text_reader_disagreement"]
    elif damage == "missing_evidence":
        audit["evidence"] = []
    elif damage == "object_hash":
        item["text_sha256"] = "0" * 64
    elif damage == "object_order":
        item["object_order"] = audit["object_count"]
    elif damage == "object_active":
        item["active"] = False
    elif damage == "object_clip":
        item["clips"] = [{"rectangle": [0, 0, 10, 10]}]
    elif damage == "object_cover":
        item["later_paint_evidence"] = [{"kind": "filled_path"}]
    elif damage == "matrix":
        item["matrix"][0] = 0
    elif damage == "page_size":
        audit["page_size"][0] += 10
    elif damage == "geometry":
        item["bounds_pdf_bottom_left"][0] = float("nan")
    elif damage == "duplicate_object":
        audit["evidence"].append(deepcopy(item))
    elif damage == "ocr_source":
        page["ocr"]["source_hash"] = "0" * 64
    elif damage == "ocr_page":
        page["ocr"]["page"] = 2
    elif damage == "ocr_incomplete":
        page["ocr"]["complete"] = False
    elif damage == "extra_nan":
        audit["unrecognized_extra"] = float("nan")
    kept, proof = filter_offpage_amount_anchors(page, number_anchors(page["native_text"]))
    assert proof is None and any(a["field"] == "amount" for a in kept)
    validate_ocr_page(page)
    assert page["status"] == "ocr_uncertain"
    assert "native_ocr_amount_mismatch" in page["ocr"]["validation_warnings"]


@pytest.mark.parametrize(
    "stream",
    [
        VISIBLE + OFFPAGE.replace(b"350 100", b"280 100"),
        VISIBLE + b"q 20 95 10 20 re W n " + OFFPAGE.replace(b"350 100", b"20 100") + b" Q",
        VISIBLE + b"q 0 0 200 30 re W n " + OFFPAGE.replace(b"350 100", b"20 100") + b" Q",
        VISIBLE + OFFPAGE.replace(b"350 100", b"20 100") + b" 1 1 1 rg 0 90 300 30 re f",
        VISIBLE + b"1 1 1 rg " + OFFPAGE,
    ],
    ids=["partially_offpage", "partial_clip", "outside_clip", "painted_over", "white_text"],
)
def test_real_partial_clipping_and_occlusion_are_never_omitted(stream):
    _, page = fixture(stream)
    validate_ocr_page(page)
    assert page["status"] == "ocr_uncertain"
    assert "native_ocr_amount_mismatch" in page["ocr"]["uncertain_spans"]


def test_same_amount_also_on_page_cannot_be_excluded_by_offpage_copy():
    _, page = fixture(VISIBLE + OFFPAGE.replace(b"350 100", b"20 100") + b" " + OFFPAGE)
    native = number_anchors(page["native_text"])
    assert len([a for a in native if a["field"] == "amount"]) == 2
    kept, proof = filter_offpage_amount_anchors(page, native)
    assert kept == native and proof is None
    validate_ocr_page(page)
    assert page["status"] == "ocr_uncertain"


def test_excluding_offpage_amount_does_not_clear_a_different_onpage_amount_conflict():
    visible_amount = b"BT /F1 12 Tf 20 200 Td (123.456,00) Tj ET "
    _, page = fixture(VISIBLE + visible_amount + OFFPAGE)
    kept, proof = filter_offpage_amount_anchors(page, number_anchors(page["native_text"]))
    assert proof and [a["text"] for a in kept] == ["123.456,00"]
    validate_ocr_page(page)
    assert page["status"] == "ocr_uncertain"


def test_other_numeric_fields_and_model_uncertainty_keep_their_gate():
    _, page = fixture(VISIBLE + b"BT /F1 12 Tf 350 100 Td (Nomor 7 Tahun 2025) Tj ET")
    validate_ocr_page(page)
    assert "native_ocr_law_number_year_mismatch" in page["ocr"]["uncertain_spans"]
    _, page = fixture()
    page["ocr"]["uncertain_spans"] = ["name obscured"]
    validate_ocr_page(page)
    assert page["status"] == "ocr_uncertain" and page["ocr"]["uncertain_spans"] == ["name obscured"]


def test_saved_paid_text_recovery_skips_offpage_region_requests_and_keeps_history(monkeypatch):
    from app import vision

    content, page = fixture()
    page["status"] = "pending_ocr"
    page["ocr"].update(
        phase="recovery",
        uncertain_spans=["native_ocr_amount_mismatch"],
        validation_warnings=["native_ocr_amount_mismatch"],
    )
    before = deepcopy(page)
    assert plan_regions(content, page) == []
    monkeypatch.setattr(vision, "transcribe_region", lambda *_: pytest.fail("paid request repeated"))
    recovered = recover_page_step(content, page, on_region=lambda *_: pytest.fail("unneeded crop"))
    assert page == before
    assert recovered["status"] == "ocr_complete" and recovered["text"] == before["text"]
    assert recovered["ocr"]["request_id"] == before["ocr"]["request_id"]
    assert recovered["ocr"]["attempts"] == 1
    assert recovered["ocr"]["recovery_history"][0]["ocr"] == before["ocr"]
    assert recovered["native_text"] == before["native_text"]
    assert recovered["native_visibility"] == before["native_visibility"]
