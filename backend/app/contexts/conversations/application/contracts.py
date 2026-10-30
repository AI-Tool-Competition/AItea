"""Conversation application input and output contracts."""

import json
from typing import Literal

from pydantic import Field, StrictInt, model_validator

from app.shared.domain.models import StrictModel


class MessageInput(StrictModel):
    stage_id: str = Field(min_length=1, max_length=64)
    task_id: str | None = Field(default=None, max_length=64)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    mode: Literal["auto", "legal"] = "auto"
    skills: list[
        Literal[
            "guidance",
            "materials",
            "legal",
            "draft",
            "handoff",
            "research",
            "vision",
            "business",
            "export_planning",
            "legal_lookup",
            "contract_review",
            "litigation_analysis",
            "long_image_to_pdf",
            "document_operations",
            "china-lawyer-analyst",
            "chinese-law-verifier",
            "contract-review",
            "legal-writing",
            "litigation-analysis",
            "long-image-to-pdf",
            "document-operations",
            "legal-research",
            "foreign-law-research",
            "proposition-checking",
            "adversarial-qc",
            "sgcite",
            "bart-statutory-reference-checker",
            "document-fill",
            "coquill",
            "legal-translation",
            "contract-qa",
            "redlines",
            "comms-improver",
        ]
    ] = Field(default_factory=list, max_length=21)
    content: str = Field(min_length=1, max_length=20000)
    client_message_id: str = Field(min_length=1, max_length=100)


class ConversationCreate(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=160)


class GuideRetry(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=100)


class ToolOutputItem(StrictModel):
    label: str = Field(default="", max_length=200)
    text: str = Field(default="", max_length=1200)
    status: str | None = Field(default=None, max_length=64)
    url: str | None = Field(default=None, max_length=2000)


class ToolOutputPreview(StrictModel):
    """Bounded excerpts of actual tool returns, never another generated summary."""

    text: str = Field(default="", max_length=60000)
    items: list[ToolOutputItem] = Field(default_factory=list, max_length=8)
    notice: str = Field(default="", max_length=400)
    truncated: bool = False


class ToolCallSnapshot(StrictModel):
    """Public output and provider summaries; never credentials, prompts or private reasoning."""

    id: str = Field(min_length=1, max_length=100)
    name: Literal[
        "update_business_card",
        "search_documents",
        "search_legal_evidence",
        "lookup_chinese_law",
        "read_law",
        "get_law_versions",
        "check_materials",
        "generate_guidance",
        "guide_business",
        "analyze_legal",
        "draft_document",
        "get_handoff",
        "discover_tools",
        "tool_search",
        "load_skill",
        "list_documents",
        "read_document",
        "inspect_image",
        "convert_long_image_to_pdf",
        "inspect_document_file",
        "read_contract_template",
        "create_document_file",
        "edit_document_file",
        "web_search",
        "recall_memory",
        "remember_memory",
        "update_plan",
        "update_task_brief",
        "agent_turn",
        "compact_context",
        "read_tool_result",
    ]
    label: str = Field(min_length=1, max_length=100)
    position: StrictInt = Field(ge=1, le=128)
    status: Literal["running", "retrying", "completed", "failed", "cancelled"]
    attempt: StrictInt = Field(ge=1, le=2)
    max_attempts: StrictInt = Field(ge=1, le=2)
    detail: str = Field(max_length=400)
    duration_ms: float = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=64)
    output_preview: ToolOutputPreview | None = None

    @model_validator(mode="after")
    def valid_attempt(self):
        if self.attempt > self.max_attempts:
            raise ValueError("tool attempt exceeds budget")
        if self.name != "agent_turn" and self.output_preview and len(self.output_preview.text) > 4000:
            raise ValueError("tool output exceeds excerpt budget")
        return self


class ToolOutputDelta(StrictModel):
    step_id: str = Field(min_length=1, max_length=100)
    run_id: str = Field(min_length=1, max_length=100)
    tool_id: str = Field(min_length=1, max_length=100)
    sequence: StrictInt = Field(ge=1)
    text: str = Field(min_length=1, max_length=3500)


class MessageStreamEvent(StrictModel):
    """SSE envelope; `done` carries the same result as POST /messages."""

    event: Literal["status", "start", "delta", "step", "tool_delta", "done", "error"]
    data: dict

    @model_validator(mode="after")
    def valid_tool_progress(self):
        if self.event == "start" and "append" in self.data and type(self.data["append"]) is not bool:
            raise ValueError("invalid continuation flag")
        if self.event == "tool_delta":
            ToolOutputDelta.model_validate(self.data)
        if self.event == "step" and "tools" in self.data:
            tools = self.data["tools"]
            if not isinstance(tools, list) or len(tools) > 128:
                raise ValueError("invalid tool history")
            for tool in tools:
                ToolCallSnapshot.model_validate(tool)
        return self

    def encode(self):

        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"


class MemoryInput(StrictModel):
    content: str = Field(min_length=1, max_length=2000)
    kind: Literal["preference", "note"] = "note"
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)
    expected_revision: StrictInt | None = Field(default=None, ge=1)


class MemoryEntry(StrictModel):
    id: str
    owner_id: str
    case_id: str
    stage_id: str
    conversation_id: str | None
    kind: Literal["preference", "note"]
    content: str
    revision: int
    case_revision: int
    source_message_id: str | None
    deleted: int
    stale: bool
    created_at: str
    updated_at: str
    context_hash: str


class MemoryList(StrictModel):
    items: list[MemoryEntry]
    total: int


class MemoryDeleted(StrictModel):
    deleted: bool


class AgentToolInfo(StrictModel):
    name: str
    label: str
    description: str
    parallel: bool
    capability: str = "operations"
    entry_condition: str = ""
    result_contract: str = ""
    cost_class: str = "local"
    timeout_seconds: float = 45


class AgentSkillInfo(StrictModel):
    id: str
    label: str
    description: str
    tools: list[str]
    aliases: list[str] = Field(default_factory=list)


class AgentCombination(StrictModel):
    label: str
    skills: list[str]
    description: str


class AgentCatalog(StrictModel):
    tools: list[AgentToolInfo]
    skills: list[AgentSkillInfo]
    combinations: list[AgentCombination]
