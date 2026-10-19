"""Independent exact counts cannot hide incomplete or inconsistent saved pages."""

from copy import deepcopy
import json
import re

import pytest

from test_egazette_campaign import campaign, collector, row


def setup_source(tmp_path, monkeypatch, *, split=False, confirmation="exact"):
    calls = []
    records = [row(index) for index in range(15)]
    for index, record in enumerate(records):
        record["publishTimestamp"] = 1609430400000 + (1000 if index < 8 else 20_000_000_000)

    def download(url, **kwargs):
        body = kwargs["json_body"]
        calls.append(body)
        count_query = body["hitsPerPage"] == 1
        interval = re.search(r"publishTimestamp >= (\d+) AND publishTimestamp <= (\d+)", body["filters"])
        selected = deepcopy(records)
        if interval:
            lo, hi = map(int, interval.groups())
            selected = [record for record in selected if lo <= record["publishTimestamp"] <= hi]
        total, size = len(selected), body["hitsPerPage"]
        if count_query and confirmation == "count_drift":
            total += 1
        start = body["page"] * size
        hits = selected[start : start + size]
        if count_query and confirmation == "identity_drift":
            hits[0]["objectID"] = "another-official-record"
        if count_query and confirmation == "metadata_drift":
            hits[0]["title"] = "Changed title"
        exhaustive = bool(interval) or (count_query and confirmation != "approximate")
        result = {
            "page": body["page"],
            "hitsPerPage": size,
            "nbHits": total,
            "nbPages": 1 if split and not interval and not count_query else (total + size - 1) // size,
            "hits": hits,
            "exhaustiveNbHits": exhaustive,
        }
        if count_query:
            result.update(facets={"publishYear": {"2021": 90000}}, exhaustiveFacetsCount=False)
        return {"content": json.dumps(result).encode(), "url": url, "mime_type": "application/json"}

    monkeypatch.setattr(campaign, "_download", download)
    runner = collector(tmp_path)
    state = runner.partition("Revised Acts", 2021)
    calls.clear()
    return runner, state, calls


@pytest.mark.parametrize("split", [False, True])
def test_exact_one_hit_count_confirms_saved_identity_union_without_changing_original_evidence(
    tmp_path, monkeypatch, split
):
    runner, state, calls = setup_source(tmp_path, monkeypatch, split=split)
    assert state["status"] == ("incomplete_split" if split else "catalogue_count_unconfirmed")
    before = {path: path.read_bytes() for path in (tmp_path / "catalog").rglob("*") if path.is_file()}
    result = runner.confirm_partition_count(state)
    assert result["status"] == "catalogue_complete"
    assert result["exhaustive_source_count"] is False  # Original approximate signal survives.
    assert result["count_confirmation"]["enumerated_index_records"] == 15
    assert result["current_effect_verified"] is False
    assert len(calls) == 1 and calls[0]["hitsPerPage"] == 1
    assert calls[0]["facets"] == ["publishYear"] and calls[0]["maxValuesPerFacet"] == 100
    assert result["count_confirmation"]["facet_counts_used"] is False
    assert calls[0]["filters"] == state["filters"]
    assert all(path.read_bytes() == raw for path, raw in before.items())
    calls.clear()
    assert runner.confirm_partition_count(state) == result and not calls


def test_approximate_one_hit_response_does_not_complete_partition(tmp_path, monkeypatch):
    runner, state, _ = setup_source(tmp_path, monkeypatch, confirmation="approximate")
    result = runner.confirm_partition_count(state)
    assert result["status"] == "catalogue_count_unconfirmed"
    assert result["count_confirmation"]["confirmed_exhaustive_source_count"] is False


@pytest.mark.parametrize("failure", ["count_drift", "identity_drift", "metadata_drift"])
def test_changed_count_or_first_record_refuses_completion(tmp_path, monkeypatch, failure):
    runner, state, _ = setup_source(tmp_path, monkeypatch, confirmation=failure)
    with pytest.raises(ValueError):
        runner.confirm_partition_count(state)


@pytest.mark.parametrize("failure", ["missing_page", "corrupt_page", "truncated_state", "scope", "overlap"])
def test_invalid_saved_coverage_is_refused_before_any_confirmation_request(tmp_path, monkeypatch, failure):
    runner, state, calls = setup_source(tmp_path, monkeypatch, split=failure == "overlap")
    if failure == "missing_page":
        (tmp_path / "catalog/2021-revised-acts/page-00001.json").unlink()
    elif failure == "corrupt_page":
        (tmp_path / "catalog/2021-revised-acts/page-00000.raw.json").write_bytes(b"{}")
    elif failure == "truncated_state":
        state["validated_pages"] = [0]
    elif failure == "scope":
        state["filters"] += ' AND title:"one"'
    else:
        state["children"][1] = deepcopy(state["children"][0])
    with pytest.raises(ValueError):
        runner.confirm_partition_count(state)
    assert calls == []
