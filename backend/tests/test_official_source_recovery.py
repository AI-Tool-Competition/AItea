"""Only documented original paths widen; legal and source gates stay intact."""

import json
from copy import deepcopy

import pytest
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
    _Recipe,
    discover_page,
    recipe_for,
)
from app.contexts.ingestion.infrastructure.source_adapters.official_source_recovery import (
    recover_official_recipe,
    request_headers_for,
)
from app.configuration.paths import PROJECT_ROOT

PLAN = PROJECT_ROOT / "config/collection-plans/indonesia-20261003.json"
PLAN_V11 = PROJECT_ROOT / "config/collection-plans/versions/indonesia-20261003.v11.json"
PLAN_V12 = PROJECT_ROOT / "config/collection-plans/versions/indonesia-20261003.v12.json"


def item_recipe(item_id, *, version=None):
    if version == 12:
        plan = json.loads(PLAN_V12.read_text())
        item = next(item for item in plan["items"] if item["id"] == item_id)
        return recipe_for(item), item
    plan = json.loads(PLAN.read_text())
    item = next((item for item in plan["items"] if item["id"] == item_id), None)
    if item is None:
        # Existing immutable runs retain their versioned adapter behavior after
        # inaccessible source categories are removed from the active plan.
        plan = json.loads(PLAN_V11.read_text())
        item = next(item for item in plan["items"] if item["id"] == item_id)
    return recipe_for(item), item


def test_active_mk_puu_seeds_are_only_the_two_verified_official_originals():
    recipe, item = item_recipe("3.1")
    expected = {
        "https://s.mkri.id/public/content/persidangan/putusan/putusan_mkri_14426_1781684026.pdf",
        "https://s.mkri.id/public/content/persidangan/putusan/putusan_mkri_14258_1778572852.pdf",
    }
    assert {entry["url"] for entry in item["entries"]} == expected
    assert all(entry["purpose"] == "document" for entry in item["entries"])
    rules = _Recipe(recipe)
    assert all(rules.purpose(url) == "document" for url in expected)
    assert rules.purpose("https://jdih.mkri.id/produk-hukum/putusan?jenis=PUU") is None


@pytest.mark.parametrize(
    ("item_id", "version", "source", "original"),
    [
        (
            "4.4",
            None,
            "https://jdih.kemnaker.go.id/peraturan/detail/2983/surat-edaran-menteri-nomor-3-tahun-2026",
            "https://jdih.kemnaker.go.id/asset/data_puu/2026senaker003.pdf",
        ),
        (
            "4.6",
            12,
            "https://www.bi.go.id/id/publikasi/peraturan/Pages/PADG_032026.aspx",
            "https://www.bi.go.id/id/publikasi/peraturan/Documents/PADG_032026.pdf",
        ),
        (
            "2.2",
            None,
            "https://putusan3.mahkamahagung.go.id/rumusan_rakernas/detail/11eb5a277c763046aede313432353230.html",
            "https://putusan3.mahkamahagung.go.id/rumusan_rakernas/download_file/11eb5a277c7632629062313432353230/pdf/11eb5a277c763046aede313432353230.html",
        ),
        (
            "3.2",
            None,
            "https://putusan3.mahkamahagung.go.id/yurisprudensi/detail/11e93a313416280ab9c0303834343231.html",
            "https://putusan3.mahkamahagung.go.id/direktori/download_file/11eff43e5c1541069bbf313933363534/pdf/11eff43e5c12b2c4ac0c313933363534",
        ),
    ],
)
def test_documented_original_routes_are_discovered(item_id, version, source, original):
    recipe, _ = item_recipe(item_id, version=version)
    recovered = recover_official_recipe(recipe, country="ID", item_id=item_id)
    body = f'<html><title>Official regulation</title><a href="{original}">Dokumen</a></html>'.encode()
    result = discover_page(source, body, "text/html", recovered)
    assert result["matched"]
    assert result["links"][0]["url"] == original
    assert result["links"][0]["purpose"] == "document"


@pytest.mark.parametrize("item_id", ["3.2", "3.3", "3.4"])
def test_judgment_route_applies_only_to_declared_ma_categories(item_id):
    recipe, _ = item_recipe(item_id)
    recovered = recover_official_recipe(recipe, country="ID", item_id=item_id)
    address = (
        "https://putusan3.mahkamahagung.go.id/direktori/download_file/"
        "11eff43e5c1541069bbf313933363534/pdf/11eff43e5c12b2c4ac0c313933363534"
    )
    assert _Recipe(recovered).purpose(address) == "document"


def test_original_address_returning_html_still_fails_original_gate():
    recipe, _ = item_recipe("2.2")
    recovered = recover_official_recipe(recipe, country="ID", item_id="2.2")
    address = (
        "https://putusan3.mahkamahagung.go.id/rumusan_rakernas/download_file/"
        "11eb5a277c7632629062313432353230/pdf/11eb5a277c763046aede313432353230.html"
    )
    result = discover_page(
        address, b"<html><title>Rumusan</title>Summary only</html>", "text/html", recovered
    )
    assert not result["matched"]
    assert "HTML" in result["reason"]


