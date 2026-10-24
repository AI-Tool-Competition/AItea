"""Exclude only uniquely bound, wholly off-page amounts from OCR comparison.

The native layer and its visibility audit stay intact. This is not permission
to omit clipped, covered, invisible, or otherwise ambiguous printed content.
"""

import hashlib
import json
import math
import re
from collections import Counter

from app.contexts.documents.infrastructure.parsers.pdf_limits import ARCHIVE_MAX_PAGES
from app.contexts.documents.infrastructure.parsers.pdf_text_visibility import VERSION as VISIBILITY_VERSION

VERSION = "offpage-native-amount-comparison-v1"
_OUTSIDE = "native_text_outside_page_bounds"
_MAX_CHARS = 200_000


def _coordinates(value, count):
    return (
        isinstance(value, (list, tuple))
        and len(value) == count
        and all(type(v) in {int, float} and math.isfinite(v) and abs(v) <= 20_000 for v in value)
    )


def _box(value):
    return _coordinates(value, 4) and value[0] < value[2] and value[1] < value[3]


def _offpage_objects(page):
    native, audit = page.get("native_text"), page.get("native_visibility")
    ocr = page.get("ocr")
    if (
        not isinstance(native, str)
        or not 0 < len(native) <= _MAX_CHARS
        or not isinstance(audit, dict)
        or not isinstance(page.get("source_hash"), str)
        or re.fullmatch(r"[a-f0-9]{64}", page["source_hash"]) is None
        or type(page.get("page")) is not int
        or not 1 <= page["page"] <= ARCHIVE_MAX_PAGES
        or audit.get("version") != VISIBILITY_VERSION
        or audit.get("source_hash") != page["source_hash"]
        or not isinstance(ocr, dict)
        or ocr.get("source_hash") != page["source_hash"]
        or type(ocr.get("page")) is not int
        or ocr["page"] != page["page"]
        or ocr.get("complete") is not True
        or type(audit.get("page")) is not int
        or audit["page"] != page["page"]
        or audit.get("native_text_sha256") != hashlib.sha256(native.encode()).hexdigest()
        or audit.get("native_text_field") != "pages[].native_text"
        or audit.get("visibility_complete_certified") is not False
        or audit.get("requires_visual_review") is not True
        or audit.get("unknown_reasons") != []
        or audit.get("reasons") != [_OUTSIDE]
        or type(audit.get("annotation_count")) is not int
        or audit["annotation_count"] != 0
    ):
        return None
    bounds, size, evidence = audit.get("page_bounds"), audit.get("page_size"), audit.get("evidence")
    object_count, text_count = audit.get("object_count"), audit.get("text_object_count")
    if (
        not _box(bounds)
        or not _coordinates(size, 2)
        or abs(size[0] - (bounds[2] - bounds[0])) > 0.01
        or abs(size[1] - (bounds[3] - bounds[1])) > 0.01
        or not isinstance(evidence, list)
        or not 0 < len(evidence) <= 1024
        or type(object_count) is not int
        or type(text_count) is not int
        or not len(evidence) <= text_count <= object_count <= 4096
    ):
        return None
    orders, characters = set(), 0
    for item in evidence:
        if not isinstance(item, dict):
            return None
        box, matrix, fill = item.get("bounds_pdf_bottom_left"), item.get("matrix"), item.get("fill_rgba")
        text, order = item.get("text"), item.get("object_order")
        if (
            not isinstance(text, str)
            or not 0 < len(text) <= 16384
            or item.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest()
            or type(order) is not int
            or not 0 <= order < object_count
            or order in orders
            or item.get("reasons") != [_OUTSIDE]
            or item.get("active") is not True
            or item.get("clips") != []
            or item.get("later_paint_evidence", []) != []
            or type(item.get("render_mode")) is not int
            or item["render_mode"] != 0
            or not isinstance(fill, list)
            or len(fill) != 4
            or any(type(v) is not int or not 0 <= v <= 255 for v in fill)
            or fill[3] != 255
            or fill[:3] == [255, 255, 255]
            or not _coordinates(matrix, 6)
            or tuple(matrix[:4]) != (1, 0, 0, 1)
            or not _box(box)
            # Require a gap, not just a partly clipped or touching rectangle.
            or not (
                box[2] < bounds[0] - 1
                or box[0] > bounds[2] + 1
                or box[3] < bounds[1] - 1
                or box[1] > bounds[3] + 1
            )
        ):
            return None
        orders.add(order)
        characters += len(text)
        if characters > _MAX_CHARS:
            return None
    return evidence


def filter_offpage_amount_anchors(page, anchors):
    """Return comparison anchors and reproducible proof without changing a page."""
    from app.contexts.documents.infrastructure.parsers.ingestion_quality import anchor_present, number_anchors

    evidence = _offpage_objects(page)
    if evidence is None:
        return anchors, None
    candidates = [
        (anchor, item)
        for item in evidence
        for anchor in number_anchors(item["text"])
        if anchor["field"] == "amount"
    ]
    native_counts = Counter(anchor["value"] for anchor in anchors if anchor["field"] == "amount")
    evidence_counts = Counter(anchor["value"] for anchor, _ in candidates)
    objects = {anchor["value"]: item for anchor, item in candidates}
    transcribed = number_anchors(page.get("text", ""))
    excluded, kept = [], []
    for anchor in anchors:
        value = anchor["value"]
        if (
            anchor["field"] != "amount"
            or native_counts[value] != 1
            or evidence_counts[value] != 1
            or anchor_present(anchor, transcribed)
        ):
            kept.append(anchor)
            continue
        item = objects[value]
        excluded.append(
            {
                "native_anchor": anchor,
                "object_order": item["object_order"],
                "object_text_sha256": item["text_sha256"],
                "bounds_pdf_bottom_left": list(item["bounds_pdf_bottom_left"]),
            }
        )
    if not excluded:
        return anchors, None
    audit = page["native_visibility"]
    try:
        audit_hash = hashlib.sha256(
            json.dumps(audit, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
    except (ValueError, TypeError):
        return anchors, None
    proof = {
        "version": VERSION,
        "source_hash": page["source_hash"],
        "page": page["page"],
        "native_text_sha256": audit["native_text_sha256"],
        "visibility_audit_sha256": audit_hash,
        "excluded_amounts": excluded,
    }
    return kept, proof
