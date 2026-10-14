"""Saved editor drafts and isolated stage workflow previews."""

import hashlib
import json
import os
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .db import dump, now, uid
from .defaults import RETIRED_TEMPLATE_ID


class AdminStudio:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        from .debugger import WorkflowDebugger

        self.debugger = WorkflowDebugger(service)
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
                FROM template_drafts WHERE owner_id=? AND id!=? ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?""",
                (owner, RETIRED_TEMPLATE_ID, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM template_drafts WHERE owner_id=? AND id!=?",
                (owner, RETIRED_TEMPLATE_ID),
            ).fetchone()[0]
            return {
                "items": [dict(row) | {"title": (row["title"] or "").strip() or row["id"]} for row in rows],
                "total": total,
            }

    def draft(self, owner, template_id, conn=None):
        if template_id == RETIRED_TEMPLATE_ID:
            self.service.template(owner, template_id)
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
            or template_id == RETIRED_TEMPLATE_ID
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

    def debug(self, owner, payload, **options):
        return self.debugger.debug(owner, payload, **options)

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
                "rerank": self.service.rerank_fingerprint(),
                "runner_version": 3,
            }
        )
        # Iterate bounded-size metadata rows instead of collecting document bodies.
        for row in conn.execute(
            """SELECT d.id,d.version,d.content_hash,d.status,d.case_id,d.filename,d.source_url,dc.country,
            COALESCE(m.revision,0) metadata_revision,COALESCE(r.revision,0) index_revision FROM documents d
            LEFT JOIN document_metadata m ON m.document_id=d.id
            LEFT JOIN document_countries dc ON dc.document_id=d.id
            LEFT JOIN retrieval_documents r ON r.document_id=d.id
            WHERE d.owner_id=? AND (d.case_id IS NULL OR d.case_id=?) AND d.role='primary' ORDER BY d.id""",
            (owner, case["id"]),
        ):
            include(dict(row))
        for row in conn.execute(
            """SELECT c.document_id,c.collection,c.level,c.revision FROM legal_classifications c
            JOIN documents d ON d.id=c.document_id WHERE d.owner_id=?
            AND (d.case_id IS NULL OR d.case_id=?) AND d.role='primary'
            ORDER BY c.document_id,c.collection""",
            (owner, case["id"]),
        ):
            include(dict(row))
        for row in conn.execute(
            "SELECT id,version,confirmed FROM artifacts WHERE owner_id=? AND case_id=? ORDER BY id",
            (owner, case["id"]),
        ):
            include(dict(row))
        return digest.hexdigest()

    def analyze(self, owner, case_id, stage_id, payload, *, emit=None, check_cancelled=None):
        from .countries import resolve_country
        from .service import AppError, fail
        from .streaming import StreamCancelled

        legal = self.service.legal

        def inputs(conn):
            case = self.service.case_detail(owner, case_id, conn)
            self.service.check_revision(case, payload.expected_revision)
            stage = next((item for item in case["stages"] if item["id"] == stage_id), None)
            if stage is None:
                fail(404, "not_found", "未找到办理节点")
            if stage["status"] == "locked":
                fail(409, "stage_locked", "请先完成前序节点的必办任务")
            return case, stage

        with closing(self.db.connect()) as conn:
            case, stage = inputs(conn)
        agent = stage.get("analysis") or case["template"]["agent"]
        legal_enabled = (agent.get("legal") or {}).get("enabled", False)
        countries = (
            legal.allowed_countries(case["template"], stage)
            if legal_enabled
            else [resolve_country(case["template"].get("destination"))]
        )
        if legal_enabled or any(
            step.get("enabled") and step["type"] == "retrieve" for step in agent["steps"]
        ):
            # Prepare before reserving the immutable analysis snapshot. Old-library
            # migration cannot invalidate its own first generated answer; each request
            # still processes at most one shared batch across all allowed countries.
            progress = self.service.index.prepare_scopes(
                owner, countries, case_id=case_id, historical=legal_enabled
            )
            if progress["remaining"]:
                fail(409, "index_preparing", "本次范围仍有资料待索引，已完成一批准备；请重试后再分析。")

        with self.db.transaction() as conn:
            # Preserve the original revision/claim guards against actual concurrent
            # edits during index preparation or provider work.
            case, stage = inputs(conn)
            fingerprint = self.fingerprint(conn, owner, case)
            existing = conn.execute(
                "SELECT * FROM stage_analyses WHERE case_id=? AND stage_id=? AND fingerprint=?",
                (case_id, stage_id, fingerprint),
            ).fetchone()
            if existing and existing["status"] == "running" and existing["expires_at"] > now():
                fail(409, "analysis_in_progress", "此节点正在分析，请稍后重新查看")
            # The link lives in the existing payload, including interrupted runs.
            # Only node-entry records qualify; separate question histories remain intact.
            prior = conn.execute(
                """SELECT payload FROM stage_analyses WHERE case_id=? AND stage_id=?
                AND json_extract(payload,'$.legal_analysis_id') IS NOT NULL
                ORDER BY expires_at DESC,id DESC LIMIT 1""",
                (case_id, stage_id),
            ).fetchone()
            previous = None
            if prior:
                # Expiring a reservation advances its report revision. Read the
                # revision afterwards so an interrupted inline refresh can resume.
                legal.expire_requests(conn)
                previous = legal.mark_stale(
                    conn,
                    legal.own(conn, owner, case_id, json.loads(prior["payload"])["legal_analysis_id"]),
                    legal.snapshot(conn, owner, case),
                )
                if previous["status"] == "running":
                    # Keep the existing node cache while its linked report is
                    # refreshed; an admission conflict must not replace it.
                    fail(409, "analysis_in_progress", "此节点的报告正在分析，请稍后重新查看")
            if existing and existing["status"] in ("completed", "failed"):
                cached = json.loads(existing["payload"])
                same_inputs = previous and previous.get("_snapshot") == legal.snapshot(conn, owner, case)
                if same_inputs and (previous["status"] not in ("failed", "cancelled") or not payload.retry):
                    return cached | self.node_result(stage_id, previous)
            analysis_id = uid()
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            link = {"legal_analysis_id": previous["id"]} if previous else {}
            conn.execute(
                """INSERT INTO stage_analyses VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(case_id,stage_id,fingerprint) DO UPDATE SET id=excluded.id,
                status=excluded.status,expires_at=excluded.expires_at,payload=excluded.payload""",
                (case_id, stage_id, fingerprint, analysis_id, "running", expires_at, dump(link)),
            )

        def progress(event, data):
            if event == "start":
                link["legal_analysis_id"] = data["analysis_id"]
                with self.db.transaction() as conn:
                    conn.execute("UPDATE stage_analyses SET payload=? WHERE id=?", (dump(link), analysis_id))
            if emit:
                emit(event, data)

        # Providers run outside the claim transaction. LegalAnalysis owns revisions,
        # cancellation checkpoints, evidence rules and preservation of manual edits.
        try:
            request = {"client_request_id": "node-" + analysis_id}
            if previous:
                request["expected_revision"] = previous["revision"]
            else:
                request.update(
                    stage_id=stage_id,
                    question=f"开始「{stage['name']}」：{stage['description']}。请分析现有资料、缺项与下一步办理建议。",
                )
            report = legal.analyze(
                owner,
                case_id,
                request,
                analysis_id=previous["id"] if previous else None,
                emit=progress,
                check_cancelled=check_cancelled,
            )
        except (AppError, StreamCancelled) as error:
            if isinstance(error, AppError) and error.code == "legal_analysis_failed" and link:
                report = legal.get(owner, case_id, link["legal_analysis_id"])
            else:
                with self.db.transaction() as conn:
                    conn.execute("UPDATE stage_analyses SET status='cancelled' WHERE id=?", (analysis_id,))
                if isinstance(error, AppError) and error.code == "analysis_context_changed":
                    fail(409, "analysis_stale", "分析期间资料已有更新，请基于当前资料重新分析")
                raise
        except BaseException:
            with self.db.transaction() as conn:
                conn.execute("UPDATE stage_analyses SET status='cancelled' WHERE id=?", (analysis_id,))
            raise
        result = self.node_result(stage_id, report) | {
            "id": analysis_id,
            "case_id": case_id,
            "case_revision": case["revision"],
            "created_at": now(),
        }
        with self.db.transaction() as conn:
            current = self.service.case_detail(owner, case_id, conn)
            claim = conn.execute("SELECT id FROM stage_analyses WHERE id=?", (analysis_id,)).fetchone()
            saved = legal.own(conn, owner, case_id, report["id"])
            stale = (
                not claim
                or self.fingerprint(conn, owner, current) != fingerprint
                or saved.get("_snapshot") != legal.snapshot(conn, owner, current)
            )
            if stale:
                # Keep the report link for the next attempt, never serve a stale result.
                conn.execute("UPDATE stage_analyses SET status='cancelled' WHERE id=?", (analysis_id,))
            else:
                conn.execute(
                    "UPDATE stage_analyses SET status=?,payload=? WHERE id=?",
                    (result["status"], dump(result), analysis_id),
                )
        if stale:
            fail(409, "analysis_stale", "分析期间资料已有更新，请基于当前资料重新分析")
        return result

    def node_context(self, owner, case, stage_id):
        """Bounded, current report context for follow-up chat, including manual text."""
        legal = self.service.legal
        with self.db.transaction() as conn:
            row = conn.execute(
                """SELECT payload FROM stage_analyses WHERE case_id=? AND stage_id=?
                AND json_extract(payload,'$.legal_analysis_id') IS NOT NULL
                ORDER BY expires_at DESC,id DESC LIMIT 1""",
                (case["id"], stage_id),
            ).fetchone()
            if not row:
                return None
            report = legal.own(conn, owner, case["id"], json.loads(row["payload"])["legal_analysis_id"])
            current = report.get("_snapshot") == legal.snapshot(conn, owner, case)
            context = {"id": report["id"], "revision": report["revision"], "status": report["status"]}
            if not current or report["status"] in ("running", "failed", "cancelled"):
                return context | {"notice": "节点分析尚未完成或资料已变化，请先检查最新资料，不沿用旧结论。"}
            return context | {
                "question": report["question"][:2000],
                "sections": [
                    {
                        "title": section["title"],
                        "content": section["content"][:1500],
                        "edited": section["edited"],
                        "needs_review": section["needs_review"],
                    }
                    for section in report["sections"][:6]
                ],
                "truncated": len(report["sections"]) > 6
                or any(len(s["content"]) > 1500 for s in report["sections"]),
                "followups": report["result"]["followups"][:12],
            }

    @staticmethod
    def node_result(stage_id, report):
        result = report["result"]
        outputs = [step.get("output_data") or {} for step in result.get("trace", [])]
        hybrid = any(
            output.get("retrieval_mode") == "hybrid"
            or any(item.get("mode") == "hybrid" for item in output.get("coverage", []))
            for output in outputs
        )
        return {
            **result,
            "stage_id": stage_id,
            "checks": [],
            "retrieval_mode": "hybrid" if hybrid else "lexical",
            "legal_analysis_id": report["id"],
            "legal_revision": report["revision"],
            "status": "failed" if report["status"] in ("failed", "cancelled") else "completed",
        }
