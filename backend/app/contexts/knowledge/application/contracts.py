"""Knowledge application input and output contracts."""

from datetime import date
from typing import Literal

from pydantic import Field, JsonValue

from app.shared.domain.models import StrictModel, Tag


class PreviewCell(StrictModel):
    row: int
    text: str
    col_span: int = 1
    row_span: int = 1


class DocumentPreviewBlock(StrictModel):
    kind: str
    text: str = ""
    image_id: str | None = None
    caption: str = ""
    summary: str = ""
    width: int | None = None
    height: int | None = None
    cells: list[PreviewCell] | None = None


class DocumentPreview(StrictModel):
    summary: str
    blocks: list[DocumentPreviewBlock]
    total: int
    offset: int


class LibraryCountry(StrictModel):
    country: str = Field(pattern=r"^[A-Z]{2}$")


class LibraryCountryList(StrictModel):
    items: list[LibraryCountry] = Field(max_length=676)
    total: int = Field(ge=0, le=676)


class Search(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    case_id: str | None = None
    limit: int = Field(default=5, ge=1, le=20)
    as_of: date | None = None
    collection: str | None = Field(default=None, min_length=1, max_length=100)
    country: str | None = Field(default=None, pattern=r"^(?:[A-Z]{2}|unassigned)$")
    source_scope: Literal["personal", "system"] | None = None


class SearchResult(StrictModel):
    items: list[dict[str, JsonValue]]
    mode: Literal["lexical", "hybrid", "ragflow", "unavailable"]
    reranked: bool = False
    warning: str | None = None


class ClassificationPatch(StrictModel):
    collection: str = Field(min_length=1, max_length=100)
    level: Literal["primary", "secondary"]
    tags: list[Tag] = Field(default_factory=list, max_length=20)
    expected_revision: int = Field(ge=0)


class LegalRelationInput(StrictModel):
    collection: str = Field(min_length=1, max_length=100)
    target_document_id: str = Field(min_length=1, max_length=64)
    relation: Literal["cites", "related"] = "related"
    locator: str = Field(default="", max_length=1000)
