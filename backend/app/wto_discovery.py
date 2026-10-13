"""Public WTO source discovery with explicit member and original mappings.

Downloaded catalogs are evidence, never legal text or approval. No network,
authentication, archive extraction, guessed file conversions or global search.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit
from xml.etree import ElementTree

GOODS = "goods-schedules.wto.org"
DOCS = "docs.wto.org"
XML_PATH = "/dol2fe/Pages/SS/GetXMLResults.aspx"
MEMBERS = {
    "ID": {
        "name": "Indonesia",
        "slug": "indonesia",
        "schedule": "XXI",
        "number": 21,
        "node": 36658,
        "service": 43,
    },
    "SG": {
        "name": "Singapore",
        "slug": "singapore",
        "schedule": "LXXIII",
        "number": 73,
        "node": 36723,
        "service": 76,
    },
}
MAX_BYTES = 4 * 1024 * 1024
_AUTN = "{http://schemas.autonomy.com/aci/}"
# These exact official originals were located through the public legal-text
# directory and the three amendment sources below, not a generic WT/L search.
_GENERIC_FILES = {
    "Q:/WT/L/1014.PDF",
    "Q:/WT/LET/1497.PDF",
    "Q:/PLURI/IDA/8.PDF",
    "Q:/PLURI/IMA/8.PDF",
    "Q:/WT/L/641.PDF",
    "Q:/WT/L/940.PDF",
    "Q:/WT/MIN22/33.PDF",
}
PROTOCOL_SOURCES = {
    "WT/L/641": "https://www.wto.org/english/tratop_e/trips_e/wtl641_e.htm",
    "WT/L/940": "https://www.wto.org/english/res_e/publications_e/ai17_e/wto_agree_art10_oth.pdf",
    "WT/L/1144": "https://www.wto.org/english/tratop_e/rulesneg_e/fish_e/agreement_fisheries_subsidies_e.htm",
}
_PROTOCOL_FILES = {
    "WT/L/641": "Q:/WT/L/641.PDF",
    "WT/L/940": "Q:/WT/L/940.PDF",
    "WT/L/1144": "Q:/WT/MIN22/33.PDF",
}
# Two published historical MDB responses have no Jet header. Preserve only
# these exact, fully hashed source snapshots as quarantined originals; this
# does not generalize extension or MIME claims into a format detector.
_UNVERIFIED_MDB = {
    f"https://{GOODS}/system/files/WTO_import/DVD/Indonesia/3_Rectifications%20-%20Modifications/GMATARRS23-01_E.mdb": {
        "member": "ID",
        "size": 262144,
        "source_sha256": "31a611ce880e276b9c23cbc24d672e00760c25f5cb3409575a0a94ab37618ab0",
        "catalog_url": f"https://{GOODS}/browse/39503/file_browser",
        "catalog_sha256": "e0f40a5362b082cc05b8c42254faaa209c7652067484030de164ce30220c0768",
    },
    f"https://{GOODS}/system/files/WTO_import/DVD/Singapore/3_Rectifications%20-%20Modifications/GMATARRS20-01_E.MDB": {
        "member": "SG",
        "size": 294912,
        "source_sha256": "adbb377da2f3f858278e6c37e840f9803bd6bc7f1af69b9abeb8c6a8285e3e6e",
        "catalog_url": f"https://{GOODS}/browse/39500/file_browser",
        "catalog_sha256": "b474ec53f1da46163942c5ac761808e763940137aeeb04ae66eb5782156f26ff",
    },
}
_HIDDEN = {"script", "style", "template", "noscript"}
_VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


def services_catalog_url(member: str, family: str) -> str:
    if member not in MEMBERS or family not in {"SC", "EL"}:
        raise ValueError("WTO 服务目录成员或文件族无效。")
    return catalog_url(f"GATS/{family}/{MEMBERS[member]['service']}")


def catalog_url(symbol: str) -> str:
    return f"https://{DOCS}{XML_PATH}?" + urlencode(
        sorted(
            {
                "DataSource": "Cat",
                "Query": f"@Symbol={symbol}*",
                "XMLMaxHitsPerPage": "100",
                "Language": "English",
            }.items()
        )
    )


def _query(url: str) -> dict:
    pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True, max_num_fields=10)
    query = dict(pairs)
    if len(pairs) != len(query):
        raise ValueError("WTO 请求包含重复参数。")
    return query


def catalog_family(url: str, member: str) -> str | None:
    try:
        parsed, query = urlsplit(url), _query(url)
        if (
            parsed.hostname != DOCS
            or parsed.path != XML_PATH
            or set(query) != {"DataSource", "Query", "XMLMaxHitsPerPage", "Language"}
            or query["DataSource"] != "Cat"
            or query["XMLMaxHitsPerPage"] != "100"
            or query["Language"] != "English"
        ):
            return None
        allowed = {f"GATS/{family}/{MEMBERS[member]['service']}" for family in ("SC", "EL")} | set(
            PROTOCOL_SOURCES
        )
        return next((symbol for symbol in allowed if query["Query"] == f"@Symbol={symbol}*"), None)
    except (ValueError, KeyError):
        return None


def original_filename(url: str, member: str) -> str | None:
    try:
        parsed, query = urlsplit(url), _query(url)
        if (
            parsed.hostname != DOCS
            or parsed.path not in {"/dol2fe/Pages/SS/directdoc.aspx", "/dol2fe/Pages/SS/directdoc2.aspx"}
            or not set(query) <= {"filename", "Open"}
            or ("Open" in query and query["Open"] != "True")
        ):
            return None
        filename = query.get("filename", "").upper()
        number = MEMBERS[member]["service"]
        if filename in _GENERIC_FILES or re.fullmatch(
            rf"Q:/SCHD/(?:GATS-SC/SC{number}|GATS-EL/EL{number})(?:(?:S|R|C)[1-9]\d{{0,2}})*\.PDF", filename
        ):
            return filename
    except (ValueError, KeyError):
        pass
    return None


def filename_matches(filename, symbol, family, member):
    """Verify the published symbol/filename pair without inventing a URL."""
    if not all(isinstance(value, str) for value in (filename, symbol, family)) or member not in MEMBERS:
        return False
    if family in _PROTOCOL_FILES:
        return family in symbol.split("#") and filename.upper() == _PROTOCOL_FILES[family]
    number = MEMBERS[member]["service"]
    match = re.fullmatch(rf"GATS/(SC|EL)/{number}", family)
    if not match or not re.fullmatch(
        re.escape(family) + r"(?:/(?:Suppl\.[1-9]\d{0,2}|Rev\.[1-9]\d{0,2}|Corr\.[1-9]\d{0,2}))*", symbol
    ):
        return False
    prefix, stem = match[1], match[1] + str(number)
    for suffix in symbol.removeprefix(family).split("/"):
        if suffix:
            part = re.fullmatch(r"(Suppl|Rev|Corr)\.([1-9]\d{0,2})", suffix)
            stem += {"Suppl": "S", "Rev": "R", "Corr": "C"}[part[1]] + part[2]
    return filename.upper() == f"Q:/SCHD/GATS-{prefix}/{stem}.PDF"


def _goods_file(url: str, member: str) -> bool:
    parsed = urlsplit(url)
    path = unquote(parsed.path)
    if parsed.hostname != GOODS or parsed.query or not re.search(r"\.(?:pdf|docx?|xls|mdb|wpf)$", path, re.I):
        return False
    if re.fullmatch(r"/system/files/\d{4}-(?:0[1-9]|1[0-2])/[A-Za-z0-9_.-]+", path):
        return True
    return bool(
        re.fullmatch(
            r"/system/files/WTO_import/(?:Drive/(?:WT-Let|G_MA_TAR)_(?:English|French|Spanish)|"
            + rf"DVD/{MEMBERS[member]['name']}/[A-Za-z0-9_ .%()'-]+(?:/[A-Za-z0-9_ .%()'-]+)*)"
            + r"/[A-Za-z0-9_ .%()'-]+",
            path,
        )
    )


def purpose(url: str, member: str) -> str | None:
    """An allowed URL still needs its downloaded member/schedule verified."""
    from .discovery import _canonical

    try:
        current, host = _canonical(url)
        if member not in MEMBERS or current != url:
            return None
        parsed, query, info = urlsplit(current), _query(current), MEMBERS[member]
        if host == DOCS:
            if parsed.path == XML_PATH:
                return "catalog" if catalog_family(current, member) else None
            if original_filename(current, member):
                return "document" if parsed.path.endswith("directdoc2.aspx") else "detail"
            return None
        if host != GOODS:
            return None
        if parsed.path.rstrip("/") == f"/member/{info['slug']}":
            if (
                set(query) <= {"page", "status"}
                and query.get("status", "All") == "All"
                and re.fullmatch(r"0|[1-9]\d{0,3}", query.get("page", "0"))
                and int(query.get("page", "0")) < 2000
            ):
                return "catalog"
            return None
        if parsed.path == f"/member/download/{info['node']}" and not query:
            return "catalog"  # A raw bundle audit asset; never a law candidate.
        if re.fullmatch(r"/browse/[1-9]\d{0,8}/file_browser/?", parsed.path) and not query:
            return "detail"
        if _goods_file(current, member):
            return "document"
    except (ValueError, KeyError):
        pass
    return None


@dataclass
class _Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)

    def nodes(self):
        stack = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed([child for child in node.children if isinstance(child, _Node)]))

    def text(self):
        parts, stack = [], [self]
        while stack:
            item = stack.pop()
            if isinstance(item, str):
                parts.append(item)
            else:
                stack.extend(reversed(item.children))
        return " ".join(" ".join(parts).split())

    def has(self, name):
        value = self.attrs.get("class")
        return isinstance(value, str) and name in value.split()


class _Tree(HTMLParser):
    """A bounded view of visible source DOM; no script or handler execution."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root, self.hidden, self.count = _Node("root"), 0, 0
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN:
            self.hidden += 1
        if self.hidden:
            return
        self.count += 1
        if self.count > 100_000 or len(self.stack) > 100:
            raise ValueError("WTO 目录节点或嵌套超过上限。")
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in _VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in _HIDDEN:
                self.hidden -= 1
            return
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        if not self.hidden:
            self.stack[-1].children.append(value)


