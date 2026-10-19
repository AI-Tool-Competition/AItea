"""Account isolation, genuine input checkpoint reuse and atomic import boundaries."""

import json
import hashlib
import io
import base64
from contextlib import closing
from pathlib import Path

import pytest

from app.collection_import import CollectionImportError, import_collected_resources
from app.db import dump, now
from app.parsing import parse_bounded
from app.service import Service
from app.system_library import SYSTEM_OWNER_ID, ensure_principal


@pytest.fixture
def pair(tmp_path, monkeypatch):
    for key in ("EMBEDDING_API_KEY", "EMBEDDING_MODEL", "EMBEDDING_BASE_URL"):
        monkeypatch.setenv(key, "test-value")
    src, dst = Service(tmp_path / "source"), Service(tmp_path / "target")
    for service, owner in [(src, "source"), (dst, "target"), (dst, "unrelated")]:
        with service.db.transaction() as conn:
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 1, now()))
    return src, dst


def original(src, key="base", *, level="primary", parent=None, content=None, complete=True):
    content = content or ("Source original " + key).encode()
    blob = src.archive.blobs.put_bytes("source", content)
    with src.db.transaction() as conn:
        doc = dict(
            src.archive.store_original(
                conn,
                "source",
                blob,
                key + ".txt",
                "text/plain",
                "https://official.example/" + key,
                collection="country:ID",
                level=level,
                tags=["kind:law"],
                identity_key=key,
                parent_id=parent,
                kind_hint="law" if level != "attachment" else None,
            )
        )
        if level != "attachment":
            manifest = parse_bounded(key + ".txt", content, "text/plain")
            if not complete:
                manifest["quality"] = {"complete": False, "pending_pages": [1]}
            manifest["embedding_fingerprint"] = src.embedding_fingerprint()
            conn.execute(
                "UPDATE documents SET manifest=?,status='ready' WHERE id=?", (dump(manifest), doc["id"])
            )
            src.index.sync_document(conn, doc["id"], manifest)
            batch = src.index.pending(conn, doc["id"], src.embedding_fingerprint(), limit=128)
            if complete and level == "primary":
                src.index.store_vectors(
                    conn, doc["id"], src.embedding_fingerprint(), batch, [[0.6, 0.8] for _ in batch]
                )
    return doc


def transfer(src, dst, **kwargs):
    return import_collected_resources(src.db.path.parent, dst.db.path.parent, "source", "target", **kwargs)


def facts(service):
    with closing(service.db.connect()) as conn:
        return {
            "documents": [dict(x) for x in conn.execute("SELECT * FROM documents ORDER BY id")],
            "metadata": [
                dict(x) for x in conn.execute("SELECT * FROM document_metadata ORDER BY document_id")
            ],
            "classifications": [
                dict(x) for x in conn.execute("SELECT * FROM legal_classifications ORDER BY document_id")
            ],
            "vectors": [
                dict(x) for x in conn.execute("SELECT * FROM retrieval_vectors ORDER BY chunk_rowid")
            ],
            "jobs": [dict(x) for x in conn.execute("SELECT * FROM processing_jobs ORDER BY document_id")],
            "users": conn.execute("SELECT count(*) FROM users").fetchone()[0],
        }


def html_image_document(src, monkeypatch):
    from PIL import Image
    from app import native_asset_ocr as native, vision
    from app.html_linked_asset_ocr import bind_linked_image_bounded

    body = b'<main><p>Official import requirements.</p><img src="/images/chart.jpg"></main>'
    doc = original(src, "html-image", content=body)
    manifest = parse_bounded("law.html", body, "text/html")
    output = io.BytesIO()
    Image.new("RGB", (24, 16), "blue").save(output, format="JPEG")
    image = output.getvalue()
    blob = src.archive.blobs.put_bytes("source", image)
    reference = {
        "storage_path": str(blob.path.relative_to(src.db.data_dir)),
        "content_hash": blob.digest,
        "size": blob.size,
    }
    manifest = bind_linked_image_bounded(
        body,
        manifest,
        object_key=manifest["html_source"]["objects"][0]["object_key"],
        source_url="https://www.customs.gov.sg/import-law",
        image=image,
        image_blob=reference,
    )
    key = native.assets(manifest)[0]["asset_key"]
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    monkeypatch.setattr(
        vision,
        "transcribe_image",
        lambda _: {
            "text": "Imported goods require the complete declaration.",
            "complete": True,
            "is_blank": False,
            "uncertain_spans": [],
            "model": "unit-image-model",
            "requested_model": "unit-image-model",
            "prompt_version": vision.IMAGE_PROMPT_VERSION,
            "review_status": "unreviewed",
            "physical_pages_known": False,
            "finish_reason": "stop",
        },
    )

    def save_image(rendered):
        png = src.archive.blobs.put_bytes("source", base64.b64decode(rendered["image_base64"]))
        return {
            "storage_path": str(png.path.relative_to(src.db.data_dir)),
            "content_hash": png.digest,
            "size": png.size,
        }

    manifest = native.ocr_native_assets_bounded(
        body, manifest, image_contents={key: image}, on_image=save_image
    )
    manifest["embedding_fingerprint"] = src.embedding_fingerprint()
    with src.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET manifest=?,filename='law.html',mime_type='text/html',status='ready' WHERE id=?",
            (dump(manifest), doc["id"]),
        )
        src.index.sync_document(conn, doc["id"], manifest)
        batch = src.index.pending(conn, doc["id"], src.embedding_fingerprint(), limit=128)
        src.index.store_vectors(
            conn, doc["id"], src.embedding_fingerprint(), batch, [[0.6, 0.8] for _ in batch]
        )
    return doc, body, manifest, image


def test_html_image_import_rebinds_original_png_checkpoint_and_vectors_without_model_calls(pair, monkeypatch):
    from app import native_asset_ocr as native, vision
    from app.html_linked_asset_ocr import read_linked_images
    from app.ingestion_quality import text_complete

    src, dst = pair
    doc, body, before, image = html_image_document(src, monkeypatch)
    old = native.assets(before)[0]
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: pytest.fail("complete OCR must be reused"))
    preview = transfer(src, dst)
    assert preview["reusable_vectors"] == len(before["chunks"])
    assert preview["target_writes"] == 0
    result = transfer(src, dst, apply=True)
    ident = result["document_versions"][doc["id"]]["document_id"]
    with closing(dst.db.connect()) as conn:
        saved = json.loads(conn.execute("SELECT manifest FROM documents WHERE id=?", (ident,)).fetchone()[0])
        rows = list(
            conn.execute(
                "SELECT c.chunk_id,c.input_hash,v.input_hash FROM retrieval_chunks c JOIN retrieval_vectors v ON c.rowid=v.chunk_rowid WHERE c.document_id=?",
                (ident,),
            )
        )
    rebound = native.assets(saved)[0]
    assert rebound["asset_key"] != old["asset_key"] and rebound["input_identity"] != old["input_identity"]
    assert rebound["collection_asset_import"]["source_input_identity"] == old["input_identity"]
    assert rebound["collection_asset_import"]["source_asset_key"] == old["asset_key"]
    assert rebound["ocr"]["input_identity"] == rebound["input_identity"]
    assert (
        rebound["ocr"]["text"] == old["ocr"]["text"] and rebound["ocr"]["attempts"] == old["ocr"]["attempts"]
    )
    assert read_linked_images(dst.db.data_dir, "target", saved) == {rebound["asset_key"]: image}
    png = dst.db.data_dir / rebound["ocr"]["image_blob"]["storage_path"]
    assert (
        png.read_bytes().startswith(b"\x89PNG")
        and hashlib.sha256(png.read_bytes()).hexdigest() == rebound["ocr"]["image_sha256"]
    )
    assert text_complete(saved) and len(rows) == len(before["chunks"])
    assert all(row[1] == row[2] for row in rows)
    assert rebound["asset_key"] in {row[0] for row in rows} and old["asset_key"] not in {
        row[0] for row in rows
    }
    assert native.ocr_native_assets_bounded(body, saved) == saved
    assert transfer(src, dst, apply=True)["unchanged_documents"] == 1
    transfer(src, dst, apply=True, refresh=True)
    with closing(dst.db.connect()) as conn:
        refreshed = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (ident,)).fetchone()[0]
        )
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == len(rows)
    assert native.assets(refreshed)[0] == rebound


