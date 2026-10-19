"""Actual bounded pixels, durable attempts and owner/version-fenced page reuse."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy

import pytest

from app import parsing, pdf_ocr_reuse, vision
from app.db import dump, now
from app.ingestion import parse_file
from app.ingestion_quality import text_complete
from app.service import Service
from test_ingestion_quality import make_pdf
from test_vision_ocr import fake_provider, payload

NATIVE = "Pasal 1\nSimpan bukti yang lengkap."
SOURCE = "https://official.invalid/acfta/full-original.pdf"


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("VISION_OCR_ENABLED", "true")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "private-test-key")
    monkeypatch.setenv("LLM_MODEL", "test-vision-model")


@pytest.fixture
def service(tmp_path):
    result = Service(tmp_path / "owned")
    with result.db.transaction() as conn:
        for owner in ("owner", "other"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 1, now()))
    return result


def register(service, content, *, key, owner="owner", country="ID", level="primary", url=SOURCE):
    blob = service.archive.blobs.put_bytes(owner, content)
    manifest = parse_file("official.pdf", content, "application/pdf")
    with service.db.transaction() as conn:
        doc = service.archive.store_original(
            conn,
            owner,
            blob,
            "official.pdf",
            "application/pdf",
            url,
            collection="country:" + country,
            level=level,
            tags=["country:" + country],
            identity_key=key,
            kind_hint="law",
        )
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
        return dict(service.own_document(conn, owner, doc["id"]))


def stored(service, doc):
    with closing(service.db.connect()) as conn:
        row = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone())
        row["manifest"] = json.loads(row["manifest"])
        return row


def process(service, doc):
    claim = service.collection.claim_processing(owner_id=doc["owner_id"], document_ids=[doc["id"]])
    assert claim
    service.collection.process_item(claim)
    return stored(service, doc)


@pytest.fixture
def pair(service, enabled, monkeypatch):
    requests = fake_provider(monkeypatch, payload(text=NATIVE))
    content = make_pdf([(NATIVE, True)])
    donor = register(service, content, key="donor", country="ID")
    target = register(service, content, key="target", country="SG")
    donor_after = process(service, donor)
    assert donor_after["manifest"]["pages"][0]["status"] == "ocr_complete"
    assert len(requests) == 1
    rendered = parsing.render_pdf_page_bounded(content, 1)
    return service, donor, target, content, rendered, requests


def find(pair, target_page=None):
    service, _donor, target, _content, rendered, _requests = pair
    page = target_page or stored(service, target)["manifest"]["pages"][0]
    return pdf_ocr_reuse.find_verified_reuse(service, target, page, rendered)


def change_donor(pair, mutation):
    service, donor, *_ = pair
    with service.db.transaction() as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (donor["id"],)).fetchone()[0]
        )
        mutation(manifest["pages"][0])
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), donor["id"]))


def test_same_original_across_countries_reuses_success_with_zero_new_provider_calls(pair):
    service, donor, target, _content, _rendered, requests = pair
    before = stored(service, donor)
    after = process(service, target)
    page = after["manifest"]["pages"][0]
    assert len(requests) == 1
    assert page["status"] == "ocr_complete" and page["text"] == NATIVE
    assert page["ocr"]["attempts"] == 0
    proof = page["ocr"]["reuse"]
    assert proof["source_document_id"] == donor["id"]
    assert proof["source_document_version"] == donor["version"]
    assert proof["target_document_id"] == target["id"]
    assert proof["target_model_calls"] == 0 and proof["origin_attempts"] == 1
    assert proof["local_bounded_render_verified"]
    assert page["ocr"]["provider_profile"]["fingerprint"] == pdf_ocr_reuse.current_profile()["fingerprint"]
    assert text_complete(after["manifest"])
    assert stored(service, donor) == before
    with closing(service.db.connect()) as conn:
        assert (
            conn.execute(
                "SELECT country FROM document_countries WHERE document_id=?", (target["id"],)
            ).fetchone()[0]
            == "SG"
        )
        assert not conn.execute("SELECT count(*) FROM legal_relations").fetchone()[0]
        assert not conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0]
        assert (
            service.catalog.metadata_for(target["id"], source_url=SOURCE, conn=conn)["metadata"][
                "review_status"
            ]
            == "draft"
        )


def test_independent_secondary_text_reuses_but_never_becomes_indexable_or_vectorized(pair):
    service, _donor, _target, content, _rendered, requests = pair
    target = register(service, content, key="secondary", country="SG", level="secondary")
    after = process(service, target)
    assert after["manifest"]["pages"][0]["ocr"]["reuse"]["target_model_calls"] == 0
    assert len(requests) == 1
    with closing(service.db.connect()) as conn:
        assert not service.archive.can_index(conn, target["id"])
        assert not conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0]
        assert after["role"] == "primary"


@pytest.mark.parametrize("problem", ["owner", "raw_hash", "version", "source_url", "role"])
def test_donor_scope_identity_changes_are_rejected(pair, problem):
    service, donor, *_ = pair
    column, value = {
        "owner": ("owner_id", "other"),
        "raw_hash": ("content_hash", "0" * 64),
        "version": ("version", 2),
        "source_url": ("source_url", "https://official.invalid/other.pdf"),
        "role": ("role", "secondary"),
    }[problem]
    with service.db.transaction() as conn:
        conn.execute(f"UPDATE documents SET {column}=? WHERE id=?", (value, donor["id"]))
    assert find(pair) is None


@pytest.mark.parametrize(
    "problem",
    [
        "legacy_profile",
        "profile",
        "prompt",
        "model",
        "native",
        "reasons",
        "page",
        "source",
        "pixel_hash",
        "width",
        "png_reference_owner",
        "uncertainty",
        "validation_warning",
        "numbers",
        "recovery",
        "incomplete",
        "hint",
        "native_transcript_disagreement",
    ],
)
def test_donor_content_image_or_provider_evidence_is_not_guessed(pair, problem):
    def mutate(page):
        ocr = page["ocr"]
        if problem == "legacy_profile":
            ocr.pop("provider_profile")
        elif problem == "profile":
            ocr["provider_profile"]["fingerprint"] = "a" * 64
        elif problem == "prompt":
            ocr["prompt_version"] = "legacy-page-prompt"
        elif problem == "model":
            ocr["requested_model"] = "another-model"
        elif problem == "native":
            page["native_text"] += " changed"
        elif problem == "reasons":
            page["reasons"].append("independent_review_required")
        elif problem == "page":
            page["page"] = 2
        elif problem == "source":
            page["source_hash"] = "b" * 64
        elif problem == "pixel_hash":
            ocr["image_sha256"] = "c" * 64
        elif problem == "width":
            ocr["width"] += 1
        elif problem == "png_reference_owner":
            ocr["image_blob"]["storage_path"] = ocr["image_blob"]["storage_path"].replace(
                hashlib.sha256(b"owner").hexdigest(), hashlib.sha256(b"other").hexdigest()
            )
        elif problem == "uncertainty":
            ocr["uncertain_spans"] = ["independent doubt"]
        elif problem == "validation_warning":
            ocr["validation_warnings"] = ["independent source boundary"]
        elif problem == "numbers":
            page["text"] = ocr["text"] = "Pasal 2\nSimpan bukti yang lengkap."
        elif problem == "recovery":
            ocr["recovery"] = {"status": "complete", "regions": []}
        elif problem == "incomplete":
            ocr["complete"] = False
        elif problem == "hint":
            ocr["native_hint_hash"] = "d" * 64
        elif problem == "native_transcript_disagreement":
            page["text"] = "some other body"

    change_donor(pair, mutate)
    assert find(pair) is None


@pytest.mark.parametrize("field", ["LLM_BASE_URL", "LLM_MODEL"])
def test_current_provider_change_rejects_old_success(pair, monkeypatch, field):
    monkeypatch.setenv(
        field, "https://another-provider.invalid/v1" if field == "LLM_BASE_URL" else "different-model"
    )
    assert find(pair) is None


@pytest.mark.parametrize("change", ["attempt", "failed", "uncertain", "history", "recovery", "pending_image"])
def test_target_independent_attempts_failures_and_history_are_not_overwritten(pair, change):
    target_page = deepcopy(stored(pair[0], pair[2])["manifest"]["pages"][0])
    if change == "attempt":
        target_page["ocr"] = {"attempts": 1}
    elif change == "failed":
        target_page["status"] = "ocr_failed"
    elif change == "uncertain":
        target_page["status"] = "ocr_uncertain"
    elif change == "history":
        target_page["ocr"] = {"attempts": 0, "recovery_history": [{"conflict": True}]}
    elif change == "recovery":
        target_page["ocr"] = {"attempts": 0, "phase": "recovery"}
    elif change == "pending_image":
        target_page["ocr"] = {"image_blob": {"content_hash": "f" * 64}}
    before = deepcopy(target_page)
    assert find(pair, target_page) is None and target_page == before


def test_corrupt_owner_cas_png_is_rejected_without_mutating_donor(pair):
    service, donor, *_ = pair
    before = stored(service, donor)
    reference = before["manifest"]["pages"][0]["ocr"]["image_blob"]
    path = service.db.data_dir / reference["storage_path"]
    path.chmod(0o600)
    path.write_bytes(b"broken PNG")
    assert find(pair) is None and stored(service, donor) == before


def test_actual_local_render_must_match_source_dimensions_and_pixels(pair):
    altered = deepcopy(pair[4])
    altered["image_base64"] = base64.b64encode(b"not the page PNG").decode()
    assert (
        pdf_ocr_reuse.find_verified_reuse(
            pair[0], pair[2], stored(pair[0], pair[2])["manifest"]["pages"][0], altered
        )
        is None
    )


@pytest.mark.parametrize("which", ["donor", "target"])
def test_candidate_is_rechecked_in_transaction_before_reuse_commit(pair, which):
    candidate = find(pair)
    assert candidate
    service, donor, target, *_ = pair
    with service.db.transaction() as conn:
        assert pdf_ocr_reuse.candidate_is_current(conn, target, candidate)
        identifier = donor["id"] if which == "donor" else target["id"]
        conn.execute("UPDATE documents SET version=version+1 WHERE id=?", (identifier,))
        assert not pdf_ocr_reuse.candidate_is_current(conn, target, candidate)


def test_target_lease_expiry_after_verified_candidate_stops_provider_and_reuse(pair, monkeypatch):
    service, _donor, target, _content, _rendered, requests = pair
    original = pdf_ocr_reuse.find_verified_reuse

    def expire(*args):
        candidate = original(*args)
        with service.db.transaction() as conn:
            conn.execute("UPDATE processing_jobs SET lease_until=1 WHERE document_id=?", (target["id"],))
        return candidate

    monkeypatch.setattr(pdf_ocr_reuse, "find_verified_reuse", expire)
    after = process(service, target)
    assert len(requests) == 1
    assert after["manifest"]["pages"][0]["status"] == "pending_ocr"
    assert not after["manifest"]["pages"][0].get("ocr", {}).get("reuse")


def test_request_intent_and_profile_are_checkpointed_before_provider(enabled, monkeypatch):
    content = make_pdf([(NATIVE, True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    states = []
    requested = []

    def provider(rendered):
        assert len(states) == 1
        state = states[0]
        assert state["status"] == "pending_ocr"
        assert (
            state["ocr"]["attempts"] == 1
            and state["ocr"]["provider_profile"] == pdf_ocr_reuse.current_profile()
        )
        assert state["ocr"]["image_blob"]["content_hash"] == rendered["image_sha256"]
        requested.append(True)
        return {
            "text": NATIVE,
            "printed_page": None,
            "is_blank": False,
            "complete": True,
            "uncertain_spans": [],
            "finish_reason": "stop",
        }

    monkeypatch.setattr(vision, "transcribe_page", provider)
    result = parsing._ocr_one_page(
        content,
        page,
        on_page=lambda p: states.append(deepcopy(p)),
        on_image=lambda r: {"content_hash": r["image_sha256"]},
    )
    assert requested == [True] and result["status"] == "ocr_complete"


@pytest.mark.parametrize("error_type", [ValueError, RuntimeError])
def test_request_checkpoint_failure_stops_provider(enabled, monkeypatch, error_type):
    requests = fake_provider(monkeypatch, payload(text=NATIVE))
    content = make_pdf([(NATIVE, True)])

    def fail(_page):
        raise error_type("request intent unavailable")

    with pytest.raises(error_type, match="request intent unavailable"):
        parsing._ocr_one_page(content, parse_file("scan.pdf", content)["pages"][0], on_page=fail)
    assert requests == []


def test_crash_checkpoint_preserves_unknown_paid_intent_without_reissuing(enabled, monkeypatch):
    content = make_pdf([(NATIVE, True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    checkpoints = []
    calls = []

    def interrupted(_rendered):
        calls.append(True)
        raise RuntimeError("process interrupted during provider request")

    monkeypatch.setattr(vision, "transcribe_page", interrupted)
    with pytest.raises(RuntimeError, match="process interrupted"):
        parsing._ocr_one_page(content, page, on_page=lambda p: checkpoints.append(deepcopy(p)))
    page = checkpoints[-1]
    assert page["ocr"]["attempts"] == 1 and page["status"] == "pending_ocr"
    monkeypatch.setattr(
        parsing, "render_pdf_page_bounded", lambda *_: pytest.fail("unknown request rendered")
    )
    held = parsing._ocr_one_page(content, page)
    assert held["status"] == "ocr_failed" and held["ocr"]["attempts"] == 1 and len(calls) == 1
    assert held["ocr"]["request_outcome"] == "unknown_after_interruption"
    assert held["ocr_attempt_history"][0]["previous_page"] == page
    assert parsing._ocr_one_page(content, held) == held and len(calls) == 1


@pytest.mark.parametrize("attempt", [-1, 4, True, "1"])
def test_invalid_attempt_counter_never_resets_or_sends_request(enabled, monkeypatch, attempt):
    content = make_pdf([(NATIVE, True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    page["ocr"] = {"attempts": attempt}
    monkeypatch.setattr(vision, "transcribe_page", lambda *_: pytest.fail("invalid counter requested"))
    with pytest.raises(ValueError, match="尝试次数无效"):
        parsing._ocr_one_page(content, page)


def test_unverified_visibility_masks_native_hint_without_changing_source_evidence(enabled, monkeypatch):
    requests = fake_provider(monkeypatch, payload(text="Visible source page."))
    content = make_pdf([("Hidden Bill 2022 source evidence. " * 20, True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    original_native = page["native_text"]
    page["native_visibility"] = {"requires_visual_review": True, "reasons": ["text_outside_clip"]}
    result = parsing._ocr_one_page(content, page)
    hint_part = requests[0]["messages"][1]["content"][1]["text"]
    assert "Hidden Bill" not in hint_part and hint_part.endswith('""')
    assert result["native_text"] == original_native
    assert result["ocr"]["native_hint_strategy"] == "masked-unverified-native-hint-v1"
    assert result["ocr"]["native_hint_chars"] == 0
    assert result["ocr"]["native_hint_hash"] == hashlib.sha256(b"").hexdigest()
    assert "transcription_much_shorter_than_native_text" not in result["ocr"]["uncertain_spans"]
    assert "masked-unverified-native-hint-v1" in result["ocr"]["provider_profile"]["request_recipe"]


def test_reuse_keeps_the_four_page_batch_bound(service, enabled, monkeypatch):
    requests = fake_provider(monkeypatch, payload(text=NATIVE))
    content = make_pdf([(NATIVE, True)] * 6)
    donor = register(service, content, key="six-pages-donor", country="ID")
    target = register(service, content, key="six-pages-target", country="SG")
    process(service, donor)
    assert len(requests) == 4
    after = process(service, target)
    assert len(requests) == 4
    assert after["manifest"]["quality"]["completed_pages"] == [1, 2, 3, 4]
    assert after["manifest"]["quality"]["pending_pages"] == [5, 6]
    assert not text_complete(after["manifest"])


def test_masked_hint_does_not_remove_blank_response_independent_doubt(enabled, monkeypatch):
    response = payload(text="")
    response["choices"][0]["message"]["content"] = json.dumps(
        {"complete": True, "printed_page": None, "text": "", "uncertain_spans": [], "is_blank": True}
    )
    requests = fake_provider(monkeypatch, response)
    content = make_pdf([(NATIVE, True)])
    page = parse_file("scan.pdf", content)["pages"][0]
    page["native_visibility"] = {"requires_visual_review": True, "reasons": ["text_outside_clip"]}
    result = parsing._ocr_one_page(content, page)
    assert requests[0]["messages"][1]["content"][1]["text"].endswith('""')
    assert result["native_text"] == NATIVE
    assert result["status"] == "ocr_uncertain"
    assert "blank_response_conflicts_with_native_text" in result["ocr"]["uncertain_spans"]


@pytest.mark.parametrize("metadata", [[], False, "broken metadata"])
def test_malformed_target_metadata_is_not_treated_as_never_attempted(metadata):
    page = {
        "page": 1,
        "source_hash": "a" * 64,
        "native_text": "source",
        "text": "source",
        "status": "pending_ocr",
        "reasons": ["large_raster"],
        "ocr": metadata,
    }
    assert not pdf_ocr_reuse.pristine_pending(page)
    with pytest.raises(ValueError, match="检查点格式无效"):
        parsing._ocr_one_page(b"source", page)


def claim(service, *documents):
    return service.collection.claim_processing(
        owner_id=documents[0]["owner_id"], document_ids=[d["id"] for d in documents]
    )


def job(service, document):
    with closing(service.db.connect()) as conn:
        return dict(
            conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (document["id"],)).fetchone()
        )


def test_concurrent_claims_serialize_same_source_parse_before_any_model_call(service):
    service.collection.processing_workers = 2
    content = make_pdf([(NATIVE, True)])
    a = register(service, content, key="source-ID", country="ID")
    b = register(service, content, key="source-SG", country="SG")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim(service, a, b), range(2)))
    assert sum(item is not None for item in claims) == 1
    assert sorted(job(service, d)["status"] for d in (a, b)) == ["processing", "queued"]


def test_other_original_can_fill_the_second_slot_while_same_source_waits(service):
    service.collection.processing_workers = 2
    content = make_pdf([(NATIVE, True)])
    a = register(service, content, key="shared-first")
    b = register(service, content, key="shared-second", country="SG")
    other = register(service, make_pdf([("Another source original.", True)]), key="other-original")
    assert claim(service, a)["document_id"] == a["id"]
    second = claim(service, b, other)
    assert second["document_id"] == other["id"]
    assert job(service, b)["status"] == "queued"
    assert claim(service, b) is None  # The global two-slot bound is unchanged.


@pytest.mark.parametrize("difference", ["owner", "source_url", "version"])
def test_different_source_identity_is_not_serialized(service, difference):
    service.collection.processing_workers = 2
    content = make_pdf([(NATIVE, True)])
    a = register(service, content, key="first")
    b = register(
        service,
        content,
        key="second",
        country="SG",
        owner="other" if difference == "owner" else "owner",
        url="https://official.invalid/alternate.pdf" if difference == "source_url" else SOURCE,
    )
    if difference == "version":
        with service.db.transaction() as conn:
            conn.execute("UPDATE documents SET version=2 WHERE id=?", (b["id"],))
    assert claim(service, a)
    assert claim(service, b)


def test_expired_same_source_lease_does_not_block_scoped_target_or_mutate_donor(service):
    content = make_pdf([(NATIVE, True)])
    a = register(service, content, key="expired")
    b = register(service, content, key="target", country="SG")
    assert claim(service, a)
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE processing_jobs SET lease_until=? WHERE document_id=?", (time.time() - 1, a["id"])
        )
    before = job(service, a)
    assert claim(service, b)
    assert job(service, a) == before


@pytest.mark.parametrize("first_phase", ["parse", "embed"])
def test_same_source_serialization_preserves_embedding_phase_fairness(service, first_phase):
    service.collection.processing_workers = 2
    content = make_pdf([(NATIVE, True)])
    a = register(service, content, key="first")
    b = register(service, content, key="second", country="SG")
    with service.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET phase=? WHERE document_id=?", (first_phase, a["id"]))
        conn.execute(
            "UPDATE processing_jobs SET phase=? WHERE document_id=?",
            ("embed" if first_phase == "parse" else "parse", b["id"]),
        )
    assert claim(service, a)["phase"] == first_phase
    assert claim(service, b)["phase"] != first_phase


def test_secondary_same_source_waits_then_remains_zero_vectors_without_models(service):
    content = make_pdf([(NATIVE, True)])
    a = register(service, content, key="primary")
    b = register(service, content, key="supporting", country="SG", level="secondary")
    assert claim(service, a) and claim(service, b) is None
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE processing_jobs SET status='completed',lease_token=NULL,lease_until=NULL WHERE document_id=?",
            (a["id"],),
        )
    picked = claim(service, b)
    assert picked
    service.collection.process_item(picked)
    with closing(service.db.connect()) as conn:
        assert not service.archive.can_index(conn, b["id"])
        assert not conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0]
