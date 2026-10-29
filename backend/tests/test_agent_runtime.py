"""End-to-end tool scheduling against isolated SQLite and controlled providers."""

import base64
import copy
import json
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest
from app.contexts.conversations.application import agent_loop
from app.contexts.conversations.application.agent_tools import AgentTools, LawReadInput, LegalSearchInput
from app.contexts.conversations.application.agent_journal import AgentJournal
from app.contexts.conversations.application.agent_loop import compact_messages
from app.contexts.conversations.application.agent_skills import (
    SKILLS,
    SKILL_ALIASES,
    read_skill,
    skill_catalog,
)
from app.contexts.conversations.application.tool_runtime import ToolFailure
from app.interfaces.http.factory import create_app
from app.shared.application.streaming import StreamCancelled
from app.shared.domain.errors import AppError
from app.shared.infrastructure.persistence.database import Database
from app.shared.infrastructure.operations.data_backup import create_backup, restore_backup, verify_backup
from fastapi.testclient import TestClient
from pydantic import ValidationError


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


def scenario(monkeypatch, replies, seen=None, *, auto_discover=True):
    """Script only actual model turns; no planner or final-editor injection."""
    monkeypatch.setenv("LLM_API_KEY", "fixture-only")
    monkeypatch.setenv("AGENT_MODEL", "gemini-3.8-flash")
    replies = iter(replies)
    pending = []

    async def turn(messages, definitions, delta, check, **kwargs):
        check()
        reply = pending.pop() if pending else next(replies)
        if callable(reply):
            reply = reply()
        if auto_discover and isinstance(reply, dict) and reply.get("tool_calls"):
            available = {d["function"]["name"] for d in definitions}
            missing = [
                c["function"]["name"] for c in reply["tool_calls"] if c["function"]["name"] not in available
            ]
            if missing:
                pending.append(reply)
                return tools(("tool_search", {"query": " ".join(missing)}))
        if seen is not None:
            seen.append((copy.deepcopy(messages), copy.deepcopy(definitions)))
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            delta(reply)
            return {"role": "assistant", "content": reply}
        if reply.get("content"):
            delta(reply["content"])
        return reply

    monkeypatch.setattr(agent_loop, "agent_turn", turn)


def last_run(workspace):
    with closing(workspace[1].db.connect()) as conn:
        return json.loads(
            conn.execute("SELECT result FROM agent_runs ORDER BY created_at DESC LIMIT 1").fetchone()[0]
        )


def stored_receipt(workspace, call_id):
    with closing(workspace[1].db.connect()) as conn:
        row = conn.execute(
            "SELECT result FROM agent_tool_receipts WHERE call_id=? ORDER BY updated_at DESC LIMIT 1",
            (call_id,),
        ).fetchone()
        assert row is not None, call_id
        return json.loads(row[0])


def send(workspace, payload=None, *, stream=False):
    client, _, _, case, default = workspace
    return client.post(
        f"/api/tea/cases/{case['id']}/messages" + ("/stream" if stream else ""), json=payload or default
    )


@pytest.mark.parametrize("values", [{}, {"record_id": "record", "url": "https://www.gov.cn/law"}])
def test_read_law_requires_exactly_one_source(values):
    with pytest.raises(ValidationError):
        LawReadInput(jurisdiction="CN", **values)


def legal_tools(workspace, check=lambda: None):
    _, service, owner, case, payload = workspace
    return AgentTools(service, owner, case, case["stages"][0], payload, {}, "tool-test", check)


@pytest.mark.parametrize("model_fails,stream", [(False, False), (False, True), (True, False), (True, True)])
def test_document_files_commit_with_answer_and_have_authenticated_downloads(
    workspace, monkeypatch, model_fails, stream
):
    import io
    import re

    from docx import Document
    from pypdf import PdfReader

    client, service, _, case, payload = workspace
    text = "# 催款函\n请支付货款8000元。\n收款账号：[待补充]"
    final = (
        ToolFailure("timeout", "受控模型超时")
        if model_fails
        else "催款函已准备。[下载](/api/tea/documents/新文件ID/download)"
    )
    scenario(
        monkeypatch,
        [
            tools(("load_skill", {"name": "document-operations"})),
            tools(
                ("create_document_file", {"filename": "催款函.docx", "format": "docx", "content": text}),
                ("create_document_file", {"filename": "催款函.pdf", "format": "pdf", "content": text}),
            ),
            final,
        ],
        auto_discover=False,
    )
    response = send(workspace, {**payload, "content": "生成一份Word催款函并提供PDF副本"}, stream=stream)
    assert response.status_code == 200, response.text
    with closing(service.db.connect()) as conn:
        files = conn.execute("SELECT * FROM documents WHERE case_id=?", (case["id"],)).fetchall()
        assert not conn.execute("SELECT 1 FROM processing_jobs").fetchone()
    assert len(files) == 2
    receipt = stored_receipt(workspace, "call_create_document_file_0")
    assert (
        receipt["validation"]["reopened"] and "storage_path" not in receipt and "file_base64" not in receipt
    )
    if model_fails:
        state = last_run(workspace)["_task_state"]
        assert state["delivery"] == "partially_answered"
        assert state["delivery_recovery"] == "prepared_files"
    saved = (
        next(value for event, value in stream_events(response) if event == "done")
        if stream
        else response.json()
    )
    assert "新文件ID" not in saved["messages"][1]["content"]
    urls = re.findall(r"\]\((/api/tea/documents/[^)]+/download)\)", saved["messages"][1]["content"])
    assert len(urls) == 2
    downloads = [client.get(url) for url in urls]
    assert all(d.status_code == 200 for d in downloads)
    assert Document(io.BytesIO(downloads[0].content)).paragraphs[1].text == "请支付货款8000元。"
    assert "8000" in PdfReader(io.BytesIO(downloads[1].content)).pages[0].extract_text()
    assert all(f["status"] == "stored" and f["version"] == 1 for f in files)
    client.post("/api/tea/auth/logout")
    client.post("/api/tea/auth/demo", json={})
    assert all(client.get(url).status_code == 404 for url in urls)


