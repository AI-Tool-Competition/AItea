"""Discovery only returns bounded, reviewable candidates; it never approves laws."""

from pathlib import Path
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from app.discovery import discover_page, normalized_seeds_for, recipe_for

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"

SETNEG = "https://jdih.setneg.go.id/api/hukumproduk/"


def setneg_item():
    return item("ID", "1.3", "bpk", (SETNEG + "produkhukum?jns=UU&length=5&start=0", "catalog"))


def test_setneg_catalog_uses_verified_type_total_and_replayable_next_page():
    definition = setneg_item()
    result = discover_page(
        definition["entries"][0]["url"],
        (FIXTURES / "setneg-catalog.json").read_bytes(),
        "application/json",
        recipe_for(definition),
    )
    assert result["matched"] and result["purpose"] == "catalog"
    assert len(result["links"]) == 6
    assert all(link["purpose"] == "detail" for link in result["links"][:5])
    record = result["links"][0]["source_record"]
    assert record["provider"] == "setneg"
    assert record["interpretation"] == "unreviewed_source_values"
    assert record["fields"] == {
        key: value
        for key, value in json.loads((FIXTURES / "setneg-catalog.json").read_text())["data"][0].items()
        if key
        in {
            "idperaturan",
            "jns",
            "nama_jenis",
            "no_peraturan",
            "tahun",
            "tentang",
            "tgl_di",
            "diundangkan",
            "status_hukum",
        }
    }
    assert result["links"][-1]["purpose"] == "catalog"
    assert parse_qs(urlsplit(result["links"][-1]["url"]).query) == {
        "jns": ["UU"],
        "length": ["5"],
        "start": ["5"],
    }
    assert result["scope"] == {
        "provider": "setneg",
        "jns": "UU",
        "total": 901,
        "offset": 0,
        "page_size": 5,
        "returned": 5,
    }
    last = discover_page(
        SETNEG + "produkhukum?jns=UU&length=5&start=900",
        (FIXTURES / "setneg-last-page.json").read_bytes(),
        "application/json",
        recipe_for(definition),
    )
    assert last["matched"] and len(last["links"]) == 1
    assert last["links"][0]["purpose"] == "detail"
    assert last["scope"]["returned"] == 1


def test_setneg_request_body_is_derived_only_from_trusted_item_and_fixed_api():
    from app.discovery import request_parameters

    definition = {"country": "ID", "item": setneg_item()}
    params = request_parameters(SETNEG + "produkhukum?jns=UU&length=5&start=5", definition)
    assert params == {
        "json_body": {
            "tentang": "",
            "p_lihan": "semua",
            "jns": ["UU"],
            "thn": [],
            "status": "",
            "terx": "All",
            "sortOrder": "desc",
            "length": 5,
            "start": 5,
        },
        "accept_compressed_json": True,
    }
    assert request_parameters(SETNEG + "detaildata?jns=UU&no=2&thn=2026", definition)["json_body"] == {
        "jns": "UU",
        "no": "2",
        "thn": "2026",
        "k": "",
    }
    assert request_parameters("https://peraturan.bpk.go.id/Search?jenis=8", definition) == {}
    for url in (
        SETNEG + "produkhukum?jns=PP&length=5&start=0",
        SETNEG + "produkhukum?jns=UU&length=500&start=0",
        SETNEG + "produkhukum?jns=UU&length=5&start=-1",
        SETNEG + "produkhukum?jns=UU&length=5&start=0&start=5",
        SETNEG + "delete?jns=UU",
    ):
        with pytest.raises(ValueError):
            request_parameters(url, definition)
    with pytest.raises(ValueError):
        request_parameters(
            SETNEG + "produkhukum?jns=UU&length=5&start=0", {"country": "SG", "item": setneg_item()}
        )


def test_setneg_detail_discovers_originals_without_abstracts_or_fulltext_claims():
    recipe = recipe_for(setneg_item())
    result = discover_page(
        SETNEG + "detaildata?jns=UU&no=2&thn=2026",
        (FIXTURES / "setneg-detail.json").read_bytes(),
        "application/json",
        recipe,
    )
    assert result["matched"] and result["purpose"] == "detail"
    assert len(result["links"]) == 1
    link = result["links"][0]
    assert link["purpose"] == "document" and link["identity_key"].startswith("setneg:P20560:")
    assert link["source_record"]["provider"] == "setneg"
    assert link["source_record"]["fields"]["teu"] == "Kementerian Sekretariat Negara"
    assert link["source_record"]["fields"]["diundangkan"] == "2026-04-30T00:00:00.000Z"
    assert link["source_record"]["fields"]["status"] == "Disahkan"
    assert link["source_record"]["file"] == {
        "realName": "Salinan UU Nomor 2 Tahun 2026.pdf",
        "size": 1733281,
        "ext": ".pdf",
    }
    assert parse_qs(urlsplit(link["url"]).query) == {
        "f": ["Salinan UU Nomor 2 Tahun 2026.pdf"],
        "fl": ["P20560"],
        "l": ["uploads"],
    }
    assert discover_page(link["url"], b"%PDF-1.4\n%%EOF", "application/pdf", recipe)["matched"]
    assert not discover_page(
        link["url"].replace("l=uploads", "l=Abstrak"), b"%PDF-1.4\n%%EOF", "application/pdf", recipe
    )["matched"]
    multi = discover_page(
        SETNEG + "detaildata?jns=UU&no=19&thn=2025",
        (FIXTURES / "setneg-files.json").read_bytes(),
        "application/json",
        recipe,
    )
    assert len(multi["links"]) == 2
    assert len({x["identity_key"] for x in multi["links"]}) == 2


