"""ID-preserving system migration must not overwrite an existing private app."""

import hashlib
import io
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image

from app import system_corpus_migration as module
from app.db import dump, now
from app.defaults import DEFAULT_TEMPLATE
from app.service import Service
from app.system_library import SYSTEM_OWNER_ID, ensure_principal

AUTOMATIC_STREAM_REQUIRED = module._stream_required


def private_state(service):
    with closing(service.db.connect()) as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            for table in [
                "users",
                "sessions",
                "cases",
                "templates",
                "task_states",
                "facts",
                "messages",
                "events",
            ]
        }


def downgrade9(service):
    with service.db.transaction() as conn:
        for name in [
            "retrieval_chunk_insert",
            "retrieval_chunk_delete",
            "retrieval_chunk_update",
            "retrieval_document_insert",
            "retrieval_document_update",
        ]:
            conn.execute("DROP TRIGGER " + name)
        for name in [
            "retrieval_fts",
            "retrieval_vectors",
            "retrieval_models",
            "retrieval_chunks",
            "retrieval_dirty",
            "retrieval_documents",
            "collection_transfer_parts",
            "collection_transfers",
            "checklist_page_evidence",
        ]:
            conn.execute("DROP TABLE " + name)
        for name in (
            "guide_messages",
            "node_memories",
            "conversation_messages",
            "stage_guide_runs",
            "stage_conversations",
            "user_access",
        ):
            conn.execute("DROP TABLE IF EXISTS " + name)
        conn.execute("DELETE FROM schema_migrations WHERE version>9")


