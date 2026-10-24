"""The public investment catalog is scoped evidence, not legal approval."""

import json
from copy import deepcopy
from pathlib import Path

import pytest
from app.contexts.ingestion.application.checklists import load_blueprints
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import (
    discover_page,
    recipe_for,
    request_parameters,
)

API = "https://jdih.bkpm.go.id/api/document-search?id_theme=1&limit=2&page=1"
FIXTURE = Path(__file__).parent / "fixtures/discovery/bkpm-investment.json"


def item():
    return next(x for x in load_blueprints()["indonesia-20261003"]["items"] if x["id"] == "4.1")


def discover(data=None, *, url=API, mime="application/json", definition=None):
    content = FIXTURE.read_bytes() if data is None else json.dumps(data).encode()
    return discover_page(url, content, mime, recipe_for(definition or item()))


def test_public_catalog_keeps_original_identity_pagination_and_metadata():
    result = discover()
    assert result["matched"] and result["purpose"] == "catalog"
    assert [link["purpose"] for link in result["links"]] == ["document", "document", "catalog"]
    assert result["links"][0]["identity_key"] == "bkpm:17386"
    assert all("jdih-storage.bkpm.go.id/jdih/jdih/" in link["url"] for link in result["links"][:2])
    source_record = result["links"][0]["source_record"]
    source_row = json.loads(FIXTURE.read_text())["allDocuments"][0]
    assert source_record["provider"] == "bkpm"
    assert source_record["interpretation"] == "unreviewed_source_values"
    assert source_record["fields"] == {
        "id": 17386,
        "id_theme": 1,
        "title": source_row["title"],
        "document_type": "Peraturan",
        "document_file_url": "https://jdih-storage.bkpm.go.id/jdih/jdih/2026Kepdepinvesthil15.S-.pdf",
        "verified_original_url": "https://jdih-storage.bkpm.go.id/jdih/jdih/2026Kepdepinvesthil15.S-.pdf",
        "regulation_number": "15.S",
        "regulation_type": "Keputusan",
        "establishment_date": "2026-07-24",
        "publication_date": "2026-09-01",
        "status": "Berlaku",
        "slug": source_row["slug"],
    }
    scope = result["scope"]
    assert (scope["theme_id"], scope["total"], scope["returned"], scope["offset"]) == (1, 88, 2, 0)
    assert scope["documents"][0]["detail_url"].startswith("https://jdih.bkpm.go.id/id/document/")
    assert "id_theme=1" in result["links"][-1]["url"] and "page=2" in result["links"][-1]["url"]
    assert request_parameters(API, {"item": item(), "plan": {"country": "ID"}}) == {
        "accept_compressed_json": True
    }


@pytest.mark.parametrize("change", ["duplicate", "count", "rows", "theme", "boolean_id"])
def test_inconsistent_response_does_not_claim_valid_scope(change):
    data = json.loads(FIXTURE.read_text())
    if change == "duplicate":
        data["allDocuments"][1] = deepcopy(data["allDocuments"][0])
    elif change == "count":
        data["v_peraturan_prior_aggregate"]["aggregate"]["count"] = True
    elif change == "rows":
        data["allDocuments"].pop()
    elif change == "theme":
        data["allDocuments"][0]["id_theme"] = 2
    else:
        data["allDocuments"][0]["id"] = True
    result = discover(data)
    assert not result["matched"] and not result["links"]


@pytest.mark.parametrize(
    "query",
    [
        "id_theme=2&limit=2&page=1",
        "id_theme=1&limit=51&page=1",
        "id_theme=1&limit=2&page=0",
        "id_theme=1&limit=2&page=1&page=2",
        "id_theme=1&limit=2&page=1&callback=x",
    ],
)
def test_only_verified_readonly_pagination_parameters_are_allowed(query):
    url = API.split("?")[0] + "?" + query
    assert not discover(url=url)["matched"]
    with pytest.raises(ValueError):
        request_parameters(url, item())


@pytest.mark.parametrize(
    "file_url",
    [
        "https://evil.example/jdih/jdih/law.pdf",
        "https://jdih-storage.bkpm.go.id/private/law.pdf",
        "https://jdih-storage.bkpm.go.id/jdih/jdih/law.pdf?token=secret",
        "https://jdih-storage.bkpm.go.id/jdih/jdih/%252e%252e/law.pdf",
    ],
)
def test_untrusted_originals_leave_a_visible_gap(file_url):
    data = json.loads(FIXTURE.read_text())
    data["allDocuments"][0]["document_file_url"] = file_url
    result = discover(data)
    assert result["matched"] and result["warnings"]
    assert all(link.get("identity_key") != "bkpm:17386" for link in result["links"])


def test_other_plan_or_country_cannot_enable_the_endpoint():
    for patch in ({"id": "4.2"}, {"tags": ["country:SG"]}):
        definition = item() | patch
        assert not discover(definition=definition)["matched"]
        with pytest.raises(ValueError):
            request_parameters(API, definition)
    assert not discover(mime="text/html")["matched"]
    assert not discover(data={"allDocuments": []})["matched"]
