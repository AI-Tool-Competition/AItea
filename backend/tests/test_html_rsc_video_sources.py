"""Source-video evidence must survive thumbnail recognition without execution."""

import hashlib
import json
from copy import deepcopy

import pytest
from app.contexts.knowledge.infrastructure.parsing.html_parsing import RSC_VIDEO_SCAN_VERSION
from app.contexts.knowledge.infrastructure.parsing.native import _decode, parse_file
from app.contexts.knowledge.infrastructure.parsing.native_mapping_inventory import mappings, validate_mapping
from app.contexts.knowledge.infrastructure.parsing.parsing import parse_bounded
from app.contexts.knowledge.infrastructure.providers.html_linked_asset_ocr import (
    image_candidates_bounded,
    update_quality,
)

VIDEO_ID = "sYvtQX3oPbE"
VIDEO = "https://www.youtube-nocookie.com/embed/" + VIDEO_ID + "?si=example"
THUMBNAIL = "https://i.ytimg.com/vi/" + VIDEO_ID + "/sddefault.jpg"


def sha(value):
    return hashlib.sha256(value).hexdigest()


def component(parent="section", hidden=False, src=VIDEO, title="Foreign business options"):
    return [
        "$",
        parent,
        None,
        {
            "hidden": hidden,
            "children": [
                "$",
                "$L8a",
                None,
                {
                    "src": src,
                    "videoId": VIDEO_ID,
                    "title": title,
                    "shouldLazyLoad": False,
                },
            ],
        },
    ]


def source(value=None, *, thumbnail=True, split=False, escaped=False):
    value = component() if value is None else value
    record = "7b:" + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
    if escaped:
        record = record.replace("https://", "https:\\/\\/")
    parts = [record[:13], record[13:]] if split else [record]
    scripts = "".join(
        "<script>self.__next_f.push(" + json.dumps([1, part], ensure_ascii=False) + ")</script>"
        for part in parts
    )
    return (
        '<meta charset="utf-8"><main><p>茶 Retain corporate records.</p>'
        + (f'<img src="{THUMBNAIL}" alt="Thumbnail for a video">' if thumbnail else "")
        + '</main><iframe src="https://www.googletagmanager.com/ns.html?id=GTM-X" style="display:none;visibility:hidden"></iframe>'
        + scripts
    ).encode()


def test_detached_next_component_maps_actual_body_video_and_script_boundaries():
    raw = source()
    result = parse_file("law.html", raw)
    assert result["parser_version"] == "9"
    assert result["pipeline_version"].split("+", 1)[0] == "html-source-mapping-v9"
    image, video = result["html_source"]["objects"]
    assert image["tag"] == "img" and video["tag"] == "rsc-video"
    assert video["vision_status"] == "audiovisual_transcription_pending"
    assert video["rsc_source"]["video_src"] == VIDEO
    assert video["rsc_source"]["record_id"] == "7b"
    assert video["rsc_source"]["json_pointer"] == "/3/children/3/src"
    assert result["html_source"]["rsc_video_scan"]["protocol"] == RSC_VIDEO_SCAN_VERSION
    assert result["html_source"]["rsc_video_scan"]["scan_errors"] == 0
    text = _decode(raw)
    loc = video["locator"]
    assert text[loc["char_start"] : loc["char_end"]].startswith("<script>self.__next_f.push(")
    assert text[loc["char_start"] : loc["char_end"]].endswith("</script>")
    assert sha(text[loc["char_start"] : loc["char_end"]].encode()) == video["source_fragment_sha256"]
    ref = video["url_attributes"][0]
    assert text[ref["char_start"] : ref["char_end"]] == ref["raw_value"] == VIDEO
    assert sha(ref["raw_value"].encode()) == ref["raw_value_sha256"]
    assert "googletagmanager" not in json.dumps(result["html_source"]["objects"])
    assert not result["quality"]["complete"]
    assert VIDEO not in " ".join(c["text"] for c in result["chunks"])
    for _, _, record in mappings(result, sha(json.dumps(result).encode())):
        validate_mapping(record, result["source_hash"])


def test_completed_thumbnail_cannot_clear_untranscribed_video_gate():
    result = parse_file("law.html", source())
    update_quality(result, [{"object_key": "html-object-000001", "asset_status": "ocr_complete"}])
    assert result["quality"]["pending_objects"] == ["html-object-000002"]
    assert not result["quality"]["complete"]


