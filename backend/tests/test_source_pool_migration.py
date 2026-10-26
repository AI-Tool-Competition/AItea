"""Pool metadata upgrades preserve source identity and collection evidence."""

import copy
import hashlib
import importlib
import json
import sys
from datetime import UTC, datetime

import pytest
from app.configuration.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT / "scripts"))
migration = importlib.import_module("migrate_unattended_source_pool")


def test_migration_updates_only_binding_and_keeps_source_history(tmp_path):
    old_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    new_pool = copy.deepcopy(old_pool)
    new_pool["protocol"] = "aitea-official-source-pool-v1"
    new_pool.pop("schema_version")
    new_pool.pop("groups")
    new_pool.pop("source_relations")
    for source in new_pool["sources"]:
        source.pop("status")
        source.pop("transport")
        source.pop("group_id")
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    country = "ID"
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    root = tmp_path / "collector"
    root.mkdir()
    original = {
        "binding": {
            "protocol": "aitea-unattended-collection-v1",
            "pool_sha256": old_hash,
            "country": country,
            "root": str(root.resolve()),
            "limits": {"workers": 4},
        },
        "sources": {"id-kemendag-regulations": {"health": "healthy", "attempts": 3}},
        "active_sources": [],
        "status": "waiting",
    }
    (root / "state.json").write_text(json.dumps(original))
    ledger = root / "request-ledger.jsonl"
    ledger.write_text('{"preserve":true}\n')

    result = migration.migrate(old_path, new_path, root, country)
    state = json.loads((root / "state.json").read_text())
    expected = copy.deepcopy(original)
    expected["binding"]["pool_sha256"] = result["new_pool_sha256"]
    assert {key: state[key] for key in original} == expected
    assert state["pool_migrations"][0]["old_pool_sha256"] == old_hash
    assert state["pool_migrations"][0]["source_ids"] == sorted(
        source["id"] for source in old_pool["sources"] if source["country"] == country
    )
    assert ledger.read_text() == '{"preserve":true}\n'
    events = list((root / "events").glob("*/*source-pool-migration.json"))
    assert len(events) == 1


def test_migration_refuses_scope_changes_and_active_requests(tmp_path):
    old_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    new_pool = copy.deepcopy(old_pool)
    new_pool["sources"][0]["allowed_hosts"].append("additional.go.id")
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    root = tmp_path / "collector"
    root.mkdir()
    (root / "state.json").write_text("{}")
    with pytest.raises(ValueError, match="network/document scope"):
        migration.migrate(old_path, new_path, root, "ID")

    new_pool["sources"][0]["allowed_hosts"] = old_pool["sources"][0]["allowed_hosts"]
    bpom = next(row for row in new_pool["sources"] if row["id"] == "id-bpom-regulations")
    bpom["discovery_exclude_keywords"].append("unreviewed subject")
    bpom["verification"]["discovery_scope_change"] = {
        "previous_exclude_keywords": [],
        "current_exclude_keywords": bpom["discovery_exclude_keywords"],
        "reason": "A changed exclusion list without the exact previous mapping.",
    }
    new_path.write_text(json.dumps(new_pool))
    with pytest.raises(ValueError, match="exact scope record"):
        migration.migrate(old_path, new_path, root, "ID")

    bpom["discovery_exclude_keywords"].pop()
    bpom["verification"]["discovery_scope_change"] = copy.deepcopy(
        next(row for row in old_pool["sources"] if row["id"] == "id-bpom-regulations")["verification"][
            "discovery_scope_change"
        ]
    )
    new_pool["groups"][0]["title"] += "（更新）"
    new_path.write_text(json.dumps(new_pool))
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    (root / "state.json").write_text(
        json.dumps(
            {
                "binding": {"pool_sha256": old_hash, "country": "ID", "root": str(root.resolve())},
                "active_sources": ["id-kemendag-regulations"],
            }
        )
    )
    with pytest.raises(ValueError, match="active requests"):
        migration.migrate(old_path, new_path, root, "ID")


