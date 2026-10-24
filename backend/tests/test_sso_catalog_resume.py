"""Exercise durable catalogue replay across an actual access-challenge failure."""

import gzip
import importlib.util
import json
import re
from pathlib import Path

import pytest
from app.configuration.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
spec = importlib.util.spec_from_file_location("collect_sso_catalog", ROOT / "scripts/collect_sso_catalog.py")
collector_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector_module)
FIXTURE = Path(__file__).parent / "fixtures/discovery/sso-sl-current-20261019.html"


def plan():
    return json.loads((ROOT / "config/collection-plans/singapore-demo-20261009.json").read_text())


def two_pages():
    first = FIXTURE.read_text().replace("5872 results in 294 pages", "40 results in 2 pages")
    first = first.replace("of 294", "of 2")
    second = first.replace("1&nbsp;&nbsp;&nbsp;", "2&nbsp;&nbsp;&nbsp;")
    second = re.sub(r'<a [^>]*aria-label="(?:Next|Last) Page"[^>]*>.*?</a>', "", second, flags=re.S)
    second = re.sub(r"(/SL/[A-Za-z0-9_-]+)(?=\?)", r"\1-NEXT", second)
    return first.encode(), second.encode()


def response(url, raw):
    return {"url": url, "mime_type": "text/html", "content": raw, "response_status": 200}


def test_resume_replays_frozen_pages_and_retries_only_missing_page(tmp_path, monkeypatch):
    first, second = two_pages()
    challenge = b'<html><script src="https://public.token.awswaf.com/official/challenge.js"></script><body></body></html>'
    calls = []

    def download(url, **kwargs):
        calls.append(url)
        return response(url, first if "/All/1?" not in url else challenge)

    monkeypatch.setattr(collector_module, "_download", download)
    first_run = collector_module.CatalogCollector(tmp_path, plan(), ["sl-current"], page_size=20).run()
    assert first_run["catalogues_complete"] is False
    assert first_run["catalogs"][0]["rows"] == 20
    assert len(calls) == 2
    assert "验证" in first_run["catalogs"][0]["errors"][0]["error"]
    saved = tmp_path / "sl-current/page-00000.json"
    frozen = saved.read_bytes()
    failure = next((tmp_path / "sl-current/failed-attempts").glob("*.json"))
    evidence = json.loads(failure.read_text())
    assert gzip.decompress((failure.parent / evidence["raw_file"]).read_bytes()) == challenge
    assert evidence["original_or_catalogue_rows_acquired"] is False
    calls.clear()
    monkeypatch.setattr(
        collector_module, "_download", lambda url, **kw: calls.append(url) or response(url, second)
    )
    resumed = collector_module.CatalogCollector(tmp_path, plan(), ["sl-current"], page_size=20).run()
    assert resumed["catalogues_complete"] is True
    assert resumed["catalogs"][0]["rows"] == resumed["catalogs"][0]["total"] == 40
    assert len(calls) == 1 and "/All/1?" in calls[0]
    assert saved.read_bytes() == frozen
    assert resumed["all_required_content_complete"] is False
    assert resumed["raw_original_downloads"] == resumed["model_calls"] == 0


@pytest.mark.parametrize("mutation", ["raw", "receipt_url", "changed_total"])
def test_corrupt_or_drifting_snapshot_cannot_complete(tmp_path, monkeypatch, mutation):
    first, second = two_pages()
    monkeypatch.setattr(
        collector_module,
        "_download",
        lambda url, **kw: response(url, first if "/All/1?" not in url else second),
    )
    result = collector_module.CatalogCollector(tmp_path, plan(), ["sl-current"], page_size=20).run()
    assert result["catalogues_complete"]
    page = tmp_path / "sl-current/page-00000.json"
    record = json.loads(page.read_text())
    if mutation == "raw":
        (page.parent / record["raw_file"]).write_bytes(gzip.compress(b"changed"))
    elif mutation == "receipt_url":
        record["requested_url"] = "https://sso.agc.gov.sg/Browse/SL/Revoked"
        page.write_text(json.dumps(record))
    else:
        second_receipt = page.parent / "page-00001.json"
        second_receipt.unlink()
        (page.parent / "page-00001.html.gz").unlink()
        changed = second.replace(b"40 results in 2 pages", b"41 results in 3 pages")
        monkeypatch.setattr(collector_module, "_download", lambda url, **kw: response(url, changed))
    result = collector_module.CatalogCollector(tmp_path, plan(), ["sl-current"], page_size=20).run()
    assert result["catalogues_complete"] is False
    assert result["catalogs"][0]["status"] == "incomplete"
