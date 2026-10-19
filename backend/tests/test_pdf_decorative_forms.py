"""Flat line-only Forms do not hide text or bypass paint/clip evidence."""

import hashlib
import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject

from app import pdf_text_visibility as visibility
from app.ingestion_quality import text_complete
from test_pdf_text_visibility import TEXT, inspect, original
from test_pdf_visibility_ingestion import parse

LINE = b"q 1 0 0 1 50 250 cm 1 w 0 0 m 200 0 l S Q"


def decorated(lines=LINE, *, after=False, matrix=None, nested=False, root_text=TEXT):
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(original(root_text))))
    page = writer.pages[0]
    resources = DictionaryObject(dict(page["/Resources"]))
    form = DecodedStreamObject()
    form.set_data(lines)
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([FloatObject(v) for v in (0, 0, 300, 300)]),
            NameObject("/Resources"): resources,
        }
    )
    if nested:
        inner = DecodedStreamObject()
        inner.set_data(LINE)
        inner.update({key: value for key, value in form.items() if key != "/Resources"})
        resources[NameObject("/XObject")] = DictionaryObject(
            {NameObject("/Nested"): writer._add_object(inner)}
        )
        form.set_data(b"/Nested Do")
    page[NameObject("/Resources")] = DictionaryObject(dict(page["/Resources"]))
    page["/Resources"][NameObject("/XObject")] = DictionaryObject(
        {NameObject("/Decoration"): writer._add_object(form)}
    )
    contents = DecodedStreamObject()
    invocation = b" /Decoration Do "
    if matrix is not None:
        invocation = b" q " + " ".join(str(value) for value in matrix).encode() + b" cm" + invocation + b" Q "
    contents.set_data(root_text + invocation if after else invocation + root_text)
    page[NameObject("/Contents")] = writer._add_object(contents)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_translated_header_rules_preserve_native_text_and_source_bound_proof():
    content = decorated(LINE + b" " + LINE.replace(b"250 cm", b"253 cm"))
    native, audit = inspect(content)
    assert native == inspect(original(TEXT))[0]
    assert audit["source_hash"] == hashlib.sha256(content).hexdigest()
    assert audit["native_text_sha256"] == hashlib.sha256(native.encode()).hexdigest()
    assert not audit["requires_visual_review"]
    assert not audit["visibility_complete_certified"]
    form = audit["decorative_forms"][0]
    assert form["version"] == "pdf-decorative-form-lines-v1"
    assert form["object_order"] == 0 and form["child_count"] == 2
    assert form["matrix"] == [1, 0, 0, 1, 0, 0] and form["clips"] == []
    assert form["children"][0]["matrix"] == [1, 0, 0, 1, 50, 250]
    assert form["children"][0]["draw_mode"] == {"fill": 0, "stroke": 1}


@pytest.mark.parametrize("archive", [False, True])
def test_bounded_ingestion_accepts_proved_lines_and_keeps_page_mapping(tmp_path, archive):
    result = parse(tmp_path, decorated(), archive)
    assert text_complete(result)
    page = result["pages"][0]
    assert page["native_text"] == inspect(original(TEXT))[0]
    assert page["page"] == page["native_visibility"]["page"] == 1
    assert page["source_hash"] == page["native_visibility"]["source_hash"] == result["source_hash"]
    assert page["native_visibility"]["decorative_forms"][0]["child_count"] == 1


@pytest.mark.parametrize("color", [b"0 0 0 RG", b"1 1 1 RG"])
def test_later_form_stroke_over_text_retains_occlusion_gate(color):
    lines = color + b" 8 w 40 104 m 240 104 l S"
    native, audit = inspect(decorated(lines, after=True))
    assert "Act 2025" in native
    assert "later_painted_object_overlaps_native_text" in audit["reasons"]
    assert audit["requires_visual_review"]
    row = audit["evidence"][0]
    paint = row["later_paint_evidence"][0]
    assert paint["kind"] == "decorative_form_stroke"
    assert paint["object_order"] > row["object_order"]
    assert paint["stroke_width"] == 8


def test_rules_do_not_launder_hidden_clipped_bill_text():
    content = decorated(root_text=b"q 0 0 200 30 re W n " + TEXT + b" Q")
    native, audit = inspect(content)
    assert "Act 2025" in native and audit["decorative_forms"]
    assert "native_text_outside_clip" in audit["reasons"]
    assert audit["requires_visual_review"]


@pytest.mark.parametrize(
    "lines",
    [
        LINE + b" " + TEXT,  # Root reader agreement cannot admit child text.
        b"50 240 200 20 re f",  # Filled paths can cover a whole paragraph.
        b"50 240 m 80 270 170 230 250 250 c S",  # Curves have different envelopes.
        b"50 240 m 250 260 l S",  # Diagonals need transformed stroke geometry.
        b"q 2 0 0 2 0 0 cm 25 125 m 125 125 l S Q",
        b"q 0 0 100 100 re W n " + LINE + b" Q",
        b"0 w 50 250 m 250 250 l S",  # Hairlines depend on device pixels.
        b"20 w 50 250 m 250 250 l S",
        b" ".join([LINE] * 9),
    ],
)
def test_unsupported_form_content_remains_unknown(lines):
    _, audit = inspect(decorated(lines))
    assert "form_visibility_geometry_unverified" in audit["unknown_reasons"]
    assert audit["requires_visual_review"]
    assert "decorative_forms" not in audit


@pytest.mark.parametrize("matrix", [(1, 0, 0, 1, 5, 0), (0, 1, -1, 0, 300, 0)])
def test_nonidentity_form_matrix_remains_unknown(matrix):
    _, audit = inspect(decorated(matrix=matrix))
    assert "form_visibility_geometry_unverified" in audit["unknown_reasons"]
    assert audit["requires_visual_review"]


def test_nested_form_is_not_traversed_or_accepted():
    _, audit = inspect(decorated(nested=True))
    assert "form_visibility_geometry_unverified" in audit["unknown_reasons"]
    assert audit["requires_visual_review"]


def test_children_consume_remaining_page_object_budget(monkeypatch):
    monkeypatch.setattr(visibility, "_MAX_OBJECTS", 2)  # One Form and one text object.
    _, audit = inspect(decorated())
    assert "form_visibility_geometry_unverified" in audit["unknown_reasons"]
    assert "decorative_forms" not in audit


def test_missing_stroke_api_keeps_review_gate(monkeypatch):
    import pypdfium2.raw as raw

    monkeypatch.delattr(raw, "FPDFPageObj_GetStrokeWidth")
    _, audit = inspect(decorated())
    assert audit["requires_visual_review"]
    assert "visibility_evidence_unavailable:AttributeError" in audit["unknown_reasons"]


def test_forms_do_not_clear_optional_content_gate():
    _, audit = inspect(decorated(), optional_content=True)
    assert audit["decorative_forms"]
    assert "optional_content_visibility_unverified" in audit["unknown_reasons"]
    assert audit["requires_visual_review"]
