import io

import pytest

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app.ingestion import _decode, parse_file
from app.vendor.geo_parser.document_ingestion import ParsedDocument, ParsedElement, build_manifest


def make_pdf(pages: list[tuple[str, bool]]) -> bytes:
    """Small deterministic PDFs; images are actual full-page 1-bit raster objects."""
    writer = PdfWriter()
    for text, scanned in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        operations = b""
        if scanned:
            image = DecodedStreamObject()
            image.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(1200),
                    NameObject("/Height"): NumberObject(1200),
                    NameObject("/BitsPerComponent"): NumberObject(1),
                    NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                }
            )
            image.set_data(b"\xff" * 180000)
            resources[NameObject("/XObject")] = DictionaryObject(
                {NameObject("/Im0"): writer._add_object(image)}
            )
            operations += b"q 612 0 0 792 0 0 cm /Im0 Do Q\n"
        if text:
            operations += b"BT /F1 12 Tf 50 730 Td\n"
            for line in text.splitlines():
                encoded = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").encode("latin-1")
                operations += b"(" + encoded + b") Tj 0 -16 Td\n"
            operations += b"ET"
        stream = DecodedStreamObject()
        stream.set_data(operations)
        page[NameObject("/Resources")] = resources
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_declared_html_legacy_charset_preserves_accented_text():
    content = (
        '<meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1"><p>Fran\xe7ais</p>'.encode(
            "latin-1"
        )
    )
    assert "Français" in _decode(content)
    assert "Français" in parse_file("wto.html", content)["chunks"][0]["text"]


def test_html_keeps_article_title_and_publication_details_inside_main():
    content = b"""<html><header>SITE_MENU</header><main><article>
<header><h1 id="law">Food Regulations</h1><p>Issued on 9 October 2026</p></header>
<p>Article 1. Keep the original evidence.</p>
<footer><p>Published by the official regulator.</p></footer>
</article></main><footer>SITE_FOOTER</footer></html>"""
    result = parse_file("law.html", content, "text/html")
    text = " ".join(element["content_text"] for element in result["elements"])
    assert "Food Regulations" in text and "9 October 2026" in text
    assert "official regulator" in text
    assert "SITE_MENU" not in text and "SITE_FOOTER" not in text
    heading = result["elements"][0]
    assert heading["locator"]["anchor"] == "law"
    assert heading["locator"]["line_start"] == 2
    assert result["elements"][1]["section_path"] == ["Food Regulations"]


@pytest.mark.parametrize(
    "decoy",
    [
        b"<!-- <main>template marker</main> -->",
        b'<script>const template = "<main>template marker</main>";</script>',
        b"<template><main>template marker</main></template>",
    ],
)
def test_html_template_main_does_not_hide_the_actual_document(decoy):
    content = b"<html>" + decoy + b"<h1>Official requirements</h1><p>Article 1. Retain records.</p></html>"
    result = parse_file("law.html", content, "text/html")
    text = " ".join(element["content_text"] for element in result["elements"])
    assert "Official requirements" in text and "Retain records" in text
    assert "template marker" not in text


def test_scan_with_nonempty_broken_text_layer_requires_ocr():
    result = parse_file("scan.pdf", make_pdf([("Pasal 1\nPerjanjian tahun 2OL4", True)]))
    assert result["quality"]["complete"] is False
    assert result["quality"]["pending_pages"] == [1]
    assert result["pages"][0]["native_text"].endswith("2OL4")
    assert result["pages"][0]["status"] == "pending_ocr"
    assert "large_raster" in result["pages"][0]["reasons"]


def test_mixed_pdf_retains_missing_page_state():
    result = parse_file("mixed.pdf", make_pdf([("Readable page", False), ("", True)]))
    assert result["quality"]["page_count"] == 2
    assert result["quality"]["completed_pages"] == [1]
    assert result["quality"]["pending_pages"] == [2]
    assert not result["quality"]["complete"]
    assert len(result["pages"]) == 2


def test_image_only_pdf_returns_pending_manifest_without_fabricated_chunks():
    result = parse_file("scan.pdf", make_pdf([("", True)]))
    assert result["chunks"] == []
    assert result["quality"]["pending_pages"] == [1]


def test_operative_and_explanation_articles_have_separate_paths():
    result = parse_file(
        "law.pdf",
        make_pdf(
            [
                (
                    "UNDANG-UNDANG REPUBLIK INDONESIA\nPasal 1\nKetentuan pertama.\nPasal 2\nKetentuan kedua.",
                    False,
                ),
                (
                    "PENJELASAN\nATAS UNDANG-UNDANG\nII. PASAL DEMI PASAL\nPasal 1\nCukup jelas.\nPasal 2\nCukup jelas.",
                    False,
                ),
            ]
        ),
    )
    paths = [row["section_path"] for row in result["chunks"]]
    assert ["正文", "Pasal 1"] in paths
    assert ["正文", "Pasal 2"] in paths
    assert ["PENJELASAN", "Pasal 1"] in paths
    assert ["PENJELASAN", "Pasal 2"] in paths
    assert result["quality"]["complete"]


