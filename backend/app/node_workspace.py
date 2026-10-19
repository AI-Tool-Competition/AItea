"""Node folders: isolated chats, source-linked memory and append-only guidance."""

import json
import re
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .db import dump, now, uid
from .streaming import StreamCancelled
from .tool_runtime import ToolRun


SKILLS = {
    "guidance": "办理指导",
    "materials": "资料核对",
    "legal": "法律分析",
    "draft": "文稿起草",
    "handoff": "节点交接",
}


def select_skills(content, requested=(), mode="auto"):
    """High-recall routing; selected operations must still use the real runners."""
    selected = set(requested)
    if mode == "legal" or re.search(
        r"法规|法律|合规|资质|许可|海关|报关|税|合同|期限|责任|清真|准入|检疫|依据|审批|认证|能否|能不能|可不可以|是否需要|可以.{0,6}[卖售进出]|BPOM|Halal|法条",
        content,
        re.I,
    ):
        selected.add("legal")
    if re.search(r"资料|材料|文件|证据|缺.{0,3}[项什]|清单|核对|核验|检查", content):
        selected.add("materials")
    if re.search(
        r"(?:新建|创建|起草|撰写|编写|生成|写|拟).{0,18}(?:文稿|报告|合同|邮件|说明|方案|函|清单|报价|证明|计划)",
        content,
    ):
        selected.add("draft")
    if re.search(r"下一.{0,3}节点|前.{0,3}节点|交接|后续|进度|前面|之前", content):
        selected.add("handoff")
    if not selected:
        selected.add("guidance")
    return [key for key in SKILLS if key in selected]


