"""Published official original paths absent from the generic directory recipes.

Pure recipe augmentation only: no requests, state mutation, legal approval or
implied catalog completeness. The caller must provide a trusted checklist item.
HTML responses at these addresses still fail discovery's original-file gate.
"""

import os
from copy import deepcopy

BROWSER_HEADER_PROFILE = "ordinary-browser-v1"
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}


def request_headers_for(host: str) -> dict[str, str]:
    """Operator-only, exact-host browser compatibility; default is no override.

    The static profile never carries cookies, authentication or challenge
    tokens. DNS, public-address validation, TLS and request bounds remain the
    downloader's responsibility and are unaffected by these three headers.
    """
    value = os.getenv("COLLECTION_BROWSER_HEADERS_HOSTS", "")
    if not value:
        return {}
    from app.contexts.ingestion.infrastructure.collection_proxy import _canonical_host, _hosts
    from app.shared.infrastructure.http.download import DownloadError

    try:
        allowed = _hosts(value)
        if any("." not in entry or entry == "localhost" or entry.endswith(".localhost") for entry in allowed):
            raise ValueError
        current = _canonical_host(host)
    except (ValueError, UnicodeError):
        raise DownloadError(
            "COLLECTION_BROWSER_HEADERS_HOSTS 必须是最多 64 个精确公网来源域名，不能包含 URL、通配符、端口或认证信息。"
        ) from None
    return dict(_BROWSER_HEADERS) if current in allowed else {}


_MA_HOST = "putusan3.mahkamahagung.go.id"
_MA_DOCUMENT = r"^/direktori/download_file/[a-z0-9]{16,64}/pdf/[a-z0-9]{16,64}(?:\.html)?(?:\?|$)"
_RAKERNAS_DOCUMENT = (
    r"^/rumusan_rakernas/download_file/[a-z0-9]{16,64}/pdf/[a-z0-9]{16,64}(?:\.html)?(?:\?|$)"
)
_DOCUMENT_PATHS = {
    # Official THR original indexed at /asset/data_puu/2026senaker003.pdf;
    # /asset/ is singular, unlike the generic /assets/ file location.
    "4.4": (
        "jdih.kemnaker.go.id",
        (r"^/asset/data_puu/[^/?]+\.pdf(?:\?|$)",),
    ),
    # The old BI SharePoint site publishes originals inside its regulation
    # branch. FAQ documents remain locating evidence and are not originals.
    "4.6": (
        "www.bi.go.id",
        (
            r"^/id/publikasi/peraturan/Documents/(?!(?:[^/?]*)(?:FAQ|Tanya|Jawab))"
            r"(?:PBI|PADG)[^/?]*\.pdf(?:\?|$)",
        ),
    ),
    # Both cryptographic identifiers and the literal /pdf/ route component
    # appear in published official downloads; the old generic route missed it.
    "2.2": (_MA_HOST, (_RAKERNAS_DOCUMENT,)),
    "3.2": (_MA_HOST, (_MA_DOCUMENT,)),
    "3.3": (_MA_HOST, (_MA_DOCUMENT,)),
    "3.4": (_MA_HOST, (_MA_DOCUMENT,)),
}


def recover_official_recipe(recipe: dict, *, country: str, item_id: str) -> dict:
    """Add only that item’s documented original-file paths on its trusted host.

    Existing directory/detail scope, queries, filters and original paths remain
    intact. This does not add hosts or seeds: a file must still be linked from
    evidence captured through the applicable configured official directory.
    """
    result = deepcopy(recipe)
    addition = _DOCUMENT_PATHS.get(item_id) if country == "ID" else None
    if addition is None:
        return result
    host, patterns = addition
    if host not in result.get("allowed_hosts", []):
        return result
    scoped = result.setdefault("document_patterns_by_host", {})
    current = scoped.setdefault(host, list(result.get("document_patterns", [])))
    current.extend(pattern for pattern in patterns if pattern not in current)
    return result
