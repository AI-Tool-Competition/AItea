"""Reusing a paid page result cannot replace unrelated evidence or bypass gates."""

import base64
import hashlib
import importlib.util
import json
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest

from app import parsing, vision
from app.blob_store import BlobStore
from app.db import dump, now
from app.ingestion_quality import text_complete
from app.ocr_page_retry import prepare_physical_page_retry
from app.page_checkpoint_recovery import (
    apply_page_checkpoint,
    apply_page_checkpoints,
    canonical_hash,
    isolated_directory,
    manifest_hash,
    quarantine_scope_pages,
)
from app.service import Service
from test_ocr_page_retry import legacy_manifest
from test_vision_ocr import enabled, fake_provider, payload  # noqa: F401


def image_reference(store, directory, owner, image):
    blob = store.put_bytes(owner, image)
    return {
        "storage_path": str(blob.path.relative_to(directory)),
        "content_hash": blob.digest,
        "size": blob.size,
        "mime_type": "image/png",
    }


@pytest.fixture
def recovery(tmp_path, monkeypatch, request):
    request.getfixturevalue("enabled")
    for key in ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL"):
        monkeypatch.setenv(key, "test-value")
    service = Service(tmp_path / "target")
    content, source = legacy_manifest()
    checkpoints = tmp_path / "checkpoints"
    store = BlobStore(checkpoints / "files")
    image = base64.b64decode(parsing.render_pdf_page_bounded(content, 1)["image_base64"])
    source["pages"][0]["ocr"]["image_blob"] = image_reference(store, checkpoints, "source-owner", image)
    current = deepcopy(source)
    current["pages"][0]["ocr"]["image_blob"] = image_reference(
        service.archive.blobs, service.db.data_dir, "target-owner", image
    )
    current["collection_import"] = {"protocol": "saved-source-provenance", "source_document_id": "source-id"}
    current["collection_evidence"] = {"catalogue": "retained source evidence"}
    with service.db.transaction() as conn:
        conn.execute(
            "INSERT INTO users VALUES(?,?,?,?,?,?)", ("target-owner", "target-owner", None, None, 1, now())
        )
        blob = service.archive.blobs.put_bytes("target-owner", content)
        document = dict(
            service.archive.store_original(
                conn,
                "target-owner",
                blob,
                "law.pdf",
                "application/pdf",
                "https://official.example/law.pdf",
                collection="country:ID",
                level="primary",
                tags=["kind:law"],
                identity_key="law",
                kind_hint="law",
            )
        )
        conn.execute(
            "UPDATE documents SET manifest=?,status='failed' WHERE id=?", (dump(current), document["id"])
        )
        service.collection.queue_processing(conn, document["id"], "target-owner")
        conn.execute("UPDATE processing_jobs SET status='failed' WHERE document_id=?", (document["id"],))
        service.index.sync_document(conn, document["id"], current)
    requests = fake_provider(monkeypatch, payload(text="Pasal 57\nGubernur Nomor"))
    prepared = prepare_physical_page_retry(source, [1], source_hash=source["source_hash"])
    after = parsing._ocr_one_page(
        content,
        prepared["pages"][0],
        on_image=lambda rendered: image_reference(
            store, checkpoints, "source-owner", base64.b64decode(rendered["image_base64"])
        ),
    )
    assert len(requests) == 1 and after["status"] == "ocr_complete"
    return {
        "service": service,
        "owner": "target-owner",
        "document_id": document["id"],
        "source_hash": source["source_hash"],
        "version": 1,
        "expected_manifest": manifest_hash(dump(current)),
        "checkpoint": after,
        "checkpoint_dir": checkpoints,
        "checkpoint_owner": "source-owner",
        "before": current,
        "requests": requests,
    }


def apply(recovery, **options):
    values = {key: value for key, value in recovery.items() if key not in {"before", "requests"}}
    values.update(options)
    return apply_page_checkpoint(**values)


def facts(recovery):
    with closing(recovery["service"].db.connect()) as conn:
        return {
            table: [tuple(row) for row in conn.execute("SELECT * FROM " + table)]
            for table in (
                "documents",
                "document_metadata",
                "document_metadata_history",
                "legal_classifications",
                "legal_relations",
                "processing_jobs",
                "retrieval_vectors",
                "retrieval_documents",
                "retrieval_chunks",
            )
        }


