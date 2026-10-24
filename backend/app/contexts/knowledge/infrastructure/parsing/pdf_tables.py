"""Conservative digital-grid extraction inside the existing bounded PDF worker.

Only a single complete, painted rectangular grid is supported. Merged cells
need a rectangular region with a fully painted perimeter and absent interior
dividers. Every visible character must fit one evidenced cell or lie outside
the grid; pypdf and PDFium must agree on all non-whitespace characters. Scans
and ambiguous geometry remain OCR work. No labels or empty values are inferred.
"""

import ctypes
import hashlib
import json
import math
import unicodedata
from bisect import bisect_right
from collections import Counter

VERSION = "verified-digital-grid-v2"


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
    horizontal, vertical, junctions, graphics = [], [], [], []
    objects = list(page.get_objects(filter=[raw.FPDF_PAGEOBJ_PATH], max_depth=1))
    if len(objects) > 4000 or page.get_rotation():
        return None
    for obj in objects:
        if obj.level:
            return None
        fill, stroke = ctypes.c_int(), ctypes.c_int()
        if not raw.FPDFPath_GetDrawMode(obj.raw, fill, stroke) or not (fill.value or stroke.value):
            continue
        red, green, blue, alpha = (ctypes.c_uint() for _ in range(4))
        get_color = raw.FPDFPageObj_GetFillColor if fill.value else raw.FPDFPageObj_GetStrokeColor
        if not get_color(obj.raw, red, green, blue, alpha) or alpha.value == 0:
            continue
        left, bottom, right, top = obj.get_bounds()
        if not all(math.isfinite(x) for x in (left, bottom, right, top)):
            return None
        segment_count = raw.FPDFPath_CountSegments(obj.raw)
        straight = 2 <= segment_count <= 5 and all(
            raw.FPDFPathSegment_GetType(raw.FPDFPath_GetPathSegment(obj.raw, index))
            in {raw.FPDF_SEGMENT_LINETO, raw.FPDF_SEGMENT_MOVETO}
            for index in range(segment_count)
        )
        if not straight:
            graphics.append((left, height - top, right, height - bottom))
        elif right - left <= 1.5 and top - bottom >= 3:
            vertical.append(((left + right) / 2, height - top, height - bottom))
        elif top - bottom <= 1.5 and right - left >= 3:
            horizontal.append((height - (bottom + top) / 2, left, right))
        elif right - left <= 1.5 and top - bottom <= 1.5:
            junctions.append(((left + right) / 2, height - (top + bottom) / 2))
        elif not (fill.value and not stroke.value and red.value == green.value == blue.value == 255):
            graphics.append((left, height - top, right, height - bottom))
    if len(vertical) < 3 or len(horizontal) < 3:
        return None
    xs, ys = _cluster([v[0] for v in vertical]), _cluster([h[0] for h in horizontal])
    if not 3 <= len(xs) <= 65 or not 3 <= len(ys) <= 401:
        return None
    if any(b - a < 3 for lines in (xs, ys) for a, b in zip(lines, lines[1:])):
        return None
    # A table-like grid cannot explain additional diagrams/curves. Small
    # painted border joints are accepted only at evidenced axis intersections.
    if any(
        right > xs[0] and left < xs[-1] and bottom > ys[0] and top < ys[-1]
        for left, top, right, bottom in graphics
    ):
        return None
    if any(
        xs[0] <= x <= xs[-1]
        and ys[0] <= y <= ys[-1]
        and (
            not any(abs(x - boundary) <= 0.8 for boundary in xs)
            or not any(abs(y - boundary) <= 0.8 for boundary in ys)
        )
        for x, y in junctions
    ):
        return None
    for image in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE], max_depth=1):
        if image.level:
            return None
        left, bottom, right, top = image.get_bounds()
        top, bottom = height - top, height - bottom
        if right > xs[0] and left < xs[-1] and bottom > ys[0] and top < ys[-1]:
            return None
    from app.contexts.knowledge.infrastructure.parsing.pdf_table_geometry import rectangular_regions

    partition = rectangular_regions(xs, ys, horizontal, vertical)
    if partition is None:
        return None
    return xs, ys, width, height, *partition


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
    xs, ys, _, height, regions, owners = geometry
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
                cell_left, cell_top, cell_right, cell_bottom = regions[owners[row][col]]["bounds"]
                if (
                    left < cell_left - 0.7
                    or right > cell_right + 0.7
                    or top < cell_top - 0.7
                    or bottom > cell_bottom + 0.7
                ):
                    return None
        region_glyphs = [[] for _ in regions]
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
            if not 0 <= row < len(ys) - 1 or not 0 <= col < len(xs) - 1:
                return None
            owner = owners[row][col]
            cell_left, cell_top, cell_right, cell_bottom = regions[owner]["bounds"]
            if (
                left < cell_left - 0.7
                or right > cell_right + 0.7
                or top < cell_top - 0.7
                or bottom > cell_bottom + 0.7
            ):
                return None
            region_glyphs[owner].append(glyph)
        texts = [[""] * (len(xs) - 1) for _ in range(len(ys) - 1)]
        spans = []
        for region, glyphs in zip(regions, region_glyphs, strict=True):
            merged = region["rowspan"] > 1 or region["colspan"] > 1
            if merged:
                ordered = sorted(glyphs, key=lambda glyph: (glyph["line_y"], glyph["left"]))
                # A missing divider between independently separated labels is
                # ambiguous, even if the outer box forms a rectangle.
                for left, right in zip(ordered, ordered[1:]):
                    if (
                        abs(left["line_y"] - right["line_y"]) <= 1.2
                        and right["left"] - left["right"] > max(left["height"], right["height"]) * 3
                    ):
                        return None
            value = _lines(glyphs, original)
            texts[region["row"]][region["column"]] = value
            if merged:
                spans.append(
                    {**region, "bounds": [round(position, 4) for position in region["bounds"]], "text": value}
                )
        if any(
            not texts[regions[owner]["row"]][regions[owner]["column"]] for owner in set(owners[0])
        ) or not any(any(row) for row in texts[1:]):
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
            "cell_spans": spans,
            "default_cell_span": {"rowspan": 1, "colspan": 1},
            "character_inventory_sha256": hashlib.sha256(
                json.dumps(inventory, ensure_ascii=False).encode()
            ).hexdigest(),
            "readers": ["pypdf", "pypdfium2"],
            "complete_character_assignment": True,
            "discretionary_hyphens": hyphens,
        }
    finally:
        textpage.close()
