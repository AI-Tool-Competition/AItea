"""Validator-bound range checkpoints; parts never become document originals."""

import hashlib
import os
import re
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from .blob_store import Blob
from .db import now, uid
from .ingestion import DownloadError, MAX_ORIGINAL_BYTES


class TransferPending(DownloadError):
    """Useful progress or a changed source version, not a failed attempt."""

    def __init__(self):
        super().__init__("原件正在分段下载，已保存进度。", retryable=True)


class CollectionTransfers:
    def __init__(self, collection):
        self.collection, self.db = collection, collection.db
        self.blobs = collection.service.archive.blobs
        self.minimum_size = 1024 * 1024
        # Slow official sources often spend seconds opening each connection.
        # Larger bounded parts amortize that cost; existing transfers retain
        # their recorded geometry and continue from already verified bytes.
        self.part_size = 512 * 1024
        self.slow_after = 5.0
        self.minimum_rate = 32 * 1024
        with self.db.transaction() as conn:
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version=12").fetchone():
                return
            conn.execute("""CREATE TABLE collection_transfers(
                id TEXT PRIMARY KEY,item_id TEXT NOT NULL REFERENCES collection_items(id),
                owner_id TEXT NOT NULL REFERENCES users(id),url TEXT NOT NULL,etag TEXT NOT NULL,
                total_size INTEGER NOT NULL CHECK(total_size>0 AND total_size<=134217728),
                part_size INTEGER NOT NULL CHECK(part_size>0 AND part_size<=524288),
                filename TEXT NOT NULL,mime_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','complete','superseded','unsupported')),
                content_hash TEXT,storage_path TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)""")
            conn.execute(
                "CREATE UNIQUE INDEX transfer_active ON collection_transfers(item_id) WHERE status='active'"
            )
            conn.execute(
                "CREATE INDEX transfers_item ON collection_transfers(item_id,created_at DESC,id DESC)"
            )
            conn.execute("""CREATE TABLE collection_transfer_parts(
                transfer_id TEXT NOT NULL REFERENCES collection_transfers(id),
                start_byte INTEGER NOT NULL CHECK(start_byte>=0),size INTEGER NOT NULL CHECK(size>0),
                content_hash TEXT NOT NULL,storage_path TEXT NOT NULL,created_at TEXT NOT NULL,
                PRIMARY KEY(transfer_id,start_byte))""")
            conn.execute("INSERT INTO schema_migrations VALUES(12,?)", (now(),))

    def _valid(self, conn, claim):
        if not self.collection._valid_claim(conn, claim):
            raise DownloadError("采集任务已更新，旧请求不能提交结果。")

    def _current(self, conn, claim):
        return conn.execute(
            "SELECT * FROM collection_transfers WHERE item_id=? AND owner_id=? ORDER BY created_at DESC,id DESC LIMIT 1",
            (claim["id"], claim["owner_id"]),
        ).fetchone()

    def _can_segment(self, metadata):
        return (
            type(metadata.get("total_size")) is int
            and self.minimum_size <= metadata["total_size"] <= MAX_ORIGINAL_BYTES
            and metadata.get("accept_ranges")
            and isinstance(metadata.get("etag"), str)
            and re.fullmatch(r'"[\x21\x23-\x7e]{0,1020}"', metadata["etag"])
            and metadata.get("content_encoding", "identity") in {"identity", ""}
        )

    def _begin(self, conn, claim, metadata):
        self._valid(conn, claim)
        identifier = uid()
        conn.execute(
            "UPDATE collection_transfers SET status='superseded',updated_at=? WHERE item_id=? AND status='active'",
            (now(), claim["id"]),
        )
        conn.execute(
            "INSERT INTO collection_transfers VALUES(?,?,?,?,?,?,?,?,?,'active',NULL,NULL,?,?)",
            (
                identifier,
                claim["id"],
                claim["owner_id"],
                metadata["url"],
                metadata["etag"],
                metadata["total_size"],
                self.part_size,
                metadata["filename"],
                metadata["mime_type"],
                now(),
                now(),
            ),
        )
        return dict(conn.execute("SELECT * FROM collection_transfers WHERE id=?", (identifier,)).fetchone())

    def _path(self, owner, digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise DownloadError("分段文件哈希无效。")
        return (
            self.blobs.blobs_dir
            / hashlib.sha256(owner.encode()).hexdigest()
            / digest[:2]
            / digest[2:4]
            / digest
        )

    def _read_part(self, owner, row):
        path = self._path(owner, row["content_hash"])
        if Path(row["storage_path"]) != path:
            raise DownloadError("分段文件路径与账号不一致。")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != row["size"]:
                raise DownloadError("分段文件大小或类型不一致。")
            digest, count = hashlib.sha256(), 0
            while block := stream.read(65536):
                count += len(block)
                if count > row["size"]:
                    raise DownloadError("分段文件在读取时发生变化。")
                digest.update(block)
                yield block
            after = os.fstat(stream.fileno())
            if (
                count != row["size"]
                or digest.hexdigest() != row["content_hash"]
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise DownloadError("分段文件完整性校验失败。")

    def _finish(self, claim, transfer, parts):
        def chunks():
            offset = 0
            for part in parts:
                if part["start_byte"] != offset:
                    raise DownloadError("原件分段缺失或重叠。")
                yield from self._read_part(claim["owner_id"], part)
                offset += part["size"]
            if offset != transfer["total_size"]:
                raise DownloadError("原件分段总大小不一致。")

        blob = self.blobs.put(claim["owner_id"], chunks())
        with self.db.transaction() as conn:
            self._valid(conn, claim)
            current = self._current(conn, claim)
            if current["id"] != transfer["id"] or current["status"] != "active":
                raise TransferPending()
            conn.execute(
                "UPDATE collection_transfers SET status='complete',content_hash=?,storage_path=?,updated_at=? WHERE id=?",
                (blob.digest, str(blob.path), now(), transfer["id"]),
            )
        return self._result(transfer, blob)

    def _result(self, transfer, blob):
        return {
            "url": transfer["url"],
            "filename": transfer["filename"],
            "mime_type": transfer["mime_type"],
            "etag": transfer["etag"],
            "last_modified": None,
            "blob": blob,
            "transfer_bytes": transfer["total_size"],
        }

    def download(self, claim, *, validators, options, required_host, discovery=False, timeout_seconds=90):
        from .ingestion import _download

        def live_chunks(chunks):
            checked = 0
            for block in chunks:
                if time.monotonic() - checked >= 1:
                    with closing(self.db.connect()) as conn:
                        self._valid(conn, claim)
                    checked = time.monotonic()
                yield block

        maximum = options.pop("max_bytes")
        with closing(self.db.connect()) as conn:
            self._valid(conn, claim)
            row = self._current(conn, claim)
            transfer = dict(row) if row else None
        if transfer and transfer["status"] == "complete":
            blob = Blob(
                self._path(claim["owner_id"], transfer["content_hash"]),
                transfer["content_hash"],
                transfer["total_size"],
            )
            # Reuse only a locally verified completed checkpoint of this item.
            complete = {"storage_path": str(blob.path), "content_hash": blob.digest, "size": blob.size}
            for _ in self._read_part(claim["owner_id"], complete):
                pass
            return self._result(transfer, blob)
        if not transfer or transfer["status"] in {"superseded", "unsupported"}:
            deadline = time.monotonic() + timeout_seconds
            started = time.monotonic()
            metadata = {}

            def headers(meta):
                with closing(self.db.connect()) as conn:
                    self._valid(conn, claim)
                metadata.update(meta)

            def consume(chunks):
                def observed():
                    received = 0
                    for block in live_chunks(chunks):
                        received += len(block)
                        elapsed = time.monotonic() - started
                        if (
                            not discovery
                            and not (transfer and transfer["status"] == "unsupported")
                            and elapsed >= self.slow_after
                            and received / max(elapsed, 0.000001) < self.minimum_rate
                            and self._can_segment(metadata)
                        ):
                            with self.db.transaction() as conn:
                                self._begin(conn, claim, metadata)
                            raise TransferPending()
                        yield block

                return self.blobs.put(claim["owner_id"], observed())

            result = _download(
                claim["url"],
                deadline=deadline,
                required_host=required_host,
                consume=consume,
                validators=validators,
                connection_slot=lambda host: self.collection.network_slot(host, deadline),
                on_headers=headers,
                max_bytes=maximum,
                **options,
            )
            if transfer and transfer["status"] == "unsupported" and "blob" in result:
                blob = result["blob"]
                with self.db.transaction() as conn:
                    self._valid(conn, claim)
                    current = self._current(conn, claim)
                    if current["id"] != transfer["id"] or current["status"] != "unsupported":
                        raise TransferPending()
                    conn.execute(
                        """UPDATE collection_transfers SET status='complete',url=?,etag=?,
                                  total_size=?,filename=?,mime_type=?,content_hash=?,storage_path=?,updated_at=? WHERE id=?""",
                        (
                            result["url"],
                            result.get("etag") or "",
                            blob.size,
                            result["filename"],
                            result["mime_type"],
                            blob.digest,
                            str(blob.path),
                            now(),
                            transfer["id"],
                        ),
                    )
            return result

        with closing(self.db.connect()) as conn:
            parts = [
                dict(p)
                for p in conn.execute(
                    "SELECT * FROM collection_transfer_parts WHERE transfer_id=? ORDER BY start_byte",
                    (transfer["id"],),
                )
            ]
        present = {p["start_byte"] for p in parts}
        missing = [
            start for start in range(0, transfer["total_size"], transfer["part_size"]) if start not in present
        ]
        if not missing:
            return self._finish(claim, transfer, parts)

        def fetch(start):
            end = min(start + transfer["part_size"], transfer["total_size"]) - 1
            deadline = time.monotonic() + 90

            def headers(meta):
                with closing(self.db.connect()) as conn:
                    self._valid(conn, claim)
                if (
                    meta["url"] == transfer["url"]
                    and meta.get("etag") == transfer["etag"]
                    and meta["total_size"] == transfer["total_size"]
                    and meta["response_status"] == 206
                    and meta["range_start"] == start
                    and meta["range_end"] == end
                ):
                    return
                with self.db.transaction() as conn:
                    self._valid(conn, claim)
                    current = self._current(conn, claim)
                    if current["id"] != transfer["id"] or current["status"] != "active":
                        raise TransferPending()
                    if meta.get("etag") != transfer["etag"] or meta["url"] != transfer["url"]:
                        if self._can_segment(meta):
                            self._begin(conn, claim, meta)
                        else:
                            conn.execute(
                                "UPDATE collection_transfers SET status='unsupported',updated_at=? WHERE id=?",
                                (now(), transfer["id"]),
                            )
                    elif meta["response_status"] == 200:
                        conn.execute(
                            "UPDATE collection_transfers SET status='unsupported',updated_at=? WHERE id=?",
                            (now(), transfer["id"]),
                        )
                    else:
                        raise DownloadError("原件分段版本或范围校验失败。")
                raise TransferPending()

            fetched = _download(
                transfer["url"],
                deadline=deadline,
                required_host=required_host,
                consume=lambda chunks: self.blobs.put(claim["owner_id"], live_chunks(chunks)),
                byte_range=(start, end),
                range_validator=transfer["etag"],
                on_headers=headers,
                max_bytes=maximum,
                connection_slot=lambda host: self.collection.network_slot(host, deadline),
            )
            blob = fetched["blob"]
            if blob.size != end - start + 1:
                raise DownloadError("原件分段文件大小不一致。")
            with self.db.transaction() as conn:
                self._valid(conn, claim)
                current = self._current(conn, claim)
                if current["id"] != transfer["id"] or current["status"] != "active":
                    raise TransferPending()
                conn.execute(
                    "INSERT OR IGNORE INTO collection_transfer_parts VALUES(?,?,?,?,?,?)",
                    (transfer["id"], start, blob.size, blob.digest, str(blob.path), now()),
                )
                conn.execute(
                    "UPDATE collection_transfers SET updated_at=? WHERE id=?", (now(), transfer["id"])
                )

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="original-range") as pool:
            for result in pool.map(fetch, missing[:2]):
                pass
        with closing(self.db.connect()) as conn:
            parts = [
                dict(p)
                for p in conn.execute(
                    "SELECT * FROM collection_transfer_parts WHERE transfer_id=? ORDER BY start_byte",
                    (transfer["id"],),
                )
            ]
        if len(parts) == len(range(0, transfer["total_size"], transfer["part_size"])):
            return self._finish(claim, transfer, parts)
        raise TransferPending()

    def progress(self, conn, item_ids):
        if not item_ids:
            return {}
        marks = ",".join("?" for _ in item_ids)
        rows = conn.execute(
            f"""SELECT t.item_id,t.total_size,t.status,
            COALESCE(SUM(p.size),0) downloaded_bytes,t.created_at,t.id
            FROM collection_transfers t LEFT JOIN collection_transfer_parts p ON p.transfer_id=t.id
            WHERE t.item_id IN ({marks}) GROUP BY t.id ORDER BY t.created_at,t.id""",
            item_ids,
        ).fetchall()
        return {
            r["item_id"]: {
                "downloaded_bytes": r["total_size"] if r["status"] == "complete" else r["downloaded_bytes"],
                "download_total_bytes": r["total_size"],
                "resumable": r["status"] != "unsupported",
            }
            for r in rows
        }
