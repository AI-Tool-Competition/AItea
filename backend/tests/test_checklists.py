"""Document checklists are scoped plans, not an implicit full-site crawler."""

import json
from contextlib import closing
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from admin_helpers import grant_admin, login_admin

from app.main import create_app
from app.service import Service

PLAN = "indonesia-20261003"
BASE = f"/api/tea/collection-plans/{PLAN}"


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(data_dir=tmp_path)) as client:
        login_admin(client)
        yield client


def configure(client):
    response = client.post(
        BASE + "/configure", json={"expected_version": client.get(BASE).json()["plan"]["version"]}
    )
    assert response.status_code == 200, response.text
    return response.json()


def discover(client, code="1.3", request="first"):
    response = client.post(BASE + f"/items/{code}/discover", json={"client_request_id": request})
    assert response.status_code == 202, response.text
    return response.json()


def test_catalog_has_both_source_checklists_without_implicitly_seeding_data(client):
    catalog = client.get("/api/tea/collection-plans")
    assert catalog.status_code == 200
    assert [(p["country"], p["item_count"]) for p in catalog.json()["items"]] == [("ID", 29), ("SG", 16)]
    detail = client.get(BASE).json()
    assert len(detail["plan"]["chapters"]) == 5
    assert len(detail["items"]) == 29
    assert all(i["source_id"] is None and i["coverage"] == "not_started" for i in detail["items"])
    assert (
        detail["plan"]["source_document"]["sha256"]
        == "eee5a9d8b42183dd4a3ad09ca53903ded3e47141ad9b187360b6d5a27a38e944"
    )
    singapore = client.get(BASE.replace(PLAN, "singapore-demo-20261009")).json()
    assert (
        singapore["plan"]["source_document"]["sha256"]
        == "cd3f72ba56c05b62c55a8dfe558a53fe84978613843d9712380cc5d01b5feb47"
    )
    assert len(singapore["plan"]["channels"]) == 6
    assert [item["id"] for item in singapore["plan"]["items"] if item["conditional"]] == ["4.1", "4.2"]
    assert singapore["plan"]["items"][3]["execution"] == "dataset_review"
    assert all(item["source_id"] is None for item in singapore["items"])


def test_configuration_is_atomic_idempotent_versioned_and_private(client, tmp_path):
    assert configure(client) == {"created": 29, "existing": 0}
    assert configure(client) == {"created": 0, "existing": 29}
    assert (
        client.post(
            BASE + "/configure", json={"expected_version": client.get(BASE).json()["plan"]["version"] + 1}
        ).status_code
        == 409
    )
    detail = client.get(BASE).json()
    source_id = detail["items"][0]["source_id"]
    service = Service(tmp_path)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM checklist_bindings").fetchone()[0] == 29
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=7").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert not row["enabled"]
        assert json.loads(row["plan"])["collection"] == "country:ID"
    # A bound source cannot bypass the checklist into generic directory parsing.
    assert (
        client.post(f"/api/tea/sources/{source_id}/runs", json={"client_request_id": "bypass"}).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/tea/sources/{source_id}", json={"expected_revision": 1, "url": "https://example.org/other"}
        ).status_code
        == 409
    )
    client.post("/api/tea/auth/logout")
    other = client.post("/api/tea/auth/demo", json={}).json()
    assert client.get(BASE).status_code == 403
    grant_admin(client, other["id"])
    assert all(i["source_id"] is None for i in client.get(BASE).json()["items"])
    assert client.get(f"/api/tea/sources/{source_id}/runs").status_code == 404
    assert configure(client)["created"] == 29


