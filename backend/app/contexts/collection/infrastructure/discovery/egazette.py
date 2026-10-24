"""Bounded public eGazette discovery; gazette versions are not current SSO text."""

import re
from hashlib import sha256
from urllib.parse import quote, unquote, urlsplit

from app.contexts.collection.infrastructure.discovery.discovery import _flight_json, _FlightScripts
from app.shared.infrastructure.http.public_search import (
    EGAZETTE_APPLICATION,
    EGAZETTE_INDEX,
    EGAZETTE_ORIGIN,
    EGazetteSearchRequest,
)

MAX_SOURCE_BYTES = 4 * 1024 * 1024
SOURCE_ROLE = "gazette_published_or_revised_version_not_current_consolidated"
SEARCH_ATTRIBUTES = (
    "objectID",
    "title",
    "notificationNum",
    "fileUrl",
    "category",
    "subCategory",
    "publishYear",
    "publishTimestamp",
)
LEGISLATIVE_CATEGORIES = (
    "Acts Supplement",
    "Subsidiary Legislation Supplement",
    "Revised Acts",
    "Revised Subsidiary Legislation",
)


def publication_classification(record: dict) -> dict:
    """Classify known standalone reference publications by exact official title.

    A chapter or page labelled "Index" inside an Act is not this publication.
    Unknown titles retain the legislative-source default and draft review gate.
    Existing classified documents require an explicit revision-checked repair.
    """
    reference_titles = {
        "the statutes of the republic of singapore - subject index to acts": (
            ("Revised Acts",),
            "subject-index",
        ),
        "pre-publication notice - re-issue of subject index to acts": (
            ("Revised Acts", "Subsidiary Legislation Supplement"),
            "publication-notice",
        ),
    }
    title = record.get("title")
    match = reference_titles.get(title.casefold()) if isinstance(title, str) else None
    if (
        match
        and record.get("category") == "Legislative Supplements"
        and record.get("subCategory") in match[0]
    ):
        return {
            "level": "secondary",
            "kind": "guidance",
            "document_type": match[1],
            "basis": "official-catalogue-exact-reference-title-v1",
        }
    return {
        "level": "primary",
        "kind": "law",
        "document_type": "legislative-publication",
        "basis": "legislative-catalogue-pending-review",
    }


def public_search_from_html(url: str, content: bytes) -> tuple[EGazetteSearchRequest, dict]:
    """Read complete literal frontend configuration, without evaluating scripts."""
    if url != EGAZETTE_ORIGIN or not 0 < len(content) <= MAX_SOURCE_BYTES:
        raise ValueError("eGazette search configuration must come from its bounded official page")
    collector = _FlightScripts()
    collector.feed(content.decode("utf-8", errors="strict"))
    collector.close()
    pieces, blocks = [], []
    size = 0
    for number, position, body in collector.scripts:
        match = re.fullmatch(r"self\.__next_f\.push\((.*)\)\s*;?", body, re.S)
        if match is None:
            raise ValueError("eGazette frontend uses an unverified nonliteral script")
        value = _flight_json(match[1])
        if value == [0]:
            continue
        if not isinstance(value, list) or len(value) != 2 or value[0] != 1 or not isinstance(value[1], str):
            raise ValueError("eGazette frontend uses an unverified data protocol")
        payload = value[1].encode()
        if size + len(payload) > MAX_SOURCE_BYTES:
            raise ValueError("eGazette frontend data exceeded its bound")
        blocks.append(
            {
                "script_index": number,
                "html_line": position[0],
                "html_column": position[1],
                "literal_script_sha256": sha256(body.encode()).hexdigest(),
                "decoded_start_byte": size,
                "decoded_end_byte": size + len(payload),
            }
        )
        pieces.append(payload)
        size += len(payload)
    stream = b"".join(pieces)
    offset = nodes = records = 0
    found = []
    while offset < len(stream):
        if stream[offset : offset + 1] == b"\n":
            offset += 1
            continue
        start = offset
        header = re.match(rb"([a-f0-9]{0,8}):", stream[offset:])
        records += 1
        if header is None or records > 512:
            raise ValueError("eGazette frontend record is unverified or exceeds its bound")
        offset += header.end()
        if stream[offset : offset + 1] == b"T":
            length = re.match(rb"T([a-f0-9]{1,8}),", stream[offset:])
            if length is None:
                raise ValueError("eGazette frontend text length is invalid")
            offset += length.end() + int(length[1], 16)
            if offset > len(stream):
                raise ValueError("eGazette frontend text is truncated")
            continue
        end = stream.find(b"\n", offset)
        if end < 0 or end - offset > 1024 * 1024:
            raise ValueError("eGazette frontend JSON record is truncated")
        payload = stream[offset:end].decode("utf-8", errors="strict")
        offset = end + 1
        if payload.startswith(("I[", "HL[")):
            _flight_json(payload[1:] if payload.startswith("I[") else payload[2:])
            continue
        if payload in {"X", "C"}:
            continue
        pending = [(_flight_json(payload), "", 0)]
        while pending:
            value, pointer, depth = pending.pop()
            nodes += 1
            if nodes > 50_000 or depth > 64:
                raise ValueError("eGazette frontend object traversal exceeds its bound")
            if isinstance(value, dict):
                if value.get("type") == "egazette-algolia":
                    found.append(
                        (
                            value,
                            {
                                "record_id": header[1].decode(),
                                "json_pointer": pointer,
                                "decoded_record_start_byte": start,
                                "decoded_record_end_byte": end,
                            },
                        )
                    )
                pending.extend(
                    (child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"), depth + 1)
                    for key, child in value.items()
                )
            elif isinstance(value, list):
                pending.extend((child, pointer + f"/{index}", depth + 1) for index, child in enumerate(value))
    if len(found) != 1:
        raise ValueError("eGazette page must expose exactly one public search configuration")
    config, locator = found[0]
    request = EGazetteSearchRequest(
        search_key=config.get("searchApiKey"),
        source_sha256=sha256(content).hexdigest(),
        source_url=url,
        application=config.get("appId"),
        index=config.get("indexName"),
    )
    from app.shared.infrastructure.http.public_search import EGAZETTE_SEARCH_URL

    request.headers_for(EGAZETTE_SEARCH_URL, method="POST")
    return request, {
        "provider": "egazette-public-search-v1",
        "source_url": url,
        "source_page_sha256": request.source_sha256,
        "application": EGAZETTE_APPLICATION,
        "index": EGAZETTE_INDEX,
        "decoded_stream_sha256": sha256(stream).hexdigest(),
        "source_blocks": blocks,
        "config_locator": locator,
        "source_role": SOURCE_ROLE,
        "current_effect_verified": False,
    }


def search_body(*, page=0, page_size=100, query="", filters="") -> dict:
    if (
        type(page) is not int
        or not 0 <= page <= 10_000
        or type(page_size) is not int
        or not 1 <= page_size <= 100
    ):
        raise ValueError("eGazette page bounds are invalid")
    if not isinstance(query, str) or len(query) > 500 or not isinstance(filters, str) or len(filters) > 1000:
        raise ValueError("eGazette query exceeded its bound")
    result = {
        "query": query,
        "page": page,
        "hitsPerPage": page_size,
        "attributesToRetrieve": list(SEARCH_ATTRIBUTES),
        "typoTolerance": False,
        "analytics": False,
        "clickAnalytics": False,
        "attributesToHighlight": [],
        "attributesToSnippet": [],
    }
    if query:
        result["restrictSearchableAttributes"] = ["title"]
    if filters:
        result["filters"] = filters
    return result


def original_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("eGazette original URL is invalid")
    parsed = urlsplit(value)
    path = unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "assets.egazette.gov.sg"
        or parsed.query
        or parsed.fragment
        or "\\" in path
        or "%" in path
        or any(part in {".", ".."} for part in path.split("/"))
        or re.fullmatch(r"/[0-9]{4}/Legislative Supplements/[^/]{1,100}/[^/]{1,200}\.pdf", path, re.I) is None
    ):
        raise ValueError("eGazette original is outside its verified legislative PDF path")
    return "https://assets.egazette.gov.sg" + quote(path, safe="/-_.()")


