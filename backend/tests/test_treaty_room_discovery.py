"""Public treaty originals, catalog-only diplomacy, and immutable older plans."""

import json
from contextlib import closing
from copy import deepcopy
from urllib.parse import urlencode

import pytest
from app.bootstrap import Service
from app.contexts.ingestion.application.checklists import load_blueprint_version, load_blueprints
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
    _Recipe,
    discover_page,
    recipe_for,
    request_parameters,
)
from app.contexts.ingestion.infrastructure.source_adapters.treaty_room_discovery import (
    BASE,
    catalog_url,
    original_url,
)
from app.shared.domain.errors import AppError
from app.shared.domain.values import now

PLAN = "indonesia-20261003"


def item():
    definition = deepcopy(next(row for row in load_blueprints()[PLAN]["items"] if row["id"] == "5.1"))
    definition["trusted_hosts"] = ["api-treaty-room.kemlu.go.id"]
    definition["entries"] = [{"url": catalog_url(), "purpose": "catalog", "note": "Public listing"}]
    return definition


def row(identifier=1, title="Agreement on trade, investment and taxation", **updates):
    result = {
        "id_perjanjian": identifier,
        "judul": title,
        "mulaiBerlaku": "Article 14, paragraph 1",
        "masaBerlaku": "Five years, pending verification",
        "berlakuSetelah": None,
        "file": {
            "id_konten": identifier + 100,
            "file": f"1-TREATY.{identifier}",
            "file2": None,
            "file3": None,
            "file4": None,
        },
    }
    result.update(updates)
    return result


