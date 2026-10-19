"""Debugger executes the real runner against versioned, isolated test inputs."""

import copy
import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import TemplateDebugInput
from app.service import Service
from app.streaming import StreamCancelled

from admin_helpers import grant_admin, login_admin


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        login_admin(client)
        yield client


def request(client, **changes):
    template = client.get("/api/tea/templates").json()["items"][0]
    # Generic runner tests use unassigned sample material; country boundaries
    # are covered separately against real national classifications.
    template["destination"] = "待确认目的地"
    template["agent"]["steps"] = [
        {"id": kind, "type": kind, "label": kind, "enabled": True, "instructions": ""}
        for kind in ("retrieve", "check", "guide")
    ]
    return {
        "template": template,
        "stage_id": template["stages"][0]["id"],
        "input": "缺少什么资料",
        "task_values": {},
        "document_ids": [],
        "expectations": [],
        **changes,
    }


def sample(client, payload=None, name="缺少资料"):
    payload = payload or request(client)
    url = f"/api/tea/templates/{payload['template']['id']}/debug-samples/sample-a"
    body = {
        k: v
        for k, v in payload.items()
        if k in ("template", "stage_id", "input", "task_values", "document_ids", "expectations")
    }
    response = client.put(url, json={**body, "name": name, "expected_revision": 0})
    assert response.status_code == 200, response.text
    return url, response.json()


def test_sample_persists_revision_snapshot_and_owner_isolation(client, tmp_path):
    payload = request(client)
    url, saved = sample(client, payload)
    assert saved["revision"] == 1
    assert saved["template"] == payload["template"]
    assert saved["document_versions"] == []
    with TestClient(create_app(data_dir=tmp_path)) as reopened:
        reopened.cookies.update(client.cookies)
        assert reopened.get(url).json() == saved
    conflict = client.put(url, json={**payload, "name": "覆盖", "expected_revision": 0})
    assert conflict.status_code == 409
    listing = client.get(url.rsplit("/", 1)[0] + "?limit=1&offset=0").json()
    assert listing["total"] == 1 and "template" not in listing["items"][0]
    assert client.delete(url + "?expected_revision=2").status_code == 409
    client.post("/api/tea/auth/logout")
    other = client.post("/api/tea/auth/demo", json={}).json()
    assert client.get(url).status_code == 403
    assert client.get(url.rsplit("/", 1)[0]).status_code == 403
    assert client.delete(url + "?expected_revision=1").status_code == 403
    grant_admin(client, other["id"])
    assert client.get(url).status_code == 404
    assert client.get(url.rsplit("/", 1)[0]).json()["total"] == 0
    assert client.delete(url + "?expected_revision=1").status_code == 404


