"""Pure stage gates, optimistic revision checks and typed task input rules."""

import math

from app.shared.domain.errors import fail


def get_task(case, task_id, *, require_unlocked=True):
    for stage in case["stages"]:
        for task in stage["tasks"]:
            if task["id"] == task_id:
                if require_unlocked and stage["status"] == "locked":
                    fail(409, "stage_locked", "请先完成前序节点的必办任务")
                return stage, task
    fail(404, "not_found", "未找到任务")


def check_revision(task, expected):
    if task["revision"] != expected:
        fail(409, "stale_revision", "资料已经更新，请刷新后基于当前版本操作")


def normalize_values(task, supplied):
    definitions = {field["key"]: field for field in task["fields"]}
    values = dict(task["values"])
    for key, value in supplied.items():
        if key not in definitions:
            fail(422, "unknown_field", f"任务未定义字段：{key}")
        if isinstance(value, str) and len(value) > 10000:
            fail(422, "value_too_long", "单个字段内容不能超过 10000 字")
        if definitions[key]["type"] == "number" and value != "":
            try:
                value = float(value)
                if not math.isfinite(value):
                    raise ValueError()
                if value.is_integer():
                    value = int(value)
            except (TypeError, ValueError, OverflowError):
                fail(422, "invalid_number", f"{definitions[key]['label']}必须是有限数值")
        elif definitions[key]["type"] != "number":
            value = str(value).strip()
        if definitions[key]["type"] == "select" and value != "" and value not in definitions[key]["options"]:
            fail(422, "invalid_option", f"{definitions[key]['label']}必须选择配置中的选项")
        values[key] = value
    return values
