#!/usr/bin/env python3
"""Build and manage an explicit existing instance using the calling user's Docker."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.configuration.paths import InstancePaths  # noqa: E402
from app.configuration.environment import environment_lock, write_environment  # noqa: E402


def existing_instance(data: Path) -> Path:
    data = data.resolve(strict=True)
    if any(char in str(data) for char in ",\r\n\x00'"):
        raise ValueError("实例挂载路径不能包含逗号、换行、空字符或单引号。")
    database = data / "tea.sqlite3"
    with database.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise ValueError("指定目录不是已有的 AItea SQLite 实例。")
    if not (data / "files").is_dir() or not (data / "config/application.env").is_file():
        raise ValueError("实例必须包含 files/ 和 config/application.env；先按实例管理文档归集配置。")
    return data


def base_path(value: str) -> str:
    if not re.fullmatch(r"(?:/[A-Za-z0-9_-]+)+", value):
        raise ValueError("生产前缀必须是 /aitea 一类的路径，不包含末尾斜线、查询或转义。")
    return value


def public_origin(value: str) -> str:
    if any(char in value for char in "\r\n\x00"):
        raise ValueError("生产来源不能包含控制字符。")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})
    ):
        raise ValueError("生产来源使用 HTTPS 站点地址；仅本机测试允许 HTTP，不包含路径或凭据。")
    # Validate a malformed port before persisting settings.
    parsed.port
    return value.rstrip("/")


def port(value: str | int) -> int:
    result = int(value)
    if not 1024 <= result <= 65535:
        raise ValueError("生产端口须在 1024 至 65535 之间。")
    return result


def prepare(args, root=ROOT):
    data = existing_instance(args.data_dir or InstancePaths.resolve(root).data)
    paths = InstancePaths.resolve(root, environ=dict(os.environ) | {"TEA_DATA_DIR": str(data)})
    catalogs = (args.catalogs or paths.catalogs).resolve(strict=True)
    if not catalogs.is_dir() or any(char in str(catalogs) for char in ",\r\n\x00'"):
        raise ValueError("活动配置必须是已有的目录，路径不能含逗号、换行或引号。")
    origin = public_origin(args.origin)
    prefix = base_path(args.base_path)
    ports = [port(args.api_port), port(args.web_port), port(args.gateway_port)]
    if len(set(ports)) != 3:
        raise ValueError("API、前端和转发入口必须使用不同端口。")
    for value in (args.uid, args.gid):
        if value is not None and value < 0:
            raise ValueError("容器 UID/GID 必须为非负整数。")
    engine = getattr(args, "engine", "standard")
    uid = args.uid if args.uid is not None else (0 if engine == "gvisor" else os.getuid())
    gid = args.gid if args.gid is not None else (0 if engine == "gvisor" else os.getgid())
    validate_engine(engine, args.network, str(uid), str(gid))
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    values = {
        "TEA_INSTANCE_DATA": str(data),
        "TEA_INSTANCE_CONFIG": str(catalogs),
        "TEA_PUBLIC_ORIGIN": origin,
        "TEA_BASE_PATH": prefix,
        "TEA_UID": str(uid),
        "TEA_GID": str(gid),
        "TEA_API_PORT": str(ports[0]),
        "TEA_WEB_PORT": str(ports[1]),
        "TEA_GATEWAY_PORT": str(ports[2]),
        "TEA_COOKIE_SECURE": "true" if origin.startswith("https:") else "false",
        "TEA_IMAGE_TAG": revision[:12],
        "TEA_REVISION": revision,
        "TEA_COMPOSE_PROJECT": "aitea-production",
        "TEA_DOCKER_NETWORK": args.network,
        "TEA_DOCKER_ENGINE": engine,
    }
    path = args.config.resolve()
    if path.exists() and not args.replace:
        raise ValueError("部署参数已经存在；核对后使用 --replace 替换，不会修改业务或私有配置。")
    if path.exists():
        artifact = dotenv_values(path, interpolate=False).get("TEA_WEB_ARTIFACT")
        if artifact:
            values["TEA_WEB_ARTIFACT"] = artifact
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".deploy-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            for key, value in values.items():
                if any(char in value for char in "\r\n\x00'"):
                    raise ValueError("部署参数不能包含换行、空字符或单引号。")
                output.write(f"{key}='{value}'\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(json.dumps({"config": str(path), "data": str(data), "entry": origin + prefix}, ensure_ascii=False))
    return values


def settings(path: Path):
    values = {
        key: value for key, value in dotenv_values(path, interpolate=False).items() if value is not None
    }
    if not values:
        raise ValueError("尚未准备生产参数，请先执行 production-prepare。")
    for key in values:
        if key in os.environ:
            values[key] = os.environ[key]
    data = Path(values["TEA_INSTANCE_DATA"])
    if not data.is_absolute():
        raise ValueError("数据挂载必须使用绝对路径。")
    if not Path(values["TEA_INSTANCE_CONFIG"]).is_absolute():
        raise ValueError("活动配置挂载必须使用绝对路径。")
    base_path(values["TEA_BASE_PATH"])
    public_origin(values["TEA_PUBLIC_ORIGIN"])
    ports = [port(values[key]) for key in ("TEA_API_PORT", "TEA_WEB_PORT", "TEA_GATEWAY_PORT")]
    if len(set(ports)) != 3:
        raise ValueError("生产端口不能重复。")
    if values.get("TEA_DOCKER_NETWORK", "host") not in {"host", "bridge"}:
        raise ValueError("Docker 网络必须为 host 或 bridge。")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", values["TEA_IMAGE_TAG"]):
        raise ValueError("镜像标签无效。")
    for key in ("TEA_UID", "TEA_GID"):
        if not values[key].isdigit():
            raise ValueError("容器 UID/GID 必须为非负整数。")
    validate_engine(
        values.get("TEA_DOCKER_ENGINE", "standard"),
        values.get("TEA_DOCKER_NETWORK", "host"),
        values["TEA_UID"],
        values["TEA_GID"],
    )
    return values


def validate_engine(engine, network, uid, gid):
    if engine not in {"standard", "gvisor"}:
        raise ValueError("Docker 运行方式必须为 standard 或 gvisor。")
    if engine == "gvisor" and (network != "host" or uid != "0" or gid != "0"):
        raise ValueError("单 UID 的 Docker-in-gVisor 必须使用 host 网络与命名空间内 UID/GID 0。")


def docker_environment(root=ROOT):
    env = dict(os.environ)
    # Optional project-local CLI plugins, without changing the user's defaults.
    config = root / ".dev/production/docker-cli"
    if (config / "config.json").is_file() and "DOCKER_CONFIG" not in env:
        env["DOCKER_CONFIG"] = str(config)
        if "DOCKER_HOST" not in env and "DOCKER_CONTEXT" not in env:
            original = Path.home() / ".docker/config.json"
            current = json.loads(original.read_text()) if original.is_file() else {}
            env["DOCKER_CONTEXT"] = current.get("currentContext", "default")
    return env


def frontend_fingerprint(root=ROOT):
    paths = subprocess.check_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "frontend"],
        cwd=root,
    ).split(b"\0")
    digest = hashlib.sha256()
    for name in sorted(set(filter(None, paths))):
        if name.startswith(b"frontend/tests/"):
            continue
        path = root / os.fsdecode(name)
        if path.is_file():
            digest.update(name + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def api_upstream(values):
    return (
        "http://127.0.0.1:" + values["TEA_API_PORT"]
        if values.get("TEA_DOCKER_NETWORK", "host") == "host"
        else "http://api:8000"
    )


def package_web(values, config, *, root=ROOT):
    for name in (".env", ".env.local", ".env.production", ".env.production.local"):
        if (root / "frontend" / name).exists():
            raise ValueError("前端存在独立环境文件，请使用干净的源码容器构建，避免把私有配置打进产物。")
    node = subprocess.check_output(["node", "-p", "process.versions.node"], text=True).strip()
    if node.split(".")[0] != "22":
        raise ValueError("本机产物构建需要 Node.js 22，与生产镜像一致；从 make 入口使用项目工具链。")
    original = config.read_bytes()
    fingerprint = frontend_fingerprint(root)
    build_env = dict(os.environ) | {
        "NEXT_PUBLIC_TEA_BASE_PATH": values["TEA_BASE_PATH"],
        "NEXT_CONFIG_BUILD_OUTPUT": "standalone",
    }
    subprocess.run(
        ["make", "build", "TEA_API_URL=" + api_upstream(values)], cwd=root, env=build_env, check=True
    )
    if frontend_fingerprint(root) != fingerprint:
        raise ValueError("编译期间前端源码发生变化，请重建。")
    output = root / "frontend/.next"
    built = json.loads((output / "required-server-files.json").read_text())["config"]
    if built.get("basePath") != values["TEA_BASE_PATH"] or built.get("output") != "standalone":
        raise ValueError("前端产物前缀或输出格式不符。")
    rewrites = json.loads((output / "routes-manifest.json").read_text())["rewrites"]
    if isinstance(rewrites, dict):
        rewrites = [item for group in rewrites.values() for item in group]
    if not any(
        item.get("source") == values["TEA_BASE_PATH"] + "/api/tea/:path*"
        and item.get("destination") == api_upstream(values) + "/api/tea/:path*"
        for item in rewrites
    ):
        raise ValueError("前端固化的 API 地址与部署参数不同。")
    runtime = root / ".dev/production/web-artifacts"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifact = Path(tempfile.mkdtemp(prefix="web-" + fingerprint[:12] + "-", dir=runtime))
    shutil.copytree(output / "standalone", artifact, dirs_exist_ok=True, symlinks=True)
    shutil.copytree(output / "static", artifact / ".next/static", dirs_exist_ok=True, symlinks=True)
    shutil.copytree(root / "frontend/public", artifact / "public", dirs_exist_ok=True, symlinks=True)
    if any(path.is_symlink() and not path.resolve().is_relative_to(artifact) for path in artifact.rglob("*")):
        raise ValueError("前端产物含有指向包外的链接，不能封装。")
    metadata = {
        "schema": 1,
        "frontend_fingerprint": fingerprint,
        "base_path": values["TEA_BASE_PATH"],
        "api_url": api_upstream(values),
        "node_major": 22,
        "build_id": (output / "BUILD_ID").read_text().strip(),
    }
    (artifact / ".tea-build.json").write_text(json.dumps(metadata, indent=2) + "\n")
    with environment_lock(config):
        if config.read_bytes() != original:
            raise ValueError("编译期间部署参数发生变化，请核对后重建。")
        write_environment(config, original, {"TEA_WEB_ARTIFACT": str(artifact)})
    print(json.dumps({"artifact": str(artifact), "fingerprint": fingerprint}))


def web_artifact(values, *, root=ROOT):
    artifact = Path(values.get("TEA_WEB_ARTIFACT", "")).resolve()
    if not artifact.is_relative_to((root / ".dev/production/web-artifacts").resolve()):
        raise ValueError("尚未生成受管理的前端产物，请先执行 production-web-build。")
    metadata = json.loads((artifact / ".tea-build.json").read_text())
    if (
        metadata.get("schema") != 1
        or metadata.get("frontend_fingerprint") != frontend_fingerprint(root)
        or metadata.get("base_path") != values["TEA_BASE_PATH"]
        or metadata.get("api_url") != api_upstream(values)
        or metadata.get("node_major") != 22
        or not (artifact / "server.js").is_file()
    ):
        raise ValueError("前端产物已过期或配置不同，请重新执行 production-web-build。")
    return artifact


def compose_command(values, config, *, workers=False, root=ROOT):
    docker = shutil.which("docker")
    if not docker:
        raise ValueError("未找到当前用户的 Docker。")
    command = [docker, "compose", "--env-file", str(config.resolve()), "-f", str(root / "compose.yaml")]
    if values.get("TEA_DOCKER_NETWORK", "host") == "host":
        if sys.platform != "linux":
            raise ValueError("host 网络配方仅支持 Linux；其他系统请使用 bridge 并配置可达的外部服务。")
        command.extend(["-f", str(root / "deploy/compose.host.yaml")])
    if values.get("TEA_DOCKER_ENGINE", "standard") == "gvisor":
        validate_engine("gvisor", values["TEA_DOCKER_NETWORK"], values["TEA_UID"], values["TEA_GID"])
        command.extend(["-f", str(root / "deploy/compose.gvisor.yaml")])
    if workers:
        command.extend(["--profile", "workers"])
    return command


def image_build_command(command, values, services=()):
    if values.get("TEA_DOCKER_ENGINE", "standard") != "gvisor":
        return command + ["build", *services]
    # Bake requires the caller to grant the host network explicitly. All
    # interpolation values come from the already validated deployment settings.
    files = [command[index + 1] for index, value in enumerate(command) if value == "-f"]
    return (
        [command[0], "buildx", "bake", "--allow=network.host"]
        + [item for path in files for item in ("-f", path)]
        + list(services)
    )


def verify_mount(token_file: Path, token: str, data_dir: Path):
    data = existing_instance(data_dir)
    if token_file.parent != data / ".runtime" or token_file.read_text() != token:
        raise ValueError("Docker 守护进程看到的不是本机这个资料实例。")
    print("已确认 Docker 可读取同一份数据库、原件目录和私有配置。")


def preflight(values, *, root=ROOT, run=subprocess.run, env=None):
    data = existing_instance(Path(values["TEA_INSTANCE_DATA"]))
    runtime = data / ".runtime"
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, filename = tempfile.mkstemp(prefix=".production-mount-", dir=runtime)
    token, token_file = secrets.token_hex(24), Path(filename)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(token)
        command = [
            shutil.which("docker") or "docker",
            "run",
            "--rm",
            "--no-healthcheck",
            "--network",
            "host" if values.get("TEA_DOCKER_ENGINE", "standard") == "gvisor" else "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--user",
            values["TEA_UID"] + ":" + values["TEA_GID"],
            "--mount",
            f"type=bind,source={data},target={data},readonly",
            "--mount",
            f"type=bind,source={values['TEA_INSTANCE_CONFIG']},target={values['TEA_INSTANCE_CONFIG']},readonly",
            "--env",
            "TEA_DATA_DIR=" + str(data),
            "--entrypoint",
            "python",
            "aitea-api:" + values["TEA_IMAGE_TAG"],
            "/app/scripts/production.py",
            "verify-mount",
            "--token-file",
            str(token_file),
            "--token",
            token,
        ]
        result = run(
            command, env=env if env is not None else docker_environment(root), check=False, timeout=60
        )
        if result.returncode:
            raise ValueError(
                "Docker 无法访问同一份现有资料；请核对当前连接、数据挂载及 UID/GID。"
                "远端 Docker 的路径属于远端主机，启动已中止，未创建空库。"
            )
    finally:
        token_file.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["prepare", "web-build", "build", "check", "up", "status", "logs", "stop", "verify-mount"],
    )
    parser.add_argument("--config", type=Path, default=ROOT / ".dev/production/deploy.env")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--catalogs", type=Path)
    parser.add_argument("--origin", default="https://aerisun.top")
    parser.add_argument("--base-path", default="/aitea")
    parser.add_argument("--api-port", default=8000, type=port)
    parser.add_argument("--web-port", default=3000, type=port)
    parser.add_argument("--gateway-port", default=3080, type=port)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    parser.add_argument("--network", choices=["host", "bridge"], default="host")
    parser.add_argument("--engine", choices=["standard", "gvisor"], default="standard")
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--with-workers", action="store_true")
    parser.add_argument("--prebuilt-web", action="store_true")
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--token")
    args = parser.parse_args(argv)
    try:
        if args.action == "prepare":
            prepare(args)
            return 0
        if args.action == "verify-mount":
            if args.token_file is None or args.token is None:
                raise ValueError("缺少挂载验证标识。")
            verify_mount(args.token_file, args.token, Path(os.environ["TEA_DATA_DIR"]))
            return 0
        values = settings(args.config)
        if args.action == "web-build":
            package_web(values, args.config)
            return 0
        command = compose_command(values, args.config, workers=args.with_workers)
        env = docker_environment() | values
        subprocess.run(command + ["config", "--quiet"], env=env, check=True)
        if args.action in {"check", "up"}:
            preflight(values, env=env)
        if args.action == "check":
            return 0
        if args.action == "build" and args.prebuilt_web:
            artifact = web_artifact(values)
            subprocess.run(image_build_command(command, values, ("api", "gateway")), env=env, check=True)
            return subprocess.run(
                [
                    command[0],
                    "build",
                    "--target",
                    "prebuilt",
                    "--network",
                    "host" if values.get("TEA_DOCKER_NETWORK", "host") == "host" else "default",
                    "--file",
                    str(ROOT / "deploy/Dockerfile.web"),
                    "--build-arg",
                    "TEA_REVISION=" + values["TEA_REVISION"],
                    "--build-arg",
                    "TEA_BASE_PATH=" + values["TEA_BASE_PATH"],
                    "--tag",
                    "aitea-web:" + values["TEA_IMAGE_TAG"],
                    str(artifact),
                ],
                env=env,
                check=False,
            ).returncode
        if args.action == "build":
            return subprocess.run(
                image_build_command(command, values, ("api", "web", "gateway")), env=env, check=False
            ).returncode
        action = {
            "up": ["up", "-d", "--wait", "--wait-timeout", "180"],
            "status": ["ps"],
            "logs": ["logs", "--tail", "100"],
            "stop": ["stop"],
        }[args.action]
        return subprocess.run(command + action, env=env, check=False).returncode
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"生产操作未完成：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
