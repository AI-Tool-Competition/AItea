"""Single path contract for the API, workers and operational commands.

Durable state belongs to the instance data root; disposable process state stays
in the checkout's .dev directory. Resolution never creates or moves files.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_BACKEND_ROOT = Path(__file__).resolve().parents[2]
# Source checkouts and current images keep app/ under backend/; legacy flat
# images remain readable through the same explicit path contract.
PROJECT_ROOT = _BACKEND_ROOT.parent if _BACKEND_ROOT.name == "backend" else _BACKEND_ROOT


@dataclass(frozen=True)
class InstancePaths:
    project: Path
    data: Path
    catalogs: Path
    env_file: Path

    @classmethod
    def resolve(cls, project=PROJECT_ROOT, *, environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ
        project = Path(project).resolve()

        def path(value, default):
            return (project / Path(value or default).expanduser()).resolve()

        data = path(env.get("TEA_DATA_DIR"), project / "data")
        private = data / "config/application.env"
        legacy = project / ".env"
        explicit = env.get("TEA_ENV_FILE")
        if not explicit and private.exists() and legacy.exists():
            raise ValueError("同时存在旧 .env 和 data/config/application.env，请明确 TEA_ENV_FILE。")
        env_file = path(explicit, legacy if legacy.exists() and not private.exists() else private)
        # Restored instances carry their exact configuration revision. New
        # checkouts use version-controlled defaults until an explicit restore.
        bundled = data / "config/catalogs"
        catalogs = path(env.get("TEA_CONFIG_DIR"), bundled if bundled.exists() else project / "config")
        return cls(project, data, catalogs, env_file)

    @property
    def private_config(self):
        return self.data / "config"

    @property
    def database(self):
        return self.data / "tea.sqlite3"

    @property
    def ragflow_config(self):
        return self.private_config / "ragflow"

    @property
    def ragflow_data(self):
        return self.data / "ragflow"

    @property
    def ragflow_runtime(self):
        return self.project / ".dev/ragflow"

    @property
    def collection_fleet(self):
        return self.private_config / "collection/fleet.json"

    @property
    def collection_state(self):
        return self.data / "control/collection-service"

    @property
    def collection_registry(self):
        return self.data / "control/collection-bindings"

    @property
    def source_pool(self):
        return self.catalogs / "collection-sources/id-sg-unattended.json"

    @property
    def embedding_admission(self):
        return self.data / "control/embedding-admission.sqlite3"

    def inventory(self):
        """Paths only: never display configuration values or credentials."""
        return {
            name: str(getattr(self, name))
            for name in (
                "project",
                "data",
                "database",
                "env_file",
                "catalogs",
                "ragflow_config",
                "ragflow_data",
                "ragflow_runtime",
                "collection_fleet",
                "collection_state",
                "embedding_admission",
            )
        }
