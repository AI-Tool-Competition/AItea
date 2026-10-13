"""Pure document ingestion, grounded retrieval and bounded HTTPS collection.

Call these blocking functions from a worker, never from the API event loop.
Business ownership, version persistence and access control belong to the service.
"""

from __future__ import annotations

import hashlib
import codecs
import http.client
import io
import ipaddress
import json
import math
import mimetypes
import os
import queue
import re
import socket
import ssl
import threading
import time
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from .vendor.geo_parser.document_ingestion import (
    DoclingDocumentParser,
    ParsedDocument,
    ParsedElement,
    _parse_native_text,
    build_manifest,
)

MAX_BYTES = 20 * 1024 * 1024
MAX_ORIGINAL_BYTES = 128 * 1024 * 1024
MAX_COMPRESSED_HTML_BYTES = 4 * 1024 * 1024
MAX_TEXT = 4_000_000
MAX_ELEMENTS = 20_000
MAX_PAGES = 300
_DNS_SLOTS = threading.BoundedSemaphore(8)
_DIRECT_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md", ".xlsx", ".xls", ".csv"}
_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}


def _decode(content: bytes) -> str:
    # Honor a declared HTML charset before trying a Chinese legacy encoding:
    # Latin-1 can otherwise decode successfully as mojibake under GB18030.
    if not content.startswith(b"\xef\xbb\xbf"):
        for tag in re.findall(rb"<meta\b[^>]{0,1024}>", content[:8192], re.I):
            match = re.search(rb"charset\s*=\s*[\"']?\s*([A-Za-z0-9._-]{1,40})", tag, re.I)
            if match:
                try:
                    encoding = codecs.lookup(match[1].decode("ascii")).name
                    if encoding not in {"utf-8", "iso8859-1", "cp1252", "gb18030", "gbk", "gb2312", "big5"}:
                        raise ValueError("HTML 声明了不支持的文本编码。")
                    return content.decode(encoding)
                except (LookupError, UnicodeDecodeError) as exc:
                    raise ValueError("HTML 声明的文本编码无法读取。") from exc
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("文档文本编码无法识别，请转换为 UTF-8。")


