"""Read-only public search credentials, isolated transport, and honest counts."""

import json
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256

import pytest
from app.contexts.collection.infrastructure.discovery.egazette import (
    inspect_search_response,
    original_url,
    public_search_from_html,
    publication_classification,
    search_body,
)
from app.shared.infrastructure.http import download as ingestion_download
from app.shared.infrastructure.http.public_search import (
    EGAZETTE_APPLICATION,
    EGAZETTE_INDEX,
    EGAZETTE_ORIGIN,
    EGAZETTE_SEARCH_HOST,
    EGAZETTE_SEARCH_URL,
    EGazetteSearchRequest,
)
from test_stream_download import Response


@pytest.mark.parametrize(
    "title",
    [
        "Income Tax Act 1947",
        "The Statutes of the Republic of Singapore 2004",
        "Index of Legislation Act 2026",
        "The Statutes of the Republic of Singapore - Subject Index to Acts (Amendment)",
        "Pre-Publication Notice - Re-issue of Subject Index to Acts Act 2026",
    ],
)
def test_reference_title_rule_does_not_exclude_statutes_with_internal_indexes(title):
    result = publication_classification(
        {
            "title": title,
            "category": "Legislative Supplements",
            "subCategory": "Revised Acts",
            "page_heading": "Subject Index to Acts",
        }
    )
    assert result["level"] == "primary" and result["kind"] == "law"


@pytest.mark.parametrize(
    "category,subcategory", [("Other", "Revised Acts"), ("Legislative Supplements", "Other")]
)
def test_reference_title_rule_requires_official_legislative_category(category, subcategory):
    assert (
        publication_classification(
            {
                "title": "The Statutes of the Republic of Singapore - Subject Index to Acts",
                "category": category,
                "subCategory": subcategory,
            }
        )["level"]
        == "primary"
    )


def test_publication_notice_can_appear_in_subsidiary_supplement():
    assert (
        publication_classification(
            {
                "title": "Pre-Publication Notice - Re-Issue of Subject Index to Acts",
                "category": "Legislative Supplements",
                "subCategory": "Subsidiary Legislation Supplement",
            }
        )["level"]
        == "secondary"
    )


def config_html(*, config=None, wrapper=None):
    config = config or {
        "type": "egazette-algolia",
        "appId": EGAZETTE_APPLICATION,
        "indexName": EGAZETTE_INDEX,
        "searchApiKey": "a" * 32,
    }
    stream = "1:" + json.dumps(wrapper or {"config": config}) + "\n"
    return ("<html><script>self.__next_f.push(" + json.dumps([1, stream]) + ")</script></html>").encode()


def test_literal_official_frontend_config_preserves_proof_but_does_not_log_key():
    raw = config_html()
    request, proof = public_search_from_html(EGAZETTE_ORIGIN, raw)
    assert request.source_sha256 == sha256(raw).hexdigest()
    assert request.search_key == "a" * 32
    assert "a" * 32 not in repr(request)
    assert "a" * 32 not in json.dumps(proof)
    assert proof["config_locator"]["json_pointer"] == "/config"
    assert proof["source_role"].endswith("not_current_consolidated")


@pytest.mark.parametrize(
    "alteration", ["source", "expression", "truncated", "duplicate", "app", "index", "key"]
)
def test_frontend_rejects_unverified_config(alteration):
    raw, url = config_html(), EGAZETTE_ORIGIN
    if alteration == "source":
        url = "https://example.com/egazette-browse/"
    elif alteration == "expression":
        raw = raw.replace(b"self.__next_f.push(", b"self.__next_f.push(eval(")
    elif alteration == "truncated":
        raw = raw[:-25]
    elif alteration == "duplicate":
        raw = raw + raw
    else:
        before, after = {
            "app": (EGAZETTE_APPLICATION, "OTHERAPP12"),
            "index": (EGAZETTE_INDEX, "admin"),
            "key": ("a" * 32, "a\\r\\nHeader: value"),
        }[alteration]
        raw = raw.replace(before.encode(), after.encode())
    with pytest.raises(ValueError):
        public_search_from_html(url, raw)


def credential(key="a" * 32):
    return EGazetteSearchRequest(key, "b" * 64)


@pytest.mark.parametrize(
    "url,parameters",
    [
        ("https://example.com/1/indexes/prod_ogp_egazettes_index/query", {}),
        (EGAZETTE_SEARCH_URL + "?key=1", {}),
        (EGAZETTE_SEARCH_URL.replace("/query", "/settings"), {}),
        (EGAZETTE_SEARCH_URL, {"json_body": None}),
        (EGAZETTE_SEARCH_URL, {"validators": {"etag": '"x"'}}),
        (EGAZETTE_SEARCH_URL, {"byte_range": (0, 2)}),
        (EGAZETTE_SEARCH_URL, {"public_search": {"search_key": "a" * 32}}),
        (EGAZETTE_SEARCH_URL, {"public_search": replace(credential(), source_url="https://example.com/")}),
    ],
)
def test_credentials_are_rejected_before_dns_for_wrong_endpoint_or_method(monkeypatch, url, parameters):
    monkeypatch.setattr(
        ingestion_download, "_public_addresses", lambda *a, **k: pytest.fail("must reject before DNS")
    )
    arguments = {"json_body": search_body(), "public_search": credential(), **parameters}
    with pytest.raises(ingestion_download.DownloadError):
        ingestion_download._download(url, deadline=time.monotonic() + 5, **arguments)


