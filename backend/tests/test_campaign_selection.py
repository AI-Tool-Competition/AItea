"""Material tiers, immutable associations, model work and private text exports."""

from contextlib import closing
from copy import deepcopy
import io
import json
import time
import textwrap

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.archive_inventory import export_inventory
from app.checklists import load_blueprints
from app.discovery import discover_page, recipe_for
from app.resource_campaign import ResourceCampaign, validate_extension
from app.service import Service

PLAN = "indonesia-20261003"
CATALOG = "https://rcepsec.org/rcep-agreements/"
PRIMARY = "https://rcepsec.org/wp-content/uploads/2026/06/Chapter-3.pdf"
SECONDARY = "https://rcepsec.org/wp-content/uploads/2026/06/Chapter-3-Annex-3B.pdf"


def selected_options():
    return {
        "items": ["5.3"],
        "confirm_items": [],
        "discovery_batches": 1,
        "originals_per_item": 2,
        "live_models": True,
        "selection": {
            "plan_id": PLAN,
            "source_document_sha256": load_blueprints()[PLAN]["source_document"]["sha256"],
            "materials": [
                {
                    "key": "origin",
                    "item_id": "5.3",
                    "url": PRIMARY,
                    "level": "primary",
                    "evidence_url": CATALOG,
                    "locator": "Chapter 3: Rules of Origin",
                },
                {
                    "key": "certificate",
                    "item_id": "5.3",
                    "url": SECONDARY,
                    "level": "secondary",
                    "primary_keys": ["origin"],
                    "evidence_url": CATALOG,
                    "locator": "Annex 3B: Minimum Information Requirements",
                },
            ],
        },
    }


def extended_options():
    value = selected_options()
    value["originals_per_item"] = 3
    value["selection"]["materials"].append(
        {
            "key": "product-specific-rules",
            "item_id": "5.3",
            "url": "https://rcepsec.org/wp-content/uploads/2026/06/Chapter-3-Annex-3A.pdf",
            "level": "primary",
            "evidence_url": CATALOG,
            "locator": "Annex 3A: Product-Specific Rules",
        }
    )
    return value


def test_additive_selection_extension_keeps_originals_and_request_history(tmp_path, monkeypatch):
    service, campaign = run_selected(tmp_path, monkeypatch)
    previous = deepcopy(campaign.state)
    with closing(service.db.connect()) as conn:
        originals = [
            tuple(row) for row in conn.execute("SELECT id,version,content_hash FROM documents ORDER BY id")
        ]
        owner_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    extended = ResourceCampaign(service, PLAN, extended_options(), state=deepcopy(previous), extend=True)
    assert extended.state["owner_id"] == previous["owner_id"]
    assert (
        extended.state["items"]["5.3"]["selected_candidates"]
        == previous["items"]["5.3"]["selected_candidates"]
    )
    assert extended.state["items"]["5.3"]["collection_runs"] == previous["items"]["5.3"]["collection_runs"]
    assert extended.state["extensions"][-1]["added_material_keys"] == ["product-specific-rules"]
    with closing(service.db.connect()) as conn:
        assert [
            tuple(row) for row in conn.execute("SELECT id,version,content_hash FROM documents ORDER BY id")
        ] == originals
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == owner_count


@pytest.mark.parametrize(
    "mutation", ["remove", "retier", "url", "anchor", "evidence", "source_hash", "budget"]
)
def test_extension_cannot_rewrite_existing_material_or_reduce_limits(mutation):
    after = extended_options()
    entries = after["selection"]["materials"]
    if mutation == "remove":
        entries.pop(1)
    elif mutation == "retier":
        entries[1].update(level="primary", primary_keys=[])
    elif mutation == "url":
        entries[0]["url"] = "https://rcepsec.org/wp-content/uploads/2026/06/Chapter-4.pdf"
    elif mutation == "anchor":
        entries[1]["primary_keys"] = ["product-specific-rules"]
    elif mutation == "evidence":
        entries[0]["locator"] = "Changed source evidence"
    elif mutation == "source_hash":
        after["selection"]["source_document_sha256"] = "f" * 64
    elif mutation == "budget":
        after["originals_per_item"] = 1
    with pytest.raises(ValueError):
        validate_extension(selected_options(), after)


