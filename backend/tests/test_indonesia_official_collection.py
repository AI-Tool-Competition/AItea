"""Full provider pagination is proven independently of originals and legal scope."""

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import sqlite3
import time
import threading
from types import SimpleNamespace

import pytest

from app.indonesia_official import SETNEG_ITEMS, original_file_relations, reconcile_catalogue
from app.ingestion import DownloadError

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "indonesia_collector", ROOT / "scripts/collect_indonesia_official.py"
)
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def pages():
    return [
        {"offset": 0, "total": 3, "page_size": 2, "returned": 2, "official_ids": ["P1", "P2"]},
        {"offset": 2, "total": 3, "page_size": 2, "returned": 1, "official_ids": ["P3"]},
    ]


def test_balanced_provider_pages_do_not_claim_full_indonesian_scope():
    result = reconcile_catalogue(pages()[::-1])
    assert result["observed_catalogue_complete"]
    assert result["provider_total"] == result["unique_official_ids"] == 3
    assert result["atomic_snapshot"] is False
    assert result["full_checklist_scope_complete"] is False


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "total_changed", "overlap", "missing_identity"])
def test_lost_or_changed_records_cannot_balance(mutation):
    rows = pages()
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows[1]["official_ids"] = ["P1"]
    elif mutation == "total_changed":
        rows[1]["total"] = 4
    elif mutation == "overlap":
        rows[1]["offset"] = 1
    else:
        rows[0]["official_ids"].pop()
    assert reconcile_catalogue(rows)["observed_catalogue_complete"] is False


def test_no_request_is_not_an_empty_catalogue():
    assert reconcile_catalogue([])["observed_catalogue_complete"] is False
    empty = [{"offset": 0, "total": 0, "page_size": 50, "returned": 0, "official_ids": []}]
    assert reconcile_catalogue(empty)["observed_catalogue_complete"]


def arguments(tmp_path):
    return SimpleNamespace(
        output=tmp_path / "new-collection",
        storage_root=tmp_path,
        resume=False,
        retry_failed=False,
        workers=4,
        per_host=2,
        timeout=10,
        time_budget=30,
        detail_types=[],
        original_byte_budget=0,
        min_free_bytes=256 * 1024 * 1024,
        prepare_library=False,
    )


def mock_catalogue(monkeypatch):
    requested, fail_once = [], {("PP", 50)}

    def download(url, *, consume, **options):
        body = options["json_body"]
        jns, start, length = body["jns"][0], body["start"], body["length"]
        requested.append((jns, start))
        if (jns, start) in fail_once:
            fail_once.remove((jns, start))
            raise DownloadError("test source temporarily unavailable", retryable=True)
        data = {
            "jml": 51,
            "jn": [jns],
            "data": [
                {
                    "jns": jns,
                    "idperaturan": f"P{index + 1}",
                    "no_peraturan": str(index + 1),
                    "tahun": "2020",
                    "tentang": "Synthetic instrument for pagination regression",
                }
                for index in range(start, min(51, start + length))
            ],
        }
        raw = json.dumps(data).encode()
        return {
            "url": url,
            "filename": "catalogue.json",
            "mime_type": "application/json",
            "response_status": 200,
            "blob": consume(iter([raw])),
        }

    monkeypatch.setattr(sys.modules[collector.SourceVerifier.__module__], "_download", download)
    return requested


def test_real_journal_resume_retries_only_failed_page_and_preserves_full_ledger(tmp_path, monkeypatch):
    requested = mock_catalogue(monkeypatch)
    args = arguments(tmp_path)
    assert collector.run(args) == 2
    before = list(requested)
    first = json.loads((args.output / "report.json").read_text())
    assert not first["observed_catalogues_complete"]
    assert first["categories"]["1.5"]["observed_rows"] == 50
    args.resume = args.retry_failed = True
    assert collector.run(args) == 0
    assert requested[len(before) :] == [("PP", 50)]
    report = json.loads((args.output / "report.json").read_text())
    assert report["ledger_rows"] == 4 * 51
    assert report["observed_catalogues_complete"]
    assert report["full_29_item_checklist_complete"] is False
    rows = [json.loads(line) for line in (args.output / "ledger.jsonl").read_text().splitlines()]
    assert {row["item_code"] for row in rows} == set(SETNEG_ITEMS)
    assert all(row["country"] == "ID" and row["legal_review"] == "unreviewed" for row in rows)
    assert all(not row["assets"] and not row["relations"] for row in rows)


