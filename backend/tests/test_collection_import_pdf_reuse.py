"""Cross-owner OCR imports retain original proofs, paid bytes and donor closure."""

import copy
import json
from contextlib import closing

import numpy as np
import pytest

from app import data_backup, pdf_ocr_reuse, pdf_ocr_reuse_import
from app.collection_import import CollectionImportError, import_collected_resources
from app.db import dump, now
from app.service import Service
from test_collection_import import facts
from test_pdf_ocr_reuse import enabled as enabled
from test_pdf_ocr_reuse import pair as pair
from test_pdf_ocr_reuse import process, register, stored
from test_pdf_ocr_reuse import service as service


@pytest.fixture
def prepared(pair, tmp_path, monkeypatch):
    src, donor, target, content, _rendered, requests = pair
    process(src, target)
    secondary = register(src, content, key="secondary", country="SG", level="secondary")
    process(src, secondary)
    for key in ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL"):
        monkeypatch.setenv(key, "test-value")
    with src.db.transaction() as conn:
        for doc in (donor, target):
            manifest = json.loads(
                conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
            )
            manifest["embedding_fingerprint"] = src.embedding_fingerprint()
            conn.execute(
                "UPDATE documents SET manifest=?,status='ready' WHERE id=?", (dump(manifest), doc["id"])
            )
            src.index.sync_document(conn, doc["id"], manifest, strict_vectors=True)
            batch = src.index.pending(conn, doc["id"], src.embedding_fingerprint(), limit=128)
            assert batch
            src.index.store_vectors(
                conn, doc["id"], src.embedding_fingerprint(), batch, [[0.6, 0.8] for _ in batch]
            )
        # Accepted unit tolerance must not entail silently normalizing paid bytes again.
        conn.execute(
            "UPDATE retrieval_vectors SET vector=?",
            (np.array([0.600003, 0.800004], dtype="float32").tobytes(),),
        )
    dst = Service(tmp_path / "imported")
    with dst.db.transaction() as conn:
        for owner in ("target", "unrelated"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 1, now()))
    assert len(requests) == 1
    return src, dst, donor, target, secondary, requests


def transfer(prepared, **kwargs):
    src, dst, *_ = prepared
    return import_collected_resources(src.db.data_dir, dst.db.data_dir, "owner", "target", **kwargs)


def active(prepared, result):
    _src, dst, donor, target, *_ = prepared
    mapped = result["document_versions"]
    donor = stored(dst, {"id": mapped[donor["id"]]["document_id"]})
    target = stored(dst, {"id": mapped[target["id"]]["document_id"]})
    paths = {p.name: p for p in (dst.db.data_dir / "files/blobs").glob("*/*/*/*")}
    return target, donor, target["manifest"]["pages"][0], donor["manifest"]["pages"][0], paths


def test_transfer_preserves_original_proof_pixels_paid_vectors_and_replays(prepared):
    src, dst, donor, target, secondary, requests = prepared
    source_before, destination_before = facts(src), facts(dst)
    before = stored(src, target)["manifest"]["pages"][0]
    donor_before = stored(src, donor)["manifest"]["pages"][0]
    preview = transfer(prepared)
    assert preview["verified_pdf_reuse_pages"] == 2 and not preview["blockers"]
    assert facts(dst) == destination_before
    result = transfer(prepared, apply=True)
    assert result["target_writes"] == 3
    document, donor_doc, page, donor_page, paths = active(prepared, result)
    pdf_ocr_reuse_import.validate_imported(document, donor_doc, page, donor_page, paths, dst.db.data_dir)
    receipt = page["ocr"]["reuse_import"]
    archive = json.loads(paths[receipt["archive_sha256"]].read_bytes())
    assert archive["source_page"] == before and archive["source_donor_page"] == donor_before
    assert archive["source_proof"] == before["ocr"]["reuse"]
    assert archive["target_before_reuse_checkpoint_available"] is False
    assert page["ocr"]["reuse"]["target_document_id"] == document["id"] != target["id"]
    assert page["text"] == before["text"] and receipt["import_model_calls"] == 0
    assert receipt["source_owner_sha256"] != receipt["target_owner_sha256"]
    with closing(dst.db.connect()) as conn:
        vectors = [r[0] for r in conn.execute("SELECT vector FROM retrieval_vectors")]
        assert vectors and all(v == source_before["vectors"][0]["vector"] for v in vectors)
        secondary_id = result["document_versions"][secondary["id"]]["document_id"]
        assert not conn.execute(
            "SELECT 1 FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id=?",
            (secondary_id,),
        ).fetchone()
        assert not dst.archive.can_index(conn, secondary_id)
        assert all(
            json.loads(r[0])["review_status"] == "draft"
            for r in conn.execute("SELECT payload FROM document_metadata")
        )
        assert not conn.execute("SELECT 1 FROM documents WHERE owner_id='unrelated'").fetchone()
    after = facts(dst)
    assert transfer(prepared, apply=True)["unchanged_documents"] == 3
    assert facts(dst) == after and facts(src) == source_before and len(requests) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_document_id", "not-selected"),
        ("source_document_version", 2),
        ("target_document_version", 2),
        ("source_page", 2),
        ("source_page_checkpoint_sha256", "0" * 64),
        ("png_content_hash", "1" * 64),
    ],
)
def test_tampered_source_proof_never_writes_target(prepared, field, value):
    src, dst, _donor, target, *_ = prepared
    with src.db.transaction() as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (target["id"],)).fetchone()[0]
        )
        manifest["pages"][0]["ocr"]["reuse"][field] = value
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), target["id"]))
    before = facts(dst)
    assert transfer(prepared)["blockers"]
    with pytest.raises(CollectionImportError, match="OCR_reuse"):
        transfer(prepared, apply=True)
    assert facts(dst) == before


