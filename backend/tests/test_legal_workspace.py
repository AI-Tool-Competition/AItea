"""The legal workspace keeps facts, evidence and editable answers versioned."""

from contextlib import closing

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("ragflow_library")


@pytest.fixture
def client(tmp_path):
    from app.interfaces.http.factory import create_app

    with TestClient(create_app(data_dir=tmp_path)) as client:
        yield client


def workspace(client):
    assert client.post("/api/tea/auth/demo", json={}).status_code == 200
    bootstrap = client.get("/api/tea/bootstrap").json()
    case = client.get("/api/tea/cases/" + bootstrap["cases"][0]["id"]).json()
    return case, f"/api/tea/cases/{case['id']}/legal"


def fact_payload(value="2026-06-01", revision=0, **extra):
    return {
        "expected_revision": revision,
        "label": "付款到期日",
        "value": value,
        "confirmed": True,
        "evidence_status": "unverified",
        "evidence_refs": [],
        "review_note": "",
        **extra,
    }


def test_policy_and_workspace_require_authentication(client):
    assert client.get("/api/tea/legal/policy").status_code == 401
    case, url = workspace(client)
    policy = client.get("/api/tea/legal/policy")
    assert policy.status_code == 200, policy.text
    assert len(policy.json()["modules"]) == 6
    assert policy.json()["program_checks"]
    state = client.get(url, params={"stage_id": case["stages"][0]["id"]})
    assert state.status_code == 200, state.text
    assert state.json()["analyses"] == []
    assert client.get(url, params={"stage_id": "unknown"}).status_code == 422


def test_fact_versions_conflicts_and_owner_scope(client):
    case, url = workspace(client)
    before = case["completed_tasks"]
    response = client.patch(url + "/facts/payment_due", json=fact_payload())
    assert response.status_code == 200, response.text
    fact = response.json()["fact"]
    assert fact["revision"] == 1
    assert fact["confirmed"] is True
    assert fact["evidence_status"] == "unverified"
    assert client.patch(url + "/facts/payment_due", json=fact_payload()).status_code == 409
    response = client.patch(url + "/facts/payment_due", json=fact_payload("2026-06-15", 1))
    assert response.status_code == 200
    assert response.json()["fact"]["value"] == "2026-06-15"
    assert response.json()["fact"]["revision"] == 2
    assert client.get(f"/api/tea/cases/{case['id']}").json()["completed_tasks"] == before
    with closing(client.app.state.service.db.connect()) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM legal_fact_versions WHERE case_id=?", (case["id"],)
            ).fetchone()[0]
            == 2
        )
    assert client.post("/api/tea/auth/logout").status_code == 204
    workspace(client)
    assert client.get(url).status_code == 404
    assert client.patch(url + "/facts/payment_due", json=fact_payload("x", 2)).status_code == 404


def test_confirmation_cannot_claim_evidence_verification(client):
    _, url = workspace(client)
    response = client.patch(url + "/facts/payment_due", json=fact_payload(evidence_status="verified"))
    assert response.status_code == 422, response.text
    response = client.patch(url + "/facts/payment_due", json=fact_payload(evidence_refs=["unknown"]))
    assert response.status_code == 404


def test_legal_migration_upgrades_existing_data_and_reopens(client, tmp_path):
    from app.bootstrap import Service

    case, url = workspace(client)
    assert client.patch(url + "/facts/payment_due", json=fact_payload()).status_code == 200
    with closing(client.app.state.service.db.connect()) as conn:
        owner = conn.execute("SELECT owner_id FROM cases WHERE id=?", (case["id"],)).fetchone()[0]
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=9").fetchone()
    reopened = Service(tmp_path)
    state = reopened.legal.workspace(owner, case["id"])
    assert state["facts"][0]["value"] == "2026-06-01"
    assert reopened.case_detail(owner, case["id"])["template_version"] == case["template_version"]


def test_scope_search_preserves_legacy_country_gate(client):
    from app.shared.domain.errors import AppError

    case, _ = workspace(client)
    service = client.app.state.service
    with closing(service.db.connect()) as conn:
        owner = conn.execute("SELECT owner_id FROM cases WHERE id=?", (case["id"],)).fetchone()[0]
    # China is a permitted origin for legal research, while legacy /search stays destination-only.
    result = service.legal.search(owner, "tea", case["id"], case["template"], ["CN", "ID"], 5)
    assert isinstance(result["items"], list)
    with pytest.raises(AppError) as caught:
        service.legal.search(owner, "tea", case["id"], case["template"], ["US"], 5)
    assert caught.value.status == 422
    assert (
        client.post(
            "/api/tea/search", json={"query": "tea", "case_id": case["id"], "country": "CN"}
        ).status_code
        == 422
    )


