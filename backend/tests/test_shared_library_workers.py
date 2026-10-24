"""External shared workers leave system evidence intact and private queues live."""

import socket
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event

import pytest
from app.bootstrap import Service
from app.contexts.ingestion.application.collection_jobs import CollectionWorkers
from app.contexts.ingestion.application.collection_recovery import Decision
from app.contexts.documents.application.system_library import (
    SYSTEM_OWNER_ID,
    BackgroundWorkerScope,
    ensure_principal,
)
from app.interfaces.http.factory import create_app
from app.shared.domain.errors import AppError
from app.shared.domain.values import now
from app.shared.infrastructure.operations.scheduler import ScheduledCollector
from fastapi.testclient import TestClient
from test_system_library import approve, original

pytestmark = pytest.mark.usefixtures("ragflow_library")

EXTERNAL = BackgroundWorkerScope("external")


@pytest.fixture
def service(tmp_path):
    result = Service(tmp_path / "data")
    with result.db.transaction() as conn:
        ensure_principal(conn)
        for owner in ("alice", "bob", "reviewer"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 0, now()))
    return result


def job(service, key, *, owner=SYSTEM_OWNER_ID, phase="parse", status="queued", attempts=0, lease=None):
    doc = original(service, key, owner=owner)
    with service.db.transaction() as conn:
        conn.execute(
            """UPDATE processing_jobs SET phase=?,status=?,attempts=?,lease_until=?,lease_token=?,updated_at=?
            WHERE document_id=?""",
            (
                phase,
                status,
                attempts,
                lease,
                "old-token" if lease is not None else None,
                "0000" if owner == SYSTEM_OWNER_ID else "9999",
                doc["id"],
            ),
        )
    return doc


def source(service, key, *, owner=SYSTEM_OWNER_ID, enabled=False, host="example.org"):
    return service.save_source(
        owner,
        {
            "name": key,
            "url": f"https://{host}/{key}.txt",
            "enabled": enabled,
            "interval_hours": 24,
            "tags": [],
            "plan": {"discover_attachments": False},
        },
    )


def fetch(service, key, *, owner=SYSTEM_OWNER_ID, status="queued", attempts=0, lease=None):
    src = source(
        service,
        key,
        owner=owner,
        host="system.example.org" if owner == SYSTEM_OWNER_ID else "private.example.org",
    )
    run = service.collection.enqueue(owner, src["id"], {"client_request_id": key})
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE collection_items SET status=?,attempts=?,lease_until=?,lease_token=? WHERE run_id=?",
            (status, attempts, lease, "old-token" if lease is not None else None, run["id"]),
        )
        return dict(conn.execute("SELECT * FROM collection_items WHERE run_id=?", (run["id"],)).fetchone())


def system_state(service):
    with closing(service.db.connect()) as conn:
        tables = (
            "documents",
            "processing_jobs",
            "sources",
            "collection_runs",
            "collection_items",
            "collection_recovery_jobs",
        )
        result = {
            table: [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM {table} WHERE owner_id=? ORDER BY rowid", (SYSTEM_OWNER_ID,)
                )
            ]
            for table in tables
        }
        result["events"] = [
            dict(r)
            for r in conn.execute(
                """SELECT e.* FROM collection_recovery_events e JOIN collection_recovery_jobs r
            ON r.id=e.recovery_id WHERE r.owner_id=? ORDER BY e.rowid""",
                (SYSTEM_OWNER_ID,),
            )
        ]
        return result


@pytest.mark.parametrize("phase", ["parse", "embed"])
def test_external_claim_ignores_system_candidates_and_private_capacity_is_independent(service, phase):
    # An external campaign holds twelve leases; the main worker still has two private slots.
    for number in range(12):
        job(service, f"active-{number}", phase="embed", status="processing", lease=time.time() + 600)
    job(service, "first-in-queue", phase=phase)
    for number in range(3):
        job(service, f"private-{number}", owner="alice", phase=phase)
    before = system_state(service)
    assert service.collection.claim_processing() is None  # Legacy global quota is unchanged.
    for _ in range(2):
        claimed = service.collection.claim_processing(worker_scope=EXTERNAL)
        assert claimed["owner_id"] == "alice" and claimed["phase"] == phase
    assert service.collection.claim_processing(worker_scope=EXTERNAL) is None
    assert system_state(service) == before


