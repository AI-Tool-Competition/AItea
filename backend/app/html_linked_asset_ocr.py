"""Explicit, bounded binding of stored HTML body images; no remote fetching.

Responsive pictures, SVG, canvas and script-created visuals remain unresolved.
Callers authorize the parent document and persist original images in account CAS.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urljoin

PROTOCOL = "html-linked-image-v1"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 32 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class _BaseHref(HTMLParser):
    def handle_starttag(self, tag, attrs):
        if tag == "base" and any(key == "href" for key, _ in attrs):
            raise ValueError("HTML 含 base/href，相对图片地址需另行核验。")

    handle_startendtag = handle_starttag


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def resolved_url(source_url, literal):
    from .ingestion import _validated_url

    source_url, _ = _validated_url(source_url)
    joined = urljoin(source_url, literal)
    # HTML URL attributes may contain spaces. Preserve the literal separately;
    # encode only for the same HTTPS validation used by the source downloader.
    encoded = quote(joined, safe="%:/?#[]@!$&'()*+,;=~-._")
    return _validated_url(encoded)[0]


def _object(manifest, object_key):
    source = manifest.get("html_source") or {}
    if (
        not isinstance(source, dict)
        or manifest.get("parser") != "html-basic"
        or manifest.get("parser_version") != "3"
        or source.get("mapping_version") != "html-source-mapping-v3"
        or source.get("source_hash") != manifest.get("source_hash")
    ):
        raise ValueError("HTML 图片需要当前版本的原文映射。")
    rows = source.get("objects", [])
    if not isinstance(rows, list) or len(rows) > 500 or any(not isinstance(row, dict) for row in rows):
        raise ValueError("HTML 对象映射数量无效。")
    matches = [row for row in rows if isinstance(row, dict) and row.get("object_key") == object_key]
    if len(matches) != 1:
        raise ValueError("HTML 图片必须唯一对应正文对象。")
    row = matches[0]
    attributes = row.get("url_attributes", [])
    locator = row.get("locator") or {}
    if (
        not isinstance(locator, dict)
        or row.get("tag") != "img"
        or row.get("parent_object_key") is not None
        or row.get("duplicate_url_attributes") is not False
        or locator.get("source_boundary_complete") is not True
        or row.get("source_hash") != manifest.get("source_hash")
        or locator.get("source_hash") != manifest.get("source_hash")
        or not isinstance(attributes, list)
        or len(attributes) != 1
        or not isinstance(attributes[0], dict)
        or attributes[0].get("attribute") != "src"
        or not attributes[0].get("decoded_value")
    ):
        raise ValueError("HTML 图片源有多个候选或不是独立正文 img/src，仍需核验。")
    return row


def _reference(reference, digest, size):
    if (
        not isinstance(reference, dict)
        or reference.get("content_hash") != digest
        or reference.get("size") != size
        or not isinstance(reference.get("storage_path"), str)
        or not re.fullmatch(
            rf"files/blobs/[0-9a-f]{{64}}/{digest[:2]}/{digest[2:4]}/{digest}",
            reference["storage_path"],
        )
    ):
        raise ValueError("HTML 图片原件必须保存在匹配 hash 的账号 CAS 中。")


def validate_binding(manifest, value):
    if not isinstance(value, dict):
        raise ValueError("HTML 图片绑定结构无效。")
    row = _object(manifest, value.get("object_key"))
    source_hash = manifest.get("source_hash")
    digest, size = value.get("image_hash"), value.get("size")
    if (
        not isinstance(source_hash, str)
        or not _HASH.fullmatch(source_hash)
        or value.get("source_hash") != source_hash
        or not isinstance(digest, str)
        or not _HASH.fullmatch(digest)
        or type(size) is not int
        or not 0 < size <= MAX_IMAGE_BYTES
        or value.get("locator") != row["locator"]
        or value.get("source_fragment_sha256") != row.get("source_fragment_sha256")
        or value.get("url_attribute") != row["url_attributes"][0]
        or value.get("protocol") != PROTOCOL
        or value.get("requested_url")
        != resolved_url(value.get("source_url"), row["url_attributes"][0]["decoded_value"])
    ):
        raise ValueError("HTML 图片绑定与父原件的位置、网址或图片字节不一致。")
    from .ingestion import _validated_url

    if _validated_url(value.get("fetched_url"))[0] != value["fetched_url"]:
        raise ValueError("HTML 图片最终来源网址无效。")
    _reference(value.get("original_image_blob"), digest, size)
    receipt = value.get("download_receipt_blob")
    if receipt is not None:
        if (
            not isinstance(receipt, dict)
            or not isinstance(receipt.get("content_hash"), str)
            or not _HASH.fullmatch(receipt["content_hash"])
            or type(receipt.get("size")) is not int
            or not 0 < receipt["size"] <= 2 * 1024 * 1024
        ):
            raise ValueError("HTML 图片下载收据 CAS 引用无效。")
        _reference(receipt, receipt["content_hash"], receipt["size"])
    return row


def _parent_objects(content, manifest):
    from .ingestion import _decode, _parse_html

    if not isinstance(content, bytes) or not content or len(content) > 20 * 1024 * 1024:
        raise ValueError("HTML 父原件超过处理限制。")
    if _hash(content) != manifest.get("source_hash"):
        raise ValueError("HTML 父原件 hash 不匹配。")
    base = _BaseHref()
    base.feed(_decode(content))
    base.close()
    document = _parse_html(content)
    fresh = {
        "parser": document.parser_name,
        "parser_version": document.parser_version,
        "source_hash": _hash(content),
        "html_source": document.html_source,
    }
    if not isinstance(manifest.get("html_source"), dict) or (
        manifest["html_source"].get("objects") != fresh["html_source"]["objects"]
    ):
        raise ValueError("HTML 图片 DOM、字符范围或 URL 原文与原件不一致。")
    quality = manifest.get("quality") or {}
    if not isinstance(quality, dict) or any(
        quality.get(key) != document.quality.get(key)
        for key in ("source_mapping_complete", "body_text_present", "error_page_detected")
    ):
        raise ValueError("HTML 原件完整性证据已变化，需要重新进行受限解析。")
    return fresh["html_source"]["objects"]


def _fresh(content, manifest, object_key):
    _parent_objects(content, manifest)
    return _object(manifest, object_key)


def _candidates(content, manifest):
    rows = _parent_objects(content, manifest)
    eligible, unresolved = [], []
    for row in rows:
        try:
            eligible.append(deepcopy(_object(manifest, row["object_key"])))
        except ValueError:
            unresolved.append({"object_key": row["object_key"], "tag": row["tag"]})
    return {"eligible": eligible, "unresolved": unresolved}


def _bind(content, manifest, object_key, source_url, image, reference, fetched_url, verify_pixels=False):
    from .ingestion import _validated_url

    row = _fresh(content, manifest, object_key)
    if not isinstance(image, bytes) or not 0 < len(image) <= MAX_IMAGE_BYTES:
        raise ValueError("HTML 图片原件超过限制。")
    digest = _hash(image)
    _reference(reference, digest, len(image))
    source_url, _ = _validated_url(source_url)
    requested = resolved_url(source_url, row["url_attributes"][0]["decoded_value"])
    final = _validated_url(fetched_url or requested)[0]
    result = deepcopy(manifest)
    rows = result["html_source"].setdefault("linked_images", [])
    if not isinstance(rows, list) or len(rows) >= 500 or any(r.get("object_key") == object_key for r in rows):
        raise ValueError("HTML 图片已经绑定或数量超过限制。")
    rows.append(
        {
            "protocol": PROTOCOL,
            "object_key": object_key,
            "source_hash": manifest["source_hash"],
            "locator": deepcopy(row["locator"]),
            "source_fragment_sha256": row["source_fragment_sha256"],
            "url_attribute": deepcopy(row["url_attributes"][0]),
            "source_url": source_url,
            "requested_url": requested,
            "fetched_url": final,
            "image_hash": digest,
            "size": len(image),
            "original_image_blob": deepcopy(reference),
            "asset_status": "pending_vision",
        }
    )
    if verify_pixels:
        from .native_asset_ocr import _render, assets

        # Use the same full-frame decoder and pixel limits as OCR, in this
        # bounded child. Acquisition never accepts an octet-stream by MIME alone.
        rendered = _render(content, result, assets(result)[-1], image_content=image)
        rows[-1]["pixel_verification"] = {
            key: rendered[key]
            for key in (
                "original_image_sha256",
                "original_image_size",
                "width",
                "height",
                "original_width",
                "original_height",
                "frame_count",
                "frame_basis",
                "pixel_transform",
                "image_sha256",
                "image_size",
            )
        }
    return result


def _worker(connection, operation, args):
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (35,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        result = _bind(*args) if operation == "bind" else _candidates(*args)
        encoded = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > 40 * 1024 * 1024:
            raise ValueError("HTML 图片 manifest 超过限制。")
        connection.send_bytes(encoded)
    except Exception as exc:
        message = str(exc)[:500] if isinstance(exc, ValueError) else "HTML 图片绑定超过资源限制。"
        connection.send_bytes(json.dumps({"error": message}, ensure_ascii=False).encode())
    finally:
        connection.close()


def bind_linked_image_bounded(
    content,
    manifest,
    *,
    object_key,
    source_url,
    image,
    image_blob,
    fetched_url=None,
    verify_pixels=False,
):
    """Bind explicitly acquired bytes, preserving the raw src literal and CAS."""
    from .parsing import _run_bounded

    return _run_bounded(
        _worker,
        ("bind", (content, manifest, object_key, source_url, image, image_blob, fetched_url, verify_pixels)),
        45,
    )


def image_candidates_bounded(content, manifest):
    """Verify actual source before choosing any remote img/src downloads."""
    from .parsing import _run_bounded

    return _run_bounded(_worker, ("candidates", (content, manifest)), 45)


def extract_image(content, manifest, value, image):
    validate_binding(manifest, value)
    _fresh(content, manifest, value["object_key"])
    if not isinstance(image, bytes) or len(image) != value["size"] or _hash(image) != value["image_hash"]:
        raise ValueError("HTML 图片原件字节与已保存的 CAS 绑定不一致。")
    return image


def read_image_blob(data_dir, owner, reference):
    """Read bounded original image CAS bytes for a verified account."""
    if not isinstance(reference, dict):
        raise ValueError("HTML 图片 CAS 引用无效。")
    digest, size = reference.get("content_hash"), reference.get("size")
    if (
        not isinstance(digest, str)
        or not _HASH.fullmatch(digest)
        or type(size) is not int
        or not 0 < size <= MAX_IMAGE_BYTES
    ):
        raise ValueError("HTML 图片 CAS 大小或 hash 无效。")
    _reference(reference, digest, size)
    path = reference["storage_path"]
    if path.split("/")[2] != _hash(owner.encode()):
        raise ValueError("HTML 图片 CAS 不属于当前账号。")
    descriptor = os.open(Path(data_dir).resolve(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.split("/")
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
        with os.fdopen(file_fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                raise ValueError("HTML 图片 CAS 原件大小无效。")
            raw = stream.read(size + 1)
            after = os.fstat(stream.fileno())
            if (
                len(raw) != size
                or _hash(raw) != digest
                or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)
            ):
                raise ValueError("HTML 图片 CAS 原件发生变化。")
            return raw
    finally:
        os.close(descriptor)


def linked_image_references(manifest, *, source_url=None, allowed_source_urls=None):
    """Validate all mappings without reading every image into memory."""
    from .native_asset_ocr import assets

    rows = [row for row in assets(manifest) if row["source_format"] == "html"]
    for row in rows:
        validate_binding(manifest, row)
        if source_url is not None or allowed_source_urls is not None:
            from .ingestion import _validated_url

            allowed = list(allowed_source_urls or [])
            if source_url is not None:
                allowed.append(source_url)
            if len(allowed) > 51 or row["source_url"] not in {_validated_url(url)[0] for url in allowed}:
                raise ValueError("HTML 图片父来源网址与当前文档不一致。")
    return rows


def read_linked_images(
    data_dir,
    owner,
    manifest,
    *,
    source_url=None,
    allowed_source_urls=None,
    pending_only=False,
    max_assets=None,
):
    """Read only this account's exact CAS objects, without following symlinks."""
    rows = linked_image_references(
        manifest,
        source_url=source_url,
        allowed_source_urls=allowed_source_urls,
    )
    if pending_only:
        rows = [row for row in rows if row["asset_status"] == "pending_vision"]
    if max_assets is not None:
        if type(max_assets) is not int or not 1 <= max_assets <= 20:
            raise ValueError("HTML 图片批次大小无效。")
        rows = rows[:max_assets]
    if sum(row["size"] for row in rows) > MAX_TOTAL_IMAGE_BYTES:
        raise ValueError("HTML 图片原件累计读取超过限制。")
    result = {}
    for row in rows:
        result[row["asset_key"]] = read_image_blob(data_dir, owner, row["original_image_blob"])
    return result


