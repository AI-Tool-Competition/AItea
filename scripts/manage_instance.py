"""Inspect, adopt, back up and restore a complete private AItea instance."""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.configuration.paths import InstancePaths  # noqa: E402
from app.shared.infrastructure.operations.data_backup import (  # noqa: E402
    DataBackupError,
    verify_backup,
)
from app.shared.infrastructure.operations.instance_storage import (  # noqa: E402
    adopt_instance,
    backup_instance,
    restore_instance,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("paths", help="Show active paths, never secrets")
    commands.add_parser("adopt", help="Move legacy private config after stopping this instance")
    for action in ("backup", "verify", "restore"):
        command = commands.add_parser(action)
        command.add_argument("--backup-dir", type=Path, required=True)
        if action == "restore":
            command.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    paths = InstancePaths.resolve(ROOT)
    try:
        if args.command == "paths":
            result = paths.inventory()
        elif args.command == "adopt":
            result = adopt_instance(paths)
        elif args.command == "backup":
            result = backup_instance(paths, args.backup_dir)
        elif args.command == "verify":
            result = verify_backup(args.backup_dir)
        else:
            result = restore_instance(args.backup_dir, args.target_dir, project=ROOT)
    except (ValueError, OSError) as error:
        reason = str(error) if isinstance(error, DataBackupError) else type(error).__name__
        print(json.dumps({"status": "refused", "reason": reason}))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
