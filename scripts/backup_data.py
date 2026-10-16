"""Create, verify, or restore a complete private AItea data snapshot."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create", help="Online SQLite snapshot plus all published files"
    )
    create.add_argument("--source-dir", type=Path, required=True)
    create.add_argument("--backup-dir", type=Path, required=True)
    create.add_argument("--timeout", type=float, default=300)
    verify = commands.add_parser(
        "verify", help="Read-only full integrity and SHA verification"
    )
    verify.add_argument("--backup-dir", type=Path, required=True)
    restore = commands.add_parser(
        "restore", help="Restore only into a new directory; existing targets refused"
    )
    restore.add_argument("--backup-dir", type=Path, required=True)
    restore.add_argument("--target-dir", type=Path, required=True)
    args = parser.parse_args()

    def offline(event, arguments):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError("Data backup/restore does not permit network calls")

    sys.addaudithook(offline)
    from app.data_backup import (
        DataBackupError,
        create_backup,
        restore_backup,
        verify_backup,
    )

    try:
        if args.command == "create":
            result = create_backup(
                args.source_dir, args.backup_dir, timeout=args.timeout
            )
        elif args.command == "verify":
            result = verify_backup(args.backup_dir)
        else:
            result = restore_backup(args.backup_dir, args.target_dir)
    except (DataBackupError, OSError, sqlite3.Error) as exc:
        # Fixed diagnostics avoid dumping SQLite statements, rows, or secrets.
        reason = str(exc) if isinstance(exc, DataBackupError) else type(exc).__name__
        print(json.dumps({"status": "refused", "reason": reason}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
