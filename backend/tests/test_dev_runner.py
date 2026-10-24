"""Lifecycle boundaries: safe ownership, lockfiles, ports, cache and rollback."""

from __future__ import annotations

import importlib.util
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.paths import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts/dev.py"
spec = importlib.util.spec_from_file_location("tea_dev_runner", SCRIPT)
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)


def managed_process(root: Path) -> tuple[subprocess.Popen, dict]:
    env = {**os.environ, "TEA_DEV_ROOT": str(root), "TEA_DEV_TOKEN": "test-private-launch"}
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], cwd=root, env=env, start_new_session=True
    )
    record = dev.launched_identity(process, "test-private-launch", root)
    assert record
    return process, record


def test_new_child_identity_waits_for_launch_credentials(monkeypatch, tmp_path):
    expected = {"pid": 123, "owner": str(tmp_path), "token": "owned-launch"}
    snapshots = iter([None, {**expected, "token": ""}, {**expected, "owner": "/other"}, expected])
    monkeypatch.setattr(dev, "process_identity", lambda pid: next(snapshots))
    monkeypatch.setattr(dev.time, "sleep", lambda delay: None)
    process = SimpleNamespace(pid=123, poll=lambda: None)
    assert dev.launched_identity(process, "owned-launch", tmp_path) == expected


def test_exited_child_identity_cannot_be_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(
        dev, "process_identity", lambda pid: pytest.fail("exited process must not be recorded")
    )
    process = SimpleNamespace(pid=123, poll=lambda: 1)
    assert dev.launched_identity(process, "owned-launch", tmp_path) is None


def test_occupied_port_is_rejected_without_stopping_listener():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        with pytest.raises(dev.DevError, match="已被占用"):
            dev.available_port(listener.getsockname()[1])
        assert listener.fileno() >= 0


def test_real_owned_process_and_pid_reuse_protection(tmp_path):
    process, record = managed_process(tmp_path)
    try:
        assert dev.owns_process(record, tmp_path)
        assert not dev.owns_process({**record, "start": "0"}, tmp_path)
        assert not dev.owns_process({**record, "token": "another-launch"}, tmp_path)
        assert not dev.owns_process(record, tmp_path / "other")
        assert not dev.signal_owned({**record, "start": "0"}, signal.SIGTERM, tmp_path, group=True)
        assert process.poll() is None
        assert dev.signal_owned(record, signal.SIGTERM, tmp_path, group=True)
        process.wait(timeout=5)
        assert not dev.owns_process(record, tmp_path)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_foreign_state_refuses_stop_and_clean(tmp_path):
    dev.write_json(dev.runtime(tmp_path) / "dev.json", {"owner": "/different-checkout"})
    with pytest.raises(dev.DevError, match="不属于"):
        dev.stop_locked(tmp_path)
    with pytest.raises(dev.DevError, match="不属于"):
        dev.clean_locked(tmp_path)


def test_concurrent_lifecycle_cannot_enter_lock(tmp_path):
    with dev.control_lock(tmp_path):
        with pytest.raises(dev.DevError, match="正在启动"):
            with dev.control_lock(tmp_path):
                pytest.fail("second launcher entered")
    with dev.control_lock(tmp_path):
        pass


