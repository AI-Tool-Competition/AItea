"""Owner-scoped test snapshots and checkpoints around the shared workflow runner."""

import copy
import hashlib
import json
import os
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone

from .assistant import reuse_trace
from .catalog import DocumentMetadata, eligible
from .db import dump, now, uid
from .streaming import StreamCancelled

MAX_SAMPLES = 100
MAX_RUNS = 100
MAX_RUN_BYTES = 16 * 1024 * 1024
MAX_OWNER_BYTES = 64 * 1024 * 1024
RUNNER_VERSION = 2


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()


class WorkflowDebugger:
    def __init__(self, service):
        self.service, self.db = service, service.db
        with self.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=7").fetchone():
                conn.execute("""CREATE TABLE debug_samples(
                    owner_id TEXT NOT NULL REFERENCES users(id), id TEXT NOT NULL,
                    template_id TEXT NOT NULL, stage_id TEXT NOT NULL, name TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL,
                    scope_hash TEXT NOT NULL, PRIMARY KEY(owner_id,template_id,id))""")
                conn.execute(
                    "CREATE INDEX debug_samples_scope ON debug_samples(owner_id,template_id,stage_id,updated_at,id)"
                )
                conn.execute("""CREATE TABLE debug_runs(
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                    template_id TEXT NOT NULL, stage_id TEXT NOT NULL, sample_id TEXT,
                    client_id TEXT NOT NULL, request_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL, status TEXT NOT NULL, payload TEXT,
                    checkpoints TEXT NOT NULL, signatures TEXT NOT NULL,
                    UNIQUE(owner_id,client_id))""")
                conn.execute(
                    "CREATE INDEX debug_runs_scope ON debug_runs(owner_id,template_id,stage_id,created_at,id)"
                )
                conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (7, now()))

    def inputs(self, payload):
        from .service import fail

        template = payload.template.model_dump()
        stage_id = payload.stage_id or template["stages"][0]["id"]
        stage = next((stage for stage in template["stages"] if stage["id"] == stage_id), None)
        if stage is None:
            fail(422, "invalid_stage", "调试节点不存在，请重新选择")
        if payload.task_values.keys() - {task["id"] for task in stage["tasks"]}:
            fail(422, "invalid_task", "样例包含不属于当前节点的任务，请重新载入样例或调整配置")
        tasks = []
        for definition in stage["tasks"]:
            task = {**definition, "values": {}, "evidence_ids": [], "confirmed": False}
            task["values"] = self.service.normalize_values(task, payload.task_values.get(task["id"], {}))
            tasks.append(task)
        if len(dump(payload.task_values).encode()) > 64 * 1024:
            fail(422, "sample_too_large", "样例字段内容合计不能超过 64 KB")
        for expectation in payload.expectations:
            if expectation.kind == "check":
                relevant = [
                    task for task in tasks if not expectation.task_id or task["id"] == expectation.task_id
                ]
                checks = {"reported_weight_conflict"}
                for task in relevant:
                    checks.update(field["key"] for field in task["fields"])
                    checks.update(("document", "confirmation"))
                if not relevant or expectation.check_id not in checks:
                    fail(422, "invalid_expectation", "预期检查项不属于当前节点，请调整预期或配置")
        return template, stage, tasks

    def document_scope(self, conn, owner, document_ids):
        """Bind selection, immutable versions and mutable eligibility without loading file bodies."""
        from .service import fail

        sql = """SELECT d.id,d.version,d.content_hash,d.status,d.filename,d.title,d.source_url,d.family_id,
            COALESCE(m.revision,0) metadata_revision,m.payload metadata_payload,length(CAST(d.manifest AS BLOB)) manifest_size,
            (SELECT MAX(v.version) FROM documents v WHERE v.owner_id=d.owner_id AND v.family_id=d.family_id) family_version
            FROM documents d LEFT JOIN document_metadata m ON m.document_id=d.id
            WHERE d.owner_id=? AND d.case_id IS NULL AND d.role='primary' """
        parameters = [owner]
        if document_ids is None:
            sql += " AND d.status='ready'"
        elif not document_ids:
            return [], [], digest([])
        else:
            document_ids = sorted(set(document_ids))
            sql += " AND d.id IN (" + ",".join("?" for _ in document_ids) + ")"
            parameters.extend(document_ids)
        rows = conn.execute(sql + " ORDER BY d.id LIMIT 201", parameters).fetchall()
        if len(rows) > 200:
            fail(422, "document_scope_too_large", "测试最多使用 200 份资料，请选择较小的资料范围")
        if document_ids is not None and len(rows) != len(document_ids):
            fail(404, "not_found", "未找到部分测试资料，请确认资料属于当前账号的资料库")
        if any(row["status"] != "ready" for row in rows):
            fail(409, "document_not_ready", "测试资料尚未准备好，请等待解析完成或重新选择")
        if sum(row["manifest_size"] for row in rows) > 64 * 1024 * 1024:
            fail(422, "document_scope_too_large", "测试资料的索引超过 64 MB，请选择较小的资料范围")
        ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in ids)
        classifications = {}
        manifests = {}
        if ids:
            for classification in conn.execute(
                f"SELECT document_id,collection,level,revision FROM legal_classifications WHERE document_id IN ({placeholders}) ORDER BY collection",
                ids,
            ):
                classifications.setdefault(classification["document_id"], []).append(dict(classification))
            # Hash bounded stored manifests one at a time: index/parse updates are
            # execution inputs even when the original file version is unchanged.
            for manifest in conn.execute(
                f"SELECT id,manifest FROM documents WHERE id IN ({placeholders})", ids
            ):
                manifests[manifest["id"]] = hashlib.sha256(manifest["manifest"].encode()).hexdigest()
        versions, details = [], []
        today = datetime.now(timezone.utc).date()
        for row in rows:
            versions.append(
                {
                    "document_id": row["id"],
                    "version": row["version"],
                    "metadata_revision": row["metadata_revision"],
                }
            )
            item = dict(row)
            default = (
                DocumentMetadata(kind="law", review_status="draft")
                if row["source_url"]
                else DocumentMetadata()
            )
            metadata = (
                json.loads(row["metadata_payload"])
                if row["metadata_payload"]
                else default.model_dump(mode="json")
            )
            item["eligible"] = eligible(metadata, today)
            item["classifications"] = classifications.get(row["id"], [])
            item["manifest_hash"] = manifests[row["id"]]
            details.append(item)
        return [row["id"] for row in rows], versions, digest(details)

    def sample(self, owner, template_id, sample_id, conn=None, *, private=False):
        from .service import fail

        if conn is None:
            with closing(self.db.connect()) as db:
                return self.sample(owner, template_id, sample_id, db, private=private)
        row = conn.execute(
            "SELECT * FROM debug_samples WHERE owner_id=? AND template_id=? AND id=?",
            (owner, template_id, sample_id),
        ).fetchone()
        if not row:
            fail(404, "not_found", "未找到测试样例")
        return dict(row) if private else json.loads(row["payload"])

    def samples(self, owner, template_id, stage_id=None, limit=50, offset=0):
        with closing(self.db.connect()) as conn:
            where = "owner_id=? AND template_id=?"
            params = [owner, template_id]
            if stage_id:
                where += " AND stage_id=?"
                params.append(stage_id)
            rows = conn.execute(
                f"SELECT id,name,stage_id,revision,updated_at FROM debug_samples WHERE {where} ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM debug_samples WHERE {where}", params).fetchone()[0]
        return {"items": [dict(row) for row in rows], "total": total}

    def save_sample(self, owner, template_id, sample_id, payload):
        from .service import fail

        if template_id != payload.template.id or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", sample_id):
            fail(422, "invalid_sample_id", "样例编号或流程编号无效")
        template, stage, _ = self.inputs(payload)
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT revision FROM debug_samples WHERE owner_id=? AND template_id=? AND id=?",
                (owner, template_id, sample_id),
            ).fetchone()
            revision = existing["revision"] if existing else 0
            if revision != payload.expected_revision:
                fail(409, "stale_sample", "样例已有更新，请重新载入后再保存；当前输入仍可保留")
            if (
                not existing
                and conn.execute("SELECT COUNT(*) FROM debug_samples WHERE owner_id=?", (owner,)).fetchone()[
                    0
                ]
                >= MAX_SAMPLES
            ):
                fail(409, "sample_limit", "最多保存 100 个测试样例，请删除不再使用的样例")
            document_ids, versions, scope_hash = self.document_scope(conn, owner, payload.document_ids)
            at = now()
            result = payload.model_dump(exclude={"expected_revision"}) | {
                "id": sample_id,
                "template_id": template_id,
                "template": template,
                "stage_id": stage["id"],
                "document_ids": document_ids,
                "document_versions": versions,
                "revision": revision + 1,
                "updated_at": at,
            }
            conn.execute(
                """INSERT INTO debug_samples VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(owner_id,template_id,id) DO UPDATE SET stage_id=excluded.stage_id,name=excluded.name,
                revision=excluded.revision,updated_at=excluded.updated_at,payload=excluded.payload,scope_hash=excluded.scope_hash""",
                (
                    owner,
                    sample_id,
                    template_id,
                    stage["id"],
                    payload.name,
                    revision + 1,
                    at,
                    dump(result),
                    scope_hash,
                ),
            )
        return result

    def delete_sample(self, owner, template_id, sample_id, revision):
        from .service import fail

        with self.db.transaction() as conn:
            row = self.sample(owner, template_id, sample_id, conn, private=True)
            if row["revision"] != revision:
                fail(409, "stale_sample", "样例已有更新，请重新载入后再删除")
            conn.execute(
                "DELETE FROM debug_samples WHERE owner_id=? AND template_id=? AND id=?",
                (owner, template_id, sample_id),
            )

    def run(self, owner, run_id, conn=None, *, private=False):
        from .service import fail

        if conn is None:
            with self.db.transaction() as db:
                self.expire_runs(db, owner)
                return self.run(owner, run_id, db, private=private)
        row = conn.execute("SELECT * FROM debug_runs WHERE owner_id=? AND id=?", (owner, run_id)).fetchone()
        if not row:
            fail(404, "not_found", "未找到试运行记录，记录可能已超过保留范围")
        if private:
            return dict(row)
        if row["status"] == "running":
            fail(409, "debug_in_progress", "试运行尚未结束，请稍后查看记录")
        return json.loads(row["payload"])

    def runs(self, owner, template_id, stage_id=None, limit=50, offset=0):
        with self.db.transaction() as conn:
            self.expire_runs(conn, owner)
            where = "owner_id=? AND template_id=? AND status!='running'"
            params = [owner, template_id]
            if stage_id:
                where += " AND stage_id=?"
                params.append(stage_id)
            rows = conn.execute(
                f"""SELECT id,created_at,status,stage_id,sample_id,
                CASE WHEN EXISTS(SELECT 1 FROM json_each(payload,'$.evaluations') e WHERE json_extract(e.value,'$.status')='failed') THEN 'failed'
                    WHEN EXISTS(SELECT 1 FROM json_each(payload,'$.evaluations') e WHERE json_extract(e.value,'$.status')='manual') THEN 'manual'
                    WHEN json_array_length(payload,'$.evaluations')>0 THEN 'passed' ELSE 'none' END evaluation_status
                FROM debug_runs WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?""",
                [*params, limit, offset],
            ).fetchall()
            total = conn.execute(f"SELECT COUNT(*) FROM debug_runs WHERE {where}", params).fetchone()[0]
        return {"items": [dict(row) for row in rows], "total": total}

    def signatures(self, template, stage, tasks, request, scope_hash):
        agent = stage.get("analysis") or template["agent"]
        prefix = {
            "template_id": template["id"],
            "stage_id": stage["id"],
            "stage_name": stage["name"],
            "origin": template["origin"],
            "destination": template["destination"],
            "tasks": tasks,
            "input": request["input"],
            "scope_hash": scope_hash,
            "date": datetime.now(timezone.utc).date().isoformat(),
            "mode": self.service.capabilities["assistant_mode"],
            "provider": digest([os.getenv("LLM_MODEL"), os.getenv("LLM_BASE_URL"), os.getenv("LLM_API_KEY")]),
            "embedding": self.service.embedding_fingerprint(),
            "embedding_provider": digest(
                [
                    os.getenv("EMBEDDING_MODEL"),
                    os.getenv("EMBEDDING_BASE_URL"),
                    os.getenv("EMBEDDING_API_KEY"),
                ]
            ),
            "runner": RUNNER_VERSION,
        }
        signatures, previous = {}, digest(prefix)
        for step in agent["steps"]:
            config = {"step": step}
            if step["type"] == "retrieve":
                config["retrieval_limit"] = agent["retrieval_limit"]
            if step["type"] == "guide":
                config.update(instructions=agent["instructions"], language=agent["language"])
            previous = digest([previous, config])
            signatures[step["id"]] = previous
        return signatures

    def evaluations(self, expectations, result):
        evaluations = []
        valid_run = result.get("status") in ("completed", "paused")
        guide = next(
            (
                step
                for step in reversed(result["trace"])
                if step.get("type") == "guide" and step["status"] in ("completed", "cached")
            ),
            None,
        )
        valid_answer = bool(
            valid_run and guide and guide.get("output_data", {}).get("content") == result["output"]
        )
        for item in expectations:
            kind, text = item["kind"], item.get("text") or ""
            passed, actual, label = False, "", text
            if kind in ("contains", "not_contains"):
                found = text in result["output"]
                passed = found if kind == "contains" else not found
                label = ("应包含：" if kind == "contains" else "不应包含：") + text
                actual = "回答中包含此内容" if found else "回答中未包含此内容"
                if not valid_answer:
                    evaluations.append(
                        {
                            "id": item["id"],
                            "label": label,
                            "status": "manual",
                            "actual": "未评估：本次尚无有效的完整回答，请完成指导步骤后再检查",
                        }
                    )
                    continue
            elif kind == "check":
                checks = [
                    check
                    for check in result["checks"]
                    if check["id"] == item["check_id"]
                    and (not item.get("task_id") or check.get("task_id") == item["task_id"])
                ]
                passed = bool(checks) and all(check["status"] == item["status"] for check in checks)
                names = {"passed": "通过", "missing": "缺少资料", "conflict": "存在冲突"}
                label = f"{checks[0]['label'] if checks else item['check_id']}：应为{names[item['status']]}"
                actual = (
                    "；".join(
                        f"{check.get('task_title', '')} {names[check['status']]}".strip() for check in checks
                    )
                    or "本次没有执行此检查"
                )
            elif kind == "citation":
                found = next(
                    (c for c in result["citations"] if c["document_id"] == item["document_id"]), None
                )
                passed = bool(found)
                label = ("应引用：" if guide else "应检索到：") + (
                    found["title"] if found else item["document_id"]
                )
                actual = (
                    ("回答已引用该资料" if found else "回答未引用该资料")
                    if guide
                    else ("检索已找到该资料" if found else "检索未找到该资料")
                )
                if not valid_run:
                    evaluations.append(
                        {
                            "id": item["id"],
                            "label": label,
                            "status": "manual",
                            "actual": "未评估：试运行未完成或资料已变化，请重新运行",
                        }
                    )
                    continue
            else:
                actual = "请根据运行结果人工判断"
            evaluations.append(
                {
                    "id": item["id"],
                    "label": label,
                    "status": "manual" if kind == "manual" else ("passed" if passed else "failed"),
                    "actual": actual,
                }
            )
        return evaluations

    def expire_runs(self, conn, owner):
        conn.execute(
            """UPDATE debug_runs SET status='cancelled',
            payload=json_set(payload,'$.status','cancelled','$.error',json(?))
            WHERE owner_id=? AND status='running' AND expires_at<=?""",
            (
                dump({"code": "debug_interrupted", "message": "上次运行已中断；已完成步骤可查看或继续运行"}),
                owner,
                now(),
            ),
        )

    def trim_runs(self, conn, owner, keep_id):
        rows = conn.execute(
            "SELECT id,length(CAST(COALESCE(payload,'') AS BLOB))+length(CAST(checkpoints AS BLOB))+length(CAST(signatures AS BLOB)) size FROM debug_runs WHERE owner_id=? AND status!='running' ORDER BY created_at DESC,id DESC",
            (owner,),
        ).fetchall()
        total = 0
        for index, row in enumerate(rows):
            total += row["size"]
            if row["id"] != keep_id and (index >= MAX_RUNS or total > MAX_OWNER_BYTES):
                conn.execute("DELETE FROM debug_runs WHERE owner_id=? AND id=?", (owner, row["id"]))

    def debug(self, owner, payload, *, emit=None, check_cancelled=None):
        from .service import fail

        raw_request = payload.model_dump()
        request_hash = digest(raw_request)
        client_id = payload.client_id or uid()
        with self.db.transaction() as conn:
            self.expire_runs(conn, owner)
            existing = conn.execute(
                "SELECT * FROM debug_runs WHERE owner_id=? AND client_id=?", (owner, client_id)
            ).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    fail(409, "idempotency_conflict", "此次运行标识已用于其他输入，请重新发起试运行")
                if existing["status"] != "running":
                    return json.loads(existing["payload"])
                if existing["expires_at"] > now():
                    fail(409, "debug_in_progress", "相同试运行正在执行，请稍后查看记录")
                fail(409, "debug_interrupted", "上次运行已中断，请使用新的运行标识重试")
            template, stage, tasks = self.inputs(payload)
            agent = stage.get("analysis") or template["agent"]
            step_ids = [step["id"] for step in agent["steps"]]
            for selected in (payload.start_step_id, payload.stop_after_step_id):
                if selected and selected not in step_ids:
                    fail(422, "invalid_step", "调试步骤不存在，请重新选择")
            start_index = step_ids.index(payload.start_step_id) if payload.start_step_id else 0
            if payload.stop_after_step_id and step_ids.index(payload.stop_after_step_id) < start_index:
                fail(422, "invalid_step_range", "结束步骤不能位于起始步骤之前")
            document_ids, versions, scope_hash = self.document_scope(conn, owner, payload.document_ids)
            request = raw_request | {"stage_id": stage["id"], "document_ids": document_ids}
            if payload.sample_id:
                sample = self.sample(owner, template["id"], payload.sample_id, conn, private=True)
                saved = json.loads(sample["payload"])
                keys = ("stage_id", "input", "task_values", "document_ids", "expectations")
                if (
                    sample["revision"] != payload.sample_revision
                    or sample["scope_hash"] != scope_hash
                    or any(request[key] != saved[key] for key in keys)
                ):
                    fail(
                        409, "stale_sample", "样例输入或资料版本已有变化，请重新载入，或另存当前样例后再运行"
                    )
            signatures = self.signatures(template, stage, tasks, request, scope_hash)
            resume, checkpoints = None, {}
            if payload.source_run_id:
                source = self.run(owner, payload.source_run_id, conn, private=True)
                if source["status"] == "running":
                    fail(409, "debug_in_progress", "上游试运行尚未结束，请先停止或等待完成")
                if source["template_id"] != template["id"] or source["stage_id"] != stage["id"]:
                    fail(409, "stale_checkpoint", "历史记录属于其他流程或业务节点，请从头运行")
                if start_index:
                    previous_id = step_ids[start_index - 1]
                    old_signatures, old_checkpoints = (
                        json.loads(source["signatures"]),
                        json.loads(source["checkpoints"]),
                    )
                    if previous_id not in old_checkpoints or signatures[previous_id] != old_signatures.get(
                        previous_id
                    ):
                        fail(409, "stale_checkpoint", "上游配置、输入或资料已变化，请先重新运行上游步骤")
                    prior = json.loads(source["payload"])
                    prefix = prior["trace"][:start_index]
                    if [step["id"] for step in prefix] != step_ids[:start_index] or any(
                        step["status"] not in ("completed", "cached", "skipped") for step in prefix
                    ):
                        fail(409, "stale_checkpoint", "历史记录缺少完整的上游结果，请先运行到起始步骤")
                    resume = {"trace": prefix, "state": old_checkpoints[previous_id]}
                    checkpoints = {
                        id: old_checkpoints[id] for id in step_ids[:start_index] if id in old_checkpoints
                    }
            if conn.execute(
                "SELECT 1 FROM debug_runs WHERE owner_id=? AND status='running' AND expires_at>? LIMIT 1",
                (owner, now()),
            ).fetchone():
                fail(409, "debug_in_progress", "当前账号已有试运行，请停止或等待结束后再试")
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM debug_runs WHERE status='running' AND expires_at>?", (now(),)
                ).fetchone()[0]
                >= 8
            ):
                fail(429, "debug_busy", "试运行服务繁忙，请稍后重试")
            run_id, created_at = uid(), now()
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
            result = {
                "output": "本次尚未生成建议。",
                "trace": [],
                "checks": [],
                "citations": [],
                "stage_id": stage["id"],
                "assistant_mode": self.service.capabilities["assistant_mode"],
                "retrieval_mode": "lexical",
            }
            if resume:
                result.update(copy.deepcopy(resume["state"]["result"]))
                if resume["state"]["guidance_citations"] is not None:
                    result["citations"] = copy.deepcopy(resume["state"]["guidance_citations"])
                result["trace"] = [reuse_trace(step) for step in resume["trace"]]
            metadata = {
                "id": run_id,
                "created_at": created_at,
                "request": request,
                "sample_id": payload.sample_id,
                "document_versions": versions,
                "source_run_id": payload.source_run_id,
            }
            initial = result | metadata | {"status": "cancelled", "evaluations": []}
            conn.execute(
                "INSERT INTO debug_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    owner,
                    template["id"],
                    stage["id"],
                    payload.sample_id,
                    client_id,
                    request_hash,
                    created_at,
                    expires_at,
                    "running",
                    dump(initial),
                    dump(checkpoints),
                    dump(signatures),
                ),
            )
            self.trim_runs(conn, owner, run_id)

        status, error = "completed", None

        def checkpoint(step_id, progress, state):
            nonlocal result, checkpoints
            candidate = checkpoints | ({step_id: state} if state is not None else {})
            # Persist finished steps before emitting completion, so stop retains them.
            public_progress = copy.deepcopy(progress)
            current_state = state or next(reversed(candidate.values()), None)
            if current_state and current_state["guidance_citations"] is not None:
                public_progress["citations"] = copy.deepcopy(current_state["guidance_citations"])
            snapshot = public_progress | metadata | {"status": "cancelled", "evaluations": []}
            encoded, saved_checkpoints = dump(snapshot), dump(candidate)
            # Leave room for bounded expectation labels and the final error envelope.
            if len(encoded.encode()) + len(saved_checkpoints.encode()) > MAX_RUN_BYTES - 1024 * 1024:
                fail(422, "debug_result_too_large", "此次运行记录超过 16 MB，请缩小样例或分析步骤后再试")
            with self.db.transaction() as conn:
                changed = conn.execute(
                    "UPDATE debug_runs SET payload=?,checkpoints=? WHERE id=? AND owner_id=? AND status='running' AND expires_at>?",
                    (encoded, saved_checkpoints, run_id, owner, now()),
                ).rowcount
                if not changed:
                    raise StreamCancelled()
            result = public_progress
            checkpoints = candidate

        def forward(event, data):
            if emit and event != "start":
                emit(event, data)

        cancelled = False
        try:
            if emit:
                emit("run", {"id": run_id})
            result = self.service.run_workflow(
                owner,
                template,
                stage,
                payload.input,
                tasks,
                context={"test_input": payload.input},
                stop_after_step_id=payload.stop_after_step_id,
                document_ids=document_ids,
                capture_data=True,
                resume=resume,
                on_checkpoint=checkpoint,
                emit=forward if emit else None,
                check_cancelled=check_cancelled,
            )
            if any(step["status"] == "failed" for step in result["trace"]):
                status, error = (
                    "failed",
                    {"code": "step_failed", "message": "有步骤执行失败，请查看该步骤的运行结果"},
                )
            elif (
                payload.stop_after_step_id and step_ids.index(payload.stop_after_step_id) < len(step_ids) - 1
            ):
                status = "paused"
            with closing(self.db.connect()) as conn:
                _, current_versions, current_scope = self.document_scope(conn, owner, document_ids)
                if current_scope != scope_hash or current_versions != versions:
                    status, error = (
                        "failed",
                        {
                            "code": "debug_context_changed",
                            "message": "运行期间资料已有变化，请基于当前版本重新测试",
                        },
                    )
                    checkpoints.clear()
        except StreamCancelled:
            cancelled, status = True, "cancelled"
        except Exception as failure:
            from .service import AppError

            status = "failed"
            error = (
                {"code": failure.code, "message": failure.message}
                if isinstance(failure, AppError)
                else {"code": "debug_failed", "message": "试运行未能完成，请稍后重试"}
            )
        result |= metadata | {"status": status}
        result["evaluations"] = self.evaluations(request["expectations"], result)
        if error:
            result["error"] = error
        with self.db.transaction() as conn:
            conn.execute(
                'UPDATE debug_runs SET status=?,payload=?,checkpoints=? WHERE owner_id=? AND id=? AND status="running"',
                (status, dump(result), dump(checkpoints), owner, run_id),
            )
            self.trim_runs(conn, owner, run_id)
        if cancelled:
            raise StreamCancelled()
        return result
