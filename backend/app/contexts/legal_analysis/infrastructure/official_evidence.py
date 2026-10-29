"""Bounded official HTML acquisition; search snippets are never legal evidence."""

import asyncio
import hashlib
import json
import math
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from app.contexts.conversations.infrastructure.agent_transport import gateway_tool
from app.contexts.documents.infrastructure.parsers.html_parsing import HTMLDocument
from app.shared.domain.content import _decode
from app.shared.domain.values import now
from app.shared.infrastructure.http.download import DownloadError, _download, _validated_url


class _GuidanceSections(HTMLParser):
    """Read explicit accordion labels as section boundaries, including FAQ answers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.labels, self.parts, self.depth = [], [], 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.depth:
            self.depth += 1
        elif attrs.get("aria-controls") and (tag == "button" or attrs.get("role") == "button"):
            self.depth = 1
            self.parts = []

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
            if not self.depth:
                label = " ".join(" ".join(self.parts).split())
                if 3 <= len(label) <= 300:
                    self.labels.append(label)

    def handle_data(self, data):
        if self.depth and sum(map(len, self.parts)) < 400:
            self.parts.append(data)


def official_url(url, country):
    try:
        normalized, host = _validated_url(url)
    except (ValueError, TypeError):
        return None
    suffix = {"CN": ".gov.cn", "ID": ".go.id", "SG": ".gov.sg"}.get(country)
    if suffix and (host.endswith(suffix) or (country == "CN" and host == "npc.gov.cn")):
        return normalized
    return None


def _save(root, value):
    """Atomic public-text snapshot. No account facts or search credentials are retained."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / (value["content_hash"] + ".json")
    descriptor, temporary = tempfile.mkstemp(prefix=".source-", dir=root)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, ensure_ascii=False)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def fetch_official(data_dir, url, country, *, deadline, check_cancelled=None):
    check = check_cancelled or (lambda: None)
    normalized = official_url(url, country)
    if not normalized:
        raise ValueError("来源不属于指定法域的官方来源")
    check()

    def consume(chunks):
        parts = []
        for part in chunks:
            check()
            parts.append(part)
        return b"".join(parts)

    result = _download(
        normalized,
        deadline=deadline,
        max_bytes=2 * 1024 * 1024,
        accept_compressed_html=True,
        consume=consume,
    )
    check()
    if not official_url(result["url"], country):
        raise ValueError("来源重定向超出指定法域的官方来源")
    # PDF/Office keep their existing MinerU ingestion route; no local OCR fallback.
    if result["mime_type"] not in {"text/html", "text/plain"}:
        raise ValueError("此原文需要先完成文档解析")
    content = result.get("blob") or result.get("content")
    if not isinstance(content, bytes):
        raise ValueError("未取得来源正文")
    decoded = _decode(content)
    section_starts = []
    if result["mime_type"] == "text/html":
        parser = HTMLDocument(source=decoded, exclude_navigation=True, max_elements=10000)
        parser.feed(decoded)
        parser.finish()
        document = parser.document()
        parts, headings_at, offset = [], [], 0
        for element in document.elements:
            if not element.content_text:
                continue
            tag = element.locator.get("semantic_tag", "")
            if element.kind == "heading" and re.fullmatch(r"h[2-6]", tag):
                headings_at.append((int(tag[1]), offset))
            parts.append(element.content_text)
            offset += len(element.content_text) + 1
        text = "\n".join(parts)
        # For official guidance without numbered articles, keep complete
        # sections at the highest subheading level, including nested exceptions.
        if headings_at:
            level = min(level for level, _ in headings_at)
            section_starts = [start for depth, start in headings_at if depth == level]
        accordion = _GuidanceSections()
        accordion.feed(decoded)
        positions, offset = [], 0
        for label in accordion.labels:
            match = re.search(r"\s+".join(map(re.escape, label.split())), text[offset:])
            if match:
                offset += match.start()
                positions.append(offset)
                offset += len(match[0])
        if len(positions) >= 2:
            section_starts = positions
        if not text.strip() or document.quality.get("error_page_detected"):
            raise ValueError("来源未包含可用正文")
        headings = parser.headings
    else:
        text, headings = decoded, []
    if len(text.strip()) < 80 or len(text) > 600000:
        raise ValueError("来源正文不足或超过读取限制")
    digest = hashlib.sha256(text.encode()).hexdigest()
    head_title = re.search(r"<title\b[^>]*>([^<]{1,1000})</title\s*>", decoded, re.I)
    record = {
        "url": result["url"],
        "requested_url": normalized,
        "country": country,
        "text": text,
        "content_hash": digest,
        "fetched_at": now(),
        "title": (
            unescape(head_title[1]).strip()
            if head_title
            else str(headings[0] if headings else "官方来源原文")
        )[:1000],
        "section_starts": section_starts,
    }
    _save(Path(data_dir) / "legal-library" / "web-evidence", record)
    return record


