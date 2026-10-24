from typing import Annotated, Literal

from pydantic import Field, model_validator

from app.contexts.legal.domain.legal_models import LegalPolicy
from app.shared.domain.models import StrictModel


class FieldDefinition(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    label: str = Field(min_length=1, max_length=100)
    type: Literal["text", "number", "textarea", "select"]
    required: bool = True
    min: float | None = None
    placeholder: str | None = None
    options: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None, min_length=1, max_length=50
    )
    accepted_values: list[Annotated[str, Field(min_length=1, max_length=100)]] | None = Field(
        default=None, min_length=1, max_length=50
    )

    @model_validator(mode="after")
    def valid_options(self):
        if self.type != "select":
            if self.options is not None or self.accepted_values is not None:
                raise ValueError("仅单选字段支持选项及核验通过值")
        elif not self.options or len(set(self.options)) != len(self.options):
            raise ValueError("单选字段需要非空且不重复的选项")
        if self.accepted_values is not None and (
            not self.required
            or len(set(self.accepted_values)) != len(self.accepted_values)
            or not set(self.accepted_values).issubset(self.options or [])
        ):
            raise ValueError("核验通过值必须是必填单选字段的不重复选项子集")
        return self


class TaskDefinition(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=2000)
    required: bool = True
    blocks_next_stage: bool = True
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
    legal: LegalPolicy | None = None

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
