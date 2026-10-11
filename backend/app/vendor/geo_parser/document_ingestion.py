from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import os
import re
import zipfile
from copy import deepcopy
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling_core.types.doc.document import DoclingDocument
from xml.etree import ElementTree

from .bounded_concurrency import bounded_ordered_map


logger = logging.getLogger(__name__)

_NATIVE_TEXT_EXTENSIONS = frozenset({".txt", ".jsonl", ".md", ".markdown"})
_NATIVE_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
_MAX_DOCUMENT_PAGES = 500
_MAX_DOCUMENT_FILE_BYTES = 50 * 1024 * 1024
_MAX_IMAGE_PIXELS = 50_000_000
_MIN_EMBEDDED_VISION_IMAGE_DIMENSION = 14
_PARENT_TARGET_TOKENS = 600
_PARENT_MAX_TOKENS = 900
_PARENT_MIN_TOKENS = 80
_FORCED_SPLIT_OVERLAP_TOKENS = 64
_CHILD_TARGET_TOKENS = 180
_CHILD_MAX_TOKENS = 300
_CHILD_MAX_UNITS = 8
_CHUNKER_VERSION = "layout-token-hierarchical-v2"
_MAX_MANIFEST_ELEMENTS = 20_000
_MAX_MANIFEST_CHUNKS = 10_000
_MAX_MANIFEST_ASSETS = 500
_MAX_MANIFEST_WARNINGS = 1_000
_DOCLING_MODEL_FILES = (
    "docling-project--docling-layout-heron/config.json",
    "docling-project--docling-layout-heron/preprocessor_config.json",
    "docling-project--docling-layout-heron/model.safetensors",
    "docling-project--docling-models/model_artifacts/tableformer/accurate/tm_config.json",
    "docling-project--docling-models/model_artifacts/tableformer/accurate/tableformer_accurate.safetensors",
)


ElementKind = Literal[
    "title",
    "heading",
    "paragraph",
    "list",
    "table",
    "figure",
    "caption",
    "formula",
    "code",
    "page",
]


@dataclass(frozen=True, slots=True)
class ParsedElement:
    element_key: str
    kind: ElementKind
    order: int
    content_text: str
    locator: dict[str, Any]
    section_path: list[str]
    parent_key: str | None = None
    metadata_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParsedAsset:
    asset_key: str
    kind: Literal["image", "figure", "page_render", "table_render"]
    mime_type: str
    content: bytes
    locator: dict[str, Any]
    width: int | None = None
    height: int | None = None
    caption: str | None = None
    ocr_text: str | None = None
    related_element_keys: list[str] = field(default_factory=list)
    vision_summary: str | None = None
    vision_details: dict[str, Any] | None = None
    vision_model: str | None = None
    vision_prompt_version: str | None = None
    vision_status: Literal["succeeded"] | None = None


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    parser_name: str
    parser_version: str
    pipeline_version: str
    elements: list[ParsedElement]
    assets: list[ParsedAsset]
    warnings: list[str]


def validate_document_manifest_capacity(document: ParsedDocument) -> None:
    if len(document.elements) > _MAX_MANIFEST_ELEMENTS:
        raise ValueError("document manifest cannot exceed 20,000 elements")
    if len(document.assets) > _MAX_MANIFEST_ASSETS:
        raise ValueError("document manifest cannot exceed 500 assets")
    if len(document.warnings) > _MAX_MANIFEST_WARNINGS:
        raise ValueError("document manifest cannot exceed 1,000 warnings")


def document_parse_stage(filename: str) -> Literal["extract", "layout"]:
    extension = Path(filename).suffix.lower()
    if extension in _NATIVE_TEXT_EXTENSIONS | _NATIVE_IMAGE_EXTENSIONS:
        return "extract"
    return "layout"


