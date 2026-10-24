#!/usr/bin/env python3
"""Periodic official-directory discovery with frozen, raw-only acquisition cycles."""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from official_source_supervisor_recovery import (
    RecoveryDeferred,
    confined,
    digest,
    identity,
    load,
    lock,
    spawn_owned,
    still_alive,
    value_digest,
    write,
)

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = "periodic-official-collection-v1"
CATEGORIES = [
    "Acts Supplement",
    "Subsidiary Legislation Supplement",
    "Revised Acts",
    "Revised Subsidiary Legislation",
]
MAX_RECORDS = 50_000
STOP = threading.Event()
DEFAULTS = {
    "interval_seconds": 86400,
    "window_seconds": 600,
    "timeout_seconds": 45,
    "max_windows_per_stage": 16,
    "max_original_attempts": 8,
    "backoff_seconds": 60,
    "max_backoff_seconds": 3600,
    "original_byte_budget": 1024**3,
    "total_original_byte_budget": 10 * 1024**3,
    "min_free_bytes": 40 * 1024**3,
    "page_size": 100,
    "workers": 4,
    "per_host": 2,
    "recheck_after_seconds": 7 * 86400,
    "recheck_limit": 10,
}


def real_path(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("schedule paths must not contain symbolic links")
    return path


def bindings():
    # Freeze implementation and approved checklist recipes, without copying
    # credentials or depending on a mutable worktree commit label alone.
    names = [
        "scripts/periodic_official_collection.py",
        "scripts/periodic_official_sources.py",
        "scripts/periodic_official_budget.py",
        "scripts/official_source_supervisor_recovery.py",
        "scripts/collect_egazette.py",
        "scripts/collect_indonesia_official.py",
        "scripts/verify_collection_sources.py",
        "scripts/collect_sso_catalog.py",
        "config/collection-plans/indonesia-20261003.json",
        "config/collection-plans/singapore-demo-20261009.json",
    ]
    names += [str(path.relative_to(ROOT)) for path in sorted((ROOT / "backend/app").rglob("*.py"))]
    return {name: digest(ROOT / name) for name in names}


def validate_config(config):
    expected = {
        "protocol",
        "source",
        "scope",
        "project_root",
        "schedule_root",
        "bindings",
        "created_at",
        "models",
        "system_library_writes",
        "parse",
        "auto_import",
        *DEFAULTS,
    }
    if set(config) != expected or config["protocol"] != PROTOCOL:
        raise ValueError("unknown periodic configuration protocol or fields")
    if any(config[key] is not False for key in ("models", "system_library_writes", "parse", "auto_import")):
        raise ValueError("periodic collection is raw-only and cannot import or process documents")
    limits = {
        "interval_seconds": (60, 31 * 86400),
        "window_seconds": (1, 7200),
        "timeout_seconds": (1, 90),
        "max_windows_per_stage": (1, 100),
        "max_original_attempts": (1, 8),
        "backoff_seconds": (1, 86400),
        "max_backoff_seconds": (1, 7 * 86400),
        "original_byte_budget": (1, 10 * 1024**3),
        "total_original_byte_budget": (1, 64 * 1024**3),
        "min_free_bytes": (256 * 1024**2, 1024**5),
        "page_size": (10, 100),
        "workers": (1, 8),
        "per_host": (1, 2),
        "recheck_after_seconds": (3600, 366 * 86400),
        "recheck_limit": (0, 1000),
    }
    for key, (low, high) in limits.items():
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError(f"invalid bounded setting: {key}")
    if (
        config["page_size"] not in {10, 20, 50, 100}
        or config["backoff_seconds"] > config["max_backoff_seconds"]
    ):
        raise ValueError("invalid pagination or backoff")
    scope = config["scope"]
    if config["source"] == "egazette":
        if set(scope) != {"categories", "year_from", "year_through"} or not scope["categories"]:
            raise ValueError("explicit eGazette categories and years required")
        if (
            not set(scope["categories"]) <= set(CATEGORIES)
            or len(scope["categories"]) != len(set(scope["categories"]))
            or type(scope["year_from"]) is not int
            or type(scope["year_through"]) is not int
            or not 1998 <= scope["year_from"] <= scope["year_through"] <= datetime.now(UTC).year
        ):
            raise ValueError("invalid eGazette source scope")
    elif config["source"] == "setneg":
        if (
            set(scope) != {"types"}
            or not scope["types"]
            or not set(scope["types"]) <= {"UU", "PERPU", "PP", "PERPRES"}
            or len(scope["types"]) != len(set(scope["types"]))
        ):
            raise ValueError("invalid Setneg source scope")
    else:
        raise ValueError("unsupported official source")


def initialize(root, source, scope, *, storage_root=None, **options):
    root = real_path(root)
    allowed = real_path(storage_root or ROOT / ".dev")
    if not allowed.is_dir() or not root.is_relative_to(allowed) or root == allowed:
        raise ValueError("schedule must be a new child of the explicit existing storage root")
    if root.exists():
        raise ValueError("schedule already exists; configuration cannot be overwritten")
    config = {
        "protocol": PROTOCOL,
        "source": source,
        "scope": scope,
        "project_root": str(ROOT),
        "schedule_root": str(root),
        "created_at": time.time(),
        "bindings": bindings(),
        "models": False,
        "system_library_writes": False,
        "parse": False,
        "auto_import": False,
        **DEFAULTS,
        **options,
    }
    validate_config(config)
    root.mkdir(mode=0o700, parents=True)
    from periodic_official_budget import initialize as initialize_budget

    initialize_budget(root)
    write(root / "config.json", config, exclusive=True)
    write(
        root / "state.json",
        {
            "config_sha256": digest(root / "config.json"),
            "active_cycle": None,
            "last_cycle": None,
            "next_at": 0,
            "status": "ready",
            "completed_cycles": 0,
        },
        exclusive=True,
    )
    (root / "cycles").mkdir(mode=0o700)
    return config


def configuration(root):
    root = real_path(root)
    config = load(root / "config.json")
    validate_config(config)
    if config["schedule_root"] != str(root) or config["project_root"] != str(ROOT):
        raise ValueError("periodic configuration belongs to another project or storage path")
    if config["bindings"] != bindings():
        raise ValueError("collector/checklist implementation changed; create a reviewed new schedule")
    state = load(root / "state.json")
    if state["config_sha256"] != digest(root / "config.json"):
        raise ValueError("immutable periodic configuration changed")
    return config, state


def save_state(root, state, **values):
    state.update(values, updated_at=time.time())
    write(root / "state.json", state)


def frozen(path, value):
    if path.exists():
        if load(path) != value:
            raise ValueError("frozen cycle evidence changed")
    else:
        write(path, value, exclusive=True)


def checked_records(records):
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise ValueError("catalogue exceeds the configured evidence bound")
    result = {}
    for row in records:
        if (
            not isinstance(row.get("identity"), str)
            or not row["identity"]
            or row["fingerprint"] != value_digest(row["metadata"])
        ):
            raise ValueError("invalid official catalogue identity")
        if row["identity"] in result:
            raise ValueError("ambiguous duplicate official catalogue identity")
        result[row["identity"]] = row
    return result


def verify_acquisition(root, acquisition):
    cycle = confined(root / "cycles", acquisition["cycle"])
    source = cycle / "source"
    for row in acquisition["assets"]:
        path = confined(source, row["path"])
        if not path.is_file() or path.stat().st_size != row["size"] or digest(path) != row["sha256"]:
            raise ValueError("previous successful original changed; cannot skip acquisition")
    receipt = acquisition.get("receipt")
    if receipt and digest(confined(source, receipt["path"])) != receipt["sha256"]:
        raise ValueError("previous original receipt changed")
    if acquisition["status"] == "acquired" and not acquisition["assets"]:
        raise ValueError("successful acquisition has no verified original")


def freeze_selection(root, cycle, records, previous):
    current = checked_records(records)
    prior = previous.get("inventory", {})
    old_active = set(previous.get("active_identities", []))
    added = sorted(set(current) - old_active)
    changed = sorted(
        key for key in set(current) & old_active if prior[key]["fingerprint"] != current[key]["fingerprint"]
    )
    config = load(root / "config.json")
    due = sorted(
        (
            key
            for key in current
            if key in prior
            and prior[key]["fingerprint"] == current[key]["fingerprint"]
            and prior[key]["acquisition"]["status"] == "acquired"
            and not prior[key].get("recheck_blocked")
            and prior[key].get("checked_at", 0) + config["recheck_after_seconds"]
            <= load(cycle / "plan.json")["created_at"]
        ),
        key=lambda key: (prior[key].get("checked_at", 0), key),
    )[: config["recheck_limit"]]
    selected, retained = [], {}
    for key, row in current.items():
        old = prior.get(key)
        if old and old["fingerprint"] == row["fingerprint"]:
            # An unchanged permanent/exhausted gap remains visible instead of
            # silently resetting its attempts every time the directory refreshes.
            verify_acquisition(root, old["acquisition"])
            retained[key] = old
            if key in due:
                selected.append(row)
        else:
            selected.append(row)
    value = {
        "catalogue_sha256": digest(cycle / "catalogue.json"),
        "added": added,
        "changed": changed,
        "removed_from_directory": sorted(old_active - set(current)),
        "unchanged": sorted(set(current) & old_active - set(changed)),
        "selected": selected,
        "retained": retained,
        "recheck": due,
        "previous_inventory": prior,
        "legal_effect_inferred": False,
    }
    frozen(cycle / "selection.json", value)
    return value


def new_cycle(root, state):
    # Number+random identity is committed before a child can start. An orphan
    # empty directory is harmless; its name is never reused after a crash.
    number = max((int(path.name.split("-")[1]) for path in (root / "cycles").glob("cycle-*")), default=0) + 1
    name = f"cycle-{number:06d}-{uuid.uuid4().hex[:12]}"
    cycle = root / "cycles" / name
    cycle.mkdir(mode=0o700)
    (cycle / "windows").mkdir(mode=0o700)
    write(
        cycle / "plan.json",
        {
            "cycle": name,
            "config_sha256": state["config_sha256"],
            "previous_cycle": state["last_cycle"],
            "previous_handoff_sha256": state.get("last_handoff_sha256"),
            "created_at": time.time(),
            "models": False,
            "auto_import": False,
        },
        exclusive=True,
    )
    save_state(root, state, active_cycle=name, status="running", next_at=0)
    return cycle


def previous_handoff(root, cycle):
    plan = load(cycle / "plan.json")
    prior = plan["previous_cycle"]
    if not prior:
        return {}
    path = confined(root / "cycles", prior) / "handoff.json"
    if digest(path) != plan["previous_handoff_sha256"]:
        raise ValueError("previous handoff changed")
    return load(path)


def cycle_cooldown_until(cycle, result):
    # A terminal window still owes the upstream its Retry-After. Keep the
    # greatest recorded deadline, including an offline crash reconciliation,
    # so finishing a cycle or replaying its handoff cannot reset that debt.
    from periodic_official_sources import persisted_cooldown

    results = [result]
    for window in sorted((cycle / "windows").glob("window-*")):
        for name in ("result.json", "offline-reconciliation.json"):
            if (window / name).exists():
                results.append(load(window / name))
    deadlines = [item.get("next_at", 0) for item in results]
    if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in deadlines):
        raise ValueError("invalid persisted source cooldown")
    return max(*deadlines, persisted_cooldown(load(cycle.parent.parent / "config.json"), cycle))


