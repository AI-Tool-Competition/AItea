import copy
import hashlib
import json
import logging
import re
from contextlib import closing

from app.shared.domain.errors import fail
from app.shared.domain.values import dump, now, uid

logger = logging.getLogger(__name__)


class ConversationService:
    def __init__(self, app):
        self.app = app

    def tool_catalog(self, owner, case_id, stage_id):
        from app.contexts.conversations.application.agent_tools import AgentTools

        with closing(self.app.db.connect()) as conn:
            case = self.app.agent_memory.scope(conn, owner, case_id, stage_id)
        stage = next(s for s in case["stages"] if s["id"] == stage_id)
        return AgentTools(self.app, owner, case, stage, {}, {}, "", lambda: None).catalog()

    def insert_message(self, conn, case_id, stage_id, task_id, role, content, citations, trace):
        message = {
            "id": uid(),
            "role": role,
            "content": content,
            "stage_id": stage_id,
            "task_id": task_id,
            "created_at": now(),
            "citations": citations,
            "trace": trace,
        }
        conn.execute(
            "INSERT INTO messages VALUES (?,?,?,?,?,?,?,?,?)",
            (
                message["id"],
                case_id,
                stage_id,
                task_id,
                role,
                content,
                dump(citations),
                dump(trace),
                message["created_at"],
            ),
        )
        return message

    def messages(self, owner, case_id, stage_id=None, limit=100, offset=0, conversation_id=None):
        with closing(self.app.db.connect()) as conn:
            self.app.own_case(conn, owner, case_id)
            if conversation_id:
                self.app.node_workspace.own_conversation(conn, owner, case_id, conversation_id, stage_id)
            rows = conn.execute(
                """SELECT m.*,cm.conversation_id,gr.result guide_result FROM messages m LEFT JOIN conversation_messages cm ON cm.message_id=m.id
                LEFT JOIN guide_messages gm ON gm.message_id=m.id LEFT JOIN stage_guide_runs gr ON gr.id=gm.guide_id
                WHERE m.case_id=? AND (? IS NULL OR m.stage_id=?) AND (? IS NULL OR cm.conversation_id=?)
                ORDER BY m.created_at DESC,m.id DESC LIMIT ? OFFSET ?""",
                (case_id, stage_id, stage_id, conversation_id, conversation_id, limit, offset),
            ).fetchall()
            total = conn.execute(
                """SELECT COUNT(*) n FROM messages m LEFT JOIN conversation_messages cm ON cm.message_id=m.id
                WHERE m.case_id=? AND (? IS NULL OR m.stage_id=?) AND (? IS NULL OR cm.conversation_id=?)""",
                (case_id, stage_id, stage_id, conversation_id, conversation_id),
            ).fetchone()["n"]
            return {
                "items": [
                    {
                        key: self.app.node_workspace.public_report(row[key])
                        if key == "guide_result"
                        else json.loads(row[key])
                        if key in ("citations", "trace")
                        else row[key]
                        for key in (
                            "id",
                            "role",
                            "content",
                            "stage_id",
                            "task_id",
                            "created_at",
                            "citations",
                            "trace",
                            "conversation_id",
                            "guide_result",
                        )
                    }
                    for row in reversed(rows)
                ],
                "total": total,
            }

    def run_workflow(self, owner, template, stage, content, tasks, **options):
        from app.contexts.conversations.application.assistant import run_workflow

        agent = stage.get("analysis") or template["agent"]
        if (agent.get("legal") or {}).get("enabled"):
            from app.contexts.legal_analysis.domain.analysis_models import LegalFact

            case_id = options.get("case_id")
            if case_id:
                with self.app.db.transaction() as conn:
                    self.app.own_case(conn, owner, case_id)
                    self.app.legal.sync_facts(conn, owner, case_id)
                    facts = self.app.legal.facts(conn, case_id)
            else:
                values = {}
                counts = {}
                for task in tasks:
                    for key in task.get("values", {}):
                        counts[key] = counts.get(key, 0) + 1
                for task in tasks:
                    labels = {field["key"]: field["label"] for field in task.get("fields", [])}
                    for key, value in task.get("values", {}).items():
                        fact_key = f"{task['id']}:{key}" if counts[key] > 1 else key
                        values[fact_key] = LegalFact(
                            key=fact_key,
                            label=labels.get(key, key),
                            value=value,
                            source="测试样例 · " + task["title"],
                        ).model_dump(mode="json")
                facts = list(values.values())
            checkpoint = options.get("on_checkpoint")

            def adapt(result):
                outputs = [step.get("output_data") or {} for step in result.get("trace", [])]
                hybrid = any(
                    output.get("retrieval_mode") == "ragflow"
                    or any(item.get("mode") == "ragflow" for item in output.get("coverage", []))
                    for output in outputs
                )
                return {
                    **result,
                    "checks": [],
                    "stage_id": stage["id"],
                    "retrieval_mode": "ragflow" if hybrid else "unavailable",
                }

            legal_options = {
                key: value
                for key, value in options.items()
                if key
                in (
                    "case_id",
                    "emit",
                    "check_cancelled",
                    "document_ids",
                    "stop_after_step_id",
                    "resume",
                    "context",
                    "prompt_cache_key",
                )
            }
            if checkpoint:
                legal_options["on_checkpoint"] = lambda step_id, result, state: checkpoint(
                    step_id, adapt(result), state
                )
            return adapt(
                self.app.legal.execute_engine(owner, template, stage, content, facts, **legal_options)
            )
        return run_workflow(self.app, owner, template, stage, content, tasks, **options)

    def prepare_message(self, owner, case_id, payload):
        # Retain historical request hashes for legacy clients.
        if not payload.get("conversation_id"):
            payload = {
                key: value
                for key, value in payload.items()
                if key not in ("conversation_id", "mode", "skills")
            }
        payload_hash = hashlib.sha256(dump(payload).encode()).hexdigest()
        with closing(self.app.db.connect()) as conn:
            case = self.app.case_detail(owner, case_id, conn)
            conversation_id = payload.get("conversation_id")
            conversation = None
            if conversation_id:
                conversation = self.app.node_workspace.own_conversation(
                    conn, owner, case_id, conversation_id, payload["stage_id"]
                )
            existing = conn.execute(
                "SELECT * FROM message_requests WHERE case_id=? AND client_id=?",
                (case_id, payload["client_message_id"]),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    fail(409, "idempotency_conflict", "同一消息编号不能提交不同内容")
                return {"response": {"messages": json.loads(existing["response"]), "case": case}}
            stage = next((s for s in case["stages"] if s["id"] == payload["stage_id"]), None)
            if stage is None:
                fail(422, "invalid_stage", "无效的对话节点")
            if conversation_id and stage["status"] == "locked":
                fail(409, "stage_locked", "节点解锁后才能继续对话")
            task_id = payload.get("task_id")
            if task_id and not any(t["id"] == task_id for t in stage["tasks"]):
                fail(422, "invalid_task", "所选任务不属于当前节点")
            selected = next((t for t in stage["tasks"] if t["id"] == task_id), stage["tasks"][0])
            recent = conn.execute(
                """SELECT m.role,m.content FROM messages m LEFT JOIN conversation_messages cm ON cm.message_id=m.id
                WHERE m.case_id=? AND m.stage_id=? AND (? IS NULL OR cm.conversation_id=?)
                ORDER BY m.created_at DESC,m.id DESC LIMIT 12""",
                (case_id, stage["id"], conversation_id, conversation_id),
            ).fetchall()
            history = [
                {"role": message["role"], "content": message["content"][:3000]}
                for message in reversed(recent)
            ]
            memory = (
                self.app.node_workspace.memory(
                    conn, owner, case, stage["id"], conversation_id, payload["content"]
                )
                if conversation_id
                else None
            )
            guide_phase = (
                self.app.node_workspace.guide_phase(conn, conversation_id)
                if conversation and conversation["kind"] == "guide"
                else None
            )
        return {
            "case": case,
            "stage": stage,
            "selected": selected,
            "history": history,
            "task_id": task_id,
            "payload_hash": payload_hash,
            "memory": memory,
            "guide_phase": guide_phase,
        }

    def post_message(self, owner, case_id, payload, *, emit=None, check_cancelled=None):
        # Authenticate before inspecting the in-flight registry. Both JSON and
        # streaming callers use the same claim and persisted idempotency key.
        prepared = self.app.prepare_message(owner, case_id, payload)
        if "response" in prepared:
            return prepared["response"]
        with self.app._chat_lock:
            if case_id in self.app._active_chats:
                fail(409, "message_in_progress", "此业务正在生成回复，请稍后重试。")
            if len(self.app._active_chats) >= 4:
                fail(503, "chat_busy", "当前对话较多，请稍后重试。")
            self.app._active_chats.add(case_id)
        try:
            # Reload after claiming: a prior request may have committed while
            # this request was reading its initial snapshot.
            prepared = self.app.prepare_message(owner, case_id, payload)
            if "response" in prepared:
                return prepared["response"]
            return self.app.finish_message(owner, case_id, payload, prepared, emit, check_cancelled)
        finally:
            with self.app._chat_lock:
                self.app._active_chats.discard(case_id)

    def finish_message(self, owner, case_id, payload, prepared, emit, check_cancelled):
        from app.contexts.conversations.application.assistant import conversation_prompt_cache_key

        case, stage = prepared["case"], prepared["stage"]
        task_id, payload_hash = prepared["task_id"], prepared["payload_hash"]
        prompt_cache_key = conversation_prompt_cache_key(owner, case_id, stage["id"])
        if check_cancelled:
            check_cancelled()
        agent = stage.get("analysis") or case["template"]["agent"]
        legal_enabled = bool((agent.get("legal") or {}).get("enabled"))
        conversation_id = payload.get("conversation_id")
        dynamic = bool(conversation_id and self.app.capabilities["assistant_mode"] == "live")
        skills = []
        collecting = False
        if conversation_id and not dynamic:
            from app.contexts.conversations.application.node_workspace import SKILLS, select_skills

            skills = select_skills(payload["content"], payload.get("skills", []), payload.get("mode", "auto"))
            legal_enabled = "legal" in skills
            phase = prepared.get("guide_phase")
            start_report = (
                bool(
                    re.search(
                        r"开始分析|进行分析|先分析|直接分析|生成.{0,6}报告|就这些|没有.{0,5}(?:材料|资料)|先继续",
                        payload["content"],
                    )
                )
                or payload.get("mode") == "legal"
            )
            collecting = bool(
                phase
                and not phase["has_report"]
                and phase["answers"] < 2
                and not start_report
                and "draft" not in skills
            )
            if phase and not collecting and (not phase["has_report"] or start_report):
                legal_enabled = True
                if "legal" not in skills:
                    skills.append("legal")
            if collecting:
                legal_enabled = False
                skills = ["guidance"]
            if emit:
                emit("status", {"label": "正在调用" + "、".join(SKILLS[key] for key in skills) + "…"})
        if dynamic:
            from app.contexts.conversations.application.agent_loop import run_agent

            legal_enabled = False
            result = run_agent(
                self.app, owner, case, stage, payload, prepared, emit=emit, check_cancelled=check_cancelled
            )
        elif legal_enabled:
            question = payload["content"]
            chat_context = None
            if conversation_id and len(question) < 12000:
                memory = prepared["memory"]
                shared = {
                    "recent_messages": [
                        {"role": item["role"], "content": item["content"][:500]}
                        for item in prepared["history"][-6:]
                    ],
                    "node_discussions": [
                        {
                            "message_id": item["message_id"],
                            "question": item["question"][:300],
                            "summary": item["summary"][:400],
                            "status": item["status"],
                        }
                        for item in memory["other_conversations"][:3]
                    ],
                    "shared_facts": [
                        {**fact, "value": str(fact["value"])[:200]} for fact in memory["shared_facts"][:15]
                    ],
                    "node_progress": memory["node_progress"],
                }
                context_text = dump(shared)
                if len(question) + len(context_text) < 19000:
                    chat_context = shared
            with closing(self.app.db.connect()) as conn:
                retry = conn.execute(
                    """SELECT a.payload FROM legal_requests r JOIN legal_analyses a ON a.id=r.analysis_id
                    WHERE r.owner_id=? AND r.case_id=? AND r.client_id=?""",
                    (owner, case_id, "chat:" + payload["client_message_id"]),
                ).fetchone()
                prior = conn.execute(
                    "SELECT payload FROM legal_analyses WHERE owner_id=? AND case_id=? AND stage_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                    (owner, case_id, stage["id"]),
                ).fetchone()
            if retry:
                stored = json.loads(retry["payload"])
                question = stored["question"]
                chat_context = stored.get("_chat_context")
            elif prior and not conversation_id:
                prior = json.loads(prior["payload"])
                if prior["status"] == "needs_input":
                    question = prior["question"] + "\n\n用户补充说明：" + question
            if len(question) > 20000:
                fail(422, "analysis_context_limit", "当前问题与补充说明过长，请在本节点分析中整理事实后继续")

            def legal_emit(event, data):
                if emit and event in ("status", "start", "delta", "step"):
                    emit(event, data)

            analysis = self.app.legal.analyze(
                owner,
                case_id,
                {
                    "stage_id": stage["id"],
                    "question": question,
                    "client_request_id": "chat:" + payload["client_message_id"],
                    "_chat_content": payload["content"],
                    **({"_chat_context": chat_context} if chat_context is not None else {}),
                },
                emit=legal_emit if emit else None,
                check_cancelled=check_cancelled,
                prompt_cache_key=prompt_cache_key,
            )
            result = analysis["result"]
            if emit:
                emit("delta", {"text": result["output"]})
        else:
            workflow_stage = stage
            if conversation_id:
                workflow_stage = copy.deepcopy(stage)
                workflow_stage["analysis"] = copy.deepcopy(agent)
                workflow_stage["analysis"]["legal"] = {
                    **(workflow_stage["analysis"].get("legal") or {}),
                    "enabled": False,
                }
                if "draft" in skills:
                    for step in workflow_stage["analysis"]["steps"]:
                        if step["type"] == "guide":
                            step["instructions"] = (
                                step.get("instructions", "")
                                + "\n用户要求新建文稿，请直接输出完整 Markdown 文稿正文，缺少的事实标为待补充，不要声称文稿已审批或手续已办理。"
                            )
            if collecting:
                result = self.app.node_workspace.intake(
                    agent,
                    payload["content"],
                    {"node_memory": prepared["memory"], "recent_messages": prepared["history"]},
                    emit,
                    check_cancelled,
                    prompt_cache_key=prompt_cache_key,
                )
            else:
                result = self.app.run_workflow(
                    owner,
                    case["template"],
                    workflow_stage,
                    payload["content"],
                    [prepared["selected"]],
                    case_id=case_id,
                    prompt_cache_key=prompt_cache_key,
                    **({"emit": emit, "check_cancelled": check_cancelled} if emit else {}),
                    context={
                        "facts": prepared["memory"]["shared_facts"] if conversation_id else case["facts"],
                        "guide_intake": prepared.get("guide_phase")
                        if conversation_id and collecting
                        else None,
                        "mode": next(
                            (mode for mode in case["template"]["modes"] if mode["id"] == case["mode_id"]),
                            None,
                        ),
                        "recent_messages": prepared["history"],
                        "node_analysis": self.app.studio.node_context(owner, case, stage["id"]),
                        **(
                            {"node_memory": prepared["memory"], "selected_skills": skills}
                            if conversation_id
                            else {}
                        ),
                    },
                )
        if (
            conversation_id
            and "draft" in skills
            and legal_enabled
            and self.app.capabilities["assistant_mode"] == "live"
        ):
            from app.contexts.conversations.application.assistant import generate_guidance
            from app.contexts.conversations.application.tool_runtime import ToolRun

            tools = ToolRun(emit, check_cancelled)
            draft_trace = copy.deepcopy(result["trace"][-1])
            draft_trace.update(status="running", detail="正在根据分析结果起草文稿。")
            tools.publish(draft_trace)

            if emit:
                emit("start", {"step_id": "draft"})
                emit("status", {"label": "正在起草 Markdown 文稿…"})
            with tools.step(draft_trace):
                try:
                    content = generate_guidance(
                        agent,
                        {
                            "instructions": "用户明确要求新建文稿。请直接输出完整 Markdown 文稿正文；以法律核对结果为约束，未知内容标为待补充。"
                        },
                        payload["content"],
                        {"node_memory": prepared["memory"], "recent_messages": prepared["history"]},
                        result["citations"],
                        [],
                        result["output"],
                        on_delta=(lambda text: emit("delta", {"text": text})) if emit else None,
                        check_cancelled=check_cancelled,
                        max_tokens=4000,
                        prompt_cache_key=prompt_cache_key,
                    )
                except Exception:
                    draft_trace.update(status="failed", detail="文稿生成未完成，请重试。")
                    tools.publish(draft_trace)
                    raise
            draft_trace.update(status="completed", detail="已根据分析结果生成文稿正文。")
            tools.publish(draft_trace)
            result["trace"][-1] = draft_trace
            result = {**result, "output": content}
        if any(step["status"] == "failed" for step in result["trace"]):
            fail(502, "assistant_failed", "回复未能完成，已收到的内容仅供参考。请重试。")
        output, trace, citations = result["output"], result["trace"], result["citations"]
        with self.app.db.transaction() as conn:
            if check_cancelled:
                check_cancelled()
            current = self.app.case_detail(owner, case_id, conn)
            if dynamic and current["revision"] != case["revision"]:
                fail(409, "agent_context_changed", "业务资料已变化，本轮文稿和记忆未保存，请重新发送")
            if legal_enabled:
                legal_record = self.app.legal.own(conn, owner, case_id, analysis["id"])
                if legal_record.get("_snapshot") != self.app.legal.snapshot(conn, owner, current):
                    fail(409, "analysis_context_changed", "事实或资料已变化，请重新分析后再使用本次答复")
            existing = conn.execute(
                "SELECT * FROM message_requests WHERE case_id=? AND client_id=?",
                (case_id, payload["client_message_id"]),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != payload_hash:
                    fail(409, "idempotency_conflict", "同一消息编号不能提交不同内容")
                return {"messages": json.loads(existing["response"]), "case": current}
            if current["revision"] != case["revision"]:
                output += "\n\n提示：生成答复期间业务资料发生了更新，请以当前任务卡片为准并重新核验。"
            messages = [
                self.app.insert_message(
                    conn, case_id, stage["id"], task_id, "user", payload["content"], [], []
                ),
                self.app.insert_message(
                    conn, case_id, stage["id"], task_id, "assistant", output, citations, trace
                ),
            ]
            if dynamic:
                from app.contexts.conversations.application.agent_loop import commit_agent_result

                notice = commit_agent_result(self.app, conn, owner, current, stage["id"], result, messages[0])
                if notice:
                    messages[1]["content"] += "\n\n" + notice
                    conn.execute(
                        "UPDATE messages SET content=? WHERE id=?",
                        (messages[1]["content"], messages[1]["id"]),
                    )
            if conversation_id:
                if prepared.get("guide_phase") is not None and (
                    legal_enabled or (dynamic and result["_analyses"])
                ):
                    if dynamic:
                        analysis = self.app.legal.own(conn, owner, case_id, result["_analyses"][-1]["id"])
                    guide_id = self.app.node_workspace.queue_guide(
                        conn, current, stage["id"], "chat:" + payload["client_message_id"], "引导会话分析报告"
                    )
                    snapshot = {
                        **(analysis["result"] if dynamic else result),
                        "status": "completed",
                        "legal_analysis_id": analysis["id"],
                        "legal_revision": analysis["revision"],
                        "case_revision": current["revision"],
                        "created_at": now(),
                    }
                    self.app.node_workspace.save_guide(
                        conn,
                        current,
                        stage["id"],
                        snapshot,
                        "chat:" + payload["client_message_id"],
                        guide_id,
                        messages[1]["id"],
                    )
                    messages[1]["guide_result"] = {**snapshot, "trace": []}
                if "draft" in skills and not dynamic:
                    title_match = re.search(r"[《「“]([^》」”\n]{1,100})[》」”]", payload["content"])
                    title = title_match[1] if title_match else prepared["selected"]["title"] + " · 文稿"
                    artifact = self.app.create_artifact(
                        owner,
                        case_id,
                        {"task_id": prepared["selected"]["id"], "title": title},
                        content=output if self.app.capabilities["assistant_mode"] == "live" else None,
                        conn=conn,
                    )
                    messages[1]["content"] += (
                        f"\n\n已新建文稿「{artifact['title']}」，可在右侧“文稿”中预览和编辑。"
                    )
                    conn.execute(
                        "UPDATE messages SET content=? WHERE id=?",
                        (messages[1]["content"], messages[1]["id"]),
                    )
                self.app.node_workspace.remember_turn(conn, current, conversation_id, messages)
            conn.execute(
                "INSERT INTO message_requests VALUES (?,?,?,?)",
                (case_id, payload["client_message_id"], payload_hash, dump(messages)),
            )
            self.app.touch(conn, case_id)
            return {"messages": messages, "case": self.app.case_detail(owner, case_id, conn)}
