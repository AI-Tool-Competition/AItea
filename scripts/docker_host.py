#!/usr/bin/env python3
"""Manage a wenbo-owned Docker Engine through gVisor on the local host.

This is Docker-in-gVisor, not Docker's standard RootlessKit installation.
The outer processes use the calling user's single UID mapping. Docker uses the
host network driver and VFS storage; its socket is inside a private directory.
The installation receipt pins an independent rootfs and every runtime binary.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

CONTAINER = "wenbo-docker-host"
CAPABILITIES = [
    "CAP_" + name.upper()
    for name in (
        "audit_write",
        "chown",
        "dac_override",
        "fowner",
        "fsetid",
        "kill",
        "mknod",
        "net_bind_service",
        "net_admin",
        "net_raw",
        "setfcap",
        "setgid",
        "setpcap",
        "setuid",
        "sys_admin",
        "sys_chroot",
        "sys_ptrace",
    )
]


def read_installation(directory: Path):
    directory = directory.resolve(strict=True)
    if directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise ValueError("Docker runtime directory must be private and owned by this user")
    settings = json.loads((directory / "installation.json").read_text())
    if settings.get("directory") != str(directory) or settings.get("uid") != os.getuid():
        raise ValueError("Docker installation belongs to another directory or user")
    if settings.get("runtime") != "gvisor" or settings.get("network") != "host":
        raise ValueError("Unsupported Docker runtime or network configuration")
    for relative, expected in settings["binaries"].items():
        path = (directory / relative).resolve(strict=True)
        if not path.is_relative_to(directory / "bin") or not path.is_file():
            raise ValueError("Runtime binary is outside its recorded installation")
        digest = _digest(path)
        if digest != expected:
            raise ValueError("Runtime binary differs from its installation receipt: " + relative)
    if not (directory / "rootfs").is_dir() or (directory / "rootfs").is_symlink():
        raise ValueError("Docker base filesystem is missing or is a symlink")
    for mount in settings["mount_roots"]:
        path = Path(mount)
        if not path.is_absolute() or not path.is_dir() or path.resolve() != path:
            raise ValueError("Docker mount root is missing or not a canonical absolute directory")
    return directory, settings


def _digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_spec(directory, settings):
    mounts = [
        {"destination": "/proc", "type": "proc", "source": "proc"},
        {
            "destination": "/dev",
            "type": "tmpfs",
            "source": "tmpfs",
            "options": ["nosuid", "mode=755", "size=65536k"],
        },
        {
            "destination": "/dev/pts",
            "type": "devpts",
            "source": "devpts",
            "options": ["nosuid", "noexec", "newinstance", "ptmxmode=0666", "mode=0620"],
        },
        {
            "destination": "/sys",
            "type": "sysfs",
            "source": "sysfs",
            "options": ["nosuid", "noexec", "nodev", "ro"],
        },
        {
            "destination": "/sys/fs/cgroup",
            "type": "cgroup",
            "source": "cgroup",
            "options": ["nosuid", "noexec", "nodev", "rw"],
        },
        *(
            {
                "destination": target,
                "type": "tmpfs",
                "source": "tmpfs",
                "options": ["nosuid", "nodev", "mode=755"],
            }
            for target in ("/run", "/tmp")
        ),
    ]
    for source, target, readonly in (
        (directory / "bin", "/opt/docker", True),
        (directory / "state", "/dockerd-state", False),
        (directory / "resolv.conf", "/etc/resolv.conf", True),
        *((Path(path), path, False) for path in settings["mount_roots"]),
    ):
        mounts.append(
            {
                "destination": str(target),
                "type": "bind",
                "source": str(source),
                "options": ["rbind", "nosuid", "nodev", "ro" if readonly else "rw"],
            }
        )
    args = [
        "/opt/docker/dockerd",
        "--host=unix:///dockerd-state/run/docker.sock",
        "--data-root=/dockerd-state/data",
        # Runtime PIDs belong to this sandbox's PID namespace and must not
        # survive a restart; PID 1 always refers to the new dockerd instance.
        "--exec-root=/run/docker-exec",
        "--pidfile=/run/dockerd.pid",
        "--storage-driver=vfs",
        "--bridge=none",
        "--iptables=false",
        "--ip6tables=false",
        "--ip-forward=false",
        "--ip-masq=false",
        "--feature=containerd-snapshotter=false",
    ]
    environment = [
        "PATH=/opt/docker:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME=/root",
    ]
    private = directory / "environment.private.json"
    if private.exists():
        if private.stat().st_mode & 0o077:
            raise ValueError("Docker network environment must be private")
        for key, value in json.loads(private.read_text()).items():
            if key not in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"):
                raise ValueError("Unsupported Docker network environment key")
            environment.append(key + "=" + value)
    return {
        "ociVersion": "1.0.2",
        "root": {"path": str(directory / "rootfs"), "readonly": True},
        "hostname": CONTAINER,
        "mounts": mounts,
        "process": {
            "terminal": False,
            "user": {"uid": 0, "gid": 0},
            "args": args,
            "cwd": "/",
            "env": environment,
            "noNewPrivileges": True,
            "capabilities": {
                name: CAPABILITIES
                for name in ("bounding", "effective", "inheritable", "permitted", "ambient")
            },
            "rlimits": [{"type": "RLIMIT_NOFILE", "soft": 65536, "hard": 65536}],
        },
        "linux": {"namespaces": [{"type": name} for name in ("pid", "ipc", "uts", "mount")]},
    }


def runtime_prefix(directory):
    return [
        "/usr/bin/unshare",
        "-Urm",
        str(directory / "bin/runsc"),
        "--root=" + str(directory / "runsc-state"),
        "--rootless",
        "--platform=systrap",
        "--directfs=true",
        "--network=host",
        "--host-uds=all",
        "--overlay2=root:memory",
    ]


def engine_info(directory):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
    }
    result = subprocess.run(
        [
            str(directory / "bin/docker"),
            "--host=unix://" + str(directory / "state/run/docker.sock"),
            "info",
            "--format",
            "{{json .}}",
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=5,
    )
    if result.returncode:
        return None
    info = json.loads(result.stdout)
    if info.get("Name") != CONTAINER or info.get("DockerRootDir") != "/dockerd-state/data":
        raise ValueError("Docker endpoint belongs to another engine")
    return {
        "name": info["Name"],
        "version": info["ServerVersion"],
        "driver": info["Driver"],
        "runtime": "gvisor",
        "host_uid": os.getuid(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "stop", "check", "status"))
    parser.add_argument("--runtime-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    directory, settings = read_installation(args.runtime_dir)
    if args.command in ("check", "status"):
        deadline = time.monotonic() + (60 if args.command == "check" else 0)
        while True:
            info = engine_info(directory)
            if info:
                print(json.dumps(info))
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Local Docker Engine is not ready")
            time.sleep(0.5)
    prefix = runtime_prefix(directory)
    if args.command == "stop":
        subprocess.run(prefix + ["kill", CONTAINER, "TERM"], check=False, timeout=15)
        return
    lock = (directory / "service.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.set_inheritable(lock.fileno(), True)
    subprocess.run(prefix + ["delete", "--force", CONTAINER], capture_output=True, timeout=20)
    bundle = directory / "bundle"
    bundle.mkdir(mode=0o700, exist_ok=True)
    (bundle / "config.json").write_text(json.dumps(build_spec(directory, settings), indent=2))
    command = prefix + ["run", "--bundle=" + str(bundle), CONTAINER]
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
