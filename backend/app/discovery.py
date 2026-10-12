"""Bounded discovery from trusted checklist recipes, without legal approval.

These functions do no networking, persistence, parsing or indexing. A discovered
document is a candidate for review, not an assertion about its type or force.
Regular expressions are repository-owned configuration, never model or API input.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from .ingestion import _decode, _validated_url

MAX_HTML_BYTES = 4 * 1024 * 1024
MAX_PAGE_LINKS = 10_000
_PATTERN_FIELDS = (
    "catalog_patterns",
    "detail_patterns",
    "document_patterns",
    "html_document_patterns",
    "exclude_patterns",
)
_ADAPTERS = {"bpk", "ma_jdih", "sso", "html_links", "manual"}
_HIDDEN_TAGS = {"script", "style", "template", "noscript"}
_ERROR_HEADING = re.compile(
    r"(?:^|\b)(?:just a moment|access denied|403\s+forbidden|404\s+(?:error|not found)|"
    r"page not found|service unavailable|internal server error|checking your browser|"
    r"attention required|verify (?:that )?you are human|sign in|log in|login)(?:\b|$)",
    re.I,
)
_ERROR_BODY = re.compile(
    r"checking your browser|verify (?:that )?you are human|enable javascript and cookies to continue|"
    r"you (?:do not|don't) have permission to access|the page you requested could not be found",
    re.I,
)


def _canonical(url: str, base: str | None = None) -> tuple[str, str]:
    value = urljoin(base, url) if base else url
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


@dataclass
class _Link:
    url: str
    tag: str
    text: list[str] = field(default_factory=list)
    title: str = ""


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[_Link] = []
        self.active: _Link | None = None
        self.hidden: list[str] = []
        self.title: list[str] = []
        self.headings: list[str] = []
        self.text: list[str] = []
        self.in_title = False
        self.in_heading = False
        self.challenge = False
        self.truncated = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in _HIDDEN_TAGS:
            self.hidden.append(tag)
        if tag == "script" and "/cdn-cgi/challenge-platform/" in (values.get("src") or ""):
            self.challenge = True
        if self.hidden:
            return
        if (values.get("id") or "").casefold() in {
            "challenge-running",
            "cf-challenge-running",
            "cf-error-details",
        }:
            self.challenge = True
        self.in_title = self.in_title or tag == "title"
        self.in_heading = self.in_heading or tag in {"h1", "h2", "h3"}
        if tag not in {"a", "iframe", "embed", "object"}:
            return
        target = values.get("href" if tag == "a" else "data" if tag == "object" else "src")
        if not target:
            return
        if len(self.links) >= MAX_PAGE_LINKS:
            self.truncated = True
            return
        link = _Link(target.strip(), tag, title=(values.get("title") or values.get("aria-label") or "")[:300])
        self.links.append(link)
        if tag == "a":
            self.active = link

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if tag == "a":
            self.active = None
        elif tag == "title":
            self.in_title = False
        elif tag in {"h1", "h2", "h3"}:
            self.in_heading = False

    def handle_data(self, data):
        if self.hidden:
            return
        value = " ".join(data.split())
        if not value:
            return
        self.text.append(value)
        if self.in_title:
            self.title.append(value)
        if self.in_heading:
            self.headings.append(value)
        if self.active:
            # Extremely large anchor text must not be copied into every record.
            if sum(map(len, self.active.text)) < 500:
                self.active.text.append(value[:500])


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
        self.reason = recipe.get("reason")

    @staticmethod
    def matches(url: str, patterns) -> bool:
        parsed = urlsplit(url)
        target = unquote(parsed.path) + ("?" + unquote(parsed.query) if parsed.query else "")
        return any(pattern.search(target) for pattern in patterns)

    def purpose(self, url: str) -> str | None:
        if urlsplit(url).hostname not in self.hosts or self.matches(url, self.exclude_patterns):
            return None
        for purpose, patterns in (
            ("catalog", self.catalog_patterns),
            ("document", self.html_document_patterns),
            ("document", self.document_patterns),
            ("detail", self.detail_patterns),
        ):
            if self.matches(url, patterns):
                return purpose
        return None

    def query_allowed(self, url: str) -> bool:
        values = _query_values(url)
        for key, expected in self.required_query.items():
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


def _identity(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.hostname == "sso.agc.gov.sg":
        match = re.match(r"^/(Act|SL)/([^/]+)", parsed.path, re.I)
        if match:
            return f"sso:{match[1].lower()}:{match[2].lower()}"
    return None


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
    if mime == "application/pdf" or content.startswith(b"%PDF-"):
        if purpose == "document" and content.startswith(b"%PDF-"):
            return _result(matched=True, purpose=purpose)
        return _result(reason="PDF 地址、响应类型或文件签名不匹配。")
    if mime not in {"text/html", "application/xhtml+xml"}:
        return _result(reason="该格式不进入通用法规发现；数据集须单独审核。")
    if len(content) > MAX_HTML_BYTES:
        return _result(reason="HTML 页面超过 4 MiB 发现上限。")
    page = _Page()
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
    if not visible:
        return _result(reason="页面没有可见内容，可能需要脚本或人工复核。")
    if purpose == "document" and not rules.matches(current, rules.html_document_patterns):
        return _result(reason="文件地址返回 HTML，未取得配置要求的原件。")
    candidates = []
    seen = {current}
    warnings = []
    for link in page.links:
        if not link.url or link.url.startswith("#"):
            continue
        try:
            candidate, _ = _canonical(link.url, current)
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
        context = (label + " " + unquote(candidate)).casefold()
        type_context = (label + " " + unquote(urlsplit(candidate).path)).casefold()
        if candidate_purpose == "catalog":
            if not rules.query_allowed(candidate):
                continue
            if (
                rules.adapter == "bpk"
                and rules.bpk_types
                and urlsplit(candidate).path.casefold() == "/search"
            ):
                same_type = _query_values(candidate).get("jenis") == _query_values(current).get("jenis")
                if not same_type and not any(term in type_context for term in rules.bpk_types):
                    continue
        else:
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
                and rules.bpk_types
                and not any(term in type_context for term in rules.bpk_types)
            ):
                if purpose == "catalog":
                    continue
        seen.add(candidate)
        if len(candidates) >= rules.max_links:
            warnings.append("单页候选达到上限，仍有链接未登记；不能据此认定目录完整。")
            break
        entry = {"url": candidate, "purpose": candidate_purpose, "title": label}
        identity = _identity(candidate)
        if identity:
            entry["identity_key"] = identity
        candidates.append(entry)
    if page.truncated:
        warnings.append("页面链接超过扫描上限，需缩小范围或补充适配。")
    if not candidates and purpose != "document":
        warnings.append("未发现配置范围内的子页面或原件；请核对空目录、动态分页及访问限制。")
    return _result(matched=True, purpose=purpose, links=candidates, warnings=warnings)


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
    if adapter == "bpk":
        result.update(
            catalog_patterns=[r"^/Search(?:\?|$)"]
            + [rf"^{re.escape(urlsplit(x['url']).path)}(?:\?|$)" for x in seeds if "/Jenis/" in x["url"]],
            detail_patterns=[r"^/Details/\d+/[^/?]+(?:\?|$)"],
            document_patterns=[r"^/(?:Home/)?Download/\d+/[^?]+(?:\?|$)"],
        )
        jenis = {_query_values(entry["url"]).get("jenis", [None])[0] for entry in seeds}
        if len(jenis) == 1 and None not in jenis:
            result["required_query"] = {"jenis": jenis.pop()}
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
        return result
    if adapter == "sso":
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
    for seed in seeds:
        path = urlsplit(seed["url"]).path
        result["catalog_patterns"].append(rf"^{re.escape(path)}/?(?:\?|$)")
    for host in hosts:
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
    result["document_patterns"] = [
        r"^/(?:sites/default/files|files|uploads?|storage|media|docs|documents|wp-content/uploads|assets)/[^?]+\.pdf(?:\?|$)",
    ]
    if "putusan3.mahkamahagung.go.id" in hosts:
        result["document_patterns"].append(r"^/direktori/download_file/[^/?]+/[^/?]+(?:\?|$)")
    if "s.mkri.id" in hosts:
        result["document_patterns"].append(r"^/public/content/infoumum/regulation/pdf/[^?]+\.pdf(?:\?|$)")
    result["exclude_patterns"] = [
        r"(?:^|/)(?:login|logout|sign-in|careers|contact-us|sitemap|news|news-events|press-releases|events)(?:/|\?|$)",
        r"(?:^|/)(?:privacy-policy|terms-of-use)(?:/|\?|$)",
    ]
    return result
