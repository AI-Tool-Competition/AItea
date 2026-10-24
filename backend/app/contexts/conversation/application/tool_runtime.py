"""Small, server-owned tool contracts and observable, bounded execution.

Operations close over an already authorized business scope. Model input cannot
select an account, grant permission, or dispatch arbitrary Python functions.
"""

import copy
import logging
import random
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic, sleep
from typing import Annotated
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.shared.application.streaming import StreamCancelled

logger = logging.getLogger(__name__)
_current = ContextVar("tea_tool_scope", default=None)
ShortID = Annotated[str, Field(min_length=1, max_length=100)]


class ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SearchInput(ToolInput):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(ge=1, le=20)
    country: str = Field(min_length=1, max_length=32)
    document_ids: list[ShortID] | None = Field(default=None, max_length=200)


class LegalSearchInput(ToolInput):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(ge=1, le=20)
    jurisdictions: list[Annotated[str, Field(pattern=r"^[A-Z]{2}$")]] = Field(max_length=22)
    document_ids: list[ShortID] | None = Field(default=None, max_length=200)


class CheckInput(ToolInput):
    task_ids: list[ShortID] = Field(max_length=100)


class GenerationInput(ToolInput):
    max_tokens: int = Field(ge=1, le=8000)
    stream: bool


@dataclass(frozen=True)
class ToolSpec:
    name: str
    label: str
    description: str
    input_model: type[ToolInput]
    attempts: int = 1
    timeout_seconds: float = 45


SEARCH_DOCUMENTS = ToolSpec(
    "search_documents", "检索业务资料", "在当前账号与业务范围内查找原文片段。", SearchInput, 2
)
SEARCH_LEGAL = ToolSpec(
    "search_legal_evidence", "检索法律依据", "按允许法域和所选资料检索带版本的原文。", LegalSearchInput, 2
)
CHECK_MATERIALS = ToolSpec(
    "check_materials", "检查任务材料", "按服务端规则检查字段、材料和确认状态。", CheckInput
)
GENERATE_GUIDANCE = ToolSpec(
    "generate_guidance", "生成分析与建议", "根据已提供的事实和依据生成有界回复。", GenerationInput, 2
)
TOOL_CATALOG = (SEARCH_DOCUMENTS, CHECK_MATERIALS, SEARCH_LEGAL, GENERATE_GUIDANCE)


