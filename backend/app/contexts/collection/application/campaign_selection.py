"""Explicit material tiers and evidence-backed associations within one checklist.

The selected uses are independent of legislative rank. Only shared, archived
official source links establish an association; they never establish legal force,
an amendment, ratification, or a citation that has not been checked in the text.
"""

import hashlib
import json
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contexts.collection.infrastructure.discovery.discovery import (
    MAX_HTML_BYTES,
    _canonical,
    _Recipe,
    discover_page,
    recipe_for,
)
from app.shared.domain.values import dump, now


class SelectedMaterial(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(pattern=r"^[a-z0-9-]{1,100}$")
    item_id: str = Field(pattern=r"^\d+\.\d+$")
    url: str = Field(pattern=r"^https://", max_length=2000)
    level: Literal["primary", "secondary"]
    primary_keys: list[str] = Field(default_factory=list, max_length=20)
    evidence_url: str = Field(pattern=r"^https://", max_length=2000)
    locator: str = Field(min_length=1, max_length=1000)


class MaterialSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    source_document_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    materials: list[SelectedMaterial] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def references(self):
        indexed = {entry.key: entry for entry in self.materials}
        if len(indexed) != len(self.materials):
            raise ValueError("Selected material keys must be unique")
        for entry in self.materials:
            if (entry.level == "secondary") != bool(entry.primary_keys):
                raise ValueError("Secondary materials require primary keys; primaries cannot have them")
            if len(set(entry.primary_keys)) != len(entry.primary_keys):
                raise ValueError("Primary keys must be unique")
            if any(key not in indexed or indexed[key].level != "primary" for key in entry.primary_keys):
                raise ValueError("Every secondary must reference declared primary materials")
        return self


def validate_selection(plan, options):
    raw = options.get("selection")
    if raw is None:
        return None
    selection = MaterialSelection.model_validate(raw)
    if (
        selection.plan_id != plan["id"]
        or selection.source_document_sha256 != plan["source_document"]["sha256"]
    ):
        raise ValueError("Selection must match the checklist and its source document")
    definitions = {item["id"]: item for item in plan["items"]}
    counts, seen = {}, set()
    for entry in selection.materials:
        if entry.item_id not in options["items"]:
            raise ValueError("Selected materials must belong to selected checklist items")
        item = definitions[entry.item_id]
        if item["conditional"] and entry.item_id not in options["confirm_items"]:
            raise ValueError("Selected conditional materials require explicit confirmation")
        rules = _Recipe(recipe_for(item))
        original, _ = _canonical(entry.url)
        evidence, _ = _canonical(entry.evidence_url)
        if original != entry.url or evidence != entry.evidence_url:
            raise ValueError("Selected URLs must be canonical HTTPS addresses")
        if rules.purpose(original) != "document" or not rules.purpose(evidence):
            raise ValueError("Selected originals and evidence must stay within the checked-in source recipe")
        if (entry.item_id, original) in seen:
            raise ValueError("One original cannot have conflicting material uses in the same item")
        seen.add((entry.item_id, original))
        counts[entry.item_id] = counts.get(entry.item_id, 0) + 1
        if counts[entry.item_id] > options["originals_per_item"]:
            raise ValueError("Original budget must cover the selected materials in each item")
    if plan["country"] == "SG" and sum(count for key, count in counts.items() if key.startswith("3.")) > 45:
        raise ValueError("Singapore regulatory material selection exceeds the checklist limit of 45")
    return selection


class CampaignSelection:
    def __init__(self, campaign, definition):
        self.campaign, self.definition = campaign, definition
        self.materials = {entry.key: entry for entry in definition.materials}

    def seed(self):
        """Inspect explicitly selected originals through the normal discovery queue."""
        campaign = self.campaign
        with campaign.service.db.transaction() as conn:
            for entry in self.definition.materials:
                item = campaign.definitions[entry.item_id]
                binding, _ = campaign.service.checklists._configure_item(
                    conn, campaign.state["owner_id"], campaign.plan, item
                )
                rules = _Recipe(recipe_for(item))
                for url in (entry.evidence_url, entry.url):
                    conn.execute(
                        "INSERT OR IGNORE INTO checklist_frontier VALUES(?,?,?,?,?,?,?)",
                        (binding["id"], url, rules.purpose(url), 0, "waiting", None, now()),
                    )

    def choose(self, conn, binding, record, remaining):
        selected = []
        level = None
        anchors, waiting = [], []
        for entry in self.definition.materials:
            if entry.item_id != binding["item_id"]:
                continue
            candidate = conn.execute(
                """SELECT c.id,c.status FROM checklist_candidates c WHERE c.binding_id=?
                AND (c.url=? OR c.url IN (SELECT p.final_url FROM checklist_pages p WHERE p.binding_id=? AND p.url=?))
                ORDER BY (c.url=?) DESC,c.created_at DESC LIMIT 1""",
                (binding["id"], entry.url, binding["id"], entry.url, entry.url),
            ).fetchone()
            if (
                not candidate
                or candidate["status"] != "pending"
                or candidate["id"] in record["selected_candidates"]
            ):
                continue
            primary_ids = [self.document(conn, self.materials[key]) for key in entry.primary_keys]
            if any(document is None for document in primary_ids):
                waiting.extend(
                    key for key, document in zip(entry.primary_keys, primary_ids) if document is None
                )
                continue
            if level is not None and level != entry.level:
                continue
            level = entry.level
            anchors.extend(document["id"] for document in primary_ids)
            selected.append(candidate["id"])
            if len(selected) >= remaining:
                break
        return selected, level or "primary", list(dict.fromkeys(anchors)), list(dict.fromkeys(waiting))

    def document(self, conn, entry):
        campaign = self.campaign
        row = conn.execute(
            """SELECT d.id,d.version,d.content_hash FROM documents d
            JOIN checklist_candidates c ON c.document_id=d.id JOIN checklist_bindings b ON b.id=c.binding_id
            WHERE b.owner_id=? AND b.plan_id=? AND b.version=? AND b.item_id=?
            AND (c.url=? OR c.url IN (SELECT p.final_url FROM checklist_pages p WHERE p.binding_id=b.id AND p.url=?))
            ORDER BY (c.url=?) DESC,c.created_at DESC LIMIT 1""",
            (
                campaign.state["owner_id"],
                campaign.plan["id"],
                campaign.plan["version"],
                entry.item_id,
                entry.url,
                entry.url,
                entry.url,
            ),
        ).fetchone()
        return dict(row) if row else None

    def associations(self):
        """Link only stored immutable versions with archived shared-source evidence."""
        campaign, results = self.campaign, []
        owner, collection = campaign.state["owner_id"], f"country:{campaign.plan['country']}"
        with closing(campaign.service.db.connect()) as conn:
            documents = {}
            for entry in self.definition.materials:
                if document := self.document(conn, entry):
                    documents[entry.key] = document
            evidence_cache = {}
            for target in self.definition.materials:
                for key in target.primary_keys:
                    source = self.materials[key]
                    result = {
                        "primary_key": key,
                        "secondary_key": target.key,
                        "relation": "related",
                        "status": "missing_original",
                    }
                    results.append(result)
                    if key not in documents or target.key not in documents:
                        continue
                    cache_key = (target.item_id, target.evidence_url)
                    if cache_key not in evidence_cache:
                        evidence = conn.execute(
                            """SELECT p.final_url,p.storage_path,p.size,p.content_hash FROM checklist_pages p
                            JOIN checklist_bindings b ON b.id=p.binding_id
                            WHERE b.owner_id=? AND b.plan_id=? AND b.version=? AND b.item_id=?
                            AND (p.url=? OR p.final_url=?) ORDER BY p.created_at DESC LIMIT 1""",
                            (
                                owner,
                                campaign.plan["id"],
                                campaign.plan["version"],
                                target.item_id,
                                target.evidence_url,
                                target.evidence_url,
                            ),
                        ).fetchone()
                        links = set()
                        if evidence and evidence["size"] <= MAX_HTML_BYTES:
                            from app.shared.infrastructure.blob_store import _verify_existing

                            owner_hash = hashlib.sha256(owner.encode()).hexdigest()
                            digest = evidence["content_hash"]
                            expected = (
                                campaign.service.archive.blobs.blobs_dir
                                / owner_hash
                                / digest[:2]
                                / digest[2:4]
                                / digest
                            )
                            if Path(evidence["storage_path"]) != expected:
                                raise ValueError("Mapping source is outside the account content store")
                            _verify_existing(expected, digest, evidence["size"])
                            body = Path(evidence["storage_path"]).read_bytes()
                            if hashlib.sha256(body).hexdigest() != evidence["content_hash"]:
                                raise ValueError("Mapping source evidence failed its original hash check")
                            mime = "application/json" if body.lstrip().startswith(b"{") else "text/html"
                            page = discover_page(
                                evidence["final_url"],
                                body,
                                mime,
                                recipe_for(campaign.definitions[target.item_id]),
                            )
                            if page["matched"]:
                                links = {link["url"] for link in page["links"]}
                                if page["purpose"] == "document":
                                    links.add(evidence["final_url"])
                        evidence_cache[cache_key] = (evidence, links)
                    evidence, links = evidence_cache[cache_key]
                    if not {source.url, target.url} <= links:
                        result["status"] = "missing_shared_source_evidence"
                        continue
                    locator = dump(
                        {
                            "basis": "selected_materials_on_official_source",
                            "source_document_sha256": self.definition.source_document_sha256,
                            "url": target.evidence_url,
                            "content_hash": evidence["content_hash"],
                            "locator": target.locator,
                            "primary_version": documents[key]["version"],
                            "primary_hash": documents[key]["content_hash"],
                            "secondary_version": documents[target.key]["version"],
                            "secondary_hash": documents[target.key]["content_hash"],
                        }
                    )
                    relation = campaign.service.archive.add_relation(
                        owner,
                        documents[key]["id"],
                        {
                            "collection": collection,
                            "target_document_id": documents[target.key]["id"],
                            "relation": "related",
                            "locator": locator,
                        },
                    )
                    result.update(
                        status="mapped",
                        relation_id=relation["id"],
                        primary_document_id=documents[key]["id"],
                        secondary_document_id=documents[target.key]["id"],
                        locator=json.loads(locator),
                    )
        return results