class _HTMLDocument(HTMLParser):
    """Extract readable blocks and actual HTML source-line anchors, not fake offsets."""

    _blocks = {
        "p",
        "div",
        "section",
        "article",
        "li",
        "tr",
        "blockquote",
        "pre",
        "br",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
    _hidden = {"script", "style", "noscript", "template", "head"}

    def __init__(self, *, main_only=False) -> None:
        super().__init__(convert_charrefs=True)
        self.main_only, self.in_main = main_only, False
        self.has_main = False
        self.elements: list[ParsedElement] = []
        self.headings: list[str] = []
        self.buffer: list[str] = []
        self.heading_level = 0
        self.hidden: list[str] = []
        self.start_line = 1
        self.links: list[str] = []
        self.anchor: str | None = None

    def _flush(self) -> None:
        text = re.sub(r"\s+", " ", "".join(self.buffer)).strip()
        self.buffer = []
        if not text:
            return
        if len(self.elements) >= MAX_ELEMENTS:
            raise ValueError("HTML 元素数量超过限制。")
        kind = "paragraph"
        if self.heading_level:
            self.headings = self.headings[: self.heading_level - 1] + [text]
            kind = "heading"
        order = len(self.elements) + 1
        locator = {
            "kind": "html",
            "element_start": order,
            "element_end": order,
            "line_start": self.start_line,
            "line_end": self.getpos()[0],
            "heading_path": list(self.headings),
        }
        if self.anchor:
            locator["anchor"] = self.anchor
        self.elements.append(
            ParsedElement(f"html-{order:06}", kind, order - 1, text, locator, list(self.headings))
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._hidden or (self.main_only and tag in {"nav", "aside"}):
            self.hidden.append(tag)
            return
        if self.hidden:
            return
        if tag == "main":
            self.in_main, self.has_main = True, True
        # Article headers/footers can contain the title, issue date and source.
        # Site-wide versions already fall outside the actual main element.
        if self.main_only and not self.in_main:
            return
        values = dict(attrs)
        if tag == "a" and values.get("href"):
            self.links.append(values["href"])
        if tag in self._blocks:
            self._flush()
            self.heading_level = int(tag[1]) if re.fullmatch("h[1-6]", tag) else 0
            self.start_line = self.getpos()[0]
            self.anchor = values.get("id")

    def handle_endtag(self, tag: str) -> None:
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if self.main_only and tag == "main":
            self._flush()
            self.in_main = False
            return
        if self.main_only and not self.in_main:
            return
        if tag in self._blocks:
            self._flush()
            self.heading_level = 0
            self.anchor = None
            self.start_line = self.getpos()[0]

    def handle_data(self, data: str) -> None:
        if not self.hidden and (not self.main_only or self.in_main):
            self.buffer.append(data)

    def finish(self) -> None:
        self.close()
        self._flush()


def _parse_html(content: bytes) -> ParsedDocument:
    source = _decode(content)
    if len(source) > MAX_TEXT:
        raise ValueError("HTML 文本超过处理限制。")
    extractor = _HTMLDocument(main_only=True)
    extractor.feed(source)
    extractor.finish()
    if not extractor.has_main:
        # A main marker inside a comment, script or template is not document
        # structure. Preserve legacy pages without a real main container.
        extractor = _HTMLDocument()
        extractor.feed(source)
        extractor.finish()
    return ParsedDocument("html-basic", "2", "html-blocks-v2", extractor.elements, [], [])


def _parse_docx(content: bytes) -> ParsedDocument:
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > 2000 or sum(x.file_size for x in entries) > 50 * 1024 * 1024:
                raise ValueError("DOCX 解压大小或文件数量超过限制。")
            entry = archive.getinfo("word/document.xml")
            if entry.file_size > 16 * 1024 * 1024:
                raise ValueError("DOCX 正文超过处理限制。")
            source = archive.read(entry)
    except (zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise ValueError("DOCX 文件损坏或缺少正文。") from exc
    if re.search(rb"<!\s*(DOCTYPE|ENTITY)\b", source, flags=re.I):
        raise ValueError("DOCX 不允许 DOCTYPE 或 ENTITY 声明。")
    try:
        root = ElementTree.fromstring(source)
    except ElementTree.ParseError as exc:
        raise ValueError("DOCX 正文 XML 无法解析。") from exc
    elements: list[ParsedElement] = []
    headings: list[str] = []
    for index, paragraph in enumerate(root.findall(".//w:p", ns), start=1):
        if index > MAX_ELEMENTS:
            raise ValueError("DOCX 段落数量超过限制。")
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", ns)).strip()
        if not text:
            continue
        style = paragraph.find("w:pPr/w:pStyle", ns)
        style_name = style.get(f"{{{ns['w']}}}val", "") if style is not None else ""
        heading = re.search(r"(?:heading|标题)\s*([1-6])", style_name, re.I)
        kind = "paragraph"
        if heading:
            level = int(heading.group(1))
            headings = headings[: level - 1] + [text]
            kind = "heading"
        locator = {
            "kind": "docx",
            "paragraph_start": index,
            "paragraph_end": index,
            "section_path": list(headings),
        }
        elements.append(
            ParsedElement(f"paragraph-{index:06}", kind, index - 1, text, locator, list(headings))
        )
    return ParsedDocument(
        "docx-basic",
        "1",
        "docx-paragraphs-v1",
        elements,
        [],
        ["轻量 DOCX 解析保留段落顺序；复杂表格和图片请使用 Docling 并核对原文。"],
    )


def _parse_pdf(content: bytes) -> ParsedDocument:
    try:
        from pypdf import PdfReader
        from importlib.metadata import version
    except ImportError as exc:
        raise ValueError("PDF 解析器尚未安装，请安装后端 pypdf 依赖。") from exc
    try:
        reader = PdfReader(io.BytesIO(content), strict=False)
        if reader.is_encrypted:
            raise ValueError("不支持加密 PDF，请先提供解密后的文档。")
        if len(reader.pages) > MAX_PAGES:
            raise ValueError(f"PDF 不能超过 {MAX_PAGES} 页。")
        pages: list[dict] = []
        empty_pages: list[int] = []
        characters = 0
        source_hash = hashlib.sha256(content).hexdigest()
        for index, page in enumerate(reader.pages, start=1):
            stream = page.get_contents()
            if stream is not None and len(stream.get_data()) > 16 * 1024 * 1024:
                raise ValueError("PDF 页面内容流超过处理限制。")
            text = (page.extract_text() or "").strip()
            characters += len(text)
            if characters > MAX_TEXT:
                raise ValueError("PDF 提取文本超过处理限制。")
            if not text:
                empty_pages.append(index)
            reasons = [] if text else ["empty_text"]
            if _pdf_large_raster(page):
                reasons.append("large_raster")
            if _pdf_table_layout(stream):
                reasons.append("table_layout")
            pages.append(
                {
                    "page": index,
                    "printed_page": None,
                    "native_text": text,
                    "text": text,
                    "status": "pending_ocr" if reasons else "native",
                    "ocr_required": bool(reasons),
                    "reasons": reasons,
                    "source_hash": source_hash,
                }
            )
        table_pages = [page for page in pages if page["reasons"] == ["table_layout"]]
        if table_pages:
            import pypdfium2 as pdfium
            from .pdf_tables import VERSION, extract_grid

            with pdfium.PdfDocument(content) as document:
                for page in table_pages:
                    page["native_grid_attempt"] = VERSION
                    pdfpage = document[page["page"] - 1]
                    try:
                        table = extract_grid(pdfpage, page["native_text"])
                    finally:
                        pdfpage.close()
                    if table:
                        page.update(
                            text=table.pop("text"),
                            status="native",
                            ocr_required=False,
                            reasons=[],
                            native_table={**table, "source_hash": source_hash, "page": page["page"]},
                        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF 无法读取，请检查文件是否损坏。") from exc
    warnings = ["PDF 原生文本不保证扫描页或复杂表格准确；页状态保留待识别和待核对项。"]
    if empty_pages:
        warnings.append("以下页未提取到文本，需要核对或 OCR：" + ", ".join(map(str, empty_pages)))
    if any("large_raster" in page["reasons"] for page in pages):
        warnings.append("存在大型扫描图，文字层仍需 OCR 核对。")
    return ParsedDocument(
        "pypdf", version("pypdf"), "pdf-pages-v4", _pdf_elements(pages), [], warnings, pages
    )


def _pdf_large_raster(page) -> bool:
    """Inspect dimensions only; never decompress image bytes to detect scans."""
    pending = [(page.get("/Resources", {}), 0)]
    inspected = 0
    while pending:
        resources, depth = pending.pop()
        objects = resources.get_object().get("/XObject", {})
        for reference in (
            objects.get_object().values() if hasattr(objects, "get_object") else objects.values()
        ):
            inspected += 1
            if inspected > 1000:
                return True  # Complex image trees require review, not an unbounded walk.
            obj = reference.get_object()
            if obj.get("/Subtype") == "/Image":
                width, height = int(obj.get("/Width", 0)), int(obj.get("/Height", 0))
                if width >= 600 and height >= 600 and width * height >= 1_000_000:
                    return True
            elif obj.get("/Subtype") == "/Form" and depth < 4 and obj.get("/Resources"):
                pending.append((obj["/Resources"], depth + 1))
    return False


TABLE_LAYOUT_VERSION = "painted-grid-intersections-v2"


def _pdf_table_layout(stream) -> bool:
    """Detect connected grid boundaries, retaining merged cells as OCR work.

    Three unrelated rules do not establish a table. Outer columns must enclose
    at least three spanning row rules and a distinct internal divider. This
    excludes separate note boxes and most diagrams, while incomplete internal
    dividers still require OCR rather than native cell reconstruction.
    """
    if stream is None:
        return False
    horizontal, vertical, pending_h, pending_v = [], [], [], []
    point = None
    for index, (args, operator) in enumerate(stream.operations):
        if index > 100_000:
            return True  # Geometry is too complex to certify plain reading order.
        if operator == b"re" and len(args) == 4:
            x, y, width, height = map(float, args)
            if abs(width) > 20 and 0 < abs(height) < 2:
                pending_h.append((y, min(x, x + width), max(x, x + width)))
            if abs(height) > 20 and 0 < abs(width) < 2:
                pending_v.append((x, min(y, y + height), max(y, y + height)))
        elif operator == b"m" and len(args) == 2:
            point = tuple(map(float, args))
        elif operator == b"l" and len(args) == 2 and point is not None:
            x, y = map(float, args)
            if abs(x - point[0]) > 20 and abs(y - point[1]) < 1:
                pending_h.append((y, min(x, point[0]), max(x, point[0])))
            if abs(y - point[1]) > 20 and abs(x - point[0]) < 1:
                pending_v.append((x, min(y, point[1]), max(y, point[1])))
            point = (x, y)
        elif operator in {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*", b"n"}:
            if operator != b"n":
                horizontal.extend(pending_h)
                vertical.extend(pending_v)
            pending_h, pending_v, point = [], [], None
    if len(horizontal) < 3 or len(vertical) < 3:
        return False
    if len(horizontal) > 4096 or len(vertical) > 4096:
        return True
    from .pdf_tables import _cluster

    def merged(lines):
        positions = _cluster([line[0] for line in lines], tolerance=2)
        if len(positions) > 256:
            return None
        result = []
        for position in positions:
            spans = sorted((a, b) for p, a, b in lines if abs(p - position) <= 2)
            combined = []
            for a, b in spans:
                if combined and a <= combined[-1][1] + 1.5:
                    combined[-1] = (combined[-1][0], max(combined[-1][1], b))
                else:
                    combined.append((a, b))
            result.extend((position, a, b) for a, b in combined)
        return result

    hs, vs = merged(horizontal), merged(vertical)
    if hs is None or vs is None:
        return True
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
            if len(rows) < 3:
                continue
            if any(left + 3 < x < right - 3 and min(b, rows[-1]) - max(a, rows[0]) > 6 for x, a, b in vs):
                return True
    return False


def pdf_quality(pages: list[dict]) -> dict:
    completed = [page["page"] for page in pages if page["status"] in {"native", "ocr_complete"}]
    pending = [page["page"] for page in pages if page["status"] == "pending_ocr"]
    failed = [page["page"] for page in pages if page["status"] == "ocr_failed"]
    review = [page["page"] for page in pages if page["status"] == "ocr_uncertain"]
    deferred = [
        page["page"] for page in pages if page["status"] == "pending_ocr" and page.get("deferred_reason")
    ]
    pending_parse = [page["page"] for page in pages if page["status"] == "pending_parse"]
    retry_delay = max(
        [5, *(page.get("ocr", {}).get("recovery", {}).get("retry_after_seconds", 0) for page in pages)]
    )
    complete = bool(pages) and len(completed) == len(pages)
    return {
        "status": "complete" if complete else ("needs_ocr" if pending and not completed else "partial"),
        "complete": complete,
        "page_count": len(pages),
        "completed_pages": completed,
        "pending_pages": pending,
        "pending_parse_pages": pending_parse,
        "failed_pages": failed,
        "review_pages": review,
        "review_required": bool(review),
        "deferred_pages": deferred,
        "retry_after_seconds": retry_delay if deferred else 0,
    }


def _pdf_elements(pages: list[dict]) -> list[ParsedElement]:
    elements = []
    is_case = bool(pages and re.search(r"(?mi)^\s*PUTUSAN\s*$", pages[0]["text"][:1200]))
    legal = not is_case and any(
        re.search(r"(?mi)^\s*Pasal\s+\d+[A-Za-z]?\s*\.?\s*$", page["text"]) for page in pages
    )
    section = "正文" if legal else ""
    article = ""
    for page in pages:
        text = page["text"]
        boundaries = (
            []
            if is_case
            else list(
                re.finditer(
                    r"(?mi)^[ \t]*(PENJELASAN(?:[ \t]+ATAS)?|Pasal[ \t]+\d+[A-Za-z]?[ \t]*\.?)[ \t]*$", text
                )
            )
        )
        starts = sorted({0, *(match.start() for match in boundaries), len(text)})
        markers = {match.start(): match.group(1).strip() for match in boundaries}
        for start, end in zip(starts, starts[1:]):
            marker = markers.get(start, "")
            if marker.upper().startswith("PENJELASAN"):
                section, article = "PENJELASAN", ""
            elif marker.lower().startswith("pasal"):
                article = re.sub(r"\s+", " ", marker.rstrip(". "))
            path = [value for value in (section, article) if value]
            for kind, block_start, block_end in _pdf_text_blocks(text, start, end):
                body = text[block_start:block_end].strip()
                if not body:
                    continue
                locator = {"kind": "pdf", "page": page["page"]}
                if path or kind == "table":
                    locator.update(
                        {
                            "section": section or None,
                            "article": article or None,
                            "char_start": block_start,
                            "char_end": block_end,
                        }
                    )
                if page.get("printed_page") is not None:
                    locator["printed_page"] = page["printed_page"]
                elements.append(
                    ParsedElement(
                        f"page-{page['page']:06}-part-{block_start:06}",
                        kind,
                        len(elements),
                        body,
                        locator,
                        path,
                        metadata_json={"page_status": page["status"], "source_hash": page["source_hash"]},
                    )
                )
    return elements


def _pdf_text_blocks(text: str, start: int, end: int):
    offset = start
    for match in re.finditer(r"(?m)(?:^[ \t]*\|[^\n]*\|[ \t]*\r?\n?){2,}", text[start:end]):
        lines = match[0].splitlines()
        if len(lines) < 2 or not re.fullmatch(r"\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*", lines[1]):
            continue
        table_start, table_end = start + match.start(), start + match.end()
        if table_start > offset:
            yield "paragraph", offset, table_start
        yield "table", table_start, table_end
        offset = table_end
    if offset < end:
        yield "paragraph", offset, end


def rebuild_pdf_manifest(manifest: dict) -> dict:
    from .ingestion_quality import OCR_VALIDATION_VERSION, validate_ocr_page

    pages = manifest["pages"]
    if len(pages) > MAX_PAGES or sum(len(page["text"]) for page in pages) > MAX_TEXT:
        raise ValueError("PDF 识别结果超过处理限制。")
    for page in pages:
        validate_ocr_page(page)
    document = ParsedDocument(
        manifest["parser"],
        manifest["parser_version"],
        f"pdf-pages-v4+vision-ocr-v2+{OCR_VALIDATION_VERSION}",
        _pdf_elements(pages),
        [],
        manifest["warnings"],
        pages,
    )
    return _parsed_result(document, manifest["source_hash"], manifest["title"])


def parse_file(
    filename: str, content: bytes, mime_type: str = "", *, role: str = "primary", parser: str = "basic"
) -> dict:
    if role != "primary":
        raise ValueError("secondary 文档仅保存原件，禁止解析或建立索引。")
    if not content or len(content) > MAX_BYTES:
        raise ValueError("文档不能为空且不能超过 20 MB。")
    extension = Path(filename).suffix.lower()
    if not extension:
        extension = _MIME_EXTENSIONS.get(mime_type.split(";")[0].strip().lower(), "")
        filename += extension
    supported = {".txt", ".md", ".markdown", ".html", ".htm", ".docx", ".pdf"}
    if extension not in supported:
        raise ValueError("支持 PDF、DOCX、HTML、TXT 和 Markdown；旧版 DOC 请转换为 DOCX。")
    if parser == "docling":
        # Explicit opt-in prevents accidental model downloads on a basic installation.
        if not os.environ.get("DOCLING_MODELS_PATH"):
            raise ValueError("Docling 需要配置已安装模型目录 DOCLING_MODELS_PATH。")
        try:
            document = DoclingDocumentParser(document_timeout_seconds=90).parse(
                filename=filename, mime_type=mime_type, content=content
            )
        except ImportError as exc:
            raise ValueError("Docling 可选依赖尚未安装。") from exc
        if document.assets:
            document = replace(
                document, assets=[], warnings=[*document.warnings, "图像尚未经过视觉核验，未用于检索或引用。"]
            )
    elif parser != "basic":
        raise ValueError("未知解析器，仅支持 basic 或 docling。")
    elif extension in {".txt", ".md", ".markdown"}:
        if len(content) > MAX_TEXT:
            raise ValueError("文本超过处理限制。")
        document = _parse_native_text(filename=filename, content=content)
    elif extension in {".html", ".htm"}:
        document = _parse_html(content)
    elif extension == ".docx":
        document = _parse_docx(content)
    else:
        document = _parse_pdf(content)
    if (not document.elements and not document.pages) or sum(
        len(x.content_text) for x in document.elements
    ) > MAX_TEXT:
        raise ValueError("文档没有可解析的正文，或正文超过处理限制。")
    return _parsed_result(document, hashlib.sha256(content).hexdigest(), Path(filename).stem)


def _parsed_result(document: ParsedDocument, source_hash: str, title: str) -> dict:
    manifest, _ = build_manifest(document, source_hash=source_hash, document_title=title)
    if not manifest["chunks"] and not document.pages:
        raise ValueError("文档仅含标题或图片，没有可供检索的正文。")
    by_key = {element.element_key: element for element in document.elements}
    chunks = []
    for chunk in manifest["chunks"]:
        paths = [by_key[key].section_path for key in chunk["element_keys"] if key in by_key]
        chunks.append(
            {
                "id": chunk["chunk_key"],
                "text": chunk["content_text"],
                "section_path": paths[0] if paths else [],
                "locator": chunk["locator"],
                "retrieval_text": chunk["retrieval_text"],
                "content_hash": chunk["content_hash"],
            }
        )
    return {
        "parser": document.parser_name,
        "parser_version": document.parser_version,
        "pipeline_version": manifest["pipeline_version"],
        "source_hash": manifest["source_hash"],
        "title": next((e.content_text for e in document.elements if e.kind in {"title", "heading"}), title),
        "elements": manifest["elements"],
        "chunks": chunks,
        "warnings": document.warnings,
        "retrieval_mode": "lexical",
        **({"pages": document.pages, "quality": pdf_quality(document.pages)} if document.pages else {}),
    }


def _terms(text: str) -> list[str]:
    normalized = text.casefold()
    result = re.findall(r"[a-z0-9]+", normalized)
    for segment in re.findall(r"[\u3400-\u9fff]+", normalized):
        result.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        if len(segment) == 1:
            result.append(segment)
    return result


def lexical_search(query: str, chunks: list[dict], limit: int = 5) -> list[dict]:
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("检索数量应为 1 至 50。")
    if len(query) > 4000:
        raise ValueError("检索问题过长。")
    query_terms = set(_terms(query))
    if not query_terms or not chunks:
        return []
    terms = [Counter(_terms(str(chunk.get("retrieval_text") or chunk.get("text") or ""))) for chunk in chunks]
    average = sum(sum(counts.values()) for counts in terms) / len(terms) or 1
    frequencies = {term: sum(term in counts for counts in terms) for term in query_terms}
    hits = []
    for chunk, counts in zip(chunks, terms, strict=True):
        score = 0.0
        length = sum(counts.values())
        for term in query_terms:
            frequency = counts[term]
            if frequency:
                inverse = math.log(1 + (len(chunks) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / average))
        if score > 0:
            hits.append({**chunk, "score": round(score, 6), "retrieval_mode": "lexical"})
    return sorted(hits, key=lambda item: item["score"], reverse=True)[:limit]


def embedding_batch_size() -> int:
    """Tune provider throughput without changing chunk contents or vector identity."""
    value = os.getenv("EMBEDDING_BATCH_SIZE", "64").strip()
    if not value.isdigit() or not 1 <= int(value) <= 64:
        raise ValueError("EMBEDDING_BATCH_SIZE 必须为 1 至 64 的整数。")
    return int(value)


def embed_texts(texts: list[str]) -> list[list[float]] | None:
    base = os.environ.get("EMBEDDING_BASE_URL", "").strip()
    key = os.environ.get("EMBEDDING_API_KEY", "").strip()
    model = os.environ.get("EMBEDDING_MODEL", "").strip()
    if not any((base, key, model)):
        return None
    if not all((base, key, model)):
        raise ValueError("必须同时配置 EMBEDDING_BASE_URL、EMBEDDING_API_KEY 和 EMBEDDING_MODEL。")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("EMBEDDING_BASE_URL 无效。")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("远程 embedding 服务必须使用 HTTPS。")
    if not texts:
        return []
    if len(texts) > 10000 or any(not isinstance(text, str) or len(text) > 20000 for text in texts):
        raise ValueError("Embedding 输入超过处理限制。")
    import httpx

    vectors: list[list[float]] = []
    dimension = None
    batch_size = embedding_batch_size()
    with httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False, trust_env=False
    ) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            try:
                with client.stream(
                    "POST",
                    base.rstrip("/") + "/embeddings",
                    json={"model": model, "input": batch, "encoding_format": "float"},
                    headers={"Authorization": f"Bearer {key}"},
                ) as response:
                    response.raise_for_status()
                    payload = bytearray()
                    for block in response.iter_bytes():
                        payload.extend(block)
                        if len(payload) > 16 * 1024 * 1024:
                            raise ValueError("Embedding 服务响应超过 16 MB。")
                    body = json.loads(payload)
            except (httpx.HTTPError, ValueError) as exc:
                raise ValueError("Embedding 服务请求失败，请检查服务配置或稍后重试。") from exc
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list) or len(data) != len(batch):
                raise ValueError("Embedding 服务返回数量与输入不一致。")
            if {entry.get("index") for entry in data if isinstance(entry, dict)} != set(range(len(batch))):
                raise ValueError("Embedding 服务返回索引无效。")
            for entry in sorted(data, key=lambda item: item["index"]):
                vector = entry.get("embedding")
                if (
                    not isinstance(vector, list)
                    or not vector
                    or len(vector) > 65536
                    or any(
                        isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
                        for n in vector
                    )
                ):
                    raise ValueError("Embedding 服务返回无效向量。")
                if not any(vector):
                    raise ValueError("Embedding 服务返回零向量。")
                dimension = dimension or len(vector)
                if len(vector) != dimension:
                    raise ValueError("Embedding 向量维度不一致。")
                vectors.append([float(value) for value in vector])
    return vectors


class DownloadError(ValueError):
    """A bounded collection failure; the durable job runner owns retry policy."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        response_host: str | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after if retryable else None
        self.response_host = response_host


def _validated_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\x7f]", url):
        raise DownloadError("抓取 URL 无效。")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise DownloadError("抓取仅支持不含凭据的标准端口 HTTPS URL。")
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError("抓取 URL 无效。") from exc
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise DownloadError("抓取地址必须是 public 公网地址。")
    netloc = f"[{host}]" if ":" in host else host
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&%/:?@!$'()*+,;~-._")
    return urlunsplit(("https", netloc, path, query, "")), host


def _public_addresses(host: str, *, deadline: float | None = None) -> list[tuple]:
    # System DNS has no Python timeout. Bound waiting and outstanding resolver
    # threads so a resolver outage cannot exhaust application worker threads.
    remaining = min(10.0, (deadline or time.monotonic() + 10) - time.monotonic())
    if remaining <= 0 or not _DNS_SLOTS.acquire(timeout=max(0, remaining)):
        raise DownloadError("DNS 解析超过时间限制或队列已满。", retryable=True)
    result: queue.Queue = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except Exception as exc:
            result.put(exc)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, name="tea-source-dns", daemon=True).start()
    try:
        addresses = result.get(
            timeout=max(0, min(10.0, (deadline or time.monotonic() + 10) - time.monotonic()))
        )
    except queue.Empty as exc:
        raise DownloadError("DNS 解析超过时间限制。", retryable=True) from exc
    if isinstance(addresses, Exception):
        raise DownloadError("抓取来源域名无法解析。", retryable=True) from addresses
    if not addresses:
        raise DownloadError("抓取来源没有可用地址。", retryable=True)
    for address in addresses:
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise DownloadError("抓取来源的所有 DNS 地址必须是 public 公网地址。")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect exactly to a validated address; TLS still authenticates the hostname."""

    def __init__(self, host: str, address: tuple, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address
        self.alternate_addresses: list[tuple] = []
        self.active_socket = None
        self.deadline: float | None = None
        self.deadline_expired = threading.Event()

    def _remaining_timeout(self) -> float:
        remaining = self.timeout if self.deadline is None else self.deadline - time.monotonic()
        if self.deadline_expired.is_set() or remaining <= 0:
            raise TimeoutError("Collection deadline expired")
        return min(self.timeout, remaining)

    def connect(self) -> None:
        self._remaining_timeout()
        connect_deadline = time.monotonic() + self._remaining_timeout()
        addresses = [self.address, *self.alternate_addresses]
        failure = None
        for family, socket_type, protocol, _, destination in addresses:
            raw = tls = None
            connected = False

            def timeout():
                remaining = min(self._remaining_timeout(), connect_deadline - time.monotonic(), 5.0)
                if remaining <= 0:
                    raise TimeoutError("Collection connection deadline expired")
                return remaining

            try:
                timeout()
                raw = socket.socket(family, socket_type, protocol)
                # Only one physical socket is active at a time; every retry
                # stays inside the same host lease and absolute watchdog.
                self.active_socket = raw
                raw.settimeout(timeout())
                raw.connect(destination)
                raw.settimeout(timeout())
                tls = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
                self.sock = self.active_socket = tls
                tls.settimeout(timeout())
                tls.do_handshake()
                tls.settimeout(self._remaining_timeout())
                connected = True
                return
            except OSError as exc:
                failure = exc
            finally:
                if not connected:
                    if tls is not None:
                        tls.close()
                    if raw is not None:
                        raw.close()
                    self.sock = self.active_socket = None
        if failure is not None:
            raise failure
        raise TimeoutError("No validated address connected before deadline")


def _bounded_header(value: str | None, limit: int = 1024) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(char) < 32 or ord(char) == 127 or ord(char) > 255 for char in value):
        return None
    return value


def _retry_after(value: str | None) -> float | None:
    """RFC 9110 section 10.2.3: delay-seconds or HTTP-date, capped locally."""
    value = _bounded_header(value, 128)
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return min(300.0, float(value))
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return min(300.0, max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None


def _download(
    url: str,
    *,
    deadline: float,
    required_host: str | None = None,
    consume: Callable[[Iterable[bytes]], object] | None = None,
    validators: dict[str, str] | None = None,
    connection_slot: Callable[[str], AbstractContextManager] | None = None,
    accept_compressed_html: bool = False,
    accept_compressed_json: bool = False,
    json_body: dict | None = None,
    max_bytes: int | None = None,
    byte_range: tuple[int, int] | None = None,
    range_validator: str | None = None,
    on_headers: Callable[[dict], None] | None = None,
) -> dict:
    """Read a bounded HTTPS resource, optionally into an atomic blob consumer.

    A consumer must exhaust the iterable and only publish after normal completion.
    A 304 does not invoke it: callers must validate their cached original and URL.
    Conditional headers belong exclusively to the initial requested resource.
    A connection slot accounts for the actual host at every redirect hop.
    """
    current = url
    validators = validators or {}
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ORIGINAL_BYTES:
        raise DownloadError("原件传输额度无效，最多 128 MB。")
    if byte_range is not None:
        if (
            not isinstance(byte_range, tuple)
            or len(byte_range) != 2
            or any(type(value) is not int for value in byte_range)
            or not 0 <= byte_range[0] <= byte_range[1] < max_bytes
            or validators
            or json_body is not None
            or accept_compressed_html
            or accept_compressed_json
        ):
            raise DownloadError("原件分段请求参数无效。")
        if range_validator is not None and not re.fullmatch(r'"[\x21\x23-\x7e]{0,1020}"', range_validator):
            raise DownloadError("分段原件需要有效的强版本标识。")
    elif range_validator is not None:
        raise DownloadError("原件分段校验参数缺少范围。")
    request_body = None
    if json_body is not None:
        if not isinstance(json_body, dict) or validators:
            raise DownloadError("目录 JSON 请求参数无效。")
        try:
            request_body = json.dumps(json_body, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError) as exc:
            raise DownloadError("目录 JSON 请求参数无效。") from exc
        if len(request_body) > 16384:
            raise DownloadError("目录 JSON 请求超过 16 KB。")
    conditionals = {
        "If-None-Match": _bounded_header(validators.get("etag") or validators.get("ETag")),
        "If-Modified-Since": _bounded_header(
            validators.get("last_modified") or validators.get("Last-Modified"), 128
        ),
    }
    for redirect in range(4):
        current, host = _validated_url(current)
        if required_host is not None and host != required_host:
            raise DownloadError("关联附件重定向到了不同来源，已拒绝抓取。")
        # Some official domains advertise IPv6 without a reachable route from
        # this host. All answers are validated before ordering or connecting.
        addresses = sorted(
            _public_addresses(host, deadline=deadline), key=lambda row: row[0] != socket.AF_INET
        )
        with connection_slot(host) if connection_slot else nullcontext():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadError("抓取超过总时间限制。", retryable=True)
            connection = _PinnedHTTPSConnection(host, addresses[0], min(15.0, remaining))
            connection.alternate_addresses = addresses[1:]
            connection.deadline = deadline
            expired = threading.Event()
            connection.deadline_expired = expired

            def expire_connection(connection=connection, expired=expired) -> None:
                # Bind this hop's socket and flag: a late timer from an earlier
                # redirect must never close the next hop's connection.
                expired.set()
                active = getattr(connection, "active_socket", None)
                if active is not None:
                    try:
                        active.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        active.close()
                    except OSError:
                        pass

            expiry = threading.Timer(max(0, deadline - time.monotonic()), expire_connection)
            expiry.daemon = True
            expiry.start()
            response = None
            try:
                if expired.is_set() or deadline <= time.monotonic():
                    raise DownloadError("抓取超过总时间限制。", retryable=True)
                parsed = urlsplit(current)
                headers = {
                    "User-Agent": "AItea/0.1 SourceCollector",
                    "Accept-Encoding": "gzip, identity"
                    if accept_compressed_html or accept_compressed_json
                    else "identity",
                    "Accept": "application/pdf,text/html,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,*/*;q=0.5",
                }
                headers.update({key: value for key, value in conditionals.items() if value is not None})
                if byte_range is not None:
                    headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
                    if range_validator is not None:
                        headers["If-Range"] = range_validator
                request_path = parsed.path + ("?" + parsed.query if parsed.query else "")
                if request_body is None:
                    connection.request("GET", request_path, headers=headers)
                else:
                    headers["Content-Type"] = "application/json"
                    connection.request("POST", request_path, headers=headers, body=request_body)
                remaining = deadline - time.monotonic()
                if expired.is_set() or remaining <= 0:
                    raise DownloadError("抓取超过总时间限制。", retryable=True)
                if getattr(connection, "sock", None):
                    connection.sock.settimeout(min(15.0, remaining))
                response = connection.getresponse()
                if expired.is_set() or deadline <= time.monotonic():
                    raise DownloadError("抓取超过总时间限制。", retryable=True)
                if response.status in {301, 302, 303, 307, 308}:
                    if request_body is not None:
                        raise DownloadError("目录 JSON 请求不允许重定向。")
                    location = response.getheader("Location")
                    if not location or redirect == 3:
                        raise DownloadError("抓取来源重定向过多或地址缺失。")
                    # Even on the same host, another path represents a different
                    # cached resource. Do not leak or reuse the original validators.
                    conditionals = {}
                    range_validator = None
                    current = urljoin(current, location)
                    continue
                response_validators = {
                    "etag": _bounded_header(response.getheader("ETag")),
                    "last_modified": _bounded_header(response.getheader("Last-Modified"), 128),
                }
                if response.status == 304:
                    if not (headers.get("If-None-Match") or headers.get("If-Modified-Since")):
                        raise DownloadError("来源对无条件请求返回 304，无法校验本地原件。")
                    return {"url": current, "not_modified": True, **response_validators}
                if not 200 <= response.status < 300:
                    retryable = response.status in {408, 429} or 500 <= response.status <= 599
                    raise DownloadError(
                        f"抓取来源返回 HTTP {response.status}。",
                        retryable=retryable,
                        retry_after=_retry_after(response.getheader("Retry-After")) if retryable else None,
                        response_host=host,
                    )
                if response.status == 206 and byte_range is None:
                    raise DownloadError("抓取来源返回了不完整的分段响应。")
                encoding = response.getheader("Content-Encoding", "identity").strip().lower()
                compressed = (accept_compressed_html or accept_compressed_json) and encoding == "gzip"
                if encoding not in {"identity", ""} and not compressed:
                    raise DownloadError("抓取来源返回压缩传输，无法安全校验大小。")
                transfer_limit = min(max_bytes, MAX_COMPRESSED_HTML_BYTES) if compressed else max_bytes
                length = response.getheader("Content-Length")
                if length is not None and (
                    not re.fullmatch(r"[0-9]{1,12}", length.strip()) or int(length) > transfer_limit
                ):
                    raise DownloadError("抓取文件大小无效或超过配置的原件额度。")
                expected_length = int(length) if length is not None else None
                range_start = range_end = None
                total_size = expected_length
                if response.status == 206:
                    match = re.fullmatch(
                        r"bytes (\d{1,12})-(\d{1,12})/(\d{1,12})", response.getheader("Content-Range", "")
                    )
                    if match is None:
                        raise DownloadError("分段响应缺少有效的完整文件范围。")
                    range_start, range_end, total_size = map(int, match.groups())
                    if (
                        not 0 <= range_start <= range_end < total_size <= max_bytes
                        or range_start != byte_range[0]
                        or range_end != min(byte_range[1], total_size - 1)
                        or (expected_length is not None and expected_length != range_end - range_start + 1)
                    ):
                        raise DownloadError("分段响应范围或总大小与请求不一致。")
                    expected_length = range_end - range_start + 1
                    transfer_limit = expected_length
                mime = (
                    response.getheader("Content-Type", "application/octet-stream")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if len(mime) > 255 or not re.fullmatch(r"[a-z0-9!#$&^_.+*-]+/[a-z0-9!#$&^_.+*-]+", mime):
                    raise DownloadError("抓取来源返回无效的文件类型。")
                if compressed and not (
                    (accept_compressed_html and mime in {"text/html", "application/xhtml+xml"})
                    or (accept_compressed_json and mime == "application/json")
                ):
                    raise DownloadError("仅清单目录或详情 HTML/JSON 允许受限压缩传输。")
                name = Path(unquote(parsed.path)).name or "source"
                disposition = _bounded_header(response.getheader("Content-Disposition"), 4096)
                if disposition:
                    try:
                        message = EmailMessage()
                        message["Content-Disposition"] = disposition
                        declared_name = message.get_filename()
                        if isinstance(declared_name, str) and declared_name.strip():
                            name = Path(declared_name.replace("\\", "/")).name or name
                    except (ValueError, LookupError):
                        pass  # A malformed display header cannot change stored bytes.
                name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name)[:180]
                if not Path(name).suffix or (
                    mime in _MIME_EXTENSIONS
                    and Path(name).suffix.lower() != _MIME_EXTENSIONS[mime]
                    and not (mime == "text/html" and Path(name).suffix.lower() == ".htm")
                ):
                    name += _MIME_EXTENSIONS.get(mime, mimetypes.guess_extension(mime) or ".bin")
                exhausted = False
                transfer_bytes = 0
                metadata = {
                    "url": current,
                    "filename": name,
                    "mime_type": mime,
                    "content_encoding": encoding or "identity",
                    "request_method": "POST" if request_body is not None else "GET",
                    "response_status": response.status,
                    "total_size": total_size,
                    "range_start": range_start,
                    "range_end": range_end,
                    "accept_ranges": response.getheader("Accept-Ranges", "").strip().lower() == "bytes",
                    **response_validators,
                }
                if on_headers is not None:
                    on_headers(metadata.copy())

                def chunks() -> Iterable[bytes]:
                    nonlocal exhausted, transfer_bytes
                    size = decoded_size = 0
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if compressed else None
                    while True:
                        remaining = deadline - time.monotonic()
                        if expired.is_set() or remaining <= 0:
                            raise DownloadError("抓取超过总时间限制。", retryable=True)
                        if connection.sock:
                            connection.sock.settimeout(min(15.0, remaining))
                        block = response.read1(min(65536, transfer_limit + 1 - size))
                        if expired.is_set() or deadline <= time.monotonic():
                            raise DownloadError("抓取超过总时间限制。", retryable=True)
                        if not block:
                            break
                        size += len(block)
                        if size > transfer_limit:
                            raise DownloadError("抓取文件超过传输大小限制。")
                        if expected_length is not None and size > expected_length:
                            raise DownloadError("抓取文件长度与响应头不一致。", retryable=True)
                        if decoder is None:
                            yield block
                        else:
                            # Bound each allocation and total decoded bytes; never call
                            # flush(), whose length is an initial buffer, not a limit.
                            pending = block
                            while True:
                                if expired.is_set() or time.monotonic() >= deadline:
                                    raise DownloadError("抓取超过总时间限制。", retryable=True)
                                try:
                                    decoded = decoder.decompress(
                                        pending, min(65536, transfer_limit + 1 - decoded_size)
                                    )
                                except zlib.error as exc:
                                    raise DownloadError("压缩目录内容损坏。", retryable=True) from exc
                                decoded_size += len(decoded)
                                if decoded_size > transfer_limit:
                                    raise DownloadError("目录解压后超过 4 MB。")
                                if decoder.unused_data:
                                    raise DownloadError("压缩目录存在尾随或多段内容。")
                                if decoded:
                                    yield decoded
                                pending = decoder.unconsumed_tail
                                if not pending and (not decoded or decoder.eof):
                                    break
                    # These checks run before the consumer can atomically publish.
                    if expected_length is not None and size != expected_length:
                        raise DownloadError("抓取文件传输不完整。", retryable=True)
                    if not size:
                        raise DownloadError("抓取文件为空。")
                    if decoder is not None and (not decoder.eof or not decoded_size):
                        raise DownloadError("压缩目录传输不完整或正文为空。", retryable=True)
                    transfer_bytes = size
                    exhausted = True

                payload = consume(chunks()) if consume is not None else b"".join(chunks())
                if not exhausted:
                    raise DownloadError("文件存储未完整消费抓取数据。")
                return {
                    **metadata,
                    "transfer_bytes": transfer_bytes,
                    "blob" if consume is not None else "content": payload,
                }
            except (OSError, http.client.HTTPException) as exc:
                raise DownloadError("抓取连接失败或超时，请检查来源后重试。", retryable=True) from exc
            finally:
                expiry.cancel()
                try:
                    # For Connection: close, HTTPConnection hands the socket
                    # to the response file. Close it before releasing this
                    # host's physical connection slot, including early exits.
                    if response is not None:
                        response.close()
                finally:
                    connection.close()
    raise DownloadError("抓取重定向无法完成。")


def discover_attachments(url: str, content: bytes, mime_type: str, max_attachments: int = 5) -> list[str]:
    """Find direct file attachments without fetching them or inferring citations."""
    if (
        isinstance(max_attachments, bool)
        or not isinstance(max_attachments, int)
        or not 0 <= max_attachments <= 5
    ):
        raise ValueError("直接附件数量必须为 0 至 5。")
    if mime_type != "text/html" or not max_attachments or len(content) > MAX_TEXT:
        return []
    current, host = _validated_url(url)
    extractor = _HTMLDocument()
    extractor.feed(_decode(content))
    extractor.finish()
    candidates: list[str] = []
    seen: set[str] = set()
    for link in extractor.links:
        try:
            candidate, candidate_host = _validated_url(urljoin(current, link))
        except ValueError:
            continue
        if (
            candidate_host != host
            or Path(unquote(urlsplit(candidate).path)).suffix.lower() not in _DIRECT_EXTENSIONS
            or candidate in seen
        ):
            continue
        seen.add(candidate)
        candidates.append(candidate)
        if len(candidates) >= max_attachments:
            break
    return candidates


def fetch_source(url: str, *, max_attachments: int = 5) -> dict:
    if not isinstance(max_attachments, int) or not 0 <= max_attachments <= 5:
        raise ValueError("直接附件数量必须为 0 至 5。")
    deadline = time.monotonic() + 90.0
    primary = _download(url, deadline=deadline)
    attachments = []
    warnings: list[str] = []
    if primary["mime_type"] == "text/html" and max_attachments:
        if len(primary["content"]) > MAX_TEXT:
            return {
                **primary,
                "attachments": [],
                "warnings": ["HTML 超过附件发现大小限制，已仅保存主文件。"],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        host = urlsplit(primary["url"]).hostname
        for candidate in discover_attachments(
            primary["url"], primary["content"], primary["mime_type"], max_attachments
        ):
            try:
                attachments.append(_download(candidate, deadline=deadline, required_host=host))
            except ValueError as exc:
                warnings.append(f"附件未抓取：{candidate}；{exc}")
    return {
        **primary,
        "attachments": attachments,
        "warnings": warnings,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
