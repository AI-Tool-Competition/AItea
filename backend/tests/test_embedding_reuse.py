"""Warm numeric cache is source/owner/version fenced; all providers are mocks."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Barrier

import numpy as np
import pytest

from app import embedding_reuse, ingestion
from app.db import dump, now
from app.service import Service

SOURCE = "https://official.invalid/same-original.txt"
FP = "test-current-context-model"


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("TEA_PROCESS_WORKERS", "4")
    value = Service(tmp_path / "private")
    with value.db.transaction() as conn:
        for owner in ("owner", "other"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 1, now()))
    monkeypatch.setattr(value, "embedding_fingerprint", lambda: FP)
    monkeypatch.setattr(ingestion, "embed_texts", lambda *_: pytest.fail("Unexpected provider call"))
    return value


def register(
    service,
    key,
    *,
    owner="owner",
    country="ID",
    level="primary",
    url=SOURCE,
    content=b"Same immutable legal source.",
    count=2,
    context="shared",
):
    blob = service.archive.blobs.put_bytes(owner, content)
    manifest = {
        "source_hash": blob.digest,
        "elements": [{"element_key": "e1", "content_text": "Official complete text."}],
        "chunks": [
            {
                "id": f"chunk-{n}",
                "text": f"Exact source clause {n}.",
                "retrieval_text": f"{context} context clause {n}.",
                "locator": {"country": country, "page": n + 1},
            }
            for n in range(count)
        ],
    }
    with service.db.transaction() as conn:
        doc = service.archive.store_original(
            conn,
            owner,
            blob,
            key + ".txt",
            "text/plain",
            url,
            collection="country:" + country,
            level=level,
            tags=["country:" + country],
            identity_key=key,
            kind_hint="law",
        )
        conn.execute("UPDATE documents SET manifest=?,status='ready' WHERE id=?", (dump(manifest), doc["id"]))
        service.index.sync_document(conn, doc["id"], manifest)
        conn.execute(
            "UPDATE processing_jobs SET phase='embed',status='queued',available_at=0 WHERE document_id=?",
            (doc["id"],),
        )
        return dict(service.own_document(conn, owner, doc["id"]))


def seed_vectors(service, doc, *, limit=128, fingerprint=FP):
    with service.db.transaction() as conn:
        batch = service.index.pending(conn, doc["id"], fingerprint, limit=limit)
        service.index.store_vectors(conn, doc["id"], fingerprint, batch, [[2.0, 3.0] for _ in batch])
        conn.execute("UPDATE processing_jobs SET status='completed' WHERE document_id=?", (doc["id"],))
    return batch


def stored(service, doc):
    with closing(service.db.connect()) as conn:
        row = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone())
        row["manifest"] = json.loads(row["manifest"])
        return row


def vectors(service, doc):
    with closing(service.db.connect()) as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT v.*,c.chunk_id,c.locator FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id=? ORDER BY c.ordinal",
                (doc["id"],),
            )
        ]


def process(service, doc):
    claim = service.collection.claim_processing(owner_id=doc["owner_id"], document_ids=[doc["id"]])
    assert claim
    service.collection.process_item(claim)
    return claim


def plan(service, doc):
    with service.db.transaction() as conn:
        current = service.own_document(conn, doc["owner_id"], doc["id"])
        batch = service.index.pending(conn, doc["id"], FP)
        if not batch:
            batch = [
                dict(row)
                for row in conn.execute(
                    "SELECT rowid,chunk_id,retrieval_text,input_hash FROM retrieval_chunks WHERE document_id=? ORDER BY ordinal LIMIT 128",
                    (doc["id"],),
                )
            ]
        return embedding_reuse.prepare_batch(service, conn, current, FP, batch)


@pytest.fixture
def pair(service):
    donor = register(service, "donor")
    target = register(service, "target", country="SG")
    seed_vectors(service, donor)
    return service, donor, target


def test_all_hit_has_no_provider_and_keeps_target_locators_review_and_source(pair):
    service, donor, target = pair
    before = stored(service, donor)
    original = stored(service, target)
    process(service, target)
    after = stored(service, target)
    proof = after["manifest"]["embedding_reuse"]
    assert proof["last_batch"]["reused_vectors"] == 2
    assert proof["last_batch"]["provider_input_chunks"] == proof["last_batch"]["embed_texts_invocations"] == 0
    assert after["manifest"]["chunks"] == original["manifest"]["chunks"]
    assert stored(service, donor) == before
    assert all(json.loads(v["locator"])["country"] == "SG" for v in vectors(service, target))
    for v in vectors(service, target):
        p = proof["records"][v["chunk_id"]]
        assert p["source_document_id"] == donor["id"] and p["target_document_id"] == target["id"]
        assert p["target_vector_sha256"] == hashlib.sha256(v["vector"]).hexdigest()
        assert p["store_vectors_renormalized"] and not p["target_locators_or_approval_copied"]
    with closing(service.db.connect()) as conn:
        assert service.catalog.metadata_for(target["id"], conn=conn)["metadata"]["review_status"] == "draft"
        assert not conn.execute("SELECT 1 FROM legal_relations").fetchone()
        assert not conn.execute(
            "SELECT 1 FROM retrieval_dirty WHERE document_id=?", (target["id"],)
        ).fetchone()
        assert (
            conn.execute(
                "SELECT status FROM processing_jobs WHERE document_id=?", (target["id"],)
            ).fetchone()[0]
            == "completed"
        )
    assert not service.search("owner", "source clause", country="SG")["items"]


def test_mixed_hits_call_provider_only_for_exact_misses(service, monkeypatch):
    donor, target = register(service, "donor"), register(service, "target", country="SG")
    seed_vectors(service, donor, limit=1)
    calls = []
    monkeypatch.setattr(
        ingestion, "embed_texts", lambda texts: (calls.append(texts), [[3.0, 4.0] for _ in texts])[1]
    )
    process(service, target)
    assert calls == [["shared context clause 1."]]
    proof = stored(service, target)["manifest"]["embedding_reuse"]
    assert len(vectors(service, target)) == 2 and proof["last_batch"]["reused_vectors"] == 1
    assert proof["last_batch"]["provider_input_chunks"] == proof["last_batch"]["embed_texts_invocations"] == 1


@pytest.mark.parametrize(
    "problem",
    [
        "owner",
        "sha",
        "url",
        "version",
        "secondary",
        "dirty",
        "incomplete",
        "old_family",
        "fingerprint",
        "context",
        "raw_text",
        "failed_status",
    ],
)
def test_ineligible_donor_never_hits(pair, problem):
    service, donor, target = pair
    newer = (
        register(service, "new-family-placeholder", content=b"Newer original.")
        if problem == "old_family"
        else None
    )
    with service.db.transaction() as conn:
        if problem in {"owner", "sha", "url", "version"}:
            column, value = {
                "owner": ("owner_id", "other"),
                "sha": ("content_hash", "0" * 64),
                "url": ("source_url", SOURCE + "?other"),
                "version": ("version", 2),
            }[problem]
            conn.execute(f"UPDATE documents SET {column}=? WHERE id=?", (value, donor["id"]))
        elif problem == "secondary":
            conn.execute(
                "UPDATE legal_classifications SET level='secondary' WHERE document_id=?", (donor["id"],)
            )
        elif problem == "dirty":
            conn.execute("INSERT INTO retrieval_dirty VALUES(?)", (donor["id"],))
        elif problem == "incomplete":
            conn.execute("UPDATE retrieval_documents SET complete=0 WHERE document_id=?", (donor["id"],))
        elif problem == "failed_status":
            conn.execute("UPDATE documents SET status='failed' WHERE id=?", (donor["id"],))
        elif problem == "old_family":
            conn.execute(
                "UPDATE documents SET family_id=?,version=2 WHERE id=?", (donor["family_id"], newer["id"])
            )
        elif problem == "fingerprint":
            conn.execute("INSERT INTO retrieval_models VALUES('old',2)")
            conn.execute(
                "UPDATE retrieval_vectors SET fingerprint='old' WHERE chunk_rowid IN (SELECT rowid FROM retrieval_chunks WHERE document_id=?)",
                (donor["id"],),
            )
        else:
            column = "retrieval_text" if problem == "context" else "text"
            value = "Different source context" if problem == "context" else "Different decoded text"
            from app.retrieval import input_hash

            hash_column = "input_hash" if problem == "context" else "content_hash"
            hashed = input_hash(value) if problem == "context" else hashlib.sha256(value.encode()).hexdigest()
            conn.execute(
                f"UPDATE retrieval_chunks SET {column}=?,{hash_column}=? WHERE document_id=?",
                (value, hashed, donor["id"]),
            )
    assert not plan(service, target)["hits"]


@pytest.mark.parametrize("problem", ["secondary", "role", "dirty", "incomplete", "old_family"])
def test_target_ineligible_for_cache(service, problem):
    donor, target = register(service, "donor"), register(service, "target", country="SG")
    seed_vectors(service, donor)
    newer = register(service, "newer", content=b"new") if problem == "old_family" else None
    with service.db.transaction() as conn:
        if problem == "secondary":
            conn.execute(
                "UPDATE legal_classifications SET level='secondary' WHERE document_id=?", (target["id"],)
            )
        if problem == "role":
            conn.execute("UPDATE documents SET role='secondary' WHERE id=?", (target["id"],))
        if problem == "dirty":
            conn.execute("INSERT INTO retrieval_dirty VALUES(?)", (target["id"],))
        if problem == "incomplete":
            conn.execute("UPDATE retrieval_documents SET complete=0 WHERE document_id=?", (target["id"],))
        if newer:
            conn.execute(
                "UPDATE documents SET family_id=?,version=2 WHERE id=?", (target["family_id"], newer["id"])
            )
    p = plan(service, target)
    assert not p["target"]["eligible"] and not p["hits"]


@pytest.mark.parametrize(
    "problem", ["metadata", "classification", "index", "input", "blob", "fingerprint", "version", "manifest"]
)
def test_second_transaction_rejects_changed_target_and_rolls_back(pair, problem, monkeypatch):
    service, donor, target = pair
    original = embedding_reuse.assert_current

    def invalidate(s, conn, p):
        if problem == "metadata":
            conn.execute(
                "UPDATE document_metadata SET revision=revision+1 WHERE document_id=?", (target["id"],)
            )
        if problem == "classification":
            conn.execute(
                "UPDATE legal_classifications SET revision=revision+1 WHERE document_id=?", (target["id"],)
            )
        if problem == "index":
            conn.execute(
                "UPDATE retrieval_documents SET revision=revision+1 WHERE document_id=?", (target["id"],)
            )
        if problem == "input":
            conn.execute(
                "UPDATE retrieval_chunks SET retrieval_text='changed' WHERE document_id=?", (target["id"],)
            )
        if problem == "blob":
            from pathlib import Path

            path = Path(target["storage_path"])
            path.chmod(0o600)
            path.write_bytes(b"Corrupted original")
        if problem == "fingerprint":
            monkeypatch.setattr(s, "embedding_fingerprint", lambda: "other-fp")
        if problem == "version":
            conn.execute("UPDATE documents SET version=2 WHERE id=?", (target["id"],))
        if problem == "manifest":
            conn.execute(
                "UPDATE documents SET manifest=json_set(manifest,'$.new_source_hint','changed') WHERE id=?",
                (target["id"],),
            )
        return original(s, conn, p)

    monkeypatch.setattr(embedding_reuse, "assert_current", invalidate)
    process(service, target)
    assert vectors(service, target) == [] and len(vectors(service, donor)) == 2
    assert "embedding_reuse" not in stored(service, target)["manifest"]


@pytest.mark.parametrize("problem", ["metadata", "classification", "index", "input", "vector", "version"])
def test_second_transaction_rechecks_donor_before_copy(pair, problem, monkeypatch):
    service, donor, target = pair
    original = embedding_reuse.assert_current

    def invalidate(s, conn, p):
        if problem == "metadata":
            conn.execute(
                "UPDATE document_metadata SET revision=revision+1 WHERE document_id=?", (donor["id"],)
            )
        if problem == "classification":
            conn.execute(
                "UPDATE legal_classifications SET revision=revision+1 WHERE document_id=?", (donor["id"],)
            )
        if problem == "index":
            conn.execute(
                "UPDATE retrieval_documents SET revision=revision+1 WHERE document_id=?", (donor["id"],)
            )
        if problem == "input":
            conn.execute(
                "UPDATE retrieval_chunks SET retrieval_text='changed' WHERE document_id=?", (donor["id"],)
            )
        if problem == "vector":
            conn.execute(
                "UPDATE retrieval_vectors SET vector=? WHERE chunk_rowid IN (SELECT rowid FROM retrieval_chunks WHERE document_id=?)",
                (np.array([1, 0], dtype="<f4").tobytes(), donor["id"]),
            )
        if problem == "version":
            conn.execute("UPDATE documents SET version=2 WHERE id=?", (donor["id"],))
        return original(s, conn, p)

    monkeypatch.setattr(embedding_reuse, "assert_current", invalidate)
    process(service, target)
    assert not vectors(service, target) and len(vectors(service, donor)) == 2


@pytest.mark.parametrize(
    "values,dimension",
    [([float("nan"), 0], 2), ([float("inf"), 0], 2), ([0, 0], 2), ([2, 0], 2), ([1, 0, 0], 3)],
)
def test_invalid_cache_never_falls_back_to_provider(pair, values, dimension):
    service, donor, target = pair
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE retrieval_vectors SET dimension=?,vector=? WHERE chunk_rowid IN (SELECT rowid FROM retrieval_chunks WHERE document_id=?)",
            (dimension, np.array(values, dtype="<f4").tobytes(), donor["id"]),
        )
    process(service, target)
    assert not vectors(service, target) and "embedding_reuse" not in stored(service, target)["manifest"]


def test_invalid_vector_length_and_dimension_bounds_rejected():
    from app.retrieval import stored_vector

    for blob, dimension, expected in [(b"x", 2, 2), (b"", 0, 0), (b"", 65537, 65537), (b"1234", True, 1)]:
        with pytest.raises(ValueError):
            stored_vector(blob, dimension, expected)


def test_expired_lease_cannot_copy_hits_or_provenance(pair, monkeypatch):
    service, donor, target = pair
    original = embedding_reuse.verify_originals

    def expire(s, p):
        original(s, p)
        with s.db.transaction() as conn:
            conn.execute("UPDATE processing_jobs SET lease_until=0 WHERE document_id=?", (target["id"],))

    monkeypatch.setattr(embedding_reuse, "verify_originals", expire)
    process(service, target)
    assert not vectors(service, target) and len(vectors(service, donor)) == 2


def test_provider_failure_and_late_provenance_failure_preserve_atomicity(service, monkeypatch):
    donor, target = register(service, "donor"), register(service, "target", country="SG")
    seed_vectors(service, donor, limit=1)

    def fail(*_):
        raise ValueError("Provider mock failure")

    monkeypatch.setattr(ingestion, "embed_texts", fail)
    process(service, target)
    assert not vectors(service, target) and len(vectors(service, donor)) == 1
    with service.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET available_at=0 WHERE document_id=?", (target["id"],))
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1, 0] for _ in texts])
    monkeypatch.setattr(embedding_reuse, "record_commit", fail)
    process(service, target)
    assert not vectors(service, target) and len(vectors(service, donor)) == 1


def test_raw_integrity_failure_rejects_whole_batch_before_provider(pair):
    service, _, target = pair
    from pathlib import Path

    path = Path(target["storage_path"])
    path.chmod(0o600)
    path.write_bytes(b"Wrong raw source bytes")
    process(service, target)
    assert not vectors(service, target)


def test_same_cas_is_hashed_once_outside_write_transaction(pair, monkeypatch):
    service, _, target = pair
    calls = []
    original = embedding_reuse._verify_existing

    def verify(path, digest, size):
        with service.db.transaction() as conn:
            conn.execute("SELECT 1")
        calls.append(str(path))
        return original(path, digest, size)

    monkeypatch.setattr(embedding_reuse, "_verify_existing", verify)
    process(service, target)
    assert len(calls) == 1 and len(vectors(service, target)) == 2


def test_concurrent_cold_misses_are_not_claimed_as_deduplicated(service, monkeypatch):
    a, b = register(service, "a"), register(service, "b", country="SG")
    barrier = Barrier(2)
    calls = []

    def provider(texts):
        calls.append(texts)
        barrier.wait(timeout=10)
        return [[1, 0] for _ in texts]

    monkeypatch.setattr(ingestion, "embed_texts", provider)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda doc: process(service, doc), [a, b]))
    assert len(calls) == 2 and len(vectors(service, a)) == len(vectors(service, b)) == 2
    assert all(
        stored(service, d)["manifest"]["embedding_reuse"]["last_batch"]["reused_vectors"] == 0 for d in [a, b]
    )


def test_target_owner_is_required_before_any_donor_lookup(pair):
    service, _, target = pair
    forged = target | {"owner_id": "other"}
    with service.db.transaction() as conn:
        batch = service.index.pending(conn, target["id"], FP)
        with pytest.raises(ValueError, match="资料"):
            embedding_reuse.prepare_batch(service, conn, forged, FP, batch)
    assert not vectors(service, target)


def test_real_secondary_processing_keeps_zero_vectors_and_no_reuse(service):
    donor = register(service, "donor")
    target = register(service, "target", country="SG", level="secondary")
    seed_vectors(service, donor)
    process(service, target)
    assert not vectors(service, target) and "embedding_reuse" not in stored(service, target)["manifest"]


def test_donor_budget_uses_narrow_indexes_and_ignores_ninth(service):
    donors = [register(service, str(i)) for i in range(9)]
    seed_vectors(service, donors[-1])
    target = register(service, "target", country="SG")
    with service.db.transaction() as conn:
        traces = []
        conn.set_trace_callback(traces.append)
        p = embedding_reuse.prepare_batch(
            service, conn, target, FP, service.index.pending(conn, target["id"], FP)
        )
    assert not p["hits"]
    assert sum("documents INDEXED BY documents_source" in t for t in traces) == 1
    assert any("LIMIT 8" in t for t in traces)
    assert not any("FROM retrieval_vectors WHERE fingerprint=" in t for t in traces)


@pytest.mark.parametrize("changed", ["target_metadata", "target_secondary", "target_input", "donor_index"])
def test_real_between_transactions_mutation_rejects_mixed_batch(service, monkeypatch, changed):
    donor, target = register(service, "donor"), register(service, "target", country="SG")
    seed_vectors(service, donor, limit=1)
    calls = []

    def provider(texts):
        calls.append(texts)
        with service.db.transaction() as conn:
            if changed == "target_metadata":
                conn.execute(
                    "UPDATE document_metadata SET revision=revision+1 WHERE document_id=?", (target["id"],)
                )
            elif changed == "target_secondary":
                conn.execute(
                    "UPDATE legal_classifications SET level='secondary',revision=revision+1 WHERE document_id=?",
                    (target["id"],),
                )
            elif changed == "target_input":
                m = json.loads(
                    conn.execute("SELECT manifest FROM documents WHERE id=?", (target["id"],)).fetchone()[0]
                )
                m["chunks"][1]["retrieval_text"] = "new actual context"
                conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(m), target["id"]))
                service.index.sync_document(conn, target["id"], m)
            else:
                conn.execute(
                    "UPDATE retrieval_documents SET revision=revision+1 WHERE document_id=?", (donor["id"],)
                )
        return [[1, 0] for _ in texts]

    monkeypatch.setattr(ingestion, "embed_texts", provider)
    process(service, target)
    assert calls == [["shared context clause 1."]]
    assert not vectors(service, target) and len(vectors(service, donor)) == 1
    assert "embedding_reuse" not in stored(service, target)["manifest"]


def test_normalized_target_hash_is_actual_and_may_differ_from_donor(pair):
    service, donor, target = pair
    value = np.array([1 + np.finfo(np.float32).eps, 0], dtype="<f4").tobytes()
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE retrieval_vectors SET vector=? WHERE chunk_rowid IN (SELECT rowid FROM retrieval_chunks WHERE document_id=?)",
            (value, donor["id"]),
        )
    process(service, target)
    records = stored(service, target)["manifest"]["embedding_reuse"]["records"]
    assert all(
        r["donor_vector_sha256"] == hashlib.sha256(value).hexdigest()
        and not r["bytes_identical_after_normalization"]
        for r in records.values()
    )
    assert all(
        r["target_vector_sha256"] == hashlib.sha256(v["vector"]).hexdigest()
        for v in vectors(service, target)
        for r in [records[v["chunk_id"]]]
    )
    assert all(v["vector"] == value for v in vectors(service, donor))


@pytest.mark.parametrize("bound", ["MAX_PROVENANCE_BYTES", "MAX_PROVENANCE_RECORDS"])
def test_provenance_overflow_rolls_back_vectors(pair, monkeypatch, bound):
    service, donor, target = pair
    monkeypatch.setattr(embedding_reuse, bound, 1)
    process(service, target)
    assert not vectors(service, target) and len(vectors(service, donor)) == 2


def test_raw_proof_is_required_even_for_all_hit_plan(pair):
    service, _, target = pair
    p = plan(service, target)
    with service.db.transaction() as conn:
        with pytest.raises(ValueError, match="原件尚未"):
            embedding_reuse.assert_current(service, conn, p)


def test_symlink_parent_is_rejected_before_source_hash_or_provider(pair):
    from pathlib import Path

    service, _, target = pair
    owner_dir = Path(target["storage_path"]).parents[2]
    saved = owner_dir.with_name(owner_dir.name + "-fixture-original")
    owner_dir.rename(saved)
    owner_dir.symlink_to(saved, target_is_directory=True)
    process(service, target)
    assert not vectors(service, target)


@pytest.mark.parametrize("problem", ["metadata", "tags", "batch", "input"])
def test_corrupt_or_overlimit_state_rejected_before_provider(pair, problem):
    service, _, target = pair
    with service.db.transaction() as conn:
        if problem == "metadata":
            conn.execute(
                "UPDATE document_metadata SET payload=? WHERE document_id=?", ("x" * 65537, target["id"])
            )
        elif problem == "tags":
            conn.execute(
                "UPDATE legal_classifications SET tags=? WHERE document_id=?", ("x" * 65537, target["id"])
            )
        elif problem == "input":
            conn.execute(
                "UPDATE retrieval_chunks SET retrieval_text=? WHERE document_id=?",
                ("x" * (embedding_reuse.MAX_INPUT_BYTES + 1), target["id"]),
            )
        batch = service.index.pending(conn, target["id"], FP)
        if problem == "batch":
            batch = batch * 65
        with pytest.raises(ValueError):
            embedding_reuse.prepare_batch(service, conn, target, FP, batch)
    assert not vectors(service, target)
