"""Pure document ingestion, grounded retrieval and bounded HTTPS collection.

Call these blocking functions from a worker, never from the API event loop.
Business ownership, version persistence and access control belong to the service.
"""

from __future__ import annotations

import hashlib
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
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
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

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
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
        if tag in self._hidden:
            self.hidden.append(tag)
            return
        if self.hidden:
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
        if tag in self._blocks:
            self._flush()
            self.heading_level = 0
            self.anchor = None
            self.start_line = self.getpos()[0]

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.buffer.append(data)

    def finish(self) -> None:
        self.close()
        self._flush()


def _parse_html(content: bytes) -> ParsedDocument:
    source = _decode(content)
    if len(source) > MAX_TEXT:
        raise ValueError("HTML 文本超过处理限制。")
    extractor = _HTMLDocument()
    extractor.feed(source)
    extractor.finish()
    return ParsedDocument("html-basic", "1", "html-blocks-v1", extractor.elements, [], [])


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
        elements: list[ParsedElement] = []
        empty_pages: list[int] = []
        characters = 0
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
                continue
            elements.append(
                ParsedElement(
                    f"page-{index:06}", "paragraph", index - 1, text, {"kind": "pdf", "page": index}, []
                )
            )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF 无法读取，请检查文件是否损坏。") from exc
    if not elements:
        raise ValueError("PDF 没有可读取文本，需要 OCR；原文件可以保留，但不能作为已解析证据。")
    warnings = ["轻量 PDF 解析仅提取文字并保留页码，不识别扫描图片或复杂排版。"]
    if empty_pages:
        warnings.append("以下页未提取到文本，需要核对或 OCR：" + ", ".join(map(str, empty_pages)))
    return ParsedDocument("pypdf", version("pypdf"), "pdf-pages-v1", elements, [], warnings)


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
    if not document.elements or sum(len(x.content_text) for x in document.elements) > MAX_TEXT:
        raise ValueError("文档没有可解析的正文，或正文超过处理限制。")
    manifest, _ = build_manifest(
        document, source_hash=hashlib.sha256(content).hexdigest(), document_title=Path(filename).stem
    )
    if not manifest["chunks"]:
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
        "title": next(
            (e.content_text for e in document.elements if e.kind in {"title", "heading"}), Path(filename).stem
        ),
        "elements": manifest["elements"],
        "chunks": chunks,
        "warnings": document.warnings,
        "retrieval_mode": "lexical",
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
    with httpx.Client(
        timeout=httpx.Timeout(30.0, connect=10.0), follow_redirects=False, trust_env=False
    ) as client:
        for start in range(0, len(texts), 64):
            batch = texts[start : start + 64]
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


def _validated_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\x7f]", url):
        raise ValueError("抓取 URL 无效。")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        raise ValueError("抓取仅支持不含凭据的标准端口 HTTPS URL。")
    host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("抓取地址必须是 public 公网地址。")
    netloc = f"[{host}]" if ":" in host else host
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&%/:?@!$'()*+,;~-._")
    return urlunsplit(("https", netloc, path, query, "")), host


