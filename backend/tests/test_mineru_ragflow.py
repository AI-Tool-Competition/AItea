"""Source-bound cloud parsing, resumable indexing and scoped RAGFlow retrieval."""

import hashlib
import io
import json
import time
import zipfile
from contextlib import closing
from copy import deepcopy
from datetime import date
from threading import Lock

import pytest
from app.bootstrap import Service
from app.contexts.documents.infrastructure.parsers.ingestion_quality import text_complete
from app.contexts.documents.infrastructure.providers.mineru import (
    Admission,
    MinerUWait,
    manifest_valid,
    parse_profile_hash,
)
from app.contexts.documents.infrastructure.providers.mineru_results import (
    _assemble,
    _prepare,
    _read_archive,
    reference,
    resolve_reference,
)
from app.contexts.documents.infrastructure.providers.native_asset_ocr import assets
from app.contexts.documents.infrastructure.providers.ragflow import RAGFlowError, chunk_content
from app.shared.domain.values import dump, now
from test_retrieval_index import body, seed


def test_mineru_retry_generation_creates_a_fresh_remote_parse_identity():
    profile = {"provider": "mineru", "model_version": "vlm"}

    baseline = parse_profile_hash(profile)
    assert baseline == hashlib.sha256(dump(profile).encode()).hexdigest()
    assert parse_profile_hash(profile, "retry-1") != baseline
    assert parse_profile_hash(profile, "retry-2") != parse_profile_hash(profile, "retry-1")
    with pytest.raises(ValueError):
        parse_profile_hash(profile, "unsafe/retry")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("RAGFLOW_BASE_URL", "http://127.0.0.1:19380")
    monkeypatch.setenv("RAGFLOW_API_KEY", "test-private-key")
    monkeypatch.setenv("RAGFLOW_EMBEDDING_MODEL", "test-model-instance")
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        for owner in ("owner", "other"):
            conn.execute("INSERT INTO users VALUES(?,?,NULL,NULL,1,?)", (owner, owner, now()))
    return service


class Remote:
    def __init__(self):
        self.chunks, self.calls, self.lock = {}, [], Lock()
        self.lose_response = False

    def call(self, method, path, *, payload=None, params=None, files=None, timeout=None):
        with self.lock:
            self.calls.append((method, path, payload, params))
            if path == "/datasets":
                if method == "GET":
                    return []
                assert payload["parse_type"] == 1 and "chunk_method" not in payload
                assert payload["permission"] == "me"
                return {"id": "dataset-owner"}
            if path.endswith("/documents"):
                if method == "GET":
                    assert "keywords" in params and "name" not in params
                    return {"docs": []}
                if params and params.get("type") == "empty":
                    assert payload and payload["name"].startswith("aitea-")
                    assert files is None
                    return [{"id": "document-target"}]
                assert files["file"][1].read()
                return [{"id": "document-target"}]
            if path.endswith("/chunks"):
                if method == "GET":
                    return {"chunks": list(self.chunks.values())}
                content = payload["content"]
                identifier = hashlib.sha256(content.encode()).hexdigest()[:16]
                # The live RAGFlow chunks listing returns indexed text under
                # content_with_weight rather than the create request's content.
                self.chunks[identifier] = {"chunk_id": identifier, "content_with_weight": content}
                if self.lose_response:
                    self.lose_response = False
                    raise RAGFlowError("ReadTimeout", unknown=True)
                return {"chunk": {"id": identifier}}
            raise AssertionError((method, path))


def prepared(service, monkeypatch, *, count=1, source_bytes=b"lawful tea clause"):
    remote = Remote()
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.ragflow.RAGFlowClient", lambda: remote
    )
    original = service.archive.blobs.put_bytes("owner", source_bytes)
    with service.db.transaction() as conn:
        seed(conn, "target", body(count=count))
        conn.execute(
            "UPDATE documents SET storage_path=?,content_hash=?,size=? WHERE id='target'",
            (str(original.path), original.digest, original.size),
        )
        service.index.sync_document(conn, "target")
        service.collection.queue_processing(conn, "target", "owner")
        conn.execute("UPDATE processing_jobs SET phase='embed' WHERE document_id='target'")
    claim = service.collection.claim_processing(owner_id="owner", document_ids=["target"])
    assert claim
    with closing(service.db.connect()) as conn:
        document = dict(service.own_document(conn, "owner", "target"))
    return remote, document, claim


def test_processing_queue_finishes_started_ragflow_index_before_new_uploads(workspace):
    with workspace.db.transaction() as conn:
        for identifier, order in (("new-upload", 1), ("started-index", 2)):
            seed(conn, identifier, body(identifier), order=order)
            workspace.collection.queue_processing(conn, identifier, "owner")
            workspace.index.sync_document(conn, identifier)
            conn.execute(
                "UPDATE processing_jobs SET phase='embed',updated_at=? WHERE document_id=?",
                (
                    "2026-10-13T00:00:00.000001+00:00"
                    if identifier == "new-upload"
                    else "2026-10-13T00:00:00.000002+00:00",
                    identifier,
                ),
            )
        bound = workspace.ragflow.binding(
            conn, "started-index", workspace.embedding_fingerprint(), include_chunks=False
        )
        row, _chunks, generation = bound
        conn.execute(
            """INSERT INTO ragflow_documents(
                generation,document_id,owner_id,profile,source_hash,source_version,index_revision,
                dataset_id,remote_id,name,state,chunk_count,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,'dataset-owner','remote-started','started-index.txt','indexing',?,?,?)""",
            (
                generation,
                row["id"],
                row["owner_id"],
                workspace.embedding_fingerprint(),
                row["content_hash"],
                row["version"],
                row["index_revision"],
                row["chunk_count"],
                now(),
                now(),
            ),
        )

    claim = workspace.collection.claim_processing(
        owner_id="owner", document_ids=["new-upload", "started-index"]
    )

    assert claim and claim["document_id"] == "started-index"


