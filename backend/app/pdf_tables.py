"""Conservative digital-grid extraction inside the existing bounded PDF worker.

Only a single complete, painted rectangular grid is supported. Every visible
character must fit one cell or lie outside the grid; pypdf and PDFium must agree
on all non-whitespace characters. Scans, merged cells and ambiguous geometry
remain OCR work. No labels, rates or empty-cell values are inferred.
"""

from bisect import bisect_right
from collections import Counter
import ctypes
import hashlib
import json
import math
import unicodedata

VERSION = "verified-digital-grid-v1"


def _characters(text):
    return Counter(char for char in unicodedata.normalize("NFKC", text) if not char.isspace())


def _cluster(values, tolerance=0.8):
    groups = []
    for value in sorted(values):
        if groups and value - groups[-1][-1] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _grid(page):
    import pypdfium2.raw as raw

    width, height = page.get_size()
    horizontal, vertical = [], []
    objects = list(page.get_objects(filter=[raw.FPDF_PAGEOBJ_PATH], max_depth=1))
    if len(objects) > 4000 or page.get_rotation():
        return None
    for obj in objects:
        if obj.level:
            return None
        fill, stroke = ctypes.c_int(), ctypes.c_int()
        if not raw.FPDFPath_GetDrawMode(obj.raw, fill, stroke) or not (fill.value or stroke.value):
            continue
        if not 2 <= raw.FPDFPath_CountSegments(obj.raw) <= 5:
            continue
        red, green, blue, alpha = (ctypes.c_uint() for _ in range(4))
        get_color = raw.FPDFPageObj_GetFillColor if fill.value else raw.FPDFPageObj_GetStrokeColor
        if not get_color(obj.raw, red, green, blue, alpha) or alpha.value == 0:
            continue
        left, bottom, right, top = obj.get_bounds()
        if not all(math.isfinite(x) for x in (left, bottom, right, top)):
            return None
        if right - left <= 1.5 and top - bottom >= 20:
            vertical.append(((left + right) / 2, height - top, height - bottom))
        elif top - bottom <= 1.5 and right - left >= 20:
            horizontal.append((height - (bottom + top) / 2, left, right))
    if len(vertical) < 3 or len(horizontal) < 3:
        return None
    # A strict full grid rejects merged cells and multiple independent tables.
    xs, ys = _cluster([v[0] for v in vertical]), _cluster([h[0] for h in horizontal])
    if not 3 <= len(xs) <= 65 or not 3 <= len(ys) <= 401:
        return None
    if any(b - a < 3 for lines in (xs, ys) for a, b in zip(lines, lines[1:])):
        return None
    for x in xs:
        intervals = sorted((start, end) for position, start, end in vertical if abs(position - x) <= 0.8)
        cursor = ys[0]
        for start, end in intervals:
            if start > cursor + 1.5 or start < ys[0] - 1.5 or end > ys[-1] + 1.5:
                return None
            cursor = max(cursor, end)
        if abs(cursor - ys[-1]) > 1.5:
            return None
    if any(abs(h[1] - xs[0]) > 1.5 or abs(h[2] - xs[-1]) > 1.5 for h in horizontal):
        return None
    return xs, ys, width, height


def _lines(glyphs, original):
    lines = []
    for glyph in sorted(glyphs, key=lambda item: (item["line_y"], item["left"])):
        if lines and abs(glyph["line_y"] - lines[-1][0]["line_y"]) <= 1.2:
            lines[-1].append(glyph)
        else:
            lines.append([glyph])
    output = []
    for line in lines:
        result, previous = "", None
        for glyph in sorted(line, key=lambda item: item["left"]):
            if previous:
                lo, hi = previous["index"] + 1, glyph["index"]
                source_space = lo < hi and all(char.isspace() for char in original[lo:hi])
                visual_gap = lo > hi and glyph["left"] - previous["right"] > max(1, glyph["height"] * 0.35)
                if source_space or visual_gap:
                    result += " "
            result += glyph["char"]
            previous = glyph
        output.append(result)
    return "\n".join(output)


