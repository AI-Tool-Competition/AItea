"""Compose country/channel workflows from one maintained set of handbook tasks."""

import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from .models import AgentConfig, Mode, StrictModel, TaskDefinition, Template


class HandbookSource(StrictModel):
    filename: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    checked_on: str
    review_status: Literal["pending"]


class StageReference(StrictModel):
    id: str
    name: str
    actor: str
    description: str
    tasks: list[str] = Field(min_length=1, max_length=30)


class RouteDefinition(StrictModel):
    id: str
    title: str
    destination: str
    modes: list[Mode]
    stages: list[StageReference] = Field(min_length=1, max_length=20)


class WorkflowCatalog(StrictModel):
    version: int = Field(ge=1)
    source: HandbookSource
    description: str
    agent: AgentConfig
    tasks: dict[str, TaskDefinition] = Field(min_length=1, max_length=200)
    routes: list[RouteDefinition] = Field(min_length=1, max_length=20)


def load_workflow_catalog(path: Path) -> dict[str, dict]:
    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("文件不能超过 1 MB")
        catalog = WorkflowCatalog.model_validate(json.loads(path.read_text(encoding="utf-8")))
        if any(key != task.id for key, task in catalog.tasks.items()):
            raise ValueError("任务字典编号必须与任务编号一致")
        templates = {}
        used = set()
        for route in catalog.routes:
            if route.id in templates:
                raise ValueError("路线编号不可重复")
            stages = []
            for stage in route.stages:
                missing = set(stage.tasks) - catalog.tasks.keys()
                if missing:
                    raise ValueError(f"节点 {stage.id} 引用了不存在的任务：{', '.join(sorted(missing))}")
                used.update(stage.tasks)
                stages.append(
                    stage.model_dump()
                    | {
                        "tasks": [catalog.tasks[key].model_dump() for key in stage.tasks],
                    }
                )
            value = Template.model_validate(
                {
                    "id": route.id,
                    "version": catalog.version,
                    "title": route.title,
                    "description": (
                        f"{catalog.description}\n来源：{catalog.source.filename}；"
                        f"手册核查日期：{catalog.source.checked_on}；SHA-256：{catalog.source.sha256}。"
                        "手册为业务参考，监管适用与原文依据待法务复核。"
                    ),
                    "origin": "中国",
                    "destination": route.destination,
                    "is_demo": False,
                    "modes": [mode.model_dump() for mode in route.modes],
                    "stages": stages,
                    "agent": catalog.agent.model_dump(),
                }
            )
            templates[route.id] = value.model_dump()
        if used != catalog.tasks.keys():
            raise ValueError("存在未被任何路线使用的任务")
        return templates
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"流程目录无效（{path}）：{exc}") from exc
