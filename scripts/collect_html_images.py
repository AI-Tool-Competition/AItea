#!/usr/bin/env python3
"""Acquire mapped HTML body images and optionally process only selected owned drafts."""

import argparse
import fcntl
import json
import multiprocessing
import os
import stat
import sys
import time
from contextlib import closing
from pathlib import Path

from verify_ingestion import configure_models, terminate_owned, write_report

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def read_json(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or not 0 < before.st_size <= 2 * 1024 * 1024
        ):
            raise ValueError("需要不超过 2 MiB 的普通检查点文件。")
        raw = stream.read(before.st_size + 1)
        after = os.fstat(stream.fileno())
        if len(raw) != before.st_size or (before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("读取时检查点已变化。")
        return json.loads(raw)


def child(data_dir, output, options, models_env, state, time_budget):
    if hasattr(os, "setsid"):
        os.setsid()
    os.umask(0o077)
    configure_models(models_env)
    from app.collection import CollectionWorkers
    from app.html_image_collection import HtmlImageCollector, HtmlProcessingPending
    from app.ingestion_quality import text_complete
    from app.service import Service

    started = time.monotonic()
    deadline = started + time_budget
    workers = None
    with (output / "worker.log").open("a", buffering=1) as log:
        os.dup2(log.fileno(), 1)
        os.dup2(log.fileno(), 2)
        report = {
            "format": 1,
            "status": "preparing_owned_html_images",
            "scope": options,
            "documents": [],
            "model_calls_in_download_stage": 0,
            "formal_rag_enabled": False,
        }
        try:
            service = Service(data_dir)
            collector = HtmlImageCollector(
                service,
                state=state,
                max_requests=options["max_downloads"],
                checkpoint=lambda value: write_report(output, value, "images.json"),
            )
            for document in options["document_ids"]:
                if time.monotonic() >= deadline:
                    break
                try:
                    row = collector.prepare(
                        options["owner_id"],
                        document,
                        deadline=deadline,
                        max_images=options["images_per_document"],
                    )
                except HtmlProcessingPending:
                    row = {"document_id": document, "processing_already_pending": True}
                except Exception as error:  # noqa: BLE001 -- preserve safe per-document failure checkpoints
                    row = {"document_id": document, "error_type": type(error).__name__}
                report["documents"].append(row)
                report["protected_download_attempts"] = collector.request_attempts
                write_report(output, report)
            eligible, rejected = collector.processing_candidates(
                options["owner_id"], options["document_ids"]
            )
            report["processing_allowlist"] = eligible
            report["processing_rejected"] = rejected
            if options["live_models"] and eligible and time.monotonic() < deadline:
                workers = CollectionWorkers(
                    service.collection,
                    processing_scope=(options["owner_id"], eligible),
                    processing_policy="draft_html_v3",
                    fetch_enabled=False,
                ).start()
                while time.monotonic() < deadline:
                    with closing(service.db.connect()) as conn:
                        pending = conn.execute(
                            "SELECT count(*) FROM processing_jobs WHERE owner_id=? AND document_id IN ("
                            + ",".join("?" for _ in eligible)
                            + ") AND status IN ('queued','processing')"
                            + service.collection._processing_policy_clause(
                                "draft_html_v3"
                            ),
                            (options["owner_id"], *eligible),
                        ).fetchone()[0]
                    if not pending:
                        break
                    time.sleep(1)
                workers.stop(timeout=5)
                if workers.thread and workers.thread.is_alive():
                    report["status"] = "draining_owned_processing_until_deadline"
                    write_report(output, report)
                    while workers.thread.is_alive():
                        time.sleep(0.2)
            final = []
            fingerprint = service.embedding_fingerprint()
            with closing(service.db.connect()) as conn:
                for document in options["document_ids"]:
                    row = service.own_document(conn, options["owner_id"], document)
                    enriched = service.document_dict(dict(row), conn=conn)
                    manifest = json.loads(row["manifest"])
                    vectors = conn.execute(
                        "SELECT count(*) FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid WHERE c.document_id=?",
                        (document,),
                    ).fetchone()[0]
                    chunks = conn.execute(
                        "SELECT count(*) FROM retrieval_chunks WHERE document_id=?",
                        (document,),
                    ).fetchone()[0]
                    valid_vectors = conn.execute(
                        "SELECT count(*) FROM retrieval_vectors v JOIN retrieval_chunks c ON c.rowid=v.chunk_rowid "
                        "WHERE c.document_id=? AND v.fingerprint=? AND v.input_hash=c.input_hash",
                        (document, fingerprint),
                    ).fetchone()[0]
                    final.append(
                        {
                            "document_id": document,
                            "source_hash": row["content_hash"],
                            "source_version": row["version"],
                            "text_complete": text_complete(manifest),
                            "legal_level": enriched["legal_level"],
                            "review_status": enriched["metadata"].get("review_status"),
                            "vectors": vectors,
                            "retrieval_chunks": chunks,
                            "vectors_current_input_and_profile": valid_vectors,
                            "vector_complete": bool(
                                fingerprint and chunks and chunks == valid_vectors
                            ),
                            "status": row["status"],
                            "error": row["error"],
                        }
                    )
            report.update(
                status="owned_image_batch_finished",
                results=final,
                elapsed_seconds=round(time.monotonic() - started, 3),
                complete_selected_documents=sum(r["text_complete"] for r in final),
            )
            _final_eligible, final_rejected = collector.processing_candidates(
                options["owner_id"],
                options["document_ids"],
            )
            report["processing_rejected_at_completion"] = final_rejected
            if any(
                r.get("errors")
                or r.get("error_type")
                or r.get("remaining_eligible_images")
                or r.get("unresolved_objects")
                for r in report["documents"]
            ):
                report["status"] = "owned_image_batch_has_source_or_preparation_gaps"
            if rejected or final_rejected or any(not r["text_complete"] for r in final):
                report["status"] = "owned_image_batch_has_source_or_processing_gaps"
            if options["live_models"] and any(
                (r["legal_level"] == "primary" and not r["vector_complete"])
                or (r["legal_level"] != "primary" and r["vectors"] != 0)
                for r in final
            ):
                report["status"] = "owned_image_batch_has_vector_gaps"
            write_report(output, report)
        except Exception as error:  # noqa: BLE001 -- child failure must retain source progress without secrets
            report.update(status="failed", error_type=type(error).__name__)
            write_report(output, report)
            raise SystemExit(1) from None
        finally:
            if workers:
                workers.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument(
        "--document-ids",
        required=True,
        help="One to twenty selected IDs, comma-separated",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--images-per-document", type=int, default=4, choices=range(1, 21)
    )
    parser.add_argument("--max-downloads", type=int, default=20, choices=range(1, 101))
    parser.add_argument("--time-budget", type=int, default=300, choices=range(10, 901))
    parser.add_argument("--live-models", action="store_true")
    parser.add_argument("--models-env", type=Path)
    args = parser.parse_args()
    lock = None
    try:
        data_dir = args.data_dir.resolve()
        if (
            not (data_dir / "tea.sqlite3").is_file()
            or (data_dir / "tea.sqlite3").is_symlink()
        ):
            raise ValueError("data-dir 必须是已有资料库。")
        documents = args.document_ids.split(",")
        from app.collection import CollectionService

        CollectionService._processing_scope(args.owner, documents)
        if args.models_env and not args.live_models:
            raise ValueError("models-env 需要同时显式启用 live-models。")
        if args.live_models and (
            not args.models_env
            or not args.models_env.is_file()
            or args.models_env.is_symlink()
        ):
            raise ValueError("真实处理需要已有的私有模型配置普通文件。")
        options = {
            "data_dir": str(data_dir),
            "owner_id": args.owner,
            "document_ids": documents,
            "images_per_document": args.images_per_document,
            "max_downloads": args.max_downloads,
            "live_models": args.live_models,
        }
        output = args.output.resolve()
        if output == data_dir or output.is_relative_to(data_dir / "files"):
            raise ValueError("output 不能覆盖资料库或原件目录。")
        if args.resume:
            if read_json(output / "options.json") != options:
                raise ValueError("续跑必须保留账号、资料、下载范围和模型选项。")
        elif output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError("output 需要新目录；已有任务使用 resume。")
        output.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock = os.open(
            output / "job.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
        )
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.resume:
            state = (
                read_json(output / "images.json")
                if (output / "images.json").exists()
                else None
            )
            if (output / "report.json").exists():
                previous = (output / "report.json").read_bytes()
                prior = output / f"report-{time.time_ns()}.json"
                prior.write_bytes(previous)
                prior.chmod(0o400)
        else:
            if any(p.name != "job.lock" for p in output.iterdir()):
                raise ValueError("获取锁时输出目录已变化。")
            write_report(output, options, "options.json")
            (output / "options.json").chmod(0o400)
            state = None
    except (ValueError, OSError, TypeError) as error:
        if lock is not None:
            os.close(lock)
        parser.error(str(error))
    process = multiprocessing.get_context("spawn").Process(
        target=child,
        args=(
            data_dir,
            output,
            options,
            str(args.models_env.resolve()) if args.models_env else None,
            state,
            args.time_budget,
        ),
    )
    process.start()
    try:
        process.join(args.time_budget)
        if process.is_alive():
            terminate_owned(process)
            report = (
                read_json(output / "report.json")
                if (output / "report.json").exists()
                else {}
            )
            report.update(
                status="budget_exhausted_resumable",
                owned_process_stopped=not process.is_alive(),
            )
            write_report(output, report)
            raise SystemExit(3)
        report = (
            read_json(output / "report.json")
            if (output / "report.json").exists()
            else {}
        )
        print(
            json.dumps(
                {
                    "output": str(output),
                    "status": report.get("status"),
                    "download_attempts": report.get("protected_download_attempts", 0),
                    "complete_documents": report.get("complete_selected_documents", 0),
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(
            process.exitcode
            or (2 if report.get("status") != "owned_image_batch_finished" else 0)
        )
    except KeyboardInterrupt:
        terminate_owned(process)
        raise SystemExit(130) from None
    finally:
        os.close(lock)


if __name__ == "__main__":
    main()
