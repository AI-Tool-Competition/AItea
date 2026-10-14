"""Small BIFF8 records test source values rather than parser-generated fixtures."""

import hashlib
import json
import struct

import pytest

from app.xls_parsing import parse_xls
from app.ingestion_quality import text_complete


def record(code, data=b""):
    return struct.pack("<HH", code, len(data)) + data


def bof(kind):
    return record(0x0809, struct.pack("<HHHHII", 0x0600, kind, 0x0DBB, 1996, 0x41, 6))


def workbook(cells, *, formats=("General",), date_mode=0, merged=(), extra=b""):
    globals = bof(5) + record(0x0042, struct.pack("<H", 1200)) + record(0x0022, struct.pack("<H", date_mode))
    for index, fmt in enumerate(formats):
        key = 0 if fmt == "General" else 164 + index
        if fmt != "General":
            globals += record(0x041E, struct.pack("<HHB", key, len(fmt), 0) + fmt.encode("latin1"))
        globals += record(0x00E0, struct.pack("<HHHBBBBIIH", 0, key, 0, 0, 0, 0, 0, 0, 0, 0))
    bound = record(0x0085, struct.pack("<IBBBB", 0, 0, 0, 1, 0) + b"S")
    offset = len(globals) + len(bound) + len(record(0x000A))
    bound = record(0x0085, struct.pack("<IBBBB", offset, 0, 0, 1, 0) + b"S")
    sheet = bof(0x0010) + record(0x0200, struct.pack("<IIHHH", 0, 10, 0, 10, 0))
    for row, col, value, xf, kind in cells:
        prefix = struct.pack("<HHH", row, col, xf)
        if kind == "text":
            wide = any(ord(char) > 255 for char in value)
            payload = (
                prefix
                + struct.pack("<HB", len(value), int(wide))
                + value.encode("utf-16le" if wide else "latin1")
            )
            sheet += record(0x0204, payload)
        elif kind == "error":
            sheet += record(0x0205, prefix + bytes([value, 1]))
        elif kind == "boolean":
            sheet += record(0x0205, prefix + bytes([value, 0]))
        elif kind == "formula":
            sheet += record(0x0006, prefix + struct.pack("<dHIH", value, 0, 0, 3) + b"\x1e\x63\x00")
        else:
            sheet += record(0x0203, prefix + struct.pack("<d", value))
    if merged:
        sheet += record(
            0x00E5, struct.pack("<H", len(merged)) + b"".join(struct.pack("<HHHH", *span) for span in merged)
        )
    return globals + bound + record(0x000A) + sheet + extra + record(0x000A)


def cell(result, address="A1"):
    return next(
        c for e in result["elements"] for c in e["metadata_json"].get("cells", []) if c["cell"] == address
    )


def test_cell_text_coordinates_raw_values_and_stream_hashes_are_retained():
    source = workbook([(0, 0, "001234", 0, "text"), (0, 1, "茶叶", 0, "text")])
    result = parse_xls(source, filename="schedule.xls")
    assert result["source_hash"] == hashlib.sha256(source).hexdigest()
    assert cell(result)["raw_value"] == cell(result)["display_text"] == "001234"
    assert cell(result)["raw_type"] == 1
    locator = result["chunks"][0]["locator"]
    assert locator["kind"] == "spreadsheet" and locator["sheet"] == "S"
    assert locator["row_start"] == locator["row_end"] == 1
    assert locator["cell_range"] == "A1:B1" and locator["source_format"] == "xls"
    assert "pages" not in result and result["quality"]["physical_pages"] is False
    for address in ("A1", "B1"):
        ref = cell(result, address)["source_record"]
        payload = source[ref["stream_offset"] : ref["stream_offset"] + ref["length"]]
        assert ref["sha256"] == hashlib.sha256(payload).hexdigest()
        assert ref["stream"] == "raw_biff" and ref["source_hash"] == result["source_hash"]
    assert text_complete(result) is True


@pytest.mark.parametrize(
    "value,fmt,expected",
    [
        (123, "000000", "000123"),
        (0.125, "0.0%", "12.5%"),
        (7, "0.0", "7.0"),
        (7, "0.0_)", "7.0"),
        (-7, "0000", "-0007"),
        (2.675, "0.00", "2.68"),
        (1.234, "General", "1.234"),
    ],
)
def test_verified_numeric_formats_preserve_original_value(value, fmt, expected):
    result = parse_xls(workbook([(0, 0, value, 0, "number")], formats=(fmt,)))
    c = cell(result)
    assert c["raw_value"] == value and c["display_text"] == expected
    assert c["number_format"] == fmt and c["xf_index"] == 0
    assert c["raw_float_hex"] == float(value).hex()
    assert result["quality"]["complete"] is True