def test_catalog_accepts_only_reviewed_discovery_exclusion_update(tmp_path):
    from app.contexts.ingestion.application.source_catalog import SourceCatalog
    from app.contexts.ingestion.infrastructure.source_registry_json import JsonSourceRegistry
    from app.shared.infrastructure.http.download import _validated_url

    original = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    updated = copy.deepcopy(original)
    bpom = next(row for row in updated["sources"] if row["id"] == "id-bpom-regulations")
    previous_terms = list(bpom["discovery_exclude_keywords"])
    bpom["discovery_exclude_keywords"].append("unreviewed subject")
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps(original))
    catalog = SourceCatalog(JsonSourceRegistry(runtime_path, tmp_path / "registry-history"), _validated_url)
    runtime_path.write_text(json.dumps(updated))

    with pytest.raises(ValueError, match="Discovery scope changes"):
        catalog.candidate({})

    bpom["verification"]["discovery_scope_change"] = {
        "previous_exclude_keywords": previous_terms,
        "current_exclude_keywords": bpom["discovery_exclude_keywords"],
        "reason": "A verified source policy update with an exact prior and current scope.",
    }
    runtime_path.write_text(json.dumps(updated))
    assert catalog.candidate({}) is not None


def test_catalog_accepts_only_exactly_reviewed_checklist_remapping(tmp_path):
    from app.contexts.ingestion.application.source_catalog import SourceCatalog
    from app.contexts.ingestion.infrastructure.source_registry_json import JsonSourceRegistry
    from app.shared.infrastructure.http.download import _validated_url

    original = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    updated = copy.deepcopy(original)
    kemendag = next(row for row in updated["sources"] if row["id"] == "id-kemendag-regulations")
    previous_items = list(kemendag["verification"]["checklist_items"])
    kemendag["verification"]["checklist_items"] = ["ID-4.2", "ID-4.3"]
    kemendag["verification"].pop("checklist_scope_change", None)
    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps(original))
    catalog = SourceCatalog(JsonSourceRegistry(runtime_path, tmp_path / "registry-history"), _validated_url)

    runtime_path.write_text(json.dumps(updated))
    with pytest.raises(ValueError, match="reviewed network or document scope"):
        catalog.candidate({})

    kemendag["verification"]["checklist_scope_change"] = {
        "previous_items": ["ID-4.1"],
        "reason": "移除错误分配的交通部法规清单项，保留贸易部实际覆盖的进口规则。",
    }
    runtime_path.write_text(json.dumps(updated))
    with pytest.raises(ValueError, match="reviewed network or document scope"):
        catalog.candidate({})

    kemendag["verification"]["checklist_scope_change"]["previous_items"] = previous_items
    runtime_path.write_text(json.dumps(updated))
    candidate = catalog.candidate({})
    assert candidate is not None
    assert candidate.payload["sources"][0]["id"] == "id-kemendag-regulations"


def test_migration_records_exact_discovery_exclusion_change(tmp_path):
    new_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    old_pool = copy.deepcopy(new_pool)
    for source_id in ("id-bpom-regulations", "sg-customs-import"):
        source = next(row for row in old_pool["sources"] if row["id"] == source_id)
        source.pop("discovery_exclude_keywords")
        source["verification"].pop("discovery_scope_change")
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    root = tmp_path / "collector"
    root.mkdir()
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    (root / "state.json").write_text(
        json.dumps(
            {
                "binding": {"pool_sha256": old_hash, "country": "ID", "root": str(root.resolve())},
                "active_sources": [],
            }
        )
    )

    result = migration.migrate(old_path, new_path, root, "ID")

    changes = result["discovery_scope_changes"]
    assert list(changes) == ["id-bpom-regulations"]
    assert changes["id-bpom-regulations"]["previous_exclude_keywords"] == []
    assert changes["id-bpom-regulations"]["current_exclude_keywords"] == next(
        row for row in new_pool["sources"] if row["id"] == "id-bpom-regulations"
    )["discovery_exclude_keywords"]
    state = json.loads((root / "state.json").read_text())
    assert state["pool_migrations"][-1]["discovery_scope_changes"] == changes