@pytest.mark.parametrize(
    "change", ["wrong_type", "missing_rows", "duplicate_ids", "bool_total", "error", "malformed"]
)
def test_setneg_invalid_or_incomplete_json_is_not_a_matched_catalog(change):
    body = json.loads((FIXTURES / "setneg-catalog.json").read_bytes())
    if change == "wrong_type":
        body["data"][0]["jns"] = "PP"
    elif change == "missing_rows":
        body["data"].pop()
    elif change == "duplicate_ids":
        body["data"][1]["idperaturan"] = body["data"][0]["idperaturan"]
    elif change == "bool_total":
        body["jml"] = True
    elif change == "error":
        body = {"error": "login required"}
    content = b"{bad json" if change == "malformed" else json.dumps(body).encode()
    result = discover_page(
        SETNEG + "produkhukum?jns=UU&length=5&start=0", content, "application/json", recipe_for(setneg_item())
    )
    assert not result["matched"] and result["links"] == []


def test_setneg_rejects_filename_paths_wrong_detail_and_json_size():
    recipe = recipe_for(setneg_item())
    body = json.loads((FIXTURES / "setneg-detail.json").read_bytes())
    body["file"][0]["realName"] = "../../secret.pdf"
    result = discover_page(
        SETNEG + "detaildata?jns=UU&no=2&thn=2026", json.dumps(body).encode(), "application/json", recipe
    )
    assert not result["links"] and result["warnings"]
    wrong = discover_page(
        SETNEG + "detaildata?jns=UU&no=3&thn=2026",
        (FIXTURES / "setneg-detail.json").read_bytes(),
        "application/json",
        recipe,
    )
    assert not wrong["matched"]
    oversized = discover_page(
        SETNEG + "produkhukum?jns=UU&length=5&start=0",
        b" " * (4 * 1024 * 1024 + 1),
        "application/json",
        recipe,
    )
    assert not oversized["matched"]


def test_explicit_official_document_seed_remains_a_document_not_a_catalog():
    url = "https://s.mkri.id/public/content/infoumum/regulation/pdf/UUD%20NRI%2045%20.pdf"
    definition = item(
        "ID", "1.1", "html_links", ("https://www.mkri.id/peraturan/uud", "catalog"), (url, "document")
    )
    result = discover_page(url, b"%PDF-1.4\n%%EOF", "application/pdf", recipe_for(definition))
    assert result["matched"] and result["purpose"] == "document"
    assert not discover_page(url, html("Official menu only"), "text/html", recipe_for(definition))["matched"]


def html(body, title="Official documents"):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>".encode()


def recipe(**overrides):
    return {
        "adapter": "html_links",
        "allowed_hosts": ["laws.example.org"],
        "catalog_patterns": [r"^/catalog(?:\?|$)"],
        "detail_patterns": [r"^/laws/[^/?]+(?:\?|$)"],
        "document_patterns": [r"^/files/[^?]+\.pdf(?:\?|$)"],
        "html_document_patterns": [],
        "exclude_patterns": [],
        "include_terms": [],
        "exclude_terms": [],
        "required_query": {},
        "max_links": 200,
        **overrides,
    }


def item(country, item_id, adapter, *entries, execution="discovery"):
    return {
        "id": item_id,
        "adapter": adapter,
        "execution": execution,
        "tags": [f"country:{country}", f"checklist:{country}-{item_id}"],
        "entries": [{"url": url, "purpose": purpose} for url, purpose in entries],
    }


def test_catalog_pagination_retains_filters_and_canonicalizes_duplicates():
    result = discover_page(
        "https://laws.example.org/catalog?type=act&page=1",
        html("""<a href="?page=2&amp;type=act">2</a>
          <a href="?type=act&amp;page=2#top">Next</a>
          <a href="?page=3">Lost filter</a><a href="?page=3&amp;type=other">Wrong type</a>
          <a href="/laws/tea#section-1">Tea Act</a><a href="#top">Top</a>"""),
        "text/html; charset=UTF-8",
        recipe(required_query={"type": "act"}),
    )
    assert result["matched"] and result["purpose"] == "catalog"
    assert [(x["url"], x["purpose"]) for x in result["links"]] == [
        ("https://laws.example.org/catalog?page=2&type=act", "catalog"),
        ("https://laws.example.org/laws/tea", "detail"),
    ]
    assert result["links"][1]["title"] == "Tea Act"


def test_html5_tree_recovers_nested_labels_and_ignores_hidden_markup():
    content = b"""<!doctype html><html><head><title>Official directory</title></head>
    <body><main><a href="/laws/tea">Tea <span>Act</span>
    <template><a href="/laws/hidden">Hidden Act</a></template>
    <noscript><a href="/laws/fallback">Fallback Act</a></noscript>
    <script>const sample = '<a href=\"/laws/script\">Script Act</a>';</script>
    <p>Official directory for the current legislation and its published instruments.
    </main></body></html>"""
    result = discover_page("https://laws.example.org/catalog", content, "text/html", recipe())
    assert result["matched"] and result["purpose"] == "catalog"
    assert [link["url"] for link in result["links"]] == ["https://laws.example.org/laws/tea"]
    assert result["links"][0]["title"] == "Tea Act"


