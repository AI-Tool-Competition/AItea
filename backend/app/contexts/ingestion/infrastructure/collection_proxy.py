"""Explicit collection-only CONNECT transport and authenticated public DNS.

Ambient HTTP_PROXY variables are deliberately ignored. Each redirected target
must opt in by its exact hostname, still resolve to public numeric addresses,
and retain the original TLS hostname and certificate verification. Google DoH
is optional and uses its published IPs, not a proxy's unchecked remote DNS.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import re
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import unquote, urlencode, urlsplit

from app.configuration.paths import InstancePaths
from app.shared.infrastructure.http.download import DownloadError, _PinnedHTTPSConnection

_DOH_IPS = ("8.8.8.8", "8.8.4.4")
_DOH_SLOTS = threading.BoundedSemaphore(8)
_CACHE_LOCK = threading.Lock()
_DNS_CACHE: OrderedDict = OrderedDict()


def _canonical_host(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 253:
        raise DownloadError("采集代理需要有效的精确来源域名。")
    try:
        host = value.encode("idna").decode("ascii").lower().rstrip(".")
        address = ipaddress.ip_address(host)
    except UnicodeError as exc:
        raise DownloadError("采集代理来源域名无效。") from exc
    except ValueError:
        if (
            not host
            or len(host) > 253
            or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")
            )
        ):
            raise DownloadError("采集代理来源必须是精确域名，不支持通配符、端口或 URL。") from None
    else:
        if not address.is_global:
            raise DownloadError("采集代理目标必须是 public 公网地址。")
    return host


def _hosts(value: str) -> frozenset[str]:
    if len(value) > 8192:
        raise DownloadError("采集代理域名清单超过限制。")
    entries = value.split(",") if value else []
    if len(entries) > 64:
        raise DownloadError("采集代理域名清单最多允许 64 个精确域名。")
    return frozenset(_canonical_host(entry.strip()) for entry in entries)


def _catalog_proxy_hosts() -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Read the versioned, non-secret proxy scope from the active catalog root."""
    path = InstancePaths.resolve().catalogs / "collection-proxy.json"
    try:
        if path.stat().st_size > 64 * 1024:
            raise DownloadError("采集代理域名清单超过 64 KiB。")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except DownloadError:
        raise
    except FileNotFoundError:
        return frozenset(), frozenset(), frozenset()
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise DownloadError("采集代理域名清单无法读取。") from None
    if not isinstance(payload, dict) or payload.get("protocol") != "aitea-collection-proxy-v1":
        raise DownloadError("采集代理域名清单格式无效。")
    allowed, doh = payload.get("allowed_hosts"), payload.get("doh_hosts", [])
    browser = payload.get("browser_header_hosts", [])
    if (
        not isinstance(allowed, list)
        or not isinstance(doh, list)
        or not isinstance(browser, list)
        or any(not isinstance(value, str) for value in (*allowed, *doh, *browser))
        or any(not value or "," in value for value in browser)
    ):
        raise DownloadError("采集代理域名清单格式无效。")
    hosts, doh_hosts = _hosts(",".join(allowed)), _hosts(",".join(doh))
    browser_hosts = frozenset(_canonical_host(value) for value in browser)
    if (
        len(hosts) > 64
        or len(browser_hosts) > 64
        or not doh_hosts.issubset(hosts)
        or not browser_hosts.issubset(hosts)
    ):
        raise DownloadError("采集代理域名清单超出限制或浏览器请求头域名未获允许。")
    return hosts, doh_hosts, browser_hosts


@dataclass(frozen=True)
class ProxyEndpoint:
    address: str
    port: int
    authorization: str | None = field(default=None, repr=False)
    dns_over_https: bool = False


