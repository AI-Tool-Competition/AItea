"""Explicit, offline instance adoption and portable backup/restore.

No service is started, no model is called, and existing destinations are never
overwritten. Database and content verification reuse the data-backup protocol.
"""

import fcntl
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

from dotenv import dotenv_values, set_key

from app.configuration.paths import InstancePaths
from app.shared.infrastructure import darwin_process
from app.contexts.documents.infrastructure.providers.embedding_admission import release_restored_leases
from app.shared.domain.values import now, uid
from app.shared.infrastructure.operations.data_backup import (
    DataBackupError,
    create_backup,
    restore_backup,
    verify_backup,
)

RAGFLOW_CONFIGURATION = (
    ".env",
    "conf",
    "keys-created",
    "service_conf.yaml.template",
    "infinity.toml",
    "kvrocks.conf",
    "nginx.conf",
    "api-start.sh",
    "embedding-profile.sha256",
)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uid() + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _alive(record):
    if not isinstance(record, dict) or not record.get("pid"):
        return False
    try:
        if sys.platform == "darwin":
            start = darwin_process.process_start(int(record["pid"]))
            return start is not None and start == str(record.get("start", record.get("start_ticks")))
        fields = Path(f"/proc/{int(record['pid'])}/stat").read_text().rsplit(")", 1)[1].split()
        return fields[0] != "Z" and fields[19] == str(record.get("start"))
    except (OSError, ValueError, IndexError):
        return False


def require_stopped(paths):
    """Refuse managed writers, active collector locks and open instance files."""
    state_files = [
        paths.project / name
        for name in (
            ".dev/dev.json",
            ".dev/startup.json",
            ".dev/ragflow/processes.json",
            ".dev/collection-service/state/state.json",
        )
    ] + [paths.collection_state / "state.json"]
    for file in state_files:
        if not file.is_file():
            continue
        state = json.loads(file.read_text())

        def running(value):
            if isinstance(value, dict):
                return _alive(value) or any(running(v) for v in value.values())
            return isinstance(value, list) and any(running(v) for v in value)

        if running(state):
            raise DataBackupError("instance_services_must_be_stopped")
    for directory in (paths.data / "official-updates").glob("*"):
        for name in (".collector.lock", "collector-process.lock"):
            lock = directory / name
            if lock.is_file():
                with lock.open("rb") as stream:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise DataBackupError("instance_collectors_must_be_stopped") from None
    # This also catches independently launched local database/provider writers.
    if sys.platform == "darwin":
        try:
            for pid, name in darwin_process.open_files():
                if pid != os.getpid() and Path(name).is_relative_to(paths.data):
                    raise DataBackupError("instance_files_are_open")
        except DataBackupError:
            raise
        except (OSError, ValueError) as error:
            raise DataBackupError("instance_file_ownership_unavailable") from error
        return
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            if process.stat().st_uid != os.getuid():
                continue
            for fd in (process / "fd").iterdir():
                target = fd.resolve()
                if target.is_relative_to(paths.data):
                    raise DataBackupError("instance_files_are_open")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue


