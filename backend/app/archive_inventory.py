"""Export a private, read-only inventory of schema 7–12 without opening originals.

SQLite rows share one read transaction. Original paths are checked separately
with no-follow stat calls; stored hashes are reported, never recomputed. This
catalog describes stored records, not coverage of any external website.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .blob_store import _sync_directory

BATCH_SIZE = 200
SUPPORTED_SCHEMA_VERSIONS = (7, 8, 9, 10, 11, 12)
FILES = (
    "documents.jsonl",
    "discovery-pages.jsonl",
    "relations.jsonl",
    "ocr-pages.jsonl",
    "ocr-regions.jsonl",
)
_COUNTRY = "COALESCE(dc.country,CASE WHEN d.role='secondary' THEN pc.country END)"
_DOCUMENTS = """documents d
    LEFT JOIN document_countries dc ON dc.document_id=d.id
    LEFT JOIN documents parent ON parent.id=d.parent_id AND parent.owner_id=d.owner_id
    LEFT JOIN document_countries pc ON pc.document_id=parent.id"""
_PAGES = """checklist_pages p JOIN checklist_bindings b ON b.id=p.binding_id
    JOIN collection_items i ON i.id=p.item_id AND i.owner_id=b.owner_id"""


class InventoryError(ValueError):
    """A catalog could not be published; no archive state was changed."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _owner_hash(owner):
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()


def _country_key(country):
    if country is None:
        return "unassigned"
    if not isinstance(country, str) or re.fullmatch(r"[A-Z]{2}", country) is None:
        raise InventoryError("country must be a two-letter uppercase ISO code, for example ID")
    return country


def _where(owner, country, owner_column="d.owner_id", country_column=_COUNTRY):
    conditions, parameters = [], []
    if owner is not None:
        conditions.append(f"{owner_column}=?")
        parameters.append(owner)
    if country is not None:
        conditions.append(f"{country_column}=?")
        parameters.append(country)
    return " AND ".join(conditions) or "1", parameters


def _batches(cursor):
    while rows := cursor.fetchmany(BATCH_SIZE):
        yield rows


def _rows(cursor):
    for batch in _batches(cursor):
        yield from batch


def _json_value(value, expected, fallback, anomalies, label):
    try:
        parsed = json.loads(value)
        if not isinstance(parsed, expected):
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        anomalies.append(f"invalid_{label}")
        return fallback


def _validate_schema(conn):
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise InventoryError(
                "archive inventory requires schema 7–12; use the matching application version"
            )
        # Check required columns before creating an output directory. A version
        # marker alone cannot make a partial migration usable.
        for query in (
            "SELECT id,owner_id,family_id,parent_id,case_id,title,filename,role,status,mime_type,size,version,content_hash,storage_path,tags,source_url,manifest,error,created_at FROM documents LIMIT 0",
            "SELECT document_id,country FROM document_countries LIMIT 0",
            "SELECT document_id,collection,level,tags,revision,updated_at FROM legal_classifications LIMIT 0",
            "SELECT id,owner_id,source_document_id,target_document_id,collection,relation,locator,created_at FROM legal_relations LIMIT 0",
            "SELECT document_id,payload,revision,reviewed_at FROM document_metadata LIMIT 0",
            "SELECT document_id,owner_id,phase,status,attempts,error FROM processing_jobs LIMIT 0",
            "SELECT id,owner_id,plan_id,version,item_id,country,source_id FROM checklist_bindings LIMIT 0",
            "SELECT id,binding_id,url,document_id,status,discovered_from FROM checklist_candidates LIMIT 0",
            "SELECT item_id,binding_id,url,final_url,content_hash,storage_path,size,purpose,warnings,created_at FROM checklist_pages LIMIT 0",
            "SELECT id,owner_id,run_id,status,document_id,candidate_id,url,created_at FROM collection_items LIMIT 0",
            "SELECT id,owner_id,source_id,plan FROM collection_runs LIMIT 0",
        ):
            conn.execute(query)
        if version >= 11:
            conn.execute("SELECT item_id,evidence FROM checklist_page_evidence LIMIT 0")
        if version >= 12:
            conn.execute(
                "SELECT id,item_id,owner_id,status,content_hash,total_size FROM collection_transfers LIMIT 0"
            )
            conn.execute(
                "SELECT transfer_id,start_byte,size,content_hash,storage_path FROM collection_transfer_parts LIMIT 0"
            )
        return version
    except sqlite3.DatabaseError as exc:
        raise InventoryError("archive inventory requires a complete schema 7–12 database") from exc


