"""Prepare bounded v2 retries or apply verified v3 pages to an isolated copy."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply-checkpoint", "quarantine"])
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--source-hash", required=True)
    parser.add_argument("--version", type=int, required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--page", type=int, action="append", default=[])
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--checkpoint-dir", type=Path)
    parser.add_argument("--checkpoint-owner")
    parser.add_argument("--expected-page-sha256", action="append", default=[], metavar="PAGE:SHA256")
    parser.add_argument("--models-env", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        from app.contexts.knowledge.infrastructure.recovery.page_checkpoint_recovery import (
            _document,
            apply_page_checkpoint,
            isolated_directory,
            quarantine_scope_pages,
            readonly_service,
        )

        directory = isolated_directory(args.data_dir)

        if args.action == "prepare":
            if args.apply:
                raise ValueError("prepare_returns_checkpoint_only_no_database_writes")
            from app.contexts.knowledge.infrastructure.recovery.ocr_page_retry import (
                prepare_physical_page_retry,
            )

            with sqlite3.connect((directory / "tea.sqlite3").as_uri() + "?mode=ro", uri=True) as conn:
                conn.row_factory = sqlite3.Row
                row = _document(
                    conn,
                    args.owner,
                    args.document_id,
                    args.source_hash,
                    args.version,
                    args.expected_manifest_sha256,
                )
            prepared = prepare_physical_page_retry(
                json.loads(row["manifest"]), args.page, source_hash=args.source_hash
            )
            report = {
                "protocol": "physical-page-retry-plan-v1",
                "document_id": args.document_id,
                "source_hash": args.source_hash,
                "version": args.version,
                "expected_manifest_sha256": args.expected_manifest_sha256,
                "prepared_pages": [prepared["pages"][page - 1] for page in args.page],
                "database_writes": 0,
                "model_calls": 0,
            }
        elif args.action == "quarantine":
            page_hashes = {}
            for value in args.expected_page_sha256:
                number, separator, digest = value.partition(":")
                if (
                    not separator
                    or not number.isascii()
                    or not number.isdigit()
                    or int(number) in page_hashes
                ):
                    raise ValueError("quarantine_unique_page_sha256_required")
                page_hashes[int(number)] = digest
            service = readonly_service(directory)
            if args.apply:
                from app.bootstrap import Service

                service = Service(directory)
            report = quarantine_scope_pages(
                service,
                args.owner,
                args.document_id,
                source_hash=args.source_hash,
                version=args.version,
                expected_manifest=args.expected_manifest_sha256,
                page_hashes=page_hashes,
                apply=args.apply,
            )
        else:
            if not args.checkpoint or not args.checkpoint_dir or not args.checkpoint_owner:
                raise ValueError("checkpoint_file_directory_and_owner_required")
            if not 0 < args.checkpoint.stat().st_size <= 2 * 1024 * 1024:
                raise ValueError("checkpoint_file_exceeds_2MiB")
            if args.models_env:
                from verify_ingestion import configure_models

                configure_models(args.models_env)
            service = readonly_service(directory)
            if args.apply:
                from app.bootstrap import Service

                service = Service(directory)

            report = apply_page_checkpoint(
                service,
                args.owner,
                args.document_id,
                source_hash=args.source_hash,
                version=args.version,
                expected_manifest=args.expected_manifest_sha256,
                checkpoint=json.loads(args.checkpoint.read_bytes()),
                checkpoint_dir=args.checkpoint_dir,
                checkpoint_owner=args.checkpoint_owner,
                apply=args.apply,
            )
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
