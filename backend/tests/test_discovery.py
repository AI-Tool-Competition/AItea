"""Discovery only returns bounded, reviewable candidates; it never approves laws."""

from urllib.parse import parse_qs, urlsplit

import pytest

from app.discovery import discover_page, normalized_seeds_for, recipe_for


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
