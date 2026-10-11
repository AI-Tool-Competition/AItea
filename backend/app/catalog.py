"""Review and applicability metadata, kept separate from immutable document bytes."""

from contextlib import closing
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .db import dump, now


class DocumentMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    kind: Literal["law", "guidance", "case", "business", "demo"] = "business"
    jurisdiction: str = Field(default="", max_length=100)
    language: str = Field(default="", max_length=50)
    published_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    review_status: Literal["draft", "published"] = "published"

    @model_validator(mode="after")
    def valid_interval(self):
        if self.effective_from and self.effective_to and self.effective_from > self.effective_to:
            raise ValueError("生效起始日期不能晚于结束日期")
        return self


class MetadataPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    expected_revision: int = Field(ge=0)
    kind: Literal["law", "guidance", "case", "business", "demo"] | None = None
    jurisdiction: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, max_length=50)
    published_at: date | None = None
    effective_from: date | None = None
    effective_to: date | None = None
    review_status: Literal["draft", "published"] | None = None


def eligible(metadata: dict, as_of: date) -> bool:
    """An approval is necessary, and known effective dates bound applicability."""
    if metadata.get("review_status") != "published":
        return False
    start, end = metadata.get("effective_from"), metadata.get("effective_to")
    return (not start or date.fromisoformat(start) <= as_of) and (not end or as_of <= date.fromisoformat(end))


class LegalCatalog:
    def __init__(self, db):
        self.db = db
        with self.db.transaction() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS document_metadata(
                document_id TEXT PRIMARY KEY REFERENCES documents(id),
                payload TEXT NOT NULL, revision INTEGER NOT NULL,
                reviewed_by TEXT REFERENCES users(id), reviewed_at TEXT, updated_at TEXT NOT NULL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS document_metadata_history(
                document_id TEXT NOT NULL REFERENCES documents(id), revision INTEGER NOT NULL,
                payload TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                PRIMARY KEY(document_id,revision))""")
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES (?,?)", (2, now()))

    def metadata_for(self, document_id, *, source_url=None, conn=None):
        if conn is None:
            with closing(self.db.connect()) as db:
                return self.metadata_for(document_id, source_url=source_url, conn=db)
        import json

        row = conn.execute("SELECT * FROM document_metadata WHERE document_id=?", (document_id,)).fetchone()
        if row:
            return {
                "metadata": json.loads(row["payload"]),
                "metadata_revision": row["revision"],
                "reviewed_at": row["reviewed_at"],
            }
        # Unknown fetched sources are never treated as approved legal evidence.
        metadata = DocumentMetadata(kind="law", review_status="draft") if source_url else DocumentMetadata()
        return {"metadata": metadata.model_dump(mode="json"), "metadata_revision": 0, "reviewed_at": None}

    def enrich_document(self, document, conn=None):
        return document | self.metadata_for(document["id"], source_url=document.get("source_url"), conn=conn)

    def patch_document(self, owner, document_id, payload: MetadataPatch, on_changed=None):
        from .service import fail

        with self.db.transaction() as conn:
            doc = conn.execute(
                "SELECT * FROM documents WHERE id=? AND owner_id=?", (document_id, owner)
            ).fetchone()
            if not doc:
                fail(404, "not_found", "未找到资料")
            if doc["role"] == "secondary":
                fail(422, "secondary_metadata", "关联附件仅保存原件，不进入法规审核与检索")
            current = self.metadata_for(document_id, source_url=doc["source_url"], conn=conn)
            if payload.expected_revision != current["metadata_revision"]:
                fail(409, "stale_metadata", "资料审核信息已有更新，请刷新后再保存")
            changes = payload.model_dump(mode="json", exclude_unset=True, exclude={"expected_revision"})
            try:
                merged = DocumentMetadata.model_validate(current["metadata"] | changes).model_dump(
                    mode="json"
                )
            except ValidationError:
                fail(422, "invalid_metadata", "请检查资料类别、审核状态及生效日期范围")
            if merged == current["metadata"]:
                return current
            # Editing approved applicability data requires an explicit renewed approval.
            if "review_status" not in changes:
                merged["review_status"] = "draft"
            revision, at = current["metadata_revision"] + 1, now()
            approved = merged["review_status"] == "published"
            conn.execute(
                """INSERT INTO document_metadata VALUES(?,?,?,?,?,?)
                ON CONFLICT(document_id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,
                reviewed_by=excluded.reviewed_by,reviewed_at=excluded.reviewed_at,updated_at=excluded.updated_at""",
                (
                    document_id,
                    dump(merged),
                    revision,
                    owner if approved else None,
                    at if approved else None,
                    at,
                ),
            )
            conn.execute(
                "INSERT INTO document_metadata_history VALUES(?,?,?,?,?)",
                (document_id, revision, dump(merged), owner, at),
            )
            if on_changed:
                on_changed(conn, doc)
            return self.metadata_for(document_id, conn=conn)
