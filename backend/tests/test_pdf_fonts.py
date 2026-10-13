"""Embedded CFF encodings preserve letters and digits in bounded PDF parsing."""

import hashlib
import io

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app.parsing import parse_bounded


def cff_pdf():
    builder = FontBuilder(1000, isTTF=False)
    names = [".notdef", "T", "e", "a", "space", "one", "two", "three"]
    builder.setupGlyphOrder(names)
    strings = {}
    for name in names:
        pen = T2CharStringPen(600, None)
        pen.moveTo((50, 50))
        pen.lineTo((50, 700))
        pen.lineTo((400, 700))
        pen.lineTo((400, 50))
        pen.closePath()
        strings[name] = pen.getCharString()
    builder.setupCFF(
        "LegalTest", {"FullName": "LegalTest", "FamilyName": "LegalTest", "Weight": "Regular"}, strings, {}
    )
    encoding = [".notdef"] * 256
    for code, name in enumerate(names[1:], 1):
        encoding[code] = name
    builder.font["CFF "].cff.topDictIndex[0].Encoding = encoding
    program = io.BytesIO()
    builder.font["CFF "].cff.compile(program, builder.font)
    writer = PdfWriter()
    embedded = DecodedStreamObject()
    embedded.set_data(program.getvalue())
    embedded[NameObject("/Subtype")] = NameObject("/Type1C")
    descriptor = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/FontDescriptor"),
            NameObject("/FontName"): NameObject("/LegalTest"),
            NameObject("/Flags"): NumberObject(32),
            NameObject("/FontBBox"): ArrayObject([NumberObject(n) for n in (0, 0, 1000, 1000)]),
            NameObject("/Ascent"): NumberObject(800),
            NameObject("/Descent"): NumberObject(-200),
            NameObject("/CapHeight"): NumberObject(700),
            NameObject("/ItalicAngle"): NumberObject(0),
            NameObject("/StemV"): NumberObject(80),
            NameObject("/FontFile3"): writer._add_object(embedded),
        }
    )
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/LegalTest"),
            NameObject("/Encoding"): NameObject("/StandardEncoding"),
            NameObject("/FirstChar"): NumberObject(1),
            NameObject("/LastChar"): NumberObject(7),
            NameObject("/Widths"): ArrayObject([NumberObject(600)] * 7),
            NameObject("/FontDescriptor"): writer._add_object(descriptor),
        }
    )
    page = writer.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 50 700 Td <01020304050607> Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_font_extra_recovers_custom_cff_letters_and_digits_in_isolated_parser(monkeypatch):
    from pypdf.generic import _font

    content = cff_pdf()
    with monkeypatch.context() as without:
        without.setattr(_font, "HAS_FONTTOOLS", False)
        assert PdfReader(io.BytesIO(content)).pages[0].extract_text().strip() != "Tea 123"
    parsed = parse_bounded("encoded-law.pdf", content, "application/pdf")
    assert parsed["source_hash"] == hashlib.sha256(content).hexdigest()
    assert "Tea 123" in parsed["chunks"][0]["text"]
    assert parsed["chunks"][0]["locator"]["page"] == 1
