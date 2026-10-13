"""Native grids need complete cell geometry and independent text agreement."""

import hashlib
import io

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def grid_pdf(*, overflow=False, broken_grid=False, product="0902.30.00"):
    writer = PdfWriter()
    page = writer.add_blank_page(width=500, height=500)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    commands = [
        "50 200 350 0.5 re f",
        "50 240 350 0.5 re f",
        "50 280 350 0.5 re f",
        "50 200 0.5 80 re f",
        "400 200 0.5 80 re f",
    ]
    commands.append("230 200 0.5 40 re f" if broken_grid else "230 200 0.5 80 re f")
    # Headers are deliberately placed after the body in the object stream.
    for x, y, text in [
        (55, 220, product),
        (235, 220, "5.0%"),
        (55, 260, "HS Code"),
        (235, 260, "Year 2027"),
        (55, 310, "Official schedule"),
    ]:
        if overflow and text == "5.0%":
            x = 223
        commands.append(f"BT /F1 10 Tf {x} {y} Td ({text}) Tj ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_real_grid_reorders_cells_by_position_and_keeps_every_digit():
    from app.ingestion import parse_file

    result = parse_file("tariffs.pdf", grid_pdf())
    assert result["quality"]["complete"]
    page = result["pages"][0]
    assert "| HS Code | Year 2027 |" in page["text"]
    assert "| 0902.30.00 | 5.0% |" in page["text"]
    assert page["text"].index("HS Code") < page["text"].index("0902.30.00")
    assert page["native_table"]["columns"] == 2 and page["native_table"]["rows"] == 2
    assert page["native_table"]["source_hash"] == hashlib.sha256(grid_pdf()).hexdigest()
    assert page["native_table"]["page"] == 1
    assert any(element["kind"] == "table" for element in result["elements"])


def test_a_cell_crossing_column_border_still_requires_ocr():
    from app.ingestion import parse_file

    result = parse_file("crossed.pdf", grid_pdf(overflow=True))
    assert not result["quality"]["complete"]
    assert "native_table" not in result["pages"][0]


def test_merged_or_incomplete_grid_still_requires_ocr():
    from app.ingestion import parse_file

    result = parse_file("merged.pdf", grid_pdf(broken_grid=True))
    assert not result["quality"]["complete"]


def test_independent_reader_disagreement_cannot_be_marked_complete():
    import pypdfium2 as pdfium
    from app.pdf_tables import extract_grid

    content = grid_pdf()
    native = PdfReader(io.BytesIO(content)).pages[0].extract_text()
    with pdfium.PdfDocument(content) as document:
        page = document[0]
        try:
            assert extract_grid(page, native.replace("5.0%", "6.0%")) is None
        finally:
            page.close()


def test_pdfium_discretionary_hyphen_is_preserved_only_when_its_api_confirms_it(monkeypatch):
    import pypdfium2 as pdfium
    from app.ingestion import parse_file

    content = grid_pdf(product="green-tea")
    with pdfium.PdfDocument(content) as document:
        page = document[0]
        text = page.get_textpage()
        index = next(i for i in range(text.count_chars()) if text.get_text_range(i, 1) == "-")
        text.close()
        page.close()
    original = pdfium.PdfTextPage.get_text_range

    def encoded(self, start=0, count=-1, **kwargs):
        return "\ufffe" if start == index and count == 1 else original(self, start, count, **kwargs)

    monkeypatch.setattr(pdfium.PdfTextPage, "get_text_range", encoded)
    monkeypatch.setattr(pdfium.raw, "FPDFText_IsHyphen", lambda _t, i: int(i == index))
    result = parse_file("tariffs.pdf", content)
    assert result["quality"]["complete"] and "green-tea" in result["pages"][0]["text"]
    assert result["pages"][0]["native_table"]["discretionary_hyphens"] == 1
    monkeypatch.setattr(pdfium.raw, "FPDFText_IsHyphen", lambda *_: 0)
    assert not parse_file("unmapped.pdf", content)["quality"]["complete"]