def test_document_edit_requires_inspection_and_preserves_original(workspace, monkeypatch):
    from app.contexts.conversations.application.agent_tools import FileEditInput, FileInspectInput
    from app.contexts.documents.application.document_files import commit_file, prepare_file

    client, service, owner, case, payload = workspace
    pending = prepare_file(
        service,
        owner,
        "原报价.docx",
        {"format": "docx", "content": "单价120元\n其余条款保持不变"},
        timeout=20,
    )
    with service.db.transaction() as conn:
        source = commit_file(service, conn, owner, case["id"], pending)
    current_case = client.get(f"/api/tea/cases/{case['id']}").json()
    scope = (
        client,
        service,
        owner,
        current_case,
        {**payload, "content": "修改报价单价为125元，保留其他文字"},
    )
    bound = legal_tools(scope)
    args = FileEditInput(
        document_id=source["id"],
        version=1,
        filename="修改报价.docx",
        replacements=[{"part": "word/document.xml", "old_text": "120元", "new_text": "125元"}],
    )
    attempt = SimpleNamespace(remaining=20, check=lambda: None)
    with pytest.raises(AppError) as error:
        bound.edit_file(args, attempt)
    assert error.value.code == "file_inspection_required"
    bound.inspect_file(FileInspectInput(document_id=source["id"], version=1), attempt)
    assert bound.edit_file(args, attempt)["status"] == "pending_commit"
    with pytest.raises(AppError) as error:
        bound.inspect_file(FileInspectInput(document_id=source["id"], version=2), attempt)
    assert error.value.code == "document_version_changed"
    scenario(
        monkeypatch,
        [
            tools(("load_skill", {"name": "document-operations"})),
            tools(("inspect_document_file", {"document_id": source["id"], "version": 1})),
            tools(("edit_document_file", args.model_dump(exclude={"purpose", "objective_ids"}))),
            "报价已修改，其他文字保留。",
        ],
        auto_discover=False,
    )
    response = send(workspace, scope[-1])
    assert response.status_code == 200, response.text
    with closing(service.db.connect()) as conn:
        files = conn.execute(
            "SELECT * FROM documents WHERE case_id=? ORDER BY created_at", (case["id"],)
        ).fetchall()
    assert len(files) == 2 and files[0]["content_hash"] == pending["content_hash"]
    download = client.get(f"/api/tea/documents/{files[1]['id']}/download")
    from app.contexts.documents.infrastructure.document_files import inspect

    texts = [e["text"] for e in inspect(download.content, "docx")["elements"]]
    assert texts == ["单价125元", "其余条款保持不变"]


def test_cancellation_does_not_commit_even_prepared_document_files(workspace, monkeypatch):
    _, service, owner, case, payload = workspace
    scenario(
        monkeypatch,
        [
            tools(
                ("create_document_file", {"filename": "取消.txt", "format": "txt", "content": "待保存正文"})
            ),
            StreamCancelled(),
        ],
    )
    with pytest.raises(StreamCancelled):
        service.post_message(owner, case["id"], {**payload, "content": "生成一个txt文件"})
    with closing(service.db.connect()) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM documents WHERE case_id=?", (case["id"],)).fetchone()[0] == 0
        )


def test_document_commit_rechecks_source_version(workspace):
    from app.contexts.documents.application.document_files import commit_file, prepare_file

    _, service, owner, case, _ = workspace
    source = prepare_file(service, owner, "原文.txt", {"format": "txt", "content": "原文"}, timeout=20)
    with service.db.transaction() as conn:
        commit_file(service, conn, owner, case["id"], source)
    with closing(service.db.connect()) as conn:
        doc = dict(conn.execute("SELECT * FROM documents WHERE id=?", (source["id"],)).fetchone())
    pending = prepare_file(
        service,
        owner,
        "副本.txt",
        {"format": "txt", "replacements": [{"old_text": "原文", "new_text": "修改", "expected_count": 1}]},
        source=doc,
        timeout=20,
    )
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET version=2 WHERE id=?", (doc["id"],))
    with pytest.raises(AppError) as error, service.db.transaction() as conn:
        commit_file(service, conn, owner, case["id"], pending)
    assert error.value.code == "file_context_changed"
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1


def test_skill_document_and_related_parameters_are_loaded_only_on_request(workspace, monkeypatch):
    seen = []
    scenario(
        monkeypatch,
        [tools(("load_skill", {"name": "business"})), "先比较客户来源与收款安排。"],
        seen,
        auto_discover=False,
    )
    response = send(workspace)
    assert response.status_code == 200
    assert {d["function"]["name"] for d in seen[0][1]} == agent_loop.COMMON_TOOLS
    assert "先确认经营目标、资源与限制" not in str(seen[0][0])
    assert "先确认经营目标、资源与限制" in str(seen[1][0])
    assert seen[1][1]  # the same main model may answer directly with tools available
    assert "web_search" in str(stored_receipt(workspace, "call_load_skill_0"))
    all_schemas = [t.schema() for t in legal_tools(workspace).registry.values()]
    assert len(seen[0][1]) < len(all_schemas)


@pytest.mark.parametrize(
    "skill_id",
    ["contract_review", "draft", "litigation_analysis", "long_image_to_pdf", "document_operations"],
)
def test_document_skill_loads_full_method_and_available_tools_on_request(workspace, monkeypatch, skill_id):
    seen = []
    instructions = read_skill(skill_id)
    scenario(
        monkeypatch,
        [tools(("load_skill", {"name": skill_id})), "请提供相关文稿。"],
        seen,
        auto_discover=False,
    )
    assert send(workspace).status_code == 200
    assert all(instructions not in message.get("content", "") for message in seen[0][0])
    loaded = next(
        json.loads(message["content"])
        for message in seen[1][0]
        if message.get("tool_call_id") == "call_load_skill_0"
    )
    assert loaded["skill"] == skill_id
    assert loaded["instructions"] == instructions
    expected = set(SKILLS[skill_id].tools)
    assert set(loaded["activate_tools"]) == expected
    assert {tool["function"]["name"] for tool in loaded["tools"]} == expected
    registry = legal_tools(workspace).registry
    assert loaded["tools"] == [registry[name].schema() for name in SKILLS[skill_id].tools]
    assert expected <= {tool["function"]["name"] for tool in seen[1][1]}
    assert stored_receipt(workspace, "call_load_skill_0")["instructions"] == instructions
    assert not workspace[1].case_detail(workspace[2], workspace[3]["id"])["artifacts"]


