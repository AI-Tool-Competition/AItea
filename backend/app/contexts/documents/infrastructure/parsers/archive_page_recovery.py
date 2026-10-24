"""Restore verified native windows through the existing bounded PDF pipeline.

This bridge does not relax aggregate limits. Larger archives retain their
independent evidence until a streaming derived-text format is available.
"""

import hashlib
import json
import re
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from app.contexts.documents.infrastructure.parsers import archive_page_shards as shards
from app.contexts.documents.infrastructure.parsers.archive_parsing import (
    _MAX_BYTES,
    _MAX_TEXT,
    _open_original,
    _verify_hash,
    _verify_identity,
    archive_rebuild_bounded,
    inspect_archive_pdf_bounded,
)
from app.contexts.documents.infrastructure.parsers.ingestion_quality import text_complete
from app.contexts.documents.infrastructure.parsers.document_parser import _MAX_RESULT_BYTES
from app.contexts.documents.infrastructure.recovery.page_checkpoint_recovery import (
    _document,
    isolated_directory,
    manifest_hash,
)
from app.shared.domain.values import dump

PROTOCOL = "archive-native-page-recovery-v1"
MAX_PROOF_BYTES = 256 * 1024 * 1024


def _original(document):
    stream, info = _open_original(document["storage_path"], document["size"])
    with stream:
        _verify_hash(stream, document["content_hash"], document["size"])
        _verify_identity(document["storage_path"], info, document["size"])


def _pages(directory, root, current):
    """Read ordered, bounded segments and refuse replacing any saved page."""
    identity = root["source_identity"]
    old = current.get("pages", [])
    if old and (
        len(old) != identity["total_pages"]
        or any(
            page.get("page") != number or page.get("source_hash") != identity["content_hash"]
            for number, page in enumerate(old, 1)
        )
    ):
        raise ValueError("native_recovery_existing_page_map_conflict")
    pages, characters, payload_bytes, added = [], 0, 0, []
    for segment in sorted(root["segments"], key=lambda item: item["first_page"]):
        payload_bytes += segment["size"]
        if payload_bytes > _MAX_RESULT_BYTES:
            raise ValueError("native_recovery_aggregate_exceeds_40MiB_use_segmented_storage")
        raw = shards._read_blob(directory, identity["owner_id"], segment, shards.MAX_SEGMENT_BYTES)
        for page in json.loads(raw)["pages"]:
            number = page["page"]
            if number != len(pages) + 1:
                raise ValueError("native_recovery_complete_ordered_pages_required")
            previous = old[number - 1] if old else None
            if previous and previous.get("status") != "pending_parse":
                if previous != page:
                    raise ValueError("native_recovery_saved_page_changed")
            else:
                if previous and (previous.get("text") or previous.get("native_text") or previous.get("ocr")):
                    raise ValueError("native_recovery_placeholder_contains_evidence")
                if (
                    segment["origin"] != "native_extraction"
                    or page["status"] not in {"native", "pending_ocr"}
                    or page.get("ocr")
                    or page.get("ocr_retry_history")
                ):
                    raise ValueError("native_recovery_new_page_must_be_native_evidence")
                added.append(number)
            characters += max(len(page["native_text"]), len(page["text"]))
            if characters > _MAX_TEXT:
                raise ValueError("native_recovery_aggregate_exceeds_4000000_characters_use_segmented_storage")
            pages.append(page)
    if len(pages) != identity["total_pages"]:
        raise ValueError("native_recovery_complete_ordered_pages_required")
    return pages, added


def _proof_references(root):
    references = [root["original"], *root["segments"]]
    if root.get("legacy_checkpoint"):
        references.append(root["legacy_checkpoint"])
    references.extend(item["blob"] for item in root.get("legacy_dependencies", []))
    unique = {item["sha256"]: item for item in references}
    if sum(item["size"] for item in unique.values()) > MAX_PROOF_BYTES:
        raise ValueError("native_recovery_proof_exceeds_256MiB")
    return list(unique.values())


