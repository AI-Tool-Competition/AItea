"""Durability and domain contracts for streaming legal collection."""

from contextlib import closing
from pathlib import Path
from threading import Event
from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Service


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        client.post("/api/tea/auth/demo", json={})
        yield client


def source(client, **plan):
    response = client.post(
        "/api/tea/sources",
        json={
            "name": "Legal test",
            "url": "https://example.org/main.txt",
            "enabled": False,
            "tags": ["country:ID", "checklist:3.3"],
            "plan": {"collection": "ID", "discover_attachments": False, **plan},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def run(client, source_id, key="first", **kwargs):
    response = client.post(f"/api/tea/sources/{source_id}/runs", json={"client_request_id": key, **kwargs})
    assert response.status_code == 202, response.text
    return response.json()


def upload(client, name="main.txt", body=b"tea export requirements"):
    response = client.post("/api/tea/documents", files={"file": (name, body, "text/plain")})
    assert response.status_code == 201, response.text
    return response.json()


def downloader(monkeypatch, payload=b"tea export requirements", before=None):
    from app import ingestion

    def download(url, *, consume, **kwargs):
        if before:
            before(url)
        return {
            "url": url,
            "filename": url.rsplit("/", 1)[-1],
            "mime_type": "text/plain",
            "blob": consume(iter([payload])),
            "etag": '"v1"',
            "last_modified": None,
        }

    monkeypatch.setattr(ingestion, "_download", download)


def test_submit_is_durable_idempotent_and_owner_scoped(client, tmp_path):
    src = source(client)
    first = run(client, src["id"])
    assert first["status"] == "queued" and first["total"] == 1
    assert run(client, src["id"])["id"] == first["id"]
    restarted = Service(tmp_path)
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    assert restarted.collection.run_detail(owner, first["id"])["run"]["total"] == 1
    client.post("/api/tea/auth/logout")
    client.post("/api/tea/auth/demo", json={})
    assert client.get(f"/api/tea/collection-runs/{first['id']}").status_code == 404
    assert (
        client.post(f"/api/tea/sources/{src['id']}/runs", json={"client_request_id": "x"}).status_code == 404
    )


def test_each_original_is_committed_before_slow_peer_or_parser(client, monkeypatch):
    src = source(client, seed_urls=["https://example.org/slow.txt"])
    job = run(client, src["id"])
    collection = client.app.state.service.collection
    release, started = Event(), Event()

    def before(url):
        if "slow" in url:
            started.set()
            assert release.wait(5)

    downloader(monkeypatch, before=before)
    first, second = collection.claim_fetch(), collection.claim_fetch()
    assert first and second
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(collection.fetch_item, second)
        try:
            assert started.wait(2)
            pool.submit(collection.fetch_item, first).result(timeout=3)
            detail = client.get(f"/api/tea/collection-runs/{job['id']}").json()
            assert detail["run"]["stored"] == 1
            doc = client.get("/api/tea/documents").json()["items"][0]
            assert doc["status"] == "processing"
            assert (
                client.get(f"/api/tea/documents/{doc['id']}/download").content == b"tea export requirements"
            )
        finally:
            release.set()
        slow.result(timeout=3)


def test_new_versions_keep_old_bytes_and_duplicate_skips_processing(client, monkeypatch):
    src = source(client)
    collection = client.app.state.service.collection
    downloader(monkeypatch, b"old clause")
    run(client, src["id"])
    collection.fetch_item(collection.claim_fetch())
    old = client.get("/api/tea/documents").json()["items"][0]
    run(client, src["id"], "repeat")
    collection.fetch_item(collection.claim_fetch())
    assert client.get("/api/tea/documents").json()["total"] == 1
    with closing(collection.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM processing_jobs").fetchone()[0] == 1
    downloader(monkeypatch, b"new clause")
    run(client, src["id"], "changed")
    collection.fetch_item(collection.claim_fetch())
    latest = client.get("/api/tea/documents").json()["items"][0]
    assert latest["version"] == 2 and old["id"] != latest["id"]
    versions = client.get(f"/api/tea/documents/{latest['id']}/versions").json()
    assert [doc["id"] for doc in versions["items"]] == [latest["id"], old["id"]]
    assert client.get(f"/api/tea/documents/{old['id']}/download").content == b"old clause"
    downloader(monkeypatch, b"old clause")
    run(client, src["id"], "reverted")
    collection.fetch_item(collection.claim_fetch())
    reverted = client.get("/api/tea/documents").json()["items"][0]
    assert reverted["version"] == 3 and reverted["id"] != old["id"]
    with closing(collection.db.connect()) as conn:
        paths = [
            row[0]
            for row in conn.execute(
                "SELECT storage_path FROM documents WHERE id IN (?,?)", (reverted["id"], old["id"])
            )
        ]
    assert len(set(paths)) == 1  # Version identity is distinct; bytes are shared.


def test_secondary_law_is_textified_and_many_to_many_but_not_indexed(client, monkeypatch):
    main1, main2 = upload(client), upload(client, "another.txt")
    src = source(client, level="secondary", primary_document_ids=[main1["id"], main2["id"]])
    downloader(monkeypatch)
    run(client, src["id"])
    collection = client.app.state.service.collection
    collection.fetch_item(collection.claim_fetch())
    from app import ingestion

    monkeypatch.setattr(ingestion, "embed_texts", lambda _: pytest.fail("secondary law embedded"))
    collection.process_item(collection.claim_processing())
    doc = client.get("/api/tea/documents").json()["items"][0]
    assert doc["legal_level"] == "secondary" and doc["chunk_count"] > 0
    assert doc["classifications"][0]["tags"] == ["country:ID", "checklist:3.3"]
    relations = client.get(f"/api/tea/documents/{doc['id']}/relations").json()
    assert {r["source_document_id"] for r in relations["items"]} == {main1["id"], main2["id"]}
    assert {r["target_document_id"] for r in relations["items"]} == {doc["id"]}


def test_classification_has_revision_and_attachment_cannot_be_legal_secondary(client):
    doc = upload(client)
    patch = {"collection": "ID", "level": "secondary", "tags": ["country:ID"], "expected_revision": 0}
    url = f"/api/tea/documents/{doc['id']}/classification"
    result = client.patch(url, json=patch)
    assert result.status_code == 200, result.text
    assert result.json()["legal_level"] == "secondary"
    assert client.patch(url, json=patch).status_code == 409
    attachment = client.post(
        "/api/tea/documents",
        data={"role": "secondary", "parent_id": doc["id"]},
        files={"file": ("note.txt", b"raw only", "text/plain")},
    ).json()
    assert (
        client.patch(f"/api/tea/documents/{attachment['id']}/classification", json=patch).status_code == 422
    )


def test_expired_claim_can_resume_but_old_worker_cannot_commit(client, monkeypatch):
    src = source(client)
    job = run(client, src["id"])
    collection = client.app.state.service.collection
    downloader(monkeypatch)
    old = collection.claim_fetch()
    with collection.db.transaction() as conn:
        conn.execute("UPDATE collection_items SET lease_until=0 WHERE id=?", (old["id"],))
    recovered = collection.claim_fetch()
    assert recovered and recovered["lease_token"] != old["lease_token"]
    collection.fetch_item(old)
    assert client.get("/api/tea/documents").json()["total"] == 0
    collection.fetch_item(recovered)
    assert client.get(f"/api/tea/collection-runs/{job['id']}").json()["run"]["stored"] == 1


def test_migration_preserves_existing_document_and_is_idempotent(tmp_path):
    from app.db import Database, now

    db = Database(tmp_path)
    original = db.files_dir / "old-file"
    original.write_bytes(b"old original")
    with db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES ('u','old',NULL,NULL,1,?)", (now(),))
        conn.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "d",
                "u",
                None,
                None,
                "old",
                "old.txt",
                "primary",
                "stored",
                "text/plain",
                12,
                1,
                "oldhash",
                str(original),
                "[]",
                None,
                None,
                '{"elements":[],"chunks":[]}',
                now(),
            ),
        )
    for _ in range(2):
        service = Service(tmp_path)
        doc = service.document_detail("u", "d")["document"]
        assert doc["family_id"]
        assert service.archive.versions("u", "d")["items"][0]["id"] == "d"
    assert original.read_bytes() == b"old original"
    assert list(Path(tmp_path).glob("files/old-file"))


def test_secondary_reused_as_primary_gets_index_without_reparse(client, monkeypatch):
    main = upload(client)
    secondary = source(client, level="secondary", primary_document_ids=[main["id"]])
    downloader(monkeypatch)
    run(client, secondary["id"])
    collection = client.app.state.service.collection
    collection.fetch_item(collection.claim_fetch())
    collection.process_item(collection.claim_processing())
    doc = client.get("/api/tea/documents").json()["items"][0]
    from app import ingestion, parsing

    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: pytest.fail("reparsed unchanged original"))
    monkeypatch.setattr(collection.service, "embedding_fingerprint", lambda: "configured")
    calls = []
    monkeypatch.setattr(
        ingestion, "embed_texts", lambda texts: calls.append(texts) or [[1.0, 0.0] for _ in texts]
    )
    primary = source(client, collection="SG")
    run(client, primary["id"], "promoted")
    collection.fetch_item(collection.claim_fetch())
    collection.process_item(collection.claim_processing())
    collection.process_item(collection.claim_processing())
    assert len(calls) == 1
    assert client.get("/api/tea/documents").json()["total"] == 2
    assert client.get(f"/api/tea/documents/{doc['id']}").json()["document"]["legal_level"] == "primary"


