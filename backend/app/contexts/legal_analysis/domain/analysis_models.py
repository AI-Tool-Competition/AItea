"""Bounded public contracts and editable policy for the legal analysis runtime.

Program checks are descriptions, never configurable bypasses. Model-only contracts
exclude evidence verification and contain citation IDs instead of invented sources.
This module deliberately does not import app.shared.application.models (AgentConfig imports us).
"""

from typing import Annotated, Literal

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

# Full export routes contain up to 402 configured fields. Keep room for reviewed
# facts added during handling, plus one bounded extraction pass in a result.
MAX_CASE_FACTS = 600
MAX_EXTRACTED_FACTS = 60
MAX_ANALYSIS_FACTS = MAX_CASE_FACTS + MAX_EXTRACTED_FACTS


class LegalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, allow_inf_nan=False)


LegalID = Annotated[str, Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9_.:-]+$")]
Country = Annotated[str, Field(pattern=r"^[A-Z]{2}$")]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]
FactValue = Annotated[StrictStr, Field(max_length=10000)] | StrictInt | StrictFloat | None
Module = Literal["qa01", "qa02", "qa03", "qa04", "qa05", "qa06"]

LEGAL_MODULES = [
    {"id": "qa01", "title": "QA-01 问题识别与初步检索"},
    {"id": "qa02", "title": "QA-02 事实梳理与必要追问"},
    {"id": "qa03", "title": "QA-03 法域与适用规则"},
    {"id": "qa04", "title": "QA-04 候选依据与检索覆盖"},
    {"id": "qa05", "title": "QA-05 条件判断与有界复核"},
    {"id": "qa06", "title": "QA-06 引用报告与行动建议"},
]
PROGRAM_CHECKS = [
    {
        "id": "human_review",
        "title": "法律结论需人工复核",
        "description": "法律结论必须由人工复核；规则卡的 review_required 设置额外复核要求，不能关闭这一固定门禁。",
    },
    {
        "id": "scope",
        "title": "账号、业务与法域隔离",
        "description": "仅检索当前账号、业务和配置允许国家内的主资料；调试资料选择同样生效。",
    },
    {
        "id": "evidence",
        "title": "不可伪造证据",
        "description": "引用必须来自本次实际检索的不可变版本；未经审核、失效、替代或二级原件不能成为有效法律依据。",
    },
    {
        "id": "facts",
        "title": "事实与证据分离",
        "description": "模型抽取不能自行确认事实或核验材料；冲突保留来源，关键事实未核验时不能得出确定结论。",
    },
    {
        "id": "conclusions",
        "title": "结论受前提约束",
        "description": "无检索结果不是不存在义务的证明。缺少依据、条件或检索覆盖时降级结论，保留缺口与复核提示。",
    },
    {
        "id": "bounds",
        "title": "运行与重试有界",
        "description": "结构、输入、检索、追问、轮次和期限计算均受程序限额约束；错误与取消不会冒充完成。",
    },
    {
        "id": "deadlines",
        "title": "期限由程序计算",
        "description": "日期、时区、起算规则、工作日日历和依据齐备后才计算；未核验前提只产生明确的条件结果。",
    },
    {
        "id": "state",
        "title": "建议不改变业务状态",
        "description": "报告不能审批或完成任务；复用结果必须核对事实、规则和资料版本，用户编辑由历史修订保留。",
    },
]


class FactRequirement(LegalModel):
    key: LegalID
    label: str = Field(min_length=1, max_length=200)
    purpose: str = Field(default="", max_length=1000)
    decisive: bool = True


class LegalRule(LegalModel):
    id: LegalID
    title: str = Field(min_length=1, max_length=200)
    module: Module
    enabled: bool = True
    execution: Literal["model", "manual"] = "model"
    domains: list[ShortText] = Field(default_factory=list, max_length=20)
    jurisdictions: list[Country] = Field(default_factory=list, max_length=20)
    condition: str = Field(default="", max_length=2000)
    instructions: str = Field(default="", max_length=6000)
    required_facts: list[FactRequirement] = Field(default_factory=list, max_length=30)
    output_requirements: str = Field(default="", max_length=2000)
    review_required: bool = True
    source_reference: str = Field(default="", max_length=1000)


