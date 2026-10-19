"""Folders isolate turns, share attributed context and retain immutable reports."""

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.node_workspace import NodeWorkspace, select_skills


@pytest.fixture
def setup(tmp_path, monkeypatch, sample_workflow):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with TestClient(create_app(data_dir=tmp_path)) as client:
        owner = client.post("/api/tea/auth/demo", json={}).json()["id"]
        case = client.get("/api/tea/cases/" + client.get("/api/tea/cases").json()["items"][0]["id"]).json()
        yield client, client.app.state.service, owner, case


def conversation(client, case, key):
    response = client.post(
        f"/api/tea/cases/{case['id']}/stages/{case['stages'][0]['id']}/conversations",
        json={"client_request_id": key},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def send(client, case, conv, content, key):
    response = client.post(
        f"/api/tea/cases/{case['id']}/messages",
        json={
            "stage_id": case["stages"][0]["id"],
            "conversation_id": conv,
            "content": content,
            "client_message_id": key,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_two_conversations_have_private_history_and_attributed_shared_memory(setup):
    client, service, owner, case = setup
    first, second = conversation(client, case, "first"), conversation(client, case, "second")
    result = send(client, case, first, "乌龙茶包装安排还没确定", "turn-1")
    assert send(client, case, first, "乌龙茶包装安排还没确定", "turn-1")["messages"] == result["messages"]
    assert client.get(f"/api/tea/cases/{case['id']}/messages?conversation_id={second}").json()["total"] == 0
    prepared = service.prepare_message(
        owner,
        case["id"],
        {
            "stage_id": case["stages"][0]["id"],
            "conversation_id": second,
            "content": "包装",
            "client_message_id": "prepare",
        },
    )
    assert prepared["history"] == []
    note = prepared["memory"]["other_conversations"][0]
    assert note["message_id"] == result["messages"][1]["id"]
    assert note["status"] == "unverified_discussion"
    assert prepared["case"]["completed_tasks"] == case["completed_tasks"]
    assert conversation(client, case, "first") == first
    client.post("/api/tea/auth/logout")
    client.post("/api/tea/auth/demo", json={})
    assert client.get(f"/api/tea/cases/{case['id']}/messages?conversation_id={first}").status_code == 404


def test_guide_asks_first_materials_optional_and_appends_reports_once(setup):
    client, service, owner, case = setup
    stage = case["stages"][0]["id"]
    route = f"/api/tea/cases/{case['id']}/stages/{stage}"
    guide = client.post(route + "/guide-conversation").json()
    assert guide["kind"] == "guide"
    assert client.post(route + "/guide-conversation").json()["id"] == guide["id"]
    history = f"/api/tea/cases/{case['id']}/messages?conversation_id={guide['id']}"
    assert client.get(history).json()["total"] == 1
    answer = send(client, case, guide["id"], "我们刚开始整理产品情况", "intake")
    assert "没有材料也可以继续" in answer["messages"][1]["content"]
    assert client.get(route + "/guides").json()["total"] == 0
    report = send(client, case, guide["id"], "没有资料，开始分析", "report")
    saved = client.get(route + "/guides").json()["items"][0]
    assert saved["status"] == "completed"
    assert "recent_messages" not in saved["result"]["output"]
    assert saved["result"]["question"] == "没有资料，开始分析"
    assert {step["id"] for step in saved["trace"]} == {f"qa0{i}" for i in range(1, 7)}
    assert send(client, case, guide["id"], "没有资料，开始分析", "report")["messages"] == report["messages"]
    assert client.get(route + "/guides").json()["total"] == 1
    send(client, case, guide["id"], "接下来准备什么？", "continue")
    with service.db.transaction() as conn:
        service.node_workspace.save_guide(
            conn, case, stage, {"output": "changed", "trace": []}, "unused", saved["id"]
        )
        assert (
            json.loads(
                conn.execute("SELECT result FROM stage_guide_runs WHERE id=?", (saved["id"],)).fetchone()[0]
            )
            == saved["result"]
        )
        assert service.node_workspace.guide_phase(conn, guide["id"])["has_report"]


def test_draft_request_creates_one_editable_artifact_and_no_task_completion(setup):
    client, _, _, case = setup
    conv = conversation(client, case, "writer")
    result = send(client, case, conv, "请新建文稿《出口资料清单》", "draft")
    assert len(result["case"]["artifacts"]) == 1
    artifact = result["case"]["artifacts"][0]
    assert artifact["title"] == "出口资料清单"
    assert artifact["content"].startswith("#")
    assert result["case"]["completed_tasks"] == case["completed_tasks"]
    replay = send(client, case, conv, "请新建文稿《出口资料清单》", "draft")
    assert [item["id"] for item in replay["case"]["artifacts"]] == [artifact["id"]]


def test_task_completion_queues_unique_update_and_appends_to_guide(setup):
    client, service, owner, case = setup
    stage = case["stages"][0]["id"]
    guide = client.post(f"/api/tea/cases/{case['id']}/stages/{stage}/guide-conversation").json()
    task = case["stages"][0]["tasks"][0]
    document = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("产品.md", "产品名称：乌龙茶；成分：茶叶；净含量：250克".encode(), "text/markdown")},
    ).json()
    values = {field["key"]: (250 if field["type"] == "number" else "乌龙茶") for field in task["fields"]}
    values.update(ingredients="茶叶", bag_count=400)
    url = f"/api/tea/cases/{case['id']}/tasks/{task['id']}"
    changed = client.patch(
        url, json={"values": values, "evidence_ids": [document["id"]], "expected_revision": task["revision"]}
    ).json()["stages"][0]["tasks"][0]
    verified = client.post(url + "/verify", json={"expected_revision": changed["revision"], "confirm": True})
    assert verified.status_code == 200, verified.text
    current = verified.json()["stages"][0]["tasks"][0]
    assert current["status"] == "completed"
    client.post(url + "/verify", json={"expected_revision": current["revision"], "confirm": True})
    runs = service.node_workspace.guides(owner, case["id"], stage)
    assert runs["total"] == 1
    assert runs["items"][0]["status"] == "completed", runs
    assert service.messages(owner, case["id"], stage, conversation_id=guide["id"])["total"] == 2


def test_upgrade_backfills_old_messages_without_changing_them(setup):
    _, service, owner, case = setup
    with service.db.transaction() as conn:
        legacy = service.insert_message(
            conn, case["id"], case["stages"][0]["id"], None, "user", "旧的对话", [], []
        )
        for table in (
            "guide_messages",
            "node_memories",
            "conversation_messages",
            "stage_guide_runs",
            "stage_conversations",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM schema_migrations WHERE version IN (17,18)")
    workspace = NodeWorkspace(service)
    old = workspace.conversations(owner, case["id"])["items"][0]
    assert old["title"] == "原有会话"
    assert legacy["id"] in {
        item["id"] for item in service.messages(owner, case["id"], conversation_id=old["id"])["items"]
    }
    NodeWorkspace(service)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 19
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_skill_routing_combines_legal_material_and_drafting_intents():
    assert set(select_skills("核对海关资料并起草一份出口说明")) == {"legal", "materials", "draft"}
    assert "legal" in select_skills("帮我看看", mode="legal")
    assert "handoff" in select_skills("下一节点怎么交接")
