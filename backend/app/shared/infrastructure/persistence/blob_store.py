"""Immutable, account-scoped originals on a local POSIX filesystem.

The staging directory and blob directory must share a filesystem with atomic
hard links and directory fsync support. A completed put is durable before a
caller records its reference in SQLite. A failed database transaction must not
unlink the blob: another document or worker may already reference it.

Hard process termination can leave an unreferenced blob or staging file. Staging
writers share a process lock; startup removes only old, unreferenced temporary
files while holding its exclusive counterpart.
"""

import fcntl
import hashlib
import os
import stat
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

_STAGING_LOCK = ".writers.lock"
_STAGING_ORPHAN_AGE_SECONDS = 60 * 60
_CLEANED_STAGING_PATHS: set[Path] = set()
_CLEANUP_GUARD = Lock()


@dataclass(frozen=True)
class Blob:
    path: Path
    digest: str
    size: int


class BlobIntegrityError(ValueError):
    """An existing content-addressed object is not the expected regular file."""


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _make_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileNotFoundError:
        _make_directory(path.parent)
        _make_directory(path)
        return
    except FileExistsError:
        if not stat.S_ISDIR(path.lstat().st_mode):
            raise ValueError("blob storage requires real directories") from None
    else:
        _sync_directory(path.parent)


def _verify_existing(path: Path, digest: str, size: int) -> None:
    if not stat.S_ISREG(path.lstat().st_mode):
        raise BlobIntegrityError("blob integrity check failed: expected a regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_size != size:
            raise BlobIntegrityError("blob integrity check failed: unexpected file size")
        actual = hashlib.sha256()
        read_size = 0
        while chunk := stream.read(64 * 1024):
            read_size += len(chunk)
            if read_size > size:
                raise BlobIntegrityError("blob integrity check failed: file changed while reading")
            actual.update(chunk)
        after = os.fstat(stream.fileno())
        if read_size != size or actual.hexdigest() != digest or before.st_mtime_ns != after.st_mtime_ns:
            raise BlobIntegrityError("blob integrity check failed: unexpected content")
        os.fsync(stream.fileno())


class BlobStore:
    def __init__(self, files_dir: Path, max_bytes: int = 20 * 1024 * 1024):
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        # The configured storage root is trusted; no filename or owner is used
        # directly as a path segment below it.
        self.files_dir = Path(files_dir).resolve()
        self.max_bytes = max_bytes
        self.blobs_dir = self.files_dir / "blobs"
        self.staging_dir = self.files_dir / ".staging"
        for directory in (self.files_dir, self.blobs_dir, self.staging_dir):
            _make_directory(directory)
        self._cleanup_stale_staging_once()

    def _open_staging_lock(self):
        path = self.staging_dir / _STAGING_LOCK
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("blob staging lock must be a regular file")
            os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _cleanup_stale_staging_once(self):
        with _CLEANUP_GUARD:
            if self.staging_dir in _CLEANED_STAGING_PATHS:
                return
            try:
                completed = self._cleanup_stale_staging()
            except OSError:
                return
            if completed:
                _CLEANED_STAGING_PATHS.add(self.staging_dir)

    def _cleanup_stale_staging(self):
        lock_fd = self._open_staging_lock()
        try:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            cutoff = time.time() - _STAGING_ORPHAN_AGE_SECONDS
            removed = False
            for path in self.staging_dir.iterdir():
                if not (path.name.startswith("blob-") and path.name.endswith(".part")):
                    continue
                try:
                    info = path.lstat()
                    if not stat.S_ISREG(info.st_mode) or info.st_mtime > cutoff:
                        continue
                    path.unlink()
                    removed = True
                except FileNotFoundError:
                    continue
            if removed:
                _sync_directory(self.staging_dir)
            return True
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def put_bytes(self, owner: str, content: bytes) -> Blob:
        return self.put(owner, (content,))

    def put(self, owner: str, chunks: Iterable[bytes]) -> Blob:
        if not isinstance(owner, str) or not owner:
            raise ValueError("blob owner must be a nonempty string")
        lock_fd = self._open_staging_lock()
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
        except BaseException:
            os.close(lock_fd)
            raise
        try:
            return self._put_locked(owner, chunks)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    def _put_locked(self, owner: str, chunks: Iterable[bytes]) -> Blob:
        owner_digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        fd, temporary_name = tempfile.mkstemp(prefix="blob-", suffix=".part", dir=self.staging_dir)
        temporary = Path(temporary_name)
        try:
            stream = os.fdopen(fd, "wb")
            fd = None
            with stream:
                digest = hashlib.sha256()
                size = 0
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("blob chunks must be bytes")
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError(f"blob exceeds maximum size of {self.max_bytes} bytes")
                    stream.write(chunk)
                    digest.update(chunk)
                if not size:
                    raise ValueError("blob content must not be empty")
                stream.flush()
                os.fchmod(stream.fileno(), 0o400)
                os.fsync(stream.fileno())

            content_digest = digest.hexdigest()
            directory = self.blobs_dir
            for component in (owner_digest, content_digest[:2], content_digest[2:4]):
                directory /= component
                _make_directory(directory)
            destination = directory / content_digest
            try:
                # Unlike replace/rename, link never overwrites an existing blob.
                os.link(temporary, destination, follow_symlinks=False)
            except FileExistsError:
                _verify_existing(destination, content_digest, size)

            # Also sync ancestors created by a concurrent writer: observing an
            # existing directory does not imply its creator has flushed it yet.
            while True:
                _sync_directory(directory)
                if directory == self.files_dir:
                    break
                directory = directory.parent
            _sync_directory(self.files_dir.parent)
            return Blob(path=destination, digest=content_digest, size=size)
        finally:
            failure = sys.exception()
            try:
                if fd is not None:
                    os.close(fd)
                temporary.unlink(missing_ok=True)
                _sync_directory(self.staging_dir)
            except OSError as cleanup_error:
                if failure is None:
                    raise
                failure.add_note(f"Staging cleanup failed: {cleanup_error}")
