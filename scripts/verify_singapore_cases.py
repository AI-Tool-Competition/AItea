#!/usr/bin/env python3
"""Acquire and verify SG-LegalCite CSV in a private, resumable data directory.

The dataset and manual candidates never enter the document or vector index.
Run through uv with the locked backend environment. Network routing uses the
same explicitly configured strict source transport as normal collection.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.contexts.ingestion.infrastructure.source_adapters.singapore_cases import (
    CASE_PARSER_VERSION,
    DatasetRanges,
    DatasetValidationError,
    dataset_metadata,
    validate_csv,
    write_json,
)
from app.shared.infrastructure.persistence.blob_store import _verify_existing
from app.shared.infrastructure.http.download import DownloadError

ROOT = Path(__file__).resolve().parents[1]


def output_directory(path: Path, resume: bool) -> Path:
    path = path.absolute()
    resolved = path.resolve()
    if not any(resolved.is_relative_to(ROOT / directory) for directory in ("data", ".dev")):
        raise ValueError("output must stay below this worktree's data/ or .dev/")
    if path.is_symlink() or (path.exists() and not resume):
        raise ValueError("use a new output directory or explicitly resume this dataset")
    if resume and not path.is_dir():
        raise ValueError("resume needs this dataset's existing directory")
    path.mkdir(parents=True, mode=0o700, exist_ok=resume)
    return path.resolve()


def read_snapshot(output: Path, name: str) -> dict:
    fd = os.open(output / name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 2 * 1024 * 1024:
            raise ValueError("dataset source snapshot must be a bounded regular file")
        value = json.load(handle)
        if not isinstance(value, dict):
            raise TypeError("dataset snapshot must be an object")
        return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--revision", default="main")
    parser.add_argument("--part-mib", type=int, default=8)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--time-budget", type=float, default=1800)
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    output = None
    try:
        output = output_directory(args.output, args.resume)
        lock_fd = os.open(output / "dataset.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "w"):
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            source = (
                read_snapshot(output, "source.json")
                if args.resume
                else dataset_metadata(output, revision=args.revision, timeout=min(args.timeout, 90))
            )
            if args.metadata_only:
                result = {
                    "status": "metadata_verified_csv_not_acquired",
                    "source": source,
                    "full_hash_verified": False,
                    "full_csv_parsed": False,
                    "verified_propositions": 0,
                    "rag_records": 0,
                }
                write_json(output, "report.json", result)
            else:
                ranges = DatasetRanges(
                    output,
                    source,
                    part_bytes=args.part_mib * 1024 * 1024,
                    workers=args.workers,
                    timeout=args.timeout,
                    time_budget=args.time_budget,
                )
                original = ranges.download()
                validation_output = output
                if (output / "candidates.jsonl").exists():
                    # A new parser writes an append-only validation snapshot;
                    # previous candidates stay available for existing reviews.
                    old = read_snapshot(output, "report.json")
                    if old.get("parser_version") != CASE_PARSER_VERSION:
                        validation_output = output / "validations" / CASE_PARSER_VERSION
                        if (
                            not validation_output.resolve().is_relative_to(output)
                            or validation_output.is_symlink()
                        ):
                            raise ValueError("validation snapshots must remain inside the dataset directory")
                        validation_output.mkdir(mode=0o700, parents=True, exist_ok=True)
                if (validation_output / "candidates.jsonl").exists():
                    # Completed immutable validation is replayable without
                    # rewriting its manual candidate queue on every resume.
                    report_file = validation_output / "report.json"
                    fd = os.open(report_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "r", encoding="utf-8") as handle:
                        if os.fstat(handle.fileno()).st_size > 2 * 1024 * 1024:
                            raise ValueError("dataset validation report exceeds its bound")
                        result = json.load(handle)
                    if (
                        result.get("source") != source
                        or not result.get("full_csv_parsed")
                        or not result.get("full_hash_verified")
                        or result.get("parser_version") != CASE_PARSER_VERSION
                        or result.get("candidate_file") != "candidates.jsonl"
                    ):
                        raise DatasetValidationError(
                            "existing candidate queue lacks matching immutable validation evidence"
                        )
                    _verify_existing(
                        validation_output / result["candidate_file"],
                        result["candidate_file_sha256"],
                        result["candidate_file_size"],
                    )
                else:
                    result = validate_csv(original, source, validation_output)
                output_report = validation_output / "report.json"
    except BlockingIOError:
        parser.error("this dataset directory has an active collector")
    except Exception as exc:  # noqa: BLE001 -- signed CDN URLs must never leak through a provider exception
        # Do not expose signed CDN links or remote response bodies in CLI output.
        failure = {
            "status": "failed_or_incomplete",
            "error_type": type(exc).__name__,
            "resumable_checkpoint": "download.json",
        }
        if isinstance(exc, (DatasetValidationError, DownloadError)):
            failure["error"] = str(exc)
        if output is not None:
            write_json(output, "failure.json", failure)
        print(json.dumps(failure, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "report": str(output / "report.json" if args.metadata_only else output_report),
                "status": result["status"],
                "size": source["size"],
                "sha256": source["sha256"],
                "rows": result.get("rows"),
                "candidate_count": result.get("candidate_count", 0),
                "verified_propositions": result.get("verified_propositions", 0),
                "rag_records": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
