"""Narrow JDIH Kementerian Perhubungan catalogue and original-file rules."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

HOST = "jdih.dephub.go.id"
_TOKEN = re.compile(r"[A-Za-z0-9]{64,512}\Z")
_FORMAL_TITLE = re.compile(
    r"^\s*(?:peraturan\b|pm\s*(?:(?:nomor|no\.?)\s*)?\d+\b|"
    r"perubahan\s+atas\s+(?:peraturan\b|pm\b))",
    re.I,
)
_NON_REGULATION_TITLE = re.compile(
    r"\b(?:rancangan|draft|keputusan|instruksi|surat\s+edaran)\b", re.I
)


def _query(url: str) -> dict[str, str] | None:
    try:
        pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True, strict_parsing=True, max_num_fields=4)
    except ValueError:
        return None
    values = dict(pairs)
    if len(values) != len(pairs):
        return None
    return values


def purpose(url: str) -> str | None:
    """Classify only the verified catalogue, detail, and PDF API routes."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != HOST or parsed.port not in (None, 443):
        return None
    if parsed.path == "/peraturan" and not parsed.query:
        return "catalog"

    query = _query(url)
    if query is None:
        return None
    if parsed.path == "/peraturan/index":
        if (
            set(query) == {"kelompok", "page", "per-page"}
            and query["kelompok"] == "4"
            and query["per-page"] == "12"
            and re.fullmatch(r"[1-9]\d{0,3}", query["page"])
        ):
            return "catalog"
        return None
    if parsed.path == "/peraturan/detail" and set(query) == {"data"} and _TOKEN.fullmatch(query["data"]):
        return "detail"
    if parsed.path == "/api/media" and set(query) == {"data"} and _TOKEN.fullmatch(query["data"]):
        return "document"
    return None


def is_formal_regulation_title(value: str) -> bool:
    """Keep formal Peraturan/PM titles and reject drafts or other instruments."""
    return bool(_FORMAL_TITLE.search(value) and not _NON_REGULATION_TITLE.search(value))
