"""Volatile project and task state; all transitions are synchronized."""

import base64
import json
import math
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
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


class APIError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code

    def payload(self):
        return {"code": self.code, "message": str(self)}


def invalid(message):
    return APIError(400, "invalid_request", message)


def not_found(message):
    return APIError(404, "not_found", message)


def conflict(message):
    return APIError(409, "conflict", message)


def object_body(raw):
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise invalid("Expected a UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise invalid("Expected a JSON object")
    return value


def clean_string(value):
    return isinstance(value, str) and "\0" not in value and all(
        not 0xD800 <= ord(c) <= 0xDFFF for c in value)


def timeout_value(body):
    value = body.get("timeout", config.DEFAULT_TIMEOUT)
    try:
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            return float(value)
    except OverflowError:
        pass
    raise invalid("timeout must be a finite non-negative number of seconds")


def project_body(raw):
    body = object_body(raw)
    name, url = body.get("name"), body.get("repo_url")
    if not isinstance(name, str) or not PROJECT_NAME.fullmatch(name) or DEVICE_NAME.fullmatch(name):
        raise invalid("Invalid Windows project name")
    if not clean_string(url) or not url or any(ord(c) <= 32 or c in "\\\x7f" for c in url):
        raise invalid("repo_url must be a public HTTPS Git URL without credentials")
    try:
        parts = urlsplit(url)
        port = parts.port
        if (parts.scheme != "https" or not parts.hostname or not parts.path
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment or port == 0):
            raise ValueError
    except ValueError as exc:
        raise invalid("repo_url must be a public HTTPS Git URL without credentials") from exc
    return name, url


def action_body(raw):
    body = object_body(raw)
    commit, interpreter, script = (body.get(key) for key in ("commit", "interpreter", "script"))
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


def child_env():
    return {key: value for key, value in os.environ.items() if key.upper() != "RUNNER_TOKEN"}


class Task:
    def __init__(self, phase, *, command_id=None, mode=None):
        self.job = Job()
        self.lock = threading.RLock()
        self.done = threading.Event()
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
        return self.job.run(argv, executable=executable, cwd=cwd, env=child_env(),
                            stdout=lambda data: self.append("stdout", data),
                            stderr=lambda data: self.append("stderr", data),
                            on_started=self.started)

    def cancel(self):
        with self.lock:
            if self.done.is_set():
                return
            self.status = "cancelling"
        self.job.cancel()

    def finish(self, *, exit_code=None, error=None, source=None, stdout=b"", stderr=b""):
        self.job.close()
        with self.lock:
            self.exit_code = exit_code
            if self.status == "cancelling":
                self.status = "cancelled"
            elif error is not None or exit_code not in (None, 0):
                if error is None:
                    error = APIError(200, "process_exit_nonzero", f"Process exited with code {exit_code}")
                self.error = error.payload()
                self.status = "failed"
            else:
                self.status = "succeeded"
            if source is not None:
                self.source = source
            self.output["stdout"].extend(stdout)
            self.output["stderr"].extend(stderr)
            self.done.set()

    def snapshot(self, offsets=None, limit=65536):
        offsets = offsets or {}
        with self.lock:
            result = {"status": self.status, "phase": self.phase, "output_source": self.source,
                      "exit_code": self.exit_code, "error": self.error,
                      "stdout_bytes": len(self.output["stdout"]), "stderr_bytes": len(self.output["stderr"])}
            if self.command_id is None:
                result["commit"] = self.commit
            else:
                result.update(id=self.command_id, mode=self.mode)
            for stream, offset in offsets.items():
                output = self.output[stream]
                if offset > len(output):
                    raise APIError(400, "invalid_offset", f"{stream}_offset exceeds the stored length")
                chunk = output[offset:offset + limit]
                result[stream] = {"data_base64": base64.b64encode(chunk).decode("ascii"),
                                  "next_offset": offset + len(chunk)}
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
        status = 200 if result["status"] in ("succeeded", "failed", "cancelled") else 202
        return status, result


class Project:
    def __init__(self, name, url, job, projects_dir, caches_dir):
        self.name, self.url, self.clone_job = name, url, job
        self.worktree, self.cache = projects_dir / name, caches_dir / name
        self.lifecycle = "cloning"
        self.cloned = threading.Event()
        self.task = None
        self.visible = False
        self.readers = 0
        self.delete_in_progress = False

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
        self.projects, self.commands = {}, {}
        self.started_at = time.monotonic()
        self.base = Path(data_dir).resolve()
        self.base.mkdir(parents=True, exist_ok=True)
        self.lockfile_path = self.base / ".lock"
        self.lease = self.lockfile_path.open("a+b")
        try:
            self.lease.seek(0)
            msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.lease.close()
            raise RuntimeError(f"The runner data directory is already in use: {self.base}") from exc
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
        if project is None:
            raise not_found("Project not found")
        return project

    def _command(self, command_id):
        task = self.commands.get(command_id)
        if task is None:
            raise not_found("Command not found")
        return task

    def _repo(self, project, job):
        return Repository(project.cache, project.worktree, project.url, job, self.base)

    def list_projects(self):
        with self.lock:
            return [project.snapshot() for project in self.projects.values()]

    def get_project(self, name):
        with self.lock:
            return self._project(name).snapshot()

    def create(self, raw):
        name, url = project_body(raw)
        key = name.lower()
        with self.lock:
            self._available()
            if key in self.projects:
                raise conflict("Project name is already registered")
            project = Project(name, url, Job(), self.projects_dir, self.caches_dir)
            self.projects[key] = project
        success = False
        try:
            _remove_tree(project.worktree)
            _remove_tree(project.cache)
            self._repo(project, project.clone_job).create()
            with self.lock:
                if project.lifecycle == "deleting" or self.closing:
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
            try:
                project.clone_job.close()
                if not success:
                    try:
                        _remove_tree(project.worktree)
                        _remove_tree(project.cache)
                    except OSError:
                        with self.lock:
                            project.lifecycle = "deleting"
                        raise
                    with self.lock:
                        if not project.delete_in_progress:
                            self.projects.pop(key, None)
            finally:
                project.cloned.set()

    def delete_project(self, name):
        with self.lock:
            self._available()
            project = self._project(name)
            if project.delete_in_progress:
                raise conflict("Project deletion is already in progress")
            project.delete_in_progress = True
            project.lifecycle = "deleting"
            project.visible = False
            task = project.task
        success = False
        try:
            if not project.cloned.is_set():
                project.clone_job.cancel()
            if task is not None:
                task.cancel()
            project.cloned.wait()
            if task is not None:
                task.done.wait()
            self._wait_readers(project)
            _remove_tree(project.worktree)
            _remove_tree(project.cache)
            success = True
        finally:
            with self.lock:
                if success:
                    self.projects.pop(project.name.lower(), None)
                else:
                    project.delete_in_progress = False

    @contextmanager
    def file_entry(self, name, relative):
        with self.lock:
            project = self._project(name)
            if not project.visible or project.lifecycle is not None:
                raise not_found("The current project directory is not available")
            project.readers += 1
        try:
            with pinned_entry(project.worktree, relative) as entry:
                yield entry
        finally:
            with self.reader_changed:
                project.readers -= 1
                self.reader_changed.notify_all()

    def _wait_readers(self, project):
        with self.reader_changed:
            self.reader_changed.wait_for(lambda: project.readers == 0)

    def start_action(self, name, raw, accepted_at):
        with self.lock:
            self._available()
            project = self._project(name)
            if project.lifecycle is not None or (project.task and not project.task.done.is_set()):
                raise conflict("Project or action slot is busy")
            task = Task("reset")
            project.task, project.visible = task, False
        try:
            values, error = action_body(raw), None
        except APIError as exc:
            values, error = None, exc
        self._start_thread(self._run_action, task, project, values, error)
        if error is not None:
            task.done.wait()
            raise error
        return task.response(values[-1], accepted_at)

    @staticmethod
    def _start_thread(worker, task, *args):
        try:
            threading.Thread(target=worker, args=(task, *args), daemon=True).start()
        except Exception as exc:
            error = APIError(500, "worker_unavailable", "Could not start worker thread")
            task.finish(error=error, source="runner", stderr=str(exc).encode("utf-8", "replace"))
            raise error from exc

    def _run_action(self, task, project, values, validation_error):
        result = {}
        checkout_started = False
        try:
            self._wait_readers(project)
            _remove_tree(project.worktree)
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
                raise APIError(400, "invalid_script", f"Cannot use script path: {exc}") from exc
            exe, options = config.INTERPRETERS[interpreter]
            task.stage("launch")
            if not exe:
                raise FileNotFoundError(f"Configured interpreter {interpreter!r} is not installed")
            result["exit_code"] = task.run([exe, *options, script_path, *args], executable=exe,
                                          cwd=project.worktree)
        except JobCancelled as exc:
            if task.phase == "run":
                result["exit_code"] = exc.exit_code
        except GitFailure as exc:
            result.update(error=APIError(200, "git_failed", str(exc)), source="git",
                          stdout=exc.stdout, stderr=exc.stderr)
        except CommitError as exc:
            result.update(_failure(APIError(200, exc.code, str(exc)), "runner"))
        except APIError as exc:
            result.update(_failure(exc, "runner"))
        except Exception as exc:
            source = {"launch": "launch", "run": "process"}.get(task.phase, "runner")
            code = {"launch": "launch_failed", "process": "process_error", "runner": "runner_error"}[source]
            result.update(_failure(APIError(200, code, str(exc)), source))
        finally:
            if checkout_started:
                with self.lock:
                    if project.lifecycle is None:
                        project.visible = project.worktree.is_dir()
            task.finish(**result)

    def get_action(self, name, offsets=None, limit=65536):
        with self.lock:
            project = self._project(name)
            if project.lifecycle == "deleting":
                raise conflict("Project is being deleted")
            if project.task is None:
                raise not_found("No action has been submitted")
            return project.task.snapshot(offsets, limit)

    def cancel_action(self, name):
        with self.lock:
            self._available()
            project = self._project(name)
            if project.lifecycle == "deleting":
                raise conflict("Project is being deleted")
            if project.task is None:
                raise not_found("No action has been submitted")
            task = project.task
            task.cancel()
        task.done.wait()

    def start_command(self, raw, accepted_at):
        mode, command, timeout = command_body(raw)
        with self.lock:
            self._available()
            command_id = uuid.uuid4().hex
            while command_id in self.commands:
                command_id = uuid.uuid4().hex
            task = Task("launch", command_id=command_id, mode=mode)
            self.commands[command_id] = task
        self._start_thread(self._run_command, task, command)
        return task.response(timeout, accepted_at)

    @staticmethod
    def _run_command(task, command):
        result = {}
        try:
            if task.mode == "ps":
                executable = str(config.POWERSHELL)
                encoded = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
                argv = [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
            else:
                executable = str(config.CMD)
                argv = subprocess.list2cmdline([executable]) + ' /d /s /c "' + command + '"'
            result["exit_code"] = task.run(argv, executable=executable, cwd=Path.home())
        except JobCancelled:
            pass
        except Exception as exc:
            source = "process" if task.phase == "run" else "launch"
            result = _failure(APIError(200, "process_error" if source == "process" else "launch_failed",
                                       str(exc)), source)
        finally:
            task.finish(**result)

    def list_commands(self):
        with self.lock:
            return [task.snapshot() for task in self.commands.values()]

    def get_command(self, command_id, offsets=None, limit=65536):
        with self.lock:
            return self._command(command_id).snapshot(offsets, limit)

    def delete_command(self, command_id):
        with self.lock:
            self._available()
            task = self._command(command_id)
            if task.deleting:
                raise conflict("Command deletion is already in progress")
            task.deleting = True
            try:
                task.cancel()
            except Exception:
                task.deleting = False
                raise
        task.done.wait()
        with self.lock:
            self.commands.pop(command_id, None)

    def close(self):
        with self.lock:
            self.closing = True
            projects = list(self.projects.values())
            tasks = list(self.commands.values()) + [p.task for p in projects if p.task is not None]
        try:
            for project in projects:
                if not project.cloned.is_set():
                    project.clone_job.cancel()
            for task in tasks:
                task.cancel()
            for project in projects:
                project.cloned.wait()
            for task in tasks:
                task.done.wait()
        finally:
            self.lease.close()


def _failure(error, source):
    return {"error": error, "source": source, "stderr": str(error).encode("utf-8", "replace")}