def response(rows, *, page=1, limit=40, total=None, **updates):
    total = len(rows) if total is None else total
    data = {
        "success": True,
        "data": rows,
        "total": total,
        "current_page": page,
        "last_page": max(1, (total + limit - 1) // limit),
        "length": max(1, (total + limit - 1) // limit),
    }
    data.update(updates)
    return json.dumps(data).encode()


def inspect(rows, **options):
    return discover_page(catalog_url(), response(rows, **options), "application/json", recipe_for(item()))


def test_all_directory_metadata_is_kept_but_diplomacy_does_not_trigger_downloads():
    legal = row(1, "<i>Agreement</i> on trade and taxation", unknown_source_field={"status": "unknown"})
    diplomacy = row(2, "Memorandum of Understanding on cultural exchange and diplomatic protocol")
    result = inspect([legal, diplomacy])
    assert result["matched"] is True
    documents = result["scope"]["documents"]
    assert len(documents) == 2
    assert documents[0]["source_metadata"] == legal
    assert documents[1]["source_metadata"] == diplomacy
    assert documents[1]["original_policy"] == "catalog_only"
    assert documents[1]["originals"][0]["original_url"] == original_url(2, 102, "1-TREATY.2")
    assert [link["url"] for link in result["links"]] == [original_url(1, 101, "1-TREATY.1")]
    assert all(document["legal_review_status"] == "not_reviewed" for document in documents)
    assert result["scope"]["legal_status_inferred"] is False
    assert result["scope"]["exhaustive_content_verified"] is False
    assert "indonesia_effective_date" in documents[0]["missing_verified_fields"]


@pytest.mark.parametrize(
    "title",
    [
        "Persetujuan Kemitraan Ekonomi Komprehensif antara Pemerintah Indonesia – Pemerintah Kanada "
        "(I-C CEPA) ( The Government of the Republic of Indonesia-the Government of Canada "
        "Comprehensive Economic Partnership Agreement (I-C CEPA) )",
        "Persetujuan Kemitraan Ekonomi Komprehensif antara Pemerintah Republik Indonesia dan "
        "Pemerintah Persatuan Emirat Arab ( Comprehensive Economic Partnertship Agreement between "
        "the Government of the Republic of Indonesia and the Government of the United Arab Emirates )",
        "Protokol Perubahan Persetujuan antara Republik Indonedia dan Jepang mengenai Suatu "
        "Kemitraan Ekonomi ( Protocol Amending the Agreement between the Republic of Indonesia "
        "and Japan for an Economic Partnership )",
        "Protocol amending the Indonesia-Japan EPA",
    ],
)
def test_economic_partnership_trade_agreements_register_their_originals(title):
    result = inspect([row(title=title)])
    assert result["scope"]["documents"][0]["priority_topics"] == ["trade_customs"]
    assert len(result["links"]) == 1
    assert result["links"][0]["purpose"] == "document"


@pytest.mark.parametrize(
    "title",
    [
        "Memorandum of Understanding on Economic Cooperation",
        "Agreement on Economic Cooperation and diplomatic exchanges",
        "Memorandum of Understanding on Economic Partnership",
        "EPA scientific exhibition partnership",
        "Pernyataan Menteri Bersama tentang Peluncuran Negosiasi antara Indonesia-Korea Selatan "
        "untuk Persetujuan Kemitraan Ekonomi Komprehensif (IK-CEPA) ( Joint Ministerial Statement "
        "on the Launch of the Reactivation of Negotiations for the Indonesia - Korea "
        "Comprehensive Economic Partnership Agreement (IK-CEPA) )",
        "Pernyataan Bersama pada Penandatanganan Persetujuan antara Republik Indonesia dan "
        "Jepang mengenai Kemitraan Ekonomi ( Joint Statement at the Signing of the Agreement "
        "between the Republic of Indonesia and Japan for an Economic Partnership )",
        "Deklarasi antara Pemerintah Republik Indonesia dan Pemerintah Australia mengenai "
        "Penyelesaian Persetujuan Kemitraan Ekonomi Komprehensif ( Declaration by the Government "
        "of the Republic of Indonesia and the Government of Australia on the Conclusion of a "
        "Comprehensive Economic Partnership Agreement )",
    ],
)
def test_general_economic_cooperation_is_retained_without_automatic_original_downloads(title):
    result = inspect([row(title=title)])
    assert result["scope"]["documents"][0]["original_policy"] == "catalog_only"
    assert result["scope"]["documents"][0]["originals"]
    assert result["links"] == []


def test_four_file_columns_and_parallel_pagination_stay_under_existing_link_limit():
    records = []
    for identifier in range(1, 41):
        record = row(identifier)
        record["file"].update(
            file2=f"2-TREATY.{identifier}", file3=f"3-TREATY.{identifier}", file4=f"4-TREATY.{identifier}"
        )
        records.append(record)
    result = inspect(records, total=4160)
    assert result["matched"] is True
    assert len(result["links"]) == 200
    assert sum(link["purpose"] == "document" for link in result["links"]) == 160
    catalogs = [link for link in result["links"] if link["purpose"] == "catalog"]
    assert len(catalogs) == 40
    assert catalogs[0]["url"] == catalog_url(2)
    assert catalogs[-1]["url"] == catalog_url(41)
    assert result["scope"]["last_page"] == 104
    rules = _Recipe(recipe_for(item()))
    assert all(rules.purpose(link["url"]) == link["purpose"] for link in result["links"])


@pytest.mark.parametrize(
    "filename",
    [
        "../hidden",
        "https://evil.example/law",
        "\\private",
        "a%2fhidden",
        "file?secret",
        "file#fragment",
        "..",
        " bad ",
    ],
)
def test_malformed_original_fields_become_explicit_gaps_not_urls(filename):
    record = row()
    record["file"]["file"] = filename
    result = inspect([record])
    assert result["matched"] is True
    assert result["links"] == []
    assert result["scope"]["documents"][0]["invalid_file_slots"] == ["file"]
    assert result["warnings"]


@pytest.mark.parametrize(
    "changes",
    [
        {"total": True},
        {"current_page": 2},
        {"last_page": 3},
        {"success": False},
        {"data": []},
        {"total": 1_000_001},
    ],
)
def test_catalog_count_or_shape_mismatch_cannot_be_called_collection(changes):
    result = inspect([row()], **changes)
    assert result["matched"] is False


@pytest.mark.parametrize(
    "path",
    [
        "/login",
        "/dashboard",
        "/perjanjian",
        "/referensi/negara",
        "/publik/perjanjian/1/edit",
        "/uploads/private.pdf",
    ],
)
def test_api_host_does_not_enable_private_reference_or_arbitrary_file_routes(path):
    rules = _Recipe(recipe_for(item()))
    assert rules.purpose(BASE + path) is None
    with pytest.raises(ValueError):
        request_parameters(BASE + path, item())


def test_catalog_filter_is_replayed_and_bounded_and_is_not_a_post_body():
    definition = item()
    filters = {"judul_perjanjian": "double taxation", "id_negara": "6"}
    url = catalog_url(limit=10, filters=filters)
    rules = _Recipe(recipe_for(definition))
    assert rules.purpose(url) == "catalog"
    assert request_parameters(url, definition) == {"accept_compressed_json": True}
    result = discover_page(
        url, response([row()], limit=10, total=1), "application/json", recipe_for(definition)
    )
    assert result["scope"]["filters"]["judul_perjanjian"] == "double taxation"
    assert result["scope"]["source_length_field"] == 1
    query = {
        "judul_perjanjian": "",
        "id_negara": "",
        "id_organisasi": "",
        "jenis_perjanjian": "",
        "id_subjek": "",
        "page": "1",
        "limit": "41",
    }
    invalid = BASE + "/publik/perjanjian?" + urlencode(sorted(query.items()))
    assert rules.purpose(invalid) is None
    assert rules.purpose(url + "&page=2") is None
    definition["id"] = "4.1"
    with pytest.raises(ValueError):
        request_parameters(url, definition)


def test_snapshot_loader_rejects_invalid_or_unavailable_versions_before_state_changes():
    current = load_blueprints()[PLAN]
    assert load_blueprint_version(PLAN, current["version"]) == current
    snapshot = load_blueprint_version(PLAN, 3)
    assert snapshot["id"] == PLAN and snapshot["version"] == 3
    assert snapshot["source_document"]["sha256"] == current["source_document"]["sha256"]
    for plan, version in [
        (PLAN, True),
        (PLAN, 0),
        (PLAN, "3"),
        (PLAN, current["version"] + 1),
        ("../../outside", 3),
        (PLAN, 1),
    ]:
        with pytest.raises(ValueError):
            load_blueprint_version(plan, version)


def test_internal_frozen_start_and_detail_use_original_binding_while_public_is_current(tmp_path):
    service = Service(tmp_path)
    with service.db.transaction() as conn:
        conn.execute(
            "INSERT INTO users(id,name,is_demo,created_at) VALUES(?,?,1,?)",
            ("old-campaign", "Old campaign", now()),
        )
    before = service.checklists.catalog()
    frozen = service.checklists.definition(PLAN, version=3)
    run = service.checklists.start(
        "old-campaign", PLAN, "1.1", {"client_request_id": "old-version"}, kind="discovery", version=3
    )
    with closing(service.db.connect()) as conn:
        binding = conn.execute(
            "SELECT version,snapshot FROM checklist_bindings WHERE owner_id=?", ("old-campaign",)
        ).fetchone()
        snapshot = json.loads(binding["snapshot"])
        assert binding["version"] == 3
        assert snapshot["plan"]["version"] == 3
        assert conn.execute("SELECT count(*) FROM checklist_bindings WHERE version<>3").fetchone()[0] == 0
    assert service.checklists.detail("old-campaign", PLAN, version=3)["plan"] == frozen
    assert service.checklists.definition(PLAN)["version"] == load_blueprints()[PLAN]["version"]
    assert service.checklists.catalog() == before
    with closing(service.db.connect()) as conn:
        run_plan = json.loads(
            conn.execute("SELECT plan FROM collection_runs WHERE id=?", (run["id"],)).fetchone()[0]
        )
    assert run_plan["checklist"]["definition"]["plan"]["version"] == 3
    with pytest.raises(AppError):
        service.checklists.start(
            "old-campaign",
            PLAN,
            "1.1",
            {"client_request_id": "invalid-version"},
            kind="discovery",
            version=True,
        )
    with closing(service.db.connect()) as conn:
        assert conn.execute("SELECT count(*) FROM collection_runs").fetchone()[0] == 1