def test_migration_records_explicit_checklist_reclassification(tmp_path):
    new_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    old_pool = copy.deepcopy(new_pool)
    source_id = "id-bpjph-jdih-cari"
    old_source = next(row for row in old_pool["sources"] if row["id"] == source_id)
    old_source["verification"]["checklist_items"] = ["ID-1.8", "ID-4.2"]
    old_source["verification"].pop("checklist_scope_change")
    food_group = next(
        child
        for group in old_pool["groups"]
        for child in group.get("children", [])
        if child["id"] == "id-food-regulatory"
    )
    food_group["checklist_items"] = ["ID-1.8", "ID-4.2", "ID-4.8"]

    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    root = tmp_path / "collector"
    root.mkdir()
    (root / "state.json").write_text(
        json.dumps(
            {
                "binding": {"pool_sha256": old_hash, "country": "ID", "root": str(root.resolve())},
                "active_sources": [],
                "sources": {source_id: {"attempts": 4}},
            }
        )
    )

    result = migration.migrate(old_path, new_path, root, "ID")

    change = result["checklist_scope_changes"][source_id]
    assert change["previous_items"] == ["ID-1.8", "ID-4.2"]
    assert change["checklist_items"] == ["ID-4.8"]
    state = json.loads((root / "state.json").read_text())
    assert state["sources"][source_id]["attempts"] == 4
    assert state["pool_migrations"][0]["checklist_scope_changes"] == result[
        "checklist_scope_changes"
    ]


def test_migration_refuses_unreviewed_html_document_path_changes(tmp_path):
    old_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    new_pool = copy.deepcopy(old_pool)
    ccs = next(row for row in new_pool["sources"] if row["id"] == "sg-ccs-fair-trading")
    ccs["html_document_paths"].append("/consumer-protection/unreviewed/")
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    root = tmp_path / "collector"
    root.mkdir()
    (root / "state.json").write_text("{}")

    with pytest.raises(ValueError, match="network/document scope"):
        migration.migrate(old_path, new_path, root, "SG")


def test_migration_accepts_html_paths_linked_by_verified_cached_source_pages(tmp_path):
    old_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    new_pool = copy.deepcopy(old_pool)
    source = next(row for row in new_pool["sources"] if row["id"] == "sg-ccs-fair-trading")
    parent_url = source["url"]
    new_path = "/consumer-protection/fair-trading-practices/new-guidance/"
    source["html_document_paths"].append(new_path)
    target_url = f"https://www.ccs.gov.sg{new_path}"
    parent_body = f'<html><body><a href="{new_path}">New guidance</a></body></html>'.encode()
    target_body = b"<html><body><h1>New guidance</h1></body></html>"
    parent_hash = hashlib.sha256(parent_body).hexdigest()
    target_hash = hashlib.sha256(target_body).hexdigest()
    root = tmp_path / "collector"
    files = root / "files"
    files.mkdir(parents=True)
    parent_file, target_file = files / "parent.html", files / "detail.html"
    parent_file.write_bytes(parent_body)
    target_file.write_bytes(target_body)
    events = root / "events" / "2026-10-24"
    events.mkdir(parents=True)
    (events / "1-parent.json").write_text(
        json.dumps(
            {
                "kind": "content_version",
                "source_id": source["id"],
                "url": parent_url,
                "content_hash": parent_hash,
                "path": "files/parent.html",
            }
        )
    )
    (events / "2-detail.json").write_text(
        json.dumps(
            {
                "kind": "content_version",
                "source_id": source["id"],
                "url": target_url,
                "content_hash": target_hash,
                "path": "files/detail.html",
            }
        )
    )
    old_path, new_pool_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_pool_path.write_text(json.dumps(new_pool))
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    (root / "state.json").write_text(
        json.dumps(
            {
                "binding": {
                    "pool_sha256": old_hash,
                    "country": "SG",
                    "root": str(root.resolve()),
                },
                "active_sources": [],
                "sources": {
                    source["id"]: {
                        "cache": {
                            parent_url: {
                                "content_hash": parent_hash,
                                "path": "files/parent.html",
                                "mime_type": "text/html",
                            },
                            target_url: {
                                "content_hash": target_hash,
                                "path": "files/detail.html",
                                "mime_type": "text/html",
                            },
                        }
                    }
                },
            }
        )
    )

    result = migration.migrate(old_path, new_pool_path, root, "SG")

    proofs = result["expanded_html_document_paths"][source["id"]]
    assert proofs[0]["url"] == target_url
    assert proofs[0]["parent_url"] == parent_url
    assert proofs[0]["content_sha256"] == target_hash
    assert proofs[0]["parent_sha256"] == parent_hash


