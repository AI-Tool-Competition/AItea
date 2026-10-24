"""Apply an explicitly supplied successful OCR retry to an isolated document.

No model calls occur here. Account/version/manifest fences and the original
page pixels bind the checkpoint before a transaction replaces derived text.
"""

import hashlib
import json
import re
import sqlite3
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from app.contexts.documents.infrastructure.parsers.ingestion_quality import (
    pdf_page_status,
    text_complete,
    validate_ocr_page,
)
from app.contexts.documents.infrastructure.recovery.ocr_page_retry import (
    MAX_PAGE_BYTES,
    MAX_RETRY_PAGES,
    PROTOCOL,
    _encoded,
)
from app.contexts.documents.infrastructure.recovery.page_scope_quality import compare_page
from app.configuration.paths import PROJECT_ROOT, InstancePaths


def isolated_directory(path):
    resolved = Path(path).resolve()
    if resolved == InstancePaths.resolve().data:
        raise ValueError("isolated_recovery_copy_required_active_instance_refused")
    root = PROJECT_ROOT
    for parent in (root, *root.parents):
        if (parent / ".git").exists() and resolved == (parent / "data").resolve():
            raise ValueError("isolated_recovery_copy_required_default_project_data_refused")
    if not (resolved / "tea.sqlite3").is_file():
        raise ValueError("existing_isolated_database_required")
    return resolved