def pdf(text):
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    lines = textwrap.wrap(text, width=65)
    for start in range(0, max(1, len(lines)), 45):
        page = writer.add_blank_page(600, 800)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        commands = ["BT /F1 12 Tf 20 700 Td"]
        for line in lines[start : start + 45]:
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append("(" + escaped + ") Tj 0 -14 Td")
        commands.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def run_selected(tmp_path, monkeypatch, *, shared=True, assert_pipeline=False, primary_pdf=None):
    from app import ingestion

    for key, value in {
        "EMBEDDING_BASE_URL": "https://provider.example/v1",
        "EMBEDDING_API_KEY": "fake-test",
        "EMBEDDING_MODEL": "test-model",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])
    primary = primary_pdf or pdf(
        "Article 3.1 Originating Goods. Products must meet the requirements of this agreement and its rules of origin."
    )
    secondary = pdf(
        "Annex 3B Certificate of Origin. The minimum information requirements identify the exporter, producer and goods."
    )
    catalog = f'<title>RCEP Agreement</title><a href="{PRIMARY}">Chapter 3</a>'
    if shared:
        catalog += f'<a href="{SECONDARY}">Annex 3B</a>'
    catalog += (
        '<a href="https://rcepsec.org/wp-content/uploads/2026/06/Chapter-20.pdf">Unselected original</a>'
    )

    def download(url, *, consume, **kwargs):
        body = primary if url == PRIMARY else secondary if url == SECONDARY else catalog.encode()
        return {
            "url": url,
            "filename": "origin.pdf"
            if url == PRIMARY
            else "annex.pdf"
            if url == SECONDARY
            else "catalog.html",
            "mime_type": "application/pdf" if body.startswith(b"%PDF") else "text/html",
            "blob": consume(iter([body])),
        }

    monkeypatch.setattr(ingestion, "_download", download)
    service = Service(tmp_path)
    campaign = ResourceCampaign(service, PLAN, selected_options())
    for _ in range(15):
        active = campaign.step()
        while claim := service.collection.claim_fetch():
            service.collection.fetch_item(claim)
        if assert_pipeline and len(campaign.state["items"]["5.3"]["collection_runs"]) == 1:
            assert campaign.step()
            assert len(campaign.state["items"]["5.3"]["collection_runs"]) == 2
            with closing(service.db.connect()) as conn:
                assert conn.execute("SELECT status FROM processing_jobs").fetchone()[0] == "queued"
            assert_pipeline = False
        while claim := service.collection.claim_processing():
            service.collection.process_item(claim)
        if not active:
            break
    assert not campaign.step()
    campaign.verify_originals()
    return service, campaign


def test_secondary_original_download_does_not_wait_for_primary_embedding(tmp_path, monkeypatch):
    service, campaign = run_selected(tmp_path, monkeypatch, assert_pipeline=True)
    assert campaign.report()["summary"]["secondary_originals"] == 1


def test_embedding_retry_is_limited_to_failed_complete_primaries(tmp_path, monkeypatch):
    service, campaign = run_selected(tmp_path, monkeypatch)
    with service.db.transaction() as conn:
        primary = conn.execute(
            "SELECT document_id FROM legal_classifications WHERE level='primary'"
        ).fetchone()[0]
        secondary = conn.execute(
            "SELECT document_id FROM legal_classifications WHERE level='secondary'"
        ).fetchone()[0]
        conn.execute(
            "UPDATE processing_jobs SET status='failed',phase='embed' WHERE document_id IN (?,?)",
            (primary, secondary),
        )
    assert campaign.retry_embeddings() == [primary]
    with closing(service.db.connect()) as conn:
        assert (
            conn.execute("SELECT status FROM processing_jobs WHERE document_id=?", (secondary,)).fetchone()[0]
            == "failed"
        )
    assert campaign.retry_embeddings() == []


