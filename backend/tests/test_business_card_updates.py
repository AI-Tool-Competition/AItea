"""Conversation fills stay attributed, provisional and atomic with the final answer."""

import json
from contextlib import closing

import pytest
from app.contexts.conversations.application.agent_tools import CardUpdateInput
from app.contexts.conversations.application.tool_runtime import ToolFailure
from app.contexts.workflows.application.card_updates import prepare_card_updates
from app.shared.application.streaming import StreamCancelled
from app.shared.domain.errors import AppError
from app.shared.infrastructure.persistence.database import Database
from test_agent_runtime import last_run, scenario, send, tools
from test_agent_runtime import workspace as runtime_workspace
from test_chat_stream import frames

workspace = runtime_workspace


def card_args(value="六堡茶", quote="这批产品是六堡茶", **extra):
    return {
        "updates": [
            {
                "task_id": "product_profile",
                "fields": [{"key": "product_name", "value": value, "source_quote": quote, **extra}],
            }
        ]
    }


def card_tools(args):
    return tools(
        *[
            (
                "update_business_card",
                {
                    "task_id": update["task_id"],
                    "values_json": json.dumps({field["key"]: str(field["value"])}),
                },
            )
            for update in args["updates"]
            for field in update["fields"]
        ]
    )


def prepare(workspace, args, question="这批产品是六堡茶"):
    _, service, owner, case, _ = workspace
    return prepare_card_updates(
        service, owner, case, question, [CardUpdateInput.model_validate(update) for update in args["updates"]]
    )


def test_plain_statement_fills_multiple_cards_in_stream_and_replays_once(workspace, monkeypatch):
    _, service, owner, case, payload = workspace
    question = "这批产品是六堡茶，每袋250克。企业名称为梧州茶行，联系人是李明。"
    args = card_args(quote=question)
    args["updates"][0]["fields"].append({"key": "net_weight_g", "value": 250, "source_quote": "每袋250克"})
    args["updates"].append(
        {
            "task_id": "company_profile",
            "fields": [
                {"key": "company_name", "value": "梧州茶行", "source_quote": "企业名称为梧州茶行"},
                {"key": "contact_name", "value": "李明", "source_quote": "联系人是李明"},
            ],
        }
    )
    scenario(
        monkeypatch,
        [card_tools(args), "先整理这批茶的信息。准备发往哪个国家？"],
        auto_discover=False,
    )
    payload = {**payload, "content": question}
    events = frames(send(workspace, payload, stream=True))
    assert events[-1][0] == "done", events
    result = events[-1][1]
    task, company = result["case"]["stages"][0]["tasks"]
    assert task["values"] == {"product_name": "六堡茶", "net_weight_g": 250}
    assert company["values"] == {"company_name": "梧州茶行", "contact_name": "李明"}
    source = task["field_sources"]["product_name"]
    assert source["quote"] == question
    assert source["source_message_id"] == result["messages"][0]["id"]
    assert source["agent_run_id"] == last_run(workspace)["_run_id"]
    assert source["agent_run_id"]
    assert not task["confirmed"] and task["status"] != "completed"
    assert result["case"]["completed_tasks"] == case["completed_tasks"]
    assert result["case"]["stages"][1]["status"] == "locked"
    assert any(
        t["name"] == "update_business_card"
        for step in result["messages"][1]["trace"]
        for t in step.get("tools", [])
    )
    assert send(workspace, payload).json()["messages"] == result["messages"]
    assert service.case_detail(owner, case["id"])["stages"][0]["tasks"][0]["revision"] == task["revision"]