def test_dry_run_and_apply_reuse_pixels_without_calls_or_changing_other_evidence(recovery, monkeypatch):
    monkeypatch.setattr(
        vision, "transcribe_page", lambda *_: pytest.fail("checkpoint application called OCR")
    )
    before = facts(recovery)
    report = apply(recovery)
    assert report["text_complete"] and report["database_writes"] == report["model_calls"] == 0
    assert facts(recovery) == before
    report = apply(recovery, apply=True)
    after = facts(recovery)
    for table in (
        "document_metadata",
        "document_metadata_history",
        "legal_classifications",
        "legal_relations",
    ):
        assert after[table] == before[table]
    with closing(recovery["service"].db.connect()) as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (recovery["document_id"],)).fetchone()
        manifest = json.loads(row["manifest"])
        job = conn.execute(
            "SELECT * FROM processing_jobs WHERE document_id=?", (recovery["document_id"],)
        ).fetchone()
        assert job["phase"] == "embed" and job["status"] == "queued"
    assert text_complete(manifest)
    assert row["content_hash"] == recovery["source_hash"] and row["version"] == 1
    assert manifest["pages"][1] == recovery["before"]["pages"][1]
    assert manifest["collection_import"] == recovery["before"]["collection_import"]
    assert manifest["collection_evidence"] == recovery["before"]["collection_evidence"]
    proof = manifest["pages"][0]["ocr_retry_history"][0]
    assert proof["previous_page"] == recovery["before"]["pages"][0]
    assert proof["previous_page_sha256"] == canonical_hash(proof["previous_page"])
    assert (
        proof["source_previous_page_sha256"]
        == recovery["checkpoint"]["ocr_retry_history"][0]["previous_page_sha256"]
    )
    assert len(recovery["requests"]) == 1 and report["database_writes"] == 1
    with pytest.raises(ValueError, match="manifest_conflict"):
        apply(recovery, apply=True)


@pytest.mark.parametrize(
    "option,value,reason",
    [
        ("owner", "wrong-owner", "not_owned"),
        ("source_hash", "f" * 64, "version_conflict"),
        ("version", 2, "version_conflict"),
        ("expected_manifest", "f" * 64, "manifest_conflict"),
        ("checkpoint_owner", "wrong-owner", "账号"),
    ],
)
def test_wrong_owner_or_stale_fence_cannot_write(recovery, option, value, reason):
    before = facts(recovery)
    with pytest.raises(ValueError, match=reason):
        apply(recovery, apply=True, **{option: value})
    assert facts(recovery) == before


@pytest.mark.parametrize(
    "mutation", ["old_hash", "model_false", "changed_number", "pixels", "different_source"]
)
def test_success_flag_cannot_hide_mismatched_evidence(recovery, mutation):
    checkpoint = deepcopy(recovery["checkpoint"])
    if mutation == "old_hash":
        checkpoint["ocr_retry_history"][0]["previous_page_sha256"] = "f" * 64
    elif mutation == "model_false":
        checkpoint["ocr"]["complete"] = False
    elif mutation == "changed_number":
        checkpoint["text"] = checkpoint["ocr"]["text"] = "Pasal 51\nGubernur Nomor"
    elif mutation == "different_source":
        checkpoint["source_hash"] = "f" * 64
    else:
        wrong = b"different bytes presented as the physical page"
        checkpoint["ocr"]["image_blob"] = image_reference(
            BlobStore(recovery["checkpoint_dir"] / "files"),
            recovery["checkpoint_dir"],
            "source-owner",
            wrong,
        )
        checkpoint["ocr"]["image_sha256"] = hashlib.sha256(wrong).hexdigest()
    before = facts(recovery)
    with pytest.raises(ValueError):
        apply(recovery, checkpoint=checkpoint, apply=True)
    assert facts(recovery) == before


@pytest.mark.parametrize("active", ["lease", "published"])
def test_active_or_published_document_is_not_rewritten(recovery, active):
    with recovery["service"].db.transaction() as conn:
        if active == "lease":
            conn.execute(
                "UPDATE processing_jobs SET status='processing',lease_token='other-worker',lease_until=9999999999"
            )
        else:
            conn.execute(
                "UPDATE document_metadata SET payload=json_set(payload,'$.review_status','published')"
            )
    before = facts(recovery)
    with pytest.raises(ValueError, match="lease_active|requires_draft"):
        apply(recovery, apply=True)
    assert facts(recovery) == before


def test_cli_default_project_data_is_never_a_recovery_target(tmp_path):
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="default_project_data_refused"):
        isolated_directory(root / "data")


def test_cli_dry_run_does_not_initialize_or_migrate_service(recovery, monkeypatch, capsys):
    path = Path(__file__).resolve().parents[2] / "scripts" / "recover_page_scope.py"
    spec = importlib.util.spec_from_file_location("recovery_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    checkpoint_path = recovery["checkpoint_dir"] / "page.json"
    checkpoint_path.write_text(dump(recovery["checkpoint"]))
    monkeypatch.setattr("app.service.Service", lambda *_: pytest.fail("dry run initialized writable service"))
    monkeypatch.setattr(
        "sys.argv",
        [
            str(path),
            "apply-checkpoint",
            "--data-dir",
            str(recovery["service"].db.data_dir),
            "--owner",
            recovery["owner"],
            "--document-id",
            recovery["document_id"],
            "--source-hash",
            recovery["source_hash"],
            "--version",
            "1",
            "--expected-manifest-sha256",
            recovery["expected_manifest"],
            "--checkpoint",
            str(checkpoint_path),
            "--checkpoint-dir",
            str(recovery["checkpoint_dir"]),
            "--checkpoint-owner",
            "source-owner",
        ],
    )
    before = facts(recovery)
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out)["database_writes"] == 0
    assert facts(recovery) == before


