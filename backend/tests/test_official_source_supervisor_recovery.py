"""Real SQLite/CAS recovery after collector loss, without HTTP or model calls."""

from __future__ import annotations

import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.bootstrap import Service
from app.contexts.ingestion.infrastructure.source_adapters.egazette import (
    EGAZETTE_APPLICATION,
    EGAZETTE_INDEX,
)
from app.configuration.paths import PROJECT_ROOT
from app.shared.infrastructure.http.public_search import EGazetteSearchRequest

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT / "scripts"))
try:
    recovery = importlib.import_module("official_source_supervisor_recovery")
    supervisor = importlib.import_module("official_source_supervisor")
    sg_module = importlib.import_module("collect_egazette")
    id_module = importlib.import_module("collect_indonesia_official")
finally:
    sys.path.pop(0)


def frontend():
    stream = (
        "1:"
        + json.dumps(
            {
                "config": {
                    "type": "egazette-algolia",
                    "appId": EGAZETTE_APPLICATION,
                    "indexName": EGAZETTE_INDEX,
                    "searchApiKey": "a" * 32,
                }
            }
        )
        + "\n"
    )
    return ("<script>self.__next_f.push(" + json.dumps([1, stream]) + ")</script>").encode()


def configure_supervisor(monkeypatch, context):
    for key, value in {
        "P": ROOT,
        "DATA_ROOT": context.data,
        "BUNDLE_ROOT": context.bundle,
        "CONTROL_ROOT": context.control,
        "SG": context.root,
        "TEMPLATE": context.bundle / "templates",
        "MIN_FREE": 0,
        "STATE": {},
        "STOP": __import__("threading").Event(),
    }.items():
        monkeypatch.setattr(supervisor, key, value)


