"""Behavior contracts for saved configuration, stage runners and library inspection."""

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Service


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with TestClient(create_app(data_dir=tmp_path)) as client:
        assert client.post("/api/tea/auth/demo", json={}).status_code == 200
        yield client


def template(client):
    return client.get("/api/tea/templates").json()["items"][0]


def case(client):
    case_id = client.get("/api/tea/cases").json()["items"][0]["id"]
    return client.get("/api/tea/cases/" + case_id).json()


def agent(*types):
    return {
        "language": "中文",
        "instructions": "逐项解释材料用途",
        "retrieval_limit": 3,
        "steps": [
            {"id": f"step-{i}", "type": kind, "label": kind, "enabled": True, "instructions": ""}
            for i, kind in enumerate(types)
        ],
    }


def test_stage_analysis_override_and_step_instructions_validate(client):
    value = template(client)
    value["stages"][1]["analysis"] = agent("check", "guide")
    value["stages"][1]["analysis"]["steps"][1]["instructions"] = "用三个简短要点说明缺失信息"
    response = client.post("/api/tea/templates/validate", json={"template": value})
    assert response.json()["valid"] is True, response.text
    value["stages"][1]["analysis"]["steps"][1]["id"] = "step-0"
    assert client.post("/api/tea/templates/validate", json={"template": value}).json()["valid"] is False


def test_all_disabled_workflow_is_invalid(client):
    value = template(client)
    for step in value["agent"]["steps"]:
        step["enabled"] = False
    assert client.post("/api/tea/templates/validate", json={"template": value}).json()["valid"] is False


def test_template_draft_is_revisioned_and_survives_restart(client, tmp_path):
    value = template(client)
    url = f"/api/tea/templates/{value['id']}/draft"
    assert client.get(url).json() == {"template": None, "revision": 0, "updated_at": None}
    incomplete = {"id": value["id"], "title": "未填完的配置", "stages": []}
    saved = client.put(url, json={"template": incomplete, "expected_revision": 0})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert saved.json()["template"] == incomplete
    with TestClient(create_app(data_dir=tmp_path)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(url).json() == saved.json()
    assert client.put(url, json={"template": value, "expected_revision": 0}).status_code == 409
    assert client.put(url, json={"template": {"id": "another"}, "expected_revision": 1}).status_code == 422
    client.post("/api/tea/auth/logout")
    client.post("/api/tea/auth/demo", json={})
    assert client.get(url).json()["revision"] == 0


def test_publish_checks_latest_template_and_saved_draft_revision(client):
    value = template(client)
    url = f"/api/tea/templates/{value['id']}/draft"
    saved = client.put(url, json={"template": value, "expected_revision": 0})
    assert saved.status_code == 200, saved.text
    stale = client.post("/api/tea/templates/publish", json={"template": value, "expected_draft_revision": 0})
    assert stale.status_code == 409, stale.text
    published = client.post(
        "/api/tea/templates/publish", json={"template": value, "expected_draft_revision": 1}
    )
    assert published.status_code == 200, published.text
    draft = client.get(url).json()
    assert draft["revision"] == 2
    assert draft["template"] == published.json()
    assert client.post("/api/tea/templates/publish", json={"template": value}).status_code == 409
    assert case(client)["template_version"] == value["version"]


def test_new_draft_and_template_are_owner_scoped(client):
    value = template(client)
    value.update(id="imported-workflow", version=1)
    url = "/api/tea/templates/imported-workflow/draft"
    assert client.put(url, json={"template": value, "expected_revision": 0}).status_code == 200
    published = client.post(
        "/api/tea/templates/publish", json={"template": value, "expected_draft_revision": 1}
    )
    assert published.status_code == 200, published.text
    assert published.json()["version"] == 1
    client.post("/api/tea/auth/logout")
    client.post("/api/tea/auth/demo", json={})
    assert client.get(url).status_code == 404
    assert client.get("/api/tea/templates/imported-workflow").status_code == 404


def test_debug_uses_selected_stage_all_tasks_and_stops_at_requested_step(client):
    value = template(client)
    stage = value["stages"][1]
    stage["analysis"] = agent("check", "guide")
    values = {
        task["id"]: {field["key"]: 100 if field["type"] == "number" else "已填写" for field in task["fields"]}
        for task in stage["tasks"]
    }
    response = client.post(
        "/api/tea/templates/debug",
        json={
            "template": value,
            "input": "测试第二节点",
            "stage_id": stage["id"],
            "task_values": values,
            "stop_after_step_id": "step-0",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["stage_id"] == stage["id"]
    assert result["assistant_mode"] == "demo"
    assert result["citations"] == []
    assert [step["id"] for step in result["trace"]] == ["step-0", "step-1"]
    assert result["trace"][0]["status"] == "completed"
    assert result["trace"][1]["status"] == "skipped"
    assert {check["task_id"] for check in result["checks"]} == {task["id"] for task in stage["tasks"]}
    assert all(
        check["status"] == "passed"
        for check in result["checks"]
        if check["id"] not in ("document", "confirmation")
    )
    assert case(client)["completed_tasks"] == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"stage_id": "unknown-stage"},
        {"stop_after_step_id": "unknown-step"},
        {"task_values": {"unknown-task": {}}},
        {"task_values": {"product-info": {"unknown-field": "value"}}},
        {"task_values": {"product-info": {"net_weight_g": "not-a-number"}}},
    ],
)
def test_debug_rejects_invalid_selection_and_sample_values(client, changes):
    value = template(client)
    # Sample task ids are deliberately obtained from the actual configured schema.
    if "task_values" in changes and "product-info" in changes["task_values"]:
        changes = {
            "task_values": {value["stages"][0]["tasks"][0]["id"]: changes["task_values"]["product-info"]}
        }
    response = client.post("/api/tea/templates/debug", json={"template": value, "input": "", **changes})
    assert response.status_code == 422, response.text


