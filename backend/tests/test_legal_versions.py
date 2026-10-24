"""Version selection uses approved applicability dates and explicit collection membership."""

from contextlib import closing

import pytest
from app.interfaces.http.factory import create_app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("ragflow_library")


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        client.post("/api/tea/auth/demo", json={})
        yield client


def law(client, text, start, end=None, *, approved=True):
    doc = client.post("/api/tea/documents", files={"file": ("Act.txt", text.encode(), "text/plain")}).json()
    result = client.patch(
        f"/api/tea/documents/{doc['id']}/metadata",
        json={
            "expected_revision": 0,
            "kind": "law",
            "review_status": "published" if approved else "draft",
            "effective_from": start,
            "effective_to": end,
        },
    )
    assert result.status_code == 200, result.text
    return doc


def hits(client, **kwargs):
    return client.post("/api/tea/search", json={"query": "tea", **kwargs}).json()["items"]


def test_historical_law_is_retrievable_without_replacing_current(client):
    old = law(client, "tea old clause", "2020-01-01", "2021-12-31")
    new = law(client, "tea new clause", "2022-01-01")
    assert {hit["document_id"] for hit in hits(client, as_of="2021-01-01")} == {old["id"]}
    assert {hit["document_id"] for hit in hits(client)} == {new["id"]}
    assert {hit["document_version"] for hit in hits(client, as_of="2021-01-01")} == {1}


def test_historical_query_does_not_resurrect_superseded_open_ended_law(client):
    law(client, "tea uncertain old clause", "2020-01-01")
    law(client, "tea unreviewed new clause", "2022-01-01", approved=False)
    assert hits(client) == []
    assert hits(client, as_of="2024-01-01") == []


def test_latest_secondary_does_not_expose_older_primary_in_default_search(client):
    law(client, "tea old primary", "2020-01-01")
    new = law(client, "tea new secondary", "2022-01-01")
    response = client.patch(
        f"/api/tea/documents/{new['id']}/classification",
        json={
            "collection": "ID",
            "level": "secondary",
            "tags": [],
            "expected_revision": 0,
        },
    )
    assert response.status_code == 200
    assert hits(client) == []


def test_same_law_can_be_primary_in_one_collection_and_secondary_in_another(client):
    doc = law(client, "tea shared law", "2020-01-01")
    for collection, level in (("ID", "primary"), ("SG", "secondary")):
        assert (
            client.patch(
                f"/api/tea/documents/{doc['id']}/classification",
                json={
                    "collection": collection,
                    "level": level,
                    "tags": [f"country:{collection}"],
                    "expected_revision": 0,
                },
            ).status_code
            == 200
        )
    assert hits(client, collection="ID")
    assert hits(client, collection="SG") == []
    assert hits(client, collection="missing") == []


def test_version_list_uses_bounded_queries_and_does_not_load_manifests(client, monkeypatch):
    doc = law(client, "tea old version", "2020-01-01")
    service = client.app.state.service
    statements = []
    original = service.db.connect

    def connect():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(service.db, "connect", connect)
    listed = client.get("/api/tea/documents?legal_level=primary&limit=1").json()
    assert listed["items"][0]["id"] == doc["id"]
    assert "manifest" not in listed["items"][0]
    assert not any("SELECT * FROM documents WHERE owner_id=" in stmt for stmt in statements)
    with closing(original()) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 5


def test_upload_reversion_is_new_version_with_shared_original(client):
    first = law(client, "tea text A", "2020-01-01")
    law(client, "tea text B", "2022-01-01")
    latest = law(client, "tea text A", "2024-01-01")
    assert latest["id"] != first["id"] and latest["version"] == 3
    assert {hit["document_id"] for hit in hits(client)} == {latest["id"]}
