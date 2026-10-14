"""Offline, ID-preserving system-library migration; private business never copies."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from .collection_import import _blob, _fingerprint, _path_key, _validate_registry
from .db import dump, now
from .ingestion_quality import text_complete
from .retrieval import embedding_text, input_hash, stored_vector
from .system_library import SYSTEM_OWNER_ID, SYSTEM_OWNER_NAME, ensure_principal, official_source_allowed

PROTOCOL = "system-corpus-migration-v1"
MAX_DOCUMENTS = 1000
MAX_MANIFEST_BYTES = 40 * 1024 * 1024
MAX_SCOPE_BYTES = 1024 * 1024 * 1024
MAX_BLOBS = 100000
MAX_BLOB_BYTES = 32 * 1024 * 1024 * 1024
_TABLES = (
    "documents",
    "document_countries",
    "document_metadata",
    "document_metadata_history",
    "legal_classifications",
    "legal_classification_history",
    "legal_relations",
    "processing_jobs",
    "retrieval_documents",
    "retrieval_dirty",
    "retrieval_chunks",
    "retrieval_vectors",
)


class SystemCorpusMigrationError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise SystemCorpusMigrationError(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _hash_json(value):
    def blob(item):
        if isinstance(item, bytes):
            return {"BLOB_SHA256": _sha(item), "bytes": len(item)}
        raise TypeError("unsupported_scope_value")

    return _sha(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=blob).encode())


def _file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _readonly(path):
    path = Path(path).resolve()
    _require(path.is_file(), "existing_database_required")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    return conn


def _scope(conn):
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    _require({"users", "documents", "schema_migrations"} <= tables, "unsupported_database")
    size, largest, count = conn.execute(
        "SELECT COALESCE(SUM(length(CAST(manifest AS BLOB))),0),COALESCE(MAX(length(CAST(manifest AS BLOB))),0),COUNT(*) FROM documents WHERE owner_id=?",
        (SYSTEM_OWNER_ID,),
    ).fetchone()
    _require(
        count <= MAX_DOCUMENTS and size <= MAX_SCOPE_BYTES and largest <= MAX_MANIFEST_BYTES,
        "system_scope_too_large",
    )
    selection = "SELECT id FROM documents WHERE owner_id=?"
    if "retrieval_chunks" in tables:
        count, size = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(CAST(text AS BLOB))+length(CAST(retrieval_text AS BLOB))+length(CAST(locator AS BLOB))),0) FROM retrieval_chunks WHERE document_id IN ("
            + selection
            + ")",
            (SYSTEM_OWNER_ID,),
        ).fetchone()
        _require(count <= 500000 and size <= MAX_SCOPE_BYTES, "retrieval_scope_too_large")
    if "retrieval_vectors" in tables:
        count, size = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(v.vector)),0) FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id IN ("
            + selection
            + ")",
            (SYSTEM_OWNER_ID,),
        ).fetchone()
        _require(count <= 500000 and size <= MAX_SCOPE_BYTES, "vector_scope_too_large")
    rows = {}
    for table in _TABLES:
        if table not in tables:
            rows[table] = []
            continue
        if table == "documents":
            query = "SELECT * FROM documents WHERE owner_id=? ORDER BY id"
        elif table == "legal_relations":
            query = "SELECT * FROM legal_relations WHERE owner_id=? ORDER BY id"
        elif table == "retrieval_vectors":
            query = (
                "SELECT v.*,c.document_id,c.chunk_id FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id IN ("
                + selection
                + ") ORDER BY c.document_id,c.chunk_id"
            )
        else:
            query = f"SELECT * FROM {table} WHERE document_id IN ({selection}) ORDER BY rowid"
        rows[table] = [dict(row) for row in conn.execute(query, (SYSTEM_OWNER_ID,))]
    fingerprints = {row["fingerprint"] for row in rows["retrieval_vectors"]}
    rows["retrieval_models"] = (
        [
            dict(row)
            for row in conn.execute("SELECT * FROM retrieval_models ORDER BY fingerprint")
            if row["fingerprint"] in fingerprints
        ]
        if "retrieval_models" in tables
        else []
    )
    principal = conn.execute("SELECT * FROM users WHERE id=?", (SYSTEM_OWNER_ID,)).fetchone()
    principal = dict(principal) if principal else None
    sessions = (
        conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (SYSTEM_OWNER_ID,)).fetchone()[0]
        if "sessions" in tables
        else 0
    )
    version = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
    result = {"rows": rows, "principal": principal, "principal_sessions": sessions, "schema_version": version}
    result["scope_sha256"] = _hash_json(
        {"rows": rows, "principal": principal, "principal_sessions": sessions}
    )
    return result


def _has_leases(scope):
    return any(
        row["status"] == "processing" or row["lease_token"] is not None or row["lease_until"] is not None
        for row in scope["rows"]["processing_jobs"]
    )


def _principal(scope, *, required):
    row = scope["principal"]
    if row is None:
        _require(not required, "source_system_principal_required")
        return
    _require(
        row["name"] == SYSTEM_OWNER_NAME
        and row["email"] is None
        and row["password_hash"] is None
        and row["is_demo"] == 0
        and scope["principal_sessions"] == 0,
        "system_principal_conflict",
    )


def _backup(path, destination):
    _require(not destination.exists(), "backup_destination_exists")
    deadline = time.monotonic() + 120
    with closing(_readonly(path)) as source, closing(sqlite3.connect(destination)) as target:
        # Pin a read snapshot before SQLite's online backup, including WAL data.
        source.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()

        def progress(status, remaining, total):
            _require(time.monotonic() < deadline, "backup_deadline_exceeded")

        source.backup(target, pages=128, progress=progress, sleep=0.01)
        target.execute("PRAGMA journal_mode=DELETE")
    with destination.open("rb") as stream:
        os.fsync(stream.fileno())
    destination.chmod(0o400)
    return _file_sha(destination)


def _inventory(source_dir):
    owner_hash = _sha(SYSTEM_OWNER_ID.encode())
    root = source_dir / "files/blobs" / owner_hash
    result, total = [], 0
    if root.exists():
        _require(not root.is_symlink(), "source_CAS_symlink")
        for path in sorted(root.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            digest = path.name
            size = path.stat().st_size
            total += size
            _require(len(result) < MAX_BLOBS and total <= MAX_BLOB_BYTES, "source_CAS_scope_too_large")
            verified = _blob(source_dir, SYSTEM_OWNER_ID, digest)
            _require(verified == path, "source_CAS_identity_mismatch")
            result.append({"sha256": digest, "bytes": size, "path": str(path.relative_to(source_dir))})
    return result


def export_system_corpus(source_dir, snapshot_dir):
    """Create a stable SQLite backup and immutable-owner CAS inventory; no source writes."""
    source_dir, snapshot_dir = Path(source_dir).resolve(), Path(snapshot_dir).resolve()
    _require(source_dir != snapshot_dir and not snapshot_dir.exists(), "new_snapshot_directory_required")
    _require((source_dir / "tea.sqlite3").is_file(), "source_database_required")
    snapshot_dir.mkdir(mode=0o700, parents=True)
    digest = _backup(source_dir / "tea.sqlite3", snapshot_dir / "tea.sqlite3")
    with closing(_readonly(snapshot_dir / "tea.sqlite3")) as conn:
        scope = _scope(conn)
    _principal(scope, required=True)
    _require(scope["schema_version"] == 15, "source_schema15_required")
    inventory = _inventory(source_dir)
    report = {
        "protocol": PROTOCOL,
        "source_data_dir": str(source_dir),
        "created_at": now(),
        "snapshot_db_sha256": digest,
        "source_scope_sha256": scope["scope_sha256"],
        "source_snapshot_has_processing_leases": _has_leases(scope),
        "CAS_blobs": inventory,
        "CAS_inventory_sha256": _hash_json(inventory),
        "documents": len(scope["rows"]["documents"]),
        "relations": len(scope["rows"]["legal_relations"]),
        "vectors": len(scope["rows"]["retrieval_vectors"]),
        "source_DB_writes": 0,
        "source_files_moved": 0,
        "model_calls": 0,
        "HTTP_calls": 0,
    }
    payload = dump(report).encode()
    with (snapshot_dir / "snapshot.json").open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    (snapshot_dir / "snapshot.json").chmod(0o400)
    from .blob_store import _sync_directory

    _sync_directory(snapshot_dir)
    _sync_directory(snapshot_dir.parent)
    return {key: value for key, value in report.items() if key != "CAS_blobs"} | {
        "snapshot_dir": str(snapshot_dir),
        "snapshot_report_sha256": _sha(payload),
    }


def _cas_reference(value, source_dir, paths):
    if isinstance(value, list):
        for item in value:
            _cas_reference(item, source_dir, paths)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key == "image_blob" and isinstance(item, dict):
                digest = item.get("content_hash")
                _require(
                    digest in paths and item.get("mime_type") == "image/png", "invalid_PNG_CAS_reference"
                )
                expected = str(paths[digest].relative_to(source_dir))
                _require(
                    item.get("storage_path") == expected
                    and item.get("size") == paths[digest].stat().st_size
                    and value.get("image_sha256", digest) == digest,
                    "PNG_storage_path_or_size_mismatch",
                )
                with paths[digest].open("rb") as stream:
                    _require(stream.read(8) == b"\x89PNG\r\n\x1a\n", "PNG_signature_mismatch")
            if _path_key(key) and isinstance(item, str) and "files/blobs/" in item:
                _require(not Path(item).is_absolute(), "absolute_manifest_CAS_path_not_portable")
                digest = Path(item).name
                _require(
                    digest in paths and item == str(paths[digest].relative_to(source_dir)),
                    "manifest_CAS_reference_mismatch",
                )
            _cas_reference(item, source_dir, paths)
        for path_key, hash_key in [
            ("full_page_PNG", "PNG_sha256"),
            ("full_page_PNG_path", "full_page_PNG_sha256"),
            ("source_PNG_path", "image_sha256"),
            ("PNG_CAS_path", "image_sha256"),
            ("mapping_path", "mapping_sha256"),
            ("registry_path", "registry_sha256"),
            ("source_registry_path", "source_registry_sha256"),
        ]:
            if (
                isinstance(value.get(path_key), str)
                and "files/blobs/" in value[path_key]
                and hash_key in value
            ):
                _require(Path(value[path_key]).name == value[hash_key], "proof_path_hash_mismatch")


def _validate_source(scope, source_dir, inventory):
    rows = scope["rows"]
    documents = {row["id"]: row for row in rows["documents"]}
    countries = {row["document_id"]: row["country"] for row in rows["document_countries"]}
    metadata = {row["document_id"]: row for row in rows["document_metadata"]}
    classes = {(row["document_id"], row["collection"]): row for row in rows["legal_classifications"]}
    jobs = {row["document_id"]: row for row in rows["processing_jobs"]}
    chunks = {(row["document_id"], row["chunk_id"]): row for row in rows["retrieval_chunks"]}
    indices = {row["document_id"]: row for row in rows["retrieval_documents"]}
    dirty = {row["document_id"] for row in rows["retrieval_dirty"]}
    paths = {}
    _require(
        len(inventory) <= MAX_BLOBS and sum(row["bytes"] for row in inventory) <= MAX_BLOB_BYTES,
        "snapshot_CAS_scope_too_large",
    )
    for item in inventory:
        digest = item["sha256"]
        path = _blob(source_dir, SYSTEM_OWNER_ID, digest)
        _require(
            str(path.relative_to(source_dir)) == item["path"] and path.stat().st_size == item["bytes"],
            "snapshot_CAS_changed",
        )
        paths[digest] = path
    families = set()
    _require(
        all(row["owner_id"] == SYSTEM_OWNER_ID for row in rows["processing_jobs"]),
        "processing_owner_mismatch",
    )
    for doc in documents.values():
        identity = (doc["family_id"], doc["version"])
        _require(
            type(doc["version"]) is int
            and doc["version"] >= 1
            and bool(doc["family_id"])
            and identity not in families,
            "immutable_family_version_conflict",
        )
        families.add(identity)
        parent = documents.get(doc["parent_id"])
        country = countries.get(doc["id"], countries.get(doc["parent_id"]))
        _require(
            doc["case_id"] is None and official_source_allowed(doc["source_url"], country),
            "official_system_source_required",
        )
        _require(
            doc["content_hash"] in paths
            and paths[doc["content_hash"]].stat().st_size == doc["size"]
            and Path(doc["storage_path"]).resolve() == paths[doc["content_hash"]],
            "original_CAS_identity_mismatch",
        )
        manifest = json.loads(doc["manifest"])
        _cas_reference(manifest, source_dir, paths)
        if doc["role"] == "secondary":
            _require(
                parent
                and parent["role"] == "primary"
                and countries.get(parent["id"]) == country
                and not manifest.get("pages")
                and not manifest.get("chunks")
                and doc["id"] not in metadata
                and doc["id"] not in jobs
                and not any(key[0] == doc["id"] for key in chunks),
                "ordinary_attachment_has_derived_body_or_invalid_parent",
            )
        else:
            _require(
                doc["role"] == "primary"
                and doc["parent_id"] is None
                and doc["id"] in metadata
                and (doc["id"], "country:" + country) in classes
                and manifest.get("source_hash") == doc["content_hash"],
                "normative_document_identity_required",
            )
        doc["parsed_manifest"], doc["country"] = manifest, country
        by_id = {
            str(item.get("id", "")): item
            for item in manifest.get("chunks", [])
            if str(item.get("text", "")).strip()
        }
        for (ident, chunk_id), chunk in chunks.items():
            if ident != doc["id"]:
                continue
            original = by_id.get(chunk_id)
            _require(
                original is not None
                and chunk["text"] == original["text"]
                and chunk["retrieval_text"] == embedding_text(original)
                and chunk["input_hash"] == input_hash(embedding_text(original))
                and chunk["content_hash"] == _sha(original["text"].encode()),
                "chunk_input_identity_mismatch",
            )
    for doc in documents.values():
        for page in doc["parsed_manifest"].get("pages", []):
            proof = (page.get("ocr") or {}).get("reuse")
            if not proof:
                continue
            donor = documents.get(proof.get("source_document_id"))
            _require(
                proof.get("protocol") == "same-owner-pdf-page-reuse-v1"
                and donor
                and donor["role"] == "primary"
                and donor["version"] == proof.get("source_document_version") == doc["version"]
                and donor["content_hash"] == proof.get("source_content_hash") == doc["content_hash"]
                and donor["source_url"] == doc["source_url"]
                and proof.get("target_document_id") == doc["id"]
                and proof.get("target_document_version") == doc["version"],
                "same_owner_OCR_identity_mismatch",
            )
            number = proof.get("source_page")
            donor_page = next(
                (item for item in donor["parsed_manifest"].get("pages", []) if item.get("page") == number),
                None,
            )
            _require(
                donor_page
                and number == page.get("page")
                and _hash_json(donor_page) == proof.get("source_page_checkpoint_sha256"),
                "same_owner_OCR_checkpoint_mismatch",
            )
        provenance = doc["parsed_manifest"].get("embedding_reuse") or {}
        if provenance:
            _require(
                provenance.get("protocol") == "same-owner-original-embedding-reuse-v1",
                "unsupported_embedding_provenance",
            )
        for record in provenance.get("records", {}).values():
            donor = documents.get(record.get("source_document_id"))
            _require(
                donor
                and donor["role"] == "primary"
                and donor["version"] == record.get("source_document_version")
                and donor["content_hash"] == record.get("source_hash")
                and record.get("target_document_id") == doc["id"]
                and record.get("target_document_version") == doc["version"]
                and record.get("target_source_hash") == doc["content_hash"],
                "same_owner_embedding_identity_mismatch",
            )
    fingerprint = _fingerprint()
    models = {row["fingerprint"]: row["dimension"] for row in rows["retrieval_models"]}
    for vector in rows["retrieval_vectors"]:
        doc, chunk = documents[vector["document_id"]], chunks[(vector["document_id"], vector["chunk_id"])]
        index = indices.get(doc["id"])
        _require(
            classes[(doc["id"], "country:" + doc["country"])]["level"] == "primary"
            and doc["role"] == "primary"
            and text_complete(doc["parsed_manifest"])
            and index
            and index["complete"]
            and index["source_hash"] == doc["content_hash"]
            and doc["id"] not in dirty,
            "vector_document_not_current_primary",
        )
        _require(
            fingerprint is not None
            and vector["fingerprint"] == fingerprint
            and doc["parsed_manifest"].get("embedding_fingerprint", fingerprint) == fingerprint,
            "embedding_configuration_mismatch",
        )
        _require(vector["input_hash"] == chunk["input_hash"], "vector_input_hash_mismatch")
        try:
            stored_vector(vector["vector"], vector["dimension"], models.get(vector["fingerprint"]))
        except ValueError as exc:
            raise SystemCorpusMigrationError("invalid_source_unit_vector") from exc
    for relation in rows["legal_relations"]:
        source, target = (
            documents.get(relation["source_document_id"]),
            documents.get(relation["target_document_id"]),
        )
        _require(
            source
            and target
            and source["role"] == target["role"] == "primary"
            and source["country"] == target["country"]
            and relation["collection"] == "country:" + source["country"],
            "relationship_endpoint_identity_mismatch",
        )
        try:
            locator = json.loads(relation["locator"])
        except (TypeError, json.JSONDecodeError):
            locator = {}
        digest = locator.get("registry_sha256") or locator.get("source_registry_sha256")
        if digest:
            _cas_reference(locator, source_dir, paths)
            _require(
                digest in paths and paths[digest].stat().st_size <= 4 * 1024 * 1024, "typed_registry_required"
            )
            registry = json.loads(paths[digest].read_bytes())
            _require(registry.get("owner_id") == SYSTEM_OWNER_ID, "registry_owner_mismatch")
            _cas_reference(registry, source_dir, paths)
            try:
                _validate_registry(registry, list(documents.values()), paths)
            except (ValueError, KeyError, TypeError) as exc:
                raise SystemCorpusMigrationError("typed_registry_page_binding_invalid") from exc
    for doc in documents.values():
        doc.pop("parsed_manifest")
        doc.pop("country")
    return paths


def _normalized(source, target_dir):
    rows = {table: [dict(row) for row in values] for table, values in source["rows"].items()}
    prefix = target_dir / "files/blobs" / _sha(SYSTEM_OWNER_ID.encode())
    for row in rows["documents"]:
        digest = row["content_hash"]
        row["storage_path"] = str(prefix / digest[:2] / digest[2:4] / digest)
    documents = {row["id"]: row for row in rows["documents"]}
    jobs = {row["document_id"]: row for row in rows["processing_jobs"]}
    vectors = {}
    for row in rows["retrieval_vectors"]:
        vectors.setdefault(row["document_id"], set()).add(row["chunk_id"])
    for doc in documents.values():
        if doc["role"] != "primary":
            continue
        manifest = json.loads(doc["manifest"])
        complete = text_complete(manifest)
        job = jobs.get(doc["id"])
        missing_vectors = bool(_fingerprint()) and any(
            str(chunk.get("id", "")) not in vectors.get(doc["id"], set())
            for chunk in manifest.get("chunks", [])
            if str(chunk.get("text", "")).strip()
        )
        country = next(
            row["country"] for row in rows["document_countries"] if row["document_id"] == doc["id"]
        )
        primary = any(
            row["document_id"] == doc["id"]
            and row["collection"] == "country:" + country
            and row["level"] == "primary"
            for row in rows["legal_classifications"]
        )
        pending = not complete or (primary and missing_vectors)
        if pending and (job is None or job["status"] == "completed"):
            job = {
                "document_id": doc["id"],
                "owner_id": SYSTEM_OWNER_ID,
                "phase": "parse" if not complete else "embed",
                "status": "queued",
                "attempts": 0,
                "available_at": 0,
                "lease_token": None,
                "lease_until": None,
                "error": None,
                "updated_at": doc["created_at"],
            }
            # Preserve any schema-default fields, such as durable queue revision.
            template = next(iter(source["rows"]["processing_jobs"]), {})
            job = {key: job.get(key, template[key]) for key in template} if template else job
            if doc["id"] in jobs:
                rows["processing_jobs"] = [
                    item for item in rows["processing_jobs"] if item["document_id"] != doc["id"]
                ]
            rows["processing_jobs"].append(job)
        if not complete and doc["status"] == "ready":
            doc["status"] = "failed" if job and job["status"] == "failed" else "processing"
    return rows


def _comparable(rows):
    result = {}
    for table, values in rows.items():
        normalized = []
        for row in values:
            item = dict(row)
            if table == "retrieval_chunks":
                item.pop("rowid")
            if table == "retrieval_vectors":
                item.pop("chunk_rowid")
            normalized.append(item)
        result[table] = sorted((_hash_json(row) for row in normalized))
    return result


def _check_target(conn, source, expected, target):
    _principal(target, required=False)
    _require(not _has_leases(target), "target_has_processing_lease")
    incoming = {row["id"]: row for row in expected["documents"]}
    for ident in incoming:
        existing = conn.execute("SELECT * FROM documents WHERE id=?", (ident,)).fetchone()
        _require(
            existing is None
            or (existing["owner_id"] == SYSTEM_OWNER_ID and dict(existing) == incoming[ident]),
            "target_document_conflict",
        )
        family = conn.execute(
            "SELECT id FROM documents WHERE family_id=? AND version=? AND id<>?",
            (incoming[ident]["family_id"], incoming[ident]["version"], ident),
        ).fetchone()
        _require(family is None, "target_family_version_conflict")
    existing_ids = {row["id"] for row in target["rows"]["documents"]}
    _require(existing_ids <= incoming.keys(), "target_system_scope_contains_other_documents")
    if existing_ids:
        source_rows = {
            table: [
                row
                for row in values
                if (
                    row.get("document_id") in existing_ids
                    or row.get("id") in existing_ids
                    or table == "legal_relations"
                    and row["source_document_id"] in existing_ids
                    and row["target_document_id"] in existing_ids
                    or table == "retrieval_models"
                )
            ]
            for table, values in expected.items()
        }
        _require(_comparable(source_rows) == _comparable(target["rows"]), "target_system_data_changed")
    actors = {
        row["actor_id"]
        for table in ["document_metadata_history", "legal_classification_history"]
        for row in expected[table]
    }
    actors |= {row["reviewed_by"] for row in expected["document_metadata"] if row["reviewed_by"]}
    for actor in actors - {SYSTEM_OWNER_ID}:
        _require(
            conn.execute("SELECT 1 FROM users WHERE id=?", (actor,)).fetchone(),
            "target_reviewer_or_actor_missing",
        )
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for relation in expected["legal_relations"]:
        prior = conn.execute("SELECT * FROM legal_relations WHERE id=?", (relation["id"],)).fetchone()
        _require(prior is None or dict(prior) == relation, "target_relation_id_conflict")
    if "retrieval_models" in tables:
        for model in expected["retrieval_models"]:
            prior = conn.execute(
                "SELECT dimension FROM retrieval_models WHERE fingerprint=?", (model["fingerprint"],)
            ).fetchone()
            _require(prior is None or prior[0] == model["dimension"], "target_model_dimension_conflict")


def _insert(conn, table, row):
    fields = list(row)
    conn.execute(
        f"INSERT INTO {table}({','.join(fields)}) VALUES({','.join('?' for _ in fields)})",
        [row[field] for field in fields],
    )


def migrate_system_corpus(snapshot_dir, target_dir, *, apply=False, source_stopped=False, backup_dir=None):
    """Default preflight does not initialize, upgrade or write the target."""
    snapshot_dir, target_dir = Path(snapshot_dir).resolve(), Path(target_dir).resolve()
    receipt = json.loads((snapshot_dir / "snapshot.json").read_bytes())
    _require(
        receipt["protocol"] == PROTOCOL
        and _file_sha(snapshot_dir / "tea.sqlite3") == receipt["snapshot_db_sha256"],
        "snapshot_database_hash_mismatch",
    )
    _require(
        _hash_json(receipt["CAS_blobs"]) == receipt["CAS_inventory_sha256"],
        "snapshot_CAS_inventory_hash_mismatch",
    )
    source_dir = Path(receipt["source_data_dir"]).resolve()
    _require(target_dir not in {source_dir, snapshot_dir}, "source_snapshot_target_must_differ")
    with closing(_readonly(snapshot_dir / "tea.sqlite3")) as conn:
        source = _scope(conn)
    _principal(source, required=True)
    _require(source["scope_sha256"] == receipt["source_scope_sha256"], "snapshot_scope_hash_mismatch")
    paths = _validate_source(source, source_dir, receipt["CAS_blobs"])
    blockers = []
    if not source_stopped:
        blockers.append("source_stop_attestation_required")
    if _has_leases(source):
        blockers.append("source_snapshot_has_processing_lease")
    with closing(_readonly(source_dir / "tea.sqlite3")) as conn:
        live = _scope(conn)
    if _has_leases(live):
        blockers.append("source_has_processing_lease")
    if live["scope_sha256"] != source["scope_sha256"]:
        blockers.append("source_changed_since_snapshot")
    expected = _normalized(source, target_dir)
    with closing(_readonly(target_dir / "tea.sqlite3")) as conn:
        target = _scope(conn)
        _check_target(conn, source, expected, target)
    existing_ids = {row["id"] for row in target["rows"]["documents"]}
    report = {
        "protocol": PROTOCOL,
        "mode": "dry_run",
        "source_scope_sha256": source["scope_sha256"],
        "documents": len(expected["documents"]),
        "ordinary_attachments": sum(row["role"] == "secondary" for row in expected["documents"]),
        "relations": len(expected["legal_relations"]),
        "true_vectors": len(expected["retrieval_vectors"]),
        "verified_CAS_blobs": len(paths),
        "source_schema": source["schema_version"],
        "target_schema_before": target["schema_version"],
        "blockers": blockers,
        "source_stopped_attested": source_stopped,
        "target_writes": 0,
        "model_calls": 0,
        "HTTP_calls": 0,
        "private_users_sessions_and_business_copied": False,
    }
    if not apply:
        return report
    _require(not blockers, "migration_preflight_blocked:" + ",".join(blockers))
    backup_dir = (
        Path(backup_dir).resolve()
        if backup_dir
        else target_dir / "migration-backups" / (now().replace(":", "") + "-" + uuid4().hex)
    )
    _require(not backup_dir.exists(), "new_target_backup_directory_required")
    backup_dir.mkdir(mode=0o700, parents=True)
    backup_sha = _backup(target_dir / "tea.sqlite3", backup_dir / "tea.sqlite3")
    with (backup_dir / "backup.json").open("x") as stream:
        stream.write(
            dump(
                {
                    "protocol": PROTOCOL,
                    "target_db_sha256": backup_sha,
                    "target_schema_before": target["schema_version"],
                    "source_scope_sha256": source["scope_sha256"],
                    "created_at": now(),
                }
            )
        )
        stream.flush()
        os.fsync(stream.fileno())
    from .blob_store import _sync_directory

    _sync_directory(backup_dir)
    _sync_directory(backup_dir.parent)
    from .service import Service

    # Structural upgrade is committed separately before any system corpus copy.
    service = Service(target_dir)
    with closing(service.db.connect()) as conn:
        upgraded = _scope(conn)
        _check_target(conn, source, expected, upgraded)
    from .blob_store import BlobStore

    blobs = BlobStore(target_dir / "files", max_bytes=128 * 1024 * 1024)
    for digest, path in paths.items():
        with path.open("rb") as stream:
            blob = blobs.put(SYSTEM_OWNER_ID, iter(lambda: stream.read(1024 * 1024), b""))
        _require(blob.digest == digest, "source_blob_changed_during_copy")
    with closing(_readonly(source_dir / "tea.sqlite3")) as conn:
        current_source = _scope(conn)
    _require(
        not _has_leases(current_source) and current_source["scope_sha256"] == source["scope_sha256"],
        "source_resumed_or_changed_during_copy",
    )
    with service.db.transaction() as conn:
        current = _scope(conn)
        _require(current["scope_sha256"] == upgraded["scope_sha256"], "target_changed_during_copy")
        _check_target(conn, source, expected, current)
        ensure_principal(conn)
        for doc in sorted(expected["documents"], key=lambda row: (row["role"], row["version"], row["id"])):
            if doc["id"] not in existing_ids:
                _insert(conn, "documents", doc)
        for table in [
            "document_countries",
            "document_metadata",
            "document_metadata_history",
            "legal_classifications",
            "legal_classification_history",
            "processing_jobs",
            "retrieval_documents",
        ]:
            for row in expected[table]:
                if row["document_id"] not in existing_ids:
                    _insert(conn, table, row)
        for model in expected["retrieval_models"]:
            if not conn.execute(
                "SELECT 1 FROM retrieval_models WHERE fingerprint=?", (model["fingerprint"],)
            ).fetchone():
                _insert(conn, "retrieval_models", model)
        chunk_ids = {}
        for row in expected["retrieval_chunks"]:
            if row["document_id"] not in existing_ids:
                copied = {key: value for key, value in row.items() if key != "rowid"}
                _insert(conn, "retrieval_chunks", copied)
            chunk_ids[(row["document_id"], row["chunk_id"])] = conn.execute(
                "SELECT rowid FROM retrieval_chunks WHERE document_id=? AND chunk_id=?",
                (row["document_id"], row["chunk_id"]),
            ).fetchone()[0]
        for row in expected["retrieval_vectors"]:
            if row["document_id"] not in existing_ids:
                copied = {key: value for key, value in row.items() if key not in {"document_id", "chunk_id"}}
                copied["chunk_rowid"] = chunk_ids[(row["document_id"], row["chunk_id"])]
                _insert(conn, "retrieval_vectors", copied)
        for relation in expected["legal_relations"]:
            if not conn.execute("SELECT 1 FROM legal_relations WHERE id=?", (relation["id"],)).fetchone():
                _insert(conn, "legal_relations", relation)
        dirty = {row["document_id"] for row in expected["retrieval_dirty"]}
        for doc in expected["documents"]:
            if doc["id"] not in existing_ids and doc["id"] not in dirty:
                conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (doc["id"],))
        final = _scope(conn)
        _require(_comparable(final["rows"]) == _comparable(expected), "migration_readback_mismatch")
        _require(not conn.execute("PRAGMA foreign_key_check").fetchone(), "migration_foreign_key_error")
    return report | {
        "mode": "applied",
        "target_writes": len(expected["documents"]) - len(existing_ids),
        "unchanged_documents": len(existing_ids),
        "target_schema_after": final["schema_version"],
        "target_backup_path": str(backup_dir / "tea.sqlite3"),
        "target_backup_sha256": backup_sha,
        "original_IDs_versions_manifest_registry_bytes_preserved": True,
        "vector_bytes_preserved_without_renormalization": True,
        "copied_scope_sha256": _hash_json(_comparable(final["rows"])),
    }