def test_debug_sequence_does_not_report_later_checks_as_earlier_guidance(client):
    value = template(client)
    value["agent"] = agent("guide", "check", "draft", "verify")
    response = client.post("/api/tea/templates/debug", json={"template": value, "input": "缺什么"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert "当前需要先核对" not in result["output"]
    assert result["checks"]
    assert [step["status"] for step in result["trace"]] == ["completed", "completed", "skipped", "skipped"]
    assert "未调用" in result["trace"][0]["detail"]


def test_document_filters_and_paginated_structure_do_not_expose_embeddings(client):
    main = client.post(
        "/api/tea/documents", files={"file": ("structure.md", b"# Topic\n\nFirst\n\nSecond", "text/markdown")}
    ).json()
    secondary = client.post(
        "/api/tea/documents",
        data={"role": "secondary", "parent_id": main["id"]},
        files={"file": ("appendix.txt", b"Never index this", "text/plain")},
    ).json()
    parent_url = f"/api/tea/documents?parent_id={main['id']}"
    listed = client.get(parent_url).json()
    assert [row["id"] for row in listed["items"]] == [secondary["id"]]
    assert client.get("/api/tea/documents?role=primary&q=structure").json()["total"] == 1
    assert client.get("/api/tea/documents?role=secondary&q=absent").json()["total"] == 0
    service = client.app.state.service
    with service.db.transaction() as conn:
        row = conn.execute("SELECT manifest FROM documents WHERE id=?", (main["id"],)).fetchone()
        manifest = json.loads(row["manifest"])
        manifest["chunks"][0]["embedding"] = [9, 8, 7]
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), main["id"]))
    result = client.get(f"/api/tea/documents/{main['id']}?element_offset=1&chunk_offset=0&limit=1").json()
    assert result["element_total"] > 1
    assert len(result["elements"]) == 1
    assert result["elements"][0] == manifest["elements"][1]
    assert len(result["chunks"]) == 1
    assert "embedding" not in result["chunks"][0]
    assert result["chunk_total"] == len(manifest["chunks"])
    assert result["parser"]
    assert "warnings" in result
    second = client.get("/api/tea/documents/" + secondary["id"]).json()
    assert second["element_total"] == second["chunk_total"] == 0
    assert client.get(parent_url + "&case_id=" + case(client)["id"]).status_code == 404
    client.post("/api/tea/auth/logout")
    client.post("/api/tea/auth/demo", json={})
    assert client.get(parent_url).status_code == 404


