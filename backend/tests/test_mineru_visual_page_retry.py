import hashlib
import json
from contextlib import closing
from copy import deepcopy

from app.bootstrap import Service
from app.contexts.documents.infrastructure.parsers import document_parser
from app.contexts.documents.infrastructure.providers.mineru import manifest_valid
from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import (
    PROTOCOL,
    markdown_table_is_consistent,
    rebuild_manifest,
)
from app.shared.domain.values import dump, now
from test_retrieval_index import body, seed
from support.ingestion_quality import make_pdf


def _manifest(table_text):
    source_hash = "a" * 64
    table_page = {
        "page": 1,
        "status": "ocr_complete",
        "source_hash": source_hash,
        "native_text": "",
        "text": table_text,
        "reasons": ["table_structure_ambiguous"],
        "ocr": {
            "provider": "vision",
            "prompt_version": "legal-page-transcription-v3",
            "request_id": "request-1",
            "complete": True,
            "uncertain_spans": [],
            "text_sha256": hashlib.sha256(table_text.encode()).hexdigest(),
            "image_sha256": "b" * 64,
        },
        "ocr_fallback": {
            "protocol": PROTOCOL,
            "attempts": 1,
            "source_result_hash": "c" * 64,
            "source_page": 1,
        },
    }
    other_page = {
        "page": 2,
        "status": "ocr_complete",
        "source_hash": source_hash,
        "native_text": "Other page",
        "text": "Other page",
        "reasons": [],
        "ocr": {"provider": "mineru", "complete": True, "uncertain_spans": []},
    }
    return {
        "parser": "mineru",
        "parser_version": "api-v4",
        "pipeline_version": "mineru-precise-v1",
        "source_hash": source_hash,
        "title": "RCEP tariff schedule",
        "warnings": [],
        "remote_parse": {"provider": "mineru", "state": "done", "source_hash": source_hash},
        "pages": [table_page, other_page],
        "elements": [
            {
                "element_key": "mineru-000001-000001",
                "kind": "table",
                "order": 1,
                "content_text": "old ambiguous table",
                "locator": {
                    "kind": "pdf",
                    "page": 1,
                    "physical_page": 1,
                    "bbox": [10, 10, 990, 990],
                    "coordinate_system": "mineru-0-1000",
                    "source_hash": source_hash,
                    "provider_task_id": "task-1",
                    "provider_page_idx": 0,
                },
                "section_path": ["Schedule"],
                "parent_key": None,
                "metadata_json": {"provider": "mineru"},
            },
            {
                "element_key": "mineru-000002-000001",
                "kind": "paragraph",
                "order": 2,
                "content_text": "Other page",
                "locator": {
                    "kind": "pdf",
                    "page": 2,
                    "physical_page": 2,
                    "bbox": [10, 10, 990, 990],
                    "coordinate_system": "mineru-0-1000",
                    "source_hash": source_hash,
                },
                "section_path": ["Schedule"],
                "parent_key": None,
                "metadata_json": {"provider": "mineru"},
            },
        ],
        "chunks": [],
    }


def test_markdown_table_requires_consistent_columns():
    assert markdown_table_is_consistent("| Code | Rate |\n| --- | --- |\n| 0101 | 0% |")
    assert not markdown_table_is_consistent("| Code | Rate |\n| --- | --- |\n| 0101 | 0% | extra")
    assert not markdown_table_is_consistent(
        "| Code | Rate |\n| --- | --- |\n| 0101 | 0% |\n\n| Name | Rate |\n| --- | --- |\n| Tea | 2% | extra"
    )
    assert not markdown_table_is_consistent("Code: 0101, Rate: 0%")


def test_accepted_visual_fallback_replaces_only_that_page_and_rebuilds_chunks():
    manifest = _manifest("| Code | Rate |\n| --- | --- |\n| 0101 | 0% |")
    rebuilt = rebuild_manifest(manifest)

    page_one = [
        item
        for item in rebuilt["elements"]
        if item["locator"].get("physical_page") == 1
    ]
    page_two = [
        item
        for item in rebuilt["elements"]
        if item["locator"].get("physical_page") == 2
    ]
    assert len(page_one) == 1
    assert page_one[0]["element_key"] == "mineru-visual-page-000001"
    assert page_one[0]["kind"] == "table"
    assert page_one[0]["metadata_json"]["fallback_protocol"] == PROTOCOL
    assert len(page_two) == 1 and page_two[0]["content_text"] == "Other page"
    assert rebuilt["pages"][0]["ocr_fallback"]["accepted"] is True
    assert "table_structure_ambiguous" not in rebuilt["pages"][0]["reasons"]
    assert rebuilt["quality"]["complete"] is True
    assert any("| 0101 | 0% |" in chunk["text"] for chunk in rebuilt["chunks"])
    assert rebuilt["remote_parse"]["state"] == "done"


