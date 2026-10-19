#!/usr/bin/env python3
"""Resume public Setneg discovery with complete provider denominators and CAS receipts.

All four configured categories are enumerated to their terminal pages. Detail
and original acquisition can be selected separately. This does not shrink the
29-item checklist, grant legal approval, or bypass existing download controls.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.blob_store import Blob, BlobStore, _verify_existing  # noqa: E402 -- repository script bootstrap
from app.checklists import load_blueprints  # noqa: E402
from app.data_backup import _safe_absolute  # noqa: E402
from app.discovery import (  # noqa: E402
    MAX_HTML_BYTES,
    discover_page,
    normalized_seeds_for,
    recipe_for,
)
from app.indonesia_official import (  # noqa: E402
    SETNEG_ITEMS,
    ledger_record,
    original_file_relations,
    reconcile_catalogue,
    source_relation_claims,
)
from verify_collection_sources import SourceVerifier, write_report  # noqa: E402

SOURCE_OWNER = "setneg-official-corpus-20261019"
MAX_ORIGINAL_BYTES = 128 * 1024 * 1024
MAX_ORIGINAL_BYTE_BUDGET = 64 * 1024**3
MIN_FREE_BYTES = 256 * 1024 * 1024


class CorpusBlobs(BlobStore):
    def put(self, _probe_owner, chunks):
        return super().put(SOURCE_OWNER, chunks)


def open_state(
    output: Path,
    *,
    resume: bool,
    definition_hash: str,
    storage_root: Path | None = None,
) -> sqlite3.Connection:
    resolved = output.resolve()
    allowed = [ROOT / p for p in ("data", ".dev")]
    if storage_root is not None:
        if not storage_root.is_absolute() or not storage_root.is_dir() or storage_root.is_symlink():
            raise ValueError("explicit storage root must be an existing real absolute directory")
        allowed.append(storage_root.resolve())
    if output.is_symlink() or not any(resolved.is_relative_to(p) for p in allowed):
        raise ValueError("output must be below project data/.dev or the explicit storage root")
    if resume:
        if not (output / "journal.sqlite3").is_file():
            raise ValueError("resume requires the existing collection journal")
    elif output.exists():
        raise ValueError("new collection output must not already exist")
    else:
        output.mkdir(parents=True, mode=0o700)
    c = sqlite3.connect(output / "journal.sqlite3")
    c.row_factory = sqlite3.Row
    c.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(
            item_code TEXT NOT NULL, url TEXT NOT NULL, purpose TEXT NOT NULL,
            parent_url TEXT, status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
            result TEXT, PRIMARY KEY(item_code,url));
        CREATE TABLE IF NOT EXISTS attempts(
            id INTEGER PRIMARY KEY, item_code TEXT NOT NULL,url TEXT NOT NULL,result TEXT NOT NULL);
    """)
    saved = c.execute("SELECT value FROM config WHERE key='definition_hash'").fetchone()
    if saved and saved[0] != definition_hash:
        c.close()
        raise ValueError("the immutable collection plan changed; use a new output directory")
    c.execute("INSERT OR IGNORE INTO config VALUES('definition_hash',?)", (definition_hash,))
    c.commit()
    return c


