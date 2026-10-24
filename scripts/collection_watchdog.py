#!/usr/bin/env python3
"""Supervise explicitly configured local collection commands without changing their ledgers.

fleet.json contains protocol, project_root, tasks and config_hash. config_hash is
SHA256 of canonical JSON excluding config_hash. Each task has exactly id, command,
cwd, enabled, restart_seconds and max_restart_seconds. Commands run as argv,
never through a shell. State and child ownership survive watchdog restarts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.configuration.paths import InstancePaths  # noqa: E402
from app.configuration.environment import read_environment  # noqa: E402

from official_source_supervisor_recovery import (
    RecoveryDeferred,
    identity,
    load,
    lock,
    spawn_owned,
    value_digest,
    write,
)

PROTOCOL = "aitea-collection-fleet-v1"
STOP = threading.Event()
MAX_LOG_BYTES = 2 * 1024**2
LOG_BACKUPS = 3
TASK_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


def local_path(value, root=None, *, exists=True):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("absolute local paths without '..' are required")
    resolved = path.resolve(strict=exists)
    if root is not None and not resolved.is_relative_to(root):
        raise ValueError("collection paths must remain inside project_root")
    return resolved


def command_hash(command):
    return hashlib.sha256(b"\0".join(os.fsencode(part) for part in command) + b"\0").hexdigest()


def validate_command(command, project):
    if not isinstance(command, list) or not 2 <= len(command) <= 128:
        raise ValueError("a bounded explicit argv command is required")
    if any(not isinstance(item, str) or not item or "\0" in item or "\n" in item for item in command):
        raise ValueError("invalid command argument")
    executable = local_path(command[0])
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ValueError("command executable must be an executable local file")
    name = executable.name
    if name == "uv":
        if len(command) < 7 or command[1:3] != ["run", "--project"]:
            raise ValueError("uv must run the declared project backend")
        if local_path(command[3], project) != project / "backend":
            raise ValueError("uv commands require the declared backend")
        index, flags = 4, []
        while index < len(command) and command[index].startswith("--"):
            flags.append(command[index])
            index += 1
        if (
            "--no-sync" not in flags
            or len(flags) != len(set(flags))
            or not set(flags) <= {"--no-sync", "--offline"}
            or index + 1 >= len(command)
            or command[index] != "python"
        ):
            raise ValueError("uv commands require --no-sync python; only --offline is optional")
        script_index = index + 1
    elif re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", name):
        allowed = {Path(sys.executable).resolve()}
        backend_python = project / "backend/.venv/bin/python"
        if backend_python.exists():
            allowed.add(backend_python.resolve())
        if executable not in allowed:
            raise ValueError("python must be the current runtime or project backend runtime")
        script_index = 1
    else:
        raise ValueError("only uv or the project Python runtime may launch collection")
    script = local_path(command[script_index], project)
    if script.suffix != ".py" or not script.is_file():
        raise ValueError("collection entry must be an explicit project Python file")
    for argument in command[script_index + 1 :]:
        path_argument = (
            argument.split("=", 1)[1] if argument.startswith("--") and "=" in argument else argument
        )
        if path_argument.startswith("/"):
            local_path(path_argument, project, exists=False)
    return list(command)


def read_config(path):
    config = load(path)
    if set(config) != {"protocol", "project_root", "tasks", "config_hash"} or config["protocol"] != PROTOCOL:
        raise ValueError("invalid collection fleet contract")
    if config["config_hash"] != value_digest(
        {key: value for key, value in config.items() if key != "config_hash"}
    ):
        raise ValueError("fleet config_hash mismatch; explicit local configuration review required")
    project = local_path(config["project_root"])
    local_path(path, project)
    if not (project / "backend/pyproject.toml").is_file() or not (project / "scripts").is_dir():
        raise ValueError("project_root must identify an AItea project")
    tasks = config["tasks"]
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 64:
        raise ValueError("fleet must contain 1 to 64 explicit tasks")
    seen = set()
    for task in tasks:
        if set(task) != {
            "id",
            "command",
            "cwd",
            "enabled",
            "restart_seconds",
            "max_restart_seconds",
        }:
            raise ValueError("unknown task fields")
        if not isinstance(task["id"], str) or not TASK_ID.fullmatch(task["id"]) or task["id"] in seen:
            raise ValueError("task ids must be unique bounded local identifiers")
        seen.add(task["id"])
        if type(task["enabled"]) is not bool:
            raise ValueError("enabled must be boolean")
        for field in ("restart_seconds", "max_restart_seconds"):
            if (
                type(task[field]) not in (int, float)
                or not math.isfinite(task[field])
                or not 0.05 <= task[field] <= 86400
            ):
                raise ValueError("restart delays must be finite and bounded")
        if task["restart_seconds"] > task["max_restart_seconds"]:
            raise ValueError("max_restart_seconds must cover restart_seconds")
        local_path(task["cwd"], project)
        validate_command(task["command"], project)
    return config


def strict_alive(saved):
    if not saved:
        return False
    current = identity(saved["pid"])
    return bool(current and current == saved)


def lock_busy(directory):
    try:
        with lock(directory / "collector-process.lock"):
            return False
    except RecoveryDeferred:
        return True


def find_owned(task, state, project):
    """Recover a launch gap or a surviving uv child from durable launch credentials."""
    token = state.get("launch_token")
    if not token:
        return None
    expected = {
        b"AITEA_COLLECTION_LAUNCH_TOKEN": token.encode(),
        b"AITEA_COLLECTION_TASK": task["id"].encode(),
        b"AITEA_COLLECTION_PROJECT": str(project).encode(),
    }
    command = task["command"]
    launcher_hash = command_hash(command)
    uv = Path(command[0]).resolve().name == "uv"
    script_args = command[command.index("python", 4) + 1 :] if uv else []
    allowed_python = {Path(sys.executable).resolve()}
    backend_python = project / "backend/.venv/bin/python"
    if backend_python.exists():
        allowed_python.add(backend_python.resolve())
    launchers, descendants = [], []
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            env = dict(
                part.split(b"=", 1) for part in (proc / "environ").read_bytes().split(b"\0") if b"=" in part
            )
            current = identity(int(proc.name))
            if not current or not all(env.get(key) == value for key, value in expected.items()):
                continue
            if current["command_sha256"] == launcher_hash:
                launchers.append(current)
            elif uv:
                argv = [os.fsdecode(part) for part in (proc / "cmdline").read_bytes().split(b"\0") if part]
                if (
                    argv
                    and argv[1:] == script_args
                    and current["command_sha256"] == command_hash(argv)
                    and Path(argv[0]).resolve() in allowed_python
                ):
                    descendants.append(current)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    matches = launchers or descendants
    if len(matches) > 1:
        raise RecoveryDeferred("multiple processes match one launch intent; preserve their evidence")
    return matches[0] if matches else None


class RotatingOutput:
    """Keep a stable output inode so a killed watchdog cannot break child stdout.

    Only diagnostic logs use lossy copy/truncate rotation. Archives contain at
    most MAX_LOG_BYTES each. The active log is checked each supervision poll;
    its temporary overshoot is the output produced since the previous check.
    During watchdog downtime output stays writable and is trimmed on recovery.
    Request/receipt journals are never opened or truncated here.
    """

    def __init__(self, path):
        self.path = path

    def open(self):
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise ValueError("watchdog diagnostic output must be a regular file")
        return os.fdopen(fd, "a+b", buffering=0)

    def rotate(self):
        with self.open() as stream:
            size = os.fstat(stream.fileno()).st_size
            if size <= MAX_LOG_BYTES:
                return
            # Clip instead of copying an arbitrarily large file after downtime.
            stream.seek(size - MAX_LOG_BYTES)
            tail = stream.read(MAX_LOG_BYTES)
            self.path.with_name(self.path.name + f".{LOG_BACKUPS}").unlink(missing_ok=True)
            for number in range(LOG_BACKUPS - 1, 0, -1):
                previous = self.path.with_name(self.path.name + f".{number}")
                if previous.exists():
                    previous.replace(self.path.with_name(self.path.name + f".{number + 1}"))
            archive = self.path.with_name(self.path.name + ".1")
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as output:
                output.write(tail)
                output.flush()
                os.fsync(output.fileno())
            # Preserve the inode and inherited child fd, including across a
            # watchdog SIGKILL. O_APPEND writers continue at the new end.
            os.ftruncate(stream.fileno(), 0)


def model_free_environment(project, task, token):
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LLM_", "VISION_", "EMBEDDING_", "RERANK_", "MINERU_", "RAGFLOW_"))
    }
    for prefix in ("LLM", "VISION", "EMBEDDING", "RERANK"):
        for field in ("BASE_URL", "API_KEY", "MODEL"):
            env[f"{prefix}_{field}"] = ""
    for key in (
        "MINERU_API_TOKEN",
        "RAGFLOW_BASE_URL",
        "RAGFLOW_API_KEY",
        "RAGFLOW_EMBEDDING_MODEL",
        "RAGFLOW_RERANK_MODEL",
    ):
        env[key] = ""
    proxy_config = {}
    values = read_environment(project)
    for key in (
        "COLLECTION_PROXY_URL",
        "COLLECTION_PROXY_HOSTS",
        "COLLECTION_DOH_HOSTS",
    ):
        if key in values:
            proxy_config[key] = values[key]
    for key in (
        "COLLECTION_PROXY_URL",
        "COLLECTION_PROXY_HOSTS",
        "COLLECTION_DOH_HOSTS",
    ):
        if key in os.environ:
            proxy_config[key] = os.environ[key]
    pool_path = InstancePaths.resolve(project).source_pool
    if pool_path.is_file() and pool_path.stat().st_size <= 2 * 1024**2:
        pool = json.loads(pool_path.read_text(encoding="utf-8"))
        if pool.get("protocol") == "aitea-official-source-pool-v2":
            proxy_hosts = {
                host
                for source in pool.get("sources", [])
                if source.get("status") == "active" and source.get("transport") == "proxy"
                for host in source.get("allowed_hosts", [])
            }
            if len(proxy_hosts) > 64:
                raise ValueError("Active collection proxy host scope exceeds 64 exact hosts")
            configured = {
                host.strip().lower()
                for host in proxy_config.get("COLLECTION_PROXY_HOSTS", "").split(",")
                if host.strip()
            }
            configured |= proxy_hosts
            if len(configured) > 64:
                raise ValueError("Collection proxy host scope exceeds 64 exact hosts")
            proxy_config["COLLECTION_PROXY_HOSTS"] = ",".join(sorted(configured))
    env.update(proxy_config)
    env.update(
        VISION_OCR_ENABLED="false",
        TEA_SHARED_LIBRARY_WORKERS="external",
        PYTHONDONTWRITEBYTECODE="1",
        AITEA_COLLECTION_PROJECT=str(project),
        AITEA_COLLECTION_TASK=task["id"],
        AITEA_COLLECTION_LAUNCH_TOKEN=token,
    )
    return env


def new_task_state():
    return {
        "status": "idle",
        "failures": 0,
        "launches": 0,
        "next_at": 0,
        "identity": None,
    }


def record_exit(state, task, now, returncode=None, *, globally_draining=False):
    # Time spent waiting to observe an exit is not evidence of a healthy run.
    # Only an identity-verified, continuously observed run may reset backoff.
    failures = 1 if state.get("stable_run", False) else state["failures"] + 1
    delay = min(
        task["max_restart_seconds"],
        task["restart_seconds"] * 2 ** min(failures - 1, 30),
    )
    state.update(
        status="paused" if returncode == 75 and not globally_draining else "cooldown",
        failures=failures,
        next_at=now + delay,
        identity=None,
        last_exit_at=now,
        last_returncode=returncode,
        restart_delay=delay,
    )


def run(config_path, state_dir, *, poll_seconds=0.2):
    config_path = local_path(config_path)
    config = read_config(config_path)
    project = Path(config["project_root"]).resolve()
    state_dir = local_path(state_dir, project, exists=False)
    registry = local_path(InstancePaths.resolve(project).collection_registry, project, exists=False)
    registry.mkdir(mode=0o700, parents=True, exist_ok=True)
    # One reviewed fleet cannot accidentally move its state directory after a
    # crash and bypass the inherited locks of children in its original location.
    with lock(registry / f"{config['config_hash']}.lock"):
        binding_path = registry / f"{config['config_hash']}.json"
        binding = {
            "protocol": PROTOCOL,
            "config_hash": config["config_hash"],
            "state_dir": str(state_dir),
        }
        if binding_path.exists():
            if load(binding_path) != binding:
                raise ValueError("fleet is already bound to another state directory")
        else:
            write(binding_path, binding, exclusive=True)
        return _run(config_path, state_dir, poll_seconds=poll_seconds)


def _run(config_path, state_dir, *, poll_seconds):
    config_path = local_path(config_path)
    config = read_config(config_path)
    project = Path(config["project_root"]).resolve()
    state_dir = local_path(state_dir, project, exists=False)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with lock(state_dir / "watchdog.lock"):
        state_path = state_dir / "state.json"
        if state_path.exists():
            state = load(state_path)
            if state.get("protocol") != PROTOCOL or state.get("config_hash") != config["config_hash"]:
                raise ValueError("existing fleet state belongs to another immutable configuration")
        else:
            state = {
                "protocol": PROTOCOL,
                "config_hash": config["config_hash"],
                "project_root": str(project),
                "tasks": {task["id"]: new_task_state() for task in config["tasks"]},
            }
        children, signalled = {}, set()
        state.update(status="running", watchdog_identity=identity(os.getpid()))
        write(state_path, state)
        last_digest = value_digest(state)
        last_write = time.monotonic()
        while True:
            draining = STOP.is_set() or (state_dir / "STOP").exists()
            state["status"] = "draining" if draining else "running"
            active = False
            for task in config["tasks"]:
                task_id = task["id"]
                directory = local_path(state_dir / "tasks" / task_id, project, exists=False)
                directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                output = RotatingOutput(directory / "watchdog-output.log")
                output.rotate()
                saved = state["tasks"][task_id]
                child = children.get(task_id)
                if child is not None:
                    child.poll()  # Reap exited direct children before /proc identity checks.
                alive = strict_alive(saved.get("identity"))
                busy = lock_busy(directory)
                if not alive and (
                    busy or saved["status"] in {"running", "starting", "draining", "blocked_owned_child"}
                ):
                    # A reaped direct Python child with a free inherited lock
                    # cannot need PID-gap recovery. uv may leave its Python
                    # subprocess alive, so its descendants must still be checked.
                    known_exited = child is not None and child.returncode is not None
                    uv_launcher = Path(task["command"][0]).resolve().name == "uv"
                    recovered = (
                        find_owned(task, saved, project) if busy or not known_exited or uv_launcher else None
                    )
                    if recovered:
                        saved.update(identity=recovered, status="running", adopted_at=time.time())
                        alive = True
                    elif busy:
                        saved["status"] = "blocked_owned_child"
                        active = True
                        continue
                if alive:
                    active = True
                    observed = saved.setdefault("alive_observed_monotonic", time.monotonic())
                    if time.monotonic() - observed >= task["max_restart_seconds"]:
                        saved["stable_run"] = True
                    saved["status"] = "draining" if draining or not task["enabled"] else "running"
                    # Re-read the complete identity immediately before sending a signal.
                    if (
                        (draining or not task["enabled"])
                        and task_id not in signalled
                        and strict_alive(saved["identity"])
                    ):
                        os.kill(saved["identity"]["pid"], signal.SIGTERM)
                        signalled.add(task_id)
                    continue
                if saved["status"] in {
                    "running",
                    "starting",
                    "draining",
                    "blocked_owned_child",
                }:
                    record_exit(
                        saved,
                        task,
                        time.time(),
                        child.returncode if child is not None else None,
                        globally_draining=draining,
                    )
                if draining or not task["enabled"]:
                    if saved["status"] != "paused":
                        saved["status"] = "stopped" if draining else "disabled"
                    continue
                if saved["status"] == "paused":
                    continue
                if saved["next_at"] > time.time():
                    continue
                # Detect edited local configuration before every new task launch.
                if read_config(config_path) != config:
                    raise ValueError("fleet configuration changed during supervision")
                import uuid

                token = uuid.uuid4().hex
                saved.update(
                    status="starting",
                    launch_token=token,
                    identity=None,
                    started_at=time.time(),
                    stable_run=False,
                )
                saved.pop("alive_observed_monotonic", None)
                saved["launches"] += 1
                write(state_path, state)  # Durable intent precedes Popen.
                with output.open() as writer:
                    child = spawn_owned(
                        task["command"],
                        window=directory,
                        kind="collector",
                        cwd=task["cwd"],
                        env=model_free_environment(project, task, token),
                        stdout=writer,
                    )
                children[task_id] = child
                saved.update(status="running", identity=identity(child.pid))
                write(state_path, state)
                active = True
            if draining and not active:
                state["status"] = "stopped"
                state["watchdog_identity"] = None
                write(state_path, state)
                return 75 if (state_dir / "STOP").exists() else 0
            comparable = {key: value for key, value in state.items() if key != "updated_at"}
            changed = value_digest(comparable)
            if changed != last_digest or time.monotonic() - last_write >= 5:
                state["updated_at"] = time.time()
                write(state_path, state)
                last_digest, last_write = changed, time.monotonic()
            STOP.wait(poll_seconds)
            if STOP.is_set():
                # An already set Event must not turn a slow drain into a busy loop.
                time.sleep(poll_seconds)


def status(config_path, state_dir):
    config = read_config(config_path)
    directory = local_path(state_dir, Path(config["project_root"]).resolve(), exists=False)
    state = load(directory / "state.json") if (directory / "state.json").exists() else None
    if state and state.get("config_hash") != config["config_hash"]:
        raise ValueError("status configuration does not match persistent state")
    return {
        "protocol": PROTOCOL,
        "config_hash": config["config_hash"],
        "state_dir": str(directory),
        "stop_requested": (directory / "STOP").exists(),
        "state": state,
        "watchdog_alive": bool(state and strict_alive(state.get("watchdog_identity"))),
        "tasks": {
            task["id"]: {
                "enabled": task["enabled"],
                "alive": bool(state and strict_alive(state["tasks"][task["id"]].get("identity"))),
            }
            for task in config["tasks"]
        },
    }


def resume_task(config_path, state_dir, task_id):
    """Explicit local resume after the operator clears the task's own STOP."""
    config = read_config(config_path)
    project = Path(config["project_root"]).resolve()
    directory = local_path(state_dir, project)
    registry = local_path(InstancePaths.resolve(project).collection_registry, project)
    if task_id not in {task["id"] for task in config["tasks"]}:
        raise ValueError("unknown task id")
    with (
        lock(registry / f"{config['config_hash']}.lock"),
        lock(directory / "watchdog.lock"),
    ):
        state = load(directory / "state.json")
        if state.get("config_hash") != config["config_hash"]:
            raise ValueError("resume configuration does not match persistent state")
        saved = state["tasks"][task_id]
        if saved["status"] != "paused" or strict_alive(saved.get("identity")):
            raise ValueError("only a stopped paused task can be explicitly resumed")
        saved.update(status="cooldown", resumed_at=time.time())
        write(directory / "state.json", state)
    return state


