import hashlib

import pytest

from app.source_envelopes import MAX_ENVELOPE_BYTES, extract_bkpm_multipart_pdf

URL = "https://jdih-storage.bkpm.go.id/jdih/jdih/2026-law.pdf"
BOUNDARY = b"----WebKitFormBoundaryAbc123"
PDF = b"%PDF-1.7\noriginal\x00binary\n%%EOF\r\n"


def envelope(pdf=PDF):
    return (
        b"--" + BOUNDARY + b'\r\nContent-Disposition: form-data; name="file"; filename="law.pdf"'
        b"\r\nContent-Type: application/pdf\r\n\r\n"
        + pdf
        + b"\r\n--"
        + BOUNDARY
        + b'\r\nContent-Disposition: form-data; name="customPath"\r\n\r\noriginal/archive'
        b"\r\n--" + BOUNDARY + b"--\r\n"
    )


def test_envelope_keeps_exact_binary_pdf_and_byte_mapping():
    body = envelope()
    pdf, proof = extract_bkpm_multipart_pdf(URL, body)
    assert pdf == PDF
    assert proof["container_sha256"] == hashlib.sha256(body).hexdigest()
    for part in proof["parts"]:
        raw = body[part["byte_offset"] : part["byte_offset"] + part["size"]]
        assert hashlib.sha256(raw).hexdigest() == part["sha256"]
    assert proof["parts"][proof["pdf_part"]]["sha256"] == hashlib.sha256(PDF).hexdigest()


@pytest.mark.parametrize(
    "mutation",
    [
        "truncated",
        "no_eof",
        "epilogue",
        "second_file",
        "unknown_part",
        "encoding",
        "path",
        "duplicate_header",
        "oversized",
    ],
)
def test_ambiguous_or_incomplete_envelope_never_becomes_an_original(mutation):
    body = envelope()
    if mutation == "truncated":
        body = body[: -len(BOUNDARY) - 6]
    elif mutation == "no_eof":
        body = envelope(PDF.replace(b"%%EOF", b"broken"))
    elif mutation == "epilogue":
        body += b"unexpected bytes"
    elif mutation == "second_file":
        body = body.replace(b'name="customPath"', b'name="file"; filename="second.pdf"')
    elif mutation == "unknown_part":
        body = body.replace(b'name="customPath"', b'name="unknown"')
    elif mutation == "encoding":
        body = body.replace(
            b"Content-Type: application/pdf",
            b"Content-Type: application/pdf\r\nContent-Transfer-Encoding: base64",
        )
    elif mutation == "path":
        body = body.replace(b'filename="law.pdf"', b'filename="../../law.pdf"')
    elif mutation == "duplicate_header":
        body = body.replace(
            b"Content-Type: application/pdf", b"Content-Type: application/pdf\r\nContent-Type: text/html"
        )
    else:
        body += b" " * MAX_ENVELOPE_BYTES
    with pytest.raises(ValueError):
        extract_bkpm_multipart_pdf(URL, body)


@pytest.mark.parametrize(
    "url", ["https://example.com/jdih/jdih/law.pdf", URL + "?token=x", URL.replace("https:", "http:")]
)
def test_unrelated_sources_cannot_enable_representation_recovery(url):
    assert extract_bkpm_multipart_pdf(url, envelope()) is None


def test_arbitrary_prefix_before_pdf_is_not_an_envelope():
    assert extract_bkpm_multipart_pdf(URL, b"Access denied\n" + PDF) is None
