"""Shared completeness gate; old text-only manifests retain their semantics."""

import hashlib
import json
import re

OCR_VALIDATION_VERSION = "legal-numbers-v3"
_MONTH = "januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember"
_NUMBER_FIELDS = {
    "law_number_year": r"\bnomor\s+(\d{1,5})\s+tahun\s+((?:18|19|20|21)\d{2})\b",
    "date": rf"\btanggal\s+(\d{{1,2}})\s+({_MONTH})\s+((?:18|19|20|21)\d{{2}})\b",
    "publication": r"\btahun[ \t]+((?:18|19|20|21)\d{2})[ \t]+nomor[ \t]+(\d{1,5})\b",
    "article": r"\bpasal\s+(\d{1,4}[a-z]?)\b",
    # Broken PDF layers can insert spaces inside a grouping separator or its
    # three digits. Ignore those spaces, never substitute any numeric digit.
    "amount": r"(?<![\d.,])(\d{1,3}(?:[ \t]*\.[ \t]*\d(?:[ \t]*\d){2})+(?:[ \t]*,[ \t]*\d(?:[ \t]*\d)?)?)(?![\d.,])",
    "year": r"\btahun\s+((?:18|19|20|21)\d{2})\b",
}


def pdf_page_status(page: dict) -> str:
    """Judge the accepted text, preserving damaged native text as evidence."""
    status = page["status"]
    if any(
        "the linked image cannot be displayed" in str(page.get(key, "")).casefold()
        for key in ("native_text", "text")
    ):
        # Pixels and text cannot reconstruct an image absent from the source.
        # Preserve the original and require a replacement source, never OCR it
        # into a seemingly complete legal document.
        return "source_incomplete"
    replacement = "\ufffd" in page.get("text", "")
    if status == "native" and replacement:
        return "pending_ocr"
    if status == "ocr_complete":
        metadata = page.get("ocr") or {}
        if (
            replacement
            or metadata.get("complete") is False
            or metadata.get("uncertain_spans")
            or metadata.get("validation_warnings")
        ):
            return "ocr_uncertain"
    return status


def validate_pdf_page_decode(page: dict) -> None:
    """Revalidate saved native pages without guessing missing characters."""
    if page["status"] in {"native", "pending_ocr"} and "\ufffd" in page.get("text", ""):
        page.update(status="pending_ocr", ocr_required=True)
        reasons = page.setdefault("reasons", [])
        if "native_decode_replacement" not in reasons:
            reasons.append("native_decode_replacement")


def number_anchors(text: str) -> list[dict]:
    result = []
    for field, pattern in _NUMBER_FIELDS.items():
        for match in re.finditer(pattern, text, re.I):
            if field == "year" and any(
                anchor["span"][0] <= match.start() < anchor["span"][1]
                for anchor in result
                if anchor["field"] in {"law_number_year", "publication"}
            ):
                continue
            result.append(
                {
                    "field": field,
                    "value": tuple(re.sub(r"[ \t]", "", part.casefold()) for part in match.groups()),
                    "span": [match.start(), match.end()],
                    "text": match.group(),
                }
            )
    return sorted(result, key=lambda item: item["span"][0])


def _reading_intact(region: dict) -> bool:
    reading = region.get("reading")
    if not isinstance(reading, dict) or not isinstance(reading.get("text"), str):
        return False

    try:
        return (
            region.get("reading_hash")
            == hashlib.sha256(
                json.dumps(reading, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
            ).hexdigest()
        )
    except (ValueError, TypeError):
        return False


def anchor_present(anchor: dict, candidates: list[dict]) -> bool:
    """A year remains the same evidence when a damaged reference becomes readable."""
    for candidate in candidates:
        if candidate["field"] == anchor["field"] and candidate["value"] == anchor["value"]:
            return True
        if anchor["field"] == "year":
            index = {"law_number_year": 1, "publication": 0}.get(candidate["field"])
            if index is not None and candidate["value"][index] == anchor["value"][0]:
                return True
    return False


def _number_verified(page: dict, anchor: dict, transcribed: list[dict]) -> bool:
    recovery = (page.get("ocr") or {}).get("recovery") or {}
    regions = recovery.get("regions", [])
    for proof in recovery.get("verifications", []):
        if proof.get("field") != anchor["field"] or tuple(proof.get("native_value", [])) != anchor["value"]:
            continue
        value = tuple(proof.get("verified_value", []))
        if not any(item["field"] == anchor["field"] and item["value"] == value for item in transcribed):
            continue
        region_ids = proof.get("region_ids", [])
        if (
            not isinstance(region_ids, list)
            or len(region_ids) != 2
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in region_ids
            )
            or len(set(region_ids)) != 2
        ):
            continue
        evidence = [item for item in regions if item.get("region_id") in region_ids]
        if (
            len(evidence) != 2
            or {item.get("region_id") for item in evidence} != set(region_ids)
            or len({item.get("image_sha256") for item in evidence}) != 2
        ):
            continue
        if all(
            item.get("source_hash") == page.get("source_hash")
            and item.get("page") == page.get("page")
            and isinstance(item.get("image_blob"), dict)
            and item["image_blob"].get("content_hash") == item.get("image_sha256")
            and _reading_intact(item)
            and not (item.get("reading") or {}).get("uncertain_spans", ["missing"])
            and any(
                candidate["field"] == anchor["field"] and candidate["value"] == value
                for candidate in number_anchors((item.get("reading") or {}).get("text", ""))
            )
            for item in evidence
        ):
            return True
    return False