def proxy_for_host(host: str) -> ProxyEndpoint | None:
    """Read only backend operator configuration, never a request's proxy URL."""
    url = os.getenv("COLLECTION_PROXY_URL", "")
    host_list = os.getenv("COLLECTION_PROXY_HOSTS", "")
    doh_list = os.getenv("COLLECTION_DOH_HOSTS", "")
    if not (url or host_list or doh_list):
        return None
    catalog_hosts, catalog_doh_hosts, _ = _catalog_proxy_hosts()
    hosts = catalog_hosts | _hosts(host_list)
    doh_hosts = catalog_doh_hosts | _hosts(doh_list)
    if not url or not hosts:
        raise DownloadError("采集代理需要配置端点和受审阅的精确域名清单。")
    if len(hosts) > 64:
        raise DownloadError("采集代理域名清单最多允许 64 个精确域名。")
    if not doh_hosts.issubset(hosts):
        raise DownloadError("COLLECTION_DOH_HOSTS 必须属于已允许的采集代理域名。")
    try:
        if len(url) > 4096 or re.search(r"[\x00-\x20\x7f]", url):
            raise ValueError
        parsed = urlsplit(url)
        proxy_ip = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
        if (
            parsed.scheme != "http"
            or port is None
            or not 1 <= port <= 65535
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not (proxy_ip.is_global or proxy_ip.is_loopback)
        ):
            raise ValueError
        authorization = None
        if parsed.username is not None or parsed.password is not None:
            # CONNECT itself is plaintext; never send credentials over a remote
            # plaintext transport. Local relay authentication remains private.
            if not proxy_ip.is_loopback or parsed.username is None or parsed.password is None:
                raise ValueError
            user, password = unquote(parsed.username), unquote(parsed.password)
            if ":" in user or any(ord(char) < 32 or ord(char) == 127 for char in user + password):
                raise ValueError
            authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    except (ValueError, UnicodeError):
        # The exception message never includes the configured URL or credentials.
        raise DownloadError(
            "采集代理配置无效：使用数字公网或回环 IP 的 HTTP CONNECT 端点；认证仅允许回环代理。"
        ) from None
    host = _canonical_host(host)
    if host not in hosts:
        return None
    return ProxyEndpoint(str(proxy_ip), port, authorization, host in doh_hosts)


def _public_row(row: tuple) -> str:
    try:
        family, socket_type, protocol, _, destination = row
        address = ipaddress.ip_address(destination[0])
        if (
            family not in (socket.AF_INET, socket.AF_INET6)
            or family != (socket.AF_INET if address.version == 4 else socket.AF_INET6)
            or socket_type != socket.SOCK_STREAM
            or protocol not in (0, socket.IPPROTO_TCP)
            or destination[1] != 443
            or not address.is_global
            or (family == socket.AF_INET6 and (len(destination) != 4 or destination[2:] != (0, 0)))
            or (family == socket.AF_INET and len(destination) != 2)
        ):
            raise ValueError
    except (ValueError, TypeError, IndexError):
        raise DownloadError("采集代理目标必须是已校验的标准 HTTPS public 公网地址。") from None
    return str(address)


def _address_row(value: str) -> tuple:
    address = ipaddress.ip_address(value)
    return (
        socket.AF_INET if address.version == 4 else socket.AF_INET6,
        socket.SOCK_STREAM,
        socket.IPPROTO_TCP,
        "",
        (str(address), 443) if address.version == 4 else (str(address), 443, 0, 0),
    )


