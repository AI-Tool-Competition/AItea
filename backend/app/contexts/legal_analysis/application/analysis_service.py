"""Versioned legal workspaces. Model suggestions never advance business tasks."""

import copy
import hashlib
import json
import logging
import os
import re
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

from app.contexts.legal_analysis.domain.analysis_models import MAX_CASE_FACTS
from app.shared.domain.countries import resolve_country
from app.shared.domain.values import dump, now, uid

logger = logging.getLogger(__name__)


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def fail(status, code, message):
    from app.shared.domain.errors import fail as service_fail

    service_fail(status, code, message)


class LegalService:
    def __init__(self, service):
        self.service, self.db = service, service.db
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=9").fetchone():
                return
            statements = [
                """CREATE TABLE legal_facts(case_id TEXT NOT NULL REFERENCES cases(id),
                    key TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,
                    PRIMARY KEY(case_id,key))""",
                """CREATE TABLE legal_fact_versions(case_id TEXT NOT NULL REFERENCES cases(id),
                    key TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,
                    actor_id TEXT NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,
                    PRIMARY KEY(case_id,key,revision))""",
                """CREATE TABLE legal_analyses(id TEXT PRIMARY KEY,owner_id TEXT NOT NULL REFERENCES users(id),
                    case_id TEXT NOT NULL REFERENCES cases(id),stage_id TEXT NOT NULL,revision INTEGER NOT NULL,
                    status TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""",
                "CREATE INDEX legal_analyses_case ON legal_analyses(owner_id,case_id,stage_id,created_at DESC,id DESC)",
                """CREATE TABLE legal_analysis_versions(analysis_id TEXT NOT NULL REFERENCES legal_analyses(id),
                    revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,
                    PRIMARY KEY(analysis_id,revision))""",
                """CREATE TABLE legal_requests(owner_id TEXT NOT NULL REFERENCES users(id),
                    case_id TEXT NOT NULL REFERENCES cases(id),client_id TEXT NOT NULL,payload_hash TEXT NOT NULL,
                    analysis_id TEXT NOT NULL REFERENCES legal_analyses(id),state TEXT NOT NULL,
                    token TEXT NOT NULL,expires_at TEXT NOT NULL,response TEXT,
                    PRIMARY KEY(owner_id,case_id,client_id))""",
            ]
            for statement in statements:
                conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (9, now()))

    def policy(self):
        from app.contexts.legal_analysis.domain.analysis_models import (
            LEGAL_MODULES,
            PROGRAM_CHECKS,
            default_legal_policy,
        )

        policy = default_legal_policy()
        return {
            "policy": policy.model_dump(mode="json") if hasattr(policy, "model_dump") else policy,
            "program_checks": PROGRAM_CHECKS,
            "modules": LEGAL_MODULES,
        }

    def stage(self, case, stage_id):
        stage = next((item for item in case["stages"] if item["id"] == stage_id), None)
        if stage is None:
            fail(422, "invalid_stage", "无效的业务节点")
        return stage

    def facts(self, conn, case_id):
        return [
            self.public(json.loads(row["payload"]))
            for row in conn.execute(
                "SELECT payload FROM legal_facts WHERE case_id=? ORDER BY key LIMIT ?",
                (case_id, MAX_CASE_FACTS),
            )
        ]

    def write_fact(self, conn, owner, case_id, fact):
        conn.execute(
            "INSERT INTO legal_facts VALUES(?,?,?,?) ON CONFLICT(case_id,key) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
            (case_id, fact["key"], fact["revision"], dump(fact)),
        )
        conn.execute(
            "INSERT INTO legal_fact_versions VALUES(?,?,?,?,?,?)",
            (case_id, fact["key"], fact["revision"], dump(fact), owner, now()),
        )

    def facts_truncated(self, conn, case_id):
        return (
            conn.execute(
                "SELECT COUNT(*) FROM (SELECT key FROM facts WHERE case_id=? UNION SELECT key FROM legal_facts WHERE case_id=?)",
                (case_id, case_id),
            ).fetchone()[0]
            > MAX_CASE_FACTS
        )

    def valid_evidence(self, conn, owner, case_id, ids):
        from app.contexts.documents.domain.metadata import DocumentMetadata, eligible

        if not ids:
            return set()
        rows = conn.execute(
            """SELECT d.id,d.source_url,m.payload,
            EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=d.id) classified,
            EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=d.id AND c.level='primary') primary_source
            FROM documents d LEFT JOIN document_metadata m ON m.document_id=d.id
            WHERE d.owner_id=? AND (d.case_id IS NULL OR d.case_id=?) AND d.role='primary' AND d.status='ready'
            AND NOT EXISTS(SELECT 1 FROM documents newer WHERE newer.owner_id=d.owner_id AND newer.family_id=d.family_id AND newer.version>d.version)
            AND d.id IN ("""
            + ",".join("?" for _ in ids)
            + ")",
            [owner, case_id, *ids],
        )
        valid = set()
        for row in rows:
            metadata = (
                json.loads(row["payload"])
                if row["payload"]
                else DocumentMetadata(kind="law", review_status="draft").model_dump(mode="json")
                if row["source_url"]
                else DocumentMetadata().model_dump(mode="json")
            )
            if (
                metadata["kind"] != "demo"
                and eligible(metadata, datetime.now(timezone.utc).date())
                and (not row["classified"] or row["primary_source"])
            ):
                valid.add(row["id"])
        # Shared legal sources use the full country/review/quality/version gate.
        # The pre-existing private evidence behavior above is unchanged.
        if case_id:
            from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID

            case = self.service.own_case(conn, owner, case_id)
            template = json.loads(case["template"])
            countries = set(self.allowed_countries(template))
            for stage in template.get("stages", []):
                countries.update(self.allowed_countries(template, stage))
            for country in sorted(countries):
                sql, parameters = self.service.archive.eligibility_sql(
                    conn,
                    owner,
                    case_id,
                    None,
                    datetime.now(timezone.utc).date(),
                    False,
                    country,
                    ids,
                )
                valid.update(
                    row["id"] for row in conn.execute(sql, parameters) if row["owner_id"] == SYSTEM_OWNER_ID
                )
        return valid

    def sync_facts(self, conn, owner, case_id):
        stored = {
            row["key"]: json.loads(row["payload"])
            for row in conn.execute(
                "SELECT key,payload FROM legal_facts WHERE case_id=? ORDER BY key LIMIT ?",
                (case_id, MAX_CASE_FACTS),
            )
        }
        for row in conn.execute("SELECT * FROM facts WHERE case_id=? ORDER BY key", (case_id,)):
            fingerprint = digest(dict(row))
            fact = stored.get(row["key"])
            if fact and fact.get("_base_hash") == fingerprint:
                continue
            original = json.loads(row["value"])
            if fact is None:
                if len(stored) >= MAX_CASE_FACTS:
                    continue
                fact = {
                    "key": row["key"],
                    "label": row["label"],
                    "value": original,
                    "source": row["source"],
                    "confirmed": bool(row["confirmed"]),
                    "extraction_confidence": "unknown",
                    "evidence_status": "unverified",
                    "evidence_refs": [],
                    "conflicts": [],
                    "resolved_conflicts": [],
                    "revision": 0,
                    "review_note": "",
                }
            elif not fact.get("_manual") and fact["source"] == row["source"]:
                fact.update(
                    value=original,
                    confirmed=bool(row["confirmed"]),
                    source=row["source"],
                    evidence_status="unverified",
                )
            elif original != fact["value"]:
                conflict = {"value": original, "source": row["source"], "evidence_refs": []}
                if conflict not in fact["conflicts"]:
                    fact["conflicts"] = [*fact["conflicts"], conflict][-30:]
                fact.update(confirmed=False, evidence_status="conflict")
            fact.update(revision=fact["revision"] + 1, _base_hash=fingerprint)
            self.write_fact(conn, owner, case_id, fact)
            stored[fact["key"]] = fact
        verified = [fact for fact in stored.values() if fact["evidence_status"] == "verified"]
        valid = self.valid_evidence(
            conn, owner, case_id, list({ref for fact in verified for ref in fact["evidence_refs"]})
        )
        for fact in verified:
            if not fact["evidence_refs"] or not set(fact["evidence_refs"]) <= valid:
                fact.update(evidence_status="unverified", revision=fact["revision"] + 1)
                self.write_fact(conn, owner, case_id, fact)

    def allowed_countries(self, template, stage=None):
        agent = (stage or {}).get("analysis") or template["agent"]
        countries = [resolve_country(template.get(key, "")) for key in ("origin", "destination")]
        countries.extend((agent.get("legal") or {}).get("jurisdictions", []))
        return list(dict.fromkeys(country for country in countries if country))

    def validate_documents(self, conn, owner, case_id, document_ids, countries):
        from app.shared.domain.countries import document_country

        for document_id in document_ids or []:
            doc = self.service.read_document(conn, owner, document_id)
            if doc["case_id"] not in (None, case_id) or (case_id is None and doc["case_id"] is not None):
                fail(404, "not_found", "未找到可用的检索资料")
            country = document_country(conn, document_id)
            if country and country not in countries:
                fail(422, "country_mismatch", "所选资料超出了本次分析允许的法域")
            if doc["role"] != "primary":
                fail(422, "invalid_document", "关联附件仅保存原件，不能作为正文检索来源")

    def search(
        self,
        owner,
        query,
        case_id,
        template,
        jurisdictions,
        limit,
        document_ids=None,
        as_of=None,
        *,
        check_cancelled=None,
    ):
        from app.shared.domain.countries import document_country

        allowed = set(self.allowed_countries(template))
        for stage in template.get("stages", []):
            allowed.update(self.allowed_countries(template, stage))
        requested = list(dict.fromkeys(jurisdictions or sorted(allowed)))
        if len(requested) > 22 or any(country not in allowed for country in requested):
            fail(422, "country_mismatch", "检索法域超出已配置的业务范围，请先核对规则配置")
        if not 1 <= limit <= 20:
            fail(422, "invalid_limit", "检索数量须在 1 至 20 之间")
        with closing(self.db.connect()) as conn:
            if case_id:
                self.service.own_case(conn, owner, case_id)
            self.validate_documents(conn, owner, case_id, document_ids, allowed)
        historical = as_of is not None
        if isinstance(as_of, str):
            as_of = date.fromisoformat(as_of)
        as_of = as_of or datetime.now(timezone.utc).date()
        found, warnings, modes = {}, [], set()
        for country in requested or [None]:
            if check_cancelled:
                check_cancelled()
            result = self.service.search_eligible(
                owner,
                query,
                case_id,
                limit,
                as_of,
                country=country,
                document_ids=document_ids,
                historical=historical,
            )
            if check_cancelled:
                check_cancelled()
            modes.add(result["mode"])
            if result.get("warning"):
                warnings.append(result["warning"])
            for citation in result["items"]:
                chunk = citation.get("chunk_id") or citation["locator"]
                citation_id = f"{citation['document_id']}:{citation['document_version']}:{digest(chunk)[:16]}"
                found[citation_id] = {**citation, "id": citation_id}
        with closing(self.db.connect()) as conn:
            for citation in found.values():
                citation["country"] = document_country(conn, citation["document_id"])
        if len(found) > 80:
            warnings.append("本轮候选资料超过返回预算，仍需分项补检；不能据此认定依据齐全。")
        return {
            "items": list(found.values())[:80],
            "mode": "ragflow" if "ragflow" in modes else "unavailable",
            "warning": "；".join(dict.fromkeys(warnings)) or None,
        }

    def preview(self, owner, payload, **options):
        from app.contexts.legal_analysis.application.analysis_engine import run_legal_analysis
        from app.contexts.legal_analysis.domain.analysis_models import LegalFact
        from app.contexts.workflows.domain.models import Template

        template = Template.model_validate(payload["template"]).model_dump(mode="json")
        stage = self.stage({"stages": template["stages"]}, payload["stage_id"])
        facts = [LegalFact.model_validate(fact).model_dump(mode="json") for fact in payload.get("facts", [])]
        # Preview input can explore hypothetical facts but cannot grant evidence verification.
        for fact in facts:
            fact["evidence_status"] = "conflict" if fact.get("conflicts") else "unverified"
        countries = self.allowed_countries(template, stage)
        with closing(self.db.connect()) as conn:
            self.validate_documents(conn, owner, None, payload.get("document_ids", []), countries)
        return run_legal_analysis(
            self.service,
            owner,
            template,
            stage,
            payload["input"],
            facts,
            document_ids=payload.get("document_ids", []),
            **options,
        )

    def snapshot(self, conn, owner, case):
        self.sync_facts(conn, owner, case["id"])
        facts = self.facts(conn, case["id"])
        value = {
            "template": case["template"],
            "company": case["company"],
            "product": case["product"],
            "mode_id": case["mode_id"],
            "date": datetime.now(timezone.utc).date().isoformat(),
            "provider": digest(
                [
                    os.getenv("AGENT_MODEL") or os.getenv("LLM_MODEL"),
                    os.getenv("AGENT_BASE_URL") or os.getenv("LLM_BASE_URL"),
                    os.getenv("AGENT_API_KEY") or os.getenv("LLM_API_KEY"),
                ]
            ),
            "embedding": self.service.embedding_fingerprint(),
            "rerank": self.service.rerank_fingerprint(),
            "ragflow_index": self.service.ragflow.cache_signature(conn, owner, case),
            "runner": 1,
            "task_evidence": [
                (task["id"], task["evidence_ids"]) for stage in case["stages"] for task in stage["tasks"]
            ],
        }
        sources = hashlib.sha256(dump(value).encode())
        loaded_keys = {fact["key"] for fact in facts}
        for row in conn.execute("SELECT * FROM facts WHERE case_id=? ORDER BY key", (case["id"],)):
            if row["key"] not in loaded_keys:
                sources.update(dump(dict(row)).encode())
        for row in conn.execute(
            """SELECT d.id,d.version,d.content_hash,d.status,d.source_url,dc.country,
                COALESCE(m.revision,0) metadata_revision,
                COALESCE(r.revision,0) index_revision
                FROM documents d LEFT JOIN document_metadata m ON m.document_id=d.id
                LEFT JOIN document_countries dc ON dc.document_id=d.id
                LEFT JOIN retrieval_documents r ON r.document_id=d.id
                WHERE d.owner_id=? AND (d.case_id IS NULL OR d.case_id=?) AND d.role='primary' ORDER BY d.id""",
            (owner, case["id"]),
        ):
            sources.update(dump(dict(row)).encode())
        for row in conn.execute(
            """SELECT c.document_id,c.collection,c.level,c.revision FROM legal_classifications c
                JOIN documents d ON d.id=c.document_id WHERE d.owner_id=?
                AND (d.case_id IS NULL OR d.case_id=?) ORDER BY c.document_id,c.collection""",
            (owner, case["id"]),
        ):
            sources.update(dump(dict(row)).encode())
        from app.contexts.documents.application.system_library import (
            public_document_sql,
            register_source_guard,
        )

        register_source_guard(conn)
        countries = set(self.allowed_countries(case["template"]))
        for stage in case["template"].get("stages", []):
            countries.update(self.allowed_countries(case["template"], stage))
        if countries:
            placeholders = ",".join("?" for _ in countries)
            for row in conn.execute(
                f"""SELECT d.id,d.version,d.content_hash,d.status,dc.country,
                    COALESCE(m.revision,0) metadata_revision,COALESCE(r.revision,0) index_revision,
                    c.collection,c.level,c.revision classification_revision
                    FROM documents d JOIN document_countries dc ON dc.document_id=d.id
                    LEFT JOIN document_metadata m ON m.document_id=d.id
                    LEFT JOIN retrieval_documents r ON r.document_id=d.id
                    JOIN legal_classifications c ON c.document_id=d.id
                    WHERE {public_document_sql("d")} AND d.role='primary' AND dc.country IN ({placeholders})
                    ORDER BY d.id,c.collection""",
                sorted(countries),
            ):
                sources.update(dump(dict(row)).encode())
        return {"environment": sources.hexdigest(), "facts": {fact["key"]: digest(fact) for fact in facts}}

    def affected(self, result, old, current):
        issues = result.get("issues", [])
        all_ids = [issue["id"] for issue in issues]
        if not old or old.get("environment") != current["environment"]:
            return all_ids
        old_facts = old.get("facts", {})
        changed = {
            key
            for key in old_facts.keys() | current["facts"].keys()
            if old_facts.get(key) != current["facts"].get(key)
        }
        if not changed:
            return []
        if any(key not in old_facts for key in changed):
            return all_ids
        affected = set()
        for issue in issues:
            keys = set(issue.get("fact_keys", [])) | {item["key"] for item in issue.get("missing_facts", [])}
            if not keys or keys & changed:
                affected.add(issue["id"])
        while True:
            expanded = affected | {
                issue["id"] for issue in issues if set(issue.get("depends_on", [])) & affected
            }
            if expanded == affected:
                return [key for key in all_ids if key in affected]
            affected = expanded

    def public(self, analysis):
        return {key: value for key, value in analysis.items() if not key.startswith("_")}

    def own(self, conn, owner, case_id, analysis_id):
        self.service.own_case(conn, owner, case_id)
        row = conn.execute(
            "SELECT payload FROM legal_analyses WHERE id=? AND owner_id=? AND case_id=?",
            (analysis_id, owner, case_id),
        ).fetchone()
        if row is None:
            fail(404, "not_found", "未找到法律分析")
        return json.loads(row["payload"])

    def save(self, conn, analysis, *, initial=False, history=True):
        if len(dump(analysis).encode()) > 2 * 1024 * 1024:
            fail(422, "analysis_too_large", "分析记录过大，请缩小问题或资料范围")
        analysis["updated_at"] = now()
        if initial:
            conn.execute(
                "INSERT INTO legal_analyses VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    analysis["id"],
                    analysis["_owner"],
                    analysis["case_id"],
                    analysis["stage_id"],
                    analysis["revision"],
                    analysis["status"],
                    dump(analysis),
                    analysis["created_at"],
                    analysis["updated_at"],
                ),
            )
        else:
            conn.execute(
                "UPDATE legal_analyses SET revision=?,status=?,payload=?,updated_at=? WHERE id=?",
                (
                    analysis["revision"],
                    analysis["status"],
                    dump(analysis),
                    analysis["updated_at"],
                    analysis["id"],
                ),
            )
        if history:
            conn.execute(
                "INSERT INTO legal_analysis_versions VALUES(?,?,?,?)",
                (
                    analysis["id"],
                    analysis["revision"],
                    dump(self.public(analysis)),
                    analysis["updated_at"],
                ),
            )

    def mark_stale(self, conn, analysis, snapshot):
        if analysis["status"] == "running":
            return analysis
        affected = sorted(
            set(analysis.get("affected_issue_ids", []))
            | set(self.affected(analysis["result"], analysis.get("_snapshot"), snapshot))
        )
        if not affected or (
            affected == analysis.get("affected_issue_ids", []) and analysis["status"] == "needs_review"
        ):
            return analysis
        analysis["affected_issue_ids"] = affected
        analysis["status"] = "needs_review"
        analysis["revision"] += 1
        for section in analysis["sections"]:
            if section["issue_id"] in affected:
                section["needs_review"] = True
        self.save(conn, analysis)
        return analysis

    def workspace(self, owner, case_id, stage_id=None, limit=20, offset=0):
        with self.db.transaction() as conn:
            case = self.service.case_detail(owner, case_id, conn)
            self.expire_requests(conn)
            if stage_id:
                self.stage(case, stage_id)
            snapshot = self.snapshot(conn, owner, case)
            where = "owner_id=? AND case_id=?" + (" AND stage_id=?" if stage_id else "")
            params = [owner, case_id] + ([stage_id] if stage_id else [])
            rows = conn.execute(
                f"SELECT payload FROM legal_analyses WHERE {where} ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
            analyses = []
            for row in rows:
                item = self.mark_stale(conn, json.loads(row["payload"]), snapshot)
                analyses.append(
                    {
                        key: item[key]
                        for key in (
                            "id",
                            "case_id",
                            "stage_id",
                            "question",
                            "revision",
                            "status",
                            "created_at",
                            "updated_at",
                            "affected_issue_ids",
                            "error",
                        )
                    }
                )
            total = conn.execute(f"SELECT COUNT(*) FROM legal_analyses WHERE {where}", params).fetchone()[0]
            return {
                "facts": self.facts(conn, case_id),
                "analyses": analyses,
                "total": total,
                "facts_truncated": self.facts_truncated(conn, case_id),
            }

    def get(self, owner, case_id, analysis_id):
        with self.db.transaction() as conn:
            analysis = self.own(conn, owner, case_id, analysis_id)
            case = self.service.case_detail(owner, case_id, conn)
            return self.public(self.mark_stale(conn, analysis, self.snapshot(conn, owner, case)))

    def patch_fact(self, owner, case_id, key, payload):
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,159}", key):
            fail(422, "invalid_fact_key", "事实编号无效")
        with self.db.transaction() as conn:
            self.service.own_case(conn, owner, case_id)
            self.sync_facts(conn, owner, case_id)
            current = next((fact for fact in self.facts(conn, case_id) if fact["key"] == key), None)
            revision = current["revision"] if current else 0
            if payload["expected_revision"] != revision:
                fail(409, "stale_fact", "事实已有新版本，你的输入已保留，请核对最新内容后再保存")
            count = conn.execute("SELECT COUNT(*) FROM legal_facts WHERE case_id=?", (case_id,)).fetchone()[0]
            if revision == 0 and count >= MAX_CASE_FACTS:
                fail(422, "fact_limit", "本业务已达到事实记录上限，请整理现有事实")
            evidence = list(dict.fromkeys(payload.get("evidence_refs", [])))
            for document_id in evidence:
                doc = self.service.read_document(conn, owner, document_id)
                if doc["case_id"] not in (None, case_id):
                    fail(404, "not_found", "未找到可用的证明材料")
                if doc["role"] != "primary" or doc["status"] != "ready":
                    fail(422, "invalid_evidence", "证明材料必须是已解析的主文件")
            note = payload.get("review_note", "")
            status = payload.get("evidence_status", "unverified")
            if status == "verified" and (not evidence or not note or not payload.get("confirmed")):
                fail(422, "evidence_review_required", "核验证据需要选择材料、填写核验说明并确认事实")
            if status == "verified" and not set(evidence) <= self.valid_evidence(
                conn, owner, case_id, evidence
            ):
                fail(422, "invalid_evidence", "证明材料未通过审核、已过适用期或已有新版本，请重新核验")
            conflicts = copy.deepcopy((current or {}).get("conflicts", []))
            if conflicts and status != "conflict" and not note:
                fail(422, "conflict_review_required", "请说明如何核对冲突，原始冲突及来源仍会保留")
            resolved = copy.deepcopy((current or {}).get("resolved_conflicts", []))
            if status != "conflict":
                resolved = [*resolved, *[item for item in conflicts if item not in resolved]][-30:]
            base = conn.execute("SELECT * FROM facts WHERE case_id=? AND key=?", (case_id, key)).fetchone()
            fact = {
                "key": key,
                "label": payload["label"],
                "value": payload["value"],
                "source": "用户修订",
                "confirmed": payload.get("confirmed", False),
                "extraction_confidence": "unknown",
                "evidence_status": status,
                "evidence_refs": evidence,
                "conflicts": conflicts if status == "conflict" else [],
                "revision": revision + 1,
                "resolved_conflicts": resolved,
                "review_note": note,
                "_manual": True,
                "_base_hash": digest(dict(base)) if base else None,
            }
            if status == "conflict":
                fact["confirmed"] = False
            conn.execute(
                "INSERT INTO legal_facts VALUES(?,?,?,?) ON CONFLICT(case_id,key) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                (case_id, key, fact["revision"], dump(fact)),
            )
            conn.execute(
                "INSERT INTO legal_fact_versions VALUES(?,?,?,?,?,?)",
                (case_id, key, fact["revision"], dump(fact), owner, now()),
            )
            self.service.touch(conn, case_id)
            self.service.event(
                conn,
                case_id,
                "legal_fact_updated",
                "更新分析事实",
                f"{fact['label']} · 第 {fact['revision']} 版；相关结论需要检查。",
            )
            case = self.service.case_detail(owner, case_id, conn)
            snapshot = self.snapshot(conn, owner, case)
            affected = []
            for row in conn.execute(
                "SELECT payload FROM legal_analyses WHERE owner_id=? AND case_id=? ORDER BY created_at DESC,id DESC LIMIT 20",
                (owner, case_id),
            ):
                analysis = json.loads(row["payload"])
                updated = self.mark_stale(conn, analysis, snapshot)
                if updated["affected_issue_ids"]:
                    affected.append(updated["id"])
            return {"fact": self.public(fact), "affected_analysis_ids": affected}

    def execute_engine(self, owner, template, stage, question, facts, **options):
        from app.contexts.legal_analysis.application.analysis_engine import run_legal_analysis

        return run_legal_analysis(self.service, owner, template, stage, question, facts, **options)

    def expire_requests(self, conn):
        for row in conn.execute(
            "SELECT * FROM legal_requests WHERE state='running' AND expires_at<=?", (now(),)
        ).fetchall():
            analysis = self.own(conn, row["owner_id"], row["case_id"], row["analysis_id"])
            if analysis["status"] == "running":
                analysis.update(
                    status="cancelled",
                    error="上次运行已中断，可重试继续。",
                    revision=analysis["revision"] + 1,
                )
                self.save(conn, analysis)
            conn.execute(
                "UPDATE legal_requests SET state='cancelled' WHERE owner_id=? AND case_id=? AND client_id=?",
                (row["owner_id"], row["case_id"], row["client_id"]),
            )

    def reserve(self, owner, case_id, payload, analysis_id):
        from app.contexts.legal_analysis.domain.analysis_models import LegalResult

        request_hash = digest({"analysis_id": analysis_id, "payload": payload})
        with closing(self.db.connect()) as conn:
            case = self.service.case_detail(owner, case_id, conn)
            existing = conn.execute(
                "SELECT * FROM legal_requests WHERE owner_id=? AND case_id=? AND client_id=?",
                (owner, case_id, payload["client_request_id"]),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != request_hash:
                    fail(409, "idempotency_conflict", "同一请求编号不能提交不同内容")
                if existing["state"] == "completed":
                    return {"response": json.loads(existing["response"])}
                if existing["state"] == "running" and existing["expires_at"] > now():
                    fail(409, "analysis_in_progress", "本次分析正在进行，请等待结果")
            current_id = existing["analysis_id"] if existing else analysis_id
            stage_id = (
                self.own(conn, owner, case_id, current_id)["stage_id"] if current_id else payload["stage_id"]
            )
            stage = self.stage(case, stage_id)
        progress = self.service.index.prepare_scopes(
            owner,
            self.allowed_countries(case["template"], stage),
            case_id=case_id,
            historical=True,
        )
        if progress["remaining"]:
            fail(409, "index_preparing", "本次范围仍有资料待索引，已完成一批准备；请重试后再分析。")
        with self.db.transaction() as conn:
            case = self.service.case_detail(owner, case_id, conn)
            self.expire_requests(conn)
            existing = conn.execute(
                "SELECT * FROM legal_requests WHERE owner_id=? AND case_id=? AND client_id=?",
                (owner, case_id, payload["client_request_id"]),
            ).fetchone()
            if existing:
                if existing["payload_hash"] != request_hash:
                    fail(409, "idempotency_conflict", "同一请求编号不能提交不同内容")
                if existing["state"] == "completed":
                    return {"response": json.loads(existing["response"])}
                if existing["state"] == "running":
                    fail(409, "analysis_in_progress", "本次分析正在进行，请等待结果")
                analysis_id = existing["analysis_id"]
            if conn.execute(
                "SELECT 1 FROM legal_requests WHERE case_id=? AND state='running'", (case_id,)
            ).fetchone():
                fail(409, "analysis_in_progress", "本业务正在分析，请完成或取消后再重试")
            if conn.execute("SELECT COUNT(*) FROM legal_requests WHERE state='running'").fetchone()[0] >= 4:
                fail(503, "analysis_busy", "当前分析较多，请稍后重试")
            snapshot = self.snapshot(conn, owner, case)
            if self.facts_truncated(conn, case_id):
                fail(
                    422,
                    "fact_limit",
                    f"业务事实超过本版 {MAX_CASE_FACTS} 条分析上限，请先整理事实，避免遗漏重要前提",
                )
            if analysis_id:
                analysis = self.mark_stale(conn, self.own(conn, owner, case_id, analysis_id), snapshot)
                if not existing and payload["expected_revision"] != analysis["revision"]:
                    fail(409, "stale_analysis", "分析已有新版本，请核对后再重新分析")
                stage = self.stage(case, analysis["stage_id"])
                previous = copy.deepcopy(analysis["result"])
                affected = analysis["affected_issue_ids"] or [issue["id"] for issue in previous["issues"]]
                resume = analysis.get("_checkpoint") if analysis.get("_run_snapshot") == snapshot else None
                analysis.update(revision=analysis["revision"] + 1, status="running", error=None)
                self.save(conn, analysis)
            else:
                stage = self.stage(case, payload["stage_id"])
                previous, affected, resume = None, None, None
                analysis = {
                    "id": uid(),
                    "case_id": case_id,
                    "stage_id": stage["id"],
                    "question": payload["question"],
                    "revision": 1,
                    "status": "running",
                    "created_at": now(),
                    "updated_at": now(),
                    "result": LegalResult(
                        question=payload["question"],
                        assistant_mode=self.service.capabilities["assistant_mode"],
                    ).model_dump(mode="json"),
                    "sections": [],
                    "affected_issue_ids": [],
                    "error": None,
                    "_owner": owner,
                    "_snapshot": snapshot,
                    **(
                        {"_chat_context": payload["_chat_context"]}
                        if payload.get("_chat_context") is not None
                        else {}
                    ),
                }
                self.save(conn, analysis, initial=True)
            analysis["_run_snapshot"] = snapshot
            if resume is None:
                analysis.pop("_checkpoint", None)
                analysis.pop("_progress", None)
            self.save(conn, analysis, history=False)
            token = uid()
            conn.execute(
                """INSERT INTO legal_requests VALUES(?,?,?,?,?,'running',?,?,NULL)
                ON CONFLICT(owner_id,case_id,client_id) DO UPDATE SET state='running',token=excluded.token,
                expires_at=excluded.expires_at,response=NULL""",
                (
                    owner,
                    case_id,
                    payload["client_request_id"],
                    request_hash,
                    analysis["id"],
                    token,
                    (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
                ),
            )
            return {
                "analysis": analysis,
                "case": case,
                "stage": stage,
                "facts": self.facts(conn, case_id),
                "snapshot": snapshot,
                "previous": previous,
                "affected": affected,
                "resume": resume,
                "token": token,
                "client_id": payload["client_request_id"],
            }

    def claim(self, conn, owner, case_id, prepared):
        row = conn.execute(
            "SELECT * FROM legal_requests WHERE owner_id=? AND case_id=? AND client_id=?",
            (owner, case_id, prepared["client_id"]),
        ).fetchone()
        if not row or row["state"] != "running" or row["token"] != prepared["token"]:
            fail(409, "analysis_replaced", "运行已失效，请读取当前分析后重试")
        return row

    def merge_sections(self, old, result):
        previous = {section["id"]: section for section in old}
        sections = []
        issues = {issue["id"]: issue for issue in result["issues"]}
        for section in result["sections"]:
            prior = previous.pop(section["id"], None)
            if (
                prior
                and prior["edited"]
                and (prior["title"] != section["title"] or prior["issue_id"] != section["issue_id"])
            ):
                # Model-generated IDs can be reused after classification changes.
                # Retain the human paragraph under its original title, separately.
                preserved = {
                    **prior,
                    "id": "preserved-" + digest([prior["id"], prior["title"], prior["content"]])[:24],
                    "needs_review": True,
                    "suggested_content": None,
                }
                previous[preserved["id"]] = preserved
                prior = None
            needs_review = issues.get(section["issue_id"], {}).get("needs_review", True)
            if prior and prior["edited"]:
                sections.append(
                    {
                        **prior,
                        "title": section["title"],
                        "generated_content": section["content"],
                        "suggested_content": section["content"]
                        if section["content"] != prior["content"]
                        else None,
                        "needs_review": True,
                    }
                )
            else:
                sections.append(
                    {
                        **section,
                        "generated_content": section["content"],
                        "suggested_content": None,
                        "edited": False,
                        "needs_review": needs_review,
                    }
                )
        # A changed issue split must never delete a human-authored paragraph.
        sections.extend(
            {**section, "needs_review": True, "suggested_content": None}
            for section in previous.values()
            if section["edited"]
        )
        return sections

    def save_proposals(self, conn, owner, case_id, result):
        current = {fact["key"]: fact for fact in self.facts(conn, case_id)}
        citations = {item.get("id"): item["document_id"] for item in result["citations"]}
        for proposed in result["facts"]:
            prior = current.get(proposed["key"])
            # Input facts and unchanged confirmations are server-owned.
            fills_missing = bool(
                prior and prior["value"] in (None, "") and proposed["value"] not in (None, "")
            )
            if prior and not fills_missing and prior.get("conflicts") == proposed.get("conflicts"):
                continue
            if not prior and len(current) >= MAX_CASE_FACTS:
                result["gaps"].append("事实已达记录上限；部分新提取事实未保存，请先整理事实。")
                break
            fact = copy.deepcopy(prior or proposed)
            if fills_missing:
                fact.update(
                    {
                        key: proposed[key]
                        for key in ("value", "source", "extraction_confidence", "evidence_refs")
                    }
                )
                fact["_manual"] = True
            fact["evidence_refs"] = list(
                dict.fromkeys(citations.get(key, key) for key in fact["evidence_refs"])
            )
            fact["confirmed"] = False
            fact["evidence_status"] = "conflict" if proposed["conflicts"] else "unverified"
            fact["conflicts"] = copy.deepcopy(proposed["conflicts"])
            for conflict in fact["conflicts"]:
                conflict["evidence_refs"] = list(
                    dict.fromkeys(citations.get(key, key) for key in conflict["evidence_refs"])
                )
            fact["revision"] = (prior or {}).get("revision", 0) + 1
            if prior:
                stored = conn.execute(
                    "SELECT payload FROM legal_facts WHERE case_id=? AND key=?", (case_id, fact["key"])
                ).fetchone()
                if stored:
                    fact.update(
                        {
                            key: value
                            for key, value in json.loads(stored["payload"]).items()
                            if key.startswith("_")
                        }
                    )
            conn.execute(
                "INSERT INTO legal_facts VALUES(?,?,?,?) ON CONFLICT(case_id,key) DO UPDATE SET revision=excluded.revision,payload=excluded.payload",
                (case_id, fact["key"], fact["revision"], dump(fact)),
            )
            conn.execute(
                "INSERT INTO legal_fact_versions VALUES(?,?,?,?,?,?)",
                (case_id, fact["key"], fact["revision"], dump(fact), owner, now()),
            )
            current[fact["key"]] = self.public(fact)
        result["facts"] = list(current.values())

    def analyze(
        self,
        owner,
        case_id,
        payload,
        *,
        analysis_id=None,
        emit=None,
        check_cancelled=None,
        prompt_cache_key=None,
    ):
        from app.contexts.legal_analysis.domain.analysis_models import LegalResult
        from app.shared.application.streaming import StreamCancelled
        from app.shared.domain.errors import AppError

        prepared = self.reserve(owner, case_id, payload, analysis_id)
        if "response" in prepared:
            return prepared["response"]
        analysis = prepared["analysis"]

        def checkpoint(step_id, result, state):
            with self.db.transaction() as conn:
                self.claim(conn, owner, case_id, prepared)
                current = self.own(conn, owner, case_id, analysis["id"])
                current["_checkpoint"] = {"step_id": step_id, "state": state}
                current["_progress"] = result
                self.save(conn, current, history=False)
                conn.execute(
                    "UPDATE legal_requests SET expires_at=? WHERE owner_id=? AND case_id=? AND client_id=?",
                    (
                        (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat(),
                        owner,
                        case_id,
                        prepared["client_id"],
                    ),
                )

        try:
            if check_cancelled:
                check_cancelled()
            if emit:
                emit("start", {"analysis_id": analysis["id"], "revision": analysis["revision"]})
            result = self.execute_engine(
                owner,
                prepared["case"]["template"],
                prepared["stage"],
                analysis["question"],
                prepared["facts"],
                case_id=case_id,
                context={**prepared["case"], "conversation": analysis.get("_chat_context")},
                previous=prepared["previous"],
                affected_issue_ids=prepared["affected"],
                resume=prepared["resume"],
                emit=emit,
                check_cancelled=check_cancelled,
                on_checkpoint=checkpoint,
                prompt_cache_key=prompt_cache_key,
            )
            result = LegalResult.model_validate(result).model_dump(mode="json")
            stale = False
            with self.db.transaction() as conn:
                self.claim(conn, owner, case_id, prepared)
                if check_cancelled:
                    check_cancelled()
                current = self.own(conn, owner, case_id, analysis["id"])
                case = self.service.case_detail(owner, case_id, conn)
                snapshot = self.snapshot(conn, owner, case)
                if snapshot != prepared["snapshot"]:
                    current.update(
                        status="needs_review",
                        error="运行期间事实或资料已变化，请根据最新内容重新分析。",
                        affected_issue_ids=[issue["id"] for issue in current["result"]["issues"]],
                    )
                    for section in current["sections"]:
                        section["needs_review"] = True
                    stale = True
                else:
                    self.save_proposals(conn, owner, case_id, result)
                    current["result"] = result
                    current["sections"] = self.merge_sections(current["sections"], result)
                    current["affected_issue_ids"] = []
                    current["status"] = (
                        "needs_input"
                        if result["followups"]
                        else "needs_review"
                        if (
                            result["assistant_mode"] == "demo"
                            or result["gaps"]
                            or any(section["needs_review"] for section in current["sections"])
                        )
                        else "completed"
                    )
                    current["error"] = None
                    current["_snapshot"] = self.snapshot(conn, owner, case)
                    current.pop("_checkpoint", None)
                    current.pop("_progress", None)
                current["revision"] += 1
                self.save(conn, current)
                response = self.public(current)
                conn.execute(
                    "UPDATE legal_requests SET state=?,response=? WHERE owner_id=? AND case_id=? AND client_id=?",
                    (
                        "stale" if stale else "completed",
                        None if stale else dump(response),
                        owner,
                        case_id,
                        prepared["client_id"],
                    ),
                )
            if stale:
                fail(409, "analysis_context_changed", current["error"])
            return response
        except BaseException as error:
            with self.db.transaction() as conn:
                request = conn.execute(
                    "SELECT state,token FROM legal_requests WHERE owner_id=? AND case_id=? AND client_id=?",
                    (owner, case_id, prepared["client_id"]),
                ).fetchone()
                if request and request["state"] == "running" and request["token"] == prepared["token"]:
                    current = self.own(conn, owner, case_id, analysis["id"])
                    cancelled = isinstance(error, StreamCancelled)
                    current.update(
                        status="cancelled" if cancelled else "failed",
                        error="分析已取消，可重试继续。"
                        if cancelled
                        else "分析未完成，请检查失败步骤后重试。",
                        revision=current["revision"] + 1,
                    )
                    partial = current.get("_progress") or (current.get("_checkpoint") or {}).get(
                        "state", {}
                    ).get("result")
                    if partial:
                        current["result"] = partial
                    for section in current["sections"]:
                        section["needs_review"] = True
                    self.save(conn, current)
                    conn.execute(
                        "UPDATE legal_requests SET state=? WHERE owner_id=? AND case_id=? AND client_id=?",
                        (current["status"], owner, case_id, prepared["client_id"]),
                    )
            if isinstance(error, (AppError, StreamCancelled)) or not isinstance(error, Exception):
                raise
            logger.exception("legal_analysis_failed", extra={"analysis_id": analysis["id"]})
            fail(502, "legal_analysis_failed", "分析未完成；已保存的内容仍然保留，请稍后重试")

    def patch_section(self, owner, case_id, analysis_id, section_id, payload):
        with self.db.transaction() as conn:
            analysis = self.own(conn, owner, case_id, analysis_id)
            case = self.service.case_detail(owner, case_id, conn)
            analysis = self.mark_stale(conn, analysis, self.snapshot(conn, owner, case))
            if analysis["status"] == "running":
                fail(409, "analysis_in_progress", "请在本轮分析完成后保存段落，当前输入会保留")
            if analysis["revision"] != payload["expected_revision"]:
                fail(409, "stale_analysis", "报告已有新版本，你的输入已保留，请核对后再保存")
            section = next((item for item in analysis["sections"] if item["id"] == section_id), None)
            if section is None:
                fail(404, "not_found", "未找到报告段落")
            if payload.get("accept_suggestion"):
                if section["suggested_content"] is None or payload["content"] != section["suggested_content"]:
                    fail(422, "suggestion_mismatch", "请采用当前更新建议，或作为手工修订保存")
                section.update(content=section["suggested_content"], suggested_content=None, edited=False)
                issue = next(
                    (item for item in analysis["result"]["issues"] if item["id"] == section["issue_id"]), {}
                )
                section["needs_review"] = (
                    issue.get("needs_review", True) or section["issue_id"] in analysis["affected_issue_ids"]
                )
            else:
                section.update(content=payload["content"], edited=True, needs_review=True)
            analysis.update(revision=analysis["revision"] + 1, status="needs_review")
            self.save(conn, analysis)
            return self.public(analysis)

    def versions(self, owner, case_id, analysis_id, limit=20, offset=0):
        with closing(self.db.connect()) as conn:
            self.own(conn, owner, case_id, analysis_id)
            items = [
                dict(row)
                for row in conn.execute(
                    "SELECT revision,created_at FROM legal_analysis_versions WHERE analysis_id=? ORDER BY revision DESC LIMIT ? OFFSET ?",
                    (analysis_id, limit, offset),
                )
            ]
            total = conn.execute(
                "SELECT COUNT(*) FROM legal_analysis_versions WHERE analysis_id=?", (analysis_id,)
            ).fetchone()[0]
            return {"items": items, "total": total}

    def version(self, owner, case_id, analysis_id, revision):
        with closing(self.db.connect()) as conn:
            self.own(conn, owner, case_id, analysis_id)
            row = conn.execute(
                "SELECT payload FROM legal_analysis_versions WHERE analysis_id=? AND revision=?",
                (analysis_id, revision),
            ).fetchone()
            if row is None:
                fail(404, "not_found", "未找到分析版本")
            return json.loads(row["payload"])
