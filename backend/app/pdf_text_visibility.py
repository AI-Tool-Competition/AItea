"""Conservative native-text evidence checks inside the existing PDF worker.

These checks flag demonstrable clipping and unsupported visibility conditions.
They never delete source text or certify that every PDF painting feature was
interpreted. Rendering remains owned by the existing bounded page renderer.
"""

import base64
import ctypes
import hashlib
import io
import math
import re
import unicodedata

VERSION = "pdf-native-visibility-v1"
_HASH = re.compile(r"[a-f0-9]{64}")
_MAX_OBJECTS = 4096
_MAX_OBJECT_TEXT = 16384
_MAX_EVIDENCE = 1024
_MAX_PAINT_COMPARISONS = 1_000_000
_MAX_CLIPS = 8
_MAX_SEGMENTS = 64
_MAX_PIXELS = 6_000_000
_MAX_COMPARISON_CHARS = 20_000


def _finite(values):
    return all(math.isfinite(value) and abs(value) <= 20_000 for value in values)


def _box(values):
    values = tuple(float(value) for value in values)
    if len(values) != 4 or not _finite(values) or values[0] > values[2] or values[1] > values[3]:
        raise ValueError("PDF 可见性坐标无效。")
    return values


def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _intersects(a, b):
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _rgba(raw, function, obj):
    values = [ctypes.c_uint() for _ in range(4)]
    if not function(obj, *values):
        raise ValueError("PDF 绘制颜色无法核验。")
    return [value.value for value in values]


def _rectangle(segments):
    if len(segments) not in {4, 5} or segments[0]["type"] != 2:
        return None
    if any(segment["type"] != 0 for segment in segments[1:]):
        return None
    points = [tuple(segment["xy"]) for segment in segments]
    if len(points) == 5:
        if points[0] != points[-1]:
            return None
        points.pop()
    xs, ys = {point[0] for point in points}, {point[1] for point in points}
    if len(xs) != 2 or len(ys) != 2 or len(set(points)) != 4:
        return None
    if any(a[0] != b[0] and a[1] != b[1] for a, b in zip(points, points[1:] + points[:1], strict=True)):
        return None
    return _box((min(xs), min(ys), max(xs), max(ys)))


def _segments(raw, count, getter):
    if not 0 <= count <= _MAX_SEGMENTS:
        raise ValueError("PDF 可见性路径超过限制。")
    result = []
    for index in range(count):
        segment = getter(index)
        x, y = ctypes.c_float(), ctypes.c_float()
        if not segment or not raw.FPDFPathSegment_GetPoint(segment, x, y) or not _finite((x.value, y.value)):
            raise ValueError("PDF 可见性路径坐标无法核验。")
        result.append({"xy": [x.value, y.value], "type": raw.FPDFPathSegment_GetType(segment)})
    return result


def _clip_evidence(raw, obj):
    clip = raw.FPDFPageObj_GetClipPath(obj)
    if not clip:
        return []
    count = raw.FPDFClipPath_CountPaths(clip)
    # PDFium returns the valid object's internal clip state even when it has
    # no reference; CountPaths returns -1 for that empty state (HasRef=false).
    # See fpdfsdk/fpdf_transformpage.cpp, not a NULL/failed page-object handle.
    if count == -1:
        return []
    if not 0 <= count <= _MAX_CLIPS:
        raise ValueError("PDF 可见性裁剪路径超过限制。")
    result = []
    for index in range(count):
        segments = _segments(
            raw,
            raw.FPDFClipPath_CountPathSegments(clip, index),
            lambda at, index=index: raw.FPDFClipPath_GetPathSegment(clip, index, at),
        )
        result.append({"segments": segments, "rectangle": _rectangle(segments)})
    return result


