"""Real local processes cover daemon restart, launch gaps and durable cooldowns."""

from copy import deepcopy
import importlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
watchdog = importlib.import_module("collection_watchdog")


@pytest.fixture
def fleet(tmp_path):
    project = tmp_path / "project"
    (project / "backend").mkdir(parents=True)
    (project / "backend/pyproject.toml").write_text(
        "[project]\nname='watchdog-local-fixture'\nversion='0.0.1'\n"
    )
    (project / "scripts").mkdir()
    for name in ("collection_watchdog.py", "official_source_supervisor_recovery.py"):
        shutil.copyfile(ROOT / "scripts" / name, project / "scripts" / name)
    (project / "deploy/collection").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "deploy/collection/aitea-collection.service",
        project / "deploy/collection/aitea-collection.service",
    )
    worker = project / "scripts/worker.py"
    worker.write_text("""import os, signal, sys, time
from pathlib import Path
root=Path(sys.argv[1]); root.mkdir(exist_ok=True)
with (root/'launches').open('a') as stream: stream.write(str(os.getpid())+'\\n')
(root/'ready').write_text(str(os.getpid()))
def stop(*_):
 (root/'term').write_text('received')
 time.sleep(.15)
 (root/'drained').write_text('complete')
 raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
if (root/'exit').exists(): raise SystemExit(7)
while True: time.sleep(.02)
""")
    tasks = []
    for name in ("id", "sg"):
        tasks.append(
            {
                "id": name,
                "command": [sys.executable, str(worker), str(project / name)],
                "cwd": str(project),
                "enabled": True,
                "restart_seconds": 0.15,
                "max_restart_seconds": 2,
            }
        )
    config = {"protocol": watchdog.PROTOCOL, "project_root": str(project), "tasks": tasks}
    config["config_hash"] = watchdog.value_digest(config)
    path = project / "fleet.json"
    watchdog.write(path, config)
    state = project / ".dev/fleet"
    processes = []

    def start(*, gap=False):
        if gap:
            runner = """import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import official_source_supervisor_recovery as recovery
import collection_watchdog as watchdog
original=recovery.write
def write(path,value,**kwargs):
 if value.get('phase')=='started': os._exit(23)
 return original(path,value,**kwargs)
recovery.write=write
watchdog.run(Path(sys.argv[2]),Path(sys.argv[3]))
"""
            command = [sys.executable, "-c", runner, str(project / "scripts"), str(path), str(state)]
        else:
            command = [
                sys.executable,
                str(project / "scripts/collection_watchdog.py"),
                "run",
                "--fleet",
                str(path),
                "--state-dir",
                str(state),
            ]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        processes.append(process)
        return process

    yield type(
        "Fleet",
        (),
        {"project": project, "config": config, "path": path, "state": state, "start": staticmethod(start)},
    )
    # Only signal identities created by this fixture. Never match a broad process name.
    if state.exists():
        (state / "STOP").touch()
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        saved = watchdog.load(state / "state.json") if (state / "state.json").exists() else {}
        alive = [
            task.get("identity")
            for task in saved.get("tasks", {}).values()
            if watchdog.strict_alive(task.get("identity"))
        ]
        if not alive:
            break
        for record in alive:
            os.kill(record["pid"], signal.SIGTERM)
        time.sleep(0.05)
    for process in processes:
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)


def wait_for(predicate, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.02)
    raise AssertionError("local process condition did not become true")


def saved(fleet):
    path = fleet.state / "state.json"
    return watchdog.load(path) if path.exists() else {}


def launches(fleet, name="id"):
    path = fleet.project / name / "launches"
    return path.read_text().splitlines() if path.exists() else []


def test_two_tasks_run_concurrently_stop_drains_and_preserves_data(fleet):
    durable = fleet.project / "request-ledger.jsonl"
    durable.write_text('{"existing_receipt":true}\n')
    process = fleet.start()
    wait_for(lambda: len(launches(fleet, "id")) == len(launches(fleet, "sg")) == 1)
    report = watchdog.status(fleet.path, fleet.state)
    assert report["watchdog_alive"] and all(task["alive"] for task in report["tasks"].values())
    (fleet.state / "STOP").touch()
    assert process.wait(timeout=8) == 75
    assert all((fleet.project / name / "drained").exists() for name in ("id", "sg"))
    assert saved(fleet)["status"] == "stopped"
    assert durable.read_text() == '{"existing_receipt":true}\n'
    second = fleet.start()
    assert second.wait(timeout=5) == 75
    assert len(launches(fleet)) == 1


