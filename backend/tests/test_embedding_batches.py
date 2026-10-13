"""Provider batch tuning preserves input order and durable processing checkpoints."""

import json

import httpx
import pytest

from app import ingestion


@pytest.fixture
def configured(monkeypatch):
    for key, value in {"BASE_URL": "https://embed.example/v1", "MODEL": "embed", "API_KEY": "test"}.items():
        monkeypatch.setenv("EMBEDDING_" + key, value)


def test_configured_small_batches_preserve_response_order(configured, monkeypatch):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "2")
    requests = []

    def handler(request):
        values = json.loads(request.content)["input"]
        requests.append(values)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": [float(value), 1.0]}
                    for i, value in reversed(list(enumerate(values)))
                ]
            },
        )

    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    assert ingestion.embed_texts(["1", "2", "3", "4", "5"]) == [[float(i), 1.0] for i in range(1, 6)]
    assert requests == [["1", "2"], ["3", "4"], ["5"]]


@pytest.mark.parametrize("size", ["0", "65", "invalid"])
def test_invalid_batch_configuration_never_calls_provider(configured, monkeypatch, size):
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", size)
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("invalid configuration called provider"))
    with pytest.raises(ValueError, match="EMBEDDING_BATCH_SIZE"):
        ingestion.embed_texts(["text"])
