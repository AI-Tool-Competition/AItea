"""Resume a fixed owner/model document plan, preserving each successful HTTP batch.

Run with uv and an explicit private models file. The plan is frozen before any
work and contains document IDs, original versions/hashes and the fingerprint.
This command never discovers new documents, downloads, parses, OCRs or approves.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import signal
import sqlite3
import statistics
import sys
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def stable_manifest(raw):
    result = json.loads(raw)
    result.pop("embedding_fingerprint", None)
    result.pop("embedding_reuse", None)
    return digest(result)


def unresolved_issued_inputs(output, saved, *, run_ids=None):
    """A durable intent without a known rejection or saved receipt is uncertain.

    An intent precedes sending, so an interruption in that narrow gap can be a
    conservative false positive. Never silently infer that it was unbilled.
    Legacy completed-request journals remain valid without intent records.
    """
    completed = output / "requests.jsonl"
    records = (
        [json.loads(line) for line in completed.read_text().splitlines()]
        if completed.exists()
        else []
    )
    terminal = {r["request_id"]: r for r in records if r.get("request_id")}
    intents = output / "issued-requests.jsonl"
    if intents.exists():
        records.extend(
            terminal.get(record["request_id"], record)
            for record in map(json.loads, intents.read_text().splitlines())
        )
    attempted = set()
    for record in records:
        if run_ids is not None and record.get("run_id") not in run_ids:
            continue
        if record.get("http_started") and record.get("http_status") not in {
            400,
            401,
            403,
            404,
            413,
            429,
        }:
            attempted.update(record["input_hashes"])
    return attempted - saved


def consumed_recovery_inputs(output, selection_sha256):
    """A new unknown result spends that exact recovery authorization once.

    Preflight files bind past run IDs to the immutable selection file. This
    also protects runs made before the supervisor existed, including a crash
    after a durable send intent. Known rejection/429 does not spend an input.
    """
    run_ids = set()
    for path in output.glob("preflight-*.json"):
        previous = json.loads(path.read_text())
        if (
            previous.get("configuration", {}).get("retry_unresolved_selection_sha256")
            == selection_sha256
        ):
            run_ids.add(previous["run_id"])
    return unresolved_issued_inputs(output, set(), run_ids=run_ids)


def read_unresolved_selection(path, plan, fixed_input_hashes):
    """Bind an explicit retry decision to exact inputs in this owner/model plan."""
    with path.open("rb") as stream:
        raw = stream.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Unresolved recovery selection exceeds 4 MiB")
    value = json.loads(raw)
    expected = {
        "protocol": "fixed-embedding-unresolved-recovery-v1",
        "owner": plan["owner"],
        "fingerprint": plan["fingerprint"],
        "source_plan_sha256": digest(plan),
    }
    if (
        not isinstance(value, dict)
        or set(value) != {*expected, "input_hashes"}
        or any(value[key] != item for key, item in expected.items())
    ):
        raise ValueError("Unresolved recovery selection does not match the frozen plan")
    hashes = value["input_hashes"]
    if (
        not isinstance(hashes, list)
        or not hashes
        or any(
            not isinstance(item, str)
            or len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
            for item in hashes
        )
        or len(hashes) != len(set(hashes))
        or not set(hashes).issubset(fixed_input_hashes)
    ):
        raise ValueError(
            "Unresolved recovery selection contains invalid or out-of-plan inputs"
        )
    return {
        "path": str(path.resolve()),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "input_hashes": hashes,
    }


def deferred_input_documents(document_inputs, quarantined_inputs):
    return {
        ident
        for ident, hashes in document_inputs.items()
        if quarantined_inputs.intersection(hashes)
    }


class Observations:
    def __init__(self, output):
        self.output = output
        self.lock = threading.Lock()
        self.active = self.peak = 0
        self.records = []
        self.run_id = None
        self.stop = threading.Event()
        self.failure = None
        self.database_waits = []
        self.runtime_errors = []

    def append(self, name, record):
        # The caller holds self.lock; flush before any corresponding HTTP send.
        with (self.output / name).open("a") as stream:
            os.chmod(stream.name, 0o600)
            stream.write(json.dumps(record) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def database_wait(self, operation, **details):
        record = {
            "run_id": self.run_id,
            "at": time.time(),
            "operation": operation,
            **details,
        }
        with self.lock:
            self.database_waits.append(record)
            self.append("database-waits.jsonl", record)

    def runtime_error(self, operation, error):
        self.stop.set()
        record = {
            "run_id": self.run_id,
            "at": time.time(),
            "operation": operation,
            "error_type": type(error).__name__,
            "sqlite_errorcode": getattr(error, "sqlite_errorcode", None),
        }
        with self.lock:
            self.failure = self.failure or "local_runtime_error"
            self.runtime_errors.append(record)
            self.append("runtime-errors.jsonl", record)

    def install(self):
        from app import ingestion, parsing, vision
        from app.retrieval import input_hash

        original = ingestion._embedding_response
        expected = ingestion.model_service_config("EMBEDDING")[0] + "/embeddings"
        recorder = self

        class Client:
            def __init__(self, client, record):
                self.client, self.record = client, record

            @contextmanager
            def stream(self, method, url, **kwargs):
                if method != "POST" or url != expected:
                    raise ValueError("Unexpected embedding endpoint")
                record = self.record
                started = time.monotonic()
                with recorder.lock:
                    if recorder.stop.is_set():
                        raise ingestion.EmbeddingProviderDeferred(retry_after=60)
                    record["started_at"] = time.time()
                    record["http_started"] = True
                    recorder.append("issued-requests.jsonl", record)
                    recorder.active += 1
                    recorder.peak = max(recorder.peak, recorder.active)
                try:
                    with self.client.stream(method, url, **kwargs) as response:
                        record["headers_seconds"] = round(time.monotonic() - started, 6)
                        record["http_status"] = response.status_code
                        if (
                            response.status_code in {408, 429}
                            or response.status_code >= 500
                        ):
                            record["rate_limited"] = response.status_code == 429
                            record["retry_after_seconds"] = ingestion._retry_after(
                                response.headers.get("Retry-After")
                            )
                            recorder.failure = (
                                "provider_rate_limited"
                                if response.status_code == 429
                                else "provider_unavailable"
                            )
                            recorder.stop.set()
                        yield response
                finally:
                    record["seconds"] = round(time.monotonic() - started, 6)
                    with recorder.lock:
                        recorder.active -= 1

        def observed(client, batch, base, key, model, call):
            record = {
                "request_id": uuid.uuid4().hex,
                "run_id": self.run_id,
                "input_hashes": [input_hash(t) for t in batch],
                "inputs": len(batch),
                "input_characters": sum(len(t) for t in batch),
                "http_started": False,
            }
            try:
                result = original(Client(client, record), batch, base, key, model, call)
                record["outcome"] = "response"
                return result
            except Exception as error:
                record["outcome"] = type(error).__name__
                if record["http_started"] and type(error).__name__ in {
                    "ReadTimeout",
                    "WriteTimeout",
                    "ReadError",
                    "WriteError",
                    "RemoteProtocolError",
                }:
                    self.failure = "issued_request_result_unknown"
                    self.stop.set()
                raise
            finally:
                with self.lock:
                    self.records.append(record)
                    self.append("requests.jsonl", record)

        ingestion._embedding_response = observed

        def forbid(*args, **kwargs):
            self.failure = "non_embedding_operation_prevented"
            self.stop.set()
            raise AssertionError(
                "This fixed embedding plan cannot download, parse or OCR"
            )

        ingestion._download = forbid
        parsing.parse_bounded = parsing.ocr_bounded = forbid
        vision.transcribe_page = vision.transcribe_image = forbid

    def summary(self):
        with self.lock:
            rows = [r for r in self.records if r["http_started"]]
            times = sorted(r["seconds"] for r in rows)
            inputs = [value for r in rows for value in r["input_hashes"]]
            return {
                "http_requests": len(rows),
                "http_peak": self.peak,
                "active_http": self.active,
                "http_statuses": dict(Counter(str(r.get("http_status")) for r in rows)),
                "outcomes": dict(Counter(r["outcome"] for r in rows)),
                "requested_inputs": sum(r["inputs"] for r in rows),
                "unique_requested_inputs": len(set(inputs)),
                "repeated_requested_inputs": len(inputs) - len(set(inputs)),
                "latency_median": statistics.median(times) if times else None,
                "latency_p95": times[math.ceil(len(times) * 0.95) - 1]
                if times
                else None,
            }


def finish_claims(active, observations):
    """Drain every completed future, even if one local commit failed."""
    finished = 0
    for future in list(active):
        if not future.done():
            continue
        active.pop(future)
        finished += 1
        try:
            future.result()
        except Exception as error:  # noqa: BLE001 - preserve peers and report the local failure
            observations.runtime_error("processing_result", error)
    return finished


def admission_candidates(service, owner, ids, limit):
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    timestamp = time.time()
    with closing(service.db.connect()) as conn:
        return [
            row[0]
            for row in conn.execute(
                f"""SELECT document_id FROM processing_jobs
                WHERE owner_id=? AND document_id IN ({placeholders}) AND phase='embed'
                AND ((status='queued' AND available_at<=?) OR (status='processing' AND lease_until<=?))
                ORDER BY updated_at,document_id LIMIT ?""",
                (owner, *ids, timestamp, timestamp, limit),
            )
        ]


def capture(service, ids):
    with closing(service.db.connect()) as conn:
        documents = {}
        for ident in ids:
            row = dict(
                conn.execute("SELECT * FROM documents WHERE id=?", (ident,)).fetchone()
            )
            documents[ident] = {
                "source": {
                    k: row[k]
                    for k in (
                        "owner_id",
                        "role",
                        "version",
                        "content_hash",
                        "source_url",
                        "storage_path",
                        "size",
                    )
                },
                "text_sha256": stable_manifest(row["manifest"]),
            }
        vectors = {
            str(r["chunk_rowid"]): {
                "fingerprint": r["fingerprint"],
                "input_hash": r["input_hash"],
                "dimension": r["dimension"],
                "sha256": hashlib.sha256(r["vector"]).hexdigest(),
            }
            for r in conn.execute(
                "SELECT * FROM retrieval_vectors ORDER BY chunk_rowid"
            )
        }
        metadata = {}
        for table in (
            "document_metadata",
            "document_metadata_history",
            "legal_classifications",
            "legal_relations",
        ):
            metadata[table] = digest(
                [
                    dict(r)
                    for r in conn.execute(
                        'SELECT * FROM "' + table + '" ORDER BY rowid'
                    )
                ]
            )
    return {"documents": documents, "vectors": vectors, "metadata": metadata}


def metadata_rows(service):
    keys = {
        "document_metadata": ("document_id",),
        "document_metadata_history": ("document_id", "revision"),
        "legal_classifications": ("document_id", "collection"),
        "legal_relations": ("id",),
    }
    with closing(service.db.connect()) as conn:
        return {
            table: {
                json.dumps([row[key] for key in columns]): digest(dict(row))
                for row in conn.execute('SELECT * FROM "' + table + '"')
            }
            for table, columns in keys.items()
        }


def existing_metadata_intact(service, guard):
    current = metadata_rows(service)
    return all(
        current[table].get(key) == value
        for table, rows in guard["rows"].items()
        for key, value in rows.items()
    )


def document_worker_limit(http_workers, request_concurrency, requested=None):
    ceiling = min(32, (http_workers + request_concurrency - 1) // request_concurrency)
    if requested is not None and not 1 <= requested <= ceiling:
        raise ValueError(
            "Document workers must fit the shared HTTP and 32-document limits"
        )
    if requested is None and http_workers > 32 and request_concurrency == 1:
        raise ValueError("More than 32 HTTP workers requires --request-concurrency 2")
    return requested or ceiling


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--models-env", type=Path, required=True)
    parser.add_argument(
        "--admission-path",
        type=Path,
        help="Runtime SQLite budget shared with the API process",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--supervise",
        action="store_true",
        help="Persistently resume safe work after cooldowns",
    )
    parser.add_argument("--supervisor-window-seconds", type=int, default=300)
    parser.add_argument(
        "--supervisor-max-document-workers", type=int, choices=range(1, 33)
    )
    parser.add_argument("--http-workers", type=int, choices=range(1, 65), default=2)
    parser.add_argument(
        "--document-workers",
        type=int,
        help="Active background documents within the shared ceiling",
    )
    parser.add_argument("--batch-size", type=int, choices=range(1, 65), default=6)
    parser.add_argument("--request-concurrency", type=int, choices=(1, 2), default=1)
    parser.add_argument("--stop-after-claims", type=int, default=0)
    parser.add_argument("--http-timeout", type=int, choices=range(1, 61), default=30)
    parser.add_argument(
        "--max-run-seconds",
        type=int,
        default=0,
        help="Stop new work after this window, then drain",
    )
    retry = parser.add_mutually_exclusive_group()
    retry.add_argument(
        "--retry-unresolved",
        action="store_true",
        help="Explicitly allow inputs from earlier issued requests with no saved result; billing may recur",
    )
    retry.add_argument(
        "--retry-unresolved-selection",
        type=Path,
        help="Allow only exact input hashes in an owner/model/frozen-plan recovery file; billing may recur",
    )
    args = parser.parse_args()
    try:
        document_workers = document_worker_limit(
            args.http_workers, args.request_concurrency, args.document_workers
        )
    except ValueError as error:
        parser.error(str(error))
    if args.max_run_seconds < 0:
        parser.error("Run window must be nonnegative")
    if args.supervise:
        if not args.apply or args.retry_unresolved:
            parser.error(
                "Supervision requires --apply and forbids broad --retry-unresolved"
            )
        if args.supervisor_window_seconds < 30:
            parser.error("Supervision windows must be at least 30 seconds")
        from embedding_supervisor import supervise

        return supervise(args, Path(__file__), sys.argv[1:], document_workers)
    from processing_runtime import retry_local_database
    from verify_ingestion import configure_models, write_report

    configure_models(args.models_env)
    for key in tuple(os.environ):
        if key.startswith(("LLM_", "RERANK_", "VISION_")):
            os.environ.pop(key)
    os.environ.update(
        VISION_OCR_ENABLED="false",
        TEA_PROCESS_WORKERS="32",
        TEA_EMBED_WORKERS=str(document_workers),
        EMBEDDING_INPUT_CACHE="true",
        EMBEDDING_BATCH_SIZE=str(args.batch_size),
        EMBEDDING_HTTP_WORKERS=str(args.http_workers),
        EMBEDDING_REQUEST_CONCURRENCY=str(args.request_concurrency),
        EMBEDDING_HTTP_TIMEOUT_SECONDS=str(args.http_timeout),
        EMBEDDING_RETRY_AMBIGUOUS_TIMEOUTS="false",
    )
    from app import ingestion
    from app.embedding_reuse import InputCheckpointCache
    from app.ingestion_quality import text_complete
    from app.retrieval import embedding_text, input_hash
    from app.service import Service

    plan = json.loads(args.plan.read_text())
    os.environ["EMBEDDING_ADMISSION_PATH"] = str(
        args.admission_path
        or os.getenv("EMBEDDING_ADMISSION_PATH")
        or ROOT / ".dev" / "embedding-admission.sqlite3"
    )
    ids = [d["id"] for d in plan["documents"]]
    assert ids and len(ids) == len(set(ids))
    data = Path(plan["data_dir"]).resolve()
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = (output / "runner.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    service = Service(data)
    owner, fingerprint = plan["owner"], service.embedding_fingerprint()
    assert fingerprint and fingerprint == plan["fingerprint"]
    document_inputs = {}
    for item in plan["documents"]:
        with closing(service.db.connect()) as conn:
            doc = service.own_document(conn, owner, item["id"])
            assert all(
                doc[k] == item[k]
                for k in (
                    "version",
                    "content_hash",
                    "source_url",
                    "size",
                    "storage_path",
                )
            )
            assert service.archive.can_index(conn, doc["id"])
            manifest = json.loads(doc["manifest"])
            assert doc["role"] == "primary" and text_complete(manifest)
            assert (
                service.catalog.metadata_for(doc["id"], conn=conn)["metadata"][
                    "review_status"
                ]
                == "draft"
            )
            chunks = conn.execute(
                "SELECT * FROM retrieval_chunks WHERE document_id=? ORDER BY ordinal,rowid",
                (doc["id"],),
            ).fetchall()
            expected = [c for c in manifest["chunks"] if str(c.get("text", "")).strip()]
            assert len(chunks) == len(expected)
            for chunk, expected_chunk in zip(chunks, expected, strict=True):
                assert chunk["retrieval_text"] == embedding_text(expected_chunk)
                assert chunk["input_hash"] == input_hash(chunk["retrieval_text"])
                assert (
                    chunk["text"] == expected_chunk["text"]
                    and json.loads(chunk["locator"]) == expected_chunk["locator"]
                )
            document_inputs[doc["id"]] = {chunk["input_hash"] for chunk in chunks}
        with Path(item["storage_path"]).open("rb") as stream:
            assert (
                hashlib.file_digest(stream, "sha256").hexdigest()
                == item["content_hash"]
            )
    baseline_path = output / "baseline.json"
    baseline = (
        json.loads(baseline_path.read_text())
        if baseline_path.exists()
        else capture(service, ids)
    )
    if not baseline_path.exists():
        write_report(output, baseline, "baseline.json")
    current = capture(service, ids)
    assert current["documents"] == baseline["documents"]
    assert all(current["vectors"].get(k) == v for k, v in baseline["vectors"].items())
    guard_path = output / "existing-metadata-guard.json"
    if guard_path.exists():
        metadata_guard = json.loads(guard_path.read_text())
        assert metadata_guard["legacy_metadata"] == baseline["metadata"]
    else:
        # Upgrade only while all pre-existing rows still match the original
        # whole-table evidence. Keep that original baseline file untouched.
        assert current["metadata"] == baseline["metadata"]
        with closing(service.db.connect()) as conn:
            old_ids = [
                r[0] for r in conn.execute("SELECT id FROM documents ORDER BY id")
            ]
        metadata_guard = {
            "protocol": "existing-metadata-stable-keys-v1",
            "legacy_metadata": baseline["metadata"],
            "document_ids": old_ids,
            "rows": metadata_rows(service),
        }
        write_report(output, metadata_guard, guard_path.name)
    assert existing_metadata_intact(service, metadata_guard)
    cache = InputCheckpointCache(
        output / "successful-inputs.sqlite3",
        {"owner": owner, "fingerprint": fingerprint, "source_plan": digest(plan)},
    )
    with closing(sqlite3.connect(cache.path)) as conn:
        saved = {row[0] for row in conn.execute("SELECT input_hash FROM inputs")}
    unresolved = unresolved_issued_inputs(output, saved)
    recovery_selection = (
        read_unresolved_selection(
            args.retry_unresolved_selection,
            plan,
            set().union(*document_inputs.values()),
        )
        if args.retry_unresolved_selection
        else None
    )
    allowed_unresolved = (
        unresolved
        if args.retry_unresolved
        else unresolved.intersection(
            recovery_selection["input_hashes"] if recovery_selection else ()
        )
    )
    consumed_recovery = (
        consumed_recovery_inputs(output, recovery_selection["sha256"]).intersection(
            recovery_selection["input_hashes"]
        )
        if recovery_selection
        else set()
    )
    allowed_unresolved -= consumed_recovery
    quarantined_inputs = unresolved - allowed_unresolved
    deferred_documents = deferred_input_documents(document_inputs, quarantined_inputs)
    observations = Observations(output)
    observations.install()
    original_embed = ingestion.embed_texts

    def cached(texts):
        try:
            return cache.embed(texts, original_embed)
        except (OSError, sqlite3.Error) as error:
            observations.failure = type(error).__name__
            observations.stop.set()
            raise

    ingestion.embed_texts = cached
    signal.signal(signal.SIGTERM, lambda *_: observations.stop.set())
    signal.signal(signal.SIGINT, lambda *_: observations.stop.set())
    placeholders = ",".join("?" for _ in ids)
    admission_ids = [ident for ident in ids if ident not in deferred_documents]
    started = time.monotonic()
    finished = 0
    active = {}
    run_id = str(time.time_ns())
    observations.run_id = run_id
    write_report(
        output,
        {
            "input_hashes": sorted(unresolved),
            "allowed_input_hashes": sorted(allowed_unresolved),
            "quarantined_input_hashes": sorted(quarantined_inputs),
            "recovery_selection": recovery_selection,
            "deferred_document_ids": sorted(deferred_documents),
        },
        "unresolved-" + run_id + ".json",
    )

    last_checkpoint = 0

    def progress():
        nonlocal last_checkpoint
        with closing(service.db.connect()) as conn:
            jobs = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM processing_jobs WHERE document_id IN ({placeholders})",
                    ids,
                )
            ]
            pending = conn.execute(
                f"""SELECT count(*),count(distinct c.input_hash) FROM retrieval_chunks c
              LEFT JOIN retrieval_vectors v ON v.chunk_rowid=c.rowid
              WHERE c.document_id IN ({placeholders}) AND (v.chunk_rowid IS NULL OR v.fingerprint!=? OR v.input_hash!=c.input_hash)""",
                (*ids, fingerprint),
            ).fetchone()
            count = conn.execute("SELECT count(*) FROM retrieval_vectors").fetchone()[0]
        report = {
            "run_id": run_id,
            "pid": os.getpid(),
            "configuration": {
                "http_workers": args.http_workers,
                "document_workers": document_workers,
                "batch_size": args.batch_size,
                "request_concurrency": args.request_concurrency,
                "http_timeout": args.http_timeout,
                "max_run_seconds": args.max_run_seconds,
                "retry_unresolved": args.retry_unresolved,
                "retry_unresolved_selection_sha256": (
                    recovery_selection["sha256"] if recovery_selection else None
                ),
            },
            "status": "running" if active else "idle",
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "fixed_documents": len(ids),
            "completed_documents": sum(j["status"] == "completed" for j in jobs),
            "failed_documents": sum(j["status"] == "failed" for j in jobs),
            "unresolved_issued_inputs_at_start": len(unresolved),
            "explicitly_allowed_unresolved_inputs": len(allowed_unresolved),
            "consumed_recovery_inputs": len(consumed_recovery.intersection(unresolved)),
            "quarantined_inputs": len(quarantined_inputs),
            "deferred_documents": len(deferred_documents),
            "runnable_documents": sum(
                j["status"] in {"queued", "processing"}
                and j["document_id"] not in deferred_documents
                for j in jobs
            ),
            "active_claims": len(active),
            "finished_claims_this_process": finished,
            "missing_rows": pending[0],
            "unique_missing_inputs": pending[1],
            "total_vectors": count,
            "provider": observations.summary(),
            "shared_admission": ingestion._EMBEDDING_HTTP_SLOTS.snapshot(),
            "checkpoint_cache": cache.snapshot(),
            "database_waits": len(observations.database_waits),
            "runtime_errors": list(observations.runtime_errors),
        }
        write_report(output, report, "progress.json")
        milestone = report["checkpoint_cache"]["new_saved_this_process"] // 200
        if milestone > last_checkpoint:
            # The receipt database commits every successful HTTP batch. These
            # immutable milestones add a compact audit snapshot every 200 inputs.
            write_report(
                output, report, f"checkpoint-{run_id}-{milestone * 200:08}.json"
            )
            last_checkpoint = milestone
        return report, jobs

    report, jobs = progress()
    write_report(output, report, "preflight-" + run_id + ".json")
    print(json.dumps(report), flush=True)
    if not args.apply:
        return 0
    next_report = 0
    storage_failed = False

    def local_database(operation, function):
        return retry_local_database(
            function,
            on_wait=lambda **details: observations.database_wait(operation, **details),
        )

    with ThreadPoolExecutor(max_workers=document_workers) as pool:
        while True:
            finished += finish_claims(active, observations)
            if (
                args.max_run_seconds
                and time.monotonic() - started >= args.max_run_seconds
            ):
                observations.stop.set()
            if (
                args.stop_after_claims
                and finished + len(active) >= args.stop_after_claims
            ):
                observations.stop.set()
            try:
                if not observations.stop.is_set() and len(active) < document_workers:
                    queued = admission_candidates(
                        service, owner, admission_ids, document_workers
                    )
                    for ident in queued:
                        if ident in deferred_documents or any(
                            c["document_id"] == ident for c in active.values()
                        ):
                            continue
                        if (
                            len(active) >= document_workers
                            or observations.stop.is_set()
                        ):
                            break
                        claim = local_database(
                            "claim",
                            lambda ident=ident: service.collection.claim_processing(
                                owner_id=owner, document_ids=[ident]
                            ),
                        )
                        if claim:
                            assert claim["phase"] == "embed"
                            active[
                                pool.submit(service.collection.process_item, claim)
                            ] = claim
                if active and not storage_failed:
                    local_database(
                        "heartbeat",
                        lambda: service.collection.heartbeat(
                            [(True, c) for c in active.values()]
                        ),
                    )
            except Exception as error:  # noqa: BLE001 - stop admission, drain peers and report
                storage_failed = True
                observations.runtime_error("local_admission_or_heartbeat", error)
            if time.monotonic() >= next_report:
                report, jobs = progress()
                print(json.dumps(report), flush=True)
                next_report = time.monotonic() + 20
            if not active:
                report, jobs = progress()
                if observations.stop.is_set() or not any(
                    j["status"] in {"queued", "processing"}
                    and j["document_id"] not in deferred_documents
                    for j in jobs
                ):
                    break
            time.sleep(0.5)
    after = capture(service, ids)
    report, jobs = progress()
    integrity = {
        "selected_originals_and_all_text_unchanged": after["documents"]
        == baseline["documents"],
        "all_old_vectors_exact_bytes": all(
            after["vectors"].get(k) == v for k, v in baseline["vectors"].items()
        ),
        "metadata_classifications_relations_unchanged": existing_metadata_intact(
            service, metadata_guard
        ),
    }
    report.update(
        status="complete" if report["missing_rows"] == 0 else "stopped",
        workers_running=False,
        integrity=integrity,
        failure=observations.failure,
    )
    write_report(output, report, "report-" + run_id + ".json")
    write_report(output, report, "report.json")
    print(json.dumps(report), flush=True)
    assert all(integrity.values()) and not active and observations.active == 0
    return 0 if not report["failed_documents"] and not observations.failure else 3


if __name__ == "__main__":
    raise SystemExit(main())