def test_duplicate_watchdog_is_rejected_and_term_drains(fleet):
    first = fleet.start()
    wait_for(lambda: len(launches(fleet, "id")) == len(launches(fleet, "sg")) == 1)
    duplicate = fleet.start()
    assert duplicate.wait(timeout=5) == 2
    assert "active lock" in duplicate.stderr.read()
    first.terminate()
    assert first.wait(timeout=8) == 0
    assert all((fleet.project / name / "drained").exists() for name in ("id", "sg"))


def test_killed_child_restarts_while_other_task_continues(fleet):
    process = fleet.start()
    wait_for(lambda: len(launches(fleet, "id")) == len(launches(fleet, "sg")) == 1)
    old = int(launches(fleet)[0])
    os.kill(old, signal.SIGKILL)
    wait_for(lambda: len(launches(fleet)) == 2)
    assert int(launches(fleet)[-1]) != old
    assert len(launches(fleet, "sg")) == 1
    assert process.poll() is None
    assert saved(fleet)["tasks"]["id"]["failures"] == 1


def test_hard_killed_watchdog_adopts_surviving_children_without_duplication(fleet):
    first = fleet.start()
    wait_for(lambda: len(launches(fleet, "id")) == len(launches(fleet, "sg")) == 1)
    before = {key: value["identity"] for key, value in saved(fleet)["tasks"].items()}
    first.kill()
    first.wait(timeout=5)
    replacement = fleet.start()
    wait_for(lambda: saved(fleet).get("watchdog_identity", {}).get("pid") == replacement.pid)
    time.sleep(0.35)
    assert all(saved(fleet)["tasks"][name]["identity"] == before[name] for name in before)
    assert len(launches(fleet)) == len(launches(fleet, "sg")) == 1


def test_spawn_pid_record_gap_recovers_token_owned_child(fleet):
    first = fleet.start(gap=True)
    assert first.wait(timeout=5) == 23
    wait_for(lambda: len(launches(fleet)) == 1)
    assert saved(fleet)["tasks"]["id"]["identity"] is None
    assert watchdog.load(fleet.state / "tasks/id/collector-process.json")["phase"] == "starting"
    replacement = fleet.start()
    wait_for(lambda: saved(fleet).get("tasks", {}).get("id", {}).get("adopted_at"))
    wait_for(lambda: len(launches(fleet, "sg")) == 1)
    assert len(launches(fleet)) == 1
    assert replacement.poll() is None


def test_crash_backoff_is_persisted_across_watchdog_restart(fleet):
    for name in ("id", "sg"):
        (fleet.project / name).mkdir()
        (fleet.project / name / "exit").touch()
    first = fleet.start()
    state = wait_for(
        lambda: saved(fleet) if saved(fleet).get("tasks", {}).get("id", {}).get("failures", 0) >= 4 else None
    )
    next_at = state["tasks"]["id"]["next_at"]
    assert state["tasks"]["id"]["restart_delay"] >= 1
    first.kill()
    first.wait(timeout=5)
    count = len(launches(fleet))
    replacement = fleet.start()
    wait_for(lambda: saved(fleet).get("watchdog_identity", {}).get("pid") == replacement.pid)
    assert saved(fleet)["tasks"]["id"]["next_at"] == next_at
    time.sleep(0.15)
    if time.time() < next_at:
        assert len(launches(fleet)) == count
    wait_for(lambda: len(launches(fleet)) > count)


def test_identity_command_hash_prevents_signalling_reused_or_changed_process():
    current = watchdog.identity(os.getpid())
    assert watchdog.strict_alive(current)
    assert not watchdog.strict_alive({**current, "command_sha256": "0" * 64})
    assert not watchdog.strict_alive({**current, "boot_id": "another-boot"})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda config: config.update(config_hash="bad"),
        lambda config: config["tasks"][0].update(command=["/bin/sh", "-c", "true"]),
        lambda config: config["tasks"][0].update(command=[sys.executable, "-c", "print('bad')"]),
        lambda config: config["tasks"][0].update(cwd="/tmp"),
        lambda config: config["tasks"][0].update(command=[sys.executable, "/tmp/outside.py"]),
        lambda config: config["tasks"][0].update(restart_seconds=float("inf")),
    ],
)
def test_invalid_configuration_is_rejected_without_launch(fleet, mutation):
    config = deepcopy(fleet.config)
    mutation(config)
    if config["config_hash"] != "bad" and all(
        task["restart_seconds"] != float("inf") for task in config["tasks"]
    ):
        config["config_hash"] = watchdog.value_digest(
            {key: value for key, value in config.items() if key != "config_hash"}
        )
    if any(task["restart_seconds"] == float("inf") for task in config["tasks"]):
        with pytest.raises(ValueError):
            watchdog.value_digest(config)
        return
    watchdog.write(fleet.path, config)
    with pytest.raises((ValueError, FileNotFoundError)):
        watchdog.read_config(fleet.path)
    assert not launches(fleet)


