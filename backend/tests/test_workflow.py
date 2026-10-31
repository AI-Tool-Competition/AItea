import pytest
from admin_helpers import grant_admin, login_admin
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("sample_workflow")


@pytest.fixture
def client(tmp_path):
    from app.interfaces.http.factory import create_app

    with TestClient(create_app(data_dir=tmp_path)) as client:
        yield client


def demo(client):
    assert client.post("/api/tea/auth/demo", json={}).status_code == 200
    bootstrap = client.get("/api/tea/bootstrap").json()
    return client.get("/api/tea/cases/" + bootstrap["cases"][0]["id"]).json()


def test_unauthenticated_and_account_isolation(client, tmp_path):
    assert client.get("/api/tea/cases").status_code == 401
    case = demo(client)
    assert client.post("/api/tea/auth/logout").status_code == 204
    assert client.post("/api/tea/auth/demo", json={}).status_code == 200
    assert client.get("/api/tea/cases/" + case["id"]).status_code == 404


def test_serial_gate_and_missing_evidence(client):
    case = demo(client)
    last = case["stages"][1]["tasks"][0]
    response = client.post(
        f"/api/tea/cases/{case['id']}/tasks/{last['id']}/verify",
        json={"expected_revision": last["revision"], "confirm": True},
    )
    assert response.status_code == 409
    task = case["stages"][0]["tasks"][0]
    response = client.post(
        f"/api/tea/cases/{case['id']}/tasks/{task['id']}/verify",
        json={"expected_revision": task["revision"], "confirm": True},
    )
    assert response.status_code == 200
    task = response.json()["stages"][0]["tasks"][0]
    assert task["status"] == "needs_input"
    assert task["verification"]["status"] == "needs_input"
    assert any(check["status"] == "missing" for check in task["verification"]["checks"])


def test_verified_task_revision_and_reopening(client):
    case = demo(client)
    task = case["stages"][0]["tasks"][0]
    doc = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={
            "file": (
                "产品.md",
                "产品名称：乌龙茶\n成分：茶叶\n每袋净含量：250 克\n".encode(),
                "text/markdown",
            )
        },
    ).json()
    values = {f["key"]: (250 if f["type"] == "number" else "乌龙茶") for f in task["fields"]}
    values.update({"ingredients": "茶叶", "bag_count": 400})
    url = f"/api/tea/cases/{case['id']}/tasks/{task['id']}"
    response = client.patch(
        url, json={"values": values, "evidence_ids": [doc["id"]], "expected_revision": task["revision"]}
    )
    assert response.status_code == 200, response.text
    updated = response.json()["stages"][0]["tasks"][0]
    assert (
        client.post(
            url + "/verify", json={"expected_revision": task["revision"], "confirm": True}
        ).status_code
        == 409
    )
    response = client.post(url + "/verify", json={"expected_revision": updated["revision"], "confirm": True})
    verified = response.json()["stages"][0]["tasks"][0]
    assert verified["status"] == "completed", response.text
    response = client.patch(
        url, json={"values": {"bag_count": 500}, "expected_revision": verified["revision"]}
    )
    reopened = response.json()["stages"][0]["tasks"][0]
    assert reopened["needs_review"] is True
    assert reopened["status"] != "completed"
    assert reopened["confirmed"] is False


def test_message_idempotency_and_cannot_complete_by_claim(client):
    case = demo(client)
    payload = {
        "stage_id": case["stages"][0]["id"],
        "content": "我已经全部完成了",
        "client_message_id": "retry-1",
    }
    url = f"/api/tea/cases/{case['id']}/messages"
    first = client.post(url, json=payload)
    assert first.status_code == 200
    second = client.post(url, json=payload)
    assert [m["id"] for m in first.json()["messages"]] == [m["id"] for m in second.json()["messages"]]
    assert first.json()["case"]["completed_tasks"] == 0
    assert len(client.get(url).json()["items"]) == 3  # initial guidance + one pair


