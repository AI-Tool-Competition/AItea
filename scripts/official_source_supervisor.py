"""Continue two explicit official source scopes with the existing locked collectors."""

from __future__ import annotations

import argparse
import fcntl
import importlib
import importlib.util
import json
import os
import shutil
import signal
import sqlite3
import sys
import threading
import time
import uuid
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from official_source_supervisor_recovery import (
    RecoveryDeferred,
    confined,
    identity,
    load,
    observe_finalized_workers,
    pending_window,
    recover_interrupted_window,
    spawn_owned,
    validate_bindings,
    write,
)
from official_source_supervisor_recovery import (
    digest as sha,
)

BUNDLE_ROOT = P = DATA_ROOT = CONTROL_ROOT = ID = SG = TEMPLATE = None
STOP = threading.Event()
MIN_FREE = 40 * 1024**3
MUTEX = threading.Lock()
STATE = {}


def configure(bundle):
    global BUNDLE_ROOT, P, DATA_ROOT, CONTROL_ROOT, ID, SG, TEMPLATE, MIN_FREE
    BUNDLE_ROOT = Path(bundle).absolute()
    if BUNDLE_ROOT.is_symlink() or not BUNDLE_ROOT.is_dir():
        raise ValueError("bundle must be an existing real directory")
    # Verify the sealed package before executing its preflight helper.
    manifest = load(BUNDLE_ROOT / "manifest.json")
    if not 1 <= len(manifest["files"]) <= 1000:
        raise ValueError("invalid sealed package file count")
    for relative, record in manifest["files"].items():
        path = confined(BUNDLE_ROOT, relative)
        if path.stat().st_size != record["size"] or sha(path) != record["sha256"]:
            raise ValueError("sealed package changed: " + relative)
    for name in ("operator_control.py", "plan.json", "repository.json"):
        if name not in manifest["files"]:
            raise ValueError("package is missing a required verified file")
    spec = importlib.util.spec_from_file_location(
        "official_source_bundle_preflight", BUNDLE_ROOT / "operator_control.py"
    )
    operator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(operator)
    proof = operator.preflight(BUNDLE_ROOT, check_data_paths=True)
    runtime = load(BUNDLE_ROOT / "runtime.json")
    P, DATA_ROOT = Path(runtime["project_root"]), Path(runtime["data_root"])
    CONTROL_ROOT = confined(DATA_ROOT, runtime["control_relative"])
    plan = load(BUNDLE_ROOT / "plan.json")
    if plan["protocol"] != "fixed-official-source-supervisor-v2" or any(
        plan[key] is not False
        for key in (
            "paid_models",
            "system_library_writes",
            "native_setneg",
            "scope_auto_expansion",
        )
    ):
        raise ValueError("only the explicit offline-model fixed source protocol is supported")
    ID, SG = [confined(DATA_ROOT, plan["source_dirs"][key]) for key in ("id", "sg")]
    TEMPLATE = BUNDLE_ROOT / "templates"
    MIN_FREE = plan["min_free_bytes"]
    return plan, proof


def offline_parent(event, _args):
    if event in {
        "socket.connect",
        "socket.getaddrinfo",
        "socket.sendto",
        "socket.bind",
    }:
        raise RuntimeError(
            "source supervisor/recovery process forbids network; only collector children may fetch"
        )


def idle(root, names):
    handles = []
    try:
        for name in names:
            handle = (root / name).open("a")
            handles.append(handle)
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        for handle in handles:
            handle.close()


