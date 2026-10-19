"""Bounded recovery for BKPM's observed multipart upload saved as a PDF.

This handles a provider-specific representation, not an arbitrary PDF prefix.
RFC 2046 boundary framing must be complete and unambiguous. The caller retains
the untouched HTTP entity and records the exact byte range of the recovered PDF.
"""

from __future__ import annotations

import hashlib
import re
from email import policy
from email.errors import MessageError
from email.parser import BytesHeaderParser
from urllib.parse import urlsplit

MAX_ENVELOPE_BYTES = 4 * 1024 * 1024
MAX_HEADER_BYTES = 8192
_BOUNDARY = re.compile(rb"----WebKitFormBoundary[A-Za-z0-9]{1,40}")


def extract_bkpm_multipart_pdf(url: str, body: bytes):
    """Return exact PDF bytes and offsets, or None for an unrelated response.

    A matching but malformed envelope raises ValueError. Unknown parts, multiple
    files, transfer encodings and missing end boundaries remain acquisition gaps.
    URL allowlisting by the immutable source recipe is also required at the caller.
    """
    target = urlsplit(url)
    if (
        target.scheme != "https"
        or target.netloc != "jdih-storage.bkpm.go.id"
        or not target.path.startswith("/jdih/jdih/")
        or target.query
        or target.fragment
        or not body.startswith(b"------WebKitFormBoundary")
    ):
        return None
    if len(body) > MAX_ENVELOPE_BYTES:
        raise ValueError("multipart_response_exceeds_bound")
    first_end = body.find(b"\r\n", 0, 74)
    boundary = body[2:first_end]
    if first_end < 0 or not _BOUNDARY.fullmatch(boundary):
        raise ValueError("multipart_boundary_invalid")
    delimiter = b"\r\n--" + boundary
    cursor, parts, pdf, pdf_part = first_end + 2, [], None, None
    while True:
        if len(parts) >= 4:
            raise ValueError("multipart_too_many_parts")
        header_end = body.find(b"\r\n\r\n", cursor, cursor + MAX_HEADER_BYTES + 4)
        if header_end < 0:
            raise ValueError("multipart_headers_incomplete")
        raw_headers = body[cursor:header_end]
        if any(byte < 32 and byte not in (9, 10, 13) for byte in raw_headers):
            raise ValueError("multipart_headers_invalid")
        try:
            headers = BytesHeaderParser(policy=policy.strict).parsebytes(raw_headers + b"\r\n\r\n")
            names = [name.lower() for name in headers.keys()]
            name = headers.get_param("name", header="content-disposition")
            filename = headers.get_filename()
            if (
                headers.defects
                or len(names) != len(set(names))
                or set(names) - {"content-disposition", "content-type", "content-transfer-encoding"}
                or headers.get_content_disposition() != "form-data"
                or headers.get("Content-Transfer-Encoding", "binary").lower() not in {"binary", "8bit"}
                or name not in {"file", "customPath"}
                or name in {part["name"] for part in parts}
            ):
                raise ValueError("multipart_headers_invalid")
        except (MessageError, TypeError, AttributeError, UnicodeError) as exc:
            raise ValueError("multipart_headers_invalid") from exc
        start = header_end + 4
        end = body.find(delimiter, start)
        if end < 0:
            raise ValueError("multipart_end_boundary_missing")
        content = body[start:end]
        if name == "file":
            if (
                not isinstance(filename, str)
                or len(filename) > 255
                or any(char in filename for char in ("/", "\\"))
                or any(ord(char) < 32 for char in filename)
                or not filename.lower().endswith(".pdf")
                or headers.get_content_type() != "application/pdf"
                or not content.startswith(b"%PDF-")
                or not content.rstrip().endswith(b"%%EOF")
            ):
                raise ValueError("multipart_pdf_part_invalid")
            pdf, pdf_part = content, len(parts)
        elif filename is not None or len(content) > 4096 or headers.get_content_type() != "text/plain":
            raise ValueError("multipart_auxiliary_part_invalid")
        parts.append(
            {
                "name": name,
                "byte_offset": start,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
        cursor = end + len(delimiter)
        if body[cursor:] in (b"--", b"--\r\n"):
            break
        if body[cursor : cursor + 2] != b"\r\n":
            raise ValueError("multipart_boundary_invalid")
        cursor += 2
    if pdf is None:
        raise ValueError("multipart_pdf_part_missing")
    return pdf, {
        "protocol": "bkpm-multipart-pdf-v1",
        "boundary": boundary.decode("ascii"),
        "container_sha256": hashlib.sha256(body).hexdigest(),
        "container_size": len(body),
        "pdf_part": pdf_part,
        "parts": parts,
    }
