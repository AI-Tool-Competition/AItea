"""Member-bound WTO catalogs preserve original mappings and explicit gaps."""

from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
from urllib.parse import urlencode
from xml.etree import ElementTree

import pytest
from app.contexts.ingestion.application.checklists import (
    ChecklistService,
    load_blueprint_version,
    load_blueprints,
)
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
    _Recipe,
    discover_page,
    recipe_for,
    request_parameters,
)
from app.contexts.ingestion.infrastructure.source_adapters.wto_discovery import (
    DOCS,
    GOODS,
    MEMBERS,
    XML_PATH,
    catalog_url,
    services_catalog_url,
)
from app.configuration.paths import PROJECT_ROOT


def item(member="ID", version=None):
    plan_id, code = ("indonesia-20261003", "5.4") if member == "ID" else ("singapore-demo-20261009", "5.2")
    plan = load_blueprint_version(plan_id, version) if version else load_blueprints()[plan_id]
    return deepcopy(next(row for row in plan["items"] if row["id"] == code))


def inspect(url, body, member="ID", mime="text/html"):
    return discover_page(url, body, mime, recipe_for(item(member)))


def goods_catalog(member="ID", *, total=1, page=0, nodes=None, status="All", pre_wto=True, pager=None):
    info = MEMBERS[member]
    rows = ""
    for index in range(max(0, min(10, total - page * 10))):
        rows += f"""<tr><td headers="view-title-table-column">Procedure {page * 10 + index}</td>
        <td headers="view-field-last-updated-table-column">15.04.1994</td>
        <td headers="view-fields-procedure-type-table-column">Pre-WTO&gt;HS</td>
        <td headers="view-field-procedure-status-table-column">Unknown status</td>
        <td headers="view-procedure-certifications-table-column">G/HS</td>
        <td headers="view-fields-access-document-member-table-column"><a href="/browse/{500 + index}/file_browser">+</a></td></tr>"""
    if pager is None:
        pager = (
            f'<nav class="pager"><a href="?page={page}">Current</a><a href="?page={page + 1}">Next</a></nav>'
            if total > 10
            else ""
        )
    return f'''<html><head><link rel="shortlink" href="/node/{nodes or info["node"]}"></head><body>
    <article class="member"><h1>{info["name"]}</h1><div class="schedule-label">Schedule {info["schedule"]} ({info["number"]})</div>
    <div class="view-display-id-all_documents"><input name="status" value="{status}" checked>
    <input name="type_1[1]" value="1" {"checked" if not pre_wto else ""}>
    <div class="view-header">{total} results<a href="/member/download/{info["node"]}">Download all public documents</a></div>
    <table>{rows}</table>{pager}</div></article></body></html>'''.encode()


def browser(member="ID", *, extension="mdb", schedule=None, path=None, pre_wto=False):
    info = MEMBERS[member]
    path = (
        path or f"/system/files/WTO_import/DVD/{info['name']}/1_Pre-Uruguay%20Schedule/original.{extension}"
    )
    return f'''<html><h1 class="page-title">L/6987/Add.1</h1><div class="node-file-browser {"pre-ur" if pre_wto else "procedure"}">
    <div class="field--name-field-procedure-schedule">{schedule or info["schedule"]}</div>
    <div class="accordion-download"><div class="accordion-header">L/6987/Add.1 Document collection</div>
    <div class="paragraph-document"><div class="field--type-datetime"><div class="field__item">02.04.1992</div></div>
    <div class="file-item"><div class="name">original.{extension}</div><div class="lang"><span>English</span><a href="{path}"></a></div>
    <div class="lang"><span>French</span><a href="{path}"></a></div></div></div></div></div></html>'''.encode()


