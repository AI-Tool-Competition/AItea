"""Cross-module acceptance and adversarial requests, using a real temporary store."""

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    from app.main import create_app

    with TestClient(create_app(data_dir=tmp_path)) as instance:
        assert instance.post("/api/tea/auth/demo", json={}).status_code == 200
        yield instance


def current_case(client):
    bootstrap = client.get("/api/tea/bootstrap").json()
    return client.get("/api/tea/cases/" + bootstrap["cases"][0]["id"]).json()


def upload(client, case_id, filename, content, **extra):
    response = client.post(
        "/api/tea/documents",
        data={"case_id": case_id, **extra},
        files={"file": (filename, content.encode(), "text/plain")},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_retrieval_never_crosses_cases_owned_by_the_same_account(client):
    first = current_case(client)
    second = client.post(
        "/api/tea/cases",
        json={
            "title": "另一次业务",
            "company": "演示茶商",
            "product": "红茶",
            "mode_id": first["mode_id"],
            "template_id": first["template"]["id"],
        },
    ).json()
    upload(client, first["id"], "scope.txt", "orchidneedle42 belongs only to the first export business.")
    assert client.post("/api/tea/search", json={"query": "orchidneedle42", "case_id": first["id"]}).json()[
        "items"
    ]
    assert (
        client.post("/api/tea/search", json={"query": "orchidneedle42", "case_id": second["id"]}).json()[
            "items"
        ]
        == []
    )


def test_download_remains_private_and_uses_attachment_disposition(client):
    case = current_case(client)
    document = upload(client, case["id"], "说明.txt", "用于原文下载权限验证的产品说明。")
    path = f"/api/tea/documents/{document['id']}/download"
    response = client.get(path)
    assert response.status_code == 200
    assert "attachment" in response.headers.get("content-disposition", "")
    client.post("/api/tea/auth/logout")
    assert client.get(path).status_code == 401
    client.post("/api/tea/auth/demo", json={})
    assert client.get(path).status_code == 404


def test_cookie_session_rejects_mutation_from_untrusted_origin(client):
    response = client.post("/api/tea/auth/logout", headers={"Origin": "https://untrusted.example"})
    assert response.status_code == 403
    assert client.get("/api/tea/auth/me").status_code == 200


def test_a_case_survives_application_restart(tmp_path):
    from app.main import create_app

    with TestClient(create_app(data_dir=tmp_path)) as first:
        first.post("/api/tea/auth/demo", json={})
        case = current_case(first)
        cookies = dict(first.cookies)
    with TestClient(create_app(data_dir=tmp_path)) as restarted:
        restarted.cookies.update(cookies)
        response = restarted.get("/api/tea/cases/" + case["id"])
        assert response.status_code == 200
        assert response.json()["template"] == case["template"]


def test_nonfinite_numeric_values_do_not_crash_or_enter_business_state(client):
    case = current_case(client)
    task = case["stages"][0]["tasks"][0]
    body = json.dumps({"values": {"net_weight_g": float("nan")}, "expected_revision": task["revision"]})
    response = client.patch(
        f"/api/tea/cases/{case['id']}/tasks/{task['id']}",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422


def test_failed_parse_keeps_original_without_indexing_it(client):
    case = current_case(client)
    response = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={"file": ("broken.pdf", b"not a pdf", "application/pdf")},
    )
    assert response.status_code == 201, response.text
    document = response.json()
    assert document["status"] == "failed"
    assert document["chunk_count"] == 0
    assert client.get(f"/api/tea/documents/{document['id']}/download").content == b"not a pdf"


def test_old_message_key_cannot_silently_replace_a_different_message(client):
    case = current_case(client)
    payload = {
        "stage_id": case["stages"][0]["id"],
        "content": "请列出资料缺项",
        "client_message_id": "same-key",
    }
    url = f"/api/tea/cases/{case['id']}/messages"
    assert client.post(url, json=payload).status_code == 200
    payload["content"] = "这是另一个完全不同的问题"
    assert client.post(url, json=payload).status_code == 409


def test_legal_review_and_effective_dates_gate_retrieval(client):
    case = current_case(client)
    doc = upload(
        client, case["id"], "法规演示.txt", "samplelegalneedle 这是一条用于系统测试的非真实法律文字。"
    )
    classified = client.patch(
        f"/api/tea/documents/{doc['id']}/classification",
        json={"collection": "country:ID", "level": "primary", "expected_revision": 0},
    )
    assert classified.status_code == 200, classified.text
    url = f"/api/tea/documents/{doc['id']}/metadata"
    response = client.patch(
        url,
        json={
            "expected_revision": classified.json()["metadata_revision"],
            "kind": "law",
            "jurisdiction": "Indonesia",
            "review_status": "draft",
        },
    )
    assert response.status_code == 200, response.text
    search = {"query": "samplelegalneedle", "case_id": case["id"]}
    assert client.post("/api/tea/search", json=search).json()["items"] == []
    reviewed = client.patch(
        url,
        json={
            "expected_revision": response.json()["metadata_revision"],
            "review_status": "published",
            "effective_from": "2025-01-01",
            "effective_to": "2025-12-31",
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    assert client.post("/api/tea/search", json={**search, "as_of": "2026-10-11"}).json()["items"] == []
    hits = client.post("/api/tea/search", json={**search, "as_of": "2025-06-01"}).json()["items"]
    assert len(hits) == 1
    assert hits[0]["metadata"]["kind"] == "law"
    assert hits[0]["metadata_revision"] == reviewed.json()["metadata_revision"]
    assert client.patch(url, json={"expected_revision": 1, "language": "id"}).status_code == 409


def test_unreviewed_fetched_material_is_excluded_by_default(client, monkeypatch):
    from app import ingestion

    def download(url, *, consume, **kwargs):
        return {
            "url": url,
            "filename": "regulation.txt",
            "mime_type": "text/plain",
            "blob": consume(iter([b"legalreviewneedle fixture, not an actual law."])),
        }

    monkeypatch.setattr(ingestion, "_download", download)
    source = client.post(
        "/api/tea/sources", json={"name": "测试来源", "url": "https://legal.example/regulation"}
    ).json()
    result = client.post(f"/api/tea/sources/{source['id']}/fetch").json()
    assert result["status"] == "queued"
    collection = client.app.state.service.collection
    collection.fetch_item(collection.claim_fetch())
    collection.process_item(collection.claim_processing())
    doc = client.get("/api/tea/documents").json()["items"][0]
    assert doc["status"] == "ready"
    assert doc["metadata"]["review_status"] == "draft"
    assert client.post("/api/tea/search", json={"query": "legalreviewneedle"}).json()["items"] == []


def test_new_unreviewed_version_blocks_silent_fallback_to_old_approved_version(client):
    case = current_case(client)
    upload(client, case["id"], "版本材料.txt", "versionneedle this is the old approved content.")
    new = upload(client, case["id"], "版本材料.txt", "versionneedle this is the new pending review content.")
    client.patch(
        f"/api/tea/documents/{new['id']}/metadata", json={"expected_revision": 0, "review_status": "draft"}
    )
    assert (
        client.post("/api/tea/search", json={"query": "versionneedle", "case_id": case["id"]}).json()["items"]
        == []
    )


def test_withdrawn_document_review_reopens_task_and_blocks_reverification(client):
    case = current_case(client)
    doc = upload(client, case["id"], "业务说明.txt", "每袋净含量：250 克。")
    task = case["stages"][0]["tasks"][0]
    path = f"/api/tea/cases/{case['id']}/tasks/{task['id']}"
    saved = client.patch(
        path,
        json={
            "values": {"product_name": "乌龙茶", "ingredients": "茶叶", "net_weight_g": 250, "bag_count": 10},
            "evidence_ids": [doc["id"]],
            "expected_revision": task["revision"],
        },
    ).json()
    verified = client.post(
        path + "/verify",
        json={"confirm": True, "expected_revision": saved["stages"][0]["tasks"][0]["revision"]},
    ).json()
    assert verified["stages"][0]["tasks"][0]["status"] == "completed"
    client.patch(
        f"/api/tea/documents/{doc['id']}/metadata", json={"expected_revision": 0, "review_status": "draft"}
    )
    updated = current_case(client)["stages"][0]["tasks"][0]
    assert updated["needs_review"] is True
    checked = client.post(
        path + "/verify", json={"confirm": True, "expected_revision": updated["revision"]}
    ).json()
    assert checked["stages"][0]["tasks"][0]["status"] == "needs_input"


def test_conversation_pagination_opens_latest_messages_and_can_load_earlier(client):
    case = current_case(client)
    path = f"/api/tea/cases/{case['id']}/messages"
    for index in range(3):
        client.post(
            path,
            json={
                "stage_id": case["stages"][0]["id"],
                "content": f"第{index}个问题",
                "client_message_id": f"page-{index}",
            },
        )
    latest = client.get(path, params={"limit": 2}).json()
    older = client.get(path, params={"limit": 2, "offset": 2}).json()
    assert latest["total"] == 7
    assert latest["items"][0]["content"] == "第2个问题"
    assert older["items"][0]["content"] == "第1个问题"
