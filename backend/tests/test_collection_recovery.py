"""Recovery is durable, source-fenced, and preserves paid evidence."""

import base64
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from app.bootstrap import Service
from app.contexts.ingestion.application.collection_recovery import (
    BASE_RECOVERY_SECONDS,
    CollectionRecovery,
    Decision,
    classify_processing,
)
from app.contexts.documents.infrastructure.parsers.native_parser import (
    _parse_pdf,
    _parsed_result,
    parse_file,
)
from app.contexts.documents.infrastructure.providers import native_asset_ocr, vision
from app.contexts.documents.infrastructure.recovery.ocr_error_evidence import (
    preserve_previous_asset_attempt,
    persist_response_evidence,
)
from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import bind_linked_image_bounded
from app.shared.domain.values import dump, now
from app.shared.infrastructure.http.download import DownloadError
from app.shared.infrastructure.operations.scheduler import ScheduledCollector
from support.ingestion_quality import make_pdf
from support.native_asset_ocr import raster
from support.pdf_ocr_reuse import register


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


def bound_failed_vision_page(service, page, *, owner="owner"):
    request = {
        "request_id": "vision-request-1",
        "attempts": 1,
        "image_sha256": "a" * 64,
        "prompt_version": "legal-page-transcription-v3",
        "requested_model": "vision-test-model",
    }
    page["ocr"] = {"provider": "vision", **request}
    response = b'{"choices":[]}'
    evidence = {
        "format": "ocr-error-response-v1",
        "error_code": "invalid_provider_choices",
        "payload_kind": "provider_response",
        "request_sha256": hashlib.sha256(b"request").hexdigest(),
        "source_hash": page["source_hash"],
        "page": page["page"],
        "request_id": request["request_id"],
        "attempt": request["attempts"],
        "image_sha256": request["image_sha256"],
        "prompt_version": request["prompt_version"],
        "requested_model": request["requested_model"],
        "response_base64": base64.b64encode(response).decode("ascii"),
        "content_hash": hashlib.sha256(response).hexdigest(),
        "size": len(response),
        "observed_bytes": len(response),
        "capture_truncated": False,
        "response_complete": True,
    }
    reference = persist_response_evidence(
        service.archive.blobs,
        owner,
        evidence,
        source_hash=page["source_hash"],
        request_page=page,
    )
    page["ocr"]["error_response"] = {
        **{key: value for key, value in evidence.items() if key != "response_base64"},
        "blob": reference,
    }
    return page


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
    from app.shared.infrastructure.http import download as ingestion_download

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

    monkeypatch.setattr(ingestion_download, "_download", download)
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
    "reason,provider_delay,expected_delay",
    [
        ("ocr_quality_retry", 0, BASE_RECOVERY_SECONDS),
        ("paid_ocr_retry", 0, BASE_RECOVERY_SECONDS),
        ("mineru_visual_table_retry", 0, BASE_RECOVERY_SECONDS),
        ("ocr_quality_retry", 7200, 7200),
        ("transient_processing", 0, BASE_RECOVERY_SECONDS * 2**6),
    ],
)
def test_ocr_retry_backoff_stays_automatic_and_preserves_provider_delay(
    service, reason, provider_delay, expected_delay
):
    document = register(service, make_pdf([("Official terms", False)]), key=f"retry-delay-{reason}")
    claim = {
        "document_id": document["id"],
        "owner_id": "owner",
        "phase": "parse",
        "attempts": 3,
    }
    with service.db.transaction() as conn:
        service.collection.recovery.record_failure(
            conn,
            claim,
            processing=True,
            decision=Decision("transient_processing", True),
            exhausted=True,
        )
        conn.execute(
            "UPDATE collection_recovery_jobs SET recovery_count=6 WHERE item_id=?",
            (document["id"],),
        )
        before = time.time()
        service.collection.recovery.record_failure(
            conn,
            claim,
            processing=True,
            decision=Decision(reason, True, not_before=before + provider_delay),
            exhausted=True,
        )

    recovery = rows(service, "collection_recovery_jobs")[0]
    assert recovery["status"] == "waiting"
    assert recovery["available_at"] >= before + expected_delay - 3
    assert recovery["available_at"] <= before + expected_delay + 3


@pytest.mark.parametrize(
    "metadata",
    [
        {"attempts": 1, "request_id": "lost"},
        {"attempts": 0, "request_outcome": "unknown_transport_result"},
        {"attempts": 1, "http_status": 504},
        {"attempts": 1, "http_status": 408},
    ],
)
def test_processing_unknown_ocr_outcomes_are_retried_with_paid_attempt_history(metadata):
    manifest = {"pages": [{"page": 1, "status": "pending_ocr", "ocr": metadata}]}
    decision = classify_processing(manifest, phase="parse", retryable=True)
    assert decision.reason == "paid_ocr_retry"
    assert decision.retryable and decision.pages == (1,)


def test_unknown_ocr_outcome_keeps_retrying_after_three_preserved_cycles():
    manifest = {
        "pages": [
            {
                "page": 1,
                "status": "ocr_failed",
                "ocr": {"attempts": 1, "request_outcome": "unknown_transport_result"},
                "ocr_retry_cycles": [{"previous_page": {}} for _ in range(5)],
            }
        ]
    }
    decision = classify_processing(manifest, phase="parse")
    assert decision.reason == "paid_ocr_retry"
    assert decision.retryable and decision.pages == (1,)