def test_visual_result_without_structured_table_stays_in_review():
    rebuilt = rebuild_manifest(_manifest("Code 0101, rate 0%"))
    assert rebuilt["pages"][0]["status"] == "ocr_uncertain"
    assert "table_layout_not_preserved" in rebuilt["pages"][0]["ocr"]["uncertain_spans"]
    assert rebuilt["quality"]["complete"] is False
    assert any(item["element_key"] == "mineru-000001-000001" for item in rebuilt["elements"])


def test_mineru_receipt_validation_accepts_only_source_bound_visual_page_evidence():
    manifest = _manifest("| Code | Rate |\n| --- | --- |\n| 0101 | 0% |")
    source_hash = manifest["source_hash"]
    result_hash = "c" * 64
    original_text = "Original MinerU table row"
    previous = {
        "page": 1,
        "status": "ocr_uncertain",
        "source_hash": source_hash,
        "text": original_text,
        "ocr": {
            "provider": "mineru",
            "task_id": "task-1",
            "provider_page_idx": 0,
            "result_hash": result_hash,
            "text_sha256": hashlib.sha256(original_text.encode()).hexdigest(),
        },
    }
    previous_digest = hashlib.sha256(
        json.dumps(previous, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest["pages"][0]["ocr_retry_cycles"] = [
        {"protocol": "ocr-automatic-retry-checkpoint-v1", "previous_page": previous, "previous_page_sha256": previous_digest}
    ]
    image_hash = "b" * 64
    text_hash = hashlib.sha256(manifest["pages"][0]["text"].encode()).hexdigest()
    manifest["pages"][0]["ocr"].update(
        provider="vision",
        source_hash=source_hash,
        page=1,
        text_sha256=text_hash,
        image_sha256=image_hash,
        image_blob={"content_hash": image_hash, "size": 100, "mime_type": "image/png"},
        prompt_version="legal-page-transcription-v3",
        request_id="1" * 32,
    )
    manifest["pages"][0]["ocr_fallback"].update(
        accepted=True,
        text_sha256=text_hash,
        prompt_version="legal-page-transcription-v3",
    )
    manifest["pages"][0]["reasons"] = []
    manifest["pages"][1]["ocr"].update(
        task_id="task-1",
        provider_page_idx=1,
        result_hash=result_hash,
        text_sha256=hashlib.sha256(manifest["pages"][1]["text"].encode()).hexdigest(),
    )
    manifest["remote_parse"].update(
        page_count=2,
        parts=[
            {
                "task_id": "task-1",
                "source_hash": source_hash,
                "first_page": 1,
                "last_page": 2,
                "result_blob": {"content_hash": result_hash},
            }
        ],
    )

    assert manifest_valid(manifest)
    invalid = json.loads(json.dumps(manifest))
    invalid["pages"][0]["ocr_fallback"]["text_sha256"] = "0" * 64
    assert not manifest_valid(invalid)


def test_bounded_ocr_accepts_large_mineru_manifest_with_small_batch(monkeypatch, tmp_path):
    content = make_pdf([("page", False) for _ in range(380)])
    source_hash = hashlib.sha256(content).hexdigest()
    manifest = {
        "parser": "mineru",
        "source_hash": source_hash,
        "pages": [
            {
                "page": page,
                "source_hash": source_hash,
                "status": "ocr_complete",
                "text": "",
                "native_text": "",
                "ocr": {"complete": True, "uncertain_spans": []},
            }
            for page in range(1, 381)
        ],
    }
    monkeypatch.setattr(document_parser, "rebuild_pdf_bounded", lambda value: value)
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.vision.vision_ocr_configured",
        lambda: False,
    )
    source_path = tmp_path / "large.pdf"
    source_path.write_bytes(content)
    rebuilt = document_parser.ocr_bounded(
        "large.pdf",
        None,
        "application/pdf",
        manifest,
        max_pages=2,
        archive_path=source_path,
        archive_size=len(content),
    )
    assert len(rebuilt["pages"]) == 380


def test_bounded_ocr_skips_rechunking_when_ocr_fails_without_new_text(monkeypatch):
    content = b"source-bound MinerU fixture"
    source_hash = hashlib.sha256(content).hexdigest()
    manifest = _manifest("| Code | Rate |\n| --- | --- |\n| 0101 | 0% |")
    manifest["source_hash"] = source_hash
    manifest["remote_parse"]["source_hash"] = source_hash
    for page in manifest["pages"]:
        page["source_hash"] = source_hash
    for element in manifest["elements"]:
        element["locator"]["source_hash"] = source_hash
    manifest["pages"][0].update(
        status="pending_ocr",
        ocr_fallback={"protocol": PROTOCOL, "attempts": 1},
        ocr={"provider": "vision", "attempts": 1},
    )

    def failed_checkpoint(_content, page, *_args, **_kwargs):
        updated = deepcopy(page)
        updated["ocr"].update(attempts=2, finish_reason="length")
        updated.update(status="ocr_failed", error="model response was truncated")
        return updated

    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.vision.vision_ocr_configured",
        lambda: True,
    )
    monkeypatch.setattr(document_parser, "_ocr_one_page", failed_checkpoint)
    monkeypatch.setattr(
        document_parser,
        "rebuild_pdf_bounded",
        lambda _manifest: (_ for _ in ()).throw(AssertionError("unnecessary full rebuild")),
    )

    rebuilt = document_parser.ocr_bounded(
        "rcep.pdf",
        content,
        "application/pdf",
        manifest,
        max_pages=1,
    )

    assert rebuilt["pages"][0]["status"] == "ocr_failed"
    assert rebuilt["pages"][0]["ocr"]["attempts"] == 2
    assert rebuilt["chunks"] == manifest["chunks"]
    assert rebuilt["quality"]["failed_pages"] == [1]


