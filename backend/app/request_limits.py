"""Enforce body limits while ASGI reads, before framework parsing can spool freely."""

from __future__ import annotations

import json
from uuid import uuid4

from starlette.formparsers import MultiPartException
from starlette.types import ASGIApp, Message, Receive, Scope, Send


class BodyLimitExceeded(MultiPartException):
    """Multipart parser catches this type and closes every partially opened file."""


class RequestLimitMiddleware:
    def __init__(
        self, app: ASGIApp, *, multipart_limit: int = 21 * 1024 * 1024, json_limit: int = 1024 * 1024
    ):
        if min(multipart_limit, json_limit) <= 0:
            raise ValueError("Request body limits must be positive")
        self.app = app
        self.multipart_limit = multipart_limit
        self.json_limit = json_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        multipart = (
            headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower() == b"multipart/form-data"
        )
        limit = self.multipart_limit if multipart else self.json_limit
        consumed = 0
        exceeded = False
        response_started = False
        response_finished = False

        async def reject() -> None:
            nonlocal response_started, response_finished
            if response_finished:
                return
            if response_started:
                # Headers cannot be replaced after a streaming response starts.
                # Terminate its body once; normal API handlers read before send.
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                response_finished = True
                return
            state = scope.setdefault("state", {})
            request_id = state.setdefault("request_id", uuid4().hex)
            body = json.dumps(
                {
                    "error": {
                        "code": "request_too_large",
                        "message": "请求体超过允许大小，请缩小文件或内容后重试。",
                        "request_id": request_id,
                    }
                },
                ensure_ascii=False,
            ).encode("utf-8")
            response_started = True
            await send(
                {
                    "type": "http.response.start",
                    "status": 413,
                    "headers": [
                        (b"content-type", b"application/json; charset=utf-8"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"x-request-id", request_id.encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})
            response_finished = True

        # Declared length permits an early rejection; actual bytes remain the
        # authority when absent, understated, or transferred in chunks.
        declared = headers.get(b"content-length", b"")
        if declared.isdigit() and (len(declared) > 12 or int(declared) > limit):
            await reject()
            return

        async def limited_receive() -> Message:
            nonlocal consumed, exceeded
            if exceeded:
                raise BodyLimitExceeded("Request body exceeds the configured limit")
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    exceeded = True
                    # Starlette closes partial multipart files on this exact
                    # exception family, then converts it to HTTP 400. We map
                    # the final response to 413 without sending two starts.
                    raise BodyLimitExceeded("Request body exceeds the configured limit")
            return message

        async def limited_send(message: Message) -> None:
            nonlocal response_started, response_finished
            if exceeded:
                await reject()
                return
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response_finished = True
            await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if not exceeded:
                raise
            await reject()
        if exceeded:
            await reject()
