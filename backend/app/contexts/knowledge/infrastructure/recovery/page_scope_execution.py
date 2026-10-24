"""Explicit physical-page prompt recovery with one durable request per page.

No background service starts here. One process coordinates the shared OCR slots;
successful pages are applied in batches of at most twenty. Existing request
intents without responses are never automatically submitted again.
"""

import base64
import fcntl
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from difflib import unified_diff
from pathlib import Path

from app.contexts.knowledge.infrastructure.parsing.ingestion_quality import pdf_page_status, text_complete
from app.contexts.knowledge.infrastructure.recovery.ocr_page_retry import (
    MAX_PAGE_BYTES,
    MAX_RETRY_PAGES,
    prepare_physical_page_retry,
)
from app.contexts.knowledge.infrastructure.recovery.page_checkpoint_recovery import (
    _document,
    apply_page_checkpoints,
    canonical_hash,
    isolated_directory,
)
from app.contexts.knowledge.infrastructure.recovery.page_scope_quality import compare_page
from app.shared.infrastructure.blob_store import BlobStore

PROTOCOL = "physical-page-recovery-execution-v1"
MAX_DOCUMENTS = 32
MAX_PLAN_PAGES = 2000


def _save(path, value, *, exclusive=False):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode()
    target = path if exclusive else path.with_suffix(path.suffix + ".tmp")
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC), 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    if not exclusive:
        target.replace(path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def preflight(service, plan):
    isolated_directory(service.db.data_dir)
    if not isinstance(plan, dict) or plan.get("protocol") != PROTOCOL:
        raise ValueError("page_scope_execution_plan_protocol_invalid")
    owner, entries = plan.get("owner_id"), plan.get("documents")
    if (
        not isinstance(owner, str)
        or not owner
        or len(owner) > 100
        or not isinstance(entries, list)
        or not 1 <= len(entries) <= MAX_DOCUMENTS
    ):
        raise ValueError("page_scope_execution_scope_invalid")
    ids, total, documents = set(), 0, []
    with closing(service.db.connect()) as conn:
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("page_scope_execution_document_invalid")
            identifier, numbers = entry.get("document_id"), entry.get("physical_pages")
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier)
                or identifier in ids
                or not isinstance(numbers, list)
                or not numbers
                or any(type(n) is not int or n < 1 for n in numbers)
                or len(set(numbers)) != len(numbers)
            ):
                raise ValueError("page_scope_execution_unique_scope_required")
            ids.add(identifier)
            total += len(numbers)
            if total > MAX_PLAN_PAGES:
                raise ValueError("page_scope_execution_page_limit")
            row = _document(
                conn,
                owner,
                identifier,
                entry.get("source_hash"),
                entry.get("version"),
                entry.get("expected_manifest_sha256"),
            )
            manifest = json.loads(row["manifest"])
            batches = []
            for offset in range(0, len(numbers), MAX_RETRY_PAGES):
                batch = numbers[offset : offset + MAX_RETRY_PAGES]
                prepared = prepare_physical_page_retry(manifest, batch, source_hash=row["content_hash"])
                batches.append([prepared["pages"][number - 1] for number in batch])
            metadata = conn.execute(
                "SELECT payload FROM document_metadata WHERE document_id=?", (identifier,)
            ).fetchone()[0]
            documents.append({"entry": entry, "row": row, "batches": batches, "metadata": metadata})
    return documents, {
        "protocol": PROTOCOL,
        "mode": "dry_run",
        "documents": len(documents),
        "pages": total,
        "batches": sum(len(item["batches"]) for item in documents),
        "model_calls": 0,
        "database_writes": 0,
        "formal_legal_review": False,
    }


def _read_result(path):
    if not 0 < path.stat().st_size <= MAX_PAGE_BYTES:
        raise ValueError("page_scope_saved_checkpoint_exceeds_limit")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise ValueError("page_scope_saved_checkpoint_invalid")
    return value


def _process_page(document, prepared, ledger, owner, blobs):
    result = {"page": prepared["page"], "new_request_intents": 0, "reused_response": False}
    try:
        return _process_page_impl(document, prepared, ledger, owner, blobs, result)
    except (ValueError, OSError) as exc:
        return None, result | {"status": "refused", "reason": str(exc)[:500]}


