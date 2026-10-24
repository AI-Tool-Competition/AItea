"""Application boundary for reading active official-source entries."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.contexts.collection.domain.source_registry import validate_source_registry


def active_sources(pool: dict, country: str, validate_url: Callable[[str], tuple[str, str]]) -> list[dict]:
    return validate_source_registry(pool, country, validate_url)


@dataclass(frozen=True)
class RegistryRevision:
    digest: str
    payload: dict
    raw: bytes


class RegistryRepository(Protocol):
    def read(self) -> RegistryRevision: ...
    def restore(self, digest: str) -> RegistryRevision: ...
    def preserve(self, revision: RegistryRevision) -> None: ...


class SourceCatalog:
    """Validate a whole revision before the scheduler changes its active sources."""

    def __init__(self, repository: RegistryRepository, validate_url, *, previous=None):
        self.repository, self.validate_url = repository, validate_url
        try:
            revision = repository.read()
            if previous and revision.digest != previous:
                revision = repository.restore(previous)
        except (OSError, ValueError):
            if not previous:
                raise
            revision = repository.restore(previous)
        self.validate(revision)
        self.current = revision

    def validate(self, revision):
        try:
            for country in ("ID", "SG"):
                active_sources(revision.payload, country, self.validate_url)
        except (KeyError, TypeError, IndexError, AttributeError) as error:
            raise ValueError("Invalid source registry structure") from error

    def candidate(self, identities):
        revision = self.repository.read()
        if revision.digest == self.current.digest:
            return None
        self.validate(revision)
        for source in revision.payload["sources"]:
            if source["id"] in identities and identities[source["id"]] != source["country"]:
                raise ValueError("A source ID cannot be rebound to another country")
        return revision

    def accept(self, revision):
        self.repository.preserve(revision)
        self.current = revision

    def sources(self, country):
        return active_sources(self.current.payload, country, self.validate_url)