class LegalPolicy(LegalModel):
    enabled: bool = False
    jurisdictions: list[Country] = Field(default_factory=list, max_length=20)
    question_limit: int = Field(default=3, ge=1, le=5)
    max_rounds: int = Field(default=2, ge=1, le=3)
    retrieval_limit: int = Field(default=5, ge=1, le=20)
    rules: list[LegalRule] = Field(default_factory=list, max_length=60)

    @model_validator(mode="after")
    def distinct_rules(self):
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("法律规则编号不能重复")
        if len(set(self.jurisdictions)) != len(self.jurisdictions):
            raise ValueError("允许法域不能重复")
        return self


def default_legal_policy():
    instructions = [
        "区分一般信息咨询与法律判断，将混合问题拆成独立争点；领域标签与问题类型分别输出。先参考实际初步检索，保留跨争点依赖。",
        "围绕每个争点与已检索规则确定必要事实，不套用固定通用表单。区分口述、材料摘录、确认和证据核验，保留所有冲突来源。优先追问影响结论的缺失事实。",
        "分别说明监管机关与行政救济、法院管辖或仲裁地及准据法，不把目的地自动等同于准据法。核对条约适用范围与冲突法连接点；只有事实与依据支持时才选择法域。只使用允许国家，为每个争点与领域生成有界检索候选。",
        "逐候选检索并记录上位法、有效版本、定义、例外、官方解释和相应层级案例，比较来源中的事实与本案事实。区分法律原文、指导、案例和商业资料，保留候选与来源对应关系。当前仅使用已审核内部检索范围，不声称完成全网采集；部分覆盖、失败、未审核或待补资料必须说明，无结果不是不存在规则的证明。",
        "组合通用规则、领域规则和适用法域规则，分别解释规范与涵摄事实，核对成立条件、例外、抗辩、规范冲突和跨争点影响。采用支持、不支持、附条件、无法判断四类结论。主动识别缺少前提和相互矛盾的判断，必要时请求有界复核；期限只提交有依据、起算与中止延长规则明确的程序计算参数。",
        "按争点报告法律效果、可选行动及代价，优先展示已过或临近期限并保留条件日期的假设。区分法定义务、附条件事项、建议和商业选择；金额只关联经核验的事实与依据，不编造计算结果。关键主张附实际引用和定位，说明未解决的事实、检索与复核缺口。前提不足不得输出确定结论，建议不能完成业务任务。",
    ]
    return LegalPolicy(
        rules=[
            LegalRule(
                id=f"{module['id']}-default",
                title=module["title"],
                module=module["id"],
                instructions=instructions[index],
                source_reference=f"法律问题分析业务规则.docx · QA-0{index + 1}（初版转译，待法务复核）",
            )
            for index, module in enumerate(LEGAL_MODULES)
        ]
    ).model_dump(mode="json")


class FactConflict(LegalModel):
    value: FactValue = None
    source: str = Field(default="", max_length=1000)
    evidence_refs: list[LegalID] = Field(default_factory=list, max_length=30)


class LegalFact(LegalModel):
    key: LegalID
    label: str = Field(min_length=1, max_length=200)
    value: FactValue = None
    source: str = Field(default="", max_length=1000)
    confirmed: bool = False
    extraction_confidence: Literal["unknown", "low", "medium", "high"] = "unknown"
    evidence_status: Literal["unverified", "verified", "conflict"] = "unverified"
    evidence_refs: list[LegalID] = Field(default_factory=list, max_length=30)
    conflicts: list[FactConflict] = Field(default_factory=list, max_length=30)
    resolved_conflicts: list[FactConflict] = Field(default_factory=list, max_length=30)
    revision: int = Field(default=0, ge=0)
    review_note: str | None = Field(default=None, max_length=2000)


