"""Missing decoded glyphs cannot enter RAG through cached or resumed pages."""

from copy import deepcopy
import hashlib

import pytest

from app.archive_parsing import archive_rebuild_bounded, parse_archive_pdf_bounded
from app.ingestion import parse_file, pdf_quality, rebuild_pdf_manifest
from app.ingestion_quality import ocr_pending, text_complete, validate_ocr_page
from test_pdf_fonts import missing_character_pdf


def cached_broken_manifest():
    parsed = parse_file("missing-digit.pdf", missing_character_pdf())
    parsed["pages"][0].update(status="native", ocr_required=False, reasons=[])
    parsed["quality"].update(status="complete", complete=True, pending_pages=[], completed_pages=[1])
    return parsed


def test_old_complete_flag_cannot_accept_a_current_missing_native_glyph():
    manifest = cached_broken_manifest()
    before = deepcopy(manifest)
    assert text_complete(manifest) is False
    assert ocr_pending(manifest) == [1]
    assert pdf_quality(manifest["pages"])["pending_pages"] == [1]
    assert manifest == before  # Eligibility does not rewrite a stored source.
    rebuilt = rebuild_pdf_manifest(manifest)
    assert rebuilt["pages"][0]["status"] == "pending_ocr"
    assert rebuilt["pages"][0]["native_text"] == before["pages"][0]["native_text"]
    assert rebuilt["source_hash"] == before["source_hash"]
    assert rebuilt["quality"]["complete"] is False


def test_finishing_other_ocr_pages_does_not_complete_an_old_broken_native_page():
    manifest = parse_file("two-pages.pdf", missing_character_pdf(copies=2))
    manifest["pages"][0].update(status="native", reasons=[], ocr_required=False)
    manifest["pages"][1].update(
        status="ocr_complete", text="Tea 123", ocr={"complete": True, "uncertain_spans": []}
    )
    rebuilt = rebuild_pdf_manifest(manifest)
    assert rebuilt["quality"]["completed_pages"] == [2]
    assert rebuilt["quality"]["pending_pages"] == [1]
    assert not rebuilt["quality"]["complete"]
    assert all(page["source_hash"] == rebuilt["source_hash"] for page in rebuilt["pages"])


def test_clean_complete_ocr_replaces_current_text_but_keeps_the_broken_native_evidence():
    manifest = parse_file("missing-digit.pdf", missing_character_pdf())
    before = deepcopy(manifest["pages"][0])
    page = manifest["pages"][0]
    page.update(status="ocr_complete", text="Tea 123", ocr={"complete": True, "uncertain_spans": []})
    rebuilt = rebuild_pdf_manifest(manifest)
    assert rebuilt["quality"]["complete"] and text_complete(rebuilt)
    assert "native_decode_replacement" not in page["reasons"]
    assert page["native_visibility"] == before["native_visibility"]
    assert "native_visibility_review" in page["reasons"]
    assert page["native_text"] == before["native_text"] == "Tea \ufffd23"
    assert page["source_hash"] == before["source_hash"]
    assert page["text"] == "Tea 123"


def test_replacement_in_current_ocr_text_is_uncertain_until_it_is_really_replaced():
    manifest = cached_broken_manifest()
    page = manifest["pages"][0]
    page.update(status="ocr_complete", ocr={"complete": True, "uncertain_spans": []})
    validate_ocr_page(page)
    assert page["status"] == "ocr_uncertain"
    assert page["ocr"]["validation_warnings"] == ["ocr_decode_replacement"]
    assert not text_complete({**manifest, "quality": {"complete": True}})
    page["text"] = "Tea 123"
    validate_ocr_page(page)
    assert page["status"] == "ocr_complete"
    assert page["ocr"]["validation_warnings"] == page["ocr"]["uncertain_spans"] == []
    assert page["native_text"] == "Tea \ufffd23"


@pytest.mark.parametrize(
    "metadata,warning",
    [
        ({"complete": False, "uncertain_spans": []}, "model_reported_incomplete"),
        ({"complete": True, "validation_warnings": ["independent_warning"]}, "independent_warning"),
        ({"complete": True, "uncertain_spans": ["independent_warning"]}, "independent_warning"),
    ],
)
def test_clean_transcription_cannot_clear_incomplete_models_or_independent_warnings(metadata, warning):
    manifest = parse_file("missing-digit.pdf", missing_character_pdf())
    page = manifest["pages"][0]
    page.update(status="ocr_complete", text="Tea 123", ocr=metadata)
    validate_ocr_page(page)
    assert page["status"] == "ocr_uncertain"
    assert warning in page["ocr"]["uncertain_spans"]
    assert "native_decode_replacement" in page["reasons"]
    assert not pdf_quality([page])["complete"]
    assert not text_complete({**manifest, "quality": {"complete": True}})


def test_archive_worker_and_cached_restart_revalidate_missing_native_characters(tmp_path):
    content = missing_character_pdf()
    path = tmp_path / "original.pdf"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    parsed = parse_archive_pdf_bounded(path, source_hash=digest, size=len(content), max_pages=1)
    assert set(parsed["pages"][0]["reasons"]) == {"native_decode_replacement", "native_visibility_review"}
    assert parsed["archive_pdf"]["complete"] is True
    assert parsed["quality"]["complete"] is False
    parsed["pages"][0].update(status="native", reasons=[], ocr_required=False)
    parsed["quality"]["complete"] = True
    before = deepcopy(parsed["pages"][0])
    rebuilt = archive_rebuild_bounded(parsed)
    assert rebuilt["quality"]["pending_pages"] == [1]
    assert rebuilt["pages"][0]["status"] == "pending_ocr"
    assert rebuilt["pages"][0]["native_text"] == before["native_text"]
    assert rebuilt["pages"][0]["source_hash"] == digest
    resumed = parse_archive_pdf_bounded(path, source_hash=digest, size=len(content), manifest=parsed)
    assert resumed["quality"]["complete"] is False
    assert "native_decode_replacement" in resumed["pages"][0]["reasons"]
    assert resumed["pages"][0]["native_visibility"] == before["native_visibility"]
    assert resumed["pages"][0]["native_visibility"]["requires_visual_review"]
