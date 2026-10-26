"""End-to-end tool scheduling against isolated SQLite and controlled providers."""

import base64
import copy
import json
import sqlite3
import threading
from contextlib import closing

import pytest
from app.contexts.conversations.application import agent_loop
from app.contexts.conversations.application.agent_tools import AgentTools
from app.contexts.conversations.application.agent_journal import AgentJournal
from app.contexts.conversations.application.agent_loop import compact_messages
from app.contexts.conversations.application.tool_runtime import ToolFailure
from app.interfaces.http.factory import create_app
from app.shared.application.streaming import StreamCancelled
from app.shared.domain.errors import AppError
from app.shared.infrastructure.persistence.database import Database
from app.shared.infrastructure.operations.data_backup import create_backup, restore_backup, verify_backup
from fastapi.testclient import TestClient


@pytest.fixture
def workspace(tmp_path, monkeypatch, sample_workflow):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        owner = client.post("/api/tea/auth/demo", json={}).json()["id"]
        case_id = client.get("/api/tea/cases").json()["items"][0]["id"]
        case = client.get(f"/api/tea/cases/{case_id}").json()
        stage = case["stages"][0]["id"]
        conv = client.post(
            f"/api/tea/cases/{case_id}/stages/{stage}/conversations", json={"client_request_id": "agent-test"}
        ).json()["id"]
        payload = {
            "stage_id": stage,
            "conversation_id": conv,
            "content": "核对资料并说明下一步",
            "client_message_id": "turn-1",
        }
        yield client, client.app.state.service, owner, case, payload


def tools(*pairs):
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": f"call_{name}_{index}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
            for index, (name, args) in enumerate(pairs)
        ],
    }


def scenario(monkeypatch, replies, seen=None):
    monkeypatch.setenv("LLM_API_KEY", "fixture-only")
    monkeypatch.setenv("AGENT_MODEL", "gemini-3.8-flash")
    replies = iter(replies)

    async def turn(messages, definitions, delta, check, **kwargs):
        check()
        if seen is not None:
            seen.append((copy.deepcopy(messages), copy.deepcopy(definitions)))
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        if callable(reply):
            reply = reply()
        if isinstance(reply, str):
            delta(reply)
            return {"role": "assistant", "content": reply}
        return reply

    monkeypatch.setattr(agent_loop, "agent_turn", turn)


def send(workspace, payload=None, *, stream=False):
    client, _, _, case, default = workspace
    return client.post(
        f"/api/tea/cases/{case['id']}/messages" + ("/stream" if stream else ""), json=payload or default
    )


