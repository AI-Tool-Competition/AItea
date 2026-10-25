"""Official source snapshots enter the existing versioned, bounded parse queue."""

import hashlib
import json
import time

from app.bootstrap import Service
from app.contexts.ingestion.application.official_source_import import (
    OfficialSourceImporter,
    _sso_catalogue_evidence,
)
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


def test_official_html_image_parse_continues_across_bounded_asset_batches(tmp_path, monkeypatch):
    from contextlib import closing

    from app.contexts.documents.application.system_library import ensure_principal
    from app.contexts.documents.infrastructure.providers import vision
    from app.shared.domain.values import now
    from app.shared.infrastructure.http import download as ingestion_download
    from test_html_image_collection import document, image
    from test_html_linked_asset_ocr import transcript

    service, config = _importer(tmp_path)
    OfficialSourceImporter(service, config_path=config)
    source_url = "https://www.ccs.gov.sg/consumer-protection/fair-trading-practices/about-fair-trading-practices/"
    asset_host = "isomer-user-content.by.gov.sg"
    body = (
        "<main><h1>Official image guidance</h1><p>Review each example.</p>"
        + "".join(f'<img src="https://{asset_host}/assets/example-{index}.png">' for index in range(5))
        + "</main>"
    )
    with service.db.transaction() as conn:
        ensure_principal(conn)
    doc = document(
        service,
        owner=SYSTEM_OWNER_ID,
        name="ccs-batched-images",
        source_url=source_url,
        html=body,
    )
    with service.db.transaction() as conn:
        conn.execute(
            """INSERT INTO official_source_imports(
            event_key,source_id,country,source_url,content_hash,mime_type,document_id,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,? ,?,'stored',?,?)""",
            (
                "ccs-batched-images",
                "sg-ccs-fair-trading",
                "SG",
                source_url,
                doc["content_hash"],
                "text/html",
                doc["id"],
                now(),
                now(),
            ),
        )
        service.collection.queue_processing(conn, doc["id"], SYSTEM_OWNER_ID)
    monkeypatch.setattr(
        service.collection,
        "_official_html_asset_hosts",
        lambda _document: [asset_host],
    )
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    calls, downloads = [], []
    monkeypatch.setattr(vision, "transcribe_image", lambda value: (calls.append(value), transcript())[1])
    monkeypatch.setenv("COLLECTION_PROXY_URL", "http://127.0.0.1:8030")
    monkeypatch.setenv("COLLECTION_PROXY_HOSTS", f"www.ccs.gov.sg,{asset_host}")

    def download(url, *, required_host, deadline, connection_slot, consume, max_bytes, **_options):
        downloads.append((url, required_host))
        assert required_host == asset_host and deadline > time.monotonic() and max_bytes == 8 * 1024 * 1024
        with connection_slot(required_host):
            blob = consume([image()])
        return {
            "url": url,
            "blob": blob,
            "mime_type": "image/png",
            "response_status": 200,
            "request_method": "GET",
        }

    monkeypatch.setattr(ingestion_download, "_download", download)
    failures = []
    original_failure = service.collection._failure

    def record_failure(claim, error, *, processing):
        failures.append(error)
        original_failure(claim, error, processing=processing)

    monkeypatch.setattr(service.collection, "_failure", record_failure)
    for index, expected_batch in enumerate((2, 2, 1)):
        if index:
            with service.db.transaction() as conn:
                conn.execute("UPDATE processing_jobs SET available_at=0 WHERE document_id=?", (doc["id"],))
        claim = service.collection.claim_processing(
            owner_id=SYSTEM_OWNER_ID,
            document_ids=[doc["id"]],
        )
        assert claim and claim["phase"] == "parse"
        before = len(calls)
        service.collection.process_item(claim)
        assert not failures, repr(failures)
        assert len(calls) - before == expected_batch
        with closing(service.db.connect()) as conn:
            job = conn.execute(
                "SELECT status FROM processing_jobs WHERE document_id=?", (doc["id"],)
            ).fetchone()
        assert job["status"] == ("queued" if index < 2 else "completed")

    with closing(service.db.connect()) as conn:
        parsed = conn.execute(
            "SELECT status,manifest FROM documents WHERE id=?", (doc["id"],)
        ).fetchone()
    manifest = json.loads(parsed["manifest"])
    assert parsed["status"] == "ready"
    assert len(downloads) == 5 and len(manifest["html_source"]["linked_images"]) == 5
    assert all(value["asset_status"] == "ocr_complete" for value in manifest["html_source"]["linked_images"])
    assert manifest["quality"]["complete"] is True


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


