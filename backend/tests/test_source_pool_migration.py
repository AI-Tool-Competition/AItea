"""Pool metadata upgrades preserve source identity and collection evidence."""

import copy
import hashlib
import importlib
import json
import sys

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
