import json
import logging
from contextlib import closing
from datetime import datetime, timedelta, timezone

from app.shared.domain.errors import fail
from app.shared.domain.values import dump, now, uid

logger = logging.getLogger(__name__)


class SourceService:
    def __init__(self, app):
        self.app = app

    def source_dict(self, row, conn=None, latest_run=...):
        from app.contexts.ingestion.application.contracts import CollectionPlan

        if conn is None:
            with closing(self.app.db.connect()) as conn:
                return self.app.source_dict(row, conn)
        result = {
            key: bool(row[key]) if key == "enabled" else json.loads(row[key]) if key == "tags" else row[key]
            for key in (
                "id",
                "name",
                "url",
                "enabled",
                "interval_hours",
                "tags",
                "last_fetched_at",
                "status",
                "last_error",
                "revision",
            )
        }
        result["plan"] = CollectionPlan.model_validate(json.loads(row["plan"])).model_dump()
        result["checklist"] = json.loads(row["checklist"]) if row["checklist"] else None
        if latest_run is ...:
            latest = conn.execute(
                "SELECT * FROM collection_runs WHERE source_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            latest_run = self.app.collection.run_dict(conn, latest) if latest else None
        result["latest_run"] = latest_run
        return result

    def sources(self, owner, limit=100, offset=0):
        with closing(self.app.db.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM sources WHERE owner_id=? ORDER BY created_at,id LIMIT ? OFFSET ?",
                (owner, limit, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM sources WHERE owner_id=?", (owner,)).fetchone()[0]
            latest = {}
            if rows:
                placeholders = ",".join("?" for _ in rows)
                runs = conn.execute(
                    f"""SELECT * FROM (
                    SELECT r.*,ROW_NUMBER() OVER(PARTITION BY source_id ORDER BY created_at DESC,id DESC) selected
                    FROM collection_runs r WHERE source_id IN ({placeholders})
                ) WHERE selected=1""",
                    [row["id"] for row in rows],
                ).fetchall()
                counts = self.app.collection.run_counts(conn, [row["id"] for row in runs])
                latest = {
                    row["source_id"]: self.app.collection.run_dict(conn, row, counts[row["id"]])
                    for row in runs
                }
            return {
                "items": [self.app.source_dict(row, conn, latest.get(row["id"])) for row in rows],
                "total": total,
            }

    def save_source(self, owner, payload, id=None, conn=None):
        from app.contexts.ingestion.application.contracts import CollectionPlan
        from app.shared.infrastructure.http.download import _validated_url

        if conn is None:
            with self.app.db.transaction() as conn:
                return self.app.save_source(owner, payload, id, conn)
        if id:
            row = conn.execute("SELECT * FROM sources WHERE owner_id=? AND id=?", (owner, id)).fetchone()
            if not row:
                fail(404, "not_found", "未找到采集来源")
            if row["checklist"]:
                fail(409, "checklist_source", "清单来源由版本化计划管理，请在采集清单中操作")
            if (
                payload.get("expected_revision") is not None
                and payload["expected_revision"] != row["revision"]
            ):
                fail(409, "stale_source", "来源配置已有更新，请刷新后保存")
            values = self.app.source_dict(row, conn) | {k: v for k, v in payload.items() if v is not None}
            self.app.collection.validate_plan(conn, owner, values["plan"])
            try:
                _validated_url(values["url"])
            except ValueError as exc:
                fail(422, "invalid_source", str(exc))
            conn.execute(
                "UPDATE sources SET name=?,url=?,enabled=?,interval_hours=?,tags=?,updated_at=?,plan=?,revision=revision+1 WHERE id=?",
                (
                    values["name"],
                    values["url"],
                    int(values["enabled"]),
                    values["interval_hours"],
                    dump(values["tags"]),
                    now(),
                    dump(values["plan"]),
                    id,
                ),
            )
        else:
            plan = CollectionPlan.model_validate(payload.get("plan", {})).model_dump()
            self.app.collection.validate_plan(conn, owner, plan)
            try:
                _validated_url(payload["url"])
            except ValueError as exc:
                fail(422, "invalid_source", str(exc))
            id, at = uid(), now()
            conn.execute(
                "INSERT INTO sources(id,owner_id,name,url,enabled,interval_hours,tags,last_fetched_at,last_attempt_at,status,last_error,created_at,updated_at,plan) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    id,
                    owner,
                    payload["name"],
                    payload["url"],
                    int(payload["enabled"]),
                    payload["interval_hours"],
                    dump(payload["tags"]),
                    None,
                    None,
                    "idle",
                    None,
                    at,
                    at,
                    dump(plan),
                ),
            )
        return self.app.source_dict(conn.execute("SELECT * FROM sources WHERE id=?", (id,)).fetchone(), conn)

    def due_sources(self, *, worker_scope=None):
        from app.contexts.documents.application.system_library import BackgroundWorkerScope

        scope, arguments = (worker_scope or BackgroundWorkerScope()).sql("s.owner_id")
        with closing(self.app.db.connect()) as conn:
            rows = conn.execute(
                """SELECT id,owner_id,last_attempt_at,interval_hours,status,
                EXISTS(
                    SELECT 1 FROM checklist_bindings b
                    JOIN checklist_candidates c ON c.binding_id=b.id
                    WHERE b.source_id=s.id AND b.owner_id=s.owner_id AND c.status='pending'
                        AND json_extract(b.snapshot,'$.item.execution')='discovery'
                        AND json_extract(b.snapshot,'$.item.conditional')=0
                        AND NOT EXISTS(
                            SELECT 1 FROM collection_items i WHERE i.candidate_id=c.id
                        )
                ) AS has_unattempted_candidates
                FROM sources s WHERE enabled=1
                AND NOT EXISTS(SELECT 1 FROM collection_runs r WHERE r.source_id=s.id AND r.status IN ('queued','running'))
                AND NOT EXISTS(
                    SELECT 1 FROM checklist_bindings new_binding
                    JOIN checklist_bindings old_binding
                        ON old_binding.owner_id=new_binding.owner_id
                        AND old_binding.plan_id=new_binding.plan_id
                        AND old_binding.item_id=new_binding.item_id
                        AND old_binding.version<new_binding.version
                    JOIN collection_runs old_run ON old_run.source_id=old_binding.source_id
                        AND old_run.status IN ('queued','running')
                    JOIN collection_items old_item ON old_item.run_id=old_run.id
                        AND old_item.status IN ('queued','fetching')
                    WHERE new_binding.source_id=s.id AND new_binding.owner_id=s.owner_id
                )
                AND (last_attempt_at IS NULL OR julianday(last_attempt_at)+interval_hours/24.0<=julianday('now')
                    OR EXISTS(
                        SELECT 1 FROM checklist_bindings b
                        JOIN checklist_candidates c ON c.binding_id=b.id
                        WHERE b.source_id=s.id AND b.owner_id=s.owner_id AND c.status='pending'
                            AND json_extract(b.snapshot,'$.item.execution')='discovery'
                            AND json_extract(b.snapshot,'$.item.conditional')=0
                            AND NOT EXISTS(
                                SELECT 1 FROM collection_items i WHERE i.candidate_id=c.id
                            )
                    ))"""
                + scope
                + " ORDER BY COALESCE(last_attempt_at,''),id LIMIT 100",
                arguments,
            ).fetchall()
        current = datetime.now(timezone.utc)
        return [
            {"id": r["id"], "owner_id": r["owner_id"]}
            for r in rows
            if r["has_unattempted_candidates"]
            or r["last_attempt_at"] is None
            or datetime.fromisoformat(r["last_attempt_at"]) + timedelta(hours=r["interval_hours"]) <= current
        ]

    def fetch_source(self, owner_id, source_id, payload=None):
        """Compatibility entry point; all collection uses the durable queue."""
        return self.app.collection.enqueue(owner_id, source_id, payload or {"client_request_id": uid()})

    def detail(self, owner, source_id):
        with closing(self.app.db.connect()) as conn:
            row = conn.execute(
                "SELECT * FROM sources WHERE id=? AND owner_id=?", (source_id, owner)
            ).fetchone()
            if not row:
                fail(404, "not_found", "未找到采集来源")
            return self.source_dict(row, conn)
