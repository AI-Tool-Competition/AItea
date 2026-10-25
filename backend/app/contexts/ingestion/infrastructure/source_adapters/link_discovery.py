"""Bounded discovery from trusted checklist recipes, without legal approval.

These functions do no networking, persistence, parsing or indexing. A discovered
document is a candidate for review, not an assertion about its type or force.
Regular expressions are repository-owned configuration, never model or API input.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from app.contexts.documents.infrastructure.parsers.native_parser import _decode
from app.shared.infrastructure.http.download import _validated_url

MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_BI_JDIH_HTML_BYTES = 6 * 1024 * 1024
MAX_PAGE_LINKS = 10_000
_PATTERN_FIELDS = (
    "catalog_patterns",
    "detail_patterns",
    "document_patterns",
    "html_document_patterns",
    "exclude_patterns",
)
_ADAPTERS = {"bpk", "ma_jdih", "sso", "html_links", "kemenkeu_tax", "bi_jdih", "bi_official", "manual"}
_SETNEG_HOST = "jdih.setneg.go.id"
_SETNEG_API = "https://jdih.setneg.go.id/api/hukumproduk/"
_SETNEG_TYPES = {"1.3": "UU", "1.4": "PERPU", "1.5": "PP", "1.6": "PERPRES"}
_SETNEG_SOURCE_FIELDS = (
    "idperaturan",
    "jns",
    "nama_jenis",
    "no_peraturan",
    "tahun",
    "tentang",
    "teu",
    "tgl_di",
    "diundangkan",
    "ln",
    "tln",
    "bahasa",
    "tempat",
    "lokasi",
    "subjek",
    "bidang_hukum",
    "status",
    "status_hukum",
    "keterangan",
    "terkait",
    "mengubah",
    "mencabut",
    "diubah",
    "dicabut",
    "diubah_sebagian",
    "dicabut_sebagian",
    "mngbh_s",
    "mcbt_s",
    "mencabutsebagian",
    "mengubahsebagian",
)
_COURT_HOST = "kepaniteraan.mahkamahagung.go.id"
_COURT_CATEGORIES = {"1.9": "16", "2.1": "10", "2.3": "57"}
_LKPP_HOST = "jdih.lkpp.go.id"
_LKPP_FILES = "https://jdih.lkpp.go.id/admin/uploads/documents"
_BKPM_HOST = "jdih.bkpm.go.id"
_BKPM_FILES_HOST = "jdih-storage.bkpm.go.id"
_BKPM_API = "https://jdih.bkpm.go.id/api/document-search"
_DEPHUB_HOST = "jdih.dephub.go.id"
_PDPC_HOST = "www.pdpc.gov.sg"
_IPOS_HOST = "www.ipos.gov.sg"
_SG_OFFICIAL_HOSTS = {
    "www.sfa.gov.sg",
    "www.iras.gov.sg",
    "www.customs.gov.sg",
    "www.pdpc.gov.sg",
}
_PDPC_GUIDANCE = "/organisations/regulations-decisions/regulatory-guidance"
_PDPC_GUIDES = (
    "advisory-guidelines-on-key-concepts-in-the-personal-data-protection-act",
    "advisory-guidelines-on-the-personal-data-protection-act-for-selected-topics",
    "advisory-guidelines-on-the-do-not-call-provisions",
    "advisory-guidelines-on-requiring-consent-for-marketing-purposes",
)
_PDPC_INTERMEDIARIES = "/organisations/resources/guidance-by-topic/guide-to-managing-data-intermediaries"
_CUSTOMS_TEXT_PATHS = (
    "/doing-business/import-operations/import-operations-overview/",
    "/doing-business/import-operations/import-procedures/import-procedures-overview/",
    "/doing-business/import-operations/import-procedures/types-of-import-permits/",
    "/doing-business/import-operations/import-procedures/controlled-and-prohibited-goods-for-imports/",
    "/doing-business/import-operations/import-procedures/retaining-your-trade-documents/",
    "/doing-business/valuation-duties-and-fees/goods-and-services-tax-gst/",
    "/doing-business/valuation-duties-and-fees/establishing-customs-value/establishing-customs-value-and-methods/",
    "/doing-business/business-resources/hs-classification-of-goods/application-for-customs-ruling-on-classification-of-goods/",
)
_SG_UUID = r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
_PDPC_FLIGHT_BYTES = 2 * 1024 * 1024
_PDPC_FLIGHT_RECORD_BYTES = 1024 * 1024
_PDPC_FLIGHT_RECORDS = 512
_PDPC_FLIGHT_NODES = 50_000
_PDPC_FLIGHT_DEPTH = 64
_PDF_BUTTON = re.compile(
    r"\s*(?:loadPdf|toggleSubMenu)\(this,\s*(['\"])(https://[^'\"<>\s]+\.pdf)\1\)\s*;?\s*",
    re.I,
)
_WTO_SERVICE_FILES = {
    "ID": re.compile(r"Q:/SCHD/(?:GATS-SC/SC43|GATS-EL/EL43)(?:(?:S|R|C)[1-9]\d{0,2})*\.pdf", re.I),
    "SG": re.compile(r"Q:/SCHD/(?:GATS-SC/SC76|GATS-EL/EL76)(?:(?:S|R|C)[1-9]\d{0,2})*\.pdf", re.I),
}
_HIDDEN_TAGS = {"script", "style", "template", "noscript"}
_HTML_NODE_LIMIT = 100_000
_HTML_DEPTH_LIMIT = 128
_SENSITIVE_HTML_ATTRIBUTES = {
    "href",
    "src",
    "data",
    "id",
    "title",
    "aria-label",
    "onclick",
    "data-file-base-url",
    "data-initial-file",
}
_ERROR_HEADING = re.compile(
    r"(?:^|\b)(?:just a moment|access denied|403\s+forbidden|404\s+(?:error|not found)|"
    r"page not found|service unavailable|internal server error|checking your browser|"
    r"attention required|verify (?:that )?you are human|akses\s+ditolak|request\s+rejected|"
    r"sign in|log in|login)(?:\b|$)",
    re.I,
)
_ERROR_BODY = re.compile(
    r"checking your browser|verify (?:that )?you are human|enable javascript and cookies to continue|"
    r"you (?:do not|don't) have permission to access|the page you requested could not be found|"
    r"the requested url was rejected|url yang anda minta ditolak",
    re.I,
)


def _canonical(url: str, base: str | None = None) -> tuple[str, str]:
    # urljoin/urlsplit silently discard some control characters. Reject them
    # before parsing, rather than changing the requested resource.
    if re.search(r"[\\\x00-\x1f\x7f]", url):
        raise ValueError("目录链接包含无效字符。")
    value = urljoin(base, url) if base else url
    if base:
        # Public HTML sometimes contains unescaped filename spaces (Badilum).
        # Encode only the resource path/query; credentials and authority remain
        # untouched for the same strict URL validation as a direct request.
        parsed = urlsplit(value)
        # These exact official hosts have verified HTTPS counterparts despite
        # emitting legacy HTTP links. Never upgrade another origin, credentials
        # or a nonstandard port.
        scheme = parsed.scheme
        if (
            scheme == "http"
            and (
                (
                    parsed.netloc in {"jdih.bandung.go.id", "www.wto.org", "www.iras.gov.sg"}
                    and urlsplit(base).hostname == parsed.netloc
                )
                or (
                    parsed.netloc == "repository.beacukai.go.id"
                    and urlsplit(base).hostname == "peraturan.beacukai.go.id"
                )
            )
        ):
            scheme = "https"
        value = urlunsplit(
            (
                scheme,
                parsed.netloc,
                quote(parsed.path, safe="/%:@!$&'()*+,;=-._~"),
                quote(parsed.query, safe="%=&?/:;+,$@!*'()-._~"),
                "",
            )
        )
    canonical, host = _validated_url(value)
    parsed = urlsplit(canonical)
    decoded = parsed.path
    # Repeated encoding must not escape a configured path after a proxy decodes
    # it. Do not normalize it into a different, more permissive request.
    for _ in range(3):
        decoded = unquote(decoded)
        if re.search(r"[\\\x00-\x1f\x7f]", decoded) or any(x in {".", ".."} for x in decoded.split("/")):
            raise ValueError("目录链接包含无效路径。")
    query = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=100)
    if host == "sso.agc.gov.sg":
        query = [(key, value) for key, value in query if key.casefold() != "provids"]
    query.sort()
    return urlunsplit(("https", parsed.netloc, parsed.path, urlencode(query), "")), host


def _query_values(url: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True, max_num_fields=100):
        key = re.sub(r"\[\d*\]", "[]", key).casefold()
        result.setdefault(key, []).append(value)
    return result


def _pdf_basename(value) -> bool:
    if not isinstance(value, str) or not 5 <= len(value) <= 240 or not value.lower().endswith(".pdf"):
        return False
    decoded = value
    for _ in range(3):
        decoded = unquote(decoded)
        if re.search(r"[/\\:\x00-\x1f\x7f]", decoded) or decoded in {".", ".."}:
            return False
    return not value.startswith(".")


def _bounded_dom_text(node, *, max_chars=1000, max_nodes=2000):
    """Read text-node order with explicit work and output bounds."""
    parts, size, visited = [], 0, 0
    for current in node.traverse(include_text=True):
        visited += 1
        if visited > max_nodes:
            break
        if current.tag != "-text":
            continue
        value = " ".join(current.text(deep=False).split())
        if not value:
            continue
        remaining = max_chars - size
        if remaining <= 0:
            break
        value = value[:remaining]
        parts.append(value)
        size += len(value) + 1
    return " ".join(parts), min(visited, max_nodes)


def _setneg_query(url: str, instrument: str) -> tuple[str, dict]:
    parsed = urlsplit(url)
    values = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=10)
    query = dict(values)
    if len(query) != len(values):
        raise ValueError("Setneg 请求包含重复参数。")
    endpoint = parsed.path.removeprefix("/api/hukumproduk/")
    if endpoint == "pdf":
        if (
            set(query) != {"f", "fl", "l"}
            or query["l"] != "uploads"
            or not re.fullmatch(r"P\d{1,12}", query["fl"])
            or not _pdf_basename(query["f"])
        ):
            raise ValueError("Setneg 原件参数超出已验证范围。")
        return "document", query
    if query.get("jns") != instrument:
        raise ValueError("Setneg 请求的法规类型与清单不一致。")
    if endpoint == "produkhukum":
        if set(query) != {"jns", "length", "start"} or not all(
            re.fullmatch(r"0|[1-9]\d{0,6}", query[key]) for key in ("length", "start")
        ):
            raise ValueError("Setneg 分页参数无效。")
        if not 1 <= int(query["length"]) <= 50 or not 0 <= int(query["start"]) <= 1_000_000:
            raise ValueError("Setneg 分页参数超过上限。")
        return "catalog", query
    if endpoint == "detaildata":
        if (
            set(query) != {"jns", "no", "thn"}
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./() -]{0,39}", query["no"])
            or not re.fullmatch(r"(?:19|20)\d{2}", query["thn"])
        ):
            raise ValueError("Setneg 文书定位参数无效。")
        return "detail", query
    raise ValueError("Setneg 仅允许已验证的只读目录、详情和原件接口。")


def _bkpm_query(url: str) -> dict:
    values = parse_qsl(urlsplit(url).query, keep_blank_values=True, max_num_fields=10)
    query = dict(values)
    if (
        len(query) != len(values)
        or set(query) != {"id_theme", "limit", "page"}
        or query["id_theme"] != "1"
        or not all(re.fullmatch(r"[1-9]\d{0,5}", query[key]) for key in ("limit", "page"))
        or not 1 <= int(query["limit"]) <= 50
        or not 1 <= int(query["page"]) <= 20_000
    ):
        raise ValueError("BKPM 目录仅允许有界的投资主题分页。")
    return query


def request_parameters(url: str, definition: dict) -> dict:
    """Derive public read-only POST bodies from the immutable trusted plan.

    Query parameters identify a replayable queue entry; the real request uses
    the same URL and a body generated here, never an arbitrary stored body.
    """
    current, host = _canonical(url)
    item = definition.get("item", definition)
    wto = recipe_for(item).get("wto_public_catalog", False)
    if wto and host in {"docs.wto.org", "goods-schedules.wto.org"}:
        country = _country(item)
        expected = definition.get("country", definition.get("plan", {}).get("country", country))
        if expected != country or not _Recipe(recipe_for(item)).purpose(current):
            raise ValueError("WTO 请求超出可信成员和公开目录范围。")
        return {}
    if host == "api-treaty-room.kemlu.go.id":
        from app.contexts.ingestion.infrastructure.source_adapters.treaty_room_discovery import purpose

        item = definition.get("item", definition)
        if _country(item) != "ID" or item.get("id") != "5.1" or not recipe_for(item).get("treaty_room"):
            raise ValueError("TreatyRoom 请求没有对应的可信公开国际协定目录配置。")
        kind = purpose(current)
        if kind == "catalog":
            return {"accept_compressed_json": True}
        if kind == "document":
            return {}
        raise ValueError("TreatyRoom 请求不在公开目录和已发布原件路径内。")
    if host == _BKPM_HOST and urlsplit(current).path == "/api/document-search":
        item = definition.get("item", definition)
        if _country(item) != "ID" or item.get("id") != "4.1" or not recipe_for(item).get("bkpm_investment"):
            raise ValueError("BKPM 请求没有对应的可信投资清单配置。")
        _bkpm_query(current)
        return {"accept_compressed_json": True}
    if host != _SETNEG_HOST:
        return {}
    item = definition.get("item", definition)
    instrument = _SETNEG_TYPES.get(item.get("id"))
    if (
        not instrument
        or definition.get("country", definition.get("plan", {}).get("country", _country(item))) != "ID"
        or _country(item) != "ID"
        or host not in recipe_for(item)["allowed_hosts"]
    ):
        raise ValueError("Setneg 请求没有对应的可信印尼清单配置。")
    return setneg_request_parameters(current, instrument)


def setneg_request_parameters(url: str, instrument: str) -> dict:
    """Build a bounded Setneg read-only API request for one approved instrument."""
    if not isinstance(instrument, str) or instrument not in _SETNEG_TYPES.values():
        raise ValueError("Setneg 请求没有对应的可信印尼法规类型。")
    current, host = _canonical(url)
    if host != _SETNEG_HOST:
        raise ValueError("Setneg 请求超出官方主机范围。")
    purpose, query = _setneg_query(current, instrument)
    if purpose == "document":
        return {}
    if purpose == "catalog":
        body = {
            "tentang": "",
            "p_lihan": "semua",
            "jns": [instrument],
            "thn": [],
            "status": "",
            "terx": "All",
            "sortOrder": "desc",
            "length": int(query["length"]),
            "start": int(query["start"]),
        }
    else:
        body = {"jns": instrument, "no": query["no"], "thn": query["thn"], "k": ""}
    return {"json_body": body, "accept_compressed_json": True}


def _setneg_json(url: str, content: bytes, mime: str, rules) -> dict:
    if mime != "application/json" or len(content) > MAX_HTML_BYTES:
        return _result(reason="Setneg 目录必须返回不超过 4 MiB 的 JSON。")
    purpose, query = _setneg_query(url, rules.setneg_type)
    try:
        data = json.loads(content)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return _result(reason="Setneg 返回了无效 JSON，未确认目录。")
    if not isinstance(data, dict):
        return _result(reason="Setneg 响应结构与已验证接口不一致。")

    def row_valid(row):
        return (
            isinstance(row, dict)
            and row.get("jns") == rules.setneg_type
            and isinstance(row.get("idperaturan"), str)
            and re.fullmatch(r"P\d{1,12}", row["idperaturan"])
            and isinstance(row.get("no_peraturan"), str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./() -]{0,39}", row["no_peraturan"])
            and isinstance(row.get("tahun"), str)
            and re.fullmatch(r"(?:19|20)\d{2}", row["tahun"])
            and isinstance(row.get("tentang"), str)
            and len(row["tentang"]) <= 10_000
        )

    def source_record(row, file=None):
        fields, warnings = {}, []
        for key in _SETNEG_SOURCE_FIELDS:
            if key not in row:
                continue
            value = row[key]
            if value is None or type(value) is int:
                fields[key] = value
            elif isinstance(value, str) and len(value) <= 10_000:
                fields[key] = value
            else:
                warnings.append(f"source field {key} omitted: invalid type or over 10000 characters")
        value = {
            "provider": "setneg",
            "fields": fields,
            "interpretation": "unreviewed_source_values",
        }
        if file is not None:
            value["file"] = {
                key: file[key]
                for key in ("realName", "ext", "size")
                if key in file and (isinstance(file[key], str) or type(file[key]) is int)
            }
        if warnings:
            value["warnings"] = warnings
        return value

    links, warnings = [], []
    if purpose == "catalog":
        rows, total = data.get("data"), data.get("jml")
        start, length = int(query["start"]), int(query["length"])
        if (
            not isinstance(total, int)
            or isinstance(total, bool)
            or not 0 <= total <= 1_000_000
            or data.get("jn") != [rules.setneg_type]
            or not isinstance(rows, list)
            or len(rows) != max(0, min(length, total - start))
            or not all(row_valid(row) for row in rows)
            or len({row["idperaturan"] for row in rows}) != len(rows)
        ):
            return _result(reason="Setneg 目录类型、总数、分页行数或文书标识不一致。")
        for row in rows:
            target = (
                _SETNEG_API
                + "detaildata?"
                + urlencode({"jns": rules.setneg_type, "no": row["no_peraturan"], "thn": row["tahun"]})
            )
            links.append(
                {
                    "url": target,
                    "purpose": "detail",
                    "title": f"{row['jns']} {row['no_peraturan']}/{row['tahun']} — {row['tentang']}"[:300],
                    "identity_key": "setneg:" + row["idperaturan"],
                    "source_record": source_record(row),
                }
            )
        if start + len(rows) < total:
            links.append(
                {
                    "url": _SETNEG_API
                    + "produkhukum?"
                    + urlencode({"jns": rules.setneg_type, "length": length, "start": start + len(rows)}),
                    "purpose": "catalog",
                    "title": "Next page",
                }
            )
        if len(links) > rules.max_links:
            return _result(reason="Setneg 单页行数超过候选限额，需减小请求分页。")
        result = _result(matched=True, purpose=purpose, links=links)
        result["scope"] = {
            "provider": "setneg",
            "jns": rules.setneg_type,
            "total": total,
            "offset": start,
            "page_size": length,
            "returned": len(rows),
        }
        return result
    rows, files = data.get("row"), data.get("file")
    if (
        not isinstance(rows, list)
        or len(rows) != 1
        or not row_valid(rows[0])
        or rows[0]["no_peraturan"] != query["no"]
        or rows[0]["tahun"] != query["thn"]
        or not isinstance(files, list)
        or len(files) > 100
    ):
        return _result(reason="Setneg 详情与请求文书或文件结构不一致。")
    row = rows[0]
    seen = set()
    for file in files:
        name = file.get("realName") if isinstance(file, dict) else None
        if not _pdf_basename(name):
            warnings.append("Setneg 文件名或格式超出已验证范围，需人工核对该文件。")
            continue
        target = _SETNEG_API + "pdf?" + urlencode({"f": name, "fl": row["idperaturan"], "l": "uploads"})
        if target in seen:
            continue
        seen.add(target)
        links.append(
            {
                "url": target,
                "purpose": "document",
                "title": name,
                "identity_key": f"setneg:{row['idperaturan']}:{sha256(name.encode()).hexdigest()[:16]}",
                "source_record": source_record(row, file),
            }
        )
    if len(links) > rules.max_links:
        warnings.append("Setneg 文件数超过单页限额，仍有文件未登记。")
        links = links[: rules.max_links]
    if not links:
        warnings.append("详情未取得可确认的正式 PDF 文件；摘要不作为法规原文。")
    return _result(matched=True, purpose="detail", links=links, warnings=warnings)


@dataclass
class _Link:
    url: str
    tag: str
    text: list[str] = field(default_factory=list)
    title: str = ""
    source_locator: dict | None = None
    source_record: dict | None = None


def _rcep_structure(node, title: str, target: str) -> dict:
    normalized = " ".join(title.split())[:500]
    kinds = (
        (r"^preamble\b", "preamble"),
        (r"^chapter\s+\d+\b", "chapter"),
        (r"^annex\s+[\w.-]+\b", "annex"),
        (r"^appendix\b", "appendix"),
        (r"\bschedule\b", "schedule"),
        (r"^protocol\b", "protocol"),
    )
    unit_type = next(
        (kind for pattern, kind in kinds if re.search(pattern, normalized, re.I)), "document_part"
    )
    number_match = re.search(
        r"^(?:Chapter\s+(\d+)|Annex\s+([\w.-]+)|Appendix\s+([\w.-]+))\b", normalized, re.I
    )
    number = next((value for value in number_match.groups() if value), None) if number_match else None
    parents, parent = [], node.parent
    while parent is not None:
        if parent.tag == "li" and "rcep-item" in (parent.attributes.get("class") or "").split():
            child = parent.child
            while child is not None:
                classes = (child.attributes.get("class") or "").split()
                if child.tag == "a" and "rcep-link" in classes:
                    parent_title, _ = _bounded_dom_text(child, max_chars=500, max_nodes=256)
                    if parent_title and parent_title != normalized:
                        parents.append(parent_title)
                    break
                child = child.next
        parent = parent.parent
    parents.reverse()
    path_name = unquote(urlsplit(target).path.rsplit("/", 1)[-1])[:300]
    classes = (node.attributes.get("class") or "")[:200]
    return {
        "unit_type": unit_type,
        "number": number,
        "parent_titles": parents,
        "file_name": path_name,
        "anchor_class": classes,
        "link_mechanism": "verified_literal_pdf_call",
    }


def _ipos_source_record(
    node, link_text: str, target: str, section_title: str | None, *, provider="ipos"
) -> dict:
    fields = {"link_text": link_text[:1000]}
    row = node.parent
    while row is not None and row.tag != "tr":
        row = row.parent
    date_value = summary = None
    if row is not None:
        cells = row.css("td")
        anchor_cell = node.parent
        while anchor_cell is not None and anchor_cell.tag != "td":
            anchor_cell = anchor_cell.parent
        anchor_cell_id = anchor_cell.mem_id if anchor_cell else None
        cell_texts = [_bounded_dom_text(cell, max_chars=2000, max_nodes=1024)[0] for cell in cells]
        if cells:
            possible_date = cell_texts[0]
            if re.fullmatch(r"\d{1,2}\s+[A-Za-z]+\s+\d{4}", possible_date):
                date_value = possible_date
        for cell in cells:
            if cell.mem_id == anchor_cell_id or cell is cells[0]:
                continue
            text_value = _bounded_dom_text(cell, max_chars=4000, max_nodes=2048)[0]
            if text_value:
                summary = text_value[:4000]
        if date_value:
            fields["publication_date"] = date_value
        if summary:
            fields["row_description"] = summary
    listing = node.parent
    while listing is not None and listing.tag != "li":
        listing = listing.parent
    if provider == "acra" and listing is not None:
        listing_text = _bounded_dom_text(listing, max_chars=4000, max_nodes=2048)[0]
        version_match = re.search(r"\bversion\s+([\w.-]+)", listing_text, re.I)
        issue_date_match = re.search(r"\bissued\s+on\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})", listing_text, re.I)
        if version_match:
            fields["listed_version"] = version_match[1]
        if issue_date_match:
            fields["listed_issue_date"] = issue_date_match[1]
    context = " ".join([section_title or "", link_text]).casefold()
    if "registrar" in context and "interpret" in context:
        unit_type = "registrar_interpretation"
    elif "practice direction" in context:
        unit_type = "practice_direction"
    elif "circular" in context:
        unit_type = "circular"
    elif "work manual" in context or "manual" in context:
        unit_type = "manual_section"
    elif "guide" in context or "infopack" in context or "guidance" in context:
        unit_type = "guide"
    elif "form" in context or "statutory declaration" in context:
        unit_type = "form_or_template"
    else:
        unit_type = "document_link"
    number_match = re.search(
        r"\b(?:circular|practice direction)\s+(?:no\.?\s*)?"
        r"(\d+[A-Za-z]?(?:\.?\s+of\s+\d{4})?)",
        link_text,
        re.I,
    )
    number = re.sub(r"\.(?=\s+of\b)", "", number_match[1], flags=re.I) if number_match else None
    parents = [section_title[:300]] if section_title else []
    return {
        "provider": provider,
        "interpretation": "unreviewed_source_values",
        "fields": fields,
        "structure": {
            "unit_type": unit_type,
            "number": number,
            "parent_titles": parents,
            "file_name": unquote(urlsplit(target).path.rsplit("/", 1)[-1])[:300],
            "anchor_class": (node.attributes.get("class") or "")[:200],
            "link_mechanism": "href",
        },
    }


def _wto_html_source_record(node, link_text: str, target: str, page_title: str, headings: list[str]) -> dict:
    listing = node.parent
    while listing is not None and listing.tag != "li":
        listing = listing.parent
    list_text = _bounded_dom_text(listing, max_chars=2000, max_nodes=512)[0] if listing else ""
    source_context = " ".join([page_title, *headings, list_text, link_text])[:5000]
    symbol_match = re.search(
        r"\b(?:GATS/(?:SC|EL)/\d+(?:/(?:Suppl|Rev|Corr)\.\d+)*|WT/[A-Z0-9()/-]+(?:#[A-Z0-9()/.-]+)?)",
        source_context,
        re.I,
    )
    filename = unquote(urlsplit(target).path.rsplit("/", 1)[-1])
    if not filename:
        filename = parse_qs(urlsplit(target).query).get("filename", [""])[0].rsplit("/", 1)[-1]
    unit_type = "schedule_reference" if "schedule" in source_context.casefold() else "legal_text_link"
    return {
        "provider": "wto",
        "interpretation": "unreviewed_source_values",
        "fields": {
            "link_text": link_text[:1000],
            "source_page_title": page_title[:1000],
            "document_heading": headings[-1][:1000] if headings else "",
            "list_context": list_text[:2000],
            "linked_url": target[:2000],
        },
        "structure": {
            "unit_type": unit_type,
            "number": symbol_match[0][:300] if symbol_match else None,
            "parent_titles": [value[:500] for value in headings[-5:]],
            "file_name": filename[:300],
            "anchor_class": (node.attributes.get("class") or "")[:200],
            "link_mechanism": "official_page_link",
        },
    }


def _sg_official_source_record(node, link_text: str, target: str, page_title: str, headings: list[str]):
    listing = row = node.parent
    while listing is not None and listing.tag != "li":
        listing = listing.parent
    while row is not None and row.tag != "tr":
        row = row.parent
    list_text = _bounded_dom_text(listing, max_chars=2000, max_nodes=512)[0] if listing else ""
    row_text = _bounded_dom_text(row, max_chars=3000, max_nodes=768)[0] if row else ""
    publication_date = None
    if row is not None:
        cells = row.css("td")
        if cells:
            possible_date = _bounded_dom_text(cells[0], max_chars=100, max_nodes=64)[0]
            if re.fullmatch(r"\d{1,2}\s+[A-Za-z]+\s+\d{4}", possible_date):
                publication_date = possible_date
    context = " ".join([page_title, *headings, link_text]).casefold()
    if any(term in context for term in ("form", "template", "declaration")):
        unit_type = "form_or_template"
    elif any(term in context for term in ("guideline", "guide", "requirements", "procedure")):
        unit_type = "guideline"
    elif any(term in context for term in ("regulation", "act", "order", "notice")):
        unit_type = "regulatory_document"
    else:
        unit_type = "document_link"
    filename = unquote(urlsplit(target).path.rsplit("/", 1)[-1])[:300]
    fields = {
        "link_text": link_text[:1000],
        "source_page_title": page_title[:1000],
        "document_heading": headings[-1][:1000] if headings else "",
        "list_context": list_text[:2000],
        "table_row_context": row_text[:3000],
        "linked_url": target[:2000],
    }
    if publication_date:
        fields["publication_date"] = publication_date
    return {
        "provider": "sg_official_html",
        "interpretation": "unreviewed_source_values",
        "fields": fields,
        "structure": {
            "unit_type": unit_type,
            "number": None,
            "parent_titles": [value[:500] for value in headings[-5:]],
            "file_name": filename,
            "anchor_class": (node.attributes.get("class") or "")[:200],
            "link_mechanism": "href",
        },
    }


def _sg_official_page_record(url: str, page_title: str, headings: list[str]) -> dict:
    path = unquote(urlsplit(url).path)
    return {
        "provider": "sg_official_html",
        "interpretation": "unreviewed_source_values",
        "fields": {
            "link_text": page_title[:1000],
            "source_page_title": page_title[:1000],
            "document_heading": headings[0][:1000] if headings else "",
            "linked_url": url[:2000],
        },
        "structure": {
            "unit_type": "official_html_page",
            "number": None,
            "parent_titles": [value[:500] for value in headings[:5]],
            "file_name": path.rsplit("/", 1)[-1][:300],
            "anchor_class": "",
            "link_mechanism": "page_url_self_reference",
        },
    }


class _FlightScripts(HTMLParser):
    """Collect literal Next Flight pushes without interpreting JavaScript."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self.parts = None
        self.position = None
        self.script_index = 0

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.script_index += 1
            self.parts = [] if not dict(attrs).get("src") else None
            self.position = self.getpos()

    def handle_data(self, data):
        if self.parts is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.parts is not None:
            body = "".join(self.parts).strip()
            if body.startswith("self.__next_f.push("):
                self.scripts.append((self.script_index, self.position, body))
                if len(self.scripts) > 128:
                    raise ValueError("PDPC Next 数据块超过上限。")
            self.parts = None


