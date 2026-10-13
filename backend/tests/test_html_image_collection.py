"""Protected image preparation, cache recovery and scoped processing fairness."""

from contextlib import closing
from copy import deepcopy
import hashlib
import io
import json
import time

from PIL import Image
import pytest

from app import ingestion
from app.db import dump, now
from app.html_image_collection import HtmlImageCollector
from app.html_linked_asset_ocr import read_linked_images
from app.ingestion_quality import text_complete
from app.service import Service


@pytest.fixture
def service(tmp_path, monkeypatch):
    for name in list(ingestion.os.environ):
        if name.startswith(("EMBEDDING_", "LLM_", "VISION_OCR_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("TEA_PROCESS_WORKERS", "2")
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        for owner in ("owner", "other"):
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)", (owner, owner, None, None, 0, now()))
    return service


def document(service, *, owner="owner", level="primary", name="law", html=None):
    content = (
        html or '<main><nav><img src="logo.png"></nav><p>Keep records.</p><img src="/body.png"></main>'
    ).encode()
    with service.db.transaction() as conn:
        blob = service.archive.blobs.put_bytes(owner, content)
        doc = dict(
            service.archive.store_original(
                conn,
                owner,
                blob,
                name + ".html",
                "text/html",
                "https://example.org/" + name,
                collection="country:SG",
                level=level,
                tags=[],
            )
        )
        conn.execute(
            "UPDATE documents SET manifest=?,status='ready' WHERE id=?",
            (dump(ingestion.parse_file(name + ".html", content)), doc["id"]),
        )
        conn.execute("UPDATE processing_jobs SET status='completed' WHERE document_id=?", (doc["id"],))
    return doc


def image():
    result = io.BytesIO()
    Image.new("RGB", (60, 40), "white").save(result, format="PNG")
    return result.getvalue()


def downloader(service, monkeypatch, *, during=None, mime="image/png", raw=None):
    requests = []

    def download(url, *, deadline, connection_slot, consume, max_bytes, **_options):
        requests.append(url)
        assert max_bytes == 8 * 1024 * 1024 and deadline > time.monotonic()
        with connection_slot("example.org"):
            with closing(service.db.connect()) as conn:
                assert conn.execute("SELECT count(*) FROM collection_network_leases").fetchone()[0] >= 1
            blob = consume([raw if raw is not None else image()])
            if during:
                during()
            return {"url": url, "blob": blob, "mime_type": mime, "response_status": 200}

    monkeypatch.setattr(ingestion, "_download", download)
    return requests


def test_body_images_use_protected_leases_then_existing_worker_and_resume_skips_transfer(
    service, monkeypatch
):
    doc = document(service, level="secondary")
    requests = downloader(service, monkeypatch)
    saved = []
    collector = HtmlImageCollector(service, checkpoint=lambda state: saved.append(deepcopy(state)))
    result = collector.prepare("owner", doc["id"], deadline=time.monotonic() + 60)
    assert requests == ["https://example.org/body.png"] and result["model_calls"] == 0
    with closing(service.db.connect()) as conn:
        row = conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()
        manifest = json.loads(row["manifest"])
        assert row["version"] == doc["version"] and row["content_hash"] == doc["content_hash"]
        assert not text_complete(manifest)
        assert (
            conn.execute(
                "SELECT status,phase FROM processing_jobs WHERE document_id=?", (doc["id"],)
            ).fetchone()[0]
            == "queued"
        )
        assert conn.execute("SELECT count(*) FROM collection_network_leases").fetchone()[0] == 0
    assert len(read_linked_images(service.db.data_dir, "owner", manifest)) == 1
    assert any(any(r["status"] == "downloaded" for r in state["images"].values()) for state in saved)
    assert not saved[-1]["images"]  # committed receipt remains immutable in parent CAS
    receipt = manifest["html_source"]["linked_images"][0]["download_receipt_blob"]
    raw_receipt = (service.db.data_dir / receipt["storage_path"]).read_bytes()
    assert hashlib.sha256(raw_receipt).hexdigest() == receipt["content_hash"]
    assert json.loads(raw_receipt)["http"]["response_status"] == 200
    assert not requests[0].endswith("logo.png")
    service.collection.process_item(
        service.collection.claim_processing(owner_id="owner", document_ids=[doc["id"]])
    )
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0] == 0
    before = len(requests)
    resumed = HtmlImageCollector(service, state=collector.state).prepare(
        "owner", doc["id"], deadline=time.monotonic() + 60
    )
    assert (
        len(requests) == before and resumed["already_bound_images"] == 1 and not resumed["queue_processing"]
    )


