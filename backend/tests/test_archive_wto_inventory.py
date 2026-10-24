"""WTO export has source/account boundaries and never infers legal eligibility."""

import hashlib
import json
from copy import deepcopy
from urllib.parse import urlsplit

import pytest
from app.bootstrap import Service
from app.contexts.collection.application.checklists import ChecklistService
from app.contexts.collection.infrastructure.discovery.discovery import _canonical
from app.contexts.collection.infrastructure.discovery.wto_discovery import (
    _UNVERIFIED_MDB,
    GOODS,
    MEMBERS,
    services_catalog_url,
)
from test_archive_inventory import AT, export, page, records
from test_wto_discovery import browser, goods_catalog, inspect, xml_catalog


@pytest.fixture
def archive(tmp_path):
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        for owner in ("owner-a", "owner-b"):
            conn.execute("INSERT INTO users(id,name,is_demo,created_at) VALUES(?,?,0,?)", (owner, owner, AT))
    return service


def wto_page(archive, *, country="ID", kind="services", mutate=None, original_url=None, original_body=None):
    page(archive, country=country)
    info = MEMBERS[country]
    if kind == "services":
        url, body, mime = services_catalog_url(country, "SC"), xml_catalog(country), "text/xml"
    elif kind == "goods":
        url, body, mime = f"https://{GOODS}/member/{info['slug']}", goods_catalog(country), "text/html"
    elif kind == "browser":
        url, body, mime = (
            f"https://{GOODS}/browse/39503/file_browser",
            browser(country, extension="xls"),
            "text/html",
        )
    else:
        url, body, mime = (
            f"https://{GOODS}/member/download/{info['node']}",
            b"PK\x03\x04private bundle bytes",
            "application/zip",
        )
    url, body = original_url or url, original_body or body
    url = _canonical(url)[0]
    result = inspect(url, body, country, mime)
    assert result["matched"], result
    blob = archive.archive.blobs.put_bytes("owner-a", body)
    evidence = {"request": {"method": "GET", "url": url}, "scope": deepcopy(result["scope"])}
    if mutate:
        mutate(evidence)
    with archive.db.transaction() as conn:
        conn.execute(
            "UPDATE checklist_bindings SET plan_id=?,version=?,item_id=? WHERE id='binding'",
            (
                "indonesia-20261003" if country == "ID" else "singapore-demo-20261009",
                6 if country == "ID" else 3,
                "5.4" if country == "ID" else "5.2",
            ),
        )
        conn.execute(
            "UPDATE collection_items SET url=?,host=?,status='discovered',purpose=? WHERE id='page-item'",
            (url, urlsplit(url).hostname, result["purpose"]),
        )
        conn.execute(
            "UPDATE checklist_pages SET url=?,final_url=?,content_hash=?,storage_path=?,size=?,purpose=? WHERE item_id='page-item'",
            (url, url, blob.digest, str(blob.path), blob.size, result["purpose"]),
        )
        conn.execute("INSERT INTO checklist_page_evidence VALUES('page-item',?)", (json.dumps(evidence),))
    return blob, evidence, result


