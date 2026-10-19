"""Native evidence recovery preserves document fences and whole-source chunks."""

from contextlib import closing
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from app import archive_page_recovery as recovery
from app import archive_page_shards as shards
from app.db import dump, now
from app.ingestion_quality import text_complete
from app.page_checkpoint_recovery import manifest_hash, readonly_service
from app.service import Service
from test_archive_parsing import pdf_file


@pytest.fixture
def case(tmp_path):
    path, digest, size = pdf_file(
        tmp_path,
        ["Pasal 1\nTea first article.", "Continued first article.", "PENJELASAN\nPasal 1\nExplanation."],
    )
    service = Service(tmp_path / "library")
    owner = "native-recovery-owner"
    with service.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 1, now()))
        blob = service.archive.blobs.put_bytes(owner, path.read_bytes())
        document = dict(
            service.archive.store_original(
                conn,
                owner,
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
        service.collection.queue_processing(conn, document["id"], owner)
        conn.execute("UPDATE processing_jobs SET status='failed' WHERE document_id=?", (document["id"],))
    source = shards.ArchivePageSource(
        Path(document["storage_path"]), document["id"], 1, owner, digest, size, 3
    )
    output = tmp_path / "shards"
    shards.extract_native_page_shards(source, output, window_pages=1, workers=2)
    root = json.loads((output / "root.json").read_bytes())
    return {
        "service": service,
        "owner": owner,
        "document_id": document["id"],
        "source_hash": digest,
        "version": 1,
        "expected_manifest": manifest_hash(document["manifest"]),
        "shards_dir": output,
        "expected_root": hashlib.sha256((output / "root.json").read_bytes()).hexdigest(),
        "root": root,
        "document": document,
    }


def arguments(case, **changes):
    return {key: value for key, value in case.items() if key not in {"root", "document"}} | changes


def snapshot(case):
    with closing(case["service"].db.connect()) as conn:
        return {
            table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
            for table in (
                "documents",
                "document_metadata",
                "document_metadata_history",
                "legal_classifications",
                "legal_relations",
                "processing_jobs",
                "retrieval_documents",
                "retrieval_chunks",
                "retrieval_vectors",
            )
        }


def test_dry_run_then_apply_rebuilds_cross_page_sections_and_all_cas_proof(case):
    before = snapshot(case)
    blob_count = len(list(case["service"].db.files_dir.rglob("*")))
    plan = recovery.recover_native_page_shards(
        **arguments(case, service=readonly_service(case["service"].db.data_dir))
    )
    assert plan["text_complete"] and plan["restored_native_pages"] == 3
    assert plan["database_writes"] == plan["HTTP_calls"] == plan["model_calls"] == 0
    assert snapshot(case) == before and len(list(case["service"].db.files_dir.rglob("*"))) == blob_count
    report = recovery.recover_native_page_shards(**arguments(case, apply=True))
    assert report["database_writes"] == 1
    with closing(case["service"].db.connect()) as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (case["document_id"],)).fetchone()
        manifest = json.loads(row["manifest"])
        assert text_complete(manifest)
        for key in ("id", "version", "content_hash", "storage_path", "source_url", "owner_id", "family_id"):
            assert row[key] == case["document"][key]
        assert any(
            e["locator"].get("page") == 2 and e["section_path"] == ["正文", "Pasal 1"]
            for e in manifest["elements"]
        )
        assert any(
            e["locator"].get("page") == 3 and e["section_path"] == ["PENJELASAN", "Pasal 1"]
            for e in manifest["elements"]
        )
        assert len({c["id"] for c in manifest["chunks"]}) == len(manifest["chunks"])
        assert conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 0
        assert (
            conn.execute("SELECT status FROM processing_jobs WHERE document_id=?", (row["id"],)).fetchone()[0]
            == "queued"
        )
        proof = manifest["native_page_recovery"]
        assert proof["root_sha256"] == case["expected_root"]
        for blob in proof["evidence_blobs"]:
            raw = (case["service"].db.data_dir / blob["storage_path"]).read_bytes()
            assert len(raw) == blob["size"] and hashlib.sha256(raw).hexdigest() == blob["content_hash"]
        assert case["expected_manifest"] in {blob["content_hash"] for blob in proof["evidence_blobs"]}
    after = snapshot(case)
    for table in (
        "document_metadata",
        "document_metadata_history",
        "legal_classifications",
        "legal_relations",
    ):
        assert after[table] == before[table]
    replay = recovery.recover_native_page_shards(
        **arguments(case, expected_manifest=report["manifest_sha256"], apply=True)
    )
    assert replay["unchanged"] and replay["database_writes"] == 0 and snapshot(case) == after


@pytest.mark.parametrize(
    "change,match",
    [
        ({"owner": "other"}, "not_owned"),
        ({"version": 2}, "version_conflict"),
        ({"source_hash": "a" * 64}, "version_conflict"),
        ({"expected_manifest": "a" * 64}, "manifest_conflict"),
        ({"expected_root": "a" * 64}, "root_conflict"),
    ],
)
def test_explicit_identity_fences_refuse_before_database_changes(case, change, match):
    before = snapshot(case)
    with pytest.raises(ValueError, match=match):
        recovery.recover_native_page_shards(**arguments(case, **change, apply=True))
    assert snapshot(case) == before


def test_active_claim_is_never_stolen(case):
    with case["service"].db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET status='processing',lease_token='active',lease_until=1")
    before = snapshot(case)
    with pytest.raises(ValueError, match="lease_active"):
        recovery.recover_native_page_shards(**arguments(case, apply=True))
    assert snapshot(case) == before


def test_missing_or_corrupt_native_segment_is_not_applied(case):
    segment = case["shards_dir"] / case["root"]["segments"][0]["cas_path"]
    segment.chmod(0o600)
    segment.write_bytes(b"corrupt")
    before = snapshot(case)
    with pytest.raises(ValueError, match="hash/size"):
        recovery.recover_native_page_shards(**arguments(case, apply=True))
    assert snapshot(case) == before


def test_partial_source_map_is_not_promoted(case):
    root = deepcopy(case["root"])
    root["segments"].pop()
    shards._summary(root)
    raw = shards._encoded(root)
    (case["shards_dir"] / "root.json").write_bytes(raw)
    before = snapshot(case)
    with pytest.raises(ValueError, match="complete_mapping"):
        recovery.recover_native_page_shards(
            **arguments(case, expected_root=hashlib.sha256(raw).hexdigest(), apply=True)
        )
    assert snapshot(case) == before


def test_whole_document_text_limit_remains_closed(case, monkeypatch):
    monkeypatch.setattr(recovery, "_MAX_TEXT", 5)
    before = snapshot(case)
    with pytest.raises(ValueError, match="4000000_characters"):
        recovery.recover_native_page_shards(**arguments(case, apply=True))
    assert snapshot(case) == before


def test_saved_page_is_never_replaced_by_independently_extracted_native_text(case):
    pages = []
    for segment in case["root"]["segments"]:
        pages.extend(json.loads((case["shards_dir"] / segment["cas_path"]).read_bytes())["pages"])
    pages[0]["text"] += " old accepted text must stay"
    current = {"source_hash": case["source_hash"], "pages": pages}
    with case["service"].db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(current), case["document_id"]))
    before = snapshot(case)
    with pytest.raises(ValueError, match="saved_page_changed"):
        recovery.recover_native_page_shards(
            **arguments(case, expected_manifest=manifest_hash(dump(current)), apply=True)
        )
    assert snapshot(case) == before


