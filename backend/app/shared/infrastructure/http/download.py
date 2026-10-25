"""Bounded public HTTPS transport with pinned DNS and streaming limits."""

from __future__ import annotations

import http.client
import ipaddress
import json
import mimetypes
import os
import queue
import re
import socket
import ssl
import threading
import time
import zlib
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from app.contexts.documents.infrastructure.parsers.html_parsing import HTMLDocument as _HTMLDocument
from app.shared.domain.content import MAX_TEXT, _decode
from app.shared.infrastructure.http.public_search import EGazetteSearchRequest

MAX_BYTES = 20 * 1024 * 1024


MAX_ORIGINAL_BYTES = 300 * 1024 * 1024


DEFAULT_COMPRESSED_HTML_BYTES = 4 * 1024 * 1024
MAX_COMPRESSED_HTML_BYTES = 6 * 1024 * 1024


_DNS_SLOTS = threading.BoundedSemaphore(8)


_DIRECT_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md", ".xlsx", ".xls", ".csv"}


_MIME_EXTENSIONS = {
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.ms-excel": ".xls",
    "application/x-msaccess": ".mdb",
    "application/vnd.ms-access": ".mdb",
    "application/msaccess": ".mdb",
    "application/msword": ".doc",
    "application/vnd.wordperfect": ".wpf",
    "application/wordperfect": ".wpf",
}