def test_index_uses_cloud_chunks_once_and_resumes(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch, count=3)
    assert workspace.ragflow.step(document, claim) == 1
    assert workspace.ragflow.step(document, claim) == 0
    assert workspace.ragflow.step(document, claim) == 0
    posts = [call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")]
    assert len(posts) == 3
    assert not any("/ingest" in call[1] or "/parse" in call[1] for call in remote.calls)
    with closing(workspace.db.connect()) as conn:
        assert not workspace.index.pending(conn, "target", workspace.embedding_fingerprint())
        assert conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 0


def test_large_source_uses_empty_ragflow_document_and_existing_mineru_chunks(workspace, monkeypatch):
    from app.contexts.documents.infrastructure.providers import ragflow

    source = b"large source already parsed by MinerU"
    monkeypatch.setattr(ragflow, "MAX_DATASET_DOCUMENT_UPLOAD_BYTES", len(source) - 1)
    remote, document, claim = prepared(workspace, monkeypatch, source_bytes=source)

    assert workspace.ragflow.step(document, claim) == 1

    uploads = [call for call in remote.calls if call[0] == "POST" and call[1].endswith("/documents")]
    assert len(uploads) == 1
    assert uploads[0][3] == {"type": "empty"}
    assert uploads[0][2]["name"].startswith("aitea-")
    assert not any(call[1].endswith("/parse") or "/ingest" in call[1] for call in remote.calls)


def test_lost_embedding_response_is_reconciled_without_resubmission(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch)
    workspace.ragflow.step(document, claim)
    remote.lose_response = True
    with pytest.raises(RAGFlowError):
        workspace.ragflow.step(document, claim)
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE ragflow_chunks SET next_at=0")
    assert workspace.ragflow.step(document, claim) == 0
    assert len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")]) == 1


def test_recovery_reconciles_exact_remote_receipt_without_resubmitting(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch)
    assert workspace.ragflow.step(document, claim) == 1
    remote.lose_response = True
    with pytest.raises(RAGFlowError):
        workspace.ragflow.step(document, claim)
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE ragflow_chunks SET intent_at=?,next_at=0", (time.time() - 1000,))
    workspace.collection._failure(claim, RAGFlowError("embedding_outcome_unknown"), processing=True)
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE collection_recovery_jobs SET available_at=0")
    result = workspace.collection.recovery.run_due()
    assert result["reconciled"] == 1, json.dumps(result)
    with closing(workspace.db.connect()) as conn:
        chunk = conn.execute("SELECT state,remote_id FROM ragflow_chunks").fetchone()
        job = conn.execute("SELECT status FROM processing_jobs WHERE document_id='target'").fetchone()
        recovery = conn.execute("SELECT status,reason FROM collection_recovery_jobs").fetchone()
    assert chunk["state"] == "done" and chunk["remote_id"]
    assert job["status"] == "queued"
    assert recovery["status"] == "queued" and recovery["reason"] == "remote_receipts_reconciled"
    assert len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")]) == 1

    claim = workspace.collection.claim_processing(owner_id="owner", document_ids=[document["id"]])
    assert claim
    workspace.collection.process_item(claim)
    with closing(workspace.db.connect()) as conn:
        assert (
            conn.execute("SELECT status FROM processing_jobs WHERE document_id='target'").fetchone()[0]
            == "completed"
        )
        assert conn.execute("SELECT state FROM ragflow_documents").fetchone()[0] == "ready"
        assert conn.execute("SELECT status FROM collection_recovery_jobs").fetchone()[0] == "completed"
    assert len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")]) == 1


def test_recovery_retries_unmatched_paid_intent_after_remote_absence_is_confirmed(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch)
    assert workspace.ragflow.step(document, claim) == 1
    remote.lose_response = True
    with pytest.raises(RAGFlowError):
        workspace.ragflow.step(document, claim)
    remote.chunks.clear()
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE ragflow_chunks SET intent_at=?,next_at=0", (time.time() - 1000,))
    workspace.collection._failure(claim, RAGFlowError("embedding_outcome_unknown"), processing=True)
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE collection_recovery_jobs SET available_at=0")

    result = workspace.collection.recovery.run_due()

    assert result["reconciled"] == 0
    assert result["requeued"] == 1
    assert result["partially_reconciled"] == 0
    with closing(workspace.db.connect()) as conn:
        chunk = conn.execute("SELECT state FROM ragflow_chunks").fetchone()
        job = conn.execute("SELECT status FROM processing_jobs WHERE document_id='target'").fetchone()
        recovery = conn.execute("SELECT status,reason,available_at FROM collection_recovery_jobs").fetchone()
    assert chunk["state"] == "intent"
    assert job["status"] == "queued"
    assert recovery["status"] == "queued" and recovery["reason"] == "unmatched_intents_retry"
    posts_before = len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")])
    for _ in range(4):
        with workspace.db.transaction() as conn:
            conn.execute(
                "UPDATE processing_jobs SET available_at=0 WHERE document_id='target' AND status='queued'"
            )
        claim = workspace.collection.claim_processing(owner_id="owner", document_ids=["target"])
        assert claim
        workspace.collection.process_item(claim)
        with closing(workspace.db.connect()) as conn:
            if (
                conn.execute("SELECT status FROM processing_jobs WHERE document_id='target'").fetchone()[0]
                == "completed"
            ):
                break
    with closing(workspace.db.connect()) as conn:
        assert conn.execute("SELECT state FROM ragflow_documents").fetchone()[0] == "ready"
        assert conn.execute("SELECT state FROM ragflow_chunks").fetchone()[0] == "done"
    posts_after = len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")])
    assert posts_after == posts_before + 1


def test_unknown_paid_index_auto_retries_after_receipt_audit_error_with_backoff(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch)
    assert workspace.ragflow.step(document, claim) == 1
    remote.lose_response = True
    with pytest.raises(RAGFlowError):
        workspace.ragflow.step(document, claim)
    workspace.collection._failure(claim, RAGFlowError("embedding_outcome_unknown"), processing=True)
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE collection_recovery_jobs SET available_at=0")
        conn.execute("UPDATE ragflow_chunks SET intent_at=0,next_at=0")

    def unavailable(*_args, **_kwargs):
        raise RAGFlowError("receipt_audit_unavailable", retry_after=10)

    monkeypatch.setattr(workspace.ragflow, "reconcile_pending_intents", unavailable)
    result = workspace.collection.recovery.run_due()

    assert result["receipt_audit_errors"] == 1 and result["requeued"] == 1
    with closing(workspace.db.connect()) as conn:
        recovery = conn.execute("SELECT status,reason,available_at FROM collection_recovery_jobs").fetchone()
        job = conn.execute("SELECT status,available_at FROM processing_jobs WHERE document_id='target'").fetchone()
        chunk = conn.execute("SELECT state,next_at FROM ragflow_chunks").fetchone()
        event = conn.execute(
            "SELECT event,detail FROM collection_recovery_events ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    assert tuple(recovery[:2]) == ("queued", "unknown_result_auto_retry")
    assert tuple(job[:1]) == ("queued",) and job["available_at"] >= recovery["available_at"]
    assert chunk["state"] == "intent" and chunk["next_at"] >= recovery["available_at"]
    assert event["event"] == "unknown_remote_result_auto_requeued"
    assert json.loads(event["detail"])["duplicate_provider_charge_possible"] is True

    # The retry waits until its durable backoff expires instead of querying the
    # provider once per scheduler tick.
    later = workspace.collection.recovery.run_due()
    assert later["receipt_audit_errors"] == 0 and later["requeued"] == 0


def test_recovery_requeues_legacy_unknown_embedding_without_remote_write_ledger(workspace, monkeypatch):
    remote, _document, claim = prepared(workspace, monkeypatch)
    workspace.collection._failure(claim, RAGFlowError("embedding_outcome_unknown"), processing=True)
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE collection_recovery_jobs SET available_at=0")
    result = workspace.collection.recovery.run_due()
    assert result["reconciled"] == 0 and result["receipt_audit_errors"] == 0
    assert result["requeued"] == 1
    assert remote.calls == []
    with closing(workspace.db.connect()) as conn:
        recovery = conn.execute("SELECT status,reason FROM collection_recovery_jobs").fetchone()
        job = conn.execute("SELECT status FROM processing_jobs WHERE document_id='target'").fetchone()
    assert tuple(recovery) == ("queued", "unstarted_ragflow_retry")
    assert job["status"] == "queued"


def test_recovery_reconciles_partial_receipts_and_retries_unmatched_paid_intents(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch, count=2)
    assert workspace.ragflow.step(document, claim) == 1
    remote.lose_response = True
    with pytest.raises(RAGFlowError):
        workspace.ragflow.step(document, claim)
    with closing(workspace.db.connect()) as conn:
        bound = workspace.ragflow.binding(conn, document["id"], workspace.embedding_fingerprint())
        _doc, chunks, generation = bound
    remote.chunks.clear()
    remote.chunks["verified-receipt"] = {
        "chunk_id": "verified-receipt",
        "content_with_weight": chunk_content(chunks[0]),
    }
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE ragflow_chunks SET state='intent',intent_at=?,next_at=0 WHERE generation=?",
            (time.time() - 100, generation),
        )
    workspace.collection._failure(claim, RAGFlowError("embedding_outcome_unknown"), processing=True)
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE collection_recovery_jobs SET available_at=0")
    posts_before = len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")])

    result = workspace.collection.recovery.run_due()

    assert result["partially_reconciled"] == 0
    assert result["requeued"] == 1
    assert result["reconciled"] == 0
    with closing(workspace.db.connect()) as conn:
        states = [row[0] for row in conn.execute("SELECT state FROM ragflow_chunks ORDER BY local_id")]
        job = conn.execute("SELECT status FROM processing_jobs WHERE document_id='target'").fetchone()[0]
        recovery = conn.execute("SELECT status,reason,available_at FROM collection_recovery_jobs").fetchone()
    assert states == ["done", "intent"] or states == ["intent", "done"]
    assert job == "queued"
    assert recovery["status"] == "queued" and recovery["reason"] == "unmatched_intents_retry"
    posts_after = len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")])
    assert posts_after == posts_before


def test_recent_unmatched_intent_is_retried_after_exact_remote_check(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch)
    assert workspace.ragflow.step(document, claim) == 1
    assert workspace.ragflow.step(document, claim) == 0
    with closing(workspace.db.connect()) as conn:
        _row, _chunks, generation = workspace.ragflow.binding(
            conn, document["id"], workspace.embedding_fingerprint()
        )
    remote.chunks.clear()
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE ragflow_chunks SET state='intent',intent_at=?,next_at=0 WHERE generation=?",
            (time.time() - 1, generation),
        )

    posts_before = len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")])
    assert workspace.ragflow.step(document, claim) == 0

    posts_after = len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")])
    assert posts_after == posts_before + 1
    with closing(workspace.db.connect()) as conn:
        assert conn.execute(
            "SELECT state FROM ragflow_chunks WHERE generation=?", (generation,)
        ).fetchone()[0] == "done"


