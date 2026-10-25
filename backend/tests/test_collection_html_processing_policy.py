"""Scoped HTML workers authorize the current source before every model phase."""

import json
import time
from contextlib import closing
from copy import deepcopy
from threading import Event

import pytest
from app.contexts.ingestion.application.collection_jobs import CollectionWorkers
from app.contexts.ingestion.application.html_image_collection import HtmlImageCollector
from app.contexts.documents.infrastructure.providers import embedding as ingestion_embedding
from app.contexts.documents.infrastructure.providers import vision as vision
from app.shared.domain.values import dump, now
from test_html_image_collection import document, downloader
from test_html_image_collection import service as service
from test_html_linked_asset_ocr import transcript

POLICY = "draft_html_v3"


def row(service, table, identifier):
    column = "id" if table == "documents" else "document_id"
    with closing(service.db.connect()) as conn:
        found = conn.execute(f"SELECT * FROM {table} WHERE {column}=?", (identifier,)).fetchone()
        return dict(found) if found else None


def change(service, identifier, reason):
    with service.db.transaction() as conn:
        stored = conn.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone()
        if reason == "reviewed":
            payload = json.loads(
                conn.execute(
                    "SELECT payload FROM document_metadata WHERE document_id=?", (identifier,)
                ).fetchone()[0]
            )
            payload["review_status"] = "published"
            conn.execute(
                "UPDATE document_metadata SET payload=? WHERE document_id=?", (dump(payload), identifier)
            )
        elif reason == "attachment":
            conn.execute("UPDATE documents SET role='secondary' WHERE id=?", (identifier,))
        elif reason == "owner":
            conn.execute("UPDATE documents SET owner_id='other' WHERE id=?", (identifier,))
        elif reason == "missing_source_url":
            conn.execute("UPDATE documents SET source_url=NULL WHERE id=?", (identifier,))
        elif reason == "invalid_manifest":
            conn.execute("UPDATE documents SET manifest='invalid-json' WHERE id=?", (identifier,))
        else:
            manifest = json.loads(stored["manifest"])
            if reason == "parser":
                manifest["parser"] = "pdf-basic"
            elif reason == "version":
                manifest["parser_version"] = "2"
            elif reason == "pipeline_case":
                manifest["pipeline_version"] = "HTML-source-mapping-v3+layout-token-hierarchical-v2"
            elif reason == "pipeline":
                manifest["pipeline_version"] = "html-source-mapping-v30+layout-token-hierarchical-v2"
            elif reason == "mapping":
                manifest["html_source"]["mapping_version"] = "html-source-mapping-v2"
            elif reason == "source_hash":
                manifest["source_hash"] = "0" * 64
            elif reason == "mapping_source_hash":
                manifest["html_source"]["source_hash"] = "0" * 64
            else:
                raise AssertionError(reason)
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), identifier))


def queue(service, identifier, owner="owner", *, expired=False):
    with service.db.transaction() as conn:
        conn.execute(
            """INSERT INTO processing_jobs(document_id,owner_id,phase,status,updated_at)
            VALUES(?,?,'parse','queued',?) ON CONFLICT(document_id) DO UPDATE SET status='queued'""",
            (identifier, owner, now()),
        )
        if expired:
            conn.execute(
                """UPDATE processing_jobs SET status='processing',attempts=3,
                lease_token='unrelated-expired-lease',lease_until=1 WHERE document_id=?""",
                (identifier,),
            )


@pytest.mark.parametrize(
    "reason",
    [
        "reviewed",
        "attachment",
        "parser",
        "version",
        "pipeline",
        "pipeline_case",
        "missing_source_url",
        "mapping",
        "source_hash",
        "mapping_source_hash",
        "invalid_manifest",
    ],
)
def test_ineligible_queued_document_cannot_be_claimed_or_expired(service, reason):
    doc = document(service, name=reason)
    change(service, doc["id"], reason)
    queue(service, doc["id"])
    before = row(service, "processing_jobs", doc["id"])
    assert (
        service.collection.claim_processing(
            owner_id="owner", document_ids=[doc["id"]], processing_policy=POLICY
        )
        is None
    )
    assert row(service, "processing_jobs", doc["id"]) == before
    queue(service, doc["id"], expired=True)
    before = row(service, "processing_jobs", doc["id"])
    assert (
        service.collection.claim_processing(
            owner_id="owner", document_ids=[doc["id"]], processing_policy=POLICY
        )
        is None
    )
    assert row(service, "processing_jobs", doc["id"]) == before


