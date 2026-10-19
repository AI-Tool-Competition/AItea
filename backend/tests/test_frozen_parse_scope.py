"""An operator continuation cannot silently change its frozen ownership."""

import base64
import hashlib
import importlib.util
import json
import sqlite3
import time
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread

import pytest

SPEC = importlib.util.spec_from_file_location(
    "frozen_parse_runner", Path(__file__).parents[2] / "scripts/process_frozen_parse.py"
)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


@pytest.mark.parametrize("damage", [None, "ids", "documents", "directory", "owner"])
def test_frozen_input_hash_owner_and_database_guards(tmp_path, damage):
    data_dir = tmp_path / "data"
    directory = tmp_path / "scope"
    directory.mkdir()
    ids = json.dumps(["first", "second"]).encode()
    documents = json.dumps(
        [{"id": identifier, "owner_id": "shared"} for identifier in ("first", "second")]
    ).encode()
    report = {
        "initial_id_list_sha256": hashlib.sha256(ids).hexdigest(),
        "initial_documents_sha256": hashlib.sha256(documents).hexdigest(),
        "source_data_dir": str(data_dir),
        "owner_id": "shared",
        "initial_parse_queued_documents": 2,
    }
    (directory / "initial-parse-ids.json").write_bytes(ids + (b" " if damage == "ids" else b""))
    (directory / "initial-parse-documents.json").write_bytes(
        documents + (b" " if damage == "documents" else b"")
    )
    if damage == "owner":
        report["owner_id"] = "someone-else"
    (directory / "initial-report.json").write_text(json.dumps(report))
    if damage == "directory":
        data_dir = tmp_path / "different-data"
    if damage:
        with pytest.raises(ValueError):
            RUNNER.frozen_inputs(directory, data_dir)
    else:
        result, guarded = RUNNER.frozen_inputs(directory, data_dir)
        assert result == report and set(guarded) == {"first", "second"}


def test_local_lock_retry_waits_for_writer_without_repeating_success(tmp_path):
    database = tmp_path / "locked.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE heartbeat (value INTEGER)")
        conn.execute("INSERT INTO heartbeat VALUES (0)")
    locked, release = Event(), Event()

    def writer():
        with sqlite3.connect(database) as conn:
            conn.execute("BEGIN IMMEDIATE")
            locked.set()
            assert release.wait(5)

    thread = Thread(target=writer)
    thread.start()
    assert locked.wait(5)
    attempts, waits = [], []

    def local_transaction():
        attempts.append(True)
        with sqlite3.connect(database, timeout=0.01) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE heartbeat SET value=value+1")

    def on_wait(**record):
        waits.append(record)
        release.set()

    try:
        RUNNER.retry_local_database(local_transaction, on_wait=on_wait)
    finally:
        release.set()
        thread.join(5)
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM heartbeat").fetchone()[0] == 1
    assert len(attempts) == 2 and len(waits) == 1


@pytest.mark.parametrize("code,expected_attempts", [(sqlite3.SQLITE_BUSY, 3), (sqlite3.SQLITE_ERROR, 1)])
def test_local_retry_is_bounded_and_does_not_hide_other_errors(monkeypatch, code, expected_attempts):
    calls, waits = [], []
    monkeypatch.setattr(RUNNER.time, "sleep", lambda _: None)

    def fail():
        calls.append(True)
        error = sqlite3.OperationalError("controlled failure")
        error.sqlite_errorcode = code
        raise error

    with pytest.raises(sqlite3.OperationalError):
        RUNNER.retry_local_database(fail, on_wait=lambda **record: waits.append(record))
    assert len(calls) == expected_attempts and len(waits) == expected_attempts - 1