def test_environment_does_not_disable_explicit_system_runner_and_same_job_leases_exclude_peers(
    service, monkeypatch
):
    monkeypatch.setenv("TEA_SHARED_LIBRARY_WORKERS", "external")
    shared = job(service, "runner-only", phase="embed")
    private = job(service, "private", owner="alice")
    restarted = Service(service.db.data_dir)
    with ThreadPoolExecutor(max_workers=2) as pool:
        calls = [
            pool.submit(
                restarted.collection.claim_processing, owner_id=SYSTEM_OWNER_ID, document_ids=[shared["id"]]
            )
            for _ in range(2)
        ]
        claims = [call.result() for call in calls]
    assert sum(claim is not None for claim in claims) == 1
    assert service.collection.claim_processing(worker_scope=EXTERNAL)["document_id"] == private["id"]


@pytest.mark.parametrize("kind", ["processing", "fetch"])
def test_external_expiry_filters_before_limit_and_never_requeues_system_claims(service, kind):
    for number in range(35):
        if kind == "processing":
            job(service, f"expired-{number}", status="processing", attempts=3, lease=0)
        else:
            fetch(service, f"expired-{number}", status="fetching", attempts=3, lease=0)
    if kind == "processing":
        private = job(service, "private-expired", owner="alice", status="processing", lease=0)
        key = "document_id"
        claim = service.collection.claim_processing
    else:
        private = fetch(service, "private-expired", owner="alice", status="fetching", lease=0)
        key = "id"
        claim = service.collection.claim_fetch
    before = system_state(service)
    assert claim(worker_scope=EXTERNAL)[key] == private["id"]
    assert system_state(service) == before


def test_external_download_backpressure_ignores_system_parse_backlog(service):
    for number in range(201):
        job(service, f"backlog-{number}")
    fetch(service, "system-first")
    private = fetch(service, "private", owner="alice")
    before = system_state(service)
    assert service.collection.claim_fetch() is None
    assert service.collection.claim_fetch(worker_scope=EXTERNAL)["id"] == private["id"]
    assert system_state(service) == before


def test_external_fetch_capacity_is_private_but_same_host_and_cooldown_stay_global(service):
    shared = fetch(service, "active", status="fetching", lease=time.time() + 600)
    private = fetch(service, "private", owner="alice")
    service.collection.download_workers = 1
    assert service.collection.claim_fetch(worker_scope=EXTERNAL)["id"] == private["id"]
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE collection_items SET status='queued',lease_until=NULL WHERE id=?", (private["id"],)
        )
        conn.execute("UPDATE collection_items SET host=? WHERE id=?", (shared["host"], private["id"]))
    service.collection.host_workers = 1
    assert service.collection.claim_fetch(worker_scope=EXTERNAL) is None
    with service.db.transaction() as conn:
        conn.execute("UPDATE collection_items SET status='completed' WHERE id=?", (shared["id"],))
        conn.execute(
            "INSERT INTO collection_host_cooldowns(host,available_at) VALUES(?,?)",
            (shared["host"], time.time() + 600),
        )
    assert service.collection.claim_fetch(worker_scope=EXTERNAL) is None


def record_failed(service, doc, *, decision=Decision("transient_processing", True)):
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE processing_jobs SET status='failed',attempts=3 WHERE document_id=?", (doc["id"],)
        )
        row = dict(conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (doc["id"],)).fetchone())
        service.collection.recovery.record_failure(
            conn, row, processing=True, decision=decision, exhausted=True, error_type="fixture"
        )
        conn.execute("UPDATE collection_recovery_jobs SET available_at=0 WHERE item_id=?", (doc["id"],))


