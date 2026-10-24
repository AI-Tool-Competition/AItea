"""Complete SQLite/CAS restore preserves scope and immutable paid checkpoints."""

import hashlib
import json
import sqlite3
import struct
from contextlib import closing
from pathlib import Path

import pytest
from app.bootstrap import Service
from app.contexts.ingestion.application.official_source_import import OfficialSourceImporter
from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID, ensure_principal
from app.shared.domain.errors import AppError
from app.shared.domain.values import dump, now
from app.shared.infrastructure.persistence.database import Database
from app.shared.infrastructure.operations import data_backup as module


def original(service, owner, key):
    body = ("Private tea clause " + key).encode()
    blob = service.archive.blobs.put_bytes(owner, body)
    with service.db.transaction() as conn:
        doc = dict(
            service.archive.store_original(
                conn,
                owner,
                blob,
                key + ".txt",
                "text/plain",
                "https://www.bi.go.id/" + key,
                collection="country:ID",
                level="primary",
                tags=[],
                identity_key=key,
            )
        )
        manifest = {
            "elements": [{"element_key": "e1", "content_text": body.decode()}],
            "chunks": [
                {
                    "id": "c1",
                    "text": body.decode(),
                    "retrieval_text": body.decode(),
                    "locator": {"element_key": "e1"},
                }
            ],
            "pages": [
                {
                    "page": 1,
                    "status": "ocr_complete",
                    "text": body.decode(),
                    "ocr": {"complete": True, "attempts": 2, "uncertain_spans": []},
                }
            ],
        }
        conn.execute("UPDATE documents SET manifest=?,status='ready' WHERE id=?", (dump(manifest), doc["id"]))
        service.index.sync_document(conn, doc["id"])
    return doc


def rows(path, table):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()


@pytest.fixture
def populated(tmp_path):
    service = Service(tmp_path / "source")
    with service.db.transaction() as conn:
        ensure_principal(conn)
        for owner in ("alice", "bob"):
            conn.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?)",
                (owner, owner, owner + "@example.test", "password-hash", 0, now()),
            )
            conn.execute("INSERT INTO sessions VALUES(?,?,?)", (owner + "-session", owner, "2099-01-01"))
            conn.execute(
                "INSERT INTO cases VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    owner + "-case",
                    owner,
                    owner + " business",
                    "Company",
                    "tea",
                    "mode",
                    "{}",
                    3,
                    now(),
                    now(),
                ),
            )
            conn.execute(
                "INSERT INTO facts VALUES(?,?,?,?,?,?,?)",
                (owner + "-case", "fact", "Fact", "private", "manual", 1, now()),
            )
    documents = {owner: original(service, owner, owner) for owner in ("alice", "bob", SYSTEM_OWNER_ID)}
    proof = service.archive.blobs.put_bytes("alice", b'{"protocol":"unchanged-proof-v1","paid":true}')
    with service.db.transaction() as conn:
        doc = documents["alice"]
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
        manifest["proof"] = {
            "registry_path": str(proof.path.relative_to(service.db.data_dir)),
            "registry_sha256": proof.digest,
        }
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
        service.index.sync_document(conn, doc["id"])
        conn.execute("INSERT INTO retrieval_models VALUES(?,?)", ("checkpoint-fingerprint", 2))
        rowid, input_digest = conn.execute(
            "SELECT rowid,input_hash FROM retrieval_chunks WHERE document_id=?", (doc["id"],)
        ).fetchone()
        conn.execute(
            "INSERT INTO retrieval_vectors VALUES(?,?,?,?,?)",
            (rowid, "checkpoint-fingerprint", input_digest, 2, struct.pack("<ff", 1.0, 0.0)),
        )
        conn.execute(
            "UPDATE processing_jobs SET status='processing',phase='parse',attempts=2,available_at=12,lease_token='old-process',lease_until=9999999999,error='retain note' WHERE document_id=?",
            (doc["id"],),
        )
        conn.execute(
            "UPDATE processing_jobs SET status='failed',attempts=3,error='needs review' WHERE document_id=?",
            (documents["bob"]["id"],),
        )
        conn.execute(
            "INSERT INTO collection_network_leases VALUES(?,?,?)", ("old-net", "example.test", 9999999999)
        )
    OfficialSourceImporter(service)
    with service.db.transaction() as conn:
        conn.execute(
            """INSERT INTO official_source_imports(
                event_key,source_id,country,source_url,content_hash,document_id,status,reason,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,'stored',NULL,?,?)""",
            (
                "official-updates/ID/events/2026-10-24/receipt.json",
                "id-bpom-regulations",
                "ID",
                "https://www.bi.go.id/regulation",
                documents[SYSTEM_OWNER_ID]["content_hash"],
                documents[SYSTEM_OWNER_ID]["id"],
                now(),
                now(),
            ),
        )
    return service, documents, proof


