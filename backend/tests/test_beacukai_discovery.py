"""The DJBC legacy catalogue exposes laws through query-routed pages."""

import json
import importlib
import sys

from app.configuration.paths import PROJECT_ROOT
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import discover_page

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
unattended_collection = importlib.import_module("unattended_collection")


def source():
    path = PROJECT_ROOT / "config/collection-sources/id-sg-unattended.json"
    registry = json.loads(path.read_text(encoding="utf-8"))
    return next(row for row in registry["sources"] if row["id"] == "id-beacukai-regulations")


def test_beacukai_recipe_separates_catalogue_detail_and_original_files():
    configured = source()
    recipe = unattended_collection.recipe(configured)
    category = "https://peraturan.beacukai.go.id/index.html?page=jenis/12/peraturan-menteri-keuangan/1.html"
    detail = (
        "https://peraturan.beacukai.go.id/index.html?page=detail/jenis/12/576/"
        "peraturan-menteri-keuangan/pmk-208-pmk-11-2013/tarif-bea-masuk.html"
    )
    original = (
        "https://repository.beacukai.go.id/referensi/2026/10/"
        "a5d5a7b56c24-peraturan-bea-masuk.pdf"
    )
    assert recipe["max_links"] == 200
    assert recipe["include_terms"] == []
    assert unattended_collection.recipe(configured) == recipe

    assert discover_page(category, b"<h1>Peraturan Menteri Keuangan</h1>", "text/html", recipe)[
        "purpose"
    ] == "catalog"
    assert discover_page(detail, b"<h1>Official regulation detail</h1>", "text/html", recipe)[
        "purpose"
    ] == "detail"
    assert discover_page(original, b"%PDF-1.7\n", "application/pdf", recipe)["matched"]
    assert not discover_page(
        "https://repository.beacukai.go.id/files/unreviewed.pdf",
        b"%PDF-1.7\n",
        "application/pdf",
        recipe,
    )["matched"]


def test_beacukai_home_discovery_skips_navigation_and_upgrades_its_official_file_link():
    recipe = unattended_collection.recipe(source())
    category = "index.html?page=jenis/12/peraturan-menteri-keuangan/1.html"
    detail = (
        "index.html?page=detail/jenis/12/576/peraturan-menteri-keuangan/"
        "pmk-208-pmk-11-2013/tarif-bea-masuk.html"
    )
    manual = "http://repository.beacukai.go.id/referensi/2011/11/a5d5a7b56c24-user-manual.pdf"
    original = (
        "http://repository.beacukai.go.id/referensi/2026/10/"
        "a5d5a7b56c24-pmk-208-2026.pdf"
    )
    html = (
        "<html><head><title>Direktori Peraturan DJBC</title></head><body>"
        "<h1>Direktori Peraturan</h1>"
        '<a href="index.html?page=about">About</a>'
        f'<a href="{category}">Peraturan Menteri Keuangan</a>'
        f'<a href="{detail}">PMK 208 tentang tarif bea masuk</a>'
        f'<a href="{manual}">User manual</a>'
        f'<a href="{original}">PMK 208/2026 original PDF</a>'
        "</body></html>"
    ).encode()
    result = discover_page("https://peraturan.beacukai.go.id/", html, "text/html", recipe)
    by_url = {row["url"]: row["purpose"] for row in result["links"]}
    assert by_url == {
        "https://peraturan.beacukai.go.id/index.html?page=jenis%2F12%2Fperaturan-menteri-keuangan%2F1.html": "catalog",
        "https://peraturan.beacukai.go.id/index.html?page=detail%2Fjenis%2F12%2F576%2Fperaturan-menteri-keuangan%2Fpmk-208-pmk-11-2013%2Ftarif-bea-masuk.html": "detail",
        "https://repository.beacukai.go.id/referensi/2026/10/a5d5a7b56c24-pmk-208-2026.pdf": "document",
    }
