"""Node entry uses the editable six-step analysis, scoped to that node."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from app.interfaces.http.factory import create_app
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with TestClient(create_app(data_dir=tmp_path)) as client:
        client.post("/api/tea/auth/demo", json={})
        yield client


def entry(client):
    case_id = client.get("/api/tea/cases").json()["items"][0]["id"]
    case = client.get(f"/api/tea/cases/{case_id}").json()
    route = f"/api/tea/cases/{case_id}/stages/{case['stages'][0]['id']}/analysis"
    return case, route


def test_node_entry_streams_six_steps_and_persists_editable_result(client):
    case, route = entry(client)
    response = client.post(route + "/stream", json={"expected_revision": case["revision"]})
    assert response.status_code == 200, response.text
    frames = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    steps = [frame for frame in frames if "id" in frame and frame["id"].startswith("qa")]
    assert {step["id"] for step in steps} == {f"qa0{i}" for i in range(1, 7)}
    result = frames[-1]["analysis"]
    saved = client.get(f"/api/tea/cases/{case['id']}/legal/analyses/{result['legal_analysis_id']}").json()
    assert saved["stage_id"] == case["stages"][0]["id"]
    assert saved["sections"]
    assert result["legal_revision"] == saved["revision"]
    assert client.post(route, json={"expected_revision": case["revision"]}).json() == result
    current = client.get(f"/api/tea/cases/{case['id']}").json()
    assert current["revision"] == case["revision"]
    assert current["stages"] == case["stages"]


def test_material_change_refreshes_same_node_report_and_preserves_manual_edit(client):
    case, route = entry(client)
    first = client.post(route, json={"expected_revision": case["revision"]}).json()
    detail_url = f"/api/tea/cases/{case['id']}/legal/analyses/{first['legal_analysis_id']}"
    saved = client.get(detail_url).json()
    section = saved["sections"][0]
    edited = client.patch(
        detail_url + f"/sections/{section['id']}",
        json={
            "expected_revision": saved["revision"],
            "content": "人工核对后的办理说明",
        },
    )
    assert edited.status_code == 200, edited.text
    client.post(
        "/api/tea/documents", files={"file": ("补充材料.md", "新的资料准备要求".encode(), "text/markdown")}
    )
    second = client.post(route, json={"expected_revision": case["revision"]}).json()
    assert second["id"] != first["id"]
    assert second["legal_analysis_id"] == first["legal_analysis_id"]
    refreshed = client.get(detail_url).json()
    assert any(s["content"] == "人工核对后的办理说明" and s["edited"] for s in refreshed["sections"])
    assert client.post(route, json={"expected_revision": case["revision"]}).json() == second


def test_cancelled_entry_releases_claim_and_resumes_same_report(client, monkeypatch):
    from app.contexts.workflows.application.contracts import StageAnalysisInput
    from app.shared.application.streaming import StreamCancelled

    case, route = entry(client)
    service = client.app.state.service
    owner = client.get("/api/tea/auth/me").json()["id"]
    engine = service.legal.execute_engine
    calls = []

    def interrupt(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise StreamCancelled()
        return engine(*args, **kwargs)

    monkeypatch.setattr(service.legal, "execute_engine", interrupt)
    with pytest.raises(StreamCancelled):
        service.studio.analyze(
            owner, case["id"], case["stages"][0]["id"], StageAnalysisInput(expected_revision=case["revision"])
        )
    history_url = f"/api/tea/cases/{case['id']}/legal"
    cancelled = client.get(history_url).json()["analyses"][0]
    assert cancelled["status"] == "cancelled"
    resumed = client.post(route, json={"expected_revision": case["revision"]})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["legal_analysis_id"] == cancelled["id"]
    assert len(client.get(history_url).json()["analyses"]) == 1


def test_followup_chat_receives_current_node_report_with_manual_edits(client, monkeypatch):
    case, route = entry(client)
    first = client.post(route, json={"expected_revision": case["revision"]}).json()
    url = f"/api/tea/cases/{case['id']}/legal/analyses/{first['legal_analysis_id']}"
    report = client.get(url).json()
    client.patch(
        url + f"/sections/{report['sections'][0]['id']}",
        json={
            "expected_revision": report["revision"],
            "content": "人工保留的付款建议",
        },
    )
    service = client.app.state.service
    runner = service.run_workflow
    contexts = []

    def capture(*args, **kwargs):
        contexts.append(kwargs["context"])
        return runner(*args, **kwargs)

    monkeypatch.setattr(service, "run_workflow", capture)
    response = client.post(
        f"/api/tea/cases/{case['id']}/messages",
        json={
            "stage_id": case["stages"][0]["id"],
            "content": "接下来怎么做？",
            "client_message_id": "node-followup",
        },
    )
    assert response.status_code == 200, response.text
    node = contexts[0]["node_analysis"]
    assert node["id"] == report["id"]
    assert any(section["content"] == "人工保留的付款建议" for section in node["sections"])
    current = client.get(f"/api/tea/cases/{case['id']}").json()
    assert client.post(route, json={"expected_revision": current["revision"]}).json()["id"] == first["id"]


def test_entry_retry_recovers_a_report_failed_by_inline_refresh(client, monkeypatch):
    case, route = entry(client)
    first = client.post(route, json={"expected_revision": case["revision"]}).json()
    url = f"/api/tea/cases/{case['id']}/legal/analyses/{first['legal_analysis_id']}"
    report = client.get(url).json()
    service = client.app.state.service
    engine = service.legal.execute_engine

    def fail_refresh(*args, **kwargs):
        raise TimeoutError("provider interrupted")

    monkeypatch.setattr(service.legal, "execute_engine", fail_refresh)
    assert (
        client.post(
            url + "/refresh",
            json={
                "expected_revision": report["revision"],
                "client_request_id": "failed-inline-refresh",
            },
        ).status_code
        == 502
    )
    assert client.post(route, json={"expected_revision": case["revision"]}).json()["status"] == "failed"
    monkeypatch.setattr(service.legal, "execute_engine", engine)
    recovered = client.post(route, json={"expected_revision": case["revision"], "retry": True}).json()
    assert recovered["status"] == "completed"
    assert recovered["legal_analysis_id"] == report["id"]


def test_entry_waits_for_inline_refresh_without_discarding_cache_or_running_twice(client, monkeypatch):
    case, route = entry(client)
    first = client.post(route, json={"expected_revision": case["revision"]}).json()
    url = f"/api/tea/cases/{case['id']}/legal/analyses/{first['legal_analysis_id']}"
    report = client.get(url).json()
    service = client.app.state.service
    engine = service.legal.execute_engine
    entered, release = threading.Event(), threading.Event()
    calls = []

    def hold_refresh(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return engine(*args, **kwargs)

    monkeypatch.setattr(service.legal, "execute_engine", hold_refresh)
    with ThreadPoolExecutor(max_workers=1) as pool:
        refreshing = pool.submit(
            client.post,
            url + "/refresh",
            json={"expected_revision": report["revision"], "client_request_id": "inline-refresh"},
        )
        assert entered.wait(5)
        try:
            waiting = client.post(route, json={"expected_revision": case["revision"]})
            assert waiting.status_code == 409, waiting.text
            assert waiting.json()["error"]["code"] == "analysis_in_progress"
        finally:
            release.set()
        refreshed = refreshing.result(timeout=5)
    assert refreshed.status_code == 200, refreshed.text
    resumed = client.post(route, json={"expected_revision": case["revision"]})
    assert resumed.status_code == 200, resumed.text
    assert len(calls) == 1
    assert resumed.json()["id"] == first["id"]
    assert resumed.json()["legal_revision"] == refreshed.json()["revision"]


def test_entry_retry_expires_an_abandoned_inline_refresh_before_reading_its_revision(client):
    case, route = entry(client)
    first = client.post(route, json={"expected_revision": case["revision"]}).json()
    url = f"/api/tea/cases/{case['id']}/legal/analyses/{first['legal_analysis_id']}"
    report = client.get(url).json()
    owner = client.get("/api/tea/auth/me").json()["id"]
    service = client.app.state.service
    service.legal.reserve(
        owner,
        case["id"],
        {"expected_revision": report["revision"], "client_request_id": "abandoned-inline"},
        report["id"],
    )
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE legal_requests SET expires_at=? WHERE case_id=? AND client_id=?",
            ("2020-01-01T00:00:00+00:00", case["id"], "abandoned-inline"),
        )
    resumed = client.post(route, json={"expected_revision": case["revision"], "retry": True})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["legal_analysis_id"] == report["id"]
