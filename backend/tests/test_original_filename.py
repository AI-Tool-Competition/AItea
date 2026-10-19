"""Long display names preserve parser format and existing document families."""

import json
from pathlib import Path

import pytest

from app.archive import family_key, sanitize_filename
from app.service import Service
from test_archive_parsing import pdf_file


@pytest.fixture
def service(tmp_path):
    instance = Service(tmp_path / "library")
    with instance.db.transaction() as conn:
        conn.execute("INSERT INTO users(id,name,is_demo,created_at) VALUES('owner','owner',0,'2026-10-19')")
    return instance


def test_long_pdf_upload_parses_real_pages_and_retains_version_family(service, tmp_path):
    path, digest, _ = pdf_file(tmp_path, ["Tea source law."])
    content = path.read_bytes()
    unsafe_name = "C:\\folder\\" + "Legal original " * 30 + "\x01.pdf"
    cleaned = "Legal original " * 30 + ".pdf"
    first = service.store_document("owner", unsafe_name, content, "application/pdf")
    assert first["filename"].endswith(".pdf") and len(first["filename"]) == 180
    assert "\\" not in first["filename"] and "\x01" not in first["filename"]
    assert first["status"] == "ready"
    with service.db.connect() as conn:
        original = dict(conn.execute("SELECT * FROM documents WHERE id=?", (first["id"],)).fetchone())
    manifest = json.loads(original["manifest"])
    assert manifest["pages"][0]["source_hash"] == digest
    assert "Tea source law." in manifest["pages"][0]["native_text"]
    assert original["family_id"] == family_key("owner", cleaned[:180])
    repeat = service.store_document("owner", unsafe_name, content, "application/pdf")
    assert repeat["id"] == first["id"] and repeat["version"] == 1
    path, _, _ = pdf_file(tmp_path, ["Changed tea source law."])
    second = service.store_document("owner", unsafe_name, path.read_bytes(), "application/pdf")
    assert second["version"] == 2 and second["id"] != first["id"]
    with service.db.connect() as conn:
        assert dict(conn.execute("SELECT * FROM documents WHERE id=?", (first["id"],)).fetchone()) == original
        assert (
            conn.execute("SELECT family_id FROM documents WHERE id=?", (second["id"],)).fetchone()[0]
            == original["family_id"]
        )
    assert Path(original["storage_path"]).read_bytes() == content


def test_archive_long_original_is_parsed_by_normal_processing_worker(service, tmp_path):
    path, digest, _ = pdf_file(tmp_path, ["Official acquired law."])
    name = "Official title " * 40 + ".pdf"
    blob = service.archive.blobs.put_bytes("owner", path.read_bytes())
    with service.db.transaction() as conn:
        doc = service.archive.store_original(
            conn,
            "owner",
            blob,
            name,
            "application/pdf",
            "https://example.org/official.pdf",
            collection="country:SG",
            level="primary",
            tags=[],
        )
    assert len(doc["filename"]) == 180 and doc["filename"].endswith(".pdf")
    claim = service.collection.claim_processing(owner_id="owner", document_ids=[doc["id"]])
    assert claim
    service.collection.process_item(claim)
    with service.db.connect() as conn:
        saved = conn.execute("SELECT status,manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()
    assert saved["status"] == "ready"
    manifest = json.loads(saved["manifest"])
    assert manifest["pages"][0]["page"] == 1 and manifest["pages"][0]["source_hash"] == digest
    assert "Official acquired law." in manifest["pages"][0]["native_text"]


def test_existing_long_attachment_identity_and_filename_are_not_changed(service):
    parent = service.store_document("owner", "parent.txt", b"Primary source text.")
    name = "a" * 240 + ".pdf"
    old = service.store_document(
        "owner", name[:180], b"historical original", role="secondary", parent_id=parent["id"]
    )
    with service.db.connect() as conn:
        before = dict(conn.execute("SELECT * FROM documents WHERE id=?", (old["id"],)).fetchone())
    repeated = service.store_document(
        "owner", name, b"historical original", role="secondary", parent_id=parent["id"]
    )
    assert repeated["id"] == old["id"] and repeated["filename"] == name[:180]
    with service.db.connect() as conn:
        assert dict(conn.execute("SELECT * FROM documents WHERE id=?", (old["id"],)).fetchone()) == before


@pytest.mark.parametrize("suffix", [".txt", ".docx", ".HTML", ".xlsx", ".wpf", "", "." + "a" * 190])
def test_filename_limit_cleanup_and_supported_suffixes(suffix):
    name = "相同来源" * 60 + suffix
    display, legacy = sanitize_filename("/unsafe/" + name, fallback="source.bin")
    assert len(display) <= 180 and legacy == name[:180]
    if suffix and len(suffix) < 180:
        assert display.endswith(suffix)
    assert "/" not in display
    assert sanitize_filename("\x00\x01", fallback="source.bin") == ("source.bin", "source.bin")
