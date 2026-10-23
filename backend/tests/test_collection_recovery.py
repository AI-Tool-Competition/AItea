"""Recovery is durable, source-fenced, bounded, and never guesses paid outcomes."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import sqlite3
import time

import pytest

from app.collection_recovery import BASE_RECOVERY_SECONDS, CollectionRecovery, Decision, classify_processing
from app.db import dump, now
from app.ingestion import DownloadError, EmbeddingServiceError
from app.scheduler import ScheduledCollector
from app.service import AppError, Service
from test_ingestion_quality import make_pdf
from test_pdf_ocr_reuse import register, stored


@pytest.fixture
def service(tmp_path):
    result = Service(tmp_path / "owned")
    with result.db.transaction() as conn:
        for owner in ("owner", "other"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 1, now()))
    return result


def fetch_job(service, key="first", owner="owner"):
    source = service.save_source(
        owner,
        {
            "name": "Official",
            "url": "https://example.org/law.txt",
            "enabled": False,
            "interval_hours": 24,
            "tags": [],
            "plan": {"discover_attachments": False},
        },
    )
    run = service.collection.enqueue(owner, source["id"], {"client_request_id": key})
    return source, run


def failed_fetch(service, *, retryable=True, retry_after=None):
    source, run = fetch_job(service)
    for _ in range(3 if retryable else 1):
        claim = service.collection.claim_fetch()
        assert claim
        service.collection._failure(
            claim,
            DownloadError("source unavailable", retryable=retryable, retry_after=retry_after),
            processing=False,
        )
        with service.db.transaction() as conn:
            if not retry_after:
                conn.execute("UPDATE collection_items SET available_at=0 WHERE id=?", (claim["id"],))
    return source, run, claim


def rows(service, table):
    with closing(service.db.connect()) as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def due(service):
    with service.db.transaction() as conn:
        conn.execute("UPDATE collection_recovery_jobs SET available_at=0 WHERE status='waiting'")
        conn.execute("UPDATE collection_items SET available_at=0 WHERE status='failed'")
        conn.execute("UPDATE processing_jobs SET available_at=0 WHERE status='failed'")


def test_empty_and_existing18_migration_preserves_all_business_rows(service):
    fetch_job(service)
    before = {
        name: rows(service, name) for name in ("users", "sources", "collection_runs", "collection_items")
    }
    with service.db.transaction() as conn:
        conn.execute("DROP TABLE collection_recovery_events")
        conn.execute("DROP TABLE collection_recovery_jobs")
        conn.execute("DELETE FROM schema_migrations WHERE version=19")
    restarted = Service(service.db.data_dir)
    assert all(rows(restarted, name) == values for name, values in before.items())
    assert rows(restarted, "collection_recovery_jobs") == []
    assert [row["version"] for row in rows(restarted, "schema_migrations")].count(19) == 1
    CollectionRecovery(restarted.collection)
    assert [row["version"] for row in rows(restarted, "schema_migrations")].count(19) == 1


def test_marker_only_migration_is_rejected(service):
    with service.db.transaction() as conn:
        conn.execute("DROP TABLE collection_recovery_events")
    with pytest.raises(sqlite3.OperationalError):
        CollectionRecovery(service.collection)


def test_exhausted_fetch_restarts_once_with_history_and_completes(service, monkeypatch):
    from app import ingestion

    _source, _run, claim = failed_fetch(service)
    record = rows(service, "collection_recovery_jobs")[0]
    assert record["status"] == "waiting" and record["failure_count"] == 3
    assert record["available_at"] >= time.time() + BASE_RECOVERY_SECONDS - 3
    assert service.collection.recovery.run_due()["requeued"] == 0
    restarted = Service(service.db.data_dir)
    due(restarted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: restarted.collection.recovery.run_due(), range(2)))
    assert sum(result["requeued"] for result in results) == 1
    events = rows(restarted, "collection_recovery_events")
    assert [event["event"] for event in events] == ["attempt_failed"] * 3 + ["requeued"]
    assert json.loads(events[-1]["detail"])["previous_queue_attempts"] == 3
    assert rows(restarted, "collection_items")[0]["attempts"] == 0

    def download(url, *, consume, **_kwargs):
        return {
            "url": url,
            "filename": "law.txt",
            "mime_type": "text/plain",
            "blob": consume(iter([b"Official text"])),
        }

    monkeypatch.setattr(ingestion, "_download", download)
    restarted.collection.fetch_item(restarted.collection.claim_fetch())
    assert rows(restarted, "collection_items")[0]["status"] == "stored"
    assert rows(restarted, "collection_recovery_jobs")[0]["status"] == "completed"
    assert rows(restarted, "collection_recovery_events")[-1]["event"] == "completed"
    assert len(rows(restarted, "documents")) == 1
    assert rows(restarted, "documents")[0]["owner_id"] == claim["owner_id"]


@pytest.mark.parametrize("change", ["permanent", "source", "cancelled", "owner"])
def test_unsafe_or_cancelled_fetch_never_requeues(service, change):
    source, run, _claim = failed_fetch(service, retryable=change != "permanent")
    with service.db.transaction() as conn:
        if change == "source":
            conn.execute("UPDATE sources SET url='https://example.org/changed' WHERE id=?", (source["id"],))
        elif change == "cancelled":
            conn.execute("UPDATE collection_runs SET status='cancelled' WHERE id=?", (run["id"],))
        elif change == "owner":
            conn.execute("UPDATE collection_recovery_jobs SET owner_id='other'")
    due(service)
    before = rows(service, "collection_items")
    assert service.collection.recovery.run_due()["requeued"] == 0
    assert rows(service, "collection_items") == before
    assert rows(service, "collection_recovery_jobs")[0]["status"] == "blocked"


def test_retry_after_is_persistent_and_not_shortened(service):
    _source, _run = fetch_job(service)
    claim = service.collection.claim_fetch()
    # Exhaust the bounded burst without making any actual request.
    with service.db.transaction() as conn:
        conn.execute("UPDATE collection_items SET attempts=3 WHERE id=?", (claim["id"],))
    claim["attempts"] = 3
    before = time.time()
    service.collection._failure(
        claim, DownloadError("rate limited", retryable=True, retry_after=7200), processing=False
    )
    record = rows(service, "collection_recovery_jobs")[0]
    assert record["available_at"] >= before + 7200
    assert Service(service.db.data_dir).collection.recovery.run_due()["requeued"] == 0
    assert rows(service, "collection_host_cooldowns")[0]["available_at"] >= before + 7200


@pytest.mark.parametrize(
    "metadata",
    [
        {"attempts": 1, "request_id": "lost"},
        {"attempts": 0, "request_outcome": "unknown_transport_result"},
        {"attempts": 1, "http_status": 504},
        {"attempts": 1, "http_status": 408},
    ],
)
def test_processing_unknown_paid_intents_remain_blocked(metadata):
    manifest = {"pages": [{"page": 1, "status": "pending_ocr", "ocr": metadata}]}
    assert classify_processing(manifest, phase="parse", retryable=True).reason == "paid_result_unknown"
    assert not classify_processing(manifest, phase="parse", retryable=True).retryable


def test_embed_last_rejection_does_not_prove_entire_multibatch_call_was_unpaid():
    assert classify_processing({}, phase="embed", retryable=True).reason == "paid_result_unknown"


def test_only_complete_legacy_mineru_receipts_blocked_on_chart_content_are_refreshable(monkeypatch):
    from app import mineru

    source_hash = hashlib.sha256(b"verified source").hexdigest()
    text = "Section 1. Official text."
    manifest = {
        "parser": "mineru",
        "source_hash": source_hash,
        "pages": [
            {
                "page": 1,
                "status": "ocr_complete",
                "text": text,
                "reasons": [],
                "ocr": {
                    "provider": "mineru",
                    "complete": True,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
            },
            {
                "page": 2,
                "status": "ocr_uncertain",
                "text": "",
                "reasons": ["chart_content_missing"],
                "ocr": {
                    "provider": "mineru",
                    "complete": False,
                    "uncertain_spans": ["chart_content_missing"],
                    "text_sha256": hashlib.sha256(b"").hexdigest(),
                },
            },
        ],
        "remote_parse": {
            "provider": "mineru",
            "state": "done",
            "source_hash": source_hash,
            "page_count": 2,
            "parts": [
                {
                    "task_id": "completed-task",
                    "source_hash": source_hash,
                    "first_page": 1,
                    "last_page": 2,
                    "result_blob": {"content_hash": "a" * 64},
                }
            ],
        },
    }
    monkeypatch.setattr(mineru, "manifest_valid", lambda _manifest: True)
    decision = classify_processing(manifest, phase="parse")
    assert decision == Decision("mineru_chart_refresh", True)

    mixed = json.loads(json.dumps(manifest))
    mixed["pages"][1]["reasons"].append("native_visibility_review")
    assert classify_processing(mixed, phase="parse").reason == "quality_review"
    upgraded = json.loads(json.dumps(manifest))
    upgraded["mineru_chart_inventory_version"] = "mineru-chart-assets-v1"
    assert classify_processing(upgraded, phase="parse").reason == "quality_review"


def test_blocked_legacy_chart_review_requeues_once_with_audited_receipt(service, monkeypatch):
    from app import mineru, vision

    monkeypatch.setattr(mineru, "manifest_valid", lambda _manifest: True)
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    content = make_pdf([("Official source", False)])
    doc = register(service, content, key="legacy-mineru-chart")
    source_hash = doc["content_hash"]
    text = "Section 1. Official text."
    manifest = {
        "parser": "mineru",
        "source_hash": source_hash,
        "pages": [
            {
                "page": 1,
                "status": "ocr_uncertain",
                "text": text,
                "reasons": ["chart_content_missing"],
                "ocr": {
                    "provider": "mineru",
                    "complete": False,
                    "uncertain_spans": ["chart_content_missing"],
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
            }
        ],
        "remote_parse": {
            "provider": "mineru",
            "state": "done",
            "source_hash": source_hash,
            "page_count": 1,
            "parts": [
                {
                    "task_id": "completed-task",
                    "source_hash": source_hash,
                    "first_page": 1,
                    "last_page": 1,
                    "result_blob": {"content_hash": "b" * 64},
                }
            ],
        },
    }
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=?,status='failed' WHERE id=?", (dump(manifest), doc["id"]))
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='failed',attempts=3,available_at=0 WHERE document_id=?",
            (doc["id"],),
        )
        claim = {"document_id": doc["id"], "owner_id": "owner", "phase": "parse", "attempts": 3}
        service.collection.recovery.record_failure(
            conn,
            claim,
            processing=True,
            decision=Decision("quality_review"),
            exhausted=True,
            error_type="legacy_quality_review",
        )
    result = service.collection.recovery.run_due()
    assert result["chart_refreshes"] == 1
    assert result["requeued"] == 1
    assert rows(service, "processing_jobs")[0]["status"] == "queued"
    record = rows(service, "collection_recovery_jobs")[0]
    assert record["status"] == "queued"
    assert record["reason"] == "mineru_chart_refresh"
    event = rows(service, "collection_recovery_events")[-1]
    assert event["event"] == "requeued"
    assert json.loads(event["detail"])["previous_queue_attempts"] == 3


def test_native_processing_continues_multiple_bursts_without_losing_history(service, monkeypatch):
    monkeypatch.setenv("VISION_OCR_ENABLED", "false")
    for key in ("BASE_URL", "API_KEY", "MODEL"):
        monkeypatch.setenv("EMBEDDING_" + key, "unit-test-only")
    doc = register(service, make_pdf([("Article 1. Keep the original document.", False)]), key="native")
    original = stored(service, doc)
    for cycle in range(2):
        for attempt in range(1, 4):
            claim = service.collection.claim_processing(owner_id="owner", document_ids=[doc["id"]])
            assert claim and claim["attempts"] == attempt
            service.collection._failure(claim, RuntimeError("local worker unavailable"), processing=True)
            with service.db.transaction() as conn:
                conn.execute("UPDATE processing_jobs SET available_at=0 WHERE document_id=?", (doc["id"],))
        record = rows(service, "collection_recovery_jobs")[0]
        assert record["reason"] == "transient_processing" and record["status"] == "waiting"
        assert record["failure_count"] == (cycle + 1) * 3
        assert record["available_at"] >= time.time() + BASE_RECOVERY_SECONDS * 2**cycle - 3
        due(service)
        service = Service(service.db.data_dir)
        assert service.collection.recovery.run_due()["requeued"] == 1
        assert stored(service, doc)["manifest"] == original["manifest"]
    claim = service.collection.claim_processing(owner_id="owner", document_ids=[doc["id"]])
    service.collection.process_item(claim)
    events = rows(service, "collection_recovery_events")
    assert [event["event"] for event in events].count("attempt_failed") == 6
    assert [event["event"] for event in events].count("requeued") == 2
    assert events[-1]["event"] == "completed"
    assert rows(service, "processing_jobs")[0]["phase"] == "embed"
    assert rows(service, "retrieval_vectors") == []
    assert stored(service, doc)["content_hash"] == original["content_hash"]


def test_unknown_embedding_is_held_on_first_failure_and_ordinary_retry(service):
    doc = register(service, make_pdf([("Official text", False)]), key="embed")
    with service.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET phase='embed' WHERE document_id=?", (doc["id"],))
    claim = service.collection.claim_processing(owner_id="owner", document_ids=[doc["id"]])
    service.collection._failure(
        claim,
        EmbeddingServiceError(
            retryable=True, http_status=504, request_outcome_unknown=True, request_sent=True
        ),
        processing=True,
    )
    before = rows(service, "processing_jobs")
    assert before[0]["status"] == "failed" and before[0]["attempts"] == 1
    assert rows(service, "collection_recovery_jobs")[0]["reason"] == "paid_result_unknown"
    with service.db.transaction() as conn:
        with pytest.raises(AppError) as raised:
            service.collection.queue_processing(conn, doc["id"], "owner", retry_ocr=True)
    assert raised.value.code == "embedding_outcome_review_required"
    due(service)
    assert service.collection.recovery.run_due()["requeued"] == 0
    assert rows(service, "processing_jobs")[0]["status"] == "failed"
    assert rows(service, "retrieval_vectors") == []


def rejected_page(service, *, status=429):
    doc = register(service, make_pdf([("First", True), ("Second", True)]), key="rejected")
    manifest = stored(service, doc)["manifest"]
    first, page = manifest["pages"]
    first.update(status="ocr_complete", ocr={"attempts": 1, "text": "First", "complete": True})
    page.update(status="ocr_failed", error="Explicit rate-limit rejection")
    page["ocr"] = {
        "attempts": 3,
        "request_id": "rejected-request",
        "source_hash": doc["content_hash"],
        "page": 2,
        "http_status": status,
        "image_sha256": "a" * 64,
        "prompt_version": "legal-page-transcription-v3",
        "requested_model": "test-model",
    }
    evidence = {
        "format": "ocr-error-response-v1",
        "source_hash": doc["content_hash"],
        "page": 2,
        "request_id": "rejected-request",
        "attempt": 3,
        "image_sha256": "a" * 64,
        "prompt_version": "legal-page-transcription-v3",
        "requested_model": "test-model",
        "http_status": status,
        "response_base64": "",
        "size": 0,
        "content_hash": hashlib.sha256(b"").hexdigest(),
    }
    raw = dump(evidence).encode()
    blob = service.archive.blobs.put_bytes("owner", raw)
    page["ocr"]["error_response"] = {
        key: value for key, value in evidence.items() if key != "response_base64"
    }
    page["ocr"]["error_response"]["blob"] = {
        "content_hash": blob.digest,
        "size": blob.size,
        "storage_path": str(blob.path.relative_to(service.db.data_dir)),
        "mime_type": "application/json",
    }
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
        conn.execute("UPDATE processing_jobs SET status='failed' WHERE document_id=?", (doc["id"],))
    return doc, manifest


def test_exact_429_page_retry_preserves_success_original_and_complete_old_evidence(service):
    doc, before = rejected_page(service)
    original = dict(rows(service, "documents")[0])
    assert service.collection.recovery.run_due()["adopted"] == 1
    record = rows(service, "collection_recovery_jobs")[0]
    assert record["reason"] == "ocr_rate_limited" and record["status"] == "waiting"
    due(service)
    assert service.collection.recovery.run_due()["requeued"] == 1
    after = stored(service, doc)["manifest"]
    assert after["pages"][0] == before["pages"][0]
    assert after["pages"][1]["status"] == "pending_ocr" and after["pages"][1]["ocr"] == {"attempts": 0}
    history = after["pages"][1]["ocr_retry_cycles"][-1]
    assert history["previous_page"] == before["pages"][1]
    raw = (service.db.data_dir / history["blob"]["storage_path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == history["blob"]["content_hash"]
    assert json.loads(raw)["previous_page"] == before["pages"][1]
    assert {
        key: rows(service, "documents")[0][key]
        for key in original
        if key not in {"manifest", "status", "error"}
    } == {key: value for key, value in original.items() if key not in {"manifest", "status", "error"}}
    assert rows(service, "retrieval_vectors") == []
    assert rows(service, "legal_relations") == []


@pytest.mark.parametrize("problem", ["source", "bad_receipt", "unknown", "quality", "budget", "request"])
def test_ocr_recovery_rechecks_current_evidence_and_never_blanket_resets(service, problem):
    doc, manifest = rejected_page(service)
    service.collection.recovery.run_due()
    page = manifest["pages"][1]
    if problem == "source":
        page["source_hash"] = "f" * 64
    elif problem == "bad_receipt":
        page["ocr"]["error_response"]["blob"]["content_hash"] = "f" * 64
    elif problem == "unknown":
        page["ocr"]["request_outcome"] = "unknown_transport_result"
    elif problem == "quality":
        page["status"] = "ocr_uncertain"
    elif problem == "request":
        page["ocr"]["request_id"] = "different-request-after-plan"
    else:
        page["ocr_retry_cycles"] = [{}, {}, {}]
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
    due(service)
    assert service.collection.recovery.run_due()["requeued"] == 0
    assert stored(service, doc)["manifest"] == manifest
    assert rows(service, "collection_recovery_jobs")[0]["status"] == "blocked"


def test_retry_plan_cannot_expand_to_another_physical_page(service):
    from copy import deepcopy

    doc, manifest = rejected_page(service)
    service.collection.recovery.run_due()
    manifest["pages"][0] = deepcopy(manifest["pages"][1])
    manifest["pages"][0]["page"] = 1
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
    due(service)
    assert service.collection.recovery.run_due()["blocked"] == 1
    assert rows(service, "collection_recovery_jobs")[0]["reason"] == "checkpoint_changed"
    assert stored(service, doc)["manifest"] == manifest


def test_full_backup_restores_429_checkpoint_history_and_error_cas(service, tmp_path):
    from app.data_backup import create_backup, restore_backup, verify_backup

    doc, before = rejected_page(service)
    service.collection.recovery.run_due()
    due(service)
    assert service.collection.recovery.run_due()["requeued"] == 1
    after = stored(service, doc)["manifest"]
    backup = tmp_path / "ocr-backup"
    create_backup(service.db.data_dir, backup)
    assert verify_backup(backup)["schema_version"] == 19
    restore_backup(backup, tmp_path / "ocr-restored")
    restored = Service(tmp_path / "ocr-restored")
    assert stored(restored, doc)["manifest"] == after
    assert rows(restored, "collection_recovery_events") == rows(service, "collection_recovery_events")
    history = after["pages"][1]["ocr_retry_cycles"][-1]
    for ref in (history["blob"], before["pages"][1]["ocr"]["error_response"]["blob"]):
        raw = (restored.db.data_dir / ref["storage_path"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == ref["content_hash"]


def test_backup_restore_preserves_recovery_events_and_due_queue(service, tmp_path):
    from app.data_backup import create_backup, verify_backup, restore_backup

    failed_fetch(service)
    before = rows(service, "collection_recovery_events")
    backup = tmp_path / "backup"
    create_backup(service.db.data_dir, backup)
    assert verify_backup(backup)["schema_version"] == 19
    restore_backup(backup, tmp_path / "restored")
    restored = Service(tmp_path / "restored")
    assert rows(restored, "collection_recovery_events") == before
    with restored.db.transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("DELETE FROM collection_recovery_events")
    due(restored)
    assert restored.collection.recovery.run_due()["requeued"] == 1


def test_scheduler_routes_managed_source_to_published_discovery_once(service):
    plan = service.checklists.definition("indonesia-20261003")
    service.checklists.configure("owner", plan["id"], plan["version"])
    with service.db.transaction() as conn:
        binding = conn.execute(
            "SELECT * FROM checklist_bindings WHERE owner_id='owner' AND item_id='1.3'"
        ).fetchone()
        conn.execute("UPDATE sources SET enabled=1 WHERE id=?", (binding["source_id"],))
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: ScheduledCollector(service).run_once(), range(2)))
    runs = rows(service, "collection_runs")
    assert len(runs) == 1
    checklist = json.loads(runs[0]["plan"])["checklist"]
    assert checklist["kind"] == "discovery" and checklist["definition"]["plan"]["version"] == plan["version"]
    assert rows(service, "documents") == []
    assert all(item["purpose"] in {"catalog", "detail"} for item in rows(service, "collection_items"))


def test_scheduler_does_not_grant_conditional_permission_or_spin_on_409(service):
    plan = service.checklists.definition("singapore-demo-20261009")
    service.checklists.configure("owner", plan["id"], plan["version"])
    with service.db.transaction() as conn:
        binding = conn.execute(
            "SELECT * FROM checklist_bindings WHERE owner_id='owner' AND item_id='4.1'"
        ).fetchone()
        conn.execute("UPDATE sources SET enabled=1 WHERE id=?", (binding["source_id"],))
    collector = ScheduledCollector(service)
    assert collector.run_once()["failed"] == 0
    assert service.due_sources() == [] and rows(service, "collection_runs") == []
    source = next(row for row in rows(service, "sources") if row["id"] == binding["source_id"])
    assert source["last_attempt_at"] and source["last_error"]
