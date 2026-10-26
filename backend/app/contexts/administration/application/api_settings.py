"""Edit the active instance file without mutating running worker environments."""

import os
from urllib.parse import urlsplit, urlunsplit

from app.configuration.environment import (
    environment_content,
    environment_lock,
    environment_revision,
    private_environment,
    startup_environment,
    write_environment,
)
from app.shared.domain.errors import fail

MODEL_PROVIDERS = ("agent", "embedding", "rerank")
API_SETTING_KEYS = tuple(
    name.upper() + "_" + field for name in MODEL_PROVIDERS for field in ("BASE_URL", "API_KEY", "MODEL")
) + ("MINERU_API_TOKEN",)
RUNTIME_BINDING_KEYS = (
    "RAGFLOW_BASE_URL",
    "RAGFLOW_API_KEY",
    "RAGFLOW_EMBEDDING_MODEL",
    "RAGFLOW_RERANK_MODEL",
)


def public_base_url(value):
    # Legacy/manual files may contain credentials in URLs. Never echo those.
    try:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
    except ValueError:
        return ""


class ApiSettingsService:
    def __init__(self, paths):
        self.paths = paths
        self.overrides = startup_environment()

    def _view(self, content):
        saved = private_environment(content)
        values = dict(os.environ) | saved | self.overrides
        providers = {
            name: {
                "base_url": public_base_url(values.get(name.upper() + "_BASE_URL", "")),
                "model": values.get(name.upper() + "_MODEL", ""),
                "api_key_configured": bool(values.get(name.upper() + "_API_KEY", "").strip()),
            }
            for name in MODEL_PROVIDERS
        }
        return {
            "revision": environment_revision(content),
            **providers,
            "mineru": {"api_key_configured": bool(values.get("MINERU_API_TOKEN", "").strip())},
            "agent_inherits_api_key": not providers["agent"]["api_key_configured"]
            and bool(values.get("LLM_API_KEY", "").strip()),
            "embedding_inherits_api_key": not providers["embedding"]["api_key_configured"]
            and bool(values.get("LLM_API_KEY", "").strip()),
            "environment_overrides": [key for key in API_SETTING_KEYS if key in self.overrides],
            "restart_required": any(
                key in saved and saved[key] != os.environ.get(key, "") and key not in self.overrides
                for key in (*API_SETTING_KEYS, *RUNTIME_BINDING_KEYS)
            ),
        }

    def read(self):
        try:
            return self._view(environment_content(self.paths.env_file))
        except (OSError, ValueError):
            fail(503, "api_settings_unavailable", "无法读取实例私有配置，请检查配置文件及权限。")

    def update(self, payload):
        changes = {}
        for name in (*MODEL_PROVIDERS, "mineru"):
            patch = getattr(payload, name)
            if patch is None:
                continue
            if name in MODEL_PROVIDERS:
                for field in ("base_url", "model"):
                    value = getattr(patch, field)
                    if value is not None:
                        changes[name.upper() + "_" + field.upper()] = value
            key = "MINERU_API_TOKEN" if name == "mineru" else name.upper() + "_API_KEY"
            secret = patch.api_key.get_secret_value().strip() if patch.api_key else ""
            if patch.clear_api_key or secret:
                changes[key] = "" if patch.clear_api_key else secret
        try:
            with environment_lock(self.paths.env_file):
                content = environment_content(self.paths.env_file)
                if environment_revision(content) != payload.expected_revision:
                    fail(
                        409,
                        "stale_api_settings",
                        "API 配置已被其他管理员修改。请读取最新版本后再保存，你的输入仍会保留。",
                    )
                saved = private_environment(content)
                changes = {key: value for key, value in changes.items() if saved.get(key) != value}
                if any(
                    key in self.overrides and self.overrides[key] != value for key, value in changes.items()
                ):
                    fail(
                        409,
                        "api_settings_environment_override",
                        "部分配置由启动环境固定，请先调整启动环境后再修改。",
                    )
                if changes:
                    content = write_environment(self.paths.env_file, content, changes)
                return self._view(content)
        except (OSError, ValueError):
            fail(
                503,
                "api_settings_unavailable",
                "无法保存实例私有配置，请检查文件权限和可用空间。你的输入仍会保留。",
            )
