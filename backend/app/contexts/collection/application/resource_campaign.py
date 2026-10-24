"""Resume bounded checklist campaigns through the existing durable queues.

This is an engineering collection account. Candidates and originals remain
drafts; automatic sampling never grants legal approval or national coverage.
"""

import hashlib
import json
import time
from contextlib import closing
from pathlib import Path

from app.contexts.collection.infrastructure.discovery.discovery import recipe_for
from app.shared.domain.errors import AppError
from app.shared.domain.values import dump, now, uid

ACTIVE = {"queued", "running"}


def validate_extension(previous, current):
    added_materials = []
    if previous.get("selection") != current.get("selection"):
        from app.contexts.collection.application.campaign_selection import MaterialSelection

        if not previous.get("selection") or not current.get("selection"):
            raise ValueError("Extend cannot replace sampling with a different material selection")
        before = MaterialSelection.model_validate(previous["selection"])
        after = MaterialSelection.model_validate(current["selection"])
        old = {entry.key: entry.model_dump() for entry in before.materials}
        new = {entry.key: entry.model_dump() for entry in after.materials}
        if (
            before.plan_id != after.plan_id
            or before.source_document_sha256 != after.source_document_sha256
            or any(new.get(key) != entry for key, entry in old.items())
            or not new.keys() - old.keys()
        ):
            raise ValueError("Extend may only append materials while preserving every existing selection")
        added_materials = sorted(new.keys() - old.keys())
    if (
        any(previous.get(key) != current.get(key) for key in ("items", "confirm_items", "live_models"))
        or any(current[key] < previous[key] for key in ("discovery_batches", "originals_per_item"))
        or (
            not added_materials
            and all(current[key] == previous[key] for key in ("discovery_batches", "originals_per_item"))
        )
    ):
        raise ValueError("Extend may only increase budgets within the same plan, items and model mode")
    return added_materials