def test_foreign_expired_rows_and_wrong_document_owner_remain_untouched(service):
    allowed = document(service, name="allowed")
    foreign = document(service, owner="other", name="foreign")
    changed = document(service, name="changed-owner")
    change(service, changed["id"], "owner")
    queue(service, foreign["id"], "other", expired=True)
    queue(service, changed["id"], expired=True)
    queue(service, allowed["id"])
    before = {d["id"]: row(service, "processing_jobs", d["id"]) for d in (foreign, changed)}
    claim = service.collection.claim_processing(
        owner_id="owner", document_ids=[allowed["id"], foreign["id"], changed["id"]], processing_policy=POLICY
    )
    assert claim["document_id"] == allowed["id"]
    assert claim["_processing_policy"] == POLICY
    assert all(row(service, "processing_jobs", identifier) == value for identifier, value in before.items())


def bound(service, monkeypatch):
    doc = document(service, level="secondary")
    requests = downloader(service, monkeypatch)
    prepared = HtmlImageCollector(service).prepare("owner", doc["id"], deadline=time.monotonic() + 30)
    assert prepared["bound_images"] and requests
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    return doc


@pytest.mark.parametrize(
    "reason",
    [
        "reviewed",
        "attachment",
        "parser",
        "version",
        "pipeline",
        "pipeline_case",
        "missing_source_url",
        "mapping",
        "source_hash",
        "mapping_source_hash",
        "owner",
    ],
)
def test_authorization_changes_after_claim_cannot_call_model_or_change_document(service, monkeypatch, reason):
    doc = bound(service, monkeypatch)
    claim = service.collection.claim_processing(
        owner_id="owner", document_ids=[doc["id"]], processing_policy=POLICY
    )
    assert claim is not None
    change(service, doc["id"], reason)
    before = row(service, "documents", doc["id"])
    before_job = row(service, "processing_jobs", doc["id"])
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: pytest.fail("No current HTML draft authority"))
    monkeypatch.setattr(
        ingestion_embedding, "embed_texts", lambda *_: pytest.fail("No current HTML draft authority")
    )
    service.collection.process_item(claim)
    assert row(service, "documents", doc["id"]) == before
    after_job = row(service, "processing_jobs", doc["id"])
    if reason == "owner":
        assert after_job == before_job
    else:
        assert after_job["status"] == "queued" and after_job["lease_token"] is None
        assert after_job["attempts"] == before_job["attempts"] - 1


def test_review_change_during_image_read_is_checked_before_provider(service, monkeypatch):
    from app.contexts.documents.infrastructure.providers import html_linked_asset_ocr as html_linked_asset_ocr

    doc = bound(service, monkeypatch)
    claim = service.collection.claim_processing(
        owner_id="owner", document_ids=[doc["id"]], processing_policy=POLICY
    )
    original = html_linked_asset_ocr.read_linked_images
    observed = []

    def read(*args, **kwargs):
        assert kwargs["pending_only"] is True and kwargs["max_assets"] == 4
        result = original(*args, **kwargs)
        change(service, doc["id"], "reviewed")
        observed.append(row(service, "documents", doc["id"]))
        return result

    monkeypatch.setattr(html_linked_asset_ocr, "read_linked_images", read)
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: pytest.fail("Review changed before provider"))
    service.collection.process_item(claim)
    assert observed and row(service, "documents", doc["id"]) == observed[0]
    assert row(service, "processing_jobs", doc["id"])["status"] == "queued"


