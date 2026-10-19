"""Saved provider wrappers can be decoded without a second paid request."""

from contextlib import closing
from copy import deepcopy
import base64
import hashlib
import json

import pytest

from app import ingestion, parsing, vision
from app.db import dump
from app.ocr_error_evidence import persist_response_evidence
from app.ocr_response_reparse import apply_saved_response_reparse, reparse_saved_page_response
from app.pdf_ocr_reuse import native_hint
from app.service import Service
from test_ingestion_quality import make_pdf


@pytest.fixture
def saved(tmp_path, monkeypatch):
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users(id,name,is_demo,created_at) VALUES('owner','owner',0,'2026-10-19')")
        content = make_pdf([("Pasal 1\nNOMOR 19 TAHUN 2025", True)])
        blob = service.archive.blobs.put_bytes("owner", content)
        doc = dict(
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
    manifest = ingestion.parse_file("law.pdf", content)
    page = manifest["pages"][0]
    rendered = parsing.render_pdf_page_bounded(content, 1)
    image = service.archive.blobs.put_bytes("owner", base64.b64decode(rendered["image_base64"]))
    hint, strategy = native_hint(page)
    page.update(status="ocr_failed", error="old strict schema failure")
    page["ocr"] = {
        "attempts": 1,
        "source_hash": doc["content_hash"],
        "page": 1,
        "request_id": "one-original-request",
        "prompt_version": vision.PROMPT_VERSION,
        "requested_model": "saved-model",
        "image_sha256": rendered["image_sha256"],
        "native_hint_hash": hashlib.sha256(hint.encode()).hexdigest(),
        "native_hint_chars": len(hint),
        "native_hint_strategy": strategy,
        "image_blob": {
            "content_hash": image.digest,
            "size": image.size,
            "storage_path": str(image.path.relative_to(tmp_path)),
        },
    }
    raw = json.dumps(
        {
            "type": "json_object",
            "content": {
                "complete": True,
                "printed_page": "1",
                "text": "Pasal 1\nNOMOR 19 TAHUN 2025",
                "uncertain_spans": [],
                "is_blank": False,
            },
        }
    )
    error = vision._response_error(
        "old schema failure",
        {},
        raw,
        error_code="invalid_transcription_schema",
        payload_kind="assistant_message",
        finish_reason="stop",
    )
    evidence = {
        **error.response_evidence,
        **{
            key: page["ocr"][key]
            for key in (
                "source_hash",
                "page",
                "request_id",
                "image_sha256",
                "prompt_version",
                "requested_model",
            )
        },
        "attempt": 1,
        "finish_reason": "stop",
    }
    reference = persist_response_evidence(
        service.archive.blobs, "owner", evidence, source_hash=doc["content_hash"], request_page=page
    )
    page["ocr"]["error_response"] = {
        **{key: value for key, value in evidence.items() if key != "response_base64"},
        "blob": reference,
    }
    manifest["quality"] = ingestion.pdf_quality(manifest["pages"])
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed' WHERE id=?", (dump(manifest), doc["id"])
        )
        service.collection.queue_processing(conn, doc["id"], "owner")
        conn.execute("UPDATE processing_jobs SET status='failed' WHERE document_id=?", (doc["id"],))
    monkeypatch.setattr(vision, "_invoke", lambda *_: pytest.fail("saved response called model"))
    return service, doc, manifest, (tmp_path / reference["storage_path"]).read_bytes()


def test_offline_reparse_keeps_request_attempts_and_exact_old_failure(saved):
    _, _, manifest, raw = saved
    old = deepcopy(manifest["pages"][0])
    updated = reparse_saved_page_response(old, raw)
    assert updated["status"] == "ocr_complete"
    assert updated["ocr"]["request_id"] == old["ocr"]["request_id"]
    assert updated["ocr"]["attempts"] == old["ocr"]["attempts"] == 1
    assert updated["ocr_attempt_history"][0]["previous_page"] == old
    assert updated["ocr"]["error_response"] == old["ocr"]["error_response"]
    with pytest.raises(ValueError):
        reparse_saved_page_response(updated, raw)


@pytest.mark.parametrize("damage", ["source", "request", "blob", "hint"])
def test_reparse_refuses_changed_source_request_blob_or_native_hint(saved, damage):
    _, _, manifest, raw = saved
    page = deepcopy(manifest["pages"][0])
    if damage == "source":
        page["source_hash"] = "f" * 64
    elif damage == "request":
        page["ocr"]["request_id"] = "different-request"
    elif damage == "blob":
        raw += b" "
    else:
        page["native_visibility"] = {"requires_visual_review": False}
        page["native_text"] += " changed original layer"
    with pytest.raises(ValueError):
        reparse_saved_page_response(page, raw)


def test_same_live_quality_gate_keeps_missing_table_pending(saved):
    _, _, manifest, raw = saved
    page = deepcopy(manifest["pages"][0])
    page["reasons"].append("table_layout")
    updated = reparse_saved_page_response(page, raw)
    assert updated["status"] == "ocr_uncertain"
    assert "table_layout_not_preserved" in updated["ocr"]["uncertain_spans"]


def test_apply_is_owner_version_fenced_and_replay_never_reissues(saved):
    service, doc, manifest, _ = saved
    expected = hashlib.sha256(dump(manifest).encode()).hexdigest()
    args = dict(
        page_number=1, source_hash=doc["content_hash"], version=doc["version"], expected_manifest=expected
    )
    for owner, changes in (
        ("other", {}),
        ("owner", {"version": 2}),
        ("owner", {"expected_manifest": "f" * 64}),
    ):
        with pytest.raises(ValueError):
            apply_saved_response_reparse(service, owner, doc["id"], **(args | changes), apply=True)
    report = apply_saved_response_reparse(service, "owner", doc["id"], **args)
    assert report["database_writes"] == report["model_calls"] == 0 and report["text_complete"]
    applied = apply_saved_response_reparse(service, "owner", doc["id"], **args, apply=True)
    assert applied["database_writes"] == 1 and applied["model_calls"] == 0
    with closing(service.db.connect()) as conn:
        current = conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()
        assert current["content_hash"] == doc["content_hash"] and current["version"] == doc["version"]
        assert not conn.execute("SELECT 1 FROM retrieval_vectors").fetchone()
    with pytest.raises(ValueError):
        apply_saved_response_reparse(service, "owner", doc["id"], **args, apply=True)
