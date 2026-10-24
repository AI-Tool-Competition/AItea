"""Explicit page-scope prompt migration; no database writes or model calls.

The authorized caller persists the returned checkpoint in its transaction and
queues normal bounded OCR. Old incomplete readings never become accepted text
without a new provider response and the existing numeric/structural checks.
"""

import hashlib
import json
import re
from copy import deepcopy

from app.contexts.documents.infrastructure.parsers.ingestion_quality import pdf_page_status

PROTOCOL = "physical-page-completeness-retry-v1"
MAX_RETRY_PAGES = 20
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 40 * 1024 * 1024


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()


def prepare_physical_page_retry(manifest: dict, page_numbers: list[int], *, source_hash: str) -> dict:
    """Prepare selected v2 incomplete pages once; preserve all old page evidence.

    This narrowly targets the v2 physical-page ambiguity, not every failed OCR
    result. No implicit migration or blanket clearing of uncertainty is allowed.
    """
    from app.contexts.documents.infrastructure.parsers.native_parser import pdf_quality
    from app.contexts.documents.infrastructure.providers.vision import PROMPT_VERSION

    if (
        not isinstance(source_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
        or not isinstance(manifest, dict)
        or manifest.get("source_hash") != source_hash
    ):
        raise ValueError("page_retry_source_hash_mismatch")
    if (
        not isinstance(page_numbers, list)
        or not 1 <= len(page_numbers) <= MAX_RETRY_PAGES
        or any(type(number) is not int or number < 1 for number in page_numbers)
        or len(set(page_numbers)) != len(page_numbers)
    ):
        raise ValueError("page_retry_requires_1_to_20_unique_physical_pages")
    pages = manifest.get("pages")
    if (
        not isinstance(pages, list)
        or not pages
        or any(not isinstance(page, dict) for page in pages)
        or [page.get("page") for page in pages] != list(range(1, len(pages) + 1))
        or any(page.get("source_hash") != source_hash for page in pages)
        or max(page_numbers) > len(pages)
    ):
        raise ValueError("page_retry_physical_mapping_invalid")
    if PROMPT_VERSION != "legal-page-transcription-v3":
        raise ValueError("page_retry_strategy_prompt_changed")
    if len(_encoded(manifest)) > MAX_MANIFEST_BYTES:
        raise ValueError("page_retry_manifest_too_large")
    for number in page_numbers:
        page = pages[number - 1]
        metadata = page.get("ocr")
        if (
            not isinstance(metadata, dict)
            or pdf_page_status(page) != "ocr_uncertain"
            or metadata.get("prompt_version") != "legal-page-transcription-v2"
            or metadata.get("complete") is not False
            or metadata.get("source_hash") != source_hash
            or metadata.get("page") != number
            or "model_reported_incomplete" not in metadata.get("uncertain_spans", [])
        ):
            raise ValueError("page_retry_requires_old_incomplete_transcription")
        # A single immutable previous page is enough. Nested retry histories
        # would grow with every migration and could enable a paid retry loop.
        if "ocr_retry_history" in page:
            raise ValueError("page_retry_already_prepared")
        if len(_encoded(page)) > MAX_PAGE_BYTES:
            raise ValueError("page_retry_checkpoint_too_large")
    updated = deepcopy(manifest)
    for number in page_numbers:
        page = updated["pages"][number - 1]
        previous = deepcopy(page)
        page["ocr_retry_history"] = [
            {
                "protocol": PROTOCOL,
                "source_hash": source_hash,
                "page": number,
                "target_prompt_version": PROMPT_VERSION,
                "previous_page_sha256": hashlib.sha256(_encoded(previous)).hexdigest(),
                "previous_page": previous,
            }
        ]
        page.update(status="pending_ocr", ocr={"attempts": 0})
        page.pop("error", None)
        page.pop("deferred_reason", None)
    updated["quality"] = pdf_quality(updated["pages"])
    updated.pop("embedding_fingerprint", None)
    updated.pop("embedding_reuse", None)
    for chunk in updated.get("chunks", []):
        chunk.pop("embedding", None)
        chunk.pop("embedding_input_hash", None)
    if len(_encoded(updated)) > MAX_MANIFEST_BYTES:
        raise ValueError("page_retry_manifest_too_large")
    return updated