def test_failed_vision_page_with_bound_response_retries_after_visual_quality_budget(service):
    from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import (
        MAX_RETRY_CYCLES,
        PROTOCOL,
    )

    content = make_pdf([("Official text", False)])
    original = service.archive.blobs.put_bytes("owner", content)
    manifest = _parsed_result(_parse_pdf(content), original.digest, "law")
    page = manifest["pages"][0]
    page.update(status="ocr_failed", source_hash=original.digest)
    bound_failed_vision_page(service, page)
    page["ocr_fallback"] = {"protocol": PROTOCOL, "attempts": MAX_RETRY_CYCLES}
    page["ocr_retry_cycles"] = [{"protocol": "previous-attempt"} for _ in range(MAX_RETRY_CYCLES)]
    decision = classify_processing(manifest, phase="parse")
    assert decision.reason == "ocr_quality_retry"
    assert decision.retryable and decision.pages == (page["page"],)

    with service.db.transaction() as conn:
        document = service.archive.store_original(
            conn,
            "owner",
            original,
            "law.pdf",
            "application/pdf",
            "https://official.invalid/law.pdf",
            collection="country:ID",
            level="primary",
            tags=["country:ID"],
            identity_key="bound-vision-error-automatic-retry",
            kind_hint="law",
        )
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed' WHERE id=?",
            (dump(manifest), document["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='failed',attempts=3,available_at=0 WHERE document_id=?",
            (document["id"],),
        )
        service.collection.recovery.record_failure(
            conn,
            {"document_id": document["id"], "owner_id": "owner", "phase": "parse", "attempts": 3},
            processing=True,
            decision=Decision("ocr_failure_review"),
            exhausted=True,
            error_type="incomplete_checkpoint",
        )

    result = service.collection.recovery.run_due(limit=32)

    assert result["requeued"] == 1
    with closing(service.db.connect()) as conn:
        saved = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
        )
        job = conn.execute(
            "SELECT phase,status FROM processing_jobs WHERE document_id=?", (document["id"],)
        ).fetchone()
        recovery = conn.execute(
            "SELECT status,reason FROM collection_recovery_jobs WHERE item_id=?", (document["id"],)
        ).fetchone()
    assert tuple(job) == ("parse", "queued")
    assert tuple(recovery) == ("queued", "ocr_quality_retry")
    retried = saved["pages"][0]
    assert retried["status"] == "pending_ocr" and retried["ocr"] == {"attempts": 0}
    assert len(retried["ocr_retry_cycles"]) == MAX_RETRY_CYCLES + 1
    assert retried["ocr_retry_cycles"][-1]["previous_page"]["ocr"]["error_response"]["blob"]


def test_native_asset_retry_history_has_no_three_cycle_stop():
    asset = {
        "asset_status": "ocr_uncertain",
        "asset_key": "chart-1",
        "input_identity": "identity",
        "source_hash": "a" * 64,
    }
    for attempt in range(5):
        asset["ocr"] = {"attempts": 1, "request_id": f"request-{attempt}"}
        preserve_previous_asset_attempt(asset)
    assert len(asset["ocr_attempt_history"]) == 5
    assert all("ocr_attempt_history" not in item["previous_asset"] for item in asset["ocr_attempt_history"])


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"attempts": 1, "request_outcome": "unknown_transport_result"}, "paid_ocr_retry"),
        (
            {
                "attempts": 2,
                "prompt_version": "legal-page-transcription-v3",
                "request_id": "completed-request",
                "complete": True,
            },
            "ocr_quality_retry",
        ),
    ],
)
def test_page_recovery_preserves_history_without_recursively_copying_old_cycles(service, metadata, reason):
    content = make_pdf([("Official text", False)])
    original = service.archive.blobs.put_bytes("owner", content)
    manifest = _parsed_result(_parse_pdf(content), original.digest, "law")
    page = manifest["pages"][0]
    page.update(
        status="ocr_uncertain",
        text="Previous OCR output",
        ocr=metadata,
        ocr_retry_cycles=[{"protocol": "old", "previous_page": {"text": "older output"}} for _ in range(5)],
    )
    with service.db.transaction() as conn:
        document = service.archive.store_original(
            conn,
            "owner",
            original,
            "law.pdf",
            "application/pdf",
            "https://official.invalid/law.pdf",
            collection="country:ID",
            level="primary",
            tags=["country:ID"],
            identity_key="retry-history-compaction",
            kind_hint="law",
        )
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), document["id"]))
        service.collection.recovery._retry_pages(
            conn,
            {"owner_id": "owner", "id": "recovery", "recovery_count": 5},
            {"id": document["id"], "version": 1, "content_hash": original.digest},
            manifest,
            Decision(reason, True, pages=(1,)),
        )
    with closing(service.db.connect()) as conn:
        stored_manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
        )
    cycles = stored_manifest["pages"][0]["ocr_retry_cycles"]
    assert len(cycles) == 6
    assert cycles[-1]["previous_page"]["text"] == "Previous OCR output"
    assert "ocr_retry_cycles" not in cycles[-1]["previous_page"]
    assert stored_manifest["pages"][0]["status"] == "pending_ocr"


def test_incomplete_vision_page_and_mineru_table_review_are_retried_visually():
    vision_page = {
        "page": 4,
        "status": "ocr_uncertain",
        "ocr": {
            "prompt_version": "legal-page-transcription-v3",
            "attempts": 1,
            "complete": False,
            "uncertain_spans": ["model_reported_incomplete"],
        },
    }
    decision = classify_processing({"pages": [vision_page]}, phase="parse")
    assert decision.reason == "ocr_quality_retry" and decision.pages == (4,)

    completed_but_uncertain = {
        "page": 8,
        "status": "ocr_uncertain",
        "ocr": {
            "prompt_version": "legal-page-transcription-v3",
            "request_id": "completed-request",
            "attempts": 2,
            "complete": True,
        },
    }
    decision = classify_processing({"pages": [completed_but_uncertain]}, phase="parse")
    assert decision.reason == "ocr_quality_retry" and decision.pages == (8,)

    mineru_page = {
        "page": 4,
        "status": "ocr_uncertain",
        "reasons": ["table_structure_ambiguous"],
        "ocr": {
            "provider": "mineru",
            "result_hash": "a" * 64,
            "complete": False,
            "uncertain_spans": ["table_structure_ambiguous"],
        },
    }
    decision = classify_processing({"pages": [mineru_page]}, phase="parse")
    assert decision.reason == "mineru_visual_table_retry" and decision.retryable
    assert decision.pages == (4,)


def test_mineru_visual_fallback_retries_incomplete_output_only_three_quality_cycles():
    page = {
        "page": 4,
        "status": "ocr_uncertain",
        "ocr_fallback": {"protocol": "mineru-table-visual-page-v1", "attempts": 2},
        "ocr": {
            "prompt_version": "legal-page-transcription-v3",
            "request_id": "completed-request",
            "attempts": 1,
            "complete": True,
            "uncertain_spans": ["table_layout_not_preserved"],
        },
    }
    decision = classify_processing({"pages": [page]}, phase="parse")
    assert decision.reason == "ocr_quality_retry" and decision.pages == (4,)

    page["ocr_fallback"]["attempts"] = 3
    decision = classify_processing({"pages": [page]}, phase="parse")
    assert decision.reason == "quality_review" and not decision.retryable