@pytest.mark.parametrize(
    "args,question,code",
    [
        (card_args(quote="假设这批产品是六堡茶"), "假设这批产品是六堡茶", "hypothetical_card_source"),
        (card_args(), "不要记录这批产品是六堡茶", "card_fill_declined"),
        (
            {
                "updates": [
                    {
                        "task_id": "product_profile",
                        "fields": [{"key": "fake", "value": "六堡茶", "source_quote": "六堡茶"}],
                    }
                ]
            },
            "六堡茶",
            "unknown_field",
        ),
        (card_args(source_message_id="foreign"), "这批产品是六堡茶", "invalid_card_source"),
    ],
)
def test_untrusted_or_declined_values_do_not_write(workspace, args, question, code):
    with pytest.raises(AppError) as caught:
        prepare(workspace, args, question)
    assert caught.value.code == code
    assert not workspace[1].case_detail(workspace[2], workspace[3]["id"])["stages"][0]["tasks"][0]["values"]


def test_prior_user_message_can_supply_quote_but_assistant_or_other_case_cannot(workspace):
    _, service, owner, case, _ = workspace
    with service.db.transaction() as conn:
        old = service.insert_message(
            conn, case["id"], case["stages"][0]["id"], None, "user", "这批产品是六堡茶", [], []
        )
        assistant = service.insert_message(
            conn, case["id"], case["stages"][0]["id"], None, "assistant", "这批产品是红茶", [], []
        )
    result = prepare(workspace, card_args(), "接着安排吧")
    assert result["card_updates"][0]["field_sources"]["product_name"]["source_message_id"] == old["id"]
    with pytest.raises(AppError):
        prepare(workspace, card_args("红茶", "这批产品是红茶", source_message_id=assistant["id"]))
    foreign = service.create_case(
        owner, {"title": "另一业务", "template_id": case["template"]["id"], "mode_id": case["mode_id"]}
    )
    with service.db.transaction() as conn:
        other = service.insert_message(
            conn, foreign["id"], foreign["stages"][0]["id"], None, "user", "这批产品是六堡茶", [], []
        )
    with pytest.raises(AppError):
        prepare(workspace, card_args(source_message_id=other["id"]))


def test_conflict_keeps_saved_value_and_explicit_correction_replaces_it(workspace, monkeypatch):
    _, service, owner, case, payload = workspace
    service.patch_task(
        owner,
        case["id"],
        "product_profile",
        {"values": {"product_name": "红茶", "bag_count": 10}, "expected_revision": 1},
    )
    current = service.case_detail(owner, case["id"])
    args = [CardUpdateInput.model_validate(update) for update in card_args()["updates"]]
    conflict = prepare_card_updates(service, owner, current, "这批产品是六堡茶", args)
    assert not conflict["card_updates"] and conflict["conflicts"][0]["saved"] == "红茶"
    scenario(
        monkeypatch,
        [
            card_tools(card_args(quote="产品改为六堡茶", replace_existing=True)),
            "已经记下更正，其他信息保留。",
        ],
    )
    response = send(workspace, {**payload, "content": "产品改为六堡茶"})
    assert response.status_code == 200, response.text
    task = response.json()["case"]["stages"][0]["tasks"][0]
    assert task["values"] == {"product_name": "六堡茶", "bag_count": 10}
    changed = service.patch_task(
        owner, case["id"], task["id"], {"values": {"bag_count": 20}, "expected_revision": task["revision"]}
    )["stages"][0]["tasks"][0]
    assert "product_name" in changed["field_sources"]
    manual = service.patch_task(
        owner,
        case["id"],
        task["id"],
        {"values": {"product_name": "乌龙茶"}, "expected_revision": changed["revision"]},
    )["stages"][0]["tasks"][0]
    assert "product_name" not in manual["field_sources"] and not manual["confirmed"]


@pytest.mark.parametrize("failure", [StreamCancelled(), ToolFailure("timeout", "timeout"), "revision"])
def test_failed_cancelled_or_stale_turn_does_not_commit_cards(workspace, monkeypatch, failure):
    _, service, owner, case, payload = workspace

    def mutate():
        with service.db.transaction() as conn:
            service.touch(conn, case["id"])
        return "已整理"

    scenario(
        monkeypatch,
        [card_tools(card_args()), mutate if failure == "revision" else failure],
    )
    with pytest.raises((AppError, StreamCancelled)):
        service.post_message(owner, case["id"], {**payload, "content": "这批产品是六堡茶"})
    assert not service.case_detail(owner, case["id"])["stages"][0]["tasks"][0]["values"]


