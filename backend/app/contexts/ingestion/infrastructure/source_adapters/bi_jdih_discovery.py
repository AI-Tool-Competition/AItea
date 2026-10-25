"""Read-only, bounded discovery for Bank Indonesia's official JDIH API."""

from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qs, urlencode, urlsplit

_HOST = "jdih.bi.go.id"
_CATALOG_PATH = "/Web/DaftarPeraturan"
_DETAIL_PATH = "/api/WebJDIH/GetDataWebPeraturan"
_FILE_PREFIX = "/api/WebJDIH/GetFilePeraturan/"
_ID = re.compile(r"[1-9][0-9]{0,11}\Z")
_CATALOG_BYTES = 6 * 1024 * 1024
_JSON_BYTES = 1024 * 1024
_CATALOG_BATCH = 199


def _query(url: str) -> dict[str, list[str]] | None:
    try:
        return parse_qs(urlsplit(url).query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        return None


def _single_identifier(values: dict[str, list[str]], key: str) -> str | None:
    matches = [value for name, rows in values.items() if name.casefold() == key.casefold() for value in rows]
    if len(matches) != 1 or not _ID.fullmatch(matches[0]):
        return None
    return str(int(matches[0]))


def purpose(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname != _HOST:
        return None
    values = _query(url)
    if values is None:
        return None
    if parsed.path == _CATALOG_PATH:
        if not values:
            return "catalog"
        if set(values) != {"aitea_offset"} or len(values["aitea_offset"]) != 1:
            return None
        offset = values["aitea_offset"][0]
        return "catalog" if offset.isdecimal() and int(offset) <= 100_000 else None
    if parsed.path == _DETAIL_PATH:
        return "detail" if set(values) == {"PeraturanID"} and _single_identifier(values, "PeraturanID") else None
    if not parsed.query and parsed.path.startswith(_FILE_PREFIX):
        return "document" if _ID.fullmatch(parsed.path.removeprefix(_FILE_PREFIX)) else None
    return None


def _safe_value(value, *, depth=0):
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:2000]
    if depth >= 3:
        return None
    if isinstance(value, list):
        return [_safe_value(entry, depth=depth + 1) for entry in value[:50]]
    if isinstance(value, dict):
        return {
            str(key)[:80]: _safe_value(entry, depth=depth + 1)
            for key, entry in list(value.items())[:50]
            if isinstance(key, (str, int))
        }
    return None


def _field(data: dict, *names):
    by_casefold = {str(key).casefold(): value for key, value in data.items()}
    for name in names:
        if name.casefold() in by_casefold:
            return by_casefold[name.casefold()]
    return None


def _metadata_result(url: str, content: bytes, mime: str, rules):
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _result

    values = _query(url) or {}
    identifier = _single_identifier(values, "PeraturanID")
    if mime != "application/json" or len(content) > _JSON_BYTES or not identifier:
        return _result(reason="央行 JDIH 详情必须是受限大小的官方 JSON 响应。")
    try:
        envelope = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _result(reason="央行 JDIH 详情不是有效 JSON，未登记原件。")
    if not isinstance(envelope, dict) or envelope.get("StatusCode") != 200:
        return _result(reason="央行 JDIH 详情接口未确认成功状态。")
    data = envelope.get("Data")
    if not isinstance(data, dict):
        return _result(reason="央行 JDIH 详情缺少结构化法规记录。")
    record_identifier = _field(data, "PeraturanID", "IdPeraturan", "idperaturan", "ID")
    if record_identifier is not None and str(record_identifier) != identifier:
        return _result(reason="央行 JDIH 详情记录编号与请求编号不一致。")
    number = _field(data, "NomorPeraturan")
    title = _field(data, "Judul")
    if not isinstance(number, str) or not number.strip() or not isinstance(title, str) or not title.strip():
        return _result(reason="央行 JDIH 详情缺少法规编号或标题。")

    normalized = {
        "number": _field(data, "NomorPeraturan"),
        "title": _field(data, "Judul"),
        "instrument_type": _field(data, "JenisPeraturanDesc", "JenisPeraturan"),
        "issuer": _field(data, "Penerbit", "NamaInstansi", "NamaPenerbit"),
        "established_date": _field(data, "TanggalPenetapan"),
        "promulgated_date": _field(data, "TanggalPengundangan"),
        "effective_date": _field(data, "TanggalBerlaku"),
        "status": _field(data, "StatusText", "Status"),
        "official_source": _field(data, "Sumber"),
        "legal_basis": _field(data, "DasarHukum"),
        "amends": _field(data, "Mengubah"),
        "repeals": _field(data, "Mencabut"),
        "amended_by": _field(data, "Diubah"),
        "repealed_by": _field(data, "Dicabut"),
        "related_regulations": _field(data, "PeraturanTerkait"),
    }
    record = {
        "provider": "bi_jdih",
        "interpretation": "unreviewed_source_values",
        "fields": {key: _safe_value(value) for key, value in normalized.items() if value is not None},
        "source_values": _safe_value(data),
        "structure": {
            "unit_type": "official_regulation_metadata_candidate",
            "number": number.strip()[:300],
            "title": title.strip()[:1000],
            "instrument_type": _safe_value(normalized["instrument_type"]),
            "parent_titles": [],
            "file_name": f"BI-JDIH-{identifier}.pdf",
            "anchor_class": "",
            "link_mechanism": "read_only_official_metadata_api",
        },
    }
    file_url = f"https://{_HOST}{_FILE_PREFIX}{identifier}"
    return _result(
        matched=True,
        purpose="detail",
        links=[
            {
                "url": file_url,
                "purpose": "document",
                "title": title.strip()[:500],
                "identity_key": f"bi:{identifier}",
                "source_record": record,
            }
        ],
    )


def _catalog_result(url: str, content: bytes, mime: str, rules):
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
        MAX_PAGE_LINKS,
        _ERROR_BODY,
        _ERROR_HEADING,
        _Page,
        _canonical,
        _decode,
        _result,
    )

    if mime not in {"text/html", "application/xhtml+xml"} or len(content) > _CATALOG_BYTES:
        return _result(reason="央行 JDIH 目录必须是 6 MiB 内的 HTML 页面。")
    page = _Page()
    try:
        page.feed(_decode(content))
        page.close()
    except (ValueError, AssertionError, UnicodeError):
        return _result(reason="央行 JDIH 目录结构无法读取，未确认法规记录。")
    title = " ".join(page.title)
    visible = " ".join(page.text)
    heading = " ".join(page.headings[:3])
    if (
        page.challenge
        or _ERROR_HEADING.search(title)
        or _ERROR_HEADING.search(heading)
        or (len(visible) < 1500 and _ERROR_BODY.search(visible))
    ):
        return _result(reason="央行 JDIH 返回验证、登录或错误页面，未登记候选。")

    query = _query(url) or {}
    offset = int(query.get("aitea_offset", ["0"])[0]) if query else 0
    identifiers = []
    seen = set()
    for link in page.links:
        if link.tag != "a" or not link.url or link.url.startswith("#"):
            continue
        try:
            candidate, host = _canonical(link.url, url)
        except ValueError:
            continue
        match = re.fullmatch(
            r"/Web/(?:DaftarPeraturan/Detail|Beranda/DetailPeraturan)/([1-9][0-9]{0,11})",
            urlsplit(candidate).path,
        )
        if host != _HOST or not match or candidate in seen:
            continue
        seen.add(candidate)
        identifier = str(int(match[1]))
        label = (" ".join(link.text) or link.title or f"BI regulation {identifier}")[:500]
        identifiers.append((identifier, label))
    if not identifiers:
        return _result(reason="央行 JDIH 目录没有可核验的法规详情链接。")
    total = len(identifiers)
    batch = identifiers[offset : offset + _CATALOG_BATCH]
    links = [
        {
            "url": f"https://{_HOST}/api/WebJDIH/GetDataWebPeraturan?"
            + urlencode({"PeraturanID": identifier}),
            "purpose": "detail",
            "title": label,
        }
        for identifier, label in batch
    ]
    next_offset = offset + len(batch)
    if next_offset < total and len(links) < rules.max_links:
        links.append(
            {
                "url": f"https://{_HOST}{_CATALOG_PATH}?"
                + urlencode({"aitea_offset": next_offset}),
                "purpose": "catalog",
                "title": "Continue official JDIH catalog",
            }
        )
    if len(links) > rules.max_links or len(links) > MAX_PAGE_LINKS:
        return _result(reason="央行 JDIH 单页候选超过配置上限，未登记。")
    result = _result(matched=True, purpose="catalog", links=links)
    result["scope"] = {
        "provider": "bi-jdih-readonly-api-v1",
        "catalog_url": f"https://{_HOST}{_CATALOG_PATH}",
        "catalog_sha256": hashlib.sha256(content).hexdigest(),
        "offset": offset,
        "total_detail_links": total,
        "page_size": len(batch),
        "returned": len(batch),
        "has_next": next_offset < total,
    }
    return result


def inspect(url: str, content: bytes, mime: str, rules):
    parsed = urlsplit(url)
    current_purpose = purpose(url)
    if current_purpose == "catalog" and parsed.path == _CATALOG_PATH:
        return _catalog_result(url, content, mime, rules)
    if current_purpose == "detail":
        return _metadata_result(url, content, mime, rules)
    if current_purpose == "document":
        from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _result

        if mime == "application/pdf" and content.startswith(b"%PDF-"):
            return _result(matched=True, purpose="document")
        return _result(reason="央行 JDIH 原件响应不是具有 PDF 文件签名的 PDF。")
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _result

    return _result(reason="页面不在央行 JDIH 适配器允许的路径内。")
