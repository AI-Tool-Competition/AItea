"""Prepare attributed card edits; the chat transaction commits them after success."""

import re
from contextlib import closing

from app.shared.domain.errors import fail
from app.shared.domain.values import now


def prepare_card_updates(service, owner, case, question, updates):
    if re.search(r"(?:不要|不必|别).{0,8}(?:记录|记下|保存|填入|填写|录入)", question):
        fail(403, "card_fill_declined", "本轮已要求不记录业务信息，未填写业务卡")
    prepared, conflicts, rejected, accepted = [], [], [], []
    with closing(service.db.connect()) as conn:
        service.own_case(conn, owner, case["id"])
        for update in updates:
            _, task = service.get_task(case, update.task_id)
            definitions = {f["key"]: f for f in task["fields"]}
            values, sources = {}, {}
            for field in update.fields:
                definition = definitions.get(field.key)
                if not definition:
                    fail(422, "unknown_field", "只能填写业务卡已有字段，请使用上下文中的字段编号")
                message_id = field.source_message_id
                if message_id:
                    message = conn.execute(
                        "SELECT content FROM messages WHERE id=? AND case_id=? AND role='user'",
                        (message_id, case["id"]),
                    ).fetchone()
                    if not message:
                        fail(422, "invalid_card_source", "来源必须是当前业务中茶商的实际发言")
                    text = message["content"]
                else:
                    text = question
                quote = field.source_quote
                if not message_id and quote not in text:
                    message = conn.execute(
                        "SELECT id,content FROM messages WHERE case_id=? AND role='user' "
                        "AND instr(content,?)>0 ORDER BY created_at DESC,id DESC LIMIT 1",
                        (case["id"], quote),
                    ).fetchone()
                    if message:
                        message_id, text = message["id"], message["content"]
                if not str(field.value).strip():
                    continue
                if quote not in text or str(field.value).strip() not in quote:
                    rejected.append(
                        {
                            "task_id": task["id"],
                            "key": field.key,
                            "field": definition["label"],
                            "code": "unsupported_card_value",
                            "reason": "用户原话未支持这个填写值，已留空；请只摘录原文，不加说明或推断。",
                        }
                    )
                    continue
                if re.search(r"假设|假定|假如|举例|示例", text) or re.search(r"如果|比如", quote):
                    fail(422, "hypothetical_card_source", "假设和举例只用于本次分析，不写入实际业务卡")
                value = service.normalize_values(task, {field.key: field.value})[field.key]
                if value == "已核验完成":
                    fail(
                        422,
                        "card_verification_required",
                        "业务卡可以填写办理信息，核验结果需通过提交核验取得",
                    )
                old = task["values"].get(field.key)
                if old == value:
                    accepted.append({"task_id": task["id"], "key": field.key})
                    continue
                if old not in (None, "") and not (
                    field.replace_existing and re.search(r"改为|改成|更正|改到|调整为|以.{0,80}为准", quote)
                ):
                    conflicts.append(
                        {
                            "task_id": task["id"],
                            "key": field.key,
                            "task": task["title"],
                            "field": definition["label"],
                            "saved": old,
                            "new": value,
                        }
                    )
                    continue
                accepted.append({"task_id": task["id"], "key": field.key})
                values[field.key] = value
                sources[field.key] = {
                    "kind": "user_statement",
                    "source_message_id": message_id,
                    "quote": quote,
                    "updated_at": now(),
                }
            if values:
                prepared.append(
                    {
                        "task_id": task["id"],
                        "title": task["title"],
                        "expected_revision": task["revision"],
                        "values": values,
                        "field_sources": sources,
                    }
                )
    return {
        "card_updates": prepared,
        "accepted_fields": accepted,
        "conflicts": conflicts,
        "rejected_fields": rejected,
        "status": "pending_commit" if prepared else "unchanged",
        "notice": "仅暂存原话支持的信息；答复成功后与消息一起写入，供茶商修改和核对。不会确认材料、完成任务或解锁节点。冲突字段保留原值，需向茶商核对。",
    }


def commit_card_updates(service, conn, owner, case, updates, user_message, run_id):
    grouped = {}
    for update in updates:
        task_id = update["task_id"]
        merged = grouped.setdefault(task_id, {**update, "values": {}, "field_sources": {}})
        for key, value in update["values"].items():
            if key in merged["values"] and merged["values"][key] != value:
                fail(409, "conflicting_card_updates", "同一字段出现不同填写内容，请核对后重试")
            merged["values"][key] = value
            source = {**update["field_sources"][key], "agent_run_id": run_id}
            source["source_message_id"] = source.get("source_message_id") or user_message["id"]
            merged["field_sources"][key] = source
    for task_id, update in grouped.items():
        service.workspaces.patch_task(
            owner,
            case["id"],
            task_id,
            {"values": update["values"], "expected_revision": update["expected_revision"]},
            conn=conn,
            field_sources=update["field_sources"],
        )
    return [u["title"] for u in grouped.values()]
