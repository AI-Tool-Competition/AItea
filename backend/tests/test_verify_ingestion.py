"""The opt-in verifier never touches a populated data directory or implicit model keys."""

import json
import subprocess
import sys
from pathlib import Path

from app.configuration.paths import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "scripts/verify_ingestion.py"


def invoke(tmp_path, plan, *options):
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan))
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--plan", str(path), "--output", str(tmp_path / "result"), *options],
        capture_output=True,
        text=True,
        timeout=35,
    )


def test_local_verification_exercises_dedup_restart_and_tier_gates(tmp_path):
    main = tmp_path / "main.txt"
    main.write_text("importpermitunique Tea import requires a permit.")
    secondary = tmp_path / "reference.txt"
    secondary.write_text("referencesecret This is an associated reference.")
    plan = {
        "country": "ID",
        "samples": [
            {"id": "main", "path": str(main), "level": "primary"},
            {"id": "ref", "path": str(secondary), "level": "secondary", "primary_sample_id": "main"},
        ],
        "queries": [{"text": "importpermitunique", "expected_sample_ids": ["main"]}],
    }
    result = invoke(tmp_path, plan, "--time-budget", "30")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "result/report.json").read_text())
    assert report["status"] == "passed"
    assert report["legal_review"] == "isolated_demo_only_not_legal_approval"
    assert all(sample["duplicate_same_document"] for sample in report["samples"])
    assert report["validation_scope"] == "ingestion_only"
    assert report["retrieval_verified"] is False
    assert report["queries"][0]["expected_recalled"] is False
    assert report["queries"][0]["restart_consistent"]
    assert report["queries"][0]["restart"]["hits"] == report["queries"][0]["hits"]
    assert report["queries"][0]["restart"]["mode"] == "unavailable"
    assert "RAGFlow" in report["queries"][0]["restart"]["warning"]
    assert report["queries"][0]["restart"]["reranked"] is False
    assert (
        report["gates"]["draft_excluded"]
        and report["gates"]["secondary_excluded"]
        and report["gates"]["country_excluded"]
    )
    assert all(sample["vectors"] == 0 for sample in report["samples"])
    assert Path(report["inventory"]["path"]).is_dir()


def test_verifier_refuses_nonempty_output_and_incomplete_live_opt_in(tmp_path):
    sample = tmp_path / "main.txt"
    sample.write_text("tea")
    plan = {"samples": [{"id": "one", "path": str(sample), "level": "primary"}], "queries": []}
    output = tmp_path / "result"
    output.mkdir()
    untouched = output / "user-data.txt"
    untouched.write_text("keep")
    result = invoke(tmp_path, plan)
    assert result.returncode != 0 and untouched.read_text() == "keep"
    assert not (output / "tea.sqlite3").exists()
    untouched.unlink()
    result = invoke(tmp_path, plan, "--live-models")
    assert result.returncode != 0
    assert not (output / "tea.sqlite3").exists()


def test_verifier_rejects_oversize_plan_before_creating_database(tmp_path):
    result = invoke(
        tmp_path,
        {
            "samples": [{"id": f"item-{i}", "path": "missing", "level": "primary"} for i in range(21)],
            "queries": [],
        },
    )
    assert result.returncode != 0
    assert not (tmp_path / "result/tea.sqlite3").exists()


def test_verifier_preserves_original_filename_for_content_addressed_samples(tmp_path):
    original = tmp_path / ("a" * 64)
    original.write_text("archiverefunique tea export permit")
    plan = {
        "samples": [{"id": "archive", "path": str(original), "filename": "official-law.txt"}],
        "queries": [{"text": "archiverefunique", "expected_sample_ids": ["archive"]}],
    }
    result = invoke(tmp_path, plan, "--time-budget", "30")
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((tmp_path / "result/report.json").read_text())
    assert report["samples"][0]["filename"] == "official-law.txt"
    assert report["samples"][0]["complete"]
    assert report["queries"][0]["restart_consistent"]


def test_verifier_rejects_paths_in_display_filenames(tmp_path):
    original = tmp_path / "original.txt"
    original.write_text("tea")
    result = invoke(tmp_path, {"samples": [{"id": "bad", "path": str(original), "filename": "../law.pdf"}]})
    assert result.returncode != 0
    assert not (tmp_path / "result/tea.sqlite3").exists()


def test_disabled_models_cannot_be_restored_by_later_dotenv_imports(tmp_path):
    configuration = tmp_path / "providers.env"
    values = {
        f"{prefix}_{field}": "explicit-test-value"
        for prefix in ("LLM", "EMBEDDING", "RERANK", "VISION_OCR")
        for field in ("BASE_URL", "API_KEY", "MODEL")
    }
    values.update(
        {
            "MINERU_API_TOKEN": "explicit-test-value",
            "RAGFLOW_BASE_URL": "http://127.0.0.1:19380",
            "RAGFLOW_API_KEY": "explicit-test-value",
            "RAGFLOW_EMBEDDING_MODEL": "fixture",
        }
    )
    values["VISION_OCR_ENABLED"] = "true"
    configuration.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    code = """
import os, sys
from dotenv import load_dotenv
sys.path.insert(0, sys.argv[1])
from verify_ingestion import configure_models
configure_models(None)
load_dotenv(sys.argv[2])
for prefix in ('LLM', 'EMBEDDING', 'RERANK', 'VISION_OCR'):
    for field in ('BASE_URL', 'API_KEY', 'MODEL'):
        assert os.environ[prefix + '_' + field] == ''
assert os.environ['VISION_OCR_ENABLED'] == 'false'
assert os.environ['MINERU_API_TOKEN'] == os.environ['RAGFLOW_API_KEY'] == ''
configure_models(sys.argv[2])
assert os.environ['EMBEDDING_API_KEY'] == 'explicit-test-value'
assert os.environ['VISION_OCR_ENABLED'] == 'true'
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(SCRIPT.parent), str(configuration)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
