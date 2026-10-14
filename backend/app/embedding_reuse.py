"""Bounded warm numerical reuse between one owner's identical legal originals.

No global cache, approval, source text or locator is copied. The caller owns the
live target lease and transaction. Cold misses still use the existing provider;
parallel cold calls are not deduplicated by this warm-cache optimization.
"""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path

from .blob_store import _verify_existing
from .db import dump, now
from .ingestion_quality import text_complete
from .retrieval import input_hash, stored_vector

PROTOCOL = "same-owner-original-embedding-reuse-v1"
MAX_DONORS = 8
MAX_BATCH = 128
MAX_MANIFEST_BYTES = 40 * 1024 * 1024
MAX_INPUT_BYTES = 1024 * 1024
MAX_PROVENANCE_BYTES = 4 * 1024 * 1024
MAX_PROVENANCE_RECORDS = 4096


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _document(service, conn, document_id, owner):
    row = conn.execute(
        """SELECT id,owner_id,role,version,family_id,content_hash,source_url,size,
        storage_path,status,CASE WHEN length(CAST(manifest AS BLOB))<=? THEN manifest END manifest
        FROM documents WHERE id=? AND owner_id=?""",
        (MAX_MANIFEST_BYTES, document_id, owner),
    ).fetchone()
    if row is None or row["manifest"] is None:
        raise ValueError("向量处理资料或正文检查点无效")
    metadata = conn.execute(
        """SELECT revision,CASE WHEN length(CAST(payload AS BLOB))<=65536 THEN payload END payload
        FROM document_metadata WHERE document_id=?""",
        (document_id,),
    ).fetchone()
    classes = [
        dict(r)
        for r in conn.execute(
            """SELECT collection,level,revision,CASE WHEN length(CAST(tags AS BLOB))<=65536 THEN tags END tags
            FROM legal_classifications WHERE document_id=? ORDER BY collection LIMIT 65""",
            (document_id,),
        )
    ]
    if (
        len(classes) > 64
        or any(r["tags"] is None for r in classes)
        or (metadata and metadata["payload"] is None)
    ):
        raise ValueError("向量复用资料元数据超过限制")
    country = conn.execute(
        "SELECT country FROM document_countries WHERE document_id=?", (document_id,)
    ).fetchone()
    index = conn.execute(
        "SELECT source_hash,complete,revision FROM retrieval_documents WHERE document_id=?",
        (document_id,),
    ).fetchone()
    dirty = conn.execute("SELECT 1 FROM retrieval_dirty WHERE document_id=?", (document_id,)).fetchone()
    latest = conn.execute(
        """SELECT id FROM documents INDEXED BY documents_family WHERE owner_id=? AND family_id=?
        ORDER BY version DESC,created_at DESC,id DESC LIMIT 1""",
        (owner, row["family_id"]),
    ).fetchone()
    manifest = json.loads(row["manifest"])
    eligible = (
        row["role"] == "primary"
        and row["status"] == "ready"
        and bool(country)
        and any(
            r["collection"] == "country:" + country["country"] and r["level"] == "primary" for r in classes
        )
        and service.archive.can_index(conn, document_id)
        and index is not None
        and index["source_hash"] == row["content_hash"]
        and index["complete"] == 1
        and not dirty
        and latest is not None
        and latest["id"] == document_id
        and manifest.get("source_hash") == row["content_hash"]
        and text_complete(manifest)
    )
    return {
        **{k: row[k] for k in row.keys() if k != "manifest"},
        "manifest_sha256": _hash(row["manifest"].encode()),
        "metadata": dict(metadata) if metadata else None,
        "classifications": classes,
        "country": country["country"] if country else None,
        "index": dict(index) if index else None,
        "dirty": bool(dirty),
        "latest_document_id": latest["id"] if latest else None,
        "eligible": bool(eligible),
    }