def _flight_json(value: str):
    # Limit nesting before json.loads; its own recursion limit is much broader
    # than the verified source protocol. Never accept NaN or Infinity.
    depth = 0
    quoted = escaped = False
    for character in value:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > _PDPC_FLIGHT_DEPTH:
                raise ValueError("PDPC Next 数据深度超过上限。")
        elif character in "]}":
            depth -= 1

    def reject_constant(_):
        raise ValueError("PDPC Next 数据包含无效数值。")

    try:
        return json.loads(value, parse_constant=reject_constant)
    except (RecursionError, json.JSONDecodeError) as exc:
        raise ValueError("PDPC Next 数据不是完整 JSON。") from exc


def _pdpc_next_links(decoded: str, source_hash: str) -> tuple[list[_Link], dict]:
    collector = _FlightScripts()
    collector.feed(decoded)
    collector.close()
    pieces = []
    source_blocks = []
    length = 0
    for index, position, body in collector.scripts:
        match = re.fullmatch(r"self\.__next_f\.push\((.*)\)\s*;?", body, re.S)
        if match is None:
            raise ValueError("PDPC Next 数据包含非字面脚本。")
        value = _flight_json(match[1])
        if not isinstance(value, list) or len(value) != 2 or value[0] != 1 or not isinstance(value[1], str):
            raise ValueError("PDPC Next 数据块协议未核验。")
        payload = value[1].encode("utf-8")
        if len(payload) > _PDPC_FLIGHT_BYTES or length + len(payload) > _PDPC_FLIGHT_BYTES:
            raise ValueError("PDPC Next 数据超过字节上限。")
        source_blocks.append(
            {
                "script_index": index,
                "html_line": position[0],
                "html_column": position[1],
                "literal_script_sha256": sha256(body.encode()).hexdigest(),
                "decoded_payload_sha256": sha256(payload).hexdigest(),
                "decoded_stream_start_byte": length,
                "decoded_stream_end_byte": length + len(payload),
            }
        )
        pieces.append(payload)
        length += len(payload)
    stream = b"".join(pieces)
    links = []
    position = count = nodes = 0
    records = []

    def append_fragment(fragment, locator):
        page = _Page(sg_official_source=True)
        page.feed(fragment)
        page.close()
        if page.challenge or _ERROR_HEADING.search(" ".join(page.headings[:3])):
            raise ValueError("PDPC Next 内容返回验证或错误页。")
        for link in page.links:
            link.source_locator = locator
            links.append(link)
        if page.truncated or len(links) > MAX_PAGE_LINKS:
            raise ValueError("PDPC Next 链接超过扫描上限。")

    while position < len(stream):
        if stream[position : position + 1] == b"\n":
            position += 1
            continue
        count += 1
        if count > _PDPC_FLIGHT_RECORDS:
            raise ValueError("PDPC Next 记录超过上限。")
        start = position
        header = re.match(rb"([0-9a-f]{0,8}):", stream[position:])
        if not header:
            raise ValueError("PDPC Next 记录头未核验。")
        record_id = header[1].decode()
        position += header.end()
        locator = {
            "source_page_sha256": source_hash,
            "record_id": record_id,
            "decoded_record_start_byte": start,
        }
        if stream[position : position + 1] == b"T":
            text_header = re.match(rb"T([0-9a-f]{1,8}),", stream[position:])
            if not text_header:
                raise ValueError("PDPC Next 文本长度未核验。")
            size = int(text_header[1], 16)
            position += text_header.end()
            end = position + size
            if size > _PDPC_FLIGHT_RECORD_BYTES or end > len(stream):
                raise ValueError("PDPC Next 文本截断或超过记录上限。")
            fragment = stream[position:end].decode("utf-8", errors="strict")
            position = end
            locator.update(decoded_record_end_byte=end, fragment_sha256=sha256(fragment.encode()).hexdigest())
            append_fragment(fragment, locator)
            records.append(locator)
            continue
        end = stream.find(b"\n", position)
        if end < 0 or end - position > _PDPC_FLIGHT_RECORD_BYTES:
            raise ValueError("PDPC Next JSON 记录截断或超过上限。")
        payload = stream[position:end].decode("utf-8", errors="strict")
        position = end + 1
        locator["decoded_record_end_byte"] = end
        if payload.startswith(("I[", "HL[")):
            # Module names and resource hints are never document links.
            _flight_json(payload[1:] if payload.startswith("I[") else payload[2:])
            continue
        if payload in {"X", "C"}:
            # Verified empty/control records carry no content or navigation.
            continue
        value = _flight_json(payload)
        pending = [(value, "", 0)]
        while pending:
            value, pointer, depth = pending.pop()
            nodes += 1
            if nodes > _PDPC_FLIGHT_NODES or depth > _PDPC_FLIGHT_DEPTH:
                raise ValueError("PDPC Next JSON 对象数或深度超过上限。")
            if isinstance(value, dict):
                data = value.get("data")
                if isinstance(data, dict) and isinstance(data.get("content"), str):
                    fragment = data["content"]
                    if len(fragment.encode()) > _PDPC_FLIGHT_RECORD_BYTES:
                        raise ValueError("PDPC Next 正文片段超过上限。")
                    anchor = {
                        **locator,
                        "json_pointer": pointer + "/data/content",
                        "fragment_sha256": sha256(fragment.encode()).hexdigest(),
                    }
                    append_fragment(fragment, anchor)
                    records.append(anchor)
                for key, child in value.items():
                    escaped_key = key.replace("~", "~0").replace("/", "~1")
                    pending.append((child, pointer + "/" + escaped_key, depth + 1))
            elif isinstance(value, list):
                # React's literal anchor props are a data record, never a
                # component invocation. Ignore all script/image/src records.
                if len(value) == 4 and value[:2] == ["$", "a"] and isinstance(value[3], dict):
                    href = value[3].get("href")
                    label = value[3].get("children")
                    if isinstance(href, str):
                        links.append(
                            _Link(
                                href,
                                "a",
                                text=[label[:500]] if isinstance(label, str) else [],
                                source_locator={**locator, "json_pointer": pointer + "/3/href"},
                            )
                        )
                pending.extend((child, pointer + f"/{index}", depth + 1) for index, child in enumerate(value))
    if len(links) > MAX_PAGE_LINKS:
        raise ValueError("PDPC Next 链接超过扫描上限。")
    return links, {
        "provider": "pdpc-next-flight-v1",
        "source_page_sha256": source_hash,
        "decoded_stream_sha256": sha256(stream).hexdigest(),
        "decoded_bytes": len(stream),
        "records": count,
        "source_blocks": source_blocks,
        "content_records": records,
    }


