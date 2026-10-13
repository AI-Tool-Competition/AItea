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
    monkeypatch.setenv("EMBEDDING_BATCH_SIZE", "64")
