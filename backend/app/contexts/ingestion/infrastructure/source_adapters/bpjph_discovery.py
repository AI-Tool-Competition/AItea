"""Bounded discovery from BPJPH's official JDIH public directory.

The site publishes its current catalogue in a Next.js Flight payload. This
adapter reads that inert payload as JSON, follows only the observed page query,
and confirms each PDF through the matching official detail page's iframe.
Discovered metadata remains unreviewed source data.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, quote, urlsplit

from app.contexts.documents.infrastructure.parsers.native_parser import _decode

HOST = "jdih.halal.go.id"
ROOT = "https://jdih.halal.go.id"
MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_CATALOG_PAGES = 100
MAX_DOCUMENTS = 10_000
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_FILE_PATH = re.compile(r"jdih/dokumen/[A-Za-z0-9_.() -]{1,220}\.pdf", re.I)
_FLIGHT_PUSH = re.compile(r"self\.__next_f\.push\(\[1,")
_ROW_FIELDS = (
    "id",
    "judul",
    "nomor_dokumen",
    "nomor_peraturan",
    "tahun_terbit",
    "tanggal_penetapan",
    "tanggal_dibacakan",
    "status",
    "bidang_hukum",
    "penerbit",
    "dokumen_tipe_nama",
    "dokumen_jenis_nama",
    "dokumen_jenis_kode",
    "dokumen_jenis_singkatan",
    "lampiran_file_id",
)
_ERROR_PAGE = re.compile(
    r"access denied|403 forbidden|404 not found|page not found|service unavailable|"
    r"internal server error|checking your browser|verify (?:that )?you are human|"
    r"akses ditolak|request rejected|login required",
    re.I,
)


def _valid_html(content: bytes):
    if not content or len(content) > MAX_HTML_BYTES:
        return False
    from selectolax.lexbor import LexborHTMLParser

    try:
        tree = LexborHTMLParser(_decode(content), scripting=False)
        nodes = tree.css("*")
        if len(nodes) > 50_000:
            return False
        title = tree.css_first("title")
        headings = tree.css("h1, h2, h3")[:5]
        title_text = " ".join(title.text(deep=True, separator=" ", strip=True).split()) if title else ""
        heading_text = " ".join(
            " ".join(node.text(deep=True, separator=" ", strip=True).split()) for node in headings
        )
        body = tree.body
        visible = " ".join(body.text(deep=True, separator=" ", strip=True).split()) if body else ""
    except (ValueError, AssertionError, RecursionError):
        return False
    if _ERROR_PAGE.search(title_text) or _ERROR_PAGE.search(heading_text):
        return False
    if not re.search(r"\b(?:JDIH|BPJPH)\b", title_text + " " + heading_text, re.I):
        return False
    return len(visible) >= 100


def _result(*, matched=False, purpose=None, links=None, warnings=None, reason=None):
    return {
        "links": links or [],
        "warnings": warnings or [],
        "matched": matched,
        "purpose": purpose,
        "reason": reason,
    }


def _query(url: str, *, allow_page=False):
    parsed = urlsplit(url)
    values = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
    if allow_page:
        if set(values) - {"page"} or any(len(rows) != 1 for rows in values.values()):
            raise ValueError("BPJPH 目录仅接受单一页码参数。")
        page = values.get("page", ["1"])[0]
        if not re.fullmatch(r"[1-9]\d{0,2}", page) or int(page) > MAX_CATALOG_PAGES:
            raise ValueError("BPJPH 目录页码超出有界范围。")
        return int(page)
    if set(values) != {"path"} or len(values["path"]) != 1:
        raise ValueError("BPJPH 原件仅接受单一文件路径参数。")
    path = values["path"][0]
    if not _FILE_PATH.fullmatch(path) or ".." in path or "\\" in path:
        raise ValueError("BPJPH 原件路径不符合已核验格式。")
    return path


def purpose(url: str) -> str | None:
    parsed = urlsplit(url)
    if (
        parsed.hostname != HOST
        or parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
    ):
        return None
    if parsed.path == "/cari":
        try:
            _query(url, allow_page=True)
        except ValueError:
            return None
        return "catalog"
    detail = re.fullmatch(r"/jdih/dokumen/([0-9a-f-]{36})", parsed.path, re.I)
    if detail:
        return "detail" if _UUID.fullmatch(detail[1]) and not parsed.query else None
    if parsed.path == "/api/jdih/pdf":
        try:
            _query(url)
        except ValueError:
            return None
        return "document"
    return None


def _flight_chunks(content: bytes) -> list[str]:
    if not content or len(content) > MAX_HTML_BYTES:
        return []
    source = _decode(content)
    decoder = json.JSONDecoder()
    chunks = []
    for match in _FLIGHT_PUSH.finditer(source):
        try:
            value, _ = decoder.raw_decode(source, match.end())
        except (json.JSONDecodeError, RecursionError):
            continue
        if isinstance(value, str):
            chunks.append(value)
            if len(chunks) >= 128:
                break
    return chunks


def _catalog_payload(content: bytes, requested_page: int):
    decoder = json.JSONDecoder()
    candidates = []
    for chunk in _flight_chunks(content):
        for marker in re.finditer(r'\{"data":\[', chunk):
            try:
                value, _ = decoder.raw_decode(chunk, marker.start())
            except (json.JSONDecodeError, RecursionError):
                continue
            if isinstance(value, dict) and {"data", "total", "page", "limit", "totalPages"} <= value.keys():
                candidates.append(value)
                if len(candidates) > 8:
                    return None
    if len(candidates) != 1:
        return None
    data = candidates[0]
    rows, total, page, limit, total_pages = (
        data.get("data"),
        data.get("total"),
        data.get("page"),
        data.get("limit"),
        data.get("totalPages"),
    )
    if (
        not isinstance(rows, list)
        or type(total) is not int
        or not 0 <= total <= MAX_DOCUMENTS
        or type(page) is not int
        or page != requested_page
        or type(limit) is not int
        or limit != 10
        or type(total_pages) is not int
        or total_pages != (total + limit - 1) // limit
        or not 0 <= total_pages <= MAX_DOCUMENTS
        or (total_pages == 0 and page != 1)
        or (total_pages > 0 and page > total_pages)
        or len(rows) != max(0, min(limit, total - (page - 1) * limit))
        or data.get("search") not in ("", None)
        or data.get("bahasa") not in ("", None)
        or data.get("expandedTerms") not in ([], None)
    ):
        return None
    identifiers = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("id"), str)
            or not _UUID.fullmatch(row["id"])
            or row["id"].casefold() in identifiers
            or not isinstance(row.get("judul"), str)
            or not 1 <= len(row["judul"]) <= 2000
        ):
            return None
        identifiers.add(row["id"].casefold())
    return data


def _source_record(row: dict, *, page_url: str, original_url: str | None = None):
    fields = {}
    for key in _ROW_FIELDS:
        if key not in row:
            continue
        value = row[key]
        if value is None or type(value) is int:
            fields[key] = value
        elif isinstance(value, str) and len(value) <= 2000:
            fields[key] = value
    title = fields.get("judul") or ""
    file_path = fields.get("lampiran_file_id")
    number = fields.get("nomor_peraturan") or fields.get("nomor_dokumen")
    structure = {
        "unit_type": fields.get("dokumen_jenis_kode") or fields.get("dokumen_tipe_nama"),
        "document_id": fields.get("id"),
        "title": title,
        "number": number,
        "year": fields.get("tahun_terbit"),
        "parent_titles": [],
        "file_name": file_path.rsplit("/", 1)[-1] if isinstance(file_path, str) else None,
        "link_mechanism": "official_detail_iframe" if original_url else "official_catalogue_record",
    }
    value = {
        "provider": "bpjph",
        "interpretation": "unreviewed_source_values",
        "fields": fields,
        "structure": structure,
        "evidence": {"source_page_url": page_url},
    }
    if original_url:
        value["fields"]["official_file_url"] = original_url
        value["fields"]["file_format"] = "pdf"
    return value


def inspect_catalog(url: str, content: bytes, mime: str, rules) -> dict:
    if mime not in {"text/html", "application/xhtml+xml"} or not _valid_html(content):
        return _result(reason="BPJPH 目录必须是 4 MiB 内的官方 HTML 页面。")
    requested_page = _query(url, allow_page=True)
    payload = _catalog_payload(content, requested_page)
    if payload is None:
        return _result(reason="BPJPH 目录的页码、记录数或公开数据结构无法核验。")
    links, warnings = [], []
    terms = rules.include_terms
    excluded = rules.exclude_terms
    for row in payload["data"]:
        context = " ".join(
            str(row.get(key) or "")
            for key in ("judul", "dokumen_jenis_nama", "dokumen_jenis_kode", "dokumen_tipe_nama")
        ).casefold()
        if (terms and not any(term in context for term in terms)) or any(
            term in context for term in excluded
        ):
            continue
        identifier = row["id"].casefold()
        links.append(
            {
                "url": f"{ROOT}/jdih/dokumen/{identifier}",
                "purpose": "detail",
                "title": row["judul"][:300],
                "identity_key": f"bpjph:{identifier}",
                "source_record": _source_record(row, page_url=url),
            }
        )
    total_pages = payload["totalPages"]
    if requested_page < total_pages and requested_page < MAX_CATALOG_PAGES:
        links.append(
            {
                "url": f"{ROOT}/cari?page={requested_page + 1}",
                "purpose": "catalog",
                "title": f"BPJPH catalogue page {requested_page + 1}",
            }
        )
    if total_pages > MAX_CATALOG_PAGES and requested_page == MAX_CATALOG_PAGES:
        warnings.append("BPJPH 目录页数超过 100 页发现上限，剩余目录需要后续有界任务。")
    return {
        **_result(matched=True, purpose="catalog", links=links, warnings=warnings),
        "scope": {
            "provider": "bpjph-jdih",
            "total": payload["total"],
            "page": payload["page"],
            "page_size": payload["limit"],
            "pages_reported": total_pages,
            "returned": len(payload["data"]),
            "matched_documents": len(links) - int(requested_page < total_pages),
            "directory_snapshot_atomic": False,
        },
    }


def detail_evidence(url: str, content: bytes):
    """Return the page metadata and PDF URL for link_discovery's hardened DOM scan."""
    if len(content) > MAX_HTML_BYTES:
        return {"reason": "BPJPH 详情超过 4 MiB 发现上限。"}
    identifier = urlsplit(url).path.rsplit("/", 1)[-1].casefold()
    decoder = json.JSONDecoder()
    records = []
    needle = f'{{"id":"{identifier}"'
    for chunk in _flight_chunks(content):
        for match in re.finditer(re.escape(needle), chunk, re.I):
            try:
                value, _ = decoder.raw_decode(chunk, match.start())
            except (json.JSONDecodeError, RecursionError):
                continue
            if (
                isinstance(value, dict)
                and isinstance(value.get("id"), str)
                and value["id"].casefold() == identifier
            ):
                records.append(value)
                if len(records) > 4:
                    return {"reason": "BPJPH 详情重复或歧义，未确认原件。"}
    if len(records) != 1:
        return {"reason": "BPJPH 详情没有唯一匹配到目录文书元数据。"}
    row = records[0]
    file_path = row.get("lampiran_file_id")
    if (
        not isinstance(row.get("judul"), str)
        or not isinstance(file_path, str)
        or not _FILE_PATH.fullmatch(file_path)
        or ".." in file_path
        or "\\" in file_path
    ):
        return {"reason": "BPJPH 详情没有可确认的正式 PDF 附件。"}
    expected_url = f"{ROOT}/api/jdih/pdf?path={quote(file_path, safe='')}"
    record = _source_record(row, page_url=url, original_url=expected_url)
    return {
        "expected_url": expected_url,
        "identity_key": f"bpjph:{identifier}",
        "title": row["judul"][:300],
        "source_record": record,
    }


def inspect_document(url: str, content: bytes, mime: str) -> dict:
    if purpose(url) != "document" or mime != "application/pdf" or not content.startswith(b"%PDF-"):
        return _result(reason="BPJPH 原件响应类型或 PDF 签名不匹配。")
    return _result(matched=True, purpose="document")
