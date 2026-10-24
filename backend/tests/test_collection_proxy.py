"""Collection proxy boundaries, real CONNECT framing and DNS trust regression."""

from __future__ import annotations

import io
import json
import ssl
import time

import pytest
from app.contexts.ingestion.infrastructure import collection_proxy as transport
from app.shared.infrastructure.http.download import DownloadError


@pytest.fixture(autouse=True)
def isolated_transport(monkeypatch):
    for name in ("COLLECTION_PROXY_URL", "COLLECTION_PROXY_HOSTS", "COLLECTION_DOH_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    transport._DNS_CACHE.clear()


def configure(monkeypatch, *, doh=False, url="http://127.0.0.1:17890"):
    monkeypatch.setenv("COLLECTION_PROXY_URL", url)
    monkeypatch.setenv("COLLECTION_PROXY_HOSTS", "official.example,huggingface.co")
    if doh:
        monkeypatch.setenv("COLLECTION_DOH_HOSTS", "huggingface.co")


def address(value="8.8.8.8"):
    return transport._address_row(value)


def answer(host, record_type, values=(), *, ttl=60):
    return {
        "Status": 0,
        "TC": False,
        "CD": False,
        "Question": [{"name": host + ".", "type": record_type}],
        "Answer": [{"name": host + ".", "type": record_type, "TTL": ttl, "data": value} for value in values],
    }


def test_ambient_proxy_variables_do_not_enable_collection_proxy(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://secret:password@127.0.0.1:17890")
    monkeypatch.setenv("http_proxy", "http://secret:password@127.0.0.1:17890")
    calls = []
    assert transport.proxy_for_host("official.example") is None
    assert transport.resolve_addresses(
        "official.example",
        deadline=time.monotonic() + 1,
        fallback=lambda host, *, deadline: calls.append(host) or [address()],
    ) == [address()]
    assert calls == ["official.example"]


def test_exact_host_opt_in_does_not_enable_unreviewed_redirect_hosts(monkeypatch):
    configure(monkeypatch, doh=True)
    assert transport.proxy_for_host("official.example").dns_over_https is False
    assert transport.proxy_for_host("huggingface.co").dns_over_https is True
    assert transport.proxy_for_host("sub.official.example") is None
    assert transport.proxy_for_host("official.example.attacker.example") is None
    assert transport.proxy_for_host("HUGGINGFACE.CO.").dns_over_https is True


@pytest.mark.parametrize(
    "host_list", ["*", "*.official.example", "official.example:443", "https://official.example"]
)
def test_host_scope_rejects_wildcards_ports_and_urls(monkeypatch, host_list):
    configure(monkeypatch)
    monkeypatch.setenv("COLLECTION_PROXY_HOSTS", host_list)
    with pytest.raises(DownloadError, match="精确|无效"):
        transport.proxy_for_host("official.example")


def test_partial_and_unreviewed_doh_configuration_fail_closed(monkeypatch):
    monkeypatch.setenv("COLLECTION_PROXY_URL", "http://127.0.0.1:17890")
    with pytest.raises(DownloadError, match="同时配置"):
        transport.proxy_for_host("official.example")
    configure(monkeypatch)
    monkeypatch.setenv("COLLECTION_DOH_HOSTS", "unreviewed.example")
    with pytest.raises(DownloadError, match="必须属于"):
        transport.proxy_for_host("official.example")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:17890",
        "http://localhost:17890",
        "http://192.168.1.1:17890",
        "http://127.0.0.1:17890/path",
        "http://127.0.0.1:17890/?proxy=all",
        "http://127.0.0.1:17890/#all",
        "http://secret:password@8.8.8.8:17890",
        "http://secret:pass%0Aword@127.0.0.1:17890",
    ],
)
def test_invalid_proxy_urls_and_remote_plaintext_credentials_are_redacted(monkeypatch, url):
    configure(monkeypatch, url=url)
    with pytest.raises(DownloadError) as error:
        transport.proxy_for_host("official.example")
    assert "secret" not in str(error.value)
    assert "password" not in str(error.value)
    assert url not in str(error.value)


def test_local_proxy_credentials_do_not_appear_in_endpoint_repr(monkeypatch):
    configure(monkeypatch, url="http://secret:password@127.0.0.1:17890")
    endpoint = transport.proxy_for_host("official.example")
    assert endpoint.authorization.startswith("Basic ")
    assert "secret" not in repr(endpoint)
    assert "password" not in repr(endpoint)
    assert endpoint.authorization not in repr(endpoint)


class Socket:
    def __init__(self, response=b"HTTP/1.1 200 Connection Established\r\n\r\n"):
        self.response = response
        self.sent = []
        self.destination = None
        self.closed = False
        self.handshake = False

    def settimeout(self, timeout):
        assert timeout > 0

    def connect(self, destination):
        self.destination = destination

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, *args):
        return io.BytesIO(self.response)

    def do_handshake(self):
        self.handshake = True

    def close(self):
        self.closed = True


