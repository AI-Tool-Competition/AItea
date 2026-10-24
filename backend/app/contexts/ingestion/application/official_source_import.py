"""Import newly collected official source versions into the bounded parse queue."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import mimetypes
import os
import re
import stat
import threading
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID, ensure_principal
from app.configuration.paths import InstancePaths
from app.shared.domain.errors import AppError
from app.shared.domain.values import now

logger = logging.getLogger(__name__)
_COUNTRIES = ("ID", "SG")
_BATCH_SIZE = 4
_INTERVAL_SECONDS = 30
_MAX_EVENT_BYTES = 64 * 1024
_MAX_SOURCE_POOL_BYTES = 1024 * 1024
_MAX_ORIGINAL_BYTES = 8 * 1024 * 1024
_SUPPORTED_MIME = {
    "text/html",
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _safe_file(root: Path, relative: str, expected_hash: str) -> bytes:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError("invalid_original_path")
    root = root.resolve(strict=True)
    candidate = root / relative
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        info = candidate.lstat()
    except (OSError, ValueError):
        raise ValueError("original_missing_or_outside_source_store") from None
    if (
        not stat.S_ISREG(info.st_mode)
        or candidate.is_symlink()
        or info.st_size < 1
        or info.st_size > _MAX_ORIGINAL_BYTES
    ):
        raise ValueError("original_type_or_size_invalid")
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("original_changed_during_validation")
        chunks = []
        size = 0
        while chunk := os.read(descriptor, 64 * 1024):
            size += len(chunk)
            if size > _MAX_ORIGINAL_BYTES:
                raise ValueError("original_exceeds_size_limit")
            digest.update(chunk)
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    if digest.hexdigest() != expected_hash or size != opened.st_size or size != info.st_size:
        raise ValueError("original_hash_mismatch")
    return b"".join(chunks)


def _filename(url: str, mime_type: str) -> str:
    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix not in {".html", ".htm", ".pdf", ".doc", ".docx"}:
        suffix = mimetypes.guess_extension(mime_type) or ".bin"
        if suffix == ".htm":
            suffix = ".html"
    host = (urlsplit(url).hostname or "official-source").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", host).strip("-")[:48] or "official-source"
    return f"{slug}-{hashlib.sha256(url.encode()).hexdigest()[:16]}{suffix}"


def _content_mime(content: bytes, declared: str, url: str) -> str:
    """Prefer original-byte signatures when official servers mislabel a file."""
    header = content[:8192]
    suffix = Path(urlsplit(url).path).suffix.lower()
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"PK\x03\x04") and (
        suffix == ".docx" or declared.endswith("wordprocessingml.document")
    ):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") and suffix == ".doc":
        return "application/msword"
    if re.search(rb"<(?:!doctype\s+html|html\b|head\b|body\b)", header, re.I):
        return "text/html"
    return declared


class OfficialSourceImporter:
    """Turn captured source versions into durable, versioned document parse jobs."""

    def __init__(self, service, *, config_path: Path | None = None, interval: float = _INTERVAL_SECONDS):
        self.service = service
        self.root = service.db.data_dir.resolve()
        self.config_path = config_path or (InstancePaths.resolve().source_pool)
        self.interval = interval
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.health = service.collection.worker_health["official_source_import"]
        self._migrate()
        self.pool: dict = {}
        self.sources: dict[str, dict] = {}
        self._refresh_sources()

    def _refresh_sources(self) -> None:
        """Reload the validated active source set so newly approved receipts can resume."""
        try:
            if (
                self.config_path.is_symlink()
                or not self.config_path.is_file()
                or self.config_path.stat().st_size > _MAX_SOURCE_POOL_BYTES
            ):
                raise ValueError("invalid bounded source registry file")
            pool = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise RuntimeError("official source pool is unavailable") from error
        if not isinstance(pool, dict) or not isinstance(pool.get("sources"), list):
            raise RuntimeError("official source pool is invalid")
        if pool.get("protocol") == "aitea-official-source-pool-v2":
            from app.contexts.ingestion.application.source_catalog import active_sources
            from app.shared.infrastructure.http.download import _validated_url

            rows = [
                *active_sources(pool, "ID", _validated_url),
                *active_sources(pool, "SG", _validated_url),
            ]
        else:
            rows = [
                item
                for item in pool["sources"]
                if isinstance(item, dict)
                and item.get("country") in _COUNTRIES
                and item.get("status", "active") == "active"
            ]
        sources = {}
        for item in rows:
            identifier = item.get("id")
            if not isinstance(identifier, str) or not identifier or identifier in sources:
                raise RuntimeError("official source identifiers are invalid")
            sources[identifier] = item
        self.pool = pool
        self.sources = sources

    def _migrate(self) -> None:
        with self.service.db.transaction() as conn:
            if not conn.execute("SELECT 1 FROM schema_migrations WHERE version=23").fetchone():
                conn.execute(
                    """CREATE TABLE official_source_imports(
                        event_key TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL,
                        country TEXT NOT NULL CHECK(country IN ('ID','SG')),
                        source_url TEXT NOT NULL,
                        content_hash TEXT NOT NULL,
                        previous_content_hash TEXT,
                        source_event_at REAL,
                        source_kind TEXT,
                        declared_mime_type TEXT,
                        mime_type TEXT,
                        discovery_matched INTEGER CHECK(discovery_matched IS NULL OR discovery_matched IN (0,1)),
                        discovery_purpose TEXT,
                        discovery_parent_url TEXT,
                        discovery_parent_hash TEXT,
                        discovery_title TEXT,
                        source_root INTEGER CHECK(source_root IS NULL OR source_root IN (0,1)),
                        document_id TEXT REFERENCES documents(id),
                        status TEXT NOT NULL CHECK(status IN ('stored','skipped','blocked')),
                        reason TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )"""
                )
                conn.execute(
                    "CREATE INDEX official_source_imports_doc ON official_source_imports(document_id,status)"
                )
                conn.execute(
                    "CREATE TABLE official_source_import_cursor(country TEXT PRIMARY KEY CHECK(country IN ('ID','SG')), event_ns INTEGER NOT NULL)"
                )
                conn.executemany(
                    "INSERT INTO official_source_import_cursor(country,event_ns) VALUES(?,?)",
                    [(country, 0) for country in _COUNTRIES],
                )
                conn.execute("INSERT INTO schema_migrations VALUES(?,?)", (23, now()))
            conn.execute(
                "SELECT event_key,source_id,country,source_url,content_hash,previous_content_hash,source_event_at,source_kind,declared_mime_type,mime_type,discovery_matched,discovery_purpose,discovery_parent_url,discovery_parent_hash,discovery_title,source_root,document_id,status,reason FROM official_source_imports LIMIT 0"
            )
            conn.execute("SELECT country,event_ns FROM official_source_import_cursor LIMIT 0")

    def start(self):
        if self.thread is None:
            self.health["status"] = "running"
            self.thread = threading.Thread(target=self._run, name="tea-official-source-import", daemon=True)
            self.thread.start()
        return self

    def stop(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=max(1, self.interval + 1))
            self.thread = None
        self.health["status"] = "stopped"

    def _run(self):
        while not self.stop_event.is_set():
            try:
                counts = self.run_once()
                self.health["last_cycle_at"] = now()
                self.health["last_cycle_counts"] = counts
                self.health["last_error_at"] = None
            except Exception:
                self.health["last_error_at"] = now()
                logger.exception("official_source_import_cycle_failed")
            self.stop_event.wait(self.interval)

    def _event_files(self, country: str):
        directory = self.root / "official-updates" / country / "events"
        if not directory.is_dir() or directory.is_symlink():
            return
        for day in sorted(directory.iterdir()):
            if day.is_dir() and not day.is_symlink():
                for path in sorted(day.glob("*.json")):
                    if path.is_file() and not path.is_symlink():
                        yield path

    @staticmethod
    def _event_timestamp(path: Path) -> int:
        try:
            return int(path.name.split("-", 1)[0])
        except ValueError:
            return 0

    def _advance_cursor(self, country: str, event_ns: int) -> None:
        with self.service.db.transaction() as conn:
            conn.execute(
                "UPDATE official_source_import_cursor SET event_ns=MAX(event_ns,?) WHERE country=?",
                (event_ns, country),
            )

    def _eligible(
        self, country: str, source: dict, event: dict, state: dict, *, verified_mime_type: str | None = None
    ) -> tuple[bool, str | None]:
        if event.get("kind") != "content_version":
            return False, "not_content_version"
        try:
            state_updated = float(state.get("updated_at", 0) or 0)
            event_at = float(event.get("at", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            return False, "cache_not_committed"
        if not math.isfinite(state_updated) or not math.isfinite(event_at) or state_updated < event_at:
            return False, "cache_not_committed"
        url = event.get("url")
        if not isinstance(url, str):
            return False, "source_url_missing"
        try:
            parsed = urlsplit(url)
        except ValueError:
            return False, "source_url_invalid"
        try:
            has_unapproved_port = parsed.port not in (None, 443)
        except ValueError:
            has_unapproved_port = True
        if (
            parsed.scheme != "https"
            or parsed.hostname not in source.get("allowed_hosts", [])
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or has_unapproved_port
        ):
            return False, "url_outside_source_scope"
        if source.get("country") != country or source.get("kind") not in {
            "catalogue",
            "guidance",
            "legislation",
        }:
            return False, "source_configuration_mismatch"
        if event.get("source_kind") and event["source_kind"] != source["kind"]:
            return False, "source_configuration_mismatch"
        current = state.get("sources", {}).get(source["id"], {})
        asset = current.get("cache", {}).get(url, {})
        if source["kind"] == "guidance" and event.get("source_root") is True:
            return True, None
        discovery = asset.get("discovery", {})
        purpose = event.get("discovery_purpose") or discovery.get("purpose")
        matched = event.get("discovery_matched", discovery.get("matched"))
        if matched is True and purpose == "document":
            return True, None
        # Older receipts lack a stored discovery purpose. Recognize only direct,
        # typed document URLs; ambiguous HTML catalogue/detail pages stay raw.
        suffix = Path(parsed.path).suffix.lower()
        if suffix in {".pdf", ".doc", ".docx"}:
            return True, None
        if purpose == "detail" and verified_mime_type in _SUPPORTED_MIME - {"text/html"}:
            return True, None
        return False, "catalogue_page_not_document"

    def _record(
        self,
        event_key,
        source_id,
        country,
        url,
        content_hash,
        *,
        status,
        reason=None,
        document_id=None,
        event=None,
    ):
        event = event or {}
        evidence = self._evidence(event)
        timestamp = now()
        with self.service.db.transaction() as conn:
            conn.execute(
                """INSERT INTO official_source_imports(
                    event_key,source_id,country,source_url,content_hash,previous_content_hash,source_event_at,
                    source_kind,declared_mime_type,mime_type,
                    discovery_matched,discovery_purpose,discovery_parent_url,discovery_parent_hash,discovery_title,
                    source_root,document_id,status,reason,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_key) DO UPDATE SET status=excluded.status,reason=excluded.reason,
                    mime_type=excluded.mime_type,updated_at=excluded.updated_at
                WHERE (official_source_imports.status='skipped'
                    AND official_source_imports.reason='catalogue_page_not_document')
                    OR (official_source_imports.status='blocked'
                    AND official_source_imports.reason='source_not_in_approved_pool')""",
                (
                    event_key,
                    source_id,
                    country,
                    url,
                    content_hash,
                    *evidence,
                    document_id,
                    status,
                    reason,
                    timestamp,
                    timestamp,
                ),
            )

    @staticmethod
    def _evidence(event: dict) -> tuple:
        def text(key: str, limit: int) -> str | None:
            value = event.get(key)
            return value[:limit] if isinstance(value, str) else None

        previous_hash = text("previous_content_hash", 64)
        if previous_hash and not re.fullmatch(r"[0-9a-f]{64}", previous_hash):
            previous_hash = None
        parent_hash = text("discovery_parent_hash", 64)
        if parent_hash and not re.fullmatch(r"[0-9a-f]{64}", parent_hash):
            parent_hash = None
        event_at = event.get("at")
        if isinstance(event_at, bool) or not isinstance(event_at, (int, float)):
            event_at = None
        else:
            try:
                event_at = float(event_at)
                if not math.isfinite(event_at):
                    event_at = None
            except (OverflowError, ValueError):
                event_at = None
        return (
            previous_hash,
            event_at,
            text("source_kind", 32),
            text("declared_mime_type", 128),
            text("mime_type", 128),
            int(event["discovery_matched"]) if isinstance(event.get("discovery_matched"), bool) else None,
            text("discovery_purpose", 32),
            text("discovery_parent_url", 2048),
            parent_hash,
            text("discovery_title", 500),
            int(event["source_root"]) if isinstance(event.get("source_root"), bool) else None,
        )

    def _import(self, country: str, event_path: Path, event: dict, source: dict, state: dict) -> str:
        event_key = event_path.relative_to(self.root).as_posix()
        event = dict(event)
        url, digest = event.get("url"), event.get("content_hash")
        if isinstance(url, str):
            source_state = state.get("sources", {}).get(source["id"], {})
            asset = source_state.get("cache", {}).get(url, {})
            frontier = source_state.get("frontier", {}).get(url, {})
            discovery = asset.get("discovery", {})
            event["source_kind"] = event.get("source_kind") or source.get("kind")
            if not isinstance(event.get("source_root"), bool):
                event["source_root"] = url == source.get("url")
            event["mime_type"] = event.get("mime_type") or asset.get("mime_type")
            event["declared_mime_type"] = event.get("declared_mime_type") or event.get("mime_type")
            if not isinstance(event.get("discovery_matched"), bool):
                event["discovery_matched"] = (
                    frontier.get("purpose") == "document" or discovery.get("matched") is True
                )
            event["discovery_purpose"] = (
                event.get("discovery_purpose") or frontier.get("purpose") or discovery.get("purpose")
            )
            event["discovery_parent_url"] = event.get("discovery_parent_url") or frontier.get("parent_url")
            event["discovery_parent_hash"] = event.get("discovery_parent_hash") or frontier.get("parent_hash")
            event["discovery_title"] = event.get("discovery_title") or frontier.get("title")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            self._record(
                event_key,
                source["id"],
                country,
                str(url or ""),
                "",
                status="blocked",
                reason="invalid_content_hash",
                event=event,
            )
            return "blocked"
        eligible, reason = self._eligible(country, source, event, state)
        if reason == "cache_not_committed":
            return "deferred"
        detail_candidate = event.get("discovery_purpose") == "detail"
        if not eligible and not detail_candidate:
            self._record(
                event_key,
                source["id"],
                country,
                str(url or ""),
                digest,
                status="skipped",
                reason=reason,
                event=event,
            )
            return "skipped"
        if not isinstance(url, str):
            self._record(
                event_key,
                source["id"],
                country,
                "",
                digest,
                status="blocked",
                reason="source_url_missing",
                event=event,
            )
            return "blocked"
        if not self.service.db.data_dir.exists():
            return "deferred"
        with closing(self.service.db.connect()) as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM processing_jobs WHERE status IN ('queued','processing')"
            ).fetchone()[0]
        if pending >= 200:
            return "deferred"
        cache = state.get("sources", {}).get(source["id"], {}).get("cache", {}).get(url, {})
        mime_type = (
            str(event.get("mime_type") or cache.get("mime_type") or mimetypes.guess_type(url)[0] or "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        path_value = event.get("path")
        try:
            original = _safe_file(self.root / "official-updates" / country, path_value, digest)
        except ValueError as error:
            reason = str(error)[:120] if str(error) else type(error).__name__
            self._record(
                event_key, source["id"], country, url, digest, status="blocked", reason=reason, event=event
            )
            return "blocked"
        except OSError:
            return "deferred"
        mime_type = _content_mime(original, mime_type, url)
        event["mime_type"] = mime_type
        if not eligible:
            eligible, reason = self._eligible(country, source, event, state, verified_mime_type=mime_type)
            if not eligible:
                self._record(
                    event_key,
                    source["id"],
                    country,
                    url,
                    digest,
                    status="skipped",
                    reason=reason or "catalogue_page_not_document",
                    event=event,
                )
                return "skipped"
        if mime_type not in _SUPPORTED_MIME:
            self._record(
                event_key,
                source["id"],
                country,
                url,
                digest,
                status="skipped",
                reason="unsupported_mime",
                event=event,
            )
            return "skipped"
        try:
            blob = self.service.archive.blobs.put(SYSTEM_OWNER_ID, iter([original]))
            if blob.digest != digest or blob.size != len(original):
                raise ValueError("blob_copy_mismatch")
            with self.service.db.transaction() as conn:
                ensure_principal(conn)
                doc = self.service.archive.store_original(
                    conn,
                    SYSTEM_OWNER_ID,
                    blob,
                    _filename(url, mime_type),
                    mime_type,
                    url,
                    collection=f"country:{country}",
                    level="primary",
                    tags=["official-source", source["id"]],
                    identity_key=f"official-source:{source['id']}:{url}",
                    kind_hint="guidance" if source["kind"] == "guidance" else "law",
                )
                conn.execute(
                    """INSERT INTO official_source_imports(
                        event_key,source_id,country,source_url,content_hash,previous_content_hash,source_event_at,
                        source_kind,declared_mime_type,mime_type,
                        discovery_matched,discovery_purpose,discovery_parent_url,discovery_parent_hash,discovery_title,
                        source_root,document_id,status,reason,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'stored',NULL,?,?)
                    ON CONFLICT(event_key) DO UPDATE SET document_id=excluded.document_id,status='stored',
                        reason=NULL,mime_type=excluded.mime_type,updated_at=excluded.updated_at
                    WHERE (official_source_imports.status='skipped'
                        AND official_source_imports.reason='catalogue_page_not_document')
                        OR (official_source_imports.status='blocked'
                        AND official_source_imports.reason='source_not_in_approved_pool')""",
                    (
                        event_key,
                        source["id"],
                        country,
                        url,
                        digest,
                        *self._evidence(event),
                        doc["id"],
                        now(),
                        now(),
                    ),
                )
            return "stored"
        except OSError:
            return "deferred"
        except AppError as error:
            if error.status >= 500:
                return "deferred"
            reason = error.code or "document_store_rejected"
            self._record(
                event_key, source["id"], country, url, digest, status="blocked", reason=reason, event=event
            )
            return "blocked"
        except ValueError as error:
            reason = getattr(error, "code", None) or (
                str(error)[:120] if str(error) else type(error).__name__
            )
            self._record(
                event_key, source["id"], country, url, digest, status="blocked", reason=reason, event=event
            )
            return "blocked"

    def run_once(self) -> dict[str, int]:
        self._refresh_sources()
        counts = {"stored": 0, "skipped": 0, "blocked": 0, "deferred": 0}
        for country in _COUNTRIES:
            state_path = self.root / "official-updates" / country / "state.json"
            try:
                if (
                    state_path.is_symlink()
                    or not state_path.is_file()
                    or state_path.stat().st_size > 16 * 1024 * 1024
                ):
                    continue
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            with closing(self.service.db.connect()) as conn:
                cursor = conn.execute(
                    "SELECT event_ns FROM official_source_import_cursor WHERE country=?", (country,)
                ).fetchone()[0]
                retryable = {
                    row["event_key"]
                    for row in conn.execute(
                        """SELECT event_key FROM official_source_imports
                        WHERE country=? AND status='skipped' AND reason='catalogue_page_not_document'
                            AND discovery_purpose='detail'
                            AND mime_type IN ('application/pdf','application/msword',
                                'application/vnd.openxmlformats-officedocument.wordprocessingml.document')""",
                        (country,),
                    )
                }
                retryable.update(
                    row["event_key"]
                    for row in conn.execute(
                        """SELECT event_key,source_id FROM official_source_imports
                        WHERE country=? AND status='blocked' AND reason='source_not_in_approved_pool'""",
                        (country,),
                    )
                    if (source := self.sources.get(row["source_id"])) is not None
                    and source.get("country") == country
                )
            processed = 0
            for path in self._event_files(country):
                event_ns = self._event_timestamp(path)
                event_key = path.relative_to(self.root).as_posix()
                is_recheck = event_key in retryable
                if event_ns <= cursor and not is_recheck:
                    continue
                try:
                    info = path.stat()
                    if info.st_size > _MAX_EVENT_BYTES:
                        self._record(
                            event_key,
                            "unknown",
                            country,
                            "",
                            "",
                            status="blocked",
                            reason="receipt_exceeds_size_limit",
                        )
                        counts["blocked"] += 1
                        self._advance_cursor(country, event_ns)
                        continue
                    event = json.loads(path.read_text(encoding="utf-8"))
                except OSError:
                    counts["deferred"] += 1
                    return counts
                except (UnicodeDecodeError, ValueError):
                    self._record(
                        event_key, "unknown", country, "", "", status="blocked", reason="receipt_invalid"
                    )
                    counts["blocked"] += 1
                    self._advance_cursor(country, event_ns)
                    continue
                if not isinstance(event, dict):
                    self._record(
                        event_key, "unknown", country, "", "", status="blocked", reason="receipt_not_object"
                    )
                    counts["blocked"] += 1
                    self._advance_cursor(country, event_ns)
                    continue
                if event.get("kind") != "content_version":
                    self._advance_cursor(country, event_ns)
                    continue
                if is_recheck:
                    seen = True
                else:
                    with closing(self.service.db.connect()) as conn:
                        seen = conn.execute(
                            "SELECT 1 FROM official_source_imports WHERE event_key=?", (event_key,)
                        ).fetchone()
                if seen and not is_recheck:
                    self._advance_cursor(country, event_ns)
                    continue
                source = self.sources.get(event.get("source_id"))
                if source is None:
                    self._record(
                        event_key,
                        str(event.get("source_id") or ""),
                        country,
                        str(event.get("url") or ""),
                        str(event.get("content_hash") or ""),
                        status="blocked",
                        reason="source_not_in_approved_pool",
                    )
                    counts["blocked"] += 1
                    self._advance_cursor(country, event_ns)
                    continue
                outcome = self._import(country, path, event, source, state)
                if outcome != "deferred":
                    self._advance_cursor(country, event_ns)
                counts[outcome] += 1
                if outcome == "deferred":
                    return counts
                processed += 1
                if processed >= _BATCH_SIZE:
                    return counts
        if any(counts.values()):
            logger.info("official_source_import_cycle_finished", extra=counts)
        return counts
