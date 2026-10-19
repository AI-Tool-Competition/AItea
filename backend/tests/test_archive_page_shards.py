"""Native page archives preserve immutable evidence and restart without OCR."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from app import archive_page_shards as shards
from app.blob_store import BlobStore
from app.ingestion_quality import text_complete
from test_archive_parsing import pdf_file


def source(tmp_path, count=5):
    path, digest, size = pdf_file(tmp_path, [f"Physical page {number}." for number in range(1, count + 1)])
    return shards.ArchivePageSource(path, "immutable-law", 3, "system-test-owner", digest, size, count)


def page(original, number, *, pending=False):
    text = "" if pending else f"Saved physical page {number}."
    return {
        "page": number,
        "source_hash": original.content_hash,
        "native_text": text,
        "text": text,
        "status": "pending_parse" if pending else "pending_ocr",
        "printed_page": str(number + 5),
        "ocr_required": not pending,
        "reasons": [] if pending else ["table_layout"],
    }


def checkpoint(tmp_path, original, saved=2):
    manifest = {
        "source_hash": original.content_hash,
        "archive_pdf": {"version": 1, "source_size": original.size, "total_pages": original.total_pages},
        "pages": [
            page(original, number, pending=number > saved) for number in range(1, original.total_pages + 1)
        ],
        "historical_proof": {"do_not_drop": "entire original checkpoint"},
        "chunks": [{"id": "chunk-000000", "text": "Never index local window chunks."}],
    }
    manifest["pages"][0]["ocr"] = {
        "attempts": 4,
        "history": [{"model": "old-model", "text": "previous result"}],
    }
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return path, manifest


def fake_windows(monkeypatch, original, calls, *, delay=0, body_size=0):
    monkeypatch.setattr(
        shards,
        "inspect_archive_pdf_bounded",
        lambda path, *, source_hash, size: {
            "source_hash": source_hash,
            "source_size": size,
            "total_pages": original.total_pages,
        },
    )

    def extract(path, *, source_hash, size, total_pages, first_page, max_pages):
        assert path == original.path and source_hash == original.content_hash
        assert size == original.size and total_pages == original.total_pages
        numbers = list(range(first_page, min(total_pages + 1, first_page + max_pages)))
        calls.extend(numbers)
        started = time.monotonic_ns()
        if delay:
            time.sleep(delay * (2 if first_page % 2 else 1))
        pages = [page(original, number) for number in numbers]
        if body_size:
            for value in pages:
                value.update(text="a" * body_size, native_text="a" * body_size)
        return {
            "source_hash": source_hash,
            "source_size": size,
            "total_pages": total_pages,
            "pages": pages,
            "derived_status": "not_generated",
            "index_status": "not_generated",
            "worker_stats": {
                "pid": 1,
                "started_monotonic_ns": started,
                "finished_monotonic_ns": time.monotonic_ns(),
            },
        }

    monkeypatch.setattr(shards, "parse_archive_pdf_pages_bounded", extract)


def test_legacy_bytes_and_all_page_history_survive_resume_without_reparsing(tmp_path, monkeypatch):
    original = source(tmp_path)
    old, manifest = checkpoint(tmp_path, original)
    old_bytes = old.read_bytes()
    calls = []
    fake_windows(monkeypatch, original, calls)
    output = tmp_path / "archive"
    first = shards.extract_native_page_shards(original, output, checkpoint=old, window_pages=2, max_windows=1)
    assert first["mapped_pages"] == 4 and first["pending_pages"] == 1
    second = shards.extract_native_page_shards(original, output, checkpoint=old, window_pages=2)
    assert calls == [3, 4, 5]
    assert second["newly_extracted_pages"] == 1 and second["native_mapping_complete"]
    root = json.loads((output / "root.json").read_bytes())
    assert (
        shards._read_blob(output, original.owner_id, root["legacy_checkpoint"], shards.MAX_CHECKPOINT_BYTES)
        == old_bytes
    )
    saved = json.loads(
        shards._read_blob(output, original.owner_id, root["segments"][0], shards.MAX_SEGMENT_BYTES)
    )
    assert saved["pages"] == manifest["pages"][:2]
    assert old.read_bytes() == old_bytes
    assert root["segments"][-1]["first_page"] == root["segments"][-1]["last_page"] == 5
    assert "chunks" not in root and "elements" not in root
    assert not text_complete(root) and not root["text_complete"]
    assert root["derived_status"] == root["index_status"] == "not_generated"
    again = shards.extract_native_page_shards(original, output, checkpoint=old)
    assert again["scheduled_windows"] == 0 and calls == [3, 4, 5]


def test_interruption_after_durable_commit_does_not_reextract_committed_pages(tmp_path, monkeypatch):
    original = source(tmp_path, 3)
    calls = []
    fake_windows(monkeypatch, original, calls)
    output = tmp_path / "archive"

    def interrupt(_progress):
        raise RuntimeError("simulated process interruption after commit")

    with pytest.raises(RuntimeError, match="interruption"):
        shards.extract_native_page_shards(
            original, output, window_pages=1, max_windows=1, on_commit=interrupt
        )
    assert json.loads((output / "root.json").read_bytes())["mapped_pages"] == 1
    result = shards.extract_native_page_shards(original, output, window_pages=1)
    assert result["native_mapping_complete"] and calls == [1, 2, 3]


@pytest.mark.parametrize(
    "change", [{"version": 4}, {"document_id": "other"}, {"owner_id": "other"}, {"content_hash": "0" * 64}]
)
def test_resume_rejects_source_version_owner_and_hash_mismatch(tmp_path, monkeypatch, change):
    original = source(tmp_path)
    calls = []
    fake_windows(monkeypatch, original, calls)
    output = tmp_path / "archive"
    shards.extract_native_page_shards(original, output, max_windows=0)
    with pytest.raises(ValueError, match="binding|hash"):
        shards.extract_native_page_shards(replace(original, **change), output)
    assert calls == []


def test_changed_checkpoint_bytes_rejected_even_if_json_has_same_meaning(tmp_path, monkeypatch):
    original = source(tmp_path)
    old, manifest = checkpoint(tmp_path, original)
    calls = []
    fake_windows(monkeypatch, original, calls)
    output = tmp_path / "archive"
    shards.extract_native_page_shards(original, output, checkpoint=old, max_windows=0)
    old.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="bytes changed"):
        shards.extract_native_page_shards(original, output, checkpoint=old)
    assert calls == []


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_corrupt_segment_fails_before_new_extraction(tmp_path, monkeypatch, damage):
    original = source(tmp_path)
    calls = []
    fake_windows(monkeypatch, original, calls)
    output = tmp_path / "archive"
    shards.extract_native_page_shards(original, output, window_pages=1, max_windows=1)
    root = json.loads((output / "root.json").read_bytes())
    path = output / root["segments"][0]["cas_path"]
    if damage == "missing":
        path.unlink()
    else:
        path.chmod(0o600)
        path.write_bytes(b"corrupt")
    with pytest.raises((ValueError, FileNotFoundError)):
        shards.extract_native_page_shards(original, output)
    assert calls == [1]


def test_nested_legacy_image_and_response_proof_copied_without_rewriting_page(tmp_path, monkeypatch):
    original = source(tmp_path)
    old, manifest = checkpoint(tmp_path, original)
    evidence_root = tmp_path / "source-data"
    blobs = BlobStore(evidence_root / "files")
    image = blobs.put_bytes(original.owner_id, b"old image evidence")
    response = blobs.put_bytes(original.owner_id, b'{"old_response":"preserve"}')
    first = manifest["pages"][0]
    first["ocr"].update(
        image_blob={"path": str(image.path), "content_hash": image.digest},
        error_response={"blob": {"path": str(response.path), "content_hash": response.digest}},
        previous_page={
            "ocr": {"attempts": 2, "image_blob": {"path": str(image.path), "content_hash": image.digest}}
        },
    )
    old.write_text(json.dumps(manifest, indent=2))
    calls = []
    fake_windows(monkeypatch, original, calls)
    output = tmp_path / "archive"
    shards.extract_native_page_shards(
        original, output, checkpoint=old, evidence_root=evidence_root, max_windows=0
    )
    root = json.loads((output / "root.json").read_bytes())
    assert len(root["legacy_dependencies"]) == 3
    for dependency in root["legacy_dependencies"]:
        assert dependency["page"] == 1 and dependency["source_hash"] == original.content_hash
        assert (
            hashlib.sha256((output / dependency["blob"]["cas_path"]).read_bytes()).hexdigest()
            == dependency["blob"]["sha256"]
        )
    image.path.unlink()
    response.path.unlink()
    resumed = shards.extract_native_page_shards(original, output, max_windows=0)
    assert resumed["mapped_pages"] == 2 and not calls
    migrated = tmp_path / "migrated-archive"
    shutil.copytree(output, migrated)
    relocated_source = replace(original, path=migrated / root["original"]["cas_path"])
    original.path.unlink()
    relocated = shards.extract_native_page_shards(relocated_source, migrated, max_windows=0)
    assert relocated["mapped_pages"] == 2 and not calls
    root["legacy_dependencies"][0]["page"] = 2
    (output / "root.json").write_text(json.dumps(root))
    with pytest.raises(ValueError, match="mapping changed"):
        shards.extract_native_page_shards(relocated_source, output, max_windows=0)


def test_parallel_nonoverlapping_windows_have_ordered_monotonic_commits(tmp_path, monkeypatch):
    original = source(tmp_path, 7)
    calls, counts = [], []
    fake_windows(monkeypatch, original, calls, delay=0.02)
    output = tmp_path / "archive"
    report = shards.extract_native_page_shards(
        original,
        output,
        workers=3,
        window_pages=2,
        on_commit=lambda value: counts.append(value["mapped_pages"]),
    )
    root = json.loads((output / "root.json").read_bytes())
    assert sorted(calls) == list(range(1, 8)) and len(calls) == 7
    assert counts == sorted(set(counts)) and counts[-1] == 7
    assert [segment["first_page"] for segment in root["segments"]] == [1, 3, 5, 7]
    assert report["measured_peak_worker_intervals"] == 3


def test_second_coordinator_rejected_while_first_holds_archive_lock(tmp_path, monkeypatch):
    original = source(tmp_path, 1)
    entered, release = threading.Event(), threading.Event()
    calls = []
    fake_windows(monkeypatch, original, calls)

    def hold(_value):
        entered.set()
        assert release.wait(5)

    output = tmp_path / "archive"
    errors = []

    def run():
        try:
            shards.extract_native_page_shards(original, output, on_commit=hold)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(5)
        with pytest.raises(ValueError, match="active coordinator"):
            shards.extract_native_page_shards(original, output)
    finally:
        release.set()
        thread.join(5)
    assert not errors and not thread.is_alive()


def test_aggregate_above_four_million_characters_uses_bounded_segments(tmp_path, monkeypatch):
    original = source(tmp_path, 3)
    calls = []
    fake_windows(monkeypatch, original, calls, body_size=2_000_001)
    output = tmp_path / "archive"
    report = shards.extract_native_page_shards(original, output, window_pages=1)
    assert report["mapped_pages"] == 3 and report["native_mapping_complete"]
    assert report["text_complete"] is False
    root = json.loads((output / "root.json").read_bytes())
    assert len(root["segments"]) == 3
    assert max(segment["size"] for segment in root["segments"]) < shards.MAX_SEGMENT_BYTES


def test_worker_may_not_return_local_chunks_for_indexing(tmp_path, monkeypatch):
    original = source(tmp_path, 1)
    calls = []
    fake_windows(monkeypatch, original, calls)
    native = shards.parse_archive_pdf_pages_bounded

    def with_chunks(*args, **kwargs):
        return {**native(*args, **kwargs), "chunks": [{"id": "chunk-000000"}]}

    monkeypatch.setattr(shards, "parse_archive_pdf_pages_bounded", with_chunks)
    with pytest.raises(ValueError, match="invalid absolute window"):
        shards.extract_native_page_shards(original, tmp_path / "archive")


def test_real_bounded_window_records_absolute_last_page_without_full_assembly(tmp_path):
    from app.archive_parsing import parse_archive_pdf_pages_bounded

    original = source(tmp_path, 65)
    result = parse_archive_pdf_pages_bounded(
        original.path, source_hash=original.content_hash, size=original.size, total_pages=65, first_page=65
    )
    assert [value["page"] for value in result["pages"]] == [65]
    assert "Physical page 65." in result["pages"][0]["native_text"]
    assert result["pages"][0]["native_visibility"]["page"] == 65
    assert result["worker_stats"]["finished_monotonic_ns"] > result["worker_stats"]["started_monotonic_ns"]
    assert not text_complete(result) and "chunks" not in result and "elements" not in result


def test_explicit_window_does_not_extract_any_earlier_page(tmp_path, monkeypatch):
    from pypdf._page import PageObject
    from app.archive_parsing import _parse_archive_page_window

    original = source(tmp_path, 4)
    native, extracted = PageObject.extract_text, []

    def observe(page, *args, **kwargs):
        result = native(page, *args, **kwargs)
        extracted.append(result.strip())
        return result

    monkeypatch.setattr(PageObject, "extract_text", observe)
    result = _parse_archive_page_window(
        str(original.path), original.content_hash, original.size, 4, 3, 64, 10000
    )
    assert [value["page"] for value in result["pages"]] == [3, 4]
    assert extracted == ["Physical page 3.", "Physical page 4."]


@pytest.mark.parametrize(
    "changes", [{"total_pages": 4}, {"source_hash": "0" * 64}, {"first_page": 0}, {"max_pages": 65}]
)
def test_explicit_window_rejects_bad_original_and_range(tmp_path, changes):
    from app.archive_parsing import parse_archive_pdf_pages_bounded

    original = source(tmp_path, 3)
    arguments = {
        "source_hash": original.content_hash,
        "size": original.size,
        "total_pages": 3,
        "first_page": 1,
        **changes,
    }
    with pytest.raises(ValueError):
        parse_archive_pdf_pages_bounded(original.path, **arguments)


def test_cli_creates_pointer_and_resumes_using_readonly_source_database(tmp_path):
    original = replace(source(tmp_path, 3), document_id="cli-native-" + uuid4().hex)
    database = tmp_path / "tea.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE documents(id,version,owner_id,content_hash,size,storage_path,mime_type)")
        conn.execute(
            "INSERT INTO documents VALUES(?,?,?,?,?,?,?)",
            (
                original.document_id,
                original.version,
                original.owner_id,
                original.content_hash,
                original.size,
                str(original.path),
                "application/pdf",
            ),
        )
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    project = Path(__file__).resolve().parents[2]
    output = tmp_path / "archive"
    arguments = [
        sys.executable,
        str(project / "scripts/extract_archive_page_shards.py"),
        "--source-db",
        str(database),
        "--document-id",
        original.document_id,
        "--total-pages",
        "3",
        "--output",
        str(output),
        "--storage-root",
        str(tmp_path),
        "--window-pages",
        "2",
        "--workers",
        "2",
    ]
    filename = (
        hashlib.sha256(f"{original.document_id}:{original.version}".encode()).hexdigest()[:20] + ".json"
    )
    pointer = project / ".dev/archive-page-shards" / filename
    try:
        subprocess.run(
            [*arguments, "--max-windows", "1"],
            check=True,
            capture_output=True,
            timeout=60,
            env=os.environ.copy(),
        )
        assert json.loads((output / "last-run.json").read_bytes())["mapped_pages"] == 2
        assert json.loads(pointer.read_bytes())["output"] == str(output)
        subprocess.run(arguments, check=True, capture_output=True, timeout=60, env=os.environ.copy())
        report = json.loads((output / "last-run.json").read_bytes())
        assert report["native_mapping_complete"] and report["newly_extracted_pages"] == 1
        assert report["model_calls"] == 0 and report["text_complete"] is False
        assert len(list(output.glob("run-*.json"))) == 2
        assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    finally:
        pointer.unlink(missing_ok=True)


@pytest.mark.parametrize(
    "attack",
    ["ready", "overlap", "coverage", "other_owner_path", "traversal", "legacy_omitted", "huge_range"],
)
def test_malformed_root_is_rejected_before_new_native_extraction(tmp_path, monkeypatch, attack):
    original = source(tmp_path)
    old, _ = checkpoint(tmp_path, original)
    calls = []
    fake_windows(monkeypatch, original, calls)
    output = tmp_path / "archive"
    shards.extract_native_page_shards(original, output, checkpoint=old, max_windows=0)
    path = output / "root.json"
    root = json.loads(path.read_bytes())
    if attack == "ready":
        root["text_complete"] = True
    elif attack == "overlap":
        root["segments"].append(root["segments"][0].copy())
    elif attack == "coverage":
        root["pending_pages"] = []
    elif attack == "other_owner_path":
        root["segments"][0]["cas_path"] = root["segments"][0]["cas_path"].replace(
            hashlib.sha256(original.owner_id.encode()).hexdigest(), "0" * 64
        )
    elif attack == "traversal":
        root["segments"][0]["cas_path"] = "../outside-evidence"
    elif attack == "huge_range":
        root["segments"][0]["last_page"] = 10**12
    else:
        root["segments"] = []
        shards._summary(root)
    path.write_text(json.dumps(root))
    with pytest.raises(ValueError):
        shards.extract_native_page_shards(original, output, checkpoint=old)
    assert not calls


def test_actual_page_count_checked_even_when_all_legacy_pages_appear_saved(tmp_path):
    original = source(tmp_path, 3)
    truncated = replace(original, total_pages=2)
    old, _ = checkpoint(tmp_path, truncated, saved=2)
    with pytest.raises(ValueError, match="actual original page count"):
        shards.extract_native_page_shards(truncated, tmp_path / "archive", checkpoint=old, max_windows=0)


@pytest.mark.parametrize("location", ["ocr", "native_visibility", "error_response"])
def test_old_current_evidence_cannot_move_to_another_physical_page(tmp_path, monkeypatch, location):
    original = source(tmp_path)
    old, manifest = checkpoint(tmp_path, original)
    if location == "error_response":
        manifest["pages"][0]["ocr"]["error_response"] = {"source_hash": original.content_hash, "page": 2}
    else:
        manifest["pages"][0][location] = {"source_hash": original.content_hash, "page": 2}
    old.write_text(json.dumps(manifest))
    calls = []
    fake_windows(monkeypatch, original, calls)
    with pytest.raises(ValueError, match="physical-page binding"):
        shards.extract_native_page_shards(original, tmp_path / "archive", checkpoint=old)
    assert not calls


def test_old_nested_evidence_cannot_read_another_owner_blob(tmp_path, monkeypatch):
    original = source(tmp_path)
    old, manifest = checkpoint(tmp_path, original)
    evidence_root = tmp_path / "source-data"
    blob = BlobStore(evidence_root / "files").put_bytes("another-owner", b"private pixels")
    manifest["pages"][0]["ocr"]["image_blob"] = {"path": str(blob.path), "content_hash": blob.digest}
    old.write_text(json.dumps(manifest))
    calls = []
    fake_windows(monkeypatch, original, calls)
    with pytest.raises(ValueError, match="outside the original owner CAS"):
        shards.extract_native_page_shards(
            original, tmp_path / "archive", checkpoint=old, evidence_root=evidence_root
        )
    assert not calls


def test_metadata_inspection_never_extracts_native_text(tmp_path, monkeypatch):
    from app.archive_parsing import _inspect_archive_pdf
    from pypdf._page import PageObject

    original = source(tmp_path, 3)
    monkeypatch.setattr(
        PageObject,
        "extract_text",
        lambda *args, **kwargs: pytest.fail("metadata inspection extracted an old page"),
    )
    result = _inspect_archive_pdf(str(original.path), original.content_hash, original.size)
    assert result == {"source_hash": original.content_hash, "source_size": original.size, "total_pages": 3}
