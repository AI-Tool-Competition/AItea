import hashlib
import json
import base64
import io
import zipfile
from contextlib import closing
from copy import deepcopy

import pytest
from app.bootstrap import Service
from app.contexts.documents.infrastructure.parsers import document_parser
from app.contexts.documents.infrastructure.providers.mineru import manifest_valid
from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import (
    EMPTY_PAGE_PROTOCOL,
    PROTOCOL,
    is_mineru_empty_page_review,
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


def test_large_mineru_rebuild_has_a_bounded_document_scale_cpu_window(monkeypatch):
    import resource

    from app.contexts.documents.infrastructure.providers import mineru_visual_page_retry

    limits = []
    sent = []

    class Connection:
        def send_bytes(self, payload):
            sent.append(json.loads(payload))

        def close(self):
            pass

    monkeypatch.setattr(resource, "setrlimit", lambda kind, value: limits.append((kind, value)))
    monkeypatch.setattr(mineru_visual_page_retry, "rebuild_manifest", lambda value: value)
    payload = {"parser": "mineru", "pages": []}
    document_parser._auxiliary_worker(Connection(), "mineru_rebuild", payload)

    assert (resource.RLIMIT_CPU, (document_parser._MINERU_REBUILD_CPU_SECONDS,) * 2) in limits
    assert sent == [{"result": payload}]

    bounded_call = {}

    def run_bounded(worker, args, timeout):
        bounded_call.update(worker=worker, args=args, timeout=timeout)
        return payload

    monkeypatch.setattr(document_parser, "_run_bounded", run_bounded)
    assert document_parser.rebuild_pdf_bounded(payload) == payload
    assert bounded_call["worker"] is document_parser._auxiliary_worker
    assert bounded_call["args"] == ("mineru_rebuild", payload)
    assert bounded_call["timeout"] == document_parser._MINERU_REBUILD_TIMEOUT_SECONDS


def test_visual_table_retry_guidance_uses_the_saved_quality_failure_category():
    page = {
        "ocr_fallback": {"protocol": PROTOCOL, "attempts": 5},
        "ocr_retry_cycles": [
            {"previous_page": {"ocr": {"uncertain_spans": ["table_layout_not_preserved"]}}}
        ],
    }

    assert document_parser._visual_table_retry_guidance(page) == "table_layout_not_preserved"
    page["ocr_retry_cycles"][0]["previous_page"]["ocr"]["uncertain_spans"] = ["illegible_printed_text"]
    assert document_parser._visual_table_retry_guidance(page) is None


def test_visual_result_without_structured_table_stays_in_review():
    rebuilt = rebuild_manifest(_manifest("Code 0101, rate 0%"))
    assert rebuilt["pages"][0]["status"] == "ocr_uncertain"
    assert "table_layout_not_preserved" in rebuilt["pages"][0]["ocr"]["uncertain_spans"]
    assert rebuilt["quality"]["complete"] is False
    assert any(item["element_key"] == "mineru-000001-000001" for item in rebuilt["elements"])


def _empty_page_visual_manifest(text, *, is_blank):
    manifest = _manifest("MinerU returned no searchable body text")
    source_hash = manifest["source_hash"]
    result_hash = "c" * 64
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    previous = {
        "page": 1,
        "status": "ocr_complete",
        "source_hash": source_hash,
        "text": "",
        "ocr": {
            "provider": "mineru",
            "task_id": "task-1",
            "provider_page_idx": 0,
            "result_hash": result_hash,
            "text_sha256": hashlib.sha256(b"").hexdigest(),
        },
    }
    previous_digest = hashlib.sha256(
        json.dumps(previous, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    page = manifest["pages"][0]
    page.update(
        status="ocr_complete",
        text=text,
        reasons=[],
        ocr={
            "provider": "vision",
            "source_hash": source_hash,
            "page": 1,
            "complete": True,
            "is_blank": is_blank,
            "uncertain_spans": [],
            "text_sha256": text_hash,
            "image_sha256": "b" * 64,
            "image_blob": {"content_hash": "b" * 64, "size": 100, "mime_type": "image/png"},
            "prompt_version": "legal-page-transcription-v6",
            "request_id": "1" * 32,
        },
        ocr_fallback={
            "protocol": EMPTY_PAGE_PROTOCOL,
            "attempts": 1,
            "source_result_hash": result_hash,
            "source_page": 1,
        },
        ocr_retry_cycles=[
            {
                "protocol": "ocr-automatic-retry-checkpoint-v1",
                "previous_page": previous,
                "previous_page_sha256": previous_digest,
            }
        ],
    )
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
                "batch_id": "batch-1",
                "source_hash": source_hash,
                "first_page": 1,
                "last_page": 2,
                "result_blob": {"content_hash": result_hash},
            }
        ],
    )
    return manifest


