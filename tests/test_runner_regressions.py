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
from unittest.mock import patch

import runner


REPO_URL = "https://example.invalid/repo.git"
COMMIT = "a" * 40
ACTION = b'{"commit":"aaaa","interpreter":"python","script":"script.py","timeout":0}'
COMMAND = b'{"mode":"cmd","command":"exit 0","timeout":30}'
OFFSETS = {"stdout": 0, "stderr": 0}
WAIT = 5


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

    @staticmethod
    def project_body(name):
        return json.dumps({"name": name, "repo_url": REPO_URL}).encode("utf-8")

    def create(self, name="sample"):
        self.model.create(self.project_body(name))
        return self.model.projects[name.lower()]

    def assert_api_error(self, status, code, operation, *args, **kwargs):
        with self.assertRaises(runner.APIError) as caught:
            operation(*args, **kwargs)
        self.assertEqual((caught.exception.status, caught.exception.code), (status, code))
        return caught.exception

    def background(self, operation, *args):
        outcome = Future()

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

    def test_worktree_status_error_finishes_action_and_releases_slot(self):
        # #01: successful execution must still finish if final inspection fails.
        project = self.create()
        path_type = type(project.worktree)
        is_dir = path_type.is_dir

        def broken_is_dir(path):
            if path == project.worktree:
                raise PermissionError("worktree stat failed")
            return is_dir(path)

        with patch.object(path_type, "is_dir", broken_is_dir), self.assertLogs("runner", "ERROR"):
            task = self.start_action(project)
            result = self.wait_action(project, task)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "worktree_status_failed")
        self.assertIn("worktree stat failed", result["error"]["message"])
        self.assertEqual(result["output_source"], "process")
        self.assertEqual(base64.b64decode(result["stderr"]["data_base64"]), b"process stderr")
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
            self.assertEqual((result["status"], result["phase"]), ("cancelled", "reset"))
            self.assertTrue((project.worktree / "script.py").exists())
        self.job.run.assert_not_called()
        self.assertEqual(self.wait_action(project, self.start_action(project))["status"], "succeeded")

    def test_delete_times_out_with_live_reader_and_can_be_retried(self):
        # #02: a reader without a callback cannot be interrupted by DELETE.
        project = self.create()
        with (
            patch.object(runner.config, "PROJECT_DRAIN_TIMEOUT", 0.05),
            self.model.file_entry(project.name, "script.py"),
        ):
            deletion = self.background(self.model.delete_project, project.name)
            error = self.assert_api_error(409, "project_busy", deletion.result, timeout=WAIT)
            self.assertIn("retry", str(error))
            self.assertEqual(self.model.get_project(project.name)["status"], "delete_failed")
            self.assertTrue(project.worktree.exists())
        self.model.delete_project(project.name)
        self.assert_api_error(404, "not_found", self.model.get_project, project.name)
        self.assertFalse(project.worktree.exists())

    def test_delete_interrupts_reader_and_waits_for_file_release(self):
        # #02: a callback can wake the reader to close the real pinned handle.
        project = self.create()
        opened, release = threading.Event(), threading.Event()

        def read():
            with self.model.file_entry(project.name, "script.py", cancel_read=release.set):
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
        raw = self.project_body("Sample")
        worktree = self.model.projects_dir / "Sample"
        cache = self.model.caches_dir / "Sample"
        remove_tree = runner._remove_tree

        def fail_clone(repo):
            repo.cache.mkdir()
            repo.worktree.mkdir()
            (repo.worktree / "partial").touch()
            raise runner.GitFailure("clone exited 128", stderr=b"simulated Git transport error\n")

        def block_rollback(path):
            if path == worktree and (path / "partial").exists():
                raise PermissionError("worktree handle is held")
            remove_tree(path)

        with (
            patch.object(LocalRepository, "create", fail_clone),
            patch.object(runner, "_remove_tree", side_effect=block_rollback),
            self.assertLogs("runner", "WARNING") as logs,
        ):
            error = self.assert_api_error(502, "git_clone_failed", self.model.create, raw)
            self.assertEqual(str(error), "simulated Git transport error")

        self.assertIn("worktree handle is held", "\n".join(logs.output))
        self.assert_api_error(404, "not_found", self.model.get_project, "sample")
        self.assertEqual(self.model.list_projects(), [])
        self.assertEqual(self.model.pending_cleanup["sample"], (worktree,))
        self.assertTrue((worktree / "partial").is_file())
        self.assertFalse(cache.exists(), "unblocked paths should still be cleaned")

        retry = self.project_body("sample")
        with (
            patch.object(runner, "_remove_tree", side_effect=block_rollback),
            patch.object(LocalRepository, "create", autospec=True) as clone,
            self.assertLogs("runner", "WARNING"),
        ):
            self.assert_api_error(409, "project_cleanup_blocked", self.model.create, retry)
            clone.assert_not_called()
        self.assert_api_error(404, "not_found", self.model.get_project, "sample")

        # LocalRepository.create uses mkdir(exist_ok=False): it cannot reuse stale paths.
        retried = self.model.create(retry)
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
            error = self.assert_api_error(409, "project_cleanup_blocked", self.model.delete_project, project.name)
        self.assertIn("retry", str(error))
        self.assertEqual(self.model.get_project(project.name)["status"], "delete_failed")
        self.assert_api_error(404, "not_found", self.open_script, project)
        self.model.delete_project(project.name)
        self.assert_api_error(404, "not_found", self.model.get_project, project.name)
        self.assertFalse(project.worktree.exists())

    def test_cancelled_command_reports_reaped_exit_code(self):
        # #06: DELETE cancels while the caller is still waiting for the command result.
        running, release = threading.Event(), threading.Event()

        def interrupted_process(*args, on_started, **kwargs):
            on_started()
            running.set()
            self.assertTrue(release.wait(2 * WAIT), "command cancellation did not reach the job")
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
            self.assertEqual((status, result["status"], result["exit_code"]), (200, "cancelled", 23))
        finally:
            release.set()

    def test_worker_start_failure_physically_resets_previous_worktree(self):
        # #14: failure to spawn does not skip the reset for the accepted round.
        project = self.create()
        old = project.worktree / "old.txt"
        old.write_text("previous round", encoding="utf-8")
        with patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start test thread")):
            self.assert_api_error(500, "worker_unavailable", self.start_action, project)
        self.assertFalse(project.worktree.exists())
        result = self.wait_action(project, project.task)
        self.assertEqual((result["status"], result["phase"], result["output_source"]), ("failed", "reset", "runner"))
        self.assertIn(b"cannot start test thread", base64.b64decode(result["stderr"]["data_base64"]))
        self.job.run.assert_not_called()

    def test_worker_start_failure_logs_incomplete_reset_and_hides_old_files(self):
        # #14: a blocked reset logs both failures without retaining the old outputs.
        project = self.create()
        prior = self.wait_action(project, self.start_action(project))
        self.assertGreater(prior["stdout_bytes"], 0)
        with (
            patch.object(threading.Thread, "start", side_effect=RuntimeError("cannot start test thread")),
            patch.object(runner, "_remove_tree", side_effect=PermissionError("old worktree is in use")) as remove,
        ):
            error = self.assert_api_error(500, "worker_unavailable_reset_incomplete", self.start_action, project)
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
        running, terminating, release = threading.Event(), threading.Event(), threading.Event()

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
            self.assert_api_error(409, "conflict", self.model.delete_command, command_id)
        finally:
            release.set()
            if deletion is not None:
                deletion.result(timeout=WAIT)
            response.result(timeout=WAIT)
        self.assert_api_error(404, "not_found", self.model.get_command, command_id)
