"""Account isolation, genuine input checkpoint reuse and atomic import boundaries."""

import hashlib
import json
from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest
from app.bootstrap import Service
from app.contexts.ingestion.application.collection_import import (
    CollectionImportError,
    import_collected_resources,
)
from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID, ensure_principal
from app.contexts.documents.infrastructure.parsers.document_parser import parse_bounded
from app.shared.domain.values import dump, now


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
            conn.execute(
                "UPDATE documents SET manifest=?,status='ready' WHERE id=?", (dump(manifest), doc["id"])
            )
            src.index.sync_document(conn, doc["id"], manifest)
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


def test_mineru_chart_checkpoint_identity_survives_owner_cas_rebinding():
    from app.contexts.ingestion.application.collection_import import _rewrite_manifest
    from app.contexts.documents.infrastructure.providers.native_asset_ocr import assets

    source_hash = hashlib.sha256(b"pdf source").hexdigest()
    image = b"chart source image"
    image_hash = hashlib.sha256(image).hexdigest()
    old_path = f"files/blobs/{'a' * 64}/{image_hash[:2]}/{image_hash[2:4]}/{image_hash}"
    new_path = f"files/blobs/{'b' * 64}/{image_hash[:2]}/{image_hash[2:4]}/{image_hash}"
    element_key = "mineru-000001-000002"
    manifest = {
        "parser": "mineru",
        "source_hash": source_hash,
        "remote_parse": {"provider": "mineru", "state": "done"},
        "mineru_chart_assets": [
            {
                "asset_key": "mineru-chart-" + hashlib.sha256(element_key.encode()).hexdigest()[:24],
                "element_key": element_key,
                "block_type": "chart",
                "source_hash": source_hash,
                "image_hash": image_hash,
                "size": len(image),
                "image_blob": {
                    "content_hash": image_hash,
                    "size": len(image),
                    "storage_path": old_path,
                },
                "locator": {"kind": "pdf", "page": 1, "source_hash": source_hash},
            }
        ],
    }
    identity = assets(manifest)[0]["input_identity"]
    inserted = deepcopy(manifest["mineru_chart_assets"][0])
    inserted["element_key"] = "mineru-000001-000001"
    inserted["asset_key"] = (
        "mineru-chart-" + hashlib.sha256(inserted["element_key"].encode()).hexdigest()[:24]
    )
    manifest["mineru_chart_assets"].insert(0, inserted)
    assert (
        next(value for value in assets(manifest) if value["element_key"] == element_key)["input_identity"]
        == identity
    )
    rebound, _ = _rewrite_manifest(
        manifest,
        {old_path: image_hash},
        {image_hash: new_path},
        {},
        bound_hash=source_hash,
        target_owner="target",
    )
    target_asset = next(value for value in assets(rebound) if value["element_key"] == element_key)
    assert target_asset["image_blob"]["storage_path"] == new_path
    assert target_asset["input_identity"] == identity


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


def test_multiversion_family_refused_before_apply(pair):
    src, dst = pair
    original(src)
    original(src, content=b"Amended source original")
    before = facts(dst)
    assert any(x.startswith("unsupported_multiversion_family") for x in transfer(src, dst)["blockers"])
    with pytest.raises(CollectionImportError, match="unsupported_multiversion"):
        transfer(src, dst, apply=True)
    assert facts(dst) == before


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
        locator = json.loads(relation["locator"])
        assert locator["source_locator"] == "p. 1"
        assert locator["source_version"] == locator["target_version"] == 1
        assert locator["source_hash"] == supplement["content_hash"]
        assert locator["target_hash"] == base["content_hash"]
        origin = locator["collection_relation_import"]
        assert origin["source_document_id"] == supplement["id"]
        assert origin["target_document_id"] == base["id"]
        assert origin["source_owner_sha256"] == hashlib.sha256(b"source").hexdigest()
        assert json.loads((dst.db.data_dir / origin["archive_path"]).read_bytes())["source_locator"] == "p. 1"
        assert origin["source_locator_sha256"] == hashlib.sha256(b"p. 1").hexdigest()
    before = facts(dst)
    assert (
        transfer(src, dst, document_ids=[supplement["id"], base["id"]], apply=True)["unchanged_documents"]
        == 2
    )
    assert facts(dst) == before
    with closing(dst.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM legal_relations").fetchone()[0] == 1


def test_user_metadata_edit_refuses_refresh(pair):
    src, dst = pair
    doc = original(src)
    result = transfer(src, dst, apply=True)
    target = result["document_versions"][doc["id"]]["document_id"]
    with dst.db.transaction() as conn:
        conn.execute("UPDATE document_metadata SET revision=revision+1 WHERE document_id=?", (target,))
    with pytest.raises(CollectionImportError, match="target_changed"):
        transfer(src, dst, apply=True, refresh=True)


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
    from app.contexts.documents.application.archive import LegalArchive

    def fail(*args, **kwargs):
        raise RuntimeError("injected transaction failure")

    monkeypatch.setattr(LegalArchive, "add_relation", fail)
    before = facts(dst)
    with pytest.raises(RuntimeError, match="transaction failure"):
        transfer(src, dst, apply=True)
    assert facts(dst) == before
