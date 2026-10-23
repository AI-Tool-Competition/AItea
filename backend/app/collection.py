"""Durable single-node collection queues. Network/model work never owns a DB write lock."""

import hashlib
import base64
import json
import logging
import os
import random
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

from .db import dump, now, uid
from .models import CollectionPlan
from .parsing import parse_worker_limit
from .system_library import BackgroundWorkerScope

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120
MAX_ATTEMPTS = 3
_PROCESSING_SOURCE_FIELDS = (
    "owner_id",
    "role",
    "version",
    "content_hash",
    "size",
    "storage_path",
    "source_url",
)


def _setting(name, default, maximum):
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


def _storage_busy(error):
    code = getattr(error, "sqlite_errorcode", None)
    return (
        isinstance(error, sqlite3.OperationalError)
        and isinstance(code, int)
        and code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    )


class _HostCooldownDeferred(ValueError):
    """An already cooled redirect target issued no new network request."""

    retryable = True

    def __init__(self, host, retry_after):
        super().__init__("来源站点正在冷却，任务稍后继续采集")
        self.response_host = host
        self.retry_after = retry_after


class CollectionService:
    def __init__(self, service):
        self.service, self.db = service, service.db
        self.download_workers = _setting("TEA_COLLECT_WORKERS", 8, 16)
        self.host_workers = min(_setting("TEA_COLLECT_PER_HOST", 2, 4), self.download_workers)
        self.processing_workers = _setting("TEA_PROCESS_WORKERS", 2, 32)
        self.embedding_workers = min(_setting("TEA_EMBED_WORKERS", 2, 32), self.processing_workers)
        parse_worker_limit()  # Reject invalid parser capacity before claiming work.
        self.worker_health = {
            "status": "stopped",
            "last_error_at": None,
            "shared_library_workers": "internal",
        }
        self._migrate()
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=6").fetchone():
                conn.execute(
                    "CREATE TABLE collection_network_leases(id TEXT PRIMARY KEY,host TEXT NOT NULL,expires_at REAL NOT NULL)"
                )
                conn.execute("CREATE INDEX network_leases_host ON collection_network_leases(host,expires_at)")
                conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (6, now()))
        from .transfers import CollectionTransfers

        self.transfers = CollectionTransfers(self)
        from .collection_recovery import CollectionRecovery

        self.recovery = CollectionRecovery(self)
        from .mineru import MinerUParser

        self.parser = MinerUParser(self)

    def _migrate(self):
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=5").fetchone():
                return
            conn.execute("ALTER TABLE sources ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            conn.execute("ALTER TABLE sources ADD COLUMN plan TEXT NOT NULL DEFAULT '{}' ")
            conn.execute("""CREATE TABLE collection_runs(
                id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), source_id TEXT NOT NULL REFERENCES sources(id),
                client_request_id TEXT NOT NULL, request_hash TEXT NOT NULL, plan TEXT NOT NULL,
                status TEXT NOT NULL, revision INTEGER NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(owner_id,client_request_id))""")
            conn.execute(
                "CREATE INDEX runs_source ON collection_runs(owner_id,source_id,created_at DESC,id DESC)"
            )
            conn.execute("CREATE INDEX runs_active ON collection_runs(status,source_id)")
            conn.execute("""CREATE TABLE collection_items(
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES collection_runs(id), owner_id TEXT NOT NULL REFERENCES users(id),
                url TEXT NOT NULL, host TEXT NOT NULL, identity_key TEXT, level TEXT NOT NULL, parent_document_id TEXT REFERENCES documents(id),
                document_id TEXT REFERENCES documents(id), status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL, error TEXT,
                final_url TEXT, fetched_at TEXT, created_at TEXT NOT NULL,
                UNIQUE(run_id,url,level,parent_document_id))""")
            conn.execute(
                "CREATE INDEX items_claim ON collection_items(status,available_at,lease_until,created_at,id)"
            )
            conn.execute("CREATE INDEX items_host ON collection_items(host,status,lease_until)")
            conn.execute("CREATE INDEX items_run ON collection_items(run_id,created_at,id)")
            conn.execute("CREATE INDEX items_document ON collection_items(document_id,run_id)")
            conn.execute("""CREATE TABLE processing_jobs(
                document_id TEXT PRIMARY KEY REFERENCES documents(id), owner_id TEXT NOT NULL REFERENCES users(id),
                phase TEXT NOT NULL, status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0, lease_token TEXT, lease_until REAL, error TEXT, updated_at TEXT NOT NULL)""")
            conn.execute(
                "CREATE INDEX processing_claim ON processing_jobs(status,available_at,lease_until,updated_at)"
            )
            conn.execute("""CREATE TABLE collection_http_cache(
                owner_id TEXT NOT NULL REFERENCES users(id), url TEXT NOT NULL, document_id TEXT NOT NULL REFERENCES documents(id),
                etag TEXT, last_modified TEXT, final_url TEXT NOT NULL, PRIMARY KEY(owner_id,url))""")
            conn.execute(
                "CREATE TABLE collection_host_cooldowns(host TEXT PRIMARY KEY, available_at REAL NOT NULL)"
            )
            conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (5, now()))

    def validate_plan(self, conn, owner, plan):
        from .ingestion import _validated_url
        from .service import fail
        from .countries import country_from_collection, document_country

        country = country_from_collection(plan["collection"])
        for url in plan["seed_urls"]:
            try:
                _validated_url(url)
            except ValueError as exc:
                fail(422, "invalid_source", str(exc))
        for id in plan["primary_document_ids"]:
            doc = self.service.own_document(conn, owner, id)
            if doc["case_id"] or doc["role"] != "primary":
                fail(422, "invalid_primary", "采集来源只能关联账号公共库中的一级法规")
            if country and document_country(conn, id) != country:
                fail(422, "country_mismatch", "一级法规必须属于当前国家")
            row = conn.execute(
                "SELECT level FROM legal_classifications WHERE document_id=? AND collection=?",
                (id, plan["collection"]),
            ).fetchone()
            if (country and not row) or (row and row[0] != "primary"):
                fail(422, "invalid_primary", "所选法规在当前采集组中不是一级资料")

    @staticmethod
    def source_due(conn, owner, source_id):
        return (
            conn.execute(
                """SELECT 1 FROM sources s WHERE s.id=? AND s.owner_id=? AND s.enabled=1
            AND (s.last_attempt_at IS NULL OR julianday(s.last_attempt_at)+s.interval_hours/24.0<=julianday('now'))
            AND NOT EXISTS(SELECT 1 FROM collection_runs r WHERE r.source_id=s.id AND r.status IN ('queued','running'))""",
                (source_id, owner),
            ).fetchone()
            is not None
        )

    def enqueue_scheduled(self, owner, source_id):
        """Poll a source while preserving its checklist and legal review gates."""
        from .service import AppError, fail

        with closing(self.db.connect()) as conn:
            source = conn.execute(
                "SELECT * FROM sources WHERE id=? AND owner_id=?", (source_id, owner)
            ).fetchone()
            if source is None:
                fail(404, "not_found", "未找到采集来源")
            if not self.source_due(conn, owner, source_id):
                return None
            binding = conn.execute(
                "SELECT * FROM checklist_bindings WHERE source_id=? AND owner_id=?", (source_id, owner)
            ).fetchone()
            if binding is not None:
                previous = conn.execute(
                    """SELECT json_extract(plan,'$.checklist.kind') FROM collection_runs
                    WHERE source_id=? ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (source_id,),
                ).fetchone()
                previous_kind = previous[0] if previous else None
                pending = conn.execute(
                    "SELECT COUNT(*) FROM checklist_candidates WHERE binding_id=? AND status='pending'",
                    (binding["id"],),
                ).fetchone()[0]
                candidate_ids = []
                kind = "discovery"
                if pending or previous_kind == "discovery":
                    kind = "collection"
                    states = ("pending",) if pending else ("stored",)
                    marks = ",".join("?" for _ in states)
                    candidate_ids = [
                        row[0]
                        for row in conn.execute(
                            f"""SELECT c.id FROM checklist_candidates c
                            LEFT JOIN (SELECT candidate_id,MAX(fetched_at) last_fetched
                                FROM collection_items WHERE candidate_id IS NOT NULL GROUP BY candidate_id) f
                                ON f.candidate_id=c.id
                            WHERE c.binding_id=? AND c.status IN ({marks})
                            ORDER BY COALESCE(julianday(f.last_fetched),julianday(c.created_at)),c.id LIMIT 200""",
                            (binding["id"], *states),
                        )
                    ]
                    if not candidate_ids:
                        kind = "discovery"
        if not source["checklist"]:
            return self.enqueue(owner, source_id, {"client_request_id": "schedule-" + uid()}, scheduled=True)
        if binding is None:
            fail(409, "checklist_binding_missing", "清单来源缺少版本绑定，请核验配置")
        payload = {"client_request_id": "schedule-" + uid(), "confirm_condition": False}
        if kind == "collection":
            payload.update(candidate_ids=candidate_ids, level="primary", primary_document_ids=[])
        try:
            return self.service.checklists.start(
                owner,
                binding["plan_id"],
                binding["item_id"],
                payload,
                kind=kind,
                version=binding["version"],
                scheduled=True,
            )
        except AppError as error:
            if error.code not in {
                "condition_required",
                "specialized_review_required",
                "adapter_required",
                "no_candidates",
                "definition_changed",
                "unknown_plan",
                "not_found",
                "candidate_unavailable",
                "no_candidates",
            }:
                raise
            # A bounded scheduled attempt does not grant condition approval or
            # replace immutable plan definitions. Keep the actionable reason.
            with self.db.transaction() as conn:
                if self.source_due(conn, owner, source_id):
                    conn.execute(
                        "UPDATE sources SET last_attempt_at=?,status='failed',last_error=?,updated_at=? WHERE id=? AND owner_id=?",
                        (now(), error.message[:450], now(), source_id, owner),
                    )
            return None

    def enqueue(
        self, owner, source_id, payload, *, scheduled=False, conn=None, trusted_plan=None, request_hash=None
    ):
        from .ingestion import _validated_url
        from .service import fail

        request_hash = (
            request_hash or hashlib.sha256(dump([source_id, payload.get("entries")]).encode()).hexdigest()
        )
        if conn is None:
            with self.db.transaction() as conn:
                return self.enqueue(
                    owner,
                    source_id,
                    payload,
                    scheduled=scheduled,
                    conn=conn,
                    trusted_plan=trusted_plan,
                    request_hash=request_hash,
                )
        source = conn.execute(
            "SELECT * FROM sources WHERE owner_id=? AND id=?", (owner, source_id)
        ).fetchone()
        if not source:
            fail(404, "not_found", "未找到采集来源")
        if source["checklist"] and trusted_plan is None:
            fail(409, "checklist_source", "请从采集清单发现候选并选择正文，目录不能直接入库")
        if scheduled and not self.source_due(conn, owner, source_id):
            return None  # Re-check due state atomically; another scheduler/manual request may have won.
        previous = conn.execute(
            "SELECT * FROM collection_runs WHERE owner_id=? AND client_request_id=?",
            (owner, payload["client_request_id"]),
        ).fetchone()
        if previous:
            if previous["request_hash"] != request_hash:
                fail(409, "request_conflict", "同一请求标识不能用于不同的采集内容")
            return self.run_dict(conn, previous)
        plan = trusted_plan or CollectionPlan.model_validate(json.loads(source["plan"])).model_dump()
        self.validate_plan(conn, owner, plan)
        entries = payload.get("entries") or [{"url": url} for url in [source["url"], *plan["seed_urls"]]]
        unique = {}
        for entry in entries:
            try:
                url, host = _validated_url(entry["url"])
            except ValueError as exc:
                fail(422, "invalid_source", str(exc))
            if url in unique and unique[url].get("identity_key") != entry.get("identity_key"):
                fail(422, "ambiguous_identity", "同一网址不能指定多个法规标识")
            unique[url] = dict(entry) | {"url": url, "host": host}
        if len(unique) > plan["max_items"]:
            fail(422, "collection_limit", "种子网址数量超过本次采集上限")
        outstanding = conn.execute(
            "SELECT COUNT(*) FROM collection_items WHERE status IN ('queued','fetching')"
        ).fetchone()[0]
        owned = conn.execute(
            "SELECT COUNT(*) FROM collection_items WHERE owner_id=? AND status IN ('queued','fetching')",
            (owner,),
        ).fetchone()[0]
        if outstanding + len(unique) > 2000 or owned + len(unique) > 1000:
            fail(429, "collection_backpressure", "采集队列已满，请等待已有任务完成")
        plan["tags"] = list(dict.fromkeys(json.loads(source["tags"])))
        id, at = uid(), now()
        conn.execute(
            "INSERT INTO collection_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                id,
                owner,
                source_id,
                payload["client_request_id"],
                request_hash,
                dump(plan),
                "queued",
                1,
                None,
                at,
                at,
            ),
        )
        for entry in unique.values():
            self._insert_item(
                conn,
                id,
                owner,
                entry["url"],
                plan["level"],
                identity_key=entry.get("identity_key"),
                purpose=entry.get("purpose", "document") if trusted_plan else "document",
                depth=entry.get("depth", 0) if trusted_plan else 0,
                candidate_id=entry.get("candidate_id") if trusted_plan else None,
            )
        conn.execute(
            "UPDATE sources SET status='processing',last_attempt_at=?,last_error=NULL WHERE id=?",
            (at, source_id),
        )
        return self.run_dict(conn, self.own_run(conn, owner, id))

    def _insert_item(
        self,
        conn,
        run_id,
        owner,
        url,
        level,
        *,
        identity_key=None,
        parent=None,
        purpose="document",
        depth=0,
        candidate_id=None,
    ):
        # NULL parents do not deduplicate under SQLite UNIQUE, so check under the same write lock.
        if conn.execute(
            "SELECT 1 FROM collection_items WHERE run_id=? AND url=? AND level=? AND parent_document_id IS ?",
            (run_id, url, level, parent),
        ).fetchone():
            return
        conn.execute(
            """INSERT INTO collection_items(id,run_id,owner_id,url,host,identity_key,level,parent_document_id,status,created_at,purpose,depth,candidate_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uid(),
                run_id,
                owner,
                url,
                urlsplit(url).hostname,
                identity_key,
                level,
                parent,
                "queued",
                now(),
                purpose,
                depth,
                candidate_id,
            ),
        )

    def own_run(self, conn, owner, run_id):
        from .service import fail

        row = conn.execute(
            "SELECT * FROM collection_runs WHERE id=? AND owner_id=?", (run_id, owner)
        ).fetchone()
        if not row:
            fail(404, "not_found", "未找到采集任务")
        return row

    def run_counts(self, conn, run_ids):
        if not run_ids:
            return {}
        placeholders = ",".join("?" for _ in run_ids)
        rows = conn.execute(
            f"""SELECT i.run_id,COUNT(*) total,
            COALESCE(SUM(i.status='stored'),0) stored, COALESCE(SUM(i.status='discovered'),0) pages,
            COALESCE(SUM(i.status='failed'),0) failed,
            COALESCE(SUM(i.status IN ('queued','fetching')),0) pending,
            COALESCE(SUM(i.status='stored' AND p.status IN ('queued','processing')),0) processing,
            COALESCE(SUM(i.status='stored' AND (p.status='completed' OR p.document_id IS NULL)),0) processed,
            COALESCE(SUM(i.status='stored' AND p.status='failed'),0) processing_failed
            FROM collection_items i LEFT JOIN processing_jobs p ON p.document_id=i.document_id
            WHERE i.run_id IN ({placeholders}) GROUP BY i.run_id""",
            run_ids,
        ).fetchall()
        return {
            row["run_id"]: {
                key: row[key]
                for key in (
                    "total",
                    "stored",
                    "pages",
                    "failed",
                    "pending",
                    "processing",
                    "processed",
                    "processing_failed",
                )
            }
            for row in rows
        }

    def run_dict(self, conn, row, counts=None):
        if counts is None:
            counts = self.run_counts(conn, [row["id"]]).get(row["id"], {})
        return (
            {
                key: row[key]
                for key in ("id", "source_id", "status", "revision", "error", "created_at", "updated_at")
            }
            | counts
            | {"kind": json.loads(row["plan"]).get("checklist", {}).get("kind", "collection")}
        )

    def run_detail(self, owner, run_id, limit=50, offset=0):
        with closing(self.db.connect()) as conn:
            run = self.run_dict(conn, self.own_run(conn, owner, run_id))
            rows = conn.execute(
                """SELECT i.id,i.url,i.level,i.purpose,i.status,i.attempts,i.document_id,i.error,i.final_url,i.fetched_at,
                p.status processing_status,p.error processing_error
                FROM collection_items i LEFT JOIN processing_jobs p ON p.document_id=i.document_id
                WHERE i.run_id=? ORDER BY i.created_at,i.id LIMIT ? OFFSET ?""",
                (run_id, limit, offset),
            ).fetchall()
            progress = self.transfers.progress(conn, [row["id"] for row in rows])
            return {
                "run": run,
                "items": [dict(row) | progress.get(row["id"], {}) for row in rows],
                "total": run["total"],
            }

    def runs(self, owner, source_id, limit=20, offset=0):
        from .service import fail

        with closing(self.db.connect()) as conn:
            if not conn.execute(
                "SELECT 1 FROM sources WHERE id=? AND owner_id=?", (source_id, owner)
            ).fetchone():
                fail(404, "not_found", "未找到采集来源")
            rows = conn.execute(
                "SELECT * FROM collection_runs WHERE source_id=? AND owner_id=? ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (source_id, owner, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) FROM collection_runs WHERE source_id=? AND owner_id=?", (source_id, owner)
            ).fetchone()[0]
            counts = self.run_counts(conn, [row["id"] for row in rows])
            return {"items": [self.run_dict(conn, row, counts[row["id"]]) for row in rows], "total": total}

    def control(self, owner, run_id, action, expected_revision):
        from .service import fail

        with self.db.transaction() as conn:
            row = self.own_run(conn, owner, run_id)
            if row["revision"] != expected_revision:
                fail(409, "stale_run", "采集进度已有更新，请刷新后操作")
            if action == "cancel":
                if row["status"] in ("completed", "partial", "cancelled"):
                    return self.run_dict(conn, row)
                for item in conn.execute(
                    "SELECT * FROM collection_items WHERE run_id=? AND status IN ('queued','fetching')",
                    (run_id,),
                ).fetchall():
                    self.service.checklists.release_candidate(conn, dict(item))
                conn.execute(
                    "UPDATE collection_items SET status='cancelled',lease_token=NULL,lease_until=NULL WHERE run_id=? AND status IN ('queued','fetching')",
                    (run_id,),
                )
                status = "cancelled"
            else:
                if row["status"] not in ("partial", "cancelled"):
                    fail(409, "run_active", "请等待当前采集结束后重试失败项")
                if json.loads(row["plan"]).get("checklist"):
                    if conn.execute(
                        "SELECT 1 FROM collection_runs WHERE source_id=? AND id<>? AND status IN ('queued','running')",
                        (row["source_id"], run_id),
                    ).fetchone():
                        fail(409, "collection_active", "本条目已有进行中的任务")
                    candidates = conn.execute(
                        """SELECT c.id,c.status FROM collection_items i
                        JOIN checklist_candidates c ON c.id=i.candidate_id WHERE i.run_id=?
                        AND i.status IN ('failed','cancelled')""",
                        (run_id,),
                    ).fetchall()
                    if any(candidate["status"] not in ("pending", "stored") for candidate in candidates):
                        fail(409, "candidate_unavailable", "重试候选已排除或正在采集，请刷新")
                    conn.executemany(
                        "UPDATE checklist_candidates SET status='queued',revision=revision+1 WHERE id=?",
                        [(c["id"],) for c in candidates],
                    )
                retryable = conn.execute(
                    """SELECT COUNT(*) FROM collection_items i
                    LEFT JOIN processing_jobs p ON p.document_id=i.document_id
                    WHERE i.run_id=? AND (i.status IN ('failed','cancelled') OR p.status='failed')""",
                    (run_id,),
                ).fetchone()[0]
                if not retryable:
                    fail(
                        409,
                        "nothing_to_retry",
                        "没有可重试的失败项；如需补充采集范围，请调整来源后创建新任务",
                    )
                downloads = conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE run_id=? AND status IN ('failed','cancelled')",
                    (run_id,),
                ).fetchone()[0]
                outstanding = conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE status IN ('queued','fetching')"
                ).fetchone()[0]
                owned = conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE owner_id=? AND status IN ('queued','fetching')",
                    (owner,),
                ).fetchone()[0]
                if outstanding + downloads > 2000 or owned + downloads > 1000:
                    fail(429, "collection_backpressure", "采集队列已满，请等待已有任务完成后再重试")
                conn.execute(
                    "UPDATE collection_items SET status='queued',attempts=0,available_at=0,error=NULL,lease_token=NULL,lease_until=NULL WHERE run_id=? AND status IN ('failed','cancelled')",
                    (run_id,),
                )
                for job in conn.execute(
                    """SELECT document_id FROM processing_jobs WHERE status='failed' AND owner_id=?
                    AND document_id IN (SELECT document_id FROM collection_items WHERE run_id=?)""",
                    (owner, run_id),
                ).fetchall():
                    self.queue_processing(conn, job["document_id"], owner)
                status = "queued"
            conn.execute(
                "UPDATE collection_runs SET status=?,revision=revision+1,updated_at=? WHERE id=?",
                (status, now(), run_id),
            )
            self._update_source(conn, run_id)
            return self.run_dict(conn, self.own_run(conn, owner, run_id))

    def queue_processing(self, conn, document_id, owner):
        if conn.execute(
            """SELECT 1 FROM processing_jobs p JOIN collection_recovery_jobs r
            ON r.item_id=p.document_id AND r.owner_id=p.owner_id AND r.queue_kind='processing'
            AND r.phase=p.phase WHERE p.document_id=? AND p.owner_id=? AND p.status='failed'
            AND p.phase='embed' AND r.status='blocked' AND r.reason='paid_result_unknown'""",
            (document_id, owner),
        ).fetchone():
            from .service import fail

            fail(
                409,
                "embedding_outcome_review_required",
                "向量请求结果尚未确认，请先核验已保存回执并指定补齐范围",
            )
        conn.execute(
            """INSERT INTO processing_jobs(document_id,owner_id,phase,status,updated_at) VALUES(?,?,'parse','queued',?)
            ON CONFLICT(document_id) DO UPDATE SET status='queued',attempts=0,available_at=0,error=NULL,updated_at=excluded.updated_at
            WHERE processing_jobs.status IN ('completed','failed')""",
            (document_id, owner, now()),
        )

    def claim_fetch(self, *, worker_scope=BackgroundWorkerScope()):
        scope, arguments = worker_scope.sql()
        item_scope, item_arguments = worker_scope.sql("i.owner_id")
        timestamp = time.time()
        with self.db.transaction() as conn:
            self._expire(conn, "collection_items", "fetching", timestamp, worker_scope=worker_scope)
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE status='fetching' AND lease_until>?" + scope,
                    (timestamp, *arguments),
                ).fetchone()[0]
                >= self.download_workers
            ):
                return None
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')" + scope,
                    arguments,
                ).fetchone()[0]
                >= 200
            ):
                return None  # Disk/parse queue backpressure, not an unbounded in-memory future queue.
            row = conn.execute(
                """SELECT i.*,r.plan FROM collection_items i JOIN collection_runs r ON r.id=i.run_id
                LEFT JOIN collection_host_cooldowns h ON h.host=i.host
                WHERE i.status='queued' AND i.available_at<=? AND r.status IN ('queued','running')
                AND COALESCE(h.available_at,0)<=? AND (SELECT COUNT(*) FROM collection_items a
                    WHERE a.host=i.host AND a.status='fetching' AND a.lease_until>?)<?"""
                + item_scope
                + """
                -- Rotate durable runs before taking another page/file from a
                -- large category sharing the same host.
                ORDER BY r.updated_at,i.created_at,i.id LIMIT 1""",
                (timestamp, timestamp, timestamp, self.host_workers, *item_arguments),
            ).fetchone()
            if not row:
                return None
            token = uid()
            conn.execute(
                "UPDATE collection_items SET status='fetching',attempts=attempts+1,lease_token=?,lease_until=? WHERE id=?",
                (token, timestamp + LEASE_SECONDS, row["id"]),
            )
            conn.execute(
                "UPDATE collection_runs SET status='running',updated_at=? WHERE id=?",
                (now(), row["run_id"]),
            )
            return dict(row) | {"lease_token": token, "attempts": row["attempts"] + 1}

    @staticmethod
    def _processing_scope(owner_id, document_ids):
        if owner_id is None and document_ids is None:
            return "", ()
        if (
            not isinstance(owner_id, str)
            or not owner_id
            or len(owner_id) > 100
            or not isinstance(document_ids, (list, tuple))
            or not 1 <= len(document_ids) <= 20
            or any(not isinstance(item, str) or not item or len(item) > 100 for item in document_ids)
            or len(set(document_ids)) != len(document_ids)
        ):
            raise ValueError("处理范围需要账号和一至二十个不重复的文档标识。")
        return " AND owner_id=? AND document_id IN (" + ",".join("?" for _ in document_ids) + ")", (
            owner_id,
            *document_ids,
        )

    @staticmethod
    def _processing_policy_clause(policy):
        if policy is None:
            return ""
        if policy != "draft_html_v3":
            raise ValueError("正文处理授权策略无效。")
        # Metadata and parser markers come from the current stored document,
        # never from a prepare result or the worker's earlier document copy.
        manifest = "CASE WHEN json_valid(d.manifest) THEN d.manifest ELSE '{}' END"
        metadata = "CASE WHEN json_valid(m.payload) THEN m.payload ELSE '{}' END"
        return f""" AND EXISTS (
            SELECT 1 FROM documents d JOIN document_metadata m ON m.document_id=d.id
            WHERE d.id=processing_jobs.document_id AND d.owner_id=processing_jobs.owner_id
                AND d.role='primary' AND d.source_url IS NOT NULL AND d.source_url<>''
                AND json_extract({metadata},'$.review_status')='draft'
                AND json_extract({manifest},'$.parser')='html-basic'
                AND json_extract({manifest},'$.parser_version') IN ('3','4','5','6','7','8')
                AND (json_extract({manifest},'$.pipeline_version') IN ('html-source-mapping-v3','html-source-mapping-v4','html-source-mapping-v5','html-source-mapping-v6','html-source-mapping-v7','html-source-mapping-v8')
                    OR substr(json_extract({manifest},'$.pipeline_version'),1,23) IN ('html-source-mapping-v3+','html-source-mapping-v4+','html-source-mapping-v5+','html-source-mapping-v6+','html-source-mapping-v7+','html-source-mapping-v8+'))
                AND json_extract({manifest},'$.html_source.mapping_version') IN ('html-source-mapping-v3','html-source-mapping-v4','html-source-mapping-v5','html-source-mapping-v6','html-source-mapping-v7','html-source-mapping-v8')
                AND json_extract({manifest},'$.source_hash')=d.content_hash
                AND json_extract({manifest},'$.html_source.source_hash')=d.content_hash
        )"""

    def _persist_ocr_image(self, claim, rendered):
        """Persist bounded OCR pixels only while this draft still has authority."""
        with closing(self.db.connect()) as conn:
            if not self._valid_claim(conn, claim, True):
                raise RuntimeError("图片识别授权或处理租约已变化，停止保存模型输入。")
        raw = base64.b64decode(rendered["image_base64"], validate=True)
        blob = self.service.archive.blobs.put_bytes(claim["owner_id"], raw)
        return {
            "content_hash": blob.digest,
            "size": blob.size,
            "storage_path": str(blob.path.relative_to(self.db.data_dir.resolve())),
            "mime_type": "image/png",
        }

    def claim_processing(
        self,
        *,
        owner_id=None,
        document_ids=None,
        processing_policy=None,
        expected_expired_claim=None,
        worker_scope=BackgroundWorkerScope(),
    ):
        scope, arguments = self._processing_scope(owner_id, document_ids)
        background_scope, background_arguments = worker_scope.sql()
        policy = self._processing_policy_clause(processing_policy)
        if policy and not scope:
            raise ValueError("正文处理授权策略必须限定账号和文档范围。")
        if expected_expired_claim is not None and (
            not isinstance(expected_expired_claim, dict)
            or owner_id != expected_expired_claim.get("owner_id")
            or document_ids != [expected_expired_claim.get("document_id")]
            or not expected_expired_claim.get("lease_token")
            or not isinstance(expected_expired_claim.get("_processing_source_guard"), dict)
        ):
            raise ValueError("停止任务恢复必须限定一个已领取文档及其原件守卫。")
        timestamp = time.time()
        with self.db.transaction() as conn:
            if expected_expired_claim is not None and (
                not self._processing_source_matches(conn, expected_expired_claim)
                or not conn.execute(
                    """SELECT 1 FROM processing_jobs WHERE document_id=? AND owner_id=?
                    AND status='processing' AND lease_token=? AND lease_until<=?""",
                    (document_ids[0], owner_id, expected_expired_claim["lease_token"], timestamp),
                ).fetchone()
            ):
                return None
            self._expire(
                conn,
                "processing_jobs",
                "processing",
                timestamp,
                owner_id=owner_id,
                document_ids=document_ids,
                processing_policy=processing_policy,
                worker_scope=worker_scope,
            )
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM processing_jobs WHERE status='processing' AND lease_until>?"
                    + background_scope,
                    (timestamp, *background_arguments),
                ).fetchone()[0]
                >= self.processing_workers
            ):
                return None
            # Long OCR should not occupy every slot while complete text waits
            # for embeddings (or the reverse). Prefer the missing phase only
            # when another phase is already active; preserve FIFO otherwise.
            preferred = None
            if self.processing_workers > 1:
                active_phases = {
                    item["phase"]
                    for item in conn.execute(
                        "SELECT DISTINCT phase FROM processing_jobs WHERE status='processing' AND lease_until>?"
                        + background_scope,
                        (timestamp, *background_arguments),
                    )
                }
                if active_phases == {"parse"}:
                    preferred = "embed"
                elif active_phases == {"embed"}:
                    preferred = "parse"
            order = """CASE WHEN phase='embed' AND EXISTS (
                SELECT 1 FROM ragflow_documents started
                WHERE started.document_id=processing_jobs.document_id AND started.state='indexing'
            ) THEN 0 ELSE 1 END,"""
            if preferred:
                order += "CASE WHEN phase=? THEN 0 ELSE 1 END,"
            # Keep embed admission durable and bounded independently of parser
            # capacity. Explicit transport tuning must also opt into more live
            # document leases; the default remains two.
            embedding_available = (
                conn.execute(
                    "SELECT COUNT(*) FROM processing_jobs WHERE status='processing' AND phase='embed' AND lease_until>?"
                    + background_scope,
                    (timestamp, *background_arguments),
                ).fetchone()[0]
                < self.embedding_workers
            )
            row = conn.execute(
                "SELECT * FROM processing_jobs WHERE status='queued' AND available_at<=?"
                + scope
                + background_scope
                + policy
                + " AND (phase!='embed' OR ?)"
                + """ AND EXISTS(SELECT 1 FROM documents owned_source
                    WHERE owned_source.id=processing_jobs.document_id
                        AND owned_source.owner_id=processing_jobs.owner_id AND owned_source.role='primary')"""
                + """ AND (phase!='parse' OR NOT EXISTS (
                    SELECT 1 FROM documents candidate
                    JOIN documents busy_document INDEXED BY documents_source
                        ON busy_document.owner_id=candidate.owner_id
                        AND busy_document.source_url IS candidate.source_url
                        AND busy_document.content_hash=candidate.content_hash
                        AND busy_document.version=candidate.version
                    JOIN processing_jobs busy_job ON busy_job.document_id=busy_document.id
                    WHERE candidate.id=processing_jobs.document_id
                        AND candidate.owner_id=processing_jobs.owner_id
                        AND busy_document.id!=candidate.id
                        AND busy_job.owner_id=candidate.owner_id
                        AND busy_job.status='processing' AND busy_job.phase='parse'
                        AND busy_job.lease_until>?
                ))"""
                + " ORDER BY "
                + order
                + "updated_at,document_id LIMIT 1",
                (
                    timestamp,
                    *arguments,
                    *background_arguments,
                    embedding_available,
                    timestamp,
                    *((preferred,) if preferred else ()),
                ),
            ).fetchone()
            if not row:
                return None
            source = conn.execute(
                "SELECT " + ",".join(_PROCESSING_SOURCE_FIELDS) + " FROM documents WHERE id=?",
                (row["document_id"],),
            ).fetchone()
            token = uid()
            conn.execute(
                "UPDATE processing_jobs SET status='processing',attempts=attempts+1,lease_token=?,lease_until=? WHERE document_id=?",
                (token, timestamp + LEASE_SECONDS, row["document_id"]),
            )
            claim = dict(row) | {"lease_token": token, "attempts": row["attempts"] + 1}
            claim["_processing_source_guard"] = dict(source)
            if processing_policy is not None:
                claim.update(
                    _processing_policy=processing_policy,
                    _processing_document_ids=tuple(document_ids),
                )
            return claim

    @staticmethod
    def _processing_source_matches(conn, claim):
        guard = claim.get("_processing_source_guard")
        if guard is None:
            return True  # Existing durable claims predate this optional in-memory fence.
        if (
            not isinstance(guard, dict)
            or set(guard) != set(_PROCESSING_SOURCE_FIELDS)
            or guard.get("owner_id") != claim.get("owner_id")
            or guard.get("role") != "primary"
        ):
            return False
        row = conn.execute(
            "SELECT " + ",".join(_PROCESSING_SOURCE_FIELDS) + " FROM documents WHERE id=?",
            (claim["document_id"],),
        ).fetchone()
        return row is not None and all(row[field] == guard[field] for field in _PROCESSING_SOURCE_FIELDS)

    def recover_expired_processing_claim(self, claim):
        """After our future has stopped, defer only its unchanged expired token.

        Reuse scoped queue expiry/claim and failure handling. The transaction
        compares the old token and immutable source before issuing a new token;
        failure handling compares that new token/source again before releasing.
        No page attempt, transcription, import guard or legal metadata changes.
        """
        from .parsing import ProcessingDeferred

        recovered = self.claim_processing(
            owner_id=claim["owner_id"],
            document_ids=[claim["document_id"]],
            processing_policy=claim.get("_processing_policy"),
            expected_expired_claim=claim,
        )
        if recovered is None:
            return False
        self._failure(recovered, ProcessingDeferred("工作进程已停止，保存的检查点稍后继续"), processing=True)
        with closing(self.db.connect()) as conn:
            job = conn.execute(
                "SELECT status,attempts,lease_token,lease_until FROM processing_jobs WHERE document_id=?",
                (claim["document_id"],),
            ).fetchone()
            return bool(
                job
                and job["status"] == "queued"
                and job["attempts"] == 0
                and job["lease_token"] is None
                and job["lease_until"] is None
            )

    def _expire(
        self,
        conn,
        table,
        status,
        timestamp,
        *,
        owner_id=None,
        document_ids=None,
        processing_policy=None,
        worker_scope=BackgroundWorkerScope(),
    ):
        # Table names come exclusively from the two internal queue implementations.
        scope, arguments = self._processing_scope(owner_id, document_ids)
        background_scope, background_arguments = worker_scope.sql()
        if scope and table != "processing_jobs":
            raise ValueError("限定范围只适用于正文处理任务。")
        policy = self._processing_policy_clause(processing_policy)
        if policy and (not scope or table != "processing_jobs"):
            raise ValueError("正文处理授权策略必须限定账号和文档范围。")
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE status=? AND lease_until<=?"
            + scope
            + background_scope
            + policy
            + " LIMIT 32",
            (status, timestamp, *arguments, *background_arguments),
        ).fetchall()
        key = "id" if table == "collection_items" else "document_id"
        for row in rows:
            exhausted = row["attempts"] >= MAX_ATTEMPTS
            conn.execute(
                f"UPDATE {table} SET status=?,lease_token=NULL,lease_until=NULL,error=? WHERE {key}=?",
                (
                    "failed" if exhausted else "queued",
                    "工作进程中断，重试次数已用尽" if exhausted else row["error"],
                    row[key],
                ),
            )
            if exhausted:
                from .collection_recovery import Decision

                decision = (
                    self.recovery.processing_decision(conn, dict(row), interrupted=True)
                    if table == "processing_jobs"
                    else Decision("worker_interrupted", True)
                )
                self.recovery.record_failure(
                    conn,
                    dict(row),
                    processing=table == "processing_jobs",
                    decision=decision,
                    exhausted=True,
                    error_type="expired_lease",
                )
                if table == "collection_items":
                    self.service.checklists.release_candidate(conn, dict(row))
                    self._refresh(conn, row["run_id"])
                else:
                    conn.execute(
                        "UPDATE documents SET error=?,status=CASE WHEN status='processing' THEN 'failed' ELSE status END WHERE id=?",
                        ("处理进程反复中断，可重试处理", row[key]),
                    )
                    self._refresh_document(conn, row[key])

    def _valid_claim(self, conn, claim, processing=False):
        table, key, state = (
            ("processing_jobs", "document_id", "processing")
            if processing
            else ("collection_items", "id", "fetching")
        )
        policy = self._processing_policy_clause(claim.get("_processing_policy")) if processing else ""
        if policy and claim["document_id"] not in claim.get("_processing_document_ids", ()):
            return False
        valid = (
            conn.execute(
                f"SELECT 1 FROM {table} WHERE {key}=? AND status=? AND lease_token=? AND lease_until>?"
                + (" AND owner_id=?" if policy else "")
                + policy,
                (
                    claim[key],
                    state,
                    claim["lease_token"],
                    time.time(),
                    *((claim["owner_id"],) if policy else ()),
                ),
            ).fetchone()
            is not None
        )
        return valid and (not processing or self._processing_source_matches(conn, claim))

    def _release_disallowed_policy_claim(self, conn, claim):
        """Release only our live lease; do not modify the now-ineligible document."""
        policy = self._processing_policy_clause(claim.get("_processing_policy"))
        if not policy:
            return
        row = conn.execute(
            """SELECT 1 FROM processing_jobs WHERE document_id=? AND owner_id=?
            AND status='processing' AND lease_token=? AND lease_until>?
            AND EXISTS(SELECT 1 FROM documents d
                WHERE d.id=processing_jobs.document_id AND d.owner_id=processing_jobs.owner_id)""",
            (claim["document_id"], claim["owner_id"], claim["lease_token"], time.time()),
        ).fetchone()
        if row is None or self._valid_claim(conn, claim, True):
            return
        conn.execute(
            """UPDATE processing_jobs SET status='queued',attempts=MAX(attempts-1,0),
            lease_token=NULL,lease_until=NULL,updated_at=? WHERE document_id=? AND lease_token=?""",
            (now(), claim["document_id"], claim["lease_token"]),
        )

    def heartbeat(self, claims):
        with self.db.transaction() as conn:
            timestamp = time.time()
            for processing, claim in claims:
                if (
                    processing
                    and claim.get("_processing_policy")
                    and not self._valid_claim(conn, claim, True)
                ):
                    continue
                table, key = ("processing_jobs", "document_id") if processing else ("collection_items", "id")
                conn.execute(
                    f"UPDATE {table} SET lease_until=? WHERE {key}=? AND lease_token=? AND lease_until>?",
                    (timestamp + LEASE_SECONDS, claim[key], claim["lease_token"], timestamp),
                )

    @contextmanager
    def network_slot(self, host, deadline):
        """Physical request limits survive cancelled claims and apply to redirect destinations."""
        token = uid()
        acquired = False
        try:
            while not acquired:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    from .ingestion import DownloadError

                    raise DownloadError("等待站点并发额度超时", retryable=True)
                with self.db.transaction() as conn:
                    timestamp = time.time()
                    conn.execute("DELETE FROM collection_network_leases WHERE expires_at<=?", (timestamp,))
                    total = conn.execute("SELECT COUNT(*) FROM collection_network_leases").fetchone()[0]
                    count = conn.execute(
                        "SELECT COUNT(*) FROM collection_network_leases WHERE host=?", (host,)
                    ).fetchone()[0]
                    cooldown = conn.execute(
                        "SELECT available_at FROM collection_host_cooldowns WHERE host=?", (host,)
                    ).fetchone()
                    if cooldown and cooldown[0] > timestamp:
                        # Redirect targets are unknown when the original URL is
                        # claimed. Yield this worker to other hosts immediately.
                        raise _HostCooldownDeferred(host, cooldown[0] - timestamp)
                    if total < self.download_workers and count < self.host_workers:
                        # Socket watchdog closes at the absolute download deadline, before this lease expires.
                        conn.execute(
                            "INSERT INTO collection_network_leases VALUES(?,?,?)",
                            (token, host, timestamp + remaining + 5),
                        )
                        acquired = True
                if not acquired:
                    time.sleep(min(0.1, remaining))
            yield
        finally:
            if acquired:
                with self.db.transaction() as conn:
                    conn.execute("DELETE FROM collection_network_leases WHERE id=?", (token,))

    def fetch_item(self, claim):
        from .discovery import request_parameters
        from .ingestion import discover_attachments

        if claim is None:
            return
        try:
            with closing(self.db.connect()) as conn:
                if not self._valid_claim(conn, claim):
                    return
                cache = conn.execute(
                    """SELECT c.*,d.storage_path,d.content_hash,d.size,d.filename,d.mime_type
                    FROM collection_http_cache c JOIN documents d ON d.id=c.document_id WHERE c.owner_id=? AND c.url=?""",
                    (claim["owner_id"], claim["url"]),
                ).fetchone()
            if cache and not Path(cache["storage_path"]).is_file():
                cache = None
            plan = json.loads(claim["plan"])
            request_options = (
                request_parameters(claim["url"], plan["checklist"]["definition"])
                if plan.get("checklist")
                else {}
            )
            validators = {key: cache[key] for key in ("etag", "last_modified")} if cache else None
            if request_options.get("json_body") is not None:
                validators = None
            discovery = claim.get("purpose") in {"catalog", "detail"}
            timeout_seconds = 90 if discovery else plan.get("download_timeout_seconds", 90)
            if timeout_seconds > 90:
                with self.db.transaction() as conn:
                    if not self._valid_claim(conn, claim):
                        return
                    conn.execute(
                        "UPDATE collection_items SET lease_until=? WHERE id=? AND lease_token=?",
                        (time.time() + timeout_seconds + 30, claim["id"], claim["lease_token"]),
                    )
            fetched = self.transfers.download(
                claim,
                required_host=claim["host"] if claim["level"] == "attachment" else None,
                validators=validators,
                discovery=discovery,
                timeout_seconds=timeout_seconds,
                options={
                    "accept_compressed_html": claim.get("purpose") in {"catalog", "detail"},
                    "max_bytes": plan.get("max_original_bytes", 20 * 1024 * 1024),
                    **request_options,
                },
            )
            fetched["request_evidence"] = {
                "method": fetched.get("request_method", "GET"),
                "url": fetched["url"],
            }
            if "json_body" in request_options:
                fetched["request_evidence"]["json_body"] = request_options["json_body"]
            if fetched.get("not_modified"):
                if not cache:
                    raise ValueError("来源返回 304，但本地没有可复用原件")
                if fetched["url"] != cache["final_url"]:
                    raise ValueError("来源跳转地址变更，不能复用旧响应；请重新抓取")
                with Path(cache["storage_path"]).open("rb") as file:
                    blob = self.service.archive.blobs.put(
                        claim["owner_id"], iter(lambda: file.read(65536), b"")
                    )
                if blob.digest != cache["content_hash"]:
                    raise ValueError("本地原件校验失败，请重新抓取")
                fetched = dict(cache) | fetched | {"blob": blob}
            attachments = []
            blob = fetched["blob"]
            checklist_result = (
                self.service.checklists.inspect(plan, fetched) if plan.get("checklist") else None
            )
            kind_hint = plan.get("checklist", {}).get("definition", {}).get("item", {}).get("kind")
            if plan.get("checklist", {}).get("kind") == "discovery":
                with self.db.transaction() as conn:
                    if self._valid_claim(conn, claim):
                        self.service.checklists.save_page(conn, claim, fetched, plan, checklist_result)
                        self.recovery.completed(conn, claim, processing=False)
                return
            if (
                plan["discover_attachments"]
                and claim["level"] != "attachment"
                and fetched["mime_type"] == "text/html"
                and blob.size <= 4_000_000
            ):
                attachments = discover_attachments(
                    fetched["url"], blob.path.read_bytes(), fetched["mime_type"]
                )
            with self.db.transaction() as conn:
                if not self._valid_claim(conn, claim):
                    return
                self.service.checklists.verify_wto_candidate_origin(conn, claim, plan, checklist_result)
                doc = self.service.archive.store_original(
                    conn,
                    claim["owner_id"],
                    blob,
                    fetched["filename"],
                    fetched["mime_type"],
                    claim["url"],
                    collection=plan["collection"],
                    level=claim["level"],
                    tags=plan["tags"],
                    identity_key=claim["identity_key"],
                    parent_id=claim["parent_document_id"],
                    primary_document_ids=plan["primary_document_ids"]
                    if claim["level"] == "secondary" and not plan.get("checklist", {}).get("defer_relations")
                    else [],
                    kind_hint="law" if kind_hint == "treaty" else kind_hint,
                )
                conn.execute(
                    """UPDATE collection_items SET status='stored',document_id=?,final_url=?,fetched_at=?,error=NULL,lease_token=NULL,lease_until=NULL WHERE id=?""",
                    (doc["id"], fetched["url"], now(), claim["id"]),
                )
                if plan.get("checklist"):
                    self.service.checklists.stored_candidate(conn, claim, doc)
                conn.execute(
                    """INSERT INTO collection_http_cache VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,url) DO UPDATE SET
                    document_id=excluded.document_id,etag=excluded.etag,last_modified=excluded.last_modified,final_url=excluded.final_url""",
                    (
                        claim["owner_id"],
                        claim["url"],
                        doc["id"],
                        fetched.get("etag"),
                        fetched.get("last_modified"),
                        fetched["url"],
                    ),
                )
                count = conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE run_id=?", (claim["run_id"],)
                ).fetchone()[0]
                global_pending = conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE status IN ('queued','fetching')"
                ).fetchone()[0]
                owner_pending = conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE owner_id=? AND status IN ('queued','fetching')",
                    (claim["owner_id"],),
                ).fetchone()[0]
                for index, url in enumerate(attachments):
                    if (
                        count + index >= plan["max_items"]
                        or global_pending + index >= 2000
                        or owner_pending + index >= 1000
                    ):
                        conn.execute(
                            "UPDATE collection_runs SET error=? WHERE id=?",
                            ("发现附件超过本次或队列上限，请补充网址后另建任务", claim["run_id"]),
                        )
                        break
                    self._insert_item(
                        conn, claim["run_id"], claim["owner_id"], url, "attachment", parent=doc["id"]
                    )
                self._refresh(conn, claim["run_id"])
                self.recovery.completed(conn, claim, processing=False)
        except Exception as error:
            self._failure(claim, error, processing=False)

    def process_item(self, claim):
        from .parsing import parse_bounded
        from .ingestion_quality import (
            quality_warning,
            text_complete,
        )
        from .mineru import supports

        if claim is None:
            return
        try:
            with closing(self.db.connect()) as conn:
                if not self._valid_claim(conn, claim, True):
                    with self.db.transaction() as write:
                        self._release_disallowed_policy_claim(write, claim)
                    return
                doc = self.service.own_document(conn, claim["owner_id"], claim["document_id"])
                should_index = self.service.archive.can_index(conn, doc["id"])
            manifest = json.loads(doc["manifest"])
            provenance_keys = (
                "collection_import",
                "collection_evidence",
                "gazette_publication",
                "native_page_recovery",
            )
            stored_provenance = {key: deepcopy(manifest[key]) for key in provenance_keys if key in manifest}
            stored_html_source = deepcopy(manifest.get("html_source"))

            def preserve_import_origin(result):
                # Keep stored source evidence and already mapped CAS paths when
                # parsers rebuild derived text. Do not accept parser-created
                # provenance or recompute an import guard: neither grants an
                # older snapshot permission to replace paid OCR or vectors.
                for key in provenance_keys:
                    if key in stored_provenance:
                        result[key] = deepcopy(stored_provenance[key])
                    else:
                        result.pop(key, None)
                previous_html = stored_html_source
                if (
                    isinstance(previous_html, dict)
                    and previous_html.get("source_hash") == result.get("source_hash")
                    and previous_html.get("linked_images")
                    and isinstance(result.get("html_source"), dict)
                    and not result["html_source"].get("linked_images")
                ):
                    # A fresh structural parse must not discard verified image
                    # bindings or OCR checkpoints collected after the original
                    # HTML was stored. Validate each binding against the new
                    # source map before carrying it forward, then rebuild the
                    # derived text from the preserved checkpoint.
                    from .html_linked_asset_ocr import validate_binding

                    linked_images = deepcopy(previous_html["linked_images"])
                    for value in linked_images:
                        validate_binding(result, value)
                    result["html_source"]["linked_images"] = linked_images
                    from .native_asset_ocr import rebuild_native_asset_text_bounded

                    result = rebuild_native_asset_text_bounded(result)
                return result

            fingerprint = self.service.embedding_fingerprint()
            if claim["phase"] == "parse":
                if supports(doc["filename"], doc["mime_type"]):
                    manifest = preserve_import_origin(self.parser.step(doc, claim, manifest))
                else:
                    content = Path(doc["storage_path"]).read_bytes()
                    if hashlib.sha256(content).hexdigest() != doc["content_hash"]:
                        raise ValueError("原件完整性校验失败")
                    manifest = preserve_import_origin(
                        parse_bounded(doc["filename"], content, doc["mime_type"])
                    )
                    if manifest.get("parser") == "html-basic" and manifest.get("html_source", {}).get(
                        "linked_images"
                    ):
                        from .html_linked_asset_ocr import read_linked_images
                        from .native_asset_ocr import apply_asset_checkpoint, ocr_native_assets_bounded

                        image_contents = read_linked_images(
                            self.db.data_dir,
                            claim["owner_id"],
                            manifest,
                            pending_only=True,
                            max_assets=4,
                        )

                        def checkpoint_asset(value):
                            with self.db.transaction() as conn:
                                if not self._valid_claim(conn, claim, True):
                                    raise RuntimeError("图片识别租约已失效，停止提交检查点。")
                                current = json.loads(
                                    conn.execute(
                                        "SELECT manifest FROM documents WHERE id=?", (doc["id"],)
                                    ).fetchone()[0]
                                )
                                if current.get("parser_version") != manifest.get("parser_version"):
                                    if dump(current) != doc["manifest"]:
                                        raise ValueError("HTML 原文映射在识别期间已变化，未覆盖当前资料。")
                                    # Checkpoints must use the same structural
                                    # map that produced the OCR identity. Older
                                    # stored HTML maps are promoted atomically
                                    # with the first verified OCR checkpoint.
                                    current = deepcopy(manifest)
                                apply_asset_checkpoint(current, value)
                                conn.execute(
                                    "UPDATE documents SET manifest=? WHERE id=?",
                                    (dump(current), doc["id"]),
                                )

                        def store_rendered_image(rendered):
                            return self._persist_ocr_image(claim, rendered)

                        manifest = ocr_native_assets_bounded(
                            content,
                            manifest,
                            on_asset=checkpoint_asset,
                            on_image=store_rendered_image,
                            max_assets=4,
                            image_contents=image_contents,
                        )
            elif should_index and fingerprint and text_complete(manifest):
                self.service.ragflow.step(doc, claim)
            manifest = preserve_import_origin(manifest)
            warning = "；".join(str(item) for item in manifest.get("warnings", []))[:500] or None
            warning = quality_warning(manifest) or warning
            with self.db.transaction() as conn:
                if not self._valid_claim(conn, claim, True):
                    self._release_disallowed_policy_claim(conn, claim)
                    return
                # Classification may change while parsing or calling the provider. Decide
                # follow-up work from current state in the same transaction as completion.
                current_fingerprint = self.service.embedding_fingerprint()
                if claim["phase"] == "parse":
                    conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
                    self.service.index.sync_document(conn, doc["id"], manifest)
                elif conn.execute(
                    "SELECT 1 FROM retrieval_dirty WHERE document_id=?", (doc["id"],)
                ).fetchone():
                    self.service.index.sync_document(conn, doc["id"])
                indexable = self.service.archive.can_index(conn, doc["id"])
                needs_index = (
                    indexable
                    and current_fingerprint
                    and text_complete(manifest)
                    and self.service.index.pending(conn, doc["id"], current_fingerprint, limit=1)
                )
                needs_parse = manifest.get("remote_parse", {}).get("state") in {"planned", "pending"}
                if claim["phase"] == "parse":
                    from .native_asset_ocr import pending_assets
                    from .vision import vision_ocr_configured

                    # A bounded native-image batch can leave more verified
                    # images to process. Queue another parse lease only when
                    # the recognizer is configured; otherwise keep the exact
                    # pending state visible instead of spinning forever.
                    needs_parse = needs_parse or (vision_ocr_configured() and bool(pending_assets(manifest)))
                deferred = needs_parse
                index_delay = (
                    self.service.ragflow.retry_delay(conn, doc["id"], current_fingerprint)
                    if needs_index
                    else 0
                )
                next_phase = "parse" if needs_parse else ("embed" if needs_index else None)
                incomplete = not text_complete(manifest) and not next_phase
                conn.execute(
                    "UPDATE documents SET status=?,error=? WHERE id=?",
                    (
                        "processing" if needs_parse else ("failed" if incomplete else "ready"),
                        warning,
                        doc["id"],
                    ),
                )
                if not needs_index and current_fingerprint and indexable and text_complete(manifest):
                    conn.execute(
                        "UPDATE documents SET manifest=json_set(manifest,'$.embedding_fingerprint',?) WHERE id=?",
                        (current_fingerprint, doc["id"]),
                    )
                    # Only the model marker changed; the text checkpoint is already current.
                    conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (doc["id"],))
                conn.execute(
                    "UPDATE processing_jobs SET phase=?,status=?,attempts=0,available_at=?,lease_token=NULL,lease_until=NULL,error=?,updated_at=? WHERE document_id=?",
                    (
                        next_phase or claim["phase"],
                        "queued" if next_phase else ("failed" if incomplete else "completed"),
                        time.time()
                        + max(
                            manifest.get("quality", {}).get("retry_after_seconds", 5),
                            manifest.get("remote_parse", {}).get("retry_after_seconds", 5),
                        )
                        if deferred
                        else time.time() + index_delay
                        if index_delay
                        else 0,
                        warning if incomplete else None,
                        now(),
                        doc["id"],
                    ),
                )
                self._refresh_document(conn, doc["id"])
                if incomplete:
                    self.recovery.record_failure(
                        conn,
                        claim,
                        processing=True,
                        decision=self.recovery.processing_decision(conn, claim),
                        exhausted=True,
                        error_type="incomplete_checkpoint",
                    )
                elif next_phase != claim["phase"]:
                    self.recovery.completed(conn, claim, processing=True)
        except Exception as error:
            self._failure(claim, error, processing=True)

    def _failure(self, claim, error, *, processing):
        from .parsing import ProcessingDeferred
        from .transfers import TransferPending
        from .service import AppError
        from .ragflow import RAGFlowError

        storage_wait = _storage_busy(error)
        # Unexpected exceptions/paths/provider response bodies never become public error text.
        safe = (
            str(error)[:450]
            if isinstance(error, (ValueError, AppError))
            else "处理失败，请检查运行日志后重试"
        )
        retryable = getattr(error, "retryable", not isinstance(error, (ValueError, AppError)))
        # Model/network failures may be transient; deterministic parse failures are not.
        if processing and claim["phase"] == "embed" and not storage_wait:
            retryable = getattr(error, "retryable", True)
            safe = str(error) if isinstance(error, RAGFlowError) else "向量服务暂不可用，原件和正文已保留"
        if storage_wait:
            safe = "本地资料存储繁忙，任务稍后继续处理"
        transfer_progress = not processing and isinstance(error, TransferPending)
        provider_wait = processing and isinstance(error, RAGFlowError) and error.retryable
        host_wait = not processing and isinstance(error, _HostCooldownDeferred)
        deferred = (
            (processing and isinstance(error, ProcessingDeferred))
            or transfer_progress
            or provider_wait
            or host_wait
            or storage_wait
        )
        exhausted = not deferred and (claim["attempts"] >= MAX_ATTEMPTS or not retryable)
        # Bound our own exponential backoff, but never shorten a server's
        # Retry-After. Long waits remain queue timestamps, not worker sleeps.
        delay = max(
            min(300, 2 ** claim["attempts"] + random.random()),
            getattr(error, "retry_after", None) or 0,
        )
        if deferred:
            delay = (
                max(1, error.retry_after) if provider_wait or host_wait else (0 if transfer_progress else 5)
            )
        table, key = ("processing_jobs", "document_id") if processing else ("collection_items", "id")
        with self.db.transaction() as conn:
            if not self._valid_claim(conn, claim, processing):
                if processing:
                    self._release_disallowed_policy_claim(conn, claim)
                return
            from .collection_recovery import Decision

            decision = (
                Decision("deferred", True)
                if deferred
                else self.recovery.processing_decision(conn, claim, error=error, retryable=retryable)
                if processing
                else Decision("transient_fetch", True)
                if retryable
                else Decision("permanent_fetch")
            )
            if not deferred and getattr(error, "request_outcome_unknown", False):
                exhausted = True
                decision = Decision("paid_result_unknown")
            conn.execute(
                f"UPDATE {table} SET status=?,available_at=?,error=?,lease_token=NULL,lease_until=NULL WHERE {key}=?",
                (
                    "failed" if exhausted else "queued",
                    time.time() + delay,
                    None if transfer_progress else safe,
                    claim[key],
                ),
            )
            if host_wait:
                # Undo only this claim's increment; earlier network failures
                # still count toward the finite retry budget.
                conn.execute(f"UPDATE {table} SET attempts=MAX(attempts-1,0) WHERE {key}=?", (claim[key],))
            elif deferred:
                conn.execute(f"UPDATE {table} SET attempts=0 WHERE {key}=?", (claim[key],))
            if processing:
                conn.execute(
                    "UPDATE documents SET error=?,status=CASE WHEN ? AND status='processing' THEN 'failed' ELSE status END WHERE id=?",
                    (safe, exhausted, claim[key]),
                )
                self._refresh_document(conn, claim[key])
            else:
                if exhausted:
                    self.service.checklists.release_candidate(conn, claim)
                if getattr(error, "retry_after", None):
                    conn.execute(
                        "INSERT INTO collection_host_cooldowns VALUES(?,?) ON CONFLICT(host) DO UPDATE SET available_at=MAX(available_at,excluded.available_at)",
                        (getattr(error, "response_host", None) or claim["host"], time.time() + delay),
                    )
                self._refresh(conn, claim["run_id"])
            if not deferred:
                self.recovery.record_failure(
                    conn,
                    claim,
                    processing=processing,
                    decision=decision,
                    exhausted=exhausted,
                    ready_at=time.time() + delay,
                    error_type=type(error).__name__,
                )
        logger.info(
            "collection_item_deferred" if deferred else "collection_item_failed",
            extra={"queue": table, "item_id": claim[key], "error_type": type(error).__name__},
        )

    def _refresh_document(self, conn, document_id):
        for row in conn.execute(
            "SELECT DISTINCT run_id FROM collection_items WHERE document_id=?", (document_id,)
        ):
            self._refresh(conn, row[0])

    def _refresh(self, conn, run_id):
        row = conn.execute("SELECT * FROM collection_runs WHERE id=?", (run_id,)).fetchone()
        if not row or row["status"] == "cancelled":
            return
        summary = self.run_dict(conn, row)
        status = (
            "running"
            if summary["pending"] or summary["processing"]
            else (
                "partial"
                if summary["failed"] or summary["processing_failed"] or row["error"]
                else "completed"
            )
        )
        conn.execute(
            "UPDATE collection_runs SET status=?,updated_at=? WHERE id=?",
            (status, now(), run_id),
        )
        self._update_source(conn, run_id)

    def _update_source(self, conn, run_id):
        row = conn.execute("SELECT * FROM collection_runs WHERE id=?", (run_id,)).fetchone()
        latest = conn.execute(
            "SELECT id FROM collection_runs WHERE source_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (row["source_id"],),
        ).fetchone()
        if latest[0] != run_id:
            return
        status = (
            "processing"
            if row["status"] in ("queued", "running")
            else "ready"
            if row["status"] == "completed"
            else "failed"
        )
        error = row["error"] or ("部分文件未完成，请查看采集任务" if status == "failed" else None)
        conn.execute(
            "UPDATE sources SET status=?,last_error=?,last_fetched_at=COALESCE(?,last_fetched_at),updated_at=? WHERE id=?",
            (status, error, now() if status == "ready" else None, now(), row["source_id"]),
        )


