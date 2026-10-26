"""Network boundary checks for incremental collection and conditional requests."""

import http.client
import socket
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
from app.shared.infrastructure.http import download as ingestion_download


class Response:
    def __init__(self, chunks=(), *, status=200, headers=None):
        self.status = status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.chunks = iter(chunks)
        self.reads = 0
        self.closed = False

    def close(self):
        self.closed = True

    def getheader(self, name, default=None):
        return self.headers.get(name.lower(), default)

    def read1(self, limit):
        self.reads += 1
        block = next(self.chunks, b"")
        if isinstance(block, Exception):
            raise block
        assert len(block) <= limit
        return block


@pytest.fixture
def transport(monkeypatch):
    responses = []
    requests = []
    connections = []

    class Connection:
        sock = None

        def __init__(self, host, address, timeout):
            self.host = host
            self.closed = False
            connections.append(self)

        def request(self, method, path, *, headers, body=None):
            requests.append((self.host, path, headers.copy()))
            self.method, self.body = method, body

        def getresponse(self):
            return responses.pop(0)

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        ingestion_download,
        "_public_addresses",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(ingestion_download, "_PinnedHTTPSConnection", Connection)
    return responses, requests, connections


def download(**kwargs):
    return ingestion_download._download("https://example.com/law", deadline=time.monotonic() + 10, **kwargs)


def test_consumer_receives_chunks_before_later_network_reads(transport):
    responses, _, connections = transport
    response = Response(
        [b"abc", b"def", b"ghi"],
        headers={"Content-Length": "9", "Content-Type": "application/pdf", "ETag": '"version-1"'},
    )
    responses.append(response)
    published = object()

    def consume(chunks):
        for count, block in enumerate(chunks, 1):
            assert response.reads == count
            assert block == (b"abc", b"def", b"ghi")[count - 1]
        return published

    result = download(consume=consume)
    assert result["blob"] is published
    assert "content" not in result
    assert result["etag"] == '"version-1"'
    assert result["filename"] == "law.pdf"
    assert all(connection.closed for connection in connections)


def test_connect_failure_before_response_records_zero_body_bytes(transport, monkeypatch):
    _, _, connections = transport

    class FailedRequest:
        sock = None

        def __init__(self, host, address, timeout):
            connections.append(self)

        def request(self, method, path, *, headers, body=None):
            raise TimeoutError("connect timeout")

        def close(self):
            pass

    monkeypatch.setattr(ingestion_download, "_PinnedHTTPSConnection", FailedRequest)
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.bytes_received == 0


def test_partial_response_failure_keeps_quota_reservation_unknown(transport):
    responses, _, _ = transport
    responses.append(
        Response(
            [b"abc", TimeoutError("read timeout")],
            headers={"Content-Length": "9", "Content-Type": "application/pdf"},
        )
    )
    with pytest.raises(ingestion_download.DownloadError) as error:
        download(consume=lambda chunks: list(chunks))
    assert error.value.bytes_received is None


def test_pinned_transport_falls_back_without_changing_tls_hostname(monkeypatch):
    attempts, closed, names = [], [], []

    class Socket:
        def __init__(self, family):
            self.family = family
            self.handshake_finished = False

        def settimeout(self, value):
            assert 0 < value <= (15 if self.handshake_finished else 5)

        def connect(self, destination):
            attempts.append(destination)
            if self.family == socket.AF_INET6:
                raise OSError("IPv6 route unavailable")

        def close(self):
            closed.append(self.family)

        def do_handshake(self):
            self.handshake_finished = True

    class Context:
        def wrap_socket(self, raw, *, server_hostname, do_handshake_on_connect):
            names.append(server_hostname)
            assert do_handshake_on_connect is False
            return raw

    first = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::6812:1c3a", 443, 0, 0))
    second = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    monkeypatch.setattr(ingestion_download.ssl, "create_default_context", lambda: Context())
    monkeypatch.setattr(ingestion_download.socket, "socket", lambda family, *_: Socket(family))
    connection = ingestion_download._PinnedHTTPSConnection("official.example", first, 15)
    connection.alternate_addresses = [second]
    connection.deadline = time.monotonic() + 20
    connection.connect()
    assert attempts == [first[4], second[4]]
    assert names == ["official.example"]
    assert socket.AF_INET6 in closed
    assert connection.sock.family == socket.AF_INET