class PinnedProxyHTTPSConnection(_PinnedHTTPSConnection):
    """CONNECT to a public numeric IP, then authenticate the original hostname."""

    def __init__(self, host: str, address: tuple, timeout: float, *, proxy: ProxyEndpoint):
        _canonical_host(host)
        _public_row(address)
        super().__init__(host, address, timeout)
        self.proxy = proxy

    def connect(self) -> None:
        addresses = [self.address, *self.alternate_addresses]
        if not 1 <= len(addresses) <= 32:
            raise DownloadError("采集代理目标地址数量超过限制。")
        # Reject all non-public answers before dialing, including unused fallback
        # addresses. The proxy may never choose a hostname's target by itself.
        destinations = [_public_row(row) for row in addresses]
        connect_deadline = time.monotonic() + self._remaining_timeout()
        failure = None
        for destination in destinations:
            raw = tls = None
            connected = False

            def timeout() -> float:
                remaining = min(self._remaining_timeout(), connect_deadline - time.monotonic(), 5.0)
                if remaining <= 0:
                    raise TimeoutError("Collection proxy connection deadline expired")
                return remaining

            try:
                proxy_ip = ipaddress.ip_address(self.proxy.address)
                raw = socket.socket(socket.AF_INET if proxy_ip.version == 4 else socket.AF_INET6)
                self.active_socket = raw
                raw.settimeout(timeout())
                raw.connect((self.proxy.address, self.proxy.port))
                # A separate CONNECT object prevents http.client from replacing
                # the origin Host header with the tunnel's numeric IP later.
                tunnel = http.client.HTTPConnection(self.proxy.address, self.proxy.port, timeout=timeout())
                authority = f"[{destination}]:443" if ":" in destination else destination + ":443"
                tunnel_headers = {"Host": authority}
                if self.proxy.authorization:
                    tunnel_headers["Proxy-Authorization"] = self.proxy.authorization
                tunnel.set_tunnel(
                    destination,
                    443,
                    headers=tunnel_headers,
                )
                tunnel.sock = raw
                try:
                    tunnel._tunnel()
                except (OSError, http.client.HTTPException):
                    raise OSError("Collection proxy CONNECT failed") from None
                finally:
                    tunnel.sock = None
                raw.settimeout(timeout())
                tls = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
                self.sock = self.active_socket = tls
                tls.settimeout(timeout())
                tls.do_handshake()
                tls.settimeout(self._remaining_timeout())
                connected = True
                return
            except (OSError, http.client.HTTPException) as exc:
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
        raise TimeoutError("No validated proxy target connected before deadline")


def _abort(connection: PinnedProxyHTTPSConnection) -> None:
    connection.deadline_expired.set()
    active = connection.active_socket
    if active is not None:
        try:
            active.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        active.close()


def _query_dns(host: str, record_type: int, proxy: ProxyEndpoint, deadline: float) -> dict:
    connection = PinnedProxyHTTPSConnection(
        "dns.google",
        _address_row(_DOH_IPS[0]),
        min(10.0, max(0.001, deadline - time.monotonic())),
        proxy=proxy,
    )
    connection.alternate_addresses = [_address_row(_DOH_IPS[1])]
    connection.deadline = deadline
    expiry = threading.Timer(max(0, deadline - time.monotonic()), _abort, args=(connection,))
    expiry.daemon = True
    expiry.start()
    response = None
    try:
        query = urlencode({"name": host, "type": record_type, "cd": "0", "edns_client_subnet": "0.0.0.0/0"})
        connection.request(
            "GET",
            "/resolve?" + query,
            headers={"Accept": "application/dns-json", "Accept-Encoding": "identity", "Host": "dns.google"},
        )
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Encoding", "identity") != "identity":
            raise DownloadError("公共 DNS HTTPS 响应无效或不可用。", retryable=True)
        content = response.read(65537)
        if len(content) > 65536 or connection.deadline_expired.is_set() or time.monotonic() >= deadline:
            raise DownloadError("公共 DNS HTTPS 响应超过大小或时间限制。", retryable=True)
        return json.loads(content)
    except (OSError, http.client.HTTPException, ValueError, RecursionError) as exc:
        if isinstance(exc, DownloadError):
            raise
        raise DownloadError("公共 DNS HTTPS 查询失败。", retryable=True) from None
    finally:
        expiry.cancel()
        if response is not None:
            response.close()
        connection.close()


