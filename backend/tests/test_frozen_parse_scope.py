"""An operator continuation cannot silently change its frozen ownership."""

import hashlib
import importlib.util
import json
import sqlite3
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