class LegalCitation(LegalModel):
    id: LegalID
    document_id: LegalID
    title: str = Field(min_length=1, max_length=1000)
    excerpt: str = Field(max_length=5000)
    locator: dict[str, JsonValue] = Field(default_factory=dict, max_length=30)
    source_url: str | None = Field(default=None, max_length=2000)
    document_version: int = Field(ge=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict, max_length=50)
    metadata_revision: int = Field(default=0, ge=0)
    country: Country | None = None
    chunk_id: str | None = Field(default=None, max_length=200)
    chunk_hash: str | None = Field(default=None, max_length=64)


class LegalCondition(LegalModel):
    label: str = Field(min_length=1, max_length=300)
    state: Literal["met", "not_met", "unknown", "not_applicable"]
    fact_keys: list[LegalID] = Field(default_factory=list, max_length=50)
    fact_sources: list[ShortText] = Field(default_factory=list, max_length=10)
    citation_ids: list[LegalID] = Field(default_factory=list, max_length=30)
    reason: str = Field(default="", max_length=2000)


class BusinessActor(LegalModel):
    id: LegalID
    name: ShortText = Field(
        description="主体短名称，如用户茶商、甲工厂、乙进口商；业务角色另列，不在名称中堆叠。"
    )
    entity_type: str = Field(default="未知", max_length=100)
    jurisdiction: Country | None = None
    roles: list[ShortText] = Field(
        default_factory=list,
        max_length=12,
        description="依据事实识别实际角色。安排生产、采购或发货不能证明亲自生产；未知生产者等角色可用待确认主体表示，不预设与用户相同或独立。",
    )
    identity_status: Literal["identified", "unknown"] = "unknown"
    sources: list[ShortText] = Field(default_factory=list, max_length=8)


class BusinessRelationship(LegalModel):
    from_actor_id: LegalID
    to_actor_id: LegalID
    relationship: ShortText
    sources: list[ShortText] = Field(default_factory=list, max_length=8)


class BusinessCredential(LegalModel):
    id: LegalID
    actor_id: LegalID
    credential_type: ShortText
    scope: str = Field(default="", max_length=1000)
    proof: str = Field(default="", max_length=1000)
    status: Literal["reported_completed", "documented", "unknown"] = "unknown"
    sources: list[ShortText] = Field(default_factory=list, max_length=8)


class BusinessContext(LegalModel):
    actors: list[BusinessActor] = Field(default_factory=list, max_length=20)
    relationships: list[BusinessRelationship] = Field(default_factory=list, max_length=40)
    credentials: list[BusinessCredential] = Field(default_factory=list, max_length=40)


class LegalObligation(LegalModel):
    id: LegalID
    matter: ShortText
    kind: Literal["statutory", "contractual", "right"] = "statutory"
    legal_actor_id: LegalID | None = Field(
        default=None, description="法律直接规定的义务承担者或权利人；委托及费用约定不改变此身份。"
    )
    performer_id: LegalID | None = Field(
        default=None, description="实际提交材料或操作系统的主体，可与法定义务人不同。"
    )
    registrant_id: LegalID | None = Field(
        default=None, description="申请、备案、许可或证书登记在其名下的主体。"
    )
    business_responsible_id: LegalID | None = Field(
        default=None, description="现有合同明确约定负责该事项或费用的主体；无约定用null。"
    )
    trigger: str = Field(min_length=1, max_length=1000)
    citation_ids: list[LegalID] = Field(default_factory=list, max_length=20)
    prerequisites: list[ShortText] = Field(default_factory=list, max_length=10)
    completion_proof: list[ShortText] = Field(default_factory=list, max_length=10)
    status: Literal["completed", "pending", "not_applicable", "unconfirmed"] = "unconfirmed"
    credential_ids: list[LegalID] = Field(default_factory=list, max_length=10)
    fact_sources: list[ShortText] = Field(default_factory=list, max_length=10)
    validity_status: Literal["reference_checked", "unconfirmed", "historical"] = "unconfirmed"
    fact_status: Literal["sourced", "unknown"] = "unknown"
    reason: str = Field(
        default="",
        max_length=2000,
        description="说明所引规则规定谁承担责任，哪项事实使该主体满足身份条件；身份未知时分别说明不同身份下的分配，不以办理或费用约定代替法定身份。",
    )


