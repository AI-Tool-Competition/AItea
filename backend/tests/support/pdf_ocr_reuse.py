"""Historical fixture builders for version/migration and preview contracts."""

from __future__ import annotations

import json
from contextlib import closing

import pytest
from app.bootstrap import Service
from app.contexts.knowledge.infrastructure.parsing import parsing as parsing
from app.contexts.knowledge.infrastructure.parsing.native import parse_file
from app.contexts.knowledge.infrastructure.providers import vision as vision
from app.contexts.knowledge.infrastructure.recovery import pdf_ocr_reuse as pdf_ocr_reuse
from app.shared.domain.values import dump, now

SOURCE = "https://official.invalid/acfta/full-original.pdf"


@pytest.fixture
def service(tmp_path):
    result = Service(tmp_path / "owned")
    with result.db.transaction() as conn:
        for owner in ("owner", "other"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 1, now()))
    return result


def register(service, content, *, key, owner="owner", country="ID", level="primary", url=SOURCE):
    blob = service.archive.blobs.put_bytes(owner, content)
    manifest = parse_file("official.pdf", content, "application/pdf")
    with service.db.transaction() as conn:
        doc = service.archive.store_original(
            conn,
            owner,
            blob,
            "official.pdf",
            "application/pdf",
            url,
            collection="country:" + country,
            level=level,
            tags=["country:" + country],
            identity_key=key,
            kind_hint="law",
        )
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
        return dict(service.own_document(conn, owner, doc["id"]))


def stored(service, doc):
    with closing(service.db.connect()) as conn:
        row = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone())
        row["manifest"] = json.loads(row["manifest"])
        return row