def test_download_prefers_validated_ipv4_before_unreachable_ipv6(transport, monkeypatch):
    responses, _, _ = transport
    responses.append(Response([b"original"], headers={"Content-Length": "8"}))
    ipv6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::6812:1c3a", 443, 0, 0))
    ipv4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
    selected = []
    original = ingestion_download._PinnedHTTPSConnection

    def connection(host, address, timeout):
        selected.append(address)
        return original(host, address, timeout)

    monkeypatch.setattr(ingestion_download, "_public_addresses", lambda *a, **kw: [ipv6, ipv4])
    monkeypatch.setattr(ingestion_download, "_PinnedHTTPSConnection", connection)
    assert download()["content"] == b"original"
    assert selected == [ipv4]


def test_explicit_range_binds_offsets_total_and_strong_validator(transport):
    responses, requests, _ = transport
    responses.append(
        Response(
            [b"efgh"],
            status=206,
            headers={
                "Content-Length": "4",
                "Content-Range": "bytes 4-7/12",
                "ETag": '"v1"',
                "Content-Type": "application/pdf",
                "Accept-Ranges": "bytes",
            },
        )
    )
    headers = []
    got = download(byte_range=(4, 7), range_validator='"v1"', on_headers=headers.append)
    assert requests[0][2]["Range"] == "bytes=4-7"
    assert requests[0][2]["If-Range"] == '"v1"'
    assert got["content"] == b"efgh" and got["total_size"] == 12
    assert got["range_start"] == 4 and got["range_end"] == 7
    assert headers[0]["response_status"] == 206


def test_requested_range_rejects_full_response_before_reading_body(transport):
    responses, requests, _ = transport
    response = Response(
        [b"full document must not be consumed"],
        status=200,
        headers={"Content-Length": "34", "Content-Type": "application/pdf"},
    )
    responses.append(response)
    with pytest.raises(ingestion_download.DownloadError, match="未按要求返回分段响应"):
        download(byte_range=(0, 0), max_bytes=300 * 1024**2)
    assert requests[0][2]["Range"] == "bytes=0-0"
    assert response.reads == 0


def test_dedicated_dataset_range_exceeds_generic_total_only_with_exact_binding(transport):
    responses, requests, _ = transport
    start = 200 * 1024 * 1024
    total = 764 * 1024 * 1024
    responses.append(
        Response(
            [b"csv!"],
            status=206,
            headers={
                "Content-Length": "4",
                "Content-Range": f"bytes {start}-{start + 3}/{total}",
                "ETag": '"fixed-version"',
            },
        )
    )
    result = download(
        byte_range=(start, start + 3), max_bytes=4, range_total_bytes=total, range_validator='"fixed-version"'
    )
    assert result["content"] == b"csv!" and result["total_size"] == total
    assert requests[0][2]["Range"] == f"bytes={start}-{start + 3}"


@pytest.mark.parametrize("status,actual_total", [(200, 800), (206, 763 * 1024 * 1024)])
def test_dataset_range_rejects_whole_file_or_different_total_before_reading(transport, status, actual_total):
    responses, _, _ = transport
    response = Response(
        [b"csv!"],
        status=status,
        headers={
            "Content-Length": "4",
            "Content-Range": f"bytes 0-3/{actual_total}",
        },
    )
    responses.append(response)
    with pytest.raises(ingestion_download.DownloadError):
        download(byte_range=(0, 3), max_bytes=4, range_total_bytes=764 * 1024 * 1024)
    assert response.reads == 0