def verify_receipts(c: sqlite3.Connection, output: Path) -> None:
    """Successful requests are never skipped on resume without verifying their bytes."""
    for row in c.execute("SELECT result FROM tasks WHERE status='downloaded'"):
        result = json.loads(row[0])
        relative = Path(result["evidence_path"])
        path = output / relative
        if relative.is_absolute() or not path.resolve().is_relative_to(output.resolve()) or path.is_symlink():
            raise ValueError("saved receipt path is not a regular local evidence file")
        if not path.is_file() or path.stat().st_size != result["size"]:
            raise ValueError("saved receipt size changed")
        with path.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        if digest != result["content_hash"]:
            raise ValueError("saved receipt hash changed")
        reuse = result.get("cache_reuse")
        if reuse:
            relative = Path(reuse["source_result_cas"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("historical receipt proof must remain inside the target CAS")
            path = _safe_absolute(output / relative)
            if not path.is_file() or not 0 < path.stat().st_size <= MAX_HTML_BYTES:
                raise ValueError("historical receipt proof size changed")
            with path.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != reuse["source_result_sha256"]:
                    raise ValueError("historical receipt proof hash changed")


def record_result(c: sqlite3.Connection, task: dict, result: dict, *, network_attempt=True) -> None:
    discovery = result.get("discovery", {})
    status = result["status"]
    if status == "downloaded" and not discovery.get("matched"):
        status = "rejected"
    encoded = json.dumps(result, ensure_ascii=False)
    with c:
        c.execute(
            "INSERT INTO attempts(item_code,url,result) VALUES(?,?,?)",
            (task["item_code"], task["url"], encoded),
        )
        c.execute(
            "UPDATE tasks SET status=?,attempts=attempts+?,result=? WHERE item_code=? AND url=?",
            (status, int(network_attempt), encoded, task["item_code"], task["url"]),
        )
        delay = result.get("retry_after_seconds")
        if network_attempt and result["status"] == "downloaded":
            c.execute("INSERT OR REPLACE INTO config VALUES('transport_failure_streak','0')")
        elif network_attempt and status == "failed" and result.get("retryable"):
            previous = c.execute("SELECT value FROM config WHERE key='transport_failure_streak'").fetchone()
            streak = (int(previous[0]) if previous else 0) + 1
            c.execute(
                "INSERT OR REPLACE INTO config VALUES('transport_failure_streak',?)",
                (str(streak),),
            )
            if streak >= 3:
                # A whole failing batch can indicate a source/network outage
                # even when no HTTP response supplies Retry-After. Preserve
                # untouched queued work and cool down before another batch.
                delay = max(float(delay or 0), min(900, 60 * 2 ** min(streak - 3, 4)))
        if network_attempt and ("HTTP 429" in result.get("error", "") or delay):
            # All queued work here is for this one official provider. Persist
            # the entire source's pause, including across collector restarts.
            until = time.time() + max(float(delay or 0), 60)
            previous = c.execute("SELECT value FROM config WHERE key='cooldown_until'").fetchone()
            if previous:
                until = max(until, float(previous[0]))
            c.execute(
                "INSERT OR REPLACE INTO config VALUES('cooldown_until',?)",
                (str(until),),
            )
        if status != "downloaded":
            return
        for link in discovery.get("links", []):
            if link["purpose"] not in {"catalog", "detail", "document"}:
                continue
            # Links already passed discover_page's immutable recipe. Avoid
            # following any provider unrelated to this exact Setneg category.
            if not link["url"].startswith("https://jdih.setneg.go.id/api/hukumproduk/"):
                continue
            c.execute(
                "INSERT OR IGNORE INTO tasks(item_code,url,purpose,parent_url) VALUES(?,?,?,?)",
                (task["item_code"], link["url"], link["purpose"], task["url"]),
            )


def reuse_receipts(c, output, source_dirs, definitions):
    """Reuse explicit historical responses, never claim a new upstream fetch.

    Only already-discovered detail/original tasks are eligible. Re-inspect the
    stored response with the current frozen recipe and rebind verified bytes to
    this collector's CAS. Cached success must not reset source transport backoff.
    """
    if len(source_dirs) > 8:
        raise ValueError("receipt reuse supports at most eight explicit source directories")
    records = []
    for directory in source_dirs:
        directory = _safe_absolute(directory)
        if directory == output.resolve():
            raise ValueError("receipt reuse source must differ from the target")
        journal = _safe_absolute(directory / "journal.sqlite3")
        with closing(sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True)) as donor:
            donor.row_factory = sqlite3.Row
            donor.execute("PRAGMA query_only=ON")
            for purpose in ("detail", "document"):
                for row in donor.execute(
                    "SELECT * FROM tasks WHERE purpose=? AND status='downloaded' ORDER BY item_code,url",
                    (purpose,),
                ):
                    definition = definitions.get(row["item_code"])
                    task = c.execute(
                        "SELECT * FROM tasks WHERE item_code=? AND url=? AND purpose=?",
                        (row["item_code"], row["url"], purpose),
                    ).fetchone()
                    if definition is None or task is None or task["status"] == "downloaded":
                        continue
                    raw_result = row["result"]
                    if not isinstance(raw_result, str) or len(raw_result) > MAX_HTML_BYTES:
                        raise ValueError("saved receipt exceeds the bounded JSON limit")
                    result = json.loads(raw_result)
                    if (
                        not isinstance(result, dict)
                        or result.get("status") != "downloaded"
                        or result.get("requested_url") != row["url"]
                        or result.get("configured_purpose") != purpose
                        or type(result.get("size")) is not int
                        or not 0 < result["size"] <= 128 * 1024 * 1024
                        or any(
                            not isinstance(result.get(key), str) or not result[key]
                            for key in (
                                "evidence_path",
                                "content_hash",
                                "final_url",
                                "mime_type",
                            )
                        )
                    ):
                        raise ValueError("saved receipt identity or size changed")
                    relative = Path(result["evidence_path"])
                    digest = result["content_hash"]
                    if (
                        relative.is_absolute()
                        or ".." in relative.parts
                        or len(relative.parts) != 6
                        or relative.parts[:2] != ("files", "blobs")
                        or len(digest) != 64
                        or any(char not in "0123456789abcdef" for char in digest)
                        or relative.parts[3:] != (digest[:2], digest[2:4], digest)
                    ):
                        raise ValueError("receipt reuse requires a local CAS evidence path")
                    path = _safe_absolute(directory / relative)
                    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(descriptor, "rb") as handle:
                        before = os.fstat(handle.fileno())
                        if not stat.S_ISREG(before.st_mode) or before.st_size != result["size"]:
                            raise ValueError("saved receipt size changed")
                        if hashlib.file_digest(handle, "sha256").hexdigest() != result["content_hash"]:
                            raise ValueError("saved receipt hash changed")
                        handle.seek(0)
                        prefix = handle.read(8)
                        if prefix.startswith(b"%PDF-"):
                            content = prefix
                        elif before.st_size <= MAX_HTML_BYTES:
                            handle.seek(0)
                            content = handle.read(MAX_HTML_BYTES + 1)
                        else:
                            raise ValueError("saved discovery response exceeds its size limit")
                        recipe = recipe_for(definition)
                        discovery = discover_page(result["final_url"], content, result["mime_type"], recipe)
                        requested = discover_page(row["url"], content, result["mime_type"], recipe)
                        if any(
                            not item.get("matched") or item.get("purpose") != purpose
                            for item in (discovery, requested)
                        ):
                            raise ValueError("saved response does not match the current source recipe")
                        handle.seek(0)
                        blobs = CorpusBlobs(output / "files", max_bytes=128 * 1024 * 1024)
                        blob = blobs.put(SOURCE_OWNER, iter(lambda: handle.read(1024 * 1024), b""))
                        after = os.fstat(handle.fileno())
                        if (
                            (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
                            or blob.digest != result["content_hash"]
                            or blob.size != result["size"]
                        ):
                            raise ValueError("saved receipt changed while copying")
                    receipt_blob = blobs.put(SOURCE_OWNER, [raw_result.encode()])
                    result.update(
                        evidence_path=str(blob.path.relative_to(output)),
                        discovery=discovery,
                        original_acquired=purpose == "document",
                        cache_reuse={
                            "source_directory": str(directory),
                            "source_item": row["item_code"],
                            "source_url": row["url"],
                            "source_result_sha256": hashlib.sha256(raw_result.encode()).hexdigest(),
                            "source_result_cas": str(receipt_blob.path.relative_to(output)),
                            "source_evidence_path": str(relative),
                            "network_attempts": 0,
                            "freshness": "historical_response_not_refetched",
                        },
                    )
                    record_result(c, dict(task), result, network_attempt=False)
                    records.append(
                        {
                            "item_code": row["item_code"],
                            "url": row["url"],
                            "purpose": purpose,
                            "sha256": blob.digest,
                            "source_directory": str(directory),
                        }
                    )
    return {"reused_responses": len(records), "network_attempts": 0, "records": records}


def export_state(c: sqlite3.Connection, output: Path) -> dict:
    tasks = [dict(row) for row in c.execute("SELECT * FROM tasks ORDER BY item_code,purpose,url")]
    by_url = {(row["item_code"], row["url"]): row for row in tasks}
    catalogue = {key: [] for key in SETNEG_ITEMS}
    ledger = []
    known_ids = set()
    for task in tasks:
        if task["purpose"] == "catalog" and task["status"] == "downloaded":
            proof = json.loads(task["result"])
            data = json.loads((output / proof["evidence_path"]).read_bytes())
            known_ids.update(row["idperaturan"] for row in data["data"])
    for task in tasks:
        if task["purpose"] != "catalog" or task["status"] != "downloaded":
            continue
        receipt = json.loads(task["result"])
        body = json.loads((output / receipt["evidence_path"]).read_bytes())
        scope = receipt["discovery"]["scope"]
        catalogue[task["item_code"]].append(
            {
                **{key: scope[key] for key in ("total", "offset", "page_size", "returned")},
                "official_ids": [row["idperaturan"] for row in body["data"]],
                "sha256": receipt["content_hash"],
                "path": receipt["evidence_path"],
                "url": receipt["final_url"],
            }
        )
        for source in body["data"]:
            record = ledger_record(source, task["item_code"], receipt)
            detail = by_url.get((task["item_code"], record["url"]))
            if detail:
                record["detail_status"] = detail["status"]
                if detail["status"] == "downloaded":
                    proof = json.loads(detail["result"])
                    record["detail_evidence"] = {
                        key: proof[key] for key in ("final_url", "content_hash", "evidence_path")
                    }
                    if proof.get("cache_reuse"):
                        record["detail_receipt_reuse"] = proof["cache_reuse"]
                    record["detail_warnings"] = proof["discovery"].get("warnings", [])
                    detail_payload = json.loads((output / proof["evidence_path"]).read_bytes())
                    raw_detail = detail_payload["row"][0]
                    if raw_detail.get("idperaturan") != record["official_id"]:
                        raise ValueError("official detail identity differs from its catalogue record")
                    record["source_metadata"] = raw_detail
                    record["source_relation_claims"] = source_relation_claims(raw_detail, proof, known_ids)
                    for link in proof["discovery"].get("links", []):
                        original = by_url.get((task["item_code"], link["url"]))
                        asset = {
                            "url": link["url"],
                            "title": link["title"],
                            "status": original["status"] if original else "unconfirmed",
                        }
                        positions = [
                            index
                            for index, file in enumerate(detail_payload.get("file", []))
                            if isinstance(file, dict) and file.get("realName") == link["title"]
                        ]
                        if len(positions) == 1:
                            asset["official_file"] = {
                                "json_pointer": f"/file/{positions[0]}/realName",
                                "filename": link["title"],
                            }
                        if original and original["result"]:
                            evidence = json.loads(original["result"])
                            for key in (
                                "size",
                                "content_hash",
                                "evidence_path",
                                "original_acquired",
                                "error",
                                "cache_reuse",
                            ):
                                if key in evidence:
                                    asset[key] = evidence[key]
                        record["assets"].append(asset)
                    record["status"] = (
                        "originals_acquired"
                        if record["assets"]
                        and all(a.get("original_acquired") for a in record["assets"])
                        and not record["detail_warnings"]
                        else "detail_observed_originals_incomplete"
                    )
                    record["file_associations"] = original_file_relations(record)
            ledger.append(record)
    temporary = output / "ledger.jsonl.tmp"
    with temporary.open("w", encoding="utf-8") as handle:
        for row in ledger:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(output / "ledger.jsonl")
    categories = {
        item: {
            **reconcile_catalogue(pages),
            "instrument": SETNEG_ITEMS[item],
            "page_evidence": pages,
        }
        for item, pages in catalogue.items()
    }
    for item, category in categories.items():
        records = [row for row in ledger if row["item_code"] == item]
        acquired = sum(row["status"] == "originals_acquired" for row in records)
        # Count instruments separately from files: additional attachments must
        # never compensate for another instrument with no original at all.
        category.update(
            instruments_with_all_observed_originals=acquired,
            instrument_original_scope_complete=bool(
                category["observed_catalogue_complete"] and acquired == len(records)
            ),
            downloaded_original_files=sum(
                bool(asset.get("original_acquired")) for row in records for asset in row["assets"]
            ),
            observed_details_without_originals=[
                row["source_key"]
                for row in records
                if row.get("detail_status") == "downloaded" and not row["assets"]
            ],
        )
    originals = [
        json.loads(t["result"]) for t in tasks if t["purpose"] == "document" and t["status"] == "downloaded"
    ]
    report = {
        "format": "aitea-setneg-observed-catalogue-v1",
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "plan_sha256": c.execute("SELECT value FROM config WHERE key='definition_hash'").fetchone()[0],
        "categories": categories,
        "ledger_rows": len(ledger),
        "observed_catalogues_complete": all(
            row["observed_catalogue_complete"] for row in categories.values()
        ),
        "originals_acquired": len(originals),
        "original_bytes": sum(r["size"] for r in originals),
        "source_retry_state": {
            row["key"]: row["value"]
            for row in c.execute(
                "SELECT key,value FROM config WHERE key IN ('cooldown_until','transport_failure_streak')"
            )
        },
        "text_processing": "not_executed_by_source_collector",
        "legal_review": "unreviewed; no automatic current-law or RAG admission",
        "full_29_item_checklist_complete": False,
        "tasks": {
            purpose: {
                status: sum(t["purpose"] == purpose and t["status"] == status for t in tasks)
                for status in sorted({t["status"] for t in tasks})
            }
            for purpose in ("catalog", "detail", "document")
        },
        "errors": [
            {
                "item_code": t["item_code"],
                "url": t["url"],
                "status": t["status"],
                "result": json.loads(t["result"]),
            }
            for t in tasks
            if t["status"] in {"failed", "rejected"}
        ],
    }
    write_report(output, report)
    return report


def prepare_library(output: Path, *, min_free_bytes=MIN_FREE_BYTES) -> dict:
    """Create original versions and durable processing jobs, without model calls."""
    from app.db import dump, now
    from app.service import Service

    service = Service(output)
    with service.db.transaction() as conn:
        exists = conn.execute("SELECT id,name,is_demo FROM users WHERE id=?", (SOURCE_OWNER,)).fetchone()
        if exists and (exists["name"] != "Setneg official source collection" or not exists["is_demo"]):
            raise ValueError("source collection owner conflicts with another account")
        conn.execute(
            "INSERT OR IGNORE INTO users VALUES(?,?,?,?,?,?)",
            (SOURCE_OWNER, "Setneg official source collection", None, None, 1, now()),
        )
    state_path = output / "campaign.json"
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {
            "format": "setneg-official-originals-v1",
            "owner_id": SOURCE_OWNER,
            "documents": {},
            "country": "ID",
            "legal_review": "draft",
            "model_calls_by_preparer": 0,
        }
    )
    if state.get("format") != "setneg-official-originals-v1" or state.get("owner_id") != SOURCE_OWNER:
        raise ValueError("source preparation checkpoint belongs to another collection")
    state.pop("model_calls", None)
    state["model_calls_by_preparer"] = 0
    with closing(service.db.connect()) as conn:
        existing_documents = {
            row["id"]: dict(row)
            for row in conn.execute(
                "SELECT id,version,content_hash FROM documents WHERE owner_id=?",
                (SOURCE_OWNER,),
            )
        }

    def owned_blob(relative, digest, size=None):
        path = output / relative
        target = (
            service.archive.blobs.blobs_dir
            / hashlib.sha256(SOURCE_OWNER.encode()).hexdigest()
            / digest[:2]
            / digest[2:4]
            / digest
        )
        if path.absolute() == target.absolute():
            return Blob(path, digest, path.stat().st_size)
        source_size = path.stat().st_size
        if size is not None and source_size != size:
            raise ValueError("source original changed during preparation")
        if target.exists():
            _verify_existing(path, digest, source_size)
            _verify_existing(target, digest, source_size)
            return Blob(target, digest, source_size)
        if shutil.disk_usage(output).free < min_free_bytes + source_size:
            raise ValueError("library preparation paused at the disk reserve; resume after freeing space")
        with path.open("rb") as handle:
            blob = service.archive.blobs.put(SOURCE_OWNER, iter(lambda: handle.read(64 * 1024), b""))
        if blob.digest != digest or (size is not None and blob.size != size):
            raise ValueError("source original changed during preparation")
        return blob

    with (output / "ledger.jsonl").open() as ledger:
        for line in ledger:
            record = json.loads(line)
            for asset in record["assets"]:
                if not asset.get("original_acquired"):
                    continue
                key = record["source_key"] + ":" + hashlib.sha256(asset["url"].encode()).hexdigest()[:16]
                previous = state["documents"].get(key)
                if previous:
                    existing = existing_documents.get(previous["document_id"])
                    if (
                        not existing
                        or existing["content_hash"] != asset["content_hash"]
                        or existing["version"] != previous["document_version"]
                    ):
                        raise ValueError("prepared original identity changed; inspect the owned library")
                    # Model workers may be changing this document's derived
                    # manifest. Never resave or reclassify an existing original.
                    continue
                blob = owned_blob(asset["evidence_path"], asset["content_hash"], asset["size"])
                proofs = {}
                for label, proof in (
                    ("catalogue", record["discovery_page"]),
                    ("detail", record["detail_evidence"]),
                ):
                    digest = proof.get("sha256", proof.get("content_hash"))
                    saved = owned_blob(proof.get("path", proof.get("evidence_path")), digest)
                    proofs[label] = {
                        "cas_path": str(saved.path),
                        "sha256": saved.digest,
                        "url": proof.get("url", proof.get("final_url")),
                    }
                for label, reuse in (
                    ("detail_receipt", record.get("detail_receipt_reuse")),
                    ("original_receipt", asset.get("cache_reuse")),
                ):
                    if reuse:
                        saved = owned_blob(reuse["source_result_cas"], reuse["source_result_sha256"])
                        proofs[label] = {
                            "cas_path": str(saved.path),
                            "sha256": saved.digest,
                            "url": reuse["source_url"],
                            "freshness": "historical_response_not_refetched",
                        }
                with service.db.transaction() as conn:
                    doc = service.archive.store_original(
                        conn,
                        SOURCE_OWNER,
                        blob,
                        asset["title"],
                        "application/pdf",
                        asset["url"],
                        collection="country:ID",
                        level="primary",
                        kind_hint="law",
                        tags=[
                            "country:ID",
                            "checklist:ID-" + record["item_code"],
                            "setneg-official",
                            "effect-unreviewed",
                        ],
                        identity_key=key,
                    )
                    manifest = json.loads(doc["manifest"])
                    if not manifest.get("source_hash"):
                        manifest["source_hash"] = blob.digest
                        conn.execute(
                            "UPDATE documents SET manifest=? WHERE id=?",
                            (dump(manifest), doc["id"]),
                        )
                state["documents"][key] = {
                    "document_id": doc["id"],
                    "document_version": doc["version"],
                    "source_key": record["source_key"],
                    "source_type": record["source_type"],
                    "level": "primary",
                    "item_code": record["item_code"],
                    "source_url": asset["url"],
                    "original": {"cas_path": str(blob.path), "sha256": blob.digest},
                    "source_evidence": proofs,
                    "source_metadata": record.get("source_metadata", {}),
                    "source_relation_claims": record.get("source_relation_claims", []),
                    "legal_review": "draft",
                }
            for relation in original_file_relations(record):
                keys = [
                    record["source_key"] + ":" + hashlib.sha256(relation[side].encode()).hexdigest()[:16]
                    for side in ("source_url", "target_url")
                ]
                source, target = (state["documents"].get(key) for key in keys)
                if not source or not target:
                    continue
                with service.db.transaction() as conn:
                    service.archive.add_relation(
                        SOURCE_OWNER,
                        source["document_id"],
                        {
                            "target_document_id": target["document_id"],
                            "collection": "country:ID",
                            "relation": "related",
                            "locator": json.dumps(
                                relation["locator"],
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        },
                        conn=conn,
                    )
    state["updated_at"] = now()
    with closing(service.db.connect()) as conn:
        state["queued_jobs"] = conn.execute(
            "SELECT count(*) FROM processing_jobs WHERE owner_id=? AND status='queued'",
            (SOURCE_OWNER,),
        ).fetchone()[0]
    temporary = output / "campaign.json.tmp"
    with temporary.open("w") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(state_path)
    return {
        "owner_id": SOURCE_OWNER,
        "database": str(output / "tea.sqlite3"),
        "documents": len(state["documents"]),
        "queued_jobs": state["queued_jobs"],
        "model_calls_by_preparer": 0,
    }


def run(args) -> int:
    plan_path = ROOT / "config/collection-plans/indonesia-20261003.json"
    digest = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    plan = load_blueprints()["indonesia-20261003"]
    definitions = {row["id"]: row for row in plan["items"] if row["id"] in SETNEG_ITEMS}
    output = args.output.absolute()
    c = open_state(
        output,
        resume=args.resume,
        definition_hash=digest,
        storage_root=args.storage_root,
    )
    lock = (output / "collector.lock").open("a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        c.close()
        lock.close()
        raise ValueError("another collector owns this output") from None
    try:
        verify_receipts(c, output)
        for item, definition in definitions.items():
            for seed in normalized_seeds_for(definition):
                if seed["url"].startswith("https://jdih.setneg.go.id/api/hukumproduk/produkhukum?"):
                    c.execute(
                        "INSERT OR IGNORE INTO tasks(item_code,url,purpose) VALUES(?,?,'catalog')",
                        (item, seed["url"]),
                    )
        if args.retry_failed:
            c.execute(
                "UPDATE tasks SET status='queued' WHERE status IN ('failed','rejected','budget_exhausted')"
            )
        c.commit()
        if source_dirs := getattr(args, "reuse_receipts_from", []):
            reused = reuse_receipts(c, output, source_dirs, definitions)
            print(json.dumps({"receipt_reuse": reused}, ensure_ascii=False), flush=True)
        verifier = SourceVerifier(
            output,
            timeout=args.timeout,
            time_budget=args.time_budget,
            workers=args.workers,
            per_host=args.per_host,
            max_original_bytes=MAX_ORIGINAL_BYTES,
        )
        verifier.blobs = CorpusBlobs(output / "files", max_bytes=MAX_ORIGINAL_BYTES)
        original_bytes = sum(
            json.loads(r[0])["size"]
            for r in c.execute("SELECT result FROM tasks WHERE purpose='document' AND status='downloaded'")
        )
        next_export = time.monotonic() + 15
        storage_backpressure = False
        for stage in ("catalog", "content"):
            while time.monotonic() < verifier.deadline:
                cooldown = c.execute("SELECT value FROM config WHERE key='cooldown_until'").fetchone()
                if cooldown and float(cooldown[0]) > time.time():
                    break
                pending = [
                    dict(r)
                    for r in c.execute(
                        "SELECT * FROM tasks WHERE purpose IN (?,?) AND status='queued' ORDER BY item_code,url",
                        ("catalog", "catalog") if stage == "catalog" else ("detail", "document"),
                    )
                ]
                if stage != "catalog":
                    pending = [r for r in pending if SETNEG_ITEMS[r["item_code"]] in args.detail_types]
                remaining = args.original_byte_budget - original_bytes
                available_disk = max(0, shutil.disk_usage(output).free - args.min_free_bytes)
                original_limit = min(remaining, MAX_ORIGINAL_BYTES) if remaining > 0 else 0
                # Every queued future may publish its maximum response. Reserve
                # the whole batch, including futures waiting for a host slot.
                document_slots = (
                    min(
                        args.per_host,
                        args.workers,
                        remaining // original_limit,
                        available_disk // original_limit,
                    )
                    if original_limit
                    else 0
                )
                if stage == "content":
                    originals = [r for r in pending if r["purpose"] == "document"]
                    if originals and original_limit and not document_slots:
                        storage_backpressure = True
                    if not document_slots:
                        originals = []
                    # Consume discovered originals before expanding another set
                    # of details; no type waits behind another type's full list.
                    pending = originals or [r for r in pending if r["purpose"] == "detail"]
                if not pending:
                    break
                purpose = pending[0]["purpose"]
                if purpose == "document":
                    verifier.max_original_bytes = original_limit
                    slots = document_slots
                else:
                    verifier.max_original_bytes = MAX_ORIGINAL_BYTES
                    slots = min(args.workers, available_disk // MAX_HTML_BYTES)
                if not slots:
                    storage_backpressure = True
                    break
                batch, selected = [], set()
                for row in pending:
                    if row["item_code"] not in selected:
                        batch.append(row)
                        selected.add(row["item_code"])
                    if len(batch) == slots:
                        break
                batch.extend(row for row in pending if row not in batch and len(batch) < slots)
                with ThreadPoolExecutor(max_workers=len(batch)) as executor:
                    futures = {}
                    for task in batch:
                        definition = definitions[task["item_code"]]
                        row = {
                            "country": "ID",
                            "item_id": task["item_code"],
                            "recipe": recipe_for(definition),
                        }
                        future = executor.submit(
                            verifier.probe,
                            row,
                            {"url": task["url"], "purpose": task["purpose"]},
                            definition,
                        )
                        futures[future] = (task, definition)
                    for future in as_completed(futures):
                        task, definition = futures[future]
                        result = future.result()
                        record_result(c, task, result)
                        if task["purpose"] == "document" and result.get("original_acquired"):
                            original_bytes += result["size"]
                if time.monotonic() >= next_export:
                    report = export_state(c, output)
                    print(
                        json.dumps(
                            {
                                "stage": stage,
                                "catalogue_records": report["ledger_rows"],
                                "originals": report["originals_acquired"],
                                "original_bytes": report["original_bytes"],
                                "errors": len(report["errors"]),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                    next_export = time.monotonic() + 15
                    if args.prepare_library:
                        prepare_library(output, min_free_bytes=args.min_free_bytes)
        report = export_state(c, output)
        verify_receipts(c, output)
        requested = set(args.detail_types)
        unfinished_details = sum(
            1
            for row in c.execute("SELECT item_code,status FROM tasks WHERE purpose='detail'")
            if SETNEG_ITEMS[row["item_code"]] in requested and row["status"] != "downloaded"
        )
        unfinished_originals = sum(
            1
            for row in c.execute("SELECT item_code,status FROM tasks WHERE purpose='document'")
            if SETNEG_ITEMS[row["item_code"]] in requested and row["status"] != "downloaded"
        )
        with (output / "ledger.jsonl").open() as handle:
            original_scope_gaps = [
                row["source_key"]
                for line in handle
                if (row := json.loads(line))["source_type"] in requested
                and row.get("detail_status") == "downloaded"
                and row["status"] != "originals_acquired"
            ]
        report["requested_scope"] = {
            "catalogue_types": list(SETNEG_ITEMS.values()),
            "detail_types": args.detail_types,
            "original_byte_budget": args.original_byte_budget,
            "storage_backpressure": storage_backpressure,
            "min_free_bytes": args.min_free_bytes,
            "unfinished_details": unfinished_details,
            "unfinished_originals": unfinished_originals,
            "original_scope_gaps": original_scope_gaps,
            "complete": bool(
                report["observed_catalogues_complete"]
                and not report["errors"]
                and not unfinished_details
                and (not args.original_byte_budget or (not unfinished_originals and not original_scope_gaps))
            ),
        }
        write_report(output, report)
        if args.prepare_library:
            print(
                json.dumps(
                    {"prepared_library": prepare_library(output, min_free_bytes=args.min_free_bytes)},
                    ensure_ascii=False,
                ),
                flush=True,
            )
        return 0 if report["requested_scope"]["complete"] else 2
    finally:
        c.close()
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--storage-root",
        type=Path,
        help="explicitly authorized data root on another disk",
    )
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--reuse-receipts-from",
        type=Path,
        action="append",
        default=[],
        help="reuse verified historical detail/original responses from another explicit collection journal",
    )
    parser.add_argument("--detail-types", type=lambda value: value.split(","), default=["PERPU"])
    parser.add_argument(
        "--original-byte-budget",
        type=int,
        default=0,
        help="cumulative original bytes across resumes, 0 disables originals; maximum 64 GiB",
    )
    parser.add_argument("--min-free-bytes", type=int, default=MIN_FREE_BYTES)
    parser.add_argument(
        "--prepare-library",
        action="store_true",
        help="save originals and queue processing; do not call models",
    )
    parser.add_argument("--time-budget", type=int, default=1200)
    parser.add_argument("--timeout", type=int, default=45)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--per-host", type=int, default=2)
    args = parser.parse_args()
    if (
        not set(args.detail_types) <= set(SETNEG_ITEMS.values())
        or not 0 <= args.original_byte_budget <= MAX_ORIGINAL_BYTE_BUDGET
    ):
        parser.error("detail types must be UU, PERPU, PP or PERPRES; original byte budget is 0–64 GiB")
    if args.min_free_bytes < MIN_FREE_BYTES:
        parser.error("minimum free space must be at least 256 MiB")
    try:
        return run(args)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"collection stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
