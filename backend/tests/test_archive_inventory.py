"""Offline catalogs describe immutable originals without moving or reading them."""

import hashlib
import json
import sqlite3
import struct
from pathlib import Path

import pytest
from app.bootstrap import Service

AT = "2026-10-12T00:00:00+00:00"


@pytest.fixture
def archive(tmp_path):
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        for owner in ("owner-a", "owner-b"):
            conn.execute("INSERT INTO users(id,name,is_demo,created_at) VALUES(?,?,0,?)", (owner, owner, AT))
    return service


def document(
    archive,
    id,
    *,
    owner="owner-a",
    country="ID",
    family=None,
    version=1,
    parent=None,
    level="primary",
    body=b"original",
    path=None,
):
    blob = archive.archive.blobs.put_bytes(owner, body)
    with archive.db.transaction() as conn:
        conn.execute(
            """INSERT INTO documents(id,owner_id,parent_id,title,filename,role,status,mime_type,size,
            version,content_hash,storage_path,tags,source_url,manifest,created_at,family_id)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                id,
                owner,
                parent,
                f"Title {id}",
                f"{id}.pdf",
                "secondary" if parent else "primary",
                "ready",
                "application/pdf",
                len(body),
                version,
                blob.digest,
                str(path or blob.path),
                '["topic:tea"]',
                "https://official.example/law.pdf",
                json.dumps(
                    {
                        "elements": [{"text": "PRIVATE_BODY"}],
                        "chunks": [{"text": "PRIVATE_CHUNK", "embedding": [0.123456789]}],
                    }
                ),
                AT,
                family or id,
            ),
        )
        if country:
            conn.execute("INSERT INTO document_countries VALUES(?,?)", (id, country))
        if not parent:
            conn.execute(
                "INSERT INTO legal_classifications VALUES(?,?,?,?,?,?)",
                (id, f"country:{country}" if country else "legacy", level, '["source:official"]', 1, AT),
            )
            conn.execute(
                "INSERT INTO document_metadata VALUES(?,?,?,?,?,?)",
                (
                    id,
                    json.dumps(
                        {
                            "kind": "law",
                            "review_status": "draft",
                            "jurisdiction": country or "",
                            "api_key": "DO_NOT_EXPORT_KEY",
                        }
                    ),
                    1,
                    None,
                    None,
                    AT,
                ),
            )
    return blob


def page(archive, *, owner="owner-a", country="ID"):
    blob = archive.archive.blobs.put_bytes(owner, b"<html>CATALOG_PRIVATE_BODY</html>")
    with archive.db.transaction() as conn:
        conn.execute(
            """INSERT INTO sources(id,owner_id,name,url,enabled,interval_hours,tags,status,created_at,updated_at)
            VALUES('source',?,'Official catalog','https://official.example/list',0,24,'[]','idle',?,?)""",
            (owner, AT, AT),
        )
        conn.execute(
            "INSERT INTO checklist_bindings VALUES('binding',?,'indonesia-20261003',2,'1.3',?,'source','{}',?)",
            (owner, country, AT),
        )
        conn.execute(
            "INSERT INTO collection_runs VALUES('run',?,'source','request','hash','{}','completed',1,NULL,?,?)",
            (owner, AT, AT),
        )
        conn.execute(
            """INSERT INTO collection_items(id,run_id,owner_id,url,host,level,status,created_at,purpose)
            VALUES('page-item','run',?,'https://official.example/list','official.example','primary','completed',?,'catalog')""",
            (owner, AT),
        )
        conn.execute(
            "INSERT INTO checklist_pages VALUES('page-item','binding',?,?,?,?,?,'catalog','[]',?)",
            (
                "https://official.example/list",
                "https://official.example/list",
                blob.digest,
                str(blob.path),
                blob.size,
                AT,
            ),
        )
    return blob


def export(archive, **kwargs):
    from app.contexts.collection.infrastructure.archive_inventory import export_inventory

    return export_inventory(archive.db.data_dir, **kwargs)


def records(report, owner="owner-a", country="ID", filename="documents.jsonl"):
    namespace = hashlib.sha256(owner.encode()).hexdigest()
    path = report["path"] / namespace / country / filename
    return [json.loads(line) for line in path.read_text().splitlines()]


def index_records(archive, document_id, *, current=True, complete=True, vectors=None):
    vectors = vectors if vectors is not None else [("model-a", True), ("model-b", True), ("model-a", False)]
    with archive.db.transaction() as conn:
        source_hash = conn.execute(
            "SELECT content_hash FROM documents WHERE id=?", (document_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO retrieval_documents VALUES(?,?,?,?,1)",
            (document_id, source_hash if current else "stale-source", int(complete), len(vectors)),
        )
        for index, (fingerprint, matching) in enumerate(vectors):
            cursor = conn.execute(
                """INSERT INTO retrieval_chunks(document_id,chunk_id,ordinal,text,retrieval_text,tokens,
                locator,content_hash,input_hash) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    f"chunk-{index}",
                    index,
                    "PRIVATE_INDEX_TEXT",
                    "PRIVATE_INDEX_CONTEXT",
                    "PRIVATE_INDEX_TOKENS",
                    "{}",
                    "body-hash",
                    f"input-{index}",
                ),
            )
            conn.execute("INSERT OR IGNORE INTO retrieval_models VALUES(?,2)", (fingerprint,))
            conn.execute(
                "INSERT INTO retrieval_vectors VALUES(?,?,?,?,?)",
                (
                    cursor.lastrowid,
                    fingerprint,
                    f"input-{index}" if matching else "stale-input",
                    2,
                    struct.pack("<ff", 0.6, 0.8),
                ),
            )


def test_retrieval_inventory_reports_current_inputs_and_models_without_vector_bytes_or_rag_claims(archive):
    document(archive, "indexed")
    document(archive, "not-indexed")
    document(archive, "other-owner", owner="owner-b")
    index_records(archive, "indexed")
    index_records(archive, "other-owner", vectors=[("PRIVATE_OTHER_MODEL", True)])
    report = export(archive, owner="owner-a", country="ID")
    metadata = {row["document_id"]: row["retrieval"] for row in records(report)}
    assert metadata["indexed"] == {
        "status": "available",
        "index_present": True,
        "indexed_chunk_count": 3,
        "declared_chunk_count": 3,
        "source_hash_matches": True,
        "complete": True,
        "matching_vector_count": 2,
        "fingerprint_counts": {"model-a": 1, "model-b": 1},
    }
    assert metadata["not-indexed"] == {
        "status": "available",
        "index_present": False,
        "indexed_chunk_count": 0,
        "declared_chunk_count": None,
        "source_hash_matches": None,
        "complete": None,
        "matching_vector_count": 0,
        "fingerprint_counts": {},
    }
    content = "\n".join(path.read_text() for path in report["path"].rglob("*.jsonl"))
    assert all(
        secret not in content
        for secret in ("PRIVATE_INDEX_TEXT", "PRIVATE_INDEX_CONTEXT", "PRIVATE_OTHER_MODEL")
    )
    assert '"vector"' not in content and '"retrieval_eligible":true' not in content
    assert report["summary"]["format_version"] == 8
    assert (
        report["summary"]["retrieval_scope"]
        == "stored_index_and_input_hash_matching_vectors_not_rag_eligibility"
    )


