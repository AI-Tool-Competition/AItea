"""Offline server import; dry-run is the default and never creates accounts."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--target-dir", type=Path, required=True)
    parser.add_argument("--source-owner", required=True)
    parser.add_argument("--target-owner", required=True)
    parser.add_argument("--models-env", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument(
        "--document-id",
        action="append",
        dest="document_ids",
        help="Import only these source documents; repeat for dependencies and related originals",
    )
    args = parser.parse_args()
    if args.models_env:
        from verify_ingestion import configure_models

        configure_models(args.models_env)
    from app.collection_import import CollectionImportError, import_collected_resources

    try:
        result = import_collected_resources(
            args.source_dir,
            args.target_dir,
            args.source_owner,
            args.target_owner,
            apply=args.apply,
            refresh=args.refresh,
            document_ids=args.document_ids,
        )
    except (CollectionImportError, sqlite3.Error) as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
