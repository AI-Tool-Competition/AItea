"""Collection application input and output contracts."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.shared.domain.models import PublicURL, StrictModel, Tag


class CollectionPlan(StrictModel):
    collection: str = Field(default="default", min_length=1, max_length=100)
    level: Literal["primary", "secondary"] = "primary"
    seed_urls: list[PublicURL] = Field(default_factory=list, max_length=200)
    primary_document_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=20
    )
    discover_attachments: bool = True
    max_items: int = Field(default=200, ge=1, le=500)
    max_original_bytes: int = Field(default=20 * 1024 * 1024, ge=1, le=128 * 1024 * 1024)
    download_timeout_seconds: int = Field(default=90, ge=30, le=900)

    @model_validator(mode="after")
    def valid_secondary(self):
        if self.level == "secondary" and not self.primary_document_ids:
            raise ValueError("二级法规需关联至少一份一级法规")
        if self.level == "primary" and self.primary_document_ids:
            raise ValueError("一级法规不能指定引用它的一级资料")
        return self


class CollectionEntry(StrictModel):
    url: PublicURL
    identity_key: str | None = Field(default=None, min_length=1, max_length=200)


class CollectionStart(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=100)
    entries: list[CollectionEntry] | None = Field(default=None, min_length=1, max_length=200)


class ChecklistConfigure(StrictModel):
    expected_version: int = Field(ge=1)


class ChecklistDiscover(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=100)
    confirm_condition: bool = False


class ChecklistCollect(ChecklistDiscover):
    candidate_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        min_length=1, max_length=200
    )
    level: Literal["primary", "secondary"]
    primary_document_ids: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(
        default_factory=list, max_length=20
    )

    @model_validator(mode="after")
    def valid_anchors(self):
        if self.level == "secondary" and not self.primary_document_ids:
            raise ValueError("二级法规需关联至少一份本国一级法规")
        if self.level == "primary" and self.primary_document_ids:
            raise ValueError("一级法规不能指定父法规")
        return self


class CandidatePatch(StrictModel):
    expected_revision: int = Field(ge=1)
    status: Literal["pending", "excluded"]
    note: str | None = Field(default=None, max_length=1000)


class SourceInput(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000, pattern=r"^https://")
    enabled: bool = True
    interval_hours: int = Field(default=24, ge=1, le=8760)
    tags: list[Tag] = Field(default_factory=list, max_length=20)
    plan: CollectionPlan = Field(default_factory=CollectionPlan)


class SourcePatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2000, pattern=r"^https://")
    enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=8760)
    tags: list[Tag] | None = Field(default=None, max_length=20)
    plan: CollectionPlan | None = None
    expected_revision: int = Field(ge=1)
