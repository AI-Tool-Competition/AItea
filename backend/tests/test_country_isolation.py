"""Immutable country membership cannot be bypassed with tags or collections."""

from contextlib import closing
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.countries import country_from_collection, document_country, resolve_country
from app.main import create_app
from app.service import AppError


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("EMBEDDING_API_KEY", raising=False)
    with TestClient(create_app(data_dir=tmp_path)) as client:
        assert client.post("/api/tea/auth/demo", json={}).status_code == 200
        bootstrap = client.get("/api/tea/bootstrap").json()
        yield client.app.state.service, bootstrap["user"]["id"], bootstrap["cases"][0]["id"]


def stored(
    workspace, country, *, identity="same-law", body=b"tea import rule", level="primary", kind_hint=None
):
    service, owner, _ = workspace
    blob = service.archive.blobs.put_bytes(owner, body)
    with service.db.transaction() as conn:
        doc = service.archive.store_original(
            conn,
            owner,
            blob,
            "Act.txt",
            "text/plain",
            "https://official.example/act.txt",
            collection=f"country:{country}",
            level=level,
            tags=[],
            identity_key=identity,
            kind_hint=kind_hint,
        )
        return dict(doc)


def classify(workspace, document_id, collection, *, level="primary", revision=0):
    service, owner, _ = workspace
    return service.archive.patch_classification(
        owner,
        document_id,
        {
            "collection": collection,
            "level": level,
            "tags": [],
            "expected_revision": revision,
        },
    )


def publish(workspace, document_id, start="2020-01-01", end=None):
    from app.catalog import MetadataPatch

    service, owner, _ = workspace
    # Process the same durable queue used by the live collector.
    while job := service.collection.claim_processing():
        service.collection.process_item(job)
    service.catalog.patch_document(
        owner,
        document_id,
        MetadataPatch(
            expected_revision=service.catalog.metadata_for(document_id)["metadata_revision"],
            kind="law",
            review_status="published",
            effective_from=start,
            effective_to=end,
        ),
    )


def retrieved(workspace, *, country=None, collection=None, case_id=None, historical=False, as_of=None):
    service, owner, _ = workspace
    with closing(service.db.connect()) as conn:
        rows, _, _ = service.archive.retrieval_rows(
            conn,
            owner,
            case_id,
            collection,
            as_of or date(2026, 10, 12),
            historical,
            country=country,
        )
        return {row["id"] for row in rows}


@pytest.mark.parametrize(
    "collection",
    ["country:id", "COUNTRY:ID", "country:Indonesia", "country:I", "country:", "country:ID:x", "country:ID "],
)
def test_country_namespace_rejects_noncanonical_codes(collection):
    with pytest.raises(AppError) as error:
        country_from_collection(collection)
    assert error.value.status == 422


def test_country_namespace_and_extensible_destination_aliases():
    assert country_from_collection("country:ID") == "ID"
    assert country_from_collection("country:SG") == "SG"
    assert country_from_collection("ordinary-country-group") is None
    assert resolve_country("印度尼西亚") == resolve_country("Indonesia") == resolve_country("ID") == "ID"
    assert resolve_country("新加坡") == "SG"
    assert resolve_country("future destination") is None


def test_same_url_and_identity_have_separate_country_families_but_shared_blob(workspace):
    indonesian, singaporean = stored(workspace, "ID"), stored(workspace, "SG")
    assert indonesian["id"] != singaporean["id"]
    assert indonesian["family_id"] != singaporean["family_id"]
    assert indonesian["version"] == singaporean["version"] == 1
    assert indonesian["storage_path"] == singaporean["storage_path"]
    service, _, _ = workspace
    with closing(service.db.connect()) as conn:
        assert document_country(conn, indonesian["id"]) == "ID"
        assert document_country(conn, singaporean["id"]) == "SG"


def test_changing_tags_or_ordinary_groups_does_not_change_country(workspace):
    indonesian = stored(workspace, "ID")
    classify(workspace, indonesian["id"], "arbitrary", level="secondary")
    with pytest.raises(AppError) as error:
        classify(workspace, indonesian["id"], "country:SG")
    assert error.value.status == 422
    service, _, _ = workspace
    with closing(service.db.connect()) as conn:
        assert document_country(conn, indonesian["id"]) == "ID"
        assert not conn.execute(
            "SELECT 1 FROM legal_classifications WHERE document_id=? AND collection='country:SG'",
            (indonesian["id"],),
        ).fetchone()