@pytest.fixture
def sg(tmp_path, monkeypatch):
    root = tmp_path / "campaigns/sg/egazette-legislative-round3-full"
    root.mkdir(parents=True)
    rows = [
        {
            "objectID": f"example-{i}",
            "title": f"Example {i} Act 2021",
            "notificationNum": str(i),
            "fileUrl": f"https://assets.egazette.gov.sg/2021/Legislative%20Supplements/Revised%20Acts/{i}.pdf",
            "category": "Legislative Supplements",
            "subCategory": "Revised Acts",
            "publishYear": 2021,
            "publishTimestamp": 1640880000000,
        }
        for i in range(5)
    ]

    def download(url, **kwargs):
        if url == sg_module.EGAZETTE_SEARCH_URL:
            return {
                "content": json.dumps(
                    {
                        "page": 0,
                        "nbHits": 5,
                        "nbPages": 1,
                        "hitsPerPage": 10,
                        "exhaustiveNbHits": True,
                        "hits": rows,
                    }
                ).encode(),
                "url": url,
                "mime_type": "application/json",
            }
        return {"blob": kwargs["consume"]([b"%PDF-1.4\n" + url.encode() + b"\n%%EOF"]), "url": url}

    monkeypatch.setattr(sg_module, "_download", download)
    runner = sg_module.GazetteCollector(
        root, EGazetteSearchRequest("a" * 32, "b" * 64), page_size=10, min_free_bytes=0
    )
    runner.partition("Revised Acts", 2021)
    selected = runner.rows()
    runner.originals(selected[:1], max_files=1, byte_budget=200 * 1024**2)
    runner.stage_originals(selected)
    with sqlite3.connect(root / "tea.sqlite3") as conn:
        doc_id, raw = conn.execute("SELECT id,manifest FROM documents").fetchone()
        manifest = json.loads(raw)
        manifest["pages"] = [{"page": 1, "status": "native", "text": "Already successful physical page"}]
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), doc_id))
    (root / "frontend.html").write_bytes(frontend())
    recovery.write(root / "selected-records.json", selected)
    recovery.write(
        root / "frozen-catalogue-files.json",
        {
            str(path.relative_to(root)): recovery.digest(path)
            for path in (root / "catalog").rglob("*")
            if path.is_file()
        },
    )
    recovery.write(
        root / "plan.json",
        {
            "selected_sha256": recovery.digest(root / "selected-records.json"),
            "frontend_sha256": recovery.digest(root / "frontend.html"),
            "copied_catalogue_hashes_sha256": recovery.digest(root / "frozen-catalogue-files.json"),
        },
    )
    recovery.write(root / "live-progress.json", {"elapsed_seconds": 110.0})
    bundle, control = tmp_path / "bundle", tmp_path / "operations/official-source-supervisor"
    (bundle / "templates").mkdir(parents=True)
    window = control / "sg/window-0001"
    window.mkdir(parents=True)
    for name in ("collect_sg_round3_full.py", "process_sg_round3_native.py"):
        (bundle / "templates" / name).write_text("# sealed test template\n")
        (window / name).write_bytes((bundle / "templates" / name).read_bytes())
    plan = {
        "source_dirs": {"sg": str(root.relative_to(tmp_path))},
        "source_owners": {"sg": sg_module.SOURCE_OWNER},
        "code_sha256": {"scripts/collect_egazette.py": recovery.digest(ROOT / "scripts/collect_egazette.py")},
        "scope_bindings": [
            {
                "root": "data",
                "path": str((root / name).relative_to(tmp_path)),
                "sha256": recovery.digest(root / name),
            }
            for name in ("plan.json", "selected-records.json", "frontend.html")
        ],
        "per_window_seconds": 20,
        "max_total_original_attempts": 8,
        "min_free_bytes": 0,
        "worker_limits": {"sg_host": 2},
        "selected_remaining_initial": {"sg": 5},
    }
    recovery.write(bundle / "plan.json", plan)
    recovery.write(
        window / "previous-report.json",
        {
            "elapsed_seconds_total": 100.0,
            "elapsed_seconds": 10,
            "status_counts": {"downloaded": 1},
            "remaining_unattempted": 4,
        },
    )
    binding = {
        "protocol": "egazette-controlled-window-v1",
        "base_plan_sha256": recovery.digest(root / "plan.json"),
        "selected_sha256": recovery.digest(root / "selected-records.json"),
        "previous_final_report_sha256": recovery.digest(window / "previous-report.json"),
        "supervisor_plan_sha256": recovery.digest(bundle / "plan.json"),
        "official_downloader_sha256": plan["code_sha256"]["scripts/collect_egazette.py"],
        "additional_budget_seconds": 20,
        "previous_elapsed_seconds_total": 100.0,
        "effective_total_budget_seconds": 120.0,
        "max_original_attempts": 8,
        "host_limit": 2,
        "models": False,
        "scope_change": False,
        "min_free_bytes": 0,
    }
    recovery.write(window / "window-plan.json", binding)
    ctx = SimpleNamespace(
        root=root,
        data=tmp_path,
        bundle=bundle,
        control=control,
        window=window,
        plan=plan,
        runner=runner,
        rows=selected,
        old_id=doc_id,
    )
    ctx.arguments = dict(
        source="sg",
        window=window,
        project=ROOT,
        data=tmp_path,
        bundle=bundle,
        control=control,
        plan=plan,
        modules={"sg": sg_module},
        saved_state={},
    )
    return ctx


def finish_receipts(context, monkeypatch):
    context.runner.originals(context.rows, max_files=5, byte_budget=200 * 1024**2)
    monkeypatch.setattr(sg_module, "_download", lambda *a, **k: pytest.fail("recovery must never redownload"))


