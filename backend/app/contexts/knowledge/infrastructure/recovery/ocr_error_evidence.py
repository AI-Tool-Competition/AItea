"""Persist bounded failed OCR responses against a previously saved request."""

import base64
import hashlib
import json
from copy import deepcopy

from app.contexts.knowledge.infrastructure.providers.vision import MAX_ERROR_RESPONSE_BYTES
from app.shared.domain.values import dump


class PageAttemptHistoryLimit(ValueError):
    """A page reached its bounded evidence-history limit before another request."""


def request_outcome_unknown(metadata):
    """Inspect current requests only; prior resolved history is not a new hold."""
    if not isinstance(metadata, dict):
        return True
    outcomes = ("unknown_transport_result", "unknown_after_interruption")
    recovery = metadata.get("recovery")
    regions = recovery.get("regions", []) if isinstance(recovery, dict) else []
    if not isinstance(regions, list) or any(not isinstance(region, dict) for region in regions):
        return True
    for record in (metadata, *regions):
        envelope = record.get("error_response")
        if record.get("request_outcome") in outcomes or (
            isinstance(envelope, dict) and envelope.get("request_outcome") in outcomes
        ):
            return True
    return False


def preserve_previous_asset_attempt(asset):
    """Retain failed native-image evidence before an explicit retry resets it."""
    metadata = asset.get("ocr") or {}
    if not (metadata.get("attempts") or metadata.get("request_id")):
        return
    history = asset.get("ocr_attempt_history", [])
    if not isinstance(history, list) or len(history) >= 3:
        raise ValueError("原生图片识别尝试历史已达到上限或格式无效")
    previous = {key: deepcopy(value) for key, value in asset.items() if key != "ocr_attempt_history"}
    raw = json.dumps(
        previous, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("原生图片识别尝试检查点超过存储限额")
    asset["ocr_attempt_history"] = [
        *deepcopy(history),
        {
            "protocol": "ocr-asset-attempt-history-v1",
            "source_hash": asset["source_hash"],
            "asset_key": asset["asset_key"],
            "input_identity": asset["input_identity"],
            "previous_asset_sha256": hashlib.sha256(raw).hexdigest(),
            "previous_asset": previous,
        },
    ]


def preserve_previous_page_attempt(page):
    """Append a bounded prior checkpoint before replacing request metadata."""
    metadata = page.get("ocr") or {}
    if not metadata.get("attempts"):
        return
    history = page.get("ocr_attempt_history", [])
    if not isinstance(history, list) or len(history) > 3:
        raise ValueError("识别尝试历史格式无效")
    previous = {key: deepcopy(value) for key, value in page.items() if key != "ocr_attempt_history"}
    raw = json.dumps(
        previous, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError("识别尝试检查点超过存储限额")
    digest = hashlib.sha256(raw).hexdigest()
    if history and history[-1].get("previous_page_sha256") == digest:
        return
    if len(history) >= 3:
        raise PageAttemptHistoryLimit("识别尝试历史已达到上限")
    page["ocr_attempt_history"] = [
        *deepcopy(history),
        {
            "protocol": "ocr-page-attempt-history-v1",
            "source_hash": page["source_hash"],
            "page": page["page"],
            "previous_page_sha256": digest,
            "previous_page": previous,
        },
    ]


def validate_response_binding(evidence, source_hash, request_page):
    if (
        not isinstance(evidence, dict)
        or evidence.get("format") != "ocr-error-response-v1"
        or evidence.get("source_hash") != source_hash
        or type(evidence.get("page")) is not int
        or not isinstance(request_page, dict)
        or request_page.get("page") != evidence["page"]
        or request_page.get("source_hash") != source_hash
    ):
        raise ValueError("识别错误响应与原件或物理页不符")
    request = request_page.get("ocr", {})
    if evidence.get("request_kind") == "region":
        regions = request.get("recovery", {}).get("regions", []) if isinstance(request, dict) else []
        matches = [
            r for r in regions if isinstance(r, dict) and r.get("region_id") == evidence.get("region_id")
        ]
        if (
            not evidence.get("region_id")
            or len(matches) != 1
            or type(evidence.get("pass_number")) is not int
            or evidence["pass_number"] not in (0, 1)
            or any(
                matches[0].get(key) != evidence.get(key)
                for key in ("source_hash", "page", "region_bbox", "pass_number")
            )
        ):
            raise ValueError("局部识别错误响应与已保存区域不符")
        request = matches[0]
    elif evidence.get("request_kind") not in (None, "page"):
        raise ValueError("识别错误响应请求类型无效")
    if (
        not isinstance(request, dict)
        or not evidence.get("request_id")
        or type(evidence.get("attempt")) is not int
        or any(
            request.get(key) != evidence.get(source_key)
            for key, source_key in (
                ("request_id", "request_id"),
                ("attempts", "attempt"),
                ("image_sha256", "image_sha256"),
                ("prompt_version", "prompt_version"),
                ("requested_model", "requested_model"),
            )
        )
    ):
        raise ValueError("识别错误响应与已保存请求不一致")
    return request


def preserve_previous_region_attempt(record):
    """Keep prior attempts on the same crop without nesting its own history."""
    if not (record.get("attempts") or record.get("error") or record.get("request_id")):
        return
    history = record.get("attempt_history", [])
    if not isinstance(history, list) or len(history) > 3:
        raise ValueError("局部识别尝试历史格式无效")
    previous = {key: deepcopy(value) for key, value in record.items() if key != "attempt_history"}
    raw = json.dumps(
        previous, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    if len(raw) > 2 * 1024 * 1024 or len(history) >= 3:
        raise ValueError("局部识别尝试历史超过存储限额")
    record["attempt_history"] = [
        *deepcopy(history),
        {
            "protocol": "ocr-region-attempt-history-v1",
            "previous_region_sha256": hashlib.sha256(raw).hexdigest(),
            "previous_region": previous,
        },
    ]


def persist_response_evidence(blobs, owner, evidence, *, source_hash, request_page):
    validate_response_binding(evidence, source_hash, request_page)
    encoded = evidence.get("response_base64")
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_ERROR_RESPONSE_BYTES + 2) // 3):
        raise ValueError("识别错误响应超过存储限额")
    raw = base64.b64decode(encoded, validate=True)
    if (
        len(raw) > MAX_ERROR_RESPONSE_BYTES
        or type(evidence.get("size")) is not int
        or len(raw) != evidence["size"]
        or hashlib.sha256(raw).hexdigest() != evidence.get("content_hash")
    ):
        raise ValueError("识别错误响应完整性校验失败")
    # Keep an envelope so that even an empty paid response has a durable object.
    # Its CAS digest is separate from the digest of the raw response bytes.
    payload = dump(evidence).encode()
    if len(payload) > 2 * MAX_ERROR_RESPONSE_BYTES:
        raise ValueError("识别错误响应证据超过存储限额")
    blob = blobs.put_bytes(owner, payload)
    return {
        "content_hash": blob.digest,
        "size": blob.size,
        "storage_path": str(blob.path.relative_to(blobs.files_dir.parent)),
        "mime_type": "application/json",
    }
