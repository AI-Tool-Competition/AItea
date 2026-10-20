"""Periodic discovery exercises actual collectors with only local fake HTTP bytes."""

from copy import deepcopy
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
periodic = importlib.import_module("periodic_official_collection")
sources = importlib.import_module("periodic_official_sources")
sg = importlib.import_module("collect_egazette")


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr(periodic, "STOP", threading.Event())
    clock = [1_800_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    root = tmp_path / "schedule"
    config = periodic.initialize(
        root,
        "egazette",
        {
            "categories": ["Revised Acts"],
            "year_from": 2026,
            "year_through": 2026,
        },
        storage_root=tmp_path,
        min_free_bytes=256 * 1024**2,
        page_size=10,
        window_seconds=2,
        max_windows_per_stage=3,
        interval_seconds=60,
        recheck_limit=1,
    )
    state = periodic.load(root / "state.json")
    hits = []
    requests = []
    pdfs = {}
    errors = {}

    def frontend():
        from app.public_search import EGAZETTE_APPLICATION, EGAZETTE_INDEX

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

    def download(url, **kwargs):
        requests.append(url)
        if errors.get(url):
            error = errors[url].pop(0)
            raise error
        if url == sg.EGAZETTE_ORIGIN:
            return {"content": frontend(), "url": url, "mime_type": "text/html"}
        if url == sg.EGAZETTE_SEARCH_URL:
            return {
                "content": json.dumps(
                    {
                        "page": 0,
                        "nbHits": len(hits),
                        "nbPages": 1,
                        "hitsPerPage": 10,
                        "exhaustiveNbHits": True,
                        "hits": hits,
                    }
                ).encode(),
                "url": url,
                "mime_type": "application/json",
            }
        return {"blob": kwargs["consume"]([pdfs[url]]), "url": url}

    monkeypatch.setattr(sg, "_download", download)
    from app.service import Service

    monkeypatch.setattr(
        Service, "__init__", lambda *a, **k: pytest.fail("periodic raw collector created Service")
    )

    def add(number):
        url = f"https://assets.egazette.gov.sg/2026/Legislative%20Supplements/Revised%20Acts/{number}.pdf"
        hits.append(
            {
                "objectID": str(number),
                "title": f"Example {number} Act 2026",
                "notificationNum": str(number),
                "fileUrl": url,
                "category": "Legislative Supplements",
                "subCategory": "Revised Acts",
                "publishYear": 2026,
                "publishTimestamp": 1767225600000,
            }
        )
        pdfs[url] = f"%PDF-1.4\noriginal {number}\n%%EOF".encode()
        return url

    def launch(root, window, state):
        periodic.worker(root, str(window.relative_to(root)))

    def step():
        return periodic.step(root, config, state, launch=launch)

    def cycle():
        assert step() == "running"  # fresh catalogue, frozen selection
        assert step() == "scheduled"
        return periodic.load(root / "cycles" / state["last_cycle"] / "handoff.json")

    return SimpleNamespace(
        root=root,
        config=config,
        state=state,
        clock=clock,
        hits=hits,
        requests=requests,
        pdfs=pdfs,
        errors=errors,
        add=add,
        launch=launch,
        step=step,
        cycle=cycle,
    )


def test_next_period_discovers_new_original_without_fetching_success_again(context):
    c = context
    url1 = c.add(1)
    first = c.cycle()
    first_path = c.root / "cycles" / first["cycle"]
    frozen_bytes = {
        str(path.relative_to(first_path)): path.read_bytes()
        for path in first_path.rglob("*")
        if path.is_file()
    }
    c.clock[0] += 61
    url2 = c.add(2)
    second = c.cycle()
    assert second["active_identities"] == ["egazette:1", "egazette:2"]
    assert c.requests.count(url1) == c.requests.count(url2) == 1
    assert c.requests.count(sg.EGAZETTE_SEARCH_URL) == 2
    selection = periodic.load(c.root / "cycles" / second["cycle"] / "selection.json")
    assert selection["added"] == ["egazette:2"]
    assert [row["identity"] for row in selection["selected"]] == ["egazette:2"]
    assert all((first_path / name).read_bytes() == raw for name, raw in frozen_bytes.items())
    assert not list(c.root.rglob("tea.sqlite3"))
    assert second["model_calls"] == 0 and not second["processing_started"]


def test_retry_after_is_durable_and_successful_sibling_not_repeated(context):
    c = context
    good, failing = c.add(1), c.add(2)
    c.errors[failing] = [sg.DownloadError("HTTP 429", retryable=True, retry_after=600)]
    assert c.step() == "running"
    assert c.step() == "cooldown"
    saved = periodic.load(c.root / "state.json")
    assert saved["next_at"] >= c.clock[0] + 600
    before = list(c.requests)
    assert c.step() == "waiting" and c.requests == before
    c.clock[0] += 601
    assert c.step() == "scheduled"
    assert c.requests.count(good) == 1 and c.requests.count(failing) == 2
    handoff = periodic.load(c.root / "cycles" / c.state["last_cycle"] / "handoff.json")
    assert not handoff["gaps"]
    receipt = handoff["inventory"]["egazette:2"]["acquisition"]["receipt"]
    raw = periodic.load(c.root / "cycles" / handoff["cycle"] / "source" / receipt["path"])
    assert raw["attempt_count"] == 2 and len(raw["previous_attempts"]) == 1


@pytest.mark.parametrize("stage", ["catalogue", "originals"])
@pytest.mark.parametrize("crash_after_handoff", [False, True])
def test_terminal_window_keeps_retry_after_across_new_cycles(
    context, monkeypatch, stage, crash_after_handoff
):
    c = context
    first = c.add(1)
    root = c.root.parent / "last-window-cooldown"
    config = periodic.initialize(
        root,
        "egazette",
        c.config["scope"],
        storage_root=c.root.parent,
        min_free_bytes=256 * 1024**2,
        page_size=10,
        max_windows_per_stage=1,
        interval_seconds=60,
    )
    state = periodic.load(root / "state.json")
    c.errors[sg.EGAZETTE_SEARCH_URL if stage == "catalogue" else first] = [
        sg.DownloadError("HTTP 429", retryable=True, retry_after=3600)
    ]

    def step():
        return periodic.step(root, config, state, launch=c.launch)

    if stage == "originals":
        assert step() == "running"
    if crash_after_handoff:
        with monkeypatch.context() as patch:

            def crash(*args):
                raise RuntimeError("after frozen handoff")

            patch.setattr(periodic, "finish_state", crash)
            with pytest.raises(RuntimeError, match="after frozen handoff"):
                step()
        assert (root / "cycles" / state["active_cycle"] / "handoff.json").exists()
        c.clock[0] += 1
    assert step() == "scheduled"
    handoff = periodic.load(root / "cycles" / state["last_cycle"] / "handoff.json")
    assert state["next_at"] == handoff["source_cooldown_until"] == 1_800_003_600
    before = list(c.requests)
    second = c.add(2)
    c.clock[0] += 61
    state = periodic.load(root / "state.json")
    assert step() == "waiting" and c.requests == before
    c.clock[0] = state["next_at"]
    assert step() == "running"
    assert step() == "scheduled"
    assert c.requests.count(second) == 1


@pytest.mark.parametrize("stage", ["catalogue", "originals"])
def test_lost_last_result_retains_source_cooldown(context, stage):
    c = context
    url = c.add(1)
    root = c.root.parent / "lost-result-cooldown"
    config = periodic.initialize(
        root,
        "egazette",
        c.config["scope"],
        storage_root=c.root.parent,
        min_free_bytes=256 * 1024**2,
        page_size=10,
        max_windows_per_stage=1,
        interval_seconds=60,
    )
    state = periodic.load(root / "state.json")
    c.errors[sg.EGAZETTE_SEARCH_URL if stage == "catalogue" else url] = [
        sg.DownloadError("HTTP 429", retryable=True, retry_after=3600)
    ]
    if stage == "originals":
        assert periodic.step(root, config, state, launch=c.launch) == "running"

    def lost(root, window, state):
        c.launch(root, window, state)
        (window / "result.json").unlink()

    assert periodic.step(root, config, state, launch=lost) == "scheduled"
    assert state["next_at"] == 1_800_003_600
    c.clock[0] += 61
    before = list(c.requests)
    assert periodic.step(root, config, state, launch=c.launch) == "waiting"
    assert c.requests == before


def test_setneg_persisted_source_cooldown_is_read_without_network(tmp_path):
    import sqlite3

    cycle = tmp_path / "cycle"
    output = cycle / "source"
    output.mkdir(parents=True)
    with sqlite3.connect(output / "journal.sqlite3") as conn:
        conn.execute("CREATE TABLE config(key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO config VALUES('cooldown_until', '1800003600')")
    assert sources.persisted_cooldown({"source": "setneg"}, cycle) == 1_800_003_600
    with sqlite3.connect(output / "journal.sqlite3") as conn:
        conn.execute("UPDATE config SET value='nan'")
    with pytest.raises(ValueError, match="cooldown"):
        sources.persisted_cooldown({"source": "setneg"}, cycle)


def test_permanent_gap_not_reset_and_new_discovery_continues(context):
    c = context
    denied = c.add(1)
    c.errors[denied] = [sg.DownloadError("HTTP 403", retryable=False)]
    first = c.cycle()
    assert first["gaps"] == ["egazette:1"]
    c.clock[0] += 61
    new = c.add(2)
    second = c.cycle()
    assert second["gaps"] == ["egazette:1"]
    assert c.requests.count(denied) == c.requests.count(new) == 1
    assert second["inventory"]["egazette:1"] == first["inventory"]["egazette:1"]


def test_same_url_recheck_keeps_both_bytes_and_only_candidate_version(context):
    c = context
    url = c.add(1)
    first = c.cycle()
    c.clock[0] += 7 * 86400 + 1
    second = c.cycle()
    assert second["content_observations"][0]["content_comparison"] == "same_bytes"
    assert second["content_observations"][0]["legal_version_assigned"] is False
    c.clock[0] += 7 * 86400 + 1
    c.pdfs[url] = b"%PDF-1.4\nchanged silently at same URL\n%%EOF"
    third = c.cycle()
    observation = third["content_observations"][0]
    assert observation["content_comparison"] == "changed_bytes_candidate_version"
    for source in (first, second, third):
        periodic.verify_acquisition(c.root, source["inventory"]["egazette:1"]["acquisition"])
    assert c.requests.count(url) == 3


def test_recheck_rotates_oldest_success_and_caps_each_cycle(context):
    c = context
    urls = [c.add(number) for number in (1, 2, 3)]
    c.cycle()
    c.clock[0] += 7 * 86400 + 1
    for number in (1, 2, 3):
        result = c.cycle()
        assert len(result["content_observations"]) == 1
        assert result["content_observations"][0]["identity"] == f"egazette:{number}"
        c.clock[0] += 61
    assert [c.requests.count(url) for url in urls] == [2, 2, 2]


def test_failed_recheck_keeps_success_and_does_not_reset_failure(context):
    c = context
    url = c.add(1)
    old = c.cycle()["inventory"]["egazette:1"]["acquisition"]
    c.clock[0] += 7 * 86400 + 1
    c.errors[url] = [sg.DownloadError("HTTP 404", retryable=False)]
    failed = c.cycle()
    assert failed["recheck_gaps"] == ["egazette:1"]
    assert failed["inventory"]["egazette:1"]["acquisition"] == old
    c.clock[0] += 7 * 86400 + 1
    c.cycle()
    assert c.requests.count(url) == 2


def test_catalogue_removal_is_not_legal_repeal_and_reappearance_reuses(context):
    c = context
    url = c.add(1)
    first = c.cycle()
    saved = c.hits.pop()
    # The real parser requires a genuine terminal empty catalogue response.
    c.clock[0] += 61
    c.add(2)
    second = c.cycle()
    plan = periodic.load(c.root / "cycles" / second["cycle"] / "selection.json")
    assert plan["removed_from_directory"] == ["egazette:1"] and not plan["legal_effect_inferred"]
    assert second["inventory"]["egazette:1"] == first["inventory"]["egazette:1"]
    c.hits.append(saved)
    c.clock[0] += 61
    c.cycle()
    assert c.requests.count(url) == 1


def test_lost_final_report_resumes_durable_success_without_http(context):
    c = context
    url = c.add(1)
    c.step()

    class Crash(Exception):
        pass

    def lost(root, window, state):
        cycle = window.parent.parent
        sources.collect(c.config, cycle, "originals", periodic.load(cycle / "selection.json")["selected"])
        raise Crash

    with pytest.raises(Crash):
        periodic.step(c.root, c.config, c.state, launch=lost)
    assert c.requests.count(url) == 1
    assert c.step() == "cooldown"
    c.clock[0] = c.state["next_at"] + 1
    assert c.step() == "scheduled"
    assert c.requests.count(url) == 1


def test_last_crashed_window_reconciles_saved_originals_offline(context, monkeypatch):
    c = context
    c.add(1)
    c.step()
    cycle = c.root / "cycles" / c.state["active_cycle"]
    result = sources.collect(
        c.config, cycle, "originals", periodic.load(cycle / "selection.json")["selected"]
    )
    assert result["complete"]
    for number in range(2, 5):
        window = cycle / "windows" / f"window-{number:06d}"
        window.mkdir()
        periodic.write(
            window / "plan.json",
            {
                "cycle": cycle.name,
                "stage": "originals",
                "config_sha256": c.state["config_sha256"],
                "selection_sha256": periodic.digest(cycle / "selection.json"),
            },
        )
        if number < 4:
            periodic.write(window / "reconciled.json", {})
    before = list(c.requests)
    with monkeypatch.context() as patch:

        def crash_before_finalize(*args, **kwargs):
            raise RuntimeError("lost process after offline report")

        patch.setattr(periodic, "finalize", crash_before_finalize)
        with pytest.raises(RuntimeError, match="after offline report"):
            c.step()
    saved = (window / "offline-reconciliation.json").read_bytes()
    assert c.step() == "scheduled"
    assert (window / "offline-reconciliation.json").read_bytes() == saved
    assert c.requests == before
    handoff = periodic.load(cycle / "handoff.json")
    assert not handoff["gaps"]
    assert periodic.load(window / "offline-reconciliation.json")["network_calls"] == 0


def test_success_corruption_and_selection_tampering_fail_closed(context):
    c = context
    c.add(1)
    first = c.cycle()
    acquisition = first["inventory"]["egazette:1"]["acquisition"]
    original = c.root / "cycles" / first["cycle"] / "source" / acquisition["assets"][0]["path"]
    original.chmod(0o600)
    original.write_bytes(b"tampered")
    c.clock[0] += 61
    with pytest.raises(ValueError, match="previous successful original changed"):
        c.step()


def test_stop_before_window_sends_nothing(context):
    c = context
    (c.root / "STOP").touch()
    assert c.step() == "stopped" and not c.requests
    assert not list((c.root / "cycles").iterdir())


def test_stop_during_window_drains_then_does_not_start_next(context):
    c = context
    c.add(1)

    def stop_after_launch(root, window, state):
        (root / "STOP").touch()
        c.launch(root, window, state)

    assert periodic.step(c.root, c.config, c.state, launch=stop_after_launch) == "running"
    before = list(c.requests)
    assert c.step() == "stopped" and c.requests == before
    assert periodic.load(c.root / "cycles" / c.state["active_cycle"] / "catalogue.json")["records"]


def test_model_configuration_cannot_leak_into_child(monkeypatch):
    for prefix in ("LLM", "VISION", "EMBEDDING", "RERANK"):
        for field in ("BASE_URL", "API_KEY", "MODEL"):
            monkeypatch.setenv(f"{prefix}_{field}", "never-copy")
    env = periodic.model_free_env()
    assert all(
        env[f"{p}_{f}"] == ""
        for p in ("LLM", "VISION", "EMBEDDING", "RERANK")
        for f in ("BASE_URL", "API_KEY", "MODEL")
    )
    assert env["VISION_OCR_ENABLED"] == "false"


def test_frozen_recheck_threshold_and_prior_handoff_survive_restart(context):
    c = context
    c.add(1)
    first = c.cycle()
    c.clock[0] += 7 * 86400 - 1
    assert c.step() == "running"
    cycle = c.root / "cycles" / c.state["active_cycle"]
    window = cycle / "windows/window-000001"
    # Simulate death after selection persistence but before reconciling window.
    (window / "reconciled.json").unlink()
    original = (cycle / "selection.json").read_bytes()
    c.clock[0] += 2
    assert c.step() == "running"
    assert (cycle / "selection.json").read_bytes() == original
    assert not periodic.load(cycle / "selection.json")["recheck"]
    previous = c.root / "cycles" / first["cycle"] / "handoff.json"
    raw = periodic.load(previous)
    raw["inventory"] = {}
    periodic.write(previous, raw)
    with pytest.raises(ValueError, match="previous handoff changed"):
        periodic.previous_handoff(c.root, cycle)


@pytest.mark.parametrize("change", ["config", "code", "symlink", "duplicate"])
def test_config_scope_and_catalogue_boundaries(context, monkeypatch, change):
    c = context
    if change == "config":
        raw = periodic.load(c.root / "config.json")
        raw["scope"]["year_from"] = 2025
        periodic.write(c.root / "config.json", raw)
        with pytest.raises(ValueError, match="configuration changed"):
            periodic.configuration(c.root)
    elif change == "code":
        monkeypatch.setattr(periodic, "bindings", lambda: {})
        with pytest.raises(ValueError, match="implementation changed"):
            periodic.configuration(c.root)
    elif change == "symlink":
        link = c.root.parent / "link"
        link.symlink_to(c.root)
        with pytest.raises(ValueError, match="symbolic"):
            periodic.configuration(link)
    else:
        row = sources.record("x", {"title": "x"}, {})
        with pytest.raises(ValueError, match="duplicate"):
            periodic.checked_records([row, deepcopy(row)])


def test_concurrent_supervisor_is_rejected_before_network(context):
    c = context
    script = """import sys
sys.path.insert(0, sys.argv[1])
from official_source_supervisor_recovery import lock
from pathlib import Path
with lock(Path(sys.argv[2]) / 'supervisor.lock'):
 print('ready', flush=True)
 sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(ROOT / "scripts"), str(c.root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "ready"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/periodic_official_collection.py"),
                "run",
                "--schedule-dir",
                str(c.root),
                "--once",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=periodic.model_free_env(),
        )
        assert result.returncode != 0 and "RecoveryDeferred" in result.stderr
        assert not list((c.root / "cycles").iterdir())
    finally:
        process.communicate("stop\n", timeout=10)


def test_parent_death_before_pid_record_keeps_inherited_child_lock(tmp_path):
    window = tmp_path / "window"
    window.mkdir()
    gate = tmp_path / "release"
    script = r"""import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import official_source_supervisor_recovery as r
old=r.write
def write(path,value,**kwargs):
 if value.get('phase') == 'started': os._exit(17)
 return old(path,value,**kwargs)
r.write=write
child="import time,sys;from pathlib import Path;p=Path(sys.argv[1]);p.with_suffix('.ready').write_text('ready');\nwhile not p.exists(): time.sleep(.05)"
with open(Path(sys.argv[2])/'log','w') as log:
 r.spawn_owned([sys.executable,'-c',child,sys.argv[3]],window=Path(sys.argv[2]),kind='collector',cwd=Path.cwd(),env=os.environ.copy(),stdout=log)
"""
    parent = subprocess.run(
        [sys.executable, "-c", script, str(ROOT / "scripts"), str(window), str(gate)], timeout=10, check=False
    )
    assert parent.returncode == 17
    try:
        deadline = time.monotonic() + 5
        while not gate.with_suffix(".ready").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert gate.with_suffix(".ready").exists()
        assert periodic.load(window / "collector-process.json")["identity"] is None
        with pytest.raises(periodic.RecoveryDeferred):
            with periodic.available_window(window):
                pass
    finally:
        gate.touch()
    deadline = time.monotonic() + 5
    while True:
        try:
            with periodic.available_window(window):
                break
        except periodic.RecoveryDeferred:
            assert time.monotonic() < deadline
            time.sleep(0.02)


def test_setneg_real_journal_only_selected_types_and_frozen_candidate_closure(tmp_path, monkeypatch):
    ind = importlib.import_module("collect_indonesia_official")
    verifier = sys.modules[ind.SourceVerifier.__module__]
    detail = json.loads((ROOT / "backend/tests/fixtures/discovery/setneg-detail.json").read_text())
    rows = [detail["row"][0]]
    requested = []

    def download(url, *, consume, **options):
        requested.append(url)
        if "produkhukum?" in url:
            assert options["json_body"]["jns"] == ["UU"]
            raw = json.dumps({"jml": len(rows), "jn": ["UU"], "data": rows}).encode()
        elif "detaildata?" in url:
            raw = json.dumps(detail).encode()
        else:
            assert "/pdf?" in url
            raw = b"%PDF-1.4\nvalidated raw original\n%%EOF"
        return {
            "url": url,
            "filename": "fixture",
            "mime_type": "application/pdf" if raw.startswith(b"%PDF") else "application/json",
            "response_status": 200,
            "blob": consume([raw]),
        }

    monkeypatch.setattr(verifier, "_download", download)
    root = tmp_path / "schedule-id"
    config = periodic.initialize(
        root,
        "setneg",
        {"types": ["UU"]},
        storage_root=tmp_path,
        min_free_bytes=256 * 1024**2,
        window_seconds=5,
    )
    state = periodic.load(root / "state.json")
    cycle = periodic.new_cycle(root, state)
    catalogue = sources.collect(config, cycle, "catalogue", [])
    assert catalogue["complete"] and len(catalogue["records"]) == 1
    assert all("produkhukum?" in url for url in requested)
    first = sources.collect(config, cycle, "originals", catalogue["records"])
    assert first["complete"]
    before = list(requested)
    assert sources.collect(config, cycle, "originals", catalogue["records"])["complete"]
    assert requested == before
    recovered = sources.inspect_originals(config, cycle, catalogue["records"])
    assert recovered["complete"] and recovered["network_calls"] == 0
    assert requested == before
    assert not list(root.rglob("tea.sqlite3"))


def test_low_storage_does_not_create_network_window(context, monkeypatch):
    c = context
    monkeypatch.setattr(periodic.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    assert c.step() == "waiting"
    assert c.state["status"] == "storage_wait"
    cycle = c.root / "cycles" / c.state["active_cycle"]
    assert not list((cycle / "windows").iterdir()) and not c.requests


def test_setneg_retry_budget_and_403_are_not_reset():
    config = {"max_original_attempts": 3}
    transient = {"status": "failed", "attempts": 2, "result": json.dumps({"retryable": True})}
    assert sources._id_eligible(transient, config)
    assert not sources._id_eligible({**transient, "attempts": 3}, config)
    assert not sources._id_eligible(
        {**transient, "result": json.dumps({"retryable": True, "error": "HTTP 403"})}, config
    )


def test_first_get_intent_survives_real_child_exit_before_receipt(context):
    c = context
    c.add(1)
    root = c.root.parent / "single-attempt"
    config = periodic.initialize(
        root,
        "egazette",
        c.config["scope"],
        storage_root=c.root.parent,
        min_free_bytes=256 * 1024**2,
        page_size=10,
        max_original_attempts=1,
    )
    cycle = periodic.new_cycle(root, periodic.load(root / "state.json"))
    catalogue = sources.collect(config, cycle, "catalogue", [])
    original = sg._download
    calls = c.root.parent / "raw-get-calls"

    def crash_after_body(url, **kwargs):
        response = original(url, **kwargs)
        if "assets.egazette.gov.sg" in url:
            with calls.open("a") as output:
                output.write(url + "\n")
                output.flush()
                os.fsync(output.fileno())
            os._exit(17)
        return response

    def child():
        sg._download = crash_after_body
        sources.collect(config, cycle, "originals", catalogue["records"])

    process = multiprocessing.get_context("fork").Process(target=child)
    process.start()
    process.join(timeout=10)
    assert process.exitcode == 17
    assert len(calls.read_text().splitlines()) == 1
    assert not list((cycle / "source/originals").glob("*.json"))
    before = list(c.requests)
    result = sources.collect(config, cycle, "originals", catalogue["records"])
    assert not result["complete"] and c.requests == before
    assert result["transfer_budget"]["requests"] == result["transfer_budget"]["unknown_requests"] == 1
    assert result["transfer_budget"]["charged_bytes"] == 32 * 1024**2


def test_title_only_change_does_not_reset_terminal_url_failure(context):
    c = context
    url = c.add(1)
    c.errors[url] = [sg.DownloadError("HTTP 403", retryable=False)]
    c.cycle()
    c.hits[0]["title"] = "Corrected title for the same official original"
    c.clock[0] += 61
    result = c.cycle()
    assert c.requests.count(url) == 1
    assert result["gaps"] == ["egazette:1"]


def test_total_budget_and_unknown_reservations_persist_across_cycles(context):
    from periodic_official_budget import BudgetBlocked, reserve, summary

    c = context
    config = {**c.config, "original_byte_budget": 100, "total_original_byte_budget": 150}
    reserve(c.root, config, "cycle-a", "https://assets.egazette.gov.sg/one.pdf", 100)
    _, allowed = reserve(c.root, config, "cycle-b", "https://assets.egazette.gov.sg/two.pdf", 100)
    assert allowed == 50
    with pytest.raises(BudgetBlocked, match="byte budget exhausted"):
        reserve(c.root, config, "cycle-c", "https://assets.egazette.gov.sg/three.pdf", 100)
    assert summary(c.root)["charged_bytes"] == 150
    assert summary(c.root)["unknown_requests"] == 2


def test_deleted_budget_cannot_silently_reset_counters(context):
    c = context
    (c.root / "transfers.sqlite3").unlink()
    with pytest.raises(ValueError, match="budget is missing"):
        c.step()
    assert not c.requests and not (c.root / "transfers.sqlite3").exists()


def test_concurrent_original_intents_share_total_reservation(context):
    from concurrent.futures import ThreadPoolExecutor
    from periodic_official_budget import reserve, summary

    c = context
    config = {**c.config, "original_byte_budget": 150, "total_original_byte_budget": 150}
    barrier = threading.Barrier(2)

    def reserve_one(number):
        barrier.wait(timeout=5)
        return reserve(c.root, config, "cycle-a", f"https://assets.egazette.gov.sg/{number}.pdf", 100)[1]

    with ThreadPoolExecutor(max_workers=2) as pool:
        allowed = list(pool.map(reserve_one, [1, 2]))
    assert sorted(allowed) == [50, 100] and summary(c.root)["charged_bytes"] == 150


def test_egazette_explicit_single_host_limit_is_actual(context, monkeypatch):
    c = context
    c.add(1)
    root = c.root.parent / "single-connection"
    config = periodic.initialize(
        root,
        "egazette",
        {
            **c.config["scope"],
            "categories": ["Revised Acts", "Acts Supplement"],
        },
        storage_root=c.root.parent,
        min_free_bytes=256 * 1024**2,
        page_size=10,
        workers=1,
        per_host=1,
    )
    cycle = periodic.new_cycle(root, periodic.load(root / "state.json"))
    original = sg._download
    active, peak = 0, 0
    mutex = threading.Lock()

    def download(url, **options):
        nonlocal active, peak
        if url != sg.EGAZETTE_SEARCH_URL:
            return original(url, **options)
        with options["connection_slot"](sg.EGAZETTE_SEARCH_HOST):
            with mutex:
                active += 1
                peak = max(active, peak)
            try:
                time.sleep(0.03)
                category = (
                    "Acts Supplement"
                    if 'subCategory:"Acts Supplement"' in options["json_body"]["filters"]
                    else "Revised Acts"
                )
                row = {
                    **c.hits[0],
                    "objectID": category,
                    "subCategory": category,
                    "fileUrl": "https://assets.egazette.gov.sg/2026/Legislative%20Supplements/"
                    + category.replace(" ", "%20")
                    + "/1.pdf",
                }
                raw = {
                    "page": 0,
                    "nbHits": 1,
                    "nbPages": 1,
                    "hitsPerPage": 10,
                    "exhaustiveNbHits": True,
                    "hits": [row],
                }
                return {"content": json.dumps(raw).encode(), "url": url, "mime_type": "application/json"}
            finally:
                with mutex:
                    active -= 1

    monkeypatch.setattr(sg, "_download", download)
    result = sources.collect(config, cycle, "catalogue", [])
    assert result["complete"] and len(result["records"]) == 2 and peak == 1


@pytest.mark.parametrize(
    "source,scope",
    [
        ("setneg", {"types": ["UU", "UU"]}),
        (
            "egazette",
            {"categories": ["Revised Acts", "Revised Acts"], "year_from": 2026, "year_through": 2026},
        ),
    ],
)
def test_duplicate_scope_rejected_before_creating_schedule(tmp_path, source, scope):
    root = tmp_path / "duplicate"
    with pytest.raises(ValueError, match="scope"):
        periodic.initialize(root, source, scope, storage_root=tmp_path)
    assert not root.exists()
