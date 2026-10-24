"""Prepare a project-local, model-free ID/SG fleet and its systemd user unit.

Preparation is idempotent and never edits an existing fleet or collection state.
Activate the resulting unit explicitly with systemctl --user, as documented.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from collection_watchdog import read_config, render_unit
from official_source_supervisor_recovery import load, value_digest, write


def prepare(project: Path, uv: Path):
    project, uv = project.resolve(), uv.resolve()
    directory = project / ".dev/collection-service"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fleet = directory / "fleet.json"
    state = directory / "state"
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
                    str(project / "config/collection-sources/id-sg-unattended.json"),
                    "--root",
                    str(project / "data/official-updates" / country),
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
            raise ValueError("Existing fleet differs; preserve its state and review a migration")
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
    executable = shutil.which("uv")
    if not executable:
        raise SystemExit("uv must be installed before preparing the collection service")
    print(json.dumps(prepare(Path(__file__).resolve().parents[1], Path(executable)), ensure_ascii=False))
