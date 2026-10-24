"""A same-original merge retains both paid checkpoints and every old citation."""

import hashlib
import json
from contextlib import closing
from types import SimpleNamespace

import pytest
from support.legacy_vectors import checkpoint_inputs, schema19_snapshot
from app.contexts.collection.application.collection_import import _fingerprint
from app.bootstrap import Service
from app.contexts.knowledge.application.system_library import SYSTEM_OWNER_ID, ensure_principal
from app.shared.domain.values import dump, now
from app.shared.infrastructure.operations import system_corpus_merge as merge
from app.shared.infrastructure.operations import system_corpus_migration as migration
from app.shared.infrastructure.operations.data_backup import DataBackupError, create_backup, restore_backup


def make_doc(service, key, *, complete=True, vector=None, owner=SYSTEM_OWNER_ID):
    raw = ("The original legal instrument " + key).encode()
    blob = service.archive.blobs.put_bytes(owner, raw)
    with service.db.transaction() as conn:
        doc = dict(
            service.archive.store_original(
                conn,
                owner,
                blob,
                key + ".pdf",
                "application/pdf",
                "https://sso.agc.gov.sg/Act/" + key,
                collection="country:SG",
                level="primary",
                tags=[],
                identity_key=key,
                kind_hint="law",
            )
        )
        text = "A complete provision for " + key if complete else ""
        manifest = {
            "source_hash": blob.digest,
            "pages": [
                {
                    "page": 1,
                    "source_hash": blob.digest,
                    "native_text": "",
                    "text": text,
                    "status": "ocr_complete" if complete else "pending_ocr",
                    "ocr": {"complete": complete, "attempts": 1 if complete else 0, "uncertain_spans": []},
                }
            ],
            "chunks": [{"id": "page-1", "text": text, "retrieval_text": text, "locator": {"page": 1}}]
            if complete
            else [],
            "quality": {"complete": complete},
            "collection_import": {
                "protocol": "collected-resource-import-v1",
                "source_owner_sha256": hashlib.sha256(b"upstream").hexdigest(),
                "source_document_id": "upstream-" + key,
                "source_family_id": "upstream-family-" + key,
                "source_version": 1,
                "source_hash": blob.digest,
            },
            "collection_evidence": {"record": {"document_id": doc["id"], "downloaded": True}},
        }
        if vector:
            manifest["embedding_fingerprint"] = service.embedding_fingerprint()
        conn.execute(
            "UPDATE documents SET manifest=?,status=? WHERE id=?",
            (dump(manifest), "ready" if complete else "processing", doc["id"]),
        )
        service.index.sync_document(conn, doc["id"], manifest)
        if vector:
            batch = checkpoint_inputs(conn, doc["id"], service.embedding_fingerprint())
            service.index.store_vectors(conn, doc["id"], service.embedding_fingerprint(), batch, [vector])
        conn.execute(
            "UPDATE processing_jobs SET status=? WHERE document_id=?",
            ("completed" if complete else "queued", doc["id"]),
        )
    return doc["id"]


def scope(service, ids):
    with closing(service.db.connect()) as conn:
        return migration._scope(conn, ids)


