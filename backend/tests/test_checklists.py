"""Document checklists are scoped plans, not an implicit full-site crawler."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from admin_helpers import grant_admin, login_admin
from app.bootstrap import Service
from app.interfaces.http.factory import create_app
from app.shared.domain.values import now
from app.shared.infrastructure.http import download as ingestion_download
from fastapi.testclient import TestClient

PLAN = "indonesia-20261003"
BASE = f"/api/tea/collection-plans/{PLAN}"
SG_PLAN = "singapore-demo-20261009"


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
    assert [(p["country"], p["item_count"]) for p in catalog.json()["items"]] == [("ID", 16), ("SG", 15)]
    detail = client.get(BASE).json()
    assert len(detail["plan"]["chapters"]) == 5
    assert len(detail["items"]) == 16
    assert all(i["source_id"] is None and i["coverage"] == "not_started" for i in detail["items"])
    central_regulations = next(item for item in detail["plan"]["items"] if item["id"] == "1.8")
    assert central_regulations["trusted_hosts"] == ["jdih.dephub.go.id"]
    assert any(
        entry["url"] == "https://jdih.dephub.go.id/peraturan/index?kelompok=4&page=1&per-page=12"
        for entry in central_regulations["entries"]
    )
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
    assert len(singapore["plan"]["items"]) == 15
    assert {"1.1", "1.2", "1.3"} <= {item["id"] for item in singapore["plan"]["items"]}
    assert all(item["id"] != "2.1" for item in singapore["plan"]["items"])
    assert all(item["source_id"] is None for item in singapore["items"])

    from app.contexts.ingestion.application.checklists import load_blueprint_version

    singapore_v5 = load_blueprint_version(SG_PLAN, 5)
    assert len(singapore_v5["items"]) == 16
    old_case_item = next(item for item in singapore_v5["items"] if item["id"] == "2.1")
    assert old_case_item["execution"] == "dataset_review"


def test_oversized_official_law_items_have_explicit_bounded_caps(client):
    plan = client.app.state.service.checklists.plans[PLAN]
    items = {item["id"]: item for item in plan["items"]}
    assert plan["version"] == 16
    for item_id in ("1.3", "1.4", "1.5", "1.6", "4.1"):
        assert items[item_id]["limits"]["max_original_bytes"] == 300 * 1024 * 1024
        assert items[item_id]["limits"]["download_timeout_seconds"] == 900
    for item_id in ("1.8", "1.10", "2.4", "5.1"):
        assert items[item_id]["limits"]["max_original_bytes"] == 128 * 1024 * 1024
        assert items[item_id]["limits"]["download_timeout_seconds"] == 900


def test_large_source_caps_are_frozen_into_each_collection_run(client):
    configure(client)
    with closing(client.app.state.service.db.connect()) as conn:
        for item_id in ("1.8", "5.1"):
            run = discover(client, code=item_id, request=f"bounded-original-{item_id}")
            saved = conn.execute("SELECT plan FROM collection_runs WHERE id=?", (run["id"],)).fetchone()
            plan = json.loads(saved["plan"])
            assert plan["max_original_bytes"] == 128 * 1024 * 1024
            assert plan["download_timeout_seconds"] == 900


def test_indonesia_v16_uses_reachable_sources_and_keeps_version_history(client):
    from app.contexts.ingestion.application.checklists import load_blueprint_version

    current = client.app.state.service.checklists.plans[PLAN]
    previous_v15 = load_blueprint_version(PLAN, 15)
    previous_v14 = load_blueprint_version(PLAN, 14)
    previous_v13 = load_blueprint_version(PLAN, 13)
    previous = load_blueprint_version(PLAN, 12)
    previous_v11 = load_blueprint_version(PLAN, 11)
    current_ids = {item["id"] for item in current["items"]}
    previous_v15_ids = {item["id"] for item in previous_v15["items"]}
    previous_v14_ids = {item["id"] for item in previous_v14["items"]}
    previous_v13_ids = {item["id"] for item in previous_v13["items"]}
    previous_ids = {item["id"] for item in previous["items"]}
    previous_v11_ids = {item["id"] for item in previous_v11["items"]}
    removed_v15 = {"1.1"}
    removed_v14 = {"4.7", "5.3"}
    removed_v13 = {"5.2"}
    v11_removed = {"1.2", "1.9", "2.1", "2.2", "2.3", "3.2", "3.3", "3.4", "4.5"}
    assert current["version"] == 16 and len(current_ids) == 16
    assert current["limits"]["exhaustive_scope"] is False
    assert len(previous_v15_ids) == 16 and previous_v15["version"] == 15
    previous_v15_items = {item["id"]: item for item in previous_v15["items"]}
    assert "max_original_bytes" not in previous_v15_items["1.8"]["limits"]
    assert "max_original_bytes" not in previous_v15_items["5.1"]["limits"]
    assert removed_v15.isdisjoint(current_ids) and removed_v15 <= previous_v14_ids
    assert len(previous_v14_ids) == 17 and previous_v14["version"] == 14
    assert removed_v14.isdisjoint(current_ids) and removed_v14 <= previous_v13_ids
    assert removed_v13.isdisjoint(previous_v13_ids) and removed_v13 <= previous_ids
    assert len(previous_v13_ids) == 19 and previous_v13["version"] == 13
    assert len(previous_ids) == 20 and previous["version"] == 12
    assert v11_removed.isdisjoint(previous_ids) and v11_removed <= previous_v11_ids
    assert len(previous_v11_ids) == 29

    active_urls = [entry["url"] for item in current["items"] for entry in item["entries"]]
    blocked_hosts = (
        "peraturan.bpk.go.id",
        "jdih.mahkamahagung.go.id",
        "kepaniteraan.mahkamahagung.go.id",
        "putusan3.mahkamahagung.go.id",
        "jdih.mkri.id",
        "www.mkri.id",
        "jdih.kppu.go.id",
        "jdih.lkpp.go.id",
        "rcepsec.org",
        "fta.mofcom.gov.cn",
        "www.dfat.gov.au",
    )
    assert not any(host in url for host in blocked_hosts for url in active_urls)
    current_items = {item["id"]: item for item in current["items"]}
    assert current_items["1.3"]["entries"][0]["url"].startswith("https://jdih.setneg.go.id/")
    assert current_items["1.7"]["entries"][0]["url"].startswith("https://jdih.bandung.go.id/")
    assert current_items["1.8"]["entries"][0]["url"].startswith("https://jdih.dephub.go.id/")
    assert current_items["4.6"]["adapter"] == "bi_official"
    assert current_items["4.6"]["trusted_hosts"] == ["www.bi.go.id"]
    assert current_items["4.6"]["entries"][0]["url"] == (
        "https://www.bi.go.id/id/publikasi/peraturan/default.aspx?Kategori=peraturan+bank+indonesia"
    )
    assert "jdih.bi.go.id" not in {urlsplit(url).hostname for url in active_urls}


def test_indonesia_v14_preserves_official_kemenkeu_tax_catalog_and_historical_versions(client):
    from app.contexts.ingestion.application.checklists import load_blueprint_version

    current = client.app.state.service.checklists.plans[PLAN]
    tax = next(item for item in current["items"] if item["id"] == "4.3")
    previous = load_blueprint_version(PLAN, 12)
    previous_v13 = load_blueprint_version(PLAN, 13)
    old_tax = next(item for item in previous["items"] if item["id"] == "4.3")
    assert current["version"] == 16
    assert tax["adapter"] == "kemenkeu_tax"
    assert tax["entries"][0]["url"] == (
        "https://jdih.kemenkeu.go.id/search?order=desc&teu=Direktorat+Jenderal+Pajak"
    )
    assert previous["version"] == 12
    assert {item["id"] for item in previous_v13["items"]} - {item["id"] for item in current["items"]} >= {
        "4.7",
        "5.3",
    }
    assert old_tax["entries"][0]["url"] == tax["entries"][0]["url"]
    older = load_blueprint_version(PLAN, 10)
    old_tax = next(item for item in older["items"] if item["id"] == "4.3")
    assert old_tax["entries"][0]["url"] == "https://www.pajak.go.id/id/peraturan"


def test_configuration_is_atomic_idempotent_versioned_and_private(client, tmp_path):
    assert configure(client) == {"created": 16, "existing": 0}
    assert configure(client) == {"created": 0, "existing": 16}
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
        assert conn.execute("SELECT COUNT(*) FROM checklist_bindings").fetchone()[0] == 16
        assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version=7").fetchone()[0] == 1
        row = conn.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
        assert row["enabled"]
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
    assert configure(client)["created"] == 16


def test_configuration_schedules_only_unconditional_automated_sources(client):
    configure(client)
    singapore_plan = "singapore-demo-20261009"
    client.post(
        f"/api/tea/collection-plans/{singapore_plan}/configure",
        json={
            "expected_version": client.get(f"/api/tea/collection-plans/{singapore_plan}").json()["plan"][
                "version"
            ]
        },
    ).raise_for_status()
    service = client.app.state.service
    with closing(service.db.connect()) as conn:
        rows = conn.execute(
            """SELECT b.plan_id,b.item_id,s.enabled FROM checklist_bindings b
            JOIN sources s ON s.id=b.source_id"""
        ).fetchall()
    enabled = {(row["plan_id"], row["item_id"]): bool(row["enabled"]) for row in rows}
    assert all(enabled[(PLAN, item_id)] for item_id in ("1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.10"))
    assert enabled[(PLAN, "4.6")]
    assert enabled[(singapore_plan, "3.1")]
    assert (singapore_plan, "2.1") not in enabled
    assert not enabled[(singapore_plan, "4.1")]
    assert not enabled[(singapore_plan, "4.2")]


def test_new_plan_version_retires_old_sources_and_waits_for_old_runs(client):
    service = client.app.state.service
    plan = service.checklists.plans[PLAN]
    current_version = plan["version"]
    plan["version"] = current_version - 1
    assert configure(client) == {"created": 16, "existing": 0}
    old_source = client.get(BASE).json()["items"][0]["source_id"]
    old_run = discover(client, code="1.3", request="old-version-active")

    plan["version"] = current_version
    assert configure(client) == {"created": 16, "existing": 0}
    new_source = client.get(BASE).json()["items"][0]["source_id"]
    assert new_source != old_source

    with closing(service.db.connect()) as conn:
        owner = conn.execute("SELECT owner_id FROM sources WHERE id=?", (old_source,)).fetchone()[0]
        assert conn.execute("SELECT enabled FROM sources WHERE id=?", (old_source,)).fetchone()[0] == 0
        assert conn.execute("SELECT enabled FROM sources WHERE id=?", (new_source,)).fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM checklist_bindings WHERE owner_id=? AND plan_id=? AND version=?",
                (owner, PLAN, current_version - 1),
            ).fetchone()[0]
            == 16
        )
        assert not service.collection.source_due(conn, owner, new_source)
    assert not any(row["id"] == new_source for row in service.due_sources())

    with closing(service.db.connect()) as conn:
        revision = conn.execute(
            "SELECT revision FROM collection_runs WHERE id=?", (old_run["id"],)
        ).fetchone()[0]
    cancelled = client.post(
        f"/api/tea/collection-runs/{old_run['id']}/cancel",
        json={"expected_revision": revision},
    )
    assert cancelled.status_code == 200
    with closing(service.db.connect()) as conn:
        assert service.collection.source_due(conn, owner, new_source)
    assert any(row["id"] == new_source for row in service.due_sources())


def fake_web(monkeypatch):
    catalog_url = "https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=UU&length=50&start=0"
    fixtures = Path(__file__).parent / "fixtures/discovery"
    catalog = json.loads((fixtures / "setneg-catalog.json").read_text())
    catalog["data"] = catalog["data"][:2]
    catalog["jml"] = 2
    pages = {catalog_url: json.dumps(catalog).encode()}

    def detail_for(number):
        current_catalog = json.loads(pages[catalog_url])
        entry = next(row for row in current_catalog["data"] if row["no_peraturan"] == number)
        detail = json.loads((fixtures / "setneg-detail.json").read_text())
        detail["row"][0].update(
            {
                "idperaturan": entry["idperaturan"],
                "no": number,
                "no_peraturan": number,
                "tahun": entry["tahun"],
                "tentang": entry["tentang"],
            }
        )
        filename = f"Salinan UU Nomor {number} Tahun {entry['tahun']}.pdf"
        detail["file"] = [{"name": filename, "size": 1024, "ext": ".pdf", "realName": filename}]
        detail["v"] = []
        return json.dumps(detail).encode()

    def download(url, *, consume, json_body=None, **kwargs):
        if "jdih.setneg.go.id/api/hukumproduk/produkhukum" in url:
            body = pages.get(url, pages[catalog_url])
            return {
                "url": url,
                "filename": "produkhukum.json",
                "mime_type": "application/json" if body.lstrip().startswith(b"{") else "text/html",
                "request_method": "POST",
                "blob": consume(iter([body])),
            }
        if "jdih.setneg.go.id/api/hukumproduk/detaildata" in url:
            number = parse_qs(urlsplit(url).query)["no"][0]
            body = pages.get(url, detail_for(number))
            return {
                "url": url,
                "filename": "detaildata.json",
                "mime_type": "application/json" if body.lstrip().startswith(b"{") else "text/html",
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

    monkeypatch.setattr(ingestion_download, "_download", download)
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
    assert run["pages"] == 3 and run["stored"] == 0, run
    with closing(client.app.state.service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM processing_jobs").fetchone()[0] == 0
    candidates = client.get(BASE + "/items/1.3/candidates").json()
    assert candidates["total"] == 2
    assert all(
        "/api/hukumproduk/pdf?" in c["url"] and c["title"].endswith(".pdf") and c["status"] == "pending"
        for c in candidates["items"]
    )
    item = next(i for i in client.get(BASE).json()["items"] if i["item_id"] == "1.3")
    assert item["counts"]["pages"] == 3 and item["counts"]["stored"] == 0
    assert item["coverage"] != "completed"


def test_bi_official_catalog_uses_bounded_html_and_preserves_visible_metadata(client, monkeypatch):
    root_url = "https://www.bi.go.id/id/publikasi/peraturan/default.aspx?Kategori=peraturan+bank+indonesia"
    main_url = "https://www.bi.go.id/id/publikasi/peraturan/Documents/PBI_082026.pdf"
    faq_url = "https://www.bi.go.id/id/publikasi/peraturan/Documents/FAQ-PBI_092026.pdf"
    attachment_url = "https://www.bi.go.id/id/publikasi/peraturan/Documents/Lampiran-PBI_092026.pdf"
    rows = []
    for number in range(1, 11):
        path = "PBI_092026" if number == 1 else f"PBI_{number + 10:02d}2026"
        label = (
            "Peraturan Bank Indonesia Nomor 9 Tahun 2026"
            if number == 1
            else f"Peraturan Bank Indonesia Nomor {number} Tahun 2026"
        )
        rows.append(f'<tr><td><a href="/id/publikasi/peraturan/Pages/{path}.aspx">{label}</a></td></tr>')
    catalog = (
        '<html><head><title>Peraturan Bank Indonesia</title></head><body><h1>Peraturan</h1>'
        '<p>Menampilkan 1–10 data dari total 10</p><table>'
        + "".join(rows)
        + "</table>"
        + "x" * (4 * 1024 * 1024 + 1024)
        + "</body></html>"
    ).encode()
    detail = f"""<html><head><title>Peraturan Bank Indonesia Nomor 9 Tahun 2026 tentang Pengaturan</title></head>
    <body><h1>Peraturan Bank Indonesia Nomor 9 Tahun 2026 tentang Pengaturan</h1>
      <table><tr><td>Peraturan : Peraturan Bank Indonesia Nomor 9 Tahun 2026 tentang Pengaturan</td></tr>
      <tr><td>Berlaku : sejak tanggal diundangkan</td></tr>
      <tr><td><a href="{main_url}">Peraturan Bank Indonesia Nomor 09 Tahun 2026.pdf</a></td></tr>
      <tr><td><a href="{faq_url}">Tanya Jawab Peraturan Bank Indonesia Nomor 09 Tahun 2026.pdf</a></td></tr>
      <tr><td><a href="{attachment_url}">Lampiran PBI 09 Tahun 2026.pdf</a></td></tr></table>
    </body></html>""".encode()
    downloads = []

    def download(url, *, consume, **options):
        downloads.append((url, options))
        if url == root_url:
            body, mime, filename = catalog, "text/html", "catalog.html"
        elif url.startswith("https://www.bi.go.id/id/publikasi/peraturan/Pages/"):
            body, mime, filename = detail, "text/html", url.rsplit("/", 1)[-1]
        elif url in {main_url, faq_url, attachment_url}:
            body, mime, filename = b"%PDF-1.7\nBI source PDF\n", "application/pdf", url.rsplit("/", 1)[-1]
        else:
            raise AssertionError(url)
        return {"url": url, "filename": filename, "mime_type": mime, "blob": consume(iter([body]))}

    monkeypatch.setattr(ingestion_download, "_download", download)
    discover(client, code="4.6", request="bi-official-catalog")
    drain_downloads(client)

    catalog_call = next(options for url, options in downloads if url == root_url)
    assert catalog_call["max_compressed_html_bytes"] == 6 * 1024 * 1024
    assert catalog_call["required_host"] == "www.bi.go.id"
    candidates = client.get(BASE + "/items/4.6/candidates").json()
    assert candidates["total"] == 3
    candidate = next(
        item for item in candidates["items"]
        if item["source_record"]["fields"]["attachment_role"] == "document_candidate"
    )
    assert candidate["url"] == main_url
    assert candidate["source_record"]["provider"] == "bi_official"
    assert candidate["source_record"]["fields"]["filename_identity_verified"] is False
    assert candidate["source_record"]["fields"]["page_metadata"]["Berlaku"] == "sejak tanggal diundangkan"


def test_setneg_source_fields_keep_page_hash_and_are_exposed_as_unreviewed(client):
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import discover_page, recipe_for

    configure(client)
    run = discover(client, request="setneg-structured-source-record")
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    with closing(service.db.connect()) as conn:
        claim = conn.execute(
            """SELECT i.*,r.plan FROM collection_items i JOIN collection_runs r ON r.id=i.run_id
            WHERE i.run_id=? AND i.host='jdih.setneg.go.id' ORDER BY i.id LIMIT 1""",
            (run["id"],),
        ).fetchone()
    assert claim
    claim = dict(claim)
    claim["plan"] = json.loads(claim["plan"])
    body = (Path(__file__).parent / "fixtures/discovery/setneg-detail.json").read_bytes()
    detail_url = "https://jdih.setneg.go.id/api/hukumproduk/detaildata?jns=UU&no=2&thn=2026"
    result = discover_page(
        detail_url,
        body,
        "application/json",
            recipe_for(next(item for item in service.checklists.plans[PLAN]["items"] if item["id"] == "1.3")),
    )
    blob = service.archive.blobs.put_bytes(owner, body)
    fetched = {
        "url": detail_url,
        "filename": "detaildata.json",
        "mime_type": "application/json",
        "blob": blob,
        "request_evidence": {"method": "POST", "url": detail_url},
    }
    with service.db.transaction() as conn:
        service.checklists.save_page(conn, claim, fetched, claim["plan"], result)

    candidates = client.get(BASE + "/items/1.3/candidates").json()["items"]
    candidate = next(item for item in candidates if "pdf?f=Salinan+UU+Nomor+2" in item["url"])
    source = candidate["source_record"]
    assert source["provider"] == "setneg"
    assert source["interpretation"] == "unreviewed_source_values"
    assert source["fields"]["nama_jenis"] == "Undang-Undang"
    assert source["fields"]["diundangkan"] == "2026-04-30T00:00:00.000Z"
    assert source["fields"]["status"] == "Disahkan"
    assert source["evidence"] == {"url": detail_url, "sha256": blob.digest}
    with service.db.transaction() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="candidate source records are immutable"):
            conn.execute(
                "UPDATE checklist_candidate_source_records SET source_record='{}' WHERE candidate_id=?",
                (candidate["id"],),
            )


def test_candidate_source_record_migration_upgrades_an_existing_database(tmp_path):
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        conn.execute("DROP TRIGGER candidate_source_record_immutable_update")
        conn.execute("DROP TRIGGER candidate_source_record_immutable_delete")
        conn.execute("DROP TABLE checklist_candidate_source_records")
        conn.execute("DELETE FROM schema_migrations WHERE version=22")

    upgraded = Service(tmp_path)
    with closing(upgraded.db.connect()) as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(checklist_candidate_source_records)")
        }
        assert columns == {
            "candidate_id",
            "source_record",
            "source_page_url",
            "source_page_sha256",
            "created_at",
        }
        assert conn.execute("SELECT 1 FROM schema_migrations WHERE version=22").fetchone()


def test_saved_setneg_detail_backfill_requires_matching_candidate_and_page_hash(client):
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import discover_page, recipe_for

    configure(client)
    run = discover(client, request="setneg-source-record-backfill")
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    with closing(service.db.connect()) as conn:
        claim = conn.execute(
            """SELECT i.*,r.plan FROM collection_items i JOIN collection_runs r ON r.id=i.run_id
            WHERE i.run_id=? AND i.host='jdih.setneg.go.id' ORDER BY i.id LIMIT 1""",
            (run["id"],),
        ).fetchone()
    assert claim
    claim = dict(claim)
    claim["plan"] = json.loads(claim["plan"])
    body = (Path(__file__).parent / "fixtures/discovery/setneg-detail.json").read_bytes()
    detail_url = "https://jdih.setneg.go.id/api/hukumproduk/detaildata?jns=UU&no=2&thn=2026"
    result = discover_page(
        detail_url,
        body,
        "application/json",
            recipe_for(next(item for item in service.checklists.plans[PLAN]["items"] if item["id"] == "1.3")),
    )
    for link in result["links"]:
        link.pop("source_record", None)
    blob = service.archive.blobs.put_bytes(owner, body)
    fetched = {
        "url": detail_url,
        "filename": "detaildata.json",
        "mime_type": "application/json",
        "blob": blob,
        "request_evidence": {"method": "POST", "url": detail_url},
    }
    with service.db.transaction() as conn:
        service.checklists.save_page(conn, claim, fetched, claim["plan"], result)

    candidate = client.get(BASE + "/items/1.3/candidates").json()["items"][0]
    assert candidate["source_record"] is None
    recovered = service.checklists.backfill_setneg_candidate_source_records(limit=10)
    assert recovered == {"scanned": 1, "verified": 1, "inserted": 1, "skipped": 0}
    candidate = next(
        item
        for item in client.get(BASE + "/items/1.3/candidates").json()["items"]
        if item["id"] == candidate["id"]
    )
    assert candidate["source_record"]["evidence"] == {"url": detail_url, "sha256": blob.digest}


def test_saved_bkpm_catalog_backfill_preserves_verified_row_metadata(client):
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import discover_page, recipe_for

    configure(client)
    run = discover(client, code="4.1", request="bkpm-source-record-backfill")
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    with closing(service.db.connect()) as conn:
        claim = conn.execute(
            """SELECT i.*,r.plan FROM collection_items i JOIN collection_runs r ON r.id=i.run_id
            WHERE i.run_id=? AND i.host='jdih.bkpm.go.id' ORDER BY i.id LIMIT 1""",
            (run["id"],),
        ).fetchone()
    assert claim
    claim = dict(claim)
    claim["plan"] = json.loads(claim["plan"])
    body = (Path(__file__).parent / "fixtures/discovery/bkpm-investment.json").read_bytes()
    catalog_url = "https://jdih.bkpm.go.id/api/document-search?id_theme=1&limit=2&page=1"
    item = next(entry for entry in service.checklists.plans[PLAN]["items"] if entry["id"] == "4.1")
    result = discover_page(catalog_url, body, "application/json", recipe_for(item))
    for link in result["links"]:
        link.pop("source_record", None)
    blob = service.archive.blobs.put_bytes(owner, body)
    fetched = {
        "url": catalog_url,
        "filename": "document-search.json",
        "mime_type": "application/json",
        "blob": blob,
        "request_evidence": {"method": "POST", "url": catalog_url},
    }
    with service.db.transaction() as conn:
        service.checklists.save_page(conn, claim, fetched, claim["plan"], result)

    candidates = client.get(BASE + "/items/4.1/candidates").json()["items"]
    candidate = next(item for item in candidates if "2026Kepdepinvesthil15.S-.pdf" in item["url"])
    assert candidate["source_record"] is None
    recovered = service.checklists.backfill_candidate_source_records(providers={"bkpm"})
    assert recovered == {"scanned": 2, "verified": 2, "inserted": 2, "skipped": 0}
    candidate = next(
        item
        for item in client.get(BASE + "/items/4.1/candidates").json()["items"]
        if item["id"] == candidate["id"]
    )
    assert candidate["source_record"]["provider"] == "bkpm"
    assert candidate["source_record"]["fields"]["regulation_number"] == "15.S"
    assert candidate["source_record"]["evidence"] == {"url": catalog_url, "sha256": blob.digest}


def test_saved_kemenkeu_tax_catalog_backfill_preserves_instrument_identity(client):
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import discover_page, recipe_for

    configure(client)
    run = discover(client, code="4.3", request="kemenkeu-tax-source-record-backfill")
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    with closing(service.db.connect()) as conn:
        claim = conn.execute(
            """SELECT i.*,r.plan FROM collection_items i JOIN collection_runs r ON r.id=i.run_id
            WHERE i.run_id=? AND i.host='jdih.kemenkeu.go.id' ORDER BY i.id LIMIT 1""",
            (run["id"],),
        ).fetchone()
    assert claim
    claim = dict(claim)
    claim["plan"] = json.loads(claim["plan"])
    item = next(entry for entry in service.checklists.plans[PLAN]["items"] if entry["id"] == "4.3")
    detail_url = "https://jdih.kemenkeu.go.id/dok/per-6pj2026/files"
    body = b"""<html><head><title>PER-6/PJ/2026 - Official tax regulation | JDIH Kementerian Keuangan</title></head><body>
      <div class="breadcrumb-inner"><div class="page-title"><h1>PER-6/PJ/2026</h1></div>
      <ul class="dok-meta"><li>Direktorat Jenderal Pajak</li><li>01 Januari 2026</li><li>Berlaku</li></ul></div>
      <div class="dok-file-list"><div class="justify-content-between d-flex"><div>
      <a href="/api/download/19e80404-0ce8-48b1-8938-31530ada8701/PER-6%20PJ%202026.pdf">Fulltext PDF (Indonesia)</a>
      </div><span>1 MB</span></div></div></body></html>"""
    result = discover_page(detail_url, body, "text/html", recipe_for(item))
    for link in result["links"]:
        link.pop("source_record", None)
    blob = service.archive.blobs.put_bytes(owner, body)
    fetched = {
        "url": detail_url,
        "filename": "djp-files.html",
        "mime_type": "text/html",
        "blob": blob,
        "request_evidence": {"method": "GET", "url": detail_url},
    }
    with service.db.transaction() as conn:
        service.checklists.save_page(conn, claim, fetched, claim["plan"], result)

    candidate = client.get(BASE + "/items/4.3/candidates").json()["items"][0]
    assert candidate["source_record"] is None
    recovered = service.checklists.backfill_candidate_source_records(providers={"jdih_kemenkeu"})
    assert recovered == {"scanned": 1, "verified": 1, "inserted": 1, "skipped": 0}
    candidate = next(
        row
        for row in client.get(BASE + "/items/4.3/candidates").json()["items"]
        if row["id"] == candidate["id"]
    )
    assert candidate["source_record"]["provider"] == "jdih_kemenkeu"
    assert candidate["source_record"]["fields"]["document_code"] == "PER-6/PJ/2026"
    assert candidate["source_record"]["evidence"] == {"url": detail_url, "sha256": blob.digest}


def test_retired_rcep_catalog_remains_available_in_immutable_plan_history(client):
    from app.contexts.ingestion.application.checklists import load_blueprint_version
    from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import discover_page, recipe_for

    current = client.app.state.service.checklists.plans[PLAN]
    archived = load_blueprint_version(PLAN, 8)
    previous = load_blueprint_version(PLAN, 9)
    current_sources = [
        entry["url"] for item in current["items"] if item["id"] == "5.3" for entry in item["entries"]
    ]
    historical_item = next(item for item in archived["items"] if item["id"] == "5.3")
    previous_central = next(item for item in previous["items"] if item["id"] == "1.8")
    assert previous["version"] == 9
    assert "jdih.dephub.go.id" not in previous_central["trusted_hosts"]
    assert not any("rcepsec.org" in url for url in current_sources)
    assert any("rcepsec.org" in entry["url"] for entry in historical_item["entries"])
    body = b"""<html><title>RCEP Agreements</title><body><ul class="rcep-list">
    <li class="rcep-item"><a class="rcep-link" onclick="loadPdf(this, 'https://rcepsec.org/wp-content/uploads/2026/06/Chapter-3.pdf')">Chapter 3: Rules of Origin</a>
    <ul><li><a class="rcep-link rcep-sub-link" onclick="loadPdf(this, 'https://rcepsec.org/wp-content/uploads/2026/06/Chapter-3-Annex-3A.pdf')">Annex 3A: Product-Specific Rules</a></li></ul></li>
    </ul></body></html>"""
    page_url = "https://rcepsec.org/rcep-agreements/"
    result = discover_page(page_url, body, "text/html", recipe_for(historical_item))
    candidate = next(link for link in result["links"] if "Chapter-3-Annex-3A.pdf" in link["url"])
    assert candidate["source_record"]["provider"] == "rcepsec"
    assert candidate["source_record"]["structure"]["parent_titles"] == ["Chapter 3: Rules of Origin"]


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
    with closing(client.app.state.service.db.connect()) as conn:
        run_plan = json.loads(
            conn.execute("SELECT plan FROM collection_runs WHERE id=?", (response.json()["id"],)).fetchone()[
                0
            ]
        )
    assert run_plan["max_original_bytes"] == 300 * 1024 * 1024
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


def test_bkpm_multipart_original_is_recovered_during_collection(client, monkeypatch):
    import hashlib

    configure(client)
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    url = "https://jdih-storage.bkpm.go.id/jdih/jdih/2026Kepdepinvesthil15.S-.pdf"
    pdf = b"%PDF-1.7\nverified official original\n%%EOF\r\n"
    boundary = b"----WebKitFormBoundaryAbc123"
    envelope = (
        b"--" + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="law.pdf"'
        b"\r\nContent-Type: application/pdf\r\n\r\n"
        + pdf
        + b"\r\n--"
        + boundary
        + b'\r\nContent-Disposition: form-data; name="customPath"\r\n\r\noriginal/archive'
        b"\r\n--" + boundary + b"--\r\n"
    )
    with service.db.transaction() as conn:
        binding = conn.execute(
            "SELECT id FROM checklist_bindings WHERE owner_id=? AND plan_id=? AND item_id='4.1'",
            (owner, PLAN),
        ).fetchone()
        conn.execute(
            """INSERT INTO checklist_candidates
            (id,binding_id,url,title,identity_key,discovered_from,status,created_at)
            VALUES('bkpm-multipart-candidate',?,?,?,NULL,?,'pending',?)""",
            (
                binding["id"],
                url,
                "Investment regulation",
                "https://jdih.bkpm.go.id/id/search",
                now(),
            ),
        )
    response = client.post(
        BASE + "/items/4.1/collect",
        json={
            "client_request_id": "bkpm-multipart-original",
            "candidate_ids": ["bkpm-multipart-candidate"],
            "level": "primary",
            "primary_document_ids": [],
        },
    )
    assert response.status_code == 202, response.text

    def download(source_url, *, consume, **kwargs):
        return {
            "url": source_url,
            "filename": source_url.rsplit("/", 1)[-1],
            "mime_type": "application/pdf",
            "blob": consume(iter([envelope])),
            "etag": '"envelope-v1"',
            "last_modified": "Sat, 24 Oct 2026 00:00:00 GMT",
        }

    monkeypatch.setattr(ingestion_download, "_download", download)
    claim = service.collection.claim_fetch()
    assert claim and claim["url"] == url
    service.collection.fetch_item(claim)

    with closing(service.db.connect()) as conn:
        item = conn.execute(
            "SELECT status,document_id FROM collection_items WHERE id=?", (claim["id"],)
        ).fetchone()
        doc = conn.execute("SELECT * FROM documents WHERE id=?", (item["document_id"],)).fetchone()
        cached = conn.execute(
            "SELECT etag,last_modified FROM collection_http_cache WHERE owner_id=? AND url=?",
            (owner, url),
        ).fetchone()
    assert item["status"] == "stored"
    assert doc["mime_type"] == "application/pdf" and Path(doc["storage_path"]).read_bytes() == pdf
    manifest = json.loads(doc["manifest"])
    source_response = manifest["source_response"]
    assert source_response["sha256"] == hashlib.sha256(envelope).hexdigest()
    assert Path(source_response["cas_path"]).read_bytes() == envelope
    assert source_response["extraction"]["protocol"] == "bkpm-multipart-pdf-v1"
    assert cached["etag"] is None and cached["last_modified"] is None


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
    pages["https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=UU&length=50&start=0"] = (
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
    from app.contexts.ingestion.application import checklists as checklists

    monkeypatch.setattr(checklists, "PAGE_BATCH", 1)
    fake_web(monkeypatch)
    first = discover(client)
    drain_downloads(client)
    progress = client.get(f"/api/tea/collection-runs/{first['id']}").json()["run"]
    assert progress["pages"] == 1 and progress["total"] == 1
    state = next(x for x in client.get(BASE).json()["items"] if x["item_id"] == "1.3")
    assert state["counts"]["remaining_pages"] > 0
    service = Service(tmp_path)
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    second = service.checklists.start(
        owner, PLAN, "1.3", {"client_request_id": "resume", "confirm_condition": False}, kind="discovery"
    )
    drain_downloads(client)
    assert second["id"] != first["id"]
    third = service.checklists.start(
        owner, PLAN, "1.3", {"client_request_id": "resume-final", "confirm_condition": False}, kind="discovery"
    )
    drain_downloads(client)
    assert third["id"] != second["id"]
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM checklist_pages").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(DISTINCT url) FROM checklist_pages").fetchone()[0] == 3
    assert client.get(BASE + "/items/1.3/candidates").json()["total"] == 2


def test_scheduled_checklist_collects_pending_candidates_without_manual_request(client, tmp_path):
    configure(client)
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    with service.db.transaction() as conn:
        binding = conn.execute(
            "SELECT * FROM checklist_bindings WHERE owner_id=? AND plan_id=? AND item_id='1.3'",
            (owner, PLAN),
        ).fetchone()
        conn.execute(
            "UPDATE sources SET enabled=1,last_attempt_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (binding["source_id"],),
        )
        conn.execute(
            """INSERT INTO checklist_candidates
            (id,binding_id,url,title,identity_key,discovered_from,status,created_at)
            VALUES('scheduled-candidate',?,?,?,NULL,?,'pending','2026-01-01T00:00:00+00:00')""",
            (
                binding["id"],
                "https://peraturan.bpk.go.id/Details/123/fixture",
                "Fixture regulation",
                "https://peraturan.bpk.go.id/Search?jenis=8",
            ),
        )

    run = service.collection.enqueue_scheduled(owner, binding["source_id"])
    assert run["kind"] == "collection"
    assert run["total"] == 1
    with closing(service.db.connect()) as conn:
        queued = conn.execute(
            "SELECT status FROM checklist_candidates WHERE id='scheduled-candidate'"
        ).fetchone()[0]
        kind = conn.execute(
            "SELECT json_extract(plan,'$.checklist.kind') FROM collection_runs WHERE id=?", (run["id"],)
        ).fetchone()[0]
    assert queued == "queued" and kind == "collection"


def test_new_candidates_bypass_poll_interval_once_but_failed_fetches_keep_backoff(client):
    configure(client)
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    with service.db.transaction() as conn:
        binding = conn.execute(
            "SELECT * FROM checklist_bindings WHERE owner_id=? AND plan_id=? AND item_id='1.3'",
            (owner, PLAN),
        ).fetchone()
        conn.execute(
            "UPDATE sources SET enabled=1,interval_hours=24,last_attempt_at=? WHERE id=?",
            (now(), binding["source_id"]),
        )
        conn.execute(
            """INSERT INTO checklist_candidates
            (id,binding_id,url,title,identity_key,discovered_from,status,created_at)
            VALUES('new-scheduled-candidate',?,?,?,NULL,?,'pending',?)""",
            (
                binding["id"],
                "https://peraturan.bpk.go.id/Details/456/fixture",
                "New fixture regulation",
                "https://peraturan.bpk.go.id/Search?jenis=8",
                now(),
            ),
        )

    with closing(service.db.connect()) as conn:
        assert service.collection.source_due(conn, owner, binding["source_id"])
    assert any(row["id"] == binding["source_id"] for row in service.due_sources())
    run = service.collection.enqueue_scheduled(owner, binding["source_id"])
    assert run["kind"] == "collection" and run["total"] == 1
    with service.db.transaction() as conn:
        item = conn.execute(
            "SELECT id FROM collection_items WHERE candidate_id='new-scheduled-candidate'"
        ).fetchone()
        conn.execute("UPDATE collection_items SET status='failed' WHERE id=?", (item["id"],))
        conn.execute("UPDATE collection_runs SET status='partial' WHERE id=?", (run["id"],))
        conn.execute("UPDATE checklist_candidates SET status='pending' WHERE id='new-scheduled-candidate'")
        assert not service.collection.source_due(conn, owner, binding["source_id"])
    assert not any(row["id"] == binding["source_id"] for row in service.due_sources())


def test_unattempted_conditional_candidates_do_not_bypass_poll_interval(client):
    plan_url = f"/api/tea/collection-plans/{SG_PLAN}"
    response = client.post(
        plan_url + "/configure",
        json={"expected_version": client.get(plan_url).json()["plan"]["version"]},
    )
    assert response.status_code == 200, response.text
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    with service.db.transaction() as conn:
        binding = conn.execute(
            "SELECT * FROM checklist_bindings WHERE owner_id=? AND plan_id=? AND item_id='4.1'",
            (owner, SG_PLAN),
        ).fetchone()
        conn.execute(
            "UPDATE sources SET enabled=1,interval_hours=24,last_attempt_at=? WHERE id=?",
            (now(), binding["source_id"]),
        )
        conn.execute(
            """INSERT INTO checklist_candidates
            (id,binding_id,url,title,identity_key,discovered_from,status,created_at)
            VALUES('conditional-scheduled-candidate',?,?,?,NULL,?,'pending',?)""",
            (
                binding["id"],
                "https://www.judiciary.gov.sg/hearings/court-hearings",
                "Conditional fixture",
                "https://www.judiciary.gov.sg/",
                now(),
            ),
        )

    with closing(service.db.connect()) as conn:
        assert not service.collection.source_due(conn, owner, binding["source_id"])
    assert not any(row["id"] == binding["source_id"] for row in service.due_sources())


def test_scheduled_checklist_rechecks_stored_candidate_after_catalog_poll(client, tmp_path):
    configure(client)
    service = client.app.state.service
    owner = client.get("/api/tea/bootstrap").json()["user"]["id"]
    with service.db.connect() as conn:
        binding = conn.execute(
            "SELECT * FROM checklist_bindings WHERE owner_id=? AND plan_id=? AND item_id='1.3'",
            (owner, PLAN),
        ).fetchone()
    previous = service.checklists.start(
        owner,
        PLAN,
        "1.3",
        {"client_request_id": "scheduled-discovery-prior", "confirm_condition": False},
        kind="discovery",
    )
    with service.db.transaction() as conn:
        conn.execute("UPDATE collection_runs SET status='completed' WHERE id=?", (previous["id"],))
        conn.execute(
            "UPDATE sources SET enabled=1,last_attempt_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (binding["source_id"],),
        )
        conn.execute(
            """INSERT INTO checklist_candidates
            (id,binding_id,url,title,identity_key,discovered_from,status,created_at)
            VALUES('stored-candidate',?,?,?,NULL,?,'stored','2026-01-01T00:00:00+00:00')""",
            (
                binding["id"],
                "https://peraturan.bpk.go.id/Details/123/fixture",
                "Fixture regulation",
                "https://peraturan.bpk.go.id/Search?jenis=8",
            ),
        )

    run = service.collection.enqueue_scheduled(owner, binding["source_id"])
    assert run["kind"] == "collection" and run["total"] == 1
    with closing(service.db.connect()) as conn:
        candidate = conn.execute(
            "SELECT status FROM checklist_candidates WHERE id='stored-candidate'"
        ).fetchone()[0]
    assert candidate == "queued"


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
            conn.execute("SELECT snapshot FROM checklist_bindings WHERE item_id='1.3'").fetchone()[0]
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
    assert batch["started"] == 16 and len(batch["runs"]) == 16
    repeated = client.post(BASE + "/discover", json={"client_request_id": "country-batch"}).json()
    assert {run["id"] for run in repeated["runs"]} == {run["id"] for run in batch["runs"]}
    assert client.post(BASE + "/discover", json={"client_request_id": "other-batch"}).json()["started"] == 0


def test_fresh_discovery_revisits_completed_catalog_pages_for_new_laws(client, monkeypatch):
    pages = fake_web(monkeypatch)
    discover(client)
    drain_downloads(client)
    assert client.get(BASE + "/items/1.3/candidates").json()["total"] == 2
    catalog_url = "https://jdih.setneg.go.id/api/hukumproduk/produkhukum?jns=UU&length=50&start=0"
    catalog = json.loads(pages[catalog_url])
    latest = json.loads((Path(__file__).parent / "fixtures/discovery/setneg-catalog.json").read_text())
    catalog["data"].append(latest["data"][2])
    catalog["jml"] = 3
    pages[catalog_url] = json.dumps(catalog).encode()
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


def test_business_search_reports_unavailable_without_ragflow_and_rejects_overrides(client):
    case = client.get("/api/tea/bootstrap").json()["cases"][0]
    payload = {"case_id": case["id"], "query": "tea"}
    response = client.post("/api/tea/search", json=payload)
    assert response.status_code == 200
    assert response.json()["mode"] == "unavailable"
    assert response.json()["items"] == []
    for country in ("SG", "unassigned"):
        assert client.post("/api/tea/search", json=payload | {"country": country}).status_code == 422
        assert client.get(f"/api/tea/documents?case_id={case['id']}&country={country}").status_code == 422
    assert client.post("/api/tea/search", json={"query": "tea"}).json()["items"] == []


@pytest.mark.parametrize(
    "code,kind,url",
    [
        (
            "3.1",
            "case",
            "https://s.mkri.id/public/content/persidangan/putusan/putusan_mkri_14426_1781684026.pdf",
        ),
        ("4.1", "guidance", "https://jdih.bkpm.go.id/uploads/guide.pdf"),
        ("5.4", "law", "https://www.wto.org/english/docs_e/legal_e/wtoagreement.pdf"),
    ],
)
def test_checklist_document_type_is_only_a_draft_hint(client, monkeypatch, code, kind, url):
    from app.shared.domain.values import now, uid

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
    from app.shared.infrastructure.http import download as ingestion_download

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

    monkeypatch.setattr(ingestion_download, "_download", download)
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
