"""Bounded optional ranking of an already authorized retrieval candidate set."""

import json
import math
import os
import threading
from time import monotonic
from urllib.parse import urlsplit

import httpx

_SLOTS = threading.BoundedSemaphore(2)
_MAX_RESPONSE_BYTES = 1024 * 1024


def configuration():
    """Non-secret cache identity; key rotation alone does not change ranking."""
    base, model, key = (
        os.environ.get(f"RERANK_{field}", "").strip() for field in ("BASE_URL", "MODEL", "API_KEY")
    )
    return {"base": base.rstrip("/"), "model": model, "enabled": bool(base and model and key)}


def rerank_texts(query: str, texts: list[str], *, limit: int | None = None) -> list[dict] | None:
    base, key, model = (
        os.environ.get(f"RERANK_{field}", "").strip() for field in ("BASE_URL", "API_KEY", "MODEL")
    )
    if not any((base, key, model)):
        return None
    if not all((base, key, model)):
        raise ValueError("必须同时配置 RERANK_BASE_URL、RERANK_API_KEY 和 RERANK_MODEL。")
    parsed = urlsplit(base)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("RERANK_BASE_URL 无效。")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("远程排序服务必须使用 HTTPS。")
    if not isinstance(query, str) or not query.strip() or len(query) > 10000:
        raise ValueError("排序查询超过处理限制。")
    if (
        not isinstance(texts, list)
        or len(texts) > 48
        or any(not isinstance(text, str) or len(text) > 20000 for text in texts)
        or sum(len(text) for text in texts) > 240000
    ):
        raise ValueError("排序输入超过处理限制。")
    if not texts:
        return []
    limit = len(texts) if limit is None else limit
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= len(texts):
        raise ValueError("排序数量超过处理限制。")
    if not _SLOTS.acquire(timeout=1):
        raise ValueError("排序服务繁忙，本次保留原检索顺序。")
    try:
        deadline = monotonic() + 15
        with httpx.Client(
            timeout=httpx.Timeout(12, connect=5), follow_redirects=False, trust_env=False
        ) as client:
            with client.stream(
                "POST",
                base.rstrip("/") + "/rerank",
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model, "query": query, "documents": texts, "top_n": limit},
            ) as response:
                response.raise_for_status()
                raw = bytearray()
                for block in response.iter_bytes():
                    if monotonic() > deadline or len(raw) + len(block) > _MAX_RESPONSE_BYTES:
                        raise ValueError("排序服务响应超过处理限制。")
                    raw.extend(block)
        body = json.loads(raw)
        entries = body.get("results") if isinstance(body, dict) else None
        if not isinstance(entries, list) or len(entries) != limit:
            raise ValueError("排序服务返回数量无效。")
        ranks, seen = [], set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("排序服务返回结果无效。")
            index, score = entry.get("index"), entry.get("relevance_score")
            if (
                isinstance(index, bool)
                or not isinstance(index, int)
                or not 0 <= index < len(texts)
                or index in seen
                or isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise ValueError("排序服务返回索引或分数无效。")
            seen.add(index)
            ranks.append({"index": index, "score": float(score)})
        return sorted(ranks, key=lambda item: (-item["score"], item["index"]))
    except (httpx.HTTPError, ValueError, OverflowError) as exc:
        raise ValueError("排序服务暂不可用，本次保留原检索顺序。") from exc
    finally:
        _SLOTS.release()