@pytest.fixture(params=["legacy", "streaming"])
def pair(tmp_path, monkeypatch, request):
    if request.param == "streaming":
        monkeypatch.setattr(module, "_stream_required", lambda _conn: True)
        monkeypatch.setattr(module, "MAX_BATCH_DOCUMENTS", 2)
    for key in ["EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY"]:
        monkeypatch.setenv(key, "unit-test-model")
    source, target = Service(tmp_path / "source"), Service(tmp_path / "target")
    with source.db.transaction() as conn:
        ensure_principal(conn)
        conn.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?)", ("source-private", "Do not copy", None, None, 1, now())
        )
    with target.db.transaction() as conn:
        conn.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?)", ("target-private", "Keep", None, None, 1, now())
        )
        conn.execute(
            "INSERT INTO sessions VALUES(?,?,?)", ("private-session", "target-private", "2099-01-01")
        )
    mode = next(row["id"] for row in DEFAULT_TEMPLATE["modes"] if row["enabled"])
    target.create_case(
        "target-private",
        {
            "title": "Keep trade case",
            "company": "Company",
            "product": "tea",
            "mode_id": mode,
            "template_id": DEFAULT_TEMPLATE["id"],
        },
    )
    buf = io.BytesIO()
    Image.new("RGB", (2, 3), "white").save(buf, format="PNG")
    png = source.archive.blobs.put_bytes(SYSTEM_OWNER_ID, buf.getvalue())
    docs = {}
    with source.db.transaction() as conn:
        for key, level in [
            ("a", "primary"),
            ("b", "primary"),
            ("secondary", "secondary"),
            ("pending", "primary"),
            ("failed", "primary"),
            ("attachment", "attachment"),
        ]:
            raw = ("original a" if key in {"a", "b"} else "original " + key).encode()
            blob = source.archive.blobs.put_bytes(SYSTEM_OWNER_ID, raw)
            doc = dict(
                source.archive.store_original(
                    conn,
                    SYSTEM_OWNER_ID,
                    blob,
                    key + ".txt",
                    "text/plain",
                    "https://www.bi.go.id/" + ("a" if key == "b" else key),
                    collection="country:ID",
                    level=level,
                    tags=[],
                    identity_key=key,
                    parent_id=docs["a"]["id"] if level == "attachment" else None,
                )
            )
            docs[key] = doc
            if level == "attachment":
                continue
            text = raw.decode()
            image = {
                "content_hash": png.digest,
                "size": png.size,
                "storage_path": str(png.path.relative_to(source.db.data_dir)),
                "mime_type": "image/png",
            }
            page = {
                "page": 1,
                "source_hash": blob.digest,
                "native_text": text,
                "text": text,
                "status": "ocr_complete",
                "ocr": {
                    "image_sha256": png.digest,
                    "image_blob": image,
                    "complete": True,
                    "uncertain_spans": [],
                    "attempts": 1,
                },
            }
            if key == "b":
                first = json.loads(
                    conn.execute("SELECT manifest FROM documents WHERE id=?", (docs["a"]["id"],)).fetchone()[
                        0
                    ]
                )["pages"][0]
                page["ocr"]["reuse"] = {
                    "protocol": "same-owner-pdf-page-reuse-v1",
                    "source_document_id": docs["a"]["id"],
                    "source_document_version": 1,
                    "source_content_hash": blob.digest,
                    "source_page": 1,
                    "source_page_checkpoint_sha256": module._hash_json(first),
                    "target_document_id": doc["id"],
                    "target_document_version": 1,
                }
            complete = key not in {"pending", "failed"}
            if not complete:
                page["status"] = "pending_ocr" if key == "pending" else "ocr_uncertain"
                page["ocr"]["complete"] = False
            manifest = {
                "source_hash": blob.digest,
                "elements": [],
                "chunks": [{"id": "chunk-1", "text": text, "retrieval_text": text, "locator": {"page": 1}}],
                "pages": [page],
                "quality": {"complete": complete},
            }
            if complete and level == "primary":
                manifest["embedding_fingerprint"] = source.embedding_fingerprint()
            conn.execute(
                "UPDATE documents SET manifest=?,status=? WHERE id=?",
                (dump(manifest), "ready" if complete else "processing", doc["id"]),
            )
            source.index.sync_document(conn, doc["id"], manifest)
            if complete and level == "primary":
                batch = source.index.pending(conn, doc["id"], source.embedding_fingerprint())
                source.index.store_vectors(
                    conn, doc["id"], source.embedding_fingerprint(), batch, [[0.6, 0.8] for _ in batch]
                )
            conn.execute(
                "UPDATE processing_jobs SET status=?,error=?,attempts=? WHERE document_id=?",
                (
                    "completed" if complete else "failed" if key == "failed" else "queued",
                    "review required" if key == "failed" else None,
                    3 if key == "failed" else 0,
                    doc["id"],
                ),
            )
        for number, a, b, relation in [
            (1, "a", "b", "related"),
            (2, "a", "b", "cites"),
            (3, "b", "a", "related"),
        ]:
            registry = {
                "owner_id": SYSTEM_OWNER_ID,
                "relations": [
                    {
                        "source_document_id": docs[a]["id"],
                        "source_version": 1,
                        "source_hash": docs[a]["content_hash"],
                        "target_document_id": docs[b]["id"],
                        "target_version": 1,
                        "target_hash": docs[b]["content_hash"],
                    }
                ],
                "immutable_proof_number": number,
            }
            blob = source.archive.blobs.put_bytes(SYSTEM_OWNER_ID, dump(registry).encode())
            source.archive.add_relation(
                SYSTEM_OWNER_ID,
                docs[a]["id"],
                {
                    "collection": "country:ID",
                    "target_document_id": docs[b]["id"],
                    "relation": relation,
                    "locator": dump(
                        {
                            "registry_sha256": blob.digest,
                            "registry_path": str(blob.path.relative_to(source.db.data_dir)),
                        }
                    ),
                },
                conn,
            )
    return source, target, docs


def snapshot(source, tmp_path):
    path = tmp_path / "snapshot"
    result = module.export_system_corpus(source.db.data_dir, path)
    assert result["source_DB_writes"] == result["HTTP_calls"] == result["model_calls"] == 0
    return path


def test_subset_export_refuses_to_drop_system_recovery_history(pair, tmp_path):
    from app.collection_recovery import Decision

    source, _target, docs = pair
    document = docs["a"]
    with source.db.transaction() as conn:
        source.collection.recovery.record_failure(
            conn,
            {"document_id": document["id"], "owner_id": SYSTEM_OWNER_ID, "phase": "embed", "attempts": 3},
            processing=True,
            decision=Decision("paid_result_unknown"),
            exhausted=True,
        )
        before = [tuple(row) for row in conn.execute("SELECT * FROM collection_recovery_events")]
    with pytest.raises(
        module.SystemCorpusMigrationError, match="recovery_ledger_requires_full_backup_restore"
    ):
        module.export_system_corpus(source.db.data_dir, tmp_path / "blocked-subset")
    with closing(source.db.connect()) as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM collection_recovery_events")] == before


