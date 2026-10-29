"""Resolve whole source units before fallback decisions and legal analysis."""

from itertools import zip_longest

from app.contexts.documents.infrastructure.country_packs import CountryPacks


def retrieval_mode(trace):
    modes = set()
    for step in trace:
        output = step.get("output_data") or {}
        modes.update(str(output.get("retrieval_mode") or output.get("mode") or "").split("+"))
        modes.update(item.get("mode") for item in output.get("coverage", []))
    return next(
        (mode for mode in ("ragflow", "local_pack_vector", "local_pack", "official_web") if mode in modes),
        "unavailable",
    )


def balance_countries(citations, countries):
    groups = [[item for item in citations if item.get("country") == country] for country in countries]
    return [item for batch in zip_longest(*groups) for item in batch if item is not None]


def complete_source_unit(citation, data_dir=None):
    metadata = citation.get("metadata") or {}
    if metadata.get("source_location_verified") is False:
        return None, "source_location_unverified"
    if metadata.get("context_complete") is False:
        record_id = metadata.get("source_record_id")
        full = (
            CountryPacks(data_dir).read(citation.get("country"), record_id)
            if data_dir and record_id
            else None
        )
        if not full or not full["metadata"].get("context_complete"):
            return None, "incomplete_source_unit"
        citation = {**full, "id": citation["id"]}
        if citation["metadata"].get("source_location_verified") is False:
            return None, "source_location_unverified"
    text = citation.get("excerpt") or ""
    if not text.strip():
        return None, "missing_source_text"
    if len(text) > 5000:
        return None, "whole_unit_exceeds_citation_budget"
    # A physical OCR block can span columns or start midway through an annex
    # row. It is not a complete legal unit merely because its bytes fit.
    unit = str(citation.get("locator", {}).get("unit_id") or "")
    if "BLOCK" in unit and not citation.get("locator", {}).get("article"):
        return None, "unresolved_table_scope"
    return citation, None
