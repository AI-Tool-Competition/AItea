"""Explicit bounded acquisition of mapped HTML body images for owned drafts.

Successful protected transfers are checkpointed before binding, so a restart
can verify and reuse original image CAS bytes without another HTTP request.
"""

import hashlib
import json
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from threading import Lock

from app.contexts.documents.infrastructure.providers.html_linked_asset_ocr import (
    MAX_IMAGE_BYTES,
    MAX_TOTAL_IMAGE_BYTES,
    bind_linked_image_bounded,
    image_candidates_bounded,
    linked_image_references,
    read_image_blob,
    resolved_url,
)
from app.contexts.documents.infrastructure.recovery.html_image_transfer_receipts import TransferReceipts
from app.shared.domain.values import dump, now


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class HtmlProcessingPending(ValueError):
    """A valid draft is already queued and may resume through scoped workers."""


class HtmlTransferUnconfirmed(ValueError):
    """An interrupted request needs reconciliation before another transfer."""


class HtmlImageCollector:
    def __init__(self, service, *, state=None, checkpoint=None, max_requests=20):
        self.service = service
        self.receipts = TransferReceipts(service.archive.blobs)
        if type(max_requests) is not int or not 1 <= max_requests <= 100:
            raise ValueError("一次网页图片采集允许一至一百次下载尝试。")
        self.max_requests, self.request_attempts, self.reserved_requests = max_requests, 0, 0
        self.state = deepcopy(state or {"format": 1, "images": {}})
        if (
            self.state.get("format") != 1
            or not isinstance(self.state.get("images"), dict)
            or len(self.state["images"]) > 400
        ):
            raise ValueError("网页图片检查点格式或数量无效。")
        self.checkpoint = checkpoint or (lambda _: None)
        self.lock = Lock()

    def _save(self, key, value):
        with self.lock:
            candidate = deepcopy(self.state)
            candidate["images"][key] = value
            if len(candidate["images"]) > 400 or len(dump(candidate).encode()) > 2 * 1024 * 1024:
                raise ValueError("网页图片检查点超过限制。")
            self.state = candidate
            self.checkpoint(deepcopy(self.state))

    def _snapshot(
        self, owner, document, *, allow_processing=False, official_source=False, active_claim=None
    ):
        with closing(self.service.db.connect()) as conn:
            row = dict(self.service.own_document(conn, owner, document))
            enriched = self.service.document_dict(row, conn=conn)
            if official_source:
                from app.contexts.documents.application.system_library import SYSTEM_OWNER_ID

                imported = conn.execute(
                    "SELECT 1 FROM official_source_imports WHERE document_id=? AND status='stored' LIMIT 1",
                    (document,),
                ).fetchone()
                if owner != SYSTEM_OWNER_ID or row["role"] != "primary" or not imported:
                    raise ValueError("官方图片采集只允许已入库的系统主资料。")
            elif row["role"] != "primary" or enriched["metadata"].get("review_status") != "draft":
                raise ValueError("网页图片采集只允许账号内未审核的主资料，不处理普通附件。")
            if active_claim is not None and (
                active_claim.get("owner_id") != owner
                or active_claim.get("document_id") != document
                or not self.service.collection._valid_claim(conn, active_claim, True)
            ):
                raise ValueError("官方图片采集处理租约已失效。")
            if (
                not allow_processing
                and conn.execute(
                    "SELECT 1 FROM processing_jobs WHERE document_id=? AND status IN ('queued','processing')",
                    (document,),
                ).fetchone()
            ):
                raise HtmlProcessingPending("文档已有排队或在途处理，稍后再绑定图片。")
            aliases = [
                r["final_url"]
                for r in conn.execute(
                    "SELECT final_url FROM collection_items WHERE document_id=? AND owner_id=? AND status='stored' AND final_url IS NOT NULL ORDER BY fetched_at DESC,id DESC LIMIT 50",
                    (document, owner),
                )
            ]
            if official_source:
                aliases = [row["source_url"]] if row["source_url"] else []
        manifest = json.loads(row["manifest"])
        if (
            manifest.get("parser") != "html-basic"
            or manifest.get("parser_version") not in {"3", "4", "5", "6", "7", "8", "9", "10"}
            or not row["source_url"]
        ):
            raise ValueError("需要有实际来源网址的当前 HTML 原件映射。")
        path = Path(row["storage_path"])
        if not path.resolve().is_relative_to(self.service.db.files_dir.resolve()):
            raise ValueError("HTML 原件不属于当前资料库。")
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != row["size"]
                or not 0 < before.st_size <= 20 * 1024 * 1024
            ):
                raise ValueError("HTML 原件大小或文件类型无效。")
            raw = stream.read(before.st_size + 1)
            after = os.fstat(stream.fileno())
            if (
                len(raw) != before.st_size
                or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)
                or _sha(raw) != row["content_hash"]
                or manifest.get("source_hash") != row["content_hash"]
            ):
                raise ValueError("HTML 原件或解析来源 hash 已变化。")
        return row, enriched, manifest, raw, aliases[0] if aliases else row["source_url"], aliases

    def processing_candidates(self, owner, documents):
        """Independently validate draft HTML processing, including queued resumes."""
        eligible, rejected = [], []
        for document in documents:
            try:
                _row, _enriched, manifest, raw, _source, _aliases = self._snapshot(
                    owner,
                    document,
                    allow_processing=True,
                )
                image_candidates_bounded(raw, manifest)
                eligible.append(document)
            except Exception as error:
                rejected.append({"document_id": document, "error_type": type(error).__name__})
        return eligible, rejected

    def _prune_bound_receipts(self, rows, owner, document):
        # A committed binding carries the immutable HTTP receipt CAS. Remove
        # only its matching active checkpoint; failed and unbound downloads stay.
        with self.lock:
            removable = []
            for key, receipt in self.state["images"].items():
                if receipt.get("status") != "downloaded":
                    continue
                identity = receipt.get("identity") or {}
                for row in rows:
                    if (
                        identity.get("owner_id") == owner
                        and identity.get("document_id") == document
                        and row.get("source_hash") == identity.get("source_hash")
                        and row.get("object_key") == identity.get("object_key")
                        and row.get("locator") == identity.get("locator")
                        and row.get("original_image_blob") == receipt.get("image_blob")
                        and row.get("download_receipt_blob", {}).get("content_hash")
                        == _sha(dump(receipt).encode())
                    ):
                        removable.append(key)
                        break
            if removable:
                for key in removable:
                    del self.state["images"][key]
                self.checkpoint(deepcopy(self.state))

    def _image(self, owner, row, source_url, obj, deadline, *, allowed_hosts=None):
        from app.shared.infrastructure.http import download as ingestion_download

        request = resolved_url(source_url, obj["url_attributes"][0]["decoded_value"])
        required_host = None
        if allowed_hosts is not None:
            required_host = ingestion_download._validated_url(request)[1]
            if required_host not in allowed_hosts:
                raise ValueError("HTML 图片不属于来源清单批准的精确主机。")
            from app.contexts.ingestion.infrastructure.collection_proxy import proxy_for_host

            if proxy_for_host(required_host) is None:
                raise ValueError("官方 HTML 图片主机未配置项目采集代理。")
        identity = {
            "owner_id": owner,
            "document_id": row["id"],
            "source_hash": row["content_hash"],
            "source_version": row["version"],
            "source_url": source_url,
            "object_key": obj["object_key"],
            "locator": obj["locator"],
            "attribute": obj["url_attributes"][0],
            "requested_url": request,
        }
        key = _sha(dump(identity).encode())
        previous = deepcopy(self.state["images"].get(key, {}))
        completed = self.receipts.read(owner, key)
        if completed is not None:
            if completed.get("identity") != identity or completed.get("status") != "downloaded":
                raise ValueError("独立图片下载收据与实际原文不一致。")
            previous = completed
        if previous.get("status") == "downloaded":
            if previous.get("identity") != identity:
                raise ValueError("网页图片检查点与实际原文不一致。")
            read_image_blob(self.service.db.data_dir, owner, previous["image_blob"])
            self.receipts.store(owner, key, previous)
            self._save(key, previous)
            return {**previous, "reused_transfer": True}
        if previous.get("status") == "request_intent" or (
            not previous
            and any(self.receipts.read(owner, key, kind=f"intent-{attempt}") for attempt in (1, 2, 3))
        ):
            raise HtmlTransferUnconfirmed("图片请求中断且没有可验证完成收据，需核对原下载状态。")
        attempts = previous.get("attempts", 0)
        if type(attempts) is not int or attempts < 0 or attempts >= 3:
            raise ValueError("图片下载已达到三次自动尝试限制。")
        receipt = {
            "identity": identity,
            "status": "request_intent",
            "attempts": attempts + 1,
            "requested_at": now(),
        }
        with self.lock:
            if self.reserved_requests >= self.max_requests:
                raise ValueError("网页图片下载尝试预算已耗尽。")
            self.reserved_requests += 1
        self.receipts.store(owner, key, receipt, kind=f"intent-{attempts + 1}")
        self._save(key, receipt)
        request_deadline = min(deadline, time.monotonic() + 90)
        try:
            with self.lock:
                self.request_attempts += 1
            fetched = ingestion_download._download(
                request,
                deadline=request_deadline,
                required_host=required_host,
                max_bytes=MAX_IMAGE_BYTES,
                consume=lambda chunks: self.service.archive.blobs.put(owner, chunks),
                connection_slot=lambda host: self.service.collection.network_slot(host, request_deadline),
            )
            blob = fetched["blob"]
            reference = {
                "content_hash": blob.digest,
                "size": blob.size,
                "storage_path": str(blob.path.relative_to(self.service.db.data_dir.resolve())),
                "mime_type": fetched["mime_type"],
            }
            receipt.update(
                completed_at=now(),
                image_blob=reference,
                final_url=fetched["url"],
                http={
                    field: fetched.get(field)
                    for field in (
                        "mime_type",
                        "response_status",
                        "etag",
                        "last_modified",
                        "content_encoding",
                        "transfer_bytes",
                        "request_method",
                    )
                },
            )
            if fetched["mime_type"] not in {
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
                "application/octet-stream",
                "binary/octet-stream",
            }:
                raise ValueError("图片网址返回了其他文件类型。")
            read_image_blob(self.service.db.data_dir, owner, reference)
            receipt["status"] = "downloaded"
        except Exception as error:
            receipt.update(status="failed", error_type=type(error).__name__)
            delay = getattr(error, "retry_after", None)
            if delay:
                with self.service.db.transaction() as conn:
                    host = (
                        getattr(error, "response_host", None) or ingestion_download._validated_url(request)[1]
                    )
                    conn.execute(
                        "INSERT INTO collection_host_cooldowns VALUES(?,?) ON CONFLICT(host) DO UPDATE SET available_at=MAX(available_at,excluded.available_at)",
                        (host, time.time() + delay),
                    )
            self._save(key, receipt)
            raise
        # Checkpoint failures are not HTTP failures. Keep the downloaded
        # receipt and CAS reusable; never overwrite it with a retryable status.
        self.receipts.store(owner, key, receipt)
        self._save(key, receipt)
        return {**receipt, "reused_transfer": False}

    def prepare(
        self,
        owner,
        document,
        *,
        deadline,
        max_images=4,
        official_source=False,
        active_claim=None,
        allowed_asset_hosts=None,
    ):
        if type(max_images) is not int or not 1 <= max_images <= 20:
            raise ValueError("每份网页一次采集一至二十张正文图。")
        if active_claim is not None and not official_source:
            raise ValueError("活动处理租约只能用于受限的官方来源图片采集。")
        if allowed_asset_hosts is not None and (
            not isinstance(allowed_asset_hosts, (list, tuple, set, frozenset))
            or len(allowed_asset_hosts) > 16
            or any(not isinstance(host, str) or not host for host in allowed_asset_hosts)
        ):
            raise ValueError("官方 HTML 图片主机清单无效。")
        if time.monotonic() >= deadline:
            raise ValueError("网页图片采集预算已耗尽。")
        row, enriched, manifest, raw, source_url, aliases = self._snapshot(
            owner,
            document,
            allow_processing=active_claim is not None,
            official_source=official_source,
            active_claim=active_claim,
        )
        approved_hosts = None
        if allowed_asset_hosts is not None:
            from app.shared.infrastructure.http.download import _validated_url

            approved_hosts = set()
            parent_host = _validated_url(source_url)[1]
            for host in allowed_asset_hosts:
                normalized, parsed_host = _validated_url(f"https://{host}/")
                if (
                    normalized != f"https://{host}/"
                    or parsed_host != host
                    or not host.endswith((".gov.sg", ".go.id"))
                    or host == parent_host
                ):
                    raise ValueError("官方 HTML 图片主机必须是与正文主机分离的精确政府域名。")
                approved_hosts.add(parsed_host)
            approved_hosts.add(parent_host)
            if allowed_asset_hosts and os.getenv("COLLECTION_PROXY_URL"):
                proxy_hosts = {
                    host.strip().lower()
                    for host in os.getenv("COLLECTION_PROXY_HOSTS", "").split(",")
                    if host.strip()
                }
                proxy_hosts.update(allowed_asset_hosts)
                if len(proxy_hosts) > 64:
                    raise ValueError("官方 HTML 图片代理范围超过 64 个精确主机。")
                os.environ["COLLECTION_PROXY_HOSTS"] = ",".join(sorted(proxy_hosts))
        candidates = image_candidates_bounded(raw, manifest)
        bound = linked_image_references(
            manifest,
            source_url=row["source_url"],
            allowed_source_urls=aliases,
        )
        for value in bound:
            read_image_blob(self.service.db.data_dir, owner, value["original_image_blob"])
            if value.get("download_receipt_blob"):
                read_image_blob(self.service.db.data_dir, owner, value["download_receipt_blob"])
        self._prune_bound_receipts(bound, owner, document)
        existing = {value["object_key"] for value in manifest.get("html_source", {}).get("linked_images", [])}
        eligible = [obj for obj in candidates["eligible"] if obj["object_key"] not in existing]
        errors = []
        if approved_hosts is not None:
            from app.shared.infrastructure.http.download import _validated_url

            permitted = []
            for obj in eligible:
                request = resolved_url(source_url, obj["url_attributes"][0]["decoded_value"])
                host = _validated_url(request)[1]
                if host in approved_hosts:
                    permitted.append(obj)
                else:
                    errors.append({"object_key": obj["object_key"], "error_type": "HostNotApproved"})
            eligible = permitted
        selected = eligible[:max_images]
        results = []
        if selected:
            with ThreadPoolExecutor(
                max_workers=min(2, len(selected)), thread_name_prefix="html-image-download"
            ) as pool:
                futures = [
                    pool.submit(
                        self._image,
                        owner,
                        row,
                        source_url,
                        obj,
                        deadline,
                        allowed_hosts=approved_hosts,
                    )
                    for obj in selected
                ]
                for obj, future in zip(selected, futures, strict=True):
                    try:
                        results.append((obj, future.result()))
                    except Exception as error:
                        errors.append({"object_key": obj["object_key"], "error_type": type(error).__name__})
        updated = deepcopy(manifest)
        total = 0
        bindings = []
        for obj, result in results:
            if time.monotonic() >= deadline or total + result["image_blob"]["size"] > MAX_TOTAL_IMAGE_BYTES:
                errors.append({"object_key": obj["object_key"], "error_type": "PreparationBudgetReached"})
                continue
            try:
                next_manifest = bind_linked_image_bounded(
                    raw,
                    updated,
                    object_key=obj["object_key"],
                    source_url=source_url,
                    image=read_image_blob(self.service.db.data_dir, owner, result["image_blob"]),
                    image_blob=result["image_blob"],
                    fetched_url=result["final_url"],
                    verify_pixels=True,
                )
            except ValueError as error:
                errors.append({"object_key": obj["object_key"], "error_type": type(error).__name__})
                continue
            receipt = {key: value for key, value in result.items() if key != "reused_transfer"}
            receipt_blob = self.service.archive.blobs.put_bytes(owner, dump(receipt).encode())
            next_manifest["html_source"]["linked_images"][-1]["download_receipt_blob"] = {
                "content_hash": receipt_blob.digest,
                "size": receipt_blob.size,
                "storage_path": str(receipt_blob.path.relative_to(self.service.db.data_dir.resolve())),
                "mime_type": "application/json",
            }
            updated = next_manifest
            total += result["image_blob"]["size"]
            bindings.append(
                {
                    "object_key": obj["object_key"],
                    "image_blob": result["image_blob"],
                    "reused_transfer": result["reused_transfer"],
                    "model_called": False,
                }
            )
        if bindings:
            with self.service.db.transaction() as conn:
                if active_claim is not None:
                    if not self.service.collection._valid_claim(conn, active_claim, True):
                        raise ValueError("图片下载期间官方处理租约已变化；原图片检查点已保留。")
                current = dict(self.service.own_document(conn, owner, document))
                current_enriched = self.service.document_dict(current, conn=conn)
                if (
                    any(
                        current[field] != row[field]
                        for field in ("manifest", "content_hash", "version", "source_url", "role")
                    )
                    or current_enriched["metadata_revision"] != enriched["metadata_revision"]
                    or current_enriched["classifications"] != enriched["classifications"]
                    or (
                        not official_source
                        and current_enriched["metadata"].get("review_status") != "draft"
                    )
                    or (
                        active_claim is None
                        and conn.execute(
                            "SELECT 1 FROM processing_jobs WHERE document_id=? AND status IN ('queued','processing')",
                            (document,),
                        ).fetchone()
                    )
                ):
                    raise ValueError("图片下载期间文档或任务已变化；原图片检查点已保留，未覆盖当前正文。")
                conn.execute(
                    "UPDATE documents SET manifest=?,status='processing' WHERE id=? AND owner_id=?",
                    (dump(updated), document, owner),
                )
                if active_claim is None:
                    self.service.collection.queue_processing(conn, document, owner)
                    conn.execute(
                        "UPDATE processing_jobs SET phase='parse' WHERE document_id=? AND owner_id=? AND status='queued'",
                        (document, owner),
                    )
            self._prune_bound_receipts(updated["html_source"]["linked_images"], owner, document)
        remaining = len(candidates["eligible"]) - len(existing) - len(bindings)
        return {
            "document_id": document,
            "source_hash": row["content_hash"],
            "source_version": row["version"],
            "bound_images": bindings,
            "already_bound_images": len(existing),
            "remaining_eligible_images": max(0, remaining),
            "unresolved_objects": candidates["unresolved"],
            "errors": errors,
            "model_calls": 0,
            "queue_processing": bool(bindings) and active_claim is None,
            "formal_rag_enabled": False,
        }