class NodeWorkspace:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self._slots = threading.BoundedSemaphore(2)
        self._lock = threading.Lock()
        self._active = set()
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=17").fetchone():
                self._migrate_guide_messages(conn)
                return
            conn.execute("""CREATE TABLE stage_conversations(
                id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                stage_id TEXT NOT NULL,title TEXT NOT NULL,client_id TEXT NOT NULL,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(case_id,client_id))""")
            conn.execute(
                "CREATE INDEX conversations_node ON stage_conversations(case_id,stage_id,created_at,id)"
            )
            conn.execute("""CREATE TABLE conversation_messages(
                message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES stage_conversations(id) ON DELETE CASCADE)""")
            conn.execute(
                "CREATE INDEX conversation_message_scope ON conversation_messages(conversation_id,message_id)"
            )
            conn.execute("""CREATE TABLE node_memories(
                message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
                conversation_id TEXT NOT NULL REFERENCES stage_conversations(id) ON DELETE CASCADE,
                case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,stage_id TEXT NOT NULL,
                question TEXT NOT NULL,summary TEXT NOT NULL,created_at TEXT NOT NULL)""")
            conn.execute(
                "CREATE INDEX memories_node ON node_memories(case_id,stage_id,created_at,message_id)"
            )
            conn.execute("""CREATE TABLE stage_guide_runs(
                id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                stage_id TEXT NOT NULL,event_key TEXT NOT NULL,title TEXT NOT NULL,status TEXT NOT NULL,
                case_revision INTEGER NOT NULL,created_at TEXT NOT NULL,completed_at TEXT,
                result TEXT,trace TEXT NOT NULL DEFAULT '[]',error TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,lease_until TEXT NOT NULL DEFAULT '',
                UNIQUE(case_id,stage_id,event_key))""")
            conn.execute("CREATE INDEX guides_node ON stage_guide_runs(case_id,stage_id,created_at,id)")
            # Existing chats become real folders; the original message rows and
            # idempotency responses are retained byte-for-byte.
            for row in conn.execute(
                "SELECT case_id,stage_id,MIN(created_at) first_at,MAX(created_at) last_at FROM messages GROUP BY case_id,stage_id"
            ).fetchall():
                conversation_id = uid()
                conn.execute(
                    "INSERT INTO stage_conversations VALUES(?,?,?,?,?,?,?)",
                    (
                        conversation_id,
                        row["case_id"],
                        row["stage_id"],
                        "原有会话",
                        "legacy:" + row["stage_id"],
                        row["first_at"],
                        row["last_at"],
                    ),
                )
                conn.execute(
                    "INSERT INTO conversation_messages SELECT id,? FROM messages WHERE case_id=? AND stage_id=?",
                    (conversation_id, row["case_id"], row["stage_id"]),
                )
            conn.execute("INSERT INTO schema_migrations VALUES(17,?)", (now(),))
            self._migrate_guide_messages(conn)

    @staticmethod
    def _migrate_guide_messages(conn):
        if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=18").fetchone():
            conn.execute("""CREATE TABLE guide_messages(
                guide_id TEXT PRIMARY KEY REFERENCES stage_guide_runs(id) ON DELETE CASCADE,
                message_id TEXT NOT NULL UNIQUE REFERENCES messages(id) ON DELETE CASCADE)""")
            conn.execute("INSERT INTO schema_migrations VALUES(18,?)", (now(),))

    @staticmethod
    def public_report(value):
        return {**json.loads(value), "trace": []} if value else None

    @staticmethod
    def conversation_dict(row):
        return dict(row) | {"kind": "guide" if row["client_id"] == "guide:" + row["stage_id"] else "chat"}

    def stage(self, case, stage_id):
        from .service import fail

        stage = next((item for item in case["stages"] if item["id"] == stage_id), None)
        if stage is None:
            fail(404, "not_found", "未找到办理节点")
        return stage

    def own_conversation(self, conn, owner, case_id, conversation_id, stage_id=None):
        from .service import fail

        self.service.own_case(conn, owner, case_id)
        row = conn.execute(
            "SELECT * FROM stage_conversations WHERE id=? AND case_id=?", (conversation_id, case_id)
        ).fetchone()
        if row is None or (stage_id and row["stage_id"] != stage_id):
            fail(404, "not_found", "未找到本节点的会话")
        return self.conversation_dict(row)

    def conversations(self, owner, case_id, limit=100, offset=0, stage_id=None):
        with closing(self.db.connect()) as conn:
            self.service.own_case(conn, owner, case_id)
            items = conn.execute(
                "SELECT * FROM stage_conversations WHERE case_id=? AND (? IS NULL OR stage_id=?) ORDER BY created_at,id LIMIT ? OFFSET ?",
                (case_id, stage_id, stage_id, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM stage_conversations WHERE case_id=? AND (? IS NULL OR stage_id=?)",
                (case_id, stage_id, stage_id),
            ).fetchone()[0]
            return {"items": [self.conversation_dict(row) for row in items], "total": total}

    def create_conversation(self, owner, case_id, stage_id, payload, *, guide=False):
        from .service import fail

        with self.db.transaction() as conn:
            if not guide and payload["client_request_id"].startswith(("guide:", "legacy:")):
                fail(422, "reserved_request_id", "此会话编号由系统保留")
            case = self.service.case_detail(owner, case_id, conn)
            stage = self.stage(case, stage_id)
            if stage["status"] == "locked":
                fail(409, "stage_locked", "节点解锁后才能新建会话")
            prior = conn.execute(
                "SELECT * FROM stage_conversations WHERE case_id=? AND client_id=?",
                (case_id, payload["client_request_id"]),
            ).fetchone()
            if prior:
                if prior["stage_id"] != stage_id:
                    fail(409, "idempotency_conflict", "会话请求编号已用于其他节点")
                return self.conversation_dict(prior)
            count = conn.execute(
                "SELECT COUNT(*) FROM stage_conversations WHERE case_id=? AND stage_id=?", (case_id, stage_id)
            ).fetchone()[0]
            if count >= 100 and not guide:
                fail(422, "conversation_limit", "本节点已达到 100 个会话的上限")
            conversation_id, at = uid(), now()
            conn.execute(
                "INSERT INTO stage_conversations VALUES(?,?,?,?,?,?,?)",
                (
                    conversation_id,
                    case_id,
                    stage_id,
                    payload.get("title") or f"新会话 {count + 1}",
                    payload["client_request_id"],
                    at,
                    at,
                ),
            )
            return self.own_conversation(conn, owner, case_id, conversation_id, stage_id)

    def guide_conversation(self, owner, case_id, stage_id):
        conversation = self.create_conversation(
            owner,
            case_id,
            stage_id,
            {"client_request_id": "guide:" + stage_id, "title": "节点引导"},
            guide=True,
        )
        with self.db.transaction() as conn:
            case = self.service.case_detail(owner, case_id, conn)
            stage = self.stage(case, stage_id)
            if not conn.execute(
                "SELECT 1 FROM conversation_messages WHERE conversation_id=? LIMIT 1", (conversation["id"],)
            ).fetchone():
                content = (
                    f"我们一起开始 **{stage['name']}**。先把情况说明白，再整理分析报告；之后也可以随时继续补充和讨论。\n\n"
                    "1. 这个节点你目前已经办理到哪一步？最希望先解决什么问题？\n"
                    "2. 实际参与的企业、产品和交易安排有哪些已确定，哪些还不清楚？\n"
                    "3. 如有相关证照、产品说明、合同或办理凭证，可以先上传。**暂时没有材料也可以直接说明，不会阻止继续分析。**\n\n"
                    "你可以分几次回答；准备好后告诉我“开始分析”，我会结合共享进度和已有资料分析，并把报告保存在这里。"
                )
                message = self.service.insert_message(
                    conn, case_id, stage_id, None, "assistant", content, [], []
                )
                conn.execute(
                    "INSERT INTO conversation_messages VALUES(?,?)", (message["id"], conversation["id"])
                )
        return conversation

    def intake(self, agent, question, context, emit=None, check_cancelled=None, prompt_cache_key=None):
        from .assistant import generate_guidance

        trace = {
            "id": "intake",
            "label": "梳理当前情况",
            "type": "guide",
            "status": "running",
            "detail": "正在整理已提供的信息。",
        }
        tools = ToolRun(emit, check_cancelled)
        tools.publish(trace)
        with tools.step(trace):
            try:
                if self.service.capabilities["assistant_mode"] == "live":
                    output = generate_guidance(
                        agent,
                        {
                            "instructions": "这是节点引导的信息收集阶段。简短确认用户已说明的内容，再追问最关键的1至2项缺口；邀请上传相关材料，同时明确没有材料也可继续。此轮先不生成最终报告，不重复询问已有事实。"
                        },
                        question,
                        context,
                        [],
                        [],
                        "",
                        on_delta=(lambda text: emit("delta", {"text": text})) if emit else None,
                        check_cancelled=check_cancelled,
                        prompt_cache_key=prompt_cache_key,
                    )
                else:
                    output = (
                        "已记下你的补充。当前为演示模式，以下按节点准备要求继续收集信息。\n\n"
                        "请再说明目前最着急解决的事项，以及参与企业、产品或交易安排中尚未确定的部分。"
                        "如果有证照、产品说明或合同，可以上传；**没有材料也可以继续**。\n\n"
                        "准备好后点击“开始分析”，或直接告诉我“开始分析”，我会整理现有信息与待核实事项。"
                    )
                    if emit:
                        emit("delta", {"text": output})
                trace.update(status="completed", detail="已整理当前情况与待补充事项。")
            except StreamCancelled:
                trace.update(status="cancelled", detail="回复已停止。")
                raise
            except Exception:
                trace.update(status="failed", detail="回复未完成，请重试。")
                tools.publish(trace)
                raise
        tools.publish(trace)
        return {
            "output": output,
            "trace": [trace],
            "citations": [],
            "assistant_mode": self.service.capabilities["assistant_mode"],
        }

    @staticmethod
    def guide_phase(conn, conversation_id):
        answers = conn.execute(
            "SELECT COUNT(*) FROM messages m JOIN conversation_messages cm ON cm.message_id=m.id WHERE cm.conversation_id=? AND m.role='user'",
            (conversation_id,),
        ).fetchone()[0]
        report = conn.execute(
            """SELECT 1 FROM guide_messages gm JOIN conversation_messages cm ON cm.message_id=gm.message_id
            WHERE cm.conversation_id=? LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        return {
            "answers": answers,
            "has_report": bool(report),
        }

    def memory(self, conn, owner, case, stage_id, conversation_id, question):
        self.own_conversation(conn, owner, case["id"], conversation_id, stage_id)
        stages = case["stages"]
        index = next(i for i, stage in enumerate(stages) if stage["id"] == stage_id)
        rows = conn.execute(
            """SELECT * FROM node_memories WHERE case_id=? AND stage_id=?
            AND conversation_id!=? ORDER BY created_at DESC,message_id DESC LIMIT 40""",
            (case["id"], stage_id, conversation_id),
        ).fetchall()
        tokens = set(re.findall(r"[a-z0-9_]{2,}|[\u4e00-\u9fff]{2}", question.lower()))
        ranked = sorted(
            enumerate(rows),
            key=lambda pair: (
                -sum(token in (pair[1]["question"] + pair[1]["summary"]).lower() for token in tokens),
                pair[0],
            ),
        )
        current_sources = {"task:" + task["id"] for task in stages[index]["tasks"]}
        return {
            "scope": {"case_id": case["id"], "stage_id": stage_id, "conversation_id": conversation_id},
            "shared_facts": [
                {**fact, "value": str(fact["value"])[:1000]}
                for fact in case["facts"]
                if fact.get("confirmed")
                or not fact.get("source", "").startswith("task:")
                or fact["source"] in current_sources
            ][:40],
            "node_progress": [
                {
                    "id": task["id"],
                    "title": task["title"],
                    "status": task["status"],
                    "needs_review": task["needs_review"],
                }
                for task in stages[index]["tasks"]
            ],
            "other_conversations": [
                {
                    "message_id": row["message_id"],
                    "conversation_id": row["conversation_id"],
                    "question": row["question"],
                    "summary": row["summary"],
                    "status": "unverified_discussion",
                    "created_at": row["created_at"],
                }
                for _, row in ranked[:6]
            ],
            "upstream_handoffs": [
                {
                    "stage_id": stage["id"],
                    "stage": stage["name"],
                    "status": stage["status"],
                    "tasks": [
                        {
                            "id": task["id"],
                            "title": task["title"],
                            "status": task["status"],
                            "needs_review": task["needs_review"],
                        }
                        for task in stage["tasks"]
                        if task["status"] == "completed" or task["needs_review"]
                    ],
                }
                for stage in stages[:index][-6:]
            ],
            "documents": [
                {"id": doc["id"], "title": doc["title"], "version": doc["version"], "status": doc["status"]}
                for doc in case["documents"][:30]
            ],
            "notice": "其他会话的摘要是待核实的讨论记录，不是法规、证据或已确认事实；任务进度以当前服务端状态为准。",
        }

    def remember_turn(self, conn, case, conversation_id, messages):
        conversation = conn.execute(
            "SELECT * FROM stage_conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        for message in messages:
            conn.execute("INSERT INTO conversation_messages VALUES(?,?)", (message["id"], conversation_id))
            message["conversation_id"] = conversation_id
        user_message, answer = messages
        conn.execute(
            "INSERT INTO node_memories VALUES(?,?,?,?,?,?,?)",
            (
                answer["id"],
                conversation_id,
                case["id"],
                conversation["stage_id"],
                user_message["content"][:700],
                answer["content"][:1200],
                answer["created_at"],
            ),
        )
        title = conversation["title"]
        if title.startswith("新会话 "):
            title = re.sub(r"\s+", " ", user_message["content"]).strip()[:48]
        conn.execute(
            "UPDATE stage_conversations SET title=?,updated_at=? WHERE id=?", (title, now(), conversation_id)
        )

    def queue_guide(self, conn, case, stage_id, event_key, title):
        run_id = uid()
        conn.execute(
            """INSERT OR IGNORE INTO stage_guide_runs
            (id,case_id,stage_id,event_key,title,status,case_revision,created_at)
            VALUES(?,?,?,?,?,'queued',?,?)""",
            (run_id, case["id"], stage_id, event_key, title, case["revision"], now()),
        )
        return conn.execute(
            "SELECT id FROM stage_guide_runs WHERE case_id=? AND stage_id=? AND event_key=?",
            (case["id"], stage_id, event_key),
        ).fetchone()[0]

    def save_guide(self, conn, case, stage_id, result, fingerprint, run_id=None, message_id=None):
        if run_id is None:
            run_id = self.queue_guide(
                conn,
                case,
                stage_id,
                "input:" + fingerprint,
                "首次节点引导"
                if not conn.execute(
                    "SELECT 1 FROM stage_guide_runs WHERE case_id=? AND stage_id=? LIMIT 1",
                    (case["id"], stage_id),
                ).fetchone()
                else "根据最新资料更新",
            )
        # A completed snapshot is immutable, even when its linked editable report
        # receives later revisions or an identical request is replayed.
        conn.execute(
            """UPDATE stage_guide_runs SET result=?,trace=?,status=?,completed_at=?,case_revision=?,error=''
            WHERE id=? AND case_id=? AND stage_id=? AND status!='completed'""",
            (
                dump(result),
                dump(result.get("trace", [])),
                "failed" if result.get("status") == "failed" else "completed",
                now(),
                case["revision"],
                run_id,
                case["id"],
                stage_id,
            ),
        )
        saved = conn.execute("SELECT * FROM stage_guide_runs WHERE id=?", (run_id,)).fetchone()
        conversation = conn.execute(
            "SELECT * FROM stage_conversations WHERE case_id=? AND client_id=?",
            (case["id"], "guide:" + stage_id),
        ).fetchone()
        if (
            saved["status"] == "completed"
            and conversation
            and not conn.execute("SELECT 1 FROM guide_messages WHERE guide_id=?", (run_id,)).fetchone()
        ):
            if message_id is None:
                snapshot = json.loads(saved["result"])
                message = self.service.insert_message(
                    conn,
                    case["id"],
                    stage_id,
                    None,
                    "assistant",
                    "## " + saved["title"] + "\n\n" + snapshot.get("output", ""),
                    snapshot.get("citations", []),
                    snapshot.get("trace", []),
                )
                message_id = message["id"]
                conn.execute(
                    "INSERT INTO conversation_messages VALUES(?,?)", (message_id, conversation["id"])
                )
                conn.execute(
                    "UPDATE stage_conversations SET updated_at=? WHERE id=?", (now(), conversation["id"])
                )
            conn.execute("INSERT INTO guide_messages VALUES(?,?)", (run_id, message_id))

    def guides(self, owner, case_id, stage_id, limit=20, offset=0):
        with closing(self.db.connect()) as conn:
            case = self.service.case_detail(owner, case_id, conn)
            self.stage(case, stage_id)
            rows = conn.execute(
                "SELECT * FROM stage_guide_runs WHERE case_id=? AND stage_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (case_id, stage_id, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM stage_guide_runs WHERE case_id=? AND stage_id=?", (case_id, stage_id)
            ).fetchone()[0]
            return {
                "items": [
                    {
                        key: json.loads(row[key]) if key in ("result", "trace") and row[key] else row[key]
                        for key in (
                            "id",
                            "stage_id",
                            "title",
                            "status",
                            "case_revision",
                            "created_at",
                            "completed_at",
                            "result",
                            "trace",
                            "error",
                        )
                    }
                    for row in rows
                ],
                "total": total,
            }

    def retry_guide(self, owner, case_id, stage_id, guide_id, client_id):
        from .service import fail

        with self.db.transaction() as conn:
            case = self.service.case_detail(owner, case_id, conn)
            row = conn.execute(
                "SELECT * FROM stage_guide_runs WHERE id=? AND case_id=? AND stage_id=?",
                (guide_id, case_id, stage_id),
            ).fetchone()
            if row is None:
                fail(404, "not_found", "未找到节点分析记录")
            return {
                "id": self.queue_guide(
                    conn, case, stage_id, "retry:" + client_id, "重新分析 · " + row["title"]
                )
            }

    def process_pending(self, owner, case_id):
        from .models import StageAnalysisInput
        from .service import AppError

        with self._lock:
            if case_id in self._active or not self._slots.acquire(blocking=False):
                return
            self._active.add(case_id)
        try:
            for _ in range(4):
                with self.db.transaction() as conn:
                    case = self.service.case_detail(owner, case_id, conn)
                    row = conn.execute(
                        "SELECT * FROM stage_guide_runs WHERE case_id=? AND (status='queued' OR (status='running' AND lease_until<?)) ORDER BY created_at,id LIMIT 1",
                        (case_id, now()),
                    ).fetchone()
                    if row is None:
                        return
                    row = dict(row)
                    lease = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
                    conn.execute(
                        "UPDATE stage_guide_runs SET status='running',attempts=attempts+1,lease_until=? WHERE id=?",
                        (lease, row["id"]),
                    )

                def progress(event, data):
                    if event != "step":
                        return
                    with self.db.transaction() as conn:
                        saved = conn.execute(
                            "SELECT trace FROM stage_guide_runs WHERE id=?", (row["id"],)
                        ).fetchone()
                        trace = [step for step in json.loads(saved[0]) if step["id"] != data["id"]]
                        conn.execute(
                            "UPDATE stage_guide_runs SET trace=? WHERE id=? AND status='running'",
                            (dump([*trace, data]), row["id"]),
                        )

                try:
                    self.service.studio.analyze(
                        owner,
                        case_id,
                        row["stage_id"],
                        StageAnalysisInput(expected_revision=case["revision"], retry=True),
                        emit=progress,
                        guide_id=row["id"],
                    )
                except Exception as error:
                    retryable = (
                        isinstance(error, AppError)
                        and error.code
                        in (
                            "analysis_in_progress",
                            "analysis_busy",
                            "analysis_stale",
                            "analysis_context_changed",
                            "stale_revision",
                            "index_preparing",
                        )
                        and row["attempts"] < 3
                    )
                    with self.db.transaction() as conn:
                        conn.execute(
                            "UPDATE stage_guide_runs SET status=?,error=? WHERE id=? AND status!='completed'",
                            (
                                "queued" if retryable else "failed",
                                error.message if isinstance(error, AppError) else "自动分析未完成，请重试。",
                                row["id"],
                            ),
                        )
                    if retryable:
                        return
        finally:
            with self._lock:
                self._active.discard(case_id)
                self._slots.release()
