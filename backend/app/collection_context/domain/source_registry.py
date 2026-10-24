"""Validation rules for the reviewed, hierarchical official-source registry."""

from __future__ import annotations

import re
from collections.abc import Callable

SETNEG_ID = "id-setneg-perpu"
SETNEG_URL = "https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=PERPU&length=50&start=0"
SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,79}\Z")
CHECKLIST_ITEM = re.compile(r"(?:ID|SG)-[0-9]+(?:\.[0-9]+)*\Z")
RELATION_TYPES = {"complements", "cross_checks", "implements", "publishes_original"}


def validate_source_registry(pool: dict, country: str, validate_url: Callable[[str], tuple[str, str]]) -> list[dict]:
    """Validate registry invariants and return active sources for one country.

    ``validate_url`` is the application adapter for the shared public-HTTPS
    policy. This keeps the domain independent from HTTP and ingestion modules.
    """
    protocol = pool.get("protocol")
    if protocol not in {"aitea-official-source-pool-v1", "aitea-official-source-pool-v2"} or country not in {
        "ID",
        "SG",
    }:
        raise ValueError("Unsupported official source pool/country")
    if protocol == "aitea-official-source-pool-v2" and pool.get("schema_version") != 2:
        raise ValueError("Unsupported official source pool schema version")
    sources = pool["sources"]
    if not isinstance(sources, list) or not 1 <= len(sources) <= 64:
        raise ValueError("Official source pool must contain 1–64 bounded sources")

    ids: set[str] = set()
    source_rows = {}
    for source in sources:
        identifier = source["id"]
        if not isinstance(identifier, str) or not SOURCE_ID.fullmatch(identifier) or identifier in ids:
            raise ValueError("Source IDs must be unique stable identifiers")
        ids.add(identifier)
        source_rows[identifier] = source
        scope = source["country"]
        hosts = source["allowed_hosts"]
        normalized, host = validate_url(source["url"])
        if (identifier == SETNEG_ID or host == "jdih.setneg.go.id") and (
            identifier != SETNEG_ID
            or scope != "ID"
            or hosts != ["jdih.setneg.go.id"]
            or normalized != SETNEG_URL
            or source["kind"] != "catalogue"
            or source.get("setneg_type", "PERPU") != "PERPU"
        ):
            raise ValueError("Setneg collection must retain the reviewed PERPU scope")
        if (
            scope not in {"ID", "SG"}
            or not isinstance(hosts, list)
            or not 1 <= len(hosts) <= 16
            or any(not official_host(host_name, scope) for host_name in hosts)
            or host not in hosts
            or normalized != source["url"]
            or type(source["priority"]) is not int
            or not 1 <= source["priority"] <= 100
            or type(source["interval_seconds"]) is not int
            or not 3600 <= source["interval_seconds"] <= 30 * 86400
            or source["kind"] not in {"guidance", "legislation", "catalogue"}
            or not source["topics"]
            or not all(isinstance(topic, str) and 1 <= len(topic) <= 100 for topic in source["topics"])
            or not 1 <= len(source["discovery_keywords"]) <= 40
            or any(not isinstance(word, str) or not 1 <= len(word) <= 100 for word in source["discovery_keywords"])
        ):
            raise ValueError("Invalid official source scope or bounds")
        if protocol == "aitea-official-source-pool-v2":
            if source.get("status") not in {"active", "archived"} or source.get("transport") != "proxy":
                raise ValueError("Every v2 source needs an explicit status and proxy transport")
            if not isinstance(source.get("group_id"), str) or not SOURCE_ID.fullmatch(source["group_id"]):
                raise ValueError("Every v2 source needs one stable primary group")

    if protocol == "aitea-official-source-pool-v2":
        groups = pool.get("groups")
        if not isinstance(groups, list) or not 2 <= len(groups) <= 64:
            raise ValueError("V2 source pool needs a bounded hierarchical group list")
        group_rows = {}
        group_sources = set()

        def validate_group(group, parent_id=None, depth=0):
            if not isinstance(group, dict) or depth > 3:
                raise ValueError("Source group hierarchy exceeds its depth limit")
            group_id = group.get("id")
            if (
                not isinstance(group_id, str)
                or not SOURCE_ID.fullmatch(group_id)
                or group_id in group_rows
                or group.get("parent_id") != parent_id
                or group.get("country") not in {"ID", "SG"}
                or not isinstance(group.get("title"), str)
                or not 1 <= len(group["title"]) <= 100
                or not isinstance(group.get("checklist_items"), list)
                or any(not isinstance(item, str) or not CHECKLIST_ITEM.fullmatch(item) for item in group["checklist_items"])
                or not isinstance(group.get("source_ids"), list)
            ):
                raise ValueError("Invalid source group or checklist relationship")
            group_rows[group_id] = group
            if parent_id and group_rows[parent_id]["country"] != group["country"]:
                raise ValueError("Source group cannot cross country boundaries")
            for source_id in group["source_ids"]:
                source = source_rows.get(source_id)
                if (
                    source is None
                    or source["country"] != group["country"]
                    or source.get("group_id") != group_id
                    or source_id in group_sources
                ):
                    raise ValueError("Source groups must map each source exactly once within its country")
                group_sources.add(source_id)
            children = group.get("children", [])
            if not isinstance(children, list):
                raise TypeError("Source group children must be a list")
            for child in children:
                validate_group(child, group_id, depth + 1)

        for group in groups:
            validate_group(group)
        if group_sources != ids:
            raise ValueError("Every configured source must appear in exactly one group")
        for source in sources:
            if source["group_id"] not in group_rows:
                raise ValueError("Source references a missing group")
            checklist = set(source.get("verification", {}).get("checklist_items", []))
            if not checklist.issubset(set(group_rows[source["group_id"]]["checklist_items"])):
                raise ValueError("Source checklist links must be covered by its group")

        relations = pool.get("source_relations")
        if not isinstance(relations, list) or len(relations) > 256:
            raise ValueError("Source relationships must be a bounded list")
        relation_keys = set()
        for relation in relations:
            left, right = relation.get("source_id"), relation.get("related_source_id")
            key = (left, right, relation.get("type"))
            related_checklists = {
                *source_rows.get(left, {}).get("verification", {}).get("checklist_items", []),
                *source_rows.get(right, {}).get("verification", {}).get("checklist_items", []),
            }
            items = relation.get("checklist_items", [])
            if (
                left not in ids
                or right not in ids
                or left == right
                or key in relation_keys
                or relation.get("type") not in RELATION_TYPES
                or not isinstance(relation.get("reason"), str)
                or not 10 <= len(relation["reason"]) <= 500
                or not isinstance(items, list)
                or any(not isinstance(item, str) or not CHECKLIST_ITEM.fullmatch(item) or item not in related_checklists for item in items)
            ):
                raise ValueError("Invalid or duplicate source relationship")
            relation_keys.add(key)

    return [
        source
        for source in sources
        if source["country"] == country and (protocol == "aitea-official-source-pool-v1" or source["status"] == "active")
    ]


def official_host(host: str, country: str) -> bool:
    suffix = ".go.id" if country == "ID" else ".gov.sg"
    return isinstance(host, str) and host.endswith(suffix)