def test_import_preserves_failed_response_evidence_without_indexing_its_body(pair):
    src, dst = pair
    doc = original(src, complete=False)
    raw = dump({"format": "ocr-error-response-v1", "response_base64": "aW52YWxpZCBKU09O"}).encode()
    blob = src.archive.blobs.put_bytes("source", raw)
    with src.db.transaction() as conn:
        manifest = json.loads(src.own_document(conn, "source", doc["id"])["manifest"])
        manifest["pages"] = [
            {
                "page": 1,
                "source_hash": doc["content_hash"],
                "status": "ocr_failed",
                "ocr": {
                    "error_response": {
                        "format": "ocr-error-response-v1",
                        "request_id": "fixed-request",
                        "blob": {
                            "storage_path": str(blob.path.relative_to(src.db.data_dir)),
                            "content_hash": blob.digest,
                            "size": blob.size,
                        },
                    }
                },
            }
        ]
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
    result = transfer(src, dst, apply=True)
    ident = result["document_versions"][doc["id"]]["document_id"]
    with closing(dst.db.connect()) as conn:
        saved = json.loads(dst.own_document(conn, "target", ident)["manifest"])
        assert not conn.execute("SELECT 1 FROM retrieval_vectors").fetchone()
    proof = saved["pages"][0]["ocr"]["error_response"]
    target_path = dst.db.data_dir / proof["blob"]["storage_path"]
    assert proof["request_id"] == "fixed-request" and target_path.read_bytes() == raw
    assert target_path.parent.parent.parent.name == hashlib.sha256(b"target").hexdigest()
    assert "response_base64" not in json.dumps(saved)


@pytest.mark.parametrize(
    "key,path",
    [
        ("mapping_path", "/etc/passwd"),
        ("mapping_path", "/html_source/linked_images/../../etc/passwd"),
        ("mapping_path", "/html_source/linked_images/2000"),
        ("asset_mapping_path", "/html_source/linked_images/0/extra"),
        ("image_path", "/html_source/linked_images/0"),
    ],
)
def test_manifest_private_paths_are_rejected_in_preflight_before_any_target_write(pair, key, path):
    src, dst = pair
    doc = original(src)
    with src.db.transaction() as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
        manifest["untrusted_proof"] = {key: path}
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
    before, files = facts(dst), set(dst.db.files_dir.rglob("*"))
    for apply in (False, True):
        with pytest.raises(CollectionImportError, match="unmapped_private_path"):
            transfer(src, dst, apply=apply)
        assert facts(dst) == before and set(dst.db.files_dir.rglob("*")) == files


def test_inconsistent_source_html_ocr_identity_is_rejected_before_transfer(pair, monkeypatch):
    src, dst = pair
    doc, _, manifest, _ = html_image_document(src, monkeypatch)
    manifest["html_source"]["linked_images"][0]["ocr"]["input_identity"] = "0" * 64
    with src.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
    with pytest.raises(CollectionImportError, match="invalid_source_native_asset_checkpoint"):
        transfer(src, dst)


def test_campaign_catalogue_and_detail_evidence_remain_reachable_after_import(pair):
    src, dst = pair
    doc = original(src)
    catalogue = src.archive.blobs.put_bytes("source", b"Official catalogue listing this law")
    detail = src.archive.blobs.put_bytes("source", b"Official detail page linking its original")
    receipt = src.archive.blobs.put_bytes("source", b"Receipt bound to the catalogue and original")
    record = {
        "document_id": doc["id"],
        "source_hash": doc["content_hash"],
        "receipt_sha256": receipt.digest,
        "receipt_path": "receipts/original.json",
        "receipt_cas_path": str(receipt.path.relative_to(src.db.data_dir)),
        "assets": [
            {"kind": "catalogue", "sha256": catalogue.digest, "cas_path": str(catalogue.path)},
            {"kind": "detail", "sha256": detail.digest, "cas_path": str(detail.path)},
        ],
    }
    state = {"owner_id": "source", "documents": {"law": record}}
    (src.db.data_dir / "campaign.json").write_text(dump(state))
    preview = transfer(src, dst)
    assert preview["source_evidence_documents"] == 1
    result = transfer(src, dst, apply=True)
    ident = result["document_versions"][doc["id"]]["document_id"]
    with closing(dst.db.connect()) as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (ident,)).fetchone()[0]
        )
    evidence = manifest["collection_evidence"]
    assert evidence["source_original_sha256"] == doc["content_hash"]
    assert evidence["source_record_sha256"] == hashlib.sha256(dump(record).encode()).hexdigest()
    assert evidence["record"]["document_id"] == ident
    receipt_path = evidence["record"]["receipt_path"]
    assert receipt_path == evidence["record"]["receipt_cas_path"]
    assert (dst.db.data_dir / receipt_path).read_bytes() == b"Receipt bound to the catalogue and original"
    for asset in evidence["record"]["assets"]:
        assert not Path(asset["cas_path"]).is_absolute()
        assert (
            hashlib.sha256((dst.db.data_dir / asset["cas_path"]).read_bytes()).hexdigest() == asset["sha256"]
        )
    assert str(src.db.data_dir) not in dump(evidence)
    assert transfer(src, dst, apply=True)["unchanged_documents"] == 1
    record["catalogue_page"] = 2
    (src.db.data_dir / "campaign.json").write_text(dump(state))
    with pytest.raises(CollectionImportError, match="source_checkpoint_changed_requires_refresh"):
        transfer(src, dst, apply=True)
    transfer(src, dst, apply=True, refresh=True)
    with closing(dst.db.connect()) as conn:
        updated = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (ident,)).fetchone()[0]
        )
    assert updated["collection_evidence"]["record"]["catalogue_page"] == 2