def recover_native_page_shards(
    service,
    owner,
    document_id,
    *,
    source_hash,
    version,
    expected_manifest,
    shards_dir,
    expected_root,
    apply=False,
):
    """No network calls; draft/owner/version/lease fences precede a short commit."""
    isolated_directory(service.db.data_dir)
    directory = Path(shards_dir).absolute()
    if directory.is_symlink() or not re.fullmatch(r"[0-9a-f]{64}", expected_root or ""):
        raise ValueError("native_recovery_root_reference_invalid")
    with closing(service.db.connect()) as conn:
        document = _document(conn, owner, document_id, source_hash, version, expected_manifest)
    if document["mime_type"] != "application/pdf":
        raise ValueError("native_recovery_requires_pdf")
    if len(document["manifest"].encode()) > _MAX_RESULT_BYTES:
        raise ValueError("native_recovery_existing_manifest_exceeds_40MiB")
    raw_root = shards._read(directory / "root.json", shards.MAX_ROOT_BYTES)
    if hashlib.sha256(raw_root).hexdigest() != expected_root:
        raise ValueError("native_recovery_root_conflict")
    root = json.loads(raw_root)
    inspected = inspect_archive_pdf_bounded(
        document["storage_path"], source_hash=source_hash, size=document["size"]
    )
    identity = shards.ArchivePageSource(
        Path(document["storage_path"]),
        document_id,
        version,
        owner,
        source_hash,
        document["size"],
        inspected["total_pages"],
    ).identity()
    shards._validate_root(directory, root, identity)
    if not root["native_mapping_complete"]:
        raise ValueError("native_recovery_complete_mapping_required")
    current = json.loads(document["manifest"])
    pages, added = _pages(directory, root, current)
    report = {
        "protocol": PROTOCOL,
        "mode": "apply" if apply else "dry_run",
        "document_id": document_id,
        "version": version,
        "source_hash": source_hash,
        "root_sha256": expected_root,
        "previous_manifest_sha256": expected_manifest,
        "physical_pages": len(pages),
        "restored_native_pages": len(added),
        "database_writes": 0,
        "HTTP_calls": 0,
        "model_calls": 0,
        "formal_legal_review": False,
    }
    if not added:
        return report | {"unchanged": True, "text_complete": text_complete(current)}
    candidate = deepcopy(current)
    candidate.update(
        source_hash=source_hash,
        title=current.get("title") or document["title"],
        pipeline_version="archive-pdf-pages-v3",
        pages=pages,
        archive_pdf={
            "version": 1,
            "source_size": document["size"],
            "total_pages": len(pages),
            "window_size": min(
                64, max(item["last_page"] - item["first_page"] + 1 for item in root["segments"])
            ),
        },
    )
    rebuilt = archive_rebuild_bounded(candidate)
    # Validation may demote new evidence, but must not rewrite old successes.
    added_set = set(added)
    if any(
        old != new for old, new in zip(pages, rebuilt["pages"], strict=True) if old["page"] not in added_set
    ):
        raise ValueError("native_recovery_rebuild_changed_saved_page")
    candidate.update(rebuilt)
    candidate.pop("embedding_fingerprint", None)
    candidate.pop("embedding_reuse", None)
    references = _proof_references(root)
    report.update(
        text_complete=text_complete(candidate),
        pending_ocr_pages=len(candidate["quality"].get("pending_pages", [])),
        pending_native_pages=len(candidate["quality"].get("pending_parse_pages", [])),
        chunks=len(candidate.get("chunks", [])),
    )
    if not apply:
        return report
    proof_blobs = []
    for raw in [raw_root, document["manifest"].encode()]:
        blob = service.archive.blobs.put_bytes(owner, raw)
        proof_blobs.append(
            {
                "storage_path": str(blob.path.relative_to(service.db.data_dir)),
                "content_hash": blob.digest,
                "size": blob.size,
            }
        )
    for reference in references:
        raw = shards._read_blob(directory, owner, reference, _MAX_BYTES)
        blob = service.archive.blobs.put_bytes(owner, raw)
        proof_blobs.append(
            {
                "storage_path": str(blob.path.relative_to(service.db.data_dir)),
                "content_hash": blob.digest,
                "size": blob.size,
            }
        )
    candidate["native_page_recovery"] = report | {"evidence_blobs": proof_blobs}
    encoded = dump(candidate)
    if len(encoded.encode()) > _MAX_RESULT_BYTES:
        raise ValueError("native_recovery_result_exceeds_40MiB")
    _original(document)
    if shards._read(directory / "root.json", shards.MAX_ROOT_BYTES) != raw_root:
        raise ValueError("native_recovery_root_changed_during_prepare")
    with service.db.transaction() as conn:
        latest = _document(conn, owner, document_id, source_hash, version, expected_manifest)
        if any(
            latest[key] != document[key]
            for key in ("size", "storage_path", "source_url", "family_id", "filename", "parent_id")
        ):
            raise ValueError("native_recovery_original_metadata_changed")
        conn.execute(
            "UPDATE documents SET manifest=?,status='processing',error=NULL WHERE id=?",
            (encoded, document_id),
        )
        service.index.sync_document(conn, document_id, candidate, strict_vectors=True)
        service.collection.queue_processing(conn, document_id, owner)
        phase = (
            "embed"
            if text_complete(candidate)
            and service.archive.can_index(conn, document_id)
            and service.embedding_fingerprint()
            else "parse"
        )
        conn.execute(
            "UPDATE processing_jobs SET phase=? WHERE document_id=? AND status='queued'", (phase, document_id)
        )
        service.collection._refresh_document(conn, document_id)
    return report | {"database_writes": 1, "manifest_sha256": manifest_hash(encoded)}
