"""Bounded, allowlisted projections of stored native source mappings.

This module reads no original, executes no converter, and infers no legal or
retrieval eligibility. Inline bytes remain in the authorized database manifest;
their references bind its exact UTF-8 hash and JSON pointer.
"""

import base64
import binascii
import hashlib
import math
import re
from urllib.parse import urlsplit
from html import unescape

MAX_MANIFEST_BYTES = 40 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_INLINE_BYTES = 12 * 1024 * 1024
MAX_STRING = 1024 * 1024
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_OFFSETS = {
    "size",
    "length",
    "stream_offset",
    "cp_start",
    "cp_end",
    "file_offset",
    "database_page",
    "row_slot",
    "start",
    "entry_offset",
    "artifact_byte_offset",
    "char_start",
    "char_end",
    "decoded_record_start_byte",
    "decoded_record_end_byte",
}


def validate_mapping(value, source_hash, depth=0):
    """A stored locator cannot silently name another immutable original."""
    if depth > 12:
        raise ValueError("native mapping projection nesting exceeds limit")
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "source_hash" and item != source_hash:
                raise ValueError("native mapping source hash mismatch")
            if item is not None and (
                key.endswith("sha256")
                or key in {"content_hash", "part_hash", "image_hash", "target_hash", "xml_hash"}
            ):
                _hash(item)
            if key in _OFFSETS and (type(item) is not int or item < 0):
                raise ValueError("invalid native mapping byte or character offset")
            validate_mapping(item, source_hash, depth + 1)
        for first, last in (
            ("cp_start", "cp_end"),
            ("char_start", "char_end"),
            ("decoded_record_start_byte", "decoded_record_end_byte"),
        ):
            if first in value and last in value and value[first] > value[last]:
                raise ValueError("reversed native mapping character range")
    elif isinstance(value, list):
        for item in value:
            validate_mapping(item, source_hash, depth + 1)


def _scalar(value):
    if value is None or type(value) in {bool, int}:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if isinstance(value, str) and len(value) <= MAX_STRING:
        return value
    raise ValueError("invalid native mapping scalar")


def _select(value, keys):
    if not isinstance(value, dict):
        raise ValueError("invalid native mapping object")
    return {key: _scalar(value[key]) for key in keys.split() if key in value}


def _items(value):
    if not isinstance(value, list):
        raise ValueError("invalid native mapping list")
    return enumerate(value)


def _pointer(base, key):
    return base + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _hash(value):
    if not isinstance(value, str) or _HASH.fullmatch(value) is None:
        raise ValueError("invalid native mapping hash")
    return value


def storage(value):
    result = _select(
        value,
        "kind source_format source_hash stream stream_offset length sha256 record_type entry_offset "
        "database_page row_slot start file_offset storage_null_bit payload_sha256 cp_start cp_end "
        "encoding directory_id parent_directory_id entry_type declared_size state",
    )
    if "path" in value:
        result["path"] = [_scalar(part) for _, part in _items(value["path"])]
    if "memo_storage" in value:
        result["memo_storage"] = [storage(part) for _, part in _items(value["memo_storage"])]
    return result


def locator(value, depth=0):
    if depth > 5:
        raise ValueError("native mapping locator nesting exceeds limit")
    result = _select(
        value,
        "kind source_format source_hash package_part part_hash xml_path section paragraph table row "
        "column column_start column_end column_span row_start row_end note_id note_type cell_range "
        "sheet source_area print_section artifact_sha256 artifact_kind xpath physical_page_verified "
        "database_page row_slot start length sha256 file_offset row_ordinal column_ordinal "
        "source_type stream stream_offset cp_start cp_end encoding page char_start char_end "
        "asset_key asset_mapping_path original_image_sha256 image_sha256 source_kind physical_pages_known "
        "dom_path dom_basis line_start column_start offset_basis end_tag_observed anchor "
        "boundary_kind source_boundary_complete",
    )
    for key in ("source_record", "field_storage"):
        if key in value:
            result[key] = storage(value[key])
    for key in ("section_path", "bbox", "region_bbox"):
        if key in value:
            result[key] = [_scalar(item) for _, item in _items(value[key])]
    if "source_anchors" in value:
        result["source_anchors"] = [locator(item, depth + 1) for _, item in _items(value["source_anchors"])]
    return result