def _process_page_impl(document, prepared, ledger, owner, blobs, result):
    from app.contexts.knowledge.infrastructure.parsing.parsing import _ocr_one_page
    from app.contexts.knowledge.infrastructure.recovery.ocr_error_evidence import persist_response_evidence

    number = prepared["page"]
    directory = ledger / str(number)
    saved, intent = directory / "page-after.json", directory / "request-intent.json"
    error_record = directory / "error-response.json"
    before = prepared["ocr_retry_history"][0]["previous_page"]
    if saved.exists():
        after = _read_result(saved)
        history = after.get("ocr_retry_history")
        if not isinstance(history, list) or len(history) != 1 or not isinstance(history[0], dict):
            raise ValueError("page_scope_saved_checkpoint_invalid")
        proof = history[0]
        if proof.get("previous_page_sha256") != canonical_hash(before):
            raise ValueError("page_scope_saved_checkpoint_source_conflict")
        result["reused_response"] = True
    elif intent.exists():
        if error_record.exists():
            result["error_response"] = _read_result(error_record)
        return None, result | {"status": "interrupted_request_requires_review"}
    else:
        _save(directory / "page-before.json", before)

        def on_image(rendered):
            image = base64.b64decode(rendered["image_base64"], validate=True)
            blob = blobs.put_bytes(owner, image)
            return {
                "storage_path": str(blob.path.relative_to(blobs.files_dir.parent)),
                "content_hash": blob.digest,
                "size": blob.size,
                "mime_type": "image/png",
            }

        def on_page(page):
            _save(intent, page, exclusive=True)
            result["new_request_intents"] = 1

        def on_response_error(evidence):
            reference = persist_response_evidence(
                blobs,
                owner,
                evidence,
                source_hash=document["content_hash"],
                request_page=_read_result(intent),
            )
            # Keep the diagnostic discoverable even if the later page checkpoint
            # cannot be written. An existing request intent still forbids retry.
            record = {
                **{key: value for key, value in evidence.items() if key != "response_base64"},
                "blob": reference,
            }
            _save(error_record, record, exclusive=True)
            result["error_response"] = record
            return reference

        # Waiting for a local slot never constitutes another provider attempt.
        for _ in range(24):
            after = _ocr_one_page(
                None,
                prepared,
                on_image=on_image,
                on_page=on_page,
                archive_path=document["storage_path"],
                archive_size=document["size"],
                on_response_error=on_response_error,
            )
            if after.get("ocr", {}).get("attempts", 0) or after.get("deferred_reason") not in {
                "ocr_capacity",
                "render_capacity",
            }:
                break
            time.sleep(0.1)
        if after.get("ocr", {}).get("attempts", 0) == 0 and after.get("deferred_reason") in {
            "ocr_capacity",
            "render_capacity",
        }:
            return None, result | {"status": "local_capacity_unrequested"}
        _save(saved, after, exclusive=True)
    difference = compare_page(before, after)
    result.update(
        difference,
        status=pdf_page_status(after),
        source_hash=after.get("source_hash"),
        image_sha256=after.get("ocr", {}).get("image_sha256"),
        finish_reason=after.get("ocr", {}).get("finish_reason"),
        model_page_status=pdf_page_status(after),
        uncertain_spans=after.get("ocr", {}).get("uncertain_spans", []),
    )
    (directory / "transcript.diff").write_text(
        "\n".join(
            unified_diff(
                before.get("text", "").splitlines(),
                after.get("text", "").splitlines(),
                fromfile="v2",
                tofile="v3",
            )
        )
    )
    if result["status"] != "ocr_complete" or not difference["scope_only_change_accepted"]:
        result["status"] = "manual_review_required"
        _save(directory / "comparison.json", result)
        return None, result
    _save(directory / "comparison.json", result)
    return after, result


