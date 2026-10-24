"""Freeze one drained OCR scope, then optionally run the existing embedding supervisor.

Without --apply this only reads the corpus and writes private handoff evidence.
--apply explicitly authorizes Service initialization by the existing preflight,
then paid embedding work. It never authorizes retrying an uncertain request.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import time
from collections import Counter
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "scripts")]

from complete_embeddings import (  # noqa: E402
    digest,
    stable_manifest,
    unresolved_issued_inputs,
)
from embedding_supervisor import persist  # noqa: E402
from process_frozen_parse import (  # noqa: E402
    frozen_inputs,
    inspect_frozen_document,
    read_db,
)

PROTOCOL = "ocr-embedding-handoff-v1"
SOURCE_FIELDS = (
    "id",
    "owner_id",
    "version",
    "content_hash",
    "source_url",
    "size",
    "storage_path",
)
INTEGRITY = {
    "selected_originals_and_all_text_unchanged",
    "all_old_vectors_exact_bytes",
    "metadata_classifications_relations_unchanged",
}


def read_json(path):
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("Expected an evidence object: " + str(path))
    return value


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def reference(path):
    return {"path": str(path.resolve(strict=True)), "sha256": sha(path)}


def referenced(ref):
    path = Path(ref["path"]).resolve(strict=True)
    if sha(path) != ref["sha256"]:
        raise ValueError("Evidence bytes changed: " + str(path))
    return read_json(path)


def runtime_digest():
    # Operators freeze this tree before production use. Bind every imported
    # application/helper/config file, rather than an unrelated ancestor HEAD.
    return digest(
        {
            str(path.relative_to(ROOT)): sha(path)
            for directory in ("backend/app", "scripts", "config")
            for path in sorted((ROOT / directory).rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        }
    )


def owned_lock(path, inherited_fd=None):
    handle = path.open("a") if inherited_fd is None else os.fdopen(inherited_fd, "a")
    try:
        actual, expected = os.fstat(handle.fileno()), path.stat()
        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (
            expected.st_dev,
            expected.st_ino,
        ):
            raise ValueError("Inherited descriptor is not this handoff lock")
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Only this verified, currently held descriptor is ever passed to a child.
        return handle
    except BaseException:
        handle.close()
        raise


def exclusions(args):
    from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID

    policy = read_json(args.exclusions)
    if (
        policy.get("protocol") != "ocr-embedding-exclusions-v1"
        or Path(policy["handoff_root"]).resolve() != args.handoff_root
        or policy.get("owner") != SYSTEM_OWNER_ID
        or Path(policy["admission_path"]).resolve() != args.admission_path
    ):
        raise ValueError("Exclusions must bind the shared registry, owner and admission path")
    documents, inputs = set(), set()
    plans = policy["embedding_plans"]
    if (
        sum(p.get("kind") == "legacy_fixed" for p in plans) != 1
        or not any(p.get("kind") == "prior_handoff" for p in plans)
        or any(p.get("kind") not in {"legacy_fixed", "prior_handoff"} for p in plans)
    ):
        raise ValueError("Require the complete legacy fixed plan, including all its inputs")
    for entry in plans:
        plan, inventory = referenced(entry["plan"]), referenced(entry["inputs"])
        docs = {doc["id"]: doc for doc in plan["documents"]}
        if (
            plan.get("protocol") != "fixed-embedding-continuation-v1"
            or plan["owner"] != policy["owner"]
            or plan["fingerprint"] != policy["fingerprint"]
            or len(docs) != len(plan["documents"])
            or len(docs) != entry["expected_documents"]
            or inventory.get("protocol") != "fixed-embedding-input-inventory-v1"
            or inventory.get("plan_sha256") != entry["plan"]["sha256"]
            or inventory.get("plan_digest") != digest(plan)
            or set(inventory["documents"]) != set(docs)
        ):
            raise ValueError("Incomplete fixed-plan exclusion evidence")
        hashes = set()
        for identifier, rows in inventory["documents"].items():
            if (
                not rows
                or len(rows) != docs[identifier]["chunk_count"]
                or [row["ordinal"] for row in rows] != list(range(len(rows)))
            ):
                raise ValueError("Exclusion inventory omits fixed inputs")
            for row in rows:
                value = row["input_hash"]
                if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                    raise ValueError("Invalid excluded input hash")
                hashes.add(value)
        if inventory.get("unique_inputs_sha256") != digest(sorted(hashes)):
            raise ValueError("Excluded input set digest mismatch")
        documents.update(docs)
        inputs.update(hashes)
    holds, historical = (
        referenced(policy["ocr_holds"]),
        referenced(policy["ocr_history"]),
    )
    held = set(holds["historical_paid_missing_document_ids"]) | set(historical["additional_document_holds"])
    for key in (
        "current_records",
        "historical_records",
        "historical_paid_missing_records",
    ):
        held.update(row["document_id"] for row in holds[key])
    if len(held) != policy["expected_held_documents"]:
        raise ValueError("Historical OCR hold evidence is incomplete")
    documents.update(held)
    return policy, documents, inputs


def configured_model(args, policy):
    from app.bootstrap import Service
    from app.contexts.documents.infrastructure.providers.embedding_admission import (
        model_scope,
    )
    from verify_ingestion import configure_models

    configure_models(args.models_env)
    # These methods only read configuration; never construct Service here.
    fingerprint = Service.embedding_fingerprint(None)
    if not fingerprint or fingerprint != policy["fingerprint"]:
        raise ValueError("Embedding configuration differs from the exclusion scope")
    scope = model_scope(os.environ["EMBEDDING_BASE_URL"], os.environ["EMBEDDING_MODEL"])
    if scope != policy["admission_scope"]:
        raise ValueError("Provider admission scope differs")
    with closing(sqlite3.connect(args.admission_path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only=ON")
        row = conn.execute("SELECT capacity FROM budgets WHERE scope=?", (scope,)).fetchone()
        if row != (64,):
            raise ValueError("Require the existing shared 64-request admission budget")


def ocr_terminal(args, policy):
    from frozen_parse_supervisor import completed_window, load_success_guard

    report, initial = frozen_inputs(args.ocr_snapshot, Path(policy["data_dir"]))
    binding = read_json(args.ocr_snapshot / "supervisor-binding.json")
    state = read_json(args.ocr_snapshot / "supervisor-state.json")
    selected = json.loads((args.ocr_snapshot / "initial-parse-ids.json").read_bytes())
    # The first version supports the entire original fixed scope only. Subscope
    # discovery or a missing terminal report cannot confer new authorization.
    if (
        report["owner_id"] != policy["owner"]
        or binding.get("selected_ids_sha256") != digest(selected)
        or binding.get("selected_documents") != len(selected)
        or binding.get("owner_id") != policy["owner"]
        or binding.get("data_dir") != str(Path(policy["data_dir"]).resolve())
        or binding.get("snapshot") != str(args.ocr_snapshot)
        or type(state.get("generation")) is not int
        or state["generation"] < 1
        or state.get("status") not in {"completed", "blocked_quality", "blocked_inputs"}
        or state.get("active_run_id") is not None
        or state.get("child_pid") is not None
        or not state.get("last_run_id")
    ):
        raise ValueError("OCR scope is not a trusted, fully drained terminal scope")
    for name, expected in binding["snapshot_sha256"].items():
        if name not in {
            "initial-report.json",
            "initial-parse-ids.json",
            "initial-parse-documents.json",
        }:
            raise ValueError("Unexpected OCR snapshot evidence path")
        if sha(args.ocr_snapshot / name) != expected:
            raise ValueError("Frozen OCR evidence changed")
    if set(binding["snapshot_sha256"]) != {
        "initial-report.json",
        "initial-parse-ids.json",
        "initial-parse-documents.json",
    }:
        raise ValueError("Incomplete OCR snapshot binding")
    run_id = state["last_run_id"]
    if not all(c.isalnum() or c in "-_" for c in run_id):
        raise ValueError("Invalid OCR terminal run ID")
    pattern = re.compile(re.escape(binding["run_prefix"]) + r"-(\d{6})-[0-9a-f]{12}")
    windows = {}
    for path in (args.ocr_snapshot / "runs").iterdir():
        match = pattern.fullmatch(path.name)
        if match:
            generation = int(match[1])
            if generation in windows:
                raise ValueError("Multiple OCR windows claim the same generation")
            windows[generation] = path
    if set(windows) != set(range(1, state["generation"] + 1)) or windows[state["generation"]].name != run_id:
        raise ValueError("OCR generation history is incomplete")
    launches = json_lines(args.ocr_snapshot / "supervisor-events.jsonl")
    window_paths = [args.ocr_snapshot / "supervisor-events.jsonl"]
    for generation, path in sorted(windows.items()):
        if not any(
            event.get("generation") == generation
            and event.get("status") == "launching"
            and event.get("active_run_id") == path.name
            for event in launches
        ):
            raise ValueError("OCR window lacks its durable launch intent")
        terminal = completed_window(SimpleNamespace(snapshot=args.ocr_snapshot), binding, selected, path.name)
        if terminal["status"] != "drained" or terminal["report"].get("blockers"):
            raise ValueError("OCR terminal report is missing, blocked or untrusted")
        audit_ocr_events(path / "events.jsonl", terminal["report"], set(selected))
        window_paths.extend(path / name for name in ("run-input.json", "report.json", "events.jsonl"))
    success = load_success_guard(args.ocr_snapshot, binding)
    if set(success) != set(selected):
        raise ValueError("Incomplete cross-window success guard")
    paths = [
        args.ocr_snapshot / name
        for name in (
            "initial-report.json",
            "initial-parse-ids.json",
            "initial-parse-documents.json",
            "supervisor-binding.json",
            "supervisor-state.json",
            "supervisor-success-pages.json",
        )
    ]
    paths.extend(window_paths)
    return initial, selected, success, binding, [reference(path) for path in paths]


def json_lines(path):
    raw = path.read_bytes()
    if raw and not raw.endswith(b"\n"):
        raise ValueError("Truncated event journal: " + str(path))
    result = [json.loads(line) for line in raw.splitlines()]
    if any(not isinstance(item, dict) for item in result):
        raise ValueError("Invalid event journal: " + str(path))
    return result


def audit_ocr_events(path, report, selected):
    events = json_lines(path)
    transport = report.get("transport", {})
    feedback = Counter(
        f"http_{row['status_code']}" if row.get("status_code") else "transport_" + str(row.get("timeout"))
        for row in events
        if row.get("kind") == "provider_result"
    )
    reported_feedback = {
        key: value for key, value in transport.items() if key.startswith(("http_", "transport_"))
    }
    checkpoints = [
        {k: v for k, v in row.items() if k not in {"kind", "at"}}
        for row in events
        if row.get("kind") == "document_checkpoint"
    ]
    if (
        any(row.get("document_id") and row["document_id"] not in selected for row in events)
        or transport.get("inflight", 0) != 0
        or transport.get("unknown_paid_outcome", 0) != 0
        or sum(row.get("kind") == "claim" for row in events) != report["claims"]
        or sum(row.get("kind") == "request_started" for row in events) != transport.get("requests", 0)
        or feedback != reported_feedback
        or sorted(map(digest, checkpoints)) != sorted(map(digest, report["document_checkpoints"]))
    ):
        raise ValueError("OCR event journal does not close against its terminal report")


def inspect_document(conn, data, initial, success, identifier, fingerprint):
    from app.contexts.documents.application.archive import LegalArchive
    from app.contexts.documents.application.retrieval import embedding_text, input_hash
    from app.contexts.documents.infrastructure.parsers.ingestion_quality import (
        pdf_page_status,
        text_complete,
    )
    from app.shared.domain.countries import document_country

    checked, manifest = inspect_frozen_document(
        data, initial, identifier, successful_pages=success[identifier]
    )
    row = conn.execute(
        "SELECT d.*,j.phase,j.status job_status,j.available_at,j.lease_token,j.lease_until,"
        "m.payload metadata,m.revision metadata_revision FROM documents d "
        "JOIN processing_jobs j ON j.document_id=d.id "
        "LEFT JOIN document_metadata m ON m.document_id=d.id WHERE d.id=?",
        (identifier,),
    ).fetchone()
    if not row or dict(row) != checked:
        raise ValueError("Document changed while capturing the read-only snapshot")
    original = Path(row["storage_path"])
    if original.stat().st_size != row["size"] or sha(original) != row["content_hash"]:
        raise ValueError("Original bytes differ from the frozen OCR source")
    accepted = [
        {
            "page": page["page"],
            "validated_status": pdf_page_status(page),
            "sha256": digest(page),
        }
        for page in manifest.get("pages", [])
        if pdf_page_status(page) in {"native", "ocr_complete"}
    ]
    if accepted != success[identifier]:
        raise ValueError("Accepted pages differ from the drained OCR success ledger")
    country = document_country(conn, identifier)
    classifications = [
        dict(r)
        for r in conn.execute(
            "SELECT collection,level,revision FROM legal_classifications WHERE document_id=? ORDER BY collection",
            (identifier,),
        )
    ]
    metadata = json.loads(row["metadata"]) if row["metadata"] else None
    evidence = {
        **{key: row[key] for key in SOURCE_FIELDS},
        "stable_manifest": stable_manifest(row["manifest"]),
        "accepted_pages": accepted,
        "country": country,
        "role": row["role"],
        "classifications": classifications,
        "metadata_revision": row["metadata_revision"],
        "metadata_sha256": hashlib.sha256((row["metadata"] or "").encode()).hexdigest(),
        "review_status": metadata.get("review_status") if metadata else None,
        "text_complete": text_complete(manifest),
        "can_index": LegalArchive.can_index(None, conn, identifier, "country:" + (country or "")),
    }
    index = conn.execute("SELECT * FROM retrieval_documents WHERE document_id=?", (identifier,)).fetchone()
    dirty = conn.execute("SELECT 1 FROM retrieval_dirty WHERE document_id=?", (identifier,)).fetchone()
    if not proof_ready(evidence, index, dirty, row):
        return row, {**evidence, "index_valid": False}, [], 0, False
    rows = conn.execute(
        "SELECT c.*,v.chunk_rowid vector_row,v.fingerprint vector_fp,v.input_hash vector_input "
        "FROM retrieval_chunks c LEFT JOIN retrieval_vectors v ON v.chunk_rowid=c.rowid "
        "WHERE c.document_id=? ORDER BY c.ordinal,c.rowid",
        (identifier,),
    ).fetchall()
    chunks = [c for c in manifest.get("chunks", []) if str(c.get("text", "")).strip()]
    inventory = []
    for ordinal, (chunk, expected) in enumerate(zip(rows, chunks, strict=True)):
        if (
            chunk["ordinal"] != ordinal
            or chunk["chunk_id"] != str(expected.get("id") or f"chunk-{ordinal}")
            or chunk["text"] != expected["text"]
            or chunk["retrieval_text"] != embedding_text(expected)
            or chunk["input_hash"] != input_hash(chunk["retrieval_text"])
            or json.loads(chunk["locator"]) != expected["locator"]
        ):
            raise ValueError("Retrieval chunks differ from complete manifest inputs")
        inventory.append(
            {
                "chunk_id": chunk["chunk_id"],
                "ordinal": ordinal,
                "input_hash": chunk["input_hash"],
                "text_sha256": hashlib.sha256(chunk["retrieval_text"].encode()).hexdigest(),
                "locator_sha256": digest(json.loads(chunk["locator"])),
            }
        )
    evidence["index_valid"] = bool(
        index
        and index["complete"]
        and index["chunk_count"] == len(rows)
        and index["source_hash"] == row["content_hash"]
        and not dirty
        and rows
    )
    missing = sum(r["vector_row"] is None for r in rows)
    # Existing vectors for another model require an explicit recovery plan;
    # this additive handoff must never replace an old physical vector row.
    wrong_vector = any(
        r["vector_row"] is not None
        and (r["vector_fp"] != fingerprint or r["vector_input"] != r["input_hash"])
        for r in rows
    )
    return row, evidence, inventory, missing, wrong_vector


def proof_ready(evidence, index, dirty, row):
    return bool(
        evidence["text_complete"]
        and index
        and index["complete"]
        and not dirty
        and index["source_hash"] == row["content_hash"]
    )


def capture_candidates(args, policy, excluded_docs, excluded_inputs):
    data = Path(policy["data_dir"]).resolve(strict=True)
    with ExitStack() as stack:
        # Existing lock files only: taking a shared operation's idle lock must
        # not create or repair its historical supervision evidence.
        for name in (".parse-supervisor.lock", ".parse-owner.lock"):
            handle = stack.enter_context((args.ocr_snapshot / name).open("rb"))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        initial, selected, success, binding, refs = ocr_terminal(args, policy)
        documents, evidence, inventory, deferred = [], {}, {}, {}
        missing_rows = 0
        with closing(read_db(data)) as conn:
            conn.execute("BEGIN")
            marks = ",".join("?" for _ in selected)
            if conn.execute(
                f"SELECT 1 FROM processing_jobs WHERE document_id IN ({marks}) AND phase='parse' "
                "AND (status='processing' OR lease_token IS NOT NULL) LIMIT 1",
                selected,
            ).fetchone():
                raise ValueError("OCR terminal scope still has a parse claim")
            for identifier in selected:
                if identifier in excluded_docs:
                    deferred[identifier] = "excluded_document_or_prior_reservation"
                    continue
                row, proof, inputs, missing, wrong = inspect_document(
                    conn, data, initial, success, identifier, policy["fingerprint"]
                )
                reason = None
                if row["owner_id"] != policy["owner"] or row["case_id"] or row["role"] != "primary":
                    reason = "not_shared_primary"
                elif not proof["text_complete"]:
                    reason = "incomplete_or_quality_held"
                elif not proof["can_index"] or not any(
                    r["collection"] == "country:" + (proof["country"] or "") and r["level"] == "primary"
                    for r in proof["classifications"]
                ):
                    reason = "no_explicit_national_primary_classification"
                elif proof["review_status"] != "draft":
                    reason = "not_draft"
                elif not proof["index_valid"] or wrong:
                    reason = "index_dirty_incomplete_or_existing_vector_mismatch"
                elif row["phase"] != "embed" or row["job_status"] != "queued" or row["lease_token"]:
                    reason = "requires_existing_queue_service"
                elif row["available_at"] > time.time() or (row["lease_until"] or 0) > time.time():
                    reason = "active_lease_or_cooldown"
                elif excluded_inputs.intersection(r["input_hash"] for r in inputs):
                    reason = "excluded_fixed_input_or_prior_reservation"
                elif not missing:
                    reason = "already_embedded"
                if reason:
                    deferred[identifier] = reason
                    continue
                documents.append(
                    {
                        **{key: row[key] for key in SOURCE_FIELDS},
                        "chunk_count": len(inputs),
                    }
                )
                evidence[identifier], inventory[identifier] = proof, inputs
                missing_rows += missing
        return {
            "documents": sorted(documents, key=lambda item: item["id"]),
            "evidence": evidence,
            "inventory": inventory,
            "deferred": deferred,
            "missing_rows": missing_rows,
            "ocr_binding": digest(binding),
            "ocr_evidence": refs,
            "ocr_selected": selected,
        }


def validate_batch(root, entry):
    batch = root / entry["batch_id"]
    if len(entry["batch_id"]) != 64 or any(c not in "0123456789abcdef" for c in entry["batch_id"]):
        raise ValueError("Invalid batch reservation")
    for name, expected in entry["files"].items():
        if name not in {
            "selection.json",
            "selection-proof.json",
            "input-inventory.json",
        }:
            raise ValueError("Unexpected reserved evidence filename")
        if sha(batch / name) != expected:
            raise ValueError("Reserved batch evidence changed or disappeared")
    if set(entry["files"]) != {
        "selection.json",
        "selection-proof.json",
        "input-inventory.json",
    }:
        raise ValueError("Incomplete batch reservation")
    if str(batch / "output") != entry["output"] or read_json(batch / "output/identity.json") != {
        "batch_id": entry["batch_id"],
        "plan_sha256": entry["files"]["selection.json"],
    }:
        raise ValueError("Original output identity disappeared; never create a replacement")
    inventory = read_json(batch / "input-inventory.json")
    inputs = {row["input_hash"] for rows in inventory["documents"].values() for row in rows}
    for name in ("requests.jsonl", "issued-requests.jsonl"):
        for row in json_lines(batch / "output" / name):
            if (
                not row.get("run_id")
                or not row.get("request_id")
                or type(row.get("http_started")) is not bool
                or not row.get("input_hashes")
                or not set(row["input_hashes"]).issubset(inputs)
            ):
                raise ValueError("Request journal lost its fixed input identity")
    if entry.get("paid_launch_intent"):
        for name in (
            "baseline.json",
            "existing-metadata-guard.json",
            "successful-inputs.sqlite3",
        ):
            if not (batch / "output" / name).is_file():
                raise ValueError("Launched batch lost a baseline or receipt cache")
    if entry["status"] == "completed" and reference(batch / "output/report.json") != entry["final_report"]:
        raise ValueError("Completed batch lost its immutable terminal receipt")
    return batch


def prepare(args):
    policy, excluded_docs, excluded_inputs = exclusions(args)
    configured_model(args, policy)
    root = args.handoff_root
    binding = {
        "protocol": PROTOCOL,
        "policy": reference(args.exclusions),
        "snapshot": str(args.ocr_snapshot),
        "models_file_sha256": sha(args.models_env),
        "runtime_sha256": runtime_digest(),
    }
    state_path = root / "handoff-state.json"
    if state_path.exists():
        state = read_json(state_path)
        if state.get("binding") != binding:
            raise ValueError("Registry binding changed; cross-scope discovery is deferred")
    else:
        if any(path.name != ".handoff.lock" for path in root.iterdir()):
            raise ValueError("Missing registry for existing evidence; refusing an empty replacement")
        state = {"binding": binding, "batches": []}
        persist(state_path, state)
    for entry in state["batches"]:
        batch = validate_batch(root, entry)
        inventory = read_json(batch / "input-inventory.json")
        excluded_docs.update(inventory["documents"])
        excluded_inputs.update(r["input_hash"] for rows in inventory["documents"].values() for r in rows)
        if entry["status"] != "completed":
            return state, entry
    captured = capture_candidates(args, policy, excluded_docs, excluded_inputs)
    persist(
        root / "latest-prepare.json",
        {
            "status": "prepared" if captured["documents"] else "deferred",
            "selected_documents": len(captured["documents"]),
            "missing_rows": captured["missing_rows"],
            "deferred": captured["deferred"],
            "business_database_writes": 0,
            "model_requests": 0,
        },
    )
    if not captured["documents"]:
        return state, None
    batch_id = digest(
        {
            "ocr_binding": captured["ocr_binding"],
            "fingerprint": policy["fingerprint"],
            "documents": captured["evidence"],
        }
    )
    batch = root / batch_id
    if batch.exists():
        raise ValueError("Unregistered deterministic batch exists; preserve and inspect it")
    plan = {
        "protocol": "fixed-embedding-continuation-v1",
        "data_dir": str(Path(policy["data_dir"]).resolve()),
        "owner": policy["owner"],
        "fingerprint": policy["fingerprint"],
        "documents": captured["documents"],
    }
    inventory = {
        "protocol": "fixed-embedding-input-inventory-v1",
        "documents": captured["inventory"],
        "plan_digest": digest(plan),
        "unique_inputs_sha256": digest(
            sorted({r["input_hash"] for rows in captured["inventory"].values() for r in rows})
        ),
    }
    # The reservation precedes publication. A crash here blocks, rather than
    # making a second output eligible for the same possibly paid inputs.
    entry = {
        "batch_id": batch_id,
        "status": "reserved",
        "output": str(batch / "output"),
        "files": {},
    }
    state["batches"].append(entry)
    persist(state_path, state)
    batch.mkdir(mode=0o700)
    (batch / "output").mkdir(mode=0o700)
    persist(batch / "selection.json", plan)
    inventory["plan_sha256"] = sha(batch / "selection.json")
    persist(batch / "input-inventory.json", inventory)
    persist(
        batch / "selection-proof.json",
        {
            "protocol": PROTOCOL,
            "created_at": time.time(),
            "runtime_sha256": binding["runtime_sha256"],
            "plan_sha256": inventory["plan_sha256"],
            "plan_digest": digest(plan),
            "ocr_binding": captured["ocr_binding"],
            "ocr_selected_ids_sha256": digest(captured["ocr_selected"]),
            "selected_ids_sha256": digest([d["id"] for d in plan["documents"]]),
            "ocr_evidence": captured["ocr_evidence"],
            "exclusions": binding["policy"],
            "documents": captured["evidence"],
            "deferred": captured["deferred"],
            "missing_rows": captured["missing_rows"],
        },
    )
    persist(
        batch / "output/identity.json",
        {"batch_id": batch_id, "plan_sha256": inventory["plan_sha256"]},
    )
    for name in ("requests.jsonl", "issued-requests.jsonl"):
        with (batch / "output" / name).open("x") as stream:
            stream.flush()
            os.fsync(stream.fileno())
    entry.update(
        status="prepared",
        files={
            name: sha(batch / name)
            for name in (
                "selection.json",
                "selection-proof.json",
                "input-inventory.json",
            )
        },
    )
    persist(state_path, state)
    return state, entry


def runner_arguments(args, batch):
    return [
        "--plan",
        str(batch / "selection.json"),
        "--output",
        str(batch / "output"),
        "--models-env",
        str(args.models_env),
        "--admission-path",
        str(args.admission_path),
        "--http-workers",
        "64",
        "--document-workers",
        "12",
        "--request-concurrency",
        "2",
        "--batch-size",
        "2",
        "--http-timeout",
        "60",
    ]


def revalidate(args, batch):
    policy = read_json(args.exclusions)
    proof = read_json(batch / "selection-proof.json")
    inventory = read_json(batch / "input-inventory.json")
    data = Path(policy["data_dir"])
    with ExitStack() as stack:
        for name in (".parse-supervisor.lock", ".parse-owner.lock"):
            handle = stack.enter_context((args.ocr_snapshot / name).open("rb"))
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        initial, _, success, binding, refs = ocr_terminal(args, policy)
        if digest(binding) != proof["ocr_binding"] or refs != proof["ocr_evidence"]:
            raise ValueError("OCR evidence bytes changed after reservation")
        with closing(read_db(data)) as conn:
            conn.execute("BEGIN")
            for identifier, expected in proof["documents"].items():
                _, current, inputs, _, wrong = inspect_document(
                    conn, data, initial, success, identifier, policy["fingerprint"]
                )
                if current != expected or inputs != inventory["documents"][identifier] or wrong:
                    raise ValueError("Reserved input or eligibility evidence changed")


def completion(batch):
    from app.contexts.documents.application.retrieval import stored_vector

    plan = read_json(batch / "selection.json")
    output = batch / "output"
    report = read_json(output / "report.json")
    state = read_json(output / "supervisor-state.json")
    integrity = report.get("integrity", {})
    if not (
        state.get("status") == "completed"
        and state.get("plan_sha256") == sha(batch / "selection.json")
        and state.get("last_run_id") == report.get("run_id")
        and state.get("owner") == plan["owner"]
        and state.get("fingerprint") == plan["fingerprint"]
        and report.get("missing_rows") == 0
        and report.get("active_claims") == 0
        and report.get("workers_running") is False
        and report.get("failure") is None
        and report.get("failed_documents") == 0
        and report.get("quarantined_inputs") == 0
        and INTEGRITY.issubset(integrity)
        and all(value is True for value in integrity.values())
    ):
        raise ValueError("Missing final completion and integrity evidence")
    with closing(
        sqlite3.connect((output / "successful-inputs.sqlite3").as_uri() + "?mode=ro", uri=True)
    ) as conn:
        if json.loads(conn.execute("SELECT payload FROM scope WHERE version=1").fetchone()[0]) != {
            "owner": plan["owner"],
            "fingerprint": plan["fingerprint"],
            "source_plan": digest(plan),
        }:
            raise ValueError("Completed receipt scope differs")
        inventory = read_json(batch / "input-inventory.json")
        texts = {
            row["input_hash"]: row["text_sha256"] for rows in inventory["documents"].values() for row in rows
        }
        saved = set()
        for key, text_sha, dimension, vector, vector_sha in conn.execute("SELECT * FROM inputs"):
            if texts.get(key) != text_sha or hashlib.sha256(vector).hexdigest() != vector_sha:
                raise ValueError("Successful receipt bytes differ from the fixed inputs")
            stored_vector(vector, dimension, dimension)
            saved.add(key)
    if unresolved_issued_inputs(output, saved):
        raise ValueError("Unknown request survives in the completed batch")
    with closing(read_db(Path(plan["data_dir"]))) as conn:
        conn.execute("BEGIN")
        for doc in plan["documents"]:
            chunks = conn.execute(
                "SELECT chunk_id,ordinal,retrieval_text,locator,input_hash FROM retrieval_chunks "
                "WHERE document_id=? ORDER BY ordinal,rowid",
                (doc["id"],),
            ).fetchall()
            current_inputs = [
                {
                    "chunk_id": row["chunk_id"],
                    "ordinal": row["ordinal"],
                    "input_hash": row["input_hash"],
                    "text_sha256": hashlib.sha256(row["retrieval_text"].encode()).hexdigest(),
                    "locator_sha256": digest(json.loads(row["locator"])),
                }
                for row in chunks
            ]
            job = conn.execute(
                "SELECT phase,status,lease_token,lease_until FROM processing_jobs WHERE document_id=?",
                (doc["id"],),
            ).fetchone()
            if (
                not chunks
                or len(chunks) != doc["chunk_count"]
                or current_inputs != inventory["documents"][doc["id"]]
                or job is None
                or tuple(job) != ("embed", "completed", None, None)
                or conn.execute(
                    "SELECT 1 FROM retrieval_chunks c LEFT JOIN retrieval_vectors v ON v.chunk_rowid=c.rowid "
                    "WHERE c.document_id=? AND (v.chunk_rowid IS NULL OR v.fingerprint!=? "
                    "OR v.input_hash!=c.input_hash) LIMIT 1",
                    (doc["id"], plan["fingerprint"]),
                ).fetchone()
            ):
                raise ValueError("Final corpus is not drained and complete")
    return reference(output / "report.json")


def execute(args, state, entry, lock):
    from app.contexts.documents.infrastructure.providers.embedding_admission import (
        process_identity,
    )
    from embedding_supervisor import supervise

    batch = validate_batch(args.handoff_root, entry)
    revalidate(args, batch)
    entry.update(
        status="preflighting",
        pid=os.getpid(),
        start_identity=process_identity(os.getpid()),
    )
    state_path = args.handoff_root / "handoff-state.json"
    persist(state_path, state)
    command = [
        sys.executable,
        str(ROOT / "scripts/complete_embeddings.py"),
        *runner_arguments(args, batch),
    ]
    log = batch / f"preflight-{time.time_ns()}.log"
    child = None
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True
        if child is not None and child.poll() is None:
            child.terminate()

    old = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        with log.open("w") as stream:
            child = subprocess.Popen(
                command,
                stdout=stream,
                stderr=subprocess.STDOUT,
                pass_fds=(lock.fileno(),),
            )
        code = child.wait()
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    entry.update(preflight_exit=code, preflight_log=reference(log))
    if code or stopped:
        entry["status"] = "blocked"
        persist(state_path, state)
        return 130 if stopped else code
    progress = read_json(batch / "output/progress.json")
    config = progress["configuration"]
    expected = {
        "http_workers": 64,
        "document_workers": 12,
        "request_concurrency": 2,
        "batch_size": 2,
        "http_timeout": 60,
        "retry_unresolved": False,
        "retry_unresolved_selection_sha256": None,
    }
    if (
        progress["fixed_documents"] != len(read_json(batch / "selection.json")["documents"])
        or any(config.get(key) != value for key, value in expected.items())
        or progress["provider"]["http_requests"] != 0
        or progress["active_claims"] != 0
    ):
        raise ValueError("Preflight changed the fixed scope or retry authorization")
    entry.update(
        status="preflight_passed",
        preflight=reference(batch / "output" / f"preflight-{progress['run_id']}.json"),
    )
    persist(state_path, state)
    # A resumed, already completed runner needs no new subprocess or provider.
    if (batch / "output/report.json").exists() and (batch / "output/supervisor-state.json").exists():
        try:
            final = completion(batch)
        except ValueError:
            pass
        else:
            entry.update(status="completed", final_report=final)
            persist(state_path, state)
            return 0
    arguments = [
        *runner_arguments(args, batch),
        "--apply",
        "--supervise",
        "--supervisor-window-seconds",
        "300",
        "--supervisor-max-document-workers",
        "12",
    ]
    options = SimpleNamespace(
        output=batch / "output",
        plan=batch / "selection.json",
        supervisor_max_document_workers=12,
        http_workers=64,
        request_concurrency=2,
        retry_unresolved_selection=None,
        supervisor_window_seconds=300,
    )
    entry["status"] = "running"
    entry["paid_launch_intent"] = True
    persist(state_path, state)
    code = supervise(
        options,
        ROOT / "scripts/complete_embeddings.py",
        arguments,
        12,
        inherited_fds=(lock.fileno(),),
    )
    entry["status"] = "blocked"
    if code == 0:
        entry.update(status="completed", final_report=completion(batch))
    entry["exit_code"] = code
    persist(state_path, state)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "ocr-snapshot",
        "handoff-root",
        "exclusions",
        "models-env",
        "admission-path",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Run the mutating preflight and paid supervisor",
    )
    parser.add_argument("--inherited-lock-fd", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--run-batch", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if (args.inherited_lock_fd is None) != (args.run_batch is None) or args.run_batch and not args.apply:
        parser.error("Inherited execution requires a matching batch and --apply")
    for name in (
        "ocr_snapshot",
        "handoff_root",
        "exclusions",
        "models_env",
        "admission_path",
    ):
        setattr(args, name, getattr(args, name).resolve())
    os.umask(0o077)
    args.handoff_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with owned_lock(args.handoff_root / ".handoff.lock", args.inherited_lock_fd) as lock:
            state, entry = prepare(args)
            if entry is None:
                print(
                    json.dumps(
                        {
                            "status": "deferred",
                            "report": str(args.handoff_root / "latest-prepare.json"),
                        }
                    )
                )
                return 0
            if args.run_batch:
                if entry["batch_id"] != args.run_batch:
                    raise ValueError("Child batch differs from durable reservation")
                return execute(args, state, entry, lock)
            print(
                json.dumps(
                    {
                        "status": entry["status"],
                        "batch": str(args.handoff_root / entry["batch_id"]),
                    }
                ),
                flush=True,
            )
            if not args.apply:
                return 0
            # Preflight has not run yet: persist intent without changing the
            # prepared status that permits its first cache initialization.
            entry["launch_intent_at"] = time.time()
            persist(args.handoff_root / "handoff-state.json", state)
            command = [sys.executable, str(Path(__file__).resolve())]
            for name in (
                "ocr_snapshot",
                "handoff_root",
                "exclusions",
                "models_env",
                "admission_path",
            ):
                command.extend(("--" + name.replace("_", "-"), str(getattr(args, name))))
            command.extend(
                (
                    "--apply",
                    "--run-batch",
                    entry["batch_id"],
                    "--inherited-lock-fd",
                    str(lock.fileno()),
                )
            )
            child = subprocess.Popen(command, pass_fds=(lock.fileno(),))
            old = {
                sig: signal.signal(sig, lambda *_: child.terminate() if child.poll() is None else None)
                for sig in (signal.SIGTERM, signal.SIGINT)
            }
            try:
                return child.wait()
            finally:
                for sig, handler in old.items():
                    signal.signal(sig, handler)
    except (ValueError, OSError, KeyError, TypeError, sqlite3.Error) as error:
        print(
            json.dumps(
                {
                    "status": "blocked",
                    "error_type": type(error).__name__,
                    "reason": str(error),
                }
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
