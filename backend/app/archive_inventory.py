"""Export a private, read-only inventory of schema 7–18 without opening originals.

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
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from .blob_store import _sync_directory
from .ingestion_quality import text_complete
from .native_mapping_inventory import MAX_MANIFEST_BYTES, MAX_RECORD_BYTES, mappings, validate_mapping

BATCH_SIZE = 200
SUPPORTED_SCHEMA_VERSIONS = (7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18)
FILES = (
    "documents.jsonl",
    "discovery-pages.jsonl",
    "catalog-documents.jsonl",
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
    except (TypeError, ValueError, RecursionError):
        anomalies.append(f"invalid_{label}")
        return fallback


def _validate_schema(conn):
    try:
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise InventoryError(
                "archive inventory requires schema 7–18; use the matching application version"
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
            "SELECT id,owner_id FROM sources LIMIT 0",
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
        if version >= 16:
            conn.execute("SELECT user_id,can_login,is_admin,revision FROM user_access LIMIT 0")
        return version
    except sqlite3.DatabaseError as exc:
        raise InventoryError("archive inventory requires a complete schema 7–18 database") from exc


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
            fd = os.open(directory / filename, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
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
            "catalog_documents": 0,
            "catalog_document_gaps": 0,
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


def _treaty_context(row):
    """An adapter name alone cannot expand the private export boundary."""
    from .treaty_room_discovery import catalog_query, purpose

    if row is None:
        return False
    try:
        return (
            row["country"] == "ID"
            and row["plan_id"] == "indonesia-20261003"
            and type(row["plan_version"]) is int
            and row["plan_version"] >= 5
            and row["checklist_item_id"] == "5.1"
            and row["purpose"] == "catalog"
            and row["owner_id"] == row["run_owner_id"]
            and row["owner_id"] == row["source_owner_id"]
            and row["source_id"] == row["run_source_id"]
            and row["url"] == row["item_url"] == row["final_url"]
            and purpose(row["final_url"]) == "catalog"
            and bool(catalog_query(row["final_url"]))
            and re.fullmatch(r"[a-f0-9]{64}", row["content_hash"]) is not None
            and row["storage_path"].endswith(
                f"/files/blobs/{_owner_hash(row['owner_id'])}/"
                f"{row['content_hash'][:2]}/{row['content_hash'][2:4]}/{row['content_hash']}"
            )
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _parse_page_evidence(raw, anomalies, row=None):
    limit = (
        4 * 1024 * 1024
        if row is not None
        and (
            row["evidence_is_treaty"] and _treaty_context(row) or row["evidence_is_wto"] and _wto_context(row)
        )
        else 16384
    )
    if row is not None and row["evidence_is_bkpm"] and _bkpm_context(row):
        limit = 64 * 1024
    try:
        valid_size = isinstance(raw, str) and len(raw.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        valid_size = False
    if not valid_size:
        anomalies.append("invalid_discovery_evidence")
        return {}
    value = _json_value(raw, dict, {}, anomalies, "discovery_evidence")
    scope = value.get("scope", {})
    if not isinstance(scope, dict):
        anomalies.append("invalid_discovery_evidence")
        return {}
    if value and scope.get("provider") == "treaty-room-public":
        request = value.get("request", {})
        if (
            not _treaty_context(row)
            or not isinstance(request, dict)
            or request.get("method") != "GET"
            or request.get("url") != row["final_url"]
        ):
            anomalies.append("invalid_treaty_source_provenance")
            return {}
    if value and scope.get("provider") in _WTO_PROVIDERS:
        request = value.get("request", {})
        if (
            not _wto_context(row)
            or not isinstance(request, dict)
            or request.get("method") != "GET"
            or request.get("url") != row["final_url"]
            or scope.get("member") != row["country"]
        ):
            anomalies.append("invalid_wto_source_provenance")
            return {}
    if value and scope.get("provider") == "bkpm":
        request = value.get("request", {})
        if (
            not _bkpm_context(row)
            or not isinstance(request, dict)
            or request.get("method") != "GET"
            or request.get("url") != row["final_url"]
        ):
            anomalies.append("invalid_bkpm_source_provenance")
            return {}
    return value


def _page_evidence(raw, anomalies, *, value=None):
    """Export the official adapter's bounded request fields, never arbitrary metadata."""
    value = _parse_page_evidence(raw, anomalies) if value is None else value
    if not value:
        return {}
    request, scope = value.get("request", {}), value.get("scope", {})
    if not isinstance(request, dict) or not isinstance(scope, dict):
        anomalies.append("invalid_discovery_evidence")
        return {}

    def scalar(value):
        return (
            isinstance(value, str)
            and len(value) <= 2048
            and not re.search(r"[\x00-\x1f\x7f]", value)
            or type(value) is int
            and 0 <= value <= 1000000000
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
    request_url = request.get("url")
    if isinstance(request_url, str):
        try:
            parsed = urlsplit(request_url)
            keys = {key.lower().replace("-", "_") for key, _ in parse_qsl(parsed.query, max_num_fields=20)}
            valid_url = (
                len(request_url) <= 2048
                and not re.search(r"[\x00-\x1f\x7f]", request_url)
                and parsed.scheme == "https"
                and parsed.hostname
                and not parsed.username
                and not parsed.password
                and not parsed.fragment
                and parsed.port in (None, 443)
                and not keys
                & {
                    "api_key",
                    "apikey",
                    "token",
                    "access_token",
                    "auth",
                    "authorization",
                    "password",
                    "secret",
                    "signature",
                    "credential",
                }
            )
        except ValueError:
            valid_url = False
        if valid_url:
            result["request"]["url"] = request_url
        else:
            anomalies.append("unsafe_discovery_request_url")
    if selected:
        result["request"]["json_body"] = selected
    for key in ("provider", "jns", "total", "offset", "page_size", "returned"):
        if scalar(scope.get(key)):
            result["scope"][key] = scope[key]
    if scope.get("provider") == "bkpm":
        if scope.get("theme_id") == 1:
            result["scope"]["theme_id"] = 1
        documents, returned = scope.get("documents"), scope.get("returned")
        if isinstance(documents, list) and type(returned) is int and 0 <= len(documents) <= returned <= 50:
            result["scope"]["stored_document_count"] = len(documents)
            result["scope"]["returned_rows_without_document_metadata"] = returned - len(documents)
    if scope.get("provider") == "treaty-room-public":
        from .treaty_room_discovery import FILTERS

        for key in ("page", "last_page"):
            if type(scope.get(key)) is int and 1 <= scope[key] <= 20_000:
                result["scope"][key] = scope[key]
        filters = scope.get("filters")
        if (
            isinstance(filters, dict)
            and set(filters) == set(FILTERS)
            and all(
                isinstance(item, str) and len(item) <= 200 and not re.search(r"[\x00-\x1f\x7f]", item)
                for item in filters.values()
            )
        ):
            result["scope"]["filters"] = filters
        for key in ("legal_status_inferred", "exhaustive_content_verified"):
            if scope.get(key) is False:
                result["scope"][key] = False
    if scope.get("provider") in _WTO_PROVIDERS:
        for key in (
            "member",
            "member_name",
            "member_id",
            "schedule",
            "schedule_number",
            "page",
            "family",
            "language",
            "procedure_symbol",
            "source_url",
            "source_sha256",
            "status_filter",
            "protocol_symbol_source",
        ):
            if scalar(scope.get(key)):
                result["scope"][key] = scope[key]
        for key in (
            "legal_status_inferred",
            "exhaustive_content_verified",
            "itip_exports_verified",
            "include_pre_wto",
            "member_bound",
            "bundle_members_verified",
        ):
            if type(scope.get(key)) is bool:
                result["scope"][key] = scope[key]
    return result


_WTO_PROVIDERS = {"wto-goods-public", "wto-docs-public", "wto-goods-bundle-audit"}


def _bkpm_context(row):
    """A larger BKPM page is bound to the exact frozen owned investment recipe."""
    from .discovery import _BKPM_API, _bkpm_query

    if row is None:
        return False
    try:
        parsed = urlsplit(row["final_url"])
        query = _bkpm_query(row["final_url"])
        return (
            row["country"] == "ID"
            and row["plan_id"] == "indonesia-20261003"
            and type(row["plan_version"]) is int
            and row["plan_version"] >= 4
            and row["checklist_item_id"] == "4.1"
            and row["purpose"] == "catalog"
            and row["owner_id"] == row["run_owner_id"] == row["source_owner_id"]
            and row["source_id"] == row["run_source_id"]
            and row["url"] == row["item_url"] == row["final_url"]
            and row["frozen_binding_id"] == row["binding_id"]
            and row["frozen_kind"] == "discovery"
            and row["frozen_country"] == "ID"
            and row["frozen_plan_id"] == row["plan_id"]
            and type(row["frozen_plan_version"]) is int
            and row["frozen_plan_version"] == row["plan_version"]
            and row["frozen_plan_country"] == "ID"
            and row["frozen_item_id"] == "4.1"
            and row["frozen_bkpm_recipe"] == 1
            and parsed.scheme == "https"
            and parsed.netloc == "jdih.bkpm.go.id"
            and parsed.path == urlsplit(_BKPM_API).path
            and not parsed.fragment
            and query["limit"] == "50"
            and re.fullmatch(r"[a-f0-9]{64}", row["content_hash"]) is not None
            and row["storage_path"].endswith(
                f"/files/blobs/{_owner_hash(row['owner_id'])}/"
                f"{row['content_hash'][:2]}/{row['content_hash'][2:4]}/{row['content_hash']}"
            )
        )
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return False


def _bkpm_catalog_documents(row, page_record, value, partitions, summary):
    from .discovery import _BKPM_FILES_HOST, _bkpm_query, _canonical

    pointer = {
        "provider": "bkpm",
        "owner_sha256": page_record["owner_sha256"],
        **{
            key: page_record[key]
            for key in (
                "country",
                "plan_id",
                "plan_version",
                "checklist_item_id",
                "binding_id",
                "source_id",
                "run_id",
            )
        },
        "source_page_id": row["item_id"],
        "source_hash": row["content_hash"],
        "source_url": row["final_url"],
        "source_original": page_record["original"],
        "mapping_record_indexed": False,
        "legal_review_status": "not_reviewed",
        "legal_status_inferred": False,
        "exhaustive_content_verified": False,
    }

    def emit(record, anomalies, valid):
        _write_jsonl(
            partitions.file(row["owner_id"], row["country"], "catalog-documents.jsonl"),
            {**pointer, **record, "anomalies": sorted(set(anomalies))},
        )
        _count(summary, "catalog_documents" if valid else "catalog_document_gaps", row["country"], anomalies)

    scope = value.get("scope", {})
    try:
        query = _bkpm_query(row["final_url"])
        offset = (int(query["page"]) - 1) * 50
        total, returned, documents = scope.get("total"), scope.get("returned"), scope.get("documents")
        if (
            not _bkpm_context(row)
            or page_record["original"]["exists"] is not True
            or page_record["original"]["actual_size"] != row["size"]
            or scope.get("provider") != "bkpm"
            or type(scope.get("theme_id")) is not int
            or scope["theme_id"] != 1
            or type(total) is not int
            or not 0 <= total <= 1_000_000
            or type(scope.get("offset")) is not int
            or scope["offset"] != offset
            or type(scope.get("page_size")) is not int
            or scope["page_size"] != 50
            or type(returned) is not int
            or returned != max(0, min(50, total - offset))
            or not isinstance(documents, list)
            or len(documents) > returned
        ):
            raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError):
        page_record["anomalies"].append("invalid_bkpm_catalog_page")
        emit({"export_status": "invalid_page"}, ["invalid_bkpm_catalog_page"], False)
        return
    seen = set()
    for index, document in enumerate(documents):
        anomalies = list(page_record["anomalies"])
        try:
            if not isinstance(document, dict):
                raise ValueError
            identity = document.get("identity_key")
            match = re.fullmatch(r"bkpm:([1-9]\d{0,6})", identity) if isinstance(identity, str) else None
            if not match or not 0 < int(match[1]) <= 1_000_000 or identity in seen:
                raise ValueError
            seen.add(identity)
            title, target = document.get("title"), document.get("original_url")
            original, host = _canonical(target)
            parsed = urlsplit(original)
            if (
                not title
                or not _catalog_text(title, 1000)
                or host != _BKPM_FILES_HOST
                or original != target
                or parsed.query
                or parsed.fragment
                or re.fullmatch(r"/jdih/(?:jdih|userfiles)/[^?]+\.pdf", parsed.path, re.I) is None
            ):
                raise ValueError
            metadata = {}
            for key in (
                "regulation_number",
                "regulation_type",
                "establishment_date",
                "publication_date",
                "status",
            ):
                if key in document:
                    if not _catalog_text(document[key], 200) or not isinstance(document[key], str):
                        raise ValueError
                    metadata[key] = document[key]
            detail = document.get("detail_url")
            if detail is not None:
                if (
                    not isinstance(detail, str)
                    or len(detail) > 550
                    or re.fullmatch(
                        r"https://jdih\.bkpm\.go\.id/id/document/[a-z0-9]+(?:-[a-z0-9]+)*", detail
                    )
                    is None
                ):
                    raise ValueError
                metadata["detail_url"] = detail
            record = {
                "identity_key": identity,
                "bkpm_document_id": int(match[1]),
                "title": title,
                "original_url": original,
                "source_metadata": metadata,
                "source_record_sha256": hashlib.sha256(
                    json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest(),
            }
        except (TypeError, ValueError, RecursionError):
            emit(
                {"export_status": "invalid_record", "source_row_index": index},
                [*anomalies, "invalid_bkpm_catalog_record"],
                False,
            )
        else:
            emit({**record, "export_status": "exported", "source_row_index": index}, anomalies, True)


def _wto_context(row):
    """Only the two frozen WTO items can carry larger public source evidence."""
    from .wto_discovery import purpose

    if row is None:
        return False
    try:
        country, plan, code, minimum = {
            "ID": ("ID", "indonesia-20261003", "5.4", 6),
            "SG": ("SG", "singapore-demo-20261009", "5.2", 3),
        }[row["country"]]
        return (
            row["plan_id"] == plan
            and type(row["plan_version"]) is int
            and row["plan_version"] >= minimum
            and row["checklist_item_id"] == code
            and row["purpose"] in {"catalog", "detail"}
            and row["owner_id"] == row["run_owner_id"] == row["source_owner_id"]
            and row["source_id"] == row["run_source_id"]
            and row["url"] == row["item_url"] == row["final_url"]
            and purpose(row["final_url"], country) == row["purpose"]
            and re.fullmatch(r"[a-f0-9]{64}", row["content_hash"]) is not None
            and row["storage_path"].endswith(
                f"/files/blobs/{_owner_hash(row['owner_id'])}/"
                f"{row['content_hash'][:2]}/{row['content_hash'][2:4]}/{row['content_hash']}"
            )
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _wto_document(document, scope, country, anomalies):
    from .discovery import _canonical
    from .wto_discovery import GOODS, MEMBERS, filename_matches, original_filename, purpose

    if not isinstance(document, dict) or len(json.dumps(document, ensure_ascii=True)) > 32 * 1024:
        raise ValueError("invalid_wto_catalog_record")
    info = MEMBERS[country]
    if document.get("legal_review_status") != "not_reviewed":
        raise ValueError("invalid_wto_catalog_record")
    if scope["provider"] == "wto-goods-public":
        if (
            document.get("schedule") != info["schedule"]
            or document.get("schedule_number") != info["number"]
            or type(document.get("schedule_number")) is not int
        ):
            raise ValueError("invalid_wto_catalog_record")
        if "file_browser_urls" in document:
            symbol, urls = document.get("procedure_symbol"), document.get("file_browser_urls")
            if (
                not isinstance(symbol, str)
                or not symbol
                or not _catalog_text(symbol, 300)
                or document.get("identity_key") != f"wto-goods:{country}:procedure:{symbol}"
                or document.get("member_id") != info["node"]
                or type(document.get("member_id")) is not int
                or not isinstance(urls, list)
                or len(urls) > 10
                or not all(
                    isinstance(url, str)
                    and purpose(url, country) == "detail"
                    and urlsplit(url).hostname == GOODS
                    for url in urls
                )
                or document.get("original_policy") != ("public_file_browser" if urls else "catalog_only")
                or document.get("missing_original") is not (not urls)
                or any(
                    not isinstance(document.get(key), str) or not _catalog_text(document[key], 1000)
                    for key in ("status", "procedure_type", "last_date", "certifications")
                )
            ):
                raise ValueError("invalid_wto_catalog_record")
            keys = {
                "identity_key",
                "procedure_symbol",
                "schedule",
                "schedule_number",
                "member_id",
                "status",
                "procedure_type",
                "last_date",
                "certifications",
                "file_browser_urls",
                "original_policy",
                "missing_original",
                "legal_review_status",
            }
        else:
            target, dates = document.get("original_url"), document.get("document_dates")
            if not isinstance(target, str) or len(target) > 2048:
                raise ValueError("invalid_wto_catalog_record")
            canonical, host = _canonical(target)
            accepted = purpose(target, country) == "document"
            if (
                target != canonical
                or host != GOODS
                or urlsplit(target).query
                or not urlsplit(target).path.startswith("/system/files/")
                or document.get("identity_key")
                != f"wto-goods:{country}:file:{hashlib.sha256(target.encode()).hexdigest()}"
                or document.get("procedure_symbol") != scope.get("procedure_symbol")
                or document.get("source_format") != urlsplit(target).path.rsplit(".", 1)[-1].lower()
                or document.get("original_policy")
                != ("public_original" if accepted else "unsupported_public_path")
                or document.get("representation_equivalence_verified") is not False
                or not isinstance(dates, list)
                or len(dates) > 20
                or not all(isinstance(date, str) and _catalog_text(date, 100) for date in dates)
                or any(
                    not isinstance(document.get(key), str) or not _catalog_text(document[key], limit)
                    for key, limit in (("filename", 240), ("language", 200), ("collection", 1000))
                )
            ):
                raise ValueError("invalid_wto_catalog_record")
            keys = {
                "identity_key",
                "original_url",
                "filename",
                "language",
                "collection",
                "procedure_symbol",
                "document_dates",
                "schedule",
                "schedule_number",
                "source_format",
                "original_policy",
                "legal_review_status",
                "representation_equivalence_verified",
            }
        record = {key: document[key] for key in keys}
    else:
        metadata, originals = document.get("source_metadata"), document.get("originals")
        if not isinstance(metadata, dict) or not isinstance(originals, list) or len(originals) > 2:
            raise ValueError("invalid_wto_catalog_record")
        safe_fields = {
            "CATID",
            "SYMBOL",
            "CATTITLE",
            "CONCERNEDCOUNTRIES",
            "COLLECTION",
            "ISSUINGDATE",
            "RESTRICTIONTYPENAME",
            "HasAccess",
            "FILENAMESA",
            "FILENAMESB",
            "FILENAMESC",
        }
        selected = {key: metadata[key] for key in safe_fields if key in metadata}
        if set(metadata) - safe_fields:
            anomalies.append("wto_metadata_fields_omitted")
        identifier, symbol = metadata.get("CATID"), metadata.get("SYMBOL")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[1-9]\d{0,9}", identifier)
            or document.get("identity_key") != f"wto-docs:{identifier}"
            or not isinstance(symbol, str)
            or not symbol
            or not _catalog_text(symbol, 300)
            or document.get("symbol") != symbol
            or document.get("title") != metadata.get("CATTITLE", "")[:1000]
            or any(
                not isinstance(value, str) or not _catalog_text(value, 20_000) for value in selected.values()
            )
            or document.get("original_policy")
            != ("public_original" if metadata.get("HasAccess") == "True" else "catalog_only")
            or document.get("missing_original")
            is not (not (metadata.get("HasAccess") == "True" and originals))
        ):
            raise ValueError("invalid_wto_catalog_record")
        if scope.get("member_bound") is True and (
            metadata.get("CONCERNEDCOUNTRIES") != info["name"]
            or metadata.get("COLLECTION") != "SCHD"
            or not re.fullmatch(
                re.escape(scope["family"])
                + r"(?:/(?:Suppl\.[1-9]\d{0,2}|Rev\.[1-9]\d{0,2}|Corr\.[1-9]\d{0,2}))*",
                symbol,
            )
        ):
            raise ValueError("invalid_wto_catalog_record")
        if scope.get("member_bound") is False and scope["family"] not in symbol.split("#"):
            raise ValueError("invalid_wto_catalog_record")
        seen = set()
        for original in originals:
            if not isinstance(original, dict) or set(original) != {"slot", "original_url", "filename"}:
                raise ValueError("invalid_wto_catalog_record")
            slot, target = original["slot"], original["original_url"]
            if (
                slot not in {"FILENAMESA", "FILENAMESB"}
                or slot in seen
                or original["filename"] != metadata.get(slot)
                or original_filename(target, country) != original["filename"].upper()
                or not filename_matches(original["filename"], symbol, scope["family"], country)
            ):
                raise ValueError("invalid_wto_catalog_record")
            seen.add(slot)
        expected = {
            slot
            for slot in ("FILENAMESA", "FILENAMESB")
            if filename_matches(metadata.get(slot), symbol, scope["family"], country)
        }
        if seen != expected:
            raise ValueError("invalid_wto_catalog_record")
        keys = {
            "identity_key",
            "title",
            "symbol",
            "source_metadata",
            "originals",
            "original_policy",
            "missing_original",
            "legal_review_status",
        }
        record = {key: document[key] for key in keys - {"source_metadata"}} | {"source_metadata": selected}
    if set(document) - keys:
        anomalies.append("wto_metadata_fields_omitted")
    record["source_record_sha256"] = hashlib.sha256(
        json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return record


def _wto_catalog_documents(row, page_record, value, partitions, summary):
    from .wto_discovery import DOCS, GOODS, MEMBERS, PROTOCOL_SOURCES, catalog_family

    scope, country = value.get("scope", {}), row["country"]
    pointer = {
        "provider": scope.get("provider")
        if scope.get("provider") in _WTO_PROVIDERS
        else "wto-public-unverified",
        "owner_sha256": page_record["owner_sha256"],
        **{
            key: page_record[key]
            for key in (
                "country",
                "plan_id",
                "plan_version",
                "checklist_item_id",
                "binding_id",
                "source_id",
                "run_id",
            )
        },
        "source_page_id": row["item_id"],
        "source_hash": row["content_hash"],
        "source_url": row["final_url"],
        "source_original": page_record["original"],
        "retrieval_eligible": False,
    }

    def emit(record, anomalies, *, valid):
        _write_jsonl(
            partitions.file(row["owner_id"], country, "catalog-documents.jsonl"),
            {**pointer, **record, "anomalies": sorted(set(anomalies))},
        )
        _count(summary, "catalog_documents" if valid else "catalog_document_gaps", country, anomalies)

    try:
        info = MEMBERS[country]
        if (
            not _wto_context(row)
            or scope.get("member") != country
            or scope.get("legal_status_inferred") is not False
            or scope.get("exhaustive_content_verified") is not False
        ):
            raise ValueError
        if scope.get("provider") == "wto-goods-bundle-audit":
            if (
                scope.get("original_url") != row["final_url"]
                or scope.get("member_id") != info["node"]
                or type(scope.get("member_id")) is not int
                or scope.get("bundle_members_verified") is not False
                or scope.get("documents") != []
            ):
                raise ValueError
            return  # The raw ZIP remains a discovery-page audit asset.
        documents = scope.get("documents")
        if (
            scope.get("source_url") != row["final_url"]
            or scope.get("source_sha256") != row["content_hash"]
            or scope.get("member_name") != info["name"]
            or scope.get("itip_exports_verified") is not False
            or not isinstance(documents, list)
            or len(documents) > 1000
        ):
            raise ValueError
        parsed = urlsplit(row["final_url"])
        if scope.get("provider") == "wto-goods-public":
            if (
                parsed.hostname != GOODS
                or scope.get("schedule") != info["schedule"]
                or scope.get("schedule_number") != info["number"]
                or type(scope.get("schedule_number")) is not int
            ):
                raise ValueError
            if row["purpose"] == "catalog":
                total, page = scope.get("total"), scope.get("page")
                query = dict(parse_qsl(parsed.query, keep_blank_values=True))
                if (
                    type(total) is not int
                    or not 0 <= total <= 20_000
                    or type(page) is not int
                    or page != int(query.get("page", "0"))
                    or scope.get("page_size") != 10
                    or type(scope.get("page_size")) is not int
                    or type(scope.get("returned")) is not int
                    or scope.get("returned") != len(documents)
                    or len(documents) != max(0, min(10, total - page * 10))
                    or scope.get("status_filter") != "All"
                    or scope.get("include_pre_wto") is not True
                    or scope.get("member_id") != info["node"]
                ):
                    raise ValueError
            elif not isinstance(scope.get("procedure_symbol"), str) or not _catalog_text(
                scope["procedure_symbol"], 300
            ):
                raise ValueError
        elif scope.get("provider") == "wto-docs-public":
            family = catalog_family(row["final_url"], country)
            if (
                parsed.hostname != DOCS
                or family != scope.get("family")
                or scope.get("language") != "English"
                or scope.get("member_bound") is not (family not in PROTOCOL_SOURCES)
                or scope.get("protocol_symbol_source") != PROTOCOL_SOURCES.get(family)
                or type(scope.get("total")) is not int
                or scope["total"] != len(documents)
                or type(scope.get("returned")) is not int
                or scope.get("returned") != len(documents)
                or len(documents) > 100
            ):
                raise ValueError
        else:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        page_record["anomalies"].append("invalid_wto_catalog_page")
        emit({"export_status": "invalid_page"}, ["invalid_wto_catalog_page"], valid=False)
        return
    seen = set()
    for index, document in enumerate(documents):
        anomalies = list(page_record["anomalies"])
        try:
            record = _wto_document(document, scope, country, anomalies)
            if scope["provider"] == "wto-docs-public" or row["purpose"] == "catalog":
                if record["identity_key"] in seen:
                    raise ValueError("duplicate_wto_catalog_record")
                seen.add(record["identity_key"])
        except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
            emit(
                {"export_status": "invalid_record", "source_row_index": index},
                [*anomalies, "invalid_wto_catalog_record"],
                valid=False,
            )
        else:
            emit({**record, "export_status": "exported", "source_row_index": index}, anomalies, valid=True)


def _catalog_text(value, limit):
    return value is None or (
        isinstance(value, str)
        and len(value) <= limit
        and not re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff]", value)
    )


def _treaty_document(document, anomalies):
    """Project known public fields; keep the stored classification unchanged."""
    from .treaty_room_discovery import MAX_ID, _PRIORITIES, original_url, title_text

    if not isinstance(document, dict) or len(json.dumps(document, ensure_ascii=True)) > 32 * 1024:
        raise ValueError("invalid_treaty_catalog_record")
    identifier, metadata = document.get("treaty_id"), document.get("source_metadata")
    if (
        type(identifier) is not int
        or not 0 < identifier <= MAX_ID
        or document.get("identity_key") != f"treaty-room:{identifier}"
        or not isinstance(metadata, dict)
        or metadata.get("id_perjanjian") != identifier
        or type(metadata.get("id_perjanjian")) is not int
        or not isinstance(metadata.get("judul"), str)
        or not _catalog_text(metadata["judul"], 20_000)
        or not metadata["judul"]
        or document.get("title") != title_text(metadata["judul"])[:1000]
    ):
        raise ValueError("invalid_treaty_catalog_record")
    known = {"id_perjanjian", "judul", "mulaiBerlaku", "masaBerlaku", "berlakuSetelah", "catatan", "file"}
    selected = {key: metadata[key] for key in known if key in metadata}
    if set(metadata) - known:
        anomalies.append("treaty_metadata_fields_omitted")
    if any(not _catalog_text(metadata.get(key), 4096) for key in known - {"id_perjanjian", "judul", "file"}):
        raise ValueError("invalid_treaty_catalog_record")
    files = metadata.get("file")
    slots = ("file", "file2", "file3", "file4")
    if isinstance(files, dict):
        file_keys = {"id_konten", "teks_perjanjian", *slots}
        if (
            type(files.get("id_konten")) is not int
            or not 0 < files["id_konten"] <= MAX_ID
            or not _catalog_text(files.get("teks_perjanjian"), 4096)
            or any(not _catalog_text(files.get(key), 180) for key in slots)
        ):
            raise ValueError("invalid_treaty_catalog_record")
        selected["file"] = {key: files[key] for key in file_keys if key in files}
        if set(files) - file_keys:
            anomalies.append("treaty_metadata_fields_omitted")
    elif files is not None and files != []:
        raise ValueError("invalid_treaty_catalog_record")
    topics, missing, invalid, originals = (
        document.get(key)
        for key in ("priority_topics", "missing_verified_fields", "invalid_file_slots", "originals")
    )
    missing_fields = {
        "contracting_parties",
        "signing_date",
        "international_effective_date",
        "indonesia_effective_date",
        "legal_status",
        "instrument_type",
        "language",
        "legal_force",
    }
    for values, allowed in (
        (topics, set(_PRIORITIES) | {"asean_agreement"}),
        (missing, missing_fields),
        (invalid, set(slots)),
    ):
        if (
            not isinstance(values, list)
            or not all(isinstance(v, str) for v in values)
            or len(values) != len(set(values))
            or set(values) - allowed
        ):
            raise ValueError("invalid_treaty_catalog_record")
    if (
        document.get("original_policy") != ("priority_legal_topic" if topics else "catalog_only")
        or document.get("legal_review_status") != "not_reviewed"
        or not isinstance(originals, list)
        or len(originals) > 4
    ):
        raise ValueError("invalid_treaty_catalog_record")
    seen = set()
    for original in originals:
        if not isinstance(original, dict) or set(original) != {"slot", "original_url", "identity_key"}:
            raise ValueError("invalid_treaty_catalog_record")
        slot = original.get("slot")
        if slot not in slots or slot in seen or not isinstance(files, dict):
            raise ValueError("invalid_treaty_catalog_record")
        if (
            original.get("original_url") != original_url(identifier, files["id_konten"], files.get(slot))
            or original.get("identity_key") != f"treaty-room:{identifier}:{slot}"
        ):
            raise ValueError("invalid_treaty_catalog_record")
        seen.add(slot)
    expected, invalid_slots = set(), set()
    if isinstance(files, dict):
        for slot in slots:
            if files.get(slot) in (None, ""):
                continue
            try:
                original_url(identifier, files["id_konten"], files[slot])
            except ValueError:
                invalid_slots.add(slot)
            else:
                expected.add(slot)
    if seen != expected or set(invalid) != invalid_slots:
        raise ValueError("invalid_treaty_catalog_record")
    return {
        **{
            key: document[key]
            for key in (
                "identity_key",
                "treaty_id",
                "title",
                "priority_topics",
                "original_policy",
                "missing_verified_fields",
                "invalid_file_slots",
                "legal_review_status",
                "originals",
            )
        },
        "source_metadata": selected,
        "source_record_sha256": hashlib.sha256(
            json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def _catalog_documents(row, page_record, value, partitions, summary):
    from .treaty_room_discovery import FILTERS, MAX_ID, MAX_TOTAL, catalog_query

    pointer = {
        "provider": "treaty-room-public",
        "owner_sha256": page_record["owner_sha256"],
        **{
            key: page_record[key]
            for key in (
                "country",
                "plan_id",
                "plan_version",
                "checklist_item_id",
                "binding_id",
                "source_id",
                "run_id",
            )
        },
        "source_page_id": row["item_id"],
        "source_hash": row["content_hash"],
        "source_url": row["final_url"],
        "source_original": page_record["original"],
        "retrieval_eligible": False,
    }

    def emit(record, anomalies, *, valid):
        _write_jsonl(
            partitions.file(row["owner_id"], row["country"], "catalog-documents.jsonl"),
            {
                **pointer,
                **record,
                "anomalies": sorted(set(anomalies)),
            },
        )
        _count(summary, "catalog_documents" if valid else "catalog_document_gaps", row["country"], anomalies)

    scope = value.get("scope", {})
    try:
        query = catalog_query(row["final_url"])
        page, limit = int(query["page"]), int(query["limit"])
        total, documents = scope.get("total"), scope.get("documents")
        if (
            not _treaty_context(row)
            or scope.get("provider") != "treaty-room-public"
            or scope.get("filters") != {key: query[key] for key in FILTERS}
            or type(total) is not int
            or not 0 <= total <= MAX_TOTAL
            or type(scope.get("page")) is not int
            or scope["page"] != page
            or type(scope.get("page_size")) is not int
            or scope["page_size"] != limit
            or type(scope.get("last_page")) is not int
            or scope["last_page"] != max(1, (total + limit - 1) // limit)
            or page > scope["last_page"]
            or type(scope.get("offset")) is not int
            or scope["offset"] != (page - 1) * limit
            or type(scope.get("returned")) is not int
            or scope["returned"] != max(0, min(limit, total - scope["offset"]))
            or not isinstance(documents, list)
            or len(documents) != scope["returned"]
            or len(documents) > 40
            or scope.get("legal_status_inferred") is not False
            or scope.get("exhaustive_content_verified") is not False
        ):
            raise ValueError
    except (TypeError, ValueError):
        page_record["anomalies"].append("invalid_treaty_catalog_page")
        emit({"export_status": "invalid_page"}, ["invalid_treaty_catalog_page"], valid=False)
        return
    seen = set()
    for index, document in enumerate(documents):
        anomalies = list(page_record["anomalies"])
        try:
            record = _treaty_document(document, anomalies)
            if record["treaty_id"] in seen:
                raise ValueError("duplicate_treaty_catalog_record")
            seen.add(record["treaty_id"])
        except (TypeError, ValueError, RecursionError):
            identifier = document.get("treaty_id") if isinstance(document, dict) else None
            if type(identifier) is not int or not 0 < identifier <= MAX_ID:
                identifier = None
            emit(
                {"export_status": "invalid_record", "source_row_index": index, "treaty_id": identifier},
                [*anomalies, "invalid_treaty_catalog_record"],
                valid=False,
            )
        else:
            emit({**record, "export_status": "exported", "source_row_index": index}, anomalies, valid=True)


def _pages(conn, partitions, originals, summary, owner, country):
    where, params = _where(owner, country, "b.owner_id", "b.country")
    evidence = (
        "CASE WHEN length(CAST(e.evidence AS BLOB))<=4194304 THEN COALESCE(e.evidence,'{}') END"
        if summary["schema_version"] >= 11
        else "'{}'"
    )
    is_treaty = (
        "CASE WHEN json_valid(e.evidence) THEN json_extract(e.evidence,'$.scope.provider')='treaty-room-public' ELSE 0 END"
        if summary["schema_version"] >= 11
        else "0"
    )
    is_wto = (
        "CASE WHEN json_valid(e.evidence) THEN json_extract(e.evidence,'$.scope.provider') "
        "IN ('wto-goods-public','wto-docs-public','wto-goods-bundle-audit') ELSE 0 END"
        if summary["schema_version"] >= 11
        else "0"
    )
    is_bkpm = (
        "CASE WHEN json_valid(e.evidence) THEN json_extract(e.evidence,'$.scope.provider')='bkpm' ELSE 0 END"
        if summary["schema_version"] >= 11
        else "0"
    )
    plan = "CASE WHEN json_valid(r.plan) THEN r.plan ELSE '{}' END"
    evidence_join = (
        "LEFT JOIN checklist_page_evidence e ON e.item_id=p.item_id"
        if summary["schema_version"] >= 11
        else ""
    )
    cursor = conn.execute(
        f"""SELECT p.*,b.owner_id,b.country,b.plan_id,b.version plan_version,b.item_id checklist_item_id,
        b.source_id,i.run_id,i.status fetch_status,i.url item_url,r.owner_id run_owner_id,r.source_id run_source_id,
        s.owner_id source_owner_id,
        CASE WHEN json_valid(p.warnings) THEN json_array_length(p.warnings) END warning_count,
        {evidence} evidence,{is_treaty} evidence_is_treaty,{is_wto} evidence_is_wto,{is_bkpm} evidence_is_bkpm,
        json_extract({plan},'$.checklist.binding_id') frozen_binding_id,
        json_extract({plan},'$.checklist.kind') frozen_kind,
        json_extract({plan},'$.checklist.country') frozen_country,
        json_extract({plan},'$.checklist.definition.plan.id') frozen_plan_id,
        json_extract({plan},'$.checklist.definition.plan.version') frozen_plan_version,
        json_extract({plan},'$.checklist.definition.plan.country') frozen_plan_country,
        json_extract({plan},'$.checklist.definition.item.id') frozen_item_id,
        (json_type({plan},'$.checklist.recipe.bkpm_investment')='true') frozen_bkpm_recipe
        FROM {_PAGES} JOIN collection_runs r ON r.id=i.run_id
        LEFT JOIN sources s ON s.id=b.source_id {evidence_join} WHERE {where}
        ORDER BY b.owner_id,b.country,b.plan_id,b.version,b.item_id,p.created_at,p.item_id""",
        params,
    )
    # A validated provider can carry a 4 MiB page; keep only one page in memory.
    while (row := cursor.fetchone()) is not None:
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
        value = _parse_page_evidence(row["evidence"], anomalies, row)
        record.update(
            collection_item_id=row["item_id"],
            binding_id=row["binding_id"],
            owner_sha256=_owner_hash(row["owner_id"]),
            original=original,
            evidence=_page_evidence(row["evidence"], anomalies, value=value),
            anomalies=anomalies,
        )
        if row["evidence_is_treaty"]:
            _catalog_documents(row, record, value, partitions, summary)
        if row["evidence_is_wto"]:
            _wto_catalog_documents(row, record, value, partitions, summary)
        if row["evidence_is_bkpm"]:
            _bkpm_catalog_documents(row, record, value, partitions, summary)
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


def _texts(conn, partitions, summary, owner, country):
    """One immutable original at a time; secondary legal text stays outside RAG."""
    where, params = _where(owner, country)
    cursor = conn.execute(
        f"""SELECT d.id,d.owner_id,d.version,d.content_hash,d.source_url,d.filename,{_COUNTRY} country,
        l.level,CASE WHEN length(CAST(d.manifest AS BLOB))<={MAX_MANIFEST_BYTES}
            THEN d.manifest END manifest,
        json_extract(m.payload,'$.review_status') review_status
        FROM {_DOCUMENTS}
        LEFT JOIN legal_classifications l ON l.document_id=d.id AND l.collection='country:'||dc.country
        LEFT JOIN document_metadata m ON m.document_id=d.id
        WHERE {where} AND d.role='primary' ORDER BY d.id""",
        params,
    )
    while (row := cursor.fetchone()) is not None:
        anomalies = []
        manifest = _json_value(row["manifest"], dict, {}, anomalies, "manifest")
        elements, chunks = manifest.get("elements", []), manifest.get("chunks", [])
        if not isinstance(elements, list) or not isinstance(chunks, list):
            elements, chunks = [], []
            anomalies.append("invalid_manifest_structure")
        quality = manifest.get("quality")
        try:
            complete = bool(chunks) and not anomalies and text_complete(manifest)
        except (TypeError, KeyError, AttributeError):
            complete = False
        record = {
            "document_id": row["id"],
            "version": row["version"],
            "source_hash": row["content_hash"],
            "country": row["country"],
            "legal_level": row["level"],
            "source_url": row["source_url"],
            "filename": row["filename"],
            "review_status": row["review_status"],
            "text_complete": complete,
            "quality": quality,
            "elements": elements,
            "chunks": [
                {key: chunk.get(key) for key in ("id", "text", "locator", "section_path")}
                for chunk in chunks
                if isinstance(chunk, dict)
            ],
            "anomalies": sorted(set(anomalies)),
        }
        _write_jsonl(partitions.file(row["owner_id"], row["country"], "texts.jsonl"), record)
        summary["text_documents"] += 1
        summary["complete_text_documents"] += int(complete)


def _mapping_manifest(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate manifest field")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError("non-finite manifest value")

    value = json.loads(raw, object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("invalid manifest")
    return value


def _source_mappings(conn, partitions, originals, summary, owner, country):
    """Stream explicit mapping records; attachments never expose their manifest."""
    where, params = _where(owner, country)
    cursor = conn.execute(
        f"""SELECT d.id,d.owner_id,d.version,d.content_hash,d.size,d.storage_path,d.role,
        d.status,{_COUNTRY} country,l.level,
        CASE WHEN d.role='primary' THEN length(CAST(d.manifest AS BLOB)) END manifest_size,
        CASE WHEN d.role='primary' AND length(CAST(d.manifest AS BLOB))<={MAX_MANIFEST_BYTES}
            THEN d.manifest END manifest
        FROM {_DOCUMENTS} LEFT JOIN legal_classifications l
            ON l.document_id=d.id AND l.collection='country:'||dc.country
        WHERE {where} ORDER BY d.owner_id,COALESCE({_COUNTRY},''),d.id""",
        params,
    )
    while (row := cursor.fetchone()) is not None:
        original, anomalies = originals.inspect(row["storage_path"], row["size"])
        pointer = {
            "document_id": row["id"],
            "document_version": row["version"],
            "owner_sha256": _owner_hash(row["owner_id"]),
            "country": row["country"],
            "source_hash": row["content_hash"],
            "role": row["role"],
            "legal_level": "attachment" if row["role"] != "primary" else row["level"],
            "mapping_record_indexed": False,
        }
        stream = partitions.file(row["owner_id"], row["country"], "source-mapping.jsonl")
        count, gaps = 0, 0

        def emit(kind, value, json_pointer=None):
            nonlocal count, gaps
            record = {**pointer, "record_kind": kind, "manifest_pointer": json_pointer, **value}
            encoded = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) > MAX_RECORD_BYTES:
                record = {
                    **pointer,
                    "record_kind": "mapping_gap",
                    "manifest_pointer": json_pointer,
                    "anomalies": ["mapping_record_exceeds_limit"],
                }
                gaps += 1
            _write_jsonl(stream, record)
            count += 1

        start = {
            "original": original,
            "status": row["status"],
            "manifest_size": row["manifest_size"],
            "text_complete": None,
            "projection_complete": False,
            "anomalies": anomalies,
        }
        if row["role"] != "primary":
            start["mapping_status"] = "attachment_original_only"
            emit("document", start)
        elif row["manifest"] is None:
            start["mapping_status"] = "manifest_unavailable"
            start["anomalies"] = [*anomalies, "manifest_missing_or_exceeds_limit"]
            emit("document", start)
            gaps += 1
        else:
            try:
                raw = row["manifest"].encode("utf-8")
                manifest_hash = hashlib.sha256(raw).hexdigest()
                manifest = _mapping_manifest(row["manifest"])
                if manifest.get("source_hash", row["content_hash"]) != row["content_hash"]:
                    raise ValueError("manifest source mismatch")
                pointer["manifest_sha256"] = manifest_hash
                start.update(
                    mapping_status="stored_manifest_projection",
                    text_complete=bool(manifest.get("chunks")) and text_complete(manifest),
                )
                emit("document", start)
                for kind, path, mapping in mappings(manifest, manifest_hash):
                    validate_mapping(mapping, row["content_hash"])
                    emit(kind, {"mapping": mapping}, path)
            except (TypeError, ValueError, AttributeError, KeyError, RecursionError, UnicodeError):
                if count == 0:
                    start["mapping_status"] = "invalid_manifest"
                    emit("document", start)
                emit("mapping_gap", {"anomalies": ["invalid_native_mapping_manifest"]})
                gaps += 1
        emit(
            "document_end",
            {
                "projection_complete": row["role"] == "primary" and not gaps,
                "mapping_gaps": gaps,
                "record_count_before_end": count,
            },
        )
        summary["source_mapping_documents"] += 1
        summary["source_mapping_records"] += count
        summary["source_mapping_gaps"] += gaps


def export_inventory(data_dir, *, country=None, owner=None, include_text=False, include_source_mapping=False):
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
                "format_version": 5 if include_source_mapping else 4,
                "schema_version": schema_version,
                "snapshot_id": snapshot_id,
                "snapshot_started_at": _now(),
                "completed": False,
                "filters": {"country": country, "owner_sha256": _owner_hash(owner) if owner else None},
                "documents": 0,
                "discovery_pages": 0,
                "catalog_documents": 0,
                "catalog_document_gaps": 0,
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
            if include_text:
                summary.update(text_documents=0, complete_text_documents=0)
            if include_source_mapping:
                summary.update(
                    source_mapping_documents=0,
                    source_mapping_records=0,
                    source_mapping_gaps=0,
                    source_mapping_scope="stored_manifest_allowlist_only_not_legal_completeness",
                    source_mapping_manifest_limit=MAX_MANIFEST_BYTES,
                    source_mapping_record_limit=MAX_RECORD_BYTES,
                )
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
                if include_text:
                    _texts(conn, partitions, summary, owner, country)
                if include_source_mapping:
                    _source_mappings(conn, partitions, originals, summary, owner, country)
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
        description="Export a read-only catalog of stored originals (schema 7–18)"
    )
    parser.add_argument(
        "--data-dir", default=os.getenv("TEA_DATA_DIR") or str(Path(__file__).resolve().parents[2] / "data")
    )
    parser.add_argument("--country", default=os.getenv("TEA_INVENTORY_COUNTRY") or None)
    parser.add_argument("--owner", default=os.getenv("TEA_INVENTORY_OWNER") or None)
    parser.add_argument("--include-text", action="store_true")
    parser.add_argument("--include-source-mapping", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = export_inventory(
            args.data_dir,
            country=args.country,
            owner=args.owner,
            include_text=args.include_text,
            include_source_mapping=args.include_source_mapping,
        )
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