def test_new_ocr_claim_cannot_be_smuggled_in_as_native_window(case):
    root = deepcopy(case["root"])
    segment = root["segments"][0]
    payload = json.loads((case["shards_dir"] / segment["cas_path"]).read_bytes())
    payload["pages"][0]["ocr"] = {"attempts": 1, "text": "invented paid response"}
    store = shards.BlobStore(case["shards_dir"] / "files")
    blob = store.put_bytes(case["owner"], shards._encoded(payload))
    segment.update(shards._reference(blob, case["shards_dir"]))
    segment["page_sha256"] = [shards._sha(shards._encoded(page)) for page in payload["pages"]]
    raw = shards._encoded(root)
    (case["shards_dir"] / "root.json").write_bytes(raw)
    with pytest.raises(ValueError, match="must_be_native_evidence"):
        recovery.recover_native_page_shards(
            **arguments(case, expected_root=hashlib.sha256(raw).hexdigest(), apply=True)
        )


def test_concurrent_document_change_rolls_back_before_any_index_replacement(case, monkeypatch):
    original = recovery.archive_rebuild_bounded

    def concurrent(manifest):
        rebuilt = original(manifest)
        with case["service"].db.transaction() as conn:
            conn.execute("UPDATE documents SET manifest='{}' WHERE id=?", (case["document_id"],))
        return rebuilt

    monkeypatch.setattr(recovery, "archive_rebuild_bounded", concurrent)
    with pytest.raises(ValueError, match="manifest_conflict"):
        recovery.recover_native_page_shards(**arguments(case, apply=True))
    with closing(case["service"].db.connect()) as conn:
        assert (
            conn.execute("SELECT manifest FROM documents WHERE id=?", (case["document_id"],)).fetchone()[0]
            == "{}"
        )
        assert conn.execute("SELECT count(*) FROM retrieval_chunks").fetchone()[0] == 0