@pytest.mark.parametrize("change", [None, "missing", "text", "duplicate", "new_page"])
def test_successful_page_guard_detects_loss_and_allows_new_text(change):
    page = {"page": 1, "text": "Saved success", "status": "ocr_complete"}
    initial = [{"page": 1, "validated_status": "ocr_complete", "sha256": RUNNER.digest(page)}]
    current = [dict(page)]
    if change == "missing":
        current = []
    elif change == "text":
        current[0]["text"] = "Changed"
    elif change == "duplicate":
        current.append(dict(page))
    elif change == "new_page":
        current.append({"page": 2, "status": "native", "text": "New derived text"})
    if change in {"missing", "text", "duplicate"}:
        with pytest.raises(ValueError):
            RUNNER.verify_successful_pages(initial, current)
    else:
        RUNNER.verify_successful_pages(initial, current)


def test_expired_parse_lease_is_resumed_but_unknown_paid_page_is_not_resent(tmp_path, monkeypatch):
    from app import parsing, vision
    from app.db import dump, now
    from app.service import Service
    from test_ingestion_quality import make_pdf
    from test_pdf_ocr_reuse import register, stored

    for key, value in {
        "VISION_OCR_ENABLED": "true",
        "LLM_BASE_URL": "https://provider.invalid/v1",
        "LLM_API_KEY": "private-test-key",
        "LLM_MODEL": "test-model",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(parsing, "_OCR_SLOTS", parsing._VisionSlots())
    service = Service(tmp_path / "owned")
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("owner", "owner", None, None, 1, now()))
    content = make_pdf([("Immutable original page", True)])
    doc = register(service, content, key="interrupted")
    claim = service.collection.claim_processing(owner_id="owner", document_ids=[doc["id"]])
    manifest = stored(service, doc)["manifest"]
    page = manifest["pages"][0]
    page["ocr"] = {
        "attempts": 1,
        "request_id": "issued-before-crash",
        "source_hash": doc["content_hash"],
        "page": 1,
    }
    original_request_page = deepcopy(page)
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
        conn.execute(
            "UPDATE processing_jobs SET lease_until=? WHERE document_id=?", (time.time() - 1, doc["id"])
        )
    with service.db.connect() as conn:
        assert RUNNER.available_parse_ids(conn, "owner", time.time()) == {doc["id"]}
        assert RUNNER.available_parse_ids(conn, "other", time.time()) == set()
        row = conn.execute(
            "SELECT phase,status job_status,available_at,lease_until FROM processing_jobs WHERE document_id=?",
            (doc["id"],),
        ).fetchone()
        assert RUNNER.parse_job_due(row, time.time())
    resumed = RUNNER.retry_local_database(
        lambda: service.collection.claim_processing(owner_id="owner", document_ids=[doc["id"]]),
        on_wait=lambda **_: pytest.fail("unexpected storage wait"),
    )
    assert resumed and resumed["lease_token"] != claim["lease_token"]
    monkeypatch.setattr(vision, "transcribe_page", lambda *_: pytest.fail("unknown paid request resent"))
    service.collection.process_item(resumed)
    saved_page = stored(service, doc)["manifest"]["pages"][0]
    assert saved_page["status"] == "ocr_failed"
    assert saved_page["ocr"]["attempts"] == 1
    assert saved_page["ocr"]["request_outcome"] == "unknown_after_interruption"
    assert saved_page["ocr_attempt_history"][0]["previous_page"] == original_request_page
    with service.db.connect() as conn:
        assert RUNNER.available_parse_ids(conn, "owner", time.time()) == set()
        row = conn.execute(
            "SELECT status,lease_token FROM processing_jobs WHERE document_id=?", (doc["id"],)
        ).fetchone()
        assert row["status"] == "failed" and row["lease_token"] is None


@pytest.mark.parametrize(
    "phase,status,available,lease,expected",
    [
        ("parse", "queued", 10, None, True),
        ("parse", "queued", 20, None, False),
        ("parse", "processing", 0, 10, True),
        ("parse", "processing", 0, 20, False),
        ("parse", "processing", 0, None, False),
        ("parse", "failed", 0, 10, False),
        ("embed", "queued", 0, None, False),
    ],
)
def test_parse_candidate_checks_phase_availability_and_live_leases(phase, status, available, lease, expected):
    assert (
        RUNNER.parse_job_due(
            {"phase": phase, "job_status": status, "available_at": available, "lease_until": lease}, 15
        )
        is expected
    )