def test_rolling_logs_are_bounded_without_modifying_request_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "MAX_LOG_BYTES", 100)
    monkeypatch.setattr(watchdog, "LOG_BACKUPS", 2)
    ledger = tmp_path / "requests.jsonl"
    ledger.write_text("original ledger\n")
    output = watchdog.RotatingOutput(tmp_path / "watchdog-output.log")
    with output.open() as child_output:
        original_inode = os.fstat(child_output.fileno()).st_ino
        for _ in range(50):
            child_output.write(b"diagnostic" * 5)
            output.rotate()
            assert output.path.stat().st_ino == original_inode
    logs = list(tmp_path.glob("watchdog-output.log*"))
    assert len(logs) == 3
    assert all(path.stat().st_size <= 100 for path in logs)
    assert ledger.read_text() == "original ledger\n"


def test_model_credentials_are_removed_from_worker_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("EMBEDDING_API_KEY", "private-not-for-collection")
    monkeypatch.setenv("VISION_MODEL", "paid-model")
    env = watchdog.model_free_environment(tmp_path, {"id": "raw"}, "launch-token")
    assert env["EMBEDDING_API_KEY"] == env["VISION_MODEL"] == ""
    assert env["VISION_OCR_ENABLED"] == "false"
    assert env["AITEA_COLLECTION_LAUNCH_TOKEN"] == "launch-token"


def test_systemd_unit_uses_explicit_paths_and_stop_status(fleet):
    uv = fleet.project / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o700)
    text = watchdog.render_unit(fleet.path, fleet.state, Path(uv))
    assert "@EXEC_START@" not in text and "@WORKING_DIRECTORY@" not in text
    assert "Restart=always" in text and "RestartPreventExitStatus=75" in text
    assert "Type=exec" in text and "KillMode=mixed" in text and "TimeoutStopSec=180" in text
    assert f"WorkingDirectory={fleet.project}\n" in text
    assert str(fleet.project / "scripts/collection_watchdog.py") in text
    assert '"--no-sync" "python"' in text
    assert watchdog.unit_argument("/path/with % and $ and spaces") == '"/path/with %% and $$ and spaces"'


def test_same_fleet_cannot_change_state_directory_after_parent_crash(fleet):
    first = fleet.start()
    wait_for(lambda: len(launches(fleet, "id")) == len(launches(fleet, "sg")) == 1)
    first.kill()
    first.wait(timeout=5)
    with pytest.raises(ValueError, match="another state directory"):
        watchdog.run(fleet.path, fleet.project / ".dev/other-state")
    assert len(launches(fleet)) == len(launches(fleet, "sg")) == 1


def test_uv_command_supports_offline_locked_project_runtime(fleet):
    uv = fleet.project / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o700)
    command = [
        str(uv),
        "run",
        "--project",
        str(fleet.project / "backend"),
        "--offline",
        "--no-sync",
        "python",
        str(fleet.project / "scripts/worker.py"),
        str(fleet.project / "id"),
    ]
    assert watchdog.validate_command(command, fleet.project) == command
    with pytest.raises(ValueError):
        watchdog.validate_command(command[:4] + ["--with", "unapproved", *command[4:]], fleet.project)


