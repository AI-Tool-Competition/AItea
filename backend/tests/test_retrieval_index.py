"""Persistent retrieval must cover all eligible originals without loading manifests."""

import hashlib
from contextlib import closing

import pytest
from app.bootstrap import Service
from app.contexts.knowledge.domain.metadata import DocumentMetadata
from app.shared.domain.values import dump, now


@pytest.fixture
def workspace(tmp_path):
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        for owner in ("owner", "other"):
            conn.execute("INSERT INTO users VALUES(?,?,NULL,NULL,1,?)", (owner, owner, now()))
    return service


def body(text="tea lawful clause", count=1):
    return {
        "elements": [{"element_key": "e1", "content_text": text}],
        "chunks": [
            {"id": f"chunk-{i}", "text": text, "retrieval_text": text, "locator": {"page": i + 1}}
            for i in range(count)
        ],
    }


def seed(
    conn,
    identifier,
    manifest,
    *,
    country="ID",
    owner="owner",
    level="primary",
    family=None,
    version=1,
    order=1,
    approved=True,
    start="2020-01-01",
    end=None,
):
    stamp = f"2026-10-13T00:00:00.{order:06d}+00:00"
    conn.execute(
        """INSERT INTO documents(id,owner_id,case_id,parent_id,title,filename,role,status,mime_type,
        size,version,content_hash,storage_path,tags,source_url,error,manifest,created_at,family_id)
        VALUES(?,?,NULL,NULL,?,?,'primary','ready','text/plain',1,?,?,'/fixture/not-used',
        '[]',?,NULL,?,?,?)""",
        (
            identifier,
            owner,
            identifier,
            identifier + ".txt",
            version,
            hashlib.sha256(identifier.encode()).hexdigest(),
            "https://official.example/" + identifier,
            manifest if isinstance(manifest, str) else dump(manifest),
            stamp,
            family or identifier,
        ),
    )
    conn.execute("INSERT INTO document_countries VALUES(?,?)", (identifier, country))
    conn.execute(
        "INSERT INTO legal_classifications VALUES(?,?,?,'[]',1,?)",
        (identifier, f"country:{country}", level, stamp),
    )
    metadata = DocumentMetadata(
        kind="law",
        jurisdiction=country,
        language="id",
        review_status="published" if approved else "draft",
        effective_from=start,
        effective_to=end,
    ).model_dump(mode="json")
    conn.execute(
        "INSERT INTO document_metadata VALUES(?,?,1,?,?,?)", (identifier, dump(metadata), owner, stamp, stamp)
    )


def catalog_matches(service, query):
    # These migration contracts check persisted text/quality. The configured
    # remote retrieval and its authorization gates are tested in test_mineru_ragflow.
    service.index.ensure_current("owner", country="ID")
    with closing(service.db.connect()) as conn:
        rows = conn.execute(
            """SELECT DISTINCT c.document_id FROM retrieval_chunks c
            JOIN retrieval_documents d ON d.document_id=c.document_id
            JOIN documents original ON original.id=c.document_id
            WHERE original.owner_id='owner' AND d.complete=1 AND c.text LIKE ? ORDER BY c.document_id""",
            ("%" + query + "%",),
        ).fetchall()
        return {"items": [dict(row) for row in rows]}


def test_pdf_quality_upgrade_quarantines_old_published_flags_once_without_editing_text(workspace):
    broken = body("damaged clause")
    broken.update(
        quality={"complete": True}, pages=[{"page": 1, "status": "native", "text": "damaged \ufffd clause"}]
    )
    with workspace.db.transaction() as conn:
        seed(conn, "good-upgrade", body("soundupgradeclause"))
        seed(conn, "broken-upgrade", broken)
        seed(conn, "foreign-upgrade", body("otherclause"), owner="other")
        for identifier in ("good-upgrade", "broken-upgrade", "foreign-upgrade"):
            workspace.index.sync_document(conn, identifier)
        # Simulate the accepted complete flag from the older quality recipe.
        conn.execute("UPDATE retrieval_documents SET complete=1 WHERE document_id='broken-upgrade'")
        conn.execute("DELETE FROM retrieval_dirty")
        conn.execute("DELETE FROM schema_migrations WHERE version=13")
        originals = [
            tuple(row)
            for row in conn.execute("SELECT id,manifest,content_hash,version FROM documents ORDER BY id")
        ]
        reviews = [tuple(row) for row in conn.execute("SELECT * FROM document_metadata ORDER BY document_id")]
    upgraded = Service(workspace.db.data_dir)
    with closing(upgraded.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_dirty").fetchone()[0] == 3
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=13").fetchone()
        assert [
            tuple(row)
            for row in conn.execute("SELECT id,manifest,content_hash,version FROM documents ORDER BY id")
        ] == originals
        assert [
            tuple(row) for row in conn.execute("SELECT * FROM document_metadata ORDER BY document_id")
        ] == reviews
    assert not catalog_matches(upgraded, "damaged")["items"]
    assert [item["document_id"] for item in catalog_matches(upgraded, "soundupgradeclause")["items"]] == [
        "good-upgrade"
    ]
    restarted = Service(workspace.db.data_dir)
    with closing(restarted.db.connect()) as conn:
        assert [row[0] for row in conn.execute("SELECT document_id FROM retrieval_dirty")] == [
            "foreign-upgrade"
        ]
        assert (
            conn.execute(
                "SELECT complete FROM retrieval_documents WHERE document_id='broken-upgrade'"
            ).fetchone()[0]
            == 0
        )
        assert [
            tuple(row)
            for row in conn.execute("SELECT id,manifest,content_hash,version FROM documents ORDER BY id")
        ] == originals


def test_empty_database_records_pdf_quality_upgrade_idempotently(tmp_path):
    service = Service(tmp_path)
    Service(tmp_path)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=13").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM retrieval_dirty").fetchone()


