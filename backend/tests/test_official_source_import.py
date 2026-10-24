"""Official source snapshots enter the existing versioned, bounded parse queue."""

import hashlib
import json
import time

from app.bootstrap import Service
from app.contexts.ingestion.application.official_source_import import OfficialSourceImporter
from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID
from app.interfaces.http.factory import create_app


def _write_version(
    data_dir, *, content: bytes, at_ns: int, purpose="document", url=None, mime_type="text/html"
):
    country_root = data_dir / "official-updates/ID"
    original = country_root / "files" / "original.html"
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    url = url or "https://jdih.pom.go.id/document/food-packaging"
    source_id = "id-bpom-regulations"
    event_dir = country_root / "events" / "2026-10-24"
    event_dir.mkdir(parents=True, exist_ok=True)
    event_path = event_dir / f"{at_ns}-receipt.json"
    event_path.write_text(
        json.dumps(
            {
                "at": time.time(),
                "kind": "content_version",
                "source_id": source_id,
                "url": url,
                "content_hash": digest,
                "path": "files/original.html",
                "legal_review": "unreviewed",
            }
        )
    )
    state = {
        "updated_at": time.time() + 1,
        "sources": {
            source_id: {
                "cache": {
                    url: {
                        "content_hash": digest,
                        "mime_type": mime_type,
                        "discovery": {"matched": True, "purpose": purpose},
                    }
                }
            }
        },
    }
    (country_root / "state.json").write_text(json.dumps(state))
    return event_path


def _importer(data_dir):
    config = data_dir / "test-source-pool.json"
    config.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "id-bpom-regulations",
                        "country": "ID",
                        "kind": "catalogue",
                        "allowed_hosts": ["jdih.pom.go.id"],
                    }
                ]
            }
        )
    )
    return Service(data_dir), config


