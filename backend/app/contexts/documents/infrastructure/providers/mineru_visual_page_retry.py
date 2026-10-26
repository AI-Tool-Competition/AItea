"""Auditable visual fallback for MinerU pages with ambiguous table layout."""

from __future__ import annotations

import re
from copy import deepcopy

PROTOCOL = "mineru-table-visual-page-v1"
EMPTY_PAGE_PROTOCOL = "mineru-empty-page-visual-v1"
_VISUAL_PROTOCOLS = frozenset({PROTOCOL, EMPTY_PAGE_PROTOCOL})


def is_mineru_table_review(page: dict) -> bool:
    metadata = page.get("ocr") or {}
    spans = metadata.get("uncertain_spans")
    return (
        page.get("status") == "ocr_uncertain"
        and metadata.get("provider") == "mineru"
        and isinstance(spans, list)
        and "table_structure_ambiguous" in spans
    )


def is_visual_fallback_page(page: dict) -> bool:
    marker = page.get("ocr_fallback")
    return isinstance(marker, dict) and marker.get("protocol") in _VISUAL_PROTOCOLS


def is_table_visual_fallback_page(page: dict) -> bool:
    marker = page.get("ocr_fallback")
    return isinstance(marker, dict) and marker.get("protocol") == PROTOCOL


def is_empty_page_visual_fallback_page(page: dict) -> bool:
    marker = page.get("ocr_fallback")
    return isinstance(marker, dict) and marker.get("protocol") == EMPTY_PAGE_PROTOCOL


def is_accepted_empty_page_fallback(page: dict) -> bool:
    proof = page.get("ocr") or {}
    return (
        is_empty_page_visual_fallback_page(page)
        and (page.get("ocr_fallback") or {}).get("accepted") is True
        and page.get("status") == "ocr_complete"
        and proof.get("provider") == "vision"
        and proof.get("is_blank") is True
        and not str(page.get("text", "")).strip()
    )


def is_mineru_empty_page_review(page: dict) -> bool:
    """Require a visual check when MinerU accepted a page without searchable text."""
    proof = page.get("ocr") or {}
    spans = proof.get("uncertain_spans", [])
    reasons = page.get("reasons", [])
    return (
        page.get("status") in {"ocr_complete", "ocr_uncertain"}
        and proof.get("provider") == "mineru"
        and not str(page.get("text", "")).strip()
        and isinstance(spans, list)
        and all(item == "empty_text_unverified" for item in spans)
        and isinstance(reasons, list)
        and all(item == "empty_text_unverified" for item in reasons)
        and not is_accepted_empty_page_fallback(page)
    )


def has_pending_visual_fallback(manifest: dict) -> bool:
    remote = manifest.get("remote_parse") or {}
    return (
        manifest.get("parser") == "mineru"
        and remote.get("state") == "done"
        and remote.get("source_hash") == manifest.get("source_hash")
        and any(
            is_visual_fallback_page(page) and page.get("status") == "pending_ocr"
            for page in manifest.get("pages", [])
        )
    )


def fallback_attempts(page: dict) -> int:
    marker = page.get("ocr_fallback")
    if not is_visual_fallback_page(page):
        return 0
    attempts = marker.get("attempts")
    if type(attempts) is not int or attempts < 1:
        raise ValueError("MinerU 视觉回退次数检查点无效。")
    return attempts


def missing_visual_provider_pages(manifest: dict) -> tuple[int, ...]:
    """Find accepted vision pages whose receipt is otherwise valid but lacks its provider tag.

    Older OCR checkpoints omitted ``provider='vision'`` from the vision receipt.
    Retry only when adding that one missing field makes the full MinerU manifest
    pass its source, task, page, image, and transcript validation.
    """
    if (
        not isinstance(manifest, dict)
        or manifest.get("parser") != "mineru"
        or (manifest.get("remote_parse") or {}).get("state") != "done"
    ):
        return ()
    pages = manifest.get("pages")
    if not isinstance(pages, list):
        return ()

    candidates = []
    for index, page in enumerate(pages):
        if not isinstance(page, dict) or page.get("status") != "ocr_complete":
            continue
        marker = page.get("ocr_fallback")
        proof = page.get("ocr")
        if (
            not is_visual_fallback_page(page)
            or not isinstance(marker, dict)
            or marker.get("accepted") is not True
            or not isinstance(proof, dict)
            or proof.get("provider") is not None
        ):
            continue
        try:
            fallback_attempts(page)
        except ValueError:
            continue
        candidates.append((index, page))

    if not candidates:
        return ()

    probe = dict(manifest)
    probe_pages = list(pages)
    for index, page in candidates:
        repaired_page = dict(page)
        repaired_proof = dict(page["ocr"])
        repaired_proof["provider"] = "vision"
        repaired_page["ocr"] = repaired_proof
        probe_pages[index] = repaired_page
    probe["pages"] = probe_pages
    try:
        from app.contexts.documents.infrastructure.providers.mineru import manifest_valid

        if not manifest_valid(probe):
            return ()
    except (KeyError, TypeError, ValueError):
        return ()
    return tuple(page["page"] for _, page in candidates if type(page.get("page")) is int)


