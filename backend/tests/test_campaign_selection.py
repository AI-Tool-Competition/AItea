"""Material tiers, immutable associations, model work and private text exports."""

from contextlib import closing
from copy import deepcopy
import io
import json

import pytest
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.archive_inventory import export_inventory
from app.checklists import load_blueprints
from app.discovery import discover_page, recipe_for
from app.resource_campaign import ResourceCampaign
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


def pdf(text):
    writer = PdfWriter()
    page = writer.add_blank_page(600, 800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(("BT /F1 12 Tf 20 700 Td (" + text + ") Tj ET").encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def run_selected(tmp_path, monkeypatch, *, shared=True, assert_pipeline=False):
    from app import ingestion

    for key, value in {
        "EMBEDDING_BASE_URL": "https://provider.example/v1",
        "EMBEDDING_API_KEY": "fake-test",
        "EMBEDDING_MODEL": "test-model",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ingestion, "embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])
    primary = pdf(
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


def test_html_main_text_drops_navigation_and_preserves_original_line_locators():
    from app.ingestion import parse_file

    body = b'<html>\n<nav>OUTSIDE_MENU</nav>\n<main>\n<nav>INNER_MENU</nav>\n<h1 id="law">Legal requirements</h1>\n<p>Businesses must retain the original evidence.</p>\n</main>\n<footer>FOOTER</footer></html>'
    manifest = parse_file("guide.html", body, "text/html")
    texts = " ".join(entry["content_text"] for entry in manifest["elements"])
    assert "Legal requirements" in texts and "original evidence" in texts
    assert all(term not in texts for term in ["OUTSIDE_MENU", "INNER_MENU", "FOOTER"])
    assert manifest["elements"][0]["locator"]["line_start"] == 5