def finalize(root, state, cycle, result, *, catalogue_incomplete=False):
    if catalogue_incomplete:
        previous = previous_handoff(root, cycle)
        inventory, active = previous.get("inventory", {}), previous.get("active_identities", [])
    else:
        selection = load(cycle / "selection.json")
        inventory = {**selection["previous_inventory"], **selection["retained"]}
        selected = {row["identity"]: row for row in selection["selected"]}
        outcomes = {row["identity"]: row for row in result["originals"]}
        if set(outcomes) != set(selected):
            raise ValueError("original results differ from the frozen selection")
        observations = []
        for key, outcome in outcomes.items():
            if outcome["fingerprint"] != selected[key]["fingerprint"]:
                raise ValueError("original result belongs to a different catalogue revision")
            acquisition = {**outcome, "cycle": cycle.name}
            verify_acquisition(root, acquisition)
            old = inventory.get(key)
            observation = {"identity": key, "acquisition": acquisition, "legal_version_assigned": False}
            if old:
                observation["previous_acquisition"] = old["acquisition"]
                observation["content_comparison"] = (
                    "not_acquired"
                    if outcome["status"] != "acquired"
                    else "same_bytes"
                    if sorted(item["sha256"] for item in acquisition["assets"])
                    == sorted(item["sha256"] for item in old["acquisition"]["assets"])
                    else "changed_bytes_candidate_version"
                )
            observations.append(observation)
            if key in selection["recheck"] and outcome["status"] != "acquired":
                # A failed refresh neither invalidates the historical original
                # nor resets the exhausted refresh on every directory cycle.
                inventory[key] = {**old, "recheck_blocked": {"cycle": cycle.name, "result": outcome}}
            else:
                inventory[key] = {
                    "fingerprint": selected[key]["fingerprint"],
                    "acquisition": acquisition,
                    "checked_at": time.time(),
                }
        active = list(checked_records(load(cycle / "catalogue.json")["records"]))
    gaps = [key for key in active if inventory[key]["acquisition"]["status"] != "acquired"]
    recheck_gaps = [key for key in active if inventory[key].get("recheck_blocked")]
    handoff = {
        "protocol": PROTOCOL,
        "cycle": cycle.name,
        "config_sha256": state["config_sha256"],
        "finished_at": time.time(),
        "source_cooldown_until": cycle_cooldown_until(cycle, result),
        "catalogue_complete": not catalogue_incomplete,
        "status": "catalogue_incomplete"
        if catalogue_incomplete
        else "ready_for_review_with_gaps"
        if gaps or recheck_gaps
        else "ready_for_review",
        "inventory": inventory,
        "active_identities": active,
        "gaps": gaps,
        "recheck_gaps": recheck_gaps,
        "model_calls": 0,
        "processing_started": False,
        "system_library_writes": False,
        "automatically_imported": False,
        "legal_review": "unreviewed",
        "content_observations": [] if catalogue_incomplete else observations,
        "note": "Directory changes and changed bytes require review; no legal version is assigned.",
    }
    frozen(cycle / "handoff.json", handoff)
    finish_state(root, state, cycle, handoff)
    return handoff


