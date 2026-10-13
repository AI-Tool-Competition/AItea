"""Persistent retrieval must cover all eligible originals without loading manifests."""

from contextlib import closing
from datetime import date
import hashlib
import json
from types import SimpleNamespace

import pytest

from app import ingestion
from app.catalog import DocumentMetadata
from app.db import dump, now
from app.service import Service


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


def search(service, query, **kwargs):
    return service.search("owner", query, country="ID", **kwargs)


def test_sync_rejects_a_cached_complete_pdf_with_a_missing_native_glyph(workspace):
    manifest = body("permit clause")
    manifest.update(
        quality={"complete": True},
        pages=[{"page": 1, "status": "native", "text": "permit clause", "native_text": "permit clause"}],
    )
    with workspace.db.transaction() as conn:
        seed(conn, "missing-glyph", manifest)
    assert search(workspace, "permit")["items"]
    manifest["pages"][0]["text"] = manifest["pages"][0]["native_text"] = "permit \ufffd clause"
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id='missing-glyph'", (dump(manifest),))
        workspace.index.sync_document(conn, "missing-glyph")
        assert (
            conn.execute(
                "SELECT complete FROM retrieval_documents WHERE document_id='missing-glyph'"
            ).fetchone()[0]
            == 0
        )
    assert search(workspace, "permit")["items"] == []


def test_search_matches_documents_beyond_old_500_family_cap(workspace):
    with workspace.db.transaction() as conn:
        seed(conn, "old-target", body("uniqueoldanswer"), order=0)
        for i in range(500):
            seed(conn, f"new-{i}", body("ordinary noise"), order=i + 1)
    # The explicit migration worker consumes resumable batches; warm retrieval
    # then searches every eligible document with no historical 500-family cap.
    while workspace.index.rebuild("owner")["remaining"]:
        pass
    result = search(workspace, "uniqueoldanswer")
    assert [item["document_id"] for item in result["items"]] == ["old-target"]
    assert "warning" not in result


def test_single_large_legacy_manifest_is_not_dropped_from_search(workspace):
    # Valid legacy JSON containing a large ignored diagnostic attribute; no huge vectors are needed.
    encoded = dump(body("largeuniqueanswer"))[:-1] + ',"old_payload":"' + "x" * (64 * 1024 * 1024) + '"}'
    with workspace.db.transaction() as conn:
        seed(conn, "large", encoded)
    del encoded
    assert [hit["document_id"] for hit in search(workspace, "largeuniqueanswer")["items"]] == ["large"]


