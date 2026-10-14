import hashlib

import pytest

from app.ingestion import _decode, parse_file
from app.parsing import parse_bounded


def digest(value):
    return hashlib.sha256(value).hexdigest()


def test_actual_customs_flow_image_is_mapped_and_blocks_complete_text():
    url = "https://isomer-user-content.by.gov.sg/174/420282d6-4bf3-4200-ac0b-0f9f4ee31d99/how-to-import-ur-goods.jpg"
    body = f'<html><nav><img src="site-logo.png"></nav><main id="main-content"><nav><svg><path/></svg></nav><h1>Import Operations Overview</h1><p>Import pathways from a Free Trade Zone.</p><img src="{url}" alt="A flowchart illustrating import pathways." width="100%" height="auto"></main></html>'.encode()
    result = parse_file("customs.html", body, "text/html")
    assert result["parser"] == "html-basic" and result["parser_version"] == "3"
    assert result["pipeline_version"].split("+", 1)[0] == "html-source-mapping-v3"
    assert result["html_source"]["source_hash"] == digest(body)
    assert not result["quality"]["complete"]
    objects = result["html_source"]["objects"]
    assert len(objects) == 1
    obj = objects[0]
    assert obj["url_attributes"][0]["raw_value"] == url
    assert obj["metadata"]["alt"] == "A flowchart illustrating import pathways."
    assert "flowchart" not in " ".join(chunk["text"] for chunk in result["chunks"])
    assert obj["vision_status"] == "pending" and obj["resource_fetched"] is False
    assert obj["locator"]["dom_path"] == "html[1]/main[1]/img[1]"


def test_unicode_positions_and_literal_entity_url_hashes_are_exact():
    body = '<meta charset="utf-8"><main><p>茶叶和 café。</p>\n<picture><source srcset="small.png?x=1&amp;y=2 1x, large.png 2x"><img src="../原图.png" alt="不能把alt当正文"></picture></main>'.encode()
    result = parse_file("law.html", body)
    source = _decode(body)
    mapping = result["html_source"]
    assert mapping["decoded_source_sha256"] == digest(source.encode())
    assert [obj["tag"] for obj in mapping["objects"]] == ["picture", "source", "img"]
    picture = mapping["objects"][0]
    for obj in mapping["objects"]:
        locator = obj["locator"]
        raw = source[locator["char_start"] : locator["char_end"]]
        assert digest(raw.encode()) == obj["source_fragment_sha256"]
        assert locator["offset_basis"] == "decoded_html_unicode_characters"
        for reference in obj["url_attributes"]:
            value = source[reference["char_start"] : reference["char_end"]]
            assert value == reference["raw_value"]
            assert digest(value.encode()) == reference["raw_value_sha256"]
    assert mapping["objects"][1]["parent_object_key"] == picture["object_key"]
    assert mapping["objects"][1]["url_attributes"][0]["decoded_value"] == "small.png?x=1&y=2 1x, large.png 2x"
    assert "不能把alt" not in " ".join(chunk["text"] for chunk in result["chunks"])


def test_explicit_visibility_filters_have_the_same_boundary_for_text_and_objects():
    body = b"""<main><h1>Requirements</h1><p>Retain records.</p>
<nav><img src="navigation.svg">NAV_TEXT</nav><aside><canvas>ASIDE_TEXT</canvas></aside>
<div hidden="false"><img src="hidden.png">HIDDEN_TEXT</div>
<div aria-hidden="true"><picture><img src="aria-hidden.png"></picture>ARIA_TEXT</div>
<div style="display: none !important"><svg><text>STYLE_TEXT</text></svg></div>
<div style="visibility:hidden"><object data="secret.pdf">SECRET_TEXT</object></div>
<img hidden src="void-hidden.png"><p>Keep the original.</p><svg aria-hidden="true"><path/></svg>
<script>document.write('<main><img src="script.png"></main>')</script>
<template><main><img src="template.png"></main></template></main>"""
    result = parse_file("law.html", body)
    assert result["quality"]["complete"]
    assert result["html_source"]["objects"] == []
    text = " ".join(chunk["text"] for chunk in result["chunks"])
    assert "Keep the original" in text
    for token in ("NAV_TEXT", "ASIDE_TEXT", "HIDDEN_TEXT", "ARIA_TEXT", "STYLE_TEXT", "SECRET_TEXT"):
        assert token not in text


