"""Offline recovery of an interrupted, explicitly frozen official-source window.

The caller owns supervisor.lock. This module never downloads, launches workers,
resets attempts, or infers that a vanished process completed normally.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import uuid
from collections import Counter
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.shared.infrastructure import darwin_process  # noqa: E402

MAX_JSON_BYTES = 256 * 1024**2
MAX_DOCUMENTS = 50_000
MAX_RECEIPTS = 100_000
WINDOW = re.compile(r"window-\d{4,8}\Z")


class RecoveryDeferred(Exception):
    """A live/unknown process or storage reserve forbids recovery for now."""


_OFFLINE = threading.local()


def _offline_audit(event, _args):
    if getattr(_OFFLINE, "active", False) and event in {
        "socket.connect",
        "socket.getaddrinfo",
        "socket.sendto",
        "socket.bind",
        "subprocess.Popen",
    }:
        raise RuntimeError("offline source recovery forbids network, models and child processes")


sys.addaudithook(_offline_audit)


def offline_only(function):
    @wraps(function)
    def run(*args, **kwargs):
        previous = getattr(_OFFLINE, "active", False)
        _OFFLINE.active = True
        try:
            return function(*args, **kwargs)
        finally:
            _OFFLINE.active = previous

    return run


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"invalid bounded JSON file: {path}")
    return json.loads(path.read_bytes())


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()


def value_digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def write(path, value, *, exclusive=False):
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with temporary.open("xb") as stream:
            os.chmod(temporary, 0o600)
            stream.write(encoded(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def confined(root, relative):
    root, relative = Path(root), Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path must remain relative to its declared root")
    path = root
    for part in relative.parts:
        path /= part
        if path.is_symlink():
            raise ValueError("source/recovery paths must not contain symlinks")
    return path


@contextmanager
def lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RecoveryDeferred(f"active lock: {path}") from exc
        yield stream


def identity(pid):
    if type(pid) is not int or pid < 1:
        raise ValueError("invalid process identity")
    try:
        if sys.platform == "darwin":
            start = darwin_process.process_start(pid)
            if start is None:
                return None
            command, _ = darwin_process.process_arguments(pid)
            if darwin_process.process_start(pid) != start:
                raise RecoveryDeferred("process identity changed during inspection")
            return {
                "pid": pid,
                "start_ticks": start,
                "boot_id": "darwin",
                "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            }
        root = Path("/proc") / str(pid)
        fields = (root / "stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "start_ticks": fields[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "command_sha256": hashlib.sha256((root / "cmdline").read_bytes()).hexdigest(),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None
    except PermissionError as exc:
        raise RecoveryDeferred("process identity is not observable") from exc


def still_alive(saved):
    if not saved:
        return False
    current = identity(saved["pid"])
    return bool(
        current
        and current["start_ticks"] == saved["start_ticks"]
        and (not saved.get("boot_id") or current["boot_id"] == saved["boot_id"])
    )


def spawn_owned(argv, *, window, kind, cwd, env, stdout):
    """An inherited flock closes the Popen-to-PID-record crash gap."""
    if kind not in {"collector", "native"}:
        raise ValueError("invalid owned process kind")
    with lock(window / f"{kind}-process.lock") as guard:
        record = {"argv": list(argv), "phase": "starting", "identity": None}
        write(window / f"{kind}-process.json", record)
        child = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            pass_fds=(guard.fileno(),),
        )
        # Never unlock explicitly: the child holds the same open file description.
        write(
            window / f"{kind}-process.json",
            {**record, "phase": "started", "identity": identity(child.pid)},
        )
        return child


def source_windows(control, source):
    directory = confined(control, source)
    if not directory.exists():
        return []
    windows = []
    for path in directory.iterdir():
        if WINDOW.fullmatch(path.name):
            if path.is_symlink() or not path.is_dir():
                raise ValueError("invalid window directory")
            final = path / "final-report.json"
            if final.is_symlink() or (final.exists() and not final.is_file()):
                raise ValueError("invalid terminal report path")
            windows.append(path)
    return sorted(windows, key=lambda path: int(path.name.split("-")[1]))


def pending_window(control, source):
    windows = source_windows(control, source)
    pending = [path for path in windows if not (path / "final-report.json").exists()]
    if len(pending) > 1 or (pending and pending[0] != windows[-1]):
        raise ValueError("ambiguous unfinished source windows; preserve all evidence")
    return pending[0] if pending else None


def observe_finalized_workers(control, source, saved_state):
    windows = source_windows(control, source)
    if not windows or not (windows[-1] / "final-report.json").exists():
        return
    window = windows[-1]
    for kind in ("collector", "native") if source == "sg" else ("collector",):
        if still_alive(_process_record(window, kind, saved_state).get("identity")):
            raise RecoveryDeferred("finalized window still has a live owned worker")
        path = window / f"{kind}-process.lock"
        if path.exists():
            with lock(path):
                pass


def validate_bindings(project, data, bundle, plan):
    if load(bundle / "plan.json") != plan:
        raise ValueError("supervisor plan changed after startup")
    roots = {"project": project, "data": data, "bundle": bundle}
    for relative, expected in plan["code_sha256"].items():
        if digest(confined(project, relative)) != expected:
            raise ValueError("formal collector changed; explicit review required")
    for binding in plan["scope_bindings"]:
        if digest(confined(roots[binding["root"]], binding["path"])) != binding["sha256"]:
            raise ValueError("frozen source scope or template changed")


def _process_record(window, kind, saved_state):
    path = window / f"{kind}-process.json"
    if path.exists():
        return load(path)
    if saved_state.get("window") == str(window):
        return {"identity": saved_state.get("child_identity" if kind == "collector" else "native_identity")}
    return {"identity": None}


def _validate_window(source, window, root, bundle, plan):
    previous = load(window / "previous-report.json")
    if source == "id":
        command = load(window / "command.json")
        expected = [
            str(Path(__file__).resolve().parent / "collect_indonesia_official.py"),
            "--output",
            str(root),
            "--storage-root",
            str(root.parents[2]),
            "--resume",
            "--retry-transient",
            "--detail-types",
            "UU,PERPU,PP,PERPRES",
            "--original-byte-budget",
            str(plan["cumulative_byte_budgets"]["id"]),
            "--min-free-bytes",
            str(plan["min_free_bytes"]),
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
        if not isinstance(command, list) or command[1:] != expected or not Path(command[0]).is_absolute():
            raise ValueError("Setneg window command differs from the frozen collector scope")
        binding = {
            "protocol": "setneg-controlled-window-v1",
            "supervisor_plan_sha256": digest(bundle / "plan.json"),
            "previous_final_report_sha256": digest(window / "previous-report.json"),
            "command_sha256": digest(window / "command.json"),
            "additional_budget_seconds": plan["per_window_seconds"],
        }
        if (window / "window-plan.json").exists() and load(window / "window-plan.json") != binding:
            raise ValueError("Setneg interrupted window command/budget binding mismatch")
        return binding, previous
    binding = load(window / "window-plan.json")
    if (
        binding.get("protocol") != "egazette-controlled-window-v1"
        or binding.get("base_plan_sha256") != digest(root / "plan.json")
        or binding.get("selected_sha256") != digest(root / "selected-records.json")
        or binding.get("previous_final_report_sha256") != digest(window / "previous-report.json")
        or binding.get("supervisor_plan_sha256") != digest(bundle / "plan.json")
        or binding.get("official_downloader_sha256") != plan["code_sha256"]["scripts/collect_egazette.py"]
        or binding.get("additional_budget_seconds") != plan["per_window_seconds"]
        or binding.get("max_original_attempts") != plan["max_total_original_attempts"]
        or binding.get("host_limit") != plan["worker_limits"]["sg_host"]
        or binding.get("models") is not False
        or binding.get("scope_change") is not False
        or binding.get("min_free_bytes") != plan["min_free_bytes"]
    ):
        raise ValueError("SG interrupted window scope/budget binding mismatch")
    old = previous["elapsed_seconds_total"]
    total = binding["effective_total_budget_seconds"]
    if (
        type(old) not in (int, float)
        or not math.isfinite(old)
        or old < 0
        or binding["previous_elapsed_seconds_total"] != old
        or total != old + plan["per_window_seconds"]
    ):
        raise ValueError("interrupted window budget is invalid")
    for name in ("collect_sg_round3_full.py", "process_sg_round3_native.py"):
        if digest(window / name) != digest(bundle / "templates" / name):
            raise ValueError("interrupted window template changed")
    return binding, previous


def _holds_native_lock(pid, path):
    wanted = path.stat()
    try:
        if sys.platform == "darwin":
            raise RecoveryDeferred(
                "native source-lock ownership is not observable on macOS; wait for the current consumer to exit"
            )
        for descriptor in (Path("/proc") / str(pid) / "fd").iterdir():
            try:
                actual = descriptor.stat()
                if (actual.st_dev, actual.st_ino) != (wanted.st_dev, wanted.st_ino):
                    continue
                info = (descriptor.parent.parent / "fdinfo" / descriptor.name).read_text()
                if any(
                    "FLOCK" in line and "WRITE" in line and str(pid) in line.split()
                    for line in info.splitlines()
                    if line.startswith("lock:")
                ):
                    return True
            except FileNotFoundError:
                continue
    except OSError as exc:
        raise RecoveryDeferred("native source-lock ownership is not observable") from exc
    return False


def _native_guard(stack, root, window, saved_state):
    """Only the verified same-window native consumer may coexist with staging."""
    record = _process_record(window, "native", saved_state)
    saved = record.get("identity")
    if not still_alive(saved):
        stack.enter_context(lock(root / "native-processing.lock"))
        stack.enter_context(lock(window / "native-process.lock"))
        return {"active": False}
    current = identity(saved["pid"])
    script = str(window / "process_sg_round3_native.py")
    try:
        command = (
            darwin_process.process_arguments(saved["pid"])[0].encode()
            if sys.platform == "darwin"
            else (Path("/proc") / str(saved["pid"]) / "cmdline").read_bytes()
        )
        argv = [part.decode() for part in command.split(b"\0") if part]
    except (OSError, UnicodeError) as exc:
        raise RecoveryDeferred("native identity changed during validation") from exc
    command_matches = current and (
        current["command_sha256"] == saved.get("command_sha256") or argv == record.get("argv")
    )
    if (
        not command_matches
        or script not in argv
        or not _holds_native_lock(saved["pid"], root / "native-processing.lock")
    ):
        raise RecoveryDeferred("native process is not the verified same-window source-lock owner")
    return {"active": True, "identity": current, "script_sha256": digest(Path(script))}


def _row_hash(row):
    return value_digest(
        [{"bytes_sha256": hashlib.sha256(v).hexdigest()} if isinstance(v, bytes) else v for v in row]
    )


def capture_guard(root, owner):
    """Snapshot only immutable data and completed pages; native progress is allowed."""
    from app.shared.infrastructure.operations.data_backup import (
        SUPPORTED_SCHEMA_VERSION,
    )

    result = {"documents": {}, "tables": {}}
    with closing(sqlite3.connect((root / "tea.sqlite3").as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        version = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        if version not in range(19, SUPPORTED_SCHEMA_VERSION + 1):
            raise ValueError("source schema must be explicitly upgraded before recovery")
        for row in conn.execute("SELECT * FROM documents ORDER BY id"):
            if len(result["documents"]) >= MAX_DOCUMENTS or row["owner_id"] != owner:
                raise ValueError("source owner/document bound mismatch")
            raw = dict(row)
            manifest = json.loads(raw.pop("manifest"))
            raw.pop("status")
            raw.pop("error")
            path = Path(row["storage_path"])
            if not path.is_absolute() or not path.is_relative_to(root):
                raise ValueError("source original path escaped its source")
            path = confined(root, path.relative_to(root))
            if path.stat().st_size != row["size"] or digest(path) != row["content_hash"]:
                raise ValueError("immutable source original changed")
            result["documents"][row["id"]] = {
                "identity_sha256": value_digest(raw),
                "source_url": row["source_url"],
                "sha256": row["content_hash"],
                "size": row["size"],
                "source_hash": manifest.get("source_hash"),
                "gazette_publication": manifest.get("gazette_publication"),
                "pages": {
                    str(page.get("page_number", page.get("page", index))): value_digest(page)
                    for index, page in enumerate(manifest.get("pages", []))
                    if page.get("status") in {"native", "ocr_complete"}
                },
            }
        for table in (
            "schema_migrations",
            "users",
            "sessions",
            "cases",
            "document_metadata",
            "document_metadata_history",
            "legal_classifications",
            "legal_classification_history",
            "legal_relations",
            "retrieval_vectors",
        ):
            result["tables"][table] = dict(
                Counter(_row_hash(row) for row in conn.execute(f'SELECT * FROM "{table}"'))
            )
    campaign = load(root / "campaign.json") if (root / "campaign.json").exists() else {}
    result["campaign_documents"] = {
        key: value_digest(entry) for key, entry in campaign.get("documents", {}).items()
    }
    return result


def assert_preserved(before, after):
    for key, old in before["documents"].items():
        new = after["documents"].get(key)
        if not new or any(old[field] != new[field] for field in old if field != "pages"):
            raise ValueError("existing original identity or source evidence changed")
        if any(new["pages"].get(page) != digest for page, digest in old["pages"].items()):
            raise ValueError("an already successful page changed during recovery")
    if any(
        after["campaign_documents"].get(key) != value for key, value in before["campaign_documents"].items()
    ):
        raise ValueError("existing campaign source-evidence mapping changed")
    for table, rows in before["tables"].items():
        if Counter(rows) - Counter(after["tables"][table]):
            raise ValueError(f"old protected rows changed: {table}")


def verify_sg_catalogue(root):
    plan = load(root / "plan.json")
    manifest = root / "frozen-catalogue-files.json"
    if digest(manifest) != plan["copied_catalogue_hashes_sha256"]:
        raise ValueError("frozen catalogue manifest changed")
    files = load(manifest)
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_RECEIPTS:
        raise ValueError("invalid frozen catalogue file bound")
    for relative, expected in files.items():
        if not str(relative).startswith("catalog/") or digest(confined(root, relative)) != expected:
            raise ValueError("frozen catalogue evidence changed")


def sg_inventory(root, module, rows):
    indexed = {row["download_url"]: row for row in rows}
    if len(indexed) != len(rows):
        raise ValueError("duplicate frozen original URL")
    paths = sorted((root / "originals").glob("*.json"))
    if len(paths) > min(MAX_RECEIPTS, len(rows)):
        raise ValueError("original receipt count exceeds the frozen selection")
    receipts, history, fingerprints, urls = [], {}, {}, set()
    for path in paths:
        receipt = load(path)
        url = module.original_url(receipt["source_url"])
        if url not in indexed or url in urls or path.stem != hashlib.sha256(url.encode()).hexdigest():
            raise ValueError("receipt does not uniquely belong to the frozen selection")
        if receipt["title"] != indexed[url]["title"] or receipt["status"] not in {
            "downloaded",
            "incomplete",
        }:
            raise ValueError("receipt title/status differs from the fixed scope")
        urls.add(url)
        for prior in module.original_attempts(root, receipt):
            prior_path = confined(root, prior["path"])
            if prior_path.stat().st_size != prior["size"] or digest(prior_path) != prior["sha256"]:
                raise ValueError("prior failure receipt changed")
            history[prior["path"]] = prior["sha256"]
        if receipt["status"] == "downloaded":
            raw = confined(root, receipt["path"])
            if raw.stat().st_size != receipt["size"] or digest(raw) != receipt["sha256"]:
                raise ValueError("downloaded original differs from its immutable receipt")
        fingerprints[path.name] = digest(path)
        receipts.append(receipt)
    return {"receipts": receipts, "files": fingerprints, "history": history}


def _journal_fingerprint(conn):
    result = {}
    for table in ("config", "tasks", "attempts"):
        hasher = hashlib.sha256()
        count = 0
        for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid'):
            hasher.update(_row_hash(row).encode())
            count += 1
        result[table] = {"rows": count, "sha256": hasher.hexdigest()}
    return result


def _sg_preflight(root, rows, guard, inventory):
    from app.contexts.ingestion.infrastructure.source_adapters.egazette import (
        SOURCE_ROLE,
        publication_classification,
    )

    indexed = {row["download_url"]: row for row in rows}
    receipts = {row["source_url"]: row for row in inventory["receipts"]}
    for old in guard["documents"].values():
        row = indexed.get(old["source_url"])
        if row is None:
            raise ValueError("existing original is outside the frozen selection")
        receipt = receipts.get(old["source_url"])
        if (
            not receipt
            or receipt["status"] != "downloaded"
            or receipt["sha256"] != old["sha256"]
            or receipt["size"] != old["size"]
        ):
            raise ValueError("existing original has no matching immutable download receipt")
        expected = {
            key: row[key]
            for key in (
                "title",
                "publishYear",
                "publishTimestamp",
                "subCategory",
                "notificationNum",
            )
        }
        expected |= {
            "source_role": SOURCE_ROLE,
            "current_effect_verified": False,
            "classification": publication_classification(row),
        }
        if old["source_hash"] != old["sha256"] or old["gazette_publication"] != expected:
            # A partly created document requires explicit review, not overwriting evidence.
            raise ValueError("existing source provenance differs from the frozen catalogue")
    with closing(sqlite3.connect((root / "tea.sqlite3").as_uri() + "?mode=ro", uri=True)) as conn:
        for (raw,) in conn.execute("SELECT payload FROM document_metadata"):
            if json.loads(raw).get("review_status") != "draft":
                raise ValueError("recovery cannot restage reviewed source metadata")


@offline_only
def recover_interrupted_window(*, source, window, project, data, bundle, control, plan, modules, saved_state):
    """Reconcile exactly one stopped window; return None when already finalized.

    All writes are local. RecoveryDeferred means observe/wait without launching
    another window. Any integrity error requires attention and preserves receipts.
    """
    if (
        source not in {"id", "sg"}
        or window.parent != confined(control, source)
        or not WINDOW.fullmatch(window.name)
        or window.is_symlink()
        or not window.is_dir()
    ):
        raise ValueError("invalid source window")
    if (window / "final-report.json").exists():
        return None
    validate_bindings(project, data, bundle, plan)
    root = confined(data, plan["source_dirs"][source])
    binding, _ = _validate_window(source, window, root, bundle, plan)
    record = _process_record(window, "collector", saved_state)
    if still_alive(record.get("identity")):
        raise RecoveryDeferred("collector identity is still alive; observation timeout cannot restart it")
    if shutil.disk_usage(root).free < plan["min_free_bytes"] + 128 * 1024**2:
        raise RecoveryDeferred("recovery paused at the original disk reserve")
    with ExitStack() as stack:
        stack.enter_context(lock(window / "collector-process.lock"))
        stack.enter_context(lock(root / ("collector.lock" if source == "id" else "continuation.lock")))
        if still_alive(record.get("identity")):
            raise RecoveryDeferred("collector is still alive")
        native = _native_guard(stack, root, window, saved_state) if source == "sg" else {"active": False}
        evidence = confined(window, "interrupted-recovery")
        evidence.mkdir(mode=0o700, exist_ok=True)
        stack.enter_context(lock(evidence / "recovery.lock"))
        module = modules[source]
        expected = project / (
            "scripts/collect_egazette.py" if source == "sg" else "scripts/collect_indonesia_official.py"
        )
        if Path(module.__file__).resolve() != expected.resolve():
            raise ValueError("collector module belongs to another checkout")
        if module.SOURCE_OWNER != plan["source_owners"][source]:
            raise ValueError("collector principal differs from the frozen source owner")
        terminal = root / ("continuation-report.json" if source == "sg" else "report.json")
        if source == "sg" and terminal.exists():
            saved_terminal = load(terminal)
            if (
                saved_terminal.get("controlled_window") != binding
                and digest(terminal) != binding["previous_final_report_sha256"]
            ):
                raise ValueError("a terminal report from another window appeared")
        current = capture_guard(root, plan["source_owners"][source])
        rows = load(root / "selected-records.json") if source == "sg" else None
        if source == "sg":
            verify_sg_catalogue(root)
            if len(rows) != plan["selected_remaining_initial"]["sg"]:
                raise ValueError("frozen selection count changed")
            inventory = sg_inventory(root, module, rows)
            _sg_preflight(root, rows, current, inventory)
        else:
            conn = stack.enter_context(
                closing(sqlite3.connect((root / "journal.sqlite3").as_uri() + "?mode=ro", uri=True))
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            definition = conn.execute("SELECT value FROM config WHERE key='definition_hash'").fetchone()
            if not definition or definition[0] != load(window / "previous-report.json").get("plan_sha256"):
                raise ValueError("source journal belongs to a different immutable definition")
            module.verify_receipts(conn, root)
            inventory = _journal_fingerprint(conn)
        baseline_path = evidence / "baseline.json"
        if baseline_path.exists():
            baseline = load(baseline_path)
            if baseline["window_plan_sha256"] != value_digest(binding) or baseline["inventory"] != inventory:
                raise ValueError("recovery inputs changed since the first attempt")
            assert_preserved(baseline["guard"], current)
        else:
            baseline = {
                "window_plan_sha256": value_digest(binding),
                "inventory": inventory,
                "guard": current,
            }
            for name in (
                "campaign.json",
                "live-progress.json",
                "continuation-report.json",
                "report.json",
            ):
                path = root / name
                if path.exists() and not (evidence / ("before-" + name)).exists():
                    write(evidence / ("before-" + name), load(path), exclusive=True)
            write(baseline_path, baseline, exclusive=True)
        # The supervisor process strips model configuration and installs an audit
        # hook before calling this helper. Staging itself uses no HTTP or worker.
        if any(
            os.environ.get(prefix + suffix)
            for prefix in ("LLM", "VISION", "EMBEDDING", "RERANK")
            for suffix in ("_API_KEY", "_BASE_URL", "_MODEL")
        ):
            raise ValueError("offline recovery requires model-free environment")
        if os.environ.get("VISION_OCR_ENABLED", "false").lower() != "false":
            raise ValueError("offline recovery requires OCR disabled")
        if source == "sg":
            from app.contexts.ingestion.infrastructure.source_adapters.egazette import (
                EGAZETTE_ORIGIN,
                public_search_from_html,
            )

            request, proof = public_search_from_html(EGAZETTE_ORIGIN, (root / "frontend.html").read_bytes())
            write(evidence / "public-config-proof.json", proof)
            runner = module.GazetteCollector(
                root,
                request,
                timeout=35,
                time_budget=0,
                page_size=100,
                min_free_bytes=plan["min_free_bytes"],
            )
            stage = runner.stage_originals(rows)
            if runner.hops or stage["HTTP_calls"] or stage["model_calls"]:
                raise ValueError("offline stage unexpectedly called an external service")
            if sg_inventory(root, module, rows) != inventory:
                raise ValueError("recovery changed original download receipts/history")
            counts = dict(Counter(row["status"] for row in inventory["receipts"]))
            progress = (
                load(evidence / "before-live-progress.json")
                if (evidence / "before-live-progress.json").exists()
                else {}
            )
            observed = progress.get("elapsed_seconds", binding["previous_elapsed_seconds_total"])
            if type(observed) not in (int, float) or not math.isfinite(observed) or observed < 0:
                raise ValueError("invalid observed elapsed time")
            report = {
                "selected": len(rows),
                "status_counts": counts,
                "remaining_unattempted": len(rows) - len(inventory["receipts"]),
                "elapsed_seconds": max(0, observed - binding["previous_elapsed_seconds_total"]),
                "elapsed_seconds_kind": "observed_lower_bound_before_interrupt",
                "elapsed_seconds_total": max(observed, binding["effective_total_budget_seconds"]),
                "elapsed_seconds_total_kind": "conservative_budget_charge_not_measured_runtime",
                "retained_prior_failure_receipts": len(inventory["history"]),
                "controlled_window": binding,
                "scope_frozen": True,
                "current_effect_verified": False,
                "model_calls": 0,
            }
        else:
            # A crash can leave the exporter's exclusive temporary report.
            # Preserve its exact bytes before permitting the normal atomic export.
            for name in ("report.json.tmp", "ledger.jsonl.tmp", "campaign.json.tmp"):
                path = root / name
                if path.exists():
                    if path.is_symlink() or not path.is_file():
                        raise ValueError("invalid interrupted export temporary file")
                    target = evidence / (name + "." + digest(path))
                    if target.exists():
                        if digest(target) != digest(path):
                            raise ValueError("interrupted export evidence changed")
                        path.unlink()
                    else:
                        path.replace(target)
            report = module.export_state(conn, root)
            stage = module.prepare_library(root, min_free_bytes=plan["min_free_bytes"])
            with closing(
                sqlite3.connect((root / "journal.sqlite3").as_uri() + "?mode=ro", uri=True)
            ) as current_journal:
                if _journal_fingerprint(current_journal) != inventory:
                    raise ValueError("recovery changed journal attempts or durable backoff")
            report["requested_scope"] = {
                "complete": False,
                "reason": "interrupted_window_requires_normal_scope_evaluation",
            }
            report["recovery_requires_scope_evaluation"] = True
        after = capture_guard(root, plan["source_owners"][source])
        assert_preserved(baseline["guard"], after)
        if source == "sg":
            originals = {
                doc["source_url"]: (doc["sha256"], doc["size"]) for doc in after["documents"].values()
            }
            expected = {
                row["source_url"]: (row["sha256"], row["size"])
                for row in inventory["receipts"]
                if row["status"] == "downloaded"
            }
            if len(originals) != len(after["documents"]) or originals != expected:
                raise ValueError("saved original receipts did not map exactly once to source documents")
        details = {
            "status": "interrupted_reconciled",
            "normal_collector_completion": False,
            "process_disappearance_cause": "unproven",
            "baseline_sha256": digest(baseline_path),
            "window_plan_sha256": value_digest(binding),
            "new_documents": len(after["documents"]) - len(baseline["guard"]["documents"]),
            "native": native,
            "stage": stage,
            "network_requests": 0,
            "model_calls": 0,
            "source_scope_complete": False,
            "reconciled_at": datetime.now(UTC).isoformat(),
        }
        report["recovery"] = details
        write(evidence / "after.json", {"guard": after, "result": details})
        # Publish the terminal report before final-report. A crash between either
        # write is safely replayed against the same immutable baseline/receipts.
        write(
            root / ("continuation-report.json" if source == "sg" else "report.json"),
            report,
        )
        write(window / "final-report.json", report, exclusive=True)
        return details
