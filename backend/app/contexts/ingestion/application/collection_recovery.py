"""Durable, bounded recovery of proven transient failures; paid ambiguity is held.

Queue retries remain three-attempt bursts. This ledger retains every burst and
reschedules eligible exhausted work after a longer delay, without rewriting
originals, legal metadata, completed pages, or historical request receipts.
"""

import hashlib
import json
import math
import time
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass

from app.contexts.documents.application.system_library import BackgroundWorkerScope
from app.contexts.documents.infrastructure.recovery.ocr_error_evidence import request_outcome_unknown
from app.shared.domain.values import dump, now, uid

SCHEMA_VERSION = 19
BASE_RECOVERY_SECONDS = 15 * 60
MAX_RECOVERY_SECONDS = 24 * 60 * 60
MAX_BATCH = 32


def _encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _timestamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0


@dataclass(frozen=True)
class Decision:
    reason: str
    retryable: bool = False
    pages: tuple[int, ...] = ()
    not_before: float = 0


def _unknown(record):
    metadata = record.get("ocr", {})
    if request_outcome_unknown(metadata):
        return True
    status = metadata.get("http_status")
    if type(status) is int and (status == 408 or 500 <= status <= 599):
        return True
    # An intent without a final receipt may have survived a killed worker.
    return bool(
        metadata.get("attempts")
        and not metadata.get("finish_reason")
        and "text" not in metadata
        and "complete" not in metadata
        and not metadata.get("error_response")
        and not record.get("error")
    )


def _legacy_mineru_chart_refresh(manifest):
    """Recognize only pre-inventory MinerU manifests blocked solely on charts."""
    remote = manifest.get("remote_parse")
    if (
        manifest.get("parser") != "mineru"
        or manifest.get("mineru_chart_inventory_version")
        or manifest.get("mineru_chart_assets")
        or not isinstance(remote, dict)
        or remote.get("provider") != "mineru"
        or remote.get("state") != "done"
    ):
        return False
    try:
        from app.contexts.documents.infrastructure.providers.mineru import manifest_valid

        if not manifest_valid(manifest):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    pages = manifest.get("pages")
    if not isinstance(pages, list) or not pages:
        return False
    chart_pages = 0
    for page in pages:
        reasons = page.get("reasons")
        proof = page.get("ocr")
        if not isinstance(reasons, list) or not isinstance(proof, dict):
            return False
        if "chart_content_missing" in reasons:
            chart_pages += 1
            if (
                set(reasons) != {"chart_content_missing"}
                or page.get("status") != "ocr_uncertain"
                or proof.get("provider") != "mineru"
                or proof.get("complete") is not False
                or proof.get("uncertain_spans") != ["chart_content_missing"]
            ):
                return False
        elif reasons or page.get("status") != "ocr_complete" or proof.get("complete") is not True:
            return False
        text = page.get("text")
        if not isinstance(text, str) or proof.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest():
            return False
    return chart_pages > 0


def classify_processing(manifest, *, phase, error=None, retryable=False, interrupted=False):
    """Use explicit outcome evidence, never an exception's localized message."""
    from app.contexts.documents.infrastructure.providers.native_asset_ocr import assets

    if phase == "embed":
        # The generic multi-batch call has no complete per-input receipt ledger.
        # Its final 429 alone cannot prove earlier batches were not billed.
        return Decision("paid_result_unknown")
    if not isinstance(manifest, dict):
        return Decision("invalid_checkpoint")
    pages = manifest.get("pages", [])
    if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
        return Decision("invalid_checkpoint")
    try:
        images = assets(manifest)
        if any(_unknown(record) for record in [*pages, *images]):
            return Decision("paid_result_unknown")
    except (ValueError, TypeError, AttributeError):
        return Decision("invalid_checkpoint")
    if phase == "parse" and _legacy_mineru_chart_refresh(manifest):
        return Decision("mineru_chart_refresh", True)
    if any(
        page.get("status") == "ocr_uncertain"
        or page.get("ocr", {}).get("phase") == "recovery"
        or page.get("ocr", {}).get("uncertain_spans")
        for page in pages
    ) or any(image["asset_status"] in ("ocr_uncertain", "image_reference_pending") for image in images):
        return Decision("quality_review")
    failed = [page for page in pages if page.get("status") == "ocr_failed"]
    if any(image["asset_status"] == "ocr_failed" for image in images):
        # Legacy native-image failures lack a bound HTTP error CAS receipt.
        return Decision("image_failure_review")
    if failed:
        if len(failed) > 20:
            return Decision("ocr_retry_scope_review")
        if any(
            not isinstance(page.get("ocr_retry_cycles", []), list)
            or len(page.get("ocr_retry_cycles", [])) >= 3
            for page in failed
        ):
            return Decision("ocr_retry_budget_review")
        if all(
            type(page.get("page")) is int
            and page["page"] > 0
            and page.get("ocr", {}).get("http_status") == 429
            and isinstance(page.get("ocr", {}).get("error_response", {}).get("blob"), dict)
            for page in failed
        ):
            return Decision(
                "ocr_rate_limited",
                True,
                tuple(page["page"] for page in failed),
                max(_timestamp(page["ocr"].get("retry_not_before")) for page in failed),
            )
        return Decision("ocr_failure_review")
    if interrupted or retryable:
        return Decision("transient_processing", True)
    return Decision("processing_review")


