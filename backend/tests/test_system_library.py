"""Shared official originals do not weaken private or legal review boundaries."""

import hashlib
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth import Auth, COOKIE
from app.catalog import MetadataPatch
from app.db import dump, now
from app.main import create_app
from app.models import ClassificationPatch, LegalRelationInput
from app.service import AppError, Service
from app.system_library import SYSTEM_OWNER_ID, ensure_principal, official_source_allowed


@pytest.fixture
def service(tmp_path):
    value = Service(tmp_path)
    with value.db.transaction() as conn:
        ensure_principal(conn)
        for owner in ("alice", "bob", "reviewer"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 0, now()))
    return value


def original(
    service,
    key,
    *,
    owner=SYSTEM_OWNER_ID,
    country="ID",
    level="primary",
    parent=None,
    body=None,
    version_key=None,
    source_url=None,
):
    raw = (body or f"tea lawful import clause {key}").encode()
    blob = service.archive.blobs.put_bytes(owner, raw)
    url = source_url or ("https://www.bi.go.id/" if country == "ID" else "https://sso.agc.gov.sg/") + key
    with service.db.transaction() as conn:
        doc = dict(
            service.archive.store_original(
                conn,
                owner,
                blob,
                key + ".txt",
                "text/plain",
                url,
                collection="country:" + country,
                level=level,
                tags=["public"],
                identity_key=version_key or key,
                parent_id=parent,
            )
        )
        if level != "attachment":
            manifest = {
                "elements": [{"element_key": "e1", "content_text": raw.decode()}],
                "chunks": [
                    {
                        "id": "chunk-1",
                        "text": raw.decode(),
                        "retrieval_text": raw.decode(),
                        "locator": {"element_key": "e1"},
                    }
                ],
            }
            conn.execute(
                "UPDATE documents SET manifest=?,status='ready' WHERE id=?", (dump(manifest), doc["id"])
            )
            service.index.sync_document(conn, doc["id"])
        return doc


def approve(service, doc, *, start="2020-01-01", end=None, reviewed_by="reviewer"):
    service.catalog.patch_document(
        doc["owner_id"],
        doc["id"],
        MetadataPatch(
            expected_revision=service.catalog.metadata_for(doc["id"])["metadata_revision"],
            review_status="published",
            effective_from=start,
            effective_to=end,
        ),
    )
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE document_metadata SET reviewed_by=? WHERE document_id=?", (reviewed_by, doc["id"])
        )


def ids(result):
    return {item.get("id", item.get("document_id")) for item in result["items"]}


@pytest.mark.parametrize(
    "url,country",
    [
        ("https://peraturan.bpk.go.id/Download/x.pdf", "ID"),
        ("https://www.bi.go.id/x.pdf", "ID"),
        ("https://s.mkri.id/x.pdf", "ID"),
        ("https://www.mkri.id/x.pdf", "ID"),
        ("https://jdih.mkri.id/x.pdf", "ID"),
        ("https://sso.agc.gov.sg/Act", "SG"),
        ("https://isomer-user-content.by.gov.sg/x.pdf", "SG"),
        ("https://docs.wto.org/x.pdf", "ID"),
        ("https://asean.org/x.pdf", "SG"),
        ("https://fta.mofcom.gov.cn/x.pdf", "ID"),
        ("https://www.dfat.gov.au/x.pdf", "ID"),
        ("https://fta.mofcom.gov.cn/x.pdf", "SG"),
        ("https://www.dfat.gov.au/x.pdf", "SG"),
    ],
)
def test_only_static_official_https_identity_is_admitted(url, country):
    assert official_source_allowed(url, country)