def test_complete_backup_restores_private_and_shared_without_reinterpreting_proofs(populated, tmp_path):
    source, docs, proof = populated
    with source.db.transaction() as conn:
        conn.execute(
            """INSERT INTO sources(id,owner_id,name,url,enabled,interval_hours,tags,status,created_at,updated_at)
            VALUES('setneg-source-alice','alice','Setneg test source','https://jdih.setneg.go.id',0,24,'[]','idle',?,?)""",
            (now(), now()),
        )
        conn.execute(
            """INSERT INTO checklist_bindings
            VALUES('setneg-binding-alice','alice','indonesia-20261003',7,'1.3','ID','setneg-source-alice','{}',?)""",
            (now(),),
        )
        conn.execute(
            """INSERT INTO checklist_candidates
            (id,binding_id,url,title,discovered_from,status,created_at)
            VALUES('setneg-candidate-alice','setneg-binding-alice',?,'UU 2/2026',?,'pending',?)""",
            (
                "https://jdih.setneg.go.id/api/hukumproduk/pdf?f=law.pdf",
                "https://jdih.setneg.go.id/api/hukumproduk/detaildata?jns=UU&no=2&thn=2026",
                now(),
            ),
        )
        conn.execute(
            """INSERT INTO checklist_candidate_source_records VALUES(?,?,?,?,?)""",
            (
                "setneg-candidate-alice",
                json.dumps(
                    {
                        "provider": "setneg",
                        "interpretation": "unreviewed_source_values",
                        "fields": {"no_peraturan": "2", "tahun": "2026"},
                        "evidence": {
                            "url": "https://jdih.setneg.go.id/api/hukumproduk/detaildata?jns=UU&no=2&thn=2026",
                            "sha256": "b" * 64,
                        },
                    }
                ),
                "https://jdih.setneg.go.id/api/hukumproduk/detaildata?jns=UU&no=2&thn=2026",
                "b" * 64,
                now(),
            ),
        )
    before = {
        table: rows(source.db.path, table)
        for table in (
            "users",
            "sessions",
            "cases",
            "facts",
            "document_metadata",
            "document_metadata_history",
            "legal_classifications",
            "legal_classification_history",
            "retrieval_vectors",
            "retrieval_chunks",
            "checklist_candidate_source_records",
            "official_source_imports",
            "official_source_import_cursor",
        )
    }
    manifests = {
        row[0]: row[1] for row in sqlite3.connect(source.db.path).execute("SELECT id,manifest FROM documents")
    }
    backup = tmp_path / "backup"
    created = module.create_backup(source.db.data_dir, backup)
    assert created["status"] == "complete"
    assert created["schema_version"] == module.SUPPORTED_SCHEMA_VERSION
    assert module.verify_backup(backup)["status"] == "verified"
    restored = tmp_path / "relocated"
    receipt = module.restore_backup(backup, restored)
    assert receipt["schema_upgraded"] is False
    assert receipt["changes"]["tea.sqlite3"]["released_claims"] == {
        "processing_jobs": 1,
        "collection_items": 0,
        "collection_network_leases": 1,
    }
    for table, expected in before.items():
        assert rows(restored / "tea.sqlite3", table) == expected
        assert rows(source.db.path, table) == expected
    with sqlite3.connect(restored / "tea.sqlite3") as conn:
        assert dict(conn.execute("SELECT id,manifest FROM documents")) == manifests
        job = conn.execute(
            "SELECT status,phase,attempts,available_at,lease_token,lease_until,error FROM processing_jobs WHERE document_id=?",
            (docs["alice"]["id"],),
        ).fetchone()
        assert job == ("queued", "parse", 2, 12, None, None, "retain note")
        assert conn.execute(
            "SELECT status,attempts,error FROM processing_jobs WHERE document_id=?", (docs["bob"]["id"],)
        ).fetchone() == ("failed", 3, "needs review")
        for path, size, digest in conn.execute("SELECT storage_path,size,content_hash FROM documents"):
            assert Path(path).is_relative_to(restored)
            assert len(Path(path).read_bytes()) == size
            assert hashlib.sha256(Path(path).read_bytes()).hexdigest() == digest
    assert (restored / proof.path.relative_to(source.db.data_dir)).read_bytes() == proof.path.read_bytes()
    app = Service(restored)
    with closing(app.db.connect()) as conn:
        assert app.read_document(conn, "bob", docs[SYSTEM_OWNER_ID]["id"])["owner_id"] == SYSTEM_OWNER_ID
        assert app.read_document(conn, "alice", docs["alice"]["id"])["owner_id"] == "alice"
        with pytest.raises(AppError) as denied:
            app.read_document(conn, "bob", docs["alice"]["id"])
        assert denied.value.status == 404
    assert (backup.stat().st_mode & 0o777) == 0o700
    assert all((path.stat().st_mode & 0o077) == 0 for path in backup.rglob("*") if path.is_file())


