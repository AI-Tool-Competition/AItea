import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import assistant
from app.main import create_app
from app.service import AppError


@pytest.fixture
def chat(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        client.post("/api/tea/auth/demo", json={})
        bootstrap = client.get("/api/tea/bootstrap").json()
        case = client.get("/api/tea/cases/" + bootstrap["cases"][0]["id"]).json()
        payload = {
            "stage_id": case["stages"][0]["id"],
            "content": "先准备什么？",
            "client_message_id": "stream-1",
        }
        yield client, case, payload, bootstrap["user"]["id"]


def frames(response):
    return [
        (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
        for frame in response.text.split("\n\n")
        if (lines := frame.splitlines()) and lines[0].startswith("event:")
    ]


def test_stream_demo_persists_once_and_replays(chat):
    client, case, payload, _ = chat
    url = f"/api/tea/cases/{case['id']}/messages/stream"
    response = client.post(url, json=payload)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert "no-transform" in response.headers["cache-control"]
    events = frames(response)
    assert events[0][0] == "status"
    assert any(event == "delta" and "演示" in data["text"] for event, data in events)
    assert events[-1][0] == "done"
    done = events[-1][1]
    assert done["messages"][0]["content"] == payload["content"]
    assert done["case"]["completed_tasks"] == case["completed_tasks"]
    assert frames(client.post(url, json=payload))[-1][1]["messages"] == done["messages"]
    assert client.post(url, json=payload | {"content": "另一条"}).status_code == 409


def test_stream_validates_ownership_and_stage_before_headers(chat):
    client, case, payload, _ = chat
    url = f"/api/tea/cases/{case['id']}/messages/stream"
    assert client.post(url, json=payload | {"stage_id": "missing"}).status_code == 422
    assert client.post(url, json=payload | {"task_id": "missing"}).status_code == 422
    client.post("/api/tea/auth/logout")
    assert client.post(url, json=payload).status_code == 401
    client.post("/api/tea/auth/demo", json={})
    assert client.post(url, json=payload).status_code == 404


def provider(monkeypatch, chunks):
    import httpx

    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    sent = []

    class ProviderResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def raise_for_status(self):
            pass

        async def aiter_bytes(self):
            for chunk in chunks():
                yield chunk

    class ProviderClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, *args, **kwargs):
            assert kwargs["json"]["stream"] is True
            sent.append(kwargs["json"])
            return ProviderResponse()

    monkeypatch.setattr(httpx, "AsyncClient", ProviderClient)
    return sent


def delta(text):
    return (
        "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False) + "\r\n\r\n"
    ).encode()


def test_provider_forwards_utf8_deltas_before_reading_next_chunk(monkeypatch):
    seen = []

    def chunks():
        first = delta("准备茶叶")
        for byte in first:
            yield bytes([byte])
        assert seen == ["准备茶叶"]  # callback ran before the provider finished
        yield delta("资料。")
        yield b"data: [DONE]\n\n"

    provider(monkeypatch, chunks)
    output = assistant.generate_guidance(
        {"language": "zh-CN", "instructions": ""}, {}, "", {}, [], [], "", on_delta=seen.append
    )
    assert output == "准备茶叶资料。"
    assert seen == ["准备茶叶", "资料。"]


def test_chat_stream_reuses_opaque_openai_cache_key_and_logs_usage(chat, monkeypatch, caplog):
    import logging

    client, case, payload, owner = chat
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com/v1")
    caplog.set_level(logging.INFO, logger="app.assistant")

    usage = {
        "choices": [],
        "usage": {
            "prompt_tokens": 1500,
            "prompt_tokens_details": {"cached_tokens": 1024, "cache_write_tokens": 0},
        },
    }

    def chunks():
        yield delta("请先整理产品信息。")
        yield ("data: " + json.dumps(usage) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    sent = provider(monkeypatch, chunks)
    url = f"/api/tea/cases/{case['id']}/messages/stream"
    first = client.post(url, json=payload)
    second = client.post(
        url,
        json=payload | {"content": "还需要哪些材料？", "client_message_id": "stream-2"},
    )

    expected = assistant.conversation_prompt_cache_key(owner, case["id"], payload["stage_id"])
    assert first.status_code == second.status_code == 200
    assert sent[0]["prompt_cache_key"] == sent[1]["prompt_cache_key"] == expected
    assert all(value not in expected for value in (owner, case["id"], payload["stage_id"]))
    assert expected != assistant.conversation_prompt_cache_key(
        "another-user", case["id"], payload["stage_id"]
    )
    assert expected != assistant.conversation_prompt_cache_key(owner, case["id"], "another-stage")
    assert sent[0]["stream_options"] == {"include_usage": True}
    usage_records = [record for record in caplog.records if record.getMessage() == "provider_prompt_usage"]
    assert len(usage_records) == 2
    assert usage_records[-1].cached_tokens == 1024
    assert usage_records[-1].prompt_tokens == 1500


def test_openai_specific_cache_fields_are_omitted_for_custom_provider(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    sent = provider(monkeypatch, lambda: iter([delta("已收到。"), b"data: [DONE]\n\n"]))
    output = assistant.generate_guidance(
        {"language": "zh-CN", "instructions": ""},
        {},
        "",
        {},
        [],
        [],
        "",
        on_delta=lambda _: None,
        prompt_cache_key="aitea-chat-v1-test",
    )
    assert output == "已收到。"
    assert "prompt_cache_key" not in sent[0]
    assert "stream_options" not in sent[0]


def test_aerisun_deepseek_requests_usage_and_keeps_case_context_in_cache_prefix(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("LLM_BASE_URL", "https://aerisun.top/sub2api")
    caplog.set_level(logging.INFO, logger="app.assistant")
    usage = {
        "choices": [],
        "usage": {
            "prompt_tokens": 1800,
            "prompt_tokens_details": {"cached_tokens": 1280},
            "prompt_cache_miss_tokens": 520,
        },
    }

    def chunks():
        yield delta("先整理材料。")
        yield ("data: " + json.dumps(usage) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    sent = provider(monkeypatch, chunks)
    monkeypatch.setenv("LLM_MODEL", "deepseek-v4.1-flash")
    agent = {"language": "zh-CN", "instructions": ""}
    step = {"instructions": "列出下一步"}
    common = {"business_name": "六堡茶出口", "recent_messages": []}
    assistant.generate_guidance(
        agent,
        step,
        "先准备什么？",
        common | {"recent_messages": [{"role": "user", "content": "你好"}]},
        [],
        [],
        "",
        on_delta=lambda _: None,
        prompt_cache_key="opaque-key-that-must-not-be-forwarded",
    )
    assistant.generate_guidance(
        agent,
        step,
        "还需要哪些材料？",
        common
        | {
            "recent_messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "请说明产品信息。"},
            ]
        },
        [],
        [],
        "",
        on_delta=lambda _: None,
        prompt_cache_key="opaque-key-that-must-not-be-forwarded",
    )

    assert len(sent) == 2
    assert all(body["model"] == "deepseek-v4.1-flash" for body in sent)
    assert all(body["stream_options"] == {"include_usage": True} for body in sent)
    assert all("prompt_cache_key" not in body for body in sent)
    assert sent[0]["messages"][:2] == sent[1]["messages"][:2]
    assert json.loads(sent[0]["messages"][1]["content"]) == {
        "case_context": {"business_name": "六堡茶出口"}
    }
    assert sent[0]["messages"][-1] != sent[1]["messages"][-1]
    usage_records = [record for record in caplog.records if record.getMessage() == "provider_prompt_usage"]
    assert len(usage_records) == 2
    assert usage_records[-1].cached_tokens == 1280
    assert usage_records[-1].cache_miss_tokens == 520


def test_truncated_provider_stream_fails_without_saving_a_reply(chat, monkeypatch):
    client, case, payload, _ = chat
    provider(monkeypatch, lambda: iter([delta("未完成的片段")]))
    before = client.get(f"/api/tea/cases/{case['id']}/messages").json()["total"]
    response = client.post(f"/api/tea/cases/{case['id']}/messages/stream", json=payload)
    events = frames(response)
    assert any(event == "delta" for event, _ in events)
    assert events[-1][0] == "error"
    assert client.get(f"/api/tea/cases/{case['id']}/messages").json()["total"] == before


def test_cancel_and_duplicate_inflight_do_not_save_or_generate_twice(chat, monkeypatch):
    from app.streaming import StreamCancelled

    client, case, payload, owner = chat
    service = client.app.state.service
    entered, release = threading.Event(), threading.Event()
    calls = []

    def run(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        kwargs["emit"]("delta", {"text": "partial"})

    def cancel(*args):
        raise StreamCancelled()

    monkeypatch.setattr(service, "run_workflow", run)
    before = service.messages(owner, case["id"])["total"]
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(service.post_message, owner, case["id"], payload, emit=cancel)
        assert entered.wait(5)
        try:
            with pytest.raises(AppError, match="正在"):
                service.post_message(owner, case["id"], payload)
        finally:
            release.set()
        with pytest.raises(StreamCancelled):
            first.result(timeout=5)
    assert len(calls) == 1
    assert service.messages(owner, case["id"])["total"] == before


def test_stop_interrupts_a_provider_waiting_for_its_first_byte(monkeypatch):
    import asyncio
    import httpx
    from app.streaming import StreamCancelled

    entered, cancelled, closed = threading.Event(), threading.Event(), threading.Event()
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_API_KEY", "test-key")

    class WaitingResponse:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.set()

        def raise_for_status(self):
            pass

        async def aiter_bytes(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""

    class WaitingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def stream(self, *args, **kwargs):
            return WaitingResponse()

    monkeypatch.setattr(httpx, "AsyncClient", WaitingClient)

    def check_cancelled():
        if cancelled.is_set():
            raise StreamCancelled()

    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(
            assistant.generate_guidance,
            {"language": "zh", "instructions": ""},
            {},
            "",
            {},
            [],
            [],
            "",
            on_delta=lambda text: None,
            check_cancelled=check_cancelled,
        )
        try:
            assert entered.wait(1), "stream must use a cancellable provider transport"
        finally:
            cancelled.set()
        with pytest.raises(StreamCancelled):
            pending.result(timeout=1)
    assert closed.is_set()