def fake_web(monkeypatch):
    from app import ingestion

    pages = {
        "https://peraturan.bpk.go.id/Search?jenis=8": b'<html><title>Undang-Undang</title><a href="/Details/10/uu-1">UU No 1</a><a href="/Search?jenis=8&p=2">Next</a></html>',
        "https://peraturan.bpk.go.id/Search?jenis=8&p=2": b'<html><title>Undang-Undang</title><a href="/Details/20/uu-2">UU No 2</a></html>',
        "https://peraturan.bpk.go.id/Details/10/uu-1": b'<html><title>UU 1</title><a href="/Download/10/uu-1.pdf">Original</a></html>',
        "https://peraturan.bpk.go.id/Details/20/uu-2": b'<html><title>UU 2</title><a href="/Download/20/uu-2.pdf">Original</a></html>',
    }

    def download(url, *, consume, **kwargs):
        if "jdih.setneg.go.id/api/hukumproduk/produkhukum" in url:
            body = b'{"data":[],"jml":0,"jn":["UU"]}'
            return {
                "url": url,
                "filename": "produkhukum",
                "mime_type": "application/json",
                "request_method": "POST",
                "blob": consume(iter([body])),
            }
        body = pages.get(url, b"%PDF-1.4\n%%EOF")
        return {
            "url": url,
            "filename": url.rsplit("/", 1)[-1],
            "mime_type": "application/pdf" if url.endswith(".pdf") else "text/html",
            "blob": consume(iter([body])),
        }

    monkeypatch.setattr(ingestion, "_download", download)
    return pages


def drain_downloads(client):
    queue = client.app.state.service.collection
    for _ in range(30):
        claim = queue.claim_fetch()
        if not claim:
            return
        queue.fetch_item(claim)
    pytest.fail("unbounded discovery queue")