def test_donor_must_be_explicitly_selected_and_owned(prepared):
    src, dst, donor, target, *_ = prepared
    before = facts(dst)
    with pytest.raises(CollectionImportError, match="selected_donor_required"):
        transfer(prepared, document_ids=[target["id"]], apply=True)
    with src.db.transaction() as conn:
        conn.execute("UPDATE documents SET owner_id='other' WHERE id=?", (donor["id"],))
    with pytest.raises(CollectionImportError, match="selected_donor_required"):
        transfer(prepared, apply=True)
    assert facts(dst) == before


def test_recomputed_active_proof_cannot_replace_archived_checkpoint(prepared):
    result = transfer(prepared, apply=True)
    target, donor, page, donor_page, paths = copy.deepcopy(active(prepared, result))
    page["audit_note"] = donor_page["audit_note"] = "changed since source checkpoint"
    page["ocr"]["reuse"]["source_page_checkpoint_sha256"] = pdf_ocr_reuse._json_hash(donor_page)
    receipt = page["ocr"].pop("reuse_import")
    receipt["derived_page_sha256"] = pdf_ocr_reuse._json_hash(page)
    receipt["derived_proof_sha256"] = pdf_ocr_reuse._json_hash(page["ocr"]["reuse"])
    page["ocr"]["reuse_import"] = receipt
    with pytest.raises(ValueError, match="archived_checkpoint_binding_mismatch"):
        pdf_ocr_reuse_import.validate_imported(
            target, donor, page, donor_page, paths, prepared[1].db.data_dir
        )


def test_backup_restore_retains_complete_source_and_derived_proof_chain(prepared, tmp_path):
    result = transfer(prepared, apply=True)
    _src, dst, *_ = prepared
    backup, restored = tmp_path / "backup", tmp_path / "restored"
    data_backup.create_backup(dst.db.data_dir, backup)
    data_backup.verify_backup(backup)
    data_backup.restore_backup(backup, restored)
    target, donor, page, donor_page, paths = active(prepared, result)
    restored_paths = {digest: restored / path.relative_to(dst.db.data_dir) for digest, path in paths.items()}
    pdf_ocr_reuse_import.validate_imported(target, donor, page, donor_page, restored_paths, restored)
    assert all(path.read_bytes() == restored_paths[digest].read_bytes() for digest, path in paths.items())


@pytest.mark.parametrize("missing", ["donor", "archive"])
def test_backup_rejects_broken_imported_proof_closure(prepared, tmp_path, missing):
    result = transfer(prepared, apply=True)
    _src, dst, *_ = prepared
    _target, donor, page, _donor_page, paths = active(prepared, result)
    if missing == "donor":
        page["ocr"]["reuse"]["source_document_id"] = "missing-donor"
        receipt = page["ocr"].pop("reuse_import")
        receipt["derived_page_sha256"] = pdf_ocr_reuse._json_hash(page)
        receipt["derived_proof_sha256"] = pdf_ocr_reuse._json_hash(page["ocr"]["reuse"])
        page["ocr"]["reuse_import"] = receipt
        with dst.db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET manifest=? WHERE id=?", (dump(_target["manifest"]), _target["id"])
            )
    else:
        paths[page["ocr"]["reuse_import"]["archive_sha256"]].unlink()
    with pytest.raises(data_backup.DataBackupError):
        data_backup.create_backup(dst.db.data_dir, tmp_path / "broken-backup")


def test_png_tampering_is_rejected_before_target_writes(prepared):
    src, dst, donor, *_ = prepared
    image = stored(src, donor)["manifest"]["pages"][0]["ocr"]["image_blob"]
    path = src.db.data_dir / image["storage_path"]
    path.chmod(0o600)
    path.write_bytes(b"\x89PNG\r\n\x1a\nchanged")
    before = facts(dst)
    with pytest.raises(CollectionImportError, match="owner_blob_hash_mismatch"):
        transfer(prepared, apply=True)
    assert facts(dst) == before


def test_duplicate_target_physical_page_is_not_silently_collapsed(prepared):
    src, dst, _donor, target, *_ = prepared
    with src.db.transaction() as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (target["id"],)).fetchone()[0]
        )
        manifest["pages"].append(copy.deepcopy(manifest["pages"][0]))
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), target["id"]))
    before = facts(dst)
    with pytest.raises(CollectionImportError, match="physical_page_missing_or_ambiguous"):
        transfer(prepared, apply=True)
    assert facts(dst) == before