def test_tool_search_loads_one_requested_data_capability(workspace, monkeypatch):
    seen = []
    scenario(
        monkeypatch,
        [
            tools(("tool_search", {"query": "list_documents", "limit": 1})),
            tools(("list_documents", {})),
            "资料清单已取得。",
        ],
        seen,
        auto_discover=False,
    )
    response = send(workspace)
    assert response.status_code == 200
    assert {d["function"]["name"] for d in seen[1][1]} == agent_loop.COMMON_TOOLS | {"list_documents"}
    receipt = stored_receipt(workspace, "call_list_documents_0")
    assert receipt["total"] == 0


def test_unloaded_tool_is_rejected_without_executing_handler(workspace, monkeypatch):
    seen = []
    monkeypatch.setattr(AgentTools, "image", lambda *a: pytest.fail("unloaded tool executed"))
    scenario(monkeypatch, [tools(("inspect_image", {})), "需要先加载相关能力。"], seen, auto_discover=False)
    assert send(workspace).status_code == 200
    assert last_run(workspace)["_task_state"]["calls"][-1]["error"] == "unknown_tool"


def mcp_request(workspace, method, params=None, *, identifier=1):
    client, _, _, case, payload = workspace
    endpoint = f"/api/tea/cases/{case['id']}/stages/{payload['stage_id']}/mcp"
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if identifier is not None:
        body["id"] = identifier
    return client.post(
        endpoint,
        json=body,
        headers={"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-11-25"},
    )


def test_mcp_initialization_discovery_and_data_call_share_authorized_registry(workspace):
    initialized = mcp_request(
        workspace,
        "initialize",
        {"protocolVersion": "2025-11-25", "clientInfo": {"name": "test", "version": "1"}, "capabilities": {}},
    ).json()
    assert initialized["result"]["protocolVersion"] == "2025-11-25"
    assert mcp_request(workspace, "notifications/initialized", identifier=None).status_code == 202
    definitions = mcp_request(workspace, "tools/list").json()["result"]["tools"]
    schemas = {t["name"]: t for t in definitions}
    assert (
        schemas["read_document"]["inputSchema"]
        == legal_tools(workspace).registry["read_document"].schema()["function"]["parameters"]
    )
    assert "draft_document" not in schemas and "remember_memory" not in schemas
    result = mcp_request(workspace, "tools/call", {"name": "list_documents", "arguments": {}}).json()[
        "result"
    ]
    assert result["isError"] is False and result["structuredContent"]["total"] == 0
    with closing(workspace[1].db.connect()) as conn:
        receipt = conn.execute("SELECT name,status FROM agent_tool_receipts").fetchone()
        assert tuple(receipt) == ("list_documents", "completed")


@pytest.mark.parametrize(
    "skill_id",
    ["contract_review", "draft", "litigation_analysis", "long_image_to_pdf", "document_operations"],
)
def test_mcp_document_skill_limits_tools_to_authorized_registry(workspace, skill_id):
    from app.contexts.conversations.application.mcp_tools import EXPORTED_TOOLS

    result = mcp_request(
        workspace, "tools/call", {"name": "load_skill", "arguments": {"name": skill_id}}
    ).json()["result"]
    assert result["isError"] is False
    loaded = result["structuredContent"]
    assert loaded["skill"] == skill_id
    assert loaded["instructions"] == read_skill(skill_id)
    expected = set(SKILLS[skill_id].tools) & EXPORTED_TOOLS
    assert set(loaded["activate_tools"]) == expected
    assert {tool["function"]["name"] for tool in loaded["tools"]} == expected
    assert "draft_document" not in loaded["activate_tools"]
    assert "convert_long_image_to_pdf" not in loaded["activate_tools"]
    assert stored_receipt(workspace, "mcp-call")["instructions"] == loaded["instructions"]


