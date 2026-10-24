"""Identity application input and output contracts."""

from pydantic import Field, StrictBool, model_validator

from app.shared.domain.models import StrictModel


class Register(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=10, max_length=128)
    bootstrap_token: str | None = Field(default=None, max_length=256)


class AdminBootstrapInput(StrictModel):
    bootstrap_token: str = Field(min_length=1, max_length=256)


class UserAccessPatch(StrictModel):
    expected_revision: int = Field(ge=0)
    can_login: StrictBool | None = None
    is_admin: StrictBool | None = None

    @model_validator(mode="after")
    def valid_update(self):
        if self.can_login is None and self.is_admin is None:
            raise ValueError("至少修改一项账号权限")
        if ("can_login" in self.model_fields_set and self.can_login is None) or (
            "is_admin" in self.model_fields_set and self.is_admin is None
        ):
            raise ValueError("账号权限不能设为空")
        if self.can_login is False and self.is_admin is True:
            raise ValueError("不可登录账号不能设为管理员")
        return self


class Login(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)