def finish_state(root, state, cycle, handoff):
    config = load(root / "config.json")
    save_state(
        root,
        state,
        active_cycle=None,
        last_cycle=cycle.name,
        status="scheduled",
        next_at=max(handoff["finished_at"] + config["interval_seconds"], handoff["source_cooldown_until"]),
        completed_cycles=state["completed_cycles"] + 1,
        last_handoff_sha256=digest(cycle / "handoff.json"),
    )


def model_free_env():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LLM_", "VISION_", "EMBEDDING_", "RERANK_"))
    }
    for prefix in ("LLM", "VISION", "EMBEDDING", "RERANK"):
        for field in ("BASE_URL", "API_KEY", "MODEL"):
            env[f"{prefix}_{field}"] = ""
    env.update(VISION_OCR_ENABLED="false", TEA_SHARED_LIBRARY_WORKERS="external", PYTHONDONTWRITEBYTECODE="1")
    return env


@contextmanager
def available_window(window):
    # The inherited flock also covers a parent killed between Popen and PID
    # persistence. A PID number by itself is never treated as ownership proof.
    with lock(window / "collector-process.lock"):
        marker = window / "collector-process.json"
        if marker.exists() and still_alive(load(marker).get("identity")):
            raise RecoveryDeferred("owned collector is still alive")
        yield


