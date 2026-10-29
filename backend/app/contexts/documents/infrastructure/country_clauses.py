"""Source-matched clause vectors; legal evidence remains the original record."""

import hashlib
import heapq
import sqlite3
from pathlib import Path

from app.contexts.documents.application.retrieval import stored_vector
from app.contexts.documents.infrastructure.country_semantic import configuration_fingerprint, query_vector

CLAUSE_RECIPE = "clause-window-1800-overlap200-context-float32-v1"
COUNTRIES = ("CN", "ID", "SG")


def windows(text):
    """Bound model inputs while retaining exact character offsets and coverage."""
    start = 0
    while start < len(text):
        end = min(len(text), start + 1800)
        if end < len(text):
            boundary = max(text.rfind("\n", start + 1350, end), text.rfind(" ", start + 1350, end))
            if boundary > start:
                end = boundary + 1
        yield start, end
        if end == len(text):
            break
        start = end - 200


def profile(title, article, text):
    return f"{title[:180]}\n{article[:40]}\n{text}"


def profile_hash(text):
    return hashlib.sha256((CLAUSE_RECIPE + "\0" + text).encode()).hexdigest()


class ClauseSemantic:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "legal-library" / "clause-vectors.sqlite3"

    def status(self, source_fingerprint):
        if not self.path.is_file():
            return {"status": "not_indexed", "countries": {}}
        try:
            with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
                meta = dict(conn.execute("SELECT key,value FROM metadata"))
            configured = configuration_fingerprint()
            state = {
                "status": "ready",
                "granularity": "clause",
                "fingerprint": meta["model_fingerprint"],
                "model": meta["model"],
                "dimension": int(meta.get("dimension", 0)),
                "countries": {
                    c: {
                        "planned": int(meta.get(f"planned_{c}", 0)),
                        "completed": int(meta.get(f"done_{c}", 0)),
                    }
                    for c in COUNTRIES
                },
            }
            if not configured:
                state["status"] = "not_configured"
            elif configured != meta["model_fingerprint"]:
                state["status"] = "model_mismatch"
            elif meta.get("recipe") != CLAUSE_RECIPE:
                state["status"] = "recipe_mismatch"
            elif meta.get("source_fingerprint") != source_fingerprint:
                state["status"] = "source_index_changed"
            elif (
                meta.get("prepared") != "1"
                or not state["dimension"]
                or any(item["completed"] != item["planned"] for item in state["countries"].values())
            ):
                state["status"] = "partial"
            return state
        except (sqlite3.Error, ValueError, KeyError):
            return {"status": "invalid_index", "countries": {}}

    def search(self, country, query, source_fingerprint, limit=64):
        state = self.status(source_fingerprint)
        # Completed checkpoints are usable while other units/countries are
        # still indexing. Fingerprint, recipe and model checks precede partial
        # status; the join below reads only profiles with a committed vector.
        if state["status"] not in {"ready", "partial"} or not state["countries"].get(country, {}).get(
            "completed"
        ):
            return [], state["status"]
        query_values, query_status = query_vector(query, state)
        if query_values is None:
            return [], query_status
        import numpy as np

        try:
            top = []
            with sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True) as conn:
                # Scan bounded batches; never retain a country's entire matrix.
                cursor = conn.execute(
                    """SELECT u.record_id,u.source_hash,u.text_kind,u.start,u.end,p.dimension,p.vector
                    FROM units u JOIN profiles p USING(profile_hash)
                    WHERE u.country=? AND p.vector IS NOT NULL""",
                    (country,),
                )
                serial = 0
                while rows := cursor.fetchmany(256):
                    matrix = np.stack([stored_vector(r[6], r[5], state["dimension"]) for r in rows])
                    scores = np.einsum("ij,j->i", matrix, query_values, optimize=False)
                    for row, score in zip(rows, scores, strict=True):
                        item = (float(score), serial, row[:5])
                        serial += 1
                        if len(top) < limit:
                            heapq.heappush(top, item)
                        elif item > top[0]:
                            heapq.heapreplace(top, item)
            return [
                {
                    "record_id": r[0],
                    "source_hash": r[1],
                    "text_kind": r[2],
                    "start": r[3],
                    "end": r[4],
                    "score": score,
                }
                for score, _, r in sorted(top, reverse=True)
            ], state["status"]
        except (sqlite3.Error, ValueError):
            return [], "invalid_index"