@pytest.mark.parametrize("alias,target", SKILL_ALIASES.items())
def test_source_skill_alias_works_without_duplicate_catalog_or_writes(workspace, alias, target):
    result = mcp_request(
        workspace, "tools/call", {"name": "load_skill", "arguments": {"name": alias}}
    ).json()["result"]
    assert result["isError"] is False
    loaded = result["structuredContent"]
    assert loaded["skill"] == target
    assert loaded["requested_name"] == alias
    assert loaded["instructions"] == read_skill(target) == read_skill(alias)
    catalog = skill_catalog()
    assert len({s["id"] for s in catalog}) == len(SKILLS)
    assert alias in next(s["aliases"] for s in catalog if s["id"] == target)
    with closing(workspace[1].db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_skill_reference_is_explicit_and_cannot_read_arbitrary_file(workspace):
    def load(name, resource="SKILL.md"):
        return mcp_request(
            workspace,
            "tools/call",
            {"name": "load_skill", "arguments": {"name": name, "resource": resource}},
        ).json()["result"]

    entrance = load("litigation-analysis")["structuredContent"]
    assert entrance["resources"] == ["references/materials.md"]
    detail = load("litigation-analysis", entrance["resources"][0])["structuredContent"]
    assert detail["instructions"] == read_skill("litigation_analysis", "references/materials.md")
    assert detail["instructions"] not in entrance["instructions"]
    for resource in ("../../NOTICE.md", "/etc/passwd", "references/../SKILL.md", "missing.md"):
        assert load("litigation-analysis", resource)["isError"] is True
    assert load("unknown")["isError"] is True


@pytest.mark.parametrize("model_fails,stream", [(False, False), (False, True), (True, False), (True, True)])
def test_image_pdf_is_downloadable_only_after_successful_chat_commit(
    workspace, monkeypatch, model_fails, stream
):
    import hashlib
    import io
    import re

    from PIL import Image
    from pypdf import PdfReader

    _, service, owner, case, payload = workspace
    source = io.BytesIO()
    Image.new("RGB", (120, 5100), "white").save(source, format="PNG")
    original = source.getvalue()
    doc = service.store_document(owner, "chat.png", original, "image/png", case_id=case["id"])
    with closing(service.db.connect()) as conn:
        before = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone())
    reply = ToolFailure("timeout", "受控模型超时") if model_fails else "截图已按顺序分页。"
    scenario(
        monkeypatch,
        [
            tools(("load_skill", {"name": "long-image-to-pdf"})),
            tools(("convert_long_image_to_pdf", {"document_id": doc["id"], "version": doc["version"]})),
            reply,
        ],
        auto_discover=False,
    )
    response = send(workspace, {**payload, "content": "请把长截图转成 PDF"}, stream=stream)
    assert response.status_code == 200, response.text
    receipt = stored_receipt(workspace, "call_convert_long_image_to_pdf_0")
    assert receipt["status"] == "pending_commit"
    assert receipt["page_count"] == 1 and receipt["slice_count"] == 3
    assert "storage_path" not in receipt and "pdf_base64" not in receipt
    with closing(service.db.connect()) as conn:
        after = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone())
        children = conn.execute("SELECT * FROM documents WHERE parent_id=?", (doc["id"],)).fetchall()
    assert before == after
    assert hashlib.sha256(original).hexdigest() == after["content_hash"]
    assert len(children) == 1
    if model_fails:
        state = last_run(workspace)["_task_state"]
        assert state["delivery"] == "partially_answered"
        assert state["delivery_recovery"] == "prepared_files"
    if stream:
        events = stream_events(response)
        assert not [value for name, value in events if name == "error"]
        saved = next(value for name, value in events if name == "done")
        assert any(
            tool["name"] == "convert_long_image_to_pdf"
            for name, value in events
            if name == "step"
            for tool in value.get("tools", [])
        )
    else:
        saved = response.json()
    url = re.search(r"\]\((/api/tea/documents/[^)]+/download)\)", saved["messages"][1]["content"])[1]
    download = workspace[0].get(url)
    assert download.status_code == 200
    assert len(PdfReader(io.BytesIO(download.content)).pages) == 1
    assert children[0]["role"] == "secondary" and children[0]["status"] == "stored"
    with closing(service.db.connect()) as conn:
        assert not conn.execute(
            "SELECT 1 FROM processing_jobs WHERE document_id=?", (children[0]["id"],)
        ).fetchone()
    workspace[0].post("/api/tea/auth/logout")
    workspace[0].post("/api/tea/auth/demo", json={})
    assert workspace[0].get(url).status_code == 404


def test_image_pdf_rejects_other_business_and_unrequested_conversion(workspace):
    from app.contexts.conversations.application.agent_tools import ImagePDFInput

    bound = legal_tools(workspace)
    args = ImagePDFInput(document_id="not-visible", version=1)
    attempt = SimpleNamespace(remaining=20, check=lambda: None)
    with pytest.raises(AppError) as error:
        bound.image_pdf(args, attempt)
    assert error.value.code == "conversion_not_requested"
    bound.payload = {**bound.payload, "content": "转成 PDF"}
    with pytest.raises(AppError) as error:
        bound.image_pdf(args, attempt)
    assert error.value.status == 404


def test_mcp_rejects_scope_injection_unknown_tools_and_foreign_account(workspace):
    result = mcp_request(
        workspace, "tools/call", {"name": "list_documents", "arguments": {"owner": "foreign"}}
    ).json()["result"]
    assert result["isError"] is True
    assert (
        mcp_request(workspace, "tools/call", {"name": "run_sql", "arguments": {"sql": "SELECT 1"}}).json()[
            "error"
        ]["code"]
        == -32602
    )
    workspace[0].post("/api/tea/auth/logout")
    assert mcp_request(workspace, "tools/list").status_code == 401
    workspace[0].post("/api/tea/auth/demo", json={})
    assert mcp_request(workspace, "tools/list").status_code == 404


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_mcp_rejects_untrusted_origin_on_every_transport_method(workspace, method):
    client, _, _, case, payload = workspace
    endpoint = f"/api/tea/cases/{case['id']}/stages/{payload['stage_id']}/mcp"
    assert (
        client.request(method, endpoint, headers={"Origin": "https://untrusted.example"}).status_code == 403
    )


def test_read_law_official_source_returns_saved_original_with_citation_identity(workspace, monkeypatch):
    from app.contexts.legal_analysis.infrastructure import official_evidence

    text = "第一条 经营者应当遵守相关法律规定，依法办理本条列举的事项，并按规定保存相关证明文件供主管部门查验。\n第二条 符合本条例外条件的，可以依照例外规定处理，不能省略这些条件。"
    body = (
        "<html><body><h1>测试法规</h1><p>" + text.replace("\n", "</p><p>") + "</p></body></html>"
    ).encode()
    monkeypatch.setattr(
        official_evidence,
        "_download",
        lambda *a, **kw: {"url": "https://www.gov.cn/law", "mime_type": "text/html", "blob": body},
    )
    result = legal_tools(workspace).read_law(
        LawReadInput(jurisdiction="CN", url="https://www.gov.cn/law", query="例外"),
        SimpleNamespace(check=lambda: None),
    )
    assert result["mode"] == "official_web" and result["items"]
    assert any("不能省略" in item["excerpt"] for item in result["items"])
    for item in result["items"]:
        assert item["id"].startswith(item["document_id"] + ":")
        assert len(item["locator"]["source_hash"]) == 64
        assert item["metadata"]["review_status"] == "draft"


@pytest.mark.parametrize("tool_name", ["read_law", "legal_search"])
def test_legal_tools_reject_unconfigured_country_before_acquisition(workspace, monkeypatch, tool_name):
    from app.contexts.legal_analysis.infrastructure import official_evidence

    bound = legal_tools(workspace)
    monkeypatch.setattr(bound.service.legal, "allowed_countries", lambda *a: ["CN"])
    monkeypatch.setattr(bound.service.legal, "acquire_evidence", lambda *a, **kw: pytest.fail("out of scope"))
    monkeypatch.setattr(official_evidence, "fetch_official", lambda *a, **kw: pytest.fail("out of scope"))
    args = (
        LawReadInput(jurisdiction="SG", url="https://sso.agc.gov.sg/test")
        if tool_name == "read_law"
        else LegalSearchInput(jurisdiction="SG", query="food")
    )
    with pytest.raises(AppError) as caught:
        getattr(bound, tool_name)(args, SimpleNamespace(check=lambda: None))
    assert caught.value.code == "country_mismatch"


