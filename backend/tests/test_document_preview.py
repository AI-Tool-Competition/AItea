"""Content projection, bounded source images and existing ownership boundaries."""

import hashlib
import json
from copy import deepcopy

import pytest
from app.bootstrap import Service
from app.contexts.knowledge.application.document_preview import preview
from app.interfaces.http.factory import create_app
from app.shared.domain.errors import AppError
from app.shared.domain.values import dump, now
from fastapi.testclient import TestClient
from support.native_asset_ocr import docx, raster


@pytest.fixture
def service(tmp_path):
    value = Service(tmp_path)
    with value.db.transaction() as conn:
        for owner in ("alice", "bob"):
            conn.execute(
                "INSERT INTO users(id,name,is_demo,created_at) VALUES(?,?,0,?)", (owner, owner, now())
            )
    return value


def original(service, content, manifest, owner="alice"):
    blob = service.archive.blobs.put_bytes(owner, content)
    with service.db.transaction() as conn:
        document = service.archive.store_original(
            conn,
            owner,
            blob,
            "preview.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "https://example.org/preview.docx",
            collection="country:ID",
            level="primary",
            tags=[],
        )
        conn.execute(
            "UPDATE documents SET manifest=?,status='ready' WHERE id=?", (dump(manifest), document["id"])
        )
    return document["id"]


def test_preview_preserves_headings_table_spans_and_source_manifest():
    from app.contexts.knowledge.infrastructure.parsing.docx_parsing import parse_docx
    from support.docx_parsing import _p, _package

    manifest = parse_docx(
        _package(
            _p("Before table")
            + '<w:tbl><w:tblGrid><w:gridCol/><w:gridCol/></w:tblGrid><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
            + _p("Merged header")
            + "</w:tc></w:tr><w:tr><w:tc>"
            + _p("Name")
            + _p("Second paragraph")
            + "</w:tc><w:tc>"
            + _p("Tea")
            + "</w:tc></w:tr></w:tbl>"
            + _p("After table")
        )
    )
    before = deepcopy(manifest)
    result = preview(manifest)
    assert [block["kind"] for block in result["blocks"]] == ["paragraph", "table", "paragraph"]
    cells = result["blocks"][1]["cells"]
    assert cells[0]["col_span"] == 2
    assert cells[1]["text"] == "Name\nSecond paragraph"
    assert cells[2]["text"] == "Tea"
    assert manifest == before
    assert not result["summary"]  # No duplicate blurb for a short document.
    legacy = preview(
        {
            "elements": [
                {"kind": "table", "content_text": "Unmapped table paragraph one", "locator": {"table": 1}},
                {"kind": "table", "content_text": "Unmapped table paragraph two", "locator": {"table": 1}},
                {"kind": "paragraph", "content_text": "a" * 100001},
            ]
        }
    )
    assert len(legacy["blocks"]) == 3
    assert legacy["blocks"][1]["text"] == "Unmapped table paragraph two"
    assert len(legacy["blocks"][2]["text"]) == 100001


def test_preview_paginates_real_images_and_uses_only_existing_summary():
    _, manifest = docx(count=3)
    manifest["summary"] = "Stored source summary"
    before = deepcopy(manifest)
    first = preview(manifest, 0, 2)
    second = preview(manifest, 2, 2)
    assert first["summary"] == "Stored source summary"
    assert first["total"] == second["total"] == 4
    blocks = first["blocks"] + second["blocks"]
    assert [block["image_id"] for block in blocks if block["kind"] == "image"] == [
        "native-0",
        "native-1",
        "native-2",
    ]
    assert all(not block["summary"] for block in blocks if block["kind"] == "image")
    assert manifest == before
    excerpt = preview({"elements": [{"kind": "paragraph", "content_text": "Source words " * 50}]})
    assert not excerpt["summary"]


def test_document_image_reads_real_docx_pixels_with_existing_bounded_worker(service):
    content, manifest = docx()
    document_id = original(service, content, manifest)
    rendered = service.document_image("alice", document_id, "native-0")
    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    assert service.document_detail("alice", document_id)["preview"]["blocks"][-1]["image_id"] == "native-0"
    with pytest.raises(AppError) as denied:
        service.document_image("bob", document_id, "native-0")
    assert denied.value.status == 404
    with pytest.raises(AppError) as missing:
        service.document_image("alice", document_id, "../../secrets")
    assert missing.value.status == 404
    with service.db.transaction() as conn:
        saved = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document_id,)).fetchone()["manifest"]
        )
    assert saved == manifest
    assert "preview" not in service.document_detail("alice", document_id, include_preview=False)


def test_mapped_docx_pixels_cannot_read_external_or_modified_sources(service):
    content, manifest = docx()
    manifest["assets_pending"][0]["image_hash"] = "a" * 64
    document_id = original(service, content, manifest)
    with pytest.raises(AppError) as error:
        service.document_image("alice", document_id, "native-0")
    assert error.value.code == "image_unavailable"


