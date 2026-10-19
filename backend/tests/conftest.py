"""Keep local provider credentials out of deterministic application tests."""

import pytest


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch):
    # Empty values also prevent load_dotenv from filling credentials on import.
    # Provider contract tests explicitly replace these with mocked settings.
    for prefix in ("LLM", "EMBEDDING", "RERANK", "VISION"):
        for field in ("BASE_URL", "API_KEY", "MODEL"):
            monkeypatch.setenv(f"{prefix}_{field}", "")
    monkeypatch.setenv("VISION_OCR_ENABLED", "false")
    monkeypatch.setenv("VISION_OCR_WORKERS", "2")
    monkeypatch.setenv("VISION_OCR_ADAPTIVE", "false")
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "64")
    # Real deployment quotas must never be opened by fake-model unit tests.
    # An empty value also prevents a later dotenv import from filling the path.
    monkeypatch.setenv("EMBEDDING_ADMISSION_PATH", "")
    monkeypatch.setenv("EMBEDDING_HTTP_WORKERS", "2")
    monkeypatch.setenv("EMBEDDING_REQUEST_CONCURRENCY", "1")
    monkeypatch.setenv("TEA_REQUIRE_EXISTING_DATA", "false")


@pytest.fixture
def sample_workflow(monkeypatch):
    """Explicit compact fixture for generic engine tests, never a shipped route."""
    import json
    from pathlib import Path

    from app import auth, service
    from app.models import Template

    sample = Template.model_validate(
        json.loads((Path(__file__).parent / "fixtures/minimal-workflow.json").read_text())
    ).model_dump()
    monkeypatch.setattr(auth, "DEFAULT_TEMPLATE", sample)
    monkeypatch.setattr(service, "BUILTIN_TEMPLATES", {sample["id"]: sample})
