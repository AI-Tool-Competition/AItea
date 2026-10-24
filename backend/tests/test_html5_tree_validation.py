import hashlib

from app.contexts.documents.infrastructure.parsers.ingestion_quality import quality_warning
from app.contexts.documents.infrastructure.parsers.native_parser import (
    _parse_html,
    _parsed_result,
    parse_file,
)


def test_lexbor_validates_recoverable_html5_without_rewriting_citation_source():
    raw = (
        b"<html><main><h1>Import</h1><p id='condition'>Keep &amp; review"
        b"<table><tr><th>Code<td>0902</table><img src='diagram.png'>"
    )
    source_hash = hashlib.sha256(raw).hexdigest()
    parsed = _parsed_result(_parse_html(raw), source_hash, "law")
    validation = parsed["html_source"]["dom_validation"]

    assert validation["engine"] == "selectolax.lexbor"
    assert validation["engine_version"] == "1.0.2"
    assert validation["html5_tree_built"] is True
    assert validation["scripts_executed"] is False
    assert validation["main_count"] == 1
    assert validation["table_count"] == 1
    assert validation["heading_count"] == 1
    assert parsed["source_hash"] == source_hash
    table = next(element for element in parsed["elements"] if element["kind"] == "table")
    assert table["content_text"] == "| Code | 0902 |\n| --- | --- |"
    assert table["locator"]["line_start"] == 1
    table_chunk = next(chunk for chunk in parsed["chunks"] if "| Code |" in chunk["text"])
    assert "Code" in table_chunk["text"] and "0902" in table_chunk["text"]
    image = parsed["html_source"]["objects"][0]
    reference = image["url_attributes"][0]
    assert raw.decode()[reference["char_start"] : reference["char_end"]] == "diagram.png"
    assert image["locator"]["source_hash"] == source_hash


def test_html_uses_source_mapped_structuring_instead_of_mineru_vlm():
    result = parse_file("official.html", b"<main><h1>Official title</h1><p>Preserve legal text.</p></main>")

    assert result["parser"] == "html-basic"
    assert result["parser_version"] == "9"
    assert result["html_source"]["dom_validation"]["engine"] == "selectolax.lexbor"
    assert result["pipeline_version"].startswith("html-source-mapping-v9+")
    assert "remote_parse" not in result
    assert any(chunk["text"] == "Preserve legal text." for chunk in result["chunks"])


def test_html_table_keeps_grid_rows_and_exact_original_source_range():
    raw = (
        "<main><h1>Tariff</h1><table><thead><tr><th>Product</th><th>Rate</th></tr></thead>"
        "<tbody><tr><td>Tea</td><td>20%</td></tr></tbody></table></main>"
    )
    result = parse_file("tariff.html", raw.encode(), "text/html")
    table = next(element for element in result["elements"] if element["kind"] == "table")

    assert table["content_text"] == "| Product | Rate |\n| --- | --- |\n| Tea | 20% |"
    locator = table["locator"]
    assert (
        raw[locator["char_start"] : locator["char_end"]]
        == raw[raw.index("<table>") : raw.index("</table>") + len("</table>")]
    )
    assert locator["column_counts"] == [2, 2]
    assert locator["table_row_count"] == 2
    assert locator["structure_status"] == "complete"
    assert [len(row["cells"]) for row in locator["rows"]] == [2, 2]
    assert locator["rows"][1]["cells"][1]["text"] == "20%"
    assert locator["rows"][1]["cells"][1]["source_boundary_complete"] is True
    first_row = locator["rows"][0]
    assert raw[first_row["char_start"] : first_row["char_end"]] == ("<tr><th>Product</th><th>Rate</th></tr>")
    assert result["quality"]["complete"] is True


