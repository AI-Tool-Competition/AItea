"""Shared application input and output contracts."""

from pydantic import Field

from app.shared.domain.models import StrictModel


class RevisionInput(StrictModel):
    expected_revision: int = Field(ge=1)