def test_empty_retry_keeps_partial_terminal_state(client, monkeypatch):
    src = source(client, max_items=1, discover_attachments=True)
    from app import ingestion

    def download(url, *, consume, **kwargs):
        return {
            "url": url,
            "filename": "main.html",
            "mime_type": "text/html",
            "blob": consume(iter([b'<p>tea law</p><a href="attachment.txt">attachment</a>'])),
        }

    monkeypatch.setattr(ingestion, "_download", download)
    job = run(client, src["id"])
    collection = client.app.state.service.collection
    collection.fetch_item(collection.claim_fetch())
    collection.process_item(collection.claim_processing())
    summary = client.get(f"/api/tea/collection-runs/{job['id']}").json()["run"]
    assert summary["status"] == "partial"
    response = client.post(
        f"/api/tea/collection-runs/{job['id']}/retry", json={"expected_revision": summary["revision"]}
    )
    assert response.status_code == 409
    assert client.get(f"/api/tea/collection-runs/{job['id']}").json()["run"]["status"] == "partial"


def test_coordinator_recovers_after_transient_database_error(client, monkeypatch):
    import sqlite3
    from app.collection import CollectionWorkers

    collection = client.app.state.service.collection
    attempted, recovered = Event(), Event()

    def claim():
        if not attempted.is_set():
            attempted.set()
            raise sqlite3.OperationalError("temporary contention")
        recovered.set()
        return None

    monkeypatch.setattr(collection, "claim_fetch", claim)
    worker = CollectionWorkers(collection).start()
    try:
        assert recovered.wait(4)
        assert worker.thread.is_alive()
        assert collection.worker_health["last_error_at"]
    finally:
        worker.stop()