@pytest.mark.parametrize("location", ["https://example.com/steal", EGAZETTE_SEARCH_URL])
def test_search_credentials_never_follow_redirects(monkeypatch, location):
    requests = []

    class Connection:
        sock = None

        def __init__(self, host, *args):
            self.host = host

        def request(self, method, path, *, headers, body):
            requests.append((self.host, method, path, headers.copy()))

        def getresponse(self):
            return Response(status=307, headers={"Location": location})

        def close(self):
            pass

    monkeypatch.setattr(
        ingestion_download,
        "_public_addresses",
        lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(ingestion_download, "_PinnedHTTPSConnection", Connection)
    with pytest.raises(ingestion_download.DownloadError, match="不允许重定向"):
        ingestion_download._download(
            EGAZETTE_SEARCH_URL,
            deadline=time.monotonic() + 5,
            json_body=search_body(),
            public_search=credential(),
        )
    assert len(requests) == 1 and requests[0][0] == EGAZETTE_SEARCH_HOST


def test_parallel_search_credentials_do_not_cross_requests_or_leak_into_ordinary_download(monkeypatch):
    barrier, requests = threading.Barrier(3), []

    class Connection:
        sock = None

        def __init__(self, host, *args):
            self.host = host

        def request(self, method, path, *, headers, body=None):
            self.record = (self.host, method, headers.copy(), json.loads(body) if body else None)
            barrier.wait(timeout=5)
            requests.append(self.record)

        def getresponse(self):
            return Response([b"{}"], headers={"Content-Type": "application/json", "Content-Length": "2"})

        def close(self):
            pass

    monkeypatch.setattr(
        ingestion_download,
        "_public_addresses",
        lambda *a, **k: [(socket.AF_INET, 1, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(ingestion_download, "_PinnedHTTPSConnection", Connection)

    def run(index):
        return ingestion_download._download(
            EGAZETTE_SEARCH_URL if index < 2 else "https://assets.egazette.gov.sg/public.pdf",
            deadline=time.monotonic() + 10,
            **(
                {"json_body": search_body(query=str(index)), "public_search": credential(str(index) * 32)}
                if index < 2
                else {}
            ),
        )

    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(run, range(3)))
    assert all(result["content"] == b"{}" for result in results)
    for host, method, headers, body in requests:
        if body:
            assert method == "POST" and host == EGAZETTE_SEARCH_HOST
            assert headers["X-Algolia-API-Key"] == body["query"] * 32
        else:
            assert method == "GET" and "X-Algolia-API-Key" not in headers
    assert all("search_key" not in result for result in results)


def response(*, total=2, pages=1, records=2, exhaustive=True):
    return json.dumps(
        {
            "page": 0,
            "nbHits": total,
            "nbPages": pages,
            "hitsPerPage": 100,
            "exhaustiveNbHits": exhaustive,
            "hits": [
                {
                    "objectID": f"foo-text-{i}",
                    "title": "Example Act",
                    "fileUrl": "https://assets.egazette.gov.sg/2021/Legislative%20Supplements/Revised%20Acts/Example.pdf",
                }
                for i in range(records)
            ],
        }
    ).encode()


def test_records_from_same_document_are_kept_with_distinct_locators_and_count_unit():
    result = inspect_search_response(response(), search_body())
    assert result["source_index_records"] == 2
    assert len({row["download_url"] for row in result["rows"]}) == 1
    assert result["rows"][0]["source_locator"]["json_pointer"] == "/hits/0"
    assert result["count_unit"] == "search_index_record_not_unique_document"
    assert not result["current_effect_verified"]


def test_search_window_and_approximate_counts_cannot_prove_complete_directory():
    result = inspect_search_response(response(total=1500, pages=10, records=100), search_body())
    assert not result["pagination_window_complete"]
    result = inspect_search_response(response(exhaustive=False), search_body())
    assert not result["exhaustive_source_count"]
    with pytest.raises(ValueError, match="row count"):
        inspect_search_response(response(total=3), search_body())


@pytest.mark.parametrize(
    "url",
    [
        "http://assets.egazette.gov.sg/2021/Legislative Supplements/Acts/foo.pdf",
        "https://assets.egazette.gov.sg.evil.test/2021/Legislative Supplements/Acts/foo.pdf",
        "https://assets.egazette.gov.sg/2021/Legislative Supplements/Acts/../foo.pdf",
        "https://assets.egazette.gov.sg/2021/Legislative Supplements/Acts/%252e%252e.pdf",
        "https://assets.egazette.gov.sg/2021/Legislative Supplements/Acts/foo.pdf?key=1",
        "https://assets.egazette.gov.sg/2021/Legislative Supplements/Acts/foo.html",
    ],
)
def test_original_download_mapping_rejects_unverified_paths(url):
    with pytest.raises(ValueError):
        original_url(url)
