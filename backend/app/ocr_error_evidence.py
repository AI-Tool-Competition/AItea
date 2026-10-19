"""Persist bounded failed OCR responses against a previously saved request."""

import base64
import hashlib

from .db import dump
from .vision import MAX_ERROR_RESPONSE_BYTES


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
