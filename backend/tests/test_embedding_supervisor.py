"""Durable scheduling never converts a stopped or uncertain batch to success."""

from contextlib import closing
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from complete_embeddings import consumed_recovery_inputs  # noqa: E402
from embedding_supervisor import child_command, decide, retry_delay  # noqa: E402


def completed_report(**overrides):
    return {
        "run_id": "one",
        "workers_running": False,
        "missing_rows": 10,
        "integrity": {
            "selected_originals_and_all_text_unchanged": True,
            "all_old_vectors_exact_bytes": True,
            "metadata_classifications_relations_unchanged": True,
        },
        "runnable_documents": 5,
        "failure": None,
        "provider": {"latency_p95": 30},
        "checkpoint_cache": {"new_saved_this_process": 20},
        **overrides,
    }


def state(**overrides):
    return {"document_workers": 12, "maximum_document_workers": 16, **overrides}


def test_429_respects_retry_after_and_reduces_background_without_changing_total():
    decision = decide(
        state(http_ceiling=64),
        completed_report(failure="provider_rate_limited"),
        timestamp=100,
        retry_after=9999,
    )
    assert decision["status"] == "cooldown"
    assert decision["next_run_at"] == 10099
    assert decision["document_workers"] == 6
    assert "http_ceiling" not in decision


def test_unknown_and_exhausted_work_is_blocked_not_completed_or_tightly_retried():
    decision = decide(state(), completed_report(runnable_documents=0, quarantined_inputs=10), timestamp=100)
    assert decision["status"] == "blocked_inputs"
    assert decision["next_run_at"] == 400
    assert decision["missing_rows"] == 10


def test_successful_windows_probe_bounded_background_after_two_stable_windows():
    first = decide(state(), completed_report(), timestamp=100)
    assert first["document_workers"] == 12
    second = decide(state(**first), completed_report(), timestamp=200)
    assert second["document_workers"] == 14
    assert (
        decide(state(stable_windows=1), completed_report(provider={"latency_p95": 50}), timestamp=200)[
            "document_workers"
        ]
        == 12
    )


def test_only_zero_missing_with_final_integrity_is_complete():
    assert decide(state(), completed_report(missing_rows=0), timestamp=0)["status"] == "completed"
    assert (
        decide(state(), completed_report(missing_rows=0, integrity={}), timestamp=0)["status"]
        == "blocked_inputs"
    )
    assert decide(state(), completed_report(workers_running=True), timestamp=0)["halt"]


def test_child_command_cannot_recurse_or_keep_stale_background_parameters():
    command = child_command(
        Path("runner.py"),
        [
            "--supervise",
            "--supervisor-window-seconds=300",
            "--supervisor-max-document-workers",
            "16",
            "--document-workers",
            "32",
            "--max-run-seconds=10",
            "--http-workers",
            "64",
            "--apply",
        ],
        12,
        300,
    )
    assert command[2:] == [
        "--http-workers",
        "64",
        "--apply",
        "--document-workers",
        "12",
        "--max-run-seconds",
        "300",
    ]


def test_retry_delay_uses_only_current_run_and_never_shortens_provider_hint(tmp_path):
    records = [
        {"run_id": "other", "retry_after_seconds": 50000},
        {"run_id": "one", "retry_after_seconds": 180},
    ]
    (tmp_path / "requests.jsonl").write_text("\n".join(json.dumps(row) for row in records) + "\n")
    report = completed_report(shared_admission={"cooldown_until": 1300})
    assert retry_delay(tmp_path, report, 1000) == 300


def test_exact_recovery_new_unknown_is_spent_even_after_hard_crash(tmp_path):
    (tmp_path / "preflight-one.json").write_text(
        json.dumps({"run_id": "one", "configuration": {"retry_unresolved_selection_sha256": "authorized"}})
    )
    (tmp_path / "preflight-other.json").write_text(
        json.dumps({"run_id": "other", "configuration": {"retry_unresolved_selection_sha256": "different"}})
    )
    intents = [
        {"request_id": "a", "run_id": "one", "http_started": True, "input_hashes": ["unknown"]},
        {"request_id": "b", "run_id": "one", "http_started": True, "input_hashes": ["rejected"]},
        {"request_id": "c", "run_id": "other", "http_started": True, "input_hashes": ["unrelated"]},
    ]
    (tmp_path / "issued-requests.jsonl").write_text("\n".join(json.dumps(row) for row in intents) + "\n")
    (tmp_path / "requests.jsonl").write_text(json.dumps({**intents[1], "http_status": 429}) + "\n")
    assert consumed_recovery_inputs(tmp_path, "authorized") == {"unknown"}
    # A definitive rejection permits a later safe attempt; no global override.
    assert consumed_recovery_inputs(tmp_path, "missing") == set()


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            pytest.fail("Timed out waiting for durable child state")
        time.sleep(0.02)


