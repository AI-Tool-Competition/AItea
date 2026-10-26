"""Readable shared catalogs preserve privacy, evidence boundaries and atomic publication."""

import csv
import hashlib
import json
import stat

import pytest
from app.bootstrap import Service
from app.contexts.ingestion.infrastructure import collection_catalog as collection_catalog
from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID


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


def test_index_queue_summary_is_aggregate_and_shared_only(data):
    service = Service(data)
    stale = "2026-10-20T00:00:00+00:00"
    with service.db.transaction() as conn:
        for document_id, owner_id, generation in (
            ("id-law", SYSTEM_OWNER_ID, "shared-generation"),
            ("private", "personal", "private-generation"),
        ):
            conn.execute(
                """INSERT INTO ragflow_documents(
                generation,document_id,owner_id,profile,source_hash,source_version,index_revision,
                name,state,chunk_count,created_at,updated_at
                ) VALUES(?,?,?,'profile','hash',1,1,'stored-name','indexing',1,?,?)""",
                (generation, document_id, owner_id, stale, stale),
            )
            conn.execute(
                """INSERT INTO ragflow_chunks(
                generation,local_id,input_hash,content_hash,state,attempts,intent_at,next_at
                ) VALUES(?,?, 'input','content','intent',1,0,0)""",
                (generation, document_id + '-chunk'),
            )
            conn.execute(
                """INSERT INTO processing_jobs(document_id,owner_id,phase,status,updated_at)
                VALUES(?,?,'embed','failed',?)""",
                (document_id, owner_id, stale),
            )
            if owner_id == SYSTEM_OWNER_ID:
                conn.execute(
                    """INSERT INTO collection_recovery_jobs(
                    id,owner_id,queue_kind,item_id,phase,status,reason,binding,binding_sha256,created_at,updated_at
                    ) VALUES('shared-unknown',?,'processing',?,'embed','blocked','paid_result_unknown','{}','hash',?,?)""",
                    (owner_id, document_id, stale, stale),
                )

    report = collection_catalog.export_materials(data)
    queue = report["summary"]["inventory"]["index_queue"]
    assert queue["status"] == "available"
    assert queue["remote_generations"] == {"indexing": 1}
    assert queue["chunks"] == {"intent": 1}
    assert queue["processing_jobs"] == {"embed:failed": 1}
    assert queue["blocked_paid_result_unknown_jobs"] == 1
    assert queue["stale_remote_generations_over_15m"] == 1
    assert "private" not in json.dumps(queue)


def _add_ragflow_index(data, *, profile, matching_receipt):
    service = Service(data)
    with service.db.transaction() as conn:
        document = conn.execute(
            "SELECT owner_id,content_hash,version FROM documents WHERE id='id-law'"
        ).fetchone()
        source_hash = document["content_hash"]
        conn.execute(
            "INSERT INTO retrieval_documents VALUES('id-law',?,1,1,1)", (source_hash,)
        )
        conn.execute(
            """INSERT INTO retrieval_chunks(
            document_id,chunk_id,ordinal,text,retrieval_text,tokens,locator,content_hash,input_hash
            ) VALUES('id-law','law-chunk',0,'law text','law text','law','p1',?,'input-hash')""",
            (source_hash,),
        )
        conn.execute("DELETE FROM retrieval_dirty WHERE document_id='id-law'")
        conn.execute(
            """INSERT INTO ragflow_documents(
            generation,document_id,owner_id,profile,source_hash,source_version,index_revision,
            dataset_id,remote_id,name,state,chunk_count,created_at,updated_at
            ) VALUES('current-generation','id-law',?,?,?,1,1,'dataset','remote-law','law','ready',1,?,?)""",
            (document["owner_id"], profile, source_hash, "2026-10-20", "2026-10-20"),
        )
        conn.execute(
            """INSERT INTO ragflow_chunks(
            generation,local_id,input_hash,content_hash,remote_id,state,attempts,intent_at,next_at
            ) VALUES('current-generation','law-chunk','input-hash',?,?,'done',1,0,0)""",
            (source_hash if matching_receipt else "stale-content-hash", "remote-chunk"),
        )


