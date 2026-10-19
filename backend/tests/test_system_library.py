"""Shared official originals do not weaken private or legal review boundaries."""

import hashlib
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from admin_helpers import login_admin
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
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=15").fetchone()
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


def test_personal_inventory_includes_all_own_countries_and_case_uploads(service, tmp_path):
    from app.defaults import DEFAULT_TEMPLATE

    def make_case(owner, title):
        return service.create_case(
            owner,
            {
                "title": title,
                "company": "",
                "product": "tea",
                "mode_id": next(mode["id"] for mode in DEFAULT_TEMPLATE["modes"] if mode["enabled"]),
                "template_id": DEFAULT_TEMPLATE["id"],
            },
        )

    first, second = make_case("alice", "first"), make_case("alice", "second")
    foreign_case = make_case("bob", "foreign")
    id_doc = original(service, "own-id", owner="alice")
    sg_doc = original(service, "own-sg", owner="alice", country="SG")
    loose = service.store_document("alice", "loose.txt", b"private unassigned")
    attached = service.store_document("alice", "case.txt", b"private case", case_id=first["id"])
    other_case = service.store_document("alice", "other.txt", b"differentcaseneedle42", case_id=second["id"])
    annex = service.store_document(
        "alice",
        "annex.bin",
        b"private annex",
        role="secondary",
        parent_id=attached["id"],
        case_id=first["id"],
    )
    original(service, "bob-private", owner="bob")
    service.store_document("bob", "foreign.txt", b"foreign case", case_id=foreign_case["id"])
    original(service, "system-shared")
    expected = {item["id"] for item in (id_doc, sg_doc, loose, attached, other_case, annex)}
    listed = service.documents("alice", source_scope="personal")
    assert ids(listed) == expected and listed["total"] == len(expected)
    pages = [service.documents("alice", source_scope="personal", limit=2, offset=i) for i in (0, 2, 4)]
    assert {item["id"] for page in pages for item in page["items"]} == expected
    assert all(page["total"] == len(expected) for page in pages)
    assert ids(service.documents("alice", source_scope="personal", country="ID")) == {id_doc["id"]}
    assert ids(service.documents("alice", source_scope="personal", country="SG")) == {sg_doc["id"]}
    assert ids(service.documents("alice", source_scope="personal", country="unassigned")) == {
        loose["id"],
        attached["id"],
        other_case["id"],
        annex["id"],
    }
    assert ids(service.documents("alice", source_scope="personal", case_id=first["id"])) == {
        attached["id"],
        annex["id"],
    }
    assert ids(service.documents("alice", source_scope="personal", parent_id=attached["id"])) == {annex["id"]}
    assert ids(service.documents("alice")) == {loose["id"]}  # Legacy library callers keep their scope.
    library_ids = {id_doc["id"], sg_doc["id"], loose["id"]}
    with TestClient(create_app(data_dir=tmp_path)) as client:
        client.cookies.set(COOKIE, client.app.state.auth.session("alice"))
        candidates = [
            client.get(
                "/api/tea/documents",
                params={
                    "source_scope": "personal",
                    "library_only": "true",
                    "limit": 1,
                    "offset": offset,
                },
            ).json()
            for offset in range(3)
        ]
        assert all(page["total"] == 3 and len(page["items"]) == 1 for page in candidates)
        assert {item["id"] for page in candidates for item in page["items"]} == library_ids
        assert (
            client.get(
                "/api/tea/documents",
                params={
                    "case_id": first["id"],
                    "library_only": "true",
                    "source_scope": "personal",
                },
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/tea/documents",
                params={
                    "parent_id": attached["id"],
                    "library_only": "true",
                    "source_scope": "personal",
                },
            ).status_code
            == 404
        )
    assert not service.search("alice", "differentcaseneedle42", case_id=first["id"], source_scope="personal")[
        "items"
    ]