@pytest.mark.parametrize(
    "options",
    [
        {"range_total_bytes": 200 * 1024 * 1024},
        {"byte_range": (0, 7), "max_bytes": 4, "range_total_bytes": 8},
        {"byte_range": (0, 3), "range_total_bytes": 1024 * 1024 * 1024 + 1},
        {"byte_range": (0, 3), "range_total_bytes": True},
        {"byte_range": (200 * 1024 * 1024, 200 * 1024 * 1024 + 3)},
    ],
)
def test_dataset_opt_in_cannot_remove_response_or_generic_file_limits(transport, options):
    _, requests, _ = transport
    with pytest.raises(ingestion_download.DownloadError):
        download(**options)
    assert not requests


@pytest.mark.parametrize("header", ["bytes 3-6/12", "bytes 4-8/12", "bytes 4-7/*", "bytes 4-7/3"])
def test_malformed_or_unrequested_ranges_are_not_consumed(transport, header):
    responses, _, _ = transport
    response = Response([b"bad!"], status=206, headers={"Content-Length": "4", "Content-Range": header})
    responses.append(response)
    with pytest.raises(ingestion_download.DownloadError):
        download(byte_range=(4, 7), range_validator='"v1"')
    assert response.reads == 0


def test_header_callback_can_defer_large_file_before_body_is_read(transport):
    responses, _, connections = transport
    response = Response(
        [b"original"], headers={"Content-Length": "8", "ETag": '"v1"', "Accept-Ranges": "bytes"}
    )
    responses.append(response)

    def defer(meta):
        assert meta["total_size"] == 8 and meta["accept_ranges"] is True
        raise ingestion_download.DownloadError("defer before body", retryable=True)

    with pytest.raises(ingestion_download.DownloadError, match="defer"):
        download(on_headers=defer)
    assert response.reads == 0 and all(c.closed for c in connections)


def test_larger_original_limit_is_explicit_and_still_stream_bounded(transport, monkeypatch):
    responses, _, _ = transport
    monkeypatch.setattr(ingestion_download, "MAX_BYTES", 3)
    responses.append(Response([b"abcdefgh"], headers={"Content-Length": "8"}))
    with pytest.raises(ingestion_download.DownloadError, match="大小"):
        download()
    responses.append(Response([b"abcdefgh"], headers={"Content-Length": "8"}))
    assert download(max_bytes=8)["content"] == b"abcdefgh"
    with pytest.raises(ingestion_download.DownloadError):
        download(max_bytes=300 * 1024 * 1024 + 1)


@pytest.mark.parametrize("mime", ["text/html", "text/xml", "application/xml"])
def test_explicit_catalog_gzip_is_streamed_and_binds_uncompressed_original(transport, mime):
    import gzip

    responses, requests, _ = transport
    raw = b"<html><p>Official legal catalog</p></html>" * 1000
    compressed = gzip.compress(raw)
    responses.append(
        Response(
            [compressed[:20], compressed[20:]],
            headers={
                "Content-Length": str(len(compressed)),
                "Content-Encoding": "gzip",
                "Content-Type": mime,
            },
        )
    )
    result = download(accept_compressed_html=True)
    assert requests[0][2]["Accept-Encoding"] == "gzip, identity"
    assert result["content"] == raw
    assert result["transfer_bytes"] == len(compressed)
    assert result["content_encoding"] == "gzip"


def test_six_mib_compressed_html_cap_requires_an_explicit_adapter_opt_in(transport):
    import gzip

    responses, _, _ = transport
    raw = b"<html>" + b"A" * (4 * 1024 * 1024 + 1) + b"</html>"
    compressed = gzip.compress(raw)
    headers = {
        "Content-Length": str(len(compressed)),
        "Content-Encoding": "gzip",
        "Content-Type": "text/html",
    }
    responses.append(Response([compressed], headers=headers))
    with pytest.raises(ingestion_download.DownloadError, match="4 MB"):
        download(accept_compressed_html=True)

    responses.append(Response([compressed], headers=headers))
    result = download(
        accept_compressed_html=True,
        max_compressed_html_bytes=6 * 1024 * 1024,
    )
    assert result["content"] == raw


