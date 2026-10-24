"""Historical fixture builders for version/migration and preview contracts."""

from __future__ import annotations

import io
import zipfile

from app.contexts.knowledge.infrastructure.parsing import docx_parsing as docx_parsing
from app.contexts.knowledge.infrastructure.parsing.docx_parsing import PR, R, W


def _p(text):
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _package(body, *, parts=None, relations=None):
    members = {
        "word/document.xml": f'<w:document xmlns:w="{W}" xmlns:r="{R}"><w:body>{body}</w:body></w:document>'.encode()
    }
    members.update(parts or {})
    if relations:
        members["word/_rels/document.xml.rels"] = (
            f'<Relationships xmlns="{PR}">'
            + "".join(
                f'<Relationship Id="{rid}" Type="{R}/{kind}" Target="{target}"'
                + (f' TargetMode="{mode}"' if mode else "")
                + "/>"
                for rid, kind, target, mode in relations
            )
            + "</Relationships>"
        ).encode()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in members.items():
            archive.writestr(name, value)
    return buffer.getvalue()
