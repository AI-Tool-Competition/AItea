import json
from contextlib import closing

import httpx
import pytest
from fastapi.testclient import TestClient

from app.ingestion import rerank_chunks


def configure(monkeypatch):
    for field, value in {
        "BASE_URL": "https://provider.invalid/v1",
        "API_KEY": "test-only",
        "MODEL": "reranker",
    }.items():
        monkeypatch.setenv(f"RERANK_{field}", value)


def mock_provider(monkeypatch, handler):
    client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs)
    )


def test_unconfigured_rerank_skips_provider(monkeypatch):
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("unconfigured provider must not run"))
    assert rerank_chunks("tea", [{"text": "tea"}]) is None


def test_rerank_partial_configuration_fails(monkeypatch):
    monkeypatch.setenv("RERANK_MODEL", "reranker")
    with pytest.raises(ValueError, match="RERANK_BASE_URL"):
        rerank_chunks("tea", [{"text": "tea"}])


def test_rerank_sorts_indices_and_keeps_original_evidence(monkeypatch):
    configure(monkeypatch)
    chunks = [
        {"text": "tea one", "document_id": "owned-1", "document_version": 3, "locator": {"page": 2}},
        {"text": "tea two", "document_id": "owned-2", "document_version": 1},
    ]

    def handler(request):
        assert request.url.path == "/v1/rerank"
        assert json.loads(request.content) == {
            "model": "reranker",
            "query": "tea",
            "documents": ["tea one", "tea two"],
            "top_n": 2,
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 0, "relevance_score": 0.1, "document": {"text": "forged"}},
                    {"index": 1, "relevance_score": 0.9},
                ]
            },
        )

    mock_provider(monkeypatch, handler)
    hits = rerank_chunks("tea", chunks, 2)
    assert [hit["document_id"] for hit in hits] == ["owned-2", "owned-1"]
    assert hits[1]["text"] == "tea one" and hits[1]["locator"] == {"page": 2}
    assert hits[1]["document_version"] == 3
    assert "rerank_score" not in chunks[0]


@pytest.mark.parametrize(
    "results",
    [
        [],
        [{"index": 0, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.9}],
        [{"index": 0, "relevance_score": 0.1}, {"index": 2, "relevance_score": 0.9}],
        [{"index": 0, "relevance_score": 0.1}, {"index": True, "relevance_score": 0.9}],
        [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": True}],
        [{"index": 0, "relevance_score": 0.1}, {"index": 1, "relevance_score": "high"}],
    ],
)
def test_invalid_rerank_response_cannot_create_or_duplicate_evidence(monkeypatch, results):
    configure(monkeypatch)
    mock_provider(monkeypatch, lambda request: httpx.Response(200, json={"results": results}))
    with pytest.raises(ValueError, match="Rerank"):
        rerank_chunks("tea", [{"text": "tea one"}, {"text": "tea two"}], 2)


def test_rerank_rejects_remote_http_and_oversized_candidates(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: pytest.fail("invalid input must not reach provider")
    )
    monkeypatch.setenv("RERANK_BASE_URL", "http://provider.invalid/v1")
    with pytest.raises(ValueError, match="HTTPS"):
        rerank_chunks("tea", [{"text": "tea"}])
    monkeypatch.setenv("RERANK_BASE_URL", "https://provider.invalid/v1")
    with pytest.raises(ValueError, match="输入"):
        rerank_chunks("tea", [{"text": "tea"}] * 61)


def test_rerank_response_size_is_bounded(monkeypatch):
    configure(monkeypatch)
    mock_provider(monkeypatch, lambda request: httpx.Response(200, content=b"x" * (1024 * 1024 + 1)))
    with pytest.raises(ValueError, match="Rerank"):
        rerank_chunks("tea", [{"text": "tea"}])


@pytest.fixture
def client(tmp_path):
    from app.main import create_app

    with TestClient(create_app(data_dir=tmp_path)) as client:
        assert client.post("/api/tea/auth/demo", json={}).status_code == 200
        yield client


def seed_candidates(client):
    bootstrap = client.get("/api/tea/bootstrap").json()
    case = client.get("/api/tea/cases/" + bootstrap["cases"][0]["id"]).json()
    visible = []
    for index in range(3):
        doc = client.post(
            "/api/tea/documents",
            data={"case_id": case["id"]},
            files={"file": (f"visible-{index}.txt", f"tea export visible {index}".encode(), "text/plain")},
        )
        assert doc.status_code == 201
        visible.append(doc.json())
    other = client.post(
        "/api/tea/cases",
        json={
            "title": "Other case",
            "company": "Other",
            "product": "Tea",
            "mode_id": "mode-a",
            "template_id": case["template"]["id"],
        },
    )
    assert other.status_code == 201
    private = client.post(
        "/api/tea/documents",
        data={"case_id": other.json()["id"]},
        files={"file": ("private.txt", b"tea export PRIVATE other case", "text/plain")},
    )
    assert private.status_code == 201
    return case, visible


def test_search_reranks_only_eligible_candidates_and_preserves_citations(client, monkeypatch):
    case, visible = seed_candidates(client)
    configure(monkeypatch)
    selected = {}

    def handler(request):
        documents = json.loads(request.content)["documents"]
        assert len(documents) == 3 and all("PRIVATE" not in text for text in documents)
        selected["text"] = documents[-1]
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": len(documents) - 1, "relevance_score": 0.9, "document": {"text": "forged"}}
                ]
            },
        )

    mock_provider(monkeypatch, handler)
    response = client.post("/api/tea/search", json={"query": "tea export", "case_id": case["id"], "limit": 1})
    assert response.status_code == 200
    result = response.json()
    expected = visible[int(selected["text"].rsplit(" ", 1)[-1])]
    assert result["reranked"] is True and result["mode"] == "lexical"
    assert result["items"][0]["document_id"] == expected["id"]
    assert result["items"][0]["document_version"] == expected["version"]
    assert result["items"][0]["excerpt"] == selected["text"]


def test_search_keeps_original_ranking_and_reports_provider_failure(client, monkeypatch):
    case, _ = seed_candidates(client)
    request = {"query": "tea export", "case_id": case["id"], "limit": 2}
    original = client.post("/api/tea/search", json=request).json()
    configure(monkeypatch)
    mock_provider(monkeypatch, lambda request: httpx.Response(503, text="private provider error"))
    result = client.post("/api/tea/search", json=request).json()
    assert result["items"] == original["items"] and result["reranked"] is False
    assert "重排服务暂不可用" in result["warning"] and "private provider error" not in result["warning"]


def test_rerank_configuration_invalidates_saved_analysis_inputs(client, monkeypatch):
    service = client.app.state.service
    case_id = client.get("/api/tea/bootstrap").json()["cases"][0]["id"]
    owner = client.get("/api/tea/auth/me").json()["id"]
    with closing(service.db.connect()) as conn:
        case = service.case_detail(owner, case_id, conn)
        before = service.studio.fingerprint(conn, owner, case)
        legal_before = service.legal.snapshot(conn, owner, case)
        configure(monkeypatch)
        assert service.studio.fingerprint(conn, owner, case) != before
        assert service.legal.snapshot(conn, owner, case) != legal_before
