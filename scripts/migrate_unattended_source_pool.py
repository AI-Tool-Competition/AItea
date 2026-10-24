"""Migrate a stopped official collector's pool binding without resetting evidence."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from official_source_supervisor_recovery import load, lock, write
from unattended_collection import source_pool


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def migrate(old_pool: Path, new_pool: Path, root: Path, country: str) -> dict:
    old_pool, new_pool, root = old_pool.resolve(), new_pool.resolve(), root.resolve()
    old_hash, new_hash = digest(old_pool), digest(new_pool)
    old = load(old_pool)
    new = load(new_pool)
    if old_hash == new_hash:
        raise ValueError("source pool is unchanged")
    for candidate in (old_pool, new_pool):
        source_pool(candidate, country)
    old_sources = {row["id"]: row for row in old["sources"] if row["country"] == country}
    new_sources = {row["id"]: row for row in new["sources"] if row["country"] == country}
    if old_sources.keys() != new_sources.keys():
        raise ValueError("Pool migration must preserve every source ID; archive instead of deleting")
    protected = (
        "country",
        "url",
        "allowed_hosts",
        "kind",
        "topics",
        "discovery_keywords",
        "setneg_type",
    )
    for source_id, prior in old_sources.items():
        updated = new_sources[source_id]
        if any(prior.get(key) != updated.get(key) for key in protected) or prior.get(
            "verification", {}
        ).get("checklist_items", []) != updated.get("verification", {}).get("checklist_items", []):
            raise ValueError("Pool migration cannot change a source's reviewed network/document scope")

    state_path = root / "state.json"
    if not root.is_dir() or state_path.is_symlink():
        raise ValueError("Collector state must already exist in a regular project directory")
    with lock(root / ".collector.lock"):
        state = load(state_path)
        binding = state.get("binding", {})
        if binding.get("pool_sha256") != old_hash or binding.get("country") != country:
            raise ValueError("Collector binding does not match the supplied prior pool and country")
        if binding.get("root") != str(root):
            raise ValueError("Collector state path differs from the requested root")
        if state.get("active_sources"):
            raise ValueError("Collector still records active requests; stop and reconcile before migration")
        original = copy.deepcopy(state)
        migration = {
            "at": time.time(),
            "country": country,
            "old_pool_sha256": old_hash,
            "new_pool_sha256": new_hash,
            "source_ids": sorted(new_sources),
            "prior_state_sha256": hashlib.sha256(
                json.dumps(original, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
            ).hexdigest(),
        }
        state["binding"]["pool_sha256"] = new_hash
        state.setdefault("pool_migrations", []).append(migration)
        state["updated_at"] = time.time()
        write(state_path, state)
        if load(state_path) != state:
            raise OSError("Migrated collector state verification failed")
        event_dir = root / "events" / datetime.now(UTC).strftime("%Y-%m-%d")
        event_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write(
            event_dir / f"{time.time_ns()}-source-pool-migration.json",
            {
                "kind": "source_pool_binding_migrated",
                **migration,
            },
            exclusive=True,
        )
    return {"country": country, "old_pool_sha256": old_hash, "new_pool_sha256": new_hash}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-pool", type=Path, required=True)
    parser.add_argument("--new-pool", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--country", choices=("ID", "SG"), required=True)
    args = parser.parse_args(argv)
    os.umask(0o077)
    try:
        print(json.dumps(migrate(args.old_pool, args.new_pool, args.root, args.country)))
    except (OSError, ValueError) as error:
        print(json.dumps({"status": "blocked", "error_type": type(error).__name__}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
