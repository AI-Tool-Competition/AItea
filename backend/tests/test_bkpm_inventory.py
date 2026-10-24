"""A real oversized BKPM page gets a narrow, source-bound inventory projection."""

import json

import pytest
from test_archive_inventory import AT, export, page, records
from test_archive_inventory import archive as archive


def bkpm_page(archive, *, mutate=None):
    from app.contexts.collection.infrastructure.discovery.discovery import discover_page, recipe_for

    page(archive)
    url = "https://jdih.bkpm.go.id/api/document-search?id_theme=1&limit=50&page=1"
    plan_definition, item = archive.checklists.definition("indonesia-20261003", "4.1")
    definition = {"plan": {key: plan_definition[key] for key in ("id", "version", "country")}, "item": item}
    recipe = recipe_for(item)
    rows = [
        {
            "id": index + 1,
            "id_theme": 1,
            "title": "Investment regulation " + "x" * 650,
            "document_type": "Peraturan" if index < 30 else "Unverified",
            "document_file_url": f"https://jdih-storage.bkpm.go.id/jdih/jdih/regulation-{index + 1}.pdf",
            "slug": f"regulation-{index + 1}",
            "regulation_number": str(index + 1),
            "regulation_type": "Keputusan",
            "establishment_date": "2026-07-24",
            "publication_date": "2026-09-01",
            "status": "Berlaku",
        }
        for index in range(50)
    ]
    body = json.dumps(
        {"allDocuments": rows, "v_peraturan_prior_aggregate": {"aggregate": {"count": 88}}}
    ).encode()
    result = discover_page(url, body, "application/json", recipe)
    assert result["matched"] and len(result["scope"]["documents"]) == 30
    evidence = {"request": {"method": "GET", "url": url}, "scope": result["scope"]}
    if mutate:
        mutate(evidence)
    blob = archive.archive.blobs.put_bytes("owner-a", body)
    plan = {
        "checklist": {
            "binding_id": "binding",
            "kind": "discovery",
            "country": "ID",
            "definition": definition,
            "recipe": recipe,
        }
    }
    with archive.db.transaction() as conn:
        conn.execute("UPDATE sources SET url=? WHERE id='source'", (url,))
        conn.execute(
            "UPDATE checklist_bindings SET version=?,item_id='4.1' WHERE id='binding'",
            (definition["plan"]["version"],),
        )
        conn.execute("UPDATE collection_runs SET plan=? WHERE id='run'", (json.dumps(plan),))
        conn.execute(
            "UPDATE collection_items SET url=?,host='jdih.bkpm.go.id',status='discovered' WHERE id='page-item'",
            (url,),
        )
        conn.execute(
            "UPDATE checklist_pages SET url=?,final_url=?,content_hash=?,storage_path=?,size=?,warnings=? WHERE item_id='page-item'",
            (url, url, blob.digest, str(blob.path), blob.size, json.dumps(result["warnings"])),
        )
        conn.execute("INSERT INTO checklist_page_evidence VALUES('page-item',?)", (json.dumps(evidence),))
    return blob, evidence


def test_bkpm_exports_exact_saved_records_without_inventing_missing_rows_or_legal_approval(archive):
    blob, evidence = bkpm_page(archive)
    assert 16384 < len(json.dumps(evidence).encode()) < 64 * 1024
    report = export(archive, owner="owner-a", country="ID")
    rows = records(report, filename="catalog-documents.jsonl")
    assert len(rows) == 30 and report["summary"]["catalog_documents"] == 30
    assert report["summary"]["catalog_document_gaps"] == report["summary"]["anomalies"] == 0
    expected = {value["identity_key"]: value for value in evidence["scope"]["documents"]}
    for row in rows:
        assert row["identity_key"] in expected
        assert row["title"] == expected[row["identity_key"]]["title"]
        assert row["original_url"] == expected[row["identity_key"]]["original_url"]
        assert row["source_hash"] == blob.digest and row["source_page_id"] == "page-item"
        assert row["source_original"]["exists"] and row["source_original"]["actual_size"] == blob.size
        assert row["mapping_record_indexed"] is False and row["legal_status_inferred"] is False
        assert row["legal_review_status"] == "not_reviewed" and "legal_level" not in row
    exported = records(report, filename="discovery-pages.jsonl")[0]
    assert exported["warning_count"] == 20
    assert exported["evidence"]["scope"]["returned"] == 50
    assert exported["evidence"]["scope"]["stored_document_count"] == 30
    assert exported["evidence"]["scope"]["returned_rows_without_document_metadata"] == 20
    with archive.db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM retrieval_chunks").fetchone()[0] == 0


def test_bkpm_allowlist_excludes_arbitrary_request_and_record_fields(archive):
    def mutate(value):
        value["request"]["headers"] = {"Authorization": "PRIVATE_TOKEN"}
        value["scope"]["api_key"] = "PRIVATE_SCOPE"
        value["scope"]["documents"][0]["secret"] = "PRIVATE_RECORD"

    bkpm_page(archive, mutate=mutate)
    report = export(archive)
    assert len(records(report, filename="catalog-documents.jsonl")) == 30
    assert "PRIVATE_" not in "".join(path.read_text() for path in report["path"].rglob("*.jsonl"))


