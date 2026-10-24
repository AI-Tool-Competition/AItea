"""Prepare a project-local, model-free ID/SG fleet and its systemd user unit.

Preparation is idempotent and never edits an existing fleet or collection state.
Activate the resulting unit explicitly with systemctl --user, as documented.
"""

from __future__ import annotations

import json
import argparse
import os
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.configuration.paths import InstancePaths  # noqa: E402

from collection_watchdog import read_config, render_unit, status
from official_source_supervisor_recovery import load, value_digest, write


def prepare(project: Path, uv: Path):
    project, uv = project.resolve(), uv.resolve()
    directory = project / ".dev/collection-service"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = InstancePaths.resolve(project)
    fleet = paths.collection_fleet
    fleet.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = paths.collection_state
    config = {
        "protocol": "aitea-collection-fleet-v1",
        "project_root": str(project),
        "tasks": [
            {
                "id": "official-" + country.lower(),
                "command": [
                    str(uv),
                    "run",
                    "--project",
                    str(project / "backend"),
                    "--offline",
                    "--no-sync",
                    "python",
                    str(project / "scripts/unattended_collection.py"),
                    "run",
                    "--pool",
                    str(paths.source_pool),
                    "--root",
                    str(paths.data / "official-updates" / country),
                    "--country",
                    country,
                ],
                "cwd": str(project),
                "enabled": True,
                "restart_seconds": 30,
                "max_restart_seconds": 900,
            }
            for country in ("ID", "SG")
        ],
    }
    config["config_hash"] = value_digest(config)
    if fleet.exists():
        if load(fleet) != config:
            try:
                existing = read_config(fleet)
            except ValueError as error:
                raise ValueError("Existing fleet differs; invalid immutable configuration") from error
            if existing["project_root"] != str(project):
                raise ValueError("Existing fleet differs; project migration required")
            config = existing
    else:
        write(fleet, config, exclusive=True)
    read_config(fleet)
    unit = directory / "aitea-collection.service"
    content = render_unit(fleet, state, uv)
    if unit.exists() and unit.read_text() != content:
        raise ValueError("Existing unit differs; preserve it and explicitly review the upgrade")
    if not unit.exists():
        with unit.open("x") as stream:
            stream.write(content)
    return {
        "fleet": str(fleet),
        "state_dir": str(state),
        "unit": str(unit),
        "config_hash": config["config_hash"],
    }


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    if args.status:
        print(
            json.dumps(
                status(
                    InstancePaths.resolve(project).collection_fleet,
                    InstancePaths.resolve(project).collection_state,
                ),
                ensure_ascii=False,
            )
        )
        raise SystemExit(0)
    executable = shutil.which("uv")
    if not executable:
        raise SystemExit("uv must be installed before preparing the collection service")
    print(json.dumps(prepare(project, Path(executable)), ensure_ascii=False))
