"""Early exit must close the real stdlib response, independent of GC."""

import http.client
import os
import socket
import time

import pytest

from app import ingestion


@pytest.fixture
def stdlib_transport(monkeypatch):
    client, peer = socket.socketpair()
    original_fd = client.fileno()
    responses = []
    peer.sendall(
        b"HTTP/1.1 200 OK\r\nConnection: close\r\n"
        b"Content-Type: application/pdf\r\nContent-Length: 100\r\n\r\nabc"
    )

    class Connection(http.client.HTTPConnection):
        def __init__(self, host, address, timeout):
            super().__init__(host, port=443, timeout=timeout)
            self.active_socket = client

        def connect(self):
            # Only the wire is local: request/response ownership is stdlib's
            # real implementation, including its Connection: close behavior.
            self.sock = client

        def getresponse(self):
            response = super().getresponse()
            responses.append(response)  # Retain it: GC cannot hide a leaked fd.
            return response

    monkeypatch.setattr(
        ingestion,
        "_public_addresses",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )
    monkeypatch.setattr(ingestion, "_PinnedHTTPSConnection", Connection)
    try:
        yield responses, original_fd
    finally:
        for response in responses:
            response.close()
        client.close()
        peer.close()


@pytest.mark.parametrize("stage", ["headers", "partial_body"])
def test_early_exit_closes_connection_close_response_and_underlying_fd(stdlib_transport, stage):
    responses, original_fd = stdlib_transport

    def abort_headers(_metadata):
        raise ingestion.DownloadError("intentional header abort")

    def abort_consumer(chunks):
        assert next(iter(chunks)) == b"abc"
        raise ingestion.DownloadError("intentional body abort")

    options = {"on_headers": abort_headers} if stage == "headers" else {"consume": abort_consumer}
    with pytest.raises(ingestion.DownloadError, match="intentional"):
        ingestion._download("https://example.com/law", deadline=time.monotonic() + 5, **options)
    assert len(responses) == 1 and responses[0].will_close
    assert responses[0].isclosed(), "stdlib response still owns its socket file"
    assert responses[0].fp is None
    with pytest.raises(OSError):
        os.fstat(original_fd)
