"""Restore a frozen native page archive into its original isolated document."""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--source-hash", required=True)
    parser.add_argument("--version", required=True, type=int)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--shards-dir", required=True, type=Path)
    parser.add_argument("--expected-root-sha256", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        from app.contexts.documents.infrastructure.parsers.archive_page_recovery import (
            recover_native_page_shards,
        )
        from app.contexts.documents.infrastructure.recovery.page_checkpoint_recovery import (
            isolated_directory,
            readonly_service,
        )
        from extract_archive_page_shards import _offline

        sys.addaudithook(_offline)
        directory = isolated_directory(args.data_dir)
        service = readonly_service(directory)
        if args.apply:
            from app.bootstrap import Service

            service = Service(directory)
        report = recover_native_page_shards(
            service,
            args.owner,
            args.document_id,
            source_hash=args.source_hash,
            version=args.version,
            expected_manifest=args.expected_manifest_sha256,
            shards_dir=args.shards_dir,
            expected_root=args.expected_root_sha256,
            apply=args.apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
