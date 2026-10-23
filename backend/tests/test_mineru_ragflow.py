"""Source-bound cloud parsing, resumable indexing and scoped RAGFlow retrieval."""

from contextlib import closing
from datetime import date
import hashlib
import io
import json
from threading import Lock
import zipfile

import pytest

from app.db import now
from app.ingestion_quality import text_complete
from app.mineru import Admission, MinerUWait, manifest_valid
from app.mineru_results import _assemble, _prepare, reference, resolve_reference
from app.ragflow import RAGFlowError
from app.service import Service
from test_retrieval_index import body, seed


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

    def call(self, method, path, *, payload=None, params=None, files=None):
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
                assert files["file"][1].read()
                return [{"id": "document-target"}]
            if path.endswith("/chunks"):
                if method == "GET":
                    return {"chunks": list(self.chunks.values())}
                content = payload["content"]
                identifier = hashlib.sha256(content.encode()).hexdigest()[:16]
                self.chunks[identifier] = {"id": identifier, "content": content}
                if self.lose_response:
                    self.lose_response = False
                    raise RAGFlowError("ReadTimeout", unknown=True)
                return {"chunk": self.chunks[identifier]}
            raise AssertionError((method, path))


def prepared(service, monkeypatch, *, count=1):
    remote = Remote()
    monkeypatch.setattr("app.ragflow.RAGFlowClient", lambda: remote)
    original = service.archive.blobs.put_bytes("owner", b"lawful tea clause")
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


def test_expired_unconfirmed_embedding_is_held_for_review(workspace, monkeypatch):
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
            == "failed"
        )
    with workspace.db.transaction() as conn:
        with pytest.raises(Exception) as rejected:
            workspace.collection.queue_processing(conn, document["id"], "owner")
    assert getattr(rejected.value, "code", None) == "embedding_outcome_review_required"
    assert len([call for call in remote.calls if call[0] == "POST" and call[1].endswith("/chunks")]) == 1


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
    from pypdf import PdfWriter, PdfReader
    from pypdf._page import PageObject
    from app.blob_store import BlobStore

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


def result_archive(tmp_path, *, unsafe=False):
    from app.blob_store import BlobStore

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
        if unsafe:
            archive.writestr("../escape.txt", "invalid")
    raw = store.put_bytes("owner", out.getvalue())
    original = store.put_bytes("owner", b"original source")
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


def test_mineru_retains_artifacts_and_requires_page_source_proofs(tmp_path):
    store, task = result_archive(tmp_path)
    manifest = _assemble([task], task["source_hash"], "source.pdf", str(store.files_dir), "owner", None)
    assert text_complete(manifest) and manifest_valid(manifest)
    artifacts = manifest["remote_parse"]["parts"][0]["artifacts"]
    assert {"layout.json", "task_content_list.json", "full.md"} <= artifacts.keys()
    assert all(resolve_reference(ref, store.files_dir, "owner").is_file() for ref in artifacts.values())
    manifest["pages"][0]["text"] = "tampered"
    assert not text_complete(manifest)


def test_untrusted_mineru_zip_cannot_escape_storage(tmp_path):
    store, task = result_archive(tmp_path, unsafe=True)
    with pytest.raises(ValueError, match="不安全"):
        _assemble([task], task["source_hash"], "source.pdf", str(store.files_dir), "owner", None)
    assert not (tmp_path / "escape.txt").exists()


def test_mineru_limit_and_retry_after_are_shared(tmp_path, monkeypatch):
    from app import mineru

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
    from app.mineru import MinerUClient
    from app.blob_store import BlobStore

    monkeypatch.setenv("MINERU_API_TOKEN", "test-mineru-token")
    store = BlobStore(tmp_path / "files")

    def download(url, *, deadline, consume, max_bytes):
        assert url == "https://results.example/result.zip"
        assert 100 < deadline - time.monotonic() <= 120
        assert max_bytes == 128 * 1024 * 1024
        return {"blob": consume(iter([b"bounded provider result"]))}

    monkeypatch.setattr("app.ingestion._download", download)
    result = MinerUClient(tmp_path).download("https://results.example/result.zip", store, "owner")
    assert resolve_reference(result, store.files_dir, "owner").read_bytes() == b"bounded provider result"


def test_mineru_merged_tables_keep_original_text_on_each_physical_page(tmp_path):
    from app.blob_store import BlobStore

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
    from app import mineru

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
    from app.mineru import MinerUError

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
    from app.mineru_results import _TableText

    table = _TableText()
    table.feed(
        '<table><tr><th colspan="2">Rates</th></tr><tr><td rowspan="2">Tea</td><td>5%</td></tr><tr><td>0%</td></tr></table>'
    )
    assert table.rows == [["Rates", "Rates"], ["Tea", "5%"], ["Tea", "0%"]]


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
    "content,complete", [("", False), ("<table><tr><td>Tea</td><td>5%</td></tr></table>", True)]
)
def test_chart_keeps_asset_and_requires_readable_content(tmp_path, content, complete):
    store, task = result_archive(tmp_path)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("layout.json", json.dumps({"pdf_info": [{"page_idx": 0}]}))
        archive.writestr(
            "task_content_list.json",
            json.dumps(
                [
                    {"type": "text", "text": "lawful tea clause", "page_idx": 0},
                    {"type": "chart", "content": content, "page_idx": 0, "img_path": "images/chart.jpg"},
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