def test_abrupt_collector_death_reuses_four_saved_unstaged_originals(sg, monkeypatch):
    # Actual abrupt process exit after saving four CAS receipts, before staging.
    script = sg.data / "interrupted_collector.py"
    script.write_text("""import sys, os, json
from pathlib import Path
sys.path[:0] = [sys.argv[1] + '/scripts', sys.argv[1] + '/backend']
import collect_egazette as m
from app.shared.infrastructure.http.public_search import EGazetteSearchRequest
root = Path(sys.argv[2])
def local(url, **options):
    return {'blob': options['consume']([b'%PDF-1.4\\n' + url.encode() + b'\\n%%EOF']), 'url': url}
m._download = local
runner = m.GazetteCollector(root, EGazetteSearchRequest('a'*32,'b'*64), min_free_bytes=0)
runner.originals(json.loads((root/'selected-records.json').read_text()), max_files=5, byte_budget=200*1024**2)
os._exit(7)
""")
    process = subprocess.run(
        [sys.executable, str(script), str(ROOT), str(sg.root)], check=False, env=os.environ.copy()
    )
    assert process.returncode == 7
    monkeypatch.setattr(sg_module, "_download", lambda *a, **k: pytest.fail("no new HTTP"))
    before = recovery.capture_guard(sg.root, sg_module.SOURCE_OWNER)
    receipts = recovery.sg_inventory(sg.root, sg_module, sg.rows)
    result = recovery.recover_interrupted_window(**sg.arguments)
    assert result["new_documents"] == 4
    assert result["normal_collector_completion"] is False
    assert result["network_requests"] == result["model_calls"] == 0
    report = recovery.load(sg.root / "continuation-report.json")
    assert report["elapsed_seconds_total"] == 120.0  # Unknown unused budget is forfeited.
    assert report["elapsed_seconds"] == 10.0
    assert report["remaining_unattempted"] == 0
    assert result["source_scope_complete"] is False
    after = recovery.capture_guard(sg.root, sg_module.SOURCE_OWNER)
    recovery.assert_preserved(before, after)
    assert len(after["documents"]) == 5
    assert recovery.sg_inventory(sg.root, sg_module, sg.rows) == receipts
    assert recovery.recover_interrupted_window(**sg.arguments) is None
    assert recovery.capture_guard(sg.root, sg_module.SOURCE_OWNER) == after


@pytest.mark.parametrize("failure_point", ["after_stage", "after_terminal"])
def test_recovery_itself_can_be_interrupted_without_duplicate_version_or_attempt(
    sg, monkeypatch, failure_point
):
    finish_receipts(sg, monkeypatch)
    original_write = recovery.write
    triggered = False

    def interrupt(path, value, **kwargs):
        nonlocal triggered
        if not triggered and Path(path).name == (
            "after.json" if failure_point == "after_stage" else "final-report.json"
        ):
            triggered = True
            raise OSError("simulated crash while publishing recovery")
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(recovery, "write", interrupt)
    with pytest.raises(OSError, match="simulated crash"):
        recovery.recover_interrupted_window(**sg.arguments)
    mid = recovery.capture_guard(sg.root, sg_module.SOURCE_OWNER)
    assert len(mid["documents"]) == 5
    result = recovery.recover_interrupted_window(**sg.arguments)
    assert result["new_documents"] == 4  # Counted against the persisted first baseline.
    assert recovery.capture_guard(sg.root, sg_module.SOURCE_OWNER) == mid


def test_retained_failed_receipts_are_not_erased_by_offline_recovery(sg, monkeypatch):
    finish_receipts(sg, monkeypatch)
    old_url = recovery.capture_guard(sg.root, sg_module.SOURCE_OWNER)["documents"][sg.old_id]["source_url"]
    receipt_path = next(
        path
        for path in (sg.root / "originals").glob("*.json")
        if recovery.load(path)["source_url"] != old_url
    )
    receipt = recovery.load(receipt_path)
    prior = {key: receipt[key] for key in ("source_url", "title", "catalog_locators")}
    prior |= {"status": "incomplete", "error": {"type": "DownloadError", "message": "saved timeout"}}
    service = Service(sg.root)
    blob = service.archive.blobs.put_bytes(sg_module.SOURCE_OWNER, recovery.encoded(prior))
    receipt["attempt_count"] = 2
    receipt["previous_attempts"] = [
        {"attempt": 1, "sha256": blob.digest, "size": blob.size, "path": str(blob.path.relative_to(sg.root))}
    ]
    recovery.write(receipt_path, receipt)
    before = recovery.sg_inventory(sg.root, sg_module, sg.rows)
    recovery.recover_interrupted_window(**sg.arguments)
    assert recovery.sg_inventory(sg.root, sg_module, sg.rows) == before
    assert recovery.load(sg.root / "continuation-report.json")["retained_prior_failure_receipts"] == 1


