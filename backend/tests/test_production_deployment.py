"""Deployment cannot accidentally create a replacement library on another Docker host."""

import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.interfaces.http.factory import create_app

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("production", ROOT / "scripts/production.py")
production = importlib.util.module_from_spec(spec)
spec.loader.exec_module(production)


@pytest.fixture
def instance(tmp_path):
    data = tmp_path / "existing data"
    (data / "files").mkdir(parents=True)
    (data / "config").mkdir()
    (data / "config/catalogs").mkdir()
    (data / "config/application.env").write_text("AGENT_API_KEY='private-should-not-leak'\n")
    with sqlite3.connect(data / "tea.sqlite3") as conn:
        conn.execute("CREATE TABLE original_documents(id TEXT)")
        conn.execute("INSERT INTO original_documents VALUES ('kept')")
    return data


def arguments(instance, tmp_path, **changes):
    return SimpleNamespace(
        **{
            "data_dir": instance,
            "catalogs": None,
            "origin": "https://example.com",
            "base_path": "/aitea",
            "api_port": 8000,
            "web_port": 3000,
            "gateway_port": 3080,
            "uid": 1005,
            "gid": 1005,
            "network": "host",
            "config": tmp_path / "runtime/deploy.env",
            "replace": False,
            **changes,
        }
    )


