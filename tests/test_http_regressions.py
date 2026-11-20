"""HTTP regression tests with a loopback listener and a fake Runner."""

from contextlib import nullcontext
import http.client
import json
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import config
from winrunner import HTTPHandler, HTTPServer, ThreadingHTTPServer

TOKEN = "test-bearer-token"


class QuietHandler(HTTPHandler):
    def log_message(self, *args):
        pass


class TrackingServer(HTTPServer):
    def __init__(self, *args, **kwargs):
        self.worker_count = 0
        self.worker_started = threading.Event()
        self.worker_done = threading.Event()
        super().__init__(*args, **kwargs)

    def process_request_thread(self, request, client_address):
        self.worker_count += 1
        self.worker_started.set()
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.worker_done.set()


class HTTPRegressionTests(unittest.TestCase):
    def start_server(self, *, limit=64):
        with patch.object(config, "MAX_HTTP_CONNECTIONS", limit, create=True):
            self.server = TrackingServer(("127.0.0.1", 0), QuietHandler)
        self.server.token = TOKEN.encode("ascii")
        self.server.runner = self.runner = Mock()
        self.runner.list_projects.return_value = []
        self.port = self.server.server_address[1]
        thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.addCleanup(self.server.server_close)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)

    def connect(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)

    def request(self, method, path, *, auth=True):
        conn = self.connect()
        try:
            headers = {"Authorization": "Bearer " + TOKEN} if auth else {}
            conn.request(method, path, headers=headers)
            response = conn.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body else body, response.headers
        finally:
            conn.close()

    def test_rejects_over_budget_before_starting_worker_and_reuses_permit(self):
        self.start_server(limit=1)
        with socket.create_connection(("127.0.0.1", self.port), timeout=3):
            self.assertTrue(self.server.worker_started.wait(3))
            status, body, headers = self.request("GET", "/projects")
            self.assertEqual(status, 503)
            self.assertEqual(body["error"]["code"], "server_busy")
            self.assertEqual(headers["Retry-After"], "1")
            self.assertEqual(headers["Connection"], "close")
            self.assertEqual(headers.get_content_type(), "application/json")
            with socket.create_connection(
                ("127.0.0.1", self.port), timeout=3
            ) as waiting:
                # A saturated server must reply even before it gets a request line.
                with http.client.HTTPResponse(waiting) as response:
                    response.begin()
                    self.assertEqual(response.status, 503)
                    self.assertEqual(
                        json.loads(response.read())["error"]["code"], "server_busy"
                    )
            self.assertEqual(self.server.worker_count, 1)
            self.assertEqual(self.runner.mock_calls, [])
        self.assertTrue(self.server.worker_done.wait(3))

        status, body, _ = self.request("GET", "/projects")
        self.assertEqual((status, body), (200, {"projects": []}))
        self.assertEqual(self.server.worker_count, 2)

    def test_client_reset_before_request_or_headers_finish_is_quiet(self):
        self.start_server(limit=1)
        linger_format = "HH" if sys.platform == "win32" else "ii"
        for fragment in (b"GET /pro", b"GET /projects HTTP/1.1\r\nHost: local"):
            with self.subTest(fragment=fragment):
                self.server.worker_started.clear()
                self.server.worker_done.clear()
                with patch.object(self.server, "handle_error") as unexpected:
                    with socket.create_connection(
                        ("127.0.0.1", self.port), timeout=3
                    ) as conn:
                        conn.setsockopt(
                            socket.SOL_SOCKET,
                            socket.SO_LINGER,
                            struct.pack(linger_format, 1, 0),
                        )
                        self.assertTrue(self.server.worker_started.wait(3))
                        conn.sendall(fragment)
                        # Closing without a graceful shutdown sends TCP RST.
                    self.assertTrue(self.server.worker_done.wait(3))
                    unexpected.assert_not_called()
        self.assertEqual(self.runner.mock_calls, [])
        status, body, _ = self.request("GET", "/projects")
        self.assertEqual((status, body), (200, {"projects": []}))

    def test_releases_connection_permit_when_thread_start_fails(self):
        self.start_server(limit=1)
        failed = threading.Event()
        with (
            patch.object(
                ThreadingHTTPServer,
                "process_request",
                side_effect=RuntimeError("start failed"),
            ),
            patch.object(
                self.server, "handle_error", side_effect=lambda *_: failed.set()
            ),
            socket.create_connection(("127.0.0.1", self.port), timeout=3) as conn,
        ):
            self.assertTrue(failed.wait(3))
            self.assertEqual(conn.recv(1), b"")
            self.assertFalse(self.server.worker_started.is_set())
        status, body, _ = self.request("GET", "/projects")
        self.assertEqual((status, body), (200, {"projects": []}))

    def test_known_routes_reject_unsupported_methods_after_auth(self):
        self.start_server()
        cases = [
            ("POST", "/health", "GET"),
            ("OPTIONS", "/projects", "GET, POST"),
            ("POST", "/projects/missing", "GET, DELETE"),
            ("PATCH", "/projects/missing/actions", "GET, POST, DELETE"),
            ("DELETE", "/projects/missing/files?path=one&path=two", "GET"),
            ("PUT", "/commands", "GET, POST"),
            ("EXTENSION", "/commands/missing", "GET, DELETE"),
        ]
        for method, path, allow in cases:
            with self.subTest(method=method, path=path):
                status, body, headers = self.request(method, path)
                self.assertEqual(status, 405)
                self.assertEqual(body["error"]["code"], "method_not_allowed")
                self.assertEqual(headers["Allow"], allow)
        status, body, headers = self.request(
            "EXTENSION", "/projects/missing/actions", auth=False
        )
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertIsNone(headers["Allow"])
        self.assertEqual(self.runner.mock_calls, [])

    def test_head_is_bodyless_and_unknown_path_is_not_a_method_error(self):
        self.start_server()
        for path, auth, expected, allow in [
            ("/health", True, 405, "GET"),
            ("/unknown", True, 404, None),
            ("/projects", False, 401, None),
        ]:
            with self.subTest(path=path):
                # Read the raw response so http.client cannot mask an erroneous HEAD body.
                with socket.create_connection(
                    ("127.0.0.1", self.port), timeout=3
                ) as conn:
                    request = f"HEAD {path} HTTP/1.1\r\nHost: localhost\r\n"
                    if auth:
                        request += f"Authorization: Bearer {TOKEN}\r\n"
                    conn.sendall((request + "\r\n").encode("ascii"))
                    with conn.makefile("rb") as wire:
                        headers, sep, body = wire.read().partition(b"\r\n\r\n")
                self.assertTrue(sep)
                self.assertEqual(int(headers.split(b" ", 2)[1]), expected)
                allow_line = [
                    line
                    for line in headers.split(b"\r\n")
                    if line.startswith(b"Allow:")
                ]
                self.assertEqual(
                    allow_line, [f"Allow: {allow}".encode("ascii")] if allow else []
                )
                self.assertEqual(body, b"")

        for method in ("GET", "DELETE", "EXTENSION"):
            with self.subTest(method=method):
                status, body, headers = self.request(
                    method, "/projects/demo/unknown?x=1&x=2"
                )
                self.assertEqual(status, 404)
                self.assertEqual(body["error"]["code"], "not_found")
                self.assertIsNone(headers["Allow"])
        self.assertEqual(self.runner.mock_calls, [])

    def test_only_windows_sharing_and_lock_violations_on_files_are_busy(self):
        self.start_server()
        path = "/projects/demo/files?path=artifact.txt"
        for code in (32, 33):
            with self.subTest(winerror=code):
                error = PermissionError("file occupied")
                error.winerror = code
                self.runner.file_entry.side_effect = error
                status, body, _ = self.request("GET", path)
                self.assertEqual(status, 409)
                self.assertEqual(body["error"]["code"], "file_busy")

                self.runner.list_projects.side_effect = error
                with patch("winrunner.traceback.print_exc") as log:
                    status, body, _ = self.request("GET", "/projects")
                    log.assert_called_once()
                self.assertEqual(status, 500)
                self.assertEqual(body["error"]["code"], "internal_error")

        self.runner.file_entry.side_effect = FileNotFoundError()
        status, body, _ = self.request("GET", path)
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "not_found")

        error = PermissionError("access denied")
        error.winerror = 5
        self.runner.file_entry.side_effect = None
        entry = Mock(is_dir=True)
        entry.list_entries.side_effect = error
        self.runner.file_entry.return_value = nullcontext(entry)
        with patch("winrunner.traceback.print_exc") as log:
            status, body, _ = self.request("GET", path)
            log.assert_called_once()
        self.assertEqual(status, 500)
        self.assertEqual(body["error"]["code"], "internal_error")

    def test_file_cancel_shuts_down_only_its_connection(self):
        self.start_server()
        reading, release = threading.Event(), threading.Event()

        def read(_size):
            reading.set()
            release.wait(6)
            return b""

        with tempfile.TemporaryFile() as file:
            file.write(b"test data")
            file.flush()
            reader = Mock(wraps=file)
            reader.read.side_effect = read
            entry = Mock(is_dir=False)
            entry.open_file.return_value = nullcontext(reader)
            self.runner.file_entry.return_value = nullcontext(entry)
            conn = self.connect()
            try:
                conn.request(
                    "GET",
                    "/projects/demo/files?path=artifact.txt",
                    headers={"Authorization": "Bearer " + TOKEN},
                )
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertTrue(reading.wait(3))
                cancel_read = self.runner.file_entry.call_args.kwargs["cancel_read"]
                cancel_read()
                with self.assertRaises(http.client.IncompleteRead):
                    response.read()
                status, body, _ = self.request("GET", "/projects")
                self.assertEqual((status, body), (200, {"projects": []}))
            finally:
                release.set()
                conn.close()

    def test_file_read_error_after_headers_is_logged_and_truncates_response(self):
        self.start_server()
        with (
            tempfile.TemporaryFile() as file,
            patch("winrunner.traceback.print_exc") as log,
        ):
            file.write(b"test data")
            file.flush()
            reader = Mock(wraps=file)
            reader.read.side_effect = OSError("read failed")
            entry = Mock(is_dir=False)
            entry.open_file.return_value = nullcontext(reader)
            self.runner.file_entry.return_value = nullcontext(entry)
            conn = self.connect()
            try:
                conn.request(
                    "GET",
                    "/projects/demo/files?path=artifact.txt",
                    headers={"Authorization": "Bearer " + TOKEN},
                )
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Length"], "9")
                with self.assertRaises(http.client.IncompleteRead) as raised:
                    response.read()
                self.assertEqual(raised.exception.partial, b"")
                log.assert_called_once()
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