def test_queue_recovers_lost_embedding_response_without_repeating_the_write(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch)
    workspace.collection.process_item(claim)
    remote.lose_response = True
    claim = workspace.collection.claim_processing(owner_id="owner", document_ids=[document["id"]])
    workspace.collection.process_item(claim)
    with workspace.db.transaction() as conn:
        state = conn.execute(
            "SELECT status,attempts FROM processing_jobs WHERE document_id='target'"
        ).fetchone()
        assert state["status"] == "queued" and state["attempts"] == 0
        conn.execute("UPDATE ragflow_chunks SET next_at=0")
        conn.execute("UPDATE processing_jobs SET available_at=0")
    claim = workspace.collection.claim_processing(owner_id="owner", document_ids=[document["id"]])
    workspace.collection.process_item(claim)
    with closing(workspace.db.connect()) as conn:
        assert (
            conn.execute("SELECT status FROM processing_jobs WHERE document_id='target'").fetchone()[0]
            == "completed"
        )
    assert len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")]) == 1


def test_expired_unconfirmed_embedding_is_retried_after_exact_absence_check(workspace, monkeypatch):
    import time

    remote, document, claim = prepared(workspace, monkeypatch)
    workspace.collection.process_item(claim)
    remote.lose_response = True
    claim = workspace.collection.claim_processing(owner_id="owner", document_ids=[document["id"]])
    workspace.collection.process_item(claim)
    remote.chunks.clear()
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE ragflow_chunks SET intent_at=?,next_at=0", (time.time() - 1000,))
        conn.execute("UPDATE processing_jobs SET available_at=0")
    claim = workspace.collection.claim_processing(owner_id="owner", document_ids=[document["id"]])
    workspace.collection.process_item(claim)
    with closing(workspace.db.connect()) as conn:
        assert (
            conn.execute("SELECT status FROM processing_jobs WHERE document_id='target'").fetchone()[0]
            == "completed"
        )
        assert conn.execute("SELECT state FROM ragflow_documents").fetchone()[0] == "ready"
        assert conn.execute("SELECT state FROM ragflow_chunks").fetchone()[0] == "done"
    assert len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")]) == 2