def test_template_publish_freezes_case(client):
    case = demo(client)
    grant_admin(client, client.get("/api/tea/auth/me").json()["id"])
    template = case["template"]
    template["stages"][0]["name"] = "修改后的节点"
    published = client.post("/api/tea/templates/publish", json={"template": template})
    assert published.status_code == 200, published.text
    assert published.json()["version"] == template["version"] + 1
    preserved = client.get("/api/tea/cases/" + case["id"]).json()
    assert preserved["stages"][0]["name"] != "修改后的节点"
    template["stages"][0]["tasks"][0]["invented_rule"] = True
    validation = client.post("/api/tea/templates/validate", json={"template": template}).json()
    assert validation["valid"] is False


def test_artifact_versions_conflict_and_confirmation(client):
    case = demo(client)
    task = case["stages"][0]["tasks"][0]
    artifact = client.post(f"/api/tea/cases/{case['id']}/artifacts", json={"task_id": task["id"]}).json()
    url = "/api/tea/artifacts/" + artifact["id"]
    update = client.patch(
        url, json={"title": artifact["title"], "content": "用户修订", "expected_version": 1}
    )
    assert update.status_code == 200, update.text
    assert update.json()["version"] == 2
    assert (
        client.patch(url, json={"title": "旧请求", "content": "覆盖", "expected_version": 1}).status_code
        == 409
    )
    assert client.post(url + "/confirm", json={"expected_version": 2}).json()["confirmed"] is True
    assert len(client.get(url + "/versions").json()["items"]) == 2


def test_secondary_no_index_and_parent_scope(client):
    case = demo(client)
    main = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("main.md", b"Original document", "text/markdown")},
    ).json()
    response = client.post(
        "/api/tea/documents",
        data={"role": "secondary", "parent_id": main["id"]},
        files={"file": ("attachment.txt", b"secret attachment term", "text/plain")},
    )
    assert response.status_code == 422
    response = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"], "role": "secondary", "parent_id": main["id"]},
        files={"file": ("attachment.txt", b"secret attachment term", "text/plain")},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "stored"
    assert response.json()["chunk_count"] == 0
    assert (
        client.post(
            "/api/tea/search", json={"query": "secret attachment term", "case_id": case["id"]}
        ).json()["items"]
        == []
    )


def test_conflicting_and_superseded_evidence_cannot_pass(client):
    case = demo(client)
    task = case["stages"][0]["tasks"][0]
    doc = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("产品.txt", "净含量：250克".encode(), "text/plain")},
    ).json()
    url = f"/api/tea/cases/{case['id']}/tasks/{task['id']}"
    changed = client.patch(
        url,
        json={
            "expected_revision": 1,
            "values": {
                "product_name": "乌龙茶",
                "ingredients": "茶叶",
                "net_weight_g": 500,
                "bag_count": 400,
            },
            "evidence_ids": [doc["id"]],
        },
    ).json()
    result = client.post(
        url + "/verify",
        json={"expected_revision": changed["stages"][0]["tasks"][0]["revision"], "confirm": True},
    ).json()
    result_task = result["stages"][0]["tasks"][0]
    assert any(check["status"] == "conflict" for check in result_task["verification"]["checks"])
    new_doc = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("产品.txt", "净含量：500克".encode(), "text/plain")},
    ).json()
    assert new_doc["version"] == 2
    result_task = client.get("/api/tea/cases/" + case["id"]).json()["stages"][0]["tasks"][0]
    result = client.post(
        url + "/verify", json={"expected_revision": result_task["revision"], "confirm": True}
    ).json()
    assert any(
        "新版本" in check["reason"] for check in result["stages"][0]["tasks"][0]["verification"]["checks"]
    )


