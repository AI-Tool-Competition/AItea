"""Account isolation, genuine input checkpoint reuse and atomic import boundaries."""

import json
import hashlib
import io
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
def test_typed_relation_transfers_owned_registry_and_both_page_mappings(pair, registry_key, path_key):
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
    dry = transfer(src, dst)
    assert dry["relations"] == 1
    result = transfer(src, dst, apply=True)
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
    assert transfer(src, dst, apply=True)["unchanged_documents"] == 2
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