def execute(root, window, state):
    args = [
        sys.executable,
        str(Path(__file__).resolve()),
        "worker",
        "--schedule-dir",
        str(root),
        "--window",
        str(window.relative_to(root)),
    ]
    with (window / "collector.log").open("a") as output:
        child = spawn_owned(
            args, window=window, kind="collector", cwd=ROOT, env=model_free_env(), stdout=output
        )
    save_state(root, state, status="running", child_identity=identity(child.pid))
    while child.poll() is None:
        if STOP.is_set() or (root / "STOP").exists():
            if state["status"] != "draining":
                save_state(root, state, status="draining")
        time.sleep(0.2)
    write(window / "collector-exit.json", {"returncode": child.returncode, "at": time.time()}, exclusive=True)


def step(root, config, state, *, launch=execute, clock=None):
    clock = clock or time.time
    if state["next_at"] > clock():
        return "waiting"
    if STOP.is_set() or (root / "STOP").exists():
        save_state(root, state, status="stopped")
        return "stopped"
    cycle = (
        confined(root / "cycles", state["active_cycle"]) if state["active_cycle"] else new_cycle(root, state)
    )
    if (cycle / "handoff.json").exists():
        finish_state(root, state, cycle, load(cycle / "handoff.json"))
        return "scheduled"
    plan = load(cycle / "plan.json")
    if plan["config_sha256"] != state["config_sha256"]:
        raise ValueError("cycle belongs to another schedule configuration")
    stage = "originals" if (cycle / "selection.json").exists() else "catalogue"
    windows = sorted((cycle / "windows").glob("window-*"))
    pending = [path for path in windows if not (path / "reconciled.json").exists()]
    if len(pending) > 1 or (pending and pending[0] != windows[-1]):
        raise ValueError("ambiguous unfinished windows")
    if pending:
        window = pending[0]
        with available_window(window):
            pass
        # Missing final report after an exited child is not completion. Its
        # successful durable receipts are verified by the next bounded window.
        if not (window / "result.json").exists():
            write(
                window / "result.json",
                {"complete": False, "retryable": True, "interrupted": True, "next_at": 0},
                exclusive=True,
            )
    else:
        from periodic_official_budget import summary

        budget = summary(root)
        if budget["charged_bytes"] >= config["total_original_byte_budget"]:
            save_state(
                root,
                state,
                status="budget_wait",
                transfer_budget=budget,
                next_at=clock() + config["interval_seconds"],
            )
            return "waiting"
        if shutil.disk_usage(root).free < config["min_free_bytes"] + 128 * 1024**2:
            save_state(root, state, status="storage_wait", next_at=clock() + config["backoff_seconds"])
            return "waiting"
        window = cycle / "windows" / f"window-{len(windows) + 1:06d}"
        window.mkdir(mode=0o700)
        write(
            window / "plan.json",
            {
                "cycle": cycle.name,
                "stage": stage,
                "config_sha256": state["config_sha256"],
                "selection_sha256": digest(cycle / "selection.json") if stage == "originals" else None,
            },
            exclusive=True,
        )
        launch(root, window, state)
        with available_window(window):
            pass
        if not (window / "result.json").exists():
            write(
                window / "result.json",
                {"complete": False, "retryable": True, "interrupted": True, "next_at": 0},
                exclusive=True,
            )
    window_plan, result = load(window / "plan.json"), load(window / "result.json")
    stage = window_plan["stage"]
    if window_plan["config_sha256"] != state["config_sha256"] or window_plan["cycle"] != cycle.name:
        raise ValueError("window scope changed")
    attempts = sum(
        load(path / "plan.json")["stage"] == stage for path in (cycle / "windows").glob("window-*")
    )
    until = cycle_cooldown_until(cycle, result)
    if result["complete"] and stage == "catalogue":
        checked_records(result["records"])
        frozen(cycle / "catalogue.json", {"records": result["records"], "window": window.name})
        freeze_selection(root, cycle, result["records"], previous_handoff(root, cycle))
        save_state(root, state, next_at=until, status="running")
    elif stage == "originals" and (
        result["complete"] or not result.get("retryable", True) or attempts >= config["max_windows_per_stage"]
    ):
        # A crash may not have emitted a full original inventory yet. Always
        # run one offline receipt reconciliation instead of inventing gaps.
        if "originals" not in result:
            from periodic_official_sources import inspect_originals

            result = inspect_originals(config, cycle, load(cycle / "selection.json")["selected"])
            frozen(window / "offline-reconciliation.json", result)
        finalize(root, state, cycle, result)
    elif stage == "catalogue" and attempts >= config["max_windows_per_stage"]:
        finalize(root, state, cycle, result, catalogue_incomplete=True)
    else:
        delay = min(config["max_backoff_seconds"], config["backoff_seconds"] * 2 ** min(attempts - 1, 16))
        save_state(root, state, status="cooldown", next_at=max(clock() + delay, until), child_identity=None)
    write(window / "reconciled.json", {"result_sha256": digest(window / "result.json")}, exclusive=True)
    return state["status"]