@pytest.mark.parametrize("variant", ["truncated", "checksum", "trailing", "bomb", "pdf", "not_opted_in"])
def test_compressed_responses_cannot_publish_invalid_or_unbounded_content(transport, monkeypatch, variant):
    import gzip

    responses, _, _ = transport
    body = gzip.compress(b"<html>" + b"A" * 4096 + b"</html>")
    if variant == "truncated":
        body = body[:-5]
    elif variant == "checksum":
        body = body[:-8] + bytes([body[-8] ^ 255]) + body[-7:]
    elif variant == "trailing":
        body += gzip.compress(b"extra")
    elif variant == "bomb":
        monkeypatch.setattr(ingestion_download, "MAX_COMPRESSED_HTML_BYTES", 512)
    responses.append(
        Response(
            [body],
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "application/pdf" if variant == "pdf" else "text/html",
                "Content-Length": str(len(body)),
            },
        )
    )
    published = []

    def consume(blocks):
        result = b"".join(blocks)
        published.append(result)
        return result

    with pytest.raises(ingestion_download.DownloadError):
        download(accept_compressed_html=variant != "not_opted_in", consume=consume)
    assert not published


def test_official_catalog_json_request_and_response_are_bounded(transport):
    import gzip
    import json

    responses, _, connections = transport
    payload = {"jns": ["UU"], "start": 0, "length": 5}
    body = gzip.compress(b'{"jml":1,"data":[{"idperaturan":"P1"}]}')
    responses.append(
        Response([body], headers={"Content-Encoding": "gzip", "Content-Type": "application/json"})
    )
    result = download(json_body=payload, accept_compressed_json=True)
    assert connections[0].method == "POST"
    assert json.loads(connections[0].body) == payload
    assert json.loads(result["content"])["jml"] == 1
    assert result["request_method"] == "POST"


def test_catalog_post_never_replays_body_on_redirect(transport):
    responses, requests, _ = transport
    responses.append(Response(status=307, headers={"Location": "https://another.example/list"}))
    with pytest.raises(ingestion_download.DownloadError):
        download(json_body={"jns": ["UU"]})
    assert len(requests) == 1


def test_same_origin_catalog_form_post_is_host_bound_and_records_only_body_hash(transport):
    from urllib.parse import parse_qs

    responses, requests, connections = transport
    responses.append(Response([b"<html>page 2</html>"], headers={"Content-Type": "text/html"}))
    result = download(
        required_host="example.com",
        form_fields=[("__VIEWSTATE", "opaque-state"), ("__EVENTTARGET", "pager$next")],
    )
    assert connections[0].method == "POST"
    assert requests[0][2]["Content-Type"] == "application/x-www-form-urlencoded"
    assert parse_qs(connections[0].body.decode()) == {
        "__VIEWSTATE": ["opaque-state"],
        "__EVENTTARGET": ["pager$next"],
    }
    assert result["request_method"] == "POST"
    assert len(result["request_body_sha256"]) == 64
    assert "opaque-state" not in result


def test_catalog_form_post_requires_host_binding_and_rejects_redirects(transport):
    responses, _, _ = transport
    with pytest.raises(ingestion_download.DownloadError):
        download(form_fields=[("__VIEWSTATE", "opaque-state")])
    assert not transport[2]

    responses.append(Response(status=307, headers={"Location": "https://another.example/list"}))
    with pytest.raises(ingestion_download.DownloadError):
        download(required_host="example.com", form_fields=[("__VIEWSTATE", "opaque-state")])
    assert len(transport[1]) == 1


@pytest.mark.parametrize(
    "fields",
    [
        [("unsafe&name", "x")],
        [("__VIEWSTATE", "x" * (1024 * 1024 + 1))],
    ],
)
def test_catalog_form_post_rejects_unbounded_fields_before_connecting(transport, fields):
    with pytest.raises(ingestion_download.DownloadError):
        download(required_host="example.com", form_fields=fields)
    assert not transport[2]


def test_oversized_catalog_request_is_rejected_before_connecting(transport):
    with pytest.raises(ingestion_download.DownloadError):
        download(json_body={"filter": "x" * 16385})
    assert not transport[2]


