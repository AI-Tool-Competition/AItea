"""Bounded model HTTP adapter for historical embedding operations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
import uuid
from collections import deque
from contextlib import AbstractContextManager, contextmanager
from contextvars import ContextVar
from pathlib import Path
from urllib.parse import urlsplit

from app.shared.infrastructure.http.download import _retry_after

_EMBEDDING_PROFILES: dict[str, dict] = {}


_EMBEDDING_PROFILE_LOCK = threading.Lock()


_EMBEDDING_PROFILE_LIMIT = 32


_EMBEDDING_PRIORITY = ContextVar("embedding_priority", default="bulk")


_EMBEDDING_WAIT = ContextVar("embedding_wait", default=None)


@contextmanager
def embedding_priority(priority):
    """Only an explicit interactive query gets priority over document indexing."""
    if priority not in {"query", "bulk"}:
        raise ValueError("Invalid embedding request priority")
    token = _EMBEDDING_PRIORITY.set(priority)
    try:
        yield
    finally:
        _EMBEDDING_PRIORITY.reset(token)


@contextmanager
def _embedding_wait(priority, *, defer=None):
    # One ticket survives the 250 ms cancellation/cooldown polls. Reticketing
    # every poll would let newer arrivals repeatedly overtake a long document.
    request = {"priority": priority, "token": object(), "cleanup": None, "defer": defer}
    token = _EMBEDDING_WAIT.set(request)
    try:
        yield
    finally:
        if request["cleanup"]:
            request["cleanup"]()
        _EMBEDDING_WAIT.reset(token)


def embedding_http_worker_limit() -> int:
    value = os.getenv("EMBEDDING_HTTP_WORKERS", "2").strip()
    if not value.isdigit() or not 1 <= int(value) <= 64:
        raise ValueError("EMBEDDING_HTTP_WORKERS 必须为 1 至 64 的整数。")
    return int(value)


def embedding_http_timeout() -> int:
    value = os.getenv("EMBEDDING_HTTP_TIMEOUT_SECONDS", "30").strip()
    if not value.isdigit() or not 1 <= int(value) <= 60:
        raise ValueError("EMBEDDING_HTTP_TIMEOUT_SECONDS 必须为 1 至 60 的整数。")
    return int(value)


def embedding_retry_ambiguous_timeouts() -> bool:
    value = os.getenv("EMBEDDING_RETRY_AMBIGUOUS_TIMEOUTS", "true").strip().lower()
    if value not in {"true", "false"}:
        raise ValueError("EMBEDDING_RETRY_AMBIGUOUS_TIMEOUTS 必须为 true 或 false。")
    return value == "true"


class _EmbeddingSlots:
    """Shared query priority with reserved query slots and a sustained bulk floor."""

    def __init__(self):
        self.condition = threading.Condition()
        self.limit = None
        self.active = {"query": 0, "bulk": 0}
        self.queues = {"query": deque(), "bulk": deque()}
        self.waiters = {}
        self.held = threading.local()
        self.peak = self.query_streak = 0
        self.grants = {"query": 0, "bulk": 0}
        self.max_wait = {"query": 0.0, "bulk": 0.0}
        self.shared = self.shared_configuration = None
        self.shared_held = threading.local()

    def _shared(self):
        path = os.getenv("EMBEDDING_ADMISSION_PATH", "").strip()
        if not path:
            if self.shared_configuration:
                raise ValueError("Shared embedding admission path changed; restart this client")
            return None
        from app.contexts.knowledge.infrastructure.providers.embedding_admission import (
            SharedEmbeddingBudget,
            model_scope,
        )

        base, _, model = model_service_config("EMBEDDING")
        config = (
            os.getpid(),
            str(Path(path).absolute()),
            model_scope(base, model),
            embedding_http_worker_limit(),
        )
        with self.condition:
            if self.shared_configuration != config:
                if self.shared_configuration and self.shared_configuration[0] == config[0]:
                    raise ValueError("Shared embedding admission configuration changed; restart this client")
                self.shared_held = threading.local()
                self.shared = SharedEmbeddingBudget(config[1], config[2], config[3])
                self.shared_configuration = config
            return self.shared

    def _shared_acquire(self, budget, blocking, timeout):
        from app.contexts.knowledge.infrastructure.providers.embedding_admission import (
            SharedEmbeddingCooldown,
        )

        request = _EMBEDDING_WAIT.get()
        lane = request["priority"] if request else _EMBEDDING_PRIORITY.get()
        token = request.setdefault("shared_token", uuid.uuid4().hex) if request else uuid.uuid4().hex
        if request:
            request["cleanup"] = lambda: budget.release(token, waiting_only=True)
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                try:
                    acquired = budget.try_acquire(token, lane)
                except SharedEmbeddingCooldown as error:
                    if request and request["defer"]:
                        request["defer"](error.retry_after)
                    raise EmbeddingProviderDeferred(retry_after=error.retry_after) from None
                if acquired:
                    if not hasattr(self.shared_held, "leases"):
                        self.shared_held.leases = []
                    self.shared_held.leases.append((budget, token))
                    return True
                remaining = None if deadline is None else deadline - time.monotonic()
                if not blocking or remaining is not None and remaining <= 0:
                    return False
                time.sleep(0.025 if remaining is None else min(0.025, remaining))
        finally:
            if request is None:
                budget.release(token, waiting_only=True)

    def _remove(self, token):
        waiter = self.waiters.pop(token, None)
        if waiter:
            self.queues[waiter[0]].remove(token)
            self.condition.notify_all()

    def _cancel(self, token):
        with self.condition:
            self._remove(token)

    def _next(self):
        if sum(self.active.values()) >= self.limit:
            return None
        query, bulk = bool(self.queues["query"]), bool(self.queues["bulk"])
        if query and bulk and self.limit == 1:
            return "query" if self.query_streak < 3 else "bulk"
        if bulk and self.active["bulk"] < max(1, self.limit // 4):
            return "bulk"
        if query:
            return "query"
        if bulk and self.active["bulk"] < self.limit - min(4, self.limit // 16):
            return "bulk"
        return None

    def acquire(self, blocking=True, timeout=None):
        if not blocking and timeout is not None:
            raise ValueError("Timeout cannot be used with nonblocking admission")
        shared = self._shared()
        if shared:
            return self._shared_acquire(shared, blocking, timeout)
        limit = embedding_http_worker_limit()
        request = _EMBEDDING_WAIT.get()
        lane = request["priority"] if request else _EMBEDDING_PRIORITY.get()
        token = request["token"] if request else object()
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.condition:
            if self.limit is None:
                self.limit = limit
            elif limit != self.limit:
                raise ValueError("EMBEDDING_HTTP_WORKERS 已变更，请重启处理进程。")
            if token not in self.waiters:
                self.waiters[token] = lane, time.monotonic()
                self.queues[lane].append(token)
                if request:
                    request["cleanup"] = lambda: self._cancel(token)
            try:
                while True:
                    if self._next() == lane and self.queues[lane][0] is token:
                        started = self.waiters[token][1]
                        self._remove(token)
                        self.active[lane] += 1
                        self.peak = max(self.peak, sum(self.active.values()))
                        self.grants[lane] += 1
                        self.max_wait[lane] = max(self.max_wait[lane], time.monotonic() - started)
                        self.query_streak = self.query_streak + 1 if lane == "query" else 0
                        if not hasattr(self.held, "lanes"):
                            self.held.lanes = []
                        self.held.lanes.append(lane)
                        return True
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if not blocking or remaining is not None and remaining <= 0:
                        return False
                    self.condition.wait(remaining)
            finally:
                if request is None:
                    self._remove(token)

    def release(self):
        shared = getattr(self.shared_held, "leases", [])
        if shared:
            budget, token = shared.pop()
            budget.release(token)
            return
        with self.condition:
            lanes = getattr(self.held, "lanes", [])
            if not lanes:
                raise ValueError("尚未取得向量请求并发名额。")
            self.active[lanes.pop()] -= 1
            self.condition.notify_all()

    def snapshot(self):
        shared = self._shared()
        if shared:
            return shared.snapshot()
        with self.condition:
            limit = self.limit or embedding_http_worker_limit()
            return {
                "scope": "process",
                "limit": limit,
                "query_reserved": min(4, limit // 16),
                "bulk_floor_when_waiting": max(1, limit // 4),
                "bulk_ceiling": limit - min(4, limit // 16),
                "active": dict(self.active),
                "waiting": {key: len(queue) for key, queue in self.queues.items()},
                "peak": self.peak,
                "grants": dict(self.grants),
                "maximum_wait_seconds": dict(self.max_wait),
            }

    def cooldown(self, delay):
        shared = self._shared()
        if shared:
            shared.cooldown(delay)


_EMBEDDING_HTTP_SLOTS = _EmbeddingSlots()


class EmbeddingServiceError(ValueError):
    """Provider failure; the durable queue owns long waits and outer retries.

    Evidence fields describe the last failed HTTP request. They do not prove
    that earlier batches in this complete call issued no billable requests.
    """

    def __init__(
        self,
        *,
        retryable: bool,
        retry_after: float | None = None,
        http_status: int | None = None,
        request_outcome_unknown: bool = False,
        request_sent: bool | None = None,
    ):
        super().__init__("Embedding 服务请求失败，请检查服务配置或稍后重试。")
        self.retryable = retryable
        self.retry_after = retry_after if retryable else None
        self.http_status = http_status
        self.request_outcome_unknown = request_outcome_unknown
        self.request_sent = request_sent


class EmbeddingProviderDeferred(EmbeddingServiceError):
    """Provider cooldown deferred a call that issued no HTTP."""

    def __init__(self, retry_after: float):
        super().__init__(retryable=True, retry_after=retry_after, request_sent=False)


def _embedding_profile_key() -> str | None:
    values = [os.getenv("EMBEDDING_" + name, "").strip() for name in ("BASE_URL", "API_KEY", "MODEL")]
    if not all(values):
        return None
    # Separate credentials/models without retaining their values in the bounded cache.
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def _embedding_profile(key: str | None, *, size=None, cooldown=None) -> dict:
    if key is None:
        return {}
    with _EMBEDDING_PROFILE_LOCK:
        previous = _EMBEDDING_PROFILES.pop(key, {})
        if size is not None:
            previous["size"] = min(previous.get("size", 64), size)
        if cooldown is not None:
            previous["available_at"] = max(previous.get("available_at", 0), time.monotonic() + cooldown)
        _EMBEDDING_PROFILES[key] = previous
        while len(_EMBEDDING_PROFILES) > _EMBEDDING_PROFILE_LIMIT:
            del _EMBEDDING_PROFILES[next(iter(_EMBEDDING_PROFILES))]
        return dict(previous)


def embedding_batch_size() -> int:
    """A configured ceiling, reduced after observed provider batch failures.

    Subsequent durable jobs checkpoint the learned size instead of repeating the
    oversized request. The profile is process-local and does not change model or
    input fingerprints; explicit provider tuning survives process restarts.
    """
    value = os.getenv("EMBEDDING_BATCH_SIZE", "64").strip()
    if not value.isdigit() or not 1 <= int(value) <= 64:
        raise ValueError("EMBEDDING_BATCH_SIZE 必须为 1 至 64 的整数。")
    return min(int(value), _embedding_profile(_embedding_profile_key()).get("size", 64))


def embedding_request_concurrency() -> int:
    """Operator opt-in; all callers share the configured process-wide HTTP slots."""
    value = os.getenv("EMBEDDING_REQUEST_CONCURRENCY", "1").strip()
    if value not in {"1", "2"}:
        raise ValueError("EMBEDDING_REQUEST_CONCURRENCY 必须为 1 或 2。")
    return int(value)


def embedding_claim_size() -> int:
    """Bounded durable claim size, separate from the actual HTTP batch ceiling."""
    return embedding_batch_size() * embedding_request_concurrency()


class _EmbeddingCallCancelled(Exception):
    """Internal cooperative cancellation; never becomes a queue failure."""


class _EmbeddingCall(AbstractContextManager):
    def __init__(self, profile_key: str | None):
        self.profile_key = profile_key
        self.priority = _EMBEDDING_PRIORITY.get()
        self.deadline = time.monotonic() + 300
        self.stop = threading.Event()
        self.preserve_inflight = False
        self.lock = threading.Lock()
        self.failures = self.active = self.requests = 0

    def __exit__(self, *args) -> None:
        # Exit before the executor context drains, including operator interrupts.
        self.stop.set()

    def check(self) -> None:
        if self.stop.is_set():
            raise _EmbeddingCallCancelled()
        if time.monotonic() >= self.deadline:
            raise EmbeddingServiceError(retryable=True)
        remaining = _embedding_profile(self.profile_key).get("available_at", 0) - time.monotonic()
        if remaining > 0:
            self.defer(remaining)

    def defer(self, delay: float) -> None:
        # Queue attempts are only reset when this complete call issued no HTTP.
        with self.lock:
            attempted = self.requests > 0
        if attempted:
            raise EmbeddingServiceError(retryable=True, retry_after=delay)
        raise EmbeddingProviderDeferred(retry_after=delay)

    def begin_request(self) -> None:
        self.check()
        with self.lock:
            # Reserve failures for in-flight requests too, so two retry lanes
            # together cannot exceed eight failed attempts in one complete call.
            if self.failures + self.active >= 8:
                raise EmbeddingServiceError(retryable=True)
            self.active += 1
            self.requests += 1

    def end_request(self, *, failed: bool) -> None:
        with self.lock:
            self.active -= 1
            self.failures += int(failed)


def _embedding_response(client, batch: list[str], base: str, key: str, model: str, call: _EmbeddingCall):
    import httpx

    # Normal contention is not a provider failure. Wait within this call's
    # existing budget and poll cancellation/cooldown, without issuing retries.
    with _embedding_wait(call.priority, defer=call.defer):
        while True:
            call.check()
            if _EMBEDDING_HTTP_SLOTS.acquire(timeout=max(0, min(0.25, call.deadline - time.monotonic()))):
                break
    begun, failed = False, False
    try:
        call.begin_request()
        begun = True
        # Slot waiting consumes the same complete-call budget as HTTP/retries.
        timeout = embedding_http_timeout()
        response_deadline = min(call.deadline, time.monotonic() + timeout + 5)
        request_timeout = max(0.001, min(timeout, call.deadline - time.monotonic()))
        with client.stream(
            "POST",
            base.rstrip("/") + "/embeddings",
            json={"model": model, "input": batch, "encoding_format": "float"},
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(request_timeout, connect=min(10.0, request_timeout)),
        ) as response:
            response.raise_for_status()
            payload = bytearray()
            for block in response.iter_bytes():
                if call.stop.is_set() and not call.preserve_inflight:
                    raise _EmbeddingCallCancelled()
                payload.extend(block)
                if len(payload) > 16 * 1024 * 1024:
                    raise ValueError("Embedding 服务响应超过 16 MB。")
                if time.monotonic() > response_deadline:
                    raise httpx.ReadTimeout("Embedding response deadline exceeded")
            return json.loads(payload)
    except httpx.HTTPError:
        failed = True
        raise
    finally:
        if begun:
            call.end_request(failed=failed)
        _EMBEDDING_HTTP_SLOTS.release()


def _embedding_batches(
    client, texts: list[str], base: str, key: str, model: str, call: _EmbeddingCall, on_batch=None
):
    import httpx

    vectors: list[list[float]] = []
    dimension = None
    batch_size = embedding_batch_size()
    start, singleton_failures = 0, 0
    try:
        while start < len(texts):
            call.check()
            batch_size = min(batch_size, embedding_batch_size())
            batch = texts[start : start + batch_size]
            try:
                body = _embedding_response(client, batch, base, key, model, call)
            except httpx.HTTPError as exc:
                status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                unknown = isinstance(
                    exc,
                    (
                        httpx.ReadTimeout,
                        httpx.WriteTimeout,
                        httpx.ReadError,
                        httpx.WriteError,
                        httpx.RemoteProtocolError,
                    ),
                ) or (status == 408 or status is not None and 500 <= status <= 599)
                sent = (
                    True
                    if status is not None or unknown
                    else (
                        False
                        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                        else None
                    )
                )
                evidence = {"http_status": status, "request_outcome_unknown": unknown, "request_sent": sent}
                retryable = isinstance(
                    exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)
                ) or (status in {408, 429} or status is not None and 500 <= status <= 599)
                retry_after = (
                    _retry_after(exc.response.headers.get("Retry-After"))
                    if isinstance(exc, httpx.HTTPStatusError) and retryable
                    else None
                )
                if status == 429 or retry_after:
                    delay = retry_after if retry_after is not None else 2.0
                    _embedding_profile(call.profile_key, cooldown=delay)
                    if hasattr(_EMBEDDING_HTTP_SLOTS, "cooldown"):
                        _EMBEDDING_HTTP_SLOTS.cooldown(delay)
                    raise EmbeddingServiceError(retryable=True, retry_after=delay, **evidence) from None
                if unknown and not embedding_retry_ambiguous_timeouts():
                    # A transport/gateway failure may have happened after the
                    # provider accepted and billed the input. Preserve uncertainty.
                    raise EmbeddingServiceError(retryable=True, retry_after=60, **evidence) from None
                shrink = isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout)) or status in {408, 413, 504}
                with call.lock:
                    can_retry = call.failures < 8
                if shrink and len(batch) > 1 and can_retry and time.monotonic() < call.deadline:
                    batch_size = max(1, len(batch) // 2)
                    _embedding_profile(call.profile_key, size=batch_size)
                    continue
                singleton_failures += 1
                if retryable and singleton_failures < 3 and can_retry and time.monotonic() < call.deadline:
                    time.sleep(
                        max(
                            0,
                            min(1.0, 0.25 * 2 ** (singleton_failures - 1), call.deadline - time.monotonic()),
                        )
                    )
                    continue
                raise EmbeddingServiceError(retryable=retryable, **evidence) from None
            except EmbeddingServiceError:
                raise
            except (ValueError, UnicodeDecodeError):
                raise EmbeddingServiceError(retryable=False) from None
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list) or len(data) != len(batch):
                raise ValueError("Embedding 服务返回数量与输入不一致。")
            if any(not isinstance(entry, dict) or type(entry.get("index")) is not int for entry in data) or {
                entry["index"] for entry in data
            } != set(range(len(batch))):
                raise ValueError("Embedding 服务返回索引无效。")
            completed = []
            for entry in sorted(data, key=lambda item: item["index"]):
                vector = entry.get("embedding")
                if (
                    not isinstance(vector, list)
                    or not vector
                    or len(vector) > 65536
                    or any(
                        isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
                        for n in vector
                    )
                ):
                    raise ValueError("Embedding 服务返回无效向量。")
                if not any(vector):
                    raise ValueError("Embedding 服务返回零向量。")
                dimension = dimension or len(vector)
                if len(vector) != dimension:
                    raise ValueError("Embedding 向量维度不一致。")
                completed.append([float(value) for value in vector])
            # A durable operator callback can retain this successful response
            # even if a later batch or parallel sibling fails. No partial or
            # unvalidated provider payload crosses this checkpoint boundary.
            if on_batch is not None:
                on_batch(list(batch), completed)
            vectors.extend(completed)
            start += len(batch)
            singleton_failures = 0
        return vectors
    except Exception:
        call.stop.set()
        raise


def model_service_config(prefix: str) -> tuple[str, str, str] | None:
    base = os.environ.get(f"{prefix}_BASE_URL", "").strip()
    key = os.environ.get(f"{prefix}_API_KEY", "").strip()
    model = os.environ.get(f"{prefix}_MODEL", "").strip()
    if not any((base, key, model)):
        return None
    if not all((base, key, model)):
        raise ValueError(f"必须同时配置 {prefix}_BASE_URL、{prefix}_API_KEY 和 {prefix}_MODEL。")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{prefix}_BASE_URL 无效。")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"远程 {prefix.lower()} 服务必须使用 HTTPS。")
    return base.rstrip("/"), key, model


def embed_texts(texts: list[str], *, on_batch=None) -> list[list[float]] | None:
    configuration = model_service_config("EMBEDDING")
    if configuration is None:
        return None
    base, key, model = configuration
    if not texts:
        return []
    if len(texts) > 10000 or any(not isinstance(text, str) or len(text) > 20000 for text in texts):
        raise ValueError("Embedding 输入超过处理限制。")
    from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

    import httpx

    embedding_http_worker_limit()
    embedding_http_timeout()
    embedding_retry_ambiguous_timeouts()
    concurrency = embedding_request_concurrency()
    batch_size = embedding_batch_size()
    call = _EmbeddingCall(_embedding_profile_key())
    # Already issued siblings may still return billable, valid vectors after a
    # peer fails. A durable callback can retain those within existing deadlines.
    call.preserve_inflight = on_batch is not None
    # HTTPX read timeouts bound inactivity, not a complete trickling response.
    # The existing response/call deadlines remain shared across both lanes.
    with httpx.Client(
        timeout=httpx.Timeout(embedding_http_timeout(), connect=10.0), follow_redirects=False, trust_env=False
    ) as client:
        if concurrency == 1 or len(texts) <= batch_size:
            return _embedding_batches(client, texts, base, key, model, call, on_batch)
        vectors, start, dimension = [], 0, None
        # Only one wave of at most two futures exists. HTTPX clients are thread
        # safe; drain workers before closing the shared client on every exit.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="tea-embedding") as pool, call:
            while start < len(texts):
                call.check()
                size = embedding_batch_size()
                batches = [
                    texts[offset : offset + size]
                    for offset in range(start, min(len(texts), start + 2 * size), size)
                ]
                futures = {
                    pool.submit(_embedding_batches, client, batch, base, key, model, call, on_batch): index
                    for index, batch in enumerate(batches)
                }
                results, errors = {}, []
                for future in as_completed(futures):
                    try:
                        results[futures[future]] = future.result()
                    except Exception as exc:
                        call.stop.set()
                        errors.append(exc)
                        for pending in futures:
                            pending.cancel()
                if errors:
                    error = next(
                        (
                            exc
                            for exc in errors
                            if not isinstance(exc, (_EmbeddingCallCancelled, CancelledError))
                        ),
                        errors[0],
                    )
                    raise error from None
                for index in range(len(batches)):
                    for vector in results[index]:
                        dimension = dimension or len(vector)
                        if len(vector) != dimension:
                            call.stop.set()
                            raise ValueError("Embedding 向量维度不一致。")
                        vectors.append(vector)
                start += sum(len(batch) for batch in batches)
        return vectors


_RERANK_SLOTS = threading.BoundedSemaphore(4)


def rerank_chunks(query: str, chunks: list[dict], limit: int = 5) -> list[dict] | None:
    """Rank eligible candidates by returned indices; never accept provider evidence."""
    configuration = model_service_config("RERANK")
    if configuration is None:
        return None
    if not chunks:
        return []
    texts = [chunk.get("text") for chunk in chunks]
    if (
        not isinstance(query, str)
        or not 1 <= len(query) <= 2000
        or len(chunks) > 60
        or not 1 <= limit <= 20
        or any(not isinstance(text, str) or not text or len(text) > 20000 for text in texts)
        or sum(len(text) for text in texts) > 1_000_000
    ):
        raise ValueError("Rerank 输入超过处理限制。")
    if not _RERANK_SLOTS.acquire(blocking=False):
        raise ValueError("Rerank 服务繁忙。")
    import httpx

    base, key, model = configuration
    count = min(limit, len(chunks))
    try:
        deadline = time.monotonic() + 20
        with httpx.Client(
            timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False, trust_env=False
        ) as client:
            with client.stream(
                "POST",
                base + "/rerank",
                json={"model": model, "query": query, "documents": texts, "top_n": count},
                headers={"Authorization": f"Bearer {key}"},
            ) as response:
                response.raise_for_status()
                payload = bytearray()
                for block in response.iter_bytes():
                    payload.extend(block)
                    if time.monotonic() > deadline or len(payload) > 1024 * 1024:
                        raise ValueError("Rerank 服务响应超过处理限制。")
                body = json.loads(payload)
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list) or len(results) != count:
            raise ValueError("Rerank 服务返回数量与请求不一致。")
        ranked, indices = [], set()
        for result in results:
            index = result.get("index") if isinstance(result, dict) else None
            score = result.get("relevance_score") if isinstance(result, dict) else None
            if (
                type(index) is not int
                or not 0 <= index < len(chunks)
                or index in indices
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise ValueError("Rerank 服务返回索引或分数无效。")
            indices.add(index)
            ranked.append({**chunks[index], "rerank_score": float(score)})
        return sorted(ranked, key=lambda item: item["rerank_score"], reverse=True)
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError("Rerank 服务请求失败，请检查服务配置或稍后重试。") from exc
    finally:
        _RERANK_SLOTS.release()
