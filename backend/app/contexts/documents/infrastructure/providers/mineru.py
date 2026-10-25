"""Durable MinerU precise API jobs, shared admission, and source-bound imports."""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import random
import re
import stat
import time
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx

from app.contexts.documents.infrastructure.parsers.document_parser import _run_bounded
from app.contexts.documents.infrastructure.providers.mineru_results import (
    MAX_RESULT_BYTES,
    MAX_SOURCE_BYTES,
    VERSION,
    reference,
    resolve_reference,
    worker,
)
from app.shared.domain.values import dump, now, uid
from app.shared.infrastructure.persistence.blob_store import BlobStore, _verify_existing

SCHEMA_VERSION = 20
API_BASE = "https://mineru.net/api/v4"
SUPPORTED = {
    ".pdf",
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".png",
    ".jpg",
    ".jpeg",
    ".jp2",
    ".webp",
    ".gif",
    ".bmp",
    ".html",
    ".htm",
}
ACTIVE = (
    "allocating",
    "allocated",
    "uploading",
    "uploaded",
    "waiting-file",
    "pending",
    "running",
    "converting",
)


def supports(filename, mime_type=""):
    extension = Path(filename).suffix.lower()
    # MinerU's HTML API has a separate model/result schema. The project uses
    # its source-mapped HTML5 pipeline for HTML instead of routing it through
    # the PDF/Office VLM worker.
    if extension in {".html", ".htm"} or mime_type.split(";", 1)[0].strip().lower() == "text/html":
        return False
    return extension in SUPPORTED or mime_type == "application/pdf"


def pending_manifest(filename, content_hash):
    return {
        "parser": "mineru",
        "parser_version": "api-v4",
        "pipeline_version": VERSION,
        "source_hash": content_hash,
        "title": Path(filename).stem,
        "elements": [],
        "chunks": [],
        "warnings": [],
        "quality": {"complete": False},
        "remote_parse": {"provider": "mineru", "source_hash": content_hash, "state": "planned"},
    }


def _number(name, default, minimum, maximum):
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} 配置无效。") from exc
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 至 {maximum} 之间。")
    return value


def parse_profile_hash(profile, retry_generation=None):
    """Return a stable parse identity, with an explicit opt-in for fresh runs."""
    if retry_generation is None:
        payload = profile
    else:
        if not isinstance(retry_generation, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,100}", retry_generation
        ):
            raise ValueError("MinerU 重试批次标识无效。")
        payload = {"profile": profile, "retry_generation": retry_generation}
    return hashlib.sha256(dump(payload).encode()).hexdigest()


class MinerUWait(ValueError):
    def __init__(self, code="rate_wait", retry_after=15):
        super().__init__("MinerU 正在等待远端容量，任务和原件已保留。")
        self.code, self.retry_after = str(code), max(1, retry_after)


