"""Current public agency originals, frozen recipes and literal data boundaries."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from app.checklists import load_blueprint_version
from app.discovery import _CUSTOMS_TEXT_PATHS, discover_page, recipe_for


FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
PLAN = "singapore-demo-20261009"
PDPC = "https://www.pdpc.gov.sg"
GUIDANCE = PDPC + "/organisations/regulations-decisions/regulatory-guidance/"
DNC = GUIDANCE + "advisory-guidelines-on-the-do-not-call-provisions"
ASSET = PDPC + "/assets/8440dd4d-973b-4b1b-bbb9-2c42c5dec81b"


def item(code, version=4):
    return next(row for row in load_blueprint_version(PLAN, version)["items"] if row["id"] == code)


def flight_html(stream, *, title="PDPC Advisory Guidelines"):
    encoded = json.dumps([1, stream], ensure_ascii=True).replace("<", r"\u003c")
    return (
        f"<title>{title}</title><h1>{title}</h1><main><div class='rte'></div></main>"
        f"<script>self.__next_f.push({encoded})</script>"
    ).encode()


def fragment_record(fragment, record_id="27"):
    return f"{record_id}:T{len(fragment.encode()):x}," + fragment


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (
            "key-concepts-current",
            {
                "34058be5-ae13-4c40-89e6-1c945d19f65c",
                "a9d1f217-d3cb-4056-bbd0-41a29ab63605",
                "889e03cc-5e3d-4caa-be15-ee3342672008",
                "1582fb83-088e-4899-b298-0be0d85bbfa1",
            },
        ),
        ("selected-topics-current", {"d1fbaa47-1471-4208-bfad-9f4bf33453ad"}),
        ("dnc", {"8440dd4d-973b-4b1b-bbb9-2c42c5dec81b"}),
        ("marketing-consent", {"564456e5-5a3a-41da-b882-f468ba1575d7"}),
        (
            "data-intermediaries",
            {"27e814c7-b1a7-43d0-ab97-595005850ab3", "b35cc60c-eab1-46be-a1ca-24e4d3896288"},
        ),
    ],
)
def test_actual_public_next_records_discover_all_selected_pdfs_with_locator(label, expected):
    evidence = next(
        row
        for row in json.loads((FIXTURES / "pdpc-next-source-20261013.json").read_text())
        if row["fixture"] == f"pdpc-{label}-next-20261013.html"
    )
    content = (FIXTURES / evidence["fixture"]).read_bytes()
    assert sha256(content).hexdigest() == evidence["fixture_sha256"]
    result = discover_page(evidence["source_url"], content, "text/html", recipe_for(item("3.6")))
    assert result["matched"] and result["purpose"] == "detail"
    originals = [link for link in result["links"] if link["purpose"] == "document"]
    assert {link["url"].rsplit("/", 1)[-1] for link in originals} == expected
    assert all(
        link["source_locator"]["source_page_sha256"] == sha256(content).hexdigest() for link in originals
    )
    assert all(link["source_locator"]["record_id"] in {"24", "27"} for link in originals)
    assert result["scope"]["provider"] == "pdpc-next-flight-v1"
    assert {row["url"] for row in result["scope"]["link_records"] if row["purpose"] == "document"} == {
        link["url"] for link in originals
    }
    assert all(
        row["locator"]["decoded_record_end_byte"] > row["locator"]["decoded_record_start_byte"]
        for row in result["scope"]["link_records"]
    )


def test_frozen_v3_scope_and_source_requirements_are_not_expanded():
    old = load_blueprint_version(PLAN, 3)
    new = load_blueprint_version(PLAN, 4)
    assert old["version"] == 3 and new["version"] == 4
    assert len(old["items"]) == len(new["items"]) == 16
    assert old["source_document"] == new["source_document"]
    for before, after in zip(old["items"], new["items"], strict=True):
        assert before["id"] == after["id"]
        for field in (
            "verbatim_requirements",
            "scope_include",
            "scope_exclude",
            "steps",
            "acceptance",
            "relation_rules",
        ):
            assert before[field] == after[field]
        if before["id"] not in {"3.1", "3.6"}:
            assert before == after
    assert not discover_page(ASSET, b"%PDF-1.4\n%%EOF", "application/pdf", recipe_for(item("3.6", 3)))[
        "matched"
    ]
    customs = "https://www.customs.gov.sg" + _CUSTOMS_TEXT_PATHS[0]
    assert not discover_page(
        customs, b"<h1>Import Operations Overview</h1>", "text/html", recipe_for(item("3.1", 3))
    )["matched"]


def test_pdpc_asset_is_a_candidate_only_for_matching_path_and_pdf_signature():
    recipe = recipe_for(item("3.6"))
    assert discover_page(ASSET, b"%PDF-1.4\n%%EOF", "application/pdf", recipe)["matched"]
    assert not discover_page(ASSET, b"<h1>Advisory Guidelines</h1>", "text/html", recipe)["matched"]
    for target in (
        ASSET + ".webp",
        ASSET + "?download=1",
        ASSET.replace("www.pdpc.gov.sg", "apps.pdpc.gov.sg"),
        ASSET.replace("/assets/", "/private/"),
    ):
        assert not discover_page(target, b"%PDF-1.4\n%%EOF", "application/pdf", recipe)["matched"]


@pytest.mark.parametrize("path", _CUSTOMS_TEXT_PATHS)
def test_customs_only_verified_public_articles_are_html_originals(path):
    recipe = recipe_for(item("3.1"))
    result = discover_page(
        "https://www.customs.gov.sg" + path,
        b"<title>Singapore Customs</title><h1>Import Requirements</h1><p>Official explanatory article.</p>",
        "text/html",
        recipe,
    )
    assert result["matched"] and result["purpose"] == "document"
    assert not discover_page(
        "https://www.customs.gov.sg" + path + "?admin=1", b"<h1>Import requirements</h1>", "text/html", recipe
    )["matched"]


def test_customs_official_174_file_hosts_and_other_paths_stay_scoped():
    recipe = recipe_for(item("3.1"))
    for host in ("isomer-user-content.by.gov.sg",):
        official = f"https://{host}/174/a2c17d2b-0d0b-42c8-b650-ab29bc0c9847/customs-guide-on-keeping-and-maintaining-records-in-image-system.pdf"
        assert discover_page(official, b"%PDF-1.4\n%%EOF", "application/pdf", recipe)["matched"]
        for target in (
            official.replace("/174/", "/45/"),
            official + "?token=x",
            official.replace("a2c17d2b-0d0b-42c8-b650-ab29bc0c9847", "not-a-document-id"),
        ):
            assert not discover_page(target, b"%PDF-1.4\n%%EOF", "application/pdf", recipe)["matched"]
    for target in (
        "https://www.customs.gov.sg/doing-business/import-operations/import-procedures/",
        "https://www.customs.gov.sg/doing-business/valuation-duties-and-fees/random-article/",
        "https://www.pdpc.gov.sg/organisations/regulations-decisions/regulatory-guidance/financial-data-guidelines",
    ):
        result = discover_page(target, b"<h1>Official article</h1>", "text/html", recipe)
        assert not result["matched"] or result["purpose"] != "document"


def test_next_text_record_counts_utf8_bytes_and_preserves_multiple_push_mapping():
    fragment = f'<p>资料 Δ</p><a href="{ASSET}">Download</a>'
    stream = fragment_record(fragment)
    boundary = stream.index("<a")
    first = json.dumps([1, stream[:boundary]], ensure_ascii=True)
    second = json.dumps([1, stream[boundary:]], ensure_ascii=True)
    content = f"<h1>PDPC guidelines</h1><script>self.__next_f.push({first})</script><script>self.__next_f.push({second})</script>".encode()
    result = discover_page(DNC, content, "text/html", recipe_for(item("3.6")))
    assert result["matched"] and [row["url"] for row in result["links"]] == [ASSET]
    blocks = result["scope"]["source_blocks"]
    assert len(blocks) == 2 and blocks[0]["decoded_stream_end_byte"] == blocks[1]["decoded_stream_start_byte"]
    assert result["scope"]["decoded_bytes"] == len(stream.encode())


@pytest.mark.parametrize(
    "stream",
    [
        "27:T10,<a href='/assets/a'>",
        "27:Tffffffff,<p>Huge declared text</p>",
        '24:{"data":{"content":"<a href=\\"/assets/a\\">x</a>"}}',
        "27:Qarbitrary-unknown-protocol\n",
        '27:{"data":{"content":NaN}}\n',
        '27:["$","a",null,{"href":fetch("/assets/a")}]\n',
    ],
)
def test_next_truncation_unknown_protocol_and_expressions_fail_closed(stream):
    result = discover_page(DNC, flight_html(stream), "text/html", recipe_for(item("3.6")))
    assert not result["matched"] and not result["links"]


def test_literal_push_never_evaluates_scripts_or_reads_module_links():
    imports = '1:I["' + ASSET + '"]\n2:HL["' + ASSET + '"]\n'
    unknown = '3:"<a href=\\"' + ASSET + '\\">hidden string</a>"\n'
    result = discover_page(DNC, flight_html(imports + unknown), "text/html", recipe_for(item("3.6")))
    assert result["matched"] and not result["links"]
    script = (
        '<h1>PDPC Guidelines</h1><script>self.__next_f.push([1,"0:null\\n"]);fetch("' + ASSET + '")</script>'
    )
    assert not discover_page(DNC, script.encode(), "text/html", recipe_for(item("3.6")))["matched"]


def test_next_record_node_and_byte_limits_fail_closed(monkeypatch):
    import app.discovery as discovery

    recipe = recipe_for(item("3.6"))
    monkeypatch.setattr(discovery, "_PDPC_FLIGHT_RECORDS", 2)
    assert not discover_page(DNC, flight_html("1:null\n2:null\n3:null\n"), "text/html", recipe)["matched"]
    monkeypatch.setattr(discovery, "_PDPC_FLIGHT_RECORDS", 512)
    monkeypatch.setattr(discovery, "_PDPC_FLIGHT_NODES", 3)
    assert not discover_page(DNC, flight_html("1:[0,1,2,3]\n"), "text/html", recipe)["matched"]
    monkeypatch.setattr(discovery, "_PDPC_FLIGHT_NODES", 50000)
    monkeypatch.setattr(discovery, "_PDPC_FLIGHT_BYTES", 10)
    assert not discover_page(DNC, flight_html("1:null\n2:null\n"), "text/html", recipe)["matched"]


def test_pdpc_runtime_error_heading_in_decoded_body_still_fails_closed():
    stream = fragment_record(f'<h1>404 Page Not Found</h1><a href="{ASSET}">Download</a>')
    result = discover_page(DNC, flight_html(stream), "text/html", recipe_for(item("3.6")))
    assert not result["matched"] and not result["links"]


def test_agency_file_hosts_cannot_inherit_html_article_purpose():
    recipe = recipe_for(item("3.1"))
    for host in ("isomer-user-content.by.gov.sg",):
        url = "https://" + host + _CUSTOMS_TEXT_PATHS[0]
        assert not discover_page(url, b"<h1>Official import explanation</h1>", "text/html", recipe)["matched"]


def test_pdpc_next_links_do_not_extend_unrelated_country_or_agency_rules():
    content = flight_html(fragment_record(f'<a href="{ASSET}">Download</a>'))
    assert not discover_page(DNC, content, "text/html", recipe_for(item("3.1")))["matched"]
    old = discover_page(DNC, content, "text/html", recipe_for(item("3.6", 3)))
    assert not old["matched"] or not old["links"]


def test_pdpc_next_one_record_and_link_total_are_bounded(monkeypatch):
    import app.discovery as discovery

    recipe = recipe_for(item("3.6"))
    monkeypatch.setattr(discovery, "_PDPC_FLIGHT_RECORD_BYTES", 10)
    stream = fragment_record(f'<a href="{ASSET}">Download</a>')
    assert not discover_page(DNC, flight_html(stream), "text/html", recipe)["matched"]
    monkeypatch.setattr(discovery, "_PDPC_FLIGHT_RECORD_BYTES", 1024 * 1024)
    monkeypatch.setattr(discovery, "MAX_PAGE_LINKS", 1)
    stream = fragment_record(f'<a href="{ASSET}">One</a><a href="{ASSET}">Two</a>')
    assert not discover_page(DNC, flight_html(stream), "text/html", recipe)["matched"]


def test_next_nesting_is_bounded_before_json_decoder_and_recipe_flag_is_typed():
    content = flight_html("1:" + "[" * 65 + "0" + "]" * 65 + "\n")
    recipe = recipe_for(item("3.6"))
    assert not discover_page(DNC, content, "text/html", recipe)["matched"]
    invalid = deepcopy(recipe)
    invalid["sg_official_2026"] = "true"
    with pytest.raises(ValueError, match="新加坡官方新站"):
        discover_page(DNC, b"<h1>Official</h1>", "text/html", invalid)


@pytest.mark.parametrize("error", ["404 Page Not Found", "Access denied", "Verify you are human"])
def test_http_200_soft_errors_do_not_create_original_candidates(error):
    stream = fragment_record(f'<p>{error}</p><a href="{ASSET}">Download</a>')
    result = discover_page(DNC, flight_html(stream, title=error), "text/html", recipe_for(item("3.6")))
    assert not result["matched"] and not result["links"]
    customs = "https://www.customs.gov.sg" + _CUSTOMS_TEXT_PATHS[0]
    assert not discover_page(customs, f"<h1>{error}</h1>".encode(), "text/html", recipe_for(item("3.1")))[
        "matched"
    ]