@pytest.mark.parametrize("tampered", [False, True])
def test_ocr_retry_history_hash_is_verified_then_rebound_to_target_paths(pair, tampered):
    from app.ocr_page_retry import _encoded

    src, dst = pair
    doc = original(src)
    image = src.archive.blobs.put_bytes("source", b"Original physical page image")
    previous = {
        "page": 1,
        "source_hash": doc["content_hash"],
        "status": "ocr_uncertain",
        "ocr": {"image_blob": {"storage_path": str(image.path), "content_hash": image.digest}},
        "text": "An incomplete previous reading",
    }
    digest = hashlib.sha256(_encoded(previous)).hexdigest()
    history = {
        "protocol": "physical-page-completeness-retry-v1",
        "page": 1,
        "source_hash": doc["content_hash"],
        "previous_page_sha256": "0" * 64 if tampered else digest,
        "previous_page": previous,
    }
    with src.db.transaction() as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
        manifest["ocr_recovery_audit"] = history
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
        src.index.sync_document(conn, doc["id"], manifest)
    if tampered:
        before = facts(dst)
        with pytest.raises(CollectionImportError, match="invalid_previous_OCR_checkpoint"):
            transfer(src, dst)
        assert facts(dst) == before
        return
    result = transfer(src, dst, apply=True)
    ident = result["document_versions"][doc["id"]]["document_id"]
    with closing(dst.db.connect()) as conn:
        saved = json.loads(conn.execute("SELECT manifest FROM documents WHERE id=?", (ident,)).fetchone()[0])
    moved = saved["ocr_recovery_audit"]
    assert moved["source_previous_page_sha256"] == digest
    assert moved["previous_page_sha256"] == hashlib.sha256(_encoded(moved["previous_page"])).hexdigest()
    assert moved["previous_page_sha256"] != digest
    path = moved["previous_page"]["ocr"]["image_blob"]["storage_path"]
    assert (dst.db.data_dir / path).read_bytes() == b"Original physical page image"


@pytest.mark.parametrize("attachment", [False, True])
def test_system_admission_rejected_before_copying_any_original(pair, attachment):
    src, dst = pair
    parent = original(src)
    if attachment:
        rogue = original(src, "annex", level="attachment", parent=parent["id"])
        with src.db.transaction() as conn:
            conn.execute(
                "UPDATE documents SET source_url='https://jdih.example.go.id/base' WHERE id=?",
                (parent["id"],),
            )
    else:
        rogue = parent
    with dst.db.transaction() as conn:
        ensure_principal(conn)
    before = facts(dst)
    blobs_before = set(dst.db.files_dir.rglob("*"))
    args = (src.db.path.parent, dst.db.path.parent, "source", SYSTEM_OWNER_ID)
    assert "system_source_required:" + rogue["id"] in import_collected_resources(*args)["blockers"]
    with pytest.raises(CollectionImportError, match="system_source_required"):
        import_collected_resources(*args, apply=True)
    assert facts(dst) == before
    assert set(dst.db.files_dir.rglob("*")) == blobs_before


def test_official_system_import_reuses_vectors_without_publishing_draft(pair):
    src, dst = pair
    doc = original(src)
    with src.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET source_url='https://jdih.example.go.id/base' WHERE id=?", (doc["id"],)
        )
    with dst.db.transaction() as conn:
        ensure_principal(conn)
    result = import_collected_resources(
        src.db.path.parent, dst.db.path.parent, "source", SYSTEM_OWNER_ID, apply=True
    )
    target = result["document_versions"][doc["id"]]["document_id"]
    with closing(dst.db.connect()) as conn:
        assert dst.read_document(conn, "unrelated", target)["owner_id"] == SYSTEM_OWNER_ID
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] > 0
    assert dst.search("unrelated", "Source original", country="ID")["items"] == []


def test_image_checkpoint_keeps_PNG_identity_after_both_passes_and_explicit_refresh(pair):
    from PIL import Image

    src, dst = pair
    doc = original(src)
    buffer = io.BytesIO()
    Image.new("RGB", (2, 3), "white").save(buffer, format="PNG")
    png = src.archive.blobs.put_bytes("source", buffer.getvalue())
    with src.db.transaction() as conn:
        manifest = json.loads(src.own_document(conn, "source", doc["id"])["manifest"])
        manifest["quality"] = {"complete": False, "pending_pages": [1]}
        manifest["pages"] = [
            {
                "page": 1,
                "source_hash": doc["content_hash"],
                "status": "pending_ocr",
                "ocr": {
                    "image_sha256": png.digest,
                    "image_blob": {
                        "content_hash": png.digest,
                        "size": png.size,
                        "mime_type": "image/png",
                        "storage_path": str(png.path.relative_to(src.db.path.parent)),
                    },
                },
            }
        ]
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
    result = transfer(src, dst, apply=True)
    target_id = result["document_versions"][doc["id"]]["document_id"]

    def check_image():
        with closing(dst.db.connect()) as conn:
            target = dst.own_document(conn, "target", target_id)
            blob = json.loads(target["manifest"])["pages"][0]["ocr"]["image_blob"]
            image_path = dst.db.path.parent / blob["storage_path"]
            assert image_path != Path(target["storage_path"])
            assert hashlib.sha256(image_path.read_bytes()).hexdigest() == blob["content_hash"] == png.digest
            assert image_path.read_bytes() == buffer.getvalue()
            assert target["version"] == 1

    check_image()
    assert transfer(src, dst, apply=True)["unchanged_documents"] == 1
    check_image()
    refreshed = transfer(src, dst, apply=True, refresh=True)
    assert refreshed["unchanged_documents"] == 0 and refreshed["target_writes"] == 0
    assert refreshed["document_versions"] == result["document_versions"]
    check_image()


def test_dry_run_no_writes_and_complete_vectors_reuse(pair):
    src, dst = pair
    doc = original(src)
    before = facts(dst)
    dry = transfer(src, dst)
    assert dry["mode"] == "dry_run" and dry["reusable_vectors"] > 0 and dry["target_writes"] == 0
    assert facts(dst) == before
    result = transfer(src, dst, apply=True)
    assert result["target_writes"] == 1
    target = result["document_versions"][doc["id"]]["document_id"]
    with closing(dst.db.connect()) as conn:
        row = dst.own_document(conn, "target", target)
        assert row["content_hash"] == doc["content_hash"] and row["version"] == 1
        assert (
            json.loads(
                conn.execute(
                    "SELECT payload FROM document_metadata WHERE document_id=?", (target,)
                ).fetchone()[0]
            )["review_status"]
            == "draft"
        )
        assert conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == dry["reusable_vectors"]
        assert not conn.execute("SELECT 1 FROM documents WHERE owner_id='unrelated'").fetchone()
    assert dst.search("target", "Source original", country="ID")["items"] == []
    before = facts(dst)
    retry = transfer(src, dst, apply=True)
    assert retry["document_versions"] == result["document_versions"] and retry["unchanged_documents"] == 1
    assert facts(dst) == before


def test_dry_run_missing_target_never_initializes_directory(pair, tmp_path):
    src, _ = pair
    original(src)
    path = tmp_path / "never-created"
    result = import_collected_resources(src.db.path.parent, path, "source", "unknown")
    assert result["blockers"] == ["target_existing_database_required"] and not path.exists()