def test_clean_preserves_data_dependencies_config_and_caches(tmp_path):
    paths = (
        "data/business.sqlite",
        ".env",
        "backend/.venv/pyvenv.cfg",
        ".dev/uv-cache/item",
        ".dev/pnpm-store/item",
        ".dev/dependencies.json",
        ".dev/control.lock",
    )
    for name in (*paths, ".dev/api.log", ".dev/web.log"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("preserve")
    dev.write_json(tmp_path / ".dev/dev.json", {"owner": str(tmp_path), "services": []})
    dev.clean_locked(tmp_path)
    assert all((tmp_path / name).is_file() for name in paths)
    assert not (tmp_path / ".dev/dev.json").exists()
    assert not (tmp_path / ".dev/api.log").exists()


def test_clean_refuses_running_owned_service(tmp_path):
    process, record = managed_process(tmp_path)
    try:
        dev.write_json(dev.runtime(tmp_path) / "dev.json", {"owner": str(tmp_path), "services": [record]})
        with pytest.raises(dev.DevError, match="仍在运行"):
            dev.clean_locked(tmp_path)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_dependency_invalidation_and_skip_missing_environment(tmp_path):
    (tmp_path / "backend/.venv/bin").mkdir(parents=True)
    for name in ("python", "uvicorn"):
        (tmp_path / "backend/.venv/bin" / name).write_text("installed")
    (tmp_path / "backend/pyproject.toml").write_text("initial")
    (tmp_path / "backend/uv.lock").write_text("lock-v1")
    assert dev.needs_sync("api", "auto", tmp_path)
    dev.write_json(
        dev.runtime(tmp_path) / "dependencies.json",
        {
            "api": dev.dependency_fingerprint("api", tmp_path),
        },
    )
    assert not dev.needs_sync("api", "auto", tmp_path)
    assert dev.needs_sync("api", "force", tmp_path)
    (tmp_path / "backend/uv.lock").write_text("lock-v2")
    assert dev.needs_sync("api", "auto", tmp_path)
    assert not dev.needs_sync("api", "skip", tmp_path)
    (tmp_path / "backend/.venv/bin/python").unlink()
    with pytest.raises(dev.DevError, match="尚未安装"):
        dev.needs_sync("api", "skip", tmp_path)


def test_uv_environment_is_checkout_local(tmp_path, monkeypatch):
    monkeypatch.setenv("VIRTUAL_ENV", "/other/.venv")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "/other/.venv")
    env = dev.local_env(tmp_path)
    assert "VIRTUAL_ENV" not in env
    assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "backend/.venv")
    assert env["UV_CACHE_DIR"] == str(tmp_path / ".dev/uv-cache")
    assert env["npm_config_cache"] == str(tmp_path / ".dev/npm-cache")


def test_linked_worktree_ports_are_stable_and_distinct(tmp_path, monkeypatch):
    assert dev.default_ports(tmp_path) == (8000, 3000)
    (tmp_path / ".git").write_text("gitdir: elsewhere")
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(
            stdout="/repo/.git/worktrees/topic\n/repo/.git\n",
            returncode=0,
        ),
    )
    ports = dev.default_ports(tmp_path)
    assert ports == dev.default_ports(tmp_path)
    assert 9000 <= ports[0] <= 9999
    assert 4000 <= ports[1] <= 4999


def test_invalid_cli_ports_fail(monkeypatch):
    monkeypatch.delenv("TEA_API_PORT", raising=False)
    monkeypatch.delenv("TEA_WEB_PORT", raising=False)
    for argv in (["--api-port", "0"], ["--web-port", "65536"], ["--api-port", "4000", "--web-port", "4000"]):
        with pytest.raises(SystemExit) as error:
            dev.parse_args(argv)
        assert error.value.code == 2


def test_backend_uses_uv_locked_environment_and_dev_reload(monkeypatch):
    monkeypatch.setattr(dev, "require_tools", lambda: ("uv", "node"))
    args = SimpleNamespace(production=False, api_port=8000, web_port=3000)
    command = dev.service_commands(args)[0][2]
    assert command[:2] == ["uv", "run"]
    assert "--locked" in command and "--no-sync" in command and "--reload" in command
    args.production = True
    assert "--reload" not in dev.service_commands(args)[0][2]


def test_failed_dependency_sync_does_not_mark_fingerprint(tmp_path, monkeypatch):
    (tmp_path / "backend").mkdir()
    monkeypatch.setattr(dev, "require_tools", lambda: ("uv", "node"))
    monkeypatch.setattr(dev, "run_install", lambda *a, **k: 1)
    with pytest.raises(dev.DevError, match="同步失败"):
        dev.sync_dependencies("force", tmp_path)
    assert not (tmp_path / ".dev/dependencies.json").exists()


def test_supervisor_rolls_back_when_second_service_fails(tmp_path):
    """Run a real supervisor with a failed web child and verify API rollback."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    # Use the actual runner functions with only the child commands replaced. This
    # exercises Popen, /proc identity, failure cleanup, and persisted failed state.
    source = SCRIPT.read_text()
    source = source[: source.rfind('if __name__ == "__main__":')]
    source += """