def test_retrieval_inventory_distinguishes_stale_source_partial_index_and_declared_count(archive):
    document(archive, "stale")
    index_records(archive, "stale", current=False, complete=False)
    with archive.db.transaction() as conn:
        conn.execute("UPDATE retrieval_documents SET chunk_count=8 WHERE document_id='stale'")
    metadata = records(export(archive))[0]["retrieval"]
    assert metadata["source_hash_matches"] is False
    assert metadata["complete"] is False
    assert metadata["indexed_chunk_count"] == 3
    assert metadata["declared_chunk_count"] == 8
    assert metadata["matching_vector_count"] == 2


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ({"elements": [{"text": "body"}], "chunks": [{"text": "body"}]}, True),
        (
            {
                "parser": "html-basic",
                "parser_version": "2",
                "elements": [{"text": "body"}],
                "chunks": [{"text": "body"}],
                "quality": {"complete": True},
            },
            False,
        ),
        (
            {
                "chunks": [{"text": "body"}],
                "quality": {"complete": True},
                "pages": [
                    {"page": 1, "status": "ocr_complete", "text": "body", "ocr": {"uncertain_spans": ["1"]}}
                ],
            },
            False,
        ),
    ],
)
def test_inventory_text_complete_uses_real_gate_instead_of_quality_flag(archive, manifest, expected):
    from app.contexts.knowledge.infrastructure.parsing.ingestion_quality import text_complete

    document(archive, "law")
    with archive.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id='law'", (json.dumps(manifest),))
    row = records(export(archive))[0]
    assert row["parsing"]["complete"] is expected
    assert row["parsing"]["complete"] is text_complete(manifest)


def test_retrieval_inventory_uses_same_snapshot_across_batches_and_concurrent_vector_updates(
    archive, monkeypatch
):
    from app.contexts.collection.infrastructure import archive_inventory as archive_inventory

    for identifier in ("first", "second"):
        document(archive, identifier)
        index_records(archive, identifier, vectors=[("model-a", True)])
    monkeypatch.setattr(archive_inventory, "BATCH_SIZE", 1)
    write_document = archive_inventory._write_document
    changed = []

    def concurrent_change(stream, record):
        write_document(stream, record)
        if not changed:
            changed.append(True)
            with archive.db.transaction() as conn:
                conn.execute(
                    """DELETE FROM retrieval_vectors WHERE chunk_rowid IN
                    (SELECT rowid FROM retrieval_chunks WHERE document_id='second')"""
                )

    monkeypatch.setattr(archive_inventory, "_write_document", concurrent_change)
    report = export(archive)
    assert [row["retrieval"]["matching_vector_count"] for row in records(report)] == [1, 1]
    with archive.db.transaction() as conn:
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 1


def test_retrieval_inventory_reads_metadata_in_bounded_batches_not_per_document(archive, monkeypatch):
    from app.contexts.collection.infrastructure import archive_inventory as archive_inventory

    for identifier in ("one", "two", "three"):
        document(archive, identifier)
    monkeypatch.setattr(archive_inventory, "BATCH_SIZE", 2)
    connect = sqlite3.connect
    statements = []

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(archive_inventory.sqlite3, "connect", traced)
    report = export(archive)
    assert len(records(report)) == 3
    assert sum("FROM retrieval_documents i JOIN documents" in sql for sql in statements) == 2
    assert sum("FROM retrieval_chunks" in sql for sql in statements) == 4


def test_accounts_countries_and_history_have_stable_separate_catalogs(archive):
    original = document(archive, "law-v1", family="law", version=1)
    document(archive, "law-v2", family="law", version=2, body=b"new original")
    shared = document(archive, "sg-law", country="SG")
    document(archive, "other-account", owner="owner-b")
    document(archive, "unassigned", country=None)
    report = export(archive)
    assert original.path == shared.path
    assert [row["document_id"] for row in records(report)] == ["law-v1", "law-v2"]
    assert [row["version"] for row in records(report)] == [1, 2]
    assert records(report, country="SG")[0]["document_id"] == "sg-law"
    assert records(report, owner="owner-b")[0]["document_id"] == "other-account"
    assert records(report, country="unassigned")[0]["country"] is None
    assert report["summary"]["documents"] == 5 and report["summary"]["owners"] == 2
    assert report["summary"]["countries"]["ID"]["documents"] == 3
    assert report["summary"]["countries"]["SG"]["documents"] == 1
    assert (report["path"] / "COMPLETE").is_file()
    assert json.loads((report["path"] / "summary.json").read_text()) == report["summary"]


def test_export_filters_account_and_country_including_inherited_attachments(archive):
    document(archive, "main")
    document(archive, "attachment", parent="main", country=None)
    document(archive, "sg-law", country="SG")
    document(archive, "private-other", owner="owner-b")
    report = export(archive, owner="owner-a", country="ID")
    rows = records(report)
    assert {row["document_id"] for row in rows} == {"main", "attachment"}
    attachment = next(row for row in rows if row["document_id"] == "attachment")
    assert attachment["country"] == "ID" and attachment["legal_level"] == "attachment"
    assert attachment["parent_id"] == "main"
    assert list(report["summary"]["countries"]) == ["ID"]
    assert report["summary"]["owners"] == 1
    assert len(list(report["path"].glob("*/ID/documents.jsonl"))) == 1