class _Originals:
    """Walk below the trusted root without following any archive path symlink."""

    def __init__(self, root):
        self.root = root
        self.fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        os.close(self.fd)

    def inspect(self, stored_path, expected_size):
        result = {"relative_path": None, "exists": None, "actual_size": None}
        try:
            if not isinstance(stored_path, str) or not stored_path or "\x00" in stored_path:
                return result, ["invalid_storage_path"]
            path = Path(stored_path)
            relative = path.relative_to(self.root) if path.is_absolute() else path
            if ".." in relative.parts:
                return result, ["outside_data_dir"]
        except ValueError:
            return result, ["outside_data_dir"]
        if not relative.parts:
            return result, ["not_regular_file"]
        result["relative_path"] = relative.as_posix()
        fd = os.dup(self.fd)
        try:
            for index, part in enumerate(relative.parts):
                entry = os.stat(part, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISLNK(entry.st_mode):
                    result["relative_path"] = None
                    return result, ["unsafe_symlink"]
                if index == len(relative.parts) - 1:
                    if not stat.S_ISREG(entry.st_mode):
                        return result, ["not_regular_file"]
                    result.update(exists=True, actual_size=entry.st_size)
                    return result, [] if entry.st_size == expected_size else ["size_mismatch"]
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
        except FileNotFoundError:
            result["exists"] = False
            return result, ["missing_file"]
        except OSError:
            return result, ["path_unavailable"]
        finally:
            os.close(fd)


def _write_jsonl(stream, record):
    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


def _write_document(stream, record):
    _write_jsonl(stream, record)


def _private_file(path, record):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        _write_jsonl(stream, record)
        stream.flush()
        os.fsync(stream.fileno())


class _Partitions:
    """At most one report file is open, independent of account count."""

    def __init__(self, root):
        self.root, self.key, self.stream = root, None, None

    def close(self):
        if self.stream:
            try:
                self.stream.flush()
                os.fsync(self.stream.fileno())
            finally:
                self.stream.close()
                self.stream = None

    def file(self, owner, country, filename):
        key = (_owner_hash(owner), _country_key(country), filename)
        if key != self.key:
            self.close()
            directory = self.root
            for part in key[:2]:
                directory /= part
                if not directory.exists():
                    directory.mkdir(mode=0o700)
                    directory.chmod(0o700)
            for name in FILES:
                try:
                    fd = os.open(
                        directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
                    )
                except FileExistsError:
                    continue
                else:
                    try:
                        os.fchmod(fd, 0o600)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
            fd = os.open(directory / filename, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
            self.stream = os.fdopen(fd, "w", encoding="utf-8")
            self.key = key
        return self.stream


def _count(summary, kind, country, anomalies):
    key = _country_key(country)
    counts = summary["countries"].setdefault(
        key,
        {
            "documents": 0,
            "discovery_pages": 0,
            "relations": 0,
            "ocr_pages": 0,
            "ocr_regions": 0,
            "anomalies": 0,
        },
    )
    summary[kind] += 1
    counts[kind] += 1
    if anomalies:
        summary["anomalies"] += 1
        counts["anomalies"] += 1
        summary["anomaly_counts"].update(set(anomalies))


def _document_metadata(conn, ids):
    marks = ",".join("?" for _ in ids)
    classifications, references = {}, {}
    for row in _rows(
        conn.execute(
            f"SELECT document_id,collection,level,tags,revision,updated_at FROM legal_classifications WHERE document_id IN ({marks}) ORDER BY document_id,collection",
            ids,
        )
    ):
        item = dict(row)
        classifications.setdefault(item.pop("document_id"), []).append(item)
    # Candidate.document_id follows the newest fetch. The item pointer and the
    # frozen run plan retain which original version each collection actually got.
    collected_candidates = set()
    for row in _rows(
        conn.execute(
            f"""WITH collected AS (
            SELECT i.document_id,i.id collection_item_id,i.candidate_id,i.url,i.status,i.created_at,
                r.id run_id,r.source_id,d.owner_id,
                CASE WHEN json_valid(r.plan) THEN r.plan ELSE '{{}}' END plan
            FROM collection_items i JOIN documents d ON d.id=i.document_id AND d.owner_id=i.owner_id
            JOIN collection_runs r ON r.id=i.run_id AND r.owner_id=d.owner_id
            WHERE i.document_id IN ({marks}))
        SELECT c.document_id,c.collection_item_id,c.candidate_id,c.run_id,c.source_id,c.url,c.status,
            json_extract(c.plan,'$.checklist.binding_id') binding_id,
            COALESCE(json_extract(c.plan,'$.checklist.definition.plan.id'),b.plan_id) plan_id,
            COALESCE(json_extract(c.plan,'$.checklist.definition.plan.version'),b.version) plan_version,
            COALESCE(json_extract(c.plan,'$.checklist.definition.item.id'),b.item_id) checklist_item_id
        FROM collected c LEFT JOIN checklist_bindings b
            ON b.id=json_extract(c.plan,'$.checklist.binding_id') AND b.owner_id=c.owner_id
        WHERE json_type(c.plan,'$.checklist')='object'
        ORDER BY c.document_id,c.created_at,c.run_id,c.collection_item_id""",
            ids,
        )
    ):
        item = dict(row)
        document_id = item.pop("document_id")
        item["reference_kind"] = "collection_item"
        references.setdefault(document_id, []).append(item)
        collected_candidates.add((document_id, item["candidate_id"]))
    for row in _rows(
        conn.execute(
            f"""SELECT c.document_id,c.id candidate_id,b.id binding_id,b.plan_id,b.version plan_version,
        b.item_id checklist_item_id,b.source_id,c.url,c.discovered_from,c.status
        FROM checklist_candidates c JOIN checklist_bindings b ON b.id=c.binding_id
        JOIN documents d ON d.id=c.document_id AND d.owner_id=b.owner_id
        WHERE c.document_id IN ({marks}) ORDER BY c.document_id,b.plan_id,b.version,b.item_id,c.id""",
            ids,
        )
    ):
        item = dict(row)
        document_id = item.pop("document_id")
        if (document_id, item["candidate_id"]) not in collected_candidates:
            item.update(reference_kind="candidate", run_id=None, collection_item_id=None)
            references.setdefault(document_id, []).append(item)
    return classifications, references


def _documents(conn, partitions, originals, summary, owner, country):
    where, params = _where(owner, country)
    cursor = conn.execute(
        f"""SELECT d.id,d.owner_id,d.family_id,d.version,d.parent_id,d.case_id,d.title,d.filename,d.role,
        d.status,d.mime_type,d.tags,d.source_url,d.created_at,d.content_hash,d.size,d.storage_path,
        {_COUNTRY} country,m.payload metadata,m.revision metadata_revision,m.reviewed_at,
        j.phase,j.status job_status,j.attempts,(d.error IS NOT NULL OR j.error IS NOT NULL) has_error,
        json_valid(d.manifest) manifest_valid,
        CASE WHEN json_valid(d.manifest) THEN json_array_length(d.manifest,'$.elements') END element_count,
        CASE WHEN json_valid(d.manifest) THEN json_array_length(d.manifest,'$.chunks') END chunk_count,
        CASE WHEN json_valid(d.manifest) THEN json_extract(d.manifest,'$.quality.complete') END text_complete,
        CASE WHEN json_valid(d.manifest) THEN json_extract(d.manifest,'$.quality.page_count') END page_count,
        CASE WHEN json_valid(d.manifest) THEN json_array_length(d.manifest,'$.quality.pending_pages') END pending_pages,
        CASE WHEN json_valid(d.manifest) THEN json_array_length(d.manifest,'$.quality.failed_pages') END failed_pages,
        CASE WHEN json_valid(d.manifest) THEN json_array_length(d.manifest,'$.quality.review_pages') END review_pages,
        CASE WHEN json_valid(d.manifest) THEN
            CASE WHEN json_type(d.manifest,'$.parser')='text' THEN json_extract(d.manifest,'$.parser') END END parser
        FROM {_DOCUMENTS} LEFT JOIN document_metadata m ON m.document_id=d.id
        LEFT JOIN processing_jobs j ON j.document_id=d.id AND j.owner_id=d.owner_id
        WHERE {where} ORDER BY d.owner_id,COALESCE({_COUNTRY},''),d.family_id,d.version,d.id""",
        params,
    )
    for batch in _batches(cursor):
        classifications, references = _document_metadata(conn, [row["id"] for row in batch])
        for row in batch:
            original, anomalies = originals.inspect(row["storage_path"], row["size"])
            tags = _json_value(row["tags"], list, [], anomalies, "tags")
            levels = classifications.get(row["id"], [])
            for item in levels:
                item["tags"] = _json_value(item["tags"], list, [], anomalies, "classification_tags")
            national = next(
                (item for item in levels if item["collection"] == f"country:{row['country']}"), None
            )
            level = (
                national["level"]
                if national
                else (
                    "primary"
                    if not levels or any(item["level"] == "primary" for item in levels)
                    else "secondary"
                )
            )
            if row["role"] == "secondary":
                level = "attachment"
            metadata = (
                _json_value(row["metadata"], dict, {}, anomalies, "metadata") if row["metadata"] else {}
            )
            review = {
                key: metadata.get(key)
                for key in (
                    "kind",
                    "jurisdiction",
                    "language",
                    "review_status",
                    "published_at",
                    "effective_from",
                    "effective_to",
                )
            }
            review.update(
                metadata_present=row["metadata"] is not None,
                revision=row["metadata_revision"] or 0,
                reviewed_at=row["reviewed_at"],
            )
            if not row["manifest_valid"]:
                anomalies.append("invalid_manifest")
            record = {
                "document_id": row["id"],
                "family_id": row["family_id"],
                "version": row["version"],
                "owner_sha256": _owner_hash(row["owner_id"]),
                "country": row["country"],
                "title": row["title"],
                "filename": row["filename"],
                "parent_id": row["parent_id"],
                "case_id": row["case_id"],
                "role": row["role"],
                "legal_level": level,
                "classifications": levels,
                "tags": tags,
                "checklist_refs": references.get(row["id"], []),
                "source_url": row["source_url"],
                "created_at": row["created_at"],
                "status": row["status"],
                "mime_type": row["mime_type"],
                "content_hash": row["content_hash"],
                "size": row["size"],
                "original": original,
                "review": review,
                "parsing": {
                    "status": row["status"],
                    "phase": row["phase"],
                    "job_status": row["job_status"],
                    "attempts": row["attempts"],
                    "element_count": row["element_count"],
                    "chunk_count": row["chunk_count"],
                    "parser": row["parser"],
                    "has_error": bool(row["has_error"]),
                    "complete": None if row["text_complete"] is None else row["text_complete"] == 1,
                    "page_count": row["page_count"],
                    "pending_pages": row["pending_pages"],
                    "failed_pages": row["failed_pages"],
                    "review_pages": row["review_pages"],
                },
                "anomalies": sorted(set(anomalies)),
            }
            _write_document(partitions.file(row["owner_id"], row["country"], "documents.jsonl"), record)
            _count(summary, "documents", row["country"], anomalies)


def _ocr_pages(conn, partitions, originals, summary, owner, country):
    """Only provenance scalars cross this boundary; page text and pixels stay private."""
    where, params = _where(owner, country)
    page = "CASE WHEN p.type='object' THEN p.value ELSE '{}' END"
    cursor = conn.execute(
        f"""SELECT d.id,d.owner_id,d.version,d.content_hash,{_COUNTRY} country,
        json_extract({page},'$.page') page,json_extract({page},'$.printed_page') printed_page,
        json_extract({page},'$.status') status,json_extract({page},'$.source_hash') source_hash,
        json_extract({page},'$.ocr.model') model,json_extract({page},'$.ocr.prompt_version') prompt_version,
        json_extract({page},'$.ocr.image_sha256') image_hash,
        json_extract({page},'$.ocr.image_blob.storage_path') storage_path,
        json_extract({page},'$.ocr.image_blob.size') size
        FROM {_DOCUMENTS}, json_each(CASE WHEN json_valid(d.manifest) THEN d.manifest ELSE '{{}}' END,'$.pages') p
        WHERE {where} AND json_type({page},'$.ocr.image_blob')='object'
        ORDER BY d.owner_id,COALESCE({_COUNTRY},''),d.family_id,d.version,d.id,p.key""",
        params,
    )
    for row in _rows(cursor):
        original, anomalies = originals.inspect(row["storage_path"], row["size"])
        if row["source_hash"] != row["content_hash"]:
            anomalies.append("ocr_source_hash_mismatch")
        record = {
            "document_id": row["id"],
            "document_version": row["version"],
            "owner_sha256": _owner_hash(row["owner_id"]),
            "country": row["country"],
            "source_hash": row["source_hash"],
            "page": row["page"],
            "printed_page": row["printed_page"],
            "status": row["status"],
            "model": row["model"],
            "prompt_version": row["prompt_version"],
            "image_hash": row["image_hash"],
            "size": row["size"],
            "original": original,
            "anomalies": sorted(set(anomalies)),
        }
        _write_jsonl(partitions.file(row["owner_id"], row["country"], "ocr-pages.jsonl"), record)
        _count(summary, "ocr_pages", row["country"], anomalies)


def _ocr_regions(conn, partitions, originals, summary, owner, country):
    """Keep each verification crop mapped to its immutable source; exclude readings."""
    where, params = _where(owner, country)
    page = "CASE WHEN p.type='object' THEN p.value ELSE '{}' END"
    region = "CASE WHEN r.type='object' THEN r.value ELSE '{}' END"
    cursor = conn.execute(
        f"""SELECT d.id,d.owner_id,d.version,d.content_hash,{_COUNTRY} country,
        json_extract({page},'$.page') page,json_extract({page},'$.ocr.model') model,
        json_extract({page},'$.ocr.recovery.status') recovery_status,
        json_extract({region},'$.source_hash') source_hash,
        json_extract({region},'$.region_id') region_id,
        json_extract({region},'$.region_bbox') region_bbox,
        json_extract({region},'$.image_sha256') image_hash,
        json_extract({region},'$.image_blob.content_hash') blob_hash,
        json_extract({region},'$.image_blob.storage_path') storage_path,
        json_extract({region},'$.image_blob.size') size,
        json_extract({region},'$.pass_number') pass_number,
        json_extract({region},'$.reading_hash') reading_hash,
        json_extract({region},'$.render_version') render_version,
        json_extract({region},'$.dpi') dpi
        FROM {_DOCUMENTS},
        json_each(CASE WHEN json_valid(d.manifest) THEN d.manifest ELSE '{{}}' END,'$.pages') p,
        json_each({page},'$.ocr.recovery.regions') r
        WHERE {where} AND r.type='object' AND json_type({region},'$.image_blob')='object'
        ORDER BY d.owner_id,COALESCE({_COUNTRY},''),d.family_id,d.version,d.id,p.key,r.key""",
        params,
    )
    for row in _rows(cursor):
        original, anomalies = originals.inspect(row["storage_path"], row["size"])
        if row["source_hash"] != row["content_hash"]:
            anomalies.append("ocr_source_hash_mismatch")
        if row["image_hash"] != row["blob_hash"]:
            anomalies.append("ocr_image_hash_mismatch")
        bbox = _json_value(row["region_bbox"], list, [], anomalies, "region_bbox")
        if len(bbox) != 4 or any(type(value) not in {int, float} for value in bbox):
            bbox = []
            anomalies.append("invalid_region_bbox")
        record = {
            key: row[key]
            for key in (
                "country",
                "source_hash",
                "page",
                "region_id",
                "model",
                "recovery_status",
                "image_hash",
                "size",
                "pass_number",
                "reading_hash",
                "render_version",
                "dpi",
            )
        }
        record.update(
            document_id=row["id"],
            document_version=row["version"],
            owner_sha256=_owner_hash(row["owner_id"]),
            region_bbox=bbox,
            original=original,
            anomalies=sorted(set(anomalies)),
        )
        _write_jsonl(partitions.file(row["owner_id"], row["country"], "ocr-regions.jsonl"), record)
        _count(summary, "ocr_regions", row["country"], anomalies)


def _page_evidence(raw, anomalies):
    """Export the official adapter's bounded request fields, never arbitrary metadata."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 16384:
        anomalies.append("invalid_discovery_evidence")
        return {}
    value = _json_value(raw, dict, {}, anomalies, "discovery_evidence")
    if not value:
        return {}
    request, scope = value.get("request", {}), value.get("scope", {})
    if not isinstance(request, dict) or not isinstance(scope, dict):
        anomalies.append("invalid_discovery_evidence")
        return {}

    def scalar(value):
        return (
            isinstance(value, str) and len(value) <= 2048 or type(value) is int and 0 <= value <= 1000000000
        )

    body = request.get("json_body", {})
    selected = {}
    if isinstance(body, dict):
        for key in (
            "tentang",
            "p_lihan",
            "jns",
            "thn",
            "status",
            "terx",
            "sortOrder",
            "length",
            "start",
            "no",
            "k",
        ):
            item = body.get(key)
            if scalar(item) or isinstance(item, list) and len(item) <= 20 and all(scalar(v) for v in item):
                selected[key] = item
    result = {"request": {}, "scope": {}}
    if request.get("method") in ("GET", "POST"):
        result["request"]["method"] = request["method"]
    if isinstance(request.get("url"), str) and len(request["url"]) <= 2048:
        result["request"]["url"] = request["url"]
    if selected:
        result["request"]["json_body"] = selected
    for key in ("provider", "jns", "total", "offset", "page_size", "returned"):
        if scalar(scope.get(key)):
            result["scope"][key] = scope[key]
    return result


def _pages(conn, partitions, originals, summary, owner, country):
    where, params = _where(owner, country, "b.owner_id", "b.country")
    evidence = "COALESCE(e.evidence,'{}')" if summary["schema_version"] >= 11 else "'{}'"
    evidence_join = (
        "LEFT JOIN checklist_page_evidence e ON e.item_id=p.item_id"
        if summary["schema_version"] >= 11
        else ""
    )
    cursor = conn.execute(
        f"""SELECT p.*,b.owner_id,b.country,b.plan_id,b.version plan_version,b.item_id checklist_item_id,
        b.source_id,i.run_id,i.status fetch_status,
        CASE WHEN json_valid(p.warnings) THEN json_array_length(p.warnings) END warning_count,
        {evidence} evidence
        FROM {_PAGES} {evidence_join} WHERE {where}
        ORDER BY b.owner_id,b.country,b.plan_id,b.version,b.item_id,p.created_at,p.item_id""",
        params,
    )
    for row in _rows(cursor):
        original, anomalies = originals.inspect(row["storage_path"], row["size"])
        record = {
            key: row[key]
            for key in (
                "country",
                "plan_id",
                "plan_version",
                "checklist_item_id",
                "source_id",
                "run_id",
                "url",
                "final_url",
                "purpose",
                "fetch_status",
                "content_hash",
                "size",
                "created_at",
                "warning_count",
            )
        }
        record.update(
            collection_item_id=row["item_id"],
            binding_id=row["binding_id"],
            owner_sha256=_owner_hash(row["owner_id"]),
            original=original,
            evidence=_page_evidence(row["evidence"], anomalies),
            anomalies=anomalies,
        )
        _write_jsonl(partitions.file(row["owner_id"], row["country"], "discovery-pages.jsonl"), record)
        _count(summary, "discovery_pages", row["country"], anomalies)


def _relations(conn, partitions, summary, owner, country):
    where, params = _where(owner, country)
    cursor = conn.execute(
        f"""SELECT r.id,r.collection,r.relation,r.locator,r.created_at,d.owner_id,
        d.id source_document_id,d.family_id source_family_id,d.version source_version,{_COUNTRY} source_country,
        t.id target_document_id,t.family_id target_family_id,t.version target_version,
        COALESCE(tc.country,CASE WHEN t.role='secondary' THEN tpc.country END) target_country
        FROM {_DOCUMENTS} JOIN legal_relations r ON r.source_document_id=d.id AND r.owner_id=d.owner_id
        LEFT JOIN documents t ON t.id=r.target_document_id AND t.owner_id=d.owner_id
        LEFT JOIN document_countries tc ON tc.document_id=t.id
        LEFT JOIN documents tp ON tp.id=t.parent_id AND tp.owner_id=t.owner_id
        LEFT JOIN document_countries tpc ON tpc.document_id=tp.id
        WHERE {where} ORDER BY d.owner_id,COALESCE({_COUNTRY},''),d.family_id,d.version,d.id,r.created_at,r.id""",
        params,
    )
    for row in _rows(cursor):
        record = dict(row)
        record["owner_sha256"] = _owner_hash(record.pop("owner_id"))
        anomalies = []
        if row["target_document_id"] is None:
            anomalies.append("unresolved_relation_target")
        elif row["source_country"] != row["target_country"]:
            anomalies.append("cross_country_relation")
        record["anomalies"] = anomalies
        _write_jsonl(partitions.file(row["owner_id"], row["source_country"], "relations.jsonl"), record)
        _count(summary, "relations", row["source_country"], anomalies)


def export_inventory(data_dir, *, country=None, owner=None):
    """Publish a catalog and return its Path and summary; no services are started."""
    country, owner = country or None, owner or None
    _country_key(country)
    if owner is not None and not isinstance(owner, str):
        raise InventoryError("owner must be an account identifier")
    temporary = None
    try:
        root = Path(data_dir).expanduser().resolve(strict=True)
        database = root / "tea.sqlite3"
        if not root.is_dir() or not stat.S_ISREG(database.lstat().st_mode):
            raise InventoryError("data directory must contain a regular tea.sqlite3 database")
        # mode=ro respects WAL. immutable=1 would incorrectly ignore an active WAL.
        with closing(
            sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=15, isolation_level=None)
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            schema_version = _validate_schema(conn)
            snapshot_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ-") + uuid4().hex
            summary = {
                "format_version": 3,
                "schema_version": schema_version,
                "snapshot_id": snapshot_id,
                "snapshot_started_at": _now(),
                "completed": False,
                "filters": {"country": country, "owner_sha256": _owner_hash(owner) if owner else None},
                "documents": 0,
                "discovery_pages": 0,
                "relations": 0,
                "ocr_pages": 0,
                "ocr_regions": 0,
                "anomalies": 0,
                "anomaly_counts": Counter(),
                "countries": {},
                "original_check": "path_existence_and_size_only",
                "content_hashes": "database_values_not_recomputed",
                "scope": "stored_records_only",
            }
            dw, dp = _where(owner, country)
            pw, pp = _where(owner, country, "b.owner_id", "b.country")
            summary["owners"] = conn.execute(
                f"SELECT COUNT(DISTINCT owner_id) FROM (SELECT d.owner_id FROM {_DOCUMENTS} WHERE {dw} UNION ALL SELECT b.owner_id FROM {_PAGES} WHERE {pw})",
                (*dp, *pp),
            ).fetchone()[0]
            catalogs = root / "catalogs"
            try:
                catalogs.mkdir(mode=0o700)
            except FileExistsError:
                if not stat.S_ISDIR(catalogs.lstat().st_mode):
                    raise InventoryError("catalogs must be a real directory inside data") from None
            temporary = Path(tempfile.mkdtemp(prefix=".inventory-", dir=catalogs))
            temporary.chmod(0o700)
            with closing(_Partitions(temporary)) as partitions, closing(_Originals(root)) as originals:
                _documents(conn, partitions, originals, summary, owner, country)
                _ocr_pages(conn, partitions, originals, summary, owner, country)
                _ocr_regions(conn, partitions, originals, summary, owner, country)
                _pages(conn, partitions, originals, summary, owner, country)
                _relations(conn, partitions, summary, owner, country)
            conn.rollback()
        summary.update(completed=True, completed_at=_now())
        summary["anomaly_counts"] = dict(sorted(summary["anomaly_counts"].items()))
        summary["countries"] = dict(sorted(summary["countries"].items()))
        _private_file(temporary / "summary.json", summary)
        _private_file(
            temporary / "COMPLETE", {"snapshot_id": snapshot_id, "completed_at": summary["completed_at"]}
        )
        for directory, _, _ in os.walk(temporary, topdown=False):
            _sync_directory(Path(directory))
        destination = catalogs / snapshot_id
        if destination.exists():
            raise InventoryError("catalog snapshot already exists; retry to create a new snapshot")
        temporary.rename(destination)
        temporary = None
        _sync_directory(catalogs)
        return {"path": destination, "summary": summary}
    except FileNotFoundError as exc:
        raise InventoryError("data directory must contain an existing tea.sqlite3 database") from exc
    except (OSError, sqlite3.DatabaseError) as exc:
        raise InventoryError(
            "archive inventory failed while reading the database or writing its private report"
        ) from exc
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export a read-only catalog of stored originals (schema 7–12)"
    )
    parser.add_argument(
        "--data-dir", default=os.getenv("TEA_DATA_DIR") or str(Path(__file__).resolve().parents[2] / "data")
    )
    parser.add_argument("--country", default=os.getenv("TEA_INVENTORY_COUNTRY") or None)
    parser.add_argument("--owner", default=os.getenv("TEA_INVENTORY_OWNER") or None)
    args = parser.parse_args(argv)
    try:
        report = export_inventory(args.data_dir, country=args.country, owner=args.owner)
    except InventoryError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "path": str(report["path"]),
                **{
                    key: report["summary"][key]
                    for key in ("documents", "discovery_pages", "relations", "anomalies")
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
