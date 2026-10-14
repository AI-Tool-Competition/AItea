"""Read BIFF8 cells without evaluating formulas, macros or external references.

Called inside parse_bounded. Workbook stream offsets are logical stream byte
ranges, never invented offsets in the enclosing OLE file or physical pages.
xlrd's documented omissions remain explicit review gates:
https://xlrd.readthedocs.io/en/latest/
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import struct
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

import xlrd
from xlrd.compdoc import CompDoc, CompDocError

from .vendor.geo_parser.document_ingestion import ParsedDocument, ParsedElement

MAX_BYTES = 20 * 1024 * 1024
MAX_CELLS = 100_000
MAX_ROWS = 20_000
MAX_RECORDS = 200_000
MAX_REVIEW = 2000
MAX_TEXT = 4_000_000
_OLE = bytes.fromhex("d0cf11e0a1b11ae1")
_CELL_RECORDS = {0x0203, 0x027E, 0x00FD, 0x0204, 0x00D6, 0x0006, 0x0205}
_UNMAPPED = {
    0x00EB: "drawing_group",
    0x00EC: "drawing",
    0x00ED: "drawing_selection",
    0x005D: "embedded_object",
    0x007F: "image",
    0x01B6: "text_box",
    0x001C: "cell_note",
    0x01B8: "hyperlink",
    0x0023: "external_name",
    0x04BC: "shared_formula",
    0x0221: "array_formula",
    0x0236: "data_table_formula",
    0x01B0: "conditional_format",
    0x01B1: "conditional_format_rule",
}


def _address(row, col):
    letters, number = "", col + 1
    while number:
        number, digit = divmod(number - 1, 26)
        letters = chr(65 + digit) + letters
    return f"{letters}{row + 1}"


def _record(stream, offset, size, code, name, source_hash):
    return {
        "kind": "xls_biff_record",
        "stream": name,
        "source_hash": source_hash,
        "stream_offset": offset,
        "length": size + 4,
        "record_type": f"0x{code:04x}",
        "sha256": hashlib.sha256(stream[offset : offset + size + 4]).hexdigest(),
    }


def _print_field(payload, ref, sheet, area):
    """MS-XLS Header: literal spans stay separate from print-time placeholders.

    https://learn.microsoft.com/en-us/openspecs/office_file_formats/ms-xls/b64cf6b8-9472-4f97-9a69-d839f0fa1089
    """
    if not payload:
        text = ""
    else:
        if len(payload) < 2:
            raise ValueError("XLS 打印字段损坏。")
        count = struct.unpack_from("<H", payload)[0]
        if not count:
            text = ""
        else:
            if (
                len(payload) < 3
                or payload[2] not in (0, 1)
                or count > 255
                or len(payload) != 3 + count * (2 if payload[2] else 1)
            ):
                raise ValueError("XLS 打印字段编码或大小无效。")
            text = payload[3:].decode("utf-16le" if payload[2] else "latin1")
    sections, dynamic, unknown = {key: [] for key in ("left", "center", "right")}, [], []
    section, offset = "center", 0
    fields = {
        "P": "page_number",
        "N": "page_total",
        "D": "print_date",
        "T": "print_time",
        "F": "file_name",
        "A": "sheet_name",
        "Z": "file_path",
        "G": "picture",
    }
    while offset < len(text):
        start = offset
        if text[offset] != "&":
            end = text.find("&", offset)
            offset = len(text) if end == -1 else end
            sections[section].append({"char_start": start, "char_end": offset, "text": text[start:offset]})
            continue
        match = re.match(r'&(?:"[^"\r\n]*"|\d{1,3}|K[0-9a-fA-F]{6}|[LCRBIUESXYPNDTFAZG&])', text[offset:])
        if match is None:
            unknown.append([start, min(len(text), start + 2)])
            offset += min(2, len(text) - offset)
            continue
        token = match[0]
        offset += len(token)
        code = token[1:]
        if code in {"L", "C", "R"}:
            section = {"L": "left", "C": "center", "R": "right"}[code]
        elif code == "&":
            sections[section].append({"char_start": start, "char_end": offset, "text": "&"})
        elif code in fields:
            if code == "P":
                extra = re.match(r"[+-]\d+", text[offset:])
                if extra:
                    offset += len(extra[0])
            dynamic.append(
                {
                    "field": fields[code],
                    "section": section,
                    "char_start": start,
                    "char_end": offset,
                    "raw_code": text[start:offset],
                }
            )
            if code == "G":
                unknown.append([start, offset])
    return {
        "sheet": sheet,
        "source_area": area,
        "raw_text": text,
        "sections": sections,
        "dynamic_fields": dynamic,
        "unmapped_spans": unknown,
        "source_record": ref,
    }


def _inspect_stream(content):
    macros = []
    if content.startswith(_OLE):
        container = CompDoc(content, logfile=io.StringIO())
        stream, name = container.get_named_stream("Workbook"), "Workbook"
        if stream is None:
            stream, name = container.get_named_stream("Book"), "Book"
        macros = [
            node.name for node in container.dirlist if re.search(r"VBA|Macros|ObjectPool", node.name, re.I)
        ]
        if stream is None:
            raise ValueError("XLS 缺少 Workbook/Book 数据流。")
    elif content[:2] == b"\x09\x08":
        stream, name = content, "raw_biff"
    else:
        raise ValueError("XLS 不是有效的 OLE/BIFF 文件。")
    if not stream or len(stream) > MAX_BYTES:
        raise ValueError("XLS 数据流超过处理限制。")
    refs, formulas, blanks, review, bounds, stack, xfs, print_fields = {}, {}, [], [], {}, [], [], []
    offset, counts, record_count = 0, Counter(), 0
    source_hash = hashlib.sha256(content).hexdigest()
    previous_formula = None
    continuation_target = None
    while offset < len(stream):
        if len(stream) - offset < 4:
            if any(stream[offset:]):
                raise ValueError("XLS BIFF 尾部记录不完整。")
            break
        code, size = struct.unpack_from("<HH", stream, offset)
        if code == size == 0:
            if any(stream[offset:]):
                raise ValueError("XLS BIFF 填充区包含未映射的数据。")
            break
        if offset + size + 4 > len(stream) or record_count >= MAX_RECORDS:
            raise ValueError("XLS BIFF 记录损坏或超过处理限制。")
        payload = stream[offset + 4 : offset + 4 + size]
        ref = _record(stream, offset, size, code, name, source_hash)
        counts[code] += 1
        record_count += 1
        if code == 0x002F:
            raise ValueError("不支持加密 XLS，原件需保留待处理。")
        if code == 0x00E0:
            if size != 20:
                raise ValueError("XLS XF 格式记录损坏。")
            font, number_format = struct.unpack_from("<HH", payload)
            xfs.append({"font_index": font, "number_format_key": number_format, "source_record": ref})
        if code == 0x0085:
            if size < 8 or payload[7] & ~1:
                raise ValueError("XLS 工作表名称记录损坏。")
            position, visibility, kind, length, wide = struct.unpack_from("<IBBBB", payload)
            needed = 8 + length * (2 if wide else 1)
            if not length or needed != size or visibility > 2:
                raise ValueError("XLS 工作表边界记录损坏。")
            sheet = payload[8:needed].decode("utf-16le" if wide else "latin1")
            if position in bounds:
                raise ValueError("XLS 工作表边界重复。")
            bounds[position] = {"name": sheet, "kind": kind, "visibility": visibility, "source_record": ref}
            if kind != 0:
                review.append({"reason": "non_worksheet_substream", "sheet": sheet, "source_record": ref})
        if code == 0x0809:
            if size < 4 or struct.unpack_from("<H", payload)[0] != 0x0600:
                raise ValueError("仅支持已验证的 BIFF8 XLS。")
            substream = struct.unpack_from("<H", payload, 2)[0]
            sheet = bounds.get(offset, {}).get("name") or (stack[-1] if stack else None)
            stack.append(sheet)
            if substream not in {0x0005, 0x0010}:
                review.append({"reason": "unmapped_substream", "sheet": sheet, "source_record": ref})
        sheet = stack[-1] if stack else None
        if code in {0x0014, 0x0015}:
            if sheet is None:
                raise ValueError("XLS 打印字段缺少来源工作表。")
            field = _print_field(payload, ref, sheet, "print_header" if code == 0x0014 else "print_footer")
            print_fields.append(field)
            if field["unmapped_spans"]:
                review.append({"reason": "unmapped_print_field", "sheet": sheet, "source_record": ref})
        if code in _CELL_RECORDS:
            if not sheet or size < 6:
                raise ValueError("XLS 单元格记录缺少来源工作表。")
            row, col, _xf = struct.unpack_from("<HHH", payload)
            key = (sheet, row, col)
            if key in refs:
                raise ValueError("XLS 单元格记录重复，不能选择其中一份。")
            refs[key] = ref
            if code == 0x0006:
                if size < 22 or 22 + struct.unpack_from("<H", payload, 20)[0] > size:
                    raise ValueError("XLS 公式缓存或 token 记录损坏。")
                cache = payload[6:14]
                tokens = payload[22 : 22 + struct.unpack_from("<H", payload, 20)[0]]
                formulas[key] = {
                    "cached_result_hex": cache.hex(),
                    "tokens_hex": tokens.hex(),
                    "tokens_sha256": hashlib.sha256(tokens).hexdigest(),
                    "source_record": ref,
                }
                previous_formula = key if cache[6:8] == b"\xff\xff" and cache[0] == 0 else None
                review.append(
                    {
                        "reason": "formula_cache_not_recalculated",
                        "sheet": sheet,
                        "cell": _address(row, col),
                        "source_record": ref,
                    }
                )
        elif code == 0x00BD:
            if not sheet or size < 12 or (size - 6) % 6:
                raise ValueError("XLS MULRK 记录损坏。")
            row, first = struct.unpack_from("<HH", payload)
            last = struct.unpack_from("<H", payload, size - 2)[0]
            if last - first + 1 != (size - 6) // 6:
                raise ValueError("XLS MULRK 列范围不一致。")
            for col in range(first, last + 1):
                key = (sheet, row, col)
                if key in refs:
                    raise ValueError("XLS 单元格记录重复。")
                refs[key] = {**ref, "entry_offset": 4 + (col - first) * 6}
        elif code == 0x0207 and previous_formula is not None:
            formulas[previous_formula]["string_result_record"] = ref
            previous_formula = None
        if code in {0x0201, 0x00BE}:
            blanks.append({"sheet": sheet, **ref})
        if code in _UNMAPPED:
            continuation_target = {
                "reason": "unmapped_" + _UNMAPPED[code],
                "sheet": sheet,
                "source_record": ref,
            }
            review.append(continuation_target)
        elif code == 0x003C and continuation_target is not None:
            continuation_target.setdefault("continuation_records", []).append(ref)
        else:
            continuation_target = None
        if code == 0x01AE and size >= 4 and struct.unpack_from("<H", payload, 2)[0] != 0x0401:
            review.append({"reason": "external_workbook_reference", "sheet": sheet, "source_record": ref})
        if code == 0x000A:
            if not stack:
                raise ValueError("XLS BIFF 子流结束标记不一致。")
            stack.pop()
        if len(refs) > MAX_CELLS or len(review) > MAX_REVIEW:
            raise ValueError("XLS 单元格或待核验对象超过处理限制。")
        offset += 4 + size
    if stack or not counts[0x0809] or not bounds:
        raise ValueError("XLS 缺少完整的工作表数据流。")
    if macros:
        review.append({"reason": "unmapped_ole_storage", "container_entries": macros})
    return (
        {
            "stream_name": name,
            "stream_size": len(stream),
            "stream_sha256": hashlib.sha256(stream).hexdigest(),
            "record_counts": {f"0x{k:04x}": v for k, v in counts.items()},
            "blank_format_records": blanks,
            "xf_records": xfs,
            "sheet_boundaries": list(bounds.values()),
            "print_fields": print_fields,
        },
        refs,
        formulas,
        review,
    )


def _display(value, kind, number_format, datemode):
    if kind == xlrd.XL_CELL_TEXT:
        return value, "literal", None
    if kind == xlrd.XL_CELL_BOOLEAN:
        return "TRUE" if value else "FALSE", "boolean", None
    if kind == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(value, f"ERROR({value})"), "error", "cell_error"
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return "", "invalid", "invalid_numeric_value"
    raw = str(int(value)) if float(value).is_integer() else repr(value)
    if kind == xlrd.XL_CELL_DATE:
        if datemode not in (0, 1) or value < 0 or (datemode == 0 and value < 61):
            return raw, "raw_serial", "ambiguous_date_serial"
        if number_format not in {"yyyy-mm-dd", "dd/mm/yyyy", "mm/dd/yyyy"}:
            return raw, "raw_serial", "unknown_date_format"
        try:
            date = xlrd.xldate_as_datetime(value, datemode)
        except (OverflowError, ValueError):
            return raw, "raw_serial", "invalid_date_serial"
        return (
            date.strftime(
                {"yyyy-mm-dd": "%Y-%m-%d", "dd/mm/yyyy": "%d/%m/%Y", "mm/dd/yyyy": "%m/%d/%Y"}[number_format]
            ),
            "date",
            None,
        )
    if number_format in {"General", "@"}:
        return raw, "raw_numeric", None
    plain = number_format.removesuffix("_)")
    match = re.fullmatch(r"(0{1,16})(?:\.(0{1,8}))?(%)?", plain)
    if not match:
        return raw, "raw_numeric", "unknown_number_format"
    if len(match[1]) > 1 and (match[2] or match[3] or not float(value).is_integer()):
        return raw, "raw_numeric", "unknown_number_format"
    try:
        places = len(match[2] or "")
        scaled = Decimal(str(value)) * (100 if match[3] else 1)
        decimal = scaled.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        text = f"{decimal:.{places}f}"
        if len(match[1]) > 1:
            text = ("-" if decimal < 0 else "") + str(abs(int(decimal))).zfill(len(match[1]))
        return text + ("%" if match[3] else ""), "number_format", None
    except InvalidOperation:
        return raw, "raw_numeric", "numeric_format_overflow"


def parse_xls(content: bytes, *, filename: str = "workbook.xls") -> dict:
    """Return the standard manifest plus native workbook and review metadata."""
    from .ingestion import _parsed_result

    if not isinstance(content, bytes) or not content or len(content) > MAX_BYTES:
        raise ValueError("XLS 原件不能为空且不能超过 20 MB。")
    source_hash = hashlib.sha256(content).hexdigest()
    try:
        native, refs, formulas, review = _inspect_stream(content)
        book = xlrd.open_workbook(
            file_contents=content, formatting_info=True, on_demand=True, use_mmap=False, logfile=io.StringIO()
        )
    except (ValueError, xlrd.XLRDError, CompDocError, IndexError, struct.error, UnicodeError) as exc:
        raise ValueError("XLS 数据结构损坏或不受支持；原件需保留待处理。") from exc
    elements, sheets, cell_count, text_size, mapped = [], [], 0, 0, set()
    try:
        if book.biff_version != 80 or book.nsheets > 100 or book.datemode not in (0, 1):
            raise ValueError("XLS 版本、工作表数量或日期模式不受支持。")
        for index, sheet in enumerate(book.sheets()):
            if sheet.nrows > MAX_ROWS or sheet.ncols > 256 or len(sheet.merged_cells) > 2000:
                raise ValueError("XLS 行列或合并范围超过处理限制。")
            sheets.append(
                {
                    "index": index,
                    "name": sheet.name,
                    "row_count": sheet.nrows,
                    "column_count": sheet.ncols,
                    "visibility": sheet.visibility,
                    "merged_ranges": [
                        {
                            "cell_range": f"{_address(r0, c0)}:{_address(r1 - 1, c1 - 1)}",
                            "row_start": r0 + 1,
                            "row_end": r1,
                            "column_start": c0 + 1,
                            "column_end": c1,
                        }
                        for r0, r1, c0, c1 in sheet.merged_cells
                    ],
                }
            )
            for row in range(sheet.nrows):
                cells, text = [], []
                for col, cell in enumerate(sheet.row(row)):
                    if cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK}:
                        continue
                    key, address = (sheet.name, row, col), _address(row, col)
                    if key not in refs or not 0 <= cell.xf_index < len(book.xf_list):
                        raise ValueError("XLS 单元格与源记录或 XF 格式无法对应。")
                    mapped.add(key)
                    xf = book.xf_list[cell.xf_index]
                    if cell.xf_index >= len(native["xf_records"]):
                        raise ValueError("XLS XF 格式未对应到源记录。")
                    native_xf = native["xf_records"][cell.xf_index]
                    format_key = native_xf["number_format_key"]
                    number_format = (
                        book.format_map[format_key].format_str if format_key in book.format_map else ""
                    )
                    display, display_status, problem = _display(
                        cell.value, cell.ctype, number_format, book.datemode
                    )
                    if xf.format_key != format_key or not number_format:
                        problem = "unmapped_xf_number_format"
                    if isinstance(cell.value, str) and (
                        "\ufffd" in cell.value or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", cell.value)
                    ):
                        problem = "uncertain_native_text"
                    locator = {
                        "kind": "spreadsheet",
                        "source_format": "xls",
                        "source_hash": source_hash,
                        "sheet": sheet.name,
                        "cell_range": address,
                        "row_start": row + 1,
                        "row_end": row + 1,
                        "column_start": col + 1,
                        "column_end": col + 1,
                    }
                    metadata = {
                        "cell": address,
                        "row": row + 1,
                        "column": col + 1,
                        "raw_type": cell.ctype,
                        "raw_value": repr(cell.value)
                        if isinstance(cell.value, float) and not math.isfinite(cell.value)
                        else cell.value,
                        "xf_index": cell.xf_index,
                        "number_format_key": format_key,
                        "number_format": number_format,
                        "display_text": display,
                        "display_status": display_status,
                        "source_record": refs[key],
                    }
                    if isinstance(cell.value, float):
                        metadata["raw_float_hex"] = cell.value.hex()
                    if key in formulas:
                        metadata["formula_cache"] = formulas[key]
                    if problem:
                        metadata["review_reason"] = problem
                        review.append({"reason": problem, "locator": locator, "source_record": refs[key]})
                    cells.append(metadata)
                    text.append(f"{address}: {display}")
                    text_size += len(display)
                    cell_count += 1
                    if cell_count > MAX_CELLS or text_size > MAX_TEXT or len(review) > MAX_REVIEW:
                        raise ValueError("XLS 单元格、正文或待核验对象超过处理限制。")
                if not cells:
                    continue
                locator = {
                    "kind": "spreadsheet",
                    "source_format": "xls",
                    "source_hash": source_hash,
                    "sheet": sheet.name,
                    "row_start": row + 1,
                    "row_end": row + 1,
                    "column_start": cells[0]["column"],
                    "column_end": cells[-1]["column"],
                    "cell_range": f"{cells[0]['cell']}:{cells[-1]['cell']}",
                }
                elements.append(
                    ParsedElement(
                        f"sheet-{index}-row-{row + 1}",
                        "table",
                        len(elements),
                        "\n".join(text),
                        locator,
                        [sheet.name],
                        metadata_json={
                            "cells": cells,
                            "hidden_row": bool(getattr(sheet.rowinfo_map.get(row), "hidden", 0)),
                        },
                    )
                )
            for field_index, field in enumerate(native["print_fields"]):
                if field["sheet"] != sheet.name:
                    continue
                for section, spans in field["sections"].items():
                    literal = "".join(span["text"] for span in spans)
                    if not literal.strip():
                        continue
                    locator = {
                        "kind": "spreadsheet",
                        "source_format": "xls",
                        "source_hash": source_hash,
                        "sheet": sheet.name,
                        "source_area": field["source_area"],
                        "print_section": section,
                        "source_record": field["source_record"],
                    }
                    elements.append(
                        ParsedElement(
                            f"sheet-{index}-print-{field_index}-{section}",
                            "paragraph",
                            len(elements),
                            literal,
                            locator,
                            [sheet.name, field["source_area"], section],
                            metadata_json={"print_literal_spans": spans},
                        )
                    )
        if set(refs) - mapped:
            raise ValueError("XLS 存在未被 xlrd 映射的单元格记录。")
        if (
            not elements
            or len(elements) > MAX_ROWS
            or sum(len(element.content_text) for element in elements) > MAX_TEXT
        ):
            raise ValueError("XLS 没有可提取正文或行元素超过限制。")
        warnings = sorted({item["reason"] for item in review})
        result = _parsed_result(
            ParsedDocument("xls-xlrd", xlrd.__version__, "xls-native-cells-v1", elements, [], warnings),
            source_hash,
            Path(filename).stem,
        )
        result["quality"] = {
            "format": "xls",
            "complete": not review,
            "pending_review": review,
            "sheet_count": len(sheets),
            "cell_count": cell_count,
            "physical_pages": False,
        }
        result["workbook"] = {
            **native,
            "source_hash": source_hash,
            "date_mode": book.datemode,
            "biff_version": book.biff_version,
            "sheets": sheets,
            "formula_count": len(formulas),
            "cell_count": cell_count,
            "cell_mapping_complete": True,
        }
        return result
    finally:
        book.release_resources()
