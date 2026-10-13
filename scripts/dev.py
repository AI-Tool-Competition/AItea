#!/usr/bin/env python3
"""Own this checkout's local API/frontend lifecycle; never stop port holders."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PNPM_VERSION = "10.26.2"
START_TIMEOUT = 120
STATE_NAME = "dev.json"
RUNTIME_FILES = ("startup.json", STATE_NAME, "api.log", "web.log", "supervisor.log", "setup-api.log", "setup-web.log")


class DevError(RuntimeError):
    pass


def runtime(root: Path = ROOT) -> Path:
    path = root / ".dev"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(path)


@contextmanager
def control_lock(root: Path = ROOT, *, timeout: float = 0):
    # Retain the lock inode during clean; unlinking it would let another start race.
    with (runtime(root) / "control.lock").open("a") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise DevError("当前工作目录正在启动或安装依赖；可用 make dev-stop 取消，然后重试。") from exc
                time.sleep(0.1)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def process_identity(pid: int) -> dict | None:
    """Linux identity prevents PID reuse; launch credentials prove checkout ownership."""
    try:
        base = Path("/proc") / str(pid)
        for attempt in range(5):
            stat = (base / "stat").read_text()
            fields = stat[stat.rfind(")") + 2:].split()
            if fields[0] in {"Z", "X"}:
                return None
            environment = dict(
                item.split(b"=", 1) for item in (base / "environ").read_bytes().split(b"\0") if b"=" in item
            )
            # Popen's exec handshake can finish while /proc still exposes the
            # child's transitional environment. Wait only for our direct child;
            # never fabricate ownership from the parent's credentials.
            if int(fields[1]) != os.getpid() or environment.get(b"TEA_DEV_ROOT") or attempt == 4:
                break
            time.sleep(0.01)
        return {
            "pid": pid,
            "start": fields[19],
            "pgid": int(fields[2]),
            "sid": int(fields[3]),
            "cwd": str((base / "cwd").resolve()),
            "cmdline": (base / "cmdline").read_bytes().decode(errors="replace"),
            "token": environment.get(b"TEA_DEV_TOKEN", b"").decode(),
            "owner": environment.get(b"TEA_DEV_ROOT", b"").decode(),
        }
    except (OSError, ValueError, IndexError):
        return None


def owns_process(record: dict, root: Path = ROOT) -> bool:
    if not isinstance(record, dict) or record.get("owner") != str(root) or not record.get("token"):
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    current = process_identity(pid)
    return current is not None and all(
        current.get(key) == record.get(key)
        for key in ("pid", "start", "pgid", "sid", "cwd", "cmdline", "token", "owner")
    )


def owned_group(record: dict, root: Path = ROOT) -> bool:
    """A surviving descendant can prove ownership after its group leader exited."""
    if (record.get("owner") != str(root) or not record.get("token")
            or not isinstance(record.get("pid"), int) or record["pid"] <= 1
            or record.get("pgid") != record["pid"] or record.get("sid") != record["pid"]):
        return False
    current_leader = process_identity(record["pid"])
    if current_leader and current_leader.get("start") != record.get("start"):
        return False
    if owns_process(record, root):
        return True
    # Tokens are unique to a launch and inherited by children; an unrelated
    # process that reused the numeric PID/group cannot pass this test.
    for path in Path("/proc").iterdir():
        if not path.name.isdigit():
            continue
        try:
            stat = (path / "stat").read_text()
            fields = stat[stat.rfind(")") + 2:].split()
            if (int(fields[2]), int(fields[3])) != (record["pgid"], record["sid"]):
                continue
        except (OSError, ValueError, IndexError):
            continue
        current = process_identity(int(path.name))
        if current and all(current.get(key) == record.get(key) for key in ("pgid", "sid", "owner", "token")):
            return True
    return False


def live_records(state: dict, root: Path = ROOT) -> list[dict]:
    if state.get("owner") != str(root):
        return []
    records = [state.get("supervisor", {}), *state.get("services", [])]
    return [record for record in records if owns_process(record, root) or owned_group(record, root)]


def signal_owned(record: dict, signum: int, root: Path = ROOT, *, group: bool = False) -> bool:
    if not (owned_group(record, root) if group else owns_process(record, root)):
        return False
    try:
        pid = record["pid"]
        if group:
            if record["pgid"] != pid or record["sid"] != pid or pid == os.getpgrp():
                return False
            os.killpg(pid, signum)
        else:
            os.kill(pid, signum)
        return True
    except ProcessLookupError:
        return False


def terminate(records: list[dict], root: Path = ROOT) -> None:
    for record in records:
        signal_owned(record, signal.SIGTERM, root, group=True)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(owned_group(record, root) for record in records):
        time.sleep(0.1)
    for record in records:
        signal_owned(record, signal.SIGKILL, root, group=True)
    # Signals are asynchronous. Do not report cleanup while owned descendants
    # are still exiting, especially after the group leader has already gone.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and any(owned_group(record, root) for record in records):
        time.sleep(0.02)



def startup_path(root: Path = ROOT) -> Path:
    return root / ".dev/startup.json"


def owns_launcher(state: dict, root: Path = ROOT) -> bool:
    """The bootstrap Python process has no launch token in its initial environ."""
    record = state.get("launcher", {})
    if state.get("owner") != str(root) or not isinstance(record, dict):
        return False
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False
    current = process_identity(pid)
    if not current or any(current.get(key) != record.get(key)
                          for key in ("pid", "start", "pgid", "sid", "cwd", "cmdline")):
        return False
    arguments = current["cmdline"].rstrip("\0").split("\0")
    for index, argument in enumerate(arguments[1:], 1):
        script = Path(argument)
        if not script.is_absolute():
            script = Path(current["cwd"]) / script
        if script.resolve() == root / "scripts/dev.py":
            commands = set(arguments[index + 1:]) & {"start", "setup", "serve", "stop", "clean", "status", "list"}
            return not commands or commands <= {"start", "setup"}
    return False


def begin_startup(root: Path = ROOT) -> str:
    identity = process_identity(os.getpid())
    if identity is None:
        raise DevError("无法读取启动器进程身份，拒绝启动。")
    token = secrets.token_hex(24)
    write_json(startup_path(root), {"owner": str(root), "token": token, "launcher": identity,
                                   "installer": {}, "status": "preparing", "started_at": time.time()})
    return token


def finish_startup(token: str, root: Path = ROOT) -> None:
    if read_json(startup_path(root)).get("token") == token:
        startup_path(root).unlink(missing_ok=True)


def cancel_startup(root: Path = ROOT) -> None:
    state = read_json(startup_path(root))
    if not state:
        return
    if state.get("owner") != str(root):
        raise DevError("准备记录不属于当前工作目录，拒绝停止其中的进程。")
    launcher = state.get("launcher", {})
    if owns_launcher(state, root):
        print("正在取消当前工作目录的依赖准备或启动…", flush=True)
        try:
            os.kill(launcher["pid"], signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + 20
        while owns_launcher(state, root) and time.monotonic() < deadline:
            time.sleep(0.1)
    installer = state.get("installer", {})
    if isinstance(installer, dict) and installer.get("token") == state.get("token"):
        terminate([installer], root)
    if owns_launcher(state, root):
        try:
            os.kill(launcher["pid"], signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_install(command: list[str], cwd: Path, env: dict, log: Path, root: Path = ROOT) -> int:
    state = read_json(startup_path(root))
    token = state.get("token") or secrets.token_hex(24)
    launch_env = {**env, "TEA_DEV_ROOT": str(root), "TEA_DEV_TOKEN": token}
    with log.open("wb") as handle:
        process = subprocess.Popen(command, cwd=cwd, env=launch_env, stdout=handle,
                                   stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True)
        record = process_identity(process.pid)
        try:
            if not record:
                raise DevError("依赖安装进程在记录身份前退出。")
            if state.get("owner") == str(root) and state.get("launcher", {}).get("pid") == os.getpid():
                state["installer"] = record
                write_json(startup_path(root), state)
            last_update = time.monotonic()
            last_line = ""
            while process.poll() is None:
                now = time.monotonic()
                if now - last_update >= 3:
                    line = log_tail(log, limit=1)
                    if line and line != last_line:
                        print(f"    {line}", flush=True)
                        last_line = line
                    elif now - last_update >= 15:
                        print(f"    依赖仍在下载或安装，可用 make dev-stop 取消；日志：{log}", flush=True)
                    else:
                        time.sleep(0.1)
                        continue
                    last_update = now
                time.sleep(0.1)
            return process.returncode
        finally:
            if record:
                terminate([record], root)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            current = read_json(startup_path(root))
            if current.get("token") == token:
                current["installer"] = {}
                write_json(startup_path(root), current)


def available_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise DevError(f"端口 {port} 已被占用，请指定其他端口；不会停止已有服务。") from exc


def default_ports(root: Path = ROOT) -> tuple[int, int]:
    # A linked worktree has a .git file, unlike the primary checkout. Submodules
    # use the regular defaults unless git confirms a distinct common directory.
    offset = 0
    if (root / ".git").is_file():
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-dir", "--git-common-dir"],
            capture_output=True, text=True, check=False,
        )
        lines = result.stdout.splitlines()
        if result.returncode == 0 and len(lines) == 2 and lines[0] != lines[1]:
            offset = 1000 + int(hashlib.sha256(str(root).encode()).hexdigest()[:8], 16) % 1000
    return 8000 + offset, 3000 + offset


def local_env(root: Path = ROOT) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.update({
        "UV_CACHE_DIR": str(root / ".dev/uv-cache"),
        "UV_PROJECT_ENVIRONMENT": str(root / "backend/.venv"),
        "UV_PYTHON_INSTALL_DIR": str(root / ".dev/python"),
        "npm_config_cache": str(root / ".dev/npm-cache"),
        "NEXT_TELEMETRY_DISABLED": "1",
        "SKIP_ENV_VALIDATION": "1",
    })
    return env


def node_option_names(value: str) -> set[str] | None:
    try:
        return {argument.split("=", 1)[0].replace("_", "-")
                for argument in shlex.split(value) if argument.startswith("--")}
    except ValueError:
        # Let Node report malformed user options without rewriting them.
        return None


def dependency_fingerprint(component: str, root: Path = ROOT) -> str:
    names = ("backend/pyproject.toml", "backend/uv.lock") if component == "api" else (
        "frontend/package.json", "frontend/pnpm-lock.yaml", "frontend/pnpm-workspace.yaml", "frontend/.npmrc",
    )
    digest = hashlib.sha256(f"tea-dev-v2:{sys.platform}:{PNPM_VERSION}".encode())
    for name in names:
        path = root / name
        digest.update(name.encode())
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
    return digest.hexdigest()


def installed(component: str, root: Path = ROOT) -> bool:
    paths = ("backend/.venv/bin/python", "backend/.venv/bin/uvicorn") if component == "api" else (
        "frontend/node_modules/next/dist/bin/next", "frontend/node_modules/.modules.yaml",
    )
    return all((root / name).is_file() for name in paths)


def needs_sync(component: str, mode: str, root: Path = ROOT) -> bool:
    if mode == "skip":
        if not installed(component, root):
            raise DevError(f"{component} 依赖尚未安装；请运行 make setup 或使用 --sync auto。")
        return False
    stamp = read_json(root / ".dev/dependencies.json")
    return mode == "force" or not installed(component, root) or stamp.get(component) != dependency_fingerprint(
        component, root
    )


def pnpm_command(env: dict) -> list[str]:
    executable = shutil.which("pnpm")
    if executable:
        version = subprocess.run([executable, "--version"], env=env, capture_output=True, text=True, timeout=15, check=False)
        if version.returncode == 0 and version.stdout.strip() == PNPM_VERSION:
            return [executable]
    npm = shutil.which("npm")
    if not npm:
        raise DevError(f"需要 Node.js/npm；前端将通过项目缓存运行 pnpm@{PNPM_VERSION}，不做全局安装。")
    return [npm, "exec", "--yes", f"--package=pnpm@{PNPM_VERSION}", "--", "pnpm"]


def require_tools() -> tuple[str, str]:
    uv, node = shutil.which("uv"), shutil.which("node")
    if not uv or not node:
        raise DevError("需要先安装 uv 和 Node.js（建议 Node.js 22）；Python 虚拟环境由 uv 管理。")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False)
    try:
        major = int(version.stdout.strip().removeprefix("v").split(".")[0])
    except ValueError:
        major = 0
    if version.returncode or major < 22:
        raise DevError("前端开发需要 Node.js 22 或更新版本。")
    return uv, node


def log_tail(path: Path, limit: int = 25) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 12000))
            return "\n".join(handle.read().decode(errors="replace").splitlines()[-limit:])
    except OSError:
        return ""


def sync_dependencies(mode: str, root: Path = ROOT) -> None:
    uv, _ = require_tools()
    env = local_env(root)
    for component, label in (("api", "Python / uv"), ("web", "前端 / pnpm")):
        if not needs_sync(component, mode, root):
            print(f"  {label}：依赖已就绪（跳过同步）", flush=True)
            continue
        command = [uv, "sync", "--project", str(root / "backend"), "--locked", "--dev"] if component == "api" else (
            pnpm_command(env) + ["install", "--frozen-lockfile", "--store-dir", str(root / ".dev/pnpm-store")]
        )
        path = runtime(root) / f"setup-{component}.log"
        print(f"  {label}：同步锁定依赖…", flush=True)
        code = run_install(command, root / ("backend" if component == "api" else "frontend"),
                           {**env, "CI": "true"}, path, root)
        if code:
            raise DevError(f"{label} 同步失败；详见 {path}\n{log_tail(path)}")
        stamp = read_json(runtime(root) / "dependencies.json")
        stamp[component] = dependency_fingerprint(component, root)
        write_json(runtime(root) / "dependencies.json", stamp)


def health_ready(url: str, *, api: bool = False) -> bool:
    try:
        # A developer's proxy configuration must not intercept localhost probes.
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=2) as response:
            if response.status != 200:
                return False
            if api:
                return json.loads(response.read(65536)).get("status") == "ok"
            return True
    except (OSError, ValueError, urllib.error.URLError):
        return False


def readiness_diagnostics(api_ok: bool, web_ok: bool) -> str:
    lines = [f"API 健康检查：{'已就绪' if api_ok else '未就绪'}；前端 /tea 及 API 转发：{'已就绪' if web_ok else '未就绪'}。"]
    for name in ("api", "web"):
        summary = log_tail(runtime() / f"{name}.log", limit=8)[-3500:]
        lines.append(f"{name}.log（末尾摘要）：\n{summary or '暂无输出。'}")
    return "\n".join(lines)


class StartupWatcherLog:
    """Scan bounded chunks without losing earlier errors or split keywords."""

    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.tail = b""
        self.line_enospc = self.line_watch = False
        self.failed = self.complete = False

    def scan(self) -> bool:
        if self.failed:
            return True
        try:
            with self.path.open("rb") as handle:
                if handle.seek(0, os.SEEK_END) < self.offset:
                    self.offset = 0
                    self.tail = b""
                    self.line_enospc = self.line_watch = False
                handle.seek(self.offset)
                chunk = handle.read(65536)
                self.offset = handle.tell()
                self.complete = self.offset >= os.fstat(handle.fileno()).st_size
        except OSError:
            self.complete = False
            return False
        parts = chunk.split(b"\n")
        for index, part in enumerate(parts):
            text = self.tail + part.lower()
            self.line_enospc |= b"enospc" in text
            self.line_watch |= b"watch" in text
            if self.line_enospc and self.line_watch:
                self.failed = True
                return True
            # Keep only enough bytes to match a keyword crossing the next chunk.
            self.tail = text[-5:]
            if index < len(parts) - 1:
                self.tail = b""
                self.line_enospc = self.line_watch = False
        return False


def state_path(root: Path = ROOT) -> Path:
    return root / ".dev" / STATE_NAME


def service_commands(args: argparse.Namespace, root: Path = ROOT) -> list[tuple[str, Path, list[str]]]:
    uv, node = require_tools()
    api = [uv, "run", "--project", str(root / "backend"), "--locked", "--no-sync", "python", "-m", "uvicorn",
           "app.main:app", "--host", "127.0.0.1", "--port", str(args.api_port)]
    if not args.production:
        api += ["--reload", "--reload-dir", str(root / "backend/app")]
    web = [node, str(root / "frontend/node_modules/next/dist/bin/next"), "start" if args.production else "dev",
           "--hostname", "127.0.0.1", "--port", str(args.web_port)]
    if not args.production:
        web += ["--webpack"]
        options = node_option_names(os.environ.get("NODE_OPTIONS", ""))
        if options is not None and "--enable-source-maps" not in options:
            web += ["--disable-source-maps"]
    return [("api", root / "backend", api), ("web", root / "frontend", web)]


def serve(args: argparse.Namespace) -> int:
    token = os.environ.get("TEA_DEV_TOKEN")
    if not token or token != args.token or os.environ.get("TEA_DEV_ROOT") != str(ROOT):
        raise DevError("serve 是内部监管命令，请使用 make dev。")
    supervisor = process_identity(os.getpid())
    if supervisor is None:
        raise DevError("无法读取 Linux /proc 进程身份，拒绝启动无法安全管理的服务。")
    env = local_env()
    env.setdefault("TEA_DATA_DIR", str(ROOT / "data"))
    env["TEA_API_URL"] = f"http://127.0.0.1:{args.api_port}"
    env.setdefault("TEA_ALLOWED_ORIGINS", f"http://127.0.0.1:{args.web_port},http://localhost:{args.web_port}")
    state = {"owner": str(ROOT), "token": token, "status": "starting", "supervisor": supervisor,
             "services": [], "api_port": args.api_port, "web_port": args.web_port,
             "production": args.production, "started_at": time.time()}
    write_json(state_path(), state)
    processes: list[tuple[str, subprocess.Popen]] = []
    handles = []
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    code = 0
    api_ok = web_ok = False
    web_log_check = StartupWatcherLog(runtime() / "web.log")
    try:
        for name, cwd, command in service_commands(args):
            if stopping:
                break
            handle = (runtime() / f"{name}.log").open("wb", buffering=0)
            handles.append(handle)
            service_env = env
            if name == "web" and not args.production:
                # Next's config/directory watcher is separate from webpack's
                # watcher. The process-local setting covers both instances.
                service_env = {**env, "WATCHPACK_POLLING": env.get("WATCHPACK_POLLING", "1000")}
                node_options = env.get("NODE_OPTIONS", "")
                names = node_option_names(node_options)
                if (names is not None and not env.get("NEXT_DISABLE_MEM_OVERRIDE")
                        and not names & {"--max-old-space-size", "--max-old-space-size-percentage"}):
                    service_env["NODE_OPTIONS"] = node_options + (" " if node_options else "") + "--max-old-space-size=1536"
            process = subprocess.Popen(command, cwd=cwd, env=service_env, stdin=subprocess.DEVNULL,
                                       stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
            processes.append((name, process))
            record = process_identity(process.pid)
            if not record:
                raise DevError(f"{name} 在记录进程身份前退出；请查看 {name}.log。")
            state["services"].append(record)
            write_json(state_path(), state)
        deadline = time.monotonic() + START_TIMEOUT
        while not stopping:
            for name, process in processes:
                if process.poll() is not None:
                    api_ok = api_ok and name != "api"
                    web_ok = web_ok and name != "web"
                    raise DevError(f"{name} 服务退出，退出码 {process.returncode}。")
            if state["status"] == "starting":
                api_ok = health_ready(f"http://127.0.0.1:{args.api_port}/api/tea/health", api=True)
                web_ok = (health_ready(f"http://127.0.0.1:{args.web_port}/tea")
                          and health_ready(f"http://127.0.0.1:{args.web_port}/api/tea/health", api=True))
                if web_log_check.scan():
                    raise DevError("前端文件监听资源不足（ENOSPC），无法可靠热更新；已停止本次启动。"
                                   "请检查 WATCHPACK_POLLING 是否禁用了文件轮询。")
                if api_ok and web_ok and web_log_check.complete:
                    # Record the stable executable identity after uv/node bootstrap.
                    records = [process_identity(process.pid) for _, process in processes]
                    if not all(records):
                        raise DevError("服务在就绪检查时退出。")
                    state["services"] = records
                    state["status"] = "ready"
                    write_json(state_path(), state)
                    print("前后端已就绪。", flush=True)
                elif time.monotonic() >= deadline:
                    raise DevError(f"服务未在 {START_TIMEOUT:g} 秒内就绪。请查看 .dev/api.log 和 .dev/web.log。")
            time.sleep(0.25)
    except (DevError, OSError) as exc:
        state["error"] = f"{exc}\n{readiness_diagnostics(api_ok, web_ok)}"
        print(state["error"], file=sys.stderr, flush=True)
        code = 1
    finally:
        # Child handles prove these processes were created here. Refresh identities
        # before shutdown in case a launcher exec'd into its server binary.
        records = list(state["services"])
        for _, process in processes:
            if process.poll() is None:
                record = process_identity(process.pid)
                if record and record.get("token") == token and record.get("owner") == str(ROOT):
                    records.append(record)
        terminate(records)
        for _, process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for handle in handles:
            handle.close()
        state["status"] = "failed" if code else "stopped"
        # Do not overwrite a newer launcher in case the state file was replaced.
        if read_json(state_path()).get("token") == token:
            write_json(state_path(), state)
    return code


def stop_locked(root: Path = ROOT) -> None:
    state = read_json(state_path(root))
    if state and state.get("owner") != str(root):
        raise DevError("运行记录不属于当前工作目录；拒绝停止其中的进程。")
    supervisor = state.get("supervisor", {})
    signal_owned(supervisor, signal.SIGTERM, root)
    deadline = time.monotonic() + 15
    while owns_process(supervisor, root) and time.monotonic() < deadline:
        time.sleep(0.1)
    # Also clean owned service groups when a supervisor had already crashed.
    terminate(state.get("services", []), root)
    signal_owned(supervisor, signal.SIGKILL, root)
    if live_records(state, root):
        raise DevError("仍有本项目服务未停止，请查看 .dev 日志。")
    if state:
        state["status"] = "stopped"
        write_json(state_path(root), state)
    print("当前工作目录的服务已停止；业务数据与依赖已保留。", flush=True)



def check_production_build(api_port: int, root: Path = ROOT) -> None:
    if not (root / "frontend/.next/BUILD_ID").is_file():
        raise DevError("生产前端尚未构建，请先运行 make build。")
    manifest = read_json(root / "frontend/.next/routes-manifest.json")
    rewrites = manifest.get("rewrites", [])
    if isinstance(rewrites, dict):
        rewrites = [rule for phase in rewrites.values() for rule in phase if isinstance(phase, list)]
    expected = f"http://127.0.0.1:{api_port}/api/tea/:path*"
    if manifest and not any(rule.get("source") == "/api/tea/:path*" and rule.get("destination") == expected
                            for rule in rewrites):
        raise DevError(f"前端构建的 API 地址与当前端口不一致；请先 make build API_PORT={api_port}。")


def launch(args: argparse.Namespace) -> subprocess.Popen:
    current = read_json(state_path())
    if current and current.get("owner") != str(ROOT):
        raise DevError("运行记录属于其他工作目录；请检查 .dev/dev.json，不会自动接管。")
    if live_records(current):
        raise DevError("当前工作目录已在运行；使用 make dev-status 查看或 make dev-stop 停止。")
    available_port(args.api_port)
    available_port(args.web_port)
    if args.production:
        check_production_build(args.api_port)
    sync_dependencies(args.sync)
    token = secrets.token_hex(24)
    env = {**local_env(), "TEA_DEV_TOKEN": token, "TEA_DEV_ROOT": str(ROOT)}
    command = [sys.executable, str(ROOT / "scripts/dev.py"), "serve", "--token", token,
               "--api-port", str(args.api_port), "--web-port", str(args.web_port)]
    if args.production:
        command.append("--production")
    with (runtime() / "supervisor.log").open("wb") as handle:
        process = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                                   stdout=handle, stderr=subprocess.STDOUT, start_new_session=True)
    supervisor = process_identity(process.pid)
    if not args.production:
        polling = env.get("WATCHPACK_POLLING")
        mode = "文件轮询（每 1000 毫秒）" if polling is None else (
            "系统文件监听（沿用 WATCHPACK_POLLING 设置）" if polling in {"", "false", "0"}
            else "文件轮询（沿用 WATCHPACK_POLLING 设置）"
        )
        print(f"  前端监听：{mode}。", flush=True)
    print("  启动并检查前后端服务…", flush=True)
    try:
        deadline = time.monotonic() + START_TIMEOUT + 15
        while time.monotonic() < deadline:
            state = read_json(state_path())
            if state.get("token") == token and state.get("status") == "ready" and len(live_records(state)) == 3:
                print(f"茶渡工作台：http://127.0.0.1:{args.web_port}/tea", flush=True)
                print(f"日志：{ROOT / '.dev'}；停止：make dev-stop", flush=True)
                return process
            if process.poll() is not None or (state.get("token") == token and state.get("status") == "failed"):
                error = state.get("error") if state.get("token") == token else None
                raise DevError(f"启动失败。\n{error or log_tail(runtime() / 'supervisor.log')}")
            time.sleep(0.2)
        raise DevError("启动超时；已回收本次启动的进程。")
    except BaseException:
        if supervisor:
            signal_owned(supervisor, signal.SIGTERM)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            state = read_json(state_path())
            if state.get("token") == token:
                terminate(state.get("services", []))
            if supervisor:
                signal_owned(supervisor, signal.SIGKILL)
            process.wait(timeout=5)
        raise


def status(root: Path = ROOT) -> dict:
    state = read_json(state_path(root))
    live = live_records(state, root)
    running = state.get("status") == "ready" and len(live) == 3
    preparing = owns_launcher(read_json(startup_path(root)), root)
    return {"root": str(root), "status": "preparing" if preparing else "running" if running else "partial" if live else "stopped",
            "url": f"http://127.0.0.1:{state['web_port']}/tea" if state.get("web_port") else None,
            "api_port": state.get("api_port"), "web_port": state.get("web_port"),
            "pids": [record["pid"] for record in live], "error": state.get("error")}


def list_worktrees() -> None:
    result = subprocess.run(["git", "-C", str(ROOT), "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, check=False)
    roots = [Path(line.removeprefix("worktree ")) for line in result.stdout.splitlines()
             if line.startswith("worktree ")] if result.returncode == 0 else [ROOT]
    for root in roots:
        info = status(root.resolve())
        print(f"{info['status']:8} {info['root']}" + (f"  {info['url']}" if info['url'] else ""))


def clean_locked(root: Path = ROOT) -> None:
    state = read_json(state_path(root))
    if state and state.get("owner") != str(root):
        raise DevError("运行记录不属于当前工作目录，拒绝清理。")
    if live_records(state, root):
        raise DevError("服务仍在运行，请先使用 make dev-stop。")
    # Explicit allowlist: never recursive-delete .dev, data, environments or caches.
    for name in (*RUNTIME_FILES, "dev.tmp"):
        (root / ".dev" / name).unlink(missing_ok=True)
    print("运行记录与日志已清理；数据、配置、虚拟环境和依赖缓存均保留。")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="茶渡本地开发：按工作目录隔离的 uv + DeerFlow 服务")
    parser.add_argument("command", nargs="?", default="start", choices=("start", "stop", "status", "list", "clean", "setup", "serve"))
    parser.add_argument("--api-port", type=int)
    parser.add_argument("--web-port", type=int)
    parser.add_argument("--production", action="store_true", help="运行已构建的前端，并关闭后端自动重载")
    parser.add_argument("--foreground", action="store_true", help="保持前台等待，Ctrl+C 停止本次服务")
    parser.add_argument("--sync", choices=("auto", "force", "skip"), default=os.getenv("SYNC", "auto"))
    parser.add_argument("--token", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.sync not in {"auto", "force", "skip"}:
        parser.error("SYNC 必须是 auto、force 或 skip。")
    default_api, default_web = default_ports()
    try:
        args.api_port = args.api_port if args.api_port is not None else int(os.getenv("TEA_API_PORT", default_api))
        args.web_port = args.web_port if args.web_port is not None else int(os.getenv("TEA_WEB_PORT", default_web))
    except ValueError:
        parser.error("TEA_API_PORT 和 TEA_WEB_PORT 必须为整数。")
    if not all(1 <= port <= 65535 for port in (args.api_port, args.web_port)):
        parser.error("端口必须在 1–65535 之间。")
    if args.api_port == args.web_port:
        parser.error("前后端需要不同端口。")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    process = None
    previous_term_handler = None
    startup_token = None

    def request_cancel(_signum: int, _frame: object) -> None:
        raise KeyboardInterrupt

    # A launcher receives TERM separately from its detached supervisor. Route
    # cancellation through the same rollback used by Ctrl+C; serve owns its
    # own signal handlers and must not inherit this launcher behavior.
    if args.command in {"start", "setup"}:
        previous_term_handler = signal.signal(signal.SIGTERM, request_cancel)
    try:
        if args.command == "serve":
            return serve(args)
        if args.command == "status":
            print(json.dumps(status(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "list":
            list_worktrees()
            return 0
        if args.command in {"stop", "clean"}:
            cancel_startup()
        with control_lock(timeout=20 if args.command in {"stop", "clean"} else 0):
            if args.command in {"start", "setup"}:
                startup_token = begin_startup()
            if args.command == "setup":
                state = read_json(state_path())
                if state and state.get("owner") != str(ROOT):
                    raise DevError("运行记录不属于当前工作目录，拒绝安装依赖。")
                if live_records(state):
                    raise DevError("服务仍在运行，请先 make dev-stop 再安装依赖。")
                sync_dependencies("force" if args.sync == "auto" else args.sync)
            elif args.command == "stop":
                stop_locked()
            elif args.command == "clean":
                stop_locked()
                clean_locked()
            else:
                process = launch(args)
            if startup_token:
                finish_startup(startup_token)
                startup_token = None
        if process is not None and args.foreground:
            print("前台模式：Ctrl+C 停止本次启动的服务。", flush=True)
            return process.wait()
        return 0
    except KeyboardInterrupt:
        if process is not None:
            with control_lock():
                state = read_json(state_path())
                if state.get("supervisor", {}).get("pid") == process.pid:
                    stop_locked()
            return 0
        print("操作已取消。", file=sys.stderr)
        return 130
    except (DevError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if startup_token:
            finish_startup(startup_token)
        if previous_term_handler is not None:
            signal.signal(signal.SIGTERM, previous_term_handler)


if __name__ == "__main__":
    sys.exit(main())