def test_region_catalog_preserves_source_page_and_crop_without_exporting_readings(archive):
    original = document(archive, "main")
    image = archive.archive.blobs.put_bytes("owner-a", b"region pixels")
    with archive.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET manifest=? WHERE id='main'",
            (
                json.dumps(
                    {
                        "pages": [
                            {
                                "page": 2,
                                "source_hash": original.digest,
                                "status": "ocr_complete",
                                "ocr": {
                                    "model": "vision-model",
                                    "recovery": {
                                        "status": "complete",
                                        "regions": [
                                            {
                                                "region_id": "a" * 64,
                                                "source_hash": original.digest,
                                                "page": 2,
                                                "region_bbox": [10, 20, 40, 60],
                                                "image_sha256": image.digest,
                                                "render_version": "bounded-region-png-v1",
                                                "dpi": 400,
                                                "image_blob": {
                                                    "storage_path": str(image.path),
                                                    "size": image.size,
                                                    "content_hash": image.digest,
                                                },
                                                "pass_number": 1,
                                                "reading_hash": "b" * 64,
                                                "reading": {
                                                    "text": "PRIVATE_REGION_READING",
                                                    "api_key": "PRIVATE_REGION_KEY",
                                                },
                                            }
                                        ],
                                    },
                                },
                            }
                        ]
                    }
                ),
            ),
        )
    report = export(archive, owner="owner-a", country="ID")
    rows = records(report, filename="ocr-regions.jsonl")
    assert len(rows) == 1
    region = rows[0]
    assert region["document_id"] == "main" and region["document_version"] == 1
    assert region["source_hash"] == original.digest and region["page"] == 2
    assert region["region_bbox"] == [10, 20, 40, 60] and region["pass_number"] == 1
    assert region["image_hash"] == image.digest and region["reading_hash"] == "b" * 64
    assert region["original"]["exists"] and region["anomalies"] == []
    assert report["summary"]["ocr_regions"] == 1
    serialized = json.dumps(rows)
    assert "PRIVATE_REGION_READING" not in serialized and "PRIVATE_REGION_KEY" not in serialized


def test_relations_retain_exact_versions_and_country_level_wins(archive):
    document(archive, "main-v1", family="main", version=1)
    document(archive, "main-v2", family="main", version=2)
    document(archive, "related-v1", family="related", version=1, level="secondary")
    document(archive, "related-v2", family="related", version=2, level="secondary")
    with archive.db.transaction() as conn:
        conn.execute(
            "INSERT INTO legal_classifications VALUES('related-v1','ordinary','primary','[]',1,?)", (AT,)
        )
        conn.execute(
            "INSERT INTO legal_relations VALUES('edge','owner-a','country:ID','main-v1','related-v1','cites','Article 3',?)",
            (AT,),
        )
    rows = {row["document_id"]: row for row in records(export(archive))}
    edges = records(export(archive), filename="relations.jsonl")
    edge = edges[0]
    assert (
        edge["source_document_id"],
        edge["source_version"],
        edge["target_document_id"],
        edge["target_version"],
    ) == ("main-v1", 1, "related-v1", 1)
    assert len(edges) == 1
    assert rows["related-v1"]["legal_level"] == "secondary"
    assert len(rows["related-v1"]["classifications"]) == 2