@pytest.mark.parametrize(
    "url,country",
    [
        ("http://www.bi.go.id/x", "ID"),
        ("https://example.org/x", "ID"),
        ("https://www.bi.go.id.evil.example/x", "ID"),
        ("https://www.bi.go.id@evil.example/x", "ID"),
        ("https://user:secret@www.bi.go.id/x", "ID"),
        ("https://www.bi.go.id:444/x", "ID"),
        ("https://www.bi.go.id./x", "ID"),
        ("https://127.0.0.1/x", "ID"),
        ("https://www.bi.go.id/x", "SG"),
        ("https://sso.agc.gov.sg/x", "ID"),
        ("https://arbitrary.asean.org/x", "SG"),
        ("https://www.bi.go.id/x", "XX"),
        ("https://bad..go.id/x", "ID"),
        ("https://-bad.go.id/x", "ID"),
        ("\x01https://www.bi.go.id/x", "ID"),
    ],
)
def test_nonofficial_source_cannot_become_public_by_tag(service, url, country):
    assert not official_source_allowed(url, country)
    with pytest.raises(AppError) as error:
        original(service, "blocked", country=country, source_url=url)
    assert error.value.code == "system_source_required"
    assert service.documents("alice", country="ID")["total"] == 0


def test_principal_is_idempotent_without_credentials_and_cannot_login(service):
    auth = Auth(service)
    with service.db.transaction() as conn:
        assert ensure_principal(conn) == SYSTEM_OWNER_ID
        row = conn.execute("SELECT * FROM users WHERE id=?", (SYSTEM_OWNER_ID,)).fetchone()
        assert row["email"] is row["password_hash"] is None and row["is_demo"] == 0
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 15
    with pytest.raises(AppError) as error:
        auth.session(SYSTEM_OWNER_ID)
    assert error.value.code == "system_principal"
    token = "forged-system-session"
    with service.db.transaction() as conn:
        conn.execute(
            "INSERT INTO sessions VALUES(?,?,?)",
            (
                hashlib.sha256(token.encode()).hexdigest(),
                SYSTEM_OWNER_ID,
                (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            ),
        )
    assert auth.current(token) is None
    with service.db.transaction() as conn:
        with pytest.raises(ValueError, match="登录会话"):
            ensure_principal(conn)


def test_principal_collision_fails_without_rewriting_user(service):
    with service.db.transaction() as conn:
        conn.execute("UPDATE users SET email='collision@example.test' WHERE id=?", (SYSTEM_OWNER_ID,))
        with pytest.raises(ValueError, match="冲突"):
            ensure_principal(conn)
        assert (
            conn.execute("SELECT email FROM users WHERE id=?", (SYSTEM_OWNER_ID,)).fetchone()[0]
            == "collision@example.test"
        )


def test_country_library_reads_are_shared_private_documents_stay_private(service):
    id_doc = original(service, "id-public")
    sg_doc = original(service, "sg-public", country="SG")
    private = original(service, "alice-private", owner="alice")
    for owner in ("alice", "bob"):
        listed = service.documents(owner, country="ID")
        assert id_doc["id"] in ids(listed) and sg_doc["id"] not in ids(listed)
        public = next(item for item in listed["items"] if item["id"] == id_doc["id"])
        assert public["source_scope"] == "system"
        assert service.document_detail(owner, id_doc["id"])["chunks"][0]["text"].endswith("id-public")
        assert id_doc["id"] not in ids(service.documents(owner))
        assert ids(service.documents(owner, country="SG")) == {sg_doc["id"]}
    assert private["id"] in ids(service.documents("alice", country="ID"))
    assert ids(service.documents("alice", country="ID", source_scope="system")) == {id_doc["id"]}
    assert ids(service.documents("alice", country="ID", source_scope="personal")) == {private["id"]}
    assert private["id"] not in ids(service.documents("bob", country="ID"))
    with pytest.raises(AppError) as error:
        service.document_detail("bob", private["id"])
    assert error.value.status == 404


def test_draft_secondary_and_attachment_are_visible_without_rag_or_vectors(service):
    primary = original(service, "draft")
    secondary = original(service, "secondary", level="secondary")
    attachment = original(service, "annex", level="attachment", parent=primary["id"])
    approve(service, secondary)
    assert ids(service.documents("bob", country="ID")) == {primary["id"], secondary["id"], attachment["id"]}
    assert service.search("bob", "tea lawful", country="ID")["items"] == []
    assert service.document_detail("bob", secondary["id"])["chunks"]
    assert service.document_detail("bob", attachment["id"])["chunks"] == []
    assert ids(service.documents("bob", country="ID", parent_id=primary["id"])) == {attachment["id"]}
    with closing(service.db.connect()) as conn:
        assert not service.archive.can_index(conn, secondary["id"])
        assert not conn.execute(
            "SELECT 1 FROM processing_jobs WHERE document_id=?", (attachment["id"],)
        ).fetchone()
        assert not conn.execute(
            "SELECT 1 FROM retrieval_chunks WHERE document_id=?", (attachment["id"],)
        ).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 0


@pytest.mark.parametrize(
    "start,reviewer,expected",
    [
        (None, "reviewer", False),
        ("2020-01-01", None, False),
        ("2020-01-01", SYSTEM_OWNER_ID, False),
        ("2020-01-01", "reviewer", True),
    ],
)
def test_shared_rag_needs_known_effective_date_and_real_reviewer(service, start, reviewer, expected):
    doc = original(service, "review-gate")
    approve(service, doc, start=start, reviewed_by=reviewer)
    result = service.search("bob", "lawful import", country="ID")
    assert bool(result["items"]) is expected
    if expected:
        assert ids(result) == {doc["id"]}
        hit = result["items"][0]
        assert hit["source_scope"] == "system" and hit["document_version"] == 1
        assert hit["chunk_hash"] and hit["locator"]["chunk_id"] == "chunk-1"
    assert service.search("bob", "lawful import", country="SG")["items"] == []
    assert service.search("bob", "lawful import")["items"] == []


def test_private_published_unknown_date_behavior_is_unchanged(service):
    doc = original(service, "private-existing", owner="alice")
    approve(service, doc, start=None, reviewed_by="alice")
    assert ids(service.search("alice", "lawful import", country="ID")) == {doc["id"]}
    assert service.search("bob", "lawful import", country="ID")["items"] == []


def test_new_draft_version_excludes_older_default_source_but_keeps_history(service):
    old = original(service, "version", body="tea lawful original", version_key="family")
    approve(service, old, end="2022-12-31")
    new = original(service, "version", body="tea lawful amended", version_key="family")
    assert new["version"] == 2
    assert ids(service.archive.versions("bob", old["id"])) == {old["id"], new["id"]}
    assert service.search("bob", "lawful", country="ID")["items"] == []
    assert ids(service.search("bob", "lawful", country="ID", as_of=date(2021, 1, 1))) == {old["id"]}
    approve(service, new, start="2023-01-01")
    assert ids(service.search("bob", "lawful", country="ID")) == {new["id"]}


def test_foreign_private_relation_and_all_shared_writes_are_rejected(service):
    main = original(service, "main")
    child = original(service, "independent-secondary", level="secondary")
    payload = LegalRelationInput(target_document_id=child["id"], collection="country:ID", relation="cites")
    relation = service.archive.add_relation(SYSTEM_OWNER_ID, main["id"], payload.model_dump())
    assert service.archive.relations("bob", main["id"])["items"][0]["id"] == relation["id"]
    writes = [
        lambda: service.archive.patch_classification(
            "bob",
            main["id"],
            ClassificationPatch(
                collection="country:ID",
                level="secondary",
                tags=[],
                expected_revision=1,
            ).model_dump(),
        ),
        lambda: service.catalog.patch_document(
            "bob", main["id"], MetadataPatch(expected_revision=1, review_status="published")
        ),
        lambda: service.archive.add_relation("bob", main["id"], payload.model_dump()),
        lambda: service.store_document(
            "bob", "private-annex.txt", b"private", role="secondary", parent_id=main["id"]
        ),
    ]
    for write in writes:
        with pytest.raises(AppError) as error:
            write()
        assert error.value.status == 404


def test_forged_system_url_is_hidden_even_with_country_and_public_tags(service):
    doc = original(service, "tampered")
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET source_url='https://private.example/x' WHERE id=?", (doc["id"],))
    assert service.documents("bob", country="ID")["total"] == 0
    with pytest.raises(AppError):
        service.document_detail("bob", doc["id"])


def test_attachment_country_is_immutable_and_parent_scope_is_preserved(service):
    parent = original(service, "attachment-parent")
    attachment = original(service, "country-conflict", level="attachment", parent=parent["id"])
    with service.db.transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE document_countries SET country='SG' WHERE document_id=?", (attachment["id"],)
            )
    assert attachment["id"] in ids(service.documents("bob", country="ID"))
    assert attachment["id"] not in ids(service.documents("bob", country="SG"))
    assert service.document_detail("bob", attachment["id"])["document"]["country"] == "ID"


