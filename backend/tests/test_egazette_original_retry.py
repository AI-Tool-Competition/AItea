"""A transient original gap gets one explicit, evidenced and restart-safe retry."""

from hashlib import sha256
import json
import os
from pathlib import Path
import time

import pytest

from test_egazette_campaign import campaign, collector, original_row, response


def failed_source(tmp_path, monkeypatch, *, error=None, with_catalog=False):
    clock = [time.time()]
    monkeypatch.setattr(campaign.time, "time", lambda: clock[0])
    calls = []
    failure = error or campaign.DownloadError("connection timeout", retryable=True)

    def download(url, **kwargs):
        if url == campaign.EGAZETTE_SEARCH_URL:
            return response(0, total=1)
        calls.append(url)
        raise failure

    monkeypatch.setattr(campaign, "_download", download)
    runner = collector(tmp_path)
    if with_catalog:
        runner.partition("Revised Acts", 2021)
        rows = runner.rows()
    else:
        rows = [original_row()]
    assert runner.originals(rows, max_files=1, byte_budget=1000)["failed"] == 1
    path = next((tmp_path / "originals").glob("*.json"))
    return rows, clock, calls, path


def success(monkeypatch, calls):
    raw = b"%PDF-1.4\nrecovered original\n%%EOF"

    def download(url, **kwargs):
        calls.append(url)
        return {"blob": kwargs["consume"]([raw]), "url": url, "mime_type": "application/pdf"}

    monkeypatch.setattr(campaign, "_download", download)
    return raw


def test_default_resume_and_backoff_do_not_request_or_change_failed_receipt(tmp_path, monkeypatch):
    rows, clock, calls, path = failed_source(tmp_path, monkeypatch)
    before = path.read_bytes()
    success(monkeypatch, calls)
    assert collector(tmp_path).originals(rows, max_files=1, byte_budget=1000)["failed"] == 1
    waiting = collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)
    assert waiting["retry_pending"] == 1 and len(calls) == 1 and path.read_bytes() == before
    clock[0] += 61
    assert (
        collector(tmp_path).originals(rows, max_files=0, byte_budget=1000, retry_incomplete=True)["pending"]
        == 1
    )
    assert path.read_bytes() == before and len(calls) == 1


def test_retry_preserves_byte_exact_failed_receipt_and_never_repeats_success(tmp_path, monkeypatch):
    rows, clock, calls, path = failed_source(tmp_path, monkeypatch)
    before = path.read_bytes()
    raw = success(monkeypatch, calls)
    clock[0] += 61
    result = collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)
    assert result["downloaded"] == result["retry_attempts_this_run"] == 1
    saved = json.loads(path.read_text())
    assert saved["attempt_count"] == 2 and saved["sha256"] == sha256(raw).hexdigest()
    history = campaign.original_attempts(tmp_path, saved)
    assert (tmp_path / history[0]["path"]).read_bytes() == before
    collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)
    assert len(calls) == 2


def test_legacy_connection_timeout_can_retry_after_receipt_age(tmp_path, monkeypatch):
    rows, clock, calls, path = failed_source(tmp_path, monkeypatch)
    saved = json.loads(path.read_text())
    for key in ("retry_not_before", "attempt_count", "attempted_at"):
        saved.pop(key)
    saved["error"] = {"type": "DownloadError", "message": "抓取连接失败或超时，请检查来源后重试。"}
    path.write_text(json.dumps(saved))
    os.utime(path, (clock[0] - 61, clock[0] - 61))
    success(monkeypatch, calls)
    assert (
        collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)[
            "downloaded"
        ]
        == 1
    )
    assert len(calls) == 2


@pytest.mark.parametrize(
    "error",
    [
        campaign.DownloadError("HTTP 403", retryable=True),
        campaign.DownloadError("HTTP 404"),
        ValueError("not a PDF"),
    ],
)
def test_denied_missing_and_invalid_content_are_not_retried(tmp_path, monkeypatch, error):
    rows, clock, calls, path = failed_source(tmp_path, monkeypatch, error=error)
    before = path.read_bytes()
    clock[0] += 1000
    success(monkeypatch, calls)
    assert (
        collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)["failed"]
        == 1
    )
    assert len(calls) == 1 and path.read_bytes() == before


def test_host_retry_after_survives_new_collector_and_defers_retry(tmp_path, monkeypatch):
    rows, clock, calls, path = failed_source(
        tmp_path,
        monkeypatch,
        error=campaign.DownloadError(
            "HTTP 429", retryable=True, retry_after=120, response_host="assets.egazette.gov.sg"
        ),
    )
    clock[0] += 61
    before = path.read_bytes()
    assert (
        collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)[
            "retry_pending"
        ]
        == 1
    )
    assert len(calls) == 1 and path.read_bytes() == before


