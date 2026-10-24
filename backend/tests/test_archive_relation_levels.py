"""Link legal originals without granting a secondary regulation RAG eligibility."""

import json
from contextlib import closing

import pytest
from app.bootstrap import Service
from app.contexts.documents.application.contracts import LegalRelationInput
from app.shared.domain.errors import AppError
from app.shared.domain.values import now


@pytest.fixture
def service(tmp_path):
    value = Service(tmp_path)
    with value.db.transaction() as conn:
        for owner in ("owner", "other"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 1, now()))
    return value


def original(service, key, *, owner="owner", country="ID", level="secondary", parent=None):
    blob = service.archive.blobs.put_bytes(owner, key.encode())
    with service.db.transaction() as conn:
        return dict(
            service.archive.store_original(
                conn,
                owner,
                blob,
                key + ".txt",
                "text/plain",
                "https://official.example/" + key,
                collection="country:" + country,
                level=level,
                tags=["source:official"],
                identity_key=key,
                parent_id=parent,
                kind_hint="law" if level != "attachment" else None,
            )
        )


def state(service, document):
    with closing(service.db.connect()) as conn:
        return {
            "document": dict(service.own_document(conn, document["owner_id"], document["id"])),
            "metadata": service.catalog.metadata_for(document["id"], conn=conn),
            "classifications": [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM legal_classifications WHERE document_id=? ORDER BY collection",
                    (document["id"],),
                )
            ],
            "jobs": [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM processing_jobs WHERE document_id=?", (document["id"],)
                )
            ],
            "indexable": service.archive.can_index(conn, document["id"]),
        }


def link(service, source, target, *, relation="related", collection="country:ID"):
    payload = LegalRelationInput(
        target_document_id=target["id"],
        collection=collection,
        relation=relation,
        locator=json.dumps({"kind": "amends_base", "review": "draft", "page": 2}),
    )
    return service.archive.add_relation("owner", source["id"], payload.model_dump())


@pytest.mark.parametrize(
    "source_level,target_level",
    [("secondary", "secondary"), ("secondary", "primary"), ("primary", "secondary")],
)
def test_relation_preserves_existing_levels_metadata_tags_and_processing(service, source_level, target_level):
    source = original(service, "source", level=source_level)
    target = original(service, "target", level=target_level)
    before = state(service, source), state(service, target)
    result = link(service, source, target)
    assert result["source_document_id"] == source["id"] and result["target_document_id"] == target["id"]
    assert (state(service, source), state(service, target)) == before
    assert link(service, source, target)["id"] == result["id"]
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM legal_relations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 0
    assert service.search("owner", "source target", country="ID", collection="country:ID")["items"] == []


def test_secondary_citation_keeps_exact_old_versions_after_new_original(service):
    source = original(service, "source")
    target = original(service, "target")
    edge = link(service, source, target, relation="cites")
    newer = original(service, "target", level="secondary")
    assert newer["id"] == target["id"]
    blob = service.archive.blobs.put_bytes("owner", b"actual amended original")
    with service.db.transaction() as conn:
        newer = service.archive.store_original(
            conn,
            "owner",
            blob,
            "target.txt",
            "text/plain",
            "https://official.example/target",
            collection="country:ID",
            level="secondary",
            tags=["source:official"],
            identity_key="target",
            kind_hint="law",
        )
        assert newer["version"] == 2 and newer["id"] != target["id"]
    result = service.archive.relations("owner", source["id"])["items"]
    assert len(result) == 1 and result[0]["id"] == edge["id"]
    assert result[0]["target_document_id"] == target["id"] and result[0]["target_version"] == 1


@pytest.mark.parametrize("side", ["source", "target"])
def test_ordinary_attachment_cannot_link_as_an_independent_regulation(service, side):
    source, target = original(service, "source"), original(service, "target")
    attachment = original(service, "attachment", level="attachment", parent=source["id"])
    before = state(service, source), state(service, target), state(service, attachment)
    with pytest.raises(AppError) as exc:
        link(service, attachment if side == "source" else source, attachment if side == "target" else target)
    assert exc.value.code == "invalid_relation"
    assert (state(service, source), state(service, target), state(service, attachment)) == before


@pytest.mark.parametrize("problem", ["other_owner", "other_country", "missing_collection"])
def test_relation_refusals_preserve_secondary_source_and_zero_links(service, problem):
    source = original(service, "source")
    target = original(
        service,
        "target",
        owner="other" if problem == "other_owner" else "owner",
        country="SG" if problem == "other_country" else "ID",
    )
    before = state(service, source), state(service, target)
    with pytest.raises(AppError):
        link(
            service, source, target, collection="missing" if problem == "missing_collection" else "country:ID"
        )
    assert (state(service, source), state(service, target)) == before
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM legal_relations").fetchone()[0] == 0
