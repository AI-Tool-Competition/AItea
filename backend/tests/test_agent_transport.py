import asyncio
import json

import httpx
import pytest
from app.contexts.conversations.infrastructure import agent_transport as transport
from app.contexts.conversations.application.tool_runtime import ToolFailure
from app.shared.application.streaming import StreamCancelled


def frame(value):
    return "data: " + (value if isinstance(value, str) else json.dumps(value)) + "\n\n"


def delta(value, finish=None):
    return {"choices": [{"delta": value, "finish_reason": finish}]}


def provider(monkeypatch, body, status=200):
    requests = []
    original = httpx.AsyncClient

    def handler(request):
        requests.append(request)
        return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})

    monkeypatch.setattr(
        transport.httpx, "AsyncClient", lambda **kw: original(**kw, transport=httpx.MockTransport(handler))
    )
    monkeypatch.setenv("AGENT_API_KEY", "fixture-only")
    monkeypatch.setenv("AGENT_BASE_URL", "https://provider.invalid/sub2api")
    return requests


def test_followup_normalizes_empty_tool_calls_from_persisted_answers(monkeypatch):
    body = (frame(delta({"content": "第二次回答"}, "stop")) + frame("[DONE]")).encode()
    requests = provider(monkeypatch, body)
    history = [
        {"role": "user", "content": "第一问"},
        {"role": "assistant", "content": "第一答", "tool_calls": []},
        {"role": "user", "content": "第二问"},
    ]
    result = asyncio.run(transport.agent_turn(history, [], lambda _: None, lambda: None))
    assert "tool_calls" not in json.loads(requests[0].content)["messages"][1]
    assert "tool_calls" not in result
    assert history[1]["tool_calls"] == []


def test_fragmented_tool_arguments_and_gemini_signatures_are_preserved(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "gemini-3.8-flash")
    chunks = [
        delta(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "check_materials", "arguments": '{"task_'},
                        "extra_content": {"google": {"thought_signature": "opaque-test-signature"}},
                    }
                ]
            }
        ),
        delta({"tool_calls": [{"index": 0, "function": {"arguments": 'ids":[]}'}}]}),
        delta({}, "tool_calls"),
        {"choices": [], "usage": {"prompt_tokens": 10}},
        "[DONE]",
    ]
    requests = provider(monkeypatch, "".join(frame(c) for c in chunks).encode())
    visible = []
    result = asyncio.run(
        transport.agent_turn([{"role": "user", "content": "test"}], [], visible.append, lambda: None)
    )
    call = result["tool_calls"][0]
    assert call["function"]["arguments"] == '{"task_ids":[]}'
    assert call["extra_content"]["google"]["thought_signature"] == "opaque-test-signature"
    assert not visible
    assert json.loads(requests[0].content)["model"] == "gemini-3.8-flash"
    assert requests[0].url.path == "/sub2api/chat/completions"


@pytest.mark.parametrize(
    "chunks",
    [
        [delta({"content": "unfinished"}), "[DONE]"],
        [delta({"refusal": "no"}, "stop"), "[DONE]"],
        [delta({"tool_calls": [{"index": 9, "id": "x"}]}), "[DONE]"],
        [
            delta(
                {
                    "tool_calls": [
                        {"index": 0, "id": "x", "function": {"name": "check_materials", "arguments": "{}"}}
                    ]
                }
            ),
            "[DONE]",
        ],
    ],
)
def test_incomplete_streams_never_dispatch_tools_or_appear_complete(monkeypatch, chunks):
    requests = provider(monkeypatch, "".join(frame(c) for c in chunks).encode())
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert caught.value.code == "invalid_response"
    assert caught.value.response_error
    assert len(requests) == 1


@pytest.mark.parametrize("tool", [False, True])
def test_finished_stream_without_redundant_done_is_usable(monkeypatch, tool):
    value = (
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call",
                    "function": {"name": "web_search", "arguments": '{"query":"茶博会"}'},
                }
            ]
        }
        if tool
        else {"content": "完整回答"}
    )
    provider(monkeypatch, frame(delta(value, "tool_calls" if tool else "stop")).encode())
    result = asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert bool(result.get("tool_calls")) is tool
    if not tool:
        assert result["content"] == "完整回答"


