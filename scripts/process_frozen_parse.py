#!/usr/bin/env python3
"""Process only frozen parse IDs; text completion never claims embedding work."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import ContextVar, copy_context
from copy import deepcopy
from pathlib import Path
from threading import Event, Lock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))
from processing_runtime import retry_local_database  # noqa: E402

SCOPE = ContextVar("frozen_parse_request_scope", default=None)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def verify_successful_pages(initial_pages, current_pages):
    current = {page["page"]: page for page in current_pages}
    if len(current) != len(current_pages):
        raise ValueError("Duplicate physical page checkpoint")
    for prior in initial_pages:
        if (
            prior["validated_status"] in {"native", "ocr_complete"}
            and digest(current.get(prior["page"])) != prior["sha256"]
        ):
            raise ValueError("Previously successful page changed or disappeared")


def frozen_inputs(directory, data_dir):
    report = json.loads((directory / "initial-report.json").read_text())
    raw = (directory / "initial-parse-ids.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != report["initial_id_list_sha256"]:
        raise ValueError("Frozen parse identifier list changed")
    ids = json.loads(raw)
    raw_documents = (directory / "initial-parse-documents.json").read_bytes()
    if hashlib.sha256(raw_documents).hexdigest() != report["initial_documents_sha256"]:
        raise ValueError("Frozen parse source guards changed")
    documents = {item["id"]: item for item in json.loads(raw_documents)}
    if (
        len(ids) != len(set(ids))
        or set(ids) != set(documents)
        or len(ids) != report["initial_parse_queued_documents"]
        or Path(report["source_data_dir"]).resolve() != data_dir.resolve()
        or any(item["owner_id"] != report["owner_id"] for item in documents.values())
    ):
        raise ValueError("Frozen parse ownership or source directory mismatch")
    return report, documents


def read_db(data_dir):
    conn = sqlite3.connect(
        (data_dir / "tea.sqlite3").as_uri() + "?mode=ro", uri=True, timeout=15
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def parse_job_due(row, timestamp):
    return row["phase"] == "parse" and (
        row["job_status"] == "queued"
        and row["available_at"] <= timestamp
        or row["job_status"] == "processing"
        and row["lease_until"] is not None
        and row["lease_until"] <= timestamp
    )


def available_parse_ids(conn, owner_id, timestamp):
    return {
        row["document_id"]
        for row in conn.execute(
            "SELECT document_id FROM processing_jobs WHERE owner_id=? AND phase='parse' "
            "AND ((status='queued' AND available_at<=?) "
            "OR (status='processing' AND lease_until<=?))",
            (owner_id, timestamp, timestamp),
        )
    }


def ambiguous_http_outcome(status):
    return type(status) is int and (status == 408 or 500 <= status <= 599)


def hold_ambiguous_response(error):
    """The batch runner cannot infer that an upstream 408/5xx was unbilled."""
    if ambiguous_http_outcome(error.http_status):
        error.retryable = False
        error.request_outcome_unknown = True
    return error


def hold_previous_ambiguous_page(page):
    """Append a hold checkpoint without altering an older receipt or attempt."""
    metadata = page.get("ocr", {})
    if page.get("status") != "pending_ocr" or not ambiguous_http_outcome(
        metadata.get("http_status")
    ):
        return None
    from app.ocr_error_evidence import preserve_previous_page_attempt

    updated = deepcopy(page)
    preserve_previous_page_attempt(updated)
    updated["ocr"]["request_outcome"] = "unknown_transport_result"
    updated["ocr"]["outcome_hold_reason"] = "previous_ambiguous_http_response"
    updated.update(
        status="ocr_failed",
        error="上次请求返回超时或上游错误，结果与计费未知，需核验后显式补齐。",
    )
    updated.pop("deferred_reason", None)
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--models-env", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument(
        "--document-ids",
        help="At most 20 IDs from the frozen set; otherwise schedule that set fairly",
    )
    parser.add_argument("--ocr-workers", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--ocr-start-workers", type=int, choices=(1, 2, 4))
    parser.add_argument("--document-workers", type=int, choices=range(1, 9), default=2)
    parser.add_argument("--seconds", type=int, choices=range(30, 3601), default=300)
    parser.add_argument("--claim-limit", type=int, choices=range(1, 1001), default=20)
    args = parser.parse_args()
    os.umask(0o077)
    args.data_dir = args.data_dir.resolve(strict=True)
    args.snapshot = args.snapshot.resolve(strict=True)
    report, initial = frozen_inputs(args.snapshot, args.data_dir)
    selected = args.document_ids.split(",") if args.document_ids else list(initial)
    if (
        not selected
        or len(set(selected)) != len(selected)
        or set(selected) - set(initial)
        or args.document_ids
        and len(selected) > 20
        or not args.run_id
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for c in args.run_id
        )
    ):
        raise ValueError("Require unique authorized IDs and a safe run identifier")
    lock_file = (args.snapshot / ".parse-owner.lock").open("a")
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    output = args.snapshot / "runs" / args.run_id
    output.mkdir(parents=True, mode=0o700, exist_ok=False)
    from dotenv import dotenv_values

    for key in list(os.environ):
        if key.startswith(("LLM_", "EMBEDDING_", "RERANK_", "VISION_OCR_")):
            os.environ.pop(key)
    for key, value in dotenv_values(args.models_env, interpolate=False).items():
        if (
            key.startswith(("LLM_", "EMBEDDING_", "RERANK_", "VISION_OCR_"))
            and value is not None
        ):
            os.environ[key] = value
    os.environ.update(
        VISION_OCR_ENABLED="true",
        VISION_OCR_WORKERS=str(args.ocr_workers),
        VISION_OCR_ADAPTIVE="true",
        TEA_PROCESS_WORKERS="32",
        TEA_PARSE_WORKERS="8",
    )
    from app import ingestion, parsing, vision
    from app.ingestion_quality import pdf_page_status, text_complete
    from app.service import Service

    parsing._OCR_SLOTS = parsing._VisionSlots(initial_target=args.ocr_start_workers)

    def forbidden(*_args, **_kwargs):
        raise RuntimeError("Frozen parse runner cannot call embedding or download")

    ingestion.embed_texts = forbidden
    ingestion.download = forbidden
    service = Service(args.data_dir)
    assert vision.vision_ocr_configured()
    service.collection.fetch_item = forbidden
    stop = Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log_lock = Lock()
    counters = Counter()
    touched, claims, completed = set(), {}, []
    original_invoke, original_feedback = vision._invoke, vision._report_provider_result
    started = time.monotonic()
    events = (output / "events.jsonl").open("x", buffering=1)
    cooldown_path = args.snapshot / "provider-cooldown.json"
    cooldown = (
        json.loads(cooldown_path.read_text()).get("retry_not_before", 0)
        if cooldown_path.exists()
        else 0
    )

    def event(kind, **values):
        with log_lock:
            record = {"at": time.time(), "kind": kind, **values}
            events.write(json.dumps(record, ensure_ascii=False) + "\n")
            events.flush()
            os.fsync(events.fileno())

    def feedback(**kwargs):
        nonlocal cooldown
        status = kwargs.get("status_code")
        with log_lock:
            counters[
                f"http_{status}"
                if status
                else "transport_" + str(kwargs.get("timeout"))
            ] += 1
            retry_after = kwargs.get("retry_after")
            if retry_after is not None:
                cooldown = max(cooldown, time.time() + retry_after)
                temporary = cooldown_path.with_suffix(".json.tmp")
                with temporary.open("w") as stream:
                    json.dump({"retry_not_before": cooldown}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(cooldown_path)
        event("provider_result", **(SCOPE.get() or {}), **kwargs)
        return original_feedback(**kwargs)

    def invoke(body):
        with log_lock:
            counters["requests"] += 1
            counters["inflight"] += 1
            counters["peak_inflight"] = max(
                counters["peak_inflight"], counters["inflight"]
            )
        try:
            return original_invoke(body)
        except vision.OCRResponseError as exc:
            hold_ambiguous_response(exc)
            if exc.request_outcome_unknown:
                with log_lock:
                    counters["unknown_paid_outcome"] += 1
            elif exc.http_status is None and not exc.retryable:
                with log_lock:
                    counters["invalid_response"] += 1
            raise
        finally:
            with log_lock:
                counters["inflight"] -= 1

    class ScopedExecutor(ThreadPoolExecutor):
        def submit(self, fn, /, *values, **kwargs):
            context = copy_context()
            return super().submit(context.run, fn, *values, **kwargs)

    original_page = parsing._ocr_one_page

    def page_request(content, page, *values, **kwargs):
        token = SCOPE.set(
            {
                **(SCOPE.get() or {}),
                "page": page["page"],
                "source_hash": page["source_hash"],
            }
        )
        try:
            held = hold_previous_ambiguous_page(page)
            if held is not None:
                event(
                    "prior_http_outcome_held",
                    **SCOPE.get(),
                    request_id=held["ocr"].get("request_id"),
                )
                return held
            return original_page(content, page, *values, **kwargs)
        finally:
            SCOPE.reset(token)

    parsing.ThreadPoolExecutor = ScopedExecutor
    parsing._ocr_one_page = page_request
    vision._invoke, vision._report_provider_result = invoke, feedback
    dispatch_path = args.snapshot / "dispatch-counts.json"
    dispatch = json.loads(dispatch_path.read_text()) if dispatch_path.exists() else {}

    def save(name, value):
        temporary = output / (name + ".tmp")
        with temporary.open("w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output / name)

    def inspect_document(identifier, *, protect=True):
        with closing(read_db(args.data_dir)) as conn:
            row = conn.execute(
                "SELECT d.*,j.phase,j.status job_status,j.available_at,j.lease_token,j.lease_until,m.payload metadata,m.revision metadata_revision FROM documents d JOIN processing_jobs j ON j.document_id=d.id LEFT JOIN document_metadata m ON m.document_id=d.id WHERE d.id=?",
                (identifier,),
            ).fetchone()
        old = initial[identifier]
        for key in (
            "owner_id",
            "version",
            "content_hash",
            "storage_path",
            "size",
            "role",
            "source_url",
            "family_id",
            "case_id",
            "parent_id",
            "title",
            "filename",
            "mime_type",
            "metadata_revision",
        ):
            if row[key] != old[key]:
                raise ValueError("Frozen document source guard changed: " + identifier)
        if (
            hashlib.sha256((row["metadata"] or "").encode()).hexdigest()
            != old["metadata_sha256"]
        ):
            raise ValueError("Frozen document legal metadata changed: " + identifier)
        manifest = json.loads(row["manifest"])
        if protect:
            for key, value in old["protected_provenance"].items():
                if digest(manifest.get(key)) != value:
                    raise ValueError("Stored source provenance changed: " + identifier)
            verify_successful_pages(old["pages"], manifest.get("pages", []))
        return dict(row), manifest

    def job(claim):
        identifier = claim["document_id"]
        token = SCOPE.set(
            {
                "document_id": identifier,
                "document_version": initial[identifier]["version"],
            }
        )
        try:
            service.collection.process_item(claim)
            row, manifest = inspect_document(identifier)
            result = {
                "document_id": identifier,
                "phase": row["phase"],
                "job_status": row["job_status"],
                "text_complete": text_complete(manifest),
                "page_states": dict(
                    Counter(pdf_page_status(page) for page in manifest.get("pages", []))
                ),
                "manifest_sha256": hashlib.sha256(row["manifest"].encode()).hexdigest(),
            }
            event("document_checkpoint", **result)
            return result
        finally:
            SCOPE.reset(token)

    def progress():
        pages = Counter()
        complete = 0
        unaccepted = 0
        with closing(read_db(args.data_dir)) as conn:
            for identifier in touched:
                raw = conn.execute(
                    "SELECT manifest FROM documents WHERE id=?", (identifier,)
                ).fetchone()[0]
                manifest = json.loads(raw)
                complete += text_complete(manifest)
                before_pages = {p["page"]: p for p in initial[identifier]["pages"]}
                unaccepted += sum(
                    bool(page.get("ocr", {}).get("uncertain_spans"))
                    or page.get("status") == "ocr_failed"
                    for page in manifest.get("pages", [])
                    if page.get("ocr", {}).get("attempts", 0)
                    > before_pages.get(page["page"], {}).get("attempts", 0)
                )
                pages.update(
                    pdf_page_status(page) for page in manifest.get("pages", [])
                )
        initial_complete = sum(
            sum(
                page["validated_status"] == "ocr_complete"
                for page in initial[identifier]["pages"]
            )
            for identifier in touched
        )
        with log_lock:
            transport = dict(counters)
        summary = {
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "claimed_documents": len(touched),
            "claims": claim_count,
            "transport": transport,
            "new_complete_documents": complete,
            "new_request_pages_not_accepted": unaccepted,
            "new_ocr_complete_pages": pages["ocr_complete"] - initial_complete,
            "new_ocr_uncertain_pages": pages["ocr_uncertain"]
            - sum(
                page["validated_status"] == "ocr_uncertain"
                for identifier in touched
                for page in initial[identifier]["pages"]
            ),
            "current_page_states_in_touched_documents": dict(pages),
            "capacity": vision.vision_ocr_capacity_state(),
        }
        save("progress.json", summary)
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        return summary

    futures = {}
    last_progress = last_heartbeat = 0.0
    claim_count = 0
    blockers = []

    def blocker(exc, *, document_id=None):
        record = {
            "document_id": document_id,
            "error_type": type(exc).__name__,
            "reason": str(exc),
        }
        event("runner_blocker", **record)
        stop.set()
        blockers.append(record)

    save(
        "run-input.json",
        {
            "pid": os.getpid(),
            "owner_id": report["owner_id"],
            "initial_id_list_sha256": report["initial_id_list_sha256"],
            "selected_ids": selected,
            "ocr_workers": args.ocr_workers,
            "ocr_start_workers": args.ocr_start_workers,
            "document_workers": args.document_workers,
            "claim_limit": args.claim_limit,
            "seconds": args.seconds,
            "models_file_sha256": hashlib.sha256(
                args.models_env.read_bytes()
            ).hexdigest(),
            "embedding_calls_authorized": False,
            "ambiguous_http_outcomes_require_explicit_recovery": True,
        },
    )
    with ThreadPoolExecutor(max_workers=args.document_workers) as executor:
        while True:
            for future in list(futures):
                if future.done():
                    identifier = futures.pop(future)
                    try:
                        completed.append(future.result())
                    except Exception as exc:  # noqa: BLE001 -- drain other owned leases before exiting
                        blocker(exc, document_id=identifier)
            try:
                elapsed = time.monotonic() - started
                if elapsed >= args.seconds or claim_count >= args.claim_limit:
                    stop.set()
                capacity = vision.vision_ocr_capacity_state()
                admission_limit = min(
                    args.document_workers,
                    capacity.get("target") or args.document_workers,
                )
                if (
                    not stop.is_set()
                    and len(futures) < admission_limit
                    and time.time() >= cooldown
                ):
                    with closing(read_db(args.data_dir)) as conn:
                        available = available_parse_ids(
                            conn, report["owner_id"], time.time()
                        )
                    candidates = [
                        identifier
                        for identifier in selected
                        if identifier in available
                        and identifier not in futures.values()
                    ]
                    # Fair rounds prevent long documents from being permanently displaced;
                    # within each round, missing native text and nearly complete PDFs go first.
                    candidates.sort(
                        key=lambda identifier: (
                            dispatch.get(identifier, 0),
                            bool(initial[identifier]["pages"]),
                            len(
                                (initial[identifier]["quality"] or {}).get(
                                    "pending_pages", []
                                )
                            )
                            or 10**8,
                            len(initial[identifier]["pages"]),
                            identifier,
                        )
                    )
                    for identifier in candidates[
                        : min(
                            admission_limit - len(futures),
                            args.claim_limit - claim_count,
                        )
                    ]:
                        row, _ = inspect_document(identifier)
                        if not parse_job_due(row, time.time()):
                            continue
                        claim = retry_local_database(
                            lambda identifier=identifier: (
                                service.collection.claim_processing(
                                    owner_id=report["owner_id"],
                                    document_ids=[identifier],
                                )
                            ),
                            on_wait=lambda **values: event(
                                "claim_storage_wait", **values
                            ),
                        )
                        if claim is None:
                            continue
                        if claim["phase"] != "parse":
                            raise RuntimeError(
                                "Unexpected non-parse claim; frozen scope ownership violated"
                            )
                        dispatch[identifier] = dispatch.get(identifier, 0) + 1
                        temporary = dispatch_path.with_suffix(".json.tmp")
                        with temporary.open("w") as stream:
                            json.dump(dispatch, stream, sort_keys=True)
                            stream.flush()
                            os.fsync(stream.fileno())
                        temporary.replace(dispatch_path)
                        claims[identifier] = claim
                        touched.add(identifier)
                        claim_count += 1
                        event(
                            "claim",
                            document_id=identifier,
                            phase=claim["phase"],
                            lease_token=claim["lease_token"],
                        )
                        futures[executor.submit(job, claim)] = identifier
                    if not futures and not candidates:
                        break
                if futures and time.monotonic() - last_heartbeat >= 5:
                    retry_local_database(
                        lambda: service.collection.heartbeat(
                            [
                                (True, claims[identifier])
                                for identifier in futures.values()
                            ]
                        ),
                        on_wait=lambda **values: event(
                            "heartbeat_storage_wait", **values
                        ),
                    )
                    last_heartbeat = time.monotonic()
                if time.monotonic() - last_progress >= 30:
                    progress()
                    last_progress = time.monotonic()
            except Exception as exc:  # noqa: BLE001 -- stop claims while in-flight work drains
                blocker(exc)
                last_heartbeat = last_progress = time.monotonic()
            if stop.is_set() and not futures:
                break
            time.sleep(0.2)
    temporary = dispatch_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(dispatch, sort_keys=True) + "\n")
    temporary.replace(dispatch_path)
    for identifier in touched:
        row, _ = inspect_document(identifier)
        if row["lease_token"] is not None:
            raise RuntimeError("Owned parse lease did not drain")
    summary = progress()
    save(
        "report.json",
        {
            **summary,
            "status": "blocked" if blockers else "drained",
            "blockers": blockers,
            "document_checkpoints": completed,
            "initial_source_and_metadata_guards_preserved": True,
            "initial_successful_pages_unchanged": True,
            "embedding_requests": 0,
            "download_requests": 0,
            "remaining_scope_is_not_complete": True,
        },
    )
    events.close()
    if blockers:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
