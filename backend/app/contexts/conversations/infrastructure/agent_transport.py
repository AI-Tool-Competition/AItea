"""Bounded, cancellable OpenAI-compatible tool turns, including Gemini signatures."""

import asyncio
import codecs
import json
import os
import re
from urllib.parse import urlsplit

import httpx

from app.contexts.conversations.application.tool_runtime import ToolFailure


def provider_settings():
    return {
        "base": (
            os.getenv("AGENT_BASE_URL") or os.getenv("LLM_BASE_URL") or "https://aerisun.top/sub2api"
        ).rstrip("/"),
        "key": os.getenv("AGENT_API_KEY") or os.getenv("LLM_API_KEY") or "",
        "model": os.getenv("AGENT_MODEL") or "gemini-3.8-flash",
    }


async def cancellable(operation, check):
    async def watch():
        while True:
            check()
            await asyncio.sleep(0.05)

    running, watching = asyncio.create_task(operation), asyncio.create_task(watch())
    try:
        done, _ = await asyncio.wait((running, watching), return_when=asyncio.FIRST_COMPLETED)
        if watching in done:
            watching.result()
        return await running
    finally:
        running.cancel()
        watching.cancel()
        await asyncio.gather(running, watching, return_exceptions=True)


def valid_message(message, finish):
    if not isinstance(message, dict) or message.get("refusal"):
        raise ValueError("invalid message")
    content = message.get("content") or ""
    calls = message.get("tool_calls") or []
    if not isinstance(content, str) or len(content) > 20000 or not isinstance(calls, list) or len(calls) > 8:
        raise ValueError("invalid message bounds")
    if calls:
        if finish not in ("tool_calls", "stop"):
            raise ValueError("incomplete tools")
        seen = set()
        for call in calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ValueError("invalid call")
            identity, function = call.get("id"), call.get("function")
            if (
                not isinstance(identity, str)
                or not re.fullmatch(r"[\w.:-]{1,100}", identity)
                or identity in seen
            ):
                raise ValueError("invalid call id")
            seen.add(identity)
            if (
                not isinstance(function, dict)
                or not isinstance(function.get("name"), str)
                or not re.fullmatch(r"[a-z_]{1,64}", function["name"])
            ):
                raise ValueError("invalid function")
            arguments = function.get("arguments")
            if not isinstance(arguments, str) or len(arguments) > 24000:
                raise ValueError("invalid arguments")
    elif finish != "stop" or not content.strip():
        raise ValueError("incomplete answer")
    # Preserve provider-owned extra_content (Gemini thought signatures) on replay.
    return {
        key: value
        for key, value in {**message, "role": "assistant", "content": content}.items()
        if key in ("role", "content", "tool_calls", "extra_content")
    }