def _reference_url(value):
    """Reuse downloader syntax/public-literal checks without DNS or requests."""
    from .ingestion import DownloadError, _validated_url

    result = {"target_sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}
    try:
        _, host = _validated_url(value)
        parsed = urlsplit(value)
        if (
            len(value) > 2048
            or parsed.fragment
            or "." not in host
            or host.endswith((".localhost", ".local", ".internal"))
            or parsed.query
        ):
            raise ValueError
    except (ValueError, UnicodeError, DownloadError):
        result["target_export_status"] = "redacted"
    else:
        result.update(target=value, target_export_status="public_url_syntax_only_dns_not_checked")
    return result


def relationship(value):
    result = _select(
        value,
        "id relationship_id source_part type target_mode status target_hash asset_key attribute xml_path",
    )
    if "locator" in value:
        result["locator"] = locator(value["locator"])
    target = value.get("target")
    if isinstance(target, str):
        if value.get("target_mode") == "External":
            result.update(_reference_url(target))
        else:
            result["target_sha256"] = hashlib.sha256(target.encode("utf-8")).hexdigest()
    part = value.get("target_part")
    if isinstance(part, str) and not part.startswith("/") and ".." not in part.split("/"):
        result["target_part"] = _scalar(part)
    return result


def _inline(value, base, manifest_hash, field, *, hash_key="sha256", size_key="size"):
    """Check base64 in bounded blocks; do not copy or write decoded artifact bytes."""
    encoded, digest, size = value.get(field), _hash(value.get(hash_key)), value.get(size_key)
    if not isinstance(encoded, str) or type(size) is not int or not 0 <= size <= MAX_INLINE_BYTES:
        raise ValueError("invalid inline native artifact")
    if len(encoded) != 4 * ((size + 2) // 3):
        raise ValueError("inline native artifact size mismatch")
    hasher, decoded_size = hashlib.sha256(), 0
    try:
        for offset in range(0, len(encoded), 65536):
            block = encoded[offset : offset + 65536]
            if "=" in block and offset + len(block) != len(encoded):
                raise ValueError
            decoded = base64.b64decode(block, validate=True)
            if base64.b64encode(decoded).decode("ascii") != block:
                raise ValueError
            decoded_size += len(decoded)
            hasher.update(decoded)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid inline native artifact encoding") from exc
    if decoded_size != size or hasher.hexdigest() != digest:
        raise ValueError("inline native artifact hash mismatch")
    return {
        "storage": "documents.manifest",
        "manifest_sha256": manifest_hash,
        "json_pointer": _pointer(base, field),
        "content_hash": digest,
        "size": size,
        "status": "inline_bytes_verified_not_exported",
    }


def _asset(value, base, manifest_hash):
    result = _select(
        value,
        "asset_key package_part image_hash image_sha256 size relationship_id source_part mime_type "
        "width height frame_count state status image_bytes_verified original_object_mapping "
        "reference_sha256 format supported_for_vision reason dimensions_from pixels_verified "
        "asset_status mapping_path source_format source_hash input_identity",
    )
    if "locator" in value:
        result["locator"] = locator(value["locator"])
    if "content_base64" in value:
        result["bytes_reference"] = _inline(
            value, base, manifest_hash, "content_base64", hash_key="image_sha256"
        )
    if "ocr" in value:
        ocr = value["ocr"]
        proof = _select(
            ocr,
            "source_hash asset_key mapping_path image_sha256 original_image_sha256 "
            "width height renderer renderer_version render_version physical_page_verified "
            "attempts prompt_version requested_model model finish_reason complete is_blank "
            "review_status text_sha256 reading_hash input_identity protocol source_format "
            "original_image_size image_size original_width original_height frame_count "
            "frame_basis pixel_transform exif_orientation physical_pages_known",
        )
        if "locator" in ocr:
            proof["locator"] = locator(ocr["locator"])
        if "image_blob" in ocr:
            reference = _select(ocr["image_blob"], "content_hash size mime_type")
            path = ocr["image_blob"].get("storage_path")
            if path is not None:
                match = re.fullmatch(
                    r"files/blobs/[a-f0-9]{64}/([a-f0-9]{2})/([a-f0-9]{2})/([a-f0-9]{64})",
                    path if isinstance(path, str) else "",
                )
                if not match or (
                    match[3] != reference.get("content_hash")
                    or match[1] != match[3][:2]
                    or match[2] != match[3][2:4]
                ):
                    raise ValueError("invalid native image blob reference")
                reference["storage_path"] = path
            if reference.get("content_hash") != ocr.get("image_sha256"):
                raise ValueError("native image blob hash mismatch")
            proof["image_blob"] = reference
        if "uncertain_spans" in ocr:
            proof["uncertain_spans"] = [_scalar(item) for _, item in _items(ocr["uncertain_spans"])]
        if "text" in ocr:
            if not isinstance(ocr["text"], str) or len(ocr["text"]) > MAX_STRING:
                raise ValueError("invalid native image transcription")
            digest = hashlib.sha256(ocr["text"].encode()).hexdigest()
            if ocr.get("text_sha256", digest) != digest:
                raise ValueError("native image transcription hash mismatch")
            proof["text_reference"] = {
                "storage": "documents.manifest",
                "manifest_sha256": manifest_hash,
                "json_pointer": _pointer(base + "/ocr", "text"),
                "sha256": digest,
                "chars": len(ocr["text"]),
            }
        result["ocr"] = proof
    result["vision_completed_by_export"] = False
    return result


def mappings(manifest, manifest_hash):
    """Yield (kind, JSON pointer, allowlisted mapping) one source item at a time."""

    def emit(kind, base, value):
        return kind, base, value

    html = manifest.get("html_source")
    if html is not None:
        yield emit(
            "html_source",
            "/html_source",
            _select(
                html,
                "mapping_version source_hash decoded_source_sha256 decoded_character_count "
                "offset_basis fragment_hash_basis dom_basis main_container_found navigation_excluded "
                "external_resources_fetched scripts_executed",
            ),
        )
        if "rsc_video_scan" in html:
            yield emit(
                "html_script_video_scan",
                "/html_source/rsc_video_scan",
                _select(
                    html["rsc_video_scan"],
                    "protocol script_execution scripts records video_objects scan_errors scope",
                ),
            )
        for index, value in _items(html.get("objects", [])):
            base = _pointer("/html_source/objects", index)
            record = _select(
                value,
                "object_key tag source_hash source_fragment_sha256 parent_object_key "
                "alt_is_recognized_text duplicate_url_attributes vision_status resource_fetched indexed",
            )
            record["locator"] = locator(value["locator"])
            if "metadata" in value:
                record["metadata"] = _select(value["metadata"], "alt title type width height media sizes")
            if "rsc_source" in value:
                proof = value["rsc_source"]
                record["rsc_source"] = _select(
                    proof,
                    "protocol record_id json_pointer decoded_record_sha256 decoded_record_start_byte "
                    "decoded_record_end_byte decoded_stream_sha256 video_src_sha256 visible_thumbnail_id",
                )
                url = proof.get("video_src")
                if not isinstance(url, str) or hashlib.sha256(url.encode()).hexdigest() != proof.get(
                    "video_src_sha256"
                ):
                    raise ValueError("HTML script video source hash mismatch")
                record["rsc_source"]["video_url"] = _reference_url(url)
                record["rsc_source"]["literal_reference"] = {
                    "storage": "documents.manifest",
                    "manifest_sha256": manifest_hash,
                    "json_pointer": base + "/rsc_source/video_src",
                    "sha256": proof["video_src_sha256"],
                }
                record["rsc_source"]["script_ranges"] = [
                    _select(part, "char_start char_end sha256")
                    for _, part in _items(proof.get("script_ranges"))
                ]
            yield emit("html_visual_object", base, record)
            for offset, attr in _items(value.get("url_attributes", [])):
                pointer = _pointer(base + "/url_attributes", offset)
                attribute = _select(
                    attr,
                    "attribute raw_value_sha256 char_start char_end offset_basis resource_fetched",
                )
                raw, decoded = attr.get("raw_value"), attr.get("decoded_value")
                expected = (
                    raw
                    if attr.get("attribute") == "rsc-video-src"
                    else unescape(raw)
                    if isinstance(raw, str)
                    else None
                )
                if (
                    not isinstance(raw, str)
                    or not isinstance(decoded, str)
                    or len(raw) > MAX_STRING
                    or len(decoded) > MAX_STRING
                    or hashlib.sha256(raw.encode()).hexdigest() != attr.get("raw_value_sha256")
                    or expected != decoded
                ):
                    raise ValueError("HTML image reference literal hash mismatch")
                attribute.update(_reference_url(decoded))
                attribute["literal_reference"] = {
                    "storage": "documents.manifest",
                    "manifest_sha256": manifest_hash,
                    "json_pointer": _pointer(pointer, "raw_value"),
                    "sha256": attr["raw_value_sha256"],
                }
                yield emit("html_visual_reference", pointer, attribute)
        for index, value in _items(html.get("linked_images", [])):
            from .html_linked_asset_ocr import validate_binding

            validate_binding(manifest, value)
            base = _pointer("/html_source/linked_images", index)
            record = _asset(value, base, manifest_hash)
            record.update(_select(value, "protocol object_key source_fragment_sha256"))
            for field in ("source_url", "requested_url", "fetched_url"):
                record[field] = _reference_url(value[field])
            record["original_image_blob"] = _select(
                value["original_image_blob"], "content_hash size storage_path mime_type"
            )
            if "download_receipt_blob" in value:
                record["download_receipt_blob"] = _select(
                    value["download_receipt_blob"], "content_hash size storage_path mime_type"
                )
            if "pixel_verification" in value:
                record["pixel_verification"] = _select(
                    value["pixel_verification"],
                    "original_image_sha256 original_image_size width height original_width "
                    "original_height frame_count frame_basis pixel_transform image_sha256 image_size",
                )
            record["original_url_attribute"] = {
                "attribute": value["url_attribute"]["attribute"],
                "raw_value_sha256": value["url_attribute"]["raw_value_sha256"],
                "json_pointer": base + "/url_attribute/raw_value",
                "storage": "documents.manifest",
                "manifest_sha256": manifest_hash,
            }
            yield emit("html_linked_image", base, record)
    for index, page in _items(manifest.get("pages", [])):
        base = _pointer("/pages", index)
        audit = page.get("native_visibility")
        if audit is not None:
            record = _select(
                audit,
                "version source_hash page native_text_sha256 native_text_field "
                "visibility_complete_certified requires_visual_review annotation_count "
                "object_count text_object_count audit_scope",
            )
            for key in ("reasons", "unknown_reasons", "page_bounds", "page_size"):
                if key in audit:
                    record[key] = [_scalar(item) for _, item in _items(audit[key])]
            yield emit("pdf_native_visibility", base + "/native_visibility", record)
            for evidence_index, evidence in _items(audit.get("evidence", [])):
                pointer = _pointer(base + "/native_visibility/evidence", evidence_index)
                value = _select(evidence, "object_order text_sha256 render_mode active")
                for key in ("bounds_pdf_bottom_left", "matrix", "fill_rgba", "stroke_rgba", "reasons"):
                    if key in evidence:
                        value[key] = [_scalar(item) for _, item in _items(evidence[key])]
                value["text_reference"] = {
                    "storage": "documents.manifest",
                    "manifest_sha256": manifest_hash,
                    "json_pointer": pointer + "/text",
                    "sha256": hashlib.sha256(evidence["text"].encode()).hexdigest(),
                }
                yield emit("pdf_native_visibility_object", pointer, value)
        blank = page.get("native_blank")
        if blank is not None:
            from .pdf_text_visibility import prove_blank_page

            if prove_blank_page(blank, audit, native_text=page.get("native_text", "")) is None:
                raise ValueError("invalid native PDF blank-page proof")
            record = _select(
                blank,
                "version source_hash page image_sha256 image_size mime_type width height dpi "
                "renderer renderer_version pdfium_version render_version is_blank proof_kind "
                "native_text_sha256 visibility_audit_version model_calls",
            )
            record["bytes_reference"] = _inline(
                blank,
                base + "/native_blank",
                manifest_hash,
                "image_base64",
                size_key="image_size",
                hash_key="image_sha256",
            )
            yield emit("pdf_native_blank_page", base + "/native_blank", record)
        ocr = page.get("ocr", {})
        if "native_hint_strategy" in ocr:
            yield emit(
                "pdf_ocr_native_hint",
                base + "/ocr",
                _select(
                    ocr, "native_hint_strategy native_hint_hash native_hint_chars source_hash page attempts"
                ),
            )
        if "reuse" in ocr:
            record = _select(
                ocr["reuse"],
                "protocol source_document_id source_document_version source_content_hash source_page "
                "source_page_checkpoint_sha256 target_document_id target_document_version "
                "profile_fingerprint png_content_hash png_size native_text_sha256 reasons_sha256 "
                "text_sha256 origin_attempts target_model_calls local_bounded_render_verified",
            )
            yield emit("pdf_ocr_reuse", base + "/ocr/reuse", record)
        if "reuse_import" in ocr:
            yield emit(
                "pdf_ocr_reuse_import",
                base + "/ocr/reuse_import",
                _select(
                    ocr["reuse_import"],
                    "protocol archive_sha256 archive_size archive_path source_owner_sha256 "
                    "target_owner_sha256 source_proof_sha256 derived_proof_sha256 "
                    "derived_page_sha256 import_model_calls",
                ),
            )
        if "provider_profile" in ocr:
            yield emit(
                "pdf_ocr_provider_profile",
                base + "/ocr/provider_profile",
                _select(
                    ocr["provider_profile"],
                    "version fingerprint requested_model prompt_version prompt_sha256",
                ),
            )
    for key in ("elements", "chunks"):
        for index, value in _items(manifest.get(key, [])):
            base = _pointer("/" + key, index)
            record = _select(value, "id element_key chunk_key kind order parent_key content_hash")
            if "locator" in value:
                record["locator"] = locator(value["locator"])
            yield emit("text_anchor", base, record)
            metadata = value.get("metadata_json", {}) if key == "elements" else {}
            if not isinstance(metadata, dict):
                raise ValueError("invalid element mapping metadata")
            for cell_index, cell in _items(metadata.get("cells", [])):
                pointer = _pointer(base + "/metadata_json/cells", cell_index)
                cell_record = _select(
                    cell,
                    "cell row column raw_type raw_value raw_float_hex xf_index number_format_key "
                    "number_format display_text display_status review_reason",
                )
                if "source_record" in cell:
                    cell_record["source_record"] = storage(cell["source_record"])
                if "formula_cache" in cell:
                    formula = cell["formula_cache"]
                    cell_record["formula_cache"] = _select(formula, "cached_result_hex tokens_sha256")
                    for name in ("source_record", "string_result_record"):
                        if name in formula:
                            cell_record["formula_cache"][name] = storage(formula[name])
                yield emit("spreadsheet_cell", pointer, cell_record)
            for field_index, field in _items(metadata.get("field_storage", [])):
                yield emit(
                    "database_field",
                    _pointer(base + "/metadata_json/field_storage", field_index),
                    storage(field),
                )
    for key, kind in (("package_parts", "package_part"), ("references", "package_reference")):
        for index, value in _items(manifest.get(key, [])):
            record = _select(value, "package_part size sha256 status relationship_id")
            if "locator" in value:
                record["locator"] = locator(value["locator"])
            yield emit(kind, _pointer("/" + key, index), record)
    for index, value in _items(manifest.get("relationships", [])):
        yield emit("package_relationship", _pointer("/relationships", index), relationship(value))
    for index, value in _items(manifest.get("objects", [])):
        record = _select(value, "object_key object_type xml_hash status")
        if "locator" in value:
            record["locator"] = locator(value["locator"])
        if "relationships" in value:
            record["relationships"] = [relationship(item) for _, item in _items(value["relationships"])]
        yield emit("package_object", _pointer("/objects", index), record)
    for index, value in _items(manifest.get("assets_pending", [])):
        base = _pointer("/assets_pending", index)
        yield emit("image", base, _asset(value, base, manifest_hash))
    for index, value in _items(manifest.get("tables", [])):
        base = _pointer("/tables", index)
        record = _select(value, "table row_count column_count")
        if "locator" in value:
            record["locator"] = locator(value["locator"])
        yield emit("native_table", base, record)
        for cell_index, cell in _items(value.get("cells", [])):
            record = _select(cell, "table row column_start column_end column_span row_span vertical_merge")
            if "locator" in cell:
                record["locator"] = locator(cell["locator"])
            if "merged_origin" in cell:
                record["merged_origin"] = _select(cell["merged_origin"], "row column")
            yield emit("native_table_cell", _pointer(base + "/cells", cell_index), record)
    workbook = manifest.get("workbook")
    if workbook is not None:
        yield emit(
            "workbook",
            "/workbook",
            _select(
                workbook,
                "stream_name stream_size stream_sha256 source_hash date_mode biff_version "
                "formula_count cell_count cell_mapping_complete",
            ),
        )
        for key in ("blank_format_records", "xf_records", "sheet_boundaries", "sheets", "print_fields"):
            for index, value in _items(workbook.get(key, [])):
                base = _pointer("/workbook/" + key, index)
                record = _select(
                    value,
                    "sheet name index kind visibility row_count column_count "
                    "font_index number_format_key source_area",
                )
                if "source_record" in value:
                    record["source_record"] = storage(value["source_record"])
                elif key == "blank_format_records":
                    record.update(storage(value))
                yield emit("workbook_" + key, base, record)
                for merge_index, merge in _items(value.get("merged_ranges", [])):
                    yield emit(
                        "spreadsheet_merge",
                        _pointer(base + "/merged_ranges", merge_index),
                        _select(merge, "cell_range row_start row_end column_start column_end"),
                    )
                if key == "print_fields":
                    sections = value.get("sections", {})
                    if not isinstance(sections, dict):
                        raise ValueError("invalid spreadsheet print sections")
                    for section in ("left", "center", "right"):
                        for span_index, span in _items(sections.get(section, [])):
                            yield emit(
                                "spreadsheet_print_literal",
                                _pointer(base + "/sections/" + section, span_index),
                                _select(span, "char_start char_end text"),
                            )
                    for field_index, field in _items(value.get("dynamic_fields", [])):
                        yield emit(
                            "spreadsheet_print_dynamic",
                            _pointer(base + "/dynamic_fields", field_index),
                            _select(field, "field section char_start char_end raw_code"),
                        )
    database = manifest.get("database")
    if database is not None:
        yield emit(
            "database",
            "/database",
            _select(
                database,
                "source_hash reader_sha256 reader_protocol table_mapping_complete queries_executed macros_executed",
            ),
        )
        for key in ("tables", "catalog"):
            for index, value in _items(database.get(key, [])):
                base = _pointer("/database/" + key, index)
                yield emit(
                    "database_" + key,
                    base,
                    _select(value, "name type declared_rows definition_page recovered_rows system kind"),
                )
                for column_index, column in _items(value.get("columns", [])):
                    yield emit(
                        "database_column",
                        _pointer(base + "/columns", column_index),
                        _select(column, "name type size precision scale fixed source_type"),
                    )
    legacy = manifest.get("legacy")
    if legacy is not None:
        yield emit(
            "legacy",
            "/legacy",
            _select(
                legacy,
                "source_hash format macros_executed external_links_fetched independent_character_evidence",
            ),
        )
        source = legacy.get("source_storage", {})
        if "source_storage" in legacy:
            yield emit(
                "compound_source",
                "/legacy/source_storage",
                _select(
                    source,
                    "nFib fHasPic table_stream cp_count layout_visibility_mapping physical_pages container_warning",
                ),
            )
        for key in ("subdocuments", "pieces", "control_markers", "property_records", "streams", "objects"):
            for index, value in _items(source.get(key, [])):
                base = _pointer("/legacy/source_storage/" + key, index)
                record = storage(value)
                record.update(
                    _select(
                        value, "cp_count cp code_unit meaning decode_state prm mime_type image_bytes_verified"
                    )
                )
                if "pcd_reference" in value:
                    record["pcd_reference"] = storage(value["pcd_reference"])
                if "storage_base64" in value:
                    record["bytes_reference"] = _inline(
                        value, base, manifest_hash, "storage_base64", size_key="length"
                    )
                yield emit("compound_" + key, base, record)
        conversion = legacy.get("conversion", legacy)
        base = "/legacy/conversion" if "conversion" in legacy else "/legacy"
        artifacts = [(base + "/artifact", conversion["artifact"])] if "artifact" in conversion else []
        artifacts.extend(
            (_pointer(base + "/artifacts", i), value) for i, value in _items(conversion.get("artifacts", []))
        )
        for pointer, value in artifacts:
            record = _select(value, "kind sha256 size encoding source_mapping")
            if "content_base64" in value:
                record["bytes_reference"] = _inline(value, pointer, manifest_hash, "content_base64")
            yield emit("conversion_artifact", pointer, record)
        for key in ("nodes", "events", "declared_breaks", "images", "tables"):
            for index, value in _items(conversion.get(key, [])):
                pointer = _pointer(base + "/" + key, index)
                if key == "images":
                    record = _asset(value, pointer, manifest_hash)
                else:
                    record = _select(
                        value,
                        "xpath tag ordinal artifact_byte_offset length sha256 kind "
                        "physical_page_verified structure_source original_merge_geometry_verified text_in_ancestor_element",
                    )
                    if "locator" in value:
                        record["locator"] = locator(value["locator"])
                yield emit("converted_" + key, pointer, record)
                if key == "tables":
                    for row_index, row in _items(value.get("rows", [])):
                        for column_index, cell in _items(row):
                            cell_pointer = _pointer(_pointer(pointer + "/rows", row_index), column_index)
                            record = _select(cell, "row column row_span column_span")
                            if "attributes" in cell:
                                record["converted_attributes"] = _select(
                                    cell["attributes"], "rowspan colspan namest nameend morerows"
                                )
                            yield emit(
                                "converted_table_cell",
                                cell_pointer,
                                record,
                            )
    for key in ("review",):
        for index, value in _items(manifest.get(key, [])):
            record = _select(
                value,
                "reason status relationship_id object_key object_type image_count package_part part_hash",
            )
            if "locator" in value:
                record["locator"] = locator(value["locator"])
            yield emit("pending_review", _pointer("/" + key, index), record)
    quality = manifest.get("quality") or {}
    for index, value in _items(quality.get("pending_review", [])):
        record = _select(value, "reason")
        if "locator" in value:
            record["locator"] = locator(value["locator"])
        yield emit("pending_review", _pointer("/quality/pending_review", index), record)