class DoclingDocumentParser:
    def __init__(
        self,
        converter: DocumentConverter | None = None,
        *,
        num_threads: int = 2,
        document_timeout_seconds: float = 600.0,
        batch_size: int = 2,
        queue_max_size: int = 8,
    ) -> None:
        self._converter = converter
        self._num_threads = num_threads
        self._document_timeout_seconds = document_timeout_seconds
        self._batch_size = batch_size
        self._queue_max_size = queue_max_size

    def _build_pdf_pipeline_options(self) -> PdfPipelineOptions:
        from docling.datamodel.accelerator_options import (
            AcceleratorDevice,
            AcceleratorOptions,
        )
        from docling.datamodel.pipeline_options import PdfPipelineOptions

        models_path = os.environ.get("DOCLING_MODELS_PATH", "").strip()
        artifacts_path = Path(models_path) if models_path else None
        if artifacts_path is not None:
            missing = [
                name
                for name in _DOCLING_MODEL_FILES
                if not (artifacts_path / name).is_file()
                or (artifacts_path / name).stat().st_size == 0
            ]
            if missing:
                raise FileNotFoundError(
                    "Docling offline models are incomplete: " + ", ".join(missing)
                )

        return PdfPipelineOptions(
            artifacts_path=artifacts_path,
            accelerator_options=AcceleratorOptions(
                num_threads=self._num_threads,
                device=AcceleratorDevice.CPU,
            ),
            document_timeout=self._document_timeout_seconds,
            ocr_batch_size=self._batch_size,
            layout_batch_size=self._batch_size,
            table_batch_size=self._batch_size,
            queue_max_size=self._queue_max_size,
            enable_remote_services=False,
            allow_external_plugins=False,
            generate_parsed_pages=False,
            generate_picture_images=True,
            do_ocr=False,
        )

    def parse(self, *, filename: str, mime_type: str, content: bytes) -> ParsedDocument:
        del mime_type
        extension = Path(filename).suffix.lower()
        if extension in _NATIVE_TEXT_EXTENSIONS:
            return _parse_native_text(filename=filename, content=content)
        if extension in _NATIVE_IMAGE_EXTENSIONS:
            return _parse_native_image(filename=filename, content=content)
        from docling.datamodel.base_models import InputFormat
        from docling_core.types.io import DocumentStream
        from docling.document_converter import (
            DocumentConverter,
            PdfFormatOption,
        )

        converter = self._converter
        if converter is None:
            converter = DocumentConverter(
                format_options={
                    InputFormat.PDF: PdfFormatOption(
                        pipeline_options=self._build_pdf_pipeline_options()
                    )
                }
            )
        result = converter.convert(
            DocumentStream(name=filename, stream=io.BytesIO(content)),
            max_num_pages=_MAX_DOCUMENT_PAGES,
            max_file_size=_MAX_DOCUMENT_FILE_BYTES,
        )
        document: DoclingDocument = result.document
        elements, assets = self._normalize_document(
            document, extension=extension, source=content
        )
        return ParsedDocument(
            parser_name="docling",
            parser_version=version("docling"),
            pipeline_version="docling-structured-v1",
            elements=elements,
            assets=assets,
            warnings=[],
        )

    def _normalize_document(
        self,
        document: DoclingDocument,
        *,
        extension: str,
        source: bytes,
    ) -> tuple[list[ParsedElement], list[ParsedAsset]]:
        elements: list[ParsedElement] = []
        assets: list[ParsedAsset] = []
        headings: list[str] = []
        source_text = _decode_text_source(source, extension)
        spreadsheet_sheets = (
            _spreadsheet_sheet_names(source) if extension == ".xlsx" else []
        )
        text_cursor = 0
        for order, (item, level) in enumerate(document.iterate_items()):
            label = _label_value(getattr(item, "label", "paragraph"))
            kind = _element_kind(label)
            if kind is None:
                continue
            content_text = _item_text(item, document, kind)
            if kind == "heading" and content_text:
                depth = max(1, int(level or 1))
                headings = headings[: depth - 1] + [content_text]
            section_path = list(headings)
            locator, text_cursor = _source_locator(
                item,
                extension=extension,
                order=order,
                source=source,
                source_text=source_text,
                text=content_text,
                text_cursor=text_cursor,
                section_path=section_path,
                document=document,
                spreadsheet_sheets=spreadsheet_sheets,
            )
            element_key = _stable_key("element", order, getattr(item, "self_ref", None))
            element = ParsedElement(
                element_key=element_key,
                kind=kind,
                order=order,
                content_text=content_text,
                locator=locator,
                section_path=section_path,
                metadata_json={"docling_label": label},
            )
            elements.append(element)
            if kind == "figure":
                asset = _picture_asset(
                    item,
                    document=document,
                    order=order,
                    locator=locator,
                    element_key=element_key,
                )
                if asset is not None:
                    assets.append(asset)
        return elements, assets


def _parse_native_image(*, filename: str, content: bytes) -> ParsedDocument:
    extension = Path(filename).suffix.lower()
    width, height = _image_dimensions(content, fallback=(0, 0))
    if width <= 0 or height <= 0:
        raise ValueError("knowledge image is invalid")
    _validate_image_pixel_count(width, height)
    return ParsedDocument(
        parser_name="native-image",
        parser_version="1",
        pipeline_version="native-image-v1",
        elements=[],
        assets=[
            ParsedAsset(
                asset_key="asset-source-image",
                kind="image",
                mime_type=("image/png" if extension == ".png" else "image/jpeg"),
                content=content,
                locator={
                    "kind": "image",
                    "pixel_width": width,
                    "pixel_height": height,
                },
                width=width,
                height=height,
                caption=Path(filename).stem,
                ocr_text=None,
            )
        ],
        warnings=[],
    )


def _parse_native_text(*, filename: str, content: bytes) -> ParsedDocument:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("text knowledge documents must use UTF-8") from exc
    extension = Path(filename).suffix.lower()
    is_jsonl = extension == ".jsonl"
    is_markdown = extension in {".md", ".markdown"}
    elements: list[ParsedElement] = []
    heading_path: list[str] = []
    cursor = 0
    for line_number, raw_line in enumerate(text.splitlines(keepends=True), start=1):
        line_without_break = raw_line.rstrip("\r\n")
        normalized = line_without_break.strip()
        leading = len(line_without_break) - len(line_without_break.lstrip())
        start = cursor + leading
        end = start + len(normalized)
        cursor += len(raw_line)
        if not normalized:
            continue
        if is_jsonl:
            try:
                json.loads(normalized)
            except json.JSONDecodeError as exc:
                raise ValueError(f"JSONL line {line_number} is invalid JSON") from exc
        kind = "paragraph"
        content_text = normalized
        if is_markdown:
            heading_match = re.fullmatch(r"(#{1,6})\s+(.+)", normalized)
            if heading_match:
                level = len(heading_match.group(1))
                content_text = heading_match.group(2).strip()
                start += normalized.index(content_text)
                end = start + len(content_text)
                kind = "title" if level == 1 and not elements else "heading"
                heading_path = heading_path[: level - 1]
                heading_path.append(content_text)
        elements.append(
            ParsedElement(
                element_key=f"line-{line_number:06d}",
                kind=kind,
                order=len(elements),
                content_text=content_text,
                locator={
                    "kind": "text",
                    "line_start": line_number,
                    "line_end": line_number,
                    "char_start": start,
                    "char_end": end,
                    "heading_path": list(heading_path),
                },
                section_path=list(heading_path),
            )
        )
    if not elements:
        raise ValueError("text knowledge document contains no content")
    return ParsedDocument(
        parser_name="native-text",
        parser_version="1.0.0",
        pipeline_version="native-text-structured-v1",
        elements=elements,
        assets=[],
        warnings=[],
    )


