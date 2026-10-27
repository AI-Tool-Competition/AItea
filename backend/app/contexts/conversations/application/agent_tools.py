"""Account-bound tools; model arguments cannot choose owners, services or file paths."""

import asyncio
import base64
import hashlib
import re
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from app.contexts.conversations.application.agent_skills import COMPOSITION_HINTS, SKILLS, skill_catalog
from app.contexts.conversations.application.tool_runtime import ToolInput, ToolSpec
from app.contexts.conversations.infrastructure.agent_transport import gateway_tool
from app.shared.domain.countries import resolve_country
from app.shared.domain.errors import fail
from app.shared.domain.values import now


class EmptyInput(ToolInput):
    reason: str = Field(
        default="", max_length=300, description="可选的简短调用目的，不改变工具权限或执行规则。"
    )


class QueryInput(ToolInput):
    query: str = Field(min_length=1, max_length=2000)
    limit: int = Field(default=8, ge=1, le=20)


class SkillInput(ToolInput):
    name: Literal["guidance", "materials", "legal", "draft", "handoff", "research", "vision"]


class TasksInput(ToolInput):
    task_ids: list[str] = Field(
        default_factory=list,
        max_length=50,
        description="不传或 [] 核对当前节点全部任务；仅用户限定某任务时传服务端返回的任务 id，不推测编号。",
    )


class DocumentsInput(ToolInput):
    query: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0, le=10000)
    limit: int = Field(default=20, ge=1, le=50)


class DocumentInput(ToolInput):
    document_id: str = Field(min_length=1, max_length=100)
    version: int = Field(ge=1)
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=10, ge=1, le=30)


class ImageInput(ToolInput):
    document_id: str = Field(
        min_length=1, max_length=100, description="list_documents 返回的当前业务文档 id，不是文件名。"
    )
    version: int = Field(ge=1, description="该文档实际返回的不可变版本号。")
    prompt: str = Field(
        min_length=1, max_length=2000, description="只询问图中可见内容，不清楚的内容标为不确定。"
    )
    image_id: str | None = Field(
        default=None,
        max_length=200,
        description="原始 PNG/JPEG/WebP 图片必须省略或传 JSON null；仅查看文档页面图时传 read_document 返回的真实 image_id，不能填字符串 none/null 或猜测。",
    )

    @field_validator("image_id")
    @classmethod
    def real_page_id(cls, value):
        if value is not None and value.strip().lower() in ("", "none", "null", "undefined"):
            raise ValueError("原图请省略 image_id 或传 JSON null；页面图必须使用实际返回的编号")
        return value


class WebInput(ToolInput):
    query: str = Field(min_length=1, max_length=2000)
    domains: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="仅填用户指定或已由实际搜索结果确认的域名，不猜测政府域名后缀；不确定时省略或传 []。",
    )
    recency_days: int | None = Field(default=None, ge=1, le=3650)
    max_results: int = Field(default=5, ge=1, le=10)


class LegalInput(ToolInput):
    question: str = Field(min_length=1, max_length=4000)


class DraftInput(ToolInput):
    title: str = Field(min_length=1, max_length=120)
    content: str = Field(
        min_length=1,
        max_length=18000,
        description="完整 Markdown 正文。用户陈述标明来源，未核验内容不能写已确认；正文不包含工具名、任务编号或内部状态。",
    )
    task_id: str = Field(
        min_length=1,
        max_length=64,
        description="当前节点的真实任务 id；上下文或 guide_business 可取得，不使用任务标题或猜测编号。",
    )


class RememberInput(ToolInput):
    content: str = Field(min_length=1, max_length=2000)
    source_quote: str = Field(min_length=1, max_length=2000)
    kind: Literal["preference", "note"] = Field(
        default="note", description="回复格式、语言、先后顺序等表达偏好选 preference；业务事项笔记选 note。"
    )


class PlanInput(ToolInput):
    steps: list[str] = Field(min_length=1, max_length=8)
    current: int = Field(ge=0, le=8)


class ReceiptInput(ToolInput):
    call_id: str = Field(min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0, le=240000)
    limit: int = Field(default=8000, ge=1, le=12000)


@dataclass(frozen=True)
class BoundTool:
    spec: ToolSpec
    handler: object

    def schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.spec.name,
                "description": self.spec.description,
                "parameters": self.spec.input_model.model_json_schema(),
            },
        }


