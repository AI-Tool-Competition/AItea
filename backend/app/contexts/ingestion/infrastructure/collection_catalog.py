"""Publish a readable, shared-library-only view of the existing immutable inventory.

The formal library is one SQLite read snapshot. Raw source states are separate,
timestamped observations, never evidence that a law was parsed or approved.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections import Counter
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.contexts.ingestion.infrastructure.archive_inventory import (
    InventoryError,
    _Originals,
    _private_file,
    export_inventory,
)
from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID
from app.configuration.paths import InstancePaths
from app.shared.infrastructure.persistence.blob_store import _sync_directory

MAX_STATE_BYTES = 32 * 1024**2
MAX_SOURCE_POOL_BYTES = 1024 * 1024
DOCUMENT_COLUMNS = (
    "country",
    "legal_level",
    "document_id",
    "family_id",
    "version",
    "title",
    "filename",
    "parent_id",
    "source_url",
    "original_path",
    "content_hash",
    "size",
    "original_exists",
    "text_complete",
    "pending_pages",
    "failed_pages",
    "review_pages",
    "review_status",
    "index_status",
    "index_backend",
    "indexed_chunk_count",
    "source_hash_matches",
    "index_complete",
    "matching_vector_count",
    "fingerprint_counts",
    "ragflow_generation_state",
    "ragflow_declared_chunk_count",
    "ragflow_matching_chunk_count",
    "ragflow_profile_matches_current",
    "gaps",
)
RELATION_COLUMNS = (
    "collection",
    "relation",
    "source_document_id",
    "source_family_id",
    "source_version",
    "source_country",
    "target_document_id",
    "target_family_id",
    "target_version",
    "target_country",
    "locator",
    "anomalies",
)
SOURCE_COLUMNS = (
    "country",
    "source_id",
    "registry_status",
    "title",
    "url",
    "kind",
    "topics",
    "health",
    "scheduling_status",
    "next_at_utc",
    "last_success_utc",
    "observation_status",
    "http_status",
    "error_type",
    "replacement_eligible",
    "replacement_reason",
    "cached_urls",
    "frontier_urls",
    "scope_excluded_urls",
    "candidate_policy_version",
    "state_updated_utc",
)
RAW_COLUMNS = (
    "country",
    "source_id",
    "requested_url",
    "final_url",
    "content_hash",
    "size",
    "mime_type",
    "original_path",
    "original_exists",
    "anomalies",
    "observed_utc",
    "legal_review",
    "discovery_purpose",
    "scope_excluded",
)


def _cell(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


@contextmanager
def _csv(path, columns):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
        os.fchmod(stream.fileno(), 0o600)
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        yield lambda row: writer.writerow({key: _cell(row.get(key)) for key in columns})
        stream.flush()
        os.fsync(stream.fileno())


def _json_lines(path):
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            yield json.loads(line)


def _utc(value):
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _documents(snapshot, output, summary):
    with _csv(output / "documents.csv", DOCUMENT_COLUMNS) as write:
        for path in sorted(snapshot.glob("*/*/documents.jsonl")):
            for row in _json_lines(path):
                parsing, review, index = row["parsing"], row["review"], row.get("retrieval", {})
                ragflow = index.get("ragflow", {})
                uses_ragflow = index.get("index_backend") == "ragflow"
                active_index_complete = (
                    ragflow.get("complete")
                    if uses_ragflow
                    else None
                    if index.get("index_backend") == "unconfigured"
                    else index.get("complete")
                )
                active_source_matches = (
                    ragflow.get("source_hash_matches") if uses_ragflow else index.get("source_hash_matches")
                )
                if index.get("index_backend") == "unconfigured":
                    active_source_matches = None
                gaps = list(row["anomalies"])
                if row["legal_level"] != "attachment" and parsing["complete"] is not True:
                    gaps.append("text_incomplete_or_unknown")
                if row["legal_level"] == "primary":
                    if active_source_matches is not True or active_index_complete is not True:
                        gaps.append("index_incomplete_or_stale")
                    if uses_ragflow:
                        if ragflow.get("complete") is not True:
                            gaps.append("ragflow_chunks_incomplete_or_unknown")
                    elif not index.get("indexed_chunk_count") or max(
                        (index.get("fingerprint_counts") or {}).values(), default=0
                    ) != index.get("indexed_chunk_count"):
                        gaps.append("vectors_incomplete_or_unknown")
                if review.get("review_status") != "published":
                    gaps.append("legal_review_not_published")
                summary["legal_levels"][row["legal_level"]] += 1
                summary["documents_with_gaps"] += bool(gaps)
                summary["gap_counts"].update(gaps)
                write(
                    {
                        **row,
                        "original_path": row["original"]["relative_path"],
                        "original_exists": row["original"]["exists"],
                        "text_complete": parsing["complete"],
                        **{key: parsing[key] for key in ("pending_pages", "failed_pages", "review_pages")},
                        "review_status": review.get("review_status"),
                        "index_status": (
                            "ready"
                            if active_index_complete is True
                            else "incomplete"
                            if uses_ragflow
                            else "unconfigured"
                            if index.get("index_backend") == "unconfigured"
                            else index.get("status")
                        ),
                        "index_backend": index.get("index_backend", "unknown"),
                        **{
                            key: index.get(key)
                            for key in (
                                "indexed_chunk_count",
                                "matching_vector_count",
                                "fingerprint_counts",
                            )
                        },
                        "source_hash_matches": active_source_matches,
                        "index_complete": active_index_complete,
                        "ragflow_generation_state": ragflow.get("generation_state"),
                        "ragflow_declared_chunk_count": ragflow.get("declared_chunk_count"),
                        "ragflow_matching_chunk_count": ragflow.get("matching_chunk_count"),
                        "ragflow_profile_matches_current": ragflow.get("profile_matches_current"),
                        "gaps": sorted(set(gaps)),
                    }
                )
    with _csv(output / "relations.csv", RELATION_COLUMNS) as write:
        for path in sorted(snapshot.glob("*/*/relations.jsonl")):
            for row in _json_lines(path):
                write(row)


def _state(path):
    # A configured data root is trusted; raw subdirectories must never escape it.
    for parent in (path.parent.parent, path.parent):
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError("raw source directories must be real directories")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("raw source state must be a regular file")
        raw = stream.read(MAX_STATE_BYTES + 1)
    if len(raw) > MAX_STATE_BYTES:
        raise ValueError("raw source state exceeds catalog limit")
    state = json.loads(raw)
    if not isinstance(state, dict) or not isinstance(state.get("sources"), dict):
        raise ValueError("invalid raw source state")
    return state, hashlib.sha256(raw).hexdigest()


def _source_metadata(root, digest, country):
    if (
        country not in {"ID", "SG"}
        or not isinstance(digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", digest)
    ):
        return {}
    official_updates = root / "official-updates"
    directories = (
        official_updates / country / "registry-versions",
        official_updates / "pools",
    )
    for directory in directories:
        try:
            for parent in (official_updates, directory.parent, directory):
                if not stat.S_ISDIR(parent.lstat().st_mode):
                    raise ValueError("source registry history must use real directories")
            fd = os.open(directory / (digest + ".json"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                raw = stream.read(MAX_SOURCE_POOL_BYTES + 1)
            if len(raw) > MAX_SOURCE_POOL_BYTES or hashlib.sha256(raw).hexdigest() != digest:
                continue
            payload = json.loads(raw)
            sources = payload.get("sources") if isinstance(payload, dict) else None
            if not isinstance(sources, list):
                continue
            return {
                row["id"]: row
                for row in sources
                if isinstance(row, dict)
                and isinstance(row.get("id"), str)
                and row.get("country") == country
            }
        except (OSError, ValueError, TypeError, RecursionError):
            continue
    return {}


def _raw(root, output, countries, summary):
    with (
        closing(_Originals(root)) as originals,
        _csv(output / "sources.csv", SOURCE_COLUMNS) as source_row,
        _csv(output / "raw-latest-versions.csv", RAW_COLUMNS) as raw_row,
    ):
        for country in countries:
            written = {"source_rows": 0, "version_rows": 0}
            try:
                state, digest = _state(root / "official-updates" / country / "state.json")
                if state["binding"]["country"] != country:
                    raise ValueError("raw source country does not match directory")
                metadata = _source_metadata(root, state["binding"].get("pool_sha256"), country)
                summary["raw_observations"][country] = {
                    "state_sha256": digest,
                    "state_updated_utc": _utc(state.get("updated_at")),
                    "status": state.get("status"),
                    "sources": len(state["sources"]),
                    "pool_sha256": state["binding"].get("pool_sha256"),
                    "limits": state["binding"].get("limits"),
                    "source_metadata_available": bool(metadata),
                    "snapshot_scope": "latest_cache_only_history_in_requests_and_events",
                }
                for identifier, source in sorted(state["sources"].items()):
                    cache = source.get("cache", {})
                    frontier = source.get("frontier", {})
                    candidates = frontier.values() if isinstance(frontier, dict) else frontier
                    observation = source.get("last_observation", {})
                    source_row(
                        {
                            **{
                                key: metadata.get(identifier, {}).get(key)
                                for key in ("title", "url", "kind", "topics")
                            },
                            **source,
                            "country": country,
                            "source_id": identifier,
                            "registry_status": metadata.get(identifier, {}).get(
                                "status", "unlisted" if metadata else "unknown"
                            ),
                            "next_at_utc": _utc(source.get("next_at")),
                            "last_success_utc": _utc(source.get("last_success_at")),
                            "state_updated_utc": _utc(state.get("updated_at")),
                            "observation_status": observation.get("status"),
                            "http_status": observation.get("http_status"),
                            "error_type": observation.get("error_type"),
                            "cached_urls": len(cache),
                            "frontier_urls": len(frontier),
                            "scope_excluded_urls": sum(
                                bool(item.get("scope_excluded")) for item in candidates
                            ),
                            "candidate_policy_version": source.get("candidate_policy", {}).get("version"),
                        }
                    )
                    written["source_rows"] += 1
                    for url, entry in sorted(cache.items()):
                        path = Path(entry["path"])
                        if path.is_absolute() or ".." in path.parts:
                            original, anomalies = (
                                {"relative_path": None, "exists": None},
                                ["invalid_raw_path"],
                            )
                        else:
                            original, anomalies = originals.inspect(
                                str(Path("official-updates") / country / path), entry.get("size")
                            )
                        raw_row(
                            {
                                **entry,
                                "country": country,
                                "source_id": identifier,
                                "requested_url": entry.get("requested_url", url),
                                "final_url": entry.get("url"),
                                "original_path": original["relative_path"],
                                "original_exists": original["exists"],
                                "anomalies": anomalies,
                                "observed_utc": _utc(entry.get("observed_at")),
                                "legal_review": "unreviewed",
                                "discovery_purpose": entry.get("discovery", {}).get("purpose"),
                                "scope_excluded": bool(frontier.get(url, {}).get("scope_excluded"))
                                if isinstance(frontier, dict)
                                else None,
                            }
                        )
                        written["version_rows"] += 1
                summary["raw_observations"][country].update(written)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                # Formal records remain available even if a raw collector has not been installed.
                summary["raw_observations"][country] = {
                    "status": "partial_invalid" if any(written.values()) else "unavailable_or_invalid",
                    **written,
                }


def _readme(snapshot, summary):
    counts = summary["inventory"]
    queue = counts.get("index_queue", {})
    if queue.get("status") == "available":
        generation_counts = "、".join(
            f"{key} {value}" for key, value in queue.get("remote_generations", {}).items()
        ) or "无"
        chunk_counts = "、".join(f"{key} {value}" for key, value in queue.get("chunks", {}).items()) or "无"
        job_counts = "、".join(
            f"{key} {value}" for key, value in queue.get("processing_jobs", {}).items()
        ) or "无"
        queue_status = (
            f"索引队列观察时间 {queue.get('as_of')}：远端代次 {generation_counts}；分块 {chunk_counts}；"
            f"处理任务 {job_counts}；待只读核账的未知提交 {queue.get('blocked_paid_result_unknown_jobs', 0)}；"
            f"超过 15 分钟未更新的待处理代次 {queue.get('stale_remote_generations_over_15m', 0)}。"
        )
    else:
        queue_status = "索引队列状态未导出；请结合逐份资料的 index_status 和 gaps 核对。"
    return f"""# 法规资料目录