def _terms(query, country):
    query = re.sub(r"^\s*(?:CN|ID|SG)\s*:\s*", "", query, flags=re.I)
    terms = set(re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]{2}", query.casefold()))
    if country == "ID":
        for equivalents in (
            ("食品", "food", "pangan"),
            ("进口", "import", "impor"),
            (
                "准入",
                "许可",
                "注册",
                "登记",
                "registration",
                "registrasi",
                "pendaftaran",
                "didaftarkan",
                "permit",
                "licence",
                "license",
            ),
            ("标签", "label", "pelabelan"),
            ("茶叶", "tea", "teh"),
        ):
            if any(word in query.casefold() for word in equivalents):
                terms.update(equivalents)
    return terms


def formal_title(title):
    """Use the printed Chinese instrument name, without a site's version label."""
    name = re.split(r"[（(]|\s+[-|—]\s+", title, maxsplit=1)[0].strip().strip("《》")
    return name if re.fullmatch(r"[\u3400-\u9fff]{4,100}(?:法|条例|办法|规定|解释|决定)", name) else None


def source_version(record):
    """Transcribe an explicit adoption/revision date; a fetch date is not one."""
    if record.get("country") != "CN":
        return {}
    title = formal_title(record.get("title", ""))
    if not title:
        title = next(
            (
                formal_title(line.strip())
                for line in record.get("text", "")[:1200].splitlines()
                if formal_title(line.strip())
            ),
            None,
        )
    if not title:
        return {}
    header = record.get("title", "") + "\n" + record.get("text", "")[:1200]
    dates = []
    for match in re.finditer(r"(20\d{2}|19\d{2})年(\d{1,2})月(\d{1,2})日", header):
        if re.search(r"通过|修正|修订", header[match.end() : match.end() + 65]):
            try:
                dates.append(datetime(*map(int, match.groups())).date().isoformat())
            except ValueError:
                pass
    return {"catalog_title": title, **({"declared_version_date": max(dates)} if dates else {})}


def citations_from_text(record, query, limit):
    if re.search(r"/(?:search|pencarian)(?:/|$)", urlsplit(record.get("url", "")).path, re.I):
        # A government catalogue locates documents; numbered search rows do
        # not become articles merely because the host is official.
        return []
    text = record["text"]
    # Preserve complete articles/sections, including provisos. Oversized
    # units are excluded instead of silently cropping exceptions.
    starts = [
        m.start()
        for m in re.finditer(
            r"(?:第[一二三四五六七八九十百千零〇\d]+条[\s\u3000]|^\s*Pasal\s+\d+\b)",
            text,
            re.M,
        )
    ]
    if not starts:
        starts = [n for n in record.get("section_starts", []) if type(n) is int and 0 <= n < len(text)]
    if not starts:
        starts = [m.start() for m in re.finditer(r"^\s*\d+[A-Z]?\.(?:[—–-]|\s)", text, re.M)]
    bounds = sorted(set([0, *starts, len(text)]))
    units = [(a, b, text[a:b].strip()) for a, b in zip(bounds, bounds[1:])]
    # Unknown section structure: keep the entire page or exclude it. Selecting
    # individual lines could discard an exception in the following paragraph.
    terms = _terms(query, record.get("country"))
    units = [unit for unit in units if 30 <= len(unit[2]) <= 4800]
    # FAQs often put the coverage and exemptions in separate accordion panels.
    # Keep that pair as one original interval before ranking procedural details.
    scope_pattern = r"不(?:适用|需要)|豁免|免于|除外|哪些.{0,20}(?:须|应)|what.{0,35}(?:must|required)|exempt|exceptions?|tidak wajib|apa saja yang wajib|dikecualikan|pengecualian"
    scopes = set()
    merged = []
    index = 0
    while index < len(units):
        start, end, body = units[index]
        heading = body.splitlines()[0].casefold()
        scope = bool(re.search(scope_pattern, heading, re.I)) and sum(t in heading for t in terms) >= 1
        if scope and index + 1 < len(units):
            adjacent = units[index + 1]
            next_heading = adjacent[2].splitlines()[0].casefold()
            if re.search(scope_pattern, next_heading, re.I) and adjacent[1] - start <= 4800:
                end = adjacent[1]
                body = text[start:end].strip()
                index += 1
        if scope:
            scopes.add(start)
        merged.append((start, end, body))
        index += 1
    units = sorted(
        merged,
        key=lambda unit: (unit[0] in scopes, sum(t in unit[2].casefold() for t in terms)),
        reverse=True,
    )
    items = []
    for start, end, original in units[:limit]:
        anchor = hashlib.sha256(f"{record['content_hash']}:{start}:{end}".encode()).hexdigest()
        article = re.match(r"(?:第[^条\n]{1,20}条|Pasal\s+\d+)", original)
        if not article and not record.get("section_starts"):
            article = re.match(r"\d+[A-Z]?\.", original)
        items.append(
            {
                "document_id": "web-" + record["content_hash"][:32],
                "document_version": int(record["content_hash"][:7], 16) + 1,
                "title": source_version(record).get("catalog_title") or record["title"],
                "excerpt": original,
                "country": record["country"],
                "source_url": record["url"],
                "locator": {
                    "start": start,
                    "end": end,
                    "article": article[0] if article else None,
                    "source_hash": record["content_hash"],
                    "fetched_at": record["fetched_at"],
                },
                "chunk_id": anchor,
                "chunk_hash": hashlib.sha256(original.encode()).hexdigest(),
                "metadata_revision": 0,
                "metadata": {
                    "kind": "guidance",
                    "review_status": "draft",
                    "effectivity": "unreviewed",
                    "jurisdiction": record["country"],
                    "evidence_store": "official_web",
                    "source_type": "官方页面原文，规范身份与适用版本需按具体内容判断",
                    **({"retrieval_role": "applicability_scope"} if start in scopes else {}),
                    **source_version(record),
                },
            }
        )
    return items


