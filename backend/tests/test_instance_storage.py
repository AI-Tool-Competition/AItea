"""Portable instance contracts: credentials, originals, versions and queue state."""

import json
import io
import sqlite3
import subprocess
import sys
from contextlib import closing

import pytest
from PIL import Image
from app.configuration.environment import read_environment
from app.configuration.paths import InstancePaths
from app.contexts.documents.infrastructure.providers.embedding_admission import SharedEmbeddingBudget
from app.shared.infrastructure.operations.data_backup import DataBackupError, verify_backup
from app.shared.infrastructure.operations.instance_storage import (
    _fleet_digest,
    adopt_instance,
    backup_instance,
    require_stopped,
    restore_instance,
)
from test_data_backup import populated as populated, rows


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_paths_and_environment_have_one_deterministic_source(tmp_path, monkeypatch):
    project = tmp_path / "project"
    monkeypatch.chdir(tmp_path)
    paths = InstancePaths.resolve(project, environ={"TEA_DATA_DIR": "instance"})
    assert paths.database == project / "instance/tea.sqlite3"
    assert paths.env_file == project / "instance/config/application.env"
    from app.contexts.documents.infrastructure.recovery.page_checkpoint_recovery import isolated_directory

    monkeypatch.setenv("TEA_DATA_DIR", str(paths.data))
    with pytest.raises(ValueError, match="active_instance_refused"):
        isolated_directory(paths.data)
    write(paths.env_file, "LLM_API_KEY='literal-${SECRET}-key'\nLLM_MODEL=file-model\n")
    values = read_environment(project, environ={"TEA_DATA_DIR": "instance", "LLM_MODEL": "process-model"})
    assert values["LLM_API_KEY"] == "literal-${SECRET}-key"
    assert values["LLM_MODEL"] == "process-model"
    assert "literal-" not in json.dumps(paths.inventory())
    write(project / ".env", "LLM_MODEL=legacy\n")
    with pytest.raises(ValueError, match="同时存在"):
        InstancePaths.resolve(project, environ={"TEA_DATA_DIR": "instance"})
    selected = read_environment(project, environ={"TEA_ENV_FILE": str(paths.env_file)})
    assert selected["LLM_MODEL"] == "file-model"
    with pytest.raises(ValueError, match="不存在"):
        read_environment(project, environ={"TEA_ENV_FILE": "missing.env"})


def test_adoption_is_idempotent_and_preserves_legacy_credentials_and_data(tmp_path):
    paths = InstancePaths.resolve(tmp_path, environ={})
    write(tmp_path / ".env", "MINERU_API_TOKEN=fixture-only\n")
    write(paths.ragflow_runtime / ".env", "MYSQL_PASSWORD=unchanged\n")
    write(paths.ragflow_runtime / "conf/private.pem", "existing-private-key")
    write(paths.ragflow_runtime / "web.log", "diagnostic")
    legacy_state = tmp_path / ".dev/collection-service/state"
    write(legacy_state / "state.json", '{"tasks":{"id":{"status":"paused","next_at":123}}}')
    write(tmp_path / ".dev/collection-watchdog/fleet.json", json.dumps({"state_dir": str(legacy_state)}))
    write(tmp_path / ".dev/embedding-admission.sqlite3", "existing-quota-state")
    write(paths.database, "do-not-initialize-or-replace")
    before = paths.database.stat().st_ino
    assert adopt_instance(paths)["moved"] == 6
    assert adopt_instance(InstancePaths.resolve(tmp_path, environ={}))["moved"] == 0
    assert paths.database.stat().st_ino == before
    assert (paths.private_config / "application.env").read_text() == "MINERU_API_TOKEN=fixture-only\n"
    assert (paths.ragflow_config / "conf/private.pem").read_text() == "existing-private-key"
    assert paths.embedding_admission.read_text() == "existing-quota-state"
    assert (paths.ragflow_runtime / "web.log").read_text() == "diagnostic"
    assert not (tmp_path / ".env").exists()
    assert (
        json.loads((paths.collection_state / "state.json").read_text())["tasks"]["id"]["status"] == "paused"
    )
    assert json.loads((paths.collection_registry / "fleet.json").read_text())["state_dir"] == str(
        paths.collection_state
    )


