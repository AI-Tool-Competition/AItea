"""Country-aware debug inputs and migration compatibility with collection main."""

from contextlib import closing

import pytest
from admin_helpers import grant_admin, login_admin
from app.bootstrap import Service
from app.interfaces.http.factory import create_app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("ragflow_library")


def test_empty_database_has_distinct_country_and_debugger_migrations(tmp_path):
    service = Service(tmp_path)
    with closing(service.db.connect()) as conn:
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"debug_samples", "debug_runs", "document_countries", "checklist_bindings"} <= tables
        assert (
            conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version IN (7,8)").fetchone()[0] == 2
        )


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        login_admin(client)
        yield client


def payload(client, destination="印度尼西亚"):
    template = client.get("/api/tea/templates").json()["items"][0]
    template["destination"] = destination
    template["agent"]["steps"] = [
        {"id": kind, "type": kind, "label": kind, "enabled": True, "instructions": ""}
        for kind in ("retrieve", "check", "guide")
    ]
    return {
        "template": template,
        "stage_id": template["stages"][0]["id"],
        "input": "nationaltea",
        "document_ids": None,
    }


def document(client, country=None, *, name="rule.txt", level="primary", case_id=None):
    response = client.post(
        "/api/tea/documents",
        data={"case_id": case_id} if case_id else {},
        files={"file": (name, b"nationaltea actual source", "text/plain")},
    )
    assert response.status_code == 201, response.text
    doc = response.json()
    if country:
        changed = client.patch(
            f"/api/tea/documents/{doc['id']}/classification",
            json={
                "collection": f"country:{country}",
                "level": level,
                "tags": [],
                "expected_revision": 0,
            },
        )
        assert changed.status_code == 200, changed.text
        approved = client.patch(
            f"/api/tea/documents/{doc['id']}/metadata",
            json={
                "expected_revision": changed.json()["metadata_revision"],
                "review_status": "published",
                "effective_from": "2020-01-01",
            },
        )
        assert approved.status_code == 200, approved.text
    return doc


def test_debug_candidates_and_execution_use_template_country_and_isolate_scope(client):
    indonesian = document(client, "ID", name="indonesia.txt")
    singaporean = document(client, "SG", name="singapore.txt")
    unassigned = document(client, name="unassigned.txt")
    case = client.get("/api/tea/cases").json()["items"][0]
    private = document(client, "ID", name="case-private.txt", case_id=case["id"])
    candidates = client.get(
        "/api/tea/templates/debug/documents", params={"destination": "Indonesia", "limit": 1}
    )
    assert candidates.status_code == 200, candidates.text
    assert candidates.json()["country"] == "ID"
    assert candidates.json()["total"] == 1
    assert [row["id"] for row in candidates.json()["items"]] == [indonesian["id"]]
    assert client.get("/api/tea/templates/debug/documents?destination=ID&limit=201").status_code == 422
    request = payload(client)
    result = client.post("/api/tea/templates/debug", json=request)
    assert result.status_code == 200, result.text
    assert result.json()["country"] == "ID"
    assert result.json()["request"]["document_ids"] == [indonesian["id"]]
    assert {citation["document_id"] for citation in result.json()["citations"]} == {indonesian["id"]}
    assert result.json()["trace"][0]["input_data"]["country"] == "ID"
    for invalid in (singaporean, unassigned):
        denied = client.post("/api/tea/templates/debug", json=request | {"document_ids": [invalid["id"]]})
        assert denied.status_code == 422 and denied.json()["error"]["code"] == "country_mismatch"
    assert (
        client.post("/api/tea/templates/debug", json=request | {"document_ids": [private["id"]]}).status_code
        == 404
    )
    unresolved = payload(client, destination="待确认目的地")
    output = client.post("/api/tea/templates/debug", json=unresolved).json()
    assert output["country"] is None
    assert {citation["document_id"] for citation in output["citations"]} == {unassigned["id"]}
    client.post("/api/tea/auth/logout")
    other = client.post("/api/tea/auth/demo", json={}).json()
    assert client.get("/api/tea/templates/debug/documents?destination=ID").status_code == 403
    grant_admin(client, other["id"])
    assert client.get("/api/tea/templates/debug/documents?destination=ID").json()["total"] == 0


def test_country_and_classification_changes_invalidate_fixed_samples_and_checkpoints(client):
    indonesian = document(client, "ID")
    request = payload(client)
    request["document_ids"] = [indonesian["id"]]
    sample_url = f"/api/tea/templates/{request['template']['id']}/debug-samples/national"
    saved = client.put(sample_url, json=request | {"name": "印尼规则", "expected_revision": 0})
    assert saved.status_code == 200, saved.text
    assert saved.json()["country"] == "ID"
    first = client.post("/api/tea/templates/debug", json=request).json()
    classified = client.patch(
        f"/api/tea/documents/{indonesian['id']}/classification",
        json={
            "collection": "country:ID",
            "level": "secondary",
            "tags": [],
            "expected_revision": 1,
        },
    )
    assert classified.status_code == 200, classified.text
    for options in (
        {"source_run_id": first["id"], "start_step_id": "guide"},
        {"sample_id": "national", "sample_revision": 1},
    ):
        stale = client.post("/api/tea/templates/debug", json=request | options)
        assert stale.status_code == 409, stale.text
    fresh = client.post("/api/tea/templates/debug", json=request).json()
    assert fresh["citations"] == []
    request["template"]["destination"] = "Singapore"
    changed = client.post("/api/tea/templates/debug", json=request)
    assert changed.status_code == 422


