import json
from pathlib import Path

from app.contexts.ingestion.infrastructure.source_adapters.bi_official_discovery import (
    catalog_form_request,
    catalog_url,
    inspect,
    page_number,
    purpose,
)
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import recipe_for
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _Recipe

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLAN = json.loads((PROJECT_ROOT / "config/collection-plans/indonesia-20261003.json").read_text())
ITEM = next(row for row in PLAN["items"] if row["id"] == "4.6")
RULES = recipe_for(ITEM)


def catalog_page(page_number, *, total=20):
    start = (page_number - 1) * 10 + 1
    end = min(start + 9, total)
    entries = []
    for number in range(start, end + 1):
        entries.append(
            f'<a href="/id/publikasi/peraturan/Pages/PBI_{number:02d}2026.aspx">'
            f"Peraturan Bank Indonesia Nomor {number} Tahun 2026</a>"
        )
    next_link = (
        f"""<div id="ctl00_ctl51_DataPagerPeraturan"><a href="javascript:__doPostBack('ctl00$ctl51$DataPagerPeraturan$ctl01$ctl01','')">{page_number + 1}</a></div>"""
        if end < total
        else '<div id="ctl00_ctl51_DataPagerPeraturan"><span>2</span></div>'
    )
    return (
        '<html><head><title>Peraturan</title></head><body><h1>Peraturan</h1>'
        f"<p>Menampilkan {start}–{end} data dari total {total}</p>"
        + "".join(entries)
        + next_link
        + "</body></html>"
    ).encode()


def test_recipe_and_purpose_are_bound_to_indonesia_46_and_exact_official_paths():
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import _Recipe

    rules = _Recipe(RULES)
    assert RULES["adapter"] == "bi_official"
    assert RULES["allowed_hosts"] == ["www.bi.go.id"]
    assert rules.purpose(catalog_url(1)) == "catalog"
    assert rules.purpose(catalog_url(2)) == "catalog"
    assert page_number(catalog_url(2)) == 2
    assert purpose("https://www.bi.go.id/id/publikasi/peraturan/Pages/PBI_092026.aspx") == "detail"
    assert purpose("https://www.bi.go.id/id/publikasi/peraturan/Documents/FAQ-PBI_092026.pdf") == "document"
    assert purpose("https://jdih.bi.go.id/Web/DaftarPeraturan") is None
    assert purpose("https://www.bi.go.id/id/publikasi/peraturan/Pages/PBI_092026.aspx?x=1") is None
    assert purpose("https://www.bi.go.id/id/publikasi/peraturan/Documents/%2e%2e%2Fsecret.pdf") is None
    assert purpose("https://www.bi.go.id/id/publikasi/peraturan/default.aspx?Kategori=other") is None
    assert purpose("https://www.bi.go.id/id/publikasi/peraturan/default.aspx?Kategori=peraturan+bank+indonesia&aitea_page=2001") is None
    assert rules.purpose("https://www.bi.go.id/en/publications/regulations/Pages/PBI_092026.aspx") is None


def test_catalog_requires_full_page_range_matches_detail_rows_and_continues_one_page_at_a_time():
    first_url = catalog_url(1)
    first = inspect(first_url, catalog_page(1), "text/html", _Recipe(RULES))
    assert first["matched"] and first["purpose"] == "catalog"
    assert first["scope"] == {
        "provider": "bi-official-regulations-v1",
        "catalog_sha256": first["scope"]["catalog_sha256"],
        "page": 1,
        "start": 1,
        "end": 10,
        "total": 20,
        "page_size": 10,
        "returned": 10,
        "total_pages": 2,
        "has_next": True,
    }
    assert len([link for link in first["links"] if link["purpose"] == "detail"]) == 10
    continuation = [link for link in first["links"] if link["purpose"] == "catalog"]
    assert len(continuation) == 1 and continuation[0]["url"] == catalog_url(2, expected_total=20)

    second = inspect(catalog_url(2, expected_total=20), catalog_page(2), "text/html", _Recipe(RULES))
    assert second["matched"] and second["scope"]["page"] == 2
    assert not any(link["purpose"] == "catalog" for link in second["links"])
    changed_total = inspect(
        catalog_url(2, expected_total=20), catalog_page(2, total=30), "text/html", _Recipe(RULES)
    )
    assert not changed_total["matched"]

    incomplete = catalog_page(1).replace(
        b"/id/publikasi/peraturan/Pages/PBI_102026.aspx", b"/outside/PBI_102026.aspx"
    )
    assert not inspect(first_url, incomplete, "text/html", _Recipe(RULES))["matched"]


