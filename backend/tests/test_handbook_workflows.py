"""Handbook route creation, conservative gates, and account/snapshot compatibility."""

import copy
import json

import pytest
from admin_helpers import grant_admin
from app.contexts.legal_analysis.domain.analysis_models import MAX_CASE_FACTS
from app.contexts.workflows.domain.models import FieldDefinition
from app.contexts.workflows.infrastructure.defaults import (
    BUILTIN_TEMPLATES,
    RETIRED_TEMPLATE_ID,
    load_template,
)
from app.contexts.workflows.infrastructure.workflow_catalog import load_workflow_catalog
from app.interfaces.http.factory import create_app
from app.configuration.paths import PROJECT_ROOT
from app.shared.domain.values import now
from fastapi.testclient import TestClient

ROUTES = [key for key in BUILTIN_TEMPLATES if key.startswith("liubao-")]
CATALOG = PROJECT_ROOT / "config/workflows/liubao-export.json"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        assert client.post("/api/tea/auth/demo", json={}).status_code == 200
        yield client


def test_only_four_handbook_routes_and_new_demo_case_are_available(client, monkeypatch):
    bootstrap = client.get("/api/tea/bootstrap").json()
    assert [item["id"] for item in bootstrap["templates"]] == [
        "liubao-id-online",
        "liubao-id-offline",
        "liubao-sg-online",
        "liubao-sg-offline",
    ]
    assert len(bootstrap["cases"]) == 1
    case = client.get(f"/api/tea/cases/{bootstrap['cases'][0]['id']}").json()
    assert case["product"] == "六堡茶"
    assert case["template"]["id"] == "liubao-id-online"
    assert client.get(f"/api/tea/templates/{RETIRED_TEMPLATE_ID}").status_code == 403
    grant_admin(client, bootstrap["user"]["id"])
    assert client.get(f"/api/tea/templates/{RETIRED_TEMPLATE_ID}").status_code == 404
    monkeypatch.setenv("TEA_TEMPLATE_FILE", "config/workflows/china-indonesia.json")
    assert load_template()["id"] == "liubao-id-online"


def test_old_demo_cases_published_templates_and_drafts_are_retired(client):
    bootstrap = client.get("/api/tea/bootstrap").json()
    owner = bootstrap["user"]["id"]
    grant_admin(client, owner)
    old_case = bootstrap["cases"][0]["id"]
    retired = dict(bootstrap["templates"][0], id=RETIRED_TEMPLATE_ID)
    payload = json.dumps(retired)
    with client.app.state.service.db.transaction() as conn:
        conn.execute("UPDATE cases SET template=? WHERE id=?", (payload, old_case))
        conn.execute(
            "INSERT INTO templates VALUES(?,?,?,?,?)", (owner, RETIRED_TEMPLATE_ID, 1, payload, now())
        )
        conn.execute(
            "INSERT INTO template_drafts VALUES(?,?,?,?,?)", (owner, RETIRED_TEMPLATE_ID, payload, 1, now())
        )
    assert client.get("/api/tea/cases").json() == {"items": [], "total": 0}
    assert client.get(f"/api/tea/cases/{old_case}").status_code == 404
    assert len(client.get("/api/tea/templates").json()["items"]) == 4
    assert client.get("/api/tea/templates/drafts").json() == {"items": [], "total": 0}
    assert client.get(f"/api/tea/templates/{RETIRED_TEMPLATE_ID}/draft").status_code == 404
    assert client.post("/api/tea/templates/publish", json={"template": retired}).status_code == 422
    assert (
        client.put(
            f"/api/tea/templates/{RETIRED_TEMPLATE_ID}/draft",
            json={"template": retired, "expected_revision": 1},
        ).status_code
        == 422
    )
    # A fresh handbook business can be created while the retired one is excluded.
    current = create(client, "liubao-sg-offline")
    listing = client.get("/api/tea/cases?limit=1").json()
    assert listing["total"] == 1 and listing["items"][0]["id"] == current["id"]