@pytest.mark.parametrize(
    "held_lock", ["collector-process.lock", "continuation.lock", "native-processing.lock"]
)
def test_held_lock_prevents_recovery_and_does_not_replace_receipts(sg, held_lock):
    target = sg.window / held_lock if held_lock.startswith("collector") else sg.root / held_lock
    with recovery.lock(target), pytest.raises(recovery.RecoveryDeferred):
        recovery.recover_interrupted_window(**sg.arguments)
    assert not (sg.window / "interrupted-recovery").exists()
    assert not (sg.root / "continuation-report.json").exists()


def test_alive_pid_without_source_lock_is_observed_even_after_long_timeout(sg):
    sg.arguments["saved_state"] = {
        "window": str(sg.window),
        "child_identity": recovery.identity(os.getpid()),
        "updated_at": "2000-01-01T00:00:00Z",
    }
    with pytest.raises(recovery.RecoveryDeferred, match="still alive"):
        recovery.recover_interrupted_window(**sg.arguments)
    assert not (sg.window / "interrupted-recovery").exists()


def test_inherited_process_lock_survives_missing_pid_record(sg):
    script = sg.data / "wait.py"
    script.write_text("import time\ntime.sleep(5)\n")
    with (sg.data / "child.log").open("w") as output:
        child = recovery.spawn_owned(
            [sys.executable, str(script)],
            window=sg.window,
            kind="collector",
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=output,
        )
    try:
        (sg.window / "collector-process.json").unlink()  # Supervisor died before writing its PID.
        with pytest.raises(recovery.RecoveryDeferred, match="active lock"):
            recovery.recover_interrupted_window(**sg.arguments)
    finally:
        child.terminate()  # Only this test's exact owned process.
        child.wait(timeout=10)


