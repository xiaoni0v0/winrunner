"""Windows runner regressions with local files and no Git or child processes."""

import base64
from concurrent.futures import Future
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from typing import TypeVar
from unittest.mock import patch

from winrunner import runner

PROJECT_BODY = json.dumps({"repo_url": "https://example.invalid/repo.git"}).encode(
    "utf-8"
)
COMMIT = "a" * 40
ACTION = b'{"commit":"aaaa","interpreter":"python","script":"script.py","timeout":0}'
COMMAND = b'{"mode":"cmd","command":"exit 0","timeout":30}'
OFFSETS = {"stdout": 0, "stderr": 0}
WAIT = 5
T = TypeVar("T")


class CommandIDTests(unittest.TestCase):
    def test_short_command_id_uses_lowercase_base36(self):
        for number, expected in (
            (0, "0"),
            (1, "1"),
            (9, "9"),
            (10, "a"),
            (35, "z"),
            (36, "10"),
            (1295, "zz"),
            (1296, "100"),
        ):
            with self.subTest(number=number):
                self.assertEqual(runner.short_command_id(number), expected)


class LocalRepository:
    def __init__(self, cache, worktree, url, job, base):
        self.cache = cache
        self.worktree = worktree

    def create(self):
        self.cache.mkdir()
        self.materialize(COMMIT)

    def fetch(self):
        pass

    def resolve(self, prefix):
        return COMMIT

    def materialize(self, commit):
        self.worktree.mkdir()
        (self.worktree / "script.py").touch()


