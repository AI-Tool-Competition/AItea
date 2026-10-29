"""Budgets for the main-model evidence loop; no intermediate task representation."""

import os
from dataclasses import dataclass
from math import isfinite

from app.contexts.conversations.application.tool_runtime import ToolFailure


@dataclass(frozen=True)
class ExecutionBudget:
    total: float = 180
    model: float = 120
    evidence: float = 45
    answer_reserve: float = 60
    retries: int = 1
    discovery: int = 2
    tokens: int = 12000

    @classmethod
    def configured(cls):
        values = {}
        for field, default in cls().__dict__.items():
            try:
                value = float(os.getenv(f"AGENT_BUDGET_{field.upper()}", str(default)))
            except ValueError:
                raise ToolFailure("not_configured", "Agent预算必须为有效数字。") from None
            maximum = {"retries": 1, "discovery": 4, "tokens": 24000}.get(field, 900)
            if (
                not isfinite(value)
                or not 0 <= value <= maximum
                or (field in {"retries", "discovery", "tokens"} and not value.is_integer())
            ):
                raise ToolFailure("not_configured", "Agent预算超出允许范围。")
            values[field] = int(value) if field in {"retries", "discovery", "tokens"} else value
        if (
            min(values[k] for k in ("total", "model", "evidence", "answer_reserve")) <= 0
            or values["answer_reserve"] >= values["total"]
            or values["tokens"] < 1024
        ):
            raise ToolFailure("not_configured", "时间预算必须为正，并为取证保留时间。")
        return cls(**values)
