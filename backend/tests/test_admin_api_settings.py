"""Isolated instance editing, administrator access and credential redaction."""

import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from dotenv import dotenv_values
from fastapi.testclient import TestClient

from admin_helpers import login_admin
from app.configuration import environment
from app.configuration.environment import read_environment
from app.contexts.administration.application.api_settings import ApiSettingsService
from app.contexts.administration.application.contracts import ApiSettingsPatch
from app.contexts.conversations.infrastructure.agent_transport import provider_settings
from app.interfaces.http.factory import create_app
from app.shared.domain.errors import AppError


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(environment, "_startup_environment", {})
    with TestClient(create_app(tmp_path / "instance", enable_scheduler=False)) as client:
        yield client


def put_configuration(client, content):
    path = client.app.state.api_settings.paths.env_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def settings(client):
    response = client.get("/api/tea/admin/api-settings")
    assert response.status_code == 200
    return response.json()


def save(client, payload, revision=None):
    return client.patch(
        "/api/tea/admin/api-settings",
        json={"expected_revision": revision or settings(client)["revision"], **payload},
    )


def test_api_settings_require_admin_for_reads_and_writes(client):
    for method in ("GET", "PATCH"):
        response = client.request(method, "/api/tea/admin/api-settings", json={})
        assert response.status_code == 401
    client.post("/api/tea/auth/demo", json={})
    for method in ("GET", "PATCH"):
        response = client.request(method, "/api/tea/admin/api-settings", json={})
        assert response.status_code == 403
    assert not client.app.state.api_settings.paths.env_file.exists()


def test_read_masks_secrets_and_reports_agent_inheritance(client):
    login_admin(client)
    put_configuration(client, "LLM_API_KEY='shared-secret'\nMINERU_API_TOKEN='mineru-secret'\n")
    response = settings(client)
    assert response["agent_inherits_api_key"] is True
    assert response["agent"]["api_key_configured"] is False
    assert response["mineru"]["api_key_configured"] is True
    assert "secret" not in str(response)
    assert "api_key" not in response["agent"]


def test_save_all_providers_preserves_other_values_and_uses_private_permissions(client, monkeypatch):
    login_admin(client)
    path = put_configuration(client, "# keep this comment\nUNRELATED='literal-${KEEP}'\n")
    providers = {
        name: {
            "base_url": f"https://{name}.example/v1/",
            "model": f"{name}-model",
            "api_key": f"{name}-secret",
        }
        for name in ("agent", "embedding", "rerank")
    }
    response = save(client, providers | {"mineru": {"api_key": "mineru-secret"}})
    assert response.status_code == 200
    assert response.json()["restart_required"] is True
    assert "secret" not in response.text
    assert path.stat().st_mode & 0o777 == 0o600
    assert "# keep this comment" in path.read_text()
    assert dotenv_values(path, interpolate=False)["UNRELATED"] == "literal-${KEEP}"
    assert os.environ.get("AGENT_API_KEY", "") == ""
    restarted = read_environment(environ={"TEA_ENV_FILE": str(path)})
    for key, value in restarted.items():
        monkeypatch.setenv(key, value)
    assert provider_settings() == {
        "base": "https://agent.example/v1",
        "key": "agent-secret",
        "model": "agent-model",
    }
    assert settings(client)["restart_required"] is False
    assert restarted["EMBEDDING_API_KEY"] == "embedding-secret"
    assert restarted["RERANK_API_KEY"] == "rerank-secret"
    assert restarted["MINERU_API_TOKEN"] == "mineru-secret"


def test_blank_secret_keeps_original_and_explicit_clear_only_clears_selected_key(client):
    login_admin(client)
    path = put_configuration(client, "AGENT_API_KEY='original-secret'\nMINERU_API_TOKEN='other-secret'\n")
    response = save(client, {"agent": {"api_key": "  ", "model": "new-model"}})
    assert response.status_code == 200
    assert dotenv_values(path)["AGENT_API_KEY"] == "original-secret"
    response = save(client, {"agent": {"clear_api_key": True}})
    assert response.status_code == 200
    values = dotenv_values(path)
    assert values["AGENT_API_KEY"] == ""
    assert values["MINERU_API_TOKEN"] == "other-secret"


def test_secret_roundtrips_literally_without_interpolation(client):
    login_admin(client)
    token = "new-'quoted-$token-${UNDEFINED}"
    assert save(client, {"mineru": {"api_key": token}}).status_code == 200
    path = client.app.state.api_settings.paths.env_file
    assert dotenv_values(path, interpolate=False)["MINERU_API_TOKEN"] == token
    assert token not in str(settings(client))