def fake_result(question, content="原始建议"):
    from app.contexts.legal_analysis.domain.analysis_models import LegalResult

    return LegalResult.model_validate(
        {
            "question": question,
            "issues": [
                {
                    "id": "payment",
                    "question": question,
                    "kind": "legal",
                    "fact_keys": ["payment_due"],
                    "status": "conditional",
                    "conclusion": content,
                }
            ],
            "sections": [
                {"id": "payment.answer", "issue_id": "payment", "title": "付款问题", "content": content}
            ],
            "assistant_mode": "demo",
            "output": content,
        }
    ).model_dump(mode="json")


def test_analysis_idempotency_manual_edits_partial_refresh_and_history(client, monkeypatch):
    case, url = workspace(client)
    runs = []

    def execute(owner, template, stage, question, facts, **options):
        runs.append(options)
        return fake_result(question, "修订建议" if len(runs) > 1 else "原始建议")

    monkeypatch.setattr(client.app.state.service.legal, "execute_engine", execute, raising=False)
    request = {
        "stage_id": case["stages"][0]["id"],
        "question": "付款义务如何判断？",
        "client_request_id": "analysis-1",
    }
    assert client.patch(url + "/facts/payment_due", json=fact_payload()).status_code == 200
    response = client.post(url + "/analyses", json=request)
    assert response.status_code == 200, response.text
    analysis = response.json()
    aid = analysis["id"]
    assert client.post(url + "/analyses", json=request).json()["id"] == aid
    assert len(runs) == 1
    assert client.post(url + "/analyses", json={**request, "question": "不同问题"}).status_code == 409
    assert (
        client.patch(
            url + f"/analyses/{aid}/sections/payment.answer",
            json={"expected_revision": analysis["revision"], "content": "我的判断"},
        ).status_code
        == 200
    )
    assert (
        client.patch(
            url + f"/analyses/{aid}/sections/payment.answer",
            json={"expected_revision": analysis["revision"], "content": "旧窗口覆盖"},
        ).status_code
        == 409
    )
    assert client.patch(url + "/facts/payment_due", json=fact_payload("2026-06-15", 1)).status_code == 200
    stale = client.get(url + f"/analyses/{aid}").json()
    assert stale["affected_issue_ids"] == ["payment"]
    assert stale["status"] == "needs_review"
    refresh = {"expected_revision": stale["revision"], "client_request_id": "refresh-1"}
    response = client.post(url + f"/analyses/{aid}/refresh", json=refresh)
    assert response.status_code == 200, response.text
    updated = response.json()
    section = updated["sections"][0]
    assert section["content"] == "我的判断"
    assert section["suggested_content"] == "修订建议"
    assert section["needs_review"] is True
    assert runs[1]["affected_issue_ids"] == ["payment"]
    assert client.post(url + f"/analyses/{aid}/refresh", json=refresh).json() == updated
    response = client.patch(
        url + f"/analyses/{aid}/sections/payment.answer",
        json={"expected_revision": updated["revision"], "content": "修订建议", "accept_suggestion": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["sections"][0]["edited"] is False
    history = client.get(url + f"/analyses/{aid}/versions").json()
    assert history["total"] >= 5
    first = client.get(url + f"/analyses/{aid}/versions/{analysis['revision']}")
    assert first.status_code == 200
    assert first.json()["sections"][0]["content"] == "原始建议"


def test_changed_facts_during_generation_do_not_publish_stale_result(client, monkeypatch):
    case, url = workspace(client)
    service = client.app.state.service
    assert client.patch(url + "/facts/payment_due", json=fact_payload()).status_code == 200

    def execute(owner, template, stage, question, facts, **options):
        service.legal.patch_fact(owner, case["id"], "payment_due", fact_payload("2026-07-01", 1))
        return fake_result(question)

    monkeypatch.setattr(service.legal, "execute_engine", execute, raising=False)
    response = client.post(
        url + "/analyses",
        json={"stage_id": case["stages"][0]["id"], "question": "付款义务", "client_request_id": "stale-run"},
    )
    assert response.status_code == 409, response.text
    analyses = client.get(url).json()["analyses"]
    assert len(analyses) == 1
    assert analyses[0]["status"] == "needs_review"
    saved = client.get(url + "/analyses/" + analyses[0]["id"]).json()
    assert saved["sections"] == []


def test_legal_stream_and_analysis_ownership(client, monkeypatch):
    case, url = workspace(client)
    monkeypatch.setattr(
        client.app.state.service.legal,
        "execute_engine",
        lambda owner, template, stage, question, facts, **options: fake_result(question),
        raising=False,
    )
    response = client.post(
        url + "/analyses/stream",
        json={"stage_id": case["stages"][0]["id"], "question": "付款义务", "client_request_id": "stream-1"},
    )
    assert response.status_code == 200, response.text
    assert "event: done" in response.text
    assert '"analysis"' in response.text
    aid = client.get(url).json()["analyses"][0]["id"]
    assert client.post("/api/tea/auth/logout").status_code == 204
    workspace(client)
    assert client.get(url + "/analyses/" + aid).status_code == 404
    assert client.get(url + f"/analyses/{aid}/versions").status_code == 404


def test_task_fact_changes_fence_old_edit_and_preserve_resolved_conflicts(client):
    from app.shared.domain.values import dump, now

    case, url = workspace(client)
    service = client.app.state.service
    with service.db.transaction() as conn:
        conn.execute(
            "INSERT INTO facts VALUES(?,?,?,?,?,?,?)",
            (case["id"], "payment_due", "到期日", dump("2026-06-01"), "task:payment", 0, now()),
        )
    first = client.get(url).json()["facts"][0]
    assert first["revision"] >= 1
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE facts SET value=?,updated_at=? WHERE case_id=? AND key=?",
            (dump("2026-06-15"), now(), case["id"], "payment_due"),
        )
    assert (
        client.patch(
            url + "/facts/payment_due", json=fact_payload("2026-06-03", first["revision"])
        ).status_code
        == 409
    )
    newer = client.get(url).json()["facts"][0]
    assert newer["value"] == "2026-06-15"
    assert (
        client.patch(
            url + "/facts/payment_due", json=fact_payload("2026-06-16", newer["revision"])
        ).status_code
        == 200
    )
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE facts SET value=?,updated_at=? WHERE case_id=? AND key=?",
            (dump("2026-06-20"), now(), case["id"], "payment_due"),
        )
    conflict = client.get(url).json()["facts"][0]
    assert conflict["evidence_status"] == "conflict"
    assert conflict["conflicts"][0]["value"] == "2026-06-20"
    assert conflict["confirmed"] is False
    assert (
        client.patch(
            url + "/facts/payment_due", json=fact_payload("2026-06-16", conflict["revision"])
        ).status_code
        == 422
    )
    resolved = client.patch(
        url + "/facts/payment_due",
        json=fact_payload("2026-06-16", conflict["revision"], review_note="以补充协议为准"),
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["fact"]["conflicts"] == []
    assert resolved.json()["fact"]["resolved_conflicts"][0]["value"] == "2026-06-20"


def test_failed_or_cancelled_analysis_reuses_request_and_releases_claim(client, monkeypatch):
    from app.shared.application.streaming import StreamCancelled

    case, url = workspace(client)
    service = client.app.state.service
    with closing(service.db.connect()) as conn:
        owner = conn.execute("SELECT owner_id FROM cases WHERE id=?", (case["id"],)).fetchone()[0]
    runs = []

    def execute(owner, template, stage, question, facts, **options):
        runs.append(options)
        if len(runs) == 1:
            raise StreamCancelled()
        return fake_result(question)

    monkeypatch.setattr(service.legal, "execute_engine", execute)
    request = {
        "stage_id": case["stages"][0]["id"],
        "question": "付款义务",
        "client_request_id": "cancelled-1",
    }
    with pytest.raises(StreamCancelled):
        service.legal.analyze(owner, case["id"], request)
    cancelled = client.get(url).json()["analyses"][0]
    assert cancelled["status"] == "cancelled"
    response = client.post(url + "/analyses", json=request)
    assert response.status_code == 200, response.text
    assert response.json()["id"] == cancelled["id"]
    assert client.get(url).json()["total"] == 1


def test_demo_runs_real_engine_without_inventing_sources(client):
    case, url = workspace(client)
    response = client.post(
        url + "/analyses",
        json={
            "stage_id": case["stages"][0]["id"],
            "question": "可以索赔吗，茶叶是什么？",
            "client_request_id": "demo-real",
        },
    )
    assert response.status_code == 200, response.text
    analysis = response.json()
    assert analysis["result"]["assistant_mode"] == "demo"
    assert len(analysis["result"]["trace"]) == 6
    assert all(issue["status"] in ("conditional", "undetermined") for issue in analysis["result"]["issues"])
    assert analysis["result"]["citations"] == []
    assert analysis["status"] != "completed"


def test_empty_proposal_is_saved_without_self_confirmation(client, monkeypatch):
    from app.contexts.legal_analysis.domain.analysis_models import LegalFact

    case, url = workspace(client)
    assert (
        client.patch(url + "/facts/payment_due", json=fact_payload(None, confirmed=False)).status_code == 200
    )

    def execute(owner, template, stage, question, facts, **options):
        result = fake_result(question)
        result["facts"] = [
            LegalFact(
                key="payment_due",
                label="付款到期日",
                value="2026-06-15",
                source="合同摘录",
                extraction_confidence="high",
            ).model_dump(mode="json")
        ]
        return result

    monkeypatch.setattr(client.app.state.service.legal, "execute_engine", execute)
    response = client.post(
        url + "/analyses",
        json={
            "stage_id": case["stages"][0]["id"],
            "question": "付款义务",
            "client_request_id": "fill-missing",
        },
    )
    assert response.status_code == 200, response.text
    fact = client.get(url).json()["facts"][0]
    assert fact["value"] == "2026-06-15"
    assert fact["confirmed"] is False
    assert fact["evidence_status"] == "unverified"
    assert fact["revision"] == 2


def test_task_fact_does_not_overwrite_a_model_fact_from_another_source(client, monkeypatch):
    from app.contexts.legal_analysis.domain.analysis_models import LegalFact
    from app.shared.domain.values import dump, now

    case, url = workspace(client)
    service = client.app.state.service

    def execute(owner, template, stage, question, facts, **options):
        result = fake_result(question)
        result["facts"] = [
            LegalFact(
                key="payment_due",
                label="付款到期日",
                value="2026-06-15",
                source="合同摘录",
            ).model_dump(mode="json")
        ]
        return result

    monkeypatch.setattr(service.legal, "execute_engine", execute)
    response = client.post(
        url + "/analyses",
        json={
            "stage_id": case["stages"][0]["id"],
            "question": "付款义务",
            "client_request_id": "model-then-task",
        },
    )
    assert response.status_code == 200, response.text
    with service.db.transaction() as conn:
        conn.execute(
            "INSERT INTO facts VALUES(?,?,?,?,?,?,?)",
            (case["id"], "payment_due", "到期日", dump("2026-06-20"), "task:payment", 1, now()),
        )
    fact = client.get(url).json()["facts"][0]
    assert fact["value"] == "2026-06-15"
    assert fact["source"] == "合同摘录"
    assert fact["evidence_status"] == "conflict"
    assert fact["confirmed"] is False
    assert fact["conflicts"] == [{"value": "2026-06-20", "source": "task:payment", "evidence_refs": []}]


def test_reclassified_issue_never_relabels_manual_paragraph(client):
    service = client.app.state.service.legal
    old = [
        {
            "id": "same-id",
            "issue_id": "issue-1",
            "title": "付款义务",
            "content": "付款人工判断",
            "generated_content": "旧建议",
            "suggested_content": None,
            "edited": True,
            "needs_review": False,
        }
    ]
    result = fake_result("交货义务")
    result["sections"] = [
        {"id": "same-id", "issue_id": "issue-1", "title": "交货义务", "content": "交货新建议"}
    ]
    result["issues"][0]["id"] = "issue-1"
    merged = service.merge_sections(old, result)
    assert len(merged) == 2
    preserved = next(item for item in merged if item["edited"])
    assert preserved["content"] == "付款人工判断"
    assert preserved["title"] == "付款义务"
    assert preserved["needs_review"]
    assert next(item for item in merged if not item["edited"])["content"] == "交货新建议"


def test_scoped_sources_and_revoked_evidence_need_review(client):
    from app.contexts.documents.domain.metadata import MetadataPatch
    from test_country_isolation import publish, stored

    case, url = workspace(client)
    service = client.app.state.service
    with closing(service.db.connect()) as conn:
        owner = conn.execute("SELECT owner_id FROM cases WHERE id=?", (case["id"],)).fetchone()[0]
    scope = service, owner, case["id"]
    china = stored(scope, "CN", identity="cn-law", body=b"tea payment China")
    indonesia = stored(scope, "ID", identity="id-law", body=b"tea payment Indonesia")
    singapore = stored(scope, "SG", identity="sg-law", body=b"tea payment Singapore")
    for doc in (china, indonesia, singapore):
        publish(scope, doc["id"])
    result = service.legal.search(owner, "tea payment", case["id"], case["template"], ["CN", "ID"], 5)
    assert {item["document_id"] for item in result["items"]} == {china["id"], indonesia["id"]}
    assert {item["country"] for item in result["items"]} == {"CN", "ID"}
    response = client.patch(
        url + "/facts/payment_due",
        json=fact_payload(evidence_status="verified", evidence_refs=[china["id"]], review_note="核对了原文"),
    )
    assert response.status_code == 200, response.text
    metadata = service.catalog.metadata_for(china["id"])
    service.catalog.patch_document(
        owner,
        china["id"],
        MetadataPatch(expected_revision=metadata["metadata_revision"], review_status="draft"),
    )
    fact = client.get(url).json()["facts"][0]
    assert fact["evidence_status"] == "unverified"
    assert fact["revision"] == 2
    assert (
        client.patch(
            url + "/facts/payment_due",
            json=fact_payload(
                revision=2,
                evidence_status="verified",
                evidence_refs=[china["id"]],
                review_note="旧审核已失效",
            ),
        ).status_code
        == 422
    )


def test_migration_from_version_eight_preserves_case_and_facts(client, tmp_path):
    from app.bootstrap import Service
    from app.shared.domain.values import dump, now

    case, _ = workspace(client)
    service = client.app.state.service
    with service.db.transaction() as conn:
        owner = conn.execute("SELECT owner_id FROM cases WHERE id=?", (case["id"],)).fetchone()[0]
        conn.execute(
            "INSERT INTO facts VALUES(?,?,?,?,?,?,?)",
            (case["id"], "payment_due", "付款日期", dump("2026-06-01"), "task:payment", 0, now()),
        )
        for name in (
            "legal_requests",
            "legal_analysis_versions",
            "legal_analyses",
            "legal_fact_versions",
            "legal_facts",
        ):
            conn.execute("DROP TABLE " + name)
        conn.execute("DELETE FROM schema_migrations WHERE version=9")
    reopened = Service(tmp_path)
    assert reopened.case_detail(owner, case["id"])["template_version"] == case["template_version"]
    assert reopened.legal.workspace(owner, case["id"])["facts"][0]["value"] == "2026-06-01"


def test_enabled_chat_uses_versioned_legal_analysis_and_sse_text(client):
    from app.contexts.legal_analysis.domain.analysis_models import default_legal_policy
    from app.shared.domain.values import dump

    case, url = workspace(client)
    template = case["template"]
    policy = default_legal_policy()
    policy["enabled"] = True
    template["agent"]["legal"] = policy
    for stage in template["stages"]:
        if stage.get("analysis"):
            stage["analysis"]["legal"] = policy
    with client.app.state.service.db.transaction() as conn:
        conn.execute("UPDATE cases SET template=? WHERE id=?", (dump(template), case["id"]))
    request = {
        "stage_id": case["stages"][0]["id"],
        "content": "付款义务如何判断",
        "client_message_id": "legal-chat",
    }
    route = f"/api/tea/cases/{case['id']}/messages/stream"
    response = client.post(route, json=request)
    assert response.status_code == 200
    assert "event: done" in response.text
    assert '"text"' in response.text
    assert client.get(url).json()["total"] == 1
    again = client.post(route, json=request)
    assert "event: done" in again.text
    assert client.get(url).json()["total"] == 1


def test_failed_public_step_is_kept_separate_from_resumable_checkpoint(client, monkeypatch):
    case, url = workspace(client)

    def execute(owner, template, stage, question, facts, **options):
        completed = fake_result(question)
        public = fake_result(question)
        public["trace"] = [
            {"id": "qa03", "type": "guide", "label": "法域判断", "status": "failed", "detail": "来源范围无效"}
        ]
        options["on_checkpoint"]("qa03", public, {"result": completed, "work": {}})
        raise ValueError("invalid model output")

    monkeypatch.setattr(client.app.state.service.legal, "execute_engine", execute)
    response = client.post(
        url + "/analyses",
        json={
            "stage_id": case["stages"][0]["id"],
            "question": "付款义务",
            "client_request_id": "failed-step",
        },
    )
    assert response.status_code == 502
    aid = client.get(url).json()["analyses"][0]["id"]
    failed = client.get(url + "/analyses/" + aid).json()
    assert failed["status"] == "failed"
    assert failed["result"]["trace"][-1]["status"] == "failed"
    with closing(client.app.state.service.db.connect()) as conn:
        import json

        stored = json.loads(
            conn.execute("SELECT payload FROM legal_analyses WHERE id=?", (aid,)).fetchone()[0]
        )
    assert stored["_checkpoint"]["state"]["result"]["trace"] == []
