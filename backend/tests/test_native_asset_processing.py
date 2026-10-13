"""Native image checkpoints preserve source identity and cannot publish legal text."""

import base64
import hashlib
import io
import json
from contextlib import closing
from pathlib import Path

import pytest
from PIL import Image

from app.ingestion_quality import ocr_pending, ocr_work_pending, text_complete
from app.service import Service
from test_docx_parsing import _package, _p


def image_document():
    image = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(image, format="PNG")
    return _package(
        _p("Original native text")
        + '<w:p><w:r><w:drawing><x:blip xmlns:x="urn:picture" r:embed="pic"/></w:drawing></w:r></w:p>',
        parts={"word/media/picture.png": image.getvalue()},
        relations=[("pic", "image", "media/picture.png", None)],
    )


@pytest.fixture
def service(tmp_path, monkeypatch):
    from app import ingestion

    service = Service(tmp_path)
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users(id,name,is_demo,created_at) VALUES('owner','owner',0,'2026-10-13')")
    monkeypatch.setattr(ingestion, "embed_texts", lambda *_: pytest.fail("Unverified Word text was embedded"))
    return service


def store(service, level="primary"):
    content = image_document()
    with service.db.transaction() as conn:
        blob = service.archive.blobs.put_bytes("owner", content)
        doc = service.archive.store_original(
            conn,
            "owner",
            blob,
            "law.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "https://example.org/law.docx",
            collection="country:ID",
            level=level,
            tags=[],
        )
    return dict(doc)


def saved(service, identifier):
    with closing(service.db.connect()) as conn:
        return json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (identifier,)).fetchone()[0]
        )


def transcript():
    return {
        "text": "All visible printed text in the embedded figure.",
        "complete": True,
        "is_blank": False,
        "uncertain_spans": [],
        "model": "verified-test-provider",
        "requested_model": "test-model",
        "prompt_version": "test-image-v1",
        "review_status": "unreviewed",
        "finish_reason": "stop",
    }


def test_disabled_native_images_are_mapped_and_never_faked_as_pdf_pages(service, monkeypatch):
    from app import native_asset_ocr, vision

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: False)
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: pytest.fail("Unconfigured image model called"))
    doc = store(service)
    service.collection.process_item(service.collection.claim_processing())
    manifest = saved(service, doc["id"])
    assert ocr_work_pending(manifest) and not ocr_pending(manifest)
    assert native_asset_ocr.pending_assets(manifest)
    assert "pages" not in manifest and not text_complete(manifest)
    assert hashlib.sha256(Path(doc["storage_path"]).read_bytes()).hexdigest() == doc["content_hash"]
    assert service.collection.claim_processing() is None


@pytest.mark.parametrize("level", ["primary", "secondary"])
def test_native_pixels_and_attempt_are_durable_before_model_and_resume_reuses_checkpoint(
    service, monkeypatch, level
):
    from app import native_asset_ocr, parsing, vision

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    doc = store(service, level)
    calls = []

    def recognize(rendered):
        manifest = saved(service, doc["id"])
        asset = native_asset_ocr.assets(manifest)[0]
        assert asset["ocr"]["attempts"] == 1
        reference = asset["ocr"]["image_blob"]
        sent = base64.b64decode(rendered["image_base64"], validate=True)
        assert (service.db.data_dir / reference["storage_path"]).read_bytes() == sent
        assert reference["content_hash"] == hashlib.sha256(sent).hexdigest()
        assert rendered["source_hash"] == doc["content_hash"]
        assert "page" not in rendered and "printed_page" not in rendered
        calls.append(rendered)
        return transcript()

    monkeypatch.setattr(vision, "transcribe_image", recognize)
    service.collection.process_item(service.collection.claim_processing())
    manifest = saved(service, doc["id"])
    asset = native_asset_ocr.assets(manifest)[0]
    assert len(calls) == 1 and asset["asset_status"] == "ocr_complete"
    assert not native_asset_ocr.pending_assets(manifest)
    assert "unresolved_document_object" in manifest["quality"]["review_reasons"]
    assert not text_complete(manifest)
    assert any("All visible printed text" in item["content_text"] for item in manifest["elements"])
    before = json.dumps(manifest["chunks"], sort_keys=True)
    monkeypatch.setattr(parsing, "parse_bounded", lambda *_a, **_k: pytest.fail("Original parsed twice"))
    with service.db.transaction() as conn:
        service.collection.queue_processing(conn, doc["id"], "owner")
    service.collection.process_item(service.collection.claim_processing())
    assert len(calls) == 1
    assert json.dumps(saved(service, doc["id"])["chunks"], sort_keys=True) == before
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 0


def test_stale_native_worker_cannot_commit_transcription(service, monkeypatch):
    from app import native_asset_ocr, vision

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    doc = store(service)

    def replaced(rendered):
        with service.db.transaction() as conn:
            conn.execute(
                "UPDATE processing_jobs SET lease_token='replacement' WHERE document_id=?", (doc["id"],)
            )
        return transcript()

    monkeypatch.setattr(vision, "transcribe_image", replaced)
    service.collection.process_item(service.collection.claim_processing())
    asset = native_asset_ocr.assets(saved(service, doc["id"]))[0]
    assert asset["asset_status"] == "pending_vision" and asset["ocr"]["attempts"] == 1
    assert "text" not in asset["ocr"]


def test_explicit_retry_resets_failed_asset_without_new_legal_version(service, monkeypatch):
    from app import native_asset_ocr, vision

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    doc = store(service)

    def failed(_):
        raise ValueError("Malformed image provider response")

    monkeypatch.setattr(vision, "transcribe_image", failed)
    service.collection.process_item(service.collection.claim_processing())
    assert native_asset_ocr.assets(saved(service, doc["id"]))[0]["asset_status"] == "ocr_failed"
    with service.db.transaction() as conn:
        service.collection.queue_processing(conn, doc["id"], "owner", retry_ocr=True)
    monkeypatch.setattr(vision, "transcribe_image", lambda _: transcript())
    service.collection.process_item(service.collection.claim_processing())
    asset = native_asset_ocr.assets(saved(service, doc["id"]))[0]
    assert asset["asset_status"] == "ocr_complete" and asset["ocr"]["attempts"] == 1
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    assert hashlib.sha256(Path(doc["storage_path"]).read_bytes()).hexdigest() == doc["content_hash"]