def inspect_search_response(content: bytes, body: dict) -> dict:
    if not 0 < len(content) <= MAX_SOURCE_BYTES:
        raise ValueError("eGazette search response exceeded its bound")
    value = _flight_json(content.decode("utf-8", errors="strict"))
    if not isinstance(value, dict) or not isinstance(value.get("hits"), list):
        raise ValueError("eGazette search response is not a result page")
    for name in ("page", "nbHits", "nbPages", "hitsPerPage"):
        if type(value.get(name)) is not int or not 0 <= value[name] <= 100_000_000:
            raise ValueError("eGazette search result counters are invalid")
    page, total, pages, size = (value[name] for name in ("page", "nbHits", "nbPages", "hitsPerPage"))
    if (
        page != body["page"]
        or size != body["hitsPerPage"]
        or len(value["hits"]) != min(size, max(0, total - page * size))
    ):
        raise ValueError("eGazette search page or row count disagrees with its query")
    exhaustive = value.get("exhaustiveNbHits", value.get("exhaustive", {}).get("nbHits")) is True
    window_complete = pages * size >= total
    if (total and not 1 <= pages <= (total + size - 1) // size) or (total == 0 and pages not in {0, 1}):
        raise ValueError("eGazette search page count is inconsistent")
    rows, seen = [], set()
    digest = sha256(content).hexdigest()
    for index, hit in enumerate(value["hits"]):
        if not isinstance(hit, dict):
            raise ValueError("eGazette search record is not an object")
        identity = hit.get("objectID")
        if not isinstance(identity, str) or not 0 < len(identity) <= 2048 or identity in seen:
            raise ValueError("eGazette search record has a missing or duplicate identity")
        seen.add(identity)
        row = {key: hit.get(key) for key in SEARCH_ATTRIBUTES}
        # Nonlegislative results remain metadata evidence; never turn their URL
        # into a download until the legislative original policy has validated it.
        row["download_url"] = original_url(row["fileUrl"])
        row["source_locator"] = {
            "response_sha256": digest,
            "json_pointer": f"/hits/{index}",
            "object_id": identity,
        }
        rows.append(row)
    return {
        "provider": "egazette-public-search-v1",
        "response_sha256": digest,
        "page": page,
        "source_index_records": total,
        "source_pages": pages,
        "page_size": size,
        "returned_index_records": len(rows),
        "rows": rows,
        "count_unit": "search_index_record_not_unique_document",
        "exhaustive_source_count": exhaustive,
        "pagination_window_complete": window_complete,
        "next_page": page + 1 if page + 1 < pages else None,
        "source_role": SOURCE_ROLE,
        "current_effect_verified": False,
    }