def _hyphen_reader_comparison(raw, textpage, native_text, object_text, text_objects):
    """Reconcile only API-proved line-end hyphens in a derived comparison.

    PDFium marks these characters U+0002 while retaining their source glyph.
    Every non-space character must agree in order; source text is never edited.
    This does not resolve clipping, painting, Forms or optional content.
    """
    try:
        count = textpage.count_chars()
        if not 0 < count <= _MAX_COMPARISON_CHARS or len(native_text) > _MAX_COMPARISON_CHARS:
            return None
        chars, candidate, markers = [], [], []
        per_object_chars = {}
        normalized_index = 0
        for index in range(count):
            value = raw.FPDFText_GetUnicode(textpage.raw, index)
            if not 0 <= value <= 0x10FFFF or 0xD800 <= value <= 0xDFFF:
                return None
            char = chr(value)
            chars.append(char)
            obj = raw.FPDFText_GetTextObject(textpage.raw, index)
            address = ctypes.cast(obj, ctypes.c_void_p).value if obj else None
            if address is not None:
                per_object_chars.setdefault(address, []).append(char)
            if value == 2:
                flags = (
                    raw.FPDFText_IsHyphen(textpage.raw, index),
                    raw.FPDFText_IsGenerated(textpage.raw, index),
                    raw.FPDFText_HasUnicodeMapError(textpage.raw, index),
                )
                if flags != (1, 0, 0) or len(markers) >= _MAX_EVIDENCE or address not in text_objects:
                    return None
                markers.append(
                    {
                        "pdfium_char_index": index,
                        "normalized_nonspace_index": normalized_index,
                        "unicode": value,
                        "is_hyphen": flags[0],
                        "is_generated": flags[1],
                        "unicode_map_error": flags[2],
                        "comparison_character": "-",
                        "page_text_object_order": text_objects[address][0],
                    }
                )
                char = "-"
            candidate.append(char)
            normalized_index += sum(not item.isspace() for item in unicodedata.normalize("NFKC", char))
        if not markers:
            return None

        def normalized(text):
            return "".join(char for char in unicodedata.normalize("NFKC", text) if not char.isspace())

        reader_text = "".join(chars)
        comparison_text = "".join(candidate)
        # This audit inspected the page's root text objects. Do not use the
        # full textpage to silently fill a missing/unsupported Form subtree.
        if normalized(object_text) != normalized(reader_text):
            return None
        for address, values in per_object_chars.items():
            if any(value == "\x02" for value in values):
                if address not in text_objects or normalized("".join(values)) != normalized(
                    text_objects[address][1]
                ):
                    return None
        native_sequence = normalized(native_text)
        if normalized(comparison_text) != native_sequence:
            return None
        # NFKC can map full-width or compatibility symbols to '-'. Each alias
        # must instead bind to a literal U+002D in the immutable native string.
        native_positions, expanded = [], []
        for native_index, char in enumerate(native_text):
            for normalized_char in unicodedata.normalize("NFKC", char):
                if not normalized_char.isspace():
                    expanded.append(normalized_char)
                    native_positions.append((native_index, char))
        if "".join(expanded) != native_sequence:
            return None
        for marker in markers:
            native_index, native_char = native_positions[marker["normalized_nonspace_index"]]
            if native_char != "-":
                return None
            marker["native_char_index"] = native_index
        from pypdfium2.version import PDFIUM_INFO, PYPDFIUM_INFO

        return {
            "version": "pdfium-api-hyphen-reader-comparison-v1",
            "purpose": "derived_reader_comparison_only",
            "native_text_unchanged": True,
            "native_text_sha256": hashlib.sha256(native_text.encode()).hexdigest(),
            "object_reader_text_sha256": hashlib.sha256(object_text.encode()).hexdigest(),
            "api_reader_text_sha256": hashlib.sha256(reader_text.encode()).hexdigest(),
            "comparison_text_sha256": hashlib.sha256(comparison_text.encode()).hexdigest(),
            "normalized_native_sequence_sha256": hashlib.sha256(native_sequence.encode()).hexdigest(),
            "normalization": "NFKC_nonspace_exact_order",
            "pypdfium2_version": str(PYPDFIUM_INFO),
            "pdfium_version": str(PDFIUM_INFO),
            "pdfium_char_count": count,
            "markers": markers,
        }
    except Exception:  # noqa: BLE001 -- missing/ambiguous API proof keeps the original disagreement
        return None


