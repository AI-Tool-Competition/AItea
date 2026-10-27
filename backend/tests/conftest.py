"""Keep local provider credentials out of deterministic application tests."""

import os
from pathlib import Path

import pytest

# Factories and workflow defaults are imported during collection, before any
# fixture can clear credentials. Never select the running instance's config.
os.environ["TEA_ENV_FILE"] = str(Path(__file__).parent / "fixtures/environment.env")


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch):
    # Individual path/config tests and their local subprocess fixtures select
    # their own project. The app defaults have already loaded the empty file.
    monkeypatch.delenv("TEA_ENV_FILE", raising=False)
    # Empty values also prevent load_dotenv from filling credentials on import.
    # Provider contract tests explicitly replace these with mocked settings.
    for prefix in ("LLM", "AGENT", "EMBEDDING", "RERANK", "VISION"):
        for field in ("BASE_URL", "API_KEY", "MODEL"):
            monkeypatch.setenv(f"{prefix}_{field}", "")
    monkeypatch.setenv("AGENT_TOOLS_BASE_URL", "")
    monkeypatch.setenv("AGENT_REASONING_EFFORT", "")
    for key in (
        "MINERU_API_TOKEN",
        "RAGFLOW_BASE_URL",
        "RAGFLOW_API_KEY",
        "RAGFLOW_EMBEDDING_MODEL",
        "RAGFLOW_RERANK_MODEL",
    ):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("VISION_OCR_ENABLED", "false")
    monkeypatch.setenv("VISION_OCR_PROVIDER", "openai")
    monkeypatch.setenv("VISION_OCR_URL", "")
    monkeypatch.setenv("VISION_OCR_API_KEY", "")
    monkeypatch.setenv("SUB2API_API_KEY", "")
    monkeypatch.setenv("VISION_OCR_WORKERS", "2")
    monkeypatch.setenv("VISION_OCR_REQUEST_TIMEOUT_SECONDS", "180")
    monkeypatch.delenv("VISION_OCR_REQUEST_WORKERS", raising=False)
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", "false")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "64")
    # Real deployment quotas must never be opened by fake-model unit tests.
    # An empty value also prevents a later dotenv import from filling the path.
    monkeypatch.setenv("EMBEDDING_ADMISSION_PATH", "")
    monkeypatch.setenv("EMBEDDING_HTTP_WORKERS", "2")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "1")
    monkeypatch.setenv("TEA_REQUIRE_EXISTING_DATA", "false")
    monkeypatch.setenv("TEA_PROCESS_WORKERS", "2")
    monkeypatch.setenv("TEA_PARSE_WORKERS", "2")
    monkeypatch.setenv("TEA_EMBED_WORKERS", "2")
    # Collection transport tests use local mock servers and set proxy policy
    # explicitly; never inherit a developer's real source allowlist.
    for key in ("COLLECTION_PROXY_URL", "COLLECTION_PROXY_HOSTS", "COLLECTION_DOH_HOSTS"):
        monkeypatch.setenv(key, "")
    from app.contexts.documents.infrastructure.parsers import document_parser as parsing

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

    from app.contexts.accounts.application import auth as auth
    from app.contexts.workflows.application import configurations
    from app.contexts.workflows.domain.models import Template

    sample = Template.model_validate(
        json.loads((Path(__file__).parent / "fixtures/minimal-workflow.json").read_text())
    ).model_dump()
    monkeypatch.setattr(auth, "DEFAULT_TEMPLATE", sample)
    monkeypatch.setattr(configurations, "BUILTIN_TEMPLATES", {sample["id"]: sample})


@pytest.fixture
def ragflow_library(isolated_providers, monkeypatch):
    """Application policy tests run with an explicitly completed fake remote index."""
    from app.contexts.documents.application.retrieval import RetrievalIndex
    from support.ragflow import MemoryRAGFlow, finish_indexes

    for key, value in {
        "RAGFLOW_BASE_URL": "http://127.0.0.1:19380",
        "RAGFLOW_API_KEY": "fixture-only",
        "RAGFLOW_EMBEDDING_MODEL": "fixture-model",
    }.items():
        monkeypatch.setenv(key, value)
    remote = MemoryRAGFlow()
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.providers.ragflow.RAGFlowClient", lambda: remote
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