def test_migration_accepts_asset_host_only_when_cached_approved_html_references_it(tmp_path):
    old_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    new_pool = copy.deepcopy(old_pool)
    source = next(row for row in new_pool["sources"] if row["id"] == "sg-ccs-fair-trading")
    next(row for row in old_pool["sources"] if row["id"] == source["id"]).pop("html_asset_hosts")
    page_url = f"https://www.ccs.gov.sg{source['html_document_paths'][0]}"
    body = (
        b'<html><body><main><img src="https://isomer-user-content.by.gov.sg/images/example.png">'
        b"</main></body></html>"
    )
    body_hash = hashlib.sha256(body).hexdigest()
    root = tmp_path / "collector"
    cached = root / "files" / "ccs.html"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(body)
    events = root / "events" / "2026-10-24"
    events.mkdir(parents=True)
    (events / "1-content.json").write_text(
        json.dumps(
            {
                "kind": "content_version",
                "source_id": source["id"],
                "url": page_url,
                "content_hash": body_hash,
                "path": "files/ccs.html",
            }
        )
    )
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    root.mkdir(exist_ok=True)
    (root / "state.json").write_text(
        json.dumps(
            {
                "binding": {"pool_sha256": old_hash, "country": "SG", "root": str(root.resolve())},
                "active_sources": [],
                "sources": {
                    source["id"]: {
                        "cache": {
                            page_url: {
                                "content_hash": body_hash,
                                "path": "files/ccs.html",
                                "mime_type": "text/html",
                            }
                        }
                    }
                },
            }
        )
    )

    result = migration.migrate(old_path, new_path, root, "SG")

    proof = result["expanded_html_asset_hosts"][source["id"]][0]
    assert proof["host"] == "isomer-user-content.by.gov.sg"
    assert proof["page_url"] == page_url
    assert proof["page_sha256"] == body_hash
    assert proof["image_path"] == "/images/example.png"


def test_migration_rejects_asset_host_without_cached_official_image_reference(tmp_path):
    old_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    new_pool = copy.deepcopy(old_pool)
    source = next(row for row in new_pool["sources"] if row["id"] == "sg-ccs-fair-trading")
    next(row for row in old_pool["sources"] if row["id"] == source["id"]).pop("html_asset_hosts")
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    root = tmp_path / "collector"
    root.mkdir()
    (root / "state.json").write_text("{}")

    with pytest.raises(ValueError, match="network/document scope"):
        migration.migrate(old_path, new_path, root, "SG")


def test_migration_records_new_unavailable_source_without_activating_it(tmp_path):
    new_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    old_pool = copy.deepcopy(new_pool)
    old_pool["sources"] = [row for row in old_pool["sources"] if row["id"] != "id-bpk-regulations"]
    for group in old_pool["groups"]:
        group["children"] = [child for child in group["children"] if child["id"] != "id-general-law-archived"]
    old_pool["source_relations"] = [
        relation for relation in old_pool["source_relations"] if relation["source_id"] != "id-bpk-regulations"
    ]
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    root = tmp_path / "collector"
    root.mkdir()
    (root / "state.json").write_text(
        json.dumps(
            {
                "binding": {"pool_sha256": old_hash, "country": "ID", "root": str(root.resolve())},
                "active_sources": [],
                "sources": {},
            }
        )
    )

    result = migration.migrate(old_path, new_path, root, "ID")

    assert result["added_source_ids"] == ["id-bpk-regulations"]
    assert result["archived_source_ids"] == []
    assert "id-bpk-regulations" not in {
        row["id"] for row in migration.source_pool(new_path, "ID")
    }
    state = json.loads((root / "state.json").read_text())
    assert state["binding"]["pool_sha256"] == result["new_pool_sha256"]