def test_killed_uv_launcher_adopts_surviving_python_child_and_drains(fleet):
    uv = fleet.project / "uv"
    uv.write_text(
        "#!" + sys.executable + "\n"
        "import subprocess,sys\n"
        "child=subprocess.Popen([sys.executable,*sys.argv[sys.argv.index('python')+1:]])\n"
        "raise SystemExit(child.wait())\n"
    )
    uv.chmod(0o700)
    config = deepcopy(fleet.config)
    original = config["tasks"][0]["command"]
    config["tasks"][0]["command"] = [
        str(uv),
        "run",
        "--project",
        str(fleet.project / "backend"),
        "--offline",
        "--no-sync",
        "python",
        *original[1:],
    ]
    config["config_hash"] = watchdog.value_digest(
        {key: value for key, value in config.items() if key != "config_hash"}
    )
    watchdog.write(fleet.path, config)
    process = fleet.start()
    wait_for(lambda: len(launches(fleet, "id")) == len(launches(fleet, "sg")) == 1)
    launcher = saved(fleet)["tasks"]["id"]["identity"]
    worker_pid = int(launches(fleet)[0])
    assert launcher["pid"] != worker_pid
    os.kill(launcher["pid"], signal.SIGKILL)
    wait_for(lambda: saved(fleet)["tasks"]["id"]["identity"]["pid"] == worker_pid)
    assert len(launches(fleet)) == 1
    (fleet.state / "STOP").touch()
    assert process.wait(timeout=8) == 75
    assert (fleet.project / "id/drained").exists()


def test_continuous_output_survives_parent_kill_and_recovery_without_epipe(fleet):
    worker = fleet.project / "scripts/worker.py"
    worker.write_text(
        worker.read_text().replace(
            "while True: time.sleep(.02)",
            "while True:\n os.write(1,b'diagnostic output ' * 2048)\n (root/'heartbeat').write_text(str(time.time()))\n time.sleep(.005)",
        )
    )
    first = fleet.start()
    wait_for(lambda: len(launches(fleet, "id")) == len(launches(fleet, "sg")) == 1)
    wait_for(lambda: (fleet.project / "id/heartbeat").exists())
    old_identity = saved(fleet)["tasks"]["id"]["identity"]
    old_heartbeat = (fleet.project / "id/heartbeat").read_text()
    old_log_inode = (fleet.state / "tasks/id/watchdog-output.log").stat().st_ino
    first.kill()
    first.wait(timeout=5)
    # A child keeps writing while no watchdog exists. The former pipe design
    # dies here with EPIPE before its original PID could be adopted.
    time.sleep(0.5)
    assert watchdog.strict_alive(old_identity)
    assert (fleet.project / "id/heartbeat").read_text() != old_heartbeat
    replacement = fleet.start()
    wait_for(lambda: saved(fleet).get("watchdog_identity", {}).get("pid") == replacement.pid)
    wait_for(lambda: list((fleet.state / "tasks/id").glob("watchdog-output.log.[123]")))
    assert saved(fleet)["tasks"]["id"]["identity"] == old_identity
    assert len(launches(fleet)) == 1
    assert (fleet.state / "tasks/id/watchdog-output.log").stat().st_ino == old_log_inode
    (fleet.state / "STOP").touch()
    assert replacement.wait(timeout=8) == 75
    logs = list((fleet.state / "tasks/id").glob("watchdog-output.log*"))
    assert len(logs) <= watchdog.LOG_BACKUPS + 1
    assert all(path.stat().st_size <= watchdog.MAX_LOG_BYTES for path in logs)
    assert all(b"BrokenPipe" not in path.read_bytes() for path in logs)


def test_child_stop_exit_is_durably_paused_until_explicit_offline_resume(fleet):
    worker = fleet.project / "scripts/worker.py"
    worker.write_text(
        worker.read_text().replace(
            "while True: time.sleep(.02)",
            "while True:\n if (root/'business-STOP').exists(): raise SystemExit(75)\n time.sleep(.02)",
        )
    )
    first = fleet.start()
    wait_for(lambda: len(launches(fleet)) == len(launches(fleet, "sg")) == 1)
    (fleet.project / "id/business-STOP").touch()
    wait_for(lambda: saved(fleet)["tasks"]["id"]["status"] == "paused")
    time.sleep(0.4)
    assert len(launches(fleet)) == 1
    with pytest.raises(watchdog.RecoveryDeferred):
        watchdog.resume_task(fleet.path, fleet.state, "id")
    first.terminate()
    assert first.wait(timeout=8) == 0
    assert saved(fleet)["tasks"]["id"]["status"] == "paused"
    (fleet.project / "id/business-STOP").unlink()
    watchdog.resume_task(fleet.path, fleet.state, "id")
    replacement = fleet.start()
    wait_for(lambda: len(launches(fleet)) == 2)
    assert replacement.poll() is None