class _Page:
    """Extract bounded visible structure from an HTML5 tree without executing scripts."""

    def __init__(
        self,
        *,
        literal_pdf_calls=False,
        lkpp_pdf_files=False,
        ipos_source=False,
        acra_source=False,
        wto_source=False,
        sg_official_source=False,
    ):
        self.parts = []
        self.links: list[_Link] = []
        self.title: list[str] = []
        self.headings: list[str] = []
        self.text: list[str] = []
        self.challenge = False
        self.invalid_markup = False
        self.truncated = False
        self.literal_pdf_calls = literal_pdf_calls
        self.lkpp_pdf_files = lkpp_pdf_files
        self.ipos_source = ipos_source
        self.acra_source = acra_source
        self.wto_source = wto_source
        self.sg_official_source = sg_official_source

    def feed(self, content):
        self.parts.append(content)

    def close(self):
        from selectolax.lexbor import LexborHTMLParser

        source = "".join(self.parts)

        class AttributeAudit(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=False)
                self.duplicates = {}
                self.tag_counts = Counter()

            def handle_starttag(self, tag, attrs):
                tag_index = self.tag_counts[tag]
                self.tag_counts[tag] += 1
                counts = Counter(name.casefold() for name, _ in attrs if name)
                duplicates = {
                    name for name, count in counts.items() if name in _SENSITIVE_HTML_ATTRIBUTES and count > 1
                }
                if duplicates:
                    self.duplicates[(tag, tag_index)] = duplicates

        audit = AttributeAudit()
        audit.feed(source)
        audit.close()
        if any(tag == "script" and "src" in names for (tag, _), names in audit.duplicates.items()):
            self.challenge = True

        tree = LexborHTMLParser(source, scripting=False)
        nodes = tree.css("*")
        if len(nodes) > _HTML_NODE_LIMIT:
            raise ValueError("HTML 标签数量超过目录解析上限。")
        for node in nodes:
            depth, parent = 0, node.parent
            while parent is not None:
                depth += 1
                if depth > _HTML_DEPTH_LIMIT:
                    raise ValueError("HTML 嵌套深度超过目录解析上限。")
                parent = parent.parent

        label_work_remaining = 200_000
        heading_context = {}
        active_headings = {}
        for node in nodes:
            if node.tag in {f"h{level}" for level in range(1, 7)} and not self._has_hidden_parent(node):
                level = int(node.tag[1])
                value, used = _bounded_dom_text(
                    node, max_chars=500, max_nodes=min(1000, label_work_remaining)
                )
                label_work_remaining -= used
                if value:
                    active_headings = {key: text for key, text in active_headings.items() if key < level}
                    active_headings[level] = value
            elif node.tag == "a":
                heading_context[node.mem_id] = [active_headings[key] for key in sorted(active_headings)]
        title = tree.css_first("title")
        if title:
            value, used = _bounded_dom_text(title, max_chars=1000, max_nodes=min(2000, label_work_remaining))
            label_work_remaining -= used
            if value:
                self.title.append(value[:1000])
                self.text.append(value[:1000])
        for node in nodes:
            attrs = node.attributes
            if node.tag == "script":
                script_url = attrs.get("src") or ""
                script_host = urlsplit(script_url).hostname or ""
                if "/cdn-cgi/challenge-platform/" in script_url or (
                    script_host.endswith(".token.awswaf.com")
                    and urlsplit(script_url).path.endswith("/challenge.js")
                ):
                    self.challenge = True
            if (attrs.get("id") or "").casefold() in {
                "challenge-running",
                "cf-challenge-running",
                "cf-error-details",
            }:
                self.challenge = True
            if node.tag in {"h1", "h2", "h3"} and not self._has_hidden_parent(node):
                value, used = _bounded_dom_text(
                    node, max_chars=1000, max_nodes=min(2000, label_work_remaining)
                )
                label_work_remaining -= used
                if value:
                    self.headings.append(value[:1000])

        link_nodes = []
        tag_indexes = Counter()
        for node in tree.css("a,iframe,embed,object"):
            tag_index = tag_indexes[node.tag]
            tag_indexes[node.tag] += 1
            if not self._has_hidden_parent(node):
                link_nodes.append((node, audit.duplicates.get((node.tag, tag_index), set())))

        # Hidden and executable content must not influence link discovery or the
        # short-page/error-page classifier.
        for node in list(tree.css("script,style,template,noscript")):
            node.decompose()
        body = tree.body
        if body:
            visible = " ".join(body.text(deep=True, separator=" ", strip=True).split())
            if visible:
                self.text.append(visible[:MAX_HTML_BYTES])

        for node, duplicates in link_nodes:
            tag, attrs = node.tag, node.attributes
            target_attribute = "href" if tag == "a" else "data" if tag == "object" else "src"
            if target_attribute in duplicates:
                continue
            target = attrs.get(target_attribute)
            if tag == "iframe" and self.lkpp_pdf_files and attrs.get("id") == "file-frame":
                # LKPP publishes its original as literal PDF-viewer attributes.
                # Ambiguous viewer attributes cannot form a candidate.
                name = attrs.get("data-initial-file")
                if (
                    not any(
                        attribute in duplicates
                        for attribute in ("id", "data-file-base-url", "data-initial-file")
                    )
                    and attrs.get("data-file-base-url") == _LKPP_FILES
                    and _pdf_basename(name)
                    and not re.search(r"[%?#]", name)
                ):
                    target = _LKPP_FILES + "/" + quote(name, safe="")
            literal_pdf_call = False
            if not target and tag == "a" and self.literal_pdf_calls:
                # Read only the verified fixed viewer grammar; never execute JS.
                call = None if "onclick" in duplicates else _PDF_BUTTON.fullmatch(attrs.get("onclick") or "")
                target = call[2] if call else None
                literal_pdf_call = call is not None
            if not target:
                continue
            if len(self.links) >= MAX_PAGE_LINKS:
                self.truncated = True
                break
            label = ""
            if "title" not in duplicates:
                label = attrs.get("title") or ""
            if not label and "aria-label" not in duplicates:
                label = attrs.get("aria-label") or ""
            link = _Link(target.strip(" "), tag, title=label[:300])
            if tag == "a":
                anchor_text, used = _bounded_dom_text(
                    node, max_chars=500, max_nodes=min(512, label_work_remaining)
                )
                label_work_remaining -= used
                if anchor_text:
                    link.text.append(anchor_text)
            if literal_pdf_call:
                link_title = anchor_text if tag == "a" and anchor_text else label or target.rsplit("/", 1)[-1]
                link.source_record = {
                    "provider": "rcepsec",
                    "interpretation": "unreviewed_source_values",
                    "fields": {"link_text": link_title[:500]},
                    "structure": _rcep_structure(node, link_title, target),
                }
            elif (self.ipos_source or self.acra_source) and tag == "a":
                link_title = anchor_text if anchor_text else label or target.rsplit("/", 1)[-1]
                headings = heading_context.get(node.mem_id, [])
                section_title = headings[-1] if headings else None
                link.source_record = _ipos_source_record(
                    node,
                    link_title,
                    target,
                    section_title,
                    provider="acra" if self.acra_source else "ipos",
                )
            elif self.wto_source and tag == "a":
                link_title = anchor_text if anchor_text else label or target.rsplit("/", 1)[-1]
                link.source_record = _wto_html_source_record(
                    node,
                    link_title,
                    target,
                    self.title[0] if self.title else "",
                    heading_context.get(node.mem_id, []),
                )
            elif self.sg_official_source and tag == "a":
                link_title = anchor_text if anchor_text else label or target.rsplit("/", 1)[-1]
                link.source_record = _sg_official_source_record(
                    node,
                    link_title,
                    target,
                    self.title[0] if self.title else "",
                    heading_context.get(node.mem_id, []),
                )
            self.links.append(link)

    @staticmethod
    def _has_hidden_parent(node):
        parent = node.parent
        while parent is not None:
            if parent.tag in _HIDDEN_TAGS:
                return True
            parent = parent.parent
        return False


