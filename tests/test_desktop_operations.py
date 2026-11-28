"""Exercise the desktop forms, backend operations, and their shared HTTP state."""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from winrunner import winrunner_gui as gui
from winrunner.runner import APIError, Project, Runner, Task
from winrunner.server import StartReplay
from winrunner.winrunner_desktop import DesktopRunner, ProjectSelection, Submission
from winrunner.winrunner_gui_forms import SubmitForm


class DesktopAdapterTests(unittest.TestCase):
    def test_command_replay_uses_same_scope_and_body_as_http(self):
        runner = Mock(spec=Runner)
        replay = StartReplay()
        desktop = DesktopRunner(runner, replay)
        params = {"mode": "ps", "command": "Write-Output '测试'\n", "timeout": 0}
        runner.start_command.return_value = (202, {"id": "0"})
        from_http = replay.submit(
            "command",
            "shared",
            json.dumps(params).encode(),
            lambda: (202, {"id": "6"}),
            log_errors=False,
        )
        self.assertEqual(
            desktop.start_command(None, params, "shared"), Submission(*from_http)
        )
        runner.start_command.assert_not_called()
        local = desktop.start_command(None, params, "other")
        self.assertEqual(local.data["id"], "0")
        do_not_start = Mock()
        self.assertEqual(
            replay.submit(
                "command",
                "other",
                json.dumps(params).encode(),
                do_not_start,
                log_errors=False,
            ),
            (202, {"id": "0"}),
        )
        do_not_start.assert_not_called()
        runner.start_command.assert_called_once()

    def test_action_replay_does_not_require_a_current_project_or_revalidate(self):
        runner = Mock(spec=Runner)
        replay = StartReplay()
        desktop = DesktopRunner(runner, replay)
        params: dict[str, object] = {
            "commit": "abcd",
            "interpreter": "old_alias",
            "script": "run.py",
        }
        replay.submit(
            "action:demo",
            "old",
            json.dumps(params).encode(),
            lambda: (200, {"action_id": "old-round"}),
            log_errors=False,
        )
        result = desktop.start_action(
            ProjectSelection("Demo", "old-scope"), params, "old"
        )
        self.assertEqual(result, Submission(200, {"action_id": "old-round"}))
        self.assertFalse(runner.mock_calls)

    def test_invalid_action_does_not_occupy_or_reset_slot(self):
        runner = Mock(spec=Runner)
        replay = StartReplay()
        desktop = DesktopRunner(runner, replay)
        project = ProjectSelection("Demo", "scope")
        with self.assertRaisesRegex(APIError, "Parent directory components"):
            desktop.start_action(
                project,
                dict(commit="abc", interpreter="python", script="../script.py"),
                "new",
            )
        self.assertFalse(replay.records)
        runner.start_action.assert_not_called()

    def test_stale_action_form_does_not_claim_shared_replay_key(self):
        runner = Mock(spec=Runner)
        runner.start_action.side_effect = APIError(409, "project_changed", "stale")
        replay = StartReplay()
        desktop = DesktopRunner(runner, replay)
        values: dict[str, object] = dict(
            commit="abc", interpreter="python", script="run.py"
        )
        self.assertEqual(
            desktop.start_action(
                ProjectSelection("Demo", "old"), values, "shared"
            ).status,
            409,
        )
        self.assertFalse(replay.records)
        status, body = replay.submit(
            "action:demo",
            "shared",
            json.dumps(values).encode(),
            lambda: (202, {"action_id": "accepted"}),
            log_errors=False,
        )
        self.assertEqual((status, body), (202, {"action_id": "accepted"}))

    @unittest.skipUnless(sys.platform == "win32", "Windows Runner required")
    def test_generation_checks_and_raw_export(self):
        with tempfile.TemporaryDirectory() as base:
            runner = Runner(Path(base) / "data")
            try:
                with patch.object(runner, "_repo"):
                    runner.create(
                        "Demo", b'{"repo_url":"https://example.org/demo.git"}'
                    )
                project: Project = runner.command_project("Demo")
                selection = ProjectSelection(project.name, project.command_scope)
                previous, current = Task("run"), Task("run")
                previous.finish(exit_code=0)
                project.task = current
                output = bytes(range(256)) * 300
                try:
                    current.append("stderr", output)
                    previous_id, current_id = previous.action_id, current.action_id
                    assert previous_id is not None and current_id is not None
                    desktop = DesktopRunner(runner, StartReplay())
                    with self.assertRaisesRegex(
                        APIError, "selected Action was replaced"
                    ):
                        desktop.cancel_action(selection, previous_id)
                    self.assertFalse(current.cancelled.is_set())
                    path = Path(base) / "stderr.log"
                    self.assertEqual(
                        desktop.save_log(
                            selection,
                            None,
                            current_id,
                            "stderr",
                            path,
                            threading.Event(),
                        ),
                        len(output),
                    )
                    self.assertEqual(path.read_bytes(), output)
                finally:
                    current.finish(exit_code=0)
                runner.delete_project("Demo")
                with patch.object(runner, "_repo"):
                    runner.create(
                        "Demo", b'{"repo_url":"https://example.org/demo.git"}'
                    )
                with self.assertRaisesRegex(APIError, "selected project was replaced"):
                    desktop.delete_project(selection)
                self.assertEqual(runner.get_project("Demo")["status"], "idle")
            finally:
                runner.close()


class DesktopTkTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = gui.tk.Tk()
        except gui.tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.desktop = Mock(spec=DesktopRunner)
        self.health = gui.Response({"run_id": "test"})
        self.projects: list[dict] = []
        self.global_commands: list[dict] = []
        self.project_commands: list[dict] = []
        self.action = gui.Response(status=404)
        self.dashboard = gui.Dashboard(
            self.root,
            snapshot=self.snapshot,
            is_running=lambda: True,
            desktop=self.desktop,
        )
        self.addCleanup(self.dashboard.close)

    def snapshot(self, view: gui.View) -> gui.Snapshot:
        commands = gui.Response(
            {
                "commands": list(
                    self.project_commands if view.project else self.global_commands
                )
            }
        )
        return gui.Snapshot(
            self.health,
            gui.Response({"projects": list(self.projects)}),
            gui.Response({"commands": list(self.global_commands)}),
            selected=gui.ViewData(
                view, self.action if view.project else None, commands
            ),
        )

    def pump(self, predicate):
        until = time.monotonic() + 4
        while time.monotonic() < until:
            self.root.update()
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for desktop state")

    def test_create_form_works_off_tk_thread_then_selects_project(self):
        entered, release = threading.Event(), threading.Event()
        threads = []

        def create(name, values):
            threads.append(threading.get_ident())
            entered.set()
            release.wait(4)
            project = dict(name=name, status="idle", scope="instance", **values)
            self.projects.append(project)
            return project

        self.desktop.create.side_effect = create
        self.dashboard.controls["create"].invoke()
        form = self.dashboard._forms[("create",)]
        self.assertIsInstance(form, SubmitForm)
        form._fields["name"].set("Demo")
        form._fields["repo_url"].set("https://example.org/demo.git")
        try:
            form.submit_button.invoke()
            self.pump(entered.is_set)
            self.assertTrue(form.busy)
            self.assertNotEqual(threads, [threading.get_ident()])
        finally:
            release.set()
        self.pump(lambda: self.dashboard._view.project == "Demo")
        self.assertFalse(form.winfo_exists())
        self.desktop.create.assert_called_once_with(
            "Demo", {"repo_url": "https://example.org/demo.git"}
        )

    def test_global_submit_selects_and_delete_requires_confirmation(self):
        d = self.dashboard
        d.tabs.select(d.commands)
        command = "Write-Output 1\nWrite-Output 2\n"

        def submit(*_):
            task: dict[str, object] = dict(id="0", mode="ps", status="running")
            self.global_commands.append(task)
            return Submission(202, task)

        self.desktop.start_command.side_effect = submit
        self.desktop.delete_command.side_effect = (
            lambda *_: self.global_commands.clear()
        )
        d.controls["global_submit"].invoke()
        form = d._forms[("start_command", "")]
        form.command_text.insert("1.0", command)
        form.submit_button.invoke()
        self.pump(lambda: d._view.target == gui.Target(None, "0"))
        self.desktop.start_command.assert_called_once_with(
            None, {"mode": "ps", "command": command, "timeout": 0}, ""
        )
        with patch.object(
            gui.messagebox, "askyesno", side_effect=[False, True]
        ) as confirm:
            d.controls["global_delete"].invoke()
            self.desktop.delete_command.assert_not_called()
            d.controls["global_delete"].invoke()
            self.pump(lambda: not d.commands.tree.exists("0"))
        self.assertEqual(confirm.call_count, 2)
        self.desktop.delete_command.assert_called_once_with(None, "0")
        self.assertIsNone(d._view.target)

    def test_command_shortcut_preserves_body_and_does_not_block_next_form(self):
        d = self.dashboard
        d.tabs.select(d.commands)
        entered, release = threading.Event(), threading.Event()
        text = "Write-Output 'same text'"

        def submit(_project, params, _key):
            if params["command"] == text:
                entered.set()
                release.wait(4)
                return Submission(200, {"id": "0", "status": "succeeded"})
            return Submission(202, {"id": "1", "status": "running"})

        self.desktop.start_command.side_effect = submit
        d.controls["global_submit"].invoke()
        first = d._forms[("start_command", "")]
        first.command_text.insert("1.0", text)
        first.command_text.focus_force()
        self.root.update()
        try:
            first.command_text.event_generate("<Control-Return>")
            self.pump(entered.is_set)
            self.assertEqual(first.command_text.get("1.0", "end-1c"), text)
            first.destroy()
            d.controls["global_submit"].invoke()
            second = d._forms[("start_command", "")]
            second.command_text.insert("1.0", "Write-Output 'next'")
            second.submit_button.invoke()
            self.pump(lambda: not second.winfo_exists())
            self.assertEqual(self.desktop.start_command.call_count, 2)
        finally:
            release.set()
        self.pump(lambda: not d._operations)

    def test_same_name_project_recreation_clears_old_selection(self):
        d = self.dashboard
        self.projects[:] = [dict(name="Demo", scope="first", status="idle")]
        self.pump(lambda: d.projects.tree.exists("Demo"))
        d.projects.tree.selection_set("Demo")
        self.pump(lambda: d._project_selection() == ProjectSelection("Demo", "first"))
        d.controls["run_action"].invoke()
        old_form = d._forms[("start_action", "first")]
        self.projects[:] = [dict(name="Demo", scope="second", status="idle")]
        self.pump(lambda: d._project_selection() is None)
        self.assertTrue(d.projects.tree.exists("Demo"))
        self.assertFalse(d.projects.tree.selection())
        d.projects.tree.selection_set("Demo")
        self.pump(lambda: d._project_selection() == ProjectSelection("Demo", "second"))
        d.controls["run_action"].invoke()
        self.assertIsNot(d._forms[("start_action", "second")], old_form)

    def test_project_actions_commands_and_deletion_use_selected_scope(self):
        d = self.dashboard
        selection = ProjectSelection("Demo", "instance")
        url = "https://example.org/org/demo.git"
        self.projects.append(
            dict(name="Demo", scope="instance", status="idle", repo_url=url)
        )
        self.pump(lambda: d.projects.tree.exists("Demo"))
        d.projects.tree.selection_set("Demo")
        self.pump(lambda: d.controls["run_action"].instate(["!disabled"]))
        d.controls["copy_repo"].invoke()
        self.assertEqual(self.root.clipboard_get(), url)
        self.desktop.browse.return_value = {
            "path": "",
            "entries": [],
            "next_entry_offset": None,
        }
        d.controls["files"].invoke()
        browser = d._browsers["instance"]
        self.pump(lambda: browser._page is not None)
        self.assertEqual(self.desktop.browse.call_args.args[:3], (selection, "", 0))
        browser.close()

        def start(*_):
            state: dict[str, object] = {"status": "running", "action_id": "now"}
            self.action = gui.Response(state)
            return Submission(202, state)

        self.desktop.start_action.side_effect = start
        self.desktop.cancel_action.side_effect = lambda *_: setattr(
            self, "action", gui.Response({"status": "cancelled", "action_id": "now"})
        )
        self.desktop.start_command.return_value = Submission(
            202, {"id": "0", "status": "running"}
        )
        self.desktop.delete_project.side_effect = lambda *_: self.projects.clear()
        with patch.object(gui.messagebox, "askyesno", return_value=True):
            d.controls["run_action"].invoke()
            action = d._forms[("start_action", "instance")]
            for name, value in dict(
                commit="abcd", script="script.py", args='["a b", ""]'
            ).items():
                action._fields[name].set(value)
            action.submit_button.invoke()
            self.pump(
                lambda: d._view.target == gui.Target("Demo")
                and d.controls["cancel_action"].instate(["!disabled"])
            )
            d.controls["cancel_action"].invoke()
            self.pump(lambda: d._action_summary.get("status") == "cancelled")
            d.controls["project_submit"].invoke()
            command = d._forms[("start_command", "instance")]
            command._fields["mode"].set("CMD")
            command.command_text.insert("1.0", "echo demo")
            command.submit_button.invoke()
            self.pump(lambda: not command.winfo_exists())
            d.controls["delete_project"].invoke()
            self.pump(lambda: not d.projects.tree.exists("Demo"))
        self.desktop.start_action.assert_called_once_with(
            selection,
            {
                "commit": "abcd",
                "script": "script.py",
                "interpreter": "python",
                "args": ["a b", ""],
                "timeout": 0,
            },
            "",
        )
        self.desktop.cancel_action.assert_called_once_with(selection, "now")
        self.desktop.start_command.assert_called_once_with(
            selection, {"mode": "cmd", "command": "echo demo", "timeout": 0}, ""
        )
        self.desktop.delete_project.assert_called_once_with(selection)