def test_pending_mineru_visual_fallback_retries_after_interrupted_parse(monkeypatch):
    from app.contexts.documents.infrastructure.providers import vision
    from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import (
        MAX_RETRY_CYCLES,
        PROTOCOL,
    )

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    page = {
        "page": 4,
        "status": "pending_ocr",
        "reasons": ["table_structure_ambiguous"],
        "ocr_fallback": {"protocol": PROTOCOL, "attempts": 1},
        "ocr": {"attempts": 0},
    }
    decision = classify_processing({"pages": [page]}, phase="parse")
    assert decision.reason == "mineru_visual_table_retry"
    assert decision.retryable and decision.pages == (4,)

    page["ocr_fallback"]["attempts"] = MAX_RETRY_CYCLES
    decision = classify_processing({"pages": [page]}, phase="parse")
    assert decision.reason == "processing_review"
    assert not decision.retryable


def test_pending_mineru_visual_fallback_retries_saved_provider_error_past_quality_cap(monkeypatch):
    from app.contexts.documents.infrastructure.providers import vision
    from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import PROTOCOL
    from app.contexts.ingestion.application.collection_recovery import _digest

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    source_hash = "a" * 64
    retry_not_before = time.time() + 3600
    previous = {
        "page": 4,
        "source_hash": source_hash,
        "status": "ocr_failed",
        "ocr": {
            "provider": "vision",
            "prompt_version": "legal-page-transcription-v3",
            "request_id": "saved-failed-request",
            "retry_not_before": retry_not_before,
            "error_response": {
                "format": "ocr-error-response-v1",
                "blob": {"content_hash": "b" * 64},
            },
        },
    }
    page = {
        "page": 4,
        "source_hash": source_hash,
        "status": "pending_ocr",
        "ocr_fallback": {"protocol": PROTOCOL, "attempts": 3},
        "ocr": {"attempts": 0},
        "ocr_retry_cycles": [
            {
                "protocol": "ocr-automatic-retry-checkpoint-v1",
                "previous_page_sha256": _digest(previous),
                "previous_page": previous,
            }
        ],
    }
    decision = classify_processing({"pages": [page]}, phase="parse")
    assert decision.reason == "mineru_visual_table_retry"
    assert decision.retryable and decision.pages == (4,)
    assert decision.not_before == retry_not_before

    page["ocr_retry_cycles"][0]["previous_page"]["status"] = "ocr_complete"
    decision = classify_processing({"pages": [page]}, phase="parse")
    assert decision.reason == "processing_review"
    assert not decision.retryable


def test_pending_mineru_visual_fallback_retries_archived_unknown_request(monkeypatch):
    from app.contexts.documents.infrastructure.providers import vision
    from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import PROTOCOL
    from app.contexts.ingestion.application.collection_recovery import _digest

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    source_hash = "c" * 64
    retry_not_before = time.time() + 3600
    previous = {
        "page": 8,
        "source_hash": source_hash,
        "status": "pending_ocr",
        "ocr": {
            "source_hash": source_hash,
            "page": 8,
            "attempts": 1,
            "request_id": "archived-unresolved-request",
            "image_sha256": "d" * 64,
            "prompt_version": "legal-page-transcription-v3",
            "requested_model": "vision-test-model",
            "request_outcome": "unknown_transport_result",
            "retry_not_before": retry_not_before,
        },
    }
    page = {
        "page": 8,
        "source_hash": source_hash,
        "status": "pending_ocr",
        "ocr_fallback": {"protocol": PROTOCOL, "attempts": 3},
        "ocr": {"attempts": 0},
        "ocr_retry_cycles": [
            {
                "protocol": "ocr-automatic-retry-checkpoint-v1",
                "previous_page_sha256": _digest(previous),
                "previous_page": previous,
            }
        ],
    }

    decision = classify_processing({"pages": [page]}, phase="parse")
    assert decision.reason == "paid_ocr_retry"
    assert decision.retryable and decision.pages == (8,)
    assert decision.not_before == retry_not_before


@pytest.mark.parametrize(("validator_accepts", "attempts", "expected"), [(True, 2, True), (False, 2, False), (True, 3, False)])
def test_mineru_visual_page_with_missing_provider_tag_is_retried_only_when_receipt_valid(
    monkeypatch, validator_accepts, attempts, expected
):
    from app.contexts.documents.infrastructure.providers import mineru

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    monkeypatch.setattr(
        mineru,
        "manifest_valid",
        lambda value: validator_accepts and value["pages"][0]["ocr"].get("provider") == "vision",
    )
    page = {
        "page": 1,
        "status": "ocr_complete",
        "ocr_fallback": {
            "protocol": "mineru-table-visual-page-v1",
            "attempts": attempts,
            "accepted": True,
        },
        "ocr": {"complete": True, "provider": None, "uncertain_spans": []},
    }
    manifest = {"parser": "mineru", "remote_parse": {"state": "done"}, "pages": [page]}

    decision = classify_processing(manifest, phase="parse")

    if expected:
        assert decision.reason == "mineru_visual_table_retry"
        assert decision.retryable and decision.pages == (1,)
    else:
        assert decision.reason == "processing_review"
        assert not decision.retryable


def test_bound_pending_image_is_automatically_queued_when_vision_is_configured(monkeypatch):
    from app.contexts.documents.infrastructure.providers import native_asset_ocr, vision

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    monkeypatch.setattr(
        native_asset_ocr,
        "assets",
        lambda _manifest: [
            {"asset_key": "native-asset-ocr-bound", "asset_status": "pending_vision", "ocr": {}}
        ],
    )

    decision = classify_processing({"pages": []}, phase="parse")

    assert decision.reason == "ocr_quality_retry"
    assert decision.retryable
    assert decision.asset_keys == ("native-asset-ocr-bound",)


def test_bound_pending_image_stays_held_when_vision_is_not_configured(monkeypatch):
    from app.contexts.documents.infrastructure.providers import native_asset_ocr, vision

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: False)
    monkeypatch.setattr(
        native_asset_ocr,
        "assets",
        lambda _manifest: [
            {"asset_key": "native-asset-ocr-bound", "asset_status": "pending_vision", "ocr": {}}
        ],
    )

    decision = classify_processing({"pages": []}, phase="parse")

    assert decision.reason == "processing_review"
    assert not decision.retryable