@pytest.mark.parametrize(
    ("header", "name"),
    [
        ('attachment; filename="UU Nomor 2 Tahun 2026.pdf"', "UU Nomor 2 Tahun 2026.pdf"),
        ("attachment; filename*=UTF-8''%E6%B3%95%E8%A7%84%20UU.pdf", "法规 UU.pdf"),
        ('attachment; filename="../../law.pdf"', "law.pdf"),
        ('attachment; filename="misleading.html"', "misleading.html.pdf"),
        ('attachment; filename="bad\r\nheader.pdf"', "law.pdf"),
    ],
)
def test_download_display_name_uses_bounded_disposition_without_affecting_storage(transport, header, name):
    responses, _, _ = transport
    responses.append(
        Response(
            [b"pdf contents"], headers={"Content-Type": "application/pdf", "Content-Disposition": header}
        )
    )
    assert download()["filename"] == name


@pytest.mark.parametrize(
    ("chunks", "headers", "limit", "retryable"),
    [
        ([b"abc"], {"Content-Length": "6"}, 20, True),
        ([b"abc", b"def"], {}, 5, False),
        ([], {}, 20, False),
        ([b"abc", http.client.IncompleteRead(b"d", 5)], {}, 20, True),
        ([b"abc", TimeoutError()], {}, 20, True),
    ],
)
def test_incomplete_or_invalid_body_never_reaches_publication(
    transport, monkeypatch, chunks, headers, limit, retryable
):
    responses, _, connections = transport
    responses.append(Response(chunks, headers=headers))
    monkeypatch.setattr(ingestion_download, "MAX_BYTES", limit)
    published = []

    def consume(blocks):
        temporary = bytearray()
        for block in blocks:
            temporary.extend(block)
        published.append(bytes(temporary))
        return temporary

    with pytest.raises(ingestion_download.DownloadError) as error:
        download(consume=consume)
    assert error.value.retryable is retryable
    assert published == []
    assert all(connection.closed for connection in connections)


def test_304_does_not_open_a_blob_and_only_sends_conditional_headers(transport):
    responses, requests, _ = transport
    modified = "Mon, 12 Oct 2026 10:00:00 GMT"
    responses.append(Response(status=304, headers={"ETag": '"v2"', "Last-Modified": modified}))

    def forbidden(_):
        pytest.fail("304 must not create a blob")

    result = download(
        consume=forbidden,
        validators={"etag": '"v1"', "last_modified": modified, "Authorization": "private"},
    )
    headers = requests[0][2]
    assert headers["If-None-Match"] == '"v1"'
    assert headers["If-Modified-Since"] == modified
    assert "Authorization" not in headers
    assert result["url"] == "https://example.com/law"
    assert result["not_modified"] is True
    assert result["etag"] == '"v2"'
    assert "blob" not in result and "content" not in result


@pytest.mark.parametrize("target", ["/another-law", "https://other.example/law"])
def test_redirect_never_sends_validators_for_a_different_resource(transport, target):
    responses, requests, _ = transport
    responses.extend([Response(status=302, headers={"Location": target}), Response([b"body"])])
    download(validators={"ETag": '"v1"', "Last-Modified": "Mon, 12 Oct 2026 10:00:00 GMT"})
    assert "If-None-Match" in requests[0][2]
    assert "If-None-Match" not in requests[1][2]
    assert "If-Modified-Since" not in requests[1][2]


def test_response_and_request_validators_are_bounded_and_cannot_inject_headers(transport):
    responses, requests, _ = transport
    responses.append(Response([b"body"], headers={"ETag": '"' + "x" * 2048 + '"'}))
    result = download(validators={"etag": '"v1"\r\nAuthorization: private', "last_modified": "x" * 2048})
    assert "If-None-Match" not in requests[0][2]
    assert "If-Modified-Since" not in requests[0][2]
    assert result["etag"] is None


@pytest.mark.parametrize("status", [408, 429, 500, 502, 599])
def test_transient_http_errors_preserve_server_retry_after(transport, status):
    responses, _, _ = transport
    responses.append(Response(status=status, headers={"Retry-After": "900"}))
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.retryable is True
    assert error.value.retry_after == 900
    assert error.value.response_host == "example.com"
    assert error.value.bytes_received == 0