def test_legacy_url_credentials_are_not_echoed(client):
    login_admin(client)
    put_configuration(client, "AGENT_BASE_URL='https://name:secret@example.com/v1?api_key=secret'\n")
    current = settings(client)
    assert "secret" not in str(current)
    assert current["agent"]["base_url"] == "https://example.com/v1"


def test_rerank_model_and_endpoint_change_cache_identity_but_key_rotation_does_not(client, monkeypatch):
    service = client.app.state.service
    monkeypatch.setenv("RAGFLOW_RERANK_MODEL", "registered-id")
    monkeypatch.setenv("RERANK_BASE_URL", "https://rank.example/v1")
    monkeypatch.setenv("RERANK_MODEL", "rank-one")
    original = service.rerank_fingerprint()
    monkeypatch.setenv("RERANK_API_KEY", "rotated-secret")
    assert service.rerank_fingerprint() == original
    monkeypatch.setenv("RERANK_MODEL", "rank-two")
    assert service.rerank_fingerprint() != original
    monkeypatch.setenv("RERANK_MODEL", "rank-one")
    monkeypatch.setenv("RERANK_BASE_URL", "https://other.example/v1")
    assert service.rerank_fingerprint() != original


def test_stale_page_cannot_replace_newer_config(client):
    login_admin(client)
    revision = settings(client)["revision"]
    assert save(client, {"agent": {"model": "first"}}, revision).status_code == 200
    stale = save(client, {"agent": {"model": "second", "api_key": "never-saved-secret"}}, revision)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_api_settings"
    assert "secret" not in stale.text
    assert settings(client)["agent"]["model"] == "first"


def test_file_lock_serializes_distinct_settings_services(client):
    service = client.app.state.api_settings
    other = ApiSettingsService(service.paths)
    revision = service.read()["revision"]

    def change(pair):
        instance, model = pair
        try:
            instance.update(ApiSettingsPatch(expected_revision=revision, agent={"model": model}))
            return 200
        except AppError as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, [(service, "first"), (other, "second")]))
    assert sorted(outcomes) == [200, 409]


def test_explicit_launcher_values_are_locked_and_never_silently_overwritten(client):
    login_admin(client)
    put_configuration(client, "AGENT_MODEL='file-model'\n")
    client.app.state.api_settings.overrides = {
        "AGENT_MODEL": "launch-model",
        "AGENT_API_KEY": "launch-secret",
    }
    current = settings(client)
    assert current["agent"]["model"] == "launch-model"
    assert current["environment_overrides"] == ["AGENT_API_KEY", "AGENT_MODEL"]
    response = save(client, {"agent": {"model": "new-model"}})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "api_settings_environment_override"
    assert "launch-secret" not in str(current)


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example/v1",
        "https://user:secret@example.com/v1",
        "https://example.com?key=secret",
        "https://example.com/#secret",
        "file:///etc/passwd",
        "https://example.com:bad",
        "https://example.com\nNEW=value",
    ],
)
def test_rejects_unsafe_provider_urls_without_echoing_credentials(client, url):
    login_admin(client)
    response = save(client, {"agent": {"base_url": url}})
    assert response.status_code == 422
    assert "secret" not in response.text
    assert not client.app.state.api_settings.paths.env_file.exists()


def test_rejects_secret_injection_and_ambiguous_key_change(client):
    login_admin(client)
    for payload in ({"api_key": "secret\nNEW_KEY=value"}, {"api_key": "secret", "clear_api_key": True}):
        response = save(client, {"mineru": payload})
        assert response.status_code == 422
        assert "secret" not in response.text


def test_write_failure_keeps_previous_file_and_hides_details(client, monkeypatch):
    login_admin(client)
    path = put_configuration(client, "MINERU_API_TOKEN='original-secret'\n")

    def fail_replace(*args):
        raise OSError("sensitive path and secret")

    monkeypatch.setattr(environment.os, "replace", fail_replace)
    response = save(client, {"mineru": {"api_key": "new-secret"}})
    assert response.status_code == 503
    assert "secret" not in response.text
    assert path.read_text() == "MINERU_API_TOKEN='original-secret'\n"
    assert not list(path.parent.glob(".application-*.env"))


def test_response_schemas_do_not_contain_secret_fields(client):
    schema = client.app.openapi()
    operations = schema["paths"]["/api/tea/admin/api-settings"]
    for method in ("get", "patch"):
        assert operations[method]["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/ApiSettings"
        }
    for name in ("ModelProviderSettings", "ProviderKeySettings"):
        assert "api_key" not in schema["components"]["schemas"][name]["properties"]