def tables(service):
    with closing(service.db.connect()) as conn:
        return {
            r[0]: sorted((tuple(row) for row in conn.execute('SELECT * FROM "' + r[0] + '"')), key=repr)
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        }


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(Service, "embedding_fingerprint", lambda self: _fingerprint())
    for name in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY"):
        monkeypatch.setenv(name, "unit-test")
    monkeypatch.setattr(migration, "MAX_BATCH_DOCUMENTS", 2)
    source, target = Service(tmp_path / "source"), Service(tmp_path / "target")
    for service in (source, target):
        with service.db.transaction() as conn:
            ensure_principal(conn)
    with target.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("private", "Private", None, None, 1, now()))
        conn.execute("INSERT INTO sessions VALUES(?,?,?)", ("session", "private", "2099-01-01"))
    sa = make_doc(source, "same-a", vector=[0.6, 0.8])
    ta = make_doc(target, "same-a", vector=[0.8, 0.6])
    sb = make_doc(source, "same-b", complete=False)
    tb = make_doc(target, "same-b", vector=[1.0, 0.0])
    incoming = [make_doc(source, name, vector=[0.6, 0.8]) for name in ("new-x", "new-y")]
    preserved = [make_doc(target, name, vector=[0.6, 0.8]) for name in ("old-x", "old-y")]
    private = make_doc(target, "private", vector=[0.6, 0.8], owner="private")
    for service, ids in ((source, incoming), (target, preserved)):
        service.archive.add_relation(
            SYSTEM_OWNER_ID,
            ids[0],
            {
                "collection": "country:SG",
                "target_document_id": ids[1],
                "relation": "related",
                "locator": "section 1",
            },
        )
    schema19_snapshot(source)
    schema19_snapshot(target)
    snapshot = tmp_path / "snapshot"
    migration.export_system_corpus(source.db.data_dir, snapshot)
    candidates = [
        {"source_document_id": sa, "target_document_id": ta},
        {"source_document_id": sb, "target_document_id": tb},
    ]
    return {
        "source": source,
        "target": target,
        "snapshot": snapshot,
        "candidates": candidates,
        "source_aliases": [sa, sb],
        "target_aliases": [ta, tb],
        "incoming": incoming,
        "preserved": preserved,
        "private": private,
    }


def plan(f):
    return merge.create_alias_plan(f["snapshot"], f["target"].db.data_dir, f["candidates"])


def apply(f, p, **kw):
    return migration.migrate_system_corpus(
        f["snapshot"], f["target"].db.data_dir, alias_plan=p, apply=True, source_stopped=True, **kw
    )


def test_alias_merge_preserves_paid_results_main_ids_private_data_and_replays(fixture, tmp_path):
    f = fixture
    before = tables(f["target"])
    original = {i: scope(f["target"], [i])["rows"] for i in f["target_aliases"] + f["preserved"]}
    p = plan(f)
    assert p["expected_system_documents"] == 6 and p["expected_relations"] == 2
    assert p["aliases"][0]["source_only_successful_pages"] == 0
    assert tables(f["target"]) == before
    dry = migration.migrate_system_corpus(
        f["snapshot"], f["target"].db.data_dir, alias_plan=p, source_stopped=True
    )
    assert dry["new_documents"] == 2 and dry["target_writes"] == dry["model_calls"] == 0
    result = apply(f, p)
    assert result["new_documents"] == 2 and result["target_writes"] == 4
    assert result["alias_receipts_created"] == 2
    assert result["expected_system_documents"] == 6
    with closing(f["target"].db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 7
        assert conn.execute("SELECT count(*) FROM legal_relations").fetchone()[0] == 2
        for ident in f["source_aliases"]:
            assert conn.execute("SELECT 1 FROM documents WHERE id=?", (ident,)).fetchone() is None
    paths = merge._owner_paths(f["target"].db.data_dir)
    for source_id, target_id in zip(f["source_aliases"], f["target_aliases"], strict=True):
        after = scope(f["target"], [target_id])["rows"]
        current_manifest = json.loads(after["documents"][0]["manifest"])
        merge.validate_alias_archives(current_manifest, SYSTEM_OWNER_ID, paths)
        marker = current_manifest[merge.MARKER][0]
        receipt = json.loads(merge._read_ref(marker["receipt"], paths))
        archived_source = merge._unpack(receipt["source"], paths)
        assert archived_source == scope(f["source"], [source_id])["rows"]
        assert merge._unpack(receipt["target_before"], paths) == original[target_id]
        after["documents"][0]["manifest"] = original[target_id]["documents"][0]["manifest"]
        assert after == original[target_id]
    for ident in f["preserved"]:
        assert scope(f["target"], [ident])["rows"] == original[ident]
    after = tables(f["target"])
    for table in ("users", "sessions", "cases", "messages", "templates"):
        assert before[table] == after[table]
    private_before = next(r for r in before["documents"] if r[0] == f["private"])
    assert private_before in after["documents"]
    again = apply(f, p, backup_dir=tmp_path / "replay-backup")
    assert again["new_documents"] == again["target_writes"] == 0
    assert tables(f["target"]) == after


def test_alias_archives_survive_formal_backup_restore_and_keep_conflicting_blob(fixture, tmp_path):
    f = fixture
    apply(f, plan(f))
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    create_backup(f["target"].db.data_dir, backup)
    restore_backup(backup, restored)
    paths = merge._owner_paths(restored)
    with closing(migration._readonly(restored / "tea.sqlite3")) as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (f["target_aliases"][0],)).fetchone()[0]
        )
    merge.validate_alias_archives(manifest, SYSTEM_OWNER_ID, paths)
    receipt = json.loads(merge._read_ref(manifest[merge.MARKER][0]["receipt"], paths))
    source = merge._unpack(receipt["source"], paths)
    target = merge._unpack(receipt["target_before"], paths)
    assert source["retrieval_vectors"][0]["vector"] != target["retrieval_vectors"][0]["vector"]
    assert source["documents"][0]["id"] == f["source_aliases"][0]
    assert target["documents"][0]["id"] == f["target_aliases"][0]


