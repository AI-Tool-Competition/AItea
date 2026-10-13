"""Offline catalogs describe immutable originals without moving or reading them."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.service import Service

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
    from app.archive_inventory import export_inventory

    return export_inventory(archive.db.data_dir, **kwargs)


def records(report, owner="owner-a", country="ID", filename="documents.jsonl"):
    namespace = hashlib.sha256(owner.encode()).hexdigest()
    path = report["path"] / namespace / country / filename
    return [json.loads(line) for line in path.read_text().splitlines()]


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
    report = export(archive)
    pages = records(report, filename="discovery-pages.jsonl")
    assert len(pages) == 1 and report["summary"]["discovery_pages"] == 1
    assert pages[0]["plan_id"] == "indonesia-20261003"
    assert pages[0]["plan_version"] == 2 and pages[0]["checklist_item_id"] == "1.3"
    assert pages[0]["purpose"] == "catalog" and pages[0]["collection_item_id"] == "page-item"
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
    from app import archive_inventory

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
    from app import archive_inventory

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


@pytest.mark.parametrize("version", [7, 8, 9])
def test_supported_schema_versions_are_reported_without_upgrading(archive, version):
    document(archive, "law")
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
    assert archive.db.path.read_bytes() == before


@pytest.mark.parametrize("version", [6, 10])
def test_missing_or_unsupported_database_is_not_created_or_upgraded(tmp_path, version):
    from app.archive_inventory import InventoryError, export_inventory

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
    from app.archive_inventory import main

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
    from app.archive_inventory import InventoryError

    outside = tmp_path.parent / "not-a-catalog-root"
    outside.mkdir()
    (tmp_path / "catalogs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(InventoryError):
        export(archive)
    assert not list(outside.iterdir())
