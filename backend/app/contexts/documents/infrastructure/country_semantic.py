"""Bounded semantic discovery for explicitly mounted legal source packs.

Vectors identify source documents; returned legal evidence still comes from the
version-matched original records in country_packs. No approval is inferred.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

from app.contexts.documents.application.retrieval import normalized_vector, stored_vector
from app.contexts.documents.infrastructure.providers.embedding import (
    EmbeddingServiceError,
    embed_texts,
    embedding_priority,
    model_service_config,
)

RECIPE = "document-profile-float32-v2"
_QUERY_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()


def configuration_fingerprint():
    config = model_service_config("EMBEDDING")
    if not config:
        return None
    base, _, model = config
    raw = json.dumps([base, model, RECIPE], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def query_vector(query, state):
    """Share real query embeddings across document and clause indexes."""
    cache_key = (state["fingerprint"], query)
    with _CACHE_LOCK:
        cached = _QUERY_CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] < 300:
            _QUERY_CACHE.move_to_end(cache_key)
            vector = cached[1]
            return (vector, "ready") if len(vector) == state["dimension"] else (None, "dimension_mismatch")
    try:
        with embedding_priority("query"):
            vectors = embed_texts([query])
        if not vectors:
            return None, "not_configured"
        vector = normalized_vector(vectors[0])
        if len(vector) != state["dimension"]:
            return None, "dimension_mismatch"
        with _CACHE_LOCK:
            _QUERY_CACHE[cache_key] = time.monotonic(), vector
            while len(_QUERY_CACHE) > 128:
                _QUERY_CACHE.popitem(last=False)
        return vector, "ready"
    except (EmbeddingServiceError, ValueError):
        return None, "embedding_unavailable"


class CountrySemantic:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "legal-library" / "vectors.sqlite3"

    def status(self, source_fingerprint):
        if not self.path.is_file():
            return {"status": "not_indexed", "countries": {}}
        try:
            configured = configuration_fingerprint()
            with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
                meta = dict(conn.execute("SELECT key,value FROM metadata"))
            state = {
                "status": "ready",
                "countries": {c: int(meta.get(f"vector_count_{c}", 0)) for c in ("CN", "ID", "SG")},
                "dimension": int(meta["dimension"]),
                "model": meta["model"],
                "granularity": "document_profile",
                "fingerprint": meta["model_fingerprint"],
            }
            if not configured:
                state["status"] = "not_configured"
            elif configured != meta["model_fingerprint"]:
                state["status"] = "model_mismatch"
            elif meta["source_fingerprint"] != source_fingerprint:
                state["status"] = "source_index_changed"
            return state
        except ValueError:
            return {"status": "not_configured", "countries": {}}
        except (sqlite3.Error, KeyError):
            return {"status": "invalid_index", "countries": {}}

    def search(self, country, query, source_fingerprint, limit=12):
        state = self.status(source_fingerprint)
        if state["status"] != "ready" or not state["countries"].get(country):
            return [], state["status"]
        query_values, query_status = query_vector(query, state)
        if query_values is None:
            return [], query_status
        import numpy as np

        try:
            with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
                rows = conn.execute(
                    "SELECT document_id,source_hash,dimension,vector FROM document_vectors WHERE country=?",
                    (country,),
                ).fetchall()
            scored = []
            for document_id, source_hash, dimension, vector in rows:
                # A corrupt checkpoint never enters similarity ranking.
                values = stored_vector(vector, dimension, state["dimension"])
                score = float(np.dot(query_values, values))
                scored.append((document_id, source_hash, score))
            scored.sort(key=lambda item: item[2], reverse=True)
            return scored[:limit], "ready"
        except (sqlite3.Error, ValueError):
            return [], "invalid_index"
