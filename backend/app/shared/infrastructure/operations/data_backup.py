"""Self-contained, offline-restorable data snapshots for one SQLite deployment.

No service is initialized here: backup/restore never upgrades schemas, calls a
model, or changes source rows. A snapshot is published only after verification.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from pathlib import Path, PurePosixPath

from app.shared.domain.values import now, uid
from app.shared.infrastructure.persistence.blob_store import _sync_directory
from app.shared.infrastructure.operations.system_corpus_migration import (
    SystemCorpusMigrationError,
    _cas_reference,
)

PROTOCOL = "aitea-data-backup-v1"
SUPPORTED_SCHEMA_VERSION = 23
MAX_MANIFEST_BYTES = 128 * 1024 * 1024
MAX_FILES = 1_000_000
_CHUNK = 1024 * 1024
_CAS = re.compile(r"(?:.*/)?files/blobs/([a-f0-9]{64})/([a-f0-9]{2})/([a-f0-9]{2})/([a-f0-9]{64})\Z")
_STORAGE_TABLES = ("documents", "checklist_pages", "collection_transfers", "collection_transfer_parts")


class DataBackupError(ValueError):
    """The proposed snapshot/restore is incomplete, unsafe, or incompatible."""


def _directory_pair(value, first, second=None):
    """Match full path components; handoff-files is not the files directory."""
    parts = PurePosixPath(value).parts
    if second is None:
        return first in parts[:-1]
    return any(left == first and right == second for left, right in zip(parts, parts[1:]))


def _require(condition, reason):
    if not condition:
        raise DataBackupError(reason)


def _safe_absolute(path, *, exists=True):
    path = Path(os.path.abspath(Path(path).expanduser()))
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            _require(not exists and current == path, "existing_parent_directory_required")
            return path
        _require(not stat.S_ISLNK(mode), "symlink_path_refused")
    if exists:
        _require(path.exists(), "existing_path_required")
    return path


def _relative(value):
    _require(isinstance(value, str) and value and "\\" not in value and "\0" not in value, "invalid_path")
    path = PurePosixPath(value)
    _require(not path.is_absolute() and path.as_posix() == value, "invalid_path")
    _require(all(part not in {".", ".."} for part in path.parts), "path_escape_refused")
    return Path(*path.parts)


def _hash(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        _require(stat.S_ISREG(before.st_mode), "regular_file_required")
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        after = os.fstat(stream.fileno())
        _require((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), "file_changed")
        return digest, after.st_size


def _copy_file(source, target):
    _safe_absolute(source)
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as incoming, target.open("xb") as outgoing:
        os.fchmod(outgoing.fileno(), 0o600)
        before = os.fstat(incoming.fileno())
        _require(stat.S_ISREG(before.st_mode), "regular_file_required")
        digest, total = hashlib.sha256(), 0
        while block := incoming.read(_CHUNK):
            digest.update(block)
            total += len(block)
            outgoing.write(block)
        after = os.fstat(incoming.fileno())
        _require(
            (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
            and total == before.st_size,
            "file_changed",
        )
        outgoing.flush()
        os.fsync(outgoing.fileno())
    return digest.hexdigest(), total


def _readonly(path, *, immutable=False):
    _safe_absolute(path)
    options = "?mode=ro&immutable=1" if immutable else "?mode=ro"
    conn = sqlite3.connect(Path(path).as_uri() + options, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _sqlite_backup(source, target, *, timeout):
    _safe_absolute(source)
    # Match the system-corpus migration's bounded online backup; never copy a
    # live main file and its independently changing WAL with filesystem tools.
    deadline = time.monotonic() + timeout
    with closing(_readonly(source)) as incoming, closing(sqlite3.connect(target)) as outgoing:
        incoming.execute("BEGIN")
        incoming.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()

        def progress(status, remaining, total):
            _require(time.monotonic() < deadline, "sqlite_backup_timeout")

        incoming.backup(outgoing, pages=128, progress=progress, sleep=0.01)
        outgoing.execute("PRAGMA journal_mode=DELETE")
    target.chmod(0o600)
    with target.open("rb") as stream:
        os.fsync(stream.fileno())
    return _hash(target)


def _sqlite_file(path, relative):
    # User originals, including an uploaded SQLite database, retain exact bytes.
    if _directory_pair(relative, "files") or path.suffix.lower() not in {".sqlite3", ".sqlite", ".db"}:
        return False
    with path.open("rb") as stream:
        return stream.read(16) == b"SQLite format 3\0"


def _tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _database_check(path, *, primary=False):
    # Snapshot files have no WAL. Immutable readers cannot create sidecars or
    # change an archive whose exact bytes are bound by an earlier receipt.
    with closing(_readonly(path, immutable=True)) as conn:
        _require(conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "sqlite_integrity_failed")
        _require(conn.execute("PRAGMA foreign_key_check").fetchone() is None, "sqlite_foreign_key_failed")
        tables = _tables(conn)
        version = None
        if "schema_migrations" in tables:
            version = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
        if primary:
            _require({"schema_migrations", "users", "documents"} <= tables, "application_database_required")
            _require(type(version) is int and 1 <= version <= SUPPORTED_SCHEMA_VERSION, "unsupported_schema")
            if version >= 16:
                try:
                    conn.execute("SELECT user_id,can_login,is_admin,revision FROM user_access LIMIT 0")
                except sqlite3.DatabaseError as exc:
                    raise DataBackupError("account_access_schema_incomplete") from exc
            if version >= 19:
                try:
                    conn.execute(
                        "SELECT id,owner_id,queue_kind,item_id,phase,status,reason,available_at,binding,binding_sha256,retry_plan,revision FROM collection_recovery_jobs LIMIT 0"
                    )
                    conn.execute(
                        "SELECT id,recovery_id,event,detail,created_at FROM collection_recovery_events LIMIT 0"
                    )
                except sqlite3.DatabaseError as exc:
                    raise DataBackupError("collection_recovery_schema_incomplete") from exc
            if version >= 20:
                try:
                    conn.execute(
                        "SELECT id,document_id,owner_id,source_hash,profile_hash,part_index,first_page,last_page,part_blob,state,batch_id,private_data,result_blob,progress,attempts,next_at,error_code FROM document_parse_tasks LIMIT 0"
                    )
                except sqlite3.DatabaseError as exc:
                    raise DataBackupError("document_parse_schema_incomplete") from exc
            if version >= 21:
                try:
                    conn.execute("SELECT owner_id,profile,name,remote_id FROM ragflow_datasets LIMIT 0")
                    conn.execute(
                        "SELECT generation,document_id,owner_id,source_hash,source_version,index_revision,state FROM ragflow_documents LIMIT 0"
                    )
                    conn.execute(
                        "SELECT generation,local_id,input_hash,content_hash,remote_id,state FROM ragflow_chunks LIMIT 0"
                    )
                except sqlite3.DatabaseError as exc:
                    raise DataBackupError("ragflow_schema_incomplete") from exc
            if version >= 22:
                try:
                    conn.execute(
                        "SELECT candidate_id,source_record,source_page_url,source_page_sha256,created_at FROM checklist_candidate_source_records LIMIT 0"
                    )
                except sqlite3.DatabaseError as exc:
                    raise DataBackupError("candidate_source_record_schema_incomplete") from exc
            if version >= 23:
                try:
                    conn.execute(
                        "SELECT event_key,source_id,country,source_url,content_hash,previous_content_hash,source_event_at,source_kind,declared_mime_type,mime_type,discovery_matched,discovery_purpose,discovery_parent_url,discovery_parent_hash,discovery_title,source_root,document_id,status,reason,created_at,updated_at FROM official_source_imports LIMIT 0"
                    )
                    conn.execute("SELECT country,event_ns FROM official_source_import_cursor LIMIT 0")
                except sqlite3.DatabaseError as exc:
                    raise DataBackupError("official_source_import_schema_incomplete") from exc
        counts = {
            table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("users", "sessions", "cases", "documents", "legal_relations", "retrieval_vectors")
            if table in tables
        }
    return {"schema_version": version, "counts": counts}


def _path_relative(value, source_root):
    _require(isinstance(value, str) and value, "storage_path_required")
    path = Path(value)
    if path.is_absolute():
        try:
            return _relative(path.relative_to(source_root).as_posix())
        except ValueError as exc:
            raise DataBackupError("external_storage_path_refused") from exc
    return _relative(value)


def _storage_references(conn, source_root, files, *, prefix=Path()):
    """Prove all operational originals before any new restore is published."""
    count = 0
    tables = _tables(conn)
    for table in _STORAGE_TABLES:
        if table not in tables:
            continue
        columns = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        _require("storage_path" in columns, "unknown_storage_schema")
        size = "size" if "size" in columns else "total_size" if table == "collection_transfers" else None
        fields = (
            ["storage_path"]
            + (["content_hash"] if "content_hash" in columns else [])
            + ([size] if size else [])
        )
        owner = "owner_id" if "owner_id" in columns else None
        if owner:
            fields.append(owner)
        elif table == "checklist_pages":
            fields.append(
                "(SELECT owner_id FROM checklist_bindings WHERE id=checklist_pages.binding_id) AS backup_owner"
            )
            owner = "backup_owner"
        elif table == "collection_transfer_parts":
            fields.append(
                "(SELECT owner_id FROM collection_transfers WHERE id=collection_transfer_parts.transfer_id) AS backup_owner"
            )
            owner = "backup_owner"
        for row in conn.execute(f'SELECT {",".join(fields)} FROM "{table}" WHERE storage_path IS NOT NULL'):
            relative = _path_relative(row["storage_path"], source_root)
            if not Path(row["storage_path"]).is_absolute():
                relative = prefix / relative
            relative = relative.as_posix()
            _require(relative in files, "referenced_original_missing")
            actual = files[relative]
            _require(actual["kind"] != "cold_archive_file", "cold_archive_reference_not_operational")
            cas = _CAS.fullmatch(relative)
            if owner and cas:
                _require(cas[1] == hashlib.sha256(row[owner].encode()).hexdigest(), "original_owner_mismatch")
            if "content_hash" in columns:
                _require(row["content_hash"] == actual["sha256"], "original_hash_mismatch")
            if size:
                _require(row[size] == actual["bytes"], "original_size_mismatch")
            count += 1
    return count


def _manifest_cas(value, files, *, prefix=Path(), owner=None):
    if isinstance(value, list):
        for item in value:
            _manifest_cas(item, files, prefix=prefix, owner=owner)
    elif isinstance(value, dict):
        for key, item in value.items():
            # An import marker is historical identity, never an executable path.
            if key == "collection_import":
                continue
            if (
                (key.endswith("_path") or key in {"path", "full_page_PNG", "PNG_CAS_path"})
                and isinstance(item, str)
                and _directory_pair(item, "files", "blobs")
            ):
                relative = (prefix / _relative(item)).as_posix()
                _require(
                    relative in files and files[relative]["sha256"] == Path(relative).name,
                    "manifest_CAS_missing",
                )
                cas = _CAS.fullmatch(relative)
                _require(
                    cas and cas[1] == hashlib.sha256(owner.encode()).hexdigest(), "manifest_owner_mismatch"
                )
            _manifest_cas(item, files, prefix=prefix, owner=owner)


def _primary_references(database, source_root, files, *, prefix=Path()):
    owners = {}
    for relative in files:
        match = _CAS.fullmatch(relative)
        if match and relative.startswith((prefix / "files/blobs").as_posix() + "/"):
            owners.setdefault(match[1], {})[match[4]] = database.parent / Path(relative).relative_to(prefix)
    with closing(_readonly(database, immutable=True)) as conn:
        count = _storage_references(conn, source_root, files, prefix=prefix)
        if "document_parse_tasks" in _tables(conn):
            for task in conn.execute("SELECT owner_id,part_blob,result_blob FROM document_parse_tasks"):
                for value in task[1:]:
                    if value:
                        try:
                            _manifest_cas(json.loads(value), files, prefix=prefix, owner=task[0])
                        except (ValueError, TypeError) as exc:
                            raise DataBackupError("parse_task_artifact_invalid") from exc
        for row in conn.execute("SELECT manifest,owner_id,id,content_hash FROM documents"):
            _require(len(row[0].encode()) <= 40 * 1024 * 1024, "manifest_too_large")
            try:
                value = json.loads(row[0])
                _manifest_cas(value, files, prefix=prefix, owner=row[1])
                # Reuse the existing mapping validator for PNG size/signature,
                # image hashes and registry/proof digest bindings. Its inputs
                # are the copied owner CAS, never old external source paths.
                _cas_reference(
                    value, database.parent, owners.get(hashlib.sha256(row[1].encode()).hexdigest(), {})
                )
                for page in value.get("pages", []):
                    if not isinstance(page.get("ocr"), dict) or "reuse_import" not in page["ocr"]:
                        continue
                    from app.contexts.documents.infrastructure.recovery.pdf_ocr_reuse_import import (
                        _page,
                        validate_imported,
                    )

                    target = dict(conn.execute("SELECT * FROM documents WHERE id=?", (row[2],)).fetchone())
                    donor = conn.execute(
                        "SELECT * FROM documents WHERE id=? AND owner_id=?",
                        (page["ocr"]["reuse"]["source_document_id"], row[1]),
                    ).fetchone()
                    _require(donor is not None, "imported_OCR_donor_missing")
                    _require(len(donor["manifest"].encode()) <= 40 * 1024 * 1024, "manifest_too_large")
                    validate_imported(
                        target,
                        dict(donor),
                        page,
                        _page(json.loads(donor["manifest"]), page["page"]),
                        owners.get(hashlib.sha256(row[1].encode()).hexdigest(), {}),
                        database.parent,
                    )
                if "corpus_import_aliases" in value:
                    from app.shared.infrastructure.operations.system_corpus_merge import (
                        validate_alias_archives,
                    )

                    validate_alias_archives(
                        value,
                        row[1],
                        owners.get(hashlib.sha256(row[1].encode()).hexdigest(), {}),
                        document_id=row[2],
                        content_hash=row[3],
                    )
            except SystemCorpusMigrationError as exc:
                raise DataBackupError(str(exc)) from exc
            except (json.JSONDecodeError, RecursionError) as exc:
                raise DataBackupError("invalid_document_manifest") from exc
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise DataBackupError("invalid_imported_OCR_reuse:" + str(exc)) from exc
        relations = (
            conn.execute("SELECT * FROM legal_relations") if "legal_relations" in _tables(conn) else ()
        )
        for relation in relations:
            try:
                locator = json.loads(relation["locator"])
            except (ValueError, TypeError):
                continue
            if not isinstance(locator, dict) or "collection_relation_import" not in locator:
                continue
            from app.contexts.ingestion.application.collection_import import validate_relation_import

            endpoints = [
                conn.execute(
                    "SELECT * FROM documents WHERE id=? AND owner_id=?", (relation[key], relation["owner_id"])
                ).fetchone()
                for key in ("source_document_id", "target_document_id")
            ]
            _require(all(endpoints), "imported_relation_endpoint_missing")
            try:
                _manifest_cas(locator, files, prefix=prefix, owner=relation["owner_id"])
                paths = owners.get(hashlib.sha256(relation["owner_id"].encode()).hexdigest(), {})
                _cas_reference(locator, database.parent, paths)
                validate_relation_import(locator, *endpoints, paths, database.parent, relation=relation)
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise DataBackupError("invalid_relation_import:" + str(exc)) from exc
        if "collection_recovery_events" in _tables(conn):
            for row in conn.execute(
                "SELECT e.detail,r.owner_id FROM collection_recovery_events e JOIN collection_recovery_jobs r ON r.id=e.recovery_id"
            ):
                _manifest_cas(json.loads(row[0]), files, prefix=prefix, owner=row[1])
    return count


def _write_json(path, value):
    with path.open("xb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode())
        stream.flush()
        os.fsync(stream.fileno())


def _publish(stage, destination):
    _require(not destination.exists(), "new_destination_required")
    for directory, _, _ in os.walk(stage, topdown=False):
        _sync_directory(Path(directory))
    # The final directory is never visible without its verified COMPLETE file.
    # Linux renameat2(RENAME_NOREPLACE) also refuses an empty directory created
    # by another operator between the preflight and publication.
    rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    _require(rename is not None, "atomic_noreplace_unavailable")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(stage), -100, os.fsencode(destination), 1) != 0:
        code = ctypes.get_errno()
        if code in {errno.EEXIST, errno.ENOTEMPTY}:
            raise DataBackupError("new_destination_required")
        raise OSError(code, os.strerror(code))
    _sync_directory(destination.parent)


def create_backup(source_dir, backup_dir, *, timeout=300, configuration_dir=None, project_root=None):
    """Back up every user's rows and files; source remains read-only."""
    source, destination = _safe_absolute(source_dir), _safe_absolute(backup_dir, exists=False)
    _require(source.is_dir() and (source / "tea.sqlite3").is_file(), "source_database_required")
    _require(
        not destination.exists() and not destination.is_relative_to(source), "external_new_backup_required"
    )
    _require(type(timeout) in {int, float} and 0 < timeout <= 3600, "invalid_timeout")
    stage = Path(tempfile.mkdtemp(prefix=".aitea-backup-", dir=destination.parent))
    try:
        payload = stage / "data"
        payload.mkdir(mode=0o700)
        files, directories, excluded, databases, cold_anomalies = [], [], [], [], []
        # Pin the authoritative root DB first; blobs are published before their
        # DB references, so later unreferenced additions are harmless.
        root_digest, root_size = _sqlite_backup(
            source / "tea.sqlite3", payload / "tea.sqlite3", timeout=timeout
        )
        database_info = _database_check(payload / "tea.sqlite3", primary=True)
        online_databases = {"tea.sqlite3"}
        for directory, subdirs, names in os.walk(source, topdown=True, followlinks=False):
            base = Path(directory)
            relative_base = base.relative_to(source)
            _safe_absolute(base)
            for name in sorted(subdirs.copy()):
                path = base / name
                _require(not path.is_symlink(), "symlink_path_refused")
                if name == ".staging" and base.name == "files":
                    subdirs.remove(name)
                    excluded.append(
                        {"path": path.relative_to(source).as_posix(), "reason": "unpublished_blob_staging"}
                    )
                    continue
                relative = path.relative_to(source)
                (payload / relative).mkdir(mode=0o700)
                directories.append(relative.as_posix())
            for name in sorted(names):
                path = base / name
                relative = (relative_base / name).as_posix()
                # MySQL's local socket is process state, never a database file.
                # Keep this exact allowlist; all other symlinks still fail shut.
                if relative == "ragflow/mysql/mysql.sock":
                    excluded.append({"path": relative, "reason": "ragflow_process_socket"})
                    continue
                suffix = next((s for s in ("-wal", "-shm", "-journal") if name.endswith(s)), None)
                original = path.with_name(name.removesuffix(suffix)) if suffix else None
                if original is not None and original.relative_to(source).as_posix() in online_databases:
                    # The committed state is already in a verified SQLite
                    # snapshot. A final connection closing can remove its
                    # sidecars after os.walk, or even after this path check.
                    # Only the final sidecar may be absent; parents, the main
                    # file and any extant sidecar must still be safe.
                    _safe_absolute(original)
                    _require(stat.S_ISREG(original.lstat().st_mode), "regular_file_required")
                    _safe_absolute(path, exists=False)
                    try:
                        mode = path.lstat().st_mode
                    except FileNotFoundError:
                        pass
                    else:
                        _require(not stat.S_ISLNK(mode), "symlink_path_refused")
                        _require(stat.S_ISREG(mode), "regular_file_required")
                    excluded.append(
                        {"path": relative, "reason": "sqlite_online_backup_includes_committed_WAL"}
                    )
                    continue
                _safe_absolute(path)
                _require(stat.S_ISREG(path.lstat().st_mode), "regular_file_required")
                if original is not None:
                    if original.exists() and _sqlite_file(original, original.relative_to(source).as_posix()):
                        excluded.append(
                            {"path": relative, "reason": "sqlite_online_backup_includes_committed_WAL"}
                        )
                        continue
                target = payload / relative
                kind = "file"
                if relative == "tea.sqlite3":
                    digest, size, kind = root_digest, root_size, "sqlite"
                elif _sqlite_file(path, relative):
                    if relative.startswith("campaigns/") and name == "tea.sqlite3":
                        digest, size = _sqlite_backup(path, target, timeout=timeout)
                        kind = "sqlite"
                    else:
                        wal = path.with_name(name + "-wal")
                        _require(not wal.exists() or wal.stat().st_size == 0, "frozen_archive_has_WAL")
                        digest, size = _copy_file(path, target)
                        _require(not wal.exists() or wal.stat().st_size == 0, "frozen_archive_has_WAL")
                        kind = "frozen_sqlite"
                    databases.append(
                        {
                            "path": relative,
                            "policy": kind,
                            "snapshot_sha256": digest,
                            **_database_check(target),
                        }
                    )
                else:
                    digest, size = _copy_file(path, target)
                cas = _CAS.fullmatch(relative)
                if _directory_pair(relative, "files", "blobs"):
                    _require(
                        cas and cas[2] == cas[4][:2] and cas[3] == cas[4][2:4],
                        "CAS_integrity_failed",
                    )
                    if relative.startswith("ingestion-archive/"):
                        # Frozen test evidence can deliberately contain a bad
                        # image under its original address. Preserve the bytes
                        # and the fault; it is never an operational CAS object.
                        kind = "cold_archive_file"
                        if digest != cas[4]:
                            cold_anomalies.append(
                                {"path": relative, "expected_sha256": cas[4], "actual_sha256": digest}
                            )
                    else:
                        _require(digest == cas[4], "CAS_integrity_failed")
                        kind = "blob"
                files.append({"path": relative, "sha256": digest, "bytes": size, "kind": kind})
                if kind == "sqlite":
                    online_databases.add(relative)
                _require(len(files) <= MAX_FILES, "too_many_files")
        if configuration_dir is not None:
            configuration = _safe_absolute(configuration_dir)
            prefix = Path("config/catalogs")
            bundled = source / prefix
            _require(not bundled.exists() or bundled == configuration, "conflicting_configuration_catalogs")
            if configuration != bundled:
                for directory, subdirs, names in os.walk(configuration, followlinks=False):
                    base = _safe_absolute(directory)
                    for name in subdirs:
                        _safe_absolute(base / name)
                    relative = prefix / base.relative_to(configuration)
                    (payload / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
                    for parent in (prefix.parent, relative):
                        if parent.as_posix() not in directories:
                            directories.append(parent.as_posix())
                    for name in sorted(names):
                        original = _safe_absolute(base / name)
                        _require(stat.S_ISREG(original.stat().st_mode), "regular_file_required")
                        target = relative / name
                        digest, size = _copy_file(original, payload / target)
                        files.append(
                            {"path": target.as_posix(), "sha256": digest, "bytes": size, "kind": "file"}
                        )
                        _require(len(files) <= MAX_FILES, "too_many_files")
        files.sort(key=lambda row: row["path"])
        indexed = {row["path"]: row for row in files}
        references = _primary_references(payload / "tea.sqlite3", source, indexed)
        for database in databases:
            if database["policy"] == "sqlite":
                relative = Path(database["path"])
                _database_check(payload / relative, primary=True)
                database["referenced_originals"] = _primary_references(
                    payload / relative, source, indexed, prefix=relative.parent
                )
        report = {
            "protocol": PROTOCOL,
            "created_at": now(),
            "source_data_dir": str(source),
            "database": database_info,
            "archive_databases": databases,
            "directories": sorted(directories),
            "files": files,
            "excluded": excluded,
            "cold_archive_anomalies": cold_anomalies,
            "referenced_originals": references,
            "source_writes": 0,
            "configuration_included": configuration_dir is not None,
            "instance_project_root": str(Path(project_root).resolve()) if project_root is not None else None,
        }
        _write_json(stage / "manifest.json", report)
        _require((stage / "manifest.json").stat().st_size <= MAX_MANIFEST_BYTES, "backup_manifest_too_large")
        digest = _hash(stage / "manifest.json")[0]
        _write_json(stage / "COMPLETE", {"protocol": PROTOCOL, "manifest_sha256": digest})
        verified = verify_backup(stage)
        _publish(stage, destination)
        return verified | {"status": "complete", "backup_dir": str(destination)}
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise


def verify_backup(backup_dir):
    """Verify every payload file, DB, reference, and the completion seal."""
    root = _safe_absolute(backup_dir)
    for name in ("manifest.json", "COMPLETE"):
        _safe_absolute(root / name)
        _require((root / name).stat().st_size <= MAX_MANIFEST_BYTES, "backup_manifest_too_large")
    try:
        report = json.loads((root / "manifest.json").read_bytes())
        seal = json.loads((root / "COMPLETE").read_bytes())
        _require(report["protocol"] == seal["protocol"] == PROTOCOL, "unknown_backup_protocol")
        _require(seal["manifest_sha256"] == _hash(root / "manifest.json")[0], "backup_manifest_hash_mismatch")
        source = Path(report["source_data_dir"])
        _require(source.is_absolute() and str(source) == os.path.abspath(source), "invalid_source_root")
        records = report["files"]
        _require(isinstance(records, list) and 0 < len(records) <= MAX_FILES, "invalid_file_inventory")
        files, expected, cold_anomalies = {}, {"manifest.json", "COMPLETE"}, []
        for row in records:
            relative = _relative(row["path"]).as_posix()
            _require(
                relative not in files and re.fullmatch(r"[a-f0-9]{64}", row["sha256"]),
                "invalid_file_inventory",
            )
            _require(
                type(row["bytes"]) is int
                and row["bytes"] >= 0
                and row["kind"] in {"file", "sqlite", "frozen_sqlite", "blob", "cold_archive_file"},
                "invalid_file_inventory",
            )
            path = _safe_absolute(root / "data" / relative)
            digest, size = _hash(path)
            _require((digest, size) == (row["sha256"], row["bytes"]), "backup_file_hash_mismatch")
            cas = _CAS.fullmatch(relative)
            if _directory_pair(relative, "files", "blobs"):
                _require(
                    cas and cas[2] == cas[4][:2] and cas[3] == cas[4][2:4],
                    "CAS_integrity_failed",
                )
                if relative.startswith("ingestion-archive/") and row["kind"] == "cold_archive_file":
                    if digest != cas[4]:
                        cold_anomalies.append(
                            {"path": relative, "expected_sha256": cas[4], "actual_sha256": digest}
                        )
                else:
                    _require(digest == cas[4] and row["kind"] == "blob", "CAS_integrity_failed")
            else:
                _require(row["kind"] != "cold_archive_file", "invalid_cold_archive_policy")
            if row["kind"] in {"sqlite", "frozen_sqlite"}:
                online = relative == "tea.sqlite3" or (
                    relative.startswith("campaigns/") and Path(relative).name == "tea.sqlite3"
                )
                _require((row["kind"] == "sqlite") == online, "database_policy_mismatch")
                _database_check(path, primary=relative == "tea.sqlite3")
            files[relative] = row
            expected.add("data/" + relative)
        _require(
            "tea.sqlite3" in files and files["tea.sqlite3"]["kind"] == "sqlite", "primary_database_missing"
        )
        _require(
            sorted(cold_anomalies, key=lambda row: row["path"])
            == sorted(report.get("cold_archive_anomalies", []), key=lambda row: row["path"]),
            "cold_archive_anomaly_inventory_mismatch",
        )
        _require(
            _database_check(root / "data/tea.sqlite3", primary=True) == report["database"],
            "database_inventory_mismatch",
        )
        _require(
            isinstance(report["directories"], list) and len(report["directories"]) <= MAX_FILES,
            "invalid_directory_inventory",
        )
        listed_dirs = ["data/" + _relative(item).as_posix() for item in report["directories"]]
        _require(len(set(listed_dirs)) == len(listed_dirs), "invalid_directory_inventory")
        expected_dirs = {"data"} | set(listed_dirs)
        actual, actual_dirs = set(), set()
        for directory, subdirs, names in os.walk(root, followlinks=False):
            for name in subdirs:
                path = _safe_absolute(Path(directory) / name)
                actual_dirs.add(path.relative_to(root).as_posix())
            for name in names:
                path = _safe_absolute(Path(directory) / name)
                actual.add(path.relative_to(root).as_posix())
        _require(actual == expected and actual_dirs == expected_dirs, "unlisted_backup_content")
        count = _primary_references(root / "data/tea.sqlite3", source, files)
        _require(count == report["referenced_originals"], "reference_inventory_mismatch")
        for database in report["archive_databases"]:
            if database["policy"] == "sqlite":
                relative = _relative(database["path"])
                _database_check(root / "data" / relative, primary=True)
                count = _primary_references(root / "data" / relative, source, files, prefix=relative.parent)
                _require(count == database["referenced_originals"], "reference_inventory_mismatch")
    except (KeyError, TypeError, json.JSONDecodeError, RecursionError) as exc:
        raise DataBackupError("invalid_backup_manifest") from exc
    return {
        "status": "verified",
        "protocol": PROTOCOL,
        "files": len(files),
        "bytes": sum(row["bytes"] for row in files.values()),
        "schema_version": report["database"]["schema_version"],
        "manifest_sha256": seal["manifest_sha256"],
        "excluded": report["excluded"],
        "cold_archive_anomalies": cold_anomalies,
    }


def _restore_database(path, source, target):
    """Rewrite actual file handles, not immutable JSON proofs or vector blobs."""
    paths, claims = 0, {}
    with closing(sqlite3.connect(path)) as conn:
        tables = _tables(conn)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        for table in _STORAGE_TABLES:
            if table not in tables:
                continue
            for rowid, old in conn.execute(
                f'SELECT rowid,storage_path FROM "{table}" WHERE storage_path IS NOT NULL'
            ):
                value = Path(old)
                if value.is_absolute() and value.is_relative_to(source):
                    relative = _relative(value.relative_to(source).as_posix())
                    conn.execute(
                        f'UPDATE "{table}" SET storage_path=? WHERE rowid=?', (str(target / relative), rowid)
                    )
                    paths += 1
        for table, active in (("processing_jobs", "processing"), ("collection_items", "fetching")):
            if table not in tables:
                continue
            # Preserve durable phase, attempt budgets, failed holds and paid OCR
            # checkpoints. Only the cloned process's transient ownership expires.
            count = conn.execute(
                f'SELECT COUNT(*) FROM "{table}" WHERE status=? OR lease_token IS NOT NULL OR lease_until IS NOT NULL',
                (active,),
            ).fetchone()[0]
            conn.execute(
                f"UPDATE \"{table}\" SET status=CASE WHEN status=? THEN 'queued' ELSE status END,lease_token=NULL,lease_until=NULL WHERE status=? OR lease_token IS NOT NULL OR lease_until IS NOT NULL",
                (active, active),
            )
            claims[table] = count
        if "collection_network_leases" in tables:
            claims["collection_network_leases"] = conn.execute(
                "SELECT COUNT(*) FROM collection_network_leases"
            ).fetchone()[0]
            conn.execute("DELETE FROM collection_network_leases")
        conn.commit()
    return {"relocated_paths": paths, "released_claims": claims}


def restore_backup(backup_dir, target_dir):
    """Restore into a new directory only; the backup and existing apps are intact."""
    verified = verify_backup(backup_dir)
    root = _safe_absolute(backup_dir)
    target = _safe_absolute(target_dir, exists=False)
    _require(not target.exists() and not target.is_relative_to(root), "new_restore_directory_required")
    raw_manifest = (root / "manifest.json").read_bytes()
    _require(
        hashlib.sha256(raw_manifest).hexdigest() == verified["manifest_sha256"],
        "backup_changed_during_restore",
    )
    report = json.loads(raw_manifest)
    source = Path(report["source_data_dir"])
    _require(target != source, "restore_source_directory_refused")
    stage = Path(tempfile.mkdtemp(prefix=".aitea-restore-", dir=target.parent))
    try:
        for relative in report["directories"]:
            (stage / _relative(relative)).mkdir(mode=0o700)
        changes = {}
        for row in report["files"]:
            relative = _relative(row["path"])
            digest, size = _copy_file(root / "data" / relative, stage / relative)
            _require((digest, size) == (row["sha256"], row["bytes"]), "backup_changed_during_restore")
            if row["kind"] == "blob":
                (stage / relative).chmod(0o400)
        for row in report["files"]:
            if row["kind"] == "sqlite":
                path = stage / _relative(row["path"])
                changes[row["path"]] = _restore_database(path, source, target)
                _database_check(path, primary=row["path"] == "tea.sqlite3")
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
        receipt_file = "RESTORE.json" if not (stage / "RESTORE.json").exists() else f"RESTORE-{uid()}.json"
        receipt = {
            "protocol": PROTOCOL,
            "restored_at": now(),
            "backup_manifest_sha256": verified["manifest_sha256"],
            "schema_version": verified["schema_version"],
            "changes": changes,
            "schema_upgraded": False,
            "configuration_included": False,
            "receipt_file": receipt_file,
        }
        _write_json(stage / receipt_file, receipt)
        _publish(stage, target)
        return {"status": "restored", "target_dir": str(target), **receipt}
    except BaseException:
        if stage.exists():
            shutil.rmtree(stage)
        raise
