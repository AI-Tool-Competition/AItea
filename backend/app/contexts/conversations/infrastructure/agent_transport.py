"""Bounded tool turns with separate provider thinking and lossless history replay."""

import asyncio
import codecs
import json
import os
import re
from urllib.parse import urlsplit

import httpx

from app.contexts.conversations.application.tool_runtime import ToolFailure


def provider_settings():
    model = os.getenv("AGENT_MODEL") or "Nemotron-3.5"
    return {
        "base": (
            os.getenv("AGENT_BASE_URL") or os.getenv("LLM_BASE_URL") or "https://aerisun.top/sub2api"
        ).rstrip("/"),
        "key": os.getenv("AGENT_API_KEY") or os.getenv("LLM_API_KEY") or "",
        "model": model,
        "reasoning_effort": reasoning_effort(model),
    }


def reasoning_effort(model):
    name = model.lower().rsplit("/", 1)[-1]
    effort = (os.getenv("AGENT_REASONING_EFFORT") or "").strip().lower()
    if not effort:
        return "high" if name.startswith("nemotron-3") else "low" if name.startswith("gemini-") else None
    if effort not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "auto"):
        raise ToolFailure("not_configured", "模型思考强度配置无效，请检查 AGENT_REASONING_EFFORT。")
    return effort


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
    reasoning = message.get("reasoning_content") or ""
    calls = message.get("tool_calls") or []
    if not isinstance(reasoning, str) or len(reasoning) > 60000:
        raise ValueError("invalid thinking bounds")
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
    # Keep provider thinking separate from the answer, but replay it verbatim
    # with tool results, like sub2api web chat. Never render opaque signatures.
    return {
        key: value
        for key, value in {**message, "role": "assistant", "content": content}.items()
        if key in ("role", "content", "reasoning_content", "tool_calls", "extra_content")
    }


async def agent_turn(
    messages,
    tools,
    on_delta,
    check,
    *,
    timeout=180,
    on_usage=None,
    on_summary=None,
    cache_key=None,
    request_key=None,
):
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
    if cache_key:
        body["prompt_cache_key"] = cache_key
    model_name = settings["model"].lower().rsplit("/", 1)[-1]
    gemini = model_name.startswith("gemini-")
    visible_thinking = gemini or model_name.startswith("nemotron-3")
    if settings["reasoning_effort"]:
        body["reasoning_effort"] = settings["reasoning_effort"]
    if gemini and on_summary:
        body["generationConfig"] = {"thinkingConfig": {"includeThoughts": True}}
    if len(json.dumps(body, ensure_ascii=False).encode()) > 256 * 1024:
        raise ToolFailure("input_limit", "上下文超过预算，请缩小问题范围。")

    async def read():
        decoder = codecs.getincrementaldecoder("utf-8")()
        buffer, size, finish, content, reasoning = "", 0, None, "", ""
        calls, extra = {}, {}
        usage = {}
        async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            async with client.stream(
                "POST",
                settings["base"] + "/chat/completions",
                json=body,
                headers={
                    "Authorization": "Bearer " + settings["key"],
                    **({"X-Session-Id": cache_key} if cache_key else {}),
                    **({"Idempotency-Key": request_key} if request_key else {}),
                },
            ) as response:
                response.raise_for_status()
                async for part in response.aiter_bytes():
                    check()
                    size += len(part)
                    # Nemotron can emit one small thinking fragment per token;
                    # SSE envelopes count toward this bound as well as content.
                    if size > 4 * 1024 * 1024:
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
                                if reasoning:
                                    message["reasoning_content"] = reasoning
                                valid = valid_message(message, finish)
                                if on_usage and usage:
                                    on_usage(usage)
                                return valid
                            payload = json.loads(data)
                            if not isinstance(payload, dict) or "error" in payload:
                                raise ValueError("provider error")
                            if isinstance(reported := payload.get("usage"), dict):
                                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                                    if type(reported.get(key)) is int and reported[key] >= 0:
                                        usage[key] = reported[key]
                                details = reported.get("prompt_tokens_details")
                                if (
                                    isinstance(details, dict)
                                    and type(details.get("cached_tokens")) is int
                                    and details["cached_tokens"] >= 0
                                ):
                                    usage["cached_tokens"] = details["cached_tokens"]
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
                            if visible_thinking:
                                summary = delta.get("reasoning_content") or delta.get("reasoning") or ""
                                if not isinstance(summary, str) or len(reasoning) + len(summary) > 60000:
                                    raise ValueError("invalid thinking delta")
                                reasoning += summary
                                if on_summary and summary:
                                    on_summary(summary)
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