def test_finished_stream_with_truncated_trailing_frame_is_not_complete(monkeypatch):
    provider(monkeypatch, (frame(delta({"content": "正文"}, "stop")) + 'data: {"usage":').encode())
    with pytest.raises(ToolFailure) as error:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert error.value.response_error == "missing_done"


@pytest.mark.parametrize("prefix", ["thinking", "answer", "tool"])
@pytest.mark.parametrize("timeout", [None, float("inf"), 2])
def test_idle_heartbeats_do_not_keep_stalled_model_alive_and_only_empty_generations_retry(
    monkeypatch, prefix, timeout
):
    import time

    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            value = (
                {"reasoning_content": "private"}
                if prefix == "thinking"
                else {"content": "已收到正文"}
                if prefix == "answer"
                else {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "pending",
                            "function": {"name": "web_search", "arguments": '{"query":'},
                        }
                    ]
                }
            )
            yield frame(delta(value)).encode()
            while True:
                await asyncio.sleep(0.01)
                yield b": heartbeat\n\n"

    original = httpx.AsyncClient
    monkeypatch.setenv("AGENT_API_KEY", "fixture-only")
    monkeypatch.setenv("AGENT_MODEL", "Nemotron-3.5")
    monkeypatch.setenv("AGENT_STREAM_IDLE_TIMEOUT", "0.06")
    monkeypatch.setattr(
        transport.httpx,
        "AsyncClient",
        lambda **kw: original(
            **kw, transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stalled()))
        ),
    )
    started = time.monotonic()
    with pytest.raises(Exception) as error:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None, timeout=timeout))
    assert time.monotonic() - started < 1
    if prefix == "thinking":
        assert isinstance(error.value, ToolFailure) and error.value.code == "model_stream_stalled"
        assert error.value.retryable is True
    else:
        assert isinstance(error.value, TimeoutError)
        assert not getattr(error.value, "retryable", False)
        assert error.value.partial_content == ("已收到正文" if prefix == "answer" else "")


@pytest.mark.parametrize("timeout", [None, float("inf"), 2])
def test_active_reasoning_can_take_longer_than_one_idle_window(monkeypatch, timeout):
    class Active(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(6):
                yield frame(delta({"reasoning_content": "检查资料"})).encode()
                await asyncio.sleep(0.02)
            yield frame(delta({"content": "已完成"}, "stop")).encode()
            yield frame("[DONE]").encode()

    original = httpx.AsyncClient
    monkeypatch.setenv("AGENT_API_KEY", "fixture-only")
    monkeypatch.setenv("AGENT_MODEL", "Nemotron-3.5")
    monkeypatch.setenv("AGENT_STREAM_IDLE_TIMEOUT", "0.06")
    monkeypatch.setattr(
        transport.httpx,
        "AsyncClient",
        lambda **kw: original(
            **kw, transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Active()))
        ),
    )
    result = asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None, timeout=timeout))
    assert result["content"] == "已完成"


@pytest.mark.parametrize("timeout", [None, float("inf")])
def test_unlimited_model_request_still_has_network_idle_and_connect_limits(monkeypatch, timeout):
    requests = provider(monkeypatch, (frame(delta({"content": "完成"}, "stop")) + frame("[DONE]")).encode())
    asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None, timeout=timeout))
    assert requests[0].extensions["timeout"] == {"connect": 10, "read": 30, "write": None, "pool": 10}


@pytest.mark.parametrize("timeout", [None, float("inf")])
def test_unlimited_model_request_can_be_cancelled_while_waiting_for_more_data(monkeypatch, timeout):
    closed = []
    checks = []

    class Waiting(httpx.AsyncByteStream):
        async def __aiter__(self):
            try:
                yield frame(delta({"reasoning_content": "检查"})).encode()
                await asyncio.sleep(30)
            finally:
                closed.append(True)

    original = httpx.AsyncClient
    monkeypatch.setenv("AGENT_API_KEY", "fixture-only")
    monkeypatch.setattr(
        transport.httpx,
        "AsyncClient",
        lambda **kw: original(
            **kw, transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Waiting()))
        ),
    )

    def check():
        checks.append(True)
        if len(checks) >= 3:
            raise StreamCancelled()

    with pytest.raises(StreamCancelled):
        asyncio.run(transport.agent_turn([], [], lambda _: None, check, timeout=timeout))
    assert closed


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "121", "bad"])
def test_invalid_idle_timeout_does_not_call_provider(monkeypatch, value):
    requests = provider(monkeypatch, b"")
    monkeypatch.setenv("AGENT_STREAM_IDLE_TIMEOUT", value)
    with pytest.raises(ToolFailure) as error:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert error.value.code == "not_configured" and not requests