def test_adoption_conflict_does_not_move_any_source(tmp_path):
    paths = InstancePaths.resolve(tmp_path, environ={})
    write(tmp_path / ".env", "original")
    write(paths.ragflow_runtime / "conf/key", "original-key")
    write(paths.ragflow_config / "conf/key", "different-key")
    with pytest.raises(DataBackupError, match="conflict"):
        adopt_instance(paths)
    assert (tmp_path / ".env").read_text() == "original"
    assert (paths.ragflow_runtime / "conf/key").read_text() == "original-key"


def test_open_instance_files_block_offline_operations(tmp_path):
    paths = InstancePaths.resolve(tmp_path, environ={})
    write(paths.database, "database-placeholder")
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys,time; f=open(sys.argv[1]); print('ready',flush=True); time.sleep(60)",
            str(paths.database),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(DataBackupError, match="files_are_open"):
            require_stopped(paths)
    finally:
        process.terminate()
        process.wait(timeout=5)
    require_stopped(paths)


def instance(populated, tmp_path):
    service, documents, proof = populated
    # MinerU chart assets retain their original JPEG bytes, unlike historical
    # page OCR which always wrote PNG. Both must survive a complete backup.
    image = io.BytesIO()
    Image.new("RGB", (4, 4), "white").save(image, format="JPEG")
    blob = service.archive.blobs.put_bytes("alice", image.getvalue())
    with service.db.transaction() as conn:
        identifier = documents["alice"]["id"]
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (identifier,)).fetchone()[0]
        )
        manifest["mineru_chart_assets"] = [
            {
                "image_blob": {
                    "content_hash": blob.digest,
                    "size": blob.size,
                    "mime_type": "image/jpeg",
                    "storage_path": str(blob.path.relative_to(service.db.data_dir)),
                }
            }
        ]
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), identifier))
    data = service.db.data_dir
    paths = InstancePaths(tmp_path, data, tmp_path / "config", data / "config/application.env")
    write(paths.env_file, "RAGFLOW_API_KEY='fixture-only-$literal'\n")
    write(paths.ragflow_config / ".env", "MYSQL_PASSWORD='existing-fixture-password'\n")
    write(paths.ragflow_config / "conf/private.pem", "keep-ragflow-key")
    write(paths.catalogs / "collection-sources/pool.json", '{"version":3}\n')
    write(paths.catalogs / "workflows/sample.json", '{"id":"preserve-revision"}\n')
    root = data / "official-updates/ID"
    write(
        root / "state.json",
        json.dumps(
            {
                "binding": {"root": str(root), "pool_sha256": "fixed"},
                "sources": {"source": {"attempt_count": 9, "next_at": 42}},
            }
        ),
    )
    write(root / "requests.jsonl", '{"immutable_receipt":true}\n')
    fleet = {
        "protocol": "aitea-collection-fleet-v1",
        "project_root": str(paths.project),
        "tasks": [
            {
                "id": "official-id",
                "enabled": False,
                "restart_seconds": 30,
                "max_restart_seconds": 900,
                "cwd": str(paths.project),
                "command": [
                    str(paths.project / "backend/.venv/bin/python"),
                    str(paths.project / "scripts/unattended_collection.py"),
                    "--root",
                    str(root),
                ],
            }
        ],
    }
    fleet["config_hash"] = _fleet_digest(fleet)
    write(paths.collection_fleet, json.dumps(fleet))
    write(
        paths.collection_state / "state.json",
        json.dumps(
            {
                "config_hash": fleet["config_hash"],
                "project_root": str(paths.project),
                "tasks": {
                    "official-id": {
                        "status": "paused",
                        "failures": 5,
                        "next_at": 12345,
                        "identity": None,
                        "launch_token": "old-host-token",
                    }
                },
            }
        ),
    )
    write(paths.ragflow_data / "mysql/ibdata1", "cold-mysql-state")
    (paths.ragflow_data / "mysql/mysql.sock").symlink_to("/tmp/not-a-backup-input.sock")
    budget = SharedEmbeddingBudget(paths.embedding_admission, "fixture-provider", 2)
    with budget.transaction() as conn:
        conn.execute("UPDATE budgets SET cooldown_until=4102444800,bulk_grants=17")
        conn.execute(
            "INSERT INTO leases(token,scope,lane,status,pid,process_start,expires_at,created_at) "
            "VALUES('old-process','fixture-provider','bulk','active',1,'old-start',4102444800,1)"
        )
    return paths, documents, proof


