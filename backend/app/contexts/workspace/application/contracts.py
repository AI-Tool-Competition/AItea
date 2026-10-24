"""Workspace application input and output contracts."""

import json
from typing import Annotated, Literal

from pydantic import Field, JsonValue, StrictFloat, StrictInt, StrictStr, model_validator

from app.contexts.conversation.application.contracts import MessageStreamEvent
from app.contexts.workspace.domain.models import Template
from app.shared.domain.models import StrictModel

DebugID = Annotated[str, Field(min_length=1, max_length=100)]


class TemplateInput(StrictModel):
    # Kept as JSON so incomplete editor forms can receive path-specific validation.
    template: dict[str, JsonValue]

    @model_validator(mode="after")
    def bounded_template(self):
        if len(json.dumps(self.template, ensure_ascii=False, allow_nan=False).encode()) > 512 * 1024:
            raise ValueError("配置内容不能超过 512 KB")
        return self


class TemplateDraftInput(TemplateInput):
    expected_revision: int = Field(ge=0)


class TemplatePublishInput(TemplateInput):
    expected_draft_revision: int | None = Field(default=None, ge=0)


class DebugExpectation(StrictModel):
    id: DebugID
    kind: Literal["contains", "not_contains", "check", "citation", "manual"]
    text: str | None = Field(default=None, min_length=1, max_length=2000)
    task_id: DebugID | None = None
    check_id: DebugID | None = None
    status: Literal["passed", "missing", "conflict"] | None = None
    document_id: DebugID | None = None

    @model_validator(mode="after")
    def required_target(self):
        if self.kind in ("contains", "not_contains", "manual") and not self.text:
            raise ValueError("请填写预期内容")
        if self.kind == "check" and not (self.check_id and self.status):
            raise ValueError("请选择检查项及预期状态")
        if self.kind == "citation" and not self.document_id:
            raise ValueError("请选择预期引用的资料")
        return self


class DebugSampleValues(StrictModel):
    template: Template
    input: str = Field(default="", max_length=20000)
    stage_id: str | None = Field(default=None, min_length=1, max_length=64)
    task_values: dict[str, dict[str, StrictStr | StrictInt | StrictFloat]] = Field(
        default_factory=dict, max_length=30
    )
    document_ids: list[DebugID] | None = Field(default=None, max_length=200)
    expectations: list[DebugExpectation] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def bounded_snapshot(self):
        if len(json.dumps(self.model_dump(), ensure_ascii=False).encode()) > 512 * 1024:
            raise ValueError("样例及配置合计不能超过 512 KB")
        if len({item.id for item in self.expectations}) != len(self.expectations):
            raise ValueError("预期结果编号不可重复")
        if any(len(values) > 30 for values in self.task_values.values()):
            raise ValueError("每项任务最多填写 30 个字段")
        return self


class DebugSampleInput(DebugSampleValues):
    name: str = Field(min_length=1, max_length=100)
    expected_revision: int = Field(ge=0)


class TemplateDebugInput(DebugSampleValues):
    stop_after_step_id: str | None = Field(default=None, min_length=1, max_length=64)
    start_step_id: str | None = Field(default=None, min_length=1, max_length=64)
    source_run_id: DebugID | None = None
    client_id: DebugID | None = None
    sample_id: DebugID | None = None
    sample_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def references(self):
        if bool(self.start_step_id) != bool(self.source_run_id):
            raise ValueError("局部运行需要同时指定历史记录和起始步骤")
        if bool(self.sample_id) != bool(self.sample_revision):
            raise ValueError("样例编号与修订号需要同时指定")
        return self


class StageAnalysisInput(StrictModel):
    expected_revision: int = Field(ge=1)
    retry: bool = False


class NewCase(StrictModel):
    title: str = Field(min_length=1, max_length=150)
    company: str = Field(default="", max_length=200)
    product: str = Field(default="", max_length=200)
    mode_id: str = Field(min_length=1, max_length=64)
    template_id: str = Field(min_length=1, max_length=64)


class TaskPatch(StrictModel):
    values: dict[str, str | int | float] = Field(max_length=30)
    evidence_ids: list[str] | None = Field(default=None, max_length=30)
    expected_revision: int = Field(ge=1)


class Verify(StrictModel):
    expected_revision: int = Field(ge=1)
    confirm: bool


class DebugStreamEvent(MessageStreamEvent):
    event: Literal["status", "step", "delta", "run", "done", "error"]