def xml_catalog(member="ID", *, symbol=None, filename=None, country=None, public="True", total=None, **extra):
    number = MEMBERS[member]["service"]
    fields = {
        "CATID": "123",
        "SYMBOL": symbol or f"GATS/SC/{number}/Suppl.4",
        "CATTITLE": "Original schedule supplement",
        "CONCERNEDCOUNTRIES": country or MEMBERS[member]["name"],
        "COLLECTION": "SCHD",
        "ISSUINGDATE": "01/01/2026 00:00:00",
        "HasAccess": public,
        "FILENAMESA": filename or f"Q:/SCHD/GATS-SC/SC{number}S4.pdf",
        "FILENAMESB": "",
        **extra,
    }
    namespace = "{http://schemas.autonomy.com/aci/}"
    root = ElementTree.Element("autnresponse")
    ElementTree.SubElement(root, "response").text = "SUCCESS"
    data = ElementTree.SubElement(root, "responsedata")
    ElementTree.SubElement(data, namespace + "numhits").text = "1"
    ElementTree.SubElement(data, namespace + "totalhits").text = str(1 if total is None else total)
    hit = ElementTree.SubElement(data, namespace + "hit")
    ElementTree.SubElement(hit, namespace + "database").text = "English"
    content = ElementTree.SubElement(hit, namespace + "content")
    document = ElementTree.SubElement(content, "DOCUMENT")
    for key, value in fields.items():
        ElementTree.SubElement(document, key).text = value
    return ElementTree.tostring(root, encoding="utf-8")


@pytest.mark.parametrize("member", ["ID", "SG"])
def test_catalog_requires_the_exact_member_and_retains_historical_status(member):
    url = f"https://{GOODS}/member/{MEMBERS[member]['slug']}"
    body = goods_catalog(member)
    result = inspect(url, body, member)
    assert result["matched"] and result["purpose"] == "catalog"
    assert result["scope"]["source_sha256"] == sha256(body).hexdigest()
    assert result["scope"]["member_id"] == MEMBERS[member]["node"]
    assert result["scope"]["schedule_number"] == MEMBERS[member]["number"]
    assert result["scope"]["documents"][0]["status"] == "Unknown status"
    assert result["scope"]["include_pre_wto"]
    assert not result["scope"]["exhaustive_content_verified"]
    assert not result["scope"]["itip_exports_verified"]
    assert {link["purpose"] for link in result["links"]} == {"detail", "catalog"}
    other = "SG" if member == "ID" else "ID"
    assert not inspect(url, goods_catalog(other), member)["matched"]
    assert not inspect(url, goods_catalog(member, nodes=1), member)["matched"]
    assert not inspect(url, goods_catalog(member, status="Certified"), member)["matched"]
    assert not inspect(url, goods_catalog(member, pre_wto=False), member)["matched"]


def test_goods_pagination_must_preserve_the_real_member_and_count():
    url = f"https://{GOODS}/member/indonesia"
    result = inspect(url, goods_catalog(total=12))
    assert result["matched"] and result["scope"]["returned"] == 10
    assert any(link["url"].endswith("?page=1") for link in result["links"])
    assert inspect(
        url + "?page=1",
        goods_catalog(
            total=12, page=1, pager='<nav class="pager"><a href="?page=1">2</a><a href="?page=0">1</a></nav>'
        ),
    )["matched"]
    for pager in (
        "",
        '<nav class="pager"><a href="?page=2">Next</a></nav>',
        '<nav class="pager"><a href="/member/singapore?page=1">Next</a></nav>',
        '<nav class="pager"><a href="?status=Certified&page=1">Next</a></nav>',
    ):
        assert not inspect(url, goods_catalog(total=12, pager=pager))["matched"]
    assert not inspect(url + "?page=2", goods_catalog(total=12, page=2))["matched"]
    assert not inspect(url + "?type_1%5B1%5D=1", goods_catalog())["matched"]


@pytest.mark.parametrize("extension", ["pdf", "doc", "docx", "xls", "mdb", "wpf"])
def test_all_published_legacy_representations_keep_language_and_source_mapping(extension):
    result = inspect(f"https://{GOODS}/browse/40331/file_browser", browser(extension=extension, pre_wto=True))
    assert result["matched"] and result["purpose"] == "detail"
    assert len(result["links"]) == 1
    records = result["scope"]["documents"]
    assert [record["language"] for record in records] == ["English", "French"]
    assert all(record["document_dates"] == ["02.04.1992"] for record in records)
    assert all(not record["representation_equivalence_verified"] for record in records)
    assert all(record["source_format"] == extension for record in records)
    assert all(record["legal_review_status"] == "not_reviewed" for record in records)