def test_duplicate_sensitive_html_attributes_are_rejected_before_link_discovery():
    result = discover_page(
        "https://laws.example.org/catalog",
        html('<a href="/laws/tea" href="https://evil.example/">Tea Act</a>'),
        "text/html",
        recipe(),
    )
    assert result["matched"] and result["links"] == []


def test_rcep_literal_pdf_links_keep_source_hierarchy_and_filename():
    source = "https://rcepsec.org/rcep-agreements/"
    content = b"""<html><title>RCEP Agreements</title><body>
    <div class="rcep-sidebar"><ul class="rcep-list">
      <li class="rcep-item"><a class="rcep-link" onclick="loadPdf(this, 'https://rcepsec.org/wp-content/uploads/2026/06/Chapter-3.pdf')">Chapter 3: Rules of Origin</a>
        <ul class="rcep-sub-list"><li><a class="rcep-link rcep-sub-link" onclick="loadPdf(this, 'https://rcepsec.org/wp-content/uploads/2026/06/Chapter-3-Annex-3A.pdf')">Annex 3A: Product-Specific Rules</a></li></ul>
      </li>
    </ul></div></body></html>"""
    rules = recipe(
        allowed_hosts=["rcepsec.org"],
        catalog_patterns=[r"^/rcep-agreements/$"],
        document_patterns=[r"^/wp-content/uploads/2026/06/.*\.pdf$"],
    )
    result = discover_page(source, content, "text/html", rules)
    assert result["matched"] and result["purpose"] == "catalog"
    annex = next(link for link in result["links"] if link["title"].startswith("Annex 3A"))
    assert annex["source_record"]["provider"] == "rcepsec"
    assert annex["source_record"]["fields"]["link_text"] == "Annex 3A: Product-Specific Rules"
    assert annex["source_record"]["structure"] == {
        "unit_type": "annex",
        "number": "3A",
        "parent_titles": ["Chapter 3: Rules of Origin"],
        "file_name": "Chapter-3-Annex-3A.pdf",
        "anchor_class": "rcep-link rcep-sub-link",
        "link_mechanism": "verified_literal_pdf_call",
    }


def test_ipos_html_record_uses_table_date_description_and_section_heading():
    source = "https://www.ipos.gov.sg/about-ip/trade-marks/circulars-and-practice-directions/"
    content = b"""<html><title>IPOS Circulars</title><body><h2>Circulars</h2>
    <h3>2026 Circulars</h3><table><tbody><tr>
    <td>25 September 2026</td><td><a href="https://file.go.gov.sg/tm-circular-no-4-of-2026.pdf">Circular No. 4 of 2026 [PDF, 300KB] (opens in new tab)</a></td>
    <td>International Classification of Goods and Services - Nice Classification, Version 2027.</td>
    </tr></tbody></table></body></html>"""
    rules = recipe(
        allowed_hosts=["www.ipos.gov.sg", "file.go.gov.sg"],
        catalog_patterns=[r"^/about-ip/trade-marks/circulars-and-practice-directions/$"],
        document_patterns=[r"^/tm-circular-no-\d+-of-\d{4}\.pdf$"],
    )
    result = discover_page(source, content, "text/html", rules)
    assert result["matched"] and result["purpose"] == "catalog"
    candidate = result["links"][0]
    record = candidate["source_record"]
    assert record["provider"] == "ipos"
    assert record["fields"]["publication_date"] == "25 September 2026"
    assert record["fields"]["row_description"].startswith("International Classification")
    assert record["structure"] == {
        "unit_type": "circular",
        "number": "4 of 2026",
        "parent_titles": ["2026 Circulars"],
        "file_name": "tm-circular-no-4-of-2026.pdf",
        "anchor_class": "",
        "link_mechanism": "href",
    }


def test_ipos_practice_direction_number_keeps_year_when_title_has_legacy_punctuation():
    source = "https://www.ipos.gov.sg/about-ip/trade-marks/circulars-and-practice-directions/"
    content = b"""<html><body><h2>Practice directions</h2><a href="https://file.go.gov.sg/tm-practice-direction-no-1-of-2007.pdf">Practice Direction No 1. of 2007 [PDF]</a></body></html>"""
    rules = recipe(
        allowed_hosts=["www.ipos.gov.sg", "file.go.gov.sg"],
        catalog_patterns=[r"^/about-ip/trade-marks/circulars-and-practice-directions/$"],
        document_patterns=[r"^/tm-practice-direction-no-1-of-2007\.pdf$"],
    )
    result = discover_page(source, content, "text/html", rules)
    assert result["links"][0]["source_record"]["structure"]["number"] == "1 of 2007"