def test_backup_refuses_missing_archived_paid_blob(fixture, tmp_path):
    f = fixture
    apply(f, plan(f))
    paths = merge._owner_paths(f["target"].db.data_dir)
    source = scope(f["source"], [f["source_aliases"][0]])
    raw = source["rows"]["retrieval_vectors"][0]["vector"]
    paths[migration._sha(raw)].unlink()
    with pytest.raises(DataBackupError, match="alias_archive_missing"):
        create_backup(f["target"].db.data_dir, tmp_path / "bad-backup")


@pytest.mark.parametrize("change", ["page", "vector", "origin", "text", "attempt"])
def test_plan_refuses_unreconciled_source_progress_or_identity(fixture, change):
    f = fixture
    service, ident = f["target"], f["target_aliases"][0]
    with service.db.transaction() as conn:
        if change == "vector":
            conn.execute(
                "DELETE FROM retrieval_vectors WHERE chunk_rowid IN (SELECT rowid FROM retrieval_chunks WHERE document_id=?)",
                (ident,),
            )
        else:
            manifest = json.loads(
                conn.execute("SELECT manifest FROM documents WHERE id=?", (ident,)).fetchone()[0]
            )
            if change == "page":
                manifest["pages"][0]["status"] = "pending_ocr"
            elif change == "origin":
                manifest["collection_import"]["source_document_id"] = "different-source"
            elif change == "text":
                manifest["pages"][0]["text"] = "Different accepted text"
            elif change == "attempt":
                manifest["pages"][0]["ocr"]["attempts"] = 0
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), ident))
    before = tables(service)
    with pytest.raises(migration.SystemCorpusMigrationError, match="alias_"):
        plan(f)
    assert tables(service) == before


def test_pair_requires_same_immutable_country(fixture):
    f = fixture
    source = scope(f["source"], [f["source_aliases"][0]])
    target = scope(f["target"], [f["target_aliases"][0]])
    target["rows"]["document_countries"][0]["country"] = "ID"
    with pytest.raises(migration.SystemCorpusMigrationError, match="alias_country_mismatch"):
        merge._pair(source, target)


