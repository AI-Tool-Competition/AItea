import json

import httpx
import pytest
from app.contexts.conversation.application import tool_runtime as runtime
from app.contexts.conversation.application.contracts import MessageStreamEvent
from app.contexts.conversation.application.tool_runtime import (
    CHECK_MATERIALS,
    GENERATE_GUIDANCE,
    SEARCH_DOCUMENTS,
    ToolRun,
    call_tool,
)
from app.shared.application.streaming import StreamCancelled
from pydantic import ValidationError


def upstream_error(status):
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    return httpx.HTTPStatusError(
        "private provider detail", request=request, response=httpx.Response(status, request=request)
    )


def scope(emit=None, cancel=None, **options):
    trace = {"id": "guide", "label": "生成建议", "status": "running", "detail": ""}
    events = []

    def send(event, data):
        # All emitted progress must conform to the actual SSE contract.
        events.append(json.loads(MessageStreamEvent(event=event, data=data).encode().split("data: ")[1]))
        if emit:
            emit(event, data)

    return ToolRun(send, cancel, **options), trace, events


def test_transient_retry_keeps_one_call_identity_and_ordered_snapshots(monkeypatch):
    monkeypatch.setattr(runtime.random, "uniform", lambda *_: 0)
    monkeypatch.setattr(runtime.ToolAttempt, "wait", lambda self, _: self.check())
    run, trace, events = scope()
    attempts = []

    def operation(args, context):
        attempts.append(args.max_tokens)
        if len(attempts) == 1:
            raise upstream_error(429)
        return "完成"

    with run.step(trace):
        assert call_tool(GENERATE_GUIDANCE, {"max_tokens": 10, "stream": True}, operation) == "完成"
    assert attempts == [10, 10]
    assert [event["tools"][0]["status"] for event in events] == [
        "running",
        "retrying",
        "running",
        "completed",
    ]
    assert len({event["tools"][0]["id"] for event in events}) == 1
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert events[-1]["tools"][0]["attempt"] == 2
    assert events[0]["tools"][0]["attempt"] == 1  # snapshots must not mutate


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_failures_are_not_retried_or_exposed(status):
    run, trace, events = scope()
    calls = []

    def operation(*_):
        calls.append(1)
        raise upstream_error(status)

    with run.step(trace), pytest.raises(httpx.HTTPStatusError):
        call_tool(GENERATE_GUIDANCE, {"max_tokens": 10, "stream": True}, operation)
    assert calls == [1]
    assert events[-1]["tools"][0]["status"] == "failed"
    assert "private provider detail" not in json.dumps(events)


def test_partial_output_is_never_retried():
    run, trace, events = scope()

    def operation(args, attempt):
        attempt.output_started = True
        raise upstream_error(503)

    with run.step(trace), pytest.raises(httpx.HTTPStatusError):
        call_tool(GENERATE_GUIDANCE, {"max_tokens": 10, "stream": True}, operation)
    assert [event["tools"][0]["status"] for event in events] == ["running", "failed"]


def test_scope_injection_and_coerced_parameters_never_reach_operation():
    run, trace, events = scope()
    for extra in ({"owner": "another-account"}, {"limit": True}):
        with run.step(trace), pytest.raises(ValidationError):
            call_tool(
                SEARCH_DOCUMENTS,
                {"query": "tea", "limit": 5, "country": "ID", **extra},
                lambda *_: pytest.fail("invalid parameters reached tool"),
            )
    assert all(tool["status"] == "failed" for tool in events[-1]["tools"])


def test_cancel_during_backoff_finishes_record_without_another_request():
    cancelled = False

    def emit(event, data):
        nonlocal cancelled
        if data["tools"][-1]["status"] == "retrying":
            cancelled = True

    def check():
        if cancelled:
            raise StreamCancelled()

    run, trace, events = scope(emit, check)
    with run.step(trace), pytest.raises(StreamCancelled):
        call_tool(
            GENERATE_GUIDANCE,
            {"max_tokens": 10, "stream": True},
            lambda *_: (_ for _ in ()).throw(upstream_error(503)),
        )
    assert trace["tools"][0]["status"] == "cancelled"
    assert trace["tools"][0]["attempt"] == 1


def test_call_budget_and_context_do_not_leak_between_runs():
    first, trace, _ = scope(max_calls=1)
    with first.step(trace):
        call_tool(CHECK_MATERIALS, {"task_ids": []}, lambda *_: [])
        with pytest.raises(runtime.ToolFailure, match="上限"):
            call_tool(CHECK_MATERIALS, {"task_ids": []}, lambda *_: pytest.fail("over budget"))
    second, other, _ = scope()
    with second.step(other):
        call_tool(CHECK_MATERIALS, {"task_ids": []}, lambda *_: [])
    assert first.run_id != second.run_id
    assert len(trace["tools"]) == len(other["tools"]) == 1


def test_retry_after_budget_and_ambiguous_timeout_are_not_repeated():
    error = upstream_error(429)
    error.response.headers["Retry-After"] = "120"
    assert runtime.retry_delay(error, 1) is None
    assert runtime.retry_delay(httpx.ReadTimeout("ambiguous"), 1) is None
    error.response.headers["Retry-After"] = "1"
    assert runtime.retry_delay(error, 1) == 1