@pytest.mark.parametrize(
    "target",
    [
        "http://laws.example.org/laws/tea",
        "javascript:alert(1)",
        "data:text/html,abc",
        "https://laws.example.org.evil.test/laws/tea",
        "https://child.laws.example.org/laws/tea",
        "https://user@laws.example.org/laws/tea",
        "https://laws.example.org:444/laws/tea",
        "https://127.0.0.1/laws/tea",
        "//evil.test/laws/tea",
        "/laws/%2e%2e/private",
        "/laws/%252e%252e/private",
        "/laws/tea%00.pdf",
        "/laws/tea\\private",
    ],
)
def test_links_obey_exact_host_https_and_safe_paths(target):
    result = discover_page(
        "https://laws.example.org/catalog",
        html(f'<a href="{target}">Tea</a>'),
        "text/html",
        recipe(),
    )
    assert result["links"] == []


def test_cross_domain_documents_require_explicit_allowlist():
    content = html('<a href="https://files.example.org/files/tea.pdf">Tea Act</a>')
    assert discover_page("https://laws.example.org/catalog", content, "text/html", recipe())["links"] == []
    result = discover_page(
        "https://laws.example.org/catalog",
        content,
        "text/html",
        recipe(allowed_hosts=["laws.example.org", "files.example.org"]),
    )
    assert result["links"][0]["purpose"] == "document"


def test_ignores_html_base_script_and_unapproved_navigation():
    content = html("""<base href="https://evil.test/"><script>
      <a href="/laws/forged">Forged</a></script><nav><a href="/contact">Contact</a></nav>
      <a href="/laws/tea">Tea Act</a>""")
    result = discover_page("https://laws.example.org/catalog", content, "text/html", recipe())
    assert [x["url"] for x in result["links"]] == ["https://laws.example.org/laws/tea"]


def test_topic_filters_do_not_drop_pagination_or_follow_unrelated_documents():
    content = html("""<a href="/catalog?page=2">Next</a><a href="/laws/gst">GST import guide</a>
      <a href="/laws/personal-tax">Personal tax guide</a><a href="/laws/property">Property GST</a>
      <a href="/files/gst.pdf">GST guide</a><a href="/laws/example-gst">Example GST</a>""")
    result = discover_page(
        "https://laws.example.org/catalog",
        content,
        "text/html",
        recipe(include_terms=["GST"], exclude_terms=["property"], exclude_patterns=["example-"]),
    )
    assert [x["purpose"] for x in result["links"]] == ["catalog", "detail", "document"]


@pytest.mark.parametrize(
    "body,title",
    [
        ("Checking your browser before accessing this site", "Just a moment..."),
        ('<div id="challenge-running">Please verify you are human</div>', "Official documents"),
        ('<form action="/login"><input type="password"></form>', "Sign in"),
        ("You do not have permission to access this resource", "403 Forbidden"),
        ("Please try again later", "Service Unavailable"),
        ("The page you requested could not be found", "Page not found"),
    ],
)
def test_http_200_challenges_errors_and_login_pages_are_not_documents(body, title):
    result = discover_page(
        "https://laws.example.org/laws/tea",
        html(body + '<a href="/files/tea.pdf">PDF</a>', title),
        "text/html",
        recipe(html_document_patterns=[r"^/laws/"]),
    )
    assert result["matched"] is False
    assert result["links"] == [] and result["purpose"] is None and result["reason"]


def test_official_body_with_subscription_verification_is_not_a_challenge():
    result = discover_page(
        "https://laws.example.org/laws/tea",
        html("""<h1>Tea Act</h1><main>
          Rules for sale of tea, labels and quality. These rules apply to every importer.</main>
          <form>verifikasi terlebih dahulu <div class="g-recaptcha"></div></form>
          <a href="/files/tea.pdf">Official PDF</a>"""),
        "text/html",
        recipe(),
    )
    assert result["matched"] and result["purpose"] == "detail"


def test_catalogs_are_not_body_documents_even_with_legal_text():
    result = discover_page(
        "https://laws.example.org/catalog",
        html("<main><h1>Tea Act</h1>Section 1</main>"),
        "text/html",
        recipe(html_document_patterns=[r"^/laws/"]),
    )
    assert result["purpose"] == "catalog" and result["matched"]
    assert result["warnings"]


def test_html_body_requires_explicit_body_path_not_pdf_link_suffix():
    error_html = html("<h1>Document viewer</h1><a href='/files/real.pdf'>Download</a>")
    result = discover_page("https://laws.example.org/files/fake.pdf", error_html, "text/html", recipe())
    assert result["purpose"] is None and not result["matched"]


def test_pdf_candidate_requires_pdf_signature_and_scoped_document_url():
    url = "https://laws.example.org/files/tea.pdf"
    assert discover_page(url, b"%PDF-1.7\n%%EOF", "application/pdf", recipe())["purpose"] == "document"
    assert not discover_page(url, b"this is not a PDF", "application/pdf", recipe())["matched"]
    assert not discover_page("https://laws.example.org/other.pdf", b"%PDF-1.7", "application/pdf", recipe())[
        "matched"
    ]


def test_csv_and_unknown_binary_are_never_generic_law_documents():
    r = recipe(document_patterns=[r"^/files/"])
    assert not discover_page(
        "https://laws.example.org/files/cases.csv", b"Paragraph,Cited Case\n", "text/csv", r
    )["matched"]
    assert not discover_page(
        "https://laws.example.org/files/data.bin", b"\x00\xff", "application/octet-stream", r
    )["matched"]