def test_corrupted_cached_receipt_stops_before_another_request(tmp_path, monkeypatch):
    requested = mock_catalogue(monkeypatch)
    args = arguments(tmp_path)
    collector.run(args)
    report = json.loads((args.output / "report.json").read_text())
    receipt = report["categories"]["1.3"]["page_evidence"][0]
    blob = args.output / receipt["path"]
    blob.chmod(0o600)
    blob.write_bytes(b"corrupted evidence")
    before = len(requested)
    args.resume = args.retry_failed = True
    with pytest.raises(ValueError, match="receipt size changed"):
        collector.run(args)
    assert len(requested) == before


def test_whole_transport_failure_batch_cools_source_without_consuming_detail_queue(tmp_path, monkeypatch):
    requested = mock_catalogue(monkeypatch)
    module = sys.modules[collector.SourceVerifier.__module__]
    successful_download = module._download

    def failing_second_pages(url, *, consume, **options):
        body = options["json_body"]
        if body.get("start") == 50:
            requested.append((body["jns"][0], 50))
            raise DownloadError("connection failed", retryable=True)
        return successful_download(url, consume=consume, **options)

    monkeypatch.setattr(module, "_download", failing_second_pages)
    args = arguments(tmp_path)
    args.detail_types = list(SETNEG_ITEMS.values())
    assert collector.run(args) == 2
    assert len(requested) == 8
    with sqlite3.connect(args.output / "journal.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM tasks WHERE purpose='detail' AND status='queued'"
            ).fetchone()[0]
            == 200
        )
        until = float(connection.execute("SELECT value FROM config WHERE key='cooldown_until'").fetchone()[0])
        assert until > time.time() + 110
    before = len(requested)
    args.resume = args.retry_failed = True
    assert collector.run(args) == 2
    assert len(requested) == before


def test_resume_rejects_changed_plan_and_unapproved_external_location(tmp_path):
    output = tmp_path / "collection"
    with pytest.raises(ValueError, match="explicit storage root"):
        collector.open_state(output, resume=False, definition_hash="first")
    state = collector.open_state(output, resume=False, definition_hash="first", storage_root=tmp_path)
    state.close()
    with pytest.raises(ValueError, match="immutable collection plan changed"):
        collector.open_state(output, resume=True, definition_hash="second", storage_root=tmp_path)


def reusable_response_journals(tmp_path):
    """A new catalogue and a historical detail/original response share one identity."""
    definition = next(
        row for row in collector.load_blueprints()["indonesia-20261003"]["items"] if row["id"] == "1.3"
    )
    source, target = tmp_path / "historical", tmp_path / "current"
    donor = collector.open_state(source, resume=False, definition_hash="historical", storage_root=tmp_path)
    current = collector.open_state(target, resume=False, definition_hash="current", storage_root=tmp_path)
    detail_url = "https://jdih.setneg.go.id/api/hukumproduk/detaildata?jns=UU&no=2&thn=2026"
    detail = (ROOT / "backend/tests/fixtures/discovery/setneg-detail.json").read_bytes()
    recipe = collector.recipe_for(definition)
    discovery = collector.discover_page(detail_url, detail, "application/json", recipe)
    assert discovery["matched"]
    original_url = next(link["url"] for link in discovery["links"] if link["purpose"] == "document")
    historical = []
    for url, purpose, content, mime in (
        (detail_url, "detail", detail, "application/json"),
        (original_url, "document", b"%PDF-1.4\nHistorical original\n%%EOF", "application/pdf"),
    ):
        blob = collector.CorpusBlobs(source / "files", max_bytes=1024 * 1024).put("donor", [content])
        result = {
            "status": "downloaded",
            "requested_url": url,
            "configured_purpose": purpose,
            "final_url": url,
            "mime_type": mime,
            "size": blob.size,
            "content_hash": blob.digest,
            "evidence_path": str(blob.path.relative_to(source)),
            "started_at": "2026-10-19T00:00:00Z",
            "discovery": collector.discover_page(url, content, mime, recipe),
        }
        donor.execute("INSERT OR IGNORE INTO tasks(item_code,url,purpose) VALUES('1.3',?,?)", (url, purpose))
        donor.commit()
        collector.record_result(donor, {"item_code": "1.3", "url": url}, result)
        historical.append(result)
    current.execute("INSERT INTO tasks(item_code,url,purpose) VALUES('1.3',?,'detail')", (detail_url,))
    current.execute("INSERT INTO config VALUES('transport_failure_streak','4')")
    current.execute("INSERT INTO config VALUES('cooldown_until','9999999999')")
    current.commit()
    donor.close()
    return current, source, target, {"1.3": definition}, historical


