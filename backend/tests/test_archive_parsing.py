"""Incremental archive PDFs keep original identity and bounded worker resources."""

from __future__ import annotations

import hashlib
import io
from copy import deepcopy
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def pdf_file(tmp_path, page_texts, *, encrypted=False):
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    reference = writer._add_object(font)
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): reference})}
        )
        if text:
            commands = [b"BT /F1 12 Tf 50 700 Td"]
            for line in text.splitlines():
                escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                commands.append(f"({escaped}) Tj 0 -14 Td".encode("ascii"))
            commands.append(b"ET")
            stream = DecodedStreamObject()
            stream.set_data(b" ".join(commands))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("not-the-password")
    buffer = io.BytesIO()
    writer.write(buffer)
    content = buffer.getvalue()
    path = tmp_path / "law.pdf"
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest(), len(content)


def parse(path, digest, size, **kwargs):
    from app import archive_parsing

    return archive_parsing.parse_archive_pdf_bounded(
        path, source_hash=digest, size=size, filename="official-law.pdf", **kwargs
    )


def test_archive_original_uses_file_worker_without_parent_reading_whole_pdf(tmp_path, monkeypatch):
    from app import ingestion

    path, digest, size = pdf_file(tmp_path, ["Tea archive original."])
    monkeypatch.setattr(ingestion, "MAX_BYTES", 128)
    with pytest.raises(ValueError, match="20 MB"):
        ingestion.parse_file(path.name, path.read_bytes())
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("parent reads whole original"))
    result = parse(path, digest, size)
    assert result["source_hash"] == digest
    assert result["quality"]["complete"]
    assert result["chunks"][0]["locator"]["page"] == 1


def test_more_than_300_pages_resume_in_windows_and_keep_completed_pages(tmp_path):
    path, digest, size = pdf_file(tmp_path, [f"Physical page {number}." for number in range(1, 302)])
    result = parse(path, digest, size)
    assert len(result["pages"]) == 301
    assert len(result["archive_pdf"]["parsed_pages"]) == 64
    assert result["pages"][64]["status"] == "pending_parse"
    assert result["quality"]["pending_parse_pages"] == list(range(65, 302))
    assert not result["quality"]["complete"]
    completed = deepcopy(result["pages"][0])
    completed.update(status="ocr_complete", printed_page="i", text="Previously verified OCR text.")
    completed["ocr"] = {"image_sha256": "a" * 64, "model": "preserved-checkpoint"}
    from app.ingestion_quality import validate_ocr_page

    validate_ocr_page(completed)
    result["pages"][0] = deepcopy(completed)
    original = deepcopy(result)
    result = parse(path, digest, size, manifest=result)
    assert result["pages"][0] == completed
    assert original["pages"][64]["status"] == "pending_parse"
    assert len(result["archive_pdf"]["parsed_pages"]) == 128
    for _ in range(3):
        result = parse(path, digest, size, manifest=result)
    assert result["archive_pdf"]["complete"]
    assert result["quality"]["complete"]
    assert result["quality"]["pending_parse_pages"] == []
    assert result["pages"][-1]["page"] == 301
    assert all(page["source_hash"] == digest for page in result["pages"])
    assert any(chunk["locator"].get("page", 0) == 301 for chunk in result["chunks"])
    assert parse(path, digest, size, manifest=result)["pages"][0] == completed


def test_final_reassembly_keeps_pasal_and_penjelasan_across_windows(tmp_path):
    path, digest, size = pdf_file(
        tmp_path,
        ["Pasal 1\nBody first article.", "Continued first article.", "PENJELASAN\nPasal 1\nExplanation."],
    )
    first = parse(path, digest, size, max_pages=2)
    result = parse(path, digest, size, manifest=first, max_pages=2)
    assert result["quality"]["complete"]
    assert any(
        element["locator"].get("page") == 2 and element["section_path"] == ["正文", "Pasal 1"]
        for element in result["elements"]
    )
    assert any(
        element["locator"].get("page") == 3 and element["section_path"] == ["PENJELASAN", "Pasal 1"]
        for element in result["elements"]
    )