def test_register_upgrade_login_logout_and_origin(client):
    original = demo(client)
    owner = client.get("/api/tea/auth/me").json()["id"]
    response = client.post(
        "/api/tea/auth/register",
        json={"name": "业务员", "email": "owner@example.com", "password": "correct horse battery"},
    )
    assert response.status_code == 201
    assert response.json()["is_demo"] is False
    assert response.json()["id"] == owner
    assert response.json()["can_login"] is True
    assert response.json()["is_admin"] is False
    assert client.get("/api/tea/cases/" + original["id"]).status_code == 200
    assert (
        client.post("/api/tea/auth/logout", headers={"origin": "https://attacker.invalid"}).status_code == 403
    )
    assert client.post("/api/tea/auth/logout").status_code == 204
    assert (
        client.post(
            "/api/tea/auth/login", json={"email": "owner@example.com", "password": "incorrect"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/tea/auth/login", json={"email": "owner@example.com", "password": "correct horse battery"}
        ).status_code
        == 200
    )
    with TestClient(client.app) as admin:
        login_admin(admin)
        access = admin.get("/api/tea/admin/users", params={"q": "owner@example.com"}).json()["items"][0]
        disabled = admin.patch(
            f"/api/tea/admin/users/{owner}",
            json={"expected_revision": access["revision"], "can_login": False},
        )
        assert disabled.status_code == 200, disabled.text
        assert client.get("/api/tea/auth/me").status_code == 401
        assert (
            client.post(
                "/api/tea/auth/login",
                json={"email": "owner@example.com", "password": "correct horse battery"},
            ).status_code
            == 403
        )
        approved = admin.patch(
            f"/api/tea/admin/users/{owner}",
            json={"expected_revision": disabled.json()["revision"], "can_login": True},
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["is_admin"] is False
    assert (
        client.post(
            "/api/tea/auth/login", json={"email": "owner@example.com", "password": "correct horse battery"}
        ).status_code
        == 200
    )
    assert client.get("/api/tea/auth/me").json()["is_admin"] is False
    assert client.get("/api/tea/cases/" + original["id"]).status_code == 200


def test_public_registration_starts_a_regular_session_without_admin_approval(client):
    payload = {"name": "新用户", "email": "Public@example.com", "password": "public-signup-test"}
    response = client.post("/api/tea/auth/register", json=payload)
    assert response.status_code == 201, response.text
    account = response.json()
    assert account["can_login"] is True and account["is_admin"] is False
    assert account["email"] == "public@example.com"
    assert client.get("/api/tea/auth/me").json()["id"] == account["id"]
    assert client.get("/api/tea/bootstrap").status_code == 200
    assert client.get("/api/tea/admin/users").status_code == 403
    assert client.post("/api/tea/auth/register", json=payload).status_code == 409
    assert client.get("/api/tea/auth/me").json()["id"] == account["id"]
    assert client.post("/api/tea/auth/logout").status_code == 204
    assert client.get("/api/tea/auth/me").status_code == 401
    credentials = {key: payload[key] for key in ("email", "password")}
    assert client.post("/api/tea/auth/login", json=credentials).status_code == 200
    assert client.get("/api/tea/auth/me").json()["id"] == account["id"]


def test_message_request_id_reuse_for_different_payload_is_rejected(client):
    case = demo(client)
    url = f"/api/tea/cases/{case['id']}/messages"
    payload = {
        "stage_id": case["stages"][0]["id"],
        "content": "资料还缺什么",
        "client_message_id": "unique-1",
    }
    assert client.post(url, json=payload).status_code == 200
    assert client.post(url, json={**payload, "content": "更改后的消息"}).status_code == 409


def test_chat_points_out_user_weight_conflict(client):
    case = demo(client)
    doc = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("产品.txt", "乌龙茶 净含量：250克".encode(), "text/plain")},
    ).json()
    task = case["stages"][0]["tasks"][0]
    patched = client.patch(
        f"/api/tea/cases/{case['id']}/tasks/{task['id']}",
        json={
            "expected_revision": task["revision"],
            "values": {"net_weight_g": 500},
            "evidence_ids": [doc["id"]],
        },
    )
    assert patched.status_code == 200
    response = client.post(
        f"/api/tea/cases/{case['id']}/messages",
        json={
            "stage_id": case["stages"][0]["id"],
            "content": "乌龙茶净含量500克，资料完成了",
            "client_message_id": "conflict-chat",
        },
    )
    assert response.status_code == 200
    assert "250" in response.json()["messages"][1]["content"]
    assert "500" in response.json()["messages"][1]["content"]


