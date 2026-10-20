"""Readable shared catalogs preserve privacy, evidence boundaries and atomic publication."""

import csv
import hashlib
import json
import stat

import pytest

from app import collection_catalog
from app.service import Service
from app.system_library import SYSTEM_OWNER_ID


@pytest.fixture
def data(tmp_path):
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        for owner in (SYSTEM_OWNER_ID, "personal"):
            conn.execute(
                "INSERT INTO users(id,name,is_demo,created_at) VALUES(?,?,0,'2026-10-20')", (owner, owner)
            )
        for identifier, owner, country, level in (
            ("id-law", SYSTEM_OWNER_ID, "ID", "primary"),
            ("sg-guidance", SYSTEM_OWNER_ID, "SG", "secondary"),
            ("private", "personal", "ID", "primary"),
        ):
            blob = service.archive.blobs.put_bytes(owner, identifier.encode())
            conn.execute(
                """INSERT INTO documents(id,owner_id,title,filename,role,status,mime_type,size,version,
                content_hash,storage_path,tags,source_url,manifest,created_at,family_id)
                VALUES(?,?,?,?,'primary','ready','text/plain',?,1,?,?,'[]',?,?,'2026-10-20',?)""",
                (
                    identifier,
                    owner,
                    "=UNTRUSTED()",
                    identifier + ".txt",
                    blob.size,
                    blob.digest,
                    str(blob.path),
                    "https://official.example/law",
                    json.dumps(
                        {
                            "elements": [{"text": identifier}],
                            "chunks": [{"text": identifier}],
                            "quality": {"complete": True},
                        }
                    ),
                    identifier,
                ),
            )
            conn.execute("INSERT INTO document_countries VALUES(?,?)", (identifier, country))
            conn.execute(
                "INSERT INTO legal_classifications VALUES(?,?,?,'[]',1,'2026-10-20')",
                (
                    identifier,
                    "country:" + country,
                    level,
                ),
            )
    return tmp_path


def rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def test_catalog_is_shared_only_filterable_and_verifiable(data):
    report = collection_catalog.export_materials(data)
    path = report["path"]
    documents = rows(path / "documents.csv")
    assert {row["document_id"] for row in documents} == {"id-law", "sg-guidance"}
    assert {row["legal_level"] for row in documents} == {"primary", "secondary"}
    assert all(row["title"] == "'=UNTRUSTED()" for row in documents)
    assert "vectors_incomplete_or_unknown" in documents[0]["gaps"]
    assert report["summary"]["raw_observations"]["ID"]["status"] == "unavailable_or_invalid"
    inventory = path.parent / report["summary"]["inventory_path"]
    assert (inventory / "COMPLETE").is_file()
    assert "private" not in (path / "documents.csv").read_text()
    complete = json.loads((path / "COMPLETE.json").read_text())
    for name, expected in complete["files"].items():
        file = path / name
        assert expected["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
        assert expected["size"] == file.stat().st_size
        assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o700
    filtered = collection_catalog.export_materials(data, country="SG")
    assert [row["document_id"] for row in rows(filtered["path"] / "documents.csv")] == ["sg-guidance"]


@pytest.mark.parametrize("raw_path", ["files/raw-hash", "../escape", "/tmp/escape"])
def test_raw_candidates_remain_unreviewed_and_distinct_from_formal_records(data, raw_path):
    directory = data / "official-updates/ID"
    directory.mkdir(parents=True)
    state = {
        "binding": {"country": "ID"},
        "updated_at": 1792480000,
        "status": "waiting",
        "sources": {
            "official": {
                "health": "access_restricted",
                "next_at": 1792540805,
                "last_observation": {"status": "http_error", "http_status": 403},
                "cache": {
                    "https://official.example/raw.pdf": {
                        "url": "https://official.example/raw.pdf",
                        "path": raw_path,
                        "content_hash": "a" * 64,
                        "size": 20,
                        "legal_review": "published",
                        "mime_type": "application/pdf",
                        "discovery": {"purpose": "document"},
                    }
                },
                "frontier": [{"url": "https://official.example/next"}],
            }
        },
    }
    (directory / "state.json").write_text(json.dumps(state))
    report = collection_catalog.export_materials(data, country="ID")
    raw = rows(report["path"] / "raw-latest-versions.csv")
    assert raw[0]["legal_review"] == "unreviewed"
    if raw_path == "files/raw-hash":
        assert raw[0]["original_path"] == "official-updates/ID/files/raw-hash"
        assert raw[0]["original_exists"] == "False"
    else:
        assert raw[0]["original_path"] == "" and "invalid_raw_path" in raw[0]["anomalies"]
    assert len(rows(report["path"] / "documents.csv")) == 1
    source = rows(report["path"] / "sources.csv")[0]
    assert source["health"] == "access_restricted" and source["http_status"] == "403"
    assert source["frontier_urls"] == "1" and source["next_at_utc"].endswith("+00:00")


def test_failed_derived_export_does_not_publish_or_destroy_core_snapshot(data, monkeypatch):
    def fail(*args):
        raise OSError("injected disk failure")

    monkeypatch.setattr(collection_catalog, "_documents", fail)
    with pytest.raises(OSError, match="injected"):
        collection_catalog.export_materials(data)
    assert not list((data / "catalogs").glob("materials-*"))
    assert not list((data / "catalogs").glob(".materials-*"))
    assert len(list((data / "catalogs").glob("*/COMPLETE"))) == 1


def test_symlink_raw_state_is_reported_without_reading_external_content(data):
    directory = data / "official-updates/ID"
    directory.mkdir(parents=True)
    (directory / "state.json").symlink_to(data / "tea.sqlite3")
    report = collection_catalog.export_materials(data)
    assert report["summary"]["raw_observations"]["ID"]["status"] == "unavailable_or_invalid"
    assert rows(report["path"] / "sources.csv") == []
    with pytest.raises(collection_catalog.InventoryError):
        collection_catalog.export_materials(data, country="XX")


def test_mixed_models_do_not_hide_vector_gaps(data, tmp_path):
    report = collection_catalog.export_materials(data, country="ID")
    inventory = report["path"].parent / report["summary"]["inventory_path"]
    record = json.loads(next(inventory.glob("*/*/documents.jsonl")).read_text())
    record["retrieval"] = {
        "status": "available",
        "source_hash_matches": True,
        "complete": True,
        "indexed_chunk_count": 2,
        "matching_vector_count": 2,
        "fingerprint_counts": {"model-a": 1, "model-b": 1},
    }
    # Use a synthetic snapshot; published production snapshots are never edited.
    snapshot = tmp_path / "synthetic-snapshot/owner/ID"
    snapshot.mkdir(parents=True)
    (snapshot / "documents.jsonl").write_text(json.dumps(record) + "\n")
    output = tmp_path / "derived-test"
    output.mkdir()
    from collections import Counter

    summary = {"legal_levels": Counter(), "gap_counts": Counter(), "documents_with_gaps": 0}
    collection_catalog._documents(snapshot.parent.parent, output, summary)
    assert "vectors_incomplete_or_unknown" in rows(output / "documents.csv")[0]["gaps"]
