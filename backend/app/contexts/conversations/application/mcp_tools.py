"""Stateless MCP tool access to the same account-bound capability registry.

The HTTP adapter handles the transport. This service owns business scope,
validation, execution budgets and durable receipts; it accepts no SQL or paths.
"""

import hashlib
import logging
from contextlib import closing

from pydantic import ValidationError

from app.contexts.conversations.application.agent_journal import AgentJournal
from app.contexts.conversations.application.agent_tools import AgentTools
from app.contexts.conversations.application.tool_runtime import ToolFailure, ToolRun
from app.shared.domain.errors import AppError
from app.shared.domain.values import dump, uid

logger = logging.getLogger(__name__)
PROTOCOL_VERSIONS = ("2025-03-26", "2025-06-18", "2025-11-25")
EXPORTED_TOOLS = frozenset(
    {
        "tool_search",
        "load_skill",
        "guide_business",
        "check_materials",
        "get_handoff",
        "list_documents",
        "read_document",
        "inspect_document_file",
        "search_documents",
        "search_legal_evidence",
        "lookup_chinese_law",
        "read_law",
        "get_law_versions",
        "web_search",
        "inspect_image",
        "recall_memory",
    }
)


def rpc_error(identifier, code, message):
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


class MCPTools:
    def __init__(self, service, owner, case_id, stage_id):
        self.service, self.owner = service, owner
        with closing(service.db.connect()) as conn:
            self.case = service.agent_memory.scope(conn, owner, case_id, stage_id)
        self.stage = next(s for s in self.case["stages"] if s["id"] == stage_id)
        self.bound = AgentTools(service, owner, self.case, self.stage, {}, {}, "", lambda: None)
        self.bound.registry = {n: t for n, t in self.bound.registry.items() if n in EXPORTED_TOOLS}

    def dispatch(self, message):
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or not isinstance(message.get("method"), str)
            or ("id" in message and type(message["id"]) not in (int, str))
        ):
            return rpc_error(None, -32600, "Invalid Request")
        identifier, method, params = message.get("id"), message["method"], message.get("params", {})
        if not isinstance(params, dict):
            return rpc_error(identifier, -32602, "Invalid params")
        if "id" not in message:
            return None
        if method == "initialize":
            if not isinstance(params.get("protocolVersion"), str):
                return rpc_error(identifier, -32602, "protocolVersion is required")
            version = params["protocolVersion"]
            result = {
                "protocolVersion": version if version in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[-1],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "tea-workspace", "version": "1.0.0"},
                "instructions": "能力受当前账号、业务及节点限制。用tool_search发现工具，用load_skill按需读取SOP。",
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            if params.get("cursor"):
                return rpc_error(identifier, -32602, "Invalid cursor")
            result = {"tools": [t.mcp_schema() for t in self.bound.registry.values()]}
        elif method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if (
                not isinstance(name, str)
                or name not in self.bound.registry
                or not isinstance(arguments, dict)
            ):
                return rpc_error(identifier, -32602, "Unknown tool or invalid arguments")
            result = self.call(name, arguments, identifier)
        else:
            return rpc_error(identifier, -32601, "Method not found")
        return {"jsonrpc": "2.0", "id": identifier, "result": result}

    def call(self, name, arguments, identifier):
        tool = self.bound.registry[name]
        try:
            tool.spec.input_model.model_validate(arguments)
        except ValidationError:
            return {
                "content": [{"type": "text", "text": "工具参数不符合已发布的输入定义。"}],
                "isError": True,
            }
        # Each transport request is a distinct invocation. No implicit retry of
        # paid or result-unknown operations; the same chat service owns leases.
        payload = {
            "stage_id": self.stage["id"],
            "content": arguments.get("question", ""),
            "client_message_id": "mcp:" + uid(),
        }
        journal = AgentJournal(
            self.service, self.owner, self.case, payload, hashlib.sha256(dump(payload).encode()).hexdigest()
        )
        self.bound.payload, self.bound.run_id = payload, journal.id
        trace = {"id": "mcp", "label": "MCP工具调用", "status": "running", "tools": []}
        run = ToolRun(timeout_seconds=90, max_calls=1, catalog=[tool.spec])
        call_id = "mcp-call"
        journal.start_tool(call_id, name, arguments)
        try:
            value = run.call(tool.spec, arguments, tool.handler, trace)
        except (ToolFailure, AppError) as error:
            value = {"error": error.code, "message": str(error)[:300]}
        except Exception:
            logger.exception("mcp_tool_failed", extra={"tool_name": name, "run_id": journal.id})
            value = {"error": "execution_failed", "message": "工具执行失败，已保留实际调用记录。"}
        try:
            journal.finish_tool(call_id, value)
            trace["status"] = "failed" if value.get("error") else "completed"
            journal.finish(
                {
                    "trace": [trace],
                    "tool_result": value,
                    "_task_state": {"transport": "mcp", "rpc_id": identifier},
                }
            )
        except Exception:
            journal.finish()
            raise
        return {
            "content": [{"type": "text", "text": dump(value)}],
            "structuredContent": value,
            "isError": bool(value.get("error")),
        }