class _Recipe:
    def __init__(self, recipe: dict):
        self.adapter = recipe.get("adapter", "html_links")
        if self.adapter not in _ADAPTERS:
            raise ValueError("未知目录发现适配器。")
        self.hosts = set()
        hosts = recipe.get("allowed_hosts", [])
        if not isinstance(hosts, list) or len(hosts) > 30:
            raise ValueError("目录允许域名配置无效。")
        for value in hosts:
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", value) or ".." in value:
                raise ValueError("目录允许域名必须为完整域名。")
            _, host = _validated_url(f"https://{value}/")
            self.hosts.add(host)
        for key in _PATTERN_FIELDS:
            patterns = recipe.get(key, [])
            if not isinstance(patterns, list) or len(patterns) > 50:
                raise ValueError("目录路径规则配置无效。")
            try:
                if any(not isinstance(x, str) or len(x) > 1024 for x in patterns):
                    raise ValueError("目录路径规则过长。")
                setattr(self, key, [re.compile(x, re.I) for x in patterns])
            except re.error as exc:
                raise ValueError("目录路径规则不是有效正则。") from exc
        scoped = recipe.get("document_patterns_by_host", {})
        if not isinstance(scoped, dict) or len(scoped) > 30:
            raise ValueError("分站原件路径规则配置无效。")
        self.document_patterns_by_host = {}
        for host, patterns in scoped.items():
            if (
                host not in self.hosts
                or not isinstance(patterns, list)
                or len(patterns) > 50
                or any(not isinstance(x, str) or len(x) > 1024 for x in patterns)
            ):
                raise ValueError("分站原件路径规则配置无效。")
            try:
                self.document_patterns_by_host[host] = [re.compile(x, re.I) for x in patterns]
            except re.error as exc:
                raise ValueError("分站原件路径规则不是有效正则。") from exc
        self.court_category = recipe.get("court_category")
        if self.court_category is not None and self.court_category not in _COURT_CATEGORIES.values():
            raise ValueError("法院目录类型配置无效。")
        self.wto_services = recipe.get("wto_services", False)
        if not isinstance(self.wto_services, bool):
            raise ValueError("WTO 服务承诺配置无效。")
        self.wto_member = recipe.get("wto_member", "ID")
        if self.wto_member not in _WTO_SERVICE_FILES:
            raise ValueError("WTO 成员配置无效。")
        self.wto_public_catalog = recipe.get("wto_public_catalog", False)
        if not isinstance(self.wto_public_catalog, bool):
            raise ValueError("WTO 公开目录配置无效。")
        self.bkpm_investment = recipe.get("bkpm_investment", False)
        if not isinstance(self.bkpm_investment, bool):
            raise ValueError("BKPM 投资目录配置无效。")
        self.treaty_room = recipe.get("treaty_room", False)
        if not isinstance(self.treaty_room, bool):
            raise ValueError("TreatyRoom 公开目录配置无效。")
        self.sg_official_2026 = recipe.get("sg_official_2026", False)
        if not isinstance(self.sg_official_2026, bool):
            raise ValueError("新加坡官方新站配置无效。")
        self.dephub_regulations = recipe.get("dephub_regulations", False)
        if not isinstance(self.dephub_regulations, bool):
            raise ValueError("交通部法规目录配置无效。")
        self.kemenkeu_tax = recipe.get("kemenkeu_tax", False)
        if not isinstance(self.kemenkeu_tax, bool):
            raise ValueError("财政部税务法规目录配置无效。")
        self.sso_catalog_2026 = recipe.get("sso_catalog_2026", False)
        if not isinstance(self.sso_catalog_2026, bool):
            raise ValueError("SSO 目录配置无效。")
        self.max_links = recipe.get("max_links", 200)
        if (
            isinstance(self.max_links, bool)
            or not isinstance(self.max_links, int)
            or not 1 <= self.max_links <= 500
        ):
            raise ValueError("单页候选数量必须为 1 至 500。")
        for key in ("include_terms", "exclude_terms", "bpk_types", "bpk_search_terms"):
            values = recipe.get(key, [])
            if (
                not isinstance(values, list)
                or len(values) > 100
                or any(not isinstance(x, str) or len(x) > 200 for x in values)
            ):
                raise ValueError("目录关键词配置无效。")
            setattr(self, key, [x.casefold() for x in values if x])
        self.required_query = recipe.get("required_query", {})
        if not isinstance(self.required_query, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in self.required_query.items()
        ):
            raise ValueError("目录筛选配置无效。")
        self.required_query_by_host = recipe.get("required_query_by_host", {})
        if (
            not isinstance(self.required_query_by_host, dict)
            or len(self.required_query_by_host) > 30
            or any(
                host not in self.hosts
                or not isinstance(values, dict)
                or len(values) > 20
                or any(
                    not isinstance(key, str) or not isinstance(value, str) for key, value in values.items()
                )
                for host, values in self.required_query_by_host.items()
            )
        ):
            raise ValueError("分站目录筛选配置无效。")
        self.reason = recipe.get("reason")
        self.setneg_type = recipe.get("setneg_type")
        if self.setneg_type is not None and self.setneg_type not in _SETNEG_TYPES.values():
            raise ValueError("Setneg 法规类型配置无效。")

    @staticmethod
    def matches(url: str, patterns) -> bool:
        parsed = urlsplit(url)
        target = unquote(parsed.path) + ("?" + unquote(parsed.query) if parsed.query else "")
        return any(pattern.search(target) for pattern in patterns)

    def purpose(self, url: str) -> str | None:
        parsed = urlsplit(url)
        if parsed.hostname not in self.hosts or self.matches(url, self.exclude_patterns):
            return None
        if self.adapter == "bi_jdih":
            from app.contexts.ingestion.infrastructure.source_adapters.bi_jdih_discovery import purpose

            return purpose(url)
        if self.adapter == "bi_official":
            from app.contexts.ingestion.infrastructure.source_adapters.bi_official_discovery import purpose

            return purpose(url)
        if self.sg_official_2026 and parsed.hostname == "isomer-user-content.by.gov.sg":
            # File hosts never inherit the origin's article/catalog patterns.
            return (
                "document"
                if self.matches(url, self.document_patterns_by_host.get(parsed.hostname, []))
                else None
            )
        if (
            self.sg_official_2026
            and parsed.hostname == "www.customs.gov.sg"
            and parsed.path in _CUSTOMS_TEXT_PATHS
            and not parsed.query
        ):
            return "document"
        if self.dephub_regulations and parsed.hostname == _DEPHUB_HOST:
            from app.contexts.ingestion.infrastructure.source_adapters.dephub_discovery import purpose

            return purpose(url)
        if parsed.hostname == "jdih.kemenkeu.go.id" and self.kemenkeu_tax:
            from app.contexts.ingestion.infrastructure.source_adapters.kemenkeu_discovery import purpose

            return purpose(url)
        if self.wto_public_catalog and parsed.hostname in {"docs.wto.org", "goods-schedules.wto.org"}:
            from app.contexts.ingestion.infrastructure.source_adapters.wto_discovery import purpose

            # Unhandled GATT originals use the existing official-file rules.
            # A goods/API/viewer path must never fall back to looser patterns.
            value = purpose(url, self.wto_member)
            if value or parsed.hostname == "goods-schedules.wto.org" or parsed.path.startswith("/dol2fe/"):
                return value
        if parsed.hostname == "api-treaty-room.kemlu.go.id":
            from app.contexts.ingestion.infrastructure.source_adapters.treaty_room_discovery import purpose

            return purpose(url) if self.treaty_room else None
        if parsed.hostname == _BKPM_HOST and parsed.path == "/api/document-search":
            if not self.bkpm_investment:
                return None
            try:
                _bkpm_query(url)
            except ValueError:
                return None
            return "catalog"
        if parsed.hostname == _COURT_HOST and parsed.path == "/id/profil-kepaniteraan":
            return _court_purpose(url, self.court_category) if self.court_category else None
        if parsed.hostname == "docs.wto.org" and parsed.path in {
            "/dol2fe/Pages/SS/directdoc.aspx",
            "/dol2fe/Pages/SS/directdoc2.aspx",
        }:
            if not self.wto_services or not _wto_service_file(url, self.wto_member):
                return None
            return "document" if parsed.path.endswith("directdoc2.aspx") else "detail"
        if urlsplit(url).hostname == _SETNEG_HOST:
            if not self.setneg_type:
                return None
            try:
                return _setneg_query(url, self.setneg_type)[0]
            except ValueError:
                return None
        for purpose, patterns in (
            ("catalog", self.catalog_patterns),
            ("document", self.html_document_patterns),
            ("document", self.document_patterns_by_host.get(parsed.hostname, self.document_patterns)),
            ("detail", self.detail_patterns),
        ):
            if self.matches(url, patterns):
                return purpose
        return None

    def query_allowed(self, url: str) -> bool:
        values = _query_values(url)
        required = self.required_query_by_host.get(urlsplit(url).hostname, self.required_query)
        for key, expected in required.items():
            key = re.sub(r"\[\d*\]", "[]", key).casefold()
            if not values.get(key) or any(value != expected for value in values[key]):
                return False
        if self.bpk_search_terms and urlsplit(url).path.casefold().rstrip("/") == "/search":
            terms = values.get("keywords", [])
            return bool(terms) and all(x.casefold() in self.bpk_search_terms for x in terms)
        return True