def _chunk(conn, document_id, chunk_id):
    row = conn.execute(
        """SELECT rowid,chunk_id,ordinal,text,retrieval_text,locator,content_hash,input_hash
        FROM retrieval_chunks WHERE document_id=? AND chunk_id=?""",
        (document_id, chunk_id),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if (
        not isinstance(result["retrieval_text"], str)
        or len(result["retrieval_text"].encode()) > MAX_INPUT_BYTES
        or len(result["text"].encode()) > MAX_INPUT_BYTES
        or result["input_hash"] != input_hash(result["retrieval_text"])
        or result["content_hash"] != _hash(result["text"].encode())
    ):
        raise ValueError("向量分块输入校验失败")
    return result


def prepare_batch(service, conn, target, fingerprint, batch):
    """Capture narrow candidates in a short transaction; perform no raw-file I/O."""
    if not isinstance(batch, list) or not 1 <= len(batch) <= MAX_BATCH:
        raise ValueError("向量复用批次数量超过限制")
    if not isinstance(fingerprint, str) or not 0 < len(fingerprint) <= 200:
        raise ValueError("向量复用模型标识无效")
    target = dict(target)
    current = _document(service, conn, target["id"], target["owner_id"])
    if any(
        current[k] != target[k]
        for k in ("owner_id", "role", "version", "content_hash", "source_url", "size", "storage_path")
    ):
        raise ValueError("向量处理原件版本已变更")
    chunks = []
    for requested in batch:
        actual = _chunk(conn, target["id"], requested["chunk_id"])
        if actual is None or any(
            actual[k] != requested[k] for k in ("rowid", "input_hash", "retrieval_text")
        ):
            raise ValueError("向量分块内容已变更")
        chunks.append(actual)
    if len({c["rowid"] for c in chunks}) != len(chunks):
        raise ValueError("向量分块重复")
    plan = {
        "target": current,
        "fingerprint": fingerprint,
        "chunks": chunks,
        "hits": {},
        "misses": list(range(len(chunks))),
        "files": [],
        "raw_verified": False,
    }
    if not current["eligible"]:
        return plan
    rows = conn.execute(
        """SELECT id FROM documents INDEXED BY documents_source
        WHERE owner_id=? AND source_url IS ? AND content_hash=? AND version=? AND role='primary'
        AND id!=? ORDER BY rowid LIMIT ?""",
        (
            current["owner_id"],
            current["source_url"],
            current["content_hash"],
            current["version"],
            current["id"],
            MAX_DONORS,
        ),
    ).fetchall()
    dimension = conn.execute(
        "SELECT dimension FROM retrieval_models WHERE fingerprint=?", (fingerprint,)
    ).fetchone()
    donors = []
    for row in rows:
        donor = _document(service, conn, row["id"], current["owner_id"])
        if donor["eligible"] and donor["size"] == current["size"]:
            donors.append(donor)
    for n, chunk in enumerate(chunks):
        for donor in donors:
            dc = _chunk(conn, donor["id"], chunk["chunk_id"])
            if dc is None or any(
                dc[k] != chunk[k] for k in ("input_hash", "retrieval_text", "text", "content_hash")
            ):
                continue
            vector = conn.execute(
                "SELECT fingerprint,input_hash,dimension,vector FROM retrieval_vectors WHERE chunk_rowid=?",
                (dc["rowid"],),
            ).fetchone()
            if (
                vector is None
                or vector["fingerprint"] != fingerprint
                or vector["input_hash"] != chunk["input_hash"]
            ):
                continue
            if dimension is None:
                raise ValueError("缓存向量模型维度不存在")
            values = stored_vector(vector["vector"], vector["dimension"], dimension["dimension"])
            plan["hits"][n] = {
                "donor": donor,
                "donor_chunk": dc,
                "dimension": vector["dimension"],
                "donor_vector_sha256": _hash(vector["vector"]),
                "values": values,
            }
            break
    plan["misses"] = [n for n in range(len(chunks)) if n not in plan["hits"]]
    return plan


def verify_originals(service, plan):
    """Verify shared immutable account CAS once, outside the SQLite write lock."""
    if not plan["target"]["eligible"]:
        return
    identities = [plan["target"], *(h["donor"] for h in plan["hits"].values())]
    checked = set()
    for row in identities:
        digest = row["content_hash"]
        owner_hash = _hash(row["owner_id"].encode())
        expected = service.archive.blobs.blobs_dir / owner_hash / digest[:2] / digest[2:4] / digest
        if (
            not re_hash(digest)
            or type(row["size"]) is not int
            or not 0 < row["size"] <= 128 * 1024 * 1024
            or Path(row["storage_path"]) != expected
        ):
            raise ValueError("向量处理原件不在账号内容存储内")
        if str(expected) in checked:
            continue
        before = _file_state(expected, service.db.files_dir.resolve())
        _verify_existing(expected, digest, row["size"])
        after = _file_state(expected, service.db.files_dir.resolve())
        if before != after:
            raise ValueError("向量处理原件核验时发生变化")
        plan["files"].append({"path": str(expected), "state": after})
        checked.add(str(expected))
    plan["raw_verified"] = True


def re_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _file_state(path, root):
    result = []
    for current in [path, *path.parents]:
        info = current.lstat()
        if current == path:
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("向量处理原件必须是普通文件")
            result.append((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("向量处理原件路径无效")
        else:
            result.append((info.st_dev, info.st_ino))
        if current == root:
            return result
    raise ValueError("向量处理原件路径越界")


def assert_current(service, conn, plan):
    """The caller rechecks its live lease before this transaction-bound fence."""
    target = plan["target"]
    if target["eligible"] and (not plan["raw_verified"] or not plan["files"]):
        raise ValueError("向量处理原件尚未完成核验")
    if (
        service.embedding_fingerprint() != plan["fingerprint"]
        or _document(service, conn, target["id"], target["owner_id"]) != target
    ):
        raise ValueError("向量处理资料、模型或索引已变更")
    for file in plan["files"]:
        if _file_state(Path(file["path"]), service.db.files_dir.resolve()) != file["state"]:
            raise ValueError("向量处理原件已变更")
    for chunk in plan["chunks"]:
        if _chunk(conn, target["id"], chunk["chunk_id"]) != chunk:
            raise ValueError("向量处理分块已变更")
    checked_donors = set()
    for hit in plan["hits"].values():
        donor = hit["donor"]
        if donor["id"] not in checked_donors:
            if _document(service, conn, donor["id"], donor["owner_id"]) != donor:
                raise ValueError("复用来源资料或索引已变更")
            checked_donors.add(donor["id"])
        if _chunk(conn, donor["id"], hit["donor_chunk"]["chunk_id"]) != hit["donor_chunk"]:
            raise ValueError("复用来源分块已变更")
        row = conn.execute(
            "SELECT fingerprint,input_hash,dimension,vector FROM retrieval_vectors WHERE chunk_rowid=?",
            (hit["donor_chunk"]["rowid"],),
        ).fetchone()
        if (
            row is None
            or row["fingerprint"] != plan["fingerprint"]
            or row["input_hash"] != hit["donor_chunk"]["input_hash"]
            or row["dimension"] != hit["dimension"]
            or _hash(row["vector"]) != hit["donor_vector_sha256"]
        ):
            raise ValueError("复用来源向量已变更")
        stored_vector(row["vector"], row["dimension"], hit["dimension"])


def record_commit(conn, plan, manifest):
    """Record actual normalized target bytes after existing store_vectors succeeds."""
    previous = manifest.get("embedding_reuse")
    if previous is not None and (not isinstance(previous, dict) or previous.get("protocol") != PROTOCOL):
        raise ValueError("向量复用检查点无效")
    records = (
        dict(previous.get("records", {}))
        if previous and previous.get("fingerprint") == plan["fingerprint"]
        else {}
    )
    if len(records) > MAX_PROVENANCE_RECORDS:
        raise ValueError("向量复用检查点超过限制")
    target = plan["target"]
    for n, chunk in enumerate(plan["chunks"]):
        if n not in plan["hits"]:
            records.pop(chunk["chunk_id"], None)
            continue
        hit = plan["hits"][n]
        row = conn.execute(
            "SELECT vector FROM retrieval_vectors WHERE chunk_rowid=?", (chunk["rowid"],)
        ).fetchone()
        donor = hit["donor"]
        records[chunk["chunk_id"]] = {
            "source_document_id": donor["id"],
            "source_document_version": donor["version"],
            "source_hash": donor["content_hash"],
            "source_url": donor["source_url"],
            "source_index_revision": donor["index"]["revision"],
            "source_metadata_revision": donor["metadata"]["revision"] if donor["metadata"] else 0,
            "source_classifications_sha256": _hash(dump(donor["classifications"]).encode()),
            "target_document_id": target["id"],
            "target_document_version": target["version"],
            "target_source_hash": target["content_hash"],
            "target_country": target["country"],
            "target_index_revision_before": target["index"]["revision"],
            "target_metadata_revision": target["metadata"]["revision"] if target["metadata"] else 0,
            "target_classifications_sha256": _hash(dump(target["classifications"]).encode()),
            "chunk_id": chunk["chunk_id"],
            "input_hash": chunk["input_hash"],
            "retrieval_text_sha256": _hash(chunk["retrieval_text"].encode()),
            "fingerprint": plan["fingerprint"],
            "dimension": hit["dimension"],
            "donor_vector_sha256": hit["donor_vector_sha256"],
            "target_vector_sha256": _hash(row["vector"]),
            "store_vectors_renormalized": True,
            "bytes_identical_after_normalization": _hash(row["vector"]) == hit["donor_vector_sha256"],
            "target_locators_or_approval_copied": False,
        }
    checkpoint = {
        "protocol": PROTOCOL,
        "fingerprint": plan["fingerprint"],
        "records": records,
        "current_reused_vector_records": len(records),
        "last_batch": {
            "committed_at": now(),
            "input_chunks": len(plan["chunks"]),
            "reused_vectors": len(plan["hits"]),
            "provider_input_chunks": len(plan["misses"]),
            "embed_texts_invocations": int(bool(plan["misses"])),
            "external_wire_calls_measured": False,
        },
    }
    if len(records) > MAX_PROVENANCE_RECORDS or len(dump(checkpoint).encode()) > MAX_PROVENANCE_BYTES:
        raise ValueError("向量复用检查点超过限制")
    return checkpoint