@pytest.mark.parametrize(
    "mutation", ["selected", "previous_report", "template", "receipt_hash", "traversal", "catalogue", "disk"]
)
def test_invalid_recovery_inputs_stop_without_staging(sg, monkeypatch, mutation):
    finish_receipts(sg, monkeypatch)
    if mutation == "selected":
        recovery.write(sg.root / "selected-records.json", sg.rows[:-1])
    elif mutation == "previous_report":
        recovery.write(sg.window / "previous-report.json", {"elapsed_seconds_total": 0})
    elif mutation == "template":
        (sg.window / "collect_sg_round3_full.py").write_text("changed")
    elif mutation == "catalogue":
        next((sg.root / "catalog").rglob("*.json")).write_text("{}")
    elif mutation in {"receipt_hash", "traversal"}:
        receipt_path = next((sg.root / "originals").glob("*.json"))
        receipt = recovery.load(receipt_path)
        receipt["sha256" if mutation == "receipt_hash" else "path"] = (
            "0" * 64 if mutation == "receipt_hash" else "../outside"
        )
        recovery.write(receipt_path, receipt)
    else:
        monkeypatch.setattr(recovery.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    with pytest.raises((ValueError, recovery.RecoveryDeferred)):
        recovery.recover_interrupted_window(**sg.arguments)
    assert not (sg.root / "continuation-report.json").exists()
    with sqlite3.connect(sg.root / "tea.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1


def test_same_window_native_consumer_is_not_killed_and_exits_after_terminal(sg, monkeypatch):
    finish_receipts(sg, monkeypatch)
    script = sg.window / "process_sg_round3_native.py"
    script.write_text(
        """import fcntl,time
from pathlib import Path
root=Path("""
        + repr(str(sg.root))
        + """)
with (root/'native-processing.lock').open('a') as lock:
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    (root/'native-ready').write_text('ready')
    while not (root/'continuation-report.json').exists():
        time.sleep(0.02)
(root/'native-drained').write_text('normally')
"""
    )
    (sg.bundle / "templates" / script.name).write_bytes(script.read_bytes())
    with (sg.data / "native.log").open("w") as output:
        child = recovery.spawn_owned(
            [sys.executable, str(script)],
            window=sg.window,
            kind="native",
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=output,
        )
    try:
        deadline = time.monotonic() + 5
        while not (sg.root / "native-ready").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (sg.root / "native-ready").exists()
        result = recovery.recover_interrupted_window(**sg.arguments)
        assert result["native"]["active"] is True
        assert child.wait(timeout=5) == 0
        assert (sg.root / "native-drained").read_text() == "normally"
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def test_next_window_cannot_refund_unknown_budget(sg, monkeypatch):
    finish_receipts(sg, monkeypatch)
    recovery.recover_interrupted_window(**sg.arguments)
    configure_supervisor(monkeypatch, sg)
    recovery.write(sg.bundle / "templates/window-plan.json", recovery.load(sg.window / "window-plan.json"))
    window, _, _ = supervisor.new_window("sg", sg.plan)
    next_plan = recovery.load(window / "window-plan.json")
    assert next_plan["previous_elapsed_seconds_total"] == 120
    assert next_plan["effective_total_budget_seconds"] == 140
    assert recovery.pending_window(sg.control, "sg") == window


def test_offline_recovery_audit_blocks_network_and_child_processes():
    import socket

    for operation in (
        lambda: socket.getaddrinfo("example.com", 443),
        lambda: subprocess.run([sys.executable, "-c", "pass"], check=True),
    ):
        with pytest.raises(RuntimeError, match="offline source recovery forbids"):
            recovery.offline_only(operation)()


def test_setneg_journal_recovery_preserves_attempts_and_partial_export(tmp_path, monkeypatch):
    root = tmp_path / "campaigns/id/setneg"
    conn = id_module.open_state(
        root, resume=False, definition_hash="frozen-definition", storage_root=tmp_path
    )
    with conn:
        conn.execute(
            "INSERT INTO tasks(item_code,url,purpose,status,attempts,result) VALUES(?,?,?,?,?,?)",
            (
                "1.3",
                "https://jdih.setneg.go.id/api/hukumproduk/pdf?f=missing",
                "document",
                "failed",
                8,
                json.dumps(
                    {
                        "status": "failed",
                        "retryable": True,
                        "error": "historical timeout",
                        "retry_not_before": 9999999999,
                    }
                ),
            ),
        )
        conn.execute(
            "INSERT INTO attempts(item_code,url,result) VALUES('1.3','preserved','historical evidence')"
        )
        conn.execute("INSERT INTO config VALUES('cooldown_until','9999999999')")
    expected = recovery._journal_fingerprint(conn)
    id_module.export_state(conn, root)
    conn.close()
    id_module.prepare_library(root, min_free_bytes=0)
    (root / "report.json.tmp").write_bytes(b'{"unfinished":')
    bundle, control = tmp_path / "bundle", tmp_path / "operations/official-source-supervisor"
    bundle.mkdir()
    control.mkdir(parents=True)
    plan = {
        "source_dirs": {"id": "campaigns/id/setneg"},
        "source_owners": {"id": id_module.SOURCE_OWNER},
        "code_sha256": {
            "scripts/collect_indonesia_official.py": recovery.digest(
                ROOT / "scripts/collect_indonesia_official.py"
            )
        },
        "scope_bindings": [],
        "per_window_seconds": 20,
        "min_free_bytes": 0,
        "cumulative_byte_budgets": {"id": 32 * 1024**3},
        "worker_limits": {"id_scheduler": 8, "id_host": 4},
    }
    recovery.write(bundle / "plan.json", plan)
    ctx = SimpleNamespace(root=root, data=tmp_path, bundle=bundle, control=control)
    configure_supervisor(monkeypatch, ctx)
    monkeypatch.setattr(supervisor, "ID", root)
    window, _, _ = supervisor.new_window("id", plan)
    (window / "window-plan.json").unlink()  # Legacy v2 kept command/previous report, without a window plan.
    result = recovery.recover_interrupted_window(
        source="id",
        window=window,
        project=ROOT,
        data=tmp_path,
        bundle=bundle,
        control=control,
        plan=plan,
        modules={"id": id_module},
        saved_state={},
    )
    assert result["new_documents"] == 0 and result["normal_collector_completion"] is False
    with sqlite3.connect(root / "journal.sqlite3") as conn:
        assert recovery._journal_fingerprint(conn) == expected
    preserved = list((window / "interrupted-recovery").glob("report.json.tmp.*"))
    assert len(preserved) == 1 and preserved[0].read_bytes() == b'{"unfinished":'
    assert recovery.load(root / "report.json")["requested_scope"]["complete"] is False


def test_recovery_detects_changed_successful_page_on_resume(sg, monkeypatch):
    finish_receipts(sg, monkeypatch)
    original_write = recovery.write

    def interrupt(path, value, **kwargs):
        if Path(path).name == "after.json":
            raise OSError("save interrupted")
        return original_write(path, value, **kwargs)

    monkeypatch.setattr(recovery, "write", interrupt)
    with pytest.raises(OSError):
        recovery.recover_interrupted_window(**sg.arguments)
    with sqlite3.connect(sg.root / "tea.sqlite3") as conn:
        raw = conn.execute("SELECT manifest FROM documents WHERE id=?", (sg.old_id,)).fetchone()[0]
        manifest = json.loads(raw)
        manifest["pages"][0]["text"] = "changed behind the checkpoint"
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), sg.old_id))
    with pytest.raises(ValueError, match="already successful page changed"):
        recovery.recover_interrupted_window(**sg.arguments)
    assert not (sg.window / "final-report.json").exists()