def test_prepare_preserves_database_and_never_copies_or_prints_keys(instance, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(production.subprocess, "check_output", lambda *a, **k: "a" * 40 + "\n")
    before = (instance / "tea.sqlite3").read_bytes()
    args = arguments(instance, tmp_path)
    values = production.prepare(args, root=tmp_path)
    assert values["TEA_INSTANCE_DATA"] == str(instance)
    assert production.settings(args.config)["TEA_UID"] == "1005"
    assert args.config.stat().st_mode & 0o777 == 0o600
    assert (instance / "tea.sqlite3").read_bytes() == before
    assert "private-should-not-leak" not in args.config.read_text() + capsys.readouterr().out
    with pytest.raises(ValueError, match="已经存在"):
        production.prepare(args, root=tmp_path)


def test_missing_instance_is_not_created(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        production.existing_instance(missing)
    assert not missing.exists()


def test_single_uid_engine_prepares_namespace_root_without_changing_host_files(
    instance, tmp_path, monkeypatch
):
    monkeypatch.setattr(production.subprocess, "check_output", lambda *a, **k: "a" * 40 + "\n")
    owner = (instance / "tea.sqlite3").stat().st_uid
    args = arguments(instance, tmp_path, engine="gvisor", uid=None, gid=None)
    values = production.prepare(args, root=tmp_path)
    assert values["TEA_UID"] == values["TEA_GID"] == "0"
    assert production.settings(args.config)["TEA_DOCKER_ENGINE"] == "gvisor"
    command = production.compose_command(values, args.config, root=tmp_path)
    assert str(tmp_path / "deploy/compose.gvisor.yaml") in command
    assert (instance / "tea.sqlite3").stat().st_uid == owner


@pytest.mark.parametrize(
    "changes", [{"network": "bridge"}, {"uid": 1000}, {"gid": 1000}, {"engine": "unknown"}]
)
def test_unsupported_single_uid_settings_fail_before_writing_config(instance, tmp_path, changes):
    options = {"engine": "gvisor", "uid": 0, "gid": 0} | changes
    args = arguments(instance, tmp_path, **options)
    with pytest.raises(ValueError):
        production.prepare(args, root=tmp_path)
    assert not args.config.exists()


def test_modified_single_uid_config_cannot_start_with_unmapped_user(instance, tmp_path, monkeypatch):
    monkeypatch.setattr(production.subprocess, "check_output", lambda *a, **k: "a" * 40 + "\n")
    args = arguments(instance, tmp_path, engine="gvisor", uid=0, gid=0)
    production.prepare(args, root=tmp_path)
    args.config.write_text(args.config.read_text().replace("TEA_UID='0'", "TEA_UID='1000'"))
    with pytest.raises(ValueError, match="单 UID"):
        production.settings(args.config)


def test_single_uid_build_grants_only_host_network_and_reuses_compose_files():
    command = [
        "docker",
        "compose",
        "--env-file",
        "private-deployment.env",
        "-f",
        "compose.yaml",
        "-f",
        "host.yaml",
        "-f",
        "gvisor.yaml",
    ]

    assert production.image_build_command(command, {}, ("api",)) == command + ["build", "api"]
    result = production.image_build_command(command, {"TEA_DOCKER_ENGINE": "gvisor"}, ("api", "gateway"))
    assert result == [
        "docker",
        "buildx",
        "bake",
        "--allow=network.host",
        "-f",
        "compose.yaml",
        "-f",
        "host.yaml",
        "-f",
        "gvisor.yaml",
        "api",
        "gateway",
    ]


@pytest.mark.parametrize("network,build_network", [("host", "host"), ("bridge", "default")])
def test_prebuilt_image_uses_supported_buildkit_network(network, build_network, tmp_path, monkeypatch):
    values = {
        "TEA_DOCKER_NETWORK": network,
        "TEA_REVISION": "a" * 40,
        "TEA_IMAGE_TAG": "candidate",
        "TEA_BASE_PATH": "/aitea",
    }
    calls = []
    monkeypatch.setattr(production, "settings", lambda path: values)
    monkeypatch.setattr(production, "compose_command", lambda *a, **k: ["docker", "compose"])
    monkeypatch.setattr(production, "docker_environment", lambda: {})
    monkeypatch.setattr(production, "web_artifact", lambda values: tmp_path)

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(production.subprocess, "run", run)
    assert production.main(["build", "--prebuilt-web"]) == 0
    command = calls[-1]
    assert command[command.index("--network") + 1] == build_network


@pytest.mark.parametrize("allow_demo", [None, "true"])
def test_production_anonymous_access_requires_explicit_opt_in(tmp_path, monkeypatch, allow_demo):
    monkeypatch.setenv("TEA_ENV", "production")
    monkeypatch.setenv("TEA_COOKIE_SECURE", "false")
    if allow_demo is None:
        monkeypatch.delenv("TEA_ALLOW_DEMO", raising=False)
    else:
        monkeypatch.setenv("TEA_ALLOW_DEMO", allow_demo)
    with TestClient(create_app(tmp_path / "store")) as client:
        response = client.post("/api/tea/auth/demo")
        assert response.status_code == (200 if allow_demo else 403)
        if allow_demo is None:
            assert response.json()["error"]["code"] == "demo_disabled"
            assert client.get("/api/tea/auth/me").status_code == 401


@pytest.mark.parametrize("failure", ["wrong_host", "timeout", "permission"])
def test_failed_remote_mount_stops_before_start_and_cleans_probe(instance, failure, monkeypatch):
    monkeypatch.setattr(production, "docker_environment", lambda *args: {})

    def run(command, **kwargs):
        assert "--read-only" in command and "--network" in command
        assert "private-should-not-leak" not in json.dumps(command)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 60)
        if failure == "permission":
            raise PermissionError("cannot read")
        return SimpleNamespace(returncode=125)

    values = {
        "TEA_INSTANCE_DATA": str(instance),
        "TEA_INSTANCE_CONFIG": str(instance / "config/catalogs"),
        "TEA_UID": "1005",
        "TEA_GID": "1005",
        "TEA_IMAGE_TAG": "candidate",
    }
    with pytest.raises((ValueError, PermissionError, subprocess.TimeoutExpired)):
        production.preflight(values, run=run)
    assert not list((instance / ".runtime").glob(".production-mount-*"))
    with sqlite3.connect(instance / "tea.sqlite3") as conn:
        assert conn.execute("SELECT id FROM original_documents").fetchone()[0] == "kept"


def test_mount_probe_requires_same_live_host_token(instance, tmp_path):
    runtime = instance / ".runtime"
    runtime.mkdir()
    probe = runtime / ".production-mount-test"
    probe.write_text("expected")
    production.verify_mount(probe, "expected", instance)
    with pytest.raises(ValueError, match="不是本机"):
        production.verify_mount(probe, "other", instance)
    foreign = tmp_path / "foreign-token"
    foreign.write_text("expected")
    with pytest.raises(ValueError, match="不是本机"):
        production.verify_mount(foreign, "expected", instance)


@pytest.mark.parametrize(
    "prefix", ["//aitea", "/aitea/", "/aitea?secret", "/aitea/../admin", "https://example.com"]
)
def test_invalid_public_prefix_is_rejected(prefix):
    with pytest.raises(ValueError):
        production.base_path(prefix)


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://user:key@example.com",
        "https://example.com/path",
        "https://example.com?key=value",
        "https://exam\nple.com",
    ],
)
def test_origin_cannot_hide_credentials_or_change_routes(origin):
    with pytest.raises(ValueError):
        production.public_origin(origin)


