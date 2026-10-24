"""Historical fixture builders for version/migration and preview contracts."""

import io

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def merged_header_pdf(*, partial_border=False):
    """A two-row HS header with an actual colspan and rowspan, then numeric data."""
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
        "50 120 400 0.5 re f",
        "50 160 400 0.5 re f",
        "50 200 250 0.5 re f",
        "50 240 400 0.5 re f",
        "50 120 0.5 120 re f",
        "180 120 0.5 20 re f" if partial_border else "180 120 0.5 80 re f",
        "300 120 0.5 120 re f",
        "450 120 0.5 120 re f",
    ]
    # Source object order does not define table row order.
    for x, y, text in (
        (55, 140, "09"),
        (185, 140, "0902.30"),
        (305, 140, "RVC40"),
        (130, 220, "HS Code"),
        (55, 180, "Chapter"),
        (185, 180, "Subheading"),
        (335, 205, "Rule"),
    ):
        commands.append(f"BT /F1 10 Tf {x} {y} Td ({text}) Tj ET")
    stream = DecodedStreamObject()
    stream.set_data("\n".join(commands).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