def test_bi_faq_remains_locating_evidence_and_other_sources_remain_excluded():
    # Existing immutable v12 runs retain the legacy Bank Indonesia source scope.
    recipe, _ = item_recipe("4.6", version=12)
    rules = _Recipe(recover_official_recipe(recipe, country="ID", item_id="4.6"))
    for address in (
        "https://www.bi.go.id/id/publikasi/peraturan/Documents/FAQ_PADG_032026.pdf",
        "https://www.bi.go.id/id/publikasi/peraturan/Documents/PADG_032026_FAQ.pdf",
        "https://www.bi.go.id/id/publikasi/peraturan/Documents/PBI_032026_Tanya_Jawab.pdf",
    ):
        assert rules.purpose(address) == "detail"
    for address in (
        "https://www.bi.go.id/id/publikasi/laporan/Documents/PADG_032026.pdf",
        "https://unofficial.example/id/publikasi/peraturan/Documents/PADG_032026.pdf",
    ):
        assert rules.purpose(address) is None


def test_augmentation_preserves_input_recipe_and_scope():
    recipe, _ = item_recipe("4.4")
    before = deepcopy(recipe)
    recovered = recover_official_recipe(recipe, country="ID", item_id="4.4")
    assert recipe == before
    assert recovered["allowed_hosts"] == before["allowed_hosts"]
    for name in ("catalog_patterns", "detail_patterns", "exclude_patterns", "required_query", "max_links"):
        assert recovered[name] == before[name]
    assert recover_official_recipe(recovered, country="ID", item_id="4.4") == recovered


def test_wrong_country_item_and_untrusted_host_do_not_gain_paths():
    recipe, _ = item_recipe("4.4")
    assert recover_official_recipe(recipe, country="SG", item_id="4.4") == recipe
    assert recover_official_recipe(recipe, country="ID", item_id="4.3") == recipe
    recipe["allowed_hosts"] = ["unofficial.example"]
    assert recover_official_recipe(recipe, country="ID", item_id="4.4") == recipe


def test_recovered_addresses_do_not_accept_nonpdf_or_traversal_paths():
    recipe, _ = item_recipe("4.4")
    recovered = recover_official_recipe(recipe, country="ID", item_id="4.4")
    source = "https://jdih.kemnaker.go.id/peraturan/detail/2983/surat-edaran-menteri-nomor-3-tahun-2026"
    body = (
        b"<html><title>Official regulation</title>"
        b'<a href="/asset/data_puu/2026senaker003.js">Script</a>'
        b'<a href="/asset/data_puu/%2e%2e/private.pdf">Traversal</a></html>'
    )
    result = discover_page(source, body, "text/html", recovered)
    assert result["matched"]
    assert not result["links"]


def test_browser_headers_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("COLLECTION_BROWSER_HEADERS_HOSTS", raising=False)
    assert request_headers_for("jdih.mahkamahagung.go.id") == {}


def test_browser_profile_only_applies_to_explicit_exact_hosts(monkeypatch):
    monkeypatch.setenv("COLLECTION_BROWSER_HEADERS_HOSTS", " JDIH.KEMNAKER.GO.ID, treaty-room.kemlu.go.id ")
    headers = request_headers_for("jdih.kemnaker.go.id")
    assert set(headers) == {"User-Agent", "Accept", "Accept-Language"}
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert request_headers_for("sub.jdih.kemnaker.go.id") == {}
    assert request_headers_for("unofficial.example") == {}
    headers["Cookie"] = "not retained"
    assert "Cookie" not in request_headers_for("jdih.kemnaker.go.id")


@pytest.mark.parametrize(
    "value",
    [
        "https://jdih.kemnaker.go.id/",
        "*.kemnaker.go.id",
        "user:secret@jdih.kemnaker.go.id",
        "jdih.kemnaker.go.id:443",
        "jdih.kemnaker.go.id/path",
        "jdih.kemnaker.go.id?cookie=secret",
        "jdih.kemnaker.go.id\nCookie:secret",
        "127.0.0.1",
        "10.0.0.2",
        "192.168.0.1",
        "::1",
        "localhost",
        "sub.localhost",
        "jdih.kemnaker.go.id,",
        ",".join(f"source{index}.example" for index in range(65)),
        "a" * 8193,
    ],
)
def test_browser_header_configuration_is_bounded_and_cannot_inject_headers(monkeypatch, value):
    monkeypatch.setenv("COLLECTION_BROWSER_HEADERS_HOSTS", value)
    with pytest.raises(ValueError, match="COLLECTION_BROWSER_HEADERS_HOSTS") as error:
        request_headers_for("jdih.kemnaker.go.id")
    assert "secret" not in str(error.value)


def test_browser_profile_accepts_at_most_64_exact_hosts(monkeypatch):
    hosts = [f"source{index}.example" for index in range(64)]
    monkeypatch.setenv("COLLECTION_BROWSER_HEADERS_HOSTS", ",".join(hosts))
    assert request_headers_for(hosts[-1])


def test_untrusted_headers_and_ambient_proxy_options_never_change_profile(monkeypatch):
    monkeypatch.setenv("COLLECTION_BROWSER_HEADERS_HOSTS", "jdih.kemnaker.go.id")
    expected = request_headers_for("jdih.kemnaker.go.id")
    monkeypatch.setenv("COLLECTION_BROWSER_COOKIE", "secret-session")
    monkeypatch.setenv("COLLECTION_BROWSER_AUTHORIZATION", "Bearer secret")
    monkeypatch.setenv("HTTP_PROXY", "http://secret@untrusted.example:8080")
    assert request_headers_for("jdih.kemnaker.go.id") == expected
