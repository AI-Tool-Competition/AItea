"""Load and validate the editable workflow file once when the service starts."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from .models import Template

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def load_template(path=None):
    configured = path or os.getenv("TEA_TEMPLATE_FILE") or "config/workflows/china-indonesia.json"
    file = Path(configured).expanduser()
    if not file.is_absolute():
        file = PROJECT_ROOT / file
    try:
        if file.stat().st_size > 1024 * 1024:
            raise ValueError("文件不能超过 1 MB")
        return Template.model_validate(json.loads(file.read_text(encoding="utf-8"))).model_dump()
    except (OSError, UnicodeError, ValueError, ValidationError) as exc:
        raise ValueError(f"流程配置无效（{file}）：{exc}") from exc


DEFAULT_TEMPLATE = load_template()