def test_target_owner_required_without_account_creation(pair):
    src, dst = pair
    original(src)
    before = facts(dst)
    with pytest.raises(CollectionImportError, match="target_owner_not_found"):
        import_collected_resources(src.db.path.parent, dst.db.path.parent, "source", "missing", apply=True)
    assert facts(dst) == before


def test_secondary_text_and_ordinary_attachment_never_vectorized(pair):
    src, dst = pair
    primary = original(src)
    secondary = original(src, "supplement", level="secondary")
    attachment = original(src, "attachment", level="attachment", parent=primary["id"])
    result = transfer(src, dst, apply=True)
    with closing(dst.db.connect()) as conn:
        for source in (secondary, attachment):
            ident = result["document_versions"][source["id"]]["document_id"]
            assert not conn.execute(
                "SELECT 1 FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id=?",
                (ident,),
            ).fetchone()
        ident = result["document_versions"][attachment["id"]]["document_id"]
        assert not conn.execute("SELECT 1 FROM processing_jobs WHERE document_id=?", (ident,)).fetchone()
        assert not conn.execute("SELECT 1 FROM retrieval_chunks WHERE document_id=?", (ident,)).fetchone()
        row = dst.own_document(conn, "target", ident)
        assert row["parent_id"] == result["document_versions"][primary["id"]]["document_id"]


@pytest.mark.parametrize("complete,mismatch", [(False, False), (True, True)])
def test_incomplete_or_unconfigured_fingerprint_no_vector_import(pair, monkeypatch, complete, mismatch):
    src, dst = pair
    original(src, complete=complete)
    if mismatch:
        monkeypatch.delenv("EMBEDDING_API_KEY")
    result = transfer(src, dst, apply=True)
    assert result["reusable_vectors"] == 0 and not facts(dst)["vectors"]


def test_corrupt_source_vector_rejected_before_target_writes(pair):
    src, dst = pair
    original(src)
    with src.db.transaction() as conn:
        conn.execute("UPDATE retrieval_vectors SET input_hash='wrong'")
    before = facts(dst)
    with pytest.raises(CollectionImportError, match="vector_input"):
        transfer(src, dst, apply=True)
    assert facts(dst) == before


def test_multiversion_family_refused_before_apply(pair):
    src, dst = pair
    original(src)
    original(src, content=b"Amended source original")
    before = facts(dst)
    assert any(x.startswith("unsupported_multiversion_family") for x in transfer(src, dst)["blockers"])
    with pytest.raises(CollectionImportError, match="unsupported_multiversion"):
        transfer(src, dst, apply=True)
    assert facts(dst) == before


def test_selected_import_ignores_unselected_missing_blobs_and_preserves_vectors(pair):
    src, dst = pair
    selected = original(src, "selected")
    omitted = original(src, "omitted")
    Path(omitted["storage_path"]).unlink()
    before = facts(dst)
    dry = transfer(src, dst, document_ids=[selected["id"]])
    assert dry["documents"] == 1 and dry["reusable_vectors"] == 1
    assert dry["selected_document_ids"] == [selected["id"]]
    assert facts(dst) == before
    result = transfer(src, dst, document_ids=[selected["id"]], apply=True)
    assert set(result["document_versions"]) == {selected["id"]}
    assert len(facts(dst)["documents"]) == 1 and len(facts(dst)["vectors"]) == 1
    assert not dst.archive.blobs.blobs_dir.joinpath(
        hashlib.sha256(b"target").hexdigest(),
        omitted["content_hash"][:2],
        omitted["content_hash"][2:4],
        omitted["content_hash"],
    ).exists()


@pytest.mark.parametrize("selection", [[], ["missing"], ["missing", "missing"], [None], ["x"] * 201])
def test_selected_import_rejects_invalid_or_missing_ids_without_target_writes(pair, selection):
    src, dst = pair
    original(src)
    before = facts(dst)
    with pytest.raises(CollectionImportError, match="document_selection|selected_documents_not_owned"):
        transfer(src, dst, document_ids=selection, apply=True)
    assert facts(dst) == before


def test_selected_import_checks_owner_before_blob_access(pair):
    src, dst = pair
    foreign = original(src, "private")
    with src.db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("foreign", "foreign", None, None, 1, now()))
        conn.execute("UPDATE documents SET owner_id='foreign' WHERE id=?", (foreign["id"],))
    Path(foreign["storage_path"]).unlink()
    before = facts(dst)
    with pytest.raises(CollectionImportError, match="selected_documents_not_owned_or_missing"):
        transfer(src, dst, document_ids=[foreign["id"]], apply=True)
    assert facts(dst) == before


def test_selected_import_cannot_hide_other_versions_of_the_same_family(pair):
    src, dst = pair
    old = original(src)
    original(src, content=b"Amended source original")
    dry = transfer(src, dst, document_ids=[old["id"]])
    assert "unsupported_multiversion_family:" + old["id"] in dry["blockers"]
    with pytest.raises(CollectionImportError, match="unsupported_multiversion"):
        transfer(src, dst, document_ids=[old["id"]], apply=True)
    assert facts(dst)["documents"] == []


def test_selected_import_requires_explicit_relation_endpoints(pair):
    src, dst = pair
    base = original(src)
    supplement = original(src, "supplement", level="secondary")
    unrelated = original(src, "unrelated")
    src.archive.add_relation(
        "source",
        supplement["id"],
        {
            "target_document_id": base["id"],
            "collection": "country:ID",
            "relation": "cites",
            "locator": "p. 1",
        },
    )
    before = facts(dst)
    with pytest.raises(CollectionImportError, match="selected_relation_dependencies_required:" + base["id"]):
        transfer(src, dst, document_ids=[supplement["id"]], apply=True)
    assert facts(dst) == before
    result = transfer(src, dst, document_ids=[supplement["id"], base["id"]], apply=True)
    assert set(result["document_versions"]) == {supplement["id"], base["id"]}
    assert unrelated["id"] not in result["document_versions"]
    with closing(dst.db.connect()) as conn:
        relation = conn.execute("SELECT * FROM legal_relations").fetchone()
        assert relation["source_document_id"] == result["document_versions"][supplement["id"]]["document_id"]
        assert relation["target_document_id"] == result["document_versions"][base["id"]]["document_id"]
        assert json.loads(relation["locator"]) == {
            "source_locator": "p. 1",
            "source_version": 1,
            "target_version": 1,
        }


def test_selected_attachment_requires_parent_and_retains_mapping(pair):
    src, dst = pair
    parent = original(src)
    attachment = original(src, "attachment", level="attachment", parent=parent["id"])
    dry = transfer(src, dst, document_ids=[attachment["id"]])
    assert "owned_parent_required:" + attachment["id"] in dry["blockers"]
    with pytest.raises(CollectionImportError, match="owned_parent_required"):
        transfer(src, dst, document_ids=[attachment["id"]], apply=True)
    result = transfer(src, dst, document_ids=[attachment["id"], parent["id"]], apply=True)
    with closing(dst.db.connect()) as conn:
        mapped = dst.own_document(
            conn, "target", result["document_versions"][attachment["id"]]["document_id"]
        )
        assert mapped["parent_id"] == result["document_versions"][parent["id"]]["document_id"]