def test_secondary_promoted_to_relation_anchor_is_indexed(client, monkeypatch):
    main = upload(client)
    src = source(client, level="secondary", primary_document_ids=[main["id"]])
    downloader(monkeypatch)
    collection = client.app.state.service.collection
    run(client, src["id"])
    collection.fetch_item(collection.claim_fetch())
    collection.process_item(collection.claim_processing())
    anchor = client.get("/api/tea/documents").json()["items"][0]
    target = upload(client, "related.txt")
    client.patch(
        f"/api/tea/documents/{target['id']}/classification",
        json={
            "collection": "SG",
            "level": "secondary",
            "tags": [],
            "expected_revision": 0,
        },
    ).raise_for_status()
    from app import ingestion, parsing

    monkeypatch.setattr(collection.service, "embedding_fingerprint", lambda: "current")
    calls = []
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: calls.append(texts) or [[1.0] for _ in texts])
    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: pytest.fail("reparsed anchor"))
    response = client.post(
        f"/api/tea/documents/{anchor['id']}/relations",
        json={
            "collection": "SG",
            "target_document_id": target["id"],
            "relation": "related",
            "locator": "",
        },
    )
    assert response.status_code == 201, response.text
    while claim := collection.claim_processing():
        collection.process_item(claim)
    assert len(calls) == 1


def test_cancelled_download_keeps_physical_host_slot_until_connection_exits(client, monkeypatch):
    from app import ingestion
    from urllib.parse import urlsplit
    from threading import Lock

    src = source(client)
    collection = client.app.state.service.collection
    collection.host_workers = 1
    job = run(client, src["id"])
    release, entered, waiting = Event(), Event(), Event()
    lock = Lock()
    active = peak = calls = 0

    def download(url, *, consume, connection_slot, **kwargs):
        nonlocal active, peak, calls
        with lock:
            calls += 1
            call = calls
        if call == 2:
            waiting.set()
        with connection_slot(urlsplit(url).hostname):
            with lock:
                active += 1
                peak = max(active, peak)
            try:
                if call == 1:
                    entered.set()
                    assert release.wait(5)
                return {
                    "url": url,
                    "filename": "main.txt",
                    "mime_type": "text/plain",
                    "blob": consume(iter([b"law"])),
                }
            finally:
                with lock:
                    active -= 1

    monkeypatch.setattr(ingestion, "_download", download)
    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(collection.fetch_item, collection.claim_fetch())
        try:
            assert entered.wait(3)
            report = client.get(f"/api/tea/collection-runs/{job['id']}").json()["run"]
            cancelled = client.post(
                f"/api/tea/collection-runs/{job['id']}/cancel", json={"expected_revision": report["revision"]}
            ).json()
            client.post(
                f"/api/tea/collection-runs/{job['id']}/retry",
                json={"expected_revision": cancelled["revision"]},
            ).raise_for_status()
            retried = pool.submit(collection.fetch_item, collection.claim_fetch())
            assert waiting.wait(3)
        finally:
            release.set()
        original.result(timeout=4)
        retried.result(timeout=4)
    assert peak == 1
    assert client.get(f"/api/tea/collection-runs/{job['id']}").json()["run"]["stored"] == 1


