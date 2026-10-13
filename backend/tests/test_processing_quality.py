"""Page checkpoints survive interruptions; incomplete legal text cannot be indexed."""

import copy
import json
from contextlib import closing

import pytest

from app.service import Service


@pytest.fixture
def service(tmp_path):
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users(id,name,is_demo,created_at) VALUES('owner','owner',0,'2026-10-13')")
    return service


def scanned():
    return {
        "elements": [],
        "chunks": [],
        "pages": [{"page": page, "status": "pending_ocr", "native_text": "", "text": ""} for page in (1, 2)],
        "quality": {"complete": False, "status": "needs_ocr", "pending_pages": [1, 2], "failed_pages": []},
    }


def store(service):
    with service.db.transaction() as conn:
        blob = service.archive.blobs.put_bytes("owner", b"scanned original")
        return dict(
            service.archive.store_original(
                conn,
                "owner",
                blob,
                "law.pdf",
                "application/pdf",
                "https://example.org/law.pdf",
                collection="country:ID",
                level="primary",
                tags=[],
            )
        )


def test_scanned_original_with_no_ocr_is_preserved_and_not_embedded(service, monkeypatch):
    from app import ingestion, parsing

    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: scanned())
    monkeypatch.setattr(ingestion, "embed_texts", lambda *_: pytest.fail("incomplete text embedded"))
    doc = store(service)
    service.collection.process_item(service.collection.claim_processing())
    detail = service.document_detail("owner", doc["id"])
    assert detail["quality"]["complete"] is False
    assert "暂不进入检索" in detail["document"]["error"]
    assert detail["document"]["status"] == "ready"
    assert service.collection.claim_processing() is None
    with closing(service.db.connect()) as conn:
        job = conn.execute(
            "SELECT status,error FROM processing_jobs WHERE document_id=?", (doc["id"],)
        ).fetchone()
    assert job["status"] == "failed" and "正文未完整" in job["error"]


def test_upload_queues_ocr_after_original_is_saved(service, monkeypatch):
    from app import parsing, vision

    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: scanned())
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    doc = service.store_document("owner", "law.pdf", b"scanned original", "application/pdf")
    assert doc["status"] == "processing"
    with closing(service.db.connect()) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM processing_jobs WHERE document_id=? AND status='queued'", (doc["id"],)
            ).fetchone()[0]
            == 1
        )


def test_page_checkpoint_survives_failure_and_resume_does_not_reparse(service, monkeypatch):
    from app import ingestion, parsing, vision

    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: scanned())
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    monkeypatch.setattr(ingestion, "embed_texts", lambda *_: pytest.fail("incomplete text embedded"))
    doc = store(service)

    def interrupted(filename, content, mime_type, manifest, *, on_page, **kwargs):
        on_page({"page": 1, "status": "ocr_complete", "native_text": "", "text": "Pasal 1. Public text."})
        raise ValueError("interrupted for test")

    monkeypatch.setattr(parsing, "ocr_bounded", interrupted)
    service.collection.process_item(service.collection.claim_processing())
    with closing(service.db.connect()) as conn:
        saved = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
    assert saved["pages"][0]["status"] == "ocr_complete"
    monkeypatch.setattr(
        parsing, "parse_bounded", lambda *a, **kw: pytest.fail("native parser repeated after checkpoint")
    )

    def resumed(filename, content, mime_type, manifest, *, on_page, **kwargs):
        assert manifest["pages"][0]["status"] == "ocr_complete"
        result = copy.deepcopy(manifest)
        result["pages"][1]["status"] = "ocr_uncertain"
        result["quality"] = {
            "complete": False,
            "status": "partial",
            "pending_pages": [],
            "failed_pages": [],
            "review_pages": [2],
        }
        return result

    monkeypatch.setattr(parsing, "ocr_bounded", resumed)
    with service.db.transaction() as conn:
        service.collection.queue_processing(conn, doc["id"], "owner")
    service.collection.process_item(service.collection.claim_processing())
    assert service.collection.claim_processing() is None
    assert service.document_detail("owner", doc["id"])["quality"]["review_pages"] == [2]


def test_rendered_image_is_saved_before_model_call_in_account_blob_store(service, monkeypatch):
    import base64
    import hashlib
    from pathlib import Path
    from app import parsing, vision

    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: scanned())
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    doc = store(service)
    image = b"test rendered PNG bytes"
    digest = hashlib.sha256(image).hexdigest()

    def recognize(filename, content, mime_type, manifest, *, on_image, on_page, **kwargs):
        reference = on_image(
            {
                "page": 1,
                "source_hash": hashlib.sha256(content).hexdigest(),
                "image_sha256": digest,
                "image_base64": base64.b64encode(image).decode(),
            }
        )
        image_path = service.db.data_dir / reference["storage_path"]
        assert image_path.read_bytes() == image
        assert image_path.name == digest
        assert image_path.parent.parent.parent.name == hashlib.sha256(b"owner").hexdigest()
        with closing(service.db.connect()) as conn:
            checkpoint = json.loads(
                conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
            )
        assert checkpoint["pages"][0]["ocr"]["image_blob"] == reference
        assert "image_base64" not in json.dumps(checkpoint)
        result = copy.deepcopy(manifest)
        result["pages"] = [{**page, "status": "ocr_failed"} for page in result["pages"]]
        result["quality"] = {"complete": False, "failed_pages": [1, 2], "pending_pages": []}
        return result

    monkeypatch.setattr(parsing, "ocr_bounded", recognize)
    service.collection.process_item(service.collection.claim_processing())
    detail = service.document_detail("owner", doc["id"])
    assert detail["quality"]["failed_pages"] == [1, 2]
    assert Path(doc["storage_path"]).read_bytes() == b"scanned original"