def extract_grid(page, native_text):
    """Return exact cell text/provenance or None; caller retains the original layer."""
    import pypdfium2.raw as raw

    geometry = _grid(page)
    if geometry is None:
        return None
    xs, ys, _, height = geometry
    textpage = page.get_textpage()
    try:
        count = textpage.count_chars()
        if not 0 < count <= 100_000:
            return None
        original, hyphens = [], 0
        for index in range(count):
            char = textpage.get_text_range(index, 1)
            # PDFium returns U+FFFE for some discretionary hyphens. Its
            # independent IsHyphen API identifies their visible dash; the
            # second reader must still contain exactly the same character.
            if char == "\ufffe" and raw.FPDFText_IsHyphen(textpage.raw, index) == 1:
                char = "-"
                hyphens += 1
            original.append(char)
        if _characters("".join(original)) != _characters(native_text):
            return None
        # Hidden native OCR layers cannot qualify as visible digital cells.
        for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_TEXT], max_depth=1, textpage=textpage):
            if obj.level or raw.FPDFTextObj_GetTextRenderMode(obj.raw) not in {0, 1, 2}:
                return None
            left, bottom, right, top = obj.get_bounds()
            top, bottom = height - top, height - bottom
            y, x = (top + bottom) / 2, (left + right) / 2
            if ys[0] <= y <= ys[-1]:
                row, col = bisect_right(ys, y) - 1, bisect_right(xs, x) - 1
                if not 0 <= row < len(ys) - 1 or not 0 <= col < len(xs) - 1:
                    return None
                if (
                    left < xs[col] - 0.7
                    or right > xs[col + 1] + 0.7
                    or top < ys[row] - 0.7
                    or bottom > ys[row + 1] + 0.7
                ):
                    return None
        cells = [[[] for _ in range(len(xs) - 1)] for _ in range(len(ys) - 1)]
        above, below = [], []
        for index, char in enumerate(original):
            if not char or char.isspace():
                continue
            if any(not value.isprintable() for value in char):
                return None
            left, bottom, right, top = textpage.get_charbox(index)
            top, bottom = height - top, height - bottom
            if (
                not all(math.isfinite(x) for x in (left, bottom, right, top))
                or left >= right
                or top >= bottom
            ):
                return None
            loose = textpage.get_charbox(index, loose=True)
            glyph = {
                "index": index,
                "char": char,
                "left": left,
                "right": right,
                "top": top,
                "height": bottom - top,
                "line_y": height - loose[3],
            }
            x, y = (left + right) / 2, (top + bottom) / 2
            if y < ys[0]:
                above.append(glyph)
                continue
            if y > ys[-1]:
                below.append(glyph)
                continue
            row, col = bisect_right(ys, y) - 1, bisect_right(xs, x) - 1
            if not 0 <= row < len(cells) or not 0 <= col < len(cells[0]):
                return None
            if (
                left < xs[col] - 0.7
                or right > xs[col + 1] + 0.7
                or top < ys[row] - 0.7
                or bottom > ys[row + 1] + 0.7
            ):
                return None
            cells[row][col].append(glyph)
        texts = [[_lines(cell, original) for cell in row] for row in cells]
        if any(not value for value in texts[0]) or not any(any(row) for row in texts[1:]):
            return None
        if _characters(
            "".join(value for row in texts for value in row)
            + _lines(above, original)
            + _lines(below, original)
        ) != _characters(native_text):
            return None

        def markdown_row(row):
            return "| " + " | ".join(value.replace("|", "\\|").replace("\n", "<br>") for value in row) + " |"

        table = "\n".join(
            [
                markdown_row(texts[0]),
                markdown_row(["---"] * len(xs[1:])),
                *[markdown_row(row) for row in texts[1:]],
            ]
        )
        output = "\n\n".join(
            value for value in (_lines(above, original), table, _lines(below, original)) if value
        )
        inventory = sorted(_characters(native_text).items())
        return {
            "text": output,
            "method": VERSION,
            "rows": len(texts),
            "columns": len(texts[0]),
            "x_boundaries": [round(x, 4) for x in xs],
            "y_boundaries": [round(y, 4) for y in ys],
            "cells": texts,
            "character_inventory_sha256": hashlib.sha256(
                json.dumps(inventory, ensure_ascii=False).encode()
            ).hexdigest(),
            "readers": ["pypdf", "pypdfium2"],
            "complete_character_assignment": True,
            "discretionary_hyphens": hyphens,
        }
    finally:
        textpage.close()
