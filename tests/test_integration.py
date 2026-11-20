"""Windows end-to-end tests: python -m unittest discover -s tests -v.

The project tests serve a tiny HTTPS Git repository on loopback; they need Git
and OpenSSL (included with Git for Windows), but do not contact public hosts.
"""

import base64
import concurrent.futures
import contextlib
import ctypes
from functools import partial
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"succeeded", "failed", "cancelled"}
ACTIVE = {"preparing", "running", "cancelling"}
TREE_SCRIPT = """\
import os, subprocess, sys, time
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
print(f'TREE {os.getpid()} {child.pid}', flush=True)
time.sleep(90)
"""
ACTION_SCRIPT = """\
import json, os, pathlib, sys, time
if sys.argv[1:] == ['--tree']:
    exec(pathlib.Path('tree.py').read_text(encoding='utf-8'))
else:
    pathlib.Path('action output.txt').write_bytes(b'new data')
    os.write(1, json.dumps(sys.argv[1:]).encode('ascii'))
    os.write(2, b'action-stderr')
    gate = pathlib.Path(sys.argv[2]) if sys.argv[1:2] == ['--gate'] else pathlib.Path('release')
    deadline = time.monotonic() + 60
    while not gate.exists():
        if time.monotonic() >= deadline:
            sys.exit(97)
        time.sleep(.05)
"""


def ps_quote(text):
    return "'" + str(text).replace("'", "''") + "'"


def log_bytes(task, stream):
    return base64.b64decode(task[stream]["data_base64"], validate=True)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RunnerCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="winrunner-integration-")
        self.addCleanup(temp.cleanup)
        self.scratch = Path(temp.name)
        self.data = self.scratch / "runner-data"
        self.work = self.scratch / "test files"
        self.work.mkdir()
        self.token = secrets.token_hex(24)
        self.port = free_port()
        self.install = self.scratch / "app"
        self.install.mkdir()
        for name in ("config.py", "gitrepo.py", "runner.py", "winfiles.py", "winprocess.py", "winrunner.py"):
            shutil.copyfile(ROOT / name, self.install / name)
        self.settings = self.install / "config.py"
        with self.settings.open("a", encoding="utf-8") as settings:
            settings.write(f"\nTOKEN = {self.token!r}\nHOST = '127.0.0.1'\nPORT = {self.port}\n"
                           f"DATA_DIR = Path({str(self.data)!r})\n")
        self.server = None
        self.addCleanup(self.stop_runner)
        self.start_runner()

    def environment(self):
        return {key: value for key, value in os.environ.items() if not key.upper().startswith("RUNNER_")}

    def start_runner(self):
        self.diagnostics = self.scratch / "runner.log"
        with self.diagnostics.open("ab") as out:
            self.server = subprocess.Popen(
                [sys.executable, "winrunner.py"], cwd=self.install, env=self.environment(),
                stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        deadline = time.monotonic() + 17
        while time.monotonic() < deadline and self.server.poll() is None:
            try:
                if self.request("GET", "/health", timeout=1)[0] == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(.05)
        self.fail("runner did not start:\n" + self.diagnostics.read_text(errors="replace"))

    def stop_runner(self):
        if self.server is not None:
            if self.server.poll() is None:
                self.server.kill()
            self.server.wait(timeout=10)
            self.server = None

    def request(self, method, path, payload=None, *, auth=True, headers=None, timeout=35):
        headers = dict(headers or {})
        if auth is True:
            headers["Authorization"] = "Bearer " + self.token
        elif auth is not False:
            headers["Authorization"] = auth
        if payload is not None:
            headers["Content-Type"] = "application/json"
            if not isinstance(payload, bytes):
                payload = json.dumps(payload).encode("utf-8")
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=payload, headers=headers)
            response = conn.getresponse()
            body = response.read()
            if response.headers.get_content_type() == "application/json" and body:
                body = json.loads(body)
            return response.status, body, response.headers
        finally:
            conn.close()

    def call(self, status, method, path, payload=None, **kwargs):
        actual, body, _ = self.request(method, path, payload, **kwargs)
        self.assertEqual(actual, status, (method, path, body))
        if status == 204:
            self.assertEqual(body, b"")
        elif status >= 400:
            self.assertIsInstance(body["error"]["code"], str)
            self.assertIsInstance(body["error"]["message"], str)
        return body

    def poll(self, endpoint, predicate, *, query="", seconds=15):
        deadline = time.monotonic() + seconds
        while True:
            snapshot = self.call(200, "GET", endpoint + query, timeout=5)
            if predicate(snapshot):
                return snapshot
            if time.monotonic() >= deadline:
                self.fail(f"condition not met for {endpoint}: {snapshot!r}")
            time.sleep(.05)

    def finished(self, endpoint, *, expected="succeeded"):
        task = self.poll(endpoint, lambda item: item["status"] in TERMINAL,
                         query="?stdout_offset=0&stderr_offset=0")
        self.assertEqual(task["status"], expected, task)
        return task

    def gated_ps(self, gate, stdout=b"out|", stderr=b"err!"):
        def write(stream, data):
            numbers = ",".join(str(n) for n in data)
            return (f"$stream = [Console]::OpenStandard{stream}()\n"
                    f"$buf = [byte[]]@({numbers})\n"
                    "$stream.Write($buf, 0, $buf.Length)\n$stream.Flush()\n")
        return (write("Output", stdout) + write("Error", stderr) +
                "$deadline = [DateTime]::UtcNow.AddSeconds(60)\n"
                f"while (-not [IO.File]::Exists({ps_quote(gate)})) {{\n"
                "    if ([DateTime]::UtcNow -gt $deadline) { exit 97 }\n"
                "    [Threading.Thread]::Sleep(50)\n"
                "}\n" + write("Output", b"done"))

    @contextlib.contextmanager
    def live_tree(self, endpoint):
        query = "?stdout_offset=0"
        pattern = re.compile(rb"TREE (\d+) (\d+)\r?\n")
        task = self.poll(endpoint, lambda item: pattern.search(log_bytes(item, "stdout")),
                         query=query)
        self.assertEqual(task["status"], "running", task)
        pids = [int(pid) for pid in pattern.search(log_bytes(task, "stdout")).groups()]
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handles = []
        try:
            for pid in pids:
                handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                handles.append(handle)
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258,
                                 f"PID {pid} already exited")

            def assert_stopped():
                for pid, handle in zip(pids, handles):
                    self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0,
                                     f"PID {pid} did not exit")

            yield assert_stopped
        finally:
            for pid, handle in reversed(list(zip(pids, handles))):
                if kernel.WaitForSingleObject(handle, 0) == 258:
                    # Prevent bugs in the runner from leaving fixtures running.
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=10,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                kernel.CloseHandle(handle)


