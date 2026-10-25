"""Bounded discovery for the Tax Directorate's official Kemenkeu JDIH catalog."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

HOST = "jdih.kemenkeu.go.id"
BASE = f"https://{HOST}"
CATALOG_PATH = "/search"
_CATALOG_FILTERS = {
    "order": "desc",
    "teu": "Direktorat Jenderal Pajak",
}
_CATALOG_KEYS = set(_CATALOG_FILTERS) | {"page"}
_MAX_PAGE = 2_000
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){0,32}\Z")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z", re.I)
_PAGE_LINK = re.compile(r"(?:^|&)page=([1-9][0-9]{0,3})(?:&|$)")
_RESULT_COUNT = re.compile(r"\bDitemukan\s+([0-9]{1,9})\s+hasil\b", re.I)
_ABSTRACT = re.compile(r"\b(?:abstrak|abstract|ringkasan|summary|pratinjau|preview)\b", re.I)
_ZERO_BYTES = re.compile(r"(?<![\w.,])0+(?:[.,]0+)?\s*(?:bytes?|[kmgtpe]?i?b)\b", re.I)
_ZERO_FULLTEXT_LABEL = re.compile(r"^\s*full\s*text\s*[:\-]?\s*0(?:\s|$)", re.I)
_FORMAL_TAX_INSTRUMENT = re.compile(r"^(?:per(?:aturan)?|kep(?:utusan)?|se(?:[-/\s])|surat\s+edaran)\b", re.I)


def catalog_url(page: int = 1) -> str:
    if type(page) is not int or not 1 <= page <= _MAX_PAGE:
        raise ValueError("Kemenkeu JDIH tax pagination is outside the bounded range.")
    return f"{BASE}{CATALOG_PATH}?" + urlencode({**_CATALOG_FILTERS, "page": str(page)})


def query_allowed(url: str) -> bool:
    """Accept only the reviewed DJP instrument filter and a bounded page number."""
    parsed = urlsplit(url)
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
    except ValueError:
        return False
    values = dict(pairs)
    if (
        len(values) != len(pairs)
        or set(values) not in (_CATALOG_KEYS, set(_CATALOG_FILTERS))
        or any(values.get(key) != value for key, value in _CATALOG_FILTERS.items())
    ):
        return False
    page = values.get("page", "1")
    return bool(re.fullmatch(r"[1-9][0-9]{0,3}", page) and 1 <= int(page) <= _MAX_PAGE)


def purpose(url: str) -> str | None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != HOST
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
    ):
        return None
    if parsed.path == CATALOG_PATH and query_allowed(url):
        return "catalog"
    if not parsed.query:
        match = re.fullmatch(r"/dok/([a-z0-9-]+)(?:/(overview|files))?", parsed.path, re.I)
        if match and _SLUG.fullmatch(match[1].casefold()):
            return "detail"
        file_match = re.fullmatch(r"/api/download/([^/]+)/([^/]+)", parsed.path)
        if file_match and _UUID.fullmatch(file_match[1]):
            filename = unquote(file_match[2])
            if (
                5 <= len(filename) <= 255
                and filename.casefold().endswith(".pdf")
                and not re.search(r"[/\\\x00-\x1f\x7f]", filename)
            ):
                return "document"
    return None


def _metadata(content: bytes) -> tuple[dict[str, str], str]:
    from selectolax.lexbor import LexborHTMLParser

    from app.shared.infrastructure.http.download import _decode

    source = _decode(content)
    tree = LexborHTMLParser(source)
    visible: dict[str, str] = {}
    for row in tree.css(".row.dok-item"):
        labels, values = row.css(".col-4"), row.css(".col-8")
        if len(labels) != 1 or len(values) != 1:
            continue
        label = " ".join(labels[0].text(separator=" ", strip=True).split())[:120]
        value = " ".join(values[0].text(separator=" ", strip=True).split())[:1200]
        if label and value:
            visible[label] = value
    if not visible:
        heading = tree.css_first(".breadcrumb-inner .page-title h1")
        document_code = " ".join(heading.text(separator=" ", strip=True).split())[:200] if heading else ""
        page_title = tree.css_first("title")
        title = " ".join(page_title.text(separator=" ", strip=True).split()) if page_title else ""
        title = re.sub(r"\s*\|\s*JDIH Kementerian Keuangan\s*$", "", title, flags=re.I)
        if document_code:
            title = re.sub(r"^" + re.escape(document_code) + r"\s*[-–:]\s*", "", title, flags=re.I)
            visible["Kode"] = document_code
        if title:
            visible["Judul"] = title[:1200]
        meta = tree.css(".breadcrumb-inner .dok-meta > li")
        meta_values = [" ".join(node.text(separator=" ", strip=True).split())[:300] for node in meta]
        if meta_values and meta_values[0]:
            visible["T.E.U. (Tajuk Entri Utama)"] = meta_values[0]
        if len(meta_values) > 1 and meta_values[1]:
            visible["source_display_date"] = meta_values[1]
        if len(meta_values) > 2 and meta_values[2]:
            visible["Status"] = meta_values[2]
    return visible, source


def _record_fields(metadata: dict[str, str]) -> dict[str, str]:
    names = {
        "document_type": "Tipe Dokumen",
        "document_code": "Kode",
        "title": "Judul",
        "number": "Nomor",
        "year": "Tahun",
        "instrument_type": "Jenis Peraturan",
        "type_abbreviation": "Singkatan Jenis",
        "issuer": "T.E.U. (Tajuk Entri Utama)",
        "decision_date": "Tanggal Penetapan",
        "promulgation_date": "Tanggal Pengundangan",
        "effective_period": "Tanggal Berlaku",
        "place_of_issue": "Tempat Terbit",
        "gazette_source": "Sumber",
        "language": "Bahasa",
        "status": "Status",
        "source_display_date": "source_display_date",
    }
    return {target: metadata[source] for target, source in names.items() if source in metadata}


def _file_role(label: str, filename: str) -> str | None:
    combined = f"{label} {filename}"
    if _ABSTRACT.search(combined):
        return None
    if filename.casefold().startswith("lampiran") or re.search(r"\blampiran\b", label, re.I):
        return "official_attachment"
    if re.search(r"\bfull\s*text\b", label, re.I):
        return "official_full_text"
    return None


def _candidate_record(metadata: dict[str, str], detail_url: str, file_url: str, label: str, role: str):
    from urllib.parse import urlsplit

    fields = _record_fields(metadata)
    filename = unquote(urlsplit(file_url).path.rsplit("/", 1)[-1])[:255]
    fields.update(
        official_detail_url=detail_url,
        official_file_url=file_url,
        official_file_label=label[:500],
        file_role=role,
        file_name=filename,
    )
    title = fields.get("title")
    code = fields.get("document_code")
    return {
        "provider": "jdih_kemenkeu",
        "interpretation": "unreviewed_source_values",
        "fields": fields,
        "structure": {
            "unit_type": role,
            "number": code or fields.get("number"),
            "parent_titles": [title] if title else [],
            "file_name": filename,
            "anchor_class": "",
            "link_mechanism": "verified_public_api_download",
        },
    }


def _file_contexts(content: bytes, base_url: str) -> dict[str, str]:
    """Read each official file's visible row, including its separately shown size."""
    from selectolax.lexbor import LexborHTMLParser

    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _canonical
    from app.shared.infrastructure.http.download import _decode

    tree = LexborHTMLParser(_decode(content))
    contexts = {}
    for anchor in tree.css(".dok-file-list a[href]")[:200]:
        ancestor = anchor
        row = None
        for _ in range(6):
            if ancestor is None:
                break
            if "justify-content-between" in ancestor.attributes.get("class", "").split():
                row = ancestor
                break
            ancestor = ancestor.parent
        if row is None:
            continue
        ancestor = row
        hidden = False
        for _ in range(32):
            if ancestor is None:
                break
            if ancestor.tag in {"script", "style", "template", "noscript"}:
                hidden = True
                break
            ancestor = ancestor.parent
        if hidden:
            continue
        try:
            target, _ = _canonical(anchor.attributes.get("href", ""), base_url)
        except (ValueError, TypeError):
            continue
        contexts.setdefault(target, " ".join(row.text(separator=" ", strip=True).split())[:1000])
    return contexts