class ToolFailure(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def failure_code(error):
    if isinstance(error, ToolFailure):
        return error.code
    if isinstance(error, (TimeoutError, httpx.TimeoutException)):
        return "timeout"
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        return (
            "rate_limited" if status == 429 else "access_denied" if status in (401, 403) else "upstream_error"
        )
    if isinstance(error, httpx.TransportError):
        return "connection_failed"
    if isinstance(error, ValidationError):
        return "invalid_input"
    if getattr(error, "status", 500) < 500:
        return "scope_or_version_conflict"
    return "execution_failed"


def retry_delay(error, attempt):
    """Retry only known transient admission/connection failures, never writes.

    Read/write timeouts are ambiguous: the provider may already have processed the
    request. Output and permission/validation failures are never automatically retried.
    """
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code not in (429, 502, 503, 504):
            return None
        header = error.response.headers.get("Retry-After")
        if header:
            try:
                # A long server delay is returned to the user instead of retrying early.
                delay = float(header)
                if not 0 <= delay <= 10:
                    return None
                return max(0.1, delay)
            except ValueError:
                return None
    elif not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
        return None
    return min(2, 0.25 * 2 ** (attempt - 1)) + random.uniform(0, 0.15)


class ToolAttempt:
    def __init__(self, run, deadline):
        self.run = run
        self.deadline = min(deadline, run.deadline)
        self.output_started = False

    def check(self):
        self.run.check_cancelled()
        if monotonic() >= self.deadline:
            raise ToolFailure("timeout", "本次调用已超过时间限制，请重试。")

    @property
    def remaining(self):
        self.check()
        return max(0.001, self.deadline - monotonic())

    def wait(self, seconds):
        until = monotonic() + seconds
        while monotonic() < until:
            self.check()
            sleep(min(0.05, max(0, until - monotonic())))
        self.check()


class ToolRun:
    def __init__(self, emit=None, check_cancelled=None, *, timeout_seconds=900, max_calls=128):
        self.emit = emit
        self.check_cancelled = check_cancelled or (lambda: None)
        self.deadline = monotonic() + timeout_seconds
        self.max_calls = max_calls
        self.run_id = uuid4().hex
        self.sequence = 0
        self.calls = 0

    @contextmanager
    def step(self, trace):
        token = _current.set((self, trace))
        try:
            yield
        finally:
            _current.reset(token)

    def publish(self, trace):
        self.sequence += 1
        trace.update(run_id=self.run_id, sequence=self.sequence)
        if self.emit:
            self.emit("step", copy.deepcopy(trace))

    def call(self, spec, inputs, operation, trace=None, *, summarize=None):
        if spec not in TOOL_CATALOG:
            raise ToolFailure("unknown_tool", "未登记的工具不能执行。")
        self.check_cancelled()
        if self.calls >= self.max_calls:
            raise ToolFailure("call_limit", "本次调用次数已达上限，请缩小问题范围。")
        self.calls += 1
        started = monotonic()
        context = ToolAttempt(self, started + spec.timeout_seconds)
        record = {
            "id": f"{self.run_id}:{self.calls}",
            "name": spec.name,
            "label": spec.label,
            "position": self.calls,
            "status": "running",
            "attempt": 1,
            "max_attempts": spec.attempts,
            "detail": spec.description,
            "duration_ms": 0,
        }
        if trace is not None:
            trace.setdefault("tools", []).append(record)

        def publish(status, detail, code=None):
            record.update(
                status=status, detail=detail[:400], duration_ms=round((monotonic() - started) * 1000, 2)
            )
            if code:
                record["error_code"] = code
            if trace is not None:
                self.publish(trace)

        try:
            publish("running", spec.description)
            args = spec.input_model.model_validate(inputs)
            for attempt in range(1, spec.attempts + 1):
                context.check()
                record["attempt"] = attempt
                if attempt > 1:
                    publish("running", f"正在重新调用，第 {attempt} 次尝试。")
                try:
                    result = operation(args, context)
                    context.check()
                    summary = summarize(result) if summarize else "已完成。"
                    publish("completed", summary)
                    return result
                except StreamCancelled:
                    raise
                except Exception as error:
                    delay = retry_delay(error, attempt)
                    if (
                        attempt >= spec.attempts
                        or context.output_started
                        or delay is None
                        or delay >= context.remaining
                    ):
                        raise
                    publish("retrying", f"服务暂时繁忙，稍后进行第 {attempt + 1} 次尝试。")
                    context.wait(delay)
        except StreamCancelled:
            record.update(
                status="cancelled",
                detail="调用已停止，未完成的结果不会作为最终答复。",
                duration_ms=round((monotonic() - started) * 1000, 2),
            )
            # A disconnected transport cannot receive a final frame; the same
            # record remains available to the existing checkpoint persistence.
            if trace is not None:
                try:
                    self.publish(trace)
                except StreamCancelled:
                    pass
            raise
        except Exception as error:
            code = failure_code(error)
            messages = {
                "timeout": "等待服务超时，请重试。",
                "invalid_input": "工具参数未通过校验，本次未执行。",
                "invalid_response": "服务返回的内容不完整或格式不正确，请重试。",
                "access_denied": "服务拒绝访问，请检查模型配置。",
                "rate_limited": "服务暂时限流，请稍后重试。",
                "scope_or_version_conflict": "资料权限或版本已变化，请刷新后重试。",
            }
            publish("failed", messages.get(code, "调用未完成，请稍后重试。"), code)
            logger.warning("agent_tool_failed", extra={"tool_name": spec.name, "error_code": code})
            raise


def call_tool(spec, inputs, operation, *, summarize=None, check_cancelled=None):
    active = _current.get()
    run, trace = active if active else (ToolRun(check_cancelled=check_cancelled), None)
    return run.call(spec, inputs, operation, trace, summarize=summarize)
