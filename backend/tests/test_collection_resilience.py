"""Durable queue contracts across competing workers, restarts, and cancellation."""

import json
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app import ingestion, parsing
from app.collection import MAX_ATTEMPTS
from app.main import create_app
from app.service import Service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TEA_COLLECT_WORKERS", "4")
    monkeypatch.setenv("TEA_COLLECT_PER_HOST", "2")
    monkeypatch.setenv("TEA_PROCESS_WORKERS", "1")
    with TestClient(create_app(data_dir=tmp_path)) as client:
        assert client.post("/api/tea/auth/demo", json={}).status_code == 200
        yield client


def make_source(client, *, url="https://one.example/main.txt", **plan):
    response = client.post(
        "/api/tea/sources",
        json={
            "name": "Resilience source",
            "url": url,
            "enabled": False,
            "tags": ["country:ID"],
            "plan": {"collection": "ID", "discover_attachments": False, "max_items": 500, **plan},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def begin(client, source_id, key="request-1", entries=None):
    body = {"client_request_id": key}
    if entries is not None:
        body["entries"] = [{"url": entry} if isinstance(entry, str) else entry for entry in entries]
    response = client.post(f"/api/tea/sources/{source_id}/runs", json=body)
    assert response.status_code == 202, response.text
    return response.json()


def detail(client, job):
    response = client.get(f"/api/tea/collection-runs/{job['id']}")
    assert response.status_code == 200, response.text
    return response.json()


def rows(collection, sql, params=()):
    with closing(collection.db.connect()) as conn:
        return [dict(row) for row in conn.execute(sql, params)]


def fake_download(monkeypatch, before=None):
    calls = []

    def download(url, *, consume, **kwargs):
        calls.append(url)
        if before:
            before(url)
        return {
            "url": url,
            "filename": url.rsplit("/", 1)[-1],
            "mime_type": "text/plain",
            "blob": consume(iter([b"Original bytes remain available before processing."])),
            "etag": '"version-one"',
            "last_modified": None,
        }

    monkeypatch.setattr(ingestion, "_download", download)
    return calls


def manifest(count=1, prefix="clause"):
    return {
        "parser": "test-native-text",
        "warnings": [],
        "elements": [{"id": "element-1", "kind": "paragraph", "text": prefix, "locator": {"line": 1}}],
        "chunks": [
            {"id": f"chunk-{index}", "text": f"{prefix}-{index}", "locator": {"line": index + 1}}
            for index in range(count)
        ],
    }


def fetch_all(collection):
    while claim := collection.claim_fetch():
        collection.fetch_item(claim)


def test_global_fetch_capacity_is_shared_by_competing_service_instances(client, tmp_path):
    source = make_source(client)
    begin(client, source["id"], entries=[f"https://host-{index}.example/law.txt" for index in range(12)])
    services = [Service(tmp_path).collection for _ in range(3)]
    release = Event()

    def claim(index):
        assert release.wait(5)
        return services[index % len(services)].claim_fetch()

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [pool.submit(claim, index) for index in range(12)]
        release.set()
        claimed = [value for future in futures if (value := future.result(timeout=10)) is not None]
    assert len(claimed) == 4
    assert len({claim["id"] for claim in claimed}) == 4
    assert all(service.claim_fetch() is None for service in services)


def test_saturated_origin_does_not_block_other_origins_or_exceed_host_limit(client, tmp_path):
    source = make_source(client)
    begin(
        client,
        source["id"],
        entries=[f"https://same.example/law-{index}.txt" for index in range(6)]
        + [f"https://other.example/law-{index}.txt" for index in range(3)],
    )
    services = [Service(tmp_path).collection for _ in range(3)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(lambda index: services[index % 3].claim_fetch(), range(8)))
    active = [claim for claim in claimed if claim is not None]
    assert Counter(claim["host"] for claim in active) == {"same.example": 2, "other.example": 2}


def test_parser_capacity_is_shared_across_process_restarts(client, tmp_path, monkeypatch):
    collection = client.app.state.service.collection
    source = make_source(
        client, seed_urls=["https://one.example/second.txt", "https://one.example/third.txt"]
    )
    begin(client, source["id"])
    fake_download(monkeypatch)
    fetch_all(collection)
    services = [Service(tmp_path).collection for _ in range(3)]
    with ThreadPoolExecutor(max_workers=6) as pool:
        claims = list(pool.map(lambda index: services[index % 3].claim_processing(), range(6)))
    assert len([claim for claim in claims if claim]) == 1
    assert client.get("/api/tea/documents").json()["total"] == 3


def test_owner_queue_backpressure_rejects_atomically(client):
    source = make_source(client)
    entries = [f"https://one.example/law-{index}.txt" for index in range(200)]
    for index in range(5):
        begin(client, source["id"], key=f"batch-{index}", entries=entries)
    rejected = client.post(
        f"/api/tea/sources/{source['id']}/runs", json={"client_request_id": "one-too-many"}
    )
    assert rejected.status_code == 429
    collection = client.app.state.service.collection
    assert rows(collection, "SELECT COUNT(*) AS n FROM collection_items")[0]["n"] == 1000
    assert client.get(f"/api/tea/sources/{source['id']}/runs").json()["total"] == 5
    assert begin(client, source["id"], key="batch-0", entries=entries)["total"] == 200


def test_global_queue_backpressure_is_shared_between_owners(client):
    entries = [f"https://one.example/law-{index}.txt" for index in range(200)]
    for owner_index in range(2):
        if owner_index:
            client.post("/api/tea/auth/logout")
            client.post("/api/tea/auth/demo", json={})
        source = make_source(client)
        for index in range(5):
            begin(client, source["id"], key=f"batch-{index}", entries=entries)
    client.post("/api/tea/auth/logout")
    client.post("/api/tea/auth/demo", json={})
    source = make_source(client)
    response = client.post(
        f"/api/tea/sources/{source['id']}/runs", json={"client_request_id": "global-overflow"}
    )
    assert response.status_code == 429
    assert client.get(f"/api/tea/sources/{source['id']}/runs").json()["total"] == 0


def test_manual_retry_cannot_bypass_queue_backpressure(client, monkeypatch):
    source = make_source(client)
    failed_run = begin(client, source["id"], key="failed-first")

    def unavailable(url):
        raise ingestion.DownloadError("HTTP 404", retryable=False)

    fake_download(monkeypatch, before=unavailable)
    collection = client.app.state.service.collection
    collection.fetch_item(collection.claim_fetch())
    assert detail(client, failed_run)["run"]["failed"] == 1
    entries = [f"https://one.example/law-{index}.txt" for index in range(200)]
    for index in range(5):
        begin(client, source["id"], key=f"full-{index}", entries=entries)
    revision = detail(client, failed_run)["run"]["revision"]
    response = client.post(
        f"/api/tea/collection-runs/{failed_run['id']}/retry", json={"expected_revision": revision}
    )
    assert response.status_code == 429
    report = detail(client, failed_run)
    assert report["run"]["failed"] == 1
    assert report["run"]["revision"] == revision
    assert (
        rows(collection, "SELECT COUNT(*) AS n FROM collection_items WHERE status IN ('queued','fetching')")[
            0
        ]["n"]
        == 1000
    )


def test_discovery_obeys_the_same_queue_budget_as_initial_submission(client, monkeypatch):
    source = make_source(client, discover_attachments=True)
    entries = [f"https://one.example/law-{index}.html" for index in range(200)]
    for index in range(5):
        begin(client, source["id"], key=f"batch-{index}", entries=entries)

    def download(url, *, consume, **kwargs):
        body = "".join(f'<a href="/attachment-{index}.pdf">file</a>' for index in range(5)).encode()
        return {"url": url, "filename": "source.html", "mime_type": "text/html", "blob": consume([body])}

    monkeypatch.setattr(ingestion, "_download", download)
    collection = client.app.state.service.collection
    collection.fetch_item(collection.claim_fetch())
    queued = rows(
        collection, "SELECT COUNT(*) AS n FROM collection_items WHERE status IN ('queued','fetching')"
    )[0]["n"]
    assert queued <= 1000, "discovered attachments must not bypass admission limits"
    assert client.get("/api/tea/documents").json()["total"] == 1


def test_processing_backlog_pauses_downloads_and_resumes_after_drain(client, monkeypatch):
    source = make_source(client)
    begin(client, source["id"], entries=[f"https://one.example/{index}.txt" for index in range(200)])
    begin(client, source["id"], key="after-backlog", entries=["https://one.example/pending.txt"])
    fake_download(monkeypatch)
    collection = client.app.state.service.collection
    for _ in range(200):
        claim = collection.claim_fetch()
        assert claim is not None
        collection.fetch_item(claim)
    assert collection.claim_fetch() is None
    assert client.get("/api/tea/documents").json()["total"] == 200
    monkeypatch.setattr(parsing, "parse_bounded", lambda *args, **kwargs: manifest())
    collection.process_item(collection.claim_processing())
    assert collection.claim_fetch() is not None


def test_fetch_crash_leases_stop_retrying_after_exhaustion(client, tmp_path):
    source = make_source(client)
    job = begin(client, source["id"])
    collection = client.app.state.service.collection
    tokens = set()
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claim = collection.claim_fetch()
        assert claim and claim["attempts"] == attempt
        assert claim["lease_token"] not in tokens
        tokens.add(claim["lease_token"])
        with collection.db.transaction() as conn:
            conn.execute("UPDATE collection_items SET lease_until=0 WHERE id=?", (claim["id"],))
        collection = Service(tmp_path).collection
    assert collection.claim_fetch() is None
    report = detail(client, job)
    assert report["run"]["status"] == "partial"
    assert report["run"]["failed"] == 1 and report["run"]["pending"] == 0
    assert report["items"][0]["attempts"] == MAX_ATTEMPTS
    assert client.get("/api/tea/documents").json()["total"] == 0


def test_cancelled_inflight_worker_cannot_publish_after_completion(client, monkeypatch):
    collection = client.app.state.service.collection
    source = make_source(client)
    job = begin(client, source["id"])
    started, release = Event(), Event()

    def before(url):
        started.set()
        assert release.wait(5)

    fake_download(monkeypatch, before=before)
    claim = collection.claim_fetch()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collection.fetch_item, claim)
        try:
            assert started.wait(5)
            revision = detail(client, job)["run"]["revision"]
            result = client.post(
                f"/api/tea/collection-runs/{job['id']}/cancel", json={"expected_revision": revision}
            )
            assert result.status_code == 200, result.text
        finally:
            release.set()
        future.result(timeout=5)
    assert detail(client, job)["run"]["status"] == "cancelled"
    assert client.get("/api/tea/documents").json()["total"] == 0
    assert rows(collection, "SELECT COUNT(*) AS n FROM processing_jobs")[0]["n"] == 0


def test_retry_preserves_successful_items_and_only_downloads_failures(client, monkeypatch):
    source = make_source(client)
    good, bad = "https://one.example/good.txt", "https://one.example/bad.txt"
    job = begin(client, source["id"], entries=[good, bad])
    failed = True

    def before(url):
        if url == bad and failed:
            raise ingestion.DownloadError("HTTP 404", retryable=False)

    downloads = fake_download(monkeypatch, before=before)
    monkeypatch.setattr(parsing, "parse_bounded", lambda *args, **kwargs: manifest())
    collection = client.app.state.service.collection
    fetch_all(collection)
    collection.process_item(collection.claim_processing())
    original = client.get("/api/tea/documents").json()["items"][0]
    first = detail(client, job)
    assert first["run"]["status"] == "partial"
    response = client.post(
        f"/api/tea/collection-runs/{job['id']}/retry", json={"expected_revision": first["run"]["revision"]}
    )
    assert response.status_code == 200, response.text
    failed = False
    fetch_all(collection)
    collection.process_item(collection.claim_processing())
    assert Counter(downloads) == {good: 1, bad: 2}
    assert detail(client, job)["run"]["status"] == "completed"
    documents = client.get("/api/tea/documents").json()
    assert documents["total"] == 2
    preserved = next(doc for doc in documents["items"] if doc["id"] == original["id"])
    assert preserved["version"] == original["version"] == 1


def test_idempotency_key_conflicts_without_creating_extra_work(client):
    source = make_source(client)
    original = begin(client, source["id"], entries=["https://one.example/one.txt"])
    conflict = client.post(
        f"/api/tea/sources/{source['id']}/runs",
        json={"client_request_id": "request-1", "entries": [{"url": "https://one.example/two.txt"}]},
    )
    assert conflict.status_code == 409
    assert client.get(f"/api/tea/sources/{source['id']}/runs").json()["total"] == 1
    assert detail(client, original)["items"][0]["url"] == "https://one.example/one.txt"


def test_other_owner_cannot_inspect_cancel_retry_or_reuse_run(client):
    source = make_source(client)
    job = begin(client, source["id"])
    client.post("/api/tea/auth/logout")
    client.post("/api/tea/auth/demo", json={})
    for url in (f"/api/tea/collection-runs/{job['id']}", f"/api/tea/sources/{source['id']}/runs"):
        assert client.get(url).status_code == 404
    for action in ("retry", "cancel"):
        response = client.post(
            f"/api/tea/collection-runs/{job['id']}/{action}", json={"expected_revision": job["revision"]}
        )
        assert response.status_code == 404
    response = client.post(f"/api/tea/sources/{source['id']}/runs", json={"client_request_id": "request-1"})
    assert response.status_code == 404


def test_retry_after_pauses_same_host_durably_but_other_hosts_progress(client, tmp_path, monkeypatch):
    source = make_source(client)
    first, sibling, other = (
        "https://limited.example/first.txt",
        "https://limited.example/second.txt",
        "https://available.example/law.txt",
    )
    begin(client, source["id"], entries=[first, sibling, other])

    def before(url):
        if url == first:
            raise ingestion.DownloadError("HTTP 429", retryable=True, retry_after=180)

    fake_download(monkeypatch, before=before)
    collection = client.app.state.service.collection
    claim = collection.claim_fetch()
    assert claim["url"] == first
    before_attempt = time.time()
    collection.fetch_item(claim)
    cooldown = rows(collection, "SELECT available_at FROM collection_host_cooldowns")[0]["available_at"]
    assert cooldown >= before_attempt + 180
    restarted = Service(tmp_path).collection
    available = restarted.claim_fetch()
    assert available["url"] == other
    assert restarted.claim_fetch() is None
    with collection.db.transaction() as conn:
        conn.execute("UPDATE collection_host_cooldowns SET available_at=0")
        conn.execute("UPDATE collection_items SET available_at=0")
    resumed = restarted.claim_fetch()
    assert resumed and resumed["host"] == "limited.example"


def test_expired_parser_cannot_overwrite_recovered_worker_result(client, tmp_path, monkeypatch):
    source = make_source(client)
    begin(client, source["id"])
    fake_download(monkeypatch)
    collection = client.app.state.service.collection
    collection.fetch_item(collection.claim_fetch())
    started, release = Event(), Event()
    calls = []

    def parse(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            started.set()
            assert release.wait(5)
            return manifest(prefix="stale")
        return manifest(prefix="recovered")

    monkeypatch.setattr(parsing, "parse_bounded", parse)
    expired = collection.claim_processing()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(collection.process_item, expired)
        try:
            assert started.wait(5)
            with collection.db.transaction() as conn:
                conn.execute(
                    "UPDATE processing_jobs SET lease_until=0 WHERE document_id=?", (expired["document_id"],)
                )
            restarted = Service(tmp_path).collection
            recovered = restarted.claim_processing()
            assert recovered["lease_token"] != expired["lease_token"]
            restarted.process_item(recovered)
        finally:
            release.set()
        future.result(timeout=5)
    persisted = rows(
        collection, "SELECT manifest,status FROM documents WHERE id=?", (expired["document_id"],)
    )[0]
    assert json.loads(persisted["manifest"])["elements"][0]["text"] == "recovered"
    assert persisted["status"] == "ready"


def test_parser_crash_exhaustion_preserves_downloadable_original(client, tmp_path, monkeypatch):
    source = make_source(client)
    job = begin(client, source["id"])
    fake_download(monkeypatch)
    collection = client.app.state.service.collection
    collection.fetch_item(collection.claim_fetch())
    document = client.get("/api/tea/documents").json()["items"][0]
    original = client.get(f"/api/tea/documents/{document['id']}/download").content
    for attempt in range(1, MAX_ATTEMPTS + 1):
        claim = collection.claim_processing()
        assert claim and claim["attempts"] == attempt
        with collection.db.transaction() as conn:
            conn.execute("UPDATE processing_jobs SET lease_until=0 WHERE document_id=?", (document["id"],))
        collection = Service(tmp_path).collection
    assert collection.claim_processing() is None
    assert detail(client, job)["run"]["processing_failed"] == 1
    assert detail(client, job)["run"]["status"] == "partial"
    assert client.get(f"/api/tea/documents/{document['id']}/download").content == original


def test_embedding_batches_resume_after_restart_without_reembedding_committed_chunks(
    client, tmp_path, monkeypatch
):
    for name, value in {
        "EMBEDDING_BASE_URL": "https://embedding.example/v1",
        "EMBEDDING_API_KEY": "test-only",
        "EMBEDDING_MODEL": "test-embedding",
    }.items():
        monkeypatch.setenv(name, value)
    source = make_source(client)
    job = begin(client, source["id"])
    fake_download(monkeypatch)
    parses, batches = [], []

    def parse(*args, **kwargs):
        parses.append(True)
        return manifest(count=130)

    fail_once = False

    def embed(texts):
        nonlocal fail_once
        batches.append(list(texts))
        if fail_once:
            fail_once = False
            raise ValueError("provider unavailable")
        return [[float(int(text.rsplit("-", 1)[-1]) + 1), 1.0] for text in texts]

    monkeypatch.setattr(parsing, "parse_bounded", parse)
    monkeypatch.setattr(ingestion, "embed_texts", embed)
    collection = client.app.state.service.collection
    collection.fetch_item(collection.claim_fetch())
    document = client.get("/api/tea/documents").json()["items"][0]
    assert document["status"] == "processing"
    assert client.get(f"/api/tea/documents/{document['id']}/download").status_code == 200
    collection.process_item(collection.claim_processing())
    collection.process_item(collection.claim_processing())
    saved = json.loads(
        rows(collection, "SELECT manifest FROM documents WHERE id=?", (document["id"],))[0]["manifest"]
    )
    assert sum(bool(chunk.get("embedding")) for chunk in saved["chunks"]) == 64
    restarted = Service(tmp_path).collection
    fail_once = True
    restarted.process_item(restarted.claim_processing())
    assert restarted.claim_processing() is None
    with collection.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET available_at=0 WHERE document_id=?", (document["id"],))
    while claim := restarted.claim_processing():
        restarted.process_item(claim)
    assert len(parses) == 1
    assert [len(batch) for batch in batches] == [64, 64, 64, 2]
    assert batches[1] == batches[2]
    assert set(batches[0]).isdisjoint(text for batch in batches[1:] for text in batch)
    saved = json.loads(
        rows(collection, "SELECT manifest FROM documents WHERE id=?", (document["id"],))[0]["manifest"]
    )
    assert sum(bool(chunk.get("embedding")) for chunk in saved["chunks"]) == 130
    assert detail(client, job)["run"]["status"] == "completed"


def test_304_for_a_different_final_resource_cannot_reuse_cached_original(client, monkeypatch):
    source = make_source(client)
    collection = client.app.state.service.collection
    fake_download(monkeypatch)
    begin(client, source["id"])
    collection.fetch_item(collection.claim_fetch())
    repeated = begin(client, source["id"], key="redirected-cache")
    monkeypatch.setattr(
        ingestion,
        "_download",
        lambda *args, **kwargs: {"not_modified": True, "url": "https://other.example/different-law.txt"},
    )
    collection.fetch_item(collection.claim_fetch())
    report = detail(client, repeated)
    assert report["run"]["failed"] == 1
    assert report["run"]["stored"] == 0
    assert client.get("/api/tea/documents").json()["total"] == 1
