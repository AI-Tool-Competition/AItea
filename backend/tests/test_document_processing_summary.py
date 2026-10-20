"""Queue progress is a read-only observation, not permission to restart OCR."""

from contextlib import closing

import pytest

from app.db import dump, now
from app.document_processing import processing_details
from app.service import AppError, Service


@pytest.fixture
def service(tmp_path):
    value = Service(tmp_path)
    with value.db.transaction() as conn:
        for owner in ("alice", "bob"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, owner, None, 0, now()))
    return value


def document(service, *, manifest=None, status="processing", phase="parse", job="queued", lease=None):
    result = service.store_document("alice", "clause.txt", b"Existing legal body.", "text/plain")
    with service.db.transaction() as conn:
        if manifest is not None:
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), result["id"]))
        conn.execute("UPDATE documents SET status=? WHERE id=?", (status, result["id"]))
        conn.execute("DELETE FROM processing_jobs WHERE document_id=?", (result["id"],))
        if job:
            conn.execute(
                "INSERT INTO processing_jobs(document_id,owner_id,phase,status,available_at,lease_token,lease_until,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    result["id"],
                    "alice",
                    phase,
                    job,
                    1100,
                    "private-token" if lease is not None else None,
                    lease,
                    now(),
                ),
            )
    return result["id"]


@pytest.mark.parametrize(
    "status,job,lease,expected",
    [
        ("processing", "queued", None, "queued"),
        ("processing", "processing", 999, "queued"),
        ("processing", "processing", 1001, "running"),
        ("processing", "processing", None, "queued"),
        ("processing", None, None, "queued"),
        ("ready", "completed", None, "complete"),
        ("failed", "failed", None, "failed"),
    ],
)
def test_only_current_lease_reports_running(service, monkeypatch, status, job, lease, expected):
    monkeypatch.setattr("app.document_processing.time.time", lambda: 1000)
    identifier = document(service, status=status, job=job, lease=lease)
    result = service.document_detail("alice", identifier)
    detail = result["document"]["processing_detail"]
    assert detail["state"] == expected
    assert detail["retry_after_seconds"] == (100 if expected == "queued" and job else None)
    assert "private-token" not in dump(result)
    assert result["elements"][0]["content_text"] == "Existing legal body."


@pytest.mark.parametrize(
    "manifest,expected",
    [
        ({"pages": [{"status": "pending_ocr"}]}, "ocr_disabled"),
        ({"pages": [{"status": "ocr_uncertain"}]}, "needs_review"),
        ({"pages": [{"status": "source_incomplete"}]}, "needs_review"),
        ({"quality": {"review_required": True}}, "needs_review"),
        ({"quality": {"review_pages": [2]}}, "needs_review"),
        (
            {"pages": [{"status": "ocr_failed", "ocr": {"request_outcome": "unknown_transport_result"}}]},
            "failed",
        ),
        ({"assets_pending": [{"asset_status": "pending_vision"}]}, "ocr_disabled"),
        ({"html_source": {"linked_images": [{"asset_status": "ocr_uncertain"}]}}, "needs_review"),
        (
            {"legacy": {"conversion": {"images": [{"asset_status": "image_reference_pending"}]}}},
            "needs_review",
        ),
        ({"assets_pending": [{"asset_status": "ocr_failed"}]}, "failed"),
    ],
)
def test_quality_is_reported_without_mutating_requests(service, manifest, expected):
    identifier = document(service, manifest=manifest)
    with closing(service.db.connect()) as conn:
        before = dict(conn.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone())
        job_before = dict(
            conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (identifier,)).fetchone()
        )
        assert processing_details(conn, [identifier])[identifier]["state"] == expected
        assert dict(conn.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone()) == before
        assert (
            dict(conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (identifier,)).fetchone())
            == job_before
        )


def test_enabled_ocr_is_waiting_until_a_worker_claims_it(service, monkeypatch):
    monkeypatch.setattr("app.document_processing.vision_ocr_configured", lambda: True)
    identifier = document(service, manifest={"pages": [{"status": "pending_ocr"}]})
    assert service.document_detail("alice", identifier)["document"]["processing_detail"]["state"] == "queued"


def test_batch_is_one_bounded_read_and_owner_permissions_still_apply(service):
    identifier = document(service)
    with closing(service.db.connect()) as conn:
        statements = []
        conn.set_trace_callback(statements.append)
        assert processing_details(conn, [identifier, "absent"])[identifier]["state"] == "queued"
        assert len(statements) == 1
        assert conn.total_changes == 0
    with pytest.raises(AppError) as error:
        service.document_detail("bob", identifier)
    assert error.value.status == 404


def test_complete_body_waiting_for_embedding_keeps_phase(service):
    identifier = document(service, status="ready", phase="embed")
    assert service.document_detail("alice", identifier)["document"]["processing_detail"] == {
        "state": "queued",
        "phase": "embed",
        "retry_after_seconds": 0,
    }


def test_confirmed_complete_body_keeps_historical_parser_notice(service):
    identifier = document(service, manifest={"quality": {"complete": True}}, status="ready", job="completed")
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET error=? WHERE id=?", ("PDF原生文本不保证扫描页或复杂表格准确", identifier)
        )
    result = service.document_detail("alice", identifier)["document"]
    assert result["processing_detail"]["state"] == "complete"
    assert result["error"] == "PDF原生文本不保证扫描页或复杂表格准确"


def test_failed_unknown_page_is_visible_even_when_other_pages_await_disabled_ocr(service):
    manifest = {
        "pages": [
            {"status": "pending_ocr"},
            {"status": "ocr_failed", "ocr": {"request_outcome": "unknown_transport_result"}},
        ]
    }
    identifier = document(service, manifest=manifest)
    with closing(service.db.connect()) as conn:
        assert processing_details(conn, [identifier])[identifier]["state"] == "failed"
        assert conn.total_changes == 0


@pytest.mark.parametrize("ocr_enabled,expected", [(False, "ocr_disabled"), (True, "failed")])
def test_disabled_ocr_is_not_misreported_as_a_parser_failure(service, monkeypatch, ocr_enabled, expected):
    monkeypatch.setattr("app.document_processing.vision_ocr_configured", lambda: ocr_enabled)
    identifier = document(
        service, manifest={"pages": [{"status": "pending_ocr"}]}, status="failed", job="failed"
    )
    result = service.document_detail("alice", identifier)["document"]
    assert result["status"] == "failed"
    assert result["processing_detail"]["state"] == expected