@pytest.mark.parametrize("extra,expected", [([], 0), (["--apply"], 2), (["--page", "2"], 2)])
def test_cli_prepare_is_readonly_and_does_not_clear_successful_pages(
    recovery, monkeypatch, capsys, extra, expected
):
    path = Path(__file__).resolve().parents[2] / "scripts" / "recover_page_scope.py"
    spec = importlib.util.spec_from_file_location("recovery_prepare_cli", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setattr("app.service.Service", lambda *_: pytest.fail("prepare initialized writable service"))
    monkeypatch.setattr(
        "sys.argv",
        [
            str(path),
            "prepare",
            "--data-dir",
            str(recovery["service"].db.data_dir),
            "--owner",
            recovery["owner"],
            "--document-id",
            recovery["document_id"],
            "--source-hash",
            recovery["source_hash"],
            "--version",
            "1",
            "--expected-manifest-sha256",
            recovery["expected_manifest"],
            "--page",
            "1",
            *extra,
        ],
    )
    before = facts(recovery)
    assert cli.main() == expected
    report = json.loads(capsys.readouterr().out)
    if expected == 0:
        assert len(report["prepared_pages"]) == 1
        assert report["prepared_pages"][0]["status"] == "pending_ocr"
        assert report["database_writes"] == report["model_calls"] == 0
    assert facts(recovery) == before


def test_batch_applies_two_pages_atomically_with_one_rebuild(recovery, monkeypatch):
    from app.ingestion_quality import validate_ocr_page

    manifest = deepcopy(recovery["before"])
    second = manifest["pages"][1]
    second["status"] = "ocr_uncertain"
    second["ocr"].update(complete=False, uncertain_spans=["model_reported_incomplete"])
    validate_ocr_page(second)
    with recovery["service"].db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), recovery["document_id"]))
    recovery["expected_manifest"] = manifest_hash(dump(manifest))
    prepared = prepare_physical_page_retry(manifest, [2], source_hash=recovery["source_hash"])
    monkeypatch.setattr(
        vision,
        "transcribe_page",
        lambda _: {
            "text": second["text"],
            "complete": True,
            "is_blank": False,
            "printed_page": None,
            "uncertain_spans": [],
            "finish_reason": "stop",
        },
    )
    with closing(recovery["service"].db.connect()) as conn:
        doc = conn.execute(
            "SELECT storage_path,size FROM documents WHERE id=?", (recovery["document_id"],)
        ).fetchone()
    after = parsing._ocr_one_page(
        None,
        prepared["pages"][1],
        archive_path=doc["storage_path"],
        archive_size=doc["size"],
        on_image=lambda rendered: image_reference(
            BlobStore(recovery["checkpoint_dir"] / "files"),
            recovery["checkpoint_dir"],
            "source-owner",
            base64.b64decode(rendered["image_base64"]),
        ),
    )
    arguments = {
        key: value for key, value in recovery.items() if key not in {"before", "requests", "checkpoint"}
    }
    invalid = deepcopy(after)
    invalid["ocr"]["complete"] = False
    before = facts(recovery)
    with pytest.raises(ValueError):
        apply_page_checkpoints(**arguments, checkpoints=[recovery["checkpoint"], invalid], apply=True)
    assert facts(recovery) == before
    rebuild = parsing.rebuild_pdf_bounded
    calls = []

    def counted(value):
        calls.append(True)
        return rebuild(value)

    monkeypatch.setattr(parsing, "rebuild_pdf_bounded", counted)
    report = apply_page_checkpoints(**arguments, checkpoints=[recovery["checkpoint"], after], apply=True)
    assert report["physical_pages"] == [1, 2] and report["text_complete"]
    assert len(calls) == 1


@pytest.mark.parametrize("count", [0, 2, 21])
def test_batch_rejects_empty_duplicate_and_excess_pages(recovery, count):
    arguments = {
        key: value for key, value in recovery.items() if key not in {"before", "requests", "checkpoint"}
    }
    before = facts(recovery)
    with pytest.raises(ValueError, match="1_to_20_unique"):
        apply_page_checkpoints(**arguments, checkpoints=[recovery["checkpoint"]] * count, apply=True)
    assert facts(recovery) == before


