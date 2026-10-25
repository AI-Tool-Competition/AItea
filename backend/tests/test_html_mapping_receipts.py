"""Exports preserve acquisition and script-video evidence rather than inventing text."""

import hashlib
import json
from copy import deepcopy

import pytest
from app.contexts.documents.infrastructure.parsers.native_parser import parse_file
from app.contexts.documents.infrastructure.parsers.native_mapping_inventory import mappings, validate_mapping
from test_html_rsc_video_sources import VIDEO, component, source


def test_script_video_export_retains_record_and_parent_script_proof():
    manifest = parse_file("source.html", source(split=True))
    digest = hashlib.sha256(json.dumps(manifest).encode()).hexdigest()
    rows = list(mappings(manifest, digest))
    scan = next(r[2] for r in rows if r[0] == "html_script_video_scan")
    assert scan["video_objects"] == 1 and scan["script_execution"] is False
    video = next(r[2] for r in rows if r[0] == "html_visual_object" and r[2]["tag"] == "rsc-video")
    proof = video["rsc_source"]
    assert len(proof["script_ranges"]) == 2
    assert proof["literal_reference"]["manifest_sha256"] == digest
    assert proof["literal_reference"]["sha256"] == proof["video_src_sha256"]
    assert proof["video_url"]["target_export_status"] == "redacted"  # query-bearing URL
    assert video["vision_status"] == "audiovisual_transcription_pending" and not video["indexed"]
    for _kind, _pointer, record in rows:
        validate_mapping(record, manifest["source_hash"])
    broken = deepcopy(manifest)
    broken["html_source"]["objects"][-1]["rsc_source"]["video_src"] += "modified"
    with pytest.raises(ValueError, match="source hash"):
        list(mappings(broken, digest))
    invalid = deepcopy(proof)
    invalid["decoded_record_end_byte"] = invalid["decoded_record_start_byte"] - 1
    with pytest.raises(ValueError, match="offset|range"):
        validate_mapping(invalid, manifest["source_hash"])


def test_script_video_uri_preserves_amp_entity_literal_after_json_decoding():
    uri = VIDEO + "&amp;language=id"
    manifest = parse_file("source.html", source(component(src=uri)))
    video = manifest["html_source"]["objects"][-1]
    reference = video["url_attributes"][0]
    assert reference["raw_value"] == reference["decoded_value"] == video["rsc_source"]["video_src"] == uri
    rows = list(mappings(manifest, hashlib.sha256(json.dumps(manifest).encode()).hexdigest()))
    exported = next(
        r[2] for r in rows if r[0] == "html_visual_reference" and r[2]["attribute"] == "rsc-video-src"
    )
    assert exported["target_sha256"] == hashlib.sha256(uri.encode()).hexdigest()


def test_html_list_element_mapping_preserves_structured_dom_and_heading_paths():
    raw = (
        "<main><h2>Tariffs</h2><ol start='-2'><li>First<ul><li>Nested</li></ul>"
        "</li><li value='7'>Seventh</li></ol></main>"
    )
    manifest = parse_file("list.html", raw.encode(), "text/html")
    digest = hashlib.sha256(json.dumps(manifest).encode()).hexdigest()
    rows = list(mappings(manifest, digest))
    anchors = {
        int(pointer.rsplit("/", 1)[1]): record
        for kind, pointer, record in rows
        if kind == "text_anchor" and pointer.startswith("/elements/")
    }
    list_elements = [
        (index, element)
        for index, element in enumerate(manifest["elements"])
        if element["kind"] == "list"
    ]

    assert list_elements
    for index, element in list_elements:
        exported = anchors[index]["locator"]
        assert exported["dom_path"] == element["locator"]["dom_path"]
        assert exported["list_path"] == element["locator"]["list_path"]
        assert exported["heading_path"] == ["Tariffs"]
    assert anchors[list_elements[0][0]]["locator"]["dom_path"] == [{"type": "ol", "index": -2}]
    assert anchors[list_elements[1][0]]["locator"]["dom_path"] == [
        {"type": "ol", "index": -2},
        {"type": "ul", "index": None},
    ]
    for _kind, _pointer, record in rows:
        validate_mapping(record, manifest["source_hash"])


def test_html_list_path_projection_rejects_unknown_structured_entries():
    manifest = parse_file("list.html", b"<main><ol><li>item</li></ol></main>", "text/html")
    broken = deepcopy(manifest)
    item = next(element for element in broken["elements"] if element["kind"] == "list")
    item["locator"]["dom_path"] = [{"type": "script", "index": 1}]

    with pytest.raises(ValueError, match="HTML list path"):
        list(mappings(broken, hashlib.sha256(json.dumps(broken).encode()).hexdigest()))
