"""Account/case-scoped endpoints for node folders and immutable guide history."""

from fastapi import BackgroundTasks, Depends, Query

from app.shared.application.models import ConversationCreate, GuideRetry


def install_node_workspace_routes(app, service, user):
    @app.post("/api/tea/cases/{case_id}/stages/{stage_id}/guide-conversation")
    def guide_conversation(case_id: str, stage_id: str, current=Depends(user)):
        return service.node_workspace.guide_conversation(current["id"], case_id, stage_id)

    @app.get("/api/tea/cases/{case_id}/conversations")
    def conversations(
        case_id: str,
        background_tasks: BackgroundTasks,
        stage_id: str | None = Query(None, max_length=64),
        limit: int = Query(100, ge=1, le=200),
        offset: int = Query(0, ge=0),
        current=Depends(user),
    ):
        result = service.node_workspace.conversations(current["id"], case_id, limit, offset, stage_id)
        background_tasks.add_task(service.node_workspace.process_pending, current["id"], case_id)
        return result

    @app.post("/api/tea/cases/{case_id}/stages/{stage_id}/conversations")
    def create_conversation(case_id: str, stage_id: str, payload: ConversationCreate, current=Depends(user)):
        return service.node_workspace.create_conversation(
            current["id"], case_id, stage_id, payload.model_dump()
        )

    @app.get("/api/tea/cases/{case_id}/stages/{stage_id}/guides")
    def guides(
        case_id: str,
        stage_id: str,
        background_tasks: BackgroundTasks,
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        current=Depends(user),
    ):
        result = service.node_workspace.guides(current["id"], case_id, stage_id, limit, offset)
        background_tasks.add_task(service.node_workspace.process_pending, current["id"], case_id)
        return result

    @app.post("/api/tea/cases/{case_id}/stages/{stage_id}/guides/{guide_id}/retry")
    def retry_guide(
        case_id: str,
        stage_id: str,
        guide_id: str,
        payload: GuideRetry,
        background_tasks: BackgroundTasks,
        current=Depends(user),
    ):
        result = service.node_workspace.retry_guide(
            current["id"], case_id, stage_id, guide_id, payload.client_request_id
        )
        background_tasks.add_task(service.node_workspace.process_pending, current["id"], case_id)
        return result