def audit_page_visibility(page, native_text, *, source_hash, page_number, optional_content=False):
    """Inspect an already-open PDFium page in its resource-bounded caller.

    A negative finding means this limited evidence check found no supported
    fault; ``visibility_complete_certified`` always remains false. The caller
    keeps the original native text and sends risky/unknown pages to recovery.
    """
    if (
        not isinstance(source_hash, str)
        or not _HASH.fullmatch(source_hash)
        or type(page_number) is not int
        or not 1 <= page_number <= 5000
        or not isinstance(native_text, str)
        or len(native_text) > 4_000_000
        or type(optional_content) is not bool
    ):
        raise ValueError("PDF 可见性检查需要有效原件和物理页定位。")
    result = {
        "version": VERSION,
        "source_hash": source_hash,
        "page": page_number,
        "native_text_sha256": hashlib.sha256(native_text.encode()).hexdigest(),
        "native_text_field": "pages[].native_text",
        "visibility_complete_certified": False,
        "requires_visual_review": True,
        "reasons": [],
        "unknown_reasons": [],
        "evidence": [],
    }
    try:
        import pypdfium2.raw as raw
        from .pdf_tables import _characters

        bounds = _box(page.get_bbox())
        size = tuple(float(value) for value in page.get_size())
        if len(size) != 2 or not _finite(size) or min(size) <= 0:
            raise ValueError("PDF 可见性页面尺寸无效。")
        result.update(page_bounds=list(bounds), page_size=list(size))
        if page.get_rotation():
            result["unknown_reasons"].append("rotated_page_geometry_unverified")
        if optional_content:
            result["unknown_reasons"].append("optional_content_visibility_unverified")
        if raw.FPDFPage_HasTransparency(page.raw):
            result["unknown_reasons"].append("page_transparency_unverified")
        annotations = raw.FPDFPage_GetAnnotCount(page.raw)
        result["annotation_count"] = annotations
        if annotations != 0:
            result["unknown_reasons"].append("annotation_visibility_unverified")
        count = raw.FPDFPage_CountObjects(page.raw)
        if not 0 <= count <= _MAX_OBJECTS:
            raise ValueError("PDF 可见性对象超过限制。")
        result["object_count"] = count
        texts, paints, text_objects = [], [], {}
        textpage = page.get_textpage()
        try:
            for index, obj in enumerate(page.get_objects(max_depth=1, textpage=textpage)):
                if index >= _MAX_OBJECTS:
                    raise ValueError("PDF 可见性对象超过限制。")
                geometry = _box(obj.get_bounds())
                matrix = tuple(float(value) for value in obj.get_matrix().get())
                if len(matrix) != 6 or not _finite(matrix):
                    raise ValueError("PDF 可见性变换矩阵无效。")
                if obj.level or obj.type == raw.FPDF_PAGEOBJ_FORM:
                    result["unknown_reasons"].append("form_visibility_geometry_unverified")
                    continue
                if obj.type == raw.FPDF_PAGEOBJ_TEXT:
                    text = obj.extract()
                    if len(text) > _MAX_OBJECT_TEXT:
                        raise ValueError("PDF 可见性文字对象超过限制。")
                    if not text.strip():
                        continue
                    text_objects[ctypes.cast(obj.raw, ctypes.c_void_p).value] = (index, text)
                    row = {
                        "object_order": index,
                        "text": text,
                        "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "bounds_pdf_bottom_left": list(geometry),
                        "matrix": list(matrix),
                        "render_mode": raw.FPDFTextObj_GetTextRenderMode(obj.raw),
                        "fill_rgba": _rgba(raw, raw.FPDFPageObj_GetFillColor, obj.raw),
                        "stroke_rgba": _rgba(raw, raw.FPDFPageObj_GetStrokeColor, obj.raw),
                        "clips": _clip_evidence(raw, obj.raw),
                        "reasons": [],
                    }
                    active = ctypes.c_int()
                    if not raw.FPDFPageObj_GetIsActive(obj.raw, active):
                        raise ValueError("PDF 文字对象启用状态无法核验。")
                    row["active"] = bool(active.value)
                    if (
                        geometry[0] == geometry[2]
                        or geometry[1] == geometry[3]
                        or matrix[0] * matrix[3] - matrix[1] * matrix[2] == 0
                    ):
                        row["reasons"].append("degenerate_native_text_geometry")
                    if not active.value:
                        row["reasons"].append("inactive_native_text")
                    if row["render_mode"] not in {0, 1, 2}:
                        row["reasons"].append("unsupported_or_invisible_text_render_mode")
                    colors = [row["fill_rgba"]] if row["render_mode"] == 0 else [row["stroke_rgba"]]
                    if row["render_mode"] == 2:
                        colors.append(row["fill_rgba"])
                    if all(color[3] == 0 for color in colors):
                        row["reasons"].append("transparent_native_text")
                    if all(color[:3] == [255, 255, 255] for color in colors):
                        row["reasons"].append("white_native_text_requires_pixel_review")
                    if not _contains(bounds, geometry):
                        row["reasons"].append("native_text_outside_page_bounds")
                    for clip in row["clips"]:
                        if clip["rectangle"] is None:
                            row["reasons"].append("native_text_clip_geometry_unverified")
                        elif not _contains(clip["rectangle"], geometry):
                            row["reasons"].append(
                                "native_text_outside_clip"
                                if not _intersects(clip["rectangle"], geometry)
                                else "native_text_partially_clipped"
                            )
                    texts.append(row)
                elif obj.type == raw.FPDF_PAGEOBJ_PATH:
                    fill, stroke = ctypes.c_int(), ctypes.c_int()
                    if not raw.FPDFPath_GetDrawMode(obj.raw, fill, stroke):
                        raise ValueError("PDF 路径绘制模式无法核验。")
                    if not fill.value:
                        continue
                    color = _rgba(raw, raw.FPDFPageObj_GetFillColor, obj.raw)
                    if color[3] == 0:
                        continue
                    segment_count = raw.FPDFPath_CountSegments(obj.raw)
                    if not 0 <= segment_count <= 100_000:
                        raise ValueError("PDF 覆盖路径超过限制。")
                    segments = (
                        _segments(
                            raw, segment_count, lambda at, obj=obj: raw.FPDFPath_GetPathSegment(obj.raw, at)
                        )
                        if segment_count <= _MAX_SEGMENTS
                        else []
                    )
                    paints.append(
                        {
                            "object_order": index,
                            "kind": "filled_path",
                            "bounds_pdf_bottom_left": list(geometry),
                            "fill_rgba": color,
                            "draw_mode": {"fill": fill.value, "stroke": stroke.value},
                            "rectangle_path_coordinates": _rectangle(segments),
                            "matrix": list(matrix),
                            "path_segment_count": segment_count,
                            "geometry_scope": "expanded_small_path" if segments else "painted_bounds_only",
                        }
                    )
                elif obj.type in {raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_SHADING}:
                    paints.append(
                        {
                            "object_order": index,
                            "kind": "image_or_shading",
                            "bounds_pdf_bottom_left": list(geometry),
                        }
                    )
                else:
                    result["unknown_reasons"].append("page_object_visibility_unverified")
            if len(texts) * len(paints) > _MAX_PAINT_COMPARISONS:
                raise ValueError("PDF 可见性绘制覆盖检查超过限制。")
            for text in texts:
                for paint in paints:
                    if paint["object_order"] <= text["object_order"] or not _intersects(
                        paint["bounds_pdf_bottom_left"], text["bounds_pdf_bottom_left"]
                    ):
                        continue
                    text["reasons"].append("later_painted_object_overlaps_native_text")
                    text.setdefault("later_paint_evidence", []).append(paint)
                    if len(text["later_paint_evidence"]) > _MAX_EVIDENCE:
                        raise ValueError("PDF 可见性覆盖证据超过限制。")
                if text["reasons"]:
                    text["reasons"] = sorted(set(text["reasons"]))
                    result["reasons"].extend(text["reasons"])
                    result["evidence"].append(text)
                    if len(result["evidence"]) > _MAX_EVIDENCE:
                        raise ValueError("PDF 可见性证据超过限制。")
            object_text = "".join(text["text"] for text in texts)
            if _characters(native_text) != _characters(object_text):
                comparison = _hyphen_reader_comparison(raw, textpage, native_text, object_text, text_objects)
                if comparison is None:
                    result["unknown_reasons"].append("native_text_reader_disagreement")
                else:
                    result["reader_comparison"] = comparison
            result["text_object_count"] = len(texts)
        finally:
            textpage.close()
    except Exception as exc:  # noqa: BLE001 -- unsupported PDF evidence must fail closed
        result["unknown_reasons"].append("visibility_evidence_unavailable:" + type(exc).__name__)
    result["reasons"] = sorted(set(result["reasons"]))
    result["unknown_reasons"] = sorted(set(result["unknown_reasons"]))
    result["requires_visual_review"] = bool(result["reasons"] or result["unknown_reasons"])
    return result