def test_competing_schedulers_submit_only_one_due_run(client, tmp_path):
    src = source(client)
    client.patch(
        f"/api/tea/sources/{src['id']}", json={"enabled": True, "expected_revision": src["revision"]}
    ).raise_for_status()
    services = [Service(tmp_path).collection for _ in range(2)]
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        submitted = list(
            pool.map(
                lambda i: services[i].enqueue(
                    owner, src["id"], {"client_request_id": f"schedule-{i}"}, scheduled=True
                ),
                range(2),
            )
        )
    assert sum(item is not None for item in submitted) == 1
    assert client.get(f"/api/tea/sources/{src['id']}/runs").json()["total"] == 1


def test_progress_does_not_conflict_with_cancel_but_retry_creates_new_control_revision(client):
    src = source(client)
    job = run(client, src["id"])
    client.app.state.service.collection.claim_fetch()
    cancelled = client.post(
        f"/api/tea/collection-runs/{job['id']}/cancel", json={"expected_revision": job["revision"]}
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["revision"] == job["revision"] + 1
    retried = client.post(
        f"/api/tea/collection-runs/{job['id']}/retry",
        json={"expected_revision": cancelled.json()["revision"]},
    )
    assert retried.status_code == 200
    assert retried.json()["revision"] == job["revision"] + 2
    assert (
        client.post(
            f"/api/tea/collection-runs/{job['id']}/cancel", json={"expected_revision": job["revision"]}
        ).status_code
        == 409
    )


@pytest.mark.parametrize("delay", [180, 900])
def test_retry_after_cools_actual_redirect_host(client, monkeypatch, delay):
    from app import ingestion

    src = source(client)
    run(client, src["id"])
    collection = client.app.state.service.collection

    def unavailable(*a, **kw):
        raise ingestion.DownloadError(
            "limited", retryable=True, retry_after=delay, response_host="limited.example"
        )

    monkeypatch.setattr(ingestion, "_download", unavailable)
    started = time.time()
    claim = collection.claim_fetch()
    collection.fetch_item(claim)
    with closing(collection.db.connect()) as conn:
        cooldowns = list(conn.execute("SELECT host,available_at FROM collection_host_cooldowns"))
        item = conn.execute("SELECT available_at FROM collection_items WHERE id=?", (claim["id"],)).fetchone()
    assert len(cooldowns) == 1 and cooldowns[0]["host"] == "limited.example"
    assert cooldowns[0]["available_at"] >= started + delay
    assert item["available_at"] >= started + delay
    assert collection.claim_fetch() is None


def test_promotion_during_parsing_queues_index_using_current_classification(client, monkeypatch):
    from app import ingestion, parsing

    main = upload(client)
    src = source(client, level="secondary", primary_document_ids=[main["id"]])
    downloader(monkeypatch)
    collection = client.app.state.service.collection
    run(client, src["id"])
    collection.fetch_item(collection.claim_fetch())
    doc = client.get("/api/tea/documents").json()["items"][0]
    started, release = Event(), Event()
    parse = parsing.parse_bounded

    def slow_parse(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return parse(*args, **kwargs)

    calls = []
    monkeypatch.setattr(parsing, "parse_bounded", slow_parse)
    monkeypatch.setattr(collection.service, "embedding_fingerprint", lambda: "configured")
    monkeypatch.setattr(
        ingestion, "embed_texts", lambda texts: calls.append(texts) or [[1.0, 0.0] for _ in texts]
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        processing = pool.submit(collection.process_item, collection.claim_processing())
        try:
            assert started.wait(3)
            response = client.patch(
                f"/api/tea/documents/{doc['id']}/classification",
                json={"collection": "ID", "level": "primary", "tags": [], "expected_revision": 1},
            )
            assert response.status_code == 200, response.text
        finally:
            release.set()
        processing.result(timeout=5)
    queued = collection.claim_processing()
    assert queued is not None and queued["phase"] == "embed"
    collection.process_item(queued)
    assert len(calls) == 1
    assert collection.claim_processing() is None
