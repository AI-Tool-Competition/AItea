import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


async def exercise(middleware, chunks, headers=()):
    sent = []
    messages = iter(
        [
            {"type": "http.request", "body": part, "more_body": index + 1 < len(chunks)}
            for index, part in enumerate(chunks)
        ]
    )

    async def receive():
        return next(messages, {"type": "http.disconnect"})

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/upload",
        "raw_path": b"/upload",
        "query_string": b"",
        "headers": list(headers),
        "server": ("test", 80),
        "client": ("client", 1),
    }
    await middleware(scope, receive, send)
    return sent


async def body_app(scope, receive, send):
    body = bytearray()
    while True:
        message = await receive()
        body.extend(message.get("body", b""))
        if not message.get("more_body", False):
            break
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": bytes(body)})


class RequestLimitsTests(unittest.TestCase):
    def setUp(self):
        self.middleware = importlib.import_module("app.request_limits").RequestLimitMiddleware

    def test_chunked_json_is_limited_without_content_length(self):
        sent = asyncio.run(
            exercise(
                self.middleware(body_app, json_limit=8),
                [b"12345", b"67890"],
                [(b"content-type", b"application/json")],
            )
        )
        self.assertEqual([m["status"] for m in sent if m["type"] == "http.response.start"], [413])
        self.assertEqual(json.loads(sent[-1]["body"])["error"]["code"], "request_too_large")

    def test_incorrect_content_length_does_not_bypass_actual_limit(self):
        sent = asyncio.run(
            exercise(
                self.middleware(body_app, json_limit=8), [b"12345", b"67890"], [(b"content-length", b"1")]
            )
        )
        self.assertEqual(sent[0]["status"], 413)

    def test_exact_limit_preserves_all_chunks(self):
        sent = asyncio.run(exercise(self.middleware(body_app, json_limit=8), [b"1234", b"5678"]))
        self.assertEqual(sent[0]["status"], 200)
        self.assertEqual(sent[-1]["body"], b"12345678")

    def test_oversized_declared_length_does_not_invoke_application(self):
        invoked = []

        async def application(scope, receive, send):
            invoked.append(True)

        sent = asyncio.run(exercise(self.middleware(application), [], [(b"content-length", b"9" * 5000)]))
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(invoked, [])

    def test_already_started_response_is_terminated_without_duplicate_headers(self):
        async def application(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await receive()

        sent = asyncio.run(exercise(self.middleware(application, json_limit=2), [b"long"]))
        self.assertEqual(len([m for m in sent if m["type"] == "http.response.start"]), 1)
        self.assertFalse(sent[-1]["more_body"])

    def test_framework_error_is_remapped_once_to_413(self):
        from starlette.formparsers import MultiPartException

        async def catching_app(scope, receive, send):
            try:
                await receive()
            except MultiPartException:
                await send({"type": "http.response.start", "status": 400, "headers": []})
                await send({"type": "http.response.body", "body": b"framework error"})

        sent = asyncio.run(exercise(self.middleware(catching_app, json_limit=2), [b"long"]))
        self.assertEqual([m["status"] for m in sent if m["type"] == "http.response.start"], [413])
        self.assertEqual(len(sent), 2)

    def test_multipart_overflow_closes_partial_spooled_files(self):
        from fastapi import FastAPI, File, UploadFile
        from starlette import formparsers

        app = FastAPI()

        @app.post("/upload")
        async def upload(file: UploadFile = File(...)):
            return {"size": len(await file.read())}

        payload = (
            b'--b\r\nContent-Disposition: form-data; name="file"; filename="a.txt"\r\nContent-Type: text/plain\r\n\r\n'
            + b"x" * 4096
            + b"\r\n--b--\r\n"
        )
        files = []
        factory = tempfile.SpooledTemporaryFile
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:

            def temporary_file(**kwargs):
                kwargs["max_size"] = 64
                file = factory(dir=directory, **kwargs)
                files.append(file)
                return file

            with patch.object(formparsers, "SpooledTemporaryFile", temporary_file):
                sent = asyncio.run(
                    exercise(
                        self.middleware(app, multipart_limit=1024),
                        [payload[i : i + 256] for i in range(0, len(payload), 256)],
                        [(b"content-type", b"multipart/form-data; boundary=b")],
                    )
                )
            self.assertEqual(sent[0]["status"], 413)
            self.assertTrue(files)
            self.assertTrue(all(file.closed for file in files))


if __name__ == "__main__":
    unittest.main()