def test_external_recovery_preserves_system_waiting_blocked_and_legacy_evidence(service):
    for number in range(12):
        shared = job(service, f"waiting-{number}")
        record_failed(service, shared)
    unknown = job(service, "unknown-paid", phase="embed")
    record_failed(service, unknown, decision=Decision("paid_result_unknown"))
    job(service, "legacy-failed", status="failed", phase="embed")
    for number in range(201):
        job(service, f"backpressure-{number}")
    private = job(service, "private-failed", owner="alice")
    record_failed(service, private)
    before = system_state(service)
    result = service.collection.recovery.run_due(limit=1, worker_scope=EXTERNAL)
    assert result == {
        "adopted": 0,
        "requeued": 1,
        "blocked": 0,
        "deferred": 0,
        "reconciled": 0,
        "partially_reconciled": 0,
        "chart_refreshes": 0,
        "receipt_audit_errors": 0,
    }
    assert system_state(service) == before
    # Manual retry retains the existing unknown-outcome block even for an explicit operator.
    with pytest.raises(AppError) as error, service.db.transaction() as conn:
        service.collection.queue_processing(conn, unknown["id"], SYSTEM_OWNER_ID)
    assert error.value.code == "embedding_outcome_review_required"


def test_external_fetch_recovery_ignores_2000_system_backlog_and_preserves_unknown_ledger(service):
    waiting = fetch(service, "shared-waiting", status="failed", attempts=3)
    fetch(service, "shared-legacy", status="failed", attempts=3)
    private = fetch(service, "private-waiting", owner="alice", status="failed", attempts=3)
    with service.db.transaction() as conn:
        for row in (waiting, private):
            service.collection.recovery.record_failure(
                conn,
                row,
                processing=False,
                decision=Decision("network_transient", True),
                exhausted=True,
                error_type="fixture",
            )
        conn.execute("UPDATE collection_recovery_jobs SET available_at=0")
    unknown = job(service, "unknown-vector-result", phase="embed")
    record_failed(service, unknown, decision=Decision("paid_result_unknown"))
    backlog = fetch(service, "shared-backlog")
    # A persisted external campaign may already fill the global disk-admission
    # limit. Seed it in one fixture transaction without issuing any downloads.
    with service.db.transaction() as conn:
        for number in range(2000):
            service.collection._insert_item(
                conn,
                backlog["run_id"],
                SYSTEM_OWNER_ID,
                f"https://system.example.org/backlog-{number}.txt",
                "primary",
            )
        assert (
            conn.execute(
                "SELECT count(*) FROM collection_items WHERE owner_id=? AND status='queued'",
                (SYSTEM_OWNER_ID,),
            ).fetchone()[0]
            == 2001
        )
    before = system_state(service)
    assert service.collection.recovery.run_due(limit=1, worker_scope=EXTERNAL) == {
        "adopted": 0,
        "requeued": 1,
        "blocked": 0,
        "deferred": 0,
        "reconciled": 0,
        "partially_reconciled": 0,
        "chart_refreshes": 0,
        "receipt_audit_errors": 0,
    }
    assert service.collection.claim_fetch(worker_scope=EXTERNAL)["id"] == private["id"]
    assert system_state(service) == before
    # Even a stale caller handing a system row directly to resume is harmless.
    with service.db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM collection_recovery_jobs WHERE item_id=?", (unknown["id"],)
        ).fetchone()
        assert (
            service.collection.recovery._resume(conn, row, time.time(), worker_scope=EXTERNAL) == "deferred"
        )
    assert system_state(service) == before


def test_external_failed_fetch_adoption_ignores_earlier_system_rows(service):
    fetch(service, "system-legacy", status="failed", attempts=3)
    private = fetch(service, "private-legacy", owner="alice", status="failed", attempts=3)
    before = system_state(service)
    assert service.collection.recovery.run_due(limit=1, worker_scope=EXTERNAL) == {
        "adopted": 1,
        "requeued": 0,
        "blocked": 0,
        "deferred": 0,
        "reconciled": 0,
        "partially_reconciled": 0,
        "chart_refreshes": 0,
        "receipt_audit_errors": 0,
    }
    with closing(service.db.connect()) as conn:
        row = conn.execute(
            "SELECT * FROM collection_recovery_jobs WHERE item_id=?", (private["id"],)
        ).fetchone()
        assert row["status"] == "blocked" and row["reason"] == "legacy_failure_review"
    assert system_state(service) == before


