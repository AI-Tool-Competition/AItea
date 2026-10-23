"""Private mapping snapshots preserve source coordinates without new parsing."""

import base64
import hashlib
import json
import sqlite3
import stat

import pytest

from app import archive_inventory
from app.archive_inventory import export_inventory
from app.service import Service

AT = "2026-10-13T00:00:00+00:00"


@pytest.fixture
def archive(tmp_path):
    service = Service(tmp_path / "data")
    with service.db.transaction() as conn:
        for owner in ("a", "b"):
            conn.execute("INSERT INTO users(id,name,is_demo,created_at) VALUES(?,?,0,?)", (owner, owner, AT))
    return service


def put(
    archive,
    identifier,
    manifest,
    *,
    owner="a",
    country="ID",
    level="primary",
    parent=None,
    version=1,
    family=None,
    path=None,
):
    original = archive.archive.blobs.put_bytes(owner, b"immutable original " + identifier.encode())
    if isinstance(manifest, dict):
        manifest.setdefault("source_hash", original.digest)
        raw = json.dumps(manifest, ensure_ascii=False)
    else:
        raw = manifest
    with archive.db.transaction() as conn:
        conn.execute(
            """INSERT INTO documents(id,owner_id,parent_id,title,filename,role,status,mime_type,size,
            version,content_hash,storage_path,tags,manifest,created_at,family_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identifier,
                owner,
                parent,
                identifier,
                identifier + ".doc",
                "secondary" if parent else "primary",
                "ready",
                "application/msword",
                original.size,
                version,
                original.digest,
                str(path or original.path),
                "[]",
                raw,
                AT,
                family or identifier,
            ),
        )
        if country:
            conn.execute("INSERT INTO document_countries VALUES(?,?)", (identifier, country))
        if not parent:
            conn.execute(
                "INSERT INTO legal_classifications VALUES(?,?,?,?,?,?)",
                (identifier, "country:" + (country or "ID"), level, "[]", 1, AT),
            )
    return original, raw


def records(report, owner="a", country="ID", filename="source-mapping.jsonl"):
    target = report["path"] / hashlib.sha256(owner.encode()).hexdigest() / country / filename
    return [json.loads(line) for line in target.read_text().splitlines()]


def selected(rows, kind):
    return [row for row in rows if row["record_kind"] == kind]


def test_html_visual_projection_preserves_literals_and_redacts_private_targets():
    from app.ingestion import parse_file
    from app.native_mapping_inventory import mappings, validate_mapping

    source = (
        '<main><p>Official trade rules.</p><img alt="Metadata only" '
        'src="https://official.example/diagram.png?token=PRIVATE_QUERY&amp;v=2"></main>'
    ).encode()
    manifest = parse_file("rules.html", source, "text/html")
    raw = json.dumps(manifest, ensure_ascii=False).encode()
    rows = list(mappings(manifest, hashlib.sha256(raw).hexdigest()))
    objects = [record for kind, _, record in rows if kind == "html_visual_object"]
    references = [record for kind, _, record in rows if kind == "html_visual_reference"]
    assert len(objects) == len(references) == 1
    assert objects[0]["alt_is_recognized_text"] is False
    assert references[0]["target_export_status"] == "redacted"
    assert "PRIVATE_QUERY" not in json.dumps(rows)
    assert references[0]["literal_reference"]["manifest_sha256"] == hashlib.sha256(raw).hexdigest()
    assert references[0]["literal_reference"]["json_pointer"].endswith("/raw_value")
    for _, _, record in rows:
        validate_mapping(record, manifest["source_hash"])
    manifest["html_source"]["objects"][0]["url_attributes"][0]["raw_value_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="literal hash"):
        list(mappings(manifest, "a" * 64))


@pytest.mark.parametrize("unsafe", [False, True])
def test_native_ocr_projection_binds_transcript_and_only_relative_cas_paths(unsafe):
    from app.native_mapping_inventory import mappings

    digest = hashlib.sha256(b"sent pixels").hexdigest()
    text = "Exact image transcription."
    path = f"files/blobs/{'a' * 64}/{digest[:2]}/{digest[2:4]}/{digest}"
    manifest = {
        "assets_pending": [
            {
                "asset_key": "image1",
                "image_hash": "b" * 64,
                "ocr": {
                    "image_sha256": digest,
                    "original_image_sha256": "b" * 64,
                    "text": text,
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "input_identity": "c" * 64,
                    "frame_basis": "all_frames_verified_single_frame",
                    "physical_pages_known": False,
                    "unknown_private_key": "PRIVATE_MODEL_KEY",
                    "image_blob": {
                        "content_hash": digest,
                        "size": 11,
                        "mime_type": "image/png",
                        "storage_path": "/private/tools/api-key.env" if unsafe else path,
                    },
                },
            }
        ]
    }
    if unsafe:
        with pytest.raises(ValueError, match="blob reference"):
            list(mappings(manifest, "d" * 64))
        return
    rows = list(mappings(manifest, "d" * 64))
    proof = rows[0][2]["ocr"]
    assert proof["image_blob"]["storage_path"] == path
    assert proof["text_reference"]["json_pointer"] == "/assets_pending/0/ocr/text"
    assert proof["text_reference"]["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert "PRIVATE_MODEL_KEY" not in json.dumps(rows)
    manifest["assets_pending"][0]["ocr"]["text"] = "Changed after checkpoint."
    with pytest.raises(ValueError, match="transcription hash"):
        list(mappings(manifest, "d" * 64))


def test_mineru_chart_projection_preserves_source_image_mapping_only_as_verified_cas():
    from app.native_mapping_inventory import mappings

    source_hash = "c" * 64
    image_hash = hashlib.sha256(b"chart image").hexdigest()
    image_path = f"files/blobs/{'a' * 64}/{image_hash[:2]}/{image_hash[2:4]}/{image_hash}"
    manifest = {
        "source_hash": source_hash,
        "parser": "mineru",
        "remote_parse": {"provider": "mineru", "state": "done"},
        "mineru_chart_assets": [
            {
                "asset_key": "mineru-chart-0123456789abcdef01234567",
                "element_key": "mineru-000001-000002",
                "block_type": "chart",
                "source_hash": source_hash,
                "image_hash": image_hash,
                "size": len(b"chart image"),
                "image_blob": {
                    "content_hash": image_hash,
                    "size": len(b"chart image"),
                    "mime_type": "image/png",
                    "storage_path": image_path,
                },
                "locator": {
                    "kind": "pdf",
                    "page": 1,
                    "physical_page": 1,
                    "source_hash": source_hash,
                    "provider_task_id": "task-1",
                    "provider_page_idx": 0,
                    "bbox": [1, 2, 3, 4],
                    "coordinate_system": "mineru-0-1000",
                },
            }
        ],
    }
    rows = list(mappings(manifest, "d" * 64))
    assert rows[0][0] == "mineru_chart_image"
    assert rows[0][2]["image_blob"]["storage_path"] == image_path
    manifest["mineru_chart_assets"][0]["image_blob"]["storage_path"] = "/private/chart.png"
    with pytest.raises(ValueError, match="original image blob"):
        list(mappings(manifest, "d" * 64))


def simple(**extra):
    return {
        "chunks": [{"id": "chunk", "text": "Native law text", "locator": {"kind": "text"}}],
        "elements": [],
        "quality": {"complete": True},
        **extra,
    }


def test_default_export_remains_v4_and_explicit_mapping_is_private_v5(archive):
    put(archive, "law", simple())
    default = export_inventory(archive.db.data_dir)
    assert default["summary"]["format_version"] == 6
    assert not list(default["path"].rglob("source-mapping.jsonl"))
    report = export_inventory(archive.db.data_dir, owner="a", country="ID", include_source_mapping=True)
    rows = records(report)
    assert report["summary"]["format_version"] == 6
    assert report["summary"]["source_mapping_documents"] == 1
    assert report["summary"]["source_mapping_records"] == len(rows)
    assert rows[0]["text_complete"] is True
    assert rows[-1]["projection_complete"] is True
    assert all(row["mapping_record_indexed"] is False for row in rows)
    for target in report["path"].rglob("*"):
        assert stat.S_IMODE(target.stat().st_mode) == (0o700 if target.is_dir() else 0o600)


def test_filters_versions_and_secondary_legal_text_but_ignores_attachment_manifest(archive, monkeypatch):
    first, _ = put(archive, "law-v1", simple(), family="law", version=1)
    second, _ = put(archive, "law-v2", simple(), family="law", version=2)
    put(archive, "guide", simple(quality={"complete": False}), level="secondary")
    put(archive, "attachment", "PRIVATE_ATTACHMENT_NOT_JSON", parent="law-v2", country=None)
    put(archive, "other-owner", simple(), owner="b")
    put(archive, "other-country", simple(), country="SG")
    monkeypatch.setattr(
        "app.parsing.parse_bounded", lambda *args, **kwargs: pytest.fail("inventory parsed a source")
    )
    report = export_inventory(
        archive.db.data_dir, owner="a", country="ID", include_source_mapping=True, include_text=True
    )
    rows = records(report)
    docs = {row["document_id"]: row for row in selected(rows, "document")}
    assert set(docs) == {"law-v1", "law-v2", "guide", "attachment"}
    assert docs["law-v1"]["source_hash"] == first.digest
    assert docs["law-v2"]["source_hash"] == second.digest
    assert docs["law-v2"]["document_version"] == 2
    assert docs["guide"]["legal_level"] == "secondary" and docs["guide"]["text_complete"] is False
    attachment = docs["attachment"]
    assert attachment["mapping_status"] == "attachment_original_only"
    assert attachment["manifest_size"] is None and "manifest_sha256" not in attachment
    assert [row["record_kind"] for row in rows if row["document_id"] == "attachment"] == [
        "document",
        "document_end",
    ]
    texts = records(report, filename="texts.jsonl")
    assert {row["document_id"] for row in texts} == {"law-v1", "law-v2", "guide"}
    assert "PRIVATE_ATTACHMENT" not in json.dumps(rows)


def test_docx_package_images_relationships_and_source_anchors_survive_allowlist(archive):
    part_hash, image_hash = "1" * 64, "2" * 64
    anchor = {
        "kind": "docx",
        "package_part": "word/header1.xml",
        "part_hash": part_hash,
        "xml_path": "/w:hdr/w:p[1]",
        "paragraph": 1,
    }
    manifest = simple(
        parser="docx-basic",
        parser_version="2",
        pipeline_version="docx-package-mapping-v2",
        quality={"complete": False, "review_reasons": ["images_pending_vision"]},
        chunks=[
            {
                "id": "mixed",
                "text": "BODY_NOT_EXPORTED",
                "locator": {"kind": "docx", "source_anchors": [anchor]},
                "embedding": [0.125],
            }
        ],
        package_parts=[
            {"package_part": "word/header1.xml", "size": 45, "sha256": part_hash, "status": "native_mapped"}
        ],
        relationships=[
            {
                "id": "rId1",
                "type": "image",
                "target_mode": "Internal",
                "target_part": "word/media/image1.png",
                "target_hash": image_hash,
                "target": "../media/image1.png",
                "status": "internal",
            },
            {
                "id": "rId2",
                "type": "hyperlink",
                "target_mode": "External",
                "target": "https://official.example/law?token=PRIVATE_TOKEN",
                "status": "external_not_fetched",
            },
        ],
        assets_pending=[
            {
                "asset_key": "image1",
                "package_part": "word/media/image1.png",
                "image_hash": image_hash,
                "size": 80,
                "status": "pending_vision",
                "locator": anchor,
                "api_key": "PRIVATE_IMAGE_KEY",
            }
        ],
        tables=[
            {
                "table": 1,
                "row_count": 2,
                "column_count": 2,
                "locator": anchor,
                "cells": [
                    {
                        "row": 1,
                        "column_start": 1,
                        "column_end": 2,
                        "column_span": 2,
                        "row_span": 2,
                        "locator": anchor,
                        "merged_origin": {"row": 1, "column": 1},
                    }
                ],
            }
        ],
        unknown_request={"secret": "PRIVATE_REQUEST_SECRET"},
    )
    _, raw = put(archive, "docx", manifest)
    report = export_inventory(archive.db.data_dir, include_source_mapping=True)
    rows = records(report)
    expected = hashlib.sha256(raw.encode()).hexdigest()
    assert all(row["manifest_sha256"] == expected for row in rows)
    assert selected(rows, "text_anchor")[0]["mapping"]["locator"]["source_anchors"] == [anchor]
    assert selected(rows, "image")[0]["mapping"]["image_hash"] == image_hash
    assert selected(rows, "image")[0]["mapping"]["vision_completed_by_export"] is False
    relationship = selected(rows, "package_relationship")[1]["mapping"]
    assert relationship["id"] == "rId2" and relationship["status"] == "external_not_fetched"
    assert relationship["target_export_status"] == "redacted" and "target" not in relationship
    assert (
        relationship["target_sha256"]
        == hashlib.sha256(manifest["relationships"][1]["target"].encode()).hexdigest()
    )
    assert selected(rows, "native_table_cell")[0]["mapping"]["column_span"] == 2
    serialized = json.dumps(rows)
    for value in (
        "PRIVATE_TOKEN",
        "PRIVATE_IMAGE_KEY",
        "PRIVATE_REQUEST_SECRET",
        "BODY_NOT_EXPORTED",
        "embedding",
    ):
        assert value not in serialized
    assert rows[0]["text_complete"] is False


@pytest.mark.parametrize(
    "target,visible",
    [
        ("https://official.example/law", True),
        ("https://official.example/law?language=en", False),
        ("https://official.example/law?x=opaque", False),
        ("https://official.example/law?x-amz-security-token=SECRET", False),
        ("https://official.example/law?api_key=SECRET", False),
        ("https://user:SECRET@official.example/law", False),
        ("https://127.0.0.1/law", False),
        ("https://internal.local/law", False),
        ("file:///PRIVATE/path", False),
    ],
)
def test_external_relationship_preserves_identity_without_secrets_or_network(
    archive, target, visible, monkeypatch
):
    put(
        archive,
        "law",
        simple(
            relationships=[
                {"id": "r", "target": target, "target_mode": "External", "status": "external_not_fetched"}
            ]
        ),
    )
    monkeypatch.setattr(
        "app.ingestion._public_addresses", lambda *args, **kwargs: pytest.fail("export resolved DNS")
    )
    mapping = selected(
        records(export_inventory(archive.db.data_dir, include_source_mapping=True)), "package_relationship"
    )[0]["mapping"]
    assert ("target" in mapping) is visible
    assert mapping["target_sha256"] == hashlib.sha256(target.encode()).hexdigest()


def test_doc_source_cp_and_converted_artifact_remain_separate_and_inline_is_referenced(archive):
    artifact = b"<book><para>Converted text</para></book>" * 2000
    digest = hashlib.sha256(artifact).hexdigest()
    manifest = simple(
        quality={"complete": False},
        legacy={
            "format": "doc",
            "tools": {"path": "/PRIVATE/TOOL", "key": "PRIVATE_TOOL_KEY"},
            "source_storage": {
                "cp_count": 7,
                "layout_visibility_mapping": "unverified",
                "physical_pages": False,
                "pieces": [
                    {
                        "kind": "compound_stream",
                        "stream": "WordDocument",
                        "cp_start": 1,
                        "cp_end": 7,
                        "stream_offset": 512,
                        "length": 12,
                        "sha256": "3" * 64,
                        "text": "SOURCE_TEXT_NOT_EXPORTED",
                    }
                ],
            },
            "conversion": {
                "artifact": {
                    "kind": "antiword-docbook-xml",
                    "sha256": digest,
                    "size": len(artifact),
                    "encoding": "utf-8",
                    "content_base64": base64.b64encode(artifact).decode(),
                    "source_mapping": "unverified",
                },
                "nodes": [
                    {"xpath": "/book/para[1]", "tag": "para", "attributes": {"api_key": "PRIVATE_ATTRIBUTE"}}
                ],
                "declared_breaks": [{"kind": "declared_break", "physical_page_verified": False}],
            },
        },
    )
    _, raw = put(archive, "doc", manifest)
    rows = records(export_inventory(archive.db.data_dir, include_source_mapping=True))
    cp = selected(rows, "compound_pieces")[0]["mapping"]
    assert cp["cp_start"] == 1 and cp["cp_end"] == 7 and cp["stream_offset"] == 512
    converted = selected(rows, "conversion_artifact")[0]["mapping"]
    reference = converted["bytes_reference"]
    assert reference["json_pointer"] == "/legacy/conversion/artifact/content_base64"
    assert reference["storage"] == "documents.manifest" and reference["content_hash"] == digest
    assert reference["manifest_sha256"] == hashlib.sha256(raw.encode()).hexdigest()
    assert reference["size"] == len(artifact)
    assert selected(rows, "converted_nodes")[0]["mapping"]["xpath"] == "/book/para[1]"
    assert selected(rows, "converted_declared_breaks")[0]["mapping"]["physical_page_verified"] is False
    assert rows[0]["text_complete"] is False and rows[-1]["projection_complete"] is True
    serialized = json.dumps(rows)
    assert "content_base64" not in converted and base64.b64encode(artifact).decode() not in serialized
    assert "PRIVATE_" not in serialized and "SOURCE_TEXT_NOT_EXPORTED" not in serialized


def test_wpf_image_bytes_and_conversion_events_are_mapped_without_claiming_source_records(archive):
    image = b"source image bytes"
    manifest = simple(
        quality={"complete": False},
        legacy={
            "format": "wpf",
            "images": [
                {
                    "image_sha256": hashlib.sha256(image).hexdigest(),
                    "size": len(image),
                    "content_base64": base64.b64encode(image).decode(),
                    "state": "vision_pending",
                    "original_object_mapping": "unverified",
                    "image_bytes_verified": True,
                    "locator": {
                        "kind": "conversion_artifact",
                        "artifact_sha256": "4" * 64,
                        "xpath": "/html/body/img[1]",
                    },
                }
            ],
            "events": [
                {
                    "ordinal": 3,
                    "artifact_byte_offset": 125,
                    "length": 20,
                    "sha256": "5" * 64,
                    "event": "PRIVATE_EVENT_NOT_EXPORTED",
                }
            ],
            "tables": [
                {
                    "xpath": "/html/body/table[1]",
                    "original_merge_geometry_verified": False,
                    "rows": [
                        [
                            {
                                "row": 1,
                                "column": 1,
                                "text": "PRIVATE_CELL",
                                "attributes": {"secret": "PRIVATE_KEY"},
                            }
                        ]
                    ],
                }
            ],
        },
    )
    put(archive, "wpf", manifest)
    rows = records(export_inventory(archive.db.data_dir, include_source_mapping=True))
    asset = selected(rows, "converted_images")[0]["mapping"]
    assert asset["state"] == "vision_pending" and asset["original_object_mapping"] == "unverified"
    assert asset["bytes_reference"]["json_pointer"] == "/legacy/images/0/content_base64"
    assert selected(rows, "converted_events")[0]["mapping"]["artifact_byte_offset"] == 125
    assert selected(rows, "converted_table_cell")[0]["mapping"] == {
        "row": 1,
        "column": 1,
        "converted_attributes": {},
    }
    assert "PRIVATE_" not in json.dumps(rows)


def test_xls_native_values_formula_and_print_offsets_are_not_converted_to_guessed_values(archive):
    source_record = {
        "kind": "xls_biff_record",
        "stream": "Workbook",
        "stream_offset": 120,
        "length": 14,
        "sha256": "6" * 64,
    }
    manifest = simple(
        quality={"complete": False},
        elements=[
            {
                "element_key": "row1",
                "kind": "table",
                "locator": {"kind": "spreadsheet", "sheet": "Tea", "cell_range": "A1:B1"},
                "metadata_json": {
                    "cells": [
                        {
                            "cell": "A1",
                            "row": 1,
                            "column": 1,
                            "raw_type": 1,
                            "raw_value": "000012",
                            "display_text": "000012",
                            "source_record": source_record,
                        },
                        {
                            "cell": "B1",
                            "raw_value": 3.25,
                            "raw_float_hex": (3.25).hex(),
                            "formula_cache": {
                                "cached_result_hex": "0000000000000a40",
                                "tokens_sha256": "7" * 64,
                                "source_record": source_record,
                                "tokens_hex": "PRIVATE_NOT_EXECUTED",
                            },
                        },
                    ]
                },
            }
        ],
        workbook={
            "source_hash": None,
            "date_mode": 1,
            "stream_name": "Workbook",
            "stream_sha256": "8" * 64,
            "print_fields": [
                {
                    "sheet": "Tea",
                    "source_area": "print_header",
                    "source_record": source_record,
                    "sections": {"left": [{"char_start": 0, "char_end": 3, "text": "Tea"}]},
                    "dynamic_fields": [
                        {
                            "field": "page_number",
                            "section": "center",
                            "char_start": 3,
                            "char_end": 5,
                            "raw_code": "&P",
                        }
                    ],
                }
            ],
        },
    )
    manifest["workbook"].pop("source_hash")
    put(archive, "xls", manifest)
    rows = records(export_inventory(archive.db.data_dir, include_source_mapping=True))
    cells = selected(rows, "spreadsheet_cell")
    assert cells[0]["mapping"]["raw_value"] == "000012"
    assert cells[1]["mapping"]["raw_float_hex"] == (3.25).hex()
    assert cells[0]["mapping"]["source_record"] == source_record
    assert selected(rows, "spreadsheet_print_dynamic")[0]["mapping"]["char_start"] == 3
    assert selected(rows, "spreadsheet_print_literal")[0]["mapping"]["text"] == "Tea"
    assert "PRIVATE_NOT_EXECUTED" not in json.dumps(rows)


def test_mdb_memo_row_storage_null_and_column_definition_remain_exact(archive):
    memo = {
        "database_page": 7,
        "row_slot": 3,
        "file_offset": 29000,
        "start": 328,
        "length": 70,
        "sha256": "9" * 64,
    }
    field = {
        "start": 20,
        "length": 12,
        "storage_null_bit": False,
        "payload_sha256": "a" * 64,
        "memo_storage": [memo],
    }
    manifest = simple(
        quality={"complete": False},
        elements=[
            {
                "element_key": "row1",
                "kind": "table",
                "locator": {
                    "kind": "database_storage",
                    "table": "Laws",
                    "row_ordinal": 1,
                    "database_page": 4,
                    "row_slot": 2,
                    "file_offset": 17000,
                    "start": 616,
                    "length": 128,
                    "sha256": "b" * 64,
                },
                "metadata_json": {
                    "field_storage": [field, {"start": 0, "length": 0, "storage_null_bit": True}],
                    "column_values": ["MEMO_BODY_NOT_EXPORTED", None],
                },
            }
        ],
        database={
            "tables": [
                {
                    "name": "Laws",
                    "declared_rows": 1,
                    "recovered_rows": 1,
                    "definition_page": 2,
                    "columns": [
                        {
                            "name": "Body",
                            "type": 12,
                            "source_type": "Memo",
                            "properties": {"secret": "PRIVATE"},
                        }
                    ],
                }
            ],
            "table_mapping_complete": True,
            "queries_executed": False,
            "macros_executed": False,
            "reader_sha256": "c" * 64,
            "reader_protocol": "read-only",
        },
    )
    put(archive, "mdb", manifest)
    rows = records(export_inventory(archive.db.data_dir, include_source_mapping=True))
    fields = selected(rows, "database_field")
    assert fields[0]["mapping"] == field
    assert fields[1]["mapping"]["storage_null_bit"] is True
    assert selected(rows, "database_column")[0]["mapping"]["source_type"] == "Memo"
    assert selected(rows, "database")[0]["mapping"]["queries_executed"] is False
    assert "MEMO_BODY_NOT_EXPORTED" not in json.dumps(rows) and "PRIVATE" not in json.dumps(rows)


@pytest.mark.parametrize(
    "manifest",
    [
        "{",
        '{"chunks":[],"chunks":[]}',
        '{"chunks":[],"value":NaN}',
        simple(source_hash="d" * 64),
        simple(elements=[{"locator": {"kind": "text", "source_hash": "e" * 64}}]),
        simple(elements=[{"locator": {"kind": "text", "stream_offset": -1}}]),
        simple(legacy={"images": [{"image_sha256": "f" * 64, "size": 3, "content_base64": "YWJj"}]}),
        simple(legacy={"artifact": {"sha256": "f" * 64, "size": 3, "content_base64": "YWI="}}),
    ],
)
def test_corrupt_manifest_fails_closed_without_aborting_other_documents(archive, manifest):
    put(archive, "invalid", manifest)
    put(archive, "valid", simple())
    report = export_inventory(archive.db.data_dir, include_source_mapping=True)
    rows = records(report)
    invalid = [row for row in rows if row["document_id"] == "invalid"]
    assert selected(invalid, "mapping_gap")
    assert selected(invalid, "document_end")[0]["projection_complete"] is False
    assert (
        selected([row for row in rows if row["document_id"] == "valid"], "document_end")[0][
            "projection_complete"
        ]
        is True
    )
    assert report["summary"]["source_mapping_gaps"] >= 1


def test_oversized_manifest_is_not_loaded_and_attachment_is_never_read(archive, monkeypatch):
    put(archive, "oversized", simple(padding="x" * 2048))
    put(archive, "attachment", "not-json", parent="oversized", country=None)
    monkeypatch.setattr(archive_inventory, "MAX_MANIFEST_BYTES", 100)
    monkeypatch.setattr(
        archive_inventory, "_mapping_manifest", lambda *args: pytest.fail("oversized manifest was loaded")
    )
    report = export_inventory(archive.db.data_dir, include_source_mapping=True)
    docs = {row["document_id"]: row for row in selected(records(report), "document")}
    assert docs["oversized"]["mapping_status"] == "manifest_unavailable"
    assert docs["attachment"]["mapping_status"] == "attachment_original_only"


def test_oversized_projected_record_gets_gap_and_immutable_manifest_pointer(archive, monkeypatch):
    put(
        archive,
        "law",
        simple(chunks=[{"id": "chunk", "locator": {"kind": "docx", "xml_path": "/" + "x" * 6000}}]),
    )
    monkeypatch.setattr(archive_inventory, "MAX_RECORD_BYTES", 2048)
    rows = records(export_inventory(archive.db.data_dir, include_source_mapping=True))
    gap = selected(rows, "mapping_gap")[0]
    assert gap["manifest_pointer"] == "/chunks/0" and gap["manifest_sha256"]
    assert gap["anomalies"] == ["mapping_record_exceeds_limit"]
    assert rows[-1]["projection_complete"] is False


@pytest.mark.parametrize("unsafe", ["symlink", "outside", "parent"])
def test_original_path_uses_existing_no_follow_boundary_without_exporting_private_path(
    archive, tmp_path, unsafe
):
    outside = tmp_path / "private-original"
    outside.write_bytes(b"unrelated")
    if unsafe == "symlink":
        path = archive.db.data_dir / "unsafe-link"
        path.symlink_to(outside)
    elif unsafe == "outside":
        path = outside
    else:
        path = "../" + outside.name
    put(archive, "law", simple(), path=path)
    rows = records(export_inventory(archive.db.data_dir, include_source_mapping=True))
    doc = selected(rows, "document")[0]
    assert doc["original"]["relative_path"] is None
    assert doc["anomalies"]
    assert str(outside) not in json.dumps(rows)


def test_export_does_not_rewrite_archive_or_index(archive):
    put(archive, "law", simple())
    with sqlite3.connect(archive.db.path) as conn:
        before = {
            name: conn.execute("SELECT * FROM " + name).fetchall()
            for name in ("documents", "retrieval_dirty", "retrieval_vectors", "schema_migrations")
        }
    export_inventory(archive.db.data_dir, include_source_mapping=True, include_text=True)
    with sqlite3.connect(archive.db.path) as conn:
        after = {name: conn.execute("SELECT * FROM " + name).fetchall() for name in before}
    assert {name: [tuple(row) for row in rows] for name, rows in before.items()} == after


@pytest.mark.parametrize(
    "version,pipeline,quality",
    [
        ("1", "docx-basic-v1", None),
        ("2", "docx-package-mapping-v2", None),
    ],
)
def test_old_or_unverified_docx_cache_is_never_reported_as_complete(archive, version, pipeline, quality):
    put(
        archive,
        "old-docx",
        simple(parser="docx-basic", parser_version=version, pipeline_version=pipeline, quality=quality),
    )
    report = export_inventory(archive.db.data_dir, include_source_mapping=True, include_text=True)
    assert selected(records(report), "document")[0]["text_complete"] is False
    assert records(report, filename="texts.jsonl")[0]["text_complete"] is False
    assert report["summary"]["complete_text_documents"] == 0