def test_new_official_html_version_is_saved_once_and_queued_for_bounded_parse(tmp_path):
    service, config = _importer(tmp_path)
    content = b"<html><body><h1>Regulation</h1><p>Food packaging rules</p></body></html>"
    _write_version(tmp_path, content=content, at_ns=time.time_ns(), mime_type="application/octet-stream")
    importer = OfficialSourceImporter(service, config_path=config)

    assert importer.run_once() == {"stored": 1, "skipped": 0, "blocked": 0, "deferred": 0}
    assert importer.run_once() == {"stored": 0, "skipped": 0, "blocked": 0, "deferred": 0}

    with service.db.connect() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE owner_id=? AND source_url=?",
            (SYSTEM_OWNER_ID, "https://jdih.pom.go.id/document/food-packaging"),
        ).fetchone()
        job = conn.execute(
            "SELECT phase,status FROM processing_jobs WHERE document_id=?", (doc["id"],)
        ).fetchone()
        review = json.loads(
            conn.execute(
                "SELECT payload FROM document_metadata WHERE document_id=?", (doc["id"],)
            ).fetchone()[0]
        )
        imported = conn.execute(
            "SELECT COUNT(*) FROM official_source_imports WHERE status='stored'"
        ).fetchone()[0]
    assert doc["version"] == 1 and doc["status"] == "processing" and doc["mime_type"] == "text/html"
    assert job["phase"] == "parse" and job["status"] == "queued"
    assert review["review_status"] == "draft"
    assert imported == 1
    claim = service.collection.claim_processing()
    assert claim and claim["document_id"] == doc["id"]
    service.collection.process_item(claim)
    with service.db.connect() as conn:
        parsed = conn.execute("SELECT status,manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()
        completed = conn.execute(
            "SELECT phase,status FROM processing_jobs WHERE document_id=?", (doc["id"],)
        ).fetchone()
    parsed_manifest = json.loads(parsed["manifest"])
    assert parsed["status"] == "ready" and completed["status"] == "completed"
    assert completed["phase"] == "parse" and parsed_manifest["parser"] == "html-basic"
    assert parsed_manifest["html_source"]["source_hash"] == doc["content_hash"]
    assert "Food packaging rules" in " ".join(
        element.get("content_text", "") for element in parsed_manifest["elements"]
    )
    from app.contexts.ingestion.infrastructure.archive_inventory import export_inventory

    inventory = export_inventory(tmp_path, country="ID", owner=SYSTEM_OWNER_ID)
    receipt_file = next(inventory["path"].rglob("official-source-imports.jsonl"))
    receipts = [json.loads(line) for line in receipt_file.read_text().splitlines()]
    assert len(receipts) == 1 and receipts[0]["document_id"] == doc["id"]
    assert receipts[0]["status"] == "stored"
    assert receipts[0]["source_kind"] == "catalogue"
    assert receipts[0]["source_event_at"] > 0
    assert receipts[0]["declared_mime_type"] == "application/octet-stream"
    assert receipts[0]["mime_type"] == "text/html"
    assert receipts[0]["discovery_matched"] == 1
    assert receipts[0]["discovery_purpose"] == "document"
    assert receipts[0]["source_root"] == 0
    assert inventory["summary"]["official_source_import_statuses"] == {"stored": 1}


def test_first_import_replays_all_retained_version_history(tmp_path):
    service, config = _importer(tmp_path)
    _write_version(tmp_path, content=b"<h1>Historical regulation</h1>", at_ns=1)
    importer = OfficialSourceImporter(service, config_path=config)

    assert importer.run_once()["stored"] == 1
    with service.db.connect() as conn:
        assert (
            conn.execute("SELECT version FROM documents WHERE owner_id=?", (SYSTEM_OWNER_ID,)).fetchone()[0]
            == 1
        )


def test_transient_archive_failure_is_retried_without_losing_the_event(tmp_path, monkeypatch):
    service, config = _importer(tmp_path)
    _write_version(tmp_path, content=b"<h1>Regulation</h1>", at_ns=time.time_ns())
    importer = OfficialSourceImporter(service, config_path=config)
    put = service.archive.blobs.put
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("temporary archive failure")
        return put(*args, **kwargs)

    monkeypatch.setattr(service.archive.blobs, "put", fail_once)
    assert importer.run_once()["deferred"] == 1
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM official_source_imports").fetchone()[0] == 0
    assert importer.run_once()["stored"] == 1


def test_new_source_hash_creates_immutable_version_in_same_family(tmp_path):
    service, config = _importer(tmp_path)
    first = _write_version(tmp_path, content=b"<h1>Version 1</h1>", at_ns=time.time_ns())
    importer = OfficialSourceImporter(service, config_path=config)
    importer.run_once()
    second = _write_version(tmp_path, content=b"<h1>Version 2</h1>", at_ns=time.time_ns() + 1)

    assert importer.run_once()["stored"] == 1
    with service.db.connect() as conn:
        docs = conn.execute(
            "SELECT version,content_hash FROM documents WHERE owner_id=? AND source_url=? ORDER BY version",
            (SYSTEM_OWNER_ID, "https://jdih.pom.go.id/document/food-packaging"),
        ).fetchall()
        assert (
            conn.execute(
                "SELECT status FROM official_source_imports WHERE event_key=?",
                (first.relative_to(tmp_path).as_posix(),),
            ).fetchone()[0]
            == "stored"
        )
        assert (
            conn.execute(
                "SELECT status FROM official_source_imports WHERE event_key=?",
                (second.relative_to(tmp_path).as_posix(),),
            ).fetchone()[0]
            == "stored"
        )
    assert [row["version"] for row in docs] == [1, 2]
    assert docs[0]["content_hash"] != docs[1]["content_hash"]


def test_import_waits_for_collector_state_commit_before_advancing_cursor(tmp_path):
    service, config = _importer(tmp_path)
    event_path = _write_version(tmp_path, content=b"<h1>Regulation</h1>", at_ns=time.time_ns())
    state_path = tmp_path / "official-updates/ID/state.json"
    state = json.loads(state_path.read_text())
    state["updated_at"] = 0
    state_path.write_text(json.dumps(state))
    importer = OfficialSourceImporter(service, config_path=config)

    assert importer.run_once()["deferred"] == 1
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM official_source_imports").fetchone()[0] == 0
        assert conn.execute(
            "SELECT event_ns FROM official_source_import_cursor WHERE country='ID'"
        ).fetchone()[0] < int(event_path.name.split("-", 1)[0])

    state["updated_at"] = time.time() + 1
    state_path.write_text(json.dumps(state))
    assert importer.run_once()["stored"] == 1


def test_catalogue_page_and_out_of_scope_url_are_not_imported(tmp_path):
    service, config = _importer(tmp_path)
    event_path = _write_version(
        tmp_path, content=b"<h1>Search result list</h1>", at_ns=time.time_ns(), purpose="catalog"
    )
    outside_path = _write_version(
        tmp_path,
        content=b"<h1>Unapproved host</h1>",
        at_ns=time.time_ns() + 1,
        url="https://example.com/document",
    )
    importer = OfficialSourceImporter(service, config_path=config)
    result = importer.run_once()
    assert result["skipped"] == 2 and result["blocked"] == 0
    with service.db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM documents WHERE owner_id=?", (SYSTEM_OWNER_ID,)).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT status,reason FROM official_source_imports WHERE event_key=?",
                (event_path.relative_to(tmp_path).as_posix(),),
            ).fetchone()["status"]
            == "skipped"
        )
        blocked = conn.execute(
            "SELECT status,reason FROM official_source_imports WHERE event_key=?",
            (outside_path.relative_to(tmp_path).as_posix(),),
        ).fetchone()
        assert tuple(blocked) == ("skipped", "url_outside_source_scope")