def test_migration_accepts_receipt_verified_additions_and_archives(tmp_path, monkeypatch):
    old_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    new_pool = copy.deepcopy(old_pool)
    added = copy.deepcopy(next(row for row in new_pool["sources"] if row["id"] == "id-beacukai-regulations"))
    added["id"] = "id-beacukai-proof-test"
    added["title"] = "Verified test addition"
    added["verification"]["checked_at"] = datetime.now(UTC).isoformat()
    added["verification"]["checklist_items"] = ["ID-4.2"]
    body_hash = hashlib.sha256(b"bounded official page").hexdigest()
    receipt = {
        "kind": "source_observation",
        "source_id": added["id"],
        "last_observation": {
            "status": "success",
            "http_status": 200,
            "url": added["url"],
            "content_hash": body_hash,
        },
    }
    receipt_path = tmp_path / ".dev" / "receipt.json"
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps(receipt))
    added["verification"]["body_sha256"] = body_hash
    added["verification"]["server_receipt_path"] = ".dev/receipt.json"
    added["verification"]["server_receipt_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    new_pool["sources"].append(added)
    trade_group = next(
        child
        for group in new_pool["groups"]
        for child in group.get("children", [])
        if child["id"] == "id-trade-origin"
    )
    trade_group["source_ids"].append(added["id"])
    next(row for row in new_pool["sources"] if row["id"] == "id-bkpm-regulations")["status"] = "archived"

    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    root = tmp_path / "collector"
    root.mkdir()
    (root / "state.json").write_text(
        json.dumps(
            {
                "binding": {"pool_sha256": old_hash, "country": "ID", "root": str(root.resolve())},
                "sources": {"id-bkpm-regulations": {"attempts": 4}},
                "active_sources": [],
            }
        )
    )
    monkeypatch.setattr(migration, "ROOT", tmp_path)
    result = migration.migrate(old_path, new_path, root, "ID")
    state = json.loads((root / "state.json").read_text())
    assert result["added_source_ids"] == [added["id"]]
    assert result["archived_source_ids"] == ["id-bkpm-regulations"]
    assert state["sources"]["id-bkpm-regulations"]["attempts"] == 4
    assert state["pool_migrations"][-1]["added_source_ids"] == [added["id"]]
    assert state["pool_migrations"][-1]["archived_source_ids"] == ["id-bkpm-regulations"]

    bad_pool = copy.deepcopy(new_pool)
    next(row for row in bad_pool["sources"] if row["id"] == added["id"])["verification"][
        "body_sha256"
    ] = "0" * 64
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad_pool))
    bad_root = tmp_path / "bad-collector"
    bad_root.mkdir()
    (bad_root / "state.json").write_text(
        json.dumps({"binding": {"pool_sha256": old_hash, "country": "ID", "root": str(bad_root.resolve())}})
    )
    with pytest.raises(ValueError, match="recent HTTP 200 content"):
        migration.migrate(old_path, bad_path, bad_root, "ID")


def test_query_source_probe_proof_uses_exact_root_request_when_health_redacts_query(tmp_path, monkeypatch):
    source = copy.deepcopy(
        next(
            row
            for row in json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())[
                "sources"
            ]
            if row["id"] == "id-beacukai-regulations"
        )
    )
    source["id"] = "id-query-source-proof"
    source["url"] = "https://peraturan.beacukai.go.id/index.html?category=regulation"
    body_hash = hashlib.sha256(b"verified query catalogue").hexdigest()
    source["verification"].update(
        checked_at=datetime.now(UTC).isoformat(),
        body_sha256=body_hash,
        server_receipt_path=".dev/query-receipt.json",
    )
    root_request = {
        "request_id": "a" * 32,
        "source_id": source["id"],
        "url": source["url"],
        "network_url": source["url"],
        "status": "finished",
        "outcome": {
            "status": "success",
            "http_status": 200,
            "url": source["url"],
            "content_hash": body_hash,
        },
        "request_evidence": {"method": "GET"},
    }
    receipt = {
        "kind": "source_observation",
        "source_id": source["id"],
        "last_observation": {
            "status": "success",
            "http_status": 200,
            "url": "https://peraturan.beacukai.go.id/index.html",
            "content_hash": body_hash,
        },
        "root_request": root_request,
    }
    monkeypatch.setattr(migration, "ROOT", tmp_path)
    receipt_path = tmp_path / source["verification"]["server_receipt_path"]
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps(receipt))
    source["verification"]["server_receipt_sha256"] = hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()

    migration._verified_addition(source)

    receipt["root_request"]["network_url"] = "https://peraturan.beacukai.go.id/index.html?category=other"
    receipt_path.write_text(json.dumps(receipt))
    source["verification"]["server_receipt_sha256"] = hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()
    with pytest.raises(ValueError, match="recent HTTP 200 content"):
        migration._verified_addition(source)


