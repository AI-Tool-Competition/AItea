import json
from typing import Literal
from datetime import date

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


class FieldDefinition(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    type: Literal["text", "number", "textarea"]
    required: bool = True
    min: float | None = None
    placeholder: str | None = None


class TaskDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=2000)
    required: bool = True
    fields: list[FieldDefinition] = Field(max_length=30)
    require_document: bool = False
    require_confirmation: bool = True
    output_template: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def distinct_fields(self):
        if len({field.key for field in self.fields}) != len(self.fields):
            raise ValueError("字段编号不可重复")
        if not self.fields and not self.require_document and not self.require_confirmation:
            raise ValueError("任务必须至少有一项完成条件")
        return self


class Mode(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=2000)
    enabled: bool = True


class AgentStep(StrictModel):
    id: str = Field(min_length=1, max_length=64)
    type: Literal["retrieve", "check", "guide", "draft", "verify"]
    label: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    instructions: str = Field(default="", max_length=3000)


class AgentConfig(StrictModel):
    language: str = Field(min_length=1, max_length=50)
    instructions: str = Field(max_length=10000)
    retrieval_limit: int = Field(ge=1, le=20)
    steps: list[AgentStep] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def executable_steps(self):
        if len({step.id for step in self.steps}) != len(self.steps):
            raise ValueError("工作流步骤编号不可重复")
        if not any(step.enabled for step in self.steps):
            raise ValueError("工作流至少启用一个步骤")
        return self


class StageDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    actor: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=2000)
    tasks: list[TaskDefinition] = Field(min_length=1, max_length=30)
    analysis: AgentConfig | None = None


class Template(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(max_length=3000)
    origin: str = Field(min_length=1, max_length=100)
    destination: str = Field(min_length=1, max_length=100)
    is_demo: bool
    modes: list[Mode] = Field(min_length=1, max_length=20)
    stages: list[StageDefinition] = Field(min_length=1, max_length=20)
    agent: AgentConfig

    @model_validator(mode="after")
    def unique_ids(self):
        groups = [
            self.modes,
            self.stages,
            [task for stage in self.stages for task in stage.tasks],
            self.agent.steps,
        ]
        if any(len({item.id for item in group}) != len(group) for group in groups):
            raise ValueError("模式、节点、任务和步骤编号在各自范围内必须唯一")
        if not any(mode.enabled for mode in self.modes):
            raise ValueError("至少启用一种模式")
        return self


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


class TemplateDebugInput(StrictModel):
    template: Template
    input: str = Field(default="", max_length=20000)
    stage_id: str | None = Field(default=None, min_length=1, max_length=64)
    task_values: dict[str, dict[str, StrictStr | StrictInt | StrictFloat]] = Field(
        default_factory=dict, max_length=30
    )
    stop_after_step_id: str | None = Field(default=None, min_length=1, max_length=64)


class StageAnalysisInput(StrictModel):
    expected_revision: int = Field(ge=1)
    retry: bool = False


class Register(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=10, max_length=128)


class Login(StrictModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


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


class MessageInput(StrictModel):
    stage_id: str = Field(min_length=1, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    content: str = Field(min_length=1, max_length=20000)
    client_message_id: str = Field(min_length=1, max_length=100)


class MessageStreamEvent(StrictModel):
    """SSE envelope; `done` carries the same result as POST /messages."""

    event: Literal["status", "start", "delta", "done", "error"]
    data: dict

    def encode(self):
        import json

        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"


class NewArtifact(StrictModel):
    task_id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=200)


class ArtifactPatch(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=200000)
    expected_version: int = Field(ge=1)


class ArtifactConfirm(StrictModel):
    expected_version: int = Field(ge=1)


class Search(StrictModel):
    query: str = Field(min_length=1, max_length=2000)
    case_id: str | None = None
    limit: int = Field(default=5, ge=1, le=20)
    as_of: date | None = None


class SourceInput(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    url: str = Field(min_length=1, max_length=2000, pattern=r"^https://")
    enabled: bool = True
    interval_hours: int = Field(default=24, ge=1, le=8760)
    tags: list[str] = Field(default_factory=list, max_length=20)


class SourcePatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    url: str | None = Field(default=None, min_length=1, max_length=2000, pattern=r"^https://")
    enabled: bool | None = None
    interval_hours: int | None = Field(default=None, ge=1, le=8760)
    tags: list[str] | None = Field(default=None, max_length=20)