def test_historical_receipt_reuse_is_offline_idempotent_and_preserves_backoff(tmp_path, monkeypatch):
    current, source, target, definitions, historical = reusable_response_journals(tmp_path)

    def no_request(*args, **kwargs):
        raise AssertionError("receipt reuse must not contact the source")

    monkeypatch.setattr(sys.modules[collector.SourceVerifier.__module__], "_download", no_request)
    donor_before = (source / "journal.sqlite3").read_bytes()
    try:
        result = collector.reuse_receipts(current, target, [source], definitions)
        assert result["reused_responses"] == 2 and result["network_attempts"] == 0
        tasks = list(current.execute("SELECT status,attempts,result FROM tasks ORDER BY purpose"))
        assert len(tasks) == 2 and all(tuple(row[:2]) == ("downloaded", 0) for row in tasks)
        for row in tasks:
            receipt = json.loads(row["result"])
            old = next(r for r in historical if r["requested_url"] == receipt["requested_url"])
            assert receipt["started_at"] == old["started_at"]
            assert (target / receipt["evidence_path"]).read_bytes() == (
                source / old["evidence_path"]
            ).read_bytes()
            reuse = receipt["cache_reuse"]
            raw_proof = (target / reuse["source_result_cas"]).read_bytes()
            assert hashlib.sha256(raw_proof).hexdigest() == reuse["source_result_sha256"]
            assert json.loads(raw_proof) == old
            assert reuse["freshness"] == "historical_response_not_refetched"
        assert (
            current.execute("SELECT value FROM config WHERE key='transport_failure_streak'").fetchone()[0]
            == "4"
        )
        assert (
            current.execute("SELECT value FROM config WHERE key='cooldown_until'").fetchone()[0]
            == "9999999999"
        )
        assert collector.reuse_receipts(current, target, [source], definitions)["reused_responses"] == 0
        assert current.execute("SELECT count(*) FROM attempts").fetchone()[0] == 2
        collector.verify_receipts(current, target)
        assert (source / "journal.sqlite3").read_bytes() == donor_before
    finally:
        current.close()


@pytest.mark.parametrize("mutation", ["hash", "traversal", "symlink", "wrong_source"])
def test_reused_receipt_rejects_corruption_and_path_or_source_escape(tmp_path, mutation):
    current, source, target, definitions, historical = reusable_response_journals(tmp_path)
    result = historical[0]
    evidence = source / result["evidence_path"]
    if mutation == "hash":
        evidence.chmod(0o600)
        evidence.write_bytes(b"X" * result["size"])
    elif mutation == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_bytes(evidence.read_bytes())
        evidence.unlink()
        evidence.symlink_to(outside)
    else:
        if mutation == "traversal":
            result["evidence_path"] = "../outside.json"
        else:
            result["final_url"] = "https://example.com/untrusted.json"
        with sqlite3.connect(source / "journal.sqlite3") as donor:
            donor.execute("UPDATE tasks SET result=? WHERE purpose='detail'", (json.dumps(result),))
    try:
        with pytest.raises(ValueError):
            collector.reuse_receipts(current, target, [source], definitions)
        assert current.execute("SELECT status,attempts FROM tasks").fetchone()[:] == ("queued", 0)
        assert current.execute("SELECT count(*) FROM attempts").fetchone()[0] == 0
    finally:
        current.close()


def test_receipt_reuse_only_imports_already_discovered_tasks(tmp_path):
    current, source, target, definitions, _ = reusable_response_journals(tmp_path)
    try:
        current.execute("DELETE FROM tasks")
        current.commit()
        assert collector.reuse_receipts(current, target, [source], definitions)["reused_responses"] == 0
        assert current.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    finally:
        current.close()