def test_resume_after_last_page_checkpoint_reassembles_without_ocr_or_reparse(service, monkeypatch):
    from app import parsing, vision

    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: scanned())
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    doc = store(service)

    def interrupted(filename, content, mime_type, manifest, *, on_page, **kwargs):
        for page in manifest["pages"]:
            on_page({**page, "status": "ocr_complete", "text": "Complete page"})
        raise ValueError("process stopped before assembling text")

    monkeypatch.setattr(parsing, "ocr_bounded", interrupted)
    service.collection.process_item(service.collection.claim_processing())
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: False)
    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: pytest.fail("original reparsed"))
    called = []

    def assemble(filename, content, mime_type, manifest, **kwargs):
        assert all(page["status"] == "ocr_complete" for page in manifest["pages"])
        called.append(True)
        return {
            **manifest,
            "quality": {"complete": True},
            "elements": [{"content_text": "Complete page"}],
            "chunks": [{"id": "complete", "text": "Complete page", "locator": {"page": 1}}],
        }

    monkeypatch.setattr(parsing, "ocr_bounded", assemble)
    with service.db.transaction() as conn:
        service.collection.queue_processing(conn, doc["id"], "owner")
    service.collection.process_item(service.collection.claim_processing())
    assert called == [True]
    assert service.document_detail("owner", doc["id"])["chunk_total"] == 1


def test_explicit_retry_resets_only_failed_ocr_pages(service, monkeypatch):
    from app import parsing

    manifest = scanned()
    manifest["pages"][0]["status"] = "ocr_complete"
    manifest["pages"][1]["status"] = "ocr_failed"
    manifest["quality"] = {"complete": False, "failed_pages": [2], "pending_pages": []}
    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: manifest)
    # No provider calls: reassembly simply reports this incomplete checkpoint.
    monkeypatch.setattr(parsing, "ocr_bounded", lambda filename, content, mime, manifest, **kw: manifest)
    doc = store(service)
    service.collection.process_item(service.collection.claim_processing())
    with service.db.transaction() as conn:
        service.collection.queue_processing(conn, doc["id"], "owner", retry_ocr=True)
        saved = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
    assert [page["status"] for page in saved["pages"]] == ["ocr_complete", "pending_ocr"]
    assert saved["quality"]["pending_pages"] == [2]


def test_busy_ocr_capacity_defers_without_failing_or_immediate_reclaim(service, monkeypatch):
    import time
    from app import parsing, vision

    manifest = scanned()
    manifest["quality"].update(deferred_pages=[1, 2], retry_after_seconds=5)
    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: manifest)
    monkeypatch.setattr(parsing, "ocr_bounded", lambda *a, **kw: manifest)
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    doc = store(service)
    before = time.time()
    service.collection.process_item(service.collection.claim_processing())
    with closing(service.db.connect()) as conn:
        job = conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (doc["id"],)).fetchone()
    assert job["status"] == "queued" and job["attempts"] == 0
    assert job["available_at"] >= before + 5
    assert service.collection.claim_processing() is None
    assert service.document_detail("owner", doc["id"])["quality"]["pending_pages"] == [1, 2]


def test_busy_parser_does_not_exhaust_retries_or_mark_document_failed(service, monkeypatch):
    import time
    from app import parsing

    def busy(*args, **kwargs):
        raise parsing.ProcessingDeferred("处理容量正在使用中")

    monkeypatch.setattr(parsing, "parse_bounded", busy)
    doc = store(service)
    with service.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET attempts=8 WHERE document_id=?", (doc["id"],))
    before = time.time()
    service.collection.process_item(service.collection.claim_processing())
    with closing(service.db.connect()) as conn:
        job = conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (doc["id"],)).fetchone()
        document = conn.execute("SELECT status FROM documents WHERE id=?", (doc["id"],)).fetchone()
    assert job["status"] == "queued" and job["attempts"] == 0
    assert before + 5 <= job["available_at"] <= time.time() + 6
    assert document["status"] != "failed"
    assert service.collection.claim_processing() is None


def test_smaller_embedding_batches_save_progress_before_remaining_work(service, monkeypatch):
    from app import ingestion, parsing

    for key, value in {
        "BASE_URL": "https://embed.example/v1",
        "MODEL": "embed",
        "API_KEY": "test",
        "BATCH_SIZE": "2",
    }.items():
        monkeypatch.setenv("EMBEDDING_" + key, value)
    manifest = {
        "elements": [{"content_text": "Full text"}],
        "chunks": [
            {"id": str(i), "text": f"Article {i} full text", "locator": {"page": i}} for i in range(1, 5)
        ],
    }
    monkeypatch.setattr(parsing, "parse_bounded", lambda *a, **kw: manifest)
    calls = []

    def embed(texts):
        calls.append(len(texts))
        return [[1.0, float(i + 1)] for i in range(len(texts))]

    monkeypatch.setattr(ingestion, "embed_texts", embed)
    doc = store(service)
    service.collection.process_item(service.collection.claim_processing())
    service.collection.process_item(service.collection.claim_processing())
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 2
        assert (
            conn.execute("SELECT status FROM processing_jobs WHERE document_id=?", (doc["id"],)).fetchone()[0]
            == "queued"
        )
    service.collection.process_item(service.collection.claim_processing())
    assert calls == [2, 2]
    assert service.collection.claim_processing() is None