def test_changed_source_fences_an_old_index_claim(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch)
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE documents SET content_hash=? WHERE id='target'", ("a" * 64,))
    with pytest.raises(RAGFlowError, match="lease_changed"):
        workspace.ragflow.step(document, claim)
    assert not remote.calls


def test_retrieval_sends_only_eligible_ids_and_rechecks_after_remote_call(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch)
    workspace.ragflow.step(document, claim)
    workspace.ragflow.step(document, claim)
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET status='completed',lease_token=NULL,lease_until=NULL")
        seed(conn, "foreign", body(), owner="other")
        workspace.index.sync_document(conn, "foreign")
    seen = []

    def retrieve(query, candidates, *, limit):
        seen.extend(row["document_id"] for row in candidates)
        return [
            {"id": next(iter(remote.chunks)), "document_id": "document-target"},
            {"id": "untrusted", "document_id": "foreign-remote"},
        ]

    monkeypatch.setattr(workspace.ragflow, "retrieve", retrieve)
    result = workspace.search("owner", "tea", country="ID", as_of=date(2026, 10, 23))
    assert seen == ["target"]
    assert [item["document_id"] for item in result["items"]] == ["target"]
    assert result["mode"] == "ragflow"

    def revoke(query, candidates, *, limit):
        with workspace.db.transaction() as conn:
            conn.execute(
                "UPDATE document_metadata SET payload=json_set(payload,'$.review_status','draft'),revision=revision+1 WHERE document_id='target'"
            )
        return retrieve(query, candidates, limit=limit)

    monkeypatch.setattr(workspace.ragflow, "retrieve", revoke)
    assert workspace.search("owner", "tea", country="ID")["items"] == []


def test_missing_ragflow_is_explicit_and_never_uses_local_vectors(workspace, monkeypatch):
    monkeypatch.setenv("RAGFLOW_API_KEY", "")
    result = workspace.search("owner", "tea", country="ID")
    assert result["mode"] == "unavailable" and not result["items"]
    assert "没有执行" in result["warning"]


def test_remote_completion_invalidates_analysis_cache_without_changing_source_revision(
    workspace, monkeypatch
):
    _, document, claim = prepared(workspace, monkeypatch)
    monkeypatch.setattr(workspace.legal, "allowed_countries", lambda *_: [])
    case = {"id": "fixture-case", "template": {"stages": []}}
    with closing(workspace.db.connect()) as conn:
        before = workspace.ragflow.cache_signature(conn, "owner", case)
        revision = conn.execute(
            "SELECT revision FROM retrieval_documents WHERE document_id='target'"
        ).fetchone()[0]
    workspace.ragflow.step(document, claim)
    workspace.ragflow.step(document, claim)
    with closing(workspace.db.connect()) as conn:
        after = workspace.ragflow.cache_signature(conn, "owner", case)
        assert before != after
        assert after == workspace.ragflow.cache_signature(conn, "owner", case)
        assert (
            conn.execute("SELECT revision FROM retrieval_documents WHERE document_id='target'").fetchone()[0]
            == revision
        )


def test_mineru_parts_are_mechanical_and_within_200_pages(tmp_path, monkeypatch):
    from app.shared.infrastructure.persistence.blob_store import BlobStore
    from pypdf import PdfReader, PdfWriter
    from pypdf._page import PageObject

    monkeypatch.setattr(PageObject, "extract_text", lambda *_a, **_k: pytest.fail("local text parser called"))
    writer = PdfWriter()
    for _ in range(201):
        writer.add_blank_page(width=100, height=200)
    out = io.BytesIO()
    writer.write(out)
    store = BlobStore(tmp_path / "files", max_bytes=128 * 1024 * 1024)
    original = store.put_bytes("owner", out.getvalue())
    result = _prepare(
        str(original.path),
        original.digest,
        original.size,
        "document.bin",
        str(store.files_dir),
        "owner",
        "application/pdf",
    )
    assert [(p["first_page"], p["last_page"]) for p in result["parts"]] == [(1, 200), (201, 201)]
    assert [
        len(PdfReader(resolve_reference(p["blob"], store.files_dir, "owner")).pages) for p in result["parts"]
    ] == [200, 1]


def test_mineru_parts_also_obey_file_size_limit(tmp_path, monkeypatch):
    from app.contexts.documents.infrastructure.providers import mineru_results
    from app.shared.infrastructure.persistence.blob_store import BlobStore
    from pypdf import PdfReader, PdfWriter

    part_limit = 2048
    monkeypatch.setattr(mineru_results, "MAX_MINERU_PART_BYTES", part_limit)
    writer = PdfWriter()
    for _ in range(40):
        writer.add_blank_page(width=100, height=200)
    out = io.BytesIO()
    writer.write(out)
    store = BlobStore(tmp_path / "files", max_bytes=128 * 1024 * 1024)
    original = store.put_bytes("owner", out.getvalue())
    result = _prepare(
        str(original.path),
        original.digest,
        original.size,
        "document.pdf",
        str(store.files_dir),
        "owner",
        "application/pdf",
    )

    assert result["page_count"] == 40
    assert result["parts"][0]["first_page"] == 1
    assert result["parts"][-1]["last_page"] == 40
    expected_page = 1
    for part in result["parts"]:
        blob_path = resolve_reference(part["blob"], store.files_dir, "owner")
        assert blob_path.stat().st_size <= part_limit
        assert part["first_page"] == expected_page
        assert part["last_page"] - part["first_page"] + 1 <= 200
        assert len(PdfReader(blob_path).pages) == part["last_page"] - part["first_page"] + 1
        expected_page = part["last_page"] + 1
    assert expected_page == 41


def result_archive(tmp_path, *, unsafe=False, source=b"original source", origin=None, extra=None):
    from app.shared.infrastructure.persistence.blob_store import BlobStore

    store = BlobStore(tmp_path / "files", max_bytes=128 * 1024 * 1024)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("layout.json", json.dumps({"pdf_info": [{"page_idx": 0, "page_size": [600, 800]}]}))
        archive.writestr(
            "task_content_list.json",
            json.dumps(
                [{"type": "text", "text": "tea lawful clause", "page_idx": 0, "bbox": [10, 20, 900, 950]}]
            ),
        )
        archive.writestr("full.md", "tea lawful clause")
        if origin is not None:
            archive.writestr("839d0d23-bf92-4c24-9b57-c8a79ff44512_origin.pdf", origin)
        if extra is not None:
            archive.writestr(*extra)
        if unsafe:
            archive.writestr("../escape.txt", "invalid")
    raw = store.put_bytes("owner", out.getvalue())
    original = store.put_bytes("owner", source)
    task = {
        "id": "task-1",
        "batch_id": "batch-1",
        "state": "done",
        "source_hash": original.digest,
        "first_page": 1,
        "last_page": 1,
        "part_blob": reference(original),
        "result_blob": reference(raw),
    }
    return store, task


