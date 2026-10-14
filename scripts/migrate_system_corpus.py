"""Offline system-corpus snapshot and ID-preserving migration; dry-run by default."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source-dir", type=Path, required=True)
    export.add_argument("--snapshot-dir", type=Path, required=True)
    migrate = commands.add_parser("migrate")
    migrate.add_argument("--snapshot-dir", type=Path, required=True)
    migrate.add_argument("--target-dir", type=Path, required=True)
    migrate.add_argument("--models-env", type=Path)
    migrate.add_argument("--source-stopped", action="store_true")
    migrate.add_argument("--backup-dir", type=Path)
    migrate.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    def deny_network(event, arguments):
        if event in {"socket.connect", "socket.getaddrinfo"}:
            raise RuntimeError("Offline corpus migration forbids external calls")

    sys.addaudithook(deny_network)
    if getattr(args, "models_env", None):
        from verify_ingestion import configure_models

        try:
            configure_models(args.models_env)
        except (ValueError, OSError):
            print(
                json.dumps(
                    {"status": "refused", "reason": "model_configuration_rejected"}
                )
            )
            return 2
    from app.system_corpus_migration import (
        SystemCorpusMigrationError,
        export_system_corpus,
        migrate_system_corpus,
    )

    try:
        if args.command == "export":
            result = export_system_corpus(args.source_dir, args.snapshot_dir)
        else:
            result = migrate_system_corpus(
                args.snapshot_dir,
                args.target_dir,
                apply=args.apply,
                source_stopped=args.source_stopped,
                backup_dir=args.backup_dir,
            )
    except (SystemCorpusMigrationError, sqlite3.Error) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