def test_fleet_term_child_exit75_is_stopped_and_restarts_automatically(fleet):
    worker = fleet.project / "scripts/worker.py"
    worker.write_text(worker.read_text().replace("raise SystemExit(0)", "raise SystemExit(75)"))
    first = fleet.start()
    wait_for(lambda: len(launches(fleet)) == len(launches(fleet, "sg")) == 1)
    first.terminate()
    assert first.wait(timeout=8) == 0
    stopped = saved(fleet)
    assert all(task["status"] == "stopped" for task in stopped["tasks"].values())
    assert all(task["last_returncode"] == 75 for task in stopped["tasks"].values())
    assert all((fleet.project / country / "drained").exists() for country in ("id", "sg"))
    replacement = fleet.start()
    wait_for(
        lambda: (
            len(launches(fleet)) == len(launches(fleet, "sg")) == 2
            and all(task["status"] == "running" for task in saved(fleet)["tasks"].values())
        )
    )
    assert replacement.poll() is None
    assert all(task["status"] == "running" for task in saved(fleet)["tasks"].values())
    replacement.terminate()
    assert replacement.wait(timeout=8) == 0


def test_working_directory_escapes_specifiers_without_exec_argument_quotes():
    assert watchdog.unit_directory("/path/with % and $ and spaces") == "/path/with %% and $ and spaces"
    for invalid in ("relative", "/path\nExecStart=/bin/false", "/path\rbroken", "/path\0broken"):
        with pytest.raises(ValueError):
            watchdog.unit_directory(invalid)


def test_rendered_unit_passes_real_systemd_analyze_verify(fleet):
    analyzer = shutil.which("systemd-analyze")
    if not analyzer:
        pytest.skip("systemd-analyze is unavailable on this platform")
    uv = fleet.project / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o700)
    text = watchdog.render_unit(fleet.path, fleet.state, uv)
    unit = fleet.project / "aitea-collection-test.service"
    env = os.environ.copy()
    runtime = fleet.project / "systemd-runtime"
    runtime.mkdir(mode=0o700)
    env["XDG_RUNTIME_DIR"] = str(runtime)
    env.pop("DBUS_SESSION_BUS_ADDRESS", None)
    for directory in (fleet.project, fleet.project / "directory with spaces %"):
        directory.mkdir(exist_ok=True)
        unit.write_text(
            text.replace(
                f"WorkingDirectory={watchdog.unit_directory(str(fleet.project))}\n",
                f"WorkingDirectory={watchdog.unit_directory(str(directory))}\n",
            )
        )
        result = subprocess.run(
            [analyzer, "--user", "verify", "--man=no", str(unit)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr


def test_delayed_exit_observation_does_not_reset_crash_backoff():
    task = {"restart_seconds": 0.15, "max_restart_seconds": 2}
    state = {"failures": 3, "started_at": 0, "stable_run": False}
    watchdog.record_exit(state, task, now=100, returncode=7)
    assert state["failures"] == 4
    assert state["restart_delay"] == 1.2
    # A verified stable run resets past crashes on its next actual exit.
    state["stable_run"] = True
    watchdog.record_exit(state, task, now=200, returncode=7)
    assert state["failures"] == 1
    assert state["restart_delay"] == 0.15


def test_verified_stable_run_resets_backoff_after_real_crashes(fleet):
    directory = fleet.project / "id"
    directory.mkdir()
    (directory / "exit").touch()
    process = fleet.start()
    wait_for(lambda: saved(fleet).get("tasks", {}).get("id", {}).get("failures", 0) >= 2)
    (directory / "exit").unlink()
    wait_for(lambda: saved(fleet)["tasks"]["id"].get("stable_run"), seconds=10)
    before = saved(fleet)["tasks"]["id"]
    assert before["failures"] >= 2 and before["alive_observed_monotonic"] > 0
    os.kill(before["identity"]["pid"], signal.SIGKILL)
    wait_for(
        lambda: (
            (current := saved(fleet)["tasks"]["id"])["failures"] == 1
            and current["status"] == "running"
            and current["identity"]["pid"] != before["identity"]["pid"]
        )
    )
    current = saved(fleet)["tasks"]["id"]
    assert current["stable_run"] is False
    assert current["restart_delay"] == 0.15
    assert process.poll() is None
