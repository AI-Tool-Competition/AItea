import json
from typing import Annotated, Literal
from datetime import date

from .legal_models import LegalPolicy

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


DebugID = Annotated[str, Field(min_length=1, max_length=100)]


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
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    mode: Literal["auto", "legal"] = "auto"
    skills: list[Literal["guidance", "materials", "legal", "draft", "handoff"]] = Field(default_factory=list, max_length=5)
    content: str = Field(min_length=1, max_length=20000)
    client_message_id: str = Field(min_length=1, max_length=100)


class ConversationCreate(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=160)


class GuideRetry(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=100)


class MessageStreamEvent(StrictModel):
    """SSE envelope; `done` carries the same result as POST /messages."""

    event: Literal["status", "start", "delta", "step", "done", "error"]
    data: dict

    def encode(self):
        import json

        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"


class DebugStreamEvent(MessageStreamEvent):
    event: Literal["status", "step", "delta", "run", "done", "error"]


class LegalStreamEvent(MessageStreamEvent):
    event: Literal["status", "start", "step", "delta", "run", "done", "error"]


class NewArtifact(StrictModel):
    task_id: str = Field(min_length=1, max_length=64)
    title: str | None = Field(default=None, max_length=200)


class ArtifactPatch(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=200000)
    expected_version: int = Field(ge=1)


class ArtifactConfirm(StrictModel):
    expected_version: int = Field(ge=1)


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
    mode: Literal["lexical", "hybrid"]
    reranked: bool = False
    warning: str | None = None


Tag = Annotated[str, Field(min_length=1, max_length=80)]
PublicURL = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"^https://")]


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
    # Stable official identifier when new revisions move to a different URL.


class CollectionStart(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=100)
    entries: list[CollectionEntry] | None = Field(default=None, min_length=1, max_length=200)


class RevisionInput(StrictModel):
    expected_revision: int = Field(ge=1)


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