def blank_pdf(page_count):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_mineru_skips_verified_source_pdf_origin_copy_above_member_limit(tmp_path, monkeypatch):
    from app.contexts.documents.infrastructure.providers import mineru_results

    source = blank_pdf(1)
    assert len(source) > 200
    monkeypatch.setattr(mineru_results, "MAX_MEMBER_BYTES", 200)
    store, task = result_archive(tmp_path, source=source, origin=source)

    blocks, layout, artifacts, _, render_pdf = _read_archive(task, store, "owner")

    assert len(blocks) == 1 and len(layout) == 1
    assert render_pdf is None
    assert "839d0d23-bf92-4c24-9b57-c8a79ff44512_origin.pdf" not in artifacts


def test_mineru_validates_office_conversion_pdf_without_materializing_it(tmp_path, monkeypatch):
    from app.contexts.documents.infrastructure.providers import mineru_results

    source = b"submitted" * 100
    derivative = blank_pdf(1)
    assert len(derivative) > 200 and derivative != source
    monkeypatch.setattr(mineru_results, "MAX_MEMBER_BYTES", 200)
    store, task = result_archive(tmp_path, source=source, origin=derivative)

    _, layout, artifacts, _, render_pdf = _read_archive(task, store, "owner")

    assert "839d0d23-bf92-4c24-9b57-c8a79ff44512_origin.pdf" not in artifacts
    assert len(layout) == 1
    assert render_pdf is None


def test_mineru_office_conversion_pdf_is_bound_to_the_remote_result_receipt(tmp_path):
    from app.contexts.documents.infrastructure.providers.mineru import manifest_valid

    store, task = result_archive(tmp_path, source=b"submitted Office file", origin=blank_pdf(1))
    manifest = _assemble([task], task["source_hash"], "source.doc", str(store.files_dir), "owner", None)
    part = manifest["remote_parse"]["parts"][0]
    from app.contexts.documents.infrastructure.providers.mineru_results import _extract_render_pdf

    render_pdf = _extract_render_pdf(part, str(store.files_dir), "owner")
    part["render_pdf"] = render_pdf

    assert manifest_valid(manifest)
    assert render_pdf["source_hash"] == task["source_hash"]
    assert render_pdf["task_id"] == task["id"]
    assert render_pdf["result_hash"] == task["result_blob"]["content_hash"]
    invalid = json.loads(json.dumps(manifest))
    invalid["remote_parse"]["parts"][0]["render_pdf"]["page_count"] = 2
    assert not manifest_valid(invalid)


def test_mineru_legacy_office_result_materializes_conversion_pdf_on_demand(tmp_path):
    from pypdf import PdfReader
    from app.contexts.documents.infrastructure.parsers.document_parser import _run_bounded
    from app.contexts.documents.infrastructure.providers.mineru_results import worker

    store, task = result_archive(tmp_path, source=b"legacy Office source", origin=blank_pdf(1))
    part = {
        "task_id": task["id"],
        "batch_id": task["batch_id"],
        "source_hash": task["source_hash"],
        "first_page": task["first_page"],
        "last_page": task["last_page"],
        "part_blob": task["part_blob"],
        "result_blob": task["result_blob"],
    }

    receipt = _run_bounded(
        worker,
        ("extract_render_pdf", (part, str(store.files_dir), "owner")),
        120,
    )

    assert receipt["task_id"] == task["id"]
    assert receipt["source_hash"] == task["source_hash"]
    assert receipt["result_hash"] == task["result_blob"]["content_hash"]
    assert receipt["page_count"] == 1
    assert PdfReader(resolve_reference(receipt["blob"], store.files_dir, "owner")).pages


def test_mineru_rejects_provider_origin_pdf_with_wrong_page_count(tmp_path, monkeypatch):
    from app.contexts.documents.infrastructure.providers import mineru_results

    monkeypatch.setattr(mineru_results, "MAX_MEMBER_BYTES", 200)
    store, task = result_archive(tmp_path, source=b"submitted" * 100, origin=blank_pdf(2))

    with pytest.raises(ValueError, match="页数与提交分片不匹配"):
        _read_archive(task, store, "owner")


def test_mineru_keeps_member_limit_for_unrelated_zip_members(tmp_path, monkeypatch):
    from app.contexts.documents.infrastructure.providers import mineru_results

    monkeypatch.setattr(mineru_results, "MAX_MEMBER_BYTES", 200)
    store, task = result_archive(tmp_path, extra=("unexpected.bin", b"x" * 201))

    with pytest.raises(ValueError, match="不安全"):
        _read_archive(task, store, "owner")


def test_mineru_retains_artifacts_and_requires_page_source_proofs(tmp_path):
    store, task = result_archive(tmp_path)
    manifest = _assemble([task], task["source_hash"], "source.pdf", str(store.files_dir), "owner", None)
    assert text_complete(manifest) and manifest_valid(manifest)
    artifacts = manifest["remote_parse"]["parts"][0]["artifacts"]
    assert {"layout.json", "task_content_list.json", "full.md"} <= artifacts.keys()
    assert all(resolve_reference(ref, store.files_dir, "owner").is_file() for ref in artifacts.values())
    manifest["pages"][0]["text"] = "tampered"
    assert not text_complete(manifest)


