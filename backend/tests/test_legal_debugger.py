"""The existing debugger uses the same six-stage legal runtime when opted in."""

import pytest
from admin_helpers import login_admin
from app.interfaces.http.factory import create_app
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("ragflow_library")


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as value:
        login_admin(value)
        yield value


def request(client, **changes):
    template = client.get("/api/tea/templates").json()["items"][0]
    template["agent"]["legal"] = {"enabled": True, "jurisdictions": ["SG"], "rules": []}
    return {
        "template": template,
        "stage_id": template["stages"][0]["id"],
        "input": "nationaltea 是否需要办理",
        "document_ids": [],
        "expectations": [],
        **changes,
    }


def document(client, country, *, case_id=None):
    created = client.post(
        "/api/tea/documents",
        data={"case_id": case_id} if case_id else {},
        files={"file": (f"{country}.txt", b"nationaltea actual rule", "text/plain")},
    )
    assert created.status_code == 201, created.text
    doc = created.json()
    classified = client.patch(
        f"/api/tea/documents/{doc['id']}/classification",
        json={"collection": f"country:{country}", "level": "primary", "tags": [], "expected_revision": 0},
    )
    assert classified.status_code == 200, classified.text
    published = client.patch(
        f"/api/tea/documents/{doc['id']}/metadata",
        json={
            "expected_revision": classified.json()["metadata_revision"],
            "review_status": "published",
            "effective_from": "2020-01-01",
        },
    )
    assert published.status_code == 200, published.text
    return doc


def test_legal_debugger_uses_canonical_stages_and_evaluates_final_report(client):
    payload = request(client, expectations=[{"id": "honest", "kind": "contains", "text": "未调用大模型"}])
    response = client.post("/api/tea/templates/debug", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed", result
    assert [item["id"] for item in result["trace"]] == [f"qa0{i}" for i in range(1, 7)]
    assert result["evaluations"][0]["status"] == "passed"
    assert result["issues"][0]["status"] == "undetermined"
    invalid = client.post("/api/tea/templates/debug", json={**payload, "stop_after_step_id": "retrieve"})
    assert invalid.status_code == 422


def test_legal_debugger_pauses_and_resumes_checked_legal_state(client):
    payload = request(client, stop_after_step_id="qa03")
    first = client.post("/api/tea/templates/debug", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "paused", first.json()
    resumed = client.post(
        "/api/tea/templates/debug",
        json={
            **payload,
            "source_run_id": first.json()["id"],
            "start_step_id": "qa04",
            "stop_after_step_id": "qa06",
        },
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "completed", resumed.json()
    assert [item["status"] for item in resumed.json()["trace"]][:3] == ["cached"] * 3
    payload["template"]["agent"]["legal"]["question_limit"] = 4
    stale = client.post(
        "/api/tea/templates/debug",
        json={
            **payload,
            "source_run_id": first.json()["id"],
            "start_step_id": "qa04",
            "stop_after_step_id": "qa06",
        },
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_checkpoint"


@pytest.mark.parametrize("mode", ["unavailable", "ragflow"])
@pytest.mark.parametrize("stop_after_step_id", ["qa01", "qa06"])
def test_legal_debugger_reports_actual_retrieval_mode(client, monkeypatch, mode, stop_after_step_id):
    monkeypatch.setattr(
        client.app.state.service.legal,
        "search",
        lambda *args, **kwargs: {"items": [], "mode": mode, "warning": None},
    )
    response = client.post(
        "/api/tea/templates/debug", json=request(client, stop_after_step_id=stop_after_step_id)
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == ("paused" if stop_after_step_id == "qa01" else "completed")
    assert result["retrieval_mode"] == mode


def test_legal_debugger_scope_includes_allowed_countries_and_excludes_private_other_cases(client):
    documents = [document(client, country) for country in ("CN", "ID", "SG")]
    forbidden = document(client, "US")
    case_id = client.get("/api/tea/cases").json()["items"][0]["id"]
    private = document(client, "ID", case_id=case_id)
    payload = request(client, document_ids=[item["id"] for item in documents])
    result = client.post("/api/tea/templates/debug", json=payload)
    assert result.status_code == 200, result.text
    assert result.json()["status"] == "completed", result.json()
    assert {item["document_id"] for item in result.json()["citations"]} == {item["id"] for item in documents}
    for doc, status in ((forbidden, 422), (private, 404)):
        denied = client.post("/api/tea/templates/debug", json={**payload, "document_ids": [doc["id"]]})
        assert denied.status_code == status, denied.text
    all_scoped = client.post("/api/tea/templates/debug", json={**payload, "document_ids": None})
    assert all_scoped.status_code == 200, all_scoped.text
    assert set(all_scoped.json()["request"]["document_ids"]) == {item["id"] for item in documents}


def test_saved_legal_sample_and_checkpoint_reject_changed_country_metadata(client):
    doc = document(client, "SG")
    payload = request(client, document_ids=[doc["id"]], stop_after_step_id="qa04")
    sample_url = f"/api/tea/templates/{payload['template']['id']}/debug-samples/legal-case"
    saved_payload = {key: value for key, value in payload.items() if key != "stop_after_step_id"}
    saved = client.put(sample_url, json={**saved_payload, "name": "法律样例", "expected_revision": 0})
    assert saved.status_code == 200, saved.text
    first = client.post("/api/tea/templates/debug", json=payload)
    assert first.status_code == 200, first.text
    with client.app.state.service.db.transaction() as conn:
        conn.execute("UPDATE document_metadata SET revision=revision+1 WHERE document_id=?", (doc["id"],))
    for options in (
        {"sample_id": "legal-case", "sample_revision": 1},
        {"source_run_id": first.json()["id"], "start_step_id": "qa05", "stop_after_step_id": "qa06"},
    ):
        rejected = client.post("/api/tea/templates/debug", json={**payload, **options})
        assert rejected.status_code == 409, rejected.text
