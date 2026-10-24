"""Segmented originals stay private and incomplete until one verified version is assembled."""

import hashlib
from contextlib import closing
from pathlib import Path
from threading import Lock

import pytest
from app.bootstrap import Service
from app.shared.infrastructure.http import download as ingestion_download


@pytest.fixture
def queued(tmp_path, monkeypatch):
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES('owner','owner',NULL,NULL,1,'2026-10-13')")
    source = service.save_source(
        "owner",
        {
            "name": "Official file",
            "url": "https://official.example/law.txt",
            "enabled": False,
            "interval_hours": 24,
            "tags": [],
            "plan": {"collection": "country:ID", "discover_attachments": False},
        },
    )
    run = service.collection.enqueue("owner", source["id"], {"client_request_id": "one"})
    return service, run, tmp_path


def row_count(service, table):
    with closing(service.db.connect()) as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def transport(monkeypatch, content=b"abcdefghijklmno", *, change_after=None):
    calls = []
    state = {"etag": '"version-1"', "content": content, "honor_ranges": True}

    def download(url, *, consume, byte_range=None, range_validator=None, on_headers=None, **kwargs):
        if change_after is not None and len(calls) == change_after:
            state.update(etag='"version-2"', content=b"123456789012345")
        body = state["content"]
        calls.append(byte_range)
        partial = byte_range is not None and range_validator == state["etag"] and state["honor_ranges"]
        start, end = byte_range if partial else (0, len(body) - 1)
        metadata = {
            "url": url,
            "filename": "law.txt",
            "mime_type": "text/plain",
            "etag": state["etag"],
            "last_modified": None,
            "total_size": len(body),
            "response_status": 206 if partial else 200,
            "accept_ranges": True,
            "range_start": start if partial else None,
            "range_end": end if partial else None,
        }
        if on_headers is not None:
            on_headers(metadata)
        return {**metadata, "blob": consume(iter([body[start : end + 1]]))}

    monkeypatch.setattr(ingestion_download, "_download", download)
    return calls, state


def tune(service):
    service.collection.transfers.minimum_size = 1
    service.collection.transfers.part_size = 4
    service.collection.transfers.slow_after = 0
    service.collection.transfers.minimum_rate = float("inf")


def drain(service, maximum=30):
    for _ in range(maximum):
        claim = service.collection.claim_fetch()
        if claim is None:
            break
        service.collection.fetch_item(claim)
    return service


def test_segments_checkpoint_across_restart_without_publishing_partial_original(queued, monkeypatch):
    service, run, path = queued
    calls, state = transport(monkeypatch)
    tune(service)
    service.collection.fetch_item(service.collection.claim_fetch())
    assert row_count(service, "documents") == 0
    assert row_count(service, "collection_transfers") == 1
    service.collection.fetch_item(service.collection.claim_fetch())
    assert row_count(service, "documents") == 0
    assert row_count(service, "collection_transfer_parts") == 2
    item = service.collection.run_detail("owner", run["id"])["items"][0]
    assert item["downloaded_bytes"] == 8 and item["download_total_bytes"] == 15
    assert item["status"] == "queued" and item["attempts"] == 0
    restarted = Service(path)
    tune(restarted)
    drain(restarted)
    assert calls.count((0, 3)) == 1 and calls.count((4, 7)) == 1
    with closing(restarted.db.connect()) as conn:
        doc = conn.execute("SELECT id,content_hash,version,storage_path FROM documents").fetchone()
    assert doc["content_hash"] == hashlib.sha256(state["content"]).hexdigest()
    assert doc["version"] == 1
    assert Path(doc["storage_path"]).read_bytes() == state["content"]


def test_changed_validator_never_combines_bytes_of_two_original_versions(queued, monkeypatch):
    service, _, _ = queued
    calls, state = transport(monkeypatch, change_after=2)
    tune(service)
    drain(service)
    with closing(service.db.connect()) as conn:
        doc = conn.execute("SELECT id,content_hash,storage_path FROM documents").fetchone()
        statuses = [r[0] for r in conn.execute("SELECT status FROM collection_transfers")]
    assert "superseded" in statuses
    assert Path(doc["storage_path"]).read_bytes() == state["content"]
    assert doc["content_hash"] == hashlib.sha256(state["content"]).hexdigest()


def test_progress_does_not_consume_network_failure_budget(queued, monkeypatch):
    service, run, _ = queued
    transport(monkeypatch, content=b"x" * 49)
    tune(service)
    drain(service)
    item = service.collection.run_detail("owner", run["id"])["items"][0]
    assert item["status"] == "stored" and item["downloaded_bytes"] == 49
    assert item["attempts"] <= 1 and row_count(service, "documents") == 1