def test_link_count_and_html_byte_budgets_are_visible():
    content = html("".join(f'<a href="/laws/{x}">Act {x}</a>' for x in range(8)))
    result = discover_page("https://laws.example.org/catalog", content, "text/html", recipe(max_links=3))
    assert len(result["links"]) == 3 and result["warnings"]
    result = discover_page(
        "https://laws.example.org/catalog", b" " * (4 * 1024 * 1024 + 1), "text/html", recipe()
    )
    assert not result["matched"] and result["reason"]


def test_unconfigured_page_is_not_used_as_a_new_discovery_root():
    result = discover_page(
        "https://laws.example.org/news",
        html('<a href="/laws/tea">Tea</a>'),
        "text/html",
        recipe(),
    )
    assert not result["matched"] and not result["links"]


def test_bpk_and_ma_downloads_and_filter_pages():
    bpk = item("ID", "1.3", "bpk", ("https://peraturan.bpk.go.id/Search?jenis=8", "catalog"))
    result = discover_page(
        bpk["entries"][0]["url"],
        html("""<h3><a href="/Details/123/uu-no-1-tahun-2026">Undang-Undang 1</a></h3>
          <a href="/Download/42/UU%20Nomor%201.pdf">UU Nomor 1.pdf</a>
          <a href="/Search?jenis=8&amp;p=2">Next</a><a href="/Search?jenis=9&amp;p=2">Other</a>"""),
        "text/html",
        recipe_for(bpk),
    )
    assert {x["purpose"] for x in result["links"]} == {"catalog", "detail", "document"}
    assert len(result["links"]) == 3
    ma = item(
        "ID",
        "2.1",
        "ma_jdih",
        (
            "https://jdih.mahkamahagung.go.id/dokumen?jenis%5B0%5D=surat-edaran-mahkamah-agung&page=1",
            "catalog",
        ),
    )
    result = discover_page(
        "https://jdih.mahkamahagung.go.id/legal-product/sema-nomor-4-tahun-2026/detail",
        html('<a href="/legal-product/sema-nomor-4-tahun-2026/download/dokumen">2026sema004.pdf</a>'),
        "text/html",
        recipe_for(ma),
    )
    assert result["purpose"] == "detail" and result["links"][0]["purpose"] == "document"


def test_bpk_historical_home_download_path_remains_a_document_candidate():
    bpk = item("ID", "1.3", "bpk", ("https://peraturan.bpk.go.id/Search?jenis=8", "catalog"))
    result = discover_page(
        "https://peraturan.bpk.go.id/Details/123/uu-no-1-tahun-2019",
        html('<a href="/Home/Download/123/UU%201%202019.pdf">UU 1 2019.pdf</a>'),
        "text/html",
        recipe_for(bpk),
    )
    assert len(result["links"]) == 1 and result["links"][0]["purpose"] == "document"


def test_sso_full_body_links_preserve_versions_but_remove_provision_focus():
    sso = item("SG", "1.3", "sso", ("https://sso.agc.gov.sg/Browse/SL/Current", "catalog"))
    result = discover_page(
        sso["entries"][0]["url"],
        html("""<a href="/SL/SFA1973-RG1?ProvIds=pr153-&amp;DocDate=20250131">Food Regulations</a>
          <a href="/SL/SFA1973-RG1?DocDate=20250131#Sc4-">Food Regulations</a>
          <a href="/SL/SFA1973-RG1?DocDate=20200101">Historical</a>
          <a href="/Browse/SL/Current?PageIndex=1">2</a>
          <a href="/Browse/Act/Current">Other type</a>"""),
        "text/html",
        recipe_for(sso),
    )
    assert len(result["links"]) == 3
    docs = [x for x in result["links"] if x["purpose"] == "document"]
    assert len(docs) == 2 and docs[0]["identity_key"] == docs[1]["identity_key"]
    assert all("ProvIds" not in x["url"] for x in docs)


def test_sso_full_html_and_uncommenced_paths_keep_current_distinction():
    sso = item("SG", "1.2", "sso", ("https://sso.agc.gov.sg/Browse/Act/Current", "catalog"))
    r = recipe_for(sso)
    current = discover_page(
        "https://sso.agc.gov.sg/Act/SFA1973",
        html("<h1>Sale of Food Act</h1><main>1. Citation</main>"),
        "text/html",
        r,
    )
    assert current["matched"] and current["purpose"] == "document"
    future = discover_page(
        "https://sso.agc.gov.sg/Act/FA2025/Uncommenced/20261101", html("Future text"), "text/html", r
    )
    assert future["purpose"] != "document"


def test_tap_seeds_use_all_three_search_terms_and_exclude_reference_examples():
    tap = item(
        "ID",
        "1.2",
        "bpk",
        ("https://peraturan.bpk.go.id/Search", "catalog"),
        ("https://peraturan.bpk.go.id/Details/123/example", "reference"),
    )
    seeds = normalized_seeds_for(tap)
    assert len(seeds) == 3
    assert {parse_qs(urlsplit(x["url"]).query)["keywords"][0] for x in seeds} == {
        "Ketetapan MPR",
        "TAP MPR",
        "TAP MPRS",
    }
    assert all(x["purpose"] == "catalog" for x in seeds)