def test_online_backup_includes_live_committed_WAL_and_does_not_change_source(populated, tmp_path):
    service, _, _ = populated
    keeper = service.db.connect()
    try:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?)",
            ("wal-user", "Committed only in WAL", None, None, 0, now()),
        )
        wal = Path(str(service.db.path) + "-wal")
        assert wal.stat().st_size > 0
        before = module._hash(service.db.path), module._hash(wal)
        result = module.create_backup(service.db.data_dir, tmp_path / "backup")
        assert any(row["path"] == "tea.sqlite3-wal" for row in result["excluded"])
        assert (module._hash(service.db.path), module._hash(wal)) == before
        with sqlite3.connect(tmp_path / "backup/data/tea.sqlite3") as conn:
            assert (
                conn.execute("SELECT name FROM users WHERE id='wal-user'").fetchone()[0]
                == "Committed only in WAL"
            )
    finally:
        keeper.close()


@pytest.mark.parametrize("disappears", ["after_walk", "after_path_check"])
@pytest.mark.parametrize("location", ["root", "campaign"])
def test_online_sidecars_can_disappear_after_snapshot(populated, tmp_path, monkeypatch, disappears, location):
    service, _, _ = populated
    database = service.db if location == "root" else Service(service.db.data_dir / "campaigns/active").db
    keeper = database.connect()
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    keeper.execute(
        "INSERT INTO users VALUES(?,?,?,?,?,?)", ("sidecar-race", "Committed", None, None, 0, now())
    )
    shm = Path(str(database.path) + "-shm")
    assert shm.is_file()
    real_walk, real_safe = module.os.walk, module._safe_absolute
    closed = False

    def disappear():
        nonlocal closed
        if not closed:
            keeper.close()
            closed = True
            assert not shm.exists()

    def racing_walk(root, *args, **kwargs):
        for base, subdirs, names in real_walk(root, *args, **kwargs):
            if Path(base) == database.data_dir and shm.name in names and disappears == "after_walk":
                disappear()
            yield base, subdirs, names

    def racing_safe(path, **kwargs):
        result = real_safe(path, **kwargs)
        if Path(path) == shm and disappears == "after_path_check":
            disappear()
        return result

    monkeypatch.setattr(module.os, "walk", racing_walk)
    monkeypatch.setattr(module, "_safe_absolute", racing_safe)
    try:
        backup = tmp_path / "backup"
        result = module.create_backup(service.db.data_dir, backup)
        assert result["status"] == "complete" and closed
        relative = database.path.relative_to(service.db.data_dir).as_posix()
        assert {row["path"] for row in result["excluded"]} >= {relative + "-shm", relative + "-wal"}
        assert module.verify_backup(backup)["status"] == "verified"
        with sqlite3.connect(backup / "data" / relative) as conn:
            assert conn.execute("SELECT name FROM users WHERE id='sidecar-race'").fetchone()[0] == "Committed"
    finally:
        keeper.close()


@pytest.mark.parametrize("replacement", ["symlink", "directory"])
def test_online_sidecar_replacement_is_not_ignored(populated, tmp_path, monkeypatch, replacement):
    service, _, _ = populated
    keeper = service.db.connect()
    keeper.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("race", "saved", None, None, 0, now()))
    shm = Path(str(service.db.path) + "-shm")
    real_walk = module.os.walk
    changed = False

    def racing_walk(root, *args, **kwargs):
        nonlocal changed
        for base, subdirs, names in real_walk(root, *args, **kwargs):
            if not changed and Path(base) == service.db.data_dir and shm.name in names:
                keeper.close()
                assert not shm.exists()
                if replacement == "symlink":
                    shm.symlink_to(tmp_path / "outside")
                else:
                    shm.mkdir()
                changed = True
            yield base, subdirs, names

    monkeypatch.setattr(module.os, "walk", racing_walk)
    try:
        with pytest.raises(module.DataBackupError, match="symlink_path_refused|regular_file_required"):
            module.create_backup(service.db.data_dir, tmp_path / "backup")
        assert changed and not (tmp_path / "backup").exists()
    finally:
        keeper.close()