def test_html_and_missing_source_upgrade_preserves_evidence_and_quarantines_cached_flags(workspace):
    old = body("omittedsourceimageclause")
    old.update(parser="html-basic", parser_version="2", pipeline_version="html-blocks-v2")
    from app.contexts.knowledge.infrastructure.parsing.native import parse_file

    good = parse_file("law.html", b"<main><p>verifiedhtmlclause</p></main>", "text/html")
    missing = body("missingsourceclause")
    missing.update(
        pages=[{"page": 1, "status": "native", "text": "The linked image cannot be displayed."}],
        quality={"complete": True},
    )
    with workspace.db.transaction() as conn:
        seed(conn, "old-html", old)
        seed(conn, "new-html", good)
        seed(conn, "missing-pdf", missing)
        seed(conn, "foreign-html", old, owner="other")
        for identifier in ("old-html", "new-html", "missing-pdf", "foreign-html"):
            workspace.index.sync_document(conn, identifier)
        conn.execute("UPDATE retrieval_documents SET complete=1")
        conn.execute("DELETE FROM retrieval_dirty")
        conn.execute("DELETE FROM schema_migrations WHERE version=15")
        before = [tuple(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")]
        reviews = [tuple(r) for r in conn.execute("SELECT * FROM document_metadata ORDER BY document_id")]
    upgraded = Service(workspace.db.data_dir)
    assert not catalog_matches(upgraded, "omittedsourceimageclause")["items"]
    assert not catalog_matches(upgraded, "missingsourceclause")["items"]
    assert [r["document_id"] for r in catalog_matches(upgraded, "verifiedhtmlclause")["items"]] == [
        "new-html"
    ]
    restarted = Service(workspace.db.data_dir)
    with closing(restarted.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=15").fetchone()[0] == 1
        assert [r[0] for r in conn.execute("SELECT document_id FROM retrieval_dirty")] == ["foreign-html"]
        assert [tuple(r) for r in conn.execute("SELECT * FROM documents ORDER BY id")] == before
        assert [
            tuple(r) for r in conn.execute("SELECT * FROM document_metadata ORDER BY document_id")
        ] == reviews


def test_docx_upgrade_rechecks_old_published_body_cache_without_rewriting_evidence(workspace):
    old = body("oldbodyonlyclause")
    old.update(parser="docx-basic", parser_version="1", pipeline_version="docx-paragraphs-v1")
    good = body("verifiedpackageclause")
    good.update(
        parser="docx-basic",
        parser_version="2",
        pipeline_version="docx-package-mapping-v2",
        quality={"complete": True},
    )
    with workspace.db.transaction() as conn:
        seed(conn, "old-docx", old)
        seed(conn, "new-docx", good)
        seed(conn, "foreign-docx", old, owner="other")
        for identifier in ("old-docx", "new-docx", "foreign-docx"):
            workspace.index.sync_document(conn, identifier)
        conn.execute("UPDATE retrieval_documents SET complete=1 WHERE document_id='old-docx'")
        conn.execute("DELETE FROM retrieval_dirty")
        conn.execute("DELETE FROM schema_migrations WHERE version=14")
        before = [
            tuple(r)
            for r in conn.execute("SELECT id,manifest,content_hash,version FROM documents ORDER BY id")
        ]
        reviews = [tuple(r) for r in conn.execute("SELECT * FROM document_metadata ORDER BY document_id")]
    upgraded = Service(workspace.db.data_dir)
    assert not catalog_matches(upgraded, "oldbodyonlyclause")["items"]
    assert [r["document_id"] for r in catalog_matches(upgraded, "verifiedpackageclause")["items"]] == [
        "new-docx"
    ]
    restarted = Service(workspace.db.data_dir)
    with closing(restarted.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=14").fetchone()[0] == 1
        assert [r[0] for r in conn.execute("SELECT document_id FROM retrieval_dirty")] == ["foreign-docx"]
        assert [
            tuple(r)
            for r in conn.execute("SELECT id,manifest,content_hash,version FROM documents ORDER BY id")
        ] == before
        assert [
            tuple(r) for r in conn.execute("SELECT * FROM document_metadata ORDER BY document_id")
        ] == reviews


def test_rebuild_is_idempotent_and_removes_stale_chunk_text(workspace):
    with workspace.db.transaction() as conn:
        seed(conn, "changing", body("olduniqueterm"))
    assert catalog_matches(workspace, "olduniqueterm")["items"]
    workspace.index.rebuild("owner")
    with closing(workspace.db.connect()) as conn:
        before = conn.execute(
            "SELECT revision FROM retrieval_documents WHERE document_id='changing'"
        ).fetchone()[0]
    workspace.index.rebuild("owner")
    with closing(workspace.db.connect()) as conn:
        assert (
            conn.execute("SELECT revision FROM retrieval_documents WHERE document_id='changing'").fetchone()[
                0
            ]
            == before
        )
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id='changing'", (dump(body("newuniqueterm")),))
    assert not catalog_matches(workspace, "olduniqueterm")["items"]
    assert catalog_matches(workspace, "newuniqueterm")["items"]


def test_schema_nine_upgrade_preserves_originals_and_backfills_once(workspace):
    legacy = body("legacyupgradeanswer")
    legacy["embedding_fingerprint"] = "old-model"
    legacy["chunks"][0]["embedding"] = [1.0, 0.0]
    original = workspace.db.files_dir / "legacy-original.txt"
    original.write_bytes(b"immutable original")
    with workspace.db.transaction() as conn:
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'retrieval_%'"
        ).fetchall():
            conn.execute(f'DROP TRIGGER "{row[0]}"')
        for table in (
            "retrieval_fts",
            "retrieval_vectors",
            "retrieval_chunks",
            "retrieval_models",
            "retrieval_dirty",
            "retrieval_documents",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("DELETE FROM schema_migrations WHERE version=10")
        seed(conn, "legacy", legacy)
        conn.execute("UPDATE documents SET storage_path=? WHERE id='legacy'", (str(original),))
        before = tuple(
            conn.execute(
                "SELECT id,content_hash,version,storage_path FROM documents WHERE id='legacy'"
            ).fetchone()
        )
    upgraded = Service(workspace.db.data_dir)
    assert catalog_matches(upgraded, "legacyupgradeanswer")["items"]
    assert original.read_bytes() == b"immutable original"
    with closing(upgraded.db.connect()) as conn:
        assert (
            tuple(
                conn.execute(
                    "SELECT id,content_hash,version,storage_path FROM documents WHERE id='legacy'"
                ).fetchone()
            )
            == before
        )
        assert conn.execute("SELECT COUNT(*) FROM retrieval_chunks").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 1
        assert not conn.execute("SELECT 1 FROM retrieval_dirty").fetchone()
    assert catalog_matches(Service(workspace.db.data_dir), "legacyupgradeanswer")["items"]


def test_vector_checkpoint_rejects_stale_input_hash_atomically(workspace):
    with workspace.db.transaction() as conn:
        seed(conn, "stale", body("before", count=2))
    workspace.index.ensure_current("owner", country="ID")
    with closing(workspace.db.connect()) as conn:
        batch = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM retrieval_chunks WHERE document_id='stale' ORDER BY ordinal"
            )
        ]
    with workspace.db.transaction() as conn:
        updated = body("after", count=2)
        conn.execute("UPDATE documents SET manifest=? WHERE id='stale'", (dump(updated),))
        workspace.index.sync_document(conn, "stale")
    with pytest.raises(ValueError, match="变更"):
        with workspace.db.transaction() as conn:
            workspace.index.store_vectors(conn, "stale", "model", batch, [[1, 0], [1, 0]])
    with closing(workspace.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 0


@pytest.mark.parametrize("vector", [[0, 0], [1, float("nan")], [1, float("inf")], [1e100, 1], [True, 1]])
def test_invalid_vectors_never_enter_persistent_index(workspace, vector):
    with workspace.db.transaction() as conn:
        seed(conn, "invalid", body())
    workspace.index.ensure_current("owner", country="ID")
    with pytest.raises(ValueError):
        with workspace.db.transaction() as conn:
            batch = workspace.index.pending(conn, "invalid", "model")
            workspace.index.store_vectors(conn, "invalid", "model", batch, [vector])
    with closing(workspace.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 0
