"""Complete-package provenance and fail-closed OOXML parsing regressions."""

from __future__ import annotations

import hashlib
import io
import struct
import zipfile
import zlib

import pytest

from app import docx_parsing
from app.docx_parsing import PR, R, W, parse_docx


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


def _part(root, text):
    return f'<w:{root} xmlns:w="{W}">{text}</w:{root}>'.encode()


@pytest.mark.parametrize("quality", [None, {"complete": True}])
def test_old_body_only_cache_cannot_claim_complete_package(quality):
    from app.ingestion_quality import text_complete

    old = {
        "parser": "docx-basic",
        "parser_version": "1",
        "pipeline_version": "docx-paragraphs-v1",
        "chunks": [{"text": "Body without header or footnote evidence"}],
    }
    if quality is not None:
        old["quality"] = quality
    assert not text_complete(old)
    assert text_complete(parse_docx(_package(_p("Verified native package"))))


def test_simple_native_text_keeps_literal_order_and_real_source_anchors():
    content = _package(
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Article 1</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>Tea</w:t><w:tab/><w:t>250</w:t><w:br/><w:t>grams</w:t></w:r></w:p>"
    )
    result = parse_docx(content, filename="law.docx")
    assert result["quality"]["complete"] is True
    assert result["elements"][1]["content_text"] == "Tea\t250\ngrams"
    assert result["chunks"][0]["section_path"] == ["Article 1"]
    locator = result["chunks"][0]["locator"]
    assert locator["paragraph_start"] == 2
    assert locator["package_part"] == "word/document.xml"
    assert locator["source_hash"] == hashlib.sha256(content).hexdigest()
    assert locator["part_hash"] == result["package_parts"][0]["sha256"]
    assert locator["xml_path"].endswith("}p[2]")
    assert locator["source_anchors"][0]["xml_path"] == locator["xml_path"]
    assert "pages" not in result and "page" not in locator


def test_header_footer_and_both_note_kinds_are_mapped_to_their_own_parts():
    content = _package(
        _p("Body") + '<w:p><w:r><w:footnoteReference w:id="9"/><w:endnoteReference w:id="11"/></w:r></w:p>'
        '<w:sectPr><w:headerReference r:id="h"/><w:footerReference r:id="f"/></w:sectPr>',
        parts={
            "word/header.xml": _part("hdr", _p("Header")),
            "word/footer.xml": _part("ftr", _p("Footer")),
            "word/footnotes.xml": _part(
                "footnotes", '<w:footnote w:id="9">' + _p("Footnote") + "</w:footnote>"
            ),
            "word/endnotes.xml": _part("endnotes", '<w:endnote w:id="11">' + _p("Endnote") + "</w:endnote>"),
        },
        relations=[
            ("h", "header", "header.xml", None),
            ("f", "footer", "footer.xml", None),
            ("fn", "footnotes", "footnotes.xml", None),
            ("en", "endnotes", "endnotes.xml", None),
        ],
    )
    result = parse_docx(content)
    assert result["quality"]["complete"] is True
    assert {e["content_text"] for e in result["elements"]} >= {
        "Body",
        "Header",
        "Footer",
        "Footnote",
        "Endnote",
    }
    refs = {r["reference_kind"]: r for r in result["references"]}
    assert refs["headerReference"]["target_part"] == "word/header.xml"
    assert refs["footnote"]["note_id"] == "9" and refs["footnote"]["status"] == "native_mapped"
    assert not any("Body" in c["text"] and "Header" in c["text"] for c in result["chunks"])
    assert (
        next(c for c in result["chunks"] if c["text"] == "Header")["locator"]["package_part"]
        == "word/header.xml"
    )
    for chunk in result["chunks"]:
        assert {anchor["package_part"] for anchor in chunk["locator"]["source_anchors"]} == {
            chunk["locator"]["package_part"]
        }