def readonly_service(directory):
    directory = isolated_directory(directory)

    def connect():
        conn = sqlite3.connect((directory / "tea.sqlite3").as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    return SimpleNamespace(db=SimpleNamespace(data_dir=directory, connect=connect))


def canonical_hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def manifest_hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def _portable(value):
    if isinstance(value, list):
        return [_portable(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _portable(item) for key, item in value.items()}
    if {"storage_path", "content_hash", "size"}.issubset(value):
        # Only the owner CAS location changes during a verified import.
        result["storage_path"] = "sha256:" + str(value["content_hash"])
    return result


def _document(conn, owner, document_id, source_hash, version, expected_manifest):
    if (
        not isinstance(source_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
        or not isinstance(expected_manifest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest)
        or type(version) is not int
        or version < 1
    ):
        raise ValueError("recovery_fences_invalid")
    row = conn.execute("SELECT * FROM documents WHERE id=? AND owner_id=?", (document_id, owner)).fetchone()
    if row is None:
        raise ValueError("recovery_document_not_owned_or_missing")
    if row["role"] != "primary" or row["case_id"] is not None:
        raise ValueError("recovery_requires_unbound_normative_document")
    if row["version"] != version or row["content_hash"] != source_hash:
        raise ValueError("recovery_document_version_conflict")
    if manifest_hash(row["manifest"]) != expected_manifest:
        raise ValueError("recovery_manifest_conflict")
    metadata = conn.execute(
        "SELECT payload FROM document_metadata WHERE document_id=?", (document_id,)
    ).fetchone()
    if metadata is None or json.loads(metadata[0]).get("review_status") != "draft":
        raise ValueError("recovery_requires_draft")
    job = conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (document_id,)).fetchone()
    if job and (job["status"] == "processing" or job["lease_token"] or job["lease_until"]):
        raise ValueError("recovery_processing_lease_active")
    return dict(row)


def _validated_page(document, manifest, checkpoint, checkpoint_dir, checkpoint_owner):
    from app.contexts.documents.infrastructure.parsers.archive_parsing import render_archive_page_bounded
    from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import read_image_blob
    from app.contexts.documents.infrastructure.providers.vision import PROMPT_VERSION

    source_hash = document["content_hash"]
    if not isinstance(checkpoint, dict):
        raise ValueError("recovery_checkpoint_invalid")
    if len(_encoded(checkpoint)) > MAX_PAGE_BYTES:
        raise ValueError("recovery_checkpoint_exceeds_2MiB")
    page_number = checkpoint.get("page")
    if type(page_number) is not int or not 1 <= page_number <= len(manifest.get("pages", [])):
        raise ValueError("recovery_physical_page_invalid")
    current = manifest["pages"][page_number - 1]
    if checkpoint.get("source_hash") != source_hash or current.get("source_hash") != source_hash:
        raise ValueError("recovery_page_source_mismatch")
    history = checkpoint.get("ocr_retry_history")
    if not isinstance(history, list) or len(history) != 1 or not isinstance(history[0], dict):
        raise ValueError("recovery_previous_checkpoint_required")
    proof = history[0]
    previous = proof.get("previous_page")
    if (
        proof.get("protocol") != PROTOCOL
        or proof.get("target_prompt_version") != PROMPT_VERSION
        or proof.get("source_hash") != source_hash
        or proof.get("page") != page_number
        or not isinstance(previous, dict)
        or "ocr_retry_history" in previous
        or proof.get("previous_page_sha256") != canonical_hash(previous)
        or _portable(previous) != _portable(current)
    ):
        raise ValueError("recovery_previous_checkpoint_conflict")
    metadata = checkpoint.get("ocr")
    if (
        not isinstance(metadata, dict)
        or metadata.get("prompt_version") != PROMPT_VERSION
        or metadata.get("source_hash") != source_hash
        or metadata.get("page") != page_number
        or metadata.get("complete") is not True
        or metadata.get("finish_reason") != "stop"
        or metadata.get("text") != checkpoint.get("text")
        or metadata.get("uncertain_spans")
        or metadata.get("validation_warnings")
        or pdf_page_status(checkpoint) != "ocr_complete"
    ):
        raise ValueError("recovery_successful_checkpoint_required")
    immutable = set(current) - {"status", "text", "printed_page", "ocr", "error", "deferred_reason"}
    if any(_portable(checkpoint.get(key)) != _portable(current[key]) for key in immutable):
        raise ValueError("recovery_immutable_page_evidence_changed")
    if not compare_page(previous, checkpoint)["scope_only_change_accepted"]:
        raise ValueError("recovery_scope_only_content_or_table_review_required")
    adopted = deepcopy(checkpoint)
    validate_ocr_page(adopted)
    if pdf_page_status(adopted) != "ocr_complete":
        raise ValueError("recovery_page_quality_rejected")
    image = read_image_blob(checkpoint_dir, checkpoint_owner, metadata.get("image_blob"))
    digest = hashlib.sha256(image).hexdigest()
    if digest != metadata.get("image_sha256"):
        raise ValueError("recovery_image_hash_mismatch")
    rendered = render_archive_page_bounded(
        document["storage_path"],
        source_hash=source_hash,
        size=document["size"],
        page_number=page_number,
    )
    if rendered["image_sha256"] != digest:
        raise ValueError("recovery_image_not_original_page")
    new_proof = adopted["ocr_retry_history"][0]
    new_proof["source_previous_page_sha256"] = proof["previous_page_sha256"]
    new_proof["previous_page"] = deepcopy(current)
    new_proof["previous_page_sha256"] = canonical_hash(current)
    return adopted, image


def apply_page_checkpoints(
    service,
    owner,
    document_id,
    *,
    source_hash,
    version,
    expected_manifest,
    checkpoints,
    checkpoint_dir,
    checkpoint_owner,
    apply=False,
):
    """Apply at most twenty already successful pages with one rebuild/transaction."""
    from contextlib import closing

    from app.contexts.documents.infrastructure.parsers.document_parser import rebuild_pdf_bounded
    from app.shared.domain.values import dump

    if (
        not isinstance(checkpoints, list)
        or not 1 <= len(checkpoints) <= MAX_RETRY_PAGES
        or any(not isinstance(page, dict) or type(page.get("page")) is not int for page in checkpoints)
        or len({page["page"] for page in checkpoints}) != len(checkpoints)
    ):
        raise ValueError("recovery_requires_1_to_20_unique_physical_pages")
    isolated_directory(service.db.data_dir)
    with closing(service.db.connect()) as conn:
        document = _document(conn, owner, document_id, source_hash, version, expected_manifest)
    manifest = json.loads(document["manifest"])
    adopted_pages = [
        _validated_page(document, manifest, page, checkpoint_dir, checkpoint_owner) for page in checkpoints
    ]
    selected = {page["page"] for page, _ in adopted_pages}
    for page, _ in adopted_pages:
        manifest["pages"][page["page"] - 1] = page
    rebuilt = rebuild_pdf_bounded(manifest)
    if any(
        old != new
        for index, (old, new) in enumerate(zip(manifest["pages"], rebuilt["pages"], strict=True))
        if index + 1 not in selected
    ):
        raise ValueError("recovery_other_page_checkpoint_changed")
    # Reassembly creates derived chunks/quality; keep import and source evidence.
    manifest.update(rebuilt)
    manifest.pop("embedding_fingerprint", None)
    manifest.pop("embedding_reuse", None)
    report = {
        "protocol": "verified-page-checkpoint-apply-v1",
        "mode": "apply" if apply else "dry_run",
        "document_id": document_id,
        "source_hash": source_hash,
        "version": version,
        "physical_pages": sorted(selected),
        "image_sha256_by_page": {str(page["page"]): page["ocr"]["image_sha256"] for page, _ in adopted_pages},
        "text_complete": text_complete(manifest),
        "HTTP_calls": 0,
        "model_calls": 0,
        "previous_manifest_sha256": expected_manifest,
        "database_writes": 0,
        "formal_legal_review": False,
    }
    if not apply:
        return report
    for page, image in adopted_pages:
        blob = service.archive.blobs.put_bytes(owner, image)
        manifest["pages"][page["page"] - 1]["ocr"]["image_blob"] = {
            "storage_path": str(blob.path.relative_to(service.db.data_dir)),
            "content_hash": blob.digest,
            "size": blob.size,
            "mime_type": "image/png",
        }
    with service.db.transaction() as conn:
        _document(conn, owner, document_id, source_hash, version, expected_manifest)
        conn.execute(
            "UPDATE documents SET manifest=?,status='processing',error=NULL WHERE id=?",
            (dump(manifest), document_id),
        )
        service.index.sync_document(conn, document_id, manifest, strict_vectors=True)
        service.collection.queue_processing(conn, document_id, owner)
        if (
            text_complete(manifest)
            and service.archive.can_index(conn, document_id)
            and service.embedding_fingerprint()
        ):
            conn.execute(
                "UPDATE processing_jobs SET phase='embed' WHERE document_id=? AND status='queued'",
                (document_id,),
            )
        service.collection._refresh_document(conn, document_id)
    return report | {"database_writes": 1, "manifest_sha256": manifest_hash(dump(manifest))}


def apply_page_checkpoint(service, owner, document_id, *, checkpoint, **options):
    report = apply_page_checkpoints(service, owner, document_id, checkpoints=[checkpoint], **options)
    number = report["physical_pages"][0]
    return report | {"physical_page": number, "image_sha256": report["image_sha256_by_page"][str(number)]}


def quarantine_scope_pages(
    service, owner, document_id, *, source_hash, version, expected_manifest, page_hashes, apply=False
):
    """Demote changed scope retries without discarding any response or retrying OCR."""
    from contextlib import closing

    from app.contexts.documents.infrastructure.parsers.document_parser import rebuild_pdf_bounded
    from app.contexts.documents.infrastructure.providers.vision import PROMPT_VERSION
    from app.shared.domain.values import dump, now

    if (
        not isinstance(page_hashes, dict)
        or not 1 <= len(page_hashes) <= MAX_RETRY_PAGES
        or any(
            type(number) is not int
            or number < 1
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for number, digest in page_hashes.items()
        )
    ):
        raise ValueError("quarantine_requires_1_to_20_page_hashes")
    isolated_directory(service.db.data_dir)
    with closing(service.db.connect()) as conn:
        document = _document(conn, owner, document_id, source_hash, version, expected_manifest)
    original = json.loads(document["manifest"])
    manifest, evidence = deepcopy(original), []
    for number, expected_page in page_hashes.items():
        if not 1 <= number <= len(manifest.get("pages", [])):
            raise ValueError("quarantine_page_unknown")
        page = manifest["pages"][number - 1]
        if page.get("page") != number or canonical_hash(page) != expected_page:
            raise ValueError("quarantine_page_checkpoint_conflict")
        history = page.get("ocr_retry_history")
        if not isinstance(history, list) or len(history) != 1 or not isinstance(history[0], dict):
            raise ValueError("quarantine_retry_history_required")
        proof, metadata = history[0], page.get("ocr", {})
        previous = proof.get("previous_page")
        if (
            page.get("source_hash") != source_hash
            or proof.get("protocol") != PROTOCOL
            or proof.get("source_hash") != source_hash
            or proof.get("page") != number
            or proof.get("target_prompt_version") != PROMPT_VERSION
            or metadata.get("prompt_version") != PROMPT_VERSION
            or not isinstance(previous, dict)
            or "ocr_retry_history" in previous
            or proof.get("previous_page_sha256") != canonical_hash(previous)
            or previous.get("page") != number
            or previous.get("source_hash") != source_hash
            or page.get("scope_retry_review")
        ):
            raise ValueError("quarantine_retry_provenance_conflict")
        comparison = compare_page(previous, page)
        if comparison["scope_only_change_accepted"]:
            raise ValueError("quarantine_scope_comparison_passed")
        page["scope_retry_review"] = {
            "protocol": "scope-retry-quarantine-v1",
            "prior_checkpoint_sha256": expected_page,
            "prior_status": page["status"],
            "prior_validation_warnings": deepcopy(metadata.get("validation_warnings", [])),
            "prior_uncertain_spans": deepcopy(metadata.get("uncertain_spans", [])),
            "comparison": comparison,
        }
        # Preserve the model's original complete flag and all text. Server
        # warnings and status alone withhold this page from the shared gate.
        page["status"] = "ocr_uncertain"
        metadata["validation_warnings"] = list(
            dict.fromkeys([*metadata.get("validation_warnings", []), *comparison["review_reasons"]])
        )
        evidence.append({"page": number, "prior_checkpoint_sha256": expected_page, **comparison})
    rebuilt = rebuild_pdf_bounded(manifest)
    if any(
        old != new
        for number, (old, new) in enumerate(zip(original["pages"], rebuilt["pages"], strict=True), 1)
        if number not in page_hashes
    ):
        raise ValueError("quarantine_other_page_checkpoint_changed")
    manifest.update(rebuilt)
    if text_complete(manifest):
        raise ValueError("quarantine_document_must_be_incomplete")
    report = {
        "protocol": "scope-retry-quarantine-v1",
        "mode": "apply" if apply else "dry_run",
        "document_id": document_id,
        "source_hash": source_hash,
        "version": version,
        "previous_manifest_sha256": expected_manifest,
        "pages": evidence,
        "database_writes": 0,
        "model_calls": 0,
        "text_complete": False,
        "formal_legal_review": False,
    }
    if not apply:
        return report
    with service.db.transaction() as conn:
        _document(conn, owner, document_id, source_hash, version, expected_manifest)
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed',error=? WHERE id=?",
            (dump(manifest), "scope_retry_content_or_table_review_required", document_id),
        )
        service.index.sync_document(conn, document_id, manifest, strict_vectors=True)
        conn.execute(
            "UPDATE processing_jobs SET status='failed',error=?,updated_at=? WHERE document_id=?",
            ("scope_retry_content_or_table_review_required", now(), document_id),
        )
        service.collection._refresh_document(conn, document_id)
    return report | {"database_writes": 1, "manifest_sha256": manifest_hash(dump(manifest))}
