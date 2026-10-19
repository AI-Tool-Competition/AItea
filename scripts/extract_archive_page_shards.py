#!/usr/bin/env python3
"""Preserve native physical-page evidence outside the production index."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from app.archive_page_shards import ArchivePageSource, extract_native_page_shards  # noqa: E402
from verify_ingestion import write_report  # noqa: E402


def _offline(event, arguments):
    # Multiprocessing may use local UNIX sockets; public networking is absent
    # from this native-only workflow, even when model keys exist in the shell.
    if event == "socket.getaddrinfo" or (event == "socket.connect" and isinstance(arguments[1], tuple)):
        raise RuntimeError("native page extraction does not permit network calls")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True, type=Path)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--total-pages", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--storage-root", type=Path, help="Explicitly authorized external storage root")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--workers", default=1, type=int)
    parser.add_argument("--window-pages", default=64, type=int)
    parser.add_argument("--max-windows", type=int)
    args = parser.parse_args()
    allowed = [ROOT / ".dev", ROOT / "data"]
    if args.storage_root is not None:
        if (
            not args.storage_root.is_absolute()
            or not args.storage_root.is_dir()
            or args.storage_root.is_symlink()
        ):
            parser.error("storage-root must be an existing real absolute directory")
        allowed.append(args.storage_root.resolve())
    output = args.output.absolute()
    if output.is_symlink() or not any(output.resolve().is_relative_to(path) for path in allowed):
        parser.error("output must be below project data/.dev or the explicit storage-root")
    for key in list(os.environ):
        if key.startswith(("VISION_OCR_", "EMBEDDING_", "RERANK_")):
            os.environ.pop(key)
    os.environ["TEA_PARSE_WORKERS"] = str(args.workers)
    sys.addaudithook(_offline)
    database = args.source_db.absolute()
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id,version,owner_id,content_hash,size,storage_path,mime_type FROM documents WHERE id=?",
            (args.document_id,),
        ).fetchone()
        if row is None or row["mime_type"] != "application/pdf":
            parser.error("source must identify an existing PDF original")
        original = Path(row["storage_path"])
        if not original.is_absolute():
            original = database.parent / original
        source = ArchivePageSource(
            original,
            row["id"],
            row["version"],
            row["owner_id"],
            row["content_hash"],
            row["size"],
            args.total_pages,
        )
    pointer_name = (
        hashlib.sha256(f"{source.document_id}:{source.version}".encode()).hexdigest()[:20] + ".json"
    )
    pointer_dir = ROOT / ".dev" / "archive-page-shards"
    pointer_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_report(
        pointer_dir,
        {
            "protocol": "archive-native-page-shards-location-v1",
            "output": str(output),
            "source_identity": source.identity(),
            "operational_index": False,
        },
        pointer_name,
    )
    report = extract_native_page_shards(
        source,
        output,
        checkpoint=args.checkpoint,
        evidence_root=database.parent,
        workers=args.workers,
        window_pages=args.window_pages,
        max_windows=args.max_windows,
        on_commit=lambda progress: print(json.dumps(progress), flush=True),
    )
    write_report(output, report, f"run-{report['run_id']}.json")
    write_report(output, report, "last-run.json")
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
