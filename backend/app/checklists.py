"""Versioned collection blueprints, per-account discovery and reviewed selection.

Directory evidence is deliberately separate from documents and their parse queue.
All network I/O continues through the bounded, durable collection worker.
"""

import hashlib
import json
import os
import stat
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .db import dump, now, uid
from .discovery import MAX_HTML_BYTES, discover_page, normalized_seeds_for, recipe_for

PAGE_BATCH = 40
FRONTIER_LIMIT = 20000
CANDIDATE_LIMIT = 20000


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Entry(Definition):
    url: str = Field(pattern=r"^https://", max_length=2000)
    purpose: Literal["catalog", "detail", "document", "reference"]
    note: str


class ChecklistItem(Definition):
    id: str = Field(pattern=r"^\d+\.\d+$")
    chapter_id: str
    title: str
    kind: Literal["law", "guidance", "case", "treaty", "dataset"]
    conditional: bool
    trigger: str
    priority: int = Field(ge=1, le=9)
    entries: list[Entry] = Field(min_length=1, max_length=20)
    trusted_hosts: list[str] = Field(default_factory=list, max_length=20)
    tags: list[str] = Field(max_length=20)
    scope_include: list[str]
    scope_exclude: list[str]
    steps: list[str]
    fields: list[str]
    version_rules: list[str]
    relation_rules: list[str]
    acceptance: list[str]
    verbatim_requirements: str
    execution: Literal["discovery", "dataset_review"]
    adapter: Literal["bpk", "ma_jdih", "sso", "html_links", "manual"]
    limits: dict


class SourceDocument(Definition):
    filename: str
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Chapter(Definition):
    id: str
    title: str
    notes: list[str]


class Blueprint(Definition):
    id: str = Field(pattern=r"^[a-z0-9-]+$", max_length=100)
    version: int = Field(ge=1)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    title: str
    source_document: SourceDocument
    description: str
    principles: list[str]
    chapters: list[Chapter] = Field(max_length=30)
    channels: list[dict] = Field(max_length=30)
    limits: dict
    items: list[ChecklistItem] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def consistent(self):
        chapters = {chapter.id for chapter in self.chapters}
        ids = {item.id for item in self.items}
        if len(ids) != len(self.items) or len(chapters) != len(self.chapters):
            raise ValueError("duplicate checklist identifier")
        if self.limits.get("item_count") != len(self.items):
            raise ValueError("checklist item count differs from source")
        for item in self.items:
            if item.chapter_id not in chapters or f"country:{self.country}" not in item.tags:
                raise ValueError("checklist chapter/country mismatch")
            normalized_seeds_for(item.model_dump())
        return self


