"""Bounded tool turns with separate provider thinking and lossless history replay."""

import asyncio
import codecs
import json
import math
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
        "legal_reasoning_effort": reasoning_effort(model, os.getenv("LEGAL_REASONING_EFFORT")),
    }


def reasoning_effort(model, override=None):
    name = model.lower().rsplit("/", 1)[-1]
    effort = (override or os.getenv("AGENT_REASONING_EFFORT") or "").strip().lower()
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
    valid = {
        key: value
        for key, value in {**message, "role": "assistant", "content": content}.items()
        if key in ("role", "content", "reasoning_content", "tool_calls", "extra_content")
    }
    if not calls:
        valid.pop("tool_calls", None)
    return valid


def invalid_response(reason):
    """Persist a controlled protocol diagnostic without response text or secrets."""
    error = ToolFailure("invalid_response", f"模型响应不完整或格式不正确（{reason}）。")
    error.response_error = reason
    return error


def stream_idle_timeout():
    try:
        value = float(os.getenv("AGENT_STREAM_IDLE_TIMEOUT", "30"))
    except ValueError:
        value = 0
    if not math.isfinite(value) or not 0 < value <= 120:
        raise ToolFailure("not_configured", "模型流空闲超时须为0至120秒之间的有效正数。")
    return value


def provider_stream_error(payload, *, retryable):
    """Classify a confirmed failure without retaining the provider's message."""
    detail = payload.get("error")
    detail = detail if isinstance(detail, dict) else {}
    status = detail.get("status") or detail.get("status_code") or payload.get("status_code")
    code = detail.get("code") or detail.get("type")
    code = code if isinstance(code, str) else None
    denied = str(status) in {"401", "403"} or code in {
        "invalid_api_key",
        "authentication_error",
        "permission_denied",
        "insufficient_quota",
    }
    limited = str(status) == "429" or code in {"rate_limit_exceeded", "rate_limit_error"}
    permanent = str(status) in {"400", "404", "405", "422"} or code in {
        "invalid_request_error",
        "context_length_exceeded",
        "model_not_found",
    }
    error = ToolFailure(
        "access_denied" if denied else "rate_limited" if limited else "upstream_error",
        "模型供应商报告生成失败。",
    )
    error.response_error = "provider_stream_error"
    error.retryable = retryable and not (denied or limited or permanent)
    return error


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
    tool_choice="auto",
    max_tokens=6000,
):
    settings = provider_settings()
    body = {
        "model": settings["model"],
        "messages": [
            {key: value for key, value in message.items() if not (key == "tool_calls" and value == [])}
            for message in messages
        ],
        **(
            {"tools": tools, "tool_choice": tool_choice}
            if tools
            else {"tool_choice": "none"}
            if tool_choice == "none"
            else {}
        ),
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if cache_key:
        body["prompt_cache_key"] = cache_key
    model_name = settings["model"].lower().rsplit("/", 1)[-1]
    gemini = model_name.startswith("gemini-")
    visible_thinking = gemini or model_name.startswith("nemotron-3")
    capture_reasoning = visible_thinking or model_name.startswith("deepseek-")
    if settings["reasoning_effort"]:
        body["reasoning_effort"] = settings["reasoning_effort"]
    if urlsplit(settings["base"]).hostname == "api.deepseek.com" and (
        tools or settings["reasoning_effort"] == "none"
    ):
        # DeepSeek rejects required/named tool choice in thinking mode. Keep
        # capability dispatch non-thinking; final composition uses the chosen
        # effort and has no tools. This also avoids mixed-mode tool histories.
        body["thinking"] = {"type": "disabled"}
        body["reasoning_effort"] = "none"
    elif urlsplit(settings["base"]).hostname == "api.deepseek.com" and settings["reasoning_effort"]:
        body["thinking"] = {"type": "enabled"}
    if gemini and on_summary:
        body["generationConfig"] = {"thinkingConfig": {"includeThoughts": True}}
    if len(json.dumps(body, ensure_ascii=False).encode()) > 256 * 1024:
        raise ToolFailure("input_limit", "上下文超过预算，请缩小问题范围。")

    usage = {}
    received = {"content": "", "tool_calls": False, "reasoning_characters": 0, "finish_reason": None}
    idle_seconds = stream_idle_timeout()
    idle_guard = None

    async def read():
        nonlocal idle_guard
        decoder = codecs.getincrementaldecoder("utf-8")()
        buffer, size, finish, content, reasoning = "", 0, None, "", ""
        calls, extra = {}, {}

        def assembled():
            message = {"content": content, "tool_calls": [calls[i] for i in sorted(calls)]}
            if extra:
                message["extra_content"] = extra
            if reasoning:
                message["reasoning_content"] = reasoning
            if finish == "length":
                raise ToolFailure("generation_truncated", "模型已达到输出长度上限，未执行不完整工具请求。")
            try:
                return valid_message(message, finish)
            except ValueError as error:
                raise invalid_response(str(error)) from error

        network_timeout = httpx.Timeout(
            timeout, connect=min(10, timeout), read=min(idle_seconds, timeout), pool=min(10, timeout)
        )
        async with (
            asyncio.timeout(timeout),
            asyncio.timeout(idle_seconds) as idle_guard,
            httpx.AsyncClient(timeout=network_timeout, trust_env=False) as client,
        ):
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
                        raise invalid_response("response_size_limit")
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
                                return assembled()
                            payload = json.loads(data)
                            if not isinstance(payload, dict):
                                raise ValueError("invalid frame")
                            if payload.get("error"):
                                raise provider_stream_error(payload, retryable=not content and not calls)
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
                                completion_details = reported.get("completion_tokens_details")
                                if (
                                    isinstance(completion_details, dict)
                                    and type(completion_details.get("reasoning_tokens")) is int
                                    and completion_details["reasoning_tokens"] >= 0
                                ):
                                    usage["reasoning_tokens"] = completion_details["reasoning_tokens"]
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
                            if capture_reasoning:
                                summary = delta.get("reasoning_content") or delta.get("reasoning") or ""
                                if not isinstance(summary, str) or len(reasoning) + len(summary) > 60000:
                                    raise ValueError("invalid thinking delta")
                                reasoning += summary
                                received["reasoning_characters"] = len(reasoning)
                                if visible_thinking and on_summary and summary:
                                    on_summary(summary)
                            if not isinstance(text, str) or len(content) + len(text) > 20000:
                                raise ValueError("oversize content")
                            content += text
                            received["content"] = content
                            if text:
                                on_delta(text)
                            if isinstance(delta.get("extra_content"), dict):
                                extra.update(delta["extra_content"])
                            updates = delta.get("tool_calls") or []
                            received["tool_calls"] |= bool(updates)
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
                            received["finish_reason"] = finish
                            if finish not in (None, "stop", "tool_calls", "length"):
                                raise ValueError("truncated reply")
                            if text or updates or (capture_reasoning and summary) or finish:
                                # Heartbeats and empty gateway frames cannot extend a stalled request.
                                idle_guard.reschedule(asyncio.get_running_loop().time() + idle_seconds)
                        except (ValueError, TypeError, AttributeError, KeyError) as exc:
                            known = {
                                "invalid message",
                                "invalid thinking bounds",
                                "invalid message bounds",
                                "incomplete tools",
                                "invalid call",
                                "invalid call id",
                                "invalid function",
                                "invalid arguments",
                                "incomplete answer",
                                "provider error",
                                "invalid choice",
                                "unexpected delta",
                                "invalid thinking delta",
                                "oversize content",
                                "invalid tool deltas",
                                "invalid tool index",
                                "invalid tool type",
                                "invalid function fragment",
                                "truncated reply",
                            }
                            reason = (
                                str(exc)
                                if str(exc) in known
                                else "invalid_json"
                                if isinstance(exc, json.JSONDecodeError)
                                else "invalid_frame"
                            )
                            raise invalid_response(reason) from exc
        # Some compatible providers close a fully finished SSE stream without
        # a redundant [DONE] sentinel. Never accept a missing finish or frame.
        if finish in {"stop", "tool_calls"} and not buffer.strip():
            return assembled()
        raise invalid_response("missing_done")

    try:
        return await cancellable(read(), check)
    except Exception as error:
        if (
            not received["content"]
            and not received["tool_calls"]
            and (
                isinstance(error, (httpx.ReadTimeout, httpx.RemoteProtocolError))
                or (isinstance(error, TimeoutError) and idle_guard is not None and idle_guard.expired())
            )
        ):
            # Only a model generation is replayable here. No tool request has
            # been delivered or executed, so business writes cannot duplicate.
            stalled = ToolFailure("model_stream_stalled", "模型响应中断，尚未收到答复或工具请求。")
            stalled.retryable = True
            stalled.partial_content = ""
            stalled.generation = {"content_characters": 0, "incomplete_tool_calls": False}
            raise stalled from error
        # Preserve public answer text only. Thinking and incomplete tool
        # requests can never become a partial business/legal deliverable.
        error.partial_content = "" if received["tool_calls"] else received["content"]
        error.generation = {
            "finish_reason": received["finish_reason"],
            "content_characters": len(received["content"]),
            "reasoning_characters": received["reasoning_characters"],
            "incomplete_tool_calls": received["tool_calls"],
            "max_tokens": max_tokens,
        }
        raise
    finally:
        if on_usage and usage:
            on_usage(dict(usage))


async def gateway_tool(name, payload, check, *, timeout):
    settings = provider_settings()
    base = (os.getenv("AGENT_TOOLS_BASE_URL") or settings["base"]).rstrip("/")
    key = os.getenv("AGENT_TOOLS_API_KEY") or settings["key"]
    endpoint, request = base + "/v1/tools/" + name, {"json": payload}
    if name == "image_recognition":
        from app.contexts.documents.infrastructure.providers.vision import (
            _vision_api_key,
            vision_ocr_endpoint,
            vision_ocr_provider,
        )

        if vision_ocr_provider() == "sub2api":
            import base64

            try:
                header, encoded = payload["image"].split(",", 1)
                mime = header.removeprefix("data:").removesuffix(";base64")
                if mime not in {"image/png", "image/jpeg", "image/webp"} or not header.endswith(";base64"):
                    raise ValueError("invalid image")
                content = base64.b64decode(encoded, validate=True)
                if not 0 < len(content) <= 8 * 1024 * 1024:
                    raise ValueError("invalid image size")
            except (KeyError, ValueError, TypeError) as exc:
                raise ToolFailure("invalid_input", "识图输入必须是已绑定的有效图片。") from exc
            endpoint, key = vision_ocr_endpoint(), _vision_api_key()
            request = {
                "files": {"image": ("image." + mime.split("/")[1], content, mime)},
                "data": {"prompt": payload["prompt"]},
            }
    parsed = urlsplit(endpoint)
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
                endpoint,
                **request,
                headers={"Authorization": "Bearer " + key},
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
