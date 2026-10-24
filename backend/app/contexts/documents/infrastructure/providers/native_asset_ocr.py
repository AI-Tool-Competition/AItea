"""Bounded, resumable OCR for mapped native images; never invent PDF pages.

The caller owns account/version authorization, leases and persistence. Native
document completeness and legal eligibility remain separate from image OCR.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy

MAX_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PIXELS = 6_000_000
MAX_HTML_PIXELS = 12_000_000
MAX_ASSETS = 2000
MAX_MANIFEST_BYTES = 40 * 1024 * 1024
PROTOCOL = "native-asset-ocr-v1"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_PREFIX = "native-asset-ocr-"


def _pixels_limit(source_format):
    return MAX_HTML_PIXELS if source_format in {"html", "mineru_chart"} else MAX_PIXELS


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def _identity(value, source_hash, source_format, pointer):
    image_blob = value.get("image_blob")
    image_blob = image_blob if isinstance(image_blob, dict) else {}
    fields = {
        "source_hash": source_hash,
        "source_format": source_format,
        # MinerU's stable element key, physical locator and exact chart bytes
        # identify the asset; its position in the sidecar list may shift when
        # another chart is added, so list position must not trigger a paid retry.
        "mapping_path": None if source_format == "mineru_chart" else pointer,
        "locator": value.get("locator"),
        "image_sha256": value.get("image_hash", value.get("image_sha256")),
        "package_part": value.get("package_part"),
        "relationship_id": value.get("relationship_id"),
        "source_part": value.get("source_part"),
        "size": value.get("size"),
    }
    if source_format == "html":
        fields.update(
            {
                key: value.get(key)
                for key in (
                    "object_key",
                    "source_fragment_sha256",
                    "source_url",
                    "requested_url",
                    "fetched_url",
                    "url_attribute",
                    "original_image_blob",
                    "protocol",
                )
            }
        )
    elif source_format == "mineru_chart":
        fields.update(
            {
                "element_key": value.get("element_key"),
                "section_path": value.get("section_path", []),
                "image_blob": {key: image_blob.get(key) for key in ("content_hash", "size")},
                "block_type": value.get("block_type"),
            }
        )
    return _digest(json.dumps(fields, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())


def assets(manifest: dict) -> list[dict]:
    """Return stable mapped assets without reading files or calling a model."""
    if manifest.get("parser") == "docx-basic" and manifest.get("parser_version") == "2":
        rows, prefix, source_format = manifest.get("assets_pending", []), "/assets_pending", "docx"
    elif manifest.get("parser") == "html-basic" and manifest.get("parser_version") in {
        "3",
        "4",
        "5",
        "6",
        "7",
        "8",
        "9",
    }:
        html = manifest.get("html_source") or {}
        if not isinstance(html, dict):
            raise ValueError("HTML 图片映射结构无效。")
        rows = html.get("linked_images", [])
        prefix, source_format = "/html_source/linked_images", "html"
    elif manifest.get("parser") in {"legacy-wpf", "legacy-doc"}:
        legacy = manifest.get("legacy") or {}
        if not isinstance(legacy, dict) or not isinstance(legacy.get("conversion", {}), dict):
            raise ValueError("旧格式图片映射结构无效。")
        rows = legacy.get("conversion", {}).get("images", [])
        prefix, source_format = "/legacy/conversion/images", legacy.get("format")
    elif manifest.get("parser") == "mineru" and manifest.get("remote_parse", {}).get("provider") == "mineru":
        rows = manifest.get("mineru_chart_assets", [])
        prefix, source_format = "/mineru_chart_assets", "mineru_chart"
    else:
        return []
    if rows == []:
        return []
    source_hash = manifest.get("source_hash")
    if not isinstance(source_hash, str) or not _HASH.fullmatch(source_hash):
        raise ValueError("原生图片父原件 hash 无效。")
    if source_format not in {"docx", "html", "mineru_chart"} and legacy.get("source_hash") != source_hash:
        raise ValueError("旧格式图片与父原件不一致。")
    if not isinstance(rows, list) or len(rows) > MAX_ASSETS:
        raise ValueError("原生图片映射数量超过限制。")
    result = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict) or not isinstance(raw.get("locator"), dict):
            raise ValueError("原生图片缺少来源定位。")
        if raw["locator"].get("source_hash") != source_hash:
            raise ValueError("原生图片定位与父原件不一致。")
        if source_format == "mineru_chart" and (
            not isinstance(raw.get("element_key"), str)
            or not re.fullmatch(r"mineru-\d{6}-\d{6}", raw["element_key"])
        ):
            raise ValueError("MinerU 图表结构块标识无效。")
        if source_format == "html":
            from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import validate_binding

            validate_binding(manifest, raw)
        pointer = f"{prefix}/{index}"
        identity = _identity(raw, source_hash, source_format, pointer)
        key = raw.get("asset_key") if source_format in {"docx", "mineru_chart"} else _PREFIX + identity[:24]
        if not isinstance(key, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", key):
            raise ValueError("原生图片标识无效。")
        if source_format not in {"docx", "mineru_chart"} and raw.get("asset_key", key) != key:
            raise ValueError("转换图片标识与来源不一致。")
        if (
            source_format == "mineru_chart"
            and key != "mineru-chart-" + hashlib.sha256(raw["element_key"].encode()).hexdigest()[:24]
        ):
            raise ValueError("MinerU 图表标识与结构块不一致。")
        value = deepcopy(raw)
        default = "pending_vision"
        if source_format == "mineru_chart":
            image_blob = raw.get("image_blob")
            section_path = raw.get("section_path", [])
            if (
                raw.get("block_type") != "chart"
                or not isinstance(image_blob, dict)
                or image_blob.get("content_hash") != raw.get("image_hash")
                or type(image_blob.get("size")) is not int
                or not 0 < image_blob["size"] <= MAX_IMAGE_BYTES
                or raw.get("size") != image_blob["size"]
                or raw["locator"].get("kind") != "pdf"
                or type(raw["locator"].get("page")) is not int
                or raw["locator"].get("source_hash") != source_hash
                or not isinstance(section_path, list)
                or len(section_path) > 128
                or any(not isinstance(part, str) or len(part) > 2000 for part in section_path)
            ):
                raise ValueError("MinerU 图表映射缺少有效标题层级或图片来源。")
        if (
            source_format not in {"docx", "html", "mineru_chart"}
            and raw.get("image_bytes_verified") is not True
        ):
            default = "image_reference_pending"
        status = raw.get("asset_status", default)
        if (
            source_format == "mineru_chart"
            and default == "image_reference_pending"
            and status == "pending_vision"
        ):
            status = default
        if status not in {
            "pending_vision",
            "ocr_complete",
            "ocr_uncertain",
            "ocr_failed",
            "image_reference_pending",
        }:
            raise ValueError("原生图片识别状态无效。")
        value.update(
            asset_key=key,
            mapping_path=pointer,
            source_format=source_format,
            source_hash=source_hash,
            input_identity=identity,
            asset_status=status,
        )
        if status in {"ocr_complete", "ocr_uncertain"}:
            ocr = value.get("ocr") or {}
            if not isinstance(ocr, dict):
                raise ValueError("已识别原生图片的检查点结构无效。")
            if (
                ocr.get("input_identity") != identity
                or ocr.get("source_hash") != source_hash
                or ocr.get("asset_key") != key
                or ocr.get("mapping_path") != pointer
                or ocr.get("original_image_sha256") != raw.get("image_hash", raw.get("image_sha256"))
                or not isinstance(ocr.get("image_sha256"), str)
                or not _HASH.fullmatch(ocr["image_sha256"])
                or not isinstance(ocr.get("text"), str)
                or len(ocr["text"]) > 100_000
                or ocr.get("protocol") != PROTOCOL
                or ocr.get("locator") != raw["locator"]
                or ocr.get("physical_pages_known") is not (source_format == "mineru_chart")
                or type(ocr.get("width")) is not int
                or type(ocr.get("height")) is not int
                or not 0 < ocr["width"] <= 10000
                or not 0 < ocr["height"] <= 10000
                or ocr["width"] * ocr["height"] > _pixels_limit(source_format)
                or ocr.get("frame_count") != 1
                or ocr.get("frame_basis") != "all_frames_verified_single_frame"
                or type(ocr.get("image_size")) is not int
                or not 0 < ocr["image_size"] <= MAX_IMAGE_BYTES
                or not isinstance(ocr.get("model"), str)
                or not ocr["model"]
                or len(ocr["model"]) > 200
                or not isinstance(ocr.get("uncertain_spans"), list)
                or type(ocr.get("complete")) is not bool
                or type(ocr.get("is_blank")) is not bool
                or (status == "ocr_complete" and (ocr["complete"] is not True or ocr["uncertain_spans"]))
            ):
                raise ValueError("已识别原生图片的检查点与来源不一致。")
        result.append(value)
    if len({value["asset_key"] for value in result}) != len(result):
        raise ValueError("原生图片标识重复。")
    if source_format == "html" and len({value["object_key"] for value in result}) != len(result):
        raise ValueError("HTML 正文对象被重复绑定。")
    if source_format == "mineru_chart" and len({value["element_key"] for value in result}) != len(result):
        raise ValueError("MinerU 图表映射重复。")
    return result


def pending_assets(manifest: dict) -> list[dict]:
    return [value for value in assets(manifest) if value["asset_status"] == "pending_vision"]


def read_mineru_chart_images(files_dir, owner_id, manifest, *, max_assets=4):
    """Read only hash-bound chart blobs selected for the next bounded OCR batch."""
    from app.contexts.documents.infrastructure.providers.mineru_results import resolve_reference
    from app.shared.infrastructure.persistence.blob_store import _verify_existing

    if type(max_assets) is not int or not 1 <= max_assets <= 20:
        raise ValueError("单批 MinerU 图表数量必须在 1 至 20 之间。")
    selected = pending_assets(manifest)[:max_assets]
    result = {}
    for asset in selected:
        blob = asset["image_blob"]
        path = resolve_reference(blob, files_dir, owner_id)
        _verify_existing(path, blob["content_hash"], blob["size"])
        raw = path.read_bytes()
        if _digest(raw) != asset["image_hash"] or len(raw) != asset["size"]:
            raise ValueError("MinerU 图表原件 hash 或大小不匹配。")
        result[asset["asset_key"]] = raw
    return result


def apply_asset_checkpoint(manifest: dict, value: dict) -> None:
    """Replace only the exact mapped asset; callers supply their own lease lock."""
    current = next(
        (item for item in assets(manifest) if item["mapping_path"] == value.get("mapping_path")), None
    )
    if current is None or any(
        current.get(key) != value.get(key)
        for key in ("asset_key", "input_identity", "source_hash", "source_format")
    ):
        raise ValueError("原生图片检查点与当前映射不一致。")
    # Source fields cannot be rewritten by a callback or model.
    if (
        _identity(value, value["source_hash"], value["source_format"], value["mapping_path"])
        != current["input_identity"]
    ):
        raise ValueError("原生图片检查点修改了原始来源。")
    index = int(value["mapping_path"].rsplit("/", 1)[1])
    if value["source_format"] == "docx":
        rows = manifest["assets_pending"]
    elif value["source_format"] == "html":
        rows = manifest["html_source"]["linked_images"]
    elif value["source_format"] == "mineru_chart":
        rows = manifest["mineru_chart_assets"]
    else:
        rows = manifest["legacy"]["conversion"]["images"]
    rows[index] = deepcopy(value)


def reuse_mineru_chart_checkpoints(previous: dict, parsed: dict) -> dict:
    """Carry OCR only when the exact MinerU chart, page and image bytes recur."""
    if (
        previous.get("parser") != "mineru"
        or parsed.get("parser") != "mineru"
        or not previous.get("mineru_chart_assets")
        or not parsed.get("mineru_chart_assets")
    ):
        return parsed
    old_by_key = {value["element_key"]: value for value in assets(previous)}
    reused = False
    for current in assets(parsed):
        old = old_by_key.get(current["element_key"])
        if old is None or old["input_identity"] != current["input_identity"]:
            continue
        apply_asset_checkpoint(parsed, old)
        reused = True
    return rebuild_native_asset_text_bounded(parsed) if reused else parsed


def summary(manifest: dict) -> dict:
    values = assets(manifest)
    return {
        "protocol": PROTOCOL,
        "pending_assets": [v["asset_key"] for v in values if v["asset_status"] == "pending_vision"],
        "failed_assets": [v["asset_key"] for v in values if v["asset_status"] == "ocr_failed"],
        "review_assets": [
            v["asset_key"]
            for v in values
            if v["asset_status"] in {"ocr_uncertain", "image_reference_pending"}
        ],
        "completed_assets": [v["asset_key"] for v in values if v["asset_status"] == "ocr_complete"],
        "deferred_assets": [
            v["asset_key"]
            for v in values
            if v.get("deferred_reason") and v["asset_status"] == "pending_vision"
        ],
        "retry_after_seconds": 5,
        "physical_pages_known": False,
    }


def _decode_inline(value, *, digest_key="sha256", limit=MAX_MANIFEST_BYTES):
    size, digest, encoded = value.get("size"), value.get(digest_key), value.get("content_base64")
    if (
        type(size) is not int
        or not 0 < size <= limit
        or not isinstance(encoded, str)
        or len(encoded) > ((limit + 2) // 3) * 4
    ):
        raise ValueError("转换图片或 artifact 字节超过限制。")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise ValueError("转换图片或 artifact 编码无效。") from exc
    if len(raw) != size or _digest(raw) != digest or base64.b64encode(raw).decode("ascii") != encoded:
        raise ValueError("转换图片或 artifact 字节 hash 不匹配。")
    return raw


def _extract(content, manifest, value, *, max_bytes=MAX_BYTES, image_content=None):
    if value["source_format"] == "docx":
        from app.contexts.documents.infrastructure.parsers.docx_parsing import _package, parse_docx

        original = parse_docx(content, max_bytes=max_bytes)
        fresh = next((item for item in assets(original) if item["asset_key"] == value["asset_key"]), None)
        if fresh is None or fresh["input_identity"] != value["input_identity"]:
            raise ValueError("DOCX 图片部件、关系或 XML 定位与原件不一致。")
        raw = _package(content, max_bytes=max_bytes)[value["package_part"]]
    elif value["source_format"] == "html":
        from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import extract_image

        raw = extract_image(content, manifest, value, image_content)
    elif value["source_format"] == "mineru_chart":
        raw = image_content
        if not isinstance(raw, bytes) or not raw:
            raise ValueError("MinerU 图表原件尚未从内容地址库读取。")
    elif value["source_format"] == "wpf":
        from app.contexts.documents.infrastructure.parsers.legacy_doc_parsing import _wpf_artifacts

        if not content.startswith(b"\xffWPC"):
            raise ValueError("WPF 父原件签名无效。")
        conversion = manifest["legacy"]["conversion"]
        artifacts = conversion.get("artifacts", [])
        if not isinstance(artifacts, list) or len(artifacts) != 2:
            raise ValueError("WPF 图片缺少转换 artifact。")
        html = next((a for a in artifacts if a.get("kind") == "libwpd-html"), None)
        events = next((a for a in artifacts if a.get("kind") == "libwpd-events"), None)
        if html is None or events is None or value["locator"].get("artifact_sha256") != html.get("sha256"):
            raise ValueError("WPF 图片与转换 HTML artifact 不一致。")
        _, conversion = _wpf_artifacts(_decode_inline(html), _decode_inline(events), value["source_hash"])
        original = {
            "parser": "legacy-wpf",
            "source_hash": value["source_hash"],
            "legacy": {"source_hash": value["source_hash"], "format": "wpf", "conversion": conversion},
        }
        fresh = next(
            (item for item in assets(original) if item["mapping_path"] == value["mapping_path"]), None
        )
        if fresh is None or fresh["input_identity"] != value["input_identity"]:
            raise ValueError("WPF 图片与转换 HTML 位置不一致。")
        raw = _decode_inline(value, digest_key="image_sha256", limit=MAX_IMAGE_BYTES)
    else:
        raise ValueError("DOC 对象尚无已核验的可识别图片字节。")
    if (
        not raw
        or len(raw) > MAX_IMAGE_BYTES
        or _digest(raw) != value.get("image_hash", value.get("image_sha256"))
    ):
        raise ValueError("原生图片原字节 hash 不匹配或超过限制。")
    return raw


def _render(content, manifest, value, archive_path=None, archive_size=None, image_content=None):
    from PIL import Image, ImageOps, UnidentifiedImageError

    max_bytes = MAX_BYTES
    mineru_chart_mode = value["source_format"] == "mineru_chart"
    if content is None and not mineru_chart_mode:
        from app.contexts.documents.infrastructure.parsers.docx_archive_parsing import (
            MAX_ARCHIVE_BYTES,
            read_docx_source,
        )

        content = read_docx_source(archive_path, source_hash=value["source_hash"], size=archive_size)
        max_bytes = MAX_ARCHIVE_BYTES
    if not mineru_chart_mode and (not isinstance(content, bytes) or _digest(content) != value["source_hash"]):
        raise ValueError("原生图片父原件 hash 不匹配。")
    raw = _extract(content, manifest, value, max_bytes=max_bytes, image_content=image_content)
    try:
        with Image.open(io.BytesIO(raw)) as image:
            width, height = image.size
            frames = getattr(image, "n_frames", 1)
            if image.format not in {"PNG", "JPEG", "GIF"}:
                raise ValueError("原生图片光栅格式不受支持。")
            if frames != 1:
                raise ValueError("多帧原生图片需要逐帧映射，不能只识别首帧。")
            if (
                not 0 < width <= 10000
                or not 0 < height <= 10000
                or width * height > _pixels_limit(value["source_format"])
            ):
                raise ValueError("原生图片像素超过限制。")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            orientation = image.getexif().get(274, 1)
            oriented = ImageOps.exif_transpose(image)
            rgba = oriented.convert("RGBA")
            canvas = Image.new("RGB", oriented.size, "white")
            canvas.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            canvas.save(output, format="PNG")
            png = output.getvalue()
            rendered_width, rendered_height = canvas.size
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("原生图片完整光栅解码失败。") from exc
    if len(png) > MAX_IMAGE_BYTES:
        raise ValueError("原生图片 PNG 超过存储限制。")
    return {
        "source_hash": value["source_hash"],
        "asset_key": value["asset_key"],
        "mapping_path": value["mapping_path"],
        "input_identity": value["input_identity"],
        "locator": deepcopy(value["locator"]),
        "source_format": value["source_format"],
        "original_image_sha256": _digest(raw),
        "original_image_size": len(raw),
        "image_sha256": _digest(png),
        "image_size": len(png),
        "image_base64": base64.b64encode(png).decode("ascii"),
        "mime_type": "image/png",
        "width": rendered_width,
        "height": rendered_height,
        "original_width": width,
        "original_height": height,
        "frame_count": 1,
        "frame_basis": "all_frames_verified_single_frame",
        "pixel_transform": "exif_orientation_then_white_alpha_composite_no_resize",
        "exif_orientation": orientation,
        "physical_pages_known": False,
    }


def render_native_asset_bounded(manifest, asset, archive_path, archive_size):
    """Render an already mapped image without OCR or manifest mutation."""
    from app.contexts.documents.infrastructure.parsers.document_parser import _run_bounded

    return _run_bounded(_worker, ("render", (None, manifest, asset, archive_path, archive_size, None)), 45)


def _worker(connection, operation, args):
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (35,) * 2)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if operation == "render":
            result = _render(*args)
        elif operation == "assemble":
            result = rebuild_native_asset_text(*args)
        elif operation == "validate_source":
            from app.contexts.documents.infrastructure.parsers.docx_archive_parsing import read_docx_source

            path, source_hash, size = args
            content = read_docx_source(path, source_hash=source_hash, size=size)
            result = {"source_hash": _digest(content), "size": len(content)}
        else:
            raise ValueError("原生图片处理操作无效。")
        encoded = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False).encode()
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError("原生图片处理结果超过限制。")
        connection.send_bytes(encoded)
    except Exception as exc:
        message = str(exc)[:500] if isinstance(exc, ValueError) else "原生图片处理失败或超过资源限制。"
        connection.send_bytes(json.dumps({"error": message}, ensure_ascii=False).encode())
    finally:
        connection.close()


def _one(
    content, manifest, value, on_asset, on_image, archive_path=None, archive_size=None, image_content=None
):
    from app.contexts.documents.infrastructure.parsers.document_parser import (
        _OCR_SLOTS,
        ProcessingDeferred,
        _run_bounded,
    )
    from app.contexts.documents.infrastructure.providers.vision import (
        IMAGE_PROMPT_VERSION,
        OCRCapacityDeferred,
        OCRResponseError,
        transcribe_chart_image,
        transcribe_image,
    )

    updated = deepcopy(value)

    def checkpoint():
        if on_asset is not None:
            on_asset(deepcopy(updated))

    def defer(reason):
        updated.update(asset_status="pending_vision", deferred_reason=reason)
        updated.pop("error", None)
        checkpoint()
        return updated

    if not _OCR_SLOTS.acquire(timeout=5):
        return defer("ocr_capacity")
    try:
        updated.pop("deferred_reason", None)
        attempts = (updated.get("ocr") or {}).get("attempts", 0)
        if type(attempts) is not int or not 0 <= attempts <= 3:
            raise ValueError("原生图片识别尝试次数无效。")
        metadata = {
            "protocol": PROTOCOL,
            "prompt_version": IMAGE_PROMPT_VERSION,
            "requested_model": os.getenv("LLM_MODEL", "")[:200],
            "source_hash": value["source_hash"],
            "asset_key": value["asset_key"],
            "mapping_path": value["mapping_path"],
            "input_identity": value["input_identity"],
            "attempts": attempts,
        }

        def failure(exc):
            if isinstance(exc, OCRCapacityDeferred):
                metadata["attempts"] = attempts
                updated.update(ocr=metadata)
                return defer("ocr_capacity")
            retryable = isinstance(exc, OCRResponseError) and exc.retryable and metadata["attempts"] < 3
            metadata["finish_reason"] = getattr(exc, "finish_reason", None)
            updated.update(
                asset_status="pending_vision" if retryable else "ocr_failed",
                ocr=metadata,
                error=str(exc)[:500],
            )
            if retryable:
                updated["deferred_reason"] = "provider_retry"
            checkpoint()
            return updated

        if attempts >= 3:
            return failure(ValueError("原生图片识别已达到自动重试上限。"))
        try:
            rendered = _run_bounded(
                _worker, ("render", (content, manifest, value, archive_path, archive_size, image_content)), 45
            )
        except ProcessingDeferred:
            return defer("render_capacity")
        except (ValueError, OSError) as exc:
            return failure(exc)
        metadata.update({key: val for key, val in rendered.items() if key != "image_base64"})
        # CAS and checkpoint callback failures propagate; no external call follows.
        if on_image is not None:
            reference = on_image(deepcopy(rendered))
            if not isinstance(reference, dict):
                raise ValueError("原生图片存储回调必须返回持久引用。")
            if (
                reference.get("content_hash") != rendered["image_sha256"]
                or type(reference.get("size")) is not int
                or reference["size"] != rendered["image_size"]
                or not isinstance(reference.get("storage_path"), str)
                or not 0 < len(reference["storage_path"]) <= 2048
            ):
                raise ValueError("原生图片持久引用与请求 PNG 不一致。")
            metadata["image_blob"] = reference
        metadata["attempts"] += 1
        updated.update(ocr=metadata, asset_status="pending_vision")
        checkpoint()
        try:
            transcript = (
                transcribe_chart_image(rendered)
                if value["source_format"] == "mineru_chart"
                else transcribe_image(rendered)
            )
        except (ValueError, OSError) as exc:
            return failure(exc)
        metadata.update(transcript)
        if value["source_format"] == "mineru_chart":
            # MinerU binds each chart crop to a verified physical page and bbox;
            # the transcript is still only a chart-region reading.
            metadata["physical_pages_known"] = True
        updated.update(
            ocr=metadata,
            asset_status="ocr_uncertain" if transcript["uncertain_spans"] else "ocr_complete",
        )
        if value["source_format"] == "mineru_chart" and transcript["is_blank"]:
            updated["asset_status"] = "ocr_uncertain"
            updated["ocr"]["uncertain_spans"] = list(
                dict.fromkeys([*updated["ocr"]["uncertain_spans"], "chart_has_no_transcribed_content"])
            )
        updated.pop("error", None)
        checkpoint()
        return updated
    finally:
        _OCR_SLOTS.release()


def rebuild_native_asset_text(manifest):
    """Rebuild independent image transcription elements, preserving native text."""
    result = deepcopy(manifest)
    elements = [
        e for e in result.get("elements", []) if not str(e.get("element_key", "")).startswith(_PREFIX)
    ]
    chunks = [c for c in result.get("chunks", []) if not str(c.get("id", "")).startswith(_PREFIX)]
    for value in assets(result):
        if value["asset_status"] != "ocr_complete" or not value["ocr"].get("text", "").strip():
            continue
        text, key = value["ocr"]["text"], _PREFIX + value["input_identity"][:24]
        locator = {
            **deepcopy(value["locator"]),
            "asset_key": value["asset_key"],
            "asset_mapping_path": value["mapping_path"],
            "original_image_sha256": value["ocr"]["original_image_sha256"],
            "image_sha256": value["ocr"]["image_sha256"],
            "source_kind": (
                "mineru_chart_transcription"
                if value["source_format"] == "mineru_chart"
                else "native_image_transcription"
            ),
            "physical_pages_known": value["source_format"] == "mineru_chart",
        }
        elements.append(
            {
                "element_key": key,
                "kind": "figure_transcription",
                "order": len(elements),
                "content_text": text,
                "locator": locator,
                "section_path": deepcopy(value.get("section_path") or []),
                "metadata_json": {
                    "protocol": PROTOCOL,
                    "review_status": "unreviewed",
                    "reading_order_basis": "independent_asset_not_document_layout",
                },
            }
        )
        chunks.append(
            {
                "id": key,
                "text": text,
                "retrieval_text": text,
                "section_path": deepcopy(value.get("section_path") or []),
                "locator": {**locator, "source_anchors": [deepcopy(locator)]},
                "content_hash": _digest(text.encode()),
            }
        )
    maximum_text = 4_000_000
    if result.get("parser") == "mineru" and result.get("remote_parse", {}).get("provider") == "mineru":
        from app.contexts.documents.infrastructure.providers.mineru_results import (
            MAX_ASSEMBLED_TEXT_CHARACTERS,
        )

        maximum_text = MAX_ASSEMBLED_TEXT_CHARACTERS
        chart_assets = [value for value in assets(result) if value["source_format"] == "mineru_chart"]
        by_page = {}
        for value in chart_assets:
            by_page.setdefault(value["locator"]["page"], []).append(value)
        for page in result.get("pages", []):
            if "chart_content_missing" not in page.get("reasons", []):
                continue
            mapped = by_page.get(page["page"], [])
            if not mapped or any(
                value["asset_status"] != "ocr_complete" or not value.get("ocr", {}).get("text", "").strip()
                for value in mapped
            ):
                continue
            page["reasons"] = [reason for reason in page["reasons"] if reason != "chart_content_missing"]
            proof = page.get("ocr") or {}
            proof["supplemental_chart_assets"] = [
                {
                    "asset_key": value["asset_key"],
                    "element_key": value["element_key"],
                    "image_sha256": value["image_hash"],
                    "transcript_sha256": _digest(value["ocr"]["text"].encode()),
                    "prompt_version": value["ocr"].get("prompt_version"),
                    "review_status": value["ocr"].get("review_status"),
                }
                for value in mapped
            ]
            proof["uncertain_spans"] = [
                reason for reason in proof.get("uncertain_spans", []) if reason != "chart_content_missing"
            ]
            proof["complete"] = not page["reasons"]
            page["ocr"] = proof
            page["status"] = "ocr_complete" if not page["reasons"] else "ocr_uncertain"
        quality = result.get("quality") or {}
        reviews = [page["page"] for page in result.get("pages", []) if page.get("status") != "ocr_complete"]
        quality.update(
            status="complete" if not reviews else "needs_review",
            complete=not reviews,
            completed_pages=[
                page["page"] for page in result.get("pages", []) if page.get("status") == "ocr_complete"
            ],
            review_pages=reviews,
            review_required=bool(reviews),
        )
        result["quality"] = quality
        result["mineru_chart_ocr"] = summary(result)
    if len(elements) > 20000 or sum(len(e.get("content_text", "")) for e in elements) > maximum_text:
        raise ValueError("原生图片正文重组超过资源限制。")
    result.update(elements=elements, chunks=chunks, native_asset_ocr=summary(result))
    if result.get("parser") == "html-basic" and result.get("html_source", {}).get("linked_images"):
        from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import update_quality

        update_quality(result, assets(result))
    return result


def rebuild_native_asset_text_bounded(manifest):
    """Reassemble a saved checkpoint through the same resource-limited worker."""
    from app.contexts.documents.infrastructure.parsers.document_parser import _run_bounded

    if len(json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode()) > MAX_MANIFEST_BYTES:
        raise ValueError("原生图片 manifest 超过处理限制。")
    return _run_bounded(_worker, ("assemble", (manifest,)), 45)


def ocr_native_assets_bounded(
    content,
    manifest,
    *,
    on_asset=None,
    on_image=None,
    max_assets=4,
    archive_path=None,
    archive_size=None,
    image_contents=None,
):
    """Process one resumable native-image batch; do not alter legal quality gates."""
    from app.contexts.documents.infrastructure.parsers.document_parser import _run_bounded
    from app.contexts.documents.infrastructure.providers.vision import vision_ocr_configured

    if type(max_assets) is not int or not 1 <= max_assets <= 20:
        raise ValueError("单批原生图片数量必须在 1 至 20 之间。")
    if image_contents is not None and (
        not isinstance(image_contents, dict)
        or len(image_contents) > 500
        or any(
            not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_IMAGE_BYTES
            for raw in image_contents.values()
        )
        or sum(len(raw) for raw in image_contents.values()) > 32 * 1024 * 1024
    ):
        raise ValueError("HTML 图片原字节集合超过处理限制。")
    mineru_chart_mode = (
        manifest.get("parser") == "mineru" and manifest.get("remote_parse", {}).get("provider") == "mineru"
    )
    if content is None and not mineru_chart_mode:
        from app.contexts.documents.infrastructure.parsers.docx_archive_parsing import MAX_ARCHIVE_BYTES

        marker = manifest.get("archive_docx") or {}
        if (
            not isinstance(marker, dict)
            or manifest.get("parser") != "docx-basic"
            or manifest.get("parser_version") != "2"
            or marker.get("source_hash") != manifest.get("source_hash")
            or marker.get("size") != archive_size
            or type(archive_size) is not int
            or not 0 < archive_size <= MAX_ARCHIVE_BYTES
            or not archive_path
        ):
            raise ValueError("原生图片 DOCX 文件模式来源标记无效。")
    elif content is not None and (
        archive_path is not None
        or archive_size is not None
        or not isinstance(content, bytes)
        or not content
        or len(content) > MAX_BYTES
        or _digest(content) != manifest.get("source_hash")
    ):
        raise ValueError("原生图片父原件 hash 不匹配或大小超过限制。")
    if len(json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode()) > MAX_MANIFEST_BYTES:
        raise ValueError("原生图片 manifest 超过处理限制。")
    result = deepcopy(manifest)
    if content is None and not mineru_chart_mode:
        _run_bounded(
            _worker, ("validate_source", (archive_path, manifest.get("source_hash"), archive_size)), 45
        )
    pending = pending_assets(result)[:max_assets]
    if pending and vision_ocr_configured():
        from app.contexts.documents.infrastructure.providers.vision import vision_ocr_request_worker_limit

        executor = ThreadPoolExecutor(
            max_workers=min(max_assets, vision_ocr_request_worker_limit()),
            thread_name_prefix="native-asset-ocr",
        )
        futures = [
            executor.submit(
                _one,
                content,
                result,
                value,
                on_asset,
                on_image,
                archive_path,
                archive_size,
                (image_contents or {}).get(value["asset_key"]),
            )
            for value in pending
        ]
        try:
            for future in as_completed(futures):
                apply_asset_checkpoint(result, future.result())
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
    return _run_bounded(_worker, ("assemble", (result,)), 45)