def test_shared_selection_and_snapshots_follow_case_country_and_review_changes(service):
    from app.defaults import DEFAULT_TEMPLATE

    case = service.create_case(
        "alice",
        {
            "title": "Shared legislation",
            "company": "",
            "product": "tea",
            "mode_id": next(mode["id"] for mode in DEFAULT_TEMPLATE["modes"] if mode["enabled"]),
            "template_id": DEFAULT_TEMPLATE["id"],
        },
    )
    id_doc = original(service, "snapshot-id")
    sg_doc = original(service, "snapshot-sg", country="SG")
    approve(service, id_doc)
    assert ids(service.documents("alice", case_id=case["id"])) == {id_doc["id"]}
    assert ids(service.search("alice", "lawful", case_id=case["id"])) == {id_doc["id"]}
    with closing(service.db.connect()) as conn:
        service.legal.validate_documents(conn, "alice", case["id"], [id_doc["id"]], {"ID"})
        with pytest.raises(AppError) as error:
            service.legal.validate_documents(conn, "alice", case["id"], [sg_doc["id"]], {"ID"})
        assert error.value.code == "country_mismatch"
        assert service.legal.valid_evidence(conn, "alice", case["id"], [id_doc["id"]]) == {id_doc["id"]}
    with service.db.transaction() as conn:
        before = service.legal.snapshot(conn, "alice", service.case_detail("alice", case["id"], conn))
    service.catalog.patch_document(
        SYSTEM_OWNER_ID,
        id_doc["id"],
        MetadataPatch(
            expected_revision=service.catalog.metadata_for(id_doc["id"])["metadata_revision"],
            review_status="draft",
        ),
    )
    with service.db.transaction() as conn:
        after = service.legal.snapshot(conn, "alice", service.case_detail("alice", case["id"], conn))
        assert service.legal.valid_evidence(conn, "alice", case["id"], [id_doc["id"]]) == set()
    assert before["environment"] != after["environment"]


