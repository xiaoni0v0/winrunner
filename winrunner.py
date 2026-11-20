"""Start WinRunner with: python -m winrunner."""

import hmac
import json
import os
import re
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, unquote, urlsplit

import config
from runner import APIError, Runner, invalid, not_found
from winfiles import BadPath
from winprocess import health


DECIMAL = re.compile(r"[0-9]+\Z")
BYTE_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)\Z", re.I)


def uint(text, name):
    try:
        if DECIMAL.fullmatch(text):
            return int(text)
    except ValueError:
        pass
    raise invalid(f"{name} must be a non-negative integer")


def log_params(query):
    offsets = {}
    for stream in ("stdout", "stderr"):
        key = stream + "_offset"
        if key in query:
            offsets[stream] = uint(query[key], key)
    limit = uint(query.get("limit", "65536"), "limit")
    if not 1 <= limit <= 1048576:
        raise invalid("limit must be between 1 and 1048576")
    if query.keys() - {"stdout_offset", "stderr_offset", "limit"}:
        raise invalid("Unexpected query parameter")
    return offsets, limit


def range_bounds(header, size):
    if header is None:
        return 0, size
    match = BYTE_RANGE.fullmatch(header.strip())
    if match and size:
        a, b = match.groups()
        try:
            if a:
                start = int(a)
                end = min(int(b) + 1, size) if b else size
                if 0 <= start < end:
                    return start, end
            elif b and int(b):
                return max(0, size - int(b)), size
        except ValueError:
            pass
    raise APIError(416, "range_not_satisfiable", "Expected one satisfiable byte range")