@pytest.mark.parametrize("scope", [None, "personal", "system"])
def test_search_scope_filters_backfill_lexical_dense_and_rerank_before_candidates(
    service, monkeypatch, scope
):
    from app import ingestion, reranking

    docs = [
        original(service, key, owner=owner)
        for key, owner in (
            ("shared-candidate", SYSTEM_OWNER_ID),
            ("private-candidate", "alice"),
            ("foreign-candidate", "bob"),
        )
    ]
    for doc in docs:
        approve(service, doc)
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://embedding.example/v1")
    monkeypatch.setenv("EMBEDDING_MODEL", "scope-test")
    monkeypatch.setenv("EMBEDDING_API_KEY", "test-only")
    fingerprint = service.embedding_fingerprint()
    with service.db.transaction() as conn:
        for doc in docs:
            chunks = [
                dict(row)
                for row in conn.execute(
                    "SELECT rowid,input_hash FROM retrieval_chunks WHERE document_id=?", (doc["id"],)
                )
            ]
            service.index.store_vectors(conn, doc["id"], fingerprint, chunks, [[1.0, 0.0]])
            conn.execute("INSERT INTO retrieval_dirty VALUES(?)", (doc["id"],))
    allowed = {
        doc["id"]
        for doc in docs[:2]
        if scope is None or (doc["owner_id"] == SYSTEM_OWNER_ID) == (scope == "system")
    }
    seen = []
    lexical, dense = service.index._lexical, service.index._dense

    def checked_lexical(conn, query):
        seen.append({row[0] for row in conn.execute("SELECT id FROM retrieval_eligible")})
        return lexical(conn, query)

    def checked_dense(conn, model, vector):
        seen.append({row[0] for row in conn.execute("SELECT id FROM retrieval_eligible")})
        return dense(conn, model, vector)

    candidates = []
    gates = []
    eligibility_sql = service.archive.eligibility_sql

    def checked_gate(*args, **kwargs):
        gates.append(kwargs.get("source_scope"))
        return eligibility_sql(*args, **kwargs)

    monkeypatch.setattr(service.archive, "eligibility_sql", checked_gate)
    monkeypatch.setattr(service.index, "_lexical", checked_lexical)
    monkeypatch.setattr(service.index, "_dense", checked_dense)
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1.0, 0.0]])
    monkeypatch.setattr(reranking, "rerank_texts", lambda query, texts, **kw: candidates.extend(texts))
    result = service.search("alice", "tea lawful", country="ID", limit=10, source_scope=scope)
    assert ids(result) == allowed and result["mode"] == "hybrid"
    assert seen == [allowed, allowed]
    assert gates == [scope, scope, scope]  # Backfill, candidate snapshot, post-provider revalidation.
    assert len(candidates) == len(allowed) and not any("foreign-candidate" in text for text in candidates)
    with closing(service.db.connect()) as conn:
        assert {row[0] for row in conn.execute("SELECT document_id FROM retrieval_dirty")} == {
            doc["id"] for doc in docs if doc["id"] not in allowed
        }