@pytest.mark.parametrize("kind", ["cooldown", "invalid_credentials", "rate_limit"])
@pytest.mark.parametrize("delay", [75, 900])
def test_embedding_provider_wait_and_permanent_errors_keep_queue_attempts_meaningful(
    tmp_path, monkeypatch, kind, delay
):
    from app.ingestion import EmbeddingProviderDeferred, EmbeddingServiceError

    service, campaign = run_selected(tmp_path, monkeypatch)
    with service.db.transaction() as conn:
        primary = conn.execute(
            "SELECT document_id FROM legal_classifications WHERE level='primary'"
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM retrieval_vectors WHERE chunk_rowid IN (SELECT rowid FROM retrieval_chunks WHERE document_id=?)",
            (primary,),
        )
        service.collection.queue_processing(conn, primary, campaign.state["owner_id"])
    claim = service.collection.claim_processing()
    assert claim["phase"] == "embed"
    error = (
        EmbeddingProviderDeferred(delay)
        if kind == "cooldown"
        else EmbeddingServiceError(retryable=kind == "rate_limit", retry_after=delay)
    )
    started = time.time()
    service.collection._failure(claim, error, processing=True)
    with closing(service.db.connect()) as conn:
        job = conn.execute(
            "SELECT status,attempts,available_at FROM processing_jobs WHERE document_id=?", (primary,)
        ).fetchone()
    assert job["status"] == ("failed" if kind == "invalid_credentials" else "queued")
    assert job["attempts"] == (0 if kind == "cooldown" else 1)
    if kind != "invalid_credentials":
        assert started + delay - 1 <= job["available_at"] <= time.time() + delay + 1


@pytest.mark.parametrize(
    "gate", ["recover", "published", "active_worker", "independent_warning", "empty_independent_warning"]
)
def test_native_table_revalidation_preserves_source_and_ocr_and_respects_review_leases(
    tmp_path, monkeypatch, gate
):
    from app.ingestion import rebuild_pdf_manifest
    from app.catalog import MetadataPatch
    from test_pdf_tables import merged_header_pdf

    service, campaign = run_selected(tmp_path, monkeypatch, primary_pdf=merged_header_pdf())
    with service.db.transaction() as conn:
        identifier = conn.execute(
            "SELECT document_id FROM legal_classifications WHERE level='primary'"
        ).fetchone()[0]
        document = service.own_document(conn, campaign.state["owner_id"], identifier)
        manifest = json.loads(document["manifest"])
        page = manifest["pages"][0]
        page.pop("native_table", None)
        page.update(
            status="pending_ocr", text=page["native_text"], reasons=["table_layout"], ocr_required=True
        )
        old_ocr = {
            "complete": True,
            "text": "Earlier OCR reading retained as immutable evidence",
            "uncertain_spans": ["table_layout_not_preserved"],
            "image_blob": {"content_hash": "a" * 64, "size": 20, "storage_path": "files/test-previous-image"},
        }
        if gate in {"independent_warning", "empty_independent_warning"}:
            page["status"] = "ocr_uncertain"
            old_ocr["uncertain_spans"].append("independent_unreadable_number")
            if gate == "empty_independent_warning":
                old_ocr.update(text="", complete=False)
        page["ocr"] = deepcopy(old_ocr)
        manifest = rebuild_pdf_manifest(manifest)
        old_ocr = deepcopy(manifest["pages"][0]["ocr"])
        conn.execute(
            'UPDATE documents SET manifest=?,status="processing" WHERE id=?',
            (json.dumps(manifest), identifier),
        )
        conn.execute(
            'UPDATE processing_jobs SET phase="parse",status=?,lease_until=? WHERE document_id=?',
            ("processing" if gate == "active_worker" else "queued", time.time() + 300, identifier),
        )
    if gate == "published":
        metadata = service.catalog.metadata_for(identifier)
        service.catalog.patch_document(
            campaign.state["owner_id"],
            identifier,
            MetadataPatch(expected_revision=metadata["metadata_revision"], review_status="published"),
            on_changed=service.metadata_changed,
        )
    results = campaign.revalidate_native_tables()
    with closing(service.db.connect()) as conn:
        current = service.own_document(conn, campaign.state["owner_id"], identifier)
    assert current["content_hash"] == document["content_hash"] and current["version"] == document["version"]
    updated = json.loads(current["manifest"])["pages"][0]
    if gate == "recover":
        assert results[0]["pages"] == [1]
        assert (
            updated["status"] == "native" and updated["native_table"]["method"] == "verified-digital-grid-v2"
        )
        assert updated["ocr"]["image_blob"] == old_ocr["image_blob"]
        assert updated["ocr"]["text"] == old_ocr["text"] and updated["ocr"]["used_for_current_text"] is False
        assert updated["native_revalidation"]["previous_page"]["ocr"] == old_ocr
    else:
        assert not results
        assert updated["status"] == page["status"] and updated["ocr"] == old_ocr


