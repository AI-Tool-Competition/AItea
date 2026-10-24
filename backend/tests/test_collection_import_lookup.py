"""One transaction-scoped provenance scan without weakening import isolation."""

import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
import test_collection_import as import_fixtures
from app.bootstrap import Service
from app.contexts.ingestion.application.collection_import import (
    CollectionImportError,
    _target_import_ids,
    import_collected_resources,
)
from app.shared.domain.values import dump, now
from app.shared.infrastructure.persistence.database import Database
from test_collection_import import facts, original, transfer

pair = import_fixtures.pair


def test_batch_retry_scans_provenance_once_inside_write_transaction(pair, monkeypatch):
    src, dst = pair
    for number in range(6):
        original(src, f"batch-{number}")
    queries = []
    connect = Database.connect

    def traced_connect(db):
        conn = connect(db)
        if db.path == dst.db.path:
            conn.set_trace_callback(lambda sql: queries.append((sql, conn.in_transaction)))
        return conn

    monkeypatch.setattr(Database, "connect", traced_connect)

    def assert_one_scan():
        scans = [(sql, active) for sql, active in queries if "$.collection_import.source_owner_sha256" in sql]
        assert len(scans) == 1
        sql, active = scans[0]
        assert active, "The provenance snapshot must be taken under the target write transaction."
        assert "SELECT *" not in sql
        queries.clear()

    created = transfer(src, dst, apply=True)
    assert created["target_writes"] == 6
    assert_one_scan()
    before = facts(dst)
    retried = transfer(src, dst, apply=True)
    assert retried["unchanged_documents"] == 6
    assert retried["document_versions"] == created["document_versions"]
    assert_one_scan()
    assert facts(dst) == before


def test_same_source_imported_into_two_owners_retries_only_its_own_rows(pair):
    src, dst = pair
    doc = original(src)
    results = {}
    for owner in ("target", "unrelated"):
        results[owner] = import_collected_resources(
            src.db.data_dir, dst.db.data_dir, "source", owner, apply=True
        )
    assert (
        results["target"]["document_versions"][doc["id"]]["document_id"]
        != results["unrelated"]["document_versions"][doc["id"]]["document_id"]
    )
    before = facts(dst)
    for owner in ("target", "unrelated"):
        retry = import_collected_resources(src.db.data_dir, dst.db.data_dir, "source", owner, apply=True)
        assert retry["unchanged_documents"] == 1
        assert retry["document_versions"] == results[owner]["document_versions"]
    assert facts(dst) == before


def test_same_document_id_from_distinct_sources_creates_independent_imports(pair, tmp_path, monkeypatch):
    src, dst = pair
    doc = original(src)
    first = transfer(src, dst, apply=True)
    other = Service(tmp_path / "other-source")
    blob = other.archive.blobs.put_bytes("different-source", b"Independent original with the same source ID")
    with monkeypatch.context() as patch:
        patch.setattr("app.contexts.documents.application.archive.uid", lambda: doc["id"])
        with other.db.transaction() as conn:
            conn.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?)",
                ("different-source", "different-source", None, None, 1, now()),
            )
            row = other.archive.store_original(
                conn,
                "different-source",
                blob,
                "same-id.txt",
                "text/plain",
                "https://official.example/same-id",
                collection="country:ID",
                level="primary",
                tags=["kind:law"],
                kind_hint="law",
            )
            assert row["id"] == doc["id"]
            conn.execute(
                "UPDATE documents SET manifest=? WHERE id=?",
                (
                    dump(
                        {
                            "source_hash": blob.digest,
                            "elements": [],
                            "chunks": [],
                            "quality": {"complete": False},
                        }
                    ),
                    row["id"],
                ),
            )
    second = import_collected_resources(
        other.db.data_dir, dst.db.data_dir, "different-source", "target", apply=True
    )
    assert second["target_writes"] == 1
    assert second["document_versions"] != first["document_versions"]
    before = facts(dst)
    assert transfer(src, dst, apply=True)["document_versions"] == first["document_versions"]
    retry = import_collected_resources(
        other.db.data_dir, dst.db.data_dir, "different-source", "target", apply=True
    )
    assert retry["document_versions"] == second["document_versions"]
    assert retry["unchanged_documents"] == 1
    assert facts(dst) == before


def test_lookup_separates_source_owners_and_ignores_unselected_or_invalid_ids():
    owner_hash = hashlib.sha256(b"source").hexdigest()
    other_hash = hashlib.sha256(b"different-source").hexdigest()
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE documents(id TEXT, owner_id TEXT, manifest TEXT)")
        for ident, owner, source_hash, source_id in (
            ("other-owner", "unrelated", owner_hash, "same-id"),
            ("different-source", "target", other_hash, "same-id"),
            ("not-selected", "target", owner_hash, "another-id"),
            ("invalid-object", "target", owner_hash, {"id": "same-id"}),
            ("invalid-array", "target", owner_hash, ["same-id"]),
            ("invalid-number", "target", owner_hash, 123),
            ("selected", "target", owner_hash, "same-id"),
            ("duplicate", "target", owner_hash, "same-id"),
        ):
            conn.execute(
                "INSERT INTO documents VALUES(?,?,?)",
                (
                    ident,
                    owner,
                    dump(
                        {
                            "collection_import": {
                                "source_owner_sha256": source_hash,
                                "source_document_id": source_id,
                            }
                        }
                    ),
                ),
            )
        conn.execute("INSERT INTO documents VALUES('missing', 'target', '{}')")
        assert _target_import_ids(conn, "target", owner_hash, ["same-id", "123"]) == {"same-id": "selected"}
        assert _target_import_ids(conn, "target", other_hash, ["same-id"]) == {"same-id": "different-source"}
        assert _target_import_ids(conn, "unrelated", owner_hash, ["same-id"]) == {"same-id": "other-owner"}


def test_prefetch_does_not_bypass_guard_or_allow_partial_batch_refresh(pair):
    src, dst = pair
    docs = [original(src, f"guard-{number}") for number in range(2)]
    imported = transfer(src, dst, apply=True)
    damaged = imported["document_versions"][docs[-1]["id"]]["document_id"]
    with dst.db.transaction() as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (damaged,)).fetchone()[0]
        )
        manifest["local_edit"] = "must not be overwritten"
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), damaged))
    before = facts(dst)
    with pytest.raises(CollectionImportError, match="target_changed_since_import"):
        transfer(src, dst, apply=True, refresh=True)
    assert facts(dst) == before
