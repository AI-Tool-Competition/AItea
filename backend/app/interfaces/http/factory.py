import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from starlette.concurrency import run_in_threadpool

from app.bootstrap import Service
from app.contexts.accounts.application.auth import COOKIE, Auth
from app.contexts.documents.application.system_library import BackgroundWorkerScope
from app.configuration.environment import load_environment
from app.configuration.paths import InstancePaths
from app.shared.domain.errors import fail

load_environment()
logger = logging.getLogger(__name__)


def create_app(data_dir=None, *, enable_scheduler=None):
    is_test = data_dir is not None
    worker_scope = BackgroundWorkerScope(os.getenv("TEA_SHARED_LIBRARY_WORKERS", "internal").strip().lower())
    existing_data = os.getenv("TEA_REQUIRE_EXISTING_DATA", "false").strip().lower()
    if existing_data not in {"true", "false"}:
        raise ValueError("TEA_REQUIRE_EXISTING_DATA 必须为 true 或 false。")
    service = Service(
        data_dir or InstancePaths.resolve().data,
        require_existing=existing_data == "true",
    )
    auth = Auth(service)
    service.collection.worker_health["shared_library_workers"] = worker_scope.shared_library_workers
    if not is_test:
        auth.ensure_default_admin()

    @asynccontextmanager
    async def lifespan(app):
        if not is_test:
            os.environ.setdefault(
                "EMBEDDING_ADMISSION_PATH",
                str(InstancePaths.resolve().embedding_admission),
            )
        from app.contexts.ingestion.application.collection_jobs import CollectionWorkers
        from app.contexts.ingestion.application.official_source_import import OfficialSourceImporter
        from app.shared.infrastructure.operations.scheduler import start_scheduler, stop_scheduler

        enabled = enable_scheduler if enable_scheduler is not None else not is_test
        handle = start_scheduler(service, enabled=enabled, **worker_scope.kwargs())
        workers = CollectionWorkers(service.collection, **worker_scope.kwargs()).start() if enabled else None
        official_importer = None
        try:
            official_importer = OfficialSourceImporter(service).start() if enabled else None
            yield
        finally:
            if official_importer:
                await run_in_threadpool(official_importer.stop)
            await run_in_threadpool(stop_scheduler, handle)
            if workers:
                await run_in_threadpool(workers.stop)

    app = FastAPI(title="AItea API", version="0.1.0", lifespan=lifespan)
    app.state.service = service
    app.state.auth = auth

    def user(request: Request):
        current = auth.current(request.cookies.get(COOKIE))
        if not current:
            fail(401, "unauthenticated", "请先登录或开始体验")
        return current

    def admin_user(current=Depends(user)):
        if not current["is_admin"]:
            fail(403, "admin_required", "只有管理员可以进入管理后台")
        return current

    @app.get("/api/tea/health")
    def health():
        service.db.check_available()
        return {
            "status": "ok",
            "version": "0.1.0",
            **service.capabilities,
            "collector": dict(service.collection.worker_health),
        }

    from app.interfaces.http.node_workspace_routes import install_node_workspace_routes

    install_node_workspace_routes(app, service, user)

    from app.interfaces.http.boundaries import install
    from app.interfaces.http.routes import (
        collection,
        conversation,
        drafting,
        identity,
        knowledge,
        legal,
        workspace,
    )

    install(app)
    for routes in (identity, workspace, legal, conversation, knowledge, drafting, collection):
        routes.register(app, service, auth, user, admin_user)
    return app
