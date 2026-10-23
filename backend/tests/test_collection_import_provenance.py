"""Processing keeps import identity without authorizing older snapshot overwrite."""

import base64
import hashlib
import io
import json
from contextlib import closing
from copy import deepcopy

import pytest
from PIL import Image

from app.collection_import import CollectionImportError, import_collected_resources
from app.db import dump, now
from app.parsing import ProcessingDeferred, parse_bounded
from app.service import Service
from test_native_asset_processing import image_document, transcript


@pytest.fixture
def pair(tmp_path, monkeypatch):
    for key in ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    source, target = Service(tmp_path / "source"), Service(tmp_path / "target")
    for service, owner in ((source, "source"), (target, "target")):
        with service.db.transaction() as conn:
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 0, now()))
    return source, target


def imported(pair, *, filename="law.pdf", content=b"%PDF-provenance-fixture", initial=None):
    source, target = pair
    with source.db.transaction() as conn:
        blob = source.archive.blobs.put_bytes("source", content)
        row = source.archive.store_original(
            conn,
            "source",
            blob,
            filename,
            "application/pdf"
            if filename.endswith(".pdf")
            else "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "https://example.org/" + filename,
            collection="country:ID",
            level="primary",
            tags=[],
        )
        manifest = (
            deepcopy(initial)
            if initial is not None
            else {
                "source_hash": row["content_hash"],
                "elements": [],
                "chunks": [],
                "quality": {"complete": False},
            }
        )
        manifest["source_hash"] = row["content_hash"]
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), row["id"]))
    result = import_collected_resources(
        source.db.data_dir, target.db.data_dir, "source", "target", apply=True
    )
    identifier = result["document_versions"][row["id"]]["document_id"]
    with closing(target.db.connect()) as conn:
        doc = dict(target.own_document(conn, "target", identifier))
    origin = json.loads(doc["manifest"])["collection_import"]
    return source, target, doc, deepcopy(origin)


def saved(service, identifier):
    with closing(service.db.connect()) as conn:
        return json.loads(service.own_document(conn, "target", identifier)["manifest"])


def pdf_manifest(doc, *, complete=False, archive=False):
    page = {
        "page": 1,
        "source_hash": doc["content_hash"],
        "native_text": "",
        "text": "Paid OCR text" if complete else "",
        "status": "ocr_complete" if complete else "pending_ocr",
    }
    if complete:
        page["ocr"] = {"attempts": 1, "complete": True, "uncertain_spans": [], "validation_warnings": []}
    result = {
        "parser": "pdf-basic",
        "pipeline_version": "archive-pdf-pages-v3" if archive else "pdf-pages-v4",
        "source_hash": doc["content_hash"],
        "pages": [page],
        "elements": [{"element_key": "page-1", "content_text": page["text"]}] if complete else [],
        "chunks": [{"id": "page-1", "text": page["text"], "locator": {"page": 1}}] if complete else [],
        "quality": {"complete": complete, "pending_pages": [] if complete else [1]},
    }
    if archive:
        result["archive_pdf"] = {"version": 1, "source_size": doc["size"], "total_pages": 1}
    return result


@pytest.mark.parametrize("archive,interrupt", [(False, False), (True, False), (False, True)])
def test_fresh_and_archive_PDF_keep_origin_before_every_checkpoint_and_after_rebuild(
    pair, monkeypatch, archive, interrupt
):
    from app import archive_parsing, collection, parsing, vision

    initial = {"archive_pdf": {"version": 1}} if archive else None
    source, target, doc, origin = imported(pair, initial=initial)
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)

    def parse(*_args, **kwargs):
        if archive:
            assert kwargs.get("max_pages") == collection._ARCHIVE_PDF_PARSE_WINDOW_PAGES
        if archive and kwargs.get("manifest"):
            kwargs["manifest"]["collection_import"]["guard"] = "parser-mutated-guard"
        return pdf_manifest(doc, archive=archive)

    monkeypatch.setattr(parsing, "parse_bounded", parse)
    monkeypatch.setattr(archive_parsing, "parse_archive_pdf_bounded", parse)
    buffer = io.BytesIO()
    Image.new("RGB", (2, 3), "white").save(buffer, format="PNG")
    png = buffer.getvalue()
    checkpoints = []

    def ocr(_filename, _content, _mime, manifest, *, on_image, on_page, **_kwargs):
        assert _kwargs.get("max_pages") == collection._COLLECTION_OCR_BATCH_PAGES
        assert manifest["collection_import"] == origin
        reference = on_image(
            {
                "source_hash": doc["content_hash"],
                "page": 1,
                "image_base64": base64.b64encode(png).decode(),
                "image_sha256": hashlib.sha256(png).hexdigest(),
                "mime_type": "image/png",
                "width": 2,
                "height": 3,
            }
        )
        assert saved(target, doc["id"])["collection_import"] == origin
        page = deepcopy(manifest["pages"][0])
        page["ocr"] = {"attempts": 1, "image_blob": reference}
        on_page(page)
        checkpoints.append(saved(target, doc["id"]))
        assert checkpoints[-1]["collection_import"] == origin
        if interrupt:
            raise ProcessingDeferred("Persisted request intent retained")
        result = pdf_manifest(doc, complete=True, archive=archive)
        result["pages"][0]["ocr"]["image_blob"] = reference
        on_page(result["pages"][0])
        checkpoints.append(saved(target, doc["id"]))
        assert checkpoints[-1]["collection_import"] == origin
        # A freshly reassembled manifest must not replace trusted stored origin.
        result["collection_import"] = {"guard": "parser-generated-guard"}
        return result

    monkeypatch.setattr(parsing, "ocr_bounded", ocr)
    claim = target.collection.claim_processing(owner_id="target", document_ids=[doc["id"]])
    target.collection.process_item(claim)
    current = saved(target, doc["id"])
    assert current["collection_import"] == origin
    assert current["collection_import"]["guard"] == origin["guard"]
    assert current["pages"][0]["ocr"]["attempts"] == 1
    assert current["pages"][0]["status"] == ("pending_ocr" if interrupt else "ocr_complete")
    assert len(checkpoints) == (1 if interrupt else 2)
    with closing(target.db.connect()) as conn:
        metadata = json.loads(
            conn.execute(
                "SELECT payload FROM document_metadata WHERE document_id=?", (doc["id"],)
            ).fetchone()[0]
        )
        assert metadata["review_status"] == "draft" and metadata["effective_from"] is None
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    with pytest.raises(CollectionImportError, match="target_changed_since_import"):
        import_collected_resources(
            source.db.data_dir, target.db.data_dir, "source", "target", apply=True, refresh=True
        )
    assert saved(target, doc["id"])["collection_import"] == origin
    assert target.search("target", "Paid OCR", country="ID")["items"] == []