def test_selected_import_limits_manifest_bytes_to_selected_documents(pair, monkeypatch):
    from app import collection_import

    src, dst = pair
    selected = original(src, "selected")
    omitted = original(src, "large")
    with src.db.transaction() as conn:
        selected_size = conn.execute(
            "SELECT length(CAST(manifest AS BLOB)) FROM documents WHERE id=?", (selected["id"],)
        ).fetchone()[0]
        conn.execute(
            "UPDATE documents SET manifest=? WHERE id=?",
            (dump({"unused": "x" * (selected_size * 2)}), omitted["id"]),
        )
    monkeypatch.setattr(collection_import, "MAX_MANIFEST_BYTES", selected_size)
    monkeypatch.setattr(collection_import, "MAX_TOTAL_MANIFEST_BYTES", selected_size + 100)
    with pytest.raises(CollectionImportError, match="total_manifest_scope_exceeded"):
        transfer(src, dst)
    dry = transfer(src, dst, document_ids=[selected["id"]])
    assert dry["documents"] == 1 and dry["reusable_vectors"] == 1


def test_source_active_lease_allowed_but_target_active_lease_refused(pair):
    src, dst = pair
    doc = original(src)
    with src.db.transaction() as conn:
        conn.execute(
            "UPDATE processing_jobs SET status='processing',lease_token='live',lease_until=9999999999 WHERE document_id=?",
            (doc["id"],),
        )
    result = transfer(src, dst, apply=True)
    target = result["document_versions"][doc["id"]]["document_id"]
    with dst.db.transaction() as conn:
        conn.execute(
            "UPDATE processing_jobs SET status='processing',lease_token='live',lease_until=9999999999 WHERE document_id=?",
            (target,),
        )
    with pytest.raises(CollectionImportError, match="target_has_active"):
        transfer(src, dst, apply=True)


def test_user_metadata_edit_refuses_refresh(pair):
    src, dst = pair
    doc = original(src)
    result = transfer(src, dst, apply=True)
    target = result["document_versions"][doc["id"]]["document_id"]
    with dst.db.transaction() as conn:
        conn.execute("UPDATE document_metadata SET revision=revision+1 WHERE document_id=?", (target,))
    with pytest.raises(CollectionImportError, match="target_changed"):
        transfer(src, dst, apply=True, refresh=True)


@pytest.mark.parametrize(
    "registry_key,path_key",
    [("registry_sha256", "registry_path"), ("source_registry_sha256", "source_registry_path")],
)
@pytest.mark.parametrize("selected", [False, True])
def test_typed_relation_transfers_owned_registry_and_both_page_mappings(
    pair, registry_key, path_key, selected
):
    from PIL import Image

    src, dst = pair
    a, b = original(src, "amendment"), original(src, "base")
    buf = io.BytesIO()
    Image.new("RGB", (2, 3), "white").save(buf, format="PNG")
    png = src.archive.blobs.put_bytes("source", buf.getvalue())
    references = []
    for doc in (a, b):
        with src.db.transaction() as conn:
            m = json.loads(src.own_document(conn, "source", doc["id"])["manifest"])
            native = "Actual source " + doc["id"]
            m["pages"] = [{"page": 1, "status": "native", "native_text": native, "text": native}]
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(m), doc["id"]))
        mapping = {
            "source_hash": doc["content_hash"],
            "physical_page": 1,
            "page": 1,
            "image_sha256": png.digest,
            "native_text_sha256": hashlib.sha256(native.encode()).hexdigest(),
            "native_text_unchanged": native,
            "no_crop": True,
            "width": 2,
            "height": 3,
            "source_original_path": doc["storage_path"],
            "source_PNG_path": "/old/private/scan.png",
            "PNG_CAS_path": "/old/verification-owner/files/blobs/" + png.digest,
        }
        proof = src.archive.blobs.put_bytes("source", dump(mapping).encode())
        references.append(
            {
                "physical_page": 1,
                "PNG_sha256": png.digest,
                "full_page_PNG": "/old/private/scan.png",
                "mapping_sha256": proof.digest,
                "mapping_path": "/old/private/" + doc["id"] + ".json",
            }
        )
    registry = {
        "owner_id": "source",
        "relations": [
            {
                "source_document_id": a["id"],
                "source_version": 1,
                "source_hash": a["content_hash"],
                "target_document_id": b["id"],
                "target_version": 1,
                "target_hash": b["content_hash"],
                "source_mapping_SHA": references[0]["mapping_sha256"],
                "source_fact_kind": "amends_base",
                "source_fact": {"source_locator": references[0], "target_identity": references[1]},
            }
        ],
        "legal_effect_reviewed": False,
    }
    options = {}
    if selected:
        c, d = original(src, "unrelated amendment"), original(src, "unrelated base")
        registry["relations"].append(
            {
                "source_document_id": c["id"],
                "source_version": 1,
                "source_hash": c["content_hash"],
                "target_document_id": d["id"],
                "target_version": 1,
                "target_hash": d["content_hash"],
                "source_fact": {"mapping_path": "/unrelated/proof.json", "mapping_sha256": "a" * 64},
            }
        )
        options["document_ids"] = [a["id"], b["id"]]
    blob = src.archive.blobs.put_bytes("source", dump(registry).encode())
    src.archive.add_relation(
        "source",
        a["id"],
        {
            "target_document_id": b["id"],
            "collection": "country:ID",
            "relation": "related",
            "locator": dump(
                {
                    registry_key: blob.digest,
                    path_key: "private/old-registry.json",
                    "review": "draft",
                    "effect": "unknown",
                }
            ),
        },
    )
    dry = transfer(src, dst, **options)
    assert dry["relations"] == 1
    result = transfer(src, dst, apply=True, **options)
    assert result["documents"] == 2
    with closing(dst.db.connect()) as conn:
        relation = dict(conn.execute("SELECT * FROM legal_relations").fetchone())
        assert relation["source_document_id"] == result["document_versions"][a["id"]]["document_id"]
        locator = json.loads(relation["locator"])
        path = dst.db.path.parent / locator[path_key]
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == locator[registry_key]
        assert b"/old/private" not in raw and b"old-registry" not in raw
        moved = json.loads(raw)
        assert moved["owner_id"] == "target" and moved["legal_effect_reviewed"] is False
        assert len(moved["relations"]) == 1
        if selected:
            assert moved["selection_provenance"]["source_registry_sha256"] == blob.digest
            assert moved["selection_provenance"]["source_relations"] == 2
            assert c["id"].encode() not in raw and d["id"].encode() not in raw
        for ref in moved["relations"][0]["source_fact"].values():
            image = (dst.db.path.parent / ref["full_page_PNG"]).read_bytes()
            assert hashlib.sha256(image).hexdigest() == ref["PNG_sha256"]
            mapping = (dst.db.path.parent / ref["mapping_path"]).read_bytes()
            assert hashlib.sha256(mapping).hexdigest() == ref["mapping_sha256"]
            assert b"/old/private" not in mapping and str(src.db.path.parent).encode() not in mapping
            mapped = json.loads(mapping)
            assert mapped["PNG_CAS_path"] == ref["full_page_PNG"]
            assert b"verification-owner" not in mapping
    before = facts(dst)
    assert transfer(src, dst, apply=True, **options)["unchanged_documents"] == 2
    assert facts(dst) == before


