#!/usr/bin/env python3
"""Resume the public legislative gazette directory and bounded original files.

The immutable source ledger preserves publication/revision dates. Gazette files
never resolve the independent gap in current consolidated SSO legislation.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.blob_store import Blob, BlobStore
from app.egazette import (
    LEGISLATIVE_CATEGORIES,
    MAX_SOURCE_BYTES,
    SOURCE_ROLE,
    inspect_search_response,
    original_url,
    publication_classification,
    public_search_from_html,
    search_body,
)
from app.ingestion import DownloadError, _download
from app.public_search import EGAZETTE_ORIGIN, EGAZETTE_SEARCH_HOST, EGAZETTE_SEARCH_URL
from collect_sso_catalog import write_json

ROOT = Path(__file__).resolve().parents[1]
MAX_ORIGINAL_BUDGET = 10 * 1024**3
MIN_FREE_DISK_BYTES = 40 * 1024**3
SOURCE_OWNER = "egazette-legislative-archive"
SOURCE_HOSTS = frozenset({EGAZETTE_SEARCH_HOST, "assets.egazette.gov.sg"})


class HostCooling(DownloadError):
    def __init__(self, host, delay):
        super().__init__(
            "official host is cooling down; no request was sent",
            retryable=True,
            retry_after=delay,
            response_host=host,
        )


def save_bytes(path, raw):
    if path.exists():
        if path.is_symlink() or path.read_bytes() != raw:
            raise ValueError("existing evidence differs; create a new source snapshot")
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def load_json(path, limit=8 * 1024 * 1024):
    if path.is_symlink() or path.stat().st_size > limit:
        raise ValueError("saved evidence path or size is invalid")
    return json.loads(path.read_text())


class GazetteCollector:
    def __init__(
        self,
        output,
        request,
        *,
        timeout=35,
        time_budget=1800,
        page_size=100,
        min_free_bytes=MIN_FREE_DISK_BYTES,
    ):
        self.output, self.request = output, request
        self.timeout, self.page_size = timeout, page_size
        self.min_free_bytes = min_free_bytes
        self.deadline = time.monotonic() + time_budget
        self.lock = threading.Lock()
        self.gates, self.active, self.peak, self.hops = {}, {}, {}, 0
        path = output / "network-state.json"
        self.cooldowns = load_json(path, limit=4096) if path.exists() else {}
        if not isinstance(self.cooldowns, dict) or any(
            host not in SOURCE_HOSTS
            or type(until) not in (int, float)
            or not math.isfinite(until)
            or until < 0
            for host, until in self.cooldowns.items()
        ):
            raise ValueError("saved official host cooldown state is invalid")

    def check_cooldown(self, host):
        with self.lock:
            delay = self.cooldowns.get(host, 0) - time.time()
        if delay > 0:
            raise HostCooling(host, delay)

    def fetch(self, url, **kwargs):
        self.check_cooldown(urlsplit(url).hostname)
        try:
            return _download(url, **kwargs)
        except DownloadError as exc:
            if isinstance(exc, HostCooling):
                raise
            delay = exc.retry_after
            if delay is None and exc.retryable and "HTTP 429" in str(exc):
                delay = 60.0
            if exc.response_host in SOURCE_HOSTS and delay is not None:
                with self.lock:
                    self.cooldowns[exc.response_host] = max(
                        self.cooldowns.get(exc.response_host, 0), time.time() + delay
                    )
                    write_json(self.output / "network-state.json", self.cooldowns)
            raise

    @contextmanager
    def slot(self, host):
        if host not in SOURCE_HOSTS:
            raise ValueError("eGazette collection cannot leave its verified origins")
        self.check_cooldown(host)
        with self.lock:
            gate = self.gates.setdefault(host, threading.BoundedSemaphore(2))
        if not gate.acquire(timeout=max(0, self.deadline - time.monotonic())):
            raise TimeoutError("campaign time budget exhausted")
        counted = False
        try:
            with self.lock:
                delay = self.cooldowns.get(host, 0) - time.time()
                if delay > 0:
                    raise HostCooling(host, delay)
                self.active[host] = self.active.get(host, 0) + 1
                self.peak[host] = max(self.peak.get(host, 0), self.active[host])
                self.hops += 1
                counted = True
            yield
        finally:
            if counted:
                with self.lock:
                    self.active[host] -= 1
            gate.release()

    def page(self, target, filters, page):
        body = search_body(page=page, page_size=self.page_size, filters=filters)
        receipt = target / f"page-{page:05d}.json"
        raw_path = target / f"page-{page:05d}.raw.json"
        if receipt.exists():
            record = load_json(receipt)
            if record["request_body"] != body or record["request_url"] != EGAZETTE_SEARCH_URL:
                raise ValueError("saved search belongs to another frozen partition")
            if raw_path.is_symlink() or raw_path.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError("saved search response is invalid")
            raw = raw_path.read_bytes()
            if sha256(raw).hexdigest() != record["sha256"]:
                raise ValueError("saved search response hash mismatch")
            parsed = inspect_search_response(raw, body)
            if parsed != record["discovery"]:
                raise ValueError("saved discovery changed on replay")
            return parsed
        response = self.fetch(
            EGAZETTE_SEARCH_URL,
            deadline=min(self.deadline, time.monotonic() + self.timeout),
            required_host=EGAZETTE_SEARCH_HOST,
            json_body=body,
            public_search=self.request,
            max_bytes=MAX_SOURCE_BYTES,
            accept_compressed_json=True,
            connection_slot=self.slot,
        )
        raw = response.pop("content")
        save_bytes(raw_path, raw)
        parsed = inspect_search_response(raw, body)
        write_json(
            receipt,
            {
                "request_url": EGAZETTE_SEARCH_URL,
                "request_body": body,
                "configuration_source_sha256": self.request.source_sha256,
                "response": response,
                "sha256": sha256(raw).hexdigest(),
                "raw_file": raw_path.name,
                "acquired_at": datetime.now(UTC).isoformat(),
                "discovery": parsed,
            },
        )
        return parsed

    def partition(self, category, year, lower=None, upper=None, depth=0):
        slug = category.lower().replace(" ", "-")
        suffix = f"-{lower}-{upper}" if lower is not None else ""
        key = f"{year}-{slug}{suffix}"
        target = self.output / "catalog" / key
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        filters = f'category:"Legislative Supplements" AND subCategory:{json.dumps(category)} AND publishYear:{year}'
        if lower is not None:
            filters += f" AND publishTimestamp >= {lower} AND publishTimestamp <= {upper}"
        state = {
            "partition": key,
            "category": category,
            "year": year,
            "filters": filters,
            "status": "pending",
            "captured_records": 0,
            "source_records": None,
            "source_role": SOURCE_ROLE,
            "current_effect_verified": False,
            "pages": 0,
            "validated_pages": [],
            "publication_interval": [lower, upper] if lower is not None else None,
            "errors": [],
        }
        seen = set()
        page = 0
        try:
            while time.monotonic() < self.deadline:
                parsed = self.page(target, filters, page)
                count = parsed["source_index_records"]
                if state["source_records"] is not None and state["source_records"] != count:
                    raise ValueError("source count changed during pagination; snapshot incomplete")
                state["source_records"] = count
                state["exhaustive_source_count"] = parsed["exhaustive_source_count"]
                identities = {row["objectID"] for row in parsed["rows"]}
                if seen.intersection(identities):
                    raise ValueError("source identities overlap across pages; snapshot incomplete")
                self.check_partition_rows(parsed["rows"], state)
                seen.update(identities)
                state["validated_pages"].append(page)
                state.update(captured_records=len(seen), pages=page + 1, status="collecting")
                if not parsed["pagination_window_complete"]:
                    # Publication metadata is day-based. Retain the unresolved
                    # window instead of retrying dozens of millisecond filters
                    # that cannot separate a single gazette publication date.
                    if depth >= 10 or (lower is not None and upper - lower < 86_400_000):
                        raise ValueError("source search window cannot enumerate this publication day")
                    if lower is None:
                        sg = timezone(timedelta(hours=8))
                        lower = int(datetime(year, 1, 1, tzinfo=sg).timestamp() * 1000)
                        upper = int(datetime(year + 1, 1, 1, tzinfo=sg).timestamp() * 1000) - 1
                    midpoint = (lower + upper) // 2
                    children = [
                        self.partition(category, year, lo, hi, depth + 1)
                        for lo, hi in ((lower, midpoint), (midpoint + 1, upper))
                    ]
                    state["children"] = children
                    captured = sum(child["captured_records"] for child in children)
                    state["captured_records"] = captured
                    state["status"] = (
                        "catalogue_complete"
                        if all(child["status"] == "catalogue_complete" for child in children)
                        and captured == count
                        and parsed["exhaustive_source_count"]
                        else "incomplete_split"
                    )
                    break
                if parsed["next_page"] is None:
                    if len(seen) != count:
                        raise ValueError("terminal source count disagrees with captured identities")
                    state["status"] = (
                        "catalogue_complete"
                        if parsed["exhaustive_source_count"]
                        else "catalogue_count_unconfirmed"
                    )
                    break
                page = parsed["next_page"]
                write_json(target / "state.json", state)
            else:
                state["status"] = "budget_exhausted_resumable"
        except (ValueError, OSError, TypeError, KeyError, TimeoutError) as exc:
            state["status"] = "incomplete"
            state["errors"].append({"type": type(exc).__name__, "message": str(exc)[:500], "page": page})
        write_json(target / "state.json", state)
        print(
            json.dumps(
                {
                    k: state[k]
                    for k in (
                        "partition",
                        "status",
                        "captured_records",
                        "source_records",
                    )
                }
            ),
            flush=True,
        )
        return state

    def catalog(self, categories, years):
        # Newest revised editions first, then all declared older publications.
        tasks = [(category, year) for category in categories for year in sorted(years, reverse=True)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            states = list(pool.map(lambda task: self.partition(*task), tasks))
        return states

    @staticmethod
    def check_partition_rows(rows, state):
        for row in rows:
            if (
                row["category"] != "Legislative Supplements"
                or row["subCategory"] != state["category"]
                or row["publishYear"] != state["year"]
            ):
                raise ValueError("returned record escaped its frozen category/year filters")
            interval = state.get("publication_interval")
            if interval and not interval[0] <= row["publishTimestamp"] <= interval[1]:
                raise ValueError("returned record escaped its frozen publication interval")

    def rows(self):
        documents = {}
        for path in sorted((self.output / "catalog").glob("*/page-*.json")):
            if path.name.endswith(".raw.json"):
                continue
            state = load_json(path.parent / "state.json")
            page = int(path.stem.removeprefix("page-"))
            # Prior snapshots record successfully accepted consecutive pages;
            # newer snapshots also retain validated parent pages before splitting.
            if page not in state.get("validated_pages", range(state["pages"])):
                continue
            parsed = self.page(path.parent, state["filters"], page)
            self.check_partition_rows(parsed["rows"], state)
            for row in parsed["rows"]:
                url = row["download_url"]
                locator = {
                    "receipt": str(path.relative_to(self.output)),
                    **row["source_locator"],
                }
                if url not in documents:
                    documents[url] = {
                        **row,
                        "catalog_locators": [],
                        "source_role": SOURCE_ROLE,
                        "current_effect_verified": False,
                    }
                if {
                    k: documents[url][k] for k in ("title", "publishYear", "publishTimestamp", "subCategory")
                } != {k: row[k] for k in ("title", "publishYear", "publishTimestamp", "subCategory")}:
                    raise ValueError("one original URL has conflicting published metadata")
                documents[url]["catalog_locators"].append(locator)
        return list(documents.values())

    def originals(self, rows, *, max_files, byte_budget, reuse_dirs=(), reuse_campaigns=()):
        target = self.output / "originals"
        target.mkdir(mode=0o700, exist_ok=True)
        store = BlobStore(self.output / "files", max_bytes=32 * 1024 * 1024)
        reused = {}
        for directory in reuse_dirs:
            connection = sqlite3.connect(f"file:{directory / 'tea.sqlite3'}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                for source in connection.execute(
                    "SELECT id,owner_id,source_url,storage_path,content_hash,size FROM documents WHERE source_url LIKE 'https://assets.egazette.gov.sg/%'"
                ):
                    row = dict(source)
                    suffix = row["storage_path"].split("/files/", 1)
                    if len(suffix) != 2 or ".." in Path(suffix[1]).parts:
                        continue
                    path = directory / "files" / suffix[1]
                    if path.is_file() and not path.is_symlink() and path.stat().st_size == row["size"]:
                        row["path"] = path
                        reused.setdefault(original_url(row["source_url"]), row)
            finally:
                connection.close()
        for directory in reuse_campaigns:
            for path in (directory / "originals").glob("*.json"):
                prior = load_json(path)
                if prior.get("status") != "downloaded":
                    continue
                original = directory / prior["path"]
                if original.is_symlink() or not original.resolve().is_relative_to(directory.resolve()):
                    raise ValueError("reuse campaign original escapes its data root")
                if original.is_file() and original.stat().st_size == prior["size"]:
                    reused.setdefault(
                        original_url(prior["source_url"]),
                        {
                            "path": original,
                            "content_hash": prior["sha256"],
                            "size": prior["size"],
                            "id": None,
                            "owner_id": None,
                            "campaign_receipt": str(path),
                        },
                    )
        receipts = []
        transferred = 0
        # Reserve the maximum transfer of each in-flight file under one lock.
        reserved, attempted = 0, 0
        existing_bytes = 0
        for path in target.glob("*.json"):
            receipt = load_json(path)
            if receipt["status"] == "downloaded":
                existing_bytes += receipt["size"]
        byte_budget -= existing_bytes

        def fetch(row):
            nonlocal transferred, reserved, attempted
            url = row["download_url"]
            key = sha256(url.encode()).hexdigest()
            receipt_path = target / (key + ".json")
            if receipt_path.exists():
                receipt = load_json(receipt_path)
                if receipt["status"] in {"downloaded", "reused"}:
                    path = Path(receipt["path"])
                    if receipt["status"] == "downloaded":
                        path = self.output / path
                    if (
                        path.is_symlink()
                        or not path.is_file()
                        or path.stat().st_size != receipt["size"]
                        or sha256(path.read_bytes()).hexdigest() != receipt["sha256"]
                    ):
                        raise ValueError("saved original integrity mismatch")
                    return receipt
                # Failed originals are durable explicit gaps; no blind retry.
                return receipt
            receipt = {
                "source_url": url,
                "source_role": SOURCE_ROLE,
                "title": row["title"],
                "catalog_locators": row["catalog_locators"],
                "current_effect_verified": False,
                "legal_review": "pending",
            }
            prior = reused.get(url)
            if prior and sha256(prior["path"].read_bytes()).hexdigest() == prior["content_hash"]:
                receipt.update(
                    status="reused",
                    path=str(prior["path"]),
                    sha256=prior["content_hash"],
                    size=prior["size"],
                    reused_document_id=prior["id"],
                    reused_owner_id=prior["owner_id"],
                    reused_campaign_receipt=prior.get("campaign_receipt"),
                )
                write_json(receipt_path, receipt)
                return receipt
            with self.lock:
                available = byte_budget - transferred - reserved
                if attempted >= max_files or available <= 0 or time.monotonic() >= self.deadline:
                    return {**receipt, "status": "budget_pending"}
                allowance = min(32 * 1024 * 1024, available)
                if shutil.disk_usage(self.output).free - reserved - allowance < self.min_free_bytes:
                    return {**receipt, "status": "storage_pending"}
                reserved += allowance
                attempted += 1
            try:

                def consume(chunks):
                    def checked():
                        prefix = b""
                        verified = False
                        for chunk in chunks:
                            if not verified:
                                prefix += chunk
                                if len(prefix) < 5:
                                    continue
                                if not prefix.startswith(b"%PDF-"):
                                    raise ValueError("eGazette original did not return a PDF signature")
                                yield prefix
                                prefix = b""
                                verified = True
                            else:
                                yield chunk
                        if not verified:
                            raise ValueError("eGazette original PDF signature is truncated")

                    return store.put(SOURCE_OWNER, checked())

                response = self.fetch(
                    url,
                    deadline=min(self.deadline, time.monotonic() + self.timeout),
                    required_host="assets.egazette.gov.sg",
                    max_bytes=allowance,
                    consume=consume,
                    connection_slot=self.slot,
                )
                blob = response.pop("blob")
                receipt.update(
                    status="downloaded",
                    path=str(blob.path.relative_to(self.output)),
                    sha256=blob.digest,
                    size=blob.size,
                    response=response,
                    acquired_at=datetime.now(UTC).isoformat(),
                )
                with self.lock:
                    transferred += blob.size
            except HostCooling as exc:
                return {
                    **receipt,
                    "status": "cooldown_pending",
                    "retry_after_seconds": exc.retry_after,
                }
            except (ValueError, OSError, TimeoutError) as exc:
                receipt.update(
                    status="incomplete",
                    error={"type": type(exc).__name__, "message": str(exc)[:500]},
                )
            finally:
                with self.lock:
                    reserved -= allowance
            write_json(receipt_path, receipt)
            return receipt

        ordered = sorted(
            rows,
            key=lambda row: (
                row["subCategory"] != "Revised Acts",
                -row["publishYear"],
                row["title"],
            ),
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            for receipt in pool.map(fetch, ordered):
                receipts.append(receipt)
                if len(receipts) % 100 == 0:
                    print(
                        json.dumps(
                            {
                                "originals_considered": len(receipts),
                                "downloaded_bytes_this_run": transferred,
                            }
                        ),
                        flush=True,
                    )
        write_json(
            self.output / "original-manifest.json",
            {
                "documents": receipts,
                "downloaded_bytes_this_run": transferred,
                "downloaded_bytes_previous_runs": existing_bytes,
                "model_calls": 0,
                "current_effect_verified": False,
            },
        )
        return {
            "documents": len(receipts),
            "downloaded": sum(x["status"] == "downloaded" for x in receipts),
            "reused": sum(x["status"] == "reused" for x in receipts),
            "pending": sum(
                x["status"] in {"budget_pending", "storage_pending", "cooldown_pending"} for x in receipts
            ),
            "storage_pending": sum(x["status"] == "storage_pending" for x in receipts),
            "cooldown_pending": sum(x["status"] == "cooldown_pending" for x in receipts),
            "failed": sum(x["status"] == "incomplete" for x in receipts),
            "new_bytes": transferred,
        }

    def stage_originals(self, rows, *, reuse_roots=()):
        """Queue verified originals and transferable source evidence, without workers."""
        from app.catalog import MetadataPatch
        from app.db import dump, now
        from app.service import Service

        indexed = {row["download_url"]: row for row in rows}
        roots = [self.output.resolve(), *(Path(root).resolve() for root in reuse_roots)]
        owner_hash = sha256(SOURCE_OWNER.encode()).hexdigest()
        lock_fd = os.open(self.output / "stage.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "w"):
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            service = Service(self.output)
            with service.db.transaction() as conn:
                owner = conn.execute("SELECT * FROM users WHERE id=?", (SOURCE_OWNER,)).fetchone()
                if owner is not None and (owner["email"] or owner["password_hash"] or owner["is_demo"]):
                    raise ValueError("gazette source principal must not have login credentials")
                conn.execute(
                    "INSERT OR IGNORE INTO users VALUES(?,?,?,?,?,?)",
                    (
                        SOURCE_OWNER,
                        "eGazette verified historical originals",
                        None,
                        None,
                        0,
                        now(),
                    ),
                )
            campaign_path = self.output / "campaign.json"
            campaign = (
                load_json(campaign_path, limit=64 * 1024 * 1024)
                if campaign_path.exists()
                else {
                    "protocol": "egazette-source-queue-v1",
                    "owner_id": SOURCE_OWNER,
                    "documents": {},
                    "source_role": SOURCE_ROLE,
                    "current_effect_verified": False,
                }
            )
            if (
                campaign.get("protocol") != "egazette-source-queue-v1"
                or campaign.get("owner_id") != SOURCE_OWNER
            ):
                raise ValueError("source campaign belongs to a different protocol or owner")
            evidence_cache = {}

            def evidence(path):
                path = Path(path)
                if path.is_symlink() or not path.resolve().is_relative_to(self.output.resolve()):
                    raise ValueError("catalogue evidence escaped the campaign directory")
                if path not in evidence_cache:
                    if not 0 < path.stat().st_size <= MAX_SOURCE_BYTES:
                        raise ValueError("catalogue evidence exceeds its byte bound")
                    blob = service.archive.blobs.put_bytes(SOURCE_OWNER, path.read_bytes())
                    evidence_cache[path] = {
                        "cas_path": str(blob.path),
                        "sha256": blob.digest,
                        "size": blob.size,
                    }
                return evidence_cache[path]

            staged = 0
            for receipt_path in sorted((self.output / "originals").glob("*.json")):
                receipt = load_json(receipt_path)
                if receipt.get("status") not in {"downloaded", "reused"}:
                    continue
                url = original_url(receipt["source_url"])
                row = indexed.get(url)
                if row is None or receipt["title"] != row["title"]:
                    raise ValueError("original is absent from the verified catalogue snapshot")
                path = Path(receipt["path"])
                if receipt["status"] == "downloaded":
                    path = self.output / path
                if (
                    path.is_symlink()
                    or not any(path.resolve().is_relative_to(root) for root in roots)
                    or not 0 < receipt["size"] <= 32 * 1024 * 1024
                    or path.stat().st_size != receipt["size"]
                ):
                    raise ValueError("original path or size does not match a permitted source root")
                raw = path.read_bytes()
                digest = sha256(raw).hexdigest()
                if digest != receipt["sha256"] or not raw.startswith(b"%PDF-"):
                    raise ValueError("original bytes disagree with their verified PDF receipt")
                expected = self.output / "files/blobs" / owner_hash / digest[:2] / digest[2:4] / digest
                blob = (
                    Blob(path=path, digest=digest, size=len(raw))
                    if path == expected
                    else service.archive.blobs.put_bytes(SOURCE_OWNER, raw)
                )
                del raw
                catalog_proofs = []
                for locator in row["catalog_locators"]:
                    source = self.output / locator["receipt"]
                    saved = load_json(source)
                    raw_path = source.parent / saved["raw_file"]
                    raw_ref = evidence(raw_path)
                    if raw_ref["sha256"] != locator["response_sha256"]:
                        raise ValueError("catalogue source locator hash mismatch")
                    catalog_proofs.append(
                        {
                            "locator": locator,
                            "raw": raw_ref,
                            "receipt": evidence(source),
                        }
                    )
                with service.db.transaction() as conn:
                    classification = publication_classification(row)
                    doc = service.archive.store_original(
                        conn,
                        SOURCE_OWNER,
                        blob,
                        row["title"] + ".pdf",
                        "application/pdf",
                        url,
                        collection="country:SG",
                        level=classification["level"],
                        tags=[
                            "country:SG",
                            "source:egazette",
                            "gazette:historical-publication",
                            "gazette:" + classification["document_type"],
                        ],
                        identity_key="egazette-published-original:" + url,
                        kind_hint=classification["kind"],
                    )
                    manifest = json.loads(doc["manifest"])
                    if manifest.get("source_hash", digest) != digest:
                        raise ValueError("queued manifest belongs to different original bytes")
                    manifest["source_hash"] = digest
                    manifest["gazette_publication"] = {
                        key: row[key]
                        for key in (
                            "title",
                            "publishYear",
                            "publishTimestamp",
                            "subCategory",
                            "notificationNum",
                        )
                    } | {
                        "source_role": SOURCE_ROLE,
                        "current_effect_verified": False,
                        "classification": classification,
                    }
                    conn.execute(
                        "UPDATE documents SET manifest=? WHERE id=?",
                        (dump(manifest), doc["id"]),
                    )
                metadata = service.catalog.metadata_for(doc["id"], source_url=url)
                if metadata["metadata_revision"] == 0:
                    service.catalog.patch_document(
                        SOURCE_OWNER,
                        doc["id"],
                        MetadataPatch(
                            expected_revision=0, kind=classification["kind"], review_status="draft"
                        ),
                    )
                elif metadata["metadata"]["review_status"] != "draft":
                    raise ValueError("staging cannot overwrite an already reviewed source document")
                key = sha256(url.encode()).hexdigest()
                campaign["documents"][key] = {
                    "document_id": doc["id"],
                    "version": doc["version"],
                    "source_url": url,
                    "source_hash": digest,
                    "original_cas_path": str(blob.path),
                    "original_sha256": digest,
                    "source_role": SOURCE_ROLE,
                    "current_effect_verified": False,
                    "classification": classification,
                    "catalogue": catalog_proofs,
                    "download_receipt": evidence(receipt_path),
                }
                staged += 1
                # A crash can leave a queued document without evidence; rerunning
                # the same URL/hash repairs its record without a new version.
                if staged % 25 == 0:
                    write_json(campaign_path, campaign)
            campaign["updated_at"] = now()
            write_json(campaign_path, campaign)
            with service.db.connect() as conn:
                jobs = conn.execute(
                    "SELECT count(*) FROM processing_jobs WHERE owner_id=?",
                    (SOURCE_OWNER,),
                ).fetchone()[0]
            report = {
                "staged_originals": staged,
                "campaign_documents": len(campaign["documents"]),
                "processing_jobs": jobs,
                "owner_id": SOURCE_OWNER,
                "workers_started": False,
                "model_calls": 0,
                "HTTP_calls": 0,
                "formal_RAG_published": False,
            }
            write_json(self.output / "source-queue.json", report)
            return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--categories",
        nargs="+",
        choices=LEGISLATIVE_CATEGORIES,
        default=list(LEGISLATIVE_CATEGORIES),
    )
    parser.add_argument("--year-from", type=int, default=1998)
    parser.add_argument("--year-through", type=int, default=datetime.now(UTC).year)
    parser.add_argument("--page-size", type=int, choices=[10, 20, 50, 100], default=100)
    parser.add_argument("--timeout", type=float, default=35)
    parser.add_argument("--time-budget", type=float, default=1800)
    parser.add_argument("--originals", type=int, default=0)
    parser.add_argument(
        "--stage-originals",
        action="store_true",
        help="queue acquired originals for later parsing; never start workers",
    )
    parser.add_argument("--original-byte-budget", type=int, default=128 * 1024 * 1024)
    parser.add_argument("--reuse-source-dir", type=Path, action="append", default=[])
    parser.add_argument("--reuse-campaign", type=Path, action="append", default=[])
    parser.add_argument("--network-env", type=Path)
    args = parser.parse_args()
    if (
        not 1998 <= args.year_from <= args.year_through <= datetime.now(UTC).year
        or not 1 <= args.timeout <= 90
        or not 1 <= args.time_budget <= 7200
    ):
        parser.error("invalid year, timeout or time budget")
    if not 0 <= args.originals <= 100_000 or not 1 <= args.original_byte_budget <= MAX_ORIGINAL_BUDGET:
        parser.error("original bounds must be at most 100000 files and 10 GiB")
    if (
        args.output.is_symlink()
        or (args.output.exists() and not args.resume)
        or (args.resume and not args.output.is_dir())
    ):
        parser.error("choose a new output directory or explicitly resume the existing frozen snapshot")
    args.output.mkdir(parents=True, mode=0o700, exist_ok=args.resume)
    if args.network_env:
        for key, value in dotenv_values(args.network_env).items():
            if key.startswith("COLLECTION_") and value:
                os.environ[key] = value
    lock = os.open(args.output / "collector.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, "w"):
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        frozen = {
            "categories": args.categories,
            "year_from": args.year_from,
            "year_through": args.year_through,
            "page_size": args.page_size,
            "source_role": SOURCE_ROLE,
        }
        plan_path = args.output / "plan.json"
        source_path = args.output / "frontend.html"
        if args.resume:
            saved = load_json(plan_path)
            if any(saved[key] != value for key, value in frozen.items()):
                parser.error("resume cannot change the declared source scope or pagination")
            raw = source_path.read_bytes()
            if sha256(raw).hexdigest() != saved["frontend_sha256"]:
                parser.error("saved official frontend hash mismatch")
        else:
            response = _download(
                EGAZETTE_ORIGIN,
                deadline=time.monotonic() + args.timeout,
                required_host="www.egazette.gov.sg",
                max_bytes=MAX_SOURCE_BYTES,
                accept_compressed_html=True,
            )
            raw = response.pop("content")
            public_search_from_html(response["url"], raw)
            save_bytes(source_path, raw)
            frozen.update(
                frontend_sha256=sha256(raw).hexdigest(),
                frontend_response=response,
                plan=json.loads((ROOT / "config/collection-plans/singapore-demo-20261009.json").read_text()),
            )
            write_json(plan_path, frozen)
        request, proof = public_search_from_html(EGAZETTE_ORIGIN, raw)
        write_json(args.output / "public-config-proof.json", proof)
        collector = GazetteCollector(
            args.output,
            request,
            timeout=args.timeout,
            time_budget=args.time_budget,
            page_size=args.page_size,
        )
        states = collector.catalog(args.categories, list(range(args.year_from, args.year_through + 1)))
        rows = collector.rows()
        write_json(
            args.output / "document-directory.json",
            {
                "documents": rows,
                "unique_documents": len(rows),
                "source_role": SOURCE_ROLE,
            },
        )
        originals = (
            collector.originals(
                rows,
                max_files=args.originals,
                byte_budget=args.original_byte_budget,
                reuse_dirs=args.reuse_source_dir,
                reuse_campaigns=args.reuse_campaign,
            )
            if args.originals
            else {"downloaded": 0}
        )
        report = {
            "as_of": datetime.now(UTC).isoformat(),
            "partitions": states,
            "declared_catalogue_complete": all(state["status"] == "catalogue_complete" for state in states),
            "source_role": SOURCE_ROLE,
            "unique_original_urls": len(rows),
            "originals": originals,
            "per_host_peak": collector.peak,
            "actual_connection_hops": collector.hops,
            "all_required_sg_content_complete": False,
            "current_effect_verified": False,
            "model_calls": 0,
        }
        if args.stage_originals:
            report["source_queue"] = collector.stage_originals(
                rows, reuse_roots=[*args.reuse_source_dir, *args.reuse_campaign]
            )
        write_json(args.output / "report.json", report)
        print(
            json.dumps({key: value for key, value in report.items() if key != "partitions"}),
            flush=True,
        )
        return 0 if report["declared_catalogue_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
