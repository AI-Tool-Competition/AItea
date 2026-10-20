"""Supervise the existing fixed OCR runner without resetting work or paid intents."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

from embedding_supervisor import load_json, persist

PROTOCOL = "fixed-parse-supervisor-v1"
TERMINAL = {
    "completed",
    "blocked_unknown",
    "blocked_quality",
    "blocked_inputs",
    "failed_configuration",
}


def acquire_parse_lock(snapshot, inherited_fd=None):
    """The child inherits the already-held open description, including at spawn."""
    path = snapshot / ".parse-owner.lock"
    handle = path.open("a") if inherited_fd is None else os.fdopen(inherited_fd, "a")
    try:
        actual, expected = os.fstat(handle.fileno()), path.stat()
        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise ValueError("Inherited parse lock does not belong to this snapshot")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


def binding_for(args, runner, *, prefix=None):
    from process_frozen_parse import digest, frozen_inputs, selected_documents

    report, initial = frozen_inputs(args.snapshot, args.data_dir)
    selected = selected_documents(args, initial)
    binding = {
        "protocol": PROTOCOL,
        "data_dir": str(args.data_dir.resolve(strict=True)),
        "snapshot": str(args.snapshot.resolve(strict=True)),
        "owner_id": report["owner_id"],
        "selected_ids_sha256": digest(selected),
        "selected_documents": len(selected),
        "snapshot_sha256": {
            name: hashlib.sha256((args.snapshot / name).read_bytes()).hexdigest()
            for name in (
                "initial-report.json",
                "initial-parse-ids.json",
                "initial-parse-documents.json",
            )
        },
        "models_file_sha256": hashlib.sha256(args.models_env.read_bytes()).hexdigest(),
        "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
        "supervisor_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "run_prefix": prefix or args.run_id,
        **{
            key: getattr(args, key)
            for key in (
                "ocr_workers",
                "ocr_start_workers",
                "document_workers",
                "seconds",
                "claim_limit",
            )
        },
    }
    return binding, initial, selected


def verify_child_binding(args, runner):
    binding = load_json(args.snapshot / "supervisor-binding.json")
    state = load_json(args.snapshot / "supervisor-state.json") or {}
    if not binding or binding_for(args, runner, prefix=binding["run_prefix"])[0] != binding:
        raise ValueError("Supervised child configuration differs from its frozen binding")
    if state.get("active_run_id") != args.run_id:
        raise ValueError("Supervised child has no matching durable launch intent")
    return load_success_guard(args.snapshot, binding)


def load_success_guard(snapshot, binding):
    from process_frozen_parse import digest

    guard = load_json(snapshot / "supervisor-success-pages.json")
    if not guard or guard.get("binding_sha256") != digest(binding):
        raise ValueError("Cross-window successful-page guard missing or mismatched")
    return guard["documents"]


def classify_document(row, manifest, timestamp):
    """Only already-authorized queued/expired parse work can be resumed."""
    from app.collection_recovery import classify_processing
    from app.ingestion_quality import pdf_page_status, text_complete

    decision = classify_processing(manifest, phase="parse", retryable=True)
    if decision.reason == "paid_result_unknown":
        return {"status": "blocked_unknown", "reason": decision.reason}
    if decision.reason == "invalid_checkpoint":
        raise ValueError("Frozen document has an invalid parse checkpoint")
    if text_complete(manifest):
        return {"status": "completed", "reason": "accepted_text_present"}
    if decision.reason == "quality_review" or any(
        pdf_page_status(page) in {"ocr_uncertain", "source_incomplete"} for page in manifest.get("pages", [])
    ):
        return {
            "status": "blocked_quality",
            "reason": "saved_text_requires_quality_review",
        }
    if not decision.retryable:
        return {"status": "blocked_inputs", "reason": decision.reason}
    if row["phase"] != "parse" or row["job_status"] not in {"queued", "processing"}:
        return {
            "status": "blocked_inputs",
            "reason": "parse_job_requires_explicit_recovery",
        }
    due = row["available_at"] if row["job_status"] == "queued" else row["lease_until"]
    if due is None:
        return {"status": "blocked_inputs", "reason": "parse_lease_has_no_expiry"}
    due = max(due, decision.not_before)
    for page in manifest.get("pages", []):
        retry_at = (page.get("ocr") or {}).get("retry_not_before", 0)
        if type(retry_at) in {int, float} and math.isfinite(retry_at):
            due = max(due, retry_at)
    return {
        "status": "ready" if due <= timestamp else "waiting",
        "reason": "parse_job_due" if due <= timestamp else "parse_job_cooldown_or_live_lease",
        "due_at": due,
    }


def inspect_scope(args, initial, selected, success_guard):
    from app.ingestion_quality import pdf_page_status
    from process_frozen_parse import digest, inspect_frozen_document

    observed, accepted = {}, {}
    for identifier in selected:
        row, manifest = inspect_frozen_document(
            args.data_dir,
            initial,
            identifier,
            successful_pages=success_guard.get(identifier, ()),
        )
        with Path(row["storage_path"]).open("rb") as source:
            if (
                os.fstat(source.fileno()).st_size != row["size"]
                or hashlib.file_digest(source, "sha256").hexdigest() != row["content_hash"]
            ):
                raise ValueError("Frozen original bytes changed: " + identifier)
        observed[identifier] = classify_document(row, manifest, time.time())
        accepted[identifier] = [
            {
                "page": page["page"],
                "validated_status": pdf_page_status(page),
                "sha256": digest(page),
            }
            for page in manifest.get("pages", [])
            if pdf_page_status(page) in {"native", "ocr_complete"}
        ]
    return observed, accepted


def decide(observed, timestamp, *, cooldown_until=0):
    counts = {
        status: sum(item["status"] == status for item in observed.values())
        for status in (
            "completed",
            "ready",
            "waiting",
            "blocked_unknown",
            "blocked_quality",
            "blocked_inputs",
        )
    }
    result = {
        "counts": counts,
        "next_run_at": 0,
        "held_documents": {
            key: value for key, value in observed.items() if value["status"].startswith("blocked_")
        },
    }
    if counts["completed"] == len(observed):
        return {**result, "status": "completed", "reason": "all_fixed_text_complete"}
    due = [item["due_at"] for item in observed.values() if item["status"] in {"ready", "waiting"}]
    if due:
        next_at = max(min(due), cooldown_until)
        return {
            **result,
            "status": "ready" if next_at <= timestamp else "cooldown",
            "reason": "fixed_parse_work_pending",
            "next_run_at": max(0, next_at),
        }
    status = next(key for key in ("blocked_unknown", "blocked_quality", "blocked_inputs") if counts[key])
    return {
        **result,
        "status": status,
        "reason": "remaining_documents_require_evidence_or_review",
    }


def child_command(args, runner, run_id, lock_fd):
    command = [sys.executable, str(runner)]
    for option in (
        "data_dir",
        "snapshot",
        "models_env",
        "document_ids",
        "ocr_workers",
        "ocr_start_workers",
        "document_workers",
        "seconds",
        "claim_limit",
    ):
        value = getattr(args, option)
        if value is not None:
            command.extend(("--" + option.replace("_", "-"), str(value)))
    return [*command, "--run-id", run_id, "--supervisor-lock-fd", str(lock_fd)]


def completed_window(args, binding, selected, run_id):
    """Absence of a terminal report never authorizes another potentially paid window."""
    output = args.snapshot / "runs" / run_id
    report = load_json(output / "report.json")
    entry = load_json(output / "run-input.json") or {}
    if report is None:
        failure = load_json(output / "startup-error.json") or {}
        if failure == {
            "run_id": run_id,
            "reason": "invalid_ocr_configuration",
            "requests_started": False,
        }:
            return {
                "status": "failed_configuration",
                "reason": "invalid_ocr_configuration",
            }
        return {
            "status": "blocked_unknown",
            "reason": "missing_terminal_window_evidence",
        }
    if (
        entry.get("run_id") != run_id
        or entry.get("selected_ids") != selected
        or entry.get("owner_id") != binding["owner_id"]
        or entry.get("models_file_sha256") != binding["models_file_sha256"]
        or report.get("run_id") != run_id
        or report.get("workers_running") is not False
        or report.get("selected_ids_sha256") != binding["selected_ids_sha256"]
        or report.get("initial_source_and_metadata_guards_preserved") is not True
        or report.get("initial_successful_pages_unchanged") is not True
        or report.get("embedding_requests") != 0
        or report.get("download_requests") != 0
    ):
        return {
            "status": "blocked_unknown",
            "reason": "unverified_terminal_window_evidence",
        }
    blockers = report.get("blockers", [])
    if blockers and any(item.get("error_type") != "OperationalError" for item in blockers):
        return {
            "status": "failed_configuration",
            "reason": "bounded_runner_guard_failed",
        }
    return {"status": "drained", "reason": "bounded_window_finished", "report": report}


def supervise(args, runner):
    from app.embedding_admission import process_identity
    from process_frozen_parse import digest

    with (args.snapshot / ".parse-supervisor.lock").open("a") as supervisor_lock:
        try:
            fcntl.flock(supervisor_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("A supervisor already owns this frozen OCR scope", file=sys.stderr)
            return 2
        state_path = args.snapshot / "supervisor-state.json"
        try:
            present = [
                (args.snapshot / name).exists()
                for name in (
                    "supervisor-state.json",
                    "supervisor-binding.json",
                    "supervisor-success-pages.json",
                )
            ]
            if any(present) and not all(present):
                raise ValueError("Supervisor ledger is incomplete")
            state = load_json(state_path)
            if state_path.exists() and (
                not isinstance(state, dict)
                or type(state.get("generation")) is not int
                or state["generation"] < 0
                or "active_run_id" not in state
                or state["active_run_id"] is not None
                and not isinstance(state["active_run_id"], str)
            ):
                raise ValueError("Invalid supervisor state")
        except (ValueError, OSError):
            # Preserve a damaged launch ledger verbatim: it may describe a child
            # that is still making requests. Never synthesize an empty replacement.
            persist(
                args.snapshot / "supervisor-startup-error.json",
                {
                    "status": "failed_configuration",
                    "reason": "supervisor_state_unreadable",
                    "updated_at": time.time(),
                },
            )
            return 2
        if state is None:
            state = {"generation": 0, "active_run_id": None}
        stop = threading.Event()
        previous = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
        child, owner_lock = None, None
        last_event = None

        def save(**updates):
            nonlocal last_event
            if state_path.exists() and all(state.get(key) == value for key, value in updates.items()):
                return
            state.update(updates, updated_at=time.time(), supervisor_pid=os.getpid())
            persist(state_path, state)
            signature = (
                state.get("status"),
                state.get("reason"),
                state.get("active_run_id"),
            )
            if signature != last_event:
                with (args.snapshot / "supervisor-events.jsonl").open("a") as stream:
                    stream.write(json.dumps(state, ensure_ascii=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                print(json.dumps(state, ensure_ascii=False), flush=True)
                last_event = signature

        try:
            binding, initial, selected = binding_for(args, runner)
            binding_path = args.snapshot / "supervisor-binding.json"
            existing = load_json(binding_path)
            if existing is None:
                if state_path.exists():
                    raise ValueError("Existing supervisor lost its fixed binding")
                persist(binding_path, binding)
                persist(
                    args.snapshot / "supervisor-success-pages.json",
                    {
                        "binding_sha256": digest(binding),
                        "documents": {},
                    },
                )
            elif existing != binding:
                raise ValueError("Supervisor configuration differs from its fixed binding")
            success_guard = load_success_guard(args.snapshot, binding)
            # Hold unknown outcomes across restarts, even if a concurrent edit made
            # the remaining queue look runnable. Explicit evidence reconciliation
            # must repair the exact terminal report; deleting state is not recovery.
            while True:
                if child is not None and child.poll() is not None:
                    state["child_exit_code"] = child.returncode
                    child = None
                try:
                    owner_lock = acquire_parse_lock(args.snapshot)
                except BlockingIOError:
                    save(
                        status="draining" if stop.is_set() else "waiting_child",
                        reason="parse_owner_lock_held",
                    )
                    if stop.is_set():
                        pid, identity = (
                            state.get("child_pid"),
                            state.get("child_identity"),
                        )
                        if pid and identity and process_identity(pid) == identity:
                            try:
                                os.kill(pid, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                    stop.wait(0.2) if not stop.is_set() else time.sleep(0.2)
                    continue
                # A subprocess can briefly close its last descriptor before exiting.
                # Do not start the next process until the one we spawned is reaped.
                if child is not None:
                    owner_lock.close()
                    owner_lock = None
                    time.sleep(0.05)
                    continue
                if binding_for(args, runner)[0] != binding:
                    raise ValueError("Frozen supervisor inputs changed between windows")
                terminal = None
                if state.get("active_run_id"):
                    terminal = completed_window(args, binding, selected, state["active_run_id"])
                    if terminal["status"] != "drained":
                        save(**terminal)
                        return 2
                try:
                    observed, accepted = inspect_scope(args, initial, selected, success_guard)
                except sqlite3.OperationalError as exc:
                    from app.collection import _storage_busy

                    if not _storage_busy(exc):
                        raise
                    failures = state.get("local_read_failures", 0) + 1
                    delay = min(3600, 60 * 2 ** min(6, failures - 1))
                    save(
                        status="cooldown",
                        reason="local_database_busy",
                        local_read_failures=failures,
                        next_run_at=time.time() + delay,
                    )
                    owner_lock.close()
                    owner_lock = None
                    if stop.wait(delay):
                        save(status="stopped", reason="operator_requested_drain")
                        return 0
                    continue
                state["local_read_failures"] = 0
                persist(
                    args.snapshot / "supervisor-success-pages.json",
                    {
                        "binding_sha256": digest(binding),
                        "documents": accepted,
                    },
                )
                success_guard = accepted
                if terminal:
                    report = terminal["report"]
                    failures = state.get("consecutive_failures", 0) + 1 if report.get("blockers") else 0
                    # Empty windows caused by local capacity contention must not spin.
                    backoff = (
                        min(3600, 60 * 2 ** min(6, failures - 1))
                        if failures
                        else (30 if not report.get("claims") else 0)
                    )
                    save(
                        last_run_id=state["active_run_id"],
                        active_run_id=None,
                        child_pid=None,
                        child_identity=None,
                        consecutive_failures=failures,
                        backoff_until=time.time() + backoff,
                    )
                cooldown = load_json(args.snapshot / "provider-cooldown.json") or {}
                decision = decide(
                    observed,
                    time.time(),
                    cooldown_until=max(
                        cooldown.get("retry_not_before", 0),
                        state.get("backoff_until", 0),
                    ),
                )
                save(**decision)
                if decision["status"] in TERMINAL:
                    return 0 if decision["status"] == "completed" else 2
                if stop.is_set():
                    save(status="stopped", reason="operator_requested_drain")
                    return 0
                if decision["status"] == "cooldown":
                    owner_lock.close()
                    owner_lock = None
                    stop.wait(max(0, decision["next_run_at"] - time.time()))
                    continue
                generation = state["generation"] + 1
                run_id = f"{args.run_id}-{generation:06d}-{uuid4().hex[:12]}"
                # Persist before spawn and pass the owner lock across exec. A killed
                # parent cannot leave an untracked child able to overlap its restart.
                save(
                    status="launching",
                    reason="fixed_window_authorized",
                    generation=generation,
                    active_run_id=run_id,
                    child_pid=None,
                    child_identity=None,
                )
                try:
                    child = subprocess.Popen(
                        child_command(args, runner, run_id, owner_lock.fileno()),
                        pass_fds=(owner_lock.fileno(),),
                        start_new_session=True,
                    )
                except OSError:
                    save(
                        status="failed_configuration",
                        reason="child_spawn_failed",
                        active_run_id=None,
                    )
                    return 2
                save(
                    status="running",
                    reason="bounded_window_running",
                    child_pid=child.pid,
                    child_identity=process_identity(child.pid),
                )
                owner_lock.close()  # Never LOCK_UN: the inherited description owns it.
                owner_lock = None
                stop.wait(0.2)
        except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as exc:
            # Do not serialize provider settings or arbitrary exception messages.
            save(
                status="failed_configuration",
                reason="frozen_preflight_or_state_invalid",
                error_type=type(exc).__name__,
            )
            return 2
        finally:
            if owner_lock is not None:
                owner_lock.close()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