def test_legal_queued_html_draft_resumes_and_secondary_legal_text_gets_no_vectors(service, monkeypatch):
    doc = bound(service, monkeypatch)
    calls = []
    monkeypatch.setattr(vision, "transcribe_image", lambda image: (calls.append(image), transcript())[1])
    monkeypatch.setattr(
        ingestion_embedding, "embed_texts", lambda *_: pytest.fail("Secondary legal text cannot embed")
    )
    claim = service.collection.claim_processing(
        owner_id="owner", document_ids=[doc["id"]], processing_policy=POLICY
    )
    service.collection.process_item(claim)
    assert len(calls) == 1
    assert row(service, "processing_jobs", doc["id"])["status"] == "completed"
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 0
    queue(service, doc["id"])
    service.collection.process_item(
        service.collection.claim_processing(
            owner_id="owner", document_ids=[doc["id"]], processing_policy=POLICY
        )
    )
    assert len(calls) == 1


def test_html_ocr_checkpoints_use_the_reparsed_mapping_version(service, monkeypatch):
    doc = bound(service, monkeypatch)
    with service.db.transaction() as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
        manifest["parser_version"] = "6"
        manifest["html_source"]["mapping_version"] = "html-source-mapping-v6"
        manifest["pipeline_version"] = "html-source-mapping-v6+layout-token-hierarchical-v2"
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
    monkeypatch.setattr(vision, "transcribe_image", lambda *_: transcript())
    monkeypatch.setattr(
        ingestion_embedding, "embed_texts", lambda *_: pytest.fail("Legal draft cannot embed")
    )
    claim = service.collection.claim_processing(
        owner_id="owner", document_ids=[doc["id"]], processing_policy=POLICY
    )

    service.collection.process_item(claim)

    updated = json.loads(row(service, "documents", doc["id"])["manifest"])
    assert updated["parser_version"] == "12"
    assert updated["html_source"]["mapping_version"] == "html-source-mapping-v12"
    assert updated["html_source"]["linked_images"][0]["asset_status"] == "ocr_complete"
    assert row(service, "processing_jobs", doc["id"])["status"] == "completed"


def test_html_image_ocr_resumes_bounded_batches_until_all_images_are_processed(service, monkeypatch):
    from app.contexts.documents.infrastructure.providers.native_asset_ocr import assets
    from test_html_image_collection import image

    html = (
        "<main><p>Five linked legal exhibits.</p>"
        + "".join(f'<img src="/body-{index}.png">' for index in range(5))
        + "</main>"
    )
    doc = document(service, html=html)
    downloader(service, monkeypatch, raw=image())
    prepared = HtmlImageCollector(service).prepare(
        "owner", doc["id"], deadline=time.monotonic() + 60, max_images=20
    )
    assert len(prepared["bound_images"]) == 5
    monkeypatch.setattr(vision, "vision_ocr_configured", lambda: True)
    calls = []
    monkeypatch.setattr(vision, "transcribe_image", lambda image: (calls.append(image), transcript())[1])

    for expected_batch in (4, 1):
        with service.db.transaction() as conn:
            conn.execute("UPDATE processing_jobs SET available_at=0 WHERE document_id=?", (doc["id"],))
        claim = service.collection.claim_processing(
            owner_id="owner", document_ids=[doc["id"]], processing_policy=POLICY
        )
        assert claim and claim["phase"] == "parse"
        before = len(calls)
        service.collection.process_item(claim)
        assert len(calls) - before == expected_batch

    with closing(service.db.connect()) as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
    assert all(asset["asset_status"] == "ocr_complete" for asset in assets(manifest))
    assert row(service, "processing_jobs", doc["id"])["status"] == "completed"