def test_studio_migration_preserves_existing_database(tmp_path):
    from app.catalog import LegalCatalog
    from app.db import Database, now

    db = Database(tmp_path)
    LegalCatalog(db)
    with db.transaction() as conn:
        conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", ("retained", "已有账号", None, None, 1, now()))
    service = Service(tmp_path)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT name FROM users WHERE id='retained'").fetchone()[0] == "已有账号"
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] >= 3
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} >= {
            "template_drafts",
            "stage_analyses",
        }
    Service(tmp_path)  # idempotent upgrade


def analysis_url(value, stage_id=None):
    return f"/api/tea/cases/{value['id']}/stages/{stage_id or value['stages'][0]['id']}/analysis"


def test_stage_analysis_is_persistent_idempotent_and_does_not_change_case(client, tmp_path):
    before = case(client)
    payload = {"expected_revision": before["revision"]}
    first = client.post(analysis_url(before), json=payload)
    assert first.status_code == 200, first.text
    result = first.json()
    assert result["status"] == "completed"
    assert result["case_revision"] == before["revision"]
    assert result["checks"]
    assert client.post(analysis_url(before), json=payload | {"retry": True}).json() == result
    with TestClient(create_app(data_dir=tmp_path)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.post(analysis_url(before), json=payload).json() == result
    after = case(client)
    assert after["revision"] == before["revision"]
    assert after["stages"] == before["stages"]


def test_stage_analysis_enforces_owner_revision_and_locked_stage(client):
    value = case(client)
    payload = {"expected_revision": value["revision"]}
    assert client.post(analysis_url(value, value["stages"][1]["id"]), json=payload).status_code == 409
    assert client.post(analysis_url(value), json={"expected_revision": 9999}).status_code == 409
    assert client.post(analysis_url(value, "unknown"), json=payload).status_code == 404
    client.post("/api/tea/auth/logout")
    client.post("/api/tea/auth/demo", json={})
    assert client.post(analysis_url(value), json=payload).status_code == 404


def test_analysis_recomputes_on_task_edit_or_library_review_change(client):
    value = case(client)
    payload = {"expected_revision": value["revision"]}
    first = client.post(analysis_url(value), json=payload)
    assert first.status_code == 200, first.text
    doc = client.post(
        "/api/tea/documents", files={"file": ("资料.md", "资料准备\n办理步骤".encode(), "text/markdown")}
    ).json()
    second = client.post(analysis_url(value), json=payload).json()
    assert second["id"] != first.json()["id"]
    changed = client.patch(
        "/api/tea/documents/" + doc["id"] + "/metadata",
        json={"expected_revision": doc["metadata_revision"], "review_status": "draft"},
    )
    assert changed.status_code == 200
    third = client.post(analysis_url(value), json=payload).json()
    assert third["id"] != second["id"]
    task = value["stages"][0]["tasks"][0]
    field = task["fields"][0]
    changed = client.patch(
        f"/api/tea/cases/{value['id']}/tasks/{task['id']}",
        json={
            "expected_revision": task["revision"],
            "values": {field["key"]: "新资料"},
        },
    ).json()
    assert client.post(analysis_url(value), json=payload).status_code == 409
    fourth = client.post(analysis_url(value), json={"expected_revision": changed["revision"]}).json()
    assert fourth["id"] != third["id"]


def enable_live(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "test-secret")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.invalid/v1")


def test_failed_analysis_is_visible_and_only_explicit_retry_calls_provider(client, monkeypatch):
    import app.assistant as assistant

    value = case(client)
    enable_live(monkeypatch)
    calls = []

    def provider(*args):
        calls.append(args)
        if len(calls) == 1:
            raise TimeoutError("test failure")
        return "重新生成的节点建议"

    monkeypatch.setattr(assistant, "generate_guidance", provider)
    first = client.post(analysis_url(value), json={"expected_revision": value["revision"]})
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "failed"
    second = client.post(analysis_url(value), json={"expected_revision": value["revision"]})
    assert second.json()["id"] == first.json()["id"]
    assert len(calls) == 1
    retried = client.post(
        analysis_url(value), json={"expected_revision": value["revision"], "retry": True}
    ).json()
    assert retried["status"] == "completed"
    assert retried["id"] != first.json()["id"]
    assert len(calls) == 2
    assert case(client)["revision"] == value["revision"]


def test_analysis_cache_expires_when_same_model_finishes_document_vectors(client, monkeypatch):
    from app import ingestion

    for field, item in {
        "BASE_URL": "https://embedding.invalid",
        "API_KEY": "test",
        "MODEL": "fixed-model",
    }.items():
        monkeypatch.setenv("EMBEDDING_" + field, item)

    def unavailable(texts):
        raise ValueError("provider temporarily unavailable")

    monkeypatch.setattr(ingestion, "embed_texts", unavailable)
    document = client.post(
        "/api/tea/documents", files={"file": ("材料.txt", "企业茶叶资料".encode(), "text/plain")}
    ).json()
    value = case(client)
    payload = {"expected_revision": value["revision"]}
    first = client.post(analysis_url(value), json=payload).json()
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1.0, 0.5] for _ in texts])
    with service.db.transaction() as conn:
        service.collection.queue_processing(conn, document["id"], owner)
    for _ in range(3):
        if claim := service.collection.claim_processing():
            service.collection.process_item(claim)
    second = client.post(analysis_url(value), json=payload).json()
    assert second["id"] != first["id"]
    assert client.post(analysis_url(value), json=payload).json()["id"] == second["id"]


