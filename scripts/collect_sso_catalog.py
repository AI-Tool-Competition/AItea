#!/usr/bin/env python3
"""Persist and reconcile the full published SSO catalogues before downloading.

This read-only collector does not publish legislation or call models. A complete
catalogue means every source row was captured exactly once, not legal approval
or acquisition of every original. Resume keeps the original frozen plan.
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.contexts.collection.infrastructure.discovery.discovery import discover_page, recipe_for
from app.shared.infrastructure.http.download import _download

ROOT = Path(__file__).resolve().parents[1]
CATALOGS = {
    "constitutional": ("1.1", "Act", "Current", "Constitutional"),
    "acts-current": ("1.2", "Act", "Current", None),
    "sl-current": ("1.3", "SL", "Current", None),
    "acts-uncommenced": ("1.2", "Act", "Uncommenced", None),
    "acts-repealed": ("1.2", "Act", "Repealed", None),
    "sl-uncommenced": ("1.3", "SL", "Uncommenced", None),
    "sl-revoked": ("1.3", "SL", "Revoked", None),
}


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class CatalogCollector:
    def __init__(self, output, plan, names, *, page_size=100, timeout=35, time_budget=1800):
        self.output, self.plan, self.names = output, plan, names
        self.page_size, self.timeout = page_size, timeout
        self.deadline = time.monotonic() + time_budget
        self.host_gate = threading.BoundedSemaphore(2)
        self.metrics_lock = threading.Lock()
        self.active, self.peak, self.requests = 0, 0, 0
        self.items = {item["id"]: item for item in plan["items"]}

    @contextmanager
    def slot(self, host):
        if host != "sso.agc.gov.sg":
            raise ValueError("SSO catalogue redirected outside its official origin")
        if not self.host_gate.acquire(timeout=max(0, self.deadline - time.monotonic())):
            raise TimeoutError("catalogue budget exhausted")
        try:
            with self.metrics_lock:
                self.active += 1
                self.requests += 1
                self.peak = max(self.peak, self.active)
            yield
        finally:
            with self.metrics_lock:
                self.active -= 1
            self.host_gate.release()

    def seed(self, name):
        _, category, status, subtype = CATALOGS[name]
        values = {"PageSize": self.page_size, "SortBy": "Title", "SortOrder": "ASC"}
        if subtype:
            values["SubType"] = subtype
        return f"https://sso.agc.gov.sg/Browse/{category}/{status}/All?" + urlencode(values)

    def collect(self, name):
        target = self.output / name
        target.mkdir(mode=0o700, exist_ok=True)
        item = self.items[CATALOGS[name][0]]
        recipe = recipe_for(item)
        state = {
            "catalog": name,
            "item_id": item["id"],
            "status": "pending",
            "total": None,
            "pages": 0,
            "rows": 0,
            "next_url": self.seed(name),
            "full_text_acquired": False,
            "current_effect_verified": False,
            "errors": [],
        }
        seen = set()
        page_index = 0
        while state["next_url"] and time.monotonic() < self.deadline:
            url = state["next_url"]
            receipt = target / f"page-{page_index:05d}.json"
            try:
                if receipt.exists():
                    record = json.loads(receipt.read_text())
                    if record["requested_url"] != url:
                        raise ValueError("saved page belongs to a different catalogue continuation")
                    raw = gzip.decompress((target / record["raw_file"]).read_bytes())
                    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
                        raise ValueError("saved catalogue hash mismatch")
                    result = discover_page(
                        record["response"]["url"],
                        raw,
                        record["response"]["mime_type"],
                        recipe,
                    )
                    if result != record["discovery"]:
                        raise ValueError("frozen catalogue replay changed; create a new snapshot")
                else:
                    started = time.monotonic()
                    response = _download(
                        url,
                        deadline=min(self.deadline, started + self.timeout),
                        max_bytes=4 * 1024 * 1024,
                        accept_compressed_html=True,
                        connection_slot=self.slot,
                        required_host="sso.agc.gov.sg",
                    )
                    raw = response.pop("content")
                    result = discover_page(response["url"], raw, response["mime_type"], recipe)
                    if not result["matched"] or result.get("scope", {}).get("provider") != "sso":
                        attempts = target / "failed-attempts"
                        attempts.mkdir(mode=0o700, exist_ok=True)
                        digest = hashlib.sha256(raw).hexdigest()
                        failed_raw = attempts / (digest + ".html.gz")
                        if not failed_raw.exists():
                            fd = os.open(
                                failed_raw,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                0o600,
                            )
                            with os.fdopen(fd, "wb") as handle:
                                handle.write(gzip.compress(raw, mtime=0))
                        write_json(
                            attempts / (digest + ".json"),
                            {
                                "requested_url": url,
                                "response": response,
                                "sha256": digest,
                                "raw_file": failed_raw.name,
                                "bytes": len(raw),
                                "discovery": result,
                                "acquired_at": datetime.now(UTC).isoformat(),
                                "original_or_catalogue_rows_acquired": False,
                            },
                        )
                        raise ValueError(result.get("reason") or "SSO row reconciliation failed")
                    record = {
                        "requested_url": url,
                        "response": response,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "raw_file": f"page-{page_index:05d}.html.gz",
                        "bytes": len(raw),
                        "acquired_at": datetime.now(UTC).isoformat(),
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "discovery": result,
                    }
                    compressed = gzip.compress(raw, mtime=0)
                    path = target / record["raw_file"]
                    if path.exists():
                        # A crash can occur after the original is durable but
                        # before its receipt; validate it before completing it.
                        if path.is_symlink() or gzip.decompress(path.read_bytes()) != raw:
                            raise ValueError("uncommitted page bytes differ; retain both snapshots")
                    else:
                        fd = os.open(
                            path,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                        )
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(compressed)
                            handle.flush()
                            os.fsync(handle.fileno())
                    write_json(receipt, record)
                scope = result["scope"]
                if scope["page_index"] != page_index or (
                    state["total"] is not None and scope["total"] != state["total"]
                ):
                    raise ValueError("source catalogue changed during pagination; snapshot incomplete")
                identities = [row["identity_key"] for row in scope["documents"]]
                if seen.intersection(identities):
                    raise ValueError("same legislation appears on multiple pages; snapshot incomplete")
                seen.update(identities)
                next_links = [row["url"] for row in result["links"] if row["purpose"] == "catalog"]
                state.update(
                    total=scope["total"],
                    pages=page_index + 1,
                    rows=len(seen),
                    next_url=next_links[0] if next_links else None,
                    status="collecting",
                )
                if not state["next_url"]:
                    if len(seen) != scope["total"] or page_index + 1 != scope["pages"]:
                        raise ValueError("terminal catalogue totals do not match captured identities")
                    state["status"] = "catalogue_complete_originals_pending"
                page_index += 1
                write_json(target / "state.json", state)
                print(
                    json.dumps({key: state[key] for key in ("catalog", "status", "pages", "rows", "total")}),
                    flush=True,
                )
            except (ValueError, OSError, KeyError, TypeError, TimeoutError) as exc:
                state["status"] = "incomplete"
                # No transport URLs with query credentials are ever used here.
                state["errors"].append(
                    {
                        "url": url,
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:500],
                    }
                )
                write_json(target / "state.json", state)
                return state
        if state["next_url"]:
            state["status"] = "budget_exhausted_resumable"
            write_json(target / "state.json", state)
        return state

    def run(self):
        with ThreadPoolExecutor(max_workers=min(len(self.names), 4)) as pool:
            states = list(pool.map(self.collect, self.names))
        report = {
            "as_of": datetime.now(UTC).isoformat(),
            "catalogs": states,
            "catalogues_complete": all(x["status"] == "catalogue_complete_originals_pending" for x in states),
            "all_required_content_complete": False,
            "actual_connection_hops": self.requests,
            "per_host_peak": self.peak,
            "source_host": "sso.agc.gov.sg",
            "model_calls": 0,
            "raw_original_downloads": 0,
            "legal_approval_changes": 0,
        }
        write_json(self.output / "report.json", report)
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--catalogs",
        nargs="+",
        choices=CATALOGS,
        default=["constitutional", "acts-current", "sl-current"],
    )
    parser.add_argument("--page-size", type=int, choices=[10, 20, 50, 100], default=100)
    parser.add_argument("--timeout", type=float, default=35)
    parser.add_argument("--time-budget", type=float, default=1800)
    parser.add_argument("--network-env", type=Path)
    parser.add_argument("--ordinary-browser-headers", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.timeout <= 90 or not 1 <= args.time_budget <= 7200:
        parser.error("timeout must be 1–90 and time budget 1–7200 seconds")
    if args.output.is_symlink() or (args.output.exists() and not args.resume):
        parser.error("output must be new, or explicitly resume its frozen snapshot")
    if args.resume and not args.output.is_dir():
        parser.error("resume requires an existing snapshot")
    args.output.mkdir(parents=True, mode=0o700, exist_ok=args.resume)
    if args.network_env:
        for key, value in dotenv_values(args.network_env).items():
            if key.startswith("COLLECTION_") and value:
                os.environ[key] = value
    if args.ordinary_browser_headers:
        os.environ["COLLECTION_BROWSER_HEADERS_HOSTS"] = "sso.agc.gov.sg"
    lock = os.open(args.output / "collector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, "w"):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        snapshot = args.output / "plan.json"
        names = list(dict.fromkeys(args.catalogs))
        if args.resume:
            frozen = json.loads(snapshot.read_text())
            if frozen["catalogs"] != names or frozen["page_size"] != args.page_size:
                parser.error("resume cannot change the frozen catalogue list or page size")
            plan = frozen["plan"]
        else:
            plan = json.loads((ROOT / "config/collection-plans/singapore-demo-20261009.json").read_text())
            write_json(snapshot, {"plan": plan, "catalogs": names, "page_size": args.page_size})
        report = CatalogCollector(
            args.output,
            plan,
            names,
            page_size=args.page_size,
            timeout=args.timeout,
            time_budget=args.time_budget,
        ).run()
        print(
            json.dumps({key: value for key, value in report.items() if key != "catalogs"}),
            flush=True,
        )
        return 0 if report["catalogues_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
