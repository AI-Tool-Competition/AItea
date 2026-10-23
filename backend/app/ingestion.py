"""Pure document ingestion, grounded retrieval and bounded HTTPS collection.

Call these blocking functions from a worker, never from the API event loop.
Business ownership, version persistence and access control belong to the service.
"""

from __future__ import annotations

import hashlib
import base64
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
import uuid
import zlib
from collections import Counter, deque
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from .html_parsing import HTMLDocument as _HTMLDocument
from .pdf_table_detection import VERSION as _TABLE_LAYOUT_VERSION
from .public_search import EGazetteSearchRequest
from .vendor.geo_parser.document_ingestion import (
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
    "application/vnd.ms-excel": ".xls",
    "application/x-msaccess": ".mdb",
    "application/vnd.ms-access": ".mdb",
    "application/msaccess": ".mdb",
    "application/msword": ".doc",
    "application/vnd.wordperfect": ".wpf",
    "application/wordperfect": ".wpf",
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


def _parse_html(content: bytes) -> ParsedDocument:
    source = _decode(content)
    if len(source) > MAX_TEXT:
        raise ValueError("HTML 文本超过处理限制。")
    from importlib.metadata import version
    from selectolax.lexbor import LexborHTMLParser

    # Lexbor supplies a mature HTML5 tree for malformed-but-recoverable source.
    # We still build legal-source structure from the original decoded bytes so
    # citations retain exact source offsets instead of pointing into a rewrite.
    tree = LexborHTMLParser(source, scripting=False)
    nodes = tree.css("*")
    if len(nodes) > 100_000:
        raise ValueError("HTML 标签数量超过处理限制。")
    max_depth = 0
    for node in nodes:
        depth, parent = 0, node.parent
        while parent is not None:
            depth += 1
            if depth > 128:
                raise ValueError("HTML 嵌套深度超过处理限制。")
            parent = parent.parent
        max_depth = max(max_depth, depth)
    dom_validation = {
        "engine": "selectolax.lexbor",
        "engine_version": version("selectolax"),
        "html5_tree_built": True,
        "scripts_executed": False,
        "node_count": len(nodes),
        "max_depth": max_depth,
        "main_count": sum(node.tag == "main" for node in nodes),
        "article_count": sum(node.tag == "article" for node in nodes),
        "table_count": sum(node.tag == "table" for node in nodes),
        "heading_count": sum(node.tag in {f"h{i}" for i in range(1, 7)} for node in nodes),
    }
    options = {
        "source": source,
        "source_hash": hashlib.sha256(content).hexdigest(),
        "exclude_navigation": True,
        "max_elements": MAX_ELEMENTS,
        "dom_validation": dom_validation,
    }
    extractor = _HTMLDocument(main_only=True, **options)
    extractor.feed(source)
    extractor.finish()
    if not extractor.has_main:
        # A main marker in a comment, script or hidden subtree is not document structure.
        extractor = _HTMLDocument(**options)
        extractor.feed(source)
        extractor.finish()
    return extractor.document()


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
            if "\ufffd" in text:
                reasons.append("native_decode_replacement")
            if _pdf_large_raster(page):
                reasons.append("large_raster")
            if _pdf_table_layout(stream, page.get("/Resources")):
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
        if pages:
            import pypdfium2 as pdfium
            from .parsing import _render_pdf_page

            with pdfium.PdfDocument(content) as document:
                for page in pages:
                    pdfpage = document[page["page"] - 1]
                    try:
                        _audit_native_pdf_page(
                            page,
                            pdfpage,
                            optional_content="/OCProperties" in reader.trailer["/Root"],
                            render_blank=lambda number: _render_pdf_page(content, number),
                        )
                    finally:
                        pdfpage.close()
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
        "pypdf", version("pypdf"), "pdf-pages-v5", _pdf_elements(pages), [], warnings, pages
    )