def test_rectangular_horizontal_and_vertical_merges_keep_empty_cells():
    body = '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge w:val="restart"/></w:tcPr>'
    body += (
        _p("Merged")
        + '</w:tc></w:tr><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/><w:vMerge/></w:tcPr><w:p/></w:tc></w:tr></w:tbl>'
    )
    result = parse_docx(_package(body))
    assert result["quality"]["complete"] is True
    origin, continuation = result["tables"][0]["cells"]
    assert (origin["row_span"], origin["column_span"]) == (2, 2)
    assert continuation["text"] == "" and continuation["merged_origin"] == {"row": 1, "column": 1}
    assert result["chunks"][0]["locator"]["source_anchors"][0]["column_end"] == 2


@pytest.mark.parametrize(
    "property_xml,reason",
    [
        ("<w:vMerge/>", "unmatched_vertical_merge"),
        ('<w:gridSpan w:val="0"/>', "invalid_table_span"),
        ("<w:hMerge/>", "legacy_horizontal_merge"),
        ("<w:tcBorders><w:tl2br/></w:tcBorders>", "diagonal_table_border"),
    ],
)
def test_unverifiable_table_shapes_block_indexing(property_xml, reason):
    result = parse_docx(
        _package(
            "<w:tbl><w:tblGrid><w:gridCol/></w:tblGrid><w:tr><w:tc><w:tcPr>"
            + property_xml
            + "</w:tcPr>"
            + _p("Literal")
            + "</w:tc></w:tr></w:tbl>"
        )
    )
    assert result["quality"]["complete"] is False
    assert reason in result["quality"]["review_reasons"]
    assert "Literal" in result["chunks"][0]["text"]


