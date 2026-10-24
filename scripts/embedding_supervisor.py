"""Persistent supervision for the existing frozen embedding runner.

Only the child runner touches corpus data or models. This module schedules the
same verified plan, preserves its journals, and never broadens retry authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

PROTOCOL = "fixed-embedding-supervisor-v1"


def load_json(path):
    return json.loads(path.read_text()) if path.exists() else None


def persist(path, value):
    temporary = path.with_suffix(".pending")
    with temporary.open("w") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def runner_idle(output):
    with (output / "runner.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True


def child_command(runner, arguments, documents, window):
    removed = {
        "--supervisor-window-seconds",
        "--supervisor-max-document-workers",
        "--document-workers",
        "--max-run-seconds",
    }
    kept, skip = [], False
    for item in arguments:
        if skip:
            skip = False
            continue
        flag = item.split("=", 1)[0]
        if flag == "--supervise":
            continue
        if flag in removed:
            skip = "=" not in item
            continue
        kept.append(item)
    return [
        sys.executable,
        str(runner),
        *kept,
        "--document-workers",
        str(documents),
        "--max-run-seconds",
        str(window),
    ]


def retry_delay(output, report, timestamp):
    shared = report.get("shared_admission", {}).get("cooldown_until", 0)
    delay = max(0, shared - timestamp)
    journal = output / "requests.jsonl"
    if journal.exists():
        with journal.open() as stream:
            for line in stream:
                row = json.loads(line)
                if row.get("run_id") == report["run_id"]:
                    delay = max(delay, row.get("retry_after_seconds") or 0)
    return delay


def decide(state, report, *, timestamp, retry_after=0):
    """Pure scheduling decision; no corpus mutation or network call."""
    integrity = report.get("integrity", {})
    required = {
        "selected_originals_and_all_text_unchanged",
        "all_old_vectors_exact_bytes",
        "metadata_classifications_relations_unchanged",
    }
    if (
        report.get("workers_running") is not False
        or not required.issubset(integrity)
        or not all(integrity.values())
    ):
        return {
            "status": "blocked_inputs",
            "reason": "missing_final_integrity_evidence",
            "halt": True,
        }
    if report["missing_rows"] == 0:
        return {
            "status": "completed",
            "reason": "all_fixed_vectors_present",
            "missing_rows": report["missing_rows"],
            "quarantined_inputs": report.get("quarantined_inputs", 0),
            "failed_documents": report.get("failed_documents", 0),
            "next_run_at": 0,
            "halt": True,
        }
    failures = state.get("consecutive_failures", 0)
    documents = state["document_workers"]
    stable = state.get("stable_windows", 0)
    failed = bool(report.get("failure") or report.get("runtime_errors"))
    if failed:
        failures += 1
        stable = 0
        documents = max(1, min(12, documents // 2))
        delay = max(retry_after, min(3600, 60 * 2 ** min(6, failures - 1)))
        reason = report.get("failure") or "local_runtime_error"
    else:
        failures = 0
        stable += int(report.get("checkpoint_cache", {}).get("new_saved_this_process", 0) > 0)
        p95 = report.get("provider", {}).get("latency_p95")
        if stable >= 2 and p95 is not None and p95 <= 40:
            documents = min(state["maximum_document_workers"], documents + 2)
            stable = 0
        delay, reason = 0, "bounded_window_finished"
    blocked = report.get("runnable_documents", 0) == 0
    return {
        "status": "blocked_inputs" if blocked else "cooldown",
        "reason": "remaining_inputs_require_evidence_or_review" if blocked else reason,
        "document_workers": documents,
        "consecutive_failures": failures,
        "stable_windows": stable,
        "next_run_at": timestamp + max(delay, 300 if blocked else 0),
        "missing_rows": report["missing_rows"],
        "quarantined_inputs": report.get("quarantined_inputs", 0),
        "failed_documents": report.get("failed_documents", 0),
        "halt": False,
    }


def supervise(args, runner: Path, arguments, initial_documents, *, inherited_fds=()):
    from app.contexts.knowledge.infrastructure.providers.embedding_admission import process_identity
    from complete_embeddings import digest, document_worker_limit

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    maximum = args.supervisor_max_document_workers or initial_documents
    if not initial_documents <= maximum <= min(32, args.http_workers):
        raise ValueError("Supervisor background ceiling must contain the initial document limit")
    document_worker_limit(args.http_workers, args.request_concurrency, maximum)
    plan_raw = args.plan.read_bytes()
    plan = json.loads(plan_raw)
    binding = {
        "protocol": PROTOCOL,
        "plan_sha256": hashlib.sha256(plan_raw).hexdigest(),
        "owner": plan["owner"],
        "fingerprint": plan["fingerprint"],
        "selection_sha256": hashlib.sha256(args.retry_unresolved_selection.read_bytes()).hexdigest()
        if args.retry_unresolved_selection
        else None,
        "http_ceiling": args.http_workers,
        "maximum_document_workers": maximum,
    }
    cache = output / "successful-inputs.sqlite3"
    if cache.exists():
        with closing(sqlite3.connect(cache.as_uri() + "?mode=ro", uri=True)) as conn:
            actual = json.loads(conn.execute("SELECT payload FROM scope WHERE version=1").fetchone()[0])
        if actual != {
            "owner": plan["owner"],
            "fingerprint": plan["fingerprint"],
            "source_plan": digest(plan),
        }:
            raise ValueError("Supervisor receipt cache does not match the fixed plan")
    state_path = output / "supervisor-state.json"
    with (output / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        state = load_json(state_path)
        if state:
            if any(state.get(key) != value for key, value in binding.items()):
                raise ValueError("Supervisor configuration differs from the persisted fixed plan")
        else:
            previous = load_json(output / "report.json") or {}
            state = {
                **binding,
                "document_workers": initial_documents,
                "next_run_at": 0,
                "last_run_id": previous.get("run_id"),
                "child_pid": None,
                "status": "cooldown",
                "reason": "initial_preflight",
            }
        stopped = threading.Event()
        previous_handlers = {
            sig: signal.signal(sig, lambda *_: stopped.set()) for sig in (signal.SIGTERM, signal.SIGINT)
        }
        child = None
        last_emitted = None

        def save():
            nonlocal last_emitted
            state["supervisor_pid"] = os.getpid()
            state["updated_at"] = time.time()
            persist(state_path, state)
            signature = (
                state["status"],
                state.get("reason"),
                state.get("last_run_id"),
                state.get("document_workers"),
            )
            if signature != last_emitted:
                event = {key: value for key, value in state.items() if key != "child_command"}
                with (output / "supervisor-events.jsonl").open("a") as stream:
                    os.chmod(stream.name, 0o600)
                    stream.write(json.dumps(event) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                print(json.dumps(event), flush=True)
                last_emitted = signature

        try:
            save()
            while True:
                if stopped.is_set():
                    if child and child.poll() is None:
                        child.terminate()  # This supervisor's own child only; drain valid paid responses.
                        while child.poll() is None:
                            time.sleep(0.2)
                    state.update(status="cooldown", reason="operator_stopped", child_pid=None)
                    save()
                    return 130
                # After a supervisor crash its child may still be running. The
                # existing runner lock prevents a second paid worker, even if
                # the parent died in the narrow spawn/state-write gap.
                if child and child.poll() is None:
                    stopped.wait(1)
                    continue
                if not runner_idle(output):
                    state.update(status="running", reason="existing_runner_finishing")
                    if time.time() - state.get("updated_at", 0) >= 20:
                        save()
                    stopped.wait(1)
                    continue
                report = load_json(output / "report.json")
                if report and report.get("run_id") != state.get("last_run_id"):
                    decision = decide(
                        state,
                        report,
                        timestamp=time.time(),
                        retry_after=retry_delay(output, report, time.time()),
                    )
                    state.update(decision, last_run_id=report["run_id"], child_pid=None)
                    save()
                    if decision["halt"]:
                        return 0 if state["status"] == "completed" else 2
                    child = None
                elif child is not None or state.get("child_pid"):
                    # A killed child has no trusted final report. Its persisted
                    # intents will be quarantined by the next normal preflight.
                    exit_code = child.poll() if child is not None else None
                    preflight_failure = exit_code is not None and exit_code >= 0
                    state.update(
                        status="blocked_inputs" if preflight_failure else "cooldown",
                        reason="preflight_or_configuration_failed"
                        if preflight_failure
                        else "child_interrupted",
                        document_workers=max(1, min(12, state["document_workers"] // 2)),
                        next_run_at=time.time() + (300 if preflight_failure else 60),
                        child_pid=None,
                        child_exit_code=exit_code,
                    )
                    child = None
                    save()
                remaining = state.get("next_run_at", 0) - time.time()
                if remaining > 0:
                    stopped.wait(min(1, remaining))
                    continue
                command = child_command(
                    runner,
                    arguments,
                    state["document_workers"],
                    args.supervisor_window_seconds,
                )
                log = output / f"supervised-{time.time_ns()}.log"
                with log.open("w") as stream:
                    os.chmod(log, 0o600)
                    child = subprocess.Popen(
                        command, stdout=stream, stderr=subprocess.STDOUT, pass_fds=inherited_fds
                    )
                state.update(
                    status="running",
                    reason="fixed_plan_active",
                    child_pid=child.pid,
                    child_start_identity=process_identity(child.pid),
                    child_log=str(log),
                    child_command_sha256=hashlib.sha256(json.dumps(command).encode()).hexdigest(),
                )
                save()
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