def test_discovery_commits_pages_and_candidates_without_legal_documents(client, monkeypatch):
    fake_web(monkeypatch)
    run = discover(client)
    assert run["kind"] == "discovery"
    assert discover(client)["id"] == run["id"]
    drain_downloads(client)
    run = client.get(f"/api/tea/collection-runs/{run['id']}").json()["run"]
    assert run["pages"] == 5 and run["stored"] == 0, run
    with closing(client.app.state.service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM processing_jobs").fetchone()[0] == 0
    candidates = client.get(BASE + "/items/1.3/candidates").json()
    assert candidates["total"] == 2
    assert all(c["url"].endswith(".pdf") and c["status"] == "pending" for c in candidates["items"])
    item = next(i for i in client.get(BASE).json()["items"] if i["item_id"] == "1.3")
    assert item["counts"]["pages"] == 5 and item["counts"]["stored"] == 0
    assert item["coverage"] != "completed"


def test_selected_candidates_retain_country_tier_and_immutable_run_snapshot(client, monkeypatch):
    fake_web(monkeypatch)
    discover(client)
    drain_downloads(client)
    candidate = client.get(BASE + "/items/1.3/candidates").json()["items"][0]
    payload = {
        "client_request_id": "collect",
        "candidate_ids": [candidate["id"]],
        "level": "primary",
        "primary_document_ids": [],
    }
    url = BASE + "/items/1.3/collect"
    response = client.post(url, json=payload)
    assert response.status_code == 202, response.text
    assert client.post(url, json=payload).json()["id"] == response.json()["id"]
    drain_downloads(client)
    stored = client.get(BASE + "/items/1.3/candidates?status=stored").json()["items"][0]
    document = client.get(f"/api/tea/documents/{stored['document_id']}").json()["document"]
    assert document["classifications"][0]["collection"] == "country:ID"
    assert document["metadata"]["jurisdiction"] == "ID"
    assert document["metadata"]["review_status"] == "draft"
    assert (
        client.patch(
            f"/api/tea/collection-candidates/{candidate['id']}",
            json={"expected_revision": stored["revision"], "status": "excluded"},
        ).status_code
        == 409
    )


def test_candidates_are_private_and_require_revision_and_country_anchors(client, monkeypatch):
    fake_web(monkeypatch)
    discover(client)
    drain_downloads(client)
    candidate = client.get(BASE + "/items/1.3/candidates").json()["items"][0]
    patch = {"expected_revision": candidate["revision"], "status": "excluded", "note": "超出条目范围"}
    path = f"/api/tea/collection-candidates/{candidate['id']}"
    assert client.patch(path, json=patch).status_code == 200
    assert client.patch(path, json=patch).status_code == 409
    assert (
        client.post(
            BASE + "/items/1.3/collect",
            json={
                "client_request_id": "missing-anchor",
                "candidate_ids": [candidate["id"]],
                "level": "secondary",
            },
        ).status_code
        == 422
    )
    client.post("/api/tea/auth/logout")
    other = client.post("/api/tea/auth/demo", json={}).json()
    assert client.get(BASE).status_code == 403
    grant_admin(client, other["id"])
    assert client.patch(path, json=patch).status_code == 404
    assert client.get(BASE + "/items/1.3/candidates").json()["total"] == 0


def test_block_pages_do_not_create_candidates_or_documents(client, monkeypatch):
    pages = fake_web(monkeypatch)
    pages["https://peraturan.bpk.go.id/Search?jenis=8"] = (
        b"<html><title>Access Denied</title><h1>403 Forbidden</h1></html>"
    )
    run = discover(client)
    drain_downloads(client)
    detail = client.get(f"/api/tea/collection-runs/{run['id']}").json()
    assert detail["run"]["failed"] == 1
    assert client.get(BASE + "/items/1.3/candidates").json()["total"] == 0
    with closing(client.app.state.service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_bounded_pages_resume_from_durable_frontier(client, monkeypatch, tmp_path):
    from app import checklists

    monkeypatch.setattr(checklists, "PAGE_BATCH", 3)
    fake_web(monkeypatch)
    first = discover(client)
    drain_downloads(client)
    progress = client.get(f"/api/tea/collection-runs/{first['id']}").json()["run"]
    assert progress["pages"] == 3 and progress["total"] == 3
    state = next(x for x in client.get(BASE).json()["items"] if x["item_id"] == "1.3")
    assert state["counts"]["remaining_pages"] > 0
    service = Service(tmp_path)
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    second = service.checklists.start(
        owner, PLAN, "1.3", {"client_request_id": "resume", "confirm_condition": False}, kind="discovery"
    )
    drain_downloads(client)
    assert second["id"] != first["id"]
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM checklist_pages").fetchone()[0] == 5
        assert conn.execute("SELECT COUNT(DISTINCT url) FROM checklist_pages").fetchone()[0] == 5
    assert client.get(BASE + "/items/1.3/candidates").json()["total"] == 2


def test_cancel_and_retry_respects_candidate_exclusion(client, monkeypatch):
    fake_web(monkeypatch)
    discover(client)
    drain_downloads(client)
    candidate = client.get(BASE + "/items/1.3/candidates").json()["items"][0]
    run = client.post(
        BASE + "/items/1.3/collect",
        json={"client_request_id": "collect-cancel", "candidate_ids": [candidate["id"]], "level": "primary"},
    ).json()
    cancelled = client.post(
        f"/api/tea/collection-runs/{run['id']}/cancel", json={"expected_revision": run["revision"]}
    ).json()
    candidate = next(
        c for c in client.get(BASE + "/items/1.3/candidates").json()["items"] if c["id"] == candidate["id"]
    )
    assert candidate["status"] == "pending"
    assert (
        client.patch(
            f"/api/tea/collection-candidates/{candidate['id']}",
            json={"expected_revision": candidate["revision"], "status": "excluded"},
        ).status_code
        == 200
    )
    retry = client.post(
        f"/api/tea/collection-runs/{run['id']}/retry", json={"expected_revision": cancelled["revision"]}
    )
    assert retry.status_code == 409


def test_plan_snapshot_changes_require_a_new_definition_version(client):
    configure(client)
    service = client.app.state.service
    service.checklists.plans[PLAN]["items"][0]["title"] = "Changed after configuration"
    response = client.post(
        BASE + "/configure", json={"expected_version": client.get(BASE).json()["plan"]["version"]}
    )
    assert response.status_code == 409
    with closing(service.db.connect()) as conn:
        snapshot = json.loads(
            conn.execute("SELECT snapshot FROM checklist_bindings WHERE item_id='1.1'").fetchone()[0]
        )
        assert snapshot["item"]["title"] != "Changed after configuration"


def test_country_lists_and_rag_cannot_use_implicit_mixed_scope(client):
    upload = client.post(
        "/api/tea/documents", files={"file": ("law.txt", b"tea export compliance", "text/plain")}
    ).json()
    assert (
        client.patch(
            f"/api/tea/documents/{upload['id']}/classification",
            json={"collection": "country:ID", "level": "primary", "expected_revision": 0},
        ).status_code
        == 200
    )
    assert client.get("/api/tea/documents").json()["total"] == 0
    assert client.get("/api/tea/documents?country=SG").json()["total"] == 0
    docs = client.get("/api/tea/documents?country=ID").json()
    assert docs["total"] == 1 and docs["items"][0]["country"] == "ID"
    assert client.get("/api/tea/documents?country=SG&collection=country:ID").status_code == 422
    assert (
        client.post(
            "/api/tea/search", json={"query": "tea", "country": "SG", "collection": "country:ID"}
        ).status_code
        == 422
    )


def test_existing_v6_database_upgrades_without_seeding_or_rewriting_data(client, tmp_path):
    doc = client.post(
        "/api/tea/documents", files={"file": ("legacy.txt", b"preserved original", "text/plain")}
    ).json()
    service = client.app.state.service
    # Recreate the previous deployed schema shape, retaining its actual data.
    with service.db.transaction() as conn:
        for table in (
            "checklist_page_evidence",
            "checklist_pages",
            "checklist_frontier",
            "checklist_candidates",
            "checklist_bindings",
            "document_countries",
        ):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("ALTER TABLE sources DROP COLUMN checklist")
        for column in ("purpose", "depth", "candidate_id"):
            conn.execute(f"ALTER TABLE collection_items DROP COLUMN {column}")
        conn.execute("DELETE FROM schema_migrations WHERE version IN (7,11)")
        old = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone())
    restarted = Service(tmp_path)
    with closing(restarted.db.connect()) as conn:
        assert dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()) == old
        assert conn.execute("SELECT COUNT(*) FROM checklist_bindings").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=7").fetchone()[0] == 1
    Service(tmp_path)  # The migration is idempotent after a restart.