@pytest.mark.parametrize("kind", ["cas", "ordinary_wal_name", "frozen_sidecar"])
def test_disappearing_non_online_files_still_refuse_backup(populated, tmp_path, monkeypatch, kind):
    service, docs, _ = populated
    if kind == "cas":
        target = Path(docs["alice"]["storage_path"])
    elif kind == "ordinary_wal_name":
        target = service.db.data_dir / "files/upload.sqlite3-wal"
        target.write_bytes(b"a user's original, not an active sidecar")
    else:
        database = service.db.data_dir / "cold.sqlite3"
        with closing(sqlite3.connect(database)) as conn:
            conn.execute("CREATE TABLE frozen(value TEXT)")
        target = database.with_name(database.name + "-wal")
        target.touch()
    real_walk = module.os.walk
    disappeared = False

    def racing_walk(root, *args, **kwargs):
        nonlocal disappeared
        for base, subdirs, names in real_walk(root, *args, **kwargs):
            if not disappeared and Path(base) == target.parent and target.name in names:
                target.unlink()
                disappeared = True
            yield base, subdirs, names

    monkeypatch.setattr(module.os, "walk", racing_walk)
    with pytest.raises(module.DataBackupError, match="existing_parent_directory_required"):
        module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert disappeared and not (tmp_path / "backup").exists()


@pytest.mark.parametrize("schema", [1, 9, 15])
def test_empty_and_legacy_schema_snapshots_are_preserved_without_upgrade(tmp_path, schema):
    source = tmp_path / "source"
    if schema == 1:
        old_db = Database(source)
        with old_db.transaction() as conn:
            conn.execute("DROP TABLE IF EXISTS user_access")
            conn.execute("DELETE FROM schema_migrations WHERE version>1")
    elif schema == 15:
        old_service = Service(source)
        with old_service.db.transaction() as conn:
            for table in (
                "guide_messages",
                "node_memories",
                "conversation_messages",
                "stage_guide_runs",
                "stage_conversations",
                "user_access",
            ):
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute("DELETE FROM schema_migrations WHERE version>15")
    else:
        # Use the deployed pre-retrieval service migrations, not a fake version
        # number over newer tables; upgrade remains the normal service's job.
        from types import SimpleNamespace

        from app.contexts.ingestion.application.checklists import ChecklistService
        from app.contexts.ingestion.application.collection_jobs import CollectionService
        from app.contexts.documents.application.archive import LegalArchive
        from app.contexts.documents.infrastructure.catalog import LegalCatalog
        from app.contexts.legal_analysis.application.analysis_service import LegalService
        from app.contexts.workflows.application.workflow_studio import AdminStudio

        old = SimpleNamespace(db=Database(source))
        old.catalog = LegalCatalog(old.db)
        old.studio = AdminStudio(old)
        old.archive = LegalArchive(old)
        old.collection = CollectionService(old)
        old.checklists = ChecklistService(old)
        old.legal = LegalService(old)
        with old.db.transaction() as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version>9")
            conn.execute("DROP TABLE collection_transfer_parts")
            conn.execute("DROP TABLE collection_transfers")
            conn.execute("DROP TABLE checklist_page_evidence")
    module.create_backup(source, tmp_path / "backup")
    module.restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert rows(tmp_path / "restored/tea.sqlite3", "schema_migrations") == rows(
        source / "tea.sqlite3", "schema_migrations"
    )
    assert module.verify_backup(tmp_path / "backup")["schema_version"] == schema


