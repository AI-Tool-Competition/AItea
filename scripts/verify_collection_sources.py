#!/usr/bin/env python3
"""Probe every configured source without confusing access with full coverage.

Use the locked backend environment. Evidence is saved in a NEW directory under
data/ or .dev/. This verifier never publishes legal material or writes to an
existing collection database. Bounded probes cannot prove exhaustive collection.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.configuration.paths import InstancePaths  # noqa: E402
from app.configuration.environment import load_environment  # noqa: E402

from app.contexts.ingestion.application.checklists import load_blueprints
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
    MAX_HTML_BYTES,
    discover_page,
    normalized_seeds_for,
    recipe_for,
    request_parameters,
)
from app.contexts.ingestion.infrastructure.source_adapters.source_envelopes import (
    extract_bkpm_multipart_pdf,
)
from app.shared.domain.values import now
from app.shared.infrastructure.persistence.blob_store import BlobStore
from app.shared.infrastructure.http.download import DownloadError, _download

ROOT = Path(__file__).resolve().parents[1]


def write_report(output: Path, report: dict) -> None:
    """Replace only this invocation's private checkpoint after fsync."""
    path = output / "report.json.tmp"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.flush()
        os.fsync(handle.fileno())
    path.replace(output / "report.json")
    directory = os.open(output, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def new_output(path: Path) -> Path:
    path = path.absolute()
    if path.exists() or path.is_symlink():
        raise ValueError("output must be a new directory")
    resolved = path.resolve()
    if not any(resolved.is_relative_to(ROOT / directory) for directory in ("data", ".dev")):
        raise ValueError("output must be below this worktree's data/ or .dev/")
    path.mkdir(parents=True, mode=0o700)
    return path.resolve()


def source_rows(plans: dict, include_conditional: bool) -> list[dict]:
    rows = []
    current_plans = load_blueprints()
    for plan in plans.values():
        directory = InstancePaths.resolve(ROOT).catalogs / "collection-plans"
        if plan["version"] == current_plans[plan["id"]]["version"]:
            definition_path = directory / f"{plan['id']}.json"
        else:
            definition_path = directory / "versions" / f"{plan['id']}.v{plan['version']}.json"
        definition_hash = hashlib.sha256(definition_path.read_bytes()).hexdigest()
        for item in plan["items"]:
            conditional = item["conditional"] and not include_conditional
            rows.append(
                {
                    "plan_id": plan["id"],
                    "plan_version": plan["version"],
                    "plan_sha256": definition_hash,
                    "source_document_sha256": plan["source_document"]["sha256"],
                    "country": plan["country"],
                    "item_id": item["id"],
                    "title": item["title"],
                    "adapter": item["adapter"],
                    "execution": item["execution"],
                    "conditional": item["conditional"],
                    "trigger": item["trigger"],
                    "state": "conditional_not_triggered" if conditional else "pending",
                    "requirements": {
                        key: item[key]
                        for key in (
                            "scope_include",
                            "scope_exclude",
                            "steps",
                            "acceptance",
                            "limits",
                        )
                    },
                    "reference_only_entries": [
                        entry for entry in item["entries"] if entry["purpose"] == "reference"
                    ],
                    "configured_seeds": normalized_seeds_for(item),
                    "recipe": recipe_for(item),
                    "root_probes": [],
                    "follow_probes": [],
                    "originals": [],
                    "coverage": {
                        "root_reachable": None,
                        "scope_discovery_supported": False,
                        "original_acquired": False,
                        "full_required_content_verified": False,
                        "reason": "conditional trigger not confirmed"
                        if conditional
                        else "bounded probes do not prove exhaustive content",
                    },
                }
            )
    return rows


class SourceVerifier:
    def __init__(
        self,
        output: Path,
        *,
        timeout=20,
        time_budget=360,
        workers=8,
        per_host=2,
        max_original_bytes=20 * 1024 * 1024,
    ):
        if not 1 <= workers <= 16 or not 1 <= per_host <= 4 or per_host > workers:
            raise ValueError("workers require 1–16, per-host 1–4 and no more than workers")
        if not 1 <= timeout <= 90 or not 1 <= time_budget <= 1800:
            raise ValueError("timeout requires 1–90 seconds; total budget 1–1800 seconds")
        if not 1 <= max_original_bytes <= 128 * 1024 * 1024:
            raise ValueError("original byte budget requires 1–128 MiB")
        self.output, self.timeout, self.workers = output, timeout, workers
        self.deadline = time.monotonic() + time_budget
        self.per_host, self.hosts, self.lock = per_host, {}, threading.Lock()
        self.max_original_bytes = max_original_bytes
        self.blobs = BlobStore(output / "files", max_bytes=max_original_bytes)

    @contextmanager
    def slot(self, host: str):
        with self.lock:
            semaphore = self.hosts.setdefault(host, threading.BoundedSemaphore(self.per_host))
        if not semaphore.acquire(timeout=max(0, self.deadline - time.monotonic())):
            raise DownloadError("源站并发等待超过验证预算。", retryable=True)
        try:
            yield
        finally:
            semaphore.release()

    def probe(self, row: dict, seed: dict, definition: dict, *, parent: dict | None = None) -> dict:
        started = time.monotonic()
        result = {
            "requested_url": seed["url"],
            "configured_purpose": seed["purpose"],
            "started_at": now(),
        }
        if parent:
            result["discovered_from"] = {
                key: parent[key] for key in ("final_url", "content_hash", "evidence_path")
            }
        if started >= self.deadline:
            return {**result, "status": "budget_exhausted", "elapsed_seconds": 0}
        try:
            request = request_parameters(seed["url"], definition)
            owner = f"source-verification:{row['country']}:{row['item_id']}"
            original = seed["purpose"] == "document"
            fetched = _download(
                seed["url"],
                deadline=min(self.deadline, started + self.timeout),
                consume=lambda chunks: self.blobs.put(owner, chunks),
                connection_slot=self.slot,
                accept_compressed_html=not original,
                max_bytes=self.max_original_bytes
                if original
                else min(MAX_HTML_BYTES, self.max_original_bytes),
                **request,
            )
            blob = fetched["blob"]
            # Full bytes are stored and hashed by the streaming downloader. PDF
            # discovery only needs the signature; HTML/JSON must fit its bound.
            with blob.path.open("rb") as handle:
                prefix = handle.read(8)
                if prefix.startswith(b"%PDF-"):
                    content = prefix
                elif blob.size <= MAX_HTML_BYTES:
                    handle.seek(0)
                    content = handle.read(MAX_HTML_BYTES + 1)
                else:
                    content = b""
            discovery = discover_page(fetched["url"], content, fetched["mime_type"], row["recipe"])
            if original and not discovery.get("matched"):
                # A few BKPM files are complete multipart/form-data entities
                # mislabeled application/pdf. Preserve the raw response and
                # recover only a fully delimited PDF from this exact adapter.
                allowed = discover_page(fetched["url"], b"%PDF-", "application/pdf", row["recipe"])
                if allowed.get("matched") and allowed.get("purpose") == "document":
                    try:
                        recovered = extract_bkpm_multipart_pdf(fetched["url"], content)
                    except ValueError as exc:
                        result["envelope_error"] = str(exc)
                        recovered = None
                    if recovered:
                        pdf, extraction = recovered
                        result["source_response"] = {
                            "cas_path": str(blob.path),
                            "sha256": blob.digest,
                            "size": blob.size,
                            "mime_type": fetched["mime_type"],
                            "extraction": extraction,
                        }
                        blob = self.blobs.put(owner, [pdf])
                        fetched["mime_type"] = "application/pdf"
                        discovery = discover_page(fetched["url"], pdf[:8], "application/pdf", row["recipe"])
            result.update(
                status="downloaded",
                final_url=fetched["url"],
                response_status=fetched.get("response_status"),
                mime_type=fetched["mime_type"],
                filename=fetched["filename"],
                size=blob.size,
                content_hash=blob.digest,
                evidence_path=str(blob.path.relative_to(self.output)),
                request_method=fetched.get("request_method", "POST" if "json_body" in request else "GET"),
                discovery=discovery,
                original_acquired=bool(discovery.get("matched") and discovery.get("purpose") == "document"),
                parser_verification="not_executed_by_source_access_probe",
            )
            if "json_body" in request:
                result["request_body"] = request["json_body"]
        except DownloadError as exc:
            result.update(status="failed", error=str(exc), retryable=exc.retryable)
            if exc.retry_after is not None:
                result["retry_after_seconds"] = exc.retry_after
            if exc.response_host:
                result["response_host"] = exc.response_host
        except Exception as exc:  # noqa: BLE001 -- sanitize every external probe failure at the report boundary
            # Source bodies and provider credentials must not reach report logs.
            result.update(status="failed", error_type=type(exc).__name__, retryable=False)
        result["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return result

    def follow(self, row: dict, definition: dict, limit: int) -> list[dict]:
        """At most two directory/detail hops and a bounded number of requests."""
        if row["execution"] == "dataset_review" or any(
            probe.get("original_acquired") for probe in row["root_probes"]
        ):
            return []
        queue = deque()
        seen = {seed["url"] for seed in row["configured_seeds"]}
        for probe in row["root_probes"]:
            for link in probe.get("discovery", {}).get("links", []):
                queue.append((link, probe, 1))
        results = []
        while queue and len(results) < limit and time.monotonic() < self.deadline:
            # Prefer existing original links over more navigation. No URLs are
            # invented; every follow-up has a stored, replayable parent page.
            document = next((entry for entry in queue if entry[0]["purpose"] == "document"), None)
            if document is not None:
                queue.remove(document)
                seed, parent, depth = document
            else:
                seed, parent, depth = queue.popleft()
            if seed["url"] in seen:
                continue
            seen.add(seed["url"])
            probe = self.probe(row, seed, definition, parent=parent)
            results.append(probe)
            if probe.get("original_acquired"):
                break
            if depth < 2:
                for link in probe.get("discovery", {}).get("links", []):
                    queue.append((link, probe, depth + 1))
        return results


def update_coverage(row: dict) -> None:
    if row["state"] == "conditional_not_triggered":
        return
    probes = row["root_probes"] + row["follow_probes"]
    originals = [probe for probe in probes if probe.get("original_acquired")]
    supported = any(probe.get("discovery", {}).get("matched") for probe in probes)
    reachable = any(probe["status"] == "downloaded" for probe in row["root_probes"])
    row["originals"] = [
        {
            key: probe[key]
            for key in (
                "requested_url",
                "final_url",
                "content_hash",
                "size",
                "mime_type",
                "evidence_path",
                "source_response",
            )
            if key in probe
        }
        for probe in originals
    ]
    row["coverage"].update(
        root_reachable=reachable,
        scope_discovery_supported=supported,
        original_acquired=bool(originals),
    )
    row["state"] = (
        "dataset_manual_review_required"
        if row["execution"] == "dataset_review"
        else "original_sample_acquired"
        if originals
        else "discovery_only"
        if supported
        else "reachable_adapter_or_content_gap"
        if reachable
        else "source_access_failed"
    )


def verify(plans: dict, output: Path, *, include_conditional=False, follow_limit=4, **options) -> dict:
    if not 0 <= follow_limit <= 8:
        raise ValueError("follow request limit must be 0–8 per item")
    rows = source_rows(plans, include_conditional)
    definitions = {(plan["id"], item["id"]): item for plan in plans.values() for item in plan["items"]}
    runner = SourceVerifier(output, **options)
    report = {
        "format_version": 1,
        "status": "running",
        "started_at": now(),
        "all_required_content_verified": False,
        "verification_scope": "configured_roots_and_bounded_original_samples_not_exhaustive_collection",
        "conditional_sources_probed_without_claiming_trigger": include_conditional,
        "limits": {**options, "follow_requests_per_item": follow_limit},
        "items": rows,
    }
    write_report(output, report)
    with ThreadPoolExecutor(max_workers=runner.workers, thread_name_prefix="tea-source-probe") as pool:
        futures = {
            pool.submit(runner.probe, row, seed, definitions[row["plan_id"], row["item_id"]]): row
            for row in rows
            if row["state"] != "conditional_not_triggered"
            for seed in row["configured_seeds"]
        }
        for future in as_completed(futures):
            row = futures[future]
            row["root_probes"].append(future.result())
            update_coverage(row)
            report["updated_at"] = now()
            write_report(output, report)
        futures = {
            pool.submit(
                runner.follow,
                row,
                definitions[row["plan_id"], row["item_id"]],
                follow_limit,
            ): row
            for row in rows
            if row["state"] != "conditional_not_triggered"
        }
        for future in as_completed(futures):
            row = futures[future]
            row["follow_probes"] = future.result()
            update_coverage(row)
            report["updated_at"] = now()
            write_report(output, report)
    report["summary"] = {
        "items": len(rows),
        "active_items": sum(row["state"] != "conditional_not_triggered" for row in rows),
        "roots_probed": sum(len(row["root_probes"]) for row in rows),
        "reachable_items": sum(row["coverage"]["root_reachable"] is True for row in rows),
        "original_sample_items": sum(row["coverage"]["original_acquired"] for row in rows),
        "full_required_content_verified_items": 0,
        "conditional_not_triggered": sum(row["state"] == "conditional_not_triggered" for row in rows),
    }
    report.update(status="finished_with_coverage_gaps", updated_at=now())
    write_report(output, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--plan-id",
        action="append",
        help="Repeat to select plans; default is every checked-in plan",
    )
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--per-host", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--time-budget", type=float, default=360)
    parser.add_argument("--max-original-mib", type=int, default=20)
    parser.add_argument("--follow-limit", type=int, default=4)
    parser.add_argument(
        "--include-conditional",
        action="store_true",
        help="Probe conditional access without asserting a business trigger",
    )
    args = parser.parse_args()
    load_environment(ROOT)
    plans = load_blueprints()
    if args.plan_id:
        unknown = set(args.plan_id) - plans.keys()
        if unknown:
            parser.error("unknown collection plan")
        plans = {key: plans[key] for key in dict.fromkeys(args.plan_id)}
    try:
        output = new_output(args.output)
        report = verify(
            plans,
            output,
            workers=args.workers,
            per_host=args.per_host,
            timeout=args.timeout,
            time_budget=args.time_budget,
            max_original_bytes=args.max_original_mib * 1024 * 1024,
            follow_limit=args.follow_limit,
            include_conditional=args.include_conditional,
        )
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(
        json.dumps(
            {"report": str(output / "report.json"), **report["summary"]},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
