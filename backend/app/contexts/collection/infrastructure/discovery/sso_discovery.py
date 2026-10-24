"""Read SSO's published browse rows without following navigation permutations.

The source count describes a particular catalogue snapshot, not verified legal
force or acquired full text. Every row retains its literal versioned links.
"""

from __future__ import annotations

import hashlib
import math
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

HOST = "sso.agc.gov.sg"
BROWSE = re.compile(
    r"^/Browse/(?P<category>Act|SL)/(?P<state>Current|Uncommenced|Repealed|Revoked|Spent|RepealedSpent|RevokedSpent)"
    r"(?:/(?P<letter>All|[A-Z]))?(?:/(?P<index>\d{1,6}))?/?$",
    re.I,
)
DOCUMENT = re.compile(
    r"^/(Act|SL)/([A-Za-z0-9_-]+)(?:/(\d{8}|Uncommenced|Repealed|Revoked|Spent)(?:/\d{8})?)?/?$", re.I
)
MAX_ROWS = 500


def failure(reason):
    return {"matched": False, "purpose": None, "links": [], "warnings": [], "reason": reason}


def _query(url):
    pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True, max_num_fields=20)
    result = {key.casefold(): value for key, value in pairs}
    if len(result) != len(pairs):
        raise ValueError("duplicate query fields")
    return result


def _url(value, base):
    parsed = urlsplit(urljoin(base, value))
    if parsed.scheme != "https" or parsed.netloc != HOST or parsed.fragment or "\\" in parsed.path:
        raise ValueError("unexpected source link")
    pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=20)
    # Cache-busting timestamps do not name legal editions. DocDate is retained.
    pairs = [(key, value) for key, value in pairs if key != "_" and key.casefold() != "provids"]
    return urlunsplit(("https", HOST, parsed.path, urlencode(sorted(pairs)), ""))


class _Catalog(HTMLParser):
    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.offsets = [0]
        for match in re.finditer("\n", source):
            self.offsets.append(match.end())
        self.tables = []
        self.table_count = 0
        self.body = False
        self.row = None
        self.rows = []
        self.anchor = None
        self.hidden = []
        self.fields = []
        self.values = {"count": [], "index": [], "size": []}
        self.next_links = []
        self.number = False
        self.incomplete = False

    def source_offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if self.hidden:
            if tag in {"script", "style", "template", "noscript"}:
                self.hidden.append(tag)
            return
        if tag in {"script", "style", "template", "noscript"}:
            self.hidden.append(tag)
            return
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "table":
            is_browse = "browse-list" in classes
            self.tables.append(is_browse)
            self.table_count += is_browse
        elif tag == "tbody" and self.tables and self.tables[-1]:
            self.body = True
        elif tag == "tr" and self.tables and self.tables[-1] and self.body:
            if self.row is not None or len(self.rows) >= MAX_ROWS:
                raise ValueError("invalid catalogue row count")
            self.row = {"start": self.source_offset(), "anchors": [], "number": []}
        elif tag == "td" and self.row is not None:
            self.number = "col-no" in classes
        if tag == "a":
            if values.get("aria-label") == "Next Page":
                self.next_links.append(values.get("href") or "")
            if self.row is not None:
                if self.anchor is not None:
                    raise ValueError("nested catalogue anchor")
                self.anchor = {"href": values.get("href") or "", "classes": classes, "text": []}
        field = (
            "count"
            if "page-count" in classes
            else "index"
            if "page-index" in classes
            else "size"
            if "page-size" in classes
            else None
        )
        if field:
            self.fields.append({"tag": tag, "kind": field, "text": []})

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
            return
        if self.fields and tag == self.fields[-1]["tag"]:
            field = self.fields.pop()
            self.values[field["kind"]].append(" ".join(" ".join(field["text"]).split()))
        if tag == "a" and self.anchor is not None:
            self.row["anchors"].append(self.anchor)
            self.anchor = None
        if tag == "td":
            self.number = False
        elif tag == "tr" and self.row is not None:
            if self.anchor is not None:
                raise ValueError("unclosed catalogue anchor")
            self.row["end"] = self.source_offset() + len("</tr>")
            self.rows.append(self.row)
            self.row = None
        elif tag == "tbody" and self.tables and self.tables[-1]:
            self.body = False
        elif tag == "table":
            if not self.tables:
                raise ValueError("unbalanced catalogue table")
            self.tables.pop()

    def handle_data(self, data):
        if self.hidden:
            return
        if self.fields:
            self.fields[-1]["text"].append(data)
        if self.anchor is not None:
            self.anchor["text"].append(data)
        if self.number and self.row is not None:
            self.row["number"].append(data)


def _single(values, pattern):
    distinct = set(values)
    if len(distinct) != 1:
        raise ValueError("catalogue controls disagree")
    match = re.fullmatch(pattern, next(iter(distinct)))
    if not match:
        raise ValueError("catalogue controls absent or invalid")
    return tuple(int(value.replace(",", "")) for value in match.groups())