@pytest.mark.parametrize("fault", ["missing", "bytes", "manifest", "unlisted", "symlink", "escape"])
def test_tampered_or_incomplete_backup_is_never_restored(populated, tmp_path, fault):
    service, docs, _ = populated
    backup = tmp_path / "backup"
    module.create_backup(service.db.data_dir, backup)
    original = backup / "data" / Path(docs["alice"]["storage_path"]).relative_to(service.db.data_dir)
    if fault == "missing":
        original.unlink()
    elif fault == "bytes":
        original.write_bytes(b"tampered")
    elif fault == "manifest":
        (backup / "manifest.json").write_text("{}")
    elif fault == "unlisted":
        (backup / "data/extra").write_bytes(b"unlisted")
    elif fault == "symlink":
        original.unlink()
        original.symlink_to(docs["alice"]["storage_path"])
    else:
        manifest = json.loads((backup / "manifest.json").read_bytes())
        manifest["files"][0]["path"] = "../outside"
        (backup / "manifest.json").write_text(dump(manifest))
        seal = json.loads((backup / "COMPLETE").read_bytes())
        seal["manifest_sha256"] = module._hash(backup / "manifest.json")[0]
        (backup / "COMPLETE").write_text(dump(seal))
    with pytest.raises((module.DataBackupError, OSError)):
        module.restore_backup(backup, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert not list(tmp_path.glob(".aitea-restore-*"))


def test_source_symlinks_and_cross_owner_originals_are_refused(populated, tmp_path):
    service, docs, _ = populated
    link = service.db.data_dir / "outside"
    link.symlink_to(tmp_path / "other")
    with pytest.raises(module.DataBackupError, match="symlink"):
        module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    link.unlink()
    with service.db.transaction() as conn:
        # Even identical bytes cannot turn Alice's file into Bob's original.
        conn.execute(
            "UPDATE documents SET storage_path=?,content_hash=?,size=? WHERE id=?",
            (
                docs["alice"]["storage_path"],
                docs["alice"]["content_hash"],
                docs["alice"]["size"],
                docs["bob"]["id"],
            ),
        )
    with pytest.raises(module.DataBackupError, match="owner"):
        module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_atomic_failure_and_existing_destination_cannot_publish_partial_data(
    populated, tmp_path, monkeypatch
):
    service, _, _ = populated

    def fail_publish(stage, destination):
        raise OSError("injected before publication")

    monkeypatch.setattr(module, "_publish", fail_publish)
    with pytest.raises(OSError):
        module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    assert not list(tmp_path.glob(".aitea-backup-*"))


def test_atomic_publication_refuses_racing_existing_empty_directory(tmp_path):
    stage, target = tmp_path / "stage", tmp_path / "existing"
    stage.mkdir()
    (stage / "COMPLETE").write_text("complete")
    target.mkdir()
    with pytest.raises(module.DataBackupError, match="new_destination"):
        module._publish(stage, target)
    assert list(target.iterdir()) == []
    assert stage.exists()


def test_backup_destination_inside_source_and_restore_existing_are_refused(populated, tmp_path):
    service, _, _ = populated
    with pytest.raises(module.DataBackupError):
        module.create_backup(service.db.data_dir, service.db.data_dir / "backup")
    module.create_backup(service.db.data_dir, tmp_path / "backup")
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep").write_text("user")
    with pytest.raises(module.DataBackupError):
        module.restore_backup(tmp_path / "backup", existing)
    assert (existing / "keep").read_text() == "user"


def test_raw_SQLite_CAS_and_frozen_receipt_databases_keep_exact_bytes(populated, tmp_path):
    service, _, _ = populated
    frozen = service.db.data_dir / "verification/frozen.sqlite3"
    frozen.parent.mkdir()
    with sqlite3.connect(frozen) as conn:
        conn.execute("CREATE TABLE evidence(value TEXT)")
        conn.execute("INSERT INTO evidence VALUES('receipt bound bytes')")
    body = frozen.read_bytes()
    blob = service.archive.blobs.put_bytes("alice", body)
    expected = module._hash(frozen)
    module.create_backup(service.db.data_dir, tmp_path / "backup")
    module.restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert module._hash(tmp_path / "restored/verification/frozen.sqlite3") == expected
    assert (tmp_path / "restored" / blob.path.relative_to(service.db.data_dir)).read_bytes() == body


def test_frozen_SQLite_with_active_WAL_refuses_silent_evidence_conversion(populated, tmp_path):
    service, _, _ = populated
    frozen = service.db.data_dir / "verification/frozen.sqlite3"
    frozen.parent.mkdir()
    with closing(sqlite3.connect(frozen)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE evidence(value TEXT)")
        conn.commit()
        with pytest.raises(module.DataBackupError, match="frozen_archive_has_WAL"):
            module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_corrupt_root_and_unknown_future_schema_are_refused(tmp_path):
    source = tmp_path / "source"
    Service(source)
    with sqlite3.connect(source / "tea.sqlite3") as conn:
        conn.execute("INSERT INTO schema_migrations VALUES(999,'future')")
    with pytest.raises(module.DataBackupError, match="unsupported_schema"):
        module.create_backup(source, tmp_path / "backup")
    (source / "tea.sqlite3").write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        module.create_backup(source, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_campaign_live_snapshot_relocates_its_own_paths(populated, tmp_path):
    service, _, _ = populated
    campaign = Service(service.db.data_dir / "campaigns/live")
    with campaign.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("alice", "alice", None, None, 0, now()))
    doc = original(campaign, "alice", "campaign")
    module.create_backup(service.db.data_dir, tmp_path / "backup")
    result = module.restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert result["changes"]["campaigns/live/tea.sqlite3"]["relocated_paths"] == 1
    with sqlite3.connect(tmp_path / "restored/campaigns/live/tea.sqlite3") as conn:
        path = Path(conn.execute("SELECT storage_path FROM documents WHERE id=?", (doc["id"],)).fetchone()[0])
        assert path.is_relative_to(tmp_path / "restored/campaigns/live")
        assert path.read_bytes() == Path(doc["storage_path"]).read_bytes()


def test_campaign_external_original_path_is_refused_instead_of_silently_broken(populated, tmp_path):
    service, _, _ = populated
    campaign = Service(service.db.data_dir / "campaigns/live")
    with campaign.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("alice", "alice", None, None, 0, now()))
    doc = original(campaign, "alice", "campaign")
    external = tmp_path / "old-worktree-original.txt"
    external.write_bytes(Path(doc["storage_path"]).read_bytes())
    with campaign.db.transaction() as conn:
        conn.execute("UPDATE documents SET storage_path=? WHERE id=?", (str(external), doc["id"]))
    with pytest.raises(module.DataBackupError, match="external_storage"):
        module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def insert(conn, table, values):
    fields = ",".join(values)
    conn.execute(
        f"INSERT INTO {table}({fields}) VALUES({','.join('?' for _ in values)})", tuple(values.values())
    )


def test_download_parts_page_evidence_and_fetch_claims_survive_relocation(populated, tmp_path):
    service, docs, _ = populated
    raw = Path(docs["alice"]["storage_path"])
    digest, size = module._hash(raw)
    with service.db.transaction() as conn:
        insert(
            conn,
            "sources",
            {
                "id": "source",
                "owner_id": "alice",
                "name": "source",
                "url": "https://www.bi.go.id/x",
                "enabled": 1,
                "interval_hours": 24,
                "tags": "[]",
                "status": "idle",
                "created_at": now(),
                "updated_at": now(),
            },
        )
        insert(
            conn,
            "collection_runs",
            {
                "id": "run",
                "owner_id": "alice",
                "source_id": "source",
                "client_request_id": "request",
                "request_hash": "hash",
                "plan": "{}",
                "status": "running",
                "revision": 1,
                "created_at": now(),
                "updated_at": now(),
            },
        )
        insert(
            conn,
            "collection_items",
            {
                "id": "item",
                "run_id": "run",
                "owner_id": "alice",
                "url": "https://www.bi.go.id/x",
                "host": "www.bi.go.id",
                "level": "primary",
                "status": "fetching",
                "attempts": 2,
                "available_at": 42,
                "lease_token": "old-fetch",
                "lease_until": 9999999999,
                "created_at": now(),
            },
        )
        insert(
            conn,
            "checklist_bindings",
            {
                "id": "binding",
                "owner_id": "alice",
                "plan_id": "plan",
                "version": 1,
                "item_id": "planned",
                "country": "ID",
                "source_id": "source",
                "snapshot": "{}",
                "created_at": now(),
            },
        )
        insert(
            conn,
            "checklist_pages",
            {
                "item_id": "item",
                "binding_id": "binding",
                "url": "https://www.bi.go.id/x",
                "final_url": "https://www.bi.go.id/x",
                "content_hash": digest,
                "storage_path": str(raw),
                "size": size,
                "purpose": "evidence",
                "warnings": "[]",
                "created_at": now(),
            },
        )
        insert(
            conn,
            "collection_transfers",
            {
                "id": "transfer",
                "item_id": "item",
                "owner_id": "alice",
                "url": "https://www.bi.go.id/x",
                "etag": '"etag"',
                "total_size": size,
                "part_size": 65536,
                "filename": "x.txt",
                "mime_type": "text/plain",
                "status": "complete",
                "content_hash": digest,
                "storage_path": str(raw),
                "created_at": now(),
                "updated_at": now(),
            },
        )
        insert(
            conn,
            "collection_transfer_parts",
            {
                "transfer_id": "transfer",
                "start_byte": 0,
                "size": size,
                "content_hash": digest,
                "storage_path": str(raw),
                "created_at": now(),
            },
        )
    module.create_backup(service.db.data_dir, tmp_path / "backup")
    module.restore_backup(tmp_path / "backup", tmp_path / "restored")
    with sqlite3.connect(tmp_path / "restored/tea.sqlite3") as conn:
        for table in ("checklist_pages", "collection_transfers", "collection_transfer_parts"):
            path = Path(conn.execute(f"SELECT storage_path FROM {table}").fetchone()[0])
            assert path.is_relative_to(tmp_path / "restored")
            assert path.read_bytes() == raw.read_bytes()
        assert conn.execute(
            "SELECT status,attempts,available_at,lease_token,lease_until FROM collection_items"
        ).fetchone() == ("queued", 2, 42, None, None)


def test_operational_manifest_external_CAS_and_cross_owner_refs_are_refused(populated, tmp_path):
    service, docs, _ = populated
    with service.db.transaction() as conn:
        manifest = {"image_blob": {"storage_path": docs["alice"]["storage_path"]}}
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), docs["alice"]["id"]))
    with pytest.raises(module.DataBackupError, match="invalid_path"):
        module.create_backup(service.db.data_dir, tmp_path / "backup")
    with service.db.transaction() as conn:
        manifest["image_blob"]["storage_path"] = str(
            Path(docs["bob"]["storage_path"]).relative_to(service.db.data_dir)
        )
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), docs["alice"]["id"]))
    with pytest.raises(module.DataBackupError, match="manifest_owner"):
        module.create_backup(service.db.data_dir, tmp_path / "backup")


