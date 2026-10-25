"""Material tiers, immutable associations, model work and private text exports."""

import io
import textwrap
from contextlib import closing
from copy import deepcopy

import pytest
from app.bootstrap import Service
from app.contexts.ingestion.application.checklists import load_blueprints
from app.contexts.ingestion.application.resource_campaign import ResourceCampaign, validate_extension
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import discover_page, recipe_for
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

PLAN = "indonesia-20261003"
CATALOG = "https://fta.mofcom.gov.cn/channel/rcep.shtml"
PRIMARY = "https://fta.mofcom.gov.cn/upload/agreementFiles/rcep/rceppdf/d3z_en.pdf"
SECONDARY = "https://fta.mofcom.gov.cn/upload/agreementFiles/rcep/rceppdf/d3z_fj2_en.pdf"


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
            "url": "https://fta.mofcom.gov.cn/upload/agreementFiles/rcep/rceppdf/d3z_fj1_en.pdf",
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
    extended = ResourceCampaign(
        service, PLAN, extended_options(), state=deepcopy(previous), version=13, extend=True
    )
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
    from app.contexts.documents.infrastructure.providers import embedding as ingestion_embedding
    from app.shared.infrastructure.http import download as ingestion_download

    for key, value in {
        "EMBEDDING_BASE_URL": "https://provider.example/v1",
        "EMBEDDING_API_KEY": "fake-test",
        "EMBEDDING_MODEL": "test-model",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ingestion_embedding, "embed_texts", lambda texts: [[1.0, 0.0] for _ in texts])
    primary = primary_pdf or pdf(
        "Article 3.1 Originating Goods. Products must meet the requirements of this agreement and its rules of origin."
    )
    secondary = pdf(
        "Annex 3B Certificate of Origin. The minimum information requirements identify the exporter, producer and goods."
    )
    catalog = f'<title>RCEP Agreement</title><a href="{PRIMARY}">Chapter 3</a>'
    if shared:
        catalog += f'<a href="{SECONDARY}">Annex 3B</a>'
    catalog += '<a href="https://fta.mofcom.gov.cn/upload/agreementFiles/rcep/rceppdf/d20z_en.pdf">Unselected original</a>'

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

    monkeypatch.setattr(ingestion_download, "_download", download)
    service = Service(tmp_path)
    campaign = ResourceCampaign(service, PLAN, selected_options(), version=13)
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


def test_missing_mapping_evidence_keeps_both_originals_without_inventing_a_relation(tmp_path, monkeypatch):
    service, campaign = run_selected(tmp_path, monkeypatch, shared=False)
    assert campaign.report()["summary"]["originals"] == 2
    assert campaign.report()["mapping"][0]["status"] == "missing_shared_source_evidence"
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM legal_relations").fetchone()[0] == 0


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
        ResourceCampaign(service, PLAN, options, version=13)
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
    from app.contexts.documents.infrastructure.parsers.native_parser import parse_file

    body = b'<html>\n<nav>OUTSIDE_MENU</nav>\n<main>\n<nav>INNER_MENU</nav>\n<h1 id="law">Legal requirements</h1>\n<p>Businesses must retain the original evidence.</p>\n</main>\n<footer>FOOTER</footer></html>'
    manifest = parse_file("guide.html", body, "text/html")
    texts = " ".join(entry["content_text"] for entry in manifest["elements"])
    assert "Legal requirements" in texts and "original evidence" in texts
    assert all(term not in texts for term in ["OUTSIDE_MENU", "INNER_MENU", "FOOTER"])
    assert manifest["elements"][0]["locator"]["line_start"] == 5