def build_manifest(
    document: ParsedDocument,
    *,
    source_hash: str,
    document_title: str = "",
    description: str = "",
    max_chunk_tokens: int = _PARENT_MAX_TOKENS,
    target_chunk_tokens: int | None = None,
    min_chunk_tokens: int = _PARENT_MIN_TOKENS,
    forced_split_overlap_tokens: int = _FORCED_SPLIT_OVERLAP_TOKENS,
    max_chunk_chars: int | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    validate_document_manifest_capacity(document)
    if max_chunk_tokens < 1:
        raise ValueError("max_chunk_tokens must be positive")
    if forced_split_overlap_tokens < 0:
        raise ValueError("forced_split_overlap_tokens must not be negative")
    target_tokens = min(
        max_chunk_tokens,
        target_chunk_tokens
        if target_chunk_tokens is not None
        else min(_PARENT_TARGET_TOKENS, max_chunk_tokens),
    )
    if target_tokens < 1:
        raise ValueError("target_chunk_tokens must be positive")
    minimum_tokens = min(max(1, min_chunk_tokens), target_tokens)

    title = next(
        (
            element.content_text.strip()
            for element in document.elements
            if element.kind == "title" and element.content_text.strip()
        ),
        "",
    )
    if document_title or description:
        # Dense context is bounded; full description is retained by the source
        # contract and lexical index, never copied into verbatim body chunks.
        context_budget = max(0, min(240, max_chunk_tokens // 3))
        if max_chunk_chars is not None:
            context_budget = min(context_budget, max_chunk_chars // 3)
        title = "\n".join(
            filter(
                None,
                (
                    _context_prefix(document_title or title, context_budget // 2),
                    _context_prefix(description, context_budget - context_budget // 2),
                ),
            )
        )
    chunks: list[dict[str, Any]] = []
    pending: list[ParsedElement] = []

    def flush_pending() -> None:
        if not pending:
            return
        _append_text_element_chunks(
            chunks,
            pending,
            title=title,
            target_tokens=target_tokens,
            max_tokens=max_chunk_tokens,
            min_tokens=minimum_tokens,
            overlap_tokens=min(forced_split_overlap_tokens, max_chunk_tokens - 1),
            max_chars=max_chunk_chars,
        )
        pending.clear()

    for element in document.elements:
        text = element.content_text.strip()
        if _is_ignorable_element(element) or not text:
            continue
        is_pptx = element.locator.get("kind") == "pptx"
        if element.kind in {"title", "heading"} and not is_pptx:
            flush_pending()
            continue
        if element.kind in {"figure", "page"}:
            continue
        if element.kind == "table":
            flush_pending()
            _append_table_chunks(
                chunks,
                element,
                title=title,
                max_tokens=max_chunk_tokens,
                overlap_tokens=min(forced_split_overlap_tokens, max_chunk_tokens - 1),
                max_chars=max_chunk_chars,
            )
            continue
        if pending and (
            not _same_locator_anchor(pending[0].locator, element.locator)
            or (not is_pptx and pending[0].section_path != element.section_path)
        ):
            flush_pending()
        pending.append(element)
    flush_pending()

    asset_payloads = {asset.asset_key: asset.content for asset in document.assets}
    assets = []
    for asset in document.assets:
        _require_successful_vision_analysis(asset)
        content_hash = hashlib.sha256(asset.content).hexdigest()
        retrieval_text = _parsed_asset_retrieval_text(asset)
        assets.append(
            {
                "asset_key": asset.asset_key,
                "kind": asset.kind,
                "mime_type": asset.mime_type,
                "content_hash": content_hash,
                "locator": asset.locator,
                "width": asset.width,
                "height": asset.height,
                "caption": asset.caption,
                "ocr_text": asset.ocr_text,
                "vision_summary": asset.vision_summary,
                "vision_details": asset.vision_details,
                "vision_model": asset.vision_model,
                "vision_prompt_version": asset.vision_prompt_version,
                "vision_status": asset.vision_status,
                "related_element_keys": asset.related_element_keys,
                "visual_embedding": None,
                "text_embedding": None,
            }
        )
        if retrieval_text:
            chunks.append(
                {
                    **_chunk_manifest(
                        kind="image",
                        order=len(chunks),
                        content_text=retrieval_text,
                        title=title,
                        section_path=[],
                        locator=asset.locator,
                        element_keys=asset.related_element_keys,
                    ),
                    "asset_key": asset.asset_key,
                }
            )
    if len(chunks) > _MAX_MANIFEST_CHUNKS:
        raise ValueError("document manifest cannot exceed 10,000 chunks")
    manifest = {
        "schema_version": 1,
        "parser_name": document.parser_name,
        "parser_version": document.parser_version,
        "pipeline_version": f"{document.pipeline_version}+{_CHUNKER_VERSION}",
        "source_hash": source_hash,
        "elements": [
            {
                "element_key": element.element_key,
                "kind": element.kind,
                "order": element.order,
                "content_text": element.content_text,
                "locator": element.locator,
                "parent_key": element.parent_key,
                "section_path": element.section_path,
                "metadata_json": element.metadata_json,
            }
            for element in document.elements
        ],
        "chunks": chunks,
        "assets": assets,
        "warnings": document.warnings,
    }
    return manifest, asset_payloads


def _append_text_element_chunks(
    chunks: list[dict[str, Any]],
    elements: list[ParsedElement],
    *,
    title: str,
    target_tokens: int,
    max_tokens: int,
    min_tokens: int,
    overlap_tokens: int,
    max_chars: int | None,
) -> None:
    if not elements:
        return
    section_path = _representative_section_path(elements)
    grouped: list[list[ParsedElement]] = []
    current: list[ParsedElement] = []

    def append_current() -> None:
        if current:
            grouped.append(list(current))
            current.clear()

    for element in elements:
        if not _chunk_fits(
            element.content_text.strip(),
            title=title,
            section_path=section_path,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            append_current()
            for part in _split_text_for_budget(
                element.content_text.strip(),
                title=title,
                section_path=section_path,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
                max_chars=max_chars,
            ):
                grouped.append(
                    [
                        ParsedElement(
                            element_key=element.element_key,
                            kind=element.kind,
                            order=element.order,
                            content_text=part,
                            locator=element.locator,
                            section_path=element.section_path,
                            parent_key=element.parent_key,
                            metadata_json=element.metadata_json,
                        )
                    ]
                )
            continue

        candidate = [*current, element]
        candidate_text = _joined_element_text(candidate)
        if current and (
            not _chunk_fits(
                candidate_text,
                title=title,
                section_path=section_path,
                max_tokens=max_tokens,
                max_chars=max_chars,
            )
            or (
                _chunk_token_count(
                    _joined_element_text(current),
                    title=title,
                    section_path=section_path,
                )
                >= target_tokens
                and _chunk_token_count(
                    candidate_text,
                    title=title,
                    section_path=section_path,
                )
                > target_tokens
            )
        ):
            append_current()
        current.append(element)
    append_current()

    if len(grouped) >= 2:
        tail = grouped[-1]
        previous = grouped[-2]
        tail_tokens = _chunk_token_count(
            _joined_element_text(tail),
            title=title,
            section_path=section_path,
        )
        combined_text = _joined_element_text([*previous, *tail])
        if tail_tokens < min_tokens and _chunk_fits(
            combined_text,
            title=title,
            section_path=section_path,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            grouped[-2:] = [[*previous, *tail]]

    for group in grouped:
        content_text = _joined_element_text(group)
        chunks.append(
            _chunk_manifest(
                kind="text",
                order=len(chunks),
                content_text=content_text,
                title=title,
                section_path=_representative_section_path(group),
                locator=_merge_locators([element.locator for element in group]),
                element_keys=list(
                    dict.fromkeys(element.element_key for element in group)
                ),
            )
        )


def _append_table_chunks(
    chunks: list[dict[str, Any]],
    element: ParsedElement,
    *,
    title: str,
    max_tokens: int,
    overlap_tokens: int,
    max_chars: int | None,
) -> None:
    text = element.content_text.strip()
    if _chunk_fits(
        text,
        title=title,
        section_path=element.section_path,
        max_tokens=max_tokens,
        max_chars=max_chars,
    ):
        table_parts = [(text, element.locator)]
    else:
        markdown_parts = _split_markdown_table(
            text,
            locator=element.locator,
            title=title,
            section_path=element.section_path,
            max_tokens=max_tokens,
            max_chars=max_chars,
        )
        table_parts = markdown_parts or [
            (part, element.locator)
            for part in _split_text_for_budget(
                text,
                title=title,
                section_path=element.section_path,
                max_tokens=max_tokens,
                overlap_tokens=overlap_tokens,
                max_chars=max_chars,
            )
        ]
    for content_text, locator in table_parts:
        chunks.append(
            _chunk_manifest(
                kind="table",
                order=len(chunks),
                content_text=content_text,
                title=title,
                section_path=element.section_path,
                locator=locator,
                element_keys=[element.element_key],
            )
        )


def _split_markdown_table(
    text: str,
    *,
    locator: dict[str, Any],
    title: str,
    section_path: list[str],
    max_tokens: int,
    max_chars: int | None,
) -> list[tuple[str, dict[str, Any]]]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 3 or not _is_markdown_separator(lines[1]):
        return []
    header = lines[:2]
    rows = lines[2:]
    groups: list[list[str]] = []
    current: list[str] = []
    for row in rows:
        candidate = "\n".join([*header, *current, row])
        if current and not _chunk_fits(
            candidate,
            title=title,
            section_path=section_path,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            groups.append(current)
            current = []
        row_candidate = "\n".join([*header, row])
        if not _chunk_fits(
            row_candidate,
            title=title,
            section_path=section_path,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            return []
        current.append(row)
    if current:
        groups.append(current)
    if len(groups) < 2:
        return []

    source_start = int(locator.get("row_start") or 1)
    source_end = int(locator.get("row_end") or (source_start + len(lines) - 1))
    data_start = min(source_end, source_start + len(header))
    row_cursor = data_start
    result: list[tuple[str, dict[str, Any]]] = []
    for index, group in enumerate(groups):
        first_row = source_start if index == 0 else row_cursor
        last_row = min(source_end, row_cursor + len(group) - 1)
        result.append(
            (
                "\n".join([*header, *group]),
                _spreadsheet_row_locator(locator, first_row, last_row),
            )
        )
        row_cursor = last_row + 1
    return result


def _is_markdown_separator(line: str) -> bool:
    cells = [cell.strip() for cell in line.strip("|").split("|")]
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _spreadsheet_row_locator(
    locator: dict[str, Any], row_start: int, row_end: int
) -> dict[str, Any]:
    if locator.get("kind") != "spreadsheet":
        return deepcopy(locator)
    result = deepcopy(locator)
    result["row_start"] = row_start
    result["row_end"] = row_end
    cell_range = str(locator.get("cell_range") or "")
    match = re.fullmatch(r"([A-Z]+)\d+(?::([A-Z]+)\d+)?", cell_range)
    if match:
        start_column = match.group(1)
        end_column = match.group(2) or start_column
        start_cell = f"{start_column}{row_start}"
        end_cell = f"{end_column}{row_end}"
        result["cell_range"] = (
            start_cell if start_cell == end_cell else f"{start_cell}:{end_cell}"
        )
    return result


def _split_text_for_budget(
    text: str,
    *,
    title: str,
    section_path: list[str],
    max_tokens: int,
    overlap_tokens: int,
    max_chars: int | None,
) -> list[str]:
    units: list[str] = []
    for unit in _semantic_units(text):
        remainder = unit
        while remainder and not _chunk_fits(
            remainder,
            title=title,
            section_path=section_path,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            prefix_length = _largest_fitting_prefix(
                remainder,
                title=title,
                section_path=section_path,
                max_tokens=max_tokens,
                max_chars=max_chars,
            )
            units.append(remainder[:prefix_length].strip())
            remainder = remainder[prefix_length:].strip()
        if remainder:
            units.append(remainder)
    if not units:
        return []

    parts: list[str] = []
    start = 0
    while start < len(units):
        end = start
        selected: list[str] = []
        while end < len(units):
            candidate = "".join([*selected, units[end]])
            if selected and not _chunk_fits(
                candidate,
                title=title,
                section_path=section_path,
                max_tokens=max_tokens,
                max_chars=max_chars,
            ):
                break
            if not selected and not _chunk_fits(
                candidate,
                title=title,
                section_path=section_path,
                max_tokens=max_tokens,
                max_chars=max_chars,
            ):
                raise ValueError("chunk context leaves no room for source content")
            selected.append(units[end])
            end += 1
        parts.append("".join(selected).strip())
        if end >= len(units):
            break
        overlap_start = end
        overlap_size = 0
        while overlap_start > start:
            unit_tokens = _estimate_tokens(units[overlap_start - 1])
            if overlap_size + unit_tokens > overlap_tokens:
                break
            overlap_size += unit_tokens
            overlap_start -= 1
        if overlap_start == end and overlap_tokens > 0:
            suffix = _largest_token_bounded_suffix(parts[-1], overlap_tokens)
            if suffix and suffix != parts[-1]:
                units.insert(end, suffix)
            start = end
        elif overlap_start > start:
            start = overlap_start
        else:
            # Keep whole-unit overlap where possible while always advancing.
            start = min(end, start + 1)
    return parts


def _largest_fitting_prefix(
    text: str,
    *,
    title: str,
    section_path: list[str],
    max_tokens: int,
    max_chars: int | None,
) -> int:
    low = 1
    high = len(text)
    best = 0
    while low <= high:
        middle = (low + high) // 2
        if _chunk_fits(
            text[:middle],
            title=title,
            section_path=section_path,
            max_tokens=max_tokens,
            max_chars=max_chars,
        ):
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    if best == 0:
        raise ValueError("chunk context leaves no room for source content")
    return best


def _semantic_units(text: str) -> list[str]:
    return [
        unit.strip()
        for unit in re.split(r"(?<=[。！？.!?])|\n{2,}", text)
        if unit.strip()
    ]


def _joined_element_text(elements: list[ParsedElement]) -> str:
    return "\n\n".join(
        element.content_text.strip()
        for element in elements
        if element.content_text.strip()
    )


def _representative_section_path(elements: list[ParsedElement]) -> list[str]:
    return list(
        max((element.section_path for element in elements), key=len, default=[])
    )


def _chunk_fits(
    content_text: str,
    *,
    title: str,
    section_path: list[str],
    max_tokens: int,
    max_chars: int | None,
) -> bool:
    if max_chars is not None and len(content_text) > max_chars:
        return False
    return (
        _chunk_token_count(
            content_text,
            title=title,
            section_path=section_path,
        )
        <= max_tokens
    )


def _chunk_token_count(
    content_text: str, *, title: str, section_path: list[str]
) -> int:
    return _estimate_tokens(
        _retrieval_text(
            content_text,
            title=title,
            section_path=section_path,
        )
    )


def _estimate_tokens(text: str) -> int:
    """Conservative tokenizer-independent estimate for mixed CJK/Latin evidence."""
    tokens = 0
    for unit in re.findall(r"[A-Za-z0-9_]+|[^\x00-\x7F]|\S", text):
        if unit.isascii() and (unit[0].isalnum() or unit[0] == "_"):
            tokens += max(1, math.ceil(len(unit) / 4))
        else:
            tokens += 1
    return max(1, tokens)


def _context_prefix(value: str, max_tokens: int) -> str:
    if max_tokens <= 0:
        return ""
    low, high = 0, len(value)
    while low < high:
        middle = (low + high + 1) // 2
        if _estimate_tokens(value[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return value[:low].strip()


def _retrieval_text(content_text: str, *, title: str, section_path: list[str]) -> str:
    content_lines = {line.strip() for line in content_text.splitlines() if line.strip()}
    context = [
        value
        for value in dict.fromkeys([title, *section_path])
        if value and value not in content_lines
    ]
    return "\n".join([*filter(None, context), content_text])


def _is_ignorable_element(element: ParsedElement) -> bool:
    if element.locator.get("kind") != "pptx":
        return False
    label = str(element.metadata_json.get("docling_label") or "").lower()
    slide = str(element.locator.get("slide") or "")
    if element.content_text.strip() != slide:
        return False
    if label == "page_footer":
        return True
    bbox = element.locator.get("bbox")
    if not isinstance(bbox, dict):
        return False
    width = float(bbox.get("right", 1)) - float(bbox.get("left", 0))
    height = float(bbox.get("bottom", 1)) - float(bbox.get("top", 0))
    near_vertical_edge = (
        float(bbox.get("top", 1)) <= 0.12 or float(bbox.get("bottom", 0)) >= 0.88
    )
    return width <= 0.15 and height <= 0.15 and near_vertical_edge


def enrich_manifest_embeddings(
    manifest: dict[str, Any],
    *,
    asset_payloads: dict[str, bytes],
    embed_texts: Callable[[list[str]], list[list[float]]],
    embed_image: Callable[[bytes, str, str | None], list[float] | None] | None,
    max_concurrency: int = 1,
) -> tuple[dict[str, Any], int]:
    enriched = deepcopy(manifest)
    chunks = enriched.get("chunks", [])
    dense_chunk_indexes = [
        index for index, chunk in enumerate(chunks) if chunk.get("kind") != "image"
    ]
    if dense_chunk_indexes:
        dense_embeddings = embed_texts(
            [str(chunks[index]["retrieval_text"]) for index in dense_chunk_indexes]
        )
        if len(dense_embeddings) != len(dense_chunk_indexes):
            raise ValueError("dense embedding count does not match retrieval chunks")
        for index, embedding in zip(dense_chunk_indexes, dense_embeddings, strict=True):
            chunks[index]["dense_embedding"] = embedding

    units_by_chunk = [
        _multi_vector_units(str(chunk["retrieval_text"])) for chunk in chunks
    ]
    flattened_units = [unit for units in units_by_chunk for unit in units]
    unit_embeddings = embed_texts(flattened_units) if flattened_units else []
    cursor = 0
    for chunk, units in zip(chunks, units_by_chunk, strict=True):
        chunk["multi_embedding"] = unit_embeddings[cursor : cursor + len(units)]
        cursor += len(units)

    assets = enriched.get("assets", [])
    text_asset_indexes: list[int] = []
    text_asset_inputs: list[str] = []
    for index, asset in enumerate(assets):
        asset_text = _asset_retrieval_text(asset)
        if asset_text:
            text_asset_indexes.append(index)
            text_asset_inputs.append(asset_text)
    if text_asset_inputs:
        text_asset_embeddings = embed_texts(text_asset_inputs)
        if len(text_asset_embeddings) != len(text_asset_indexes):
            raise ValueError("asset text embedding count does not match assets")
        for index, embedding in zip(
            text_asset_indexes, text_asset_embeddings, strict=True
        ):
            assets[index]["text_embedding"] = embedding
    if embed_image is not None:
        visual_embeddings = bounded_ordered_map(
            assets,
            lambda asset: embed_image(
                asset_payloads[str(asset["asset_key"])],
                str(asset["mime_type"]),
                _asset_retrieval_text(asset) or None,
            ),
            max_concurrency=max_concurrency,
            thread_name_prefix="knowledge-image-embedding",
        )
        for asset, visual_embedding in zip(assets, visual_embeddings, strict=True):
            asset["visual_embedding"] = visual_embedding

    embeddings = [
        embedding
        for chunk in chunks
        for embedding in [
            chunk.get("dense_embedding"),
            *(chunk.get("multi_embedding") or []),
        ]
        if embedding is not None
    ]
    embeddings.extend(
        embedding
        for asset in assets
        for embedding in (
            asset.get("visual_embedding"),
            asset.get("text_embedding"),
        )
        if embedding is not None
    )
    if not embeddings:
        raise ValueError("document manifest contains no embeddable evidence")
    dimension = len(embeddings[0])
    if dimension == 0 or any(len(embedding) != dimension for embedding in embeddings):
        raise ValueError("document manifest embeddings have inconsistent dimensions")
    return enriched, dimension


def _chunk_manifest(
    *,
    kind: Literal["text", "table", "image"],
    order: int,
    content_text: str,
    title: str,
    section_path: list[str],
    locator: dict[str, Any],
    element_keys: list[str],
) -> dict[str, Any]:
    retrieval_text = _retrieval_text(
        content_text,
        title=title,
        section_path=section_path,
    )
    return {
        "chunk_key": f"chunk-{order:06d}",
        "kind": kind,
        "order": order,
        "content_text": content_text,
        "retrieval_text": retrieval_text,
        "content_hash": hashlib.sha256(content_text.encode()).hexdigest(),
        "locator": locator,
        "element_keys": element_keys,
        "asset_key": None,
        "parent_chunk_key": None,
        "dense_embedding": None,
        "multi_embedding": None,
    }


def _multi_vector_units(text: str) -> list[str]:
    if _estimate_tokens(text) <= _CHILD_MAX_TOKENS:
        return []
    semantic_units = _semantic_units(text)
    children: list[str] = []
    current: list[str] = []
    for unit in semantic_units:
        candidate = "".join([*current, unit])
        if current and _estimate_tokens(candidate) > _CHILD_TARGET_TOKENS:
            children.append("".join(current).strip())
            current = []
        if _estimate_tokens(unit) > _CHILD_MAX_TOKENS:
            remainder = unit
            while remainder:
                prefix_length = _largest_plain_text_prefix(remainder, _CHILD_MAX_TOKENS)
                children.append(remainder[:prefix_length].strip())
                remainder = remainder[prefix_length:].strip()
            continue
        current.append(unit)
    if current:
        children.append("".join(current).strip())

    bounded: list[str] = []
    for child in children:
        if not child:
            continue
        if _estimate_tokens(child) <= _CHILD_MAX_TOKENS:
            bounded.append(child)
        else:
            remainder = child
            while remainder:
                prefix_length = _largest_plain_text_prefix(remainder, _CHILD_MAX_TOKENS)
                bounded.append(remainder[:prefix_length].strip())
                remainder = remainder[prefix_length:].strip()
    if len(bounded) < 2:
        return []
    return bounded[:_CHILD_MAX_UNITS]


def _largest_plain_text_prefix(text: str, max_tokens: int) -> int:
    low = 1
    high = len(text)
    best = 1
    while low <= high:
        middle = (low + high) // 2
        if _estimate_tokens(text[:middle]) <= max_tokens:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return best


def _largest_token_bounded_suffix(text: str, max_tokens: int) -> str:
    low = 0
    high = len(text)
    best = ""
    while low <= high:
        middle = (low + high) // 2
        candidate = text[len(text) - middle :]
        if _estimate_tokens(candidate) <= max_tokens:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best


def _asset_retrieval_text(asset: dict[str, Any]) -> str:
    return vision_retrieval_text(
        vision_summary=asset.get("vision_summary"),
        vision_details=asset.get("vision_details"),
        caption=asset.get("caption"),
        ocr_text=asset.get("ocr_text"),
    )


def _parsed_asset_retrieval_text(asset: ParsedAsset) -> str:
    return vision_retrieval_text(
        vision_summary=asset.vision_summary,
        vision_details=asset.vision_details,
        caption=asset.caption,
        ocr_text=asset.ocr_text,
    )


def vision_retrieval_text(
    *,
    vision_summary: object,
    vision_details: object,
    caption: object,
    ocr_text: object,
) -> str:
    lines: list[str] = []
    if isinstance(vision_summary, str) and vision_summary.strip():
        lines.append(vision_summary.strip())
    if isinstance(vision_details, dict):
        asset_type = vision_details.get("asset_type")
        if isinstance(asset_type, str) and asset_type.strip():
            lines.append(f"图像类型：{asset_type.strip()}")
        for key, label in (
            ("visible_text", "可见文字"),
            ("key_facts", "关键事实"),
            ("visual_relationships", "视觉关系"),
            ("uncertainties", "不确定项"),
        ):
            values = vision_details.get(key)
            if isinstance(values, list):
                normalized = [
                    value.strip()
                    for value in values
                    if isinstance(value, str) and value.strip()
                ]
                if normalized:
                    lines.append(f"{label}：{'；'.join(normalized)}")
    if isinstance(caption, str) and caption.strip():
        lines.append(f"标题：{caption.strip()}")
    if isinstance(ocr_text, str) and ocr_text.strip():
        lines.append(f"OCR：{ocr_text.strip()}")
    return "\n".join(lines)


def _require_successful_vision_analysis(asset: ParsedAsset) -> None:
    text_fields = (
        asset.vision_summary,
        asset.vision_model,
        asset.vision_prompt_version,
    )
    required_detail_keys = {
        "asset_type",
        "visible_text",
        "key_facts",
        "visual_relationships",
        "uncertainties",
    }
    details = asset.vision_details
    details_valid = (
        isinstance(details, dict)
        and set(details) == required_detail_keys
        and isinstance(details.get("asset_type"), str)
        and bool(str(details["asset_type"]).strip())
        and all(
            isinstance(details.get(key), list)
            and all(
                isinstance(value, str) and bool(value.strip()) for value in details[key]
            )
            for key in required_detail_keys - {"asset_type"}
        )
    )
    if (
        asset.vision_status != "succeeded"
        or not all(isinstance(value, str) and value.strip() for value in text_fields)
        or not details_valid
    ):
        raise ValueError(
            f"asset {asset.asset_key!r} is missing successful vision analysis"
        )


def _same_locator_anchor(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("kind") != right.get("kind"):
        return False
    keys_by_kind = {
        "pdf": ("page",),
        "docx": ("section_path",),
        "pptx": ("slide",),
        "spreadsheet": ("sheet",),
        "image": (),
        "text": ("heading_path",),
        "html": ("heading_path",),
    }
    return all(left.get(key) == right.get(key) for key in keys_by_kind[left["kind"]])


def _merge_locators(locators: list[dict[str, Any]]) -> dict[str, Any]:
    merged = deepcopy(locators[0])
    kind = merged["kind"]
    if kind == "docx":
        merged["paragraph_start"] = min(
            int(locator["paragraph_start"]) for locator in locators
        )
        merged["paragraph_end"] = max(
            int(locator["paragraph_end"]) for locator in locators
        )
    elif kind in {"text", "html"}:
        for start_field, end_field in (
            ("line_start", "line_end"),
            ("char_start", "char_end"),
            ("element_start", "element_end"),
        ):
            starts = [
                int(locator[start_field])
                for locator in locators
                if locator.get(start_field) is not None
            ]
            ends = [
                int(locator[end_field])
                for locator in locators
                if locator.get(end_field) is not None
            ]
            if starts:
                merged[start_field] = min(starts)
            if ends:
                merged[end_field] = max(ends)
    boxes = [
        locator["bbox"] for locator in locators if isinstance(locator.get("bbox"), dict)
    ]
    if boxes:
        merged["bbox"] = {
            "left": min(float(box["left"]) for box in boxes),
            "top": min(float(box["top"]) for box in boxes),
            "right": max(float(box["right"]) for box in boxes),
            "bottom": max(float(box["bottom"]) for box in boxes),
        }
    return merged


def _label_value(label: object) -> str:
    value = getattr(label, "value", label)
    return str(value).lower()


def _element_kind(label: str) -> ElementKind | None:
    if label == "title":
        return "title"
    if label in {"section_header", "heading"}:
        return "heading"
    if label == "table":
        return "table"
    if label == "picture":
        return "figure"
    if label in {"caption", "footnote"}:
        return "caption"
    if label == "formula":
        return "formula"
    if label == "code":
        return "code"
    if label in {"list_item", "checkbox_selected", "checkbox_unselected"}:
        return "list"
    if label in {"text", "paragraph", "page_header", "page_footer"}:
        return "paragraph"
    return None


def _item_text(item: object, document: object, kind: ElementKind) -> str:
    export = getattr(item, "export_to_markdown", None)
    if kind == "table" and callable(export):
        return str(export(doc=document)).strip()
    return str(getattr(item, "text", "") or "").strip()


def _source_locator(
    item: object,
    *,
    extension: str,
    order: int,
    source: bytes,
    source_text: str | None,
    text: str,
    text_cursor: int,
    section_path: list[str],
    document: object | None = None,
    spreadsheet_sheets: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    provenance = list(getattr(item, "prov", []) or [])
    page = max(1, int(getattr(provenance[0], "page_no", 1))) if provenance else 1
    normalized_bbox = _normalized_bbox(item, document, page)
    if extension == ".pdf":
        return {
            "kind": "pdf",
            "page": page,
            **({"bbox": normalized_bbox} if normalized_bbox else {}),
        }, text_cursor
    if extension == ".docx":
        return (
            {
                "kind": "docx",
                "section_path": section_path,
                "paragraph_start": order,
                "paragraph_end": order,
                **({"bbox": normalized_bbox} if normalized_bbox else {}),
            },
            text_cursor,
        )
    if extension == ".pptx":
        return (
            {
                "kind": "pptx",
                "slide": page,
                "shape_id": str(getattr(item, "self_ref", order)),
                **({"bbox": normalized_bbox} if normalized_bbox else {}),
            },
            text_cursor,
        )
    if extension == ".xlsx":
        bbox = getattr(provenance[0], "bbox", None) if provenance else None
        start_column = max(1, math.floor(float(getattr(bbox, "l", 0))) + 1)
        end_column = max(start_column, math.ceil(float(getattr(bbox, "r", 1))))
        row_start = max(1, math.floor(float(getattr(bbox, "t", 0))) + 1)
        row_end = max(row_start, math.ceil(float(getattr(bbox, "b", 1))))
        start_cell = f"{_excel_column_name(start_column)}{row_start}"
        end_cell = f"{_excel_column_name(end_column)}{row_end}"
        sheets = spreadsheet_sheets or []
        return (
            {
                "kind": "spreadsheet",
                "sheet": sheets[page - 1] if page <= len(sheets) else f"Sheet{page}",
                "cell_range": (
                    start_cell if start_cell == end_cell else f"{start_cell}:{end_cell}"
                ),
                "row_start": row_start,
                "row_end": row_end,
            },
            text_cursor,
        )
    if extension in {".png", ".jpg", ".jpeg"}:
        width, height = _image_dimensions(source, fallback=(1, 1))
        return (
            {
                "kind": "image",
                "pixel_width": width,
                "pixel_height": height,
                **({"bbox": normalized_bbox} if normalized_bbox else {}),
            },
            text_cursor,
        )
    start = source_text.find(text, text_cursor) if source_text and text else -1
    start = text_cursor if start < 0 else start
    end = start + len(text)
    line_start = (source_text or "")[:start].count("\n") + 1
    line_end = (source_text or "")[:end].count("\n") + 1
    return (
        {
            "kind": "text",
            "line_start": line_start,
            "line_end": line_end,
            "char_start": start,
            "char_end": max(start + 1, end),
            "heading_path": section_path,
        },
        end,
    )


def _normalized_bbox(
    item: object, document: object | None, page: int
) -> dict[str, float] | None:
    provenance = list(getattr(item, "prov", []) or [])
    pages = getattr(document, "pages", {}) if document is not None else {}
    page_item = pages.get(page) if isinstance(pages, dict) else None
    size = getattr(page_item, "size", None)
    bbox = getattr(provenance[0], "bbox", None) if provenance else None
    if bbox is None or size is None:
        return None
    try:
        normalized = bbox.to_top_left_origin(float(size.height)).normalized(size)
        values = {
            "left": float(normalized.l),
            "top": float(normalized.t),
            "right": float(normalized.r),
            "bottom": float(normalized.b),
        }
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return None
    return {key: min(1.0, max(0.0, value)) for key, value in values.items()}


def _spreadsheet_sheet_names(source: bytes) -> list[str]:
    try:
        with zipfile.ZipFile(io.BytesIO(source)) as archive:
            root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    except (ElementTree.ParseError, KeyError, OSError, zipfile.BadZipFile):
        return []
    namespace = {"main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    return [
        name
        for sheet in root.findall("main:sheets/main:sheet", namespace)
        if (name := str(sheet.attrib.get("name") or "").strip())
    ]


def _excel_column_name(index: int) -> str:
    name = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name or "A"


def _picture_asset(
    item: object,
    *,
    document: object,
    order: int,
    locator: dict[str, Any],
    element_key: str,
) -> ParsedAsset | None:
    get_image = getattr(item, "get_image", None)
    if not callable(get_image):
        return None
    from PIL import Image

    image = get_image(document)
    if image is None:
        return None
    if not isinstance(image, Image.Image):
        raise TypeError("Document image extraction must return a Pillow image")
    if min(int(image.width), int(image.height)) < _MIN_EMBEDDED_VISION_IMAGE_DIMENSION:
        return None
    _validate_image_pixel_count(int(image.width), int(image.height))
    output = io.BytesIO()
    image.save(output, format="PNG")
    content = output.getvalue()
    return ParsedAsset(
        asset_key=f"asset-{order:06d}",
        kind="figure",
        mime_type="image/png",
        content=content,
        locator=locator,
        width=int(image.width),
        height=int(image.height),
        ocr_text=None,
        related_element_keys=[element_key],
    )


@lru_cache(maxsize=1)
def _asset_ocr_engine() -> object:
    from rapidocr import EngineType, RapidOCR

    return RapidOCR(
        params={
            "Det.engine_type": EngineType.TORCH,
            "Cls.engine_type": EngineType.TORCH,
            "Rec.engine_type": EngineType.TORCH,
        }
    )


def extract_image_ocr_text(content: bytes) -> str | None:
    try:
        result = _asset_ocr_engine()(content)  # type: ignore[operator]
        texts = getattr(result, "txts", None) or ()
        text = "\n".join(str(value).strip() for value in texts if str(value).strip())
        return text[:20_000] or None
    except Exception:
        # The conditional OCR caller turns an empty result into a retryable task error.
        logger.warning("embedded knowledge image OCR failed", exc_info=True)
        return None


def _decode_text_source(source: bytes, extension: str) -> str | None:
    if extension not in {".txt", ".md", ".csv", ".jsonl"}:
        return None
    return source.decode("utf-8")


def _image_dimensions(source: bytes, *, fallback: tuple[int, int]) -> tuple[int, int]:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(source)) as image:
            return int(image.width), int(image.height)
    except (OSError, ValueError):
        return fallback


def _validate_image_pixel_count(width: int, height: int) -> None:
    if width * height > _MAX_IMAGE_PIXELS:
        raise ValueError("knowledge image exceeds the configured pixel limit")


def _stable_key(prefix: str, order: int, self_ref: object) -> str:
    suffix = hashlib.sha256(str(self_ref or order).encode()).hexdigest()[:12]
    return f"{prefix}-{order:06d}-{suffix}"