class CollectionRecovery:
    def __init__(self, collection):
        self.collection, self.service, self.db = collection, collection.service, collection.db
        self._migrate()

    def _migrate(self):
        with self.db.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)
            ).fetchone():
                conn.execute("""CREATE TABLE IF NOT EXISTS collection_recovery_jobs(
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                    queue_kind TEXT NOT NULL CHECK(queue_kind IN ('fetch','processing')),
                    item_id TEXT NOT NULL,phase TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('waiting','blocked','queued','completed')),
                    reason TEXT NOT NULL,available_at REAL NOT NULL DEFAULT 0,
                    recovery_count INTEGER NOT NULL DEFAULT 0,failure_count INTEGER NOT NULL DEFAULT 0,
                    binding TEXT NOT NULL CHECK(json_valid(binding)),binding_sha256 TEXT NOT NULL,
                    retry_plan TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(retry_plan)),
                    revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    UNIQUE(owner_id,queue_kind,item_id,phase))""")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS recovery_due ON collection_recovery_jobs(status,available_at,id)"
                )
                conn.execute("""CREATE TABLE IF NOT EXISTS collection_recovery_events(
                    id TEXT PRIMARY KEY,recovery_id TEXT NOT NULL REFERENCES collection_recovery_jobs(id),
                    event TEXT NOT NULL,detail TEXT NOT NULL CHECK(json_valid(detail)),created_at TEXT NOT NULL)""")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS recovery_events ON collection_recovery_events(recovery_id,created_at,id)"
                )
                for operation in ("UPDATE", "DELETE"):
                    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS recovery_events_immutable_{operation.lower()}
                        BEFORE {operation} ON collection_recovery_events
                        BEGIN SELECT RAISE(ABORT,'recovery event is immutable'); END""")
                conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (SCHEMA_VERSION, now()))
            # Reject a marker-only or partially copied migration.
            conn.execute(
                "SELECT id,owner_id,queue_kind,item_id,phase,status,reason,available_at,recovery_count,failure_count,binding,binding_sha256,retry_plan,revision FROM collection_recovery_jobs LIMIT 0"
            )
            conn.execute(
                "SELECT id,recovery_id,event,detail,created_at FROM collection_recovery_events LIMIT 0"
            )

    @staticmethod
    def _event(conn, identifier, event, detail):
        conn.execute(
            "INSERT INTO collection_recovery_events VALUES(?,?,?,?,?)",
            (uid(), identifier, event, dump(detail), now()),
        )

    @staticmethod
    def _binding(conn, owner, kind, item_id, phase):
        if kind == "processing":
            row = conn.execute(
                """SELECT d.id,d.owner_id,d.case_id,d.parent_id,d.role,d.version,d.content_hash,d.size,d.source_url
                FROM documents d JOIN processing_jobs p ON p.document_id=d.id AND p.owner_id=d.owner_id
                WHERE d.id=? AND d.owner_id=?""",
                (item_id, owner),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT i.id,i.owner_id,i.run_id,i.url,i.identity_key,i.level,i.parent_document_id,
                i.purpose,i.candidate_id,r.source_id,r.plan,s.url AS source_url,s.plan AS source_plan,s.checklist
                FROM collection_items i JOIN collection_runs r ON r.id=i.run_id AND r.owner_id=i.owner_id
                JOIN sources s ON s.id=r.source_id AND s.owner_id=i.owner_id WHERE i.id=? AND i.owner_id=?""",
                (item_id, owner),
            ).fetchone()
        return {"queue_kind": kind, "phase": phase, **dict(row)} if row else None

    def record_failure(self, conn, claim, *, processing, decision, exhausted, ready_at=0, error_type=None):
        kind, phase = ("processing", claim["phase"]) if processing else ("fetch", "fetch")
        item_id = claim["document_id"] if processing else claim["id"]
        owner = claim["owner_id"]
        binding = self._binding(conn, owner, kind, item_id, phase)
        if binding is None:
            return
        fingerprint = _digest(binding)
        row = conn.execute(
            "SELECT * FROM collection_recovery_jobs WHERE owner_id=? AND queue_kind=? AND item_id=? AND phase=?",
            (owner, kind, item_id, phase),
        ).fetchone()
        timestamp = time.time()
        if row is None:
            identifier = uid()
            conn.execute(
                """INSERT INTO collection_recovery_jobs(id,owner_id,queue_kind,item_id,phase,status,reason,binding,binding_sha256,created_at,updated_at)
                VALUES(?,?,?,?,?,'queued',?,?,?,?,?)""",
                (
                    identifier,
                    owner,
                    kind,
                    item_id,
                    phase,
                    decision.reason,
                    dump(binding),
                    fingerprint,
                    now(),
                    now(),
                ),
            )
            row = conn.execute("SELECT * FROM collection_recovery_jobs WHERE id=?", (identifier,)).fetchone()
        if row["binding_sha256"] != fingerprint:
            decision = Decision("source_changed")
            exhausted = True
        delay = min(MAX_RECOVERY_SECONDS, BASE_RECOVERY_SECONDS * 2 ** min(row["recovery_count"], 7))
        available = (
            max(timestamp + delay, _timestamp(ready_at), decision.not_before)
            if exhausted and decision.retryable
            else _timestamp(ready_at)
        )
        status = ("waiting" if decision.retryable else "blocked") if exhausted else "queued"
        retry_plan = []
        if processing and decision.pages:
            manifest = json.loads(
                conn.execute(
                    "SELECT manifest FROM documents WHERE id=? AND owner_id=?", (item_id, owner)
                ).fetchone()[0]
            )
            retry_plan = [
                {"page": page["page"], "checkpoint_sha256": _digest(page)}
                for page in manifest.get("pages", [])
                if page.get("page") in decision.pages
            ]
        conn.execute(
            """UPDATE collection_recovery_jobs SET status=?,reason=?,available_at=?,failure_count=failure_count+1,
            retry_plan=?,revision=revision+1,updated_at=? WHERE id=?""",
            (status, decision.reason, available, dump(retry_plan), now(), row["id"]),
        )
        detail = {
            "queue_attempt": claim.get("attempts", 0),
            "reason": decision.reason,
            "retryable": decision.retryable,
            "exhausted": exhausted,
            "available_at": available,
            "binding_sha256": fingerprint,
            "error_type": error_type,
            "physical_pages": list(decision.pages),
            "retry_plan": retry_plan,
        }
        self._event(conn, row["id"], "attempt_failed", detail)

    def processing_decision(self, conn, claim, **kwargs):
        row = conn.execute(
            "SELECT manifest FROM documents WHERE id=? AND owner_id=?",
            (claim["document_id"], claim["owner_id"]),
        ).fetchone()
        try:
            manifest = json.loads(row["manifest"]) if row else None
        except (ValueError, TypeError):
            return Decision("invalid_checkpoint")
        return classify_processing(manifest, phase=claim["phase"], **kwargs)

    def completed(self, conn, claim, *, processing):
        kind, phase = ("processing", claim["phase"]) if processing else ("fetch", "fetch")
        item_id = claim["document_id"] if processing else claim["id"]
        row = conn.execute(
            "SELECT * FROM collection_recovery_jobs WHERE owner_id=? AND queue_kind=? AND item_id=? AND phase=?",
            (claim["owner_id"], kind, item_id, phase),
        ).fetchone()
        if row and row["status"] != "completed":
            conn.execute(
                "UPDATE collection_recovery_jobs SET status='completed',revision=revision+1,updated_at=? WHERE id=?",
                (now(), row["id"]),
            )
            self._event(conn, row["id"], "completed", {"queue_attempt": claim.get("attempts", 0)})

    def _block(self, conn, row, reason):
        conn.execute(
            "UPDATE collection_recovery_jobs SET status='blocked',reason=?,revision=revision+1,updated_at=? WHERE id=?",
            (reason, now(), row["id"]),
        )
        self._event(conn, row["id"], "blocked", {"reason": reason})

    def _retry_pages(self, conn, row, document, manifest, decision):
        """Archive exact rejected checkpoints in CAS before opening a new burst."""
        from app.contexts.documents.infrastructure.parsers.native_parser import pdf_quality
        from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import read_image_blob
        from app.contexts.documents.infrastructure.recovery.ocr_error_evidence import (
            validate_response_binding,
        )

        updated = deepcopy(manifest)
        by_number = {page.get("page"): page for page in updated.get("pages", [])}
        receipts = []
        for number in decision.pages:
            page = by_number[number]
            if page.get("source_hash") != document["content_hash"]:
                raise ValueError("retry_page_source_mismatch")
            envelope = page["ocr"]["error_response"]
            raw = read_image_blob(self.db.data_dir, row["owner_id"], envelope["blob"])
            evidence = json.loads(raw)
            validate_response_binding(evidence, document["content_hash"], page)
            if evidence.get("http_status") != 429 or any(
                evidence.get(key) != value for key, value in envelope.items() if key != "blob"
            ):
                raise ValueError("retry_requires_bound_429_receipt")
            previous = deepcopy(page)
            payload = {
                "protocol": "ocr-automatic-retry-checkpoint-v1",
                "owner_id": row["owner_id"],
                "document_id": document["id"],
                "version": document["version"],
                "source_hash": document["content_hash"],
                "page": number,
                "recovery_id": row["id"],
                "recovery_count": row["recovery_count"] + 1,
                "previous_page": previous,
                "previous_page_sha256": _digest(previous),
            }
            encoded = _encoded(payload)
            if len(encoded) > 2 * 1024 * 1024:
                raise ValueError("retry_page_checkpoint_too_large")
            blob = self.service.archive.blobs.put_bytes(row["owner_id"], encoded)
            reference = {
                "content_hash": blob.digest,
                "size": blob.size,
                "storage_path": str(blob.path.relative_to(self.db.data_dir)),
                "mime_type": "application/json",
            }
            # Keep bounded inline history as well as CAS. Existing import and
            # backup walkers can still discover every old image/error blob.
            page["ocr_retry_cycles"] = [
                *page.get("ocr_retry_cycles", []),
                {
                    "protocol": payload["protocol"],
                    "blob": reference,
                    "previous_page_sha256": payload["previous_page_sha256"],
                    "previous_page": previous,
                },
            ]
            page.update(status="pending_ocr", ocr={"attempts": 0})
            for key in ("error", "deferred_reason", "ocr_attempt_history", "ocr_retry_history"):
                page.pop(key, None)
            receipts.append({"page": number, "checkpoint": reference})
        updated["quality"] = pdf_quality(updated["pages"])
        if len(_encoded(updated)) > 40 * 1024 * 1024:
            raise ValueError("retry_manifest_too_large")
        conn.execute(
            "UPDATE documents SET manifest=? WHERE id=? AND owner_id=?",
            (dump(updated), document["id"], row["owner_id"]),
        )
        return receipts

    def _resume(self, conn, row, timestamp, *, worker_scope=BackgroundWorkerScope()):
        if not worker_scope.allows(row["owner_id"]):
            return "deferred"  # An excluded row must not gain an event or state change.
        scope, arguments = worker_scope.sql()
        binding = self._binding(conn, row["owner_id"], row["queue_kind"], row["item_id"], row["phase"])
        if binding is None or _digest(binding) != row["binding_sha256"]:
            self._block(conn, row, "source_changed")
            return "blocked"
        processing = row["queue_kind"] == "processing"
        table, key = ("processing_jobs", "document_id") if processing else ("collection_items", "id")
        job = conn.execute(
            f"SELECT * FROM {table} WHERE {key}=? AND owner_id=?", (row["item_id"], row["owner_id"])
        ).fetchone()
        if job["status"] != "failed" or (processing and job["phase"] != row["phase"]):
            self._block(conn, row, "queue_changed")
            return "blocked"
        if _timestamp(job["available_at"]) > timestamp:
            self._defer(conn, row, job["available_at"])
            return "deferred"
        receipts = []
        if processing:
            document = conn.execute(
                "SELECT * FROM documents WHERE id=? AND owner_id=?", (row["item_id"], row["owner_id"])
            ).fetchone()
            manifest = json.loads(document["manifest"])
            decision = classify_processing(manifest, phase=row["phase"], retryable=True)
            if not decision.retryable:
                self._block(conn, row, decision.reason)
                return "blocked"
            if decision.not_before > timestamp:
                self._defer(conn, row, decision.not_before)
                return "deferred"
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')" + scope,
                    arguments,
                ).fetchone()[0]
                >= 200
            ):
                self._defer(conn, row, timestamp)
                return "deferred"
            if decision.pages:
                expected = json.loads(row["retry_plan"])
                actual = [
                    {"page": page["page"], "checkpoint_sha256": _digest(page)}
                    for page in manifest.get("pages", [])
                    if page.get("page") in decision.pages
                ]
                if expected != actual or len({item["page"] for item in actual}) != len(decision.pages):
                    self._block(conn, row, "checkpoint_changed")
                    return "blocked"
                try:
                    receipts = self._retry_pages(conn, row, document, manifest, decision)
                except (OSError, ValueError, TypeError, KeyError):
                    self._block(conn, row, "retry_evidence_invalid")
                    return "blocked"
            conn.execute(
                "UPDATE documents SET status='processing',error=NULL WHERE id=? AND owner_id=?",
                (row["item_id"], row["owner_id"]),
            )
        else:
            run = conn.execute(
                "SELECT * FROM collection_runs WHERE id=? AND owner_id=?", (job["run_id"], row["owner_id"])
            ).fetchone()
            if run is None or run["status"] == "cancelled":
                self._block(conn, row, "cancelled")
                return "blocked"
            if conn.execute(
                "SELECT 1 FROM collection_runs WHERE source_id=? AND id<>? AND status IN ('queued','running')",
                (run["source_id"], run["id"]),
            ).fetchone():
                self._defer(conn, row, timestamp)
                return "deferred"
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE status IN ('queued','fetching')" + scope,
                    arguments,
                ).fetchone()[0]
                >= 2000
                or conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE owner_id=? AND status IN ('queued','fetching')",
                    (row["owner_id"],),
                ).fetchone()[0]
                >= 1000
            ):
                self._defer(conn, row, timestamp)
                return "deferred"
            if job["candidate_id"]:
                candidate = conn.execute(
                    """SELECT c.* FROM checklist_candidates c JOIN checklist_bindings b ON b.id=c.binding_id
                    WHERE c.id=? AND b.owner_id=? AND b.source_id=? AND c.url=?""",
                    (job["candidate_id"], row["owner_id"], run["source_id"], job["url"]),
                ).fetchone()
                if candidate is None or candidate["status"] not in ("pending", "stored"):
                    self._block(conn, row, "candidate_changed")
                    return "blocked"
                conn.execute(
                    "UPDATE checklist_candidates SET status='queued',revision=revision+1 WHERE id=?",
                    (candidate["id"],),
                )
            conn.execute(
                "UPDATE collection_runs SET status='queued',revision=revision+1,updated_at=? WHERE id=?",
                (now(), run["id"]),
            )
        conn.execute(
            f"UPDATE {table} SET status='queued',attempts=0,available_at=?,lease_token=NULL,lease_until=NULL,error=NULL WHERE {key}=? AND owner_id=? AND status='failed'",
            (timestamp, row["item_id"], row["owner_id"]),
        )
        conn.execute(
            "UPDATE collection_recovery_jobs SET status='queued',recovery_count=recovery_count+1,revision=revision+1,updated_at=? WHERE id=?",
            (now(), row["id"]),
        )
        self._event(
            conn,
            row["id"],
            "requeued",
            {
                "previous_queue_attempts": job["attempts"],
                "recovery_count": row["recovery_count"] + 1,
                "binding_sha256": row["binding_sha256"],
                "checkpoints": receipts,
            },
        )
        if processing:
            self.collection._refresh_document(conn, row["item_id"])
        else:
            self.collection._refresh(conn, job["run_id"])
        return "requeued"

    @staticmethod
    def _defer(conn, row, timestamp):
        conn.execute(
            "UPDATE collection_recovery_jobs SET available_at=MAX(available_at,?),revision=revision+1,updated_at=? WHERE id=?",
            (timestamp + 60, now(), row["id"]),
        )

    def adopt_failed(self, conn, *, limit, worker_scope=BackgroundWorkerScope()):
        """Inventory legacy failures conservatively; never guess paid outcomes."""
        scope, arguments = worker_scope.sql("j.owner_id")
        adopted = 0
        for kind, table, key, phase in (
            ("fetch", "collection_items", "id", "'fetch'"),
            ("processing", "processing_jobs", "document_id", "j.phase"),
        ):
            rows = conn.execute(
                f"""SELECT j.* FROM {table} j WHERE j.status='failed' AND NOT EXISTS(
                SELECT 1 FROM collection_recovery_jobs r WHERE r.owner_id=j.owner_id AND r.queue_kind=? AND r.item_id=j.{key} AND r.phase={phase})"""
                + scope
                + f" ORDER BY j.{key} LIMIT ?",
                (kind, *arguments, limit - adopted),
            ).fetchall()
            for row in rows:
                claim = dict(row)
                decision = (
                    self.processing_decision(conn, claim)
                    if kind == "processing"
                    else Decision("legacy_failure_review")
                )
                self.record_failure(
                    conn,
                    claim,
                    processing=kind == "processing",
                    decision=decision,
                    exhausted=True,
                    error_type="legacy_checkpoint",
                )
                adopted += 1
            if adopted >= limit:
                break
        return adopted

    def _queue_legacy_chart_refreshes(self, *, limit, worker_scope):
        """Upgrade old chart-only review holds once their full MinerU receipts verify."""
        from app.contexts.documents.infrastructure.providers.vision import vision_ocr_configured

        if not vision_ocr_configured():
            return 0
        scope, arguments = worker_scope.sql("j.owner_id")
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='parse' AND r.status='blocked'
                        AND r.reason='quality_review' AND j.phase='parse' AND j.status='failed'"""
                    + scope
                    + " ORDER BY r.updated_at,r.id LIMIT ?",
                    (*arguments, limit),
                )
            ]
        promoted = 0
        for identifier in identifiers:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked' AND reason='quality_review'",
                    (identifier,),
                ).fetchone()
                if row is None or not worker_scope.allows(row["owner_id"]):
                    continue
                binding = self._binding(conn, row["owner_id"], "processing", row["item_id"], "parse")
                if binding is None or _digest(binding) != row["binding_sha256"]:
                    continue
                document = conn.execute(
                    "SELECT manifest,content_hash FROM documents WHERE id=? AND owner_id=?",
                    (row["item_id"], row["owner_id"]),
                ).fetchone()
                job = conn.execute(
                    "SELECT phase,status FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (row["item_id"], row["owner_id"]),
                ).fetchone()
                if document is None or job is None or job["phase"] != "parse" or job["status"] != "failed":
                    continue
                try:
                    manifest = json.loads(document["manifest"])
                except (TypeError, ValueError):
                    continue
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("source_hash") != document["content_hash"]
                    or not _legacy_mineru_chart_refresh(manifest)
                ):
                    continue
                part_hashes = [
                    part["result_blob"]["content_hash"] for part in manifest["remote_parse"]["parts"]
                ]
                page_count = sum("chart_content_missing" in page["reasons"] for page in manifest["pages"])
                conn.execute(
                    """UPDATE collection_recovery_jobs SET status='waiting',reason='mineru_chart_refresh',
                    available_at=0,retry_plan='[]',revision=revision+1,updated_at=? WHERE id=?""",
                    (now(), identifier),
                )
                self._event(
                    conn,
                    identifier,
                    "chart_transcription_upgrade_queued",
                    {
                        "source_hash": document["content_hash"],
                        "result_hashes": part_hashes,
                        "chart_review_pages": page_count,
                        "remote_tasks_resubmitted": False,
                    },
                )
                promoted += 1
        return promoted

    def _reconcile_unknown_ragflow(self, *, limit, worker_scope):
        """Reconcile paid-unknown chunk intents by exact, read-only remote receipts."""
        from app.contexts.documents.infrastructure.providers.ragflow import configured, profile

        result = {"reconciled": 0, "partially_reconciled": 0, "receipt_audit_errors": 0}
        if not configured():
            return result
        fingerprint = profile()
        if not fingerprint:
            return result
        scope, arguments = worker_scope.sql("j.owner_id")
        queue_scope, queue_arguments = worker_scope.sql()
        timestamp = time.time()
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='embed' AND r.status='blocked'
                    AND r.reason='paid_result_unknown' AND r.available_at<=?
                    AND j.phase='embed' AND j.status='failed'
                    AND EXISTS(SELECT 1 FROM ragflow_documents d WHERE d.document_id=r.item_id
                        AND d.owner_id=r.owner_id AND d.profile=? AND d.dataset_id IS NOT NULL
                        AND d.remote_id IS NOT NULL)"""
                    + scope
                    + " ORDER BY r.available_at,r.updated_at,r.id LIMIT ?",
                    (timestamp, *arguments, fingerprint, min(limit, 2)),
                )
            ]
        for identifier in identifiers:
            with closing(self.db.connect()) as conn:
                recovery = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=?", (identifier,)
                ).fetchone()
                if recovery is None or not worker_scope.allows(recovery["owner_id"]):
                    continue
                owner, document_id = recovery["owner_id"], recovery["item_id"]
                revision, binding_hash = recovery["revision"], recovery["binding_sha256"]
            try:
                audit = self.service.ragflow.reconcile_pending_intents(document_id, owner)
            except Exception as error:
                code = getattr(error, "code", type(error).__name__)
                wait = min(
                    MAX_RECOVERY_SECONDS,
                    max(
                        BASE_RECOVERY_SECONDS * 2 ** min(recovery["failure_count"], 7),
                        getattr(error, "retry_after", None) or 0,
                    ),
                )
                with self.db.transaction() as conn:
                    current = conn.execute(
                        """SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked'
                        AND reason='paid_result_unknown' AND revision=?""",
                        (identifier, revision),
                    ).fetchone()
                    if current is not None:
                        available = time.time() + wait
                        conn.execute(
                            "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                            (available, now(), identifier),
                        )
                        self._event(
                            conn,
                            identifier,
                            "remote_receipt_audit_deferred",
                            {"error_code": str(code)[:80], "retry_after_seconds": wait},
                        )
                result["receipt_audit_errors"] += 1
                continue
            if audit is None:
                with self.db.transaction() as conn:
                    current = conn.execute(
                        """SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked'
                        AND reason='paid_result_unknown' AND revision=?""",
                        (identifier, revision),
                    ).fetchone()
                    if current is not None:
                        binding = self._binding(conn, owner, "processing", document_id, "embed")
                        if binding is None or _digest(binding) != binding_hash:
                            self._block(conn, current, "source_changed")
                        else:
                            wait = MAX_RECOVERY_SECONDS
                            conn.execute(
                                "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                                (time.time() + wait, now(), identifier),
                            )
                            self._event(
                                conn,
                                identifier,
                                "remote_receipt_audit_skipped",
                                {"reason": "current_generation_unavailable", "retry_after_seconds": wait},
                            )
                continue
            with self.db.transaction() as conn:
                current = conn.execute(
                    """SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked'
                    AND reason='paid_result_unknown' AND revision=?""",
                    (identifier, revision),
                ).fetchone()
                if current is None or not worker_scope.allows(owner):
                    continue
                binding = self._binding(conn, owner, "processing", document_id, "embed")
                if binding is None or _digest(binding) != binding_hash:
                    self._block(conn, current, "source_changed")
                    continue
                job = conn.execute(
                    "SELECT phase,status FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (document_id, owner),
                ).fetchone()
                current_bound = self.service.ragflow.binding(
                    conn, document_id, self.service.embedding_fingerprint(), include_chunks=False
                )
                if (
                    job is None
                    or job["phase"] != "embed"
                    or job["status"] != "failed"
                    or current_bound is None
                    or current_bound[2] != audit.get("generation")
                ):
                    self._block(conn, current, "source_changed")
                    continue
                remote = conn.execute(
                    "SELECT dataset_id,remote_id FROM ragflow_documents WHERE generation=? AND owner_id=? AND profile=?",
                    (audit["generation"], owner, fingerprint),
                ).fetchone()
                if (
                    remote is None
                    or remote["dataset_id"] != audit.get("dataset_id")
                    or remote["remote_id"] != audit.get("remote_id")
                ):
                    self._block(conn, current, "source_changed")
                    continue
                matched = 0
                for local_id, receipt in audit.get("matches", {}).items():
                    intent = conn.execute(
                        """SELECT content_hash,state FROM ragflow_chunks
                        WHERE generation=? AND local_id=?""",
                        (audit["generation"], local_id),
                    ).fetchone()
                    if (
                        intent is None
                        or intent["state"] != "intent"
                        or intent["content_hash"] != receipt.get("content_hash")
                    ):
                        continue
                    conn.execute(
                        """UPDATE ragflow_chunks SET remote_id=?,state='done',error_code=NULL,next_at=0
                        WHERE generation=? AND local_id=? AND state='intent' AND content_hash=?""",
                        (
                            receipt["remote_id"],
                            audit["generation"],
                            local_id,
                            receipt["content_hash"],
                        ),
                    )
                    matched += 1
                remaining = conn.execute(
                    "SELECT count(*) FROM ragflow_chunks WHERE generation=? AND state='intent'",
                    (audit["generation"],),
                ).fetchone()[0]
                detail = {
                    "source_hash": audit.get("source_hash"),
                    "generation": audit.get("generation"),
                    "remote_chunks_scanned": audit.get("remote_chunk_count", 0),
                    "remote_pages_scanned": audit.get("pages", 0),
                    "exact_receipts_reconciled": matched,
                    "unresolved_intents": remaining,
                }
                if remaining:
                    wait = min(
                        MAX_RECOVERY_SECONDS,
                        BASE_RECOVERY_SECONDS * 2 ** min(current["failure_count"], 7),
                    )
                    conn.execute(
                        "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                        (time.time() + wait, now(), identifier),
                    )
                    self._event(conn, identifier, "remote_receipts_reconciled", detail)
                    result["partially_reconciled"] += int(matched > 0)
                    continue
                if (
                    conn.execute(
                        "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')"
                        + queue_scope,
                        queue_arguments,
                    ).fetchone()[0]
                    >= 200
                ):
                    wait = 60
                    conn.execute(
                        "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                        (time.time() + wait, now(), identifier),
                    )
                    self._event(
                        conn,
                        identifier,
                        "remote_receipts_confirmed_queue_deferred",
                        {**detail, "retry_after_seconds": wait},
                    )
                    continue
                conn.execute(
                    """UPDATE processing_jobs SET status='queued',attempts=0,available_at=0,
                    lease_token=NULL,lease_until=NULL,error=NULL,updated_at=?
                    WHERE document_id=? AND owner_id=? AND phase='embed' AND status='failed'""",
                    (now(), document_id, owner),
                )
                conn.execute(
                    "UPDATE documents SET status='processing',error=NULL WHERE id=? AND owner_id=?",
                    (document_id, owner),
                )
                conn.execute(
                    """UPDATE collection_recovery_jobs SET status='queued',reason='remote_receipts_reconciled',
                    available_at=0,recovery_count=recovery_count+1,revision=revision+1,updated_at=? WHERE id=?""",
                    (now(), identifier),
                )
                self._event(conn, identifier, "remote_receipts_reconciled_and_resumed", detail)
                self.collection._refresh_document(conn, document_id)
                result["reconciled"] += 1
        return result

    def run_due(self, *, limit=10, worker_scope=BackgroundWorkerScope()):
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise ValueError("recovery batch must be between 1 and 32")
        result = {
            "adopted": 0,
            "chart_refreshes": 0,
            "reconciled": 0,
            "partially_reconciled": 0,
            "receipt_audit_errors": 0,
            "requeued": 0,
            "blocked": 0,
            "deferred": 0,
        }
        scope, arguments = worker_scope.sql()
        # One target per write transaction: OCR checkpoint validation must not
        # hold a write lock across an entire scheduled batch of large documents.
        for _ in range(limit):
            with self.db.transaction() as conn:
                adopted = self.adopt_failed(conn, limit=1, worker_scope=worker_scope)
            result["adopted"] += adopted
            if not adopted:
                break
        result["chart_refreshes"] = self._queue_legacy_chart_refreshes(limit=limit, worker_scope=worker_scope)
        for key, value in self._reconcile_unknown_ragflow(limit=limit, worker_scope=worker_scope).items():
            result[key] += value
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM collection_recovery_jobs WHERE status='waiting' AND available_at<=?"
                    + scope
                    + " ORDER BY available_at,id LIMIT ?",
                    (time.time(), *arguments, limit),
                )
            ]
        for identifier in identifiers:
            with self.db.transaction() as conn:
                timestamp = time.time()
                row = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='waiting' AND available_at<=?"
                    + scope,
                    (identifier, timestamp, *arguments),
                ).fetchone()
                if row:
                    result[self._resume(conn, row, timestamp, worker_scope=worker_scope)] += 1
        return result