def test_failed_restore_does_not_publish_or_mutate_backup(populated, tmp_path, monkeypatch):
    service, _, _ = populated
    module.create_backup(service.db.data_dir, tmp_path / "backup")
    before = {str(path): module._hash(path) for path in (tmp_path / "backup").rglob("*") if path.is_file()}

    def fail(*args):
        raise RuntimeError("injected relocation failure")

    monkeypatch.setattr(module, "_restore_database", fail)
    with pytest.raises(RuntimeError):
        module.restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert not list(tmp_path.glob(".aitea-restore-*"))
    assert {
        str(path): module._hash(path) for path in (tmp_path / "backup").rglob("*") if path.is_file()
    } == before


def test_foreign_key_corruption_refuses_successful_backup(populated, tmp_path):
    service, _, _ = populated
    # A readable SQLite file can still have lost ownership references; a hash
    # alone must not make such a snapshot look restorable.
    with sqlite3.connect(service.db.path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM users WHERE id='alice'")
    with pytest.raises(module.DataBackupError, match="sqlite_foreign_key_failed"):
        module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    assert not list(tmp_path.glob(".aitea-backup-*"))


@pytest.mark.parametrize("fault", ["png_alias", "registry_digest"])
def test_mapping_proof_bindings_are_checked_before_success(populated, tmp_path, fault):
    service, docs, proof = populated
    with service.db.transaction() as conn:
        doc = docs["alice"]
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
        if fault == "png_alias":
            manifest["pages"][0]["ocr"].update(
                {
                    "image_sha256": doc["content_hash"],
                    "image_blob": {
                        "content_hash": doc["content_hash"],
                        "size": doc["size"],
                        "storage_path": str(Path(doc["storage_path"]).relative_to(service.db.data_dir)),
                        "mime_type": "image/png",
                    },
                }
            )
        else:
            manifest["proof"]["registry_sha256"] = "0" * 64
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
    reason = "PNG_signature_mismatch" if fault == "png_alias" else "proof_path_hash_mismatch"
    with pytest.raises(module.DataBackupError, match=reason):
        module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_handoff_files_archive_is_not_misidentified_as_operational_CAS(populated, tmp_path):
    service, _, _ = populated
    owner_hash = hashlib.sha256(b"historical-proof-owner").hexdigest()
    raw = b'{"proof":"historical original bytes"}'
    digest = hashlib.sha256(raw).hexdigest()
    relative = (
        Path("ingestion-archive/source-data/verification/acfta/handoff-files/blobs")
        / owner_hash
        / digest[:2]
        / digest[2:4]
        / digest
    )
    path = service.db.data_dir / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    module.create_backup(service.db.data_dir, tmp_path / "backup")
    manifest = json.loads((tmp_path / "backup/manifest.json").read_bytes())
    record = next(row for row in manifest["files"] if row["path"] == relative.as_posix())
    assert record["kind"] == "file"
    assert record["sha256"] == digest
    module.restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert (tmp_path / "restored" / relative).read_bytes() == raw


def test_actual_CAS_with_wrong_filename_digest_is_still_refused(populated, tmp_path):
    service, _, _ = populated
    bad_digest = "0" * 64
    relative = Path("files/blobs") / hashlib.sha256(b"alice").hexdigest() / "00/00" / bad_digest
    path = service.db.data_dir / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"does not match its content address")
    with pytest.raises(module.DataBackupError, match="CAS_integrity_failed"):
        module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_online_backup_does_not_require_stable_live_main_file_hash(populated, tmp_path, monkeypatch):
    service, _, _ = populated
    real_hash = module._hash

    def reject_live_main_hash(path):
        if Path(path) == service.db.path:
            # A WAL checkpoint may change main-file mtime while it is hashed;
            # this physical-file diagnostic must not gate online backup.
            raise module.DataBackupError("file_changed")
        return real_hash(path)

    monkeypatch.setattr(module, "_hash", reject_live_main_hash)
    keeper = service.db.connect()
    try:
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?)", ("wal-checkpoint", "saved", None, None, 0, now())
        )
        result = module.create_backup(service.db.data_dir, tmp_path / "backup")
        assert result["status"] == "complete"
        with sqlite3.connect(tmp_path / "backup/data/tea.sqlite3") as conn:
            assert conn.execute("SELECT id FROM users WHERE id='wal-checkpoint'").fetchone()
        assert module.verify_backup(tmp_path / "backup")["status"] == "verified"
    finally:
        keeper.close()


