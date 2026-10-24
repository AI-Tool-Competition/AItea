"""Read-only presentation of parsed content and account-scoped raster assets.

The projection never changes indexing, review, versions or the source manifest.
Images are addressed through document-local identifiers, never filesystem paths.
"""

import base64
import hashlib
import re

from app.contexts.knowledge.application.contracts import DocumentPreview

MAX_IMAGE_BYTES = 8 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def _text(value, maximum=None):
    return value[:maximum].strip() if isinstance(value, str) else ""


def _image_summary(summary, transcript):
    if _text(summary):
        return _text(summary, 600)
    # An OCR excerpt is source content, not an invented visual description.
    text = re.sub(r"\s+", " ", _text(transcript))
    return text[:180].rstrip() + ("…" if len(text) > 180 else "")


def _dimensions(source):
    width, height = source.get("width"), source.get("height")
    return (
        {"width": width, "height": height}
        if type(width) is int and type(height) is int and 0 < width <= 10000 and 0 < height <= 10000
        else {}
    )


def _images(manifest):
    from app.contexts.knowledge.infrastructure.providers.native_asset_ocr import assets

    result = []
    # Older/incomplete image checkpoints must not hide readable body text.
    try:
        native = assets(manifest)
    except ValueError:
        native = []
    for index, asset in enumerate(native):
        ocr = asset.get("ocr") or {}
        if asset.get("source_format") != "docx" and not ocr.get("image_blob"):
            continue
        result.append(
            (
                f"native-{index}",
                asset,
                {
                    "kind": "image",
                    "image_id": f"native-{index}",
                    "caption": _text(asset.get("caption"), 500),
                    "summary": _image_summary(
                        asset.get("vision_summary") or ocr.get("summary"), ocr.get("text")
                    ),
                    **(_dimensions(ocr) or _dimensions(asset)),
                },
            )
        )
    for page in manifest.get("pages", []):
        number = page.get("page")
        if type(number) is not int or not 1 <= number <= 5000:
            continue
        ocr = page.get("ocr") or {}
        # The stored OCR pixels or an identified visual page are real source
        # material. Do not pretend an unparsed image has a vision summary.
        if not ocr.get("image_blob") and not set(page.get("reasons", [])).intersection(
            {"large_raster", "table_layout", "native_visibility_review"}
        ):
            continue
        result.append(
            (
                f"page-{number}",
                page,
                {
                    "kind": "image",
                    "image_id": f"page-{number}",
                    "caption": f"第 {number} 页",
                    "summary": _image_summary(ocr.get("summary"), ocr.get("text") or page.get("text")),
                    **_dimensions(ocr),
                },
            )
        )
    return result


def _same_location(left, right):
    if left.get("asset_key") and left.get("asset_key") == right.get("asset_key"):
        return True
    if left.get("page") is not None:
        return left.get("page") == right.get("page")
    paragraph = left.get("paragraph_start")
    return (
        paragraph is not None
        and paragraph == right.get("paragraph_start")
        and left.get("package_part", left.get("part")) == right.get("package_part", right.get("part"))
    )