def test_legal_and_stage_caches_expire_when_reranker_configuration_changes(client, monkeypatch):
    value = case(client)
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]

    def fingerprints():
        with service.db.transaction() as conn:
            return (
                service.legal.snapshot(conn, owner, value)["environment"],
                service.studio.fingerprint(conn, owner, value),
            )

    initial = fingerprints()
    monkeypatch.setenv("RERANK_BASE_URL", "https://rank.example/v1")
    monkeypatch.setenv("RERANK_MODEL", "rank-1")
    partial = fingerprints()
    assert all(a != b for a, b in zip(initial, partial, strict=True))
    monkeypatch.setenv("RERANK_API_KEY", "private-test-key")
    enabled = fingerprints()
    assert all(a != b for a, b in zip(partial, enabled, strict=True))
    monkeypatch.setenv("RERANK_API_KEY", "rotated-private-test-key")
    assert fingerprints() == enabled
    monkeypatch.setenv("RERANK_MODEL", "rank-2")
    assert all(a != b for a, b in zip(enabled, fingerprints(), strict=True))


def test_analysis_claim_prevents_duplicate_provider_calls_without_holding_write_lock(client, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import app.assistant as assistant

    value = case(client)
    enable_live(monkeypatch)
    started, release = Event(), Event()
    calls = []

    def provider(*args):
        calls.append(args)
        started.set()
        assert release.wait(5)
        return "节点建议"

    monkeypatch.setattr(assistant, "generate_guidance", provider)
    payload = {"expected_revision": value["revision"]}
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.post, analysis_url(value), json=payload)
        try:
            assert started.wait(5)
            with client.app.state.service.db.transaction() as conn:
                assert conn.execute("SELECT 1").fetchone()[0] == 1
            duplicate = client.post(analysis_url(value), json=payload)
            assert duplicate.status_code == 409
            assert duplicate.json()["error"]["code"] == "analysis_in_progress"
        finally:
            release.set()
        first = pending.result(timeout=5)
    assert first.status_code == 200, first.text
    assert len(calls) == 1
    assert client.post(analysis_url(value), json=payload).json()["id"] == first.json()["id"]


def test_analysis_rejects_results_when_case_changes_during_generation(client, monkeypatch):
    import app.assistant as assistant

    value = case(client)
    owner = client.get("/api/tea/auth/me").json()["id"]
    task = value["stages"][0]["tasks"][0]
    service = client.app.state.service
    enable_live(monkeypatch)

    def provider(*args):
        service.patch_task(
            owner,
            value["id"],
            task["id"],
            {
                "expected_revision": task["revision"],
                "values": {task["fields"][0]["key"]: "已变化"},
            },
        )
        return "基于旧资料的指导"

    monkeypatch.setattr(assistant, "generate_guidance", provider)
    response = client.post(analysis_url(value), json={"expected_revision": value["revision"]})
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "analysis_stale"
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM stage_analyses WHERE status='completed'").fetchone()[0] == 0