class HTTPHandler(BaseHTTPRequestHandler):
    server_version = "WinRunner/1.0"
    sys_version = ""

    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_request(self, code="-", size="-"):
        self.log_message("%s %s", self.command, code)

    def parse_request(self):
        if not super().parse_request():
            return False
        values = self.headers.get_all("Authorization", [])
        try:
            scheme, token = values[0].split(" ", 1) if len(values) == 1 else ("", "")
            valid = scheme.lower() == "bearer" and hmac.compare_digest(token.encode("ascii"), self.server.token)
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            self.reply(401, {"error": {"code": "unauthorized", "message": "Valid Bearer token required"}},
                       {"WWW-Authenticate": "Bearer"})
            return False
        return True

    def send_error(self, code, message=None, explain=None):
        reason = self.responses.get(code, ("HTTP error",))[0]
        self.reply(code, {"error": {"code": "http_error", "message": reason}})

    def reply(self, status, value=None, headers=None):
        data = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii") if value is not None else b""
        self.send_response(status)
        if value is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        if status != 204:
            self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, val in (headers or {}).items():
            self.send_header(key, val)
        self.end_headers()
        self.close_connection = True
        if data and self.command != "HEAD":
            self.wfile.write(data)

    def raw_body(self):
        if self.headers.get_all("Transfer-Encoding"):
            raise invalid("Use Content-Length, not Transfer-Encoding")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) > 1:
            raise invalid("Multiple Content-Length headers")
        length = uint(lengths[0].strip(), "Content-Length") if lengths else 0
        if length > config.MAX_REQUEST_BYTES:
            raise APIError(413, "request_too_large", "Request body exceeds the configured limit")
        try:
            raw = self.rfile.read(length)
        except TimeoutError as exc:
            raise APIError(408, "request_timeout", "Timed out while receiving the request body") from exc
        if len(raw) != length:
            raise invalid("Request body ended before Content-Length")
        return raw

    def target(self):
        try:
            url = urlsplit(self.path)
            if not url.path.startswith("/") or url.scheme or url.netloc or url.fragment:
                raise ValueError
            parts = [unquote(part, encoding="utf-8", errors="strict") for part in url.path[1:].split("/")]
            if any(not part or any(c in part for c in "/\\\0") for part in parts):
                raise not_found("Route not found")
            queries = parse_qsl(url.query, keep_blank_values=True, errors="strict", max_num_fields=16)
            if len(queries) != len(dict(queries)):
                raise ValueError
        except (UnicodeError, ValueError) as exc:
            raise invalid("Invalid URL or duplicate query parameters") from exc
        return parts, dict(queries)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def dispatch(self, method):
        try:
            self.route(method, *self.target())
        except APIError as exc:
            self.reply(exc.status, {"error": exc.payload()})
        except (BadPath, ValueError) as exc:
            self.reply(400, {"error": {"code": "invalid_request", "message": str(exc)}})
        except FileNotFoundError:
            self.reply(404, {"error": {"code": "not_found", "message": "File or directory not found"}})
        except NotADirectoryError:
            self.reply(404, {"error": {"code": "not_found", "message": "File or directory not found"}})
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception:
            traceback.print_exc()
            self.reply(500, {"error": {"code": "internal_error", "message": "Internal runner error"}})

    def route(self, method, parts, query):
        runner = self.server.runner
        if parts == ["health"] and method == "GET":
            self.no_query(query)
            self.reply(200, health(runner.base, time.monotonic() - runner.started_at))
        elif parts == ["projects"]:
            self.no_query(query)
            if method == "GET":
                self.reply(200, {"projects": runner.list_projects()})
            elif method == "POST":
                self.reply(201, runner.create(self.raw_body()))
            else:
                self.method_error("GET, POST")
        elif len(parts) == 2 and parts[0] == "projects":
            self.no_query(query)
            name = parts[1]
            if method == "GET":
                self.reply(200, runner.get_project(name))
            elif method == "DELETE":
                runner.delete_project(name)
                self.reply(204)
            else:
                self.method_error("GET, DELETE")
        elif len(parts) == 3 and parts[0] == "projects" and parts[2] == "actions":
            name = parts[1]
            if method == "GET":
                self.reply(200, runner.get_action(name, *log_params(query)))
            elif method == "POST":
                self.no_query(query)
                raw = self.raw_body()
                self.reply(*runner.start_action(name, raw, time.monotonic()))
            else:
                self.no_query(query)
                runner.cancel_action(name)
                self.reply(204)
        elif len(parts) == 3 and parts[0] == "projects" and parts[2] == "files" and method == "GET":
            if query.keys() - {"path"}:
                raise invalid("Unexpected query parameter")
            relative = query.get("path", "")
            with runner.file_entry(parts[1], relative) as entry:
                self.files(entry, relative)
        elif parts == ["commands"]:
            self.no_query(query)
            if method == "GET":
                self.reply(200, {"commands": runner.list_commands()})
            elif method == "POST":
                raw = self.raw_body()
                self.reply(*runner.start_command(raw, time.monotonic()))
            else:
                self.method_error("GET, POST")
        elif len(parts) == 2 and parts[0] == "commands":
            if method == "GET":
                self.reply(200, runner.get_command(parts[1], *log_params(query)))
            elif method == "DELETE":
                self.no_query(query)
                runner.delete_command(parts[1])
                self.reply(204)
            else:
                self.method_error("GET, DELETE")
        else:
            raise not_found("Route not found")

    @staticmethod
    def no_query(query):
        if query:
            raise invalid("Unexpected query parameters")

    def method_error(self, allow):
        self.reply(405, {"error": {"code": "method_not_allowed", "message": "Method not allowed"}},
                   {"Allow": allow})

    def files(self, entry, relative):
        if entry.is_dir:
            items = sorted(entry.list_entries(), key=lambda item: item["name"].casefold())
            self.reply(200, {"path": "" if relative in ("", ".") else relative.replace("\\", "/"),
                             "entries": items})
        else:
            self.download(entry)

    def download(self, entry):
        with entry.open_file() as file:
            size = os.fstat(file.fileno()).st_size
            ranges = self.headers.get_all("Range", [])
            header = ",".join(ranges) if ranges else None
            try:
                start, stop = range_bounds(header, size)
            except APIError as exc:
                self.reply(416, {"error": exc.payload()}, {"Content-Range": f"bytes */{size}"})
                return
            file.seek(start)
            self.send_response(206 if header is not None else 200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(stop - start))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            if header is not None:
                self.send_header("Content-Range", f"bytes {start}-{stop - 1}/{size}")
            self.end_headers()
            self.close_connection = True
            remaining = stop - start
            try:
                while remaining:
                    data = file.read(min(remaining, 65536))
                    if not data:
                        return
                    self.wfile.write(data)
                    remaining -= len(data)
            except OSError:
                return  # Headers are sent; the client sees an interrupted download.


def main():
    if sys.platform != "win32":
        sys.exit("WinRunner only supports Windows")
    token = os.environ.get("RUNNER_TOKEN", "")
    if not token or not token.isascii() or any(not 33 <= ord(c) <= 126 for c in token):
        sys.exit("Set RUNNER_TOKEN to a nonempty ASCII token without spaces")
    with ThreadingHTTPServer((config.HOST, config.PORT), HTTPHandler) as server:
        server.token = token.encode("ascii")
        server.runner = Runner(config.DATA_DIR)
        host, port = server.server_address
        print(f"WinRunner listening on http://{host}:{port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.runner.close()


if __name__ == "__main__":
    main()
