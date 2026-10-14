"""Painted grid hints in page coordinates, never a completeness certificate.

PDF paths use the current transformation matrix; q/Q also scope that matrix.
Only stroked straight segments and filled thin rectangular rules are grid
boundaries. A straight edge of a filled curved icon is not a painted rule.
Form XObjects are visited in their own matrix/resource scope. Unsupported or
malformed geometry remains a conservative layout-review hint.
"""

from __future__ import annotations

import hashlib
import json
import math

VERSION = "painted-grid-ctm-v3"
_MAX_OPERATIONS = 1_000_000
_MAX_STREAM_OPERATIONS = 100_000
_MAX_RULES = 4096
_MAX_FORMS = 2048
_MAX_FORM_DEPTH = 8
_MAX_STREAM_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_STREAM_BYTES = 64 * 1024 * 1024
_IDENTITY = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_PAINT = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"n"}


class _Unknown(ValueError):
    pass


def _numbers(values, length):
    if len(values) != length:
        raise _Unknown("invalid_operator_arity")
    try:
        result = tuple(float(v) for v in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _Unknown("invalid_geometry_number") from exc
    if any(not math.isfinite(v) or abs(v) > 1e12 for v in result):
        raise _Unknown("invalid_geometry_number")
    return result


def _multiply(left, right):
    a, b, c, d, e, f = left
    g, h, i, j, k, offset_y = right
    return (
        a * g + b * i,
        a * h + b * j,
        c * g + d * i,
        c * h + d * j,
        e * g + f * i + k,
        e * h + f * j + offset_y,
    )


def _point(point, matrix):
    x, y = point
    a, b, c, d, e, f = matrix
    result = (a * x + c * y + e, b * x + d * y + f)
    if any(not math.isfinite(v) or abs(v) > 1e12 for v in result):
        raise _Unknown("transformed_geometry_out_of_bounds")
    return result


def _connected_grid(horizontal, vertical):
    from .pdf_tables import _cluster

    def merged(lines):
        positions = _cluster([line[0] for line in lines], tolerance=2)
        if len(positions) > 256:
            raise _Unknown("grid_axis_limit")
        result = []
        for position in positions:
            combined = []
            for a, b in sorted((a, b) for p, a, b in lines if abs(p - position) <= 2):
                if combined and a <= combined[-1][1] + 1.5:
                    combined[-1] = (combined[-1][0], max(combined[-1][1], b))
                else:
                    combined.append((a, b))
            result.extend((position, a, b) for a, b in combined)
        return result

    hs, vs = merged(horizontal), merged(vertical)
    for index, (left, low, high) in enumerate(vs):
        for right, start, end in vs[index + 1 :]:
            if right - left <= 6:
                continue
            bottom, top = max(low, start), min(high, end)
            if top - bottom < 12:
                continue
            rows = _cluster(
                [
                    y
                    for y, a, b in hs
                    if bottom - 1.5 <= y <= top + 1.5 and a <= left + 1.5 and b >= right - 1.5
                ],
                tolerance=2,
            )
            if len(rows) >= 3 and any(
                left + 3 < x < right - 3 and min(b, rows[-1]) - max(a, rows[0]) > 6 for x, a, b in vs
            ):
                return {"bounds": [left, rows[0], right, rows[-1]], "row_rules": rows}
    return None


def classify_table_layout(stream, resources=None) -> dict:
    """Return a source-geometry hint. False never certifies text or raster tables."""
    horizontal, vertical = [], []
    counts = {
        "operations": 0,
        "stream_bytes": 0,
        "forms": 0,
        "images": 0,
        "ctm_updates": 0,
        "graphics_restores": 0,
        "painted_paths": 0,
        "fill_only_curved_paths_excluded": 0,
    }
    unknown = None

    def line(a, b):
        dx, dy = abs(a[0] - b[0]), abs(a[1] - b[1])
        if dx > 20 and dy < 1:
            horizontal.append(((a[1] + b[1]) / 2, min(a[0], b[0]), max(a[0], b[0])))
        if dy > 20 and dx < 1:
            vertical.append(((a[0] + b[0]) / 2, min(a[1], b[1]), max(a[1], b[1])))
        if len(horizontal) > _MAX_RULES or len(vertical) > _MAX_RULES:
            raise _Unknown("painted_rule_limit")

    def visit(current, current_resources, initial, depth, ancestors):
        if current is None:
            return
        if depth > _MAX_FORM_DEPTH:
            raise _Unknown("form_depth_limit")
        data = current.get_data()
        if len(data) > _MAX_STREAM_BYTES:
            raise _Unknown("content_stream_size_limit")
        counts["stream_bytes"] += len(data)
        if counts["stream_bytes"] > _MAX_TOTAL_STREAM_BYTES:
            raise _Unknown("total_content_stream_size_limit")
        matrix = initial
        state_stack = []
        fill_alpha = stroke_alpha = 1.0
        subpaths = []
        path = None
        for ordinal, (args, op) in enumerate(current.operations):
            if ordinal >= _MAX_STREAM_OPERATIONS:
                raise _Unknown("stream_operation_limit")
            counts["operations"] += 1
            if counts["operations"] > _MAX_OPERATIONS:
                raise _Unknown("operation_limit")
            if op == b"q":
                if len(state_stack) >= 128:
                    raise _Unknown("graphics_state_depth_limit")
                state_stack.append((matrix, fill_alpha, stroke_alpha))
            elif op == b"Q":
                if not state_stack:
                    raise _Unknown("unbalanced_graphics_state")
                matrix, fill_alpha, stroke_alpha = state_stack.pop()
                counts["graphics_restores"] += 1
            elif op == b"cm":
                values = _numbers(args, 6)
                if abs(values[0] * values[3] - values[1] * values[2]) < 1e-12:
                    raise _Unknown("singular_transform")
                matrix = _numbers(_multiply(values, matrix), 6)
                counts["ctm_updates"] += 1
            elif op == b"gs":
                if len(args) != 1 or current_resources is None:
                    raise _Unknown("missing_graphics_state_resource")
                state = current_resources.get("/ExtGState", {}).get(args[0])
                if state is None:
                    raise _Unknown("missing_graphics_state_resource")
                state = state.get_object()
                fill_alpha = _numbers([state.get("/ca", fill_alpha)], 1)[0]
                stroke_alpha = _numbers([state.get("/CA", stroke_alpha)], 1)[0]
                if not 0 <= fill_alpha <= 1 or not 0 <= stroke_alpha <= 1:
                    raise _Unknown("invalid_paint_opacity")
            elif op == b"re":
                x, y, w, h = _numbers(args, 4)
                points = [_point(p, matrix) for p in ((x, y), (x + w, y), (x + w, y + h), (x, y + h))]
                path = {"points": points, "curve": False, "curve_edges": set(), "closed": True}
                subpaths.append(path)
            elif op == b"m":
                path = {
                    "points": [_point(_numbers(args, 2), matrix)],
                    "curve": False,
                    "curve_edges": set(),
                    "closed": False,
                }
                subpaths.append(path)
            elif op == b"l":
                if path is None:
                    raise _Unknown("line_without_moveto")
                path["points"].append(_point(_numbers(args, 2), matrix))
            elif op in {b"c", b"v", b"y"}:
                if path is None:
                    raise _Unknown("curve_without_moveto")
                values = _numbers(args, 6 if op == b"c" else 4)
                path["curve"] = True
                path["curve_edges"].add(len(path["points"]) - 1)
                path["points"].append(_point(values[-2:], matrix))
            elif op == b"h":
                if path is None:
                    raise _Unknown("close_without_path")
                path["closed"] = True
            elif op in _PAINT:
                stroke = op in {b"S", b"s", b"B", b"B*", b"b", b"b*"} and stroke_alpha > 0
                fill = op in {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"} and fill_alpha > 0
                if op in {b"s", b"b", b"b*"} and path:
                    path["closed"] = True
                if stroke or fill:
                    counts["painted_paths"] += 1
                for shape in subpaths:
                    points = shape["points"]
                    # Curved fill contours are silhouettes, not stroked grid rules.
                    if shape["curve"] and fill and not stroke:
                        counts["fill_only_curved_paths_excluded"] += 1
                    if stroke:
                        for index, (a, b) in enumerate(zip(points, points[1:])):
                            if index not in shape["curve_edges"]:
                                line(a, b)
                        if shape["closed"] and len(points) > 1:
                            line(points[-1], points[0])
                    if fill and not shape["curve"]:
                        vertices = points[:-1] if len(points) > 1 and points[0] == points[-1] else points
                        if len(vertices) == 4 and all(
                            abs(a[0] - b[0]) < 1 or abs(a[1] - b[1]) < 1
                            for a, b in zip(vertices, vertices[1:] + vertices[:1])
                        ):
                            xs, ys = [p[0] for p in vertices], [p[1] for p in vertices]
                            left, right, low, high = min(xs), max(xs), min(ys), max(ys)
                            if right - left > 20 and 0 < high - low < 2:
                                line((left, (low + high) / 2), (right, (low + high) / 2))
                            if high - low > 20 and 0 < right - left < 2:
                                line(((left + right) / 2, low), ((left + right) / 2, high))
                subpaths, path = [], None
            elif op == b"Do":
                if len(args) != 1 or current_resources is None:
                    raise _Unknown("uninspected_xobject")
                obj = current_resources.get("/XObject", {}).get(args[0])
                if obj is None:
                    raise _Unknown("missing_xobject")
                obj = obj.get_object()
                subtype = obj.get("/Subtype")
                if subtype == "/Image":
                    counts["images"] += 1
                    continue  # Image text/layout stays the existing raster OCR requirement.
                if subtype != "/Form":
                    raise _Unknown("unsupported_xobject")
                counts["forms"] += 1
                if counts["forms"] > _MAX_FORMS or id(obj) in ancestors:
                    raise _Unknown("form_recursion_or_count_limit")
                from pypdf.generic import ContentStream

                form = ContentStream(obj, current.pdf)
                transform = _numbers(obj.get("/Matrix", _IDENTITY), 6)
                if abs(transform[0] * transform[3] - transform[1] * transform[2]) < 1e-12:
                    raise _Unknown("singular_form_transform")
                visit(
                    form,
                    obj.get("/Resources", current_resources),
                    _numbers(_multiply(transform, matrix), 6),
                    depth + 1,
                    ancestors | {id(obj)},
                )
        if state_stack:
            raise _Unknown("unbalanced_graphics_state")

    try:
        visit(stream, resources, _IDENTITY, 0, set())
        grid = _connected_grid(horizontal, vertical)
    except (_Unknown, TypeError, ValueError, AttributeError, KeyError, OverflowError, RecursionError) as exc:
        grid, unknown = None, str(exc) if isinstance(exc, _Unknown) else "unsupported_stream_or_resource"
    rules = {"horizontal": horizontal, "vertical": vertical}
    return {
        "version": VERSION,
        "requires_table_review": bool(grid or unknown),
        "classification": "unknown"
        if unknown
        else "connected_painted_grid"
        if grid
        else "no_connected_painted_grid",
        "unknown_reason": unknown,
        "grid": grid,
        "counts": counts,
        "horizontal_rules": len(horizontal),
        "vertical_rules": len(vertical),
        "rules_sha256": hashlib.sha256(
            json.dumps(rules, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "plain_text_complete_certified": False,
        "raster_text_or_table_certified": False,
    }


def requires_table_review(stream, resources=None) -> bool:
    return classify_table_layout(stream, resources)["requires_table_review"]