@pytest.mark.parametrize("collection", ["country:ID", "bridge"])
def test_cross_country_relationship_cannot_use_an_ordinary_group_as_bridge(workspace, collection):
    service, owner, _ = workspace
    indonesian, singaporean = stored(workspace, "ID"), stored(workspace, "SG")
    classify(workspace, singaporean["id"], "bridge")
    with pytest.raises(AppError) as error:
        service.archive.add_relation(
            owner,
            indonesian["id"],
            {
                "target_document_id": singaporean["id"],
                "collection": collection,
            },
        )
    assert error.value.status == 422
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM legal_relations").fetchone()[0] == 0


def test_country_relation_requires_already_classified_anchor(workspace):
    service, owner, _ = workspace
    unknown = service.store_document(owner, "unknown.txt", b"tea unknown anchor", "text/plain")
    target = stored(workspace, "ID", level="secondary")
    with pytest.raises(AppError):
        service.archive.add_relation(
            owner, unknown["id"], {"target_document_id": target["id"], "collection": "country:ID"}
        )
    with closing(service.db.connect()) as conn:
        assert document_country(conn, unknown["id"]) is None


def test_relationship_within_same_country_keeps_exact_versions(workspace):
    service, owner, _ = workspace
    main = stored(workspace, "ID", identity="main")
    child = stored(workspace, "ID", identity="child", level="secondary")
    relation = service.archive.add_relation(
        owner, main["id"], {"target_document_id": child["id"], "collection": "country:ID"}
    )
    assert relation["source_document_id"] == main["id"] and relation["target_document_id"] == child["id"]


def test_country_search_is_explicit_and_legacy_default_never_mixes_countries(workspace):
    service, owner, _ = workspace
    indonesian, singaporean = stored(workspace, "ID"), stored(workspace, "SG")
    publish(workspace, indonesian["id"])
    publish(workspace, singaporean["id"])
    legacy = service.store_document(owner, "legacy.txt", b"tea legacy", "text/plain")
    assert retrieved(workspace, country="ID") == {indonesian["id"]}
    assert retrieved(workspace, country="SG") == {singaporean["id"]}
    assert retrieved(workspace) == {legacy["id"]}
    assert retrieved(workspace, collection="country:ID") == set()


def test_country_search_includes_only_this_cases_business_materials(workspace):
    service, owner, case_id = workspace
    law = stored(workspace, "ID")
    publish(workspace, law["id"])
    local = service.store_document(owner, "case.txt", b"tea business facts", "text/plain", case_id=case_id)
    library = service.store_document(owner, "public.txt", b"tea public unrelated", "text/plain")
    found = retrieved(workspace, country="ID", case_id=case_id, collection="country:ID")
    assert found == {law["id"], local["id"]} and library["id"] not in found


def test_ordinary_primary_collection_cannot_promote_country_secondary_into_rag(workspace):
    child = stored(workspace, "ID", level="secondary")
    publish(workspace, child["id"])
    assert classify(workspace, child["id"], "bridge", level="primary")["legal_level"] == "secondary"
    assert retrieved(workspace, country="ID") == set()
    assert retrieved(workspace, country="ID", collection="bridge") == set()
    service, _, _ = workspace
    with closing(service.db.connect()) as conn:
        assert not service.archive.can_index(conn, child["id"])