def test_scan_page_is_pending_ocr_after_native_windows_finish(tmp_path):
    path, digest, size = pdf_file(tmp_path, ["Native body.", ""])
    result = parse(path, digest, size)
    assert result["archive_pdf"]["complete"]
    assert not result["quality"]["complete"]
    assert result["quality"]["pending_pages"] == [2]
    assert result["pages"][1]["source_hash"] == digest


@pytest.mark.parametrize("problem", ["hash", "size", "symlink", "directory", "empty", "encrypted"])
def test_rejects_untrusted_or_unreadable_original(tmp_path, problem):
    path, digest, size = pdf_file(tmp_path, ["Original."], encrypted=problem == "encrypted")
    if problem == "hash":
        digest = "0" * 64
    elif problem == "size":
        size += 1
    elif problem == "symlink":
        linked = tmp_path / "linked.pdf"
        linked.symlink_to(path)
        path = linked
    elif problem == "directory":
        path = tmp_path
    elif problem == "empty":
        path.write_bytes(b"")
        size = 0
    with pytest.raises(ValueError):
        parse(path, digest, size)


@pytest.mark.parametrize("alter", ["source_hash", "page_number", "page_hash", "duplicate"])
def test_resume_manifest_cannot_change_source_or_physical_mapping(tmp_path, alter):
    path, digest, size = pdf_file(tmp_path, ["Original first.", "Original second."])
    manifest = parse(path, digest, size, max_pages=1)
    if alter == "source_hash":
        manifest["source_hash"] = "a" * 64
    elif alter == "page_number":
        manifest["pages"][0]["page"] = 99
    elif alter == "page_hash":
        manifest["pages"][0]["source_hash"] = "a" * 64
    else:
        manifest["pages"][1] = deepcopy(manifest["pages"][0])
    with pytest.raises(ValueError):
        parse(path, digest, size, manifest=manifest)


def test_archive_size_and_window_limits_fail_before_worker(tmp_path, monkeypatch):
    from app import archive_parsing

    path, digest, size = pdf_file(tmp_path, ["Original."])
    monkeypatch.setattr(archive_parsing, "_MAX_BYTES", size - 1)
    with pytest.raises(ValueError, match="128 MB"):
        parse(path, digest, size)
    for window in (0, 65, True, 1.5):
        with pytest.raises(ValueError):
            parse(path, digest, size, max_pages=window)


def test_page_count_limit_is_explicit_and_never_silently_truncates(tmp_path, monkeypatch):
    from app import archive_parsing

    path, digest, size = pdf_file(tmp_path, ["First.", "Second.", "Third."])
    monkeypatch.setattr(archive_parsing, "_MAX_PAGES", 2)
    with pytest.raises(ValueError, match="页数"):
        parse(path, digest, size)


def test_all_page_parse_preserves_original_byte_identity_on_mutation(tmp_path):
    path, digest, size = pdf_file(tmp_path, ["First.", "Second."])
    manifest = parse(path, digest, size, max_pages=1)
    content = path.read_bytes().replace(b"Second.", b"Changed")
    assert len(content) == size
    path.write_bytes(content)
    with pytest.raises(ValueError, match="hash"):
        parse(path, digest, size, manifest=manifest, max_pages=1)


def test_reassembly_revalidates_ocr_article_numbers_before_marking_complete(tmp_path):
    path, digest, size = pdf_file(tmp_path, ["Pasal 1\nBody first article.", "Second page."])
    manifest = parse(path, digest, size, max_pages=1)
    manifest["pages"][0].update(status="ocr_complete", text="Pasal 99\nBody first article.")
    manifest["pages"][0]["ocr"] = {"uncertain_spans": []}
    result = parse(path, digest, size, manifest=manifest, max_pages=1)
    assert result["pages"][0]["status"] == "ocr_uncertain"
    assert not result["quality"]["complete"]
    assert result["quality"]["review_pages"] == [1]