def test_mineru_assembles_documents_over_four_million_characters_within_storage_bound(tmp_path):
    from app.contexts.documents.infrastructure.providers.mineru_results import MAX_ASSEMBLED_RESULT_BYTES
    from app.shared.infrastructure.persistence.blob_store import BlobStore

    store = BlobStore(tmp_path / "files", max_bytes=128 * 1024 * 1024)
    body = "x" * 4_100_000
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as result_zip:
        result_zip.writestr("layout.json", json.dumps({"pdf_info": [{"page_idx": 0}]}))
        result_zip.writestr(
            "task_content_list.json",
            json.dumps(
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "bbox": [10, 20, 900, 950],
                        "table_body": f"<table><tr><td>{body}</td></tr></table>",
                    }
                ],
                separators=(",", ":"),
            ),
        )
    original = store.put_bytes("owner", b"source")
    task = {
        "id": "task-large",
        "batch_id": "batch-large",
        "state": "done",
        "source_hash": original.digest,
        "first_page": 1,
        "last_page": 1,
        "part_blob": reference(original),
        "result_blob": reference(store.put_bytes("owner", archive.getvalue())),
    }

    result = _assemble([task], original.digest, "large-schedule.pdf", str(store.files_dir), "owner", None)
    encoded = json.dumps({"result": result}, ensure_ascii=False, allow_nan=False).encode()
    assert text_complete(result)
    assert len(result["elements"]) == 1
    assert len(result["pages"][0]["text"]) > 4_000_000
    assert len(encoded) < MAX_ASSEMBLED_RESULT_BYTES


def test_untrusted_mineru_zip_cannot_escape_storage(tmp_path):
    store, task = result_archive(tmp_path, unsafe=True)
    with pytest.raises(ValueError, match="不安全"):
        _assemble([task], task["source_hash"], "source.pdf", str(store.files_dir), "owner", None)
    assert not (tmp_path / "escape.txt").exists()


def test_mineru_limit_and_retry_after_are_shared(tmp_path, monkeypatch):
    from app.contexts.documents.infrastructure.providers import mineru as mineru

    monkeypatch.setattr(mineru.time, "time", lambda: 1000)
    first, second = Admission(tmp_path), Admission(tmp_path)
    first.take()
    with pytest.raises(MinerUWait):
        second.take()
    first.backoff(429, 120)
    with pytest.raises(MinerUWait) as waiting:
        second.take()
    assert waiting.value.retry_after >= 120


def test_mineru_download_uses_bounded_transport_and_account_cas(tmp_path, monkeypatch):
    import time

    from app.contexts.documents.infrastructure.providers.mineru import MinerUClient
    from app.shared.infrastructure.persistence.blob_store import BlobStore

    monkeypatch.setenv("MINERU_API_TOKEN", "test-mineru-token")
    store = BlobStore(tmp_path / "files")

    def download(url, *, deadline, consume, max_bytes):
        assert url == "https://results.example/result.zip"
        assert 100 < deadline - time.monotonic() <= 120
        assert max_bytes == 128 * 1024 * 1024
        return {"blob": consume(iter([b"bounded provider result"]))}

    monkeypatch.setattr("app.shared.infrastructure.http.download._download", download)
    result = MinerUClient(tmp_path).download("https://results.example/result.zip", store, "owner")
    assert resolve_reference(result, store.files_dir, "owner").read_bytes() == b"bounded provider result"


def test_mineru_merged_tables_keep_original_text_on_each_physical_page(tmp_path):
    from app.shared.infrastructure.persistence.blob_store import BlobStore

    store = BlobStore(tmp_path / "files", max_bytes=128 * 1024 * 1024)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("layout.json", json.dumps({"pdf_info": [{"page_idx": p} for p in range(2)]}))
        archive.writestr(
            "task_content_list.json",
            json.dumps(
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "bbox": [100, 200, 900, 800],
                        "table_body": "<table><tr><td>MERGED WRONG PAGE</td></tr></table>",
                    },
                    {"type": "table", "page_idx": 1, "bbox": [100, 200, 900, 800]},
                    {"type": "page_footnote", "page_idx": 1, "text": "Legal exception applies."},
                ]
            ),
        )
        archive.writestr(
            "task_model.json",
            json.dumps(
                [
                    [
                        {
                            "type": "table",
                            "bbox": [0.1, 0.2, 0.9, 0.8],
                            "content": "<table><tr><th>Country</th><th>Rate</th></tr><tr><td>Indonesia</td><td>5%</td></tr></table>",
                        }
                    ],
                    [
                        {
                            "type": "table",
                            "bbox": [0.1, 0.2, 0.9, 0.8],
                            "content": "<table><tr><td>China</td><td>0%</td></tr></table>",
                        }
                    ],
                ]
            ),
        )
        archive.writestr("empty-extra.md", b"")
    original = store.put_bytes("owner", b"source")
    task = {
        "id": "task",
        "batch_id": "batch",
        "state": "done",
        "source_hash": original.digest,
        "first_page": 1,
        "last_page": 2,
        "part_blob": reference(original),
        "result_blob": reference(store.put_bytes("owner", out.getvalue())),
    }
    result = _assemble([task], original.digest, "source.pdf", str(store.files_dir), "owner", None)
    assert text_complete(result)
    assert "Indonesia" in result["pages"][0]["text"] and "China" not in result["pages"][0]["text"]
    assert "China" in result["pages"][1]["text"] and "Indonesia" not in result["pages"][1]["text"]
    assert "Legal exception" in result["pages"][1]["text"]
    assert "MERGED WRONG PAGE" not in result["pages"][0]["text"]
    empty = result["remote_parse"]["parts"][0]["artifacts"]["empty-extra.md"]
    assert empty["empty"] and empty["size"] == 0 and "storage_path" not in empty


def capacity_clock(tmp_path, monkeypatch):
    from app.contexts.documents.infrastructure.providers import mineru as mineru

    clock = [1000.0]
    monkeypatch.setattr(mineru.time, "time", lambda: clock[0])
    monkeypatch.setenv("MINERU_AUTO_CONCURRENCY", "true")
    monkeypatch.setenv("MINERU_MAX_ACTIVE_TASKS", "8")
    monkeypatch.setenv("MINERU_CONCURRENCY_CEILING", "128")
    monkeypatch.setenv("MINERU_CONCURRENCY_WINDOW_SECONDS", "60")
    return clock, Admission(tmp_path)