def test_scoped_worker_without_downloads_never_claims_fetch_and_freezes_declared_ids(service, monkeypatch):
    doc = document(service, html="<main><p>Complete native regulatory text.</p></main>")
    queue(service, doc["id"])
    ids = [doc["id"]]
    completed = Event()
    fetched = []
    processed = []
    original = service.collection.process_item

    def process(claim):
        processed.append(deepcopy(claim))
        original(claim)
        completed.set()

    monkeypatch.setattr(service.collection, "claim_fetch", lambda: fetched.append(True))
    monkeypatch.setattr(service.collection, "process_item", process)
    workers = CollectionWorkers(
        service.collection, processing_scope=("owner", ids), processing_policy=POLICY, fetch_enabled=False
    )
    ids[:] = ["not-authorized"]
    workers.start()
    try:
        assert completed.wait(5)
    finally:
        workers.stop(timeout=5)
    assert workers.thread is not None and not workers.thread.is_alive()
    assert not fetched and len(processed) == 1
    assert processed[0]["document_id"] == doc["id"] and processed[0]["_processing_policy"] == POLICY


def test_default_worker_keeps_existing_non_html_queue_behavior(service):
    doc = document(service)
    change(service, doc["id"], "parser")
    queue(service, doc["id"])
    claim = service.collection.claim_processing()
    assert claim["document_id"] == doc["id"] and "_processing_policy" not in claim


@pytest.mark.parametrize("policy,scope", [(POLICY, None), ("unknown", ("owner", ["doc"]))])
def test_invalid_policy_or_missing_scope_is_rejected_before_threads(service, policy, scope):
    with pytest.raises(ValueError):
        CollectionWorkers(
            service.collection, processing_scope=scope, processing_policy=policy, fetch_enabled=False
        )


def test_review_change_while_persisting_sent_pixels_is_checked_before_model(service, monkeypatch):
    doc = bound(service, monkeypatch)
    claim = service.collection.claim_processing(
        owner_id="owner", document_ids=[doc["id"]], processing_policy=POLICY
    )
    persist = service.collection._persist_ocr_image
    original = row(service, "documents", doc["id"])

    def store(*args, **kwargs):
        reference = persist(*args, **kwargs)
        change(service, doc["id"], "reviewed")
        return reference

    monkeypatch.setattr(service.collection, "_persist_ocr_image", store)
    monkeypatch.setattr(
        vision, "transcribe_image", lambda *_: pytest.fail("Reviewed pixels cannot start a model request")
    )
    service.collection.process_item(claim)
    assert row(service, "documents", doc["id"]) == original
    assert row(service, "processing_jobs", doc["id"])["status"] == "queued"


def test_review_change_when_selecting_embedding_batch_is_checked_before_model(service, monkeypatch):
    from app.contexts.documents.infrastructure.providers import ragflow as ragflow

    doc = document(service, html="<main><p>Complete primary native legal text.</p></main>")
    with service.db.transaction() as conn:
        service.index.sync_document(conn, doc["id"])
        conn.execute(
            "UPDATE processing_jobs SET status='queued',phase='embed' WHERE document_id=?", (doc["id"],)
        )
    monkeypatch.setattr(service, "embedding_fingerprint", lambda: "test-only-model-selector")
    monkeypatch.setattr(ragflow, "profile", lambda: "test-only-model-selector")
    process = service.ragflow.step

    def select(document, claim):
        with service.db.transaction() as conn:
            payload = json.loads(
                conn.execute(
                    "SELECT payload FROM document_metadata WHERE document_id=?", (doc["id"],)
                ).fetchone()[0]
            )
            payload["review_status"] = "published"
            conn.execute(
                "UPDATE document_metadata SET payload=? WHERE document_id=?", (dump(payload), doc["id"])
            )
        return process(document, claim)

    monkeypatch.setattr(service.ragflow, "step", select)
    monkeypatch.setattr(
        ragflow.RAGFlowClient,
        "call",
        lambda *_args, **_kwargs: pytest.fail("Review changed before RAGFlow request"),
    )
    claim = service.collection.claim_processing(
        owner_id="owner", document_ids=[doc["id"]], processing_policy=POLICY
    )
    service.collection.process_item(claim)
    assert row(service, "processing_jobs", doc["id"])["status"] == "queued"
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 0
