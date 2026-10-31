"""The local engine must keep its pinned runtime and host-user boundary."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "docker_host", Path(__file__).resolve().parents[2] / "scripts/docker_host.py"
)
docker_host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docker_host)


@pytest.fixture
def installation(tmp_path):
    directory = tmp_path / "runtime"
    directory.mkdir(mode=0o700)
    (directory / "bin").mkdir()
    (directory / "bin/runsc").write_bytes(b"pinned-runtime")
    (directory / "rootfs").mkdir()
    mount = tmp_path / "existing-data"
    mount.mkdir()
    settings = {
        "directory": str(directory),
        "uid": os.getuid(),
        "runtime": "gvisor",
        "network": "host",
        "binaries": {"bin/runsc": hashlib.sha256(b"pinned-runtime").hexdigest()},
        "mount_roots": [str(mount)],
    }
    (directory / "installation.json").write_text(json.dumps(settings))
    return directory, settings, mount


def test_changed_runtime_binary_is_rejected(installation):
    directory, _, _ = installation
    docker_host.read_installation(directory)
    (directory / "bin/runsc").write_bytes(b"replaced")
    with pytest.raises(ValueError, match="differs"):
        docker_host.read_installation(directory)


def test_foreign_or_public_installation_is_rejected(installation):
    directory, settings, _ = installation
    directory.chmod(0o755)
    with pytest.raises(ValueError, match="private"):
        docker_host.read_installation(directory)
    directory.chmod(0o700)
    settings["uid"] += 1
    (directory / "installation.json").write_text(json.dumps(settings))
    with pytest.raises(ValueError, match="another"):
        docker_host.read_installation(directory)


def test_runtime_and_data_cannot_redirect_through_symlinks(installation, tmp_path):
    directory, settings, mount = installation
    other = tmp_path / "other-runtime"
    other.write_bytes(b"pinned-runtime")
    (directory / "bin/runsc").unlink()
    (directory / "bin/runsc").symlink_to(other)
    with pytest.raises(ValueError, match="outside"):
        docker_host.read_installation(directory)
    (directory / "bin/runsc").unlink()
    (directory / "bin/runsc").write_bytes(b"pinned-runtime")
    mount.rmdir()
    mount.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="canonical"):
        docker_host.read_installation(directory)


def test_guest_root_only_mounts_recorded_host_data_and_keeps_base_read_only(installation):
    directory, settings, mount = installation
    result = docker_host.build_spec(directory, settings)
    assert result["root"]["readonly"] is True
    assert result["process"]["user"] == {"uid": 0, "gid": 0}
    assert result["process"]["noNewPrivileges"] is True
    mounts = {entry["destination"]: entry for entry in result["mounts"]}
    assert mounts[str(mount)]["source"] == str(mount)
    assert "rw" in mounts[str(mount)]["options"]
    assert "ro" in mounts["/opt/docker"]["options"]
    assert not any(entry["source"] == "/" for entry in result["mounts"])
    args = result["process"]["args"]
    assert "--bridge=none" in args and "--iptables=false" in args
    assert "--exec-root=/run/docker-exec" in args and "--pidfile=/run/dockerd.pid" in args
    prefix = docker_host.runtime_prefix(directory)
    assert prefix[:2] == ["/usr/bin/unshare", "-Urm"]
    assert "--network=host" in prefix


def test_private_proxy_environment_does_not_accept_arbitrary_daemon_settings(installation):
    directory, settings, _ = installation
    private = directory / "environment.private.json"
    private.write_text(json.dumps({"HTTP_PROXY": "http://proxy.invalid:3128"}))
    private.chmod(0o600)
    result = docker_host.build_spec(directory, settings)
    assert "HTTP_PROXY=http://proxy.invalid:3128" in result["process"]["env"]
    private.write_text(json.dumps({"DOCKER_HOST": "tcp://public.invalid:2375"}))
    with pytest.raises(ValueError, match="Unsupported"):
        docker_host.build_spec(directory, settings)
