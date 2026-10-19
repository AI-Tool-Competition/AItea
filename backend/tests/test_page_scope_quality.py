"""Large OCR diagnostics stay bounded without weakening exact content checks."""

import pytest

from app import page_scope_quality as quality


def forbid_matcher(*args, **kwargs):
    pytest.fail("Large or identical page diagnostics must not invoke SequenceMatcher")


def test_full_size_identical_page_uses_exact_similarity_without_quadratic_work(monkeypatch):
    monkeypatch.setattr(quality, "SequenceMatcher", forbid_matcher)
    page = {"text": "a" * 100_000}
    result = quality.compare_page(page, page)
    assert result["normalized_letters_digits_similarity"] == 1.0
    assert result["normalized_letters_digits_similarity_basis"] == "identical"
    assert result["scope_only_change_accepted"]


def test_full_size_changed_page_omits_similarity_but_still_fails_exact_gate(monkeypatch):
    monkeypatch.setattr(quality, "SequenceMatcher", forbid_matcher)
    old, new = "a" * 100_000, "a" * 99_999 + "b"
    result = quality.compare_page({"text": old}, {"text": new})
    assert result["normalized_letters_digits_similarity"] is None
    assert result["normalized_letters_digits_similarity_basis"] == "omitted_work_limit"
    assert not result["scope_only_change_accepted"]
    assert "scope_retry_letters_digits_changed" in result["review_reasons"]
    assert "scope_retry_content_or_structure_changed" in result["review_reasons"]


def test_small_changed_page_keeps_the_exact_diagnostic_metric():
    result = quality.compare_page({"text": "cat"}, {"text": "cut"})
    assert result["normalized_letters_digits_similarity"] == pytest.approx(2 / 3)
    assert result["normalized_letters_digits_similarity_basis"] == "sequence_matcher"
    assert not result["scope_only_change_accepted"]


@pytest.mark.parametrize(
    "old,new,reason",
    [
        ("| ab | c |", "| a | bc |", "scope_retry_table_cells_changed"),
        ("Fine 100.000", "Fine 100,000", "scope_retry_content_or_structure_changed"),
        ("| a | b | |", "| a | b |", "scope_retry_table_cells_changed"),
    ],
)
def test_identical_letters_do_not_short_circuit_semantic_or_table_gates(monkeypatch, old, new, reason):
    monkeypatch.setattr(quality, "SequenceMatcher", forbid_matcher)
    result = quality.compare_page({"text": old}, {"text": new})
    assert result["normalized_letters_digits_similarity"] == 1.0
    assert not result["scope_only_change_accepted"]
    assert reason in result["review_reasons"]