def test_saved_pdf_image_uses_document_owner_cas_and_ignores_manifest_paths(service):
    pixels = raster()
    blob = service.archive.blobs.put_bytes("alice", pixels)
    manifest = {
        "elements": [],
        "pages": [
            {
                "page": 1,
                "source_hash": hashlib.sha256(b"pdf").hexdigest(),
                "ocr": {
                    "text": "Parsed image caption",
                    "image_blob": {
                        "storage_path": "/etc/passwd",
                        "content_hash": blob.digest,
                        "size": blob.size,
                        "mime_type": "image/png",
                    },
                },
            }
        ],
    }
    document_id = original(service, b"pdf", manifest)
    assert service.document_image("alice", document_id, "page-1") == pixels
    assert preview(manifest)["blocks"][0]["summary"] == "Parsed image caption"
    duplicate = deepcopy(manifest)
    duplicate["elements"] = [
        {"kind": "paragraph", "content_text": "Parsed image caption", "locator": {"page": 1}}
    ]
    assert preview(duplicate)["blocks"][-1]["summary"] == ""
    duplicate["pages"][0]["ocr"]["summary"] = "Existing visual summary"
    assert preview(duplicate)["blocks"][-1]["summary"] == "Existing visual summary"
    foreign = service.archive.blobs.put_bytes("bob", raster(size=(4, 4)))
    manifest["pages"][0]["ocr"]["image_blob"].update(
        content_hash=foreign.digest, size=foreign.size, storage_path=str(foreign.path)
    )
    with service.db.transaction() as conn:
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), document_id))
    with pytest.raises(AppError) as denied:
        service.document_image("alice", document_id, "page-1")
    assert denied.value.code == "image_unavailable"


def test_image_http_route_requires_login_and_sets_private_raster_headers(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        path = "/api/tea/documents/missing/images/native-0"
        assert client.get(path).status_code == 401
        assert client.post("/api/tea/auth/demo", json={}).status_code == 200
        owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
        content, manifest = docx()
        document_id = original(client.app.state.service, content, manifest, owner)
        response = client.get(f"/api/tea/documents/{document_id}/images/native-0")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "sandbox" in response.headers["content-security-policy"]


@pytest.mark.parametrize("nested_link", ["file", "owner"])
def test_saved_page_image_allows_configured_data_link_but_rejects_nested_links(tmp_path, nested_link):
    actual = tmp_path / "restored-data"
    actual.mkdir()
    configured = tmp_path / "data"
    configured.symlink_to(actual, target_is_directory=True)
    with TestClient(create_app(configured)) as client:
        assert client.post("/api/tea/auth/demo", json={}).status_code == 200
        service = client.app.state.service
        owner = client.get("/api/tea/auth/me").json()["id"]
        pixels = raster()
        blob = service.archive.blobs.put_bytes(owner, pixels)
        manifest = {
            "elements": [],
            "pages": [
                {
                    "page": 1,
                    "ocr": {
                        "image_blob": {
                            "content_hash": blob.digest,
                            "size": blob.size,
                            "mime_type": "image/png",
                        }
                    },
                }
            ],
        }
        identifier = original(service, b"pdf", manifest, owner)
        url = f"/api/tea/documents/{identifier}/images/page-1"
        response = client.get(url)
        assert response.status_code == 200
        assert response.content == pixels
        # Even identical image bytes may not be reached through a link inside CAS.
        source = blob.path if nested_link == "file" else blob.path.parents[2]
        outside = tmp_path / "untrusted"
        source.rename(outside)
        source.symlink_to(outside, target_is_directory=nested_link == "owner")
        assert client.get(url).status_code == 422


def test_shared_law_image_reads_source_owners_pixels_without_changing_eligibility(service):
    from app.contexts.knowledge.application.system_library import SYSTEM_OWNER_ID, ensure_principal
    from test_system_library import original as public_original

    with service.db.transaction() as conn:
        ensure_principal(conn)
    document = public_original(service, "preview-image")
    pixels = raster()
    blob = service.archive.blobs.put_bytes(SYSTEM_OWNER_ID, pixels)
    with service.db.transaction() as conn:
        manifest = json.loads(
            conn.execute("SELECT manifest FROM documents WHERE id=?", (document["id"],)).fetchone()[
                "manifest"
            ]
        )
        manifest["pages"] = [
            {
                "page": 1,
                "ocr": {
                    "image_blob": {"content_hash": blob.digest, "size": blob.size, "mime_type": "image/png"}
                },
            }
        ]
        conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), document["id"]))
    before = service.document_detail("bob", document["id"])["document"]["metadata"]
    assert service.document_image("bob", document["id"], "page-1") == pixels
    assert service.document_detail("bob", document["id"])["document"]["metadata"] == before