def test_primary_filter_uses_country_level_instead_of_looser_group(client):
    doc = client.post(
        "/api/tea/documents", files={"file": ("linked.txt", b"tea linked law", "text/plain")}
    ).json()
    for collection, level in (("country:ID", "secondary"), ("loose-group", "primary")):
        assert (
            client.patch(
                f"/api/tea/documents/{doc['id']}/classification",
                json={"collection": collection, "level": level, "expected_revision": 0},
            ).status_code
            == 200
        )
    assert client.get("/api/tea/documents?country=ID&legal_level=primary").json()["total"] == 0
    assert client.get("/api/tea/documents?country=ID&legal_level=secondary").json()["total"] == 1


def test_country_batch_discovery_is_idempotent_and_starts_independent_jobs(client):
    response = client.post(BASE + "/discover", json={"client_request_id": "country-batch"})
    assert response.status_code == 202, response.text
    batch = response.json()
    assert batch["started"] == 29 and len(batch["runs"]) == 29
    repeated = client.post(BASE + "/discover", json={"client_request_id": "country-batch"}).json()
    assert {run["id"] for run in repeated["runs"]} == {run["id"] for run in batch["runs"]}
    assert client.post(BASE + "/discover", json={"client_request_id": "other-batch"}).json()["started"] == 0


def test_fresh_discovery_revisits_completed_catalog_pages_for_new_laws(client, monkeypatch):
    pages = fake_web(monkeypatch)
    discover(client)
    drain_downloads(client)
    assert client.get(BASE + "/items/1.3/candidates").json()["total"] == 2
    pages["https://peraturan.bpk.go.id/Search?jenis=8&p=2"] += b'<a href="/Details/30/uu-3">UU No 3</a>'
    pages["https://peraturan.bpk.go.id/Details/30/uu-3"] = (
        b'<html><title>UU 3</title><a href="/Download/30/uu-3.pdf">Original</a></html>'
    )
    discover(client, request="refresh")
    drain_downloads(client)
    assert client.get(BASE + "/items/1.3/candidates").json()["total"] == 3