def health_ready(*args, **kwargs):
    # Isolate this failed-start fixture from a real development server on 3000/8000.
    return False
def service_commands(args, root=ROOT):
    return [("api", ROOT, [sys.executable, "-c", "import time; time.sleep(60)"]),
            ("web", ROOT, [sys.executable, "-c", "raise SystemExit(7)"])]
if __name__ == "__main__":
    sys.exit(main())
"""
    copied = scripts / "dev.py"
    copied.write_text(source)
    (tmp_path / ".dev").mkdir()
    env = {**os.environ, "TEA_DEV_ROOT": str(tmp_path), "TEA_DEV_TOKEN": "rollback-check"}
    process = subprocess.Popen(
        [sys.executable, str(copied), "serve", "--token", "rollback-check"],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        assert process.wait(timeout=15) == 1
        state = dev.read_json(tmp_path / ".dev/dev.json")
        assert state["status"] == "failed"
        assert "web" in state["error"]
        assert not dev.live_records(state, tmp_path)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        state = dev.read_json(tmp_path / ".dev/dev.json")
        for record in state.get("services", []):
            dev.signal_owned(record, signal.SIGKILL, tmp_path, group=True)


def test_state_file_private(tmp_path):
    path = tmp_path / "dev.json"
    dev.write_json(path, {"token": "private"})
    assert path.stat().st_mode & 0o777 == 0o600


def test_new_child_identity_waits_for_launch_environment(tmp_path, monkeypatch):
    environment = {**os.environ, "TEA_DEV_ROOT": str(tmp_path), "TEA_DEV_TOKEN": "delayed-identity"}
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        cwd=tmp_path,
        env=environment,
        start_new_session=True,
    )
    read_bytes = Path.read_bytes
    attempts = 0

    def transitional_read(path):
        nonlocal attempts
        if path == Path("/proc") / str(process.pid) / "environ":
            attempts += 1
            if attempts <= 2:
                return b""
        return read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", transitional_read)
    try:
        record = dev.process_identity(process.pid)
        assert record["owner"] == str(tmp_path) and record["token"] == "delayed-identity"
        assert attempts >= 3
        assert dev.owns_process(record, tmp_path)
        dev.terminate([record], tmp_path)
        process.wait(timeout=3)
        assert not dev.process_identity(process.pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)


def test_orphaned_group_is_still_owned_and_stopped(tmp_path):
    child_file = tmp_path / "child.pid"
    child_code = (
        "import os,time; from pathlib import Path; Path("
        + repr(str(child_file))
        + ").write_text(str(os.getpid())); time.sleep(60)"
    )
    leader_code = (
        "import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', "
        + repr(child_code)
        + "]); time.sleep(0.3)"
    )
    env = {**os.environ, "TEA_DEV_ROOT": str(tmp_path), "TEA_DEV_TOKEN": "orphan-launch"}
    leader = subprocess.Popen(
        [sys.executable, "-c", leader_code], cwd=tmp_path, env=env, start_new_session=True
    )
    record = dev.process_identity(leader.pid)
    assert record
    try:
        leader.wait(timeout=5)
        assert child_file.exists()
        child = int(child_file.read_text())
        assert dev.process_identity(child)
        assert not dev.owns_process(record, tmp_path)
        assert dev.owned_group(record, tmp_path)
        dev.terminate([record], tmp_path)
        assert not dev.owned_group(record, tmp_path)
    finally:
        if leader.poll() is None:
            leader.kill()
            leader.wait()
        dev.signal_owned(record, signal.SIGKILL, tmp_path, group=True)


def test_sync_environment_default_and_invalid_value(monkeypatch):
    monkeypatch.setenv("SYNC", "skip")
    assert dev.parse_args([]).sync == "skip"
    assert dev.parse_args(["--sync", "force"]).sync == "force"
    monkeypatch.setenv("SYNC", "bad")
    with pytest.raises(SystemExit) as error:
        dev.parse_args([])
    assert error.value.code == 2


def test_production_build_rejects_wrong_api_rewrite(tmp_path):
    build = tmp_path / "frontend/.next"
    build.mkdir(parents=True)
    (build / "BUILD_ID").write_text("test-build")
    dev.write_json(
        build / "routes-manifest.json",
        {
            "rewrites": {
                "beforeFiles": [],
                "afterFiles": [
                    {"source": "/api/tea/:path*", "destination": "http://127.0.0.1:8000/api/tea/:path*"},
                ],
                "fallback": [],
            }
        },
    )
    dev.check_production_build(8000, tmp_path)
    with pytest.raises(dev.DevError, match="make build API_PORT=9000"):
        dev.check_production_build(9000, tmp_path)


@pytest.mark.parametrize("ready", [False, True], ids=["during-startup", "foreground-ready"])
def test_launcher_sigterm_reaps_detached_services(tmp_path, ready):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = SCRIPT.read_text()
    source = source[: source.rfind('if __name__ == "__main__":')]
    source += "\ndef sync_dependencies(*args, **kwargs): pass\n"
    source += "def available_port(*args, **kwargs): pass\n"
    source += "def health_ready(*args, **kwargs): return " + repr(ready) + "\n"
    source += """