def test_protocol_failure_keeps_reported_usage_without_leaking_provider_body(monkeypatch):
    body = (
        frame({"choices": [], "usage": {"total_tokens": 23}})
        + frame({"error": {"message": "private-provider-text"}})
    ).encode()
    provider(monkeypatch, body)
    usages = []
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None, on_usage=usages.append))
    assert caught.value.code == "upstream_error"
    assert caught.value.response_error == "provider_stream_error"
    assert caught.value.retryable is True
    assert "private-provider-text" not in str(caught.value)
    assert usages == [{"total_tokens": 23}]


def test_null_error_field_does_not_reject_a_valid_answer(monkeypatch):
    value = {**delta({"content": "已找到网址。"}, "stop"), "error": None}
    provider(monkeypatch, (frame(value) + frame("[DONE]")).encode())
    result = asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert result["content"] == "已找到网址。"


@pytest.mark.parametrize(
    ("prefix", "retryable"),
    [
        ({"reasoning_content": "progress fixture"}, True),
        ({"content": "尚未完成的回答"}, False),
        ({"tool_calls": [{"index": 0, "id": "pending", "function": {"name": "web_search"}}]}, False),
    ],
)
def test_explicit_stream_failure_retries_only_before_answer_or_tool_request(monkeypatch, prefix, retryable):
    body = frame(delta(prefix)) + frame({"error": {"message": "private-provider-text"}})
    provider(monkeypatch, body.encode())
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert caught.value.code == "upstream_error"
    assert caught.value.retryable is retryable
    assert "private-provider-text" not in str(caught.value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 429])
def test_permanent_or_rate_limited_stream_errors_do_not_retry_without_admission(monkeypatch, status):
    provider(monkeypatch, frame({"error": {"status": status}}).encode())
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert caught.value.retryable is False


def test_confirmed_length_stop_retains_usage_and_never_dispatches_partial_calls(monkeypatch):
    body = "".join(
        frame(c)
        for c in [
            delta(
                {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "partial",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"unfinished',
                            },
                        }
                    ]
                },
                "length",
            ),
            {"choices": [], "usage": {"completion_tokens": 6000}},
            "[DONE]",
        ]
    )
    requests = provider(monkeypatch, body.encode())
    usages = []
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None, on_usage=usages.append))
    assert caught.value.code == "generation_truncated"
    assert caught.value.partial_content == ""
    assert caught.value.generation["incomplete_tool_calls"] is True
    assert usages == [{"completion_tokens": 6000}]
    assert len(requests) == 1


@pytest.mark.parametrize("with_content", [False, True])
def test_length_stop_retains_public_text_and_diagnostics_without_thinking(monkeypatch, with_content):
    monkeypatch.setenv("AGENT_MODEL", "Nemotron-3.5")
    body = (
        frame(
            delta(
                {"reasoning_content": "private-analysis", "content": "审查意见片段" if with_content else ""},
                "length",
            )
        )
        + frame(
            {
                "choices": [],
                "usage": {
                    "completion_tokens": 12000,
                    "completion_tokens_details": {"reasoning_tokens": 11000},
                },
            }
        )
        + frame("[DONE]")
    )
    provider(monkeypatch, body.encode())
    usage = []
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(
            transport.agent_turn(
                [], [], lambda _: None, lambda: None, on_usage=usage.append, max_tokens=12000
            )
        )
    assert caught.value.partial_content == ("审查意见片段" if with_content else "")
    assert caught.value.generation["max_tokens"] == 12000
    assert caught.value.generation["finish_reason"] == "length"
    assert "private-analysis" not in str(caught.value.__dict__)
    assert usage[0]["reasoning_tokens"] == 11000


def test_provider_failure_after_public_text_preserves_it_without_retry_permission(monkeypatch):
    provider(
        monkeypatch,
        (frame(delta({"content": "已完成的一段审查意见。"})) + frame({"error": {"status": 502}})).encode(),
    )
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert caught.value.partial_content == "已完成的一段审查意见。"
    assert caught.value.retryable is False