def test_redirect_retry_after_identifies_actual_limited_host(transport):
    responses, _, _ = transport
    responses.extend(
        [
            Response(status=302, headers={"Location": "https://limited.example/law"}),
            Response(status=429, headers={"Retry-After": "180"}),
        ]
    )
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.response_host == "limited.example"
    assert error.value.retry_after == 180


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_other_http_errors_are_permanent(transport, status):
    responses, _, _ = transport
    responses.append(Response(status=status, headers={"Retry-After": "60"}))
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.retryable is False
    assert error.value.retry_after is None
    assert error.value.bytes_received == 0


@pytest.mark.parametrize("delay", [60, 900])
def test_retry_after_accepts_http_date(transport, delay):
    responses, _, _ = transport
    date = datetime.now(timezone.utc) + timedelta(seconds=delay)
    responses.append(Response(status=503, headers={"Retry-After": format_datetime(date, usegmt=True)}))
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert delay - 2 <= error.value.retry_after <= delay


@pytest.mark.parametrize("value", ["9" * 129, "900\r\nX: header", "-1", "NaN", "Infinity"])
def test_invalid_retry_after_does_not_create_a_cooldown(transport, value):
    responses, _, _ = transport
    responses.append(Response(status=429, headers={"Retry-After": value}))
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.retryable is True
    assert error.value.retry_after is None


def test_connection_slot_covers_each_actual_host_until_connection_is_closed(transport):
    responses, requests, connections = transport
    responses.extend(
        [
            Response(status=302, headers={"Location": "https://other.example/redirected.txt"}),
            Response([b"body"]),
        ]
    )
    events = []

    @contextmanager
    def slot(host):
        events.append(("entered", host))
        try:
            yield
        finally:
            assert connections[-1].closed
            events.append(("exited", host))

    result = download(connection_slot=slot)
    assert result["content"] == b"body"
    assert events == [
        ("entered", "example.com"),
        ("exited", "example.com"),
        ("entered", "other.example"),
        ("exited", "other.example"),
    ]
    assert [request[0] for request in requests] == ["example.com", "other.example"]


def test_connection_slot_releases_on_interrupted_body(transport):
    responses, _, connections = transport
    responses.append(Response([b"body", TimeoutError()]))
    released = []

    @contextmanager
    def slot(host):
        try:
            yield
        finally:
            assert connections[-1].closed
            released.append(host)

    with pytest.raises(ingestion_download.DownloadError) as error:
        download(connection_slot=slot)
    assert error.value.retryable
    assert released == ["example.com"]


def test_slot_wait_uses_remaining_deadline_before_opening_connection(transport, monkeypatch):
    _, _, connections = transport
    current_time = [100.0]
    monkeypatch.setattr(ingestion_download.time, "monotonic", lambda: current_time[0])
    events = []

    @contextmanager
    def slot(host):
        current_time[0] += 11
        events.append("entered")
        try:
            yield
        finally:
            events.append("exited")

    with pytest.raises(ingestion_download.DownloadError) as error:
        download(connection_slot=slot)
    assert error.value.retryable
    assert events == ["entered", "exited"]
    assert not connections


@pytest.mark.parametrize("redirect", [False, True])
def test_unconditional_304_cannot_validate_an_original(transport, redirect):
    responses, _, _ = transport
    if redirect:
        responses.append(Response(status=302, headers={"Location": "/different.txt"}))
    responses.append(Response(status=304))
    with pytest.raises(ingestion_download.DownloadError) as error:
        download(validators={"etag": '"v1"'} if redirect else None)
    assert error.value.retryable is False


def test_partial_response_is_rejected_without_publishing(transport):
    responses, _, _ = transport
    responses.append(Response([b"partial"], status=206))
    with pytest.raises(ingestion_download.DownloadError):
        download(consume=lambda _: pytest.fail("unsolicited partial response must not be saved"))


