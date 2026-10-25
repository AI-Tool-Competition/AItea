import json
import uuid
from urllib.parse import parse_qs, quote, urlsplit

import pytest

from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
    _Recipe,
    discover_page,
    recipe_for,
    request_parameters,
)

ROOT = "https://jdih.halal.go.id"
ITEM = {
    "id": "4.8",
    "tags": ["country:ID"],
    "adapter": "bpjph_jdih",
    "execution": "discovery",
    "trusted_hosts": ["jdih.halal.go.id"],
    "entries": [{"url": ROOT + "/cari", "purpose": "catalog"}],
    "limits": {
        "max_catalog_pages": 100,
        "discovery_include_terms": ["jaminan produk halal", "halal"],
        "discovery_exclude_terms": ["kosmetik", "rancangan"],
    },
}


def row(title, *, identifier=None, file_path=None, **changes):
    return {
        "id": identifier or str(uuid.uuid4()),
        "judul": title,
        "nomor_dokumen": "12",
        "nomor_peraturan": "12",
        "tahun_terbit": 2026,
        "tanggal_penetapan": "2026-10-12",
        "status": "berlaku",
        "penerbit": "Badan Penyelenggara Jaminan Produk Halal",
        "dokumen_tipe_nama": "Peraturan Perundang-undangan",
        "dokumen_jenis_nama": "Keputusan Kepala BPJPH",
        "dokumen_jenis_kode": "KEPUTUSAN_KEPALA_BPJPH",
        "lampiran_file_id": file_path,
        **changes,
    }


def flight_html(payload, *extra_rows):
    chunk = '4:["$","main",null,{"children":' + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if extra_rows:
        chunk += " " + " ".join(json.dumps(value, ensure_ascii=False) for value in extra_rows)
    body = (
        "<html><head><title>JDIH BPJPH - Jaringan Dokumentasi dan Informasi Hukum</title></head>"
        "<body><h1>JDIH BPJPH</h1><p>Dokumen hukum resmi BPJPH dan informasi peraturan yang berlaku.</p>"
        "<script>self.__next_f.push([1,"
        + json.dumps(chunk, ensure_ascii=False)
        + "]);</script></body></html>"
    )
    return body.encode()


def catalog(page=1):
    relevant = row("Keputusan tentang Sistem Jaminan Produk Halal untuk Pangan")
    excluded = row("Keputusan tentang Pedoman Sistem Jaminan Produk Halal Kosmetik")
    rest = [row(f"Administrasi BPJPH {n}") for n in range(8)]
    rows = [relevant, excluded, *rest]
    payload = {
        "data": rows,
        "total": 12,
        "page": page,
        "limit": 10,
        "totalPages": 2,
        "search": "",
        "bahasa": "",
        "expandedTerms": [],
    }
    return flight_html(payload), relevant


def detail_html(document):
    file_path = document["lampiran_file_id"]
    target = f"/api/jdih/pdf?path={quote(file_path, safe='')}#toolbar=0"
    chunk = '5:["$","main",null,{"children":' + json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    )
    body = (
        "<html><head><title>" + document["judul"] + " | JDIH BPJPH</title></head><body><h1>JDIH BPJPH</h1>"
        "<p>Dokumen hukum resmi BPJPH beserta metadata penerbitan dan lampirannya.</p>"
        '<iframe src="'
        + target
        + '" title="Dokumen resmi"></iframe><script>self.__next_f.push([1,'
        + json.dumps(chunk, ensure_ascii=False)
        + "]);</script></body></html>"
    )
    return body.encode()


def test_bpjph_catalog_filters_scope_and_follows_only_reported_pages():
    body, matching = catalog()
    result = discover_page(ROOT + "/cari", body, "text/html", recipe_for(ITEM))
    assert result["matched"] and result["purpose"] == "catalog"
    assert result["scope"] == {
        "provider": "bpjph-jdih",
        "total": 12,
        "page": 1,
        "page_size": 10,
        "pages_reported": 2,
        "returned": 10,
        "matched_documents": 1,
        "directory_snapshot_atomic": False,
    }
    assert [link["purpose"] for link in result["links"]] == ["detail", "catalog"]
    document = result["links"][0]
    assert document["url"] == ROOT + "/jdih/dokumen/" + matching["id"]
    assert document["identity_key"] == "bpjph:" + matching["id"]
    assert document["source_record"]["provider"] == "bpjph"
    assert document["source_record"]["fields"]["tahun_terbit"] == 2026
    assert result["links"][-1]["url"] == ROOT + "/cari?page=2"


def test_bpjph_detail_requires_matching_uuid_metadata_and_same_origin_iframe():
    body, document = catalog()
    url = ROOT + "/jdih/dokumen/" + document["id"]
    detailed = {
        **document,
        "lampiran_file_id": "jdih/dokumen/1792632931118_salinan_12_2026.pdf",
    }
    result = discover_page(url, detail_html(detailed), "text/html", recipe_for(ITEM))
    assert result["matched"] and result["purpose"] == "detail"
    assert len(result["links"]) == 1
    original = result["links"][0]
    assert original["purpose"] == "document"
    assert original["source_record"]["provider"] == "bpjph"
    assert original["source_record"]["structure"]["link_mechanism"] == "official_detail_iframe"
    assert parse_qs(urlsplit(original["url"]).query) == {"path": [detailed["lampiran_file_id"]]}
    assert discover_page(original["url"], b"%PDF-1.7\n", "application/pdf", recipe_for(ITEM))["matched"]
    assert not discover_page(original["url"], b"not a PDF", "application/pdf", recipe_for(ITEM))["matched"]
    mismatch = detail_html({**detailed, "id": str(uuid.uuid4())})
    assert not discover_page(url, mismatch, "text/html", recipe_for(ITEM))["matched"]


@pytest.mark.parametrize(
    "url",
    [
        ROOT + "/cari?page=1&page=2",
        ROOT + "/cari?query=halal",
        ROOT + "/cari?page=101",
        ROOT + "/api/jdih/pdf?path=../../outside.pdf",
    ],
)
def test_bpjph_request_parameters_reject_unlisted_routes_and_queries(url):
    with pytest.raises(ValueError):
        request_parameters(url, {"country": "ID", "item": ITEM})


def test_bpjph_catalog_requires_consistent_pagination_and_exact_source_identity():
    from app.contexts.ingestion.infrastructure.source_adapters.bpjph_discovery import purpose

    body, _ = catalog()
    recipe = recipe_for(ITEM)
    assert purpose("https://other.example/cari") is None
    assert _Recipe(recipe_for(ITEM)).purpose("https://other.example/cari") is None
    assert not discover_page(ROOT + "/cari?page=2", body, "text/html", recipe)["matched"]
    assert not discover_page(ROOT + "/cari", body, "application/json", recipe)["matched"]
    assert not discover_page(ROOT + "/cari?query=halal", body, "text/html", recipe)["matched"]
    assert not discover_page(
        ROOT + "/cari", b"<html><title>Access denied</title></html>", "text/html", recipe
    )["matched"]
