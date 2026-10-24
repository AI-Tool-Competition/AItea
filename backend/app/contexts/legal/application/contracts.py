"""Legal application input and output contracts."""

from typing import Literal

from pydantic import Field, JsonValue, StrictFloat, StrictInt, StrictStr, model_validator

from app.contexts.conversation.application.contracts import MessageStreamEvent
from app.contexts.workspace.application.contracts import TemplateInput
from app.shared.domain.models import StrictModel


class LegalStreamEvent(MessageStreamEvent):
    event: Literal["status", "start", "step", "delta", "run", "done", "error"]


class LegalAnalysisInput(StrictModel):
    stage_id: str = Field(min_length=1, max_length=64)
    question: str = Field(min_length=1, max_length=10000)
    client_request_id: str = Field(min_length=1, max_length=100)


class LegalRefreshInput(StrictModel):
    expected_revision: int = Field(ge=1)
    client_request_id: str = Field(min_length=1, max_length=100)


class LegalFactPatch(StrictModel):
    expected_revision: int = Field(ge=0)
    label: str = Field(min_length=1, max_length=200)
    value: StrictStr | StrictInt | StrictFloat | None
    confirmed: bool = False
    evidence_status: Literal["unverified", "verified", "conflict"] = "unverified"
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)
    review_note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def bounded_value(self):
        if isinstance(self.value, str) and len(self.value) > 10000:
            raise ValueError("事实内容不能超过 10000 字")
        if self.value is None and self.confirmed:
            raise ValueError("未知事实不能标记为已确认")
        return self


class LegalSectionPatch(StrictModel):
    expected_revision: int = Field(ge=1)
    content: str = Field(max_length=30000)
    accept_suggestion: bool = False


class LegalPreviewInput(TemplateInput):
    stage_id: str = Field(min_length=1, max_length=64)
    input: str = Field(min_length=1, max_length=10000)
    document_ids: list[str] = Field(default_factory=list, max_length=50)
    facts: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=100)