@pytest.mark.parametrize(
    "headers",
    [
        {"Content-Length": "999999999999999999999999999999999"},
        {"Content-Length": "3, 3"},
        {"Content-Encoding": "gzip"},
        {"Content-Type": "not a mime"},
    ],
)
def test_invalid_representation_headers_fail_before_blob_creation(transport, headers):
    responses, _, _ = transport
    responses.append(Response([b"abc"], headers=headers))
    with pytest.raises(ingestion_download.DownloadError) as error:
        download(consume=lambda _: pytest.fail("invalid response must not open storage"))
    assert error.value.retryable is False


def test_transient_dns_failure_can_retry_without_opening_connection(monkeypatch):
    def fail_dns(*args, **kwargs):
        raise socket.gaierror("temporary resolver failure")

    monkeypatch.setattr(socket, "getaddrinfo", fail_dns)
    monkeypatch.setattr(
        ingestion_download, "_PinnedHTTPSConnection", lambda *args: pytest.fail("DNS was not validated")
    )
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.retryable is True


def test_mixed_public_private_dns_is_permanent(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 443)),
        ],
    )
    monkeypatch.setattr(
        ingestion_download, "_PinnedHTTPSConnection", lambda *args: pytest.fail("private IP was not rejected")
    )
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.retryable is False


def test_discovery_is_network_free_same_origin_bounded_and_deduplicated(monkeypatch):
    monkeypatch.setattr(
        ingestion_download, "_download", lambda *args, **kwargs: pytest.fail("discovery fetched data")
    )
    html = b"""<a href="/one.pdf#article">one</a><a href="/one.pdf">again</a>
      <a href="https://other.example/two.pdf">outside</a><a href="/page.html">page</a>
      <a href="https://example.com:8443/unsafe.pdf">port</a>
      <a href="/second.docx">two</a><a href="/third.txt">three</a>"""
    assert ingestion_download.discover_attachments("https://example.com/law", html, "text/html", 2) == [
        "https://example.com/one.pdf",
        "https://example.com/second.docx",
    ]
    assert ingestion_download.discover_attachments("https://example.com/law", html, "application/pdf") == []


def test_unsafe_urls_are_permanent_before_transport(transport):
    _, requests, _ = transport
    with pytest.raises(ingestion_download.DownloadError) as error:
        ingestion_download._download("https://127.0.0.1/law", deadline=time.monotonic() + 10)
    assert error.value.retryable is False
    assert not requests


