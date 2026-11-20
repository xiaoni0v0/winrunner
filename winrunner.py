"""Start WinRunner with: python winrunner.py."""

import hmac
import json
import logging
import os
import re
import sys
import textwrap
import time
import traceback
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socket import SHUT_RDWR
from threading import BoundedSemaphore
from typing import cast
from urllib.parse import parse_qsl, unquote, urlsplit

import config
from runner import APIError, Runner, invalid, not_found
from winprocess import health

DECIMAL = re.compile(r"[0-9]+\Z")
BYTE_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)\Z", re.I)
LOG = logging.getLogger("winrunner")
LOG_FILE = Path(__file__).resolve().with_name("winrunner.log")


class ServiceFormatter(logging.Formatter):
    def __init__(self, token: str):
        super().__init__()
        self.secrets = sorted(
            {token, json.dumps(token)[1:-1]} - {""}, key=len, reverse=True
        )

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info is not None:
            kind, error, stack = record.exc_info
            if kind is not None:
                code = getattr(error, "winerror", None) or getattr(error, "errno", None)
                error_name = (
                    f"{kind.__name__}({code})"
                    if isinstance(code, int)
                    else kind.__name__
                )
                frames = " > ".join(
                    f"{Path(frame.filename).name}:{frame.lineno} {frame.name}"
                    for frame in traceback.extract_tb(stack)
                )
                message += f" | {error_name}: {frames}"
        for secret in self.secrets:
            message = message.replace(secret, "[redacted]")
        # Printable ASCII makes the bound independent of the console code page.
        message = "".join(c if c.isprintable() else ascii(c)[1:-1] for c in message)
        message = message.encode("ascii", "backslashreplace").decode("ascii")
        prefix = (
            f"{self.formatTime(record, '%Y-%m-%d %H:%M:%S')} {record.levelname:<7} "
        )
        return textwrap.fill(
            message,
            width=80,
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_on_hyphens=False,
        )


class ServiceFileHandler(logging.FileHandler):
    def __init__(self, path: Path, console: logging.Handler):
        super().__init__(path, mode="a", encoding="utf-8")
        self.console = console
        self.reported_error = False

    def handleError(self, record: logging.LogRecord) -> None:
        if not self.reported_error:
            self.reported_error = True
            warning = logging.LogRecord(
                record.name,
                logging.WARNING,
                __file__,
                0,
                "Cannot append to service log; console logging is still active",
                (),
                None,
            )
            self.console.handle(warning)


@contextmanager
def service_logging(path: Path, token: str):
    channels = (LOG, logging.getLogger("runner"))
    previous = [(channel, channel.level, channel.propagate) for channel in channels]
    formatter = ServiceFormatter(token)
    console = logging.StreamHandler(sys.stdout)
    handlers: list[logging.Handler] = [console]
    console.setFormatter(formatter)
    try:
        disk = ServiceFileHandler(path, console)
    except OSError:
        disk = None
    if disk is not None:
        disk.setFormatter(formatter)
        handlers.append(disk)
    try:
        for channel in channels:
            channel.setLevel(logging.INFO)
            channel.propagate = False
            for handler in handlers:
                channel.addHandler(handler)
        if disk is None:
            LOG.warning("Cannot open service log %s; using stdout only", path)
        yield
    finally:
        for channel, level, propagate in previous:
            for handler in handlers:
                channel.removeHandler(handler)
            channel.setLevel(level)
            channel.propagate = propagate
        for handler in handlers:
            handler.close()


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


class HTTPServer(ThreadingHTTPServer):
    token: bytes
    runner: Runner

    def __init__(self, *args, **kwargs):
        limit = getattr(config, "MAX_HTTP_CONNECTIONS", 64)
        if type(limit) is not int or limit < 1:
            raise ValueError("MAX_HTTP_CONNECTIONS must be a positive integer")
        self.connections = BoundedSemaphore(limit)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            body = b'{"error":{"code":"server_busy","message":"Too many HTTP connections"}}'
            response = (
                b"HTTP/1.0 503 Service Unavailable\r\n"
                b"Content-Type: application/json; charset=utf-8\r\n"
                b"Retry-After: 1\r\nCache-Control: no-store\r\nConnection: close\r\n"
                b"Content-Length: %d\r\n\r\n" % len(body)
            ) + body
            try:
                # Closing with unread bytes can reset the connection on Windows.
                # Discard one chunk, without waiting for a complete HTTP request.
                request.settimeout(0.01)
                try:
                    request.recv(65536)
                except TimeoutError:
                    pass
                request.setblocking(False)
                request.sendall(response)
            except OSError:
                pass  # An overloaded connection must not stall the accept loop.
            finally:
                self.shutdown_request(request)
            LOG.warning("503 connection limit peer=%s", client_address[0])
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request, client_address):
        LOG.exception("Unhandled HTTP handler error peer=%s", client_address[0])