@pytest.mark.parametrize("target", ["8.8.8.8", "2001:4860:4860::8888"])
def test_connect_pins_ip_but_preserves_origin_sni_host_and_credential_boundary(monkeypatch, target):
    configure(monkeypatch, url="http://secret:password@127.0.0.1:17890")
    endpoint = transport.proxy_for_host("official.example")
    connection = transport.PinnedProxyHTTPSConnection("official.example", address(target), 5, proxy=endpoint)
    assert connection._context.check_hostname is True
    assert connection._context.verify_mode == ssl.CERT_REQUIRED
    raw, tls = Socket(), Socket()
    monkeypatch.setattr(transport.socket, "socket", lambda *args: raw)
    tls_names = []

    class Context:
        def wrap_socket(self, sock, *, server_hostname, do_handshake_on_connect):
            assert sock is raw
            assert do_handshake_on_connect is False
            tls_names.append(server_hostname)
            return tls

    connection._context = Context()
    connection.request("GET", "/law")
    connect_bytes = b"".join(raw.sent)
    encrypted_request = b"".join(tls.sent)
    authority = f"[{target}]:443" if ":" in target else target + ":443"
    assert raw.destination == ("127.0.0.1", 17890)
    assert ("CONNECT " + authority + " HTTP/1.1\r\n").encode() in connect_bytes
    assert ("Host: " + authority).encode() in connect_bytes
    assert endpoint.authorization.encode() in connect_bytes
    assert b"Host: official.example\r\n" in encrypted_request
    assert b"Proxy-Authorization" not in encrypted_request
    assert endpoint.authorization.encode() not in encrypted_request
    assert tls_names == ["official.example"]
    assert tls.handshake is True
    assert connection.active_socket is tls
    connection.close()


def test_private_fallback_address_is_rejected_before_proxy_dial(monkeypatch):
    configure(monkeypatch)
    connection = transport.PinnedProxyHTTPSConnection(
        "official.example", address(), 5, proxy=transport.proxy_for_host("official.example")
    )
    connection.alternate_addresses = [address("127.0.0.1")]
    monkeypatch.setattr(transport.socket, "socket", lambda *args: pytest.fail("private answer dialed"))
    with pytest.raises(DownloadError, match="public"):
        connection.connect()


def test_expired_absolute_deadline_cannot_open_proxy_socket(monkeypatch):
    configure(monkeypatch)
    connection = transport.PinnedProxyHTTPSConnection(
        "official.example", address(), 5, proxy=transport.proxy_for_host("official.example")
    )
    connection.deadline = time.monotonic() - 1
    monkeypatch.setattr(transport.socket, "socket", lambda *args: pytest.fail("expired job dialed"))
    with pytest.raises(TimeoutError):
        connection.connect()


def test_proxy_failure_reason_cannot_echo_operator_credentials(monkeypatch):
    configure(monkeypatch)
    raw = Socket(b"HTTP/1.1 407 secret-password-do-not-echo\r\n\r\n")
    monkeypatch.setattr(transport.socket, "socket", lambda *args: raw)
    connection = transport.PinnedProxyHTTPSConnection(
        "official.example", address(), 5, proxy=transport.proxy_for_host("official.example")
    )
    with pytest.raises(OSError) as error:
        connection.connect()
    assert "secret-password" not in str(error.value)
    assert raw.closed
    assert connection.sock is None
    assert connection.active_socket is None


def test_doh_validates_both_address_families_and_reuses_bounded_cache(monkeypatch):
    configure(monkeypatch, doh=True)
    queries = []

    def query(host, record_type, proxy, deadline):
        queries.append((host, record_type))
        return answer(host, record_type, ["8.8.8.8"] if record_type == 1 else ["2001:4860:4860::8888"])

    monkeypatch.setattr(transport, "_query_dns", query)

    def fallback(*args, **kwargs):
        pytest.fail("DoH source used untrusted local DNS")

    first = transport.resolve_addresses("huggingface.co", deadline=time.monotonic() + 1, fallback=fallback)
    assert first == [address(), address("2001:4860:4860::8888")]
    first.clear()
    assert transport.resolve_addresses(
        "huggingface.co", deadline=time.monotonic() + 1, fallback=fallback
    ) == [address(), address("2001:4860:4860::8888")]
    assert queries == [("huggingface.co", 1), ("huggingface.co", 28)]


def test_private_aaaa_rejects_whole_answer_and_does_not_fallback_or_cache(monkeypatch):
    configure(monkeypatch, doh=True)
    monkeypatch.setattr(
        transport,
        "_query_dns",
        lambda host, kind, *args: answer(host, kind, ["8.8.8.8"] if kind == 1 else ["::1"]),
    )
    with pytest.raises(DownloadError, match="public"):
        transport.resolve_addresses(
            "huggingface.co",
            deadline=time.monotonic() + 1,
            fallback=lambda *args, **kwargs: pytest.fail("private answer used fallback"),
        )
    assert not transport._DNS_CACHE