@pytest.mark.parametrize("kind", ["parent", "donor", "relation"])
def test_plan_refuses_cross_alias_references(fixture, tmp_path, kind):
    f = fixture
    source, donor, ident = f["source"], f["source_aliases"][0], f["incoming"][0]
    with source.db.transaction() as conn:
        if kind == "parent":
            conn.execute("UPDATE documents SET parent_id=? WHERE id=?", (donor, ident))
        elif kind == "donor":
            manifest = json.loads(
                conn.execute("SELECT manifest FROM documents WHERE id=?", (ident,)).fetchone()[0]
            )
            manifest["separate_evidence"] = {"source_document_id": donor}
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), ident))
            conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (ident,))
        else:
            source.archive.add_relation(
                SYSTEM_OWNER_ID,
                ident,
                {
                    "collection": "country:SG",
                    "target_document_id": donor,
                    "relation": "cites",
                    "locator": "s1",
                },
                conn,
            )
    f["snapshot"] = tmp_path / "snapshot-refs"
    migration.export_system_corpus(source.db.data_dir, f["snapshot"])
    with pytest.raises(migration.SystemCorpusMigrationError, match="alias_|normative_document_identity"):
        plan(f)


def test_apply_refuses_target_mutation_and_live_source_resume(fixture):
    f = fixture
    p = plan(f)
    with f["target"].db.transaction() as conn:
        conn.execute("UPDATE documents SET title='changed' WHERE id=?", (f["preserved"][0],))
    before = tables(f["target"])
    with pytest.raises(migration.SystemCorpusMigrationError, match="preserved_target_changed"):
        apply(f, p)
    assert tables(f["target"]) == before


def test_apply_refuses_live_source_progress_without_rebilling(fixture):
    f = fixture
    p = plan(f)
    with f["source"].db.transaction() as conn:
        conn.execute(
            "UPDATE processing_jobs SET attempts=attempts+1 WHERE document_id=?", (f["source_aliases"][1],)
        )
    before = tables(f["target"])
    with pytest.raises(migration.SystemCorpusMigrationError, match="source_resumed_or_changed"):
        apply(f, p)
    assert tables(f["target"]) == before


def test_source_commit_while_live_snapshot_is_read_cannot_escape_guard(fixture, monkeypatch):
    f = fixture
    p = plan(f)
    before = tables(f["target"])
    original = migration._stream_summary
    changed = False

    def concurrent_summary(conn):
        nonlocal changed
        result = original(conn)
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        if path == str(f["source"].db.path) and not changed:
            changed = True
            with f["source"].db.transaction() as writer:
                writer.execute(
                    "UPDATE processing_jobs SET attempts=attempts+1 WHERE document_id=?",
                    (f["source_aliases"][1],),
                )
        return result

    monkeypatch.setattr(migration, "_stream_summary", concurrent_summary)
    with pytest.raises(migration.SystemCorpusMigrationError, match="source_resumed_or_changed_during_copy"):
        apply(f, p)
    assert changed
    after = tables(f["target"])
    for table, rows in before.items():
        if table != "schema_migrations":
            assert after[table] == rows, table


def test_default_migration_still_refuses_same_family_different_ids(fixture):
    f = fixture
    with pytest.raises(
        migration.SystemCorpusMigrationError,
        match="target_family_version_conflict|target_system_scope_contains_other_documents",
    ):
        migration.migrate_system_corpus(f["snapshot"], f["target"].db.data_dir, source_stopped=True)


def test_interrupted_committed_batch_resumes_without_replacing_success(fixture, monkeypatch, tmp_path):
    f = fixture
    p = plan(f)
    original = migration._copy_batch
    calls = 0
    monkeypatch.setattr(migration, "MAX_BATCH_DOCUMENTS", 1)

    def interrupt(conn, expected, existing):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated interruption")
        return original(conn, expected, existing)

    monkeypatch.setattr(migration, "_copy_batch", interrupt)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        apply(f, p)
    with closing(f["target"].db.connect()) as conn:
        committed = {r[0] for r in conn.execute("SELECT id FROM documents WHERE id IN (?,?)", f["incoming"])}
    assert len(committed) == 1
    before = scope(f["target"], list(committed))["rows"]
    monkeypatch.setattr(migration, "_copy_batch", original)
    apply(f, p, backup_dir=tmp_path / "resume-backup")
    assert scope(f["target"], list(committed))["rows"] == before