def _read_blueprint(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
            raise ValueError("collection blueprint must be a bounded regular file")
        return Blueprint.model_validate_json(handle.read(2_000_001)).model_dump()


def load_blueprints():
    directory = Path(__file__).resolve().parents[2] / "config" / "collection-plans"
    plans = {}
    for path in sorted(directory.glob("*.json")):
        plan = _read_blueprint(path)
        if plan["id"] in plans:
            raise ValueError("duplicate collection blueprint")
        plans[plan["id"]] = plan
    return plans


def load_blueprint_version(plan_id, version):
    """Read one checked-in immutable snapshot; never migrate a running campaign."""
    if not isinstance(plan_id, str) or type(version) is not int or version < 1:
        raise ValueError("collection blueprint identifier/version is invalid")
    current = load_blueprints().get(plan_id)
    if current is None or version > current["version"]:
        raise ValueError("collection blueprint version is not available")
    if version == current["version"]:
        return current
    path = (
        Path(__file__).resolve().parents[2]
        / "config"
        / "collection-plans"
        / "versions"
        / f"{plan_id}.v{version}.json"
    )
    try:
        plan = _read_blueprint(path)
    except (FileNotFoundError, OSError):
        raise ValueError("collection blueprint snapshot is not available") from None
    if plan["id"] != plan_id or plan["version"] != version or plan["country"] != current["country"]:
        raise ValueError("collection blueprint snapshot identity does not match")
    return plan


class ChecklistService:
    def __init__(self, service):
        self.service, self.db = service, service.db
        self.plans = load_blueprints()
        self._migrate()
        self._migrate_page_evidence()

    def _migrate(self):
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=7").fetchone():
                return
            conn.execute("ALTER TABLE sources ADD COLUMN checklist TEXT")
            conn.execute("ALTER TABLE collection_items ADD COLUMN purpose TEXT NOT NULL DEFAULT 'document'")
            conn.execute("ALTER TABLE collection_items ADD COLUMN depth INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE collection_items ADD COLUMN candidate_id TEXT")
            conn.execute("""CREATE TABLE document_countries(
                document_id TEXT PRIMARY KEY REFERENCES documents(id),
                country TEXT NOT NULL CHECK(length(country)=2 AND country GLOB '[A-Z][A-Z]'))""")
            conn.execute("CREATE INDEX documents_country ON document_countries(country,document_id)")
            conn.execute("""CREATE TRIGGER country_immutable_update BEFORE UPDATE ON document_countries
                BEGIN SELECT RAISE(ABORT,'document country is immutable'); END""")
            conn.execute("""CREATE TRIGGER country_immutable_delete BEFORE DELETE ON document_countries
                BEGIN SELECT RAISE(ABORT,'document country is immutable'); END""")
            conn.execute("""CREATE TABLE checklist_bindings(
                id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),plan_id TEXT NOT NULL,
                version INTEGER NOT NULL,item_id TEXT NOT NULL,country TEXT NOT NULL,
                source_id TEXT NOT NULL UNIQUE REFERENCES sources(id),snapshot TEXT NOT NULL,created_at TEXT NOT NULL,
                UNIQUE(owner_id,plan_id,version,item_id))""")
            conn.execute("""CREATE TABLE checklist_candidates(
                id TEXT PRIMARY KEY,binding_id TEXT NOT NULL REFERENCES checklist_bindings(id),
                url TEXT NOT NULL,title TEXT NOT NULL,identity_key TEXT,discovered_from TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',document_id TEXT REFERENCES documents(id),
                revision INTEGER NOT NULL DEFAULT 1,note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
                UNIQUE(binding_id,url))""")
            conn.execute(
                "CREATE INDEX candidates_status ON checklist_candidates(binding_id,status,created_at,id)"
            )
            conn.execute("""CREATE TABLE checklist_frontier(
                binding_id TEXT NOT NULL REFERENCES checklist_bindings(id),url TEXT NOT NULL,purpose TEXT NOT NULL,
                depth INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'waiting',run_id TEXT REFERENCES collection_runs(id),
                created_at TEXT NOT NULL,PRIMARY KEY(binding_id,url))""")
            conn.execute(
                "CREATE INDEX frontier_status ON checklist_frontier(binding_id,status,depth,created_at,url)"
            )
            conn.execute("""CREATE TABLE checklist_pages(
                item_id TEXT PRIMARY KEY REFERENCES collection_items(id),binding_id TEXT NOT NULL REFERENCES checklist_bindings(id),
                url TEXT NOT NULL,final_url TEXT NOT NULL,content_hash TEXT NOT NULL,storage_path TEXT NOT NULL,
                size INTEGER NOT NULL,purpose TEXT NOT NULL,warnings TEXT NOT NULL,created_at TEXT NOT NULL)""")
            conn.execute("CREATE INDEX pages_binding ON checklist_pages(binding_id,url)")
            conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (7, now()))

    def _migrate_page_evidence(self):
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=11").fetchone():
                return
            # Keep the deployed ten-column page table intact for existing
            # snapshots and read-only inventories. Evidence records a request,
            # not a claim that the source's legal coverage is complete.
            conn.execute("""CREATE TABLE checklist_page_evidence(
                item_id TEXT PRIMARY KEY REFERENCES checklist_pages(item_id),
                evidence TEXT NOT NULL CHECK(json_valid(evidence)))""")
            conn.execute("""CREATE TRIGGER page_evidence_immutable_update
                BEFORE UPDATE ON checklist_page_evidence
                BEGIN SELECT RAISE(ABORT,'page evidence is immutable'); END""")
            conn.execute("""CREATE TRIGGER page_evidence_immutable_delete
                BEFORE DELETE ON checklist_page_evidence
                BEGIN SELECT RAISE(ABORT,'page evidence is immutable'); END""")
            conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (11, now()))

    def definition(self, plan_id, item_id=None, *, version=None):
        from .service import fail

        plan = self.plans.get(plan_id)
        if not plan:
            fail(404, "not_found", "未找到采集清单")
        if version is not None:
            if type(version) is not int or version < 1:
                fail(422, "invalid_plan_version", "清单版本必须是正整数")
            if version != plan["version"]:
                try:
                    plan = load_blueprint_version(plan_id, version)
                except ValueError:
                    fail(404, "not_found", "未找到指定版本的采集清单")
        if item_id is None:
            return plan
        item = next((item for item in plan["items"] if item["id"] == item_id), None)
        if item is None:
            fail(404, "not_found", "未找到清单条目")
        return plan, item

    def catalog(self):
        return {
            "items": [
                {
                    key: plan[key]
                    for key in ("id", "version", "country", "title", "description", "source_document")
                }
                | {"item_count": len(plan["items"]), "chapter_count": len(plan["chapters"])}
                for plan in self.plans.values()
            ],
            "total": len(self.plans),
        }

    def binding(self, conn, owner, plan, item):
        return conn.execute(
            "SELECT * FROM checklist_bindings WHERE owner_id=? AND plan_id=? AND version=? AND item_id=?",
            (owner, plan["id"], plan["version"], item["id"]),
        ).fetchone()

    def _configure_item(self, conn, owner, plan, item):
        from .service import fail

        binding = self.binding(conn, owner, plan, item)
        if binding:
            # Definitions at a published version are immutable, even across software restarts.
            if json.loads(binding["snapshot"]) != {"plan": self._plan_snapshot(plan), "item": item}:
                fail(409, "definition_changed", "清单定义已变化，请发布新版本后配置")
            return binding, False
        seeds = normalized_seeds_for(item)
        source = self.service.save_source(
            owner,
            {
                "name": f"{plan['country']} · {item['id']} {item['title']}"[:200],
                "url": seeds[0]["url"] if seeds else item["entries"][0]["url"],
                "enabled": False,
                "interval_hours": 24,
                "tags": item["tags"],
                "plan": {
                    "collection": f"country:{plan['country']}",
                    "discover_attachments": False,
                    "max_items": PAGE_BATCH,
                },
            },
            conn=conn,
        )
        snapshot = {"plan": self._plan_snapshot(plan), "item": item}
        conn.execute(
            "INSERT INTO checklist_bindings VALUES(?,?,?,?,?,?,?,?,?)",
            (
                uid(),
                owner,
                plan["id"],
                plan["version"],
                item["id"],
                plan["country"],
                source["id"],
                dump(snapshot),
                now(),
            ),
        )
        conn.execute(
            "UPDATE sources SET checklist=? WHERE id=?",
            (
                dump(
                    {
                        "plan_id": plan["id"],
                        "item_id": item["id"],
                        "country": plan["country"],
                        "version": plan["version"],
                    }
                ),
                source["id"],
            ),
        )
        return self.binding(conn, owner, plan, item), True

    @staticmethod
    def _plan_snapshot(plan):
        return {key: plan[key] for key in ("id", "version", "country", "source_document", "limits")}

    def configure(self, owner, plan_id, expected_version):
        from .service import fail

        plan = self.definition(plan_id)
        if plan["version"] != expected_version:
            fail(409, "stale_plan", "清单版本已有更新，请刷新")
        with self.db.transaction() as conn:
            created = sum(self._configure_item(conn, owner, plan, item)[1] for item in plan["items"])
        return {"created": created, "existing": len(plan["items"]) - created}

    def discover_all(self, owner, plan_id, client_request_id):
        from .service import AppError

        plan = self.definition(plan_id)
        runs, skipped = [], []
        # Independent short transactions let workers start immediately. A partial
        # request can safely be repeated with the same key after client failure.
        for item in plan["items"]:
            key = (
                "plan-"
                + hashlib.sha256(
                    dump([plan_id, plan["version"], item["id"], client_request_id]).encode()
                ).hexdigest()
            )
            try:
                runs.append(
                    self.start(
                        owner,
                        plan_id,
                        item["id"],
                        {"client_request_id": key, "confirm_condition": False},
                        kind="discovery",
                    )
                )
            except AppError as error:
                skipped.append({"item_id": item["id"], "reason": error.message})
        return {"runs": runs, "started": len(runs), "skipped": skipped}

    def detail(self, owner, plan_id, *, version=None):
        plan = self.definition(plan_id, version=version)
        with closing(self.db.connect()) as conn:
            bindings = conn.execute(
                "SELECT * FROM checklist_bindings WHERE owner_id=? AND plan_id=? AND version=?",
                (owner, plan_id, plan["version"]),
            ).fetchall()
            indexed = {row["item_id"]: row for row in bindings}
            aggregates, latest = {}, {}
            if bindings:
                ids = [b["id"] for b in bindings]
                marks = ",".join("?" for _ in ids)
                for row in conn.execute(
                    f"""SELECT c.binding_id,COUNT(*) candidates,
                    SUM(c.status IN ('pending','queued')) pending,SUM(c.status='excluded') excluded,
                    SUM(c.document_id IS NOT NULL) stored,
                    SUM(c.document_id IS NOT NULL AND json_array_length(d.manifest,'$.chunks')>0) parsed,
                    SUM(c.document_id IS NOT NULL AND json_extract(m.payload,'$.review_status')='published') reviewed
                    FROM checklist_candidates c LEFT JOIN documents d ON d.id=c.document_id
                    LEFT JOIN document_metadata m ON m.document_id=c.document_id
                    WHERE c.binding_id IN ({marks}) GROUP BY c.binding_id""",
                    ids,
                ):
                    aggregates[row["binding_id"]] = {
                        k: row[k] or 0
                        for k in ("candidates", "pending", "excluded", "stored", "parsed", "reviewed")
                    }
                for row in conn.execute(
                    f"SELECT binding_id,COUNT(DISTINCT url) pages FROM checklist_pages WHERE binding_id IN ({marks}) GROUP BY binding_id",
                    ids,
                ):
                    aggregates.setdefault(row["binding_id"], {})["pages"] = row["pages"]
                for row in conn.execute(
                    f"SELECT binding_id,COUNT(*) remaining_pages FROM checklist_frontier WHERE binding_id IN ({marks}) AND status!='visited' GROUP BY binding_id",
                    ids,
                ):
                    aggregates.setdefault(row["binding_id"], {})["remaining_pages"] = row["remaining_pages"]
                runs = conn.execute(
                    f"""SELECT * FROM (SELECT r.*,b.item_id,
                    ROW_NUMBER() OVER(PARTITION BY r.source_id ORDER BY r.created_at DESC,r.id DESC) n
                    FROM collection_runs r JOIN checklist_bindings b ON b.source_id=r.source_id
                    WHERE b.id IN ({marks})) WHERE n=1""",
                    ids,
                ).fetchall()
                counts = self.service.collection.run_counts(conn, [r["id"] for r in runs])
                latest = {
                    r["item_id"]: self.service.collection.run_dict(conn, r, counts.get(r["id"])) for r in runs
                }
            items = []
            for item in plan["items"]:
                binding, run = indexed.get(item["id"]), latest.get(item["id"])
                counts = dict.fromkeys(
                    (
                        "pages",
                        "remaining_pages",
                        "candidates",
                        "pending",
                        "stored",
                        "parsed",
                        "reviewed",
                        "excluded",
                    ),
                    0,
                )
                if binding:
                    counts.update(aggregates.get(binding["id"], {}))
                coverage = "not_started"
                if run:
                    coverage = (
                        "in_progress"
                        if run["status"] in ("queued", "running")
                        else "blocked"
                        if run["failed"] or run["error"]
                        else "needs_review"
                    )
                items.append(
                    {
                        "item_id": item["id"],
                        "source_id": binding["source_id"] if binding else None,
                        "latest_run": run,
                        "counts": counts,
                        "coverage": coverage,
                        "last_error": run["error"]
                        or ("部分页面或文件未完成，请查看任务" if run["failed"] else None)
                        if run
                        else None,
                    }
                )
        return {"plan": plan, "items": items}

    @staticmethod
    def _gate(item, payload):
        from .service import fail

        if item["execution"] != "discovery":
            fail(409, "specialized_review_required", "此条目需专门的候选核验流程，不进入通用采集")
        if item["conditional"] and not payload.get("confirm_condition"):
            fail(422, "condition_required", "请先确认已满足本条目的执行条件")

    @staticmethod
    def _request_hash(plan, item, kind, payload):
        return hashlib.sha256(
            dump([plan["id"], plan["version"], item["id"], kind, payload]).encode()
        ).hexdigest()

    def _previous(self, conn, owner, payload, fingerprint):
        from .service import fail

        row = conn.execute(
            "SELECT * FROM collection_runs WHERE owner_id=? AND client_request_id=?",
            (owner, payload["client_request_id"]),
        ).fetchone()
        if row:
            if row["request_hash"] != fingerprint:
                fail(409, "request_conflict", "同一请求标识不能用于不同采集内容")
            return self.service.collection.run_dict(conn, row)

    def start(self, owner, plan_id, item_id, payload, *, kind, defer_relations=False, version=None):
        from .service import fail

        plan, item = self.definition(plan_id, item_id, version=version)
        self._gate(item, payload)
        recipe = recipe_for(item)
        if recipe["adapter"] == "manual":
            fail(409, "adapter_required", recipe["reason"])
        fingerprint = self._request_hash(
            plan, item, kind, payload | {"defer_relations": True} if defer_relations else payload
        )
        with self.db.transaction() as conn:
            previous = self._previous(conn, owner, payload, fingerprint)
            if previous:
                return previous
            binding, _ = self._configure_item(conn, owner, plan, item)
            if conn.execute(
                """SELECT 1 FROM collection_runs r WHERE r.source_id=? AND r.status IN ('queued','running')
                AND (?=0 OR EXISTS(SELECT 1 FROM collection_items i WHERE i.run_id=r.id AND i.status IN ('queued','fetching')))""",
                (binding["source_id"], int(defer_relations and kind == "collection")),
            ).fetchone():
                fail(409, "collection_active", "本条目已有进行中的任务")
            level = payload.get("level", "primary")
            anchors = payload.get("primary_document_ids", [])
            if kind == "discovery":
                # Recover pages left by terminal/cancelled runs before considering a fresh pass.
                conn.execute(
                    "UPDATE checklist_frontier SET status='waiting' WHERE binding_id=? AND status='queued'",
                    (binding["id"],),
                )
                if not conn.execute(
                    "SELECT 1 FROM checklist_frontier WHERE binding_id=? AND status='waiting'",
                    (binding["id"],),
                ).fetchone():
                    conn.execute(
                        "UPDATE checklist_frontier SET status='waiting' WHERE binding_id=? AND status='visited'",
                        (binding["id"],),
                    )
                    for seed in normalized_seeds_for(item):
                        conn.execute(
                            """INSERT INTO checklist_frontier VALUES(?,?,?,?,?,?,?)
                            ON CONFLICT(binding_id,url) DO UPDATE SET status='waiting',depth=0""",
                            (binding["id"], seed["url"], seed["purpose"], 0, "waiting", None, now()),
                        )
                entries = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT url,purpose,depth FROM checklist_frontier WHERE binding_id=? AND status='waiting' ORDER BY depth,created_at,url LIMIT ?",
                        (binding["id"], PAGE_BATCH),
                    )
                ]
            else:
                ids = list(dict.fromkeys(payload["candidate_ids"]))
                marks = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT * FROM checklist_candidates WHERE binding_id=? AND id IN ({marks})",
                    (binding["id"], *ids),
                ).fetchall()
                if len(rows) != len(ids):
                    fail(404, "not_found", "未找到当前条目的候选资料")
                if any(row["status"] not in ("pending", "stored") for row in rows):
                    fail(409, "candidate_unavailable", "候选资料已排除或正在采集，请刷新")
                entries = [
                    {
                        "url": row["url"],
                        "purpose": "document",
                        "identity_key": row["identity_key"],
                        "candidate_id": row["id"],
                    }
                    for row in rows
                ]
            if not entries:
                fail(409, "no_candidates", "没有可执行的页面或候选资料")
            from .models import CollectionPlan

            run_plan = CollectionPlan(
                collection=f"country:{plan['country']}",
                level=level,
                primary_document_ids=anchors,
                discover_attachments=False,
                max_items=PAGE_BATCH if kind == "discovery" else 200,
                max_original_bytes=item.get("limits", {}).get("max_original_bytes", 20 * 1024 * 1024),
                download_timeout_seconds=item.get("limits", {}).get("download_timeout_seconds", 90),
            ).model_dump()
            run_plan["checklist"] = {
                "binding_id": binding["id"],
                "kind": kind,
                "country": plan["country"],
                "definition": json.loads(binding["snapshot"]),
                "recipe": recipe,
                "confirmed_condition": payload.get("confirm_condition", False),
                "defer_relations": defer_relations,
            }
            run = self.service.collection.enqueue(
                owner,
                binding["source_id"],
                payload | {"entries": entries},
                conn=conn,
                trusted_plan=run_plan,
                request_hash=fingerprint,
            )
            if kind == "discovery":
                conn.executemany(
                    "UPDATE checklist_frontier SET status='queued',run_id=? WHERE binding_id=? AND url=?",
                    [(run["id"], binding["id"], entry["url"]) for entry in entries],
                )
            else:
                conn.executemany(
                    "UPDATE checklist_candidates SET status='queued',revision=revision+1 WHERE id=?",
                    [(entry["candidate_id"],) for entry in entries],
                )
            return run

    def candidates(self, owner, plan_id, item_id, status="pending", limit=20, offset=0):
        plan, item = self.definition(plan_id, item_id)
        with closing(self.db.connect()) as conn:
            binding = self.binding(conn, owner, plan, item)
            if not binding:
                return {"items": [], "total": 0}
            condition = "status IN ('pending','queued')" if status == "pending" else "status=?"
            args = (binding["id"],) if status == "pending" else (binding["id"], status)
            total = conn.execute(
                f"SELECT COUNT(*) FROM checklist_candidates WHERE binding_id=? AND {condition}", args
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM checklist_candidates WHERE binding_id=? AND {condition} ORDER BY created_at,id LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
            return {"items": [self._candidate(row) for row in rows], "total": total}

    @staticmethod
    def _candidate(row):
        return {
            key: row[key]
            for key in (
                "id",
                "url",
                "title",
                "discovered_from",
                "status",
                "document_id",
                "revision",
                "note",
                "created_at",
            )
        }

    def patch_candidate(self, owner, id, payload):
        from .service import fail

        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT c.* FROM checklist_candidates c JOIN checklist_bindings b ON b.id=c.binding_id WHERE c.id=? AND b.owner_id=?",
                (id, owner),
            ).fetchone()
            if not row:
                fail(404, "not_found", "未找到候选资料")
            if row["revision"] != payload["expected_revision"] or row["status"] not in (
                "pending",
                "excluded",
            ):
                fail(409, "stale_candidate", "候选状态已有更新，请刷新后重试")
            conn.execute(
                "UPDATE checklist_candidates SET status=?,note=?,revision=revision+1 WHERE id=?",
                (payload["status"], payload.get("note", row["note"]), id),
            )
            return self._candidate(
                conn.execute("SELECT * FROM checklist_candidates WHERE id=?", (id,)).fetchone()
            )

    @staticmethod
    def inspect(plan, fetched):
        # The full blob is already streamed/hashed with the global size bound.
        # Catalog JSON/HTML have their own bound; neither becomes legal text.
        blob = fetched["blob"]
        mime = fetched["mime_type"].split(";", 1)[0].strip().lower()
        if mime in {"text/html", "application/xhtml+xml", "application/json"} and blob.size > MAX_HTML_BYTES:
            raise ValueError("目录或详情页超过 4 MiB 解析上限")
        with blob.path.open("rb") as stream:
            body = stream.read(MAX_HTML_BYTES)
        result = discover_page(fetched["url"], body, fetched["mime_type"], plan["checklist"]["recipe"])
        if not result["matched"]:
            raise ValueError(result.get("reason") or "页面不符合当前清单范围")
        if plan["checklist"]["kind"] == "collection" and result["purpose"] != "document":
            raise ValueError("候选网址返回目录或详情页，未取得法规原件")
        return result

    def save_page(self, conn, claim, fetched, plan, result):
        binding = plan["checklist"]["binding_id"]
        blob = fetched["blob"]
        conn.execute(
            "INSERT INTO checklist_pages VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                claim["id"],
                binding,
                claim["url"],
                fetched["url"],
                blob.digest,
                str(blob.path),
                blob.size,
                result["purpose"],
                dump(result["warnings"]),
                now(),
            ),
        )
        evidence = {
            "request": fetched.get("request_evidence")
            or {"method": fetched.get("request_method", "GET"), "url": fetched["url"]}
        }
        if result.get("scope"):
            evidence["scope"] = result["scope"]
        conn.execute(
            "INSERT INTO checklist_page_evidence(item_id,evidence) VALUES(?,?)",
            (claim["id"], dump(evidence)),
        )
        conn.execute(
            "UPDATE collection_items SET status='discovered',final_url=?,fetched_at=?,error=NULL,lease_token=NULL,lease_until=NULL WHERE id=?",
            (fetched["url"], now(), claim["id"]),
        )
        conn.execute(
            "UPDATE checklist_frontier SET status='visited' WHERE binding_id=? AND url=?",
            (binding, claim["url"]),
        )
        links = list(result["links"])
        if result["purpose"] == "document":
            links.insert(
                0,
                {
                    "url": fetched["url"],
                    "purpose": "document",
                    "title": result.get("title", fetched["filename"]),
                    "identity_key": claim["identity_key"],
                },
            )
        page_count = conn.execute(
            "SELECT COUNT(*) FROM checklist_frontier WHERE binding_id=?", (binding,)
        ).fetchone()[0]
        candidate_count = conn.execute(
            "SELECT COUNT(*) FROM checklist_candidates WHERE binding_id=?", (binding,)
        ).fetchone()[0]
        capped = False
        for link in links:
            if link["purpose"] == "document":
                if candidate_count >= CANDIDATE_LIMIT:
                    capped = True
                    continue
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO checklist_candidates
                    (id,binding_id,url,title,identity_key,discovered_from,created_at) VALUES(?,?,?,?,?,?,?)""",
                    (
                        uid(),
                        binding,
                        link["url"],
                        link.get("title", "")[:500],
                        link.get("identity_key"),
                        fetched["url"],
                        now(),
                    ),
                )
                candidate_count += cursor.rowcount
            elif page_count < FRONTIER_LIMIT and claim["depth"] < 30:
                depth = (
                    claim["depth"]
                    if link["purpose"] == "catalog" and claim["purpose"] == "catalog"
                    else claim["depth"] + 1
                )
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO checklist_frontier VALUES(?,?,?,?,?,?,?)",
                    (binding, link["url"], link["purpose"], depth, "waiting", None, now()),
                )
                page_count += cursor.rowcount
            else:
                capped = True
        # Queue a bounded number of pages and leave the remainder in the durable
        # frontier. Another discovery run continues them after this batch ends.
        used = conn.execute(
            "SELECT COUNT(*) FROM collection_items WHERE run_id=?", (claim["run_id"],)
        ).fetchone()[0]
        global_pending = conn.execute(
            "SELECT COUNT(*) FROM collection_items WHERE status IN ('queued','fetching')"
        ).fetchone()[0]
        owned_pending = conn.execute(
            "SELECT COUNT(*) FROM collection_items WHERE owner_id=? AND status IN ('queued','fetching')",
            (claim["owner_id"],),
        ).fetchone()[0]
        available = max(0, min(plan["max_items"] - used, 2000 - global_pending, 1000 - owned_pending))
        for row in conn.execute(
            "SELECT url,purpose,depth FROM checklist_frontier WHERE binding_id=? AND status='waiting' ORDER BY depth,created_at,url LIMIT ?",
            (binding, available),
        ).fetchall():
            self.service.collection._insert_item(
                conn,
                claim["run_id"],
                claim["owner_id"],
                row["url"],
                "primary",
                purpose=row["purpose"],
                depth=row["depth"],
            )
            conn.execute(
                "UPDATE checklist_frontier SET status='queued',run_id=? WHERE binding_id=? AND url=?",
                (claim["run_id"], binding, row["url"]),
            )
        warnings = list(result["warnings"])
        if capped:
            warnings.append("发现队列达到范围上限，请拆分条目或补充适配")
        if warnings:
            conn.execute(
                "UPDATE collection_runs SET error=? WHERE id=?", ("；".join(warnings)[:500], claim["run_id"])
            )
        self.service.collection._refresh(conn, claim["run_id"])

    def stored_candidate(self, conn, claim, doc):
        if not claim["candidate_id"]:
            return
        conn.execute(
            "UPDATE checklist_candidates SET status='stored',document_id=?,revision=revision+1 WHERE id=?",
            (doc["id"], claim["candidate_id"]),
        )

    @staticmethod
    def release_candidate(conn, claim):
        if claim.get("candidate_id"):
            conn.execute(
                "UPDATE checklist_candidates SET status=CASE WHEN document_id IS NULL THEN 'pending' ELSE 'stored' END,revision=revision+1 WHERE id=?",
                (claim["candidate_id"],),
            )