def markdown_table_is_consistent(text: str) -> bool:
    """Require every pipe-delimited table block to keep one consistent shape."""
    lines = text.splitlines()

    def cells(line: str) -> list[str] | None:
        value = line.strip()
        if not (value.startswith("|") and value.endswith("|")):
            return None
        return [cell.strip() for cell in re.split(r"(?<!\\)\|", value[1:-1])]

    blocks: list[list[list[str]]] = []
    block: list[list[str]] = []
    for line in lines:
        row = cells(line)
        if row is None:
            if block:
                blocks.append(block)
                block = []
            continue
        block.append(row)
    if block:
        blocks.append(block)
    if not blocks:
        return False

    for table in blocks:
        if len(table) < 3:
            return False
        header, separator, *rows = table
        if (
            not header
            or len(header) != len(separator)
            or not all(re.fullmatch(r":?-{2,}:?", cell) for cell in separator)
            or any(len(row) != len(header) for row in rows)
        ):
            return False
    return True


def rebuild_manifest(manifest: dict) -> dict:
    """Rebuild searchable elements from accepted page transcripts, retaining MinerU evidence."""
    from app.contexts.documents.infrastructure.parsers.ingestion_quality import pdf_page_status
    from app.contexts.documents.infrastructure.parsers.native_parser import _parsed_result
    from app.shared.infrastructure.vendor.geo_parser.document_ingestion import ParsedDocument, ParsedElement

    result = deepcopy(manifest)
    pages = result.get("pages")
    raw_elements = result.get("elements")
    if not isinstance(pages, list) or not isinstance(raw_elements, list):
        raise ValueError("MinerU 页面回退缺少完整正文结构。")

    replacements = {}
    for page in pages:
        if not is_visual_fallback_page(page) or page.get("status") != "ocr_complete":
            continue
        metadata = page.get("ocr") or {}
        if (
            metadata.get("complete") is not True
            or metadata.get("uncertain_spans")
            or pdf_page_status(page) != "ocr_complete"
        ):
            continue
        empty_fallback = is_empty_page_visual_fallback_page(page)
        page_text = page.get("text", "")
        if empty_fallback and not page_text.strip() and metadata.get("is_blank") is not True:
            spans = list(metadata.get("uncertain_spans") or [])
            if "empty_page_not_confirmed" not in spans:
                spans.append("empty_page_not_confirmed")
            metadata["uncertain_spans"] = spans
            page["ocr"] = metadata
            page["status"] = "ocr_uncertain"
            continue
        if empty_fallback and page_text.strip() and metadata.get("is_blank") is not False:
            spans = list(metadata.get("uncertain_spans") or [])
            if "blank_response_conflicts_with_transcription" not in spans:
                spans.append("blank_response_conflicts_with_transcription")
            metadata["uncertain_spans"] = spans
            page["ocr"] = metadata
            page["status"] = "ocr_uncertain"
            continue
        if not empty_fallback and not markdown_table_is_consistent(page_text):
            spans = list(metadata.get("uncertain_spans") or [])
            if "table_layout_not_preserved" not in spans:
                spans.append("table_layout_not_preserved")
            metadata["uncertain_spans"] = spans
            page["ocr"] = metadata
            page["status"] = "ocr_uncertain"
            continue

        number = page.get("page")
        page_elements = [
            (index, item)
            for index, item in enumerate(raw_elements)
            if isinstance(item, dict)
            and isinstance(item.get("locator"), dict)
            and item["locator"].get("physical_page", item["locator"].get("page")) == number
        ]
        if type(number) is not int or number < 1 or (not page_elements and not empty_fallback):
            raise ValueError("MinerU 页面回退无法绑定到原有版面元素。")
        first_index = page_elements[0][0] if page_elements else next(
            (
                index
                for index, item in enumerate(raw_elements)
                if isinstance(item, dict)
                and isinstance(item.get("locator"), dict)
                and type(item["locator"].get("physical_page", item["locator"].get("page"))) is int
                and item["locator"].get("physical_page", item["locator"].get("page")) > number
            ),
            len(raw_elements),
        )
        first_element = page_elements[0][1] if page_elements else None
        first_locator = first_element["locator"] if first_element else {}
        source_hash = result.get("source_hash")
        if (
            page.get("source_hash") != source_hash
            or (first_element and first_locator.get("source_hash") != source_hash)
        ):
            raise ValueError("MinerU 页面回退原件 hash 不匹配。")
        marker = page.get("ocr_fallback")
        marker["accepted"] = True
        marker["text_sha256"] = metadata.get("text_sha256")
        marker["prompt_version"] = metadata.get("prompt_version")
        page["ocr_fallback"] = marker
        removed_reason = "empty_text_unverified" if empty_fallback else "table_structure_ambiguous"
        page["reasons"] = [reason for reason in page.get("reasons", []) if reason != removed_reason]

        if empty_fallback and not page_text.strip():
            replacement_value = None
        else:
            locator = {
                "kind": "pdf",
                "page": number,
                "physical_page": number,
                "bbox": [0, 0, 1000, 1000],
                "coordinate_system": "mineru-0-1000",
                "source_hash": source_hash,
            }
            if first_element:
                for key in ("provider_task_id", "provider_page_idx"):
                    if key in first_locator:
                        locator[key] = first_locator[key]
            else:
                part = next(
                    (
                        part
                        for part in (result.get("remote_parse") or {}).get("parts", [])
                        if part.get("first_page", 0) <= number <= part.get("last_page", 0)
                    ),
                    None,
                )
                if part:
                    locator["provider_task_id"] = part["task_id"]
                    locator["provider_page_idx"] = number - part["first_page"]
            section_path = (
                deepcopy(first_element.get("section_path", []))
                if first_element
                else next(
                    (
                        deepcopy(item.get("section_path", []))
                        for item in reversed(raw_elements[:first_index])
                        if isinstance(item, dict)
                    ),
                    [],
                )
            )
            replacement_value = {
                "element_key": f"mineru-visual-page-{number:06d}",
                "kind": "table" if markdown_table_is_consistent(page_text) else "paragraph",
                "content_text": page_text,
                "locator": locator,
                "section_path": section_path,
                "parent_key": first_element.get("parent_key") if first_element else None,
                "metadata_json": {
                    "provider": "vision",
                    "source_provider": "mineru",
                    "fallback_protocol": marker["protocol"],
                    "source_hash": source_hash,
                    "page": number,
                    "prompt_version": metadata.get("prompt_version"),
                    "request_id": metadata.get("request_id"),
                    "image_sha256": metadata.get("image_sha256"),
                },
            }

        replacements[number] = {
            "replace_at": first_index,
            "value": replacement_value,
        }
    replaced_pages = set()
    converted = []
    by_index = {}
    for number, replacement in replacements.items():
        by_index.setdefault(replacement["replace_at"], []).append((number, replacement))
    for index in range(len(raw_elements) + 1):
        for number, replacement in sorted(by_index.get(index, [])):
            if replacement["value"] is not None:
                converted.append(replacement["value"])
            replaced_pages.add(number)
        if index == len(raw_elements):
            break
        item = raw_elements[index]
        locator = item.get("locator") if isinstance(item, dict) else None
        number = (
            locator.get("physical_page", locator.get("page"))
            if isinstance(locator, dict)
            else None
        )
        if number in replacements:
            continue
        converted.append(item)
    if replaced_pages != set(replacements):
        raise ValueError("MinerU 页面回退重建未覆盖全部已接受页面。")
    for order, item in enumerate(converted):
        item["order"] = order

    try:
        parsed_elements = [
            ParsedElement(
                element_key=item["element_key"],
                kind=item["kind"],
                order=item["order"],
                content_text=item["content_text"],
                locator=item["locator"],
                section_path=item.get("section_path", []),
                parent_key=item.get("parent_key"),
                metadata_json=item.get("metadata_json", {}),
            )
            for item in converted
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("MinerU 元素映射无法重建检索片段。") from exc

    pipeline_version = result["pipeline_version"]
    chunker_suffix = "+layout-token-hierarchical-v2"
    if pipeline_version.endswith(chunker_suffix):
        pipeline_version = pipeline_version[: -len(chunker_suffix)]
    document = ParsedDocument(
        parser_name=result["parser"],
        parser_version=result["parser_version"],
        pipeline_version=pipeline_version,
        elements=parsed_elements,
        assets=[],
        warnings=result.get("warnings", []),
        pages=pages,
    )
    rebuilt = _parsed_result(document, result["source_hash"], result.get("title", ""))
    return {**result, **rebuilt}
