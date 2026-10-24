"""Large source CSV remains immutable, bounded, and outside legal approval/RAG."""

import csv
import hashlib
import io
import json

import pytest
from app.contexts.collection.infrastructure.discovery import singapore_cases as cases
from app.shared.infrastructure.blob_store import BlobIntegrityError
from app.shared.infrastructure.http.download import DownloadError

HEADER = [
    "Judgment_URL",
    "Judgment_Reference",
    "Year",
    "Court_Type",
    "Case_Number",
    "Case Name",
    "Current Court Level",
    "Fact_Query",
    "Cited Case",
    "Paragraph",
    "Key Principles Illustrated",
    "Issue",
    "Issue Group",
]


def source(body):
    commit = "1" * 40
    return {
        "repository": cases.REPOSITORY,
        "filename": cases.FILENAME,
        "commit": commit,
        "size": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "source_url": f"https://huggingface.co/datasets/{cases.REPOSITORY}/resolve/{commit}/{cases.FILENAME}?download=true",
    }


def ranges_transport(monkeypatch, body, *, failure=None, etag='"source-version-one"'):
    requests = []

    def download(url, *, consume, byte_range, range_total_bytes, max_bytes, range_validator, **options):
        start, end = byte_range
        requests.append(start)
        assert range_total_bytes == len(body)
        assert end - start + 1 <= max_bytes <= 16 * 1024 * 1024
        if failure and failure(start):
            raise DownloadError("bounded transient source error", retryable=True)
        current_etag = etag(start) if callable(etag) else etag
        return {
            "url": "https://us.aws.cdn.hf.co/dataset?SIGNED_CREDENTIAL_DO_NOT_EXPORT",
            "response_status": 206,
            "total_size": len(body),
            "etag": current_etag,
            "blob": consume(iter([body[start : end + 1]])),
        }

    monkeypatch.setattr(cases, "_download", download)
    return requests


def dataset_file(tmp_path, rows, header=HEADER, encoding="latin-1"):
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(header)
    for row in rows:
        writer.writerow([row.get(field, "") for field in header])
    body = text.getvalue().encode(encoding)
    path = tmp_path / "source.csv"
    path.write_bytes(body)
    return path, source(body)


def case(year=2016, number=1, **changes):
    row = dict(
        zip(
            HEADER,
            [
                f"https://www.elitigation.sg/gd/s/{year}_SGHC_{number}",
                f"[{year}] SGHC {number}",
                str(year),
                "SGHC",
                str(number),
                "Supply company v distributor",
                "High Court",
                "A distributor supplied defective goods.",
                "Older v Other [2000] SGCA 3",
                'Long source paragraph\nwith CSV comma, and quotation: "original"',
                "Sale of goods and breach of contract damages.",
                "Supply contract",
                "Contract",
            ],
            strict=True,
        )
    )
    row.update(changes)
    return row


def test_range_parts_resume_without_redownload_and_require_full_source_hash(tmp_path, monkeypatch):
    body = b"source-block\n" * 17000
    requests = ranges_transport(monkeypatch, body, failure=lambda start: start == 65536)
    collector = cases.DatasetRanges(tmp_path, source(body), part_bytes=65536, workers=2)
    with pytest.raises(cases.DatasetValidationError, match="incomplete"):
        collector.download()
    state = json.loads((tmp_path / "download.json").read_text())
    assert state["status"] == "download_incomplete"
    assert "65536" not in state["parts"]
    assert not (tmp_path / "dataset").exists()
    assert "SIGNED_CREDENTIAL_DO_NOT_EXPORT" not in json.dumps(state)
    requests = ranges_transport(monkeypatch, body)
    original = cases.DatasetRanges(tmp_path, source(body), part_bytes=65536, workers=2).download()
    assert requests == [65536]
    assert original.read_bytes() == body
    assert original.stat().st_mode & 0o777 == 0o400
    state = json.loads((tmp_path / "download.json").read_text())
    assert state["full_hash_verified"] is True
    assert state["status"] == "full_hash_verified"


def test_changed_etag_cannot_publish_a_mixed_original(tmp_path, monkeypatch):
    body = b"immutable" * 20000
    ranges_transport(monkeypatch, body, etag=lambda start: '"old"' if start == 0 else '"new"')
    with pytest.raises(cases.DatasetValidationError, match="incomplete"):
        cases.DatasetRanges(tmp_path, source(body), part_bytes=65536).download()
    assert not (tmp_path / "dataset").exists()
    state = json.loads((tmp_path / "download.json").read_text())
    assert list(state["parts"]) == ["0"]