def test_read_law_rejects_non_official_url_before_download(workspace, monkeypatch):
    from app.contexts.legal_analysis.infrastructure import official_evidence

    monkeypatch.setattr(official_evidence, "_download", lambda *a, **kw: pytest.fail("untrusted host"))
    with pytest.raises(AppError) as caught:
        legal_tools(workspace).read_law(
            LawReadInput(jurisdiction="CN", url="https://example.com/law"),
            SimpleNamespace(check=lambda: None),
        )
    assert caught.value.code == "official_source_required"


def test_read_law_cancellation_stops_before_download(workspace, monkeypatch):
    from app.contexts.legal_analysis.infrastructure import official_evidence

    monkeypatch.setattr(official_evidence, "fetch_official", lambda *a, **kw: pytest.fail("cancelled"))
    with pytest.raises(StreamCancelled):
        legal_tools(workspace).read_law(
            LawReadInput(jurisdiction="CN", url="https://www.gov.cn/law"),
            SimpleNamespace(check=lambda: (_ for _ in ()).throw(StreamCancelled())),
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
    current_context = next(
        json.loads(m["content"])
        for m in seen[0][0]
        if m.get("role") == "user" and '"business_context"' in m.get("content", "")
    )
    assert "stage" not in current_context and "selected_task" not in current_context
    assert {t["function"]["name"] for t in seen[0][1]} >= {"search_legal_evidence", "tool_search"}
    guide_schema = next(t for t in seen[1][1] if t["function"]["name"] == "guide_business")
    assert "一般商业或法律咨询不调用" in guide_schema["function"]["description"]
    assert "进入条件" in guide_schema["function"]["description"]
    assert [
        m["tool_call_id"]
        for m in seen[1][0]
        if m["role"] == "tool" and not m["tool_call_id"].startswith("call_tool_search")
    ] == [
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
    handoff = json.loads(
        next(m["content"] for m in seen[1][0] if m.get("tool_call_id") == "call_get_handoff_1")
    )
    current, following = workspace[3]["stages"][:2]
    handoff = stored_receipt(workspace, "call_get_handoff_1")
    assert handoff["current_stage_id"] == current["id"]
    assert handoff["next_stage"] == {key: following[key] for key in ("id", "name", "status")}
    assert handoff["next_stage"]["id"] not in {t["id"] for t in current["tasks"]}
    handoff = stored_receipt(workspace, "call_get_handoff_1")
    checks = stored_receipt(workspace, "call_check_materials_0")["checks"]
    assert {check["task_title"] for check in checks} == {t["title"] for t in current["tasks"]}


@pytest.mark.parametrize("stream", [False, True])
def test_skill_discovery_rag_and_web_are_composable_and_web_is_not_legal_evidence(
    workspace, monkeypatch, stream
):
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
                (
                    "web_search",
                    {
                        "query": "茶出口公告",
                        "domains": ["example.org"],
                        "purpose": "取得公告",
                        "objective_ids": ["law"],
                    },
                ),
                ("search_documents", {"query": "茶出口材料"}),
            ),
            "已检索官网；RAG 尚未就绪，无法确认法律要求。",
        ],
        seen,
    )
    response = send(workspace, stream=stream)
    assert response.status_code == 200, response.text
    assert requests[0][0] == "web_search"
    assert "purpose" not in requests[0][1] and "objective_ids" not in requests[0][1]
    data = (
        next(data for event, data in stream_events(response) if event == "done")
        if stream
        else response.json()
    )
    assert len(data["messages"][1]["citations"]) == 1
    assert "web_search" in {d["function"]["name"] for d in seen[1][1]}
    assert "unreviewed_web" not in str(seen[2][0])
    assert "https://example.org/notice" in str(seen[2][0])
    previews = {t["name"]: t.get("output_preview") for t in data["messages"][1]["trace"][0]["tools"]}
    assert previews["web_search"]["text"] == "官方公告"
    assert previews["web_search"]["items"][0] == {
        "label": "官方公告",
        "text": "公告原文",
        "url": "https://example.org/notice",
    }
    assert previews["search_documents"]["notice"] == "RAGFlow 尚未配置。"
    assert previews["load_skill"] is None


def test_draft_and_explicit_memory_commit_once_with_messages(workspace, monkeypatch):
    client, service, owner, case, payload = workspace
    payload = {**payload, "content": "请起草文稿，并记住：每袋净含量需要核对。", "skills": ["draft"]}
    scenario(
        monkeypatch,
        [
            tools(("tool_search", {"query": "remember_memory"})),
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
    assert "可在“记忆”" not in response.json()["messages"][1]["content"]
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
    if isinstance(failure, ToolFailure):
        with pytest.raises(AppError):
            service.post_message(owner, case["id"], payload)
        assert last_run(workspace)["_task_state"]["delivery"] == "execution_failed"
    else:
        with pytest.raises(type(failure)):
            service.post_message(owner, case["id"], payload)
    assert not service.case_detail(owner, case["id"])["artifacts"]
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT status FROM agent_runs").fetchone()[0] == (
            "completed" if isinstance(failure, ToolFailure) else "interrupted"
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM agent_tool_receipts WHERE status='completed' AND name='draft_document'"
            ).fetchone()[0]
            == 1
        )
    if isinstance(failure, StreamCancelled):
        with pytest.raises(AppError) as caught:
            service.post_message(owner, case["id"], payload)
        assert caught.value.code == "agent_request_not_replayable"
    else:
        with pytest.raises(AppError):
            service.post_message(owner, case["id"], payload)


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
        auto_discover=False,
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    errors = [
        c["error"]
        for c in last_run(workspace)["_task_state"]["calls"]
        if c["tool"] not in {"search_legal_evidence", "tool_search"}
    ]
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
    history = service.agent_memory.model_history(
        owner, case["id"], payload["stage_id"], payload["conversation_id"]
    )
    assert len(history) == 36
    assert history[0]["content"].startswith("早先乌龙茶安排")
    assert service.agent_memory.model_history(owner, case["id"], payload["stage_id"], other) == []
    bounded = service.agent_memory.model_history(
        owner, case["id"], payload["stage_id"], payload["conversation_id"], budget=4000
    )
    assert len(json.dumps(bounded, ensure_ascii=False).encode()) < 4200
    assert not bounded or bounded[0]["role"] == "user"
    with pytest.raises(AppError):
        service.agent_memory.model_history(
            "another-owner", case["id"], payload["stage_id"], payload["conversation_id"]
        )


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
        "get_law_versions",
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
    snapshots = [t for event in events for t in event.get("tools", []) if t["name"] == "check_materials"]
    assert "output_preview" not in snapshots[0]
    assert snapshots[-1]["output_preview"]["items"]
    done = json.loads(frames[-1].split("data: ")[1])
    saved = next(t for t in done["messages"][1]["trace"][0]["tools"] if t["name"] == "check_materials")
    assert saved["output_preview"] == snapshots[-1]["output_preview"]