@unittest.skipUnless(os.name == "nt", "Windows runner uses a Windows file lease")
class RunnerRegressions(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory(prefix="winrunner-regressions-")
        self.addCleanup(scratch.cleanup)
        self.model = runner.Runner(Path(scratch.name) / "data")
        # Do not wait indefinitely in close() if a regressed worker never signals done.
        self.addCleanup(self.model.lease.close)
        repo = patch.object(runner, "Repository", LocalRepository)
        repo.start()
        self.addCleanup(repo.stop)
        native_job = patch.object(runner, "Job", autospec=True)
        self.job = native_job.start().return_value
        self.addCleanup(native_job.stop)
        self.job.run.side_effect = self.successful_process

    @staticmethod
    def successful_process(*args, on_started, stdout, stderr, **kwargs):
        on_started()
        stdout(b"process stdout")
        stderr(b"process stderr")
        return 0

    def create(self, name="sample"):
        self.model.create(name, PROJECT_BODY)
        return self.model.projects[name.lower()]

    def assert_api_error(self, status, code, operation, *args, **kwargs):
        with self.assertRaises(runner.APIError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(
            (caught.exception.status, caught.exception.code), (status, code)
        )
        return caught.exception

    def background(self, operation: Callable[..., T], *args: object) -> Future[T]:
        outcome: Future[T] = Future()

        def run():
            try:
                outcome.set_result(operation(*args))
            except BaseException as exc:
                outcome.set_exception(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.addCleanup(thread.join, WAIT)
        return outcome

    def start_action(self, project):
        self.model.start_action(project.name, ACTION, time.monotonic())
        return project.task

    def wait_action(self, project, task):
        self.assertTrue(task.done.wait(WAIT), "action never signalled completion")
        return self.model.get_action(project.name, OFFSETS)

    def test_invalid_drain_timeout_does_not_latch_deletion(self):
        project = self.create()
        for timeout in ("5", True, -1, float("inf"), float("nan"), 10**100):
            with self.subTest(timeout=timeout):
                with patch.object(runner.config, "PROJECT_DRAIN_TIMEOUT", timeout):
                    self.assert_api_error(
                        500, "invalid_config", self.model.delete_project, project.name
                    )
                self.assertTrue(project.delete_done.is_set())
                self.assertFalse(project.delete_in_progress)
                self.assertTrue(project.visible)
        with patch.object(runner.config, "PROJECT_DRAIN_TIMEOUT", "5"):
            task = self.wait_action(project, self.start_action(project))
        self.assertEqual(task["error"]["code"], "invalid_config")
        self.model.delete_project(project.name)
        self.background(self.model.close).result(timeout=WAIT)

    def test_validation_exception_finishes_accepted_action_and_command(self):
        project = self.create()
        with (
            patch.object(runner.config, "INTERPRETERS", None),
            self.assertLogs(runner.LOG, level="ERROR"),
        ):
            self.assert_api_error(500, "validation_failed", self.start_action, project)
        self.assertFalse(project.worktree.exists())
        action = self.wait_action(project, project.task)
        self.assertEqual((action["status"], action["phase"]), ("failed", "validate"))
        self.assertEqual(action["error"]["code"], "validation_failed")
        with (
            patch.object(runner, "command_body", side_effect=RuntimeError("validator")),
            self.assertLogs(runner.LOG, level="ERROR"),
        ):
            self.assert_api_error(
                500,
                "validation_failed",
                self.model.start_command,
                COMMAND,
                time.monotonic(),
            )
        self.assertEqual(self.model.get_command("0")["status"], "failed")
        self.wait_action(project, self.start_action(project))
        self.background(self.model.close).result(timeout=WAIT)

    def test_shutdown_waits_for_project_deletion_before_releasing_lease(self):
        project = self.create()
        deleting, resume, draining = (threading.Event() for _ in range(3))
        clean = runner._clean_paths
        wait = project.delete_done.wait

        def gated_clean(paths):
            deleting.set()
            if not resume.wait(WAIT):
                raise TimeoutError("test did not release directory cleanup")
            return clean(paths)

        def observe_wait(timeout=None):
            draining.set()
            return wait(timeout)

        with (
            patch.object(runner, "_clean_paths", side_effect=gated_clean),
            patch.object(project.delete_done, "wait", side_effect=observe_wait),
        ):
            deletion = self.background(self.model.delete_project, project.name)
            try:
                self.assertTrue(deleting.wait(WAIT))
                shutdown = self.background(self.model.close)
                self.assertTrue(draining.wait(WAIT))
                self.assertFalse(self.model.lease.closed)
                with self.assertRaisesRegex(RuntimeError, "already in use"):
                    runner.Runner(self.model.base)
            finally:
                resume.set()
            deletion.result(timeout=WAIT)
            shutdown.result(timeout=WAIT)
        self.assertFalse(project.worktree.exists())
        self.assertTrue(self.model.lease.closed)

    def test_shutdown_drains_other_tasks_when_one_cancellation_fails(self):
        first, second = runner.Task("launch"), runner.Task("launch")
        self.model.commands.update({"0": first, "1": second})
        cancelled = threading.Event()
        with (
            patch.object(
                first.job, "request_stop", side_effect=OSError("cancel failed")
            ),
            patch.object(second.job, "request_stop", side_effect=cancelled.set),
            self.assertLogs(runner.LOG, "ERROR"),
        ):
            shutdown = self.background(self.model.close)
            try:
                self.assertTrue(cancelled.wait(WAIT))
                self.assertFalse(self.model.lease.closed)
                self.assertFalse(shutdown.done())
            finally:
                first.finish()
                second.finish()
            shutdown.result(timeout=WAIT)
        self.assertTrue(self.model.lease.closed)

    def test_incomplete_shutdown_keeps_data_lease_and_can_retry(self):
        task = runner.Task("run", command_id="0")
        self.model.commands["0"] = task
        with (
            patch.object(runner.config, "PROJECT_DRAIN_TIMEOUT", 0.02),
            patch.object(task.job, "request_stop") as stop,
        ):
            with self.assertRaisesRegex(runner.ShutdownIncomplete, "remains locked"):
                self.model.close()
            self.assertFalse(self.model.lease.closed)
            with self.assertRaisesRegex(RuntimeError, "already in use"):
                runner.Runner(self.model.base)
            self.assert_api_error(
                503,
                "shutting_down",
                self.model.start_command,
                COMMAND,
                time.monotonic(),
            )
            stop.side_effect = task.finish
            self.model.close()
            self.assertEqual(stop.call_count, 2)
        self.assertTrue(self.model.lease.closed)

    def test_shutdown_rejects_new_readers_and_drains_existing_reader(self):
        project = self.create()
        opened, interrupted, release = (threading.Event() for _ in range(3))

        def read():
            with self.model.file_entry(
                project.name, "script.py", cancel_read=interrupted.set
            ):
                opened.set()
                if not release.wait(WAIT):
                    raise TimeoutError("test did not release file reader")

        reader = self.background(read)
        try:
            self.assertTrue(opened.wait(WAIT))
            shutdown = self.background(self.model.close)
            self.assertTrue(interrupted.wait(WAIT))
            self.assert_api_error(503, "shutting_down", self.open_script, project)
            self.assertFalse(self.model.lease.closed)
        finally:
            release.set()
        reader.result(timeout=WAIT)
        shutdown.result(timeout=WAIT)
        self.assertTrue(self.model.lease.closed)

    def test_monitor_error_keeps_raw_process_output(self):
        project = self.create()

        def fail_after_start(*args, on_started, stdout, stderr, **kwargs):
            on_started()
            stdout(b"partial")
            stderr(b"\xff\x80")
            raise OSError("monitor failed")

        self.job.run.side_effect = fail_after_start
        action = self.wait_action(project, self.start_action(project))
        status, result = self.model.start_command(COMMAND, time.monotonic())
        self.assertEqual(status, 200)
        command = self.model.get_command(result["id"], OFFSETS)
        for task in (action, command):
            self.assertEqual(task["status"], "failed")
            self.assertEqual(task["error"]["code"], "process_error")
            self.assertEqual(task["output_source"], "process")
            self.assertEqual(task["stderr"]["next_offset"], 2)
            self.assertEqual(
                base64.b64decode(task["stderr"]["data_base64"]), b"\xff\x80"
            )

    def test_cmd_does_not_launch_with_a_unc_working_directory(self):
        task = runner.Task("launch", command_id="0", mode="cmd")
        self.model._run_command(task, "echo hello", Path(r"\\server\share\project"))
        self.job.run.assert_not_called()
        result = task.snapshot()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "launch_failed")

    def test_repo_url_rejects_even_empty_query_and_fragment(self):
        url = "https://example.invalid/a%3Fb%23c.git"
        self.assertEqual(
            runner.project_body("demo", json.dumps({"repo_url": url}).encode()), url
        )
        for suffix in ("?", "#"):
            raw = json.dumps({"repo_url": url + suffix}).encode()
            self.assert_api_error(
                400, "invalid_request", runner.project_body, "demo", raw
            )

    def test_worktree_status_error_finishes_action_and_releases_slot(self):
        # #01: successful execution must still finish if final inspection fails.
        project = self.create()
        path_type = type(project.worktree)
        is_dir = path_type.is_dir

        def broken_is_dir(path):
            if path == project.worktree:
                raise PermissionError("worktree stat failed")
            return is_dir(path)

        with (
            patch.object(path_type, "is_dir", broken_is_dir),
            self.assertLogs(runner.LOG, "ERROR"),
        ):
            task = self.start_action(project)
            result = self.wait_action(project, task)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "worktree_status_failed")
        self.assertIn("worktree stat failed", result["error"]["message"])
        self.assertEqual(result["output_source"], "process")
        self.assertEqual(
            base64.b64decode(result["stderr"]["data_base64"]), b"process stderr"
        )
        self.assert_api_error(404, "not_found", self.open_script, project)

        replacement = self.start_action(project)
        self.assertIsNot(task, replacement)
        self.assertEqual(self.wait_action(project, replacement)["status"], "succeeded")

    def open_script(self, project):
        with self.model.file_entry(project.name, "script.py"):
            pass

    def test_cancel_action_wakes_reader_drain_before_reader_exits(self):
        # #02: deliberately hold a real reader beyond action cancellation.
        project = self.create()
        draining = threading.Event()
        condition_wait = self.model.reader_changed.wait

        def signal_wait(timeout=None):
            draining.set()
            return condition_wait(timeout)

        with (
            patch.object(runner.config, "PROJECT_DRAIN_TIMEOUT", 30),
            patch.object(self.model.reader_changed, "wait", side_effect=signal_wait),
            self.model.file_entry(project.name, "script.py"),
        ):
            task = self.start_action(project)
            self.assertTrue(draining.wait(WAIT), "action never waited for the reader")
            cancellation = self.background(self.model.cancel_action, project.name)
            cancellation.result(timeout=WAIT)
            result = self.wait_action(project, task)
            self.assertEqual(
                (result["status"], result["phase"]), ("cancelled", "reset")
            )
            self.assertTrue((project.worktree / "script.py").exists())
        self.job.run.assert_not_called()
        self.assertEqual(
            self.wait_action(project, self.start_action(project))["status"], "succeeded"
        )

    def test_delete_times_out_with_live_reader_and_can_be_retried(self):
        # #02: a reader without a callback cannot be interrupted by DELETE.
        project = self.create()
        with (
            patch.object(runner.config, "PROJECT_DRAIN_TIMEOUT", 0.05),
            self.model.file_entry(project.name, "script.py"),
        ):
            deletion = self.background(self.model.delete_project, project.name)
            error = self.assert_api_error(
                409, "project_busy", deletion.result, timeout=WAIT
            )
            self.assertIn("retry", str(error))
            self.assertEqual(
                self.model.get_project(project.name)["status"], "delete_failed"
            )
            self.assertTrue(project.worktree.exists())
        self.model.delete_project(project.name)
        self.assert_api_error(404, "not_found", self.model.get_project, project.name)
        self.assertFalse(project.worktree.exists())

    def test_delete_interrupts_reader_and_waits_for_file_release(self):
        # #02: a callback can wake the reader to close the real pinned handle.
        project = self.create()
        opened, release = threading.Event(), threading.Event()

        def read():
            with self.model.file_entry(
                project.name, "script.py", cancel_read=release.set
            ):
                opened.set()
                self.assertTrue(release.wait(2 * WAIT), "reader was not interrupted")

        reader = self.background(read)
        try:
            self.assertTrue(opened.wait(WAIT), "reader did not open the file")
            self.model.delete_project(project.name)
            reader.result(timeout=WAIT)
            self.assertTrue(release.is_set())
            self.assertFalse(project.worktree.exists())
        finally:
            release.set()

    def test_failed_clone_rollback_stays_private_and_retry_cleans_stale_path(self):
        # #04: simulate a sharing violation only after the clone leaves files.
        worktree = self.model.projects_dir / "Sample"
        cache = self.model.caches_dir / "Sample"
        remove_tree = runner._remove_tree

        def fail_clone(repo):
            repo.cache.mkdir()
            repo.worktree.mkdir()
            (repo.worktree / "partial").touch()
            raise runner.GitFailure(
                "clone exited 128", stderr=b"simulated Git transport error\n"
            )

        def block_rollback(path):
            if path == worktree and (path / "partial").exists():
                raise PermissionError("worktree handle is held")
            remove_tree(path)

        with (
            patch.object(LocalRepository, "create", fail_clone),
            patch.object(runner, "_remove_tree", side_effect=block_rollback),
            self.assertLogs(runner.LOG, "WARNING") as logs,
        ):
            error = self.assert_api_error(
                502, "git_clone_failed", self.model.create, "Sample", PROJECT_BODY
            )
            self.assertEqual(str(error), "simulated Git transport error")

        self.assertIn("worktree handle is held", "\n".join(logs.output))
        self.assert_api_error(404, "not_found", self.model.get_project, "sample")
        self.assertEqual(self.model.list_projects(), [])
        self.assertEqual(self.model.pending_cleanup["sample"], (worktree,))
        self.assertTrue((worktree / "partial").is_file())
        self.assertFalse(cache.exists(), "unblocked paths should still be cleaned")

        with (
            patch.object(runner, "_remove_tree", side_effect=block_rollback),
            patch.object(LocalRepository, "create", autospec=True) as clone,
            self.assertLogs(runner.LOG, "WARNING"),
        ):
            self.assert_api_error(
                409,
                "project_cleanup_blocked",
                self.model.create,
                "sample",
                PROJECT_BODY,
            )
            clone.assert_not_called()
        self.assert_api_error(404, "not_found", self.model.get_project, "sample")

        # LocalRepository.create uses mkdir(exist_ok=False): it cannot reuse stale paths.
        retried = self.model.create("sample", PROJECT_BODY)
        self.assertEqual(retried["status"], "idle")
        self.assertNotIn("sample", self.model.pending_cleanup)
        self.assertFalse((worktree / "partial").exists())

    def test_delete_cleanup_failure_reports_retryable_project_state(self):
        # #04: failed cleanup of an existing project is distinct from ongoing deletion.
        project = self.create()
        remove_tree = runner._remove_tree

        def block_worktree(path):
            if path == project.worktree:
                raise PermissionError("open worktree handle")
            remove_tree(path)

        with patch.object(runner, "_remove_tree", side_effect=block_worktree):
            error = self.assert_api_error(
                409, "project_cleanup_blocked", self.model.delete_project, project.name
            )
        self.assertIn("retry", str(error))
        self.assertEqual(
            self.model.get_project(project.name)["status"], "delete_failed"
        )
        self.assert_api_error(404, "not_found", self.open_script, project)
        self.model.delete_project(project.name)
        self.assert_api_error(404, "not_found", self.model.get_project, project.name)
        self.assertFalse(project.worktree.exists())

    def test_failed_delete_of_cloning_project_keeps_name_for_retry(self):
        cloning, cancelled = threading.Event(), threading.Event()

        def clone(repo):
            repo.worktree.mkdir()
            cloning.set()
            self.assertTrue(cancelled.wait(WAIT))
            raise runner.JobCancelled()

        remove_tree = runner._remove_tree

        def remove(path):
            if cancelled.is_set() and path == self.model.projects["sample"].worktree:
                raise PermissionError("worktree busy")
            remove_tree(path)

        with (
            patch.object(runner.Repository, "create", clone),
            patch.object(runner.DeferredJob, "request_stop", side_effect=cancelled.set),
            patch.object(runner, "_remove_tree", side_effect=remove),
        ):
            creation = self.background(self.model.create, "sample", PROJECT_BODY)
            try:
                self.assertTrue(cloning.wait(WAIT))
                self.assert_api_error(
                    409, "project_cleanup_blocked", self.model.delete_project, "sample"
                )
                self.assert_api_error(409, "conflict", creation.result, timeout=WAIT)
            finally:
                cancelled.set()
        self.assertEqual(self.model.get_project("sample")["status"], "delete_failed")
        self.assert_api_error(
            409, "conflict", self.model.create, "sample", PROJECT_BODY
        )
        self.model.delete_project("sample")
        self.assert_api_error(404, "not_found", self.model.get_project, "sample")

    def test_project_commands_reject_submits_only_during_active_deletion(self):
        project = self.create()
        entered, release = threading.Event(), threading.Event()

        def block_cleanup(paths):
            entered.set()
            self.assertTrue(release.wait(2 * WAIT), "test never released deletion")
            return {project.worktree: PermissionError("open worktree handle")}

        def start_command():
            return self.model.start_command(COMMAND, time.monotonic(), project=project)

        with patch.object(runner, "_clean_paths", side_effect=block_cleanup):
            deletion = self.background(self.model.delete_project, project.name)
            try:
                self.assertTrue(entered.wait(WAIT), "deletion never started cleanup")
                self.assert_api_error(409, "conflict", start_command)
                self.assertEqual(self.model.list_commands(name=project.name), [])
            finally:
                release.set()
            self.assert_api_error(
                409, "project_cleanup_blocked", deletion.result, timeout=WAIT
            )

        self.assertEqual(
            self.model.get_project(project.name)["status"], "delete_failed"
        )
        status, result = start_command()
        self.assertEqual(
            (status, result["id"], result["status"]), (200, "0", "succeeded")
        )
        self.model.delete_project(project.name)
        self.create(project.name)
        self.assert_api_error(404, "not_found", start_command)

    def test_failed_clone_cancels_project_commands_before_rollback(self):
        cloning, fail_clone = threading.Event(), threading.Event()
        running, cancelled = threading.Event(), threading.Event()

        def clone(repo):
            repo.cache.mkdir()
            repo.worktree.mkdir()
            cloning.set()
            self.assertTrue(fail_clone.wait(2 * WAIT), "test never released clone")
            raise runner.GitFailure("clone failed")

        def process(*args, on_started, cwd, **kwargs):
            on_started()
            running.set()
            self.assertTrue(cancelled.wait(2 * WAIT), "rollback did not stop command")
            self.assertTrue(cwd.is_dir(), "rollback removed the worktree too early")
            raise runner.JobCancelled(exit_code=23)

        self.job.run.side_effect = process
        self.job.cancel.side_effect = cancelled.set
        with patch.object(LocalRepository, "create", clone):
            creation = self.background(self.create)
            try:
                self.assertTrue(cloning.wait(WAIT), "clone never started")
                project = self.model.command_project("sample")
                status, command = self.model.start_command(
                    COMMAND.replace(b'"timeout":30', b'"timeout":0'),
                    time.monotonic(),
                    project=project,
                )
                self.assertEqual((status, command["id"]), (202, "0"))
                self.assertTrue(running.wait(WAIT), "project command never started")
            finally:
                fail_clone.set()
            self.assert_api_error(
                502, "git_clone_failed", creation.result, timeout=WAIT
            )

        self.assertTrue(cancelled.is_set())
        self.assertEqual(project.commands, {})
        self.assertFalse(project.worktree.exists())
        self.assert_api_error(404, "not_found", self.model.list_commands, name="sample")

    def test_failed_clone_keeps_project_for_delete_if_commands_do_not_stop(self):
        cloning, fail_clone, running, stop = (threading.Event() for _ in range(4))

        def clone(repo):
            repo.cache.mkdir()
            repo.worktree.mkdir()
            (repo.worktree / "keep.txt").touch()
            cloning.set()
            self.assertTrue(fail_clone.wait(WAIT))
            raise runner.GitFailure("clone failed")

        def command(*args, on_started, **kwargs):
            on_started()
            running.set()
            self.assertTrue(stop.wait(WAIT))
            return 0

        self.job.run.side_effect = command
        with (
            patch.object(LocalRepository, "create", clone),
            patch.object(runner.config, "PROJECT_DRAIN_TIMEOUT", 0.02),
            self.assertLogs(runner.LOG, "WARNING"),
        ):
            creation = self.background(self.create)
            try:
                self.assertTrue(cloning.wait(WAIT))
                project = self.model.command_project("sample")
                self.model.start_command(
                    COMMAND.replace(b'"timeout":30', b'"timeout":0'),
                    time.monotonic(),
                    project=project,
                )
                self.assertTrue(running.wait(WAIT))
                fail_clone.set()
                self.assert_api_error(
                    409, "project_busy", creation.result, timeout=WAIT
                )
                self.assertEqual(
                    self.model.get_project("sample")["status"], "delete_failed"
                )
                self.assertTrue((project.worktree / "keep.txt").exists())
                self.assertEqual(
                    self.model.get_command("0", name="sample")["status"], "cancelling"
                )
                self.assert_api_error(409, "conflict", self.create)
                self.assert_api_error(
                    409,
                    "conflict",
                    lambda: self.model.start_command(
                        COMMAND, time.monotonic(), project=project
                    ),
                )
            finally:
                fail_clone.set()
                stop.set()
        self.assertTrue(project.commands["0"].done.wait(WAIT))
        self.model.delete_project("sample")
        self.assertFalse(project.worktree.exists())
        self.assertFalse(project.cache.exists())

    def test_failed_clone_discards_commands_after_delete_times_out(self):
        cloning, release = threading.Event(), threading.Event()

        def clone(repo):
            repo.worktree.mkdir()
            cloning.set()
            self.assertTrue(release.wait(2 * WAIT))
            raise runner.GitFailure("clone failed")

        with patch.object(LocalRepository, "create", clone):
            creation = self.background(self.create)
            try:
                self.assertTrue(cloning.wait(WAIT))
                project = self.model.command_project("sample")
                self.model.start_command(COMMAND, time.monotonic(), project=project)
                with patch.object(runner.config, "PROJECT_DRAIN_TIMEOUT", 0):
                    self.assert_api_error(
                        409, "project_busy", self.model.delete_project, "sample"
                    )
            finally:
                release.set()
            self.assert_api_error(
                502, "git_clone_failed", creation.result, timeout=WAIT
            )
        self.assertEqual(self.model.get_project("sample")["status"], "delete_failed")
        self.assertEqual(self.model.list_commands(name="sample"), [])
        self.assert_api_error(
            404, "not_found", self.model.get_command, "0", name="sample"
        )
        self.model.delete_project("sample")

    def test_delete_drain_deadline_survives_slow_native_cancel(self):
        project = self.create()
        running, cancelling, release = (threading.Event() for _ in range(3))

        def process(*args, on_started, **kwargs):
            on_started()
            running.set()
            self.assertTrue(release.wait(2 * WAIT))
            return 0

        def cancel():
            cancelling.set()
            self.assertTrue(release.wait(2 * WAIT))

        self.job.run.side_effect = process
        self.job.cancel.side_effect = cancel
        self.model.start_command(
            COMMAND.replace(b'"timeout":30', b'"timeout":0'),
            time.monotonic(),
            project=project,
        )
        try:
            self.assertTrue(running.wait(WAIT))
            with patch.object(runner.config, "PROJECT_DRAIN_TIMEOUT", 0.1):
                deletion = self.background(self.model.delete_project, project.name)
                self.assertTrue(cancelling.wait(WAIT))
                self.assert_api_error(
                    409, "project_busy", deletion.result, timeout=WAIT
                )
        finally:
            release.set()
        self.assertTrue(project.commands["0"].done.wait(WAIT))
        self.model.delete_project(project.name)

    def test_filter_matches_snapshot_status_and_tail_can_resume_incrementally(self):
        project = self.create()
        for index, status in enumerate(
            ("preparing", "running", "cancelling", "succeeded", "failed", "cancelled")
        ):
            task = runner.Task("run", command_id=str(index))
            task.status = status
            self.model.commands[str(index)] = task
        task = self.model.commands["1"]
        project.commands["0"] = runner.Task("validate", command_id="0")
        task.append("stdout", b"abc\x00\xff")
        task.append("stderr", b"1234")
        self.assertEqual(
            [
                row["id"]
                for row in self.model.list_commands(
                    statuses=runner.ACTIVE_TASK_STATUSES
                )
            ],
            ["0", "1", "2"],
        )
        self.assertEqual(
            self.model.list_commands(name=project.name, statuses={"running"}), []
        )
        self.assertEqual(
            [row["id"] for row in self.model.list_commands(statuses={"cancelled"})],
            ["5"],
        )
        snapshot = self.model.get_command("1", {"stderr": 1}, 2, tails={"stdout": 3})
        self.assertEqual(
            base64.b64decode(snapshot["stdout"]["data_base64"]), b"c\x00\xff"
        )
        self.assertEqual(snapshot["stdout"]["next_offset"], snapshot["stdout_bytes"])
        self.assertEqual(base64.b64decode(snapshot["stderr"]["data_base64"]), b"23")
        task.append("stdout", b"new")
        continued = self.model.get_command(
            "1", {"stdout": snapshot["stdout"]["next_offset"]}
        )
        self.assertEqual(base64.b64decode(continued["stdout"]["data_base64"]), b"new")
        empty = self.model.get_command("0", name=project.name, tails={"stderr": 10})
        self.assertEqual(empty["stderr"], {"data_base64": "", "next_offset": 0})
        self.assertNotIn("stdout", empty)

    def test_concurrent_command_cancel_waits_and_keeps_same_terminal_snapshot(self):
        running, terminating, release = (threading.Event() for _ in range(3))

        def active_process(*args, on_started, stdout, **kwargs):
            on_started()
            stdout(b"keep")
            running.set()
            self.assertTrue(release.wait(2 * WAIT))
            raise runner.JobCancelled(exit_code=23)

        def force_stop():
            terminating.set()
            self.assertTrue(release.wait(2 * WAIT))

        self.job.run.side_effect = active_process
        self.job.cancel.side_effect = force_stop
        submitted = self.background(self.model.start_command, COMMAND, time.monotonic())
        try:
            self.assertTrue(running.wait(WAIT))
            first = self.background(self.model.cancel_command, "0")
            self.assertTrue(terminating.wait(WAIT))
            second = self.background(self.model.cancel_command, "0")
            self.assertEqual(self.model.get_command("0")["status"], "cancelling")
            self.assertFalse(first.done())
            self.assertFalse(second.done())
        finally:
            release.set()
        first.result(timeout=WAIT)
        second.result(timeout=WAIT)
        submitted.result(timeout=WAIT)
        final = self.model.get_command("0", OFFSETS)
        self.assertEqual((final["status"], final["exit_code"]), ("cancelled", 23))
        self.assertEqual(base64.b64decode(final["stdout"]["data_base64"]), b"keep")
        self.model.cancel_command("0")
        self.assertEqual(self.model.get_command("0", OFFSETS), final)
        self.job.cancel.assert_called_once_with()
        self.model.delete_command("0")
        self.assert_api_error(404, "not_found", self.model.cancel_command, "0")

    def test_failed_force_cancel_can_be_retried_without_deleting_command(self):
        running, release = threading.Event(), threading.Event()

        def active_process(*args, on_started, **kwargs):
            on_started()
            running.set()
            self.assertTrue(release.wait(2 * WAIT))
            raise runner.JobCancelled()

        self.job.run.side_effect = active_process
        self.model.start_command(
            COMMAND.replace(b'"timeout":30', b'"timeout":0'), time.monotonic()
        )
        try:
            self.assertTrue(running.wait(WAIT))
            self.job.cancel.side_effect = OSError("termination failed")
            with self.assertRaisesRegex(OSError, "termination failed"):
                self.model.cancel_command("0")
            self.assertEqual(self.model.get_command("0")["status"], "cancelling")
            self.job.cancel.side_effect = release.set
            self.background(self.model.cancel_command, "0").result(timeout=WAIT)
            self.assertEqual(self.model.get_command("0")["status"], "cancelled")
            self.assertEqual(self.job.cancel.call_count, 2)
        finally:
            release.set()

    def test_cancelled_command_reports_reaped_exit_code(self):
        # #06: DELETE cancels while the caller is still waiting for the command result.
        running, release = threading.Event(), threading.Event()

        def interrupted_process(*args, on_started, **kwargs):
            on_started()
            running.set()
            self.assertTrue(
                release.wait(2 * WAIT), "command cancellation did not reach the job"
            )
            raise runner.JobCancelled(exit_code=23)

        self.job.run.side_effect = interrupted_process
        self.job.cancel.side_effect = release.set
        response = self.background(self.model.start_command, COMMAND, time.monotonic())
        try:
            self.assertTrue(running.wait(WAIT), "command did not start")
            command = self.model.list_commands()[0]
            self.assertEqual(command["status"], "running")
            deletion = self.background(self.model.delete_command, command["id"])
            status, result = response.result(timeout=WAIT)
            deletion.result(timeout=WAIT)
            self.assertEqual(
                (status, result["status"], result["exit_code"]), (200, "cancelled", 23)
            )
        finally:
            release.set()

    def test_worker_start_failure_physically_resets_previous_worktree(self):
        # #14: failure to spawn does not skip the reset for the accepted round.
        project = self.create()
        old = project.worktree / "old.txt"
        old.write_text("previous round", encoding="utf-8")
        with patch.object(
            threading.Thread,
            "start",
            side_effect=RuntimeError("cannot start test thread"),
        ):
            self.assert_api_error(500, "worker_unavailable", self.start_action, project)
        self.assertFalse(project.worktree.exists())
        result = self.wait_action(project, project.task)
        self.assertEqual(
            (result["status"], result["phase"], result["output_source"]),
            ("failed", "reset", "runner"),
        )
        self.assertIn(
            b"cannot start test thread",
            base64.b64decode(result["stderr"]["data_base64"]),
        )
        self.job.run.assert_not_called()

    def test_worker_start_failure_logs_incomplete_reset_and_hides_old_files(self):
        # #14: a blocked reset logs both failures without retaining the old outputs.
        project = self.create()
        prior = self.wait_action(project, self.start_action(project))
        self.assertGreater(prior["stdout_bytes"], 0)
        with (
            patch.object(
                threading.Thread,
                "start",
                side_effect=RuntimeError("cannot start test thread"),
            ),
            patch.object(
                runner,
                "_remove_tree",
                side_effect=PermissionError("old worktree is in use"),
            ) as remove,
        ):
            error = self.assert_api_error(
                500, "worker_unavailable_reset_incomplete", self.start_action, project
            )
        remove.assert_called_once_with(project.worktree)
        self.assertIn("old worktree is in use", str(error))
        self.assertTrue((project.worktree / "script.py").exists())
        self.assert_api_error(404, "not_found", self.open_script, project)
        result = self.wait_action(project, project.task)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], error.code)
        self.assertEqual(result["stdout_bytes"], 0)
        diagnostics = base64.b64decode(result["stderr"]["data_base64"])
        self.assertIn(b"cannot start test thread", diagnostics)
        self.assertIn(b"old worktree is in use", diagnostics)
        self.assertNotIn(b"process stderr", diagnostics)

    def test_blocked_worktree_inspection_does_not_block_other_project_snapshot(self):
        # #16: final filesystem inspection must not hold the registry lock.
        project = self.create()
        other = self.create("other")
        entered, release = threading.Event(), threading.Event()
        path_type = type(project.worktree)
        is_dir = path_type.is_dir

        def slow_is_dir(path):
            if path == project.worktree:
                entered.set()
                self.assertTrue(release.wait(2 * WAIT), "inspection stayed blocked")
            return is_dir(path)

        with patch.object(path_type, "is_dir", slow_is_dir):
            task = self.start_action(project)
            try:
                self.assertTrue(entered.wait(WAIT), "worktree inspection never started")
                snapshot = self.background(self.model.get_project, other.name)
                self.assertEqual(snapshot.result(timeout=WAIT)["status"], "idle")
            finally:
                release.set()
                self.assertEqual(self.wait_action(project, task)["status"], "succeeded")

    def test_blocked_job_termination_does_not_block_other_project_snapshot(self):
        # #16: the command must remain registered while termination is underway.
        other = self.create("other")
        running, terminating, release = (
            threading.Event(),
            threading.Event(),
            threading.Event(),
        )

        def active_process(*args, on_started, **kwargs):
            on_started()
            running.set()
            self.assertTrue(release.wait(2 * WAIT), "command stayed blocked")
            raise runner.JobCancelled(exit_code=1)

        def slow_cancel():
            terminating.set()
            self.assertTrue(release.wait(2 * WAIT), "termination stayed blocked")

        self.job.run.side_effect = active_process
        self.job.cancel.side_effect = slow_cancel
        response = self.background(self.model.start_command, COMMAND, time.monotonic())
        deletion = None
        try:
            self.assertTrue(running.wait(WAIT), "command did not start")
            command_id = self.model.list_commands()[0]["id"]
            deletion = self.background(self.model.delete_command, command_id)
            self.assertTrue(terminating.wait(WAIT), "job termination never started")
            snapshot = self.background(self.model.get_project, other.name)
            self.assertEqual(snapshot.result(timeout=WAIT)["status"], "idle")
            self.assert_api_error(
                409, "conflict", self.model.delete_command, command_id
            )
        finally:
            release.set()
            if deletion is not None:
                deletion.result(timeout=WAIT)
            response.result(timeout=WAIT)
        self.assert_api_error(404, "not_found", self.model.get_command, command_id)