def test_catalog_pages_remain_separate_and_do_not_export_body_or_secrets(archive):
    document(archive, "law")
    page(archive)
    with archive.db.transaction() as conn:
        conn.execute(
            "INSERT INTO checklist_candidates VALUES('candidate','binding','https://official.example/law.pdf','Law',NULL,'https://official.example/list','stored','law',2,'',?)",
            (AT,),
        )
        conn.execute(
            """INSERT INTO checklist_candidate_source_records
            VALUES('candidate',?,?,?,?)""",
            (
                json.dumps(
                    {
                        "provider": "setneg",
                        "interpretation": "unreviewed_source_values",
                        "fields": {"no_peraturan": "2", "tahun": "2026", "status": "Disahkan"},
                        "evidence": {
                            "url": "https://official.example/detail",
                            "sha256": "a" * 64,
                        },
                    }
                ),
                "https://official.example/detail",
                "a" * 64,
                AT,
            ),
        )
        conn.execute(
            """INSERT INTO checklist_candidates VALUES(
            'candidate-bkpm','binding','https://jdih-storage.bkpm.go.id/jdih/jdih/law.pdf',
            'BKPM law','bkpm:2','https://jdih.bkpm.go.id/api/document-search?id_theme=1&limit=50&page=1',
            'pending',NULL,1,'',?)""",
            (AT,),
        )
        conn.execute(
            """INSERT INTO checklist_candidate_source_records
            VALUES('candidate-bkpm',?,?,?,?)""",
            (
                json.dumps(
                    {
                        "provider": "bkpm",
                        "interpretation": "unreviewed_source_values",
                        "fields": {"id": 2, "regulation_number": "4"},
                        "evidence": {
                            "url": "https://jdih.bkpm.go.id/api/document-search?id_theme=1&limit=50&page=1",
                            "sha256": "b" * 64,
                        },
                    }
                ),
                "https://jdih.bkpm.go.id/api/document-search?id_theme=1&limit=50&page=1",
                "b" * 64,
                AT,
            ),
        )
        conn.execute(
            """INSERT INTO checklist_candidates VALUES(
            'candidate-rcep','binding','https://rcepsec.org/wp-content/uploads/2026/06/Chapter-3-Annex-3A.pdf',
            'Annex 3A','rcep:annex-3a','https://rcepsec.org/rcep-agreements/',
            'pending',NULL,1,'',?)""",
            (AT,),
        )
        conn.execute(
            """INSERT INTO checklist_candidate_source_records
            VALUES('candidate-rcep',?,?,?,?)""",
            (
                json.dumps(
                    {
                        "provider": "rcepsec",
                        "interpretation": "unreviewed_source_values",
                        "fields": {"link_text": "Annex 3A: Product-Specific Rules"},
                        "structure": {
                            "unit_type": "annex",
                            "number": "3A",
                            "parent_titles": ["Chapter 3: Rules of Origin"],
                            "file_name": "Chapter-3-Annex-3A.pdf",
                            "anchor_class": "rcep-link rcep-sub-link",
                            "link_mechanism": "verified_literal_pdf_call",
                        },
                        "evidence": {
                            "url": "https://rcepsec.org/rcep-agreements/",
                            "sha256": "c" * 64,
                        },
                    }
                ),
                "https://rcepsec.org/rcep-agreements/",
                "c" * 64,
                AT,
            ),
        )
        conn.execute(
            """INSERT INTO checklist_candidates VALUES(
            'candidate-ipos','binding','https://file.go.gov.sg/tm-circular-no-4-of-2026.pdf',
            'Circular No. 4 of 2026',NULL,
            'https://www.ipos.gov.sg/about-ip/trade-marks/circulars-and-practice-directions/',
            'pending',NULL,1,'',?)""",
            (AT,),
        )
        conn.execute(
            """INSERT INTO checklist_candidate_source_records
            VALUES('candidate-ipos',?,?,?,?)""",
            (
                json.dumps(
                    {
                        "provider": "ipos",
                        "interpretation": "unreviewed_source_values",
                        "fields": {
                            "link_text": "Circular No. 4 of 2026 [PDF, 300KB]",
                            "publication_date": "25 September 2026",
                            "row_description": "Nice Classification, Version 2027",
                        },
                        "structure": {
                            "unit_type": "circular",
                            "number": "4 of 2026",
                            "parent_titles": ["Circulars"],
                            "file_name": "tm-circular-no-4-of-2026.pdf",
                            "anchor_class": "",
                            "link_mechanism": "href",
                        },
                        "evidence": {
                            "url": "https://www.ipos.gov.sg/about-ip/trade-marks/circulars-and-practice-directions/",
                            "sha256": "d" * 64,
                        },
                    }
                ),
                "https://www.ipos.gov.sg/about-ip/trade-marks/circulars-and-practice-directions/",
                "d" * 64,
                AT,
            ),
        )
        conn.execute(
            """INSERT INTO checklist_candidates VALUES(
            'candidate-acra','binding','https://isomer-user-content.by.gov.sg/277/practice-direction.pdf',
            'Practice Direction No. 1 of 2026',NULL,
            'https://www.acra.gov.sg/regulations/practice-directions-registrars-interpretations-guidance/practice-directions/',
            'pending',NULL,1,'',?)""",
            (AT,),
        )
        conn.execute(
            """INSERT INTO checklist_candidate_source_records
            VALUES('candidate-acra',?,?,?,?)""",
            (
                json.dumps(
                    {
                        "provider": "acra",
                        "interpretation": "unreviewed_source_values",
                        "fields": {"link_text": "Practice Direction No. 1 of 2026"},
                        "structure": {
                            "unit_type": "practice_direction",
                            "number": "1 of 2026",
                            "parent_titles": ["Practice Directions 2026"],
                            "file_name": "practice-direction.pdf",
                            "anchor_class": "",
                            "link_mechanism": "href",
                        },
                        "evidence": {
                            "url": "https://www.acra.gov.sg/regulations/practice-directions-registrars-interpretations-guidance/practice-directions/",
                            "sha256": "e" * 64,
                        },
                    }
                ),
                "https://www.acra.gov.sg/regulations/practice-directions-registrars-interpretations-guidance/practice-directions/",
                "e" * 64,
                AT,
            ),
        )
        conn.execute(
            """INSERT INTO checklist_candidates VALUES(
            'candidate-wto','binding','https://docs.wto.org/dol2fe/Pages/SS/directdoc2.aspx?Open=True&filename=Q%3A%2FSCHD%2FGATS-SC%2FSC43S1.pdf',
            'GATS/SC/43/Suppl.1',NULL,
            'https://docs.wto.org/dol2fe/Pages/SS/GetXMLResults.aspx?DataSource=Cat&Language=English&Query=%40Symbol%3DGATS%2FSC%2F43%2A&XMLMaxHitsPerPage=100',
            'pending',NULL,1,'',?)""",
            (AT,),
        )
        conn.execute(
            """INSERT INTO checklist_candidate_source_records
            VALUES('candidate-wto',?,?,?,?)""",
            (
                json.dumps(
                    {
                        "provider": "wto",
                        "interpretation": "unreviewed_source_values",
                        "fields": {
                            "catalog_id": "123",
                            "symbol": "GATS/SC/43/Suppl.1",
                            "concerned_countries": "Indonesia",
                            "public_access": "True",
                            "filename_a": "Q:/SCHD/GATS-SC/SC43S1.pdf",
                        },
                        "structure": {
                            "unit_type": "member_schedule",
                            "number": "GATS/SC/43/Suppl.1",
                            "parent_titles": ["GATS/SC/43", "English WTO Docs Online catalog"],
                            "file_name": "SC43S1.pdf",
                            "anchor_class": "",
                            "link_mechanism": "official_xml_catalog",
                        },
                        "evidence": {
                            "url": "https://docs.wto.org/dol2fe/Pages/SS/GetXMLResults.aspx?DataSource=Cat&Language=English&Query=%40Symbol%3DGATS%2FSC%2F43%2A&XMLMaxHitsPerPage=100",
                            "sha256": "f" * 64,
                        },
                    }
                ),
                "https://docs.wto.org/dol2fe/Pages/SS/GetXMLResults.aspx?DataSource=Cat&Language=English&Query=%40Symbol%3DGATS%2FSC%2F43%2A&XMLMaxHitsPerPage=100",
                "f" * 64,
                AT,
            ),
        )
        conn.execute(
            """INSERT INTO checklist_candidates VALUES(
            'candidate-sg-html','binding','https://www.sfa.gov.sg/docs/default-source/food/retail-guideline.pdf',
            'Retail guidelines (PDF)',NULL,'https://www.sfa.gov.sg/food-retail/licence-permit/guidelines-for-licensed-food-retail-premises',
            'pending',NULL,1,'',?)""",
            (AT,),
        )
        conn.execute(
            """INSERT INTO checklist_candidate_source_records
            VALUES('candidate-sg-html',?,?,?,?)""",
            (
                json.dumps(
                    {
                        "provider": "sg_official_html",
                        "interpretation": "unreviewed_source_values",
                        "fields": {
                            "link_text": "Retail guidelines (PDF)",
                            "source_page_title": "Food Retail Guidelines | SFA",
                            "list_context": "Retail guidelines (PDF)",
                        },
                        "structure": {
                            "unit_type": "guideline",
                            "number": None,
                            "parent_titles": ["Food retail", "Licence and permit guidelines"],
                            "file_name": "retail-guideline.pdf",
                            "anchor_class": "",
                            "link_mechanism": "href",
                        },
                        "evidence": {
                            "url": "https://www.sfa.gov.sg/food-retail/licence-permit/guidelines-for-licensed-food-retail-premises",
                            "sha256": "1" * 64,
                        },
                    }
                ),
                "https://www.sfa.gov.sg/food-retail/licence-permit/guidelines-for-licensed-food-retail-premises",
                "1" * 64,
                AT,
            ),
        )
    report = export(archive)
    pages = records(report, filename="discovery-pages.jsonl")
    assert len(pages) == 1 and report["summary"]["discovery_pages"] == 1
    assert pages[0]["plan_id"] == "indonesia-20261003"
    assert pages[0]["plan_version"] == 2 and pages[0]["checklist_item_id"] == "1.3"
    assert pages[0]["purpose"] == "catalog" and pages[0]["collection_item_id"] == "page-item"
    source_records = records(report, filename="candidate-source-records.jsonl")
    assert len(source_records) == 7
    setneg_record = next(row for row in source_records if row["candidate_id"] == "candidate")
    bkpm_record = next(row for row in source_records if row["candidate_id"] == "candidate-bkpm")
    rcep_record = next(row for row in source_records if row["candidate_id"] == "candidate-rcep")
    ipos_record = next(row for row in source_records if row["candidate_id"] == "candidate-ipos")
    acra_record = next(row for row in source_records if row["candidate_id"] == "candidate-acra")
    wto_record = next(row for row in source_records if row["candidate_id"] == "candidate-wto")
    sg_record = next(row for row in source_records if row["candidate_id"] == "candidate-sg-html")
    assert setneg_record["source_record"]["fields"]["no_peraturan"] == "2"
    assert setneg_record["source_page_sha256"] == "a" * 64
    assert bkpm_record["source_record"]["provider"] == "bkpm"
    assert bkpm_record["anomalies"] == []
    assert rcep_record["source_record"]["structure"]["parent_titles"] == ["Chapter 3: Rules of Origin"]
    assert rcep_record["anomalies"] == []
    assert ipos_record["source_record"]["fields"]["publication_date"] == "25 September 2026"
    assert ipos_record["source_record"]["structure"]["unit_type"] == "circular"
    assert ipos_record["anomalies"] == []
    assert acra_record["source_record"]["structure"]["unit_type"] == "practice_direction"
    assert acra_record["anomalies"] == []
    assert wto_record["source_record"]["fields"]["symbol"] == "GATS/SC/43/Suppl.1"
    assert wto_record["source_record"]["structure"]["unit_type"] == "member_schedule"
    assert wto_record["anomalies"] == []
    assert sg_record["source_record"]["provider"] == "sg_official_html"
    assert sg_record["anomalies"] == []
    assert report["summary"]["candidate_source_records"] == 7
    docs = records(report)
    assert len(docs) == 1 and docs[0]["review"]["review_status"] == "draft"
    assert docs[0]["checklist_refs"][0]["source_id"] == "source"
    assert docs[0]["checklist_refs"][0]["plan_version"] == 2
    assert docs[0]["parsing"]["element_count"] == docs[0]["parsing"]["chunk_count"] == 1
    text = "".join(path.read_text() for path in report["path"].rglob("*") if path.is_file())
    for secret in (
        "PRIVATE_BODY",
        "PRIVATE_CHUNK",
        "CATALOG_PRIVATE_BODY",
        "0.123456789",
        "DO_NOT_EXPORT_KEY",
    ):
        assert secret not in text