class MinerUError(ValueError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__(f"MinerU 精准解析暂不能继续（错误码 {self.code}）；原件和任务回执已保留。")


class Admission:
    """Cross-process spacing/backoff without sleeping in a processing worker."""

    def __init__(self, data_dir):
        self.directory = Path(data_dir) / ".runtime"
        if self.directory.is_symlink():
            raise ValueError("MinerU 运行目录不能使用符号链接。")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        self.interval = _number("MINERU_REQUEST_INTERVAL_SECONDS", 1, 0.25, 60)

    @contextmanager
    def state(self):
        fd = os.open(self.directory / "mineru-api.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise ValueError("MinerU 准入状态必须是独立普通文件。")
            fcntl.flock(fd, fcntl.LOCK_EX)
            raw = os.read(fd, 8193)
            if len(raw) > 8192:
                raise ValueError("MinerU 准入状态损坏。")
            value = json.loads(raw) if raw else {}
            yield value
            encoded = json.dumps(value, allow_nan=False).encode()
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, encoded)
            os.ftruncate(fd, len(encoded))
            os.fsync(fd)
        finally:
            os.close(fd)

    def take(self, *, submission=False):
        with self.state() as state:
            delay = (
                max(
                    state.get("next_at", 0),
                    state.get("blocked_until", 0),
                    state.get("quota_until", 0) if submission else 0,
                )
                - time.time()
            )
            if delay > 0:
                raise MinerUWait(retry_after=delay)
            state["next_at"] = time.time() + self.interval

    def _capacity(self, state):
        initial = int(_number("MINERU_MAX_ACTIVE_TASKS", 8, 1, 256))
        enabled = os.getenv("MINERU_AUTO_CONCURRENCY", "false").lower() == "true"
        ceiling = max(initial, int(_number("MINERU_CONCURRENCY_CEILING", 128, 1, 256)))
        window = _number("MINERU_CONCURRENCY_WINDOW_SECONDS", 180, 60, 3600)
        signature = [enabled, initial, ceiling, window]
        tuning = state.get("capacity")
        if tuning and tuning.get("configuration") != signature:
            raise MinerUError("capacity_configuration_changed")
        if not tuning:
            tuning = {
                "configuration": signature,
                "limit": min(initial, ceiling) if enabled else initial,
                "stable_limit": 0,
                "unstable_limit": None,
                "mode": "exploring" if enabled else "manual",
                "history": [],
                "started_at": time.time(),
                "last_at": time.time(),
                "last_active": 0,
                "peak_active": 0,
                "loaded_seconds": 0,
                "completed": 0,
                "completed_pages": 0,
                "samples": 0,
                "errors": 0,
                "consecutive_errors": 0,
            }
            state["capacity"] = tuning
        return tuning

    @staticmethod
    def _stage(tuning, limit, mode, reason):
        tuning["history"] = (
            tuning["history"]
            + [
                {
                    "limit": tuning["limit"],
                    "started_at": tuning["started_at"],
                    "ended_at": time.time(),
                    "peak_active": tuning["peak_active"],
                    "loaded_seconds": round(tuning["loaded_seconds"], 1),
                    "completed": tuning["completed"],
                    "completed_pages": tuning["completed_pages"],
                    "samples": tuning["samples"],
                    "errors": tuning["errors"],
                    "reason": reason,
                }
            ]
        )[-12:]
        tuning.update(
            limit=limit,
            mode=mode,
            reason=reason,
            started_at=time.time(),
            last_at=time.time(),
            last_active=0,
            peak_active=0,
            loaded_seconds=0,
            completed=0,
            completed_pages=0,
            samples=0,
            errors=0,
            consecutive_errors=0,
        )

    def capacity(self, active=None, *, completed_pages=None):
        """Probe only with real, saturated work; retain evidence across restarts."""
        with self.state() as state:
            tuning = self._capacity(state)
            timestamp = time.time()
            # Observations may be sparse after a stopped worker. Never credit
            # an unobserved gap as sustained load.
            if tuning["last_active"] >= tuning["limit"]:
                tuning["loaded_seconds"] += min(30, max(0, timestamp - tuning["last_at"]))
            tuning["last_at"] = timestamp
            if active is not None:
                tuning["last_active"] = active
                tuning["peak_active"] = max(tuning["peak_active"], active)
            if completed_pages is not None:
                tuning["completed"] += 1
                tuning["completed_pages"] += completed_pages
            if (
                tuning["mode"] not in {"manual", "holding"}
                and max(state.get("blocked_until", 0), state.get("quota_until", 0)) <= timestamp
            ):
                if tuning.get("reason") == "daily_quota":
                    self._stage(tuning, tuning["limit"], "exploring", "quota_window_resumed")
                window = tuning["configuration"][3]
                required = window * (3 if tuning["mode"] == "verifying" else 1)
                loaded = tuning["loaded_seconds"] >= required * 0.5
                enough = tuning["completed"] >= max(8, (tuning["limit"] + 1) // 2)
                if (
                    timestamp - tuning["started_at"] >= required
                    and loaded
                    and enough
                    and tuning["errors"] == 0
                ):
                    tuning["stable_limit"] = max(tuning["stable_limit"], tuning["limit"])
                    lower, upper = tuning["stable_limit"], tuning["unstable_limit"]
                    ceiling = tuning["configuration"][2]
                    if tuning["mode"] == "verifying":
                        self._stage(tuning, lower, "holding", "verified_after_backoff")
                    elif upper and upper - lower <= 1:
                        self._stage(tuning, lower, "verifying", "boundary_found")
                    elif lower >= ceiling:
                        self._stage(tuning, lower, "holding", "configured_ceiling_reached")
                    else:
                        target = (
                            (lower + upper) // 2
                            if upper
                            else min(ceiling, max(lower + 1, (lower * 3 + 1) // 2))
                        )
                        self._stage(tuning, target, "exploring", "stable_stage")
            return dict(tuning)

    def outcome(self, code=None):
        with self.state() as state:
            tuning = self._capacity(state)
            tuning["samples"] += 1
            bucket = int(time.time() // 5) * 5
            recent = [item for item in tuning.get("recent", []) if bucket - item[0] < 60]
            if not recent or recent[-1][0] != bucket:
                recent.append([bucket, 0, 0])
            recent[-1][1] += 1
            recent[-1][2] += int(code is not None)
            tuning["recent"] = recent
            if code is None:
                tuning["consecutive_errors"] = 0
                return
            code = str(code)
            tuning["errors"] += 1
            tuning["consecutive_errors"] += 1
            tuning["last_error"] = {"code": code[:64], "at": time.time()}
            if tuning["mode"] == "manual":
                return
            if code in {"-60018", "-60019"}:
                # Daily quota is unrelated to parallel capacity. It must not
                # become a claimed concurrency ceiling.
                tuning["reason"] = "daily_quota"
                return
            samples, errors = sum(b[1] for b in recent), sum(b[2] for b in recent)
            unstable = (
                code in {"429", "-60009"}
                or tuning["consecutive_errors"] >= 3
                or (samples >= 20 and errors / samples > 0.05)
            )
            if not unstable:
                # An isolated transient invalidates this observation window,
                # but does not establish an upper capacity boundary.
                consecutive = tuning["consecutive_errors"]
                self._stage(tuning, tuning["limit"], "exploring", "transient_restart_window")
                tuning["consecutive_errors"] = consecutive
                return
            current = tuning["limit"]
            tuning["unstable_limit"] = min(tuning["unstable_limit"] or current, current)
            stable = tuning["stable_limit"]
            if stable >= current:
                stable = max(0, current // 2)
                tuning["stable_limit"] = stable
            target = stable or max(1, current // 2)
            self._stage(tuning, target, "exploring", "capacity_backoff")

    def backoff(self, code, retry_after=None):
        self.outcome(code)
        with self.state() as state:
            failures = min(8, state.get("failures", 0) + 1)
            delay = max(retry_after or 0, min(300, 15 * 2 ** (failures - 1)) + random.uniform(0, 3))
            if str(code) in {"-60018", "-60019"}:
                local = datetime.now(ZoneInfo("Asia/Shanghai"))
                midnight = (local + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
                delay = max(delay, midnight.timestamp() - time.time())
                state["quota_until"] = max(state.get("quota_until", 0), time.time() + delay)
            else:
                state.update(
                    failures=failures, blocked_until=max(state.get("blocked_until", 0), time.time() + delay)
                )
        return delay

    def healthy(self):
        self.outcome()
        with self.state() as state:
            state["failures"] = 0


class MinerUClient:
    def __init__(self, data_dir):
        self.files_dir = Path(data_dir) / "files"
        self.token = os.getenv("MINERU_API_TOKEN", "").strip()
        if not self.token:
            raise MinerUError("token_missing")
        self.admission = Admission(data_dir)

    def api(self, method, path, payload=None):
        self.admission.take(submission=method == "POST")
        try:
            with httpx.Client(
                timeout=httpx.Timeout(30, connect=10), trust_env=False, follow_redirects=False
            ) as client:
                with client.stream(
                    method, API_BASE + path, headers={"Authorization": "Bearer " + self.token}, json=payload
                ) as response:
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    raw = bytearray()
                    for block in response.iter_bytes():
                        raw.extend(block)
                        if len(raw) > 2 * 1024 * 1024:
                            raise MinerUError("response_size")
            if status == 429 or status >= 500:
                from app.shared.infrastructure.http.download import _retry_after

                raise MinerUWait(status, self.admission.backoff(status, _retry_after(retry_after)))
            if status in {401, 403}:
                raise MinerUError("authentication")
            if status != 200:
                raise MinerUError(f"http_{status}")
            payload = json.loads(raw)
            code = payload.get("code") if isinstance(payload, dict) else None
            if code != 0:
                if str(code) in {"-10001", "-60001", "-60007", "-60009", "-60018", "-60019"}:
                    raise MinerUWait(code, self.admission.backoff(code))
                raise MinerUError(code or "invalid_response")
            if not isinstance(payload.get("data"), dict):
                raise MinerUError("invalid_data")
            self.admission.healthy()
            return payload["data"]
        except httpx.HTTPError as exc:
            raise MinerUWait(type(exc).__name__, self.admission.backoff("network")) from None
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise MinerUError("invalid_json") from None

    def upload(self, url, blob):
        from app.shared.infrastructure.http.download import (
            _PinnedHTTPSConnection,
            _public_addresses,
            _validated_url,
        )

        url, host = _validated_url(url)
        if not (host.endswith(".aliyuncs.com") or host.endswith(".openxlab.org.cn") or host == "mineru.net"):
            raise MinerUError("upload_host")
        blob_path = resolve_reference(blob, self.files_dir)
        _verify_existing(blob_path, blob["content_hash"], blob["size"])
        addresses = _public_addresses(host)
        connection = _PinnedHTTPSConnection(host, addresses[0], 30)
        connection.alternate_addresses = addresses[1:]
        connection.deadline = time.monotonic() + 120
        try:
            target = urlsplit(url)
            with blob_path.open("rb") as content:
                connection.request(
                    "PUT",
                    target.path + ("?" + target.query if target.query else ""),
                    body=content,
                    headers={"Content-Length": str(blob["size"])},
                )
                response = connection.getresponse()
                if response.status not in {200, 201, 204}:
                    raise MinerUWait(f"upload_{response.status}", 30)
        except (OSError, TimeoutError):
            # The same batch/file object is polled before another PUT. Never
            # allocate a fresh parsing task after an uncertain upload outcome.
            raise MinerUWait("upload_unknown", 15) from None
        finally:
            connection.close()

    def download(self, url, store, owner):
        from app.shared.infrastructure.http.download import _download

        result = _download(
            url,
            deadline=time.monotonic() + 120,
            consume=lambda chunks: store.put(owner, chunks),
            max_bytes=MAX_RESULT_BYTES,
        )
        return reference(result["blob"])


class MinerUParser:
    def __init__(self, collection):
        self.collection, self.db = collection, collection.db
        self.service = collection.service
        self.store = BlobStore(self.db.files_dir, max_bytes=MAX_SOURCE_BYTES)
        self._migrate()

    def _migrate(self):
        with self.db.transaction() as conn:
            if not conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version=?", (SCHEMA_VERSION,)
            ).fetchone():
                conn.execute("""CREATE TABLE document_parse_tasks(
                    id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES documents(id),
                    owner_id TEXT NOT NULL REFERENCES users(id),source_hash TEXT NOT NULL,
                    profile_hash TEXT NOT NULL,part_index INTEGER NOT NULL,first_page INTEGER NOT NULL,
                    last_page INTEGER,part_blob TEXT NOT NULL CHECK(json_valid(part_blob)),
                    state TEXT NOT NULL,batch_id TEXT,private_data TEXT NOT NULL CHECK(json_valid(private_data)),
                    result_blob TEXT CHECK(result_blob IS NULL OR json_valid(result_blob)),
                    progress TEXT NOT NULL CHECK(json_valid(progress)),attempts INTEGER NOT NULL DEFAULT 0,
                    next_at REAL NOT NULL DEFAULT 0,error_code TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
                    UNIQUE(document_id,source_hash,profile_hash,part_index))""")
                conn.execute("CREATE INDEX document_parse_due ON document_parse_tasks(state,next_at,id)")
                conn.execute(
                    "CREATE INDEX document_parse_owner ON document_parse_tasks(owner_id,document_id,source_hash)"
                )
                conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (SCHEMA_VERSION, now()))
            conn.execute(
                "SELECT id,document_id,owner_id,source_hash,profile_hash,part_index,first_page,last_page,part_blob,state,batch_id,private_data,result_blob,progress,attempts,next_at,error_code FROM document_parse_tasks LIMIT 0"
            )

    @staticmethod
    def profile():
        # The precise VLM output keeps tables/formulas. Lightweight Agent and
        # legacy local/OCR fallbacks are deliberately absent from this path.
        return {
            "provider": "mineru",
            "api_version": "v4",
            "model_version": "vlm",
            "enable_table": True,
            "enable_formula": True,
            "is_ocr": True,
            "language": os.getenv("MINERU_LANGUAGE", "en").strip() or "en",
            "mapping_version": VERSION,
        }

    def _guard(self, conn, doc, claim):
        if not self.collection._valid_claim(conn, claim, True):
            raise MinerUError("lease_changed")
        current = self.service.own_document(conn, doc["owner_id"], doc["id"])
        if any(
            current[k] != doc[k]
            for k in ("content_hash", "version", "owner_id", "role", "size", "storage_path")
        ):
            raise MinerUError("source_changed")

    def _tasks(self, doc, fingerprint):
        with closing(self.db.connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM document_parse_tasks WHERE document_id=? AND owner_id=? AND source_hash=? AND profile_hash=? ORDER BY part_index",
                (doc["id"], doc["owner_id"], doc["content_hash"], fingerprint),
            ).fetchall()
        result = []
        for row in rows:
            task = dict(row)
            for key in ("part_blob", "private_data", "result_blob", "progress"):
                task[key] = json.loads(task[key]) if task[key] is not None else None
            result.append(task)
        return result

    def _save(self, doc, claim, task, **changes):
        was_done = task["state"] == "done"
        allowed = {
            "state",
            "batch_id",
            "private_data",
            "result_blob",
            "progress",
            "next_at",
            "error_code",
            "attempts",
        }
        if not changes or set(changes) - allowed:
            raise ValueError("MinerU 任务状态变更字段无效。")
        with self.db.transaction() as conn:
            self._guard(conn, doc, claim)
            values = [
                dump(value) if key in {"private_data", "result_blob", "progress"} else value
                for key, value in changes.items()
            ]
            conn.execute(
                "UPDATE document_parse_tasks SET "
                + ",".join(key + "=?" for key in changes)
                + ",updated_at=? WHERE id=? AND owner_id=? AND source_hash=?",
                (*values, now(), task["id"], doc["owner_id"], doc["content_hash"]),
            )
        task.update(changes)
        with closing(self.db.connect()) as conn:
            active = conn.execute(
                "SELECT count(*) FROM document_parse_tasks WHERE state IN ("
                + ",".join("?" for _ in ACTIVE)
                + ")",
                ACTIVE,
            ).fetchone()[0]
        completed_pages = (
            ((task["last_page"] - task["first_page"] + 1) if task["last_page"] else 0)
            if task["state"] == "done" and not was_done
            else None
        )
        Admission(self.db.data_dir).capacity(active, completed_pages=completed_pages)

    def _reserve(self, doc, claim, task):
        admission = Admission(self.db.data_dir)
        with self.db.transaction() as conn:
            self._guard(conn, doc, claim)
            active = conn.execute(
                "SELECT count(*) FROM document_parse_tasks WHERE state IN ("
                + ",".join("?" for _ in ACTIVE)
                + ")",
                ACTIVE,
            ).fetchone()[0]
            maximum = admission.capacity(active)["limit"]
            if active >= maximum:
                raise MinerUWait("active_tasks", 15)
            conn.execute(
                "UPDATE document_parse_tasks SET state='allocating',updated_at=? WHERE id=? AND state='planned'",
                (now(), task["id"]),
            )
            admission.capacity(active + 1)
        task["state"] = "allocating"

    def step(self, doc, claim, manifest):
        if doc["role"] != "primary" or not supports(doc["filename"], doc["mime_type"]):
            raise MinerUError("unsupported_format")
        profile = self.profile()
        retry_generation = (manifest.get("remote_parse") or {}).get("retry_generation")
        fingerprint = parse_profile_hash(profile, retry_generation)
        tasks = self._tasks(doc, fingerprint)
        previous = manifest.get("remote_parse", {}).get("previous_manifest")
        if not tasks:
            # Persist original evidence before any remote operation. Splitting
            # only copies PDF objects; it performs no text/layout/OCR analysis.
            previous = reference(self.store.put_bytes(doc["owner_id"], dump(manifest).encode()))
            prepared = _run_bounded(
                worker,
                (
                    "prepare",
                    (
                        doc["storage_path"],
                        doc["content_hash"],
                        doc["size"],
                        doc["filename"],
                        str(self.db.files_dir),
                        doc["owner_id"],
                        doc["mime_type"],
                    ),
                ),
                120,
            )
            with self.db.transaction() as conn:
                self._guard(conn, doc, claim)
                for index, part in enumerate(prepared["parts"]):
                    conn.execute(
                        """INSERT INTO document_parse_tasks(id,document_id,owner_id,source_hash,profile_hash,part_index,first_page,last_page,part_blob,state,private_data,progress,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'planned','{}','{}',?,?)""",
                        (
                            uid(),
                            doc["id"],
                            doc["owner_id"],
                            doc["content_hash"],
                            fingerprint,
                            index,
                            part["first_page"],
                            part["last_page"],
                            dump(part["blob"]),
                            now(),
                            now(),
                        ),
                    )
                manifest["remote_parse"] = {
                    "provider": "mineru",
                    "source_hash": doc["content_hash"],
                    "state": "pending",
                    "profile": profile,
                    "profile_hash": fingerprint,
                    "page_count": prepared["page_count"],
                    "previous_manifest": previous,
                    **({"retry_generation": retry_generation} if retry_generation else {}),
                }
                conn.execute("UPDATE documents SET manifest=? WHERE id=?", (dump(manifest), doc["id"]))
            tasks = self._tasks(doc, fingerprint)
        client = MinerUClient(self.db.data_dir)
        poll = _number("MINERU_POLL_SECONDS", 15, 5, 300)
        # One action per lease keeps network work bounded and lets other files
        # progress. Poll existing jobs first so capacity is released promptly.
        candidates = sorted(
            (task for task in tasks if task["state"] != "done" and task["next_at"] <= time.time()),
            key=lambda task: (task["state"] == "planned", task["next_at"], task["part_index"]),
        )
        if candidates:
            task = candidates[0]
            if task["state"] in {"failed", "blocked"}:
                raise MinerUError(task["error_code"] or "task_failed")
            try:
                if task["state"] == "planned":
                    self._reserve(doc, claim, task)
                if task["state"] == "allocating":
                    # URL allocation alone never starts parsing; only uploading
                    # the exact file object below starts this known batch.
                    data = client.api(
                        "POST",
                        "/file-urls/batch",
                        {
                            "files": [
                                {
                                    "name": Path(doc["filename"]).stem
                                    + f"-part-{task['part_index'] + 1}"
                                    + Path(doc["filename"]).suffix,
                                    "data_id": task["id"],
                                    "is_ocr": True,
                                }
                            ],
                            **{
                                k: profile[k]
                                for k in ("model_version", "enable_table", "enable_formula", "language")
                            },
                        },
                    )
                    urls = data.get("file_urls")
                    if (
                        not isinstance(data.get("batch_id"), str)
                        or not isinstance(urls, list)
                        or len(urls) != 1
                        or not isinstance(urls[0], str)
                    ):
                        raise MinerUError("invalid_upload_receipt")
                    self._save(
                        doc,
                        claim,
                        task,
                        state="allocated",
                        batch_id=data["batch_id"],
                        private_data={"upload_url": urls[0]},
                        attempts=0,
                        next_at=0,
                    )
                elif task["state"] == "allocated":
                    self._save(doc, claim, task, state="uploading", next_at=time.time() + poll)
                    client.upload(task["private_data"]["upload_url"], task["part_blob"])
                    self._save(doc, claim, task, state="uploaded", next_at=time.time() + poll, attempts=0)
                else:
                    if not task["batch_id"] or not re.fullmatch(r"[A-Za-z0-9-]{8,100}", task["batch_id"]):
                        raise MinerUError("invalid_batch_binding")
                    data = client.api("GET", "/extract-results/batch/" + task["batch_id"])
                    items = data.get("extract_result")
                    if (
                        not isinstance(items, list)
                        or len(items) != 1
                        or items[0].get("data_id") != task["id"]
                    ):
                        raise MinerUError("task_result_binding")
                    item = items[0]
                    state = item.get("state")
                    if state == "done":
                        url = item.get("full_zip_url")
                        if not isinstance(url, str):
                            raise MinerUError("missing_result_url")
                        result = client.download(url, self.store, doc["owner_id"])
                        self._save(
                            doc,
                            claim,
                            task,
                            state="done",
                            result_blob=result,
                            progress={},
                            private_data={},
                            error_code=None,
                            next_at=0,
                        )
                    elif state == "failed":
                        self._save(
                            doc,
                            claim,
                            task,
                            state="failed",
                            private_data={},
                            error_code=str(item.get("err_code") or "remote_failed"),
                        )
                        raise MinerUError(task["error_code"])
                    elif state in {"waiting-file", "pending", "running", "converting"}:
                        # A request may die after its PUT succeeded. Poll first;
                        # only a confirmed waiting-file state permits re-PUT to
                        # this same object and batch, never a new submission.
                        ready = (
                            "allocated" if state == "waiting-file" and task["state"] == "uploading" else state
                        )
                        progress = item.get("extract_progress") or {}
                        self._save(
                            doc,
                            claim,
                            task,
                            state=ready,
                            progress={
                                k: progress[k]
                                for k in ("extracted_pages", "total_pages", "start_time")
                                if k in progress
                            },
                            attempts=0,
                            error_code=None,
                            next_at=time.time() + poll,
                        )
                    else:
                        raise MinerUError("invalid_task_state")
            except MinerUWait as exc:
                self._save(doc, claim, task, next_at=time.time() + exc.retry_after, error_code=exc.code)
            except MinerUError as exc:
                # Preserve a confirmed provider-terminal failure as a distinct
                # state. Recovery can submit a fresh generation for that part
                # while reusing successful and still-live batches.
                if task["state"] != "failed" or task["error_code"] != exc.code:
                    self._save(doc, claim, task, state="blocked", error_code=exc.code)
                raise
        tasks = self._tasks(doc, fingerprint)
        if all(task["state"] == "done" for task in tasks):
            result = _run_bounded(
                worker,
                (
                    "assemble",
                    (
                        tasks,
                        doc["content_hash"],
                        doc["filename"],
                        str(self.db.files_dir),
                        doc["owner_id"],
                        previous,
                    ),
                ),
                120,
            )
            result["remote_parse"].update(profile=profile, profile_hash=fingerprint)
            if retry_generation:
                result["remote_parse"]["retry_generation"] = retry_generation
            if manifest.get("mineru_chart_assets"):
                from app.contexts.documents.infrastructure.providers.native_asset_ocr import (
                    reuse_mineru_chart_checkpoints,
                )

                result = reuse_mineru_chart_checkpoints(manifest, result)
            return result
        remote = manifest.setdefault("remote_parse", {})
        remote.update(
            provider="mineru",
            source_hash=doc["content_hash"],
            state="pending",
            profile=profile,
            profile_hash=fingerprint,
            previous_manifest=previous,
            tasks=[
                {
                    "task_id": t["id"],
                    "first_page": t["first_page"],
                    "last_page": t["last_page"],
                    "state": t["state"],
                    "progress": t["progress"],
                    "error_code": t["error_code"],
                }
                for t in tasks
            ],
        )
        if retry_generation:
            remote["retry_generation"] = retry_generation
        remote["retry_after_seconds"] = max(
            1, min((t["next_at"] - time.time() for t in tasks if t["state"] != "done"), default=poll)
        )
        manifest["quality"] = {**manifest.get("quality", {}), "complete": False}
        return manifest


def manifest_valid(manifest):
    """No remote completion without immutable source, pages, and ZIP receipts."""
    try:
        remote = manifest["remote_parse"]
        source = manifest["source_hash"]
        from app.contexts.documents.infrastructure.providers.mineru_visual_page_retry import (
            EMPTY_PAGE_PROTOCOL,
            PROTOCOL as TABLE_PROTOCOL,
        )
        if (
            remote["state"] != "done"
            or remote["source_hash"] != source
            or not re.fullmatch(r"[a-f0-9]{64}", source)
        ):
            return False
        parts = remote["parts"]
        pages = manifest["pages"]
        if not parts or len(pages) != remote["page_count"]:
            return False
        expected, by_page = 1, {}
        for part in parts:
            if (
                part["source_hash"] != source
                or part["first_page"] != expected
                or part["last_page"] < expected
            ):
                return False
            if not re.fullmatch(r"[a-f0-9]{64}", part["result_blob"]["content_hash"]):
                return False
            for number in range(expected, part["last_page"] + 1):
                by_page[number] = part
            expected = part["last_page"] + 1
        if expected != len(pages) + 1:
            return False
        for number, page in enumerate(pages, 1):
            part, proof = by_page[number], page["ocr"]
            result_hash = part["result_blob"]["content_hash"]
            if page["page"] != number or page["source_hash"] != source or not isinstance(proof, dict):
                return False
            fallback = page.get("ocr_fallback")
            if fallback is not None:
                if not isinstance(fallback, dict) or fallback.get("protocol") not in {
                    TABLE_PROTOCOL,
                    EMPTY_PAGE_PROTOCOL,
                }:
                    return False
                if not isinstance(page.get("text"), str):
                    return False
                text_hash = hashlib.sha256(page["text"].encode()).hexdigest()
                image = proof.get("image_blob")
                cycles = page.get("ocr_retry_cycles")
                previous_mineru_page = False
                if isinstance(cycles, list):
                    for cycle in cycles:
                        previous = cycle.get("previous_page") if isinstance(cycle, dict) else None
                        previous_proof = previous.get("ocr") if isinstance(previous, dict) else None
                        if not isinstance(previous_proof, dict):
                            continue
                        try:
                            encoded_previous = json.dumps(
                                previous,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ).encode()
                        except (TypeError, ValueError):
                            continue
                        previous_mineru_page = previous_mineru_page or (
                            cycle.get("previous_page_sha256") == hashlib.sha256(encoded_previous).hexdigest()
                            and previous.get("page") == number
                            and previous.get("source_hash") == source
                            and isinstance(previous.get("text"), str)
                            and previous_proof.get("provider") == "mineru"
                            and previous_proof.get("task_id") == part["task_id"]
                            and previous_proof.get("provider_page_idx") == number - part["first_page"]
                            and previous_proof.get("result_hash") == result_hash
                            and previous_proof.get("text_sha256")
                            == hashlib.sha256(previous.get("text", "").encode()).hexdigest()
                        )
                if (
                    fallback.get("accepted") is not True
                    or fallback.get("source_page") != number
                    or fallback.get("source_result_hash") != result_hash
                    or fallback.get("text_sha256") != text_hash
                    or not previous_mineru_page
                    or proof.get("provider") != "vision"
                    or page.get("status") != "ocr_complete"
                    or proof.get("source_hash") != source
                    or proof.get("page") != number
                    or proof.get("text_sha256") != text_hash
                    or proof.get("complete") is not True
                    or proof.get("uncertain_spans")
                    or proof.get("prompt_version") != fallback.get("prompt_version")
                    or not isinstance(proof.get("prompt_version"), str)
                    or not isinstance(proof.get("request_id"), str)
                    or not re.fullmatch(r"[a-f0-9]{32}", proof["request_id"])
                    or not isinstance(proof.get("image_sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", proof["image_sha256"])
                    or not isinstance(image, dict)
                    or image.get("content_hash") != proof.get("image_sha256")
                    or image.get("mime_type") != "image/png"
                    or type(image.get("size")) is not int
                    or image["size"] <= 0
                    or (
                        fallback["protocol"] == TABLE_PROTOCOL
                        and "table_structure_ambiguous" in (page.get("reasons") or [])
                    )
                    or (
                        fallback["protocol"] == EMPTY_PAGE_PROTOCOL
                        and "empty_text_unverified" in (page.get("reasons") or [])
                    )
                ):
                    return False
                if fallback["protocol"] == EMPTY_PAGE_PROTOCOL:
                    text = page.get("text", "")
                    elements = manifest.get("elements", [])
                    fallback_elements = [
                        element
                        for element in elements
                        if isinstance(element, dict)
                        and isinstance(element.get("locator"), dict)
                        and element["locator"].get(
                            "physical_page", element["locator"].get("page")
                        ) == number
                        and (element.get("metadata_json") or {}).get("fallback_protocol")
                        == EMPTY_PAGE_PROTOCOL
                    ]
                    if not text.strip():
                        if proof.get("is_blank") is not True or fallback_elements:
                            return False
                    elif (
                        proof.get("is_blank") is not False
                        or len(fallback_elements) != 1
                        or fallback_elements[0].get("content_text") != text
                    ):
                        return False
                continue
            if (
                proof["provider"] != "mineru"
                or proof["task_id"] != part["task_id"]
                or proof["provider_page_idx"] != number - part["first_page"]
                or proof["result_hash"] != result_hash
                or proof["text_sha256"] != hashlib.sha256(page["text"].encode()).hexdigest()
            ):
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False