def test_multiple_registries_remain_independent_and_bind_their_own_relation(pair):
    src, dst = pair
    origins = []
    for number, key in enumerate(("registry_sha256", "source_registry_sha256")):
        a, b = original(src, f"amendment-{number}"), original(src, f"base-{number}")
        registry = {
            "owner_id": "source",
            "independent_evidence": number,
            "relations": [
                {
                    "source_document_id": a["id"],
                    "source_version": 1,
                    "source_hash": a["content_hash"],
                    "target_document_id": b["id"],
                    "target_version": 1,
                    "target_hash": b["content_hash"],
                }
            ],
        }
        blob = src.archive.blobs.put_bytes("source", dump(registry).encode())
        src.archive.add_relation(
            "source",
            a["id"],
            {
                "target_document_id": b["id"],
                "collection": "country:ID",
                "relation": "related",
                "locator": dump({key: blob.digest}),
            },
        )
        origins.append((a, b, key, blob.digest))
    selected = [doc["id"] for a, b, _, _ in origins for doc in (a, b)]
    assert transfer(src, dst, document_ids=selected)["relation_registries"] == 2
    result = transfer(src, dst, apply=True, document_ids=selected)
    assert result["registry_sha256"] is None
    assert len(set(result["registry_sha256_by_source"].values())) == 2
    with closing(dst.db.connect()) as conn:
        for number, (a, b, key, digest) in enumerate(origins):
            sid = result["document_versions"][a["id"]]["document_id"]
            tid = result["document_versions"][b["id"]]["document_id"]
            row = conn.execute("SELECT * FROM legal_relations WHERE source_document_id=?", (sid,)).fetchone()
            locator = json.loads(row["locator"])
            path_key = "registry_path" if key == "registry_sha256" else "source_registry_path"
            raw = (dst.db.data_dir / locator[path_key]).read_bytes()
            assert (
                hashlib.sha256(raw).hexdigest() == result["registry_sha256_by_source"][digest] == locator[key]
            )
            moved = json.loads(raw)
            assert moved["independent_evidence"] == number
            assert moved["source_import"]["source_registry_sha256"] == digest
            assert [(r["source_document_id"], r["target_document_id"]) for r in moved["relations"]] == [
                (sid, tid)
            ]
    before = facts(dst)
    assert transfer(src, dst, apply=True, document_ids=selected)["unchanged_documents"] == 4
    assert facts(dst) == before


@pytest.mark.parametrize("apply", [False, True])
def test_independent_registries_with_conflicting_mapping_alias_are_refused_before_writes(pair, apply):
    src, dst = pair
    scopes = []
    png = src.archive.blobs.put_bytes("source", b"Verified page pixels")
    for number in range(2):
        a, b = original(src, f"mapping-amendment-{number}"), original(src, f"mapping-base-{number}")
        native = f"Actual source paragraph {number}"
        with src.db.transaction() as conn:
            manifest = json.loads(src.own_document(conn, "source", a["id"])["manifest"])
            manifest["pages"] = [
                {
                    "page": 1,
                    "source_hash": a["content_hash"],
                    "status": "native",
                    "native_text": native,
                    "text": native,
                }
            ]
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), a["id"]))
        mapping = src.archive.blobs.put_bytes(
            "source",
            dump(
                {
                    "source_hash": a["content_hash"],
                    "physical_page": 1,
                    "image_sha256": png.digest,
                    "native_text_sha256": hashlib.sha256(native.encode()).hexdigest(),
                    "no_crop": True,
                }
            ).encode(),
        )
        registry = {
            "owner_id": "source",
            "relations": [
                {
                    "source_document_id": a["id"],
                    "source_version": 1,
                    "source_hash": a["content_hash"],
                    "target_document_id": b["id"],
                    "target_version": 1,
                    "target_hash": b["content_hash"],
                    "source_fact": {
                        "source_locator": {
                            "physical_page": 1,
                            "mapping_path": "mapping.json",
                            "mapping_sha256": mapping.digest,
                            "full_page_PNG": "page.png",
                            "PNG_sha256": png.digest,
                        }
                    },
                }
            ],
        }
        blob = src.archive.blobs.put_bytes("source", dump(registry).encode())
        src.archive.add_relation(
            "source",
            a["id"],
            {
                "target_document_id": b["id"],
                "collection": "country:ID",
                "relation": "related",
                "locator": dump({"registry_sha256": blob.digest}),
            },
        )
        scopes.append([a["id"], b["id"]])
    # Both packages have valid owned bytes and physical-page mappings alone.
    for selected in scopes:
        assert transfer(src, dst, document_ids=selected)["relation_registries"] == 1
    before, source_before = facts(dst), facts(src)
    before_files = set(dst.archive.blobs.blobs_dir.rglob("*"))
    with pytest.raises(CollectionImportError, match="^ambiguous_blob_alias$"):
        transfer(src, dst, apply=apply)
    assert facts(dst) == before and facts(src) == source_before
    assert set(dst.archive.blobs.blobs_dir.rglob("*")) == before_files


@pytest.mark.parametrize("collision", ["relative_absolute", "target_cas"])
def test_campaign_alias_collision_with_expanded_or_destination_path_is_preflight_only(pair, collision):
    src, dst = pair
    doc = original(src)
    catalogue = src.archive.blobs.put_bytes("source", b"Catalogue evidence")
    detail = src.archive.blobs.put_bytes("source", b"Different detail evidence")
    if collision == "relative_absolute":
        first_alias = "evidence.json"
        second_alias = str(src.db.data_dir.parents[2] / first_alias)
    else:
        first_alias = "catalogue.json"
        owner_digest = hashlib.sha256(b"target").hexdigest()
        second_alias = str(
            Path("files/blobs")
            / owner_digest
            / catalogue.digest[:2]
            / catalogue.digest[2:4]
            / catalogue.digest
        )
    (src.db.data_dir / "campaign.json").write_text(
        dump(
            {
                "owner_id": "source",
                "documents": {
                    "law": {
                        "document_id": doc["id"],
                        "assets": [
                            {"sha256": blob.digest, "cas_path": str(blob.path), "source_path": alias}
                            for blob, alias in ((catalogue, first_alias), (detail, second_alias))
                        ],
                    }
                },
            }
        )
    )
    before = facts(dst)
    before_files = set(dst.archive.blobs.blobs_dir.rglob("*"))
    with pytest.raises(CollectionImportError, match="^ambiguous_blob_alias$"):
        transfer(src, dst, apply=True)
    assert facts(dst) == before
    assert set(dst.archive.blobs.blobs_dir.rglob("*")) == before_files


