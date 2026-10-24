"""Auditable visual fallback for MinerU pages with ambiguous table layout."""

from __future__ import annotations

import re
from copy import deepcopy

PROTOCOL = "mineru-table-visual-page-v1"
MAX_RETRY_CYCLES = 3


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
    return isinstance(marker, dict) and marker.get("protocol") == PROTOCOL


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
    if type(attempts) is not int or not 1 <= attempts <= MAX_RETRY_CYCLES:
        raise ValueError("MinerU 视觉回退次数检查点无效。")
    return attempts


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

    elements = []
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
        if not markdown_table_is_consistent(page.get("text", "")):
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
        if type(number) is not int or number < 1 or not page_elements:
            raise ValueError("MinerU 页面回退无法绑定到原有版面元素。")
        first_index, first_element = page_elements[0]
        first_locator = first_element["locator"]
        source_hash = result.get("source_hash")
        if (
            page.get("source_hash") != source_hash
            or first_locator.get("source_hash") != source_hash
        ):
            raise ValueError("MinerU 页面回退原件 hash 不匹配。")
        marker = page.get("ocr_fallback")
        marker["accepted"] = True
        marker["text_sha256"] = metadata.get("text_sha256")
        marker["prompt_version"] = metadata.get("prompt_version")
        page["ocr_fallback"] = marker
        page["reasons"] = [reason for reason in page.get("reasons", []) if reason != "table_structure_ambiguous"]

        locator = {
            "kind": "pdf",
            "page": number,
            "physical_page": number,
            "bbox": [0, 0, 1000, 1000],
            "coordinate_system": "mineru-0-1000",
            "source_hash": source_hash,
        }
        for key in ("provider_task_id", "provider_page_idx"):
            if key in first_locator:
                locator[key] = first_locator[key]
        elements.append(
            {
                "replace_at": first_index,
                "page": number,
                "value": {
                    "element_key": f"mineru-visual-page-{number:06d}",
                    "kind": "table",
                    "order": min(item.get("order", index) for index, item in page_elements),
                    "content_text": page["text"],
                    "locator": locator,
                    "parent_key": first_element.get("parent_key"),
                    "section_path": deepcopy(first_element.get("section_path", [])),
                    "metadata_json": {
                        "provider": "vision",
                        "source_provider": "mineru",
                        "fallback_protocol": PROTOCOL,
                        "source_hash": source_hash,
                        "page": number,
                        "prompt_version": metadata.get("prompt_version"),
                        "request_id": metadata.get("request_id"),
                        "image_sha256": metadata.get("image_sha256"),
                    },
                },
            }
        )

    replacements = {entry["page"]: entry for entry in elements}
    replaced_pages = set()
    converted = []
    for index, item in enumerate(raw_elements):
        locator = item.get("locator") if isinstance(item, dict) else None
        number = (
            locator.get("physical_page", locator.get("page"))
            if isinstance(locator, dict)
            else None
        )
        if number in replacements:
            replacement = replacements[number]
            if index == replacement["replace_at"]:
                converted.append(replacement["value"])
                replaced_pages.add(number)
            continue
        converted.append(item)
    if replaced_pages != set(replacements):
        raise ValueError("MinerU 页面回退重建未覆盖全部已接受页面。")

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