@pytest.mark.parametrize(
    "status,held", [(408, True), (500, True), (502, True), (504, True), (429, False), (400, False)]
)
def test_batch_policy_does_not_treat_upstream_timeout_as_known_unbilled_rejection(status, held):
    from app.vision import OCRResponseError

    original_evidence = {"content_hash": "saved-raw-body", "http_status": status}
    error = OCRResponseError(
        "Provider response",
        retryable=True,
        http_status=status,
        retry_after=70,
        response_evidence=original_evidence,
    )
    assert RUNNER.hold_ambiguous_response(error) is error
    assert error.request_outcome_unknown is held
    assert error.retryable is (not held)
    assert error.http_status == status and error.retry_after == 70
    assert error.response_evidence == original_evidence


def test_prior_gateway_error_is_held_with_identical_receipt_and_append_only_history():
    page = {
        "page": 1,
        "source_hash": "source",
        "status": "pending_ocr",
        "error": "Old gateway timeout",
        "deferred_reason": "provider_retry",
        "ocr": {
            "attempts": 1,
            "request_id": "old-request",
            "http_status": 504,
            "error_response": {"blob": {"content_hash": "immutable-receipt"}},
        },
    }
    before = deepcopy(page)
    held = RUNNER.hold_previous_ambiguous_page(page)
    assert page == before
    assert held["status"] == "ocr_failed"
    assert held["ocr_attempt_history"][0]["previous_page"] == before
    for field in ("attempts", "request_id", "http_status", "error_response"):
        assert held["ocr"][field] == before["ocr"][field]
    assert RUNNER.hold_previous_ambiguous_page(held) is None
    page["ocr"]["http_status"] = 429
    assert RUNNER.hold_previous_ambiguous_page(page) is None


def test_ambiguous_http_receipt_is_saved_once_and_never_automatically_reissued(tmp_path, monkeypatch):
    from app import parsing, vision
    from app.ingestion import parse_file
    from app.ocr_error_evidence import persist_response_evidence
    from app.service import Service
    from test_ingestion_quality import make_pdf

    for key, value in {
        "VISION_OCR_ENABLED": "true",
        "LLM_BASE_URL": "https://provider.invalid/v1",
        "LLM_API_KEY": "private-test-key",
        "LLM_MODEL": "test-model",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(parsing, "_OCR_SLOTS", parsing._VisionSlots())
    service = Service(tmp_path / "owned")
    content = make_pdf([("Original", True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    checkpoints, requests = [], []
    raw = b'{"error":{"message":"upstream timeout"}}'

    def invoke(body):
        requests.append(True)
        error = vision._response_error(
            "Gateway timeout",
            body,
            raw,
            error_code="http_error",
            payload_kind="http_body",
            retryable=True,
            retry_after=75,
            http_status=504,
        )
        raise RUNNER.hold_ambiguous_response(error)

    def store_error(evidence):
        return persist_response_evidence(
            service.archive.blobs,
            "owner",
            evidence,
            source_hash=page["source_hash"],
            request_page=checkpoints[-1],
        )

    monkeypatch.setattr(vision, "_invoke", invoke)
    held = parsing._ocr_one_page(
        content,
        page,
        on_page=lambda value: checkpoints.append(deepcopy(value)),
        on_response_error=store_error,
    )
    assert held["status"] == "ocr_failed" and held["ocr"]["attempts"] == 1
    assert held["ocr"]["request_outcome"] == "unknown_transport_result"
    ref = held["ocr"]["error_response"]
    envelope = json.loads((service.db.data_dir / ref["blob"]["storage_path"]).read_text())
    assert base64.b64decode(envelope["response_base64"]) == raw
    assert envelope["http_status"] == 504 and envelope["retry_after_seconds"] == 75
    assert envelope["request_outcome"] == "unknown_transport_result"
    assert parsing._ocr_one_page(content, held) == held and len(requests) == 1
