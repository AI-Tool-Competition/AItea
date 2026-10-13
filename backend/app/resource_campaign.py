"""Resume bounded checklist campaigns through the existing durable queues.

This is an engineering collection account. Candidates and originals remain
drafts; automatic sampling never grants legal approval or national coverage.
"""

import hashlib
import json
from contextlib import closing
from pathlib import Path

from .db import dump, now, uid
from .discovery import recipe_for
from .service import AppError

ACTIVE = {"queued", "running"}


def validate_extension(previous, current):
    if (
        any(previous.get(key) != current.get(key) for key in ("items", "confirm_items", "live_models"))
        or any(current[key] < previous[key] for key in ("discovery_batches", "originals_per_item"))
        or all(current[key] == previous[key] for key in ("discovery_batches", "originals_per_item"))
    ):
        raise ValueError("Extend may only increase budgets within the same plan, items and model mode")


class ResourceCampaign:
    def __init__(self, service, plan_id, options, *, state=None, extend=False, checkpoint=lambda _: None):
        self.service = service
        self.plan = service.checklists.definition(plan_id)
        self.checkpoint = checkpoint
        self.fingerprint = hashlib.sha256(dump(self.plan).encode()).hexdigest()
        definitions = {item["id"]: item for item in self.plan["items"]}
        selected = options["items"] or list(definitions)
        if not selected or len(selected) != len(set(selected)) or set(selected) - definitions.keys():
            raise ValueError("Campaign items must be unique identifiers from the selected plan")
        if not 1 <= options["discovery_batches"] <= 20 or not 0 <= options["originals_per_item"] <= 20:
            raise ValueError("Campaign limits exceed the bounded discovery or original budget")
        if set(options["confirm_items"]) - set(selected):
            raise ValueError("Conditional confirmation must refer to selected items")
        self.options = options | {"items": selected}
        self.definitions = {key: definitions[key] for key in selected}
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
                validate_extension(state["options"], self.options)
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

    def _prepare(self, item_id, record, kind, candidates=None):
        number = len(record[kind + "_runs"])
        payload = {
            "client_request_id": f"campaign:{self.state['id']}:{item_id}:{kind}:{number}",
            "confirm_condition": item_id in self.options["confirm_items"],
        }
        if candidates:
            payload.update(candidate_ids=candidates, level="primary", primary_document_ids=[])
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
            if any(
                runs.get(run_id) in ACTIVE for run_id in record["discovery_runs"] + record["collection_runs"]
            ):
                continue
            with closing(self.service.db.connect()) as conn:
                binding = self.service.checklists.binding(conn, owner, self.plan, item)
                remaining = self.options["originals_per_item"] - len(record["selected_candidates"])
                candidates = []
                waiting = 0
                if binding:
                    excluded = record["selected_candidates"]
                    marks = ",".join("?" for _ in excluded)
                    restriction = f"AND id NOT IN ({marks})" if excluded else ""
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
            if candidates:
                self._prepare(item_id, record, "collection", candidates)
            elif (
                len(record["discovery_runs"]) < self.options["discovery_batches"]
                and (not record["discovery_runs"] or waiting)
                and (self.options["originals_per_item"] == 0 or remaining > 0)
            ):
                self._prepare(item_id, record, "discovery")
            else:
                record["phase"] = "bounded" if waiting or remaining <= 0 else "exhausted"
        active = any(
            record["phase"] not in {"blocked", "skipped", "bounded", "exhausted"}
            for record in self.state["items"].values()
        )
        self.save()
        return active

    def verify_originals(self):
        from .blob_store import _verify_existing

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

    def report(self, status="running"):
        owner = self.state["owner_id"]
        detail = self.service.checklists.detail(owner, self.plan["id"])
        indexed = {item["item_id"]: item for item in detail["items"]}
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
                json_extract(m.payload,'$.review_status') review_status
                FROM checklist_candidates c JOIN checklist_bindings b ON b.id=c.binding_id
                JOIN documents d ON d.id=c.document_id
                LEFT JOIN document_metadata m ON m.document_id=d.id
                WHERE b.owner_id=? AND b.plan_id=? AND b.version=? ORDER BY b.item_id,d.id""",
                (owner, self.plan["id"], self.plan["version"]),
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
                documents.setdefault(row["item_id"], []).append(document)
            for row in conn.execute(
                """SELECT * FROM (SELECT b.item_id,i.url,i.error,i.status,i.attempts,
                ROW_NUMBER() OVER(PARTITION BY b.item_id ORDER BY i.created_at DESC,i.id) n
                FROM collection_items i JOIN collection_runs r ON r.id=i.run_id
                JOIN checklist_bindings b ON b.source_id=r.source_id
                WHERE b.owner_id=? AND i.error IS NOT NULL) WHERE n<=5""",
                (owner,),
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
                "integrity_verified": sum(
                    doc["integrity_verified"] is True for doc in all_documents.values()
                ),
                "remaining_pages": sum(item["counts"]["remaining_pages"] for item in items),
                "failed_queue_items": sum(failures.get(item_id, 0) for item_id in self.definitions),
            },
            "items": items,
        }
