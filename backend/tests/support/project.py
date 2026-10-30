"""Minimal real configuration package for standalone lifecycle subprocesses."""

import shutil
from app.configuration.paths import PROJECT_ROOT


def copy_configuration(project):
    shutil.copytree(
        PROJECT_ROOT / "backend/app/configuration",
        project / "backend/app/configuration",
        ignore=shutil.ignore_patterns("__pycache__"),
        dirs_exist_ok=True,
    )
    infrastructure = project / "backend/app/shared/infrastructure"
    infrastructure.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / "backend/app/shared/infrastructure/darwin_process.py", infrastructure)
