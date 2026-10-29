"""Build/resume real document-profile vectors for the mounted country packs.

Run with the backend uv environment. Existing vectors may be reused only when
model, profile hash, source identity and original source hash all match.
"""

import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.configuration.environment import load_environment
from app.contexts.documents.application.retrieval import (
    normalized_vector,
    stored_vector,
)
from app.contexts.documents.infrastructure.country_semantic import (
    configuration_fingerprint,
)
from app.contexts.documents.infrastructure.providers.embedding import (
    embed_texts,
    model_service_config,
)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def build(data_dir, *, reuse_index=None):
    source = Path(data_dir) / "legal-library" / "index.sqlite3"
    target = source.with_name("vectors.sqlite3")
    fingerprint = configuration_fingerprint()
    if not fingerprint:
        raise ValueError("请先配置现有的Embedding服务，不能用词项索引冒充向量索引。")
    _, _, model = model_service_config("EMBEDDING")
    with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True) as raw:
        raw.row_factory = sqlite3.Row
        metadata = dict(raw.execute("SELECT key,value FROM metadata"))
        documents = raw.execute(
            "SELECT country,document_id,source_hash,max(title) title,count(*) units FROM records GROUP BY country,document_id,source_hash"
        ).fetchall()
    identities = {(r["country"], r["document_id"], r["source_hash"]) for r in documents}
    report = {
        "source_documents": len(documents),
        "reused": 0,
        "rejected_reuse": 0,
        "embedded": 0,
    }
    # Pure lookup indexes do not change source text, identity or approval state.
    with sqlite3.connect(source) as conn:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS records_country_document ON records(country,document_id,source_hash)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS records_country_article ON records(country,article)")
    with sqlite3.connect(target) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS document_vectors(
            country TEXT NOT NULL,document_id TEXT NOT NULL,source_hash TEXT NOT NULL,
            profile_hash TEXT NOT NULL,profile_text TEXT NOT NULL,dimension INTEGER NOT NULL,vector BLOB NOT NULL,
            PRIMARY KEY(country,document_id,source_hash),CHECK(length(vector)=dimension*4))"""
        )
        prior = dict(conn.execute("SELECT key,value FROM metadata"))
        if prior.get("model_fingerprint") not in {None, fingerprint}:
            raise ValueError("已有向量模型与当前配置不同；请保留原索引后使用独立目标目录。")
        if reuse_index:
            with sqlite3.connect(f"{Path(reuse_index).resolve().as_uri()}?mode=ro", uri=True) as old:
                reused = old.execute(
                    """SELECT d.jurisdiction,d.source_document_id,d.source_hash,p.profile_hash,p.profile_text,
                    v.fingerprint,v.dimension,v.vector FROM package_documents d
                    JOIN package_document_profiles p USING(document_id)
                    JOIN package_document_vectors v USING(document_id)"""
                )
                for country, doc, sha, ph, profile, fp, dimension, vector in reused:
                    if (country, doc, sha) not in identities or fp != fingerprint or ph != digest(profile):
                        report["rejected_reuse"] += 1
                        continue
                    try:
                        stored_vector(vector, dimension, dimension)
                    except ValueError:
                        report["rejected_reuse"] += 1
                        continue
                    changed = conn.execute(
                        "INSERT OR IGNORE INTO document_vectors VALUES(?,?,?,?,?,?,?)",
                        (country, doc, sha, ph, profile, dimension, vector),
                    ).rowcount
                    report["reused"] += changed
        conn.executemany(
            "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("model_fingerprint", fingerprint),
                ("model", model),
                ("source_fingerprint", metadata["fingerprint"]),
            ],
        )
        conn.commit()
        present = set(conn.execute("SELECT country,document_id,source_hash FROM document_vectors"))
        pending = [
            row
            for row in documents
            if (row["country"], row["document_id"], row["source_hash"]) not in present
        ]
        profiles = []
        with sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True) as raw:
            for row in pending:
                units = raw.execute(
                    "SELECT article,text,translation FROM records WHERE country=? AND document_id=? AND source_hash=? AND length(text||translation)>30 ORDER BY rowid",
                    (row["country"], row["document_id"], row["source_hash"]),
                ).fetchall()
                # Distributed sampling includes later clauses without embedding
                # every unit or treating the first 3000 characters as the law.
                positions = sorted({round(i * (len(units) - 1) / 9) for i in range(10)}) if units else []
                samples = [f"{units[i][0]} {units[i][1][:240]} {units[i][2][:120]}" for i in positions]
                profile = (row["title"] + "\n" + "\n".join(samples))[:5000]
                profiles.append((row, profile))
        for offset in range(0, len(profiles), 4):
            batch = profiles[offset : offset + 4]
            vectors = embed_texts([profile for _, profile in batch])
            if vectors is None or len(vectors) != len(batch):
                raise ValueError("向量服务未返回完整批次；已完成的检查点仍保留。")
            for (row, profile), vector in zip(batch, vectors, strict=True):
                normalized = normalized_vector(vector)
                dimension = len(normalized)
                prior_dim = conn.execute("SELECT value FROM metadata WHERE key='dimension'").fetchone()
                if prior_dim and int(prior_dim[0]) != dimension:
                    raise ValueError("向量维度变化，已停止写入。")
                conn.execute(
                    "INSERT INTO document_vectors VALUES(?,?,?,?,?,?,?)",
                    (
                        row["country"],
                        row["document_id"],
                        row["source_hash"],
                        digest(profile),
                        profile,
                        dimension,
                        normalized.tobytes(),
                    ),
                )
                report["embedded"] += 1
            conn.commit()
        dimensions = {r[0] for r in conn.execute("SELECT DISTINCT dimension FROM document_vectors")}
        if len(dimensions) != 1:
            raise ValueError("索引向量维度不一致。")
        counts = {
            c: conn.execute("SELECT count(*) FROM document_vectors WHERE country=?", (c,)).fetchone()[0]
            for c in ("CN", "ID", "SG")
        }
        conn.executemany(
            "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            [
                ("dimension", str(next(iter(dimensions)))),
                *((f"vector_count_{c}", str(n)) for c, n in counts.items()),
            ],
        )
        report.update(
            {
                "vectors_by_country": counts,
                "dimension": next(iter(dimensions)),
                "model": model,
                "model_fingerprint": fingerprint,
            }
        )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--reuse-index")
    args = parser.parse_args()
    load_environment()
    print(json.dumps(build(args.data_dir, reuse_index=args.reuse_index), ensure_ascii=False))