class CollectionWorkers:
    """Fixed-size executors with at most one claimed future per available worker."""

    def __init__(
        self,
        collection,
        *,
        processing_scope=None,
        processing_policy=None,
        fetch_enabled=True,
        worker_scope=BackgroundWorkerScope(),
    ):
        self.collection = collection
        worker_scope.sql()
        self.worker_scope = worker_scope
        collection.worker_health["shared_library_workers"] = worker_scope.shared_library_workers
        if processing_scope is not None:
            if not isinstance(processing_scope, (tuple, list)) or len(processing_scope) != 2:
                raise ValueError("处理范围需要账号与文档列表。")
            collection._processing_scope(*processing_scope)
            processing_scope = (processing_scope[0], tuple(processing_scope[1]))
        collection._processing_policy_clause(processing_policy)
        if processing_policy is not None and processing_scope is None:
            raise ValueError("正文处理授权策略必须限定账号和文档范围。")
        self.processing_scope = processing_scope
        self.processing_policy = processing_policy
        self.fetch_enabled = fetch_enabled
        self.stop_event = Event()
        self.thread = None
        # Scoped campaigns have at most 20 IDs. Retain only the latest token per
        # ID; a server-wide worker does not accumulate an unbounded history.
        self.owned_processing_claims = {}

    def start(self):
        if self.thread is None:
            self.thread = Thread(target=self._run, name="tea-collection-queues", daemon=True)
            self.thread.start()
        return self

    def _run(self):
        downloads = ThreadPoolExecutor(
            max_workers=self.collection.download_workers, thread_name_prefix="tea-download"
        )
        processing = ThreadPoolExecutor(
            max_workers=self.collection.processing_workers, thread_name_prefix="tea-parse"
        )
        active = {}
        heartbeat_at = 0
        try:
            while not self.stop_event.is_set() or active:
                try:
                    for future in list(active):
                        if future.done():
                            active.pop(future)
                            try:
                                future.result()
                            except Exception:
                                logger.exception("Collection worker failed; its durable lease will expire")
                    if not self.stop_event.is_set():
                        for is_processing, executor, maximum in (
                            (False, downloads, self.collection.download_workers if self.fetch_enabled else 0),
                            (True, processing, self.collection.processing_workers),
                        ):
                            free = maximum - sum(kind == is_processing for kind, _ in active.values())
                            for _ in range(free):
                                if self.stop_event.is_set():
                                    break
                                claim = (
                                    self.collection.claim_processing(
                                        **self.worker_scope.kwargs(),
                                        **(
                                            {
                                                "owner_id": self.processing_scope[0],
                                                "document_ids": self.processing_scope[1],
                                                "processing_policy": self.processing_policy,
                                            }
                                            if self.processing_scope is not None
                                            else {}
                                        ),
                                    )
                                    if is_processing
                                    else self.collection.claim_fetch(**self.worker_scope.kwargs())
                                )
                                if not claim:
                                    break
                                if is_processing and self.processing_scope is not None:
                                    self.owned_processing_claims[claim["document_id"]] = deepcopy(claim)
                                future = executor.submit(
                                    self.collection.process_item
                                    if is_processing
                                    else self.collection.fetch_item,
                                    claim,
                                )
                                active[future] = (is_processing, claim)
                    if active and time.monotonic() >= heartbeat_at:
                        self.collection.heartbeat(active.values())
                        heartbeat_at = time.monotonic() + 10
                    self.collection.worker_health["status"] = (
                        "draining" if self.stop_event.is_set() else "running"
                    )
                except Exception:
                    self.collection.worker_health.update(status="recovering", last_error_at=now())
                    logger.exception("Collection coordinator cycle failed; will retry")
                    self.stop_event.wait(0.5) if not self.stop_event.is_set() else time.sleep(0.5)
                # Continue renewing active work while shutdown drains bounded operations.
                time.sleep(0.2) if self.stop_event.is_set() else self.stop_event.wait(0.2)
        finally:
            downloads.shutdown(wait=True, cancel_futures=True)
            processing.shutdown(wait=True, cancel_futures=True)
            for claim in self.owned_processing_claims.values():
                try:
                    self.collection.recover_expired_processing_claim(claim)
                except Exception:
                    logger.exception("Stopped scoped worker could not defer its expired processing claim")
            self.collection.worker_health["status"] = "stopped"

    def stop(self, timeout=5):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout)
