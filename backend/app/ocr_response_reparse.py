"""Reparse one complete saved response without inventing a new model request."""

import base64
from contextlib import closing
from copy import deepcopy
import hashlib
import json

from .db import dump
from .ingestion_quality import pdf_page_status, text_complete
from .ocr_error_evidence import preserve_previous_page_attempt, validate_response_binding
from .page_checkpoint_recovery import _document, isolated_directory, manifest_hash

PROTOCOL = "saved-ocr-response-reparse-v1"


def reparse_saved_page_response(page, error_blob):
    from .parsing import apply_page_transcription
    from .pdf_ocr_reuse import native_hint
    from .vision import MAX_ERROR_RESPONSE_BYTES, PROMPT_VERSION, decode_page_response

    metadata = page.get("ocr", {})
    reference = metadata.get("error_response", {})
    blob = reference.get("blob", {})
    if (
        pdf_page_status(page) != "ocr_failed"
        or metadata.get("prompt_version") != PROMPT_VERSION
        or metadata.get("response_reparse")
        or not isinstance(error_blob, bytes)
        or not 0 < len(error_blob) <= 2 * MAX_ERROR_RESPONSE_BYTES
        or len(error_blob) != blob.get("size")
        or hashlib.sha256(error_blob).hexdigest() != blob.get("content_hash")
    ):
        raise ValueError("saved_response_reparse_requires_bound_failed_page")
    evidence = json.loads(error_blob)
    validate_response_binding(evidence, page["source_hash"], page)
    if (
        {key: value for key, value in reference.items() if key != "blob"}
        != {key: value for key, value in evidence.items() if key != "response_base64"}
        or evidence.get("request_kind") not in (None, "page")
        or evidence.get("error_code") not in {"invalid_transcription_schema", "invalid_transcription_json"}
        or evidence.get("payload_kind") != "assistant_message"
        or evidence.get("finish_reason") != "stop"
        or evidence.get("response_complete") is not True
        or evidence.get("capture_truncated") is not False
    ):
        raise ValueError("saved_response_reparse_requires_complete_original_response")
    raw = base64.b64decode(evidence["response_base64"], validate=True)
    if len(raw) != evidence["size"] or hashlib.sha256(raw).hexdigest() != evidence["content_hash"]:
        raise ValueError("saved_response_reparse_body_hash_mismatch")
    hint, strategy = native_hint(page)
    if (
        metadata.get("native_hint_hash") != hashlib.sha256(hint.encode()).hexdigest()
        or metadata.get("native_hint_chars") != len(hint)
        or metadata.get("native_hint_strategy") != strategy
    ):
        raise ValueError("saved_response_reparse_native_hint_changed")
    text = raw.decode("utf-8")
    transcript = decode_page_response(
        {},
        {"choices": [{"message": {"content": text}}]},
        text,
        "stop",
        model=metadata["requested_model"],
        native_hint=hint,
    )
    updated = deepcopy(page)
    preserve_previous_page_attempt(updated)
    apply_page_transcription(updated, updated["ocr"], transcript, native_hint=hint)
    updated["ocr"]["response_reparse"] = {
        "protocol": PROTOCOL,
        "response_blob_sha256": blob["content_hash"],
        "response_sha256": evidence["content_hash"],
        "request_id": evidence["request_id"],
        "new_model_requests": 0,
        "provider_returned_model_recorded": False,
    }
    updated.pop("error", None)
    updated.pop("deferred_reason", None)
    return updated


def apply_saved_response_reparse(
    service, owner, document_id, *, page_number, source_hash, version, expected_manifest, apply=False
):
    """Version/owner fenced adoption; source evidence and other pages stay intact."""
    from .archive_parsing import render_archive_page_bounded
    from .html_linked_asset_ocr import read_image_blob
    from .parsing import rebuild_pdf_bounded

    isolated_directory(service.db.data_dir)
    with closing(service.db.connect()) as conn:
        document = _document(conn, owner, document_id, source_hash, version, expected_manifest)
    manifest = json.loads(document["manifest"])
    if type(page_number) is not int or not 1 <= page_number <= len(manifest.get("pages", [])):
        raise ValueError("saved_response_reparse_page_invalid")
    page = manifest["pages"][page_number - 1]
    if page.get("source_hash") != source_hash or page.get("page") != page_number:
        raise ValueError("saved_response_reparse_source_mismatch")
    evidence = read_image_blob(
        service.db.data_dir, owner, page.get("ocr", {}).get("error_response", {}).get("blob")
    )
    adopted = reparse_saved_page_response(page, evidence)
    rendered = render_archive_page_bounded(
        document["storage_path"], source_hash=source_hash, size=document["size"], page_number=page_number
    )
    image = read_image_blob(service.db.data_dir, owner, page["ocr"].get("image_blob"))
    if hashlib.sha256(image).hexdigest() != page["ocr"].get("image_sha256") or rendered[
        "image_sha256"
    ] != page["ocr"].get("image_sha256"):
        raise ValueError("saved_response_reparse_image_source_mismatch")
    manifest["pages"][page_number - 1] = adopted
    rebuilt = rebuild_pdf_bounded(manifest)
    if any(
        old != new
        for index, (old, new) in enumerate(zip(manifest["pages"], rebuilt["pages"], strict=True))
        if index != page_number - 1
    ):
        raise ValueError("saved_response_reparse_other_pages_changed")
    manifest.update(rebuilt)
    report = {
        "protocol": PROTOCOL,
        "mode": "apply" if apply else "dry_run",
        "document_id": document_id,
        "source_hash": source_hash,
        "version": version,
        "physical_page": page_number,
        "previous_manifest_sha256": expected_manifest,
        "page_status": pdf_page_status(adopted),
        "text_complete": text_complete(manifest),
        "model_calls": 0,
        "database_writes": 0,
        "request_id_unchanged": adopted["ocr"]["request_id"] == page["ocr"]["request_id"],
        "attempts_unchanged": adopted["ocr"]["attempts"] == page["ocr"]["attempts"],
        "formal_legal_review": False,
    }
    if not apply:
        return report
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
