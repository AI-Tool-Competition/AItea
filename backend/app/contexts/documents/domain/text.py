"""Deterministic text tokenization and lexical scoring."""

from __future__ import annotations

import math
import re
from collections import Counter


def _terms(text: str) -> list[str]:
    normalized = text.casefold()
    result = re.findall(r"[a-z0-9]+", normalized)
    for segment in re.findall(r"[\u3400-\u9fff]+", normalized):
        result.extend(segment[i : i + 2] for i in range(len(segment) - 1))
        if len(segment) == 1:
            result.append(segment)
    return result


def lexical_search(query: str, chunks: list[dict], limit: int = 5) -> list[dict]:
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError("检索数量应为 1 至 50。")
    if len(query) > 4000:
        raise ValueError("检索问题过长。")
    query_terms = set(_terms(query))
    if not query_terms or not chunks:
        return []
    terms = [Counter(_terms(str(chunk.get("retrieval_text") or chunk.get("text") or ""))) for chunk in chunks]
    average = sum(sum(counts.values()) for counts in terms) / len(terms) or 1
    frequencies = {term: sum(term in counts for counts in terms) for term in query_terms}
    hits = []
    for chunk, counts in zip(chunks, terms, strict=True):
        score = 0.0
        length = sum(counts.values())
        for term in query_terms:
            frequency = counts[term]
            if frequency:
                inverse = math.log(1 + (len(chunks) - frequencies[term] + 0.5) / (frequencies[term] + 0.5))
                score += inverse * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / average))
        if score > 0:
            hits.append({**chunk, "score": round(score, 6), "retrieval_mode": "lexical"})
    return sorted(hits, key=lambda item: item["score"], reverse=True)[:limit]