def test_selected_registry_requires_dependency_even_without_database_edge(pair):
    src, dst = pair
    a, b, c = (original(src, name) for name in ("amendment", "base", "dependency"))
    registry = {
        "owner_id": "source",
        "relations": [
            {
                "source_document_id": a["id"],
                "target_document_id": c["id"],
            }
        ],
    }
    blob = src.archive.blobs.put_bytes("source", dump(registry).encode())
    src.archive.add_relation(
        "source",
        a["id"],
        {
            "target_document_id": b["id"],
            "collection": "country:ID",
            "relation": "related",
            "locator": dump({"source_registry_sha256": blob.digest}),
        },
    )
    before = facts(dst)
    with pytest.raises(CollectionImportError, match="selected_registry_dependencies_required:" + c["id"]):
        transfer(src, dst, apply=True, document_ids=[a["id"], b["id"]])
    assert facts(dst) == before


@pytest.mark.parametrize("source_advances", [False, True])
def test_explicit_refresh_model_mismatch_clears_prior_vectors(pair, monkeypatch, source_advances):
    src, dst = pair
    doc = original(src)
    transfer(src, dst, apply=True)
    assert facts(dst)["vectors"]
    # A checkpoint advancement is explicit, rather than a destructive retry.
    if source_advances:
        with src.db.transaction() as conn:
            manifest = json.loads(src.own_document(conn, "source", doc["id"])["manifest"])
            manifest["warnings"] = ["new source checkpoint evidence"]
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
            src.index.sync_document(conn, doc["id"], manifest)
    monkeypatch.setenv("EMBEDDING_MODEL", "another-test-model")
    with pytest.raises(CollectionImportError, match="requires_refresh"):
        transfer(src, dst, apply=True)
    result = transfer(src, dst, apply=True, refresh=True)
    assert result["reusable_vectors"] == 0 and not facts(dst)["vectors"]
    assert facts(dst)["jobs"][0]["status"] == "queued"
    # Reverting to the verified source model can explicitly restore the same checkpoint.
    monkeypatch.setenv("EMBEDDING_MODEL", "test-value")
    restored = transfer(src, dst, apply=True, refresh=True)
    assert restored["reusable_vectors"] > 0 and facts(dst)["vectors"]


def test_failure_late_in_relation_import_rolls_back_every_document(pair, monkeypatch):
    src, dst = pair
    a, b = original(src, "a"), original(src, "b")
    src.archive.add_relation(
        "source",
        a["id"],
        {
            "collection": "country:ID",
            "target_document_id": b["id"],
            "relation": "related",
            "locator": "source association",
        },
    )
    from app.archive import LegalArchive

    def fail(*args, **kwargs):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(LegalArchive, "add_relation", fail)
    before = facts(dst)
    with pytest.raises(RuntimeError, match="transaction failure"):
        transfer(src, dst, apply=True)
    assert facts(dst) == before


def test_same_owner_ocr_reuse_not_misattributed_to_target(pair):
    src, dst = pair
    doc = original(src)
    with src.db.transaction() as conn:
        m = json.loads(src.own_document(conn, "source", doc["id"])["manifest"])
        m["old_proof"] = {"protocol": "same-owner-pdf-page-reuse-v1"}
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(m), doc["id"]))
    assert any("OCR_reuse_unsupported" in b for b in transfer(src, dst)["blockers"])
    with pytest.raises(CollectionImportError, match="OCR_reuse_unsupported"):
        transfer(src, dst, apply=True)


def test_source_vector_checkpoint_completion_requires_explicit_refresh(pair):
    src, dst = pair
    doc = original(src)
    with src.db.transaction() as conn:
        conn.execute("DELETE FROM retrieval_vectors")
    first = transfer(src, dst, apply=True)
    assert first["reusable_vectors"] == 0
    with src.db.transaction() as conn:
        batch = src.index.pending(conn, doc["id"], src.embedding_fingerprint(), limit=128)
        src.index.store_vectors(
            conn, doc["id"], src.embedding_fingerprint(), batch, [[0.6, 0.8] for _ in batch]
        )
    with pytest.raises(CollectionImportError, match="requires_refresh"):
        transfer(src, dst, apply=True)
    refreshed = transfer(src, dst, apply=True, refresh=True)
    assert refreshed["reusable_vectors"] > 0 and facts(dst)["vectors"]


def test_dirty_source_index_does_not_transfer_cached_vectors(pair):
    src, dst = pair
    doc = original(src)
    with src.db.transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO retrieval_dirty VALUES(?)", (doc["id"],))
    assert transfer(src, dst)["reusable_vectors"] == 0
    assert transfer(src, dst, apply=True)["reusable_vectors"] == 0
    assert not facts(dst)["vectors"]


@pytest.mark.parametrize("mode", ["complete_vectors", "secondary", "no_embedding_configuration"])
def test_finished_text_import_is_not_claimed_or_reparsed(pair, monkeypatch, mode):
    src, dst = pair
    doc = original(src, level="secondary" if mode == "secondary" else "primary")
    if mode == "no_embedding_configuration":
        monkeypatch.delenv("EMBEDDING_API_KEY")

    def forbidden(*args, **kwargs):
        pytest.fail("A finished imported checkpoint must not call parsing or models")

    monkeypatch.setattr("app.parsing.parse_bounded", forbidden)
    monkeypatch.setattr("app.ingestion.embed_texts", forbidden)
    result = transfer(src, dst, apply=True)
    ident = result["document_versions"][doc["id"]]["document_id"]
    assert dst.collection.claim_processing(owner_id="target", document_ids=[ident]) is None
    with closing(dst.db.connect()) as conn:
        row = dst.own_document(conn, "target", ident)
        assert row["status"] == "ready"
        manifest = json.loads(row["manifest"])
        job = conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (ident,)).fetchone()
        assert job["status"] == "completed" and job["attempts"] == 0
        if mode == "complete_vectors":
            assert manifest["embedding_fingerprint"] == dst.embedding_fingerprint()
            assert not dst.index.pending(conn, ident, dst.embedding_fingerprint())
        else:
            assert "embedding_fingerprint" not in manifest
            assert not conn.execute("SELECT 1 FROM retrieval_vectors").fetchone()


def test_complete_import_with_no_cached_vectors_queues_only_embedding(pair, monkeypatch):
    src, dst = pair
    doc = original(src)
    with src.db.transaction() as conn:
        conn.execute("DELETE FROM retrieval_vectors")
    result = transfer(src, dst, apply=True)
    ident = result["document_versions"][doc["id"]]["document_id"]
    requests = []

    def forbidden(*args, **kwargs):
        pytest.fail("Missing vectors do not invalidate complete source text")

    def embed(texts):
        requests.append(texts)
        return [[0.6, 0.8] for _ in texts]

    monkeypatch.setattr("app.parsing.parse_bounded", forbidden)
    monkeypatch.setattr("app.ingestion.embed_texts", embed)
    claim = dst.collection.claim_processing(owner_id="target", document_ids=[ident])
    assert claim["phase"] == "embed"
    dst.collection.process_item(claim)
    assert len(requests) == 1 and requests[0]
    with closing(dst.db.connect()) as conn:
        assert dst.own_document(conn, "target", ident)["status"] == "ready"
        assert not dst.index.pending(conn, ident, dst.embedding_fingerprint())


