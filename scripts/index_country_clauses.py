"""Checkpoint real clause embeddings without changing source records or document vectors."""

import argparse
import fcntl
import json
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.configuration.environment import load_environment
from app.contexts.documents.application.retrieval import normalized_vector
from app.contexts.documents.infrastructure.country_clauses import (
    CLAUSE_RECIPE,
    COUNTRIES,
    ClauseSemantic,
    profile,
    profile_hash,
    windows,
)
from app.contexts.documents.infrastructure.country_semantic import configuration_fingerprint
from app.contexts.documents.infrastructure.providers.embedding import (
    EmbeddingServiceError,
    embed_texts,
    model_service_config,
)


def put(conn, values):
    conn.executemany(
        "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        [(key, str(value)) for key, value in values.items()],
    )


def source_identity(path):
    with sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True) as conn:
        return dict(conn.execute("SELECT key,value FROM metadata"))["fingerprint"]


def prepare(conn, source, fingerprint, model):
    source_fingerprint = source_identity(source)
    conn.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    meta = dict(conn.execute("SELECT key,value FROM metadata"))
    identity = {
        "source_fingerprint": source_fingerprint,
        "model_fingerprint": fingerprint,
        "recipe": CLAUSE_RECIPE,
    }
    if any(meta.get(key, value) != value for key, value in identity.items()):
        raise ValueError("来源、模型或切片规则已变化；请保留原索引并选择独立目标目录。")
    if meta.get("prepared") == "1":
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS profiles(
        profile_hash TEXT PRIMARY KEY,record_id TEXT NOT NULL,text_kind TEXT NOT NULL,
        start INTEGER NOT NULL,end INTEGER NOT NULL,dimension INTEGER,vector BLOB,
        CHECK(vector IS NULL OR (dimension BETWEEN 1 AND 65536 AND length(vector)=dimension*4)))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS units(
        country TEXT NOT NULL,record_id TEXT NOT NULL,source_hash TEXT NOT NULL,text_kind TEXT NOT NULL,
        start INTEGER NOT NULL,end INTEGER NOT NULL,profile_hash TEXT NOT NULL REFERENCES profiles(profile_hash),
        PRIMARY KEY(country,record_id,text_kind,start))""")
    conn.execute("CREATE INDEX IF NOT EXISTS units_profile ON units(profile_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS profiles_pending ON profiles(profile_hash) WHERE vector IS NULL")
    counts = dict.fromkeys(COUNTRIES, 0)
    with conn, sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True) as raw:
        cursor = raw.execute("""SELECT id,country,source_hash,title,article,text,translation FROM records
            WHERE status!='source_location_unverified' AND source_type NOT IN ('heading','navigation','link')
            ORDER BY country,rowid""")
        for record_id, country, source_hash, title, article, original, translation in cursor:
            for kind, text in (("text", original), ("translation", translation)):
                if len(text.strip()) < 30:
                    continue
                for start, end in windows(text):
                    digest = profile_hash(profile(title, article, text[start:end]))
                    conn.execute(
                        "INSERT OR IGNORE INTO profiles VALUES(?,?,?,?,?,NULL,NULL)",
                        (digest, record_id, kind, start, end),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO units VALUES(?,?,?,?,?,?,?)",
                        (country, record_id, source_hash, kind, start, end, digest),
                    )
                    counts[country] += 1
        put(
            conn,
            identity
            | {"model": model, "prepared": 1}
            | {f"planned_{c}": n for c, n in counts.items()}
            | {f"done_{c}": 0 for c in COUNTRIES},
        )


def build(data_dir, *, prepare_only=False, max_batches=None):
    index = ClauseSemantic(data_dir)
    source = index.path.with_name("index.sqlite3")
    fingerprint = configuration_fingerprint()
    if not fingerprint:
        raise ValueError("请配置现有向量服务。")
    _, _, model = model_service_config("EMBEDDING")
    index.path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(index.path) + ".lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sqlite3.connect(index.path.resolve().as_uri(), uri=True, check_same_thread=False) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA wal_autocheckpoint=256")
            conn.execute("PRAGMA foreign_keys=ON")
            prepare(conn, source, fingerprint, model)
            pending = conn.execute("SELECT count(*) FROM profiles WHERE vector IS NULL").fetchone()[0]
            estimate = pending * (2560 * 4 + 1200)
            if pending and shutil.disk_usage(index.path.parent).free < estimate + 768 * 1024**2:
                raise ValueError("磁盘空间不足以保存剩余向量及安全余量，已保留检查点。")
            print(
                json.dumps(
                    {
                        "event": "planned",
                        "pending_profiles": pending,
                        "state": index.status(source_identity(source)),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            if prepare_only:
                return index.status(source_identity(source))
            conn.execute("ATTACH DATABASE ? AS source", (f"{source.resolve().as_uri()}?mode=ro",))
            guard = threading.Lock()
            started, batches = time.monotonic(), 0

            def checkpoint(texts, vectors):
                nonlocal batches
                with guard, conn:
                    dimension = dict(conn.execute("SELECT key,value FROM metadata")).get("dimension")
                    hashes = []
                    for text, vector in zip(texts, vectors, strict=True):
                        values = normalized_vector(vector)
                        if dimension is not None and int(dimension) != len(values):
                            raise ValueError("向量维度变化，已停止保存。")
                        dimension = len(values)
                        digest = profile_hash(text)
                        changed = conn.execute(
                            "UPDATE profiles SET dimension=?,vector=? WHERE profile_hash=? AND vector IS NULL",
                            (dimension, values.tobytes(), digest),
                        ).rowcount
                        if changed:
                            hashes.append(digest)
                    if hashes:
                        params = ",".join("?" for _ in hashes)
                        for country, count in conn.execute(
                            f"SELECT country,count(*) FROM units WHERE profile_hash IN ({params}) GROUP BY country",
                            hashes,
                        ):
                            conn.execute(
                                "UPDATE metadata SET value=CAST(value AS INTEGER)+? WHERE key=?",
                                (count, f"done_{country}"),
                            )
                        put(conn, {"dimension": dimension})
                batches += 1
                if batches % 8 == 0:
                    print(
                        json.dumps(
                            {
                                "event": "progress",
                                "batches": batches,
                                "seconds": round(time.monotonic() - started, 1),
                                "state": index.status(source_identity(source)),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

            while True:
                if shutil.disk_usage(index.path.parent).free < 256 * 1024**2:
                    raise ValueError("磁盘余量不足，已停止请求并保留检查点。")
                if (
                    source_identity(source)
                    != dict(conn.execute("SELECT key,value FROM metadata"))["source_fingerprint"]
                ):
                    raise ValueError("来源索引在运行中变化，已保留检查点。")
                rows = conn.execute("""SELECT p.profile_hash,r.title,r.article,
                    CASE p.text_kind WHEN 'text' THEN substr(r.text,p.start+1,p.end-p.start)
                    ELSE substr(r.translation,p.start+1,p.end-p.start) END
                    FROM profiles p JOIN source.records r ON r.id=p.record_id
                    WHERE p.vector IS NULL ORDER BY p.profile_hash LIMIT 128""").fetchall()
                if not rows or max_batches is not None and batches >= max_batches:
                    break
                texts = [profile(title, article, text) for _, title, article, text in rows]
                if any(profile_hash(text) != row[0] for text, row in zip(texts, rows, strict=True)):
                    raise ValueError("正文与切片检查点不匹配，已停止。")
                if embed_texts(texts, on_batch=checkpoint) is None:
                    raise ValueError("向量服务未配置。")
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return index.status(source_identity(source))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--max-batches", type=int)
    args = parser.parse_args()
    load_environment()
    try:
        result = build(args.data_dir, prepare_only=args.prepare_only, max_batches=args.max_batches)
        print(json.dumps({"event": "finished", "state": result}, ensure_ascii=False), flush=True)
    except Exception as error:
        details = {}
        if isinstance(error, EmbeddingServiceError):
            details = {
                key: getattr(error, key)
                for key in (
                    "http_status",
                    "retryable",
                    "retry_after",
                    "request_sent",
                    "request_outcome_unknown",
                )
            }
        print(
            json.dumps(
                {
                    "event": "stopped",
                    "error_type": type(error).__name__,
                    "checkpoints_preserved": True,
                    **details,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        raise SystemExit(1) from None
