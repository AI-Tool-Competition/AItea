"""Keep local provider credentials out of deterministic application tests."""

import pytest


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch):
    # Empty values also prevent load_dotenv from filling credentials on import.
    # Provider contract tests explicitly replace these with mocked settings.
    for prefix in ("LLM", "EMBEDDING", "RERANK", "VISION"):
        for field in ("BASE_URL", "API_KEY", "MODEL"):
            monkeypatch.setenv(f"{prefix}_{field}", "")
    for key in (
        "MINERU_API_TOKEN",
        "RAGFLOW_BASE_URL",
        "RAGFLOW_API_KEY",
        "RAGFLOW_EMBEDDING_MODEL",
        "RAGFLOW_RERANK_MODEL",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("VISION_OCR_ENABLED", "false")
    monkeypatch.setenv("VISION_OCR_WORKERS", "2")
    monkeypatch.delenv("VISION_OCR_REQUEST_WORKERS", raising=False)
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", "false")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "64")
    # Real deployment quotas must never be opened by fake-model unit tests.
    # An empty value also prevents a later dotenv import from filling the path.
    monkeypatch.setenv("EMBEDDING_ADMISSION_PATH", "")
    monkeypatch.setenv("EMBEDDING_HTTP_WORKERS", "2")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "1")
    monkeypatch.setenv("TEA_REQUIRE_EXISTING_DATA", "false")
    from app.contexts.knowledge.infrastructure.parsing import parsing as parsing

    monkeypatch.setattr(
        parsing,
        "_MODEL_REQUEST_SLOTS",
        parsing._ParserSlots(
            limit_reader=parsing.vision_ocr_request_worker_limit, setting="VISION_OCR_REQUEST_WORKERS"
        ),
    )


@pytest.fixture
def sample_workflow(monkeypatch):
    """Explicit compact fixture for generic engine tests, never a shipped route."""
    import json
    from pathlib import Path

    from app.contexts.identity.application import auth as auth
    from app.contexts.workspace.application import configurations
    from app.contexts.workspace.domain.models import Template

    sample = Template.model_validate(
        json.loads((Path(__file__).parent / "fixtures/minimal-workflow.json").read_text())
    ).model_dump()
    monkeypatch.setattr(auth, "DEFAULT_TEMPLATE", sample)
    monkeypatch.setattr(configurations, "BUILTIN_TEMPLATES", {sample["id"]: sample})


@pytest.fixture
def ragflow_library(isolated_providers, monkeypatch):
    """Application policy tests run with an explicitly completed fake remote index."""
    from app.contexts.knowledge.application.retrieval import RetrievalIndex
    from support.ragflow import MemoryRAGFlow, finish_indexes

    for key, value in {
        "RAGFLOW_BASE_URL": "http://127.0.0.1:19380",
        "RAGFLOW_API_KEY": "fixture-only",
        "RAGFLOW_EMBEDDING_MODEL": "fixture-model",
    }.items():
        monkeypatch.setenv(key, value)
    remote = MemoryRAGFlow()
    monkeypatch.setattr(
        "app.contexts.knowledge.infrastructure.providers.ragflow.RAGFlowClient", lambda: remote
    )
    prepare = RetrievalIndex.prepare_scopes

    def before_snapshot(self, *args, **kwargs):
        finish_indexes(self.service)
        return prepare(self, *args, **kwargs)

    monkeypatch.setattr(RetrievalIndex, "prepare_scopes", before_snapshot)
    search = RetrievalIndex.search

    def after_worker(self, *args, **kwargs):
        finish_indexes(self.service)
        return search(self, *args, **kwargs)

    monkeypatch.setattr(RetrievalIndex, "search", after_worker)
    return remote