def test_file_browser_schedule_and_country_path_do_not_cross_member_boundaries():
    url = f"https://{GOODS}/browse/123/file_browser"
    assert not inspect(url, browser(schedule="LXXIII"))["matched"]
    result = inspect(
        url, browser(path="/system/files/WTO_import/DVD/Singapore/1_Pre-Uruguay%20Schedule/other.pdf")
    )
    assert result["matched"] and not result["links"] and result["warnings"]
    assert result["scope"]["documents"][0]["original_policy"] == "unsupported_public_path"


@pytest.mark.parametrize("member", ["ID", "SG"])
def test_service_api_requires_actual_symbol_country_and_public_file(member):
    url = services_catalog_url(member, "SC")
    body = xml_catalog(member)
    result = inspect(url, body, member, "text/xml")
    assert result["matched"] and len(result["links"]) == 1
    record = result["scope"]["documents"][0]
    source_record = result["links"][0]["source_record"]
    assert source_record["provider"] == "wto"
    assert source_record["fields"]["symbol"] == record["symbol"]
    assert source_record["fields"]["concerned_countries"] == MEMBERS[member]["name"]
    assert source_record["structure"]["unit_type"] == "member_schedule"
    assert source_record["structure"]["link_mechanism"] == "official_xml_catalog"
    assert record["source_metadata"]["SYMBOL"].endswith("/Suppl.4")
    assert record["source_metadata"]["HasAccess"] == "True"
    assert result["scope"]["source_sha256"] == sha256(body).hexdigest()
    assert result["scope"]["language"] == "English"
    assert not result["scope"]["legal_status_inferred"]
    original = inspect(result["links"][0]["url"], b"%PDF-1.7\noriginal", member, "application/pdf")
    assert original["matched"] and original["source_record"]["provider"] == "wto"
    assert original["source_record"]["fields"]["header_signature"] == "verified_pdf_header"
    assert original["source_record"]["fields"]["legal_status"] == "not_inferred"
    wrong = "Singapore" if member == "ID" else "Indonesia"
    assert not inspect(url, xml_catalog(member, country=wrong), member, "text/xml")["matched"]
    mismatch = inspect(
        url,
        xml_catalog(member, filename=f"Q:/SCHD/GATS-SC/SC{MEMBERS[member]['service']}S3.pdf"),
        member,
        "text/xml",
    )
    assert mismatch["matched"] and not mismatch["links"] and mismatch["warnings"]
    restricted = inspect(url, xml_catalog(member, public="False"), member, "text/xml")
    assert restricted["matched"] and not restricted["links"]
    assert restricted["scope"]["documents"][0]["missing_original"]


@pytest.mark.parametrize(
    "mutation", ["truncated", "wrong_prefix", "wrong_number", "entity", "database", "duplicate_field"]
)
def test_invalid_xml_never_becomes_a_matched_or_complete_catalog(mutation):
    body = xml_catalog()
    if mutation == "truncated":
        body = xml_catalog(total=101)
    elif mutation == "wrong_prefix":
        body = xml_catalog(symbol="GATS/SC/430/Suppl.4")
    elif mutation == "wrong_number":
        body = xml_catalog(symbol="GATS/SC/76/Suppl.4")
    elif mutation == "entity":
        body = b'<!DOCTYPE autnresponse [<!ENTITY x "expanded">]>' + body
    elif mutation == "database":
        body = body.replace(b">English<", b">French<")
    else:
        body = body.replace(b"</DOCUMENT>", b"<CATID>456</CATID></DOCUMENT>")
    result = inspect(services_catalog_url("ID", "SC"), body, mime="text/xml")
    assert not result["matched"] and not result["links"] and result["reason"]


