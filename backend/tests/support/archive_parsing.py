"""Historical fixture builders for version/migration and preview contracts."""

from __future__ import annotations

import hashlib
import io

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def pdf_file(tmp_path, page_texts, *, encrypted=False):
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    reference = writer._add_object(font)
    for text in page_texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): reference})}
        )
        if text:
            commands = [b"BT /F1 12 Tf 50 700 Td"]
            for line in text.splitlines():
                escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                commands.append(f"({escaped}) Tj 0 -14 Td".encode("ascii"))
            commands.append(b"ET")
            stream = DecodedStreamObject()
            stream.set_data(b" ".join(commands))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("not-the-password")
    buffer = io.BytesIO()
    writer.write(buffer)
    content = buffer.getvalue()
    path = tmp_path / "law.pdf"
    path.write_bytes(content)
    return path, hashlib.sha256(content).hexdigest(), len(content)