def test_read_timeout_keeps_received_public_text_without_preserving_thinking(monkeypatch):
    class InterruptedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame(
                delta({"reasoning_content": "private-analysis", "content": "可保留的审查意见。"})
            ).encode()
            raise httpx.ReadTimeout("fixture timeout")

    original = httpx.AsyncClient
    monkeypatch.setenv("AGENT_MODEL", "Nemotron-3.5")
    monkeypatch.setenv("AGENT_API_KEY", "fixture-only")
    monkeypatch.setenv("AGENT_BASE_URL", "https://provider.invalid")
    monkeypatch.setattr(
        transport.httpx,
        "AsyncClient",
        lambda **kw: original(
            **kw,
            transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=InterruptedStream())),
        ),
    )
    with pytest.raises(httpx.ReadTimeout) as caught:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert caught.value.partial_content == "可保留的审查意见。"
    assert caught.value.generation["reasoning_characters"] > 0
    assert "private-analysis" not in str(caught.value.__dict__)


def test_reasoning_and_gateway_events_are_not_rendered_as_user_answer(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "deepseek-flash")
    body = (
        'event: aerisun.tool\ndata: {"status":"running"}\n\n'
        + frame(delta({"reasoning_content": "private", "content": "公开回复"}, "stop"))
        + frame("[DONE]")
    )
    provider(monkeypatch, body.encode())
    visible = []
    result = asyncio.run(transport.agent_turn([], [], visible.append, lambda: None))
    assert result["content"] == "公开回复"
    assert visible == ["公开回复"]
    assert result["reasoning_content"] == "private"


def test_gemini_summary_stream_is_separate_and_session_routing_is_stable(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL", "gemini-3.8-flash")
    body = "".join(
        frame(value)
        for value in [
            delta({"reasoning_content": "先核对已有资料，"}),
            delta({"reasoning_content": "再检查来源。"}),
            delta({"content": "以下是办理建议。"}, "stop"),
            "[DONE]",
        ]
    )
    requests = provider(monkeypatch, body.encode())
    visible, summaries = [], []
    result = asyncio.run(
        transport.agent_turn(
            [], [], visible.append, lambda: None, on_summary=summaries.append, cache_key="opaque-session"
        )
    )
    assert summaries == ["先核对已有资料，", "再检查来源。"]
    assert visible == ["以下是办理建议。"]
    assert result["reasoning_content"] == "先核对已有资料，再检查来源。"
    assert requests[0].headers["x-session-id"] == "opaque-session"
    assert json.loads(requests[0].content)["prompt_cache_key"] == "opaque-session"
    assert json.loads(requests[0].content)["generationConfig"]["thinkingConfig"]["includeThoughts"] is True


def test_nemotron_high_stream_and_exact_thinking_tool_history_replay(monkeypatch):
    thinking = "先核对资料，" * 1000
    reply = [
        delta({"reasoning_content": thinking}),
        # The web chat also accepts the compatible reasoning field.
        delta({"reasoning": "再给出建议。"}),
        delta({"reasoning_content": "已选定核对。", "reasoning": "不能重复拼接。"}),
        delta(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "check-1",
                        "type": "function",
                        "function": {"name": "check_materials", "arguments": "{}"},
                    }
                ]
            },
            "tool_calls",
        ),
        "[DONE]",
    ]
    requests = provider(monkeypatch, "".join(frame(value) for value in reply).encode())
    visible, summaries = [], []
    messages = [{"role": "user", "content": "核对资料"}]
    result = asyncio.run(
        transport.agent_turn(
            messages,
            [],
            visible.append,
            lambda: None,
            on_summary=summaries.append,
            cache_key="scoped-session",
            request_key="request-1-round-0",
        )
    )
    assert result["reasoning_content"] == thinking + "再给出建议。已选定核对。"
    assert "".join(summaries) == result["reasoning_content"]
    assert visible == []
    assert result["tool_calls"][0]["id"] == "check-1"
    history = [*messages, result, {"role": "tool", "tool_call_id": "check-1", "content": "缺少主体信息"}]
    asyncio.run(
        transport.agent_turn(
            history,
            [],
            visible.append,
            lambda: None,
            cache_key="scoped-session",
            request_key="request-1-round-1",
        )
    )
    assert json.loads(requests[1].content)["messages"] == history
    for request in requests:
        sent = json.loads(request.content)
        assert sent["model"] == "Nemotron-3.5"
        assert sent["reasoning_effort"] == "high"
        assert "generationConfig" not in sent
        assert request.headers["x-session-id"] == sent["prompt_cache_key"] == "scoped-session"
    assert requests[0].headers["idempotency-key"] != requests[1].headers["idempotency-key"]