def test_persistent_text_index_survives_restart_without_reading_manifests(workspace, monkeypatch):
    with workspace.db.transaction() as conn:
        seed(conn, "persisted", body("永久保存 indexpersist"))
    assert search(workspace, "永久保存")["items"]
    reopened = Service(workspace.db.data_dir)
    statements = []
    connect = reopened.db.connect

    def traced():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(reopened.db, "connect", traced)
    assert search(reopened, "indexpersist")["items"]
    reads = [
        sql.lower()
        for sql in statements
        if sql.lstrip().lower().startswith(("select", "with", "create temp"))
    ]
    assert not any("manifest" in sql for sql in reads)
    with closing(reopened.db.connect()) as conn:
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=10").fetchone()


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
    assert not search(upgraded, "damaged")["items"]
    assert [item["document_id"] for item in search(upgraded, "soundupgradeclause")["items"]] == [
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


def test_rebuild_is_idempotent_and_removes_stale_chunk_text(workspace):
    with workspace.db.transaction() as conn:
        seed(conn, "changing", body("olduniqueterm"))
    assert search(workspace, "olduniqueterm")["items"]
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
    assert not search(workspace, "olduniqueterm")["items"]
    assert search(workspace, "newuniqueterm")["items"]


def test_scope_gates_apply_before_bounded_candidate_selection(workspace):
    with workspace.db.transaction() as conn:
        seed(conn, "approved", body("scopeword"))
        seed(conn, "old", body("scopeword"), family="revision", version=1, end="2021-12-31")
        seed(
            conn,
            "new-draft",
            body("scopeword"),
            family="revision",
            version=2,
            approved=False,
            start="2022-01-01",
        )
        for i in range(65):
            for prefix, options in (
                ("country", {"country": "SG"}),
                ("owner", {"owner": "other"}),
                ("tier", {"level": "secondary"}),
                ("draft", {"approved": False}),
                ("expired", {"end": "2021-12-31"}),
                ("future", {"start": "2030-01-01"}),
            ):
                seed(conn, f"{prefix}-{i}", body("scopeword " * 20), **options)
    assert {item["document_id"] for item in search(workspace, "scopeword")["items"]} == {"approved"}
    historical = search(workspace, "scopeword", as_of=date(2021, 1, 1), document_ids=["old"])
    assert [item["document_id"] for item in historical["items"]] == ["old"]


def test_incomplete_ocr_text_is_not_searchable_or_embeddable(workspace):
    manifest = body("pendingunique")
    manifest["quality"] = {"complete": False, "status": "partial"}
    with workspace.db.transaction() as conn:
        seed(conn, "pending", manifest)
    assert not search(workspace, "pendingunique")["items"]
    with closing(workspace.db.connect()) as conn:
        assert workspace.index.pending(conn, "pending", "model") == []


def test_embedding_context_and_vectors_are_persisted_outside_manifest(workspace, monkeypatch):
    manifest = ingestion.parse_file(
        "Regulation.md",
        b"# Tea imports\n## Pasal 9 - Permit\nMust be available.\n## Pasal 10 - Label\nMust be available.",
    )
    with workspace.db.transaction() as conn:
        seed(conn, "context", manifest)
        workspace.collection.queue_processing(conn, "context", "owner")
    inputs = []
    monkeypatch.setattr(workspace, "embedding_fingerprint", lambda: "test-model")

    def embed(texts):
        inputs.extend(texts)
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(ingestion, "embed_texts", embed)
    while claim := workspace.collection.claim_processing():
        workspace.collection.process_item(claim)
    assert len(inputs) == 2 and inputs[0] != inputs[1]
    assert "Pasal 9" in inputs[0] and "Pasal 10" in inputs[1]
    with closing(workspace.db.connect()) as conn:
        stored = json.loads(conn.execute("SELECT manifest FROM documents WHERE id='context'").fetchone()[0])
        assert not any("embedding" in chunk for chunk in stored["chunks"])
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 2
        assert conn.execute("SELECT DISTINCT length(vector) FROM retrieval_vectors").fetchone()[0] == 8


def test_cross_batch_dimension_change_cannot_be_committed_as_complete(workspace, monkeypatch):
    with workspace.db.transaction() as conn:
        seed(conn, "dimension", body(count=65))
        workspace.collection.queue_processing(conn, "dimension", "owner")
    calls = []
    monkeypatch.setattr(workspace, "embedding_fingerprint", lambda: "model")

    def embed(texts):
        calls.append(len(texts))
        return [[1.0, 0.0] if len(calls) == 1 else [1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(ingestion, "embed_texts", embed)
    for _ in range(3):
        workspace.collection.process_item(workspace.collection.claim_processing())
    with closing(workspace.db.connect()) as conn:
        job = conn.execute(
            "SELECT status,error FROM processing_jobs WHERE document_id='dimension'"
        ).fetchone()
        assert job["status"] != "completed"
        assert job["error"]
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 64


def test_changed_embedding_input_invalidates_previously_stored_vector(workspace, monkeypatch):
    manifest = body("old words")
    manifest["embedding_fingerprint"] = "model"
    manifest["chunks"][0]["embedding"] = [1.0, 0.0]
    with workspace.db.transaction() as conn:
        seed(conn, "changed", manifest)
    monkeypatch.setattr(workspace, "embedding_fingerprint", lambda: "model")
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1.0, 0.0]])
    assert search(workspace, "semanticquestion")["mode"] == "hybrid"
    changed = body("new words")
    changed["embedding_fingerprint"] = "model"
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id='changed'", (dump(changed),))
    result = search(workspace, "semanticquestion")
    assert result["items"] == [] and result["mode"] == "lexical"


def test_search_excerpts_contain_the_matching_tail_and_stable_chunk_identity(workspace):
    manifest = ingestion.parse_file("rule.txt", ("izin kegiatan " * 120 + "tailanswer 30 hari.").encode())
    with workspace.db.transaction() as conn:
        seed(conn, "long", manifest)
    hit = search(workspace, "tailanswer")["items"][0]
    assert "tailanswer 30 hari" in hit["excerpt"]
    assert hit["chunk_id"] and hit["chunk_hash"]
    assert hit["locator"]["chunk_id"] == hit["chunk_id"]