def test_bpk_local_regulation_and_governor_branches_do_not_overlap():
    common = ("https://peraturan.bpk.go.id/Jenis/3", "catalog")
    content = html("""<a href="/Search?jenis=22">Peraturan Gubernur (Pergub)</a>
      <a href="/Search?jenis=25">Peraturan Bupati (Perbup)</a>
      <a href="/Search?jenis=26">Peraturan Walikota (Perwali)</a>
      <a href="/Search?jenis=19">Peraturan Daerah (Perda)</a>
      <a href="/Search?jenis=30">Peraturan Desa (Perdes)</a>""")
    local = discover_page(common[0], content, "text/html", recipe_for(item("ID", "1.7", "bpk", common)))
    executive = discover_page(common[0], content, "text/html", recipe_for(item("ID", "1.10", "bpk", common)))
    assert len(local["links"]) == 1 and "Perda" in local["links"][0]["title"]
    assert len(executive["links"]) == 3


def test_bpk_central_branch_only_discovers_formal_regulation_categories():
    central = item("ID", "1.8", "bpk", ("https://peraturan.bpk.go.id/Jenis/2", "catalog"))
    result = discover_page(
        central["entries"][0]["url"],
        html("""
      <a href="/Search?jenis=20">Peraturan Menteri</a>
      <a href="/Search?jenis=21">Keputusan Menteri</a>
      <a href="/Search?jenis=22">Surat Edaran Menteri</a>"""),
        "text/html",
        recipe_for(central),
    )
    assert len(result["links"]) == 1 and result["links"][0]["title"] == "Peraturan Menteri"


def test_verified_download_host_and_exact_catalog_are_preserved():
    constitution = item("ID", "1.1", "html_links", ("https://www.mkri.id/peraturan/peraturan-uud", "catalog"))
    constitution["trusted_hosts"] = ["s.mkri.id"]
    result = discover_page(
        constitution["entries"][0]["url"],
        html("""
      <a href="/peraturan/peraturan-ketua-mk">Other regulations</a>
      <a href="https://s.mkri.id/public/content/infoumum/regulation/pdf/UUD1945.pdf">UUD 1945</a>
      """),
        "text/html",
        recipe_for(constitution),
    )
    assert result["purpose"] == "catalog"
    assert len(result["links"]) == 1 and result["links"][0]["purpose"] == "document"


def test_badilum_normalizes_first_page_and_manual_dataset_has_no_auto_import():
    badilum = item(
        "ID",
        "2.4",
        "html_links",
        (
            "https://badilum.mahkamahagung.go.id/publik/peraturan/kebijakan-dirjen-badilum.html?start=20",
            "catalog",
        ),
    )
    seeds = normalized_seeds_for(badilum)
    assert parse_qs(urlsplit(seeds[0]["url"]).query).get("start", ["0"]) == ["0"]
    dataset = item(
        "SG",
        "2.1",
        "manual",
        ("https://huggingface.co/datasets/ShannonLeeYuehErn/SG-LegalCite", "catalog"),
        execution="dataset_review",
    )
    result = discover_page(dataset["entries"][0]["url"], html("CSV"), "text/html", recipe_for(dataset))
    assert not result["matched"] and not result["links"] and result["reason"]


def test_generic_seed_homepage_does_not_allow_arbitrary_site_navigation():
    sfa = item("SG", "3.2", "html_links", ("https://www.sfa.gov.sg/", "catalog"))
    result = discover_page(
        sfa["entries"][0]["url"],
        html("""<a href="/news-events/news">News</a><a href="/careers">Careers</a>
          <a href="/food-import-export/commercial-food-imports">Commercial food imports</a>"""),
        "text/html",
        recipe_for(sfa),
    )
    assert len(result["links"]) == 1 and "/food-import-export/" in result["links"][0]["url"]


def test_malformed_trusted_recipe_fails_closed():
    for r in [recipe(catalog_patterns=["["]), recipe(max_links=-1), recipe(allowed_hosts=["*.example.org"])]:
        with pytest.raises(ValueError):
            discover_page("https://laws.example.org/catalog", html("text"), "text/html", r)


def test_ma_judgment_category_pagination_is_catalog_and_detail_is_not_body():
    hum = item(
        "ID",
        "3.3",
        "html_links",
        ("https://putusan3.mahkamahagung.go.id/direktori/index/kategori/hak-uji-materiil-1.html", "catalog"),
    )
    r = recipe_for(hum)
    pagination = (
        "https://putusan3.mahkamahagung.go.id/direktori/index/kategori/hak-uji-materiil-1/page/2.html"
    )
    case = "https://putusan3.mahkamahagung.go.id/direktori/putusan/abcdef123456.html"
    content = html(
        f'<a href="{pagination}">2</a><a href="{case}">Putusan 1 HUM 2026</a><a href="/direktori/index/kategori/pidana.html">Other cases</a>'
    )
    result = discover_page(hum["entries"][0]["url"], content, "text/html", r)
    assert result["purpose"] == "catalog"
    assert [(x["url"], x["purpose"]) for x in result["links"]] == [(pagination, "catalog"), (case, "detail")]
    assert discover_page(pagination, content, "text/html", r)["purpose"] == "catalog"
    detail = discover_page(
        case,
        html(
            '<h1>Case detail</h1><p>Number, year, parties and summary</p><a href="/direktori/download_file/abc/123">PDF</a>'
        ),
        "text/html",
        r,
    )
    assert detail["purpose"] == "detail"
    assert detail["links"][0]["purpose"] == "document"