def test_a_final_report_does_not_allow_duplicate_still_starting_native(sg):
    recovery.write(sg.window / "final-report.json", {"recovery": {"status": "interrupted_reconciled"}})
    script = sg.data / "not_yet_locked.py"
    script.write_text("import time\ntime.sleep(5)\n")
    with (sg.data / "child.log").open("w") as output:
        child = recovery.spawn_owned(
            [sys.executable, str(script)],
            window=sg.window,
            kind="native",
            cwd=ROOT,
            env=os.environ.copy(),
            stdout=output,
        )
    try:
        (sg.window / "native-process.json").unlink()
        with pytest.raises(recovery.RecoveryDeferred, match="active lock"):
            recovery.observe_finalized_workers(sg.control, "sg", {})
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_supervisor_recovers_missing_terminal_before_waiting_for_native_or_new_window(sg, monkeypatch):
    finish_receipts(sg, monkeypatch)
    configure_supervisor(monkeypatch, sg)
    observed = []
    original_reconcile = supervisor.reconcile

    def reconcile(source, plan, modules):
        observed.append("recover")
        return original_reconcile(source, plan, modules)

    def idle(*_):
        observed.append("native_observation")
        supervisor.STOP.set()
        return False

    monkeypatch.setattr(supervisor, "reconcile", reconcile)
    monkeypatch.setattr(supervisor, "idle", idle)
    monkeypatch.setattr(supervisor, "new_window", lambda *a: pytest.fail("must not advance before drain"))
    supervisor.supervise("sg", sg.plan, {"sg": sg_module})
    assert observed == ["recover", "native_observation"]
    assert recovery.load(sg.window / "final-report.json")["recovery"]["new_documents"] == 4
    events = [json.loads(line) for line in (sg.control / "events.jsonl").read_text().splitlines()]
    assert any(row["status"] == "interrupted_reconciled" for row in events)
    assert all(row["status"] != "window_complete" for row in events)


def test_supervisor_defers_an_alive_collector_without_new_scope(sg, monkeypatch):
    configure_supervisor(monkeypatch, sg)
    supervisor.STATE["sg"] = {"window": str(sg.window), "child_identity": recovery.identity(os.getpid())}
    monkeypatch.setattr(
        supervisor, "new_window", lambda *a: pytest.fail("live process must not be restarted")
    )
    monkeypatch.setattr(supervisor.STOP, "wait", lambda *_: supervisor.STOP.set())
    supervisor.supervise("sg", sg.plan, {"sg": sg_module})
    events = [json.loads(line) for line in (sg.control / "events.jsonl").read_text().splitlines()]
    assert any(row["status"] == "observing_existing_worker" for row in events)
    assert not (sg.window / "final-report.json").exists()