def test_schema9_upgrade_preserves_private_business_and_exact_ids_proofs_and_vectors(pair, tmp_path):
    source, target, docs = pair
    before = private_state(target)
    downgrade9(target)
    exported = snapshot(source, tmp_path)
    dry = module.migrate_system_corpus(exported, target.db.data_dir, source_stopped=True)
    assert dry["target_schema_before"] == 9 and dry["target_writes"] == 0 and not dry["blockers"]
    with closing(sqlite3.connect(target.db.path)) as conn:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 9
    result = module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    assert (
        result["target_writes"] == 6
        and result["true_vectors"] == 2
        and result["relations"] == 3
        and result["target_schema_after"] == 19
    )
    after = private_state(target)
    after["users"] = [row for row in after["users"] if row[0] != SYSTEM_OWNER_ID]
    assert before == after
    with closing(source.db.connect()) as a, closing(target.db.connect()) as b:
        for doc in docs.values():
            src = a.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()
            dst = b.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()
            assert (
                src["manifest"] == dst["manifest"]
                and src["family_id"] == dst["family_id"]
                and dst["version"] == 1
            )
            assert Path(dst["storage_path"]).is_relative_to(target.db.data_dir)
        assert [row[0] for row in a.execute("SELECT vector FROM retrieval_vectors ORDER BY chunk_rowid")] == [
            row[0] for row in b.execute("SELECT vector FROM retrieval_vectors ORDER BY chunk_rowid")
        ]
        failed = b.execute(
            "SELECT * FROM processing_jobs WHERE document_id=?", (docs["failed"]["id"],)
        ).fetchone()
        assert (
            failed["status"] == "failed" and failed["attempts"] == 3 and failed["error"] == "review required"
        )
        assert not b.execute(
            "SELECT 1 FROM processing_jobs WHERE lease_token IS NOT NULL OR lease_until IS NOT NULL"
        ).fetchone()
        for row in b.execute("SELECT locator FROM legal_relations"):
            ref = json.loads(row[0])
            raw = (target.db.data_dir / ref["registry_path"]).read_bytes()
            assert hashlib.sha256(raw).hexdigest() == ref["registry_sha256"]
    with sqlite3.connect(result["target_backup_path"]) as conn:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 9
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    repeated = module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    assert repeated["target_writes"] == 0 and repeated["unchanged_documents"] == 6


@pytest.mark.parametrize("source_job", ["completed", "missing"])
def test_partial_vector_cache_without_manifest_fingerprint_keeps_bytes_and_queues_missing_chunks(
    pair, tmp_path, source_job
):
    source, target, docs = pair
    document_id = docs["pending"]["id"]
    fingerprint = source.embedding_fingerprint()
    with source.db.transaction() as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document_id,)).fetchone()[0]
        )
        page = manifest["pages"][0]
        page["status"], page["ocr"]["complete"] = "ocr_complete", True
        manifest["quality"]["complete"] = True
        # Both fragments come from the existing original/native text. Only the
        # first has a durable current-model vector; no completion marker exists.
        manifest["chunks"] = [
            {"id": f"part-{number}", "text": word, "retrieval_text": word, "locator": {"page": 1}}
            for number, word in enumerate(page["native_text"].split(), 1)
        ]
        manifest.pop("embedding_fingerprint", None)
        manifest_bytes = dump(manifest)
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (manifest_bytes, document_id))
        source.index.sync_document(conn, document_id, manifest)
        pending = source.index.pending(conn, document_id, fingerprint)
        assert [chunk["chunk_id"] for chunk in pending] == ["part-1", "part-2"]
        source.index.store_vectors(conn, document_id, fingerprint, pending[:1], [[0.6, 0.8]])
        if source_job == "missing":
            conn.execute("DELETE FROM processing_jobs WHERE document_id=?", (document_id,))
        else:
            conn.execute("UPDATE processing_jobs SET status='completed' WHERE document_id=?", (document_id,))
        cached = tuple(
            conn.execute(
                """SELECT v.fingerprint,v.input_hash,v.dimension,v.vector FROM retrieval_vectors v
                JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id=?""",
                (document_id,),
            ).fetchone()
        )
        original_scope = module._scope(conn)
    exported = snapshot(source, tmp_path)
    result = module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    assert result["true_vectors"] == 3 and result["vector_bytes_preserved_without_renormalization"]
    assert result["model_calls"] == result["HTTP_calls"] == 0
    with closing(target.db.connect()) as conn:
        migrated = conn.execute("SELECT manifest,status FROM documents WHERE id=?", (document_id,)).fetchone()
        assert migrated["manifest"] == manifest_bytes and migrated["status"] == "processing"
        assert "embedding_fingerprint" not in json.loads(migrated["manifest"])
        actual = tuple(
            conn.execute(
                """SELECT v.fingerprint,v.input_hash,v.dimension,v.vector FROM retrieval_vectors v
                JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id=?""",
                (document_id,),
            ).fetchone()
        )
        assert actual == cached
        assert [chunk["chunk_id"] for chunk in target.index.pending(conn, document_id, fingerprint)] == [
            "part-2"
        ]
        job = conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (document_id,)).fetchone()
        assert job["status"] == "queued" and job["phase"] == "embed" and job["attempts"] == 0
        assert job["lease_token"] is job["lease_until"] is job["error"] is None
    with closing(source.db.connect()) as conn:
        assert module._scope(conn) == original_scope
    repeated = module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    assert repeated["target_writes"] == 0 and repeated["unchanged_documents"] == 6


