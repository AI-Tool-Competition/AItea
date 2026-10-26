"""Parser, retrieval and untrusted-source boundary regression tests."""

from __future__ import annotations

import io
import socket
import unittest
import zipfile
from unittest.mock import patch

from app.contexts.documents.domain import text
from app.contexts.documents.infrastructure.parsers import native_parser as native
from app.shared.infrastructure.http import download as transport


class IngestionTests(unittest.TestCase):
    def test_markdown_sections_and_verbatim_locations(self):
        result = native.parse_file(
            "法规.md", "# 茶叶资料\n## 包装\n每袋净含量为250克。\n\n## 成分\n成分为茶叶。".encode()
        )
        self.assertEqual(len(result["chunks"]), 2)
        self.assertEqual(result["chunks"][0]["section_path"], ["茶叶资料", "包装"])
        self.assertEqual(result["chunks"][0]["locator"]["line_start"], 3)
        self.assertIn("250克", result["chunks"][0]["text"])
        self.assertEqual(result["retrieval_mode"], "lexical")

    def test_secondary_is_never_parsed(self):
        with self.assertRaisesRegex(ValueError, "secondary"):
            native.parse_file("attachment.txt", b"must not index", role="secondary")

    def test_empty_and_oversize_input_rejected(self):
        for content in (b"", b"x" * (20 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                native.parse_file("file.txt", content)

    def test_html_ignores_active_content_preserves_section_locator(self):
        result = native.parse_file(
            "law.html",
            b'<html><h1>Law</h1><script>secret</script><style>hidden</style><h2 id="a">Article 1</h2><p>Tea <b>label</b> is required.</p><p>Keep records.</p></html>',
        )
        text = " ".join(c["text"] for c in result["chunks"])
        self.assertNotIn("secret", text)
        self.assertNotIn("hidden", text)
        self.assertIn("Tea label is required.", text)
        self.assertEqual(result["chunks"][0]["locator"]["kind"], "html")
        self.assertEqual(result["chunks"][0]["section_path"], ["Law", "Article 1"])

    def test_docx_is_deferred_to_mineru_without_local_text_extraction(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr(
                "word/document.xml",
                """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>产品信息</w:t></w:r></w:p><w:p><w:r><w:t>净含量250克</w:t></w:r></w:p></w:body></w:document>""",
            )
        with patch.object(native, "_parse_native_text", side_effect=AssertionError("local DOCX parsing")):
            result = native.parse_file("product.docx", data.getvalue())
        self.assertEqual(result["parser"], "mineru")
        self.assertFalse(result["quality"]["complete"])
        self.assertEqual(result["chunks"], [])
        self.assertEqual(result["remote_parse"]["state"], "planned")

    def test_unsupported_format_rejected_and_docx_is_deferred_without_xml_expansion(self):
        with self.assertRaises(ValueError):
            native.parse_file("unsupported.exe", b"not a document")
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("word/document.xml", '<!DOCTYPE test [<!ENTITY x "oops">]><test>&x;</test>')
        with patch.object(native, "_parse_native_text", side_effect=AssertionError("local XML parsing")):
            result = native.parse_file("bad.docx", data.getvalue())
        self.assertEqual(result["parser"], "mineru")
        self.assertEqual(result["chunks"], [])
        self.assertEqual(result["remote_parse"]["state"], "planned")

    def test_jet4_mdb_uses_the_read_only_structured_parser(self):
        expected = {"parser": "mdb-libmdb-storage", "chunks": []}
        content = b"\x00\x01\x00\x00Standard Jet DB" + b"x" * 64
        with patch(
            "app.contexts.documents.infrastructure.parsers.mdb_parsing.parse_mdb",
            return_value=expected,
        ) as parse_mdb:
            for filename, mime_type in (
                ("tariff.mdb", "application/msaccess"),
                ("archive.bin", "application/vnd.ms-access; charset=binary"),
            ):
                with self.subTest(filename=filename, mime_type=mime_type):
                    self.assertIs(native.parse_file(filename, content, mime_type), expected)
                    parse_mdb.assert_called_with(content, filename=filename)

    def test_accdb_stays_outside_the_audited_jet4_parser(self):
        with patch(
            "app.contexts.documents.infrastructure.parsers.mdb_parsing.parse_mdb",
            side_effect=AssertionError("ACCDB must not enter the Jet4 parser"),
        ):
            with self.assertRaisesRegex(ValueError, "ACCDB 格式尚无经过审计"):
                native.parse_file("archive.accdb", b"stored database bytes", "application/octet-stream")

    def test_docx_compressed_xml_bomb_is_never_expanded_locally(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", b"x" * (16 * 1024 * 1024 + 1))
        self.assertLess(len(data.getvalue()), 100000)
        with patch.object(native, "_parse_native_text", side_effect=AssertionError("local XML expansion")):
            result = native.parse_file("compressed.docx", data.getvalue())
        self.assertEqual(result["parser"], "mineru")
        self.assertEqual(result["chunks"], [])
        self.assertEqual(result["remote_parse"]["state"], "planned")

    def test_chinese_retrieval_preserves_provenance_and_no_arbitrary_hits(self):
        chunks = [
            {"id": "a", "text": "茶叶包装需要注明净含量。", "document_id": "d1", "locator": {"page": 2}},
            {"id": "b", "text": "物流运输的装箱单。", "document_id": "d2"},
        ]
        hits = text.lexical_search("包装净含量", chunks)
        self.assertEqual(hits[0]["document_id"], "d1")
        self.assertEqual(hits[0]["locator"], {"page": 2})
        self.assertEqual(text.lexical_search("quantum astronomy", chunks), [])
        self.assertNotIn("score", chunks[0])

    def test_fetch_rejects_private_addresses_and_unsafe_urls_before_connect(self):
        for url in (
            "http://example.com/a",
            "https://127.0.0.1/a",
            "https://[::1]/a",
            "https://user:pass@example.com/a",
            "https://example.com:8443/a",
            "https://example.com/a\r\nX: y",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                transport.fetch_source(url)

    def test_fetch_rejects_mixed_public_private_dns(self):
        addresses = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ]
        with (
            patch("socket.getaddrinfo", return_value=addresses),
            self.assertRaisesRegex(ValueError, "public"),
        ):
            transport.fetch_source("https://example.com/law.pdf")

    def test_dns_deadline_prevents_indefinite_waiting(self):
        import time

        with self.assertRaisesRegex(ValueError, "时间"):
            transport._public_addresses("example.com", deadline=time.monotonic() - 1)

    def test_pdf_is_deferred_to_mineru_without_local_text_extraction(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        writer = PdfWriter()
        page = writer.add_blank_page(width=612, height=792)
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
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 12 Tf 50 700 Td (Tea product 250 grams) Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        output = io.BytesIO()
        writer.write(output)
        with patch.object(native, "_parse_pdf", side_effect=AssertionError("local PDF parsing")):
            result = native.parse_file("product.pdf", output.getvalue())
        self.assertEqual(result["parser"], "mineru")
        self.assertFalse(result["quality"]["complete"])
        self.assertEqual(result["chunks"], [])
        self.assertEqual(result["remote_parse"]["state"], "planned")

    def test_blank_pdf_is_deferred_instead_of_locally_marked_complete(self):
        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        output = io.BytesIO()
        writer.write(output)
        with patch.object(native, "_parse_pdf", side_effect=AssertionError("local PDF parsing")):
            result = native.parse_file("scan.pdf", output.getvalue())
        self.assertEqual(result["chunks"], [])
        self.assertEqual(result["parser"], "mineru")
        self.assertFalse(result["quality"]["complete"])
        self.assertEqual(result["remote_parse"]["state"], "planned")

    def test_only_same_origin_direct_attachments_are_fetched(self):
        fetched = []

        def download(url, **kwargs):
            fetched.append((url, kwargs))
            if len(fetched) == 1:
                return {
                    "url": url,
                    "filename": "law.html",
                    "mime_type": "text/html",
                    "content": b'<a href="/one.pdf">1</a><a href="https://external.example/two.pdf">2</a><a href="/page.html">page</a><a href="/one.pdf">duplicate</a><a href="/three.docx">3</a>',
                }
            return {
                "url": url,
                "filename": "one.pdf",
                "mime_type": "application/pdf",
                "content": b"raw-secondary",
            }

        with patch.object(transport, "_download", download):
            result = transport.fetch_source("https://example.com/law.html")
        self.assertEqual(
            [x["url"] for x in result["attachments"]],
            ["https://example.com/one.pdf", "https://example.com/three.docx"],
        )
        self.assertTrue(all(call[1]["required_host"] == "example.com" for call in fetched[1:]))
        self.assertTrue(all("chunks" not in attachment for attachment in result["attachments"]))

    def test_redirect_to_internal_ip_is_rejected(self):
        import time

        class Connection:
            def __init__(self, *args):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                from types import SimpleNamespace

                return SimpleNamespace(
                    status=302,
                    getheader=lambda name: "https://127.0.0.1/secret",
                    close=lambda: None,
                )

            def close(self):
                pass

        with (
            patch.object(
                transport,
                "_public_addresses",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
            ),
            patch.object(transport, "_PinnedHTTPSConnection", Connection),
            self.assertRaisesRegex(ValueError, "public"),
        ):
            transport._download("https://example.com/law", deadline=time.monotonic() + 10)

    def test_dynamic_download_url_uses_actual_document_mime(self):
        import time

        class Response:
            status = 200

            def __init__(self):
                self.body = io.BytesIO(b"%PDF-content")

            def getheader(self, name, default=None):
                return {"Content-Type": "application/pdf"}.get(name, default)

            def read1(self, limit):
                return self.body.read(limit)

            def close(self):
                self.body.close()

        class Connection:
            sock = None

            def __init__(self, *args):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                return Response()

            def close(self):
                pass

        with (
            patch.object(
                transport,
                "_public_addresses",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
            ),
            patch.object(transport, "_PinnedHTTPSConnection", Connection),
        ):
            result = transport._download(
                "https://example.com/transport.aspx?id=1", deadline=time.monotonic() + 10
            )
        self.assertTrue(result["filename"].endswith(".pdf"))


if __name__ == "__main__":
    unittest.main()