def loaded_stage(clock, admission, limit, *, seconds=80):
    admission.capacity(limit)
    for _ in range(seconds // 10):
        clock[0] += 10
        admission.healthy()
        admission.capacity(limit, completed_pages=3)
    return admission.capacity(limit)


def test_capacity_does_not_claim_stability_without_real_load(tmp_path, monkeypatch):
    clock, admission = capacity_clock(tmp_path, monkeypatch)
    clock[0] += 3600
    value = admission.capacity(1, completed_pages=100)
    assert value["limit"] == 8 and value["stable_limit"] == 0


def test_capacity_increases_backtracks_and_persists_evidence(tmp_path, monkeypatch):
    clock, admission = capacity_clock(tmp_path, monkeypatch)
    assert loaded_stage(clock, admission, 8)["limit"] == 12
    admission.backoff(429, 120)
    value = Admission(tmp_path).capacity(8)
    assert value["limit"] == 8 and value["stable_limit"] == 8 and value["unstable_limit"] == 12
    clock[0] += 121
    assert loaded_stage(clock, admission, 8)["limit"] == 10
    assert admission.capacity()["history"][-1]["reason"] == "stable_stage"


def test_quota_does_not_become_concurrency_boundary(tmp_path, monkeypatch):
    _, admission = capacity_clock(tmp_path, monkeypatch)
    admission.backoff(-60018)
    value = admission.capacity()
    assert value["limit"] == 8 and value["unstable_limit"] is None
    assert value["reason"] == "daily_quota"


def test_inconsistent_worker_configuration_cannot_reset_verified_evidence(tmp_path, monkeypatch):
    from app.contexts.documents.infrastructure.providers.mineru import MinerUError

    clock, admission = capacity_clock(tmp_path, monkeypatch)
    loaded_stage(clock, admission, 8)
    monkeypatch.setenv("MINERU_AUTO_CONCURRENCY", "false")
    with pytest.raises(MinerUError, match="capacity_configuration_changed"):
        Admission(tmp_path).capacity()
    monkeypatch.setenv("MINERU_AUTO_CONCURRENCY", "true")
    value = Admission(tmp_path).capacity()
    assert value["stable_limit"] == 8 and value["limit"] == 12


def test_repeated_transport_failures_reduce_capacity(tmp_path, monkeypatch):
    _, admission = capacity_clock(tmp_path, monkeypatch)
    for _ in range(3):
        admission.backoff("ReadTimeout")
    value = admission.capacity()
    assert value["limit"] == 4 and value["unstable_limit"] == 8


def test_table_spans_preserve_column_alignment():
    from app.contexts.documents.infrastructure.providers.mineru_results import _TableText

    table = _TableText()
    table.feed(
        '<table><tr><th colspan="2">Rates</th></tr><tr><td rowspan="2">Tea</td><td>5%</td></tr><tr><td>0%</td></tr></table>'
    )
    assert table.rows == [["Rates", "Rates"], ["Tea", "5%"], ["Tea", "0%"]]


def test_overlapping_mineru_table_spans_preserve_cell_order_and_require_review(tmp_path):
    from app.shared.infrastructure.persistence.blob_store import BlobStore

    store = BlobStore(tmp_path / "files", max_bytes=128 * 1024 * 1024)
    body = '<table><tr><td>A</td><td rowspan="2">B</td></tr><tr><td colspan="2">C</td></tr></table>'
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("layout.json", json.dumps({"pdf_info": [{"page_idx": 0}]}))
        archive.writestr(
            "task_content_list.json",
            json.dumps(
                [
                    {
                        "type": "table",
                        "page_idx": 0,
                        "bbox": [10, 20, 900, 950],
                        "table_body": body,
                    }
                ]
            ),
        )
    source = store.put_bytes("owner", b"source")
    result_blob = store.put_bytes("owner", output.getvalue())
    task = {
        "id": "task-overlap",
        "batch_id": "batch-overlap",
        "state": "done",
        "source_hash": source.digest,
        "first_page": 1,
        "last_page": 1,
        "part_blob": reference(source),
        "result_blob": reference(result_blob),
    }

    manifest = _assemble([task], source.digest, "source.pdf", str(store.files_dir), "owner", None)
    text = manifest["pages"][0]["text"]
    assert all(value in text for value in ("A", "B", "C"))
    assert "未推断列对齐" in text
    assert manifest["pages"][0]["reasons"] == ["table_structure_ambiguous"]
    assert not text_complete(manifest)
    table_ref = manifest["elements"][0]["metadata_json"]["table_html"]
    assert resolve_reference(table_ref, store.files_dir, "owner").read_text() == body


def test_isolated_failure_does_not_hide_later_transport_failure_streak(tmp_path, monkeypatch):
    _, admission = capacity_clock(tmp_path, monkeypatch)
    admission.backoff("ReadTimeout")
    admission.healthy()
    assert admission.capacity()["limit"] == 8
    for _ in range(3):
        admission.backoff("ConnectTimeout")
    assert admission.capacity()["limit"] == 4


def test_daily_quota_blocks_new_submissions_but_allows_existing_task_polls(tmp_path, monkeypatch):
    clock, admission = capacity_clock(tmp_path, monkeypatch)
    admission.backoff(-60018)
    admission.take()
    clock[0] += 2
    with pytest.raises(MinerUWait) as waiting:
        admission.take(submission=True)
    assert waiting.value.retry_after > 120
    Admission(tmp_path).take()


def test_capacity_refines_boundary_then_verifies_before_holding(tmp_path, monkeypatch):
    clock, admission = capacity_clock(tmp_path, monkeypatch)
    loaded_stage(clock, admission, 8)
    admission.backoff(429, 1)
    clock[0] += 400
    assert loaded_stage(clock, admission, 8)["limit"] == 10
    assert loaded_stage(clock, admission, 10)["limit"] == 11
    assert loaded_stage(clock, admission, 11)["mode"] == "verifying"
    assert admission.capacity()["stable_limit"] == 11
    value = loaded_stage(clock, admission, 11, seconds=200)
    assert value["mode"] == "holding" and value["limit"] == 11
    assert value["reason"] == "verified_after_backoff"


@pytest.mark.parametrize(
    "content,caption,complete",
    [
        ("", "", False),
        ("", "Figure 1. Regional output", False),
        ("<table><tr><td>Tea</td><td>5%</td></tr></table>", "", True),
    ],
)
def test_chart_keeps_asset_and_requires_readable_content(tmp_path, content, caption, complete):
    store, task = result_archive(tmp_path)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("layout.json", json.dumps({"pdf_info": [{"page_idx": 0}]}))
        archive.writestr(
            "task_content_list.json",
            json.dumps(
                [
                    {"type": "text", "text": "lawful tea clause", "page_idx": 0},
                    {
                        "type": "chart",
                        "content": content,
                        "chart_caption": caption,
                        "page_idx": 0,
                        "img_path": "images/chart.jpg",
                    },
                    {"type": "ref_text", "text": "Source reference", "page_idx": 0},
                ]
            ),
        )
        archive.writestr("images/chart.jpg", b"preserved raw image")
    task["result_blob"] = reference(store.put_bytes("owner", out.getvalue()))
    result = _assemble([task], task["source_hash"], "source.pdf", str(store.files_dir), "owner", None)
    assert text_complete(result) == complete
    assert "Source reference" in result["pages"][0]["text"]
    if complete:
        assert "5%" in result["pages"][0]["text"]
    else:
        assert result["pages"][0]["reasons"] == ["chart_content_missing"]
    artifacts = result["remote_parse"]["parts"][0]["artifacts"]
    assert (
        resolve_reference(artifacts["images/chart.jpg"], store.files_dir, "owner").read_bytes()
        == b"preserved raw image"
    )
    if complete:
        assert result["mineru_chart_assets"] == []
    else:
        chart_assets = assets(result)
        assert len(chart_assets) == 1
        assert chart_assets[0]["source_format"] == "mineru_chart"
        assert chart_assets[0]["locator"]["page"] == 1
        assert chart_assets[0]["image_hash"] == artifacts["images/chart.jpg"]["content_hash"]


def test_processing_runs_source_bound_mineru_chart_ocr_before_indexing(workspace, monkeypatch):
    from support.native_asset_ocr import raster

    service = workspace
    source = service.archive.blobs.put_bytes("owner", b"verified-chart-pdf")
    chart_blob = service.archive.blobs.put_bytes("owner", raster())
    result_hash, page_text = "a" * 64, "Article 1. Chart follows."
    element_key = "mineru-000001-000002"
    locator = {
        "kind": "pdf",
        "page": 1,
        "physical_page": 1,
        "bbox": [100, 200, 800, 700],
        "coordinate_system": "mineru-0-1000",
        "source_hash": source.digest,
        "provider_task_id": "task-1",
        "provider_page_idx": 0,
    }
    manifest = {
        "parser": "mineru",
        "parser_version": "api-v4",
        "pipeline_version": "mineru-precise-v1",
        "source_hash": source.digest,
        "quality": {
            "status": "needs_review",
            "complete": False,
            "review_required": True,
            "review_pages": [1],
        },
        "pages": [
            {
                "page": 1,
                "status": "ocr_uncertain",
                "source_hash": source.digest,
                "text": page_text,
                "reasons": ["chart_content_missing"],
                "ocr": {
                    "provider": "mineru",
                    "task_id": "task-1",
                    "batch_id": "batch-1",
                    "provider_page_idx": 0,
                    "part_first_page": 1,
                    "result_hash": result_hash,
                    "text_sha256": hashlib.sha256(page_text.encode()).hexdigest(),
                    "complete": False,
                    "uncertain_spans": ["chart_content_missing"],
                },
            }
        ],
        "elements": [],
        "chunks": [],
        "remote_parse": {
            "provider": "mineru",
            "state": "done",
            "source_hash": source.digest,
            "page_count": 1,
            "parts": [
                {
                    "task_id": "task-1",
                    "batch_id": "batch-1",
                    "source_hash": source.digest,
                    "first_page": 1,
                    "last_page": 1,
                    "result_blob": {"content_hash": result_hash},
                }
            ],
        },
        "mineru_chart_assets": [
            {
                "asset_key": "mineru-chart-" + hashlib.sha256(element_key.encode()).hexdigest()[:24],
                "element_key": element_key,
                "block_type": "chart",
                "locator": locator,
                "source_hash": source.digest,
                "image_hash": chart_blob.digest,
                "image_blob": reference(chart_blob),
                "size": chart_blob.size,
            }
        ],
    }
    with service.db.transaction() as conn:
        seed(conn, "chart-target", body())
        conn.execute(
            "UPDATE documents SET filename='chart.pdf',mime_type='application/pdf',storage_path=?,content_hash=?,size=?,manifest=? WHERE id='chart-target'",
            (str(source.path), source.digest, source.size, json.dumps(manifest)),
        )
        service.collection.queue_processing(conn, "chart-target", "owner")
    monkeypatch.setattr(service.collection.parser, "step", lambda *_args: deepcopy(manifest))
    monkeypatch.setattr(service, "embedding_fingerprint", lambda: None)
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.vision.vision_ocr_configured", lambda: True
    )
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.vision.transcribe_chart_image",
        lambda _rendered: {
            "text": "Chart title: Export value\n| Year | Value |\n| 2024 | 17 |",
            "complete": True,
            "is_blank": False,
            "uncertain_spans": [],
            "model": "verified-chart-model",
            "requested_model": "test-vision-model",
            "prompt_version": "mineru-chart-transcription-v1",
            "review_status": "unreviewed",
            "physical_pages_known": False,
            "finish_reason": "stop",
        },
    )
    claim = service.collection.claim_processing(owner_id="owner", document_ids=["chart-target"])
    service.collection.process_item(claim)
    with closing(service.db.connect()) as conn:
        saved = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id='chart-target'").fetchone()[0]
        )
        job = dict(conn.execute("SELECT * FROM processing_jobs WHERE document_id='chart-target'").fetchone())
    assert text_complete(saved)
    assert saved["mineru_chart_assets"][0]["ocr"]["prompt_version"] == "mineru-chart-transcription-v1"
    assert job["status"] == "completed"


