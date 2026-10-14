"""Real PDF path syntax tests for table hints, transforms, and unseen forms."""

import io

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
)

from app import pdf_table_detection as detection


def page(data, form=None):
    writer = PdfWriter()
    p = writer.add_blank_page(width=1000, height=1000)
    stream = DecodedStreamObject()
    stream.set_data(data)
    p[NameObject("/Contents")] = writer._add_object(stream)
    if form:
        p[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): DictionaryObject({NameObject("/Grid"): writer._add_object(form)})}
        )
    target = io.BytesIO()
    writer.write(target)
    return PdfReader(io.BytesIO(target.getvalue())).pages[0]


def classify(data, form=None):
    p = page(data, form)
    return detection.classify_table_layout(p.get_contents(), p.get("/Resources", {}))


def grid(stroke=False):
    if stroke:
        return b"\n".join(
            [f"50 {y} m 450 {y} l S".encode() for y in (100, 200, 300)]
            + [f"{x} 100 m {x} 300 l S".encode() for x in (50, 250, 450)]
        )
    return b"\n".join(
        [f"50 {y} 400 0.5 re f".encode() for y in (100, 200, 300)]
        + [f"{x} 100 0.5 200 re f".encode() for x in (50, 250, 450)]
    )


@pytest.mark.parametrize("stroke", [False, True])
def test_complete_painted_grid_retains_structure_review(stroke):
    result = classify(grid(stroke))
    assert result["classification"] == "connected_painted_grid"
    assert result["requires_table_review"]
    assert result["plain_text_complete_certified"] is False


@pytest.mark.parametrize("end", [b"n", b"W n", b"W* n"])
def test_unpainted_paths_and_clip_boundaries_are_not_grid(end):
    result = classify(grid(True).replace(b" S", b"") + b"\n" + end)
    assert result["classification"] == "no_connected_painted_grid"


def test_transforms_cannot_merge_distant_local_rules_into_phantom_grid():
    data = b"\n".join(
        [
            b"q 1 0 0 1 0 0 cm 50 100 400 0.5 re f Q",
            b"q 1 0 0 1 0 500 cm 50 200 400 0.5 re f Q",
            b"q 1 0 0 1 0 1000 cm 50 300 400 0.5 re f Q",
        ]
        + [f"q 1 0 0 1 2000 0 cm {x} 100 0.5 200 re f Q".encode() for x in (50, 250, 450)]
    )
    result = classify(data)
    assert result["requires_table_review"] is False
    assert result["counts"]["ctm_updates"] == 6
    assert result["counts"]["graphics_restores"] == 6


def test_q_q_restore_position_for_a_real_grid_after_transformed_art():
    result = classify(b"q 1 0 0 1 800 900 cm 0 0 20 20 re f Q\n" + grid())
    assert result["classification"] == "connected_painted_grid"
    assert result["grid"]["bounds"][0] < 51


@pytest.mark.parametrize("matrix", [b"2 0 0 2 20 30", b"0 1 -1 0 500 20"])
def test_scaled_and_right_angle_rotated_real_grids_are_detected(matrix):
    result = classify(b"q " + matrix + b" cm\n" + grid(True) + b"\nQ")
    assert result["classification"] == "connected_painted_grid"


def test_curved_filled_icons_do_not_paint_table_rules():
    # Each filled contour has a long straight edge and a curved return path.
    horizontal = [f"50 {y} m 450 {y} l 450 {y + 30} 50 {y + 30} 50 {y} c f".encode() for y in (100, 200, 300)]
    vertical = [f"{x} 100 m {x} 300 l {x + 30} 300 {x + 30} 100 {x} 100 c f".encode() for x in (50, 250, 450)]
    result = classify(b"\n".join(horizontal + vertical))
    assert result["requires_table_review"] is False
    assert result["counts"]["fill_only_curved_paths_excluded"] == 6
    assert result["horizontal_rules"] == result["vertical_rules"] == 0


def test_straight_stroked_segments_remain_grid_evidence_in_a_curved_contour():
    # A rounded decorative closure must not erase actual straight stroke rules.
    data = b"\n".join(
        [f"50 {y} m 450 {y} l 450 {y + 3} 50 {y + 3} 50 {y} c S".encode() for y in (100, 200, 300)]
        + [f"{x} 100 m {x} 300 l {x + 3} 300 {x + 3} 100 {x} 100 c S".encode() for x in (50, 250, 450)]
    )
    assert classify(data)["classification"] == "connected_painted_grid"


def test_nested_form_grid_is_inspected_with_form_and_page_transforms():
    form = DecodedStreamObject()
    form.set_data(grid())
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject([FloatObject(x) for x in (0, 0, 500, 500)]),
            NameObject("/Matrix"): ArrayObject([FloatObject(x) for x in (2, 0, 0, 2, 5, 7)]),
            NameObject("/Resources"): DictionaryObject(),
        }
    )
    result = classify(b"q 1 0 0 1 30 40 cm /Grid Do Q", form)
    assert result["classification"] == "connected_painted_grid"
    assert result["counts"]["forms"] == 1
    assert result["grid"]["bounds"][0] > 130


def test_unseen_form_resource_remains_unknown_not_no_table():
    p = page(b"/NotAvailable Do")
    result = detection.classify_table_layout(p.get_contents())
    assert result["classification"] == "unknown"
    assert result["requires_table_review"] is True


@pytest.mark.parametrize(
    "data,reason",
    [
        (b"Q", "unbalanced_graphics_state"),
        (b"q", "unbalanced_graphics_state"),
        (b"0 0 0 0 0 0 cm", "singular_transform"),
        (b"1 2 l S", "line_without_moveto"),
        (b"/Unknown gs", "missing_graphics_state_resource"),
    ],
)
def test_invalid_geometry_fails_closed(data, reason):
    result = classify(data)
    assert result["classification"] == "unknown"
    assert result["unknown_reason"] == reason
    assert result["requires_table_review"]


def test_resource_limits_fail_closed(monkeypatch):
    monkeypatch.setattr(detection, "_MAX_OPERATIONS", 2)
    result = classify(grid())
    assert result["classification"] == "unknown"
    assert result["unknown_reason"] == "operation_limit"


def test_none_stream_does_not_certify_raster_or_text():
    result = detection.classify_table_layout(None)
    assert result["requires_table_review"] is False
    assert result["raster_text_or_table_certified"] is False


def test_expanded_stream_budget_fails_closed(monkeypatch):
    monkeypatch.setattr(detection, "_MAX_TOTAL_STREAM_BYTES", 2)
    result = classify(grid())
    assert result["classification"] == "unknown"
    assert result["unknown_reason"] == "total_content_stream_size_limit"


def test_composed_transform_overflow_fails_closed():
    result = classify(b"1000000000000 0 0 1000000000000 0 0 cm 2 0 0 2 0 0 cm")
    assert result["classification"] == "unknown"
    assert result["unknown_reason"] == "invalid_geometry_number"
