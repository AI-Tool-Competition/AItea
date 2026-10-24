"""Validation rules for the reviewed, hierarchical official-source registry."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime

SETNEG_ID = "id-setneg-perpu"
SETNEG_URL = "https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=PERPU&length=50&start=0"
SOURCE_ID = re.compile(r"[a-z0-9][a-z0-9-]{1,79}\Z")
CHECKLIST_ITEM = re.compile(r"(?:ID|SG)-[0-9]+(?:\.[0-9]+)*\Z")
RELATION_TYPES = {"complements", "cross_checks", "implements", "publishes_original"}
SERVER_STATUSES = {
    "reachable_content",
    "reachable_empty_catalogue",
    "access_unavailable",
    "http_404_pending_spaced_recheck",
    "http_410_resource_removed",
}
SHA256 = re.compile(r"[a-f0-9]{64}\Z")


def _validate_verification(source: dict, country: str, hosts: list[str], validate_url) -> None:
    verification = source.get("verification")
    if not isinstance(verification, dict):
        raise ValueError("Every v2 source needs a verification record")
    evidence_url = verification.get("official_evidence_url")
    identity = verification.get("official_identity")
    business_use = verification.get("business_use")
    checked_at = verification.get("checked_at")
    checklist_items = verification.get("checklist_items")
    server_status = verification.get("server_status")
    active = source["status"] == "active"
    if (
        not isinstance(evidence_url, str)
        or not evidence_url.startswith("https://")
        or not isinstance(checked_at, str)
        or server_status not in SERVER_STATUSES
    ):
        raise ValueError("Source verification must identify its official evidence and business scope")
    if active and (
        not isinstance(identity, str)
        or not 10 <= len(identity) <= 500
        or not isinstance(business_use, str)
        or not 10 <= len(business_use) <= 500
        or not isinstance(checklist_items, list)
        or len(checklist_items) > 40
        or any(not isinstance(item, str) or not CHECKLIST_ITEM.fullmatch(item) for item in checklist_items)
    ):
        raise ValueError("Every active source needs an official identity, business use and checklist scope")
    if not isinstance(checklist_items, list):
        checklist_items = []
    try:
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Source verification timestamp must be ISO 8601") from error
    if checked.tzinfo is None:
        raise ValueError("Source verification timestamp must include a timezone")
    try:
        normalized_evidence, evidence_host = validate_url(evidence_url)
    except (TypeError, ValueError) as error:
        raise ValueError("Official evidence must be a valid public HTTPS page") from error
    if normalized_evidence != evidence_url or not official_host(evidence_host, country):
        raise ValueError("Official evidence must use an official host in the source country")

    if active and server_status not in {"reachable_content", "reachable_empty_catalogue"}:
        raise ValueError("An active source must have a successful bounded content probe")
    if server_status == "reachable_empty_catalogue":
        if source.get("empty_catalogue") is not True or source["interval_seconds"] < 7 * 86400:
            raise ValueError("Empty catalogues must be explicitly marked and checked at most weekly")
    elif source.get("empty_catalogue"):
        raise ValueError("Only a verified empty catalogue may carry empty_catalogue")

    if active:
        receipt_path = verification.get("server_receipt_path")
        receipt_hash = verification.get("server_receipt_sha256")
        body_hash = verification.get("body_sha256")
        if (
            not isinstance(receipt_path, str)
            or not receipt_path.startswith(".dev/")
            or ".." in receipt_path.split("/")
            or not isinstance(receipt_hash, str)
            or not SHA256.fullmatch(receipt_hash)
            or not isinstance(body_hash, str)
            or not SHA256.fullmatch(body_hash)
        ):
            raise ValueError("An active source needs a bounded server receipt and body checksum")


def validate_source_registry(
    pool: dict, country: str, validate_url: Callable[[str], tuple[str, str]]
) -> list[dict]:
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
            or any(
                not isinstance(word, str) or not 1 <= len(word) <= 100
                for word in source["discovery_keywords"]
            )
        ):
            raise ValueError("Invalid official source scope or bounds")
        if protocol == "aitea-official-source-pool-v2":
            if source.get("status") not in {"active", "archived"} or source.get("transport") != "proxy":
                raise ValueError("Every v2 source needs an explicit status and proxy transport")
            if not isinstance(source.get("group_id"), str) or not SOURCE_ID.fullmatch(source["group_id"]):
                raise ValueError("Every v2 source needs one stable primary group")
            _validate_verification(source, scope, hosts, validate_url)

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
                or any(
                    not isinstance(item, str) or not CHECKLIST_ITEM.fullmatch(item)
                    for item in group["checklist_items"]
                )
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
                or any(
                    not isinstance(item, str)
                    or not CHECKLIST_ITEM.fullmatch(item)
                    or item not in related_checklists
                    for item in items
                )
            ):
                raise ValueError("Invalid or duplicate source relationship")
            relation_keys.add(key)

    return [
        source
        for source in sources
        if source["country"] == country
        and (protocol == "aitea-official-source-pool-v1" or source["status"] == "active")
    ]


def official_host(host: str, country: str) -> bool:
    suffix = ".go.id" if country == "ID" else ".gov.sg"
    return isinstance(host, str) and host.endswith(suffix)