@pytest.mark.parametrize(
    "failure",
    [
        "country",
        "owner",
        "source_owner",
        "source",
        "binding",
        "recipe",
        "plan",
        "version",
        "item",
        "hash",
        "theme",
        "limit",
        "page",
    ],
)
def test_large_bkpm_evidence_requires_exact_owned_frozen_context(archive, failure):
    bkpm_page(archive)
    with archive.db.transaction() as conn:
        if failure == "country":
            conn.execute("UPDATE checklist_bindings SET country='SG' WHERE id='binding'")
        elif failure == "owner":
            conn.execute("UPDATE collection_runs SET owner_id='owner-b' WHERE id='run'")
        elif failure == "source_owner":
            conn.execute("UPDATE sources SET owner_id='owner-b' WHERE id='source'")
        elif failure == "source":
            conn.execute(
                """INSERT INTO sources(id,owner_id,name,url,enabled,interval_hours,tags,status,created_at,updated_at)
                VALUES('other-source','owner-a','Other source','https://official.example/list',0,24,'[]','idle',?,?)""",
                (AT, AT),
            )
            conn.execute("UPDATE collection_runs SET source_id='other-source' WHERE id='run'")
        elif failure == "hash":
            conn.execute("UPDATE checklist_pages SET content_hash=? WHERE item_id='page-item'", ("0" * 64,))
        elif failure in {"theme", "limit", "page"}:
            old = "id_theme=1&limit=50&page=1"
            new = {
                "theme": "id_theme=2&limit=50&page=1",
                "limit": "id_theme=1&limit=40&page=1",
                "page": "id_theme=1&limit=50&page=01",
            }[failure]
            conn.execute(
                "UPDATE checklist_pages SET final_url=replace(final_url,?,?) WHERE item_id='page-item'",
                (old, new),
            )
        else:
            plan = json.loads(conn.execute("SELECT plan FROM collection_runs WHERE id='run'").fetchone()[0])
            checklist = plan["checklist"]
            if failure == "binding":
                checklist["binding_id"] = "wrong-binding"
            elif failure == "recipe":
                checklist["recipe"]["bkpm_investment"] = 1
            elif failure == "plan":
                checklist["definition"]["plan"]["id"] = "other-plan"
            elif failure == "version":
                checklist["definition"]["plan"]["version"] -= 1
            else:
                checklist["definition"]["item"]["id"] = "4.2"
            conn.execute("UPDATE collection_runs SET plan=? WHERE id='run'", (json.dumps(plan),))
    report = export(archive)
    namespace = "SG" if failure == "country" else "ID"
    row = records(report, country=namespace, filename="discovery-pages.jsonl")[0]
    assert "invalid_discovery_evidence" in row["anomalies"]
    assert report["summary"]["catalog_documents"] == 0
    assert report["summary"]["catalog_document_gaps"] == 1


@pytest.mark.parametrize(
    "failure", ["scope", "count", "duplicate", "original", "metadata", "method", "request", "oversize"]
)
def test_invalid_bkpm_data_retains_explicit_parent_source_gap(archive, failure):
    def mutate(value):
        documents = value["scope"]["documents"]
        if failure == "scope":
            value["scope"]["offset"] = 50
        elif failure == "count":
            value["scope"]["returned"] = 30
        elif failure == "duplicate":
            documents[1] = documents[0]
        elif failure == "original":
            documents[0]["original_url"] = (
                "https://jdih-storage.bkpm.go.id/jdih/jdih/doc.pdf?token=PRIVATE_KEY"
            )
        elif failure == "metadata":
            documents[0]["status"] = {"api_key": "PRIVATE_KEY"}
        elif failure == "method":
            value["request"]["method"] = "POST"
        elif failure == "request":
            value["request"]["url"] += "&token=PRIVATE_TOKEN"
        else:
            value["secret"] = "PRIVATE_" * 10000

    blob, _ = bkpm_page(archive, mutate=mutate)
    report = export(archive)
    rows = records(report, filename="catalog-documents.jsonl")
    gaps = [row for row in rows if row["export_status"] != "exported"]
    assert gaps and all(row["source_hash"] == blob.digest for row in gaps)
    assert all(row["mapping_record_indexed"] is False for row in gaps)
    assert report["summary"]["catalog_document_gaps"] == 1
    assert "PRIVATE_" not in "".join(path.read_text() for path in report["path"].rglob("*.jsonl"))


def test_unknown_provider_keeps_generic_16k_limit(archive):
    bkpm_page(archive, mutate=lambda value: value["scope"].update(provider="unknown"))
    report = export(archive)
    row = records(report, filename="discovery-pages.jsonl")[0]
    assert "invalid_discovery_evidence" in row["anomalies"] and row["evidence"] == {}
    assert report["summary"]["catalog_documents"] == 0


def test_bkpm_original_symlink_stays_an_explicit_source_gap(archive):
    blob, _ = bkpm_page(archive)
    target = archive.db.data_dir / "private-target"
    target.write_bytes(b"PRIVATE_FILE_MUST_NOT_BE_READ")
    blob.path.unlink()
    blob.path.symlink_to(target)
    report = export(archive)
    row = records(report, filename="discovery-pages.jsonl")[0]
    assert "unsafe_symlink" in row["anomalies"]
    assert report["summary"]["catalog_documents"] == 0
    assert report["summary"]["catalog_document_gaps"] == 1
    assert "PRIVATE_FILE" not in json.dumps(records(report, filename="catalog-documents.jsonl"))