def simple_case(client):
    value = template(client)
    value["id"] = "simple-analysis"
    value["version"] = 1
    value["stages"] = [
        {
            "id": f"stage-{i}",
            "name": f"节点 {i}",
            "actor": "企业",
            "description": "",
            "analysis": agent("check", "guide"),
            "tasks": [
                {
                    "id": f"task-{i}",
                    "title": "填写名称",
                    "description": "",
                    "required": True,
                    "require_document": False,
                    "require_confirmation": False,
                    "fields": [{"key": "name", "label": "名称", "type": "text", "required": True}],
                }
            ],
        }
        for i in range(2)
    ]
    assert client.post("/api/tea/templates/publish", json={"template": value}).status_code == 200
    created = client.post(
        "/api/tea/cases",
        json={"title": "主动分析测试", "template_id": value["id"], "mode_id": value["modes"][0]["id"]},
    )
    assert created.status_code == 201, created.text
    return created.json()


def test_unlocked_stage_analysis_and_chat_use_stage_override(client):
    value = simple_case(client)
    assert (
        client.post(analysis_url(value, "stage-1"), json={"expected_revision": value["revision"]}).status_code
        == 409
    )
    url = f"/api/tea/cases/{value['id']}/tasks/task-0"
    changed = client.patch(url, json={"expected_revision": 1, "values": {"name": "茶叶"}}).json()
    verified = client.post(
        url + "/verify",
        json={"expected_revision": changed["stages"][0]["tasks"][0]["revision"], "confirm": True},
    ).json()
    response = client.post(analysis_url(value, "stage-1"), json={"expected_revision": verified["revision"]})
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["trace"]] == ["step-0", "step-1"]
    chat = client.post(
        f"/api/tea/cases/{value['id']}/messages",
        json={"stage_id": "stage-1", "content": "请分析", "client_message_id": "override-chat"},
    )
    assert chat.status_code == 200, chat.text
    assert [row["id"] for row in chat.json()["messages"][1]["trace"]] == ["step-0", "step-1"]


def test_live_debug_receives_step_instructions_and_prior_step_outputs(client, monkeypatch):
    import httpx

    value = template(client)
    value["agent"] = agent("check", "guide", "guide")
    value["agent"]["steps"][1]["instructions"] = "列出三条最优先建议"
    value["agent"]["steps"][2]["instructions"] = "根据上一条建议写简短摘要"
    enable_live(monkeypatch)
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": f"建议 {len(requests)}"}}]})

    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    response = client.post("/api/tea/templates/debug", json={"template": value, "input": "样例"})
    assert response.status_code == 200, response.text
    assert response.json()["output"] == "建议 2"
    assert "列出三条最优先建议" in requests[0]["messages"][0]["content"]
    assert "根据上一条建议写简短摘要" in requests[1]["messages"][0]["content"]
    assert json.loads(requests[1]["messages"][-1]["content"])["previous_step_output"] == "建议 1"
    assert json.loads(requests[0]["messages"][-1]["content"])["checks"]


def test_debug_rejects_numbers_outside_finite_range(client):
    value = template(client)
    task = value["stages"][0]["tasks"][0]
    numeric = next(field["key"] for field in task["fields"] if field["type"] == "number")
    response = client.post(
        "/api/tea/templates/debug",
        json={
            "template": value,
            "input": "",
            "task_values": {task["id"]: {numeric: 10**400}},
        },
    )
    assert response.status_code == 422, response.text


def test_slow_stream_cannot_extend_model_generation_indefinitely(client, monkeypatch):
    import httpx
    import app.assistant as assistant

    value = template(client)
    value["agent"] = agent("guide")
    enable_live(monkeypatch)
    moments = iter([0, 46])
    monkeypatch.setattr(assistant, "monotonic", lambda: next(moments), raising=False)
    original = httpx.Client
    response_body = {"choices": [{"message": {"content": "这份答复已超过时间预算"}}]}
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response_body)),
            **kwargs,
        ),
    )
    result = client.post("/api/tea/templates/debug", json={"template": value, "input": "请分析"})
    assert result.status_code == 200, result.text
    assert result.json()["trace"][0]["status"] == "failed"
    assert "超过时间预算" not in result.json()["output"]