def test_forced_split_preserves_contiguous_text_and_paragraph_separators():
    text = "\n\n".join(f"Pasal {i}\nKetentuan pertama. Ketentuan kedua tanpa titik" for i in range(120))
    document = ParsedDocument(
        "test", "1", "1", [ParsedElement("one", "paragraph", 0, text, {"kind": "text"}, [])], [], []
    )
    manifest, _ = build_manifest(document, source_hash="0" * 64)
    assert len(manifest["chunks"]) > 1
    assert all(chunk["content_text"] in text for chunk in manifest["chunks"])
    assert any(". Ketentuan" in chunk["content_text"] for chunk in manifest["chunks"])


def test_court_quoted_articles_are_not_assigned_law_article_hierarchy():
    result = parse_file(
        "case.pdf",
        make_pdf(
            [("PUTUSAN\nNOMOR 164/PUU-XXIV/2026\nQuoted law:\nPasal 1\nKetentuan yang dimohonkan.", False)]
        ),
    )
    assert all(not row["locator"].get("article") for row in result["elements"])


def test_vector_grid_table_page_requires_layout_transcription():
    reader = PdfReader(io.BytesIO(make_pdf([("Header A Header B Row 1 Row 2", False)])))
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    stream = DecodedStreamObject()
    stream.set_data(
        page.get_contents().get_data()
        + b"\n50 100 400 0.5 re f\n50 200 400 0.5 re f\n50 300 400 0.5 re f\n50 100 0.5 200 re f\n250 100 0.5 200 re f\n450 100 0.5 200 re f"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    result = parse_file("grid.pdf", out.getvalue())
    assert result["quality"]["pending_pages"] == [1]
    assert "table_layout" in result["pages"][0]["reasons"]


@pytest.mark.parametrize("end_path", [b"n", b"W n", b"W* n"])
def test_unpainted_clipping_paths_are_not_visible_table_rules(end_path):
    reader = PdfReader(io.BytesIO(make_pdf([("Plain legal paragraphs", False)])))
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    stream = DecodedStreamObject()
    stream.set_data(
        page.get_contents().get_data()
        + b"\nq\n50 100 m 450 100 l\n50 200 m 450 200 l\n50 300 m 450 300 l\n"
        + b"50 100 m 50 300 l\n250 100 m 250 300 l\n450 100 m 450 300 l\n"
        + end_path
        + b"\nQ"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    result = parse_file("clipping.pdf", out.getvalue())
    assert "table_layout" not in result["pages"][0]["reasons"]
    assert result["quality"]["complete"]


@pytest.mark.parametrize("layout", ["note_boxes", "flowchart"])
def test_independent_painted_boxes_are_not_a_table_grid(layout):
    reader = PdfReader(io.BytesIO(make_pdf([("Official explanation with readable original text.", False)])))
    writer = PdfWriter()
    page = writer.add_page(reader.pages[0])
    if layout == "note_boxes":
        boxes = [(x, y, 400, 60) for y in (100, 250, 400) for x in (50, 51)]
    else:
        boxes = [(50, 100, 100, 80), (220, 100, 100, 80), (390, 100, 100, 80), (220, 300, 100, 80)]
    rules = []
    for x, y, width, height in boxes:
        rules += [
            f"{x} {y} {width} 0.5 re f",
            f"{x} {y + height} {width} 0.5 re f",
            f"{x} {y} 0.5 {height} re f",
            f"{x + width} {y} 0.5 {height} re f",
        ]
    stream = DecodedStreamObject()
    stream.set_data(page.get_contents().get_data() + b"\n" + "\n".join(rules).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    result = parse_file("explanation.pdf", output.getvalue())
    assert result["quality"]["complete"]
    assert "table_layout" not in result["pages"][0]["reasons"]


def test_transcribed_markdown_table_remains_a_table_element():
    from app.ingestion import rebuild_pdf_manifest

    result = parse_file("table.pdf", make_pdf([("raw table", False)]))
    result["pages"][0].update(
        text="Pasal 1\nTable:\n| Product | Limit |\n| --- | --- |\n| Tea | 20 |\n\nNext paragraph.",
        status="ocr_complete",
    )
    result = rebuild_pdf_manifest(result)
    tables = [row for row in result["elements"] if row["kind"] == "table"]
    assert len(tables) == 1
    assert "| Tea | 20 |" in tables[0]["content_text"]
    assert tables[0]["locator"]["page"] == 1
