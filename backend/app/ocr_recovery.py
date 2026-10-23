"""Bounded, auditable pixel evidence for conflicting OCR anchors.

The native layer locates rectangles only. Two independently framed, blind reads
must agree before replacing an exact transcript span. This never approves a law.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from difflib import SequenceMatcher
import hashlib
import json
import math
import os
import re
import time
from uuid import uuid4

from .ingestion_quality import anchor_present, number_anchors, validate_ocr_page
from .pdf_limits import ARCHIVE_MAX_PAGES

RECOVERY_VERSION = "legal-region-recovery-v5"
MAX_TASKS = 24
_MAX_LOCATOR_CHARS = 20_000
_NON_LITERAL_UNCERTAINTIES = {
    "model_reported_incomplete",
    "table_layout_not_preserved",
    "blank_response_conflicts_with_native_text",
    "transcription_much_shorter_than_native_text",
    "illegible_printed_text",
    "region_provider_failed",
    "region_render_failed",
    "region_attempt_budget_exhausted",
    "recovery_baseline_changed",
    "recovery_span_conflict",
}
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


def _normal_text(text: str) -> str:
    return " ".join(text.split()).casefold()


def _literal_spans(text: str, literal: str) -> list[list[int]]:
    """Find complete literal spans; ignore whitespace, never letters or case.

    Coordinates always refer to the unmodified text. A match inside a longer
    word is not the uncertainty's token, even when its letters are identical.
    """
    compact = "".join(literal.split())
    if not compact:
        return []
    indices = [index for index, char in enumerate(text) if not char.isspace()]
    haystack = "".join(text[index] for index in indices)
    result, cursor = [], 0
    while (found := haystack.find(compact, cursor)) >= 0:
        start, end = indices[found], indices[found + len(compact) - 1] + 1
        first, last = literal.strip()[0], literal.strip()[-1]
        left_ok = (
            not (first.isalnum() or first == "_")
            or start == 0
            or not (text[start - 1].isalnum() or text[start - 1] == "_")
        )
        right_ok = (
            not (last.isalnum() or last == "_")
            or end == len(text)
            or not (text[end].isalnum() or text[end] == "_")
        )
        if left_ok and right_ok:
            result.append([start, end])
        cursor = found + 1
    return result


def _locator_literal_spans(text: str, literal: str) -> list[list[int]]:
    """Match superscript digits as their physical-text layer digit glyphs.

    Some PDFs expose a superscript glyph as a baseline ASCII digit. Both map
    one code point to one code point, so the resulting span still indexes the
    original character boxes. OCR transcript matching remains exact.
    """
    return _literal_spans(text.translate(_SUPERSCRIPT_DIGITS), literal.translate(_SUPERSCRIPT_DIGITS))


def _box_for_range(textpage, indices, start, end, width, height) -> list[float] | None:
    boxes = []
    for index in set(indices[start:end]):
        left, bottom, right, top = textpage.get_charbox(index)
        if (
            all(math.isfinite(value) for value in (left, bottom, right, top))
            and left < right
            and bottom < top
        ):
            boxes.append((left, height - top, right, height - bottom))
    if not boxes:
        return None
    box = [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]
    return _pad_box(box, width, height, 20, 6)


def _pad_box(box, width, height, horizontal, vertical):
    return [
        round(max(0, box[0] - horizontal), 4),
        round(max(0, box[1] - vertical), 4),
        round(min(width, box[2] + horizontal), 4),
        round(min(height, box[3] + vertical), 4),
    ]


def _candidate_for(anchor, originals, candidates, occurrence):
    unmatched = [
        item for item in candidates if not any(item["value"] == native["value"] for native in originals)
    ]
    field, value = anchor["field"], anchor["value"]
    identity = []
    if field == "law_number_year":
        identity = [item for item in unmatched if item["value"][0] == value[0]]
    elif field == "date":
        identity = [item for item in unmatched if item["value"][:2] == value[:2]]
    elif field == "publication":
        identity = [item for item in unmatched if item["value"][1] == value[1]]
    if identity:
        if len({item["value"] for item in identity}) == 1 and occurrence < len(identity):
            return identity[occurrence]
        return None
    missing = [item for item in originals if not any(item["value"] == old["value"] for old in candidates)]
    if len(missing) == len(unmatched) == 1:
        return unmatched[0]
    scores = [
        (SequenceMatcher(None, " ".join(value), " ".join(item["value"])).ratio(), item) for item in unmatched
    ]
    if not scores:
        return None
    best = max(score for score, _ in scores)
    winners = [item for score, item in scores if score == best]
    if best >= 0.8 and len({item["value"] for item in winners}) == 1 and occurrence < len(winners):
        return winners[occurrence]
    return None


def _printed_search_regions(line, context, width, height):
    tasks = []
    group = hashlib.sha256(f"{line.start()}:{line.end()}".encode()).hexdigest()
    for index in range(4):
        box = [0, max(0, height * index / 4 - 12), width, min(height, height * (index + 1) / 4 + 12)]
        tasks.append(
            {
                "kind": "search_printed",
                "group": group,
                "target": {
                    "candidate_span": [line.start(), line.end()],
                    "candidate_text": line.group(),
                    "context": context,
                },
                "region_bbox": box,
                "verify_bbox": _pad_box(box, width, height, 0, 8),
            }
        )
    return tasks


def plan_regions(content: bytes | None, page: dict, archive_path=None, archive_size=None) -> list[dict]:
    """Run only in a bounded worker. A supplied fd stays pinned through parsing."""
    if content is None:
        from .archive_parsing import _open_original, _verify_hash, _verify_identity

        stream, info = _open_original(archive_path, archive_size)
        with stream:
            _verify_hash(stream, page["source_hash"], archive_size)
            result = _plan_regions_input(stream, page, ARCHIVE_MAX_PAGES)
            _verify_hash(stream, page["source_hash"], archive_size)
            _verify_identity(archive_path, info, archive_size)
            return result
    return _plan_regions_input(content, page, 300)


def _plan_regions_input(content, page: dict, page_limit: int) -> list[dict]:
    import pypdfium2 as pdfium
    from .native_anchor_evidence import filter_offpage_amount_anchors

    native, _ = filter_offpage_amount_anchors(page, number_anchors(page.get("native_text", "")))
    old = number_anchors(page.get("text", ""))
    tasks = []
    with pdfium.PdfDocument(content) as document:
        if not 1 <= page.get("page", 0) <= len(document) <= page_limit:
            raise ValueError("PDF 补救页码无效。")
        pdfpage = document[page["page"] - 1]
        width, height = pdfpage.get_size()
        textpage = pdfpage.get_textpage()
        try:
            if textpage.count_chars() > _MAX_LOCATOR_CHARS:
                raise ValueError("局部补救定位文字过多，需拆分处理。")
            pieces, indices = [], []
            for index in range(textpage.count_chars()):
                piece = textpage.get_text_range(index, 1)
                pieces.append(piece)
                indices.extend([index] * len(piece))
            located_text = "".join(pieces)
            located = number_anchors(located_text)
            for field in dict.fromkeys(anchor["field"] for anchor in native):
                originals = [anchor for anchor in native if anchor["field"] == field]
                candidates = [anchor for anchor in old if anchor["field"] == field]
                for ordinal, anchor in enumerate(originals):
                    if anchor_present(anchor, old):
                        continue
                    occurrence = sum(item["value"] == anchor["value"] for item in originals[:ordinal])
                    candidate = _candidate_for(anchor, originals, candidates, occurrence)
                    if candidate is None:
                        tasks.append({"kind": "unresolved", "reason": "ambiguous_numeric_alignment"})
                        continue
                    places = [
                        item
                        for item in located
                        if item["field"] == field and item["value"] == anchor["value"]
                    ]
                    if occurrence >= len(places):
                        tasks.append({"kind": "unresolved", "reason": "numeric_location_unavailable"})
                        continue
                    boxes = [
                        _box_for_range(textpage, indices, *place["span"], width, height) for place in places
                    ]
                    boxes = sorted((box for box in boxes if box), key=lambda box: (box[1], box[0]))
                    box = boxes[occurrence] if occurrence < len(boxes) else None
                    if box is None or (box[2] - box[0]) * (box[3] - box[1]) > width * height * 0.35:
                        tasks.append({"kind": "unresolved", "reason": "numeric_location_too_broad"})
                        continue
                    tasks.append(
                        {
                            "kind": "number",
                            "target": {
                                "field": field,
                                "native_value": list(anchor["value"]),
                                "candidate_span": candidate["span"],
                                "candidate_text": candidate["text"],
                            },
                            "region_bbox": box,
                            "verify_bbox": _pad_box(box, width, height, 8, 6),
                        }
                    )
            # The native character layer locates a model-reported literal; it
            # is never the evidence used to clear its independent uncertainty.
            # Both old OCR and the physical layer must have one exact location.
            old_text = page.get("text", "")
            for literal in dict.fromkeys((page.get("ocr") or {}).get("uncertain_spans", [])):
                if (
                    not isinstance(literal, str)
                    or not 2 <= len("".join(literal.split())) <= 128
                    or literal.strip().casefold() in _NON_LITERAL_UNCERTAINTIES
                    or literal.startswith("native_ocr_")
                    or "[illegible]" in literal.casefold()
                ):
                    continue
                candidates = [
                    span
                    for span in _literal_spans(old_text, literal)
                    if old_text[span[0] : span[1]] == literal
                ]
                if len(candidates) != 1:
                    if candidates:
                        tasks.append({"kind": "unresolved", "reason": "ambiguous_literal_alignment"})
                    continue
                matches = _locator_literal_spans(located_text, literal)
                if len(matches) != 1:
                    reason = "ambiguous_literal_location" if matches else "literal_location_unavailable"
                    tasks.append({"kind": "unresolved", "reason": reason})
                    continue
                box = _box_for_range(textpage, indices, *matches[0], width, height)
                if (
                    box is None
                    or (box[2] - box[0]) * (box[3] - box[1]) > width * height * 0.10
                    or box[3] - box[1] > height * 0.12
                ):
                    tasks.append({"kind": "unresolved", "reason": "literal_location_too_broad"})
                    continue
                tasks.append(
                    {
                        "kind": "literal",
                        "target": {
                            "uncertainty_exact": literal,
                            "candidate_span": list(candidates[0]),
                            "candidate_text": literal,
                            "native_locator_span": matches[0],
                            "source_hash": page["source_hash"],
                            "page": page["page"],
                        },
                        "region_bbox": box,
                        "verify_bbox": _pad_box(box, width, height, 8, 6),
                    }
                )
            # Locate uncertain printed lines from surrounding readable words.
            # Neither those words nor any candidate transcription go to the model.
            lines = list(re.finditer(r"[^\n]+", old_text))
            for ordinal, line in enumerate(lines):
                if "[illegible]" not in line.group().casefold():
                    continue
                neighbors = " ".join(item.group() for item in lines[max(0, ordinal - 1) : ordinal + 3])
                visible = re.sub(r"\[illegible\]", "", line.group(), flags=re.I)
                own_words = re.findall(r"[A-Za-z]{5,}", visible)
                following = " ".join(item.group() for item in lines[ordinal + 1 : ordinal + 2])
                focus = visible if own_words else following
                words = sorted(set(re.findall(r"[A-Za-z]{5,}", focus or neighbors)), key=len, reverse=True)
                words = [
                    word for word in words if word.casefold() not in {"illegible", "signature", "graphic"}
                ]
                locations = []
                for word in words:
                    matches = list(re.finditer(r"\b" + re.escape(word) + r"\b", located_text, re.I))
                    if len(matches) == 1:
                        locations.append(matches[0])
                        if len(locations) == 3:
                            break
                if not locations:
                    tasks.extend(_printed_search_regions(line, neighbors, width, height))
                    continue
                boxes = [
                    _box_for_range(textpage, indices, m.start(), m.end(), width, height) for m in locations
                ]
                boxes = [b for b in boxes if b]
                if not boxes:
                    tasks.extend(_printed_search_regions(line, neighbors, width, height))
                    continue
                # The readable suffix locates the whole printed line. A pure
                # graphic marker uses the following line, avoiding unrelated
                # stamps and reference numbers farther down the page.
                box = [
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ]
                box = _pad_box(box, width, height, min(60, width * 0.10), 8 if own_words else 35)
                tasks.append(
                    {
                        "kind": "printed",
                        "target": {
                            "candidate_span": [line.start(), line.end()],
                            "candidate_text": line.group(),
                            "context": neighbors,
                        },
                        "region_bbox": box,
                        "verify_bbox": _pad_box(box, width, height, 8, 6),
                    }
                )
        finally:
            textpage.close()
            pdfpage.close()
    if len(tasks) > MAX_TASKS:
        tasks = [{"kind": "unresolved", "reason": "region_budget_exceeded"}]
    return [{**task, "attempts": [0, 0], "reading_ids": [], "status": "pending"} for task in tasks]


def _initialize(content, page, archive_path=None, archive_size=None):
    from .parsing import plan_pdf_ocr_recovery_bounded

    updated = deepcopy(page)
    metadata = updated.setdefault("ocr", {})
    before = {key: deepcopy(value) for key, value in metadata.items() if key != "recovery_history"}
    history = metadata.setdefault("recovery_history", [])
    history.append(
        {
            "source_hash": page["source_hash"],
            "page": page["page"],
            "text": page["text"],
            "text_hash": hashlib.sha256(page["text"].encode()).hexdigest(),
            "ocr": before,
            "ocr_hash": _digest(before),
        }
    )
    tasks = plan_pdf_ocr_recovery_bounded(content, page, archive_path=archive_path, archive_size=archive_size)
    metadata["uncertain_spans"] = [
        span
        for span in metadata.get("uncertain_spans", [])
        if span not in {"region_provider_failed", "region_render_failed", "region_attempt_budget_exhausted"}
    ]
    metadata["recovery"] = {
        "version": RECOVERY_VERSION,
        "status": "pending",
        "tasks": tasks,
        "regions": [],
        "verifications": [],
        "corrections": [],
        "retry_after_seconds": 0,
        "baseline_text_hash": history[-1]["text_hash"],
    }
    previous = before.get("recovery") or {}
    if previous.get("baseline_text_hash") == history[-1]["text_hash"]:
        reused = 0
        for task in tasks:
            saved = next(
                (
                    old
                    for old in previous.get("tasks", [])
                    if old.get("status") == "complete"
                    and old.get("kind") == task.get("kind")
                    and old.get("target") == task.get("target")
                ),
                None,
            )
            if saved is None:
                continue
            regions = [
                region
                for region in previous.get("regions", [])
                if region.get("region_id") in saved.get("reading_ids", [])
            ]
            if not all(
                region.get("reading_hash") == _digest(region.get("reading"))
                and region.get("source_hash") == page["source_hash"]
                and region.get("page") == page["page"]
                and region.get("image_blob", {}).get("content_hash") == region.get("image_sha256")
                for region in regions
            ):
                continue
            correction, proof = _agreement(task, regions)
            if correction is None:
                continue
            task.update(
                status="complete",
                reading_ids=deepcopy(saved["reading_ids"]),
                attempts=deepcopy(saved.get("attempts", [0, 0])),
            )
            recovery = metadata["recovery"]
            existing = {region["region_id"] for region in recovery["regions"]}
            recovery["regions"].extend(
                deepcopy(region) for region in regions if region["region_id"] not in existing
            )
            recovery["corrections"].append(
                {
                    "span": task["target"]["candidate_span"],
                    "before": task["target"]["candidate_text"],
                    "after": correction,
                }
            )
            if proof:
                recovery["verifications"].append(proof)
            reused += 1
        metadata["recovery"]["reused_tasks"] = reused
    metadata["phase"] = "recovery"
    updated["status"] = "pending_ocr"
    return updated


def _agreement(task, regions) -> tuple[str | None, dict | None]:
    if len(regions) != 2 or len({region.get("image_sha256") for region in regions}) != 2:
        return None, None
    reads = [region["reading"] for region in regions]
    if any(read.get("uncertain_spans") or "[illegible]" in read.get("text", "").casefold() for read in reads):
        return None, None
    if task["kind"] == "number":
        anchors = [
            [anchor for anchor in number_anchors(read["text"]) if anchor["field"] == task["target"]["field"]]
            for read in reads
        ]
        if any(len(values) != 1 for values in anchors) or anchors[0][0]["value"] != anchors[1][0]["value"]:
            return None, None
        value = anchors[0][0]["value"]
        return anchors[0][0]["text"], {
            "field": task["target"]["field"],
            "native_value": task["target"]["native_value"],
            "verified_value": list(value),
            "region_ids": [region["region_id"] for region in regions],
        }
    if task["kind"] == "literal":
        try:
            intact = all(
                region.get("reading_hash") == _digest(read)
                and region.get("source_hash") == task["target"]["source_hash"]
                and region.get("page") == task["target"]["page"]
                and (region.get("image_blob") or {}).get("content_hash") == region.get("image_sha256")
                for region, read in zip(regions, reads, strict=True)
            )
        except (TypeError, ValueError):
            intact = False
        if (
            not intact
            or len({region.get("region_id") for region in regions}) != 2
            or regions[0].get("region_bbox") == regions[1].get("region_bbox")
        ):
            return None, None
        candidates, read_spans = [], []
        for read in reads:
            spans = _literal_spans(read.get("text", ""), task["target"]["candidate_text"])
            if len(spans) != 1:
                return None, None
            start, end = spans[0]
            candidates.append(read["text"][start:end])
            read_spans.append(spans[0])
        if "".join(candidates[0].split()) != "".join(candidates[1].split()):
            return None, None
        return candidates[0], {
            "field": "literal",
            "uncertainty_exact": task["target"]["uncertainty_exact"],
            "candidate_span": task["target"]["candidate_span"],
            "verified_text": candidates[0],
            "reading_spans": read_spans,
            "region_ids": [region["region_id"] for region in regions],
        }
    # Printed names are complete lines, not inferred from a known signer.
    old_line = task["target"]["candidate_text"]
    suffix = re.sub(r"\[illegible\]", "", old_line, flags=re.I).strip()
    candidates = []
    for read in reads:
        lines = [line.strip() for line in read["text"].splitlines() if line.strip()]
        if suffix:
            words = re.findall(r"[A-Za-z]{4,}", suffix)
            matches = [
                line
                for line in lines
                if any(re.search(r"\b" + re.escape(word) + r"\b", line, re.I) for word in words)
            ]
            if len(matches) != 1:
                return None, None
            cursor = 0
            normalized = _normal_text(matches[0])
            for part in re.split(r"\[illegible\]", old_line, flags=re.I):
                visible = _normal_text(part)
                if visible:
                    found = normalized.find(visible, cursor)
                    if found < 0:
                        return None, None
                    cursor = found + len(visible)
            candidates.append(matches[0])
        elif any(
            re.search(r"signature|tanda\s+tangan|签字", mark, re.I) for mark in read.get("graphic_marks", [])
        ):
            if task["kind"] in {"printed", "search_printed"}:
                context_words = {
                    word.casefold()
                    for word in re.findall(r"[A-Za-z]{5,}", task["target"].get("context", ""))
                    if word.casefold() != "illegible"
                }
                seen = {word.casefold() for word in re.findall(r"[A-Za-z]{5,}", read["text"])}
                if len(context_words & seen) < 2:
                    return None, None
            candidates.append("[signature graphic]")
        else:
            return None, None
    if _normal_text(candidates[0]) != _normal_text(candidates[1]):
        return None, None
    return candidates[0], None


def _finish(page):
    metadata = page["ocr"]
    recovery = metadata["recovery"]
    direct = [task for task in recovery["tasks"] if task["kind"] != "search_printed"]
    groups = {task["group"] for task in recovery["tasks"] if task["kind"] == "search_printed"}
    if any(task["status"] != "complete" for task in direct) or any(
        not any(task.get("group") == group and task["status"] == "complete" for task in recovery["tasks"])
        for group in groups
    ):
        recovery["status"] = "conflict"
        page["status"] = "ocr_uncertain"
        return page
    # A previous task's checkpoint can be older than the last blind request.
    # Revalidate all literal proofs before clearing any literal uncertainty.
    for task in direct:
        if task["kind"] != "literal":
            continue
        regions = [region for region in recovery["regions"] if region.get("region_id") in task["reading_ids"]]
        replacement, proof = _agreement(task, regions)
        corrections = [
            item
            for item in recovery["corrections"]
            if item["span"] == task["target"]["candidate_span"]
            and item["before"] == task["target"]["candidate_text"]
        ]
        if (
            replacement is None
            or proof not in recovery["verifications"]
            or len(corrections) != 1
            or corrections[0]["after"] != replacement
        ):
            task["status"] = "conflict"
            recovery["status"] = "conflict"
            page["status"] = "ocr_uncertain"
            return page
    history = metadata["recovery_history"][-1]
    if hashlib.sha256(page["text"].encode()).hexdigest() != history["text_hash"]:
        recovery["status"] = "conflict"
        page["status"] = "ocr_uncertain"
        metadata.setdefault("uncertain_spans", []).append("recovery_baseline_changed")
        return page
    text = page["text"]
    corrections = sorted(recovery["corrections"], key=lambda item: item["span"][0], reverse=True)
    previous_start = len(text) + 1
    for correction in corrections:
        start, end = correction["span"]
        if end > previous_start or text[start:end] != correction["before"]:
            recovery["status"] = "conflict"
            page["status"] = "ocr_uncertain"
            metadata.setdefault("uncertain_spans", []).append("recovery_span_conflict")
            return page
        text = text[:start] + correction["after"] + text[end:]
        previous_start = start
    page["text"] = text
    recovery["status"] = "complete"
    recovery["final_text_hash"] = hashlib.sha256(text.encode()).hexdigest()
    # Only uncertainties proved by an exact region are removed. Structural,
    # blank-page, truncation, or omission warnings continue to block indexing.
    proved_printed = [
        task["target"]["candidate_text"]
        for task in recovery["tasks"]
        if task["kind"] in {"printed", "search_printed"} and task["status"] == "complete"
    ]
    proved_literals = {
        task["target"]["uncertainty_exact"]
        for task in recovery["tasks"]
        if task["kind"] == "literal" and task["status"] == "complete"
    }
    metadata["uncertain_spans"] = [
        span
        for span in metadata.get("uncertain_spans", [])
        if span not in proved_literals and not ("[illegible]" in span.casefold() and proved_printed)
    ]
    independent_warnings = [
        warning
        for warning in metadata.get("validation_warnings", [])
        if not (warning.startswith("native_ocr_") and warning.endswith("_mismatch"))
    ]
    if metadata.get("complete") is False and "model_reported_incomplete" not in metadata["uncertain_spans"]:
        metadata["uncertain_spans"].append("model_reported_incomplete")
    metadata["uncertain_spans"].extend(
        warning for warning in independent_warnings if warning not in metadata["uncertain_spans"]
    )
    page["status"] = "ocr_complete"
    validate_ocr_page(page)
    metadata["validation_warnings"].extend(
        warning for warning in independent_warnings if warning not in metadata["validation_warnings"]
    )
    if page["status"] != "ocr_complete":
        recovery["status"] = "conflict"
    page.pop("deferred_reason", None)
    return page


def recover_page_step(
    content: bytes | None,
    page: dict,
    *,
    on_region: Callable[[dict], dict],
    on_page: Callable[[dict], None] | None = None,
    on_response_error: Callable[[dict], dict] | None = None,
    archive_path=None,
    archive_size: int | None = None,
) -> dict:
    """Recover page evidence; model request capacity is enforced at the provider boundary."""

    current = (page.get("ocr") or {}).get("recovery")
    if (
        current
        and current.get("version") == RECOVERY_VERSION
        and current.get("status") in {"complete", "conflict", "failed"}
    ):
        return _recover_page_step(
            content,
            page,
            on_region=on_region,
            on_page=on_page,
            on_response_error=on_response_error,
            archive_path=archive_path,
            archive_size=archive_size,
        )  # Preserve cached terminal text while retaining source validation.
    return _recover_page_step(
        content,
        page,
        on_region=on_region,
        on_page=on_page,
        on_response_error=on_response_error,
        archive_path=archive_path,
        archive_size=archive_size,
    )


def _recover_page_step(
    content: bytes | None,
    page: dict,
    *,
    on_region: Callable[[dict], dict],
    on_page: Callable[[dict], None] | None = None,
    on_response_error: Callable[[dict], dict] | None = None,
    archive_path=None,
    archive_size: int | None = None,
) -> dict:
    """At most one blind region request per step; all attempts are checkpointable."""
    from .parsing import ProcessingDeferred, render_pdf_region_bounded
    from .vision import OCRCapacityDeferred, OCRResponseError, REGION_PROMPT_VERSION, transcribe_region

    if content is None:
        from .parsing import _archive_reference

        _archive_reference(archive_path, page.get("source_hash"), archive_size)
    elif archive_path is not None or page.get("source_hash") != hashlib.sha256(content).hexdigest():
        raise ValueError("局部补救原件 hash 不匹配。")
    current = (page.get("ocr") or {}).get("recovery")
    if (
        current
        and current.get("version") == RECOVERY_VERSION
        and current.get("status") in {"complete", "conflict", "failed"}
    ):
        return deepcopy(page)
    updated = (
        deepcopy(page)
        if current and current.get("version") == RECOVERY_VERSION
        else _initialize(content, page, archive_path, archive_size)
    )
    metadata = updated["ocr"]
    recovery = metadata["recovery"]
    recovery["retry_after_seconds"] = 0
    task = next((task for task in recovery["tasks"] if task["status"] == "pending"), None)
    if task is None:
        return _finish(updated)
    if task["kind"] == "unresolved":
        task["status"] = "conflict"
        task["error"] = task["reason"]
        return _finish(updated)
    retry_at = task.get("retry_not_before", 0)
    if type(retry_at) in {int, float} and math.isfinite(retry_at) and retry_at > time.time():
        updated.update(status="pending_ocr", deferred_reason="provider_retry")
        recovery.update(status="deferred", retry_after_seconds=retry_at - time.time())
        return updated
    pass_number = len(task["reading_ids"])
    box = task["region_bbox"] if pass_number == 0 else task["verify_bbox"]
    try:
        if content is None:
            from .archive_parsing import render_archive_page_bounded

            rendered = render_archive_page_bounded(
                archive_path,
                source_hash=page["source_hash"],
                size=archive_size,
                page_number=page["page"],
                region_bbox=box,
                dpi=400,
            )
        else:
            rendered = render_pdf_region_bounded(content, page["page"], box)
    except ProcessingDeferred:
        updated.update(status="pending_ocr", deferred_reason="render_capacity")
        recovery.update(status="deferred", retry_after_seconds=5)
        return updated
    except ValueError as exc:
        task.update(status="failed", error=str(exc)[:500])
        recovery["status"] = "failed"
        updated["status"] = "ocr_uncertain"
        metadata.setdefault("uncertain_spans", []).append("region_render_failed")
        return updated
    existing = next(
        (region for region in recovery["regions"] if region["region_id"] == rendered["region_id"]), None
    )
    if existing and existing.get("reading"):
        if (
            existing.get("reading_hash") != _digest(existing["reading"])
            or existing.get("source_hash") != rendered["source_hash"]
            or existing.get("page") != rendered["page"]
            or existing.get("image_sha256") != rendered["image_sha256"]
            or existing.get("image_blob", {}).get("content_hash") != rendered["image_sha256"]
        ):
            raise ValueError("已保存的局部识别证据 hash 不匹配。")
        record = existing
    else:
        if task["attempts"][pass_number] >= 3:
            task["status"] = "failed"
            recovery["status"] = "failed"
            updated["status"] = "ocr_uncertain"
            metadata.setdefault("uncertain_spans", []).append("region_attempt_budget_exhausted")
            return updated
        reference = on_region(deepcopy(rendered))
        if not isinstance(reference, dict):
            raise ValueError("局部图像回调必须返回持久引用。")
        record = {key: value for key, value in rendered.items() if key != "image_base64"}
        record.update(image_blob=reference, pass_number=pass_number)
        if existing is None:
            recovery["regions"].append(record)
        else:
            record = existing
            from .ocr_error_evidence import preserve_previous_region_attempt

            preserve_previous_region_attempt(record)
            if record.get("request_id") and not record.get("error") and not record.get("reading"):
                record["request_outcome"] = "unknown_after_interruption"
                task["status"] = recovery["status"] = "failed"
                updated["status"] = "ocr_uncertain"
                metadata.setdefault("uncertain_spans", []).append("region_request_outcome_unknown")
                return updated
            record.update({key: value for key, value in rendered.items() if key != "image_base64"})
            record.update(image_blob=reference, pass_number=pass_number)
        task["attempts"][pass_number] += 1
        record.update(
            attempts=task["attempts"][pass_number],
            request_id=uuid4().hex,
            prompt_version=REGION_PROMPT_VERSION,
            requested_model=os.getenv("LLM_MODEL", "").strip()[:200],
        )
        for key in (
            "error",
            "error_response",
            "finish_reason",
            "http_status",
            "retry_after_seconds",
            "retry_not_before",
        ):
            record.pop(key, None)
        updated.update(status="pending_ocr")
        updated.pop("deferred_reason", None)
        recovery["status"] = "pending"
        if on_page is not None:
            on_page(deepcopy(updated))
        try:
            reading = transcribe_region(
                {
                    key: value
                    for key, value in rendered.items()
                    if key not in {"native_text", "candidate", "target"}
                }
            )
        except OCRResponseError as exc:
            if isinstance(exc, OCRCapacityDeferred):
                task["attempts"][pass_number] -= 1
                record["attempts"] = task["attempts"][pass_number]
                record.pop("request_id", None)
                recovery.update(status="deferred", retry_after_seconds=exc.retry_after)
                updated.update(status="pending_ocr", deferred_reason="ocr_capacity")
                return updated
            record["error"] = str(exc)[:500]
            record["finish_reason"] = exc.finish_reason
            if exc.request_outcome_unknown:
                record["request_outcome"] = "unknown_transport_result"
            if exc.http_status is not None:
                record["http_status"] = exc.http_status
            if exc.response_evidence is not None and on_response_error is not None:
                evidence = {
                    **deepcopy(exc.response_evidence),
                    **{
                        key: record[key]
                        for key in (
                            "source_hash",
                            "page",
                            "region_id",
                            "region_bbox",
                            "pass_number",
                            "request_id",
                            "image_sha256",
                            "prompt_version",
                            "requested_model",
                        )
                    },
                    "request_kind": "region",
                    "attempt": record["attempts"],
                    "finish_reason": exc.finish_reason,
                }
                if exc.request_outcome_unknown:
                    evidence["request_outcome"] = "unknown_transport_result"
                reference = on_response_error(deepcopy(evidence))
                if not isinstance(reference, dict):
                    raise ValueError("局部识别错误响应存储回调必须返回持久引用。")
                record["error_response"] = {
                    **{key: value for key, value in evidence.items() if key != "response_base64"},
                    "blob": reference,
                }
            if exc.retryable and task["attempts"][pass_number] < 3:
                delay = max(5 * (2 ** (task["attempts"][pass_number] - 1)), exc.retry_after or 0)
                task["retry_not_before"] = time.time() + delay
                record.update(retry_after_seconds=delay, retry_not_before=task["retry_not_before"])
                recovery.update(status="deferred", retry_after_seconds=delay)
                updated["deferred_reason"] = "provider_retry"
                return updated
            task["status"] = "failed"
            recovery["status"] = "failed"
            updated["status"] = "ocr_uncertain"
            metadata.setdefault("uncertain_spans", []).append("region_provider_failed")
            return updated
        record.update(reading=reading, reading_hash=_digest(reading))
        task.pop("retry_not_before", None)
        record.pop("error", None)
    task["reading_ids"].append(record["region_id"])
    if len(task["reading_ids"]) == 2:
        evidence = [
            next(region for region in recovery["regions"] if region["region_id"] == region_id)
            for region_id in task["reading_ids"]
        ]
        replacement, proof = _agreement(task, evidence)
        if replacement is None:
            task["status"] = "miss" if task["kind"] == "search_printed" else "conflict"
        else:
            task["status"] = "complete"
            if task["kind"] == "search_printed":
                for sibling in recovery["tasks"]:
                    if sibling is not task and sibling.get("group") == task["group"]:
                        sibling["status"] = "skipped"
            recovery["corrections"].append(
                {
                    "span": task["target"]["candidate_span"],
                    "before": task["target"]["candidate_text"],
                    "after": replacement,
                    "region_ids": task["reading_ids"],
                }
            )
            if proof:
                recovery["verifications"].append(proof)
    if all(task["status"] != "pending" for task in recovery["tasks"]):
        return _finish(updated)
    return updated