def test_country_attachments_inherit_parent_scope_in_details_and_lists(client):
    doc = client.post(
        "/api/tea/documents", files={"file": ("main.txt", b"main legal text", "text/plain")}
    ).json()
    client.patch(
        f"/api/tea/documents/{doc['id']}/classification",
        json={"collection": "country:ID", "level": "primary", "expected_revision": 0},
    )
    child = client.post(
        "/api/tea/documents",
        data={"role": "secondary", "parent_id": doc["id"]},
        files={"file": ("appendix.txt", b"appendix data", "text/plain")},
    ).json()
    assert child["country"] == "ID"
    attachments = client.get(f"/api/tea/documents?parent_id={doc['id']}").json()
    assert attachments["total"] == 1 and attachments["items"][0]["country"] == "ID"
    assert client.get(f"/api/tea/documents?parent_id={doc['id']}&country=SG").json()["total"] == 0


def test_business_search_derives_country_and_rejects_overrides(client):
    documents = {}
    for country in ("ID", "SG"):
        doc = client.post(
            "/api/tea/documents", files={"file": (f"{country}.txt", b"tea country rule", "text/plain")}
        ).json()
        classified = client.patch(
            f"/api/tea/documents/{doc['id']}/classification",
            json={"collection": f"country:{country}", "level": "primary", "expected_revision": 0},
        ).json()
        reviewed = client.patch(
            f"/api/tea/documents/{doc['id']}/metadata",
            json={"expected_revision": classified["metadata_revision"], "review_status": "published"},
        )
        assert reviewed.status_code == 200, reviewed.text
        documents[country] = doc["id"]
    case = client.get("/api/tea/bootstrap").json()["cases"][0]
    payload = {"case_id": case["id"], "query": "tea"}
    found = client.post("/api/tea/search", json=payload).json()["items"]
    assert {item["document_id"] for item in found} == {documents["ID"]}
    for country in ("SG", "unassigned"):
        assert client.post("/api/tea/search", json=payload | {"country": country}).status_code == 422
        assert client.get(f"/api/tea/documents?case_id={case['id']}&country={country}").status_code == 422
    assert client.post("/api/tea/search", json={"query": "tea"}).json()["items"] == []


@pytest.mark.parametrize(
    "code,kind,url",
    [
        ("3.3", "case", "https://putusan3.mahkamahagung.go.id/direktori/download_file/abc/123"),
        ("4.1", "guidance", "https://jdih.bkpm.go.id/uploads/guide.pdf"),
        ("5.3", "law", "https://www.dfat.gov.au/sites/default/files/rcep.pdf"),
    ],
)
def test_checklist_document_type_is_only_a_draft_hint(client, monkeypatch, code, kind, url):
    from app.db import now, uid

    fake_web(monkeypatch)
    configure(client)
    service = client.app.state.service
    candidate_id = uid()
    with service.db.transaction() as conn:
        binding = conn.execute("SELECT * FROM checklist_bindings WHERE item_id=?", (code,)).fetchone()
        conn.execute(
            "INSERT INTO checklist_candidates(id,binding_id,url,title,discovered_from,created_at) VALUES(?,?,?,?,?,?)",
            (
                candidate_id,
                binding["id"],
                url,
                "Official original",
                "https://official.example/catalog",
                now(),
            ),
        )
    response = client.post(
        BASE + f"/items/{code}/collect",
        json={"client_request_id": "typed-collection", "candidate_ids": [candidate_id], "level": "primary"},
    )
    assert response.status_code == 202, response.text
    drain_downloads(client)
    candidates = client.get(BASE + f"/items/{code}/candidates?status=stored").json()
    assert candidates["total"] == 1
    document = client.get(f"/api/tea/documents/{candidates['items'][0]['document_id']}").json()["document"]
    assert document["metadata"]["kind"] == kind
    assert document["metadata"]["review_status"] == "draft"