def test_complete_instance_roundtrip_relocates_active_paths_and_keeps_history(populated, tmp_path):
    paths, documents, proof = instance(populated, tmp_path)
    backup = tmp_path / "backup"
    backup_instance(paths, backup)
    verify_backup(backup)
    destination = tmp_path / "new-checkout/data"
    destination.parent.mkdir()
    result = restore_instance(backup, destination, project=destination.parent)
    assert result["configuration_included"] and result["collector_states_rebound"] == 1
    assert result["embedding_leases_released"] == 1
    restored = InstancePaths.resolve(destination.parent, environ={})
    assert restored.catalogs == destination / "config/catalogs"
    assert restored.env_file.read_bytes() == paths.env_file.read_bytes()
    assert restored.env_file.stat().st_mode & 0o777 == 0o600
    assert (restored.ragflow_config / "conf/private.pem").read_text() == "keep-ragflow-key"
    assert (restored.ragflow_data / "mysql/ibdata1").read_text() == "cold-mysql-state"
    assert rows(paths.embedding_admission, "budgets") == rows(restored.embedding_admission, "budgets")
    assert len(rows(paths.embedding_admission, "leases")) == 1
    assert rows(restored.embedding_admission, "leases") == []
    assert not (restored.ragflow_data / "mysql/mysql.sock").is_symlink()
    fleet = json.loads(restored.collection_fleet.read_text())
    assert fleet["project_root"] == str(destination.parent)
    assert fleet["tasks"][0]["enabled"] is False
    assert fleet["tasks"][0]["command"][-1] == str(destination / "official-updates/ID")
    supervisor = json.loads((restored.collection_state / "state.json").read_text())
    assert supervisor["config_hash"] == fleet["config_hash"]
    assert supervisor["tasks"]["official-id"] == {
        "status": "paused",
        "failures": 5,
        "next_at": 12345,
        "identity": None,
    }
    state = json.loads((destination / "official-updates/ID/state.json").read_text())
    assert state["binding"]["root"] == str(destination / "official-updates/ID")
    assert state["sources"]["source"]["attempt_count"] == 9
    assert (destination / "official-updates/ID/requests.jsonl").read_bytes() == (
        paths.data / "official-updates/ID/requests.jsonl"
    ).read_bytes()
    assert rows(paths.database, "users") == rows(restored.database, "users")
    with closing(sqlite3.connect(restored.database)) as conn:
        stored = conn.execute(
            "SELECT storage_path FROM documents WHERE id=?", (documents["alice"]["id"],)
        ).fetchone()[0]
        assert stored.startswith(str(destination))
    assert (destination / proof.path.relative_to(paths.data)).read_bytes() == proof.path.read_bytes()
    backup_instance(restored, tmp_path / "second-backup")
    with pytest.raises(DataBackupError):
        restore_instance(backup, destination, project=destination.parent)


def test_corrupt_backup_and_unbound_collector_never_publish_partial_restore(populated, tmp_path):
    paths, _, _ = instance(populated, tmp_path)
    state = paths.data / "official-updates/ID/state.json"
    state.write_text(json.dumps({"binding": {"root": "/unrelated-evidence"}}))
    backup = tmp_path / "backup"
    backup_instance(paths, backup)
    destination = tmp_path / "restored"
    with pytest.raises(DataBackupError, match="binding_mismatch"):
        restore_instance(backup, destination, project=tmp_path)
    assert not destination.exists()
    (backup / "data/config/application.env").write_text("tampered")
    with pytest.raises(DataBackupError, match="hash_mismatch"):
        restore_instance(backup, destination, project=tmp_path)
    assert not destination.exists()
