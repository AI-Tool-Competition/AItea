"""Durable single-node collection queues. Network/model work never owns a DB write lock."""

import hashlib
import json
import logging
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Event, Thread
from urllib.parse import urlsplit

from .db import dump, now, uid
from .models import CollectionPlan

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120
MAX_ATTEMPTS = 3


def _setting(name, default, maximum):
    value = int(os.getenv(name, str(default)))
    if not 1 <= value <= maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return value


class CollectionService:
    def __init__(self, service):
        self.service, self.db = service, service.db
        self.download_workers = _setting("TEA_COLLECT_WORKERS", 8, 16)
        self.host_workers = min(_setting("TEA_COLLECT_PER_HOST", 2, 4), self.download_workers)
        self.processing_workers = _setting("TEA_PROCESS_WORKERS", 2, 2)
        self.worker_health = {"status": "stopped", "last_error_at": None}
        self._migrate()
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=6").fetchone():
                conn.execute(
                    "CREATE TABLE collection_network_leases(id TEXT PRIMARY KEY,host TEXT NOT NULL,expires_at REAL NOT NULL)"
                )
                conn.execute("CREATE INDEX network_leases_host ON collection_network_leases(host,expires_at)")
                conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (6, now()))

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
        if (
            scheduled
            and not conn.execute(
                """SELECT 1 FROM sources s WHERE s.id=? AND s.enabled=1
            AND (s.last_attempt_at IS NULL OR julianday(s.last_attempt_at)+s.interval_hours/24.0<=julianday('now'))
            AND NOT EXISTS(SELECT 1 FROM collection_runs r WHERE r.source_id=s.id AND r.status IN ('queued','running'))""",
                (source_id,),
            ).fetchone()
        ):
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
            return {"run": run, "items": [dict(row) for row in rows], "total": run["total"]}

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
                conn.execute(
                    """UPDATE processing_jobs SET status='queued',attempts=0,available_at=0,error=NULL,updated_at=?
                    WHERE status='failed' AND document_id IN (SELECT document_id FROM collection_items WHERE run_id=?)""",
                    (now(), run_id),
                )
                status = "queued"
            conn.execute(
                "UPDATE collection_runs SET status=?,revision=revision+1,updated_at=? WHERE id=?",
                (status, now(), run_id),
            )
            self._update_source(conn, run_id)
            return self.run_dict(conn, self.own_run(conn, owner, run_id))

    def queue_processing(self, conn, document_id, owner):
        conn.execute(
            """INSERT INTO processing_jobs(document_id,owner_id,phase,status,updated_at) VALUES(?,?,'parse','queued',?)
            ON CONFLICT(document_id) DO UPDATE SET status='queued',attempts=0,available_at=0,error=NULL,updated_at=excluded.updated_at
            WHERE processing_jobs.status IN ('completed','failed')""",
            (document_id, owner, now()),
        )

    def claim_fetch(self):
        timestamp = time.time()
        with self.db.transaction() as conn:
            self._expire(conn, "collection_items", "fetching", timestamp)
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE status='fetching' AND lease_until>?",
                    (timestamp,),
                ).fetchone()[0]
                >= self.download_workers
            ):
                return None
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')"
                ).fetchone()[0]
                >= 200
            ):
                return None  # Disk/parse queue backpressure, not an unbounded in-memory future queue.
            row = conn.execute(
                """SELECT i.*,r.plan FROM collection_items i JOIN collection_runs r ON r.id=i.run_id
                LEFT JOIN collection_host_cooldowns h ON h.host=i.host
                WHERE i.status='queued' AND i.available_at<=? AND r.status IN ('queued','running')
                AND COALESCE(h.available_at,0)<=? AND (SELECT COUNT(*) FROM collection_items a
                    WHERE a.host=i.host AND a.status='fetching' AND a.lease_until>?)<?
                ORDER BY i.created_at,i.id LIMIT 1""",
                (timestamp, timestamp, timestamp, self.host_workers),
            ).fetchone()
            if not row:
                return None
            token = uid()
            conn.execute(
                "UPDATE collection_items SET status='fetching',attempts=attempts+1,lease_token=?,lease_until=? WHERE id=?",
                (token, timestamp + LEASE_SECONDS, row["id"]),
            )
            conn.execute(
                "UPDATE collection_runs SET status='running',updated_at=? WHERE id=? AND status='queued'",
                (now(), row["run_id"]),
            )
            return dict(row) | {"lease_token": token, "attempts": row["attempts"] + 1}

    def claim_processing(self):
        timestamp = time.time()
        with self.db.transaction() as conn:
            self._expire(conn, "processing_jobs", "processing", timestamp)
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM processing_jobs WHERE status='processing' AND lease_until>?",
                    (timestamp,),
                ).fetchone()[0]
                >= self.processing_workers
            ):
                return None
            row = conn.execute(
                "SELECT * FROM processing_jobs WHERE status='queued' AND available_at<=? ORDER BY updated_at,document_id LIMIT 1",
                (timestamp,),
            ).fetchone()
            if not row:
                return None
            token = uid()
            conn.execute(
                "UPDATE processing_jobs SET status='processing',attempts=attempts+1,lease_token=?,lease_until=? WHERE document_id=?",
                (token, timestamp + LEASE_SECONDS, row["document_id"]),
            )
            return dict(row) | {"lease_token": token, "attempts": row["attempts"] + 1}

    def _expire(self, conn, table, status, timestamp):
        # Table names come exclusively from the two internal queue implementations.
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE status=? AND lease_until<=? LIMIT 32", (status, timestamp)
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
        return (
            conn.execute(
                f"SELECT 1 FROM {table} WHERE {key}=? AND status=? AND lease_token=? AND lease_until>?",
                (claim[key], state, claim["lease_token"], time.time()),
            ).fetchone()
            is not None
        )

    def heartbeat(self, claims):
        with self.db.transaction() as conn:
            timestamp = time.time()
            for processing, claim in claims:
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
                    if (
                        total < self.download_workers
                        and count < self.host_workers
                        and (not cooldown or cooldown[0] <= timestamp)
                    ):
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
        from .ingestion import _download, discover_attachments

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
            validators = {key: cache[key] for key in ("etag", "last_modified")} if cache else None
            deadline = time.monotonic() + 90
            fetched = _download(
                claim["url"],
                deadline=deadline,
                required_host=claim["host"] if claim["level"] == "attachment" else None,
                consume=lambda chunks: self.service.archive.blobs.put(claim["owner_id"], chunks),
                validators=validators,
                connection_slot=lambda host: self.network_slot(host, deadline),
            )
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
            plan = json.loads(claim["plan"])
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
                    if claim["level"] == "secondary"
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
        except Exception as error:
            self._failure(claim, error, processing=False)

    def process_item(self, claim):
        from .ingestion import embed_texts
        from .parsing import parse_bounded

        if claim is None:
            return
        try:
            with closing(self.db.connect()) as conn:
                if not self._valid_claim(conn, claim, True):
                    return
                doc = self.service.own_document(conn, claim["owner_id"], claim["document_id"])
                should_index = self.service.archive.can_index(conn, doc["id"])
            manifest = json.loads(doc["manifest"])
            fingerprint = self.service.embedding_fingerprint()
            if claim["phase"] == "parse":
                if not manifest.get("elements"):
                    content = Path(doc["storage_path"]).read_bytes()
                    if hashlib.sha256(content).hexdigest() != doc["content_hash"]:
                        raise ValueError("原件完整性校验失败")
                    manifest = parse_bounded(
                        doc["filename"], content, doc["mime_type"], parser=os.getenv("TEA_PARSER", "basic")
                    )
                    if not manifest.get("elements"):
                        raise ValueError("文件没有可提取的正文；扫描件需要 OCR")
            elif should_index and fingerprint:
                if manifest.get("embedding_fingerprint") != fingerprint:
                    for chunk in manifest.get("chunks", []):
                        chunk.pop("embedding", None)
                    manifest["embedding_fingerprint"] = fingerprint
                batch = [chunk for chunk in manifest.get("chunks", []) if not chunk.get("embedding")][:64]
                if batch:
                    vectors = embed_texts([chunk["text"] for chunk in batch])
                    if vectors is None:
                        raise ValueError("向量配置已变更，请重试处理")
                    for chunk, vector in zip(batch, vectors, strict=True):
                        chunk["embedding"] = vector
            warning = "；".join(str(item) for item in manifest.get("warnings", []))[:500] or None
            with self.db.transaction() as conn:
                if not self._valid_claim(conn, claim, True):
                    return
                # Classification may change while parsing or calling the provider. Decide
                # follow-up work from current state in the same transaction as completion.
                current_fingerprint = self.service.embedding_fingerprint()
                needs_index = (
                    self.service.archive.can_index(conn, doc["id"])
                    and current_fingerprint
                    and manifest.get("chunks")
                    and (
                        manifest.get("embedding_fingerprint") != current_fingerprint
                        or any(not chunk.get("embedding") for chunk in manifest["chunks"])
                    )
                )
                next_phase = "embed" if needs_index else None
                conn.execute(
                    "UPDATE documents SET manifest=?,status='ready',error=? WHERE id=?",
                    (dump(manifest), warning, doc["id"]),
                )
                conn.execute(
                    "UPDATE processing_jobs SET phase=?,status=?,attempts=0,available_at=0,lease_token=NULL,lease_until=NULL,error=NULL,updated_at=? WHERE document_id=?",
                    (next_phase or claim["phase"], "queued" if next_phase else "completed", now(), doc["id"]),
                )
                self._refresh_document(conn, doc["id"])
        except Exception as error:
            self._failure(claim, error, processing=True)

    def _failure(self, claim, error, *, processing):
        from .service import AppError

        # Unexpected exceptions/paths/provider response bodies never become public error text.
        safe = (
            str(error)[:450]
            if isinstance(error, (ValueError, AppError))
            else "处理失败，请检查运行日志后重试"
        )
        retryable = getattr(error, "retryable", not isinstance(error, (ValueError, AppError)))
        # Model/network failures may be transient; deterministic parse failures are not.
        if processing and claim["phase"] == "embed":
            retryable = True
            safe = "向量服务暂不可用，原件和正文已保留"
        exhausted = claim["attempts"] >= MAX_ATTEMPTS or not retryable
        delay = min(
            300, max(2 ** claim["attempts"] + random.random(), getattr(error, "retry_after", None) or 0)
        )
        table, key = ("processing_jobs", "document_id") if processing else ("collection_items", "id")
        with self.db.transaction() as conn:
            if not self._valid_claim(conn, claim, processing):
                return
            conn.execute(
                f"UPDATE {table} SET status=?,available_at=?,error=?,lease_token=NULL,lease_until=NULL WHERE {key}=?",
                ("failed" if exhausted else "queued", time.time() + delay, safe, claim[key]),
            )
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
        logger.info(
            "collection_item_failed",
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

    def __init__(self, collection):
        self.collection = collection
        self.stop_event = Event()
        self.thread = None

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
                            (False, downloads, self.collection.download_workers),
                            (True, processing, self.collection.processing_workers),
                        ):
                            free = maximum - sum(kind == is_processing for kind, _ in active.values())
                            for _ in range(free):
                                claim = (
                                    self.collection.claim_processing()
                                    if is_processing
                                    else self.collection.claim_fetch()
                                )
                                if not claim:
                                    break
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
            self.collection.worker_health["status"] = "stopped"

    def stop(self, timeout=5):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout)
