"""Conversation HTTP endpoints; business rules stay in application services."""

from fastapi import BackgroundTasks, Depends, Query
from fastapi.responses import StreamingResponse

from app.shared.application import models as models


def register(app, service, auth, user, admin_user):
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
        from app.shared.application.streaming import message_events

        data = payload.model_dump()
        service.prepare_message(current["id"], case_id, data)
        return StreamingResponse(
            message_events(service, current["id"], case_id, data),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
        )
