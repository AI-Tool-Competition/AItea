import json
from types import SimpleNamespace

from app.bootstrap import Service
from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID, ensure_principal
from app.contexts.documents.infrastructure.parsers.native_parser import parse_file
from app.contexts.ingestion.application.html_document_binding import bind_ready_official_documents
from app.contexts.ingestion.application.official_source_import import OfficialSourceImporter
from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import (
    LINKED_DOCUMENT_PROTOCOL,
    update_quality,
)
from app.contexts.documents.infrastructure.providers.native_asset_ocr import assets
from app.shared.domain.values import dump, now


def test_official_html_context_includes_the_imported_page_hash(tmp_path, monkeypatch):
    service = Service(tmp_path)
    parent_url = "https://www.enterprisesg.gov.sg/guidance/regulation"
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "sg-authority-guidance",
                        "country": "SG",
                        "status": "active",
                        "kind": "guidance",
                        "url": parent_url,
                        "allowed_hosts": ["www.enterprisesg.gov.sg"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "app.configuration.paths.InstancePaths.resolve",
        lambda *_args: SimpleNamespace(source_pool=config),
    )
    monkeypatch.setattr(
        "app.contexts.ingestion.application.source_catalog.active_sources",
        lambda pool, _country, _validate_url: pool["sources"],
    )
    OfficialSourceImporter(service, config_path=config)
    html = b"<main><h1>Official guidance</h1><p>Source page.</p></main>"
    with service.db.transaction() as conn:
        ensure_principal(conn)
        blob = service.archive.blobs.put_bytes(SYSTEM_OWNER_ID, html)
        document = dict(
            service.archive.store_original(
                conn,
                SYSTEM_OWNER_ID,
                blob,
                "guidance.html",
                "text/html",
                parent_url,
                collection="country:SG",
                level="primary",
                tags=[],
            )
        )
        conn.execute(
            """INSERT INTO official_source_imports(
            event_key,source_id,country,source_url,content_hash,mime_type,document_id,status,created_at,updated_at
            ) VALUES('page','sg-authority-guidance','SG',?,?,'text/html',?,'stored',?,?)""",
            (parent_url, blob.digest, document["id"], now(), now()),
        )

    context = service.collection._official_html_source(document)
    assert context is not None
    assert context[1]["content_hash"] == blob.digest


def test_official_html_assets_allow_same_origin_and_explicit_asset_hosts(monkeypatch, tmp_path):
    service = Service(tmp_path)
    source = {
        "url": "https://www.enterprisesg.gov.sg/guidance/regulation",
        "allowed_hosts": ["www.enterprisesg.gov.sg"],
        "html_asset_hosts": ["assets.gov.sg"],
    }
    monkeypatch.setattr(service.collection, "_official_html_source", lambda _document: (source, {}))

    assert service.collection._official_html_asset_hosts({}) == [
        "www.enterprisesg.gov.sg",
        "assets.gov.sg",
    ]


