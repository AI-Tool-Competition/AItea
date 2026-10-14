"""Actual PDF clipping/painting and full-page blank proof regression cases."""

import base64
import hashlib
import io
from copy import deepcopy

import pypdfium2 as pdfium
import pytest
from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject

from app import pdf_text_visibility as visibility
from app.parsing import render_pdf_page_bounded


def original(stream=b"", *, rotation=0, form=False, alpha=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    resources = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    if form:
        box = DecodedStreamObject()
        box.set_data(stream)
        box.update(
            {
                NameObject("/Type"): NameObject("/XObject"),
                NameObject("/Subtype"): NameObject("/Form"),
                NameObject("/BBox"): ArrayObject([FloatObject(v) for v in (0, 0, 300, 300)]),
                NameObject("/Resources"): resources,
            }
        )
        resources = DictionaryObject(
            {NameObject("/XObject"): DictionaryObject({NameObject("/Form"): writer._add_object(box)})}
        )
        stream = b"/Form Do"
    if alpha is not None:
        state = DictionaryObject(
            {NameObject("/Type"): NameObject("/ExtGState"), NameObject("/ca"): FloatObject(alpha)}
        )
        resources[NameObject("/ExtGState")] = DictionaryObject({NameObject("/GS"): writer._add_object(state)})
    contents = DecodedStreamObject()
    contents.set_data(stream)
    page[NameObject("/Contents")] = writer._add_object(contents)
    page[NameObject("/Resources")] = resources
    if rotation:
        page.rotate(rotation)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def inspect(content, *, optional_content=False):
    native = PdfReader(io.BytesIO(content)).pages[0].extract_text() or ""
    with pdfium.PdfDocument(content) as document:
        page = document[0]
        try:
            result = visibility.audit_page_visibility(
                page,
                native,
                source_hash=hashlib.sha256(content).hexdigest(),
                page_number=1,
                optional_content=optional_content,
            )
        finally:
            page.close()
    return native, result


TEXT = b"BT /F1 12 Tf 50 100 Td (Act 2025 No. 7) Tj ET"


def test_normal_native_text_does_not_claim_complete_visibility():
    native, audit = inspect(original(TEXT))
    assert "Act 2025" in native
    assert audit["requires_visual_review"] is False
    assert audit["visibility_complete_certified"] is False


def test_clip_disjoint_hidden_draft_is_detected_with_normal_opaque_render_mode():
    native, audit = inspect(original(b"q 0 0 200 30 re W n " + TEXT + b" Q"))
    assert "Act 2025" in native
    assert "native_text_outside_clip" in audit["reasons"]
    evidence = audit["evidence"][0]
    assert evidence["render_mode"] == 0
    assert evidence["fill_rgba"][3] == 255
    assert evidence["text"] == "Act 2025 No. 7"
    assert evidence["clips"][0]["rectangle"] == (0, 0, 200, 30)


def test_partial_clipping_does_not_silently_drop_unseen_characters():
    _, audit = inspect(original(b"q 50 95 10 20 re W n " + TEXT + b" Q"))
    assert "native_text_partially_clipped" in audit["reasons"]


def test_off_page_text_is_not_native_legal_content():
    _, audit = inspect(original(TEXT.replace(b"50 100", b"50 400")))
    assert "native_text_outside_page_bounds" in audit["reasons"]


def test_zero_area_text_does_not_become_visible_native_evidence():
    _, audit = inspect(original(b"BT /F1 12 Tf 0 0 0 0 50 100 Tm (Act No. 7) Tj ET"))
    assert audit["requires_visual_review"]
    # PDFium can omit an object with a zero transform; pypdf still reports it.
    assert (
        "degenerate_native_text_geometry" in audit["reasons"]
        or "native_text_reader_disagreement" in audit["unknown_reasons"]
    )


@pytest.mark.parametrize("mode", [3, 4, 5, 6, 7])
def test_invisible_or_clipping_text_render_modes_require_recovery(mode):
    _, audit = inspect(original(TEXT.replace(b"BT", f"BT {mode} Tr".encode())))
    assert "unsupported_or_invisible_text_render_mode" in audit["reasons"]


def test_white_text_is_preserved_and_requires_pixel_review():
    native, audit = inspect(original(b"1 1 1 rg " + TEXT))
    assert native
    assert "white_native_text_requires_pixel_review" in audit["reasons"]
    assert audit["evidence"][0]["text"]


def test_transparent_text_has_rgba_evidence_and_fails_closed():
    _, audit = inspect(original(b"/GS gs " + TEXT, alpha=0))
    assert audit["requires_visual_review"]
    assert "transparent_native_text" in audit["reasons"]
    assert audit["evidence"][0]["fill_rgba"][3] == 0


def test_later_painted_rectangle_preserves_before_after_evidence():
    _, audit = inspect(original(TEXT + b" 1 1 1 rg 40 90 200 30 re f"))
    assert "later_painted_object_overlaps_native_text" in audit["reasons"]
    evidence = audit["evidence"][0]
    paint = evidence["later_paint_evidence"][0]
    assert paint["object_order"] > evidence["object_order"]
    assert paint["fill_rgba"] == [255, 255, 255, 255]


def test_background_painted_before_text_does_not_claim_occlusion():
    _, audit = inspect(original(b"1 1 1 rg 40 90 200 30 re f 0 0 0 rg " + TEXT))
    assert "later_painted_object_overlaps_native_text" not in audit["reasons"]
    assert audit["requires_visual_review"] is False


def test_complex_clip_is_unknown_instead_of_assumed_visible():
    _, audit = inspect(original(b"q 0 0 m 290 0 l 150 290 l h W n " + TEXT + b" Q"))
    assert "native_text_clip_geometry_unverified" in audit["reasons"]


def test_form_coordinates_are_not_compared_as_page_coordinates():
    _, audit = inspect(original(TEXT, form=True))
    assert "form_visibility_geometry_unverified" in audit["unknown_reasons"]
    assert audit["requires_visual_review"]


def test_optional_content_visibility_is_not_inferred_from_extract_text():
    _, audit = inspect(original(TEXT), optional_content=True)
    assert "optional_content_visibility_unverified" in audit["unknown_reasons"]


def test_rotation_and_object_budget_do_not_certify_native_text(monkeypatch):
    _, audit = inspect(original(TEXT, rotation=90))
    assert "rotated_page_geometry_unverified" in audit["unknown_reasons"]
    monkeypatch.setattr(visibility, "_MAX_OBJECTS", 0)
    _, audit = inspect(original(TEXT))
    assert audit["requires_visual_review"]
    assert any(value.startswith("visibility_evidence_unavailable") for value in audit["unknown_reasons"])


def test_bad_source_identity_is_rejected_before_pdf_access():
    with pytest.raises(ValueError, match="原件"):
        visibility.audit_page_visibility(None, "", source_hash="bad", page_number=1)


def test_native_claim_without_matching_page_objects_is_not_certified():
    content = original()
    with pdfium.PdfDocument(content) as document:
        page = document[0]
        try:
            audit = visibility.audit_page_visibility(
                page, "Act No. 7", source_hash=hashlib.sha256(content).hexdigest(), page_number=1
            )
        finally:
            page.close()
    assert "native_text_reader_disagreement" in audit["unknown_reasons"]
    assert audit["requires_visual_review"]


@pytest.fixture
def blank_evidence():
    content = original()
    native, audit = inspect(content)
    return native, audit, render_pdf_page_bounded(content, 1)


def test_source_bound_full_white_page_can_be_locally_certified(blank_evidence):
    native, audit, rendered = blank_evidence
    proof = visibility.prove_blank_page(rendered, audit, native_text=native)
    assert proof and proof["is_blank"] and proof["model_calls"] == 0
    assert proof["image_sha256"] == hashlib.sha256(base64.b64decode(proof["image_base64"])).hexdigest()
    assert proof["source_hash"] == audit["source_hash"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("source_hash", "0" * 64),
        ("page", 2),
        ("dpi", 400),
        ("width", 1),
        ("render_version", "bounded-region-png-v1"),
        ("image_sha256", "f" * 64),
    ],
)
def test_changed_identity_crop_geometry_or_pixels_cannot_be_blank(blank_evidence, key, value):
    native, audit, rendered = blank_evidence
    changed = {**rendered, key: value}
    assert visibility.prove_blank_page(changed, audit, native_text=native) is None


