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
            return ProviderResponse()

    monkeypatch.setattr(httpx, "AsyncClient", ProviderClient)


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