def test_debug_trace_has_actual_io_timing_expectations_and_does_not_mutate_case(client):
    payload = request(client, client_id="trace-run")
    task = payload["template"]["stages"][0]["tasks"][0]
    field = task["fields"][0]
    payload["expectations"] = [
        {
            "id": "missing",
            "kind": "check",
            "task_id": task["id"],
            "check_id": field["key"],
            "status": "missing",
        },
        {"id": "truthful", "kind": "not_contains", "text": "手续已办理"},
        {"id": "review", "kind": "manual", "text": "解释是否清楚"},
    ]
    case_id = client.get("/api/tea/cases").json()["items"][0]["id"]
    before = client.get(f"/api/tea/cases/{case_id}").json()
    response = client.post("/api/tea/templates/debug", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "completed"
    assert [item["status"] for item in result["evaluations"]] == ["passed", "passed", "manual"]
    assert result["trace"][0]["input_data"]["query"] == payload["input"]
    assert result["trace"][1]["input_data"]["tasks"][0]["fields"] == task["fields"]
    assert result["trace"][1]["output_data"]["checks"] == result["checks"]
    assert result["trace"][2]["output_data"]["content"] == result["output"]
    assert all(item["duration_ms"] >= 0 for item in result["trace"])
    assert client.get(f"/api/tea/cases/{case_id}").json() == before
    assert client.get("/api/tea/templates/debug/runs/" + result["id"]).json() == result
    assert client.post("/api/tea/templates/debug", json=payload).json() == result
    payload["input"] = "different"
    assert client.post("/api/tea/templates/debug", json=payload).status_code == 409


def test_single_step_uses_server_checkpoint_and_rejects_changed_prefix(client, monkeypatch):
    payload = request(client, stop_after_step_id="check")
    service = client.app.state.service
    calls = []
    original = service.search
    monkeypatch.setattr(service, "search", lambda *a, **k: (calls.append(1), original(*a, **k))[1])
    first = client.post("/api/tea/templates/debug", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "paused"
    source_id = first.json()["id"]
    payload.update(source_run_id=source_id, start_step_id="guide", stop_after_step_id="guide")
    payload["template"]["agent"]["steps"][2]["instructions"] = "简短说明"
    result = client.post("/api/tea/templates/debug", json=payload)
    assert result.status_code == 200, result.text
    assert [item["status"] for item in result.json()["trace"]] == ["cached", "cached", "completed"]
    assert calls == [1]
    payload["template"]["agent"]["steps"][0]["instructions"] = "变更检索"
    stale = client.post("/api/tea/templates/debug", json=payload)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_checkpoint"
    payload["template"]["agent"]["steps"][0]["instructions"] = ""
    payload["input"] = "变更输入"
    assert client.post("/api/tea/templates/debug", json=payload).status_code == 409


def test_saved_sample_rejects_changed_inputs_revision_or_document_metadata(client):
    doc = client.post(
        "/api/tea/documents", files={"file": ("sample.txt", b"tea sample material", "text/plain")}
    ).json()
    payload = request(client, document_ids=[doc["id"]])
    url, saved = sample(client, payload)
    payload.update(sample_id=saved["id"], sample_revision=1)
    payload["template"]["agent"]["steps"][2]["instructions"] = "允许调整指导"
    assert client.post("/api/tea/templates/debug", json=payload).status_code == 200
    changed = copy.deepcopy(payload)
    changed["input"] = "篡改固定输入"
    assert client.post("/api/tea/templates/debug", json=changed).status_code == 409
    changed = copy.deepcopy(payload)
    changed["sample_revision"] = 0
    assert client.post("/api/tea/templates/debug", json=changed).status_code == 422
    with client.app.state.service.db.transaction() as conn:
        conn.execute("UPDATE documents SET version=version+1 WHERE id=?", (doc["id"],))
    stale = client.post("/api/tea/templates/debug", json=payload)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_sample"
    assert client.get(url).json()["document_versions"][0]["version"] == doc["version"]


def test_selected_documents_control_actual_retrieval_and_are_owner_scoped(client):
    doc = client.post(
        "/api/tea/documents", files={"file": ("tea.txt", b"uniqueteamaterial", "text/plain")}
    ).json()
    payload = request(client, input="uniqueteamaterial", document_ids=[])
    assert client.post("/api/tea/templates/debug", json=payload).json()["citations"] == []
    payload["document_ids"] = [doc["id"]]
    result = client.post("/api/tea/templates/debug", json=payload)
    assert result.status_code == 200, result.text
    assert {item["document_id"] for item in result.json()["citations"]} == {doc["id"]}
    run_id = result.json()["id"]
    client.post("/api/tea/auth/logout")
    other = client.post("/api/tea/auth/demo", json={}).json()
    assert client.post("/api/tea/templates/debug", json=payload).status_code == 403
    assert client.get("/api/tea/templates/debug/runs/" + run_id).status_code == 403
    grant_admin(client, other["id"])
    assert client.post("/api/tea/templates/debug", json=payload).status_code == 404
    assert client.get("/api/tea/templates/debug/runs/" + run_id).status_code == 404


def test_debug_stream_delivers_steps_before_done_and_cancel_retains_checkpoint(client):
    payload = request(client)
    response = client.post("/api/tea/templates/debug/stream", json=payload)
    assert response.status_code == 200, response.text
    frames = [
        (part.split("\n")[0][7:], json.loads(part.split("data: ", 1)[1]))
        for part in response.text.split("\n\n")
        if part.startswith("event: ")
    ]
    assert frames[-1][0] == "done"
    steps = [data for kind, data in frames if kind == "step"]
    assert [step["status"] for step in steps if step["id"] == "check"] == ["running", "completed"]
    owner = client.get("/api/tea/auth/me").json()["id"]
    completed = []
    run_id = []

    def emit(kind, data):
        if kind == "run":
            run_id.append(data["id"])
        if kind == "step" and data["status"] == "completed":
            completed.append(data["id"])
            if data["id"] == "check":
                raise StreamCancelled()

    service = client.app.state.service
    with pytest.raises(StreamCancelled):
        service.studio.debug(owner, TemplateDebugInput.model_validate(payload), emit=emit)
    saved = client.get("/api/tea/templates/debug/runs/" + run_id[0]).json()
    assert saved["status"] == "cancelled"
    assert completed == ["retrieve", "check"]
    payload.update(source_run_id=run_id[0], start_step_id="guide")
    resumed = client.post("/api/tea/templates/debug", json=payload)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["trace"][1]["status"] == "cached"


def test_debug_migration_upgrades_existing_database_idempotently(client, tmp_path):
    with client.app.state.service.db.transaction() as conn:
        conn.execute("DROP TABLE IF EXISTS debug_runs")
        conn.execute("DROP TABLE IF EXISTS debug_samples")
        conn.execute("DELETE FROM schema_migrations WHERE version=8")
    Service(tmp_path)
    Service(tmp_path)
    with closing(client.app.state.service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=8").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] > 0
        assert conn.execute("SELECT COUNT(*) FROM debug_samples").fetchone()[0] == 0


def test_expired_worker_claim_becomes_reviewable_cancelled_run(client):
    payload = request(client, client_id="interrupted-run")
    result = client.post("/api/tea/templates/debug", json=payload).json()
    with client.app.state.service.db.transaction() as conn:
        conn.execute(
            "UPDATE debug_runs SET status='running',expires_at='2000-01-01' WHERE id=?", (result["id"],)
        )
    replayed = client.post("/api/tea/templates/debug", json=payload)
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["status"] == "cancelled"
    assert replayed.json()["error"]["code"] == "debug_interrupted"


def test_failed_checkpoint_cannot_be_reused_and_retry_does_not_repeat_provider(client, monkeypatch):
    payload = request(client, client_id="failed-run")
    service = client.app.state.service
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("provider secret must not be exposed")

    monkeypatch.setattr(service, "search", unavailable)
    response = client.post("/api/tea/templates/debug", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "failed"
    assert result["trace"][0]["status"] == "failed"
    assert "provider secret" not in response.text
    assert client.post("/api/tea/templates/debug", json=payload).json() == result
    assert calls == [1]
    payload.pop("client_id")
    payload.update(source_run_id=result["id"], start_step_id="check")
    assert client.post("/api/tea/templates/debug", json=payload).status_code == 409


def test_save_library_scope_fixes_documents_and_new_documents_do_not_join(client):
    document = client.post(
        "/api/tea/documents", files={"file": ("first.txt", b"fixedsampletoken", "text/plain")}
    ).json()
    payload = request(client, document_ids=None, input="fixedsampletoken")
    _, saved = sample(client, payload)
    assert document["id"] in saved["document_ids"]
    second = client.post(
        "/api/tea/documents", files={"file": ("second.txt", b"fixedsampletoken", "text/plain")}
    ).json()
    payload.update(
        document_ids=saved["document_ids"], sample_id=saved["id"], sample_revision=saved["revision"]
    )
    result = client.post("/api/tea/templates/debug", json=payload)
    assert result.status_code == 200, result.text
    assert second["id"] not in [citation["document_id"] for citation in result.json()["citations"]]


def test_running_document_change_invalidates_result_and_saved_checkpoints(client, monkeypatch):
    document = client.post(
        "/api/tea/documents", files={"file": ("first.txt", b"fixedsampletoken", "text/plain")}
    ).json()
    payload = request(client, document_ids=[document["id"]], input="fixedsampletoken")
    service = client.app.state.service
    original = service.search

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        with service.db.transaction() as conn:
            conn.execute("UPDATE documents SET version=version+1 WHERE id=?", (document["id"],))
        return result

    monkeypatch.setattr(service, "search", mutate)
    result = client.post("/api/tea/templates/debug", json=payload).json()
    assert result["status"] == "failed"
    assert result["error"]["code"] == "debug_context_changed"
    payload.update(source_run_id=result["id"], start_step_id="guide")
    assert client.post("/api/tea/templates/debug", json=payload).status_code == 409


@pytest.mark.parametrize(
    "expectation",
    [
        {"id": "a", "kind": "contains"},
        {"id": "a", "kind": "manual"},
        {"id": "a", "kind": "check", "check_id": "confirmation"},
        {"id": "a", "kind": "citation"},
    ],
)
def test_malformed_expectations_are_rejected(client, expectation):
    response = client.post("/api/tea/templates/debug", json=request(client, expectations=[expectation]))
    assert response.status_code == 422


def test_cancelled_later_retrieval_cannot_replace_guidance_citations(client):
    payload = request(client, input="currentevidence")
    payload["template"]["agent"]["steps"] = [
        {"id": "guide", "type": "guide", "label": "guide", "enabled": True, "instructions": ""},
        {"id": "retrieve", "type": "retrieve", "label": "retrieve", "enabled": True, "instructions": ""},
    ]
    document = client.post(
        "/api/tea/documents", files={"file": ("late.txt", b"currentevidence", "text/plain")}
    ).json()
    payload["document_ids"] = [document["id"]]
    owner = client.get("/api/tea/auth/me").json()["id"]
    run_ids = []

    def cancel(event, data):
        if event == "run":
            run_ids.append(data["id"])
        if event == "step" and data["id"] == "retrieve" and data["status"] == "completed":
            raise StreamCancelled()

    with pytest.raises(StreamCancelled):
        client.app.state.service.studio.debug(owner, TemplateDebugInput.model_validate(payload), emit=cancel)
    result = client.get("/api/tea/templates/debug/runs/" + run_ids[0]).json()
    assert result["status"] == "cancelled"
    assert result["citations"] == []
    assert result["trace"][1]["output_data"]["citations"][0]["document_id"] == document["id"]


def test_provider_receives_the_inspected_guidance_payload_and_long_output(client, monkeypatch):
    import httpx

    payload = request(client)
    owner = client.get("/api/tea/auth/me").json()["id"]
    monkeypatch.setenv("LLM_API_KEY", "not-a-real-credential")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    calls = []
    output = "清楚的实际建议。" * 300
    original = httpx.Client

    def receive(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": output}}]})

    monkeypatch.setattr(
        httpx, "Client", lambda **options: original(transport=httpx.MockTransport(receive), **options)
    )
    result = client.app.state.service.studio.debug(owner, TemplateDebugInput.model_validate(payload))
    assert result["status"] == "completed"
    trace = result["trace"][-1]
    inspected = {
        key: value
        for key, value in trace["input_data"].items()
        if key not in ("instructions", "step_instructions", "language", "instructions_applied")
    }
    assert json.loads(calls[0]["messages"][-1]["content"]) == inspected
    assert trace["output_data"]["content"] == output
    assert len(trace["output_data"]["content"]) > 1500
    assert "not-a-real-credential" not in json.dumps(result)


def test_sample_and_checkpoint_reject_changed_index_even_when_file_version_is_unchanged(client):
    document = client.post(
        "/api/tea/documents", files={"file": ("index.txt", b"originalsampletoken", "text/plain")}
    ).json()
    payload = request(client, document_ids=[document["id"]], input="originalsampletoken")
    _, saved = sample(client, payload)
    first = client.post("/api/tea/templates/debug", json=payload).json()
    with client.app.state.service.db.transaction() as conn:
        row = conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()
        manifest = json.loads(row["manifest"])
        manifest["chunks"][0]["text"] = "corrected parsed text"
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), document["id"]))
    with_sample = payload | {"sample_id": saved["id"], "sample_revision": saved["revision"]}
    assert client.post("/api/tea/templates/debug", json=with_sample).status_code == 409
    continued = payload | {"source_run_id": first["id"], "start_step_id": "guide"}
    assert client.post("/api/tea/templates/debug", json=continued).status_code == 409


def test_history_is_bounded_and_stage_filtered(client, monkeypatch):
    import app.debugger as debugger

    monkeypatch.setattr(debugger, "MAX_RUNS", 2)
    payload = request(client)
    run_ids = [client.post("/api/tea/templates/debug", json=payload).json()["id"] for _ in range(3)]
    url = f"/api/tea/templates/{payload['template']['id']}/debug-runs"
    page = client.get(url + "?limit=1").json()
    assert page["total"] == 2
    assert page["items"][0]["id"] == run_ids[-1]
    assert client.get(url + "?limit=1&offset=1").json()["items"][0]["id"] == run_ids[-2]
    assert client.get(url + "?stage_id=another-stage").json()["total"] == 0
    assert client.get("/api/tea/templates/debug/runs/" + run_ids[0]).status_code == 404
    assert client.get(url + "?limit=101").status_code == 422


def test_same_account_concurrent_runs_are_rejected_before_repeating_steps(client, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from app.service import AppError

    payload = request(client, client_id="first")
    owner = client.get("/api/tea/auth/me").json()["id"]
    service = client.app.state.service
    original = service.search
    entered, released = threading.Event(), threading.Event()
    calls = []

    def wait(*args, **options):
        calls.append(1)
        entered.set()
        assert released.wait(5)
        return original(*args, **options)

    monkeypatch.setattr(service, "search", wait)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(service.studio.debug, owner, TemplateDebugInput.model_validate(payload))
        assert entered.wait(3)
        try:
            with pytest.raises(AppError) as duplicate:
                service.studio.debug(owner, TemplateDebugInput.model_validate(payload))
            assert duplicate.value.code == "debug_in_progress"
            payload["client_id"] = "second"
            with pytest.raises(AppError) as another:
                service.studio.debug(owner, TemplateDebugInput.model_validate(payload))
            assert another.value.code == "debug_in_progress"
        finally:
            released.set()
        assert pending.result(timeout=5)["status"] == "completed"
    assert calls == [1]


def test_demo_guidance_inspector_shows_all_rule_inputs(client):
    payload = request(client)
    prototype = payload["template"]["stages"][0]["tasks"][0]
    tasks = []
    for index in range(3):
        task = copy.deepcopy(prototype)
        task["id"] = f"wide-task-{index}"
        task["fields"] = [
            {"key": f"field_{field}", "label": f"资料{field}", "type": "text", "required": True}
            for field in range(30)
        ]
        tasks.append(task)
    payload["template"]["stages"][0]["tasks"] = tasks
    result = client.post("/api/tea/templates/debug", json=payload).json()
    assert len(result["checks"]) > 60
    assert result["trace"][-1]["input_data"]["checks"] == result["checks"]
    assert result["trace"][-1]["input_data"]["instructions_applied"] is False


@pytest.mark.parametrize("scenario", ["paused", "failed", "disabled", "context_changed"])
def test_answer_expectations_are_not_passed_without_a_valid_answer(client, monkeypatch, scenario):
    payload = request(
        client, expectations=[{"id": "guardrail", "kind": "not_contains", "text": "手续已办理"}]
    )
    task = payload["template"]["stages"][0]["tasks"][0]
    payload["expectations"].append(
        {
            "id": "field",
            "kind": "check",
            "task_id": task["id"],
            "check_id": task["fields"][0]["key"],
            "status": "missing",
        }
    )
    if scenario == "paused":
        payload["stop_after_step_id"] = "check"
    elif scenario == "disabled":
        payload["template"]["agent"]["steps"][-1]["enabled"] = False
    elif scenario == "failed":
        import app.assistant as assistant

        def fail(*args, **kwargs):
            raise RuntimeError("failed guide")

        monkeypatch.setattr(assistant, "rule_guidance", fail)
    else:
        document = client.post(
            "/api/tea/documents", files={"file": ("changing.txt", b"changing", "text/plain")}
        ).json()
        payload["document_ids"] = [document["id"]]
        service = client.app.state.service
        original = service.search

        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            with service.db.transaction() as conn:
                conn.execute("UPDATE documents SET version=version+1 WHERE id=?", (document["id"],))
            return result

        monkeypatch.setattr(service, "search", changed)
    response = client.post("/api/tea/templates/debug", json=payload)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["evaluations"][0]["status"] == "manual"
    assert "未" in result["evaluations"][0]["actual"]
    assert result["evaluations"][1]["status"] == "passed"
    history = client.get(f"/api/tea/templates/{payload['template']['id']}/debug-runs").json()
    assert history["items"][0]["evaluation_status"] == "manual"


@pytest.mark.parametrize("middle", ["disabled", "draft"])
def test_cached_prefix_keeps_nonexecuted_steps_skipped(client, middle):
    payload = request(client)
    steps = payload["template"]["agent"]["steps"]
    if middle == "disabled":
        steps[1]["enabled"] = False
    else:
        steps[1]["type"] = "draft"
    first = client.post("/api/tea/templates/debug", json=payload).json()
    assert first["trace"][1]["status"] == "skipped"
    result = client.post(
        "/api/tea/templates/debug",
        json=payload
        | {
            "source_run_id": first["id"],
            "start_step_id": "guide",
            "stop_after_step_id": "guide",
        },
    ).json()
    assert result["status"] == "completed"
    assert result["trace"][0]["status"] == "cached"
    assert result["trace"][1]["status"] == "skipped"
    assert result["trace"][1]["detail"] == first["trace"][1]["detail"]
    assert result["trace"][2]["status"] == "completed"