def test_tool_error_can_be_repaired_but_identical_loop_is_bounded(workspace, monkeypatch):
    wrong = tools(("check_materials", {"task_ids": ["not-in-this-stage"]}))
    replies = []
    for index in range(2):
        reply = copy.deepcopy(wrong)
        reply["tool_calls"][0]["id"] = f"loop_{index}"
        replies.append(reply)
    seen = []
    scenario(monkeypatch, [*replies, "材料核对未完成，仍可按已知事实解释准备顺序。"], seen)
    response = send(workspace)
    assert response.status_code == 200
    assert "材料核对未完成" in response.json()["messages"][1]["content"]
    assert seen[-1][1] == []
    with closing(workspace[1].db.connect()) as conn:
        state = json.loads(conn.execute("SELECT result FROM agent_runs").fetchone()[0])["_task_state"]
    assert [c for c in state["calls"] if c["tool"] not in {"tool_search", "update_task_brief"}][1][
        "reused_result"
    ] is True


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
    assert all("excerpt" not in message["content"] for message in compacted if message["role"] == "tool")


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
        assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 28


@pytest.mark.parametrize("image_provider", ["openai", "sub2api"])
def test_image_recognition_uses_scoped_original_and_keeps_it_unverified(
    workspace, monkeypatch, image_provider
):
    monkeypatch.setenv("VISION_OCR_PROVIDER", image_provider)
    monkeypatch.setenv("VISION_OCR_REQUEST_WORKERS", "48")
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
    output = next(
        t["output_preview"]
        for t in response.json()["messages"][1]["trace"][0]["tools"]
        if t["name"] == "inspect_image"
    )
    assert output["text"] == "图片太小，无法确认文字"
    assert "data:image" not in str(output)


def test_image_page_placeholder_is_correctable_without_sending_a_paid_request(workspace, monkeypatch):
    _, service, owner, case, _ = workspace
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN1cAAAAASUVORK5CYII="
    )
    doc = service.store_document(owner, "label.png", png, "image/png", case_id=case["id"])
    args = {"document_id": doc["id"], "version": doc["version"], "prompt": "描述标签"}
    requests, seen = [], []

    async def gateway(name, body, check, **kwargs):
        requests.append(name)
        return {"status": "completed", "output": {"text": "图像太小，无法确认文字。"}}

    monkeypatch.setattr("app.contexts.conversations.application.agent_tools.gateway_tool", gateway)
    corrected = tools(("inspect_image", args))
    corrected["tool_calls"][0]["id"] = "corrected-image"
    scenario(
        monkeypatch,
        [tools(("inspect_image", args | {"image_id": "none"})), corrected, "无法确认文字。"],
        seen,
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    failure = stored_receipt(workspace, "call_inspect_image_0")
    assert failure["error"] == "invalid_input"
    assert failure["validation"][0]["loc"] == ["image_id"]
    assert "JSON null" in failure["validation"][0]["msg"]
    assert requests == ["image_recognition"]
    assert response.json()["messages"][1]["content"].startswith("无法确认文字。")


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
    recovered = stored_receipt(workspace, "call_read_tool_result_0")
    assert recovered["call_id"] == "call_guide_business_0"
    assert recovered["text"]
    with closing(workspace[1].db.connect()) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM agent_tool_receipts WHERE name='guide_business'").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("stream", [False, True])
def test_agent_rag_returns_only_reviewed_current_destination_evidence(
    workspace, monkeypatch, ragflow_library, stream
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
            "已找到当前目的地的已审核原文。[S1]",
        ],
        seen,
    )
    response = send(workspace, stream=stream)
    assert response.status_code == 200, response.text
    data = (
        next(data for event, data in stream_events(response) if event == "done")
        if stream
        else response.json()
    )
    citations = data["messages"][1]["citations"]
    assert {item["document_id"] for item in citations} == {approved["id"]}
    assert all(item["document_version"] == approved["version"] and item["locator"] for item in citations)
    assert '"mode": "ragflow"' in str(seen[2][0])
    if stream:
        states = [
            tool
            for event, data in stream_events(response)
            if event == "step"
            for tool in data.get("tools", [])
            if tool["name"] == "search_documents"
        ]
        assert states[0]["status"] == "running"
        assert states[-1]["status"] == "completed"
        assert states[-1]["output_preview"]["items"][0]["text"] == citations[0]["excerpt"]


def stream_events(response):
    return [
        (part.splitlines()[0][7:], json.loads(part.split("data: ", 1)[1]))
        for part in response.text.split("\n\n")
        if part.startswith("event:")
    ]


def test_tool_planning_text_stays_out_of_final_answer_and_replays_exactly(workspace, monkeypatch):
    first = tools(("check_materials", {}))
    first["content"] = "我先核对现有材料。"
    scenario(monkeypatch, [first, tools(("guide_business", {})), "请补充主体信息。"])
    response = send(workspace, stream=True)
    events = stream_events(response)
    assert [data for event, data in events if event == "start"][-1] == {"step_id": "agent", "append": False}
    actual = "".join(data["text"] for event, data in events if event == "delta")
    assert actual == "请补充主体信息。"
    saved = next(data for event, data in events if event == "done")
    assert saved["messages"][1]["content"] == actual
    assert send(workspace).json()["messages"] == saved["messages"]