def test_download_checkpoint_survives_stale_manifest_and_reuses_cas_without_new_http(service, monkeypatch):
    doc = document(service)
    with closing(service.db.connect()) as conn:
        original = conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]

    def edit():
        with service.db.transaction() as conn:
            current = json.loads(original)
            current["warnings"].append("Concurrent edit.")
            conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(current), doc["id"]))

    requests = downloader(service, monkeypatch, during=edit)
    collector = HtmlImageCollector(service)
    with pytest.raises(ValueError, match="已变化"):
        collector.prepare("owner", doc["id"], deadline=time.monotonic() + 60)
    with service.db.transaction() as conn:
        assert (
            "Concurrent edit."
            in conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (original, doc["id"]))
    result = HtmlImageCollector(service, state=collector.state).prepare(
        "owner", doc["id"], deadline=time.monotonic() + 60
    )
    assert len(requests) == 1 and result["bound_images"][0]["reused_transfer"]


@pytest.mark.parametrize("reason", ["other_owner", "attachment", "active", "base", "private", "responsive"])
def test_unsupported_or_unauthorized_sources_never_download(service, monkeypatch, reason):
    html = {
        "base": '<head><base href="https://elsewhere.org/"></head><main><p>x</p><img src="body.png"></main>',
        "private": '<main><p>x</p><img src="https://127.0.0.1/private.png"></main>',
        "responsive": '<main><p>x</p><img src="body.png" srcset="large.png 2x"></main>',
    }.get(reason)
    doc = document(
        service,
        owner="other" if reason == "other_owner" else "owner",
        level="attachment" if reason == "attachment" else "primary",
        html=html,
    )
    if reason == "active":
        with service.db.transaction() as conn:
            conn.execute("UPDATE processing_jobs SET status='queued' WHERE document_id=?", (doc["id"],))
    monkeypatch.setattr(
        ingestion, "_download", lambda *_a, **_kw: pytest.fail("Unsupported source was fetched")
    )
    try:
        result = HtmlImageCollector(service).prepare("owner", doc["id"], deadline=time.monotonic() + 60)
        assert result["errors"] or result["unresolved_objects"]
    except (ValueError, RuntimeError) as error:
        assert reason not in {"private", "responsive"} or isinstance(error, ValueError)
    except Exception as error:
        from app.service import AppError

        assert reason == "other_owner" and isinstance(error, AppError)


def test_http_error_body_is_saved_as_source_evidence_and_never_bound(service, monkeypatch):
    doc = document(service)
    downloader(service, monkeypatch, mime="text/html")
    collector = HtmlImageCollector(service)
    result = collector.prepare("owner", doc["id"], deadline=time.monotonic() + 60)
    assert result["errors"] and not result["bound_images"]
    receipt = next(iter(collector.state["images"].values()))
    assert (
        receipt["status"] == "failed"
        and receipt["image_blob"]["content_hash"] == hashlib.sha256(image()).hexdigest()
    )


