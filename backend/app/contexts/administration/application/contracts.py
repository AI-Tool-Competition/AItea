"""Administrator-only instance provider configuration contracts."""

from urllib.parse import urlsplit

from pydantic import Field, SecretStr, StrictBool, field_validator, model_validator

from app.shared.domain.models import StrictModel


class ProviderKeyPatch(StrictModel):
    api_key: SecretStr | None = Field(default=None, max_length=4096)
    clear_api_key: StrictBool = False

    @field_validator("api_key")
    @classmethod
    def valid_key(cls, value):
        if value is not None and any(ord(char) < 32 or ord(char) == 127 for char in value.get_secret_value()):
            raise ValueError("API Key 不能包含控制字符")
        return value

    @model_validator(mode="after")
    def valid_key_change(self):
        if self.clear_api_key and self.api_key and self.api_key.get_secret_value().strip():
            raise ValueError("不能同时替换和清除 API Key")
        return self


class ModelProviderPatch(ProviderKeyPatch):
    base_url: str | None = Field(default=None, max_length=2048)
    model: str | None = Field(default=None, max_length=200)

    @field_validator("base_url", "model")
    @classmethod
    def single_line(cls, value):
        if value is not None and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("配置不能包含控制字符")
        return value.strip() if value is not None else None

    @field_validator("base_url")
    @classmethod
    def valid_base_url(cls, value):
        if not value:
            return value
        try:
            parsed = urlsplit(value)
            port = parsed.port
            valid = (
                parsed.scheme in {"https", "http"}
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
                and not any(char.isspace() for char in value)
                and (port is None or 1 <= port <= 65535)
                and (parsed.scheme == "https" or parsed.hostname in {"localhost", "127.0.0.1", "::1"})
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Base URL 需为 HTTPS 地址，本机服务可使用 HTTP；不能包含凭据、查询参数或片段")
        return value.rstrip("/")


class ApiSettingsPatch(StrictModel):
    expected_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    agent: ModelProviderPatch | None = None
    embedding: ModelProviderPatch | None = None
    rerank: ModelProviderPatch | None = None
    mineru: ProviderKeyPatch | None = None

    @model_validator(mode="after")
    def has_changes(self):
        if not any(getattr(self, name) is not None for name in ("agent", "embedding", "rerank", "mineru")):
            raise ValueError("至少提供一项 API 配置")
        return self


class ProviderKeySettings(StrictModel):
    api_key_configured: bool


class ModelProviderSettings(ProviderKeySettings):
    base_url: str
    model: str


class ApiSettings(StrictModel):
    revision: str
    agent: ModelProviderSettings
    embedding: ModelProviderSettings
    rerank: ModelProviderSettings
    mineru: ProviderKeySettings
    agent_inherits_api_key: bool
    embedding_inherits_api_key: bool
    environment_overrides: list[str]
    restart_required: bool
