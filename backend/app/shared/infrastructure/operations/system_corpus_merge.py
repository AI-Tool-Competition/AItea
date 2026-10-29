"""Explicit same-original aliases for an offline corpus migration.

The first merge policy accepts only a target checkpoint which already contains
every successful source page and vector input. It never guesses a legal version
or silently chooses between different successful texts. Both complete derived
checkpoints, including raw vector bytes, remain in owner-scoped immutable CAS.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from app.contexts.ingestion.application.collection_import import PROTOCOL as IMPORT_PROTOCOL
from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID
from app.contexts.documents.infrastructure.parsers.ingestion_quality import pdf_page_status, text_complete
from app.shared.domain.values import dump, now
from app.shared.infrastructure.persistence.blob_store import BlobStore
from app.shared.infrastructure.operations import system_corpus_migration as migration

PLAN_PROTOCOL = "system-corpus-alias-plan-v1"
ALIAS_PROTOCOL = "system-corpus-alias-v1"
CHECKPOINT_PROTOCOL = "system-corpus-derived-checkpoint-v1"
MARKER = "corpus_import_aliases"
MAX_ALIASES = 200
MAX_PLAN_BYTES = 16 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 128 * 1024 * 1024
ORIGIN_KEYS = (
    "protocol",
    "source_owner_sha256",
    "source_document_id",
    "source_family_id",
    "source_version",
    "source_hash",
)


def _validate_target_schema(conn):
    """Resume a legacy merge after Service upgrades, without adopting cloud evidence."""
    version = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
    migration._require(
        version in (19, 20, 21, 22, 23, 24, 25, 26, 27, 28), "alias_target_schema19_to28_required"
    )
    tables = []
    if version >= 20:
        tables.append("document_parse_tasks")
    if version >= 21:
        tables.extend(("ragflow_datasets", "ragflow_documents"))
    for table in tables:
        migration._require(
            conn.execute(f"SELECT 1 FROM {table} WHERE owner_id=? LIMIT 1", (SYSTEM_OWNER_ID,)).fetchone()
            is None,
            "alias_target_has_cloud_evidence",
        )


def _encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _ref(raw):
    digest = migration._sha(raw)
    return {
        "sha256": digest,
        "bytes": len(raw),
        "cas_path": str(
            Path("files/blobs") / migration._sha(SYSTEM_OWNER_ID.encode()) / digest[:2] / digest[2:4] / digest
        ),
    }


def _read_ref(ref, paths):
    migration._require(isinstance(ref, dict), "alias_archive_reference_required")
    digest = ref.get("sha256")
    path = paths.get(digest)
    migration._require(
        path is not None
        and path.is_file()
        and not path.is_symlink()
        and 0 < ref.get("bytes", 0) <= MAX_CHECKPOINT_BYTES
        and path.stat().st_size == ref["bytes"],
        "alias_archive_missing_or_size_mismatch",
    )
    raw = path.read_bytes()
    migration._require(_ref(raw) == ref, "alias_archive_hash_or_owner_mismatch")
    return raw


def _package(scope):
    """Archive all source rows; only BLOBs become lossless CAS references."""
    rows = copy.deepcopy(scope["rows"])
    objects = {}
    for vector in rows["retrieval_vectors"]:
        raw = vector["vector"]
        ref = _ref(raw)
        objects[ref["sha256"]] = raw
        vector["vector"] = ref
    value = {
        "protocol": CHECKPOINT_PROTOCOL,
        "owner_id": SYSTEM_OWNER_ID,
        "rows": rows,
        "rows_sha256": migration._hash_json(scope["rows"]),
    }
    raw = _encoded(value)
    migration._require(len(raw) <= MAX_CHECKPOINT_BYTES, "alias_checkpoint_too_large")
    ref = _ref(raw)
    objects[ref["sha256"]] = raw
    return ref, objects


def _unpack(ref, paths):
    value = json.loads(_read_ref(ref, paths))
    migration._require(
        value.get("protocol") == CHECKPOINT_PROTOCOL and value.get("owner_id") == SYSTEM_OWNER_ID,
        "alias_checkpoint_protocol_mismatch",
    )
    rows = value["rows"]
    migration._require(
        set(rows) == {*migration._TABLES, "retrieval_models"}
        and len(rows["documents"]) == 1
        and not rows["legal_relations"],
        "alias_checkpoint_scope_mismatch",
    )
    for vector in rows["retrieval_vectors"]:
        vector["vector"] = _read_ref(vector["vector"], paths)
    migration._require(migration._hash_json(rows) == value["rows_sha256"], "alias_checkpoint_rows_mismatch")
    return rows


def validate_alias_archives(manifest, owner, paths, *, document_id=None, content_hash=None):
    """Backup/export validation follows every receipt and raw paid result."""
    entries = manifest.get(MARKER)
    if entries is None:
        return
    migration._require(
        owner == SYSTEM_OWNER_ID and isinstance(entries, list) and 1 <= len(entries) <= MAX_ALIASES,
        "alias_manifest_scope_mismatch",
    )
    seen = set()
    for marker in entries:
        migration._require(isinstance(marker, dict), "alias_marker_required")
        receipt = json.loads(_read_ref(marker.get("receipt"), paths))
        migration._require(
            receipt.get("protocol") == ALIAS_PROTOCOL
            and marker.get("protocol") == ALIAS_PROTOCOL
            and marker.get("binding") == receipt.get("binding")
            and marker.get("plan_sha256") == receipt.get("plan_sha256"),
            "alias_receipt_binding_mismatch",
        )
        binding = receipt["binding"]
        migration._require(
            (document_id is None or binding["target_document_id"] == document_id)
            and (content_hash is None or binding["content_hash"] == content_hash),
            "alias_active_document_binding_mismatch",
        )
        source_id = binding["source_document_id"]
        migration._require(source_id not in seen, "duplicate_alias_binding")
        seen.add(source_id)
        plan = json.loads(_read_ref(receipt["plan"], paths))
        _validate_plan(plan)
        migration._require(plan["plan_sha256"] == marker["plan_sha256"], "alias_plan_binding_mismatch")
        pair = next((item for item in plan["aliases"] if item["binding"] == binding), None)
        migration._require(pair is not None, "alias_plan_pair_missing")
        for key, ident in (("source", source_id), ("target_before", binding["target_document_id"])):
            rows = _unpack(receipt[key], paths)
            doc = rows["documents"][0]
            migration._require(
                doc["id"] == ident
                and doc["owner_id"] == SYSTEM_OWNER_ID
                and doc["content_hash"] == binding["content_hash"]
                and doc["family_id"] == binding["family_id"]
                and doc["version"] == binding["version"]
                and migration._hash_json(rows) == pair[key + "_rows_sha256"],
                "alias_checkpoint_identity_mismatch",
            )
            original = paths.get(doc["content_hash"])
            migration._require(
                original is not None and original.stat().st_size == doc["size"], "alias_original_missing"
            )
            # Historical absolute storage_path is an audit value, never a read
            # authority. Actual originals and page assets resolve from this CAS.
            migration._cas_reference(json.loads(doc["manifest"]), original.parents[5], paths)


def _validate_plan(plan):
    migration._require(
        isinstance(plan, dict) and plan.get("protocol") == PLAN_PROTOCOL, "alias_plan_protocol_mismatch"
    )
    digest = plan.get("plan_sha256")
    migration._require(
        digest == migration._hash_json({k: v for k, v in plan.items() if k != "plan_sha256"}),
        "alias_plan_hash_mismatch",
    )
    pairs = plan.get("aliases")
    migration._require(isinstance(pairs, list) and 1 <= len(pairs) <= MAX_ALIASES, "alias_plan_scope_invalid")
    for field in ("source_document_id", "target_document_id"):
        values = [item["binding"][field] for item in pairs]
        migration._require(len(set(values)) == len(values), "alias_plan_must_be_one_to_one")


def _selection(candidates):
    migration._require(
        isinstance(candidates, list) and 1 <= len(candidates) <= MAX_ALIASES, "alias_selection_required"
    )
    pairs = []
    for row in candidates:
        migration._require(isinstance(row, dict), "alias_selection_invalid")
        source, target = row.get("source_document_id"), row.get("target_document_id")
        migration._require(
            all(isinstance(item, str) and 0 < len(item) <= 128 for item in (source, target))
            and source != target,
            "alias_selection_invalid",
        )
        pairs.append((source, target))
    migration._require(
        len({a for a, _ in pairs}) == len(pairs) == len({b for _, b in pairs}),
        "alias_selection_must_be_one_to_one",
    )
    return sorted(pairs)


def _origin(doc):
    marker = json.loads(doc["manifest"]).get("collection_import", {})
    value = {key: marker.get(key) for key in ORIGIN_KEYS}
    migration._require(
        value["protocol"] == IMPORT_PROTOCOL and all(item is not None for item in value.values()),
        "alias_original_import_identity_required",
    )
    return value


def _pair(source, target):
    a, b = source["rows"]["documents"], target["rows"]["documents"]
    migration._require(len(a) == len(b) == 1, "alias_document_missing_or_not_system_owned")
    a, b = a[0], b[0]
    identity_fields = (
        "owner_id",
        "family_id",
        "version",
        "content_hash",
        "size",
        "source_url",
        "role",
        "case_id",
        "parent_id",
    )
    migration._require(
        all(a[key] == b[key] for key in identity_fields)
        and a["role"] == "primary"
        and a["case_id"] is None
        and a["parent_id"] is None
        and _origin(a) == _origin(b),
        "alias_original_identity_mismatch",
    )
    countries = [s["rows"]["document_countries"] for s in (source, target)]
    migration._require(
        all(len(items) == 1 for items in countries)
        and countries[0][0]["country"] == countries[1][0]["country"],
        "alias_country_mismatch",
    )
    country = countries[0][0]["country"]

    def classes(scope):
        return sorted(
            [r["collection"], r["level"], r["tags"]] for r in scope["rows"]["legal_classifications"]
        )

    migration._require(classes(source) == classes(target), "alias_purpose_mismatch")
    meta = [s["rows"]["document_metadata"] for s in (source, target)]
    migration._require(
        all(len(rows) == 1 for rows in meta)
        and all(
            meta[0][0][k] == meta[1][0][k] for k in ("payload", "revision", "reviewed_by", "reviewed_at")
        ),
        "alias_legal_metadata_mismatch",
    )
    am, bm = json.loads(a["manifest"]), json.loads(b["manifest"])
    migration._require(MARKER not in am and MARKER not in bm, "alias_already_bound_use_existing_plan")
    pages = []
    ap = {p["page"]: p for p in am.get("pages", [])}
    bp = {p["page"]: p for p in bm.get("pages", [])}
    migration._require(
        len(ap) == len(am.get("pages", []))
        and len(bp) == len(bm.get("pages", []))
        and ap.keys() == bp.keys(),
        "alias_physical_page_scope_changed",
    )
    valid = {"native", "ocr_complete"}
    rank = {"pending_ocr": 0, "ocr_failed": 1, "ocr_uncertain": 2, "native": 3, "ocr_complete": 3}
    for number in sorted(ap):
        sp, tp = ap[number], bp[number]
        ss, ts = pdf_page_status(sp), pdf_page_status(tp)
        migration._require(
            sp.get("source_hash") == tp.get("source_hash") == a["content_hash"],
            "alias_page_original_mismatch",
        )
        migration._require(
            not (ss in valid and ts not in valid), "alias_source_successful_page_requires_reconciliation"
        )
        migration._require(
            not (ss in valid and sp.get("text") != tp.get("text")),
            "alias_successful_text_conflict_requires_reconciliation",
        )
        migration._require(
            ss == ts or rank.get(ss, 99) <= rank.get(ts, -1),
            "alias_source_page_progress_requires_reconciliation",
        )
        migration._require(
            (sp.get("ocr") or {}).get("attempts", 0) <= (tp.get("ocr") or {}).get("attempts", 0),
            "alias_source_OCR_attempts_require_reconciliation",
        )
        pages.append(
            {
                "page": number,
                "source_sha256": migration._hash_json(sp),
                "target_sha256": migration._hash_json(tp),
                "source_status": ss,
                "target_status": ts,
                "selected": "target",
            }
        )
    sc = {r["chunk_id"]: r for r in source["rows"]["retrieval_chunks"]}
    tc = {r["chunk_id"]: r for r in target["rows"]["retrieval_chunks"]}
    # Also protect complete non-PDF text and successful page text represented by
    # chunks; page identity alone is not a reason to discard source paragraphs.
    for key, row in sc.items():
        if not ap or text_complete(am):
            migration._require(
                key in tc
                and all(row[f] == tc[key][f] for f in ("text", "retrieval_text", "input_hash", "locator")),
                "alias_source_text_requires_reconciliation",
            )
    tv = {(r["chunk_id"], r["fingerprint"], r["input_hash"]): r for r in target["rows"]["retrieval_vectors"]}
    vectors = []
    for row in source["rows"]["retrieval_vectors"]:
        key = (row["chunk_id"], row["fingerprint"], row["input_hash"])
        old = tv.get(key)
        migration._require(
            old is not None
            and row["dimension"] == old["dimension"]
            and key[0] in tc
            and key[0] in sc
            and sc[key[0]]["retrieval_text"] == tc[key[0]]["retrieval_text"],
            "alias_source_vector_requires_reconciliation",
        )
        vectors.append(
            {
                "chunk_id": key[0],
                "fingerprint": key[1],
                "input_hash": key[2],
                "source_blob_sha256": migration._sha(row["vector"]),
                "target_blob_sha256": migration._sha(old["vector"]),
                "selected": "target",
            }
        )
    return {
        "binding": {
            "source_document_id": a["id"],
            "target_document_id": b["id"],
            "family_id": a["family_id"],
            "version": a["version"],
            "content_hash": a["content_hash"],
            "source_url": a["source_url"],
            "country": country,
            "classifications": classes(source),
            "original_import": _origin(a),
        },
        "source_rows_sha256": migration._hash_json(source["rows"]),
        "target_before_rows_sha256": migration._hash_json(target["rows"]),
        "source_manifest_sha256": migration._sha(a["manifest"].encode()),
        "target_manifest_sha256": migration._sha(b["manifest"].encode()),
        "source_text_complete": text_complete(am),
        "target_text_complete": text_complete(bm),
        "pages": pages,
        "vectors": vectors,
        "source_only_successful_pages": 0,
        "source_only_vectors": 0,
    }


def _references_alias(value, aliases):
    if isinstance(value, dict):
        return any(
            (
                key in {"document_id", "source_document_id", "target_document_id", "parent_id"}
                and isinstance(item, str)
                and item in aliases
            )
            or _references_alias(item, aliases)
            for key, item in value.items()
        )
    return isinstance(value, list) and any(_references_alias(item, aliases) for item in value)


def _no_cross_alias_refs(conn, aliases, paths):
    for row in conn.execute(
        "SELECT id,parent_id,manifest FROM documents WHERE owner_id=?", (SYSTEM_OWNER_ID,)
    ):
        migration._require(row["parent_id"] not in aliases, "alias_parent_requires_reconciliation")
        manifest = json.loads(row["manifest"])
        evidence = manifest.get("collection_evidence")
        if isinstance(evidence, dict) and isinstance(evidence.get("record"), dict):
            record = evidence["record"]
            if record.get("document_id") == row["id"]:
                # The source record identifies itself; it is retained verbatim
                # inside the archive, not a live donor or relationship edge.
                manifest["collection_evidence"] = evidence | {
                    "record": {key: value for key, value in record.items() if key != "document_id"}
                }
        migration._require(
            not _references_alias(manifest, aliases), "alias_manifest_donor_requires_reconciliation"
        )
    for row in conn.execute("SELECT * FROM legal_relations WHERE owner_id=?", (SYSTEM_OWNER_ID,)):
        migration._require(
            row["source_document_id"] not in aliases and row["target_document_id"] not in aliases,
            "alias_relation_requires_reconciliation",
        )
        try:
            locator = json.loads(row["locator"])
        except (ValueError, TypeError):
            continue
        migration._require(
            not _references_alias(locator, aliases), "alias_relation_proof_requires_reconciliation"
        )
        for key in ("registry_sha256", "source_registry_sha256"):
            if locator.get(key) in paths:
                migration._require(
                    not _references_alias(json.loads(paths[locator[key]].read_bytes()), aliases),
                    "alias_registry_requires_reconciliation",
                )


def _source_state(conn, receipt, source_dir, paths):
    summary = migration._stream_summary(conn)
    migration._principal(summary, required=True)
    expected = summary if receipt["protocol"] == migration.STREAM_PROTOCOL else migration._scope(conn)
    migration._require(
        expected["scope_sha256"] == receipt["source_scope_sha256"], "snapshot_scope_hash_mismatch"
    )
    migration._require(not summary["has_leases"], "source_snapshot_has_processing_lease")
    migration._require(
        not conn.execute(
            "SELECT 1 FROM documents WHERE owner_id=? GROUP BY family_id,version HAVING count(*)>1 LIMIT 1",
            (SYSTEM_OWNER_ID,),
        ).fetchone(),
        "immutable_family_version_conflict",
    )
    if summary["schema_version"] >= 19:
        migration._require(
            not conn.execute(
                "SELECT 1 FROM collection_recovery_jobs WHERE owner_id=? LIMIT 1", (SYSTEM_OWNER_ID,)
            ).fetchone(),
            "recovery_ledger_requires_full_backup_restore",
        )
    lookup = migration._source_lookup(conn)

    def vectors(ident, digest):
        return (
            dict(row)
            for row in conn.execute(
                "SELECT v.*,c.retrieval_text FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id=? AND c.input_hash=?",
                (ident, digest),
            )
        )

    for ids in migration._batches(summary):
        migration._validate_source(
            migration._scope(conn, ids), source_dir, [], paths=paths, lookup=lookup, vector_lookup=vectors
        )
    empty = migration._scope(conn, [])
    for row in conn.execute("SELECT * FROM legal_relations WHERE owner_id=? ORDER BY id", (SYSTEM_OWNER_ID,)):
        empty["rows"]["legal_relations"] = [dict(row)]
        migration._validate_source(empty, source_dir, [], paths=paths, lookup=lookup)
    return summary


def _relations(conn):
    return {
        row["id"]: migration._hash_json(dict(row))
        for row in conn.execute(
            "SELECT * FROM legal_relations WHERE owner_id=? ORDER BY id", (SYSTEM_OWNER_ID,)
        )
    }


def _validate_target_checkpoint(scope, conn, directory, paths):
    def vectors(ident, digest):
        return (
            dict(row)
            for row in conn.execute(
                "SELECT v.*,c.retrieval_text FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id=? AND c.input_hash=?",
                (ident, digest),
            )
        )

    migration._validate_source(
        scope, directory, [], paths=paths, lookup=migration._source_lookup(conn), vector_lookup=vectors
    )


def create_alias_plan(snapshot_dir, target_dir, candidates):
    """Read-only plan generation. Persist its returned JSON explicitly."""
    snapshot_dir, target_dir = Path(snapshot_dir).resolve(), Path(target_dir).resolve()
    receipt = migration._snapshot_receipt(snapshot_dir)
    source_dir = Path(receipt["source_data_dir"]).resolve()
    migration._require(target_dir not in {source_dir, snapshot_dir}, "source_snapshot_target_must_differ")
    pairs = _selection(candidates)
    paths = migration._validated_paths(source_dir, receipt["CAS_blobs"])
    target_paths = _owner_paths(target_dir)
    with (
        closing(migration._readonly(snapshot_dir / "tea.sqlite3")) as source,
        closing(migration._readonly(target_dir / "tea.sqlite3")) as target,
    ):
        summary = _source_state(source, receipt, source_dir, paths)
        baseline = migration._stream_summary(target)
        _validate_target_schema(target)
        migration._require(not baseline["has_leases"], "alias_target_without_leases_required")
        migration._principal(baseline, required=True)
        aliases = {a for a, _ in pairs}
        _no_cross_alias_refs(source, aliases, paths)
        entries = []
        for a, b in pairs:
            ss, ts = migration._scope(source, [a]), migration._scope(target, [b])
            entry = _pair(ss, ts)
            _validate_target_checkpoint(ts, target, target_dir, target_paths)
            # Validate target originals through exact owner CAS, never only the
            # content_hash copied from its document row.
            from app.contexts.ingestion.application.collection_import import _blob

            blob = _blob(target_dir, SYSTEM_OWNER_ID, entry["binding"]["content_hash"])
            migration._require(
                blob.stat().st_size == ts["rows"]["documents"][0]["size"],
                "alias_target_original_size_mismatch",
            )
            entries.append(entry)
        source_ids, target_ids = set(summary["document_ids"]), set(baseline["document_ids"])
        migration._require(
            aliases <= source_ids and not aliases & target_ids, "alias_source_id_already_exists_in_target"
        )
        source_relations, target_relations = _relations(source), _relations(target)
        migration._require(
            all(target_relations[k] == v for k, v in source_relations.items() if k in target_relations),
            "target_relation_id_conflict",
        )
        plan = {
            "protocol": PLAN_PROTOCOL,
            "created_at": now(),
            "target_dir": str(target_dir),
            "snapshot_db_sha256": receipt["snapshot_db_sha256"],
            "source_scope_sha256": summary["scope_sha256"],
            "source_document_ids": summary["document_ids"],
            "source_relations": source_relations,
            "target_document_guards": {
                ident: migration._hash_json(migration._scope(target, [ident])["rows"])
                for ident in baseline["document_ids"]
            },
            "target_relations": target_relations,
            "aliases": entries,
            "expected_system_documents": len(source_ids | target_ids) - len(aliases),
            "expected_relations": len(source_relations | target_relations),
        }
        plan["plan_sha256"] = migration._hash_json(plan)
        migration._require(len(_encoded(plan)) <= MAX_PLAN_BYTES, "alias_plan_too_large")
        return plan


def _archive_pair(pair, plan, source_scope, target_scope):
    source_ref, objects = _package(source_scope)
    target_ref, target_objects = _package(target_scope)
    objects.update(target_objects)
    plan_raw = _encoded(plan)
    plan_ref = _ref(plan_raw)
    objects[plan_ref["sha256"]] = plan_raw
    receipt = {
        "protocol": ALIAS_PROTOCOL,
        "binding": pair["binding"],
        "plan_sha256": plan["plan_sha256"],
        "plan": plan_ref,
        "source": source_ref,
        "target_before": target_ref,
    }
    receipt_raw = _encoded(receipt)
    receipt_ref = _ref(receipt_raw)
    objects[receipt_ref["sha256"]] = receipt_raw
    marker = {
        "protocol": ALIAS_PROTOCOL,
        "binding": pair["binding"],
        "plan_sha256": plan["plan_sha256"],
        "receipt": receipt_ref,
    }
    return marker, objects


def _original_target(scope, pair, plan, paths):
    rows = scope["rows"]
    migration._require(len(rows["documents"]) == 1, "alias_target_disappeared")
    doc = rows["documents"][0]
    manifest = json.loads(doc["manifest"])
    if MARKER not in manifest:
        migration._require(
            migration._hash_json(rows) == pair["target_before_rows_sha256"], "alias_target_changed_since_plan"
        )
        return scope, None
    validate_alias_archives(manifest, SYSTEM_OWNER_ID, paths)
    markers = manifest[MARKER]
    migration._require(
        len(markers) == 1
        and markers[0]["binding"] == pair["binding"]
        and markers[0]["plan_sha256"] == plan["plan_sha256"],
        "alias_target_binding_changed",
    )
    receipt = json.loads(_read_ref(markers[0]["receipt"], paths))
    original = _unpack(receipt["target_before"], paths)
    without = {k: v for k, v in manifest.items() if k != MARKER}
    migration._require(
        without == json.loads(original["documents"][0]["manifest"]), "alias_active_text_changed_since_merge"
    )
    restored = copy.deepcopy(scope)
    restored["rows"]["documents"][0]["manifest"] = original["documents"][0]["manifest"]
    migration._require(
        migration._hash_json(restored["rows"]) == pair["target_before_rows_sha256"],
        "alias_target_changed_since_plan",
    )
    return restored, markers[0]


def _owner_paths(directory):
    inventory = migration._inventory(directory)
    return migration._validated_paths(directory, inventory)


def migrate_alias_plan(snapshot_dir, target_dir, receipt, plan, *, apply, source_stopped, backup_dir):
    """Resume exact batches; existing documents are never refreshed wholesale."""
    _validate_plan(plan)
    migration._require(len(_encoded(plan)) <= MAX_PLAN_BYTES, "alias_plan_too_large")
    source_dir = Path(receipt["source_data_dir"]).resolve()
    migration._require(
        target_dir not in {source_dir, snapshot_dir}
        and str(target_dir) == plan["target_dir"]
        and receipt["snapshot_db_sha256"] == plan["snapshot_db_sha256"],
        "alias_plan_database_binding_mismatch",
    )
    paths = migration._validated_paths(source_dir, receipt["CAS_blobs"])
    target_paths = _owner_paths(target_dir)
    opened_source_inode = (source_dir / "tea.sqlite3").stat()
    with (
        closing(migration._readonly(snapshot_dir / "tea.sqlite3")) as source,
        closing(migration._readonly(target_dir / "tea.sqlite3")) as target,
        closing(migration._readonly(source_dir / "tea.sqlite3")) as live,
    ):
        summary = _source_state(source, receipt, source_dir, paths)
        migration._require(
            summary["scope_sha256"] == plan["source_scope_sha256"], "alias_source_changed_since_plan"
        )
        aliases = {row["binding"]["source_document_id"]: row for row in plan["aliases"]}
        _no_cross_alias_refs(source, set(aliases), paths)
        current_inode = (source_dir / "tea.sqlite3").stat()
        migration._require(
            (opened_source_inode.st_dev, opened_source_inode.st_ino)
            == (current_inode.st_dev, current_inode.st_ino),
            "source_resumed_or_changed_during_copy",
        )
        live.rollback()
        version = live.execute("PRAGMA data_version").fetchone()[0]
        live.execute("BEGIN")
        live_summary = migration._stream_summary(live)
        blockers = []
        if not source_stopped:
            blockers.append("source_stop_attestation_required")
        if live_summary["has_leases"] or live_summary["scope_sha256"] != summary["scope_sha256"]:
            blockers.append("source_resumed_or_changed_since_snapshot")
        live.rollback()
        inode = opened_source_inode

        def source_guard():
            current = (source_dir / "tea.sqlite3").stat()
            migration._require(
                (inode.st_dev, inode.st_ino) == (current.st_dev, current.st_ino)
                and live.execute("PRAGMA data_version").fetchone()[0] == version,
                "source_resumed_or_changed_during_copy",
            )

        old_ids = set(plan["target_document_guards"])
        incoming_ids = set(summary["document_ids"]) - aliases.keys()
        desired_ids = old_ids | incoming_ids
        migration._require(
            len(desired_ids) == plan["expected_system_documents"], "alias_document_union_mismatch"
        )

        def target_guard(conn):
            _validate_target_schema(conn)
            actual = migration._stream_target_guard(conn, desired_ids)
            migration._require(
                old_ids <= actual and not actual & aliases.keys(), "alias_target_scope_changed"
            )
            return actual

        target_guard(target)
        source_relations = _relations(source)
        migration._require(source_relations == plan["source_relations"], "alias_source_relations_changed")
        wanted_relations = plan["target_relations"] | source_relations

        def relations_guard(conn, *, complete=False):
            current = _relations(conn)
            migration._require(
                plan["target_relations"].items() <= current.items() <= wanted_relations.items()
                and (not complete or current == wanted_relations),
                "alias_relation_union_changed",
            )

        relations_guard(target)
        staged, original_targets, pending_aliases = {}, set(), 0
        for ident, pair in aliases.items():
            ss = migration._scope(source, [ident])
            ts = migration._scope(target, [pair["binding"]["target_document_id"]])
            original, previous = _original_target(ts, pair, plan, target_paths)
            migration._require(_pair(ss, original) == pair, "alias_page_or_vector_plan_changed")
            _validate_target_checkpoint(original, target, target_dir, target_paths)
            marker, objects = _archive_pair(pair, plan, ss, original)
            migration._require(previous is None or previous == marker, "alias_receipt_changed")
            pending_aliases += previous is None
            migration._require(
                sum(len(raw) for raw in objects.values()) <= 2 * MAX_CHECKPOINT_BYTES,
                "alias_checkpoint_package_too_large",
            )
            staged[ident] = marker
            original_targets.add(pair["binding"]["target_document_id"])
            del objects

        def baseline_guard(conn):
            for ident, digest in plan["target_document_guards"].items():
                current = migration._scope(conn, [ident])
                if ident in original_targets:
                    pair = next(p for p in aliases.values() if p["binding"]["target_document_id"] == ident)
                    current, _ = _original_target(current, pair, plan, target_paths)
                migration._require(
                    migration._hash_json(current["rows"]) == digest, "alias_preserved_target_changed"
                )

        baseline_guard(target)
        new_summary = dict(summary, document_ids=[i for i in summary["document_ids"] if i in incoming_ids])
        batches = list(migration._batches(new_summary))
        for ids in batches:
            part = migration._scope(source, ids)
            migration._check_target(
                target, part, migration._normalized(part, target_dir), migration._scope(target, ids)
            )
        existing_ids = target_guard(target)
        report = {
            "protocol": PLAN_PROTOCOL,
            "mode": "preflight",
            "plan_sha256": plan["plan_sha256"],
            "aliases": len(aliases),
            "incoming_documents": len(incoming_ids),
            "new_documents": len(incoming_ids - existing_ids),
            "alias_receipts_to_create": pending_aliases,
            "expected_system_documents": len(desired_ids),
            "expected_relations": len(wanted_relations),
            "target_writes": 0,
            "model_calls": 0,
            "HTTP_calls": 0,
            "blockers": blockers,
            "active_text_policy": "validated_target_contains_all_source_successful_inputs",
            "source_history_archival_planned": True,
            "batch_document_limit": migration.MAX_BATCH_DOCUMENTS,
            "batch_byte_limit": migration.MAX_BATCH_BYTES,
            "transaction_execution_budget_seconds": migration.MAX_TRANSACTION_SECONDS,
            "commit_duration_hard_limit": False,
        }
        if not apply:
            return report
        migration._require(not blockers, "migration_preflight_blocked:" + ",".join(blockers))
        backup_dir = (
            Path(backup_dir).resolve()
            if backup_dir
            else target_dir / "migration-backups" / (now().replace(":", "") + "-" + uuid4().hex)
        )
        migration._require(not backup_dir.exists(), "new_target_backup_directory_required")
        backup_dir.mkdir(mode=0o700, parents=True)
        backup_sha = migration._backup(target_dir / "tea.sqlite3", backup_dir / "tea.sqlite3")
        progress = report | {
            "mode": "copying",
            "target_backup_path": str(backup_dir / "tea.sqlite3"),
            "target_backup_sha256": backup_sha,
            "completed_document_batches": 0,
            "maximum_transaction_seconds": 0.0,
        }
        migration._progress(backup_dir / "backup.json", progress)
        migration._progress(backup_dir / "migration-progress.json", progress)
        from app.bootstrap import Service

        try:
            # A formal restore uses DELETE journal mode. Release our own read
            # snapshot before Service enables WAL, then validate every target
            # boundary again against the fresh snapshot. Dry-run stays read-only.
            target.rollback()
            service = Service(target_dir)
            target.execute("BEGIN")
            target_guard(target)
            baseline_guard(target)
            relations_guard(target)
            for ids in batches:
                part = migration._scope(source, ids)
                migration._check_target(
                    target, part, migration._normalized(part, target_dir), migration._scope(target, ids)
                )
            source_guard()
        except BaseException as exc:
            migration._progress(
                backup_dir / "migration-progress.json",
                progress | {"mode": "interrupted", "error_type": type(exc).__name__},
            )
            raise
        blobs = BlobStore(target_dir / "files", max_bytes=MAX_CHECKPOINT_BYTES)
        for digest, path in paths.items():
            with path.open("rb") as stream:
                copied = blobs.put(SYSTEM_OWNER_ID, iter(lambda: stream.read(1024 * 1024), b""))
            migration._require(copied.digest == digest, "source_blob_changed_during_copy")
            target_paths[digest] = copied.path
        # Rebuild one bounded pair at a time from the pinned read snapshots.
        # Never retain every document's manifests and vectors in process memory.
        for ident, pair in aliases.items():
            original, _ = _original_target(
                migration._scope(target, [pair["binding"]["target_document_id"]]), pair, plan, target_paths
            )
            marker, objects = _archive_pair(pair, plan, migration._scope(source, [ident]), original)
            migration._require(marker == staged[ident], "alias_archive_changed_before_publish")
            for digest, raw in objects.items():
                copied = blobs.put_bytes(SYSTEM_OWNER_ID, raw)
                migration._require(copied.digest == digest, "alias_archive_publish_mismatch")
                target_paths[digest] = copied.path
            del objects
        source_guard()

        def transaction(operation):
            interrupted = False
            with service.db.transaction() as conn:
                started = time.monotonic()

                def deadline():
                    nonlocal interrupted
                    interrupted = time.monotonic() - started > migration.MAX_TRANSACTION_SECONDS
                    return int(interrupted)

                conn.set_progress_handler(deadline, 1000)
                try:
                    source_guard()
                    target_guard(conn)
                    operation(conn)
                    migration._require(not deadline(), "migration_transaction_deadline")
                finally:
                    conn.set_progress_handler(None, 0)
            progress["maximum_transaction_seconds"] = max(
                progress["maximum_transaction_seconds"], time.monotonic() - started
            )

        def write_batch(ids):
            part = migration._scope(source, ids)
            expected = migration._normalized(part, target_dir)

            def operation(conn):
                current = migration._scope(conn, ids)
                migration._check_target(conn, part, expected, current)
                found = {row["id"] for row in current["rows"]["documents"]}
                migration._copy_batch(conn, expected, found)
                migration._require(
                    migration._comparable(migration._scope(conn, ids)["rows"])
                    == migration._comparable(expected),
                    "migration_readback_mismatch",
                )

            try:
                transaction(operation)
            except (sqlite3.OperationalError, migration.SystemCorpusMigrationError) as exc:
                if "interrupt" not in str(exc) and "transaction_deadline" not in str(exc):
                    raise
                migration._require(len(ids) > 1, "migration_single_document_transaction_limit")
                middle = len(ids) // 2
                write_batch(ids[:middle])
                write_batch(ids[middle:])
                return
            progress["completed_document_batches"] += 1
            migration._progress(backup_dir / "migration-progress.json", progress)

        try:
            for ident, pair in aliases.items():
                marker = staged[ident]
                target_id = pair["binding"]["target_document_id"]

                def save_alias(conn):
                    original, previous = _original_target(
                        migration._scope(conn, [target_id]), pair, plan, target_paths
                    )
                    if previous is not None:
                        return
                    doc = original["rows"]["documents"][0]
                    manifest = json.loads(doc["manifest"])
                    manifest[MARKER] = [marker]
                    encoded = dump(manifest)
                    migration._require(
                        len(encoded.encode()) <= migration.MAX_MANIFEST_BYTES, "alias_manifest_too_large"
                    )
                    validate_alias_archives(manifest, SYSTEM_OWNER_ID, target_paths)
                    conn.execute(
                        "UPDATE documents SET manifest=? WHERE id=? AND owner_id=?",
                        (encoded, target_id, SYSTEM_OWNER_ID),
                    )
                    # This is a provenance-only append. Preserve the preexisting
                    # dirty state; do not reindex or normalize paid vectors.
                    if not original["rows"]["retrieval_dirty"]:
                        conn.execute("DELETE FROM retrieval_dirty WHERE document_id=?", (target_id,))
                    _original_target(migration._scope(conn, [target_id]), pair, plan, target_paths)

                transaction(save_alias)
            for ids in batches:
                write_batch(ids)
            for raw in source.execute(
                "SELECT * FROM legal_relations WHERE owner_id=? ORDER BY id", (SYSTEM_OWNER_ID,)
            ):
                relation = dict(raw)

                def save_relation(conn):
                    prior = conn.execute(
                        "SELECT * FROM legal_relations WHERE id=?", (relation["id"],)
                    ).fetchone()
                    migration._require(
                        prior is None or dict(prior) == relation, "target_relation_id_conflict"
                    )
                    if prior is None:
                        migration._insert(conn, "legal_relations", relation)

                transaction(save_relation)
            with closing(migration._readonly(target_dir / "tea.sqlite3")) as final:
                migration._require(target_guard(final) == desired_ids, "migration_readback_mismatch")
                baseline_guard(final)
                relations_guard(final, complete=True)
                for ids in batches:
                    migration._require(
                        migration._comparable(migration._scope(final, ids)["rows"])
                        == migration._comparable(
                            migration._normalized(migration._scope(source, ids), target_dir)
                        ),
                        "migration_readback_mismatch",
                    )
                migration._require(
                    not final.execute("PRAGMA foreign_key_check").fetchone(), "migration_foreign_key_error"
                )
            source_guard()
        except BaseException as exc:
            migration._progress(
                backup_dir / "migration-progress.json",
                progress | {"mode": "interrupted", "error_type": type(exc).__name__},
            )
            raise
        report |= {
            "mode": "applied",
            "target_writes": len(incoming_ids - existing_ids) + pending_aliases,
            "alias_receipts_created": pending_aliases,
            "source_history_and_raw_vectors_archived": True,
            "target_backup_path": str(backup_dir / "tea.sqlite3"),
            "target_backup_sha256": backup_sha,
            "completed_document_batches": progress["completed_document_batches"],
            "maximum_transaction_seconds": progress["maximum_transaction_seconds"],
            "vector_bytes_preserved_without_renormalization": True,
        }
        migration._progress(backup_dir / "migration-progress.json", progress | report)
        return report
