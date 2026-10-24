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
    added["verification"]["checklist_items"] = ["ID-1.8"]
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