def _result(*, matched=False, purpose=None, links=None, warnings=None, reason=None):
    return {
        "links": links or [],
        "warnings": warnings or [],
        "matched": matched,
        "purpose": purpose,
        "reason": reason,
    }


def page_body_limit(url: str, mime_type: str, recipe: dict) -> int:
    """Return the smallest body cap for this trusted adapter and page role."""
    mime = mime_type.split(";", 1)[0].strip().casefold()
    if mime in {"text/html", "application/xhtml+xml"}:
        rules = _Recipe(recipe)
        try:
            current, _ = _canonical(url)
        except ValueError:
            return MAX_HTML_BYTES
        if rules.adapter == "bi_jdih" and rules.purpose(current) == "catalog":
            return MAX_BI_JDIH_HTML_BYTES
        if rules.adapter == "bi_official" and rules.purpose(current) in {"catalog", "detail"}:
            from app.contexts.ingestion.infrastructure.source_adapters.bi_official_discovery import (
                MAX_HTML_BYTES as MAX_BI_OFFICIAL_HTML_BYTES,
            )

            return MAX_BI_OFFICIAL_HTML_BYTES
    return MAX_HTML_BYTES


def _identity(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname == "sso.agc.gov.sg":
        match = re.match(r"^/(Act|SL)/([^/]+)", parsed.path, re.I)
        if match:
            return f"sso:{match[1].lower()}:{match[2].lower()}"
    return None


def _court_purpose(url: str, category: str) -> str | None:
    try:
        values = parse_qsl(urlsplit(url).query, keep_blank_values=True, max_num_fields=10)
    except ValueError:
        return None
    query = dict(values)
    if len(query) != len(values) or not set(query) <= {"id", "view", "catid", "start", "limit", "limitstart"}:
        return None
    for key in ("start", "limit", "limitstart"):
        if key in query and (not re.fullmatch(r"0|[1-9]\d{0,6}", query[key]) or int(query[key]) > 1_000_000):
            return None
    if query.get("view") == "category" and query.get("id") == category and "catid" not in query:
        return "catalog"
    if (
        query.get("view") == "article"
        and re.fullmatch(r"[1-9]\d{0,8}(?::[a-z0-9-]{1,400})?", query.get("id", ""), re.I)
        and re.fullmatch(re.escape(category) + r"(?::[a-z0-9-]{1,100})?", query.get("catid", ""), re.I)
    ):
        return "detail"
    return None


def _wto_service_file(url: str, member="ID") -> str | None:
    try:
        values = parse_qsl(urlsplit(url).query, keep_blank_values=True, max_num_fields=10)
    except ValueError:
        return None
    query = dict(values)
    filename = query.get("filename", "")
    if (
        len(query) != len(values)
        or not set(query) <= {"filename", "Open"}
        or ("Open" in query and query["Open"] != "True")
        or not _WTO_SERVICE_FILES[member].fullmatch(filename)
    ):
        return None
    return filename.upper().removesuffix(".PDF") + ".pdf"


def _wto_legacy_document(url: str, member="ID") -> str | None:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.netloc.casefold() != "docsonline.wto.org"
        or parsed.path != "/imrd/directdoc.asp"
    ):
        return None
    schedule = "43" if member == "ID" else "76"
    match = re.fullmatch(
        rf"DDFDocuments/t/SCHD/((?:GATS-SC/SC{schedule}|GATS-EL/EL{schedule})(?:(?:S|R|C)[1-9]\d{{0,2}})*)\.wpf",
        unquote(parsed.query),
        re.I,
    )
    if not match:
        return None
    return "https://docs.wto.org/dol2fe/Pages/SS/directdoc2.aspx?" + urlencode(
        {"filename": "Q:/SCHD/" + match[1].upper() + ".pdf", "Open": "True"}
    )


def _bkpm_json(url: str, content: bytes, mime: str, rules) -> dict:
    if mime != "application/json" or len(content) > MAX_HTML_BYTES:
        return _result(reason="BKPM 目录必须返回不超过 4 MiB 的 JSON。")
    query = _bkpm_query(url)
    try:
        data = json.loads(content)
        rows = data["allDocuments"]
        total = data["v_peraturan_prior_aggregate"]["aggregate"]["count"]
    except (ValueError, UnicodeDecodeError, RecursionError, KeyError, TypeError):
        return _result(reason="BKPM 返回了无效目录结构。")
    page, length = int(query["page"]), int(query["limit"])
    offset = (page - 1) * length
    if (
        type(total) is not int
        or not 0 <= total <= 1_000_000
        or not isinstance(rows, list)
        or len(rows) != max(0, min(length, total - offset))
        or not all(
            isinstance(row, dict)
            and type(row.get("id")) is int
            and 0 < row["id"] <= 1_000_000
            and type(row.get("id_theme")) is int
            and row["id_theme"] == 1
            and isinstance(row.get("title"), str)
            and 0 < len(row["title"]) <= 10_000
            for row in rows
        )
        or len({row["id"] for row in rows}) != len(rows)
    ):
        return _result(reason="BKPM 投资主题、总数、分页行数或文书标识不一致。")
    links, warnings, documents = [], [], []
    for row in rows:
        try:
            target, host = _canonical(row.get("document_file_url") or "")
        except (ValueError, TypeError):
            target, host = "", ""
        if (
            row.get("document_type") != "Peraturan"
            or host != _BKPM_FILES_HOST
            or rules.purpose(target) != "document"
        ):
            warnings.append(f"BKPM 文书 {row['id']} 未提供受信法规原件，保留缺口。")
            continue
        identity = f"bkpm:{row['id']}"
        source_fields = {
            "id": row["id"],
            "id_theme": row["id_theme"],
            "title": row["title"],
            "document_type": row["document_type"],
            "document_file_url": row["document_file_url"],
            "verified_original_url": target,
        }
        links.append(
            {
                "url": target,
                "purpose": "document",
                "title": row["title"][:300],
                "identity_key": identity,
                "source_record": {
                    "provider": "bkpm",
                    "interpretation": "unreviewed_source_values",
                    "fields": source_fields,
                },
            }
        )
        metadata = {"identity_key": identity, "original_url": target, "title": row["title"][:1000]}
        slug = row.get("slug")
        if isinstance(slug, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) and len(slug) <= 500:
            metadata["detail_url"] = f"https://{_BKPM_HOST}/id/document/{slug}"
        for name in (
            "regulation_number",
            "regulation_type",
            "establishment_date",
            "publication_date",
            "status",
        ):
            value = row.get(name)
            if isinstance(value, str) and len(value) <= 200:
                metadata[name] = value
                source_fields[name] = value
        slug = row.get("slug")
        if isinstance(slug, str) and re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug) and len(slug) <= 500:
            source_fields["slug"] = slug
        links[-1]["source_record"]["fields"] = source_fields
        documents.append(metadata)
    if offset + len(rows) < total:
        links.append(
            {
                "url": _BKPM_API + "?" + urlencode({"id_theme": "1", "limit": length, "page": page + 1}),
                "purpose": "catalog",
                "title": "Next page",
            }
        )
    if len(links) > rules.max_links:
        return _result(reason="BKPM 单页行数超过候选限额，需减小请求分页。")
    result = _result(matched=True, purpose="catalog", links=links, warnings=warnings)
    result["scope"] = {
        "provider": "bkpm",
        "theme_id": 1,
        "total": total,
        "offset": offset,
        "page_size": length,
        "returned": len(rows),
        "documents": documents,
    }
    return result


