#!/usr/bin/env python3
"""Prepare/start this checkout's pinned RAGFlow without changing global Docker state."""

from __future__ import annotations

import argparse
import os
import sys
import secrets
import shutil
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.configuration.paths import InstancePaths  # noqa: E402
from app.configuration.environment import read_environment  # noqa: E402

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def prepare(root=ROOT):
    root = Path(root).resolve()
    paths = InstancePaths.resolve(root)
    runtime = paths.ragflow_config
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = runtime / ".env"
    existing = dotenv_values(path, interpolate=False) if path.exists() else {}
    application = read_environment(root)
    values = {
        **{
            key: existing.get(key) or secrets.token_hex(24)
            for key in (
                "MYSQL_PASSWORD",
                "MINIO_PASSWORD",
                "REDIS_PASSWORD",
                "RAGFLOW_SERVICE_PASSWORD",
            )
        },
        "MINIO_USER": "aitea-ragflow",
        "RAGFLOW_SERVICE_EMAIL": existing.get("RAGFLOW_SERVICE_EMAIL") or "aitea-service@localhost.invalid",
        "AITEA_RAGFLOW_ENV_FILE": str(path),
        "AITEA_RAGFLOW_RUNTIME": str(runtime),
        "AITEA_RAGFLOW_DATA": str(paths.ragflow_data),
        "AITEA_RAGFLOW_API_PORT": application.get("RAGFLOW_API_PORT") or "19380",
        "AITEA_RAGFLOW_WEB_PORT": application.get("RAGFLOW_WEB_PORT") or "18080",
        "AITEA_EMBEDDING_API_KEY": application.get("EMBEDDING_API_KEY")
        or application.get("LLM_API_KEY")
        or "",
        "AITEA_EMBEDDING_BASE_URL": application.get("EMBEDDING_BASE_URL")
        or application.get("LLM_BASE_URL")
        or "",
        "AITEA_EMBEDDING_MODEL": application.get("EMBEDDING_MODEL") or "",
        "AITEA_RERANK_API_KEY": application.get("RERANK_API_KEY") or "",
        "AITEA_RERANK_BASE_URL": application.get("RERANK_BASE_URL") or "",
        "AITEA_RERANK_MODEL": application.get("RERANK_MODEL") or "",
        "RAGFLOW_API_KEY": existing.get("RAGFLOW_API_KEY") or "",
    }
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("RAGFlow 配置不允许换行。")
    # Compose single quotes keep API keys literal instead of interpolating '$'.
    path.write_text("".join(key + "='" + value.replace("'", "\\'") + "'\n" for key, value in values.items()))
    os.chmod(path, 0o600)
    shutil.copyfile(
        ROOT / "deploy/ragflow/service_conf.yaml.template",
        runtime / "service_conf.yaml.template",
    )
    for name in ("mysql", "minio", "kvrocks", "nats", "infinity", "logs"):
        (paths.ragflow_data / name).mkdir(parents=True, exist_ok=True)
    print("RAGFlow 私有配置已准备，embedding 和 rerank 沿用后端接口和密钥。")
    return path


def compose(action, root=ROOT, runtime="auto"):
    root = Path(root).resolve()
    if action in {"up", "config"}:
        env_file = prepare(root)
    else:
        env_file = InstancePaths.resolve(root).ragflow_config / ".env"
        if not env_file.exists():
            print("当前项目尚未启动 RAGFlow。")
            return 0
    docker = shutil.which("docker")
    if runtime == "user" or (
        runtime == "auto" and (not docker or (root / ".dev/ragflow/processes.json").exists())
    ):
        from ragflow_user import UserRuntime

        manager = UserRuntime(root, ROOT)
        if action == "config":
            manager._prepare()
            return 0
        if action == "up":
            previous = {name for name in manager.state if manager.alive(name)}
            try:
                return manager.up()
            except Exception:
                manager.stop(only=set(manager.state) - previous)
                raise
        return manager.stop() if action == "stop" else manager.status()
    if not docker:
        raise RuntimeError("未找到指定的 Docker 运行环境。可选择 --runtime user。")
    command = [
        docker,
        "compose",
        "--project-name",
        "aitea-ragflow-" + __import__("hashlib").sha256(str(root).encode()).hexdigest()[:8],
        "--env-file",
        str(env_file),
        "-f",
        str(ROOT / "deploy/ragflow/compose.yaml"),
    ]
    if action == "up":
        command += ["up", "-d", "--wait", "--wait-timeout", "300"]
    elif action == "stop":
        command += ["stop"]
    else:
        command += ["ps"]
    return subprocess.run(command, cwd=root, check=False).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "up", "stop", "status", "config", "bootstrap"))
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--runtime", choices=("auto", "user", "docker"), default="auto")
    args = parser.parse_args()
    if args.action == "bootstrap":
        from ragflow_bootstrap import bootstrap

        return bootstrap(args.root)
    if args.action == "prepare":
        prepare(args.root)
        return 0
    return compose(args.action, args.root, args.runtime)


if __name__ == "__main__":
    raise SystemExit(main())