def test_intentionally_broken_cold_archive_CAS_is_preserved_and_reported(populated, tmp_path):
    service, _, _ = populated
    expected_digest = "0" * 64
    relative = (
        Path(
            "ingestion-archive/source-data/verification/additional-pytest-temp/test_corrupt_owner_cas_png_is_0/owned/files/blobs"
        )
        / hashlib.sha256(b"owner").hexdigest()
        / "00/00"
        / expected_digest
    )
    path = service.db.data_dir / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"broken PNG")
    actual_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert result["cold_archive_anomalies"] == [
        {"path": relative.as_posix(), "expected_sha256": expected_digest, "actual_sha256": actual_digest}
    ]
    module.restore_backup(tmp_path / "backup", tmp_path / "restored")
    assert (tmp_path / "restored" / relative).read_bytes() == b"broken PNG"
    assert path.read_bytes() == b"broken PNG"
    assert path.name == expected_digest
    copied = tmp_path / "backup/data" / relative
    copied.write_bytes(b"changed archived fault")
    with pytest.raises(module.DataBackupError, match="backup_file_hash_mismatch"):
        module.verify_backup(tmp_path / "backup")


def test_active_campaign_CAS_still_requires_its_own_valid_content_address(populated, tmp_path):
    service, _, _ = populated
    digest = "0" * 64
    path = (
        service.db.data_dir
        / "campaigns/active/files/blobs"
        / hashlib.sha256(b"owner").hexdigest()
        / "00/00"
        / digest
    )
    path.parent.mkdir(parents=True)
    path.write_bytes(b"broken PNG")
    with pytest.raises(module.DataBackupError, match="CAS_integrity_failed"):
        module.create_backup(service.db.data_dir, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()


def test_corrupt_cold_archive_fixture_cannot_back_an_operational_document(populated, tmp_path):
    service, documents, _ = populated
    doc = documents["alice"]
    relative = (
        Path("ingestion-archive/frozen/files/blobs")
        / hashlib.sha256(b"alice").hexdigest()
        / doc["content_hash"][:2]
        / doc["content_hash"][2:4]
        / doc["content_hash"]
    )
    path = service.db.data_dir / relative
    path.parent.mkdir(parents=True)
    raw = b"broken PNG"
    path.write_bytes(raw)
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET storage_path=?,content_hash=?,size=? WHERE id=?",
            (str(path), hashlib.sha256(raw).hexdigest(), len(raw), doc["id"]),
        )
    with pytest.raises(module.DataBackupError, match="cold_archive_reference_not_operational"):
        module.create_backup(service.db.data_dir, tmp_path / "backup")