def service_commands(args, root=ROOT):
    return [(name, ROOT, [sys.executable, "-c", "import time; time.sleep(60)"])
            for name in ("api", "web")]
if __name__ == "__main__":
    sys.exit(main())
"""
    copied = scripts / "dev.py"
    copied.write_text(source)
    process = subprocess.Popen(
        [sys.executable, str(copied), "start", "--foreground"],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = dev.read_json(tmp_path / ".dev/dev.json")
            if len(state.get("services", [])) == 2 and (not ready or state.get("status") == "ready"):
                # Let the launcher observe readiness and enter its foreground wait.
                if ready:
                    time.sleep(0.4)
                break
            assert process.poll() is None
            time.sleep(0.02)
        else:
            pytest.fail("supervisor did not start its two services")
        process.terminate()
        assert process.wait(timeout=15) == (0 if ready else 130)
        state = dev.read_json(tmp_path / ".dev/dev.json")
        assert state["status"] == "stopped"
        assert not dev.live_records(state, tmp_path)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        state = dev.read_json(tmp_path / ".dev/dev.json")
        dev.signal_owned(state.get("supervisor", {}), signal.SIGKILL, tmp_path)
        for record in state.get("services", []):
            dev.signal_owned(record, signal.SIGKILL, tmp_path, group=True)


@pytest.mark.parametrize(
    "start_command,stop_command", [("start", "stop"), ("setup", "stop"), ("start", "clean")]
)
def test_stop_cancels_in_progress_dependency_install(tmp_path, start_command, stop_command):
    """A separate stop CLI must cancel the locked installer and its descendants."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    source = SCRIPT.read_text()
    source = source[: source.rfind('if __name__ == "__main__":')]
    worker_file = tmp_path / "installer-worker.pid"
    worker_code = f"import os,time; from pathlib import Path; Path({str(worker_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
    installer_code = f"import subprocess,sys,time; subprocess.Popen([sys.executable, '-c', {worker_code!r}]); time.sleep(60)"
    source += f"""
def available_port(*args, **kwargs): pass
def sync_dependencies(mode, root=ROOT):
    run_install([sys.executable, '-c', {installer_code!r}], ROOT, local_env(), runtime() / 'setup-web.log')
if __name__ == '__main__':
    sys.exit(main())
"""
    copied = scripts / "dev.py"
    copied.write_text(source)
    # Verify preservation on clean against the same cancellation path.
    (tmp_path / "data").mkdir()
    (tmp_path / "data/business").write_text("keep")
    process = subprocess.Popen(
        [sys.executable, str(copied), start_command],
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    state = {}
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = dev.read_json(tmp_path / ".dev/startup.json")
            if state.get("installer") and worker_file.exists():
                break
            assert process.poll() is None
            time.sleep(0.02)
        else:
            pytest.fail("installer and its descendant did not start")
        assert dev.status(tmp_path)["status"] == "preparing"
        worker_pid = int(worker_file.read_text())
        stopped = subprocess.run(
            [sys.executable, str(copied), stop_command],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        assert stopped.returncode == 0, stopped.stdout + stopped.stderr
        assert "取消" in stopped.stdout
        assert process.wait(timeout=5) == 130
        assert not dev.owned_group(state["installer"], tmp_path)
        assert not dev.process_identity(worker_pid)
        assert dev.status(tmp_path)["status"] == "stopped"
        assert (tmp_path / "data/business").read_text() == "keep"
        assert not (tmp_path / ".dev/startup.json").exists()
        # Repeated stop is safe, and a subsequent start can acquire the same lock.
        repeated = subprocess.run(
            [sys.executable, str(copied), "stop"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        with dev.control_lock(tmp_path):
            pass
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if state.get("installer"):
            dev.signal_owned(state["installer"], signal.SIGKILL, tmp_path, group=True)


def test_dependency_progress_is_visible_before_install_finishes(tmp_path, capsys):
    log = tmp_path / "install.log"
    result = dev.run_install(
        [
            sys.executable,
            "-c",
            "import time; print('Downloaded 12/20 packages', flush=True); time.sleep(3.3)",
        ],
        tmp_path,
        dev.local_env(tmp_path),
        log,
        tmp_path,
    )
    assert result == 0
    assert "Downloaded 12/20 packages" in capsys.readouterr().out


def fixture_runner(tmp_path: Path, overrides: str) -> Path:
    """Keep lifecycle code real while replacing only this fixture's services."""
    script = tmp_path / "scripts/dev.py"
    script.parent.mkdir()
    source = SCRIPT.read_text()
    source = source[: source.rfind('if __name__ == "__main__":')]
    source += "\ndef sync_dependencies(*args, **kwargs): pass\n"
    source += "def available_port(*args, **kwargs): pass\n"
    source += "def check_production_build(*args, **kwargs): pass\n"
    script.write_text(source + overrides + '\nif __name__ == "__main__":\n    sys.exit(main())\n')
    return script


def stop_fixture(script: Path, root: Path) -> None:
    subprocess.run(
        [sys.executable, str(script), "stop"],
        cwd=root,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=True,
    )


@pytest.mark.parametrize(
    "production,polling,expected_web",
    [
        (False, None, "1000"),
        (False, "false", "false"),
        (False, "750", "750"),
        (True, None, None),
        (True, "750", "750"),
    ],
)
def test_service_polling_is_development_web_only(tmp_path, production, polling, expected_web):
    child = (
        "import json,os,time; from pathlib import Path; "
        "time.sleep(0.1 if '{name}' == 'api' else 0); "
        "Path(os.environ['TEA_DEV_ROOT'], '{name}.json').write_text("
        "json.dumps(os.environ.get('WATCHPACK_POLLING'))); time.sleep(60)"
    )
    script = fixture_runner(
        tmp_path,
        f"""
def health_ready(url, *, api=False):
    try:
        json.loads((ROOT / ('api.json' if api else 'web.json')).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return True
def service_commands(args, root=ROOT):
    return [(name, ROOT, [sys.executable, '-c', {child!r}.format(name=name)])
            for name in ('api', 'web')]
""",
    )
    env = os.environ.copy()
    env.pop("WATCHPACK_POLLING", None)
    if polling is not None:
        env["WATCHPACK_POLLING"] = polling
    command = [sys.executable, str(script), "start"]
    if production:
        command.append("--production")
    try:
        result = subprocess.run(
            command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert dev.read_json(tmp_path / ".dev/dev.json")["status"] == "ready"
        import json

        assert json.loads((tmp_path / "web.json").read_text()) == expected_web
        assert json.loads((tmp_path / "api.json").read_text()) == polling
        assert result.stdout.count("前端监听：") == (0 if production else 1)
    finally:
        stop_fixture(script, tmp_path)


@pytest.mark.parametrize("api_ready", [False, True])
def test_start_failure_reports_each_readiness_check_and_bounded_logs(tmp_path, api_ready):
    script = fixture_runner(
        tmp_path,
        f"""
START_TIMEOUT = 0.2
def health_ready(url, *, api=False): return api and {api_ready!r}
def service_commands(args, root=ROOT):
    code = "import time; print('test diagnostic ' + '测' * 20000, flush=True); time.sleep(60)"
    return [(name, ROOT, [sys.executable, '-c', code]) for name in ('api', 'web')]
""",
    )
    try:
        result = subprocess.run(
            [sys.executable, str(script), "start"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 1
        output = result.stdout + result.stderr
        expected = "已就绪" if api_ready else "未就绪"
        assert f"API 健康检查：{expected}" in output
        assert "前端 /tea 及 API 转发：未就绪" in output
        assert "api.log" in output and "web.log" in output
        assert "测测测" in output
        assert len(output) < 10000
        state = dev.read_json(tmp_path / ".dev/dev.json")
        assert state["status"] == "failed"
        assert not dev.live_records(state, tmp_path)
    finally:
        stop_fixture(script, tmp_path)


def test_watcher_exhaustion_fails_promptly_and_reaps_services(tmp_path):
    error = "Watchpack Error (watcher): Error: ENOSPC: System limit for number of file watchers reached"
    script = fixture_runner(
        tmp_path,
        f"""
def health_ready(url, *, api=False): return api
def service_commands(args, root=ROOT):
    return [("api", ROOT, [sys.executable, "-c", "import time; time.sleep(60)"]),
            ("web", ROOT, [sys.executable, "-c", "import time; print({error!r}, flush=True); time.sleep(60)"])]
""",
    )
    started = time.monotonic()
    process = subprocess.Popen(
        [sys.executable, str(script), "start"],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        try:
            stdout, stderr = process.communicate(timeout=6)
        except subprocess.TimeoutExpired:
            pytest.fail("watcher exhaustion waited instead of failing promptly")
        assert process.returncode == 1
        assert time.monotonic() - started < 6
        output = stdout + stderr
        assert "ENOSPC" in output and "文件监听" in output
        assert "API 健康检查：已就绪" in output
        assert "前端 /tea 及 API 转发：未就绪" in output
        state = dev.read_json(tmp_path / ".dev/dev.json")
        assert state["status"] == "failed"
        assert not dev.live_records(state, tmp_path)
    finally:
        stop_fixture(script, tmp_path)
        process.communicate(timeout=5)


def test_background_services_serve_after_launcher_exits(tmp_path):
    with socket.socket() as api_socket, socket.socket() as web_socket:
        api_socket.bind(("127.0.0.1", 0))
        web_socket.bind(("127.0.0.1", 0))
        api_port = api_socket.getsockname()[1]
        web_port = web_socket.getsockname()[1]
    child = """import http.server, sys
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"status":"ok"}' if self.path == '/api/tea/health' else b'tea ready')
http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
"""
    script = fixture_runner(
        tmp_path,
        f"""
def service_commands(args, root=ROOT):
    return [(name, ROOT, [sys.executable, '-c', {child!r}, str(port)])
            for name, port in [('api', args.api_port), ('web', args.web_port)]]
""",
    )
    try:
        result = subprocess.run(
            [sys.executable, str(script), "start", "--api-port", str(api_port), "--web-port", str(web_port)],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert dev.status(tmp_path)["status"] == "running"
        assert dev.health_ready(f"http://127.0.0.1:{api_port}/api/tea/health", api=True)
        assert dev.health_ready(f"http://127.0.0.1:{web_port}/tea")
    finally:
        stop_fixture(script, tmp_path)


@pytest.mark.parametrize(
    "prefix_size,error",
    [
        (0, "Watchpack Error: ENOSPC: file watchers exhausted"),
        (65532, "ENOSPC: Watchpack file watchers exhausted"),
        (65534, "Watchpack Error: ENOSPC: file watchers exhausted"),
        (131072, "Watchpack Error: ENOSPC: file watchers exhausted"),
    ],
)
def test_buried_watcher_error_blocks_ready_across_log_chunks(tmp_path, prefix_size, error):
    marker = tmp_path / "web-log-written"
    child = (
        "import time; from pathlib import Path; "
        f"print('x' * {prefix_size} + {error!r}, flush=True); "
        "print('ordinary startup output\\n' * 30, flush=True); "
        f"Path({str(marker)!r}).write_text('done'); time.sleep(60)"
    )
    script = fixture_runner(
        tmp_path,
        f"""
def health_ready(url, *, api=False):
    deadline = time.monotonic() + 3
    while not (ROOT / 'web-log-written').exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return (ROOT / 'web-log-written').exists()
def service_commands(args, root=ROOT):
    return [("api", ROOT, [sys.executable, "-c", "import time; time.sleep(60)"]),
            ("web", ROOT, [sys.executable, "-c", {child!r}])]
""",
    )
    try:
        result = subprocess.run(
            [sys.executable, str(script), "start"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert marker.exists(), "both health checks must observe the fully written log"
        assert result.returncode == 1, "an earlier watcher failure must prevent reporting readiness"
        assert "ENOSPC" in result.stderr and "文件监听" in result.stderr
        state = dev.read_json(tmp_path / ".dev/dev.json")
        assert state["status"] == "failed"
        assert not dev.live_records(state, tmp_path)
    finally:
        stop_fixture(script, tmp_path)


def test_watcher_log_scans_incrementally_and_retains_detected_failure(tmp_path):
    log = tmp_path / "web.log"
    log.write_bytes(b"ENOSPC: disk output\nordinary watcher message\n" + b"x" * 131072 + b"\n")
    scanner = dev.StartupWatcherLog(log)
    assert not scanner.scan(), "unrelated ENOSPC and watcher lines must not combine"
    assert not scanner.complete, "one scan must not read the entire large log"
    while not scanner.complete:
        assert not scanner.scan()
    with log.open("ab") as handle:
        handle.write(b"Watchpack Error: ENO")
    assert not scanner.scan()
    with log.open("ab") as handle:
        handle.write(b"SPC: file watchers exhausted\n" + b"ordinary output\n" * 30)
    assert scanner.scan(), "appended fragments must complete the earlier error"
    log.write_bytes(b"ordinary replacement log\n")
    assert scanner.scan(), "an observed watcher failure must remain sticky"


@pytest.mark.parametrize(
    "production,node_options,disable_override",
    [
        (False, None, None),
        (False, '--trace-warnings --require "/tmp/my preload.js"', None),
        (False, "--max-old-space-size=2048 --trace-warnings", None),
        (False, "--max_old_space_size=2048 --trace-warnings", None),
        (False, "--max-old-space-size-percentage 25", None),
        (False, "--max-heap-size=2048", None),
        (False, None, "1"),
        (False, None, "0"),
        (False, None, ""),
        (True, None, None),
        (True, "--trace-warnings", "0"),
    ],
)
def test_web_environment_uses_available_memory_and_preserves_user_node_options(
    tmp_path,
    production,
    node_options,
    disable_override,
):
    child = (
        "import json,os,time; from pathlib import Path; "
        "Path(os.environ['TEA_DEV_ROOT'], '{name}.json').write_text("
        "json.dumps([os.environ.get('NODE_OPTIONS'), os.environ.get('NEXT_DISABLE_MEM_OVERRIDE')])); "
        "time.sleep(60)"
    )
    script = fixture_runner(
        tmp_path,
        f"""
def health_ready(*args, **kwargs):
    # Readiness must wait for the child snapshots that this test inspects.
    return all(path.is_file() and path.read_text().endswith(']')
               for path in (ROOT / 'api.json', ROOT / 'web.json'))
def service_commands(args, root=ROOT):
    return [(name, ROOT, [sys.executable, '-c', {child!r}.format(name=name)])
            for name in ('api', 'web')]
""",
    )
    env = os.environ.copy()
    for key, value in (("NODE_OPTIONS", node_options), ("NEXT_DISABLE_MEM_OVERRIDE", disable_override)):
        env.pop(key, None)
        if value is not None:
            env[key] = value
    command = [sys.executable, str(script), "start"]
    if production:
        command.append("--production")
    try:
        result = subprocess.run(
            command, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=10, check=False
        )
        assert result.returncode == 0, result.stdout + result.stderr
        import json

        web_options, web_override = json.loads((tmp_path / "web.json").read_text())
        options = dev.node_option_names(node_options or "")
        if production or options & {
            "--max-old-space-size",
            "--max-old-space-size-percentage",
            "--max-heap-size",
        }:
            assert web_options == node_options
        else:
            assert web_options.startswith(
                f"{node_options} --max-old-space-size=" if node_options else "--max-old-space-size="
            )
            assert int(web_options.rsplit("=", 1)[1]) > 0
        assert web_override == (disable_override if production else "1")
        assert json.loads((tmp_path / "api.json").read_text()) == [node_options, disable_override]
    finally:
        stop_fixture(script, tmp_path)


def test_development_heap_exceeds_node_default_without_preallocating_memory(monkeypatch):
    calls = []

    def memory_probe(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="32768\n")

    monkeypatch.setattr(dev.shutil, "which", lambda name: "/local/node")
    monkeypatch.setattr(dev.subprocess, "run", memory_probe)
    original = '--trace-warnings --require "/local/my preload.js"'
    env = {"NODE_OPTIONS": original, "TEA_DEV_ROOT": "/local/project"}
    assert dev.development_node_options(env) == f"{original} --max-old-space-size=32768"
    assert env["NODE_OPTIONS"] == original
    assert len(calls) == 1
    assert calls[0][0][0] == "/local/node"
    assert "availableMemory()" in calls[0][0][-1]
    assert "NODE_OPTIONS" not in calls[0][1]["env"], "the memory probe must not execute user preloads"
    assert calls[0][1]["env"]["TEA_DEV_ROOT"] == "/local/project"
    assert calls[0][1]["timeout"] == 10


@pytest.mark.parametrize(
    "value",
    [
        "--max-old-space-size=8192",
        "--max_old_space_size 8192",
        "--max-old-space-size-percentage=75",
        "--max-heap-size=8192",
        '--require "unterminated',
    ],
)
def test_explicit_or_invalid_heap_options_are_not_rewritten(monkeypatch, value):
    monkeypatch.setattr(dev.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected memory probe"))
    assert dev.development_node_options({"NODE_OPTIONS": value}) == value


@pytest.mark.parametrize("returncode,output", [(1, "8192"), (0, "0"), (0, "invalid")])
def test_unavailable_memory_fails_instead_of_silently_restoring_default_cap(monkeypatch, returncode, output):
    monkeypatch.setattr(dev.shutil, "which", lambda name: "/local/node")
    monkeypatch.setattr(
        dev.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=returncode,
            stdout=output,
        ),
    )
    with pytest.raises(dev.DevError, match="无法读取开发前端可用内存"):
        dev.development_node_options({})


@pytest.mark.parametrize(
    "production,node_options,disable_maps",
    [
        (False, None, True),
        (False, "--trace-warnings", True),
        (False, "--enable-source-maps --trace-warnings", False),
        (False, "--enable_source_maps", False),
        (False, '--require "/tmp/--enable-source-maps.js"', True),
        (True, None, False),
        (True, "--enable-source-maps", False),
    ],
)
def test_node_source_maps_default_and_explicit_debugging_choice(
    monkeypatch, production, node_options, disable_maps
):
    monkeypatch.setattr(dev, "require_tools", lambda: ("uv", "node"))
    monkeypatch.delenv("NODE_OPTIONS", raising=False)
    if node_options is not None:
        monkeypatch.setenv("NODE_OPTIONS", node_options)
    args = SimpleNamespace(production=production, api_port=8000, web_port=3000)
    commands = dev.service_commands(args)
    assert ("--disable-source-maps" in commands[1][2]) is disable_maps
    assert "--disable-source-maps" not in commands[0][2]


def test_tea_page_success_does_not_hide_failed_api_forwarding(tmp_path):
    script = fixture_runner(
        tmp_path,
        """
START_TIMEOUT = 0.2
def health_ready(url, *, api=False):
    return url != 'http://127.0.0.1:3000/api/tea/health'
def service_commands(args, root=ROOT):
    return [(name, ROOT, [sys.executable, '-c', 'import time; time.sleep(60)'])
            for name in ('api', 'web')]
""",
    )
    try:
        result = subprocess.run(
            [sys.executable, str(script), "start", "--api-port", "8000", "--web-port", "3000"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 1, "readiness must include the frontend's real API forwarding path"
        assert "API 健康检查：已就绪" in result.stderr
        assert "前端 /tea 及 API 转发：未就绪" in result.stderr
        state = dev.read_json(tmp_path / ".dev/dev.json")
        assert state["status"] == "failed"
        assert not dev.live_records(state, tmp_path)
    finally:
        stop_fixture(script, tmp_path)