def test_resume_rejects_changed_historical_response_proof(tmp_path):
    current, source, target, definitions, _ = reusable_response_journals(tmp_path)
    try:
        collector.reuse_receipts(current, target, [source], definitions)
        result = json.loads(current.execute("SELECT result FROM tasks WHERE purpose='detail'").fetchone()[0])
        proof = target / result["cache_reuse"]["source_result_cas"]
        proof.chmod(0o600)
        proof.write_bytes(b"X" * proof.stat().st_size)
        with pytest.raises(ValueError, match="historical receipt proof hash changed"):
            collector.verify_receipts(current, target)
    finally:
        current.close()


def association_record(tmp_path):
    from app.blob_store import BlobStore

    blobs = BlobStore(tmp_path / "files", max_bytes=1024 * 1024)
    catalogue = blobs.put(collector.SOURCE_OWNER, [b'{"catalogue":"source evidence"}'])
    detail = blobs.put(collector.SOURCE_OWNER, [b'{"detail":"source evidence"}'])
    assets = []
    for index, filename in enumerate(("PERPU 3 Tahun 2005.pdf", "PERPU 3 Tahun 2005_Lampiran.pdf")):
        blob = blobs.put(collector.SOURCE_OWNER, [b"%PDF-1.4\n" + filename.encode()])
        assets.append(
            {
                "url": "https://jdih.setneg.go.id/api/hukumproduk/pdf?f=" + str(index),
                "title": filename,
                "evidence_path": str(blob.path.relative_to(tmp_path)),
                "content_hash": blob.digest,
                "size": blob.size,
                "original_acquired": True,
                "official_file": {"json_pointer": f"/file/{index}/realName", "filename": filename},
            }
        )
    return {
        "source_key": "setneg:P14903",
        "official_id": "P14903",
        "item_code": "1.4",
        "source_type": "PERPU",
        "assets": assets,
        "discovery_page": {
            "url": "https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=PERPU",
            "sha256": catalogue.digest,
            "path": str(catalogue.path.relative_to(tmp_path)),
        },
        "detail_evidence": {
            "final_url": "https://jdih.setneg.go.id/api/hukumproduk/detaildata?jns=PERPU&no=3&thn=2005",
            "content_hash": detail.digest,
            "evidence_path": str(detail.path.relative_to(tmp_path)),
        },
    }


def test_explicit_file_association_is_idempotent_and_preserves_processed_versions(tmp_path, monkeypatch):
    from app.archive import LegalArchive

    record = association_record(tmp_path)
    (tmp_path / "ledger.jsonl").write_text(json.dumps(record) + "\n")
    collector.prepare_library(tmp_path)
    with sqlite3.connect(tmp_path / "tea.sqlite3") as connection:
        before = connection.execute(
            "SELECT id,role,version,content_hash FROM documents ORDER BY id"
        ).fetchall()
        assert len(before) == 2 and all(row[1] == "primary" for row in before)
        assert connection.execute("SELECT level FROM legal_classifications").fetchall() == [
            ("primary",),
            ("primary",),
        ]
        connection.execute(
            "UPDATE documents SET manifest=?", (json.dumps({"worker_checkpoint": "preserve"}),)
        )

    def forbidden_resave(*args, **kwargs):
        raise AssertionError("adding an association must not resave processed originals")

    monkeypatch.setattr(LegalArchive, "store_original", forbidden_resave)
    collector.prepare_library(tmp_path)
    with sqlite3.connect(tmp_path / "tea.sqlite3") as connection:
        assert (
            connection.execute("SELECT id,role,version,content_hash FROM documents ORDER BY id").fetchall()
            == before
        )
        assert all(
            json.loads(row[0]) == {"worker_checkpoint": "preserve"}
            for row in connection.execute("SELECT manifest FROM documents")
        )
        relations = connection.execute("SELECT relation,locator FROM legal_relations").fetchall()
        assert len(relations) == 1 and relations[0][0] == "related"
        locator = json.loads(relations[0][1])
        assert locator["official_id"] == "P14903"
        assert locator["detail_sha256"] == record["detail_evidence"]["content_hash"]
        assert locator["catalogue_sha256"] == record["discovery_page"]["sha256"]
        assert locator["target"]["json_pointer"] == "/file/1/realName"
        assert locator["target"]["filename"].endswith("_Lampiran.pdf")
        assert connection.execute("SELECT count(*) FROM processing_jobs").fetchone()[0] == 2
        assert all(
            json.loads(row[0])["review_status"] == "draft"
            for row in connection.execute("SELECT payload FROM document_metadata")
        )


