"""Collection HTTP endpoints; business rules stay in application services."""

from typing import Literal

from fastapi import Depends, Query

from app.shared.application import models as models


def register(app, service, auth, user, admin_user):
    @app.get("/api/tea/sources")
    def sources(
        limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0), current=Depends(admin_user)
    ):
        return service.sources(current["id"], limit, offset)

    @app.post("/api/tea/sources", status_code=201)
    def source_create(payload: models.SourceInput, current=Depends(admin_user)):
        return service.save_source(current["id"], payload.model_dump())

    @app.get("/api/tea/sources/{id}")
    def source_detail(id: str, current=Depends(admin_user)):
        return service.source_management.detail(current["id"], id)

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
        id: str,
        limit: int = Query(20, ge=1, le=200),
        offset: int = Query(0, ge=0),
        current=Depends(admin_user),
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
        id: str,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        current=Depends(admin_user),
    ):
        return service.collection.run_detail(current["id"], id, limit, offset)

    @app.post("/api/tea/collection-runs/{id}/retry")
    def retry_collection(id: str, payload: models.RevisionInput, current=Depends(admin_user)):
        return service.collection.control(current["id"], id, "retry", payload.expected_revision)

    @app.post("/api/tea/collection-runs/{id}/cancel")
    def cancel_collection(id: str, payload: models.RevisionInput, current=Depends(admin_user)):
        return service.collection.control(current["id"], id, "cancel", payload.expected_revision)
