import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import quote, urlparse
from typing import Literal

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import models
from .catalog import MetadataPatch
from .request_limits import RequestLimitMiddleware
from .auth import COOKIE, Auth
from .db import uid
from .service import MAX_UPLOAD, AppError, Service, fail

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
logger = logging.getLogger(__name__)


def create_app(data_dir=None, *, enable_scheduler=None):
    is_test = data_dir is not None
    service = Service(
        data_dir or os.getenv("TEA_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data"))
    )
    auth = Auth(service)

    @asynccontextmanager
    async def lifespan(app):
        from .scheduler import start_scheduler, stop_scheduler

        handle = start_scheduler(
            service, enabled=enable_scheduler if enable_scheduler is not None else not is_test
        )
        try:
            yield
        finally:
            await run_in_threadpool(stop_scheduler, handle)

    app = FastAPI(title="AItea API", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.state.auth = auth

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
        response.headers["Cache-Control"] = "no-store"
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

    def user(request: Request):
        current = auth.current(request.cookies.get(COOKIE))
        if not current:
            fail(401, "unauthenticated", "请先登录或开始体验")
        return current

    def set_session(response, token):
        secure = (
            os.getenv(
                "TEA_COOKIE_SECURE", "true" if os.getenv("TEA_ENV") == "production" else "false"
            ).lower()
            == "true"
        )
        response.set_cookie(
            COOKIE, token, httponly=True, secure=secure, samesite="lax", max_age=7 * 86400, path="/"
        )

    def throttle(request):
        auth.throttle(request.client.host if request.client else "unknown")

    @app.get("/api/tea/health")
    def health():
        return {"status": "ok", "version": "0.1.0", **service.capabilities}

    @app.post("/api/tea/auth/demo")
    def demo(request: Request, response: Response):
        throttle(request)
        current, token = auth.demo(request.cookies.get(COOKIE))
        set_session(response, token)
        return current

    @app.post("/api/tea/auth/register", status_code=201)
    def register(payload: models.Register, request: Request, response: Response):
        throttle(request)
        current, token = auth.register(payload.model_dump(), request.cookies.get(COOKIE))
        set_session(response, token)
        return current

    @app.post("/api/tea/auth/login")
    def login(payload: models.Login, request: Request, response: Response):
        throttle(request)
        current, token = auth.login(payload.model_dump(), request.cookies.get(COOKIE))
        set_session(response, token)
        return current

    @app.post("/api/tea/auth/logout", status_code=204)
    def logout(request: Request, response: Response):
        auth.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE, path="/")

    @app.get("/api/tea/auth/me")
    def me(current=Depends(user)):
        return current

    @app.get("/api/tea/bootstrap")
    def bootstrap(current=Depends(user)):
        return {
            "user": current,
            "templates": service.templates(current["id"]),
            "cases": service.cases(current["id"])["items"],
            "capabilities": service.capabilities,
        }

    @app.get("/api/tea/templates")
    def templates(current=Depends(user)):
        items = service.templates(current["id"])
        return {"items": items, "total": len(items)}

    @app.post("/api/tea/templates/validate")
    def validate(payload: models.TemplateInput, current=Depends(user)):
        return service.validate_template(payload.template)

    @app.post("/api/tea/templates/publish")
    def publish(payload: models.TemplatePublishInput, current=Depends(user)):
        return service.publish(current["id"], payload.template, payload.expected_draft_revision)

    @app.post("/api/tea/templates/debug")
    def debug(payload: models.TemplateDebugInput, current=Depends(user)):
        return service.studio.debug(current["id"], payload)

    @app.get("/api/tea/templates/drafts")
    def template_drafts(
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(user)
    ):
        return service.studio.drafts(current["id"], limit, offset)

    @app.get("/api/tea/templates/{template_id}/draft")
    def template_draft(template_id: str, current=Depends(user)):
        return service.studio.draft(current["id"], template_id)

    @app.put("/api/tea/templates/{template_id}/draft")
    def save_template_draft(template_id: str, payload: models.TemplateDraftInput, current=Depends(user)):
        return service.studio.save_draft(current["id"], template_id, payload)

    @app.get("/api/tea/templates/{template_id}")
    def template(template_id: str, current=Depends(user)):
        return service.template(current["id"], template_id)

    @app.get("/api/tea/cases")
    def cases(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(user)):
        return service.cases(current["id"], limit, offset)

    @app.post("/api/tea/cases", status_code=201)
    def create_case(payload: models.NewCase, current=Depends(user)):
        return service.create_case(current["id"], payload.model_dump())

    @app.get("/api/tea/cases/{case_id}")
    def case(case_id: str, current=Depends(user)):
        return service.case_detail(current["id"], case_id)

    @app.post("/api/tea/cases/{case_id}/stages/{stage_id}/analysis")
    def stage_analysis(
        case_id: str, stage_id: str, payload: models.StageAnalysisInput, current=Depends(user)
    ):
        return service.studio.analyze(current["id"], case_id, stage_id, payload)

    @app.patch("/api/tea/cases/{case_id}/tasks/{task_id}")
    def patch_task(case_id: str, task_id: str, payload: models.TaskPatch, current=Depends(user)):
        return service.patch_task(current["id"], case_id, task_id, payload.model_dump())

    @app.post("/api/tea/cases/{case_id}/tasks/{task_id}/verify")
    def verify(case_id: str, task_id: str, payload: models.Verify, current=Depends(user)):
        return service.verify_task(current["id"], case_id, task_id, payload.model_dump())

    @app.get("/api/tea/cases/{case_id}/events")
    def events(case_id: str, current=Depends(user)):
        items = service.case_detail(current["id"], case_id)["events"]
        return {"items": items, "total": len(items)}

    @app.get("/api/tea/cases/{case_id}/messages")
    def messages(
        case_id: str,
        stage_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
        current=Depends(user),
    ):
        return service.messages(current["id"], case_id, stage_id, limit, offset)

    @app.post("/api/tea/cases/{case_id}/messages")
    def post_message(case_id: str, payload: models.MessageInput, current=Depends(user)):
        return service.post_message(current["id"], case_id, payload.model_dump())

    @app.get("/api/tea/documents")
    def documents(
        case_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
        role: Literal["primary", "secondary"] | None = None,
        parent_id: str | None = None,
        q: str | None = Query(None, max_length=200),
        current=Depends(user),
    ):
        return service.documents(current["id"], case_id, limit, offset, role=role, parent_id=parent_id, q=q)

    @app.post("/api/tea/documents", status_code=201)
    async def upload(
        file: UploadFile = File(...),
        case_id: str | None = Form(None),
        role: str = Form("primary"),
        parent_id: str | None = Form(None),
        tags: str = Form("[]"),
        current=Depends(user),
    ):
        try:
            tags_list = json.loads(tags)
        except json.JSONDecodeError:
            fail(422, "invalid_tags", "标签格式无效")
        content = bytearray()
        try:
            while chunk := await file.read(1024 * 1024):
                content.extend(chunk)
                if len(content) > MAX_UPLOAD:
                    fail(413, "file_too_large", "文件不能超过 20 MB")
            return await run_in_threadpool(
                service.store_document,
                current["id"],
                file.filename or "document.bin",
                bytes(content),
                file.content_type or "",
                case_id=case_id or None,
                role=role,
                parent_id=parent_id or None,
                tags=tags_list,
            )
        finally:
            await file.close()

    @app.get("/api/tea/documents/{id}")
    def document(
        id: str,
        element_offset: int = Query(0, ge=0),
        chunk_offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=200),
        current=Depends(user),
    ):
        return service.document_detail(
            current["id"], id, element_offset=element_offset, chunk_offset=chunk_offset, limit=limit
        )

    @app.get("/api/tea/documents/{id}/download")
    def document_download(id: str, current=Depends(user)):
        with closing(service.db.connect()) as conn:
            row = service.own_document(conn, current["id"], id)
        return FileResponse(
            row["storage_path"],
            filename=row["filename"],
            media_type="application/octet-stream",
            content_disposition_type="attachment",
            headers={"Content-Security-Policy": "default-src 'none'; sandbox"},
        )

    @app.patch("/api/tea/documents/{id}/metadata")
    def document_metadata(id: str, payload: MetadataPatch, current=Depends(user)):
        service.catalog.patch_document(current["id"], id, payload, on_changed=service.metadata_changed)
        with closing(service.db.connect()) as conn:
            return service.document_dict(service.own_document(conn, current["id"], id))

    @app.post("/api/tea/search")
    def search(payload: models.Search, current=Depends(user)):
        return service.search(current["id"], payload.query, payload.case_id, payload.limit, payload.as_of)

    @app.post("/api/tea/cases/{case_id}/artifacts", status_code=201)
    def create_artifact(case_id: str, payload: models.NewArtifact, current=Depends(user)):
        return service.create_artifact(current["id"], case_id, payload.model_dump())

    @app.patch("/api/tea/artifacts/{id}")
    def patch_artifact(id: str, payload: models.ArtifactPatch, current=Depends(user)):
        return service.patch_artifact(current["id"], id, payload.model_dump())

    @app.post("/api/tea/artifacts/{id}/confirm")
    def confirm_artifact(id: str, payload: models.ArtifactConfirm, current=Depends(user)):
        return service.confirm_artifact(current["id"], id, payload.expected_version)

    @app.get("/api/tea/artifacts/{id}/versions")
    def versions(id: str, current=Depends(user)):
        with closing(service.db.connect()) as conn:
            service.own_artifact(conn, current["id"], id)
            items = [
                json.loads(r["payload"])
                for r in conn.execute(
                    "SELECT payload FROM artifact_versions WHERE artifact_id=? ORDER BY version DESC LIMIT 200",
                    (id,),
                )
            ]
            return {"items": items, "total": len(items)}

    @app.get("/api/tea/artifacts/{id}/download")
    def artifact_download(id: str, current=Depends(user)):
        with closing(service.db.connect()) as conn:
            row = service.own_artifact(conn, current["id"], id)
        return Response(
            row["content"],
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''" + quote(row["title"] + ".md")},
        )

    @app.get("/api/tea/sources")
    def sources(current=Depends(user)):
        return service.sources(current["id"])

    @app.post("/api/tea/sources", status_code=201)
    def source_create(payload: models.SourceInput, current=Depends(user)):
        return service.save_source(current["id"], payload.model_dump())

    @app.patch("/api/tea/sources/{id}")
    def source_update(id: str, payload: models.SourcePatch, current=Depends(user)):
        return service.save_source(current["id"], payload.model_dump(exclude_unset=True), id)

    @app.post("/api/tea/sources/{id}/fetch")
    def source_fetch(id: str, current=Depends(user)):
        return service.fetch_source(current["id"], id)

    app.add_middleware(RequestLimitMiddleware)
    return app


app = create_app()
