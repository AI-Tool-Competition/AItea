"""Immutable, owner-scoped transfer receipts independent of a CLI JSON cache."""

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager

from app.shared.domain.values import dump


class TransferReceipts:
    def __init__(self, blobs):
        self.blobs = blobs

    @contextmanager
    def _directory(self, owner, *, create=False):
        descriptor = os.open(self.blobs.files_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in ("html-image-transfer-receipts", hashlib.sha256(owner.encode()).hexdigest()):
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    # Also flush an entry created by a concurrent writer.
                    os.fsync(descriptor)
                try:
                    next_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=descriptor,
                    )
                except FileNotFoundError:
                    yield None
                    return
                os.close(descriptor)
                descriptor = next_fd
            yield descriptor
        finally:
            os.close(descriptor)

    @staticmethod
    def _filename(key, kind):
        if not re.fullmatch(r"[0-9a-f]{64}", key) or not re.fullmatch(r"complete|intent-[123]", kind):
            raise ValueError("图片下载收据标识无效。")
        return key + "." + kind + ".json"

    @staticmethod
    def _read(directory, filename):
        try:
            descriptor = os.open(filename, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= 2 * 1024 * 1024:
                raise ValueError("图片下载收据超过限制或不是普通文件。")
            raw = stream.read(before.st_size + 1)
            after = os.fstat(stream.fileno())
            if len(raw) != before.st_size or (before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                raise ValueError("图片下载收据读取期间已变化。")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("图片下载收据格式无效。")
            return result

    def read(self, owner, key, *, kind="complete"):
        filename = self._filename(key, kind)
        with self._directory(owner) as directory:
            return None if directory is None else self._read(directory, filename)

    def store(self, owner, key, receipt, *, kind="complete"):
        filename = self._filename(key, kind)
        payload = dump(receipt).encode()
        if not 0 < len(payload) <= 2 * 1024 * 1024:
            raise ValueError("图片下载收据超过限制。")
        blob = self.blobs.put_bytes(owner, payload)
        with self._directory(owner, create=True) as directory:
            try:
                # CAS and receipt index share the filesystem. A completed link
                # and fsync is durable before the mutable JSON callback runs.
                os.link(blob.path, filename, dst_dir_fd=directory, follow_symlinks=False)
            except FileExistsError:
                if self._read(directory, filename) != receipt:
                    raise ValueError("图片下载收据已存在且来源不同。") from None
            os.fsync(directory)
        return {
            "content_hash": blob.digest,
            "size": blob.size,
            "storage_path": str(blob.path.relative_to(self.blobs.files_dir.parent)),
            "mime_type": "application/json",
        }
