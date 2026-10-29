"""Bounded chat-completions transport; cancellation and response validation."""

import asyncio
import codecs
import json
import os
import re
from urllib.parse import urlsplit

import httpx

from app.contexts.conversations.application.tool_runtime import ToolFailure

_MAX_PROVIDER_BYTES = 4 * 1024 * 1024


def _uses_openai_endpoint(base):
    parsed = urlsplit(base)
    return parsed.scheme == "https" and parsed.hostname == "api.openai.com"


def _uses_aerisun_deepseek_endpoint(base, model):
    parsed = urlsplit(base)
    return (
        parsed.scheme == "https"
        and parsed.hostname == "aerisun.top"
        and parsed.path.rstrip("/") == "/sub2api"
        and model == "deepseek-v4.1-flash"
    )


def provider_choice(payload, *, allow_usage=False):
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError("provider error")
    choices = payload.get("choices")
    if choices == [] and allow_usage and isinstance(payload.get("usage"), dict):
        return None
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ValueError("invalid provider choices")
    choice = choices[0]
    # Explicit truncation, refusal or a tool request is never a finished answer.
    if choice.get("finish_reason") not in (None, "stop"):
        raise ValueError(f"provider did not finish the answer: finish_reason={choice.get('finish_reason')}")
    return choice


async def stream_guidance(base, body, on_delta, check_cancelled, on_usage=None, *, timeout=45):
    async def read():
        decoder = codecs.getincrementaldecoder("utf-8")()
        buffer, output, size, usage, finished = "", "", 0, None, False
        async with asyncio.timeout(timeout), httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            async with client.stream(
                "POST",
                base + "/chat/completions",
                headers={
                    "Authorization": "Bearer " + (os.getenv("AGENT_API_KEY") or os.environ["LLM_API_KEY"])
                },
                json=body,
            ) as response:
                response.raise_for_status()
                if not body.get("stream"):
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        if check_cancelled:
                            check_cancelled()
                        raw.extend(part)
                        if len(raw) > _MAX_PROVIDER_BYTES:
                            raise ToolFailure("invalid_response", "模型返回内容过大。")
                    try:
                        payload = json.loads(raw)
                        choice = provider_choice(payload)
                        message = choice.get("message", {})
                        if (
                            not isinstance(message, dict)
                            or message.get("tool_calls")
                            or message.get("refusal")
                        ):
                            raise ValueError("unexpected provider message")
                        if on_usage:
                            on_usage(payload.get("usage"))
                        return message.get("content")
                    except (ValueError, TypeError, AttributeError) as exc:
                        raise ToolFailure("invalid_response", "模型返回内容不完整或格式不正确。") from exc
                async for part in response.aiter_bytes():
                    if check_cancelled:
                        check_cancelled()
                    size += len(part)
                    if size > _MAX_PROVIDER_BYTES:
                        raise ToolFailure("invalid_response", "模型返回内容过大。")
                    buffer += decoder.decode(part)
                    while boundary := re.search(r"\r?\n\r?\n", buffer):
                        frame, buffer = buffer[: boundary.start()], buffer[boundary.end() :]
                        # SSE data fields belong to a complete event, including multiline JSON.
                        data = "\n".join(
                            line[5:].lstrip(" ") for line in frame.splitlines() if line.startswith("data:")
                        )
                        if not data:
                            continue
                        if data.strip() == "[DONE]":
                            if not output.strip():
                                raise ToolFailure("invalid_response", "模型没有返回有效正文。")
                            if usage is not None and on_usage:
                                on_usage(usage)
                            return output
                        try:
                            payload = json.loads(data)
                            choice = provider_choice(payload, allow_usage=True)
                            if isinstance(payload.get("usage"), dict):
                                usage = payload["usage"]
                            if choice is None:
                                continue
                            delta = choice.get("delta", {})
                            if not isinstance(delta, dict) or delta.get("tool_calls") or delta.get("refusal"):
                                raise ValueError("unexpected provider delta")
                            text = delta.get("content")
                            if text is not None:
                                if (
                                    not isinstance(text, str)
                                    or len(output) + len(text) > 20000
                                    or (finished and text)
                                ):
                                    raise ValueError("invalid provider stream content")
                                output += text
                                if text:
                                    on_delta(text)
                            finished = finished or choice.get("finish_reason") == "stop"
                        except (ValueError, TypeError, AttributeError) as exc:
                            raise ToolFailure("invalid_response", "模型流式内容不完整或格式不正确。") from exc
        raise ToolFailure("invalid_response", "模型连接中断，回复尚未完成。")

    async def watch_cancellation():
        while True:
            if check_cancelled:
                check_cancelled()
            await asyncio.sleep(0.05)

    reading = asyncio.create_task(read())
    watching = asyncio.create_task(watch_cancellation())
    try:
        done, _ = await asyncio.wait((reading, watching), return_when=asyncio.FIRST_COMPLETED)
        if watching in done:
            watching.result()
        return await reading
    finally:
        reading.cancel()
        watching.cancel()
        await asyncio.gather(reading, watching, return_exceptions=True)
