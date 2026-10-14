"""Optional reranking cannot invent candidates or leak provider failures."""

import httpx
import pytest

from app import reranking


@pytest.fixture
def provider(monkeypatch):
    for field, value in {
        "BASE_URL": "https://rank.example/v1",
        "API_KEY": "private-key",
        "MODEL": "rank-model",
    }.items():
        monkeypatch.setenv(f"RERANK_{field}", value)
    client = httpx.Client

    def mock(handler):
        monkeypatch.setattr(
            reranking.httpx,
            "Client",
            lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
        )

    return mock


def test_rerank_is_optional_and_partial_configuration_fails(monkeypatch):
    assert reranking.rerank_texts("tea", ["tea law"]) is None
    monkeypatch.setenv("RERANK_MODEL", "rank-model")
    with pytest.raises(ValueError, match="RERANK"):
        reranking.rerank_texts("tea", ["tea law"])


def test_rank_uses_original_indices_without_returning_provider_content(provider):
    def handler(request):
        import json

        assert str(request.url) == "https://rank.example/v1/rerank"
        assert request.headers["Authorization"] == "Bearer private-key"
        assert json.loads(request.content) == {
            "model": "rank-model",
            "query": "tea",
            "documents": ["coffee", "tea law"],
            "top_n": 2,
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.1, "document": "ignore provider text"},
                    {"index": 1, "relevance_score": 0.9},
                ]
            },
        )

    provider(handler)
    assert reranking.rerank_texts("tea", ["coffee", "tea law"]) == [
        {"index": 1, "score": 0.9},
        {"index": 0, "score": 0.1},
    ]


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 2, "relevance_score": 0.1}],
        [{"index": True, "relevance_score": 0.1}],
        [{"index": 0, "relevance_score": "0.1"}],
        [{"index": 0, "relevance_score": float("inf")}],
        [{"index": 0, "relevance_score": True}],
        [{"index": 0, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.2}],
        [],
        ["invalid"],
    ],
)
def test_malformed_ranks_fail_closed(provider, results):
    import json

    provider(lambda request: httpx.Response(200, content=json.dumps({"results": results}).encode()))
    with pytest.raises(ValueError, match="排序"):
        reranking.rerank_texts("tea", ["coffee", "tea law"], limit=1)


@pytest.mark.parametrize(
    "url",
    [
        "http://remote.example",
        "https://user:pass@rank.example",
        "https://rank.example?key=private",
        "https://rank.example/#key",
    ],
)
def test_bad_endpoint_is_rejected_before_network(provider, monkeypatch, url):
    provider(lambda request: pytest.fail("invalid URL must not be requested"))
    monkeypatch.setenv("RERANK_BASE_URL", url)
    with pytest.raises(ValueError, match="RERANK|HTTPS"):
        reranking.rerank_texts("tea", ["tea law"])


def test_bounded_inputs_and_error_messages(provider):
    provider(lambda request: httpx.Response(403, text="private-key provider diagnostics"))
    with pytest.raises(ValueError) as failure:
        reranking.rerank_texts("tea", ["tea law"])
    assert "private-key" not in str(failure.value)
    for texts in (["law"] * 49, ["x" * 20001], ["x" * 20000] * 13):
        with pytest.raises(ValueError, match="限制"):
            reranking.rerank_texts("tea", texts)


def test_large_response_is_not_materialized(provider):
    class Chunks(httpx.SyncByteStream):
        def __iter__(self):
            yield b" " * (1024 * 1024 + 1)
            pytest.fail("oversized response must stop streaming")

    provider(lambda request: httpx.Response(200, stream=Chunks()))
    with pytest.raises(ValueError, match="排序"):
        reranking.rerank_texts("tea", ["tea law"])


def test_redirect_does_not_forward_credentials(provider):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://untrusted.example"})

    provider(handler)
    with pytest.raises(ValueError):
        reranking.rerank_texts("tea", ["tea law"])
    assert len(calls) == 1