def test_interrupted_reserved_retry_cannot_be_sent_again_after_restart(tmp_path, monkeypatch):
    rows, clock, calls, path = failed_source(tmp_path, monkeypatch)
    clock[0] += 61

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(campaign, "_download", interrupted)
    with pytest.raises(KeyboardInterrupt):
        collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)
    saved = json.loads(path.read_text())
    assert saved["attempt_count"] == 2 and saved["error"]["type"] == "RetryInterrupted"
    success(monkeypatch, calls)
    assert (
        collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)["failed"]
        == 1
    )
    assert len(calls) == 1


def test_failed_second_attempt_stays_a_gap_without_third_request(tmp_path, monkeypatch):
    rows, clock, calls, path = failed_source(tmp_path, monkeypatch)
    clock[0] += 61
    assert (
        collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)["failed"]
        == 1
    )
    clock[0] += 1000
    assert (
        collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)["failed"]
        == 1
    )
    assert len(calls) == 2 and json.loads(path.read_text())["attempt_count"] == 2


@pytest.mark.parametrize("damage", ["hash", "path", "identity"])
def test_corrupt_retry_history_is_rejected_without_network(tmp_path, monkeypatch, damage):
    rows, clock, calls, path = failed_source(tmp_path, monkeypatch)
    clock[0] += 61
    success(monkeypatch, calls)
    collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)
    saved = json.loads(path.read_text())
    if damage == "hash":
        history = tmp_path / saved["previous_attempts"][0]["path"]
        history.chmod(0o600)
        history.write_bytes(b"bad")
    elif damage == "path":
        saved["previous_attempts"][0]["path"] = "../elsewhere"
        path.write_text(json.dumps(saved))
    else:
        saved["source_url"] += "?other-version"
        path.write_text(json.dumps(saved))
    with pytest.raises(ValueError):
        collector(tmp_path).originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)
    assert len(calls) == 2


def test_failed_attempt_cas_survives_offline_staging_and_formal_import(tmp_path, monkeypatch):
    from app.collection_import import import_collected_resources
    from app.service import Service
    from app.system_library import SYSTEM_OWNER_ID, ensure_principal

    source = tmp_path / "source"
    source.mkdir()
    rows, clock, calls, path = failed_source(source, monkeypatch, with_catalog=True)
    before = path.read_bytes()
    clock[0] += 61
    success(monkeypatch, calls)
    runner = collector(source)
    runner.originals(rows, max_files=1, byte_budget=1000, retry_incomplete=True)
    runner.stage_originals(rows)
    runner.stage_originals(rows)
    state = json.loads((source / "campaign.json").read_text())
    evidence = next(iter(state["documents"].values()))["download_attempts"][0]
    assert Path(evidence["receipt"]["cas_path"]).read_bytes() == before
    target_path = tmp_path / "target"
    target = Service(target_path)
    with target.db.transaction() as conn:
        ensure_principal(conn)
    result = import_collected_resources(
        source, target_path, campaign.SOURCE_OWNER, SYSTEM_OWNER_ID, apply=True
    )
    assert not result["blockers"]
    with target.db.connect() as conn:
        manifest = json.loads(conn.execute("SELECT manifest FROM documents").fetchone()[0])
    receipt = manifest["collection_evidence"]["record"]["download_attempts"][0]["receipt"]
    assert not Path(receipt["cas_path"]).is_absolute()
    assert (target_path / receipt["cas_path"]).read_bytes() == before
    assert receipt["sha256"] == sha256(before).hexdigest()


def test_long_official_title_keeps_pdf_suffix_and_full_publication_identity(tmp_path, monkeypatch):
    from app.service import Service
    from test_egazette_campaign import staged_fixture

    title = "Food and Imports " + "Official amendment " * 20
    runner, rows, _ = staged_fixture(tmp_path, monkeypatch, title=title)
    runner.stage_originals(rows)
    with Service(tmp_path).db.connect() as conn:
        doc = conn.execute("SELECT filename,manifest FROM documents").fetchone()
    assert len(doc["filename"]) <= 180 and doc["filename"].endswith(".pdf")
    assert json.loads(doc["manifest"])["gazette_publication"]["title"] == title


def test_explicit_recovery_by_verified_reuse_also_preserves_failed_history(tmp_path, monkeypatch):
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    calls = []
    success(monkeypatch, calls)
    collector(source).originals([original_row()], max_files=1, byte_budget=1000)
    rows, clock, calls, path = failed_source(destination, monkeypatch)
    before = path.read_bytes()
    clock[0] += 61
    monkeypatch.setattr(campaign, "_download", lambda *a, **k: pytest.fail("verified reuse is offline"))
    result = collector(destination).originals(
        rows, max_files=1, byte_budget=1000, retry_incomplete=True, reuse_campaigns=[source]
    )
    assert result["reused"] == 1 and result["retry_attempts_this_run"] == 0
    saved = json.loads(path.read_text())
    entry = campaign.original_attempts(destination, saved)[0]
    assert (destination / entry["path"]).read_bytes() == before