def cached_official(data_dir, query, country, limit=5, *, check_cancelled=None, max_age=86400):
    """Recall recent public originals, preserving whole units and fetch dates.

    This is a source cache, never an answer cache. Stale snapshots remain on
    disk as evidence but cannot suppress a current official acquisition.
    """
    check = check_cancelled or (lambda: None)
    check()
    root = Path(data_dir) / "legal-library" / "web-evidence"
    terms = _terms(query, country)
    terms -= {"gov", "import", "export", "官方", "原文", "法规"}
    if not terms:
        return []
    ranked = []
    for path in sorted(root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:2000]:
        check()
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                continue
            record = json.loads(path.read_text())
            fetched = datetime.fromisoformat(record["fetched_at"].replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - fetched).total_seconds()
            if (
                not 0 <= age <= max_age
                or record.get("country") != country
                or not official_url(record["url"], country)
            ):
                continue
            text = record["text"]
            if hashlib.sha256(text.encode()).hexdigest() != record["content_hash"]:
                continue
            haystack = (record.get("title", "") + "\n" + text).casefold()
            matches = {term for term in terms if term in haystack}
            if len(matches) < min(len(terms), max(2, math.ceil(len(terms) * 0.3))):
                continue
            score = len(matches) + sum(2 for term in matches if term in record.get("title", "").casefold())
            ranked.append((score, fetched, record))
        except (ValueError, KeyError, TypeError, OSError):
            continue
    ranked.sort(key=lambda row: (row[0], row[1]), reverse=True)
    items, seen = [], set()
    for _, _, record in ranked:
        if record["url"] in seen:
            continue
        seen.add(record["url"])
        items.extend(citations_from_text(record, query, min(4, limit - len(items))))
        if len(items) >= limit:
            break
    return items


def search_official(data_dir, query, country, limit=5, *, check_cancelled=None, timeout=35):
    check = check_cancelled or (lambda: None)
    deadline = time.monotonic() + timeout
    check()
    scope = {"CN": "site:gov.cn", "ID": "site:go.id", "SG": "site:gov.sg"}.get(country, "")
    search = asyncio.run(
        gateway_tool(
            "web_search",
            {"query": query[:1500] + " 官方法规原文 " + scope, "max_results": 5, "response_length": "short"},
            check,
            timeout=min(15, timeout),
        )
    )
    check()
    items, audit, seen = [], [], set()
    for candidate in search.get("output", {}).get("sources", [])[:10]:
        if not isinstance(candidate, dict):
            continue
        url = official_url(candidate.get("url"), country)
        if not url or url in seen:
            continue
        seen.add(url)
        if len(seen) > 3 or time.monotonic() >= deadline:
            break
        check()
        try:
            record = fetch_official(
                data_dir, url, country, deadline=min(deadline, time.monotonic() + 10), check_cancelled=check
            )
            record["title"] = str(candidate.get("title") or record["title"])[:1000]
            _save(Path(data_dir) / "legal-library" / "web-evidence", record)
            found = citations_from_text(record, query, max(1, limit - len(items)))
            items.extend(found)
            audit.append(
                {"url": url, "status": "fetched", "sha256": record["content_hash"], "citations": len(found)}
            )
        except (ValueError, DownloadError) as error:
            audit.append({"url": url, "status": "unavailable", "error_type": type(error).__name__})
        if len(items) >= limit:
            break
    return {
        "items": items,
        "mode": "official_web",
        "acquisition": audit,
        "warning": None if items else "本次尚未取得能够定位的官方原文，请核对具体法规或提供原文。",
    }