@pytest.mark.parametrize("delete", ["all", "one"])
def test_report_does_not_mark_missing_chunk_index_as_vector_complete(tmp_path, monkeypatch, delete):
    service, campaign = run_selected(
        tmp_path, monkeypatch, primary_pdf=pdf("Original legal evidence. " * 300)
    )
    with service.db.transaction() as conn:
        identifier = conn.execute(
            "SELECT document_id FROM legal_classifications WHERE level='primary'"
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT count(*) FROM retrieval_chunks WHERE document_id=?", (identifier,)
            ).fetchone()[0]
            > 1
        )
        if delete == "all":
            conn.execute("DELETE FROM retrieval_chunks WHERE document_id=?", (identifier,))
        else:
            conn.execute(
                "DELETE FROM retrieval_chunks WHERE rowid=(SELECT rowid FROM retrieval_chunks WHERE document_id=? ORDER BY ordinal DESC LIMIT 1)",
                (identifier,),
            )
    report = campaign.report()
    assert report["summary"]["primary_vector_complete"] == 0


def test_selected_levels_parse_both_vectorize_only_primary_and_map_fixed_versions(tmp_path, monkeypatch):
    service, campaign = run_selected(tmp_path, monkeypatch)
    report = campaign.report()
    summary = report["summary"]
    assert summary["originals"] == summary["text_complete"] == summary["integrity_verified"] == 2
    assert summary["primary_originals"] == summary["secondary_originals"] == 1
    assert summary["vectors"] > 0 and summary["primary_vector_complete"] == 1
    assert summary["secondary_vectors"] == 0
    assert summary["mapped_relations"] == 1 and summary["unresolved_relations"] == 0
    mapped = report["mapping"][0]
    assert mapped["locator"]["primary_version"] == mapped["locator"]["secondary_version"] == 1
    assert len(mapped["locator"]["content_hash"]) == 64
    assert not service.search(campaign.state["owner_id"], "Originating Goods", country="ID")["items"]
    assert not service.search(campaign.state["owner_id"], "Certificate of Origin", country="SG")["items"]
    restarted = ResourceCampaign(Service(tmp_path), PLAN, selected_options(), state=deepcopy(campaign.state))
    assert not restarted.step()
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM legal_relations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert set(
            row[0]
            for row in conn.execute("SELECT json_extract(payload,'$.review_status') FROM document_metadata")
        ) == {"draft"}
    inventory = export_inventory(tmp_path, owner=campaign.state["owner_id"], country="ID", include_text=True)
    text_path = next(inventory["path"].rglob("texts.jsonl"))
    records = [json.loads(line) for line in text_path.read_text().splitlines()]
    assert {entry["legal_level"] for entry in records} == {"primary", "secondary"}
    assert all(entry["elements"] and entry["chunks"] and entry["source_hash"] for entry in records)
    assert all(entry["review_status"] == "draft" for entry in records)
    assert text_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("gate", ["draft", "published", "active_worker", "frozen_version", "clean_ocr"])