def test_ocr_images_are_cataloged_by_source_version_and_physical_page_without_transcripts(archive):
    original = document(archive, "scan-v1")
    image = archive.archive.blobs.put_bytes("owner-a", b"rendered page bytes")
    with archive.db.transaction() as conn:
        conn.execute(
            "UPDATE documents SET manifest=? WHERE id='scan-v1'",
            (
                json.dumps(
                    {
                        "elements": [],
                        "chunks": [],
                        "quality": {
                            "status": "partial",
                            "complete": False,
                            "page_count": 2,
                            "pending_pages": [],
                            "failed_pages": [2],
                            "review_pages": [],
                        },
                        "pages": [
                            {
                                "page": 1,
                                "source_hash": original.digest,
                                "printed_page": "iii",
                                "status": "ocr_complete",
                                "text": "PRIVATE_TRANSCRIPT",
                                "ocr": {
                                    "model": "test-vision",
                                    "prompt_version": "v1",
                                    "image_sha256": image.digest,
                                    "image_blob": {
                                        "storage_path": str(image.path.relative_to(archive.db.data_dir)),
                                        "size": image.size,
                                        "content_hash": image.digest,
                                    },
                                },
                            }
                        ],
                    }
                ),
            ),
        )
    report = export(archive)
    assert report["summary"]["ocr_pages"] == 1
    row = records(report, filename="ocr-pages.jsonl")[0]
    assert row["document_id"] == "scan-v1" and row["document_version"] == 1
    assert row["source_hash"] == original.digest and row["image_hash"] == image.digest
    assert row["page"] == 1 and row["printed_page"] == "iii"
    assert row["original"]["exists"] is True
    quality = records(report)[0]["parsing"]
    assert quality["complete"] is False and quality["failed_pages"] == 1
    exported = "".join(path.read_text() for path in report["path"].rglob("*.jsonl"))
    assert "PRIVATE_TRANSCRIPT" not in exported and "rendered page bytes" not in exported