def test_catalog_postback_uses_bounded_hidden_state_and_records_only_request_fingerprints():
    parent_url = catalog_url(1)
    body = catalog_page(1).replace(b"<body>", b"<body><form action=\"\">").replace(
        b"</body>",
        b"""<input type="hidden" name="__VIEWSTATE" value="private-view-state">
        <input type="hidden" name="__EVENTVALIDATION" value="private-event-validation">
        <input type="hidden" name="__EVENTTARGET" value="">
        <input type="text" name="filter" value=""></form></body>""",
    )
    fields, evidence = catalog_form_request(parent_url, body, 2)
    values = dict(fields)
    assert values["__VIEWSTATE"] == "private-view-state"
    assert values["__EVENTVALIDATION"] == "private-event-validation"
    assert values["__EVENTTARGET"] == "ctl00$ctl51$DataPagerPeraturan$ctl01$ctl01"
    assert values["__EVENTARGUMENT"] == ""
    assert evidence["parent_url"] == parent_url
    assert evidence["parent_page"] == 1 and evidence["target_page"] == 2
    assert "private-view-state" not in json.dumps(evidence)
    assert len(fields) <= 512

    try:
        catalog_form_request(catalog_url(2), body, 4)
    except ValueError as error:
        assert "相邻页面" in str(error)
    else:
        raise AssertionError("non-adjacent postback unexpectedly accepted")


def test_catalog_postback_uses_same_origin_next_image_control_when_numeric_link_is_hidden():
    parent_url = catalog_url(2)
    body = catalog_page(2, total=30).replace(
        b'<a href="javascript:__doPostBack(\'ctl00$ctl51$DataPagerPeraturan$ctl01$ctl01\',\'\')">3</a>',
        b'<input type="image" name="ctl00$ctl51$DataPagerPeraturan$ctl02$ctl00" '
        b'class="next" alt="Next" src="/Style Library/next.svg">',
    ).replace(b"<body>", b"<body><form>").replace(
        b"</body>",
        b"<input type=hidden name=__VIEWSTATE value=state>"
        b"<input type=hidden name=__EVENTVALIDATION value=validation></form></body>",
    )
    fields, evidence = catalog_form_request(parent_url, body, 3)
    values = dict(fields)
    assert values["ctl00$ctl51$DataPagerPeraturan$ctl02$ctl00.x"] == "0"
    assert values["ctl00$ctl51$DataPagerPeraturan$ctl02$ctl00.y"] == "0"
    assert evidence["event_target"] is None
    assert evidence["image_submit"] == "ctl00$ctl51$DataPagerPeraturan$ctl02$ctl00"


def test_detail_preserves_metadata_and_distinguishes_body_faq_and_annex_candidates():
    url = "https://www.bi.go.id/id/publikasi/peraturan/Pages/PBI_092026.aspx"
    content = b"""<html><head><title>Peraturan Bank Indonesia Nomor 9 Tahun 2026 tentang Edukasi</title></head>
    <body><h1>Peraturan Bank Indonesia Nomor 9 Tahun 2026 tentang Edukasi</h1><table>
      <tr><td>Peraturan : Peraturan Bank Indonesia Nomor 9 Tahun 2026 tentang Edukasi</td></tr>
      <tr><td>Berlaku : sejak tanggal diundangkan</td></tr>
      <tr><td><a href="/id/publikasi/peraturan/Documents/PBI_082026.pdf">Peraturan Bank Indonesia Nomor 09 Tahun 2026.pdf</a></td></tr>
      <tr><td><a href="/id/publikasi/peraturan/Documents/FAQ-PBI_092026.pdf">Tanya Jawab Peraturan Bank Indonesia Nomor 09 Tahun 2026.pdf</a></td></tr>
      <tr><td><a href="/id/publikasi/peraturan/Documents/Lampiran-PBI_092026.pdf">Lampiran PBI 09 Tahun 2026.pdf</a></td></tr>
    </table></body></html>"""
    result = inspect(url, content, "text/html", _Recipe(RULES))
    assert result["matched"] and result["purpose"] == "detail"
    assert result["scope"]["attachment_candidates"] == 3
    links = {link["source_record"]["fields"]["attachment_role"]: link for link in result["links"]}
    assert set(links) == {"document_candidate", "faq", "official_attachment"}
    main = links["document_candidate"]["source_record"]
    assert main["provider"] == "bi_official"
    assert main["fields"]["filename_identity_verified"] is False
    assert main["fields"]["page_metadata"]["Berlaku"] == "sejak tanggal diundangkan"
    assert main["structure"]["file_name"] == "PBI_082026.pdf"


def test_document_requires_a_real_pdf_signature_and_mime_type():
    url = "https://www.bi.go.id/id/publikasi/peraturan/Documents/PBI_092026.pdf"
    assert inspect(url, b"%PDF-1.7\n", "application/pdf", _Recipe(RULES))["matched"]
    assert not inspect(url, b"<html>access denied</html>", "text/html", _Recipe(RULES))["matched"]
    assert not inspect(url, b"%PDF-1.7\n", "application/octet-stream", _Recipe(RULES))["matched"]