@pytest.mark.parametrize("phase,reason", [("embed", "paid_result_unknown"), ("parse", "processing_review")])
def test_external_failed_adoption_filters_before_limit_and_retries_private_unknown_embeddings(
    service, phase, reason
):
    for number in range(12):
        job(service, f"system-failure-{number}", status="failed")
    private = job(service, "private-failure", owner="alice", status="failed", phase=phase)
    before = system_state(service)
    result = service.collection.recovery.run_due(limit=1, worker_scope=EXTERNAL)
    assert result["adopted"] == 1
    assert result["requeued"] == int(phase == "embed")
    with closing(service.db.connect()) as conn:
        row = conn.execute(
            "SELECT * FROM collection_recovery_jobs WHERE item_id=?", (private["id"],)
        ).fetchone()
        expected_status = "queued" if phase == "embed" else "blocked"
        expected_reason = "unstarted_ragflow_retry" if phase == "embed" else reason
        assert row["owner_id"] == "alice" and row["status"] == expected_status
        assert row["reason"] == expected_reason
    assert system_state(service) == before


def test_external_scheduled_sources_filter_before_100_limit_and_keep_private_collection(service):
    for number in range(101):
        source(service, f"system-{number}", enabled=True)
    private = source(service, "private", owner="alice", enabled=True)
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE sources SET last_attempt_at='2000-01-01T00:00:00+00:00' WHERE id=?", (private["id"],)
        )
    assert len(service.due_sources()) == 100
    assert all(row["owner_id"] == SYSTEM_OWNER_ID for row in service.due_sources())
    before = system_state(service)
    collector = ScheduledCollector(service, batch_size=1, worker_scope=EXTERNAL)
    assert collector.run_once() == {"succeeded": 1, "failed": 0}
    with closing(service.db.connect()) as conn:
        assert [row[0] for row in conn.execute("SELECT owner_id FROM collection_runs")] == ["alice"]
    assert system_state(service) == before


def test_external_worker_picks_new_private_owner_after_start_and_never_processes_system(service, monkeypatch):
    job(service, "shared-queued")
    before = system_state(service)
    completed = Event()
    worker = CollectionWorkers(service.collection, worker_scope=EXTERNAL, fetch_enabled=False)
    seen = []

    def process(claim):
        seen.append((claim["owner_id"], claim["document_id"]))
        completed.set()
        worker.stop_event.set()

    monkeypatch.setattr(service.collection, "process_item", process)
    worker.start()
    try:
        with service.db.transaction() as conn:
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("new", "new", None, None, 0, now()))
        private = job(service, "new-private", owner="new")
        assert completed.wait(5)
    finally:
        worker.stop(timeout=5)
    assert not worker.thread.is_alive()
    assert seen == [("new", private["id"])]
    assert system_state(service) == before
    assert service.collection.worker_health["shared_library_workers"] == "external"
    assert service.collection.worker_health["status"] == "stopped"


def test_main_injects_mode_in_both_workers_and_keeps_upload_and_shared_search(tmp_path, monkeypatch):
    from app.contexts.ingestion.application import collection_jobs as collection
    from app.shared.infrastructure.operations import scheduler as scheduler

    monkeypatch.setenv("TEA_SHARED_LIBRARY_WORKERS", "external")
    captured = {}

    def start_scheduler(service, *, enabled, worker_scope):
        captured["scheduler"] = worker_scope
        assert enabled

    class Workers:
        def __init__(self, service, *, worker_scope):
            captured["workers"] = worker_scope

        def start(self):
            return self

        def stop(self):
            pass

    monkeypatch.setattr(scheduler, "start_scheduler", start_scheduler)
    monkeypatch.setattr(collection, "CollectionWorkers", Workers)
    app = create_app(data_dir=tmp_path / "api", enable_scheduler=True)
    with TestClient(app) as client:
        collector = client.get("/api/tea/health").json()["collector"]
        assert collector == {
            "status": "stopped",
            "last_error_at": None,
            "shared_library_workers": "external",
            "official_source_import": {
                "status": "running",
                "last_cycle_at": collector["official_source_import"]["last_cycle_at"],
                "last_cycle_counts": {
                    "stored": 0,
                    "skipped": 0,
                    "blocked": 0,
                    "deferred": 0,
                },
                "last_error_at": None,
            },
        }
        assert collector["official_source_import"]["last_cycle_at"]
        assert captured["workers"] == captured["scheduler"] == EXTERNAL
        alice = client.post("/api/tea/auth/demo", json={}).json()["id"]
        uploaded = client.post(
            "/api/tea/documents", files={"file": ("private.txt", b"private cargo receipt", "text/plain")}
        )
        assert uploaded.status_code == 201
        ident = uploaded.json()["id"]
        with app.state.service.db.transaction() as conn:
            ensure_principal(conn)
            conn.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?)", ("reviewer", "Reviewer", None, None, 0, now())
            )
            assert conn.execute("SELECT owner_id FROM documents WHERE id=?", (ident,)).fetchone()[0] == alice
        shared = original(app.state.service, "shared-readable", body="lawful tea customs uniqueclause")
        approve(app.state.service, shared)
        draft = original(app.state.service, "draft", body="lawful tea customs uniqueclause")
        response = client.post(
            "/api/tea/search", json={"query": "uniqueclause", "country": "ID", "source_scope": "system"}
        )
        assert response.status_code == 200
        ids = {hit["document_id"] for hit in response.json()["items"]}
        assert shared["id"] in ids and draft["id"] not in ids
        client.post("/api/tea/auth/logout")
        client.post("/api/tea/auth/demo", json={})
        assert client.get(f"/api/tea/documents/{ident}/download").status_code == 404


