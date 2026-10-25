"""Campaigns preserve exact submissions and pipeline independent categories."""

import json
import subprocess
import sys
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest
from app.bootstrap import Service
from app.contexts.ingestion.application.resource_campaign import ResourceCampaign
from app.configuration.paths import PROJECT_ROOT
from app.shared.infrastructure.http import download as ingestion_download

PLAN = "indonesia-20261003"
HISTORICAL_PLAN_VERSION = 14


def options(*items, originals=1):
    return {
        "items": list(items),
        "confirm_items": [],
        "discovery_batches": 1,
        "originals_per_item": originals,
        "live_models": False,
    }


def test_restart_after_transaction_before_checkpoint_reuses_the_same_run(tmp_path, monkeypatch):
    service = Service(tmp_path)
    saved = []
    campaign = ResourceCampaign(
        service,
        PLAN,
        options("1.1"),
        version=HISTORICAL_PLAN_VERSION,
        checkpoint=lambda s: saved.append(deepcopy(s)),
    )
    start = service.checklists.start

    def interrupted(*args, **kwargs):
        start(*args, **kwargs)
        raise RuntimeError("process interrupted after commit")

    monkeypatch.setattr(service.checklists, "start", interrupted)
    with pytest.raises(RuntimeError):
        campaign.step()
    assert saved[-1]["items"]["1.1"]["submission"]["kind"] == "discovery"
    monkeypatch.setattr(service.checklists, "start", start)
    resumed = ResourceCampaign(service, PLAN, options("1.1"), state=saved[-1])
    assert resumed.step()
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 1
    assert len(resumed.state["items"]["1.1"]["discovery_runs"]) == 1
    assert "submission" not in resumed.state["items"]["1.1"]


def test_fast_category_starts_original_while_slow_category_is_still_discovering(tmp_path, monkeypatch):

    service = Service(tmp_path)
    campaign = ResourceCampaign(service, PLAN, options("1.1", "5.3"), version=13)
    campaign.step()
    fast_run = campaign.state["items"]["1.1"]["discovery_runs"][0]
    slow_run = campaign.state["items"]["5.3"]["discovery_runs"][0]

    def download(url, *, consume, **kwargs):
        body = b"%PDF-1.4\n%%EOF" if "s.mkri.id" in url else b"<title>UUD 1945</title><h1>Constitution</h1>"
        return {
            "url": url,
            "filename": "UUD.pdf",
            "mime_type": "application/pdf" if body.startswith(b"%PDF") else "text/html",
            "blob": consume(iter([body])),
        }

    monkeypatch.setattr(ingestion_download, "_download", download)
    # Keep the other source's claimed network jobs alive while completing the
    # two fast discovery seeds. No global discovery barrier is allowed.
    for _ in range(8):
        claim = service.collection.claim_fetch()
        if not claim:
            break
        if claim["run_id"] == fast_run:
            service.collection.fetch_item(claim)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT status FROM collection_runs WHERE id=?", (fast_run,)).fetchone()[
            0
        ] not in {"queued", "running"}
        assert conn.execute("SELECT status FROM collection_runs WHERE id=?", (slow_run,)).fetchone()[0] in {
            "queued",
            "running",
        }
    campaign.step()
    assert len(campaign.state["items"]["1.1"]["collection_runs"]) == 1
    assert not campaign.state["items"]["5.3"]["collection_runs"]


def test_conditional_categories_are_skipped_without_confirmation(tmp_path):
    service = Service(tmp_path)
    service.checklists.plans[PLAN]["items"][0]["conditional"] = True
    campaign = ResourceCampaign(service, PLAN, options("1.3"))
    assert not campaign.step()
    assert campaign.state["items"]["1.3"]["phase"] == "skipped"
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 0


def test_original_hash_verification_detects_tampering_and_keeps_drafts(tmp_path):
    service = Service(tmp_path)
    campaign = ResourceCampaign(service, PLAN, options("1.1"), version=HISTORICAL_PLAN_VERSION)
    owner = campaign.state["owner_id"]
    blob = service.archive.blobs.put_bytes(owner, b"original legal text")
    with service.db.transaction() as conn:
        document = service.archive.store_original(
            conn,
            owner,
            blob,
            "law.txt",
            "text/plain",
            "https://example.org/law.txt",
            collection="country:ID",
            level="primary",
            tags=[],
            identity_key="law",
        )
    campaign.verify_originals()
    assert campaign.state["integrity"][document["id"]]
    blob.path.chmod(0o600)
    blob.path.write_bytes(b"changed bytes")
    campaign.verify_originals()
    assert not campaign.state["integrity"][document["id"]]
    assert not service.search(owner, "legal", country="ID")["items"]


