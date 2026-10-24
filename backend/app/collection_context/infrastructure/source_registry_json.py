"""Bounded JSON adapter for the reviewed collection registry."""

import json
from pathlib import Path

MAX_REGISTRY_BYTES = 1024 * 1024


def read_source_registry(path: str | Path) -> dict:
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_REGISTRY_BYTES:
        raise ValueError("invalid bounded source registry file")
    value = json.loads(source.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("source registry root must be an object")
    return value
