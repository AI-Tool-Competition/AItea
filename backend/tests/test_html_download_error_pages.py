import hashlib

import pytest

from app.ingestion import _parse_html, parse_file
from app.ingestion_quality import text_complete
from app.parsing import parse_bounded


@pytest.mark.parametrize(
    "source",
    [
        b"Download url is not correct",
        b"<p>Download url is not correct</p>",
        b"<title>File Download</title><main><p>Download url is not correct</p></main>",
        b"<title>Download url is not correct</title><main><p>Download url is not correct</p></main>",
        b"<main><h1>Download error</h1><p>Download url is not correct</p></main>",
        b"<main><h1>Download url is not correct</h1><p>Download url is not correct</p></main>",
        b"<main><p> DOWNLOAD&nbsp;URL is not correct. </p><p hidden>Legal content.</p></main>",
    ],
)
def test_complete_visible_download_failure_blocks_text_gate_without_discarding_evidence(source):
    manifest = parse_file("official-download.pdf.html", source, "text/html")

    assert manifest["quality"]["error_page_detected"] is True
    assert manifest["quality"]["complete"] is False
    assert text_complete(manifest) is False
    assert manifest["source_hash"] == hashlib.sha256(source).hexdigest()
    assert manifest["html_source"]["source_hash"] == manifest["source_hash"]
    assert manifest["elements"] and manifest["chunks"]
    assert any("not correct" in chunk["text"].casefold() for chunk in manifest["chunks"])
    assert all(chunk["locator"]["kind"] == "html" for chunk in manifest["chunks"])
    assert manifest["html_source"]["external_resources_fetched"] is False
    assert "response_status" not in manifest


@pytest.mark.parametrize(
    "source",
    [
        b'<main><h1>Legal reasons</h1><p>The message "Download url is not correct" is quoted in evidence.</p></main>',
        b"<main><h1>Legal reasons</h1><blockquote>Download url is not correct</blockquote></main>",
        b"<main><blockquote>Download url is not correct</blockquote></main>",
        b"<main><p><q>Download url is not correct</q></p></main>",
        b"<main><p><code>Download url is not correct</code></p></main>",
        b"<main><h1>Download url is not correct</h1><p>This judgment discusses the recorded error message.</p></main>",
        b"<title>Download url is not correct</title><main><p>Article 7 requires retaining original records.</p></main>",
        b"<main><h1>File requirements</h1><p>Download url is not correct</p><p>The parties disputed this message.</p></main>",
        b"<main><p>Download the original file and retain its source URL.</p></main>",
        b"<main><p>The download URL is not correct for the old version; the current annex is available.</p></main>",
    ],
)
def test_legal_reasoning_and_quoted_download_messages_remain_complete(source):
    manifest = parse_file("judgment.html", source, "text/html")

    assert manifest["quality"]["error_page_detected"] is False
    assert manifest["quality"]["complete"] is True
    assert text_complete(manifest) is True
    assert manifest["chunks"]


@pytest.mark.parametrize("title", ["404 Not Found", "403 Forbidden", "Access denied", "Just a moment..."])
def test_existing_error_title_rules_still_block_unrelated_visible_body(title):
    source = f"<title>{title}</title><main><p>The requested document cannot be shown.</p></main>".encode()
    manifest = parse_file("download.html", source, "text/html")

    assert manifest["quality"]["error_page_detected"] is True
    assert manifest["quality"]["complete"] is False
    assert text_complete(manifest) is False


def test_real_27_byte_error_survives_resource_bounded_reparse_with_its_exact_mapping():
    source = b"Download url is not correct"
    assert len(source) == 27
    manifest = parse_bounded("rakernas-2012.pdf.html", source, "text/html")

    assert manifest["quality"]["error_page_detected"] is True
    assert text_complete(manifest) is False
    assert manifest["source_hash"] == "8fa5bdcef7d992e0872539e82a38c1b65769e32a90050882a51493822930ab66"
    assert manifest["chunks"][0]["text"] == source.decode()
    assert manifest["chunks"][0]["locator"]["line_start"] == 1
    assert manifest["chunks"][0]["locator"]["element_start"] == 1


def test_heading_only_download_error_is_flagged_and_retains_existing_no_body_rejection():
    source = b"<main><h1>Download url is not correct</h1></main>"
    document = _parse_html(source)
    assert document.quality["error_page_detected"] is True
    assert document.quality["complete"] is False
    assert document.elements[0].content_text == "Download url is not correct"
    with pytest.raises(ValueError, match="没有可供检索的正文"):
        parse_file("download.html", source, "text/html")
