"""SSO current catalogues must not spend their link budget on navigation."""

import hashlib
import json
from pathlib import Path

import pytest
from app.contexts.ingestion.infrastructure.source_adapters.link_discovery import discover_page, recipe_for
from app.configuration.paths import PROJECT_ROOT

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"


def definition(code="1.3", *, enabled=True):
    category = "SL" if code == "1.3" else "Act"
    url = f"https://sso.agc.gov.sg/Browse/{category}/Current"
    if code == "1.1":
        url += "/All?PageSize=20&SortBy=Title&SortOrder=ASC&SubType=Constitutional"
    return {
        "id": code,
        "adapter": "sso",
        "tags": ["country:SG"],
        "entries": [{"url": url, "purpose": "catalog"}],
        "limits": {"sso_catalog_2026": enabled},
    }


def inspect(raw=None, code="1.3"):
    item = definition(code)
    if raw is None:
        name = "sso-constitutional-20261019.html" if code == "1.1" else "sso-sl-current-20261019.html"
        raw = (FIXTURES / name).read_bytes()
    return discover_page(item["entries"][0]["url"], raw, "text/html", recipe_for(item))


def test_real_current_sl_rows_and_counts_survive_navigation_link_saturation():
    raw = (FIXTURES / "sso-sl-current-20261019.html").read_bytes()
    nav = "".join(f'<a href="/Browse/SL/Current/All/{i}?PageSize=20">{i + 1}</a>' for i in range(294))
    raw = raw.replace(b"<body>", ("<body>" + nav).encode())
    result = inspect(raw)
    assert result["matched"] and result["warnings"] == []
    assert len(result["links"]) == 21
    scope = result["scope"]
    assert (scope["total"], scope["pages"], scope["returned"], scope["page_index"]) == (5872, 294, 20, 0)
    assert scope["source_sha256"] == hashlib.sha256(raw).hexdigest()
    docs = scope["documents"]
    assert len({row["identity_key"] for row in docs}) == 20
    assert docs[0]["title"] == "Accountant-General Appointed to be Accountant for Supreme Court"
    assert docs[0]["pdf_url"] is None
    assert docs[1]["document_date"] == "20250731"
    assert "ViewType=Pdf" in docs[1]["original_url"]
    assert "_=" not in docs[1]["original_url"]
    assert result["links"][-1]["purpose"] == "catalog"
    assert "/All/1?" in result["links"][-1]["url"]
    for row in docs:
        loc = row["source_locator"]
        fragment = raw.decode()[loc["start"] : loc["end"]]
        assert fragment.startswith("<tr") and fragment.endswith("</tr>")
        assert hashlib.sha256(fragment.encode()).hexdigest() == loc["fragment_sha256"]


def test_pagination_does_not_consume_the_document_candidate_limit():
    raw = (FIXTURES / "sso-sl-current-20261019.html").read_bytes()
    item = definition()
    recipe = recipe_for(item)
    recipe["max_links"] = 20
    result = discover_page(item["entries"][0]["url"], raw, "text/html", recipe)
    assert result["matched"]
    assert len(result["links"]) == 21
    assert sum(link["purpose"] == "document" for link in result["links"]) == 20
    assert sum(link["purpose"] == "catalog" for link in result["links"]) == 1


def test_three_constitutional_documents_have_distinct_identity_and_no_extra_navigation():
    result = inspect(code="1.1")
    assert result["matched"] and result["scope"]["total"] == 3
    assert result["scope"]["subtype"] == "Constitutional"
    assert {x["identity_key"] for x in result["links"]} == {
        "sso:act:cons1963",
        "sso:act:isa1965",
        "sso:act:rsia1965",
    }
    assert all(x["purpose"] == "document" for x in result["links"])
    assert result["scope"]["full_text_acquired"] is False
    assert result["scope"]["current_effect_verified"] is False


def test_same_legislation_family_can_expose_distinct_current_editions():
    raw = (FIXTURES / "sso-sl-current-20261019.html").read_text()
    import re

    rows = list(re.finditer(r'<tr class="(?:alternate)?".*?</tr>', raw, re.S))
    assert len(rows) == 20
    first = rows[0].group().replace("DocDate=19970926", "DocDate=19970927")
    last = rows[-1]
    raw = raw[: last.start()] + first + raw[last.end() :]

    result = inspect(raw.encode())

    assert result["matched"]
    editions = [
        row for row in result["scope"]["documents"] if row["identity_key"] == "sso:sl:scja1969-n2"
    ]
    assert [row["document_date"] for row in editions] == ["19970926", "19970927"]