def test_first_country_assignment_keeps_versions_and_future_unscoped_uploads_separate(workspace):
    service, owner, _ = workspace
    old = service.store_document(owner, "Act.txt", b"tea old law", "text/plain")
    current = service.store_document(owner, "Act.txt", b"tea current law", "text/plain")
    publish(workspace, old["id"], "2020-01-01", "2022-12-31")
    publish(workspace, current["id"], "2023-01-01")
    classify(workspace, current["id"], "country:ID")
    assert retrieved(workspace, country="ID") == set()
    publish(workspace, old["id"], "2020-01-01", "2022-12-31")
    publish(workspace, current["id"], "2023-01-01")
    unscoped = service.store_document(owner, "Act.txt", b"tea later unscoped upload", "text/plain")
    assert unscoped["family_id"] != current["family_id"] or unscoped["version"] == 1
    assert retrieved(workspace, country="ID") == {current["id"]}
    assert retrieved(workspace, country="ID", historical=True, as_of=date(2021, 1, 1)) == {old["id"]}
    assert retrieved(workspace) == {unscoped["id"]}


def test_existing_cross_scope_relation_prevents_partial_country_reclassification(workspace):
    service, owner, _ = workspace
    left = service.store_document(owner, "left.txt", b"tea left", "text/plain")
    right = service.store_document(owner, "right.txt", b"tea right", "text/plain")
    classify(workspace, right["id"], "legacy")
    service.archive.add_relation(
        owner, left["id"], {"target_document_id": right["id"], "collection": "legacy"}
    )
    with pytest.raises(AppError):
        classify(workspace, left["id"], "country:ID")
    with closing(service.db.connect()) as conn:
        assert document_country(conn, left["id"]) is None


def test_country_membership_survives_restart_and_cannot_be_removed_by_classification(workspace):
    from app.service import Service

    service, owner, _ = workspace
    law = stored(workspace, "ID")
    classify(workspace, law["id"], "country:ID", level="secondary", revision=1)
    restarted = Service(service.db.data_dir)
    with closing(restarted.db.connect()) as conn:
        assert document_country(conn, law["id"]) == "ID"
    with pytest.raises(AppError):
        restarted.archive.patch_classification(
            owner,
            law["id"],
            {"collection": "country:SG", "level": "primary", "tags": [], "expected_revision": 0},
        )


def test_case_document_country_must_agree_with_case_destination(workspace):
    service, owner, case_id = workspace
    doc = service.store_document(owner, "case-law.txt", b"tea regulation", "text/plain", case_id=case_id)
    with pytest.raises(AppError) as error:
        classify(workspace, doc["id"], "country:SG")
    assert error.value.status == 422
    assert classify(workspace, doc["id"], "country:ID")["country"] == "ID"


def test_raw_attachment_exposes_parent_country_without_an_independent_legal_tier(workspace):
    service, owner, _ = workspace
    law = service.store_document(owner, "law.txt", b"tea rule", "text/plain")
    attachment = service.store_document(
        owner, "image.png", b"unparsed scan", "image/png", role="secondary", parent_id=law["id"]
    )
    classify(workspace, law["id"], "country:ID")
    with closing(service.db.connect()) as conn:
        doc = service.document_dict(service.own_document(conn, owner, attachment["id"]), conn=conn)
    assert doc["country"] == "ID" and doc["legal_level"] == "attachment"


def test_semantic_ranking_receives_only_matching_country_and_primary_tier(workspace, monkeypatch):
    import json

    service, owner, _ = workspace
    main = stored(workspace, "ID", identity="main")
    child = stored(workspace, "ID", identity="child", level="secondary")
    other = stored(workspace, "SG")
    for doc in (main, child, other):
        publish(workspace, doc["id"])
    classify(workspace, child["id"], "ordinary", level="primary")
    monkeypatch.setattr(service, "embedding_fingerprint", lambda: "scope-test")
    monkeypatch.setattr("app.ingestion.embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])
    # Simulate vectors retained from older classifications: ranking must still
    # filter immutable country and today's primary/secondary membership first.
    with service.db.transaction() as conn:
        for doc in (main, child, other):
            row = conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()
            manifest = json.loads(row["manifest"])
            manifest["embedding_fingerprint"] = "scope-test"
            for chunk in manifest["chunks"]:
                chunk["embedding"] = [1.0, 0.0]
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), doc["id"]))
    result = service.search(owner, "semantic-only-question", country="ID")
    assert result["mode"] == "hybrid"
    assert {hit["document_id"] for hit in result["items"]} == {main["id"]}