def inspect_catalog(url, content, rules):
    """Validate row counts and emit each row once plus its actual next page."""
    try:
        from app.contexts.knowledge.infrastructure.parsing.native import _decode

        source = _decode(content)
        parsed = urlsplit(url)
        context = BROWSE.fullmatch(parsed.path)
        if parsed.hostname != HOST or not context:
            raise ValueError("unsupported catalogue path")
        parser = _Catalog(source)
        parser.feed(source)
        parser.close()
        if parser.table_count != 1 or parser.row is not None or parser.tables:
            raise ValueError("missing or truncated catalogue table")
        total, pages = _single(parser.values["count"], r"([\d,]+) results? in ([\d,]+) pages?")
        (visible_index,) = _single(parser.values["index"], r"(\d+)")
        (page_size,) = _single(parser.values["size"], r"(\d+)(?: items)?")
        page_index = int(context["index"] or 0)
        values = _query(url)
        if (
            not 0 < total <= 1_000_000
            or page_size not in {10, 20, 50, 100, 250, 500}
            or pages != math.ceil(total / page_size)
            or visible_index != page_index + 1
            or page_index >= pages
            or int(values.get("pagesize", page_size)) != page_size
            or len(parser.rows) != min(page_size, total - page_index * page_size)
        ):
            raise ValueError("catalogue totals or row count disagree")
        documents, links, identities = [], [], set()
        source_hash = hashlib.sha256(content).hexdigest()
        for number, row in enumerate(parser.rows, 1):
            anchors = [
                a
                for a in row["anchors"]
                if not a["classes"] & {"file-download", "add-to-collection", "browse-actions"}
            ]
            candidates = []
            for anchor in anchors:
                target = _url(anchor["href"], url)
                match = DOCUMENT.fullmatch(urlsplit(target).path)
                query = _query(target)
                if (
                    match
                    and match[1].casefold() == context["category"].casefold()
                    and "viewtype" not in query
                ):
                    label = " ".join(" ".join(anchor["text"]).split())
                    if label:
                        candidates.append((target, match, query, label))
            if len(candidates) != 1:
                raise ValueError("row lacks one explicit title link")
            html_url, match, query, title = candidates[0]
            identity = f"sso:{match[1].lower()}:{match[2].lower()}"
            if identity in identities:
                raise ValueError("duplicate legislation row")
            identities.add(identity)
            pdfs = set()
            for anchor in row["anchors"]:
                if "file-download" not in anchor["classes"]:
                    continue
                target = _url(anchor["href"], url)
                p = urlsplit(target)
                pdf_query = _query(target)
                if p.path != urlsplit(html_url).path or pdf_query != dict(query, viewtype="Pdf"):
                    raise ValueError("PDF is not the same explicit version as the title")
                pdfs.add(target)
            if len(pdfs) > 1:
                raise ValueError("conflicting original links")
            original = next(iter(pdfs), html_url)
            purpose = rules.purpose(original)
            if purpose not in {"document", "detail"}:
                raise ValueError("original outside checklist rule")
            locator = {
                "source_sha256": source_hash,
                "table_class": "browse-list",
                "row": number,
                "unit": "decoded_unicode_char",
                "start": row["start"],
                "end": row["end"],
                "fragment_sha256": hashlib.sha256(source[row["start"] : row["end"]].encode()).hexdigest(),
            }
            documents.append(
                {
                    "identity_key": identity,
                    "title": title[:1000],
                    "number": " ".join(" ".join(row["number"]).split()),
                    "html_url": html_url,
                    "pdf_url": next(iter(pdfs), None),
                    "original_url": original,
                    "document_date": query.get("docdate"),
                    "source_status": context["state"],
                    "source_locator": locator,
                }
            )
            links.append(
                {
                    "url": original,
                    "purpose": purpose,
                    "title": title[:300],
                    "identity_key": identity,
                    "source_locator": locator,
                }
            )
        next_links = {_url(value, url) for value in parser.next_links}
        if page_index + 1 == pages:
            if next_links:
                raise ValueError("last page unexpectedly has a next page")
        else:
            if len(next_links) != 1:
                raise ValueError("next page absent or conflicting")
            following = next(iter(next_links))
            route = BROWSE.fullmatch(urlsplit(following).path)
            next_query = _query(following)
            expected_query = dict(values)
            expected_query.setdefault("pagesize", str(page_size))
            expected_query.setdefault("sortby", "Title")
            expected_query.setdefault("sortorder", "ASC")
            for key in ("sortby", "sortorder"):
                expected_query[key] = expected_query[key].casefold()
                if key in next_query:
                    next_query[key] = next_query[key].casefold()
            if (
                not route
                or route["category"].casefold() != context["category"].casefold()
                or route["state"].casefold() != context["state"].casefold()
                or (route["letter"] or "All").casefold() != (context["letter"] or "All").casefold()
                or int(route["index"] or 0) != page_index + 1
                or next_query != expected_query
                or rules.purpose(following) != "catalog"
                or not rules.query_allowed(following)
            ):
                raise ValueError("next page changes catalogue filters")
            links.append({"url": following, "purpose": "catalog", "title": "Next Page"})
        if len(links) > rules.max_links:
            raise ValueError("page exceeds candidate limit; reduce PageSize")
    except (ValueError, TypeError, UnicodeError, KeyError, AssertionError, OverflowError):
        return failure("SSO 目录行、版本链接、分页或总数无法完整对账，保留缺口。")
    return {
        "matched": True,
        "purpose": "catalog",
        "links": links,
        "warnings": [],
        "reason": None,
        "scope": {
            "provider": "sso",
            "category": context["category"],
            "state": context["state"],
            "subtype": values.get("subtype"),
            "total": total,
            "pages": pages,
            "page_index": page_index,
            "page_size": page_size,
            "returned": len(documents),
            "source_sha256": source_hash,
            "documents": documents,
            "full_text_acquired": False,
            "current_effect_verified": False,
        },
    }