def test_migration_accepts_collector_state_receipt_with_exact_root_request(tmp_path, monkeypatch):
    source = copy.deepcopy(
        next(
            row
            for row in json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())[
                "sources"
            ]
            if row["id"] == "id-beacukai-regulations"
        )
    )
    source["id"] = "id-collector-state-proof"
    source["url"] = "https://peraturan.beacukai.go.id/index.html?category=regulation"
    body_hash = hashlib.sha256(b"verified query catalogue").hexdigest()
    source["verification"].update(
        checked_at=datetime.now(UTC).isoformat(),
        body_sha256=body_hash,
        server_receipt_path=".dev/source-observation.json",
    )
    exact_url = source["url"]
    receipt = {
        "kind": "source_observation",
        "source_id": source["id"],
        "last_observation": {
            "protocol": "aitea-source-health-v1",
            "source_id": source["id"],
            "health": "healthy",
            "last_observation": {
                "at": datetime.now(UTC).timestamp(),
                "status": "success",
                "http_status": 200,
                "url": "https://peraturan.beacukai.go.id/index.html",
                "content_hash": body_hash,
            },
        },
        "root_request": {
            "request_id": "b" * 32,
            "source_id": source["id"],
            "url": exact_url,
            "network_url": exact_url,
            "status": "finished",
            "outcome": {
                "status": "success",
                "http_status": 200,
                "url": exact_url,
                "content_hash": body_hash,
            },
            "request_evidence": {"method": "GET"},
        },
    }
    monkeypatch.setattr(migration, "ROOT", tmp_path)
    receipt_path = tmp_path / source["verification"]["server_receipt_path"]
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps(receipt))
    source["verification"]["server_receipt_sha256"] = hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()

    migration._verified_addition(source)