class AgentTools:
    def __init__(self, service, owner, case, stage, payload, prepared, run_id, check_cancelled):
        self.service, self.owner, self.case, self.stage = service, owner, case, stage
        self.payload, self.prepared, self.run_id = payload, prepared, run_id
        self.check_cancelled = check_cancelled
        definitions = [
            (
                "read_tool_result",
                "回查工具结果",
                "分页回查本轮已完成工具的持久收据。上下文压缩或结果过长时按 call_id 读取，不重新调用外部服务。",
                ReceiptInput,
                self.receipt,
                True,
                10,
            ),
            (
                "guide_business",
                "办理指导",
                "读取当前节点的办理要求、任务字段和服务端进度，用于制定下一步建议。",
                EmptyInput,
                self.guide,
                True,
                30,
            ),
            (
                "check_materials",
                "资料核对",
                "按现有服务端规则核对当前节点的材料缺口、确认状态和字段；不会将任务标记完成。",
                TasksInput,
                self.materials,
                True,
                30,
            ),
            (
                "analyze_legal",
                "法律分析",
                "运行已配置的法律分析引擎，保留法域、版本、审核与证据门禁；涉及法律结论时调用。",
                LegalInput,
                self.legal,
                False,
                720,
            ),
            (
                "draft_document",
                "文稿起草",
                "将完整 Markdown 文稿暂存到本轮提交；仅用户要求起草时调用，回复成功后保存为可编辑文稿。",
                DraftInput,
                self.draft,
                False,
                30,
            ),
            (
                "get_handoff",
                "节点交接",
                "读取前序及当前节点的服务端进度、复核标记，以及明确的下一节点。tasks 是节点内任务，不能当成下一节点；可与材料核对同时调用。",
                EmptyInput,
                self.handoff,
                True,
                30,
            ),
            (
                "discover_tools",
                "发现工具",
                "按名称或需求查询工具用法。登记工具均可直接调用，不需要先发现或激活；仅不清楚用法时查询。",
                QueryInput,
                self.discover,
                False,
                10,
            ),
            (
                "load_skill",
                "加载组合技能",
                "读取指定技能的组合提示；登记工具均已可用，不需要先加载技能，提示不强制固定调用顺序。",
                SkillInput,
                self.skill,
                False,
                10,
            ),
            (
                "list_documents",
                "列出业务资料",
                "分页查找当前业务及可读资料库的文档标识、版本和类型。",
                DocumentsInput,
                self.documents,
                True,
                30,
            ),
            (
                "read_document",
                "读取资料原文",
                "分页读取已有解析结果。未解析的材料返回真实状态，不能伪造正文。",
                DocumentInput,
                self.document,
                True,
                30,
            ),
            (
                "search_documents",
                "检索业务资料",
                "通过 RAGFlow 查找当前业务和允许资料库中的已审核原文，返回不可变版本和定位。",
                QueryInput,
                self.search,
                True,
                150,
            ),
            (
                "search_legal_evidence",
                "检索法律依据",
                "通过 RAGFlow 在当前业务允许的法域检索已审核且适用的法规依据。",
                QueryInput,
                self.legal_search,
                True,
                120,
            ),
            (
                "inspect_image",
                "识别图片",
                "分析当前业务原图或已有页面图。原始图片省略 image_id，即使正文解析失败也可查看原图；页面图只能使用 read_document 返回的真实编号。识图失败不能声称看到了图中文字，观察也不等于已确认事实。",
                ImageInput,
                self.image,
                True,
                90,
            ),
            (
                "web_search",
                "联网搜索",
                "实时搜索公开网络，保留来源链接和检索时间。域名不明时不要加 domains 限制；成功但无来源时可放宽域名或改关键词再查一次，仍无结果则如实说明。网页只是未审核线索，不要外发用户敏感信息。",
                WebInput,
                self.web,
                True,
                90,
            ),
            (
                "recall_memory",
                "检索记忆",
                "按当前账号、业务、节点检索已保存记忆、会话摘要及带来源的历史讨论。",
                QueryInput,
                self.recall,
                True,
                30,
            ),
            (
                "remember_memory",
                "保存用户记忆",
                "仅用户明确要求记住时，暂存原话中的偏好或笔记。必须提供当前消息中的逐字 source_quote。",
                RememberInput,
                self.remember,
                False,
                10,
            ),
            (
                "update_plan",
                "调整执行计划",
                "记录简短公开操作计划及当前步骤；不能标记业务任务完成，不输出思考过程。",
                PlanInput,
                self.plan,
                False,
                10,
            ),
        ]
        self.registry = {
            name: BoundTool(ToolSpec(name, label, description, model, 1, timeout, safe), handler)
            for name, label, description, model, handler, safe, timeout in definitions
        }

    def current(self):
        with closing(self.service.db.connect()) as conn:
            case = self.service.case_detail(self.owner, self.case["id"], conn)
            if self.payload.get("conversation_id"):
                self.service.node_workspace.own_conversation(
                    conn, self.owner, case["id"], self.payload["conversation_id"], self.stage["id"]
                )
            if case["revision"] != self.case["revision"]:
                fail(409, "agent_context_changed", "业务资料已变化，请重新发送后再使用本次结果")
            stage = next(s for s in case["stages"] if s["id"] == self.stage["id"])
            if stage["status"] == "locked":
                fail(409, "stage_locked", "节点尚未解锁")
            return case, stage

    def guide(self, args, attempt):
        case, stage = self.current()
        return {
            "stage": stage["name"],
            "tasks": stage["tasks"],
            "facts": case["facts"],
            "notice": "这是服务端当前办理要求；聊天建议不能替代任务核验。",
        }

    def materials(self, args, attempt):
        _, stage = self.current()
        tasks = {task["id"]: task for task in stage["tasks"]}
        selected = args.task_ids or list(tasks)
        if any(key not in tasks for key in selected):
            fail(422, "task_scope", "只能核对当前节点的任务")
        with closing(self.service.db.connect()) as conn:
            checks = []
            for key in dict.fromkeys(selected):
                attempt.check()
                task = tasks[key]
                checks.extend(
                    item | {"task_id": key, "task_title": task["title"]}
                    for item in self.service.checks(conn, task, task.get("confirmed", False))
                )
        return {
            "checks": checks,
            "notice": "缺项针对尚未填写或核验的服务端记录；用户在聊天中说过的信息应注明已提供、待填写或核验，不要重复索要。检查不会改变任务完成状态。",
        }

    def handoff(self, args, attempt):
        case, _ = self.current()
        index = next(i for i, stage in enumerate(case["stages"]) if stage["id"] == self.stage["id"])
        return {
            "current_stage_id": self.stage["id"],
            "stages": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "status": s["status"],
                    "tasks": [
                        {key: task.get(key) for key in ("id", "title", "status", "needs_review")}
                        for task in s["tasks"]
                    ],
                }
                for s in case["stages"][: index + 1]
            ],
            "facts": case["facts"],
            "next_stage": (
                {key: case["stages"][index + 1][key] for key in ("id", "name", "status")}
                if index + 1 < len(case["stages"])
                else None
            ),
            "notice": "stages 内的 tasks 是同节点任务，下一节点仅以 next_stage 为准；null 表示没有后续节点。状态直接来自服务端，不因聊天建议解锁。向用户使用节点名称和任务标题，不展示内部编号或状态码。",
        }

    def discover(self, args, attempt):
        words = args.query.lower().replace(",", " ").split()
        ranked = sorted(
            self.registry.values(),
            key=lambda tool: (
                -sum(
                    word in (tool.spec.name + tool.spec.description + tool.spec.label).lower()
                    for word in words
                )
            ),
        )
        names = [tool.spec.name for tool in ranked[: min(args.limit, 8)]]
        return {"activate_tools": names, "tools": [self.registry[name].schema() for name in names]}

    def skill(self, args, attempt):
        skill = SKILLS[args.name]
        return {
            "skill": args.name,
            "instructions": skill.instructions,
            "activate_tools": list(skill.tools),
            "combinations": COMPOSITION_HINTS,
        }

    def documents(self, args, attempt):
        self.current()
        result = self.service.documents(
            self.owner, self.case["id"], args.limit, args.offset, q=args.query or None
        )
        return {
            "items": [
                {
                    key: doc.get(key)
                    for key in ("id", "title", "version", "mime_type", "role", "status", "case_id")
                }
                for doc in result["items"]
            ],
            "total": result["total"],
            "offset": args.offset,
        }

    def scoped_document(self, document_id, version):
        self.current()
        with closing(self.service.db.connect()) as conn:
            doc = dict(self.service.read_document(conn, self.owner, document_id))
            if doc["case_id"] not in (None, self.case["id"]):
                fail(404, "not_found", "未找到当前业务的资料")
            if doc["version"] != version:
                fail(409, "document_version_changed", "资料版本不一致，请重新选择")
            return doc

    def document(self, args, attempt):
        self.scoped_document(args.document_id, args.version)
        result = self.service.document_detail(
            self.owner,
            args.document_id,
            element_offset=args.offset,
            chunk_offset=args.offset,
            limit=args.limit,
            include_preview=True,
        )
        return {
            key: result[key]
            for key in (
                "document",
                "elements",
                "chunks",
                "element_total",
                "chunk_total",
                "warnings",
                "preview",
            )
        }

    def search(self, args, attempt):
        self.current()
        return self.service.search(
            self.owner,
            args.query,
            self.case["id"],
            args.limit,
            country=resolve_country(self.case["template"]["destination"]) or "unassigned",
        )

    def legal_search(self, args, attempt):
        self.current()
        return self.service.legal.search(
            self.owner,
            args.query,
            self.case["id"],
            self.case["template"],
            [],
            args.limit,
            check_cancelled=attempt.check,
        )

    def legal(self, args, attempt):
        self.current()
        # The existing analysis service owns leases, evidence checks and version conflicts.
        key = hashlib.sha256(args.question.encode()).hexdigest()[:20]
        analysis = self.service.legal.analyze(
            self.owner,
            self.case["id"],
            {
                "stage_id": self.stage["id"],
                "question": args.question,
                "client_request_id": f"agent:{self.run_id}:{key}",
            },
            check_cancelled=attempt.check,
        )
        return {
            "analysis_id": analysis["id"],
            "revision": analysis["revision"],
            "status": analysis["status"],
            "result": analysis["result"],
        }

    def draft(self, args, attempt):
        _, stage = self.current()
        requested = "draft" in self.payload.get("skills", []) or re.search(
            r"(?:新建|创建|起草|撰写|编写|生成|写|拟).{0,18}(?:文稿|报告|合同|邮件|说明|方案|函|清单|报价|证明|计划)",
            self.payload["content"],
        )
        if not requested:
            fail(403, "draft_not_requested", "请先让用户明确要求创建文稿")
        if args.task_id not in {t["id"] for t in stage["tasks"]}:
            fail(422, "task_scope", "文稿必须属于当前节点任务")
        return {
            "draft": args.model_dump(),
            "status": "pending_commit",
            "notice": "正文已准备，本轮回复成功后与消息一起保存。",
        }

    def image(self, args, attempt):
        doc = self.scoped_document(args.document_id, args.version)
        if doc["case_id"] != self.case["id"]:
            fail(403, "image_scope", "识图仅接收当前业务资料")
        if args.image_id:
            content = self.service.library.document_image(self.owner, args.document_id, args.image_id)
            mime = "image/png"  # Existing preview adapter owns bounded page rendering.
        else:
            mime = doc["mime_type"]
            if (
                mime not in ("image/png", "image/jpeg", "image/webp")
                or not 0 < doc["size"] <= 8 * 1024 * 1024
            ):
                fail(422, "image_required", "请选择不超过 8 MB 的 PNG、JPEG、WebP 图片，或已有页面图")
            path = Path(doc["storage_path"]).resolve()
            if not path.is_relative_to(self.service.db.files_dir.resolve()):
                fail(422, "image_unavailable", "图片原件路径无效")
            with path.open("rb") as stream:
                content = stream.read(8 * 1024 * 1024 + 1)
            if hashlib.sha256(content).hexdigest() != doc["content_hash"]:
                fail(409, "image_changed", "原件校验不一致")
        if len(content) > 8 * 1024 * 1024 or mime not in ("image/png", "image/jpeg", "image/webp"):
            fail(422, "image_limit", "图片超过识别限制")
        from app.contexts.documents.infrastructure.providers.vision import (
            OCRCapacityDeferred,
            _VISION_REQUEST_SLOT,
            vision_ocr_provider,
        )

        shared = vision_ocr_provider() == "sub2api"
        if shared and not _VISION_REQUEST_SLOT.acquire(
            timeout=min(5, attempt.remaining), data_root=self.service.db.data_dir
        ):
            raise OCRCapacityDeferred()
        try:
            result = asyncio.run(
                gateway_tool(
                    "image_recognition",
                    {
                        "image": f"data:{mime};base64," + base64.b64encode(content).decode(),
                        "prompt": args.prompt,
                        "max_output_tokens": 2048,
                    },
                    attempt.check,
                    timeout=attempt.remaining,
                )
            )
        finally:
            if shared:
                _VISION_REQUEST_SLOT.release()
        return {
            "document_id": doc["id"],
            "document_version": doc["version"],
            "observation": result.get("output", {}).get("text", ""),
            "status": "unverified_observation",
            "notice": "仅为本轮图片观察，不改变解析、索引或审核状态。",
        }

    def web(self, args, attempt):
        self.current()
        if any(not re.fullmatch(r"(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,63}", host) for host in args.domains):
            fail(422, "invalid_domain", "搜索域名格式不正确")
        result = asyncio.run(
            gateway_tool(
                "web_search",
                args.model_dump(exclude_none=True) | {"response_length": "short"},
                attempt.check,
                timeout=attempt.remaining,
            )
        )
        output = result.get("output", {})
        sources = []
        for item in output.get("sources", [])[: args.max_results]:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            url = urlsplit(item["url"])
            if url.scheme != "https" or not url.hostname or url.username or url.password:
                continue
            sources.append(
                {
                    "title": str(item.get("title", ""))[:200],
                    "url": item["url"][:2000],
                    "excerpt": str(item.get("content") or item.get("snippet") or "")[:800],
                }
            )
        return {
            "sources": sources,
            "summary": str(output.get("summary", ""))[:1200],
            "retrieved_at": now(),
            "status": "unreviewed_web",
            "notice": "网页信息尚未作为法规入库审核。",
        }

    def recall(self, args, attempt):
        return self.service.agent_memory.recall(
            self.owner,
            self.case["id"],
            self.stage["id"],
            self.payload.get("conversation_id"),
            args.query,
            limit=min(args.limit, 10),
        )

    def remember(self, args, attempt):
        self.current()
        if (
            not re.search(
                r"记住|记下|保存.{0,5}记忆|以后.{0,6}(?:都|请)|remember", self.payload["content"], re.I
            )
            or args.source_quote not in self.payload["content"]
            or args.content not in args.source_quote
        ):
            fail(403, "memory_not_requested", "记忆必须来自用户当前明确要求保存的原话")
        return {
            "memory": {
                "content": args.content,
                "kind": args.kind,
                "conversation_id": self.payload.get("conversation_id"),
            },
            "status": "pending_commit",
        }

    def plan(self, args, attempt):
        if args.current > len(args.steps) or any(not step.strip() or len(step) > 160 for step in args.steps):
            fail(422, "invalid_plan", "计划步骤格式不正确")
        return {"plan": args.model_dump()}

    def catalog(self):
        return {
            "tools": [
                {
                    "name": t.spec.name,
                    "label": t.spec.label,
                    "description": t.spec.description,
                    "parallel": t.spec.concurrency_safe,
                }
                for t in self.registry.values()
            ],
            "skills": skill_catalog(),
            "combinations": COMPOSITION_HINTS,
        }

    def receipt(self, args, attempt):
        self.current()
        with closing(self.service.db.connect()) as conn:
            row = conn.execute(
                "SELECT t.name,t.status,t.result FROM agent_tool_receipts t JOIN agent_runs r ON r.id=t.run_id WHERE r.id=? AND r.owner_id=? AND r.case_id=? AND t.call_id=?",
                (self.run_id, self.owner, self.case["id"], args.call_id),
            ).fetchone()
        if not row or row["result"] is None:
            fail(404, "tool_result_unavailable", "没有找到已完成的工具收据")
        text = row["result"]
        return {
            "call_id": args.call_id,
            "tool": row["name"],
            "status": row["status"],
            "text": text[args.offset : args.offset + args.limit],
            "offset": args.offset,
            "total": len(text),
            "next_offset": args.offset + args.limit if args.offset + args.limit < len(text) else None,
        }
