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
    return row


def _fresh(content, manifest, object_key):
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
    row = _object(manifest, object_key)
    if row != _object(fresh, object_key):
        raise ValueError("HTML 图片 DOM、字符范围或 URL 原文与原件不一致。")
    return row


def _bind(content, manifest, object_key, source_url, image, reference, fetched_url):
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
    return result


def _worker(connection, *args):
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (35,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        result = _bind(*args)
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
    content, manifest, *, object_key, source_url, image, image_blob, fetched_url=None
):
    """Bind explicitly acquired bytes, preserving the raw src literal and CAS."""
    from .parsing import _run_bounded

    return _run_bounded(
        _worker, (content, manifest, object_key, source_url, image, image_blob, fetched_url), 45
    )


def extract_image(content, manifest, value, image):
    validate_binding(manifest, value)
    _fresh(content, manifest, value["object_key"])
    if not isinstance(image, bytes) or len(image) != value["size"] or _hash(image) != value["image_hash"]:
        raise ValueError("HTML 图片原件字节与已保存的 CAS 绑定不一致。")
    return image


def read_linked_images(data_dir, owner, manifest, *, source_url=None, allowed_source_urls=None):
    """Read only this account's exact CAS objects, without following symlinks."""
    from .native_asset_ocr import assets

    rows = [row for row in assets(manifest) if row["source_format"] == "html"]
    if sum(row["size"] for row in rows) > MAX_TOTAL_IMAGE_BYTES:
        raise ValueError("HTML 图片原件累计读取超过限制。")
    owner_hash, result = _hash(owner.encode()), {}
    root = Path(data_dir).resolve()
    for row in rows:
        validate_binding(manifest, row)
        if source_url is not None or allowed_source_urls is not None:
            from .ingestion import _validated_url

            allowed = list(allowed_source_urls or [])
            if source_url is not None:
                allowed.append(source_url)
            if len(allowed) > 51 or row["source_url"] not in {_validated_url(url)[0] for url in allowed}:
                raise ValueError("HTML 图片父来源网址与当前文档不一致。")
        path = row["original_image_blob"]["storage_path"]
        if path.split("/")[2] != owner_hash:
            raise ValueError("HTML 图片 CAS 不属于当前账号。")
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            parts = path.split("/")
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_fd
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=descriptor)
            with os.fdopen(file_fd, "rb") as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_size != row["size"]:
                    raise ValueError("HTML 图片 CAS 原件大小无效。")
                raw = stream.read(row["size"] + 1)
                after = os.fstat(stream.fileno())
                if (
                    len(raw) != row["size"]
                    or _hash(raw) != row["image_hash"]
                    or before.st_mtime_ns != after.st_mtime_ns
                    or before.st_ctime_ns != after.st_ctime_ns
                ):
                    raise ValueError("HTML 图片 CAS 原件发生变化。")
                result[row["asset_key"]] = raw
        finally:
            os.close(descriptor)
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
