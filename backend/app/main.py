import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager, closing
from pathlib import Path
from urllib.parse import quote, urlparse
from typing import Literal

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
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
    if not is_test:
        auth.ensure_default_admin()

    @asynccontextmanager
    async def lifespan(app):
        from .collection import CollectionWorkers
        from .scheduler import start_scheduler, stop_scheduler

        enabled = enable_scheduler if enable_scheduler is not None else not is_test
        handle = start_scheduler(service, enabled=enabled)
        workers = CollectionWorkers(service.collection).start() if enabled else None
        try:
            yield
        finally:
            await run_in_threadpool(stop_scheduler, handle)
            if workers:
                await run_in_threadpool(workers.stop)

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

    def admin_user(current=Depends(user)):
        if not current["is_admin"]:
            fail(403, "admin_required", "只有管理员可以进入管理后台")
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
        return {
            "status": "ok",
            "version": "0.1.0",
            **service.capabilities,
            "collector": dict(service.collection.worker_health),
        }

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
        if token:
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

    @app.get("/api/tea/admin/users")
    def admin_users(
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        q: str = Query("", max_length=160),
        current=Depends(admin_user),
    ):
        return auth.list_users(limit=limit, offset=offset, query=q)

    @app.patch("/api/tea/admin/users/{user_id}")
    def admin_update_user(
        user_id: str,
        payload: models.UserAccessPatch,
        current=Depends(admin_user),
    ):
        return auth.update_user_access(user_id, payload.model_dump(exclude_unset=True))

    @app.post("/api/tea/admin/bootstrap")
    def bootstrap_admin(payload: models.AdminBootstrapInput, current=Depends(user)):
        return auth.bootstrap_admin(current["id"], payload.bootstrap_token)

    @app.get("/api/tea/bootstrap")
    def bootstrap(current=Depends(user)):
        return {
            "user": current,
            "templates": service.templates(current["id"]),
            "cases": service.cases(current["id"])["items"],
            "capabilities": service.capabilities,
            "admin_setup_required": auth.admin_setup_required(),
        }

    @app.get("/api/tea/templates")
    def templates(current=Depends(admin_user)):
        items = service.templates(current["id"])
        return {"items": items, "total": len(items)}

    @app.post("/api/tea/templates/validate")
    def validate(payload: models.TemplateInput, current=Depends(admin_user)):
        return service.validate_template(payload.template)

    @app.post("/api/tea/templates/publish")
    def publish(payload: models.TemplatePublishInput, current=Depends(admin_user)):
        return service.publish(current["id"], payload.template, payload.expected_draft_revision)

    @app.post("/api/tea/templates/debug")
    def debug(payload: models.TemplateDebugInput, current=Depends(admin_user)):
        return service.studio.debug(current["id"], payload)

    @app.post("/api/tea/templates/debug/stream")
    def debug_stream(payload: models.TemplateDebugInput, current=Depends(admin_user)):
        from .streaming import debug_events

        return StreamingResponse(
            debug_events(service, current["id"], payload),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store, no-transform"},
        )

    @app.get("/api/tea/templates/debug/documents")
    def debug_documents(
        destination: str = Query(min_length=1, max_length=100),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        current=Depends(admin_user),
    ):
        return service.studio.debugger.documents(current["id"], destination, limit, offset)

    @app.get("/api/tea/templates/debug/runs/{run_id}")
    def debug_run(run_id: str, current=Depends(admin_user)):
        return service.studio.debugger.run(current["id"], run_id)

    @app.get("/api/tea/templates/{template_id}/debug-runs")
    def debug_runs(
        template_id: str,
        stage_id: str | None = Query(None, max_length=64),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        current=Depends(admin_user),
    ):
        return service.studio.debugger.runs(current["id"], template_id, stage_id, limit, offset)

    @app.get("/api/tea/templates/{template_id}/debug-samples")
    def debug_samples(
        template_id: str,
        stage_id: str | None = Query(None, max_length=64),
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0),
        current=Depends(admin_user),
    ):
        return service.studio.debugger.samples(current["id"], template_id, stage_id, limit, offset)

    @app.get("/api/tea/templates/{template_id}/debug-samples/{sample_id}")
    def debug_sample(template_id: str, sample_id: str, current=Depends(admin_user)):
        return service.studio.debugger.sample(current["id"], template_id, sample_id)

    @app.put("/api/tea/templates/{template_id}/debug-samples/{sample_id}")
    def save_debug_sample(
        template_id: str, sample_id: str, payload: models.DebugSampleInput, current=Depends(admin_user)
    ):
        return service.studio.debugger.save_sample(current["id"], template_id, sample_id, payload)

    @app.delete("/api/tea/templates/{template_id}/debug-samples/{sample_id}", status_code=204)
    def delete_debug_sample(
        template_id: str, sample_id: str, expected_revision: int = Query(ge=1), current=Depends(admin_user)
    ):
        service.studio.debugger.delete_sample(current["id"], template_id, sample_id, expected_revision)

    @app.get("/api/tea/templates/drafts")
    def template_drafts(
        limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(admin_user)
    ):
        return service.studio.drafts(current["id"], limit, offset)

    @app.get("/api/tea/templates/{template_id}/draft")
    def template_draft(template_id: str, current=Depends(admin_user)):
        return service.studio.draft(current["id"], template_id)

    @app.put("/api/tea/templates/{template_id}/draft")
    def save_template_draft(template_id: str, payload: models.TemplateDraftInput, current=Depends(admin_user)):
        return service.studio.save_draft(current["id"], template_id, payload)

    @app.get("/api/tea/templates/{template_id}")
    def template(template_id: str, current=Depends(admin_user)):
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

    @app.get("/api/tea/legal/policy")
    def legal_policy(current=Depends(user)):
        return service.legal.policy()

    @app.post("/api/tea/templates/legal/preview")
    def legal_preview(payload: models.LegalPreviewInput, current=Depends(admin_user)):
        return service.legal.preview(current["id"], payload.model_dump())

    @app.get("/api/tea/cases/{case_id}/legal")
    def legal_workspace(
        case_id: str,
        stage_id: str | None = None,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        current=Depends(user),
    ):
        return service.legal.workspace(current["id"], case_id, stage_id, limit, offset)

    @app.get("/api/tea/cases/{case_id}/legal/analyses/{analysis_id}")
    def legal_analysis(case_id: str, analysis_id: str, current=Depends(user)):
        return service.legal.get(current["id"], case_id, analysis_id)

    @app.patch("/api/tea/cases/{case_id}/legal/facts/{key}")
    def legal_fact(case_id: str, key: str, payload: models.LegalFactPatch, current=Depends(user)):
        return service.legal.patch_fact(current["id"], case_id, key, payload.model_dump())

    @app.post("/api/tea/cases/{case_id}/legal/analyses")
    def legal_analyze(case_id: str, payload: models.LegalAnalysisInput, current=Depends(user)):
        return service.legal.analyze(current["id"], case_id, payload.model_dump())

    @app.post("/api/tea/cases/{case_id}/legal/analyses/stream")
    def legal_analyze_stream(case_id: str, payload: models.LegalAnalysisInput, current=Depends(user)):
        from .streaming import workflow_events

        service.case_detail(current["id"], case_id)
        return StreamingResponse(
            workflow_events(
                lambda emit, check: {
                    "analysis": service.legal.analyze(
                        current["id"], case_id, payload.model_dump(), emit=emit, check_cancelled=check
                    )
                },
                models.LegalStreamEvent,
                "正在整理问题与事实…",
            ),
            media_type="text/event-stream",
        )

    @app.post("/api/tea/cases/{case_id}/legal/analyses/{analysis_id}/refresh")
    def legal_refresh(
        case_id: str, analysis_id: str, payload: models.LegalRefreshInput, current=Depends(user)
    ):
        return service.legal.analyze(current["id"], case_id, payload.model_dump(), analysis_id=analysis_id)

    @app.post("/api/tea/cases/{case_id}/legal/analyses/{analysis_id}/refresh/stream")
    def legal_refresh_stream(
        case_id: str, analysis_id: str, payload: models.LegalRefreshInput, current=Depends(user)
    ):
        from .streaming import workflow_events

        service.legal.get(current["id"], case_id, analysis_id)
        return StreamingResponse(
            workflow_events(
                lambda emit, check: {
                    "analysis": service.legal.analyze(
                        current["id"],
                        case_id,
                        payload.model_dump(),
                        analysis_id=analysis_id,
                        emit=emit,
                        check_cancelled=check,
                    )
                },
                models.LegalStreamEvent,
                "正在检查受影响的结论…",
            ),
            media_type="text/event-stream",
        )

    @app.patch("/api/tea/cases/{case_id}/legal/analyses/{analysis_id}/sections/{section_id}")
    def legal_section(
        case_id: str,
        analysis_id: str,
        section_id: str,
        payload: models.LegalSectionPatch,
        current=Depends(user),
    ):
        return service.legal.patch_section(
            current["id"], case_id, analysis_id, section_id, payload.model_dump()
        )

    @app.get("/api/tea/cases/{case_id}/legal/analyses/{analysis_id}/versions")
    def legal_versions(
        case_id: str,
        analysis_id: str,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        current=Depends(user),
    ):
        return service.legal.versions(current["id"], case_id, analysis_id, limit, offset)

    @app.get("/api/tea/cases/{case_id}/legal/analyses/{analysis_id}/versions/{revision}")
    def legal_version(case_id: str, analysis_id: str, revision: int, current=Depends(user)):
        return service.legal.version(current["id"], case_id, analysis_id, revision)

    @app.post("/api/tea/cases/{case_id}/stages/{stage_id}/analysis")
    def stage_analysis(
        case_id: str, stage_id: str, payload: models.StageAnalysisInput, current=Depends(user)
    ):
        return service.studio.analyze(current["id"], case_id, stage_id, payload)

    @app.post("/api/tea/cases/{case_id}/stages/{stage_id}/analysis/stream")
    def stage_analysis_stream(
        case_id: str, stage_id: str, payload: models.StageAnalysisInput, current=Depends(user)
    ):
        from .streaming import workflow_events

        service.case_detail(current["id"], case_id)
        return StreamingResponse(
            workflow_events(
                lambda emit, check: {
                    "analysis": service.studio.analyze(
                        current["id"], case_id, stage_id, payload, emit=emit, check_cancelled=check
                    )
                },
                models.LegalStreamEvent,
                "正在读取本节点的资料与分析记录…",
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
        )

    from .node_workspace_routes import install_node_workspace_routes

    install_node_workspace_routes(app, service, user)

    @app.patch("/api/tea/cases/{case_id}/tasks/{task_id}")
    def patch_task(case_id: str, task_id: str, payload: models.TaskPatch, current=Depends(user)):
        return service.patch_task(current["id"], case_id, task_id, payload.model_dump())

    @app.post("/api/tea/cases/{case_id}/tasks/{task_id}/verify")
    def verify(case_id: str, task_id: str, payload: models.Verify, background_tasks: BackgroundTasks, current=Depends(user)):
        result = service.verify_task(current["id"], case_id, task_id, payload.model_dump())
        background_tasks.add_task(service.node_workspace.process_pending, current["id"], case_id)
        return result

    @app.get("/api/tea/cases/{case_id}/events")
    def events(case_id: str, current=Depends(user)):
        items = service.case_detail(current["id"], case_id)["events"]
        return {"items": items, "total": len(items)}

    @app.get("/api/tea/cases/{case_id}/messages")
    def messages(
        case_id: str,
        background_tasks: BackgroundTasks,
        stage_id: str | None = None,
        conversation_id: str | None = Query(None, min_length=1, max_length=100),
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
        current=Depends(user),
    ):
        result = service.messages(current["id"], case_id, stage_id, limit, offset, conversation_id)
        background_tasks.add_task(service.node_workspace.process_pending, current["id"], case_id)
        return result

    @app.post("/api/tea/cases/{case_id}/messages")
    def post_message(case_id: str, payload: models.MessageInput, current=Depends(user)):
        return service.post_message(current["id"], case_id, payload.model_dump())

    @app.post("/api/tea/cases/{case_id}/messages/stream")
    def stream_message(case_id: str, payload: models.MessageInput, current=Depends(user)):
        from .streaming import message_events

        data = payload.model_dump()
        service.prepare_message(current["id"], case_id, data)
        return StreamingResponse(
            message_events(service, current["id"], case_id, data),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/tea/documents")
    def documents(
        case_id: str | None = None,
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
        role: Literal["primary", "secondary"] | None = None,
        parent_id: str | None = None,
        q: str | None = Query(None, max_length=200),
        legal_level: Literal["primary", "secondary", "attachment"] | None = None,
        collection: str | None = Query(None, min_length=1, max_length=100),
        country: str | None = Query(None, pattern=r"^(?:[A-Z]{2}|unassigned)$"),
        source_scope: Literal["personal", "system"] | None = None,
        library_only: bool = False,
        current=Depends(user),
    ):
        return service.documents(
            current["id"],
            case_id,
            limit,
            offset,
            role=role,
            parent_id=parent_id,
            q=q,
            legal_level=legal_level,
            collection=collection,
            country=country,
            source_scope=source_scope,
            library_only=library_only,
        )

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
            row = service.read_document(conn, current["id"], id)
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

    @app.get("/api/tea/documents/{id}/versions")
    def document_versions(
        id: str, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(user)
    ):
        return service.archive.versions(current["id"], id, limit, offset)

    @app.get("/api/tea/documents/{id}/relations")
    def document_relations(
        id: str, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(user)
    ):
        return service.archive.relations(current["id"], id, limit, offset)

    @app.patch("/api/tea/documents/{id}/classification")
    def document_classification(id: str, payload: models.ClassificationPatch, current=Depends(user)):
        return service.archive.patch_classification(current["id"], id, payload.model_dump())

    @app.post("/api/tea/documents/{id}/relations", status_code=201)
    def add_document_relation(id: str, payload: models.LegalRelationInput, current=Depends(user)):
        return service.archive.add_relation(current["id"], id, payload.model_dump())

    @app.post("/api/tea/search", response_model=models.SearchResult, response_model_exclude_none=True)
    def search(payload: models.Search, current=Depends(user)):
        return service.search(
            current["id"],
            payload.query,
            payload.case_id,
            payload.limit,
            payload.as_of,
            payload.collection,
            payload.country,
            source_scope=payload.source_scope,
        )

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
    def sources(limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(admin_user)):
        return service.sources(current["id"], limit, offset)

    @app.post("/api/tea/sources", status_code=201)
    def source_create(payload: models.SourceInput, current=Depends(admin_user)):
        return service.save_source(current["id"], payload.model_dump())

    @app.get("/api/tea/sources/{id}")
    def source_detail(id: str, current=Depends(admin_user)):
        with closing(service.db.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE id=? AND owner_id=?", (id, current["id"])
            ).fetchone()
            if not row:
                fail(404, "not_found", "未找到采集来源")
            return service.source_dict(row, conn)

    @app.patch("/api/tea/sources/{id}")
    def source_update(id: str, payload: models.SourcePatch, current=Depends(admin_user)):
        return service.save_source(current["id"], payload.model_dump(exclude_unset=True), id)

    @app.post("/api/tea/sources/{id}/fetch", status_code=202)
    def source_fetch(id: str, payload: models.CollectionStart | None = None, current=Depends(admin_user)):
        return service.fetch_source(current["id"], id, payload.model_dump() if payload else None)

    @app.post("/api/tea/sources/{id}/runs", status_code=202)
    def source_run(id: str, payload: models.CollectionStart, current=Depends(admin_user)):
        return service.collection.enqueue(current["id"], id, payload.model_dump())

    @app.get("/api/tea/sources/{id}/runs")
    def source_runs(
        id: str, limit: int = Query(20, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(admin_user)
    ):
        return service.collection.runs(current["id"], id, limit, offset)

    @app.get("/api/tea/collection-plans")
    def collection_plans(current=Depends(admin_user)):
        return service.checklists.catalog()

    @app.get("/api/tea/collection-plans/{id}")
    def collection_plan(id: str, current=Depends(admin_user)):
        return service.checklists.detail(current["id"], id)

    @app.post("/api/tea/collection-plans/{id}/configure")
    def configure_plan(id: str, payload: models.ChecklistConfigure, current=Depends(admin_user)):
        return service.checklists.configure(current["id"], id, payload.expected_version)

    @app.post("/api/tea/collection-plans/{id}/discover", status_code=202)
    def discover_plan_all(id: str, payload: models.ChecklistDiscover, current=Depends(admin_user)):
        return service.checklists.discover_all(current["id"], id, payload.client_request_id)

    @app.post("/api/tea/collection-plans/{id}/items/{item_id}/discover", status_code=202)
    def discover_plan(id: str, item_id: str, payload: models.ChecklistDiscover, current=Depends(admin_user)):
        return service.checklists.start(current["id"], id, item_id, payload.model_dump(), kind="discovery")

    @app.get("/api/tea/collection-plans/{id}/items/{item_id}/candidates")
    def plan_candidates(
        id: str,
        item_id: str,
        status: Literal["pending", "stored", "excluded"] = "pending",
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        current=Depends(admin_user),
    ):
        return service.checklists.candidates(current["id"], id, item_id, status, limit, offset)

    @app.post("/api/tea/collection-plans/{id}/items/{item_id}/collect", status_code=202)
    def collect_plan(id: str, item_id: str, payload: models.ChecklistCollect, current=Depends(admin_user)):
        return service.checklists.start(current["id"], id, item_id, payload.model_dump(), kind="collection")

    @app.patch("/api/tea/collection-candidates/{id}")
    def patch_candidate(id: str, payload: models.CandidatePatch, current=Depends(admin_user)):
        return service.checklists.patch_candidate(current["id"], id, payload.model_dump(exclude_none=True))

    @app.get("/api/tea/collection-runs/{id}")
    def collection_run(
        id: str, limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(admin_user)
    ):
        return service.collection.run_detail(current["id"], id, limit, offset)

    @app.post("/api/tea/collection-runs/{id}/retry")
    def retry_collection(id: str, payload: models.RevisionInput, current=Depends(admin_user)):
        return service.collection.control(current["id"], id, "retry", payload.expected_revision)

    @app.post("/api/tea/collection-runs/{id}/cancel")
    def cancel_collection(id: str, payload: models.RevisionInput, current=Depends(admin_user)):
        return service.collection.control(current["id"], id, "cancel", payload.expected_revision)

    app.add_middleware(RequestLimitMiddleware)
    return app


app = create_app()
