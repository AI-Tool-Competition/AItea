"""Import a delivered China legal pack without executing its code or granting approval.

Usage: uv run --project backend python scripts/import_china_pack.py ZIP --data-dir DATA
The source JSONL, originals and their locations remain in source-packs/CN for audit.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import zipfile


ROOT = "茶渡中国法律资料"
DOCS = "数据/法规与标准目录.jsonl"
UNITS = "数据/条款与正文.jsonl"
SOURCES = "数据/来源与获取记录.jsonl"
CHECKSUMS = "核对记录/文件校验清单.jsonl"
MAX_UNCOMPRESSED = 2 * 1024**3
IMPORT_FORMAT = "2"
COLUMNS = (
    "id",
    "country",
    "document_id",
    "unit_id",
    "title",
    "article",
    "text",
    "translation",
    "source_url",
    "source_path",
    "source_hash",
    "source_type",
    "language",
    "status",
)


def digest_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or "\\" in value or path.is_absolute() or ".." in path.parts or ":" in path.parts[0]:
        raise ValueError(f"Unsafe archive path: {value!r}")
    return path


def json_lines(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def extract_verified(zip_path: Path, destination: Path) -> tuple[str, int]:
    """Validate inventory and bytes before exposing an extracted pack to the importer."""
    package_hash = digest_file(zip_path)
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos if not info.is_dir()]
        if len(names) != len(set(names)) or sum(info.file_size for info in infos) > MAX_UNCOMPRESSED:
            raise ValueError("Duplicate ZIP entries or uncompressed size exceeds limit")
        for info in infos:
            path = relative_path(info.filename)
            if path.parts[0] != ROOT or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError(f"Unexpected archive root or symlink: {info.filename}")
        manifest = [
            json.loads(line)
            for line in archive.read(f"{ROOT}/{CHECKSUMS}").decode("utf-8-sig").splitlines()
            if line.strip()
        ]
        expected = {f"{ROOT}/{relative_path(row['path'])}": row for row in manifest}
        if len(expected) != len(manifest):
            raise ValueError("Duplicate checksum manifest entries")
        if set(names) != set(expected) | {f"{ROOT}/{CHECKSUMS}"}:
            raise ValueError("ZIP file inventory differs from checksum manifest")
        for info in infos:
            if info.is_dir():
                continue
            output = destination.joinpath(*relative_path(info.filename).parts)
            if output.is_symlink() or any(
                parent.is_symlink() for parent in output.parents if parent != destination.parent
            ):
                raise ValueError("Extraction destination contains a symlink")
            output.parent.mkdir(parents=True, exist_ok=True)
            expected_item = expected.get(info.filename)
            h = hashlib.sha256()
            temporary = output.with_name(output.name + ".importing")
            try:
                with archive.open(info) as source, temporary.open("xb") as target:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        h.update(block)
                        target.write(block)
                if expected_item and (
                    h.hexdigest() != expected_item["sha256"] or info.file_size != expected_item["bytes"]
                ):
                    raise ValueError(f"Package file hash mismatch: {info.filename}")
                if output.exists():
                    if digest_file(output) != h.hexdigest():
                        raise ValueError(f"Existing extracted file differs: {info.filename}")
                    temporary.unlink()
                else:
                    temporary.replace(output)
            finally:
                temporary.unlink(missing_ok=True)
    return package_hash, len(names)


def article_number(anchor: str, structure: str) -> str:
    if structure != "article":
        return ""
    match = re.fullmatch(
        r"第\s*([零〇一二三四五六七八九十百千万两\d]+)\s*条(?:之[一二三四五六七八九十]+)?", anchor
    )
    if not match:
        return ""
    value = match[1]
    if value.isdecimal():
        return str(int(value))
    digits = {c: i for i, c in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    total = current = 0
    for char in value:
        if char in digits:
            current = digits[char]
        elif char in "十百千":
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
        else:
            return anchor  # Keep unusual units distinct rather than matching the wrong article.
    # A supplementary article must not masquerade as its base article.
    return str(total + current) if "之" not in anchor else anchor


def cjk_terms(text: str) -> str:
    return " ".join(
        dict.fromkeys(
            run[i : i + 2] for run in re.findall(r"[\u3400-\u9fff]+", text) for i in range(len(run) - 1)
        )
    )


def prepare_records(pack: Path, data_dir: Path) -> tuple[list[tuple], dict]:
    documents = json_lines(pack / DOCS)
    units = json_lines(pack / UNITS)
    sources = json_lines(pack / SOURCES)
    docs = {row["document_id"]: row for row in documents}
    source_map = {row["source_id"]: row for row in sources}
    if len(docs) != len(documents) or len(source_map) != len(sources):
        raise ValueError("Duplicate document or source identity")
    ids = set()
    rows = []
    texts: dict[str, str] = {}
    normalized_texts: dict[str, str] = {}
    verified_originals: dict[str, str] = {}
    location_counts: Counter = Counter()
    unresolved_locations = []
    for unit in units:
        record_id = unit["record_id"]
        if record_id in ids:
            raise ValueError(f"Duplicate record identity: {record_id}")
        ids.add(record_id)
        doc = docs[unit["document_id"]]
        source = source_map[unit["source_id"]]
        original_rel = relative_path(source["original_path"])
        original = pack.joinpath(*original_rel.parts)
        if not original.is_file():
            raise ValueError(f"Original is missing: {original_rel}")
        if str(original_rel) not in verified_originals:
            verified_originals[str(original_rel)] = digest_file(original)
        sha = verified_originals[str(original_rel)]
        if sha != source["original_file_hash"] or sha != unit["source_sha256"]:
            raise ValueError(f"Original hash mismatch: {record_id}")
        if unit["version_id"] != doc["version_id"]:
            raise ValueError(f"Version identity mismatch: {record_id}")
        text = unit["original_text"]
        if not text.strip():
            raise ValueError(f"Empty original text: {record_id}")
        text_rel = str(relative_path(doc["original_text_path"]))
        if text_rel not in texts:
            texts[text_rel] = (pack / text_rel).read_text(encoding="utf-8-sig")
        saved = texts[text_rel]
        start, end = unit.get("char_start"), unit.get("char_end")
        record_status = "local_source_unreviewed"
        if isinstance(start, int) and isinstance(end, int) and saved[start:end] == text:
            location_counts["exact_character_range"] += 1
        elif text in saved:
            location_counts["exact_text_found_outside_declared_range"] += 1
        else:
            if text_rel not in normalized_texts:
                normalized_texts[text_rel] = re.sub(r"\s+", "", saved)
            if re.sub(r"\s+", "", text) in normalized_texts[text_rel]:
                location_counts["whitespace_normalized_match"] += 1
            else:
                location_counts["declared_record_only"] += 1
                unresolved_locations.append(record_id)
                record_status = "source_location_unverified"
        kind = unit.get("source_type") or doc.get("source_type") or "待分类资料"
        source_type = kind
        if kind in {"法律", "行政法规", "部门规章", "地方性法规", "法律修改决定"}:
            source_type += f"；规范类型：{kind}"
        elif kind in {"指导性案例", "普通裁判文书", "案例库入库案例", "典型案例（非指导性案例）"}:
            source_type += "；判决案例参考"
        rows.append(
            (
                "CN:" + record_id,
                "CN",
                unit["document_id"],
                record_id,
                unit.get("official_title") or doc["official_title"],
                article_number(unit.get("article_anchor", ""), unit.get("structure_level", "")),
                text,
                "",
                unit.get("source_url") or source.get("final_url") or source["url"],
                str(original.relative_to(data_dir / "legal-library")),
                sha,
                source_type,
                "zh",
                record_status,
            )
        )
    return rows, {
        "document_versions": len(documents),
        "record_count": len(rows),
        "article_records": sum(bool(row[5]) for row in rows),
        "originals_verified": len(verified_originals),
        "source_attempts": len(sources),
        "failed_sources": sum(row.get("status") == "fetch_or_parse_failed" for row in sources),
        "partial_documents": sum(row.get("status") == "partial_text" for row in documents),
        "location_validation": dict(location_counts),
        "unresolved_location_record_ids": unresolved_locations,
        "unindexed_structured_records": 0,
        "original_files_saved": len(list((pack / "原件").iterdir())),
        "distinct_original_content_hashes": len({path.stem for path in (pack / "原件").iterdir()}),
        "review_status": "draft",
        "effectivity": "unreviewed",
        "production_eligible": False,
    }


def index_records(db: Path, rows: list[tuple], package_hash: str, report: dict) -> dict:
    if not db.is_file():
        raise ValueError("Existing country-pack index is required")
    with sqlite3.connect(db, timeout=1) as conn:
        conn.execute("BEGIN IMMEDIATE")  # Fail before backup if another writer is active.
        conn.rollback()
        metadata = dict(conn.execute("SELECT key,value FROM metadata"))
        prior = conn.execute("SELECT * FROM records WHERE country='CN' ORDER BY id").fetchall()
        if (
            prior == sorted(rows)
            and metadata.get("china_package_sha256") == package_hash
            and metadata.get("china_import_format") == IMPORT_FORMAT
        ):
            return report | {"changed": False, "fingerprint": metadata["fingerprint"], "backup": None}
        backup = (
            db.parent
            / "backups"
            / ("index-before-cn-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3")
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(backup) as copy:
            conn.backup(copy)
        with conn:
            old_ids = conn.execute("SELECT rowid FROM records WHERE country='CN'").fetchall()
            conn.executemany("DELETE FROM record_search WHERE rowid=?", old_ids)
            conn.execute("DELETE FROM records WHERE country='CN'")
            placeholders = ",".join("?" for _ in COLUMNS)
            for row in rows:
                cursor = conn.execute(f"INSERT INTO records VALUES({placeholders})", row)
                conn.execute(
                    "INSERT INTO record_search(rowid,title,text,translation,cjk_terms) VALUES(?,?,?,?,?)",
                    (
                        cursor.lastrowid,
                        row[4],
                        row[6],
                        row[7],
                        cjk_terms(row[4] + "\n" + row[6]),
                    ),
                )
            if "fingerprint_before_CN" not in metadata:
                conn.execute(
                    "INSERT INTO metadata VALUES(?,?)", ("fingerprint_before_CN", metadata["fingerprint"])
                )
            base = metadata.get("fingerprint_before_CN", metadata["fingerprint"])
            fingerprint = hashlib.sha256(
                (base + "\0CN\0" + package_hash + "\0" + IMPORT_FORMAT).encode()
            ).hexdigest()
            updates = {
                "fingerprint": fingerprint,
                "record_count_CN": str(len(rows)),
                "record_count": str(conn.execute("SELECT count(*) FROM records").fetchone()[0]),
                "china_package_sha256": package_hash,
                "china_document_version_count": str(report["document_versions"]),
                "china_review_status": "draft",
                "china_import_format": IMPORT_FORMAT,
            }
            conn.executemany(
                "INSERT INTO metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                updates.items(),
            )
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite integrity check failed")
    return report | {"changed": True, "fingerprint": fingerprint, "backup": str(backup)}


def import_pack(zip_path: Path, data_dir: Path) -> dict:
    data_dir = data_dir.resolve()
    destination = data_dir / "legal-library" / "source-packs" / "CN"
    sha, file_count = extract_verified(zip_path, destination)
    rows, report = prepare_records(destination / ROOT, data_dir)
    result = index_records(data_dir / "legal-library" / "index.sqlite3", rows, sha, report)
    result.update(
        {"package_sha256": sha, "archive_files_verified": file_count, "source_root": str(destination / ROOT)}
    )
    (destination / "import-report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("zip_path", type=Path)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(import_pack(args.zip_path, args.data_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
