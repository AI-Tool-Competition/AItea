"""Bounded, deterministic model substitute used only by Docker smoke checks."""

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Provider(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def do_POST(self):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 < size <= 2 * 1024 * 1024:
            self.send_error(413)
            return
        payload = json.loads(self.rfile.read(size))
        if payload.get("model") != "smoke-local" or self.headers.get("Authorization") != "Bearer smoke-only":
            self.send_error(400)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for part in (
                "容器连接正常。",
                "正在检查流式输出。",
                "分段响应已经送达。",
                "这是一份隔离测试结果。",
            ):
                event = {"choices": [{"index": 0, "delta": {"content": part}, "finish_reason": None}]}
                self.wfile.write(("data: " + json.dumps(event, ensure_ascii=False) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.25)
            event = {
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            }
            self.wfile.write(("data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n").encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Provider).serve_forever()
