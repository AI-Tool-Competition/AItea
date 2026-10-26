"""Durable OCR and indexing recovery with preserved evidence and delayed retries.

Each processing attempt remains bounded. Eligible uncertain or incomplete OCR
results can start another attempt after backoff. Confirmed terminal MinerU failures
start a fresh generation after backoff, reusing successful and live part receipts.
OCR provider failures, including HTTP rejection responses, remain eligible for
automatic retry after backoff. Source-bound legacy OCR failures with unknown
provider outcomes also retry automatically. Source/checkpoint changes stay held. Unknown
RAGFlow writes are checked by exact receipt before they are automatically repeated;
unmatched writes resume after the provider backoff expires. If text completeness fails,
the worker returns to the source-bound OCR recovery queue before indexing resumes.
"""

import hashlib
import json
import math
import re
import time
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass

from app.contexts.documents.application.system_library import BackgroundWorkerScope
from app.contexts.documents.infrastructure.recovery.ocr_error_evidence import request_outcome_unknown
from app.shared.domain.values import dump, now, uid

SCHEMA_VERSION = 19
BASE_RECOVERY_SECONDS = 15 * 60
PAID_OCR_RETRY_SECONDS = 60
MAX_PAID_OCR_RETRY_SECONDS = 15 * 60
MAX_RECOVERY_SECONDS = 24 * 60 * 60
OCR_AUTOMATIC_RETRY_REASONS = frozenset(
    {"ocr_quality_retry", "paid_ocr_retry", "mineru_visual_table_retry", "mineru_empty_page_retry"}
)
MAX_BATCH = 32
MAX_AUTOMATIC_OCR_TARGETS = 20
MAX_AUTOMATIC_OCR_CHECKPOINT_BYTES = 8 * 1024 * 1024
MAX_AUTOMATIC_MINERU_QUALITY_GENERATIONS = 8
MAX_HTML_PARSER_REFRESH_ATTEMPTS = 3
PROCESSING_QUEUE_LIMIT = 200
# Keep one bounded admission slot so recovery work cannot starve behind new imports.
RECOVERY_QUEUE_RESERVE = 1


def _encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _timestamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else 0


def _recovery_delay(decision, recovery_count):
    """Keep OCR retries automatic while honoring the provider's longer delay."""
    if decision.reason == "paid_ocr_retry":
        return min(
            MAX_PAID_OCR_RETRY_SECONDS,
            PAID_OCR_RETRY_SECONDS * 2 ** min(max(recovery_count, 0), 4),
        )
    if decision.reason in OCR_AUTOMATIC_RETRY_REASONS:
        return BASE_RECOVERY_SECONDS
    return min(MAX_RECOVERY_SECONDS, BASE_RECOVERY_SECONDS * 2 ** min(recovery_count, 7))


@dataclass(frozen=True)
class Decision:
    reason: str
    retryable: bool = False
    pages: tuple[int, ...] = ()
    not_before: float = 0
    asset_keys: tuple[str, ...] = ()


def _unknown(record):
    metadata = record.get("ocr", {})
    if request_outcome_unknown(metadata):
        return True
    status = metadata.get("http_status")
    if type(status) is int and (status == 408 or 500 <= status <= 599):
        return True
    # An intent without a final receipt may have survived a killed worker.
    return bool(
        metadata.get("attempts")
        and not metadata.get("finish_reason")
        and "text" not in metadata
        and "complete" not in metadata
        and not metadata.get("error_response")
        and not record.get("error")
    )


def _unreceipted_vision_failure(page, source_hash):
    """Allow a source-bound legacy OCR failure to retry without a provider receipt."""
    from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import (
        EMPTY_PAGE_PROTOCOL,
        PROTOCOL,
    )

    metadata = page.get("ocr")
    page_number = page.get("page")
    fallback = page.get("ocr_fallback")
    bound_fallback = not fallback or (
        isinstance(fallback, dict)
        and fallback.get("protocol") in {PROTOCOL, EMPTY_PAGE_PROTOCOL}
        and fallback.get("source_page") == page_number
        and isinstance(fallback.get("source_result_hash"), str)
        and re.fullmatch(r"[a-f0-9]{64}", fallback["source_result_hash"])
        and type(fallback.get("attempts")) is int
        and fallback["attempts"] > 0
    )
    return bool(
        page.get("status") == "ocr_failed"
        and bound_fallback
        and isinstance(source_hash, str)
        and re.fullmatch(r"[a-f0-9]{64}", source_hash)
        and page.get("source_hash") == source_hash
        and isinstance(metadata, dict)
        and metadata.get("provider") != "mineru"
        and isinstance(metadata.get("prompt_version"), str)
        and bool(metadata["prompt_version"])
        and isinstance(metadata.get("requested_model"), str)
        and bool(metadata["requested_model"])
        and metadata.get("source_hash") == source_hash
        and type(page_number) is int
        and page_number > 0
        and metadata.get("page") == page_number
    )


def _legacy_mineru_render_source_proof_mismatch(manifest, page):
    """Recognize the old renderer overwriting a Word page's source hash/page."""
    if (
        not isinstance(manifest, dict)
        or manifest.get("parser") != "mineru"
        or not isinstance(page, dict)
        or page.get("status") not in {"ocr_complete", "ocr_uncertain"}
        or page.get("source_hash") != manifest.get("source_hash")
        or type(page.get("page")) is not int
    ):
        return False
    proof = page.get("ocr")
    render_source = proof.get("render_source") if isinstance(proof, dict) else None
    remote = manifest.get("remote_parse")
    if (
        not isinstance(proof, dict)
        or proof.get("provider") != "vision"
        or proof.get("complete") is not True
        or not isinstance(render_source, dict)
        or not isinstance(remote, dict)
        or remote.get("provider") != "mineru"
        or remote.get("state") != "done"
        or remote.get("source_hash") != manifest.get("source_hash")
    ):
        return False
    parts = remote.get("parts")
    if not isinstance(parts, list):
        return False
    matches = [
        part
        for part in parts
        if isinstance(part, dict)
        and type(part.get("first_page")) is int
        and type(part.get("last_page")) is int
        and part["first_page"] <= page["page"] <= part["last_page"]
    ]
    if len(matches) != 1:
        return False
    part = matches[0]
    receipt = part.get("render_pdf")
    blob = receipt.get("blob") if isinstance(receipt, dict) else None
    if not isinstance(blob, dict):
        return False
    try:
        from app.contexts.documents.infrastructure.providers.mineru_results import _valid_blob_reference

        valid_blob = _valid_blob_reference(blob)
    except (TypeError, ValueError):
        return False
    local_page = page["page"] - part["first_page"] + 1
    return bool(
        valid_blob
        and receipt.get("source_hash") == manifest["source_hash"]
        and receipt.get("task_id") == part.get("task_id")
        and receipt.get("result_hash") == (part.get("result_blob") or {}).get("content_hash")
        and receipt.get("page_count") == part["last_page"] - part["first_page"] + 1
        and render_source.get("provider") == "mineru"
        and render_source.get("source_hash") == manifest["source_hash"]
        and render_source.get("page") == page["page"]
        and render_source.get("task_id") == part.get("task_id")
        and render_source.get("result_hash") == receipt.get("result_hash")
        and render_source.get("rendered_pdf_page") == local_page
        and render_source.get("blob") == blob
        and proof.get("source_hash") == blob.get("content_hash")
        and proof.get("source_hash") != manifest["source_hash"]
        and proof.get("page") == local_page
    )


def _saved_visual_provider_retry(page):
    """Find the latest intact failed or uncertain vision request archived by a page retry."""
    cycles = page.get("ocr_retry_cycles", [])
    if not isinstance(cycles, list):
        return None
    for cycle in reversed(cycles):
        if not isinstance(cycle, dict) or cycle.get("protocol") != "ocr-automatic-retry-checkpoint-v1":
            continue
        previous = cycle.get("previous_page")
        if not isinstance(previous, dict):
            return None
        try:
            if cycle.get("previous_page_sha256") != _digest(previous):
                return None
        except (TypeError, ValueError):
            return None
        if previous.get("page") != page.get("page") or previous.get("source_hash") != page.get("source_hash"):
            return None
        metadata = previous.get("ocr")
        if not isinstance(metadata, dict):
            return None
        if previous.get("status") == "pending_ocr":
            uncertain_request = (
                _unknown(previous)
                and metadata.get("source_hash") == page.get("source_hash")
                and metadata.get("page") == page.get("page")
                and type(metadata.get("attempts")) is int
                and metadata["attempts"] > 0
                and isinstance(metadata.get("request_id"), str)
                and bool(metadata["request_id"])
                and isinstance(metadata.get("image_sha256"), str)
                and re.fullmatch(r"[a-f0-9]{64}", metadata["image_sha256"])
                and isinstance(metadata.get("prompt_version"), str)
                and isinstance(metadata.get("requested_model"), str)
            )
            if uncertain_request:
                return previous
            if _unknown(previous):
                return None
            continue
        if previous.get("status") != "ocr_failed":
            return None
        envelope = metadata.get("error_response")
        if (
            metadata.get("provider") != "mineru"
            and (metadata.get("prompt_version") or metadata.get("request_id"))
            and isinstance(envelope, dict)
            and envelope.get("format") == "ocr-error-response-v1"
            and isinstance(envelope.get("blob"), dict)
        ):
            return previous
        return None
    return None


def _legacy_mineru_chart_refresh(manifest):
    """Recognize only pre-inventory MinerU manifests blocked solely on charts."""
    remote = manifest.get("remote_parse")
    if (
        manifest.get("parser") != "mineru"
        or manifest.get("mineru_chart_inventory_version")
        or manifest.get("mineru_chart_assets")
        or not isinstance(remote, dict)
        or remote.get("provider") != "mineru"
        or remote.get("state") != "done"
    ):
        return False
    try:
        from app.contexts.documents.infrastructure.providers.mineru import manifest_valid

        if not manifest_valid(manifest):
            return False
    except (KeyError, TypeError, ValueError):
        return False
    pages = manifest.get("pages")
    if not isinstance(pages, list) or not pages:
        return False
    chart_pages = 0
    for page in pages:
        reasons = page.get("reasons")
        proof = page.get("ocr")
        if not isinstance(reasons, list) or not isinstance(proof, dict):
            return False
        if "chart_content_missing" in reasons:
            chart_pages += 1
            if (
                set(reasons) != {"chart_content_missing"}
                or page.get("status") != "ocr_uncertain"
                or proof.get("provider") != "mineru"
                or proof.get("complete") is not False
                or proof.get("uncertain_spans") != ["chart_content_missing"]
            ):
                return False
        elif reasons or page.get("status") != "ocr_complete" or proof.get("complete") is not True:
            return False
        text = page.get("text")
        if not isinstance(text, str) or proof.get("text_sha256") != hashlib.sha256(text.encode()).hexdigest():
            return False
    return chart_pages > 0


def _legacy_html_parser_refresh_plan(manifest, source_hash):
    """Bind a parser refresh to an older, intact official HTML source version."""
    from app.contexts.documents.infrastructure.parsers.html_parsing import VERSION

    prefix = "html-source-mapping-v"
    target_version = VERSION.removeprefix(prefix)
    if not isinstance(manifest, dict):
        return None
    old_version = manifest.get("parser_version")
    html_source = manifest.get("html_source")
    if (
        not target_version.isdecimal()
        or manifest.get("parser") != "html-basic"
        or not isinstance(old_version, str)
        or not old_version.isdecimal()
        or int(old_version) >= int(target_version)
        or manifest.get("source_hash") != source_hash
        or not isinstance(html_source, dict)
        or html_source.get("source_hash") != source_hash
        or html_source.get("mapping_version") != f"{prefix}{old_version}"
        or str(manifest.get("pipeline_version", "")).split("+", 1)[0] != f"{prefix}{old_version}"
        or not isinstance(html_source.get("objects"), list)
    ):
        return None
    return {
        "protocol": "official-html-parser-refresh-v1",
        "source_hash": source_hash,
        "previous_parser_version": old_version,
        "target_parser_version": target_version,
    }


def _is_decode_replacement_page(page, source_hash):
    if not isinstance(page, dict) or page.get("source_hash") != source_hash:
        return False
    proof = page.get("ocr")
    reasons = page.get("reasons")
    spans = proof.get("uncertain_spans") if isinstance(proof, dict) else None
    return (
        page.get("status") == "ocr_uncertain"
        and isinstance(reasons, list)
        and reasons == ["decode_replacement"]
        and isinstance(spans, list)
        and spans == ["decode_replacement"]
        and proof.get("provider") == "mineru"
        and proof.get("complete") is False
    )


def _mineru_decode_quality_targets(manifest, source_hash):
    """Select complete MinerU source parts containing only decode-replacement pages."""
    if not isinstance(manifest, dict):
        return None
    remote = manifest.get("remote_parse")
    pages = manifest.get("pages")
    if (
        manifest.get("parser") != "mineru"
        or manifest.get("source_hash") != source_hash
        or not isinstance(remote, dict)
        or remote.get("provider") != "mineru"
        or remote.get("state") != "done"
        or remote.get("source_hash") != source_hash
        or type(remote.get("page_count")) is not int
        or not 1 <= remote["page_count"] <= 3000
        or not isinstance(pages, list)
        or len(pages) != remote["page_count"]
    ):
        return None
    try:
        from app.contexts.documents.infrastructure.providers.mineru import manifest_valid

        if not manifest_valid(manifest):
            return None
    except (KeyError, TypeError, ValueError):
        return None

    retry_pages = []
    for number, page in enumerate(pages, 1):
        if not isinstance(page, dict) or page.get("page") != number or page.get("source_hash") != source_hash:
            return None
        proof = page.get("ocr")
        reasons = page.get("reasons")
        if not isinstance(proof, dict) or not isinstance(reasons, list):
            return None
        if page.get("status") == "ocr_uncertain":
            if not _is_decode_replacement_page(page, source_hash):
                return None
            retry_pages.append(number)
        elif page.get("status") != "ocr_complete" or reasons or proof.get("complete") is not True:
            return None
    if not retry_pages:
        return None

    parts = remote.get("parts")
    if not isinstance(parts, list) or not parts:
        return None
    target_parts = []
    expected_page = 1
    for index, part in enumerate(parts):
        if (
            not isinstance(part, dict)
            or part.get("source_hash") != source_hash
            or type(part.get("first_page")) is not int
            or type(part.get("last_page")) is not int
            or part["first_page"] != expected_page
            or part["last_page"] < part["first_page"]
            or not isinstance(part.get("task_id"), str)
            or not part["task_id"]
            or not isinstance(part.get("result_blob"), dict)
        ):
            return None
        if any(part["first_page"] <= page <= part["last_page"] for page in retry_pages):
            target_parts.append(index)
        expected_page = part["last_page"] + 1
    if expected_page != remote["page_count"] + 1 or not target_parts:
        return None
    return tuple(retry_pages), tuple(target_parts)