def test_schema27_upgrade_preserves_values_and_messages_and_is_repeatable(workspace):
    _, service, owner, case, _ = workspace
    service.patch_task(
        owner, case["id"], "product_profile", {"values": {"product_name": "六堡茶"}, "expected_revision": 1}
    )
    with service.db.transaction() as conn:
        message = service.insert_message(
            conn, case["id"], case["stages"][0]["id"], None, "user", "既有消息", [], []
        )
        conn.execute("ALTER TABLE task_states DROP COLUMN field_sources_json")
        conn.execute("DELETE FROM schema_migrations WHERE version=28")
    Database(service.db.data_dir)
    Database(service.db.data_dir)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations WHERE version=28").fetchone()[0] == 1
        assert (
            conn.execute("SELECT content FROM messages WHERE id=?", (message["id"],)).fetchone()[0]
            == "既有消息"
        )
    task = service.case_detail(owner, case["id"])["stages"][0]["tasks"][0]
    assert task["values"] == {"product_name": "六堡茶"} and task["field_sources"] == {}


def test_explicit_correction_invalidates_old_confirmation(workspace, monkeypatch):
    _, service, owner, case, payload = workspace
    updated = service.patch_task(
        owner,
        case["id"],
        "company_profile",
        {"values": {"company_name": "梧州茶行", "contact_name": "李明"}, "expected_revision": 1},
    )
    task = updated["stages"][0]["tasks"][1]
    verified = service.verify_task(
        owner, case["id"], task["id"], {"expected_revision": task["revision"], "confirm": True}
    )
    assert verified["stages"][0]["tasks"][1]["confirmed"]
    args = {
        "updates": [
            {
                "task_id": "company_profile",
                "fields": [
                    {
                        "key": "contact_name",
                        "value": "张三",
                        "source_quote": "联系人改为张三",
                        "replace_existing": True,
                    }
                ],
            }
        ]
    }
    scenario(monkeypatch, [card_tools(args), "已记下联系人变更，请核对。"])
    response = send(workspace, {**payload, "content": "联系人改为张三"})
    assert response.status_code == 200, response.text
    task = response.json()["case"]["stages"][0]["tasks"][1]
    assert not task["confirmed"] and task["status"] != "completed"
    assert task["values"]["contact_name"] == "张三"


def test_card_fill_rejects_locked_task_and_does_not_change_evidence(workspace):
    _, service, owner, case, _ = workspace
    locked = case["stages"][1]["tasks"][0]
    field = locked["fields"][0]
    with pytest.raises(AppError) as caught:
        prepare(
            workspace,
            {
                "updates": [
                    {
                        "task_id": locked["id"],
                        "fields": [{"key": field["key"], "value": "六堡茶", "source_quote": "六堡茶"}],
                    }
                ]
            },
            "六堡茶",
        )
    assert caught.value.code == "stage_locked"
    assert service.case_detail(owner, case["id"])["stages"][0]["tasks"][0]["evidence_ids"] == []


def test_batch_retains_supported_fields_and_leaves_invented_description_empty(workspace, monkeypatch):
    args = {
        "task_id": "product_profile",
        "values_json": json.dumps(
            {"product_name": "六堡茶", "net_weight_g": "250", "ingredients": "茶叶（无添加）"}
        ),
    }
    scenario(monkeypatch, [tools(("update_business_card", args)), "先确认准备销售的国家，再安排后续事项。"])
    response = send(workspace, {**workspace[4], "content": "产品是六堡茶，每袋250克"})
    assert response.status_code == 200, response.text
    task = response.json()["case"]["stages"][0]["tasks"][0]
    assert task["values"] == {"product_name": "六堡茶", "net_weight_g": 250}
    assert task["field_sources"]["product_name"]["quote"] == "产品是六堡茶，每袋250克"
    assert task["field_sources"]["product_name"]["source_message_id"] == response.json()["messages"][0]["id"]
    assert "ingredients" not in task["values"]


