"""Load and validate the editable workflow file once when the service starts."""

import json
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from app.contexts.workspace.domain.models import Template
from app.contexts.workspace.infrastructure.workflow_catalog import load_workflow_catalog
from app.paths import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")
RETIRED_TEMPLATE_ID = "china-indonesia-demo"
BUILTIN_TEMPLATES = load_workflow_catalog(PROJECT_ROOT / "config/workflows/liubao-export.json")


def load_template(path=None):
    configured = path or os.getenv("TEA_TEMPLATE_FILE")
    # Existing .env files may still name the retired bundled demo file.
    # It no longer supplies a fifth route or creates old demonstration cases.
    if (
        not configured
        or (PROJECT_ROOT / configured).resolve()
        == (PROJECT_ROOT / "config/workflows/china-indonesia.json").resolve()
    ):
        return json.loads(json.dumps(BUILTIN_TEMPLATES["liubao-id-online"]))
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
if DEFAULT_TEMPLATE["id"] not in BUILTIN_TEMPLATES:
    raise ValueError("本地流程配置必须使用四条六堡茶路线之一的编号")
BUILTIN_TEMPLATES[DEFAULT_TEMPLATE["id"]] = DEFAULT_TEMPLATE