def test_upstream_change_invalidates_downstream_completion_and_artifact(client):
    case = demo(client)
    for stage_index in (0, 1):
        task_ids = [task["id"] for task in case["stages"][stage_index]["tasks"]]
        for task_id in task_ids:
            task = next(task for task in case["stages"][stage_index]["tasks"] if task["id"] == task_id)
            values = {
                field["key"]: (250 if field["type"] == "number" else "确认资料") for field in task["fields"]
            }
            evidence = []
            if task["require_document"]:
                doc = client.post(
                    "/api/tea/documents",
                    data={"case_id": case["id"]},
                    files={"file": (task_id + ".txt", "净含量：250克".encode(), "text/plain")},
                ).json()
                evidence = [doc["id"]]
            url = f"/api/tea/cases/{case['id']}/tasks/{task_id}"
            case = client.patch(
                url, json={"values": values, "evidence_ids": evidence, "expected_revision": task["revision"]}
            ).json()
            updated = next(task for task in case["stages"][stage_index]["tasks"] if task["id"] == task_id)
            case = client.post(
                url + "/verify", json={"expected_revision": updated["revision"], "confirm": True}
            ).json()
    assert case["stages"][1]["status"] == "completed"
    downstream = case["stages"][1]["tasks"][0]
    artifact = client.post(
        f"/api/tea/cases/{case['id']}/artifacts", json={"task_id": downstream["id"]}
    ).json()
    assert (
        client.post(
            "/api/tea/artifacts/" + artifact["id"] + "/confirm",
            json={"expected_version": artifact["version"]},
        ).json()["confirmed"]
        is True
    )
    upstream = case["stages"][0]["tasks"][0]
    case = client.patch(
        f"/api/tea/cases/{case['id']}/tasks/{upstream['id']}",
        json={"values": {"bag_count": 600}, "expected_revision": upstream["revision"]},
    ).json()
    assert case["stages"][1]["tasks"][0]["needs_review"] is True
    assert case["stages"][1]["tasks"][0]["status"] != "completed"
    assert case["artifacts"][0]["confirmed"] is False
    upstream = case["stages"][0]["tasks"][0]
    case = client.post(
        f"/api/tea/cases/{case['id']}/tasks/{upstream['id']}/verify",
        json={"expected_revision": upstream["revision"], "confirm": True},
    ).json()
    assert case["stages"][1]["status"] == "active"
    assert case["stages"][1]["tasks"][0]["needs_review"] is True


def test_failed_parse_can_be_retried_with_same_bytes(client, monkeypatch):
    import app.contexts.documents.infrastructure.parsers.document_parser as parsing

    case = demo(client)

    def failed(*args, **kwargs):
        raise ValueError("temporary parser failure")

    monkeypatch.setattr(parsing, "parse_bounded", failed)
    first = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("retry.txt", b"retry document", "text/plain")},
    ).json()
    assert first["status"] == "failed"
    monkeypatch.undo()
    second = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("retry.txt", b"retry document", "text/plain")},
    ).json()
    assert second["status"] == "ready"
    assert second["id"] != first["id"]


def test_secondary_relationship_is_new_for_each_parent_version(client):
    case = demo(client)
    parent1 = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("parent.txt", b"parent version one", "text/plain")},
    ).json()
    child1 = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"], "role": "secondary", "parent_id": parent1["id"]},
        files={"file": ("child.txt", b"unchanged attachment", "text/plain")},
    ).json()
    parent2 = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("parent.txt", b"parent version two", "text/plain")},
    ).json()
    child2 = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"], "role": "secondary", "parent_id": parent2["id"]},
        files={"file": ("child.txt", b"unchanged attachment", "text/plain")},
    ).json()
    assert child2["parent_id"] == parent2["id"]
    assert child1["id"] != child2["id"]