def discover_page(url: str, content: bytes, mime_type: str, recipe: dict) -> dict:
    """Inspect one already downloaded page; never treat a match as scope review."""
    rules = _Recipe(recipe)
    if rules.adapter == "manual":
        return _result(reason=rules.reason or "该入口需要专用流程或人工核验，未执行通用发现。")
    try:
        current, _ = _canonical(url)
    except ValueError:
        return _result(reason="页面 URL 不符合抓取规则。")
    purpose = rules.purpose(current)
    if not purpose or (purpose == "catalog" and not rules.query_allowed(current)):
        return _result(reason="页面不在清单配置的路径或筛选范围内。")
    if not content:
        return _result(reason="页面为空，不能确认原文。")
    mime = mime_type.split(";", 1)[0].strip().casefold()
    if rules.adapter == "bi_jdih":
        from app.contexts.ingestion.infrastructure.source_adapters.bi_jdih_discovery import inspect

        return inspect(current, content, mime, rules)
    if rules.adapter == "bi_official":
        from app.contexts.ingestion.infrastructure.source_adapters.bi_official_discovery import inspect

        return inspect(current, content, mime, rules)
    if rules.kemenkeu_tax:
        from app.contexts.ingestion.infrastructure.source_adapters.kemenkeu_discovery import inspect

        return inspect(current, content, mime, rules)
    if rules.wto_public_catalog:
        from app.contexts.ingestion.infrastructure.source_adapters.wto_discovery import inspect

        inspected = inspect(current, content, mime, rules)
        if inspected is not None:
            return inspected
    if mime == "application/pdf" or content.startswith(b"%PDF-"):
        if purpose == "document" and content.startswith(b"%PDF-"):
            result = _result(matched=True, purpose=purpose)
            if urlsplit(current).hostname == "s.mkri.id":
                file_name = unquote(urlsplit(current).path.rsplit("/", 1)[-1])
                result["source_record"] = {
                    "provider": "mkri",
                    "interpretation": "unreviewed_source_values",
                    "fields": {
                        "official_file_url": current,
                        "file_format": "pdf",
                        "content_signature": "verified_pdf_header",
                    },
                    "structure": {
                        "unit_type": "verified_official_original",
                        "number": None,
                        "parent_titles": [],
                        "file_name": file_name[:300],
                        "anchor_class": "",
                        "link_mechanism": "verified_pdf_response",
                    },
                }
            return result
        return _result(reason="PDF 地址、响应类型或文件签名不匹配。")
    if (
        rules.treaty_room
        and urlsplit(current).hostname == "api-treaty-room.kemlu.go.id"
        and purpose == "catalog"
    ):
        from app.contexts.ingestion.infrastructure.source_adapters.treaty_room_discovery import (
            inspect_catalog,
        )

        return inspect_catalog(current, content, mime, rules)
    if urlsplit(current).hostname == _SETNEG_HOST and purpose in {"catalog", "detail"}:
        return _setneg_json(current, content, mime, rules)
    if (
        rules.bkpm_investment
        and urlsplit(current).hostname == _BKPM_HOST
        and purpose == "catalog"
        and urlsplit(current).path == "/api/document-search"
    ):
        return _bkpm_json(current, content, mime, rules)
    if mime not in {"text/html", "application/xhtml+xml"}:
        return _result(reason="该格式不进入通用法规发现；数据集须单独审核。")
    if len(content) > MAX_HTML_BYTES:
        return _result(reason="HTML 页面超过 4 MiB 发现上限。")
    page = _Page(
        literal_pdf_calls=urlsplit(current).hostname == "rcepsec.org",
        lkpp_pdf_files=urlsplit(current).hostname == _LKPP_HOST,
        ipos_source=urlsplit(current).hostname == _IPOS_HOST,
        acra_source=urlsplit(current).hostname == "www.acra.gov.sg",
        wto_source=rules.wto_public_catalog and urlsplit(current).hostname == "www.wto.org",
        sg_official_source=urlsplit(current).hostname in _SG_OFFICIAL_HOSTS,
    )
    try:
        page.feed(_decode(content))
        page.close()
    except (ValueError, AssertionError):
        return _result(reason="页面结构无法读取，未确认原文。")
    title = " ".join(page.title)
    visible = " ".join(page.text)
    heading = " ".join(page.headings[:3])
    if (
        page.challenge
        or _ERROR_HEADING.search(title)
        or _ERROR_HEADING.search(heading)
        or (len(visible) < 1500 and _ERROR_BODY.search(visible))
    ):
        return _result(reason="来源返回验证、登录或错误页面，未取得原文。")
    if rules.wto_services and urlsplit(current).hostname == "docs.wto.org" and purpose == "detail":
        # Docs Online's public viewer redirects using one literal location URL.
        # Require the same official member/document, and do not run its scripts.
        navigation = re.search(r"window\.location\.href\s*=\s*(['\"])([^'\"<>]{1,2048})\1", _decode(content))
        try:
            target, host = _canonical(navigation[2], current) if navigation else ("", "")
        except ValueError:
            target, host = "", ""
        if (
            host != "docs.wto.org"
            or rules.purpose(target) != "document"
            or _wto_service_file(target, rules.wto_member) != _wto_service_file(current, rules.wto_member)
        ):
            return _result(reason="WTO 文档跳转未指向同一成员正式文书，未确认原件。")
        return _result(
            matched=True,
            purpose=purpose,
            links=[
                {
                    "url": target,
                    "purpose": "document",
                    "title": _wto_service_file(target, rules.wto_member).rsplit("/", 1)[-1],
                }
            ],
        )
    if not visible and rules.treaty_room and purpose == "catalog":
        from app.contexts.ingestion.infrastructure.source_adapters.treaty_room_discovery import (
            is_catalog_shell,
        )

        if is_catalog_shell(current):
            result = _result(matched=True, purpose=purpose)
            result["scope"] = {
                "provider": "treaty-room-public",
                "entry_role": "client_rendered_shell",
                "catalog_data_source": "https://api-treaty-room.kemlu.go.id/publik/perjanjian",
                "directory_complete_verified": False,
            }
            return result
    if not visible:
        return _result(reason="页面没有可见内容，可能需要脚本或人工复核。")
    if rules.sso_catalog_2026 and rules.adapter == "sso" and purpose == "catalog":
        from app.contexts.ingestion.infrastructure.source_adapters.sso_discovery import inspect_catalog

        return inspect_catalog(current, content, rules)
    scope = None
    if rules.sg_official_2026 and urlsplit(current).hostname == _PDPC_HOST:
        try:
            next_links, scope = _pdpc_next_links(_decode(content), sha256(content).hexdigest())
        except (ValueError, AssertionError, UnicodeError):
            return _result(reason="PDPC 公开 Next 数据无法有界核验，未确认原件链接。")
        page.links.extend(next_links)
    if purpose == "document" and not rules.matches(current, rules.html_document_patterns):
        return _result(reason="文件地址返回 HTML，未取得配置要求的原件。")
    candidates = []
    seen = {current}
    warnings = []
    for link in page.links:
        if not link.url or link.url.startswith("#"):
            continue
        try:
            target = (
                _wto_legacy_document(link.url, rules.wto_member)
                if rules.wto_services and urlsplit(current).hostname == "www.wto.org"
                else None
            )
            candidate, _ = _canonical(target or link.url, current)
        except ValueError:
            continue
        if candidate in seen:
            continue
        candidate_purpose = rules.purpose(candidate)
        if not candidate_purpose or (link.tag != "a" and candidate_purpose != "document"):
            continue
        label = (" ".join(link.text) or link.title or unquote(urlsplit(candidate).path.rsplit("/", 1)[-1]))[
            :300
        ]
        if (
            rules.dephub_regulations
            and urlsplit(current).hostname == _DEPHUB_HOST
            and candidate_purpose == "detail"
        ):
            from app.contexts.ingestion.infrastructure.source_adapters.dephub_discovery import (
                is_formal_regulation_title,
            )

            if not is_formal_regulation_title(label):
                continue
        context = (label + " " + unquote(candidate)).casefold()
        type_context = (label + " " + unquote(urlsplit(candidate).path)).casefold()
        if candidate_purpose == "catalog":
            if not rules.query_allowed(candidate):
                continue
            if (
                rules.adapter == "bpk"
                and urlsplit(candidate).hostname == "peraturan.bpk.go.id"
                and rules.bpk_types
                and urlsplit(candidate).path.casefold() == "/search"
            ):
                same_type = _query_values(candidate).get("jenis") == _query_values(current).get("jenis")
                if not same_type and not any(term in type_context for term in rules.bpk_types):
                    continue
        else:
            if (
                urlsplit(current).hostname == _COURT_HOST
                and rules.court_category == "16"
                and candidate_purpose == "detail"
                and not re.match(r"(?:perma|peraturan mahkamah agung)\b", label, re.I)
            ):
                # The issuer's PERMA category also contains misplaced SK KMA
                # and SEMA execution guidance. Do not relabel those as PERMA.
                continue
            if (
                urlsplit(current).hostname == "jdih.bandung.go.id"
                and purpose == "catalog"
                and rules.bpk_types
                and not any(label.casefold().startswith(term + " ") for term in rules.bpk_types)
            ):
                # The mixed issuer list can mention a cited Perda inside a
                # Perwali title. Only its leading instrument type selects it.
                continue
            if any(term in context for term in rules.exclude_terms):
                continue
            if rules.include_terms and not any(term in context for term in rules.include_terms):
                # A generic PDF label on an already relevant detail page inherits
                # that page's title; this does not establish a citation or tier.
                if (
                    candidate_purpose != "document"
                    or purpose == "catalog"
                    or not any(term in title.casefold() for term in rules.include_terms)
                ):
                    continue
            if (
                rules.adapter == "bpk"
                and urlsplit(current).hostname == "peraturan.bpk.go.id"
                and rules.bpk_types
                and not any(term in type_context for term in rules.bpk_types)
            ):
                if purpose == "catalog":
                    continue
        seen.add(candidate)
        if len(candidates) >= rules.max_links:
            warnings.append("单页候选达到上限，仍有链接未登记；不能据此认定目录完整。")
            break
        if link.source_record and link.source_record.get("provider") == "wto":
            parsed_candidate = urlsplit(candidate)
            final_name = unquote(parsed_candidate.path.rsplit("/", 1)[-1])
            if not final_name:
                final_name = parse_qs(parsed_candidate.query).get("filename", [""])[0].rsplit("/", 1)[-1]
            link.source_record["fields"]["linked_url"] = candidate[:2000]
            link.source_record["structure"]["file_name"] = final_name[:300]
            if parsed_candidate.path.casefold().endswith(".pdf"):
                link.source_record["fields"]["file_format"] = "pdf"
            elif parsed_candidate.path.casefold().endswith((".doc", ".docx")):
                link.source_record["fields"]["file_format"] = parsed_candidate.path.rsplit(".", 1)[
                    -1
                ].casefold()
        entry = {"url": candidate, "purpose": candidate_purpose, "title": label}
        if link.source_locator:
            entry["source_locator"] = link.source_locator
        if link.source_record:
            entry["source_record"] = link.source_record
        identity = _identity(candidate)
        if identity:
            entry["identity_key"] = identity
        candidates.append(entry)
    if page.truncated:
        warnings.append("页面链接超过扫描上限，需缩小范围或补充适配。")
    if not candidates and purpose != "document":
        warnings.append("未发现配置范围内的子页面或原件；请核对空目录、动态分页及访问限制。")
    result = _result(matched=True, purpose=purpose, links=candidates, warnings=warnings)
    if purpose == "document" and urlsplit(current).hostname in _SG_OFFICIAL_HOSTS:
        result["source_record"] = _sg_official_page_record(current, title, page.headings)
    if scope is not None:
        # Persist only locators for the bounded selected links. Body strings and
        # unrelated navigation/module records are not legal catalog evidence.
        scope.pop("content_records")
        scope["link_records"] = [
            {"url": row["url"], "purpose": row["purpose"], "locator": row["source_locator"]}
            for row in candidates
            if row.get("source_locator")
        ]
        result["scope"] = scope
    return result


def _country(item: dict) -> str:
    for tag in item.get("tags", []):
        if tag.startswith("country:"):
            return tag.partition(":")[2].upper()
    return str(item.get("country", "")).upper()


def normalized_seeds_for(item: dict) -> list[dict]:
    """Reference screenshots are evidence, never additional crawl roots."""
    seeds = []
    seen = set()
    for entry in item.get("entries", []):
        if entry.get("purpose") == "reference":
            continue
        url, _ = _canonical(entry["url"])
        parsed = urlsplit(url)
        if _country(item) == "ID" and item.get("id") == "1.2" and parsed.hostname == "peraturan.bpk.go.id":
            roots = [
                urlunsplit(("https", parsed.netloc, "/Search", urlencode({"keywords": term}), ""))
                for term in ("Ketetapan MPR", "TAP MPR", "TAP MPRS")
            ]
        else:
            query = parse_qsl(parsed.query, keep_blank_values=True)
            if parsed.hostname == "badilum.mahkamahagung.go.id":
                query = [(key, value) for key, value in query if key != "start"]
                url = urlunsplit(("https", parsed.netloc, parsed.path, urlencode(query), ""))
            roots = [url]
        for root in roots:
            if root not in seen:
                seeds.append({"url": root, "purpose": entry.get("purpose", "catalog")})
                seen.add(root)
    return seeds