def worker(root, relative):
    config, state = configuration(root)
    window = confined(root, relative)
    if window.parent.name != "windows" or window.parent.parent.parent != root / "cycles":
        raise ValueError("worker window is outside the schedule")
    plan = load(window / "plan.json")
    cycle = window.parent.parent
    if state["active_cycle"] != cycle.name or plan["config_sha256"] != state["config_sha256"]:
        raise ValueError("worker is not the active frozen cycle")
    selected = []
    if plan["stage"] == "originals":
        if digest(cycle / "selection.json") != plan["selection_sha256"]:
            raise ValueError("frozen candidate selection changed")
        selected = load(cycle / "selection.json")["selected"]
    os.environ.update(model_free_env())
    sys.path.insert(0, str(ROOT / "backend"))
    from periodic_official_sources import collect

    with lock(cycle / "source-stage.lock"):
        if (window / "result.json").exists():
            return
        try:
            result = collect(config, cycle, plan["stage"], selected)
        except (OSError, ValueError, TimeoutError) as exc:
            result = {
                "complete": False,
                "retryable": True,
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
                "next_at": time.time() + float(getattr(exc, "retry_after", 0) or 0),
            }
        write(window / "result.json", result, exclusive=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["init", "run", "status", "worker"])
    parser.add_argument("--schedule-dir", type=Path, required=True)
    parser.add_argument("--storage-root", type=Path)
    parser.add_argument("--source", choices=["setneg", "egazette"])
    parser.add_argument("--types", nargs="+", default=["UU", "PERPU", "PP", "PERPRES"])
    parser.add_argument("--categories", nargs="+", choices=CATEGORIES, default=CATEGORIES)
    parser.add_argument("--year-from", type=int, default=datetime.now(UTC).year)
    parser.add_argument("--year-through", type=int, default=datetime.now(UTC).year)
    parser.add_argument("--once", action="store_true", help="one due bounded window, then return")
    parser.add_argument("--window", help=argparse.SUPPRESS)
    for key, default in DEFAULTS.items():
        parser.add_argument("--" + key.replace("_", "-"), type=int, default=default)
    args = parser.parse_args()
    os.umask(0o077)
    root = real_path(args.schedule_dir)
    if args.command == "init":
        scope = (
            {"types": args.types}
            if args.source == "setneg"
            else {
                "categories": args.categories,
                "year_from": args.year_from,
                "year_through": args.year_through,
            }
        )
        initialize(
            root,
            args.source,
            scope,
            storage_root=args.storage_root,
            **{key: getattr(args, key) for key in DEFAULTS},
        )
        print(json.dumps({"status": "configured", "schedule_dir": str(root)}))
        return 0
    config, state = configuration(root)
    if args.command == "status":
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    if args.command == "worker":
        worker(root, args.window)
        return 0
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: STOP.set())
    with lock(root / "supervisor.lock"):
        # Acquire before re-reading state, so two launchers cannot use stale
        # snapshots after the first process has completed a window.
        config, state = configuration(root)
        while True:
            try:
                status = step(root, config, state)
            except RecoveryDeferred:
                save_state(root, state, status="observing_existing_worker")
                status = "waiting"
            if args.once or status == "stopped":
                return 0
            if STOP.is_set() or (root / "STOP").exists():
                save_state(root, state, status="stopped", child_identity=None)
                return 0
            delay = max(0.2, min(30, state.get("next_at", 0) - time.time()))
            STOP.wait(delay)


if __name__ == "__main__":
    raise SystemExit(main())