class DownloadError(ValueError):
    """A bounded collection failure; the durable job runner owns retry policy."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after: float | None = None,
        response_host: str | None = None,
        bytes_received: int | None = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after if retryable else None
        self.response_host = response_host
        self.bytes_received = bytes_received


def _validated_url(url: str) -> tuple[str, str]:
    if not isinstance(url, str) or len(url) > 4096 or re.search(r"[\x00-\x20\x7f]", url):
        raise DownloadError("抓取 URL 无效。")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise DownloadError("抓取仅支持不含凭据的标准端口 HTTPS URL。")
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError("抓取 URL 无效。") from exc
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise DownloadError("抓取地址必须是 public 公网地址。")
    netloc = f"[{host}]" if ":" in host else host
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="=&%/:?@!$'()*+,;~-._")
    return urlunsplit(("https", netloc, path, query, "")), host


def _public_addresses(host: str, *, deadline: float | None = None) -> list[tuple]:
    # System DNS has no Python timeout. Bound waiting and outstanding resolver
    # threads so a resolver outage cannot exhaust application worker threads.
    remaining = min(10.0, (deadline or time.monotonic() + 10) - time.monotonic())
    if remaining <= 0 or not _DNS_SLOTS.acquire(timeout=max(0, remaining)):
        raise DownloadError("DNS 解析超过时间限制或队列已满。", retryable=True)
    result: queue.Queue = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except Exception as exc:
            result.put(exc)
        finally:
            _DNS_SLOTS.release()

    threading.Thread(target=resolve, name="tea-source-dns", daemon=True).start()
    try:
        addresses = result.get(
            timeout=max(0, min(10.0, (deadline or time.monotonic() + 10) - time.monotonic()))
        )
    except queue.Empty as exc:
        raise DownloadError("DNS 解析超过时间限制。", retryable=True) from exc
    if isinstance(addresses, Exception):
        raise DownloadError("抓取来源域名无法解析。", retryable=True) from addresses
    if not addresses:
        raise DownloadError("抓取来源没有可用地址。", retryable=True)
    for address in addresses:
        if not ipaddress.ip_address(address[4][0]).is_global:
            raise DownloadError("抓取来源的所有 DNS 地址必须是 public 公网地址。")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect exactly to a validated address; TLS still authenticates the hostname."""

    def __init__(self, host: str, address: tuple, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address
        self.alternate_addresses: list[tuple] = []
        self.active_socket = None
        self.deadline: float | None = None
        self.deadline_expired = threading.Event()

    def _remaining_timeout(self) -> float:
        remaining = self.timeout if self.deadline is None else self.deadline - time.monotonic()
        if self.deadline_expired.is_set() or remaining <= 0:
            raise TimeoutError("Collection deadline expired")
        return min(self.timeout, remaining)

    def connect(self) -> None:
        self._remaining_timeout()
        connect_deadline = time.monotonic() + self._remaining_timeout()
        addresses = [self.address, *self.alternate_addresses]
        failure = None
        for family, socket_type, protocol, _, destination in addresses:
            raw = tls = None
            connected = False

            def timeout():
                remaining = min(self._remaining_timeout(), connect_deadline - time.monotonic(), 5.0)
                if remaining <= 0:
                    raise TimeoutError("Collection connection deadline expired")
                return remaining

            try:
                timeout()
                raw = socket.socket(family, socket_type, protocol)
                # Only one physical socket is active at a time; every retry
                # stays inside the same host lease and absolute watchdog.
                self.active_socket = raw
                raw.settimeout(timeout())
                raw.connect(destination)
                raw.settimeout(timeout())
                tls = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
                self.sock = self.active_socket = tls
                tls.settimeout(timeout())
                tls.do_handshake()
                tls.settimeout(self._remaining_timeout())
                connected = True
                return
            except OSError as exc:
                failure = exc
            finally:
                if not connected:
                    if tls is not None:
                        tls.close()
                    if raw is not None:
                        raw.close()
                    self.sock = self.active_socket = None
        if failure is not None:
            raise failure
        raise TimeoutError("No validated address connected before deadline")


def _bounded_header(value: str | None, limit: int = 1024) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(ord(char) < 32 or ord(char) == 127 or ord(char) > 255 for char in value):
        return None
    return value


def _retry_after(value: str | None) -> float | None:
    """RFC 9110 section 10.2.3: retain the server's minimum retry delay.

    The durable queue schedules long delays without occupying a worker. The
    header bound keeps decimal conversion finite; truncating the delay would
    issue requests before the server's stated recovery time.
    """
    value = _bounded_header(value, 128)
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def _download(
    url: str,
    *,
    deadline: float,
    required_host: str | None = None,
    consume: Callable[[Iterable[bytes]], object] | None = None,
    validators: dict[str, str] | None = None,
    connection_slot: Callable[[str], AbstractContextManager] | None = None,
    accept_compressed_html: bool = False,
    accept_compressed_json: bool = False,
    max_compressed_html_bytes: int | None = None,
    json_body: dict | None = None,
    public_search: EGazetteSearchRequest | None = None,
    max_bytes: int | None = None,
    byte_range: tuple[int, int] | None = None,
    range_validator: str | None = None,
    range_total_bytes: int | None = None,
    on_headers: Callable[[dict], None] | None = None,
) -> dict:
    """Read a bounded HTTPS resource, optionally into an atomic blob consumer.

    A consumer must exhaust the iterable and only publish after normal completion.
    A 304 does not invoke it: callers must validate their cached original and URL.
    Conditional headers belong exclusively to the initial requested resource.
    A connection slot accounts for the actual host at every redirect hop.
    Public search credentials are bound to one verified read-only endpoint;
    they never enter a redirect or an arbitrary caller-provided header mapping.
    """
    current = url
    validators = validators or {}
    search_headers = {}
    if public_search is not None:
        if type(public_search) is not EGazetteSearchRequest or json_body is None or validators or byte_range:
            raise DownloadError("公开检索只允许显式受限的只读 JSON 请求。")
        try:
            search_headers = public_search.headers_for(url, method="POST")
        except ValueError as exc:
            raise DownloadError(str(exc)) from exc
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_ORIGINAL_BYTES:
        raise DownloadError("原件传输额度无效，最多 300 MiB。")
    max_compressed_html_bytes = (
        min(DEFAULT_COMPRESSED_HTML_BYTES, MAX_COMPRESSED_HTML_BYTES)
        if max_compressed_html_bytes is None
        else max_compressed_html_bytes
    )
    if type(max_compressed_html_bytes) is not int or not 1 <= max_compressed_html_bytes <= MAX_COMPRESSED_HTML_BYTES:
        raise DownloadError("压缩目录传输额度无效，最多 6 MiB。")
    if range_total_bytes is not None and (
        byte_range is None
        or type(range_total_bytes) is not int
        or not 1 <= range_total_bytes <= 1024 * 1024 * 1024
    ):
        raise DownloadError("专用数据集必须指定有效分段及不超过 1 GB 的固定总大小。")
    if byte_range is not None:
        if (
            not isinstance(byte_range, tuple)
            or len(byte_range) != 2
            or any(type(value) is not int for value in byte_range)
            or not 0 <= byte_range[0] <= byte_range[1] < (range_total_bytes or max_bytes)
            or (range_total_bytes is not None and byte_range[1] - byte_range[0] + 1 > max_bytes)
            or validators
            or json_body is not None
            or accept_compressed_html
            or accept_compressed_json
        ):
            raise DownloadError("原件分段请求参数无效。")
        if range_validator is not None and not re.fullmatch(r'"[\x21\x23-\x7e]{0,1020}"', range_validator):
            raise DownloadError("分段原件需要有效的强版本标识。")
    elif range_validator is not None:
        raise DownloadError("原件分段校验参数缺少范围。")
    request_body = None
    if json_body is not None:
        if not isinstance(json_body, dict) or validators:
            raise DownloadError("目录 JSON 请求参数无效。")
        try:
            request_body = json.dumps(json_body, ensure_ascii=False, allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError) as exc:
            raise DownloadError("目录 JSON 请求参数无效。") from exc
        if len(request_body) > 16384:
            raise DownloadError("目录 JSON 请求超过 16 KB。")
    conditionals = {
        "If-None-Match": _bounded_header(validators.get("etag") or validators.get("ETag")),
        "If-Modified-Since": _bounded_header(
            validators.get("last_modified") or validators.get("Last-Modified"), 128
        ),
    }
    for redirect in range(4):
        current, host = _validated_url(current)
        if required_host is not None and host != required_host:
            raise DownloadError("关联附件重定向到了不同来源，已拒绝抓取。")
        # Some official domains advertise IPv6 without a reachable route from
        # this host. All answers are validated before ordering or connecting.
        proxy_enabled = any(
            os.getenv(key)
            for key in ("COLLECTION_PROXY_URL", "COLLECTION_PROXY_HOSTS", "COLLECTION_DOH_HOSTS")
        )
        proxy = None
        if proxy_enabled:
            from app.contexts.ingestion.infrastructure.collection_proxy import (
                PinnedProxyHTTPSConnection,
                proxy_for_host,
                resolve_addresses,
            )

            proxy = proxy_for_host(host)
            if proxy is None:
                raise DownloadError("当前来源域名不在采集代理白名单中。")
            addresses = resolve_addresses(host, deadline=deadline, fallback=_public_addresses)
        else:
            addresses = _public_addresses(host, deadline=deadline)
        addresses = sorted(addresses, key=lambda row: row[0] != socket.AF_INET)
        with connection_slot(host) if connection_slot else nullcontext():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DownloadError("抓取超过总时间限制。", retryable=True)
            connection = (
                PinnedProxyHTTPSConnection(host, addresses[0], min(15.0, remaining), proxy=proxy)
                if proxy is not None
                else _PinnedHTTPSConnection(host, addresses[0], min(15.0, remaining))
            )
            connection.alternate_addresses = addresses[1:]
            connection.deadline = deadline
            expired = threading.Event()
            connection.deadline_expired = expired

            def expire_connection(connection=connection, expired=expired) -> None:
                # Bind this hop's socket and flag: a late timer from an earlier
                # redirect must never close the next hop's connection.
                expired.set()
                active = getattr(connection, "active_socket", None)
                if active is not None:
                    try:
                        active.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        active.close()
                    except OSError:
                        pass

            expiry = threading.Timer(max(0, deadline - time.monotonic()), expire_connection)
            expiry.daemon = True
            expiry.start()
            response = None
            try:
                if expired.is_set() or deadline <= time.monotonic():
                    raise DownloadError("抓取超过总时间限制。", retryable=True)
                parsed = urlsplit(current)
                headers = {
                    "User-Agent": "AItea/0.1 SourceCollector",
                    "Accept-Encoding": "gzip, identity"
                    if accept_compressed_html or accept_compressed_json
                    else "identity",
                    "Accept": "application/pdf,text/html,application/vnd.openxmlformats-officedocument.wordprocessingml.document,text/plain,*/*;q=0.5",
                }
                from app.contexts.ingestion.infrastructure.source_adapters.official_source_recovery import (
                    request_headers_for,
                )

                headers.update(request_headers_for(host))
                headers.update({key: value for key, value in conditionals.items() if value is not None})
                headers.update(search_headers)
                if byte_range is not None:
                    headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
                    if range_validator is not None:
                        headers["If-Range"] = range_validator
                request_path = parsed.path + ("?" + parsed.query if parsed.query else "")
                if request_body is None:
                    connection.request("GET", request_path, headers=headers)
                else:
                    headers["Content-Type"] = "application/json"
                    connection.request("POST", request_path, headers=headers, body=request_body)
                remaining = deadline - time.monotonic()
                if expired.is_set() or remaining <= 0:
                    raise DownloadError("抓取超过总时间限制。", retryable=True)
                if getattr(connection, "sock", None):
                    connection.sock.settimeout(min(15.0, remaining))
                response = connection.getresponse()
                if expired.is_set() or deadline <= time.monotonic():
                    raise DownloadError("抓取超过总时间限制。", retryable=True)
                if response.status in {301, 302, 303, 307, 308}:
                    if request_body is not None:
                        raise DownloadError("目录 JSON 请求不允许重定向。")
                    location = response.getheader("Location")
                    if not location or redirect == 3:
                        raise DownloadError("抓取来源重定向过多或地址缺失。")
                    # Even on the same host, another path represents a different
                    # cached resource. Do not leak or reuse the original validators.
                    conditionals = {}
                    range_validator = None
                    current = urljoin(current, location)
                    continue
                response_validators = {
                    "etag": _bounded_header(response.getheader("ETag")),
                    "last_modified": _bounded_header(response.getheader("Last-Modified"), 128),
                }
                if response.status == 304:
                    if not (headers.get("If-None-Match") or headers.get("If-Modified-Since")):
                        raise DownloadError("来源对无条件请求返回 304，无法校验本地原件。")
                    return {"url": current, "not_modified": True, **response_validators}
                if not 200 <= response.status < 300:
                    retryable = response.status in {408, 429} or 500 <= response.status <= 599
                    raise DownloadError(
                        f"抓取来源返回 HTTP {response.status}。",
                        retryable=retryable,
                        retry_after=_retry_after(response.getheader("Retry-After")) if retryable else None,
                        response_host=host,
                    )
                if response.status == 206 and byte_range is None:
                    raise DownloadError("抓取来源返回了不完整的分段响应。")
                if range_total_bytes is not None and response.status != 206:
                    raise DownloadError("专用数据集必须返回与固定总大小一致的分段响应。")
                encoding = response.getheader("Content-Encoding", "identity").strip().lower()
                compressed = (accept_compressed_html or accept_compressed_json) and encoding == "gzip"
                if encoding not in {"identity", ""} and not compressed:
                    raise DownloadError("抓取来源返回压缩传输，无法安全校验大小。")
                transfer_limit = min(max_bytes, max_compressed_html_bytes) if compressed else max_bytes
                length = response.getheader("Content-Length")
                if length is not None and (
                    not re.fullmatch(r"[0-9]{1,12}", length.strip()) or int(length) > transfer_limit
                ):
                    raise DownloadError("抓取文件大小无效或超过配置的原件额度。")
                expected_length = int(length) if length is not None else None
                range_start = range_end = None
                total_size = expected_length
                if response.status == 206:
                    match = re.fullmatch(
                        r"bytes (\d{1,12})-(\d{1,12})/(\d{1,12})", response.getheader("Content-Range", "")
                    )
                    if match is None:
                        raise DownloadError("分段响应缺少有效的完整文件范围。")
                    range_start, range_end, total_size = map(int, match.groups())
                    if (
                        not 0 <= range_start <= range_end < total_size <= (range_total_bytes or max_bytes)
                        or (range_total_bytes is not None and total_size != range_total_bytes)
                        or range_start != byte_range[0]
                        or range_end != min(byte_range[1], total_size - 1)
                        or (expected_length is not None and expected_length != range_end - range_start + 1)
                    ):
                        raise DownloadError("分段响应范围或总大小与请求不一致。")
                    expected_length = range_end - range_start + 1
                    transfer_limit = expected_length
                mime = (
                    response.getheader("Content-Type", "application/octet-stream")
                    .split(";")[0]
                    .strip()
                    .lower()
                )
                if len(mime) > 255 or not re.fullmatch(r"[a-z0-9!#$&^_.+*-]+/[a-z0-9!#$&^_.+*-]+", mime):
                    raise DownloadError("抓取来源返回无效的文件类型。")
                if compressed and not (
                    (
                        accept_compressed_html
                        and mime in {"text/html", "application/xhtml+xml", "text/xml", "application/xml"}
                    )
                    or (accept_compressed_json and mime == "application/json")
                ):
                    raise DownloadError("仅清单目录或详情 HTML/JSON/XML 允许受限压缩传输。")
                name = Path(unquote(parsed.path)).name or "source"
                disposition = _bounded_header(response.getheader("Content-Disposition"), 4096)
                if disposition:
                    try:
                        message = EmailMessage()
                        message["Content-Disposition"] = disposition
                        declared_name = message.get_filename()
                        if isinstance(declared_name, str) and declared_name.strip():
                            name = Path(declared_name.replace("\\", "/")).name or name
                    except (ValueError, LookupError):
                        pass  # A malformed display header cannot change stored bytes.
                name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", name)[:180]
                if not Path(name).suffix or (
                    mime in _MIME_EXTENSIONS
                    and Path(name).suffix.lower() != _MIME_EXTENSIONS[mime]
                    and not (mime == "text/html" and Path(name).suffix.lower() == ".htm")
                ):
                    name += _MIME_EXTENSIONS.get(mime, mimetypes.guess_extension(mime) or ".bin")
                exhausted = False
                transfer_bytes = 0
                metadata = {
                    "url": current,
                    "filename": name,
                    "mime_type": mime,
                    "content_encoding": encoding or "identity",
                    "request_method": "POST" if request_body is not None else "GET",
                    "response_status": response.status,
                    "total_size": total_size,
                    "range_start": range_start,
                    "range_end": range_end,
                    "accept_ranges": response.getheader("Accept-Ranges", "").strip().lower() == "bytes",
                    **response_validators,
                }
                if on_headers is not None:
                    on_headers(metadata.copy())

                def chunks() -> Iterable[bytes]:
                    nonlocal exhausted, transfer_bytes
                    size = decoded_size = 0
                    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if compressed else None
                    while True:
                        remaining = deadline - time.monotonic()
                        if expired.is_set() or remaining <= 0:
                            raise DownloadError("抓取超过总时间限制。", retryable=True)
                        if connection.sock:
                            connection.sock.settimeout(min(15.0, remaining))
                        block = response.read1(min(65536, transfer_limit + 1 - size))
                        if expired.is_set() or deadline <= time.monotonic():
                            raise DownloadError("抓取超过总时间限制。", retryable=True)
                        if not block:
                            break
                        size += len(block)
                        if size > transfer_limit:
                            raise DownloadError("抓取文件超过传输大小限制。")
                        if expected_length is not None and size > expected_length:
                            raise DownloadError("抓取文件长度与响应头不一致。", retryable=True)
                        if decoder is None:
                            yield block
                        else:
                            # Bound each allocation and total decoded bytes; never call
                            # flush(), whose length is an initial buffer, not a limit.
                            pending = block
                            while True:
                                if expired.is_set() or time.monotonic() >= deadline:
                                    raise DownloadError("抓取超过总时间限制。", retryable=True)
                                try:
                                    decoded = decoder.decompress(
                                        pending, min(65536, transfer_limit + 1 - decoded_size)
                                    )
                                except zlib.error as exc:
                                    raise DownloadError("压缩目录内容损坏。", retryable=True) from exc
                                decoded_size += len(decoded)
                                if decoded_size > transfer_limit:
                                    raise DownloadError("目录解压后超过 4 MB。")
                                if decoder.unused_data:
                                    raise DownloadError("压缩目录存在尾随或多段内容。")
                                if decoded:
                                    yield decoded
                                pending = decoder.unconsumed_tail
                                if not pending and (not decoded or decoder.eof):
                                    break
                    # These checks run before the consumer can atomically publish.
                    if expected_length is not None and size != expected_length:
                        raise DownloadError("抓取文件传输不完整。", retryable=True)
                    if not size:
                        raise DownloadError("抓取文件为空。")
                    if decoder is not None and (not decoder.eof or not decoded_size):
                        raise DownloadError("压缩目录传输不完整或正文为空。", retryable=True)
                    transfer_bytes = size
                    exhausted = True

                payload = consume(chunks()) if consume is not None else b"".join(chunks())
                if not exhausted:
                    raise DownloadError("文件存储未完整消费抓取数据。")
                return {
                    **metadata,
                    "transfer_bytes": transfer_bytes,
                    "blob" if consume is not None else "content": payload,
                }
            except (OSError, http.client.HTTPException) as exc:
                # If no response was received, no response-body bytes entered
                # the bounded downloader. Keep interrupted/partial responses
                # unmetered (None) so callers retain the full reservation.
                raise DownloadError(
                    "抓取连接失败或超时，请检查来源后重试。",
                    retryable=True,
                    bytes_received=0 if response is None else None,
                ) from exc
            finally:
                expiry.cancel()
                try:
                    # For Connection: close, HTTPConnection hands the socket
                    # to the response file. Close it before releasing this
                    # host's physical connection slot, including early exits.
                    if response is not None:
                        response.close()
                finally:
                    connection.close()
    raise DownloadError("抓取重定向无法完成。")


def discover_attachments(url: str, content: bytes, mime_type: str, max_attachments: int = 5) -> list[str]:
    """Find direct file attachments without fetching them or inferring citations."""
    if (
        isinstance(max_attachments, bool)
        or not isinstance(max_attachments, int)
        or not 0 <= max_attachments <= 5
    ):
        raise ValueError("直接附件数量必须为 0 至 5。")
    if mime_type != "text/html" or not max_attachments or len(content) > MAX_TEXT:
        return []
    current, host = _validated_url(url)
    extractor = _HTMLDocument()
    extractor.feed(_decode(content))
    extractor.finish()
    candidates: list[str] = []
    seen: set[str] = set()
    for link in extractor.links:
        try:
            candidate, candidate_host = _validated_url(urljoin(current, link))
        except ValueError:
            continue
        if (
            candidate_host != host
            or Path(unquote(urlsplit(candidate).path)).suffix.lower() not in _DIRECT_EXTENSIONS
            or candidate in seen
        ):
            continue
        seen.add(candidate)
        candidates.append(candidate)
        if len(candidates) >= max_attachments:
            break
    return candidates


def fetch_source(url: str, *, max_attachments: int = 5) -> dict:
    if not isinstance(max_attachments, int) or not 0 <= max_attachments <= 5:
        raise ValueError("直接附件数量必须为 0 至 5。")
    deadline = time.monotonic() + 90.0
    primary = _download(url, deadline=deadline)
    attachments = []
    warnings: list[str] = []
    if primary["mime_type"] == "text/html" and max_attachments:
        if len(primary["content"]) > MAX_TEXT:
            return {
                **primary,
                "attachments": [],
                "warnings": ["HTML 超过附件发现大小限制，已仅保存主文件。"],
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
        host = urlsplit(primary["url"]).hostname
        for candidate in discover_attachments(
            primary["url"], primary["content"], primary["mime_type"], max_attachments
        ):
            try:
                attachments.append(_download(candidate, deadline=deadline, required_host=host))
            except ValueError as exc:
                warnings.append(f"附件未抓取：{candidate}；{exc}")
    return {
        **primary,
        "attachments": attachments,
        "warnings": warnings,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
