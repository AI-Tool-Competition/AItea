import json
import logging
import re
from contextlib import closing

from app.shared.domain.errors import fail
from app.shared.domain.values import dump, now, uid

logger = logging.getLogger(__name__)


class ArtifactService:
    def __init__(self, app):
        self.app = app

    def artifact_dict(self, row):
        result = {
            key: row[key]
            for key in ("id", "case_id", "task_id", "title", "content", "version", "created_at", "updated_at")
        }
        result["confirmed"] = bool(row["confirmed"])
        return result

    def own_artifact(self, conn, owner, id):
        row = conn.execute("SELECT * FROM artifacts WHERE id=? AND owner_id=?", (id, owner)).fetchone()
        if not row:
            fail(404, "not_found", "未找到文稿")
        return row

    def create_artifact(self, owner, case_id, payload, *, content=None, conn=None):
        if conn is None:
            with self.app.db.transaction() as transaction:
                return self.app.create_artifact(owner, case_id, payload, content=content, conn=transaction)
        if conn is not None:
            case = self.app.case_detail(owner, case_id, conn)
            _, task = self.app.get_task(case, payload["task_id"])
            template = task.get("output_template") or "# " + task["title"] + "\n\n" + "\n\n".join(
                f"{f['label']}：{{{{{f['key']}}}}}" for f in task["fields"]
            )
            facts = {f["key"]: f["value"] for f in case["facts"]}
            facts.update(task["values"])
            content = (
                content
                if content is not None
                else re.sub(
                    r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}",
                    lambda match: str(facts.get(match[1]) or f"【待填写：{match[1]}】"),
                    template,
                )
            )
            id, at = uid(), now()
            conn.execute(
                "INSERT INTO artifacts VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    id,
                    owner,
                    case_id,
                    task["id"],
                    payload.get("title") or task["title"] + " · 草稿",
                    content,
                    1,
                    0,
                    at,
                    at,
                ),
            )
            artifact = self.app.artifact_dict(self.app.own_artifact(conn, owner, id))
            conn.execute("INSERT INTO artifact_versions VALUES (?,?,?)", (id, 1, dump(artifact)))
            self.app.touch(conn, case_id)
            self.app.event(conn, case_id, "artifact_created", "生成材料草稿", artifact["title"])
            return artifact

    def patch_artifact(self, owner, id, payload):
        with self.app.db.transaction() as conn:
            row = self.app.own_artifact(conn, owner, id)
            if row["version"] != payload["expected_version"]:
                fail(409, "stale_version", "文稿已有新版本，请刷新后再修改")
            conn.execute(
                "UPDATE artifacts SET title=?,content=?,version=version+1,confirmed=0,updated_at=? WHERE id=?",
                (payload["title"], payload["content"], now(), id),
            )
            artifact = self.app.artifact_dict(self.app.own_artifact(conn, owner, id))
            conn.execute(
                "INSERT INTO artifact_versions VALUES (?,?,?)", (id, artifact["version"], dump(artifact))
            )
            self.app.invalidate_task(
                conn, row["case_id"], row["task_id"], reason="关联文稿已修改，请复核当前任务。"
            )
            self.app.touch(conn, row["case_id"])
            self.app.event(
                conn,
                row["case_id"],
                "artifact_updated",
                "保存文稿版本",
                f"{artifact['title']} · 第 {artifact['version']} 版",
            )
            return artifact

    def confirm_artifact(self, owner, id, version):
        with self.app.db.transaction() as conn:
            row = self.app.own_artifact(conn, owner, id)
            if row["version"] != version:
                fail(409, "stale_version", "文稿已有新版本，请刷新后确认")
            conn.execute("UPDATE artifacts SET confirmed=1,updated_at=? WHERE id=?", (now(), id))
            artifact = self.app.artifact_dict(self.app.own_artifact(conn, owner, id))
            conn.execute(
                "UPDATE artifact_versions SET payload=? WHERE artifact_id=? AND version=?",
                (dump(artifact), id, version),
            )
            self.app.touch(conn, row["case_id"])
            return artifact

    def versions(self, owner, artifact_id):
        with closing(self.app.db.connect()) as conn:
            self.own_artifact(conn, owner, artifact_id)
            items = [
                json.loads(row["payload"])
                for row in conn.execute(
                    "SELECT payload FROM artifact_versions WHERE artifact_id=? ORDER BY version DESC LIMIT 200",
                    (artifact_id,),
                )
            ]
            return {"items": items, "total": len(items)}

    def download(self, owner, artifact_id):
        with closing(self.app.db.connect()) as conn:
            return dict(self.own_artifact(conn, owner, artifact_id))