def test_migration_reactivates_archived_source_with_verified_checklist_mapping(tmp_path, monkeypatch):
    new_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    old_pool = copy.deepcopy(new_pool)
    added_ids = {"id-setneg-uu", "id-setneg-pp", "id-setneg-perpres"}
    for candidate in (old_pool, new_pool):
        for row in candidate["sources"]:
            if row["id"] in added_ids:
                row["status"] = "archived"
    old_pool["source_relations"] = [
        row
        for row in old_pool["source_relations"]
        if row["source_id"] not in added_ids and row["related_source_id"] not in added_ids
    ]
    source = next(row for row in new_pool["sources"] if row["id"] == "id-setneg-perpu")
    old_source = next(row for row in old_pool["sources"] if row["id"] == "id-setneg-perpu")
    prior = copy.deepcopy(source["verification"]["prior_probe_and_retirement"])
    old_source["status"] = "archived"
    old_source["verification"] = prior["verification"]
    old_source["retirement"] = prior["retirement"]
    verification = source["verification"]
    verification.update(
        {
            "checked_at": datetime.now(UTC).isoformat(),
            "official_identity": "The Indonesian State Secretariat publishes the official Perppu register.",
            "business_use": "Capture official Perppu records for checklist item ID-1.4.",
            "checklist_items": ["ID-1.4"],
            "server_status": "reachable_content",
            "body_sha256": hashlib.sha256(b"verified Setneg catalogue").hexdigest(),
        }
    )
    group = next(row for row in new_pool["groups"] if row["id"] == "id")
    general_law = next(row for row in group["children"] if row["id"] == "id-general-law")
    general_law["checklist_items"] = ["ID-1.3", "ID-1.4", "ID-1.5", "ID-1.6"]
    body_hash = verification["body_sha256"]
    receipt = {
        "kind": "source_observation",
        "source_id": source["id"],
        "last_observation": {
            "status": "success",
            "http_status": 200,
            "url": source["url"],
            "content_hash": body_hash,
        },
    }
    receipt_path = tmp_path / ".dev" / "setneg-receipt.json"
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps(receipt))
    verification["server_receipt_path"] = ".dev/setneg-receipt.json"
    verification["server_receipt_sha256"] = hashlib.sha256(receipt_path.read_bytes()).hexdigest()

    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    from app.contexts.ingestion.application.source_catalog import SourceCatalog
    from app.contexts.ingestion.infrastructure.source_registry_json import JsonSourceRegistry
    from app.shared.infrastructure.http.download import _validated_url

    runtime_path = tmp_path / "runtime.json"
    runtime_path.write_text(json.dumps(old_pool))
    catalog = SourceCatalog(JsonSourceRegistry(runtime_path, tmp_path / "registry-history"), _validated_url)
    runtime_path.write_text(json.dumps(new_pool))
    candidate = catalog.candidate({})
    assert candidate is not None and candidate.digest != catalog.current.digest
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    root = tmp_path / "collector"
    root.mkdir()
    original = {
        "binding": {"pool_sha256": old_hash, "country": "ID", "root": str(root.resolve())},
        "active_sources": [],
        "sources": {source["id"]: {"attempts": 8, "consecutive_failures": 8}},
    }
    (root / "state.json").write_text(json.dumps(original))
    monkeypatch.setattr(migration, "ROOT", tmp_path)

    result = migration.migrate(old_path, new_path, root, "ID")

    assert result["reactivated_source_ids"] == ["id-setneg-perpu"]
    state = json.loads((root / "state.json").read_text())
    assert state["sources"]["id-setneg-perpu"] == original["sources"]["id-setneg-perpu"]
    assert state["pool_migrations"][0]["reactivated_source_ids"] == ["id-setneg-perpu"]


def test_migration_allows_only_receipt_linked_official_host_expansion(tmp_path, monkeypatch):
    old_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    new_pool = copy.deepcopy(old_pool)
    source = next(row for row in new_pool["sources"] if row["id"] == "id-beacukai-regulations")
    old_source = next(row for row in old_pool["sources"] if row["id"] == source["id"])
    old_source["allowed_hosts"] = ["peraturan.beacukai.go.id"]
    old_source["verification"].pop("host_expansions", None)

    body = (
        b'<html><body><a href="http://repository.beacukai.go.id/referensi/2026/10/'
        b'a5d5a7b56c24-regulation.pdf">official original</a></body></html>'
    )
    body_hash = hashlib.sha256(body).hexdigest()
    source["verification"]["checked_at"] = datetime.now(UTC).isoformat()
    source["verification"]["body_sha256"] = body_hash
    source["verification"]["host_expansions"] = [
        {
            "host": "repository.beacukai.go.id",
            "evidence_url": source["url"],
            "body_sha256": body_hash,
            "path_prefix": "/referensi/",
        }
    ]
    receipt = {
        "kind": "source_observation",
        "source_id": source["id"],
        "last_observation": {
            "status": "success",
            "http_status": 200,
            "url": source["url"],
            "content_hash": body_hash,
        },
    }
    monkeypatch.setattr(migration, "ROOT", tmp_path)
    receipt_path = tmp_path / ".dev" / "receipt.json"
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps(receipt))
    source["verification"]["server_receipt_path"] = ".dev/receipt.json"
    source["verification"]["server_receipt_sha256"] = hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()

    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    old_hash = hashlib.sha256(old_path.read_bytes()).hexdigest()
    new_hash = hashlib.sha256(new_path.read_bytes()).hexdigest()
    root = tmp_path / "collector"
    cache_file = root / "files" / "catalogue.html"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_bytes(body)
    root.mkdir(exist_ok=True)
    event_dir = root / "events" / datetime.now(UTC).strftime("%Y-%m-%d")
    event_dir.mkdir(parents=True)
    event_path = event_dir / f"{int(datetime.now(UTC).timestamp() * 1_000_000_000)}-reload.json"
    event_path.write_text(
        json.dumps(
            {
                "kind": "source_registry_reloaded",
                "previous_sha256": old_hash,
                "pool_sha256": new_hash,
            }
        )
    )
    (root / "state.json").write_text(
        json.dumps(
            {
                "binding": {"pool_sha256": new_hash, "country": "ID", "root": str(root.resolve())},
                "active_sources": [],
                "sources": {
                    source["id"]: {
                        "cache": {
                            source["url"]: {
                                "path": "files/catalogue.html",
                                "content_hash": body_hash,
                            }
                        }
                    }
                },
            }
        )
    )

    result = migration.migrate(old_path, new_path, root, "ID")
    assert result["expanded_source_hosts"] == {
        "id-beacukai-regulations": ["repository.beacukai.go.id"]
    }
    assert result["binding_reconciled"] is True
    migrated_state = json.loads((root / "state.json").read_text())
    assert migrated_state["binding"]["pool_sha256"] == new_hash
    assert migrated_state["pool_migrations"][-1]["binding_reconciled"] is True
    migration_events = list(root.glob("events/*/*source-pool-migration.json"))
    assert len(migration_events) == 1
    assert json.loads(migration_events[0].read_text())["kind"] == "source_pool_binding_migration_reconciled"


