"""Verified official mirrors retain country, instrument and original-file scope."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from app.discovery import discover_page, normalized_seeds_for, recipe_for

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
COURT = "https://kepaniteraan.mahkamahagung.go.id"
FTA = "https://fta.mofcom.gov.cn"
SECRETARIAT = "https://rcepsec.org/rcep-agreements/"


def definition(item_id, adapter, *urls):
    return {
        "id": item_id,
        "adapter": adapter,
        "execution": "discovery",
        "tags": ["country:ID"],
        "entries": [{"url": url, "purpose": "catalog"} for url in urls],
    }


def page(body):
    return ("<title>Official document catalog</title><h1>Official documents</h1>" + body).encode()


def test_http_200_bank_indonesia_rejection_is_never_an_empty_success():
    source = definition("4.6", "html_links", "https://jdih.bi.go.id/Web/DaftarPeraturan")
    result = discover_page(
        source["entries"][0]["url"],
        page(
            "<div>Akses Ditolak</div><p>The requested URL was rejected. Please consult with your IT Administrator.</p>"
        ),
        "text/html",
        recipe_for(source),
    )
    assert not result["matched"] and not result["links"]
    assert result["reason"]


@pytest.mark.parametrize("item_id,category", [("1.9", 16), ("2.1", 10), ("2.3", 57)])
def test_central_court_fallback_keeps_category_through_pagination_and_articles(item_id, category):
    catalog = f"{COURT}/id/profil-kepaniteraan?id={category}&view=category"
    source = definition(
        item_id,
        "ma_jdih",
        "https://jdih.mahkamahagung.go.id/dokumen?jenis%5B%5D=peraturan-mahkamah-agung",
        catalog,
    )
    other = 10 if category == 16 else 16
    instrument = {"1.9": "PERMA", "2.1": "SEMA", "2.3": "SK KMA"}[item_id]
    result = discover_page(
        catalog,
        page(f"""
        <a href="?id={category}&view=category&start=5">2</a>
        <a href="?id={other}&view=category">Wrong type</a>
        <a href="?view=category&start=5">Lost type</a>
        <a href="?view=article&id=1199:official-law&catid={category}:official">{instrument} official law</a>
        <a href="?view=article&id=1249:other-law&catid={other}">Other law</a>
        """),
        "text/html",
        recipe_for(source),
    )
    assert result["matched"] and result["purpose"] == "catalog"
    assert [x["purpose"] for x in result["links"]] == ["catalog", "detail"]
    assert parse_qs(urlsplit(result["links"][0]["url"]).query)["id"] == [str(category)]
    detail = discover_page(
        result["links"][1]["url"], page("Official metadata"), "text/html", recipe_for(source)
    )
    assert detail["matched"] and detail["purpose"] == "detail"


def test_central_court_perma_files_do_not_expand_to_sema_or_unrelated_images():
    catalog = f"{COURT}/id/profil-kepaniteraan?id=16&view=category"
    source = definition("1.9", "ma_jdih", catalog)
    result = discover_page(
        catalog,
        page("""
        <a href="/images/peraturan/perma/PERMA_02_2015_FIX4.pdf">PERMA 2/2015</a>
        <a href="/images/peraturan/sema/perma%2005%202012.pdf">PERMA 5/2012</a>
        <a href="/images/peraturan/sema/Sema22016.pdf">SEMA 2/2016</a>
        <a href="/images/article/research.pdf">Research</a>
        <a href="https://evil.example/images/peraturan/perma/example.pdf">External</a>
        """),
        "text/html",
        recipe_for(source),
    )
    assert len(result["links"]) == 2
    assert all(x["purpose"] == "document" for x in result["links"])


def test_central_court_sema_does_not_collect_perma_misplaced_inside_sema_file_folder():
    catalog = f"{COURT}/id/profil-kepaniteraan?id=10&view=category"
    source = definition("2.1", "ma_jdih", catalog)
    result = discover_page(
        catalog,
        page("""
        <a href="/images/peraturan/sema/Sema22016.pdf">SEMA 2/2016</a>
        <a href="/images/peraturan/sema/lampiran%20sema4_2014.pdf">Lampiran SEMA 4</a>
        <a href="/images/peraturan/sema/perma%2005%202012.pdf">PERMA 5/2012</a>
        """),
        "text/html",
        recipe_for(source),
    )
    assert len(result["links"]) == 2
    assert all("perma%20" not in x["url"] for x in result["links"])


def test_central_court_perma_catalog_does_not_relabel_guidance_as_perma():
    catalog = f"{COURT}/id/profil-kepaniteraan?id=16&view=category"
    source = definition("1.9", "ma_jdih", catalog)
    result = discover_page(
        catalog,
        page("""
        <a href="?view=article&id=1199:perma-2-2015&catid=16:perma">PERMA 2/2015</a>
        <a href="?view=article&id=838:juklak-sema-1-2014&catid=16:perma">Juklak Sema 1/2014</a>
        <a href="?view=article&id=184:sk-kma-047&catid=16:perma">Keputusan Ketua MA 047</a>
        """),
        "text/html",
        recipe_for(source),
    )
    assert len(result["links"]) == 1 and result["links"][0]["title"] == "PERMA 2/2015"


def test_rcep_secretariat_literal_buttons_find_complete_family_without_other_members():
    source = definition("5.3", "html_links", SECRETARIAT)
    result = discover_page(
        SECRETARIAT,
        (FIXTURES / "recovery-rcep-secretariat.html").read_bytes(),
        "text/html",
        recipe_for(source),
    )
    assert result["matched"] and len(result["links"]) == 45
    paths = [urlsplit(x["url"]).path for x in result["links"]]
    assert any("Chapter-20.pdf" in p for p in paths)
    assert any("Schedule-of-Indonesia-for-China.pdf" in p for p in paths)
    assert any("Indonesias-Schedule-on-TMNP.pdf" in p for p in paths)
    assert not any("Schedule-of-Indonesia-for-Australia.pdf" in p for p in paths)
    assert all(x["purpose"] == "document" for x in result["links"])
    rejected = discover_page(
        SECRETARIAT,
        page("""
        <a onclick="loadPdf(this, 'https://evil.example/legal.pdf')">External</a>
        <a onclick="loadPdf(this, 'https://rcepsec.org/wp-content/uploads/2026/06/Chapter-1.pdf'); alert('x')">Extra code</a>
        <a onclick="loadPdf(this, document.cookie)">Dynamic value</a>
        <a onclick="loadPdf(this, 'https://rcepsec.org/wp-content/uploads/2026/06/Chapter-1.pdf?token=secret')">Query outside scope</a>
        """),
        "text/html",
        recipe_for(source),
    )
    assert rejected["links"] == []


def test_rcep_signatory_mirror_limits_tariffs_to_indonesia_china():
    catalog = FTA + "/channel/rcep.shtml"
    source = definition("5.3", "html_links", catalog)
    result = discover_page(
        catalog, (FIXTURES / "recovery-rcep-mofcom.html").read_bytes(), "text/html", recipe_for(source)
    )
    assert result["matched"] and len(result["links"]) == 45
    paths = [urlsplit(x["url"]).path for x in result["links"]]
    assert any("29%20Schedules%20-%20ID%20for%20CN.pdf" in p for p in paths)
    assert not any("ID%20for%20AU" in p for p in paths)
    assert not any("CN%20for%20ASEAN" in p for p in paths)
    assert not any("yindunixiya.pdf" in p for p in paths)


def test_acfta_mirror_keeps_originals_annexes_and_indonesia_schedules():
    catalog = FTA + "/channel/china_dongmeng_special.shtml"
    source = definition("5.2", "html_links", catalog)
    result = discover_page(
        catalog, (FIXTURES / "recovery-acfta-mofcom.html").read_bytes(), "text/html", recipe_for(source)
    )
    assert result["matched"] and len(result["links"]) > 35
    paths = [urlsplit(x["url"]).path for x in result["links"]]
    for expected in (
        "xieyizw_en.pdf",
        "hwmyxieyi02-FR3_cn.pdf",
        "fwmyxieyi_fj-2rb-Indonesiacrb_en.pdf",
        "yds3_fl14_en",
    ):
        assert any(p.endswith(expected) for p in paths)
    assert not any("Vietnamcrb" in p or "Ching-Thailand" in p or "Chinacrb" in p for p in paths)
    assert all(x["purpose"] == "document" for x in result["links"])


def services_definition():
    source = definition(
        "5.4",
        "html_links",
        "https://www.wto.org/english/tratop_e/serv_e/telecom_e/telecom_commit_exempt_list_e.htm",
    )
    source["trusted_hosts"] = ["docs.wto.org"]
    return source


def test_wto_services_candidates_preserve_indonesia_and_normalize_published_legacy_link():
    source = services_definition()
    result = discover_page(
        source["entries"][0]["url"],
        page("""
        <a href="sc42.pdf">GATS/SC/42</a><a href="sc43.pdf">GATS/SC/43</a>
        <a href="http://docsonline.wto.org/imrd/directdoc.asp?DDFDocuments/t/SCHD/GATS-SC/sc43s2.wpf">GATS/SC/43/Suppl.2</a>
        <a href="http://docsonline.wto.org/imrd/directdoc.asp?DDFDocuments/t/SCHD/GATS-SC/sc42s2.wpf">GATS/SC/42/Suppl.2</a>
        """),
        "text/html",
        recipe_for(source),
    )
    assert len(result["links"]) == 2 and all(x["purpose"] == "document" for x in result["links"])
    assert result["links"][0]["url"].endswith("/sc43.pdf")
    assert result["links"][1]["url"].startswith("https://docs.wto.org/dol2fe/Pages/SS/directdoc2.aspx?")
    assert parse_qs(urlsplit(result["links"][1]["url"]).query)["filename"] == ["Q:/SCHD/GATS-SC/SC43S2.pdf"]


def test_wto_legacy_conversion_does_not_repair_untrusted_authority_or_protocol():
    source = services_definition()
    result = discover_page(
        source["entries"][0]["url"],
        page("""
        <a href="http://name:password@docsonline.wto.org/imrd/directdoc.asp?DDFDocuments/t/SCHD/GATS-SC/sc43s2.wpf">Credentials</a>
        <a href="http://docsonline.wto.org:444/imrd/directdoc.asp?DDFDocuments/t/SCHD/GATS-SC/sc43s2.wpf">Wrong port</a>
        <a href="javascript://docsonline.wto.org/imrd/directdoc.asp?DDFDocuments/t/SCHD/GATS-SC/sc43s2.wpf">Other protocol</a>
        """),
        "text/html",
        recipe_for(source),
    )
    assert result["links"] == []


def test_wto_wrapper_reads_fixed_literal_navigation_without_executing_script():
    source = services_definition()
    source["entries"].append(
        {
            "url": "https://docs.wto.org/dol2fe/Pages/SS/directdoc.aspx?filename=Q:/SCHD/GATS-SC/SC43.pdf&Open=True",
            "purpose": "detail",
        }
    )
    url = source["entries"][-1]["url"]
    rules = recipe_for(source)
    result = discover_page(
        url,
        b"<html><script>window.location.href='directdoc2.aspx?filename=Q%3a%2fSCHD%2fGATS-SC%2fSC43.pdf&Open=True';</script></html>",
        "text/html",
        rules,
    )
    assert result["matched"] and result["purpose"] == "detail"
    assert len(result["links"]) == 1 and result["links"][0]["purpose"] == "document"
    assert discover_page(result["links"][0]["url"], b"%PDF-1.7\n%%EOF", "application/pdf", rules)["matched"]
    switched = discover_page(
        url,
        b"<html><script>window.location.href='directdoc2.aspx?filename=Q:/SCHD/GATS-SC/SC99.pdf&Open=True';</script></html>",
        "text/html",
        rules,
    )
    assert not switched["matched"] and not switched["links"]
    other_country = discover_page(url.replace("SC43", "SC430"), b"%PDF-1.7", "application/pdf", rules)
    assert not other_country["matched"]


def test_plan_v3_preserves_previous_snapshot_and_declares_real_mirrors():
    directory = Path(__file__).resolve().parents[2] / "config" / "collection-plans"
    current = json.loads((directory / "indonesia-20261003.json").read_text())
    assert current["version"] == 3
    previous = json.loads((directory / "versions" / "indonesia-20261003.v2.json").read_text())
    assert previous["version"] == 2 and previous["id"] == current["id"]
    assert len(current["items"]) == 29 and current["country"] == "ID"
    previous_by_id = {x["id"]: x for x in previous["items"]}
    for source in current["items"]:
        old = previous_by_id[source["id"]]
        assert source["scope_include"] == old["scope_include"]
        assert source["scope_exclude"] == old["scope_exclude"]
        assert all(x in source["entries"] for x in old["entries"])
    rcep = next(x for x in current["items"] if x["id"] == "5.3")
    seeds = normalized_seeds_for(rcep)
    assert any(x["url"] == SECRETARIAT for x in seeds)
    assert any(x["url"] == FTA + "/channel/rcep.shtml" for x in seeds)