def test_cli_is_readonly_by_default_and_apply_is_explicit(case):
    script = Path(__file__).resolve().parents[2] / "scripts/recover_archive_native_pages.py"
    command = [sys.executable, str(script), "--data-dir", str(case["service"].db.data_dir)]
    for key, flag in (
        ("owner", "owner"),
        ("document_id", "document-id"),
        ("source_hash", "source-hash"),
        ("version", "version"),
        ("expected_manifest", "expected-manifest-sha256"),
        ("shards_dir", "shards-dir"),
        ("expected_root", "expected-root-sha256"),
    ):
        command.extend(["--" + flag, str(case[key])])
    before = snapshot(case)
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["database_writes"] == 0 and snapshot(case) == before
    result = subprocess.run([*command, "--apply"], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    assert json.loads(result.stdout)["database_writes"] == 1
    conn = sqlite3.connect(case["service"].db.data_dir / "tea.sqlite3")
    try:
        assert len(json.loads(conn.execute("SELECT manifest FROM documents").fetchone()[0])["pages"]) == 3
    finally:
        conn.close()


def test_restored_pages_and_entire_evidence_graph_survive_formal_backup(case, tmp_path):
    from app.data_backup import create_backup, restore_backup, verify_backup

    recovery.recover_native_page_shards(**arguments(case, apply=True))
    backup, target = tmp_path / "backup", tmp_path / "restored"
    create_backup(case["service"].db.data_dir, backup)
    verify_backup(backup)
    restore_backup(backup, target)
    with closing(case["service"].db.connect()) as conn:
        source = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (case["document_id"],)).fetchone()[0]
        )
    with closing(sqlite3.connect(target / "tea.sqlite3")) as conn:
        restored = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (case["document_id"],)).fetchone()[0]
        )
    assert restored == source
    for blob in restored["native_page_recovery"]["evidence_blobs"]:
        assert (
            hashlib.sha256((target / blob["storage_path"]).read_bytes()).hexdigest() == blob["content_hash"]
        )


def test_scanned_pages_remain_incomplete_and_out_of_search(case, monkeypatch):
    root = deepcopy(case["root"])
    store = shards.BlobStore(case["shards_dir"] / "files")
    for segment in root["segments"]:
        payload = json.loads((case["shards_dir"] / segment["cas_path"]).read_bytes())
        for page in payload["pages"]:
            page.update(status="pending_ocr", ocr_required=True, reasons=["large_raster"])
        blob = store.put_bytes(case["owner"], shards._encoded(payload))
        segment.update(shards._reference(blob, case["shards_dir"]))
        segment["page_sha256"] = [shards._sha(shards._encoded(page)) for page in payload["pages"]]
        segment["page_statuses"] = {"pending_ocr": len(payload["pages"])}
    shards._summary(root)
    raw = shards._encoded(root)
    (case["shards_dir"] / "root.json").write_bytes(raw)
    report = recovery.recover_native_page_shards(
        **arguments(case, expected_root=hashlib.sha256(raw).hexdigest(), apply=True)
    )
    assert not report["text_complete"] and report["pending_ocr_pages"] == 3
    with closing(case["service"].db.connect()) as conn:
        assert (
            conn.execute(
                "SELECT complete FROM retrieval_documents WHERE document_id=?", (case["document_id"],)
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 0