def test_migration_rejects_host_expansion_absent_from_verified_source_html(tmp_path, monkeypatch):
    old_pool = json.loads((ROOT / "config/collection-sources/id-sg-unattended.json").read_text())
    new_pool = copy.deepcopy(old_pool)
    source = next(row for row in new_pool["sources"] if row["id"] == "id-beacukai-regulations")
    old_source = next(row for row in old_pool["sources"] if row["id"] == source["id"])
    old_source["allowed_hosts"] = ["peraturan.beacukai.go.id"]
    old_source["verification"].pop("host_expansions", None)
    body = b"<html><body><h1>Official directory without original links</h1></body></html>"
    body_hash = hashlib.sha256(body).hexdigest()
    source["verification"]["checked_at"] = datetime.now(UTC).isoformat()
    source["verification"]["body_sha256"] = body_hash
    source["verification"]["host_expansions"] = [
        {
            "host": "repository.beacukai.go.id",
            "evidence_url": source["url"],
            "body_sha256": body_hash,
            "path_prefix": "/referensi/",
        }
    ]
    receipt = {
        "kind": "source_observation",
        "source_id": source["id"],
        "last_observation": {
            "status": "success",
            "http_status": 200,
            "url": source["url"],
            "content_hash": body_hash,
        },
    }
    monkeypatch.setattr(migration, "ROOT", tmp_path)
    receipt_path = tmp_path / ".dev" / "receipt.json"
    receipt_path.parent.mkdir()
    receipt_path.write_text(json.dumps(receipt))
    source["verification"]["server_receipt_path"] = ".dev/receipt.json"
    source["verification"]["server_receipt_sha256"] = hashlib.sha256(
        receipt_path.read_bytes()
    ).hexdigest()
    old_path, new_path = tmp_path / "old.json", tmp_path / "new.json"
    old_path.write_text(json.dumps(old_pool))
    new_path.write_text(json.dumps(new_pool))
    root = tmp_path / "collector"
    root.mkdir()
    cache_file = root / "files" / "catalogue.html"
    cache_file.parent.mkdir()
    cache_file.write_bytes(body)
    (root / "state.json").write_text(
        json.dumps(
            {
                "binding": {
                    "pool_sha256": hashlib.sha256(old_path.read_bytes()).hexdigest(),
                    "country": "ID",
                    "root": str(root.resolve()),
                },
                "active_sources": [],
                "sources": {
                    source["id"]: {
                        "cache": {
                            source["url"]: {
                                "path": "files/catalogue.html",
                                "content_hash": body_hash,
                            }
                        }
                    }
                },
            }
        )
    )
    with pytest.raises(ValueError, match="network/document scope"):
        migration.migrate(old_path, new_path, root, "ID")