# Column roots are deliberately explicit. A homepage is not permission to follow
# news, staff, login, or every department linked from an official site.
_SITE_BRANCHES = {
    "www.mkri.id": ("peraturan",),
    "jdih.mkri.id": ("produk-hukum/putusan",),
    "putusan3.mahkamahagung.go.id": (
        "direktori/putusan",
        "direktori/index/kategori",
        "rumusan_rakernas",
        "yurisprudensi",
        "putusan_penting",
    ),
    "badilum.mahkamahagung.go.id": ("publik/peraturan",),
    "jdih.bkpm.go.id": ("id/search", "id/document"),
    "jdih.kemendag.go.id": ("peraturan",),
    "www.pajak.go.id": ("id/peraturan",),
    "jdih.kemnaker.go.id": ("peraturan",),
    "jdih.kppu.go.id": ("dokumen",),
    "www.bi.go.id": ("id/publikasi/peraturan",),
    "jdih.lkpp.go.id": ("regulation",),
    "treaty-room.kemlu.go.id": ("daftar-perjanjian", "perjanjian"),
    "asean.org": (
        "our-communities/economic-community/integration-with-global-economy/asean-china-economic-relation",
    ),
    "www.dfat.gov.au": ("trade/agreements/in-force/rcep",),
    "www.wto.org": ("english/docs_e/legal_e", "english/thewto_e/countries_e", "english/tratop_e/schedules_e"),
    "goods-schedules.wto.org": ("member", "document"),
    "itip-services-worldbank.wto.org": ("SearchResultGats.aspx",),
    "www.customs.gov.sg": (
        "doing-business/compliance",
        "doing-business/duties-and-taxes",
        "businesses/valuation-duties-taxes-fees",
    ),
    "www.sfa.gov.sg": ("food-import-export", "food-information", "food-retail", "legislation"),
    "www.iras.gov.sg": (
        "taxes/goods-services-tax-(gst)",
        "taxes/corporate-income-tax",
        "taxes/international-tax",
        "quick-links/e-tax-guides",
    ),
    "www.acra.gov.sg": ("regulations",),
    "www.ccs.gov.sg": ("consumer-protection", "competition", "legislation", "resources/publications"),
    "www.pdpc.gov.sg": ("guidelines-and-consultation", "overview-of-pdpa", "do-not-call-register"),
    "www.ipos.gov.sg": (
        "about-ip/trade-marks",
        "about-ip/geographical-indications",
        "manage-ip/resolve-ip-disputes",
    ),
    "www.judiciary.gov.sg": ("civil", "small-claims", "practice-directions", "registrars-circulars"),
    "siac.org.sg": ("siac-rules-2025", "siac-rules", "model-clauses", "fees"),
    "www.enterprisesg.gov.sg": ("grow-your-business/go-global/international-agreements",),
}


