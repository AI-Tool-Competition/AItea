"""Explicit, offline transfer of collected originals into an existing account.

The source is never opened by Service: reading must not migrate or claim work.
This first version admits only single-version families (version 1).
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from .db import dump, now
from .ingestion_quality import text_complete
from .retrieval import embedding_text, input_hash, normalized_vector
from .system_library import SYSTEM_OWNER_ID, official_source_allowed

PROTOCOL = "collected-resource-import-v1"
MAX_DOCUMENTS = 200
MAX_MANIFEST_BYTES = 40 * 1024 * 1024
MAX_TOTAL_MANIFEST_BYTES = 256 * 1024 * 1024
MAX_ASSETS = 4096
MAX_BLOB_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BLOB_BYTES = 2 * 1024 * 1024 * 1024
MAX_PROVENANCE_BYTES = 4 * 1024 * 1024
MAX_RELATION_REGISTRIES = 32
MAX_TOTAL_REGISTRY_BYTES = 32 * 1024 * 1024
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_ASSET_POINTER = re.compile(
    r"/(?:html_source/linked_images|assets_pending|legacy/conversion/images)/(0|[1-9][0-9]{0,3})\Z"
)
_PATH_KEYS = {
    "path",
    "cas_path",
    "image_path",
    "source_path",
    "storage_path",
    "raw_path",
    "mapping_path",
    "manifest_path",
    "source_original_path",
    "source_PNG_path",
    "PNG_CAS_path",
    "full_page_PNG",
    "full_page_PNG_path",
    "full_page_png",
    "PNG_path",
    "owner_CAS_png_path",
    "registry_path",
    "source_registry_path",
}


class CollectionImportError(ValueError):
    pass


def _path_key(key):
    return key in _PATH_KEYS or key.endswith("_path")


def _asset_pointer(key, value):
    if key not in {"mapping_path", "asset_mapping_path"}:
        return False
    match = _ASSET_POINTER.fullmatch(value)
    return match is not None and int(match[1]) < 2000


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _selected_ids(document_ids):
    if document_ids is None:
        return None
    if (
        not isinstance(document_ids, (list, tuple))
        or not 1 <= len(document_ids) <= MAX_DOCUMENTS
        or any(not isinstance(ident, str) or not ident or len(ident) > 128 for ident in document_ids)
        or len(set(document_ids)) != len(document_ids)
    ):
        raise CollectionImportError("document_selection_requires_1_to_200_unique_ids")
    return list(document_ids)


def _read_db(directory, owner, *, target_only=False, document_ids=None):
    selected = _selected_ids(document_ids)
    path = Path(directory).resolve() / "tea.sqlite3"
    if not path.is_file():
        raise CollectionImportError("existing_database_required")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (owner,)).fetchone():
            raise CollectionImportError("owner_not_found")
        if target_only:
            return {"as_of": now()}
        scope, params = "d.owner_id=?", [owner]
        if selected is not None:
            scope += " AND d.id IN (" + ",".join("?" for _ in selected) + ")"
            params += selected
            count = conn.execute(f"SELECT COUNT(*) FROM documents d WHERE {scope}", params).fetchone()[0]
            if count != len(selected):
                raise CollectionImportError("selected_documents_not_owned_or_missing")
        size, largest = conn.execute(
            f"SELECT COALESCE(SUM(length(CAST(d.manifest AS BLOB))),0),COALESCE(MAX(length(CAST(d.manifest AS BLOB))),0) FROM documents d WHERE {scope}",
            params,
        ).fetchone()
        if size > MAX_TOTAL_MANIFEST_BYTES or largest > MAX_MANIFEST_BYTES:
            raise CollectionImportError("total_manifest_scope_exceeded")
        documents = [
            dict(r)
            for r in conn.execute(
                f"SELECT d.* FROM documents d WHERE {scope} ORDER BY d.role,d.family_id,d.version,d.id LIMIT ?",
                [*params, MAX_DOCUMENTS + 1],
            )
        ]
        if len(documents) > MAX_DOCUMENTS:
            raise CollectionImportError("document_scope_exceeds_200")
        ids = [d["id"] for d in documents]
        result = {
            "as_of": now(),
            "documents": documents,
            "family_counts": {
                row["family_id"]: row["count"]
                for row in conn.execute(
                    f"""SELECT history.family_id,COUNT(*) count FROM documents history
                    WHERE history.owner_id=? AND history.family_id IN
                    (SELECT d.family_id FROM documents d WHERE {scope}) GROUP BY history.family_id""",
                    [owner, *params],
                )
            },
        }
        for table in ("document_metadata", "document_countries", "legal_classifications"):
            result[table] = [
                dict(r)
                for r in conn.execute(
                    f"SELECT t.* FROM {table} t JOIN documents d ON d.id=t.document_id WHERE {scope}",
                    params,
                )
            ]
        result["relations"] = [
            dict(r)
            for r in conn.execute("SELECT * FROM legal_relations WHERE owner_id=? ORDER BY id", (owner,))
        ]
        if selected is not None:
            selected_set = set(selected)
            relations = [
                row
                for row in result["relations"]
                if row["source_document_id"] in selected_set or row["target_document_id"] in selected_set
            ]
            missing = sorted(
                {row[key] for row in relations for key in ("source_document_id", "target_document_id")}
                - selected_set
            )
            if missing:
                raise CollectionImportError("selected_relation_dependencies_required:" + ",".join(missing))
            result["relations"] = relations
        result["jobs"] = [
            dict(r)
            for r in conn.execute(
                f"SELECT t.* FROM processing_jobs t JOIN documents d ON d.id=t.document_id WHERE {scope}",
                params,
            )
        ]
        result["indices"] = {
            r["document_id"]: dict(r)
            for r in conn.execute(
                f"SELECT i.* FROM retrieval_documents i JOIN documents d ON d.id=i.document_id WHERE {scope}",
                params,
            )
        }
        result["dirty"] = {
            r[0]
            for r in conn.execute(
                f"SELECT i.document_id FROM retrieval_dirty i JOIN documents d ON d.id=i.document_id WHERE {scope}",
                params,
            )
        }
        count, vector_bytes = conn.execute(
            f"""SELECT count(*),COALESCE(sum(length(v.vector)+length(CAST(c.text AS BLOB))+length(CAST(c.retrieval_text AS BLOB))),0) FROM retrieval_chunks c
            JOIN retrieval_vectors v ON v.chunk_rowid=c.rowid JOIN documents d ON d.id=c.document_id
            WHERE {scope}""",
            params,
        ).fetchone()
        if count > 100000 or vector_bytes > MAX_TOTAL_MANIFEST_BYTES:
            raise CollectionImportError("vector_scope_exceeded")
        result["vectors"] = [
            dict(r)
            for r in conn.execute(
                f"""SELECT c.document_id,c.chunk_id,c.text,c.retrieval_text,c.input_hash,
                v.fingerprint,v.dimension,v.vector,v.input_hash vector_input_hash
                FROM retrieval_chunks c JOIN retrieval_vectors v ON v.chunk_rowid=c.rowid
                JOIN documents d ON d.id=c.document_id WHERE {scope} ORDER BY c.document_id,c.ordinal""",
                params,
            )
        ]
        if len(result["vectors"]) > 100000:
            raise CollectionImportError("vector_scope_exceeds_100000")
        result["document_ids"] = ids
        return result


def _blob(directory, owner, digest):
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise CollectionImportError("invalid_blob_digest")
    root = Path(directory).resolve()
    expected = root / "files/blobs" / _sha(owner.encode()) / digest[:2] / digest[2:4] / digest
    if any(p.is_symlink() for p in (expected, *expected.parents) if p != root.parent):
        raise CollectionImportError("symlink_blob_refused")
    if not expected.is_file() or not 0 < expected.stat().st_size <= MAX_BLOB_BYTES:
        raise CollectionImportError("missing_or_oversize_owner_blob:" + digest)
    with expected.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != digest:
        raise CollectionImportError("owner_blob_hash_mismatch")
    return expected


def _fingerprint():
    # No Service/database initialization and no provider calls in preflight.
    import os
    from .retrieval import EMBEDDING_RECIPE

    values = [os.getenv(k) for k in ("EMBEDDING_BASE_URL", "EMBEDDING_MODEL", "EMBEDDING_API_KEY")]
    return _sha(dump([*values[:2], EMBEDDING_RECIPE]).encode()) if all(values) else None


def _add_alias(aliases, path, digest):
    # Independent evidence packages may reuse local filenames. A path cannot
    # identify two different verified blobs in the combined transfer scope.
    if path in aliases and aliases[path] != digest:
        raise CollectionImportError("ambiguous_blob_alias")
    aliases[path] = digest


def _asset_refs(value, found, aliases):
    if isinstance(value, dict):
        for key in value:
            if key != "cas_path" and not key.endswith("_cas_path"):
                continue
            prefix = key.removesuffix("cas_path")
            digest = value.get(prefix + "sha256")
            if not isinstance(digest, str):
                continue
            found.add(digest)
            for path_key in (key, prefix + "path", prefix + "source_path"):
                if isinstance(value.get(path_key), str):
                    _add_alias(aliases, value[path_key], digest)
        for item in value.values():
            _asset_refs(item, found, aliases)
    elif isinstance(value, list):
        for item in value:
            _asset_refs(item, found, aliases)


def _path_refs(value, directory, owner, found, aliases):
    """Resolve only exact account CAS references, never arbitrary file paths."""
    if isinstance(value, dict):
        for key, item in value.items():
            if _path_key(key) and isinstance(item, str):
                digest = Path(item).name
                if _DIGEST.fullmatch(digest):
                    candidate = _blob(directory, owner, digest)
                    absolute = Path(item) if Path(item).is_absolute() else Path(directory) / item
                    if absolute.absolute() == candidate:
                        found.add(digest)
                        _add_alias(aliases, item, digest)
            _path_refs(item, directory, owner, found, aliases)
    elif isinstance(value, list):
        for item in value:
            _path_refs(item, directory, owner, found, aliases)


def _rewrite(value, aliases, destinations, identities, *, bound_hash=None, target_owner=None):
    if isinstance(value, list):
        return [
            _rewrite(x, aliases, destinations, identities, bound_hash=bound_hash, target_owner=target_owner)
            for x in value
        ]
    if not isinstance(value, dict):
        return value
    bound_hash = value.get("source_hash", bound_hash)
    result = {}
    for key, item in value.items():
        if key == "collection_import":
            result[key] = copy.deepcopy(item)
            continue  # Fixed source identity must never become the target ID.
        if key == "embedding_reuse":
            continue  # Its donor account guard does not transfer to another owner.
        if key == "owner_id" and target_owner:
            result[key] = target_owner
            continue
        if _path_key(key) and isinstance(item, str):
            if _asset_pointer(key, item):
                result[key] = item  # A fixed JSON pointer, never a filesystem authority.
                continue
            if item in destinations.values():
                result[key] = item  # A second pass must preserve the exact target image CAS.
                continue
            digest = aliases.get(item)
            if digest is None and key in {"source_original_path", "storage_path", "raw_path"}:
                digest = bound_hash
            if digest in destinations:
                result[key] = destinations[digest]
            elif Path(item).is_absolute() or "files/blobs/" in item:
                raise CollectionImportError("unmapped_private_path")
            else:
                result[key] = item
        elif (
            key in {"source_document_id", "target_document_id", "document_id", "parent_id"}
            and item in identities
        ):
            result[key] = identities[item]["document_id"]
        else:
            result[key] = _rewrite(
                item, aliases, destinations, identities, bound_hash=bound_hash, target_owner=target_owner
            )
    if "source_document_id" in value and value["source_document_id"] in identities:
        result["source_version"] = identities[value["source_document_id"]]["version"]
    if "target_document_id" in value and value["target_document_id"] in identities:
        result["target_version"] = identities[value["target_document_id"]]["version"]
    if value.get("protocol") == "physical-page-completeness-retry-v1":
        from .ocr_page_retry import _encoded

        digest = _sha(_encoded(result["previous_page"]))
        if digest != value["previous_page_sha256"]:
            result["source_previous_page_sha256"] = value.get(
                "source_previous_page_sha256", value["previous_page_sha256"]
            )
            result["previous_page_sha256"] = digest
    return result


def _rewrite_manifest(manifest, aliases, destinations, identities, *, bound_hash, target_owner):
    """Rebind HTML image identities after account CAS paths change, preserving OCR."""
    from .native_asset_ocr import assets, rebuild_native_asset_text

    try:
        source_assets = assets(manifest)
    except ValueError as exc:
        raise CollectionImportError("invalid_source_native_asset_checkpoint") from exc
    result = _rewrite(
        manifest, aliases, destinations, identities, bound_hash=bound_hash, target_owner=target_owner
    )
    if not source_assets or source_assets[0]["source_format"] != "html":
        return result, {}
    # Validate fresh binding identities without stale OCR identities preventing
    # the calculation. No source bytes, locator, URL or OCR text is changed.
    staged = copy.deepcopy(result)
    for row in staged["html_source"]["linked_images"]:
        for key in ("asset_key", "input_identity", "ocr"):
            row.pop(key, None)
        row["asset_status"] = "pending_vision"
    try:
        rebound = assets(staged)
    except ValueError as exc:
        raise CollectionImportError("invalid_transferred_native_asset_binding") from exc
    chunk_ids = {}
    for old, new, row in zip(source_assets, rebound, result["html_source"]["linked_images"], strict=True):
        for key in ("input_identity", "asset_key", "mapping_path", "source_format", "source_hash"):
            row[key] = new[key]
        if isinstance(row.get("ocr"), dict):
            row["ocr"].update(
                {key: new[key] for key in ("input_identity", "asset_key", "mapping_path", "source_hash")}
            )
            blob = row["ocr"].get("image_blob")
            if blob is not None and (
                not isinstance(blob, dict)
                or blob.get("content_hash") != row["ocr"].get("image_sha256")
                or blob.get("size") != row["ocr"].get("image_size")
                or destinations.get(blob.get("content_hash")) != blob.get("storage_path")
            ):
                raise CollectionImportError("invalid_transferred_native_asset_PNG")
        row["collection_asset_import"] = {
            "protocol": "native-asset-import-v1",
            "source_input_identity": old["input_identity"],
            "source_asset_key": old["asset_key"],
            "source_mapping_pointer": old["mapping_path"],
            "source_checkpoint_sha256": _sha(dump(old).encode()),
            "target_input_identity": new["input_identity"],
            "target_asset_key": new["asset_key"],
            "source_hash": bound_hash,
        }
        chunk_ids["native-asset-ocr-" + old["input_identity"][:24]] = (
            "native-asset-ocr-" + new["input_identity"][:24]
        )
    try:
        result = rebuild_native_asset_text(result)
    except ValueError as exc:
        raise CollectionImportError("invalid_transferred_native_asset_checkpoint") from exc
    original_chunks = {str(chunk.get("id")): chunk for chunk in manifest.get("chunks", [])}
    target_chunks = {str(chunk.get("id")): chunk for chunk in result.get("chunks", [])}
    for old_id, new_id in chunk_ids.items():
        if old_id not in original_chunks:
            continue
        if new_id not in target_chunks or input_hash(embedding_text(original_chunks[old_id])) != input_hash(
            embedding_text(target_chunks[new_id])
        ):
            raise CollectionImportError("transferred_native_asset_embedding_input_changed")
    return result, chunk_ids


def _validate_retry_evidence(value, source_hash):
    """Validate immutable old OCR before any path or account is rewritten."""
    if isinstance(value, list):
        for item in value:
            _validate_retry_evidence(item, source_hash)
    elif isinstance(value, dict):
        if value.get("protocol") == "physical-page-completeness-retry-v1":
            from .ocr_page_retry import _encoded

            previous = value.get("previous_page")
            if (
                not isinstance(previous, dict)
                or value.get("source_hash") != source_hash
                or previous.get("source_hash") != source_hash
                or value.get("page") != previous.get("page")
                or value.get("previous_page_sha256") != _sha(_encoded(previous))
            ):
                raise CollectionImportError("invalid_previous_OCR_checkpoint")
        for item in value.values():
            _validate_retry_evidence(item, source_hash)


def _target_import_ids(conn, target_owner, source_owner_hash, document_ids, *, unique_ids=()):
    """Read provenance once in the write transaction; retain only selected IDs."""
    selected = set(document_ids)
    if not selected:
        return {}
    matches = {}
    # Fetch only the two provenance fields, not every owner's full manifests.
    # Streaming keeps the map bounded by the already validated source selection.
    for row in conn.execute(
        """SELECT id, json_extract(manifest,
               '$.collection_import.source_owner_sha256',
               '$.collection_import.source_document_id') AS provenance
           FROM documents WHERE owner_id=?""",
        (target_owner,),
    ):
        owner_hash, source_id = json.loads(row["provenance"])
        if owner_hash == source_owner_hash and isinstance(source_id, str) and source_id in selected:
            if source_id in unique_ids and source_id in matches:
                raise CollectionImportError("ambiguous_existing_dependency:" + source_id)
            # Preserve the existing first-match behavior for duplicate markers.
            matches.setdefault(source_id, row["id"])
    return matches


def _existing_dependencies(
    conn, target_dir, target_owner, source_owner_hash, source, dependencies, *, verify_blobs
):
    """Bind immutable originals only; paid derived checkpoints may have advanced."""
    from .archive import family_key

    imported = _target_import_ids(
        conn, target_owner, source_owner_hash, dependencies, unique_ids=dependencies
    )
    identities = {}
    for doc in source["documents"]:
        ident = doc["id"]
        if ident not in dependencies:
            continue
        row = conn.execute(
            "SELECT * FROM documents WHERE id=? AND owner_id=?",
            (imported.get(ident), target_owner),
        ).fetchone()
        if row is None:
            raise CollectionImportError("existing_dependency_not_found:" + ident)
        mark = json.loads(row["manifest"]).get("collection_import", {})
        origin = {
            "protocol": PROTOCOL,
            "source_owner_sha256": source_owner_hash,
            "source_document_id": ident,
            "source_family_id": doc["family_id"],
            "source_version": doc["version"],
            "source_hash": doc["content_hash"],
        }
        country = conn.execute(
            "SELECT country FROM document_countries WHERE document_id=?", (row["id"],)
        ).fetchone()
        expected_family = family_key(
            target_owner,
            doc["filename"],
            doc["source_url"],
            identity_key=PROTOCOL + ":" + source_owner_hash + ":" + doc["family_id"],
            country=doc["country"],
        )
        if (
            any(mark.get(k) != v for k, v in origin.items())
            or doc["role"] != "primary"
            or any(row[k] != doc[k] for k in ("role", "version", "content_hash", "source_url", "size"))
            or row["case_id"] is not None
            or row["parent_id"] is not None
            or row["family_id"] != expected_family
            or country is None
            or country[0] != doc["country"]
        ):
            raise CollectionImportError("existing_dependency_identity_mismatch:" + ident)
        # add_relation may otherwise classify its source endpoint. A read-only
        # dependency must already belong to every collection used by the import.
        collections = {"country:" + doc["country"]} | {
            relation["collection"]
            for relation in source["relations"]
            if ident in (relation["source_document_id"], relation["target_document_id"])
        }
        for collection in collections:
            if not conn.execute(
                "SELECT 1 FROM legal_classifications WHERE document_id=? AND collection=?",
                (row["id"], collection),
            ).fetchone():
                raise CollectionImportError("existing_dependency_collection_missing:" + ident)
        if verify_blobs:
            path = _blob(target_dir, target_owner, doc["content_hash"])
            stored = Path(row["storage_path"])
            if not stored.is_absolute():
                stored = Path(target_dir).resolve() / stored
            if stored != path or path.stat().st_size != row["size"]:
                raise CollectionImportError("existing_dependency_original_path_mismatch:" + ident)
        identities[ident] = {"document_id": row["id"], "version": row["version"]}
    return identities


def _guard(conn, ident):
    doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (ident,)).fetchone())
    manifest = json.loads(doc["manifest"])
    manifest.pop("collection_import", None)
    metadata = [
        dict(x) for x in conn.execute("SELECT * FROM document_metadata WHERE document_id=?", (ident,))
    ]
    classes = [
        dict(x)
        for x in conn.execute(
            "SELECT * FROM legal_classifications WHERE document_id=? ORDER BY collection", (ident,)
        )
    ]
    return _sha(
        dump(
            {
                "hash": doc["content_hash"],
                "version": doc["version"],
                "role": doc["role"],
                "manifest": manifest,
                "metadata": metadata,
                "classes": classes,
            }
        ).encode()
    )


def _registry_assets(value, found, aliases):
    """A claimed path is an alias only: bytes always come from verified owner CAS."""
    if isinstance(value, dict):
        for path_key, hash_key in (
            ("full_page_PNG", "PNG_sha256"),
            ("full_page_PNG_path", "full_page_PNG_sha256"),
            ("mapping_path", "mapping_sha256"),
            ("PNG_CAS_path", "image_sha256"),
            ("source_PNG_path", "image_sha256"),
        ):
            if isinstance(value.get(path_key), str) and _DIGEST.fullmatch(str(value.get(hash_key, ""))):
                found.add(value[hash_key])
                _add_alias(aliases, value[path_key], value[hash_key])
        for item in value.values():
            _registry_assets(item, found, aliases)
    elif isinstance(value, list):
        for item in value:
            _registry_assets(item, found, aliases)


def _selected_registry(registry, selected_ids, digest):
    """Keep the selected relation closure without importing unrelated proofs."""
    relations = registry.get("relations", [])
    if not isinstance(relations, list) or any(not isinstance(row, dict) for row in relations):
        raise CollectionImportError("invalid_registry_relations")
    selected = set(selected_ids)
    kept, missing = [], set()
    for row in relations:
        endpoints = {row.get("source_document_id"), row.get("target_document_id")}
        if endpoints & selected:
            if any(not isinstance(item, str) or not item for item in endpoints):
                raise CollectionImportError("invalid_registry_endpoint")
            missing.update(endpoints - selected)
            kept.append(row)
    if missing:
        raise CollectionImportError("selected_registry_dependencies_required:" + ",".join(sorted(missing)))
    return {
        **registry,
        "relations": kept,
        "selection_provenance": {
            "protocol": "selected-relation-registry-v1",
            "source_registry_sha256": digest,
            "source_relations": len(relations),
            "selected_relations": len(kept),
        },
    }


def _plan(source_dir, target_dir, source_owner, target_owner, *, document_ids=None):
    source_dir, target_dir = Path(source_dir).resolve(), Path(target_dir).resolve()
    if source_dir == target_dir:
        raise CollectionImportError("source_and_target_must_differ")
    source = _read_db(source_dir, source_owner, document_ids=document_ids)
    blockers = []
    try:
        _read_db(target_dir, target_owner, target_only=True)
    except CollectionImportError as exc:
        blockers.append("target_" + str(exc))
    countries = {r["document_id"]: r["country"] for r in source["document_countries"]}
    metadata = {r["document_id"]: json.loads(r["payload"]) for r in source["document_metadata"]}
    classes = {}
    for r in source["legal_classifications"]:
        classes.setdefault(r["document_id"], []).append(r)
    documents = source["documents"]
    families = source["family_counts"]
    found, aliases, total = set(), {}, 0
    state_path = source_dir / "campaign.json"
    if state_path.is_file() and state_path.stat().st_size > MAX_TOTAL_MANIFEST_BYTES:
        raise CollectionImportError("campaign_evidence_too_large")
    state = json.loads(state_path.read_bytes()) if state_path.is_file() else {}
    if state and state.get("owner_id") != source_owner:
        raise CollectionImportError("campaign_owner_mismatch")
    evidence = {}
    for key, row in state.get("documents", {}).items():
        if row.get("document_id") in source["document_ids"]:
            if len(dump(row).encode()) > MAX_PROVENANCE_BYTES:
                raise CollectionImportError("document_evidence_too_large")
            if row["document_id"] in evidence and evidence[row["document_id"]] != row:
                raise CollectionImportError("ambiguous_document_evidence")
            evidence[row["document_id"]] = row
            _asset_refs(row, found, aliases)
            _path_refs(row, source_dir, source_owner, found, aliases)
    for doc in documents:
        ident = doc["id"]
        if doc["case_id"] is not None:
            blockers.append("case_bound_source_unsupported:" + ident)
        if doc["version"] != 1 or families[doc["family_id"]] != 1:
            blockers.append("unsupported_multiversion_family:" + ident)
        country = countries.get(ident, countries.get(doc["parent_id"]))
        if not country or not re.fullmatch(r"[A-Z]{2}", country):
            blockers.append("country_required:" + ident)
        if target_owner == SYSTEM_OWNER_ID and (
            doc["case_id"] is not None or not official_source_allowed(doc["source_url"], country)
        ):
            blockers.append("system_source_required:" + ident)
        if doc["role"] == "secondary" and doc["parent_id"] not in source["document_ids"]:
            blockers.append("owned_parent_required:" + ident)
        if len(doc["manifest"].encode()) > MAX_MANIFEST_BYTES:
            raise CollectionImportError("manifest_too_large")
        total += len(doc["manifest"].encode())
        if total > MAX_TOTAL_MANIFEST_BYTES:
            raise CollectionImportError("total_manifest_scope_exceeded")
        manifest = json.loads(doc["manifest"])
        _validate_retry_evidence(manifest, doc["content_hash"])
        if "same-owner-pdf-page-reuse-v1" in doc["manifest"]:
            blockers.append("source_same_owner_OCR_reuse_unsupported:" + ident)
        if doc["role"] == "secondary" and (manifest.get("pages") or manifest.get("chunks")):
            raise CollectionImportError("ordinary_attachment_contains_parsed_body")
        if doc["role"] == "primary":
            if manifest.get("source_hash") != doc["content_hash"]:
                raise CollectionImportError("manifest_original_hash_mismatch")
            if ident not in metadata or metadata[ident].get("kind") not in {"law", "guidance", "case"}:
                raise CollectionImportError("normative_metadata_required")
            from .catalog import DocumentMetadata

            if DocumentMetadata.model_validate(metadata[ident]).model_dump(mode="json") != metadata[ident]:
                raise CollectionImportError("unsupported_metadata_payload")
            if metadata[ident].get("review_status") != "draft":
                blockers.append("source_legal_review_must_be_draft:" + ident)
            if not any(c["collection"] == "country:" + country for c in classes.get(ident, [])):
                blockers.append("national_purpose_membership_required:" + ident)
        doc["country"] = country
        doc["parsed_manifest"] = manifest
        found.add(doc["content_hash"])
        _add_alias(aliases, doc["storage_path"], doc["content_hash"])
        _path_refs(manifest, source_dir, source_owner, found, aliases)
    registries = {}
    registry_hashes = set()
    for relation in source["relations"]:
        try:
            locator = json.loads(relation["locator"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(locator, dict):
            for key in ("registry_sha256", "source_registry_sha256"):
                if key in locator:
                    digest = locator[key]
                    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                        raise CollectionImportError("invalid_registry_digest")
                    registry_hashes.add(digest)
    if len(registry_hashes) > MAX_RELATION_REGISTRIES:
        raise CollectionImportError("relation_registry_scope_exceeded")
    registry_bytes = 0
    for registry_digest in sorted(registry_hashes):
        raw = _blob(source_dir, source_owner, registry_digest).read_bytes()
        if len(raw) > MAX_PROVENANCE_BYTES:
            raise CollectionImportError("registry_too_large")
        registry_bytes += len(raw)
        if registry_bytes > MAX_TOTAL_REGISTRY_BYTES:
            raise CollectionImportError("total_registry_scope_exceeded")
        registry = json.loads(raw)
        if registry.get("owner_id") != source_owner:
            raise CollectionImportError("registry_owner_mismatch")
        if document_ids is not None:
            registry = _selected_registry(registry, source["document_ids"], registry_digest)
        registries[registry_digest] = registry
        _registry_assets(registry, found, aliases)
        _path_refs(registry, source_dir, source_owner, found, aliases)
        for ref in _mapping_records(registry):
            mapping_raw = _blob(source_dir, source_owner, ref["mapping_sha256"]).read_bytes()
            if len(mapping_raw) > MAX_PROVENANCE_BYTES:
                raise CollectionImportError("mapping_too_large")
            # Old proof paths are aliases, not read authority. Paired image
            # hashes still resolve exclusively through verified source-owner CAS.
            _registry_assets(json.loads(mapping_raw), found, aliases)
        found.add(_sha(raw))
    if len(found) > MAX_ASSETS:
        raise CollectionImportError("asset_scope_exceeded")
    paths = {digest: _blob(source_dir, source_owner, digest) for digest in found}
    if sum(path.stat().st_size for path in paths.values()) > MAX_TOTAL_BLOB_BYTES:
        raise CollectionImportError("total_blob_scope_exceeds_2GiB")
    for original, digest in list(aliases.items()):
        if not Path(original).is_absolute():
            _add_alias(aliases, str(source_dir.parents[2] / original), digest)
    fingerprint = _fingerprint()
    vectors = {}
    for doc in documents:
        ident = doc["id"]
        national = next(
            (r for r in classes.get(ident, []) if r["collection"] == "country:" + doc["country"]), {}
        )
        if (
            doc["role"] != "primary"
            or national.get("level") != "primary"
            or not text_complete(doc["parsed_manifest"])
        ):
            continue
        index = source["indices"].get(ident)
        if (
            not index
            or not index["complete"]
            or index["source_hash"] != doc["content_hash"]
            or ident in source["dirty"]
        ):
            continue
        candidates = [
            v for v in source["vectors"] if v["document_id"] == ident and v["fingerprint"] == fingerprint
        ]
        chunks = {
            str(c.get("id", "")): c
            for c in doc["parsed_manifest"].get("chunks", [])
            if str(c.get("text", "")).strip()
        }
        # A paid batch can be committed before the whole-document model marker.
        # Its index BLOBs are still reusable when every saved input is current.
        if not fingerprint or not candidates or not chunks:
            continue
        validated = {}
        for row in candidates:
            chunk = chunks.get(row["chunk_id"])
            if (
                chunk is None
                or row["fingerprint"] != fingerprint
                or row["input_hash"] != input_hash(embedding_text(chunk))
                or row["vector_input_hash"] != row["input_hash"]
                or row["text"] != chunk["text"]
                or row["retrieval_text"] != embedding_text(chunk)
            ):
                raise CollectionImportError("vector_input_or_fingerprint_mismatch")
            import numpy as np

            vector = np.frombuffer(row["vector"], dtype="float32")
            if (
                len(vector) != row["dimension"]
                or not np.isfinite(vector).all()
                or not np.isclose(np.linalg.norm(vector), 1, atol=1e-5)
            ):
                raise CollectionImportError("invalid_source_vector")
            validated[row["chunk_id"]] = normalized_vector(vector)
        if len({len(v) for v in validated.values()}) != 1:
            raise CollectionImportError("source_vector_dimension_drift")
        vectors[ident] = validated
    # Validate transferred evidence during read-only preflight, before copying
    # originals or beginning any destination transaction.
    planned_destinations = {
        digest: str(Path("files/blobs") / _sha(target_owner.encode()) / digest[:2] / digest[2:4] / digest)
        for digest in paths
    }
    # Mapping derivation later registers these target CAS paths as aliases.
    # Reserve them now so any collision is rejected before destination writes.
    for digest, destination in planned_destinations.items():
        _add_alias(aliases, destination, digest)
    planned_identities = {
        doc["id"]: {"document_id": doc["id"], "version": doc["version"]} for doc in documents
    }
    for doc in documents:
        _rewrite_manifest(
            doc["parsed_manifest"],
            aliases,
            planned_destinations,
            planned_identities,
            bound_hash=doc["content_hash"],
            target_owner=target_owner,
        )
        if doc["id"] in evidence:
            _rewrite(
                evidence[doc["id"]],
                aliases,
                planned_destinations,
                planned_identities,
                bound_hash=doc["content_hash"],
                target_owner=target_owner,
            )
    public = {
        "protocol": PROTOCOL,
        "mode": "dry_run",
        "source_as_of": source["as_of"],
        "source_owner_sha256": _sha(source_owner.encode()),
        "target_owner_sha256": _sha(target_owner.encode()),
        "documents": len(documents),
        "selected_document_ids": source["document_ids"] if document_ids is not None else None,
        "ordinary_attachments": sum(d["role"] == "secondary" for d in documents),
        "complete_text_documents": sum(
            d["role"] == "primary" and text_complete(d["parsed_manifest"]) for d in documents
        ),
        "reusable_vectors": sum(len(v) for v in vectors.values()),
        "partial_vector_cache_documents": sum(
            bool(vectors.get(doc["id"]))
            and (
                len(vectors[doc["id"]])
                != sum(bool(str(c.get("text", "")).strip()) for c in doc["parsed_manifest"].get("chunks", []))
                or doc["parsed_manifest"].get("embedding_fingerprint") != fingerprint
            )
            for doc in documents
        ),
        "verified_owner_blobs": len(paths),
        "source_evidence_documents": len(evidence),
        "relations": len(source["relations"]),
        "relation_registries": len(registries),
        "blockers": blockers,
        "target_writes": 0,
        "model_calls": 0,
        "HTTP_calls": 0,
        "formal_RAG_published": False,
    }
    return source, metadata, classes, paths, aliases, registries, vectors, public, evidence


def _private_proof_paths(value, *, text=False):
    """Derived mapping metadata must not reveal an old server directory."""
    if isinstance(value, list):
        return [_private_proof_paths(x, text=text) for x in value]
    if isinstance(value, dict):
        return {
            k: _private_proof_paths(
                v, text=k in {"text", "native_text", "native_text_unchanged", "raw_native_fragment"}
            )
            for k, v in value.items()
        }
    if isinstance(value, str) and value.startswith("/") and not text:
        return "untransferred-source-path-sha256:" + _sha(value.encode())
    return value


def _mapping_records(value):
    if isinstance(value, dict):
        if "mapping_path" in value and "mapping_sha256" in value:
            yield value
        for item in value.values():
            yield from _mapping_records(item)
    elif isinstance(value, list):
        for item in value:
            yield from _mapping_records(item)


def _replace_mapping_hashes(value, replacements):
    if isinstance(value, list):
        return [_replace_mapping_hashes(x, replacements) for x in value]
    if not isinstance(value, dict):
        return value
    return {
        k: replacements.get(v, v)
        if k in {"mapping_sha256", "source_mapping_SHA"} and isinstance(v, str)
        else _replace_mapping_hashes(v, replacements)
        for k, v in value.items()
    }


def _validate_registry(registry, documents, paths):
    docs = {d["id"]: d for d in documents}
    if not registry:
        return
    for rel in registry.get("relations", []):
        source, target = docs.get(rel.get("source_document_id")), docs.get(rel.get("target_document_id"))
        if not source or not target or source["role"] != "primary" or target["role"] != "primary":
            raise CollectionImportError("registry_endpoint_not_owned_normative_source")
        target_hash = rel.get("target_hash", rel.get("target_source_hash"))
        if (
            source["version"] != rel.get("source_version")
            or source["content_hash"] != rel.get("source_hash")
            or target["version"] != rel.get("target_version")
            or target["content_hash"] != target_hash
            or source["country"] != target["country"]
        ):
            raise CollectionImportError("registry_version_or_original_mismatch")
        fact = rel.get("source_fact", {})
        for key, doc in (("source_locator", source), ("target_identity", target)):
            if key not in fact:
                continue
            ref = fact[key]
            mapping = json.loads(paths[ref["mapping_sha256"]].read_bytes())
            page = next(
                (p for p in doc["parsed_manifest"].get("pages", []) if p.get("page") == ref["physical_page"]),
                None,
            )
            if (
                page is None
                or mapping.get("source_hash") != doc["content_hash"]
                or mapping.get("physical_page") != ref["physical_page"]
                or mapping.get("image_sha256") != ref["PNG_sha256"]
                or mapping.get("native_text_sha256") != _sha(str(page.get("native_text", "")).encode())
                or mapping.get("no_crop") is not True
            ):
                raise CollectionImportError("registry_physical_page_mapping_mismatch")
        for ref in rel.get("source_anchors", []):
            if (
                ref.get("document_id") != source["id"]
                or ref.get("version") != source["version"]
                or ref.get("source_sha256") != source["content_hash"]
            ):
                raise CollectionImportError("registry_anchor_identity_mismatch")
            page = next(
                (
                    p
                    for p in source["parsed_manifest"].get("pages", [])
                    if p.get("page") == ref.get("physical_page")
                ),
                None,
            )
            text = str(page.get("native_text", "")) if page else ""
            start, end = ref.get("native_char_start"), ref.get("native_char_end")
            if (
                page is None
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not 0 <= start < end <= len(text)
                or _sha(text.encode()) != ref.get("native_text_sha256")
                or text[start:end] != ref.get("raw_native_fragment")
                or _sha(text[start:end].encode()) != ref.get("fragment_sha256")
            ):
                raise CollectionImportError("registry_anchor_text_mapping_mismatch")


def import_collected_resources(
    source_dir,
    target_dir,
    source_owner,
    target_owner,
    *,
    apply=False,
    refresh=False,
    document_ids=None,
    existing_dependency_ids=None,
):
    """Dry-run never creates target directories, databases, users or sessions."""
    dependencies = set(_selected_ids(existing_dependency_ids) or [])
    selected = _selected_ids(document_ids)
    if dependencies and (selected is None or not dependencies < set(selected)):
        raise CollectionImportError("existing_dependencies_require_selected_new_documents")
    if dependencies and refresh:
        raise CollectionImportError("existing_dependencies_cannot_refresh")
    plan = _plan(source_dir, target_dir, source_owner, target_owner, document_ids=document_ids)
    source, metadata, classes, paths, aliases, registries, vectors, report, evidence = plan
    for registry in registries.values():
        _validate_registry(registry, source["documents"], paths)
    source_owner_hash = _sha(source_owner.encode())
    bound = {}
    if dependencies and not report["blockers"]:
        database = Path(target_dir).resolve() / "tea.sqlite3"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            bound = _existing_dependencies(
                conn, target_dir, target_owner, source_owner_hash, source, dependencies, verify_blobs=True
            )
    report.update(
        existing_dependency_documents=len(dependencies),
        existing_dependency_ids=sorted(dependencies),
        import_documents=len(source["documents"]) - len(dependencies),
    )
    if not apply:
        return report
    if report["blockers"]:
        raise CollectionImportError("import_preflight_blocked:" + ",".join(report["blockers"]))
    from .blob_store import BlobStore
    from .service import Service

    service = Service(target_dir)
    blobs = BlobStore(Path(target_dir) / "files", max_bytes=MAX_BLOB_BYTES)
    published = {}
    for digest, path in paths.items():
        with path.open("rb") as stream:
            blob = blobs.put(target_owner, iter(lambda: stream.read(1024 * 1024), b""))
        if blob.digest != digest:
            raise CollectionImportError("source_blob_changed_during_copy")
        published[digest] = blob
    destinations = {
        digest: str(blob.path.relative_to(Path(target_dir).resolve())) for digest, blob in published.items()
    }
    identities, created, unchanged = {}, 0, 0
    with service.db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE id=?", (target_owner,)).fetchone():
            raise CollectionImportError("target_owner_not_found")
        if dependencies:
            current_bound = _existing_dependencies(
                conn, target_dir, target_owner, source_owner_hash, source, dependencies, verify_blobs=True
            )
            if current_bound != bound:
                raise CollectionImportError("existing_dependency_changed_during_import")
            identities.update(current_bound)
        imported_ids = _target_import_ids(
            conn, target_owner, source_owner_hash, (doc["id"] for doc in source["documents"])
        )
        for doc in source["documents"]:
            ident = doc["id"]
            if ident in dependencies:
                continue
            origin = {
                "protocol": PROTOCOL,
                "source_owner_sha256": source_owner_hash,
                "source_document_id": ident,
                "source_family_id": doc["family_id"],
                "source_version": doc["version"],
                "source_hash": doc["content_hash"],
            }
            snapshot_hash = _sha(
                dump(
                    {
                        "manifest": doc["parsed_manifest"],
                        "metadata": metadata.get(ident),
                        "classes": classes.get(ident),
                        "origin": origin,
                        **({"source_evidence": evidence[ident]} if ident in evidence else {}),
                        "vectors": {key: _sha(v.tobytes()) for key, v in vectors.get(ident, {}).items()},
                    }
                ).encode()
            )
            existing = (
                conn.execute(
                    "SELECT * FROM documents WHERE id=? AND owner_id=?",
                    (imported_ids[ident], target_owner),
                ).fetchone()
                if ident in imported_ids
                else None
            )
            if existing:
                mark = json.loads(existing["manifest"])["collection_import"]
                job = conn.execute(
                    "SELECT * FROM processing_jobs WHERE document_id=?", (existing["id"],)
                ).fetchone()
                if job and (job["status"] == "processing" or job["lease_token"] or job["lease_until"]):
                    raise CollectionImportError("target_has_active_processing_lease")
                if mark.get("guard") != _guard(conn, existing["id"]):
                    raise CollectionImportError("target_changed_since_import")
                if (
                    not refresh
                    and mark["snapshot_hash"] == snapshot_hash
                    and mark.get("target_embedding_fingerprint") == _fingerprint()
                ):
                    identities[ident] = {"document_id": existing["id"], "version": existing["version"]}
                    unchanged += 1
                    continue
                if not refresh:
                    raise CollectionImportError("source_checkpoint_changed_requires_refresh")
                if metadata.get(ident) != mark.get("source_metadata") or classes.get(ident) != mark.get(
                    "source_classes"
                ):
                    raise CollectionImportError("refresh_must_preserve_metadata_and_purpose")
                target = existing
            else:
                level = (
                    "attachment"
                    if doc["role"] == "secondary"
                    else next(
                        r["level"] for r in classes[ident] if r["collection"] == "country:" + doc["country"]
                    )
                )
                parent = identities.get(doc["parent_id"], {}).get("document_id")
                from .archive import family_key

                identity_key = PROTOCOL + ":" + origin["source_owner_sha256"] + ":" + doc["family_id"]
                family = family_key(
                    target_owner,
                    doc["filename"],
                    doc["source_url"],
                    role=doc["role"],
                    parent_id=parent,
                    identity_key=identity_key,
                    country=doc["country"],
                )
                if conn.execute(
                    "SELECT 1 FROM documents WHERE owner_id=? AND family_id=?", (target_owner, family)
                ).fetchone():
                    raise CollectionImportError("existing_family_without_import_provenance")
                target = service.archive.store_original(
                    conn,
                    target_owner,
                    published[doc["content_hash"]],
                    doc["filename"],
                    doc["mime_type"],
                    doc["source_url"],
                    collection="country:" + doc["country"],
                    level=level,
                    tags=json.loads(doc["tags"]),
                    identity_key=identity_key,
                    parent_id=parent,
                    kind_hint=metadata.get(ident, {}).get("kind"),
                )
                created += 1
                if doc["role"] == "primary":
                    for c in classes[ident]:
                        service.archive.classify(
                            conn, target_owner, target, c["collection"], c["level"], json.loads(c["tags"])
                        )
                    current = conn.execute(
                        "SELECT revision FROM document_metadata WHERE document_id=?", (target["id"],)
                    ).fetchone()
                    revision = current[0] + 1 if current else 1
                    at = now()
                    conn.execute(
                        "INSERT OR REPLACE INTO document_metadata VALUES(?,?,?,?,?,?)",
                        (target["id"], dump(metadata[ident]), revision, None, None, at),
                    )
                    conn.execute(
                        "INSERT INTO document_metadata_history VALUES(?,?,?,?,?)",
                        (target["id"], revision, dump(metadata[ident]), target_owner, at),
                    )
            identities[ident] = {"document_id": target["id"], "version": target["version"]}
            manifest, chunk_ids = _rewrite_manifest(
                copy.deepcopy(doc["parsed_manifest"]),
                aliases,
                destinations,
                identities,
                bound_hash=doc["content_hash"],
                target_owner=target_owner,
            )
            target_vectors = {
                chunk_ids.get(key, key): vector for key, vector in vectors.get(ident, {}).items()
            }
            if ident in evidence:
                manifest["collection_evidence"] = {
                    "protocol": "collection-evidence-v1",
                    "source_original_sha256": doc["content_hash"],
                    "source_record_sha256": _sha(dump(evidence[ident]).encode()),
                    "record": _rewrite(
                        copy.deepcopy(evidence[ident]),
                        aliases,
                        destinations,
                        identities,
                        bound_hash=doc["content_hash"],
                        target_owner=target_owner,
                    ),
                }
            # Inline vectors are untrusted; only validated index BLOBs transfer.
            for chunk in manifest.get("chunks", []):
                chunk.pop("embedding", None)
                chunk.pop("embedding_input_hash", None)
            embedding_complete = (
                ident in vectors
                and len(vectors[ident])
                == sum(bool(str(c.get("text", "")).strip()) for c in manifest.get("chunks", []))
                and manifest.get("embedding_fingerprint") == _fingerprint()
            )
            cache_pending = ident in vectors and not embedding_complete
            if not embedding_complete:
                manifest.pop("embedding_fingerprint", None)
            origin.update(
                snapshot_hash=snapshot_hash,
                source_metadata=metadata.get(ident),
                source_classes=classes.get(ident),
                target_embedding_fingerprint=_fingerprint(),
            )
            manifest["collection_import"] = origin
            conn.execute(
                "UPDATE documents SET title=?,manifest=?,status=?,error=? WHERE id=?",
                (
                    doc["title"],
                    dump(manifest),
                    "stored"
                    if doc["role"] == "secondary"
                    else ("ready" if text_complete(manifest) and not cache_pending else "processing"),
                    doc["error"],
                    target["id"],
                ),
            )
            if doc["role"] == "primary":
                service.index.sync_document(conn, target["id"], manifest, strict_vectors=True)
                conn.execute(
                    "DELETE FROM retrieval_vectors WHERE chunk_rowid IN (SELECT rowid FROM retrieval_chunks WHERE document_id=?)",
                    (target["id"],),
                )
                if ident in vectors:
                    chunks = [
                        dict(r)
                        for r in conn.execute(
                            "SELECT rowid,chunk_id,input_hash FROM retrieval_chunks WHERE document_id=? ORDER BY ordinal",
                            (target["id"],),
                        )
                        if r["chunk_id"] in target_vectors
                    ]
                    service.index.store_vectors(
                        conn,
                        target["id"],
                        _fingerprint(),
                        chunks,
                        [target_vectors[r["chunk_id"]] for r in chunks],
                    )
                service.collection.queue_processing(conn, target["id"], target_owner)
                if text_complete(manifest):
                    # A validated text checkpoint is already the result of
                    # parsing. Complete imports must never pay for OCR again.
                    needs_embedding = bool(
                        _fingerprint()
                        and service.archive.can_index(conn, target["id"])
                        and not embedding_complete
                    )
                    conn.execute(
                        "UPDATE processing_jobs SET phase=?,status=?,attempts=0,available_at=0,"
                        "lease_token=NULL,lease_until=NULL,error=NULL WHERE document_id=? AND status='queued'",
                        (
                            "embed" if needs_embedding or embedding_complete else "parse",
                            "queued" if needs_embedding else "completed",
                            target["id"],
                        ),
                    )
                    conn.execute(
                        "UPDATE documents SET status=? WHERE id=?",
                        ("processing" if needs_embedding else "ready", target["id"]),
                    )
            else:
                conn.execute("DELETE FROM processing_jobs WHERE document_id=?", (target["id"],))
                conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (target["id"],))
            manifest["collection_import"]["guard"] = _guard(conn, target["id"])
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), target["id"]))
            conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (target["id"],))
        mapping_replacements = {}
        for registry in registries.values():
            for ref in _mapping_records(registry):
                digest = ref["mapping_sha256"]
                if digest in mapping_replacements:
                    continue
                mapping = json.loads(paths[digest].read_bytes())
                derived = _rewrite(mapping, aliases, destinations, identities, target_owner=target_owner)
                derived = _private_proof_paths(derived)
                derived["source_mapping_sha256"] = digest
                derived["transfer_protocol"] = PROTOCOL
                payload = dump(derived).encode()
                if len(payload) > MAX_PROVENANCE_BYTES:
                    raise CollectionImportError("derived_mapping_too_large")
                mapped = blobs.put_bytes(target_owner, payload)
                _add_alias(aliases, destinations[digest], digest)
                destinations[digest] = str(mapped.path.relative_to(Path(target_dir).resolve()))
                mapping_replacements[digest] = mapped.digest
        # Forward document references and mapping paths resolve only after all IDs exist.
        for source_id, identity in identities.items():
            if source_id in dependencies:
                continue
            row = conn.execute(
                "SELECT manifest FROM documents WHERE id=?", (identity["document_id"],)
            ).fetchone()
            current = _rewrite(
                json.loads(row[0]), aliases, destinations, identities, target_owner=target_owner
            )
            current = _replace_mapping_hashes(current, mapping_replacements)
            current["collection_import"]["guard"] = _guard(conn, identity["document_id"])
            conn.execute(
                "UPDATE documents SET manifest=? WHERE id=?", (dump(current), identity["document_id"])
            )
            # Guard is computed against the final manifest, excluding its import marker.
            current["collection_import"]["guard"] = _guard(conn, identity["document_id"])
            conn.execute(
                "UPDATE documents SET manifest=? WHERE id=?", (dump(current), identity["document_id"])
            )
            if (
                conn.execute("SELECT role FROM documents WHERE id=?", (identity["document_id"],)).fetchone()[
                    0
                ]
                == "primary"
            ):
                service.index.sync_document(conn, identity["document_id"], current, strict_vectors=True)
            conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (identity["document_id"],))
        registry_blobs = {}
        for source_digest, registry in registries.items():
            transferred_registry = _rewrite(
                registry, aliases, destinations, identities, target_owner=target_owner
            )
            transferred_registry = _replace_mapping_hashes(transferred_registry, mapping_replacements)
            transferred_registry["owner_id"] = target_owner
            transferred_registry["source_import"] = {
                "protocol": PROTOCOL,
                "source_owner_sha256": _sha(source_owner.encode()),
                "source_registry_sha256": source_digest,
            }
            payload = dump(transferred_registry).encode()
            if len(payload) > MAX_PROVENANCE_BYTES:
                raise CollectionImportError("derived_registry_too_large")
            registry_blob = blobs.put_bytes(target_owner, payload)
            registry_blobs[source_digest] = registry_blob
            destinations[registry_blob.digest] = str(
                registry_blob.path.relative_to(Path(target_dir).resolve())
            )
        for relation in source["relations"]:
            if (
                relation["source_document_id"] not in identities
                or relation["target_document_id"] not in identities
            ):
                raise CollectionImportError("relation_endpoint_outside_import_scope")
            locator = relation["locator"]
            try:
                parsed = json.loads(locator)
            except (json.JSONDecodeError, TypeError):
                parsed = {"source_locator": locator}
            if isinstance(parsed, dict) and (
                "registry_sha256" in parsed or "source_registry_sha256" in parsed
            ):
                for key, path_key in (
                    ("registry_sha256", "registry_path"),
                    ("source_registry_sha256", "source_registry_path"),
                ):
                    if key in parsed:
                        registry_blob = registry_blobs.get(parsed[key])
                        if registry_blob is None:
                            raise CollectionImportError("typed_relation_registry_required")
                        parsed[key] = registry_blob.digest
                        parsed[path_key] = str(registry_blob.path.relative_to(Path(target_dir).resolve()))
            parsed = _rewrite(parsed, aliases, destinations, identities)
            sid, tid = relation["source_document_id"], relation["target_document_id"]
            if isinstance(parsed, dict):
                parsed.update(
                    source_version=identities[sid]["version"], target_version=identities[tid]["version"]
                )
            service.archive.add_relation(
                target_owner,
                identities[sid]["document_id"],
                {
                    "collection": relation["collection"],
                    "target_document_id": identities[tid]["document_id"],
                    "relation": relation["relation"],
                    "locator": dump(parsed),
                },
                conn,
            )
        payload = dump(
            {
                "protocol": PROTOCOL,
                "source_as_of": source["as_of"],
                "source_owner_sha256": _sha(source_owner.encode()),
                "document_versions": identities,
                "source_original_versions": [
                    {"document_id": d["id"], "version": d["version"], "hash": d["content_hash"]}
                    for d in source["documents"]
                ],
            }
        ).encode()
        if len(payload) > MAX_PROVENANCE_BYTES:
            raise CollectionImportError("import_provenance_too_large")
        provenance = blobs.put_bytes(target_owner, payload)
    return report | {
        "mode": "applied",
        "target_writes": created,
        "unchanged_documents": unchanged,
        "document_versions": identities,
        "provenance_sha256": provenance.digest,
        "registry_sha256": next(iter(registry_blobs.values())).digest if len(registry_blobs) == 1 else None,
        "registry_sha256_by_source": {digest: blob.digest for digest, blob in registry_blobs.items()},
    }
