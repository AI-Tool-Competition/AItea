#!/usr/bin/env python3
"""Rebuild candidate source metadata from verified saved official source pages."""

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main(argv=None):
    from app.contexts.collection.application.checklists import ChecklistService
    from app.shared.infrastructure.blob_store import BlobStore
    from app.shared.infrastructure.db import Database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--limit", type=int, default=500, help="maximum candidates to inspect (1–1000)")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000 or args.data_dir.is_symlink():
        parser.error("limit must be 1–1000 and data-dir must not be a symbolic link")
    data_dir = args.data_dir.resolve(strict=True)
    database = data_dir / "tea.sqlite3"
    if database.is_symlink() or not database.is_file():
        parser.error("data-dir must contain an existing regular tea.sqlite3 database")
    db = Database(data_dir)
    service = SimpleNamespace(
        db=db,
        archive=SimpleNamespace(blobs=BlobStore(db.files_dir, max_bytes=128 * 1024 * 1024)),
    )
    checklist = ChecklistService(service)
    print(
        json.dumps(
            checklist.backfill_candidate_source_records(args.limit),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