def test_historical_response_receipts_follow_prepared_originals(tmp_path):
    record = association_record(tmp_path)
    proof = {"status": "downloaded", "started_at": "2026-10-18T01:02:03Z"}
    raw = json.dumps(proof).encode()
    blob = collector.CorpusBlobs(tmp_path / "files", max_bytes=1024 * 1024).put("donor", [raw])
    reuse = {
        "source_result_cas": str(blob.path.relative_to(tmp_path)),
        "source_result_sha256": blob.digest,
        "source_url": record["detail_evidence"]["final_url"],
    }
    record["detail_receipt_reuse"] = reuse
    record["assets"][0]["cache_reuse"] = {**reuse, "source_url": record["assets"][0]["url"]}
    (tmp_path / "ledger.jsonl").write_text(json.dumps(record) + "\n")
    collector.prepare_library(tmp_path)
    documents = json.loads((tmp_path / "campaign.json").read_text())["documents"].values()
    for document in documents:
        evidence = document["source_evidence"]
        assert Path(evidence["detail_receipt"]["cas_path"]).read_bytes() == raw
        assert evidence["detail_receipt"]["freshness"] == "historical_response_not_refetched"
        if document["original"]["sha256"] == record["assets"][0]["content_hash"]:
            assert evidence["original_receipt"]["url"] == record["assets"][0]["url"]
            assert Path(evidence["original_receipt"]["cas_path"]).read_bytes() == raw


def test_pending_alternative_main_file_prevents_ambiguous_association(tmp_path):
    record = association_record(tmp_path)
    assert len(original_file_relations(record)) == 1
    record["assets"].append(
        {
            "url": "https://jdih.setneg.go.id/api/hukumproduk/pdf?f=english",
            "original_acquired": False,
            "official_file": {"json_pointer": "/file/2/realName", "filename": "English.pdf"},
        }
    )
    assert original_file_relations(record) == []


@pytest.mark.parametrize("per_host,budget_mb,expected", [(2, 1024, 2), (4, 1024, 4), (4, 128, 1)])
def test_original_concurrency_is_bounded_by_host_and_reserved_bytes(
    tmp_path, monkeypatch, per_host, budget_mb, expected
):
    barrier = threading.Barrier(expected)
    lock = threading.Lock()
    active = peak = 0

    def source_row(instrument):
        return {
            "jns": instrument,
            "idperaturan": f"P{list(SETNEG_ITEMS.values()).index(instrument) + 1}",
            "no_peraturan": "1",
            "tahun": "2020",
            "tentang": "Concurrency test original",
        }

    def download(url, *, consume, **options):
        nonlocal active, peak
        body = options.get("json_body")
        if body and isinstance(body["jns"], list):
            instrument = body["jns"][0]
            content = json.dumps({"data": [source_row(instrument)], "jml": 1, "jn": [instrument]}).encode()
        elif body:
            content = json.dumps(
                {"row": [source_row(body["jns"])], "file": [{"realName": "original.pdf"}]}
            ).encode()
        else:
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                barrier.wait(timeout=2)
                content = b"%PDF-1.4\noriginal"
            finally:
                with lock:
                    active -= 1
        return {
            "url": url,
            "filename": "original.pdf",
            "response_status": 200,
            "mime_type": "application/json" if body else "application/pdf",
            "blob": consume([content]),
        }

    monkeypatch.setattr(sys.modules[collector.SourceVerifier.__module__], "_download", download)
    args = arguments(tmp_path)
    args.detail_types = list(SETNEG_ITEMS.values())
    args.per_host = per_host
    args.original_byte_budget = budget_mb * 1024 * 1024
    assert collector.run(args) == 0
    assert peak == expected


