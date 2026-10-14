"""Stopped scoped workers only defer their own unchanged expired claims."""

import json
import time
from contextlib import closing

import pytest

from app.collection import CollectionWorkers
from app.db import dump, now
from app.service import Service


@pytest.fixture
def prepared(tmp_path):
    service = Service(tmp_path)
    documents = []
    with service.db.transaction() as conn:
        for owner in ("owner", "other"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 0, now()))
            blob = service.archive.blobs.put_bytes(owner, ("Source " + owner).encode())
            doc = dict(
                service.archive.store_original(
                    conn,
                    owner,
                    blob,
                    owner + ".txt",
                    "text/plain",
                    "https://example.org/" + owner,
                    collection="country:ID",
                    level="primary",
                    tags=[],
                )
            )
            manifest = {
                "source_hash": doc["content_hash"],
                "elements": [{"element_key": "e1", "content_text": "Stored source text"}],
                "chunks": [{"id": "c1", "text": "Stored source text", "locator": {"page": 1}}],
            }
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
            service.index.sync_document(conn, doc["id"], manifest)
            batch = service.index.pending(conn, doc["id"], "guard-test-model", limit=10)
            service.index.store_vectors(conn, doc["id"], "guard-test-model", batch, [[1.0, 0.0]])
            manifest.update(
                collection_import={"guard": "original-before-paid-guard"},
                quality={"complete": False},
                pages=[
                    {
                        "page": 1,
                        "source_hash": doc["content_hash"],
                        "native_text": "Original native text",
                        "text": "Already paid transcription",
                        "status": "pending_ocr",
                        "ocr": {"attempts": 2},
                    }
                ],
            )
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
            documents.append(doc)
    claim = service.collection.claim_processing(owner_id="owner", document_ids=[documents[0]["id"]])
    other = service.collection.claim_processing(owner_id="other", document_ids=[documents[1]["id"]])
    with service.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET lease_until=0 WHERE document_id=?", (claim["document_id"],))
    return service, documents[0], claim, other


def payload(service):
    with closing(service.db.connect()) as conn:
        return {
            "documents": [
                dict(r)
                for r in conn.execute(
                    "SELECT id,owner_id,role,version,content_hash,size,storage_path,source_url,manifest FROM documents ORDER BY id"
                )
            ],
            "metadata": [
                dict(r) for r in conn.execute("SELECT * FROM document_metadata ORDER BY document_id")
            ],
            "classifications": [
                dict(r)
                for r in conn.execute("SELECT * FROM legal_classifications ORDER BY document_id,collection")
            ],
            "chunks": [dict(r) for r in conn.execute("SELECT * FROM retrieval_chunks ORDER BY rowid")],
            "vectors": [
                dict(r) for r in conn.execute("SELECT * FROM retrieval_vectors ORDER BY chunk_rowid")
            ],
        }


def jobs(service):
    with closing(service.db.connect()) as conn:
        return {r["document_id"]: dict(r) for r in conn.execute("SELECT * FROM processing_jobs")}


def test_recovery_claims_and_defers_only_exact_expired_owned_original(prepared):
    service, doc, claim, other = prepared
    before, queue_before = payload(service), jobs(service)
    assert service.collection.recover_expired_processing_claim(claim)
    after = jobs(service)
    assert payload(service) == before
    assert after[other["document_id"]] == queue_before[other["document_id"]]
    assert after[doc["id"]]["status"] == "queued" and after[doc["id"]]["attempts"] == 0
    assert after[doc["id"]]["lease_token"] is after[doc["id"]]["lease_until"] is None
    assert json.loads(before["documents"][0]["manifest"])["pages"][0]["ocr"]["attempts"] == 2


def test_old_shutdown_token_cannot_reclaim_a_new_worker_token(prepared):
    service, doc, old, _ = prepared
    current = service.collection.claim_processing(owner_id="owner", document_ids=[doc["id"]])
    assert current["lease_token"] != old["lease_token"]
    before, queue_before = payload(service), jobs(service)
    assert not service.collection.recover_expired_processing_claim(old)
    assert payload(service) == before and jobs(service) == queue_before


def test_newly_recovered_token_taken_over_before_failure_is_not_released(prepared, monkeypatch):
    service, doc, old, _ = prepared
    before = payload(service)
    original = service.collection._failure

    def replacement(claim, error, *, processing):
        with service.db.transaction() as conn:
            conn.execute(
                "UPDATE processing_jobs SET lease_token='new-worker',lease_until=? WHERE document_id=?",
                (time.time() + 120, doc["id"]),
            )
        original(claim, error, processing=processing)

    monkeypatch.setattr(service.collection, "_failure", replacement)
    assert not service.collection.recover_expired_processing_claim(old)
    assert jobs(service)[doc["id"]]["lease_token"] == "new-worker"
    assert jobs(service)[doc["id"]]["status"] == "processing"
    assert payload(service) == before


@pytest.mark.parametrize(
    "field,value", [("role", "secondary"), ("version", 2), ("content_hash", "a" * 64), ("owner_id", "other")]
)
def test_recovery_source_role_version_and_owner_fences_refuse_changed_original(prepared, field, value):
    service, doc, old, _ = prepared
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET " + field + "=? WHERE id=?", (value, doc["id"]))
    before, queue_before = payload(service), jobs(service)
    assert not service.collection.recover_expired_processing_claim(old)
    assert payload(service) == before and jobs(service) == queue_before


def test_live_owned_claim_and_unscoped_recovery_are_not_cancelled(prepared):
    service, doc, old, _ = prepared
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE processing_jobs SET lease_until=? WHERE document_id=?", (time.time() + 120, doc["id"])
        )
    before = jobs(service)
    assert not service.collection.recover_expired_processing_claim(old)
    assert jobs(service) == before
    with pytest.raises(ValueError, match="限定一个"):
        service.collection.claim_processing(expected_expired_claim=old)
    assert jobs(service) == before


def test_scoped_workers_retain_latest_owned_claim_and_defer_after_future_drain(prepared, monkeypatch):
    service, doc, old, other = prepared
    before = payload(service)
    # Reclaim the expired fixture via the ordinary queue before the worker run.
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE processing_jobs SET status='queued',lease_token=NULL,lease_until=NULL,attempts=0,available_at=0 WHERE document_id=?",
            (doc["id"],),
        )
    worker = CollectionWorkers(
        service.collection, processing_scope=("owner", [doc["id"]]), fetch_enabled=False
    )

    def stopped(claim):
        with service.db.transaction() as conn:
            conn.execute(
                "UPDATE processing_jobs SET lease_until=0 WHERE document_id=?", (claim["document_id"],)
            )
        worker.stop_event.set()

    monkeypatch.setattr(service.collection, "process_item", stopped)
    worker.start()
    worker.thread.join(timeout=10)
    assert not worker.thread.is_alive()
    assert service.collection.worker_health["status"] == "stopped"
    assert len(worker.owned_processing_claims) == 1
    assert worker.owned_processing_claims[doc["id"]]["lease_token"] != old["lease_token"]
    assert jobs(service)[doc["id"]]["status"] == "queued"
    assert jobs(service)[other["document_id"]]["lease_token"] == other["lease_token"]
    assert payload(service) == before
