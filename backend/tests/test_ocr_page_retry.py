import hashlib
import json
from copy import deepcopy

import pytest

from app import parsing, vision
from app.ingestion import parse_file
from app.ingestion_quality import text_complete, validate_ocr_page
from app.ocr_page_retry import prepare_physical_page_retry
from test_ingestion_quality import make_pdf
from test_vision_ocr import enabled, fake_provider, payload  # noqa: F401


def legacy_manifest():
    content = make_pdf([("Pasal 57\nGubernur Nomor", True), ("Second page remains saved", True)])
    manifest = parse_file("original.pdf", content)
    for page in manifest["pages"]:
        page.update(status="ocr_complete", text=page["native_text"])
        page["ocr"] = {
            "prompt_version": "legal-page-transcription-v2",
            "source_hash": manifest["source_hash"],
            "page": page["page"],
            "complete": True,
            "attempts": 1,
            "uncertain_spans": [],
        }
    first = manifest["pages"][0]
    first["status"] = "ocr_uncertain"
    first["ocr"].update(
        complete=False,
        uncertain_spans=["model_reported_incomplete"],
        phase="recovery",
        recovery={"version": "legal-region-recovery-v5", "status": "conflict", "tasks": []},
    )
    for page in manifest["pages"]:
        validate_ocr_page(page)
    manifest["quality"]["complete"] = False
    return content, manifest


def test_explicit_retry_preserves_old_evidence_and_successful_sibling():
    _, manifest = legacy_manifest()
    before = deepcopy(manifest)
    result = prepare_physical_page_retry(manifest, [1], source_hash=manifest["source_hash"])
    assert manifest == before
    assert result["pages"][1] == before["pages"][1]
    first = result["pages"][0]
    assert first["status"] == "pending_ocr" and first["text"] == before["pages"][0]["text"]
    assert first["ocr"] == {"attempts": 0} and not text_complete(result)
    record = first["ocr_retry_history"][0]
    assert record["previous_page"] == before["pages"][0]
    raw = json.dumps(record["previous_page"], ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    assert record["previous_page_sha256"] == hashlib.sha256(raw).hexdigest()
    assert record["previous_page"]["ocr"]["recovery"]["status"] == "conflict"


@pytest.mark.parametrize("pages", [[], [0], [True], [3], [1, 1], list(range(1, 22))])
def test_retry_rejects_unknown_or_unbounded_pages_without_mutation(pages):
    _, manifest = legacy_manifest()
    before = deepcopy(manifest)
    with pytest.raises(ValueError, match="page_retry"):
        prepare_physical_page_retry(manifest, pages, source_hash=manifest["source_hash"])
    assert manifest == before


@pytest.mark.parametrize("target", ["manifest", "selected_page", "other_page", "ocr"])
def test_retry_rejects_hash_mismatch_for_the_whole_immutable_source(target):
    _, manifest = legacy_manifest()
    digest = manifest["source_hash"]
    row = {
        "manifest": manifest,
        "selected_page": manifest["pages"][0],
        "other_page": manifest["pages"][1],
        "ocr": manifest["pages"][0]["ocr"],
    }[target]
    row["source_hash"] = "f" * 64
    with pytest.raises(ValueError, match="page_retry"):
        prepare_physical_page_retry(manifest, [1], source_hash=digest)


@pytest.mark.parametrize("change", ["current_prompt", "complete", "no_marker", "history"])
def test_retry_cannot_clear_successes_other_errors_or_repeat_paid_strategy(change):
    _, manifest = legacy_manifest()
    page = manifest["pages"][0]
    if change == "current_prompt":
        page["ocr"]["prompt_version"] = vision.PROMPT_VERSION
    elif change == "complete":
        page.update(status="ocr_complete")
        page["ocr"].update(complete=True, uncertain_spans=[])
    elif change == "no_marker":
        page["ocr"]["uncertain_spans"] = ["native_ocr_article_mismatch"]
    else:
        page["ocr_retry_history"] = [{"previous_page": {"ocr_retry_history": []}}]
    with pytest.raises(ValueError, match="page_retry"):
        prepare_physical_page_retry(manifest, [1], source_hash=manifest["source_hash"])


@pytest.mark.parametrize("complete,mismatch", [(True, False), (False, False), (True, True)])
@pytest.mark.usefixtures("enabled")
def test_reread_uses_new_prompt_but_keeps_model_false_and_number_gates(monkeypatch, complete, mismatch):
    content, manifest = legacy_manifest()
    text = "Pasal 51\nGubernur Nomor" if mismatch else "Pasal 57\nGubernur Nomor"
    response = payload(text=text)
    body = json.loads(response["choices"][0]["message"]["content"])
    body["complete"] = complete
    response["choices"][0]["message"]["content"] = json.dumps(body)
    requests = fake_provider(monkeypatch, response)
    prepared = prepare_physical_page_retry(manifest, [1], source_hash=manifest["source_hash"])
    result = parsing.ocr_bounded("original.pdf", content, "application/pdf", prepared, max_pages=1)
    page = result["pages"][0]
    assert len(requests) == 1
    assert "this physical page" in requests[0]["messages"][0]["content"]
    assert "do not invent the continuation" in requests[0]["messages"][0]["content"]
    assert page["ocr"]["prompt_version"] == "legal-page-transcription-v3"
    assert page["text"] == text
    assert page["ocr_retry_history"][0]["previous_page"] == manifest["pages"][0]
    assert result["pages"][1] == manifest["pages"][1]
    assert text_complete(result) is (complete and not mismatch)
    if not complete:
        assert "model_reported_incomplete" in page["ocr"]["uncertain_spans"]
    if mismatch:
        assert "native_ocr_article_mismatch" in page["ocr"]["uncertain_spans"]
    with pytest.raises(ValueError, match="page_retry"):
        prepare_physical_page_retry(result, [1], source_hash=manifest["source_hash"])


def test_retry_rejects_oversize_history_before_changing_input(monkeypatch):
    from app import ocr_page_retry

    _, manifest = legacy_manifest()
    before = deepcopy(manifest)
    monkeypatch.setattr(ocr_page_retry, "MAX_PAGE_BYTES", 1)
    with pytest.raises(ValueError, match="checkpoint_too_large"):
        prepare_physical_page_retry(manifest, [1], source_hash=manifest["source_hash"])
    assert manifest == before