def recipe_for(item: dict) -> dict:
    """Build trusted patterns from a checked-in checklist item, not API regexes."""
    seeds = normalized_seeds_for(item)
    adapter = item.get("adapter", "manual")
    if item.get("execution") == "dataset_review":
        return {
            "adapter": "manual",
            "reason": "SG-LegalCite 须按 CSV 字段、年份及命题逐项审核；完整文件和长引文不进入通用向量流程。",
        }
    if adapter == "manual":
        return {"adapter": "manual", "reason": "该入口尚需专用适配或人工核验；未开启通用站点遍历。"}
    hosts = sorted({urlsplit(entry["url"]).hostname for entry in seeds} | set(item.get("trusted_hosts", [])))
    result = {
        "adapter": adapter,
        "allowed_hosts": hosts,
        "catalog_patterns": [],
        "detail_patterns": [],
        "document_patterns": [],
        "html_document_patterns": [],
        "exclude_patterns": [],
        "include_terms": [],
        "exclude_terms": [],
        "required_query": {},
        "max_links": 200,
    }
    if adapter == "kemenkeu_tax":
        if (
            _country(item) != "ID"
            or item.get("id") != "4.3"
            or hosts != ["jdih.kemenkeu.go.id"]
            or len(seeds) != 1
            or seeds[0].get("purpose") != "catalog"
        ):
            return {
                "adapter": "manual",
                "reason": "财政部税务法规适配仅允许印尼 4.3 项的官方受限目录入口。",
            }
        result["kemenkeu_tax"] = True
        return result
    if adapter == "bi_jdih":
        if (
            _country(item) != "ID"
            or item.get("id") != "4.6"
            or hosts != ["jdih.bi.go.id"]
            or len(seeds) != 1
            or seeds[0].get("url") != "https://jdih.bi.go.id/Web/DaftarPeraturan"
            or seeds[0].get("purpose") != "catalog"
        ):
            return {"adapter": "manual", "reason": "央行 JDIH 适配仅允许印尼 4.6 项的官方法规目录入口。"}
        return result | {"max_links": 200}
    if adapter == "bi_official":
        if (
            _country(item) != "ID"
            or item.get("id") != "4.6"
            or hosts != ["www.bi.go.id"]
            or len(seeds) != 1
            or seeds[0].get("url")
            != "https://www.bi.go.id/id/publikasi/peraturan/default.aspx?Kategori=peraturan+bank+indonesia"
            or seeds[0].get("purpose") != "catalog"
        ):
            return {"adapter": "manual", "reason": "央行官网法规适配仅允许印尼 4.6 项的官方受限目录入口。"}
        return result | {"max_links": 100}
    wto_member = _country(item)
    if (wto_member, item.get("id")) in {("ID", "5.4"), ("SG", "5.2")} and item.get("limits", {}).get(
        "wto_public_catalog"
    ) is True:
        result["wto_public_catalog"] = True
        result["wto_services"] = True
        result["wto_member"] = wto_member
    if adapter == "bpk":
        result.update(
            catalog_patterns=[r"^/Search(?:\?|$)"]
            + [rf"^{re.escape(urlsplit(x['url']).path)}(?:\?|$)" for x in seeds if "/Jenis/" in x["url"]],
            detail_patterns=[r"^/Details/\d+/[^/?]+(?:\?|$)"],
            document_patterns=[r"^/(?:Home/)?Download/\d+/[^?]+(?:\?|$)"],
        )
        jenis = {
            _query_values(entry["url"]).get("jenis", [None])[0]
            for entry in seeds
            if urlsplit(entry["url"]).hostname == "peraturan.bpk.go.id"
        }
        if len(jenis) == 1 and None not in jenis:
            result["required_query"] = {"jenis": jenis.pop()}
        if _country(item) == "ID" and item.get("id") == "1.8" and _DEPHUB_HOST in hosts:
            result["dephub_regulations"] = True
            result.setdefault("required_query_by_host", {})[_DEPHUB_HOST] = {}
        if item.get("id") == "1.2":
            result["bpk_types"] = ["ketetapan mpr", "ketetapan mprs", "tap mpr", "tap mprs"]
            result["bpk_search_terms"] = ["Ketetapan MPR", "TAP MPR", "TAP MPRS"]
        elif item.get("id") == "1.7":
            result["bpk_types"] = ["peraturan daerah", "perda", "qanun"]
        elif item.get("id") == "1.8":
            result["bpk_types"] = ["peraturan"]
        elif item.get("id") == "1.10":
            result["bpk_types"] = [
                "peraturan gubernur",
                "pergub",
                "peraturan bupati",
                "perbup",
                "peraturan walikota",
                "peraturan wali kota",
                "perwali",
            ]
        if "jdih.bandung.go.id" in hosts and item.get("id") in {"1.7", "1.10"}:
            # An issuer-specific supplement, not a substitute for the national
            # catalog. Existing Perda/Perwali term filters separate mixed rows.
            branch = r"/(?:index\.php/)?home/produk-hukum/peraturan-perundang-undangan-daerah"
            result["catalog_patterns"].append(rf"^{branch}/?(?:\?|$)")
            result["detail_patterns"].append(rf"^{branch}/\d+(?:\?|$)")
            result["document_patterns"].append(r"^/(?:index\.php/)?media/\d+\?download=true$")
            result["required_query_by_host"] = {"jdih.bandung.go.id": {}}
        if _SETNEG_HOST in hosts and _country(item) == "ID" and item.get("id") in _SETNEG_TYPES:
            result["setneg_type"] = _SETNEG_TYPES[item["id"]]
            result.setdefault("required_query_by_host", {})[_SETNEG_HOST] = {"jns": result["setneg_type"]}
        return result
    if adapter == "ma_jdih":
        result.update(
            catalog_patterns=[r"^/(?:index\.php/)?dokumen/?(?:\?|$)"],
            detail_patterns=[r"^/(?:index\.php/)?legal-product/[^/]+/detail/?(?:\?|$)"],
            document_patterns=[
                r"^/(?:index\.php/)?legal-product/[^/]+/download/[^/?]+(?:\?|$)",
                r"^/(?:storage|uploads?)/[^?]+\.pdf(?:\?|$)",
            ],
        )
        for seed in seeds:
            for key, values in _query_values(seed["url"]).items():
                if key == "jenis[]" and len(values) == 1:
                    result["required_query"][key] = values[0]
        if _COURT_HOST in hosts and _country(item) == "ID" and item.get("id") in _COURT_CATEGORIES:
            result["court_category"] = _COURT_CATEGORIES[item["id"]]
            result["required_query_by_host"] = {
                _COURT_HOST: {},
            }
            if "jdih.mahkamahagung.go.id" in hosts:
                result["required_query_by_host"]["jdih.mahkamahagung.go.id"] = result["required_query"]
            files = {
                "1.9": [r"^/images/peraturan/(?:perma/[^/?]+|sema/perma[^/?]*)\.pdf$"],
                "2.1": [r"^/images/peraturan/(?:sema/(?:sema|lampiran[^/?]*sema)[^/?]*|sema[^/?]*)\.pdf$"],
                "2.3": [r"^/images/(?:peraturan|buku|artikel)/[^?]+\.pdf$"],
            }
            result["document_patterns_by_host"] = {_COURT_HOST: files[item["id"]]}
        return result
    if adapter == "sso":
        if item.get("limits", {}).get("sso_catalog_2026") is True:
            result["sso_catalog_2026"] = True
        category = "SL" if any("/browse/sl/" in x["url"].casefold() for x in seeds) else "Act"
        result.update(
            catalog_patterns=[
                rf"^/Browse/{category}/(?:Current|Uncommenced|Repealed|Revoked|Spent|RepealedSpent|RevokedSpent)(?:/[^?]*)?(?:\?|$)"
            ],
            detail_patterns=[
                rf"^/{category}/[^/?]+/(?:Uncommenced|Repealed|Revoked|Spent)(?:/[^?]*)?(?:\?|$)"
            ],
            html_document_patterns=[rf"^/{category}/[^/?]+(?:/\d{{8}})?/?(?:\?|$)"],
        )
        for seed in seeds:
            for key, value in parse_qsl(urlsplit(seed["url"]).query):
                if key.casefold() == "subtype":
                    result["required_query"][key] = value
        return result
    if _country(item) == "SG":
        from app.contexts.ingestion.infrastructure.source_adapters.singapore_discovery import scope_recipe

        result["exclude_patterns"] = [
            r"(?:^|/)(?:login|logout|sign-in|careers|contact-us|sitemap|news|news-events|press-releases|events)(?:/|\?|$)",
            r"(?:^|/)(?:privacy-policy|terms-of-use)(?:/|\?|$)",
        ]
        result = scope_recipe(item, result)
        if item.get("id") in {"3.1", "3.6"} and item.get("limits", {}).get("sg_official_2026") is True:
            result["sg_official_2026"] = True
            for seed in seeds:
                if seed["purpose"] in {"detail", "document"}:
                    # Earlier generic SG recipes treated every seed as a
                    # catalog. New explicit source roles keep their own type.
                    catalog = rf"^{re.escape(urlsplit(seed['url']).path)}/?(?:\?|$)"
                    if catalog in result["catalog_patterns"]:
                        result["catalog_patterns"].remove(catalog)
            if item["id"] == "3.6":
                guide_paths = [_PDPC_GUIDANCE + "/" + slug for slug in _PDPC_GUIDES]
                result["detail_patterns"] += [
                    rf"^{re.escape(path)}/?$" for path in guide_paths + [_PDPC_INTERMEDIARIES]
                ]
                result["document_patterns_by_host"] = {
                    _PDPC_HOST: result["document_patterns"] + [rf"^/assets/{_SG_UUID}$"],
                }
                result["catalog_patterns"] += [rf"^{re.escape(_PDPC_GUIDANCE)}/?$"]
            else:
                result["html_document_patterns"] += [rf"^{re.escape(path)}$" for path in _CUSTOMS_TEXT_PATHS]
                for host in ("isomer-user-content.by.gov.sg",):
                    if host in result["allowed_hosts"]:
                        result.setdefault("document_patterns_by_host", {})[host] = [
                            rf"^/174/{_SG_UUID}/[^/?]+\.pdf$"
                        ]
        if item.get("id") == "5.2":
            result["wto_services"] = True
            result["wto_member"] = "SG"
            result["catalog_patterns"] += [
                r"^/member/singapore/?(?:\?|$)",
                r"^/browse/\d+/file_browser/?(?:\?|$)",
            ]
            result["detail_patterns"] += [r"^/english/docs_e/legal_e/[^/?]+\.htm(?:\?|$)"]
            result["document_patterns_by_host"] = {
                "www.wto.org": [
                    r"^/english/docs_e/legal_e/downloads_e/[a-z0-9_-]+\.pdf(?:\?|$)",
                    r"^/english/tratop_e/serv_e/telecom_e/sc76\.pdf$",
                ],
                "goods-schedules.wto.org": [r"^/system/files/\d{4}-\d{2}/[a-z0-9_.-]+\.pdf(?:\?|$)"],
                "docs.wto.org": [r"^/gattdocs/q/(?:[a-z0-9_-]+/)+[a-z0-9_-]+\.pdf(?:\?|$)"],
            }
            if result.get("wto_public_catalog"):
                result["document_patterns_by_host"]["www.wto.org"].append(
                    r"^/english/docs_e/legal_e/[a-z0-9_-]+\.pdf(?:\?|$)"
                )
                result["document_patterns_by_host"]["www.wto.org"].append(
                    r"^/english/docs_e/legal_e/(?:downloads_e/)?[a-z0-9_-]+\.(?:doc|docx)(?:\?|$)"
                )
        return result
    for seed in seeds:
        path = urlsplit(seed["url"]).path
        purpose = seed.get("purpose", "catalog")
        result[f"{purpose}_patterns"].append(rf"^{re.escape(unquote(path))}/?(?:\?|$)")
    for host in hosts:
        if host == "www.mkri.id" and _country(item) == "ID" and item.get("id") == "3.1":
            result["required_query_by_host"] = {"www.mkri.id": {"jenis": "PUU"}}
            continue
        if host == "goods-schedules.wto.org":
            # Member roots come only from the checklist seeds above. A generic
            # /member branch would also crawl other countries and ZIP bundles.
            result["catalog_patterns"].append(r"^/browse/\d+/file_browser/?(?:\?|$)")
            continue
        if host == "www.wto.org":
            result["detail_patterns"].append(r"^/english/docs_e/legal_e/[^/?]+\.htm(?:\?|$)")
            continue
        if host == "jdih.lkpp.go.id":
            result["catalog_patterns"].extend(
                [
                    r"^/regulation/(?:index|search)/?(?:\?|$)",
                    r"^/regulation/year/\d{4}/?(?:\?|$)",
                    r"^/regulation/(?:peraturan|keputusan|surat-edaran|instruksi)[a-z0-9-]*/?(?:\?|$)",
                ]
            )
            result["detail_patterns"].append(r"^/regulation/[a-z0-9-]+/[a-z0-9-]+/?(?:\?|$)")
            continue
        if host == "putusan3.mahkamahagung.go.id":
            # MA's category pages and case metadata are discovery evidence.
            # Neither is a complete judgment, even when it contains a summary.
            category_roots = [
                urlsplit(seed["url"]).path.removesuffix(".html")
                for seed in seeds
                if urlsplit(seed["url"]).path.startswith("/direktori/index/kategori/")
            ]
            if category_roots:
                result["catalog_patterns"].extend(
                    rf"^{re.escape(path)}(?:/page/\d+)?\.html(?:\?|$)" for path in category_roots
                )
            else:
                result["catalog_patterns"].extend(
                    [
                        r"^/direktori/index/kategori(?:/[^?]*)?(?:\?|$)",
                        r"^/(?:yurisprudensi|putusan_penting|rumusan_rakernas)(?:/page/\d+)?(?:\.html|/)?(?:\?|$)",
                    ]
                )
            result["detail_patterns"].extend(
                [
                    r"^/direktori/putusan/[^/?]+\.html(?:\?|$)",
                    r"^/(?:yurisprudensi|putusan_penting|rumusan_rakernas)/[^?]+(?:\?|$)",
                ]
            )
            continue
        for branch in _SITE_BRANCHES.get(host, ()):
            if host == "www.mkri.id" and _country(item) == "ID" and item.get("id") == "1.1":
                # The constitution row must not fan out into other MK rules.
                continue
            result["catalog_patterns"].append(rf"^/{re.escape(branch)}/?(?:\?|$)")
            # A scoped URL proves relevance, not completeness of the HTML body.
            # Unverified generic pages remain details until an explicit full-text
            # adapter can distinguish original articles from lists and summaries.
            result["detail_patterns"].append(rf"^/{re.escape(branch)}/[^?]+(?:\?|$)")
    # Only official file locations, never a catch-all file extension at any URL.
    if (
        _country(item) == "ID"
        and item.get("id") == "5.1"
        and "api-treaty-room.kemlu.go.id" in hosts
        and any(
            urlsplit(seed["url"]).hostname == "api-treaty-room.kemlu.go.id"
            and urlsplit(seed["url"]).path == "/publik/perjanjian"
            for seed in seeds
        )
    ):
        result["treaty_room"] = True
    if (
        _country(item) == "ID"
        and item.get("id") == "4.1"
        and _BKPM_FILES_HOST in hosts
        and any(urlsplit(seed["url"]).path == "/api/document-search" for seed in seeds)
    ):
        result["bkpm_investment"] = True
        result.setdefault("document_patterns_by_host", {})[_BKPM_FILES_HOST] = [
            r"^/jdih/(?:jdih|userfiles)/[^?]+\.pdf$"
        ]
    result["document_patterns"].append(
        r"^/(?:sites/default/files|files|uploads?|storage|media|docs|documents|wp-content/uploads|assets)/[^?]+\.pdf(?:\?|$)"
    )
    if "putusan3.mahkamahagung.go.id" in hosts:
        result["document_patterns"].append(r"^/direktori/download_file/[^/?]+/[^/?]+(?:\?|$)")
    if "s.mkri.id" in hosts and item.get("id") == "3.1" and _country(item) == "ID":
        result["document_patterns"].append(r"^/public/content/persidangan/putusan/[^/?]+\.pdf(?:\?|$)")
    elif "s.mkri.id" in hosts:
        result["document_patterns"].append(r"^/public/content/infoumum/regulation/pdf/[^?]+\.pdf(?:\?|$)")
    if "badilum.mahkamahagung.go.id" in hosts:
        result["document_patterns"].append(r"^/attachments/article/\d+/[^/?]+\.pdf(?:\?|$)")
    if "jdih.lkpp.go.id" in hosts:
        result["document_patterns"].append(r"^/regulation/download/[a-z0-9-]+/\d+(?:\?|$)")
        if _country(item) == "ID" and item.get("id") == "4.7":
            result.setdefault("document_patterns_by_host", {})[_LKPP_HOST] = [
                r"^/regulation/download/[a-z0-9-]+/\d+(?:\?|$)",
                r"^/admin/uploads/documents/[^/?]+\.pdf$",
            ]
    if "www.wto.org" in hosts:
        result["document_patterns"].append(r"^/english/docs_e/legal_e/downloads_e/[a-z0-9_-]+\.pdf(?:\?|$)")
        if _country(item) == "ID" and item.get("id") == "5.4":
            result["wto_services"] = True
            result["document_patterns_by_host"] = {
                "www.wto.org": result["document_patterns"]
                + [r"^/english/tratop_e/serv_e/telecom_e/sc43\.pdf$"]
            }
            if result.get("wto_public_catalog"):
                result["document_patterns_by_host"]["www.wto.org"].append(
                    r"^/english/docs_e/legal_e/[a-z0-9_-]+\.pdf(?:\?|$)"
                )
                result["document_patterns_by_host"]["www.wto.org"].append(
                    r"^/english/docs_e/legal_e/(?:downloads_e/)?[a-z0-9_-]+\.(?:doc|docx)(?:\?|$)"
                )
    if "docs.wto.org" in hosts:
        result["document_patterns"].append(r"^/gattdocs/q/(?:[a-z0-9_-]+/)+[a-z0-9_-]+\.pdf(?:\?|$)")
    if "goods-schedules.wto.org" in hosts:
        result["document_patterns"].append(r"^/system/files/\d{4}-\d{2}/[a-z0-9_.-]+\.pdf(?:\?|$)")
    if _country(item) == "ID" and item.get("id") in {"5.2", "5.3"}:
        scoped = result.setdefault("document_patterns_by_host", {})
        if "fta.mofcom.gov.cn" in hosts and item["id"] == "5.3":
            scoped["fta.mofcom.gov.cn"] = [
                r"^/upload/agreementFiles/rcep/rceppdf/(?:xy_en|d(?:[1-9]|1\d|20)z(?:_fj\d*)?_en|"
                r"00 General Notes_en|26 Headnotes - ID|29 Schedules - ID for CN|33 Appendix - ID|"
                r"08 ID's Annex III \(Services\)|09 ID's Annex III \(Investment\)|08 ID's Annex IV)\.pdf$"
            ]
        if "rcepsec.org" in hosts and item["id"] == "5.3":
            scoped["rcepsec.org"] = [
                r"^/wp-content/uploads/\d{4}/\d{2}/(?:00-Title-page-Table-of-Contents-and-Preamble|"
                r"Chapter-(?:[1-9]|1\d|20)(?:-Annex-\d{1,2}[A-D])?|"
                r"Anx-I-00-General-Notes-Schedules-of-Tariff-Commitments|"
                r"Anx-I-05-Headnotes-to-Schedule-of-Indonesia|Anx-I-05c-Schedule-of-Indonesia-for-China|"
                r"Anx-I-05g-Appendix-to-Schedule-of-Indonesia|"
                r"Anx-III-05[ab]-Indonesias-Schedule-of-Reservations-and-NCM-for-(?:Services|Investment)|"
                r"Anx-IV-05-Indonesias-Schedule-on-TMNP)\.pdf$"
            ]
        if "fta.mofcom.gov.cn" in hosts and item["id"] == "5.2":
            scoped["fta.mofcom.gov.cn"] = [
                r"^/upload/agreementFiles/china_dongmeng_upgrade/annex/(?:"
                r"(?:xieyizw|xieyifj0[1-4]|xieyixiuding(?:0[1-3])?_yidingshu(?:-fj0[1-4])?|"
                r"zhengduanjzxy|hwmyxieyi(?:-fj03(?:-flC)?|_yidingshu-02|02-FR[1-3])?|"
                r"fwmyxieyi(?:_2crb-yidingshu)?|touzixieyi|yds(?:_fj1|_fl[AB])?|yds3(?:_fl(?:\d{1,2})?)?)_(?:cn|en)|"
                r"hwmyxieyi-fj01_(?:China-Indonesiajsb_cn|Indonesia2009-2012_shuilv_en)|"
                r"fwmyxieyi_fj-(?:2rb-)?Indonesiacrb_(?:cn|en))(?:\.pdf)?$"
            ]
    result["exclude_patterns"] = [
        r"(?:^|/)(?:login|logout|sign-in|careers|contact-us|sitemap|news|news-events|press-releases|events)(?:/|\?|$)",
        r"(?:^|/)(?:privacy-policy|terms-of-use)(?:/|\?|$)",
    ]
    from app.contexts.ingestion.infrastructure.source_adapters.official_source_recovery import (
        recover_official_recipe,
    )

    return recover_official_recipe(result, country=_country(item), item_id=item["id"])