@pytest.mark.parametrize(
    "change",
    [
        "missing_row",
        "duplicate_row",
        "count",
        "page_index",
        "next_filter",
        "next_extra_filter",
        "next_host",
        "pdf_date",
        "truncated",
        "challenge",
    ],
)
def test_incomplete_or_cross_scope_catalogue_does_not_claim_completeness(change):
    raw = (FIXTURES / "sso-sl-current-20261019.html").read_text()
    import re

    rows = list(re.finditer(r'<tr class="(?:alternate)?".*?</tr>', raw, re.S))
    assert len(rows) == 20
    if change == "missing_row":
        raw = raw[: rows[0].start()] + raw[rows[0].end() :]
    elif change == "duplicate_row":
        raw = raw[: rows[1].start()] + rows[0].group() + raw[rows[1].end() :]
    elif change == "count":
        raw = raw.replace("5872 results in 294 pages", "5872 results in 293 pages")
    elif change == "page_index":
        raw = raw.replace("1&nbsp;&nbsp;&nbsp;", "2&nbsp;&nbsp;&nbsp;")
    elif change == "next_filter":
        raw = raw.replace("/Browse/SL/Current/All/1?PageSize=20", "/Browse/SL/Revoked/All/1?PageSize=20")
    elif change == "next_extra_filter":
        raw = raw.replace(
            "/Browse/SL/Current/All/1?PageSize=20", "/Browse/SL/Current/All/1?Title=Other&amp;PageSize=20"
        )
    elif change == "next_host":
        raw = raw.replace(
            'href="/Browse/SL/Current/All/1?', 'href="https://other.example/Browse/SL/Current/All/1?'
        )
    elif change == "pdf_date":
        raw = raw.replace("DocDate=20250731&amp;ViewType=Pdf", "DocDate=20200101&amp;ViewType=Pdf")
    elif change == "truncated":
        raw = raw[: rows[-1].start()]
    else:
        raw = "<html><title>Access denied</title><body>Please verify you are human</body></html>"
    result = inspect(raw.encode())
    assert not result["matched"] and result["links"] == [] and "scope" not in result


def test_aws_waf_200_page_is_explicit_access_challenge_not_empty_legislation():
    content = b"""<html><head><script src="https://published.token.awswaf.com/site/challenge.js"></script>
        </head><body><div id="challenge-container"></div></body></html>"""
    item = definition()
    result = discover_page(
        "https://sso.agc.gov.sg/SL/AA2004-R5?ViewType=Pdf", content, "text/html", recipe_for(item)
    )
    assert not result["matched"]
    assert "验证" in result["reason"] and "scope" not in result


def test_sso_plan_history_and_current_catalogue_restoration_are_versioned():
    root = PROJECT_ROOT / "config" / "collection-plans"
    old = json.loads((root / "versions/singapore-demo-20261009.v4.json").read_text())
    recipe_version = json.loads((root / "versions/singapore-demo-20261009.v5.json").read_text())
    removed_version = json.loads((root / "versions/singapore-demo-20261009.v6.json").read_text())
    current = json.loads((root / "singapore-demo-20261009.json").read_text())
    assert (
        old["version"] == 4
        and recipe_version["version"] == 5
        and removed_version["version"] == 6
        and current["version"] == 7
    )
    assert old["source_document"] == recipe_version["source_document"]
    for code in {"1.1", "1.2", "1.3"}:
        before = next(x for x in old["items"] if x["id"] == code)
        after = next(x for x in recipe_version["items"] if x["id"] == code)
        assert not recipe_for(before).get("sso_catalog_2026", False)
        assert recipe_for(after)["sso_catalog_2026"] is True
        assert before["steps"] == after["steps"] and before["acceptance"] == after["acceptance"]
        assert code not in {x["id"] for x in removed_version["items"]}
        restored = next(x for x in current["items"] if x["id"] == code)
        assert recipe_for(restored)["sso_catalog_2026"] is True
        assert "PageSize=500" in restored["entries"][0]["url"]
    assert "2.1" in {x["id"] for x in recipe_version["items"]}
    assert "2.1" not in {x["id"] for x in current["items"]}
    assert current["limits"]["item_count"] == len(current["items"]) == 15
