import json
import logging
from contextlib import closing

from pydantic import ValidationError

from app.contexts.workspace.domain.models import Template
from app.contexts.workspace.infrastructure.defaults import BUILTIN_TEMPLATES, RETIRED_TEMPLATE_ID
from app.shared.domain.errors import fail
from app.shared.domain.values import dump, now

logger = logging.getLogger(__name__)


class TemplateService:
    def __init__(self, app):
        self.app = app

    def template(self, owner, template_id, conn=None):
        if template_id == RETIRED_TEMPLATE_ID:
            fail(404, "not_found", "旧演示流程已下线，请选择六堡茶流程")
        if conn is None:
            with closing(self.app.db.connect()) as db:
                return self.app.template(owner, template_id, db)
        row = conn.execute(
            "SELECT payload FROM templates WHERE owner_id=? AND id=? ORDER BY version DESC LIMIT 1",
            (owner, template_id),
        ).fetchone()
        if row:
            return json.loads(row["payload"])
        if template_id in BUILTIN_TEMPLATES:
            return json.loads(dump(BUILTIN_TEMPLATES[template_id]))
        fail(404, "not_found", "未找到流程模板")

    def templates(self, owner):
        with closing(self.app.db.connect()) as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT DISTINCT id FROM templates WHERE owner_id=? ORDER BY id", (owner,)
                )
            ]
            return [
                self.app.template(owner, id, conn)
                for id in dict.fromkeys([*BUILTIN_TEMPLATES, *ids])
                if id != RETIRED_TEMPLATE_ID
            ]

    def validate_template(self, payload):
        if payload.get("id") == RETIRED_TEMPLATE_ID:
            return {
                "valid": False,
                "errors": [{"path": "id", "message": "旧演示流程已下线，请选择六堡茶流程"}],
                "preview": {"stages": 0, "tasks": 0},
            }
        try:
            template = Template.model_validate(payload)
            return {
                "valid": True,
                "errors": [],
                "preview": {
                    "stages": len(template.stages),
                    "tasks": sum(len(stage.tasks) for stage in template.stages),
                },
            }
        except ValidationError as error:
            return {
                "valid": False,
                "errors": [
                    {"path": ".".join(str(p) for p in item["loc"]), "message": item["msg"]}
                    for item in error.errors()
                ],
                "preview": {"stages": 0, "tasks": 0},
            }

    def publish(self, owner, payload, expected_draft_revision=None):
        validation = self.app.validate_template(payload)
        if not validation["valid"]:
            fail(
                422,
                "invalid_template",
                "; ".join(f"{e['path']}: {e['message']}" for e in validation["errors"]),
            )
        template = Template.model_validate(payload).model_dump()
        with self.app.db.transaction() as conn:
            row = conn.execute(
                "SELECT MAX(version) n FROM templates WHERE owner_id=? AND id=?", (owner, template["id"])
            ).fetchone()
            base = BUILTIN_TEMPLATES.get(template["id"], {}).get("version", 0)
            # A previously published account version remains authoritative even
            # when a later application bundle raises its built-in version.
            latest = row["n"] if row["n"] is not None else base
            if template["version"] != (latest or 1):
                fail(409, "stale_template", "已发布配置已有更新，请载入最新版本后再发布")
            if expected_draft_revision is not None:
                draft = conn.execute(
                    "SELECT revision FROM template_drafts WHERE owner_id=? AND id=?", (owner, template["id"])
                ).fetchone()
                if expected_draft_revision != (draft["revision"] if draft else 0):
                    fail(409, "stale_draft", "草稿已有更新，请重新载入后再发布")
            template["version"] = latest + 1
            conn.execute(
                "INSERT INTO templates VALUES (?,?,?,?,?)",
                (owner, template["id"], template["version"], dump(template), now()),
            )
            if expected_draft_revision is not None:
                self.app.studio.write_draft(
                    conn, owner, template["id"], template, expected_draft_revision + 1
                )
        return template