def validate_ocr_page(page: dict) -> None:
    """Flag conflicting numeric evidence, never choose or silently correct text.

    Indonesian forms are deliberately narrow. Only readable anchors in the
    native layer are compared; this is not proof that either layer is correct.
    Keep both layers and reuse this gate when reassembling saved checkpoints.
    """
    validate_pdf_page_decode(page)
    if page.get("status") not in {"ocr_complete", "ocr_uncertain"}:
        return
    native, transcribed = page.get("native_text", ""), page.get("text", "")
    metadata = page.setdefault("ocr", {})
    warnings = [
        warning
        for warning in metadata.get("validation_warnings", [])
        if not (warning.startswith("native_ocr_") and warning.endswith("_mismatch"))
        and warning != "ocr_decode_replacement"
    ]
    if "\ufffd" in transcribed:
        warnings.append("ocr_decode_replacement")
    anchors = [number_anchors(text) for text in (native, transcribed)]
    for anchor in anchors[0]:
        if not anchor_present(anchor, anchors[1]):
            if not _number_verified(page, anchor, anchors[1]):
                warning = "native_ocr_" + anchor["field"] + "_mismatch"
                if warning not in warnings:
                    warnings.append(warning)
    metadata["validation_version"] = OCR_VALIDATION_VERSION
    metadata["validation_warnings"] = warnings
    uncertainties = [
        item
        for item in metadata.get("uncertain_spans", [])
        if not (item.startswith("native_ocr_") and item.endswith("_mismatch"))
        and item != "ocr_decode_replacement"
    ]
    if metadata.get("complete") is False and "model_reported_incomplete" not in uncertainties:
        uncertainties.append("model_reported_incomplete")
    uncertainties.extend(warning for warning in warnings if warning not in uncertainties)
    metadata["uncertain_spans"] = uncertainties
    page["status"] = "ocr_uncertain" if uncertainties else "ocr_complete"
    if page["status"] == "ocr_complete":
        page["reasons"] = [
            reason for reason in page.get("reasons", []) if reason != "native_decode_replacement"
        ]


def text_complete(manifest: dict) -> bool:
    from .native_asset_ocr import assets

    try:
        if any(asset["asset_status"] != "ocr_complete" for asset in assets(manifest)):
            return False
    except (ValueError, TypeError, KeyError):
        return False
    quality = manifest.get("quality")
    if manifest.get("parser") == "html-basic" and (
        manifest.get("parser_version") != "3"
        or str(manifest.get("pipeline_version", "")).split("+", 1)[0] != "html-source-mapping-v3"
        or not isinstance(quality, dict)
        or not isinstance(manifest.get("html_source"), dict)
        or manifest.get("html_source", {}).get("mapping_version") != "html-source-mapping-v3"
    ):
        return False
    if manifest.get("parser") == "docx-basic" and (
        manifest.get("parser_version") != "2"
        or str(manifest.get("pipeline_version", "")).split("+", 1)[0] != "docx-package-mapping-v2"
        or not isinstance(quality, dict)
    ):
        # The old paragraph-only reader omitted headers, notes and objects.
        # Its cached body must not stand in for a verified complete package.
        return False
    return (quality is None or (isinstance(quality, dict) and quality.get("complete") is True)) and all(
        pdf_page_status(page) in {"native", "ocr_complete"} for page in manifest.get("pages", [])
    )


def ocr_pending(manifest: dict) -> list[int]:
    return [page["page"] for page in manifest.get("pages", []) if pdf_page_status(page) == "pending_ocr"]


def has_mapped_source_objects(manifest: dict) -> bool:
    """An image-only source can preserve mappings without invented body text."""
    from .native_asset_ocr import assets

    html = manifest.get("html_source") or {}
    return bool(
        assets(manifest)
        or (
            isinstance(html, dict)
            and html.get("mapping_version") == "html-source-mapping-v3"
            and isinstance(html.get("objects"), list)
            and html["objects"]
        )
    )


def ocr_work_pending(manifest: dict) -> bool:
    """Keep PDF page numbers separate from mapped native image identities."""
    from .native_asset_ocr import pending_assets

    try:
        return bool(ocr_pending(manifest) or pending_assets(manifest))
    except (ValueError, TypeError, KeyError):
        return False


def quality_warning(manifest: dict) -> str | None:
    from .native_asset_ocr import assets

    if text_complete(manifest):
        return None
    quality = manifest.get("quality") or {}
    pending = len(ocr_pending(manifest))
    failed = len(quality.get("failed_pages", []))
    review = len(quality.get("review_pages", []))
    source_incomplete = sum(
        pdf_page_status(page) == "source_incomplete" for page in manifest.get("pages", [])
    )
    try:
        image_assets = assets(manifest)
    except (ValueError, TypeError, KeyError):
        return "原生图片来源映射待核验；原件已保存，暂不进入检索。"
    detail = "、".join(
        label
        for count, label in (
            (pending, f"待识别 {pending} 页"),
            (failed, f"识别失败 {failed} 页"),
            (review, f"待核对 {review} 页"),
            (source_incomplete, f"原件缺少图片 {source_incomplete} 页"),
            (
                sum(asset["asset_status"] == "pending_vision" for asset in image_assets),
                "原生图片待识别",
            ),
            (sum(asset["asset_status"] == "ocr_failed" for asset in image_assets), "原生图片识别失败"),
            (sum(asset["asset_status"] == "ocr_uncertain" for asset in image_assets), "原生图片待核对"),
            (
                sum(asset["asset_status"] == "image_reference_pending" for asset in image_assets),
                "原生图片引用待核验",
            ),
        )
        if count
    )
    return f"正文未完整处理{('：' + detail) if detail else ''}；原件已保存，暂不进入检索。"