def test_collection_worker_runs_visual_fallback_and_indexes_rebuilt_table(monkeypatch, tmp_path):
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,NULL,NULL,1,?)", ("owner", "owner", now()))
    content = make_pdf([("Tariff table image", False), ("Other page", False)])
    source = service.archive.blobs.put_bytes("owner", content)
    manifest = _manifest("old ambiguous table output")
    manifest["source_hash"] = source.digest
    manifest["remote_parse"]["source_hash"] = source.digest
    for page in manifest["pages"]:
        page["source_hash"] = source.digest
    for element in manifest["elements"]:
        element["locator"]["source_hash"] = source.digest
    manifest["pages"][0].update(
        status="pending_ocr",
        text="old ambiguous table output",
        ocr={"attempts": 0},
    )
    with service.db.transaction() as conn:
        seed(conn, "visual-target", body())
        conn.execute(
            """UPDATE documents SET filename='rcep.pdf',mime_type='application/pdf',storage_path=?,
            content_hash=?,size=?,manifest=? WHERE id='visual-target'""",
            (str(source.path), source.digest, source.size, dump(manifest)),
        )
        service.collection.queue_processing(conn, "visual-target", "owner")

    monkeypatch.setattr(service, "embedding_fingerprint", lambda: None)
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.vision.vision_ocr_configured",
        lambda: True,
    )
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.vision.vision_ocr_request_worker_limit",
        lambda: 2,
    )
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.vision.transcribe_page",
        lambda _rendered: {
            "complete": True,
            "printed_page": None,
            "text": "| Code | Rate |\n| --- | --- |\n| 0101 | 0% |",
            "uncertain_spans": [],
            "is_blank": False,
        },
    )
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.mineru.manifest_valid",
        lambda value: value["remote_parse"]["state"] == "done",
    )

    claim = service.collection.claim_processing(owner_id="owner", document_ids=["visual-target"])
    assert claim
    service.collection.process_item(claim)
    with closing(service.db.connect()) as conn:
        row = conn.execute(
            "SELECT status,manifest FROM documents WHERE id='visual-target'"
        ).fetchone()
        job = conn.execute(
            "SELECT status,phase,error FROM processing_jobs WHERE document_id='visual-target'"
        ).fetchone()
    saved = json.loads(row["manifest"])
    assert job["status"] == "completed", dict(job)
    assert row["status"] == "ready"
    assert saved["quality"]["complete"] is True
    assert saved["pages"][0]["ocr_fallback"]["accepted"] is True
    assert saved["pages"][0]["ocr"]["provider"] == "vision"
    assert any("| 0101 | 0% |" in chunk["text"] for chunk in saved["chunks"])