def emit(source, **values):
    with MUTEX:
        prior = STATE.get(source, {})
        row = {**prior, **values, "updated_at": datetime.now(UTC).isoformat()}
        STATE[source] = row
        write(
            CONTROL_ROOT / "state.json",
            {
                "protocol": "fixed-official-source-supervisor-v3",
                "supervisor": {
                    key: value for key, value in identity(os.getpid()).items() if key != "boot_id"
                },
                "sources": STATE,
            },
        )
        changed = any(
            prior.get(key) != row.get(key) for key in ("status", "reason", "window", "child_pid", "next_at")
        )
        if changed:
            with (CONTROL_ROOT / "events.jsonl").open("a") as stream:
                stream.write(json.dumps({"source": source, **row}, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            print(json.dumps({"source": source, **row}, ensure_ascii=False), flush=True)


def wait_until(source, until, reason):
    emit(source, status="waiting", reason=reason, next_at=until)
    while not STOP.is_set() and time.time() < until:
        if (CONTROL_ROOT / "STOP").exists():
            STOP.set()
            break
        STOP.wait(min(30, max(0, until - time.time())))


def source_state(source, modules, plan):
    if source == "id":
        module = modules["id"]
        definitions = {
            r["id"]: r
            for r in module.load_blueprints()["indonesia-20261003"]["items"]
            if r["id"] in module.SETNEG_ITEMS
        }
        with closing(sqlite3.connect((ID / "journal.sqlite3").as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            counts = {
                f"{r[0]}:{r[1]}": r[2]
                for r in conn.execute("SELECT purpose,status,count(*) FROM tasks GROUP BY purpose,status")
            }
            queued = sum(v for k, v in counts.items() if k.endswith(":queued"))
            retry = module.transient_original_retries(conn, definitions)
            cooldown = float(dict(conn.execute("SELECT key,value FROM config")).get("cooldown_until", "0"))
            total = sum(
                json.loads(r[0])["size"]
                for r in conn.execute(
                    "SELECT result FROM tasks WHERE purpose='document' AND status='downloaded'"
                )
            )
        pending = len(retry["eligible"]) + len(retry["waiting"])
        report = load(ID / "report.json")
        return {
            "counts": counts,
            "queued": queued,
            "recoverable": pending,
            "exhausted": len(retry["exhausted"]),
            "next_at": max(cooldown, retry["next_at"] or 0) if not queued else cooldown,
            "bytes": total,
            "byte_limit": plan["cumulative_byte_budgets"]["id"],
            "complete": bool(report.get("requested_scope", {}).get("complete")),
            "catalogue_rows": report["ledger_rows"],
            "requires_scope_evaluation": bool(report.get("recovery_requires_scope_evaluation")),
        }
    module = modules["sg"]
    rows = [load(path) for path in (SG / "originals").glob("*.json")]
    counts = dict(Counter(r["status"] for r in rows))
    retry = []
    for row in rows:
        if row["status"] == "incomplete":
            until = module.original_retry_at(SG, row, max_attempts=plan["max_total_original_attempts"])
            if until is not None:
                retry.append(until)
    cooldown = float(load(SG / "network-state.json").get("assets.egazette.gov.sg", 0))
    queued = plan["selected_remaining_initial"]["sg"] - len(rows)
    if queued < 0:
        raise ValueError("receipts exceed the frozen selection")
    return {
        "counts": counts,
        "queued": queued,
        "recoverable": len(retry),
        "exhausted": counts.get("incomplete", 0) - len(retry),
        "next_at": max(cooldown, min(retry, default=0)) if not queued else cooldown,
        "bytes": sum(r["size"] for r in rows if r["status"] == "downloaded"),
        "byte_limit": plan["cumulative_byte_budgets"]["sg"],
        "complete": not queued and not counts.get("incomplete", 0),
        "catalogue_rows": plan["known_directory_rows"]["sg"],
    }


def publish_window(staging, destination):
    for path in staging.iterdir():
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    staging.rename(destination)
    for directory in (destination, destination.parent):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    return destination


def new_window(source, plan):
    directory = CONTROL_ROOT / source
    directory.mkdir(mode=0o700, exist_ok=True)
    number = max((int(p.name.split("-")[1]) for p in directory.glob("window-*")), default=0) + 1
    destination = directory / f"window-{number:04d}"
    window = directory / f".{destination.name}.{uuid.uuid4().hex}.preparing"
    window.mkdir(mode=0o700)
    if source == "id":
        args = [
            sys.executable,
            str(P / "scripts/collect_indonesia_official.py"),
            "--output",
            str(ID),
            "--storage-root",
            str(DATA_ROOT),
            "--resume",
            "--retry-transient",
            "--detail-types",
            "UU,PERPU,PP,PERPRES",
            "--original-byte-budget",
            str(plan["cumulative_byte_budgets"]["id"]),
            "--min-free-bytes",
            str(MIN_FREE),
            "--prepare-library",
            "--time-budget",
            str(plan["per_window_seconds"]),
            "--timeout",
            "90",
            "--workers",
            str(plan["worker_limits"]["id_scheduler"]),
            "--per-host",
            str(plan["worker_limits"]["id_host"]),
        ]
        shutil.copyfile(ID / "report.json", window / "previous-report.json")
        write(window / "command.json", args)
        write(
            window / "window-plan.json",
            {
                "protocol": "setneg-controlled-window-v1",
                "supervisor_plan_sha256": sha(BUNDLE_ROOT / "plan.json"),
                "previous_final_report_sha256": sha(window / "previous-report.json"),
                "command_sha256": sha(window / "command.json"),
                "additional_budget_seconds": plan["per_window_seconds"],
            },
        )
        window = publish_window(window, destination)
        return window, args, None
    previous = load(SG / "continuation-report.json")
    binding = {
        **load(TEMPLATE / "window-plan.json"),
        "created_at": datetime.now(UTC).isoformat(),
        "previous_final_report_sha256": sha(SG / "continuation-report.json"),
        "previous_elapsed_seconds_total": previous["elapsed_seconds_total"],
        "additional_budget_seconds": plan["per_window_seconds"],
        "effective_total_budget_seconds": previous["elapsed_seconds_total"] + plan["per_window_seconds"],
        "prior_status_counts": previous["status_counts"],
        "prior_unattempted": previous["remaining_unattempted"],
        "supervisor_plan_sha256": sha(BUNDLE_ROOT / "plan.json"),
    }
    write(window / "window-plan.json", binding)
    shutil.copyfile(SG / "continuation-report.json", window / "previous-report.json")
    for name in ("collect_sg_round3_full.py", "process_sg_round3_native.py"):
        shutil.copyfile(TEMPLATE / name, window / name)
    window = publish_window(window, destination)
    return (
        window,
        [sys.executable, str(window / "collect_sg_round3_full.py")],
        [sys.executable, str(window / "process_sg_round3_native.py")],
    )


def reconcile(source, plan, modules):
    window = pending_window(CONTROL_ROOT, source)
    if window is None:
        observe_finalized_workers(CONTROL_ROOT, source, STATE.get(source, {}))
        return None
    from processing_runtime import retry_local_database

    result = retry_local_database(
        lambda: recover_interrupted_window(
            source=source,
            window=window,
            project=P,
            data=DATA_ROOT,
            bundle=BUNDLE_ROOT,
            control=CONTROL_ROOT,
            plan=plan,
            modules=modules,
            saved_state=STATE.get(source, {}),
        ),
        on_wait=lambda **wait: emit(
            source, status="recovering", reason="bounded_local_database_wait", **wait
        ),
    )
    if result:
        emit(
            source,
            status="interrupted_reconciled",
            reason="verified_receipts_registered_offline",
            window=str(window),
            child_pid=None,
            recovery=result,
        )
    return result


def terminal_belongs_to_window(source, root, window):
    path = root / ("report.json" if source == "id" else "continuation-report.json")
    if not path.exists():
        return False
    report = load(path)
    if source == "sg":
        return report.get("controlled_window") == load(window / "window-plan.json")
    # An ID child may die after a periodic report. Only a recorded normal exit
    # allows its report to be regarded as a completed window.
    return report.get("plan_sha256") == load(window / "previous-report.json").get("plan_sha256")


def supervise(source, plan, modules):
    root = ID if source == "id" else SG
    locks = ["collector.lock"] if source == "id" else ["continuation.lock", "native-processing.lock"]
    child = native = None
    try:
        while not STOP.is_set():
            if (CONTROL_ROOT / "STOP").exists():
                STOP.set()
                break
            validate_bindings(P, DATA_ROOT, BUNDLE_ROOT, plan)
            try:
                # Recovery precedes the native-lock check: that consumer can be
                # waiting for the terminal report lost with its collector.
                reconcile(source, plan, modules)
            except RecoveryDeferred as exc:
                emit(
                    source,
                    status="observing_existing_worker",
                    reason=str(exc),
                    child_pid=None,
                )
                STOP.wait(30)
                continue
            if not idle(root, locks):
                emit(
                    source,
                    status="observing_existing_worker",
                    reason="source_lock_held",
                    child_pid=None,
                )
                STOP.wait(30)
                continue
            state = source_state(source, modules, plan)
            emit(source, progress=state)
            if state["complete"]:
                emit(
                    source,
                    status="raw_complete",
                    reason="fixed_original_scope_acquired",
                    child_pid=None,
                )
                return
            if (
                not state["queued"]
                and not state["recoverable"]
                and not state.get("requires_scope_evaluation")
            ):
                emit(
                    source,
                    status="needs_attention",
                    reason="only_permanent_or_exhausted_gaps_remain",
                    child_pid=None,
                )
                return
            if (
                state["bytes"] >= state["byte_limit"]
                or shutil.disk_usage(root).free < MIN_FREE + 128 * 1024**2
            ):
                emit(
                    source,
                    status="needs_attention",
                    reason="fixed_byte_or_disk_reserve",
                    child_pid=None,
                )
                return
            if state["next_at"] > time.time():
                wait_until(source, state["next_at"], "durable_source_or_original_backoff")
                continue
            window, argv, native_argv = new_window(source, plan)
            write(window / "before.json", state)
            with (window / "collector.log").open("a") as output:
                child = spawn_owned(
                    argv,
                    window=window,
                    kind="collector",
                    cwd=P,
                    env=os.environ.copy(),
                    stdout=output,
                )
            emit(
                source,
                status="running",
                reason="bounded_official_window",
                window=str(window),
                child_pid=child.pid,
                child_identity=identity(child.pid),
                next_at=None,
            )
            if native_argv:
                deadline = time.monotonic() + 60
                while (
                    (SG / "continuation-report.json").exists()
                    and child.poll() is None
                    and time.monotonic() < deadline
                ):
                    STOP.wait(0.2)
                if child.poll() is None and not (SG / "continuation-report.json").exists():
                    with (window / "native.log").open("a") as output:
                        native = spawn_owned(
                            native_argv,
                            window=window,
                            kind="native",
                            cwd=P,
                            env=os.environ.copy(),
                            stdout=output,
                        )
                    emit(
                        source,
                        native_pid=native.pid,
                        native_identity=identity(native.pid),
                    )
            while child.poll() is None:
                if (CONTROL_ROOT / "STOP").exists():
                    STOP.set()
                if STOP.is_set():
                    emit(source, status="draining", reason="operator_stop_after_window")
                time.sleep(1)
            code = child.returncode
            write(
                window / "collector-exit.json",
                {"returncode": code, "at": datetime.now(UTC).isoformat()},
            )
            child = None
            abnormal = code not in (0, 2) or not terminal_belongs_to_window(source, root, window)
            if abnormal:
                # Never wait for a native consumer before supplying the missing
                # terminal report. A live/unknown collector is still deferred.
                while True:
                    try:
                        reconcile(source, plan, modules)
                        break
                    except RecoveryDeferred as exc:
                        emit(source, status="recovering", reason=str(exc))
                        if STOP.is_set() or (CONTROL_ROOT / "STOP").exists():
                            raise
                        STOP.wait(30)
            if native:
                while native.poll() is None:
                    time.sleep(1)
                native_code = native.returncode
                write(
                    window / "native-exit.json",
                    {"returncode": native_code, "at": datetime.now(UTC).isoformat()},
                )
                native = None
                if native_code:
                    raise RuntimeError(f"native worker exit {native_code}; successful pages are retained")
            current = source_state(source, modules, plan)
            write(window / "after.json", current)
            if not abnormal:
                write(
                    window / "final-report.json",
                    load(root / ("report.json" if source == "id" else "continuation-report.json")),
                    exclusive=True,
                )
                emit(
                    source,
                    status="window_complete",
                    reason="bounded_window_drained",
                    child_pid=None,
                    native_pid=None,
                    progress=current,
                )
            if current["counts"] == state["counts"] and current["next_at"] <= time.time():
                wait_until(source, time.time() + 60, "no_progress_backoff")
        emit(
            source,
            status="stopped",
            reason="operator_stop",
            child_pid=None,
            native_pid=None,
        )
    except BaseException as exc:  # noqa: BLE001 -- durable failure report at the worker-thread boundary
        emit(
            source,
            status="needs_attention",
            reason=f"{type(exc).__name__}: {exc}",
            child_pid=child.pid if child and child.poll() is None else None,
        )
        # Never kill a child on an observation/validation failure. Its source
        # lock, inherited process lock, and bounded deadline remain authoritative.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", type=Path, required=True)
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Verify the sealed scope without starting workers",
    )
    args = parser.parse_args()
    plan, proof = configure(args.bundle_dir)
    if args.inspect:
        print(json.dumps(proof, ensure_ascii=False, indent=2))
        return
    sys.addaudithook(offline_parent)
    os.umask(0o077)
    for key in list(os.environ):
        if key.startswith(("LLM_", "VISION_", "EMBEDDING_", "RERANK_")):
            os.environ.pop(key)
    os.environ["VISION_OCR_ENABLED"] = "false"
    os.environ["AITEA_PROJECT_ROOT"] = str(P)
    os.environ["AITEA_DATA_ROOT"] = str(DATA_ROOT)
    sys.path[:0] = [str(P / "scripts"), str(P / "backend")]
    modules = {
        "id": importlib.import_module("collect_indonesia_official"),
        "sg": importlib.import_module("collect_egazette"),
    }
    with (CONTROL_ROOT / "supervisor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (CONTROL_ROOT / "state.json").exists():
            STATE.update(load(CONTROL_ROOT / "state.json")["sources"])
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: STOP.set())
        workers = [
            threading.Thread(
                target=supervise,
                args=(source, plan, modules),
                name=f"{source}-official-supervisor",
            )
            for source in ("id", "sg")
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()


if __name__ == "__main__":
    main()
