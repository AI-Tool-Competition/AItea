"""Saved editor drafts and isolated stage workflow previews."""

import hashlib
import json
import logging
import os
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .db import dump, now, uid

logger = logging.getLogger(__name__)


class AdminStudio:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        # Upgrade both pre-existing databases and empty databases under one write lock.
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=3").fetchone():
                conn.execute("""CREATE TABLE template_drafts(
                    owner_id TEXT NOT NULL REFERENCES users(id), id TEXT NOT NULL,
                    payload TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner_id,id))""")
                conn.execute("""CREATE TABLE stage_analyses(
                    case_id TEXT NOT NULL REFERENCES cases(id), stage_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, expires_at TEXT NOT NULL, payload TEXT,
                    PRIMARY KEY(case_id,stage_id,fingerprint))""")
                conn.execute("""CREATE INDEX documents_parent
                    ON documents(owner_id,case_id,parent_id,created_at,id)""")
                conn.execute("""CREATE INDEX documents_role
                    ON documents(owner_id,case_id,role,created_at,id)""")
                conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (3, now()))

    def drafts(self, owner, limit=50, offset=0):
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """SELECT id,revision,updated_at,
                CASE WHEN json_type(payload,'$.title')='text'
                    THEN substr(json_extract(payload,'$.title'),1,200) ELSE NULL END title
                FROM template_drafts WHERE owner_id=? ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?""",
                (owner, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM template_drafts WHERE owner_id=?", (owner,)
            ).fetchone()[0]
            return {
                "items": [dict(row) | {"title": (row["title"] or "").strip() or row["id"]} for row in rows],
                "total": total,
            }

    def draft(self, owner, template_id, conn=None):
        if conn is None:
            with closing(self.db.connect()) as db:
                return self.draft(owner, template_id, db)
        row = conn.execute(
            "SELECT * FROM template_drafts WHERE owner_id=? AND id=?", (owner, template_id)
        ).fetchone()
        if row:
            return {
                "template": json.loads(row["payload"]),
                "revision": row["revision"],
                "updated_at": row["updated_at"],
            }
        self.service.template(owner, template_id, conn)
        return {"template": None, "revision": 0, "updated_at": None}

    def save_draft(self, owner, template_id, payload):
        from .service import fail

        if (
            not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", template_id)
            or payload.template.get("id") != template_id
        ):
            fail(422, "invalid_template_id", "草稿编号必须与当前流程编号一致")
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT revision FROM template_drafts WHERE owner_id=? AND id=?", (owner, template_id)
            ).fetchone()
            revision = row["revision"] if row else 0
            if payload.expected_revision != revision:
                fail(409, "stale_draft", "草稿已有更新，请重新载入后再保存")
            return self.write_draft(conn, owner, template_id, payload.template, revision + 1)

    def write_draft(self, conn, owner, template_id, template, revision):
        at = now()
        conn.execute(
            """INSERT INTO template_drafts VALUES(?,?,?,?,?)
            ON CONFLICT(owner_id,id) DO UPDATE SET payload=excluded.payload,
            revision=excluded.revision,updated_at=excluded.updated_at""",
            (owner, template_id, dump(template), revision, at),
        )
        return {"template": template, "revision": revision, "updated_at": at}

    def debug(self, owner, payload):
        from .service import fail

        template = payload.template.model_dump()
        stage_id = payload.stage_id or template["stages"][0]["id"]
        stage = next((stage for stage in template["stages"] if stage["id"] == stage_id), None)
        if stage is None:
            fail(422, "invalid_stage", "调试节点不存在，请重新选择")
        unknown = payload.task_values.keys() - {task["id"] for task in stage["tasks"]}
        if unknown:
            fail(422, "invalid_task", "样例包含不属于当前节点的任务")
        tasks = []
        for task in stage["tasks"]:
            sample = {**task, "values": {}, "evidence_ids": [], "confirmed": False}
            sample["values"] = self.service.normalize_values(sample, payload.task_values.get(task["id"], {}))
            tasks.append(sample)
        return self.service.run_workflow(
            owner,
            template,
            stage,
            payload.input,
            tasks,
            context={"test_input": payload.input},
            stop_after_step_id=payload.stop_after_step_id,
        )

    def fingerprint(self, conn, owner, case):
        """Bind inputs without message timestamps, so remounts/chat do not repeat work."""
        digest = hashlib.sha256()

        def include(value):
            digest.update(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())
            digest.update(b"\n")

        include({key: case[key] for key in ("template", "company", "product", "mode_id", "stages", "facts")})
        include(
            {
                "date": datetime.now(timezone.utc).date().isoformat(),
                "assistant_mode": self.service.capabilities["assistant_mode"],
                "model": os.getenv("LLM_MODEL"),
                "base": os.getenv("LLM_BASE_URL"),
                "embedding": self.service.embedding_fingerprint(),
                "runner_version": 1,
            }
        )
        # Iterate bounded-size metadata rows instead of collecting document bodies.
        for row in conn.execute(
            """SELECT d.id,d.version,d.content_hash,d.status,d.case_id,d.filename,d.source_url,
            COALESCE(m.revision,0) metadata_revision FROM documents d
            LEFT JOIN document_metadata m ON m.document_id=d.id
            WHERE d.owner_id=? AND (d.case_id IS NULL OR d.case_id=?) AND d.role='primary' ORDER BY d.id""",
            (owner, case["id"]),
        ):
            include(dict(row))
        for row in conn.execute(
            "SELECT id,version,confirmed FROM artifacts WHERE owner_id=? AND case_id=? ORDER BY id",
            (owner, case["id"]),
        ):
            include(dict(row))
        return digest.hexdigest()

    def analyze(self, owner, case_id, stage_id, payload):
        from .service import fail

        with self.db.transaction() as conn:
            case = self.service.case_detail(owner, case_id, conn)
            self.service.check_revision(case, payload.expected_revision)
            stage = next((stage for stage in case["stages"] if stage["id"] == stage_id), None)
            if stage is None:
                fail(404, "not_found", "未找到办理节点")
            if stage["status"] == "locked":
                fail(409, "stage_locked", "请先完成前序节点的必办任务")
            fingerprint = self.fingerprint(conn, owner, case)
            existing = conn.execute(
                "SELECT * FROM stage_analyses WHERE case_id=? AND stage_id=? AND fingerprint=?",
                (case_id, stage_id, fingerprint),
            ).fetchone()
            if existing:
                if existing["status"] == "completed" or (
                    existing["status"] == "failed" and not payload.retry
                ):
                    return json.loads(existing["payload"])
                if existing["status"] == "running" and existing["expires_at"] > now():
                    fail(409, "analysis_in_progress", "此节点正在分析，请稍后重新查看")
            analysis_id = uid()
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            conn.execute(
                """INSERT INTO stage_analyses VALUES(?,?,?,?,?,?,NULL)
                ON CONFLICT(case_id,stage_id,fingerprint) DO UPDATE SET id=excluded.id,
                status=excluded.status,expires_at=excluded.expires_at,payload=NULL""",
                (case_id, stage_id, fingerprint, analysis_id, "running", expires_at),
            )

        # Retrieval/providers run after the claim transaction has committed.
        try:
            result = self.service.run_workflow(
                owner,
                case["template"],
                stage,
                f"开始「{stage['name']}」：{stage['description']}。请分析现有资料、缺项与下一步办理建议。",
                stage["tasks"],
                case_id=case_id,
                context={
                    "facts": case["facts"],
                    "company": case["company"],
                    "product": case["product"],
                    "mode": next(
                        (mode for mode in case["template"]["modes"] if mode["id"] == case["mode_id"]), None
                    ),
                },
            )
        except Exception:
            logger.warning("stage_analysis_failed", exc_info=True)
            result = {
                "stage_id": stage_id,
                "output": "节点分析暂时不可用，可以稍后重试。",
                "checks": [],
                "citations": [],
                "assistant_mode": self.service.capabilities["assistant_mode"],
                "trace": [
                    {
                        "id": "analysis",
                        "label": "节点分析",
                        "status": "failed",
                        "detail": "分析执行失败，未更新任务状态。",
                    }
                ],
            }
        result.update(
            id=analysis_id,
            case_revision=case["revision"],
            created_at=now(),
            status="failed" if any(step["status"] == "failed" for step in result["trace"]) else "completed",
        )
        with self.db.transaction() as conn:
            current = self.service.case_detail(owner, case_id, conn)
            claim = conn.execute("SELECT id FROM stage_analyses WHERE id=?", (analysis_id,)).fetchone()
            stale = not claim or self.fingerprint(conn, owner, current) != fingerprint
            if stale:
                # Never expose a result against a data/configuration snapshot that moved.
                conn.execute("DELETE FROM stage_analyses WHERE id=?", (analysis_id,))
            else:
                conn.execute(
                    "UPDATE stage_analyses SET status=?,payload=? WHERE id=?",
                    (result["status"], dump(result), analysis_id),
                )
        if stale:
            fail(409, "analysis_stale", "分析期间资料已有更新，请基于当前资料重新分析")
        return result