def test_unverified_generic_html_requires_detail_review_instead_of_body_candidate():
    ministry = item("ID", "4.2", "html_links", ("https://jdih.kemendag.go.id/peraturan", "catalog"))
    r = recipe_for(ministry)
    result = discover_page(
        ministry["entries"][0]["url"],
        html('<a href="/peraturan/tea-import">Peraturan Import</a>'),
        "text/html",
        r,
    )
    assert result["links"][0]["purpose"] == "detail"
    detail = discover_page(
        "https://jdih.kemendag.go.id/peraturan/tea-import",
        html("<h1>Official title</h1><p>This could be a summary, not the complete law.</p>"),
        "text/html",
        r,
    )
    assert detail["purpose"] == "detail" and detail["warnings"]
    assert r["html_document_patterns"] == []


def test_badilum_real_attachment_links_encode_filename_spaces_and_deduplicate():
    source = item(
        "ID",
        "2.4",
        "html_links",
        ("https://badilum.mahkamahagung.go.id/publik/peraturan/kebijakan-dirjen-badilum.html", "catalog"),
    )
    rules = recipe_for(source)
    result = discover_page(
        "https://badilum.mahkamahagung.go.id/publik/peraturan/kebijakan-dirjen-badilum/5143-decision.html",
        (FIXTURES / "badilum-detail.html").read_bytes(),
        "text/html",
        rules,
    )
    assert result["purpose"] == "detail"
    assert len(result["links"]) == 1
    document = result["links"][0]
    assert document["url"] == (
        "https://badilum.mahkamahagung.go.id/attachments/article/5143/"
        "SK%20Pembaharuan%20Juknis%20Perma%201%20Tahun%202016.pdf"
    )
    assert document["purpose"] == "document"
    assert discover_page(document["url"], b"%PDF-1.7\n", "application/pdf", rules)["matched"]


@pytest.mark.parametrize(
    "target",
    [
        "https://laws.example.org /files/Act 1.pdf",
        "https://user name@laws.example.org/files/Act 1.pdf",
        "https://laws.example.org/files/Act\n1.pdf",
        "https://laws.example.org/files/Act\t1.pdf",
        "/files/%2e%2e/Act 1.pdf",
        "/files/%252e%252e/Act 1.pdf",
        "/files/Act%00 1.pdf",
    ],
)
def test_html_filename_encoding_does_not_sanitize_unsafe_links(target):
    result = discover_page(
        "https://laws.example.org/catalog", html(f'<a href="{target}">Act</a>'), "text/html", recipe()
    )
    assert result["links"] == []


def test_raw_page_url_still_requires_valid_encoding():
    result = discover_page(
        "https://laws.example.org/files/Act 1.pdf", b"%PDF-1.7\n", "application/pdf", recipe()
    )
    assert not result["matched"]


def test_lkpp_real_extensionless_download_is_document_but_index_is_catalog():
    source = item("ID", "4.7", "html_links", ("https://jdih.lkpp.go.id/regulation/search", "catalog"))
    rules = recipe_for(source)
    result = discover_page(
        "https://jdih.lkpp.go.id/regulation/peraturan-lkpp/peraturan-lkpp-nomor-3-tahun-2026",
        (FIXTURES / "lkpp-detail.html").read_bytes(),
        "text/html",
        rules,
    )
    assert [(link["url"], link["purpose"]) for link in result["links"]] == [
        ("https://jdih.lkpp.go.id/regulation/index", "catalog"),
        ("https://jdih.lkpp.go.id/regulation/download/peraturan-lkpp-nomor-3-tahun-2026/1", "document"),
    ]
    download = result["links"][1]["url"]
    assert discover_page(download, b"%PDF-1.7\n", "application/pdf", rules)["matched"]
    assert not discover_page(download, html("Download error"), "text/html", rules)["matched"]


def wto_item():
    source = item(
        "ID",
        "5.4",
        "html_links",
        ("https://www.wto.org/english/docs_e/legal_e/legal_e.htm", "catalog"),
        ("https://goods-schedules.wto.org/member/indonesia", "catalog"),
    )
    source["trusted_hosts"] = ["docs.wto.org"]
    return source


def test_wto_real_legal_detail_discovers_official_pdf_not_word_or_restricted_books():
    rules = recipe_for(wto_item())
    result = discover_page(
        "https://www.wto.org/english/docs_e/legal_e/marag_e.htm",
        (FIXTURES / "wto-legal-detail.html").read_bytes(),
        "text/html",
        rules,
    )
    documents = [x["url"] for x in result["links"] if x["purpose"] == "document"]
    assert documents == [
        "https://docs.wto.org/gattdocs/q/G/TMB/R116.PDF",
        "https://www.wto.org/english/docs_e/legal_e/downloads_e/WTO_en.pdf",
    ]
    assert all(not x["url"].endswith(".docx") for x in result["links"])
    assert result["purpose"] == "detail"
    assert discover_page(documents[1], b"%PDF-1.7\n", "application/pdf", rules)["matched"]