@pytest.mark.parametrize(
    "kind", ["source_lease", "target_lease", "target_edit", "FP", "PNG", "source_changed", "stop_flag"]
)
def test_conflicts_refuse_without_overwriting_target(pair, tmp_path, monkeypatch, kind):
    source, target, docs = pair
    if kind == "source_lease":
        with source.db.transaction() as conn:
            conn.execute(
                "UPDATE processing_jobs SET status='processing',lease_token='owned',lease_until=9999999999 WHERE document_id=?",
                (docs["pending"]["id"],),
            )
    if kind == "PNG":
        with source.db.transaction() as conn:
            manifest = json.loads(
                conn.execute("SELECT manifest FROM documents WHERE id=?", (docs["a"]["id"],)).fetchone()[0]
            )
            manifest["pages"][0]["ocr"]["image_blob"]["storage_path"] = docs["a"]["storage_path"]
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), docs["a"]["id"]))
            source.index.sync_document(conn, docs["a"]["id"], manifest)
    exported = snapshot(source, tmp_path)
    if kind in {"target_edit", "target_lease"}:
        module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
        with target.db.transaction() as conn:
            if kind == "target_edit":
                conn.execute(
                    "UPDATE document_metadata SET revision=revision+1 WHERE document_id=?", (docs["a"]["id"],)
                )
            else:
                conn.execute(
                    "UPDATE processing_jobs SET status='processing',lease_token='target-owned',lease_until=9999999999 WHERE document_id=?",
                    (docs["pending"]["id"],),
                )
    elif kind == "FP":
        monkeypatch.setenv("EMBEDDING_MODEL", "another-model")
    elif kind == "source_changed":
        with source.db.transaction() as conn:
            conn.execute(
                "UPDATE document_metadata SET revision=revision+1 WHERE document_id=?", (docs["a"]["id"],)
            )
    with closing(target.db.connect()) as conn:
        before = module._scope(conn)
    with pytest.raises(module.SystemCorpusMigrationError):
        module.migrate_system_corpus(
            exported, target.db.data_dir, apply=True, source_stopped=kind != "stop_flag"
        )
    with closing(target.db.connect()) as conn:
        assert module._scope(conn) == before