def test_fast_original_is_not_split_into_unnecessary_range_requests(queued, monkeypatch):
    service, _, _ = queued
    calls, state = transport(monkeypatch)
    service.collection.transfers.minimum_size = 1
    service.collection.fetch_item(service.collection.claim_fetch())
    assert calls == [None]
    assert row_count(service, "documents") == 1
    assert row_count(service, "collection_transfers") == 0


def test_server_ignoring_range_falls_back_once_and_reports_the_completed_size(queued, monkeypatch):
    service, run, _ = queued
    calls, state = transport(monkeypatch)
    tune(service)
    service.collection.fetch_item(service.collection.claim_fetch())
    state["honor_ranges"] = False
    drain(service)
    item = service.collection.run_detail("owner", run["id"])["items"][0]
    assert item["status"] == "stored" and row_count(service, "documents") == 1
    assert calls.count(None) == 2
    assert item["downloaded_bytes"] == item["download_total_bytes"] == len(state["content"])


def test_modified_saved_part_cannot_publish_a_corrupted_original(queued, monkeypatch):
    service, run, _ = queued
    transport(monkeypatch)
    tune(service)
    service.collection.fetch_item(service.collection.claim_fetch())
    service.collection.fetch_item(service.collection.claim_fetch())
    with closing(service.db.connect()) as conn:
        path = conn.execute("SELECT storage_path FROM collection_transfer_parts LIMIT 1").fetchone()[0]
    Path(path).chmod(0o600)
    Path(path).write_bytes(b"xxxx")
    drain(service)
    assert row_count(service, "documents") == 0
    assert service.collection.run_detail("owner", run["id"])["items"][0]["status"] == "failed"


def test_saved_part_path_cannot_read_another_account_or_external_file(queued, monkeypatch):
    service, run, path = queued
    transport(monkeypatch)
    tune(service)
    service.collection.fetch_item(service.collection.claim_fetch())
    service.collection.fetch_item(service.collection.claim_fetch())
    outside = path / "different-owner-file"
    outside.write_bytes(b"must stay private")
    with service.db.transaction() as conn:
        conn.execute("UPDATE collection_transfer_parts SET storage_path=?", (str(outside),))
    drain(service)
    assert outside.read_bytes() == b"must stay private" and row_count(service, "documents") == 0
    assert service.collection.run_detail("owner", run["id"])["items"][0]["status"] == "failed"


def test_cancelled_segment_request_cannot_commit_or_publish_after_cancellation(queued, monkeypatch):
    service, run, _ = queued
    transport(monkeypatch)
    tune(service)
    service.collection.fetch_item(service.collection.claim_fetch())
    original = ingestion_download._download
    cancelled = False
    cancellation_lock = Lock()

    def cancel_then_download(*args, **kwargs):
        nonlocal cancelled
        # Both range workers must observe the committed cancellation, not just
        # another worker starting it; pre-cancellation checkpoints are valid.
        with cancellation_lock:
            if not cancelled:
                revision = service.collection.run_detail("owner", run["id"])["run"]["revision"]
                service.collection.control("owner", run["id"], "cancel", revision)
                cancelled = True
        return original(*args, **kwargs)

    monkeypatch.setattr(ingestion_download, "_download", cancel_then_download)
    service.collection.fetch_item(service.collection.claim_fetch())
    assert row_count(service, "documents") == row_count(service, "collection_transfer_parts") == 0
    assert service.collection.run_detail("owner", run["id"])["run"]["status"] == "cancelled"


def test_completed_checkpoint_survives_failure_before_document_transaction(queued, monkeypatch):
    service, run, path = queued
    calls, state = transport(monkeypatch)
    tune(service)
    monkeypatch.setattr(
        service.archive,
        "store_original",
        lambda *a, **kw: (_ for _ in ()).throw(ValueError("test transaction crash")),
    )
    drain(service)
    assert row_count(service, "documents") == 0
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT status FROM collection_transfers").fetchone()[0] == "complete"
    restarted = Service(path)
    tune(restarted)
    count = len(calls)
    revision = restarted.collection.run_detail("owner", run["id"])["run"]["revision"]
    restarted.collection.control("owner", run["id"], "retry", revision)
    drain(restarted)
    assert row_count(restarted, "documents") == 1 and len(calls) == count


def test_transfer_migration_preserves_existing_sources_and_is_idempotent(queued):
    service, _, path = queued
    with service.db.transaction() as conn:
        conn.execute("DROP TABLE collection_transfer_parts")
        conn.execute("DROP TABLE collection_transfers")
        conn.execute("DELETE FROM schema_migrations WHERE version=12")
    first = Service(path)
    second = Service(path)
    with closing(second.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=12").fetchone()[0] == 1
    assert row_count(first, "sources") == row_count(second, "sources") == 1
    assert row_count(second, "collection_runs") == 1
