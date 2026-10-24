"""Read published official LKPP originals without executing its PDF viewer."""

from html import escape
from pathlib import Path

import pytest
from app.contexts.collection.infrastructure.discovery.discovery import discover_page, recipe_for

BASE = "https://jdih.lkpp.go.id/admin/uploads/documents"
DETAIL = "https://jdih.lkpp.go.id/regulation/peraturan-lkpp/peraturan-lkpp-nomor-3-tahun-2026"
ORIGINAL = BASE + "/Peraturan%20Lembaga%20Nomor%203%20Tahun%202026_3287_1.pdf"
FIXTURE = Path(__file__).parent / "fixtures/discovery/recovery-lkpp-detail.html"


def recipe():
    return recipe_for(
        {
            "id": "4.7",
            "adapter": "html_links",
            "execution": "discovery",
            "tags": ["country:ID"],
            "entries": [{"url": "https://jdih.lkpp.go.id/regulation/search", "purpose": "catalog"}],
        }
    )


def test_lkpp_published_viewer_file_becomes_a_normal_original_candidate():
    result = discover_page(DETAIL, FIXTURE.read_bytes(), "text/html", recipe())
    assert result["matched"] and result["purpose"] == "detail"
    assert {row["url"] for row in result["links"]} == {
        ORIGINAL,
        "https://jdih.lkpp.go.id/regulation/download/peraturan-lkpp-nomor-3-tahun-2026/1",
    }
    assert all(row["purpose"] == "document" for row in result["links"])
    assert discover_page(ORIGINAL, b"%PDF-1.7\n%%EOF", "application/pdf", recipe())["matched"]


@pytest.mark.parametrize(
    "base,name",
    [
        ("https://evil.example/admin/uploads/documents", "law.pdf"),
        ("http://jdih.lkpp.go.id/admin/uploads/documents", "law.pdf"),
        ("https://user:secret@jdih.lkpp.go.id/admin/uploads/documents", "law.pdf"),
        ("https://jdih.lkpp.go.id:444/admin/uploads/documents", "law.pdf"),
        (BASE + "/../secret", "law.pdf"),
        (BASE + "?token=value", "law.pdf"),
        (BASE + "#fragment", "law.pdf"),
        (BASE, "https://evil.example/law.pdf"),
        (BASE, "//evil.example/law.pdf"),
        (BASE, "../secret.pdf"),
        (BASE, "folder/law.pdf"),
        (BASE, r"..\secret.pdf"),
        (BASE, "%25252e%25252e%25252fsecret.pdf"),
        (BASE, "%252525252fsecret.pdf"),
        (BASE, "javascript:law.pdf"),
        (BASE, ".hidden.pdf"),
        (BASE, "law.pdf?query=other.pdf"),
        (BASE, "law.pdf#other.pdf"),
        (BASE, "law\n.pdf"),
        (BASE, "law.exe"),
        (BASE, "a" * 241 + ".pdf"),
    ],
)
def test_lkpp_pdf_viewer_attributes_cannot_change_origin_or_escape_file_location(base, name):
    html = (
        "<title>Official detail</title><h1>Official regulation</h1>"
        f'<iframe id="file-frame" data-file-base-url="{escape(base, quote=True)}" '
        f'data-initial-file="{escape(name, quote=True)}"></iframe>'
    )
    result = discover_page(DETAIL, html.encode(), "text/html", recipe())
    assert result["matched"] and result["links"] == []


def test_lkpp_viewer_data_does_not_apply_to_another_origin_or_unrelated_admin_files():
    rules = recipe()
    rules["allowed_hosts"].append("official.example")
    html = (
        "<title>Official detail</title><h1>Official regulation</h1>"
        f'<iframe id="file-frame" data-file-base-url="{BASE}" data-initial-file="law.pdf"></iframe>'
    )
    result = discover_page(
        DETAIL.replace("jdih.lkpp.go.id", "official.example"), html.encode(), "text/html", rules
    )
    assert result["matched"] and result["links"] == []
    for url in (
        "https://jdih.lkpp.go.id/admin/private/law.pdf",
        "https://jdih.lkpp.go.id/admin/uploads/documents/folder/law.pdf",
        "https://jdih.lkpp.go.id/admin/uploads/documents/law.pdf?token=anything",
    ):
        assert not discover_page(url, b"%PDF-1.7\n%%EOF", "application/pdf", rules)["matched"]


def test_lkpp_viewer_files_hidden_in_script_template_or_error_page_are_not_originals():
    iframe = f'<iframe id="file-frame" data-file-base-url="{BASE}" data-initial-file="law.pdf"></iframe>'
    for tag in ("script", "template", "noscript"):
        html = "<title>Official detail</title><h1>Official regulation</h1>" + f"<{tag}>{iframe}</{tag}>"
        assert discover_page(DETAIL, html.encode(), "text/html", recipe())["links"] == []
    html = "<title>Access denied</title>" + iframe
    result = discover_page(DETAIL, html.encode(), "text/html", recipe())
    assert not result["matched"] and result["links"] == []


def test_lkpp_file_requires_unambiguous_published_viewer_attributes():
    for attributes in (
        f'data-file-base-url="{BASE}" data-initial-file="law.pdf"',
        'id="file-frame" data-initial-file="law.pdf"',
        f'id="file-frame" data-file-base-url="{BASE}"',
        f'id="file-frame" data-file-base-url="{BASE}" data-file-base-url="{BASE}" data-initial-file="law.pdf"',
        f'id="file-frame" data-file-base-url="{BASE}" data-initial-file="other.pdf" data-initial-file="law.pdf"',
    ):
        html = (
            "<title>Official detail</title><h1>Official regulation</h1>" + f"<iframe {attributes}></iframe>"
        )
        result = discover_page(DETAIL, html.encode(), "text/html", recipe())
        assert result["matched"] and result["links"] == []