def test_empty_mineru_pages_require_a_visual_check_before_indexing():
    from app.contexts.documents.infrastructure.parsers.ingestion_quality import pdf_page_status

    page = {
        "page": 1,
        "status": "ocr_complete",
        "text": "",
        "reasons": [],
        "ocr": {"provider": "mineru", "complete": True, "uncertain_spans": []},
    }
    assert is_mineru_empty_page_review(page)
    assert pdf_page_status(page) == "ocr_uncertain"


def test_mineru_empty_page_cannot_pass_the_document_completeness_gate():
    from app.contexts.documents.infrastructure.parsers.ingestion_quality import text_complete

    source_hash, result_hash = "a" * 64, "c" * 64
    page = {
        "page": 1,
        "status": "ocr_complete",
        "source_hash": source_hash,
        "text": "",
        "reasons": [],
        "ocr": {
            "provider": "mineru",
            "task_id": "task-1",
            "provider_page_idx": 0,
            "result_hash": result_hash,
            "text_sha256": hashlib.sha256(b"").hexdigest(),
            "complete": True,
            "uncertain_spans": [],
        },
    }
    manifest = {
        "parser": "mineru",
        "source_hash": source_hash,
        "quality": {"complete": True},
        "pages": [page],
        "elements": [],
        "remote_parse": {
            "provider": "mineru",
            "state": "done",
            "source_hash": source_hash,
            "page_count": 1,
            "parts": [
                {
                    "task_id": "task-1",
                    "source_hash": source_hash,
                    "first_page": 1,
                    "last_page": 1,
                    "result_blob": {"content_hash": result_hash},
                }
            ],
        },
    }

    assert manifest_valid(manifest)
    assert not text_complete(manifest)


@pytest.mark.parametrize(("text", "is_blank"), [("", True), ("Visible legal text", False)])
def test_empty_mineru_page_fallback_requires_blank_proof_or_indexes_its_transcript(text, is_blank):
    manifest = _empty_page_visual_manifest(text, is_blank=is_blank)
    rebuilt = rebuild_manifest(manifest)
    page_elements = [
        item for item in rebuilt["elements"] if item["locator"].get("physical_page") == 1
    ]

    assert rebuilt["pages"][0]["ocr_fallback"]["accepted"] is True
    if is_blank:
        assert page_elements == []
        assert not any(chunk["locator"].get("page") == 1 for chunk in rebuilt["chunks"])
        assert manifest_valid(rebuilt)
    else:
        assert len(page_elements) == 1
        assert page_elements[0]["content_text"] == text
        assert page_elements[0]["metadata_json"]["fallback_protocol"] == EMPTY_PAGE_PROTOCOL
        assert any(text in chunk["text"] for chunk in rebuilt["chunks"])
        assert manifest_valid(rebuilt)


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


