"""HTTP regression tests with a loopback listener and a fake Runner."""

from contextlib import nullcontext, redirect_stdout
import http.client
import io
from pathlib import Path
import json
import signal
import socket
import struct
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import config
from winrunner import (
    HTTPHandler,
    HTTPServer,
    ThreadingHTTPServer,
    serve_with_gui,
    service_logging,
)

TOKEN = "test-bearer-token"


class QuietHandler(HTTPHandler):
    def log_message(self, *args):
        pass


class TrackingServer(HTTPServer):
    def __init__(self, *args, **kwargs):
        self.worker_count = 0
        self.worker_started = threading.Event()
        self.worker_done = threading.Event()
        self.reported_peer: str | None = None
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        if self.reported_peer is not None:
            address = (self.reported_peer, address[1])
        return request, address

    def process_request_thread(self, request, client_address):
        self.worker_count += 1
        self.worker_started.set()
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.worker_done.set()


class HTTPRegressionTests(unittest.TestCase):
    def start_server(self, *, limit=64, reported_peer=None):
        with patch.object(config, "MAX_HTTP_CONNECTIONS", limit, create=True):
            self.server = TrackingServer(("127.0.0.1", 0), QuietHandler)
        self.server.reported_peer = reported_peer
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

    def request(self, method, path, *, auth=True, payload=None, key=None):
        conn = self.connect()
        try:
            headers = {"Authorization": "Bearer " + TOKEN} if auth else {}
            if key is not None:
                headers["Idempotency-Key"] = key
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            body = response.read()
            return response.status, json.loads(body) if body else body, response.headers
        finally:
            conn.close()

    def test_start_replay_scopes_keys_and_returns_original_response(self):
        self.start_server()
        self.runner.start_command.side_effect = [(202, {"id": "0"}), (200, {"id": "1"})]
        self.runner.start_action.return_value = (202, {"status": "preparing"})

        def post(path, payload, key="one"):
            status, body, _ = self.request("POST", path, payload=payload, key=key)
            return status, body

        payload = b'{"mode":"cmd","command":"echo test"}'
        self.assertEqual(post("/commands", payload), (202, {"id": "0"}))
        self.assertEqual(
            post("/commands", b'{"command":"echo test", "mode":"cmd"}'),
            (202, {"id": "0"}),
        )
        status, body = post("/commands", b'{"mode":"cmd","command":"echo other"}')
        self.assertEqual((status, body["error"]["code"]), (409, "idempotency_conflict"))
        self.assertEqual(post("/commands", payload, "two"), (200, {"id": "1"}))
        self.assertEqual(self.runner.start_command.call_count, 2)

        original = post("/projects/Demo/actions", b"{}")
        self.assertEqual(original, (202, {"status": "preparing"}))
        self.assertEqual(post("/projects/demo/actions", b"{}"), original)
        self.assertEqual(post("/projects/Other/actions", b"{}"), original)
        self.assertEqual(self.runner.start_action.call_count, 2)

    def test_concurrent_start_replay_never_submits_twice(self):
        self.start_server()
        entered, release = threading.Event(), threading.Event()
        first = []

        def start(*args):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test timed out")
            return 202, {"id": "0"}

        self.runner.start_command.side_effect = start

        def submit():
            return self.request("POST", "/commands", payload=b"{}", key="one")[:2]

        worker = threading.Thread(target=lambda: first.append(submit()))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            status, body = submit()
            self.assertEqual(
                (status, body["error"]["code"]), (409, "idempotency_pending")
            )
        finally:
            release.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(first, [(202, {"id": "0"})])
        self.assertEqual(submit(), first[0])
        self.runner.start_command.assert_called_once()

    def test_integrated_window_reads_state_while_http_serves_and_stops_on_return(self):
        from winrunner_gui import Target, View

        with HTTPServer(("127.0.0.1", 0), HTTPHandler) as server:
            server.token = TOKEN.encode("ascii")
            server.runner = runner = Mock()
            server.cpu_sampler = Mock()
            runner.list_projects.return_value = [{"name": "demo"}]
            runner.list_commands.return_value = []
            self.port = server.server_address[1]

            runner.get_action.return_value = {"status": "running"}
            runner.get_command.return_value = {"command": "echo exact"}

            def window(snapshot, is_running):
                self.assertIs(threading.current_thread(), threading.main_thread())
                self.assertTrue(is_running())
                with patch("http.client.HTTPConnection") as connection:
                    state = snapshot(View("demo", Target("demo", "0")))
                connection.assert_not_called()
                self.assertEqual(state.selected.detail.data, {"command": "echo exact"})
                runner.get_command.assert_called_once_with(
                    "0",
                    {"stdout": 0, "stderr": 0},
                    32774,
                    name="demo",
                    include_input=True,
                )
                self.assertEqual(state.health.data, {"run_id": "demo-run"})
                self.assertEqual(state.projects.data, {"projects": [{"name": "demo"}]})
                self.assertEqual(state.commands.data, {"commands": []})
                runner.list_projects.assert_called_once_with()
                status, body, _ = self.request("GET", "/projects")
                self.assertEqual(
                    (status, body), (200, {"projects": [{"name": "demo"}]})
                )
                self.backend_is_running = is_running

            with (
                patch("winrunner.health_snapshot", return_value={"run_id": "demo-run"}),
                patch("winrunner_gui.run_integrated", side_effect=window) as gui,
            ):
                serve_with_gui(server)
            gui.assert_called_once()
            self.assertFalse(self.backend_is_running())

    def test_ctrl_c_during_integrated_http_start_stops_worker(self):
        with HTTPServer(("127.0.0.1", 0), HTTPHandler) as server:
            server.runner = Mock()
            started = threading.Thread.start
            workers = []

            def start_then_interrupt(worker):
                started(worker)
                workers.append(worker)
                signal.raise_signal(signal.SIGINT)

            with (
                patch.object(threading.Thread, "start", start_then_interrupt),
                patch("winrunner_gui.run_integrated") as gui,
            ):
                serve_with_gui(server)
            gui.assert_not_called()
            self.assertEqual(len(workers), 1)
            self.assertFalse(workers[0].is_alive())

    def test_project_creation_uses_path_name_and_logs_only_repository_host(self):
        self.start_server(reported_peer="203.0.113.42")
        self.runner.create.return_value = {"name": "Demo", "status": "idle"}
        payload = b'{"repo_url":"https://git.example.invalid/private-path"}'
        with self.assertLogs("winrunner", "INFO") as logs:
            status, body, _ = self.request("POST", "/projects/%44emo", payload=payload)
            self.assertTrue(self.server.worker_done.wait(3))
        self.assertEqual((status, body), (201, {"name": "Demo", "status": "idle"}))
        self.runner.create.assert_called_once_with("Demo", payload)
        text = "\n".join(logs.output)
        self.assertIn('project="Demo"', text)
        self.assertIn('repo_host="git.example.invalid"', text)
        self.assertNotIn("private-path", text)

    def test_request_metadata_is_bounded_redacted_and_appended(self):
        self.start_server(reported_peer="203.0.113.42")
        self.runner.start_command.return_value = (202, {"id": "z", "mode": "ps"})
        self.runner.start_action.return_value = (202, {})
        console = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            logfile = Path(folder) / "winrunner.log"
            logfile.write_text("old run\n", encoding="utf-8")
            with redirect_stdout(console), service_logging(logfile, TOKEN):
                cases = [
                    (
                        "/commands",
                        {
                            "mode": "ps",
                            "timeout": 0,
                            "command": "never log this command",
                        },
                    ),
                    (
                        "/projects/demo/actions",
                        {
                            "commit": "abc1234",
                            "interpreter": "python",
                            "script": TOKEN + "/" + "宽" * 98 + "\nsecond line",
                            "args": ["never log this argument"],
                        },
                    ),
                ]
                for route, body in cases:
                    self.server.worker_done.clear()
                    conn = self.connect()
                    try:
                        conn.request(
                            "POST",
                            route,
                            body=json.dumps(body),
                            headers={"Authorization": "Bearer " + TOKEN},
                        )
                        response = conn.getresponse()
                        self.assertEqual(response.status, 202)
                        response.read()
                    finally:
                        conn.close()
                    self.assertTrue(self.server.worker_done.wait(3))
            text = console.getvalue()
            self.assertEqual(logfile.read_text(encoding="utf-8"), "old run\n" + text)
        for marker in (
            "202 POST /commands",
            'mode="ps"',
            "timeout=0",
            'id="z"',
            "202 POST /projects/{id}/actions",
            'project="demo"',
            'commit="abc1234"',
            'interpreter="python"',
            'script="[redacted]',
        ):
            self.assertIn(marker, text)
        for secret in (TOKEN, "never log this command", "never log this argument"):
            self.assertNotIn(secret, text)
        self.assertTrue(
            all(len(line) <= 80 and line.isascii() for line in text.splitlines())
        )

    def test_loopback_requests_are_silent_in_console_and_file(self):
        self.start_server()
        console = io.StringIO()
        with tempfile.TemporaryDirectory() as folder:
            logfile = Path(folder) / "winrunner.log"
            with redirect_stdout(console), service_logging(logfile, TOKEN):
                for auth, expected in ((True, 200), (False, 401)):
                    self.server.worker_done.clear()
                    status, _, _ = self.request("GET", "/projects", auth=auth)
                    self.assertEqual(status, expected)
                    self.assertTrue(self.server.worker_done.wait(3))
                self.runner.list_projects.side_effect = RuntimeError("test failure")
                self.server.worker_done.clear()
                status, _, _ = self.request("GET", "/projects")
                self.assertEqual(status, 500)
                self.assertTrue(self.server.worker_done.wait(3))
            self.assertEqual(console.getvalue(), "")
            self.assertEqual(logfile.read_text(encoding="utf-8"), "")

    def test_rejects_over_budget_before_starting_worker_and_reuses_permit(self):
        self.start_server(limit=1)
        with socket.create_connection(("127.0.0.1", self.port), timeout=3):
            self.assertTrue(self.server.worker_started.wait(3))
            status, body, headers = self.request("GET", "/projects")
            self.assertEqual(status, 503)
            # GET may arrive after the bounded overload read on Windows.
            if body:
                self.assertEqual(body["error"]["code"], "server_busy")
                self.assertEqual(headers.get_content_type(), "application/json")
            else:
                self.assertEqual(headers["Content-Length"], "0")
            self.assertEqual(headers["Retry-After"], "1")
            self.assertEqual(headers["Connection"], "close")
            with socket.create_connection(
                ("127.0.0.1", self.port), timeout=3
            ) as waiting:
                # A saturated server must reply even before it gets a request line.
                with http.client.HTTPResponse(waiting) as response:
                    response.begin()
                    self.assertEqual(response.status, 503)
                    self.assertEqual(response.getheader("Retry-After"), "1")
                    self.assertEqual(response.read(), b"")
            with socket.create_connection(("127.0.0.1", self.port), timeout=3) as head:
                head.sendall(b"HEAD /projects HTTP/1.1\r\nHost: local\r\n\r\n")
                with head.makefile("rb") as response:
                    headers, body = response.read().split(b"\r\n\r\n", 1)
                self.assertIn(b"503 Service Unavailable", headers)
                self.assertEqual(body, b"")
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
            ("POST", "/projects", "GET"),
            ("PUT", "/projects/missing", "GET, POST, DELETE"),
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
        self.start_server(reported_peer="203.0.113.42")
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
                with patch("winrunner.LOG.exception") as log:
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
        with patch("winrunner.LOG.exception") as log:
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
        self.start_server(reported_peer="203.0.113.42")
        with (
            tempfile.TemporaryFile() as file,
            patch("winrunner.LOG.exception") as log,
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