def test_legal_search_keeps_distinct_chunks_on_same_page(workspace):
    manifest = body(count=2)
    for index, chunk in enumerate(manifest["chunks"]):
        chunk.update(
            text=f"permit answer{index}", retrieval_text=f"permit answer{index}", locator={"page": 7}
        )
    with workspace.db.transaction() as conn:
        seed(conn, "same-page", manifest)
    template = {"origin": "ID", "destination": "ID", "agent": {}, "stages": []}
    hits = workspace.legal.search("owner", "permit", None, template, ["ID"], 5)["items"]
    assert len(hits) == 2 and len({item["id"] for item in hits}) == 2


def test_reranking_failure_keeps_grounded_order_with_warning(workspace, monkeypatch):
    from app import reranking

    with workspace.db.transaction() as conn:
        seed(conn, "good", body("permit answer"))
    monkeypatch.setattr(
        reranking, "rerank_texts", lambda *a, **k: (_ for _ in ()).throw(ValueError("temporary"))
    )
    result = search(workspace, "permit")
    assert [item["document_id"] for item in result["items"]] == ["good"]
    assert "重排" in result["warning"]


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
    assert search(upgraded, "legacyupgradeanswer")["items"]
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
    assert search(Service(workspace.db.data_dir), "legacyupgradeanswer")["items"]


def test_vector_checkpoint_rejects_stale_input_hash_atomically(workspace):
    with workspace.db.transaction() as conn:
        seed(conn, "stale", body("before", count=2))
    workspace.index.ensure_current("owner", country="ID")
    with closing(workspace.db.connect()) as conn:
        batch = workspace.index.pending(conn, "stale", "model")
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


def test_model_change_excludes_old_vectors_without_touching_original(workspace, monkeypatch):
    manifest = body("literal only")
    manifest["embedding_fingerprint"] = "old-model"
    manifest["chunks"][0]["embedding"] = [1.0, 0.0]
    with workspace.db.transaction() as conn:
        seed(conn, "old-model", manifest)
    monkeypatch.setattr(workspace, "embedding_fingerprint", lambda: "new-model")

    def forbidden(_):
        raise AssertionError("No query provider call when there are no matching model vectors")

    monkeypatch.setattr(ingestion, "embed_texts", forbidden)
    result = search(workspace, "semanticunmatched")
    assert result["items"] == [] and result["mode"] == "lexical"
    assert "尚未" in result["warning"]


def test_configured_reranking_uses_only_eligible_candidates(workspace, monkeypatch):
    from app import reranking

    with workspace.db.transaction() as conn:
        seed(conn, "allowed-a", body("permit first"))
        seed(conn, "allowed-b", body("permit second"))
        seed(conn, "secondary", body("permit secret"), level="secondary")
        seed(conn, "other-country", body("permit secret"), country="SG")
    calls = []

    def rerank(query, texts, *, limit):
        calls.extend(texts)
        return [{"index": index, "score": 1.0 / (index + 1)} for index in reversed(range(len(texts)))]

    monkeypatch.setattr(reranking, "rerank_texts", rerank)
    result = search(workspace, "permit")
    assert len(calls) == 2 and all("secret" not in text for text in calls)
    assert len(result["items"]) == 2


def test_unchanged_capture_does_not_requeue_complete_persistent_index(workspace, monkeypatch):
    monkeypatch.setattr(workspace, "embedding_fingerprint", lambda: "model")
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])
    blob = workspace.archive.blobs.put_bytes("owner", b"native content")

    def capture(conn):
        return workspace.archive.store_original(
            conn,
            "owner",
            blob,
            "law.txt",
            "text/plain",
            "https://official.example/law",
            collection="country:ID",
            level="primary",
            tags=[],
        )

    with workspace.db.transaction() as conn:
        document = capture(conn)
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(body()), document["id"]))
    while claim := workspace.collection.claim_processing():
        workspace.collection.process_item(claim)
    with workspace.db.transaction() as conn:
        repeated = capture(conn)
        assert repeated["id"] == document["id"]
        assert (
            conn.execute(
                "SELECT status FROM processing_jobs WHERE document_id=?", (document["id"],)
            ).fetchone()[0]
            == "completed"
        )