def test_pdf_decode_revalidation_preserves_evidence_and_requeues_only_owned_drafts(
    tmp_path, monkeypatch, gate
):
    from app.catalog import MetadataPatch

    service, campaign = run_selected(tmp_path, monkeypatch)
    owner = campaign.state["owner_id"]
    with service.db.transaction() as conn:
        row = conn.execute(
            "SELECT d.* FROM documents d JOIN legal_classifications l ON l.document_id=d.id WHERE l.level='primary'"
        ).fetchone()
        manifest = json.loads(row["manifest"])
        page = manifest["pages"][0]
        page["native_text"] += " \ufffd"
        if gate != "clean_ocr":
            page["text"] = page["native_text"]
        else:
            page.update(status="ocr_complete", ocr={"complete": True, "uncertain_spans": []})
        before = deepcopy(page)
        encoded = json.dumps(manifest)
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (encoded, row["id"]))
        if gate == "active_worker":
            conn.execute(
                "UPDATE processing_jobs SET status='processing',lease_until=? WHERE document_id=?",
                (time.time() + 300, row["id"]),
            )
        elif gate == "frozen_version":
            conn.execute("UPDATE checklist_bindings SET version=version+1")
    if gate == "published":
        metadata = service.catalog.metadata_for(row["id"])
        service.catalog.patch_document(
            owner,
            row["id"],
            MetadataPatch(expected_revision=metadata["metadata_revision"], review_status="published"),
            on_changed=service.metadata_changed,
        )
    results = campaign.revalidate_pdf_decode()
    with closing(service.db.connect()) as conn:
        current = service.own_document(conn, owner, row["id"])
        job = conn.execute("SELECT * FROM processing_jobs WHERE document_id=?", (row["id"],)).fetchone()
        index = conn.execute("SELECT * FROM retrieval_documents WHERE document_id=?", (row["id"],)).fetchone()
        updated = json.loads(current["manifest"])
        assert (current["content_hash"], current["version"], current["storage_path"]) == (
            row["content_hash"],
            row["version"],
            row["storage_path"],
        )
        if gate == "draft":
            assert results == [{"document_id": row["id"], "source_hash": row["content_hash"], "pages": [1]}]
            assert updated["pages"][0]["status"] == "pending_ocr"
            assert updated["pages"][0]["decode_revalidation"]["previous_page"] == before
            assert updated["pages"][0]["native_text"] == before["native_text"]
            assert updated["quality"]["complete"] is False and index["complete"] == 0
            assert (job["phase"], job["status"]) == ("parse", "queued")
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id=?",
                    (row["id"],),
                ).fetchone()[0]
                == 0
            )
            assert campaign.state["pdf_decode_revalidations"][-1]["documents"] == results
        else:
            assert not results and current["manifest"] == encoded
    assert not campaign.revalidate_pdf_decode()  # No duplicate audit evidence or retries on a second pass.


def test_pdf_decode_revalidation_detects_changes_before_committing_a_checkpoint(tmp_path, monkeypatch):
    from app import parsing

    service, campaign = run_selected(tmp_path, monkeypatch)
    with service.db.transaction() as conn:
        row = conn.execute(
            "SELECT d.* FROM documents d JOIN legal_classifications l ON l.document_id=d.id WHERE l.level='primary'"
        ).fetchone()
        manifest = json.loads(row["manifest"])
        manifest["pages"][0]["text"] += " \ufffd"
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (json.dumps(manifest), row["id"]))
    original = parsing.rebuild_pdf_bounded

    def concurrent_change(manifest):
        result = original(manifest)
        with service.db.transaction() as conn:
            conn.execute("UPDATE processing_jobs SET status='processing' WHERE document_id=?", (row["id"],))
        return result

    monkeypatch.setattr(parsing, "rebuild_pdf_bounded", concurrent_change)
    with pytest.raises(ValueError, match="changed during revalidation"):
        campaign.revalidate_pdf_decode()
    with closing(service.db.connect()) as conn:
        current = service.own_document(conn, campaign.state["owner_id"], row["id"])
        assert json.loads(current["manifest"])["pages"][0].get("decode_revalidation") is None


def test_missing_mapping_evidence_keeps_both_originals_without_inventing_a_relation(tmp_path, monkeypatch):
    service, campaign = run_selected(tmp_path, monkeypatch, shared=False)
    assert campaign.report()["summary"]["originals"] == 2
    assert campaign.report()["mapping"][0]["status"] == "missing_shared_source_evidence"
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM legal_relations").fetchone()[0] == 0