def test_summary_deltas_have_call_identity_and_are_saved_separately(workspace, monkeypatch):
    monkeypatch.setenv("AGENT_API_KEY", "fixture-only")

    async def turn(messages, definitions, delta, check, **options):
        options["on_summary"]("先核对")
        options["on_summary"]("现有资料。")
        delta("请补充材料。")
        return {"role": "assistant", "content": "请补充材料。"}

    monkeypatch.setattr(agent_loop, "agent_turn", turn)
    events = stream_events(send(workspace, stream=True))
    chunks = [data for event, data in events if event == "tool_delta"]
    assert len(chunks) >= 2 and len(chunks) % 2 == 0
    assert all(
        [c["text"] for c in chunks[i : i + 2]] == ["先核对", "现有资料。"] for i in range(0, len(chunks), 2)
    )
    assert all(a["sequence"] < b["sequence"] for a, b in zip(chunks, chunks[1:]))
    answer = next(data for event, data in events if event == "done")["messages"][1]
    assert answer["content"] == "请补充材料。"
    assert all(c["tool_id"] in {t["id"] for t in answer["trace"][0]["tools"]} for c in chunks)
    assert "先核对现有资料。" not in answer["content"]


def test_answer_limit_is_shared_across_model_rounds_and_failure_does_not_cache(workspace, monkeypatch):
    first = tools(("check_materials", {})) | {"content": "甲" * 11000}
    scenario(monkeypatch, [first, "乙" * 21000])
    events = stream_events(send(workspace, stream=True))
    assert events[-1][0] == "error"
    result = last_run(workspace)
    assert len(result["output"]) <= 20000
    assert result["_task_state"]["delivery"] == "execution_failed"
    assert "乙" * 21000 not in result["output"]


def test_context_migration_is_additive_and_cache_rejects_foreign_or_stale_anchor(workspace, monkeypatch):
    from app.contexts.conversations.application.conversation_context import read_context

    client, service, owner, case, payload = workspace
    scenario(monkeypatch, ["缓存回复。"])
    assert send(workspace).status_code == 200
    from app.contexts.conversations.application.conversation_context import save_context

    with service.db.transaction() as conn:
        anchor = conn.execute(
            "SELECT id FROM messages WHERE role='assistant' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()[0]
        save_context(
            conn,
            owner,
            case["id"],
            payload["stage_id"],
            payload["conversation_id"],
            anchor,
            {"signature": "legacy", "messages": [{"role": "assistant", "content": "缓存回复。"}]},
        )
    with closing(service.db.connect()) as conn:
        row = conn.execute("SELECT * FROM agent_conversation_context").fetchone()
        count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    args = (service, owner, case["id"], payload["stage_id"], payload["conversation_id"])
    assert read_context(*args, row["signature"])
    assert read_context(*args, "changed-provider-or-context") is None
    assert read_context(*args, row["signature"], budget=1) is None
    with pytest.raises(AppError):
        read_context(service, "foreign-owner", *args[2:], row["signature"])
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE agent_conversation_context SET source_message_id=(SELECT id FROM messages WHERE role='user' LIMIT 1)"
        )
    assert read_context(*args, row["signature"]) is None
    with service.db.transaction() as conn:
        conn.execute("DROP TABLE agent_conversation_context")
        conn.execute("DELETE FROM schema_migrations WHERE version=27")
    Database(service.db.data_dir, require_existing=True)
    Database(service.db.data_dir, require_existing=True)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == count
        assert conn.execute("SELECT COUNT(*) FROM agent_conversation_context").fetchone()[0] == 0


def test_simple_business_comparison_does_not_require_a_professional_tool(workspace, monkeypatch):
    scenario(monkeypatch, ["首次试水可先用当地经销商，比较获客成本、回款和渠道控制后再决定自营。"])
    response = send(workspace, {**workspace[4], "content": "六堡茶出海先找经销商还是开网店？"})
    assert response.status_code == 200
    names = [t["name"] for t in response.json()["messages"][1]["trace"][0]["tools"]]
    assert "analyze_legal" not in names
    assert last_run(workspace)["_task_state"]["finalized"]


@pytest.mark.parametrize("answer", ["进口人承担申报义务。[S1]", "进口人承担申报义务。"])
def test_final_answer_preserves_actual_source_when_model_uses_handle_or_omits_link(
    workspace, monkeypatch, answer
):
    seen = []
    citation = {
        "id": "source-import",
        "document_id": "import-law",
        "document_version": 1,
        "title": "测试进口规定",
        "source_url": "https://example.gov/import",
        "excerpt": "进口人应当申报。",
        "country": "ID",
        "locator": {"article": "3"},
    }
    monkeypatch.setattr(
        AgentTools, "legal_search", lambda *args: {"items": [citation], "mode": "official_web"}
    )
    scenario(
        monkeypatch,
        [tools(("search_legal_evidence", {"jurisdiction": "ID", "query": "进口人申报"})), answer],
        seen,
    )
    response = send(workspace)
    assert response.status_code == 200
    output = response.json()["messages"][1]["content"]
    assert ("[测试进口规定（第3条）](https://example.gov/import)" in output) == ("[S1]" in answer)
    assert bool(last_run(workspace)["citations"]) == ("[S1]" in answer)
    assert "[S1]" not in output
    assert "进口人承担申报义务。" in output
    with closing(workspace[1].db.connect()) as conn:
        saved = conn.execute(
            "SELECT content FROM messages WHERE id=?", (response.json()["messages"][1]["id"],)
        ).fetchone()[0]
    assert saved == output