@pytest.mark.parametrize("during", ["embedding", "reranking"])
@pytest.mark.parametrize("change", ["draft", "secondary", "new_version", "dirty_text", "changed_chunk"])
def test_network_call_cannot_return_a_now_ineligible_or_changed_chunk(workspace, monkeypatch, during, change):
    from app import reranking

    manifest = body("permit original")
    manifest["embedding_fingerprint"] = "model"
    manifest["chunks"][0]["embedding"] = [1.0, 0.0]
    with workspace.db.transaction() as conn:
        seed(conn, "race", manifest)
    monkeypatch.setattr(workspace, "embedding_fingerprint", lambda: "model")

    def mutate():
        with workspace.db.transaction() as conn:
            if change == "draft":
                conn.execute(
                    "UPDATE document_metadata SET payload=json_set(payload,'$.review_status','draft'),revision=revision+1 WHERE document_id='race'"
                )
            elif change == "secondary":
                conn.execute("UPDATE legal_classifications SET level='secondary' WHERE document_id='race'")
            elif change == "new_version":
                seed(conn, "v2", body(), family="race", version=2, approved=False)
            else:
                conn.execute(
                    "UPDATE documents SET manifest=? WHERE id='race'", (dump(body("permit replacement")),)
                )
                if change == "changed_chunk":
                    workspace.index.sync_document(conn, "race")

    def embed(texts):
        if during == "embedding":
            mutate()
        return [[1.0, 0.0]]

    def rerank(query, texts, *, limit):
        if during == "reranking":
            mutate()
        return None

    monkeypatch.setattr(ingestion, "embed_texts", embed)
    monkeypatch.setattr(reranking, "rerank_texts", rerank)
    result = search(workspace, "permit")
    assert result["items"] == []
    assert "变更" in result["warning"]


def test_search_backfill_is_scoped_and_bounded_with_explicit_remaining_warning(workspace):
    with workspace.db.transaction() as conn:
        seed(conn, "target", body("needle"))
        for i in range(30):
            seed(conn, f"eligible-{i:02}", body("needle"))
            seed(conn, f"sg-{i}", body(), country="SG")
            seed(conn, f"draft-{i}", body(), approved=False)
            seed(conn, f"secondary-{i}", body(), level="secondary")
    assert search(workspace, "needle", document_ids=["target"])["items"]
    with closing(workspace.db.connect()) as conn:
        assert [row[0] for row in conn.execute("SELECT document_id FROM retrieval_documents")] == ["target"]
    result = search(workspace, "needle")
    assert "待索引" in result["warning"]
    with closing(workspace.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_documents").fetchone()[0] <= 21
        assert conn.execute("SELECT COUNT(*) FROM retrieval_dirty").fetchone()[0] >= 100


def test_new_upload_dimension_error_is_visible_queued_and_retry_preserves_original(workspace, monkeypatch):
    monkeypatch.setattr(workspace, "embedding_fingerprint", lambda: "model")
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])
    workspace.store_document("owner", "first.txt", b"first permit")
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1.0, 0.0, 0.0] for _ in texts])
    document = workspace.store_document("owner", "second.txt", b"second permit")
    assert document["error"] and "向量" in document["error"]
    with closing(workspace.db.connect()) as conn:
        stored = conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[0]
        assert not json.loads(stored).get("embedding_fingerprint")
        assert workspace.index.pending(conn, document["id"], "model")
        assert (
            conn.execute(
                "SELECT status FROM processing_jobs WHERE document_id=?", (document["id"],)
            ).fetchone()[0]
            == "queued"
        )
    repeated = workspace.store_document("owner", "second.txt", b"second permit")
    assert repeated["id"] == document["id"]
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])
    while claim := workspace.collection.claim_processing():
        workspace.collection.process_item(claim)
    with closing(workspace.db.connect()) as conn:
        assert not workspace.index.pending(conn, document["id"], "model")
        assert (
            conn.execute(
                "SELECT status FROM processing_jobs WHERE document_id=?", (document["id"],)
            ).fetchone()[0]
            == "completed"
        )