def test_supervisor_hard_kill_adopts_child_without_repaying_success(tmp_path):
    # Real SQLite receipt persistence and OS process/lock recovery; the provider
    # writes a paid-input ledger locally, with no external network/model calls.
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"owner": "owner", "fingerprint": "model", "documents": []}))
    models = tmp_path / "models.env"
    models.write_text("")
    fake = tmp_path / "fake_runner.py"
    fake.write_text("""
import fcntl,json,os,time
from pathlib import Path
from app.embedding_reuse import InputCheckpointCache
from complete_embeddings import digest
root=Path(__file__).parent
plan=json.loads((root/'plan.json').read_text())
with (root/'runner.lock').open('a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 cache=InputCheckpointCache(root/'successful-inputs.sqlite3',{'owner':'owner','fingerprint':'model','source_plan':digest(plan)})
 def provider(texts,*,on_batch):
  with (root/'payments.jsonl').open('a') as stream:
   for text in texts:stream.write(json.dumps(text)+'\\n')
   stream.flush();os.fsync(stream.fileno())
  values=[[1.,0.] for _ in texts];on_batch(texts,values);return values
 cache.embed(['alpha'],provider)
 (root/'saved.json').write_text(json.dumps({'child_pid':os.getpid()}))
 deadline=time.time()+20
 while not (root/'continue').exists() and time.time()<deadline:time.sleep(.02)
 cache.embed(['alpha','beta'],provider)
 report={'run_id':'child-one','workers_running':False,'missing_rows':0,'integrity':{'selected_originals_and_all_text_unchanged':True,'all_old_vectors_exact_bytes':True,'metadata_classifications_relations_unchanged':True}}
 (root/'report.json').write_text(json.dumps(report))
""")
    driver = tmp_path / "driver.py"
    driver.write_text("""
from argparse import Namespace
from pathlib import Path
from embedding_supervisor import supervise
root=Path(__file__).parent
args=Namespace(output=root,plan=root/'plan.json',models_env=root/'models.env',retry_unresolved_selection=None,http_workers=64,request_concurrency=2,supervisor_max_document_workers=12,supervisor_window_seconds=30)
raise SystemExit(supervise(args,root/'fake_runner.py',['--apply'],12))
""")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT / "backend"), str(ROOT / "scripts")])}
    first = subprocess.Popen(
        [sys.executable, str(driver)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    second = None
    child_pid = None
    try:
        wait_for(lambda: (tmp_path / "saved.json").exists())
        child_pid = json.loads((tmp_path / "saved.json").read_text())["child_pid"]
        wait_for(
            lambda: json.loads((tmp_path / "supervisor-state.json").read_text()).get("child_pid") == child_pid
        )
        first.kill()
        first.wait(timeout=5)
        second = subprocess.Popen(
            [sys.executable, str(driver)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )
        time.sleep(0.2)
        assert [json.loads(line) for line in (tmp_path / "payments.jsonl").read_text().splitlines()] == [
            "alpha"
        ]
        (tmp_path / "continue").touch()
        second.wait(timeout=10)
        assert second.returncode == 0, second.stderr.read().decode()
        assert [json.loads(line) for line in (tmp_path / "payments.jsonl").read_text().splitlines()] == [
            "alpha",
            "beta",
        ]
        state_after = json.loads((tmp_path / "supervisor-state.json").read_text())
        assert state_after["status"] == "completed"
        with closing(sqlite3.connect(tmp_path / "successful-inputs.sqlite3")) as conn:
            assert conn.execute("SELECT count(*) FROM inputs").fetchone()[0] == 2
    finally:
        (tmp_path / "continue").touch()
        for process in (first, second):
            if process and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
        if child_pid and Path(f"/proc/{child_pid}/cmdline").exists():
            command = Path(f"/proc/{child_pid}/cmdline").read_bytes()
            if str(fake).encode() in command:
                os.kill(child_pid, signal.SIGTERM)