def test_external_real_workers_finish_private_upload_without_touching_shared_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("TEA_SHARED_LIBRARY_WORKERS", "external")
    network_attempts = []

    def forbidden_connect(_socket, address):
        network_attempts.append(str(address))
        raise AssertionError("This local parsing smoke test must not connect to any network")

    monkeypatch.setattr(socket.socket, "connect", forbidden_connect)
    app = create_app(data_dir=tmp_path / "real-workers", enable_scheduler=True)
    service = app.state.service
    with service.db.transaction() as conn:
        ensure_principal(conn)
    job(service, "system-queued")
    fetch(service, "system-download-queued")
    before = system_state(service)
    raw = b"Private tea shipment receipt for bounded local parsing."
    with TestClient(app) as client:
        owner = client.post("/api/tea/auth/demo", json={}).json()["id"]
        response = client.post("/api/tea/documents", files={"file": ("private.txt", raw, "text/plain")})
        assert response.status_code == 201 and response.json()["status"] == "ready"
        ident = response.json()["id"]
        # TXT upload parses locally before responding. Queue its stored text via
        # the real service to exercise the actual coordinator and completion path
        # as well; neither parser, process_item nor CollectionWorkers is mocked.
        with service.db.transaction() as conn:
            service.collection.queue_processing(conn, ident, owner)
            assert (
                conn.execute("SELECT status FROM processing_jobs WHERE document_id=?", (ident,)).fetchone()[0]
                == "queued"
            )
        deadline = time.monotonic() + 10
        while True:
            with closing(service.db.connect()) as conn:
                queued = dict(
                    conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (ident,)).fetchone()
                )
            if queued["status"] == "completed":
                break
            assert time.monotonic() < deadline, queued
            time.sleep(0.02)
        assert queued["owner_id"] == owner
        assert client.get(f"/api/tea/documents/{ident}").json()["document"]["status"] == "ready"
        assert client.get(f"/api/tea/documents/{ident}/download").content == raw
        assert client.get("/api/tea/health").json()["collector"]["shared_library_workers"] == "external"
        assert system_state(service) == before
    assert service.collection.worker_health["status"] == "stopped"
    assert system_state(service) == before
    assert network_attempts == []


@pytest.mark.parametrize("value", ["", "off", "false", "system", "EXTERNALX"])
def test_invalid_main_mode_fails_before_database_creation(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TEA_SHARED_LIBRARY_WORKERS", value)
    path = tmp_path / "must-not-create"
    with pytest.raises(ValueError, match="TEA_SHARED_LIBRARY_WORKERS"):
        create_app(data_dir=path)
    assert not path.exists()


def test_default_mode_keeps_system_processing_and_health(service, tmp_path, monkeypatch):
    monkeypatch.delenv("TEA_SHARED_LIBRARY_WORKERS", raising=False)
    shared = job(service, "default-system")
    assert service.collection.claim_processing()["document_id"] == shared["id"]
    with TestClient(create_app(data_dir=tmp_path / "default")) as client:
        assert client.get("/api/tea/health").json()["collector"]["shared_library_workers"] == "internal"