@unittest.skipUnless(os.name == "nt", "requires Windows shell and Job Objects")
class CommandIntegrationTests(RunnerCase):
    def test_placeholder_cannot_be_overridden_by_old_environment_setting(self):
        self.stop_runner()
        with self.settings.open("a", encoding="utf-8") as settings:
            settings.write('\nTOKEN = "123456"\n')
        result = subprocess.run(
            [sys.executable, "winrunner.py"], cwd=self.install,
            env=dict(self.environment(), RUNNER_TOKEN="legacy-setting"),
            stdin=subprocess.DEVNULL, capture_output=True, timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"Set TOKEN in config.py", result.stderr)

    def test_health_and_authentication_precede_request_processing(self):
        for method, path, payload in (("GET", "/health", None),
                                      ("GET", "/commands", None),
                                      ("POST", "/commands", b"{"),
                                      ("DELETE", "/commands/nonexistent", None),
                                      ("POST", "/projects", b"{")):
            for auth in (False, "Bearer incorrect", f"Basic {self.token}"):
                with self.subTest(method=method, path=path, auth=auth):
                    self.call(401, method, path, payload, auth=auth)
        health = self.call(200, "GET", "/health")
        self.assertGreater(health["cpu"]["logical_cores"], 0)
        cpu = health["cpu"]["usage_percent"]
        if cpu is not None:
            self.assertLessEqual(0, cpu)
            self.assertLessEqual(cpu, 100)
        for sensor in ("memory", "disk"):
            self.assertGreater(health[sensor]["total_bytes"], 0)
            self.assertLessEqual(0, health[sensor]["available_bytes"])
            self.assertLessEqual(health[sensor]["available_bytes"], health[sensor]["total_bytes"])
        self.assertGreaterEqual(health["uptime_seconds"], 0)
        self.assertEqual(self.call(200, "GET", "/commands"), {"commands": []})

    def test_cmd_and_request_validation(self):
        valid = {"mode": "cmd", "command": "echo runner-cmd-marker"}
        for bad in ({"mode": "bad"}, {"command": ["echo nope"]}, {"timeout": True},
                    {"timeout": -1}, {"timeout": "1"}, {"timeout": None},
                    {"timeout": float("inf")}, {"timeout": float("nan")}):
            with self.subTest(bad=bad):
                self.call(400, "POST", "/commands", dict(valid, **bad))
        self.call(400, "POST", "/commands", b"{")
        self.assertEqual(self.call(200, "GET", "/commands")["commands"], [])
        task = self.call(200, "POST", "/commands", dict(valid, timeout=8))
        self.assertEqual(task["mode"], "cmd")
        endpoint = "/commands/" + task["id"]
        snapshot = self.call(200, "GET", endpoint)
        self.assertNotIn("stdout", snapshot)
        self.assertNotIn("stderr", snapshot)
        task = self.finished(endpoint)
        self.assertEqual(task["phase"], "run")
        self.assertEqual(task["output_source"], "process")
        self.assertEqual(task["exit_code"], 0)
        self.assertIsNone(task["error"])
        self.assertEqual(log_bytes(task, "stdout").strip(), b"runner-cmd-marker")
        failed = self.call(200, "POST", "/commands",
                           {"mode": "cmd", "command": "exit /b 7", "timeout": 8})
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["exit_code"], 7)

    def test_zero_timeout_multiline_powershell_and_concurrent_incremental_logs(self):
        def launch(label):
            gate = self.work / (label + " release")
            out = (label + "-stdout").encode("ascii")
            err = (label + "-stderr").encode("ascii")
            task = self.call(202, "POST", "/commands", {
                "mode": "ps", "command": self.gated_ps(gate, out, err), "timeout": 0,
            })
            self.assertIn(task["status"], ACTIVE)
            self.assertEqual(task["mode"], "ps")
            return "/commands/" + task["id"], task["id"], gate, out, err

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            tasks = list(pool.map(launch, ("first", "second")))

        self.assertNotEqual(tasks[0][1], tasks[1][1])
        for endpoint, _, _, out, err in tasks:
            # Both pipes must be visible before the process exits, without newlines.
            self.poll(endpoint, lambda item: item["status"] == "running" and
                      item["stdout_bytes"] == len(out) and item["stderr_bytes"] == len(err))
            task = self.call(200, "GET", endpoint + "?stdout_offset=0&stderr_offset=0&limit=2")
            for stream, expected in (("stdout", out), ("stderr", err)):
                self.assertEqual(log_bytes(task, stream), expected[:2])
                self.assertEqual(task[stream]["next_offset"], 2)
            tail = self.call(200, "GET", endpoint + "?stdout_offset=2")
            self.assertEqual(log_bytes(tail, "stdout"), out[2:])
            self.assertEqual(tail["stdout"]["next_offset"], len(out))
            self.assertNotIn("stderr", tail)
            end = self.call(200, "GET", endpoint + f"?stdout_offset={len(out)}")
            self.assertEqual(log_bytes(end, "stdout"), b"")
            self.assertEqual(end["stdout"]["next_offset"], len(out))
            for query in (f"stdout_offset={len(out)+1}", "stderr_offset=-1", "limit=0",
                          "stdout_offset=0&limit=1048577"):
                self.call(400, "GET", f"{endpoint}?{query}")

        listed = self.call(200, "GET", "/commands")["commands"]
        self.assertEqual({item["id"] for item in listed}, {item[1] for item in tasks})
        for item in listed:
            self.assertEqual(item["status"], "running")
            self.assertNotIn("stdout", item)
            self.assertNotIn("stderr", item)
        first, second = tasks
        first[2].touch()
        self.assertEqual(log_bytes(self.finished(first[0]), "stdout"), first[3] + b"done")
        self.assertEqual(self.call(200, "GET", second[0])["status"], "running")
        second[2].touch()
        self.assertEqual(log_bytes(self.finished(second[0]), "stdout"), second[3] + b"done")

    def test_explicit_and_default_timeout_wait_without_stopping_tasks(self):
        for label, extra, lower, upper in (("explicit", {"timeout": 1}, .85, 8),
                                            ("default", {}, 9, 25)):
            with self.subTest(timeout=label):
                gate = self.work / label
                started = time.monotonic()
                task = self.call(202, "POST", "/commands", dict(
                    {"mode": "ps", "command": self.gated_ps(gate)}, **extra))
                elapsed = time.monotonic() - started
                self.assertGreaterEqual(elapsed, lower, f"{label}: {elapsed:.2f}s")
                self.assertLess(elapsed, upper, f"{label}: {elapsed:.2f}s")
                self.assertIn(task["status"], ACTIVE)
                gate.touch()
                finished = self.finished("/commands/" + task["id"])
                self.assertEqual(log_bytes(finished, "stdout"), b"out|done")
                self.assertEqual(log_bytes(finished, "stderr"), b"err!")

    def launch_tree_command(self):
        script = self.work / "tree fixture.py"
        script.write_text(TREE_SCRIPT, encoding="utf-8")
        command = f"& {ps_quote(sys.executable)} -u {ps_quote(script)}"
        task = self.call(202, "POST", "/commands", {
            "mode": "ps", "command": command, "timeout": 0,
        })
        return "/commands/" + task["id"]

    def test_delete_terminates_the_process_tree_and_removes_the_record(self):
        endpoint = self.launch_tree_command()
        gate = self.work / "independent command release"
        other = self.call(202, "POST", "/commands", {
            "mode": "ps", "command": self.gated_ps(gate), "timeout": 0,
        })
        other_endpoint = "/commands/" + other["id"]
        with self.live_tree(endpoint) as assert_stopped:
            self.call(204, "DELETE", endpoint)
            assert_stopped()
        self.call(404, "GET", endpoint)
        self.call(404, "DELETE", endpoint)
        self.assertIn(self.call(200, "GET", other_endpoint)["status"], ACTIVE)
        gate.touch()
        self.finished(other_endpoint)
        self.call(204, "DELETE", other_endpoint)
        self.assertEqual(self.call(200, "GET", "/commands")["commands"], [])

    def test_restart_terminates_live_processes_and_does_not_restore_commands(self):
        endpoint = self.launch_tree_command()
        with self.live_tree(endpoint) as assert_stopped:
            self.stop_runner()
            assert_stopped()
        self.start_runner()
        self.call(404, "GET", endpoint)
        self.assertEqual(self.call(200, "GET", "/commands")["commands"], [])
        self.call(200, "GET", "/health")


class GitHTTPHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, _format, *args):
        pass


@unittest.skipUnless(os.name == "nt", "requires Windows shell and Job Objects")
class ProjectIntegrationTests(RunnerCase):
    @classmethod
    def setUpClass(cls):
        cls.git = shutil.which("git")
        if not cls.git:
            raise unittest.SkipTest("Git for Windows is not installed")
        openssl = shutil.which("openssl")
        if not openssl:
            openssl = Path(cls.git).parent.parent / "usr" / "bin" / "openssl.exe"
            if not openssl.is_file():
                raise unittest.SkipTest("OpenSSL is needed for the local HTTPS Git fixture")
        temp = tempfile.TemporaryDirectory(prefix="winrunner-https-git-")
        cls.addClassCleanup(temp.cleanup)
        fixture = Path(temp.name)
        global_config = fixture / "empty.gitconfig"
        global_config.touch()
        cls.cert = fixture / "cert.pem"
        key = fixture / "key.pem"
        cls.git_env = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith("GIT_")}
        cls.git_env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=str(global_config),
                           GIT_SSL_CAINFO=str(cls.cert), GIT_CONFIG_COUNT="2",
                           GIT_CONFIG_KEY_0="http.sslBackend", GIT_CONFIG_VALUE_0="openssl",
                           GIT_CONFIG_KEY_1="http.proxy", GIT_CONFIG_VALUE_1="")

        def checked(args):
            proc = subprocess.run(args, env=cls.git_env, capture_output=True, timeout=20)
            if proc.returncode != 0:
                raise RuntimeError(f"fixture command failed: {args!r}\n{proc.stderr!r}")
            return proc.stdout.strip().decode("ascii", errors="replace")

        checked([os.fspath(openssl), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                 "-days", "1", "-subj", "/CN=localhost", "-addext",
                 "subjectAltName=IP:127.0.0.1", "-keyout", str(key), "-out", str(cls.cert)])
        src = fixture / "source"
        public = fixture / "public"
        public.mkdir()
        cls.remote = public / "fixture.git"
        checked([cls.git, "init", "--initial-branch=fixture-main", str(src)])
        checked([cls.git, "init", "--bare", "--initial-branch=fixture-main", str(cls.remote)])
        (src / "script with spaces.py").write_text(ACTION_SCRIPT, encoding="utf-8")
        (src / "-c").write_text("print('option-like filename ran')\n", encoding="utf-8")
        (src / "tree.py").write_text(TREE_SCRIPT, encoding="utf-8")
        (src / "read me.txt").write_bytes(b"0123456789")
        checked([cls.git, "-C", str(src), "add", "."])
        checked([cls.git, "-C", str(src), "-c", "user.name=Integration Test",
                 "-c", "user.email=integration@example.invalid", "commit", "-m", "fixture"])
        cls.commit = checked([cls.git, "-C", str(src), "rev-parse", "HEAD"])
        checked([cls.git, "-C", str(src), "push", str(cls.remote), "HEAD:refs/heads/fixture-main"])
        checked([cls.git, "-C", str(cls.remote), "update-server-info"])
        server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), partial(GitHTTPHandler, directory=str(public)))
        cls.addClassCleanup(server.server_close)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cls.cert, key)
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        cls.addClassCleanup(worker.join, 5)
        cls.addClassCleanup(server.shutdown)
        cls.repo_url = f"https://127.0.0.1:{server.server_port}/fixture.git"

    def environment(self):
        env = super().environment()
        for key in list(env):
            if key.upper().startswith("GIT_"):
                del env[key]
        env.update({key: val for key, val in self.git_env.items() if key.startswith("GIT_")})
        return env

    def register(self, name="Fixture"):
        self.call(201, "POST", "/projects", {"name": name, "repo_url": self.repo_url})
        return "/projects/" + name

    def action(self, project, **kwargs):
        fields = {"commit": self.commit[:12], "interpreter": "python",
                  "script": "script with spaces.py", "timeout": 0}
        return self.call(202, "POST", project + "/actions", dict(fields, **kwargs))

    def test_project_clone_files_validation_delete_and_restart(self):
        for name, url in (("NUL", self.repo_url), ("bad name", self.repo_url),
                          ("Sample", self.repo_url.replace("https:", "http:")),
                          ("Sample", self.repo_url.replace("https://", "https://user:secret@"))):
            self.call(400, "POST", "/projects", {"name": name, "repo_url": url})
        self.call(502, "POST", "/projects", {"name": "Fixture", "repo_url": self.repo_url + "-missing"})
        self.call(404, "GET", "/projects/Fixture")
        project = self.register()
        expected = {"name": "Fixture", "repo_url": self.repo_url, "status": "idle"}
        self.assertEqual(self.call(200, "GET", project.lower()), expected)
        self.assertEqual(self.call(200, "GET", "/projects")["projects"], [expected])
        self.call(409, "POST", "/projects", {"name": "FIXTURE", "repo_url": self.repo_url})
        self.call(404, "GET", project + "/actions")
        self.call(404, "DELETE", project + "/actions")
        listing = self.call(200, "GET", project + "/files")
        self.assertEqual(listing["path"], "")
        entries = {entry["name"]: entry for entry in listing["entries"]}
        self.assertEqual(entries["read me.txt"], {
            "name": "read me.txt", "type": "file", "size_bytes": 10,
        })
        self.assertEqual(entries[".git"]["type"], "directory")
        self.assertIsNone(entries[".git"]["size_bytes"])
        files = project + "/files?"
        head = self.call(200, "GET", files + urlencode({"path": ".git/HEAD"}))
        self.assertEqual(head.strip(), b"ref: refs/heads/fixture-main")
        file_url = files + urlencode({"path": "read me.txt"})
        status, data, headers = self.request("GET", file_url, headers={"Range": "bytes=2-5"})
        self.assertEqual((status, data), (206, b"2345"))
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(headers.get_content_type(), "application/octet-stream")
        self.call(416, "GET", file_url, headers={"Range": "bytes=30-40"})
        for path in ("../outside", "C:/Windows", "..\\outside"):
            self.call(400, "GET", files + urlencode({"path": path}))
        self.call(204, "DELETE", project.lower())
        self.call(404, "GET", project)
        self.call(404, "DELETE", project)
        self.register()
        self.assertTrue(list(self.data.rglob("read me.txt")))
        self.stop_runner()
        self.start_runner()
        self.assertEqual(self.call(200, "GET", "/projects")["projects"], [])
        self.call(404, "GET", project)
        self.assertFalse(list(self.data.rglob("read me.txt")))
        self.register()

    def test_action_conflict_cancel_preserves_logs_and_invalid_request_replaces_round(self):
        project = self.register()
        endpoint = project + "/actions"
        args = ["with spaces", "a&b", '"quoted"']
        self.action(project, args=args)
        expected_stdout = json.dumps(args).encode("ascii")
        running = self.poll(endpoint, lambda item: item["stdout_bytes"] == len(expected_stdout) and
                            item["stderr_bytes"] == len(b"action-stderr"),
                            query="?stdout_offset=0&stderr_offset=0")
        self.assertEqual(running["status"], "running")
        self.assertEqual(running["commit"], self.commit)
        self.assertEqual(running["output_source"], "process")
        self.assertEqual(json.loads(log_bytes(running, "stdout")), args)
        self.assertEqual(log_bytes(running, "stderr"), b"action-stderr")
        self.assertEqual(self.call(200, "GET", project)["status"], "running")
        self.assertEqual(self.call(200, "GET", "/projects")["projects"][0]["status"], "running")
        self.call(401, "POST", endpoint, b"{", auth=False)
        self.call(409, "POST", endpoint, b"{")
        self.call(204, "DELETE", endpoint)
        cancelled = self.finished(endpoint, expected="cancelled")
        self.assertEqual(log_bytes(cancelled, "stdout"), log_bytes(running, "stdout"))
        self.assertEqual(log_bytes(cancelled, "stderr"), log_bytes(running, "stderr"))
        self.assertEqual(self.call(200, "GET", project)["status"], "cancelled")
        file_url = project + "/files?" + urlencode({"path": "action output.txt"})
        self.assertEqual(self.call(200, "GET", file_url), b"new data")
        self.call(204, "DELETE", endpoint)
        self.assertEqual(self.call(200, "GET", endpoint)["status"], "cancelled")
        self.call(400, "POST", endpoint, b"{")
        invalid = self.finished(endpoint, expected="failed")
        self.assertIsNone(invalid["commit"])
        self.assertEqual(invalid["output_source"], "runner")
        self.assertEqual(log_bytes(invalid, "stdout"), b"")
        self.assertEqual(self.call(200, "GET", project)["status"], "failed")
        self.call(404, "GET", file_url)
        self.action(project, commit="0123456789012345678901234567890123456789")
        unresolved = self.finished(endpoint, expected="failed")
        self.assertEqual(unresolved["error"]["code"], "commit_not_found")
        self.assertIsNone(unresolved["commit"])
        gate = self.work / "already released"
        gate.touch()
        succeeded = self.call(200, "POST", endpoint, {
            "commit": self.commit, "interpreter": "python", "script": "script with spaces.py",
            "args": ["--gate", str(gate)],
        })
        self.assertEqual(succeeded["status"], "succeeded")
        self.assertEqual(succeeded["exit_code"], 0)
        self.assertEqual(self.call(200, "GET", project)["status"], "succeeded")
        self.assertEqual(self.call(200, "GET", file_url), b"new data")

    def test_invalid_action_reports_locked_worktree_reset_in_post(self):
        project = self.register()
        endpoint = project + "/actions"
        old_file = next(self.data.rglob("read me.txt"))
        # A normal Windows Python file handle denies delete sharing.
        with old_file.open("rb"):
            response = self.call(400, "POST", endpoint, b"{")
            self.assertEqual(response["error"]["code"], "invalid_request")
            snapshot = response["action"]
            self.assertEqual((snapshot["status"], snapshot["phase"]), ("failed", "reset"))
            self.assertEqual(snapshot["output_source"], "runner")
            self.assertEqual(snapshot["error"]["code"], "reset_blocked")
            self.assertEqual(snapshot, self.call(200, "GET", endpoint))
            self.assertTrue(old_file.is_file())
            self.call(404, "GET", project + "/files?" + urlencode({"path": old_file.name}))

        response = self.call(400, "POST", endpoint, b"{")
        self.assertEqual(response["error"]["code"], "invalid_request")
        self.assertNotIn("action", response)
        self.assertEqual(self.call(200, "GET", endpoint)["phase"], "validate")
        self.assertFalse(old_file.exists())

    def test_new_action_interrupts_an_existing_slow_download(self):
        project = self.register()
        worktree = next(self.data.rglob("read me.txt")).parent
        size = 64 * 1024 * 1024
        with (worktree / "pending.bin").open("wb") as file:
            file.truncate(size)
        with socket.socket() as slow:
            slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            slow.settimeout(6)
            slow.connect(("127.0.0.1", self.port))
            slow.sendall((f"GET {project}/files?path=pending.bin HTTP/1.0\r\n"
                          f"Authorization: Bearer {self.token}\r\n\r\n").encode("ascii"))
            with slow.makefile("rb") as response:
                self.assertIn(b" 200 ", response.readline())
                while True:
                    line = response.readline()
                    if line in (b"", b"\r\n"):
                        break
                # Do not drain the connection. The new Action must free the old tree anyway.
                self.call(400, "POST", project + "/actions", b"{")
                result = self.call(200, "GET", project + "/actions")
                self.assertEqual((result["status"], result["phase"]), ("failed", "validate"), result)
                self.assertFalse((worktree / "pending.bin").exists())
                try:
                    self.assertLess(len(response.read()), size)
                except ConnectionResetError:
                    pass

    def test_simultaneous_case_insensitive_project_registration(self):
        ready = threading.Barrier(2)

        def submit(name):
            ready.wait(timeout=5)
            return self.request("POST", "/projects", {"name": name, "repo_url": self.repo_url})[0]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(submit, ["Same", "sAME"]))
        self.assertEqual(sorted(statuses), [201, 409])
        self.assertEqual(len(self.call(200, "GET", "/projects")["projects"]), 1)

    def test_action_treats_option_like_script_as_filename_and_rejects_escape(self):
        project = self.register()
        endpoint = project + "/actions"
        fields = {"commit": self.commit, "interpreter": "python", "timeout": 10}
        result = self.call(200, "POST", endpoint, dict(fields, script="-c"))
        self.assertEqual(result["status"], "succeeded")
        logs = self.call(200, "GET", endpoint + "?stdout_offset=0")
        self.assertEqual(log_bytes(logs, "stdout").strip(), b"option-like filename ran")
        result = self.call(200, "POST", endpoint, dict(fields, script="..\\..\\outside.py"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "invalid_script")
        self.assertEqual(result["output_source"], "runner")

    def test_action_and_project_deletion_terminate_descendants(self):
        project = self.register()
        endpoint = project + "/actions"
        self.action(project, args=["--tree"])
        with self.live_tree(endpoint) as assert_stopped:
            self.call(204, "DELETE", endpoint)
            assert_stopped()
        cancelled = self.finished(endpoint, expected="cancelled")
        self.assertIn(b"TREE ", log_bytes(cancelled, "stdout"))
        self.action(project, args=["--tree"])
        with self.live_tree(endpoint) as assert_stopped:
            self.call(204, "DELETE", project)
            assert_stopped()
        self.call(404, "GET", project)
        self.call(404, "GET", endpoint)
        self.assertFalse(list(self.data.rglob("read me.txt")))
        self.register()


if __name__ == "__main__":
    unittest.main()
