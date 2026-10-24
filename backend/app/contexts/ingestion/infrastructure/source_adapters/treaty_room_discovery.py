"""Public TreatyRoom JSON/catalog paths confirmed from its published frontend.

No network, authentication, legal-force inference, or general site traversal.
Every directory row is evidence; only the checklist's priority legal topics
produce automatic original candidates. Other records remain catalog metadata.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

HOST = "api-treaty-room.kemlu.go.id"
BASE = f"https://{HOST}"
CATALOG_PATH = "/publik/perjanjian"
FILTERS = ("judul_perjanjian", "id_negara", "id_organisasi", "jenis_perjanjian", "id_subjek")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_TOTAL = 1_000_000
MAX_ID = 2_147_483_647
_PRIORITIES = {
    "trade_customs": r"\b(?:trade|perdagangan|customs|kepabeanan|tariff|tarif|bea\s+cukai)\b",
    "investment": r"\b(?:investment|investasi|penanaman\s+modal)\b",
    "tax": r"\b(?:tax|taxation|pajak|perpajakan)\b",
    "intellectual_property": r"\b(?:intellectual\s+property|kekayaan\s+intelektual|patent|paten|trade\s*mark|merek)\b",
    "dispute_resolution": r"\b(?:arbitration|arbitrase|dispute\s+(?:settlement|resolution)|penyelesaian\s+sengketa)\b",
    "sanitary_trade": r"\b(?:sanitary|phytosanitary|sanitari|fitosanitari|karantina)\b",
}


def catalog_url(page=1, limit=40, *, filters=None):
    query = {key: "" for key in FILTERS}
    query.update(filters or {})
    query.update(page=str(page), limit=str(limit))
    return BASE + CATALOG_PATH + "?" + urlencode(sorted(query.items()))


def catalog_query(url: str) -> dict:
    parsed = urlsplit(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=10)
    query = dict(pairs)
    if (
        parsed.scheme != "https"
        or parsed.hostname != HOST
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
        or parsed.path != CATALOG_PATH
        or len(pairs) != len(query)
        or set(query) != set(FILTERS) | {"page", "limit"}
        or not all(re.fullmatch(r"[1-9]\d{0,5}", query[key]) for key in ("page", "limit"))
        or not 1 <= int(query["limit"]) <= 40
        or not 1 <= int(query["page"]) <= 20_000
    ):
        raise ValueError("TreatyRoom 仅允许公开目录的有界筛选与分页。")
    if len(query["judul_perjanjian"]) > 200 or re.search(r"[\x00-\x1f\x7f]", query["judul_perjanjian"]):
        raise ValueError("TreatyRoom 标题筛选超过允许范围。")
    for key in FILTERS[1:]:
        if query[key] and (not re.fullmatch(r"[1-9]\d{0,5}", query[key]) or int(query[key]) > 100_000):
            raise ValueError("TreatyRoom 公开筛选标识无效。")
    return query


def _identifier(value):
    return type(value) is int and 0 < value <= MAX_ID


def _filename(value):
    return (
        isinstance(value, str)
        and 0 < len(value) <= 180
        and value.strip() == value
        and value not in {".", ".."}
        and not re.search(r"[/\\%?#\x00-\x1f\x7f]", value)
    )


def original_url(treaty, content, filename):
    if not _identifier(treaty) or not _identifier(content) or not _filename(filename):
        raise ValueError("TreatyRoom 原件标识或文件名无效。")
    return f"{BASE}{CATALOG_PATH}/{treaty}/naskah/{content}/{quote(filename, safe='-._~()')}"


def purpose(url: str) -> str | None:
    try:
        from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _canonical

        canonical, host = _canonical(url)
        if host != HOST or canonical != url:
            return None
        parsed = urlsplit(canonical)
        if parsed.path == CATALOG_PATH:
            catalog_query(canonical)
            return "catalog"
        if parsed.query or parsed.fragment:
            return None
        match = re.fullmatch(r"/publik/perjanjian/([1-9]\d{0,9})/naskah/([1-9]\d{0,9})/([^/]+)", parsed.path)
        if match and original_url(int(match[1]), int(match[2]), unquote(match[3])) == canonical:
            return "document"
    except (TypeError, ValueError):
        pass
    return None


class _Title(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, text):
        if not self.hidden:
            self.parts.append(text)


def title_text(value):
    parser = _Title()
    parser.feed(value)
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def priority_topics(title: str) -> list[str]:
    topics = [key for key, pattern in _PRIORITIES.items() if re.search(pattern, title, re.I)]
    agreement = re.search(
        r"^(?:(?:comprehensive|implementing)\s+)?(?:agreement|perjanjian|persetujuan|protocol|protokol)\b"
        r"|^pelaksanaan\s+(?:persetujuan|perjanjian)\b"
        r"|^(?:comprehensive\s+)?economic\s+partnership\s+agreement\b",
        title,
        re.I,
    )
    partnership = re.search(r"\b(?:economic\s+partnership|kemitraan\s+ekonomi|CEPA|EPA)\b", title, re.I)
    if agreement and partnership and "trade_customs" not in topics:
        topics.append("trade_customs")
    if re.search(r"\bASEAN\b", title, re.I) and re.search(
        r"\b(?:agreement|perjanjian|persetujuan|protocol|protokol|framework|kerangka)\b", title, re.I
    ):
        topics.append("asean_agreement")
    return topics


def inspect_catalog(url: str, content: bytes, mime: str, rules) -> dict:
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _result

    if mime != "application/json" or len(content) > MAX_JSON_BYTES:
        return _result(reason="TreatyRoom 公开目录必须返回不超过 4 MiB 的 JSON。")
    query = catalog_query(url)
    page, limit = int(query["page"]), int(query["limit"])
    try:
        data = json.loads(content)
        rows, total, last, current = data["data"], data["total"], data["last_page"], data["current_page"]
    except (ValueError, UnicodeDecodeError, RecursionError, KeyError, TypeError):
        return _result(reason="TreatyRoom 返回了无效公开目录结构。")
    offset = (page - 1) * limit
    if (
        data.get("success") is not True
        or type(total) is not int
        or not 0 <= total <= MAX_TOTAL
        or type(last) is not int
        or last != max(1, (total + limit - 1) // limit)
        or type(current) is not int
        or current != page
        or page > last
        or not isinstance(rows, list)
        or len(rows) != max(0, min(limit, total - offset))
        or not all(
            isinstance(row, dict)
            and _identifier(row.get("id_perjanjian"))
            and isinstance(row.get("judul"), str)
            and 0 < len(row["judul"]) <= 20_000
            for row in rows
        )
        or len({row["id_perjanjian"] for row in rows}) != len(rows)
    ):
        return _result(reason="TreatyRoom 总数、分页行数或国际协定标识不一致。")
    links, records, warnings, seen = [], [], [], set()
    for row in rows:
        treaty, title = row["id_perjanjian"], title_text(row["judul"])
        topics = priority_topics(title)
        files = row.get("file")
        originals, invalid = [], []
        if not isinstance(files, dict) or not _identifier(files.get("id_konten")):
            warnings.append(f"TreatyRoom 国际协定 {treaty} 未提供可验证的原件标识。")
        else:
            for slot in ("file", "file2", "file3", "file4"):
                name = files.get(slot)
                if name is None or name == "":
                    continue
                try:
                    target = original_url(treaty, files["id_konten"], name)
                except ValueError:
                    invalid.append(slot)
                    continue
                if rules.purpose(target) != "document":
                    invalid.append(slot)
                    continue
                identity = f"treaty-room:{treaty}:{slot}"
                originals.append({"slot": slot, "original_url": target, "identity_key": identity})
                if topics and target not in seen:
                    links.append(
                        {"url": target, "purpose": "document", "title": title[:300], "identity_key": identity}
                    )
                    seen.add(target)
        if invalid:
            warnings.append(
                f"TreatyRoom 国际协定 {treaty} 的原件字段 {','.join(invalid)} 不符合安全路径规则。"
            )
        records.append(
            {
                "identity_key": f"treaty-room:{treaty}",
                "treaty_id": treaty,
                "title": title[:1000],
                "source_metadata": row,
                "originals": originals,
                "invalid_file_slots": invalid,
                "priority_topics": topics,
                "original_policy": "priority_legal_topic" if topics else "catalog_only",
                "missing_verified_fields": [
                    "contracting_parties",
                    "signing_date",
                    "international_effective_date",
                    "indonesia_effective_date",
                    "legal_status",
                    "instrument_type",
                    "language",
                    "legal_force",
                ],
                "legal_review_status": "not_reviewed",
            }
        )
    # Enumerate a bounded window after observing the official last-page count.
    # This fills the existing durable frontier for parallel workers; it avoids
    # one serial discovery run per page without inventing any private endpoint.
    slots = max(0, min(250, rules.max_links - len(links)))
    filters = {key: query[key] for key in FILTERS}
    for next_page in range(page + 1, min(last, page + slots) + 1):
        links.append(
            {
                "url": catalog_url(next_page, limit, filters=filters),
                "purpose": "catalog",
                "title": f"Catalog page {next_page}",
            }
        )
    if len(links) > rules.max_links:
        return _result(reason="TreatyRoom 单页原件超过候选上限，需减小分页。")
    result = _result(matched=True, purpose="catalog", links=links, warnings=warnings)
    result["scope"] = {
        "provider": "treaty-room-public",
        "total": total,
        "offset": offset,
        "page": page,
        "page_size": limit,
        "returned": len(rows),
        "last_page": last,
        "filters": filters,
        "source_length_field": data.get("length"),
        "documents": records,
        "legal_status_inferred": False,
        "exhaustive_content_verified": False,
    }
    return result
