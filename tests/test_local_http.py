"""Real loopback-only regression: no proxy or redirect may receive model data."""
from __future__ import annotations

import json
import os
import threading
import unittest
import urllib.request
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from forge8.inference import ChatRequest, Message, OpenAITransport, TransportError
from forge8.server import _health_probe


_MARKER = "TEST_ONLY_SYNTHETIC_MARKER_NOT_A_SECRET"
_REPLY = json.dumps({
    "status": "ok",
    "choices": [{"message": {"content": "local reply"}, "finish_reason": "stop"}],
}).encode()


class _LocalHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(2)

    def do_GET(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.seen.append((self.command, self.path, self.headers.get("Authorization"), body))
        redirect = self.server.redirect
        self.send_response(redirect[0] if redirect else 200)
        if redirect:
            self.send_header("Location", redirect[1])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(_REPLY)))
        self.end_headers()
        self.wfile.write(_REPLY)

    do_POST = do_GET

    def log_message(self, *_args):
        pass


class LocalHttpTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Restore process environment and urllib's cached opener after every test.
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch("urllib.request._opener", None))
        self.model, self.endpoint = self.server()
        self.sink, self.sink_url = self.server()

    def server(self):
        server = HTTPServer(("127.0.0.1", 0), _LocalHandler)
        server.seen, server.redirect = [], None
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(3)
            self.assertFalse(worker.is_alive(), "loopback HTTP worker survived cleanup")

        self.stack.callback(close)
        return server, f"http://127.0.0.1:{server.server_port}"

    def proxy_environment(self):
        return patch.dict(os.environ, {
            "HTTP_PROXY": self.sink_url, "http_proxy": self.sink_url,
            "NO_PROXY": "", "no_proxy": "",
        })

    def chat(self):
        transport = OpenAITransport(self.endpoint, api_key=_MARKER, timeout_seconds=2)
        return transport.chat(ChatRequest("fake", (Message("user", "synthetic source only"),)))

    def assert_direct(self, method, path):
        self.assertEqual(len(self.model.seen), 1)
        self.assertEqual(self.model.seen[0][:3], (method, path, f"Bearer {_MARKER}"))
        self.assertEqual(self.sink.seen, [])

    def test_chat_bypasses_environment_proxy(self):
        with self.proxy_environment():
            self.assertEqual(self.chat().content, "local reply")
        self.assert_direct("POST", "/v1/chat/completions")
        self.assertIn(b"synthetic source only", self.model.seen[0][3])

    def test_health_bypasses_environment_proxy(self):
        with self.proxy_environment():
            self.assertEqual(_health_probe(self.endpoint, _MARKER, 2), (True, None))
        self.assert_direct("GET", "/health")

    def test_chat_does_not_follow_loopback_redirect(self):
        for status in (302, 307):
            with self.subTest(status=status):
                self.model.seen.clear()
                self.model.redirect = (status, self.sink_url + "/capture")
                with self.assertRaisesRegex(TransportError, f"HTTP {status}") as caught:
                    self.chat()
                self.assertNotIn(_MARKER, str(caught.exception))
                self.assert_direct("POST", "/v1/chat/completions")

    def test_health_does_not_follow_loopback_redirect(self):
        for status in (302, 307):
            with self.subTest(status=status):
                self.model.seen.clear()
                self.model.redirect = (status, self.sink_url + "/capture")
                ready, reason = _health_probe(self.endpoint, _MARKER, 2)
                self.assertFalse(ready)
                self.assertIn(f"HTTP {status}", reason)
                self.assertNotIn(_MARKER, reason)
                self.assert_direct("GET", "/health")

    def test_legacy_urlopen_control_reproduces_proxy_routing_locally(self):
        target = self.endpoint + "/v1/chat/completions"
        request = urllib.request.Request(target, data=b"synthetic source only",
                                         headers={"Authorization": f"Bearer {_MARKER}"})
        with self.proxy_environment(), urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(self.model.seen, [])
        self.assertEqual(len(self.sink.seen), 1)
        self.assertEqual(self.sink.seen[0], ("POST", target, f"Bearer {_MARKER}", b"synthetic source only"))

    def test_legacy_urlopen_control_reproduces_redirect_forwarding_locally(self):
        self.model.redirect = (302, self.sink_url + "/capture")
        request = urllib.request.Request(self.endpoint + "/health", headers={"Authorization": f"Bearer {_MARKER}"})
        with urllib.request.urlopen(request, timeout=2) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.sink.seen), 1)
        self.assertEqual(self.sink.seen[0][1:3], ("/capture", f"Bearer {_MARKER}"))


if __name__ == "__main__":
    unittest.main()