@pytest.mark.parametrize("active_phase,preferred", [("parse", "embed"), ("embed", "parse")])
def test_long_processing_phase_leaves_a_slot_for_the_other_phase(service, active_phase, preferred):
    active = document(service, name="active")
    old = document(service, name="old")
    ready = document(service, name="ready")
    with service.db.transaction() as conn:
        conn.execute(
            "UPDATE processing_jobs SET status='processing',phase=?,lease_token='held',lease_until=? WHERE document_id=?",
            (active_phase, time.time() + 120, active["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET status='queued',phase=?,updated_at='2000' WHERE document_id=?",
            (active_phase, old["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET status='queued',phase=?,updated_at='2026' WHERE document_id=?",
            (preferred, ready["id"]),
        )
    claimed = service.collection.claim_processing()
    assert claimed["document_id"] == ready["id"] and claimed["phase"] == preferred
    assert service.collection.claim_processing() is None


def test_scoped_worker_does_not_reclaim_or_modify_foreign_expired_tasks(service):
    owned = document(service, name="own")
    foreign = document(service, owner="other", name="foreign")
    with service.db.transaction() as conn:
        conn.execute("UPDATE processing_jobs SET status='queued' WHERE document_id=?", (owned["id"],))
        conn.execute(
            "UPDATE processing_jobs SET status='processing',attempts=3,lease_token='old',lease_until=1 WHERE document_id=?",
            (foreign["id"],),
        )
        before = tuple(
            conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (foreign["id"],)).fetchone()
        )
    claim = service.collection.claim_processing(owner_id="owner", document_ids=[owned["id"], foreign["id"]])
    assert claim["document_id"] == owned["id"]
    with closing(service.db.connect()) as conn:
        assert (
            tuple(
                conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (foreign["id"],)).fetchone()
            )
            == before
        )


def test_download_budget_is_enforced_across_a_multi_image_page(service, monkeypatch):
    doc = document(service, html='<main><p>x</p><img src="a.png"><img src="b.png"><img src="c.png"></main>')
    requests = downloader(service, monkeypatch)
    collector = HtmlImageCollector(service, max_requests=1)
    result = collector.prepare("owner", doc["id"], deadline=time.monotonic() + 60, max_images=3)
    assert len(requests) == 1 and len(result["bound_images"]) == 1 and len(result["errors"]) == 2


def test_checkpoint_write_failure_preserves_downloaded_status_and_never_repeats_http(service, monkeypatch):
    doc = document(service)
    requests = downloader(service, monkeypatch)

    def checkpoint(state):
        if any(r["status"] == "downloaded" for r in state["images"].values()):
            raise OSError("disk full")

    collector = HtmlImageCollector(service, checkpoint=checkpoint)
    result = collector.prepare("owner", doc["id"], deadline=time.monotonic() + 60)
    assert result["errors"] and len(requests) == 1
    assert next(iter(collector.state["images"].values()))["status"] == "downloaded"
    result = HtmlImageCollector(service, state=collector.state).prepare(
        "owner",
        doc["id"],
        deadline=time.monotonic() + 60,
    )
    assert len(requests) == 1 and result["bound_images"][0]["reused_transfer"]


def test_restart_from_disk_intent_recovers_separate_success_receipt_without_http(
    service, monkeypatch, tmp_path
):
    doc = document(service)
    requests = downloader(service, monkeypatch)
    path = tmp_path / "images.json"

    def checkpoint(state):
        if any(r["status"] == "downloaded" for r in state["images"].values()):
            raise OSError("mutable checkpoint write failed")
        path.write_text(dump(state))

    first = HtmlImageCollector(service, checkpoint=checkpoint)
    result = first.prepare("owner", doc["id"], deadline=time.monotonic() + 60)
    assert result["errors"] and len(requests) == 1
    disk = json.loads(path.read_bytes())
    assert next(iter(disk["images"].values()))["status"] == "request_intent"
    restarted_service = Service(service.db.data_dir)
    restarted = HtmlImageCollector(restarted_service, state=disk)
    result = restarted.prepare("owner", doc["id"], deadline=time.monotonic() + 60)
    assert len(requests) == 1 and result["bound_images"][0]["reused_transfer"]
    assert not restarted.state["images"]


def test_unconfirmed_interrupted_intent_never_repeats_network_in_a_new_process(
    service, monkeypatch, tmp_path
):
    doc = document(service)
    requests = downloader(service, monkeypatch)
    path = tmp_path / "images.json"

    def checkpoint(state):
        path.write_text(dump(state))
        raise OSError("interruption before transfer")

    first = HtmlImageCollector(service, checkpoint=checkpoint)
    result = first.prepare("owner", doc["id"], deadline=time.monotonic() + 60)
    assert not requests and result["errors"]
    disk = json.loads(path.read_bytes())
    for state in (disk, {"format": 1, "images": {}}):
        result = HtmlImageCollector(Service(service.db.data_dir), state=state).prepare(
            "owner",
            doc["id"],
            deadline=time.monotonic() + 60,
        )
        assert not requests and result["errors"][0]["error_type"] == "HtmlTransferUnconfirmed"


@pytest.mark.parametrize("valid_pixels", [True, False])
def test_binary_octet_stream_requires_full_raster_validation(service, monkeypatch, valid_pixels):
    doc = document(service)
    requests = downloader(
        service,
        monkeypatch,
        mime="binary/octet-stream",
        raw=image() if valid_pixels else b"%PDF invalid raster",
    )
    result = HtmlImageCollector(service).prepare("owner", doc["id"], deadline=time.monotonic() + 60)
    assert len(requests) == 1
    assert bool(result["bound_images"]) == valid_pixels
    assert bool(result["errors"]) != valid_pixels


def test_many_bound_images_over_32_mib_continue_in_separate_bounded_batches(service, monkeypatch):
    doc = document(
        service,
        html="<main><p>Five original images.</p>"
        + "".join(f'<img src="{i}.png">' for i in range(5))
        + "</main>",
    )
    raw = image() + b"\x00" * (7 * 1024 * 1024)
    requests = downloader(service, monkeypatch, raw=raw)
    collector = HtmlImageCollector(service)
    first = collector.prepare("owner", doc["id"], deadline=time.monotonic() + 120)
    assert len(first["bound_images"]) == 4 and first["remaining_eligible_images"] == 1
    with service.db.transaction() as conn:
        # This acquisition test cancels the queued OCR; it never claims those
        # images were recognized or eligible for retrieval.
        conn.execute("UPDATE processing_jobs SET status='cancelled' WHERE document_id=?", (doc["id"],))
    second = collector.prepare("owner", doc["id"], deadline=time.monotonic() + 120)
    assert len(requests) == 5 and len(second["bound_images"]) == 1 and not second["errors"]
    assert not collector.state["images"]
    with closing(service.db.connect()) as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (doc["id"],)).fetchone()[0]
        )
    assert sum(r["size"] for r in manifest["html_source"]["linked_images"]) > 32 * 1024 * 1024
    loaded = read_linked_images(service.db.data_dir, "owner", manifest, pending_only=True, max_assets=4)
    assert len(loaded) == 4 and sum(map(len, loaded.values())) <= 32 * 1024 * 1024
    for value in manifest["html_source"]["linked_images"][:4]:
        value["asset_status"] = "ocr_failed"
    loaded = read_linked_images(service.db.data_dir, "owner", manifest, pending_only=True, max_assets=4)
    assert len(loaded) == 1 and next(iter(loaded.values())) == raw
    assert not text_complete(manifest)
