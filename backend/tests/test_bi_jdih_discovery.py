import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
    discover_page,
    page_body_limit,
    recipe_for,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLAN = json.loads((PROJECT_ROOT / "config/collection-plans/indonesia-20261003.json").read_text())
ITEM = next(row for row in PLAN["items"] if row["id"] == "4.6")
ROOT_URL = "https://jdih.bi.go.id/Web/DaftarPeraturan"


def test_bi_jdih_recipe_is_limited_to_the_official_read_only_endpoints():
    recipe = recipe_for(ITEM)
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _Recipe

    rules = _Recipe(recipe)
    assert recipe["adapter"] == "bi_jdih"
    assert recipe["allowed_hosts"] == ["jdih.bi.go.id"]
    assert rules.purpose(ROOT_URL) == "catalog"
    assert rules.purpose("https://jdih.bi.go.id/Web/Beranda/DetailPeraturan/12409") is None
    assert rules.purpose("https://jdih.bi.go.id/api/WebJDIH/GetDataWebPeraturan?PeraturanID=12409") == "detail"
    assert rules.purpose("https://jdih.bi.go.id/api/WebJDIH/GetDataWebPeraturan?PeraturanID=12409&other=1") is None
    assert rules.purpose("https://jdih.bi.go.id/api/WebJDIH/GetFilePeraturan/12409") == "document"
    assert rules.purpose("https://jdih.bi.go.id/api/WebJDIH/HitDownload/12409") is None


def test_bi_jdih_large_catalog_is_bounded_and_paginates_without_duplicate_ids():
    content = (
        "<html><head><title>Peraturan - JDIH Bank Indonesia</title></head><body><h1>Peraturan</h1>"
        + "".join(
            f'<a href="/Web/DaftarPeraturan/Detail/{number}">Peraturan PBI {number} Tahun 2026</a>'
            for number in range(1, 451)
        )
        + "</body></html>"
    ).encode()
    recipe = recipe_for(ITEM)
    assert page_body_limit(ROOT_URL, "text/html", recipe) == 6 * 1024 * 1024

    discovered = []
    page_url = ROOT_URL
    while True:
        result = discover_page(page_url, content, "text/html", recipe)
        assert result["matched"] and result["purpose"] == "catalog"
        documents = [link for link in result["links"] if link["purpose"] == "detail"]
        assert len(result["links"]) <= 200
        discovered.extend(parse_qs(urlsplit(link["url"]).query)["PeraturanID"][0] for link in documents)
        continuation = [link for link in result["links"] if link["purpose"] == "catalog"]
        if not continuation:
            break
        page_url = continuation[0]["url"]

    assert discovered == [str(number) for number in range(1, 451)]


def test_bi_jdih_catalog_accepts_both_official_detail_route_shapes():
    body = (
        '<html><title>Peraturan</title><body><h1>Peraturan</h1>'
        '<a href="/Web/DaftarPeraturan/Detail/12409">PBI Nomor 9 Tahun 2026</a>'
        '<a href="/Web/Beranda/DetailPeraturan/12408">PBI Nomor 8 Tahun 2026</a>'
        "</body></html>"
    ).encode()
    result = discover_page(ROOT_URL, body, "text/html", recipe_for(ITEM))
    assert result["matched"] and result["purpose"] == "catalog"
    assert [link["url"] for link in result["links"]] == [
        "https://jdih.bi.go.id/api/WebJDIH/GetDataWebPeraturan?PeraturanID=12409",
        "https://jdih.bi.go.id/api/WebJDIH/GetDataWebPeraturan?PeraturanID=12408",
    ]


def test_bi_jdih_metadata_is_bound_to_the_requested_id_and_keeps_source_fields():
    url = "https://jdih.bi.go.id/api/WebJDIH/GetDataWebPeraturan?PeraturanID=12409"
    payload = {
        "StatusCode": 200,
        "Data": {
            "PeraturanID": 12409,
            "NomorPeraturan": "9/2026",
            "Judul": "Peraturan Bank Indonesia tentang Pengaturan",
            "JenisPeraturanDesc": "Peraturan Bank Indonesia",
            "TanggalPenetapan": "2026-05-12",
            "TanggalPengundangan": "2026-05-14",
            "TanggalBerlaku": "2026-05-14",
            "StatusText": "Berlaku",
            "Sumber": "Lembaran Negara",
            "Mengubah": ["PBI 1/2020"],
            "Mencabut": [],
            "PeraturanTerkait": ["PADG 2/2026"],
        },
    }
    result = discover_page(url, json.dumps(payload).encode(), "application/json", recipe_for(ITEM))
    assert result["matched"] and result["purpose"] == "detail"
    candidate = result["links"][0]
    assert candidate["url"] == "https://jdih.bi.go.id/api/WebJDIH/GetFilePeraturan/12409"
    record = candidate["source_record"]
    assert record["provider"] == "bi_jdih"
    assert record["interpretation"] == "unreviewed_source_values"
    assert record["fields"]["effective_date"] == "2026-05-14"
    assert record["fields"]["amends"] == ["PBI 1/2020"]
    assert record["source_values"]["PeraturanTerkait"] == ["PADG 2/2026"]

    payload["Data"]["PeraturanID"] = 12410
    mismatched = discover_page(url, json.dumps(payload).encode(), "application/json", recipe_for(ITEM))
    assert not mismatched["matched"] and not mismatched["links"]


def test_bi_jdih_file_route_requires_a_real_pdf_signature():
    recipe = recipe_for(ITEM)
    file_url = "https://jdih.bi.go.id/api/WebJDIH/GetFilePeraturan/12409"
    assert discover_page(file_url, b"%PDF-1.7\n", "application/pdf", recipe)["matched"]
    assert not discover_page(file_url, b"<html>denied</html>", "text/html", recipe)["matched"]
    assert not discover_page(file_url, b"%PDF-1.7\n", "application/octet-stream", recipe)["matched"]


def test_bi_jdih_catalog_does_not_exceed_its_six_mib_adapter_limit():
    recipe = recipe_for(ITEM)
    oversized = b"<html><body>" + b"x" * (6 * 1024 * 1024) + b"</body></html>"
    result = discover_page(ROOT_URL, oversized, "text/html", recipe)
    assert not result["matched"]
    assert "6 MiB" in result["reason"]