@pytest.mark.parametrize(
    "field,value",
    [
        ("TC", True),
        ("CD", True),
        ("Status", 2),
        ("Question", [{"name": "attacker.example.", "type": 1}]),
    ],
)
def test_doh_rejects_failed_truncated_or_wrong_question_responses(field, value):
    payload = answer("huggingface.co", 1, ["8.8.8.8"])
    payload[field] = value
    with pytest.raises(DownloadError, match="完整"):
        transport._dns_answers(payload, "huggingface.co", 1)


def test_doh_follows_confirmed_cname_but_rejects_unrelated_public_address():
    payload = answer("huggingface.co", 1)
    payload["Answer"] = [
        {"name": "huggingface.co.", "type": 5, "TTL": 60, "data": "cdn.official.example."},
        {"name": "cdn.official.example.", "type": 1, "TTL": 60, "data": "8.8.8.8"},
    ]
    assert transport._dns_answers(payload, "huggingface.co", 1) == ([address()], 60)
    payload["Answer"][1]["name"] = "attacker.example."
    with pytest.raises(DownloadError, match="别名链"):
        transport._dns_answers(payload, "huggingface.co", 1)


def test_dns_cache_respects_shorter_alias_ttl():
    payload = answer("huggingface.co", 1)
    payload["Answer"] = [
        {"name": "huggingface.co.", "type": 5, "TTL": 5, "data": "cdn.official.example."},
        {"name": "cdn.official.example.", "type": 1, "TTL": 600, "data": "8.8.8.8"},
    ]
    assert transport._dns_answers(payload, "huggingface.co", 1) == ([address()], 5)


def test_dns_failure_does_not_reuse_expired_cache_or_change_to_untrusted_resolver(monkeypatch):
    configure(monkeypatch, doh=True)
    proxy = transport.proxy_for_host("huggingface.co")
    transport._DNS_CACHE[(proxy, "huggingface.co")] = (time.monotonic() - 1, (address(),))

    def fail_query(*args):
        raise DownloadError("public resolver unavailable", retryable=True)

    monkeypatch.setattr(transport, "_query_dns", fail_query)
    with pytest.raises(DownloadError, match="unavailable"):
        transport.resolve_addresses(
            "huggingface.co",
            deadline=time.monotonic() + 1,
            fallback=lambda *args, **kwargs: pytest.fail("expired cache used unsafe DNS"),
        )


@pytest.mark.parametrize("data", [True, None, 134744072, "127.0.0.1", "2001:db8::1"])
def test_doh_rejects_malformed_or_nonpublic_address_data(data):
    payload = answer("huggingface.co", 1, [data])
    with pytest.raises(DownloadError):
        transport._dns_answers(payload, "huggingface.co", 1)


def test_unreviewed_dns_host_keeps_existing_resolver_even_with_proxy_enabled(monkeypatch):
    configure(monkeypatch, doh=True)
    monkeypatch.setattr(transport, "_query_dns", lambda *args: pytest.fail("unreviewed host used DoH"))
    calls = []
    assert transport.resolve_addresses(
        "official.example",
        deadline=time.monotonic() + 1,
        fallback=lambda host, *, deadline: calls.append(host) or [address()],
    ) == [address()]
    assert calls == ["official.example"]


@pytest.mark.parametrize("failure", ["redirect", "compressed", "oversize", "invalid_json"])
def test_doh_transport_rejects_redirects_compression_and_unbounded_responses(monkeypatch, failure):
    configure(monkeypatch, doh=True)
    endpoint = transport.proxy_for_host("huggingface.co")
    calls = []

    class Response:
        status = 302 if failure == "redirect" else 200
        closed = False

        def getheader(self, name, default=None):
            return "gzip" if failure == "compressed" else default

        def read(self, size):
            assert size == 65537
            if failure == "oversize":
                return b"x" * size
            if failure == "invalid_json":
                return b"<!DOCTYPE html><html>untrusted</html>"
            return json.dumps(answer("huggingface.co", 1, ["8.8.8.8"])).encode()

        def close(self):
            self.closed = True

    response = Response()

    class Connection:
        active_socket = None
        deadline_expired = transport.threading.Event()
        closed = False

        def __init__(self, host, row, timeout, *, proxy):
            assert host == "dns.google"
            assert row == address("8.8.8.8")
            assert proxy is endpoint

        def request(self, method, path, *, headers):
            assert self.alternate_addresses == [address("8.8.4.4")]
            assert method == "GET"
            assert headers["Host"] == "dns.google"
            assert headers["Accept-Encoding"] == "identity"
            assert "name=huggingface.co" in path
            assert "cd=0" in path
            assert "edns_client_subnet=0.0.0.0%2F0" in path
            calls.append(self)

        def getresponse(self):
            return response

        def close(self):
            self.closed = True

    monkeypatch.setattr(transport, "PinnedProxyHTTPSConnection", Connection)
    with pytest.raises(DownloadError):
        transport._query_dns("huggingface.co", 1, endpoint, time.monotonic() + 1)
    assert len(calls) == 1
    assert calls[0].closed
    assert response.closed
