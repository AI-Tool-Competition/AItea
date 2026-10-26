"""Native text/HTML manifests and historical PDF evidence."""

from __future__ import annotations

import base64
import hashlib
import io
import re
from pathlib import Path

from app.contexts.documents.infrastructure.parsers.html_parsing import HTMLDocument as _HTMLDocument
from app.contexts.documents.infrastructure.parsers.pdf_table_detection import VERSION as _TABLE_LAYOUT_VERSION
from app.shared.domain.content import MAX_TEXT, _decode
from app.shared.infrastructure.http.download import MAX_BYTES
from app.shared.infrastructure.vendor.geo_parser.document_ingestion import (
    ParsedDocument,
    ParsedElement,
    _parse_native_text,
    build_manifest,
)

MAX_ELEMENTS = 20_000


MAX_PAGES = 300
_ACCESS_DATABASE_MIMES = frozenset(
    {"application/msaccess", "application/x-msaccess", "application/vnd.ms-access"}
)


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
        from importlib.metadata import version

        from pypdf import PdfReader
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

            from app.contexts.documents.infrastructure.parsers.document_parser import _render_pdf_page

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
    from app.contexts.documents.infrastructure.parsers.pdf_tables import VERSION, extract_grid
    from app.contexts.documents.infrastructure.parsers.pdf_text_visibility import (
        VERSION as VISIBILITY_VERSION,
    )
    from app.contexts.documents.infrastructure.parsers.pdf_text_visibility import (
        audit_page_visibility,
        prove_blank_page,
    )

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
    from app.contexts.documents.infrastructure.parsers.pdf_table_detection import requires_table_review

    return requires_table_review(stream, resources)


def pdf_quality(pages: list[dict]) -> dict:
    from app.contexts.documents.infrastructure.parsers.ingestion_quality import pdf_page_status

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
    from app.contexts.documents.infrastructure.parsers.ingestion_quality import (
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
    extension = Path(filename).suffix.lower()
    normalized_mime = mime_type.split(";", 1)[0].strip().lower()
    if extension in {".html", ".htm"} or normalized_mime == "text/html":
        document = _parse_html(content)
        return _parsed_result(document, hashlib.sha256(content).hexdigest(), Path(filename).stem)
    if extension == ".accdb":
        raise ValueError("ACCDB 格式尚无经过审计的只读解析器；原件可保存，但不能作为已解析正文加入检索。")
    if extension == ".mdb" or normalized_mime in _ACCESS_DATABASE_MIMES:
        from app.contexts.documents.infrastructure.parsers.mdb_parsing import parse_mdb

        return parse_mdb(content, filename=filename)
    from app.contexts.documents.infrastructure.providers.mineru import pending_manifest, supports

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