def test_resume_rejects_changed_scope_or_frozen_definition(tmp_path):
    service = Service(tmp_path)
    campaign = ResourceCampaign(service, PLAN, options("1.3"))
    with pytest.raises(ValueError, match="same frozen"):
        ResourceCampaign(service, PLAN, options("1.3", originals=2), state=deepcopy(campaign.state))
    service.checklists.plans[PLAN]["title"] += " changed without a version"
    with pytest.raises(ValueError, match="same frozen"):
        ResourceCampaign(service, PLAN, options("1.3"), state=deepcopy(campaign.state))


def test_campaign_resume_keeps_frozen_definition_bindings_and_report(tmp_path):
    service = Service(tmp_path)
    campaign = ResourceCampaign(service, PLAN, options("1.1"), version=HISTORICAL_PLAN_VERSION)
    frozen = deepcopy(campaign.plan)
    assert campaign.step()
    newer = deepcopy(frozen)
    newer["version"] += 1
    newer["title"] += " newer"
    service.checklists.plans[PLAN] = newer

    resumed = ResourceCampaign(service, PLAN, options("1.1"), state=deepcopy(campaign.state))
    assert resumed.plan == frozen
    assert resumed.report()["plan_version"] == frozen["version"]
    # The durable request survives publication of a newer blueprint. A new
    # request must still bind to the original version rather than the latest.
    record = resumed.state["items"]["1.1"]
    record["submission"] = {
        "kind": "discovery",
        "payload": {"client_request_id": "frozen-replay", "confirm_condition": False},
    }
    resumed._submit("1.1", record)
    with closing(service.db.connect()) as conn:
        versions = {row[0] for row in conn.execute("SELECT version FROM checklist_bindings")}
    assert versions == {frozen["version"]}
    fresh = ResourceCampaign(service, PLAN, options("1.1"))
    assert fresh.plan["version"] == newer["version"]


def test_resume_rejects_another_plan_before_creating_account_or_binding(tmp_path):
    service = Service(tmp_path)
    campaign = ResourceCampaign(service, PLAN, options("1.1"), version=HISTORICAL_PLAN_VERSION)
    with pytest.raises(ValueError, match="same frozen"):
        ResourceCampaign(service, "singapore-demo-20261009", options("1.1"), state=deepcopy(campaign.state))
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM checklist_bindings").fetchone()[0] == 0


def test_explicit_extension_preserves_account_progress_and_submission_identity(tmp_path):
    service = Service(tmp_path)
    campaign = ResourceCampaign(service, PLAN, options("1.1"), version=HISTORICAL_PLAN_VERSION)
    campaign.step()
    record = campaign.state["items"]["1.1"]
    previous_run = record["discovery_runs"][0]
    record["phase"] = "bounded"
    extended = ResourceCampaign(
        service,
        PLAN,
        options("1.1", originals=2),
        version=HISTORICAL_PLAN_VERSION,
        state=deepcopy(campaign.state),
        extend=True,
    )
    assert extended.state["owner_id"] == campaign.state["owner_id"]
    assert extended.state["items"]["1.1"]["discovery_runs"] == [previous_run]
    assert extended.state["items"]["1.1"]["phase"] == "waiting"
    assert extended.state["options"]["originals_per_item"] == 2
    assert extended.step()
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    with pytest.raises(ValueError, match="only increase"):
        ResourceCampaign(
            service,
            PLAN,
            options("1.1"),
            version=HISTORICAL_PLAN_VERSION,
            state=deepcopy(extended.state),
            extend=True,
        )