def preview(manifest, offset=0, limit=100):
    elements = manifest.get("elements", [])
    images = _images(manifest)
    body_text = {}
    if images:
        for element in elements:
            locator = element.get("locator") or {}
            key = ("page", locator["page"]) if locator.get("page") else ("asset", locator.get("asset_key"))
            body_text.setdefault(key, []).append(_text(element.get("content_text", element.get("text"))))
        body_text = {key: re.sub(r"\s+", " ", " ".join(parts)) for key, parts in body_text.items()}
    after = {}
    for _, source, block in images:
        key = ("page", source["page"]) if source.get("page") else ("asset", source.get("asset_key"))
        provided = _text(source.get("vision_summary") or (source.get("ocr") or {}).get("summary"))
        if not provided and block["summary"] and block["summary"].removesuffix("…") in body_text.get(key, ""):
            block["summary"] = ""
        locator = source.get("locator") or {"page": source.get("page")}
        anchor = next(
            (
                i
                for i, element in enumerate(elements)
                if _same_location(locator, element.get("locator") or {})
            ),
            len(elements),
        )
        after.setdefault(anchor, []).append(block)
    blocks = []
    table_cells = {}
    for table in manifest.get("tables", []):
        for cell in table.get("cells", []):
            key = (table.get("table"), cell.get("row"), cell.get("column_start"))
            table_cells[key] = cell
    index = 0
    while index < len(elements):
        element = elements[index]
        locator = element.get("locator") or {}
        table_id = locator.get("table")
        if (table_id, locator.get("row"), locator.get("column_start")) in table_cells and element.get(
            "kind"
        ) == "table":
            # Group only consecutive mapped cells. A large table can span
            # bounded reading pages; its source grid and merged spans survive.
            cells, seen = [], set()
            end = index
            while end < len(elements):
                part = elements[end]
                loc = part.get("locator") or {}
                if (
                    part.get("kind") != "table"
                    or loc.get("table") != table_id
                    or loc.get("package_part") != locator.get("package_part")
                ):
                    break
                key = (table_id, loc.get("row"), loc.get("column_start"))
                if key not in seen:
                    seen.add(key)
                    mapped = table_cells.get(key, {})
                    if mapped.get("vertical_merge") != "continue":
                        cells.append(
                            {
                                "row": loc.get("row", 1),
                                "text": _text(mapped.get("text", part.get("content_text"))),
                                "col_span": max(1, min(1024, loc.get("column_span", 1))),
                                "row_span": max(1, min(10000, mapped.get("row_span", 1))),
                            }
                        )
                end += 1
                if (
                    len(cells) >= 100
                    and end < len(elements)
                    and (elements[end].get("locator") or {}).get("row") != loc.get("row")
                ):
                    break
            if cells:
                blocks.append({"kind": "table", "cells": cells})
            for at in range(index, end):
                blocks.extend(after.get(at, []))
            index = end
            continue
        text = _text(element.get("content_text", element.get("text")))
        if text:
            blocks.append({"kind": element.get("kind") or "paragraph", "text": text})
        blocks.extend(after.get(index, []))
        index += 1
    blocks.extend(after.get(len(elements), []))
    summary = _text(manifest.get("summary"), 1000)
    if not summary:
        summary = next(
            (
                _text(element.get("content_text"), 1000)
                for element in elements
                if element.get("kind") in {"abstract", "summary"}
            ),
            "",
        )
    return DocumentPreview(
        summary=summary, blocks=blocks[offset : offset + limit], total=len(blocks), offset=offset
    ).model_dump(exclude_none=True)


def _blob_image(service, row, reference):
    digest = reference.get("content_hash")
    size = reference.get("size")
    if (
        not isinstance(digest, str)
        or not _HASH.fullmatch(digest)
        or type(size) is not int
        or not 0 < size <= MAX_IMAGE_BYTES
        or reference.get("mime_type") != "image/png"
    ):
        raise ValueError("图片存储引用无效。")
    owner_digest = hashlib.sha256(row["owner_id"].encode()).hexdigest()
    # Derive the account's CAS path; never open a path supplied by a manifest.
    root = service.db.files_dir.resolve()
    path = root / "blobs" / owner_digest / digest[:2] / digest[2:4] / digest
    if not path.resolve().is_relative_to(root) or path.resolve() != path.absolute():
        raise ValueError("图片存储引用无效。")
    from app.contexts.knowledge.infrastructure.parsing.archive_parsing import _open_original

    stream, _ = _open_original(path, size)
    with stream:
        raw = stream.read(MAX_IMAGE_BYTES + 1)
    if (
        len(raw) != size
        or hashlib.sha256(raw).hexdigest() != digest
        or not raw.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        raise ValueError("图片内容校验失败。")
    return raw


def image_bytes(service, row, manifest, image_id):
    source = next((source for key, source, _ in _images(manifest) if key == image_id), None)
    if source is None:
        from app.shared.domain.errors import fail

        fail(404, "not_found", "未找到图片")
    reference = (source.get("ocr") or {}).get("image_blob")
    if reference:
        return _blob_image(service, row, reference)
    if image_id.startswith("page-"):
        from app.contexts.knowledge.infrastructure.parsing.archive_parsing import render_archive_page_bounded

        rendered = render_archive_page_bounded(
            row["storage_path"],
            source_hash=manifest["source_hash"],
            size=row["size"],
            page_number=source["page"],
        )
    else:
        from app.contexts.knowledge.infrastructure.providers.native_asset_ocr import (
            render_native_asset_bounded,
        )

        rendered = render_native_asset_bounded(manifest, source, row["storage_path"], row["size"])
    raw = base64.b64decode(rendered["image_base64"], validate=True)
    if (
        not 0 < len(raw) <= MAX_IMAGE_BYTES
        or hashlib.sha256(raw).hexdigest() != rendered["image_sha256"]
        or not raw.startswith(b"\x89PNG\r\n\x1a\n")
    ):
        raise ValueError("图片内容校验失败。")
    return raw