def test_pdf_with_no_physical_pages_is_rejected(tmp_path):
    path, digest, size = pdf_file(tmp_path, [])
    with pytest.raises(ValueError, match="页数"):
        parse(path, digest, size)


def test_fifo_is_rejected_without_opening_blocking_stream(tmp_path):
    import os

    path = tmp_path / "pipe.pdf"
    os.mkfifo(path)
    with pytest.raises(ValueError, match="普通文件|无法读取"):
        parse(path, "0" * 64, 100)


def test_oversized_page_stream_is_rejected_in_bounded_window(tmp_path, monkeypatch):
    from app import archive_parsing

    path, digest, size = pdf_file(tmp_path, ["Legal body."])
    monkeypatch.setattr(archive_parsing, "_MAX_PAGE_STREAM_BYTES", 1)
    with pytest.raises(ValueError, match="内容流"):
        archive_parsing._parse_archive_window(str(path), digest, size, path.name, None, 64, 5000)


def test_archive_renderer_accepts_page_above_300_and_keeps_source_identity(tmp_path, monkeypatch):
    import base64

    from app import archive_parsing

    path, digest, size = pdf_file(tmp_path, [""] * 300 + ["Physical page 301."])
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("parent reads whole original"))
    rendered = archive_parsing.render_archive_page_bounded(
        path, source_hash=digest, size=size, page_number=301
    )
    data = base64.b64decode(rendered["image_base64"], validate=True)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert rendered["source_hash"] == digest
    assert rendered["page"] == 301
    assert hashlib.sha256(data).hexdigest() == rendered["image_sha256"]
    assert rendered["width"] * rendered["height"] <= 6_000_000
    assert len(data) <= 8 * 1024 * 1024
    assert rendered["render_version"] == "bounded-page-png-v1"


def test_archive_region_renderer_uses_original_top_left_bbox_and_dpi(tmp_path):
    from app import archive_parsing

    path, digest, size = pdf_file(tmp_path, ["Native body."])
    bbox = [40, 70, 180, 110]
    rendered = archive_parsing.render_archive_page_bounded(
        path, source_hash=digest, size=size, page_number=1, region_bbox=bbox, dpi=400
    )
    repeated = archive_parsing.render_archive_page_bounded(
        path, source_hash=digest, size=size, page_number=1, region_bbox=bbox, dpi=400
    )
    assert rendered["region_bbox"] == bbox
    assert rendered["render_version"] == "bounded-region-png-v1"
    assert rendered["dpi"] == pytest.approx(400)
    assert rendered["region_id"] == repeated["region_id"]
    assert rendered["image_sha256"] == repeated["image_sha256"]
    assert rendered["width"] == pytest.approx((bbox[2] - bbox[0]) * 400 / 72, abs=2)
    assert rendered["height"] == pytest.approx((bbox[3] - bbox[1]) * 400 / 72, abs=2)


@pytest.mark.parametrize(
    "args",
    [
        {"page_number": 0},
        {"page_number": 2},
        {"page_number": True},
        {"page_number": 1, "region_bbox": [0, 0, 900, 20]},
        {"page_number": 1, "region_bbox": [20, 0, 10, 20]},
        {"page_number": 1, "region_bbox": [0, 0, float("nan"), 20]},
        {"page_number": 1, "region_bbox": [False, 0, 10, 20]},
        {"page_number": 1, "dpi": 1200},
    ],
)
def test_archive_renderer_rejects_invalid_absolute_page_bbox_and_dpi(tmp_path, args):
    from app import archive_parsing

    path, digest, size = pdf_file(tmp_path, ["Native body."])
    with pytest.raises(ValueError):
        archive_parsing.render_archive_page_bounded(path, source_hash=digest, size=size, **args)


