"""Exports preserve acquisition and script-video evidence rather than inventing text."""

import hashlib
import json
from copy import deepcopy

import pytest
from app.contexts.knowledge.infrastructure.parsing.native import parse_file
from app.contexts.knowledge.infrastructure.parsing.native_mapping_inventory import mappings, validate_mapping
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