class HTTPHandler(BaseHTTPRequestHandler):
    server: HTTPServer
    server_version = "WinRunner/1.0"
    sys_version = ""

    def setup(self):
        self.request.settimeout(30)
        super().setup()

    def log_request(self, code="-", size="-"):
        self.request_status = code

    def note(self, key: str, value: object) -> None:
        if isinstance(value, str):
            token = self.server.token.decode("ascii")
            if token:
                value = value.replace(token, "[redacted]")
            if len(value) > 100:
                value = value[:97] + "..."
            self.request_details.append(f"{key}={json.dumps(value, ensure_ascii=True)}")
        elif type(value) in (int, float):
            self.request_details.append(f"{key}={value}")

    def note_body(self, raw: bytes, *fields: str) -> None:
        # Metadata only; execution still validates and owns the original body.
        if len(raw) > 16 * 1024:
            return
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):
            return
        if not isinstance(body, dict):
            return
        for key in fields:
            self.note(key, body.get(key))
        if "name" in fields and isinstance(body.get("repo_url"), str):
            try:
                self.note("repo_host", urlsplit(body["repo_url"]).hostname)
            except ValueError:
                pass

    def handle_one_request(self):
        # Keep the base request-line bounds, but route every method after auth.
        self.response_started = False
        self.file_response_aborted = False
        self.request_status = None
        self.operation = "request"
        self.request_details: list[str] = []
        started = time.monotonic()
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = self.request_version = self.command = ""
                self.send_error(HTTPStatus.REQUEST_URI_TOO_LONG)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if self.parse_request():
                self.dispatch(self.command)
                self.wfile.flush()
        except TimeoutError:
            LOG.warning("HTTP request timed out peer=%s", self.client_address[0])
            self.close_connection = True
        except ConnectionError:
            # A client may reset the connection during parsing or while we
            # send an error reply. There is no peer left to respond to.
            self.close_connection = True
        finally:
            if self.request_status is not None:
                elapsed = (time.monotonic() - started) * 1000
                LOG.info(
                    "%s %s %.0fms peer=%s%s",
                    self.request_status,
                    self.operation,
                    elapsed,
                    self.client_address[0],
                    (
                        " " + " ".join(self.request_details)
                        if self.request_details
                        else ""
                    ),
                )

    def parse_request(self):
        if not super().parse_request():
            return False
        values = self.headers.get_all("Authorization", [])
        try:
            scheme, token = values[0].split(" ", 1) if len(values) == 1 else ("", "")
            valid = scheme.lower() == "bearer" and hmac.compare_digest(
                token.encode("ascii"), self.server.token
            )
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            self.operation = "authentication rejected"
            self.reply(
                401,
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "Valid Bearer token required",
                    }
                },
                {"WWW-Authenticate": "Bearer"},
            )
            return False
        return True

    def send_error(self, code, message=None, explain=None):
        reason = self.responses.get(code, ("HTTP error",))[0]
        self.reply(code, {"error": {"code": "http_error", "message": reason}})

    def end_headers(self):
        self.response_started = True
        super().end_headers()

    def reply(self, status, value=None, headers=None):
        data = (
            json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")
            if value is not None
            else b""
        )
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
            raise APIError(
                413, "request_too_large", "Request body exceeds the configured limit"
            )
        try:
            raw = self.rfile.read(length)
        except TimeoutError as exc:
            raise APIError(
                408, "request_timeout", "Timed out while receiving the request body"
            ) from exc
        if len(raw) != length:
            raise invalid("Request body ended before Content-Length")
        return raw

    def target(self):
        try:
            url = urlsplit(self.path)
            if not url.path.startswith("/") or url.scheme or url.netloc or url.fragment:
                raise ValueError
            parts = [
                unquote(part, encoding="utf-8", errors="strict")
                for part in url.path[1:].split("/")
            ]
            if any(not part or any(c in part for c in "/\\\0") for part in parts):
                raise not_found("Route not found")
        except (UnicodeError, ValueError) as exc:
            raise invalid("Invalid URL or duplicate query parameters") from exc
        return parts, url.query

    @staticmethod
    def parse_query(raw):
        try:
            items = parse_qsl(
                raw, keep_blank_values=True, errors="strict", max_num_fields=16
            )
            query = dict(items)
            if len(items) != len(query):
                raise ValueError
        except (UnicodeError, ValueError) as exc:
            raise invalid("Invalid URL or duplicate query parameters") from exc
        return query

    def dispatch(self, method):
        try:
            self.route(method, *self.target())
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            if self.file_response_aborted:
                self.close_connection = True
            elif self.response_started:
                # A download that fails after its headers cannot send a second response.
                LOG.exception("Response interrupted: %s", self.operation)
                self.close_connection = True
            elif isinstance(exc, APIError):
                self.note("error", exc.code)
                self.reply(exc.status, exc.response_payload())
            elif isinstance(exc, ValueError):  # Includes BadPath.
                self.reply(
                    400, {"error": {"code": "invalid_request", "message": str(exc)}}
                )
            elif isinstance(exc, (FileNotFoundError, NotADirectoryError)):
                self.reply(
                    404,
                    {
                        "error": {
                            "code": "not_found",
                            "message": "File or directory not found",
                        }
                    },
                )
            else:
                LOG.exception("Request failed: %s", self.operation)
                self.reply(
                    500,
                    {
                        "error": {
                            "code": "internal_error",
                            "message": "Internal runner error",
                        }
                    },
                )

    def route(self, method, parts, raw_query):
        key = (parts[0], None, *parts[2:]) if len(parts) > 1 else tuple(parts)
        route = self.routes.get(key)
        if route is None:
            self.operation = "unknown route"
            raise not_found("Route not found")
        allowed, handler = route
        route_name = "/" + "/".join(
            part if part is not None else "{id}" for part in key
        )
        if method not in allowed:
            self.operation = f"unsupported method {route_name}"
            self.method_error(", ".join(allowed))
            return
        self.operation = f"{method} {route_name}"
        name = parts[1] if len(parts) > 1 else None
        self.note("project" if parts[0] == "projects" else "id", name)
        query = self.parse_query(raw_query)
        for field in ("stdout_offset", "stderr_offset", "limit"):
            if field in query and DECIMAL.fullmatch(query[field]):
                self.note(field, query[field])
        handler(self, method, name, query)

    def get_health(self, _method, _name, query):
        self.no_query(query)
        runner = self.server.runner
        self.reply(
            200,
            {
                **health(runner.base, time.monotonic() - runner.started_at),
                "run_id": runner.run_id,
            },
        )

    def projects(self, method, _name, query):
        self.no_query(query)
        runner = self.server.runner
        if method == "GET":
            self.reply(200, {"projects": runner.list_projects()})
        else:
            raw = self.raw_body()
            self.note_body(raw, "name")
            self.reply(201, runner.create(raw))

    def project(self, method, name, query):
        self.no_query(query)
        runner = self.server.runner
        if method == "GET":
            self.reply(200, runner.get_project(name))
        else:
            runner.delete_project(name)
            self.reply(204)

    def action(self, method, name, query):
        runner = self.server.runner
        if method == "GET":
            self.reply(200, runner.get_action(name, *log_params(query)))
        elif method == "POST":
            self.no_query(query)
            raw = self.raw_body()
            accepted_at = time.monotonic()
            self.note_body(raw, "commit", "interpreter", "script", "timeout")
            self.reply(*runner.start_action(name, raw, accepted_at))
        else:
            self.no_query(query)
            runner.cancel_action(name)
            self.reply(204)

    def project_files(self, _method, name, query):
        if query.keys() - {"path"}:
            raise invalid("Unexpected query parameter")
        relative = query.get("path", "")
        self.note("path", relative or ".")
        try:
            with self.server.runner.file_entry(
                name, relative, cancel_read=self.abort_file_response
            ) as entry:
                self.files(entry, relative)
        except OSError as exc:
            if not self.response_started and getattr(exc, "winerror", None) in (32, 33):
                raise APIError(409, "file_busy", "File or directory is in use") from exc
            raise

    def abort_file_response(self):
        self.file_response_aborted = True
        try:
            self.connection.shutdown(SHUT_RDWR)
        except OSError:
            pass  # The client may have already disconnected.

    def commands(self, method, _name, query):
        self.no_query(query)
        runner = self.server.runner
        if method == "GET":
            self.reply(200, {"commands": runner.list_commands()})
        else:
            raw = self.raw_body()
            accepted_at = time.monotonic()
            self.note_body(raw, "mode", "timeout")
            status, result = runner.start_command(raw, accepted_at)
            self.note("id", result.get("id"))
            if len(raw) > 16 * 1024:
                self.note("mode", result.get("mode"))
            self.reply(status, result)

    def command_task(self, method, name, query):
        runner = self.server.runner
        if method == "GET":
            self.reply(200, runner.get_command(name, *log_params(query)))
        else:
            self.no_query(query)
            runner.delete_command(name)
            self.reply(204)

    routes = {
        ("health",): (("GET",), get_health),
        ("projects",): (("GET", "POST"), projects),
        ("projects", None): (("GET", "DELETE"), project),
        ("projects", None, "actions"): (("GET", "POST", "DELETE"), action),
        ("projects", None, "files"): (("GET",), project_files),
        ("commands",): (("GET", "POST"), commands),
        ("commands", None): (("GET", "DELETE"), command_task),
    }

    @staticmethod
    def no_query(query):
        if query:
            raise invalid("Unexpected query parameters")

    def method_error(self, allow):
        self.reply(
            405,
            {"error": {"code": "method_not_allowed", "message": "Method not allowed"}},
            {"Allow": allow},
        )

    def files(self, entry, relative):
        if entry.is_dir:
            items = sorted(
                entry.list_entries(), key=lambda item: item["name"].casefold()
            )
            self.reply(
                200,
                {
                    "path": (
                        "" if relative in ("", ".") else relative.replace("\\", "/")
                    ),
                    "entries": items,
                },
            )
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
                self.reply(
                    416, {"error": exc.payload()}, {"Content-Range": f"bytes */{size}"}
                )
                return
            file.seek(start)
            if header is not None:
                self.note("range", f"{start}-{stop - 1}")
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
            # On Windows, shutdown() in a different thread does not reliably
            # wake a socket already waiting for write readiness. Check for
            # cancellation between short writes, retaining a 30 s idle budget.
            self.connection.settimeout(0.25)
            idle_until = time.monotonic() + 30
            while remaining and not self.file_response_aborted:
                data = file.read(min(remaining, 65536))
                if not data:
                    return
                pending = memoryview(data)
                while pending and not self.file_response_aborted:
                    try:
                        sent = self.connection.send(pending)
                    except TimeoutError:
                        if time.monotonic() >= idle_until:
                            raise
                        continue
                    if sent == 0:
                        raise ConnectionResetError("Download connection closed")
                    pending = pending[sent:]
                    idle_until = time.monotonic() + 30
                remaining -= len(data)


def main():
    if sys.platform != "win32":
        sys.exit("WinRunner only supports Windows")
    token = config.TOKEN
    if (
        not isinstance(token, str)
        or not token
        or token == "123456"
        or any(not 33 <= ord(c) <= 126 for c in token)
    ):
        sys.exit(
            "Set TOKEN in config.py to a non-placeholder ASCII token without spaces"
        )
    with service_logging(LOG_FILE, token):
        try:
            serve(token)
        except Exception:
            LOG.exception("WinRunner stopped due to an error")
            sys.exit(1)


def serve(token: str) -> None:
    with HTTPServer((config.HOST, config.PORT), HTTPHandler) as server:
        server.token = token.encode("ascii")
        server.runner = Runner(config.DATA_DIR)
        host, port = cast(tuple[str, int], server.server_address)  # IPv4 listener
        LOG.info("WinRunner listening on http://%s:%s", host, port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            LOG.info("WinRunner stopping")
        finally:
            server.runner.close()


if __name__ == "__main__":
    main()