def test_business_tools_parallel_and_ordered_with_idempotent_messages(workspace, monkeypatch):
    seen, completed = [], []
    original = AgentTools.guide
    barrier = threading.Barrier(2)

    def guide(self, args, attempt):
        barrier.wait(timeout=3)
        completed.append("guide")
        return original(self, args, attempt)

    original_handoff = AgentTools.handoff

    def handoff(self, args, attempt):
        barrier.wait(timeout=3)
        completed.append("handoff")
        return original_handoff(self, args, attempt)

    monkeypatch.setattr(AgentTools, "guide", guide)
    monkeypatch.setattr(AgentTools, "handoff", handoff)
    scenario(
        monkeypatch,
        [
            tools(("guide_business", {"reason": "确认当前办理进度"}), ("get_handoff", {})),
            tools(("check_materials", {})),
            "请先补充材料，再提交核验。",
        ],
        seen,
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    answer = response.json()["messages"][1]
    assert set(completed) == {"guide", "handoff"}
    assert [m["tool_call_id"] for m in seen[1][0] if m["role"] == "tool"] == [
        "call_guide_business_0",
        "call_get_handoff_1",
    ]
    assert {t["name"] for t in answer["trace"][0]["tools"]} >= {
        "guide_business",
        "get_handoff",
        "check_materials",
        "agent_turn",
    }
    assert send(workspace).json()["messages"] == response.json()["messages"]
    assert len(seen) == 3
    assert response.json()["case"]["completed_tasks"] == workspace[3]["completed_tasks"]


def test_skill_discovery_rag_and_web_are_composable_and_web_is_not_legal_evidence(workspace, monkeypatch):
    seen, requests = [], []

    async def gateway(name, payload, check, **options):
        requests.append((name, payload))
        return {
            "status": "completed",
            "output": {
                "summary": "官方公告",
                "sources": [
                    {"title": "官方公告", "url": "https://example.org/notice", "snippet": "公告原文"}
                ],
            },
        }

    monkeypatch.setattr("app.contexts.conversations.application.agent_tools.gateway_tool", gateway)
    monkeypatch.setattr(
        workspace[1],
        "search",
        lambda *a, **k: {"items": [], "mode": "unavailable", "warning": "RAGFlow 尚未配置。"},
    )
    scenario(
        monkeypatch,
        [
            tools(("load_skill", {"name": "research"}), ("load_skill", {"name": "materials"})),
            tools(
                ("web_search", {"query": "茶出口公告", "domains": ["example.org"]}),
                ("search_documents", {"query": "茶出口材料"}),
            ),
            "已检索官网；RAG 尚未就绪，无法确认法律要求。",
        ],
        seen,
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    assert requests[0][0] == "web_search"
    assert response.json()["messages"][1]["citations"] == []
    assert "web_search" in {d["function"]["name"] for d in seen[1][1]}
    assert "unreviewed_web" in str(seen[2][0])
    assert "https://example.org/notice" in str(seen[2][0])


def test_draft_and_explicit_memory_commit_once_with_messages(workspace, monkeypatch):
    client, service, owner, case, payload = workspace
    payload = {**payload, "content": "请起草文稿，并记住：每袋净含量需要核对。", "skills": ["draft"]}
    scenario(
        monkeypatch,
        [
            tools(("discover_tools", {"query": "remember_memory"})),
            tools(
                (
                    "draft_document",
                    {
                        "title": "包装说明",
                        "content": "# 包装说明\n\n净含量：【待补充】",
                        "task_id": case["stages"][0]["tasks"][0]["id"],
                    },
                ),
                (
                    "remember_memory",
                    {"content": "每袋净含量需要核对。", "source_quote": "每袋净含量需要核对。"},
                ),
            ),
            "已准备好草稿与笔记。",
        ],
    )
    response = send(workspace, payload)
    assert response.status_code == 200, response.text
    assert len(response.json()["case"]["artifacts"]) == 1
    note = service.agent_memory.list(owner, case["id"], payload["stage_id"], payload["conversation_id"])[
        "items"
    ][0]
    assert note["content"] == "每袋净含量需要核对。"
    assert note["source_message_id"] == response.json()["messages"][0]["id"]
    assert not note["stale"]
    assert "已新建文稿" in response.json()["messages"][1]["content"]
    assert send(workspace, payload).json()["messages"] == response.json()["messages"]


@pytest.mark.parametrize("failure", [StreamCancelled(), ToolFailure("timeout", "timeout")])
def test_partial_or_cancelled_turn_never_commits_draft_or_memory(workspace, monkeypatch, failure):
    _, service, owner, case, payload = workspace
    payload = {**payload, "content": "请起草文稿", "skills": ["draft"]}
    scenario(
        monkeypatch,
        [
            tools(
                (
                    "draft_document",
                    {"title": "未完成", "content": "# 正文", "task_id": case["stages"][0]["tasks"][0]["id"]},
                )
            ),
            failure,
        ],
    )
    with pytest.raises(type(failure)):
        service.post_message(owner, case["id"], payload)
    assert not service.case_detail(owner, case["id"])["artifacts"]
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT status FROM agent_runs").fetchone()[0] == "interrupted"
        assert (
            conn.execute("SELECT COUNT(*) FROM agent_tool_receipts WHERE status='completed'").fetchone()[0]
            == 1
        )
    with pytest.raises(AppError) as caught:
        service.post_message(owner, case["id"], payload)
    assert caught.value.code == "agent_request_not_replayable"


def test_unknown_tool_scope_injection_and_write_without_request_are_recoverable(workspace, monkeypatch):
    seen = []
    scenario(
        monkeypatch,
        [
            tools(
                ("run_shell", {"command": "no"}),
                ("check_materials", {"owner": "other"}),
                (
                    "draft_document",
                    {
                        "title": "未授权",
                        "content": "text",
                        "task_id": workspace[3]["stages"][0]["tasks"][0]["id"],
                    },
                ),
            ),
            "无法执行这些操作。",
        ],
        seen,
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    errors = [json.loads(m["content"])["error"] for m in seen[1][0] if m["role"] == "tool"]
    assert errors == ["unknown_tool", "invalid_input", "scope_or_version_conflict"]
    assert not response.json()["case"]["artifacts"]


def test_memory_api_revision_ownership_forgetting_and_chat_stability(workspace, monkeypatch):
    client, service, owner, case, payload = workspace
    path = f"/api/tea/cases/{case['id']}/stages/{payload['stage_id']}/memories"
    saved = client.post(path, json={"content": "核对包装方案", "kind": "note"}).json()
    assert not saved["stale"]
    patch = {"content": "核对新版包装", "kind": "note", "expected_revision": saved["revision"]}
    assert client.patch(path + "/" + saved["id"], json=patch).status_code == 200
    assert client.patch(path + "/" + saved["id"], json=patch).status_code == 409
    scenario(monkeypatch, ["好的。"])  # A chat-only revision must not stale business memory.
    assert send(workspace).status_code == 200
    assert not client.get(path).json()["items"][0]["stale"]
    with service.db.transaction() as conn:
        conn.execute("UPDATE cases SET product='新产品' WHERE id=?", (case["id"],))
    assert client.get(path).json()["items"][0]["stale"]
    assert client.delete(path + "/" + saved["id"] + "?expected_revision=1").status_code == 409
    assert client.delete(path + "/" + saved["id"] + "?expected_revision=2").status_code == 200
    assert (
        service.agent_memory.recall(
            owner, case["id"], payload["stage_id"], payload["conversation_id"], "包装"
        )["notes"]
        == []
    )
    with closing(service.db.connect()) as conn:
        assert (
            conn.execute("SELECT content FROM agent_memory_entries WHERE id=?", (saved["id"],)).fetchone()[0]
            == ""
        )
    client.post("/api/tea/auth/logout")
    client.post("/api/tea/auth/demo", json={})
    assert client.get(path).status_code == 404
    assert client.post(path, json={"content": "越权"}).status_code == 404


def test_memory_scope_and_long_history_summary_keep_source_ids(workspace):
    _, service, owner, case, payload = workspace
    other = service.node_workspace.create_conversation(
        owner, case["id"], payload["stage_id"], {"client_request_id": "other", "title": "other"}
    )["id"]
    service.agent_memory.save(
        owner,
        case["id"],
        payload["stage_id"],
        {"kind": "preference", "content": "会话私有偏好", "conversation_id": payload["conversation_id"]},
    )
    assert service.agent_memory.list(owner, case["id"], payload["stage_id"], other)["total"] == 0
    for index in range(18):
        service.post_message(
            owner,
            case["id"],
            {
                **payload,
                "client_message_id": f"long-{index}",
                "content": ("早先乌龙茶安排" if index == 0 else "普通讨论") + str(index),
            },
        )
    recalled = service.agent_memory.recall(
        owner, case["id"], payload["stage_id"], payload["conversation_id"], "乌龙茶"
    )
    assert recalled["discussions"][0]["question"].startswith("早先乌龙茶")
    summary = json.loads(recalled["session_summary"]["summary"])
    assert len(recalled["session_summary"]["summary"]) <= 8000
    assert all(entry["source_message_id"] for entry in summary)
    assert not recalled["session_summary"]["stale"]


def test_stale_case_cannot_commit_pending_writes(workspace, monkeypatch):
    _, service, _, case, payload = workspace

    def mutate_case():
        with service.db.transaction() as conn:
            service.touch(conn, case["id"])
        return "完成"

    scenario(monkeypatch, [mutate_case])
    response = send(workspace)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "agent_context_changed"


def test_durable_claim_blocks_second_worker_and_conflicting_payload(workspace):
    _, service, owner, case, payload = workspace
    prepared = service.prepare_message(owner, case["id"], payload)
    AgentJournal(service, owner, prepared["case"], payload, prepared["payload_hash"])
    with pytest.raises(AppError) as conflict:
        AgentJournal(service, owner, case, payload, "different")
    assert conflict.value.code == "idempotency_conflict"
    with pytest.raises(AppError) as busy:
        AgentJournal(service, owner, case, {**payload, "client_message_id": "second"}, "new")
    assert busy.value.code == "message_in_progress"


def test_streaming_trace_contract_and_catalog_expose_all_business_tools(workspace, monkeypatch):
    client, _, _, case, payload = workspace
    route = f"/api/tea/cases/{case['id']}/stages/{payload['stage_id']}/agent-tools"
    names = {item["name"] for item in client.get(route).json()["tools"]}
    assert names >= {
        "guide_business",
        "check_materials",
        "analyze_legal",
        "draft_document",
        "get_handoff",
        "web_search",
        "inspect_image",
    }
    scenario(monkeypatch, [tools(("check_materials", {})), "请补充材料。"])
    response = send(workspace, stream=True)
    frames = [frame for frame in response.text.split("\n\n") if frame.startswith("event:")]
    assert frames[-1].startswith("event: done"), response.text
    events = [json.loads(frame.split("data: ")[1]) for frame in frames if frame.startswith("event: step")]
    assert [e["sequence"] for e in events] == sorted(e["sequence"] for e in events)


def test_legal_tool_uses_existing_versioned_analysis_and_preserves_evidence_gaps(workspace, monkeypatch):
    _, service, owner, case, payload = workspace
    questions = []

    def engine(owner_id, template, stage, question, facts, **options):
        questions.append(question)
        return {
            "question": question,
            "assistant_mode": "live",
            "output": "证据不足，尚不能确认准入要求。",
            "gaps": ["缺少已审核法规证据"],
            "trace": [],
        }

    monkeypatch.setattr(service.legal, "execute_engine", engine)
    scenario(
        monkeypatch,
        [tools(("analyze_legal", {"question": "茶叶出口准入要求"})), "缺少审核依据，请补充法规来源。"],
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    assert questions == ["茶叶出口准入要求"]
    with closing(service.db.connect()) as conn:
        row = conn.execute("SELECT payload FROM legal_analyses WHERE case_id=?", (case["id"],)).fetchone()
    analysis = json.loads(row["payload"])
    assert analysis["status"] == "needs_review"
    assert analysis["result"]["gaps"] == ["缺少已审核法规证据"]
    assert service.case_detail(owner, case["id"])["completed_tasks"] == case["completed_tasks"]


def test_tool_error_can_be_repaired_but_identical_loop_is_bounded(workspace, monkeypatch):
    wrong = tools(("check_materials", {"task_ids": ["not-in-this-stage"]}))
    replies = []
    for index in range(3):
        reply = copy.deepcopy(wrong)
        reply["tool_calls"][0]["id"] = f"loop_{index}"
        replies.append(reply)
    scenario(monkeypatch, replies)
    with pytest.raises(ToolFailure) as caught:
        workspace[1].post_message(workspace[2], workspace[3]["id"], workspace[4])
    assert caught.value.code == "tool_loop"


def test_context_compaction_keeps_complete_tool_pairs():
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "original question"}]
    for index in range(12):
        call = tools(("search_documents", {"query": str(index)}))
        call["tool_calls"][0]["id"] = str(index)
        messages.extend([call, {"role": "tool", "tool_call_id": str(index), "content": "结果" * 6000}])
    compacted, changed = compact_messages(messages, budget=50000)
    assert changed and compacted[:2] == messages[:2]
    for index, message in enumerate(compacted):
        if message["role"] == "tool":
            assert compacted[index - 1]["tool_calls"][0]["id"] == message["tool_call_id"]
    assert len(messages[-1]["content"]) == 12000  # original transcript unchanged


def test_existing_database_migration_preserves_messages(workspace):
    _, service, owner, case, payload = workspace
    service.post_message(owner, case["id"], payload)
    with service.db.transaction() as conn:
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        for table in ("agent_tool_receipts", "agent_runs", "agent_memory_entries", "agent_session_summaries"):
            conn.execute("DROP TABLE " + table)
        conn.execute("DELETE FROM schema_migrations WHERE version IN (24,25)")
    Database(service.db.data_dir, require_existing=True)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == count
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 25


def test_image_recognition_uses_scoped_original_and_keeps_it_unverified(workspace, monkeypatch):
    _, service, owner, case, payload = workspace
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII="
    )
    doc = service.store_document(owner, "label.png", png, "image/png", case_id=case["id"])
    requests, seen = [], []

    async def gateway(name, body, check, **kwargs):
        requests.append((name, body))
        return {"status": "completed", "output": {"text": "图片太小，无法确认文字"}}

    monkeypatch.setattr("app.contexts.conversations.application.agent_tools.gateway_tool", gateway)
    scenario(
        monkeypatch,
        [
            tools(("load_skill", {"name": "vision"})),
            tools(
                ("inspect_image", {"document_id": doc["id"], "version": doc["version"], "prompt": "描述标签"})
            ),
            "图片文字不清晰。",
        ],
        seen,
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    assert requests[0][0] == "image_recognition"
    assert requests[0][1]["image"].startswith("data:image/png;base64,")
    assert response.json()["messages"][1]["citations"] == []
    assert "图片太小，无法确认文字" in str(seen[2][0])


def test_backup_restores_memory_and_receipts_without_replaying_uncertain_calls(workspace, tmp_path):
    _, service, owner, case, payload = workspace
    note = service.agent_memory.save(
        owner, case["id"], payload["stage_id"], {"content": "保存包装核对要求", "kind": "note"}
    )
    service.post_message(owner, case["id"], {**payload, "client_message_id": "summary-turn"})
    prepared = service.prepare_message(owner, case["id"], payload)
    journal = AgentJournal(service, owner, prepared["case"], payload, prepared["payload_hash"])
    journal.start_tool("done", "web_search", {"query": "公告"})
    journal.finish_tool("done", {"summary": "已取得的结果"})
    journal.start_tool("unknown", "inspect_image", {"document_id": "fixture"})
    backup, target = (
        tmp_path.parent / (tmp_path.name + "-backup"),
        tmp_path.parent / (tmp_path.name + "-restore"),
    )
    create_backup(service.db.data_dir, backup)
    assert verify_backup(backup)["status"] == "verified"
    receipt = restore_backup(backup, target)
    assert receipt["changes"]["tea.sqlite3"]["released_claims"]["agent_runs"] == 1
    with sqlite3.connect(target / "tea.sqlite3") as restored, closing(service.db.connect()) as original:
        assert (
            restored.execute("SELECT content FROM agent_memory_entries WHERE id=?", (note["id"],)).fetchone()[
                0
            ]
            == "保存包装核对要求"
        )
        assert (
            restored.execute("SELECT status FROM agent_runs WHERE id=?", (journal.id,)).fetchone()[0]
            == "interrupted"
        )
        assert (
            original.execute("SELECT status FROM agent_runs WHERE id=?", (journal.id,)).fetchone()[0]
            == "running"
        )
        receipts = restored.execute(
            "SELECT status,result FROM agent_tool_receipts ORDER BY call_id"
        ).fetchall()
        assert receipts[0][0] == "completed" and json.loads(receipts[0][1]) == {"summary": "已取得的结果"}
        assert receipts[1] == ("running", None)
        assert restored.execute("SELECT COUNT(*) FROM agent_session_summaries").fetchone()[0] == 1


def test_compacted_tool_results_can_be_read_without_repeating_provider(workspace, monkeypatch):
    seen = []
    scenario(
        monkeypatch,
        [
            tools(("guide_business", {})),
            tools(("read_tool_result", {"call_id": "call_guide_business_0", "offset": 0, "limit": 1000})),
            "已参考本轮办理进度。",
        ],
        seen,
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    recovered = json.loads(
        next(m["content"] for m in seen[2][0] if m.get("tool_call_id") == "call_read_tool_result_0")
    )
    assert recovered["call_id"] == "call_guide_business_0"
    assert recovered["text"]
    with closing(workspace[1].db.connect()) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM agent_tool_receipts WHERE name='guide_business'").fetchone()[0]
            == 1
        )


def test_agent_rag_returns_only_reviewed_current_destination_evidence(
    workspace, monkeypatch, ragflow_library
):
    from test_country_isolation import publish, stored

    _, service, owner, case, _ = workspace
    scope = service, owner, case["id"]
    approved = stored(scope, "ID", identity="approved", body=b"tea import regulation approved")
    stored(scope, "ID", identity="unreviewed", body=b"tea import unreviewed rule")
    other_country = stored(scope, "SG", identity="other-country", body=b"tea import foreign regulation")
    publish(scope, approved["id"])
    publish(scope, other_country["id"])
    seen = []
    scenario(
        monkeypatch,
        [
            tools(("load_skill", {"name": "materials"})),
            tools(("search_documents", {"query": "tea import"})),
            "已找到当前目的地的已审核原文。",
        ],
        seen,
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    citations = response.json()["messages"][1]["citations"]
    assert {item["document_id"] for item in citations} == {approved["id"]}
    assert all(item["document_version"] == approved["version"] and item["locator"] for item in citations)
    assert '"mode": "ragflow"' in str(seen[2][0])