def classify_processing(manifest, *, phase, error=None, retryable=False, interrupted=False):
    """Use bound OCR evidence and bounded automatic retries, never exception text."""
    from app.contexts.documents.infrastructure.providers.native_asset_ocr import assets
    from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import (
        MAX_RETRY_CYCLES as MAX_MINERU_VISUAL_RETRIES,
        fallback_attempts,
        is_empty_page_visual_fallback_page,
        is_mineru_empty_page_review,
        is_mineru_table_review,
        is_table_visual_fallback_page,
        is_visual_fallback_page,
        markdown_table_is_consistent,
        missing_visual_provider_pages,
    )
    from app.contexts.documents.infrastructure.parsers.ingestion_quality import pdf_page_status
    from app.contexts.documents.infrastructure.providers.vision import vision_ocr_configured

    if phase == "embed":
        # The generic multi-batch call has no complete per-input receipt ledger.
        # Its final 429 alone cannot prove earlier batches were not billed.
        return Decision("paid_result_unknown")
    if not isinstance(manifest, dict):
        return Decision("invalid_checkpoint")
    pages = manifest.get("pages", [])
    if not isinstance(pages, list) or any(not isinstance(page, dict) for page in pages):
        return Decision("invalid_checkpoint")
    try:
        images = assets(manifest)
    except (ValueError, TypeError, AttributeError):
        return Decision("invalid_checkpoint")
    if phase == "parse" and _legacy_mineru_chart_refresh(manifest):
        return Decision("mineru_chart_refresh", True)

    remote_parse = manifest.get("remote_parse") or {}
    mineru_source_bound = (
        manifest.get("parser") == "mineru"
        and remote_parse.get("provider") == "mineru"
        and remote_parse.get("state") == "done"
        and isinstance(manifest.get("source_hash"), str)
        and remote_parse.get("source_hash") == manifest.get("source_hash")
    )
    retry_pages, retry_assets, mineru_visual_pages, mineru_empty_pages = [], [], [], []
    retry_not_before = 0
    unknown_outcome = False
    if phase == "parse":
        vision_ready = vision_ocr_configured()
        if vision_ready:
            for page_number in missing_visual_provider_pages(manifest):
                if len(retry_pages) >= MAX_AUTOMATIC_OCR_TARGETS:
                    break
                retry_pages.append(page_number)
                mineru_visual_pages.append(page_number)
        for page in pages:
            if page.get("page") in retry_pages:
                continue
            legacy_render_mismatch = vision_ready and _legacy_mineru_render_source_proof_mismatch(
                manifest, page
            )
            if legacy_render_mismatch:
                if len(retry_pages) < MAX_AUTOMATIC_OCR_TARGETS:
                    retry_pages.append(page["page"])
                    mineru_visual_pages.append(page["page"])
                    unknown_outcome = True
                continue
            metadata = page.get("ocr") or {}
            status = page.get("status")
            unknown = _unknown(page)
            unreceipted_vision_failure = _unreceipted_vision_failure(
                page, manifest.get("source_hash")
            )
            unknown |= unreceipted_vision_failure
            unknown_outcome |= unknown
            try:
                visual_fallback = is_visual_fallback_page(page)
                visual_attempts = fallback_attempts(page) if visual_fallback else 0
            except ValueError:
                return Decision("invalid_checkpoint")
            fallback = page.get("ocr_fallback") or {}
            source_result_hash = fallback.get("source_result_hash")
            failed_visual_retry = (
                vision_ready
                and status == "ocr_failed"
                and visual_fallback
                and mineru_source_bound
                and page.get("source_hash") == manifest.get("source_hash")
                and fallback.get("source_page") == page.get("page")
                and isinstance(source_result_hash, str)
                and len(source_result_hash) == 64
                and all(character in "0123456789abcdef" for character in source_result_hash)
                and visual_attempts < MAX_MINERU_VISUAL_RETRIES
            )
            failed_empty_visual_retry = failed_visual_retry and is_empty_page_visual_fallback_page(page)
            pending_visual_retry = (
                vision_ready
                and visual_fallback
                and status == "pending_ocr"
                and (
                    visual_attempts < MAX_MINERU_VISUAL_RETRIES
                    or _saved_visual_provider_retry(page) is not None
                )
            )
            visual_table_quality_retry = (
                vision_ready
                and is_table_visual_fallback_page(page)
                and visual_attempts < MAX_MINERU_VISUAL_RETRIES
                and status == "ocr_complete"
                and metadata.get("complete") is True
                and not metadata.get("uncertain_spans")
                and isinstance(page.get("text"), str)
                and not markdown_table_is_consistent(page["text"])
            )
            empty_page_retry = vision_ready and is_mineru_empty_page_review(page)
            mineru_table_retry = is_mineru_table_review(page) and not visual_fallback
            # MinerU's page-level chart/table warning is not a failed vision
            # request. Ambiguous tables have a separate, page-bounded visual
            # fallback; chart assets continue through their mapped checkpoints.
            # A provider failure is not a completed quality fallback. Keep
            # retrying failed requests after the visual-quality budget expires;
            # the preserved response receipt and retry checkpoint make each
            # repeated attempt auditable. Successful but still ambiguous visual
            # output remains capped by MAX_MINERU_VISUAL_RETRIES.
            vision_page = (
                metadata.get("provider") != "mineru"
                and bool(metadata.get("prompt_version") or metadata.get("request_id"))
                and (
                    not visual_fallback
                    or status == "ocr_failed"
                    or visual_attempts < MAX_MINERU_VISUAL_RETRIES
                )
            )
            eligible = (
                status in {"ocr_failed", "ocr_uncertain", "pending_ocr"}
                or visual_table_quality_retry
                or empty_page_retry
                or failed_visual_retry
            ) and (
                mineru_table_retry
                or empty_page_retry
                or failed_visual_retry
                or unknown
                or pending_visual_retry
                or visual_table_quality_retry
                or (
                    vision_page
                    and (
                        metadata.get("complete") is False
                        or status == "ocr_uncertain"
                        or (
                            status == "ocr_failed"
                            and isinstance(metadata.get("error_response"), dict)
                            and isinstance(metadata["error_response"].get("blob"), dict)
                        )
                        or unreceipted_vision_failure
                    )
                )
            ) and type(page.get("page")) is int and page["page"] > 0
            if not eligible:
                continue
            cycles = page.get("ocr_retry_cycles", [])
            if not isinstance(cycles, list):
                return Decision("invalid_checkpoint")
            if len(retry_pages) < MAX_AUTOMATIC_OCR_TARGETS:
                retry_pages.append(page["page"])
                retry_not_before = max(retry_not_before, _timestamp(metadata.get("retry_not_before")))
                if pending_visual_retry:
                    previous_failure = _saved_visual_provider_retry(page)
                    if previous_failure is not None:
                        unknown_outcome |= _unknown(previous_failure)
                        retry_not_before = max(
                            retry_not_before,
                            _timestamp((previous_failure.get("ocr") or {}).get("retry_not_before")),
                        )
                if mineru_table_retry or pending_visual_retry or empty_page_retry or failed_visual_retry:
                    mineru_visual_pages.append(page["page"])
                if empty_page_retry or failed_empty_visual_retry:
                    mineru_empty_pages.append(page["page"])

        for asset in images:
            metadata = asset.get("ocr") or {}
            unknown = _unknown(asset)
            unknown_outcome |= unknown
            eligible = (
                asset.get("asset_status") == "ocr_uncertain"
                or asset.get("asset_status") == "ocr_failed"
                or (asset.get("asset_status") == "pending_vision" and (unknown or vision_ready))
            )
            if not eligible:
                continue
            history = asset.get("ocr_attempt_history", [])
            if not isinstance(history, list):
                return Decision("invalid_checkpoint")
            if len(retry_assets) < MAX_AUTOMATIC_OCR_TARGETS:
                retry_assets.append(asset["asset_key"])
                retry_not_before = max(retry_not_before, _timestamp(metadata.get("retry_not_before")))

        if retry_pages or retry_assets:
            reason = (
                "paid_ocr_retry"
                if unknown_outcome
                else "mineru_empty_page_retry"
                if mineru_empty_pages and len(mineru_empty_pages) == len(retry_pages) and not retry_assets
                else "mineru_visual_table_retry"
                if mineru_visual_pages and len(mineru_visual_pages) == len(retry_pages) and not retry_assets
                else "ocr_quality_retry"
            )
            return Decision(
                reason,
                True,
                tuple(retry_pages),
                retry_not_before,
                tuple(retry_assets),
            )
        if unknown_outcome:
            return Decision("paid_result_unknown")
    elif any(_unknown(record) for record in [*pages, *images]):
        return Decision("paid_result_unknown")

    if any(
        pdf_page_status(page) == "ocr_uncertain"
        or page.get("ocr", {}).get("phase") == "recovery"
        or page.get("ocr", {}).get("uncertain_spans")
        for page in pages
    ) or any(image["asset_status"] in ("ocr_uncertain", "image_reference_pending") for image in images):
        return Decision("quality_review")
    failed = [page for page in pages if page.get("status") == "ocr_failed"]
    if any(image["asset_status"] == "ocr_failed" for image in images):
        # Legacy native-image failures lack a bound HTTP error CAS receipt.
        return Decision("image_failure_review")
    if failed:
        if len(failed) > 20:
            return Decision("ocr_retry_scope_review")
        if any(not isinstance(page.get("ocr_retry_cycles", []), list) for page in failed):
            return Decision("invalid_checkpoint")
        if all(
            type(page.get("page")) is int
            and page["page"] > 0
            and page.get("ocr", {}).get("http_status") == 429
            and isinstance(page.get("ocr", {}).get("error_response", {}).get("blob"), dict)
            for page in failed
        ):
            return Decision(
                "ocr_rate_limited",
                True,
                tuple(page["page"] for page in failed),
                max(_timestamp(page["ocr"].get("retry_not_before")) for page in failed),
            )
        return Decision("ocr_failure_review")
    if interrupted or retryable:
        return Decision("transient_processing", True)
    return Decision("processing_review")