def test_newly_approved_source_replays_events_blocked_by_previous_pool(tmp_path):
    service, config = _importer(tmp_path)
    config.write_text(json.dumps({"sources": []}))
    event_path = _write_version(tmp_path, content=b"<h1>Approved later</h1>", at_ns=time.time_ns())
    importer = OfficialSourceImporter(service, config_path=config)

    assert importer.run_once() == {"stored": 0, "skipped": 0, "blocked": 1, "deferred": 0}
    with service.db.connect() as conn:
        failed = conn.execute(
            "SELECT status,reason FROM official_source_imports WHERE event_key=?",
            (event_path.relative_to(tmp_path).as_posix(),),
        ).fetchone()
    assert failed["status"] == "blocked" and failed["reason"] == "source_not_in_approved_pool"

    config.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "id-bpom-regulations",
                        "country": "ID",
                        "kind": "catalogue",
                        "status": "active",
                        "allowed_hosts": ["jdih.pom.go.id"],
                    }
                ]
            }
        )
    )

    assert importer.run_once() == {"stored": 1, "skipped": 0, "blocked": 0, "deferred": 0}
    with service.db.connect() as conn:
        recovered = conn.execute(
            "SELECT status,reason FROM official_source_imports WHERE event_key=?",
            (event_path.relative_to(tmp_path).as_posix(),),
        ).fetchone()
    assert recovered["status"] == "stored" and recovered["reason"] is None


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


def test_sso_catalogue_pages_are_not_reported_as_invalid_document_evidence(tmp_path):
    service, config = _importer(tmp_path)
    importer = OfficialSourceImporter(service, config_path=config)
    source = {
        "id": "sg-sso-statutes",
        "country": "SG",
        "kind": "catalogue",
        "allowed_hosts": ["sso.agc.gov.sg"],
        "url": "https://sso.agc.gov.sg/Browse/SL/Current/All",
        "discovery": {
            "adapter": "sso_catalog_2026",
            "category": "SL",
            "state": "Current",
            "page_size": 500,
        },
    }
    catalogue_urls = (
        source["url"] + "?PageSize=500&SortBy=Title&SortOrder=ASC",
        source["url"] + "/1?PageSize=500&SortBy=Title&SortOrder=ASC",
    )
    for index, url in enumerate(catalogue_urls):
        at = time.time()
        event = {
            "at": at,
            "kind": "content_version",
            "url": url,
            "source_kind": "catalogue",
            "source_root": index == 0,
            "discovery_matched": True,
            "discovery_purpose": "catalog",
        }
        state = {
            "updated_at": at + 1,
            "sources": {
                source["id"]: {
                    "cache": {url: {"discovery": {"matched": True, "purpose": "catalog"}}}
                }
            },
        }
        assert importer._eligible("SG", source, event, state, verified_mime_type="text/html") == (
            False,
            "catalogue_page_not_document",
        )