def test_setneg_post_discovery_keeps_immutable_request_and_total_separate_from_documents(client, monkeypatch):
    from app import ingestion

    service = client.app.state.service
    item = next(x for x in service.checklists.plans[PLAN]["items"] if x["id"] == "1.3")
    item["entries"] = [
        {
            "url": "https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=UU&length=1&start=0",
            "purpose": "catalog",
            "note": "Bounded fixture",
        }
    ]
    detail = json.loads((Path(__file__).parent / "fixtures/discovery/setneg-detail.json").read_bytes())
    row = detail["row"][0]
    requests = []

    def download(url, *, consume, json_body=None, accept_compressed_json=False, **kwargs):
        requests.append((url, json_body, accept_compressed_json))
        assert json_body is not None and accept_compressed_json
        if urlsplit(url).path.endswith("produkhukum"):
            assert json_body["start"] == int(parse_qs(urlsplit(url).query)["start"][0])
            data = {"data": [row], "jml": 1, "jn": ["UU"]}
        else:
            assert json_body == {"jns": "UU", "no": "2", "thn": "2026", "k": ""}
            data = detail
        return {
            "url": url,
            "filename": "response.json",
            "mime_type": "application/json",
            "request_method": "POST",
            "blob": consume(iter([json.dumps(data).encode()])),
        }

    monkeypatch.setattr(ingestion, "_download", download)
    run = discover(client)
    drain_downloads(client)
    state = client.get(f"/api/tea/collection-runs/{run['id']}").json()["run"]
    assert state["pages"] == 2 and state["failed"] == 0, state
    assert len(requests) == 2  # A closed denominator must not enqueue a phantom next page.
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM processing_jobs").fetchone()[0] == 0
        rows = conn.execute(
            "SELECT p.*,e.evidence FROM checklist_pages p JOIN checklist_page_evidence e ON e.item_id=p.item_id ORDER BY p.purpose"
        ).fetchall()
        catalog = next(r for r in rows if r["purpose"] == "catalog")
        evidence = json.loads(catalog["evidence"])
        assert evidence["request"] == {
            "method": "POST",
            "url": catalog["final_url"],
            "json_body": requests[0][1],
        }
        assert evidence["scope"] == {
            "provider": "setneg",
            "jns": "UU",
            "total": 1,
            "offset": 0,
            "page_size": 1,
            "returned": 1,
        }
        saved_blob = Path(catalog["storage_path"]).read_bytes()
    # Each refresh stores a new response/request snapshot without rewriting evidence.
    discover(client, request="second-evidence")
    drain_downloads(client)
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM checklist_page_evidence").fetchone()[0] == 4
        assert (
            conn.execute(
                "SELECT evidence FROM checklist_page_evidence WHERE item_id=?", (catalog["item_id"],)
            ).fetchone()[0]
            == catalog["evidence"]
        )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE checklist_page_evidence SET evidence='{}' WHERE item_id=?", (catalog["item_id"],)
            )
    assert Path(catalog["storage_path"]).read_bytes() == saved_blob


def test_page_evidence_migrates_existing_database_without_rewriting_records(client, tmp_path):
    doc = client.post(
        "/api/tea/documents", files={"file": ("old.txt", b"keep old original", "text/plain")}
    ).json()
    service = client.app.state.service
    with service.db.transaction() as conn:
        before = dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone())
        conn.execute("DROP TABLE checklist_page_evidence")
        conn.execute("DELETE FROM schema_migrations WHERE version=11")
    restarted = Service(tmp_path)
    with closing(restarted.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=11").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM checklist_page_evidence").fetchone()[0] == 0
        assert len(conn.execute("PRAGMA table_info(checklist_pages)").fetchall()) == 10
        assert dict(conn.execute("SELECT * FROM documents WHERE id=?", (doc["id"],)).fetchone()) == before
    Service(tmp_path)
