"""Country namespaces are explicit identities, never inferred from document tags."""

import re

COUNTRY_ALIASES = {
    "CN": ("China", "中国", "中华人民共和国"),
    "ID": ("Indonesia", "印尼", "印度尼西亚"),
    "SG": ("Singapore", "新加坡"),
}


def country_from_collection(collection: str | None) -> str | None:
    """Reserve country:<ISO2>; ordinary legacy collection names stay compatible."""
    if collection is None:
        return None
    if not isinstance(collection, str):
        from app.shared.domain.errors import fail

        fail(422, "invalid_country_collection", "国别采集组格式无效")
    if not collection.strip().casefold().startswith("country:"):
        return None
    if not re.fullmatch(r"country:[A-Z]{2}", collection):
        from app.shared.domain.errors import fail

        fail(422, "invalid_country_collection", "国别采集组须使用 country: 加两位大写国家代码")
    return collection[8:]


def document_country(conn, document_id: str) -> str | None:
    row = conn.execute(
        "SELECT country FROM document_countries WHERE document_id=?", (document_id,)
    ).fetchone()
    return row["country"] if row else None


def resolve_country(value: str | None) -> str | None:
    """Resolve a configured destination label; unknown labels do not guess a country."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if re.fullmatch(r"[A-Za-z]{2}", value):
        return value.upper()
    if value.casefold().startswith("country:"):
        return country_from_collection(value)
    for country, aliases in COUNTRY_ALIASES.items():
        if value.casefold() in {alias.casefold() for alias in aliases}:
            return country
    return None