class LegalAction(LegalModel):
    id: LegalID
    title: str = Field(min_length=1, max_length=300)
    nature: Literal["mandatory", "conditional", "recommended", "commercial", "unverified"]
    details: str = Field(default="", max_length=3000)
    obligation_ids: list[LegalID] = Field(default_factory=list, max_length=20)
    responsible_actor_id: LegalID | None = Field(
        default=None,
        description="实际执行本动作的主体。向对方索取证照由索取方执行，申请进口许可由进口申请方执行；不得因关联了进口义务就把索取动作分给进口商。",
    )
    timing: str = Field(default="", max_length=300)
    prerequisites: list[ShortText] = Field(default_factory=list, max_length=10)
    completion_proof: list[ShortText] = Field(default_factory=list, max_length=10)


class DeadlineRequest(LegalModel):
    id: LegalID
    label: str = Field(min_length=1, max_length=300)
    start_fact_key: LegalID
    duration: int | None = Field(default=None, ge=1, le=3660)
    unit: Literal["days", "weeks", "months", "years"] | None = None
    timezone: str | None = Field(default=None, max_length=100)
    jurisdiction: Country | None = None
    citation_ids: list[LegalID] = Field(default_factory=list, max_length=20)
    include_start: bool | None = None
    day_type: Literal["calendar", "business"] | None = None
    roll_forward: bool | None = None
    weekend_days: list[Annotated[int, Field(ge=0, le=6)]] | None = Field(default=None, max_length=6)
    holidays: list[Annotated[str, Field(max_length=10)]] = Field(default_factory=list, max_length=1000)
    calendar_complete: bool = False
    month_end: Literal["clip", "rollover"] | None = None
    prerequisite_fact_keys: list[LegalID] = Field(default_factory=list, max_length=20)
    adjustments_resolved: bool | None = None


class DeadlineResult(LegalModel):
    id: LegalID
    label: str
    status: Literal["calculated", "conditional", "unavailable"]
    date: str | None
    timezone: str | None
    assumptions: list[str]
    missing_prerequisites: list[str]
    citation_ids: list[LegalID]
    inputs: dict[str, JsonValue]
    as_of: str | None = None
    remaining_days: int | None = None
    risk: Literal["overdue", "imminent", "upcoming", "unknown"] = "unknown"


class LegalIssue(LegalModel):
    id: LegalID
    question: str = Field(min_length=1, max_length=4000)
    kind: Literal["information", "legal"]
    domains: list[ShortText] = Field(default_factory=list, max_length=20)
    jurisdictions: list[Country] = Field(default_factory=list, max_length=22)
    fact_keys: list[LegalID] = Field(default_factory=list, max_length=100)
    depends_on: list[LegalID] = Field(default_factory=list, max_length=8)
    missing_facts: list[FactRequirement] = Field(default_factory=list, max_length=100)
    conditions: list[LegalCondition] = Field(default_factory=list, max_length=40)
    status: Literal["supported", "unsupported", "conditional", "undetermined"] = "undetermined"
    conclusion: str = Field(default="", max_length=4000)
    explanation: str = Field(default="", max_length=8000)
    rule_interpretation: str = Field(default="", max_length=3000)
    exceptions: list[ShortText] = Field(default_factory=list, max_length=10)
    obligations: list[LegalObligation] = Field(default_factory=list, max_length=20)
    citations: list[LegalCitation] = Field(default_factory=list, max_length=80)
    deadlines: list[DeadlineResult] = Field(default_factory=list, max_length=10)
    actions: list[LegalAction] = Field(default_factory=list, max_length=20)
    gaps: list[ShortText] = Field(default_factory=list, max_length=100)
    needs_review: bool = True


class LegalSection(LegalModel):
    id: LegalID
    issue_id: LegalID
    title: str = Field(min_length=1, max_length=1000)
    content: str = Field(max_length=30000)