@pytest.mark.parametrize("files_available", [True, False])
def test_original_preparation_preserves_proofs_and_queues_once_without_models(
    tmp_path, monkeypatch, files_available
):
    from app import ingestion

    calls = []
    originals = b"%PDF-1.4\n% synthetic original for persistence test\n%%EOF\n"

    def row(instrument):
        return {
            "jns": instrument,
            "idperaturan": f"P{list(SETNEG_ITEMS.values()).index(instrument) + 1}",
            "no_peraturan": "1",
            "tahun": "2020",
            "tentang": "Synthetic original",
        }

    def download(url, *, consume, **options):
        calls.append(url)
        body = options.get("json_body")
        if body and isinstance(body["jns"], list):
            instrument = body["jns"][0]
            content = json.dumps({"data": [row(instrument)], "jml": 1, "jn": [instrument]}).encode()
        elif body:
            content = json.dumps(
                {"row": [row(body["jns"])], "file": [{"realName": "original.pdf"}] if files_available else []}
            ).encode()
        else:
            content = originals
        return {
            "url": url,
            "filename": "original.pdf",
            "response_status": 200,
            "mime_type": "application/pdf" if content == originals else "application/json",
            "blob": consume([content]),
        }

    def forbidden_model(*args, **kwargs):
        raise AssertionError("source preparation must not call the embedding model")

    monkeypatch.setattr(ingestion, "embed_texts", forbidden_model)
    monkeypatch.setattr(sys.modules[collector.SourceVerifier.__module__], "_download", download)
    args = arguments(tmp_path)
    args.detail_types = ["PERPU"]
    args.original_byte_budget = 128 * 1024 * 1024
    args.prepare_library = True
    assert collector.run(args) == (0 if files_available else 2)
    state = json.loads((args.output / "campaign.json").read_text())
    if not files_available:
        assert len(state["documents"]) == state["queued_jobs"] == 0
        report = json.loads((args.output / "report.json").read_text())
        assert report["requested_scope"]["unfinished_originals"] == 0
        assert report["requested_scope"]["original_scope_gaps"] == ["setneg:P2"]
        assert report["requested_scope"]["complete"] is False
        category = report["categories"]["1.4"]
        assert category["instruments_with_all_observed_originals"] == 0
        assert category["downloaded_original_files"] == 0
        assert category["observed_details_without_originals"] == ["setneg:P2"]
        assert category["instrument_original_scope_complete"] is False
        return
    assert len(state["documents"]) == state["queued_jobs"] == 1
    assert state["model_calls_by_preparer"] == 0
    report = json.loads((args.output / "report.json").read_text())
    assert report["categories"]["1.4"]["instruments_with_all_observed_originals"] == 1
    assert report["categories"]["1.4"]["instrument_original_scope_complete"] is True
    document = next(iter(state["documents"].values()))
    assert set(document["source_evidence"]) == {"catalogue", "detail"}
    assert all(Path(p["cas_path"]).is_file() for p in document["source_evidence"].values())
    assert Path(document["original"]["cas_path"]).read_bytes() == originals
    # A parser may complete between collector invocations. Source preparation
    # must not resave/reclassify that version or overwrite derived evidence.
    from app.archive import LegalArchive

    derived_manifest = {"source_hash": document["original"]["sha256"], "worker_checkpoint": "preserve"}
    with sqlite3.connect(args.output / "tea.sqlite3") as connection:
        connection.execute(
            "UPDATE documents SET manifest=? WHERE id=?",
            (json.dumps(derived_manifest), document["document_id"]),
        )

    def forbidden_resave(*args, **kwargs):
        raise AssertionError("already prepared original must not be saved again")

    monkeypatch.setattr(LegalArchive, "store_original", forbidden_resave)
    count = len(calls)
    args.resume = True
    assert collector.run(args) == 0
    assert len(calls) == count
    connection = sqlite3.connect(args.output / "tea.sqlite3")
    try:
        assert connection.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM processing_jobs WHERE status='queued'").fetchone()[0]
            == 1
        )
        metadata = json.loads(connection.execute("SELECT payload FROM document_metadata").fetchone()[0])
        assert metadata["review_status"] == "draft"
        assert (
            json.loads(connection.execute("SELECT manifest FROM documents").fetchone()[0]) == derived_manifest
        )
    finally:
        connection.close()


def test_retry_after_is_persisted_and_blocks_new_requests_on_restart(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    calls = []

    def rate_limited(url, **options):
        calls.append(url)
        raise DownloadError(
            "抓取来源返回 HTTP 429。", retryable=True, retry_after=120, response_host="jdih.setneg.go.id"
        )

    monkeypatch.setattr(sys.modules[collector.SourceVerifier.__module__], "_download", rate_limited)
    assert collector.run(args) == 2
    assert len(calls) == 4
    connection = sqlite3.connect(args.output / "journal.sqlite3")
    try:
        until = float(connection.execute("SELECT value FROM config WHERE key='cooldown_until'").fetchone()[0])
        assert until > time.time() + 110
    finally:
        connection.close()
    args.resume = args.retry_failed = True
    assert collector.run(args) == 2
    assert len(calls) == 4