def test_merge_range_is_retained_without_filling_missing_cells():
    result = parse_xls(workbook([(0, 0, "Origin", 0, "text")], merged=((0, 1, 0, 2),)))
    assert result["workbook"]["sheets"][0]["merged_ranges"] == [
        {
            "cell_range": "A1:C2",
            "row_start": 1,
            "row_end": 2,
            "column_start": 1,
            "column_end": 3,
        }
    ]
    assert result["workbook"]["cell_count"] == 1
    assert cell(result)["raw_value"] == "Origin"


def test_formula_returns_cached_value_and_tokens_but_remains_under_review():
    result = parse_xls(workbook([(0, 0, 3.25, 0, "formula")]))
    c = cell(result)
    assert c["raw_value"] == 3.25 and c["display_text"] == "3.25"
    assert c["formula_cache"]["tokens_hex"] == "1e6300"  # Formula literal 99; it is never evaluated.
    assert c["formula_cache"]["cached_result_hex"] == struct.pack("<d", 3.25).hex()
    assert result["workbook"]["formula_count"] == 1
    assert result["quality"]["complete"] is False
    assert result["quality"]["pending_review"][0]["reason"] == "formula_cache_not_recalculated"
    assert text_complete(result) is False


@pytest.mark.parametrize(
    "code,reason",
    [
        (0x00EC, "unmapped_drawing"),
        (0x007F, "unmapped_image"),
        (0x005D, "unmapped_embedded_object"),
        (0x001C, "unmapped_cell_note"),
    ],
)
def test_unmapped_visual_or_note_record_keeps_exact_stream_locator_and_blocks_completion(code, reason):
    payload = (
        struct.pack("<4H", 0, 0, 0, 1) + b"\x01\x00\x00A\x00" if code == 0x001C else b"image-or-note-bytes"
    )
    source = workbook([(0, 0, "Text", 0, "text")], extra=record(code, payload))
    result = parse_xls(source)
    gap = next(v for v in result["quality"]["pending_review"] if v["reason"] == reason)
    ref = gap["source_record"]
    assert (
        ref["sha256"]
        == hashlib.sha256(source[ref["stream_offset"] : ref["stream_offset"] + ref["length"]]).hexdigest()
    )
    assert ref["source_hash"] == result["source_hash"]
    assert gap["sheet"] == "S" and text_complete(result) is False
    assert "image-or-note-bytes" not in " ".join(c["text"] for c in result["chunks"])


@pytest.mark.parametrize(
    "value,fmt,reason",
    [(7, "#,##0.00", "unknown_number_format"), (60, "yyyy-mm-dd", "ambiguous_date_serial")],
)
def test_unknown_format_or_excel_1900_leap_day_is_never_guessed(value, fmt, reason):
    result = parse_xls(workbook([(0, 0, value, 0, "number")], formats=(fmt,)))
    assert cell(result)["raw_value"] == value and cell(result)["display_text"] == str(value)
    assert result["quality"]["complete"] is False and cell(result)["review_reason"] == reason


def test_unknown_raw_xf_key_cannot_be_silently_changed_to_general_by_xlrd():
    source = bytearray(workbook([(0, 0, 7, 0, "number")]))
    position = source.index(b"\xe0\x00\x14\x00")
    struct.pack_into("<H", source, position + 6, 999)
    result = parse_xls(bytes(source))
    assert cell(result)["number_format_key"] == 999
    assert cell(result)["number_format"] == ""
    assert cell(result)["review_reason"] == "unmapped_xf_number_format"
    assert result["workbook"]["xf_records"][0]["number_format_key"] == 999
    assert text_complete(result) is False


def test_embedded_chart_substream_is_explicitly_unmapped_without_fake_pages():
    result = parse_xls(workbook([(0, 0, "Cell text", 0, "text")], extra=bof(0x0020) + record(0x000A)))
    assert any(gap["reason"] == "unmapped_substream" for gap in result["quality"]["pending_review"])
    assert result["quality"]["complete"] is False and "pages" not in result


def test_drawing_continuations_are_attached_only_when_adjacent_to_the_source_record():
    extra = (
        record(0x00EC, b"drawing")
        + record(0x003C, b"adjacent")
        + record(0x0012, b"\x00\x00")
        + record(0x003C, b"unrelated")
    )
    result = parse_xls(workbook([(0, 0, "Text", 0, "text")], extra=extra))
    gap = next(item for item in result["quality"]["pending_review"] if item["reason"] == "unmapped_drawing")
    assert len(gap["continuation_records"]) == 1
    assert gap["continuation_records"][0]["sha256"] == hashlib.sha256(record(0x003C, b"adjacent")).hexdigest()


