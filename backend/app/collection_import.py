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
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
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


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _read_db(directory, owner, *, target_only=False):
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
        size, largest = conn.execute(
            "SELECT COALESCE(SUM(length(CAST(manifest AS BLOB))),0),COALESCE(MAX(length(CAST(manifest AS BLOB))),0) FROM documents WHERE owner_id=?",
            (owner,),
        ).fetchone()
        if size > MAX_TOTAL_MANIFEST_BYTES or largest > MAX_MANIFEST_BYTES:
            raise CollectionImportError("total_manifest_scope_exceeded")
        documents = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM documents WHERE owner_id=? ORDER BY role,family_id,version,id LIMIT ?",
                (owner, MAX_DOCUMENTS + 1),
            )
        ]
        if len(documents) > MAX_DOCUMENTS:
            raise CollectionImportError("document_scope_exceeds_200")
        ids = [d["id"] for d in documents]
        result = {"as_of": now(), "documents": documents}
        for table in ("document_metadata", "document_countries", "legal_classifications"):
            result[table] = [
                dict(r)
                for r in conn.execute(
                    f"SELECT t.* FROM {table} t JOIN documents d ON d.id=t.document_id WHERE d.owner_id=?",
                    (owner,),
                )
            ]
        result["relations"] = [
            dict(r)
            for r in conn.execute("SELECT * FROM legal_relations WHERE owner_id=? ORDER BY id", (owner,))
        ]
        result["jobs"] = [
            dict(r) for r in conn.execute("SELECT * FROM processing_jobs WHERE owner_id=?", (owner,))
        ]
        result["indices"] = {
            r["document_id"]: dict(r)
            for r in conn.execute(
                "SELECT i.* FROM retrieval_documents i JOIN documents d ON d.id=i.document_id WHERE d.owner_id=?",
                (owner,),
            )
        }
        result["dirty"] = {
            r[0]
            for r in conn.execute(
                "SELECT i.document_id FROM retrieval_dirty i JOIN documents d ON d.id=i.document_id WHERE d.owner_id=?",
                (owner,),
            )
        }
        count, vector_bytes = conn.execute(
            """SELECT count(*),COALESCE(sum(length(v.vector)+length(CAST(c.text AS BLOB))+length(CAST(c.retrieval_text AS BLOB))),0) FROM retrieval_chunks c
            JOIN retrieval_vectors v ON v.chunk_rowid=c.rowid JOIN documents d ON d.id=c.document_id
            WHERE d.owner_id=?""",
            (owner,),
        ).fetchone()
        if count > 100000 or vector_bytes > MAX_TOTAL_MANIFEST_BYTES:
            raise CollectionImportError("vector_scope_exceeded")
        result["vectors"] = [
            dict(r)
            for r in conn.execute(
                """SELECT c.document_id,c.chunk_id,c.text,c.retrieval_text,c.input_hash,
                v.fingerprint,v.dimension,v.vector,v.input_hash vector_input_hash
                FROM retrieval_chunks c JOIN retrieval_vectors v ON v.chunk_rowid=c.rowid
                JOIN documents d ON d.id=c.document_id WHERE d.owner_id=? ORDER BY c.document_id,c.ordinal""",
                (owner,),
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


def _asset_refs(value, found, aliases):
    if isinstance(value, dict):
        if "cas_path" in value and isinstance(value.get("sha256"), str):
            found.add(value["sha256"])
            for key in ("cas_path", "source_path"):
                if isinstance(value.get(key), str):
                    aliases[value[key]] = value["sha256"]
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
                        aliases[item] = digest
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
            digest = aliases.get(item)
            if digest is None and key in {"source_original_path", "storage_path", "raw_path"}:
                digest = bound_hash
            if digest in destinations:
                result[key] = destinations[digest]
            elif item in destinations.values():
                result[key] = item  # Already mapped to a verified target CAS object.
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
    return result


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
                aliases[value[path_key]] = value[hash_key]
        for item in value.values():
            _registry_assets(item, found, aliases)
    elif isinstance(value, list):
        for item in value:
            _registry_assets(item, found, aliases)


def _plan(source_dir, target_dir, source_owner, target_owner):
    source_dir, target_dir = Path(source_dir).resolve(), Path(target_dir).resolve()
    if source_dir == target_dir:
        raise CollectionImportError("source_and_target_must_differ")
    source = _read_db(source_dir, source_owner)
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
    families = {}
    for doc in documents:
        families[doc["family_id"]] = families.get(doc["family_id"], 0) + 1
    found, aliases, total = set(), {}, 0
    state_path = source_dir / "campaign.json"
    state = json.loads(state_path.read_bytes()) if state_path.is_file() else {}
    if state and state.get("owner_id") != source_owner:
        raise CollectionImportError("campaign_owner_mismatch")
    evidence = {}
    for key, row in state.get("documents", {}).items():
        if row.get("document_id") in source["document_ids"]:
            evidence[row["document_id"]] = row
            _asset_refs(row, found, aliases)
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
        aliases[doc["storage_path"]] = doc["content_hash"]
        _path_refs(manifest, source_dir, source_owner, found, aliases)
    registry = None
    registry_hashes = set()
    for relation in source["relations"]:
        try:
            locator = json.loads(relation["locator"])
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(locator, dict):
            digest = locator.get("registry_sha256") or locator.get("source_registry_sha256")
            if digest:
                registry_hashes.add(digest)
    if len(registry_hashes) > 1:
        raise CollectionImportError("multiple_relation_registries_not_supported")
    if registry_hashes:
        registry_digest = next(iter(registry_hashes))
        raw = _blob(source_dir, source_owner, registry_digest).read_bytes()
        if len(raw) > 4 * 1024 * 1024:
            raise CollectionImportError("registry_too_large")
        registry = json.loads(raw)
        if registry.get("owner_id") != source_owner:
            raise CollectionImportError("registry_owner_mismatch")
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
            aliases[str(source_dir.parents[2] / original)] = digest
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
        candidates = [v for v in source["vectors"] if v["document_id"] == ident]
        chunks = {
            str(c.get("id", "")): c
            for c in doc["parsed_manifest"].get("chunks", [])
            if str(c.get("text", "")).strip()
        }
        if not fingerprint or doc["parsed_manifest"].get("embedding_fingerprint") != fingerprint:
            continue
        if len(candidates) != len(chunks) or not chunks:
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
    public = {
        "protocol": PROTOCOL,
        "mode": "dry_run",
        "source_as_of": source["as_of"],
        "source_owner_sha256": _sha(source_owner.encode()),
        "target_owner_sha256": _sha(target_owner.encode()),
        "documents": len(documents),
        "ordinary_attachments": sum(d["role"] == "secondary" for d in documents),
        "complete_text_documents": sum(
            d["role"] == "primary" and text_complete(d["parsed_manifest"]) for d in documents
        ),
        "reusable_vectors": sum(len(v) for v in vectors.values()),
        "verified_owner_blobs": len(paths),
        "relations": len(source["relations"]),
        "blockers": blockers,
        "target_writes": 0,
        "model_calls": 0,
        "HTTP_calls": 0,
        "formal_RAG_published": False,
    }
    return source, metadata, classes, paths, aliases, registry, vectors, public, evidence


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
    source_dir, target_dir, source_owner, target_owner, *, apply=False, refresh=False
):
    """Dry-run never creates target directories, databases, users or sessions."""
    plan = _plan(source_dir, target_dir, source_owner, target_owner)
    source, metadata, classes, paths, aliases, registry, vectors, report, evidence = plan
    _validate_registry(registry, source["documents"], paths)
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
        for doc in source["documents"]:
            ident = doc["id"]
            origin = {
                "protocol": PROTOCOL,
                "source_owner_sha256": _sha(source_owner.encode()),
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
                        "vectors": {key: _sha(v.tobytes()) for key, v in vectors.get(ident, {}).items()},
                    }
                ).encode()
            )
            existing = conn.execute(
                """SELECT * FROM documents WHERE owner_id=? AND
                   json_extract(manifest,'$.collection_import.source_owner_sha256')=? AND
                   json_extract(manifest,'$.collection_import.source_document_id')=?""",
                (target_owner, origin["source_owner_sha256"], ident),
            ).fetchone()
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
                    mark["snapshot_hash"] == snapshot_hash
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
            manifest = _rewrite(
                copy.deepcopy(doc["parsed_manifest"]),
                aliases,
                destinations,
                identities,
                bound_hash=doc["content_hash"],
                target_owner=target_owner,
            )
            # Inline vectors are untrusted; only validated index BLOBs transfer.
            for chunk in manifest.get("chunks", []):
                chunk.pop("embedding", None)
                chunk.pop("embedding_input_hash", None)
            if ident not in vectors:
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
                    else ("ready" if text_complete(manifest) else "processing"),
                    doc["error"],
                    target["id"],
                ),
            )
            if doc["role"] == "primary":
                service.index.sync_document(conn, target["id"], manifest, strict_vectors=True)
                if ident not in vectors:
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
                    ]
                    service.index.store_vectors(
                        conn,
                        target["id"],
                        _fingerprint(),
                        chunks,
                        [vectors[ident][r["chunk_id"]] for r in chunks],
                    )
                service.collection.queue_processing(conn, target["id"], target_owner)
            else:
                conn.execute("DELETE FROM processing_jobs WHERE document_id=?", (target["id"],))
                conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (target["id"],))
            manifest["collection_import"]["guard"] = _guard(conn, target["id"])
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), target["id"]))
            conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (target["id"],))
        mapping_replacements = {}
        if registry:
            for ref in _mapping_records(registry):
                digest = ref["mapping_sha256"]
                mapping = json.loads(paths[digest].read_bytes())
                derived = _rewrite(mapping, aliases, destinations, identities, target_owner=target_owner)
                derived = _private_proof_paths(derived)
                derived["source_mapping_sha256"] = digest
                derived["transfer_protocol"] = PROTOCOL
                payload = dump(derived).encode()
                if len(payload) > MAX_PROVENANCE_BYTES:
                    raise CollectionImportError("derived_mapping_too_large")
                mapped = blobs.put_bytes(target_owner, payload)
                aliases[destinations[digest]] = digest
                destinations[digest] = str(mapped.path.relative_to(Path(target_dir).resolve()))
                mapping_replacements[digest] = mapped.digest
        # Forward document references and mapping paths resolve only after all IDs exist.
        for identity in identities.values():
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
        transferred_registry = (
            _rewrite(registry, aliases, destinations, identities, target_owner=target_owner)
            if registry
            else None
        )
        transferred_registry = _replace_mapping_hashes(transferred_registry, mapping_replacements)
        if transferred_registry:
            transferred_registry["owner_id"] = target_owner
            transferred_registry["source_import"] = {
                "protocol": PROTOCOL,
                "source_owner_sha256": _sha(source_owner.encode()),
            }
            payload = dump(transferred_registry).encode()
            if len(payload) > MAX_PROVENANCE_BYTES:
                raise CollectionImportError("derived_registry_too_large")
            registry_blob = blobs.put_bytes(target_owner, payload)
            destinations[registry_blob.digest] = str(
                registry_blob.path.relative_to(Path(target_dir).resolve())
            )
        else:
            registry_blob = None
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
                if not registry_blob:
                    raise CollectionImportError("typed_relation_registry_required")
                for key, path_key in (
                    ("registry_sha256", "registry_path"),
                    ("source_registry_sha256", "source_registry_path"),
                ):
                    if key in parsed:
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
        "registry_sha256": registry_blob.digest if registry_blob else None,
    }