def test_public_xml_request_has_fixed_scope_and_country_and_no_mutations():
    definition = {"country": "ID", "item": item()}
    assert request_parameters(services_catalog_url("ID", "SC"), definition) == {}
    for url in (
        services_catalog_url("SG", "SC"),
        f"https://{DOCS}{XML_PATH}?"
        + urlencode(
            {
                "DataSource": "Cat",
                "Query": "@Symbol=GATS/SC/*",
                "XMLMaxHitsPerPage": "100",
                "Language": "English",
            }
        ),
        services_catalog_url("ID", "SC") + "&Language=French",
        f"https://{DOCS}/dol2fe/Pages/SS/Delete.aspx",
        f"https://{GOODS}/member/download/36723",
    ):
        with pytest.raises(ValueError):
            request_parameters(url, definition)
    with pytest.raises(ValueError):
        request_parameters(services_catalog_url("ID", "SC"), {"country": "SG", "item": item()})


def test_precise_protocol_symbols_retain_actual_filename_without_legal_effect_inference():
    url = catalog_url("WT/L/1144")
    body = xml_catalog(symbol="WT/L/1144#WT/MIN(22)/33", filename="q:/WT/MIN22/33.pdf")
    result = inspect(url, body, mime="text/xml")
    assert result["matched"] and len(result["links"]) == 1
    assert result["scope"]["protocol_symbol_source"].endswith("agreement_fisheries_subsidies_e.htm")
    assert result["scope"]["member_bound"] is False
    assert not result["scope"]["legal_status_inferred"]
    assert not inspect(catalog_url("WT/L/9999"), body, mime="text/xml")["matched"]


def test_bundle_is_catalog_evidence_and_cannot_be_a_law_candidate(tmp_path):
    url = f"https://{GOODS}/member/download/36658"
    path = tmp_path / "bundle.zip"
    path.write_bytes(b"PK\x03\x04" + b"x" * (4 * 1024 * 1024))
    fetched = {
        "url": url,
        "mime_type": "application/zip",
        "filename": "bundle.zip",
        "blob": SimpleNamespace(size=path.stat().st_size, path=path),
    }
    recipe = recipe_for(item())
    plan = {"checklist": {"recipe": recipe, "kind": "discovery"}}
    result = ChecklistService.inspect(plan, fetched)
    assert result["matched"] and result["purpose"] == "catalog" and not result["links"]
    assert not result["scope"]["bundle_members_verified"]
    plan["checklist"]["kind"] = "collection"
    with pytest.raises(ValueError, match="目录或详情"):
        ChecklistService.inspect(plan, fetched)


def test_original_headers_reject_html_and_do_not_assert_parser_acceptance():
    for suffix, body in {
        "pdf": b"%PDF-1.7",
        "doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        "xls": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",
        "mdb": b"\x00\x01\x00\x00Standard Jet DB",
        "wpf": b"\xffWPC",
        "docx": b"PK\x03\x04",
    }.items():
        url = f"https://{GOODS}/system/files/2020-06/original.{suffix}"
        result = inspect(url, body, mime="application/octet-stream")
        assert result["matched"] and result["purpose"] == "document"
        assert not result["scope"]["text_complete_verified"]
        assert not inspect(url, b"<html><h1>Access denied</h1></html>")["matched"]


def test_unknown_mdb_requires_the_exact_frozen_source_bytes_and_stays_unparsed(monkeypatch):
    from app.contexts.ingestion.infrastructure.source_adapters import wto_discovery as wto_discovery

    url, known = next(iter(wto_discovery._UNVERIFIED_MDB.items()))
    body = b"\x01\x00\x00\x00unrecognized historical source bytes"
    monkeypatch.setitem(
        wto_discovery._UNVERIFIED_MDB,
        url,
        {**known, "size": len(body), "source_sha256": sha256(body).hexdigest()},
    )
    result = inspect(url, body, mime="application/msaccess")
    assert result["matched"] and result["purpose"] == "document"
    scope = result["scope"]
    assert scope["pending_format_verification"]
    assert not scope["header_signature_verified"] and not scope["format_verified"]
    assert not scope["text_complete_verified"]
    assert scope["original_url"] == url and scope["source_sha256"] == sha256(body).hexdigest()
    assert scope["previous_verified_source_hash"] == known["catalog_sha256"]
    assert not inspect(url, body + b"changed", mime="application/msaccess")["matched"]
    assert not inspect(url, body, member="SG", mime="application/msaccess")["matched"]
    assert not inspect(url, body, mime="text/html")["matched"]
    assert not inspect(url.replace("GMATARRS23", "GMATARRS24"), body, mime="application/msaccess")["matched"]