def test_office_visual_retry_renders_the_mineru_conversion_pdf(monkeypatch, tmp_path):
    from pypdf import PdfWriter
    from app.contexts.documents.infrastructure.providers.mineru_results import _assemble, reference, resolve_reference
    from app.shared.infrastructure.persistence.blob_store import BlobStore

    store = BlobStore(tmp_path / "files", max_bytes=128 * 1024 * 1024)
    source = store.put_bytes("owner", b"submitted Office source")
    pdf_output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=300)
    writer.write(pdf_output)
    converted_pdf = pdf_output.getvalue()
    result_output = io.BytesIO()
    with zipfile.ZipFile(result_output, "w") as archive:
        archive.writestr("layout.json", json.dumps({"pdf_info": [{"page_idx": 0, "page_size": [200, 300]}]}))
        archive.writestr("task_content_list.json", json.dumps([{"type": "text", "text": "", "page_idx": 0}]))
        archive.writestr("full.md", "")
        archive.writestr("839d0d23-bf92-4c24-9b57-c8a79ff44512_origin.pdf", converted_pdf)
    result = store.put_bytes("owner", result_output.getvalue())
    task = {
        "id": "office-task",
        "batch_id": "office-batch",
        "state": "done",
        "source_hash": source.digest,
        "first_page": 1,
        "last_page": 1,
        "part_blob": reference(source),
        "result_blob": reference(result),
    }
    manifest = _assemble([task], source.digest, "source.doc", str(store.files_dir), "owner", None)
    page = manifest["pages"][0]
    page.update(
        status="pending_ocr",
        reasons=["empty_text_unverified"],
        ocr_fallback={
            "protocol": EMPTY_PAGE_PROTOCOL,
            "attempts": 1,
            "source_result_hash": result.digest,
            "source_page": 1,
        },
        ocr={"attempts": 0},
    )
    part = manifest["remote_parse"]["parts"][0]
    from app.contexts.documents.infrastructure.providers.mineru_results import _extract_render_pdf

    receipt = _extract_render_pdf(part, str(store.files_dir), "owner")
    part["render_pdf"] = receipt
    converted_path = resolve_reference(receipt["blob"], store.files_dir, "owner")
    render_source = document_parser._validated_mineru_render_source(
        manifest,
        1,
        {"path": str(converted_path), "receipt": receipt, "page_number": 1},
    )
    rendered_call = {}

    def render(path, **kwargs):
        rendered_call.update(path=str(path), **kwargs)
        return {
            "image_base64": base64.b64encode(b"rendered-page").decode("ascii"),
            "image_sha256": hashlib.sha256(b"rendered-page").hexdigest(),
            "source_hash": render_source["source_hash"],
            "page": render_source["page_number"],
            "width": 1200,
            "height": 1800,
            "mime_type": "image/png",
        }

    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.parsers.archive_parsing.render_archive_page_bounded",
        render,
    )
    monkeypatch.setattr(document_parser._OCR_SLOTS, "acquire", lambda timeout: True)
    monkeypatch.setattr(document_parser._OCR_SLOTS, "release", lambda: None)
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.recovery.pdf_ocr_reuse.current_profile",
        lambda: None,
    )
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.recovery.pdf_ocr_reuse.native_hint",
        lambda _page: ("", "empty"),
    )
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.vision.transcribe_page",
        lambda _image: {
            "text": "",
            "printed_page": None,
            "is_blank": True,
            "complete": True,
            "uncertain_spans": [],
        },
    )

    updated = document_parser._ocr_one_page(
        None,
        page,
        archive_path=resolve_reference(reference(source), store.files_dir, "owner"),
        archive_size=source.size,
        render_source=render_source,
    )

    assert rendered_call == {
        "path": str(converted_path),
        "source_hash": receipt["blob"]["content_hash"],
        "size": receipt["blob"]["size"],
        "page_number": 1,
    }
    assert updated["status"] == "ocr_complete"
    assert updated["ocr"]["source_hash"] == source.digest
    assert updated["ocr"]["page"] == 1
    assert updated["ocr"]["render_source"]["result_hash"] == result.digest
    assert updated["ocr"]["render_source"]["source_hash"] == source.digest


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