def test_retrieval_after_guidance_is_not_presented_as_its_supporting_evidence(client):
    value = template(client)
    value["destination"] = "待确认目的地"  # This test uses an unassigned library sample.
    value["agent"] = agent("guide", "retrieve")
    uploaded = client.post(
        "/api/tea/documents",
        files={
            "file": ("later-source.md", b"lateevidence product facts", "text/markdown"),
        },
    )
    assert uploaded.status_code == 201
    response = client.post("/api/tea/templates/debug", json={"template": value, "input": "lateevidence"})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["trace"][1]["status"] == "completed"
    assert "later-source" in result["trace"][1]["output"]
    assert result["citations"] == []


def test_draft_summaries_are_bounded_paginated_and_account_scoped(client):
    url = "/api/tea/templates/drafts"
    initial = client.get(url)
    assert initial.status_code == 200, initial.text
    assert initial.json() == {"items": [], "total": 0}
    owner = client.get("/api/tea/auth/me").json()["id"]
    for draft_id, title in [("draft-a", {"unfinished": True}), ("draft-b", "  "), ("draft-c", "标题" * 150)]:
        saved = client.put(
            f"/api/tea/templates/{draft_id}/draft",
            json={
                "template": {"id": draft_id, "title": title, "description": "只应存在详情中的草稿内容"},
                "expected_revision": 0,
            },
        )
        assert saved.status_code == 200, saved.text
    with client.app.state.service.db.transaction() as conn:
        conn.execute(
            "UPDATE template_drafts SET updated_at=? WHERE owner_id=?", ("2026-10-11T10:00:00+00:00", owner)
        )
    first = client.get(url, params={"limit": 2}).json()
    second = client.get(url, params={"limit": 2, "offset": 2}).json()
    assert first["total"] == second["total"] == 3
    assert [item["id"] for item in first["items"] + second["items"]] == ["draft-c", "draft-b", "draft-a"]
    assert all(
        set(item) == {"id", "title", "revision", "updated_at"} for item in first["items"] + second["items"]
    )
    assert len(first["items"][0]["title"]) == 200
    assert first["items"][1]["title"] == "draft-b"
    assert second["items"][0]["title"] == "draft-a"
    assert client.get(url, params={"offset": 3}).json() == {"items": [], "total": 3}
    assert client.get(url, params={"limit": 0}).status_code == 422
    assert client.get(url, params={"limit": 201}).status_code == 422
    assert client.get(url, params={"offset": -1}).status_code == 422
    client.post("/api/tea/auth/logout")
    assert client.get(url).status_code == 401
    client.post("/api/tea/auth/demo", json={})
    assert client.get(url).json() == {"items": [], "total": 0}


@pytest.mark.parametrize(("sample", "status"), [(-3, "passed"), (-6, "missing")])
def test_stage_debug_checks_signed_minimum(client, sample, status):
    value = template(client)
    value["agent"] = agent("check")
    task = value["stages"][0]["tasks"][0]
    field = next(field for field in task["fields"] if field["type"] == "number")
    field["min"] = -5
    response = client.post(
        "/api/tea/templates/debug",
        json={
            "template": value,
            "task_values": {task["id"]: {field["key"]: sample}},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["trace"][0]["status"] == "completed"
    check = next(
        item
        for item in response.json()["checks"]
        if item.get("task_id") == task["id"] and item["id"] == field["key"]
    )
    assert check["status"] == status


@pytest.mark.parametrize("sample", [True, False])
def test_debug_does_not_coerce_boolean_samples_into_numbers(client, sample):
    value = template(client)
    task = value["stages"][0]["tasks"][0]
    field = next(field for field in task["fields"] if field["type"] == "number")
    response = client.post(
        "/api/tea/templates/debug",
        json={
            "template": value,
            "task_values": {task["id"]: {field["key"]: sample}},
        },
    )
    assert response.status_code == 422, response.text
