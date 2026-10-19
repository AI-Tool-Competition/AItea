"""Page recovery invokes only explicitly selected pages, once per saved intent."""

from copy import deepcopy
import json

import pytest

from app.page_checkpoint_recovery import canonical_hash
from app.page_scope_execution import PROTOCOL, compare_page, execute, preflight
from test_page_checkpoint_recovery import facts, recovery  # noqa: F401
from test_vision_ocr import enabled  # noqa: F401


@pytest.fixture
def plan(request):
    recovery_case = request.getfixturevalue("recovery")
    return {
        "protocol": PROTOCOL,
        "owner_id": recovery_case["owner"],
        "documents": [
            {
                "document_id": recovery_case["document_id"],
                "source_hash": recovery_case["source_hash"],
                "version": 1,
                "expected_manifest_sha256": recovery_case["expected_manifest"],
                "physical_pages": [1],
            }
        ],
    }


def test_preflight_does_not_write_or_call_and_execution_only_calls_selected_page(plan, request, tmp_path):
    case = request.getfixturevalue("recovery")
    before, calls = facts(case), len(case["requests"])
    _, report = preflight(case["service"], plan)
    assert report["pages"] == 1 and report["model_calls"] == 0
    assert facts(case) == before and len(case["requests"]) == calls
    report = execute(case["service"], plan, tmp_path / "report", concurrency=1)
    assert report["new_request_intents"] == report["applied_pages"] == 1
    assert report["status"] == "selected_pages_complete"
    assert len(case["requests"]) == calls + 1
    assert report["results"][0]["unselected_pages_unchanged"]
    assert report["results"][0]["metadata_unchanged"]
    assert report["results"][0]["text_complete"]
    with pytest.raises(ValueError, match="manifest_conflict"):
        execute(case["service"], plan, tmp_path / "same-report", concurrency=1)
    assert len(case["requests"]) == calls + 1


@pytest.mark.parametrize("saved_response", [False, True])
def test_existing_request_or_success_never_calls_provider_again(plan, request, tmp_path, saved_response):
    case = request.getfixturevalue("recovery")
    service = case["service"]
    key = canonical_hash([case["owner"], case["document_id"], 1, case["source_hash"]])
    ledger = service.db.data_dir / ".page-scope-v3"
    directory = ledger / key / "1"
    directory.mkdir(parents=True)
    (directory / "request-intent.json").write_text("{}")
    if saved_response:
        from app.blob_store import BlobStore
        from app.html_linked_asset_ocr import read_image_blob
        from test_page_checkpoint_recovery import image_reference

        checkpoint = deepcopy(case["checkpoint"])
        raw = read_image_blob(
            case["checkpoint_dir"], case["checkpoint_owner"], checkpoint["ocr"]["image_blob"]
        )
        checkpoint["ocr"]["image_blob"] = image_reference(
            BlobStore(ledger / "files"), ledger, case["owner"], raw
        )
        # The stored result must be from this target's original prepared page.
        proof = checkpoint["ocr_retry_history"][0]
        proof["previous_page"] = deepcopy(case["before"]["pages"][0])
        proof["previous_page_sha256"] = canonical_hash(proof["previous_page"])
        (directory / "page-after.json").write_text(json.dumps(checkpoint))
    before, calls = facts(case), len(case["requests"])
    report = execute(service, plan, tmp_path / "resume", concurrency=1)
    assert report["new_request_intents"] == 0 and len(case["requests"]) == calls
    assert report["applied_pages"] == int(saved_response)
    if not saved_response:
        assert facts(case) == before
        assert report["results"][0]["pages"][0]["status"] == "interrupted_request_requires_review"
        assert report["status"] == "needs_review"


@pytest.mark.parametrize("field,value", [("owner_id", "unrelated"), ("protocol", "other")])
def test_execution_rejects_unowned_or_wrong_protocol_before_requests(plan, request, tmp_path, field, value):
    case = request.getfixturevalue("recovery")
    plan[field] = value
    before, calls = facts(case), len(case["requests"])
    with pytest.raises(ValueError):
        execute(case["service"], plan, tmp_path / "refused", concurrency=1)
    assert facts(case) == before and len(case["requests"]) == calls


