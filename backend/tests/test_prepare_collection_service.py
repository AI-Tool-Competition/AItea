"""Preparing unattended collection preserves existing operational configuration."""

import importlib
import shutil
import sys
from pathlib import Path

import pytest
from app.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
sys.path.insert(0, str(ROOT / "scripts"))
prepare = importlib.import_module("prepare_collection_service")


@pytest.fixture
def project(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend/pyproject.toml").touch()
    (tmp_path / "scripts").mkdir()
    for name in ("collection_watchdog.py", "unattended_collection.py"):
        (tmp_path / "scripts" / name).touch()
    (tmp_path / "deploy/collection").mkdir(parents=True)
    shutil.copyfile(
        ROOT / "deploy/collection/aitea-collection.service",
        tmp_path / "deploy/collection/aitea-collection.service",
    )
    return tmp_path


@pytest.fixture
def uv(project):
    executable = project / "uv"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return executable


def test_prepare_is_idempotent_and_never_initializes_or_resets_evidence(project, uv):
    result = prepare.prepare(project, uv)
    fleet = Path(result["fleet"])
    before = fleet.read_bytes()
    assert prepare.prepare(project, uv) == result
    assert fleet.read_bytes() == before
    config = prepare.read_config(fleet)
    assert {t["id"] for t in config["tasks"]} == {"official-id", "official-sg"}
    assert not (project / "data").exists()
    assert not Path(result["state_dir"]).exists()
    assert "Restart=always" in Path(result["unit"]).read_text()


def test_prepare_refuses_to_replace_edited_fleet(project, uv):
    result = prepare.prepare(project, uv)
    fleet = Path(result["fleet"])
    config = prepare.load(fleet)
    config["tasks"][0]["enabled"] = False
    prepare.write(fleet, config)
    before = fleet.read_bytes()
    with pytest.raises(ValueError, match="Existing fleet differs"):
        prepare.prepare(project, uv)
    assert fleet.read_bytes() == before
