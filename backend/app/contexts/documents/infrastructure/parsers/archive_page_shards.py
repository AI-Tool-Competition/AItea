"""Independent, resumable native-page evidence; never a searchable manifest.

An exclusive coordinator lock protects commits. Child workers only extract
disjoint absolute windows; immutable blobs precede atomic root publication.
Legacy page objects and their original serialized checkpoint are preserved.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.contexts.documents.infrastructure.parsers.archive_parsing import (
    _KNOWN_STATUSES,
    _MAX_BYTES,
    _MAX_TEXT,
    _open_original,
    _validate_original_request,
    _verify_hash,
    _verify_identity,
    inspect_archive_pdf_bounded,
    parse_archive_pdf_pages_bounded,
)
from app.contexts.documents.infrastructure.parsers.pdf_limits import ARCHIVE_MAX_PAGES
from app.shared.infrastructure.persistence.blob_store import BlobStore, _sync_directory

PROTOCOL = "archive-native-page-shards-v1"
SEGMENT_PROTOCOL = "archive-native-page-segment-v1"
MAX_SEGMENT_BYTES = 20 * 1024 * 1024
MAX_CHECKPOINT_BYTES = 40 * 1024 * 1024
MAX_ROOT_BYTES = 16 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}")


def _encoded(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _sha(data):
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ArchivePageSource:
    path: Path
    document_id: str
    version: int
    owner_id: str
    content_hash: str
    size: int
    total_pages: int

    def identity(self):
        _validate_original_request(self.path, self.content_hash, self.size)
        if not isinstance(self.document_id, str) or not 1 <= len(self.document_id) <= 256:
            raise ValueError("invalid source document ID")
        if not isinstance(self.owner_id, str) or not 1 <= len(self.owner_id) <= 1024:
            raise ValueError("invalid source owner")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("invalid immutable source version")
        if type(self.total_pages) is not int or not 0 < self.total_pages <= ARCHIVE_MAX_PAGES:
            raise ValueError("invalid original page count")
        return {
            "document_id": self.document_id,
            "version": self.version,
            "owner_id": self.owner_id,
            "content_hash": self.content_hash,
            "size": self.size,
            "total_pages": self.total_pages,
        }


def _read(path, maximum):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
        raise ValueError("evidence must be a bounded regular file")
    stream, opened = _open_original(str(path), info.st_size)
    with stream:
        data = stream.read(maximum + 1)
        if len(data) != info.st_size:
            raise ValueError("evidence size changed")
        _verify_identity(str(path), opened, info.st_size)
    return data


def _reference(blob, output):
    return {"cas_path": blob.path.relative_to(output).as_posix(), "sha256": blob.digest, "size": blob.size}


def _blob_path(output, owner, reference):
    digest, size = reference.get("sha256"), reference.get("size")
    if not isinstance(digest, str) or not _HASH.fullmatch(digest) or type(size) is not int or size < 1:
        raise ValueError("invalid evidence blob reference")
    expected = Path("files/blobs") / _sha(owner.encode()) / digest[:2] / digest[2:4] / digest
    if reference.get("cas_path") != expected.as_posix():
        raise ValueError("evidence blob owner/path mismatch")
    path = output / expected
    for parent in (path, *path.parents):
        if parent == output:
            break
        if parent.is_symlink():
            raise ValueError("evidence blob path must not contain symlinks")
    return path


def _read_blob(output, owner, reference, maximum):
    raw = _read(_blob_path(output, owner, reference), maximum)
    if len(raw) != reference["size"] or _sha(raw) != reference["sha256"]:
        raise ValueError("evidence blob hash/size mismatch")
    return raw


def _validate_page(page, identity):
    if not isinstance(page, dict):
        raise ValueError("invalid page checkpoint")
    number = page.get("page")
    if type(number) is not int or not 1 <= number <= identity["total_pages"]:
        raise ValueError("physical page outside original")
    if page.get("source_hash") != identity["content_hash"] or page.get("status") not in _KNOWN_STATUSES:
        raise ValueError("page original hash/status mismatch")
    if not all(isinstance(page.get(field), str) for field in ("text", "native_text")):
        raise ValueError("invalid page text evidence")
    if page["status"] == "pending_parse" and (page["text"] or page["native_text"]):
        raise ValueError("pending page has nonempty text")
    for evidence in (page.get("ocr"), page.get("native_visibility")):
        if evidence is None:
            continue
        if not isinstance(evidence, dict):
            raise ValueError("invalid current page evidence")
        if ("source_hash" in evidence and evidence["source_hash"] != identity["content_hash"]) or (
            "page" in evidence and (type(evidence["page"]) is not int or evidence["page"] != number)
        ):
            raise ValueError("current page evidence original/physical-page binding mismatch")
    error_response = (page.get("ocr") or {}).get("error_response")
    if isinstance(error_response, dict) and (
        ("source_hash" in error_response and error_response["source_hash"] != identity["content_hash"])
        or (
            "page" in error_response
            and (type(error_response["page"]) is not int or error_response["page"] != number)
        )
    ):
        raise ValueError("current OCR response evidence original/physical-page binding mismatch")


def _checkpoint(raw, identity):
    value = json.loads(raw)
    archive = value.get("archive_pdf", {})
    if (
        value.get("source_hash") != identity["content_hash"]
        or archive.get("source_size") != identity["size"]
        or archive.get("total_pages") != identity["total_pages"]
        or not isinstance(value.get("pages"), list)
        or len(value["pages"]) != identity["total_pages"]
    ):
        raise ValueError("legacy checkpoint original binding mismatch")
    for number, page in enumerate(value["pages"], 1):
        _validate_page(page, identity)
        if page["page"] != number:
            raise ValueError("legacy checkpoint page mapping is not complete and ordered")
    return value


def _path_references(value, pointer=""):
    if isinstance(value, list):
        for number, item in enumerate(value):
            yield from _path_references(item, f"{pointer}/{number}")
    elif isinstance(value, dict):
        for key, item in value.items():
            child = pointer + "/" + str(key).replace("~", "~0").replace("/", "~1")
            if (
                isinstance(item, str)
                and (key in {"path", "full_page_PNG", "PNG_CAS_path"} or key.endswith("_path"))
                and "files/blobs/" in item
            ):
                yield child, item
            yield from _path_references(item, child)


def _copy_dependencies(pages, identity, evidence_root, output, store):
    dependencies = []
    for page in pages:
        for pointer, original_path in _path_references(page):
            if evidence_root is None:
                raise ValueError("legacy page CAS references require an explicit evidence root")
            digest = Path(original_path).name
            if not _HASH.fullmatch(digest):
                raise ValueError("legacy evidence path is not a content-addressed object")
            relative = (
                Path("files/blobs") / _sha(identity["owner_id"].encode()) / digest[:2] / digest[2:4] / digest
            )
            supplied = Path(original_path)
            absolute = supplied if supplied.is_absolute() else evidence_root / supplied
            candidate = evidence_root / relative
            if absolute != candidate:
                raise ValueError("legacy evidence path is outside the original owner CAS")
            for parent in (candidate, *candidate.parents):
                if parent == evidence_root:
                    break
                if parent.is_symlink():
                    raise ValueError("legacy evidence path contains a symlink")
            raw = _read(candidate, MAX_CHECKPOINT_BYTES)
            if _sha(raw) != digest:
                raise ValueError("legacy evidence content hash mismatch")
            blob = store.put_bytes(identity["owner_id"], raw)
            dependencies.append(
                {
                    "page": page["page"],
                    "source_hash": identity["content_hash"],
                    "json_pointer": pointer,
                    "original_path": original_path,
                    "blob": _reference(blob, output),
                }
            )
    return dependencies


def _summary(root):
    covered = set()
    statuses = Counter()
    for segment in root["segments"]:
        numbers = set(range(segment["first_page"], segment["last_page"] + 1))
        if numbers & covered:
            raise ValueError("overlapping committed page segments")
        covered |= numbers
        statuses.update(segment["page_statuses"])
    total = root["source_identity"]["total_pages"]
    pending = [number for number in range(1, total + 1) if number not in covered]
    root.update(
        native_mapping_complete=not pending,
        mapped_pages=len(covered),
        pending_pages=pending,
        page_statuses=dict(statuses),
        text_complete=False,
        derived_status="not_generated",
        index_status="not_generated",
        quality={"complete": False, "status": "native_evidence_only"},
    )


def _save_root(output, root, previous_hash):
    path = output / "root.json"
    if previous_hash is not None and _sha(_read(path, MAX_ROOT_BYTES)) != previous_hash:
        raise ValueError("root changed during the exclusive extraction run")
    root["segments"].sort(key=lambda segment: segment["first_page"])
    _summary(root)
    root["revision"] += 1
    root["previous_root_sha256"] = previous_hash
    raw = _encoded(root)
    if len(raw) > MAX_ROOT_BYTES:
        raise ValueError("page archive root exceeds bounded metadata limit")
    descriptor, name = tempfile.mkstemp(prefix=".root-", suffix=".part", dir=output)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(output)
    finally:
        temporary.unlink(missing_ok=True)
    return _sha(raw)


def _add_segment(root, output, store, pages, origin, *, worker=None, run_id=None):
    identity = root["source_identity"]
    if not pages or len(pages) > 64:
        raise ValueError("page segments must contain 1 to 64 pages")
    for page in pages:
        _validate_page(page, identity)
        if page["status"] == "pending_parse":
            raise ValueError("unparsed placeholders cannot be committed as extracted pages")
    first, last = pages[0]["page"], pages[-1]["page"]
    if [page["page"] for page in pages] != list(range(first, last + 1)):
        raise ValueError("page segment is not a consecutive absolute range")
    if sum(max(len(page["text"]), len(page["native_text"])) for page in pages) > _MAX_TEXT:
        raise ValueError("page segment exceeds bounded text limit")
    if any(first <= old["last_page"] and old["first_page"] <= last for old in root["segments"]):
        raise ValueError("page segment overlaps an already committed checkpoint")
    payload = {"protocol": SEGMENT_PROTOCOL, "source_identity": identity, "pages": pages}
    raw = _encoded(payload)
    if len(raw) > MAX_SEGMENT_BYTES:
        raise ValueError("page segment exceeds bounded payload size; use a smaller native window")
    blob = store.put_bytes(identity["owner_id"], raw)
    descriptor = {
        **_reference(blob, output),
        "first_page": first,
        "last_page": last,
        "origin": origin,
        "page_statuses": dict(Counter(page["status"] for page in pages)),
        "page_sha256": [_sha(_encoded(page)) for page in pages],
    }
    if worker is not None:
        descriptor.update(worker_stats=worker, run_id=run_id)
    root["segments"].append(descriptor)


def _validate_root(output, root, identity, checkpoint_raw=None):
    if (
        not isinstance(root, dict)
        or root.get("protocol") != PROTOCOL
        or root.get("source_identity") != identity
    ):
        raise ValueError("page archive original/document/version/owner binding mismatch")
    if (
        root.get("derived_status") != "not_generated"
        or root.get("index_status") != "not_generated"
        or root.get("text_complete") is not False
        or root.get("quality", {}).get("complete") is not False
    ):
        raise ValueError("page evidence cannot claim derived text or an index")
    if (
        root["original"].get("sha256") != identity["content_hash"]
        or root["original"].get("size") != identity["size"]
    ):
        raise ValueError("preserved original reference does not match source")
    path = _blob_path(output, identity["owner_id"], root["original"])
    stream, info = _open_original(str(path), identity["size"])
    with stream:
        _verify_hash(stream, identity["content_hash"], identity["size"])
        _verify_identity(str(path), info, identity["size"])
    legacy = None
    if root.get("legacy_checkpoint"):
        raw = _read_blob(output, identity["owner_id"], root["legacy_checkpoint"], MAX_CHECKPOINT_BYTES)
        if checkpoint_raw is not None and raw != checkpoint_raw:
            raise ValueError("legacy checkpoint bytes changed")
        legacy = _checkpoint(raw, identity)
    elif checkpoint_raw is not None:
        raise ValueError("cannot add a legacy checkpoint to an existing native archive")
    if not isinstance(root.get("segments"), list) or len(root["segments"]) > identity["total_pages"]:
        raise ValueError("too many page segments")
    saved_legacy = set()
    for segment in root["segments"]:
        if not isinstance(segment, dict):
            raise ValueError("invalid page segment descriptor")
        first, last = segment.get("first_page"), segment.get("last_page")
        if (
            type(first) is not int
            or type(last) is not int
            or not 1 <= first <= last <= identity["total_pages"]
            or last - first >= 64
        ):
            raise ValueError("page segment physical range exceeds bounded original")
        raw = _read_blob(output, identity["owner_id"], segment, MAX_SEGMENT_BYTES)
        payload = json.loads(raw)
        if payload.get("protocol") != SEGMENT_PROTOCOL or payload.get("source_identity") != identity:
            raise ValueError("page segment source identity mismatch")
        pages = payload.get("pages", [])
        if not 1 <= len(pages) <= 64:
            raise ValueError("invalid page segment length")
        for page in pages:
            _validate_page(page, identity)
            if page["status"] == "pending_parse":
                raise ValueError("committed segment contains an unparsed page")
        if [page["page"] for page in pages] != list(range(segment["first_page"], segment["last_page"] + 1)):
            raise ValueError("page segment physical range mismatch")
        if [_sha(_encoded(page)) for page in pages] != segment.get("page_sha256"):
            raise ValueError("page checkpoint hash mismatch")
        if dict(Counter(page["status"] for page in pages)) != segment.get("page_statuses"):
            raise ValueError("page segment status summary mismatch")
        if sum(max(len(page["text"]), len(page["native_text"])) for page in pages) > _MAX_TEXT:
            raise ValueError("page segment exceeds bounded text limit")
        if segment.get("origin") not in {"legacy_checkpoint", "native_extraction"}:
            raise ValueError("unknown page segment origin")
        if segment.get("origin") == "legacy_checkpoint":
            if legacy is None or any(page != legacy["pages"][page["page"] - 1] for page in pages):
                raise ValueError("saved legacy page evidence changed")
            saved_legacy.update(page["page"] for page in pages)
        elif legacy is not None and any(
            legacy["pages"][page["page"] - 1]["status"] != "pending_parse" for page in pages
        ):
            raise ValueError("native extraction cannot replace an existing legacy page")
    expected_dependencies = set()
    if legacy is not None:
        old_pages = [page for page in legacy["pages"] if page["status"] != "pending_parse"]
        if saved_legacy != {page["page"] for page in old_pages}:
            raise ValueError("saved legacy page coverage changed")
        expected_dependencies = {
            (page["page"], pointer, path) for page in old_pages for pointer, path in _path_references(page)
        }
    observed_dependencies = set()
    for dependency in root.get("legacy_dependencies", []):
        if dependency.get("source_hash") != identity["content_hash"]:
            raise ValueError("legacy evidence source binding changed")
        _read_blob(output, identity["owner_id"], dependency["blob"], MAX_CHECKPOINT_BYTES)
        key = (dependency.get("page"), dependency.get("json_pointer"), dependency.get("original_path"))
        if (
            key in observed_dependencies
            or Path(dependency["original_path"]).name != dependency["blob"]["sha256"]
        ):
            raise ValueError("legacy evidence mapping/hash mismatch")
        observed_dependencies.add(key)
    if observed_dependencies != expected_dependencies:
        raise ValueError("legacy evidence path/page mapping changed")
    expected = {
        key: root.get(key)
        for key in ("mapped_pages", "pending_pages", "native_mapping_complete", "page_statuses")
    }
    _summary(root)
    if any(root[key] != value for key, value in expected.items()):
        raise ValueError("page archive coverage summary mismatch")


def _ranges(numbers, size):
    batch = []
    for number in numbers:
        if batch and (number != batch[-1] + 1 or len(batch) == size):
            yield batch
            batch = []
        batch.append(number)
    if batch:
        yield batch


def _peak_worker_count(intervals):
    events = []
    for item in intervals:
        if isinstance(item, dict) and item.get("finished_monotonic_ns", 0) > item.get(
            "started_monotonic_ns", 0
        ):
            events.extend([(item["started_monotonic_ns"], 1), (item["finished_monotonic_ns"], -1)])
    active = peak = 0
    for _, delta in sorted(events):
        active += delta
        peak = max(peak, active)
    return peak


def extract_native_page_shards(
    source: ArchivePageSource,
    output: Path,
    *,
    checkpoint: Path | None = None,
    evidence_root: Path | None = None,
    workers: int = 1,
    window_pages: int = 64,
    max_windows: int | None = None,
    on_commit=None,
):
    """Resume verified pages; native extraction only, with no model capability.

    ``max_windows`` bounds newly scheduled work and is useful for an intentional
    stop/restart check. Existing committed pages are never extracted again.
    The returned report and root explicitly prohibit indexing these fragments.
    """
    identity = source.identity()
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("native archive concurrency must be between 1 and 8")
    if type(window_pages) is not int or not 1 <= window_pages <= 64:
        raise ValueError("native archive window must be between 1 and 64")
    if max_windows is not None and (type(max_windows) is not int or max_windows < 0):
        raise ValueError("max_windows must be a nonnegative integer")
    output = Path(output).absolute()
    if output.is_symlink():
        raise ValueError("archive output cannot be a symlink")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = output / ".native-pages.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("this native page archive already has an active coordinator") from exc
        return _extract_locked(
            source, output, identity, checkpoint, evidence_root, workers, window_pages, max_windows, on_commit
        )
    finally:
        os.close(lock_fd)


def _extract_locked(
    source, output, identity, checkpoint, evidence_root, workers, window_pages, max_windows, on_commit
):
    started, run_id = time.monotonic(), uuid4().hex
    inspected = inspect_archive_pdf_bounded(source.path, source_hash=source.content_hash, size=source.size)
    if inspected != {
        "source_hash": source.content_hash,
        "source_size": source.size,
        "total_pages": source.total_pages,
    }:
        raise ValueError("actual original page count does not match source binding")
    store = BlobStore(output / "files", max_bytes=_MAX_BYTES)
    checkpoint_raw = _read(Path(checkpoint), MAX_CHECKPOINT_BYTES) if checkpoint is not None else None
    stream, info = _open_original(str(source.path), source.size)
    with stream:
        _verify_hash(stream, source.content_hash, source.size)
        blob = store.put(source.owner_id, iter(lambda: stream.read(1024 * 1024), b""))
        _verify_identity(str(source.path), info, source.size)
    if blob.digest != source.content_hash or blob.size != source.size:
        raise ValueError("original changed while preserving its immutable evidence")
    root_path = output / "root.json"
    if root_path.exists():
        raw = _read(root_path, MAX_ROOT_BYTES)
        root, root_hash = json.loads(raw), _sha(raw)
        _validate_root(output, root, identity, checkpoint_raw)
    else:
        root = {
            "protocol": PROTOCOL,
            "source_identity": identity,
            "original": _reference(blob, output),
            "legacy_checkpoint": None,
            "legacy_dependencies": [],
            "path_policy": "Page objects retain original evidence paths. Resolve only through legacy_dependencies; relocated CAS paths are relative to this archive root.",
            "segments": [],
            "revision": 0,
        }
        if checkpoint_raw is not None:
            legacy = _checkpoint(checkpoint_raw, identity)
            root["legacy_checkpoint"] = _reference(store.put_bytes(source.owner_id, checkpoint_raw), output)
            saved = [page for page in legacy["pages"] if page["status"] != "pending_parse"]
            root["legacy_dependencies"] = _copy_dependencies(
                saved, identity, Path(evidence_root).absolute() if evidence_root else None, output, store
            )
            by_number = {page["page"]: page for page in saved}
            for numbers in _ranges(sorted(by_number), 64):
                _add_segment(
                    root, output, store, [by_number[number] for number in numbers], "legacy_checkpoint"
                )
        root_hash = _save_root(output, root, None)
    before = root["mapped_pages"]
    ranges = list(_ranges(root["pending_pages"], window_pages))
    if max_windows is not None:
        ranges = ranges[:max_windows]
    committed, intervals, errors = 0, [], []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="archive-native-window") as executor:
        futures = {
            executor.submit(
                parse_archive_pdf_pages_bounded,
                source.path,
                source_hash=source.content_hash,
                size=source.size,
                total_pages=source.total_pages,
                first_page=numbers[0],
                max_pages=len(numbers),
            ): numbers
            for numbers in ranges
        }
        try:
            for future in as_completed(futures):
                numbers = futures[future]
                try:
                    result = future.result()
                except Exception as exc:
                    errors.append(
                        {"first_page": numbers[0], "last_page": numbers[-1], "error": str(exc)[:500]}
                    )
                    continue
                if (
                    result.get("source_hash") != source.content_hash
                    or result.get("source_size") != source.size
                    or result.get("total_pages") != source.total_pages
                    or [page.get("page") for page in result.get("pages", [])] != numbers
                    or result.get("derived_status") != "not_generated"
                    or result.get("index_status") != "not_generated"
                    or result.get("chunks")
                    or result.get("elements")
                ):
                    raise ValueError("native worker returned an invalid absolute window")
                worker = result.get("worker_stats")
                _add_segment(
                    root, output, store, result["pages"], "native_extraction", worker=worker, run_id=run_id
                )
                root_hash = _save_root(output, root, root_hash)
                committed += 1
                intervals.append(worker)
                if on_commit is not None:
                    on_commit(
                        {
                            "mapped_pages": root["mapped_pages"],
                            "total_pages": source.total_pages,
                            "committed_windows": committed,
                            "root_sha256": root_hash,
                        }
                    )
        finally:
            for future in futures:
                future.cancel()
    _validate_root(output, root, identity, checkpoint_raw)
    return {
        "protocol": PROTOCOL,
        "run_id": run_id,
        "source_identity": identity,
        "output": str(output),
        "root_sha256": root_hash,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "previously_mapped_pages": before,
        "newly_extracted_pages": root["mapped_pages"] - before,
        "mapped_pages": root["mapped_pages"],
        "pending_pages": len(root["pending_pages"]),
        "native_mapping_complete": root["native_mapping_complete"],
        "page_statuses": root["page_statuses"],
        "scheduled_windows": len(ranges),
        "committed_windows": committed,
        "requested_workers": workers,
        "measured_peak_worker_intervals": _peak_worker_count(intervals),
        "errors": errors,
        "model_calls": 0,
        "old_pages_reparsed": 0,
        "text_complete": False,
        "derived_status": "not_generated",
        "index_status": "not_generated",
    }