def test_failed_source_has_durable_backoff(client, monkeypatch):
    from app.shared.infrastructure.http import download as ingestion

    demo(client)
    grant_admin(client, client.get("/api/tea/auth/me").json()["id"])
    source = client.post(
        "/api/tea/sources",
        json={
            "name": "测试官方来源",
            "url": "https://public.example/source.pdf",
            "enabled": True,
            "interval_hours": 24,
            "tags": [],
        },
    ).json()
    service = client.app.state.service
    assert any(item["id"] == source["id"] for item in service.due_sources())

    def unavailable(*args, **kwargs):
        raise ValueError("network unavailable")

    monkeypatch.setattr(ingestion, "_download", unavailable)
    response = client.post("/api/tea/sources/" + source["id"] + "/fetch")
    assert response.status_code == 202
    service.collection.fetch_item(service.collection.claim_fetch())
    assert client.get("/api/tea/sources/" + source["id"]).json()["status"] == "failed"
    assert not any(item["id"] == source["id"] for item in service.due_sources())


def test_state_and_session_survive_process_recreation(tmp_path):
    from app.interfaces.http.factory import create_app

    with TestClient(create_app(data_dir=tmp_path)) as first:
        case = demo(first)
        cookies = dict(first.cookies)
    with TestClient(create_app(data_dir=tmp_path)) as second:
        second.cookies.update(cookies)
        assert second.get("/api/tea/cases/" + case["id"]).status_code == 200
        assert second.get("/api/tea/auth/me").json()["is_demo"] is True


def test_live_assistant_receives_prior_stage_messages(client, monkeypatch):
    import httpx

    case = demo(client)
    for key, value in {
        "LLM_BASE_URL": "https://provider.invalid/v1",
        "LLM_API_KEY": "test",
        "LLM_MODEL": "test-model",
    }.items():
        monkeypatch.setenv(key, value)
    captured = []

    def respond(request):
        import json

        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "先准备产品说明书。"}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs)
    )
    url = f"/api/tea/cases/{case['id']}/messages"
    assert (
        client.post(
            url,
            json={
                "stage_id": case["stages"][0]["id"],
                "content": "第一步做什么",
                "client_message_id": "live-1",
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            url,
            json={
                "stage_id": case["stages"][0]["id"],
                "content": "你刚说的材料指什么",
                "client_message_id": "live-2",
            },
        ).status_code
        == 200
    )
    assert any(
        message["role"] == "assistant" and message["content"] == "先准备产品说明书。"
        for message in captured[1]["messages"]
    )


def test_concurrent_task_updates_reject_one_stale_writer(client):
    from concurrent.futures import ThreadPoolExecutor

    case = demo(client)
    task = case["stages"][0]["tasks"][0]
    owner = client.get("/api/tea/auth/me").json()["id"]
    service = client.app.state.service

    def update(name):
        from app.shared.domain.errors import AppError

        try:
            result = service.patch_task(
                owner,
                case["id"],
                task["id"],
                {"values": {"product_name": name}, "expected_revision": task["revision"]},
            )
            return ("ok", result["stages"][0]["tasks"][0]["values"]["product_name"])
        except AppError as error:
            return (error.code, None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, ["first writer", "second writer"]))
    assert sorted(result[0] for result in results) == ["ok", "stale_revision"]
    stored = service.case_detail(owner, case["id"])["stages"][0]["tasks"][0]
    assert stored["revision"] == task["revision"] + 1
    assert stored["values"]["product_name"] == next(result[1] for result in results if result[0] == "ok")


