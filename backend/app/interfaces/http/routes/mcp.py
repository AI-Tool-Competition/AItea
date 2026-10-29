"""Stateless Streamable HTTP MCP endpoint using the existing account session."""

from fastapi import Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.contexts.conversations.application.mcp_tools import MCPTools, PROTOCOL_VERSIONS, rpc_error


def register(app, service, auth, user, admin_user):
    endpoint = "/api/tea/cases/{case_id}/stages/{stage_id}/mcp"

    @app.post(endpoint)
    async def message(case_id: str, stage_id: str, request: Request, current=Depends(user)):
        # Scope is authorized before initialization, discovery or execution.
        server = await run_in_threadpool(MCPTools, service, current["id"], case_id, stage_id)
        accept = request.headers.get("accept", "")
        if not all(mime in accept for mime in ("application/json", "text/event-stream")):
            return JSONResponse(
                rpc_error(None, -32600, "Accept must include application/json and text/event-stream"),
                status_code=406,
            )
        version = request.headers.get("mcp-protocol-version")
        if version and version not in PROTOCOL_VERSIONS:
            return JSONResponse(rpc_error(None, -32600, "Unsupported protocol version"), status_code=400)
        try:
            value = await request.json()
        except ValueError:
            return JSONResponse(rpc_error(None, -32700, "Parse error"), status_code=400)
        response = await run_in_threadpool(server.dispatch, value)
        return Response(status_code=202) if response is None else JSONResponse(response)

    @app.api_route(endpoint, methods=["GET", "DELETE"])
    def unsupported(case_id: str, stage_id: str, current=Depends(user)):
        MCPTools(service, current["id"], case_id, stage_id)
        # This server has no server-initiated stream or transport session.
        return Response(status_code=405, headers={"Allow": "POST"})