def test_conflicting_terminal_cannot_be_overwritten_by_old_window(sg, monkeypatch):
    finish_receipts(sg, monkeypatch)
    recovery.write(sg.root / "continuation-report.json", {"controlled_window": {"other_window": True}})
    before = (sg.root / "continuation-report.json").read_bytes()
    with pytest.raises(ValueError, match="another window"):
        recovery.recover_interrupted_window(**sg.arguments)
    assert (sg.root / "continuation-report.json").read_bytes() == before


def test_native_page_growth_is_preserved_when_old_original_is_restaged(sg, monkeypatch):
    finish_receipts(sg, monkeypatch)
    before = recovery.capture_guard(sg.root, sg_module.SOURCE_OWNER)
    original_stage = sg_module.GazetteCollector.stage_originals

    def concurrent_native_write(runner, rows):
        # Native parsing updates only the same document's derived pages while
        # the collector is stopped. The formal stager must read the fresh row.
        with sqlite3.connect(sg.root / "tea.sqlite3") as conn:
            raw = conn.execute("SELECT manifest FROM documents WHERE id=?", (sg.old_id,)).fetchone()[0]
            manifest = json.loads(raw)
            manifest["pages"].append({"page": 2, "status": "native", "text": "New native page"})
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), sg.old_id))
        return original_stage(runner, rows)

    monkeypatch.setattr(sg_module.GazetteCollector, "stage_originals", concurrent_native_write)
    recovery.recover_interrupted_window(**sg.arguments)
    after = recovery.capture_guard(sg.root, sg_module.SOURCE_OWNER)
    recovery.assert_preserved(before, after)
    assert len(after["documents"][sg.old_id]["pages"]) == 2


def test_reviewed_metadata_is_rejected_before_staging(sg, monkeypatch):
    finish_receipts(sg, monkeypatch)
    with sqlite3.connect(sg.root / "tea.sqlite3") as conn:
        raw = conn.execute(
            "SELECT payload FROM document_metadata WHERE document_id=?", (sg.old_id,)
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["review_status"] = "approved"
        conn.execute(
            "UPDATE document_metadata SET payload=? WHERE document_id=?", (json.dumps(payload), sg.old_id)
        )
    before = recovery.capture_guard(sg.root, sg_module.SOURCE_OWNER)
    with pytest.raises(ValueError, match="reviewed source metadata"):
        recovery.recover_interrupted_window(**sg.arguments)
    assert recovery.capture_guard(sg.root, sg_module.SOURCE_OWNER) == before
    assert not (sg.root / "continuation-report.json").exists()


def test_unknown_process_identity_is_not_treated_as_dead(sg, monkeypatch):
    sg.arguments["saved_state"] = {"window": str(sg.window), "child_identity": recovery.identity(os.getpid())}

    def unavailable(_):
        raise recovery.RecoveryDeferred("process identity is not observable")

    monkeypatch.setattr(recovery, "identity", unavailable)
    with pytest.raises(recovery.RecoveryDeferred, match="not observable"):
        recovery.recover_interrupted_window(**sg.arguments)
    assert not (sg.root / "continuation-report.json").exists()


def test_recycled_pid_does_not_inherit_the_old_collectors_identity(sg, monkeypatch):
    finish_receipts(sg, monkeypatch)
    saved = recovery.identity(os.getpid()) | {"start_ticks": "-1"}
    sg.arguments["saved_state"] = {"window": str(sg.window), "child_identity": saved}
    assert recovery.recover_interrupted_window(**sg.arguments)["new_documents"] == 4


def test_recovery_evidence_cannot_be_redirected_outside_the_window(sg):
    outside = sg.data / "outside"
    outside.mkdir()
    (sg.window / "interrupted-recovery").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        recovery.recover_interrupted_window(**sg.arguments)
    assert list(outside.iterdir()) == []
