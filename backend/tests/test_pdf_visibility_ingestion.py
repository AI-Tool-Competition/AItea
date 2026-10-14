"""Visibility decisions reach bounded parsing, resume, indexing and provenance."""

import hashlib
from copy import deepcopy

import pytest

from app.archive_parsing import parse_archive_pdf_bounded
from app.ingestion import rebuild_pdf_manifest
from app.ingestion_quality import text_complete
from app.native_mapping_inventory import mappings
from app.parsing import parse_bounded, rebuild_pdf_bounded
from test_pdf_text_visibility import TEXT, original
from test_ingestion_quality import make_pdf


def parse(tmp_path, content, archive):
    if not archive:
        return parse_bounded("official.pdf", content)
    path = tmp_path / "official.pdf"
    path.write_bytes(content)
    return parse_archive_pdf_bounded(path, source_hash=hashlib.sha256(content).hexdigest(), size=len(content))


@pytest.mark.parametrize("archive", [False, True])
def test_hidden_clipped_draft_never_passes_body_completeness(tmp_path, archive):
    content = original(b"q 0 0 200 30 re W n " + TEXT + b" Q")
    result = parse(tmp_path, content, archive)
    page = result["pages"][0]
    assert "Act 2025" in page["native_text"]
    assert "Act 2025" in page["text"]  # Preserve the original extraction as evidence.
    assert page["native_visibility"]["requires_visual_review"]
    assert "native_text_outside_clip" in page["native_visibility"]["reasons"]
    assert "native_visibility_review" in page["reasons"]
    assert not text_complete(result)
    forged = deepcopy(result)
    forged["quality"]["complete"] = True
    forged["pages"][0].update(status="native", reasons=[], ocr_required=False)
    assert not text_complete(forged)


@pytest.mark.parametrize("archive", [False, True])
def test_blank_source_requires_persisted_pixel_proof_and_exportable_mapping(tmp_path, archive):
    result = parse(tmp_path, original(), archive)
    assert text_complete(result)
    assert result["chunks"] == []
    proof = result["pages"][0]["native_blank"]
    assert proof["model_calls"] == 0 and proof["image_size"] > 0
    refs = list(mappings(result, "a" * 64))
    blanks = [row[2] for row in refs if row[0] == "pdf_native_blank_page"]
    assert len(blanks) == 1
    assert blanks[0]["bytes_reference"]["content_hash"] == proof["image_sha256"]
    assert proof["image_base64"] not in str(refs)
    altered = deepcopy(result)
    altered["pages"][0]["native_blank"]["image_sha256"] = "b" * 64
    assert not text_complete(altered)
    with pytest.raises(ValueError, match="blank-page"):
        list(mappings(altered, "a" * 64))


def test_empty_native_layer_does_not_make_painted_source_blank(tmp_path):
    result = parse(tmp_path, original(b"0 0 0 rg 20 20 100 100 re f"), False)
    assert result["quality"]["pending_pages"] == [1]
    assert "native_blank" not in result["pages"][0]
    assert not text_complete(result)


@pytest.mark.parametrize("archive", [False, True])
def test_scan_avoids_expanding_visibility_objects_and_keeps_mandatory_ocr(tmp_path, archive):
    content = make_pdf([("Original noisy scan layer.", True)])
    result = parse(tmp_path, content, archive)
    page = result["pages"][0]
    audit = page["native_visibility"]
    assert audit["audit_scope"] == "not_inspected_raster_page_already_pending_ocr"
    assert audit["evidence"] == [] and "object_count" not in audit
    assert audit["requires_visual_review"]
    assert page["native_text"] == "Original noisy scan layer."
    assert "large_raster" in page["reasons"]
    assert not text_complete(result)
    page.update(status="native", reasons=[], ocr_required=False)
    result["quality"]["complete"] = True
    assert not text_complete(result)


@pytest.mark.parametrize("field", ["source_hash", "page", "native_text_sha256"])
def test_native_audit_cannot_be_reused_for_changed_source_page_or_text(tmp_path, field):
    result = parse(tmp_path, original(TEXT), False)
    assert text_complete(result)
    result["pages"][0]["native_visibility"][field] = 2 if field == "page" else "a" * 64
    assert not text_complete(result)


def test_new_pipeline_cannot_launder_missing_native_audit(tmp_path):
    result = parse(tmp_path, original(TEXT), False)
    result["pages"][0].pop("native_visibility")
    assert not text_complete(result)
    with pytest.raises(ValueError, match="可见性检查点缺失"):
        rebuild_pdf_bounded(result)
    # Historical manifests retain their semantics until explicit revalidation.
    result["pipeline_version"] = "pdf-pages-v4"
    assert text_complete(result)


@pytest.mark.parametrize("field", ["source_hash", "page"])
def test_page_and_audit_cannot_together_move_outside_the_manifest_source(tmp_path, field):
    result = parse(tmp_path, original(TEXT), False)
    value = "b" * 64 if field == "source_hash" else 2
    result["pages"][0][field] = value
    result["pages"][0]["native_visibility"][field] = value
    assert not text_complete(result)
    with pytest.raises(ValueError, match="原件哈希或物理页码"):
        rebuild_pdf_bounded(result)


def test_archive_rebuild_keeps_new_protocol_when_native_audit_is_missing(tmp_path):
    result = parse(tmp_path, original(TEXT), True)
    result["pages"][0].pop("native_visibility")
    rebuilt = rebuild_pdf_bounded(result)
    assert rebuilt["pipeline_version"].startswith("archive-pdf-pages-v3+")
    assert not text_complete(rebuilt)
    assert rebuilt["quality"]["pending_parse_pages"] == [1]


def test_archived_native_checkpoint_is_revalidated_without_changing_source(tmp_path):
    content = original(b"q 0 0 200 30 re W n " + TEXT + b" Q")
    result = parse(tmp_path, content, True)
    old = deepcopy(result)
    old["pages"][0].pop("native_visibility")
    old["pages"][0].update(status="native", reasons=[], ocr_required=False)
    old["quality"]["complete"] = True
    old["pipeline_version"] = "archive-pdf-pages-v2"
    result = parse_archive_pdf_bounded(
        tmp_path / "official.pdf", source_hash=old["source_hash"], size=len(content), manifest=old
    )
    assert not text_complete(result)
    assert result["pages"][0]["native_text"] == old["pages"][0]["native_text"]
    assert hashlib.sha256((tmp_path / "official.pdf").read_bytes()).hexdigest() == old["source_hash"]


def test_verified_ocr_keeps_native_visibility_history_and_new_pipeline(tmp_path):
    result = parse(tmp_path, original(b"q 0 0 200 30 re W n " + TEXT + b" Q"), False)
    audit = deepcopy(result["pages"][0]["native_visibility"])
    result["pages"][0].update(
        status="ocr_complete", text="Visible enacted law.", ocr={"complete": True, "uncertain_spans": []}
    )
    rebuilt = rebuild_pdf_manifest(result)
    assert text_complete(rebuilt)
    assert rebuilt["pipeline_version"].startswith("pdf-pages-v5+")
    assert rebuilt["pages"][0]["native_visibility"] == audit