def test_blocked_bound_pending_image_is_requeued_without_an_existing_ocr_result(service, monkeypatch):
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    content = b'<main><p>Official terms.</p><img src="/figure.png"></main>'
    manifest = parse_file("law.html", content)
    raw_image = raster()
    image = service.archive.blobs.put_bytes("owner", raw_image)
    manifest = bind_linked_image_bounded(
        content,
        manifest,
        object_key=manifest["html_source"]["objects"][0]["object_key"],
        source_url="https://official.invalid/law",
        image=raw_image,
        image_blob={
            "content_hash": image.digest,
            "size": image.size,
            "storage_path": str(image.path.relative_to(service.db.data_dir)),
        },
    )
    original = service.archive.blobs.put_bytes("owner", content)
    with service.db.transaction() as conn:
        document = service.archive.store_original(
            conn,
            "owner",
            original,
            "law.html",
            "text/html",
            "https://official.invalid/law",
            collection="country:ID",
            level="primary",
            tags=["country:ID"],
            identity_key="pending-image-automatic-recovery",
            kind_hint="law",
        )
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed' WHERE id=?",
            (dump(manifest), document["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='failed',attempts=3,available_at=0 WHERE document_id=?",
            (document["id"],),
        )
        service.collection.recovery.record_failure(
            conn,
            {"document_id": document["id"], "owner_id": "owner", "phase": "parse", "attempts": 3},
            processing=True,
            decision=Decision("processing_review"),
            exhausted=True,
            error_type="incomplete_checkpoint",
        )

    result = service.collection.recovery.run_due(limit=32)

    assert result["requeued"] == 1
    with closing(service.db.connect()) as conn:
        saved = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
        )
        job = conn.execute(
            "SELECT phase,status FROM processing_jobs WHERE document_id=?", (document["id"],)
        ).fetchone()
        recovery = conn.execute(
            "SELECT status,reason FROM collection_recovery_jobs WHERE item_id=?", (document["id"],)
        ).fetchone()
    assert tuple(job) == ("parse", "queued")
    assert tuple(recovery) == ("queued", "ocr_quality_retry")
    assert native_asset_ocr.pending_assets(saved)[0]["asset_status"] == "pending_vision"
    assert "ocr" not in native_asset_ocr.pending_assets(saved)[0]


def test_mineru_table_retry_preserves_original_page_and_marks_visual_fallback(service):
    content = make_pdf([("Schedule tariff table", False)])
    original = service.archive.blobs.put_bytes("owner", content)
    manifest = _parsed_result(_parse_pdf(content), original.digest, "law")
    page = manifest["pages"][0]
    page.update(
        status="ocr_uncertain",
        reasons=["table_structure_ambiguous"],
        text="Original MinerU table text",
        ocr={
            "provider": "mineru",
            "result_hash": "b" * 64,
            "complete": False,
            "uncertain_spans": ["table_structure_ambiguous"],
        },
    )
    with service.db.transaction() as conn:
        document = service.archive.store_original(
            conn,
            "owner",
            original,
            "law.pdf",
            "application/pdf",
            "https://official.invalid/law.pdf",
            collection="country:ID",
            level="primary",
            tags=["country:ID"],
            identity_key="mineru-visual-fallback",
            kind_hint="law",
        )
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), document["id"]))
        decision = classify_processing(manifest, phase="parse")
        receipt = service.collection.recovery._retry_pages(
            conn,
            {"owner_id": "owner", "id": "recovery", "recovery_count": 0},
            {"id": document["id"], "version": 1, "content_hash": original.digest},
            manifest,
            decision,
        )
    with closing(service.db.connect()) as conn:
        stored = json.loads(conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0])
    retried = stored["pages"][0]
    assert decision.reason == "mineru_visual_table_retry"
    assert retried["status"] == "pending_ocr"
    assert retried["ocr_fallback"]["protocol"] == "mineru-table-visual-page-v1"
    assert retried["ocr_fallback"]["attempts"] == 1
    assert retried["ocr_retry_cycles"][-1]["previous_page"]["text"] == "Original MinerU table text"
    assert receipt[0]["page"] == 1


def test_blocked_mineru_table_review_is_automatically_queued_for_visual_fallback(service, monkeypatch):
    from app.contexts.documents.infrastructure.providers import mineru, vision

    monkeypatch.setattr(mineru, "manifest_valid", lambda _manifest: True)
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    content = make_pdf([("Schedule tariff table", False)])
    document = register(service, content, key="mineru-table-auto-fallback")
    manifest = _parsed_result(_parse_pdf(content), document["content_hash"], "law")
    manifest["parser"] = "mineru"
    manifest["parser_version"] = "api-v4"
    manifest["pipeline_version"] = "mineru-precise-v1+layout-token-hierarchical-v2"
    manifest["pages"][0].update(
        status="ocr_uncertain",
        reasons=["table_structure_ambiguous"],
        text="Original MinerU tariff row",
        ocr={
            "provider": "mineru",
            "result_hash": "d" * 64,
            "complete": False,
            "uncertain_spans": ["table_structure_ambiguous"],
        },
    )
    manifest["remote_parse"] = {
        "provider": "mineru",
        "state": "done",
        "source_hash": document["content_hash"],
        "page_count": 1,
        "parts": [],
    }
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed' WHERE id=?",
            (dump(manifest), document["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='failed',attempts=3,available_at=0 WHERE document_id=?",
            (document["id"],),
        )
        claim = {
            "document_id": document["id"],
            "owner_id": "owner",
            "phase": "parse",
            "attempts": 3,
        }
        service.collection.recovery.record_failure(
            conn,
            claim,
            processing=True,
            decision=Decision("quality_review"),
            exhausted=True,
            error_type="incomplete_checkpoint",
        )

    result = service.collection.recovery.run_due(limit=32)
    assert result["requeued"] == 1
    with closing(service.db.connect()) as conn:
        saved = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
        )
        job = conn.execute(
            "SELECT phase,status FROM processing_jobs WHERE document_id=?", (document["id"],)
        ).fetchone()
        recovery = conn.execute(
            "SELECT status,reason FROM collection_recovery_jobs WHERE item_id=?", (document["id"],)
        ).fetchone()
    assert tuple(job) == ("parse", "queued")
    assert tuple(recovery) == ("queued", "mineru_visual_table_retry")
    assert saved["pages"][0]["status"] == "pending_ocr"
    assert saved["pages"][0]["ocr_fallback"]["attempts"] == 1
    assert saved["pages"][0]["ocr_retry_cycles"][-1]["previous_page"]["text"] == "Original MinerU tariff row"

    # A parse interruption after resetting this page must retry the same
    # source-bound visual checkpoint without requiring a manual review step.
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET status='failed' WHERE id=?", (document["id"],))
        conn.execute(
            "UPDATE processing_jobs SET status='failed',attempts=3,available_at=0 WHERE document_id=?",
            (document["id"],),
        )
        service.collection.recovery.record_failure(
            conn,
            {**claim, "attempts": 3},
            processing=True,
            decision=Decision("processing_review"),
            exhausted=True,
            error_type="ValueError",
        )

    assert service.collection.recovery.run_due(limit=32)["requeued"] == 1
    with closing(service.db.connect()) as conn:
        saved = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
        )
        recovery = conn.execute(
            "SELECT status,reason FROM collection_recovery_jobs WHERE item_id=?", (document["id"],)
        ).fetchone()
    assert tuple(recovery) == ("queued", "mineru_visual_table_retry")
    assert saved["pages"][0]["ocr_fallback"]["attempts"] == 2