def test_live_provider_failure_is_visible_and_does_not_complete_task(client, monkeypatch):
    import httpx

    case = demo(client)
    for key, value in {
        "LLM_BASE_URL": "https://provider.invalid/v1",
        "LLM_API_KEY": "test",
        "LLM_MODEL": "test-model",
    }.items():
        monkeypatch.setenv(key, value)

    calls = []

    def unavailable(request):
        calls.append(request)
        raise httpx.ReadTimeout("unavailable", request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(unavailable), **kwargs)
    )
    url = f"/api/tea/cases/{case['id']}/messages"
    before = client.get(url).json()["items"]
    response = client.post(
        f"/api/tea/cases/{case['id']}/messages",
        json={
            "stage_id": case["stages"][0]["id"],
            "content": "办理完了，请确认",
            "client_message_id": "failed-live",
        },
    )
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "assistant_failed"
    assert len(calls) == 1
    assert client.get(url).json()["items"] == before
    assert client.get(f"/api/tea/cases/{case['id']}").json()["completed_tasks"] == 0


def test_partial_source_collection_preserves_original_and_exposes_warnings(client, monkeypatch):
    from app.shared.infrastructure.http import download as ingestion

    demo(client)
    grant_admin(client, client.get("/api/tea/auth/me").json()["id"])
    source = client.post(
        "/api/tea/sources",
        json={
            "name": "资料来源",
            "url": "https://example.org/source.txt",
            "enabled": True,
            "interval_hours": 24,
            "tags": [],
        },
    ).json()

    def download(url, *, consume, **kwargs):
        if url.endswith("attachment.txt"):
            raise ValueError("附件 A 下载失败：连接超时")
        return {
            "url": url,
            "filename": "source.html",
            "mime_type": "text/html",
            "blob": consume(iter(['<p>已取得主文件正文</p><a href="attachment.txt">附件 A</a>'.encode()])),
        }

    monkeypatch.setattr(ingestion, "_download", download)
    response = client.post("/api/tea/sources/" + source["id"] + "/fetch")
    assert response.status_code == 202
    collection = client.app.state.service.collection
    collection.fetch_item(collection.claim_fetch())
    collection.fetch_item(collection.claim_fetch())
    collection.process_item(collection.claim_processing())
    result = client.get("/api/tea/collection-runs/" + response.json()["id"]).json()
    assert result["run"]["status"] == "partial"
    assert result["run"]["stored"] == 1 and result["run"]["failed"] == 1
    assert any("附件 A" in (item["error"] or "") for item in result["items"])
    doc = client.get("/api/tea/documents").json()["items"][0]
    assert doc["status"] == "ready"
    assert b"<p>" in client.get("/api/tea/documents/" + doc["id"] + "/download").content
    assert not any(item["id"] == source["id"] for item in client.app.state.service.due_sources())


def test_partial_parse_warnings_are_visible_on_document(client, monkeypatch):
    import app.contexts.documents.infrastructure.parsers.document_parser as parsing

    case = demo(client)
    monkeypatch.setattr(
        parsing,
        "parse_bounded",
        lambda *args, **kwargs: {
            "parser": "basic",
            "elements": [
                {
                    "element_key": "p1",
                    "kind": "paragraph",
                    "content_text": "可读取的第一页",
                    "section_path": [],
                    "locator": {"page": 1},
                }
            ],
            "chunks": [{"id": "c1", "text": "可读取的第一页", "section_path": [], "locator": {"page": 1}}],
            "warnings": ["第 2 页无可提取文本，扫描页需要 OCR"],
        },
    )
    response = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("partial.pdf", b"%PDF-placeholder", "application/pdf")},
    )
    assert response.status_code == 201
    document = response.json()
    assert document["status"] == "ready"
    assert "解析提示" in document["error"]
    assert "第 2 页" in document["error"]
    persisted = client.get("/api/tea/documents/" + document["id"]).json()["document"]
    assert persisted["error"] == document["error"]
    assert persisted["chunk_count"] == 1