def test_http_authenticated_reads_download_and_anonymous_boundary(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        service = client.app.state.service
        with service.db.transaction() as conn:
            ensure_principal(conn)
        doc = original(service, "download")
        paths = [
            f"/api/tea/documents/{doc['id']}",
            f"/api/tea/documents/{doc['id']}/download",
            f"/api/tea/documents/{doc['id']}/versions",
            f"/api/tea/documents/{doc['id']}/relations",
        ]
        for path in paths:
            assert client.get(path).status_code == 401
        assert client.post("/api/tea/auth/demo", json={}).status_code == 200
        for path in paths:
            assert client.get(path).status_code == 200
        assert client.get(paths[1]).content == b"tea lawful import clause download"
        assert client.get("/api/tea/documents?country=ID").json()["items"][0]["source_scope"] == "system"
        assert (
            client.patch(
                f"{paths[0]}/metadata", json={"expected_revision": 1, "review_status": "published"}
            ).status_code
            == 404
        )
        assert (
            client.patch(
                f"{paths[0]}/classification",
                json={"collection": "country:ID", "level": "primary", "tags": [], "expected_revision": 1},
            ).status_code
            == 404
        )
        token = "forged-system-http-session"
        with service.db.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions VALUES(?,?,?)",
                (
                    hashlib.sha256(token.encode()).hexdigest(),
                    SYSTEM_OWNER_ID,
                    (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
                ),
            )
        client.cookies.set(COOKIE, token)
        assert client.get("/api/tea/auth/me").status_code == 401