生成于 {summary["generated_at"]}。只包含共享法规总库，不含个人上传。

正式总库：{counts["documents"]} 份资料、{counts["relations"]} 条版本固定关系；
{summary["legal_levels"].get("primary", 0)} 份一级资料、
{summary["legal_levels"].get("secondary", 0)} 份独立二级资料、
{summary["legal_levels"].get("attachment", 0)} 份普通附件。
有待处理状态的资料 {summary["documents_with_gaps"]} 份；这包括审核尚未发布，不表示文件损坏。

| 入口 | 用途 |
| --- | --- |
| [documents.csv](documents.csv) | 按 country、legal_level、family_id、version 筛选原件、文本、OCR、向量与待补状态 |
| [relations.csv](relations.csv) | 查看一二级映射、引用与版本关系，保留两端固定文档及版本 |
| [正式总库完整快照](../{snapshot.name}/summary.json) | 数据库同一只读事务中的资料、关系、正文及来源映射 |
| [sources.csv](sources.csv) | 自动采集来源的活动/归档状态、健康、失败原因、发现队列和下次复查时间（UTC） |
| [raw-latest-versions.csv](raw-latest-versions.csv) | 原始采集仓最新缓存版本；全部待审核，尚不等于正式入库 |
| [summary.json](summary.json) | 统计、缺口和原始状态观察时间 |

