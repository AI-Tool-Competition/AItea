"""Offline system-corpus snapshot and ID-preserving migration; dry-run by default."""

import argparse
import json
import os
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
    migrate.add_argument("--alias-plan", type=Path)
    plan = commands.add_parser("plan-merge")
    plan.add_argument("--snapshot-dir", type=Path, required=True)
    plan.add_argument("--target-dir", type=Path, required=True)
    plan.add_argument("--aliases", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--models-env", type=Path)
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
        elif args.command == "plan-merge":
            from app.system_corpus_merge import MAX_PLAN_BYTES, create_alias_plan

            if args.aliases.stat().st_size > MAX_PLAN_BYTES:
                raise SystemCorpusMigrationError("alias_selection_too_large")
            result = create_alias_plan(
                args.snapshot_dir,
                args.target_dir,
                json.loads(args.aliases.read_bytes()),
            )
            with args.output.open("x") as stream:
                stream.write(
                    json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2)
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            from app.blob_store import _sync_directory

            _sync_directory(args.output.parent)
            result = {
                "mode": "planned",
                "plan_file": str(args.output),
                "plan_sha256": result["plan_sha256"],
                "aliases": len(result["aliases"]),
                "expected_system_documents": result["expected_system_documents"],
                "expected_relations": result["expected_relations"],
                "database_writes": 0,
                "model_calls": 0,
            }
        else:
            from app.system_corpus_merge import MAX_PLAN_BYTES

            if args.alias_plan and args.alias_plan.stat().st_size > MAX_PLAN_BYTES:
                raise SystemCorpusMigrationError("alias_plan_too_large")
            result = migrate_system_corpus(
                args.snapshot_dir,
                args.target_dir,
                apply=args.apply,
                source_stopped=args.source_stopped,
                backup_dir=args.backup_dir,
                alias_plan=json.loads(args.alias_plan.read_bytes())
                if args.alias_plan
                else None,
            )
    except (
        SystemCorpusMigrationError,
        sqlite3.Error,
        ValueError,
        KeyError,
        TypeError,
        OSError,
    ) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
