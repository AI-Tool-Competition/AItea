"""HTTP error, origin and payload boundaries."""

import logging
import os
import sqlite3
from urllib.parse import urlparse

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.contexts.knowledge.domain.documents import MAX_UPLOAD
from app.interfaces.http.request_limits import RequestLimitMiddleware
from app.shared.application import models as models
from app.shared.domain.errors import AppError
from app.shared.domain.values import uid
from app.shared.infrastructure.db import DatabaseUnavailable

logger = logging.getLogger(__name__)


def install(app):
    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        request.state.request_id = uid()
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            parsed = urlparse(origin)
            allowed = {request.headers.get("host", "")}
            if os.getenv("TEA_ENV", "development") != "production":
                allowed.update(
                    ("localhost:3000", "127.0.0.1:3000", "localhost:8000", "127.0.0.1:8000", "testserver")
                )
            configured = {
                value.strip().rstrip("/")
                for value in os.getenv("TEA_ALLOWED_ORIGINS", "").split(",")
                if value.strip()
            }
            origin_allowed = origin.rstrip("/") in configured or parsed.netloc in allowed
            if not origin_allowed or parsed.scheme not in ("http", "https"):
                return JSONResponse(
                    {
                        "error": {
                            "code": "invalid_origin",
                            "message": "请求来源无效",
                            "request_id": request.state.request_id,
                        }
                    },
                    status_code=403,
                )
        length = request.headers.get("content-length")
        if length:
            try:
                if int(length) > MAX_UPLOAD + 1024 * 1024:
                    return JSONResponse(
                        {"error": {"code": "request_too_large", "message": "请求体过大"}}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"error": {"code": "invalid_length", "message": "无效请求长度"}}, status_code=400
                )
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled_request", extra={"request_id": request.state.request_id})
            response = JSONResponse(
                {
                    "error": {
                        "code": "internal_error",
                        "message": "服务暂时不可用，请稍后重试",
                        "request_id": request.state.request_id,
                    }
                },
                status_code=500,
            )
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Compression proxies can buffer small SSE chunks until completion.
        response.headers["Cache-Control"] = (
            "no-store, no-transform"
            if response.headers.get("content-type", "").startswith("text/event-stream")
            else "no-store"
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(request, error):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message, "request_id": request.state.request_id}},
            status_code=error.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        message = "；".join(".".join(str(x) for x in e["loc"][1:]) + ": " + e["msg"] for e in error.errors())
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": message,
                    "request_id": request.state.request_id,
                }
            },
            status_code=422,
        )

    @app.exception_handler(DatabaseUnavailable)
    async def storage_unavailable(request, error):
        logger.error("application_storage_unavailable", extra={"request_id": request.state.request_id})
        return JSONResponse(
            {
                "error": {
                    "code": "database_unavailable",
                    "message": "资料存储暂不可用，请检查数据目录并完成恢复后重启服务",
                    "request_id": request.state.request_id,
                }
            },
            status_code=503,
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request, error):
        logger.exception("database_operation_failed")
        return JSONResponse(
            {
                "error": {
                    "code": "database_busy",
                    "message": "数据正在更新，请稍后重试",
                    "request_id": request.state.request_id,
                }
            },
            status_code=503,
        )

    app.add_middleware(RequestLimitMiddleware)