def test_late_copy_failure_rolls_back_only_corpus_and_keeps_backup_upgrade(pair, tmp_path, monkeypatch):
    source, target, _ = pair
    before = private_state(target)
    downgrade9(target)
    exported = snapshot(source, tmp_path)
    original = module._insert

    def fail(conn, table, row):
        if table == "legal_relations":
            raise RuntimeError("injected relationship failure")
        return original(conn, table, row)

    monkeypatch.setattr(module, "_insert", fail)
    with pytest.raises(RuntimeError, match="relationship failure"):
        module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    streaming = json.loads((exported / "snapshot.json").read_text())["protocol"] == module.STREAM_PROTOCOL
    after = private_state(target)
    if streaming:
        after["users"] = [row for row in after["users"] if row[0] != SYSTEM_OWNER_ID]
    assert after == before
    with closing(target.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == (6 if streaming else 0)
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 19
    backups = list((target.db.data_dir / "migration-backups").glob("*/tea.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 9
    if streaming:
        progress = json.loads((backups[0].parent / "migration-progress.json").read_text())
        assert progress["mode"] == "interrupted" and progress["completed_documents"] == 6
        monkeypatch.setattr(module, "_insert", original)
        result = module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
        assert result["target_writes"] == 0 and result["unchanged_documents"] == 6
        with closing(target.db.connect()) as conn:
            assert conn.execute("SELECT count(*) FROM legal_relations").fetchone()[0] == 3


def test_private_document_primary_key_collision_is_not_adopted(pair, tmp_path):
    source, target, docs = pair
    with closing(source.db.connect()) as conn:
        row = dict(conn.execute("SELECT * FROM documents WHERE id=?", (docs["a"]["id"],)).fetchone())
    row["owner_id"] = "target-private"
    with target.db.transaction() as conn:
        module._insert(conn, "documents", row)
    exported = snapshot(source, tmp_path)
    with pytest.raises(module.SystemCorpusMigrationError, match="target_document_conflict"):
        module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    with closing(target.db.connect()) as conn:
        assert (
            conn.execute("SELECT owner_id FROM documents WHERE id=?", (docs["a"]["id"],)).fetchone()[0]
            == "target-private"
        )
        assert not conn.execute("SELECT 1 FROM users WHERE id=?", (SYSTEM_OWNER_ID,)).fetchone()


@pytest.mark.parametrize("corruption", ["unit_vector", "registry_path", "OCR_donor"])
def test_invalid_checkpoint_identity_is_rejected_before_copy(pair, tmp_path, corruption):
    source, target, docs = pair
    with source.db.transaction() as conn:
        if corruption == "unit_vector":
            import numpy as np

            conn.execute(
                "UPDATE retrieval_vectors SET vector=?", (np.array([float("nan"), 0], dtype="<f4").tobytes(),)
            )
        elif corruption == "registry_path":
            row = conn.execute("SELECT id,locator FROM legal_relations LIMIT 1").fetchone()
            locator = json.loads(row["locator"])
            locator["registry_path"] = str(Path(docs["a"]["storage_path"]).relative_to(source.db.data_dir))
            conn.execute("UPDATE legal_relations SET locator=? WHERE id=?", (dump(locator), row["id"]))
        else:
            manifest = json.loads(
                conn.execute("SELECT manifest FROM documents WHERE id=?", (docs["b"]["id"],)).fetchone()[0]
            )
            manifest["pages"][0]["ocr"]["reuse"]["source_document_id"] = "missing-donor"
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), docs["b"]["id"]))
            source.index.sync_document(conn, docs["b"]["id"], manifest)
    exported = snapshot(source, tmp_path)
    with pytest.raises(module.SystemCorpusMigrationError):
        module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    with closing(target.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert not (target.db.data_dir / "migration-backups").exists()


def test_streaming_committed_batches_resume_with_cross_batch_proof_and_private_data(
    pair, tmp_path, monkeypatch
):
    source, target, _docs = pair
    monkeypatch.setattr(module, "_stream_required", lambda _conn: True)
    monkeypatch.setattr(module, "MAX_BATCH_DOCUMENTS", 1)
    before = private_state(target)
    exported = snapshot(source, tmp_path)
    original, calls = module._copy_batch, []

    def fail_second(conn, expected, existing_ids):
        calls.append(expected["documents"][0]["id"])
        if len(calls) == 2:
            raise RuntimeError("bounded interruption")
        return original(conn, expected, existing_ids)

    monkeypatch.setattr(module, "_copy_batch", fail_second)
    with pytest.raises(RuntimeError, match="bounded interruption"):
        module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    with closing(target.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    original_backup = next((target.db.data_dir / "migration-backups").glob("*/tea.sqlite3"))
    backup_sha = module._file_sha(original_backup)
    monkeypatch.setattr(module, "_copy_batch", original)
    # Resume may use another safe batch size; durable data, not a saved offset,
    # determines what was committed before the interruption.
    monkeypatch.setattr(module, "MAX_BATCH_DOCUMENTS", 3)
    result = module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    assert result["target_writes"] == 5 and result["unchanged_documents"] == 1
    assert result["maximum_transaction_seconds"] < module.MAX_TRANSACTION_SECONDS
    repeated = module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    assert repeated["target_writes"] == 0
    assert module._file_sha(original_backup) == backup_sha
    after = private_state(target)
    after["users"] = [row for row in after["users"] if row[0] != SYSTEM_OWNER_ID]
    assert after == before


def test_streaming_detects_source_resume_between_batches(pair, tmp_path, monkeypatch):
    source, target, docs = pair
    monkeypatch.setattr(module, "_stream_required", lambda _conn: True)
    monkeypatch.setattr(module, "MAX_BATCH_DOCUMENTS", 1)
    exported = snapshot(source, tmp_path)
    original = module._copy_batch

    def resume_source(conn, expected, existing_ids):
        original(conn, expected, existing_ids)
        with source.db.transaction() as other:
            other.execute(
                "UPDATE document_metadata SET revision=revision+1 WHERE document_id=?", (docs["a"]["id"],)
            )

    monkeypatch.setattr(module, "_copy_batch", resume_source)
    with pytest.raises(module.SystemCorpusMigrationError, match="source_resumed_or_changed"):
        module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    with closing(target.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1


def test_streaming_single_document_deadline_rolls_back_and_keeps_resume_evidence(pair, tmp_path, monkeypatch):
    source, target, _docs = pair
    monkeypatch.setattr(module, "_stream_required", lambda _conn: True)
    exported = snapshot(source, tmp_path)
    monkeypatch.setattr(module, "MAX_TRANSACTION_SECONDS", 0)
    with pytest.raises(module.SystemCorpusMigrationError, match="single_document_transaction_limit"):
        module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    with closing(target.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    progress = json.loads(
        next((target.db.data_dir / "migration-backups").glob("*/migration-progress.json")).read_text()
    )
    assert progress["mode"] == "interrupted" and progress["completed_documents"] == 0


@pytest.mark.parametrize("limit", ["MAX_STREAM_DOCUMENTS", "MAX_STREAM_BYTES", "MAX_BATCH_BYTES"])
def test_streaming_explicit_bounds_refuse_before_target_writes(pair, tmp_path, monkeypatch, limit):
    source, target, _docs = pair
    monkeypatch.setattr(module, "_stream_required", lambda _conn: True)
    monkeypatch.setattr(module, limit, 1)
    before = private_state(target)
    with pytest.raises(module.SystemCorpusMigrationError, match="too_large"):
        snapshot(source, tmp_path)
    assert private_state(target) == before
    assert not (target.db.data_dir / "migration-backups").exists()


def test_more_than_one_thousand_documents_use_bounded_migration(pair, tmp_path, monkeypatch):
    source, target, _docs = pair
    # Exercise the real automatic dispatch rather than just the forced test path.
    monkeypatch.setattr(module, "_stream_required", AUTOMATIC_STREAM_REQUIRED)
    monkeypatch.setattr(module, "MAX_BATCH_DOCUMENTS", 40)
    blob = source.archive.blobs.put_bytes(SYSTEM_OWNER_ID, b"official pending original")
    with source.db.transaction() as conn:
        for number in range(1001):
            document = source.archive.store_original(
                conn,
                SYSTEM_OWNER_ID,
                blob,
                f"pending-{number}.txt",
                "text/plain",
                f"https://www.bi.go.id/pending-{number}",
                collection="country:ID",
                level="primary",
                tags=[],
                identity_key=f"large-{number}",
            )
            conn.execute(
                "UPDATE documents SET manifest=? WHERE id=?",
                (dump({"source_hash": blob.digest, "quality": {"complete": False}}), document["id"]),
            )
    exported = snapshot(source, tmp_path)
    result = module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    assert result["protocol"] == module.STREAM_PROTOCOL
    assert result["documents"] == result["target_writes"] == 1007
    assert result["completed_document_batches"] >= 26
    assert result["model_calls"] == result["HTTP_calls"] == 0
    assert result["document_limit"] >= 30000


def test_streaming_splits_over_budget_batch_and_never_replays_committed_pages(pair, tmp_path, monkeypatch):
    import time

    source, target, _docs = pair
    monkeypatch.setattr(module, "_stream_required", lambda _conn: True)
    monkeypatch.setattr(module, "MAX_BATCH_DOCUMENTS", 2)
    exported = snapshot(source, tmp_path)
    original = module._copy_batch
    timed_out = []

    def slow_group(conn, expected, existing_ids):
        original(conn, expected, existing_ids)
        if len(expected["documents"]) > 1:
            timed_out.append([row["id"] for row in expected["documents"]])
            time.sleep(0.3)

    monkeypatch.setattr(module, "_copy_batch", slow_group)
    monkeypatch.setattr(module, "MAX_TRANSACTION_SECONDS", 0.2)
    result = module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
    assert len(timed_out) == 3
    assert result["completed_document_batches"] == 6
    assert result["target_writes"] == 6 and result["true_vectors"] == 2
    assert result["transaction_execution_budget_seconds"] == 0.2
    assert result["commit_duration_hard_limit"] is False


@pytest.mark.parametrize(
    "corruption", [None, "input_hash", "target_vector", "donor_vector", "unknown_protocol", "copied_approval"]
)
def test_exact_input_reuse_protocol_preserves_or_rejects_precise_vector_binding(
    pair, tmp_path, monkeypatch, corruption
):
    source, target, docs = pair
    monkeypatch.setattr(module, "MAX_BATCH_DOCUMENTS", 1)
    with source.db.transaction() as conn:
        donor = docs["a"]
        document = docs["b"]
        chunk = dict(
            conn.execute("SELECT * FROM retrieval_chunks WHERE document_id=?", (document["id"],)).fetchone()
        )
        vector = dict(
            conn.execute("SELECT * FROM retrieval_vectors WHERE chunk_rowid=?", (chunk["rowid"],)).fetchone()
        )
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
        )
        record = {
            "match_policy": "exact_input",
            "source_document_id": donor["id"],
            "source_document_version": 1,
            "source_hash": donor["content_hash"],
            "source_url": donor["source_url"],
            "target_document_id": document["id"],
            "target_document_version": 1,
            "target_source_hash": document["content_hash"],
            "target_country": "ID",
            "chunk_id": chunk["chunk_id"],
            "input_hash": chunk["input_hash"],
            "retrieval_text_sha256": module._sha(chunk["retrieval_text"].encode()),
            "fingerprint": vector["fingerprint"],
            "dimension": vector["dimension"],
            "target_vector_sha256": module._sha(vector["vector"]),
            "donor_vector_sha256": module._sha(vector["vector"]),
            "target_locators_or_approval_copied": False,
        }
        provenance = {
            "protocol": "same-owner-input-embedding-reuse-v1",
            "fingerprint": vector["fingerprint"],
            "records": {chunk["chunk_id"]: record},
            "last_batch": {"provider_input_chunks": 0},
        }
        if corruption == "input_hash":
            record["input_hash"] = "0" * 64
        if corruption == "target_vector":
            record["target_vector_sha256"] = "0" * 64
        if corruption == "donor_vector":
            record["donor_vector_sha256"] = "0" * 64
        if corruption == "unknown_protocol":
            provenance["protocol"] = "unrecognized-v2"
        if corruption == "copied_approval":
            record["target_locators_or_approval_copied"] = True
        manifest["embedding_reuse"] = provenance
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), document["id"]))
        source.index.sync_document(conn, document["id"], manifest)
    exported = snapshot(source, tmp_path)
    if corruption:
        with pytest.raises(module.SystemCorpusMigrationError, match="embedding"):
            module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
        assert not (target.db.data_dir / "migration-backups").exists()
    else:
        result = module.migrate_system_corpus(exported, target.db.data_dir, apply=True, source_stopped=True)
        assert result["true_vectors"] == 2
        with closing(target.db.connect()) as conn:
            assert (
                json.loads(
                    conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
                )["embedding_reuse"]
                == provenance
            )


def test_vector_validation_checks_full_text_once_per_document(pair, monkeypatch):
    source, _target, docs = pair
    with source.db.transaction() as conn:
        ident = docs["a"]["id"]
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (ident,)).fetchone()[0]
        )
        manifest["chunks"].append(
            {"id": "chunk-2", "text": "original", "retrieval_text": "original", "locator": {"page": 1}}
        )
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), ident))
        source.index.sync_document(conn, ident, manifest)
        pending = source.index.pending(conn, ident, source.embedding_fingerprint())
        source.index.store_vectors(
            conn, ident, source.embedding_fingerprint(), pending, [[0.6, 0.8] for _ in pending]
        )
    with closing(source.db.connect()) as conn:
        scope = module._scope(conn)
        assert len(scope["rows"]["retrieval_vectors"]) == 3
    original, calls = module.text_complete, {}

    def counted(manifest):
        calls[id(manifest)] = calls.get(id(manifest), 0) + 1
        return original(manifest)

    monkeypatch.setattr(module, "text_complete", counted)
    module._validate_source(scope, source.db.data_dir, module._inventory(source.db.data_dir))
    assert calls and max(calls.values()) == 1
