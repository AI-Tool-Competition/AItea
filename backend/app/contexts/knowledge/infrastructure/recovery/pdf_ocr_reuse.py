"""Reuse a verified full-page reading within one owner and immutable PDF version.

This is derived OCR provenance, never a legal relation or approval. Missing
provider identity, prior target attempts and recovery evidence fail closed.
Candidate lookup uses the existing owner/source URL/hash index, not a scan of
all manifests. Different source URLs conservatively keep the normal OCR path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
from contextlib import closing
from copy import deepcopy

from app.contexts.knowledge.infrastructure.parsing.ingestion_quality import pdf_page_status, validate_ocr_page
from app.contexts.knowledge.infrastructure.parsing.pdf_limits import ARCHIVE_MAX_PAGES

PROTOCOL = "same-owner-pdf-page-reuse-v1"
MAX_DONORS = 8
MAX_PAGE_BYTES = 2 * 1024 * 1024
MAX_MANIFEST_BYTES = 40 * 1024 * 1024
_RENDER_KEYS = (
    "source_hash",
    "page",
    "image_sha256",
    "mime_type",
    "width",
    "height",
    "dpi",
    "renderer",
    "renderer_version",
    "pdfium_version",
    "render_version",
)


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json_hash(value) -> str:
    return _hash(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())


def current_profile() -> dict | None:
    """Record the actual page-provider recipe, without API keys or inferred legacy identity."""
    import os

    from app.contexts.knowledge.infrastructure.providers import vision as vision

    if not vision.vision_ocr_configured():
        return None
    value = {
        "version": 1,
        "base_url": os.environ["LLM_BASE_URL"].strip().rstrip("/"),
        "requested_model": os.environ["LLM_MODEL"].strip(),
        "prompt_version": vision.PROMPT_VERSION,
        "prompt_sha256": _hash(vision._PROMPT.encode()),
        "request_recipe": vision.PAGE_REQUEST_RECIPE,
    }
    return value | {"fingerprint": _json_hash(value)}


def native_hint(page: dict) -> tuple[str, str]:
    """Hidden/unverified source text remains evidence, but must not steer the model."""
    if (page.get("native_visibility") or {}).get("requires_visual_review") is True:
        return "", "masked-unverified-native-hint-v1"
    return page["native_text"][:20_000], "native-hint-20000-v1"


def pristine_pending(page: dict) -> bool:
    """Only a never-attempted page can acquire another document's reading."""
    metadata = page.get("ocr") if page.get("ocr") is not None else {}
    return (
        page.get("status") == "pending_ocr"
        and pdf_page_status(page) == "pending_ocr"
        and isinstance(page.get("native_text"), str)
        and isinstance(page.get("reasons"), list)
        and isinstance(metadata, dict)
        and set(metadata).issubset({"attempts"})
        and type(metadata.get("attempts", 0)) is int
        and metadata.get("attempts", 0) == 0
        and not page.get("error")
        and not any("history" in key or "recovery" in key for key in page)
    )


def _successful(page: dict, target_page: dict, profile: dict) -> dict | None:
    metadata = page.get("ocr")
    hint, hint_strategy = native_hint(target_page)
    if (
        not isinstance(metadata, dict)
        or page.get("status") != "ocr_complete"
        or pdf_page_status(page) != "ocr_complete"
        or metadata.get("provider_profile") != profile
        or metadata.get("complete") is not True
        or metadata.get("finish_reason") != "stop"
        or metadata.get("prompt_version") != profile["prompt_version"]
        or metadata.get("requested_model") != profile["requested_model"]
        or type(metadata.get("attempts")) is not int
        or not 1 <= metadata["attempts"] <= 3
        or metadata.get("uncertain_spans")
        or metadata.get("validation_warnings")
        or page.get("error")
        or any("recovery" in key or "history" in key or key == "reuse" for key in metadata)
        or page.get("source_hash") != target_page.get("source_hash")
        or page.get("page") != target_page.get("page")
        or page.get("native_text") != target_page.get("native_text")
        or page.get("reasons") != target_page.get("reasons")
        or page.get("native_visibility") != target_page.get("native_visibility")
        or not isinstance(page.get("text"), str)
        or metadata.get("text") != page["text"]
        or metadata.get("native_hint_hash") != _hash(hint.encode())
        or metadata.get("native_hint_chars") != len(hint)
        or metadata.get("native_hint_strategy") != hint_strategy
    ):
        return None
    checked = deepcopy(page)
    validate_ocr_page(checked)
    if checked["status"] != "ocr_complete" or checked["ocr"]["uncertain_spans"]:
        return None
    return checked


def _image_matches(service, owner: str, metadata: dict, rendered: dict) -> bool:
    from app.contexts.knowledge.infrastructure.providers.html_linked_asset_ocr import read_image_blob

    try:
        if (
            any(metadata.get(key) != rendered.get(key) for key in _RENDER_KEYS)
            or rendered.get("render_version") != "bounded-page-png-v1"
            or rendered.get("mime_type") != "image/png"
            or any(type(rendered.get(key)) is not int for key in ("width", "height"))
            or not all(0 < rendered[key] <= 4000 for key in ("width", "height"))
            or not 0 < rendered["width"] * rendered["height"] <= 6_000_000
            or not math.isfinite(rendered["dpi"])
            or not 0 < rendered["dpi"] <= 200
        ):
            return False
        image = base64.b64decode(rendered["image_base64"], validate=True)
        if not 0 < len(image) <= 8 * 1024 * 1024 or _hash(image) != rendered["image_sha256"]:
            return False
        reference = metadata["image_blob"]
        return (
            isinstance(reference, dict)
            and reference.get("mime_type") == "image/png"
            and reference.get("size") == len(image)
            and reference.get("content_hash") == rendered["image_sha256"]
            and read_image_blob(service.db.data_dir, owner, reference) == image
        )
    except (ValueError, OSError, KeyError, TypeError):
        return False