def test_partial_vector_coverage_is_explicit(workspace, monkeypatch):
    manifest = body("permit", count=2)
    manifest["embedding_fingerprint"] = "model"
    manifest["chunks"][0]["embedding"] = [1.0, 0.0]
    with workspace.db.transaction() as conn:
        seed(conn, "partial-vectors", manifest)
    monkeypatch.setattr(workspace, "embedding_fingerprint", lambda: "model")
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1.0, 0.0]])
    result = search(workspace, "permit")
    assert len(result["items"]) == 2 and result["mode"] == "hybrid"
    assert "部分" in result["warning"] and "向量" in result["warning"]


def test_historical_query_does_not_fall_back_to_older_text_when_selected_version_is_incomplete(workspace):
    with workspace.db.transaction() as conn:
        seed(conn, "old", body("permit old"), family="law", version=1, end="2028-01-01")
        newer = body("permit incomplete")
        newer["quality"] = {"complete": False, "status": "partial"}
        seed(conn, "new", newer, family="law", version=2, start="2022-01-01")
    workspace.index.rebuild("owner")
    assert search(workspace, "permit", as_of=date(2026, 1, 1))["items"] == []


@pytest.mark.parametrize("documents", [1, 31])
def test_stage_analysis_prepares_legacy_index_before_snapshot_without_wasting_model_calls(
    workspace, monkeypatch, documents
):
    from app.service import AppError

    stage = {"id": "stage", "name": "stage", "description": "", "tasks": [], "status": "active"}
    case = {
        "id": "case",
        "template": {
            "modes": [],
            "origin": "ID",
            "destination": "ID",
            "agent": {"steps": [{"type": "retrieve", "enabled": True}]},
        },
        "company": "",
        "product": "",
        "mode_id": "",
        "stages": [stage],
        "facts": [],
        "revision": 1,
    }
    with workspace.db.transaction() as conn:
        conn.execute(
            "INSERT INTO cases VALUES('case','owner','case','','','',?,1,?,?)",
            (dump(case["template"]), now(), now()),
        )
        for index in range(documents):
            seed(conn, f"legacy-{index:02}", body("permit"))
        seed(conn, "irrelevant-country", body(), country="SG")
    calls = []

    def run(*args, **kwargs):
        calls.append(True)
        hits = workspace.search("owner", "permit", case_id="case", country="ID")
        return {
            "stage_id": "stage",
            "output": "answer",
            "checks": [],
            "citations": hits["items"],
            "assistant_mode": "demo",
            "trace": [],
        }

    monkeypatch.setattr(workspace, "case_detail", lambda *_: case)
    monkeypatch.setattr(workspace, "run_workflow", run)
    payload = SimpleNamespace(expected_revision=1, retry=False)
    if documents > 20:
        with pytest.raises(AppError) as preparing:
            workspace.studio.analyze("owner", "case", "stage", payload)
        assert preparing.value.code == "index_preparing" and not calls
        with closing(workspace.db.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM retrieval_documents").fetchone()[0] == 20
    first = workspace.studio.analyze("owner", "case", "stage", payload)
    assert first["status"] == "completed" and len(calls) == 1
    assert workspace.studio.analyze("owner", "case", "stage", payload)["id"] == first["id"]
    assert len(calls) == 1
    with closing(workspace.db.connect()) as conn:
        assert conn.execute("SELECT document_id FROM retrieval_dirty").fetchone()[0] == "irrelevant-country"


@pytest.mark.parametrize("documents", [1, 31])
def test_legal_request_prepares_legacy_index_before_reserving_snapshot(workspace, monkeypatch, documents):
    from app.legal_models import LegalResult
    from app.service import AppError

    stage = {"id": "stage", "name": "stage", "description": "", "tasks": [], "status": "active"}
    case = {
        "id": "case",
        "template": {"modes": [], "origin": "ID", "destination": "ID", "agent": {}},
        "company": "",
        "product": "",
        "mode_id": "",
        "stages": [stage],
        "facts": [],
        "revision": 1,
    }
    with workspace.db.transaction() as conn:
        conn.execute(
            "INSERT INTO cases VALUES('case','owner','case','','','',?,1,?,?)",
            (dump(case["template"]), now(), now()),
        )
        for index in range(documents):
            seed(conn, f"legacy-{index:02}", body("permit"))
    calls = []

    def execute(owner, template, stage, question, facts, **options):
        calls.append(True)
        workspace.legal.search(owner, "permit", "case", template, ["ID"], 5, as_of=date(2026, 10, 13))
        return LegalResult(question=question, output="completed answer", assistant_mode="demo").model_dump(
            mode="json"
        )

    monkeypatch.setattr(workspace, "case_detail", lambda *_: case)
    monkeypatch.setattr(workspace.legal, "execute_engine", execute)
    payload = {"stage_id": "stage", "question": "permit", "client_request_id": "one"}
    if documents > 20:
        with pytest.raises(AppError) as preparing:
            workspace.legal.analyze("owner", "case", payload)
        assert preparing.value.code == "index_preparing" and not calls
        with closing(workspace.db.connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM legal_requests").fetchone()[0] == 0
    result = workspace.legal.analyze("owner", "case", payload)
    assert not result["error"] and result["result"]["output"] == "completed answer"
    assert workspace.legal.analyze("owner", "case", payload)["id"] == result["id"]
    assert len(calls) == 1


def test_debug_run_prepares_legacy_inline_vectors_before_scope_fingerprint(workspace):
    import copy
    from app.defaults import DEFAULT_TEMPLATE
    from app.models import TemplateDebugInput

    template = copy.deepcopy(DEFAULT_TEMPLATE)
    template["destination"] = "ID"
    template["agent"]["steps"] = [
        {"id": "retrieve", "type": "retrieve", "label": "retrieve", "enabled": True, "instructions": ""}
    ]
    manifest = body("permit")
    manifest["embedding_fingerprint"] = "old"
    manifest["chunks"][0]["embedding"] = [1.0, 0.0]
    with workspace.db.transaction() as conn:
        seed(conn, "legacy", manifest)
    request = TemplateDebugInput(
        template=template, input="permit", document_ids=["legacy"], client_id="debug-first"
    )
    result = workspace.studio.debug("owner", request)
    assert result["status"] == "completed", result.get("error")
    assert result["citations"]


def test_legal_request_prepares_unassigned_legacy_business_before_snapshot(workspace, monkeypatch):
    from app import parsing
    from app.legal_models import LegalResult

    monkeypatch.setattr(parsing, "parse_bounded", lambda *args, **kwargs: body("permit"))
    document = workspace.store_document("owner", "business.txt", b"business permit")
    stage = {"id": "stage", "name": "stage", "description": "", "tasks": [], "status": "active"}
    case = {
        "id": "case",
        "template": {"modes": [], "origin": "待明确", "destination": "待明确", "agent": {}},
        "company": "",
        "product": "",
        "mode_id": "",
        "stages": [stage],
        "facts": [],
        "revision": 1,
    }
    with workspace.db.transaction() as conn:
        conn.execute(
            "INSERT INTO cases VALUES('case','owner','case','','','',?,1,?,?)",
            (dump(case["template"]), now(), now()),
        )
        # Reproduce the metadata-only upgrade: original material exists while
        # its derived retrieval record still needs its first bounded backfill.
        conn.execute("DELETE FROM retrieval_documents WHERE document_id=?", (document["id"],))
        conn.execute("INSERT OR IGNORE INTO retrieval_dirty VALUES(?)", (document["id"],))
        seed(conn, "excluded-national-law", body("permit"), country="ID")
    calls = []

    def execute(owner, template, stage, question, facts, **options):
        calls.append(True)
        result = workspace.legal.search(owner, "permit", "case", template, [], 5)
        assert {item["document_id"] for item in result["items"]} == {document["id"]}
        return LegalResult(question=question, output="completed answer", assistant_mode="demo").model_dump(
            mode="json"
        )

    monkeypatch.setattr(workspace, "case_detail", lambda *_: case)
    monkeypatch.setattr(workspace.legal, "execute_engine", execute)
    payload = {"stage_id": "stage", "question": "permit", "client_request_id": "first"}
    result = workspace.legal.analyze("owner", "case", payload)
    assert not result["error"] and result["result"]["output"] == "completed answer"
    assert workspace.legal.analyze("owner", "case", payload)["id"] == result["id"]
    assert len(calls) == 1
    with closing(workspace.db.connect()) as conn:
        assert (
            conn.execute("SELECT document_id FROM retrieval_dirty").fetchone()[0] == "excluded-national-law"
        )