def _related_records(content: bytes, base_url: str) -> list[dict[str, str]]:
    """Keep only same-site document links visibly listed as related by JDIH."""
    from selectolax.lexbor import LexborHTMLParser

    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _canonical
    from app.shared.infrastructure.http.download import _decode

    tree = LexborHTMLParser(_decode(content))
    related = []
    seen = set()
    for section in tree.css(".accordion-item"):
        heading = section.css_first(".accordion-header")
        if not heading or " ".join(heading.text(separator=" ", strip=True).split()) != "Produk Hukum Terkait":
            continue
        collapse = section.css_first(".accordion-collapse.show")
        body = collapse.css_first(".accordion-body") if collapse else None
        if body is None:
            continue
        for anchor in body.css("a[href]"):
            try:
                target, host = _canonical(anchor.attributes.get("href", ""), base_url)
            except (ValueError, TypeError):
                continue
            if (
                host != HOST
                or not re.fullmatch(r"/dok/[a-z0-9-]+", urlsplit(target).path, re.I)
                or urlsplit(target).query
                or target in seen
            ):
                continue
            title_node = anchor.css_first(".title")
            title = " ".join((title_node or anchor).text(separator=" ", strip=True).split())[:500]
            if not title:
                continue
            seen.add(target)
            related.append({"label": "Produk Hukum Terkait", "title": title, "url": target})
            if len(related) >= 100:
                return related
    return related