def _one(nodes, predicate, label):
    matches = [node for node in nodes if predicate(node)]
    if len(matches) != 1:
        raise ValueError(f"WTO 目录的{label}缺失或不唯一。")
    return matches[0]


def _canonical_link(url, base):
    from .discovery import _canonical

    return _canonical(url, base)[0]


def _scope(member, url, content, provider, documents, **extra):
    return {
        "provider": provider,
        "member": member,
        "member_name": MEMBERS[member]["name"],
        "source_url": url,
        "source_sha256": sha256(content).hexdigest(),
        "documents": documents,
        "legal_status_inferred": False,
        "exhaustive_content_verified": False,
        "itip_exports_verified": False,
        **extra,
    }


def _goods_catalog(url, content, tree, rules):
    from .discovery import _result

    member, info = rules.wto_member, MEMBERS[rules.wto_member]
    nodes = list(tree.nodes())
    article = _one(nodes, lambda node: node.tag == "article" and node.has("member"), "成员正文")
    if not any(node.tag == "h1" and node.text() == info["name"] for node in article.nodes()):
        raise ValueError("WTO 商品目录成员名称不符。")
    schedule = _one(article.nodes(), lambda node: node.has("schedule-label"), "承诺表号")
    if schedule.text() != f"Schedule {info['schedule']} ({info['number']})":
        raise ValueError("WTO 商品目录承诺表号与清单成员不符。")
    shortlink = _one(
        nodes, lambda node: node.tag == "link" and node.attrs.get("rel") == "shortlink", "成员标识"
    )
    if _canonical_link(shortlink.attrs.get("href", ""), url) != f"https://{GOODS}/node/{info['node']}":
        raise ValueError("WTO 商品目录成员标识与清单不符。")
    view = _one(article.nodes(), lambda node: node.has("view-display-id-all_documents"), "完整程序目录")
    fields = [node for node in view.nodes() if node.tag == "input"]
    statuses = [
        node.attrs.get("value")
        for node in fields
        if node.attrs.get("name") == "status" and "checked" in node.attrs
    ]
    if statuses != ["All"] or any(
        node.attrs.get("name", "").startswith("type_1[") and "checked" in node.attrs for node in fields
    ):
        raise ValueError("WTO 商品目录未保留 Any 状态和 pre-WTO 文件。")
    header = _one(view.nodes(), lambda node: node.has("view-header"), "结果总数")
    total_match = re.match(r"^(\d{1,6}) results\b", header.text())
    if not total_match or int(total_match[1]) > 20_000:
        raise ValueError("WTO 商品目录结果总数无效。")
    total = int(total_match[1])
    page = int(_query(url).get("page", "0"))
    table = _one(view.nodes(), lambda node: node.tag == "table", "程序表格")
    rows = [
        node
        for node in table.nodes()
        if node.tag == "tr" and any(child.tag == "td" for child in node.nodes())
    ]
    if len(rows) != max(0, min(10, total - page * 10)) or (total and page * 10 >= total):
        raise ValueError("WTO 商品目录分页行数与总数不一致。")
    links, records, seen, warnings = [], [], set(), []
    for row in rows:
        source_cells = [node for node in row.nodes() if node.tag == "td"]
        cells = {node.attrs.get("headers"): node for node in source_cells}
        required = {
            "view-title-table-column",
            "view-field-last-updated-table-column",
            "view-fields-procedure-type-table-column",
            "view-field-procedure-status-table-column",
            "view-procedure-certifications-table-column",
            "view-fields-access-document-member-table-column",
        }
        if set(cells) != required or len(source_cells) != len(required):
            raise ValueError("WTO 商品程序目录字段缺失或不一致。")
        symbol = cells["view-title-table-column"].text()
        if not symbol or len(symbol) > 300 or symbol in seen:
            raise ValueError("WTO 商品程序标识重复或无效。")
        seen.add(symbol)
        originals = []
        for node in cells["view-fields-access-document-member-table-column"].nodes():
            if node.tag != "a" or not node.attrs.get("href"):
                continue
            target = _canonical_link(node.attrs["href"], url)
            if purpose(target, member) == "detail":
                originals.append(target)
                links.append({"url": target, "purpose": "detail", "title": symbol})
        records.append(
            {
                "identity_key": f"wto-goods:{member}:procedure:{symbol}",
                "procedure_symbol": symbol,
                "schedule": info["schedule"],
                "schedule_number": info["number"],
                "member_id": info["node"],
                "status": cells["view-field-procedure-status-table-column"].text(),
                "procedure_type": cells["view-fields-procedure-type-table-column"].text(),
                "last_date": cells["view-field-last-updated-table-column"].text(),
                "certifications": cells["view-procedure-certifications-table-column"].text(),
                "file_browser_urls": originals,
                "original_policy": "public_file_browser" if originals else "catalog_only",
                "missing_original": not originals,
                "legal_review_status": "not_reviewed",
            }
        )
        if not originals:
            warnings.append(f"WTO 商品程序 {symbol} 没有公开原件目录；已保留状态，不能认定正文完整。")
    pagers = [node for node in view.nodes() if node.tag == "nav" and node.has("pager")]
    last = max(0, (total - 1) // 10)
    pages = set()
    for pager in pagers:
        for node in pager.nodes():
            if node.tag != "a":
                continue
            target = _canonical_link(node.attrs.get("href", ""), url)
            if (
                purpose(target, member) != "catalog"
                or urlsplit(target).path.rstrip("/") != f"/member/{info['slug']}"
            ):
                raise ValueError("WTO 商品分页跳出当前成员或改变范围。")
            target_page = int(_query(target).get("page", "0"))
            if target_page > last:
                raise ValueError("WTO 商品分页超过目录总数。")
            pages.add(target_page)
            if target_page != page:
                links.append(
                    {
                        "url": target,
                        "purpose": "catalog",
                        "title": f"{info['name']} catalog page {target_page + 1}",
                    }
                )
    if last and (not pagers or page not in pages or (page < last and page + 1 not in pages)):
        raise ValueError("WTO 商品目录缺少可核对的真实分页控制。")
    bundle = f"https://{GOODS}/member/download/{info['node']}"
    if any(
        node.tag == "a" and _canonical_link(node.attrs.get("href", ""), url) == bundle
        for node in header.nodes()
    ):
        links.append(
            {"url": bundle, "purpose": "catalog", "title": "Download all public documents (bundle audit)"}
        )
    if len(links) > rules.max_links:
        raise ValueError("WTO 商品目录链接超过单页上限。")
    result = _result(
        matched=True,
        purpose="catalog",
        links=list({entry["url"]: entry for entry in links}.values()),
        warnings=warnings,
    )
    result["scope"] = _scope(
        member,
        url,
        content,
        "wto-goods-public",
        records,
        total=total,
        page=page,
        page_size=10,
        returned=len(rows),
        schedule=info["schedule"],
        schedule_number=info["number"],
        member_id=info["node"],
        status_filter="All",
        include_pre_wto=True,
    )
    return result


def _goods_browser(url, content, tree, rules):
    from .discovery import _result

    member, info = rules.wto_member, MEMBERS[rules.wto_member]
    browser = _one(
        tree.nodes(),
        lambda node: node.has("node-file-browser") and (node.has("procedure") or node.has("pre-ur")),
        "程序原件正文",
    )
    schedule = _one(
        browser.nodes(), lambda node: node.has("field--name-field-procedure-schedule"), "程序承诺表号"
    )
    if schedule.text() != info["schedule"]:
        raise ValueError("WTO 商品原件目录不是当前成员承诺表。")
    title = _one(tree.nodes(), lambda node: node.tag == "h1" and node.has("page-title"), "程序标题").text()
    if not title or len(title) > 300:
        raise ValueError("WTO 商品程序标题无效。")
    links, records, warnings, seen = [], [], [], set()
    for collection in [node for node in browser.nodes() if node.has("accordion-download")]:
        heading = _one(collection.nodes(), lambda node: node.has("accordion-header"), "文件族标题").text()
        for document in [node for node in collection.nodes() if node.has("paragraph-document")]:
            dates = [
                item.text()
                for node in document.nodes()
                if node.has("field--type-datetime")
                for item in node.nodes()
                if item.has("field__item")
            ]
            for file in [node for node in document.nodes() if node.has("file-item")]:
                filename = _one(file.nodes(), lambda node: node.has("name"), "原件名称").text()
                for language in [node for node in file.nodes() if node.has("lang")]:
                    anchors = [
                        node for node in language.nodes() if node.tag == "a" and node.attrs.get("href")
                    ]
                    if len(anchors) != 1:
                        raise ValueError("WTO 商品原件语言与地址无法一一对应。")
                    target = _canonical_link(anchors[0].attrs["href"], url)
                    accepted = purpose(target, member) == "document"
                    records.append(
                        {
                            "identity_key": f"wto-goods:{member}:file:{sha256(target.encode()).hexdigest()}",
                            "original_url": target,
                            "filename": filename,
                            "language": language.text(),
                            "collection": heading,
                            "procedure_symbol": title,
                            "document_dates": dates,
                            "schedule": info["schedule"],
                            "schedule_number": info["number"],
                            "source_format": urlsplit(target).path.rsplit(".", 1)[-1].lower(),
                            "original_policy": "public_original" if accepted else "unsupported_public_path",
                            "legal_review_status": "not_reviewed",
                            "representation_equivalence_verified": False,
                        }
                    )
                    if not accepted:
                        warnings.append("WTO 商品目录出现尚未验证的原件路径；已保留定位，不能认定采集完整。")
                    elif target not in seen:
                        seen.add(target)
                        links.append(
                            {
                                "url": target,
                                "purpose": "document",
                                "title": f"{title}: {filename} ({language.text()})"[:300],
                            }
                        )
    if len(links) > rules.max_links:
        raise ValueError("WTO 商品原件超过单页候选上限。")
    if len(records) > 1000:
        raise ValueError("WTO 商品原件语种映射超过单页上限。")
    if not records:
        warnings.append("WTO 商品程序未提供公开原件；目录已保留，内容完整性未确认。")
    result = _result(matched=True, purpose="detail", links=links, warnings=warnings)
    result["scope"] = _scope(
        member,
        url,
        content,
        "wto-goods-public",
        records,
        schedule=info["schedule"],
        schedule_number=info["number"],
        procedure_symbol=title,
    )
    return result


def _xml_catalog(url, content, mime, rules):
    from .discovery import _result

    member, family = rules.wto_member, catalog_family(url, rules.wto_member)
    xml = content.decode("utf-8", errors="strict")
    if (
        mime not in {"text/xml", "application/xml"}
        or "\x00" in xml
        or re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, re.I)
    ):
        raise ValueError("WTO 公开目录必须返回不含实体声明的 XML。")
    root = ElementTree.fromstring(xml)
    if (
        root.tag != "autnresponse"
        or len(root.findall("response")) != 1
        or root.findtext("response") != "SUCCESS"
        or len(root.findall("responsedata")) != 1
        or len(list(root.iter())) > 10_000
    ):
        raise ValueError("WTO 公开 XML 目录响应无效。")
    data = root.find("responsedata")
    if data is None:
        raise ValueError("WTO 公开 XML 目录缺失数据。")
    counts = []
    for count_field in ("numhits", "totalhits"):
        value = data.findtext(_AUTN + count_field, "")
        if len(data.findall(_AUTN + count_field)) != 1 or not re.fullmatch(r"0|[1-9]\d{0,5}", value):
            raise ValueError("WTO XML 目录计数无效。")
        counts.append(int(value))
    hits = data.findall(_AUTN + "hit")
    if counts[0] != counts[1] or counts[0] != len(hits) or len(hits) > 100:
        raise ValueError("WTO XML 目录截断或超过 100 项；须补充分页，不能认定完整。")
    links, records, seen, warnings = [], [], set(), []
    if not hits:
        warnings.append("WTO 公开文件族返回空目录；原件是否缺失仍待核对。")
    generic = family in PROTOCOL_SOURCES
    for hit in hits:
        if hit.findtext(_AUTN + "database") != "English":
            raise ValueError("WTO XML 目录返回了非英文文件族。")
        document = hit.find(_AUTN + "content" + "/DOCUMENT")
        if (
            document is None
            or any(list(node) for node in document)
            or len({node.tag for node in document}) != len(document)
        ):
            raise ValueError("WTO XML 目录文书字段缺失或重复。")
        fields = {node.tag: node.text or "" for node in document}
        if any(len(value) > 20_000 for value in fields.values()):
            raise ValueError("WTO XML 目录字段超过上限。")
        catid, symbol = fields.get("CATID", ""), fields.get("SYMBOL", "")
        if not re.fullmatch(r"[1-9]\d{0,9}", catid) or catid in seen:
            raise ValueError("WTO XML 文书标识无效或重复。")
        seen.add(catid)
        if generic:
            if family not in symbol.split("#"):
                raise ValueError("WTO 修改文书符号与已核目录不符。")
        elif (
            fields.get("CONCERNEDCOUNTRIES") != MEMBERS[member]["name"]
            or fields.get("COLLECTION") != "SCHD"
            or not re.fullmatch(
                re.escape(family) + r"(?:/(?:Suppl\.[1-9]\d{0,2}|Rev\.[1-9]\d{0,2}|Corr\.[1-9]\d{0,2}))*",
                symbol,
            )
        ):
            raise ValueError("WTO 服务文书符号或国别与清单不符。")
        originals = []
        public = fields.get("HasAccess") == "True"
        if not public:
            warnings.append(f"WTO 文书 {symbol} 没有已验证的公开原件权限。")
        for slot in ("FILENAMESA", "FILENAMESB"):
            filename = fields.get(slot, "")
            if not filename:
                continue
            target = f"https://{DOCS}/dol2fe/Pages/SS/directdoc2.aspx?" + urlencode(
                {"Open": "True", "filename": filename}
            )
            target = _canonical_link(target, url)
            valid = original_filename(target, member) is not None
            valid = valid and filename_matches(filename, symbol, family, member)
            if not valid:
                warnings.append(f"WTO 文书 {symbol} 的 {slot} 尚未核实为对应原件。")
                continue
            originals.append({"slot": slot, "original_url": target, "filename": filename})
            if public:
                links.append(
                    {
                        "url": target,
                        "purpose": "document",
                        "title": symbol,
                        "identity_key": f"wto-docs:{catid}:{slot}",
                    }
                )
        safe_fields = (
            "CATID",
            "SYMBOL",
            "CATTITLE",
            "CONCERNEDCOUNTRIES",
            "COLLECTION",
            "ISSUINGDATE",
            "RESTRICTIONTYPENAME",
            "HasAccess",
            "FILENAMESA",
            "FILENAMESB",
            "FILENAMESC",
        )
        records.append(
            {
                "identity_key": f"wto-docs:{catid}",
                "title": fields.get("CATTITLE", "")[:1000],
                "symbol": symbol,
                "source_metadata": {key: fields[key] for key in safe_fields if key in fields},
                "originals": originals,
                "original_policy": "public_original" if public else "catalog_only",
                "missing_original": not (public and originals),
                "legal_review_status": "not_reviewed",
            }
        )
    if len(links) > rules.max_links:
        raise ValueError("WTO XML 原件超过单页候选上限。")
    result = _result(matched=True, purpose="catalog", links=links, warnings=warnings)
    result["scope"] = _scope(
        member,
        url,
        content,
        "wto-docs-public",
        records,
        total=counts[1],
        returned=len(hits),
        family=family,
        language="English",
        protocol_symbol_source=PROTOCOL_SOURCES.get(family),
        member_bound=not generic,
    )
    return result