def test_sso_current_html_versions_keep_catalogue_identity_and_row_evidence(tmp_path):
    service, config = _importer(tmp_path)
    source = {
        "id": "sg-sso-statutes",
        "country": "SG",
        "kind": "catalogue",
        "allowed_hosts": ["sso.agc.gov.sg"],
        "url": "https://sso.agc.gov.sg/Browse/SL/Current/All",
        "discovery": {
            "adapter": "sso_catalog_2026",
            "category": "SL",
            "state": "Current",
            "page_size": 500,
        },
        "verification": {"checklist_items": ["SG-1.3"]},
    }
    config.write_text(json.dumps({"sources": [source]}))
    country_root = tmp_path / "official-updates/SG"
    (country_root / "files").mkdir(parents=True)
    catalogue_url = source["url"] + "?PageSize=500&SortBy=Title&SortOrder=ASC"
    catalogue_hashes = [hashlib.sha256(f"catalogue-page-{index}".encode()).hexdigest() for index in (1, 2)]
    urls = [
        "https://sso.agc.gov.sg/SL/SFA1973-RG1?DocDate=20240520",
        "https://sso.agc.gov.sg/SL/SFA1973-RG1?DocDate=20171229170000",
    ]
    cache = {}
    events = []
    first_event_ns = time.time_ns()
    for index, (url, catalogue_hash) in enumerate(zip(urls, catalogue_hashes, strict=True), start=1):
        original = f"<html><body><h1>Food Regulations edition {index}</h1></body></html>".encode()
        digest = hashlib.sha256(original).hexdigest()
        relative = f"files/sso-version-{index}.html"
        (country_root / relative).write_bytes(original)
        locator = {
            "source_sha256": catalogue_hash,
            "table_class": "browse-list",
            "row": index,
            "unit": "decoded_unicode_char",
            "start": 100 * index,
            "end": 200 * index,
            "fragment_sha256": hashlib.sha256(f"row-{index}".encode()).hexdigest(),
        }
        event_ns = first_event_ns + index
        event = {
            "at": time.time() + index,
            "kind": "content_version",
            "source_id": source["id"],
            "source_kind": "catalogue",
            "source_root": False,
            "url": url,
            "content_hash": digest,
            "previous_content_hash": None if index == 1 else "a" * 64,
            "path": relative,
            "mime_type": "text/html",
            "discovery_matched": True,
            "discovery_purpose": "detail",
            "discovery_parent_url": catalogue_url,
            "discovery_parent_hash": catalogue_hash,
            "discovery_title": "Food Regulations",
            "source_identity_key": "sso:sl:sfa1973-rg1",
            "source_locator": locator,
            "source_document_date": url.split("DocDate=", 1)[1],
            "source_status": "Current",
        }
        if index == 1:
            assert _sso_catalogue_evidence(source, event, url)
            invalid_parent = event.copy()
            invalid_parent["discovery_parent_url"] = source["url"]
            assert _sso_catalogue_evidence(source, invalid_parent, url) is None
        events.append((event_ns, event))
        cache[url] = {
            "content_hash": digest,
            "mime_type": "text/html",
            "discovery": {"matched": True, "purpose": "detail"},
        }
    event_dir = country_root / "events/2026-10-25"
    event_dir.mkdir(parents=True)
    for event_ns, event in events:
        (event_dir / f"{event_ns}-sso-version.json").write_text(json.dumps(event))
    (country_root / "state.json").write_text(
        json.dumps({"updated_at": time.time() + 10, "sources": {source["id"]: {"cache": cache}}})
    )

    importer = OfficialSourceImporter(service, config_path=config)
    assert importer.run_once() == {"stored": 2, "skipped": 0, "blocked": 0, "deferred": 0}
    with service.db.connect() as conn:
        docs = conn.execute(
            "SELECT version,family_id,manifest FROM documents WHERE owner_id=? ORDER BY version",
            (SYSTEM_OWNER_ID,),
        ).fetchall()
    assert [row["version"] for row in docs] == [1, 2]
    assert docs[0]["family_id"] == docs[1]["family_id"]
    for index, row in enumerate(docs, start=1):
        source_response = json.loads(row["manifest"])["source_response"]
        assert source_response["identity_key"] == "sso:sl:sfa1973-rg1"
        assert source_response["checklist_items"] == ["SG-1.3"]
        assert source_response["document_date"] == ("20240520" if index == 1 else "20171229170000")
        assert source_response["source_locator"]["row"] == index
        assert source_response["catalogue_sha256"] == catalogue_hashes[index - 1]