def prove_blank_page(rendered, audit, *, native_text):
    """Return durable full-page pixel evidence, or None, without rendering.

    Only the trusted bounded renderer's exact opaque white PNG is accepted.
    The caller stores the returned pixels before marking its source page blank.
    """
    from .parsing import _MAX_IMAGE_BYTES

    if not isinstance(rendered, dict) or not isinstance(audit, dict) or not isinstance(native_text, str):
        return None
    if (
        native_text.strip()
        or audit.get("native_text_sha256") != hashlib.sha256(native_text.encode()).hexdigest()
        or audit.get("version") != VERSION
        or audit.get("requires_visual_review") is not False
        or audit.get("reasons") != []
        or audit.get("unknown_reasons") != []
        or not isinstance(audit.get("source_hash"), str)
        or not _HASH.fullmatch(audit["source_hash"])
        or type(audit.get("page")) is not int
        or not 1 <= audit["page"] <= 5000
        or type(audit.get("annotation_count")) is not int
        or type(audit.get("text_object_count")) is not int
        or audit.get("annotation_count") != 0
        or audit.get("text_object_count") != 0
        or audit.get("source_hash") != rendered.get("source_hash")
        or audit.get("page") != rendered.get("page")
        or rendered.get("mime_type") != "image/png"
        or rendered.get("renderer") != "pypdfium2"
        or rendered.get("render_version") != "bounded-page-png-v1"
        or "region_bbox" in rendered
        or not isinstance(rendered.get("image_base64"), str)
        or len(rendered["image_base64"]) > (_MAX_IMAGE_BYTES + 2) // 3 * 4
        or not isinstance(rendered.get("image_sha256"), str)
        or not _HASH.fullmatch(rendered["image_sha256"])
    ):
        return None
    try:
        from PIL import Image

        dpi = rendered.get("dpi")
        width, height = rendered.get("width"), rendered.get("height")
        size = audit.get("page_size")
        if (
            type(dpi) not in {int, float}
            or not math.isfinite(dpi)
            or not 0 < dpi <= 400
            or type(width) is not int
            or type(height) is not int
            or min(width, height) <= 0
            or width * height > _MAX_PIXELS
            or not isinstance(size, list)
            or len(size) != 2
            or any(
                abs(actual - math.ceil(canvas * dpi / 72)) > 1
                for actual, canvas in zip((width, height), size, strict=True)
            )
            or not all(
                isinstance(rendered.get(key), str) and 0 < len(rendered[key]) <= 100
                for key in ("renderer_version", "pdfium_version")
            )
        ):
            return None
        data = base64.b64decode(rendered["image_base64"], validate=True)
        if (
            not data
            or len(data) > _MAX_IMAGE_BYTES
            or hashlib.sha256(data).hexdigest() != rendered["image_sha256"]
        ):
            return None
        with Image.open(io.BytesIO(data)) as image:
            if (
                image.format != "PNG"
                or image.size != (width, height)
                or getattr(image, "n_frames", 1) != 1
                or image.mode not in {"RGB", "RGBA"}
            ):
                return None
            image.load()
            extrema = image.getextrema()
            if extrema != ((255, 255),) * len(extrema):
                return None
        return {
            **rendered,
            "version": VERSION,
            "is_blank": True,
            "proof_kind": "verified_full_page_opaque_white_pixels",
            "native_text_sha256": audit["native_text_sha256"],
            "visibility_audit_version": audit["version"],
            "model_calls": 0,
        }
    except (ValueError, TypeError, OSError, MemoryError):
        return None