@pytest.mark.parametrize(
    "old,new,accepted",
    [
        ("Pasal 57 _contact center_", "Pasal 57 contact center", True),
        ("| a<br>b | 123 |", "| a b | 123 |", True),
        ("Pasal 57", "Pasal 51", False),
        ("1 a 2 b", "2 a 1 b", False),
        ("original complete visible paragraph", "paragraph", False),
        (
            "the permit is not required " * 20,
            "the permit is not required " * 19 + "the permit is required",
            False,
        ),
        ("Fine 100.000", "Fine 100,000", False),
        ("not able", "notable", False),
        ("Registrar's decision", "Registrar’s decision", True),
        ("| Pemanfaat-<br>an | Menteri/<br>Kepala |", "| Pemanfaat-an | Menteri/Kepala |", True),
        ("| perundang-undangan |", "| perundangundangan |", False),
        ("| ab | c |", "| a | bc |", False),
        ("| a | b | |", "| a | b |", False),
        ("| a | b |\n| c | d |", "| a c | b d |", False),
        ("| a | b |\n| c |", "| a | b |\n| c |", False),
        (r"| a\|b | c |", r"| a\|b | c |", True),
        ("| A | B |\n|---|---|\n| a | b |", "| A | B |\n|:---|---:|\n| a | b |", True),
    ],
)
def test_scope_repair_preserves_words_punctuation_cells_and_number_order(old, new, accepted):
    assert compare_page({"text": old}, {"text": new})["scope_only_change_accepted"] is accepted


def test_second_executor_cannot_submit_again_while_journal_locked(plan, request, tmp_path):
    import fcntl

    case = request.getfixturevalue("recovery")
    ledger = case["service"].db.data_dir / ".page-scope-v3"
    ledger.mkdir()
    before, calls = facts(case), len(case["requests"])
    with (ledger / "execution.lock").open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="executor_already_running"):
            execute(case["service"], plan, tmp_path / "blocked", concurrency=1)
    assert facts(case) == before and len(case["requests"]) == calls


@pytest.mark.parametrize("raw", [b'{"choices":', b""])
@pytest.mark.parametrize("interrupt_checkpoint", [False, True])
def test_failed_response_survives_interruption_and_never_submits_again(
    plan, request, tmp_path, monkeypatch, raw, interrupt_checkpoint
):
    import base64
    import hashlib
    from app import page_scope_execution
    from test_vision_error_evidence import raw_provider

    case = request.getfixturevalue("recovery")
    # Restore the actual Client before replacing the fixture's mock transport.
    import httpx

    monkeypatch.setattr(httpx, "Client", httpx._client.Client)
    requests = raw_provider(monkeypatch, raw)
    service = case["service"]
    before = facts(case)
    save = page_scope_execution._save

    def maybe_interrupted(path, value, **kwargs):
        if interrupt_checkpoint and path.name == "page-after.json":
            raise OSError("checkpoint interrupted after error evidence commit")
        return save(path, value, **kwargs)

    monkeypatch.setattr(page_scope_execution, "_save", maybe_interrupted)
    report = execute(service, plan, tmp_path / "failed-report", concurrency=1)
    assert report["status"] == "needs_review" and report["applied_pages"] == 0
    assert len(requests) == 1 and facts(case) == before
    ledger = service.db.data_dir / ".page-scope-v3"
    key = canonical_hash([case["owner"], case["document_id"], 1, case["source_hash"]])
    directory = ledger / key / "1"
    evidence = json.loads((directory / "error-response.json").read_bytes())
    intent = json.loads((directory / "request-intent.json").read_bytes())
    assert evidence["request_id"] == intent["ocr"]["request_id"]
    assert evidence["source_hash"] == case["source_hash"] and evidence["page"] == 1
    assert "response_base64" not in evidence
    reference = evidence["blob"]
    saved = (ledger / reference["storage_path"]).read_bytes()
    assert hashlib.sha256(saved).hexdigest() == reference["content_hash"]
    assert base64.b64decode(json.loads(saved)["response_base64"]) == raw
    assert (directory / "page-after.json").exists() is not interrupt_checkpoint
    repeated = execute(service, plan, tmp_path / "repeat", concurrency=1)
    assert repeated["new_request_intents"] == 0 and repeated["applied_pages"] == 0
    assert len(requests) == 1 and facts(case) == before
    if interrupt_checkpoint:
        assert repeated["results"][0]["pages"][0]["error_response"] == evidence