原件路径均相对于数据根目录，保留内容哈希；文件名和分类变化不移动原件。
正式快照按账号哈希/国家分组：documents.jsonl 为资料索引，texts.jsonl 为正文与定位，
relations.jsonl 为固定版本关系，ocr-pages.jsonl、ocr-regions.jsonl 为扫描页/区域映射，
source-mapping.jsonl 为来源映射。独立二级资料文本化；普通附件仅保留原件。

text_complete 是正文完整性；index_backend 与 index_complete 描述当前配置的索引。RAGFlow
资料按当前模型配置、正文摘要、版本、索引修订和逐块远端回执核验；ragflow_matching_chunk_count
是可核对的远端分块，不读取或伪称远端向量。matching_vector_count 和 fingerprint_counts
只统计旧本地向量表，不能代表 RAGFlow 向量。没有当前模型配置时索引状态会标为未配置。
索引完成不代表通过法规效力和审核门禁。
{queue_status}该队列仅汇总共享资料的已记录代次，不含文档标识、正文或错误详情；旧代次仍在总数中，逐份资料的当前索引状态以 documents.csv 和正式快照为准。未知提交只进入只读回执核对，不等于可安全重发。
只有一级资料按服务端审核、适用期、版本与索引规则进入 RAG；二级资料不建立向量。
gaps 列保留未完成、未知、过期索引和审核状态，不伪造成功。