def test_catalog_uses_current_ragflow_chunk_receipts_instead_of_legacy_vector_rows(data):
    _add_ragflow_index(data, profile="current-profile", matching_receipt=True)
    report = collection_catalog.export_materials(data, country="ID", ragflow_profile="current-profile")
    row = rows(report["path"] / "documents.csv")[0]
    gaps = json.loads(row["gaps"])
    assert row["index_backend"] == "ragflow"
    assert row["index_status"] == "ready"
    assert row["index_complete"] == "True"
    assert row["source_hash_matches"] == "True"
    assert row["matching_vector_count"] == "0"
    assert row["ragflow_generation_state"] == "ready"
    assert row["ragflow_declared_chunk_count"] == "1"
    assert row["ragflow_matching_chunk_count"] == "1"
    assert row["ragflow_profile_matches_current"] == "True"
    assert "vectors_incomplete_or_unknown" not in gaps
    assert "ragflow_chunks_incomplete_or_unknown" not in gaps


def test_catalog_keeps_remote_chunk_hash_mismatch_visible(data):
    _add_ragflow_index(data, profile="current-profile", matching_receipt=False)
    report = collection_catalog.export_materials(data, country="ID", ragflow_profile="current-profile")
    row = rows(report["path"] / "documents.csv")[0]
    gaps = json.loads(row["gaps"])
    assert row["index_status"] == "incomplete"
    assert row["index_complete"] == "False"
    assert row["ragflow_matching_chunk_count"] == "0"
    assert "ragflow_chunks_incomplete_or_unknown" in gaps


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


def test_catalog_resolves_country_registry_revision_for_source_metadata(data):
    directory = data / "official-updates/ID"
    history = directory / "registry-versions"
    history.mkdir(parents=True)
    registry = {
        "protocol": "aitea-official-source-pool-v2",
        "sources": [
            {
                "id": "official-id",
                "country": "ID",
                "status": "active",
                "title": "Indonesian official catalogue",
                "url": "https://jdih.example.id/regulations",
                "kind": "catalogue",
                "topics": ["trade"],
            },
            {
                "id": "archived-id",
                "country": "ID",
                "status": "archived",
                "title": "Archived Indonesian catalogue",
                "url": "https://archive.example.id/regulations",
                "kind": "catalogue",
                "topics": ["trade"],
            },
            {
                "id": "official-sg",
                "country": "SG",
                "title": "Singapore official catalogue",
                "url": "https://example.gov.sg/regulations",
                "kind": "catalogue",
                "topics": ["trade"],
            },
        ],
    }
    raw_registry = json.dumps(registry, ensure_ascii=False, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw_registry).hexdigest()
    (history / f"{digest}.json").write_bytes(raw_registry)
    (directory / "state.json").write_text(
        json.dumps(
            {
                "binding": {"country": "ID", "pool_sha256": digest},
                "updated_at": 1792480000,
                "status": "waiting",
                "sources": {
                    "official-id": {
                        "health": "healthy",
                        "last_observation": {"status": "success", "http_status": 200},
                        "cache": {},
                        "frontier": {},
                    },
                    "archived-id": {
                        "health": "degraded",
                        "last_observation": {"status": "network_error"},
                        "cache": {},
                        "frontier": {},
                    },
                },
            }
        )
    )

    report = collection_catalog.export_materials(data, country="ID")

    source_rows = {row["source_id"]: row for row in rows(report["path"] / "sources.csv")}
    assert report["summary"]["format_version"] == 3
    assert report["summary"]["raw_observations"]["ID"]["source_metadata_available"] is True
    assert source_rows["official-id"]["registry_status"] == "active"
    assert source_rows["official-id"]["title"] == "Indonesian official catalogue"
    assert source_rows["official-id"]["url"] == "https://jdih.example.id/regulations"
    assert source_rows["official-id"]["topics"] == '["trade"]'
    assert source_rows["archived-id"]["registry_status"] == "archived"
    assert "Singapore official catalogue" not in json.dumps(source_rows)


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
