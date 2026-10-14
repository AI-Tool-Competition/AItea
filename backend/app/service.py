import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from .db import Database, dump, now, uid
from .catalog import DocumentMetadata, LegalCatalog, eligible
from .defaults import BUILTIN_TEMPLATES, RETIRED_TEMPLATE_ID
from .models import Template

logger = logging.getLogger(__name__)
MAX_UPLOAD = 20 * 1024 * 1024
DOCUMENT_FIELDS = (
    "id",
    "title",
    "filename",
    "role",
    "status",
    "mime_type",
    "size",
    "version",
    "case_id",
    "parent_id",
    "created_at",
    "source_url",
    "error",
    "family_id",
    "content_hash",
)
# Lists never materialize plaintext or embedding arrays from the manifest.
DOCUMENT_SUMMARY = ",".join(DOCUMENT_FIELDS) + ",tags,json_array_length(manifest,'$.chunks') chunk_count"


class AppError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


def fail(status, code, message):
    raise AppError(status, code, message)


def check(id, label, status, reason, refs=None):
    return {"id": id, "label": label, "status": status, "reason": reason, "evidence_refs": refs or []}


class Service:
    def __init__(self, data_dir):
        from .studio import AdminStudio
        from .archive import LegalArchive
        from .collection import CollectionService

        self.db = Database(Path(data_dir))
        self.catalog = LegalCatalog(self.db)
        self.studio = AdminStudio(self)
        self._chat_lock = threading.Lock()
        self._active_chats = set()
        self.archive = LegalArchive(self)
        self.collection = CollectionService(self)
        from .checklists import ChecklistService

        self.checklists = ChecklistService(self)
        from .legal import LegalService

        self.legal = LegalService(self)

    @property
    def capabilities(self):
        live = all(os.getenv(key) for key in ("LLM_API_KEY", "LLM_MODEL"))
        return {
            "assistant_mode": "live" if live else "demo",
            "retrieval_mode": "hybrid" if self.embedding_fingerprint() else "lexical",
            "parser": os.getenv("TEA_PARSER", "basic"),
        }

    def embedding_fingerprint(self):
        values = [os.getenv(key) for key in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY")]
        return hashlib.sha256(dump(values[:2]).encode()).hexdigest() if all(values) else None

    def rerank_fingerprint(self):
        values = [os.getenv(key) for key in ("RERANK_BASE_URL", "RERANK_MODEL", "RERANK_API_KEY")]
        return hashlib.sha256(dump(values).encode()).hexdigest() if any(values) else None

    def template(self, owner, template_id, conn=None):
        if template_id == RETIRED_TEMPLATE_ID:
            fail(404, "not_found", "旧演示流程已下线，请选择六堡茶流程")
        if conn is None:
            with closing(self.db.connect()) as db:
                return self.template(owner, template_id, db)
        row = conn.execute(
            "SELECT payload FROM templates WHERE owner_id=? AND id=? ORDER BY version DESC LIMIT 1",
            (owner, template_id),
        ).fetchone()
        if row:
            return json.loads(row["payload"])
        if template_id in BUILTIN_TEMPLATES:
            return json.loads(dump(BUILTIN_TEMPLATES[template_id]))
        fail(404, "not_found", "未找到流程模板")

    def templates(self, owner):
        with closing(self.db.connect()) as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT DISTINCT id FROM templates WHERE owner_id=? ORDER BY id", (owner,)
                )
            ]
            return [
                self.template(owner, id, conn)
                for id in dict.fromkeys([*BUILTIN_TEMPLATES, *ids])
                if id != RETIRED_TEMPLATE_ID
            ]

    def validate_template(self, payload):
        if payload.get("id") == RETIRED_TEMPLATE_ID:
            return {
                "valid": False,
                "errors": [{"path": "id", "message": "旧演示流程已下线，请选择六堡茶流程"}],
                "preview": {"stages": 0, "tasks": 0},
            }
        try:
            template = Template.model_validate(payload)
            return {
                "valid": True,
                "errors": [],
                "preview": {
                    "stages": len(template.stages),
                    "tasks": sum(len(stage.tasks) for stage in template.stages),
                },
            }
        except ValidationError as error:
            return {
                "valid": False,
                "errors": [
                    {"path": ".".join(str(p) for p in item["loc"]), "message": item["msg"]}
                    for item in error.errors()
                ],
                "preview": {"stages": 0, "tasks": 0},
            }

    def publish(self, owner, payload, expected_draft_revision=None):
        validation = self.validate_template(payload)
        if not validation["valid"]:
            fail(
                422,
                "invalid_template",
                "; ".join(f"{e['path']}: {e['message']}" for e in validation["errors"]),
            )
        template = Template.model_validate(payload).model_dump()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT MAX(version) n FROM templates WHERE owner_id=? AND id=?", (owner, template["id"])
            ).fetchone()
            base = BUILTIN_TEMPLATES.get(template["id"], {}).get("version", 0)
            # A previously published account version remains authoritative even
            # when a later application bundle raises its built-in version.
            latest = row["n"] if row["n"] is not None else base
            if template["version"] != (latest or 1):
                fail(409, "stale_template", "已发布配置已有更新，请载入最新版本后再发布")
            if expected_draft_revision is not None:
                draft = conn.execute(
                    "SELECT revision FROM template_drafts WHERE owner_id=? AND id=?", (owner, template["id"])
                ).fetchone()
                if expected_draft_revision != (draft["revision"] if draft else 0):
                    fail(409, "stale_draft", "草稿已有更新，请重新载入后再发布")
            template["version"] = latest + 1
            conn.execute(
                "INSERT INTO templates VALUES (?,?,?,?,?)",
                (owner, template["id"], template["version"], dump(template), now()),
            )
            if expected_draft_revision is not None:
                self.studio.write_draft(conn, owner, template["id"], template, expected_draft_revision + 1)
        return template

    def own_case(self, conn, owner, case_id):
        row = conn.execute("SELECT * FROM cases WHERE id=? AND owner_id=?", (case_id, owner)).fetchone()
        if row is None or json.loads(row["template"])["id"] == RETIRED_TEMPLATE_ID:
            fail(404, "not_found", "未找到业务")
        return row

    def event(self, conn, case_id, type, title, detail):
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?)", (uid(), case_id, type, title, detail, now()))

    def touch(self, conn, case_id):
        conn.execute("UPDATE cases SET revision=revision+1,updated_at=? WHERE id=?", (now(), case_id))

    def case_detail(self, owner, case_id, conn=None):
        if conn is None:
            with closing(self.db.connect()) as db:
                return self.case_detail(owner, case_id, db)
        row = self.own_case(conn, owner, case_id)
        template = json.loads(row["template"])
        states = {
            r["task_id"]: r for r in conn.execute("SELECT * FROM task_states WHERE case_id=?", (case_id,))
        }
        stages = []
        predecessor_ok = True
        for definition in template["stages"]:
            tasks = []
            for task in definition["tasks"]:
                state = states[task["id"]]
                tasks.append(
                    {
                        **task,
                        "status": state["status"],
                        "needs_review": bool(state["needs_review"]),
                        "revision": state["revision"],
                        "values": json.loads(state["values_json"]),
                        "evidence_ids": json.loads(state["evidence_ids"]),
                        "confirmed": bool(state["confirmed"]),
                        "verification": json.loads(state["verification"]) if state["verification"] else None,
                    }
                )
            remaining = [
                t for t in tasks if t["required"] and (t["status"] != "completed" or t["needs_review"])
            ]
            ready_for_next = not any(t.get("blocks_next_stage", True) for t in remaining)
            status = (
                ("completed" if not remaining else "follow_up" if ready_for_next else "active")
                if predecessor_ok
                else "locked"
            )
            stages.append({**definition, "status": status, "tasks": tasks})
            predecessor_ok = predecessor_ok and ready_for_next
        facts = [
            {
                "key": r["key"],
                "label": r["label"],
                "value": json.loads(r["value"]),
                "source": r["source"],
                "confirmed": bool(r["confirmed"]),
                "updated_at": r["updated_at"],
            }
            for r in conn.execute("SELECT * FROM facts WHERE case_id=? ORDER BY key", (case_id,))
        ]
        events = [
            dict(r)
            for r in conn.execute(
                "SELECT id,type,title,detail,created_at FROM events WHERE case_id=? ORDER BY created_at DESC LIMIT 100",
                (case_id,),
            )
        ]
        docs = [
            self.document_dict(r)
            for r in conn.execute(
                "SELECT * FROM documents WHERE case_id=? ORDER BY created_at DESC LIMIT 200", (case_id,)
            )
        ]
        artifacts = [
            self.artifact_dict(r)
            for r in conn.execute(
                "SELECT * FROM artifacts WHERE case_id=? ORDER BY updated_at DESC LIMIT 100", (case_id,)
            )
        ]
        return {
            key: row[key]
            for key in (
                "id",
                "title",
                "company",
                "product",
                "mode_id",
                "revision",
                "created_at",
                "updated_at",
            )
        } | {
            "template_version": template["version"],
            "template": template,
            "stages": stages,
            "facts": facts,
            "documents": docs,
            "artifacts": artifacts,
            "events": events,
            "completed_tasks": sum(
                t["status"] == "completed" and not t["needs_review"] for s in stages for t in s["tasks"]
            ),
            "total_tasks": sum(len(s["tasks"]) for s in stages),
            "current_stage_id": next(
                (s["id"] for s in stages if s["status"] == "active"),
                next((s["id"] for s in stages if s["status"] == "follow_up"), stages[-1]["id"]),
            ),
        }

    def cases(self, owner, limit=100, offset=0):
        with closing(self.db.connect()) as conn:
            ids = [
                r["id"]
                for r in conn.execute(
                    "SELECT id FROM cases WHERE owner_id=? AND json_extract(template,'$.id')!=? ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
                    (owner, RETIRED_TEMPLATE_ID, limit, offset),
                )
            ]
            total = conn.execute(
                "SELECT COUNT(*) n FROM cases WHERE owner_id=? AND json_extract(template,'$.id')!=?",
                (owner, RETIRED_TEMPLATE_ID),
            ).fetchone()["n"]
            items = []
            for id in ids:
                detail = self.case_detail(owner, id, conn)
                items.append(
                    {
                        k: v
                        for k, v in detail.items()
                        if k
                        not in ("template", "stages", "facts", "documents", "artifacts", "events", "revision")
                    }
                )
            return {"items": items, "total": total}

    def create_case(self, owner, values):
        with self.db.transaction() as conn:
            template = self.template(owner, values["template_id"], conn)
            if not any(m["id"] == values["mode_id"] and m["enabled"] for m in template["modes"]):
                fail(422, "invalid_mode", "请选择模板中已启用的模式")
            id, at = uid(), now()
            conn.execute(
                "INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    id,
                    owner,
                    values["title"],
                    values.get("company", ""),
                    values.get("product", ""),
                    values["mode_id"],
                    dump(template),
                    1,
                    at,
                    at,
                ),
            )
            for stage in template["stages"]:
                for task in stage["tasks"]:
                    conn.execute(
                        "INSERT INTO task_states(case_id,task_id,stage_id,status) VALUES (?,?,?,?)",
                        (id, task["id"], stage["id"], "pending"),
                    )
            first = template["stages"][0]
            content = (
                f"欢迎来到茶叶出口工作台。我们先一起完成「{first['name']}」。\n\n{first['description']}\n\n"
                + "\n".join(f"- **{t['title']}**：{t['description']}" for t in first["tasks"])
                + "\n\n这些任务可以并行办理。你可以先上传资料，或选择一项任务开始。"
                + (
                    "当前流程为演示配置，正式要求需要法务配置和可靠来源。"
                    if template["is_demo"]
                    else "请按任务要求记录责任人、办理结果和证据；流程核验不代替主管机构审批。"
                )
            )
            self.insert_message(
                conn,
                id,
                first["id"],
                None,
                "assistant",
                content,
                [],
                [
                    {
                        "id": "initialize",
                        "label": "读取流程配置",
                        "status": "completed",
                        "detail": f"已绑定模板第 {template['version']} 版；尚未核验任何任务。",
                    }
                ],
            )
            self.event(
                conn, id, "case_created", "建立出口业务", f"已冻结流程模板第 {template['version']} 版。"
            )
            return self.case_detail(owner, id, conn)

    def get_task(self, case, task_id, *, require_unlocked=True):
        for stage in case["stages"]:
            for task in stage["tasks"]:
                if task["id"] == task_id:
                    if require_unlocked and stage["status"] == "locked":
                        fail(409, "stage_locked", "请先完成前序节点的必办任务")
                    return stage, task
        fail(404, "not_found", "未找到任务")

    def check_revision(self, task, expected):
        if task["revision"] != expected:
            fail(409, "stale_revision", "资料已经更新，请刷新后基于当前版本操作")

    def invalidate_task(self, conn, case_id, task_id, *, reason):
        row = conn.execute(
            "SELECT status,needs_review FROM task_states WHERE case_id=? AND task_id=?", (case_id, task_id)
        ).fetchone()
        if row:
            review = row["status"] == "completed" or bool(row["needs_review"])
            conn.execute(
                "UPDATE task_states SET status=?,needs_review=?,confirmed=0,revision=revision+1 WHERE case_id=? AND task_id=?",
                ("in_progress", int(review), case_id, task_id),
            )
            conn.execute(
                "UPDATE facts SET confirmed=0 WHERE case_id=? AND source=?", (case_id, f"task:{task_id}")
            )
            template = json.loads(
                conn.execute("SELECT template FROM cases WHERE id=?", (case_id,)).fetchone()["template"]
            )
            current_stage = next(
                i
                for i, stage in enumerate(template["stages"])
                if any(t["id"] == task_id for t in stage["tasks"])
            )
            changed = next(t for t in template["stages"][current_stage]["tasks"] if t["id"] == task_id)
            affected = [task_id]
            if changed.get("blocks_next_stage", True):
                affected += [
                    t["id"] for stage in template["stages"][current_stage + 1 :] for t in stage["tasks"]
                ]
            for affected_id in affected:
                if affected_id != task_id:
                    conn.execute(
                        "UPDATE task_states SET status='in_progress',needs_review=1,confirmed=0,revision=revision+1 WHERE case_id=? AND task_id=? AND (status='completed' OR needs_review=1)",
                        (case_id, affected_id),
                    )
                    conn.execute(
                        "UPDATE facts SET confirmed=0 WHERE case_id=? AND source=?",
                        (case_id, f"task:{affected_id}"),
                    )
                conn.execute(
                    "UPDATE artifacts SET confirmed=0 WHERE case_id=? AND task_id=?", (case_id, affected_id)
                )
            if review:
                self.event(conn, case_id, "review_required", "任务需要复核", reason)

    def normalize_values(self, task, supplied):
        definitions = {field["key"]: field for field in task["fields"]}
        values = dict(task["values"])
        for key, value in supplied.items():
            if key not in definitions:
                fail(422, "unknown_field", f"任务未定义字段：{key}")
            if isinstance(value, str) and len(value) > 10000:
                fail(422, "value_too_long", "单个字段内容不能超过 10000 字")
            if definitions[key]["type"] == "number" and value != "":
                try:
                    value = float(value)
                    if not math.isfinite(value):
                        raise ValueError()
                    if value.is_integer():
                        value = int(value)
                except (TypeError, ValueError, OverflowError):
                    fail(422, "invalid_number", f"{definitions[key]['label']}必须是有限数值")
            elif definitions[key]["type"] != "number":
                value = str(value).strip()
            if (
                definitions[key]["type"] == "select"
                and value != ""
                and value not in definitions[key]["options"]
            ):
                fail(422, "invalid_option", f"{definitions[key]['label']}必须选择配置中的选项")
            values[key] = value
        return values

    def patch_task(self, owner, case_id, task_id, payload):
        with self.db.transaction() as conn:
            case = self.case_detail(owner, case_id, conn)
            _, task = self.get_task(case, task_id)
            self.check_revision(task, payload["expected_revision"])
            definitions = {f["key"]: f for f in task["fields"]}
            values = self.normalize_values(task, payload["values"])
            evidence = payload.get("evidence_ids")
            if evidence is None:
                evidence = task["evidence_ids"]
            evidence = list(dict.fromkeys(evidence))
            for doc_id in evidence:
                doc = self.own_document(conn, owner, doc_id)
                if doc["case_id"] != case_id or doc["role"] != "primary" or doc["status"] != "ready":
                    fail(422, "invalid_evidence", "核验材料必须是本业务中解析成功的主文件")
            if values == task["values"] and evidence == task["evidence_ids"]:
                return case
            self.invalidate_task(
                conn, case_id, task_id, reason=f"「{task['title']}」的资料已修改，旧确认不再适用。"
            )
            conn.execute(
                "UPDATE task_states SET values_json=?,evidence_ids=? WHERE case_id=? AND task_id=?",
                (dump(values), dump(evidence), case_id, task_id),
            )
            for key, value in values.items():
                conn.execute(
                    "INSERT INTO facts VALUES (?,?,?,?,?,?,?) ON CONFLICT(case_id,key) DO UPDATE SET label=excluded.label,value=excluded.value,source=excluded.source,confirmed=0,updated_at=excluded.updated_at",
                    (case_id, key, definitions[key]["label"], dump(value), f"task:{task_id}", 0, now()),
                )
            self.touch(conn, case_id)
            self.event(
                conn,
                case_id,
                "task_updated",
                "更新任务资料",
                f"已保存「{task['title']}」的当前资料，等待核验。",
            )
            return self.case_detail(owner, case_id, conn)

    def checks(self, conn, task, confirm=False):
        checks = []
        for definition in task["fields"]:
            value = task["values"].get(definition["key"])
            missing = value is None or (isinstance(value, str) and not value.strip())
            ok = not missing or not definition["required"]
            reason = (
                "信息已提供"
                if not missing
                else ("需要补充此信息" if definition["required"] else "可选信息未填写")
            )
            if not missing and definition["type"] == "number":
                try:
                    ok = math.isfinite(float(value)) and (
                        definition.get("min") is None or float(value) >= definition["min"]
                    )
                except (ValueError, TypeError):
                    ok = False
                if not ok:
                    reason = f"需要有效数值，最小值为 {definition.get('min', '无限制')}"
            if not missing and definition["type"] == "select":
                ok = value in (definition.get("options") or [])
                if not ok:
                    reason = "请选择配置中的有效选项"
                elif definition.get("accepted_values") and value not in definition["accepted_values"]:
                    ok = False
                    reason = f"当前为「{value}」，尚未满足完成条件；请核对实际办理结果和证据"
            checks.append(
                check(definition["key"], definition["label"], "passed" if ok else "missing", reason)
            )
        docs = [
            conn.execute("SELECT * FROM documents WHERE id=?", (id,)).fetchone()
            for id in task["evidence_ids"]
        ]
        docs = [doc for doc in docs if doc and doc["status"] == "ready" and doc["role"] == "primary"]
        for doc in docs:
            metadata = self.catalog.metadata_for(doc["id"], source_url=doc["source_url"], conn=conn)[
                "metadata"
            ]
            if not eligible(metadata, datetime.now(timezone.utc).date()):
                checks.append(
                    check(
                        "document_review:" + doc["id"],
                        "材料审核与适用期",
                        "conflict",
                        f"材料「{doc['filename']}」尚未通过审核，或不在当前适用期内。",
                        [doc["id"]],
                    )
                )
            newer = conn.execute(
                "SELECT id FROM documents WHERE owner_id=? AND case_id IS ? AND filename=? AND role=? AND version>? LIMIT 1",
                (doc["owner_id"], doc["case_id"], doc["filename"], doc["role"], doc["version"]),
            ).fetchone()
            if newer:
                checks.append(
                    check(
                        "evidence_version:" + doc["id"],
                        "材料版本",
                        "conflict",
                        f"材料「{doc['filename']}」已有新版本，请关联当前版本后重新核验。",
                        [doc["id"], newer["id"]],
                    )
                )
        if task["require_document"]:
            checks.append(
                check(
                    "document",
                    "办理材料",
                    "passed" if docs else "missing",
                    "已关联可读取的主文件" if docs else "请上传主文件并关联到此任务",
                    [d["id"] for d in docs],
                )
            )
        weight = task["values"].get("net_weight_g")
        if weight not in (None, ""):
            for doc in docs:
                manifest = json.loads(doc["manifest"])
                text = "\n".join(e.get("content_text", "") for e in manifest.get("elements", []))
                found = re.findall(
                    r"(?:每袋净含量|净含量|净重|net\s*(?:weight|content))\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(千克|公斤|kg|克|g)",
                    text,
                    flags=re.I,
                )
                amounts = {
                    float(amount) * (1000 if unit.lower() in ("千克", "公斤", "kg") else 1)
                    for amount, unit in found
                }
                if amounts and any(abs(amount - float(weight)) > 0.0001 for amount in amounts):
                    checks.append(
                        check(
                            "weight_consistency:" + doc["id"],
                            "净含量一致性",
                            "conflict",
                            f"材料「{doc['filename']}」记载 {', '.join(str(x) for x in sorted(amounts))} 克，当前填报 {weight} 克。请核对或替换过期材料。",
                            [doc["id"]],
                        )
                    )
        if task["require_confirmation"]:
            checks.append(
                check(
                    "confirmation",
                    "确认当前资料",
                    "passed" if confirm else "missing",
                    "用户确认当前版本适用于本批次" if confirm else "请确认当前版本适用于本批次",
                )
            )
        return checks

    def verify_task(self, owner, case_id, task_id, payload):
        with self.db.transaction() as conn:
            case = self.case_detail(owner, case_id, conn)
            _, task = self.get_task(case, task_id)
            self.check_revision(task, payload["expected_revision"])
            if task["status"] == "completed" and not task["needs_review"]:
                return case
            checks = self.checks(conn, task, payload["confirm"])
            passed = all(c["status"] == "passed" for c in checks)
            revision = task["revision"] + 1
            verification = {
                "id": uid(),
                "status": "passed" if passed else "needs_input",
                "checks": checks,
                "created_at": now(),
                "task_revision": revision,
            }
            conn.execute(
                "INSERT INTO verifications VALUES (?,?,?,?,?,?)",
                (
                    verification["id"],
                    case_id,
                    task_id,
                    revision,
                    dump(verification),
                    verification["created_at"],
                ),
            )
            conn.execute(
                "UPDATE task_states SET status=?,needs_review=?,confirmed=?,revision=?,verification=? WHERE case_id=? AND task_id=?",
                (
                    "completed" if passed else "needs_input",
                    0 if passed else int(task["needs_review"]),
                    int(payload["confirm"] and passed),
                    revision,
                    dump(verification),
                    case_id,
                    task_id,
                ),
            )
            conn.execute(
                "UPDATE facts SET confirmed=? WHERE case_id=? AND source=?",
                (int(passed), case_id, f"task:{task_id}"),
            )
            self.touch(conn, case_id)
            self.event(
                conn,
                case_id,
                "verification_passed" if passed else "verification_needs_input",
                "任务核验通过" if passed else "核验发现待补充事项",
                f"「{task['title']}」：" + "；".join(c["reason"] for c in checks if c["status"] != "passed"),
            )
            return self.case_detail(owner, case_id, conn)

    def own_document(self, conn, owner, id):
        doc = conn.execute("SELECT * FROM documents WHERE id=? AND owner_id=?", (id, owner)).fetchone()
        if doc is None:
            fail(404, "not_found", "未找到资料")
        return doc

    def document_dict(self, row, conn=None):
        doc = self.document_base(row)
        return self.archive.enrich(self.catalog.enrich_document(doc, conn=conn), conn=conn)

    def document_base(self, row):
        doc = {key: row[key] for key in DOCUMENT_FIELDS}
        doc["tags"] = json.loads(row["tags"])
        doc["chunk_count"] = (
            row["chunk_count"]
            if "chunk_count" in row.keys()
            else len(json.loads(row["manifest"]).get("chunks", []))
        )
        return doc

    def document_summaries(self, rows, conn):
        if not rows:
            return []
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        metadata = {
            row["document_id"]: row
            for row in conn.execute(
                f"SELECT * FROM document_metadata WHERE document_id IN ({placeholders})", ids
            )
        }
        classifications = {}
        countries = {
            row["document_id"]: row["country"]
            for row in conn.execute(
                f"""SELECT d.id document_id,COALESCE(dc.country,pc.country) country FROM documents d
                    LEFT JOIN document_countries dc ON dc.document_id=d.id
                    LEFT JOIN document_countries pc ON pc.document_id=d.parent_id WHERE d.id IN ({placeholders})""",
                ids,
            )
        }
        for row in conn.execute(
            f"SELECT * FROM legal_classifications WHERE document_id IN ({placeholders}) ORDER BY collection",
            ids,
        ):
            classifications.setdefault(row["document_id"], []).append(row)
        result = []
        for row in rows:
            doc = self.document_base(row)
            doc["country"] = countries.get(row["id"])
            saved = metadata.get(row["id"])
            if saved:
                doc.update(
                    metadata=json.loads(saved["payload"]),
                    metadata_revision=saved["revision"],
                    reviewed_at=saved["reviewed_at"],
                )
            else:
                default = (
                    DocumentMetadata(kind="law", review_status="draft")
                    if row["source_url"]
                    else DocumentMetadata()
                )
                doc.update(metadata=default.model_dump(mode="json"), metadata_revision=0, reviewed_at=None)
            result.append(self.archive.with_classifications(doc, classifications.get(row["id"], [])))
        return result

    def metadata_changed(self, conn, document):
        if not document["case_id"]:
            return
        states = conn.execute(
            "SELECT task_id,evidence_ids FROM task_states WHERE case_id=?", (document["case_id"],)
        ).fetchall()
        for state in states:
            if document["id"] in json.loads(state["evidence_ids"]):
                self.invalidate_task(
                    conn,
                    document["case_id"],
                    state["task_id"],
                    reason="引用材料的审核或适用范围已变更，请重新核验。",
                )
        self.touch(conn, document["case_id"])
        self.event(
            conn, document["case_id"], "document_review_updated", "更新资料审核信息", document["filename"]
        )

    def store_document(
        self,
        owner,
        filename,
        content,
        mime_type="",
        *,
        case_id=None,
        role="primary",
        parent_id=None,
        tags=None,
        source_url=None,
    ):
        from .parsing import parse_bounded

        if len(content) > MAX_UPLOAD:
            fail(413, "file_too_large", "文件不能超过 20 MB")
        if not content:
            fail(422, "empty_file", "上传文件不能为空")
        filename = Path(filename.replace("\\", "/")).name
        filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)[:180] or "document.bin"
        if role not in ("primary", "secondary"):
            fail(422, "invalid_role", "文件角色无效")
        if (
            not isinstance(tags or [], list)
            or len(tags or []) > 20
            or any(not isinstance(t, str) or len(t) > 80 for t in tags or [])
        ):
            fail(422, "invalid_tags", "标签需为最多 20 项的短文本数组")
        with closing(self.db.connect()) as conn:
            if case_id:
                self.own_case(conn, owner, case_id)
            if role == "secondary":
                if not parent_id:
                    fail(422, "parent_required", "关联附件必须指定主文件")
                parent = self.own_document(conn, owner, parent_id)
                if parent["case_id"] != case_id or parent["role"] != "primary":
                    fail(422, "invalid_parent", "附件与主文件必须属于同一资料范围")
            elif parent_id:
                fail(422, "invalid_parent", "主文件不能指定父文件")
        mime_type = (mime_type or mimetypes.guess_type(filename)[0] or "application/octet-stream").split(";")[
            0
        ]
        from .archive import family_key

        content_hash = hashlib.sha256(content).hexdigest()
        family = family_key(owner, filename, source_url, case_id, role, parent_id)
        with closing(self.db.connect()) as conn:
            previous = conn.execute(
                "SELECT * FROM documents WHERE owner_id=? AND family_id=? ORDER BY version DESC LIMIT 1",
                (owner, family),
            ).fetchone()
            if (
                previous
                and previous["content_hash"] == content_hash
                and previous["status"] in ("ready", "stored")
                and not previous["error"]
            ):
                manifest = json.loads(previous["manifest"])
                if (
                    role == "secondary"
                    or manifest.get("embedding_fingerprint") == self.embedding_fingerprint()
                ):
                    return self.document_dict(previous, conn=conn)
        blob = self.archive.blobs.put_bytes(owner, content)
        manifest, error = {"elements": [], "chunks": []}, None
        status = "stored" if role == "secondary" else "ready"
        if role == "primary":
            try:
                manifest = parse_bounded(
                    filename, content, mime_type, parser=os.getenv("TEA_PARSER", "basic")
                )
                if not manifest.get("elements"):
                    raise ValueError("文件没有可提取的正文；扫描件可能需要 OCR")
                if manifest.get("warnings"):
                    error = "解析提示：" + "；".join(str(item) for item in manifest["warnings"])[:450]
            except Exception as exc:
                logger.info("document_parse_failed", extra={"parser_error": type(exc).__name__})
                status, error = "failed", str(exc)[:500]
            if status == "ready":
                from .ingestion import embed_texts

                try:
                    vectors = embed_texts([c["text"] for c in manifest.get("chunks", [])])
                    if vectors is not None:
                        for chunk, vector in zip(manifest["chunks"], vectors, strict=True):
                            chunk["embedding"] = vector
                        manifest["embedding_fingerprint"] = self.embedding_fingerprint()
                except Exception:
                    embedding_error = "原文和关键词索引已保存，但向量服务暂不可用；请检查模型配置后重新上传。"
                    error = "；".join(item for item in (error, embedding_error) if item)[:500]
                    manifest["embedding_error"] = embedding_error
        id, storage_path = uid(), blob.path
        try:
            with self.db.transaction() as conn:
                if case_id:
                    self.own_case(conn, owner, case_id)
                previous = conn.execute(
                    "SELECT * FROM documents WHERE owner_id=? AND family_id=? ORDER BY version DESC LIMIT 1",
                    (owner, family),
                ).fetchone()
                if (
                    previous
                    and previous["content_hash"] == content_hash
                    and previous["status"] == status
                    and previous["error"] == error
                    and json.loads(previous["manifest"]).get("embedding_fingerprint")
                    == manifest.get("embedding_fingerprint")
                ):
                    return self.document_dict(previous, conn=conn)
                version = previous["version"] + 1 if previous else 1
                conn.execute(
                    "INSERT INTO documents(id,owner_id,case_id,parent_id,title,filename,role,status,mime_type,size,version,content_hash,storage_path,tags,source_url,error,manifest,created_at,family_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        id,
                        owner,
                        case_id,
                        parent_id,
                        Path(filename).stem,
                        filename,
                        role,
                        status,
                        mime_type,
                        len(content),
                        version,
                        content_hash,
                        str(storage_path),
                        dump(tags or []),
                        source_url,
                        error,
                        dump(manifest),
                        now(),
                        family,
                    ),
                )
                if case_id:
                    if previous:
                        for state in conn.execute(
                            "SELECT task_id,evidence_ids FROM task_states WHERE case_id=?", (case_id,)
                        ).fetchall():
                            if previous["id"] in json.loads(state["evidence_ids"]):
                                self.invalidate_task(
                                    conn,
                                    case_id,
                                    state["task_id"],
                                    reason=f"材料「{filename}」上传了新版本，请重新关联和核验。",
                                )
                    self.touch(conn, case_id)
                    self.event(
                        conn,
                        case_id,
                        "document_uploaded",
                        "上传办理材料",
                        f"{filename} · 第 {version} 版 · {status}",
                    )
                return self.document_dict(self.own_document(conn, owner, id), conn=conn)
        except BaseException:
            # Immutable blobs may already be shared by another committed document.
            raise

    def documents(
        self,
        owner,
        case_id=None,
        limit=100,
        offset=0,
        *,
        role=None,
        parent_id=None,
        q=None,
        legal_level=None,
        collection=None,
        country=None,
    ):
        from .countries import country_from_collection, document_country, resolve_country

        scope = country_from_collection(collection) if collection else None
        if country and scope and country != scope:
            fail(422, "country_mismatch", "采集组与所选国家不一致")
        country = country or scope
        with closing(self.db.connect()) as conn:
            if case_id:
                case = self.own_case(conn, owner, case_id)
                destination = resolve_country(json.loads(case["template"])["destination"])
                if destination:
                    if country and country != destination:
                        fail(422, "country_mismatch", "业务只能选择目标国家的法规")
                    country = destination
            parent = self.own_document(conn, owner, parent_id) if parent_id else None
            if parent and country is None:
                country = document_country(conn, parent_id)
            conditions = ["owner_id=?", "case_id IS ?"]
            parameters = [owner, case_id]
            if country and country != "unassigned":
                conditions.append(
                    """(EXISTS(SELECT 1 FROM document_countries dc WHERE dc.document_id=COALESCE(documents.parent_id,documents.id) AND dc.country=?)
                    OR (documents.case_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM document_countries dc
                        WHERE dc.document_id=COALESCE(documents.parent_id,documents.id))))"""
                )
                parameters.append(country)
            else:
                conditions.append(
                    "NOT EXISTS(SELECT 1 FROM document_countries dc WHERE dc.document_id=COALESCE(documents.parent_id,documents.id))"
                )
            if parent_id:
                if parent["case_id"] != case_id or parent["role"] != "primary":
                    fail(404, "not_found", "当前资料库中未找到一级资料")
                if role == "primary":
                    fail(422, "invalid_role", "一级资料下只能查询二级附件")
                conditions.extend(["parent_id=?", "role='secondary'"])
                parameters.append(parent_id)
            elif role:
                conditions.append("role=?")
                parameters.append(role)
            if collection:
                conditions.append(
                    "EXISTS(SELECT 1 FROM legal_classifications lc WHERE lc.document_id=documents.id AND lc.collection=?)"
                )
                parameters.append(collection)
            if legal_level == "attachment":
                conditions.append("role='secondary'")
            elif legal_level:
                conditions.append("role='primary'")
                primary = """COALESCE((SELECT lc.level='primary' FROM legal_classifications lc
                    JOIN document_countries dc ON dc.document_id=lc.document_id
                    WHERE lc.document_id=documents.id AND lc.collection='country:'||dc.country),
                    EXISTS(SELECT 1 FROM legal_classifications lc WHERE lc.document_id=documents.id AND lc.level='primary')
                    OR NOT EXISTS(SELECT 1 FROM legal_classifications lc WHERE lc.document_id=documents.id))"""
                conditions.append(f"({primary})" if legal_level == "primary" else f"NOT ({primary})")
            if q and q.strip():
                pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                conditions.append("(title LIKE ? ESCAPE '\\' OR filename LIKE ? ESCAPE '\\')")
                parameters.extend([pattern, pattern])
            where = " AND ".join(conditions)
            rows = conn.execute(
                f"SELECT {DOCUMENT_SUMMARY} FROM documents WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            total = conn.execute(f"SELECT COUNT(*) n FROM documents WHERE {where}", parameters).fetchone()[
                "n"
            ]
            return {"items": self.document_summaries(rows, conn), "total": total}

    def document_detail(self, owner, document_id, *, element_offset=0, chunk_offset=0, limit=100):
        with closing(self.db.connect()) as conn:
            row = self.own_document(conn, owner, document_id)
            manifest = json.loads(row["manifest"])
            elements, chunks = manifest.get("elements", []), manifest.get("chunks", [])
            return {
                "document": self.document_dict(row),
                "elements": elements[element_offset : element_offset + limit],
                "chunks": [
                    {key: value for key, value in chunk.items() if key != "embedding"}
                    for chunk in chunks[chunk_offset : chunk_offset + limit]
                ],
                "element_total": len(elements),
                "chunk_total": len(chunks),
                "element_offset": element_offset,
                "chunk_offset": chunk_offset,
                "limit": limit,
                "parser": manifest.get("parser"),
                "warnings": manifest.get("warnings", []),
            }

    def search(
        self,
        owner,
        query,
        case_id=None,
        limit=5,
        as_of=None,
        collection=None,
        country=None,
        document_ids=None,
    ):
        historical = as_of is not None
        as_of = as_of or datetime.now(timezone.utc).date()
        with closing(self.db.connect()) as conn:
            from .countries import country_from_collection, resolve_country

            scope = country_from_collection(collection) if collection else None
            if country and scope and country != scope:
                fail(422, "country_mismatch", "检索国家与采集组不一致")
            country = country or scope
            if case_id:
                case = self.own_case(conn, owner, case_id)
                destination = resolve_country(json.loads(case["template"])["destination"])
                if destination:
                    if country and country != destination:
                        fail(422, "country_mismatch", "业务只能检索目标国家的法规")
                    country = destination
        return self.search_eligible(
            owner, query, case_id, limit, as_of, collection, country, document_ids, historical=historical
        )

    def search_eligible(
        self,
        owner,
        query,
        case_id,
        limit,
        as_of,
        collection=None,
        country=None,
        document_ids=None,
        *,
        historical=False,
    ):
        """Shared eligible-document search; callers resolve their allowed country scope first."""
        from .ingestion import embed_texts, lexical_search, rerank_chunks

        with closing(self.db.connect()) as conn:
            if case_id:
                self.own_case(conn, owner, case_id)
            rows, metadata, limited = self.archive.retrieval_rows(
                conn,
                owner,
                case_id,
                collection,
                as_of,
                historical,
                country=None if country == "unassigned" else country,
                document_ids=document_ids,
            )
        chunks = []
        for row in rows:
            for chunk in json.loads(row["manifest"]).get("chunks", []):
                chunks.append(
                    {
                        **chunk,
                        "document_id": row["id"],
                        "title": row["title"],
                        "source_url": row["source_url"],
                        "document_version": row["version"],
                        **metadata[row["id"]],
                    }
                )
        rerank_enabled = self.rerank_fingerprint() is not None
        candidate_limit = min(limit * 3, 60) if rerank_enabled else limit
        hits = lexical_search(query, chunks, limit=candidate_limit)
        mode, warning = "lexical", None
        fingerprint = self.embedding_fingerprint()
        indexed = []
        if fingerprint:
            for row in rows:
                manifest = json.loads(row["manifest"])
                if manifest.get("embedding_fingerprint") == fingerprint:
                    indexed.extend(c for c in chunks if c["document_id"] == row["id"] and c.get("embedding"))
            if indexed:
                try:
                    query_vector = embed_texts([query])[0]
                    query_norm = math.sqrt(sum(x * x for x in query_vector))
                    semantic = []
                    for chunk in indexed:
                        vector = chunk["embedding"]
                        if len(vector) != len(query_vector):
                            raise ValueError("embedding dimensions changed")
                        norm = math.sqrt(sum(x * x for x in vector))
                        score = (
                            sum(x * y for x, y in zip(vector, query_vector, strict=True))
                            / (query_norm * norm)
                            if query_norm and norm
                            else 0
                        )
                        if score >= 0.3:
                            semantic.append({**chunk, "score": score})
                    semantic.sort(key=lambda c: c["score"], reverse=True)
                    combined = {}
                    for ranking in (hits, semantic[: limit * 3]):
                        for rank, chunk in enumerate(ranking):
                            key = (chunk["document_id"], chunk["id"])
                            current = combined.setdefault(key, {**chunk, "fusion_score": 0})
                            current["fusion_score"] += 1 / (60 + rank + 1)
                    hits = sorted(combined.values(), key=lambda c: c["fusion_score"], reverse=True)[
                        :candidate_limit
                    ]
                    mode = "hybrid"
                except Exception:
                    warning = "向量检索暂不可用，本次已回退到关键词检索。"
            elif chunks:
                warning = "当前材料尚未使用此向量模型建立索引，本次使用关键词检索。"
        reranked = False
        if rerank_enabled and hits:
            try:
                ranked = rerank_chunks(query, hits, limit)
                if ranked is not None:
                    hits, reranked = ranked, True
            except ValueError:
                warning = "；".join(filter(None, [warning, "重排服务暂不可用，本次保留原检索顺序。"]))
        hits = hits[:limit]
        if limited:
            warning = "；".join(
                filter(
                    None, [warning, "当前检索范围超过单机检索预算，请按采集组缩小范围；结果不代表全部资料"]
                )
            )
        result = {
            "items": [
                {
                    "document_id": hit["document_id"],
                    "title": hit["title"],
                    "excerpt": hit["text"][:1500],
                    "locator": hit.get("locator", {}),
                    "source_url": hit.get("source_url"),
                    "document_version": hit["document_version"],
                    "metadata": hit["metadata"],
                    "metadata_revision": hit["metadata_revision"],
                }
                for hit in hits
            ],
            "mode": mode,
            "reranked": reranked,
        }
        if warning:
            result["warning"] = warning
        return result

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

    def messages(self, owner, case_id, stage_id=None, limit=100, offset=0):
        with closing(self.db.connect()) as conn:
            self.own_case(conn, owner, case_id)
            rows = conn.execute(
                "SELECT * FROM messages WHERE case_id=? AND (? IS NULL OR stage_id=?) ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (case_id, stage_id, stage_id, limit, offset),
            ).fetchall()
            total = conn.execute(
                "SELECT COUNT(*) n FROM messages WHERE case_id=? AND (? IS NULL OR stage_id=?)",
                (case_id, stage_id, stage_id),
            ).fetchone()["n"]
            return {
                "items": [
                    {
                        key: json.loads(row[key]) if key in ("citations", "trace") else row[key]
                        for key in (
                            "id",
                            "role",
                            "content",
                            "stage_id",
                            "task_id",
                            "created_at",
                            "citations",
                            "trace",
                        )
                    }
                    for row in reversed(rows)
                ],
                "total": total,
            }

    def run_workflow(self, owner, template, stage, content, tasks, **options):
        from .assistant import run_workflow

        agent = stage.get("analysis") or template["agent"]
        if (agent.get("legal") or {}).get("enabled"):
            from .legal_models import LegalFact

            case_id = options.get("case_id")
            if case_id:
                with self.db.transaction() as conn:
                    self.own_case(conn, owner, case_id)
                    self.legal.sync_facts(conn, owner, case_id)
                    facts = self.legal.facts(conn, case_id)
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
                    output.get("retrieval_mode") == "hybrid"
                    or any(item.get("mode") == "hybrid" for item in output.get("coverage", []))
                    for output in outputs
                )
                return {
                    **result,
                    "checks": [],
                    "stage_id": stage["id"],
                    "retrieval_mode": "hybrid" if hybrid else "lexical",
                }

            legal_options = {
                key: value
                for key, value in options.items()
                if key
                in ("case_id", "emit", "check_cancelled", "document_ids", "stop_after_step_id", "resume")
            }
            if checkpoint:
                legal_options["on_checkpoint"] = lambda step_id, result, state: checkpoint(
                    step_id, adapt(result), state
                )
            return adapt(self.legal.execute_engine(owner, template, stage, content, facts, **legal_options))
        return run_workflow(self, owner, template, stage, content, tasks, **options)

    def prepare_message(self, owner, case_id, payload):
        payload_hash = hashlib.sha256(dump(payload).encode()).hexdigest()
        with closing(self.db.connect()) as conn:
            case = self.case_detail(owner, case_id, conn)
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
            task_id = payload.get("task_id")
            if task_id and not any(t["id"] == task_id for t in stage["tasks"]):
                fail(422, "invalid_task", "所选任务不属于当前节点")
            selected = next((t for t in stage["tasks"] if t["id"] == task_id), stage["tasks"][0])
            recent = conn.execute(
                "SELECT role,content FROM messages WHERE case_id=? AND stage_id=? ORDER BY created_at DESC LIMIT 12",
                (case_id, stage["id"]),
            ).fetchall()
            history = [
                {"role": message["role"], "content": message["content"][:3000]}
                for message in reversed(recent)
            ]
        return {
            "case": case,
            "stage": stage,
            "selected": selected,
            "history": history,
            "task_id": task_id,
            "payload_hash": payload_hash,
        }

    def post_message(self, owner, case_id, payload, *, emit=None, check_cancelled=None):
        # Authenticate before inspecting the in-flight registry. Both JSON and
        # streaming callers use the same claim and persisted idempotency key.
        prepared = self.prepare_message(owner, case_id, payload)
        if "response" in prepared:
            return prepared["response"]
        with self._chat_lock:
            if case_id in self._active_chats:
                fail(409, "message_in_progress", "此业务正在生成回复，请稍后重试。")
            if len(self._active_chats) >= 4:
                fail(503, "chat_busy", "当前对话较多，请稍后重试。")
            self._active_chats.add(case_id)
        try:
            # Reload after claiming: a prior request may have committed while
            # this request was reading its initial snapshot.
            prepared = self.prepare_message(owner, case_id, payload)
            if "response" in prepared:
                return prepared["response"]
            return self.finish_message(owner, case_id, payload, prepared, emit, check_cancelled)
        finally:
            with self._chat_lock:
                self._active_chats.discard(case_id)

    def finish_message(self, owner, case_id, payload, prepared, emit, check_cancelled):
        case, stage = prepared["case"], prepared["stage"]
        task_id, payload_hash = prepared["task_id"], prepared["payload_hash"]
        if check_cancelled:
            check_cancelled()
        agent = stage.get("analysis") or case["template"]["agent"]
        legal_enabled = bool((agent.get("legal") or {}).get("enabled"))
        if legal_enabled:
            question = payload["content"]
            with closing(self.db.connect()) as conn:
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
                question = json.loads(retry["payload"])["question"]
            elif prior:
                prior = json.loads(prior["payload"])
                if prior["status"] == "needs_input":
                    question = prior["question"] + "\n\n用户补充说明：" + question
            if len(question) > 20000:
                fail(422, "analysis_context_limit", "当前问题与补充说明过长，请在本节点分析中整理事实后继续")

            def legal_emit(event, data):
                if emit and event in ("status", "delta"):
                    emit(event, data)

            analysis = self.legal.analyze(
                owner,
                case_id,
                {
                    "stage_id": stage["id"],
                    "question": question,
                    "client_request_id": "chat:" + payload["client_message_id"],
                    "_chat_content": payload["content"],
                },
                emit=legal_emit if emit else None,
                check_cancelled=check_cancelled,
            )
            result = analysis["result"]
            if emit:
                emit("delta", {"text": result["output"]})
        else:
            result = self.run_workflow(
                owner,
                case["template"],
                stage,
                payload["content"],
                [prepared["selected"]],
                case_id=case_id,
                **({"emit": emit, "check_cancelled": check_cancelled} if emit else {}),
                context={
                    "facts": case["facts"],
                    "mode": next(
                        (mode for mode in case["template"]["modes"] if mode["id"] == case["mode_id"]), None
                    ),
                    "recent_messages": prepared["history"],
                    "node_analysis": self.studio.node_context(owner, case, stage["id"]),
                },
            )
        if emit and any(step["status"] == "failed" for step in result["trace"]):
            fail(502, "assistant_failed", "回复未能完成，已收到的内容仅供参考。请重试。")
        output, trace, citations = result["output"], result["trace"], result["citations"]
        with self.db.transaction() as conn:
            if check_cancelled:
                check_cancelled()
            current = self.case_detail(owner, case_id, conn)
            if legal_enabled:
                legal_record = self.legal.own(conn, owner, case_id, analysis["id"])
                if legal_record.get("_snapshot") != self.legal.snapshot(conn, owner, current):
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
                self.insert_message(conn, case_id, stage["id"], task_id, "user", payload["content"], [], []),
                self.insert_message(
                    conn, case_id, stage["id"], task_id, "assistant", output, citations, trace
                ),
            ]
            conn.execute(
                "INSERT INTO message_requests VALUES (?,?,?,?)",
                (case_id, payload["client_message_id"], payload_hash, dump(messages)),
            )
            self.touch(conn, case_id)
            return {"messages": messages, "case": self.case_detail(owner, case_id, conn)}

    def artifact_dict(self, row):
        result = {
            key: row[key]
            for key in ("id", "case_id", "task_id", "title", "content", "version", "created_at", "updated_at")
        }
        result["confirmed"] = bool(row["confirmed"])
        return result

    def own_artifact(self, conn, owner, id):
        row = conn.execute("SELECT * FROM artifacts WHERE id=? AND owner_id=?", (id, owner)).fetchone()
        if not row:
            fail(404, "not_found", "未找到文稿")
        return row

    def create_artifact(self, owner, case_id, payload):
        with self.db.transaction() as conn:
            case = self.case_detail(owner, case_id, conn)
            _, task = self.get_task(case, payload["task_id"])
            template = task.get("output_template") or "# " + task["title"] + "\n\n" + "\n\n".join(
                f"{f['label']}：{{{{{f['key']}}}}}" for f in task["fields"]
            )
            facts = {f["key"]: f["value"] for f in case["facts"]}
            facts.update(task["values"])
            content = re.sub(
                r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}",
                lambda match: str(facts.get(match[1]) or f"【待填写：{match[1]}】"),
                template,
            )
            id, at = uid(), now()
            conn.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    id,
                    owner,
                    case_id,
                    task["id"],
                    payload.get("title") or task["title"] + " · 草稿",
                    content,
                    1,
                    0,
                    at,
                    at,
                ),
            )
            artifact = self.artifact_dict(self.own_artifact(conn, owner, id))
            conn.execute("INSERT INTO artifact_versions VALUES (?,?,?)", (id, 1, dump(artifact)))
            self.touch(conn, case_id)
            self.event(conn, case_id, "artifact_created", "生成材料草稿", artifact["title"])
            return artifact

    def patch_artifact(self, owner, id, payload):
        with self.db.transaction() as conn:
            row = self.own_artifact(conn, owner, id)
            if row["version"] != payload["expected_version"]:
                fail(409, "stale_version", "文稿已有新版本，请刷新后再修改")
            conn.execute(
                "UPDATE artifacts SET title=?,content=?,version=version+1,confirmed=0,updated_at=? WHERE id=?",
                (payload["title"], payload["content"], now(), id),
            )
            artifact = self.artifact_dict(self.own_artifact(conn, owner, id))
            conn.execute(
                "INSERT INTO artifact_versions VALUES (?,?,?)", (id, artifact["version"], dump(artifact))
            )
            self.invalidate_task(
                conn, row["case_id"], row["task_id"], reason="关联文稿已修改，请复核当前任务。"
            )
            self.touch(conn, row["case_id"])
            self.event(
                conn,
                row["case_id"],
                "artifact_updated",
                "保存文稿版本",
                f"{artifact['title']} · 第 {artifact['version']} 版",
            )
            return artifact

    def confirm_artifact(self, owner, id, version):
        with self.db.transaction() as conn:
            row = self.own_artifact(conn, owner, id)
            if row["version"] != version:
                fail(409, "stale_version", "文稿已有新版本，请刷新后确认")
            conn.execute("UPDATE artifacts SET confirmed=1,updated_at=? WHERE id=?", (now(), id))
            artifact = self.artifact_dict(self.own_artifact(conn, owner, id))
            conn.execute(
                "UPDATE artifact_versions SET payload=? WHERE artifact_id=? AND version=?",
                (dump(artifact), id, version),
            )
            self.touch(conn, row["case_id"])
            return artifact

    def source_dict(self, row, conn=None, latest_run=...):
        from .models import CollectionPlan

        if conn is None:
            with closing(self.db.connect()) as conn:
                return self.source_dict(row, conn)
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
            latest_run = self.collection.run_dict(conn, latest) if latest else None
        result["latest_run"] = latest_run
        return result

    def sources(self, owner, limit=100, offset=0):
        with closing(self.db.connect()) as conn:
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
                counts = self.collection.run_counts(conn, [row["id"] for row in runs])
                latest = {
                    row["source_id"]: self.collection.run_dict(conn, row, counts[row["id"]]) for row in runs
                }
            return {
                "items": [self.source_dict(row, conn, latest.get(row["id"])) for row in rows],
                "total": total,
            }

    def save_source(self, owner, payload, id=None, conn=None):
        from .models import CollectionPlan
        from .ingestion import _validated_url

        if conn is None:
            with self.db.transaction() as conn:
                return self.save_source(owner, payload, id, conn)
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
            values = self.source_dict(row, conn) | {k: v for k, v in payload.items() if v is not None}
            self.collection.validate_plan(conn, owner, values["plan"])
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
            self.collection.validate_plan(conn, owner, plan)
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
        return self.source_dict(conn.execute("SELECT * FROM sources WHERE id=?", (id,)).fetchone(), conn)

    def due_sources(self):
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                """SELECT id,owner_id,last_attempt_at,interval_hours,status FROM sources s WHERE enabled=1
                AND NOT EXISTS(SELECT 1 FROM collection_runs r WHERE r.source_id=s.id AND r.status IN ('queued','running'))
                AND (last_attempt_at IS NULL OR julianday(last_attempt_at)+interval_hours/24.0<=julianday('now'))
                ORDER BY COALESCE(last_attempt_at,''),id LIMIT 100"""
            ).fetchall()
        current = datetime.now(timezone.utc)
        return [
            {"id": r["id"], "owner_id": r["owner_id"]}
            for r in rows
            if r["last_attempt_at"] is None
            or datetime.fromisoformat(r["last_attempt_at"]) + timedelta(hours=r["interval_hours"]) <= current
        ]

    def fetch_source(self, owner_id, source_id, payload=None):
        """Compatibility entry point; all collection uses the durable queue."""
        return self.collection.enqueue(owner_id, source_id, payload or {"client_request_id": uid()})
