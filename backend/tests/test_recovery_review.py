"""Independent regression probes for recovery evidence boundaries."""

from copy import deepcopy

import pytest

from app import ingestion_quality, ocr_recovery, vision
from test_ocr_recovery import uncertain_page


@pytest.mark.parametrize("corruption", ["page", "blob_hash", "duplicate_region_id"])
def test_numeric_verification_rejects_evidence_from_wrong_page_or_blob(monkeypatch, corruption):
    data, page = uncertain_page("Rp 11.234.000.000,00", "Rp 1.234.000.000,00")
    monkeypatch.setattr(
        vision,
        "transcribe_region",
        lambda _: {"text": "Rp 1.234.000.000,00", "uncertain_spans": [], "graphic_marks": []},
    )

    def save(rendered):
        return {"content_hash": rendered["image_sha256"]}

    result = ocr_recovery.recover_page_step(data, page, on_region=save)
    result = ocr_recovery.recover_page_step(data, result, on_region=save)
    assert result["status"] == "ocr_complete"
    corrupted = deepcopy(result)
    region = corrupted["ocr"]["recovery"]["regions"][0]
    if corruption == "page":
        region["page"] = 2
    elif corruption == "blob_hash":
        region["image_blob"]["content_hash"] = "f" * 64
    else:
        corrupted["ocr"]["recovery"]["regions"][1]["region_id"] = region["region_id"]
    ingestion_quality.validate_ocr_page(corrupted)
    assert corrupted["status"] == "ocr_uncertain"