def inspect(url: str, content: bytes, mime: str, rules) -> dict | None:
    """Return None only for paths handled by the existing HTML/PDF discovery."""
    from .discovery import _result

    parsed, member = urlsplit(url), rules.wto_member
    # Public generic legal texts also publish native Word representations.
    # Keep the existing frozen recipe's URL scope; this is not a general
    # binary-file detector or permission to follow unrelated office files.
    if (
        parsed.hostname == "www.wto.org"
        and rules.purpose(url) == "document"
        and re.fullmatch(r"/english/docs_e/legal_e/(?:downloads_e/)?[^/]+\.(?:doc|docx)", parsed.path, re.I)
    ):
        extension = parsed.path.rsplit(".", 1)[-1].lower()
        signature = b"PK\x03\x04" if extension == "docx" else b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        if mime in {"text/html", "application/xhtml+xml"} or not content.startswith(signature):
            return _result(reason="WTO 公共法律原件签名不符，不能用拒绝页替代。")
        result = _result(matched=True, purpose="document")
        result["scope"] = {
            "provider": "wto-public-original",
            "member": member,
            "source_format": extension,
            "header_signature_verified": True,
            "text_complete_verified": False,
            "legal_status_inferred": False,
        }
        return result
    kind = purpose(url, member)
    if parsed.hostname not in {GOODS, DOCS} or not kind:
        return None
    try:
        if parsed.hostname == GOODS and parsed.path == f"/member/download/{MEMBERS[member]['node']}":
            if not content.startswith(b"PK\x03\x04"):
                raise ValueError("WTO 成员审计压缩包签名不符。")
            result = _result(
                matched=True,
                purpose="catalog",
                warnings=["成员压缩包仅作来源审计；未核对全部成员和目录差异，不构成法规正文或完整性证明。"],
            )
            result["scope"] = {
                "provider": "wto-goods-bundle-audit",
                "member": member,
                "member_id": MEMBERS[member]["node"],
                "original_url": url,
                "bundle_members_verified": False,
                "legal_status_inferred": False,
                "exhaustive_content_verified": False,
                "documents": [],
            }
            return result
        if kind == "document":
            extension = ".pdf" if parsed.hostname == DOCS else "." + parsed.path.rsplit(".", 1)[-1].lower()
            signatures = {
                ".pdf": b"%PDF-",
                ".doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
                ".xls": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
                ".docx": b"PK\x03\x04",
                ".mdb": b"\x00\x01\x00\x00Standard Jet DB",
                ".wpf": b"\xffWPC",
            }
            if not content.startswith(signatures.get(extension, b"\x00invalid")):
                known = _UNVERIFIED_MDB.get(url)
                if (
                    extension == ".mdb"
                    and known
                    and known["member"] == member
                    and mime in {"application/msaccess", "application/octet-stream"}
                    and len(content) == known["size"]
                    and b"\x00" in content[:512]
                    and sha256(content).hexdigest() == known["source_sha256"]
                ):
                    result = _result(
                        matched=True,
                        purpose="document",
                        warnings=["历史 MDB 原件已逐字节保存，文件格式尚未识别，不能进入正文或检索。"],
                    )
                    result["scope"] = {
                        "provider": "wto-public-original",
                        "member": member,
                        "source_format": "mdb",
                        "format_verified": False,
                        "header_signature_verified": False,
                        "pending_format_verification": True,
                        "text_complete_verified": False,
                        "legal_status_inferred": False,
                        "original_url": url,
                        "source_sha256": known["source_sha256"],
                        "catalog_url": known["catalog_url"],
                        "previous_verified_source_hash": known["catalog_sha256"],
                    }
                    return result
                raise ValueError("WTO 原件扩展名与文件签名不一致；不能用 HTML 错误页替代。")
            result = _result(matched=True, purpose="document")
            result["scope"] = {
                "provider": "wto-public-original",
                "member": member,
                "source_format": extension.removeprefix("."),
                "header_signature_verified": True,
                "text_complete_verified": False,
                "legal_status_inferred": False,
            }
            return result
        if len(content) > MAX_BYTES:
            raise ValueError("WTO 目录超过 4 MiB 上限。")
        if parsed.hostname == DOCS and parsed.path == XML_PATH:
            return _xml_catalog(url, content, mime, rules)
        if mime not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("WTO 目录或查看器没有返回 HTML。")
        if parsed.hostname == DOCS:
            from .ingestion import _decode

            match = re.search(r"window\.location\.href\s*=\s*(['\"])([^'\"<>]{1,2048})\1", _decode(content))
            target = _canonical_link(match[2], url) if match else ""
            if purpose(target, member) != "document" or original_filename(
                target, member
            ) != original_filename(url, member):
                raise ValueError("WTO 文档查看器未定位到同一份原件。")
            return _result(
                matched=True,
                purpose="detail",
                links=[{"url": target, "purpose": "document", "title": original_filename(target, member)}],
            )
        tree = _Tree()
        tree.feed(content.decode("utf-8", errors="strict"))
        tree.close()
        if kind == "detail":
            return _goods_browser(url, content, tree.root, rules)
        return _goods_catalog(url, content, tree.root, rules)
    except (ValueError, UnicodeDecodeError, ElementTree.ParseError, RecursionError, TypeError) as exc:
        return _result(reason=str(exc)[:500] or "WTO 目录无法核实。")
