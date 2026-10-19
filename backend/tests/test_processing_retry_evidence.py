"""A generic queue retry cannot authorize another unresolved paid request."""

from copy import deepcopy
import json

import pytest

from app import parsing, vision
from app.db import dump
from test_ingestion_quality import make_pdf
from test_pdf_ocr_reuse import enabled as enabled, register, service as service, stored


@pytest.mark.parametrize(
    "outcome", ["unknown_transport_result", "unknown_after_interruption", "envelope", "region"]
)
def test_retry_does_not_reissue_unknown_paid_page(service, enabled, monkeypatch, outcome):
    content = make_pdf([("Preserve the source.", True)])
    doc = register(service, content, key="unknown-paid-retry")
    manifest = stored(service, doc)["manifest"]
    page = manifest["pages"][0]
    page.update(status="ocr_failed", error="Unresolved paid request")
    page["ocr"] = {"attempts": 1, "request_id": "original-request"}
    if outcome == "region":
        page["ocr"]["recovery"] = {"regions": [{"request_outcome": "unknown_transport_result"}]}
    elif outcome == "envelope":
        page["ocr"]["error_response"] = {"request_outcome": "unknown_transport_result"}
    else:
        page["ocr"]["request_outcome"] = outcome
    before = deepcopy(page)
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
        conn.execute("UPDATE processing_jobs SET status='failed' WHERE document_id=?", (doc["id"],))
        service.collection.queue_processing(conn, doc["id"], "owner", retry_ocr=True)
        updated = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
    calls = []

    def provider(_):
        calls.append(True)
        return {
            "text": "Preserve the source.",
            "printed_page": None,
            "is_blank": False,
            "complete": True,
            "uncertain_spans": [],
            "finish_reason": "stop",
        }

    monkeypatch.setattr(vision, "transcribe_page", provider)
    # The normal pending-page worker is the actual payment boundary. Calling
    # it directly also checks the same defensive guard on an explicit retry.
    result = parsing._ocr_one_page(content, updated["pages"][0])
    assert calls == []
    assert updated["pages"][0] == before
    assert result == before


def test_previously_reset_unknown_checkpoint_still_cannot_issue_a_paid_request(enabled, monkeypatch):
    page = {
        "page": 1,
        "source_hash": "a" * 64,
        "status": "pending_ocr",
        "ocr": {
            "attempts": 0,
            "request_id": "original-request",
            "request_outcome": "unknown_transport_result",
        },
    }
    monkeypatch.setattr(parsing, "render_pdf_page_bounded", lambda *_: pytest.fail("unknown rendered"))
    monkeypatch.setattr(vision, "transcribe_page", lambda *_: pytest.fail("unknown requested"))
    result = parsing._ocr_one_page(b"unread", page)
    assert result["status"] == "ocr_failed"
    assert result["ocr"]["request_id"] == "original-request"


def test_known_failed_retry_preserves_attempt_and_successful_page_evidence(service):
    doc = register(service, make_pdf([("One", True), ("Two", True)]), key="known-retry")
    manifest = stored(service, doc)["manifest"]
    success, failed = manifest["pages"]
    success.update(status="ocr_complete", ocr={"attempts": 1, "text": "One", "complete": True})
    failed.update(status="ocr_failed", error="Explicit provider rejection")
    failed["ocr"] = {
        "attempts": 3,
        "request_id": "rejected-request",
        "http_status": 429,
        "error_response": {"content_hash": "b" * 64},
        "retry_not_before": 1234567890,
    }
    before_success, before_failed = deepcopy(success), deepcopy(failed)
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
        conn.execute("UPDATE processing_jobs SET status='failed' WHERE document_id=?", (doc["id"],))
        service.collection.queue_processing(conn, doc["id"], "owner", retry_ocr=True)
    updated = stored(service, doc)["manifest"]["pages"]
    assert updated[0] == before_success
    assert updated[1]["status"] == "pending_ocr" and updated[1]["ocr"]["attempts"] == 0
    assert updated[1]["ocr"]["retry_not_before"] == before_failed["ocr"]["retry_not_before"]
    assert updated[1]["ocr_attempt_history"][-1]["previous_page"] == before_failed