def test_archive_renderer_verifies_hash_and_no_follow_before_creating_image(tmp_path):
    from app import archive_parsing

    path, digest, size = pdf_file(tmp_path, ["Native body."])
    with pytest.raises(ValueError, match="hash"):
        archive_parsing.render_archive_page_bounded(path, source_hash="0" * 64, size=size, page_number=1)
    linked = tmp_path / "linked.pdf"
    linked.symlink_to(path)
    with pytest.raises(ValueError):
        archive_parsing.render_archive_page_bounded(linked, source_hash=digest, size=size, page_number=1)


def test_archive_rebuild_handles_more_than_300_pages_without_reading_original(tmp_path, monkeypatch):
    from app import archive_parsing

    path, digest, size = pdf_file(tmp_path, ["Native body."] * 301)
    manifest = parse(path, digest, size, max_pages=1)
    for page in manifest["pages"]:
        page.update(status="native", native_text="Native body.", text="Native body.")
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("rebuild reads original"))
    path.unlink()
    result = archive_parsing.archive_rebuild_bounded(manifest)
    assert result["source_hash"] == digest
    assert result["quality"]["complete"]
    assert result["quality"]["page_count"] == 301
    assert result["archive_pdf"]["complete"]
    assert result["archive_pdf"]["pending_pages"] == []
    assert result["archive_pdf"]["source_size"] == size
    assert result["pages"][300]["page"] == 301


def test_archive_rebuild_keeps_pending_parse_gate_and_ocr_number_review(tmp_path):
    from app import archive_parsing

    path, digest, size = pdf_file(tmp_path, ["Pasal 1\nBody.", "Native body."])
    manifest = parse(path, digest, size, max_pages=1)
    manifest["pages"][0].update(status="ocr_complete", text="Pasal 99\nBody.", ocr={"uncertain_spans": []})
    result = archive_parsing.archive_rebuild_bounded(manifest)
    assert not result["quality"]["complete"]
    assert result["quality"]["pending_parse_pages"] == [2]
    assert result["quality"]["review_pages"] == [1]
    assert result["pages"][1]["status"] == "pending_parse"


@pytest.mark.parametrize("field", ["total_pages", "source_size", "page_hash", "text_size"])
def test_archive_rebuild_checks_full_source_metadata_and_body_capacity(tmp_path, field):
    from app import archive_parsing

    path, digest, size = pdf_file(tmp_path, ["Native body."])
    manifest = parse(path, digest, size)
    if field == "total_pages":
        manifest["archive_pdf"]["total_pages"] = 5001
    elif field == "source_size":
        manifest["archive_pdf"]["source_size"] = 128 * 1024 * 1024 + 1
    elif field == "page_hash":
        manifest["pages"][0]["source_hash"] = "0" * 64
    else:
        manifest["pages"][0]["text"] = "x" * 4_000_001
    with pytest.raises(ValueError):
        archive_parsing.archive_rebuild_bounded(manifest)


def test_old_table_checkpoint_is_reprocessed_once_without_losing_verified_ocr(tmp_path):
    from test_pdf_tables import grid_pdf

    path = tmp_path / "tariffs.pdf"
    content = grid_pdf()
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    manifest = parse(path, digest, len(content))
    assert manifest["quality"]["complete"]
    page = manifest["pages"][0]
    page.update(status="pending_ocr", reasons=["table_layout"], ocr_required=True, text=page["native_text"])
    page.pop("native_table", None)
    page.pop("native_grid_attempt", None)
    restored = parse(path, digest, len(content), manifest=manifest)
    assert restored["quality"]["complete"] and "| HS Code | Year 2027 |" in restored["pages"][0]["text"]
    verified = deepcopy(restored)
    verified["pages"][0].update(
        status="ocr_complete", text="Previously verified table.", ocr={"uncertain_spans": []}
    )
    assert (
        parse(path, digest, len(content), manifest=verified)["pages"][0]["text"]
        == "Previously verified table."
    )
