"""Provider registration uses controlled HTTP and never calls paid models."""

import json
import sys
from types import SimpleNamespace

import httpx
import pytest
from dotenv import dotenv_values

from app.configuration.paths import PROJECT_ROOT, InstancePaths

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
import ragflow_bootstrap  # noqa: E402


@pytest.fixture
def registration(tmp_path, monkeypatch):
    monkeypatch.delenv("TEA_ENV_FILE", raising=False)
    monkeypatch.delenv("TEA_DATA_DIR", raising=False)
    monkeypatch.delenv("TEA_CONFIG_DIR", raising=False)
    values = {
        "EMBEDDING_BASE_URL": "https://embed.example/v1",
        "EMBEDDING_API_KEY": "embedding-secret",
        "EMBEDDING_MODEL": "cloud-embedding",
        "RERANK_BASE_URL": "https://rerank.example/v1",
        "RERANK_API_KEY": "rerank-secret",
        "RERANK_MODEL": "cloud-rerank",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    paths = InstancePaths.resolve(tmp_path)
    paths.ragflow_config.mkdir(parents=True)
    (paths.ragflow_config / ".env").write_text("RAGFLOW_API_KEY='known-backend-key'\n")
    (paths.ragflow_config / "conf").mkdir()
    (paths.ragflow_config / "conf/private.pem").write_text("-----BEGIN RSA PRIVATE KEY-----\nfixture")
    paths.env_file.write_text("# unrelated settings retained\nUNRELATED='keep'\n")
    monkeypatch.setattr(
        ragflow_bootstrap.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout=b"fixture-encrypted"),
    )
    calls, instances = [], {}

    def handle(request):
        path = request.url.path.removeprefix("/api/v1")
        payload = json.loads(request.content) if request.content else None
        calls.append((request.method, path, payload))
        data = {}
        if path == "/auth/login":
            return httpx.Response(
                200, json={"code": 0, "data": {}}, headers={"Authorization": "fixture-session"}
            )
        if path == "/providers":
            data = [{"name": "OpenAI-API-Compatible"}, {"name": "VLLM"}]
        elif "/instances" in path:
            provider = path.split("/")[2]
            if request.method == "GET":
                data = [item for (name, _), item in instances.items() if name == provider]
            else:
                instances[(provider, payload["instance_name"])] = payload
        elif path == "/models":
            data = [
                {
                    "name": item["model_info"][0]["model_name"],
                    "provider_name": provider,
                    "instance_name": instance,
                    "model_type": item["model_info"][0]["model_type"],
                    "model_id": instance + "-model-id",
                }
                for (provider, instance), item in instances.items()
            ]
        elif path == "/datasets":
            data = []
        elif path != "/models/default":
            raise AssertionError((request.method, path))
        return httpx.Response(200, json={"code": 0, "data": data})

    original_client = httpx.Client
    monkeypatch.setattr(
        ragflow_bootstrap.httpx,
        "Client",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle)),
    )
    return tmp_path, paths, calls, instances


def test_bootstrap_refreshes_settings_and_registers_separate_remote_models(registration):
    root, paths, calls, instances = registration
    assert ragflow_bootstrap.bootstrap(root) == 0
    assert instances[("OpenAI-API-Compatible", "aitea_cloud")]["api_key"] == "embedding-secret"
    rerank = instances[("VLLM", "aitea_rerank")]
    assert rerank["base_url"] == "https://rerank.example/v1"
    assert rerank["api_key"] == "rerank-secret"
    assert rerank["model_info"][0]["model_type"] == ["rerank"]
    saved = dotenv_values(paths.env_file, interpolate=False)
    assert saved["RAGFLOW_EMBEDDING_MODEL"] == "aitea_cloud-model-id"
    assert saved["RAGFLOW_RERANK_MODEL"] == "aitea_rerank-model-id"
    assert saved["UNRELATED"] == "keep"
    assert paths.env_file.stat().st_mode & 0o777 == 0o600
    first_registrations = [call for call in calls if call[0] in {"POST", "PUT"} and "/instances" in call[1]]
    assert len(first_registrations) == 2
    calls.clear()
    assert ragflow_bootstrap.bootstrap(root) == 0
    assert not [call for call in calls if call[0] in {"POST", "PUT"} and "/instances" in call[1]]


def test_rotated_rerank_key_updates_existing_instance(registration, monkeypatch):
    root, _paths, calls, instances = registration
    ragflow_bootstrap.bootstrap(root)
    calls.clear()
    monkeypatch.setenv("RERANK_API_KEY", "rotated-secret")
    ragflow_bootstrap.bootstrap(root)
    writes = [call for call in calls if call[0] == "PUT" and "/instances" in call[1]]
    assert len(writes) == 1
    assert writes[0][1] == "/providers/VLLM/instances/aitea_rerank"
    assert instances[("VLLM", "aitea_rerank")]["api_key"] == "rotated-secret"


def test_clearing_managed_rerank_disables_binding_but_retains_remote_history(registration, monkeypatch):
    root, paths, _calls, instances = registration
    ragflow_bootstrap.bootstrap(root)
    for field in ("BASE_URL", "API_KEY", "MODEL"):
        monkeypatch.setenv("RERANK_" + field, "")
    ragflow_bootstrap.bootstrap(root)
    assert dotenv_values(paths.env_file)["RAGFLOW_RERANK_MODEL"] == ""
    assert ("VLLM", "aitea_rerank") in instances


def test_incomplete_rerank_fails_before_network(registration, monkeypatch):
    root, _paths, calls, _instances = registration
    monkeypatch.setenv("RERANK_MODEL", "")
    with pytest.raises(RuntimeError, match="请同时配置"):
        ragflow_bootstrap.bootstrap(root)
    assert not calls


def test_sync_keeps_newer_administrator_edits(registration, monkeypatch):
    root, paths, _calls, _instances = registration
    original = ragflow_bootstrap.register_model

    def change_during_registration(*args, **kwargs):
        model_id = original(*args, **kwargs)
        paths.env_file.write_text("AGENT_MODEL='newer-admin-edit'\n")
        return model_id

    monkeypatch.setattr(ragflow_bootstrap, "register_model", change_during_registration)
    with pytest.raises(RuntimeError, match="同步期间实例配置已变更"):
        ragflow_bootstrap.bootstrap(root)
    assert paths.env_file.read_text() == "AGENT_MODEL='newer-admin-edit'\n"
