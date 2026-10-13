"""Shared completeness gate; old text-only manifests retain their semantics."""

import re

OCR_VALIDATION_VERSION = "legal-numbers-v1"
_MONTH = "januari|februari|maret|april|mei|juni|juli|agustus|september|oktober|november|desember"
_NUMBER_FIELDS = {
    "law_number_year": r"\bnomor\s+(\d{1,5})\s+tahun\s+((?:18|19|20|21)\d{2})\b",
    "date": rf"\btanggal\s+(\d{{1,2}})\s+({_MONTH})\s+((?:18|19|20|21)\d{{2}})\b",
    "publication": r"\btahun\s+((?:18|19|20|21)\d{2})\s+nomor\s+(\d{1,5})\b",
    "article": r"\bpasal\s+(\d{1,4}[a-z]?)\b",
    "amount": r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?)(?![\d.,])",
}


def validate_ocr_page(page: dict) -> None:
    """Flag conflicting numeric evidence, never choose or silently correct text.

    Indonesian forms are deliberately narrow. Only readable anchors in the
    native layer are compared; this is not proof that either layer is correct.
    Keep both layers and reuse this gate when reassembling saved checkpoints.
    """
    if page.get("status") not in {"ocr_complete", "ocr_uncertain"}:
        return
    native, transcribed = page.get("native_text", ""), page.get("text", "")
    warnings = []
    for field, pattern in _NUMBER_FIELDS.items():
        values = [
            {tuple(part.casefold() for part in match.groups()) for match in re.finditer(pattern, text, re.I)}
            for text in (native, transcribed)
        ]
        if values[0] - values[1]:
            warnings.append("native_ocr_" + field + "_mismatch")
    metadata = page.setdefault("ocr", {})
    metadata["validation_version"] = OCR_VALIDATION_VERSION
    metadata["validation_warnings"] = warnings
    if warnings:
        uncertainties = metadata.setdefault("uncertain_spans", [])
        uncertainties.extend(warning for warning in warnings if warning not in uncertainties)
        page["status"] = "ocr_uncertain"


def text_complete(manifest: dict) -> bool:
    quality = manifest.get("quality")
    return quality is None or (isinstance(quality, dict) and quality.get("complete") is True)


def ocr_pending(manifest: dict) -> list[int]:
    return [page["page"] for page in manifest.get("pages", []) if page.get("status") == "pending_ocr"]


def quality_warning(manifest: dict) -> str | None:
    if text_complete(manifest):
        return None
    quality = manifest.get("quality") or {}
    pending = len(ocr_pending(manifest))
    failed = len(quality.get("failed_pages", []))
    review = len(quality.get("review_pages", []))
    detail = "、".join(
        label
        for count, label in (
            (pending, f"待识别 {pending} 页"),
            (failed, f"识别失败 {failed} 页"),
            (review, f"待核对 {review} 页"),
        )
        if count
    )
    return f"正文未完整处理{('：' + detail) if detail else ''}；原件已保存，暂不进入检索。"