def unit_argument(value, *, environment=False):
    if any(character in value for character in "\n\r\0"):
        raise ValueError("invalid systemd argument")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    # Environment= does not perform ExecStart's dollar-variable expansion.
    return '"' + (value if environment else value.replace("$", "$$")) + '"'


def unit_directory(value):
    # WorkingDirectory is a path directive, not an ExecStart argument list.
    # Quotes are literal here and would turn an absolute path into a relative one.
    if not value.startswith("/") or any(character in value for character in "\n\r\0"):
        raise ValueError("invalid absolute systemd working directory")
    return value.replace("%", "%%")


def render_unit(config_path, state_dir, uv):
    config_path = local_path(config_path)
    config = read_config(config_path)
    project = Path(config["project_root"]).resolve()
    local_path(config_path, project)
    state_dir = local_path(state_dir, project, exists=False)
    uv = local_path(uv)
    if uv.name != "uv" or not os.access(uv, os.X_OK):
        raise ValueError("an absolute executable uv path is required")
    template = project / "deploy/collection/aitea-collection.service"
    text = template.read_text()
    command = [
        str(uv),
        "run",
        "--project",
        str(project / "backend"),
        "--offline",
        "--no-sync",
        "python",
        str(project / "scripts/collection_watchdog.py"),
        "run",
        "--fleet",
        str(config_path),
        "--state-dir",
        str(state_dir),
    ]
    paths = InstancePaths.resolve(project)
    selectors = {"TEA_DATA_DIR": paths.data, "TEA_CONFIG_DIR": paths.catalogs}
    if os.getenv("TEA_ENV_FILE"):
        selectors["TEA_ENV_FILE"] = paths.env_file
    environment = "\n".join(
        "Environment=" + unit_argument(f"{key}={value}", environment=True) for key, value in selectors.items()
    )
    return (
        text.replace("[Service]", "[Service]\n" + environment)
        .replace("@WORKING_DIRECTORY@", unit_directory(str(project)))
        .replace("@EXEC_START@", " ".join(unit_argument(item) for item in command))
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("run", "status", "render-unit", "resume-task"))
    parser.add_argument("--fleet", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--uv", type=Path, default=shutil.which("uv"))
    parser.add_argument("--task", help="explicit paused task id for resume-task")
    args = parser.parse_args(argv)
    if args.action == "resume-task":
        if not args.task:
            parser.error("resume-task requires --task")
        print(json.dumps(resume_task(args.fleet, args.state_dir, args.task), ensure_ascii=False))
        return 0
    if args.action == "status":
        print(json.dumps(status(args.fleet, args.state_dir), ensure_ascii=False))
        return 0
    if args.action == "render-unit":
        if not args.uv:
            parser.error("--uv requires an absolute executable uv path")
        print(render_unit(args.fleet, args.state_dir, args.uv), end="")
        return 0
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    try:
        return run(args.fleet, args.state_dir)
    except (RecoveryDeferred, ValueError, OSError) as exc:
        print(f"collection watchdog refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