def test_card_round_does_not_consume_two_legal_evidence_rounds(workspace, monkeypatch):
    original_tools = tools
    count = 0

    def unique_tools(*pairs):
        nonlocal count
        count += 1
        response = original_tools(*pairs)
        for call in response["tool_calls"]:
            call["id"] += f"_{count}"
        return response

    scenario(
        monkeypatch,
        [
            unique_tools(
                (
                    "update_business_card",
                    {"task_id": "product_profile", "values_json": '{"product_name":"六堡茶"}'},
                )
            ),
            unique_tools(
                (
                    "update_business_card",
                    {"task_id": "company_profile", "values_json": '{"contact_name":"李明"}'},
                )
            ),
            unique_tools(
                (
                    "update_business_card",
                    {"task_id": "product_profile", "values_json": '{"net_weight_g":"250"}'},
                )
            ),
            "先找潜在客户，确定国家后再核对手续。",
        ],
        auto_discover=False,
    )
    response = send(workspace, {**workspace[4], "content": "联系人是李明，产品是六堡茶，每袋250克"})
    assert response.status_code == 200, response.text
    product, company = response.json()["case"]["stages"][0]["tasks"]
    assert product["values"] == {"product_name": "六堡茶", "net_weight_g": 250}
    assert company["values"]["contact_name"] == "李明"


def test_unknown_empty_values_do_not_erase_saved_fields_or_create_provenance(workspace, monkeypatch):
    _, service, owner, case, payload = workspace
    service.patch_task(
        owner, case["id"], "product_profile", {"values": {"product_name": "六堡茶"}, "expected_revision": 1}
    )
    scenario(
        monkeypatch,
        [
            tools(
                (
                    "update_business_card",
                    {"task_id": "product_profile", "values_json": '{"product_name":"", "bag_count":""}'},
                )
            ),
            "原有信息保留，先确认实际库存。",
        ],
    )
    response = send(workspace, {**payload, "content": "库存安排改为待定，其他还没确定"})
    assert response.status_code == 200, response.text
    task = response.json()["case"]["stages"][0]["tasks"][0]
    assert task["values"] == {"product_name": "六堡茶"}
    assert task["field_sources"] == {}


@pytest.mark.parametrize("corrected", [False, True])
def test_unwritten_card_fields_remain_partial_until_supported_value_is_provided(
    workspace, monkeypatch, corrected
):
    _, _, _, _, payload = workspace
    steps = [
        tools(
            (
                "update_business_card",
                {"task_id": "product_profile", "values_json": '{"product_name":"六堡茶，纯茶"}'},
            )
        )
    ]
    if corrected:
        repair = tools(
            (
                "update_business_card",
                {"task_id": "product_profile", "values_json": '{"product_name":"六堡茶"}'},
            )
        )
        repair["tool_calls"][0]["id"] = "correct-card"
        steps.append(repair)
    steps.append("先确认计划销售的国家。")
    seen = []
    scenario(monkeypatch, steps, seen)
    response = send(workspace, {**payload, "content": "现有六堡茶，请填写产品信息，未知项留空。"})
    assert response.status_code == 200
    result = last_run(workspace)
    assert result["_task_state"]["model_calls"] == (3 if corrected else 2)
    assert result["_task_state"]["delivery"] == ("answered" if corrected else "partially_answered")
    assert ("部分业务信息尚未写入" in result["output"]) is (not corrected)
    task = response.json()["case"]["stages"][0]["tasks"][0]
    assert task["values"] == ({"product_name": "六堡茶"} if corrected else {})