def execute(service, plan, output, *, concurrency=64):
    from app.contexts.knowledge.infrastructure.providers.vision import (
        vision_ocr_capacity_state,
        vision_ocr_configured,
    )

    if type(concurrency) is not int or not 1 <= concurrency <= 64 or not vision_ocr_configured():
        raise ValueError("page_scope_live_models_and_1_to_64_workers_required")
    documents, report = preflight(service, plan)
    output = Path(output).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    ledger_root = Path(service.db.data_dir) / ".page-scope-v3"
    ledger_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (ledger_root / "execution.lock").open("a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("page_scope_executor_already_running") from None
        report.update(mode="applied", started_at=time.time(), plan_sha256=canonical_hash(plan), results=[])
        _save(output / "plan.json", plan)
        _save(output / "report.json", report)
        blobs = BlobStore(ledger_root / "files")
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="page-scope-ocr") as ocr_pool:

            def process_document(item):
                entry, row = item["entry"], item["row"]
                expected = entry["expected_manifest_sha256"]
                key = canonical_hash([plan["owner_id"], row["id"], row["version"], row["content_hash"]])
                ledger = ledger_root / key
                result = {"document_id": row["id"], "pages": [], "applied_batches": []}
                for batch in item["batches"]:
                    with closing(service.db.connect()) as conn:
                        _document(
                            conn, plan["owner_id"], row["id"], row["content_hash"], row["version"], expected
                        )
                    futures = [
                        ocr_pool.submit(_process_page, row, page, ledger, plan["owner_id"], blobs)
                        for page in batch
                    ]
                    accepted = []
                    for future in as_completed(futures):
                        page, evidence = future.result()
                        result["pages"].append(evidence)
                        if page is not None:
                            accepted.append(page)
                    _save(ledger / "progress.json", result)
                    if accepted:
                        applied = apply_page_checkpoints(
                            service,
                            plan["owner_id"],
                            row["id"],
                            source_hash=row["content_hash"],
                            version=row["version"],
                            expected_manifest=expected,
                            checkpoints=accepted,
                            checkpoint_dir=ledger_root,
                            checkpoint_owner=plan["owner_id"],
                            apply=True,
                        )
                        result["applied_batches"].append(applied)
                        expected = applied["manifest_sha256"]
                    result["current_manifest_sha256"] = expected
                    _save(ledger / "progress.json", result)
                with closing(service.db.connect()) as conn:
                    current = _document(
                        conn, plan["owner_id"], row["id"], row["content_hash"], row["version"], expected
                    )
                    metadata = conn.execute(
                        "SELECT payload FROM document_metadata WHERE document_id=?", (row["id"],)
                    ).fetchone()[0]
                old, new = json.loads(row["manifest"]), json.loads(current["manifest"])
                selected = set(entry["physical_pages"])
                result.update(
                    text_complete=text_complete(new),
                    metadata_unchanged=metadata == item["metadata"],
                    unselected_pages_unchanged=all(
                        a == b
                        for number, (a, b) in enumerate(zip(old["pages"], new["pages"], strict=True), 1)
                        if number not in selected
                    ),
                )
                _save(ledger / "progress.json", result)
                return result

            with ThreadPoolExecutor(
                max_workers=min(4, len(documents)), thread_name_prefix="page-scope-document"
            ) as pool:
                pending = {pool.submit(process_document, item): item["row"] for item in documents}
                for future in as_completed(pending):
                    try:
                        result = future.result()
                    except (ValueError, OSError) as exc:
                        row = pending[future]
                        key = canonical_hash(
                            [plan["owner_id"], row["id"], row["version"], row["content_hash"]]
                        )
                        progress = ledger_root / key / "progress.json"
                        result = json.loads(progress.read_bytes()) if progress.exists() else {}
                        result.update(
                            {
                                "document_id": row["id"],
                                "status": "refused",
                                "reason": str(exc)[:500],
                            }
                        )
                    report["results"].append(result)
                    _save(output / "report.json", report)
        report.update(
            elapsed_seconds=round(time.time() - report["started_at"], 3), capacity=vision_ocr_capacity_state()
        )
        report.pop("model_calls", None)
        report["new_request_intents"] = sum(
            page["new_request_intents"] for doc in report["results"] for page in doc.get("pages", [])
        )
        report["applied_pages"] = sum(
            len(batch["physical_pages"])
            for doc in report["results"]
            for batch in doc.get("applied_batches", [])
        )
        report["database_writes"] = sum(len(doc.get("applied_batches", [])) for doc in report["results"])
        report["status"] = (
            "selected_pages_complete"
            if report["applied_pages"] == report["pages"]
            and all(
                doc.get("metadata_unchanged") and doc.get("unselected_pages_unchanged")
                for doc in report["results"]
            )
            else "needs_review"
        )
        _save(output / "report.json", report)
        return report
    finally:
        lock.close()