def test_new_candidate_preflight_rejects_old_v3_video_mapping_before_network():
    raw = source()
    result = parse_file("law.html", raw)
    old = deepcopy(result)
    old["html_source"]["objects"] = old["html_source"]["objects"][:1]
    old["html_source"].pop("rsc_video_scan")
    with pytest.raises(ValueError, match="DOM|原件"):
        image_candidates_bounded(raw, old)
    current = image_candidates_bounded(raw, result)
    assert [x["tag"] for x in current["eligible"]] == ["img"]
    assert current["unresolved"] == [{"object_key": "html-object-000002", "tag": "rsc-video"}]


@pytest.mark.parametrize("parent,hidden", [("nav", False), ("footer", False), ("section", True)])
def test_hidden_or_navigation_flight_components_do_not_become_body_sources(parent, hidden):
    result = parse_file("law.html", source(component(parent, hidden)))
    assert [x["tag"] for x in result["html_source"]["objects"]] == ["img"]


def test_detached_component_without_matching_visible_thumbnail_is_excluded():
    result = parse_file("law.html", source(thumbnail=False))
    assert result["html_source"]["objects"] == []
    assert result["quality"]["complete"]


def test_serialized_main_video_without_literal_thumbnail_is_unresolved():
    result = parse_file("law.html", source(component("main"), thumbnail=False))
    assert [x["tag"] for x in result["html_source"]["objects"]] == ["rsc-video"]
    assert not result["quality"]["complete"]


def test_literal_video_and_its_source_have_actual_container_mapping():
    result = parse_file(
        "law.html",
        b'<main><p>Records.</p><video poster="thumb.png"><source src="guide.mp4" type="video/mp4"></video></main>',
    )
    video, media = result["html_source"]["objects"]
    assert [video["tag"], media["tag"]] == ["video", "source"]
    assert media["parent_object_key"] == video["object_key"]
    assert not result["quality"]["complete"]


@pytest.mark.parametrize("split,escaped", [(True, False), (False, True)])
def test_chunked_or_json_escaped_video_url_keeps_decoding_proof(split, escaped):
    raw = source(split=split, escaped=escaped)
    result = parse_file("law.html", raw)
    video = result["html_source"]["objects"][-1]
    assert video["rsc_source"]["video_src"] == VIDEO
    assert video["rsc_source"]["video_src_sha256"] == sha(VIDEO.encode())
    if split:
        assert len(video["rsc_source"]["script_ranges"]) == 2
    if escaped:
        assert video["url_attributes"] == []
    text = _decode(raw)
    for ref in video["rsc_source"]["script_ranges"]:
        assert sha(text[ref["char_start"] : ref["char_end"]].encode()) == ref["sha256"]


def test_bootstrap_modules_resource_hints_and_arbitrary_scripts_are_not_videos(monkeypatch):
    import socket

    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("Source mapping opened network")
    )
    data = '0:HL["' + VIDEO + '"]\n1:I["' + VIDEO + '"]\n2:T3,foo\n'
    raw = (
        "<main><p>Keep records.</p></main><script>self.__next_f.push([0])</script>"
        + "<script>self.__next_f.push("
        + json.dumps([1, data])
        + ")</script>"
        + '<script>fetch("'
        + VIDEO
        + '")</script>'
    ).encode()
    result = parse_file("law.html", raw)
    assert result["html_source"]["objects"] == []
    assert result["quality"]["complete"]
    assert result["html_source"]["scripts_executed"] is False


def test_incomplete_next_script_cannot_clear_source_gate():
    raw = b'<main><p>Records.</p></main><script>self.__next_f.push([1,"7b:["])</script>'
    result = parse_file("law.html", raw)
    assert result["html_source"]["rsc_video_scan"]["scan_errors"]
    assert not result["quality"]["source_mapping_complete"]
    assert not result["quality"]["complete"]


def test_old_plain_v3_manifest_remains_compatible_with_candidate_preflight():
    raw = b'<main><p>Records.</p><img src="diagram.png"></main>'
    result = parse_file("law.html", raw)
    result["html_source"].pop("rsc_video_scan")
    assert len(image_candidates_bounded(raw, result)["eligible"]) == 1


def test_bounded_parser_preserves_audiovisual_source_without_model_or_fetch():
    result = parse_bounded("law.html", source(), "text/html")
    assert result["html_source"]["objects"][-1]["tag"] == "rsc-video"
    assert not result["quality"]["complete"]
