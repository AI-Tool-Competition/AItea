"""Legal HTTP endpoints; business rules stay in application services."""

from fastapi import Depends, Query
from fastapi.responses import StreamingResponse

from app.shared.application import models as models


def register(app, service, auth, user, admin_user):
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
        from app.shared.application.streaming import workflow_events

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
        from app.shared.application.streaming import workflow_events

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
