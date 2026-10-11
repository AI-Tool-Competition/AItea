from datetime import date

import pytest
from pydantic import ValidationError

from app.catalog import DocumentMetadata, MetadataPatch, eligible


def test_unreviewed_law_is_never_eligible_for_answers():
    metadata = DocumentMetadata(kind="law", review_status="draft")
    assert not eligible(metadata.model_dump(mode="json"), date(2026, 10, 11))


def test_effective_dates_are_inclusive_and_allow_historical_queries():
    metadata = DocumentMetadata(
        kind="law", review_status="published", effective_from="2025-01-01", effective_to="2025-12-31"
    ).model_dump(mode="json")
    assert eligible(metadata, date(2025, 1, 1))
    assert eligible(metadata, date(2025, 12, 31))
    assert not eligible(metadata, date(2026, 10, 11))
    assert not eligible(metadata, date(2024, 12, 31))


def test_business_documents_remain_retrievable_but_are_not_laws():
    metadata = DocumentMetadata().model_dump(mode="json")
    assert metadata["kind"] == "business"
    assert eligible(metadata, date(2026, 10, 11))


def test_dates_and_unknown_fields_are_validated():
    with pytest.raises(ValidationError):
        DocumentMetadata(effective_from="2026-01-01", effective_to="2025-01-01")
    with pytest.raises(ValidationError):
        MetadataPatch(expected_revision=0, arbitrary="ignored")


def test_metadata_patch_requires_a_revision():
    with pytest.raises(ValidationError):
        MetadataPatch(kind="law")
