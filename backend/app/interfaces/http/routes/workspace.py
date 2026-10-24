"""Workspace HTTP endpoints; business rules stay in application services."""

from fastapi import BackgroundTasks, Depends, Query
from fastapi.responses import StreamingResponse

from app.shared.application import models as models


def register(app, service, auth, user, admin_user):
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
        from app.shared.application.streaming import debug_events

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
    def save_template_draft(
        template_id: str, payload: models.TemplateDraftInput, current=Depends(admin_user)
    ):
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

    @app.post("/api/tea/cases/{case_id}/stages/{stage_id}/analysis")
    def stage_analysis(
        case_id: str, stage_id: str, payload: models.StageAnalysisInput, current=Depends(user)
    ):
        return service.studio.analyze(current["id"], case_id, stage_id, payload)

    @app.post("/api/tea/cases/{case_id}/stages/{stage_id}/analysis/stream")
    def stage_analysis_stream(
        case_id: str, stage_id: str, payload: models.StageAnalysisInput, current=Depends(user)
    ):
        from app.shared.application.streaming import workflow_events

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

    @app.patch("/api/tea/cases/{case_id}/tasks/{task_id}")
    def patch_task(case_id: str, task_id: str, payload: models.TaskPatch, current=Depends(user)):
        return service.patch_task(current["id"], case_id, task_id, payload.model_dump())

    @app.post("/api/tea/cases/{case_id}/tasks/{task_id}/verify")
    def verify(
        case_id: str,
        task_id: str,
        payload: models.Verify,
        background_tasks: BackgroundTasks,
        current=Depends(user),
    ):
        result = service.verify_task(current["id"], case_id, task_id, payload.model_dump())
        background_tasks.add_task(service.node_workspace.process_pending, current["id"], case_id)
        return result

    @app.get("/api/tea/cases/{case_id}/events")
    def events(case_id: str, current=Depends(user)):
        items = service.case_detail(current["id"], case_id)["events"]
        return {"items": items, "total": len(items)}
