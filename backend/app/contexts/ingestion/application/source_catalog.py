"""Application boundary for reading active official-source entries."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from app.contexts.ingestion.domain.source_registry import (
    reviewed_discovery_scope_change,
    validate_source_registry,
)


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
        if self.current.payload.get("protocol") == "aitea-official-source-pool-v2":
            if revision.payload.get("protocol") != "aitea-official-source-pool-v2":
                raise ValueError("A reviewed source pool cannot be downgraded")
            previous = {row["id"]: row for row in self.current.payload["sources"]}
            updated = {row["id"]: row for row in revision.payload["sources"]}
            if not previous.keys() <= updated.keys():
                raise ValueError("Source IDs are historical records; archive instead of deleting")
            protected = (
                "country",
                "url",
                "allowed_hosts",
                "kind",
                "topics",
                "discovery_keywords",
                "html_document_paths",
                "html_asset_hosts",
                "setneg_type",
                "discovery",
            )
            for source_id, before in previous.items():
                after = updated[source_id]
                prior_checklists = set(before.get("verification", {}).get("checklist_items", []))
                updated_checklists = set(after.get("verification", {}).get("checklist_items", []))
                reactivated_mapping = (
                    before.get("status") == "archived"
                    and after.get("status") == "active"
                    and prior_checklists <= updated_checklists
                )
                checklist_scope_changed = before.get("verification", {}).get(
                    "checklist_items", []
                ) != after.get("verification", {}).get("checklist_items", [])
                exclusion_change = reviewed_discovery_scope_change(before, after)
                exclusions_changed = before.get("discovery_exclude_keywords", []) != after.get(
                    "discovery_exclude_keywords", []
                )
                if any(before.get(key) != after.get(key) for key in protected) or (
                    exclusions_changed and exclusion_change is None
                ) or (
                    checklist_scope_changed and not reactivated_mapping
                ):
                    raise ValueError("A source's reviewed network or document scope cannot change in place")
        for source in revision.payload["sources"]:
            if source["id"] in identities and identities[source["id"]] != source["country"]:
                raise ValueError("A source ID cannot be rebound to another country")
        return revision

    def accept(self, revision):
        self.repository.preserve(revision)
        self.current = revision

    def sources(self, country):
        return active_sources(self.current.payload, country, self.validate_url)
