"""Drafting application input and output contracts."""

from pydantic import Field

from app.shared.domain.models import StrictModel


class NewArtifact(StrictModel):
    task_id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=200)


class ArtifactPatch(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=200000)
    expected_version: int = Field(ge=1)


class ArtifactConfirm(StrictModel):
    expected_version: int = Field(ge=1)