def test_only_exactly_configured_guidance_html_detail_is_imported(tmp_path):
    service, config = _importer(tmp_path)
    detail_url = "https://jdih.pom.go.id/document/approved-guidance"
    _write_version(
        tmp_path,
        content=b"<html><body><h1>Approved detail</h1><p>Exact approved path.</p></body></html>",
        at_ns=time.time_ns(),
        purpose="detail",
        url=detail_url,
    )
    pool = json.loads(config.read_text())
    pool["sources"][0].update(
        kind="guidance",
        html_document_paths=["/document/approved-guidance"],
    )
    config.write_text(json.dumps(pool))

    assert OfficialSourceImporter(service, config_path=config).run_once()["stored"] == 1
    with service.db.connect() as conn:
        receipt = conn.execute("SELECT status,reason FROM official_source_imports").fetchone()
        document = conn.execute(
            "SELECT source_url,status FROM documents WHERE owner_id=?", (SYSTEM_OWNER_ID,)
        ).fetchone()
        job = conn.execute(
            "SELECT phase,status FROM processing_jobs WHERE document_id=(SELECT id FROM documents WHERE owner_id=?)",
            (SYSTEM_OWNER_ID,),
        ).fetchone()
    assert tuple(receipt) == ("stored", None)
    assert tuple(document) == (detail_url, "processing")
    assert tuple(job) == ("parse", "queued")


def test_automatically_rechecks_previously_skipped_approved_html_detail(tmp_path):
    service, config = _importer(tmp_path)
    at_ns = time.time_ns()
    detail_url = "https://jdih.pom.go.id/document/approved-guidance"
    event_path = _write_version(
        tmp_path,
        content=b"<html><body><h1>Previously skipped</h1></body></html>",
        at_ns=at_ns,
        purpose="detail",
        url=detail_url,
    )
    importer = OfficialSourceImporter(service, config_path=config)
    assert importer.run_once()["skipped"] == 1
    with service.db.connect() as conn:
        conn.execute(
            "UPDATE official_source_import_cursor SET event_ns=? WHERE country='ID'", (at_ns + 1,)
        )
    pool = json.loads(config.read_text())
    pool["sources"][0].update(kind="guidance", html_document_paths=["/document/approved-guidance"])
    config.write_text(json.dumps(pool))

    assert importer.run_once()["stored"] == 1
    with service.db.connect() as conn:
        receipt = conn.execute(
            "SELECT status,reason,document_id FROM official_source_imports WHERE event_key=?",
            (event_path.relative_to(tmp_path).as_posix(),),
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM documents WHERE owner_id=?", (SYSTEM_OWNER_ID,)).fetchone()[0]
    assert tuple(receipt[:2]) == ("stored", None)
    assert receipt["document_id"] is not None
    assert count == 1


def test_unknown_html_path_remains_skipped_when_neighbor_is_approved(tmp_path):
    service, config = _importer(tmp_path)
    detail_url = "https://jdih.pom.go.id/document/not-approved"
    _write_version(
        tmp_path,
        content=b"<html><body><h1>Not approved</h1></body></html>",
        at_ns=time.time_ns(),
        purpose="detail",
        url=detail_url,
    )
    pool = json.loads(config.read_text())
    pool["sources"][0].update(kind="guidance", html_document_paths=["/document/approved-guidance"])
    config.write_text(json.dumps(pool))

    assert OfficialSourceImporter(service, config_path=config).run_once()["skipped"] == 1
    with service.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents WHERE owner_id=?", (SYSTEM_OWNER_ID,)).fetchone()[0] == 0


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