def test_ragflow_retrieval_isolates_two_cases_of_the_same_account(workspace, monkeypatch):
    remote, document, claim = prepared(workspace, monkeypatch)
    workspace.ragflow.step(document, claim)
    workspace.ragflow.step(document, claim)
    template = workspace.templates("owner")[0]
    values = {
        "template_id": template["id"],
        "mode_id": next(m["id"] for m in template["modes"] if m["enabled"]),
        "title": "case",
    }
    first, second = workspace.create_case("owner", values), workspace.create_case("owner", values)
    with workspace.db.transaction() as conn:
        conn.execute("UPDATE documents SET case_id=? WHERE id='target'", (first["id"],))
        conn.execute("UPDATE processing_jobs SET status='completed',lease_token=NULL,lease_until=NULL")
    seen = []

    def retrieve(query, candidates, *, limit):
        seen.extend(row["document_id"] for row in candidates)
        return [{"id": next(iter(remote.chunks)), "document_id": "document-target"}]

    monkeypatch.setattr(workspace.ragflow, "retrieve", retrieve)
    assert workspace.search("owner", "tea", case_id=second["id"])["items"] == []
    assert seen == []
    assert [h["document_id"] for h in workspace.search("owner", "tea", case_id=first["id"])["items"]] == [
        "target"
    ]