@pytest.mark.parametrize("country", ["ID", "SG"])
def test_services_export_keeps_real_symbol_file_and_private_source_chain(archive, country):
    blob, evidence, _ = wto_page(archive, country=country)
    report = export(archive, include_text=True)
    row = records(report, country=country, filename="catalog-documents.jsonl")[0]
    assert row["provider"] == "wto-docs-public"
    assert row["source_hash"] == blob.digest
    assert row["source_page_id"] == "page-item"
    assert row["source_url"] == evidence["request"]["url"]
    assert (
        row["source_record_sha256"]
        == hashlib.sha256(
            json.dumps(
                evidence["scope"]["documents"][0], ensure_ascii=True, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    assert row["source_metadata"]["CONCERNEDCOUNTRIES"] == MEMBERS[country]["name"]
    assert row["symbol"].endswith("/Suppl.4")
    assert row["originals"][0]["filename"].endswith("S4.pdf")
    assert row["export_status"] == "exported" and not row["retrieval_eligible"]
    assert row["legal_review_status"] == "not_reviewed"
    assert report["summary"]["catalog_documents"] == 1
    assert report["summary"]["catalog_document_gaps"] == 0
    assert report["summary"]["anomalies"] == 0
    scope = records(report, country=country, filename="discovery-pages.jsonl")[0]["evidence"]["scope"]
    assert scope["member"] == country and scope["language"] == "English"
    assert scope["source_sha256"] == blob.digest
    assert not scope["legal_status_inferred"] and not scope["itip_exports_verified"]


@pytest.mark.parametrize("kind", ["goods", "browser"])
def test_goods_export_keeps_historical_status_sheet_format_languages_and_aliases(archive, kind):
    wto_page(archive, kind=kind)
    report = export(archive)
    rows = records(report, filename="catalog-documents.jsonl")
    assert all(row["schedule"] == "XXI" and row["schedule_number"] == 21 for row in rows)
    assert all(not row["retrieval_eligible"] for row in rows)
    assert report["summary"]["catalog_document_gaps"] == 0
    if kind == "goods":
        assert len(rows) == 1 and rows[0]["status"] == "Unknown status"
        assert rows[0]["file_browser_urls"][0].endswith("/file_browser")
    else:
        assert len(rows) == 2 and [row["language"] for row in rows] == ["English", "French"]
        assert all(row["source_format"] == "xls" for row in rows)
        assert rows[0]["original_url"] == rows[1]["original_url"]
        assert all(not row["representation_equivalence_verified"] for row in rows)


def test_bundle_export_stays_a_raw_discovery_asset_without_catalog_documents(archive):
    blob, _, _ = wto_page(archive, kind="bundle")
    report = export(archive)
    assert not records(report, filename="catalog-documents.jsonl")
    row = records(report, filename="discovery-pages.jsonl")[0]
    assert row["content_hash"] == blob.digest and row["purpose"] == "catalog"
    assert row["evidence"]["scope"]["bundle_members_verified"] is False
    assert report["summary"]["documents"] == report["summary"]["catalog_documents"] == 0


def test_wto_projection_omits_unknown_private_fields_and_headers(archive):
    def mutate(value):
        value["request"]["headers"] = {"Authorization": "DO_NOT_EXPORT_KEY"}
        value["scope"]["documents"][0]["source_metadata"]["api_key"] = "DO_NOT_EXPORT_KEY"
        value["scope"]["documents"][0]["debug"] = "DO_NOT_EXPORT_KEY"

    wto_page(archive, mutate=mutate)
    report = export(archive)
    row = records(report, filename="catalog-documents.jsonl")[0]
    assert row["export_status"] == "exported"
    assert "wto_metadata_fields_omitted" in row["anomalies"]
    for filename in ("catalog-documents.jsonl", "discovery-pages.jsonl"):
        assert "DO_NOT_EXPORT_KEY" not in json.dumps(records(report, filename=filename))


@pytest.mark.parametrize(
    "failure", ["source_hash", "member", "family", "language", "count", "method", "request_url"]
)
def test_bad_page_source_mapping_exports_a_gap_instead_of_usable_records(archive, failure):
    def mutate(value):
        if failure == "source_hash":
            value["scope"]["source_sha256"] = "0" * 64
        elif failure == "member":
            value["scope"]["member"] = "SG"
        elif failure == "family":
            value["scope"]["family"] = "GATS/SC/76"
        elif failure == "language":
            value["scope"]["language"] = "French"
        elif failure == "count":
            value["scope"]["total"] = 100
        elif failure == "method":
            value["request"]["method"] = "POST"
        else:
            value["request"]["url"] = "https://docs.wto.org/login"

    wto_page(archive, mutate=mutate)
    report = export(archive)
    rows = records(report, filename="catalog-documents.jsonl")
    assert len(rows) == 1 and rows[0]["export_status"] == "invalid_page"
    assert not rows[0]["retrieval_eligible"]
    assert report["summary"]["catalog_documents"] == 0
    assert report["summary"]["catalog_document_gaps"] == 1


@pytest.mark.parametrize("failure", ["file", "symbol", "country", "missing_original"])
def test_bad_original_mapping_cannot_be_exported_as_a_valid_service_record(archive, failure):
    def mutate(value):
        document = value["scope"]["documents"][0]
        if failure == "file":
            document["originals"][0]["filename"] = "Q:/SCHD/GATS-SC/SC76S4.pdf"
        elif failure == "symbol":
            document["symbol"] = document["source_metadata"]["SYMBOL"] = "GATS/SC/43/Suppl.3"
        elif failure == "country":
            document["source_metadata"]["CONCERNEDCOUNTRIES"] = "Singapore"
        else:
            document["originals"] = []
            document["missing_original"] = True

    wto_page(archive, mutate=mutate)
    report = export(archive)
    row = records(report, filename="catalog-documents.jsonl")[0]
    assert row["export_status"] == "invalid_record"
    assert report["summary"]["catalog_document_gaps"] == 1


def test_duplicate_service_records_are_explicit_gaps_and_not_extra_coverage(archive):
    def mutate(value):
        value["scope"]["documents"].append(deepcopy(value["scope"]["documents"][0]))
        value["scope"]["total"] = value["scope"]["returned"] = 2

    wto_page(archive, mutate=mutate)
    report = export(archive)
    rows = records(report, filename="catalog-documents.jsonl")
    assert [row["export_status"] for row in rows] == ["exported", "invalid_record"]
    assert report["summary"]["catalog_documents"] == 1
    assert report["summary"]["catalog_document_gaps"] == 1


@pytest.mark.parametrize("failure", ["plan", "version", "item", "owner", "source", "cas"])
def test_an_adapter_name_cannot_bypass_binding_account_or_frozen_item_boundaries(archive, failure):
    wto_page(archive)
    with archive.db.transaction() as conn:
        if failure == "plan":
            conn.execute("UPDATE checklist_bindings SET plan_id='singapore-demo-20261009'")
        elif failure == "version":
            conn.execute("UPDATE checklist_bindings SET version=5")
        elif failure == "item":
            conn.execute("UPDATE checklist_bindings SET item_id='5.1'")
        elif failure == "owner":
            conn.execute("UPDATE collection_runs SET owner_id='owner-b'")
        elif failure == "source":
            conn.execute(
                "INSERT INTO sources(id,owner_id,name,url,enabled,interval_hours,tags,status,created_at,updated_at) VALUES('other-source','owner-a','Other','https://official.example/other',0,24,'[]','idle',?,?)",
                (AT, AT),
            )
            conn.execute("UPDATE collection_runs SET source_id='other-source'")
        else:
            conn.execute("UPDATE checklist_pages SET storage_path=storage_path||'-wrong'")
    report = export(archive)
    assert report["summary"]["catalog_documents"] == 0
    row = records(report, filename="catalog-documents.jsonl")[0]
    assert row["export_status"] == "invalid_page"


def pending_candidate(archive, *, mutate=None):
    url, known = next(iter(_UNVERIFIED_MDB.items()))
    source_body = browser(path=url, extension="mdb")
    blob, _, _ = wto_page(
        archive, kind="browser", original_url=known["catalog_url"], original_body=source_body, mutate=mutate
    )
    with archive.db.transaction() as conn:
        conn.execute(
            "INSERT INTO checklist_candidates(id,binding_id,url,title,discovered_from,created_at) VALUES('candidate','binding',?,'Historical MDB',?,?)",
            (url, known["catalog_url"], AT),
        )
    claim = {"owner_id": "owner-a", "candidate_id": "candidate", "url": url}
    plan = {"checklist": {"binding_id": "binding", "kind": "collection"}}
    result = {
        "scope": {
            "member": "ID",
            "pending_format_verification": True,
            "catalog_url": known["catalog_url"],
            "previous_verified_source_hash": known["catalog_sha256"],
        }
    }
    return claim, plan, result, blob


def test_pending_format_original_keeps_current_and_previous_catalog_hashes(archive):
    claim, plan, result, blob = pending_candidate(archive)
    with archive.db.transaction() as conn:
        ChecklistService.verify_wto_candidate_origin(conn, claim, plan, result)
    assert result["scope"]["current_catalog_sha256"] == blob.digest
    assert result["scope"]["previous_verified_source_hash"] != blob.digest
    assert result["scope"]["pending_format_verification"]


@pytest.mark.parametrize(
    "failure",
    ["candidate", "owner", "member", "page_hash", "page_cas", "source_url", "scope_original", "plan"],
)
def test_pending_format_rejects_bare_urls_or_broken_source_ownership_and_mapping(archive, failure):
    def mutate(value):
        if failure == "scope_original":
            value["scope"]["documents"] = []

    claim, plan, result, _ = pending_candidate(archive, mutate=mutate)
    with archive.db.transaction() as conn:
        if failure == "candidate":
            claim["candidate_id"] = None
        elif failure == "owner":
            claim["owner_id"] = "owner-b"
        elif failure == "member":
            result["scope"]["member"] = "SG"
        elif failure == "page_hash":
            conn.execute("UPDATE checklist_pages SET content_hash=?", ("0" * 64,))
        elif failure == "page_cas":
            conn.execute("UPDATE checklist_pages SET storage_path=storage_path||'-wrong'")
        elif failure == "source_url":
            conn.execute(
                "UPDATE checklist_candidates SET discovered_from='https://goods-schedules.wto.org/browse/1/file_browser'"
            )
        elif failure == "plan":
            conn.execute("UPDATE checklist_bindings SET version=5")
        with pytest.raises(ValueError, match="完整映射"):
            ChecklistService.verify_wto_candidate_origin(conn, claim, plan, result)


def test_ordinary_documents_do_not_issue_additional_source_queries():
    class NoDatabase:
        def execute(self, *_):
            raise AssertionError("ordinary parsing must not inspect WTO sources")

    for result in (None, {}, {"scope": {"provider": "other"}}):
        ChecklistService.verify_wto_candidate_origin(NoDatabase(), {}, {}, result)
