"""Volatile project and task state; all transitions are synchronized."""

import base64
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

import config
from gitrepo import CommitError, GitFailure, Repository
from winfiles import BadPath, pinned_entry, pinned_path
from winprocess import Job, JobCancelled

PROJECT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
DEVICE_NAME = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])\Z", re.I)
COMMIT = re.compile(r"[0-9a-fA-F]{1,64}\Z")
LOG = logging.getLogger(__name__)
PS_PROGRESS_MODULE = Path(__file__).resolve().with_name("winrunner-progress.psm1")
PS_SINGLE_QUOTES = str.maketrans(
    {quote: quote * 2 for quote in "'\u2018\u2019\u201a\u201b"}
)
BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def short_command_id(number: int) -> str:
    digits = []
    while number >= len(BASE36):
        number, digit = divmod(number, len(BASE36))
        digits.append(BASE36[digit])
    digits.append(BASE36[number])
    return "".join(reversed(digits))


def encode_ps_command(command):
    # Import defaults before execution without wrapping the command in another
    # script block or inserting a statement before its `using` / `param` block.
    if getattr(config, "PS_SUPPRESS_PROGRESS", True):
        module_path = str(PS_PROGRESS_MODULE).translate(PS_SINGLE_QUOTES)
        command = f"using module '{module_path}'\n{command}"
    return base64.b64encode(command.encode("utf-16-le")).decode("ascii")