@pytest.fixture
def pinned_transport(monkeypatch):
    """Exercise the real pinned connector with deterministic sockets and a watchdog."""
    state = SimpleNamespace(
        clock=100.0,
        phase=None,
        tcp_elapsed=0.0,
        tls_elapsed=0.0,
        shutdown_fails=False,
        raw=None,
        tls=None,
        connection=None,
        timer=None,
        observations=[],
        wraps=[],
        response_calls=0,
        handshake_timeout=None,
    )
    monkeypatch.setattr(ingestion_download.time, "monotonic", lambda: state.clock)

    class Socket:
        def __init__(self):
            self.closed = False
            self.timeout = None

        def settimeout(self, timeout):
            if self.closed:
                raise OSError("socket is closed")
            self.timeout = timeout

        def connect(self, destination):
            assert destination == ("93.184.216.34", 443)
            state.observations.append(("tcp", getattr(state.connection, "active_socket", None) is self))
            state.clock += state.tcp_elapsed
            if state.phase == "tcp":
                state.timer.callback()
            if self.closed:
                raise OSError("watchdog closed TCP socket")

        def shutdown(self, how):
            if state.shutdown_fails:
                raise OSError("socket not connected yet")

        def close(self):
            self.closed = True

        def do_handshake(self):
            state.observations.append(("tls", getattr(state.connection, "active_socket", None) is self))
            state.handshake_timeout = self.timeout
            state.clock += state.tls_elapsed
            if state.phase == "tls":
                state.timer.callback()
            if self.closed:
                raise OSError("watchdog closed TLS socket")

    def socket_factory(*args):
        state.raw = Socket()
        if state.phase == "socket_creation":
            state.timer.callback()
        return state.raw

    class TLSContext:
        def wrap_socket(self, raw, *, server_hostname, do_handshake_on_connect=True):
            state.wraps.append((server_hostname, do_handshake_on_connect))
            state.tls = Socket()
            state.tls.timeout = raw.timeout
            if state.phase == "wrap":
                state.timer.callback()
            if do_handshake_on_connect:
                state.tls.do_handshake()
            return state.tls

    class Timer:
        def __init__(self, interval, callback):
            self.callback = callback
            state.timer = self

        def start(self):
            if state.phase == "before_socket":
                self.callback()

        def cancel(self):
            pass

    class Connection(ingestion_download._PinnedHTTPSConnection):
        def __init__(self, *args):
            super().__init__(*args)
            state.connection = self

        def request(self, *args, **kwargs):
            self.connect()

        def getresponse(self):
            state.response_calls += 1
            return Response([b"body"])

    monkeypatch.setattr(ingestion_download.socket, "socket", socket_factory)
    monkeypatch.setattr(ingestion_download.ssl, "create_default_context", lambda: TLSContext())
    monkeypatch.setattr(ingestion_download.threading, "Timer", Timer)
    monkeypatch.setattr(ingestion_download, "_PinnedHTTPSConnection", Connection)
    monkeypatch.setattr(
        ingestion_download,
        "_public_addresses",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    return state


def test_watchdog_can_observe_tcp_and_tls_sockets_before_blocking_calls(pinned_transport):
    result = download()
    assert result["content"] == b"body"
    assert pinned_transport.observations == [("tcp", True), ("tls", True)]
    assert pinned_transport.wraps == [("example.com", False)]


def test_connect_and_handshake_share_the_same_absolute_deadline(pinned_transport):
    pinned_transport.tcp_elapsed = 7
    pinned_transport.tls_elapsed = 2
    result = download()
    assert result["content"] == b"body"
    assert pinned_transport.handshake_timeout == 3
    assert pinned_transport.tls.timeout == 1


@pytest.mark.parametrize("phase", ["before_socket", "socket_creation", "tcp", "wrap", "tls"])
def test_watchdog_expiry_cannot_start_or_leave_network_work_alive(pinned_transport, phase):
    pinned_transport.phase = phase
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.retryable is True
    assert pinned_transport.response_calls == 0
    if phase == "before_socket":
        assert pinned_transport.raw is None
    else:
        assert pinned_transport.raw.closed
    if pinned_transport.tls is not None:
        assert pinned_transport.tls.closed


def test_late_tcp_completion_cannot_begin_tls_after_deadline(pinned_transport):
    pinned_transport.tcp_elapsed = 11
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.retryable
    assert pinned_transport.wraps == []
    assert pinned_transport.raw.closed


def test_late_tls_completion_cannot_begin_http_response_after_deadline(pinned_transport):
    pinned_transport.tls_elapsed = 11
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.retryable
    assert pinned_transport.response_calls == 0
    assert pinned_transport.tls.closed


def test_watchdog_closes_unconnected_socket_even_when_shutdown_fails(pinned_transport):
    pinned_transport.phase = "tcp"
    pinned_transport.shutdown_fails = True
    with pytest.raises(ingestion_download.DownloadError) as error:
        download()
    assert error.value.retryable
    assert pinned_transport.raw.closed


def test_late_redirect_watchdog_does_not_expire_the_next_connection(transport, monkeypatch):
    responses, _, connections = transport
    responses.extend(
        [Response(status=302, headers={"Location": "https://other.example/law"}), Response([b"body"])]
    )
    timers = []

    class Timer:
        def __init__(self, interval, callback):
            self.callback = callback
            timers.append(self)

        def start(self):
            if len(timers) == 2:
                # Timer.cancel cannot stop a callback that already began running.
                timers[0].callback()

        def cancel(self):
            pass

    monkeypatch.setattr(ingestion_download.threading, "Timer", Timer)
    result = download()
    assert result["content"] == b"body"
    assert connections[0].deadline_expired.is_set()
    assert not connections[1].deadline_expired.is_set()
