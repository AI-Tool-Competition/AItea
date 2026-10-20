"""Read-only progress summaries; a saved queue entry is not an active worker."""

import math
import time

from .vision import vision_ocr_configured


def processing_details(conn, document_ids):
    if not document_ids:
        return {}
    placeholders = ",".join("?" for _ in document_ids)
    # Project only flags, never list bodies, image payloads or embedding arrays.
    rows = conn.execute(
        f"""SELECT d.id,d.role,d.status,d.error,j.phase,j.status job_status,
            j.available_at,j.lease_until,j.lease_token IS NOT NULL has_lease,
            json_extract(d.manifest,'$.quality.review_required') review_required,
            json_extract(d.manifest,'$.quality.complete') text_complete,
            json_array_length(d.manifest,'$.quality.review_pages') review_count,
            EXISTS(SELECT 1 FROM json_each(d.manifest,'$.pages') p
                WHERE json_extract(p.value,'$.status') IN ('ocr_uncertain','source_incomplete')) page_review,
            EXISTS(SELECT 1 FROM json_each(d.manifest,'$.pages') p
                WHERE json_extract(p.value,'$.status')='pending_ocr') page_pending,
            EXISTS(SELECT 1 FROM json_each(d.manifest,'$.pages') p
                WHERE json_extract(p.value,'$.status')='ocr_failed') page_failed,
            EXISTS(SELECT 1 FROM json_each(COALESCE(
                json_extract(d.manifest,'$.assets_pending'),
                json_extract(d.manifest,'$.html_source.linked_images'),
                json_extract(d.manifest,'$.legacy.conversion.images'),'[]')) a
                WHERE COALESCE(json_extract(a.value,'$.asset_status'),'pending_vision')='pending_vision') asset_pending,
            EXISTS(SELECT 1 FROM json_each(COALESCE(
                json_extract(d.manifest,'$.assets_pending'),
                json_extract(d.manifest,'$.html_source.linked_images'),
                json_extract(d.manifest,'$.legacy.conversion.images'),'[]')) a
                WHERE json_extract(a.value,'$.asset_status') IN ('ocr_uncertain','image_reference_pending')) asset_review
            ,EXISTS(SELECT 1 FROM json_each(COALESCE(
                json_extract(d.manifest,'$.assets_pending'),
                json_extract(d.manifest,'$.html_source.linked_images'),
                json_extract(d.manifest,'$.legacy.conversion.images'),'[]')) a
                WHERE json_extract(a.value,'$.asset_status')='ocr_failed') asset_failed
            FROM documents d LEFT JOIN processing_jobs j ON j.document_id=d.id AND j.owner_id=d.owner_id
            WHERE d.id IN ({placeholders})""",
        document_ids,
    ).fetchall()
    timestamp = time.time()
    ocr_enabled = vision_ocr_configured()
    result = {}
    for row in rows:
        active = (
            row["job_status"] == "processing" and row["has_lease"] and (row["lease_until"] or 0) > timestamp
        )
        if row["role"] == "secondary" or row["status"] == "stored":
            state = "stored"
        elif active:
            state = "running"
        elif row["review_required"] or row["review_count"] or row["page_review"] or row["asset_review"]:
            state = "needs_review"
        elif row["page_failed"] or row["asset_failed"]:
            state = "failed"
        elif (row["page_pending"] or row["asset_pending"]) and not ocr_enabled:
            state = "ocr_disabled"
        elif row["status"] == "failed" or row["job_status"] == "failed":
            state = "failed"
        elif row["job_status"] in {"queued", "processing"} or row["status"] == "processing":
            state = "queued"
        elif row["text_complete"] == 0 or (row["error"] and row["text_complete"] != 1):
            state = "incomplete"
        else:
            state = "complete"
        result[row["id"]] = {
            "state": state,
            "phase": row["phase"],
            "retry_after_seconds": (
                max(0, math.ceil((row["available_at"] or 0) - timestamp))
                if state == "queued" and row["job_status"] is not None
                else None
            ),
        }
    return result