def test_http_regular_users_share_system_reads_but_never_each_others_uploads(tmp_path):
    app = create_app(data_dir=tmp_path)
    with TestClient(app) as client, TestClient(app) as admin:
        login_admin(admin)

        def register_user(name):
            credentials = {"email": f"{name.lower()}@example.test", "password": "test-password-123"}
            registered = client.post("/api/tea/auth/register", json={"name": name, **credentials})
            assert registered.status_code == 201, registered.text
            account = registered.json()
            assert account["can_login"] is False and account["is_admin"] is False
            assert client.post("/api/tea/auth/login", json=credentials).status_code == 403
            access = admin.get("/api/tea/admin/users", params={"q": credentials["email"]}).json()["items"][0]
            approved = admin.patch(
                f"/api/tea/admin/users/{account['id']}",
                json={"expected_revision": access["revision"], "can_login": True},
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["is_admin"] is False
            logged_in = client.post("/api/tea/auth/login", json=credentials)
            assert logged_in.status_code == 200, logged_in.text
            assert logged_in.json()["is_admin"] is False
            return account["id"]

        service = client.app.state.service
        with service.db.transaction() as conn:
            ensure_principal(conn)
            conn.execute(
                "INSERT INTO users VALUES(?,?,?,?,?,?)", ("reviewer", "Reviewer", None, None, 0, now())
            )
        shared = original(service, "visible-every-user")
        secondary = original(service, "secondary-evidence", level="secondary")
        annex = original(service, "source-annex", level="attachment", parent=shared["id"])
        approve(service, shared)
        approve(service, secondary)
        alice = register_user("Alice")
        alice_cookies = dict(client.cookies)
        uploaded = client.post(
            "/api/tea/documents",
            data={"owner_id": SYSTEM_OWNER_ID, "source_scope": "system"},
            files={"file": ("own.txt", b"accountunique42 private upload", "text/plain")},
        )
        assert uploaded.status_code == 201, uploaded.text
        private = uploaded.json()
        client.cookies.clear()
        bob = register_user("Bob")
        assert bob != alice
        bob_cookies = dict(client.cookies)
        bob_doc = client.post(
            "/api/tea/documents", files={"file": ("bob.txt", b"bobneedle42 private upload", "text/plain")}
        ).json()
        public_ids = {shared["id"], secondary["id"], annex["id"]}
        for cookies, own, other in ((alice_cookies, private, bob_doc), (bob_cookies, bob_doc, private)):
            client.cookies.clear()
            client.cookies.update(cookies)
            assert ids(client.get("/api/tea/documents", params={"source_scope": "personal"}).json()) == {
                own["id"]
            }
            assert (
                ids(
                    client.get(
                        "/api/tea/documents", params={"source_scope": "system", "country": "ID"}
                    ).json()
                )
                == public_ids
            )
            for document in (shared, secondary, annex, own):
                for suffix in ("", "/download", "/versions", "/relations"):
                    response = client.get(f"/api/tea/documents/{document['id']}{suffix}")
                    assert response.status_code == 200, response.text
            for suffix in ("", "/download", "/versions", "/relations"):
                assert client.get(f"/api/tea/documents/{other['id']}{suffix}").status_code == 404
            assert client.get(f"/api/tea/documents/{other['content_hash']}/download").status_code == 404
            assert (
                client.get(
                    "/api/tea/documents", params={"source_scope": "personal", "q": other["title"]}
                ).json()["total"]
                == 0
            )
            assert ids(
                client.post(
                    "/api/tea/search", json={"query": "tea lawful", "country": "ID", "source_scope": "system"}
                ).json()
            ) == {shared["id"]}
            assert not client.post(
                "/api/tea/search", json={"query": "tea lawful", "country": "ID", "source_scope": "personal"}
            ).json()["items"]
            needle = "accountunique42" if own["id"] == private["id"] else "bobneedle42"
            other_needle = "bobneedle42" if own["id"] == private["id"] else "accountunique42"
            assert ids(
                client.post("/api/tea/search", json={"query": needle, "source_scope": "personal"}).json()
            ) == {own["id"]}
            assert not client.post(
                "/api/tea/search", json={"query": other_needle, "source_scope": "personal"}
            ).json()["items"]
            assert (
                client.post(
                    "/api/tea/documents",
                    data={"role": "secondary", "parent_id": shared["id"]},
                    files={"file": ("forbidden.txt", b"private", "text/plain")},
                ).status_code
                == 404
            )
            assert (
                client.patch(
                    f"/api/tea/documents/{other['id']}/metadata",
                    json={"expected_revision": 1, "review_status": "published"},
                ).status_code
                == 404
            )
        assert client.post("/api/tea/search", json={"query": "tea", "source_scope": "all"}).status_code == 422
        assert client.get("/api/tea/documents", params={"source_scope": "all"}).status_code == 422
        # A client cannot choose the privileged corpus owner when uploading.
        assert private["source_scope"] == bob_doc["source_scope"] == "personal"
        with closing(service.db.connect()) as conn:
            assert (
                conn.execute("SELECT owner_id FROM documents WHERE id=?", (private["id"],)).fetchone()[0]
                == alice
            )
        client.cookies.clear()
        assert client.get(f"/api/tea/documents/{shared['id']}/download").status_code == 401