@pytest.mark.parametrize("mode", ["fresh", "cached", "archive"])
def test_native_asset_rebuild_and_image_request_keep_original_import_guard(pair, monkeypatch, mode):
    from app import native_asset_ocr, vision

    content = image_document()
    initial = {"archive_docx": {"version": 1}} if mode == "archive" else None
    source, target, doc, origin = imported(pair, filename="law.docx", content=content, initial=initial)
    if mode == "cached":
        # This is a prior native checkpoint of the already imported original,
        # not a separate cross-account DOCX locator import.
        cached = parse_bounded(
            "law.docx", content, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )
        cached["collection_import"] = deepcopy(origin)
        with target.db.transaction() as conn:
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(cached), doc["id"]))
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    real_rebuild = native_asset_ocr.rebuild_native_asset_text_bounded
    rebuilt = []

    def rebuild(manifest):
        assert manifest["collection_import"] == origin
        result = real_rebuild(manifest)
        result.pop("collection_import", None)
        rebuilt.append(True)
        return result

    calls = []

    def recognize(_rendered):
        manifest = saved(target, doc["id"])
        assert manifest["collection_import"] == origin
        asset = native_asset_ocr.assets(manifest)[0]
        assert asset["ocr"]["attempts"] == 1
        calls.append(True)
        return transcript()

    monkeypatch.setattr(native_asset_ocr, "rebuild_native_asset_text_bounded", rebuild)
    monkeypatch.setattr(vision, "transcribe_image", recognize)
    target.collection.process_item(
        target.collection.claim_processing(owner_id="target", document_ids=[doc["id"]])
    )
    assert calls == [True] and rebuilt
    assert saved(target, doc["id"])["collection_import"] == origin
    assert native_asset_ocr.assets(saved(target, doc["id"]))[0]["asset_status"] == "ocr_complete"
    with pytest.raises(CollectionImportError, match="target_changed_since_import"):
        import_collected_resources(
            source.db.data_dir, target.db.data_dir, "source", "target", apply=True, refresh=True
        )
    assert target.search("target", "printed text", country="ID")["items"] == []


def test_parser_cannot_create_collection_import_or_legal_approval_for_an_unimported_document(
    pair, monkeypatch
):
    from app import parsing, vision

    _, target = pair
    with target.db.transaction() as conn:
        blob = target.archive.blobs.put_bytes("target", b"Original plain text")
        doc = target.archive.store_original(
            conn,
            "target",
            blob,
            "law.txt",
            "text/plain",
            "https://example.org/law.txt",
            collection="country:ID",
            level="primary",
            tags=[],
        )
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: False)
    monkeypatch.setattr(
        parsing,
        "parse_bounded",
        lambda *_a, **_k: {
            "elements": [{"element_key": "e1", "content_text": "Parsed text"}],
            "chunks": [{"id": "c1", "text": "Parsed text", "locator": {}}],
            "collection_import": {"guard": "forged", "review_status": "published"},
        },
    )
    target.collection.process_item(
        target.collection.claim_processing(owner_id="target", document_ids=[doc["id"]])
    )
    assert "collection_import" not in saved(target, doc["id"])
    assert target.catalog.metadata_for(doc["id"])["metadata"]["review_status"] == "draft"
    assert target.search("target", "Parsed text", country="ID")["items"] == []
