"""Durable claims and tool receipts. Ambiguous paid requests are never replayed."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.shared.domain.errors import fail
from app.shared.domain.values import dump, now, uid


class AgentJournal:
    def __init__(self, service, owner, case, payload, payload_hash):
        self.service, self.owner, self.case = service, owner, case
        self.id, self.cached = uid(), None
        with service.db.transaction() as conn:
            service.own_case(conn, owner, case["id"])
            old = conn.execute(
                "SELECT * FROM agent_runs WHERE case_id=? AND client_id=?",
                (case["id"], payload["client_message_id"]),
            ).fetchone()
            if old:
                if old["payload_hash"] != payload_hash:
                    fail(409, "idempotency_conflict", "同一消息编号不能提交不同内容")
                self.id = old["id"]
                if old["status"] == "completed" and old["case_revision"] == case["revision"]:
                    self.cached = json.loads(old["result"])
                    return
                fail(
                    409,
                    "agent_request_not_replayable",
                    "本次请求正在处理、已中断或资料已变化，请查看已有记录后重新发送；不会自动重复外部请求",
                )
            # Lease expiry releases ownership, not permission to repeat an unknown request.
            conn.execute(
                "UPDATE agent_runs SET status='interrupted',updated_at=? WHERE case_id=? AND status='running' AND lease_until<?",
                (now(), case["id"], now()),
            )
            if conn.execute(
                "SELECT 1 FROM agent_runs WHERE case_id=? AND status='running'", (case["id"],)
            ).fetchone():
                fail(409, "message_in_progress", "此业务正在生成回复，请稍后重试")
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM agent_runs WHERE status='running' AND lease_until>?", (now(),)
                ).fetchone()[0]
                >= 4
            ):
                fail(503, "chat_busy", "当前对话较多，请稍后重试")
            expires = (datetime.now(timezone.utc) + timedelta(seconds=960)).isoformat()
            conn.execute(
                "INSERT INTO agent_runs(id,owner_id,case_id,stage_id,conversation_id,client_id,payload_hash,case_revision,status,lease_until,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,'running',?,?,?)",
                (
                    self.id,
                    owner,
                    case["id"],
                    payload["stage_id"],
                    payload.get("conversation_id"),
                    payload["client_message_id"],
                    payload_hash,
                    case["revision"],
                    expires,
                    now(),
                    now(),
                ),
            )

    def guard(self, conn):
        self.service.own_case(conn, self.owner, self.case["id"])
        row = conn.execute(
            "SELECT status,lease_until FROM agent_runs WHERE id=? AND owner_id=?", (self.id, self.owner)
        ).fetchone()
        if not row or row["status"] != "running" or row["lease_until"] < now():
            fail(409, "agent_lease_lost", "本次执行已停止，请重新发送")

    def start_tool(self, call_id, name, inputs):
        fingerprint = hashlib.sha256(dump(inputs).encode()).hexdigest()
        with self.service.db.transaction() as conn:
            self.guard(conn)
            old = conn.execute(
                "SELECT * FROM agent_tool_receipts WHERE run_id=? AND call_id=?", (self.id, call_id)
            ).fetchone()
            if old:
                if old["name"] != name or old["input_hash"] != fingerprint:
                    fail(409, "tool_id_conflict", "重复工具编号的参数不一致")
                if old["status"] == "completed":
                    return json.loads(old["result"])
                fail(409, "tool_result_unknown", "该工具的结果尚未确认，不能自动重试")
            conn.execute(
                "INSERT INTO agent_tool_receipts VALUES(?,?,?,?,?,NULL,?)",
                (self.id, call_id, name, fingerprint, "running", now()),
            )
        return None

    def finish_tool(self, call_id, result):
        with self.service.db.transaction() as conn:
            self.guard(conn)
            conn.execute(
                "UPDATE agent_tool_receipts SET status=?,result=?,updated_at=? WHERE run_id=? AND call_id=?",
                ("failed" if result.get("error") else "completed", dump(result), now(), self.id, call_id),
            )

    def checkpoint(self, trace):
        with self.service.db.transaction() as conn:
            self.guard(conn)
            conn.execute(
                "UPDATE agent_runs SET trace=?,updated_at=? WHERE id=?", (dump(trace), now(), self.id)
            )

    def finish(self, result=None):
        with self.service.db.transaction() as conn:
            self.guard(conn)
            conn.execute(
                "UPDATE agent_runs SET status=?,result=?,updated_at=? WHERE id=?",
                (
                    "completed" if result is not None else "interrupted",
                    dump(result) if result is not None else None,
                    now(),
                    self.id,
                ),
            )