@pytest.mark.parametrize("text", ["Pasal 57\nGubernurs Nomor", "| Pasal 57 | Gubernur Nomor |"])
def test_direct_checkpoint_application_cannot_bypass_scope_content_and_cell_gate(recovery, text):
    checkpoint = deepcopy(recovery["checkpoint"])
    checkpoint["text"] = checkpoint["ocr"]["text"] = text
    before = facts(recovery)
    with pytest.raises(ValueError, match="scope_only_content_or_table_review_required"):
        apply(recovery, checkpoint=checkpoint, apply=True)
    assert facts(recovery) == before


@pytest.fixture
def previously_applied_changed_page(recovery):
    apply(recovery, apply=True)
    service = recovery["service"]
    with closing(service.db.connect()) as conn:
        raw = conn.execute(
            "SELECT manifest FROM documents WHERE id=?", (recovery["document_id"],)
        ).fetchone()[0]
    manifest = json.loads(raw)
    page = manifest["pages"][0]
    page["text"] = page["ocr"]["text"] = "Pasal 57\nGubernurs Nomor"
    manifest.update(parsing.rebuild_pdf_bounded(manifest))
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), recovery["document_id"]))
        service.index.sync_document(conn, recovery["document_id"], manifest)
    return (
        recovery,
        manifest,
        {
            "service": service,
            "owner": recovery["owner"],
            "document_id": recovery["document_id"],
            "source_hash": recovery["source_hash"],
            "version": 1,
            "expected_manifest": manifest_hash(dump(manifest)),
            "page_hashes": {1: canonical_hash(manifest["pages"][0])},
        },
    )


def test_quarantine_preserves_responses_and_metadata_but_blocks_retrieval_and_retries(
    previously_applied_changed_page, monkeypatch
):
    recovery, original, arguments = previously_applied_changed_page
    monkeypatch.setattr(vision, "transcribe_page", lambda *_: pytest.fail("quarantine invoked OCR"))
    before = facts(recovery)
    report = quarantine_scope_pages(**arguments)
    assert report["database_writes"] == report["model_calls"] == 0
    assert facts(recovery) == before
    report = quarantine_scope_pages(**arguments, apply=True)
    after = facts(recovery)
    assert report["database_writes"] == 1 and not report["text_complete"]
    for table in (
        "document_metadata",
        "document_metadata_history",
        "legal_classifications",
        "legal_relations",
    ):
        assert after[table] == before[table]
    with closing(recovery["service"].db.connect()) as conn:
        document = conn.execute("SELECT * FROM documents WHERE id=?", (recovery["document_id"],)).fetchone()
        manifest = json.loads(document["manifest"])
        assert not text_complete(manifest) and document["status"] == "failed"
        assert not conn.execute("SELECT complete FROM retrieval_documents").fetchone()[0]
        assert conn.execute("SELECT status FROM processing_jobs").fetchone()[0] == "failed"
        assert not recovery["service"].index.pending(conn, recovery["document_id"], "any-fingerprint")
    assert manifest["pages"][1] == original["pages"][1]
    page, previous = manifest["pages"][0], original["pages"][0]
    assert page["text"] == previous["text"] and page["ocr"]["complete"] is True
    assert page["ocr_retry_history"] == previous["ocr_retry_history"]
    assert page["ocr"]["image_blob"] == previous["ocr"]["image_blob"]
    assert page["scope_retry_review"]["prior_checkpoint_sha256"] == canonical_hash(previous)
    with pytest.raises(ValueError, match="manifest_conflict"):
        quarantine_scope_pages(**arguments, apply=True)


@pytest.mark.parametrize("change", ["owner", "version", "page_hash", "history_hash", "active_job"])
def test_quarantine_refuses_unowned_stale_or_ambiguous_changes(previously_applied_changed_page, change):
    recovery, original, arguments = previously_applied_changed_page
    if change == "owner":
        arguments["owner"] = "unrelated"
    elif change == "version":
        arguments["version"] = 2
    elif change == "page_hash":
        arguments["page_hashes"] = {1: "f" * 64}
    elif change == "history_hash":
        original["pages"][0]["ocr_retry_history"][0]["previous_page_sha256"] = "f" * 64
        with recovery["service"].db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET manifest=? WHERE id=?", (dump(original), recovery["document_id"])
            )
        arguments["expected_manifest"] = manifest_hash(dump(original))
        arguments["page_hashes"] = {1: canonical_hash(original["pages"][0])}
    else:
        with recovery["service"].db.transaction() as conn:
            conn.execute("UPDATE processing_jobs SET status='processing',lease_token='active'")
    before = facts(recovery)
    with pytest.raises(ValueError):
        quarantine_scope_pages(**arguments, apply=True)
    assert facts(recovery) == before