def test_restored_instance_can_be_backed_up_and_restored_again_without_overwriting_history(
    populated, tmp_path
):
    service, docs, _ = populated
    first_backup, first_restore = tmp_path / "backup-1", tmp_path / "restore-1"
    second_backup, second_restore = tmp_path / "backup-2", tmp_path / "restore-2"
    module.create_backup(service.db.data_dir, first_backup)
    first = module.restore_backup(first_backup, first_restore)
    assert first["receipt_file"] == "RESTORE.json"
    first_receipt = (first_restore / first["receipt_file"]).read_bytes()
    first_manifests = dict(
        sqlite3.connect(first_restore / "tea.sqlite3").execute("SELECT id,manifest FROM documents")
    )
    first_vectors = rows(first_restore / "tea.sqlite3", "retrieval_vectors")
    original_bytes = {key: Path(doc["storage_path"]).read_bytes() for key, doc in docs.items()}
    module.create_backup(first_restore, second_backup)
    second = module.restore_backup(second_backup, second_restore)
    assert second["receipt_file"].startswith("RESTORE-") and second["receipt_file"].endswith(".json")
    assert (first_restore / "RESTORE.json").read_bytes() == first_receipt
    assert (second_restore / "RESTORE.json").read_bytes() == first_receipt
    assert (
        json.loads((second_restore / second["receipt_file"]).read_bytes())["receipt_file"]
        == second["receipt_file"]
    )
    assert (
        dict(sqlite3.connect(second_restore / "tea.sqlite3").execute("SELECT id,manifest FROM documents"))
        == first_manifests
    )
    assert rows(second_restore / "tea.sqlite3", "retrieval_vectors") == first_vectors
    assert rows(first_restore / "tea.sqlite3", "retrieval_vectors") == first_vectors
    with sqlite3.connect(second_restore / "tea.sqlite3") as conn:
        for doc_id, path in conn.execute("SELECT id,storage_path FROM documents"):
            key = next(key for key, value in docs.items() if value["id"] == doc_id)
            assert Path(path).is_relative_to(second_restore)
            assert Path(path).read_bytes() == original_bytes[key]
    assert all(Path(doc["storage_path"]).read_bytes() == original_bytes[key] for key, doc in docs.items())