def test_corrupt_checkpoint_part_is_rejected_before_network_resume(tmp_path, monkeypatch):
    body = b"source" * 20000
    ranges_transport(monkeypatch, body)
    cases.DatasetRanges(tmp_path, source(body), part_bytes=65536).download()
    state = json.loads((tmp_path / "download.json").read_text())
    path = tmp_path / state["parts"]["0"]["path"]
    path.chmod(0o600)
    path.write_bytes(b"tampered original")
    requests = ranges_transport(monkeypatch, body)
    with pytest.raises(BlobIntegrityError):
        cases.DatasetRanges(tmp_path, source(body), part_bytes=65536).download()
    assert requests == []


def test_actual_thirteen_fields_multiline_years_and_optional_mapping(tmp_path):
    rows = [case(2015), case(2016), case(2025), case(2026), case(2016)]
    path, metadata = dataset_file(tmp_path, rows)
    result = cases.validate_csv(path, metadata, tmp_path)
    assert result["actual_header"] == HEADER
    assert result["column_count"] == 13
    assert result["field_mapping"]["citing_url"] == "Judgment_URL"
    assert result["field_mapping"]["facts"] == "Fact_Query"
    assert result["field_mapping"]["cited_court_level"] is None
    assert result["field_mapping"]["precedential_weight"] is None
    assert result["annual_counts"] == {"2015": 1, "2016": 2, "2025": 1, "2026": 1}
    assert result["range_row_count"] == 3
    assert result["deduplicated_rows"] == 1
    assert result["candidate_count"] == 2
    assert result["verified_propositions"] == result["rag_records"] == 0
    candidates = [json.loads(line) for line in (tmp_path / "candidates.jsonl").read_text().splitlines()]
    assert all(c["source_locator"]["end_line"] > c["source_locator"]["start_line"] for c in candidates)
    assert all(c["rag_eligible"] is False for c in candidates)
    assert not (tmp_path / "tea.sqlite3").exists()
    assert (tmp_path / "candidates.jsonl").stat().st_mode & 0o777 == 0o600


def test_long_quotes_are_quarantined_and_never_exported(tmp_path):
    quote = "PRIVATE_LONG_JUDGMENT_LITERAL\n" * 1000
    path, metadata = dataset_file(tmp_path, [case(**{"Cited Case": quote, "Paragraph": quote})])
    result = cases.validate_csv(path, metadata, tmp_path)
    candidate_text = (tmp_path / "candidates.jsonl").read_text()
    assert "PRIVATE_LONG_JUDGMENT_LITERAL" not in candidate_text
    candidate = json.loads(candidate_text)
    assert candidate["review_status"] == "quarantined"
    assert "cited_case_contains_long_quote" in candidate["quarantine_reasons"]
    assert "cited_target_not_unambiguously_identified" in candidate["quarantine_reasons"]
    assert candidate["cited_case_content_hash"] == hashlib.sha256(quote.strip().encode()).hexdigest()
    assert result["quarantined_candidates"] == 1


def test_candidates_never_exceed_original_spec_limit(tmp_path):
    path, metadata = dataset_file(tmp_path, [case(number=number) for number in range(1, 180)])
    report = cases.validate_csv(path, metadata, tmp_path)
    assert report["business_candidates_before_queue_limit"] == 179
    assert report["candidate_count"] == 150
    with pytest.raises(cases.DatasetValidationError):
        cases.validate_csv(path, metadata, tmp_path, candidate_limit=151)


def test_financial_criminal_family_and_inconsistent_citing_year_are_not_candidates(tmp_path):
    rows = [
        case(**{"Issue Group": "Criminal sentencing"}),
        case(number=2, **{"Issue Group": "Family law"}),
        case(number=3, **{"Issue Group": "Securities regulation"}),
        case(number=4, **{"Judgment_Reference": "[2015] SGHC 4"}),
    ]
    path, metadata = dataset_file(tmp_path, rows)
    result = cases.validate_csv(path, metadata, tmp_path)
    assert result["candidate_count"] == 0
    assert result["metadata_mismatch_rows"] == 1