def _dns_answers(payload: dict, host: str, record_type: int) -> tuple[list[tuple], int]:
    if (
        not isinstance(payload, dict)
        or type(payload.get("Status")) is not int
        or payload["Status"] != 0
        or payload.get("TC") is not False
        or payload.get("CD") is not False
        or payload.get("Question") != [{"name": host + ".", "type": record_type}]
    ):
        raise DownloadError("公共 DNS 响应未确认完整的来源查询。", retryable=True)
    records = payload.get("Answer", [])
    if not isinstance(records, list) or len(records) > 64:
        raise DownloadError("公共 DNS 地址数量超过限制。")
    aliases = {host}
    for _ in range(16):
        previous = len(aliases)
        for record in records:
            if not isinstance(record, dict):
                raise DownloadError("公共 DNS 地址记录无效。")
            if record.get("type") == 5 and _canonical_host(record.get("name", "")) in aliases:
                aliases.add(_canonical_host(record.get("data", "")))
        if len(aliases) == previous:
            break
    else:
        raise DownloadError("公共 DNS 别名链超过限制。")
    rows, ttl = [], 300
    for record in records:
        record_kind = record.get("type")
        if type(record_kind) is not int:
            raise DownloadError("公共 DNS 返回了无效的地址类型。")
        if record_kind == 5 and _canonical_host(record.get("name", "")) in aliases:
            alias_ttl = record.get("TTL")
            if type(alias_ttl) is not int or alias_ttl < 0:
                raise DownloadError("公共 DNS 别名缓存期限无效。")
            ttl = min(ttl, alias_ttl)
        if record_kind not in {1, 28}:
            continue
        if not isinstance(record.get("data"), str):
            raise DownloadError("公共 DNS 返回了无效的地址。")
        try:
            row = _address_row(record.get("data", ""))
        except ValueError:
            raise DownloadError("公共 DNS 返回了无效的地址。") from None
        _public_row(row)
        if _canonical_host(record.get("name", "")) not in aliases:
            raise DownloadError("公共 DNS 地址不属于查询来源的别名链。")
        if record_kind != (1 if row[0] == socket.AF_INET else 28):
            raise DownloadError("公共 DNS 返回了不匹配的地址类型。")
        record_ttl = record.get("TTL")
        if type(record_ttl) is not int or record_ttl < 0:
            raise DownloadError("公共 DNS 缓存期限无效。")
        ttl = min(ttl, record_ttl)
        if row not in rows:
            rows.append(row)
    return rows, ttl


def resolve_addresses(host: str, *, deadline: float, fallback: Callable) -> list[tuple]:
    """Use DoH only for operator-reviewed hosts, otherwise preserve local DNS."""
    proxy = proxy_for_host(host)
    if proxy is None or not proxy.dns_over_https:
        return fallback(host, deadline=deadline)
    host = _canonical_host(host)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if not address.is_global:
            raise DownloadError("采集代理目标必须是 public 公网地址。")
        return [_address_row(str(address))]
    key = (proxy, host)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _DNS_CACHE.get(key)
        if cached and cached[0] > now:
            _DNS_CACHE.move_to_end(key)
            return list(cached[1])
    dns_deadline = min(deadline, now + 10.0)
    if not _DOH_SLOTS.acquire(timeout=max(0, dns_deadline - time.monotonic())):
        raise DownloadError("公共 DNS 查询队列已满或超过时间限制。", retryable=True)
    try:
        rows, ttl = [], 300
        for record_type in (1, 28):
            answers, record_ttl = _dns_answers(
                _query_dns(host, record_type, proxy, dns_deadline), host, record_type
            )
            rows.extend(row for row in answers if row not in rows)
            ttl = min(ttl, record_ttl)
        if not rows or len(rows) > 32:
            raise DownloadError("公共 DNS 没有可用地址或地址数量超过限制。", retryable=True)
        with _CACHE_LOCK:
            _DNS_CACHE[key] = (time.monotonic() + ttl, tuple(rows))
            _DNS_CACHE.move_to_end(key)
            while len(_DNS_CACHE) > 128:
                _DNS_CACHE.popitem(last=False)
        return rows
    finally:
        _DOH_SLOTS.release()