def test_print_header_footer_preserve_literal_source_spans_without_inventing_page_numbers():
    raw = '&L&"Times New Roman,Bold"&10Schedule XXI&CPage &P&RDoc && Annex'
    header = record(0x0014, struct.pack("<HB", len(raw), 0) + raw.encode())
    result = parse_xls(workbook([(0, 0, "Native cell", 0, "text")], extra=header))
    field = result["workbook"]["print_fields"][0]
    assert field["raw_text"] == raw
    assert field["dynamic_fields"] == [
        {
            "field": "page_number",
            "section": "center",
            "char_start": raw.index("&P"),
            "char_end": raw.index("&P") + 2,
            "raw_code": "&P",
        }
    ]
    printed = [
        element for element in result["elements"] if element["locator"].get("source_area") == "print_header"
    ]
    assert [element["content_text"] for element in printed] == ["Schedule XXI", "Page ", "Doc & Annex"]
    assert all(element["locator"]["sheet"] == "S" and "page" not in element["locator"] for element in printed)
    assert all(element["locator"]["source_record"] == field["source_record"] for element in printed)
    span = field["sections"]["left"][0]
    assert span["text"] == raw[span["char_start"] : span["char_end"]]
    assert result["quality"]["complete"] is True and "pages" not in result


@pytest.mark.parametrize("raw", ["&QUnknown", "&G"])
def test_unknown_print_command_or_picture_keeps_raw_field_and_blocks_completion(raw):
    footer = record(0x0015, struct.pack("<HB", len(raw), 0) + raw.encode())
    result = parse_xls(workbook([(0, 0, "Cell text", 0, "text")], extra=footer))
    assert result["workbook"]["print_fields"][0]["raw_text"] == raw
    assert result["quality"]["complete"] is False
    assert result["quality"]["pending_review"][0]["reason"] == "unmapped_print_field"


@pytest.mark.parametrize("mode,value,expected", [(0, 61, "1900-03-01"), (1, 0, "1904-01-01")])
def test_date_mode_is_preserved_and_applied_only_to_verified_date_format(mode, value, expected):
    result = parse_xls(workbook([(0, 0, value, 0, "number")], formats=("yyyy-mm-dd",), date_mode=mode))
    assert cell(result)["display_text"] == expected
    assert result["workbook"]["date_mode"] == mode


def test_cell_error_and_nonfinite_value_are_not_coerced_into_legal_numbers():
    result = parse_xls(workbook([(0, 0, 42, 0, "error"), (0, 1, float("nan"), 0, "number")]))
    assert cell(result)["display_text"] == "#N/A"
    assert cell(result, "B1")["raw_value"] == "nan"
    assert result["quality"]["complete"] is False
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "source", [b"not XLS", b"PK\x03\x04", bytes.fromhex("d0cf11e0a1b11ae1") + bytes(504)]
)
def test_corrupt_or_mislabeled_sources_are_rejected(source):
    with pytest.raises(ValueError):
        parse_xls(source)


def test_duplicate_cell_cannot_select_a_convenient_source_value():
    with pytest.raises(ValueError):
        parse_xls(workbook([(0, 0, "First", 0, "text"), (0, 0, "Second", 0, "text")]))


def test_encrypted_workbook_is_rejected_but_protection_is_not_encryption():
    source = workbook([(0, 0, "Text", 0, "text")], extra=record(0x002F, b"encrypted"))
    with pytest.raises(ValueError):
        parse_xls(source)
    result = parse_xls(workbook([(0, 0, "Text", 0, "text")], extra=record(0x0012, b"\x01\x00")))
    assert result["quality"]["complete"] is True


def test_input_cell_and_record_budgets_fail_before_partial_completion(monkeypatch):
    from app import xls_parsing

    with pytest.raises(ValueError):
        parse_xls(b"x" * (xls_parsing.MAX_BYTES + 1))
    source = workbook([(0, 0, "First", 0, "text"), (0, 1, "Second", 0, "text")])
    monkeypatch.setattr(xls_parsing, "MAX_CELLS", 1)
    with pytest.raises(ValueError):
        parse_xls(source)
    monkeypatch.setattr(xls_parsing, "MAX_CELLS", 100_000)
    monkeypatch.setattr(xls_parsing, "MAX_RECORDS", 1)
    with pytest.raises(ValueError):
        parse_xls(source)


def test_external_reference_is_mapped_without_opening_network_or_recalculating(monkeypatch):
    import socket

    def unexpected(*args, **kwargs):
        raise AssertionError("network access during XLS parsing")

    monkeypatch.setattr(socket, "socket", unexpected)
    result = parse_xls(
        workbook([(0, 0, "Source value", 0, "text")], extra=record(0x01AE, b"\x01\x00\x03\x00URI"))
    )
    assert result["quality"]["complete"] is False
    assert result["quality"]["pending_review"][0]["reason"] == "external_workbook_reference"
    assert cell(result)["raw_value"] == "Source value"


def test_normal_parse_bounded_role_gate_and_quality_survive_worker_serialization():
    from app.parsing import parse_bounded

    source = workbook([(0, 0, 3.25, 0, "formula")])
    with pytest.raises(ValueError, match="secondary"):
        parse_bounded("sheet.xls", source, role="secondary")
    result = parse_bounded("sheet.xls", source, "application/vnd.ms-excel")
    assert result["workbook"]["source_hash"] == hashlib.sha256(source).hexdigest()
    assert cell(result)["raw_value"] == 3.25 and text_complete(result) is False