class CollectionRecovery:
    def __init__(self, collection):
        self.collection, self.service, self.db = collection, collection.service, collection.db
        self._migrate()

    def _migrate(self):
        with self.db.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)
            ).fetchone():
                conn.execute("""CREATE TABLE IF NOT EXISTS collection_recovery_jobs(
                    id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id),
                    queue_kind TEXT NOT NULL CHECK(queue_kind IN ('fetch','processing')),
                    item_id TEXT NOT NULL,phase TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('waiting','blocked','queued','completed')),
                    reason TEXT NOT NULL,available_at REAL NOT NULL DEFAULT 0,
                    recovery_count INTEGER NOT NULL DEFAULT 0,failure_count INTEGER NOT NULL DEFAULT 0,
                    binding TEXT NOT NULL CHECK(json_valid(binding)),binding_sha256 TEXT NOT NULL,
                    retry_plan TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(retry_plan)),
                    revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    UNIQUE(owner_id,queue_kind,item_id,phase))""")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS recovery_due ON collection_recovery_jobs(status,available_at,id)"
                )
                conn.execute("""CREATE TABLE IF NOT EXISTS collection_recovery_events(
                    id TEXT PRIMARY KEY,recovery_id TEXT NOT NULL REFERENCES collection_recovery_jobs(id),
                    event TEXT NOT NULL,detail TEXT NOT NULL CHECK(json_valid(detail)),created_at TEXT NOT NULL)""")
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS recovery_events ON collection_recovery_events(recovery_id,created_at,id)"
                )
                for operation in ("UPDATE", "DELETE"):
                    conn.execute(f"""CREATE TRIGGER IF NOT EXISTS recovery_events_immutable_{operation.lower()}
                        BEFORE {operation} ON collection_recovery_events
                        BEGIN SELECT RAISE(ABORT,'recovery event is immutable'); END""")
                conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (SCHEMA_VERSION, now()))
            # Reject a marker-only or partially copied migration.
            conn.execute(
                "SELECT id,owner_id,queue_kind,item_id,phase,status,reason,available_at,recovery_count,failure_count,binding,binding_sha256,retry_plan,revision FROM collection_recovery_jobs LIMIT 0"
            )
            conn.execute(
                "SELECT id,recovery_id,event,detail,created_at FROM collection_recovery_events LIMIT 0"
            )

    @staticmethod
    def _event(conn, identifier, event, detail):
        conn.execute(
            "INSERT INTO collection_recovery_events VALUES(?,?,?,?,?)",
            (uid(), identifier, event, dump(detail), now()),
        )

    @staticmethod
    def _binding(conn, owner, kind, item_id, phase):
        if kind == "processing":
            row = conn.execute(
                """SELECT d.id,d.owner_id,d.case_id,d.parent_id,d.role,d.version,d.content_hash,d.size,d.source_url
                FROM documents d JOIN processing_jobs p ON p.document_id=d.id AND p.owner_id=d.owner_id
                WHERE d.id=? AND d.owner_id=?""",
                (item_id, owner),
            ).fetchone()
        else:
            row = conn.execute(
                """SELECT i.id,i.owner_id,i.run_id,i.url,i.identity_key,i.level,i.parent_document_id,
                i.purpose,i.candidate_id,r.source_id,r.plan,s.url AS source_url,s.plan AS source_plan,s.checklist
                FROM collection_items i JOIN collection_runs r ON r.id=i.run_id AND r.owner_id=i.owner_id
                JOIN sources s ON s.id=r.source_id AND s.owner_id=i.owner_id WHERE i.id=? AND i.owner_id=?""",
                (item_id, owner),
            ).fetchone()
        return {"queue_kind": kind, "phase": phase, **dict(row)} if row else None

    @staticmethod
    def _resumable_mineru_parse(conn, owner, document, manifest):
        """Resume only source-bound MinerU batches that already have remote IDs."""
        from app.contexts.documents.infrastructure.providers.mineru import (
            ACTIVE,
            MinerUParser,
            parse_profile_hash,
        )

        if not isinstance(manifest, dict):
            return None
        remote = manifest.get("remote_parse")
        source_hash = document["content_hash"]
        if (
            manifest.get("parser") not in {None, "mineru"}
            or manifest.get("source_hash") not in {None, source_hash}
            or not isinstance(remote, dict)
            or remote.get("provider") != "mineru"
            or remote.get("state") != "pending"
            or remote.get("source_hash") != source_hash
            or not isinstance(remote.get("profile_hash"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", remote["profile_hash"])
            or type(remote.get("page_count")) is not int
            or not 1 <= remote["page_count"] <= 3000
        ):
            return None
        try:
            current_profile = parse_profile_hash(MinerUParser.profile(), remote.get("retry_generation"))
        except ValueError:
            return None
        if current_profile != remote["profile_hash"]:
            return None
        tasks = conn.execute(
            """SELECT part_index,state,batch_id,next_at FROM document_parse_tasks
            WHERE document_id=? AND owner_id=? AND source_hash=? AND profile_hash=? ORDER BY part_index""",
            (document["id"], owner, source_hash, remote["profile_hash"]),
        ).fetchall()
        if not tasks or len(tasks) > remote["page_count"]:
            return None
        if [task["part_index"] for task in tasks] != list(range(len(tasks))):
            return None
        allowed_states = {*ACTIVE, "done"}
        if any(
            task["state"] not in allowed_states
            or not isinstance(task["batch_id"], str)
            or not re.fullmatch(r"[A-Za-z0-9-]{8,100}", task["batch_id"])
            for task in tasks
        ):
            return None
        due = [_timestamp(task["next_at"]) for task in tasks if task["state"] != "done"]
        return {"task_count": len(tasks), "available_at": min(due, default=0)}

    @staticmethod
    def _mineru_parse_tasks(conn, owner, document_id, source_hash, profile_hash):
        return conn.execute(
            """SELECT id,source_hash,profile_hash,part_index,first_page,last_page,part_blob,state,
            batch_id,private_data,result_blob,progress,attempts,next_at,error_code
            FROM document_parse_tasks WHERE document_id=? AND owner_id=? AND source_hash=?
            AND profile_hash=? ORDER BY part_index""",
            (document_id, owner, source_hash, profile_hash),
        ).fetchall()

    @staticmethod
    def _mineru_retry_descriptor(task):
        return {
            "task_id": task["id"],
            "part_index": task["part_index"],
            "first_page": task["first_page"],
            "last_page": task["last_page"],
            "state": task["state"],
            "batch_id": task["batch_id"],
            "error_code": task["error_code"],
            "part_blob_sha256": hashlib.sha256(task["part_blob"].encode()).hexdigest(),
            "private_data_sha256": hashlib.sha256(task["private_data"].encode()).hexdigest(),
            "result_blob_sha256": (
                hashlib.sha256(task["result_blob"].encode()).hexdigest() if task["result_blob"] else None
            ),
            "progress_sha256": hashlib.sha256(task["progress"].encode()).hexdigest(),
            "attempts": task["attempts"],
            "next_at": task["next_at"],
        }

    @staticmethod
    def _mineru_remote_terminal_failure(task):
        code = task["error_code"]
        return task["state"] == "failed" or (
            task["state"] == "blocked"
            and isinstance(code, str)
            and (code == "remote_failed" or re.fullmatch(r"-?\d{1,12}", code) is not None)
        )

    @classmethod
    def _terminal_mineru_retry_valid(cls, conn, owner, document, manifest, plan):
        """Check an automatic retry still points at the same failed source parts."""
        from app.contexts.documents.infrastructure.providers.mineru import (
            ACTIVE,
            MinerUParser,
            parse_profile_hash,
        )

        if not isinstance(plan, dict) or not isinstance(manifest, dict):
            return False
        remote = manifest.get("remote_parse")
        if (
            not isinstance(remote, dict)
            or remote.get("provider") != "mineru"
            or remote.get("source_hash") != document["content_hash"]
            or document["content_hash"] != plan.get("source_hash")
            or remote.get("retry_source_profile_hash") != plan.get("source_profile_hash")
            or remote.get("retry_generation") != plan.get("retry_generation")
            or remote.get("profile_hash") != plan.get("retry_profile_hash")
            or not isinstance(remote.get("profile"), dict)
            or not isinstance(plan.get("tasks"), list)
        ):
            return False
        try:
            if (
                parse_profile_hash(remote["profile"], remote["retry_generation"])
                != plan["retry_profile_hash"]
                or parse_profile_hash(MinerUParser.profile(), remote["retry_generation"])
                != plan["retry_profile_hash"]
            ):
                return False
        except ValueError:
            return False
        old_tasks = cls._mineru_parse_tasks(
            conn,
            owner,
            document["id"],
            document["content_hash"],
            plan["source_profile_hash"],
        )
        if not old_tasks or len(old_tasks) != len(plan["tasks"]):
            return False
        if [task["part_index"] for task in old_tasks] != list(range(len(old_tasks))):
            return False
        expected = {item.get("task_id"): item for item in plan["tasks"] if isinstance(item, dict)}
        if len(expected) != len(plan["tasks"]):
            return False
        if any(
            task["id"] not in expected
            or cls._mineru_retry_descriptor(task) != expected[task["id"]]
            and not (
                expected[task["id"]].get("state") in ACTIVE
                and task["state"] == "superseded"
                and {
                    **cls._mineru_retry_descriptor(task),
                    "state": expected[task["id"]]["state"],
                }
                == expected[task["id"]]
            )
            for task in old_tasks
        ):
            return False
        expected_parts = {item["part_index"]: item for item in plan["tasks"]}
        if not any(cls._mineru_remote_terminal_failure(item) for item in plan["tasks"]):
            return False
        new_tasks = cls._mineru_parse_tasks(
            conn,
            owner,
            document["id"],
            document["content_hash"],
            plan["retry_profile_hash"],
        )
        if len(new_tasks) != len(old_tasks):
            return False
        for task in new_tasks:
            original = expected_parts.get(task["part_index"])
            if original is None:
                return False
            failed = cls._mineru_remote_terminal_failure(original)
            if (
                task["source_hash"] != document["content_hash"]
                or task["profile_hash"] != plan["retry_profile_hash"]
                or hashlib.sha256(task["part_blob"].encode()).hexdigest() != original["part_blob_sha256"]
                or task["state"] != ("planned" if failed else original["state"])
                or task["batch_id"] != (None if failed else original["batch_id"])
                or (failed and (task["private_data"] != "{}" or task["result_blob"] is not None))
                or (
                    not failed
                    and task["private_data"]
                    != next(old["private_data"] for old in old_tasks if old["id"] == original["task_id"])
                )
                or (
                    not failed
                    and task["result_blob"]
                    != next(old["result_blob"] for old in old_tasks if old["id"] == original["task_id"])
                )
            ):
                return False
        return True

    @classmethod
    def _quality_mineru_retry_valid(cls, conn, owner, document, manifest, plan):
        """Verify a fresh MinerU generation targets only the saved quality pages."""
        from app.contexts.documents.infrastructure.providers.mineru import (
            MinerUParser,
            parse_profile_hash,
        )

        if not isinstance(plan, dict) or plan.get("retry_kind") != "decode_quality":
            return False
        remote = manifest.get("remote_parse") if isinstance(manifest, dict) else None
        old_hash, retry_hash, generation = (
            plan.get("source_profile_hash"),
            plan.get("retry_profile_hash"),
            plan.get("retry_generation"),
        )
        if (
            not isinstance(remote, dict)
            or remote.get("provider") != "mineru"
            or remote.get("state") != "pending"
            or remote.get("source_hash") != document["content_hash"]
            or plan.get("source_hash") != document["content_hash"]
            or remote.get("retry_source_profile_hash") != old_hash
            or remote.get("retry_generation") != generation
            or remote.get("profile_hash") != retry_hash
            or not isinstance(remote.get("profile"), dict)
            or not isinstance(old_hash, str)
            or not isinstance(retry_hash, str)
            or not re.fullmatch(r"[a-f0-9]{64}", old_hash)
            or not re.fullmatch(r"[a-f0-9]{64}", retry_hash)
            or not isinstance(generation, str)
            or not isinstance(plan.get("tasks"), list)
        ):
            return False
        try:
            if (
                parse_profile_hash(remote["profile"], generation) != retry_hash
                or parse_profile_hash(MinerUParser.profile(), generation) != retry_hash
            ):
                return False
        except ValueError:
            return False

        retry_pages = plan.get("retry_pages")
        retry_parts = plan.get("retry_part_indexes")
        pages = manifest.get("pages")
        if (
            not isinstance(retry_pages, list)
            or not retry_pages
            or any(type(page) is not int or page < 1 for page in retry_pages)
            or len(set(retry_pages)) != len(retry_pages)
            or not isinstance(retry_parts, list)
            or not retry_parts
            or any(type(index) is not int or index < 0 for index in retry_parts)
            or len(set(retry_parts)) != len(retry_parts)
            or not isinstance(pages, list)
            or any(
                page > len(pages)
                or not _is_decode_replacement_page(pages[page - 1], document["content_hash"])
                for page in retry_pages
            )
        ):
            return False

        old_tasks = cls._mineru_parse_tasks(conn, owner, document["id"], document["content_hash"], old_hash)
        if not old_tasks or len(old_tasks) != len(plan["tasks"]):
            return False
        if [task["part_index"] for task in old_tasks] != list(range(len(old_tasks))):
            return False
        expected = {item.get("task_id"): item for item in plan["tasks"] if isinstance(item, dict)}
        if len(expected) != len(plan["tasks"]) or any(
            task["id"] not in expected
            or task["state"] != "done"
            or cls._mineru_retry_descriptor(task) != expected[task["id"]]
            for task in old_tasks
        ):
            return False
        if any(index >= len(old_tasks) for index in retry_parts):
            return False
        new_tasks = cls._mineru_parse_tasks(conn, owner, document["id"], document["content_hash"], retry_hash)
        if len(new_tasks) != len(old_tasks):
            return False
        retry_set = set(retry_parts)
        for task in new_tasks:
            original = old_tasks[task["part_index"]]
            should_retry = task["part_index"] in retry_set
            if (
                task["source_hash"] != document["content_hash"]
                or task["profile_hash"] != retry_hash
                or hashlib.sha256(task["part_blob"].encode()).hexdigest()
                != hashlib.sha256(original["part_blob"].encode()).hexdigest()
                or task["state"] != ("planned" if should_retry else "done")
                or task["batch_id"] != (None if should_retry else original["batch_id"])
                or (should_retry and (task["private_data"] != "{}" or task["result_blob"] is not None))
                or (not should_retry and task["result_blob"] != original["result_blob"])
            ):
                return False
        return True

    def _schedule_mineru_retry_generation(
        self,
        conn,
        *,
        recovery,
        document,
        job,
        manifest,
        old_hash,
        old_tasks,
        retry_parts,
        reason,
        event,
        retry_pages=(),
    ):
        """Create a source-bound MinerU generation and preserve reusable part receipts."""
        from app.contexts.documents.infrastructure.providers.mineru import (
            ACTIVE,
            MinerUParser,
            parse_profile_hash,
        )
        from app.contexts.documents.infrastructure.providers.mineru_results import (
            MAX_SOURCE_BYTES,
            reference,
        )
        from app.shared.infrastructure.persistence.blob_store import BlobStore

        retry_generation = "auto-" + uid()
        retry_hash = parse_profile_hash(MinerUParser.profile(), retry_generation)
        if self._mineru_parse_tasks(
            conn, document["owner_id"], document["id"], document["content_hash"], retry_hash
        ):
            return False
        prior_blob = BlobStore(self.db.files_dir, max_bytes=MAX_SOURCE_BYTES).put_bytes(
            document["owner_id"], dump(manifest).encode()
        )
        previous_manifest = reference(prior_blob)
        descriptors = [self._mineru_retry_descriptor(task) for task in old_tasks]
        retry_set = set(retry_parts)
        new_ids = {}
        for task in old_tasks:
            retry_part = task["part_index"] in retry_set
            task_id = uid()
            new_ids[task["part_index"]] = task_id
            state = "planned" if retry_part else task["state"]
            conn.execute(
                """INSERT INTO document_parse_tasks(
                id,document_id,owner_id,source_hash,profile_hash,part_index,first_page,last_page,
                part_blob,state,batch_id,private_data,result_blob,progress,attempts,next_at,error_code,
                created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    task_id,
                    document["id"],
                    document["owner_id"],
                    document["content_hash"],
                    retry_hash,
                    task["part_index"],
                    task["first_page"],
                    task["last_page"],
                    task["part_blob"],
                    state,
                    None if retry_part else task["batch_id"],
                    "{}" if retry_part else task["private_data"],
                    None if retry_part else task["result_blob"],
                    "{}" if retry_part else task["progress"],
                    0 if retry_part else task["attempts"],
                    0 if retry_part else task["next_at"],
                    None if retry_part else task["error_code"],
                    now(),
                    now(),
                ),
            )
        active_states = tuple(ACTIVE)
        conn.execute(
            "UPDATE document_parse_tasks SET state='superseded' WHERE document_id=? AND owner_id=? AND source_hash=? AND profile_hash=? AND state IN ("
            + ",".join("?" for _ in active_states)
            + ")",
            (document["id"], document["owner_id"], document["content_hash"], old_hash, *active_states),
        )
        updated_remote = {
            **manifest["remote_parse"],
            "retry_source_profile_hash": old_hash,
            "retry_generation": retry_generation,
            "profile_hash": retry_hash,
            "previous_manifest": previous_manifest,
            "state": "pending",
            "tasks": [
                {
                    "task_id": new_ids[task["part_index"]],
                    "first_page": task["first_page"],
                    "last_page": task["last_page"],
                    "state": "planned" if task["part_index"] in retry_set else task["state"],
                    "progress": {} if task["part_index"] in retry_set else json.loads(task["progress"]),
                    "error_code": None if task["part_index"] in retry_set else task["error_code"],
                }
                for task in old_tasks
            ],
        }
        manifest["remote_parse"] = updated_remote
        conn.execute(
            "UPDATE documents SET manifest=? WHERE id=? AND owner_id=?",
            (dump(manifest), document["id"], document["owner_id"]),
        )
        timestamp = time.time()
        delay = (
            BASE_RECOVERY_SECONDS
            if reason == "mineru_quality_retry"
            else min(
                MAX_RECOVERY_SECONDS,
                BASE_RECOVERY_SECONDS * 2 ** min(max(recovery["failure_count"] - 1, 0), 7),
            )
        )
        available = max(timestamp + delay, _timestamp(job["available_at"]))
        retry_plan = {
            "source_hash": document["content_hash"],
            "source_profile_hash": old_hash,
            "retry_profile_hash": retry_hash,
            "retry_generation": retry_generation,
            "tasks": descriptors,
        }
        if reason == "mineru_quality_retry":
            retry_plan.update(
                retry_kind="decode_quality",
                retry_part_indexes=sorted(retry_set),
                retry_pages=list(retry_pages),
            )
        conn.execute(
            """UPDATE collection_recovery_jobs SET status='waiting',reason=?,available_at=?,retry_plan=?,
            revision=revision+1,updated_at=? WHERE id=?""",
            (reason, available, dump(retry_plan), now(), recovery["id"]),
        )
        self._event(
            conn,
            recovery["id"],
            event,
            {
                "source_hash": document["content_hash"],
                "retry_parts": len(retry_set),
                "reused_parts": len(old_tasks) - len(retry_set),
                "retry_pages": list(retry_pages),
                "retry_generation": retry_generation,
                "retry_after_seconds": int(available - timestamp),
                "duplicate_provider_charge_possible": True,
            },
        )
        return True

    def record_failure(self, conn, claim, *, processing, decision, exhausted, ready_at=0, error_type=None):
        kind, phase = ("processing", claim["phase"]) if processing else ("fetch", "fetch")
        item_id = claim["document_id"] if processing else claim["id"]
        owner = claim["owner_id"]
        binding = self._binding(conn, owner, kind, item_id, phase)
        if binding is None:
            return
        fingerprint = _digest(binding)
        row = conn.execute(
            "SELECT * FROM collection_recovery_jobs WHERE owner_id=? AND queue_kind=? AND item_id=? AND phase=?",
            (owner, kind, item_id, phase),
        ).fetchone()
        timestamp = time.time()
        if row is None:
            identifier = uid()
            conn.execute(
                """INSERT INTO collection_recovery_jobs(id,owner_id,queue_kind,item_id,phase,status,reason,binding,binding_sha256,created_at,updated_at)
                VALUES(?,?,?,?,?,'queued',?,?,?,?,?)""",
                (
                    identifier,
                    owner,
                    kind,
                    item_id,
                    phase,
                    decision.reason,
                    dump(binding),
                    fingerprint,
                    now(),
                    now(),
                ),
            )
            row = conn.execute("SELECT * FROM collection_recovery_jobs WHERE id=?", (identifier,)).fetchone()
        if row["binding_sha256"] != fingerprint:
            decision = Decision("source_changed")
            exhausted = True
        delay = _recovery_delay(decision, row["recovery_count"])
        available = (
            max(timestamp + delay, _timestamp(ready_at), decision.not_before)
            if exhausted and decision.retryable
            else _timestamp(ready_at)
        )
        status = ("waiting" if decision.retryable else "blocked") if exhausted else "queued"
        retry_plan = []
        if processing and (decision.pages or decision.asset_keys):
            manifest = json.loads(
                conn.execute(
                    "SELECT manifest FROM documents WHERE id=? AND owner_id=?", (item_id, owner)
                ).fetchone()[0]
            )
            retry_plan = [
                {"page": page["page"], "checkpoint_sha256": _digest(page)}
                for page in manifest.get("pages", [])
                if page.get("page") in decision.pages
            ]
            from app.contexts.documents.infrastructure.providers.native_asset_ocr import assets

            retry_plan.extend(
                {"asset_key": asset["asset_key"], "checkpoint_sha256": _digest(asset)}
                for asset in assets(manifest)
                if asset["asset_key"] in decision.asset_keys
            )
        conn.execute(
            """UPDATE collection_recovery_jobs SET status=?,reason=?,available_at=?,failure_count=failure_count+1,
            retry_plan=?,revision=revision+1,updated_at=? WHERE id=?""",
            (status, decision.reason, available, dump(retry_plan), now(), row["id"]),
        )
        detail = {
            "queue_attempt": claim.get("attempts", 0),
            "reason": decision.reason,
            "retryable": decision.retryable,
            "exhausted": exhausted,
            "available_at": available,
            "binding_sha256": fingerprint,
            "error_type": error_type,
            "physical_pages": list(decision.pages),
            "assets": list(decision.asset_keys),
            "retry_plan": retry_plan,
        }
        self._event(conn, row["id"], "attempt_failed", detail)

    def processing_decision(self, conn, claim, **kwargs):
        row = conn.execute(
            "SELECT manifest FROM documents WHERE id=? AND owner_id=?",
            (claim["document_id"], claim["owner_id"]),
        ).fetchone()
        try:
            manifest = json.loads(row["manifest"]) if row else None
        except (ValueError, TypeError):
            return Decision("invalid_checkpoint")
        return classify_processing(manifest, phase=claim["phase"], **kwargs)

    def completed(self, conn, claim, *, processing):
        kind, phase = ("processing", claim["phase"]) if processing else ("fetch", "fetch")
        item_id = claim["document_id"] if processing else claim["id"]
        row = conn.execute(
            "SELECT * FROM collection_recovery_jobs WHERE owner_id=? AND queue_kind=? AND item_id=? AND phase=?",
            (claim["owner_id"], kind, item_id, phase),
        ).fetchone()
        if row and row["status"] != "completed":
            conn.execute(
                "UPDATE collection_recovery_jobs SET status='completed',revision=revision+1,updated_at=? WHERE id=?",
                (now(), row["id"]),
            )
            self._event(conn, row["id"], "completed", {"queue_attempt": claim.get("attempts", 0)})

    def _block(self, conn, row, reason):
        conn.execute(
            "UPDATE collection_recovery_jobs SET status='blocked',reason=?,revision=revision+1,updated_at=? WHERE id=?",
            (reason, now(), row["id"]),
        )
        self._event(conn, row["id"], "blocked", {"reason": reason})

    def _retry_pages(self, conn, row, document, manifest, decision):
        """Archive exact failed OCR checkpoints in CAS before opening a new burst."""
        from app.contexts.documents.infrastructure.parsers.native_parser import pdf_quality
        from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import read_image_blob
        from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import (
            EMPTY_PAGE_PROTOCOL as MINERU_EMPTY_PAGE_FALLBACK_PROTOCOL,
            MAX_RETRY_CYCLES as MAX_MINERU_VISUAL_RETRIES,
            PROTOCOL as MINERU_VISUAL_FALLBACK_PROTOCOL,
            fallback_attempts,
            is_mineru_empty_page_review,
            is_mineru_table_review,
            is_visual_fallback_page,
        )
        from app.contexts.documents.infrastructure.recovery.ocr_error_evidence import (
            validate_response_binding,
        )

        updated = deepcopy(manifest)
        by_number = {page.get("page"): page for page in updated.get("pages", [])}
        receipts = []
        for number in decision.pages:
            page = by_number[number]
            if page.get("source_hash") != document["content_hash"]:
                raise ValueError("retry_page_source_mismatch")
            metadata = page.get("ocr") or {}
            unreceipted_vision_failure = _unreceipted_vision_failure(
                page, document["content_hash"]
            )
            legacy_render_mismatch = _legacy_mineru_render_source_proof_mismatch(
                manifest, page
            )
            previous_failure = (
                _saved_visual_provider_retry(page) if page.get("status") == "pending_ocr" else None
            )
            vision_page = metadata.get("provider") != "mineru" and bool(
                metadata.get("prompt_version") or metadata.get("request_id")
            )
            envelope = metadata.get("error_response")
            evidence_page, evidence_metadata = page, metadata
            if previous_failure is not None and not (
                isinstance(envelope, dict) and isinstance(envelope.get("blob"), dict)
            ):
                evidence_page = previous_failure
                evidence_metadata = previous_failure.get("ocr") or {}
                envelope = evidence_metadata.get("error_response")
            bound_failed_vision = (
                page.get("status") == "ocr_failed"
                and vision_page
                and isinstance(envelope, dict)
                and isinstance(envelope.get("blob"), dict)
            )
            retryable_page = legacy_render_mismatch or (
                page.get("status") in {"ocr_failed", "ocr_uncertain", "pending_ocr"}
                and (
                    _unknown(page)
                    or unreceipted_vision_failure
                    or previous_failure is not None
                    or (
                        vision_page
                        and (
                            metadata.get("complete") is False
                            or page.get("status") == "ocr_uncertain"
                            or bound_failed_vision
                        )
                    )
                )
            )
            cycles = page.get("ocr_retry_cycles", [])
            if not isinstance(cycles, list):
                raise ValueError("retry_page_checkpoint_not_eligible")
            if not retryable_page:
                mineru_fallback = is_mineru_table_review(page) and decision.reason in {
                    "mineru_visual_table_retry",
                    "paid_ocr_retry",
                    "ocr_quality_retry",
                }
                visual_fallback = is_visual_fallback_page(page)
                mineru_empty_fallback = (
                    decision.reason in {"mineru_empty_page_retry", "ocr_quality_retry", "paid_ocr_retry"}
                    and is_mineru_empty_page_review(page)
                )
                try:
                    retryable_visual = visual_fallback and fallback_attempts(page) < MAX_MINERU_VISUAL_RETRIES
                except ValueError:
                    retryable_visual = False
                if not (mineru_fallback or mineru_empty_fallback or retryable_visual):
                    raise ValueError("retry_page_checkpoint_not_eligible")
            if isinstance(envelope, dict) and isinstance(envelope.get("blob"), dict):
                raw = read_image_blob(self.db.data_dir, row["owner_id"], envelope["blob"])
                evidence = json.loads(raw)
                validate_response_binding(evidence, document["content_hash"], evidence_page)
                if any(evidence.get(key) != value for key, value in envelope.items() if key != "blob"):
                    raise ValueError("retry_requires_bound_ocr_error_receipt")
                if evidence_metadata.get("http_status") == 429 and evidence.get("http_status") != 429:
                    raise ValueError("retry_requires_bound_429_receipt")
            elif bound_failed_vision:
                raise ValueError("retry_requires_bound_ocr_error_receipt")
            previous = deepcopy(page)
            # Keep every previous checkpoint addressable from the current page,
            # but don't recursively duplicate all earlier cycles into each new
            # checkpoint. That keeps the manifest growth linear over retries.
            previous.pop("ocr_retry_cycles", None)
            payload = {
                "protocol": "ocr-automatic-retry-checkpoint-v1",
                "owner_id": row["owner_id"],
                "document_id": document["id"],
                "version": document["version"],
                "source_hash": document["content_hash"],
                "page": number,
                "recovery_id": row["id"],
                "recovery_count": row["recovery_count"] + 1,
                "previous_page": previous,
                "previous_page_sha256": _digest(previous),
            }
            encoded = _encoded(payload)
            if len(encoded) > MAX_AUTOMATIC_OCR_CHECKPOINT_BYTES:
                raise ValueError("retry_page_checkpoint_too_large")
            blob = self.service.archive.blobs.put_bytes(row["owner_id"], encoded)
            reference = {
                "content_hash": blob.digest,
                "size": blob.size,
                "storage_path": str(blob.path.relative_to(self.db.data_dir)),
                "mime_type": "application/json",
            }
            # Keep the prior output inline so import and backup walkers continue
            # to discover nested image and error-response blobs.
            page["ocr_retry_cycles"] = [
                *page.get("ocr_retry_cycles", []),
                {
                    "protocol": payload["protocol"],
                    "blob": reference,
                    "previous_page_sha256": payload["previous_page_sha256"],
                    "previous_page": previous,
                },
            ]
            if is_mineru_table_review(previous):
                page["ocr_fallback"] = {
                    "protocol": MINERU_VISUAL_FALLBACK_PROTOCOL,
                    "attempts": 1,
                    "source_result_hash": metadata.get("result_hash"),
                    "source_page": number,
                }
            elif is_mineru_empty_page_review(previous):
                page["ocr_fallback"] = {
                    "protocol": MINERU_EMPTY_PAGE_FALLBACK_PROTOCOL,
                    "attempts": 1,
                    "source_result_hash": metadata.get("result_hash"),
                    "source_page": number,
                }
            elif is_visual_fallback_page(page):
                fallback = deepcopy(page["ocr_fallback"])
                fallback["attempts"] = min(
                    MAX_MINERU_VISUAL_RETRIES,
                    fallback_attempts(page) + 1,
                )
                fallback.pop("accepted", None)
                fallback.pop("text_sha256", None)
                fallback.pop("prompt_version", None)
                page["ocr_fallback"] = fallback
            page.update(status="pending_ocr", ocr={"attempts": 0})
            for key in ("error", "deferred_reason", "ocr_attempt_history", "ocr_retry_history"):
                page.pop(key, None)
            receipts.append({"page": number, "checkpoint": reference})
        updated["quality"] = pdf_quality(updated["pages"])
        if len(_encoded(updated)) > 40 * 1024 * 1024:
            raise ValueError("retry_manifest_too_large")
        conn.execute(
            "UPDATE documents SET manifest=? WHERE id=? AND owner_id=?",
            (dump(updated), document["id"], row["owner_id"]),
        )
        return receipts

    def _retry_assets(self, conn, row, document, decision):
        """Preserve prior image outputs and reset only exact source-bound assets."""
        from app.contexts.documents.infrastructure.providers.native_asset_ocr import (
            assets,
            prepare_asset_retries,
        )

        current = conn.execute(
            "SELECT manifest FROM documents WHERE id=? AND owner_id=?",
            (document["id"], row["owner_id"]),
        ).fetchone()
        manifest = json.loads(current["manifest"]) if current else None
        if not isinstance(manifest, dict) or manifest.get("source_hash") != document["content_hash"]:
            raise ValueError("retry_asset_source_mismatch")
        selected = [asset for asset in assets(manifest) if asset["asset_key"] in decision.asset_keys]
        if len(selected) != len(decision.asset_keys) or any(
            asset["asset_status"] not in {"ocr_failed", "ocr_uncertain", "pending_vision"}
            or not isinstance(asset.get("ocr_attempt_history", []), list)
            for asset in selected
        ):
            raise ValueError("retry_asset_checkpoint_not_eligible")
        updated = prepare_asset_retries(
            manifest,
            statuses=("ocr_failed", "ocr_uncertain", "pending_vision"),
            asset_keys=decision.asset_keys,
            max_assets=MAX_AUTOMATIC_OCR_TARGETS,
        )
        conn.execute(
            "UPDATE documents SET manifest=? WHERE id=? AND owner_id=?",
            (dump(updated), document["id"], row["owner_id"]),
        )
        return [
            {"asset_key": asset["asset_key"], "previous_checkpoint_sha256": _digest(asset)}
            for asset in selected
        ]

    def _resume(self, conn, row, timestamp, *, worker_scope=BackgroundWorkerScope()):
        if not worker_scope.allows(row["owner_id"]):
            return "deferred"  # An excluded row must not gain an event or state change.
        scope, arguments = worker_scope.sql()
        binding = self._binding(conn, row["owner_id"], row["queue_kind"], row["item_id"], row["phase"])
        if binding is None or _digest(binding) != row["binding_sha256"]:
            self._block(conn, row, "source_changed")
            return "blocked"
        processing = row["queue_kind"] == "processing"
        table, key = ("processing_jobs", "document_id") if processing else ("collection_items", "id")
        job = conn.execute(
            f"SELECT * FROM {table} WHERE {key}=? AND owner_id=?", (row["item_id"], row["owner_id"])
        ).fetchone()
        if job["status"] != "failed" or (processing and job["phase"] != row["phase"]):
            self._block(conn, row, "queue_changed")
            return "blocked"
        if _timestamp(job["available_at"]) > timestamp:
            self._defer(conn, row, job["available_at"])
            return "deferred"
        receipts = []
        if processing:
            document = conn.execute(
                "SELECT * FROM documents WHERE id=? AND owner_id=?", (row["item_id"], row["owner_id"])
            ).fetchone()
            manifest = json.loads(document["manifest"])
            if row["reason"] == "html_parser_refresh":
                from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID

                try:
                    plan = json.loads(row["retry_plan"])
                except (TypeError, ValueError):
                    self._block(conn, row, "html_parser_retry_plan_invalid")
                    return "blocked"
                expected = _legacy_html_parser_refresh_plan(manifest, document["content_hash"])
                if (
                    document["owner_id"] != SYSTEM_OWNER_ID
                    or document["mime_type"] != "text/html"
                    or not document["source_url"]
                    or expected is None
                    or plan != expected
                ):
                    self._block(conn, row, "html_parser_checkpoint_changed")
                    return "blocked"
                decision = Decision("html_parser_refresh", True)
            elif row["reason"] == "mineru_parse_resume":
                resumable = self._resumable_mineru_parse(conn, row["owner_id"], document, manifest)
                if resumable is None:
                    self._block(conn, row, "mineru_checkpoint_changed")
                    return "blocked"
                decision = Decision("mineru_parse_resume", True, not_before=resumable["available_at"])
            elif row["reason"] == "mineru_terminal_retry":
                try:
                    retry_plan = json.loads(row["retry_plan"])
                except (TypeError, ValueError):
                    self._block(conn, row, "mineru_retry_plan_invalid")
                    return "blocked"
                if not self._terminal_mineru_retry_valid(
                    conn, row["owner_id"], document, manifest, retry_plan
                ):
                    self._block(conn, row, "mineru_retry_checkpoint_changed")
                    return "blocked"
                decision = Decision("mineru_terminal_failure_retry", True)
            elif row["reason"] == "mineru_quality_retry":
                try:
                    retry_plan = json.loads(row["retry_plan"])
                except (TypeError, ValueError):
                    self._block(conn, row, "mineru_retry_plan_invalid")
                    return "blocked"
                if not self._quality_mineru_retry_valid(
                    conn, row["owner_id"], document, manifest, retry_plan
                ):
                    self._block(conn, row, "mineru_retry_checkpoint_changed")
                    return "blocked"
                decision = Decision("mineru_quality_retry", True)
            else:
                decision = classify_processing(manifest, phase=row["phase"], retryable=True)
            if not decision.retryable:
                self._block(conn, row, decision.reason)
                return "blocked"
            if decision.not_before > timestamp:
                self._defer(conn, row, decision.not_before)
                return "deferred"
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')" + scope,
                    arguments,
                ).fetchone()[0]
                >= PROCESSING_QUEUE_LIMIT + RECOVERY_QUEUE_RESERVE
            ):
                self._defer(conn, row, timestamp)
                return "deferred"
            if decision.pages or decision.asset_keys:
                from app.contexts.documents.infrastructure.providers.native_asset_ocr import assets

                expected = json.loads(row["retry_plan"])
                actual_pages = [
                    {"page": page["page"], "checkpoint_sha256": _digest(page)}
                    for page in manifest.get("pages", [])
                    if page.get("page") in decision.pages
                ]
                actual_assets = [
                    {"asset_key": asset["asset_key"], "checkpoint_sha256": _digest(asset)}
                    for asset in assets(manifest)
                    if asset["asset_key"] in decision.asset_keys
                ]
                actual = [*actual_pages, *actual_assets]
                if (
                    expected != actual
                    or len({item["page"] for item in actual_pages}) != len(decision.pages)
                    or len({item["asset_key"] for item in actual_assets}) != len(decision.asset_keys)
                ):
                    self._block(conn, row, "checkpoint_changed")
                    return "blocked"
                try:
                    if decision.pages:
                        receipts.extend(self._retry_pages(conn, row, document, manifest, decision))
                    if decision.asset_keys:
                        receipts.extend(self._retry_assets(conn, row, document, decision))
                except (OSError, ValueError, TypeError, KeyError):
                    self._block(conn, row, "retry_evidence_invalid")
                    return "blocked"
            conn.execute(
                "UPDATE documents SET status='processing',error=NULL WHERE id=? AND owner_id=?",
                (row["item_id"], row["owner_id"]),
            )
        else:
            run = conn.execute(
                "SELECT * FROM collection_runs WHERE id=? AND owner_id=?", (job["run_id"], row["owner_id"])
            ).fetchone()
            if run is None or run["status"] == "cancelled":
                self._block(conn, row, "cancelled")
                return "blocked"
            if conn.execute(
                "SELECT 1 FROM collection_runs WHERE source_id=? AND id<>? AND status IN ('queued','running')",
                (run["source_id"], run["id"]),
            ).fetchone():
                self._defer(conn, row, timestamp)
                return "deferred"
            if (
                conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE status IN ('queued','fetching')" + scope,
                    arguments,
                ).fetchone()[0]
                >= 2000
                or conn.execute(
                    "SELECT COUNT(*) FROM collection_items WHERE owner_id=? AND status IN ('queued','fetching')",
                    (row["owner_id"],),
                ).fetchone()[0]
                >= 1000
            ):
                self._defer(conn, row, timestamp)
                return "deferred"
            if job["candidate_id"]:
                candidate = conn.execute(
                    """SELECT c.* FROM checklist_candidates c JOIN checklist_bindings b ON b.id=c.binding_id
                    WHERE c.id=? AND b.owner_id=? AND b.source_id=? AND c.url=?""",
                    (job["candidate_id"], row["owner_id"], run["source_id"], job["url"]),
                ).fetchone()
                if candidate is None or candidate["status"] not in ("pending", "stored"):
                    self._block(conn, row, "candidate_changed")
                    return "blocked"
                conn.execute(
                    "UPDATE checklist_candidates SET status='queued',revision=revision+1 WHERE id=?",
                    (candidate["id"],),
                )
            conn.execute(
                "UPDATE collection_runs SET status='queued',revision=revision+1,updated_at=? WHERE id=?",
                (now(), run["id"]),
            )
        conn.execute(
            f"UPDATE {table} SET status='queued',attempts=0,available_at=?,lease_token=NULL,lease_until=NULL,error=NULL WHERE {key}=? AND owner_id=? AND status='failed'",
            (timestamp, row["item_id"], row["owner_id"]),
        )
        conn.execute(
            "UPDATE collection_recovery_jobs SET status='queued',recovery_count=recovery_count+1,revision=revision+1,updated_at=? WHERE id=?",
            (now(), row["id"]),
        )
        self._event(
            conn,
            row["id"],
            "requeued",
            {
                "previous_queue_attempts": job["attempts"],
                "recovery_count": row["recovery_count"] + 1,
                "binding_sha256": row["binding_sha256"],
                "checkpoints": receipts,
            },
        )
        if processing:
            self.collection._refresh_document(conn, row["item_id"])
        else:
            self.collection._refresh(conn, job["run_id"])
        return "requeued"

    @staticmethod
    def _defer(conn, row, timestamp):
        conn.execute(
            "UPDATE collection_recovery_jobs SET available_at=MAX(available_at,?),revision=revision+1,updated_at=? WHERE id=?",
            (timestamp + 60, now(), row["id"]),
        )

    def adopt_failed(self, conn, *, limit, worker_scope=BackgroundWorkerScope()):
        """Inventory legacy failures conservatively; never guess paid outcomes."""
        scope, arguments = worker_scope.sql("j.owner_id")
        adopted = 0
        for kind, table, key, phase in (
            ("fetch", "collection_items", "id", "'fetch'"),
            ("processing", "processing_jobs", "document_id", "j.phase"),
        ):
            rows = conn.execute(
                f"""SELECT j.* FROM {table} j WHERE j.status='failed' AND NOT EXISTS(
                SELECT 1 FROM collection_recovery_jobs r WHERE r.owner_id=j.owner_id AND r.queue_kind=? AND r.item_id=j.{key} AND r.phase={phase})"""
                + scope
                + f" ORDER BY j.{key} LIMIT ?",
                (kind, *arguments, limit - adopted),
            ).fetchall()
            for row in rows:
                claim = dict(row)
                decision = (
                    self.processing_decision(conn, claim)
                    if kind == "processing"
                    else Decision("legacy_failure_review")
                )
                self.record_failure(
                    conn,
                    claim,
                    processing=kind == "processing",
                    decision=decision,
                    exhausted=True,
                    error_type="legacy_checkpoint",
                )
                adopted += 1
            if adopted >= limit:
                break
        return adopted

    def _queue_legacy_chart_refreshes(self, *, limit, worker_scope):
        """Upgrade old chart-only review holds once their full MinerU receipts verify."""
        from app.contexts.documents.infrastructure.providers.vision import vision_ocr_configured

        if not vision_ocr_configured():
            return 0
        scope, arguments = worker_scope.sql("j.owner_id")
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='parse' AND r.status='blocked'
                        AND r.reason='quality_review' AND j.phase='parse' AND j.status='failed'"""
                    + scope
                    + " ORDER BY r.updated_at,r.id LIMIT ?",
                    (*arguments, limit),
                )
            ]
        promoted = 0
        for identifier in identifiers:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked' AND reason='quality_review'",
                    (identifier,),
                ).fetchone()
                if row is None or not worker_scope.allows(row["owner_id"]):
                    continue
                binding = self._binding(conn, row["owner_id"], "processing", row["item_id"], "parse")
                if binding is None or _digest(binding) != row["binding_sha256"]:
                    continue
                document = conn.execute(
                    "SELECT manifest,content_hash FROM documents WHERE id=? AND owner_id=?",
                    (row["item_id"], row["owner_id"]),
                ).fetchone()
                job = conn.execute(
                    "SELECT phase,status FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (row["item_id"], row["owner_id"]),
                ).fetchone()
                if document is None or job is None or job["phase"] != "parse" or job["status"] != "failed":
                    continue
                try:
                    manifest = json.loads(document["manifest"])
                except (TypeError, ValueError):
                    continue
                if (
                    not isinstance(manifest, dict)
                    or manifest.get("source_hash") != document["content_hash"]
                    or not _legacy_mineru_chart_refresh(manifest)
                ):
                    continue
                part_hashes = [
                    part["result_blob"]["content_hash"] for part in manifest["remote_parse"]["parts"]
                ]
                page_count = sum("chart_content_missing" in page["reasons"] for page in manifest["pages"])
                conn.execute(
                    """UPDATE collection_recovery_jobs SET status='waiting',reason='mineru_chart_refresh',
                    available_at=0,retry_plan='[]',revision=revision+1,updated_at=? WHERE id=?""",
                    (now(), identifier),
                )
                self._event(
                    conn,
                    identifier,
                    "chart_transcription_upgrade_queued",
                    {
                        "source_hash": document["content_hash"],
                        "result_hashes": part_hashes,
                        "chart_review_pages": page_count,
                        "remote_tasks_resubmitted": False,
                    },
                )
                promoted += 1
        return promoted

    def _queue_legacy_html_parser_refreshes(self, *, limit, worker_scope):
        """Reparse failed official HTML after a mapping upgrade, with a small retry cap."""
        from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID

        scope, arguments = worker_scope.sql("j.owner_id")
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    JOIN documents d ON d.id=r.item_id AND d.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='parse' AND r.status='blocked'
                        AND r.reason='processing_review' AND j.phase='parse' AND j.status='failed'
                        AND d.owner_id=? AND d.mime_type='text/html' AND d.source_url IS NOT NULL"""
                    + scope
                    + " ORDER BY r.updated_at,r.id LIMIT ?",
                    (SYSTEM_OWNER_ID, *arguments, limit),
                )
            ]
        scheduled = 0
        for identifier in identifiers:
            with self.db.transaction() as conn:
                recovery = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked' AND reason='processing_review'",
                    (identifier,),
                ).fetchone()
                if recovery is None or not worker_scope.allows(recovery["owner_id"]):
                    continue
                binding = self._binding(
                    conn, recovery["owner_id"], "processing", recovery["item_id"], "parse"
                )
                if binding is None or _digest(binding) != recovery["binding_sha256"]:
                    self._block(conn, recovery, "source_changed")
                    continue
                document = conn.execute(
                    "SELECT owner_id,content_hash,manifest,mime_type,source_url FROM documents WHERE id=? AND owner_id=?",
                    (recovery["item_id"], recovery["owner_id"]),
                ).fetchone()
                job = conn.execute(
                    "SELECT phase,status,available_at FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (recovery["item_id"], recovery["owner_id"]),
                ).fetchone()
                if (
                    document is None
                    or job is None
                    or job["phase"] != "parse"
                    or job["status"] != "failed"
                    or document["owner_id"] != SYSTEM_OWNER_ID
                    or document["mime_type"] != "text/html"
                    or not document["source_url"]
                ):
                    continue
                try:
                    manifest = json.loads(document["manifest"])
                except (TypeError, ValueError):
                    continue
                plan = _legacy_html_parser_refresh_plan(manifest, document["content_hash"])
                if plan is None:
                    continue
                prior = conn.execute(
                    "SELECT detail FROM collection_recovery_events WHERE recovery_id=? AND event='html_parser_refresh_scheduled'",
                    (recovery["id"],),
                ).fetchall()
                matching_attempts = 0
                for event in prior:
                    try:
                        detail = json.loads(event["detail"])
                    except (TypeError, ValueError):
                        continue
                    if (
                        isinstance(detail, dict)
                        and detail.get("source_hash") == plan["source_hash"]
                        and detail.get("target_parser_version") == plan["target_parser_version"]
                    ):
                        matching_attempts += 1
                if matching_attempts >= MAX_HTML_PARSER_REFRESH_ATTEMPTS:
                    continue
                # A parser-version upgrade is a new, local parse operation. Do
                # not let exponential backoff accumulated by the old parser
                # postpone the first corrected parse or its bounded retries.
                available = time.time()
                attempt = matching_attempts + 1
                conn.execute(
                    "UPDATE processing_jobs SET available_at=? WHERE document_id=? AND owner_id=? AND status='failed'",
                    (available, recovery["item_id"], recovery["owner_id"]),
                )
                conn.execute(
                    """UPDATE collection_recovery_jobs SET status='waiting',reason='html_parser_refresh',
                    available_at=?,retry_plan=?,revision=revision+1,updated_at=? WHERE id=?""",
                    (available, dump(plan), now(), recovery["id"]),
                )
                self._event(
                    conn,
                    recovery["id"],
                    "html_parser_refresh_scheduled",
                    {**plan, "attempt": attempt, "max_attempts": MAX_HTML_PARSER_REFRESH_ATTEMPTS},
                )
                scheduled += 1
        return scheduled

    def _queue_pending_mineru_resumes(self, *, limit, worker_scope):
        """Requeue failed parses with intact, already-submitted MinerU batches."""
        scope, arguments = worker_scope.sql("j.owner_id")
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='parse' AND r.status='blocked'
                        AND r.reason='processing_review' AND j.phase='parse' AND j.status='failed'"""
                    + scope
                    + " ORDER BY r.updated_at,r.id LIMIT ?",
                    (*arguments, limit),
                )
            ]
        scheduled = 0
        for identifier in identifiers:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked' AND reason='processing_review'",
                    (identifier,),
                ).fetchone()
                if row is None or not worker_scope.allows(row["owner_id"]):
                    continue
                binding = self._binding(conn, row["owner_id"], "processing", row["item_id"], "parse")
                if binding is None or _digest(binding) != row["binding_sha256"]:
                    self._block(conn, row, "source_changed")
                    continue
                document = conn.execute(
                    "SELECT id,owner_id,content_hash,manifest FROM documents WHERE id=? AND owner_id=?",
                    (row["item_id"], row["owner_id"]),
                ).fetchone()
                job = conn.execute(
                    "SELECT phase,status FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (row["item_id"], row["owner_id"]),
                ).fetchone()
                if document is None or job is None or job["phase"] != "parse" or job["status"] != "failed":
                    continue
                try:
                    manifest = json.loads(document["manifest"])
                except (TypeError, ValueError):
                    self._block(conn, row, "invalid_checkpoint")
                    continue
                resumable = self._resumable_mineru_parse(conn, row["owner_id"], document, manifest)
                if resumable is None:
                    continue
                timestamp = time.time()
                available = max(timestamp, resumable["available_at"])
                conn.execute(
                    """UPDATE collection_recovery_jobs SET status='waiting',reason='mineru_parse_resume',
                    available_at=?,retry_plan='[]',revision=revision+1,updated_at=? WHERE id=?""",
                    (available, now(), identifier),
                )
                self._event(
                    conn,
                    identifier,
                    "existing_mineru_batch_resume_scheduled",
                    {
                        "source_hash": document["content_hash"],
                        "task_count": resumable["task_count"],
                        "remote_tasks_resubmitted": False,
                    },
                )
                scheduled += 1
        return scheduled

    def _queue_terminal_mineru_retries(self, *, limit, worker_scope):
        """Retry confirmed failed MinerU parts in a fresh generation after backoff."""
        from app.contexts.documents.infrastructure.providers.mineru import (
            ACTIVE,
            MinerUParser,
            parse_profile_hash,
        )

        scope, arguments = worker_scope.sql("j.owner_id")
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='parse' AND r.status='blocked'
                    AND r.reason='processing_review' AND j.phase='parse' AND j.status='failed'
                    AND EXISTS(
                        SELECT 1 FROM document_parse_tasks t
                        WHERE t.document_id=r.item_id AND t.owner_id=r.owner_id
                        AND t.state IN ('failed','blocked')
                    )"""
                    + scope
                    + " ORDER BY r.updated_at,r.id LIMIT ?",
                    (*arguments, limit),
                )
            ]

        scheduled = 0
        for identifier in identifiers:
            with self.db.transaction() as conn:
                recovery = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked' AND reason='processing_review'",
                    (identifier,),
                ).fetchone()
                if recovery is None or not worker_scope.allows(recovery["owner_id"]):
                    continue
                owner, document_id = recovery["owner_id"], recovery["item_id"]
                binding = self._binding(conn, owner, "processing", document_id, "parse")
                if binding is None or _digest(binding) != recovery["binding_sha256"]:
                    self._block(conn, recovery, "source_changed")
                    continue
                document = conn.execute(
                    "SELECT id,owner_id,filename,mime_type,content_hash,manifest FROM documents WHERE id=? AND owner_id=?",
                    (document_id, owner),
                ).fetchone()
                job = conn.execute(
                    "SELECT phase,status,available_at FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (document_id, owner),
                ).fetchone()
                if document is None or job is None or job["phase"] != "parse" or job["status"] != "failed":
                    continue
                try:
                    manifest = json.loads(document["manifest"])
                except (TypeError, ValueError):
                    continue
                remote = manifest.get("remote_parse") if isinstance(manifest, dict) else None
                page_count = remote.get("page_count") if isinstance(remote, dict) else None
                if (
                    not isinstance(remote, dict)
                    or remote.get("provider") != "mineru"
                    or remote.get("state") != "pending"
                    or remote.get("source_hash") != document["content_hash"]
                    or not isinstance(remote.get("profile"), dict)
                    or not isinstance(remote.get("profile_hash"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", remote["profile_hash"])
                    or (
                        page_count is not None
                        and (type(page_count) is not int or not 1 <= page_count <= 3000)
                    )
                ):
                    continue
                try:
                    old_hash = remote["profile_hash"]
                    if (
                        parse_profile_hash(remote["profile"], remote.get("retry_generation")) != old_hash
                        or parse_profile_hash(MinerUParser.profile(), remote.get("retry_generation"))
                        != old_hash
                    ):
                        continue
                except ValueError:
                    continue
                old_tasks = self._mineru_parse_tasks(
                    conn, owner, document_id, document["content_hash"], old_hash
                )
                if (
                    not old_tasks
                    or len(old_tasks) > (page_count if page_count is not None else 3000)
                    or [task["part_index"] for task in old_tasks] != list(range(len(old_tasks)))
                    or any(
                        task["state"] not in {"planned", *ACTIVE, "done", "failed", "blocked"}
                        for task in old_tasks
                    )
                    or not any(self._mineru_remote_terminal_failure(task) for task in old_tasks)
                ):
                    continue
                if any(
                    self._mineru_remote_terminal_failure(task)
                    and (
                        not task["error_code"]
                        or not isinstance(task["batch_id"], str)
                        or not re.fullmatch(r"[A-Za-z0-9-]{8,100}", task["batch_id"])
                    )
                    for task in old_tasks
                ):
                    continue

                scheduled += self._schedule_mineru_retry_generation(
                    conn,
                    recovery=recovery,
                    document=document,
                    job=job,
                    manifest=manifest,
                    old_hash=old_hash,
                    old_tasks=old_tasks,
                    retry_parts=[
                        task["part_index"] for task in old_tasks if self._mineru_remote_terminal_failure(task)
                    ],
                    reason="mineru_terminal_retry",
                    event="terminal_mineru_failure_retry_scheduled",
                )
        return scheduled

    def _queue_mineru_quality_retries(self, *, limit, worker_scope):
        """Reparse only complete MinerU parts containing replacement-decoded text."""
        from app.contexts.documents.infrastructure.providers.mineru import (
            MinerUParser,
            parse_profile_hash,
        )

        scope, arguments = worker_scope.sql("j.owner_id")
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='parse' AND r.status='blocked'
                    AND r.reason='quality_review' AND j.phase='parse' AND j.status='failed'"""
                    + scope
                    + " ORDER BY r.updated_at,r.id LIMIT ?",
                    (*arguments, limit),
                )
            ]

        scheduled = 0
        for identifier in identifiers:
            with self.db.transaction() as conn:
                recovery = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked' AND reason='quality_review'",
                    (identifier,),
                ).fetchone()
                if (
                    recovery is None
                    or not worker_scope.allows(recovery["owner_id"])
                    or recovery["recovery_count"] >= MAX_AUTOMATIC_MINERU_QUALITY_GENERATIONS
                ):
                    continue
                owner, document_id = recovery["owner_id"], recovery["item_id"]
                binding = self._binding(conn, owner, "processing", document_id, "parse")
                if binding is None or _digest(binding) != recovery["binding_sha256"]:
                    self._block(conn, recovery, "source_changed")
                    continue
                document = conn.execute(
                    "SELECT id,owner_id,filename,mime_type,content_hash,manifest FROM documents WHERE id=? AND owner_id=?",
                    (document_id, owner),
                ).fetchone()
                job = conn.execute(
                    "SELECT phase,status,available_at FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (document_id, owner),
                ).fetchone()
                if document is None or job is None or job["phase"] != "parse" or job["status"] != "failed":
                    continue
                try:
                    manifest = json.loads(document["manifest"])
                except (TypeError, ValueError):
                    continue
                targets = _mineru_decode_quality_targets(manifest, document["content_hash"])
                remote = manifest.get("remote_parse") if isinstance(manifest, dict) else None
                if targets is None or not isinstance(remote, dict):
                    continue
                old_hash = remote.get("profile_hash")
                profile = remote.get("profile")
                if not isinstance(old_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", old_hash):
                    continue
                try:
                    if (
                        not isinstance(profile, dict)
                        or parse_profile_hash(profile, remote.get("retry_generation")) != old_hash
                        or parse_profile_hash(MinerUParser.profile(), remote.get("retry_generation"))
                        != old_hash
                    ):
                        continue
                except ValueError:
                    continue
                old_tasks = self._mineru_parse_tasks(
                    conn, owner, document_id, document["content_hash"], old_hash
                )
                parts = remote.get("parts")
                if not isinstance(parts, list) or len(old_tasks) != len(parts) or not old_tasks:
                    continue
                consistent = True
                for index, (task, part) in enumerate(zip(old_tasks, parts, strict=True)):
                    try:
                        task_result = json.loads(task["result_blob"]) if task["result_blob"] else None
                    except (TypeError, ValueError):
                        task_result = None
                    if (
                        task["part_index"] != index
                        or task["state"] != "done"
                        or task["id"] != part.get("task_id")
                        or task["first_page"] != part.get("first_page")
                        or task["last_page"] != part.get("last_page")
                        or task["source_hash"] != document["content_hash"]
                        or not isinstance(task_result, dict)
                        or task_result.get("content_hash") != part.get("result_blob", {}).get("content_hash")
                    ):
                        consistent = False
                        break
                retry_pages, retry_parts = targets
                if not consistent or any(index >= len(old_tasks) for index in retry_parts):
                    continue
                scheduled += self._schedule_mineru_retry_generation(
                    conn,
                    recovery=recovery,
                    document=document,
                    job=job,
                    manifest=manifest,
                    old_hash=old_hash,
                    old_tasks=old_tasks,
                    retry_parts=retry_parts,
                    reason="mineru_quality_retry",
                    event="mineru_quality_retry_scheduled",
                    retry_pages=retry_pages,
                )
        return scheduled

    def _queue_automatic_ocr_retries(self, *, limit, worker_scope):
        """Promote eligible OCR review holds into the durable retry queue."""
        from app.contexts.documents.infrastructure.providers.native_asset_ocr import assets

        scope, arguments = worker_scope.sql("j.owner_id")
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='parse' AND r.status='blocked'
                        AND r.reason IN ('quality_review','processing_review','paid_result_unknown',
                            'ocr_failure_review','image_failure_review','ocr_retry_scope_review',
                            'ocr_retry_budget_review')
                        AND j.phase='parse' AND j.status='failed'"""
                    + scope
                    + " ORDER BY r.updated_at,r.id LIMIT ?",
                    (*arguments, limit),
                )
            ]
        scheduled = 0
        for identifier in identifiers:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked'",
                    (identifier,),
                ).fetchone()
                if row is None or not worker_scope.allows(row["owner_id"]):
                    continue
                binding = self._binding(conn, row["owner_id"], "processing", row["item_id"], "parse")
                if binding is None or _digest(binding) != row["binding_sha256"]:
                    self._block(conn, row, "source_changed")
                    continue
                document = conn.execute(
                    "SELECT manifest,content_hash FROM documents WHERE id=? AND owner_id=?",
                    (row["item_id"], row["owner_id"]),
                ).fetchone()
                job = conn.execute(
                    "SELECT phase,status,available_at FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (row["item_id"], row["owner_id"]),
                ).fetchone()
                if document is None or job is None or job["phase"] != "parse" or job["status"] != "failed":
                    continue
                try:
                    manifest = json.loads(document["manifest"])
                except (TypeError, ValueError):
                    self._block(conn, row, "invalid_checkpoint")
                    continue
                decision = classify_processing(manifest, phase="parse")
                if not decision.retryable or not (decision.pages or decision.asset_keys):
                    continue
                retry_plan = [
                    {"page": page["page"], "checkpoint_sha256": _digest(page)}
                    for page in manifest.get("pages", [])
                    if page.get("page") in decision.pages
                ]
                retry_plan.extend(
                    {"asset_key": asset["asset_key"], "checkpoint_sha256": _digest(asset)}
                    for asset in assets(manifest)
                    if asset["asset_key"] in decision.asset_keys
                )
                timestamp = time.time()
                available = max(timestamp, _timestamp(job["available_at"]), decision.not_before)
                conn.execute(
                    """UPDATE collection_recovery_jobs SET status='waiting',reason=?,available_at=?,
                    retry_plan=?,revision=revision+1,updated_at=? WHERE id=?""",
                    (decision.reason, available, dump(retry_plan), now(), identifier),
                )
                self._event(
                    conn,
                    identifier,
                    "automatic_ocr_retry_scheduled",
                    {
                        "reason": decision.reason,
                        "recovery_count": row["recovery_count"],
                        "physical_pages": list(decision.pages),
                        "assets": list(decision.asset_keys),
                        "retry_plan": retry_plan,
                    },
                )
                scheduled += 1
        return scheduled

    def _route_incomplete_embeddings_to_parse(self, *, limit, worker_scope):
        """Resume OCR first when a legacy embed retry is fenced by incomplete text."""
        scope, arguments = worker_scope.sql("j.owner_id")
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='embed' AND r.status='blocked'
                        AND r.reason='paid_result_unknown' AND j.phase='embed' AND j.status='failed'"""
                    + scope
                    + " ORDER BY r.updated_at,r.id LIMIT ?",
                    (*arguments, limit),
                )
            ]
        rerouted = 0
        for identifier in identifiers:
            with self.db.transaction() as conn:
                recovery = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked' AND reason='paid_result_unknown'",
                    (identifier,),
                ).fetchone()
                if recovery is None or not worker_scope.allows(recovery["owner_id"]):
                    continue
                owner, document_id = recovery["owner_id"], recovery["item_id"]
                embed_binding = self._binding(conn, owner, "processing", document_id, "embed")
                if embed_binding is None or _digest(embed_binding) != recovery["binding_sha256"]:
                    self._block(conn, recovery, "source_changed")
                    continue
                job = conn.execute(
                    "SELECT phase,status FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (document_id, owner),
                ).fetchone()
                document = conn.execute(
                    "SELECT content_hash,manifest FROM documents WHERE id=? AND owner_id=?",
                    (document_id, owner),
                ).fetchone()
                if (
                    job is None
                    or job["phase"] != "embed"
                    or job["status"] != "failed"
                    or document is None
                ):
                    continue
                try:
                    manifest = json.loads(document["manifest"])
                except (TypeError, ValueError):
                    self._block(conn, recovery, "invalid_checkpoint")
                    continue
                from app.contexts.documents.infrastructure.parsers.ingestion_quality import text_complete

                if text_complete(manifest):
                    continue
                if manifest.get("source_hash") != document["content_hash"]:
                    self._block(conn, recovery, "source_changed")
                    continue
                decision = classify_processing(manifest, phase="parse")
                if not decision.retryable or not (decision.pages or decision.asset_keys):
                    continue
                parse_binding = self._binding(conn, owner, "processing", document_id, "parse")
                if parse_binding is None:
                    self._block(conn, recovery, "source_changed")
                    continue
                previous_parse = conn.execute(
                    """SELECT * FROM collection_recovery_jobs WHERE owner_id=? AND queue_kind='processing'
                    AND item_id=? AND phase='parse'""",
                    (owner, document_id),
                ).fetchone()
                if previous_parse and previous_parse["binding_sha256"] != _digest(parse_binding):
                    self._block(conn, recovery, "source_changed")
                    continue
                conn.execute(
                    """UPDATE processing_jobs SET phase='parse',status='failed',attempts=0,available_at=0,
                    lease_token=NULL,lease_until=NULL,error=NULL,updated_at=?
                    WHERE document_id=? AND owner_id=? AND phase='embed' AND status='failed'""",
                    (now(), document_id, owner),
                )
                conn.execute(
                    "UPDATE documents SET status='processing',error=NULL WHERE id=? AND owner_id=?",
                    (document_id, owner),
                )
                conn.execute(
                    """UPDATE collection_recovery_jobs SET status='blocked',reason='ocr_precondition_retry',
                    available_at=0,revision=revision+1,updated_at=? WHERE id=?""",
                    (now(), identifier),
                )
                self._event(
                    conn,
                    identifier,
                    "embedding_retry_routed_to_ocr",
                    {
                        "source_hash": document["content_hash"],
                        "ocr_reason": decision.reason,
                        "physical_pages": list(decision.pages),
                        "assets": list(decision.asset_keys),
                        "requires_parse_before_index": True,
                        "old_remote_generation_preserved": True,
                    },
                )
                self.record_failure(
                    conn,
                    {
                        "document_id": document_id,
                        "owner_id": owner,
                        "phase": "parse",
                        "attempts": 0,
                    },
                    processing=True,
                    decision=decision,
                    exhausted=True,
                    error_type="embedding_waiting_for_ocr",
                )
                rerouted += 1
        return rerouted

    def _retry_unknown_ragflow_after_audit_error(
        self, conn, recovery, *, owner, document_id, fingerprint, wait, queue_scope, queue_arguments
    ):
        """Retry an uncertain paid write after backoff when receipt reads stay unavailable.

        The user has opted into automatic recovery even when the provider cannot
        confirm whether an earlier request was billed. Keep the retry bound to
        the same source generation and preserve the provider's Retry-After.
        """
        binding = self._binding(conn, owner, "processing", document_id, "embed")
        if binding is None or _digest(binding) != recovery["binding_sha256"]:
            self._block(conn, recovery, "source_changed")
            return "held"
        job = conn.execute(
            "SELECT phase,status FROM processing_jobs WHERE document_id=? AND owner_id=?",
            (document_id, owner),
        ).fetchone()
        bound = self.service.ragflow.binding(conn, document_id, fingerprint, include_chunks=False)
        if job is None or job["phase"] != "embed" or job["status"] != "failed" or bound is None:
            return "held"
        generation = bound[2]
        remote = conn.execute(
            """SELECT 1 FROM ragflow_documents WHERE generation=? AND document_id=? AND owner_id=?
            AND profile=? AND dataset_id IS NOT NULL AND remote_id IS NOT NULL""",
            (generation, document_id, owner, fingerprint),
        ).fetchone()
        intents = conn.execute(
            "SELECT count(*) FROM ragflow_chunks WHERE generation=? AND state='intent'",
            (generation,),
        ).fetchone()[0]
        if remote is None or not intents:
            return "held"
        queued = conn.execute(
            "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')" + queue_scope,
            queue_arguments,
        ).fetchone()[0]
        timestamp = time.time()
        if queued >= PROCESSING_QUEUE_LIMIT:
            conn.execute(
                "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                (timestamp + 60, now(), recovery["id"]),
            )
            self._event(
                conn,
                recovery["id"],
                "remote_receipt_audit_deferred",
                {"reason": "processing_queue_backpressure", "retry_after_seconds": 60},
            )
            return "deferred"
        available = timestamp + wait
        conn.execute(
            "UPDATE ragflow_chunks SET next_at=MAX(next_at,?) WHERE generation=? AND state='intent'",
            (available, generation),
        )
        conn.execute(
            """UPDATE processing_jobs SET status='queued',attempts=0,available_at=?,lease_token=NULL,
            lease_until=NULL,error=NULL,updated_at=? WHERE document_id=? AND owner_id=?
            AND phase='embed' AND status='failed'""",
            (available, now(), document_id, owner),
        )
        conn.execute(
            "UPDATE documents SET status='processing',error=NULL WHERE id=? AND owner_id=?",
            (document_id, owner),
        )
        conn.execute(
            """UPDATE collection_recovery_jobs SET status='queued',reason='unknown_result_auto_retry',
            available_at=?,recovery_count=recovery_count+1,revision=revision+1,updated_at=? WHERE id=?""",
            (available, now(), recovery["id"]),
        )
        self._event(
            conn,
            recovery["id"],
            "unknown_remote_result_auto_requeued",
            {
                "generation": generation,
                "intent_count": intents,
                "retry_after_seconds": wait,
                "duplicate_provider_charge_possible": True,
                "source_binding_verified": True,
            },
        )
        self.collection._refresh_document(conn, document_id)
        return "queued"

    def _reconcile_unknown_ragflow(self, *, limit, worker_scope):
        """Reconcile unknown writes and retry unmatched intents after provider backoff."""
        from app.contexts.documents.infrastructure.providers.ragflow import configured, profile

        result = {
            "reconciled": 0,
            "partially_reconciled": 0,
            "receipt_audit_errors": 0,
            "unmatched_requeued": 0,
            "unstarted_requeued": 0,
            "deferred": 0,
        }
        if not configured():
            return result
        fingerprint = profile()
        if not fingerprint:
            return result
        scope, arguments = worker_scope.sql("j.owner_id")
        queue_scope, queue_arguments = worker_scope.sql()
        timestamp = time.time()
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='embed' AND r.status='blocked'
                    AND j.phase='embed' AND j.status='failed'
                    AND r.reason='paid_result_unknown'
                    AND EXISTS(
                        SELECT 1 FROM ragflow_documents d
                        JOIN ragflow_chunks c ON c.generation=d.generation
                        WHERE d.document_id=r.item_id AND d.owner_id=r.owner_id AND d.profile=?
                        AND d.dataset_id IS NOT NULL AND d.remote_id IS NOT NULL
                    AND r.available_at<=? AND c.state='intent' AND c.intent_at<=? AND c.next_at<=?
                    )"""
                    + scope
                    + " ORDER BY r.available_at,r.updated_at,r.id LIMIT ?",
                    (fingerprint, timestamp, timestamp, timestamp, *arguments, limit),
                )
            ]
        for identifier in identifiers:
            with closing(self.db.connect()) as conn:
                recovery = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=?", (identifier,)
                ).fetchone()
                if recovery is None or not worker_scope.allows(recovery["owner_id"]):
                    continue
                owner, document_id = recovery["owner_id"], recovery["item_id"]
                revision, binding_hash = recovery["revision"], recovery["binding_sha256"]
            try:
                audit = self.service.ragflow.reconcile_pending_intents(document_id, owner)
            except Exception as error:
                code = getattr(error, "code", type(error).__name__)
                wait = min(
                    MAX_RECOVERY_SECONDS,
                    max(
                        BASE_RECOVERY_SECONDS * 2 ** min(recovery["failure_count"], 7),
                        getattr(error, "retry_after", None) or 0,
                    ),
                )
                recovery_result = "held"
                with self.db.transaction() as conn:
                    current = conn.execute(
                        """SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked'
                        AND reason='paid_result_unknown' AND revision=?""",
                        (identifier, revision),
                    ).fetchone()
                    if current is not None:
                        available = time.time() + wait
                        recovery_result = self._retry_unknown_ragflow_after_audit_error(
                            conn,
                            current,
                            owner=owner,
                            document_id=document_id,
                            fingerprint=fingerprint,
                            wait=wait,
                            queue_scope=queue_scope,
                            queue_arguments=queue_arguments,
                        )
                        if recovery_result == "held":
                            conn.execute(
                                "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                                (available, now(), identifier),
                            )
                            self._event(
                                conn,
                                identifier,
                                "remote_receipt_audit_deferred",
                                {"error_code": str(code)[:80], "retry_after_seconds": wait},
                            )
                result["receipt_audit_errors"] += 1
                if recovery_result == "queued":
                    result["unmatched_requeued"] += 1
                elif recovery_result == "deferred":
                    result["deferred"] += 1
                continue
            if audit is None:
                with self.db.transaction() as conn:
                    current = conn.execute(
                        """SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked'
                        AND reason='paid_result_unknown' AND revision=?""",
                        (identifier, revision),
                    ).fetchone()
                    if current is not None:
                        binding = self._binding(conn, owner, "processing", document_id, "embed")
                        if binding is None or _digest(binding) != binding_hash:
                            self._block(conn, current, "source_changed")
                        else:
                            wait = MAX_RECOVERY_SECONDS
                            conn.execute(
                                "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                                (time.time() + wait, now(), identifier),
                            )
                            self._event(
                                conn,
                                identifier,
                                "remote_receipt_audit_skipped",
                                {"reason": "current_generation_unavailable", "retry_after_seconds": wait},
                            )
                continue
            with self.db.transaction() as conn:
                current = conn.execute(
                    """SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked'
                    AND reason='paid_result_unknown' AND revision=?""",
                    (identifier, revision),
                ).fetchone()
                if current is None or not worker_scope.allows(owner):
                    continue
                binding = self._binding(conn, owner, "processing", document_id, "embed")
                if binding is None or _digest(binding) != binding_hash:
                    self._block(conn, current, "source_changed")
                    continue
                job = conn.execute(
                    "SELECT phase,status FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (document_id, owner),
                ).fetchone()
                current_bound = self.service.ragflow.binding(
                    conn, document_id, self.service.embedding_fingerprint(), include_chunks=False
                )
                if (
                    job is None
                    or job["phase"] != "embed"
                    or job["status"] != "failed"
                    or current_bound is None
                    or current_bound[2] != audit.get("generation")
                ):
                    self._block(conn, current, "source_changed")
                    continue
                remote = conn.execute(
                    "SELECT dataset_id,remote_id FROM ragflow_documents WHERE generation=? AND owner_id=? AND profile=?",
                    (audit["generation"], owner, fingerprint),
                ).fetchone()
                if (
                    remote is None
                    or remote["dataset_id"] != audit.get("dataset_id")
                    or remote["remote_id"] != audit.get("remote_id")
                ):
                    self._block(conn, current, "source_changed")
                    continue
                matched = 0
                for local_id, receipt in audit.get("matches", {}).items():
                    intent = conn.execute(
                        """SELECT content_hash,state FROM ragflow_chunks
                        WHERE generation=? AND local_id=?""",
                        (audit["generation"], local_id),
                    ).fetchone()
                    if (
                        intent is None
                        or intent["state"] != "intent"
                        or intent["content_hash"] != receipt.get("content_hash")
                    ):
                        continue
                    conn.execute(
                        """UPDATE ragflow_chunks SET remote_id=?,state='done',error_code=NULL,next_at=0
                        WHERE generation=? AND local_id=? AND state='intent' AND content_hash=?""",
                        (
                            receipt["remote_id"],
                            audit["generation"],
                            local_id,
                            receipt["content_hash"],
                        ),
                    )
                    matched += 1
                remaining = conn.execute(
                    "SELECT count(*) FROM ragflow_chunks WHERE generation=? AND state='intent'",
                    (audit["generation"],),
                ).fetchone()[0]
                detail = {
                    "source_hash": audit.get("source_hash"),
                    "generation": audit.get("generation"),
                    "remote_chunks_scanned": audit.get("remote_chunk_count", 0),
                    "remote_pages_scanned": audit.get("pages", 0),
                    "exact_receipts_reconciled": matched,
                    "unresolved_intents": remaining,
                }
                if remaining:
                    retryable_intents = conn.execute(
                        """SELECT count(*) FROM ragflow_chunks
                        WHERE generation=? AND state='intent' AND intent_at<=? AND next_at<=?""",
                        (audit["generation"], time.time(), time.time()),
                    ).fetchone()[0]
                    if retryable_intents:
                        if (
                            conn.execute(
                                "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')"
                                + queue_scope,
                                queue_arguments,
                            ).fetchone()[0]
                            >= 200
                        ):
                            wait = 60
                            conn.execute(
                                "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                                (time.time() + wait, now(), identifier),
                            )
                            self._event(
                                conn,
                                identifier,
                                "unmatched_remote_retry_deferred",
                                {**detail, "retry_after_seconds": wait},
                            )
                            result["deferred"] += 1
                            continue
                        conn.execute(
                            """UPDATE processing_jobs SET status='queued',attempts=0,available_at=0,
                            lease_token=NULL,lease_until=NULL,error=NULL,updated_at=?
                            WHERE document_id=? AND owner_id=? AND phase='embed' AND status='failed'""",
                            (now(), document_id, owner),
                        )
                        conn.execute(
                            "UPDATE documents SET status='processing',error=NULL WHERE id=? AND owner_id=?",
                            (document_id, owner),
                        )
                        conn.execute(
                            """UPDATE collection_recovery_jobs SET status='queued',reason='unmatched_intents_retry',
                            available_at=0,recovery_count=recovery_count+1,revision=revision+1,updated_at=? WHERE id=?""",
                            (now(), identifier),
                        )
                        self._event(
                            conn,
                            identifier,
                            "unmatched_remote_intents_auto_requeued",
                            {
                                **detail,
                                "retryable_intents": retryable_intents,
                                "absence_confirmed_after_seconds": 0,
                            },
                        )
                        self.collection._refresh_document(conn, document_id)
                        result["unmatched_requeued"] += 1
                        continue
                    next_retry = conn.execute(
                        """SELECT MIN(MAX(COALESCE(intent_at,?),COALESCE(next_at,?)))
                        FROM ragflow_chunks WHERE generation=? AND state='intent'""",
                        (timestamp, timestamp, audit["generation"]),
                    ).fetchone()[0]
                    available = max(timestamp + 60, next_retry or timestamp + BASE_RECOVERY_SECONDS)
                    wait = min(MAX_RECOVERY_SECONDS, available - timestamp)
                    conn.execute(
                        "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                        (timestamp + wait, now(), identifier),
                    )
                    self._event(
                        conn,
                        identifier,
                        "remote_receipts_reconciled",
                        {**detail, "retry_after_seconds": int(wait)},
                    )
                    result["partially_reconciled"] += int(matched > 0)
                    continue
                if (
                    conn.execute(
                        "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')"
                        + queue_scope,
                        queue_arguments,
                    ).fetchone()[0]
                    >= 200
                ):
                    wait = 60
                    conn.execute(
                        "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                        (time.time() + wait, now(), identifier),
                    )
                    self._event(
                        conn,
                        identifier,
                        "remote_receipts_confirmed_queue_deferred",
                        {**detail, "retry_after_seconds": wait},
                    )
                    continue
                conn.execute(
                    """UPDATE processing_jobs SET status='queued',attempts=0,available_at=0,
                    lease_token=NULL,lease_until=NULL,error=NULL,updated_at=?
                    WHERE document_id=? AND owner_id=? AND phase='embed' AND status='failed'""",
                    (now(), document_id, owner),
                )
                conn.execute(
                    "UPDATE documents SET status='processing',error=NULL WHERE id=? AND owner_id=?",
                    (document_id, owner),
                )
                conn.execute(
                    """UPDATE collection_recovery_jobs SET status='queued',reason='remote_receipts_reconciled',
                    available_at=0,recovery_count=recovery_count+1,revision=revision+1,updated_at=? WHERE id=?""",
                    (now(), identifier),
                )
                self._event(conn, identifier, "remote_receipts_reconciled_and_resumed", detail)
                self.collection._refresh_document(conn, document_id)
                result["reconciled"] += 1
        unstarted = self._requeue_unstarted_ragflow(
            limit=limit, worker_scope=worker_scope, fingerprint=fingerprint
        )
        result["unstarted_requeued"] += unstarted["requeued"]
        result["deferred"] += unstarted["deferred"]
        return result

    def _requeue_unstarted_ragflow(self, *, limit, worker_scope, fingerprint):
        """Resume failed indexes when the current generation has no chunk intent.

        Chunk requests are recorded as durable intents before they are sent, so
        any current-generation intent must go through receipt reconciliation.
        Remote documents from older generations do not prevent a new generation
        from resuming through RAGFlow's stable dataset/document lookup path.
        """
        result = {"requeued": 0, "deferred": 0}
        scope, arguments = worker_scope.sql("j.owner_id")
        queue_scope, queue_arguments = worker_scope.sql()
        timestamp = time.time()
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    """SELECT r.id FROM collection_recovery_jobs r
                    JOIN processing_jobs j ON j.document_id=r.item_id AND j.owner_id=r.owner_id
                    WHERE r.queue_kind='processing' AND r.phase='embed' AND r.status='blocked'
                    AND r.reason='paid_result_unknown' AND r.available_at<=?
                    AND j.phase='embed' AND j.status='failed'
                    """
                    + scope
                    + " ORDER BY r.available_at,r.updated_at,r.id LIMIT ?",
                    (timestamp, *arguments, min(128, limit * 4)),
                )
            ]
        for identifier in identifiers:
            with self.db.transaction() as conn:
                row = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='blocked'",
                    (identifier,),
                ).fetchone()
                if row is None or not worker_scope.allows(row["owner_id"]):
                    continue
                binding = self._binding(conn, row["owner_id"], "processing", row["item_id"], "embed")
                if binding is None or _digest(binding) != row["binding_sha256"]:
                    self._block(conn, row, "source_changed")
                    continue
                job = conn.execute(
                    "SELECT phase,status FROM processing_jobs WHERE document_id=? AND owner_id=?",
                    (row["item_id"], row["owner_id"]),
                ).fetchone()
                current = self.service.ragflow.binding(
                    conn, row["item_id"], fingerprint, include_chunks=False
                )
                has_current_intent = (
                    conn.execute(
                        "SELECT 1 FROM ragflow_chunks WHERE generation=? AND state='intent' LIMIT 1",
                        (current[2],),
                    ).fetchone()
                    if current is not None
                    else None
                )
                if (
                    job is None
                    or job["phase"] != "embed"
                    or job["status"] != "failed"
                    or current is None
                    or current[2] is None
                    or has_current_intent is not None
                ):
                    continue
                if (
                    conn.execute(
                        "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')"
                        + queue_scope,
                        queue_arguments,
                    ).fetchone()[0]
                    >= 200
                ):
                    available = timestamp + 60
                    conn.execute(
                        "UPDATE collection_recovery_jobs SET available_at=?,revision=revision+1,updated_at=? WHERE id=?",
                        (available, now(), identifier),
                    )
                    self._event(
                        conn,
                        identifier,
                        "unstarted_ragflow_retry_deferred",
                        {"retry_after_seconds": 60},
                    )
                    result["deferred"] += 1
                    continue
                conn.execute(
                    """UPDATE processing_jobs SET status='queued',attempts=0,available_at=?,
                    lease_token=NULL,lease_until=NULL,error=NULL,updated_at=?
                    WHERE document_id=? AND owner_id=? AND phase='embed' AND status='failed'""",
                    (timestamp, now(), row["item_id"], row["owner_id"]),
                )
                conn.execute(
                    "UPDATE documents SET status='processing',error=NULL WHERE id=? AND owner_id=?",
                    (row["item_id"], row["owner_id"]),
                )
                conn.execute(
                    """UPDATE collection_recovery_jobs SET status='queued',reason='unstarted_ragflow_retry',
                    available_at=?,recovery_count=recovery_count+1,revision=revision+1,updated_at=? WHERE id=?""",
                    (timestamp, now(), identifier),
                )
                self._event(
                    conn,
                    identifier,
                    "unstarted_ragflow_retry_queued",
                    {
                        "source_hash": current[0]["content_hash"],
                        "generation": current[2],
                        "current_generation_chunk_intent": False,
                        "previous_generation_remote_document_ignored": True,
                        "duplicate_provider_charge_possible": True,
                    },
                )
                self.collection._refresh_document(conn, row["item_id"])
                result["requeued"] += 1
        return result

    def _queue_completed_incomplete_ragflow(self, *, limit, worker_scope, fingerprint):
        """Resume current RAGFlow generations abandoned after local processing completed."""
        scope, arguments = worker_scope.sql("j.owner_id")
        queue_scope, queue_arguments = worker_scope.sql()
        with closing(self.db.connect()) as conn:
            identifiers = [
                row["id"]
                for row in conn.execute(
                    """SELECT d.id FROM documents d
                    JOIN processing_jobs j ON j.document_id=d.id AND j.owner_id=d.owner_id
                    JOIN retrieval_documents x ON x.document_id=d.id
                    WHERE d.role='primary' AND x.complete=1 AND x.chunk_count>0
                    AND j.phase='embed' AND j.status='completed'
                    AND (
                        NOT EXISTS(SELECT 1 FROM legal_classifications c WHERE c.document_id=d.id)
                        OR EXISTS(
                            SELECT 1 FROM legal_classifications c
                            WHERE c.document_id=d.id AND c.level='primary'
                        )
                    )
                    AND NOT EXISTS(
                        SELECT 1 FROM ragflow_documents r WHERE r.document_id=d.id
                        AND r.owner_id=d.owner_id AND r.profile=? AND r.source_hash=d.content_hash
                        AND r.source_version=d.version AND r.index_revision=x.revision AND r.state='ready'
                    )"""
                    + scope
                    + " ORDER BY j.updated_at,d.id LIMIT ?",
                    (fingerprint, *arguments, min(128, limit)),
                )
            ]
        resumed = 0
        for document_id in identifiers:
            with self.db.transaction() as conn:
                job = conn.execute(
                    "SELECT owner_id,phase,status FROM processing_jobs WHERE document_id=?",
                    (document_id,),
                ).fetchone()
                if (
                    job is None
                    or job["phase"] != "embed"
                    or job["status"] != "completed"
                    or not worker_scope.allows(job["owner_id"])
                ):
                    continue
                document = conn.execute(
                    "SELECT manifest FROM documents WHERE id=? AND owner_id=?",
                    (document_id, job["owner_id"]),
                ).fetchone()
                if document is None:
                    continue
                try:
                    manifest = json.loads(document["manifest"])
                except (TypeError, ValueError):
                    continue
                from app.contexts.documents.infrastructure.parsers.ingestion_quality import text_complete

                if (
                    not text_complete(manifest)
                    or not self.service.archive.can_index(conn, document_id)
                    or not self.service.ragflow.pending(conn, document_id, fingerprint, limit=1)
                ):
                    continue
                binding = self._binding(conn, job["owner_id"], "processing", document_id, "embed")
                if binding is None:
                    continue
                if (
                    conn.execute(
                        "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')"
                        + queue_scope,
                        queue_arguments,
                    ).fetchone()[0]
                    >= PROCESSING_QUEUE_LIMIT
                ):
                    break
                available = time.time() + self.service.ragflow.retry_delay(
                    conn, document_id, fingerprint
                )
                conn.execute(
                    """UPDATE processing_jobs SET status='queued',attempts=0,available_at=?,
                    lease_token=NULL,lease_until=NULL,error=NULL,updated_at=?
                    WHERE document_id=? AND owner_id=? AND phase='embed' AND status='completed'""",
                    (available, now(), document_id, job["owner_id"]),
                )
                conn.execute(
                    "UPDATE documents SET status='processing',error=NULL WHERE id=? AND owner_id=?",
                    (document_id, job["owner_id"]),
                )
                recovery = conn.execute(
                    """SELECT * FROM collection_recovery_jobs WHERE owner_id=? AND queue_kind='processing'
                    AND item_id=? AND phase='embed'""",
                    (job["owner_id"], document_id),
                ).fetchone()
                timestamp = now()
                binding_json = dump(binding)
                binding_hash = _digest(binding)
                if recovery is None:
                    recovery_id = uid()
                    conn.execute(
                        """INSERT INTO collection_recovery_jobs(
                        id,owner_id,queue_kind,item_id,phase,status,reason,available_at,binding,
                        binding_sha256,created_at,updated_at
                        ) VALUES(?,?,'processing',?,'embed','queued','ragflow_index_resume',?,?,?,?,?)""",
                        (
                            recovery_id,
                            job["owner_id"],
                            document_id,
                            available,
                            binding_json,
                            binding_hash,
                            timestamp,
                            timestamp,
                        ),
                    )
                else:
                    recovery_id = recovery["id"]
                    conn.execute(
                        """UPDATE collection_recovery_jobs SET status='queued',reason='ragflow_index_resume',
                        available_at=?,binding=?,binding_sha256=?,retry_plan='[]',revision=revision+1,updated_at=?
                        WHERE id=?""",
                        (available, binding_json, binding_hash, timestamp, recovery_id),
                    )
                self._event(
                    conn,
                    recovery_id,
                    "incomplete_ragflow_index_auto_requeued",
                    {
                        "profile": fingerprint,
                        "retry_after_seconds": max(0, int(available - time.time())),
                        "duplicate_provider_charge_possible": True,
                        "source_binding_verified": True,
                    },
                )
                self.collection._refresh_document(conn, document_id)
                resumed += 1
        return resumed

    def run_due(self, *, limit=10, worker_scope=BackgroundWorkerScope()):
        if type(limit) is not int or not 1 <= limit <= MAX_BATCH:
            raise ValueError("recovery batch must be between 1 and 32")
        result = {
            "adopted": 0,
            "chart_refreshes": 0,
            "reconciled": 0,
            "partially_reconciled": 0,
            "receipt_audit_errors": 0,
            "requeued": 0,
            "blocked": 0,
            "deferred": 0,
        }
        scope, arguments = worker_scope.sql()
        # One target per write transaction: OCR checkpoint validation must not
        # hold a write lock across an entire scheduled batch of large documents.
        for _ in range(limit):
            with self.db.transaction() as conn:
                adopted = self.adopt_failed(conn, limit=1, worker_scope=worker_scope)
            result["adopted"] += adopted
            if not adopted:
                break
        result["chart_refreshes"] = self._queue_legacy_chart_refreshes(limit=limit, worker_scope=worker_scope)
        self._queue_legacy_html_parser_refreshes(limit=limit, worker_scope=worker_scope)
        self._queue_pending_mineru_resumes(limit=limit, worker_scope=worker_scope)
        self._queue_terminal_mineru_retries(limit=limit, worker_scope=worker_scope)
        self._queue_mineru_quality_retries(limit=limit, worker_scope=worker_scope)
        self._queue_automatic_ocr_retries(limit=limit, worker_scope=worker_scope)
        result["requeued"] += self._route_incomplete_embeddings_to_parse(
            limit=limit, worker_scope=worker_scope
        )
        for key, value in self._reconcile_unknown_ragflow(limit=limit, worker_scope=worker_scope).items():
            result["requeued" if key in {"unmatched_requeued", "unstarted_requeued"} else key] += value
        from app.contexts.documents.infrastructure.providers.ragflow import configured, profile

        if configured():
            fingerprint = profile()
            if fingerprint:
                result["requeued"] += self._queue_completed_incomplete_ragflow(
                    limit=limit, worker_scope=worker_scope, fingerprint=fingerprint
                )
        with closing(self.db.connect()) as conn:
            identifiers = [
                row[0]
                for row in conn.execute(
                    "SELECT id FROM collection_recovery_jobs WHERE status='waiting' AND available_at<=?"
                    + scope
                    + " ORDER BY available_at,id LIMIT ?",
                    (time.time(), *arguments, limit),
                )
            ]
        for identifier in identifiers:
            with self.db.transaction() as conn:
                timestamp = time.time()
                row = conn.execute(
                    "SELECT * FROM collection_recovery_jobs WHERE id=? AND status='waiting' AND available_at<=?"
                    + scope,
                    (identifier, timestamp, *arguments),
                ).fetchone()
                if row:
                    result[self._resume(conn, row, timestamp, worker_scope=worker_scope)] += 1
        return result
