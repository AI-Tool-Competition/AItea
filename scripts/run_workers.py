#!/usr/bin/env python3
"""Run the existing collection, parsing and indexing queues outside the chat API."""

import argparse
import os
import signal
import sys
from pathlib import Path
from threading import Event

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    args = parser.parse_args()
    if args.env_file:
        os.environ["TEA_ENV_FILE"] = str(args.env_file.resolve())
    from app.configuration.environment import load_environment

    load_environment()
    # Limits apply to the existing persistent queue/leases; no duplicate queue.
    os.environ.setdefault("TEA_COLLECT_WORKERS", "2")
    os.environ.setdefault("TEA_PROCESS_WORKERS", "1")
    os.environ.setdefault("TEA_EMBED_WORKERS", "1")
    from app.bootstrap import Service
    from app.contexts.ingestion.application.collection_jobs import CollectionWorkers
    from app.contexts.ingestion.application.official_source_import import (
        OfficialSourceImporter,
    )
    from app.shared.infrastructure.operations.scheduler import (
        start_scheduler,
        stop_scheduler,
    )

    service = Service(args.data_dir.resolve(), require_existing=True)
    stop = Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    scheduler = start_scheduler(service)
    workers = importer = None
    try:
        workers = CollectionWorkers(service.collection).start()
        importer = OfficialSourceImporter(service).start()
        while not stop.wait(1):
            pass
    finally:
        if importer:
            importer.stop()
        stop_scheduler(scheduler)
        if workers:
            workers.stop()


if __name__ == "__main__":
    main()
