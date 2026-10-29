import json
import logging
import math
import re
from contextlib import closing
from datetime import datetime, timezone

from app.contexts.documents.domain.documents import DOCUMENT_SUMMARY
from app.contexts.documents.domain.metadata import eligible
from app.contexts.workflows.domain import rules
from app.contexts.workflows.infrastructure.defaults import RETIRED_TEMPLATE_ID
from app.shared.domain.errors import check, fail
from app.shared.domain.values import dump, now, uid

logger = logging.getLogger(__name__)


class WorkspaceService:
    def __init__(self, app):
        self.app = app

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
            with closing(self.app.db.connect()) as db:
                return self.app.case_detail(owner, case_id, db)
        row = self.app.own_case(conn, owner, case_id)
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
                        "field_sources": json.loads(state["field_sources_json"]),
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
        docs = self.app.document_summaries(
            conn.execute(
                f"SELECT {DOCUMENT_SUMMARY} FROM documents WHERE case_id=? ORDER BY created_at DESC LIMIT 200",
                (case_id,),
            ).fetchall(),
            conn,
        )
        artifacts = [
            self.app.artifact_dict(r)
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
        with closing(self.app.db.connect()) as conn:
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
                detail = self.app.case_detail(owner, id, conn)
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
        with self.app.db.transaction() as conn:
            template = self.app.template(owner, values["template_id"], conn)
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
            self.app.insert_message(
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
            self.app.event(
                conn, id, "case_created", "建立出口业务", f"已冻结流程模板第 {template['version']} 版。"
            )
            return self.app.case_detail(owner, id, conn)

    def get_task(self, case, task_id, *, require_unlocked=True):
        return rules.get_task(case, task_id, require_unlocked=require_unlocked)

    def check_revision(self, task, expected):
        return rules.check_revision(task, expected)

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
                self.app.event(conn, case_id, "review_required", "任务需要复核", reason)

    def normalize_values(self, task, supplied):
        return rules.normalize_values(task, supplied)

    def patch_task(self, owner, case_id, task_id, payload, *, conn=None, field_sources=None):
        if conn is None:
            with self.app.db.transaction() as db:
                return self.patch_task(owner, case_id, task_id, payload, conn=db, field_sources=field_sources)
        field_sources = field_sources or {}
        case = self.app.case_detail(owner, case_id, conn)
        _, task = self.app.get_task(case, task_id)
        self.app.check_revision(task, payload["expected_revision"])
        definitions = {f["key"]: f for f in task["fields"]}
        values = self.app.normalize_values(task, payload["values"])
        evidence = payload.get("evidence_ids")
        if evidence is None:
            evidence = task["evidence_ids"]
        evidence = list(dict.fromkeys(evidence))
        for doc_id in evidence:
            doc = self.app.own_document(conn, owner, doc_id)
            if doc["case_id"] != case_id or doc["role"] != "primary" or doc["status"] != "ready":
                fail(422, "invalid_evidence", "核验材料必须是本业务中解析成功的主文件")
        if values == task["values"] and evidence == task["evidence_ids"]:
            return case
        self.app.invalidate_task(
            conn, case_id, task_id, reason=f"「{task['title']}」的资料已修改，旧确认不再适用。"
        )
        provenance = dict(task["field_sources"])
        for key, value in values.items():
            if value != task["values"].get(key):
                provenance.pop(key, None)
                if key in field_sources:
                    provenance[key] = field_sources[key]
        conn.execute(
            "UPDATE task_states SET values_json=?,evidence_ids=?,field_sources_json=? WHERE case_id=? AND task_id=?",
            (dump(values), dump(evidence), dump(provenance), case_id, task_id),
        )
        for key, value in values.items():
            conn.execute(
                "INSERT INTO facts VALUES (?,?,?,?,?,?,?) ON CONFLICT(case_id,key) DO UPDATE SET label=excluded.label,value=excluded.value,source=excluded.source,confirmed=0,updated_at=excluded.updated_at",
                (case_id, key, definitions[key]["label"], dump(value), f"task:{task_id}", 0, now()),
            )
        self.app.touch(conn, case_id)
        self.app.event(
            conn,
            case_id,
            "task_updated",
            "更新任务资料",
            f"已保存「{task['title']}」的当前资料，等待核验。",
        )
        return self.app.case_detail(owner, case_id, conn)

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
            metadata = self.app.catalog.metadata_for(doc["id"], source_url=doc["source_url"], conn=conn)[
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
        with self.app.db.transaction() as conn:
            case = self.app.case_detail(owner, case_id, conn)
            _, task = self.app.get_task(case, task_id)
            self.app.check_revision(task, payload["expected_revision"])
            if task["status"] == "completed" and not task["needs_review"]:
                return case
            checks = self.app.checks(conn, task, payload["confirm"])
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
            self.app.touch(conn, case_id)
            self.app.event(
                conn,
                case_id,
                "verification_passed" if passed else "verification_needs_input",
                "任务核验通过" if passed else "核验发现待补充事项",
                f"「{task['title']}」：" + "；".join(c["reason"] for c in checks if c["status"] != "passed"),
            )
            if passed:
                current = self.app.case_detail(owner, case_id, conn)
                stage, _ = self.app.get_task(current, task_id)
                self.app.node_workspace.queue_guide(
                    conn,
                    current,
                    stage["id"],
                    f"task:{task_id}:{revision}",
                    f"完成「{task['title']}」后的分析",
                )
            return self.app.case_detail(owner, case_id, conn)