def test_wto_goods_follows_member_pagination_and_file_list_without_other_members_or_bundles():
    rules = recipe_for(wto_item())
    result = discover_page(
        "https://goods-schedules.wto.org/member/indonesia",
        (FIXTURES / "wto-goods-member.html").read_bytes(),
        "text/html",
        rules,
    )
    assert any(
        x["url"].endswith("/browse/43339/file_browser") and x["purpose"] == "catalog" for x in result["links"]
    )
    assert any("page=1" in x["url"] and x["purpose"] == "catalog" for x in result["links"])
    files = discover_page(
        "https://goods-schedules.wto.org/browse/43339/file_browser",
        (FIXTURES / "wto-goods-files.html").read_bytes(),
        "text/html",
        rules,
    )
    assert files["purpose"] == "catalog"
    assert len(files["links"]) == 7 and all(x["purpose"] == "document" for x in files["links"])
    assert all("/system/files/" in x["url"] and x["url"].lower().endswith(".pdf") for x in files["links"])
    excluded = discover_page(
        "https://goods-schedules.wto.org/member/indonesia",
        html("""
        <a href="/member/singapore">Singapore</a><a href="/member">All Members</a>
        <a href="/member/download/36658">ZIP bundle</a><a href="/download_all/43339">ZIP</a>
        <a href="/system/files/2020-10/RS619.docx">Word</a>
        <a href="https://www.wto.org/english/res_e/booksp_e/xbooksp_e/wtoagreement_e.pdf">Restricted</a>
        """),
        "text/html",
        rules,
    )
    assert excluded["links"] == []


def test_bandung_official_alternative_separates_perda_perwali_and_upgrades_only_same_host_pagination():
    catalog = "https://jdih.bandung.go.id/home/produk-hukum/peraturan-perundang-undangan-daerah"
    for item_id, count, type_term in [("1.7", 1, "Peraturan Daerah"), ("1.10", 3, "Peraturan Wali Kota")]:
        source = item(
            "ID", item_id, "bpk", ("https://peraturan.bpk.go.id/Jenis/3", "catalog"), (catalog, "catalog")
        )
        rules = recipe_for(source)
        result = discover_page(catalog, (FIXTURES / "bandung-catalog.html").read_bytes(), "text/html", rules)
        assert result["purpose"] == "catalog"
        details = [x for x in result["links"] if x["purpose"] == "detail"]
        assert len(details) == count and all(type_term in x["title"] for x in details)
        pagination = [x["url"] for x in result["links"] if x["purpose"] == "catalog"]
        assert catalog + "?page=2" in pagination and catalog + "?page=390" in pagination
        assert all(x["url"].startswith("https://") for x in result["links"])
        unsafe = discover_page(
            catalog,
            html("""
            <a href="http://evil.test/home/produk-hukum/peraturan-perundang-undangan-daerah?page=2">2</a>
            <a href="http://jdih.bandung.go.id:444/home/produk-hukum/peraturan-perundang-undangan-daerah?page=2">2</a>
            <a href="http://user@jdih.bandung.go.id/home/produk-hukum/peraturan-perundang-undangan-daerah?page=2">2</a>
            <a href="http://jdih.bandung.go.id/home/produk-hukum/berita">News</a>
            """),
            "text/html",
            rules,
        )
        assert unsafe["links"] == []
    detail = discover_page(
        catalog + "/30394",
        (FIXTURES / "bandung-detail.html").read_bytes(),
        "text/html",
        recipe_for(item("ID", "1.7", "bpk", (catalog, "catalog"))),
    )
    assert detail["purpose"] == "detail"
    assert [(x["url"], x["purpose"]) for x in detail["links"]] == [
        ("https://jdih.bandung.go.id/media/22055?download=true", "document"),
    ]


def test_mk_official_puu_alternative_keeps_type_filter_and_scopes_judgment_files():
    catalog = "https://www.mkri.id/perkara/persidangan/putusan?jenis=PUU"
    source = item(
        "ID",
        "3.1",
        "html_links",
        ("https://jdih.mkri.id/produk-hukum/putusan", "catalog"),
        (catalog, "catalog"),
    )
    source["trusted_hosts"] = ["s.mkri.id"]
    rules = recipe_for(source)
    pdf = "https://s.mkri.id/public/content/persidangan/putusan/putusan_mkri_14426_1781684026.pdf"
    result = discover_page(
        catalog,
        html(f'''
        <a href="?jenis=PUU&page=2">2</a><a href="?jenis=PHPU">Other type</a><a href="?page=2">Lost filter</a>
        <a href="{pdf}">Putusan</a><a href="/peraturan/peraturan-uud">Constitution</a>
        <a href="https://s.mkri.id/public/content/persidangan/sinopsis/summary.pdf">Summary</a>
        '''),
        "text/html",
        rules,
    )
    assert [(x["url"], x["purpose"]) for x in result["links"]] == [
        (catalog + "&page=2", "catalog"),
        (pdf, "document"),
    ]
    assert discover_page(pdf, b"%PDF-1.7\n", "application/pdf", rules)["matched"]
