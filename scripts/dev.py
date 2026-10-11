#!/usr/bin/env python3
"""Run only this checkout's API and DeerFlow frontend; stop both on exit."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]


def available_port(port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # Match the server's restart semantics: old TIME_WAIT connections are
        # harmless; a live listener still prevents bind (no SO_REUSEPORT).
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SystemExit(f"端口 {port} 已被占用，请指定其他端口；不会停止已有服务。") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="启动茶渡本地工作台（Ctrl+C 停止本次启动的服务）")
    parser.add_argument("--api-port", type=int, default=int(os.getenv("TEA_API_PORT", "8000")))
    parser.add_argument("--web-port", type=int, default=int(os.getenv("TEA_WEB_PORT", "3000")))
    parser.add_argument("--production", action="store_true", help="运行已经构建的前端")
    args = parser.parse_args()
    if not (1 <= args.api_port <= 65535 and 1 <= args.web_port <= 65535):
        parser.error("端口必须在 1–65535 之间")
    if args.api_port == args.web_port:
        parser.error("前后端需要不同端口")
    python = ROOT / "backend/.venv/bin/python"
    next_cli = ROOT / "frontend/node_modules/next/dist/bin/next"
    node = shutil.which("node")
    if not python.is_file() or not next_cli.is_file() or not node:
        raise SystemExit("依赖尚未安装，请先运行 make setup。")
    available_port(args.api_port)
    available_port(args.web_port)
    logs = ROOT / ".dev"
    logs.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.setdefault("TEA_DATA_DIR", str(ROOT / "data"))
    env["TEA_API_URL"] = f"http://127.0.0.1:{args.api_port}"
    env.setdefault("TEA_ALLOWED_ORIGINS", f"http://127.0.0.1:{args.web_port},http://localhost:{args.web_port}")
    env.setdefault("NEXT_TELEMETRY_DISABLED", "1")
    env.setdefault("SKIP_ENV_VALIDATION", "1")
    commands = [
        ("api", ROOT / "backend", [str(python), "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(args.api_port)]),
        ("web", ROOT / "frontend", [node, str(next_cli), "start" if args.production else "dev", "--hostname", "127.0.0.1", "--port", str(args.web_port)] + ([] if args.production else ["--webpack"])),
    ]
    processes: list[subprocess.Popen] = []
    handles = []
    stopped = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        for name, cwd, command in commands:
            handle = (logs / f"{name}.log").open("ab", buffering=0)
            handles.append(handle)
            processes.append(subprocess.Popen(command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True))
        print(f"茶渡工作台：http://127.0.0.1:{args.web_port}/tea", flush=True)
        print(f"日志：{logs}/api.log 和 {logs}/web.log；Ctrl+C 停止本次启动的两个服务。", flush=True)
        while not stopped:
            for (name, _, _), process in zip(commands, processes, strict=True):
                if process.poll() is not None:
                    print(f"{name} 服务退出，退出码 {process.returncode}。请查看 {logs / (name + '.log')}", file=sys.stderr)
                    return 1
            time.sleep(0.5)
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    sys.exit(main())
