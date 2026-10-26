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


def test_fragmented_tool_arguments_and_gemini_signatures_are_preserved(monkeypatch):
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
        [delta({"content": "cut"}, "length"), "[DONE]"],
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
        [delta({"content": "closed without DONE"}, "stop")],
    ],
)
def test_incomplete_streams_never_dispatch_tools_or_appear_complete(monkeypatch, chunks):
    requests = provider(monkeypatch, "".join(frame(c) for c in chunks).encode())
    with pytest.raises(ToolFailure) as caught:
        asyncio.run(transport.agent_turn([], [], lambda _: None, lambda: None))
    assert caught.value.code == "invalid_response"
    assert len(requests) == 1


def test_reasoning_and_gateway_events_are_not_rendered_as_user_answer(monkeypatch):
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
    assert "private" not in str(result)


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