@pytest.mark.parametrize("effort", ["low", "high", "max"])
def test_explicit_thinking_effort_is_used_and_part_of_provider_identity(monkeypatch, effort):
    requests = provider(monkeypatch, (frame(delta({"content": "answer"}, "stop")) + frame("[DONE]")).encode())
    monkeypatch.setenv("AGENT_REASONING_EFFORT", effort)
    asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert json.loads(requests[0].content)["reasoning_effort"] == effort
    assert transport.provider_settings()["reasoning_effort"] == effort


def test_legal_thinking_override_preserves_main_agent_effort_and_changes_identity(monkeypatch):
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "low")
    monkeypatch.setenv("LEGAL_REASONING_EFFORT", "medium")
    settings = transport.provider_settings()
    assert settings["reasoning_effort"] == "low"
    assert settings["legal_reasoning_effort"] == "medium"
    assert transport.reasoning_effort(settings["model"], "medium") == "medium"


@pytest.mark.parametrize("with_tools", [True, False])
def test_deepseek_dispatch_disables_thinking_but_composition_keeps_effort(monkeypatch, with_tools):
    requests = provider(monkeypatch, (frame(delta({"content": "answer"}, "stop")) + frame("[DONE]")).encode())
    monkeypatch.setenv("AGENT_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AGENT_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "low")
    definitions = (
        [{"type": "function", "function": {"name": "update_task_brief", "parameters": {"type": "object"}}}]
        if with_tools
        else []
    )
    asyncio.run(
        transport.agent_turn(
            [],
            definitions,
            lambda _: None,
            lambda: None,
            tool_choice={"type": "function", "function": {"name": "update_task_brief"}},
        )
    )
    body = json.loads(requests[0].content)
    assert body["reasoning_effort"] == ("none" if with_tools else "low")
    if with_tools:
        assert body["thinking"] == {"type": "disabled"}
    else:
        assert "tools" not in body


@pytest.mark.parametrize("thinking", [60001 * "x", {"invalid": "not text"}])
def test_invalid_thinking_is_rejected_before_a_tool_can_run(monkeypatch, thinking):
    provider(
        monkeypatch,
        (
            frame(delta({"reasoning_content": thinking, "content": "answer"}, "stop")) + frame("[DONE]")
        ).encode(),
    )
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert caught.value.code == "invalid_response"


def test_invalid_reasoning_configuration_does_not_call_provider(monkeypatch):
    requests = provider(monkeypatch, b"")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "unsupported")
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert caught.value.code == "not_configured"
    assert not requests


def test_fine_grained_thinking_stream_allows_envelope_overhead(monkeypatch):
    chunk = delta({"reasoning_content": "核对"}) | {"id": "completion_" + "x" * 200, "model": "Nemotron-3.5"}
    body = frame(chunk) * 4000 + frame(delta({"content": "结果"}, "stop")) + frame("[DONE]")
    assert len(body.encode()) > 1024 * 1024
    provider(monkeypatch, body.encode())
    summaries = []
    result = asyncio.run(
        transport.agent_turn([], [], lambda _: None, lambda: None, on_summary=summaries.append)
    )
    assert result["reasoning_content"] == "".join(summaries) == "核对" * 4000


def test_unknown_provider_reasoning_is_not_treated_as_gemini_summary(monkeypatch):
    provider(
        monkeypatch,
        (
            frame(delta({"reasoning_content": "private", "content": "answer"}, "stop")) + frame("[DONE]")
        ).encode(),
    )
    monkeypatch.setenv("AGENT_MODEL", "other-model")
    summaries = []
    asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None, on_summary=summaries.append))
    assert summaries == []