def test_official_api_request_evidence_retains_pagination_without_arbitrary_secrets(archive):
    page(archive)
    with archive.db.transaction() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS checklist_page_evidence(
            item_id TEXT PRIMARY KEY REFERENCES checklist_pages(item_id), evidence TEXT NOT NULL)""")
        conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(11,?)", (AT,))
        conn.execute(
            "INSERT INTO checklist_page_evidence VALUES('page-item',?)",
            (
                json.dumps(
                    {
                        "request": {
                            "method": "POST",
                            "url": "https://jdih.setneg.go.id/api/hukumproduk/produkhukum",
                            "json_body": {"jns": ["UU"], "length": 5, "start": 900, "api_key": "PRIVATE_KEY"},
                            "headers": {"Authorization": "PRIVATE_TOKEN"},
                        },
                        "scope": {
                            "provider": "setneg",
                            "jns": "UU",
                            "total": 901,
                            "offset": 900,
                            "page_size": 5,
                            "returned": 1,
                            "raw": "PRIVATE_BODY",
                        },
                    }
                ),
            ),
        )
    report = export(archive)
    row = records(report, filename="discovery-pages.jsonl")[0]
    assert row["evidence"]["request"]["json_body"] == {"jns": ["UU"], "length": 5, "start": 900}
    assert row["evidence"]["scope"]["total"] == 901
    assert row["evidence"]["scope"]["returned"] == 1
    assert "PRIVATE_" not in json.dumps(row)


@pytest.mark.parametrize(
    "url",
    [
        "https://PRIVATE_PASSWORD@official.example/list",
        "https://official.example/list?api_key=PRIVATE_KEY",
        "https://official.example/list\x00PRIVATE_CONTROL",
        "javascript:PRIVATE_INSTRUCTION",
    ],
)
def test_generic_evidence_does_not_export_request_credentials_or_controls(url):
    from app.contexts.collection.infrastructure.archive_inventory import _page_evidence

    anomalies = []
    evidence = _page_evidence(json.dumps({"request": {"method": "GET", "url": url}, "scope": {}}), anomalies)
    assert evidence["request"] == {"method": "GET"}
    assert anomalies == ["unsafe_discovery_request_url"]
    assert "PRIVATE_" not in json.dumps(evidence)


def treaty_page(archive, *, country="ID", owner="owner-a", mutate=None):
    from app.contexts.collection.infrastructure.discovery.discovery import discover_page, recipe_for
    from app.contexts.collection.infrastructure.discovery.treaty_room_discovery import catalog_url

    page(archive, owner=owner, country=country)
    url = catalog_url()
    data = {
        "success": True,
        "total": 40,
        "current_page": 1,
        "last_page": 1,
        "length": 1,
        "data": [
            {
                "id_perjanjian": identifier,
                "judul": f"Agreement on trade and taxation {identifier}",
                "mulaiBerlaku": "Published article " + "x" * 900,
                "masaBerlaku": "Published paragraph " + "y" * 800,
                "berlakuSetelah": "Article 14",
                "catatan": "Unknown legal force",
                "file": {
                    "id_konten": identifier + 100,
                    "teks_perjanjian": "-",
                    "file": f"1-TREATY.{identifier}",
                    "file2": f"2-TREATY.{identifier}",
                    "file3": None,
                    "file4": None,
                },
            }
            for identifier in range(1, 41)
        ],
    }
    body = json.dumps(data).encode()
    _, definition = archive.checklists.definition("indonesia-20261003", "5.1")
    result = discover_page(url, body, "application/json", recipe_for(definition))
    assert result["matched"] is True
    evidence = {"request": {"method": "GET", "url": url}, "scope": result["scope"]}
    if mutate:
        mutate(evidence)
    blob = archive.archive.blobs.put_bytes(owner, body)
    with archive.db.transaction() as conn:
        conn.execute("UPDATE sources SET url=? WHERE id='source'", (url,))
        conn.execute("UPDATE checklist_bindings SET version=5,item_id='5.1' WHERE id='binding'")
        conn.execute(
            "UPDATE collection_items SET url=?,host='api-treaty-room.kemlu.go.id',status='discovered' WHERE id='page-item'",
            (url,),
        )
        conn.execute(
            "UPDATE checklist_pages SET url=?,final_url=?,content_hash=?,storage_path=?,size=? WHERE item_id='page-item'",
            (url, url, blob.digest, str(blob.path), blob.size),
        )
        conn.execute("INSERT INTO checklist_page_evidence VALUES('page-item',?)", (json.dumps(evidence),))
    return blob, evidence


def test_large_treaty_page_exports_all_bounded_records_and_scalar_provenance_without_rag(archive):
    blob, evidence = treaty_page(archive)
    assert len(json.dumps(evidence).encode()) > 16384
    report = export(archive, owner="owner-a", country="ID")
    page_record = records(report, filename="discovery-pages.jsonl")[0]
    scope = page_record["evidence"]["scope"]
    assert {key: scope[key] for key in ("total", "page", "last_page", "page_size", "returned")} == {
        "total": 40,
        "page": 1,
        "last_page": 1,
        "page_size": 40,
        "returned": 40,
    }
    assert scope["filters"] == evidence["scope"]["filters"]
    assert scope["legal_status_inferred"] is scope["exhaustive_content_verified"] is False
    rows = records(report, filename="catalog-documents.jsonl")
    assert len(rows) == 40
    for index, row in enumerate(rows):
        original = evidence["scope"]["documents"][index]
        assert row["source_metadata"] == original["source_metadata"]
        assert row["originals"] == original["originals"]
        assert row["missing_verified_fields"] == original["missing_verified_fields"]
        assert row["priority_topics"] == original["priority_topics"]
        assert (
            row["source_record_sha256"]
            == hashlib.sha256(
                json.dumps(original, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        )
        assert row["source_hash"] == blob.digest
        assert row["source_page_id"] == "page-item" and row["source_row_index"] == index
        assert row["plan_id"] == "indonesia-20261003" and row["plan_version"] == 5
        assert row["owner_sha256"] == hashlib.sha256(b"owner-a").hexdigest()
        assert row["source_url"] == page_record["final_url"]
        assert row["source_original"] == page_record["original"]
        assert row["retrieval_eligible"] is False and row["export_status"] == "exported"
    assert report["summary"]["format_version"] == 8
    assert report["summary"]["catalog_documents"] == 40
    assert report["summary"]["catalog_document_gaps"] == report["summary"]["anomalies"] == 0
    assert report["summary"]["countries"]["ID"]["catalog_documents"] == 40
    with archive.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM retrieval_chunks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM retrieval_vectors").fetchone()[0] == 0
    for path in report["path"].rglob("*"):
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)


def test_treaty_projection_drops_arbitrary_headers_metadata_and_file_fields(archive):
    def mutate(value):
        value["request"]["headers"] = {"Authorization": "PRIVATE_TOKEN"}
        value["scope"]["secret"] = "PRIVATE_SCOPE"
        document = value["scope"]["documents"][0]
        document["secret"] = "PRIVATE_RECORD"
        document["source_metadata"]["api_key"] = "PRIVATE_KEY"
        document["source_metadata"]["file"]["password"] = "PRIVATE_PASSWORD"

    treaty_page(archive, mutate=mutate)
    report = export(archive)
    row = records(report, filename="catalog-documents.jsonl")[0]
    assert "treaty_metadata_fields_omitted" in row["anomalies"]
    assert row["export_status"] == "exported"
    assert "PRIVATE_" not in "".join(path.read_text() for path in report["path"].rglob("*.jsonl"))


@pytest.mark.parametrize(
    "failure", ["metadata", "oversize", "control", "title", "file", "original", "duplicate"]
)
def test_invalid_treaty_record_has_explicit_gap_and_raw_source_pointer(archive, failure):
    def mutate(value):
        documents = value["scope"]["documents"]
        record = documents[7]
        if failure == "metadata":
            record["source_metadata"] = ["PRIVATE_BAD_METADATA"]
        elif failure == "oversize":
            record["source_metadata"]["mulaiBerlaku"] = "PRIVATE_OVERSIZE" * 500
        elif failure == "control":
            record["source_metadata"]["catatan"] = "PRIVATE_CONTROL\x00"
        elif failure == "title":
            record["title"] = "PRIVATE_MISMATCHED_TITLE"
        elif failure == "file":
            record["source_metadata"]["file"]["file2"] = {"secret": "PRIVATE_FILE"}
        elif failure == "original":
            record["originals"] = []
        else:
            documents[7] = documents[0]

    blob, _ = treaty_page(archive, mutate=mutate)
    report = export(archive)
    rows = records(report, filename="catalog-documents.jsonl")
    assert len(rows) == 40
    gap = next(row for row in rows if row["export_status"] == "invalid_record")
    assert gap["source_row_index"] == 7 and gap["source_hash"] == blob.digest
    assert gap["source_original"]["exists"] is True
    assert "invalid_treaty_catalog_record" in gap["anomalies"]
    assert "source_metadata" not in gap and gap["retrieval_eligible"] is False
    assert report["summary"]["catalog_document_gaps"] == 1
    assert report["summary"]["catalog_documents"] == 39
    assert records(report, filename="discovery-pages.jsonl")[0]["evidence"]["scope"]["returned"] == 40
    assert "PRIVATE_" not in "".join(path.read_text() for path in report["path"].rglob("*.jsonl"))


@pytest.mark.parametrize("failure", ["count", "pagination", "filters", "oversize"])
def test_invalid_treaty_page_exports_one_gap_instead_of_quiet_partial_records(archive, failure):
    def mutate(value):
        if failure == "count":
            value["scope"]["documents"].append(value["scope"]["documents"][0])
        elif failure == "pagination":
            value["scope"]["last_page"] = 100
        elif failure == "filters":
            value["scope"]["filters"]["Authorization"] = "PRIVATE_FILTER"
        else:
            value["secret"] = "PRIVATE_OVERSIZE" * (4 * 1024 * 1024 // 10)

    blob, _ = treaty_page(archive, mutate=mutate)
    report = export(archive)
    rows = records(report, filename="catalog-documents.jsonl")
    assert len(rows) == 1 and rows[0]["export_status"] == "invalid_page"
    assert rows[0]["source_hash"] == blob.digest and rows[0]["source_original"]["exists"] is True
    assert report["summary"]["catalog_documents"] == 0
    assert report["summary"]["catalog_document_gaps"] == 1
    assert "invalid_treaty_catalog_page" in records(report, filename="discovery-pages.jsonl")[0]["anomalies"]
    assert "PRIVATE_" not in "".join(path.read_text() for path in report["path"].rglob("*.jsonl"))


@pytest.mark.parametrize(
    "failure", ["country", "account", "source", "source_account", "url", "source_hash", "provider"]
)
def test_adapter_name_cannot_expand_exports_across_provenance_boundaries(archive, failure):
    country = "SG" if failure == "country" else "ID"

    def mutate(value):
        if failure == "provider":
            value["scope"]["provider"] = "PRIVATE_FAKE_PROVIDER"
        value["scope"]["documents"][0]["source_metadata"]["api_key"] = "PRIVATE_KEY"

    treaty_page(archive, country=country, mutate=mutate)
    with archive.db.transaction() as conn:
        if failure == "account":
            conn.execute("UPDATE collection_runs SET owner_id='owner-b' WHERE id='run'")
        elif failure == "source_account":
            conn.execute("UPDATE sources SET owner_id='owner-b' WHERE id='source'")
        elif failure == "source":
            conn.execute(
                """INSERT INTO sources(id,owner_id,name,url,enabled,interval_hours,tags,status,created_at,updated_at)
                VALUES('other-source','owner-a','Other source','https://official.example/list',0,24,'[]','idle',?,?)""",
                (AT, AT),
            )
            conn.execute("UPDATE collection_runs SET source_id='other-source' WHERE id='run'")
        elif failure == "url":
            conn.execute(
                "UPDATE checklist_pages SET final_url='https://api-treaty-room.kemlu.go.id/login' WHERE item_id='page-item'"
            )
        elif failure == "source_hash":
            conn.execute("UPDATE checklist_pages SET content_hash=? WHERE item_id='page-item'", ("a" * 64,))
    report = export(archive)
    assert report["summary"]["catalog_documents"] == 0
    assert report["summary"]["anomalies"] > 0
    assert records(report, country=country, filename="discovery-pages.jsonl")[0]["evidence"] == {}
    assert "PRIVATE_" not in "".join(path.read_text() for path in report["path"].rglob("*.jsonl"))


def test_missing_and_unsafe_originals_are_counted_without_accessing_outside_data(archive, tmp_path):
    original = document(archive, "missing", body=b"missing original")
    original.path.unlink()
    document(archive, "outside", path=tmp_path.parent / "outside-secret.pdf")
    external = tmp_path.parent / "outside-directory"
    external.mkdir()
    (tmp_path / "linked-directory").symlink_to(external, target_is_directory=True)
    document(archive, "symlink", path=tmp_path / "linked-directory" / "secret.pdf")
    report = export(archive)
    rows = {row["document_id"]: row for row in records(report)}
    assert rows["missing"]["original"]["exists"] is False
    assert rows["missing"]["original"]["relative_path"].startswith("files/blobs/")
    assert "missing_file" in rows["missing"]["anomalies"]
    assert rows["outside"]["original"]["relative_path"] is None
    assert "outside_data_dir" in rows["outside"]["anomalies"]
    assert "unsafe_symlink" in rows["symlink"]["anomalies"]
    assert report["summary"]["anomalies"] == 3
    assert report["summary"]["countries"]["ID"]["anomalies"] == 3


def test_prior_versions_keep_frozen_run_references_after_candidate_moves(archive):
    document(archive, "law-v1", family="law", version=1)
    document(archive, "law-v2", family="law", version=2, body=b"new version")
    page(archive)
    frozen_plan = json.dumps(
        {
            "checklist": {
                "binding_id": "binding",
                "kind": "collection",
                "country": "ID",
                "definition": {"plan": {"id": "indonesia-20261003", "version": 2}, "item": {"id": "1.3"}},
            }
        }
    )
    with archive.db.transaction() as conn:
        conn.execute(
            "INSERT INTO checklist_candidates VALUES('candidate','binding','https://official.example/law.pdf','Law',NULL,'https://official.example/list','stored','law-v1',2,'',?)",
            (AT,),
        )
        for version in (1, 2):
            conn.execute(
                """INSERT INTO collection_runs VALUES(?, 'owner-a','source',?,'hash',?,'completed',1,NULL,?,?)""",
                (f"collect-{version}", f"request-{version}", frozen_plan, AT, AT),
            )
            conn.execute(
                """INSERT INTO collection_items(id,run_id,owner_id,url,host,level,status,created_at,
                purpose,candidate_id,document_id) VALUES(?,?,'owner-a','https://official.example/law.pdf',
                'official.example','primary','stored',?,'document','candidate',?)""",
                (f"collect-item-{version}", f"collect-{version}", AT, f"law-v{version}"),
            )
        conn.execute("UPDATE checklist_candidates SET document_id='law-v2',revision=3 WHERE id='candidate'")
    rows = {row["document_id"]: row for row in records(export(archive))}
    for version in (1, 2):
        refs = rows[f"law-v{version}"]["checklist_refs"]
        assert len(refs) == 1
        assert refs[0]["run_id"] == f"collect-{version}"
        assert refs[0]["collection_item_id"] == f"collect-item-{version}"
        assert refs[0]["plan_id"] == "indonesia-20261003" and refs[0]["plan_version"] == 2
        assert refs[0]["checklist_item_id"] == "1.3" and refs[0]["source_id"] == "source"


def test_export_keeps_database_and_originals_unchanged_and_report_private(archive, monkeypatch):
    blob = document(archive, "law")
    database_before = archive.db.path.read_bytes()
    original_before = (blob.path.read_bytes(), blob.path.stat().st_mtime_ns)

    def forbidden(*args, **kwargs):
        pytest.fail("the offline exporter must not initialize or migrate the service")

    monkeypatch.setattr(Service, "__init__", forbidden)
    report = export(archive)
    assert archive.db.path.read_bytes() == database_before
    assert (blob.path.read_bytes(), blob.path.stat().st_mtime_ns) == original_before
    for path in [report["path"], *report["path"].rglob("*")]:
        assert path.stat().st_mode & 0o777 == (0o700 if path.is_dir() else 0o600)
    assert not list((archive.db.data_dir / "catalogs").glob(".inventory-*"))


def test_snapshot_excludes_concurrent_writes_to_documents_and_classifications(archive, monkeypatch):
    from app.contexts.collection.infrastructure import archive_inventory as archive_inventory

    monkeypatch.setattr(archive_inventory, "BATCH_SIZE", 1)
    document(archive, "first")
    document(archive, "second")
    original_write = archive_inventory._write_document
    updated = False

    def write(*args, **kwargs):
        nonlocal updated
        if not updated:
            updated = True
            document(archive, "late")
            with archive.db.transaction() as conn:
                conn.execute(
                    "UPDATE legal_classifications SET tags='[\"late tag\"]' WHERE document_id='second'"
                )
        return original_write(*args, **kwargs)

    monkeypatch.setattr(archive_inventory, "_write_document", write)
    rows = records(export(archive))
    assert [row["document_id"] for row in rows] == ["first", "second"]
    assert all(row["classifications"][0]["tags"] == ["source:official"] for row in rows)


def test_failure_removes_only_its_own_temporary_snapshot(archive, monkeypatch):
    from app.contexts.collection.infrastructure import archive_inventory as archive_inventory

    document(archive, "law")
    previous = export(archive)["path"]
    unrelated = archive.db.data_dir / "catalogs" / ".inventory-unrelated"
    unrelated.mkdir()
    sentinel = unrelated / "user-file"
    sentinel.write_text("keep")

    def failed(*args, **kwargs):
        raise OSError("simulated disk error")

    monkeypatch.setattr(archive_inventory, "_write_document", failed)
    with pytest.raises(archive_inventory.InventoryError):
        export(archive)
    assert previous.is_dir() and sentinel.read_text() == "keep"
    assert sorted(p.name for p in unrelated.parent.iterdir()) == sorted([previous.name, unrelated.name])


@pytest.mark.parametrize("version", [7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19])
def test_supported_schema_versions_are_reported_without_upgrading(archive, version):
    document(archive, "law")
    with archive.db.transaction() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version>?", (version,))
        if version < 12:
            conn.execute("DROP TABLE collection_transfer_parts")
            conn.execute("DROP TABLE collection_transfers")
            conn.execute("DELETE FROM schema_migrations WHERE version=12")
        if version < 11:
            conn.execute("DROP TABLE IF EXISTS checklist_page_evidence")
            conn.execute("DELETE FROM schema_migrations WHERE version=11")
        else:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS checklist_page_evidence(item_id TEXT PRIMARY KEY,evidence TEXT NOT NULL)"
            )
            conn.execute("INSERT OR IGNORE INTO schema_migrations VALUES(11,?)", (AT,))
    if version < 10:
        with archive.db.transaction() as conn:
            for trigger in ("retrieval_document_insert", "retrieval_document_update"):
                conn.execute("DROP TRIGGER " + trigger)
            for table in (
                "retrieval_fts",
                "retrieval_vectors",
                "retrieval_models",
                "retrieval_chunks",
                "retrieval_dirty",
                "retrieval_documents",
            ):
                conn.execute("DROP TABLE " + table)
            conn.execute("DELETE FROM schema_migrations WHERE version=10")
    if version < 9:
        with archive.db.transaction() as conn:
            for table in (
                "legal_requests",
                "legal_analysis_versions",
                "legal_analyses",
                "legal_fact_versions",
                "legal_facts",
            ):
                conn.execute("DROP TABLE " + table)
            conn.execute("DELETE FROM schema_migrations WHERE version=9")
    if version == 7:
        with archive.db.transaction() as conn:
            conn.execute("DROP TABLE debug_runs")
            conn.execute("DROP TABLE debug_samples")
            conn.execute("DELETE FROM schema_migrations WHERE version=8")
    before = archive.db.path.read_bytes()
    report = export(archive)
    assert report["summary"]["schema_version"] == version
    assert records(report)[0]["document_id"] == "law"
    retrieval = records(report)[0]["retrieval"]
    assert retrieval["status"] == ("available" if version >= 10 else "unknown_schema")
    if version < 10:
        assert all(value is None for key, value in retrieval.items() if key != "status")
    else:
        assert retrieval["index_present"] is False
        assert retrieval["indexed_chunk_count"] == retrieval["matching_vector_count"] == 0
        assert retrieval["source_hash_matches"] is None and retrieval["complete"] is None
    assert archive.db.path.read_bytes() == before


@pytest.mark.parametrize("version", [6, 16])
def test_missing_or_unsupported_database_is_not_created_or_upgraded(tmp_path, version):
    from app.contexts.collection.infrastructure.archive_inventory import InventoryError, export_inventory

    with pytest.raises(InventoryError, match="tea.sqlite3"):
        export_inventory(tmp_path)
    assert not list(tmp_path.iterdir())
    with sqlite3.connect(tmp_path / "tea.sqlite3") as conn:
        conn.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY,applied_at TEXT)")
        conn.execute("INSERT INTO schema_migrations VALUES(?,'unsupported')", (version,))
    before = (tmp_path / "tea.sqlite3").read_bytes()
    with pytest.raises(InventoryError, match="schema 7"):
        export_inventory(tmp_path)
    assert (tmp_path / "tea.sqlite3").read_bytes() == before
    assert not (tmp_path / "catalogs").exists()


def test_cli_environment_defaults_and_explicit_filters_only_print_summary(archive, monkeypatch, capsys):
    from app.contexts.collection.infrastructure.archive_inventory import main

    document(archive, "law")
    document(archive, "sg-law", country="SG")
    monkeypatch.setenv("TEA_DATA_DIR", str(archive.db.data_dir))
    monkeypatch.setenv("TEA_INVENTORY_COUNTRY", "SG")
    monkeypatch.setenv("TEA_INVENTORY_OWNER", "owner-b")
    assert main(["--country", "ID", "--owner", "owner-a"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["documents"] == 1 and output["discovery_pages"] == 0
    assert Path(output["path"]).is_dir()
    assert "law" not in json.dumps(output) and "owner-a" not in json.dumps(output)
    monkeypatch.setenv("TEA_INVENTORY_COUNTRY", "")
    monkeypatch.setenv("TEA_INVENTORY_OWNER", "")
    assert main([]) == 0
    assert json.loads(capsys.readouterr().out)["documents"] == 2


def test_reports_directory_cannot_redirect_output_outside_data(archive, tmp_path):
    from app.contexts.collection.infrastructure.archive_inventory import InventoryError

    outside = tmp_path.parent / "not-a-catalog-root"
    outside.mkdir()
    (tmp_path / "catalogs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InventoryError):
        export(archive)
    assert not list(outside.iterdir())