def update_quality(manifest, values):
    """Only all verified body objects can remove the visual completeness gate."""
    quality = manifest.get("quality") or {}
    source = manifest.get("html_source") or {}
    rows = source.get("objects", [])
    complete_keys = {value["object_key"] for value in values if value["asset_status"] == "ocr_complete"}
    pending = [row["object_key"] for row in rows if row["object_key"] not in complete_keys]
    complete = (
        quality.get("source_mapping_complete") is True
        and quality.get("body_text_present") is True
        and quality.get("error_page_detected") is False
        and not pending
    )
    quality.update(
        status="complete" if complete else "partial",
        complete=complete,
        pending_objects=pending,
        visual_verification_required=bool(pending),
        linked_image_protocol=PROTOCOL,
    )
    if complete:
        from .html_parsing import VISUAL_PENDING_WARNING

        warnings = manifest.get("warnings")
        if isinstance(warnings, list) and VISUAL_PENDING_WARNING in warnings:
            resolved = quality.setdefault("resolved_warnings", [])
            if not isinstance(resolved, list):
                raise ValueError("HTML 已解决解析提示的记录无效。")
            if VISUAL_PENDING_WARNING not in resolved:
                resolved.append(VISUAL_PENDING_WARNING)
            manifest["warnings"] = [warning for warning in warnings if warning != VISUAL_PENDING_WARNING]
    manifest["quality"] = quality


def complete(manifest, values):
    objects = (manifest.get("html_source") or {}).get("objects", [])
    if not isinstance(objects, list) or any(not isinstance(row, dict) for row in objects):
        return False
    if not objects:
        return True
    keys = [row.get("object_key") for row in objects]
    verified = [row.get("object_key") for row in values if row["asset_status"] == "ocr_complete"]
    return len(keys) == len(set(keys)) and len(verified) == len(keys) and set(keys) == set(verified)