async def agent_turn(messages, tools, on_delta, check, *, timeout=90):
    settings = provider_settings()
    body = {
        "model": settings["model"],
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "max_tokens": 6000,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if len(json.dumps(body, ensure_ascii=False).encode()) > 256 * 1024:
        raise ToolFailure("input_limit", "上下文超过预算，请缩小问题范围。")

    async def read():
        decoder = codecs.getincrementaldecoder("utf-8")()
        buffer, size, finish, content = "", 0, None, ""
        calls, extra = {}, {}
        async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            async with client.stream(
                "POST",
                settings["base"] + "/chat/completions",
                json=body,
                headers={"Authorization": "Bearer " + settings["key"]},
            ) as response:
                response.raise_for_status()
                async for part in response.aiter_bytes():
                    check()
                    size += len(part)
                    if size > 1024 * 1024:
                        raise ToolFailure("invalid_response", "模型响应超过大小限制。")
                    buffer += decoder.decode(part)
                    while boundary := re.search(r"\r?\n\r?\n", buffer):
                        frame, buffer = buffer[: boundary.start()], buffer[boundary.end() :]
                        if any(
                            line.startswith("event:") and line[6:].strip() not in ("message", "")
                            for line in frame.splitlines()
                        ):
                            continue
                        data = "\n".join(
                            line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:")
                        )
                        if not data:
                            continue
                        try:
                            if data.strip() == "[DONE]":
                                message = {
                                    "content": content,
                                    "tool_calls": [calls[i] for i in sorted(calls)],
                                }
                                if extra:
                                    message["extra_content"] = extra
                                return valid_message(message, finish)
                            payload = json.loads(data)
                            if not isinstance(payload, dict) or "error" in payload:
                                raise ValueError("provider error")
                            choices = payload.get("choices")
                            if choices == [] and isinstance(payload.get("usage"), dict):
                                continue
                            if not isinstance(choices, list) or len(choices) != 1:
                                raise ValueError("invalid choice")
                            choice = choices[0]
                            delta = choice.get("delta", {})
                            if not isinstance(delta, dict) or delta.get("refusal") or finish is not None:
                                raise ValueError("unexpected delta")
                            text = delta.get("content") or ""
                            if not isinstance(text, str) or len(content) + len(text) > 20000:
                                raise ValueError("oversize content")
                            content += text
                            if text:
                                on_delta(text)
                            if isinstance(delta.get("extra_content"), dict):
                                extra.update(delta["extra_content"])
                            updates = delta.get("tool_calls") or []
                            if not isinstance(updates, list):
                                raise ValueError("invalid tool deltas")
                            for item in updates:
                                index = item.get("index")
                                if type(index) is not int or not 0 <= index < 8:
                                    raise ValueError("invalid tool index")
                                target = calls.setdefault(
                                    index,
                                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                                )
                                if item.get("id"):
                                    target["id"] += item["id"]
                                if item.get("type") not in (None, "function"):
                                    raise ValueError("invalid tool type")
                                for field in ("name", "arguments"):
                                    fragment = (item.get("function") or {}).get(field) or ""
                                    if not isinstance(fragment, str):
                                        raise ValueError("invalid function fragment")
                                    target["function"][field] += fragment
                                if isinstance(item.get("extra_content"), dict):
                                    target.setdefault("extra_content", {}).update(item["extra_content"])
                            finish = choice.get("finish_reason")
                            if finish not in (None, "stop", "tool_calls"):
                                raise ValueError("truncated reply")
                        except (ValueError, TypeError, AttributeError, KeyError) as exc:
                            raise ToolFailure("invalid_response", "模型工具响应不完整或格式不正确。") from exc
        raise ToolFailure("invalid_response", "模型连接中断，工具请求未执行。")

    return await cancellable(read(), check)


async def gateway_tool(name, payload, check, *, timeout):
    settings = provider_settings()
    base = (os.getenv("AGENT_TOOLS_BASE_URL") or settings["base"]).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ToolFailure("not_configured", "联网与识图服务需要配置 HTTPS 地址。")
    if name not in ("web_search", "image_recognition"):
        raise ToolFailure("unknown_tool", "未登记的外部工具。")

    async def read():
        async with (
            asyncio.timeout(timeout),
            httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client,
        ):
            async with client.stream(
                "POST",
                base + "/v1/tools/" + name,
                json=payload,
                headers={"Authorization": "Bearer " + settings["key"]},
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                async for part in response.aiter_bytes():
                    check()
                    raw.extend(part)
                    if len(raw) > 512 * 1024:
                        raise ToolFailure("invalid_response", "外部工具返回内容过大。")
                try:
                    value = json.loads(raw)
                    if (
                        not isinstance(value, dict)
                        or value.get("error")
                        or value.get("isError")
                        or value.get("status") != "completed"
                        or not isinstance(value.get("output"), dict)
                    ):
                        raise ValueError("invalid tool result")
                    output = value["output"]
                    if name == "web_search" and not isinstance(output.get("sources"), list):
                        raise ValueError("missing search sources")
                    if name == "image_recognition" and (
                        not isinstance(output.get("text"), str) or not output["text"].strip()
                    ):
                        raise ValueError("missing image observation")
                    return value
                except (ValueError, TypeError) as exc:
                    raise ToolFailure("invalid_response", "外部工具未返回有效结果。") from exc

    return await cancellable(read(), check)
