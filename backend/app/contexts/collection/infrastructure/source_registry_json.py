"""Bounded source configuration and immutable, content-addressed revisions."""

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from app.contexts.collection.application.source_catalog import RegistryRevision

MAX_REGISTRY_BYTES = 1024 * 1024


def _read_revision(path: Path) -> RegistryRevision:
    if path.is_symlink() or not path.is_file():
        raise ValueError("invalid bounded source registry file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_REGISTRY_BYTES + 1)
    if len(raw) > MAX_REGISTRY_BYTES:
        raise ValueError("source registry exceeds its size limit")
    try:
        value = json.loads(raw)
    except RecursionError as error:
        raise ValueError("source registry nesting exceeds its limit") from error
    if not isinstance(value, dict):
        raise ValueError("source registry root must be an object")
    return RegistryRevision(hashlib.sha256(raw).hexdigest(), value, raw)


def read_source_registry(path: str | Path) -> dict:
    return _read_revision(Path(path)).payload


class JsonSourceRegistry:
    def __init__(self, path, history):
        self.path, self.history = Path(path), Path(history)

    def read(self):
        return _read_revision(self.path)

    def restore(self, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise ValueError("Invalid source registry revision")
        revision = _read_revision(self.history / f"{digest}.json")
        if revision.digest != digest:
            raise ValueError("Source registry history integrity check failed")
        return revision

    def preserve(self, revision):
        self.history.mkdir(parents=True, exist_ok=True, mode=0o700)
        destination = self.history / f"{revision.digest}.json"
        if destination.exists():
            self.restore(revision.digest)
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.history, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(revision.raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            directory = os.open(self.history, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