@pytest.mark.parametrize(
    "graphic",
    [
        '<img src="https://127.0.0.1/private.png" alt="invented law">',
        '<picture><source srcset="data:image/png;base64,AA=="><img src="fallback.png"></picture>',
        "<svg><text>diagram text requires verification</text><script>throw 42</script></svg>",
        '<canvas width="100" height="50">unverified fallback</canvas>',
        '<object data="legal.pdf">unverified fallback</object>',
        '<embed src="legal.pdf" type="application/pdf">',
        '<iframe src="legal.pdf">unverified frame fallback</iframe>',
    ],
)
def test_image_or_object_only_preserves_mapping_without_fake_body_or_network(graphic, monkeypatch):
    import socket

    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("HTML source mapping used network")
    )
    body = f"<main>{graphic}</main>".encode()
    result = parse_file("image-only.html", body)
    assert result["elements"] == [] and result["chunks"] == []
    assert result["html_source"]["objects"]
    assert not result["quality"]["complete"]
    assert all(obj["indexed"] is False for obj in result["html_source"]["objects"])


def test_unterminated_visual_object_keeps_an_actual_eof_source_range():
    body = b"<main><p>Keep records.</p><svg><text>unclosed"
    result = parse_file("law.html", body)
    obj = result["html_source"]["objects"][0]
    assert obj["locator"]["char_end"] == len(body)
    assert obj["locator"]["end_tag_observed"] is False
    assert not result["quality"]["source_mapping_complete"]
    assert obj["source_fragment_sha256"] == digest(b"<svg><text>unclosed")


def test_svg_image_reference_remains_bound_to_its_svg_container():
    result = parse_file("law.html", b'<main><svg><image xlink:href="diagram.png?x=1&amp;y=2"/></svg></main>')
    parent, child = result["html_source"]["objects"]
    assert child["tag"] == "image"
    assert child["parent_object_key"] == parent["object_key"]
    assert child["url_attributes"][0]["raw_value"] == "diagram.png?x=1&amp;y=2"
    assert child["locator"]["boundary_kind"] == "self_closing_tag"
    assert child["locator"]["end_tag_observed"] is False


def test_duplicate_image_attributes_are_retained_as_uncertain_source_evidence():
    body = b'<main><p>Keep records.</p><img src="a.png" src="b.png"></main>'
    result = parse_file("law.html", body)
    obj = result["html_source"]["objects"][0]
    assert [ref["raw_value"] for ref in obj["url_attributes"]] == ["a.png", "b.png"]
    assert obj["duplicate_url_attributes"]
    assert not result["quality"]["source_mapping_complete"]


def test_first_duplicate_visibility_attribute_cannot_expose_hidden_body():
    result = parse_file(
        "law.html",
        b'<main><p>Actual body.</p><div style="display:none" style="display:block"><p>Hidden document</p><img src="hidden.png"></div></main>',
    )
    assert result["html_source"]["objects"] == []
    assert "Hidden document" not in " ".join(chunk["text"] for chunk in result["chunks"])


@pytest.mark.parametrize(
    "source",
    [
        b"<html><head><script>renderDocument()</script></head><main></main></html>",
        b'<template><main><img src="not-body.png"></main></template>',
    ],
)
def test_empty_script_shell_and_hidden_object_page_are_not_full_text(source):
    with pytest.raises(ValueError, match="正文|标题|图片"):
        parse_file("shell.html", source)


def test_soft_404_with_native_body_is_never_complete():
    result = parse_file(
        "law.html",
        b"<title>404 Not Found</title><main><h1>404</h1><p>This requested page is missing.</p></main>",
    )
    assert not result["quality"]["complete"]
    assert result["quality"]["error_page_detected"]


def test_plain_html_without_visual_sources_keeps_full_text_and_actual_line_anchors():
    result = parse_file(
        "law.html", b'<main>\n<h1 id="law">Requirements</h1>\n<p>Retain original records.</p>\n</main>'
    )
    assert result["quality"]["complete"]
    assert result["html_source"]["objects"] == []
    assert result["elements"][0]["locator"]["line_start"] == 2
    assert result["elements"][0]["locator"]["anchor"] == "law"


def test_declared_legacy_charset_positions_are_not_claimed_as_original_byte_offsets():
    body = '<meta charset="iso-8859-1"><main><p>Français.</p><img src="café.png"></main>'.encode("latin-1")
    result = parse_file("law.html", body)
    obj = result["html_source"]["objects"][0]
    source = _decode(body)
    assert obj["source_hash"] == digest(body)
    assert obj["source_fragment_sha256"] == digest(
        source[obj["locator"]["char_start"] : obj["locator"]["char_end"]].encode()
    )
    assert "byte_start" not in obj["locator"]


def test_bounded_worker_keeps_image_only_source_mapping():
    result = parse_bounded("law.html", b'<main><img src="diagram.png"></main>', "text/html")
    assert result["chunks"] == []
    assert result["html_source"]["objects"][0]["vision_status"] == "pending"


def test_visual_object_and_depth_limits_fail_without_partial_complete_manifest():
    with pytest.raises(ValueError, match="对象数量"):
        parse_file("many.html", ("<main>" + '<img src="x.png">' * 501 + "</main>").encode())
    with pytest.raises(ValueError, match="嵌套深度"):
        parse_file("deep.html", ("<main>" + "<div>" * 128 + "text" + "</div>" * 128 + "</main>").encode())
