"""API and existing background queues can run in separate processes."""

import pytest
from fastapi.testclient import TestClient
from app.interfaces.http.factory import create_app
from app.contexts.conversations.application.agent_tools import LawVersionInput, LegalSearchInput
from pydantic import ValidationError
from test_agent_runtime import workspace as workspace_fixture, legal_tools
from types import SimpleNamespace

workspace = workspace_fixture


def test_api_external_mode_does_not_start_queue_or_importer(tmp_path, monkeypatch):
    monkeypatch.setenv("TEA_BACKGROUND_WORKERS", "external")

    def forbidden(*a, **k):
        pytest.fail("API started a background worker")

    monkeypatch.setattr(
        "app.contexts.ingestion.application.collection_jobs.CollectionWorkers.start", forbidden
    )
    monkeypatch.setattr(
        "app.contexts.ingestion.application.official_source_import.OfficialSourceImporter.start", forbidden
    )
    # With an ordinary production-shaped app, default scheduling is external.
    monkeypatch.setenv("TEA_DATA_DIR", str(tmp_path / "store"))
    app = create_app()
    with TestClient(app) as client:
        health = client.get("/api/tea/health").json()
        assert health["collector"]["status"] == "stopped"
        assert health["collector"]["background_workers"] == "external"


@pytest.mark.parametrize("value", ["false", "disabled", "unknown"])
def test_invalid_worker_mode_rejected(tmp_path, monkeypatch, value):
    monkeypatch.setenv("TEA_BACKGROUND_WORKERS", value)
    with pytest.raises(ValueError, match="TEA_BACKGROUND_WORKERS"):
        create_app(data_dir=tmp_path)


def test_version_evidence_preserves_unknown_dates_and_relations(workspace, monkeypatch):
    monkeypatch.setattr(
        "app.contexts.documents.infrastructure.country_packs.CountryPacks.read",
        lambda *a: {
            "title": "已入库测试法规",
            "document_version": 2,
            "metadata": {"review_status": "approved"},
        },
    )
    value = legal_tools(workspace).law_versions(
        LawVersionInput(jurisdiction="CN", record_id="real-record"), SimpleNamespace(check=lambda: None)
    )
    assert all(date is None for date in value["dates"].values())
    assert value["relations"] == [] and value["superseded_by"] is None
    assert "现行有效" not in value


def test_evidence_search_rejects_invalid_calendar_date():
    with pytest.raises(ValidationError):
        LegalSearchInput(jurisdiction="CN", query="法律", as_of="2026-02-30")


@pytest.mark.parametrize("fail_importer", [False, True])
def test_worker_cli_stops_started_components_on_signal_or_start_failure(tmp_path, monkeypatch, fail_importer):
    import runpy
    from pathlib import Path

    events, signals = [], []
    root = Path(__file__).resolve().parents[2]
    module = runpy.run_path(str(root / "scripts/run_workers.py"))
    main = module["main"]
    monkeypatch.setattr("sys.argv", ["run_workers.py", "--data-dir", str(tmp_path)])
    monkeypatch.setattr("app.configuration.environment.load_environment", lambda: None)
    monkeypatch.setattr("signal.signal", lambda signum, handler: signals.append((signum, handler)))

    class Stop:
        def set(self):
            events.append("signal")

        def wait(self, seconds):
            signals[0][1](None, None)
            return True

    class Worker:
        def __init__(self, *args):
            pass

        def start(self):
            events.append("workers_start")
            return self

        def stop(self):
            events.append("workers_stop")

    class Importer(Worker):
        def start(self):
            events.append("importer_start")
            if fail_importer:
                raise RuntimeError("importer unavailable")
            return self

        def stop(self):
            events.append("importer_stop")

    def service(path, *, require_existing):
        assert path == tmp_path and require_existing
        return SimpleNamespace(collection=object())

    main.__globals__["Event"] = Stop
    monkeypatch.setattr("app.bootstrap.Service", service)
    monkeypatch.setattr("app.contexts.ingestion.application.collection_jobs.CollectionWorkers", Worker)
    monkeypatch.setattr(
        "app.contexts.ingestion.application.official_source_import.OfficialSourceImporter", Importer
    )
    monkeypatch.setattr(
        "app.shared.infrastructure.operations.scheduler.start_scheduler",
        lambda s: events.append("scheduler_start") or object(),
    )
    monkeypatch.setattr(
        "app.shared.infrastructure.operations.scheduler.stop_scheduler",
        lambda s: events.append("scheduler_stop"),
    )
    if fail_importer:
        with pytest.raises(RuntimeError, match="importer unavailable"):
            main()
        assert events == [
            "scheduler_start",
            "workers_start",
            "importer_start",
            "scheduler_stop",
            "workers_stop",
        ]
    else:
        main()
        assert events == [
            "scheduler_start",
            "workers_start",
            "importer_start",
            "signal",
            "importer_stop",
            "scheduler_stop",
            "workers_stop",
        ]
