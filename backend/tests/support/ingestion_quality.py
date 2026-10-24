"""Historical fixture builders for version/migration and preview contracts."""

import io

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject


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
