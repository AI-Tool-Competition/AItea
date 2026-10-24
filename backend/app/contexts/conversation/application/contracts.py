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
    skills: list[Literal["guidance", "materials", "legal", "draft", "handoff"]] = Field(
        default_factory=list, max_length=5
    )
    content: str = Field(min_length=1, max_length=20000)
    client_message_id: str = Field(min_length=1, max_length=100)


class ConversationCreate(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=100)
    title: str = Field(default="", max_length=160)


class GuideRetry(StrictModel):
    client_request_id: str = Field(min_length=1, max_length=100)


class ToolCallSnapshot(StrictModel):
    """Public execution facts only; never raw credentials, prompts or model reasoning."""

    id: str = Field(min_length=1, max_length=100)
    name: Literal["search_documents", "search_legal_evidence", "check_materials", "generate_guidance"]
    label: str = Field(min_length=1, max_length=100)
    position: StrictInt = Field(ge=1, le=128)
    status: Literal["running", "retrying", "completed", "failed", "cancelled"]
    attempt: StrictInt = Field(ge=1, le=2)
    max_attempts: StrictInt = Field(ge=1, le=2)
    detail: str = Field(max_length=400)
    duration_ms: float = Field(ge=0)
    error_code: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def valid_attempt(self):
        if self.attempt > self.max_attempts:
            raise ValueError("tool attempt exceeds budget")
        return self


class MessageStreamEvent(StrictModel):
    """SSE envelope; `done` carries the same result as POST /messages."""

    event: Literal["status", "start", "delta", "step", "done", "error"]
    data: dict

    @model_validator(mode="after")
    def valid_tool_progress(self):
        if self.event == "step" and "tools" in self.data:
            tools = self.data["tools"]
            if not isinstance(tools, list) or len(tools) > 128:
                raise ValueError("invalid tool history")
            for tool in tools:
                ToolCallSnapshot.model_validate(tool)
        return self

    def encode(self):

        return f"event: {self.event}\ndata: {json.dumps(self.data, ensure_ascii=False)}\n\n"