def test_saved_provider_error_requeues_pending_visual_page_after_quality_cap(service, monkeypatch):
    from copy import deepcopy

    from app.contexts.documents.infrastructure.providers import vision
    from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import PROTOCOL
    from app.contexts.ingestion.application.collection_recovery import _digest

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    content = make_pdf([("Original tariff table", False)])
    document = register(service, content, key="mineru-table-provider-retry")
    manifest = _parsed_result(_parse_pdf(content), document["content_hash"], "law")
    page = manifest["pages"][0]
    page.update(
        status="pending_ocr",
        source_hash=document["content_hash"],
        reasons=["table_structure_ambiguous"],
        text="Original MinerU tariff row",
        ocr_fallback={"protocol": PROTOCOL, "attempts": 3, "source_page": page["page"]},
        ocr={"attempts": 0},
    )
    previous = bound_failed_vision_page(service, deepcopy(page))
    previous["status"] = "ocr_failed"
    page["ocr_retry_cycles"] = [
        {
            "protocol": "ocr-automatic-retry-checkpoint-v1",
            "previous_page_sha256": _digest(previous),
            "previous_page": previous,
        }
    ]
    manifest.update(parser="mineru", parser_version="api-v4")
    manifest["remote_parse"] = {
        "provider": "mineru",
        "state": "done",
        "source_hash": document["content_hash"],
        "page_count": 1,
        "parts": [],
    }
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed' WHERE id=?",
            (dump(manifest), document["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='failed',attempts=3,available_at=0 WHERE document_id=?",
            (document["id"],),
        )
        service.collection.recovery.record_failure(
            conn,
            {"document_id": document["id"], "owner_id": "owner", "phase": "parse", "attempts": 3},
            processing=True,
            decision=Decision("processing_review"),
            exhausted=True,
            error_type="ValueError",
        )

    assert service.collection.recovery.run_due(limit=32)["requeued"] == 1
    with closing(service.db.connect()) as conn:
        saved = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
        )
        recovery = conn.execute(
            "SELECT status,reason FROM collection_recovery_jobs WHERE item_id=?", (document["id"],)
        ).fetchone()
    assert tuple(recovery) == ("queued", "mineru_visual_table_retry")
    assert saved["pages"][0]["status"] == "pending_ocr"
    assert saved["pages"][0]["ocr_fallback"]["attempts"] == 3


