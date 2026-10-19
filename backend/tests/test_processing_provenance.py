"""Derived text rebuilds retain mapped official evidence without granting authority."""

import hashlib
import json
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest

from app.db import dump
from test_archive_page_recovery import case as case
from test_collection_import_provenance import imported, pdf_manifest, saved
from test_collection_import_provenance import pair as pair

KEYS = ("collection_import", "collection_evidence", "gazette_publication")


def official_document(pair):
    source, _ = pair
    raw = source.archive.blobs.put_bytes("source", b"Frozen official catalogue response")
    initial = {
        "collection_evidence": {
            "record": {
                "catalogue": [
                    {
                        "raw": {"cas_path": str(raw.path), "sha256": raw.digest, "size": raw.size},
                        "locator": {"json_pointer": "/records/0", "objectID": "official-record-1"},
                    }
                ]
            }
        },
        "gazette_publication": {
            "title": "Official published law",
            "publishYear": 2026,
            "publishTimestamp": 1767225600000,
            "notificationNum": "S 1/2026",
            "classification": {"level": "primary", "basis": "legislative-catalogue-pending-review"},
            "current_effect_verified": False,
        },
    }
    _, target, doc, _ = imported(pair, initial=initial)
    current = saved(target, doc["id"])
    expected = {key: deepcopy(current[key]) for key in KEYS}
    reference = expected["collection_evidence"]["record"]["catalogue"][0]["raw"]
    assert not Path(reference["cas_path"]).is_absolute()
    assert str(source.db.data_dir) not in dump(expected)
    return target, doc, expected


def assert_preserved(target, doc, expected):
    current = saved(target, doc["id"])
    assert {key: current[key] for key in KEYS} == expected
    reference = current["collection_evidence"]["record"]["catalogue"][0]["raw"]
    raw = (target.db.data_dir / reference["cas_path"]).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == reference["sha256"]
    assert target.catalog.metadata_for(doc["id"])["metadata"]["review_status"] == "draft"
    assert target.search("target", "Paid OCR", country="ID")["items"] == []


@pytest.mark.parametrize("archive", [False, True])
def test_native_parse_and_archive_window_preserve_official_provenance(pair, monkeypatch, archive):
    from app import archive_parsing, parsing, vision

    target, doc, expected = official_document(pair)
    if archive:
        initial = saved(target, doc["id"])
        initial.update(pdf_manifest(doc, archive=True))
        initial["pages"][0]["status"] = "pending_parse"
        with target.db.transaction() as conn:
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(initial), doc["id"]))

    calls = []

    def parse(*_args, **kwargs):
        calls.append("archive" if "manifest" in kwargs else "fresh")
        if archive:
            # The captured provenance must be independent of parser input.
            kwargs["manifest"]["collection_evidence"]["record"]["catalogue"][0]["raw"]["cas_path"] = (
                "parser-mutated-path"
            )
            kwargs["manifest"]["gazette_publication"]["classification"]["level"] = "secondary"
        return pdf_manifest(doc, archive=archive) | {key: {"forged": True} for key in KEYS}

    monkeypatch.setattr(parsing, "parse_bounded", parse)
    monkeypatch.setattr(archive_parsing, "parse_archive_pdf_bounded", parse)
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: False)
    target.collection.process_item(
        target.collection.claim_processing(owner_id="target", document_ids=[doc["id"]])
    )
    assert calls == ["archive" if archive else "fresh"]
    assert_preserved(target, doc, expected)


def test_ocr_checkpoint_and_reassembly_keep_official_provenance(pair, monkeypatch):
    from app import parsing, vision

    target, doc, expected = official_document(pair)
    monkeypatch.setattr(parsing, "parse_bounded", lambda *_a, **_k: pdf_manifest(doc))
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    checkpoints = []

    def ocr(_name, _content, _mime, manifest, *, on_page, **_kwargs):
        assert {key: manifest[key] for key in KEYS} == expected
        result = pdf_manifest(doc, complete=True)
        on_page(result["pages"][0])
        assert_preserved(target, doc, expected)
        checkpoints.append(True)
        # OCR page reassembly returns only derived content; fake provenance
        # supplied by a parser is not an authorization or a locator update.
        result.update({key: {"forged": True} for key in KEYS})
        return result

    monkeypatch.setattr(parsing, "ocr_bounded", ocr)
    target.collection.process_item(
        target.collection.claim_processing(owner_id="target", document_ids=[doc["id"]])
    )
    assert checkpoints == [True]
    assert saved(target, doc["id"])["pages"][0]["status"] == "ocr_complete"
    assert_preserved(target, doc, expected)


def test_parser_cannot_inject_missing_official_provenance(pair, monkeypatch):
    from app import parsing, vision

    _, target, doc, origin = imported(pair)
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: False)
    monkeypatch.setattr(
        parsing,
        "parse_bounded",
        lambda *_a, **_k: (
            pdf_manifest(doc) | {key: {"forged": True} for key in (*KEYS, "native_page_recovery")}
        ),
    )
    target.collection.process_item(
        target.collection.claim_processing(owner_id="target", document_ids=[doc["id"]])
    )
    current = saved(target, doc["id"])
    assert current["collection_import"] == origin
    assert "collection_evidence" not in current and "gazette_publication" not in current
    assert "native_page_recovery" not in current
    assert target.catalog.metadata_for(doc["id"])["metadata"]["review_status"] == "draft"


def test_real_archive_processing_retains_native_recovery_failure_and_page_cas(case, monkeypatch):
    from app import archive_parsing, vision
    from app.archive_page_recovery import recover_native_page_shards
    from test_archive_page_recovery import arguments

    service, ident = case["service"], case["document_id"]

    def manifest():
        with closing(service.db.connect()) as conn:
            return json.loads(service.own_document(conn, case["owner"], ident)["manifest"])

    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: False)
    recover_native_page_shards(**arguments(case, apply=True))
    before = manifest()
    expected = deepcopy(before["native_page_recovery"])
    real_parser = archive_parsing.parse_archive_pdf_bounded
    calls = []

    def parse(*args, **kwargs):
        calls.append(True)
        # The actual file-backed parser rebuilds the whole document. Mutating
        # its input or returning a forged proof must not replace stored CAS.
        kwargs["manifest"]["native_page_recovery"]["evidence_blobs"][0]["storage_path"] = "changed"
        return real_parser(*args, **kwargs) | {"native_page_recovery": {"forged": True}}

    monkeypatch.setattr(archive_parsing, "parse_archive_pdf_bounded", parse)
    claim = service.collection.claim_processing(owner_id=case["owner"], document_ids=[ident])
    assert claim is not None
    service.collection.process_item(claim)
    current = manifest()
    assert calls == [True] and current["native_page_recovery"] == expected
    assert current["pages"] == before["pages"]
    assert len({chunk["id"] for chunk in current["chunks"]}) == len(current["chunks"])
    assert case["expected_manifest"] in {blob["content_hash"] for blob in expected["evidence_blobs"]}
    for reference in expected["evidence_blobs"]:
        raw = (service.db.data_dir / reference["storage_path"]).read_bytes()
        assert len(raw) == reference["size"]
        assert hashlib.sha256(raw).hexdigest() == reference["content_hash"]