def adopt_instance(paths):
    """Relocate only known private config/control files, preserving data paths."""
    require_stopped(paths)
    legacy_env = paths.project / ".env"
    moves = [(legacy_env, paths.private_config / "application.env")]
    moves += [(paths.ragflow_runtime / name, paths.ragflow_config / name) for name in RAGFLOW_CONFIGURATION]
    for suffix in ("", "-wal", "-shm"):
        moves.append(
            (
                paths.project / (".dev/embedding-admission.sqlite3" + suffix),
                Path(str(paths.embedding_admission) + suffix),
            )
        )
    moves.append((paths.project / ".dev/collection-service/fleet.json", paths.collection_fleet))
    moves.extend(
        [
            (paths.project / ".dev/collection-service/state", paths.collection_state),
            (paths.project / ".dev/collection-watchdog", paths.collection_registry),
        ]
    )
    moves = [(source, target) for source, target in moves if source.exists() or source.is_symlink()]
    for source, target in moves:
        if source.is_symlink() or target.exists() or target.is_symlink():
            raise DataBackupError("instance_adoption_conflict")
    completed = []
    bindings = []
    try:
        for source, target in moves:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source.rename(target)
            completed.append((source, target))
        # The fleet content remains frozen; only its generated unit needs a new
        # --fleet argument. Keep the old unit as rollback evidence.
        unit = paths.project / ".dev/collection-service/aitea-collection.service"
        if unit.is_file() and any(
            target in {paths.collection_fleet, paths.collection_state} for _, target in moves
        ):
            saved = paths.collection_fleet.parent / ("previous-unit-" + uid() + ".service")
            unit.rename(saved)
            completed.append((unit, saved))
        for binding in paths.collection_registry.glob("*.json"):
            raw = binding.read_bytes()
            value = json.loads(raw)
            if value.get("state_dir") == str(paths.project / ".dev/collection-service/state"):
                bindings.append((binding, raw))
                value["state_dir"] = str(paths.collection_state)
                _write(binding, value)
        if completed:
            _write(
                paths.private_config / "migrations" / (uid() + ".json"),
                {
                    "operation": "adopt-instance",
                    "at": now(),
                    "moves": [{"from": str(a), "to": str(b)} for a, b in completed],
                    "database_moved": False,
                },
            )
    except BaseException:
        for binding, raw in bindings:
            binding.write_bytes(raw)
        for source, target in reversed(completed):
            target.rename(source)
        raise
    return {"status": "adopted", "moved": len(completed), "data_dir": str(paths.data)}


def backup_instance(paths, destination):
    require_stopped(paths)
    if (
        paths.env_file != paths.private_config / "application.env"
        or any((paths.ragflow_runtime / name).exists() for name in RAGFLOW_CONFIGURATION)
        or (paths.project / ".dev/collection-service/state").exists()
    ):
        raise DataBackupError("adopt_private_configuration_before_instance_backup")
    return create_backup(
        paths.data, destination, configuration_dir=paths.catalogs, project_root=paths.project
    )


def _fleet_digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def _relocate_fleet(paths, source, old_project, old_catalogs):
    if not paths.collection_fleet.exists():
        return
    original = paths.collection_fleet.read_bytes()
    fleet = json.loads(original)
    previous_hash = fleet.pop("config_hash")
    if _fleet_digest(fleet) != previous_hash or fleet.get("project_root") != str(old_project):
        raise DataBackupError("collector_fleet_binding_mismatch")

    def relocate(value):
        if not isinstance(value, str) or not Path(value).is_absolute():
            return value
        path = Path(value)
        for old, new in (
            (old_catalogs, paths.catalogs),
            (source, paths.data),
            (old_project, paths.project),
        ):
            if path.is_relative_to(old):
                return str(new / path.relative_to(old))
        return value

    fleet["project_root"] = str(paths.project)
    for task in fleet["tasks"]:
        task["command"] = [relocate(argument) for argument in task["command"]]
        if Path(task["command"][0]).name == "uv":
            executable = shutil.which("uv")
            if not executable:
                raise DataBackupError("uv_required_for_restored_collector")
            task["command"][0] = str(Path(executable).resolve())
        task["cwd"] = relocate(task["cwd"])
    fleet["config_hash"] = _fleet_digest(fleet)
    archive = paths.collection_fleet.with_name("fleet-origin-" + uid() + ".json")
    archive.write_bytes(original)
    archive.chmod(0o600)
    _write(paths.collection_fleet, fleet)
    state_file = paths.collection_state / "state.json"
    if state_file.exists():
        state = json.loads(state_file.read_text())
        if state["config_hash"] != previous_hash:
            raise DataBackupError("collector_supervisor_binding_mismatch")
        state.update(
            project_root=str(paths.project),
            config_hash=fleet["config_hash"],
            watchdog_identity=None,
            status="stopped",
        )
        for task in state["tasks"].values():
            task["identity"] = None
            task.pop("launch_token", None)
            task.pop("alive_observed_monotonic", None)
            if task["status"] in {"running", "starting", "draining", "blocked_owned_child"}:
                task["status"] = "cooldown"
        _write(state_file, state)
        for process_file in paths.collection_state.glob("tasks/*/collector-process.json"):
            process_file.rename(process_file.with_name("collector-process-origin-" + uid() + ".json"))
    _write(
        paths.collection_registry / (fleet["config_hash"] + ".json"),
        {
            "protocol": fleet["protocol"],
            "config_hash": fleet["config_hash"],
            "state_dir": str(paths.collection_state),
        },
    )