def test_oversized_xml_cannot_accept_a_complete_prefix_with_a_different_blob_hash(tmp_path):
    path = tmp_path / "catalog.xml"
    path.write_bytes(xml_catalog() + b" " * (4 * 1024 * 1024))
    plan = {"checklist": {"recipe": recipe_for(item()), "kind": "discovery"}}
    fetched = {
        "url": services_catalog_url("ID", "SC"),
        "mime_type": "text/xml",
        "blob": SimpleNamespace(size=path.stat().st_size, path=path),
    }
    with pytest.raises(ValueError, match="4 MiB"):
        ChecklistService.inspect(plan, fetched)


def test_new_adapter_does_not_change_previous_immutable_plan_behavior():
    for member, old_version, current_version in [("ID", 5, 18), ("SG", 2, 7)]:
        current = item(member)
        old = item(member, old_version)
        assert current["verbatim_requirements"] == old["verbatim_requirements"]
        assert not recipe_for(old).get("wto_public_catalog")
        assert recipe_for(current)["wto_public_catalog"]
        api = services_catalog_url(member, "SC")
        assert _Recipe(recipe_for(old)).purpose(api) is None
        assert _Recipe(recipe_for(current)).purpose(api) == "catalog"
        plan_id = "indonesia-20261003" if member == "ID" else "singapore-demo-20261009"
        assert load_blueprints()[plan_id]["version"] == current_version
        source = PROJECT_ROOT / "config" / "collection-plans" / "versions" / f"{plan_id}.v{old_version}.json"
        assert source.exists()


@pytest.mark.parametrize("member,item,version", [("ID", "5.4", 14), ("SG", "5.2", 5)])
@pytest.mark.parametrize(
    "extension,signature", [("doc", bytes.fromhex("d0cf11e0a1b11ae1")), ("docx", b"PK\x03\x04")]
)
def test_generic_office_originals_stay_inside_the_frozen_public_legal_directory(
    member, item, version, extension, signature
):
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import discover_page, recipe_for

    name = "indonesia-20261003" if member == "ID" else "singapore-demo-20261009"
    plan = load_blueprint_version(name, version)
    assert plan["version"] == version
    definition = next(value for value in plan["items"] if value["id"] == item)
    recipe = recipe_for(definition)
    url = f"https://www.wto.org/english/docs_e/legal_e/downloads_e/WTO_en.{extension}"
    result = discover_page(url, signature + b"native bytes", "application/octet-stream", recipe)
    assert result["matched"] and result["purpose"] == "document"
    assert result["scope"]["member"] == member
    assert result["scope"]["text_complete_verified"] is False
    assert not discover_page(url, b"<html>Access denied</html>", "text/html", recipe)["matched"]
    assert not discover_page(
        url.replace("legal_e/downloads_e", "news_e"), signature, "application/octet-stream", recipe
    )["matched"]


def test_generic_direct_legal_pdfs_come_only_from_configured_official_directory_links():
    url = "https://www.wto.org/english/docs_e/legal_e/legal_e.htm"
    result = inspect(
        url,
        b'<html><title>Legal texts</title><a href="marrakesh_decl_e.pdf">Declaration</a><a href="/english/news_e/other.pdf">Unrelated</a></html>',
    )
    assert [entry["url"] for entry in result["links"]] == [
        "https://www.wto.org/english/docs_e/legal_e/marrakesh_decl_e.pdf"
    ]
    source_record = result["links"][0]["source_record"]
    assert source_record["provider"] == "wto"
    assert source_record["fields"]["link_text"] == "Declaration"
    assert source_record["structure"]["file_name"] == "marrakesh_decl_e.pdf"
    assert source_record["structure"]["link_mechanism"] == "official_page_link"
