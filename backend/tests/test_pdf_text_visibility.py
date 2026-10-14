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


def inspect(content, *, optional_content=False, native_override=None):
    native = PdfReader(io.BytesIO(content)).pages[0].extract_text() or ""
    if native_override is not None:
        native = native_override
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


HYPHEN_TEXT = b"BT /F1 12 Tf 1 0 0 1 10 140 Tm (undang-) Tj 0 -20 Td (undang123) Tj ET"


def test_api_proved_line_end_hyphen_has_only_derived_exact_comparison():
    native, audit = inspect(original(HYPHEN_TEXT))
    assert native == "undang-\nundang123"
    assert audit["native_text_sha256"] == hashlib.sha256(native.encode()).hexdigest()
    assert audit["requires_visual_review"] is False
    assert audit["visibility_complete_certified"] is False
    proof = audit["reader_comparison"]
    assert proof["native_text_unchanged"] and proof["purpose"] == "derived_reader_comparison_only"
    assert proof["normalization"] == "NFKC_nonspace_exact_order"
    assert proof["native_text_sha256"] == audit["native_text_sha256"]
    assert proof["pypdfium2_version"].startswith("5.14.0")
    assert proof["pdfium_version"] == "156.0.8076.0"
    assert len(proof["markers"]) == 1
    assert proof["markers"][0] == {
        "pdfium_char_index": 6,
        "normalized_nonspace_index": 6,
        "unicode": 2,
        "is_hyphen": 1,
        "is_generated": 0,
        "unicode_map_error": 0,
        "comparison_character": "-",
        "page_text_object_order": 0,
        "native_char_index": 6,
    }


@pytest.mark.parametrize(
    "function,value",
    [
        ("FPDFText_IsHyphen", 0),
        ("FPDFText_IsHyphen", -1),
        ("FPDFText_IsGenerated", 1),
        ("FPDFText_IsGenerated", -1),
        ("FPDFText_HasUnicodeMapError", 1),
        ("FPDFText_HasUnicodeMapError", -1),
    ],
)
def test_unproved_hyphen_flags_keep_reader_disagreement(monkeypatch, function, value):
    import pypdfium2.raw as raw

    monkeypatch.setattr(raw, function, lambda *args: value)
    native, audit = inspect(original(HYPHEN_TEXT))
    assert native == "undang-\nundang123"
    assert "reader_comparison" not in audit
    assert "native_text_reader_disagreement" in audit["unknown_reasons"]
    assert audit["requires_visual_review"]


def test_missing_hyphen_api_keeps_reader_disagreement(monkeypatch):
    import pypdfium2.raw as raw

    monkeypatch.delattr(raw, "FPDFText_IsHyphen")
    _, audit = inspect(original(HYPHEN_TEXT))
    assert "reader_comparison" not in audit
    assert "native_text_reader_disagreement" in audit["unknown_reasons"]


def test_literal_control_character_is_not_guessed_as_a_hyphen():
    _, audit = inspect(original(b"BT /F1 12 Tf 10 100 Td (A\x02B) Tj ET"), native_override="A-B")
    assert "reader_comparison" not in audit
    assert "native_text_reader_disagreement" in audit["unknown_reasons"]


@pytest.mark.parametrize(
    "claimed",
    [
        "undang-\nundang128",
        "gnadnu-\nundang123",
        "undangundang123-",
        "undang\nundang123",
        "undang－\nundang123",
    ],
)
def test_hyphen_alias_requires_full_source_character_order_and_numbers(claimed):
    _, audit = inspect(original(HYPHEN_TEXT), native_override=claimed)
    assert "reader_comparison" not in audit
    assert "native_text_reader_disagreement" in audit["unknown_reasons"]
    assert audit["requires_visual_review"]


def test_one_valid_marker_cannot_alias_an_unmarked_literal_on_the_same_page():
    mixed = HYPHEN_TEXT + b" BT /F1 12 Tf 10 80 Td (A\x02B) Tj ET"
    _, audit = inspect(original(mixed), native_override="undang-\nundang123\nA-B")
    assert "reader_comparison" not in audit
    assert "native_text_reader_disagreement" in audit["unknown_reasons"]


def test_hyphen_comparison_requires_its_text_object_identity(monkeypatch):
    import pypdfium2.raw as raw

    monkeypatch.setattr(raw, "FPDFText_GetTextObject", lambda *args: None)
    _, audit = inspect(original(HYPHEN_TEXT))
    assert "reader_comparison" not in audit
    assert "native_text_reader_disagreement" in audit["unknown_reasons"]


def test_hyphen_comparison_budget_cannot_relax_reader_disagreement(monkeypatch):
    monkeypatch.setattr(visibility, "_MAX_COMPARISON_CHARS", 5)
    _, audit = inspect(original(HYPHEN_TEXT))
    assert "reader_comparison" not in audit
    assert "native_text_reader_disagreement" in audit["unknown_reasons"]


@pytest.mark.parametrize("fault", ["clip", "cover", "optional", "invisible", "form"])
def test_hyphen_comparison_never_clears_independent_visibility_faults(fault):
    stream = HYPHEN_TEXT
    if fault == "clip":
        stream = b"q 0 0 200 30 re W n " + stream + b" Q"
    elif fault == "cover":
        stream += b" 1 1 1 rg 0 100 250 80 re f"
    elif fault == "invisible":
        stream = stream.replace(b"BT", b"BT 3 Tr")
    _, audit = inspect(original(stream, form=fault == "form"), optional_content=fault == "optional")
    assert audit["requires_visual_review"]
    assert audit["reasons"] or audit["unknown_reasons"]
    if fault == "form":
        assert "reader_comparison" not in audit
        assert "form_visibility_geometry_unverified" in audit["unknown_reasons"]
    else:
        assert "reader_comparison" in audit
        assert "native_text_reader_disagreement" not in audit["unknown_reasons"]


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