def _audit_native_pdf_page(page, pdfpage, *, optional_content, render_blank):
    """Keep native evidence; only visible text or proven white pages can pass."""
    from .pdf_tables import VERSION, extract_grid
    from .pdf_text_visibility import VERSION as VISIBILITY_VERSION, audit_page_visibility, prove_blank_page

    if "large_raster" in page["reasons"]:
        # A scan already requires full-page OCR. Its noisy text layer cannot
        # acquire native acceptance, so expanding thousands of painting/text
        # objects adds neither acceptance evidence nor useful source coverage.
        audit = {
            "version": VISIBILITY_VERSION,
            "source_hash": page["source_hash"],
            "page": page["page"],
            "native_text_sha256": hashlib.sha256(page["native_text"].encode()).hexdigest(),
            "native_text_field": "pages[].native_text",
            "audit_scope": "not_inspected_raster_page_already_pending_ocr",
            "visibility_complete_certified": False,
            "requires_visual_review": True,
            "reasons": [],
            "unknown_reasons": ["raster_native_visibility_not_audited"],
            "evidence": [],
        }
    else:
        audit = audit_page_visibility(
            pdfpage,
            page["native_text"],
            source_hash=page["source_hash"],
            page_number=page["page"],
            optional_content=optional_content,
        )
    page["native_visibility"] = audit
    if audit["requires_visual_review"]:
        page["reasons"].append("native_visibility_review")
        page.update(status="pending_ocr", ocr_required=True)
    if page["reasons"] == ["empty_text"]:
        proof = prove_blank_page(render_blank(page["page"]), audit, native_text=page["native_text"])
        if proof is not None:
            # Persist the pixels and physical source locator in the manifest
            # before any complete/indexing decision is possible.
            proof["image_size"] = len(base64.b64decode(proof["image_base64"], validate=True))
            page.update(native_blank=proof, status="native", ocr_required=False, reasons=[])
    if page["reasons"] == ["table_layout"]:
        page["native_grid_attempt"] = VERSION
        table = extract_grid(pdfpage, page["native_text"])
        if table:
            page.update(
                text=table.pop("text"),
                status="native",
                ocr_required=False,
                reasons=[],
                native_table={**table, "source_hash": page["source_hash"], "page": page["page"]},
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


TABLE_LAYOUT_VERSION = _TABLE_LAYOUT_VERSION


def _pdf_table_layout(stream, resources=None) -> bool:
    """Actual painted grid evidence in page coordinates, including Form scopes."""
    from .pdf_table_detection import requires_table_review

    return requires_table_review(stream, resources)


def pdf_quality(pages: list[dict]) -> dict:
    from .ingestion_quality import pdf_page_status

    statuses = {page["page"]: pdf_page_status(page) for page in pages}
    completed = [page["page"] for page in pages if statuses[page["page"]] in {"native", "ocr_complete"}]
    pending = [page["page"] for page in pages if statuses[page["page"]] == "pending_ocr"]
    failed = [page["page"] for page in pages if statuses[page["page"]] == "ocr_failed"]
    review = [page["page"] for page in pages if statuses[page["page"]] == "ocr_uncertain"]
    source_incomplete = [page["page"] for page in pages if statuses[page["page"]] == "source_incomplete"]
    deferred = [
        page["page"]
        for page in pages
        if statuses[page["page"]] == "pending_ocr" and page.get("deferred_reason")
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
        "source_incomplete_pages": source_incomplete,
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
    from .ingestion_quality import (
        OCR_VALIDATION_VERSION,
        PDF_VISIBILITY_PIPELINES,
        pdf_source_bindings_valid,
        validate_ocr_page,
    )

    pages = manifest["pages"]
    if len(pages) > MAX_PAGES or sum(len(page["text"]) for page in pages) > MAX_TEXT:
        raise ValueError("PDF 识别结果超过处理限制。")
    visibility_required = str(manifest.get("pipeline_version", "")).split("+", 1)[
        0
    ] in PDF_VISIBILITY_PIPELINES or any("native_visibility" in page for page in pages)
    if visibility_required:
        if not pdf_source_bindings_valid(manifest):
            raise ValueError("PDF 检查点原件哈希或物理页码不匹配，请重新解析原件。")
        if any(page.get("status") == "native" and "native_visibility" not in page for page in pages):
            raise ValueError("PDF 原生可见性检查点缺失，请重新解析原件。")
    for page in pages:
        validate_ocr_page(page)
    document = ParsedDocument(
        manifest["parser"],
        manifest["parser_version"],
        f"pdf-pages-v{5 if visibility_required else 4}+vision-ocr-v2+{OCR_VALIDATION_VERSION}",
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
    from .mineru import supports, pending_manifest

    extension = Path(filename).suffix.lower()
    if extension in {".html", ".htm"} or mime_type.split(";", 1)[0].strip().lower() == "text/html":
        document = _parse_html(content)
        return _parsed_result(document, hashlib.sha256(content).hexdigest(), Path(filename).stem)
    if supports(filename, mime_type):
        return pending_manifest(filename, hashlib.sha256(content).hexdigest())
    if extension not in {".txt", ".md", ".markdown"}:
        raise ValueError("此文件格式未获 MinerU 精准解析支持，请提供 PDF、Office、图片或 HTML 原件。")
    if len(content) > MAX_TEXT:
        raise ValueError("文本超过处理限制。")
    document = _parse_native_text(filename=filename, content=content)
    html_source = getattr(document, "html_source", {})
    if (not document.elements and not document.pages and not html_source.get("objects")) or sum(
        len(x.content_text) for x in document.elements
    ) > MAX_TEXT:
        raise ValueError("文档没有可解析的正文，或正文超过处理限制。")
    return _parsed_result(document, hashlib.sha256(content).hexdigest(), Path(filename).stem)


def _parsed_result(document: ParsedDocument, source_hash: str, title: str) -> dict:
    manifest, _ = build_manifest(document, source_hash=source_hash, document_title=title)
    html_source = getattr(document, "html_source", {})
    if not manifest["chunks"] and not document.pages and not html_source.get("objects"):
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
        **({"html_source": html_source, "quality": document.quality} if html_source else {}),
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


_EMBEDDING_PROFILES: dict[str, dict] = {}
_EMBEDDING_PROFILE_LOCK = threading.Lock()
_EMBEDDING_PROFILE_LIMIT = 32
_EMBEDDING_PRIORITY = ContextVar("embedding_priority", default="bulk")
_EMBEDDING_WAIT = ContextVar("embedding_wait", default=None)


@contextmanager
def embedding_priority(priority):
    """Only an explicit interactive query gets priority over document indexing."""
    if priority not in {"query", "bulk"}:
        raise ValueError("Invalid embedding request priority")
    token = _EMBEDDING_PRIORITY.set(priority)
    try:
        yield
    finally:
        _EMBEDDING_PRIORITY.reset(token)


@contextmanager
def _embedding_wait(priority, *, defer=None):
    # One ticket survives the 250 ms cancellation/cooldown polls. Reticketing
    # every poll would let newer arrivals repeatedly overtake a long document.
    request = {"priority": priority, "token": object(), "cleanup": None, "defer": defer}
    token = _EMBEDDING_WAIT.set(request)
    try:
        yield
    finally:
        if request["cleanup"]:
            request["cleanup"]()
        _EMBEDDING_WAIT.reset(token)


def embedding_http_worker_limit() -> int:
    value = os.getenv("EMBEDDING_HTTP_WORKERS", "2").strip()
    if not value.isdigit() or not 1 <= int(value) <= 64:
        raise ValueError("EMBEDDING_HTTP_WORKERS 必须为 1 至 64 的整数。")
    return int(value)


def embedding_http_timeout() -> int:
    value = os.getenv("EMBEDDING_HTTP_TIMEOUT_SECONDS", "30").strip()
    if not value.isdigit() or not 1 <= int(value) <= 60:
        raise ValueError("EMBEDDING_HTTP_TIMEOUT_SECONDS 必须为 1 至 60 的整数。")
    return int(value)


def embedding_retry_ambiguous_timeouts() -> bool:
    value = os.getenv("EMBEDDING_RETRY_AMBIGUOUS_TIMEOUTS", "true").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("EMBEDDING_RETRY_AMBIGUOUS_TIMEOUTS 必须为 true 或 false。")
    return value == "true"


class _EmbeddingSlots:
    """Shared query priority with reserved query slots and a sustained bulk floor."""

    def __init__(self):
        self.condition = threading.Condition()
        self.limit = None
        self.active = {"query": 0, "bulk": 0}
        self.queues = {"query": deque(), "bulk": deque()}
        self.waiters = {}
        self.held = threading.local()
        self.peak = self.query_streak = 0
        self.grants = {"query": 0, "bulk": 0}
        self.max_wait = {"query": 0.0, "bulk": 0.0}
        self.shared = self.shared_configuration = None
        self.shared_held = threading.local()

    def _shared(self):
        path = os.getenv("EMBEDDING_ADMISSION_PATH", "").strip()
        if not path:
            if self.shared_configuration:
                raise ValueError("Shared embedding admission path changed; restart this client")
            return None
        from .embedding_admission import SharedEmbeddingBudget, model_scope

        base, _, model = model_service_config("EMBEDDING")
        config = (
            os.getpid(),
            str(Path(path).absolute()),
            model_scope(base, model),
            embedding_http_worker_limit(),
        )
        with self.condition:
            if self.shared_configuration != config:
                if self.shared_configuration and self.shared_configuration[0] == config[0]:
                    raise ValueError("Shared embedding admission configuration changed; restart this client")
                self.shared_held = threading.local()
                self.shared = SharedEmbeddingBudget(config[1], config[2], config[3])
                self.shared_configuration = config
            return self.shared

    def _shared_acquire(self, budget, blocking, timeout):
        from .embedding_admission import SharedEmbeddingCooldown

        request = _EMBEDDING_WAIT.get()
        lane = request["priority"] if request else _EMBEDDING_PRIORITY.get()
        token = request.setdefault("shared_token", uuid.uuid4().hex) if request else uuid.uuid4().hex
        if request:
            request["cleanup"] = lambda: budget.release(token, waiting_only=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                try:
                    acquired = budget.try_acquire(token, lane)
                except SharedEmbeddingCooldown as error:
                    if request and request["defer"]:
                        request["defer"](error.retry_after)
                    raise EmbeddingProviderDeferred(retry_after=error.retry_after) from None
                if acquired:
                    if not hasattr(self.shared_held, "leases"):
                        self.shared_held.leases = []
                    self.shared_held.leases.append((budget, token))
                    return True
                remaining = None if deadline is None else deadline - time.monotonic()
                if not blocking or remaining is not None and remaining <= 0:
                    return False
                time.sleep(0.025 if remaining is None else min(0.025, remaining))
        finally:
            if request is None:
                budget.release(token, waiting_only=True)

    def _remove(self, token):
        waiter = self.waiters.pop(token, None)
        if waiter:
            self.queues[waiter[0]].remove(token)
            self.condition.notify_all()

    def _cancel(self, token):
        with self.condition:
            self._remove(token)

    def _next(self):
        if sum(self.active.values()) >= self.limit:
            return None
        query, bulk = bool(self.queues["query"]), bool(self.queues["bulk"])
        if query and bulk and self.limit == 1:
            return "query" if self.query_streak < 3 else "bulk"
        if bulk and self.active["bulk"] < max(1, self.limit // 4):
            return "bulk"
        if query:
            return "query"
        if bulk and self.active["bulk"] < self.limit - min(4, self.limit // 16):
            return "bulk"
        return None

    def acquire(self, blocking=True, timeout=None):
        if not blocking and timeout is not None:
            raise ValueError("Timeout cannot be used with nonblocking admission")
        shared = self._shared()
        if shared:
            return self._shared_acquire(shared, blocking, timeout)
        limit = embedding_http_worker_limit()
        request = _EMBEDDING_WAIT.get()
        lane = request["priority"] if request else _EMBEDDING_PRIORITY.get()
        token = request["token"] if request else object()
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.condition:
            if self.limit is None:
                self.limit = limit
            elif limit != self.limit:
                raise ValueError("EMBEDDING_HTTP_WORKERS 已变更，请重启处理进程。")
            if token not in self.waiters:
                self.waiters[token] = lane, time.monotonic()
                self.queues[lane].append(token)
                if request:
                    request["cleanup"] = lambda: self._cancel(token)
            try:
                while True:
                    if self._next() == lane and self.queues[lane][0] is token:
                        started = self.waiters[token][1]
                        self._remove(token)
                        self.active[lane] += 1
                        self.peak = max(self.peak, sum(self.active.values()))
                        self.grants[lane] += 1
                        self.max_wait[lane] = max(self.max_wait[lane], time.monotonic() - started)
                        self.query_streak = self.query_streak + 1 if lane == "query" else 0
                        if not hasattr(self.held, "lanes"):
                            self.held.lanes = []
                        self.held.lanes.append(lane)
                        return True
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if not blocking or remaining is not None and remaining <= 0:
                        return False
                    self.condition.wait(remaining)
            finally:
                if request is None:
                    self._remove(token)

    def release(self):
        shared = getattr(self.shared_held, "leases", [])
        if shared:
            budget, token = shared.pop()
            budget.release(token)
            return
        with self.condition:
            lanes = getattr(self.held, "lanes", [])
            if not lanes:
                raise ValueError("尚未取得向量请求并发名额。")
            self.active[lanes.pop()] -= 1
            self.condition.notify_all()

    def snapshot(self):
        shared = self._shared()
        if shared:
            return shared.snapshot()
        with self.condition:
            limit = self.limit or embedding_http_worker_limit()
            return {
                "scope": "process",
                "limit": limit,
                "query_reserved": min(4, limit // 16),
                "bulk_floor_when_waiting": max(1, limit // 4),
                "bulk_ceiling": limit - min(4, limit // 16),
                "active": dict(self.active),
                "waiting": {key: len(queue) for key, queue in self.queues.items()},
                "peak": self.peak,
                "grants": dict(self.grants),
                "maximum_wait_seconds": dict(self.max_wait),
            }

    def cooldown(self, delay):
        shared = self._shared()
        if shared:
            shared.cooldown(delay)


_EMBEDDING_HTTP_SLOTS = _EmbeddingSlots()


class EmbeddingServiceError(ValueError):
    """Provider failure; the durable queue owns long waits and outer retries.

    Evidence fields describe the last failed HTTP request. They do not prove
    that earlier batches in this complete call issued no billable requests.
    """

    def __init__(
        self,
        *,
        retryable: bool,
        retry_after: float | None = None,
        http_status: int | None = None,
        request_outcome_unknown: bool = False,
        request_sent: bool | None = None,
    ):
        super().__init__("Embedding 服务请求失败，请检查服务配置或稍后重试。")
        self.retryable = retryable
        self.retry_after = retry_after if retryable else None
        self.http_status = http_status
        self.request_outcome_unknown = request_outcome_unknown
        self.request_sent = request_sent


class EmbeddingProviderDeferred(EmbeddingServiceError):
    """Provider cooldown deferred a call that issued no HTTP."""

    def __init__(self, retry_after: float):
        super().__init__(retryable=True, retry_after=retry_after, request_sent=False)


def _embedding_profile_key() -> str | None:
    values = [os.getenv("EMBEDDING_" + name, "").strip() for name in ("BASE_URL", "API_KEY", "MODEL")]
    if not all(values):
        return None
    # Separate credentials/models without retaining their values in the bounded cache.
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _embedding_profile(key: str | None, *, size=None, cooldown=None) -> dict:
    if key is None:
        return {}
    with _EMBEDDING_PROFILE_LOCK:
        previous = _EMBEDDING_PROFILES.pop(key, {})
        if size is not None:
            previous["size"] = min(previous.get("size", 64), size)
        if cooldown is not None:
            previous["available_at"] = max(previous.get("available_at", 0), time.monotonic() + cooldown)
        _EMBEDDING_PROFILES[key] = previous
        while len(_EMBEDDING_PROFILES) > _EMBEDDING_PROFILE_LIMIT:
            del _EMBEDDING_PROFILES[next(iter(_EMBEDDING_PROFILES))]
        return dict(previous)


def embedding_batch_size() -> int:
    """A configured ceiling, reduced after observed provider batch failures.

    Subsequent durable jobs checkpoint the learned size instead of repeating the
    oversized request. The profile is process-local and does not change model or
    input fingerprints; explicit provider tuning survives process restarts.
    """
    value = os.getenv("EMBEDDING_BATCH_SIZE", "64").strip()
    if not value.isdigit() or not 1 <= int(value) <= 64:
        raise ValueError("EMBEDDING_BATCH_SIZE 必须为 1 至 64 的整数。")
    return min(int(value), _embedding_profile(_embedding_profile_key()).get("size", 64))


def embedding_request_concurrency() -> int:
    """Operator opt-in; all callers share the configured process-wide HTTP slots."""
    value = os.getenv("EMBEDDING_REQUEST_CONCURRENCY", "1").strip()
    if value not in {"1", "2"}:
        raise ValueError("EMBEDDING_REQUEST_CONCURRENCY 必须为 1 或 2。")
    return int(value)


def embedding_claim_size() -> int:
    """Bounded durable claim size, separate from the actual HTTP batch ceiling."""
    return embedding_batch_size() * embedding_request_concurrency()


class _EmbeddingCallCancelled(Exception):
    """Internal cooperative cancellation; never becomes a queue failure."""


class _EmbeddingCall(AbstractContextManager):
    def __init__(self, profile_key: str | None):
        self.profile_key = profile_key
        self.priority = _EMBEDDING_PRIORITY.get()
        self.deadline = time.monotonic() + 300
        self.stop = threading.Event()
        self.preserve_inflight = False
        self.lock = threading.Lock()
        self.failures = self.active = self.requests = 0

    def __exit__(self, *args) -> None:
        # Exit before the executor context drains, including operator interrupts.
        self.stop.set()

    def check(self) -> None:
        if self.stop.is_set():
            raise _EmbeddingCallCancelled()
        if time.monotonic() >= self.deadline:
            raise EmbeddingServiceError(retryable=True)
        remaining = _embedding_profile(self.profile_key).get("available_at", 0) - time.monotonic()
        if remaining > 0:
            self.defer(remaining)

    def defer(self, delay: float) -> None:
        # Queue attempts are only reset when this complete call issued no HTTP.
        with self.lock:
            attempted = self.requests > 0
        if attempted:
            raise EmbeddingServiceError(retryable=True, retry_after=delay)
        raise EmbeddingProviderDeferred(retry_after=delay)

    def begin_request(self) -> None:
        self.check()
        with self.lock:
            # Reserve failures for in-flight requests too, so two retry lanes
            # together cannot exceed eight failed attempts in one complete call.
            if self.failures + self.active >= 8:
                raise EmbeddingServiceError(retryable=True)
            self.active += 1
            self.requests += 1

    def end_request(self, *, failed: bool) -> None:
        with self.lock:
            self.active -= 1
            self.failures += int(failed)


def _embedding_response(client, batch: list[str], base: str, key: str, model: str, call: _EmbeddingCall):
    import httpx

    # Normal contention is not a provider failure. Wait within this call's
    # existing budget and poll cancellation/cooldown, without issuing retries.
    with _embedding_wait(call.priority, defer=call.defer):
        while True:
            call.check()
            if _EMBEDDING_HTTP_SLOTS.acquire(timeout=max(0, min(0.25, call.deadline - time.monotonic()))):
                break
    begun, failed = False, False
    try:
        call.begin_request()
        begun = True
        # Slot waiting consumes the same complete-call budget as HTTP/retries.
        timeout = embedding_http_timeout()
        response_deadline = min(call.deadline, time.monotonic() + timeout + 5)
        request_timeout = max(0.001, min(timeout, call.deadline - time.monotonic()))
        with client.stream(
            "POST",
            base.rstrip("/") + "/embeddings",
            json={"model": model, "input": batch, "encoding_format": "float"},
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(request_timeout, connect=min(10.0, request_timeout)),
        ) as response:
            response.raise_for_status()
            payload = bytearray()
            for block in response.iter_bytes():
                if call.stop.is_set() and not call.preserve_inflight:
                    raise _EmbeddingCallCancelled()
                payload.extend(block)
                if len(payload) > 16 * 1024 * 1024:
                    raise ValueError("Embedding 服务响应超过 16 MB。")
                if time.monotonic() > response_deadline:
                    raise httpx.ReadTimeout("Embedding response deadline exceeded")
            return json.loads(payload)
    except httpx.HTTPError:
        failed = True
        raise
    finally:
        if begun:
            call.end_request(failed=failed)
        _EMBEDDING_HTTP_SLOTS.release()


def _embedding_batches(
    client, texts: list[str], base: str, key: str, model: str, call: _EmbeddingCall, on_batch=None
):
    import httpx

    vectors: list[list[float]] = []
    dimension = None
    batch_size = embedding_batch_size()
    start, singleton_failures = 0, 0
    try:
        while start < len(texts):
            call.check()
            batch_size = min(batch_size, embedding_batch_size())
            batch = texts[start : start + batch_size]
            try:
                body = _embedding_response(client, batch, base, key, model, call)
            except httpx.HTTPError as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                unknown = isinstance(
                    exc,
                    (
                        httpx.ReadTimeout,
                        httpx.WriteTimeout,
                        httpx.ReadError,
                        httpx.WriteError,
                        httpx.RemoteProtocolError,
                    ),
                ) or (status == 408 or status is not None and 500 <= status <= 599)
                sent = (
                    True
                    if status is not None or unknown
                    else (
                        False
                        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                        else None
                    )
                )
                evidence = {"http_status": status, "request_outcome_unknown": unknown, "request_sent": sent}
                retryable = isinstance(
                    exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
                ) or (status in {408, 429} or status is not None and 500 <= status <= 599)
                retry_after = (
                    _retry_after(exc.response.headers.get("Retry-After"))
                    if isinstance(exc, httpx.HTTPStatusError) and retryable
                    else None
                )
                if status == 429 or retry_after:
                    delay = retry_after if retry_after is not None else 2.0
                    _embedding_profile(call.profile_key, cooldown=delay)
                    if hasattr(_EMBEDDING_HTTP_SLOTS, "cooldown"):
                        _EMBEDDING_HTTP_SLOTS.cooldown(delay)
                    raise EmbeddingServiceError(retryable=True, retry_after=delay, **evidence) from None
                if unknown and not embedding_retry_ambiguous_timeouts():
                    # A transport/gateway failure may have happened after the
                    # provider accepted and billed the input. Preserve uncertainty.
                    raise EmbeddingServiceError(retryable=True, retry_after=60, **evidence) from None
                shrink = isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout)) or status in {408, 413, 504}
                with call.lock:
                    can_retry = call.failures < 8
                if shrink and len(batch) > 1 and can_retry and time.monotonic() < call.deadline:
                    batch_size = max(1, len(batch) // 2)
                    _embedding_profile(call.profile_key, size=batch_size)
                    continue
                singleton_failures += 1
                if retryable and singleton_failures < 3 and can_retry and time.monotonic() < call.deadline:
                    time.sleep(
                        max(
                            0,
                            min(1.0, 0.25 * 2 ** (singleton_failures - 1), call.deadline - time.monotonic()),
                        )
                    )
                    continue
                raise EmbeddingServiceError(retryable=retryable, **evidence) from None
            except EmbeddingServiceError:
                raise
            except (ValueError, UnicodeDecodeError):
                raise EmbeddingServiceError(retryable=False) from None
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list) or len(data) != len(batch):
                raise ValueError("Embedding 服务返回数量与输入不一致。")
            if any(not isinstance(entry, dict) or type(entry.get("index")) is not int for entry in data) or {
                entry["index"] for entry in data
            } != set(range(len(batch))):
                raise ValueError("Embedding 服务返回索引无效。")
            completed = []
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
                completed.append([float(value) for value in vector])
            # A durable operator callback can retain this successful response
            # even if a later batch or parallel sibling fails. No partial or
            # unvalidated provider payload crosses this checkpoint boundary.
            if on_batch is not None:
                on_batch(list(batch), completed)
            vectors.extend(completed)
            start += len(batch)
            singleton_failures = 0
        return vectors
    except Exception:
        call.stop.set()
        raise


def model_service_config(prefix: str) -> tuple[str, str, str] | None:
    base = os.environ.get(f"{prefix}_BASE_URL", "").strip()
    key = os.environ.get(f"{prefix}_API_KEY", "").strip()
    model = os.environ.get(f"{prefix}_MODEL", "").strip()
    if not any((base, key, model)):
        return None
    if not all((base, key, model)):
        raise ValueError(f"必须同时配置 {prefix}_BASE_URL、{prefix}_API_KEY 和 {prefix}_MODEL。")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{prefix}_BASE_URL 无效。")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"远程 {prefix.lower()} 服务必须使用 HTTPS。")
    return base.rstrip("/"), key, model


def embed_texts(texts: list[str], *, on_batch=None) -> list[list[float]] | None:
    configuration = model_service_config("EMBEDDING")
    if configuration is None:
        return None
    base, key, model = configuration
    if not texts:
        return []
    if len(texts) > 10000 or any(not isinstance(text, str) or len(text) > 20000 for text in texts):
        raise ValueError("Embedding 输入超过处理限制。")
    import httpx
    from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

    embedding_http_worker_limit()
    embedding_http_timeout()
    embedding_retry_ambiguous_timeouts()
    concurrency = embedding_request_concurrency()
    batch_size = embedding_batch_size()
    call = _EmbeddingCall(_embedding_profile_key())
    # Already issued siblings may still return billable, valid vectors after a
    # peer fails. A durable callback can retain those within existing deadlines.
    call.preserve_inflight = on_batch is not None
    # HTTPX read timeouts bound inactivity, not a complete trickling response.
    # The existing response/call deadlines remain shared across both lanes.
    with httpx.Client(
        timeout=httpx.Timeout(embedding_http_timeout(), connect=10.0), follow_redirects=False, trust_env=False
    ) as client:
        if concurrency == 1 or len(texts) <= batch_size:
            return _embedding_batches(client, texts, base, key, model, call, on_batch)
        vectors, start, dimension = [], 0, None
        # Only one wave of at most two futures exists. HTTPX clients are thread
        # safe; drain workers before closing the shared client on every exit.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="tea-embedding") as pool, call:
            while start < len(texts):
                call.check()
                size = embedding_batch_size()
                batches = [
                    texts[offset : offset + size]
                    for offset in range(start, min(len(texts), start + 2 * size), size)
                ]
                futures = {
                    pool.submit(_embedding_batches, client, batch, base, key, model, call, on_batch): index
                    for index, batch in enumerate(batches)
                }
                results, errors = {}, []
                for future in as_completed(futures):
                    try:
                        results[futures[future]] = future.result()
                    except Exception as exc:
                        call.stop.set()
                        errors.append(exc)
                        for pending in futures:
                            pending.cancel()
                if errors:
                    error = next(
                        (
                            exc
                            for exc in errors
                            if not isinstance(exc, (_EmbeddingCallCancelled, CancelledError))
                        ),
                        errors[0],
                    )
                    raise error from None
                for index in range(len(batches)):
                    for vector in results[index]:
                        dimension = dimension or len(vector)
                        if len(vector) != dimension:
                            call.stop.set()
                            raise ValueError("Embedding 向量维度不一致。")
                        vectors.append(vector)
                start += sum(len(batch) for batch in batches)
        return vectors


_RERANK_SLOTS = threading.BoundedSemaphore(4)


def rerank_chunks(query: str, chunks: list[dict], limit: int = 5) -> list[dict] | None:
    """Rank eligible candidates by returned indices; never accept provider evidence."""
    configuration = model_service_config("RERANK")
    if configuration is None:
        return None
    if not chunks:
        return []
    texts = [chunk.get("text") for chunk in chunks]
    if (
        not isinstance(query, str)
        or not 1 <= len(query) <= 2000
        or len(chunks) > 60
        or not 1 <= limit <= 20
        or any(not isinstance(text, str) or not text or len(text) > 20000 for text in texts)
        or sum(len(text) for text in texts) > 1_000_000
    ):
        raise ValueError("Rerank 输入超过处理限制。")
    if not _RERANK_SLOTS.acquire(blocking=False):
        raise ValueError("Rerank 服务繁忙。")
    import httpx

    base, key, model = configuration
    count = min(limit, len(chunks))
    try:
        deadline = time.monotonic() + 20
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False, trust_env=False
        ) as client:
            with client.stream(
                "POST",
                base + "/rerank",
                json={"model": model, "query": query, "documents": texts, "top_n": count},
                headers={"Authorization": f"Bearer {key}"},
            ) as response:
                response.raise_for_status()
                payload = bytearray()
                for block in response.iter_bytes():
                    payload.extend(block)
                    if time.monotonic() > deadline or len(payload) > 1024 * 1024:
                        raise ValueError("Rerank 服务响应超过处理限制。")
                body = json.loads(payload)
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or len(results) != count:
            raise ValueError("Rerank 服务返回数量与请求不一致。")
        ranked, indices = [], set()
        for result in results:
            index = result.get("index") if isinstance(result, dict) else None
            score = result.get("relevance_score") if isinstance(result, dict) else None
            if (
                type(index) is not int
                or not 0 <= index < len(chunks)
                or index in indices
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise ValueError("Rerank 服务返回索引或分数无效。")
            indices.add(index)
            ranked.append({**chunks[index], "rerank_score": float(score)})
        return sorted(ranked, key=lambda item: item["rerank_score"], reverse=True)
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("Rerank 服务请求失败，请检查服务配置或稍后重试。") from exc
    finally:
        _RERANK_SLOTS.release()


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
    """RFC 9110 section 10.2.3: retain the server's minimum retry delay.

    The durable queue schedules long delays without occupying a worker. The
    header bound keeps decimal conversion finite; truncating the delay would
    issue requests before the server's stated recovery time.
    """
    value = _bounded_header(value, 128)
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
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
    public_search: EGazetteSearchRequest | None = None,
    max_bytes: int | None = None,
    byte_range: tuple[int, int] | None = None,
    range_validator: str | None = None,
    range_total_bytes: int | None = None,
    on_headers: Callable[[dict], None] | None = None,
) -> dict:
    """Read a bounded HTTPS resource, optionally into an atomic blob consumer.

    A consumer must exhaust the iterable and only publish after normal completion.
    A 304 does not invoke it: callers must validate their cached original and URL.
    Conditional headers belong exclusively to the initial requested resource.
    A connection slot accounts for the actual host at every redirect hop.
    Public search credentials are bound to one verified read-only endpoint;
    they never enter a redirect or an arbitrary caller-provided header mapping.
    """
    current = url
    validators = validators or {}
    search_headers = {}
    if public_search is not None:
        if type(public_search) is not EGazetteSearchRequest or json_body is None or validators or byte_range:
            raise DownloadError("公开检索只允许显式受限的只读 JSON 请求。")
        try:
            search_headers = public_search.headers_for(url, method="POST")
        except ValueError as exc:
            raise DownloadError(str(exc)) from exc
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ORIGINAL_BYTES:
        raise DownloadError("原件传输额度无效，最多 128 MB。")
    if range_total_bytes is not None and (
        byte_range is None
        or type(range_total_bytes) is not int
        or not 1 <= range_total_bytes <= 1024 * 1024 * 1024
    ):
        raise DownloadError("专用数据集必须指定有效分段及不超过 1 GB 的固定总大小。")
    if byte_range is not None:
        if (
            not isinstance(byte_range, tuple)
            or len(byte_range) != 2
            or any(type(value) is not int for value in byte_range)
            or not 0 <= byte_range[0] <= byte_range[1] < (range_total_bytes or max_bytes)
            or (range_total_bytes is not None and byte_range[1] - byte_range[0] + 1 > max_bytes)
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
        proxy = None
        if any(
            os.getenv(key)
            for key in ("COLLECTION_PROXY_URL", "COLLECTION_PROXY_HOSTS", "COLLECTION_DOH_HOSTS")
        ):
            from .collection_proxy import PinnedProxyHTTPSConnection, proxy_for_host, resolve_addresses

            addresses = resolve_addresses(host, deadline=deadline, fallback=_public_addresses)
            proxy = proxy_for_host(host)
        else:
            addresses = _public_addresses(host, deadline=deadline)
        addresses = sorted(addresses, key=lambda row: row[0] != socket.AF_INET)
        with connection_slot(host) if connection_slot else nullcontext():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadError("抓取超过总时间限制。", retryable=True)
            connection = (
                PinnedProxyHTTPSConnection(host, addresses[0], min(15.0, remaining), proxy=proxy)
                if proxy is not None
                else _PinnedHTTPSConnection(host, addresses[0], min(15.0, remaining))
            )
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
                if os.getenv("COLLECTION_BROWSER_HEADERS_HOSTS"):
                    from .official_source_recovery import request_headers_for

                    headers.update(request_headers_for(host))
                headers.update({key: value for key, value in conditionals.items() if value is not None})
                headers.update(search_headers)
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
                if range_total_bytes is not None and response.status != 206:
                    raise DownloadError("专用数据集必须返回与固定总大小一致的分段响应。")
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
                        not 0 <= range_start <= range_end < total_size <= (range_total_bytes or max_bytes)
                        or (range_total_bytes is not None and total_size != range_total_bytes)
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
                    (
                        accept_compressed_html
                        and mime in {"text/html", "application/xhtml+xml", "text/xml", "application/xml"}
                    )
                    or (accept_compressed_json and mime == "application/json")
                ):
                    raise DownloadError("仅清单目录或详情 HTML/JSON/XML 允许受限压缩传输。")
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