@pytest.mark.parametrize("extra_warning", [None, "model_reported_incomplete"])
def test_layout_revalidation_preserves_reading_image_and_nonlayout_uncertainty(
    tmp_path, monkeypatch, extra_warning
):
    from app.ingestion import rebuild_pdf_manifest

    service, campaign = run_selected(tmp_path, monkeypatch)
    owner = campaign.state["owner_id"]
    with service.db.transaction() as conn:
        row = conn.execute(
            "SELECT d.* FROM documents d JOIN legal_classifications c ON c.document_id=d.id WHERE c.level='primary'"
        ).fetchone()
        manifest = json.loads(row["manifest"])
        page = manifest["pages"][0]
        page.update(status="ocr_uncertain", reasons=["table_layout"])
        page["ocr"] = {
            "complete": True,
            "model": "saved-model",
            "uncertain_spans": ["table_layout_not_preserved"] + ([extra_warning] if extra_warning else []),
            "image_blob": {"content_hash": "a" * 64, "storage_path": "unchanged-image-reference"},
        }
        old_image, old_text = deepcopy(page["ocr"]["image_blob"]), page["text"]
        conn.execute(
            "UPDATE documents SET manifest=? WHERE id=?",
            (json.dumps(rebuild_pdf_manifest(manifest)), row["id"]),
        )
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status='failed' WHERE document_id=?", (row["id"],)
        )
    result = campaign.revalidate_layout()
    assert result[0]["document_id"] == row["id"] and result[0]["pages"] == [1]
    with closing(service.db.connect()) as conn:
        current = service.own_document(conn, owner, row["id"])
        manifest = json.loads(current["manifest"])
        page = manifest["pages"][0]
        assert current["content_hash"] == row["content_hash"] and current["version"] == row["version"]
        assert page["text"] == old_text and page["ocr"]["image_blob"] == old_image
        assert page["layout_revalidation"]["previous_uncertain_spans"][0] == "table_layout_not_preserved"
        assert manifest["quality"]["complete"] == (extra_warning is None)
        assert page["ocr"]["uncertain_spans"] == ([extra_warning] if extra_warning else [])


@pytest.mark.parametrize("change", ["wrong-country", "wrong-source", "missing-anchor", "budget", "duplicate"])
def test_selection_validation_rejects_scope_and_mapping_errors_before_account_creation(tmp_path, change):
    service, options = Service(tmp_path), selected_options()
    materials = options["selection"]["materials"]
    if change == "wrong-country":
        materials[0]["url"] = "https://sso.agc.gov.sg/Act/SFA1973"
    elif change == "wrong-source":
        options["selection"]["source_document_sha256"] = "0" * 64
    elif change == "missing-anchor":
        materials[1]["primary_keys"] = ["missing"]
    elif change == "budget":
        options["originals_per_item"] = 1
    else:
        materials[1]["key"] = materials[0]["key"]
    with pytest.raises(ValueError):
        ResourceCampaign(service, PLAN, options)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_singapore_source_recipes_keep_china_treaty_and_member_commitments_scoped():
    plan = load_blueprints()["singapore-demo-20261009"]
    definitions = {entry["id"]: entry for entry in plan["items"]}
    rules = recipe_for(definitions["5.2"])
    address = "https://www.wto.org/english/docs_e/legal_e/legal_e.htm"
    page = b'<title>Official legal texts</title><a href="https://docs.wto.org/dol2fe/Pages/SS/directdoc2.aspx?Open=True&amp;filename=Q:/SCHD/GATS-SC/SC76S4.pdf">Singapore Supplement 4</a><a href="https://docs.wto.org/dol2fe/Pages/SS/directdoc2.aspx?Open=True&amp;filename=Q:/SCHD/GATS-SC/SC43.pdf">Indonesia</a>'
    result = discover_page(address, page, "text/html", rules)
    assert len(result["links"]) == 1 and "SC76S4" in result["links"][0]["url"]
    assert (
        discover_page("https://goods-schedules.wto.org/member/indonesia", page, "text/html", rules)["matched"]
        is False
    )
    source = definitions["5.3"]["entries"][0]["url"]
    page = b'<title>DTAs</title><a href="/media/docs/default-source/uploadedfiles/pdf/ratificationsingaporechinadtafinalwithprotocol.pdf">China</a><a href="/docs/default-source/pdf/australia-dta.pdf">Australia</a><a href="/taxes/goods-services-tax-(gst)/other">GST</a>'
    result = discover_page(source, page, "text/html", recipe_for(definitions["5.3"]))
    assert len(result["links"]) == 1 and "chinadta" in result["links"][0]["url"]
    operational = "https://www.iras.gov.sg/docs/default-source/uploadedfiles/pdf/user-guide-(tp)_file-form-c-sfb87cf1c-ae51-4063-802b-e57454d68147.pdf?sfvrsn=bf08a50e_32"
    page = (
        '<title>Official tax guides</title><a href="'
        + operational
        + '">Tax portal filing steps</a><a href="https://www.iras.gov.sg/docs/default-source/e-tax/etaxguide_gst_general.pdf">GST general guide</a>'
    ).encode()
    result = discover_page("https://www.iras.gov.sg/", page, "text/html", recipe_for(definitions["3.3"]))
    assert len(result["links"]) == 1 and "etaxguide_gst_general" in result["links"][0]["url"]