def test_iframe_binds_only_to_same_source_pdf_version_with_ready_index(tmp_path):
    service = Service(tmp_path)
    config = tmp_path / "sources.json"
    config.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "id": "sg-authority-guidance",
                        "country": "SG",
                        "status": "active",
                        "kind": "guidance",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    OfficialSourceImporter(service, config_path=config)
    parent_url = "https://www.enterprisesg.gov.sg/guidance/regulation"
    target_url = "https://www.enterprisesg.gov.sg/documents/regulation.pdf"
    html = b'''<main><h1>Regulation</h1><p>Official source page.</p>
    <iframe src="/documents/regulation.pdf" title="Regulation.pdf"></iframe></main>'''
    pdf = b"%PDF-1.7\nverified test fixture\n%%EOF\n"
    profile = "d" * 64
    generation = "c" * 64

    with service.db.transaction() as conn:
        ensure_principal(conn)
        parent_blob = service.archive.blobs.put_bytes(SYSTEM_OWNER_ID, html)
        parent = dict(
            service.archive.store_original(
                conn,
                SYSTEM_OWNER_ID,
                parent_blob,
                "regulation-page.html",
                "text/html",
                parent_url,
                collection="country:SG",
                level="primary",
                tags=[],
            )
        )
        target_blob = service.archive.blobs.put_bytes(SYSTEM_OWNER_ID, pdf)
        target = dict(
            service.archive.store_original(
                conn,
                SYSTEM_OWNER_ID,
                target_blob,
                "regulation.pdf",
                "application/pdf",
                target_url,
                collection="country:SG",
                level="primary",
                tags=[],
            )
        )
        target_manifest = {
            "parser": "test-pdf",
            "parser_version": "1",
            "pipeline_version": "test-pdf-v1",
            "source_hash": target_blob.digest,
            "elements": [{"content_text": "Official regulation text"}],
            "chunks": [{"content_text": "Official regulation text"}],
            "quality": {"complete": True},
        }
        conn.execute(
            "UPDATE documents SET status='ready',manifest=? WHERE id=?",
            (dump(target_manifest), target["id"]),
        )
        conn.execute(
            """INSERT INTO ragflow_documents(
            generation,document_id,owner_id,profile,source_hash,source_version,index_revision,
            dataset_id,remote_id,name,state,chunk_count,created_at,updated_at
            ) VALUES(?,?,?,?,?,1,1,'dataset-1','remote-1','regulation','ready',1,?,?)""",
            (generation, target["id"], SYSTEM_OWNER_ID, profile, target_blob.digest, now(), now()),
        )
        conn.execute(
            """INSERT INTO official_source_imports(
            event_key,source_id,country,source_url,content_hash,mime_type,document_id,status,
            discovery_parent_url,discovery_parent_hash,created_at,updated_at
            ) VALUES(?,?,?,?,?,? ,?,'stored',?,?,?,?)""",
            (
                "parent-event",
                "sg-authority-guidance",
                "SG",
                parent_url,
                parent_blob.digest,
                "text/html",
                parent["id"],
                None,
                None,
                now(),
                now(),
            ),
        )
        conn.execute(
            """INSERT INTO official_source_imports(
            event_key,source_id,country,source_url,content_hash,mime_type,document_id,status,
            discovery_parent_url,discovery_parent_hash,created_at,updated_at
            ) VALUES(?,?,?,?,?,? ,?,'stored',?,?,?,?)""",
            (
                "pdf-event",
                "sg-authority-guidance",
                "SG",
                target_url,
                target_blob.digest,
                "application/pdf",
                target["id"],
                parent_url,
                parent_blob.digest,
                now(),
                now(),
            ),
        )

    service.embedding_fingerprint = lambda: profile
    parsed = parse_file("regulation-page.html", html, "text/html")
    source = {
        "id": "sg-authority-guidance",
        "allowed_hosts": ["www.enterprisesg.gov.sg"],
    }
    parent_import = {
        "source_id": "sg-authority-guidance",
        "source_url": parent_url,
        "content_hash": parent_blob.digest,
        "country": "SG",
    }
    bound = bind_ready_official_documents(
        service, parent, parsed, (source, parent_import)
    )
    update_quality(bound, assets(bound))

    proof = bound["html_source"]["linked_documents"]
    assert len(proof) == 1
    assert proof[0]["protocol"] == LINKED_DOCUMENT_PROTOCOL
    assert proof[0]["target_document_id"] == target["id"]
    assert proof[0]["target_hash"] == target_blob.digest
    assert bound["quality"]["complete"] is True

    with service.db.transaction() as conn:
        conn.execute("UPDATE ragflow_documents SET state='indexing' WHERE generation=?", (generation,))
    fresh = bind_ready_official_documents(service, parent, parsed, (source, parent_import))
    assert fresh["html_source"].get("linked_documents", []) == []
