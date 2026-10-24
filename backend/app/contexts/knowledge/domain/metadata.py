from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["law", "guidance", "case", "business", "demo"] = "business"
    jurisdiction: str = Field(default="", max_length=100)
    language: str = Field(default="", max_length=50)
    published_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    review_status: Literal["draft", "published"] = "published"

    @model_validator(mode="after")
    def valid_interval(self):
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("生效起始日期不能晚于结束日期")
        return self


class MetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=0)
    kind: Literal["law", "guidance", "case", "business", "demo"] | None = None
    jurisdiction: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, max_length=50)
    published_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    review_status: Literal["draft", "published"] | None = None


def eligible(metadata: dict, as_of: date) -> bool:
    """An approval is necessary, and known effective dates bound applicability."""
    if metadata.get("review_status") != "published":
        return False
    start, end = metadata.get("effective_from"), metadata.get("effective_to")
    return (not start or date.fromisoformat(start) <= as_of) and (not end or as_of <= date.fromisoformat(end))