def test_table_spans_are_expanded_into_an_auditable_logical_grid():
    raw = (
        '<main><table><tr><th id="code" scope="row" rowspan="2">Code</th>'
        '<td headers="code" colspan="2">Duty</td></tr>'
        "<tr><td>0902</td><td>Import</td></tr></table></main>"
    )
    result = parse_file("tariff.html", raw.encode(), "text/html")
    table = next(element for element in result["elements"] if element["kind"] == "table")
    rows = table["locator"]["rows"]
    header, duty = rows[0]["cells"]

    assert header["rowspan"] == 2 and header["rowspan_attribute_value"] == "2"
    assert header["scope"] == "row" and header["id"] == "code" and header["header_cell"]
    assert duty["colspan"] == 2 and duty["colspan_attribute_value"] == "2"
    assert duty["headers"] == "code"
    assert header["source_boundary_complete"] and duty["source_boundary_complete"]
    assert raw[header["char_start"] : header["char_end"]] == '<th id="code" scope="row" rowspan="2">Code</th>'
    assert raw[duty["char_start"] : duty["char_end"]] == '<td headers="code" colspan="2">Duty</td>'
    assert table["locator"]["structure_status"] == "complete"
    assert table["locator"]["column_counts"] == [3, 3]
    assert table["locator"]["grid"][1][0]["source_cell_index"] == 1
    assert table["locator"]["grid"][1][0]["is_origin"] is False
    assert table["locator"]["grid_coordinate_basis"].startswith("zero-based")
    assert "merged across 2 row(s) × 1 column(s)" in table["content_text"]
    assert "merged across 1 row(s) × 2 column(s)" in table["content_text"]
    assert result["quality"]["complete"] is True
    assert quality_warning(result) is None


def test_rowspan_cannot_cross_explicit_table_row_groups():
    raw = (
        "<main><table><tbody><tr><td rowspan='2'>Category</td></tr></tbody>"
        "<tbody><tr><td>Item</td></tr></tbody></table></main>"
    )
    result = parse_file("invalid-span.html", raw.encode(), "text/html")
    table = next(element for element in result["elements"] if element["kind"] == "table")

    assert table["locator"]["structure_status"] == "partial"
    assert "cell_span_crosses_row_group" in table["locator"]["structure_reasons"]
    assert result["quality"]["complete"] is False


def test_three_column_group_heading_is_preserved_as_a_single_merged_cell():
    raw = (
        "<main><table><tr><td colspan='3'>Temporary consignments</td></tr>"
        "<tr><td>Event</td><td>TCS</td><td>Temporary import</td></tr></table></main>"
    )
    result = parse_file("merged-heading.html", raw.encode(), "text/html")
    table = next(element for element in result["elements"] if element["kind"] == "table")

    assert table["locator"]["column_counts"] == [3, 3]
    assert table["locator"]["rows"][0]["source_cell_count"] == 1
    assert table["locator"]["rows"][0]["logical_column_count"] == 3
    assert "Temporary consignments [merged across 1 row(s) × 3 column(s)]" in table["content_text"]
    assert result["quality"]["complete"] is True


def test_html_ordered_and_nested_lists_keep_their_explicit_hierarchy():
    raw = "<main><ol start='3'><li>Third</li><li value='7'>Seventh<ul><li>Detail</li></ul></li></ol></main>"
    result = parse_file("list.html", raw.encode(), "text/html")
    items = [element for element in result["elements"] if element["kind"] == "list"]

    assert [element["content_text"] for element in items] == ["Third", "Seventh", "Detail"]
    assert items[0]["locator"]["list_path"] == [{"type": "ol", "index": 3}]
    assert items[1]["locator"]["list_path"] == [{"type": "ol", "index": 7}]
    assert items[2]["locator"]["list_path"] == [
        {"type": "ol", "index": 7},
        {"type": "ul", "index": None},
    ]


def test_html_links_keep_visible_anchor_text_and_exact_source_binding():
    raw = "<main><p>See <a href='/law?id=4&amp;part=2'>Article 4</a>.</p></main>"
    result = parse_file("reference.html", raw.encode(), "text/html")
    link = result["html_source"]["links"][0]
    locator = link["locator"]

    assert link["href"] == "/law?id=4&part=2"
    assert link["raw_href"] == "/law?id=4&amp;part=2"
    assert link["anchor_text"] == "Article 4"
    assert raw[locator["char_start"] : locator["char_end"]] == "<a href='/law?id=4&amp;part=2'>Article 4</a>"
    assert locator["source_boundary_complete"] is True


def test_html_preserves_source_semantic_tags_and_classes_without_classifying_them():
    raw = "<main><div id='pasal-4' class='article law-text'>Pasal 4. Import duty.</div></main>"
    result = parse_file("law.html", raw.encode(), "text/html")
    element = next(item for item in result["elements"] if item["kind"] == "paragraph")

    assert element["locator"]["semantic_tag"] == "div"
    assert element["locator"]["class_tokens"] == ["article", "law-text"]
    assert element["locator"]["anchor"] == "pasal-4"
    assert element["locator"]["dom_path"].endswith("div[1]")
