"""Durable directory resume, honest source counts and bounded original reuse."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import time

import pytest

from app.public_search import EGazetteSearchRequest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("collect_egazette", ROOT / "scripts/collect_egazette.py")
campaign = importlib.util.module_from_spec(spec)
sys.path.insert(0, str(ROOT / "scripts"))
try:
    spec.loader.exec_module(campaign)
finally:
    sys.path.pop(0)


def collector(path):
    return campaign.GazetteCollector(
        path, EGazetteSearchRequest("a" * 32, "b" * 64), page_size=10, min_free_bytes=0
    )


def row(index):
    return {
        "objectID": f"example-{index}",
        "title": f"Example {index} Act 2021",
        "notificationNum": str(index),
        "fileUrl": f"https://assets.egazette.gov.sg/2021/Legislative%20Supplements/Revised%20Acts/{index}.pdf",
        "category": "Legislative Supplements",
        "subCategory": "Revised Acts",
        "publishYear": 2021,
        "publishTimestamp": 1640880000000,
    }


def response(page, *, total=15):
    body = {
        "page": page,
        "nbHits": total,
        "nbPages": (total + 9) // 10,
        "hitsPerPage": 10,
        "exhaustiveNbHits": True,
        "hits": [row(index) for index in range(page * 10, min(total, (page + 1) * 10))],
    }
    return {
        "content": json.dumps(body).encode(),
        "url": campaign.EGAZETTE_SEARCH_URL,
        "mime_type": "application/json",
    }


def test_interrupted_directory_resumes_only_missing_page_and_preserves_raw_receipt(tmp_path, monkeypatch):
    calls = []

    def download(url, **kwargs):
        page = kwargs["json_body"]["page"]
        calls.append(page)
        if page == 1:
            raise ValueError("temporary source failure")
        return response(page)

    monkeypatch.setattr(campaign, "_download", download)
    state = collector(tmp_path).partition("Revised Acts", 2021)
    assert state["status"] == "incomplete" and state["captured_records"] == 10
    saved = tmp_path / "catalog/2021-revised-acts/page-00000.json"
    before = saved.read_bytes()
    calls.clear()
    monkeypatch.setattr(
        campaign,
        "_download",
        lambda url, **kw: calls.append(kw["json_body"]["page"]) or response(kw["json_body"]["page"]),
    )
    resumed = collector(tmp_path)
    state = resumed.partition("Revised Acts", 2021)
    assert state["status"] == "catalogue_complete" and state["captured_records"] == 15
    assert calls == [1] and saved.read_bytes() == before
    assert len(resumed.rows()) == 15


@pytest.mark.parametrize("failure", ["count_drift", "corrupt_raw", "wrong_filter", "repeated_identity"])
def test_bad_saved_or_live_evidence_cannot_complete_directory(tmp_path, monkeypatch, failure):
    def download(url, **kwargs):
        page = kwargs["json_body"]["page"]
        result = response(page, total=16 if failure == "count_drift" and page == 1 else 15)
        value = json.loads(result["content"])
        if page == 1 and failure == "wrong_filter":
            value["hits"][0]["publishYear"] = 2020
        if page == 1 and failure == "repeated_identity":
            value["hits"][0] = row(0)
        result["content"] = json.dumps(value).encode()
        return result

    monkeypatch.setattr(campaign, "_download", download)
    state = collector(tmp_path).partition("Revised Acts", 2021)
    if failure == "corrupt_raw":
        (tmp_path / "catalog/2021-revised-acts/page-00000.raw.json").write_bytes(b"{}")
        monkeypatch.setattr(
            campaign,
            "_download",
            lambda *a, **k: pytest.fail("corrupt evidence must not be silently replaced"),
        )
        state = collector(tmp_path).partition("Revised Acts", 2021)
    assert state["status"] == "incomplete"


def test_actual_connection_slots_limit_each_host_to_two(tmp_path):
    runner = collector(tmp_path)

    def use_slot(_):
        with runner.slot(campaign.EGAZETTE_SEARCH_HOST):
            time.sleep(0.01)

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(use_slot, range(24)))
    assert runner.peak == {campaign.EGAZETTE_SEARCH_HOST: 2}
    assert runner.hops == 24 and runner.active[campaign.EGAZETTE_SEARCH_HOST] == 0


def test_unconfirmed_count_enumerates_all_visible_pages_without_repartitioning(tmp_path, monkeypatch):
    calls = []

    def download(url, **kwargs):
        calls.append(kwargs["json_body"])
        result = response(kwargs["json_body"]["page"])
        value = json.loads(result["content"])
        value["exhaustiveNbHits"] = False
        result["content"] = json.dumps(value).encode()
        return result

    monkeypatch.setattr(campaign, "_download", download)
    runner = collector(tmp_path)
    state = runner.partition("Revised Acts", 2021)
    assert state["status"] == "catalogue_count_unconfirmed"
    assert state["captured_records"] == 15 and len(runner.rows()) == 15
    assert [call["page"] for call in calls] == [0, 1]
    assert all("publishTimestamp" not in call["filters"] for call in calls)


def test_invalid_filter_page_cannot_enter_original_directory_even_before_window_split(tmp_path, monkeypatch):
    calls = []

    def download(url, **kwargs):
        calls.append(kwargs["json_body"])
        result = response(0, total=15)
        value = json.loads(result["content"])
        value["nbPages"] = 1
        value["hits"][0]["publishYear"] = 2020
        result["content"] = json.dumps(value).encode()
        return result

    monkeypatch.setattr(campaign, "_download", download)
    runner = collector(tmp_path)
    state = runner.partition("Revised Acts", 2021)
    assert state["status"] == "incomplete" and state["validated_pages"] == []
    assert len(calls) == 1 and runner.rows() == []


def test_failed_later_page_does_not_publish_unvalidated_rows(tmp_path, monkeypatch):
    def download(url, **kwargs):
        page = kwargs["json_body"]["page"]
        result = response(page)
        value = json.loads(result["content"])
        if page:
            value["hits"][0]["publishYear"] = 2020
        result["content"] = json.dumps(value).encode()
        return result

    monkeypatch.setattr(campaign, "_download", download)
    runner = collector(tmp_path)
    state = runner.partition("Revised Acts", 2021)
    assert state["status"] == "incomplete" and state["validated_pages"] == [0]
    assert len(runner.rows()) == 10


def original_row():
    data = row(0)
    return {
        **data,
        "download_url": data["fileUrl"],
        "catalog_locators": [{"receipt": "verified-source.json"}],
    }


def test_original_signature_spanning_chunks_is_saved_once_and_resume_checks_hash(tmp_path, monkeypatch):
    raw = b"%PDF-1.4\noriginal\n%%EOF"
    calls = []

    def download(url, **kwargs):
        calls.append(url)
        assert "public_search" not in kwargs
        blob = kwargs["consume"]([raw[:1], raw[1:3], raw[3:5], raw[5:]])
        return {"blob": blob, "url": url, "mime_type": "application/pdf"}

    monkeypatch.setattr(campaign, "_download", download)
    result = collector(tmp_path).originals([original_row()], max_files=1, byte_budget=100)
    assert result["downloaded"] == 1 and result["new_bytes"] == len(raw)
    manifest = json.loads((tmp_path / "original-manifest.json").read_text())["documents"][0]
    assert manifest["sha256"] == sha256(raw).hexdigest()
    result = collector(tmp_path).originals([original_row()], max_files=1, byte_budget=100)
    assert len(calls) == 1 and result["new_bytes"] == 0
    path = tmp_path / manifest["path"]
    path.chmod(0o600)
    path.write_bytes(b"invalid")
    with pytest.raises(ValueError, match="integrity"):
        collector(tmp_path).originals([original_row()], max_files=1, byte_budget=100)


def test_other_campaign_original_is_reused_by_url_and_verified_hash(tmp_path, monkeypatch):
    previous, current = tmp_path / "previous", tmp_path / "current"
    previous.mkdir()
    current.mkdir()
    raw = b"%PDF-1.4\noriginal\n%%EOF"
    monkeypatch.setattr(campaign, "_download", lambda url, **kw: {"blob": kw["consume"]([raw]), "url": url})
    collector(previous).originals([original_row()], max_files=1, byte_budget=100)
    monkeypatch.setattr(
        campaign, "_download", lambda *a, **k: pytest.fail("existing original must not be downloaded")
    )
    result = collector(current).originals(
        [original_row()], max_files=1, byte_budget=100, reuse_campaigns=[previous]
    )
    assert result["reused"] == 1 and result["new_bytes"] == 0


def test_wrong_original_signature_is_a_gap_without_publishing_a_blob(tmp_path, monkeypatch):
    monkeypatch.setattr(
        campaign, "_download", lambda url, **kw: {"blob": kw["consume"]([b"<html>blocked</html>"])}
    )
    result = collector(tmp_path).originals([original_row()], max_files=1, byte_budget=100)
    assert result["failed"] == 1 and result["downloaded"] == 0
    assert not list((tmp_path / "files/blobs").glob("*/*/*/*"))


def test_zero_file_budget_does_not_download(tmp_path, monkeypatch):
    monkeypatch.setattr(campaign, "_download", lambda *a, **k: pytest.fail("no transfer allowed"))
    result = collector(tmp_path).originals([original_row()], max_files=0, byte_budget=100)
    assert result["pending"] == 1 and result["new_bytes"] == 0


def test_originals_reserve_disk_free_floor_before_network_transfer(tmp_path, monkeypatch):
    runner = collector(tmp_path)
    runner.min_free_bytes = campaign.MIN_FREE_DISK_BYTES
    usage = campaign.shutil.disk_usage(tmp_path)
    monkeypatch.setattr(
        campaign.shutil,
        "disk_usage",
        lambda path: usage._replace(free=campaign.MIN_FREE_DISK_BYTES + 99),
    )
    monkeypatch.setattr(campaign, "_download", lambda *a, **k: pytest.fail("disk reserve reached"))
    result = runner.originals([original_row()], max_files=1, byte_budget=100)
    assert result["storage_pending"] == result["pending"] == 1
    assert result["new_bytes"] == 0 and not list((tmp_path / "originals").glob("*.json"))


def test_retry_after_persists_host_cooldown_and_prevents_new_requests_on_resume(tmp_path, monkeypatch):
    from app.ingestion import DownloadError

    calls = []

    def download(url, **kwargs):
        calls.append(url)
        raise DownloadError(
            "HTTP 429", retryable=True, retry_after=120, response_host="assets.egazette.gov.sg"
        )

    monkeypatch.setattr(campaign, "_download", download)
    first = collector(tmp_path).originals([original_row()], max_files=1, byte_budget=100)
    assert first["failed"] == 1
    other = original_row()
    other["download_url"] = row(1)["fileUrl"]
    second = collector(tmp_path).originals([other], max_files=1, byte_budget=100)
    assert second["cooldown_pending"] == second["pending"] == 1 and len(calls) == 1
    assert len(list((tmp_path / "originals").glob("*.json"))) == 1
    runner = collector(tmp_path)
    with pytest.raises(campaign.HostCooling):
        with runner.slot("assets.egazette.gov.sg"):
            pytest.fail("cooled host must not acquire a new network lease")
    assert runner.hops == 0


def staged_fixture(tmp_path, monkeypatch):
    raw = b"%PDF-1.4\nqueued official original\n%%EOF"

    def download(url, **kwargs):
        if url == campaign.EGAZETTE_SEARCH_URL:
            return response(0, total=1)
        return {"blob": kwargs["consume"]([raw]), "url": url}

    monkeypatch.setattr(campaign, "_download", download)
    runner = collector(tmp_path)
    runner.partition("Revised Acts", 2021)
    rows = runner.rows()
    runner.originals(rows, max_files=1, byte_budget=100)
    monkeypatch.setattr(campaign, "_download", lambda *a, **k: pytest.fail("staging is offline"))
    return runner, rows, raw


def test_acquired_original_stages_idempotent_parse_job_and_transferable_source_evidence(
    tmp_path, monkeypatch
):
    from app.collection_import import import_collected_resources
    from app.service import Service
    from app.system_library import SYSTEM_OWNER_ID, ensure_principal

    source = tmp_path / "source"
    source.mkdir()
    runner, rows, raw = staged_fixture(source, monkeypatch)
    first = runner.stage_originals(rows)
    second = runner.stage_originals(rows)
    assert first == second
    assert first["staged_originals"] == first["campaign_documents"] == first["processing_jobs"] == 1
    assert first["workers_started"] is False and first["model_calls"] == 0
    service = Service(source)
    with service.db.connect() as conn:
        doc = dict(conn.execute("SELECT * FROM documents").fetchone())
        assert conn.execute("SELECT phase,status FROM processing_jobs").fetchone()[:] == ("parse", "queued")
        assert conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 0
    assert doc["version"] == 1 and doc["source_url"] == rows[0]["download_url"]
    assert Path(doc["storage_path"]).read_bytes() == raw
    assert service.catalog.enrich_document(doc)["metadata"]["review_status"] == "draft"
    evidence = json.loads((source / "campaign.json").read_text())["documents"].values()
    proof = next(iter(evidence))["catalogue"][0]
    for reference in (proof["raw"], proof["receipt"]):
        assert sha256(Path(reference["cas_path"]).read_bytes()).hexdigest() == reference["sha256"]
    target = Service(tmp_path / "target")
    with target.db.transaction() as conn:
        ensure_principal(conn)
    report = import_collected_resources(source, tmp_path / "target", campaign.SOURCE_OWNER, SYSTEM_OWNER_ID)
    assert report["blockers"] == [] and report["source_evidence_documents"] == 1
    assert report["verified_owner_blobs"] == 4 and report["model_calls"] == report["HTTP_calls"] == 0


@pytest.mark.parametrize("failure", ["outside_root", "wrong_hash", "not_catalogued"])
def test_original_staging_rejects_invalid_or_unmapped_receipt(tmp_path, monkeypatch, failure):
    runner, rows, raw = staged_fixture(tmp_path, monkeypatch)
    path = next((tmp_path / "originals").glob("*.json"))
    receipt = json.loads(path.read_text())
    if failure == "outside_root":
        receipt.update(path="/etc/passwd", status="reused")
    elif failure == "wrong_hash":
        receipt["sha256"] = "a" * 64
    else:
        rows = []
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError):
        runner.stage_originals(rows)
    from app.service import Service

    with Service(tmp_path).db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