def test_formal_delete_journal_restore_has_readonly_preflight_and_idempotent_apply(fixture, tmp_path):
    f = dict(fixture)
    backup, restored = tmp_path / "formal-backup", tmp_path / "formal-restored"
    create_backup(f["target"].db.data_dir, backup)
    restore_backup(backup, restored)
    database = restored / "tea.sqlite3"
    # Do not instantiate Service before exercising the real restored target:
    # that would conceal the DELETE-to-WAL initialization boundary under test.
    f["target"] = SimpleNamespace(
        db=SimpleNamespace(data_dir=restored, connect=lambda: migration._readonly(database))
    )
    before = tables(f["target"])
    before_sha = migration._file_sha(database)
    with closing(migration._readonly(database)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    p = json.loads(json.dumps(plan(f)))
    dry = migration.migrate_system_corpus(f["snapshot"], restored, alias_plan=p, source_stopped=True)
    assert dry["target_writes"] == 0 and dry["blockers"] == []
    assert tables(f["target"]) == before
    assert migration._file_sha(database) == before_sha
    with closing(migration._readonly(database)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    result = apply(f, p)
    assert result["new_documents"] == result["alias_receipts_created"] == 2
    with closing(migration._readonly(database)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    after = tables(f["target"])
    assert apply(f, p, backup_dir=tmp_path / "replay-restored")["target_writes"] == 0
    assert tables(f["target"]) == after


def test_target_change_during_service_initialization_is_not_hidden_by_old_read_snapshot(
    fixture, monkeypatch, tmp_path
):
    from app import bootstrap as service_module

    f = fixture
    p = plan(f)
    ident = f["target_aliases"][0]
    original_service = service_module.Service

    def changed_target(directory):
        service = original_service(directory)
        with service.db.transaction() as conn:
            conn.execute("UPDATE documents SET title='Concurrent change' WHERE id=?", (ident,))
        return service

    monkeypatch.setattr(service_module, "Service", changed_target)
    backup = tmp_path / "initialization-refused"
    with pytest.raises(migration.SystemCorpusMigrationError, match="alias_target_changed_since_plan"):
        apply(f, p, backup_dir=backup)
    with closing(f["target"].db.connect()) as conn:
        assert conn.execute("SELECT title FROM documents WHERE id=?", (ident,)).fetchone()[0] == (
            "Concurrent change"
        )
        assert not conn.execute("SELECT 1 FROM documents WHERE id IN (?,?)", f["incoming"]).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM documents WHERE json_type(manifest,'$.corpus_import_aliases') IS NOT NULL"
        ).fetchone()
    progress = json.loads((backup / "migration-progress.json").read_bytes())
    assert progress["mode"] == "interrupted" and progress["completed_document_batches"] == 0


@pytest.mark.parametrize("owner", [SYSTEM_OWNER_ID, "private"])
def test_legacy_merge_never_adopts_cloud_evidence_or_changes_private_index(fixture, owner):
    f = fixture
    Service(f["target"].db.data_dir)
    with f["target"].db.transaction() as conn:
        conn.execute(
            "INSERT INTO ragflow_datasets(owner_id,profile,name,created_at) VALUES(?,?,?,?)",
            (owner, "fixture", "retained-index", now()),
        )
    before = tables(f["target"])
    if owner == SYSTEM_OWNER_ID:
        with pytest.raises(migration.SystemCorpusMigrationError, match="alias_target_has_cloud_evidence"):
            plan(f)
        assert tables(f["target"]) == before
    else:
        apply(f, plan(f))
        assert tables(f["target"])["ragflow_datasets"] == before["ragflow_datasets"]