原始采集仓独立于正式总库：official-updates/ID 与 SG 各自保留 state.json、
requests/、events/ 和 files/。本目录只列最新缓存；全部历史引用、旧版本、失败请求和换源原因
仍保留在原仓。原始状态是分别观察的快照，观察时间和 SHA 见 summary.json，不冒充数据库原子快照。
403/网络失败不是网站过期；陈旧来源留证、低频复查，相关官方替代来源继续采集。
`sources.csv.registry_status` 区分当前活动来源、已归档来源及未列入当前清单的历史状态；已归档来源的旧失败不会计入活动来源故障。

目录导出只读数据库，不调用模型、不改变审核和队列，也不逐一重算原件哈希。
COMPLETE.json 记录本目录文件摘要；完整备份仍须包含数据库、原件和原始采集仓，目录不能代替备份。
应用资料入口为 /tea 中的法规总库；个人资料继续按账号隔离。
"""


def export_materials(data_dir, *, country=None, ragflow_profile=None):
    if country not in (None, "ID", "SG"):
        raise InventoryError("shared collection catalog country must be ID or SG")
    inventory = export_inventory(
        data_dir,
        country=country,
        owner=SYSTEM_OWNER_ID,
        include_text=True,
        include_source_mapping=True,
        ragflow_profile=ragflow_profile,
    )
    snapshot = inventory["path"]
    root, catalogs = snapshot.parent.parent, snapshot.parent
    staging = Path(tempfile.mkdtemp(prefix=".materials-", dir=catalogs))
    staging.chmod(0o700)
    summary = {
        "format_version": 3,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": "shared_system_library_only",
        "inventory_path": snapshot.name,
        "inventory": inventory["summary"],
        "legal_levels": Counter(),
        "gap_counts": Counter(),
        "documents_with_gaps": 0,
        "raw_observations": {},
    }
    try:
        _documents(snapshot, staging, summary)
        _raw(root, staging, [country] if country else ("ID", "SG"), summary)
        _private_file(staging / "summary.json", summary)
        fd = os.open(staging / "README.md", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(_readme(snapshot, summary))
            stream.flush()
            os.fsync(stream.fileno())
        files = {}
        for path in sorted(staging.iterdir()):
            with path.open("rb") as stream:
                files[path.name] = {
                    "size": path.stat().st_size,
                    "sha256": hashlib.file_digest(stream, "sha256").hexdigest(),
                }
        _private_file(staging / "COMPLETE.json", {"files": files, "inventory_path": snapshot.name})
        _sync_directory(staging)
        destination = catalogs / ("materials-" + snapshot.name)
        staging.rename(destination)
        _sync_directory(catalogs)
        return {"path": destination, "summary": summary}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Export shared legal materials as private, readable catalogs"
    )
    parser.add_argument("--data-dir", default=str(InstancePaths.resolve().data))
    parser.add_argument("--country", choices=("ID", "SG"), default=os.getenv("TEA_INVENTORY_COUNTRY") or None)
    args = parser.parse_args(argv)
    try:
        from app.configuration.environment import load_environment
        from app.contexts.documents.infrastructure.providers.ragflow import profile

        load_environment()
        report = export_materials(args.data_dir, country=args.country, ragflow_profile=profile())
    except (InventoryError, OSError, ValueError) as exc:
        print(f"material catalog failed: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"path": str(report["path"]), "documents": report["summary"]["inventory"]["documents"]},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