def test_first_country_assignment_resets_default_business_approval(workspace):
    from pathlib import Path

    service, owner, _ = workspace
    doc = service.store_document(owner, "new-law.txt", b"tea unpublished law", "text/plain")
    assert doc["metadata"]["review_status"] == "published" and doc["metadata"]["kind"] == "business"
    updated = classify(workspace, doc["id"], "country:ID")
    assert (
        updated["metadata"] | {"kind": "law", "review_status": "draft", "jurisdiction": "ID"}
        == updated["metadata"]
    )
    assert updated["metadata_revision"] == 1 and updated["reviewed_at"] is None
    assert retrieved(workspace, country="ID") == set()
    with closing(service.db.connect()) as conn:
        row = service.own_document(conn, owner, doc["id"])
        assert Path(row["storage_path"]).read_bytes() == b"tea unpublished law"
        assert row["content_hash"] == doc["content_hash"]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM document_metadata_history WHERE document_id=?", (doc["id"],)
            ).fetchone()[0]
            == 1
        )
    publish(workspace, doc["id"])
    assert retrieved(workspace, country="ID") == {doc["id"]}


@pytest.mark.parametrize(
    "kind,expected", [("law", "law"), ("guidance", "guidance"), ("case", "case"), ("demo", "law")]
)
def test_country_assignment_keeps_legal_kind_and_resets_existing_review(workspace, kind, expected):
    from app.catalog import MetadataPatch
    import json

    service, owner, _ = workspace
    old = service.store_document(owner, "reviewed-law.txt", b"tea reviewed v1", "text/plain")
    current = service.store_document(owner, "reviewed-law.txt", b"tea reviewed v2", "text/plain")
    for doc in (old, current):
        service.catalog.patch_document(
            owner,
            doc["id"],
            MetadataPatch(
                expected_revision=0, kind=kind, review_status="published", effective_from="2020-01-01"
            ),
        )
    classify(workspace, current["id"], "country:ID")
    for doc in (old, current):
        metadata = service.catalog.metadata_for(doc["id"])
        assert metadata["metadata"]["kind"] == expected and metadata["metadata"]["review_status"] == "draft"
        assert metadata["metadata"]["jurisdiction"] == "ID" and metadata["metadata_revision"] == 2
        with closing(service.db.connect()) as conn:
            history = conn.execute(
                "SELECT payload FROM document_metadata_history WHERE document_id=? ORDER BY revision",
                (doc["id"],),
            ).fetchall()
            assert [json.loads(x[0])["review_status"] for x in history] == ["published", "draft"]
            assert (
                conn.execute(
                    "SELECT reviewed_by FROM document_metadata WHERE document_id=?", (doc["id"],)
                ).fetchone()[0]
                is None
            )


def test_repeated_country_membership_does_not_revoke_approval_but_new_version_starts_draft(workspace):
    service, _, _ = workspace
    original = stored(workspace, "ID")
    publish(workspace, original["id"])
    prior = service.catalog.metadata_for(original["id"])
    classify(workspace, original["id"], "country:ID", revision=1)
    stored(workspace, "ID")
    assert service.catalog.metadata_for(original["id"]) == prior
    latest = stored(workspace, "ID", body=b"tea changed unreviewed rule")
    assert service.catalog.metadata_for(latest["id"])["metadata"]["review_status"] == "draft"
    assert service.catalog.metadata_for(original["id"]) == prior
    assert retrieved(workspace, country="ID") == set()


@pytest.mark.parametrize("kind", ["case", "guidance"])
def test_trusted_kind_hint_initializes_country_document_without_overwriting_review(workspace, kind):
    from app.catalog import MetadataPatch

    service, owner, _ = workspace
    doc = stored(workspace, "ID", kind_hint=kind)
    metadata = service.catalog.metadata_for(doc["id"])
    assert metadata["metadata"]["kind"] == kind and metadata["metadata"]["review_status"] == "draft"
    reviewed = service.catalog.patch_document(
        owner,
        doc["id"],
        MetadataPatch(
            expected_revision=metadata["metadata_revision"],
            review_status="published",
        ),
    )
    same = stored(workspace, "ID", kind_hint="law")
    assert same["id"] == doc["id"]
    assert service.catalog.metadata_for(doc["id"]) == reviewed