def test_blocked_mineru_download_resumes_existing_batch_without_resubmitting(service):
    from app.contexts.documents.infrastructure.providers.mineru import (
        MinerUParser,
        parse_profile_hash,
        pending_manifest,
    )

    content = make_pdf([("Existing remote task", False)])
    document = register(service, content, key="mineru-proxy-result-resume")
    manifest = pending_manifest("official.pdf", document["content_hash"])
    manifest.pop("parser")
    manifest.pop("source_hash")
    profile_hash = parse_profile_hash(MinerUParser.profile())
    manifest["remote_parse"].update(
        state="pending",
        profile_hash=profile_hash,
        page_count=1,
    )
    batch_id = "existing-batch-123"
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed',error=? WHERE id=?",
            (dump(manifest), "当前来源域名不在采集代理白名单中。", document["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='failed',attempts=3,available_at=0 WHERE document_id=?",
            (document["id"],),
        )
        conn.execute(
            """INSERT INTO document_parse_tasks(
            id,document_id,owner_id,source_hash,profile_hash,part_index,first_page,last_page,
            part_blob,state,batch_id,private_data,progress,attempts,next_at,error_code,created_at,updated_at
            ) VALUES(?,?,?,?,?,0,1,1,?,'uploaded',?,'{}','{}',0,0,'rate_wait',?,?)""",
            (
                "existing-task-123",
                document["id"],
                "owner",
                document["content_hash"],
                profile_hash,
                dump(
                    {
                        "content_hash": document["content_hash"],
                        "size": document["size"],
                        "storage_path": document["storage_path"],
                        "mime_type": document["mime_type"],
                    }
                ),
                batch_id,
                now(),
                now(),
            ),
        )
        service.collection.recovery.record_failure(
            conn,
            {"document_id": document["id"], "owner_id": "owner", "phase": "parse", "attempts": 3},
            processing=True,
            decision=Decision("processing_review"),
            exhausted=True,
            error_type="DownloadError",
        )
        current = conn.execute(
            "SELECT id,owner_id,content_hash,manifest FROM documents WHERE id=?", (document["id"],)
        ).fetchone()
        assert service.collection.recovery._resumable_mineru_parse(
            conn, "owner", current, json.loads(current["manifest"])
        ) is not None

    result = service.collection.recovery.run_due(limit=32)

    assert result["requeued"] == 1
    with closing(service.db.connect()) as conn:
        job = conn.execute(
            "SELECT phase,status FROM processing_jobs WHERE document_id=?", (document["id"],)
        ).fetchone()
        recovery = conn.execute(
            "SELECT status,reason FROM collection_recovery_jobs WHERE item_id=?", (document["id"],)
        ).fetchone()
        task = conn.execute(
            "SELECT state,batch_id,attempts FROM document_parse_tasks WHERE document_id=?",
            (document["id"],),
        ).fetchone()
    assert tuple(job) == ("parse", "queued")
    assert tuple(recovery) == ("queued", "mineru_parse_resume")
    assert tuple(task) == ("uploaded", batch_id, 0)


def test_confirmed_mineru_failure_retries_in_fresh_generation_and_reuses_other_parts(service):
    from app.contexts.documents.infrastructure.providers.mineru import (
        MinerUParser,
        parse_profile_hash,
        pending_manifest,
    )

    content = make_pdf([("Part one", False), ("Part two", False), ("Part three", False)])
    document = register(service, content, key="mineru-terminal-auto-retry")
    profile = MinerUParser.profile()
    profile_hash = parse_profile_hash(profile)
    manifest = pending_manifest("official.pdf", document["content_hash"])
    manifest["remote_parse"].update(state="pending", profile=profile, profile_hash=profile_hash)
    part_blob = dump(
        {
            "content_hash": document["content_hash"],
            "size": document["size"],
            "storage_path": document["storage_path"],
            "mime_type": document["mime_type"],
        }
    )
    done_blob = dump({"content_hash": "a" * 64, "size": 1, "storage_path": "files/result.zip"})
    live_upload = dump({"upload_url": "https://upload.example.org/signed"})
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed' WHERE id=?",
            (dump(manifest), document["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='failed',attempts=3,available_at=0 WHERE document_id=?",
            (document["id"],),
        )
        for index, (state, batch, private, result, error) in enumerate(
            [
                ("blocked", "failed-batch-0001", "{}", None, "remote_failed"),
                ("done", "done-batch-0002", "{}", done_blob, None),
                ("uploaded", "live-batch-0003", live_upload, None, None),
            ]
        ):
            conn.execute(
                """INSERT INTO document_parse_tasks(
                id,document_id,owner_id,source_hash,profile_hash,part_index,first_page,last_page,
                part_blob,state,batch_id,private_data,result_blob,progress,attempts,next_at,error_code,
                created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"task-{index}",
                    document["id"],
                    "owner",
                    document["content_hash"],
                    profile_hash,
                    index,
                    index + 1,
                    index + 1,
                    part_blob,
                    state,
                    batch,
                    private,
                    result,
                    "{}",
                    0,
                    0,
                    error,
                    now(),
                    now(),
                ),
            )
        service.collection.recovery.record_failure(
            conn,
            {"document_id": document["id"], "owner_id": "owner", "phase": "parse", "attempts": 3},
            processing=True,
            decision=Decision("processing_review"),
            exhausted=True,
            error_type="MinerUError",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        scheduled_results = list(
            pool.map(lambda _: service.collection.recovery.run_due(limit=32), range(2))
        )
    assert all(result["requeued"] == 0 for result in scheduled_results)
    with closing(service.db.connect()) as conn:
        assert conn.execute(
            "SELECT count(*) FROM collection_recovery_events WHERE event='terminal_mineru_failure_retry_scheduled'"
        ).fetchone()[0] == 1
        recovery = conn.execute(
            "SELECT status,reason,retry_plan FROM collection_recovery_jobs WHERE item_id=?",
            (document["id"],),
        ).fetchone()
        saved = json.loads(conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0])
        old_tasks = conn.execute(
            "SELECT part_index,state,batch_id FROM document_parse_tasks WHERE document_id=? AND profile_hash=? ORDER BY part_index",
            (document["id"], profile_hash),
        ).fetchall()
        retry_hash = saved["remote_parse"]["profile_hash"]
        retry_tasks = conn.execute(
            "SELECT part_index,state,batch_id,result_blob FROM document_parse_tasks WHERE document_id=? AND profile_hash=? ORDER BY part_index",
            (document["id"], retry_hash),
        ).fetchall()
    assert recovery["status"] == "waiting" and recovery["reason"] == "mineru_terminal_retry"
    assert saved["remote_parse"]["retry_generation"].startswith("auto-")
    assert retry_hash != profile_hash
    assert [(row["state"], row["batch_id"]) for row in old_tasks] == [
        ("blocked", "failed-batch-0001"),
        ("done", "done-batch-0002"),
        ("superseded", "live-batch-0003"),
    ]
    assert [(row["state"], row["batch_id"]) for row in retry_tasks] == [
        ("planned", None),
        ("done", "done-batch-0002"),
        ("uploaded", "live-batch-0003"),
    ]
    assert retry_tasks[1]["result_blob"] == done_blob

    with service.db.transaction() as conn:
        conn.execute("UPDATE collection_recovery_jobs SET available_at=0 WHERE item_id=?", (document["id"],))
        conn.execute("UPDATE processing_jobs SET available_at=0 WHERE document_id=?", (document["id"],))
        queued_documents = [
            (
                f"queue-fill-{index}",
                "owner",
                None,
                None,
                "queued",
                f"queued-{index}.pdf",
                "primary",
                "processing",
                "application/pdf",
                1,
                1,
                f"{index:064x}",
                f"files/queued-{index}.pdf",
                "[]",
                None,
                None,
                "{}",
                now(),
            )
            for index in range(200)
        ]
        conn.executemany(
            """INSERT INTO documents(
                id,owner_id,case_id,parent_id,title,filename,role,status,mime_type,size,version,
                content_hash,storage_path,tags,source_url,error,manifest,created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            queued_documents,
        )
        conn.executemany(
            "INSERT INTO processing_jobs(document_id,owner_id,phase,status,updated_at) VALUES(?,?,'embed','queued',?)",
            [(row[0], "owner", now()) for row in queued_documents],
        )
    resumed = service.collection.recovery.run_due(limit=32)
    assert resumed["requeued"] == 1
    with closing(service.db.connect()) as conn:
        job = conn.execute(
            "SELECT phase,status FROM processing_jobs WHERE document_id=?", (document["id"],)
        ).fetchone()
        recovery = conn.execute(
            "SELECT status,reason FROM collection_recovery_jobs WHERE item_id=?", (document["id"],)
        ).fetchone()
        queue_size = conn.execute(
            "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')"
        ).fetchone()[0]
    assert tuple(job) == ("parse", "queued")
    assert tuple(recovery) == ("queued", "mineru_terminal_retry")
    assert queue_size == 201


def test_decode_replacement_mineru_pages_retry_only_their_part_and_preserve_previous_manifest(
    service, monkeypatch
):
    from app.contexts.documents.infrastructure.providers import mineru
    from app.contexts.documents.infrastructure.providers.mineru_results import resolve_reference

    monkeypatch.setattr(mineru, "manifest_valid", lambda _manifest: True)
    content = make_pdf([("First page", False), ("Text with replacement", False), ("Final page", False)])
    document = register(service, content, key="mineru-decode-quality-retry")
    profile = mineru.MinerUParser.profile()
    profile_hash = mineru.parse_profile_hash(profile)
    result_hashes = ("a" * 64, "b" * 64)
    tasks = [
        {
            "id": f"quality-task-{index}",
            "source_hash": document["content_hash"],
            "profile_hash": profile_hash,
            "part_index": index,
            "first_page": first_page,
            "last_page": last_page,
            "part_blob": dump(
                {
                    "content_hash": document["content_hash"],
                    "size": document["size"],
                    "storage_path": document["storage_path"],
                    "mime_type": document["mime_type"],
                }
            ),
            "state": "done",
            "batch_id": f"done-batch-{index:04d}",
            "private_data": "{}",
            "result_blob": dump(
                {
                    "content_hash": result_hashes[index],
                    "size": 1,
                    "storage_path": f"files/result-{index}.zip",
                }
            ),
            "progress": "{}",
            "attempts": 0,
            "next_at": 0,
            "error_code": None,
        }
        for index, (first_page, last_page) in enumerate(((1, 2), (3, 3)))
    ]
    pages = []
    for number in range(1, 4):
        uncertain = number == 2
        text = "Text with replacement �" if uncertain else f"Verified page {number}."
        pages.append(
            {
                "page": number,
                "source_hash": document["content_hash"],
                "status": "ocr_uncertain" if uncertain else "ocr_complete",
                "text": text,
                "reasons": ["decode_replacement"] if uncertain else [],
                "ocr": {
                    "provider": "mineru",
                    "complete": not uncertain,
                    "uncertain_spans": ["decode_replacement"] if uncertain else [],
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
            }
        )
    manifest = {
        "parser": "mineru",
        "source_hash": document["content_hash"],
        "title": "official.pdf",
        "elements": [],
        "chunks": [],
        "warnings": [],
        "quality": {"complete": False},
        "pages": pages,
        "remote_parse": {
            "provider": "mineru",
            "state": "done",
            "source_hash": document["content_hash"],
            "profile": profile,
            "profile_hash": profile_hash,
            "page_count": 3,
            "parts": [
                {
                    "task_id": task["id"],
                    "source_hash": document["content_hash"],
                    "first_page": task["first_page"],
                    "last_page": task["last_page"],
                    "result_blob": json.loads(task["result_blob"]),
                }
                for task in tasks
            ],
        },
    }
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed' WHERE id=?",
            (dump(manifest), document["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='failed',attempts=1,available_at=0 WHERE document_id=?",
            (document["id"],),
        )
        for task in tasks:
            conn.execute(
                """INSERT INTO document_parse_tasks(
                id,document_id,owner_id,source_hash,profile_hash,part_index,first_page,last_page,
                part_blob,state,batch_id,private_data,result_blob,progress,attempts,next_at,error_code,
                created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task["id"],
                    document["id"],
                    "owner",
                    task["source_hash"],
                    task["profile_hash"],
                    task["part_index"],
                    task["first_page"],
                    task["last_page"],
                    task["part_blob"],
                    task["state"],
                    task["batch_id"],
                    task["private_data"],
                    task["result_blob"],
                    task["progress"],
                    task["attempts"],
                    task["next_at"],
                    task["error_code"],
                    now(),
                    now(),
                ),
            )
        service.collection.recovery.record_failure(
            conn,
            {"document_id": document["id"], "owner_id": "owner", "phase": "parse", "attempts": 1},
            processing=True,
            decision=Decision("quality_review"),
            exhausted=True,
            error_type="incomplete_checkpoint",
        )

    scheduled = service.collection.recovery.run_due(limit=32)
    assert scheduled["requeued"] == 0
    with closing(service.db.connect()) as conn:
        recovery = conn.execute(
            "SELECT status,reason,retry_plan FROM collection_recovery_jobs WHERE item_id=?",
            (document["id"],),
        ).fetchone()
        saved = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
        )
        old_rows = conn.execute(
            "SELECT state FROM document_parse_tasks WHERE document_id=? AND profile_hash=? ORDER BY part_index",
            (document["id"], profile_hash),
        ).fetchall()
        retry_hash = saved["remote_parse"]["profile_hash"]
        retry_rows = conn.execute(
            "SELECT part_index,state,result_blob FROM document_parse_tasks WHERE document_id=? AND profile_hash=? ORDER BY part_index",
            (document["id"], retry_hash),
        ).fetchall()
    assert recovery["status"] == "waiting" and recovery["reason"] == "mineru_quality_retry"
    retry_plan = json.loads(recovery["retry_plan"])
    assert retry_plan["retry_kind"] == "decode_quality"
    assert retry_plan["retry_pages"] == [2]
    assert retry_plan["retry_part_indexes"] == [0]
    assert [row["state"] for row in old_rows] == ["done", "done"]
    assert [(row["part_index"], row["state"]) for row in retry_rows] == [(0, "planned"), (1, "done")]
    assert retry_rows[1]["result_blob"] == tasks[1]["result_blob"]
    previous_path = resolve_reference(saved["remote_parse"]["previous_manifest"], service.db.files_dir, "owner")
    assert json.loads(previous_path.read_bytes()) == manifest

    with service.db.transaction() as conn:
        conn.execute("UPDATE collection_recovery_jobs SET available_at=0 WHERE item_id=?", (document["id"],))
        conn.execute("UPDATE processing_jobs SET available_at=0 WHERE document_id=?", (document["id"],))
    resumed = service.collection.recovery.run_due(limit=32)
    assert resumed["requeued"] == 1
    with closing(service.db.connect()) as conn:
        job = conn.execute(
            "SELECT phase,status FROM processing_jobs WHERE document_id=?", (document["id"],)
        ).fetchone()
        recovery = conn.execute(
            "SELECT status,reason,recovery_count FROM collection_recovery_jobs WHERE item_id=?",
            (document["id"],),
        ).fetchone()
    assert tuple(job) == ("parse", "queued")
    assert tuple(recovery) == ("queued", "mineru_quality_retry", 1)


def test_mineru_keeps_confirmed_remote_failure_distinct_from_local_block(monkeypatch, service):
    from app.contexts.documents.infrastructure.providers import mineru

    content = make_pdf([("Terminal failure", False)])
    document = register(service, content, key="mineru-terminal-state")
    profile = mineru.MinerUParser.profile()
    profile_hash = mineru.parse_profile_hash(profile)
    manifest = mineru.pending_manifest("official.pdf", document["content_hash"])
    manifest["remote_parse"].update(state="pending", profile=profile, profile_hash=profile_hash, page_count=1)
    part_blob = dump(
        {
            "content_hash": document["content_hash"],
            "size": document["size"],
            "storage_path": document["storage_path"],
            "mime_type": document["mime_type"],
        }
    )
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), document["id"]))
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='queued',available_at=0 WHERE document_id=?",
            (document["id"],),
        )
        conn.execute(
            """INSERT INTO document_parse_tasks(
            id,document_id,owner_id,source_hash,profile_hash,part_index,first_page,last_page,
            part_blob,state,batch_id,private_data,progress,attempts,next_at,created_at,updated_at
            ) VALUES('remote-task',?,?,?,?,0,1,1,?,'uploaded','batch-12345678','{}','{}',0,0,?,?)""",
            (document["id"], "owner", document["content_hash"], profile_hash, part_blob, now(), now()),
        )
    monkeypatch.setenv("MINERU_API_TOKEN", "test-token")
    monkeypatch.setattr(mineru.MinerUClient, "api", lambda *_args, **_kwargs: {
        "extract_result": [{"data_id": "remote-task", "state": "failed", "err_code": "remote_failed"}]
    })
    claim = service.collection.claim_processing(owner_id="owner", document_ids=[document["id"]])
    assert claim
    with closing(service.db.connect()) as conn:
        current = dict(conn.execute("SELECT * FROM documents WHERE id=?", (document["id"],)).fetchone())
        current["manifest"] = json.loads(current["manifest"])

    with pytest.raises(mineru.MinerUError, match="remote_failed"):
        mineru.MinerUParser(service.collection).step(current, claim, current["manifest"])

    with closing(service.db.connect()) as conn:
        task = conn.execute(
            "SELECT state,error_code FROM document_parse_tasks WHERE id='remote-task'"
        ).fetchone()
    assert tuple(task) == ("failed", "remote_failed")


@pytest.mark.parametrize("status", [400, 401, 403])
def test_ocr_http_rejections_are_automatically_retried_after_backoff(status):
    page = {
        "page": 1,
        "status": "ocr_failed",
        "ocr": {
            "prompt_version": "legal-page-transcription-v3",
            "attempts": 1,
            "http_status": status,
            "complete": False,
        },
    }
    decision = classify_processing({"pages": [page]}, phase="parse")
    assert decision.reason == "ocr_quality_retry"
    assert decision.retryable and decision.pages == (1,)


def test_budget_review_incomplete_image_is_automatically_retried_with_its_old_result_preserved(
    service, monkeypatch
):
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    body = b'<main><p>Official terms.</p><img src="/figure.png"></main>'
    raw = raster()
    manifest = parse_file("law.html", body)
    original_image = service.archive.blobs.put_bytes("owner", raw)
    manifest = bind_linked_image_bounded(
        body,
        manifest,
        object_key=manifest["html_source"]["objects"][0]["object_key"],
        source_url="https://official.invalid/law",
        image=raw,
        image_blob={
            "content_hash": original_image.digest,
            "size": original_image.size,
            "storage_path": str(original_image.path.relative_to(service.db.data_dir)),
        },
    )

    monkeypatch.setattr(
        vision,
        "transcribe_image",
        lambda _rendered: {
            "text": "Chart labels are partially unreadable.",
            "complete": False,
            "is_blank": False,
            "uncertain_spans": ["model_reported_incomplete"],
            "model": "test-vision",
            "requested_model": "test-vision",
            "prompt_version": vision.IMAGE_PROMPT_VERSION,
            "review_status": "unreviewed",
            "physical_pages_known": False,
            "finish_reason": "stop",
        },
    )
    image = native_asset_ocr.assets(manifest)[0]
    uncertain = native_asset_ocr.ocr_native_assets_bounded(
        body, manifest, image_contents={image["asset_key"]: raw}
    )
    previous = native_asset_ocr.assets(uncertain)[0]
    original = service.archive.blobs.put_bytes("owner", body)
    with service.db.transaction() as conn:
        doc = service.archive.store_original(
            conn,
            "owner",
            original,
            "law.html",
            "text/html",
            "https://official.invalid/law",
            collection="country:ID",
            level="primary",
            tags=["country:ID"],
            identity_key="automatic-ocr-retry",
            kind_hint="law",
        )
        conn.execute("UPDATE documents SET manifest=?,status='failed' WHERE id=?", (dump(uncertain), doc["id"]))
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='failed',attempts=3,available_at=0 WHERE document_id=?",
            (doc["id"],),
        )
        service.collection.recovery.record_failure(
            conn,
            {"document_id": doc["id"], "owner_id": "owner", "phase": "parse", "attempts": 3},
            processing=True,
            decision=Decision("ocr_retry_budget_review"),
            exhausted=True,
            error_type="incomplete_checkpoint",
        )

    result = service.collection.recovery.run_due(limit=32)
    assert result["requeued"] == 1
    assert rows(service, "collection_recovery_events")[-2]["event"] == "automatic_ocr_retry_scheduled"
    with closing(service.db.connect()) as conn:
        row = conn.execute("SELECT manifest,status FROM documents WHERE id=?", (doc["id"],)).fetchone()
        job = conn.execute("SELECT phase,status FROM processing_jobs WHERE document_id=?", (doc["id"],)).fetchone()
    retried = native_asset_ocr.assets(json.loads(row["manifest"]))[0]
    assert row["status"] == "processing" and tuple(job) == ("parse", "queued")
    assert retried["asset_status"] == "pending_vision" and "ocr" not in retried
    assert retried["ocr_attempt_history"][0]["previous_asset"] == previous
    assert retried["ocr_attempt_history"][0]["previous_asset"]["ocr"]["text"] == previous["ocr"]["text"]


def test_embed_last_rejection_does_not_prove_entire_multibatch_call_was_unpaid():
    assert classify_processing({}, phase="embed", retryable=True).reason == "paid_result_unknown"


def test_only_complete_legacy_mineru_receipts_blocked_on_chart_content_are_refreshable(monkeypatch):
    from app.contexts.documents.infrastructure.providers import mineru as mineru

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
    from app.contexts.documents.infrastructure.providers import mineru as mineru
    from app.contexts.documents.infrastructure.providers import vision as vision

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
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed' WHERE id=?", (dump(manifest), doc["id"])
        )
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


def test_backup_restore_preserves_recovery_events_and_due_queue(service, tmp_path):
    from app.shared.infrastructure.operations.data_backup import create_backup, restore_backup, verify_backup

    failed_fetch(service)
    before = rows(service, "collection_recovery_events")
    backup = tmp_path / "backup"
    create_backup(service.db.data_dir, backup)
    assert verify_backup(backup)["schema_version"] == 22
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
        conn.execute("UPDATE sources SET enabled=0 WHERE owner_id='owner'")
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
        conn.execute("UPDATE sources SET enabled=0 WHERE owner_id='owner'")
        conn.execute("UPDATE sources SET enabled=1 WHERE id=?", (binding["source_id"],))
    collector = ScheduledCollector(service)
    assert collector.run_once()["failed"] == 0
    assert service.due_sources() == [] and rows(service, "collection_runs") == []
    source = next(row for row in rows(service, "sources") if row["id"] == binding["source_id"])
    assert source["last_attempt_at"] and source["last_error"]