def test_nonwhite_pixel_is_not_erased_by_blank_shortcut(blank_evidence):
    native, audit, rendered = blank_evidence
    image = Image.open(io.BytesIO(base64.b64decode(rendered["image_base64"])))
    image.putpixel((10, 10), (254, 255, 255))
    target = io.BytesIO()
    image.save(target, format="PNG")
    image.close()
    changed = {
        **rendered,
        "image_base64": base64.b64encode(target.getvalue()).decode(),
        "image_sha256": hashlib.sha256(target.getvalue()).hexdigest(),
    }
    assert visibility.prove_blank_page(changed, audit, native_text=native) is None


def test_transparent_white_pixels_are_not_opaque_blank_evidence(blank_evidence):
    native, audit, rendered = blank_evidence
    image = Image.new("RGBA", (rendered["width"], rendered["height"]), (255, 255, 255, 0))
    target = io.BytesIO()
    image.save(target, format="PNG")
    image.close()
    changed = {
        **rendered,
        "image_base64": base64.b64encode(target.getvalue()).decode(),
        "image_sha256": hashlib.sha256(target.getvalue()).hexdigest(),
    }
    assert visibility.prove_blank_page(changed, audit, native_text=native) is None


def test_inconsistent_review_flag_does_not_overrule_recorded_unknowns(blank_evidence):
    native, audit, rendered = blank_evidence
    changed = {**audit, "unknown_reasons": ["source_unknown"]}
    assert visibility.prove_blank_page(rendered, changed, native_text=native) is None


def test_unknown_annotation_or_nonempty_native_is_not_blank(blank_evidence):
    native, audit, rendered = blank_evidence
    changed = deepcopy(audit)
    changed["annotation_count"] = 1
    assert visibility.prove_blank_page(rendered, changed, native_text=native) is None
    assert visibility.prove_blank_page(rendered, audit, native_text="Hidden Act") is None