def test_extensionless_detail_is_imported_only_after_pdf_signature_verification(tmp_path):
    service, config = _importer(tmp_path)
    content = b"%PDF-1.7\nverified official PDF bytes"
    _write_version(
        tmp_path,
        content=content,
        at_ns=time.time_ns(),
        purpose="detail",
        url="https://jdih.pom.go.id/api/peraturan?id=123",
        mime_type="application/pdf",
    )
    importer = OfficialSourceImporter(service, config_path=config)

    assert importer.run_once()["stored"] == 1
    with service.db.connect() as conn:
        doc = conn.execute(
            "SELECT mime_type,status FROM documents WHERE owner_id=?",
            (SYSTEM_OWNER_ID,),
        ).fetchone()
        receipt = conn.execute(
            "SELECT discovery_purpose,mime_type,status FROM official_source_imports"
        ).fetchone()
    assert tuple(doc) == ("application/pdf", "processing")
    assert tuple(receipt) == ("detail", "application/pdf", "stored")


def test_extensionless_detail_html_remains_a_catalogue_page(tmp_path):
    service, config = _importer(tmp_path)
    _write_version(
        tmp_path,
        content=b"<html><body><h1>Detail page</h1></body></html>",
        at_ns=time.time_ns(),
        purpose="detail",
        url="https://jdih.pom.go.id/document/detail",
        mime_type="text/html",
    )
    importer = OfficialSourceImporter(service, config_path=config)

    assert importer.run_once()["skipped"] == 1
    with service.db.connect() as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM documents WHERE owner_id=?", (SYSTEM_OWNER_ID,)).fetchone()[0]
            == 0
        )
        receipt = conn.execute("SELECT status,reason FROM official_source_imports").fetchone()
    assert tuple(receipt) == ("skipped", "catalogue_page_not_document")


def test_rechecks_older_skipped_detail_when_verified_mime_is_a_document(tmp_path):
    service, config = _importer(tmp_path)
    at_ns = time.time_ns()
    url = "https://jdih.pom.go.id/api/peraturan?id=456"
    _write_version(
        tmp_path,
        content=b"%PDF-1.7\nverified official PDF bytes",
        at_ns=at_ns,
        purpose="detail",
        url=url,
        mime_type="application/pdf",
    )
    importer = OfficialSourceImporter(service, config_path=config)
    event_key = f"official-updates/ID/events/2026-10-24/{at_ns}-receipt.json"
    with service.db.transaction() as conn:
        conn.execute(
            """INSERT INTO official_source_imports(
                event_key,source_id,country,source_url,content_hash,source_kind,mime_type,
                discovery_purpose,status,reason,created_at,updated_at
            ) VALUES(?,?,?,?,?,'catalogue','application/pdf','detail','skipped',
                'catalogue_page_not_document',?,?)""",
            (
                event_key,
                "id-bpom-regulations",
                "ID",
                url,
                hashlib.sha256(b"%PDF-1.7\nverified official PDF bytes").hexdigest(),
                "2026-10-24T00:00:00+00:00",
                "2026-10-24T00:00:00+00:00",
            ),
        )
        conn.execute("UPDATE official_source_import_cursor SET event_ns=? WHERE country='ID'", (at_ns + 1,))

    assert importer.run_once()["stored"] == 1
    with service.db.connect() as conn:
        receipt = conn.execute(
            "SELECT status,reason,document_id FROM official_source_imports WHERE event_key=?", (event_key,)
        ).fetchone()
        doc = conn.execute(
            "SELECT mime_type,status FROM documents WHERE id=?", (receipt["document_id"],)
        ).fetchone()
    assert tuple(receipt[:2]) == ("stored", None)
    assert tuple(doc) == ("application/pdf", "processing")


def test_api_health_exposes_official_import_cycle_and_shutdown(tmp_path, monkeypatch):
    from app.contexts.ingestion.application import collection_jobs as collection
    from app.shared.infrastructure.operations import scheduler as scheduler
    from fastapi.testclient import TestClient

    monkeypatch.setattr(scheduler, "start_scheduler", lambda *args, **kwargs: None)

    class Workers:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return self

        def stop(self):
            pass

    monkeypatch.setattr(collection, "CollectionWorkers", Workers)
    app = create_app(data_dir=tmp_path / "api", enable_scheduler=True)
    with TestClient(app) as client:
        report = client.get("/api/tea/health").json()["collector"]["official_source_import"]
        assert report["status"] == "running"
        assert report["last_cycle_at"]
        assert report["last_cycle_counts"] == {"stored": 0, "skipped": 0, "blocked": 0, "deferred": 0}
    assert app.state.service.collection.worker_health["official_source_import"]["status"] == "stopped"