def test_main_country_v7_database_adds_debug_v8_without_changing_country_or_case_data(client, tmp_path):
    doc = document(client, "ID")
    service = client.app.state.service
    with service.db.transaction() as conn:
        before = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone())
        country = dict(
            conn.execute("SELECT * FROM document_countries WHERE document_id=?", (doc["id"],)).fetchone()
        )
        conn.execute("DROP TABLE debug_runs")
        conn.execute("DROP TABLE debug_samples")
        conn.execute("DELETE FROM schema_migrations WHERE version=8")
    restarted = Service(tmp_path)
    with closing(restarted.db.connect()) as conn:
        assert dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()) == before
        assert (
            dict(
                conn.execute("SELECT * FROM document_countries WHERE document_id=?", (doc["id"],)).fetchone()
            )
            == country
        )
        assert conn.execute("SELECT COUNT(*) FROM debug_samples").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] > 0
        assert (
            conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version IN (7,8)").fetchone()[0] == 2
        )
    Service(tmp_path)


def test_unpublished_debug_v7_upgrades_both_schemas_without_losing_samples_runs_or_originals(
    client, tmp_path
):
    request = payload(client, "待确认目的地")
    doc = document(client)
    request["document_ids"] = [doc["id"]]
    url = f"/api/tea/templates/{request['template']['id']}/debug-samples/preserved"
    saved = client.put(url, json=request | {"name": "保留样例", "expected_revision": 0}).json()
    run = client.post("/api/tea/templates/debug", json=request).json()
    service = client.app.state.service
    owner = client.get("/api/tea/auth/me").json()["id"]
    with service.db.transaction() as conn:
        original = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone())
        for table in (
            "checklist_pages",
            "checklist_frontier",
            "checklist_candidates",
            "checklist_bindings",
            "document_countries",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("ALTER TABLE sources DROP COLUMN checklist")
        for column in ("purpose", "depth", "candidate_id"):
            conn.execute(f"ALTER TABLE collection_items DROP COLUMN {column}")
        # Simulate the unpublished branch whose debugger owned migration 7.
        conn.execute("DELETE FROM schema_migrations WHERE version=8")
    restarted = Service(tmp_path)
    assert restarted.studio.debugger.sample(owner, request["template"]["id"], "preserved") == saved
    assert restarted.studio.debugger.run(owner, run["id"]) == run
    with closing(restarted.db.connect()) as conn:
        assert dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()) == original
        assert conn.execute("SELECT COUNT(*) FROM document_countries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM checklist_bindings").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version IN (7,8)").fetchone()[0] == 2
        )
    Service(tmp_path)


def test_first_country_assignment_invalidates_unassigned_checkpoint(client):
    doc = document(client)
    request = payload(client, "待确认目的地") | {"document_ids": [doc["id"]]}
    first = client.post("/api/tea/templates/debug", json=request).json()
    result = client.patch(
        f"/api/tea/documents/{doc['id']}/classification",
        json={
            "collection": "country:ID",
            "level": "primary",
            "tags": [],
            "expected_revision": 0,
        },
    )
    assert result.status_code == 200
    stale = client.post(
        "/api/tea/templates/debug", json=request | {"source_run_id": first["id"], "start_step_id": "guide"}
    )
    assert stale.status_code == 422 and stale.json()["error"]["code"] == "country_mismatch"


def test_proactive_analysis_cache_is_invalidated_when_national_primary_becomes_secondary(client):
    doc = document(client, "ID")
    case = client.get("/api/tea/cases").json()["items"][0]
    detail = client.get("/api/tea/cases/" + case["id"]).json()
    stage = detail["stages"][0]
    route = f"/api/tea/cases/{case['id']}/stages/{stage['id']}/analysis"
    initial = client.post(route, json={"expected_revision": detail["revision"]})
    assert initial.status_code == 200, initial.text
    result = client.patch(
        f"/api/tea/documents/{doc['id']}/classification",
        json={
            "collection": "country:ID",
            "level": "secondary",
            "tags": [],
            "expected_revision": 1,
        },
    )
    assert result.status_code == 200
    changed = client.post(route, json={"expected_revision": detail["revision"]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["id"] != initial.json()["id"]


def test_empty_document_scope_still_invalidates_country_sample_and_checkpoint(client):
    request = payload(client) | {"document_ids": []}
    sample_url = f"/api/tea/templates/{request['template']['id']}/debug-samples/empty-country"
    saved = client.put(sample_url, json=request | {"name": "空资料国家样例", "expected_revision": 0})
    assert saved.status_code == 200, saved.text
    first = client.post("/api/tea/templates/debug", json=request).json()
    request["template"]["destination"] = "Singapore"
    sample = client.post(
        "/api/tea/templates/debug", json=request | {"sample_id": "empty-country", "sample_revision": 1}
    )
    assert sample.status_code == 409 and sample.json()["error"]["code"] == "stale_sample"
    checkpoint = client.post(
        "/api/tea/templates/debug", json=request | {"source_run_id": first["id"], "start_step_id": "guide"}
    )
    assert checkpoint.status_code == 409 and checkpoint.json()["error"]["code"] == "stale_checkpoint"
    full = client.post("/api/tea/templates/debug", json=request)
    assert full.status_code == 200 and full.json()["country"] == "SG"