def restore_instance(backup, target, *, project):
    """Restore content and rebind only mutable operational paths, never receipts."""
    backup, target, project = Path(backup).resolve(), Path(target).resolve(), Path(project).resolve()
    verify_backup(backup)
    manifest = json.loads((backup / "manifest.json").read_text())
    if not manifest.get("configuration_included") or not manifest.get("instance_project_root"):
        raise DataBackupError("complete_instance_backup_required")
    if target == project or not target.is_relative_to(project):
        raise DataBackupError("instance_target_must_be_a_new_project_subdirectory")
    source = Path(manifest["source_data_dir"])
    old_project = Path(manifest["instance_project_root"])
    old_catalogs = Path(manifest.get("source_configuration_dir") or old_project / "config")
    result = restore_backup(backup, target)
    try:
        changed = []
        for state_file in (target / "official-updates").glob("*/state.json"):
            state = json.loads(state_file.read_text())
            old_root = Path(state["binding"]["root"])
            expected = source / state_file.parent.relative_to(target)
            if old_root != expected:
                raise DataBackupError("collector_root_binding_mismatch")
            state["binding"]["root"] = str(state_file.parent)
            _write(state_file, state)
            changed.append(state_file.relative_to(target).as_posix())
        paths = InstancePaths.resolve(
            project,
            environ={"TEA_DATA_DIR": str(target), "TEA_ENV_FILE": str(target / "config/application.env")},
        )
        released_leases = (
            release_restored_leases(paths.embedding_admission) if paths.embedding_admission.exists() else 0
        )
        _relocate_fleet(paths, source, old_project, old_catalogs)
        if paths.env_file.exists():
            values = dotenv_values(paths.env_file, interpolate=False)
            for key in ("TEA_TEMPLATE_FILE", "EMBEDDING_ADMISSION_PATH", "AITEA_RAGFLOW_TOOLS"):
                value = values.get(key)
                if not value or not Path(value).is_absolute():
                    continue
                original = Path(value)
                if original.is_relative_to(old_catalogs):
                    replacement = paths.catalogs / original.relative_to(old_catalogs)
                elif original.is_relative_to(source):
                    replacement = target / original.relative_to(source)
                elif original.is_relative_to(old_project):
                    replacement = project / original.relative_to(old_project)
                else:
                    raise DataBackupError("external_configuration_path_requires_review")
                set_key(str(paths.env_file), key, str(replacement))
        receipt = {
            "operation": "restore-instance",
            "at": now(),
            "manifest_sha256": hashlib.sha256((backup / "manifest.json").read_bytes()).hexdigest(),
            "source_data_dir": str(source),
            "target_data_dir": str(target),
            "collector_states_rebound": changed,
            "embedding_leases_released": released_leases,
            "services_started": False,
        }
        _write(target / "config/migrations" / (uid() + ".json"), receipt)
        return result | {
            "configuration_included": True,
            "collector_states_rebound": len(changed),
            "embedding_leases_released": released_leases,
            "services_started": False,
        }
    except BaseException:
        # This directory was just created exclusively by restore_backup. It
        # never existed before this operation and has not been handed to callers.
        shutil.rmtree(target)
        raise
