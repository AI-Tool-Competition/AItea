#!/usr/bin/env python3
"""Resume only an explicit, source-frozen MinerU corpus through durable queues."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import time
from collections import Counter
from contextlib import closing
from pathlib import Path
from threading import Event

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
STOP = Event()


def main():
    from app.bootstrap import Service
    from app.contexts.collection.application.collection import CollectionWorkers
    from app.contexts.knowledge.infrastructure.providers.mineru import Admission

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--parse-only", action="store_true")
    args = parser.parse_args()
    load_dotenv(args.env_file, override=True)
    if args.parse_only:
        os.environ["RAGFLOW_API_KEY"] = ""
        os.environ["RAGFLOW_EMBEDDING_MODEL"] = ""
    if not os.getenv("MINERU_API_TOKEN", "").strip():
        raise ValueError("请先配置后端 MINERU_API_TOKEN。")
    scope_bytes = args.scope.read_bytes()
    if len(scope_bytes) > 1024 * 1024:
        raise ValueError("处理范围文件超过限制。")
    scope = json.loads(scope_bytes)
    if scope.get("protocol") != "aitea-mineru-scope-v1":
        raise ValueError("MinerU 范围文件版本无效。")
    owner, documents = scope["owner_id"], scope["documents"]
    if not isinstance(owner, str) or not isinstance(documents, list) or not 1 <= len(documents) <= 2000:
        raise ValueError("处理范围必须明确账号和一至两千个资料版本。")
    ids = [item["id"] for item in documents]
    if len(ids) != len(set(ids)) or any(not isinstance(i, str) or not 1 <= len(i) <= 100 for i in ids):
        raise ValueError("处理范围包含无效或重复资料。")
    service = Service(args.data_dir.resolve(), require_existing=True)
    with closing(service.db.connect()) as conn:
        for item in documents:
            current = service.own_document(conn, owner, item["id"])
            if current["role"] != "primary" or any(
                current[key] != item[key] for key in ("content_hash", "version", "size", "filename")
            ):
                raise ValueError("冻结的原件或版本已变更，未启动处理。")
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: STOP.set())
    workers = [
        CollectionWorkers(
            service.collection,
            processing_scope=(owner, ids[offset : offset + 20]),
            fetch_enabled=False,
        ).start()
        for offset in range(0, len(ids), 20)
    ]
    status_path = args.data_dir / "mineru-progress.json"

    def progress():
        with closing(service.db.connect()) as conn:
            # A temp allowlist keeps queries bounded without SQLite parameter limits.
            conn.execute("CREATE TEMP TABLE mineru_scope(id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO mineru_scope VALUES(?)", [(i,) for i in ids])
            rows = [
                dict(row)
                for row in conn.execute("""SELECT d.id,d.status,p.phase,p.status job_status,d.error,
                json_extract(d.manifest,'$.remote_parse.state') parse_state,
                json_extract(d.manifest,'$.quality.page_count') pages,
                json_array_length(d.manifest,'$.quality.completed_pages') completed_pages
                FROM documents d JOIN mineru_scope s ON s.id=d.id LEFT JOIN processing_jobs p ON p.document_id=d.id
                ORDER BY d.id""")
            ]
            tasks = dict(
                Counter(
                    row[0]
                    for row in conn.execute(
                        "SELECT t.state FROM document_parse_tasks t JOIN mineru_scope s ON s.id=t.document_id"
                    )
                )
            )
        value = {
            "protocol": "aitea-mineru-progress-v1",
            "pid": os.getpid(),
            "scope_sha256": hashlib.sha256(scope_bytes).hexdigest(),
            "parse_only": args.parse_only,
            "documents": len(rows),
            "jobs": dict(Counter(row["job_status"] for row in rows)),
            "remote_tasks": tasks,
            "items": rows,
            "stopping": STOP.is_set(),
            "updated_at": time.time(),
            "concurrency": Admission(service.db.data_dir).capacity(),
        }
        temporary = status_path.with_suffix(".json.tmp")
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(status_path)
        return value

    last = None
    try:
        while not STOP.is_set():
            value = progress()
            summary = (value["jobs"], value["remote_tasks"])
            if summary != last:
                print(
                    json.dumps(
                        {"jobs": value["jobs"], "remote_tasks": value["remote_tasks"]},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                last = summary
            if not (value["jobs"].get("queued") or value["jobs"].get("processing")):
                return 0 if not value["jobs"].get("failed") else 2
            STOP.wait(10)
    finally:
        for worker in workers:
            worker.stop(timeout=0)
        deadline = time.monotonic() + 180
        while any(worker.thread and worker.thread.is_alive() for worker in workers):
            if time.monotonic() > deadline:
                raise RuntimeError("处理请求仍在退出，保留当前进程和任务回执；未强制停止。")
            time.sleep(0.2)
        progress()
    return 130


if __name__ == "__main__":
    raise SystemExit(main())