class ResourceCampaign:
    def __init__(self, service, plan_id, options, *, state=None, extend=False, checkpoint=lambda _: None):
        self.service = service
        if state is not None and (
            not isinstance(state, dict)
            or state.get("plan_id") != plan_id
            or type(state.get("plan_version")) is not int
            or state["plan_version"] < 1
        ):
            raise ValueError("Resume requires the same frozen plan and campaign options")
        self.plan = service.checklists.definition(
            plan_id, version=state.get("plan_version") if state is not None else None
        )
        self.checkpoint = checkpoint
        self.fingerprint = hashlib.sha256(dump(self.plan).encode()).hexdigest()
        definitions = {item["id"]: item for item in self.plan["items"]}
        selected = options["items"] or list(definitions)
        if not selected or len(selected) != len(set(selected)) or set(selected) - definitions.keys():
            raise ValueError("Campaign items must be unique identifiers from the selected plan")
        original_limit = 200 if options.get("selection") else 20
        if (
            not 1 <= options["discovery_batches"] <= 20
            or not 0 <= options["originals_per_item"] <= original_limit
        ):
            raise ValueError("Campaign limits exceed the bounded discovery or original budget")
        if set(options["confirm_items"]) - set(selected):
            raise ValueError("Conditional confirmation must refer to selected items")
        self.options = options | {"items": selected}
        self.definitions = {key: definitions[key] for key in selected}
        from app.contexts.collection.application.campaign_selection import (
            CampaignSelection,
            validate_selection,
        )

        selection = validate_selection(self.plan, self.options)
        if (
            not selection
            and self.plan["country"] == "SG"
            and sum(key.startswith("3.") for key in selected) * options["originals_per_item"] > 45
        ):
            raise ValueError("Singapore regulatory sampling must remain within the checklist limit of 45")
        if state is not None:
            if (
                state.get("format") != 1
                or state.get("plan_hash") != self.fingerprint
                or set(state.get("items", {})) != set(selected)
            ):
                raise ValueError("Resume requires the same frozen plan and campaign options")
            if state.get("options") != self.options:
                if not extend:
                    raise ValueError("Resume requires the same frozen plan and campaign options")
                validate_extension(state["options"], self.options)
            with closing(service.db.connect()) as conn:
                owner = conn.execute("SELECT is_demo FROM users WHERE id=?", (state["owner_id"],)).fetchone()
                if not owner or not owner[0]:
                    raise ValueError("Campaign demonstration account is missing")
            self.state = state
            if extend:
                added = validate_extension(state["options"], self.options)
                state.setdefault("extensions", []).append(
                    {
                        "at": now(),
                        "previous_limits": {
                            key: state["options"][key] for key in ("discovery_batches", "originals_per_item")
                        },
                        "current_limits": {
                            key: self.options[key] for key in ("discovery_batches", "originals_per_item")
                        },
                        "added_material_keys": added,
                    }
                )
                state["options"] = self.options
                state["status"] = "running"
                for record in state["items"].values():
                    if record["phase"] in {"bounded", "exhausted"}:
                        record["phase"] = "waiting"
                self.save()
        else:
            owner = uid()
            with service.db.transaction() as conn:
                conn.execute(
                    "INSERT INTO users VALUES(?,?,?,?,?,?)",
                    (owner, "Resource collection campaign", None, None, 1, now()),
                )
            self.state = {
                "format": 1,
                "id": uid(),
                "owner_id": owner,
                "plan_id": plan_id,
                "plan_version": self.plan["version"],
                "plan_hash": self.fingerprint,
                "options": self.options,
                "started_at": now(),
                "status": "running",
                "items": {
                    key: {
                        "discovery_runs": [],
                        "collection_runs": [],
                        "selected_candidates": [],
                        "phase": "waiting",
                    }
                    for key in selected
                },
            }
            self.save()
        self.selection = CampaignSelection(self, selection) if selection else None
        if self.selection:
            self.selection.seed()

    def save(self):
        self.state["updated_at"] = now()
        self.checkpoint(self.state)

    def _submit(self, item_id, record):
        pending = record["submission"]
        try:
            run = self.service.checklists.start(
                self.state["owner_id"],
                self.plan["id"],
                item_id,
                pending["payload"],
                kind=pending["kind"],
                version=self.plan["version"],
                defer_relations=bool(self.selection and pending["payload"].get("level") == "secondary"),
            )
        except AppError as error:
            # Queue pressure is temporary. Preserve the exact request, including
            # candidate IDs, before retrying or restarting the coordinator.
            if error.code in {"collection_backpressure", "collection_active"}:
                record["error"] = error.message
                return
            record.update(phase="blocked", error=error.message)
        else:
            record[pending["kind"] + "_runs"].append(run["id"])
            record.update(phase=pending["kind"], error=None)
        record.pop("submission", None)
        self.save()

    def _prepare(self, item_id, record, kind, candidates=None, level="primary", anchors=()):
        number = len(record[kind + "_runs"])
        payload = {
            "client_request_id": f"campaign:{self.state['id']}:{item_id}:{kind}:{number}",
            "confirm_condition": item_id in self.options["confirm_items"],
        }
        if candidates:
            payload.update(candidate_ids=candidates, level=level, primary_document_ids=list(anchors))
            record["selected_candidates"].extend(candidates)
        record["submission"] = {"kind": kind, "payload": payload}
        self.save()  # Persist intent BEFORE the service transaction.
        self._submit(item_id, record)

    def step(self):
        """Advance each category independently; a slow peer never gates all originals."""
        owner = self.state["owner_id"]
        with closing(self.service.db.connect()) as conn:
            runs = {
                row["id"]: row["status"]
                for row in conn.execute("SELECT id,status FROM collection_runs WHERE owner_id=?", (owner,))
            }
            fetching = {
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT run_id FROM collection_items WHERE owner_id=? AND status IN ('queued','fetching')",
                    (owner,),
                )
            }
        for item_id, item in self.definitions.items():
            record = self.state["items"][item_id]
            if record["phase"] in {"blocked", "skipped", "bounded", "exhausted"}:
                continue
            if item["conditional"] and item_id not in self.options["confirm_items"]:
                record.update(phase="skipped", error="Execution condition requires explicit confirmation")
                continue
            if item["execution"] != "discovery" or recipe_for(item)["adapter"] == "manual":
                record.update(phase="skipped", error="Specialized review or a source adapter is required")
                continue
            if record.get("submission"):
                self._submit(item_id, record)
                continue
            active_runs = [
                run_id
                for run_id in record["discovery_runs"] + record["collection_runs"]
                if runs.get(run_id) in ACTIVE
            ]
            if active_runs and (
                not self.selection
                or any(run_id in fetching or run_id in record["discovery_runs"] for run_id in active_runs)
            ):
                continue
            with closing(self.service.db.connect()) as conn:
                binding = self.service.checklists.binding(conn, owner, self.plan, item)
                remaining = self.options["originals_per_item"] - len(record["selected_candidates"])
                candidates = []
                level = "primary"
                anchors = []
                record.pop("waiting_for_primary", None)
                waiting = 0
                if binding:
                    excluded = record["selected_candidates"]
                    marks = ",".join("?" for _ in excluded)
                    restriction = f"AND id NOT IN ({marks})" if excluded else ""
                    if self.selection and remaining > 0:
                        candidates, level, anchors, missing = self.selection.choose(
                            conn, binding, record, remaining
                        )
                        if missing:
                            record["waiting_for_primary"] = missing
                    elif not self.selection:
                        candidates = [
                            row[0]
                            for row in conn.execute(
                                f"""SELECT id FROM checklist_candidates WHERE binding_id=? AND status='pending'
                        {restriction} ORDER BY created_at,id LIMIT ?""",
                                (binding["id"], *excluded, max(0, remaining)),
                            )
                        ]
                    waiting = conn.execute(
                        "SELECT COUNT(*) FROM checklist_frontier WHERE binding_id=? AND status!='visited'",
                        (binding["id"],),
                    ).fetchone()[0]
            if active_runs and (not candidates or level != "secondary"):
                continue
            if candidates:
                self._prepare(item_id, record, "collection", candidates, level, anchors)
            elif (
                len(record["discovery_runs"]) < self.options["discovery_batches"]
                and (not record["discovery_runs"] or waiting)
                and (self.options["originals_per_item"] == 0 or remaining > 0)
            ):
                self._prepare(item_id, record, "discovery")
            else:
                record["phase"] = "bounded" if waiting or remaining <= 0 else "exhausted"
        if self.selection:
            for record in self.state["items"].values():
                if record.get("waiting_for_primary") and record["phase"] in {"bounded", "exhausted"}:
                    dependencies = [
                        self.state["items"][self.selection.materials[key].item_id]
                        for key in record["waiting_for_primary"]
                    ]
                    if any(
                        peer["phase"] not in {"blocked", "skipped", "bounded", "exhausted"}
                        for peer in dependencies
                    ):
                        record["phase"] = "waiting"
                    else:
                        record.update(
                            phase="blocked",
                            error="Selected primary originals are unavailable; secondary files remain discovery originals",
                        )
        active = any(
            record["phase"] not in {"blocked", "skipped", "bounded", "exhausted"}
            for record in self.state["items"].values()
        )
        if self.selection:
            self.state["mapping"] = self.selection.associations()
        self.save()
        return active

    def retry_embeddings(self):
        """One explicit retry of complete primaries; no OCR or source gets reset."""
        from app.contexts.knowledge.infrastructure.parsing.ingestion_quality import text_complete

        retried = []
        with self.service.db.transaction() as conn:
            rows = conn.execute(
                """SELECT DISTINCT d.id,d.manifest,b.item_id FROM documents d
                JOIN processing_jobs p ON p.document_id=d.id
                JOIN checklist_candidates c ON c.document_id=d.id JOIN checklist_bindings b ON b.id=c.binding_id
                WHERE d.owner_id=? AND b.plan_id=? AND b.version=? AND p.phase='embed' AND p.status='failed'""",
                (self.state["owner_id"], self.plan["id"], self.plan["version"]),
            ).fetchall()
            for row in rows:
                if (
                    row["item_id"] not in self.definitions
                    or not text_complete(json.loads(row["manifest"]))
                    or not self.service.archive.can_index(conn, row["id"])
                ):
                    continue
                self.service.collection.queue_processing(conn, row["id"], self.state["owner_id"])
                self.service.collection._refresh_document(conn, row["id"])
                self.state["items"][row["item_id"]].update(phase="waiting", error=None)
                retried.append(row["id"])
        self.state.setdefault("embedding_retries", []).append({"at": now(), "document_ids": retried})
        self.save()
        return retried

    def retry_ocr_recovery(self):
        """Requeue draft review pages for a new recovery implementation only."""
        from app.contexts.knowledge.infrastructure.recovery.ocr_recovery import RECOVERY_VERSION

        retried = []
        with self.service.db.transaction() as conn:
            rows = conn.execute(
                """SELECT DISTINCT d.id,d.content_hash,d.manifest,b.item_id
                FROM documents d JOIN processing_jobs p ON p.document_id=d.id
                JOIN checklist_candidates c ON c.document_id=d.id
                JOIN checklist_bindings b ON b.id=c.binding_id
                JOIN document_metadata m ON m.document_id=d.id
                WHERE d.owner_id=? AND b.plan_id=? AND b.version=?
                    AND p.phase='parse' AND p.status='failed'
                    AND json_extract(m.payload,'$.review_status')='draft'""",
                (self.state["owner_id"], self.plan["id"], self.plan["version"]),
            ).fetchall()
            for row in rows:
                if row["item_id"] not in self.definitions:
                    continue
                manifest = json.loads(row["manifest"])
                outdated = [
                    page["page"]
                    for page in manifest.get("pages", [])
                    if page.get("source_hash") == row["content_hash"]
                    and page.get("status") == "ocr_uncertain"
                    and page.get("ocr", {}).get("complete") is True
                    and page.get("ocr", {}).get("recovery", {}).get("version") != RECOVERY_VERSION
                    and page.get("ocr", {}).get("uncertain_spans")
                ]
                if not outdated:
                    continue
                self.service.collection.queue_processing(conn, row["id"], self.state["owner_id"])
                self.service.collection._refresh_document(conn, row["id"])
                self.state["items"][row["item_id"]].update(phase="waiting", error=None)
                retried.append(
                    {"document_id": row["id"], "source_hash": row["content_hash"], "pages": outdated}
                )
        self.state.setdefault("ocr_recovery_retries", []).append(
            {"at": now(), "recovery_version": RECOVERY_VERSION, "documents": retried}
        )
        self.save()
        return retried

    def verify_originals(self):
        from app.shared.infrastructure.blob_store import _verify_existing

        owner = self.state["owner_id"]
        owner_hash = hashlib.sha256(owner.encode()).hexdigest()
        results = {}
        with closing(self.service.db.connect()) as conn:
            documents = conn.execute(
                "SELECT id,content_hash,storage_path,size FROM documents WHERE owner_id=?", (owner,)
            ).fetchall()
        for document in documents:
            digest = document["content_hash"]
            expected = self.service.archive.blobs.blobs_dir / owner_hash / digest[:2] / digest[2:4] / digest
            try:
                if Path(document["storage_path"]) != expected:
                    raise ValueError("Original is outside the account content store")
                _verify_existing(expected, digest, document["size"])
            except (ValueError, OSError):
                results[document["id"]] = False
            else:
                results[document["id"]] = True
        self.state["integrity"] = results
        self.save()

    def revalidate_layout(self):
        """Recheck a derived table warning; retain original OCR pixels and reading."""
        from app.contexts.knowledge.infrastructure.parsing.ingestion_quality import (
            text_complete,
            validate_ocr_page,
        )
        from app.contexts.knowledge.infrastructure.parsing.native import (
            TABLE_LAYOUT_VERSION,
            rebuild_pdf_manifest,
        )
        from app.contexts.knowledge.infrastructure.parsing.parsing import parse_bounded
        from app.shared.infrastructure.blob_store import _verify_existing

        owner = self.state["owner_id"]
        with closing(self.service.db.connect()) as conn:
            rows = conn.execute(
                """SELECT DISTINCT d.*,b.item_id FROM documents d
                JOIN processing_jobs p ON p.document_id=d.id JOIN checklist_candidates c ON c.document_id=d.id
                JOIN checklist_bindings b ON b.id=c.binding_id WHERE d.owner_id=? AND b.plan_id=? AND b.version=?
                AND p.status='failed' AND p.phase='parse' AND d.mime_type='application/pdf' AND d.size<=?""",
                (owner, self.plan["id"], self.plan["version"], 20 * 1024 * 1024),
            ).fetchall()
        results = []
        for row in rows:
            if row["item_id"] not in self.definitions:
                continue
            manifest = json.loads(row["manifest"])
            candidates = [
                page
                for page in manifest.get("pages", [])
                if "table_layout_not_preserved" in page.get("ocr", {}).get("uncertain_spans", [])
                and page.get("ocr", {}).get("complete") is True
            ]
            if not candidates or manifest.get("archive_pdf"):
                continue
            digest = row["content_hash"]
            path = (
                self.service.archive.blobs.blobs_dir
                / hashlib.sha256(owner.encode()).hexdigest()
                / digest[:2]
                / digest[2:4]
                / digest
            )
            if Path(row["storage_path"]) != path:
                raise ValueError("Layout original is outside its account content store")
            _verify_existing(path, digest, row["size"])
            fresh = parse_bounded(row["filename"], path.read_bytes(), row["mime_type"])
            if fresh["source_hash"] != digest or len(fresh.get("pages", [])) != len(manifest["pages"]):
                raise ValueError("Layout review does not match the immutable original")
            changed = []
            for page in candidates:
                original = fresh["pages"][page["page"] - 1]
                if (
                    original["source_hash"] != page["source_hash"]
                    or original["native_text"] != page["native_text"]
                    or "table_layout" in original["reasons"]
                    or original.get("native_table")
                ):
                    continue
                page["layout_revalidation"] = {
                    "version": TABLE_LAYOUT_VERSION,
                    "source_hash": digest,
                    "page": page["page"],
                    "previous_page_hash": hashlib.sha256(dump(page).encode()).hexdigest(),
                    "previous_reasons": list(page["reasons"]),
                    "previous_uncertain_spans": list(page["ocr"]["uncertain_spans"]),
                    "basis": "bounded_original_geometry_recheck_same_native_text",
                }
                page["reasons"] = [reason for reason in page["reasons"] if reason != "table_layout"]
                page["ocr"]["uncertain_spans"] = [
                    warning
                    for warning in page["ocr"]["uncertain_spans"]
                    if warning != "table_layout_not_preserved"
                ]
                validate_ocr_page(page)
                changed.append(page["page"])
            if not changed:
                continue
            rebuilt = rebuild_pdf_manifest(manifest)
            with self.service.db.transaction() as conn:
                current = self.service.own_document(conn, owner, row["id"])
                job = conn.execute(
                    "SELECT status FROM processing_jobs WHERE document_id=?", (row["id"],)
                ).fetchone()
                if current["manifest"] != row["manifest"] or not job or job[0] != "failed":
                    raise ValueError("Layout checkpoint changed during review")
                conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(rebuilt), row["id"]))
                if text_complete(rebuilt):
                    self.service.collection.queue_processing(conn, row["id"], owner)
                    self.service.collection._refresh_document(conn, row["id"])
                    self.state["items"][row["item_id"]].update(phase="waiting", error=None)
            results.append({"document_id": row["id"], "pages": changed, "source_hash": digest})
        self.state.setdefault("layout_revalidations", []).append({"at": now(), "documents": results})
        self.save()
        return results

    def revalidate_native_tables(self):
        """Replace unfinished table-only extraction from exact, verified native cells.

        Only draft campaign originals qualify. Old pages, including OCR images
        and readings, are kept as evidence; independent model uncertainty and
        active processing leases are never overwritten.
        """
        from copy import deepcopy

        from app.contexts.knowledge.infrastructure.parsing.native import rebuild_pdf_manifest
        from app.contexts.knowledge.infrastructure.parsing.parsing import parse_bounded
        from app.contexts.knowledge.infrastructure.parsing.pdf_tables import VERSION
        from app.shared.infrastructure.blob_store import _verify_existing

        owner = self.state["owner_id"]
        with self.service.db.transaction() as conn:
            self.service.collection._expire(conn, "processing_jobs", "processing", time.time())
            rows = conn.execute(
                """SELECT DISTINCT d.*,b.item_id FROM documents d
                JOIN processing_jobs p ON p.document_id=d.id JOIN checklist_candidates c ON c.document_id=d.id
                JOIN checklist_bindings b ON b.id=c.binding_id JOIN document_metadata m ON m.document_id=d.id
                WHERE d.owner_id=? AND b.plan_id=? AND b.version=? AND p.status IN ('queued','failed')
                AND p.phase='parse' AND d.mime_type='application/pdf' AND d.size<=?
                AND json_extract(m.payload,'$.review_status')='draft'""",
                (owner, self.plan["id"], self.plan["version"], 20 * 1024 * 1024),
            ).fetchall()
        results = []
        for row in rows:
            if row["item_id"] not in self.definitions:
                continue
            manifest = json.loads(row["manifest"])
            candidates = [
                page
                for page in manifest.get("pages", [])
                if page.get("reasons") == ["table_layout"]
                and page.get("status") in {"pending_ocr", "ocr_failed", "ocr_uncertain"}
                and page.get("ocr", {}).get("complete") is not False
                and set(page.get("ocr", {}).get("uncertain_spans", [])) <= {"table_layout_not_preserved"}
                and set(page.get("ocr", {}).get("validation_warnings", [])) <= {"table_layout_not_preserved"}
                and (
                    not page.get("ocr", {}).get("text")
                    or (
                        page["ocr"].get("complete") is True
                        and set(page["ocr"].get("uncertain_spans", [])) <= {"table_layout_not_preserved"}
                    )
                )
            ]
            if not candidates or manifest.get("archive_pdf"):
                continue
            digest = row["content_hash"]
            path = (
                self.service.archive.blobs.blobs_dir
                / hashlib.sha256(owner.encode()).hexdigest()
                / digest[:2]
                / digest[2:4]
                / digest
            )
            if Path(row["storage_path"]) != path:
                raise ValueError("Native table original is outside its account content store")
            _verify_existing(path, digest, row["size"])
            fresh = parse_bounded(row["filename"], path.read_bytes(), row["mime_type"])
            if fresh["source_hash"] != digest or len(fresh.get("pages", [])) != len(manifest["pages"]):
                raise ValueError("Native table revalidation does not match the immutable source")
            changed = []
            for old in candidates:
                native = fresh["pages"][old["page"] - 1]
                table = native.get("native_table", {})
                if (
                    native["source_hash"] != old["source_hash"]
                    or native["native_text"] != old["native_text"]
                    or native["status"] != "native"
                    or native["reasons"]
                    or table.get("method") != VERSION
                    or table.get("complete_character_assignment") is not True
                    or table.get("readers") != ["pypdf", "pypdfium2"]
                ):
                    continue
                replacement = deepcopy(native)
                replacement["native_revalidation"] = {
                    "at": now(),
                    "version": VERSION,
                    "source_hash": digest,
                    "basis": "bounded_original_geometry_and_independent_visible_character_assignment",
                    "previous_page_hash": hashlib.sha256(dump(old).encode()).hexdigest(),
                    "previous_page": deepcopy(old),
                }
                # Keep existing page-image exports valid even though the model
                # reading is no longer the current text source.
                if old.get("ocr"):
                    replacement["ocr"] = deepcopy(old["ocr"])
                    replacement["ocr"]["used_for_current_text"] = False
                manifest["pages"][old["page"] - 1] = replacement
                changed.append(old["page"])
            if not changed:
                continue
            rebuilt = rebuild_pdf_manifest(manifest)
            with self.service.db.transaction() as conn:
                current = self.service.own_document(conn, owner, row["id"])
                job = conn.execute(
                    "SELECT status FROM processing_jobs WHERE document_id=?", (row["id"],)
                ).fetchone()
                metadata = conn.execute(
                    "SELECT payload FROM document_metadata WHERE document_id=?", (row["id"],)
                ).fetchone()
                if (
                    current["manifest"] != row["manifest"]
                    or not job
                    or job[0] not in {"queued", "failed"}
                    or json.loads(metadata[0]).get("review_status") != "draft"
                ):
                    raise ValueError("Native table checkpoint changed during revalidation")
                conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(rebuilt), row["id"]))
                self.service.index.sync_document(conn, row["id"], rebuilt)
                self.service.collection.queue_processing(conn, row["id"], owner)
                self.service.collection._refresh_document(conn, row["id"])
                self.state["items"][row["item_id"]].update(phase="waiting", error=None)
            results.append({"document_id": row["id"], "pages": changed, "source_hash": digest})
        self.state.setdefault("native_table_revalidations", []).append({"at": now(), "documents": results})
        self.save()
        return results

    def revalidate_pdf_decode(self):
        """Explicitly quarantine damaged accepted text in owned draft checkpoints."""
        from copy import deepcopy

        from app.contexts.knowledge.infrastructure.parsing.ingestion_quality import (
            pdf_page_status,
            validate_ocr_page,
        )
        from app.contexts.knowledge.infrastructure.parsing.parsing import rebuild_pdf_bounded
        from app.shared.infrastructure.blob_store import _verify_existing

        owner, cursor, results = self.state["owner_id"], "", []
        audit = {"at": now(), "documents": results}
        self.state.setdefault("pdf_decode_revalidations", []).append(audit)
        self.save()
        while True:
            with closing(self.service.db.connect()) as conn:
                rows = conn.execute(
                    """SELECT d.*,GROUP_CONCAT(DISTINCT b.item_id) item_ids,
                    p.status job_status,m.payload metadata_payload
                    FROM documents d JOIN checklist_candidates c ON c.document_id=d.id
                    JOIN checklist_bindings b ON b.id=c.binding_id
                    JOIN document_metadata m ON m.document_id=d.id
                    LEFT JOIN processing_jobs p ON p.document_id=d.id
                    WHERE d.owner_id=? AND b.owner_id=? AND b.plan_id=? AND b.version=?
                    AND d.id>? AND d.role='primary' AND d.mime_type='application/pdf'
                    AND (p.status IS NULL OR p.status IN ('queued','failed','completed'))
                    AND json_extract(m.payload,'$.review_status')='draft'
                    GROUP BY d.id ORDER BY d.id LIMIT 10""",
                    (owner, owner, self.plan["id"], self.plan["version"], cursor),
                ).fetchall()
            if not rows:
                break
            cursor = rows[-1]["id"]
            for row in rows:
                item_ids = set(row["item_ids"].split(",")) & self.definitions.keys()
                if not item_ids:
                    continue
                manifest = json.loads(row["manifest"])
                changed = [
                    page for page in manifest.get("pages", []) if pdf_page_status(page) != page["status"]
                ]
                if not changed:
                    continue
                digest = row["content_hash"]
                if manifest.get("source_hash") != digest or any(
                    page.get("source_hash") != digest for page in manifest["pages"]
                ):
                    raise ValueError("PDF decode checkpoint does not match its immutable source")
                path = (
                    self.service.archive.blobs.blobs_dir
                    / hashlib.sha256(owner.encode()).hexdigest()
                    / digest[:2]
                    / digest[2:4]
                    / digest
                )
                if Path(row["storage_path"]) != path:
                    raise ValueError("PDF decode original is outside its account content store")
                _verify_existing(path, digest, row["size"])
                for page in changed:
                    previous = deepcopy(page)
                    validate_ocr_page(page)
                    page["decode_revalidation"] = {
                        "at": now(),
                        "version": "accepted-pdf-text-v1",
                        "source_hash": digest,
                        "previous_page_hash": hashlib.sha256(dump(previous).encode()).hexdigest(),
                        "previous_page": previous,
                    }
                rebuilt = rebuild_pdf_bounded(manifest)
                with self.service.db.transaction() as conn:
                    current = self.service.own_document(conn, owner, row["id"])
                    job = conn.execute(
                        "SELECT status FROM processing_jobs WHERE document_id=?", (row["id"],)
                    ).fetchone()
                    metadata = conn.execute(
                        "SELECT payload FROM document_metadata WHERE document_id=?", (row["id"],)
                    ).fetchone()
                    if (
                        current["manifest"] != row["manifest"]
                        or current["version"] != row["version"]
                        or current["content_hash"] != digest
                        or (job[0] if job else None) != row["job_status"]
                        or not metadata
                        or metadata[0] != row["metadata_payload"]
                    ):
                        raise ValueError("PDF decode checkpoint changed during revalidation")
                    conn.execute(
                        "UPDATE documents SET manifest=?,status='processing',error=NULL WHERE id=?",
                        (dump(rebuilt), row["id"]),
                    )
                    self.service.index.sync_document(conn, row["id"], rebuilt)
                    self.service.collection.queue_processing(conn, row["id"], owner)
                    conn.execute(
                        "UPDATE processing_jobs SET phase='parse' WHERE document_id=? AND status='queued'",
                        (row["id"],),
                    )
                    self.service.collection._refresh_document(conn, row["id"])
                    for item_id in item_ids:
                        self.state["items"][item_id].update(phase="waiting", error=None)
                results.append(
                    {"document_id": row["id"], "source_hash": digest, "pages": [p["page"] for p in changed]}
                )
                self.save()
        audit["completed_at"] = now()
        self.save()
        return results

    def report(self, status="running"):
        owner = self.state["owner_id"]
        detail = self.service.checklists.detail(owner, self.plan["id"], version=self.plan["version"])
        indexed = {item["item_id"]: item for item in detail["items"]}
        fingerprint = self.service.embedding_fingerprint()
        documents, errors, failures = {}, {}, {}
        with closing(self.service.db.connect()) as conn:
            failures = {
                row["item_id"]: row["failed"]
                for row in conn.execute(
                    """SELECT b.item_id,SUM(i.status='failed') failed FROM collection_items i
                JOIN collection_runs r ON r.id=i.run_id JOIN checklist_bindings b ON b.source_id=r.source_id
                WHERE b.owner_id=? AND b.plan_id=? AND b.version=? GROUP BY b.item_id""",
                    (owner, self.plan["id"], self.plan["version"]),
                )
            }
            # Aggregate bounded metadata in SQLite; never load whole manifests,
            # vectors or legal text into a campaign status report.
            rows = conn.execute(
                """SELECT b.item_id,d.id,d.filename,d.version,d.size,d.status,d.error,d.content_hash,
                json_array_length(d.manifest,'$.chunks') chunks,
                json_array_length(d.manifest,'$.pages') pages,
                json_extract(d.manifest,'$.quality') quality,
                json_extract(m.payload,'$.review_status') review_status,
                l.level legal_level,
                rd.complete index_complete,rd.chunk_count expected_index_chunks,rd.source_hash index_source_hash,
                EXISTS(SELECT 1 FROM retrieval_dirty dirty WHERE dirty.document_id=d.id) index_dirty,
                json_extract(d.manifest,'$.embedding_fingerprint') embedding_fingerprint,
                (SELECT COUNT(*) FROM retrieval_chunks rc WHERE rc.document_id=d.id) indexed_chunks,
                (SELECT COUNT(*) FROM json_each(d.manifest,'$.pages') p
                    WHERE json_type(p.value,'$.ocr.image_blob')='object') ocr_images,
                (SELECT COUNT(*) FROM retrieval_vectors v JOIN retrieval_chunks rc ON rc.rowid=v.chunk_rowid
                    WHERE rc.document_id=d.id AND v.fingerprint=? AND v.input_hash=rc.input_hash) vector_count
                FROM checklist_candidates c JOIN checklist_bindings b ON b.id=c.binding_id
                JOIN documents d ON d.id=c.document_id
                LEFT JOIN document_metadata m ON m.document_id=d.id
                LEFT JOIN legal_classifications l ON l.document_id=d.id AND l.collection=?
                LEFT JOIN retrieval_documents rd ON rd.document_id=d.id
                WHERE b.owner_id=? AND b.plan_id=? AND b.version=? ORDER BY b.item_id,d.id""",
                (
                    fingerprint,
                    f"country:{self.plan['country']}",
                    owner,
                    self.plan["id"],
                    self.plan["version"],
                ),
            )
            for row in rows:
                document = dict(row)
                quality = json.loads(document.pop("quality") or "null")
                document["text_complete"] = bool(document["chunks"]) and (
                    quality is None or quality.get("complete") is True
                )
                document["integrity_verified"] = self.state.get("integrity", {}).get(document["id"])
                document["pending_pages"] = (quality or {}).get("pending_pages", [])
                document["pending_parse_pages"] = (quality or {}).get("pending_parse_pages", [])
                document["failed_pages"] = (quality or {}).get("failed_pages", [])
                document["review_pages"] = (quality or {}).get("review_pages", [])
                document["vector_complete"] = (
                    document["legal_level"] == "primary"
                    and document["text_complete"]
                    and bool(fingerprint)
                    and document["indexed_chunks"] > 0
                    and document["indexed_chunks"] == document["expected_index_chunks"]
                    and document["index_complete"] == 1
                    and document["index_source_hash"] == document["content_hash"]
                    and not document["index_dirty"]
                    and document["embedding_fingerprint"] == fingerprint
                    and document["vector_count"] == document["indexed_chunks"]
                )
                documents.setdefault(row["item_id"], []).append(document)
            for row in conn.execute(
                """SELECT * FROM (SELECT b.item_id,i.url,i.error,i.status,i.attempts,
                ROW_NUMBER() OVER(PARTITION BY b.item_id ORDER BY i.created_at DESC,i.id) n
                FROM collection_items i JOIN collection_runs r ON r.id=i.run_id
                JOIN checklist_bindings b ON b.source_id=r.source_id
                WHERE b.owner_id=? AND b.plan_id=? AND b.version=? AND i.error IS NOT NULL) WHERE n<=5""",
                (owner, self.plan["id"], self.plan["version"]),
            ):
                error = dict(row)
                error.pop("n")
                errors.setdefault(row["item_id"], []).append(error)
        items = []
        for item_id, definition in self.definitions.items():
            progress = indexed[item_id]
            items.append(
                {
                    "item_id": item_id,
                    "title": definition["title"],
                    "kind": definition["kind"],
                    "phase": self.state["items"][item_id]["phase"],
                    "counts": progress["counts"],
                    "last_error": self.state["items"][item_id].get("error"),
                    "documents": documents.get(item_id, []),
                    "errors": errors.get(item_id, []),
                    "discovery_batches": len(self.state["items"][item_id]["discovery_runs"]),
                    "original_attempts": len(self.state["items"][item_id]["selected_candidates"]),
                }
            )
        all_documents = {document["id"]: document for item in items for document in item["documents"]}
        return {
            "status": status,
            "started_at": self.state["started_at"],
            "updated_at": now(),
            "plan_id": self.plan["id"],
            "plan_version": self.plan["version"],
            "country": self.plan["country"],
            "legal_review": "draft_only_not_legal_approval",
            "mapping": self.state.get("mapping", []),
            "scope": self.options,
            "workers": {
                "download": self.service.collection.download_workers,
                "per_host": self.service.collection.host_workers,
                "processing": self.service.collection.processing_workers,
            },
            "summary": {
                "categories": len(items),
                "categories_with_candidates": sum(bool(item["counts"]["candidates"]) for item in items),
                "categories_with_originals": sum(bool(item["documents"]) for item in items),
                "pages": sum(item["counts"]["pages"] for item in items),
                "candidates": sum(item["counts"]["candidates"] for item in items),
                "originals": len(all_documents),
                "original_bytes": sum(doc["size"] for doc in all_documents.values()),
                "text_complete": sum(doc["text_complete"] for doc in all_documents.values()),
                "primary_originals": sum(doc["legal_level"] == "primary" for doc in all_documents.values()),
                "secondary_originals": sum(
                    doc["legal_level"] == "secondary" for doc in all_documents.values()
                ),
                "primary_vector_complete": sum(doc["vector_complete"] for doc in all_documents.values()),
                "vectors": sum(doc["vector_count"] for doc in all_documents.values()),
                "ocr_images": sum(doc["ocr_images"] for doc in all_documents.values()),
                "secondary_vectors": sum(
                    doc["vector_count"] for doc in all_documents.values() if doc["legal_level"] == "secondary"
                ),
                "mapped_relations": sum(row["status"] == "mapped" for row in self.state.get("mapping", [])),
                "unresolved_relations": sum(
                    row["status"] != "mapped" for row in self.state.get("mapping", [])
                ),
                "integrity_verified": sum(
                    doc["integrity_verified"] is True for doc in all_documents.values()
                ),
                "remaining_pages": sum(item["counts"]["remaining_pages"] for item in items),
                "failed_queue_items": sum(failures.get(item_id, 0) for item_id in self.definitions),
            },
            "items": items,
        }
