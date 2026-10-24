"""Real SQLite contention defers work without consuming OCR or provider attempts."""

import json
import sqlite3
import time
from contextlib import closing

import pytest
from app.bootstrap import Service
from app.contexts.collection.application import collection as module
from app.shared.domain.values import now


def busy_error(path, snapshot=False):
    first = sqlite3.connect(path, timeout=0, isolation_level=None)
    second = sqlite3.connect(path, timeout=0, isolation_level=None)
    try:
        first.execute("PRAGMA journal_mode=WAL")
        first.execute("CREATE TABLE IF NOT EXISTS probe(value INTEGER)")
        if snapshot:
            first.execute("BEGIN")
            first.execute("SELECT * FROM probe").fetchall()
            second.execute("INSERT INTO probe VALUES(1)")
            try:
                first.execute("INSERT INTO probe VALUES(2)")
            except sqlite3.OperationalError as error:
                return error
        else:
            first.execute("BEGIN IMMEDIATE")
            try:
                second.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                return error
        pytest.fail("Expected a real SQLite contention error")
    finally:
        first.rollback()
        first.close()
        second.close()


@pytest.fixture
def prepared(tmp_path):
    service = Service(tmp_path / "target")
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("owner", "Test", None, None, 1, now()))
        blob = service.archive.blobs.put_bytes("owner", b"Unreviewed original text")
        doc = service.archive.store_original(
            conn,
            "owner",
            blob,
            "original.txt",
            "text/plain",
            "https://example.org/original.txt",
            collection="country:ID",
            level="primary",
            tags=[],
            kind_hint="law",
        )
        checkpoint = {
            "elements": [],
            "chunks": [],
            "pages": [{"page": 1, "status": "pending_ocr", "ocr": {"attempts": 2}}],
        }
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(checkpoint), doc["id"]))
    claim = service.collection.claim_processing(owner_id="owner", document_ids=[doc["id"]])
    assert claim
    with service.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET attempts=3 WHERE document_id=?", (doc["id"],))
    claim["attempts"] = 3
    return service, doc, claim, checkpoint


@pytest.mark.parametrize("phase", ["parse", "embed"])
@pytest.mark.parametrize("snapshot", [False, True])
def test_actual_busy_preserves_page_history_and_defers_exhausted_claim(prepared, tmp_path, phase, snapshot):
    service, doc, claim, checkpoint = prepared
    error = busy_error(tmp_path / "contended.sqlite3", snapshot)
    assert module._storage_busy(error)
    if snapshot:
        assert error.sqlite_errorcode == sqlite3.SQLITE_BUSY_SNAPSHOT
    claim["phase"] = phase
    with service.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET phase=? WHERE document_id=?", (phase, doc["id"]))
    before = time.time()
    service.collection._failure(claim, error, processing=True)
    with closing(service.db.connect()) as conn:
        job = dict(conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (doc["id"],)).fetchone())
        current = service.own_document(conn, "owner", doc["id"])
    assert job["status"] == "queued" and job["attempts"] == 0 and job["available_at"] >= before + 5
    assert job["phase"] == phase and job["lease_token"] is job["lease_until"] is None
    assert job["error"] == "本地资料存储繁忙，任务稍后继续处理"
    assert json.loads(current["manifest"]) == checkpoint and current["content_hash"] == doc["content_hash"]


@pytest.mark.parametrize("kind", ["message", "other_sqlite"])
def test_nonbusy_error_still_fails_exhausted_claim(prepared, kind):
    service, doc, claim, _ = prepared
    error = ValueError("database is locked")
    if kind == "other_sqlite":
        with sqlite3.connect(":memory:") as conn:
            try:
                conn.execute("SELECT * FROM nonexistent")
            except sqlite3.OperationalError as actual:
                error = actual
    assert not module._storage_busy(error)
    service.collection._failure(claim, error, processing=True)
    with closing(service.db.connect()) as conn:
        job = conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (doc["id"],)).fetchone()
    assert job["status"] == "failed" and job["attempts"] == 3
