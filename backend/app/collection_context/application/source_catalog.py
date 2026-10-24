"""Application boundary for reading active official-source entries."""

from collections.abc import Callable

from ..domain.source_registry import validate_source_registry


def active_sources(pool: dict, country: str, validate_url: Callable[[str], tuple[str, str]]) -> list[dict]:
    return validate_source_registry(pool, country, validate_url)