def test_cancel_closes_provider_task_promptly():
    closed = []

    async def operation():
        try:
            await asyncio.sleep(30)
        finally:
            closed.append(True)

    def cancel():
        raise StreamCancelled()

    with pytest.raises(StreamCancelled):
        asyncio.run(transport.cancellable(operation(), cancel))
    assert closed


@pytest.mark.parametrize(
    "body",
    [
        {"status": "failed", "output": {}},
        {"status": "running", "output": {}},
        {"status": "completed", "output": {}, "error": "private"},
    ],
)
def test_gateway_failure_is_not_a_successful_tool_result(monkeypatch, body):
    requests = provider(monkeypatch, json.dumps(body).encode())
    with pytest.raises(ToolFailure):
        asyncio.run(transport.gateway_tool("web_search", {"query": "test"}, lambda: None, timeout=1))
    assert len(requests) == 1


def test_gateway_endpoint_and_auth_contract(monkeypatch):
    requests = provider(monkeypatch, json.dumps({"status": "completed", "output": {"sources": []}}).encode())
    result = asyncio.run(transport.gateway_tool("web_search", {"query": "test"}, lambda: None, timeout=1))
    assert result["status"] == "completed"
    assert requests[0].url.path == "/sub2api/v1/tools/web_search"
    assert requests[0].headers["authorization"] == "Bearer fixture-only"


@pytest.mark.parametrize("name", ["web_search", "image_recognition"])
def test_completed_gateway_envelope_requires_the_actual_tool_output(monkeypatch, name):
    provider(monkeypatch, json.dumps({"status": "completed", "output": {}}).encode())
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(transport.gateway_tool(name, {}, lambda: None, timeout=1))
    assert caught.value.code == "invalid_response"


def test_rate_limit_and_unknown_read_timeout_do_not_retry_paid_requests(monkeypatch):
    requests = provider(monkeypatch, b"private provider detail", status=429)
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert len(requests) == 1


@pytest.mark.parametrize("cached", [None, 0, 23])
def test_usage_uses_reported_cache_tokens_and_does_not_invent_a_hit(monkeypatch, cached):
    usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    provider(
        monkeypatch,
        (
            frame(delta({"content": "回复"}, "stop"))
            + frame({"choices": [], "usage": usage})
            + frame("[DONE]")
        ).encode(),
    )
    observed = []
    asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None, on_usage=observed.append))
    assert len(observed) == 1
    assert observed[0]["prompt_tokens"] == 100
    if cached is None:
        assert "cached_tokens" not in observed[0]
    else:
        assert observed[0]["cached_tokens"] == cached


def test_native_non_thinking_composition_explicitly_disables_provider_thinking(monkeypatch):
    requests = provider(monkeypatch, (frame(delta({"content": "answer"}, "stop")) + frame("[DONE]")).encode())
    monkeypatch.setenv("AGENT_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AGENT_MODEL", "deepseek-flash")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "none")
    asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    body = json.loads(requests[0].content)
    assert body["thinking"] == {"type": "disabled"}
    assert "tools" not in body


def test_native_composition_enables_requested_reasoning(monkeypatch):
    requests = provider(monkeypatch, (frame(delta({"content": "answer"}, "stop")) + frame("[DONE]")).encode())
    monkeypatch.setenv("AGENT_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AGENT_MODEL", "deepseek-v4-pro")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "low")
    asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    body = json.loads(requests[0].content)
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "low"


def test_http_client_enforces_wall_clock_timeout_during_silent_stream(monkeypatch):
    import time

    class Silent(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(10)
            yield b""

    original = httpx.AsyncClient
    monkeypatch.setattr(
        transport.httpx,
        "AsyncClient",
        lambda **kw: original(
            **kw, transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Silent()))
        ),
    )
    monkeypatch.setenv("AGENT_API_KEY", "fixture-only")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None, timeout=0.08))
    assert time.monotonic() - started < 0.8


def test_no_more_tools_sends_explicit_none_even_without_definitions(monkeypatch):
    requests = provider(monkeypatch, (frame(delta({"content": "答复"}, "stop")) + frame("[DONE]")).encode())
    asyncio.run(
        transport.agent_turn(
            [{"role": "user", "content": "完成答复"}], [], lambda _: None, lambda: None, tool_choice="none"
        )
    )
    assert json.loads(requests[0].content)["tool_choice"] == "none"
