import importlib.util
import sqlite3
from pathlib import Path

import pytest

from app.contexts.documents.infrastructure import country_clauses, country_semantic

spec = importlib.util.spec_from_file_location(
    "clause_indexer", Path(__file__).resolve().parents[2] / "scripts/index_country_clauses.py"
)
indexer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(indexer)


def source(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("EMBEDDING_API_KEY", "fixture-only")
    monkeypatch.setenv("EMBEDDING_MODEL", "fixture-model")
    path = tmp_path / "legal-library/index.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE metadata(key,value)")
        conn.execute("INSERT INTO metadata VALUES('fingerprint','source-v1')")
        conn.execute(
            "CREATE TABLE records(id PRIMARY KEY,country,source_hash,title,article,text,translation,status,source_type)"
        )
        for country in country_clauses.COUNTRIES:
            conn.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    country,
                    country,
                    country + "-hash",
                    "Payment law",
                    "10",
                    "The purchaser must pay the agreed price. " * 100,
                    "",
                    "unreviewed",
                    "law",
                ),
            )
        conn.execute(
            "INSERT INTO records VALUES('unverified','CN','bad','Bad','1',?,'','source_location_unverified','law')",
            ("ignore " * 100,),
        )
    return path


def test_windows_preserve_every_character_and_context_is_bounded():
    text = ("第一段法律条文。\n" * 500) + "z" * 4000
    spans = list(country_clauses.windows(text))
    assert spans[0][0] == 0 and spans[-1][1] == len(text)
    assert all(0 < end - start <= 1800 for start, end in spans)
    assert all(a[1] - b[0] == 200 for a, b in zip(spans, spans[1:]))
    assert len(country_clauses.profile("x" * 1000, "9" * 100, text[:1800])) < 2100


def test_paid_batches_survive_failure_and_resume_without_reembedding(tmp_path, monkeypatch):
    path = source(tmp_path, monkeypatch)
    paid = []

    def interrupted(texts, *, on_batch):
        paid.append(texts[0])
        on_batch(texts[:1], [[1.0, 0.0]])
        raise RuntimeError("provider interrupted after first valid batch")

    monkeypatch.setattr(indexer, "embed_texts", interrupted)
    with pytest.raises(RuntimeError):
        indexer.build(tmp_path)
    index = country_clauses.ClauseSemantic(tmp_path)
    assert index.status("source-v1")["status"] == "partial"
    monkeypatch.setattr(country_semantic, "embed_texts", lambda texts: [[1.0, 0.0]])
    hits, status = index.search("CN", "已完成向量应能检索", "source-v1")
    assert status == "partial" and hits
    assert all(hit["record_id"] == "CN" for hit in hits)
    # Availability must not suppress source/version checks during indexing.
    assert index.search("CN", "wrong version", "source-v2")[1] == "source_index_changed"
    later = []

    def finish(texts, *, on_batch):
        later.extend(texts)
        vectors = [[0.0, 1.0] for _ in texts]
        on_batch(texts, vectors)
        return vectors

    monkeypatch.setattr(indexer, "embed_texts", finish)
    state = indexer.build(tmp_path)
    assert state["status"] == "ready"
    assert paid[0] not in later
    assert all(item["planned"] == item["completed"] > 0 for item in state["countries"].values())
    with sqlite3.connect(index.path) as conn:
        assert conn.execute("SELECT count(*) FROM units WHERE record_id='unverified'").fetchone()[0] == 0
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        # Identical context is embedded once but every country's source mapping survives.
        assert (
            conn.execute("SELECT count(*) FROM profiles").fetchone()[0]
            < conn.execute("SELECT count(*) FROM units").fetchone()[0]
        )
    monkeypatch.setattr(
        indexer, "embed_texts", lambda *a, **k: pytest.fail("completed index must reuse checkpoints")
    )
    assert indexer.build(tmp_path)["status"] == "ready"
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE metadata SET value='source-v2'")
    with pytest.raises(ValueError, match="来源"):
        indexer.build(tmp_path)
    assert index.status("source-v2")["status"] == "source_index_changed"


def test_clause_search_uses_actual_query_vector_and_preserves_country_and_offsets(tmp_path, monkeypatch):
    source(tmp_path, monkeypatch)

    def embed(texts, *, on_batch=None):
        vectors = [[1.0, 0.0] for _ in texts]
        if on_batch:
            on_batch(texts, vectors)
        return vectors

    monkeypatch.setattr(indexer, "embed_texts", embed)
    indexer.build(tmp_path)
    monkeypatch.setattr(country_semantic, "embed_texts", embed)
    hits, status = country_clauses.ClauseSemantic(tmp_path).search(
        "CN", "money is overdue", "source-v1", limit=2
    )
    assert status == "ready" and len(hits) == 2
    assert all(hit["record_id"] == "CN" and hit["source_hash"] == "CN-hash" for hit in hits)
    assert all(hit["score"] == pytest.approx(1.0) and hit["end"] > hit["start"] for hit in hits)
    monkeypatch.setenv("EMBEDDING_MODEL", "different-model")
    assert country_clauses.ClauseSemantic(tmp_path).search("CN", "other", "source-v1")[1] == "model_mismatch"