def test_claims_rotate_runs_on_a_shared_host_and_keep_connection_limits(tmp_path, monkeypatch):
    service = Service(tmp_path)
    campaign = ResourceCampaign(service, PLAN, options("1.1"), version=HISTORICAL_PLAN_VERSION)
    owner = campaign.state["owner_id"]
    monkeypatch.setattr(service.collection, "host_workers", 1)
    runs = []
    for number in range(2):
        source = service.save_source(
            owner,
            {
                "name": f"Type {number}",
                "url": f"https://example.org/type-{number}.txt",
                "enabled": False,
                "interval_hours": 24,
                "tags": [],
                "plan": {
                    "discover_attachments": False,
                    "seed_urls": [f"https://example.org/file-{number}.txt"],
                },
            },
        )
        runs.append(service.collection.enqueue(owner, source["id"], {"client_request_id": f"run-{number}"}))
    first = service.collection.claim_fetch()
    assert first["run_id"] == runs[0]["id"]
    assert service.collection.claim_fetch() is None  # Active physical job's host still counts.
    from app.shared.infrastructure.http import download as ingestion_download

    monkeypatch.setattr(
        ingestion_download,
        "_download",
        lambda url, consume, **kw: {
            "url": url,
            "filename": "law.txt",
            "mime_type": "text/plain",
            "blob": consume(iter([b"Tea import permit."])),
        },
    )
    service.collection.fetch_item(first)
    second = service.collection.claim_fetch()
    assert second["run_id"] == runs[1]["id"]
    with closing(service.db.connect()) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM collection_items WHERE run_id=? AND status='queued'", (runs[0]["id"],)
            ).fetchone()[0]
            == 1
        )


def test_cli_preserves_nonempty_output_and_resumes_an_owned_campaign(tmp_path):
    script = PROJECT_ROOT / "scripts/collect_resources.py"
    output = tmp_path / "existing"
    output.mkdir()
    untouched = output / "user.txt"
    untouched.write_text("keep")
    command = [sys.executable, str(script), "--output", str(output), "--items", "1.1", "--time-budget", "10"]
    refused = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert refused.returncode != 0 and untouched.read_text() == "keep"
    assert not (output / "tea.sqlite3").exists() and not (output / "campaign.lock").exists()

    isolated = tmp_path / "campaign"
    isolated.mkdir()
    campaign = ResourceCampaign(
        Service(isolated),
        PLAN,
        options("1.1", originals=0),
        version=HISTORICAL_PLAN_VERSION,
        checkpoint=lambda state: (isolated / "campaign.json").write_text(json.dumps(state)),
    )
    campaign.state["items"]["1.1"].update(phase="blocked", error="Source needs an adapter")
    campaign.save()
    resumed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--output",
            str(isolated),
            "--items",
            "1.1",
            "--resume",
            "--time-budget",
            "10",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert resumed.returncode == 2, resumed.stdout + resumed.stderr
    report = json.loads((isolated / "report.json").read_text())
    assert report["status"] == "finished_with_gaps"
    assert report["legal_review"] == "draft_only_not_legal_approval"
    assert Path(report["inventory"]["path"]).is_dir()
    with closing(campaign.service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0] == 0


def test_frozen_report_excludes_errors_from_newer_bindings_for_same_account(tmp_path):
    service = Service(tmp_path)
    campaign = ResourceCampaign(service, PLAN, options("1.1"), version=HISTORICAL_PLAN_VERSION)
    assert campaign.step()
    old_run = campaign.state["items"]["1.1"]["discovery_runs"][0]
    newer = deepcopy(campaign.plan)
    newer["version"] += 1
    service.checklists.plans[PLAN] = newer
    new_run = service.checklists.start(
        campaign.state["owner_id"],
        PLAN,
        "1.1",
        {"client_request_id": "new-version-failure", "confirm_condition": False},
        kind="discovery",
        version=newer["version"],
    )
    with service.db.transaction() as conn:
        frozen_item_count = conn.execute(
            "SELECT COUNT(*) FROM collection_items WHERE run_id=?", (old_run,)
        ).fetchone()[0]
        conn.execute(
            "UPDATE collection_items SET error='new blueprint only' WHERE run_id=?", (new_run["id"],)
        )
        conn.execute("UPDATE collection_items SET error='frozen blueprint only' WHERE run_id=?", (old_run,))
    errors = campaign.report()["items"][0]["errors"]
    assert len(errors) == frozen_item_count
    assert all(error["error"] == "frozen blueprint only" for error in errors)
