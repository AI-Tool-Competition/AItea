"""Bounded, read-only discovery from Bank Indonesia's public regulations portal."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

HOST = "www.bi.go.id"
CATALOG_PATH = "/id/publikasi/peraturan/default.aspx"
DETAIL_PREFIX = "/id/publikasi/peraturan/Pages/"
DOCUMENT_PREFIX = "/id/publikasi/peraturan/Documents/"
CATEGORY = "peraturan bank indonesia"
PAGE_SIZE = 10
MAX_PAGES = 2_000
MAX_RECORDS = PAGE_SIZE * MAX_PAGES
MAX_HTML_BYTES = 6 * 1024 * 1024
_PAGE_ID = re.compile(r"[1-9][0-9]{0,3}\Z")
_DETAIL_FILE = re.compile(r"[A-Za-z0-9_-]{3,80}\.aspx\Z", re.I)
_PDF_NAME = re.compile(r"[A-Za-z0-9_.() -]{5,240}\.pdf\Z", re.I)
_SUMMARY = re.compile(
    r"\bMenampilkan\s+([0-9]{1,9})\s*[–-]\s*([0-9]{1,9})\s+data\s+dari\s+total\s+([0-9]{1,9})\b",
    re.I,
)
_POSTBACK = re.compile(
    r"\s*javascript:\s*__doPostBack\(\s*(['\"])([^'\"]{1,300})\1\s*,\s*(['\"])([^'\"]{0,100})\3\s*\)\s*;?\s*",
    re.I,
)
_ERROR = re.compile(
    r"access denied|403\s+forbidden|page not found|service unavailable|verify (?:that )?you are human|"
    r"checking your browser|halaman tidak ditemukan|akses ditolak",
    re.I,
)
_METADATA_LABELS = {
    "berlaku",
    "tanggal penetapan",
    "tanggal pengundangan",
    "status",
    "jenis peraturan",
    "sektor",
    "kategori",
    "bahasa",
    "unit kerja",
}


def _safe_url(url: str):
    from app.shared.infrastructure.http.download import _validated_url

    try:
        current, host = _validated_url(url)
        parsed = urlsplit(current)
    except (TypeError, ValueError):
        return None
    if (
        host != HOST
        or parsed.scheme != "https"
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return None
    return current, parsed


def _query(url: str):
    try:
        pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
    except ValueError:
        return None
    values = dict(pairs)
    if len(values) != len(pairs):
        return None
    return values


def _page_number(url: str) -> int | None:
    values = _query(url)
    if values is None or values.get("Kategori") != CATEGORY:
        return None
    if set(values) == {"Kategori"}:
        return 1
    if set(values) not in (
        {"Kategori", "aitea_page"},
        {"Kategori", "aitea_page", "aitea_total"},
    ):
        return None
    raw = values["aitea_page"]
    if not _PAGE_ID.fullmatch(raw):
        return None
    page = int(raw)
    if not 2 <= page <= MAX_PAGES:
        return None
    expected = values.get("aitea_total")
    if expected is not None and (
        not re.fullmatch(r"[1-9][0-9]{0,4}", expected)
        or int(expected) > MAX_RECORDS
        or page > (int(expected) + PAGE_SIZE - 1) // PAGE_SIZE
    ):
        return None
    return page


def page_number(url: str) -> int | None:
    """Return the bounded internal catalog page marker, with the seed as page 1."""
    return _page_number(url)


def catalog_url(page: int, *, expected_total: int | None = None) -> str:
    if type(page) is not int or not 1 <= page <= MAX_PAGES:
        raise ValueError("央行官网分页超出配置上限。")
    if expected_total is not None and (
        type(expected_total) is not int
        or not 1 <= expected_total <= MAX_RECORDS
        or page == 1
        or page > (expected_total + PAGE_SIZE - 1) // PAGE_SIZE
    ):
        raise ValueError("央行官网目录总量标记无效。")
    query = {"Kategori": CATEGORY}
    if page > 1:
        query["aitea_page"] = str(page)
    if expected_total is not None:
        query["aitea_total"] = str(expected_total)
    return f"https://{HOST}{CATALOG_PATH}?" + urlencode(query)


def previous_catalog_url(url: str) -> str:
    page = _page_number(url)
    values = _query(url)
    if page is None or values is None:
        raise ValueError("央行官网目录队列地址无效。")
    expected_total = int(values["aitea_total"]) if "aitea_total" in values else None
    return catalog_url(page - 1, expected_total=expected_total if page > 2 else None)


def network_catalog_url(url: str) -> str:
    if purpose(url) != "catalog":
        raise ValueError("央行官网请求地址不是已验证的法规目录。")
    # Queue-only page and count markers must not be sent to SharePoint.
    return catalog_url(1)


def purpose(url: str) -> str | None:
    safe = _safe_url(url)
    if safe is None:
        return None
    _, parsed = safe
    if parsed.path == CATALOG_PATH:
        return "catalog" if _page_number(url) else None
    if not parsed.query and re.fullmatch(re.escape(DETAIL_PREFIX) + _DETAIL_FILE.pattern, parsed.path):
        return "detail"
    if not parsed.query and parsed.path.startswith(DOCUMENT_PREFIX):
        name = parsed.path.removeprefix(DOCUMENT_PREFIX)
        # Reject encoded separators, repeated escaping, dot paths and non-PDF files.
        from urllib.parse import unquote

        decoded = name
        for _ in range(3):
            decoded = unquote(decoded)
            if re.search(r"[/\\:\x00-\x1f\x7f]", decoded) or decoded in {".", ".."}:
                return None
        return "document" if _PDF_NAME.fullmatch(decoded) else None
    return None


def _clean(value: str, maximum=2_000) -> str:
    return " ".join(value.replace("\u200b", "").replace("\ufeff", "").split())[:maximum]


def _tree(content: bytes):
    from selectolax.lexbor import LexborHTMLParser

    from app.shared.infrastructure.http.download import _decode

    return LexborHTMLParser(_decode(content))


def _visible_page(content: bytes):
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _Page

    page = _Page()
    page.feed(content.decode("utf-8", "replace"))
    page.close()
    return page


def _catalog_state(url: str, page):
    number = _page_number(url)
    matches = list(_SUMMARY.finditer(" ".join(page.text)))
    if number is None or len(matches) != 1:
        return None
    start, end, total = (int(value) for value in matches[0].groups())
    values = _query(url) or {}
    expected_total = values.get("aitea_total")
    expected_start = (number - 1) * PAGE_SIZE + 1
    expected_end = min(expected_start + PAGE_SIZE - 1, total)
    if (
        total < 1
        or total > MAX_RECORDS
        or start != expected_start
        or end != expected_end
        or end < start
        or number > MAX_PAGES
        or (expected_total is not None and int(expected_total) != total)
    ):
        return None
    return {"page": number, "start": start, "end": end, "total": total, "page_size": PAGE_SIZE}


def _next_page_controls(pager, target_page: int):
    anchor_target = None
    for anchor in pager.css("a"):
        label = _clean(anchor.text(separator=" ", strip=True), 20)
        match = _POSTBACK.fullmatch(anchor.attributes.get("href", ""))
        if label == str(target_page) and match:
            candidate, argument = match.group(2), match.group(4)
            if (
                "$DataPagerPeraturan$" in candidate
                and re.fullmatch(r"[A-Za-z0-9_$.-]{1,300}", candidate)
                and argument == ""
            ):
                anchor_target = candidate
                break
    if anchor_target:
        return anchor_target, None
    for image in pager.css('input[type="image"]'):
        name = image.attributes.get("name", "")
        classes = set(image.attributes.get("class", "").casefold().split())
        alt = image.attributes.get("alt", "").casefold().strip()
        if (
            "next" in classes
            and alt == "next"
            and "$DataPagerPeraturan$" in name
            and re.fullmatch(r"[A-Za-z0-9_$.-]{1,300}", name)
        ):
            return None, name
    return None, None


def _inside(node, ancestor) -> bool:
    current = node
    while current is not None:
        if current.mem_id == ancestor.mem_id:
            return True
        current = current.parent
    return False


def catalog_form_request(parent_url: str, parent_content: bytes, target_page: int):
    """Recreate one same-origin WebForms pager postback without storing token values."""
    from app.shared.infrastructure.http.download import _decode

    if (
        purpose(parent_url) != "catalog"
        or type(target_page) is not int
        or target_page != (_page_number(parent_url) or 0) + 1
        or len(parent_content) > MAX_HTML_BYTES
    ):
        raise ValueError("央行官网分页请求超出已验证的相邻页面范围。")
    page = _visible_page(parent_content)
    state = _catalog_state(parent_url, page)
    if state is None or target_page > (state["total"] + PAGE_SIZE - 1) // PAGE_SIZE:
        raise ValueError("央行官网父目录页码或总量无法核实。")

    tree = _tree(parent_content)
    forms = tree.css("form")
    pagers = tree.css('[id*="DataPagerPeraturan"]')
    if len(forms) != 1 or len(pagers) != 1 or not _inside(pagers[0], forms[0]):
        raise ValueError("央行官网目录分页表单或页码栏不唯一。")
    action = forms[0].attributes.get("action", "").strip()
    target = urljoin(parent_url, action) if action else parent_url
    if purpose(target) != "catalog" or urlsplit(target).path != CATALOG_PATH:
        raise ValueError("央行官网目录分页表单提交地址超出同一法规目录。")

    event_target, image_submit = _next_page_controls(pagers[0], target_page)
    if event_target is None and image_submit is None:
        raise ValueError("央行官网没有提供下一页的匹配 postback 按钮。")

    fields: list[tuple[str, str]] = []
    found_viewstate = found_validation = False
    for node in forms[0].css("input[name], textarea[name], select[name]"):
        name = node.attributes.get("name", "")
        tag = node.tag
        if tag == "input":
            kind = node.attributes.get("type", "text").casefold()
            if kind in {"button", "submit", "reset", "image", "file", "password"}:
                continue
            if kind in {"checkbox", "radio"} and "checked" not in node.attributes:
                continue
            value = node.attributes.get("value", "on" if kind in {"checkbox", "radio"} else "")
            found_viewstate |= name == "__VIEWSTATE" and bool(value)
            found_validation |= name == "__EVENTVALIDATION" and bool(value)
            if name not in {"__EVENTTARGET", "__EVENTARGUMENT"}:
                fields.append((name, value))
        elif tag == "textarea":
            fields.append((name, node.text(deep=True)))
        else:
            options = node.css("option")
            selected = [option for option in options if "selected" in option.attributes]
            if not selected and "multiple" not in node.attributes and options:
                selected = options[:1]
            fields.extend((name, option.attributes.get("value", option.text(strip=True))) for option in selected)
        if len(fields) > 510:
            raise ValueError("央行官网目录分页字段超过数量上限。")
    if not found_viewstate or not found_validation:
        raise ValueError("央行官网分页状态字段不完整。")
    if event_target is not None:
        fields.extend((("__EVENTTARGET", event_target), ("__EVENTARGUMENT", "")))
    else:
        fields.extend(((image_submit + ".x", "0"), (image_submit + ".y", "0")))
    evidence = {
        "parent_url": parent_url,
        "parent_page_sha256": hashlib.sha256(parent_content).hexdigest(),
        "parent_page": state["page"],
        "target_page": target_page,
        "event_target": event_target,
        "image_submit": image_submit,
    }
    # Assert the parser and evidence are tied to the same decoded HTML.
    if len(_decode(parent_content)) > MAX_HTML_BYTES:
        raise ValueError("央行官网分页状态超过 HTML 大小上限。")
    return fields, evidence


def _catalog_result(url: str, content: bytes, mime: str, rules):
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
        MAX_PAGE_LINKS,
        _ERROR_BODY,
        _ERROR_HEADING,
        _canonical,
        _result,
    )

    if mime not in {"text/html", "application/xhtml+xml"} or len(content) > MAX_HTML_BYTES:
        return _result(reason="央行官网法规目录必须是 6 MiB 内的 HTML 页面。")
    page = _visible_page(content)
    title = " ".join(page.title)
    headings = " ".join(page.headings[:3])
    visible = " ".join(page.text)
    state = _catalog_state(url, page)
    if (
        page.challenge
        or _ERROR_HEADING.search(title)
        or _ERROR_HEADING.search(headings)
        or (len(visible) < 1_000 and _ERROR_BODY.search(visible))
    ):
        return _result(reason="央行官网返回验证、拒绝或错误页面，未登记候选。")
    if state is None:
        return _result(reason="央行官网目录页码、显示范围或总量无法相互核验。")

    found: dict[str, str] = {}
    for link in page.links:
        if link.tag != "a" or not link.url or link.url.startswith("#"):
            continue
        try:
            candidate, host = _canonical(link.url, url)
        except ValueError:
            continue
        parsed = urlsplit(candidate)
        if (
            host != HOST
            or parsed.query
            or not parsed.path.startswith(DETAIL_PREFIX)
            or not _DETAIL_FILE.fullmatch(parsed.path.removeprefix(DETAIL_PREFIX))
        ):
            continue
        label = _clean(" ".join(link.text) or link.title or parsed.path.rsplit("/", 1)[-1], 500)
        if candidate not in found or len(label) > len(found[candidate]):
            found[candidate] = label
    expected_rows = state["end"] - state["start"] + 1
    if len(found) != expected_rows:
        return _result(reason="央行官网目录当前页法规详情数量与分页显示范围不一致。")
    links = [
        {"url": target, "purpose": "detail", "title": label}
        for target, label in sorted(found.items())
    ]
    total_pages = (state["total"] + PAGE_SIZE - 1) // PAGE_SIZE
    if state["page"] < total_pages:
        pager = None
        try:
            pager = _tree(content).css_first('[id*="DataPagerPeraturan"]')
        except (ValueError, AssertionError, UnicodeError):
            pager = None
        next_page = state["page"] + 1
        if pager is None or _next_page_controls(pager, next_page) == (None, None):
            return _result(reason="央行官网总量仍有后续记录，但当前页未给出可验证的下一页按钮。")
        continuation = catalog_url(next_page, expected_total=state["total"])
        links.append({"url": continuation, "purpose": "catalog", "title": f"目录第 {next_page} 页"})
    if len(links) > rules.max_links or len(links) > MAX_PAGE_LINKS:
        return _result(reason="央行官网目录候选超过单页限制。")
    result = _result(matched=True, purpose="catalog", links=links)
    result["scope"] = {
        "provider": "bi-official-regulations-v1",
        "catalog_sha256": hashlib.sha256(content).hexdigest(),
        **state,
        "returned": len(found),
        "total_pages": total_pages,
        "has_next": state["page"] < total_pages,
    }
    return result


def _detail_result(url: str, content: bytes, mime: str, rules):
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
        _ERROR_BODY,
        _ERROR_HEADING,
        _canonical,
        _result,
    )

    if mime not in {"text/html", "application/xhtml+xml"} or len(content) > MAX_HTML_BYTES:
        return _result(reason="央行官网法规详情必须是 6 MiB 内的 HTML 页面。")
    page = _visible_page(content)
    title = _clean(" ".join(page.title), 1_000)
    visible = " ".join(page.text)
    if (
        page.challenge
        or not title
        or _ERROR_HEADING.search(title)
        or (len(visible) < 500 and _ERROR_BODY.search(visible))
    ):
        return _result(reason="央行官网详情返回验证、拒绝或错误页面，未登记候选。")
    tree = _tree(content)
    metadata = {}
    for row in tree.css("table tr"):
        row_text = _clean(row.text(separator=" ", strip=True), 2_000)
        match = re.match(r"^([^:：]{1,120})\s*[:：]\s*(.{1,1800})$", row_text)
        if not match:
            continue
        label = _clean(match.group(1), 120)
        value = _clean(match.group(2), 1_800)
        if label.casefold() == "peraturan" or label.casefold() in _METADATA_LABELS:
            metadata.setdefault(label, value)
        if len(metadata) >= 20:
            break

    visible_targets = {}
    for link in page.links:
        if link.tag != "a" or not link.url:
            continue
        try:
            target, host = _canonical(link.url, url)
        except (TypeError, ValueError):
            continue
        if host != HOST or purpose(target) != "document":
            continue
        label = _clean(" ".join(link.text) or link.title or target.rsplit("/", 1)[-1], 500)
        if target not in visible_targets or len(label) > len(visible_targets[target]):
            visible_targets[target] = label

    attachments = {}
    for target, label in visible_targets.items():
        combined = f"{label} {target.rsplit('/', 1)[-1]}".casefold()
        role = (
            "faq"
            if re.search(r"\bfaq\b|tanya\s*jawab|pertanyaan", combined)
            else "official_attachment"
            if re.search(r"\b(?:lampiran|appendix|annex|attachment)\b", combined)
            else "english_version"
            if re.search(r"\benglish\b|\bbahasa inggris\b", combined)
            else "document_candidate"
        )
        attachments[target] = {"url": target, "label": label, "role": role}

    detail_id = urlsplit(url).path.removeprefix(DETAIL_PREFIX)
    links = []
    for attachment in attachments.values():
        filename = attachment["url"].rsplit("/", 1)[-1]
        source_record = {
            "provider": "bi_official",
            "interpretation": "unreviewed_source_values",
            "fields": {
                "detail_page_url": url,
                "detail_page_title": title,
                "page_metadata": metadata,
                "attachment_url": attachment["url"],
                "attachment_label": attachment["label"],
                "attachment_role": attachment["role"],
                "filename": filename,
                "filename_identity_verified": False,
            },
            "structure": {
                "unit_type": "official_attachment_candidate",
                "number": None,
                "title": title,
                "instrument_type": "Bank Indonesia regulation; source values unreviewed",
                "parent_titles": [],
                "file_name": filename,
                "anchor_class": "",
                "link_mechanism": "official_detail_page_attachment_link",
                "parent_detail_id": detail_id,
            },
        }
        links.append(
            {
                "url": attachment["url"],
                "purpose": "document",
                "title": f"{attachment['role']}: {attachment['label']}"[:500],
                "source_record": source_record,
            }
        )
    result = _result(matched=True, purpose="detail", links=links)
    result["scope"] = {
        "provider": "bi-official-regulations-v1",
        "detail_page_sha256": hashlib.sha256(content).hexdigest(),
        "detail_id": detail_id,
        "attachment_candidates": len(links),
        "metadata_fields": sorted(metadata),
    }
    if not links:
        result["warnings"].append("官方详情页未发现可分类的 PDF 附件；正文缺口待核。")
    return result


def inspect(url: str, content: bytes, mime: str, rules):
    kind = purpose(url)
    if not rules or not kind:
        from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _result

        return _result(reason="页面不在央行官网适配器允许的路径内。")
    if kind == "catalog":
        return _catalog_result(url, content, mime, rules)
    if kind == "detail":
        return _detail_result(url, content, mime, rules)
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _result

    if mime.split(";", 1)[0].strip().casefold() == "application/pdf" and content.startswith(b"%PDF-"):
        return _result(matched=True, purpose="document")
    return _result(reason="央行官网附件响应不含有效 PDF 文件签名。")