def test_field_keeps_cached_text_and_raw_instruction_without_evaluation():
    result = parse_docx(
        _package(
            '<w:p><w:r><w:fldChar w:fldCharType="begin"/><w:instrText> PAGE </w:instrText><w:fldChar w:fldCharType="separate"/><w:t>7</w:t><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        )
    )
    assert result["quality"]["complete"] is False
    assert result["chunks"][0]["text"] == "7"
    assert any(review.get("instruction") == " PAGE " for review in result["review"])
    assert "PAGE" not in result["chunks"][0]["text"]


def test_image_only_package_preserves_structure_and_original_image_mapping():
    png = (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", 2, 3)
        + b"\x08\x02\x00\x00\x00"
        + b"\x00" * 4
    )
    body = '<w:p><w:r><w:drawing><a:blip xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" r:embed="im"/></w:drawing></w:r></w:p>'
    result = parse_docx(
        _package(body, parts={"word/media/im.png": png}, relations=[("im", "image", "media/im.png", None)])
    )
    assert result["chunks"] == [] and result["quality"]["complete"] is False
    assert any(e["kind"] == "figure" and e["content_text"] == "" for e in result["elements"])
    image = result["assets_pending"][0]
    assert image["package_part"] == "word/media/im.png"
    assert image["image_hash"] == hashlib.sha256(png).hexdigest()
    assert (image["width"], image["height"]) == (2, 3) and image["pixels_verified"] is False
    assert image["locator"]["xml_path"].endswith("}drawing[1]")
    assert "content" not in image and "page" not in image["locator"]


def test_external_image_relationship_is_never_fetched(monkeypatch):
    import socket

    monkeypatch.setattr(socket, "socket", lambda *a, **kw: pytest.fail("network access is forbidden"))
    result = parse_docx(
        _package(
            '<w:p><w:r><w:pict r:id="im"/></w:r></w:p>',
            relations=[("im", "image", "https://127.0.0.1/private.png", "External")],
        )
    )
    assert result["relationships"][0]["status"] == "external_not_fetched"
    assert result["objects"][0]["relationships"][0]["target"] == "https://127.0.0.1/private.png"
    assert result["assets_pending"] == [] and result["quality"]["complete"] is False


def test_textbox_text_is_not_duplicated_into_its_containing_paragraph():
    body = (
        "<w:p><w:r><w:t>Outer</w:t><w:pict><w:txbxContent>"
        + _p("Inner")
        + "</w:txbxContent></w:pict></w:r></w:p>"
    )
    result = parse_docx(_package(body))
    assert [e["content_text"] for e in result["elements"] if e["content_text"]] == ["Outer", "Inner"]
    inner = next(e for e in result["elements"] if e["content_text"] == "Inner")
    assert inner["locator"]["source_area"] == "textbox"
    assert result["quality"]["complete"] is False


@pytest.mark.parametrize(
    "body,reason",
    [
        (_p("bad\ufffdtext"), "native_decode_replacement"),
        ("<w:ins>" + _p("Inserted") + "</w:ins>", "unresolved_tracked_revision"),
        ("<w:p><w:pPr><w:numPr/></w:pPr><w:r><w:t>Numbered</w:t></w:r></w:p>", "rendering_dependent_text"),
        (_p("Body") + "<w:t>Unmapped</w:t>", "unmapped_native_text"),
        (_p("Body") + '<w:p><w:r><w:footnoteReference w:id="9"/></w:r></w:p>', "unresolved_note_reference"),
    ],
)
def test_independent_native_warnings_survive_text_extraction(body, reason):
    result = parse_docx(_package(body))
    assert result["quality"]["complete"] is False
    assert reason in result["quality"]["review_reasons"]


def test_unknown_package_parts_and_missing_relationships_are_not_silently_complete():
    result = parse_docx(
        _package(
            _p("Body"),
            parts={"customXml/item.xml": b"<unknown>Literal data</unknown>", "word/blob.bin": b"opaque"},
            relations=[("h", "header", "absent.xml", None)],
        )
    )
    assert result["quality"]["complete"] is False
    assert {"unmapped_package_xml", "unmapped_binary_part", "unresolved_relationship"} <= set(
        result["quality"]["review_reasons"]
    )
    assert {part["package_part"] for part in result["package_parts"]} >= {
        "customXml/item.xml",
        "word/blob.bin",
    }


def test_inherited_hidden_style_cannot_pass_as_complete_visible_text():
    styles = _part(
        "styles",
        '<w:style w:type="paragraph" w:styleId="Base"><w:rPr><w:vanish/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Derived"><w:basedOn w:val="Base"/></w:style>',
    )
    content = _package(
        '<w:p><w:pPr><w:pStyle w:val="Derived"/></w:pPr><w:r><w:t>Literal hidden text</w:t></w:r></w:p>',
        parts={"word/styles.xml": styles},
        relations=[("st", "styles", "styles.xml", None)],
    )
    result = parse_docx(content)
    assert result["quality"]["complete"] is False
    warning = next(item for item in result["review"] if item["reason"] == "style_requires_rendering")
    assert (
        warning["style_id"] == "Base" and warning["source_style_locator"]["package_part"] == "word/styles.xml"
    )
    assert "Literal hidden text" in result["chunks"][0]["text"]


def test_unmapped_text_with_a_metadata_root_name_is_still_pending():
    result = parse_docx(_package(_p("Body"), parts={"unknown.xml": b"<styles>Hidden data</styles>"}))
    assert result["quality"]["complete"] is False
    assert "unmapped_package_xml" in result["quality"]["review_reasons"]


@pytest.mark.parametrize("name", ["../evil.xml", "/absolute.xml", "word\\evil.xml", "word/%2e%2e/evil.xml"])
def test_unsafe_zip_paths_are_rejected_without_extracting_files(name):
    with pytest.raises(ValueError, match="ZIP 路径"):
        parse_docx(_package(_p("Body"), parts={name: b"<x/>"}))


def test_duplicate_zip_member_names_are_rejected():
    content = _package(_p("Body"))
    buffer = io.BytesIO(content)
    with pytest.warns(UserWarning), zipfile.ZipFile(buffer, "a") as archive:
        archive.writestr("word/document.xml", b"<x/>")
    with pytest.raises(ValueError, match="重复"):
        parse_docx(buffer.getvalue())


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_dtd_entity_declarations_are_rejected_even_in_utf16(encoding):
    source = (
        '<!DOCTYPE document [<!ENTITY injected "untrusted">]><w:document xmlns:w="'
        + W
        + '"><w:body>'
        + _p("&injected;")
        + "</w:body></w:document>"
    )
    with pytest.raises(ValueError, match="DOCTYPE|ENTITY"):
        parse_docx(_package("", parts={"word/document.xml": source.encode(encoding)}))


def test_xml_corruption_and_missing_main_structure_are_rejected():
    for source in (b"<x>\xff</x>", b"<x/>", b"<w:document"):
        with pytest.raises(ValueError):
            parse_docx(_package("", parts={"word/document.xml": source}))


def test_zip_declared_short_size_cannot_hide_a_longer_deflate_stream():
    content = bytearray(_package(_p("Body")))
    local = content.index(b"PK\x03\x04")
    central = content.index(b"PK\x01\x02")
    struct.pack_into("I", content, local + 22, 1)
    struct.pack_into("I", content, central + 24, 1)
    with pytest.raises(ValueError, match="解压流|校验"):
        parse_docx(bytes(content))


def test_encrypted_zip_flags_are_rejected():
    content = bytearray(_package(_p("Body")))
    local, central = content.index(b"PK\x03\x04"), content.index(b"PK\x01\x02")
    struct.pack_into("<H", content, local + 6, 1)
    struct.pack_into("<H", content, central + 8, 1)
    with pytest.raises(ValueError, match="加密"):
        parse_docx(bytes(content))


def test_valid_crc_overlapping_local_records_are_rejected():
    name = b"word/document.xml"
    xml = f'<w:document xmlns:w="{W}"><w:body>{_p("Body")}</w:body></w:document>'.encode()
    embedded = (
        struct.pack(
            "<IHHHHHIIIHH", 0x04034B50, 20, 0, 0, 0, 0, zlib.crc32(xml), len(xml), len(xml), len(name), 0
        )
        + name
        + xml
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("container.bin", embedded)
        archive.writestr(name.decode(), xml)
    content = bytearray(buffer.getvalue())
    first_central = content.index(b"PK\x01\x02")
    second_central = content.index(b"PK\x01\x02", first_central + 4)
    fake_local_offset = 30 + len("container.bin")
    struct.pack_into("<I", content, second_central + 42, fake_local_offset)
    with pytest.raises(ValueError, match="重叠"):
        parse_docx(bytes(content))


def test_bounded_package_text_and_result_capacities(monkeypatch):
    content = _package(_p("Body"))
    monkeypatch.setattr(docx_parsing, "MAX_TEXT", 3)
    with pytest.raises(ValueError, match="文本总量|正文"):
        parse_docx(content)
    monkeypatch.setattr(docx_parsing, "MAX_TEXT", 4_000_000)
    monkeypatch.setattr(docx_parsing, "MAX_RESULT", 20)
    with pytest.raises(ValueError, match="结果"):
        parse_docx(content)


def test_empty_and_title_only_documents_do_not_generate_fake_body():
    for body in (
        "",
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Only title</w:t></w:r></w:p>',
    ):
        result = parse_docx(_package(body))
        assert result["chunks"] == [] and result["quality"]["complete"] is False
        assert "no_searchable_native_text" in result["quality"]["review_reasons"]


def test_original_upload_limit_remains_twenty_megabytes():
    with pytest.raises(ValueError, match="20 MB"):
        parse_docx(b"x" * (20 * 1024 * 1024 + 1))


def test_bounded_worker_preserves_header_field_review_gate():
    from app.parsing import parse_bounded

    content = _package(
        _p("Body"),
        parts={
            "word/header.xml": _part(
                "hdr", '<w:p><w:fldSimple w:instr="PAGE"><w:r><w:t>2</w:t></w:r></w:fldSimple></w:p>'
            )
        },
        relations=[("h", "header", "header.xml", None)],
    )
    result = parse_bounded("law.docx", content)
    assert result["quality"]["complete"] is False
    assert result["quality"]["native_parts"] == 2
    assert any(
        e["content_text"] == "2" and e["locator"]["package_part"] == "word/header.xml"
        for e in result["elements"]
    )


def test_secondary_original_cannot_enter_docx_parser(monkeypatch):
    from app.ingestion import parse_file
    from app.parsing import parse_bounded

    monkeypatch.setattr(
        docx_parsing, "parse_docx", lambda *a, **kw: pytest.fail("secondary reached the parser")
    )
    content = _package(_p("Body"))
    for function in (parse_file, parse_bounded):
        with pytest.raises(ValueError, match="secondary"):
            function("secondary.docx", content, role="secondary")