def test_wrong_full_hash_prevents_all_csv_exports(tmp_path):
    path, metadata = dataset_file(tmp_path, [case()])
    metadata["sha256"] = "0" * 64
    with pytest.raises(BlobIntegrityError):
        cases.validate_csv(path, metadata, tmp_path)
    assert not (tmp_path / "candidates.jsonl").exists()
    assert not (tmp_path / "report.json").exists()


def test_actual_utf8_is_verified_from_all_bytes_despite_stale_data_card(tmp_path):
    path, metadata = dataset_file(
        tmp_path, [case(**{"Fact_Query": "The buyer’s café contract."})], encoding="utf-8"
    )
    report = cases.validate_csv(path, metadata, tmp_path)
    candidate = json.loads((tmp_path / "candidates.jsonl").read_text())
    assert report["encoding"] == "utf-8"
    assert report["encoding_proof"]["strict_utf8_valid"] is True
    assert report["encoding_proof"]["bytes_checked"] == metadata["size"]
    assert candidate["facts_excerpt"] == "The buyer’s café contract."


def test_legacy_latin1_requires_observed_utf8_rejection(tmp_path):
    path, metadata = dataset_file(tmp_path, [case(**{"Fact_Query": "The café contract."})])
    report = cases.validate_csv(path, metadata, tmp_path)
    candidate = json.loads((tmp_path / "candidates.jsonl").read_text())
    assert report["encoding"] == "latin-1"
    assert report["encoding_proof"]["strict_utf8_valid"] is False
    assert candidate["facts_excerpt"] == "The café contract."


def test_invalid_utf8_offset_remains_exact_across_a_chunk_boundary(tmp_path):
    path = tmp_path / "encoding.bin"
    path.write_bytes(b"a" * 65535 + b"\xc3(")
    encoding, proof = cases.observed_encoding(path)
    assert encoding == "latin-1"
    assert proof["invalid_byte_offset"] == 65535


def test_short_named_authority_is_usable_for_manual_review_without_inferred_edge(tmp_path):
    context = "41 Contract law refers to [1996] 1 SLR(R) 540.\n42 Another authority is [1999] 1 SLR(R) 739."
    row = case(
        **{"Cited Case": "Cowie Edward Bruce v Berger International Pte Ltd [1999]", "Paragraph": context}
    )
    path, metadata = dataset_file(tmp_path, [row])
    report = cases.validate_csv(path, metadata, tmp_path)
    candidate = json.loads((tmp_path / "candidates.jsonl").read_text())
    assert candidate["review_status"] == "candidate_pending_paragraph_review"
    assert candidate["cited_case_label"] == row["Cited Case"]
    assert candidate["cited_references"] == []
    assert candidate["source_context_paragraph_numbers"] == [41, 42]
    assert report["candidate_case_labels"] == report["candidate_context_references"] == 1
    for reference in candidate["context_reference_candidates"]:
        assert reference["target_identity_verified"] is False
        assert reference["field_content_hash"] == hashlib.sha256(context.encode()).hexdigest()
        assert context[reference["character_start"] : reference["character_end"]] == reference["reference"]
    assert candidate["rag_eligible"] is False
    assert report["verified_propositions"] == 0


def test_year_and_citation_checks_cover_entire_window_before_business_filter(tmp_path):
    row = case(**{"Issue Group": "Family law", "Judgment_Reference": "[2015] SGHC 1"})
    path, metadata = dataset_file(tmp_path, [row])
    report = cases.validate_csv(path, metadata, tmp_path)
    assert report["metadata_mismatch_rows"] == 1
    assert (
        report["metadata_validation_scope"]
        == "all_citing_rows_2016_through_2025_before_business_topic_filter"
    )
    assert report["business_topic_matched_before_deduplication"] == 0
    assert report["candidate_count"] == 0


@pytest.mark.parametrize("header", [HEADER + [HEADER[0]], HEADER[:-1], HEADER + ["URL"]])
def test_ambiguous_or_missing_actual_fields_require_source_review(header):
    with pytest.raises(cases.DatasetValidationError):
        cases.field_mapping(header)


def test_range_source_cannot_switch_repository_or_arbitrary_url(tmp_path):
    metadata = source(b"original")
    metadata["source_url"] = "https://arbitrary.example/some.csv"
    with pytest.raises(cases.DatasetValidationError, match="reviewed repository"):
        cases.DatasetRanges(tmp_path, metadata)