def test_html_main_text_drops_navigation_and_preserves_original_line_locators():
    from app.ingestion import parse_file

    body = b'<html>\n<nav>OUTSIDE_MENU</nav>\n<main>\n<nav>INNER_MENU</nav>\n<h1 id="law">Legal requirements</h1>\n<p>Businesses must retain the original evidence.</p>\n</main>\n<footer>FOOTER</footer></html>'
    manifest = parse_file("guide.html", body, "text/html")
    texts = " ".join(entry["content_text"] for entry in manifest["elements"])
    assert "Legal requirements" in texts and "original evidence" in texts
    assert all(term not in texts for term in ["OUTSIDE_MENU", "INNER_MENU", "FOOTER"])
    assert manifest["elements"][0]["locator"]["line_start"] == 5


@pytest.mark.parametrize(
    "state", ["outdated", "current", "published", "active", "incomplete", "wrong_source"]
)
def test_recovery_retry_keeps_page_evidence_and_respects_version_and_review(tmp_path, monkeypatch, state):
    from app.db import dump
    from app.ocr_recovery import RECOVERY_VERSION

    service, campaign = run_selected(tmp_path, monkeypatch)
    with service.db.transaction() as conn:
        row = conn.execute("SELECT * FROM documents WHERE source_url=?", (PRIMARY,)).fetchone()
        manifest = json.loads(row["manifest"])
        page = manifest["pages"][0]
        page["status"] = "ocr_uncertain"
        page["ocr"] = {
            "complete": state != "incomplete",
            "uncertain_spans": ["printed_literal"],
            "image_sha256": "a" * 64,
            "recovery": {
                "version": RECOVERY_VERSION if state == "current" else "legal-region-recovery-v3",
                "status": "conflict",
            },
        }
        if state == "wrong_source":
            page["source_hash"] = "f" * 64
        before = dump(manifest)
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (before, row["id"]))
        conn.execute(
            "UPDATE processing_jobs SET phase='parse',status=?,lease_until=? WHERE document_id=?",
            (
                "processing" if state == "active" else "failed",
                time.time() + 120 if state == "active" else None,
                row["id"],
            ),
        )
        if state == "published":
            conn.execute(
                "UPDATE document_metadata SET payload=json_set(payload,'$.review_status','published') WHERE document_id=?",
                (row["id"],),
            )
    result = campaign.retry_ocr_recovery()
    assert bool(result) == (state == "outdated")
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT manifest FROM documents WHERE id=?", (row["id"],)).fetchone()[0] == before
        job = conn.execute(
            "SELECT status,phase FROM processing_jobs WHERE document_id=?", (row["id"],)
        ).fetchone()
    assert job["status"] == (
        "queued" if state == "outdated" else "processing" if state == "active" else "failed"
    )
    assert job["phase"] == "parse"
    audit = campaign.state["ocr_recovery_retries"][-1]
    assert audit["recovery_version"] == RECOVERY_VERSION
    if result:
        assert audit["documents"] == [
            {"document_id": row["id"], "source_hash": row["content_hash"], "pages": [1]}
        ]