class LegalResult(LegalModel):
    question: str = Field(max_length=20000)
    task_type: Literal["lookup", "consultation", "dispute", "contract_review", "business_plan"] = (
        "consultation"
    )
    business_context: BusinessContext = Field(default_factory=BusinessContext)
    issues: list[LegalIssue] = Field(default_factory=list, max_length=8)
    facts: list[LegalFact] = Field(default_factory=list, max_length=MAX_ANALYSIS_FACTS)
    followups: list[FactRequirement] = Field(default_factory=list, max_length=5)
    sections: list[LegalSection] = Field(default_factory=list, max_length=40)
    trace: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=6)
    gaps: list[ShortText] = Field(default_factory=list, max_length=100)
    assistant_mode: Literal["demo", "live"]
    output: str = Field(default="", max_length=240000)
    citations: list[LegalCitation] = Field(default_factory=list, max_length=160)


# Strict model outputs: these do not accept publication, verification or completion claims.
class IssueSeed(LegalModel):
    id: LegalID
    question: str = Field(min_length=1, max_length=4000)
    kind: Literal["information", "legal"]
    domains: list[ShortText] = Field(default_factory=list, max_length=20)
    fact_keys: list[LegalID] = Field(default_factory=list, max_length=100)
    depends_on: list[LegalID] = Field(default_factory=list, max_length=8)


class ClassificationOutput(LegalModel):
    issues: list[IssueSeed] = Field(min_length=1, max_length=8)


class ExtractedFact(LegalModel):
    key: LegalID
    label: str = Field(min_length=1, max_length=200)
    value: FactValue = None
    source: str = Field(min_length=1, max_length=1000)
    extraction_confidence: Literal["unknown", "low", "medium", "high"] = "unknown"
    evidence_refs: list[LegalID] = Field(default_factory=list, max_length=30)


class IssueRequirements(LegalModel):
    issue_id: LegalID
    facts: list[FactRequirement] = Field(default_factory=list, max_length=30)


class FactOutput(LegalModel):
    facts: list[ExtractedFact] = Field(default_factory=list, max_length=MAX_EXTRACTED_FACTS)
    requirements: list[IssueRequirements] = Field(default_factory=list, max_length=8)


class LegalCandidate(LegalModel):
    id: LegalID
    query: str = Field(min_length=1, max_length=2000)
    jurisdictions: list[Country] = Field(min_length=1, max_length=22)
    domain: ShortText


class IssueScope(LegalModel):
    issue_id: LegalID
    regulatory_jurisdictions: list[Country] = Field(default_factory=list, max_length=22)
    forum_jurisdictions: list[Country] = Field(default_factory=list, max_length=22)
    governing_law_jurisdictions: list[Country] = Field(default_factory=list, max_length=22)
    candidates: list[LegalCandidate] = Field(default_factory=list, max_length=4)
    gaps: list[ShortText] = Field(default_factory=list, max_length=20)


class ScopeOutput(LegalModel):
    issues: list[IssueScope] = Field(min_length=1, max_length=8)


class IssueAssessment(LegalModel):
    issue_id: LegalID
    status: Literal["supported", "unsupported", "conditional", "undetermined"]
    conclusion: str = Field(min_length=1, max_length=4000)
    explanation: str = Field(default="", max_length=8000)
    rule_interpretation: str = Field(default="", max_length=3000)
    exceptions: list[ShortText] = Field(default_factory=list, max_length=10)
    obligations: list[LegalObligation] = Field(default_factory=list, max_length=20)
    citation_ids: list[LegalID] = Field(default_factory=list, max_length=80)
    conditions: list[LegalCondition] = Field(default_factory=list, max_length=40)
    deadlines: list[DeadlineRequest] = Field(default_factory=list, max_length=10)
    actions: list[LegalAction] = Field(default_factory=list, max_length=20)
    gaps: list[ShortText] = Field(default_factory=list, max_length=50)
    recheck: bool = False


class AssessmentOutput(LegalModel):
    issues: list[IssueAssessment] = Field(min_length=1, max_length=8)