def inspect(url: str, content: bytes, mime_type: str, rules) -> dict:
    """Return only filtered DJP catalog rows, matching detail pages and labelled PDFs."""
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
        _Page,
        _canonical,
        _result,
    )
    from app.shared.infrastructure.http.download import _decode

    kind = purpose(url)
    if not rules.kemenkeu_tax or kind is None:
        return _result(reason="页面不在 Kemenkeu JDIH 的税务目录、详情或原件范围内。")
    if kind == "catalog" and not query_allowed(url):
        return _result(reason="Kemenkeu JDIH 目录筛选超出已审核的 DJP 税务范围。")
    if kind == "document":
        if mime_type.split(";", 1)[0].strip().casefold() == "application/pdf" and content.startswith(
            b"%PDF-"
        ):
            return _result(matched=True, purpose="document")
        return _result(reason="Kemenkeu JDIH 原件响应不是已验证的 PDF。")
    if mime_type.split(";", 1)[0].strip().casefold() not in {"text/html", "application/xhtml+xml"}:
        return _result(reason="Kemenkeu JDIH 目录或详情页不是 HTML。")
    if len(content) > 4 * 1024 * 1024:
        return _result(reason="Kemenkeu JDIH HTML 页面超过 4 MiB 发现上限。")

    page = _Page()
    try:
        page.feed(_decode(content))
        page.close()
    except (ValueError, AssertionError, UnicodeError):
        return _result(reason="Kemenkeu JDIH 页面结构无法读取。")
    title = " ".join(page.title)
    visible = " ".join(page.text)
    if (
        page.challenge
        or not visible
        or re.search(r"\b(?:access denied|forbidden|page not found|verification required)\b", title, re.I)
    ):
        return _result(reason="Kemenkeu JDIH 返回验证、拒绝或错误页面。")

    current, _ = _canonical(url)
    parsed = urlsplit(current)
    candidates, warnings, seen = [], [], {current}
    metadata: dict[str, str] = {}
    current_page = int(dict(parse_qsl(parsed.query)).get("page", "1")) if kind == "catalog" else 1
    count_match = _RESULT_COUNT.search(visible) if kind == "catalog" else None
    related_records = _related_records(content, current) if kind == "detail" else []
    if kind == "detail":
        try:
            metadata, _ = _metadata(content)
        except (ValueError, UnicodeError):
            return _result(reason="Kemenkeu JDIH 详情元数据结构无法读取。")
        source_fields = _record_fields(metadata)
        if related_records:
            source_fields["related_record_count"] = len(related_records)
        record_context = {
            "provider": "jdih_kemenkeu",
            "interpretation": "unreviewed_source_values",
            "fields": {**source_fields, "official_detail_url": current},
            "structure": {
                "unit_type": "official_regulation_record",
                "number": source_fields.get("document_code") or source_fields.get("number"),
                "parent_titles": [source_fields["title"]] if source_fields.get("title") else [],
                "file_name": "",
                "anchor_class": "",
                "link_mechanism": "official_jdih_detail_page",
                "related_links": related_records,
            },
        }
        slug = re.fullmatch(r"/dok/([a-z0-9-]+)(?:/(?:overview|files))?", parsed.path, re.I)
        document_id = slug[1].casefold() if slug else ""
    else:
        source_fields = {}
        record_context = {
            "provider": "jdih_kemenkeu",
            "interpretation": "unreviewed_source_values",
            "fields": {
                "catalog_url": current,
                "catalog_order": _CATALOG_FILTERS["order"],
                "catalog_issuer_filter": _CATALOG_FILTERS["teu"],
                "search_page": current_page,
                "reported_result_count": int(count_match[1]) if count_match else None,
            },
            "structure": {
                "unit_type": "official_tax_catalog",
                "number": None,
                "parent_titles": [],
                "file_name": "",
                "anchor_class": "",
                "link_mechanism": "server_rendered_search",
            },
        }
        document_id = ""

    file_contexts = _file_contexts(content, current) if kind == "detail" else {}

    for link in page.links:
        if not link.url or link.tag != "a":
            continue
        try:
            candidate, _ = _canonical(link.url, current)
        except ValueError:
            continue
        if candidate in seen:
            continue
        candidate_kind = purpose(candidate)
        label = " ".join([*link.text, link.title]).strip()
        label = " ".join(label.split())[:500]
        link_source_record = None
        if kind == "catalog":
            if candidate_kind == "detail":
                path = urlsplit(candidate).path
                if not re.fullmatch(r"/dok/[a-z0-9-]+", path, re.I):
                    continue
                if label and not _FORMAL_TAX_INSTRUMENT.match(label):
                    continue
                purpose_value = "detail"
                link_source_record = {
                    **record_context,
                    "fields": {**record_context["fields"], "catalog_title": label[:500]},
                }
            elif candidate_kind == "catalog":
                target_page = int(dict(parse_qsl(urlsplit(candidate).query)).get("page", "1"))
                if target_page == current_page:
                    continue
                purpose_value = "catalog"
                link_source_record = record_context
            else:
                continue
        else:
            if candidate_kind == "detail":
                next_path = urlsplit(candidate).path
                if (
                    not next_path.endswith("/files")
                    or not document_id
                    or not next_path.startswith(f"/dok/{document_id}/")
                ):
                    continue
                purpose_value = "detail"
                link_source_record = record_context
            elif candidate_kind == "document":
                filename = unquote(urlsplit(candidate).path.rsplit("/", 1)[-1])
                file_context = file_contexts.get(candidate, "")
                file_label = f"{label} {file_context}".strip()
                if _ZERO_BYTES.search(file_context) or _ZERO_FULLTEXT_LABEL.match(label):
                    seen.add(candidate)
                    if re.search(r"\bfull\s*text\b", file_label, re.I):
                        warnings.append(
                            "官方目录将 Fulltext 标为 0（含 0 Bytes 或空文件标签）；未把空文件链接当作取得正文。"
                        )
                    continue
                if _ABSTRACT.search(f"{file_label} {filename}"):
                    seen.add(candidate)
                    warnings.append("已排除标记为摘要或预览的文件，不作为法规正文。")
                    continue
                role = _file_role(file_label, filename)
                if role is None:
                    continue
                purpose_value = "document"
                link_source_record = _candidate_record(metadata, current, candidate, file_label, role)
                if related_records:
                    link_source_record["fields"]["related_record_count"] = len(related_records)
                    link_source_record["structure"]["related_links"] = related_records
            else:
                continue
        seen.add(candidate)
        if len(candidates) >= rules.max_links:
            warnings.append(f"Kemenkeu JDIH 单页候选达到 {rules.max_links} 条上限；仍有链接未登记。")
            break
        row = {"url": candidate, "purpose": purpose_value, "title": label or candidate.rsplit("/", 1)[-1]}
        if link.source_locator:
            row["source_locator"] = link.source_locator
        if link_source_record:
            row["source_record"] = link_source_record
        candidates.append(row)

    if kind == "catalog":
        page_numbers = []
        for row in candidates:
            if row["purpose"] == "catalog":
                match = _PAGE_LINK.search(urlsplit(row["url"]).query)
                if match:
                    page_numbers.append(int(match[1]))
        source_record = {
            **record_context,
            "fields": {
                **record_context["fields"],
                "search_page": current_page,
                "reported_result_count": int(count_match[1]) if count_match else None,
                "highest_linked_page": max(page_numbers, default=None),
            },
        }
        if not count_match:
            warnings.append("未能从官方目录核验结果总数；不能据此认定分页完整。")
        if not page_numbers and count_match and int(count_match[1]) > 10:
            warnings.append("官方目录报告多页结果但未发现分页链接；请检查动态分页。")
    else:
        source_record = record_context
        has_original = any(
            row["purpose"] == "document"
            and row.get("source_record", {}).get("structure", {}).get("unit_type") == "official_full_text"
            for row in candidates
        )
        has_attachment = any(
            row["purpose"] == "document"
            and row.get("source_record", {}).get("structure", {}).get("unit_type") == "official_attachment"
            for row in candidates
        )
        if not has_original:
            warnings.append("该详情未提供可验证的非空 Fulltext PDF；附件不会冒充法规正文。")
        if not has_attachment and "Lampiran" in visible:
            warnings.append("详情显示附件区域，但没有发现符合校验的附件 PDF 链接。")
    if page.truncated:
        warnings.append("Kemenkeu JDIH 页面链接达到扫描上限；仍有内容未登记，不能认定目录完整。")
    result = _result(matched=True, purpose=kind, links=candidates, warnings=warnings)
    result["source_record"] = source_record
    return result