def _page_sql():
    return "CASE WHEN length(CAST(manifest AS BLOB))<=? AND json_valid(manifest) THEN manifest ELSE '{}' END"


def find_verified_reuse(service, target: dict, page: dict, rendered: dict) -> dict | None:
    """Validate at most eight indexed donors against this actual bounded page render."""
    target = dict(target)
    profile = current_profile()
    number = page.get("page")
    if (
        profile is None
        or target.get("role") != "primary"
        or not pristine_pending(page)
        or type(number) is not int
        or not 1 <= number <= ARCHIVE_MAX_PAGES
        or page.get("source_hash") != target.get("content_hash")
        or rendered.get("source_hash") != target.get("content_hash")
        or rendered.get("page") != number
    ):
        return None
    pointer = f"$.pages[{number - 1}]"
    with closing(service.db.connect()) as conn:
        rows = conn.execute(
            f"""SELECT id,owner_id,version,content_hash,size,storage_path,
            json_extract({_page_sql()},?) page_payload,
            json_extract({_page_sql()},'$.source_hash') manifest_hash
            FROM documents INDEXED BY documents_source
            WHERE owner_id=? AND source_url IS ? AND content_hash=? AND version=?
            AND role='primary' AND id!=? ORDER BY rowid LIMIT ?""",
            (
                MAX_MANIFEST_BYTES,
                pointer,
                MAX_MANIFEST_BYTES,
                target["owner_id"],
                target.get("source_url"),
                target["content_hash"],
                target["version"],
                target["id"],
                MAX_DONORS,
            ),
        ).fetchall()
    for row in rows:
        try:
            raw = row["page_payload"]
            if (
                not isinstance(raw, str)
                or len(raw.encode()) > MAX_PAGE_BYTES
                or row["manifest_hash"] != target["content_hash"]
                or row["size"] != target["size"]
                or row["storage_path"] != target["storage_path"]
            ):
                continue
            donor = json.loads(raw)
            checked = _successful(donor, page, profile)
            if checked is None or not _image_matches(service, target["owner_id"], checked["ocr"], rendered):
                continue
            proof = {
                "protocol": PROTOCOL,
                "source_document_id": row["id"],
                "source_document_version": row["version"],
                "source_content_hash": row["content_hash"],
                "source_page": number,
                "source_page_checkpoint_sha256": _json_hash(donor),
                "target_document_id": target["id"],
                "target_document_version": target["version"],
                "profile_fingerprint": profile["fingerprint"],
                "png_content_hash": rendered["image_sha256"],
                "png_size": checked["ocr"]["image_blob"]["size"],
                "native_text_sha256": _hash(page["native_text"].encode()),
                "reasons_sha256": _json_hash(page["reasons"]),
                "text_sha256": _hash(checked["text"].encode()),
                "origin_attempts": checked["ocr"]["attempts"],
                "target_model_calls": 0,
                "local_bounded_render_verified": True,
            }
            result = deepcopy(page)
            result.update(
                text=checked["text"], printed_page=checked.get("printed_page"), status="ocr_complete"
            )
            result["ocr"] = deepcopy(checked["ocr"])
            result["ocr"].update(attempts=0, reuse=proof)
            validate_ocr_page(result)
            if result["status"] != "ocr_complete":
                continue
            return {"page": result, "target_page_sha256": _json_hash(page), "profile": profile}
        except (ValueError, KeyError, TypeError):
            continue
    return None


def candidate_is_current(conn, target: dict, candidate: dict) -> bool:
    """Recheck both exact page snapshots in the caller's lease-bound write transaction."""
    if current_profile() != candidate["profile"]:
        return False
    proof = candidate["page"]["ocr"]["reuse"]
    pointer = f"$.pages[{proof['source_page'] - 1}]"
    for identifier, expected in (
        (target["id"], candidate["target_page_sha256"]),
        (proof["source_document_id"], proof["source_page_checkpoint_sha256"]),
    ):
        row = conn.execute(
            f"""SELECT owner_id,role,version,content_hash,size,storage_path,source_url,
            json_extract({_page_sql()},?) page_payload FROM documents WHERE id=?""",
            (MAX_MANIFEST_BYTES, pointer, identifier),
        ).fetchone()
        if row is None or any(
            row[key] != target[key]
            for key in ("owner_id", "role", "version", "content_hash", "size", "storage_path", "source_url")
        ):
            return False
        try:
            raw = row["page_payload"]
            if not isinstance(raw, str) or len(raw.encode()) > MAX_PAGE_BYTES:
                return False
            if _json_hash(json.loads(raw)) != expected:
                return False
        except (ValueError, TypeError):
            return False
    return True