class APIError(Exception):
    def __init__(self, status, code, message, *, action=None, command=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.action = action
        self.command = command

    def payload(self):
        return {"code": self.code, "message": str(self)}

    def response_payload(self):
        response = {"error": self.payload()}
        if self.action is not None:
            response["action"] = self.action
        if self.command is not None:
            response["command"] = self.command
        return response


def invalid(message):
    return APIError(400, "invalid_request", message)


def not_found(message):
    return APIError(404, "not_found", message)


def conflict(message):
    return APIError(409, "conflict", message)


def json_value(raw):
    def reject_constant(value):
        raise ValueError(f"Invalid JSON constant: {value}")

    return json.loads(raw.decode("utf-8"), parse_constant=reject_constant)


def object_body(raw):
    try:
        value = json_value(raw)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise invalid("Expected a UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise invalid("Expected a JSON object")
    return value


def clean_string(value):
    return (
        isinstance(value, str)
        and "\0" not in value
        and all(not 0xD800 <= ord(c) <= 0xDFFF for c in value)
    )


def timeout_value(body):
    value = body.get("timeout", config.DEFAULT_TIMEOUT)
    try:
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return float(value)
    except OverflowError:
        pass
    raise invalid("timeout must be a finite non-negative number of seconds")


def project_body(name, raw):
    if (
        not isinstance(name, str)
        or not PROJECT_NAME.fullmatch(name)
        or DEVICE_NAME.fullmatch(name)
    ):
        raise invalid("Invalid Windows project name")
    body = object_body(raw)
    if body.keys() - {"repo_url"}:
        raise invalid("The project request body only accepts repo_url")
    url = body.get("repo_url")
    if (
        not clean_string(url)
        or not url
        or any(ord(c) <= 32 or c in "\\?#\x7f" for c in url)
    ):
        raise invalid("repo_url must be a public HTTPS Git URL without credentials")
    try:
        parts = urlsplit(url)
        port = parts.port
        if (
            parts.scheme != "https"
            or not parts.hostname
            or not parts.path
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
            or port == 0
        ):
            raise ValueError
    except ValueError as exc:
        raise invalid(
            "repo_url must be a public HTTPS Git URL without credentials"
        ) from exc
    return url


def action_body(raw):
    body = object_body(raw)
    commit, interpreter, script = (
        body.get(key) for key in ("commit", "interpreter", "script")
    )
    args = body.get("args", [])
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise invalid("commit must be a hexadecimal hash or prefix")
    if not isinstance(interpreter, str) or interpreter not in config.INTERPRETERS:
        raise invalid("interpreter is not on the configured allowlist")
    if not clean_string(script) or not script:
        raise invalid("script must be a nonempty relative path")
    if not isinstance(args, list) or not all(map(clean_string, args)):
        raise invalid("args must be an array of strings")
    return commit, interpreter, script, args, timeout_value(body)


def command_body(raw):
    body = object_body(raw)
    mode, command = body.get("mode"), body.get("command")
    if mode not in ("cmd", "ps") or not clean_string(command):
        raise invalid("Expected mode (cmd or ps) and a command string")
    return mode, command, timeout_value(body)


def _remove_tree(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        if path.is_symlink():
            path.unlink()
        elif stat.S_ISDIR(info.st_mode):
            path.rmdir()
        else:
            path.unlink()
    elif stat.S_ISDIR(info.st_mode):

        def retry(operation, entry, error):
            if not isinstance(error[1], PermissionError):
                raise error[1]
            entry_info = os.lstat(entry)
            if entry_info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise error[1]
            os.chmod(entry, stat.S_IREAD | stat.S_IWRITE)
            operation(entry)

        shutil.rmtree(path, onerror=retry)
    else:
        path.unlink()


def _clean_paths(paths):
    failures = {}
    for path in paths:
        try:
            _remove_tree(path)
        except OSError as exc:
            failures[path] = exc
    return failures


def _cleanup_error(code, message, exc):
    return APIError(
        409, code, f"{message}; release any file or directory handles and retry. {exc}"
    )


def child_env():
    return {
        key: value for key, value in os.environ.items() if key.upper() != "RUNNER_TOKEN"
    }


class DeferredJob:
    """Allocate native resources only when work runs, never under a registry lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.native = None
        self.stopped = False
        self.stop_requested = False

    def run(self, *args, **kwargs):
        with self.lock:
            if self.stopped:
                raise JobCancelled()
            if self.native is None:
                self.native = Job()
            job = self.native
        return job.run(*args, **kwargs)

    def cancel(self):
        with self.lock:
            self.stopped = True
            job = self.native
        if job is not None:
            job.cancel()

    def request_stop(self):
        with self.lock:
            self.stopped = True
            job = self.native
            if job is None or self.stop_requested:
                return
            self.stop_requested = True

        def terminate():
            try:
                job.cancel()
            except Exception:
                LOG.exception("Could not cancel job during project deletion")
            finally:
                with self.lock:
                    self.stop_requested = False

        try:
            threading.Thread(
                target=terminate, name="winrunner-cancel", daemon=True
            ).start()
        except Exception:
            with self.lock:
                self.stop_requested = False
            raise

    def close(self):
        with self.lock:
            self.stopped = True
            job, self.native = self.native, None
        if job is not None:
            job.close()


class Task:
    def __init__(self, phase, *, command_id=None, mode=None, command=None):
        self.action_id = secrets.token_hex(16) if command_id is None else None
        self.command = command
        self.job = DeferredJob()
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.cancelled = threading.Event()
        self.status, self.phase = "preparing", phase
        self.source, self.exit_code, self.error, self.commit = None, None, None, None
        self.output = {"stdout": bytearray(), "stderr": bytearray()}
        self.command_id, self.mode = command_id, mode
        self.deleting = False

    def stage(self, phase):
        with self.lock:
            self.phase = phase

    def set_commit(self, commit):
        with self.lock:
            self.commit = commit

    def append(self, stream, data):
        with self.lock:
            self.source = "process"
            self.output[stream].extend(data)

    def started(self):
        with self.lock:
            self.phase, self.source = "run", "process"
            if self.status != "cancelling":
                self.status = "running"

    def run(self, argv, *, executable, cwd):
        self.stage("launch")
        return self.job.run(
            argv,
            executable=executable,
            cwd=cwd,
            env=child_env(),
            stdout=lambda data: self.append("stdout", data),
            stderr=lambda data: self.append("stderr", data),
            on_started=self.started,
        )

    def cancel(self, wake=None, *, background=False):
        with self.lock:
            if self.done.is_set():
                return
            self.status = "cancelling"
            self.cancelled.set()
        if wake is not None:
            wake()
        if background:
            self.job.request_stop()
        else:
            self.job.cancel()

    def finish(
        self, *, exit_code=None, error=None, source=None, stdout=b"", stderr=b""
    ):
        try:
            self.job.close()
        except Exception as exc:
            LOG.exception("Could not close finished job")
            if error is None:
                error = APIError(200, "job_cleanup_failed", str(exc))
        with self.lock:
            self.exit_code = exit_code
            if self.cancelled.is_set():
                self.status = "cancelled"
            elif error is not None or exit_code not in (None, 0):
                if error is None:
                    error = APIError(
                        200,
                        "process_exit_nonzero",
                        f"Process exited with code {exit_code}",
                    )
                self.status = "failed"
            else:
                self.status = "succeeded"
            if error is not None:
                self.error = error.payload()
            if source is not None:
                self.source = source
            self.output["stdout"].extend(stdout)
            self.output["stderr"].extend(stderr)
            self.done.set()

    def snapshot(self, offsets=None, limit=65536):
        offsets = offsets or {}
        with self.lock:
            result = {
                "status": self.status,
                "phase": self.phase,
                "output_source": self.source,
                "exit_code": self.exit_code,
                "error": self.error,
                "stdout_bytes": len(self.output["stdout"]),
                "stderr_bytes": len(self.output["stderr"]),
            }
            if self.command_id is None:
                result["commit"] = self.commit
            else:
                result.update(id=self.command_id, mode=self.mode)
            for stream, offset in offsets.items():
                output = self.output[stream]
                if offset > len(output):
                    raise APIError(
                        400,
                        "invalid_offset",
                        f"{stream}_offset exceeds the stored length",
                    )
                chunk = output[offset : offset + limit]
                result[stream] = {
                    "data_base64": base64.b64encode(chunk).decode("ascii"),
                    "next_offset": offset + len(chunk),
                }
            return result

    def response(self, seconds, accepted_at):
        if seconds > 0:
            end = accepted_at + seconds
            while not self.done.is_set():
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                self.done.wait(min(remaining, threading.TIMEOUT_MAX))
        result = self.snapshot()
        if self.action_id is not None:
            result["action_id"] = self.action_id
        status = (
            200 if result["status"] in ("succeeded", "failed", "cancelled") else 202
        )
        return status, result


class Project:
    def __init__(self, name, url, job, projects_dir, caches_dir):
        self.name, self.url, self.clone_job = name, url, job
        self.worktree, self.cache = projects_dir / name, caches_dir / name
        self.lifecycle: str | None = "cloning"
        self.cloned = threading.Event()
        self.create_failed = False
        self.task = None
        self.commands: dict[str, Task] = {}
        self.command_scope = f"project_command:{secrets.token_hex(16)}"
        self.visible = False
        self.readers = {}
        self.delete_in_progress = False
        self.delete_done = threading.Event()
        self.delete_done.set()

    def tasks(self) -> list[Task]:
        tasks = list(self.commands.values())
        if self.task is not None:
            tasks.append(self.task)
        return tasks

    def snapshot(self):
        if self.lifecycle is not None:
            status = self.lifecycle
        elif self.task is None:
            status = "idle"
        else:
            with self.task.lock:
                status = self.task.status
        return {"name": self.name, "repo_url": self.url, "status": status}


class Runner:
    def __init__(self, data_dir):
        import msvcrt

        self.lock = threading.RLock()
        self.reader_changed = threading.Condition(self.lock)
        self.closing = False
        self.projects: dict[str, Project] = {}
        self.commands: dict[str, Task] = {}
        self.pending_cleanup: dict[str, tuple[Path, ...]] = {}
        # Keep issuing new IDs for the same name even after project deletion.
        # None is the independent global command namespace.
        self.next_command_numbers: dict[str | None, int] = {}
        self.started_at = time.monotonic()
        self.run_id = secrets.token_hex(16)
        self.base = Path(data_dir).resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.lockfile_path = self.base / ".lock"
        self.lease = self.lockfile_path.open("a+b")
        try:
            self.lease.seek(0)
            msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.lease.close()
            raise RuntimeError(
                f"The runner data directory is already in use: {self.base}"
            ) from exc
        self.projects_dir, self.caches_dir = self.base / "projects", self.base / "cache"
        try:
            for path in (self.projects_dir, self.caches_dir):
                _remove_tree(path)
                path.mkdir()
            config_path = self.base / "empty-git-config"
            _remove_tree(config_path)
            config_path.touch(exist_ok=False)
        except BaseException:
            self.lease.close()
            raise

    def _available(self):
        if self.closing:
            raise APIError(503, "shutting_down", "Runner is shutting down")

    def _project(self, name):
        project = self.projects.get(name.lower())
        if project is None or (
            project.create_failed and project.lifecycle == "cloning"
        ):
            raise not_found("Project not found")
        return project

    @staticmethod
    def _command(commands, command_id):
        task = commands.get(command_id)
        if task is None:
            raise not_found("Command not found")
        return task

    def _commands(self, name):
        return self.commands if name is None else self._project(name).commands

    def command_project(self, name) -> Project:
        with self.lock:
            return self._project(name)

    def _repo(self, project, job):
        return Repository(project.cache, project.worktree, project.url, job, self.base)

    def list_projects(self):
        with self.lock:
            return [
                project.snapshot()
                for project in self.projects.values()
                if not project.create_failed or project.lifecycle != "cloning"
            ]

    def get_project(self, name):
        with self.lock:
            return self._project(name).snapshot()

    def create(self, name, raw):
        url = project_body(name, raw)
        key = name.lower()
        with self.lock:
            self._available()
            if key in self.projects:
                raise conflict("Project name is already registered")
            project = Project(
                name, url, DeferredJob(), self.projects_dir, self.caches_dir
            )
            stale_paths = self.pending_cleanup.get(key, ())
            self.projects[key] = project
        paths = tuple(dict.fromkeys((*stale_paths, project.worktree, project.cache)))
        success = False
        failed_paths = {}
        try:
            failed_paths = _clean_paths(paths)
            if failed_paths:
                exc = next(iter(failed_paths.values()))
                raise _cleanup_error(
                    "project_cleanup_blocked",
                    "Could not clear stale project directories",
                    exc,
                )
            self._repo(project, project.clone_job).create()
            with self.lock:
                if project.lifecycle != "cloning" or self.closing:
                    raise conflict("Project creation was cancelled")
                project.lifecycle = None
                project.visible = True
                success = True
                return project.snapshot()
        except GitFailure as exc:
            message = exc.stderr.decode("utf-8", errors="replace").strip() or str(exc)
            raise APIError(502, "git_clone_failed", message) from exc
        except JobCancelled as exc:
            raise conflict("Project creation was cancelled") from exc
        finally:
            if not success:
                with self.lock:
                    project.create_failed = True
                    tasks = project.tasks()
                # Stop commands accepted during cloning before rolling back the
                # worktree or releasing its name, even if a cancellation fails.
                for task in tasks:
                    try:
                        self._cancel_task(task)
                    except Exception:
                        LOG.exception(
                            "Could not cancel a task during rollback for %s", name
                        )
                for task in tasks:
                    task.done.wait()
            try:
                project.clone_job.close()
            except Exception:
                LOG.exception("Could not close clone job for %s", name)
            try:
                if not success and not failed_paths:
                    failed_paths = _clean_paths(paths)
                for path, cleanup_failure in failed_paths.items():
                    LOG.warning("Project rollback left %s: %s", path, cleanup_failure)
            finally:
                with self.lock:
                    if failed_paths:
                        self.pending_cleanup[key] = tuple(failed_paths)
                    else:
                        self.pending_cleanup.pop(key, None)
                    if not success and project.lifecycle == "cloning":
                        self.projects.pop(key, None)
                    project.cloned.set()

    def delete_project(self, name):
        with self.lock:
            self._available()
            project = self._project(name)
            if project.delete_in_progress:
                raise conflict("Project deletion is already in progress")
            project.delete_in_progress = True
            project.delete_done.clear()
            project.lifecycle = "deleting"
            project.visible = False
            tasks = project.tasks()
        deadline = time.monotonic() + config.PROJECT_DRAIN_TIMEOUT
        success = False
        key = project.name.lower()
        try:
            self._interrupt_readers(project)
            for task in tasks:
                self._request_task_stop(task)
            if not project.cloned.is_set():
                project.clone_job.request_stop()
            for event in (project.cloned, *(task.done for task in tasks)):
                if not event.wait(max(0, deadline - time.monotonic())):
                    raise APIError(
                        409, "project_busy", "Project is still stopping; retry DELETE"
                    )
            self._wait_readers(project, deadline)
            with self.lock:
                stale_paths = self.pending_cleanup.get(key, ())
            paths = tuple(
                dict.fromkeys((*stale_paths, project.worktree, project.cache))
            )
            failures = _clean_paths(paths)
            if failures:
                if project.create_failed:
                    with self.lock:
                        self.pending_cleanup[key] = tuple(failures)
                exc = next(iter(failures.values()))
                raise _cleanup_error(
                    "project_cleanup_blocked",
                    "Could not delete project directories",
                    exc,
                )
            success = True
        finally:
            with self.lock:
                if success:
                    self.pending_cleanup.pop(key, None)
                if success:
                    self.projects.pop(key, None)
                else:
                    project.delete_in_progress = False
                    project.lifecycle = "delete_failed"
                project.delete_done.set()

    @contextmanager
    def file_entry(self, name, relative, *, cancel_read=None):
        key = object()
        with self.lock:
            self._available()
            project = self._project(name)
            if not project.visible or project.lifecycle is not None:
                raise not_found("The current project directory is not available")
            project.readers[key] = cancel_read
        try:
            with pinned_entry(project.worktree, relative) as entry:
                yield entry
        finally:
            with self.reader_changed:
                del project.readers[key]
                self.reader_changed.notify_all()

    def _interrupt_readers(self, project):
        with self.lock:
            callbacks = tuple(project.readers.values())
        for cancel in callbacks:
            if cancel is not None:
                try:
                    cancel()
                except Exception:
                    LOG.exception(
                        "Could not interrupt a file response for %s", project.name
                    )

    def _notify_readers(self):
        with self.reader_changed:
            self.reader_changed.notify_all()

    def _cancel_task(self, task):
        task.cancel(self._notify_readers)

    def _request_task_stop(self, task):
        task.cancel(self._notify_readers, background=True)

    def _wait_readers(self, project, deadline, cancelled=None):
        with self.reader_changed:
            self.reader_changed.wait_for(
                lambda: not project.readers
                or (cancelled is not None and cancelled.is_set()),
                timeout=max(0, deadline - time.monotonic()),
            )
            if not project.readers:
                return
            if cancelled is not None and cancelled.is_set():
                raise APIError(
                    409,
                    "reset_cancelled",
                    "Cancelled before existing downloads released the old worktree",
                )
            raise APIError(
                409,
                "project_busy",
                "Project downloads have not released their files; retry the operation",
            )

    def _reset_action(self, project, task):
        deadline = time.monotonic() + config.PROJECT_DRAIN_TIMEOUT
        self._interrupt_readers(project)
        self._wait_readers(project, deadline, task.cancelled)
        try:
            _remove_tree(project.worktree)
        except OSError as exc:
            raise _cleanup_error(
                "reset_blocked", "Could not clear the previous worktree", exc
            ) from exc

    def start_action(self, name, raw, accepted_at):
        with self.lock:
            self._available()
            project = self._project(name)
            if project.lifecycle is not None or (
                project.task and not project.task.done.is_set()
            ):
                raise conflict("Project or action slot is busy")
            task = Task("reset")
            project.task, project.visible = task, False
        try:
            values, error = action_body(raw), None
        except APIError as exc:
            values, error = None, exc
        try:
            self._start_thread(self._run_action, task, project, values, error)
        except Exception as exc:
            worker_error = APIError(
                500, "worker_unavailable", "Could not start worker thread"
            )
            diagnostic = str(exc)
            try:
                self._reset_action(project, task)
            except Exception as reset_exc:
                worker_error = APIError(
                    500,
                    "worker_unavailable_reset_incomplete",
                    f"Could not start worker thread; worktree reset incomplete: {reset_exc}",
                )
                diagnostic = f"{diagnostic}\n{worker_error}"
            finally:
                task.finish(
                    error=worker_error,
                    source="runner",
                    stderr=diagnostic.encode("utf-8", "replace"),
                )
            raise worker_error from exc
        if error is not None:
            task.done.wait()
            action = task.snapshot()
            if action["phase"] == "reset" and action["error"] is not None:
                # Report both failures without making the client race the next Action via GET.
                raise APIError(
                    error.status, error.code, str(error), action=action
                ) from error
            raise error
        return task.response(values[-1], accepted_at)

    @staticmethod
    def _start_thread(worker, task, *args):
        threading.Thread(target=worker, args=(task, *args), daemon=True).start()

    def _run_action(self, task, project, values, validation_error):
        result = {}
        checkout_started = False
        try:
            self._reset_action(project, task)
            if task.cancelled.is_set():
                raise JobCancelled()
            task.stage("validate")
            if validation_error:
                raise validation_error
            prefix, interpreter, script, args, _ = values
            repo = self._repo(project, task.job)
            task.stage("fetch")
            repo.fetch()
            task.stage("resolve")
            commit = repo.resolve(prefix)
            task.set_commit(commit)
            task.stage("checkout")
            checkout_started = True
            repo.materialize(commit)
            with self.lock:
                if project.lifecycle is None:
                    project.visible = True
            try:
                with pinned_path(project.worktree, script) as path:
                    if not path.is_file():
                        raise BadPath("script is not a regular file")
                    script_path = str(path)
            except (ValueError, OSError) as exc:
                raise APIError(
                    400, "invalid_script", f"Cannot use script path: {exc}"
                ) from exc
            exe, options = config.INTERPRETERS[interpreter]
            task.stage("launch")
            if not exe:
                raise FileNotFoundError(
                    f"Configured interpreter {interpreter!r} is not installed"
                )
            result["exit_code"] = task.run(
                [exe, *options, script_path, *args],
                executable=exe,
                cwd=project.worktree,
            )
        except JobCancelled as exc:
            if task.phase == "run":
                result["exit_code"] = exc.exit_code
        except GitFailure as exc:
            result.update(
                error=APIError(200, "git_failed", str(exc)),
                source="git",
                stdout=exc.stdout,
                stderr=exc.stderr,
            )
        except CommitError as exc:
            result.update(_failure(APIError(200, exc.code, str(exc)), "runner"))
        except APIError as exc:
            result.update(_failure(exc, "runner"))
        except Exception as exc:
            source = {"launch": "launch", "run": "process"}.get(task.phase, "runner")
            code = {
                "launch": "launch_failed",
                "process": "process_error",
                "runner": "runner_error",
            }[source]
            result.update(_failure(APIError(200, code, str(exc)), source))
        finally:
            try:
                if checkout_started:
                    self._publish_worktree(project)
            except Exception as exc:
                LOG.exception("Could not inspect current worktree for %s", project.name)
                with self.lock:
                    project.visible = False
                if "error" not in result:
                    error = APIError(
                        200,
                        "worktree_status_failed",
                        f"Could not inspect current worktree: {exc}",
                    )
                    if task.phase == "run":
                        result["error"] = (
                            error  # Do not mix Runner diagnostics into process logs.
                        )
                    else:
                        result.update(_failure(error, "runner"))
            finally:
                task.finish(**result)

    def _publish_worktree(self, project):
        visible = project.worktree.is_dir()
        with self.lock:
            if project.lifecycle is None:
                project.visible = visible

    def get_action(self, name, offsets=None, limit=65536, *, include_input=False):
        with self.lock:
            project = self._project(name)
            if project.lifecycle in ("deleting", "delete_failed"):
                raise conflict("Project deletion is in progress or must be retried")
            task = project.task
            if task is None:
                raise not_found("No action has been submitted")
            result = task.snapshot(offsets, limit)
            if include_input:
                result["action_id"] = task.action_id
            return result

    def cancel_action(self, name):
        with self.lock:
            self._available()
            project = self._project(name)
            if project.lifecycle in ("deleting", "delete_failed"):
                raise conflict("Project deletion is in progress or must be retried")
            if project.task is None:
                raise not_found("No action has been submitted")
            task = project.task
        self._cancel_task(task)
        task.done.wait()

    def start_command(self, raw, accepted_at, *, project: Project | None = None):
        with self.lock:
            self._available()
            if project is None:
                key, commands, cwd = None, self.commands, None
            else:
                key = project.name.lower()
                # A submit bound to a project instance must not run in its replacement.
                if self._project(key) is not project:
                    raise not_found("Project not found")
                if project.delete_in_progress:
                    raise conflict("Project deletion is already in progress")
                if project.lifecycle == "delete_failed" and not project.cloned.is_set():
                    raise conflict("Project creation is still rolling back")
                commands, cwd = project.commands, project.worktree
            number = self.next_command_numbers.get(key, 0)
            command_id = short_command_id(number)
            self.next_command_numbers[key] = number + 1
            task = Task("validate", command_id=command_id)
            commands[command_id] = task
        try:
            mode, command, timeout = command_body(raw)
        except APIError as exc:
            task.finish(**_failure(exc, "runner"))
            raise APIError(
                exc.status, exc.code, str(exc), command=task.snapshot()
            ) from exc
        with task.lock:
            task.mode, task.command, task.phase = mode, command, "launch"
        try:
            self._start_thread(self._run_command, task, command, cwd)
        except Exception as exc:
            error = APIError(500, "worker_unavailable", "Could not start worker thread")
            task.finish(
                error=error, source="runner", stderr=str(exc).encode("utf-8", "replace")
            )
            raise APIError(
                error.status, error.code, str(error), command=task.snapshot()
            ) from exc
        return task.response(timeout, accepted_at)

    @staticmethod
    def _run_command(task, command, cwd):
        result = {}
        try:
            cwd = Path.home() if cwd is None else cwd
            drive = cwd.drive.lower()
            if task.mode == "cmd" and (
                drive.startswith("\\\\?\\unc\\")
                or (drive.startswith("\\\\") and not drive.startswith("\\\\?\\"))
            ):
                raise OSError("CMD cannot use a UNC working directory")
            if task.mode == "ps":
                executable = str(config.POWERSHELL)
                encoded = encode_ps_command(command)
                argv = [
                    executable,
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    encoded,
                ]
                # CreateProcessW allows at most 32,767 UTF-16 code units,
                # including the terminating NUL and the encoded command.
                if len(subprocess.list2cmdline(argv).encode("utf-16-le")) >= 65534:
                    raise OSError("The command line is too long.")
            else:
                executable = str(config.CMD)
                argv = (
                    subprocess.list2cmdline([executable])
                    + ' /d /s /c "'
                    + command
                    + '"'
                )
            result["exit_code"] = task.run(argv, executable=executable, cwd=cwd)
        except JobCancelled as exc:
            result["exit_code"] = exc.exit_code
        except Exception as exc:
            source = "process" if task.phase == "run" else "launch"
            result = _failure(
                APIError(
                    200,
                    "process_error" if source == "process" else "launch_failed",
                    str(exc),
                ),
                source,
            )
        finally:
            task.finish(**result)

    def list_commands(self, *, name=None):
        with self.lock:
            return [task.snapshot() for task in self._commands(name).values()]

    def get_command(
        self, command_id, offsets=None, limit=65536, *, name=None, include_input=False
    ):
        with self.lock:
            task = self._command(self._commands(name), command_id)
            result = task.snapshot(offsets, limit)
            if include_input:
                result["command"] = task.command
            return result

    def delete_command(self, command_id, *, name=None):
        with self.lock:
            self._available()
            commands = self._commands(name)
            task = self._command(commands, command_id)
            if task.deleting:
                raise conflict("Command deletion is already in progress")
            task.deleting = True
        try:
            self._cancel_task(task)
            task.done.wait()
        except BaseException:
            with self.lock:
                task.deleting = False
            raise
        with self.lock:
            commands.pop(command_id, None)

    def close(self):
        with self.lock:
            self.closing = True
            projects = list(self.projects.values())
            tasks = list(self.commands.values())
            for project in projects:
                tasks.extend(project.tasks())
        try:
            for project in projects:
                if not project.cloned.is_set():
                    try:
                        project.clone_job.cancel()
                    except Exception:
                        LOG.exception("Could not cancel clone during shutdown")
            for project in projects:
                self._interrupt_readers(project)
            for task in tasks:
                try:
                    self._cancel_task(task)
                except Exception:
                    LOG.exception("Could not cancel task during shutdown")
            for project in projects:
                project.cloned.wait()
                project.delete_done.wait()
            for task in tasks:
                task.done.wait()
            with self.reader_changed:
                self.reader_changed.wait_for(
                    lambda: not any(p.readers for p in projects)
                )
        finally:
            self.lease.close()


def _failure(error, source):
    if source == "process":
        # A monitor failure is metadata, not bytes emitted by the child.
        return {"error": error, "source": source}
    return {
        "error": error,
        "source": source,
        "stderr": str(error).encode("utf-8", "replace"),
    }