def test_prefixed_secure_cookie_logs_in_and_logs_out_without_leaking_to_other_site_paths(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TEA_COOKIE_PATH", "/aitea")
    monkeypatch.setenv("TEA_COOKIE_SECURE", "true")
    app = FastAPI()
    app.mount("/aitea", create_app(tmp_path / "store"))

    @app.get("/other-application")
    def other(request: Request):
        return {"cookie": request.headers.get("cookie")}

    with TestClient(app, base_url="https://example.com") as client:
        response = client.post("/aitea/api/tea/auth/demo")
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "Path=/aitea" in cookie and "Secure" in cookie and "HttpOnly" in cookie
        assert client.get("/aitea/api/tea/auth/me").status_code == 200
        assert client.get("/other-application").json()["cookie"] is None
        response = client.post("/aitea/api/tea/auth/logout")
        assert response.status_code == 204 and "Path=/aitea" in response.headers["set-cookie"]
        assert client.get("/aitea/api/tea/auth/me").status_code == 401


@pytest.mark.parametrize("path", ["//example.com", "/aitea; Secure", "/aitea\n", "aitea"])
def test_invalid_cookie_scope_fails_at_startup(tmp_path, monkeypatch, path):
    monkeypatch.setenv("TEA_COOKIE_PATH", path)
    with pytest.raises(ValueError, match="TEA_COOKIE_PATH"):
        create_app(tmp_path / "store")


def test_user_docker_context_change_is_not_hidden_by_local_plugins(tmp_path, monkeypatch):
    local = tmp_path / "project/.dev/production/docker-cli"
    local.mkdir(parents=True)
    (local / "config.json").write_text('{"currentContext":"old-vm"}')
    user = tmp_path / "user"
    (user / ".docker").mkdir(parents=True)
    (user / ".docker/config.json").write_text('{"currentContext":"wenbo-local"}')
    monkeypatch.setattr(production.Path, "home", lambda: user)
    for key in ("DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_HOST"):
        monkeypatch.delenv(key, raising=False)
    env = production.docker_environment(tmp_path / "project")
    assert env["DOCKER_CONTEXT"] == "wenbo-local"
    monkeypatch.setenv("DOCKER_HOST", "unix:///explicit.sock")
    env = production.docker_environment(tmp_path / "project")
    assert "DOCKER_CONTEXT" not in env and env["DOCKER_HOST"] == "unix:///explicit.sock"
    monkeypatch.setenv("DOCKER_CONFIG", "/explicit/config")
    assert production.docker_environment(tmp_path / "project")["DOCKER_CONFIG"] == "/explicit/config"


def test_up_never_starts_services_after_failed_mount(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(production, "settings", lambda path: {})
    monkeypatch.setattr(production, "compose_command", lambda *a, **k: ["docker", "compose"])
    monkeypatch.setattr(production, "docker_environment", lambda: {})
    monkeypatch.setattr(production.subprocess, "run", lambda command, **kw: calls.append(command))

    def fail_mount(values, **kwargs):
        raise ValueError("wrong Docker host")

    monkeypatch.setattr(production, "preflight", fail_mount)
    assert production.main(["up", "--config", str(tmp_path / "deploy.env")]) == 1
    assert calls == [["docker", "compose", "config", "--quiet"]]


@pytest.fixture
def frontend_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    source = tmp_path / "frontend/page.tsx"
    source.parent.mkdir()
    source.write_text("export default function Page() { return null; }\n")
    artifact = tmp_path / ".dev/production/web-artifacts/test"
    artifact.mkdir(parents=True)
    (artifact / "server.js").write_text("console.log('fixture')")
    metadata = {
        "schema": 1,
        "frontend_fingerprint": production.frontend_fingerprint(tmp_path),
        "base_path": "/aitea",
        "api_url": "http://127.0.0.1:8000",
        "node_major": 22,
    }
    (artifact / ".tea-build.json").write_text(json.dumps(metadata))
    values = {
        "TEA_WEB_ARTIFACT": str(artifact),
        "TEA_BASE_PATH": "/aitea",
        "TEA_API_PORT": "8000",
        "TEA_DOCKER_NETWORK": "host",
    }
    return artifact, source, metadata, values


@pytest.mark.parametrize("change", ["source", "prefix", "api", "node", "outside"])
def test_stale_or_foreign_web_build_cannot_be_packaged(frontend_artifact, tmp_path, change):
    artifact, source, metadata, values = frontend_artifact
    assert production.web_artifact(values, root=tmp_path) == artifact
    if change == "source":
        source.write_text("export default function Changed() { return 'changed'; }\n")
    elif change == "prefix":
        values["TEA_BASE_PATH"] = "/another-app"
    elif change == "api":
        values["TEA_API_PORT"] = "8001"
    elif change == "node":
        metadata["node_major"] = 24
        (artifact / ".tea-build.json").write_text(json.dumps(metadata))
    else:
        values["TEA_WEB_ARTIFACT"] = str(tmp_path / "unmanaged-artifact")
    with pytest.raises(ValueError):
        production.web_artifact(values, root=tmp_path)


def test_native_web_build_refuses_private_frontend_environment_before_running(tmp_path, monkeypatch):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend/.env.production.local").write_text("PRIVATE_KEY=synthetic\n")
    monkeypatch.setattr(production.subprocess, "check_output", lambda *a, **k: pytest.fail("build started"))
    with pytest.raises(ValueError, match="独立环境文件"):
        production.package_web({}, tmp_path / "deploy.env", root=tmp_path)


def test_test_only_changes_do_not_invalidate_runtime_artifact(frontend_artifact, tmp_path):
    artifact, _, _, values = frontend_artifact
    tests = tmp_path / "frontend/tests"
    tests.mkdir()
    (tests / "fixture.test.ts").write_text("test-only source")
    assert production.web_artifact(values, root=tmp_path) == artifact


def test_native_web_build_detects_source_changes_during_compile(frontend_artifact, tmp_path, monkeypatch):
    _, source, _, values = frontend_artifact
    config = tmp_path / "deploy.env"
    config.write_text("TEA_BASE_PATH=/aitea\n")

    def output(command, *args, **kwargs):
        return "22.23.0\n" if command[0] == "node" else b"frontend/page.tsx\0"

    monkeypatch.setattr(production.subprocess, "check_output", output)
    monkeypatch.setattr(production.subprocess, "run", lambda *args, **kwargs: source.write_text("changed"))
    with pytest.raises(ValueError, match="源码发生变化"):
        production.package_web(values, config, root=tmp_path)
    assert config.read_text() == "TEA_BASE_PATH=/aitea\n"