def test_provider_length_stop_composes_once_without_repeating_tools(workspace, monkeypatch):
    seen = []
    scenario(
        monkeypatch,
        [
            tools(("guide_business", {})),
            ToolFailure("generation_truncated", "known length stop"),
            "保留已核对的进度，下一步补齐材料。",
        ],
        seen,
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    assert any(tool["function"]["name"] == "lookup_chinese_law" for tool in seen[-1][1])
    assert "guide_business" in str(seen[-1][0])
    assert any(m.get("role") == "tool" for m in seen[-1][0])
    answer = response.json()["messages"][1]
    assert answer["content"] == "保留已核对的进度，下一步补齐材料。"
    assert [t["name"] for t in answer["trace"][0]["tools"]].count("guide_business") == 1


@pytest.mark.parametrize("code", ["generation_truncated", "connection_failed"])
def test_delivery_recovery_is_bounded_and_never_retries_unknown_network_results(workspace, monkeypatch, code):
    seen = []
    scenario(monkeypatch, [ToolFailure(code, "failure"), ToolFailure(code, "failure")], seen)
    response = send(workspace)
    assert response.status_code == 502
    assert last_run(workspace)["_task_state"]["delivery"] == "execution_failed"
    assert len(last_run(workspace)["_task_state"]["failures"]) <= 2


def test_repeated_read_reuses_receipt_even_when_purpose_changes(workspace, monkeypatch):
    actual, seen = [], []

    def web(self, args, attempt):
        actual.append(args.query)
        return {"sources": [{"url": "https://example.com/a", "title": "来源", "excerpt": "同一内容"}]}

    monkeypatch.setattr(AgentTools, "web", web)
    first = tools(("web_search", {"query": "市场", "purpose": "查渠道"}))
    second = tools(("web_search", {"query": "市场", "purpose": "核对渠道"}))
    second["tool_calls"][0]["id"] = "another-call"
    scenario(monkeypatch, [first, second, "按已有渠道信息比较方案。"], seen)
    response = send(workspace)
    assert response.status_code == 200
    assert actual == ["市场"]
    returned = stored_receipt(workspace, "another-call")
    assert returned["reused_result"] is True
    assert last_run(workspace)["_task_state"]["calls"][-1]["reused_result"] is True


def test_same_batch_duplicate_reads_share_one_external_receipt(workspace, monkeypatch):
    actual = []

    def web(self, args, attempt):
        actual.append(args.query)
        return {"sources": [{"url": "https://example.com/a", "excerpt": "渠道事实"}]}

    monkeypatch.setattr(AgentTools, "web", web)
    scenario(
        monkeypatch,
        [
            tools(
                ("web_search", {"query": "市场", "purpose": "查渠道"}),
                ("web_search", {"query": "市场", "purpose": "核对渠道", "domains": []}),
            ),
            "按取得的渠道资料比较方案。",
        ],
    )
    response = send(workspace)
    assert response.status_code == 200, response.text
    assert actual == ["市场"]


def test_repeated_sources_end_research_without_losing_other_deliverables(workspace, monkeypatch):
    seen = []
    monkeypatch.setattr(
        AgentTools,
        "web",
        lambda *args: {"sources": [{"url": "https://example.com/a", "excerpt": "相同原文"}]},
    )
    rounds = [tools(("web_search", {"query": query})) for query in ("市场", "渠道")]
    for index, reply in enumerate(rounds):
        reply["tool_calls"][0]["id"] = f"research-{index}"
    scenario(monkeypatch, [*rounds, "已有资料可支持模式比较，具体报价尚未取得。"], seen)
    response = send(workspace)
    assert response.status_code == 200, response.text
    assert seen[-1][1] == []
    assert "具体报价尚未取得" in response.json()["messages"][1]["content"]


def test_followup_source_handles_preserve_prior_mapping_without_appending_old_links():
    from app.contexts.conversations.application.answer_sources import AnswerSources

    history = [
        {
            "role": "tool",
            "content": json.dumps(
                {
                    "sources": [
                        {
                            "id": "old",
                            "reference": "S7",
                            "title": "旧来源",
                            "source_url": "https://example.gov/old",
                        }
                    ]
                }
            ),
        }
    ]
    sources = AnswerSources(history)
    sources.collect({"items": [{"id": "new", "title": "新来源", "source_url": "https://example.gov/new"}]})
    assert sources.handles == {"old": "S7", "new": "S8"}
    assert "https://example.gov/old" in sources.render("沿用该依据[S7]。")
    answer = sources.render("本次新依据[S8]。")
    assert "https://example.gov/new" in answer
    assert "https://example.gov/old" not in answer


def test_composition_cited_source_excludes_unused_search_candidates():
    from app.contexts.conversations.application.answer_sources import AnswerSources

    sources = AnswerSources()
    sources.collect(
        {
            "items": [
                {"id": "used", "title": "适用依据", "source_url": "https://example.gov/used"},
                {"id": "unused", "title": "候选资料", "source_url": "https://example.gov/unused"},
            ]
        }
    )
    answer = sources.render("本项判断依据[S1]。")
    assert "https://example.gov/used" in answer
    assert "https://example.gov/unused" not in answer


@pytest.mark.parametrize(
    "wrapper",
    ["[S1](https://wrong.example/a)", "[[S1]](https://wrong.example/a)", "[S1, S2]", "S1", "【S1】"],
)
def test_citation_handles_replace_entire_markdown_wrapper_and_deduplicate(wrapper):
    from app.contexts.conversations.application.answer_sources import AnswerSources

    sources = AnswerSources()
    sources.collect(
        {
            "items": [
                {"id": "a", "title": "官方说明", "source_url": "https://example.gov/a"},
                {"id": "b", "title": "官方说明", "source_url": "https://example.gov/a"},
            ]
        }
    )
    rendered = sources.render(f"依据{wrapper}[S2]。")
    assert rendered == "依据[官方说明](https://example.gov/a)。"
    assert "wrong.example" not in rendered


def test_response_deadline_returns_recoverable_error_without_committing_answer(workspace, monkeypatch):
    clock = {"seconds": 0}
    monkeypatch.setattr(agent_loop, "monotonic", lambda: clock["seconds"])
    monkeypatch.setenv("AGENT_API_KEY", "fixture-only")

    async def delayed(*args, **options):
        clock["seconds"] = 181
        return {"role": "assistant", "content": "不应保存的超时答复"}

    monkeypatch.setattr(agent_loop, "agent_turn", delayed)
    events = stream_events(send(workspace, stream=True))
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "response_timeout"
    assert "总时间限制" in events[-1][1]["message"]
    with closing(workspace[1].db.connect()) as conn:
        assert conn.execute("SELECT status FROM agent_runs").fetchone()[0] == "interrupted"
        assert (
            conn.execute("SELECT COUNT(*) FROM messages WHERE content=?", ("不应保存的超时答复",)).fetchone()[
                0
            ]
            == 0
        )