def _public_addresses(host: str, *, deadline: float | None = None) -> list[tuple]:
    # System DNS has no Python timeout. Bound waiting and outstanding resolver
    # threads so a resolver outage cannot exhaust application worker threads.
    remaining = min(10.0, (deadline or time.monotonic() + 10) - time.monotonic())
    if remaining <= 0 or not _DNS_SLOTS.acquire(timeout=max(0, remaining)):
        raise ValueError("DNS 解析超过时间限制或队列已满。")
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
        raise ValueError("DNS 解析超过时间限制。") from exc
    if isinstance(addresses, Exception):
        raise ValueError("抓取来源域名无法解析。") from addresses
    if not addresses:
        raise ValueError("抓取来源没有可用地址。")
    for address in addresses:
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise ValueError("抓取来源的所有 DNS 地址必须是 public 公网地址。")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect exactly to a validated address; TLS still authenticates the hostname."""

    def __init__(self, host: str, address: tuple, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        family, socket_type, protocol, _, destination = self.address
        raw = socket.socket(family, socket_type, protocol)
        raw.settimeout(self.timeout)
        try:
            raw.connect(destination)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
            self.active_socket = self.sock
        except BaseException:
            raw.close()
            raise


def _download(url: str, *, deadline: float, required_host: str | None = None) -> dict:
    current = url
    for redirect in range(4):
        current, host = _validated_url(current)
        if required_host is not None and host != required_host:
            raise ValueError("关联附件重定向到了不同来源，已拒绝抓取。")
        addresses = _public_addresses(host, deadline=deadline)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("抓取超过总时间限制。")
        connection = _PinnedHTTPSConnection(host, addresses[0], min(15.0, remaining))

        def expire_connection() -> None:
            # An overall deadline also interrupts slow response headers and
            # HTTPResponse-owned sockets after Connection: close.
            active = getattr(connection, "active_socket", None)
            if active is not None:
                try:
                    active.shutdown(socket.SHUT_RDWR)
                    active.close()
                except OSError:
                    pass

        expiry = threading.Timer(remaining, expire_connection)
        expiry.daemon = True
        expiry.start()
        try:
            parsed = urlsplit(current)
            connection.request(
                "GET",
                parsed.path + ("?" + parsed.query if parsed.query else ""),
                headers={
                    "User-Agent": "AItea/0.1 SourceCollector",
                    "Accept-Encoding": "identity",
                    "Accept": "application/pdf,text/html,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,*/*;q=0.5",
                },
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location or redirect == 3:
                    raise ValueError("抓取来源重定向过多或地址缺失。")
                current = urljoin(current, location)
                continue
            if not 200 <= response.status < 300:
                raise ValueError(f"抓取来源返回 HTTP {response.status}。")
            if response.getheader("Content-Encoding", "identity").lower() not in {"identity", ""}:
                raise ValueError("抓取来源返回压缩传输，无法安全校验大小。")
            length = response.getheader("Content-Length")
            if length and (not length.isdigit() or int(length) > MAX_BYTES):
                raise ValueError("抓取文件大小无效或超过 20 MB。")
            payload = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("抓取超过总时间限制。")
                if connection.sock:
                    connection.sock.settimeout(min(15.0, remaining))
                block = response.read1(min(65536, MAX_BYTES + 1 - len(payload)))
                if not block:
                    break
                payload.extend(block)
                if len(payload) > MAX_BYTES:
                    raise ValueError("抓取文件超过 20 MB。")
            if not payload:
                raise ValueError("抓取文件为空。")
            mime = (
                response.getheader("Content-Type", "application/octet-stream").split(";")[0].strip().lower()
            )
            name = Path(unquote(parsed.path)).name or "source"
            name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name)[:180]
            if not Path(name).suffix or (
                mime in _MIME_EXTENSIONS
                and Path(name).suffix.lower() != _MIME_EXTENSIONS[mime]
                and not (mime == "text/html" and Path(name).suffix.lower() == ".htm")
            ):
                name += _MIME_EXTENSIONS.get(mime, mimetypes.guess_extension(mime) or ".bin")
            return {"url": current, "filename": name, "mime_type": mime, "content": bytes(payload)}
        except (OSError, http.client.HTTPException) as exc:
            raise ValueError("抓取连接失败或超时，请检查来源后重试。") from exc
        finally:
            expiry.cancel()
            connection.close()
    raise ValueError("抓取重定向无法完成。")


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
        extractor = _HTMLDocument()
        extractor.feed(_decode(primary["content"]))
        extractor.finish()
        host = urlsplit(primary["url"]).hostname
        seen: set[str] = set()
        for link in extractor.links:
            try:
                candidate, candidate_host = _validated_url(urljoin(primary["url"], link))
            except ValueError:
                continue
            if (
                candidate_host != host
                or Path(unquote(urlsplit(candidate).path)).suffix.lower() not in _DIRECT_EXTENSIONS
                or candidate in seen
            ):
                continue
            seen.add(candidate)
            if len(seen) > max_attachments:
                break
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