def partial_original(src, *, remove_vector=True):
    body = "\n\n".join(
        f"Section {number}. Source obligations require accurate original context. "
        + "Applicable clauses and original context. " * 25
        for number in range(10)
    )
    doc = original(src, content=body.encode())
    with src.db.transaction() as conn:
        chunks = list(
            conn.execute(
                "SELECT rowid,chunk_id,retrieval_text FROM retrieval_chunks WHERE document_id=? ORDER BY ordinal",
                (doc["id"],),
            )
        )
        assert len(chunks) > 1
        if remove_vector:
            conn.execute("DELETE FROM retrieval_vectors WHERE chunk_rowid=?", (chunks[-1]["rowid"],))
        manifest = json.loads(src.own_document(conn, "source", doc["id"])["manifest"])
        manifest.pop("embedding_fingerprint", None)
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
        src.index.sync_document(conn, doc["id"], manifest)
    return doc, chunks


@pytest.mark.parametrize("remove_vector", [True, False])
def test_partial_paid_cache_is_transferred_and_worker_requests_only_missing_inputs(
    pair, monkeypatch, remove_vector
):
    src, dst = pair
    doc, chunks = partial_original(src, remove_vector=remove_vector)
    source_before = facts(src)
    result = transfer(src, dst, apply=True)
    target = result["document_versions"][doc["id"]]["document_id"]
    assert result["partial_vector_cache_documents"] == 1
    assert result["reusable_vectors"] == len(chunks) - int(remove_vector)
    with closing(dst.db.connect()) as conn:
        row = dst.own_document(conn, "target", target)
        assert row["status"] == "processing" and row["version"] == 1
        assert "embedding_fingerprint" not in json.loads(row["manifest"])
        job = conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (target,)).fetchone()
        assert job["phase"] == "embed" and job["status"] == "queued"
        pending = dst.index.pending(conn, target, dst.embedding_fingerprint())
        assert [r["chunk_id"] for r in pending] == ([chunks[-1]["chunk_id"]] if remove_vector else [])

    requests = []

    def embed(texts):
        requests.append(texts)
        return [[0.6, 0.8] for _ in texts]

    def parsing_not_needed(*args, **kwargs):
        pytest.fail("A complete imported text checkpoint must not be parsed or OCRed again")

    monkeypatch.setattr("app.ingestion.embed_texts", embed)
    monkeypatch.setattr("app.parsing.parse_bounded", parsing_not_needed)
    claim = dst.collection.claim_processing(owner_id="target", document_ids=[target])
    assert claim and claim["phase"] == "embed"
    dst.collection.process_item(claim)
    assert requests == ([[chunks[-1]["retrieval_text"]]] if remove_vector else [])
    assert facts(src) == source_before
    with closing(dst.db.connect()) as conn:
        row = dst.own_document(conn, "target", target)
        assert row["status"] == "ready"
        assert json.loads(row["manifest"])["embedding_fingerprint"] == dst.embedding_fingerprint()
        assert not dst.index.pending(conn, target, dst.embedding_fingerprint())
        assert (
            conn.execute("SELECT status FROM processing_jobs WHERE document_id=?", (target,)).fetchone()[0]
            == "completed"
        )
    assert dst.search("target", "Source obligations", country="ID")["items"] == []
    with pytest.raises(CollectionImportError, match="target_changed_since_import"):
        transfer(src, dst, apply=True, refresh=True)


@pytest.mark.parametrize("gate", ["incomplete", "secondary", "dirty", "model_mismatch"])
def test_partial_cache_cannot_bypass_text_purpose_index_or_model_gates(pair, monkeypatch, gate):
    src, dst = pair
    doc, _ = partial_original(src)
    with src.db.transaction() as conn:
        if gate == "incomplete":
            manifest = json.loads(src.own_document(conn, "source", doc["id"])["manifest"])
            manifest["quality"] = {"complete": False, "pending_pages": [1]}
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
        elif gate == "secondary":
            conn.execute(
                "UPDATE legal_classifications SET level='secondary' WHERE document_id=?", (doc["id"],)
            )
        elif gate == "dirty":
            conn.execute("INSERT OR IGNORE INTO retrieval_dirty VALUES(?)", (doc["id"],))
    if gate == "model_mismatch":
        monkeypatch.setenv("EMBEDDING_MODEL", "another-test-model")
    result = transfer(src, dst, apply=True)
    assert result["reusable_vectors"] == 0
    assert result["partial_vector_cache_documents"] == 0
    assert not facts(dst)["vectors"]


@pytest.mark.parametrize("stale_marker", [False, True])
def test_mixed_model_partial_cache_reuses_only_exact_current_model_inputs(pair, stale_marker):
    src, dst = pair
    doc, chunks = partial_original(src)
    previous_fingerprint = "a" * 64
    with src.db.transaction() as conn:
        conn.execute("INSERT INTO retrieval_models VALUES(?,2)", (previous_fingerprint,))
        conn.execute(
            "UPDATE retrieval_vectors SET fingerprint=? WHERE chunk_rowid=?",
            (previous_fingerprint, chunks[0]["rowid"]),
        )
        if stale_marker:
            manifest = json.loads(src.own_document(conn, "source", doc["id"])["manifest"])
            manifest["embedding_fingerprint"] = previous_fingerprint
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
            src.index.sync_document(conn, doc["id"], manifest)
    result = transfer(src, dst, apply=True)
    assert result["reusable_vectors"] == len(chunks) - 2
    assert result["partial_vector_cache_documents"] == 1
    target = result["document_versions"][doc["id"]]["document_id"]
    with closing(dst.db.connect()) as conn:
        assert {r["chunk_id"] for r in dst.index.pending(conn, target, dst.embedding_fingerprint())} == {
            chunks[0]["chunk_id"],
            chunks[-1]["chunk_id"],
        }
        assert "embedding_fingerprint" not in json.loads(dst.own_document(conn, "target", target)["manifest"])


@pytest.mark.parametrize("corruption", ["input", "dimension", "nonfinite", "nonunit"])
def test_partial_cache_corruption_is_rejected_before_any_target_copy(pair, corruption):
    import numpy as np

    src, dst = pair
    partial_original(src)
    with src.db.transaction() as conn:
        rowid = conn.execute("SELECT chunk_rowid FROM retrieval_vectors LIMIT 1").fetchone()[0]
        if corruption == "input":
            conn.execute("UPDATE retrieval_vectors SET input_hash='wrong' WHERE chunk_rowid=?", (rowid,))
        elif corruption == "dimension":
            conn.execute(
                "UPDATE retrieval_vectors SET dimension=3,vector=? WHERE chunk_rowid=?",
                (np.asarray([0.6, 0.8, 0], dtype="float32").tobytes(), rowid),
            )
        else:
            values = [float("nan"), 0.8] if corruption == "nonfinite" else [0.6, 0.3]
            conn.execute(
                "UPDATE retrieval_vectors SET vector=? WHERE chunk_rowid=?",
                (np.asarray(values, dtype="float32").tobytes(), rowid),
            )
    before = facts(dst)
    blobs_before = set(dst.db.files_dir.rglob("*"))
    with pytest.raises(CollectionImportError, match="vector"):
        transfer(src, dst, apply=True)
    assert facts(dst) == before
    assert set(dst.db.files_dir.rglob("*")) == blobs_before