def create(client, route):
    template = next(
        item for item in client.get("/api/tea/bootstrap").json()["templates"] if item["id"] == route
    )
    response = client.post(
        "/api/tea/cases",
        json={
            "title": "六堡茶流程验收",
            "company": "测试企业",
            "product": "六堡茶",
            "mode_id": template["modes"][0]["id"],
            "template_id": route,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def values(task, result="已核验完成", include_optional=False):
    return {
        field["key"]: result if field["type"] == "select" else "验收数据：证据、责任人和依据已核对"
        for field in task["fields"]
        if field["required"] or include_optional
    }


def evidence(client, case):
    response = client.post(
        "/api/tea/documents",
        data={"case_id": case["id"]},
        files={
            "file": (
                "流程验收数据.txt",
                "仅用于自动化验证的业务材料，不是真实办理凭证。".encode(),
                "text/plain",
            ),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_route_journey_requires_evidence_and_unlocks_in_order(client):
    # The longest route includes independent exhibition closeout as well as serial gates.
    # All four route compositions/country differences are checked below without replaying the same engine.
    route = "liubao-id-offline"
    case = create(client, route)
    assert case["template"]["destination"] == ("印度尼西亚" if "-id-" in route else "新加坡")
    tasks = [task for stage in case["stages"] for task in stage["tasks"]]
    keys = [field["key"] for task in tasks for field in task["fields"]]
    assert len(keys) == len(set(keys))  # Shared facts cannot overwrite another task's record.
    doc = evidence(client, case)
    later = case["stages"][-1]["tasks"][0]
    later_url = f"/api/tea/cases/{case['id']}/tasks/{later['id']}"
    assert (
        client.post(later_url + "/verify", json={"expected_revision": 1, "confirm": True}).status_code == 409
    )
    closeout = next((t for t in tasks if t["id"] == "e08"), None)
    if closeout:
        assert (
            client.post(
                f"/api/tea/cases/{case['id']}/tasks/e08/verify",
                json={"expected_revision": 1, "confirm": True},
            ).status_code
            == 409
        )
    for index, stage in enumerate(case["stages"]):
        for task in stage["tasks"]:
            if task.get("blocks_next_stage") is False:
                continue
            url = f"/api/tea/cases/{case['id']}/tasks/{task['id']}"
            saved = client.patch(
                url,
                json={
                    "values": values(task, include_optional=True),
                    "evidence_ids": [doc],
                    "expected_revision": 1,
                },
            )
            assert saved.status_code == 200, saved.text
            response = client.post(url + "/verify", json={"expected_revision": 2, "confirm": True})
            assert response.status_code == 200, response.text
            current = response.json()
            assert (
                next(t for s in current["stages"] for t in s["tasks"] if t["id"] == task["id"])["status"]
                == "completed"
            )
        if index + 1 < len(case["stages"]):
            assert current["stages"][index + 1]["status"] == "active"
        if stage["id"] == "event_followup":
            assert current["stages"][index]["status"] == "follow_up"
            # Closeout starts after the event, independently of the sales order.
            started = client.patch(
                f"/api/tea/cases/{case['id']}/tasks/e08",
                json={
                    "values": values(closeout, "等待外部", include_optional=True),
                    "expected_revision": 1,
                },
            )
            assert started.status_code == 200, started.text
            current = started.json()
            assert current["stages"][index + 1]["status"] == "active"
    if closeout:
        assert current["completed_tasks"] == len(tasks) - 1
        assert current["current_stage_id"] == "event_followup"
        closeout_url = f"/api/tea/cases/{case['id']}/tasks/e08"
        waiting = next(t for s in current["stages"] for t in s["tasks"] if t["id"] == "e08")
        missing = client.post(
            closeout_url + "/verify", json={"expected_revision": waiting["revision"], "confirm": True}
        ).json()
        pending = next(t for s in missing["stages"] for t in s["tasks"] if t["id"] == "e08")
        assert pending["status"] == "needs_input"
        assert missing["completed_tasks"] == len(tasks) - 1
        saved = client.patch(
            closeout_url,
            json={
                "values": values(closeout, include_optional=True),
                "evidence_ids": [doc],
                "expected_revision": pending["revision"],
            },
        ).json()
        ready = next(t for s in saved["stages"] for t in s["tasks"] if t["id"] == "e08")
        current = client.post(
            closeout_url + "/verify", json={"expected_revision": ready["revision"], "confirm": True}
        ).json()
        assert all(s["status"] == "completed" for s in current["stages"])
    assert current["completed_tasks"] == current["total_tasks"] == len(tasks)
    # Full routes exceed the old 200-fact ceiling; late-stage analysis must still
    # include every entered field and must not silently discard business facts.
    legal_url = f"/api/tea/cases/{case['id']}/legal"
    workspace = client.get(legal_url).json()
    assert workspace["facts_truncated"] is False
    assert set(keys) <= {fact["key"] for fact in workspace["facts"]}
    analysis = client.post(
        legal_url + "/analyses",
        json={
            "stage_id": current["stages"][-1]["id"],
            "question": "整理业务结案仍需核对的事项",
            "client_request_id": "completed-handbook-analysis",
        },
    )
    assert analysis.status_code == 200, analysis.text
    # Rule-only analysis correctly asks for substantive legal review instead of
    # declaring the fixture's business documents legally sufficient.
    assert analysis.json()["status"] == "needs_input"
    assert analysis.json()["error"] is None
    assert set(keys) <= {fact["key"] for fact in analysis.json()["result"]["facts"]}
    if closeout:
        finished = next(t for s in current["stages"] for t in s["tasks"] if t["id"] == "e08")
        reopened = client.patch(
            closeout_url,
            json={
                "values": {"e08_exception": "仅展品核销补充，不改变另批正式订单"},
                "expected_revision": finished["revision"],
            },
        ).json()
        assert reopened["completed_tasks"] == len(tasks) - 1
        assert next(s for s in reopened["stages"] if s["id"] == "event_followup")["status"] == "follow_up"
        assert reopened["stages"][-1]["status"] == "completed"
    # A changed source record reopens completed dependent steps instead of erasing history.
    first = current["stages"][0]["tasks"][0]
    updated = client.patch(
        f"/api/tea/cases/{case['id']}/tasks/{first['id']}",
        json={
            "values": {first["fields"][0]["key"]: "责任主体发生变更"},
            "expected_revision": first["revision"],
        },
    ).json()
    assert updated["stages"][-1]["tasks"][0]["needs_review"] is True


def test_long_workflow_analysis_still_rejects_fact_overflow(client):
    case = create(client, "liubao-id-offline")
    service = client.app.state.service
    with service.db.transaction() as conn:
        conn.executemany(
            "INSERT INTO facts(case_id,key,label,value,source,confirmed,updated_at) VALUES(?,?,?,?,?,?,?)",
            [
                (case["id"], f"extra_{index}", "额外事实", '"记录"', "用户填写", 0, now())
                for index in range(MAX_CASE_FACTS + 1)
            ],
        )
    url = f"/api/tea/cases/{case['id']}/legal"
    workspace = client.get(url).json()
    assert workspace["facts_truncated"] is True
    assert len(workspace["facts"]) == MAX_CASE_FACTS
    response = client.post(
        url + "/analyses",
        json={
            "stage_id": case["stages"][0]["id"],
            "question": "超限不能忽略事实继续分析",
            "client_request_id": "overflow-analysis",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "fact_limit"


@pytest.mark.parametrize("result", ["未开始", "进行中", "等待外部", "待核验", "异常阻断"])
def test_pending_result_cannot_pass_even_with_document_and_confirmation(client, result):
    case = create(client, "liubao-id-online")
    task = case["stages"][0]["tasks"][0]
    doc = evidence(client, case)
    url = f"/api/tea/cases/{case['id']}/tasks/{task['id']}"
    assert (
        client.patch(
            url,
            json={
                "values": values(task, result),
                "evidence_ids": [doc],
                "expected_revision": 1,
            },
        ).status_code
        == 200
    )
    verified = client.post(url + "/verify", json={"expected_revision": 2, "confirm": True}).json()
    checked = verified["stages"][0]["tasks"][0]
    assert checked["status"] == "needs_input"
    assert any(
        item["id"] == "route_result" and item["status"] == "missing"
        for item in checked["verification"]["checks"]
    )
    assert verified["stages"][1]["status"] == "locked"


def test_completed_choice_alone_is_not_evidence_and_arbitrary_choices_are_rejected(client):
    case = create(client, "liubao-sg-online")
    task = case["stages"][0]["tasks"][0]
    url = f"/api/tea/cases/{case['id']}/tasks/{task['id']}"
    for option in ("经核验不适用", "随便通过"):
        assert (
            client.patch(url, json={"values": {"route_result": option}, "expected_revision": 1}).status_code
            == 422
        )
    client.patch(url, json={"values": values(task), "expected_revision": 1})
    result = client.post(url + "/verify", json={"expected_revision": 2, "confirm": True}).json()
    assert result["stages"][0]["tasks"][0]["status"] == "needs_input"
    assert result["completed_tasks"] == 0


def test_builtin_publish_is_account_scoped_revisioned_and_preserves_existing_cases(client, tmp_path):
    case = create(client, "liubao-id-offline")
    grant_admin(client, client.get("/api/tea/auth/me").json()["id"])
    original = case["template"]
    changed = copy.deepcopy(original)
    changed["stages"][0]["name"] = "本账号定制准备"
    response = client.post("/api/tea/templates/publish", json={"template": changed})
    assert response.status_code == 200, response.text
    assert response.json()["version"] == original["version"] + 1
    assert client.post("/api/tea/templates/publish", json={"template": changed}).status_code == 409
    with TestClient(create_app(data_dir=tmp_path)) as restarted:
        restarted.cookies.update(client.cookies)
        assert restarted.get(f"/api/tea/cases/{case['id']}").json()["template"] == original
        assert (
            restarted.get("/api/tea/templates/liubao-id-offline").json()["version"] == original["version"] + 1
        )
    client.post("/api/tea/auth/logout")
    other = client.post("/api/tea/auth/demo", json={}).json()
    assert client.get("/api/tea/templates/liubao-id-offline").status_code == 403
    grant_admin(client, other["id"])
    assert client.get("/api/tea/templates/liubao-id-offline").json()["version"] == original["version"]
    assert client.get(f"/api/tea/cases/{case['id']}").status_code == 404


def test_catalog_routes_keep_country_and_channel_requirements_separate():
    for route in ROUTES:
        template = BUILTIN_TEMPLATES[route]
        stages = {
            task["id"]: index for index, stage in enumerate(template["stages"]) for task in stage["tasks"]
        }
        assert stages["f07_inspection"] < stages["f07_clearance"] < stages["f08"]
        assert stages["f10"] < stages["f11"] < stages["f12_settlement"]
        if "-id-" in route:
            assert not any(task.startswith("sg") for task in stages)
            assert stages["id04"] < stages["f03"]
            assert stages["id05_lpph"] < stages["f08"]
            assert stages["id06"] < stages["f10"]
        else:
            assert not any(task.startswith("id") for task in stages)
            assert stages["sg01"] < stages["sg03"] < stages["f08"]
        assert ("e08" in stages) == route.endswith("offline")
        assert ("o02" in stages) == route.endswith("online")
        if "e08" in stages:
            assert stages["e06"] < stages["e08"] == stages["e07"] < stages["f01"]
        else:
            assert stages["p05"] < stages["p06"] < stages["o04"]


@pytest.mark.parametrize("change", ["missing_reference", "duplicate_route", "unused_task"])
def test_invalid_catalog_fails_visibly(tmp_path, change):
    catalog = json.loads(CATALOG.read_text())
    if change == "missing_reference":
        catalog["routes"][0]["stages"][0]["tasks"].append("unknown")
    elif change == "duplicate_route":
        catalog["routes"].append(catalog["routes"][0])
    else:
        catalog["tasks"]["unused"] = dict(catalog["tasks"]["p01"], id="unused")
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(catalog))
    with pytest.raises(ValueError, match="流程目录无效"):
        load_workflow_catalog(path)


@pytest.mark.parametrize(
    "changes",
    [
        {"options": None},
        {"options": ["完成", "完成"]},
        {"accepted_values": ["未配置"]},
        {"required": False},
        {"type": "text"},
    ],
)
def test_invalid_select_rules_are_rejected(changes):
    with pytest.raises(ValueError):
        FieldDefinition.model_validate(
            {
                "key": "result",
                "label": "结果",
                "type": "select",
                "required": True,
                "options": ["待核验", "完成"],
                "accepted_values": ["完成"],
                **changes,
            }
        )
