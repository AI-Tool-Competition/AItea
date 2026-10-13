#!/usr/bin/env python3
"""Bounded ingestion verification in a NEW, isolated demonstration data directory.

Run with the project's locked environment, for example:
  uv run --locked --project backend python scripts/verify_ingestion.py \
      --plan /path/plan.json --output .dev/quality/run-unique --time-budget 300

The plan contains country, samples [{id,path|url,source_url,level,
primary_sample_id}], and queries [{text,expected_sample_ids}]. Real providers
require BOTH --live-models and --models-env. Credentials are never persisted.
This is an engineering check, not a legal review or a production import.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import multiprocessing
import os
import re
import signal
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_PREFIXES = ("LLM_", "EMBEDDING_", "RERANK_", "VISION_OCR_")
TERMINAL = {"completed", "failed", "cancelled"}


def read_plan(path):
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("plan exceeds 1 MiB")
    plan = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(plan, dict):
        raise TypeError("plan must be an object")
    samples, queries = plan.get("samples"), plan.get("queries", [])
    if not isinstance(samples, list) or not 1 <= len(samples) <= 20:
        raise ValueError("plan requires 1–20 samples")
    if not isinstance(queries, list) or len(queries) > 10:
        raise ValueError("plan accepts at most 10 queries")
    country = plan.get("country", "ID")
    if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("country must be two uppercase letters")
    ids = set()
    for sample in samples:
        if not isinstance(sample, dict) or not isinstance(sample.get("id"), str):
            raise TypeError("each sample requires an id")
        identifier = sample["id"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", identifier) or identifier in ids:
            raise ValueError("sample ids must be unique short identifiers")
        ids.add(identifier)
        if bool(sample.get("path")) == bool(sample.get("url")):
            raise ValueError("each sample requires exactly one path or url")
        if sample.get("level", "primary") not in {"primary", "secondary"}:
            raise ValueError("sample level must be primary or secondary")
        sample["level"] = sample.get("level", "primary")
        if sample.get("path"):
            source = Path(sample["path"]).expanduser()
            source = (path.parent / source).resolve() if not source.is_absolute() else source.resolve()
            if not source.is_file() or not 0 < source.stat().st_size <= 20 * 1024 * 1024:
                raise ValueError("sample paths must be nonempty regular files at most 20 MiB")
            sample["path"] = str(source)
        filename = sample.get("filename")
        if filename is not None and (
            not isinstance(filename, str) or not 1 <= len(filename) <= 240
            or re.search(r"[/\\\x00-\x1f\x7f]", filename) or filename in {".", ".."}
        ):
            raise ValueError("sample filenames must be plain short display names")
        for key in ("url", "source_url"):
            value = sample.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 4096):
                raise ValueError("sample URLs must be short strings")
    primary_ids = {sample["id"] for sample in samples if sample["level"] == "primary"}
    for sample in samples:
        if sample["level"] == "secondary" and sample.get("primary_sample_id") not in primary_ids:
            raise ValueError("secondary samples must refer to a declared primary sample")
        if sample["level"] == "primary" and sample.get("primary_sample_id"):
            raise ValueError("primary samples cannot declare a primary_sample_id")
    for query in queries:
        if (
            not isinstance(query, dict)
            or not isinstance(query.get("text"), str)
            or not 1 <= len(query["text"]) <= 4000
        ):
            raise ValueError("queries require text of 1–4000 characters")
        expected = query.get("expected_sample_ids", [])
        if not isinstance(expected, list) or any(
            not isinstance(item, str) or item not in ids for item in expected
        ):
            raise ValueError("expected_sample_ids must reference declared samples")
    return {"country": country, "samples": samples, "queries": queries}


def write_report(output, report, filename="report.json"):
    temporary = output / (filename + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        os.chmod(temporary, 0o600)
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(output / filename)
    if hasattr(os, "O_DIRECTORY"):
        directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def blocks(path):
    with Path(path).open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            yield chunk


def configure_models(models_env):
    for key in list(os.environ):
        if key.startswith(PROVIDER_PREFIXES):
            os.environ.pop(key)
    if models_env:
        from dotenv import dotenv_values

        values = dotenv_values(models_env, interpolate=False)
        for key, value in values.items():
            if key.startswith(PROVIDER_PREFIXES) and value is not None:
                os.environ[key] = value


def run_verification(plan, output, models_env):
    sys.path.insert(0, str(ROOT / "backend"))
    configure_models(models_env)
    from app.archive_inventory import export_inventory
    from app.catalog import MetadataPatch
    from app.collection import CollectionWorkers
    from app.db import now, uid
    from app.ingestion_quality import text_complete
    from app.service import Service

    started = time.monotonic()
    report = {
        "status": "running",
        "legal_review": "isolated_demo_only_not_legal_approval",
        "country": plan["country"],
        "live_models": bool(models_env),
        "samples": [],
        "queries": [],
        "gates": {},
        "started_at": now(),
    }
    service = Service(output)
    owner = uid()
    with service.db.transaction() as conn:
        conn.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?)",
            (owner, "Ingestion verification", None, None, 1, now()),
        )
    collection = f"country:{plan['country']}"
    states = {
        sample["id"]: {
            "id": sample["id"],
            "level": sample["level"],
            "source_kind": "live_download" if sample.get("url") else "previously_saved_original",
            "source_url": sample.get("url") or sample.get("source_url"),
        }
        for sample in plan["samples"]
    }
    report["samples"] = list(states.values())
    workers = CollectionWorkers(service.collection).start()

    def checkpoint():
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        write_report(output, report)

    def queue(sample):
        state = states[sample["id"]]
        state["queued_seconds"] = round(time.monotonic() - started, 3)
        anchors = []
        if sample["level"] == "secondary":
            anchor = states[sample["primary_sample_id"]].get("document_id")
            if not anchor:
                state.update(status="failed", error="Primary original could not be stored")
                return
            anchors = [anchor]
        state["primary_document_ids"] = anchors
        if sample.get("path"):
            path = Path(sample["path"])
            filename = sample.get("filename") or path.name
            blob = service.archive.blobs.put(owner, blocks(path))
            with service.db.transaction() as conn:
                document = service.archive.store_original(
                    conn,
                    owner,
                    blob,
                    filename,
                    mimetypes.guess_type(filename)[0] or "application/octet-stream",
                    sample.get("source_url"),
                    collection=collection,
                    level=sample["level"],
                    tags=["verification"],
                    identity_key=f"verification:{sample['id']}",
                    primary_document_ids=anchors,
                )
            state["document_id"] = document["id"]
            state["stored_seconds"] = round(time.monotonic() - started, 3)
        else:
            source = service.save_source(
                owner,
                {
                    "name": f"Verification {sample['id']}",
                    "url": sample["url"],
                    "enabled": False,
                    "interval_hours": 24,
                    "tags": ["verification"],
                    "plan": {
                        "collection": collection,
                        "level": sample["level"],
                        "primary_document_ids": anchors,
                        "discover_attachments": False,
                        "max_items": 1,
                    },
                },
            )
            payload = {
                "client_request_id": f"verification:{sample['id']}",
                "entries": [
                    {
                        "url": sample["url"],
                        "identity_key": f"verification:{sample['id']}",
                    }
                ],
            }
            run = service.collection.enqueue(owner, source["id"], payload)
            state.update(source_id=source["id"], run_id=run["id"], request=payload)
        checkpoint()

    def observe(originals_only=False, selected=None):
        pending = False
        with closing(service.db.connect()) as conn:
            for state in selected or states.values():
                if state.get("status") == "failed" and not state.get("document_id"):
                    continue
                if state.get("run_id") and not state.get("document_id"):
                    item = conn.execute(
                        "SELECT status,document_id,error FROM collection_items WHERE run_id=? ORDER BY created_at LIMIT 1",
                        (state["run_id"],),
                    ).fetchone()
                    if item and item["document_id"]:
                        state["document_id"] = item["document_id"]
                        state["stored_seconds"] = round(time.monotonic() - started, 3)
                    elif item and item["status"] in {"failed", "cancelled"}:
                        state.update(status="failed", error=item["error"])
                        continue
                if not state.get("document_id"):
                    pending = True
                    continue
                if originals_only:
                    continue
                job = conn.execute(
                    "SELECT status,error FROM processing_jobs WHERE document_id=?",
                    (state["document_id"],),
                ).fetchone()
                if job and job["status"] not in TERMINAL:
                    pending = True
                elif "completed_seconds" not in state:
                    state["completed_seconds"] = round(time.monotonic() - started, 3)
                    state["job_status"] = job["status"] if job else None
                    state["job_error"] = job["error"] if job else None
        checkpoint()
        return pending

    try:
        for sample in plan["samples"]:
            if sample["level"] == "primary":
                queue(sample)
        primaries = [state for state in states.values() if state["level"] == "primary"]
        while observe(originals_only=True, selected=primaries):
            time.sleep(0.2)
        for sample in plan["samples"]:
            if sample["level"] == "secondary":
                queue(sample)
        while observe():
            time.sleep(0.2)
    finally:
        workers.stop()

    probe = plan["queries"][0]["text"] if plan["queries"] else "tea"
    draft = service.search(owner, probe, country=plan["country"])
    report["gates"]["draft_excluded"] = not draft["items"]
    for sample in plan["samples"]:
        state = states[sample["id"]]
        identifier = state.get("document_id")
        if not identifier:
            continue
        with closing(service.db.connect()) as conn:
            document = dict(service.own_document(conn, owner, identifier))
            manifest = json.loads(document["manifest"])
            vectors = conn.execute(
                "SELECT COUNT(*) FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id=?",
                (identifier,),
            ).fetchone()[0]
            current_vectors = conn.execute(
                """SELECT COUNT(*) FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid
                WHERE c.document_id=? AND v.fingerprint=? AND v.input_hash=c.input_hash""",
                (identifier, service.embedding_fingerprint()),
            ).fetchone()[0]
            relation_targets = conn.execute(
                "SELECT source_document_id FROM legal_relations WHERE target_document_id=? AND owner_id=? AND collection=?",
                (identifier, owner, collection),
            ).fetchall()
        digest = hashlib.sha256()
        for chunk in blocks(document["storage_path"]):
            digest.update(chunk)
        images, image_errors = [], []
        for page in manifest.get("pages", []):
            ocr = page.get("ocr") or {}
            image = ocr.get("image_blob")
            if not image:
                if page.get("status") == "ocr_complete":
                    image_errors.append(page.get("page"))
                continue
            path = (output / image["storage_path"]).resolve()
            image_hash = hashlib.sha256()
            if path.is_relative_to(output) and path.is_file():
                for chunk in blocks(path):
                    image_hash.update(chunk)
            valid = (
                image_hash.hexdigest() == image.get("content_hash") == ocr.get("image_sha256")
                and page.get("source_hash") == document["content_hash"]
                and path.stat().st_size == image.get("size")
            )
            if not valid:
                image_errors.append(page.get("page"))
            images.append(
                {"page": page.get("page"), "content_hash": image.get("content_hash"), "verified": valid}
            )
        state.update(
            status=document["status"],
            filename=document["filename"],
            error=document["error"],
            content_hash=document["content_hash"],
            hash_verified=digest.hexdigest() == document["content_hash"],
            size=document["size"],
            version=document["version"],
            pages=len(manifest.get("pages", [])),
            quality=manifest.get("quality"),
            complete=text_complete(manifest),
            chunks=len(manifest.get("chunks", [])),
            vectors=vectors,
            current_vectors=current_vectors,
            ocr_images=images,
            ocr_images_verified=not image_errors,
            relation_verified=sample["level"] == "primary"
            or set(state["primary_document_ids"]) <= {row["source_document_id"] for row in relation_targets},
            processing_seconds=round(
                state.get("completed_seconds", time.monotonic() - started) - state["queued_seconds"],
                3,
            ),
        )
        metadata = service.catalog.metadata_for(identifier)
        state["initial_review_status"] = metadata["metadata"]["review_status"]
        service.catalog.patch_document(
            owner,
            identifier,
            MetadataPatch(
                expected_revision=metadata["metadata_revision"],
                kind="demo",
                review_status="published",
            ),
        )
        # Identical bytes use the real CAS and archive deduplication path again.
        blob = service.archive.blobs.put(owner, blocks(document["storage_path"]))
        with service.db.transaction() as conn:
            duplicate = service.archive.store_original(
                conn,
                owner,
                blob,
                document["filename"],
                document["mime_type"],
                document["source_url"],
                collection=collection,
                level=sample["level"],
                tags=["verification"],
                identity_key=f"verification:{sample['id']}",
                primary_document_ids=state["primary_document_ids"],
            )
        state["duplicate_same_document"] = (
            duplicate["id"] == identifier
            and duplicate["version"] == document["version"]
            and str(blob.path) == document["storage_path"]
        )
        if state.get("run_id"):
            duplicate_run = service.collection.enqueue(owner, state["source_id"], state.pop("request"))
            state["request_idempotent"] = duplicate_run["id"] == state["run_id"]
    # These approvals apply solely to the newly created is_demo account above.
    report["gates"]["draft_excluded"] &= all(
        state.get("initial_review_status") == "draft" for state in states.values() if state.get("document_id")
    )
    secondary_ids = [
        state["document_id"]
        for state in states.values()
        if state["level"] == "secondary" and state.get("document_id")
    ]
    report["gates"]["secondary_excluded"] = all(
        not service.search(owner, probe, country=plan["country"], document_ids=[identifier])["items"]
        for identifier in secondary_ids
    )
    report["gates"]["secondary_without_vectors"] = all(
        state.get("vectors", 0) == 0 for state in states.values() if state["level"] == "secondary"
    )
    report["gates"]["primary_vectors_complete"] = not models_env or all(
        state.get("chunks", 0) > 0 and state.get("current_vectors") == state.get("chunks")
        for state in states.values()
        if state["level"] == "primary"
    )
    report["gates"]["ocr_images_verified"] = all(
        state.get("ocr_images_verified") for state in states.values()
    )
    report["gates"]["relations_verified"] = all(state.get("relation_verified") for state in states.values())
    report["gates"]["requests_idempotent"] = all(
        state.get("request_idempotent") for state in states.values() if state.get("run_id")
    )
    wrong_country = "SG" if plan["country"] != "SG" else "ID"
    report["gates"]["country_excluded"] = not service.search(owner, probe, country=wrong_country)["items"]
    by_document = {state["document_id"]: state["id"] for state in states.values() if state.get("document_id")}

    def query_result(instance, query):
        result = instance.search(owner, query["text"], country=plan["country"], limit=5)
        return {
            "sample_ids": list(dict.fromkeys(by_document[hit["document_id"]] for hit in result["items"])),
            "mode": result["mode"],
            "warning": result.get("warning"),
            "hits": [
                {
                    key: hit.get(key)
                    for key in (
                        "document_id",
                        "chunk_id",
                        "chunk_hash",
                        "document_version",
                        "locator",
                    )
                }
                for hit in result["items"]
            ],
        }

    for query in plan["queries"]:
        query_started = time.monotonic()
        result = query_result(service, query)
        report["queries"].append(
            {
                "text": query["text"],
                **result,
                "expected_sample_ids": query.get("expected_sample_ids", []),
                "expected_recalled": set(query.get("expected_sample_ids", [])) <= set(result["sample_ids"]),
                "elapsed_seconds": round(time.monotonic() - query_started, 3),
            }
        )
        checkpoint()
    restarted = Service(output)
    for query, result in zip(plan["queries"], report["queries"], strict=True):
        again = query_result(restarted, query)
        result["restart_consistent"] = set(again["sample_ids"]) == set(result["sample_ids"])
        result["restart_expected_recalled"] = set(query.get("expected_sample_ids", [])) <= set(
            again["sample_ids"]
        )
    inventory = export_inventory(output, owner=owner)
    report["inventory"] = {
        "path": str(inventory["path"]),
        "summary": inventory["summary"],
    }
    passed = (
        all(report["gates"].values())
        and all(
            state.get("status") == "ready"
            and state.get("complete")
            and state.get("hash_verified")
            and state.get("duplicate_same_document")
            and state.get("job_status") == "completed"
            for state in states.values()
        )
        and all(
            result["expected_recalled"]
            and (not models_env or result["mode"] == "hybrid")
            and result["restart_consistent"]
            and result["restart_expected_recalled"]
            for result in report["queries"]
        )
    )
    report["status"] = "passed" if passed else "failed"
    checkpoint()
    return passed


def child(plan, output, models_env):
    # Isolate only our own process group, including bounded parser subprocesses.
    if hasattr(os, "setsid"):
        os.setsid()
    os.umask(0o077)
    with (output / "worker.log").open("w", encoding="utf-8", buffering=1) as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        try:
            passed = run_verification(plan, output, models_env)
        except BaseException as exc:  # noqa: BLE001 — report interrupted verification in the child only.
            report_path = output / "report.json"
            report = json.loads(report_path.read_text()) if report_path.exists() else {}
            # Exception classes are useful without leaking provider error bodies/keys.
            report.update(status="failed", error_type=type(exc).__name__)
            write_report(output, report)
            raise SystemExit(1) from None
    raise SystemExit(0 if passed else 1)


def terminate_owned(process):
    if not process.is_alive():
        return
    try:
        if hasattr(os, "killpg") and os.getpgid(process.pid) == process.pid:
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    process.join(1)
    if process.is_alive():
        try:
            if hasattr(os, "killpg") and os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.join(1)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--time-budget",
        type=int,
        default=300,
        choices=range(10, 901),
        metavar="10..900",
    )
    parser.add_argument("--live-models", action="store_true")
    parser.add_argument("--models-env", type=Path)
    args = parser.parse_args()
    try:
        if args.live_models != bool(args.models_env):
            raise ValueError("real providers require both --live-models and --models-env")
        if args.models_env and not args.models_env.is_file():
            raise ValueError("models-env must be an existing file")
        plan = read_plan(args.plan.resolve())
        if args.output.is_symlink() or (
            args.output.exists() and (not args.output.is_dir() or any(args.output.iterdir()))
        ):
            raise ValueError("output must be a new or empty independent directory")
        args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(args.output, 0o700)
        output = args.output.resolve()
    except (ValueError, OSError, TypeError) as exc:
        parser.error(str(exc))
    process = multiprocessing.get_context("spawn").Process(
        target=child,
        args=(
            plan,
            output,
            str(args.models_env.resolve()) if args.models_env else None,
        ),
    )
    started = time.monotonic()
    process.start()
    try:
        process.join(args.time_budget - 3)
        if process.is_alive():
            terminate_owned(process)
            path = output / "report.json"
            report = json.loads(path.read_text()) if path.exists() else {}
            report.update(status="timeout", elapsed_seconds=round(time.monotonic() - started, 3))
            write_report(output, report)
            return 1
    except KeyboardInterrupt:
        terminate_owned(process)
        return 130
    print(f"Verification {'passed' if process.exitcode == 0 else 'failed'}: {output / 'report.json'}")
    return process.exitcode or 0


if __name__ == "__main__":
    raise SystemExit(main())
