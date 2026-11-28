"""Exercise dashboard behavior and check layout when a Tk display is available."""

import base64
import signal
import threading
import unittest
from datetime import datetime
from queue import SimpleQueue
from unittest.mock import MagicMock, call, patch

from winrunner import winrunner_gui as gui


class ViewTests(unittest.TestCase):
    def test_project_detail_is_lazy_and_uses_both_offsets(self) -> None:
        list_commands = MagicMock(return_value=gui.Response({}))
        get_task = MagicMock(return_value=gui.Response({}))
        target = gui.Target("Demo", "0")
        view = gui.View("Demo", target, 32768, 65536)
        state = gui.read_view(view, list_commands, get_task)
        self.assertEqual(state.view, view)
        list_commands.assert_called_once_with("Demo")
        self.assertEqual(
            get_task.call_args_list,
            [
                call(gui.Target("Demo"), None),
                call(target, {"stdout": 32765, "stderr": 65533}),
            ],
        )
        list_commands.reset_mock()
        get_task.reset_mock()
        gui.read_view(gui.View(), list_commands, get_task)
        list_commands.assert_not_called()
        get_task.assert_not_called()


class IntegratedTests(unittest.TestCase):
    @staticmethod
    def dashboard(snapshot, is_running):
        root = MagicMock()
        root.tk.call.return_value = 1.33
        root.winfo_screenwidth.return_value = 1920
        root.winfo_screenheight.return_value = 1080
        with (
            patch.object(gui.tk, "Frame"),
            patch.object(gui.tk, "Label"),
            patch.object(gui, "_label", side_effect=lambda *a, **kw: MagicMock()),
            patch.object(gui, "_apply_theme"),
            patch.object(gui, "FitLabel"),
            patch.object(gui.ttk, "Button"),
            patch.object(gui.ttk, "Notebook"),
            patch.object(gui, "TaskDetail"),
            patch.object(gui, "Metric"),
            patch.object(gui, "StatusTable"),
        ):
            dashboard = gui.Dashboard(root, snapshot=snapshot, is_running=is_running)
        return dashboard

    @patch("http.client.HTTPConnection")
    def test_integrated_poll_uses_worker_and_queue(self, getter: MagicMock) -> None:
        sample = gui.Snapshot(gui.Response({"run_id": "integrated"}))
        worker_threads = []

        def snapshot(_view: gui.View) -> gui.Snapshot:
            worker_threads.append(threading.current_thread())
            return sample

        dashboard = self.dashboard(snapshot, lambda: True)
        dashboard._show = MagicMock()
        dashboard.refresh()
        queued = dashboard._results.get(timeout=2)
        self.assertIs(queued, sample)
        self.assertEqual(len(worker_threads), 1)
        self.assertIsNot(worker_threads[0], threading.main_thread())
        dashboard._show.assert_not_called()
        dashboard._results.put(queued)
        dashboard._receive()
        dashboard._show.assert_called_once_with(sample)
        dashboard.root.after.assert_called_with(
            gui.INTEGRATED_REFRESH_MS, dashboard.refresh
        )
        getter.assert_not_called()

        dashboard.close()
        dashboard.close()
        dashboard.root.destroy.assert_called_once_with()

    def test_window_close_hides_and_reopens_without_stopping_the_backend(self) -> None:
        alive = threading.Event()
        alive.set()
        dashboard = self.dashboard(MagicMock(), alive.is_set)
        dashboard._window_close()
        dashboard._window_close()
        dashboard.root.withdraw.assert_called_once_with()
        dashboard.root.after.assert_called_once_with(gui.REOPEN_MS, dashboard._reopen)
        dashboard.root.destroy.assert_not_called()
        self.assertFalse(dashboard._stop.is_set())
        dashboard._reopen()
        dashboard.root.deiconify.assert_called_once_with()
        dashboard._window_close()
        alive.clear()
        dashboard._reopen()
        dashboard.root.deiconify.assert_called_once_with()
        dashboard.root.after.assert_called_with(gui.BACKEND_EXIT_MS, dashboard.close)
        dashboard.close()
        dashboard.root.destroy.assert_called_once_with()

    def test_replaced_action_resets_open_log_offsets(self) -> None:
        dashboard = self.dashboard(MagicMock(), lambda: True)
        dashboard._run_id = "current"
        view = gui.View("Demo", gui.Target("Demo"), gui.LOG_PAGE, 0, 3)
        dashboard._view = view
        dashboard._action_id = "old-action"
        detail = gui.Response({"action_id": "new-action", "status": "running"})
        dashboard._show(
            gui.Snapshot(
                gui.Response({"run_id": "current"}),
                selected=gui.ViewData(view, action=detail, detail=detail),
            )
        )
        expected = gui.View("Demo", gui.Target("Demo"), revision=4)
        self.assertEqual(dashboard._view, expected)
        dashboard.detail.show.assert_called_once_with(expected, None)

    def test_temporary_disconnect_preserves_selection_and_restart_clears_it(
        self,
    ) -> None:
        dashboard = self.dashboard(MagicMock(), lambda: True)
        dashboard._run_id = "old"
        view = gui.View(None, gui.Target(None, "0"), gui.LOG_PAGE, 0, 2)
        dashboard._view = view
        dashboard._show(gui.Snapshot(gui.Response(error="busy")))
        self.assertEqual(dashboard._run_id, "old")
        self.assertIs(dashboard._view, view)
        dashboard.commands.show_rows.assert_called_with(
            [], "等待 Runner 状态", reset=False
        )
        dashboard._show(gui.Snapshot(gui.Response({"run_id": "new"})))
        self.assertIsNone(dashboard._view.target)
        self.assertTrue(view.superseded.is_set())

    def test_repeated_next_click_does_not_skip_a_page(self) -> None:
        dashboard = self.dashboard(MagicMock(), lambda: True)
        dashboard._view = gui.View(None, gui.Target(None, "0"), 0, gui.LOG_PAGE, 2)
        dashboard._detail_revision = 2
        dashboard._page("stdout", 1)
        dashboard._page("stdout", 1)
        self.assertEqual(
            dashboard._view.offsets(), {"stdout": gui.LOG_PAGE, "stderr": gui.LOG_PAGE}
        )

    def test_failed_refresh_thread_start_is_retried_without_reopening_tk(self) -> None:
        dashboard = self.dashboard(MagicMock(), lambda: True)
        with (
            patch.object(
                gui.threading,
                "Thread",
                side_effect=RuntimeError("can't start new thread"),
            ),
            self.assertLogs(gui.LOG, level="ERROR") as logs,
        ):
            dashboard.refresh()
        # The failure is reported, the window stays open, and no snapshot is
        # awaited: retrying must not depend on a worker that never started.
        self.assertIn("refresh worker", "\n".join(logs.output))
        self.assertEqual(
            dashboard.notice.set_text.call_args.args, ("状态刷新未启动，正在重试…",)
        )
        self.assertFalse(dashboard._busy)
        dashboard.root.withdraw.assert_not_called()
        dashboard.root.destroy.assert_not_called()
        dashboard.root.deiconify.assert_not_called()
        self.assertTrue(dashboard._results.empty())
        # A single retry timer replaced any normal receive timer.
        dashboard.root.after.assert_called_once_with(
            gui.REFRESH_RETRY_MS, dashboard.refresh
        )

    def test_refresh_retry_resumes_normal_polling(self) -> None:
        sample = gui.Snapshot(gui.Response({"run_id": "integrated"}))
        dashboard = self.dashboard(lambda _view: sample, lambda: True)
        dashboard._show = MagicMock()
        with patch.object(
            gui.threading, "Thread", side_effect=RuntimeError("can't start new thread")
        ):
            dashboard.refresh()
        self.assertFalse(dashboard._busy)
        # The retry starts a real worker and polls normally again.
        dashboard.refresh()
        self.assertTrue(dashboard._busy)
        self.assertIs(dashboard._results.get(timeout=2), sample)

    def test_backend_exit_closes_during_a_poll(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        alive = threading.Event()
        alive.set()

        def snapshot(_view: gui.View) -> gui.Snapshot:
            entered.set()
            release.wait(timeout=2)
            return gui.Snapshot(gui.Response({"run_id": "old"}))

        dashboard = self.dashboard(snapshot, alive.is_set)
        try:
            dashboard.refresh()
            self.assertTrue(entered.wait(timeout=2))
            alive.clear()
            dashboard._receive()
            dashboard.connection.configure.assert_called_with(
                text="● 已停止", fg=gui.AMBER
            )
            dashboard.root.after.assert_called_with(
                gui.BACKEND_EXIT_MS, dashboard.close
            )
            dashboard.close()
            dashboard.root.destroy.assert_called_once_with()
        finally:
            release.set()

    def test_snapshot_error_is_retried_and_details_are_not_displayed(self) -> None:
        snapshot = MagicMock(side_effect=ValueError("private details"))
        stop = threading.Event()
        results: SimpleQueue[gui.Snapshot] = SimpleQueue()
        gui._poll_snapshot(snapshot, stop, results)
        error = results.get_nowait().health.error
        self.assertTrue(error)
        self.assertNotIn("private details", error)
        sample = gui.Snapshot(gui.Response({}))
        snapshot.side_effect = None
        snapshot.return_value = sample
        gui._poll_snapshot(snapshot, stop, results)
        self.assertIs(results.get_nowait(), sample)

    @patch.object(gui, "Dashboard")
    @patch.object(gui.tk, "Tk")
    def test_entry_point_creates_the_integrated_dashboard(
        self, tk_factory, dashboard
    ) -> None:
        snapshot, is_running = MagicMock(), MagicMock()
        previous = signal.getsignal(signal.SIGINT)

        def mainloop() -> None:
            check = dashboard.call_args.kwargs["is_running"]
            self.assertTrue(check())
            signal.raise_signal(signal.SIGINT)
            self.assertFalse(check())

        tk_factory.return_value.mainloop.side_effect = mainloop
        gui.run_integrated(snapshot, is_running)
        self.assertEqual(dashboard.call_args.args, (tk_factory.return_value,))
        self.assertIs(dashboard.call_args.kwargs["snapshot"], snapshot)
        self.assertIs(signal.getsignal(signal.SIGINT), previous)
        tk_factory.return_value.mainloop.assert_called_once_with()


class TkLayoutTests(unittest.TestCase):
    def test_command_table_heading_divider_is_a_native_resize_handle(self) -> None:
        try:
            root = gui.tk.Tk()
        except gui.tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        root.geometry("620x220")
        gui._apply_theme(root)
        table = gui.StatusTable(root, "命令", gui.COMMAND_COLUMNS)
        table.pack(fill="both", expand=True)
        root.update()

        tree = table.tree
        original = tree.column("id", "width")
        x, y = original - 1, 10
        self.assertEqual(tree.identify_region(x, y), "separator")
        tree.event_generate("<Motion>", x=x, y=y)
        self.assertTrue(tree.cget("cursor"))
        tree.event_generate("<ButtonPress-1>", x=x, y=y)
        tree.event_generate("<B1-Motion>", x=x + 36, y=y)
        tree.event_generate("<ButtonRelease-1>", x=x + 36, y=y)
        root.update()

        resized = tree.column("id", "width")
        self.assertGreater(resized, original)
        self.assertEqual(tree.identify_region(resized - 1, y), "separator")
        self.assertEqual(tree.heading("id", "text"), "ID ▼")

    def test_command_table_header_click_sorts_and_refresh_keeps_selection(self) -> None:
        try:
            root = gui.tk.Tk()
        except gui.tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        table = gui.StatusTable(root, "命令", gui.COMMAND_COLUMNS)
        table.pack(fill="both", expand=True)

        def show(z_exit: int) -> None:
            response = gui.Response(
                {
                    "commands": [
                        {"id": "10", "exit_code": 2},
                        {"id": "z", "exit_code": z_exit},
                    ]
                }
            )
            table.show_rows(*gui._rows(response, "commands"))
            root.update()

        def click_exit(event_time: int) -> None:
            x = (
                sum(
                    table.tree.column(key, "width") for key in table.tree["columns"][:4]
                )
                + 10
            )
            self.assertEqual(table.tree.identify_region(x, 5), "heading")
            table.tree.event_generate("<ButtonPress-1>", x=x, y=5, time=event_time)
            table.tree.event_generate(
                "<ButtonRelease-1>", x=x, y=5, time=event_time + 1
            )
            root.update()

        show(-1)
        self.assertEqual(table.tree.get_children(), ("10", "z"))
        self.assertEqual(table.tree.heading("id", "text"), "ID ▼")
        table.tree.selection_set("10")
        click_exit(1000)
        self.assertEqual(table.tree.get_children(), ("z", "10"))
        self.assertEqual(table.tree.heading("exit", "text"), "退出码 ▲")
        click_exit(2000)
        self.assertEqual(table.tree.get_children(), ("10", "z"))
        show(12)
        self.assertEqual(table.tree.get_children(), ("z", "10"))
        self.assertEqual(table.tree.selection(), ("10",))
        self.assertEqual(table.tree.heading("exit", "text"), "退出码 ▼")
        self.assertEqual(table.tree.heading("id", "text"), "ID")

    def test_task_details_show_both_local_times(self) -> None:
        try:
            root = gui.tk.Tk()
        except gui.tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(root.destroy)
        detail = gui.TaskDetail(root, lambda: None, lambda *_: None)
        start = 1_767_323_045_000
        end = start + 1000
        for target in (
            gui.Target("Demo"),
            gui.Target("Demo", "1"),
            gui.Target(None, "1"),
        ):
            with self.subTest(target=target):
                detail.open(target)
                detail.show(
                    gui.View(target.project, target),
                    gui.Response({"started_at": start, "finished_at": end}),
                )
                self.assertEqual(
                    detail.times._text,
                    f"开始：{gui._local_time(start)}  ·  结束：{gui._local_time(end)}",
                )

    def test_switching_command_keeps_detail_tab_until_collapsed(self) -> None:
        try:
            root = gui.tk.Tk()
        except gui.tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        dashboard = gui.Dashboard(
            root,
            snapshot=lambda view: gui.Snapshot(gui.Response()),
            is_running=lambda: True,
        )
        try:
            if dashboard._after_id is not None:
                root.after_cancel(dashboard._after_id)
                dashboard._after_id = None
            with patch.object(dashboard, "_schedule"):
                dashboard.tabs.select(dashboard.commands)
                tree = dashboard.commands.tree
                for command_id in ("0", "1"):
                    tree.insert("", "end", iid=command_id)
                tree.selection_set("0")
                root.update()
                detail = dashboard.detail
                self.assertEqual(detail.tabs.select(), str(detail.input))
                for tab in (
                    detail.outputs["stdout"].master,
                    detail.outputs["stderr"].master,
                    detail.diagnostics,
                ):
                    with self.subTest(tab=tab):
                        detail.tabs.select(tab)
                        current = tree.selection()[0]
                        next_id = "1" if current == "0" else "0"
                        tree.selection_set(next_id)
                        root.update()
                        self.assertEqual(
                            dashboard._view.target, gui.Target(None, next_id)
                        )
                        self.assertEqual(detail.tabs.select(), str(tab))
                dashboard._collapse()
                root.update()
                self.assertFalse(detail.winfo_ismapped())
                tree.selection_set("0")
                root.update()
                self.assertTrue(detail.winfo_ismapped())
                self.assertEqual(detail.tabs.select(), str(detail.input))
        finally:
            dashboard.close()

    def test_project_commands_show_multiple_full_rows_with_detail_open(self) -> None:
        for scale in (1.33, 2.0):
            with self.subTest(scale=scale):
                try:
                    root = gui.tk.Tk()
                except gui.tk.TclError as exc:
                    self.skipTest(f"Tk display unavailable: {exc}")
                root.tk.call("tk", "scaling", scale)
                dashboard = gui.Dashboard(
                    root,
                    snapshot=MagicMock(),
                    is_running=lambda: True,
                    desktop=MagicMock(),
                )
                try:
                    if dashboard._after_id is not None:
                        root.after_cancel(dashboard._after_id)
                        dashboard._after_id = None
                    with patch.object(dashboard, "_schedule"):
                        root.update()
                        initial_size = (root.winfo_width(), root.winfo_height())
                        dashboard.projects.tree.insert("", "end", iid="Demo")
                        dashboard.projects.tree.selection_set("Demo")
                        root.update()
                        commands = gui.Response(
                            {
                                "commands": [
                                    {"id": str(i), "mode": "ps", "status": "succeeded"}
                                    for i in range(9)
                                ]
                            }
                        )
                        dashboard._update_project(
                            gui.ViewData(dashboard._view, commands=commands)
                        )
                        tree = dashboard.project_commands.tree
                        tree.selection_set("8")
                        root.update()
                        self.assertEqual(
                            dashboard._view.target, gui.Target("Demo", "8")
                        )
                        self.assertEqual(
                            (root.winfo_width(), root.winfo_height()), initial_size
                        )
                        width, height = root.minsize()
                        for minimum in (False, True):
                            with self.subTest(minimum=minimum):
                                if minimum:
                                    root.geometry(f"{width}x{height}")
                                dashboard.detail.tabs.select(
                                    dashboard.detail.outputs["stdout"].master
                                )
                                root.update()
                                bottom = min(
                                    tree.winfo_rooty() + tree.winfo_height(),
                                    dashboard.project_commands.winfo_rooty()
                                    + dashboard.project_commands.winfo_height(),
                                    dashboard.tabs.winfo_rooty()
                                    + dashboard.tabs.winfo_height(),
                                )
                                boxes = (tree.bbox(key) for key in tree.get_children())
                                self.assertGreaterEqual(
                                    sum(
                                        bool(
                                            box
                                            and tree.winfo_rooty() + box[1] + box[3]
                                            <= bottom
                                        )
                                        for box in boxes
                                    ),
                                    3,
                                )
                                self.assertGreater(
                                    dashboard.detail.outputs[
                                        "stdout"
                                    ].text.winfo_height(),
                                    25,
                                )
                finally:
                    dashboard.close()

    def test_long_content_at_minimum_size_and_larger_scaling(self) -> None:
        for scale in (1.33, 2.0):
            with self.subTest(scale=scale):
                try:
                    root = gui.tk.Tk()
                except gui.tk.TclError as exc:
                    self.skipTest(f"Tk display unavailable: {exc}")
                root.tk.call("tk", "scaling", scale)
                dashboard = gui.Dashboard(
                    root,
                    snapshot=lambda view: gui.Snapshot(gui.Response()),
                    is_running=lambda: True,
                )
                scheduler = patch.object(dashboard, "_schedule")
                scheduler.start()
                try:
                    if dashboard._after_id is not None:
                        root.after_cancel(dashboard._after_id)
                        dashboard._after_id = None
                    width, height = root.minsize()
                    root.geometry(f"{width}x{height}+0+0")
                    name = "W" * 64
                    url = "https://github.example/owner/" + "long-segment/" * 80
                    command_id = "1"
                    command = {
                        "id": command_id,
                        "mode": "ps",
                        "status": "failed",
                        "phase": "fetch",
                        "command": "Write-Output " + "中" * 512,
                        "stdout_bytes": 2**62,
                        "stderr_bytes": 2**62,
                        "stdout": {
                            "data_base64": base64.b64encode(b"x" * 512).decode()
                        },
                    }
                    health = gui.Response({"run_id": "r" * 32})
                    projects = gui.Response(
                        {
                            "projects": [
                                {"name": name, "repo_url": url, "status": "idle"}
                            ]
                        }
                    )
                    commands = gui.Response({"commands": [command]})
                    dashboard._show(gui.Snapshot(health, projects, commands))
                    dashboard.projects.tree.selection_set(name)
                    root.update()
                    self.assertEqual(dashboard._view.project, name)
                    action = gui.Response(error="错误信息" * 200, status=409)
                    dashboard._show(
                        gui.Snapshot(
                            health,
                            projects,
                            commands,
                            selected=gui.ViewData(dashboard._view, action, commands),
                        )
                    )
                    dashboard.project_commands.tree.selection_set(command_id)
                    root.update()
                    self.assertEqual(
                        dashboard._view.target, gui.Target(name, command_id)
                    )
                    dashboard._show(
                        gui.Snapshot(
                            health,
                            projects,
                            commands,
                            "很长的刷新状态" * 100,
                            gui.ViewData(
                                dashboard._view, action, commands, gui.Response(command)
                            ),
                        )
                    )
                    output = dashboard.detail.outputs["stdout"]
                    dashboard.detail.tabs.select(output.master)
                    root.update()
                    for label in (
                        dashboard.repo,
                        dashboard.detail.title,
                        dashboard.action_text,
                        dashboard.notice,
                    ):
                        text = str(label.cget("text"))
                        self.assertTrue(text.endswith("…"), text)
                        self.assertLessEqual(
                            label._font.measure(text), label.winfo_width()
                        )
                    for button in dashboard.detail.buttons["stdout"]:
                        self.assertTrue(button.winfo_ismapped())
                    self.assertGreater(output.text.winfo_height(), 25)
                    self.assertLess(output.text.xview()[1], 1)
                    self.assertEqual(output.value, "x" * 512)
                    tree = dashboard.project_commands.tree
                    self.assertTrue(tree.bbox(command_id))
                    if tree.xview()[1] < 1:
                        self.assertTrue(
                            dashboard.project_commands.horizontal.winfo_ismapped()
                        )
                        tree.xview_moveto(1.0)
                        root.update()
                        self.assertEqual(tree.xview()[1], 1)
                    for widget in (dashboard.detail, dashboard.projects, tree, output):
                        self.assertGreaterEqual(
                            widget.winfo_rootx(), root.winfo_rootx()
                        )
                        self.assertLessEqual(
                            widget.winfo_rootx() + widget.winfo_width(),
                            root.winfo_rootx() + root.winfo_width(),
                        )
                        self.assertLessEqual(
                            widget.winfo_rooty() + widget.winfo_height(),
                            root.winfo_rooty() + root.winfo_height(),
                        )
                    root.geometry(f"{width + 250}x{height}")
                    root.update()
                    self.assertIn(
                        "https://github.example/", str(dashboard.repo.cget("text"))
                    )
                finally:
                    scheduler.stop()
                    dashboard.close()


class DisplayTests(unittest.TestCase):
    def test_log_pages_preserve_utf8_at_live_and_terminal_boundaries(self) -> None:
        for stream in gui.STREAMS:
            for char in ("中", "🙂"):
                prefix = b"x" * (gui.LOG_PAGE - 1)
                complete = prefix + char.encode()
                with self.subTest(stream=stream, char=char):

                    def pages(raw, status):
                        return tuple(
                            gui._decode_log(
                                {
                                    "status": status,
                                    stream: {
                                        "data_base64": base64.b64encode(chunk).decode()
                                    },
                                },
                                stream,
                                offset,
                            )
                            for offset, chunk in (
                                (0, raw),
                                (gui.LOG_PAGE, raw[gui.LOG_PAGE - gui.LOG_OVERLAP :]),
                            )
                        )

                    self.assertEqual(
                        pages(complete[:-1], "running"), (prefix.decode(), "")
                    )
                    self.assertEqual(
                        pages(complete + b"y", "succeeded"),
                        (prefix.decode() + char, "y"),
                    )

    def test_short_command_ids_stay_strings(self) -> None:
        commands = gui.Response(
            {
                "commands": [
                    {"id": "z", "status": "running", "mode": "ps", "phase": "run"},
                    {"id": "10", "status": "succeeded", "exit_code": 0},
                ]
            }
        )
        rows, error = gui._rows(commands, "commands")
        self.assertEqual(error, "")
        ordered = gui._sort_commands(rows, "id", descending=True)
        self.assertEqual([row.key for row in ordered], ["10", "z"])
        self.assertEqual(ordered[0].values[4], "0")
        self.assertEqual(ordered[1].values[1], "PowerShell")

    def test_command_columns_sort_by_raw_values_with_newest_id_breaking_ties(
        self,
    ) -> None:
        stamp = 1_767_323_045_000
        fields = (
            ("mode", "mode", "cmd", "ps", None),
            ("status", "status", "preparing", "running", "cancelled"),
            ("phase", "phase", "validate", "launch", "run"),
            ("exit", "exit_code", -2, 2, 12),
            ("stdout", "stdout_bytes", 1100, 1101, 2**54),
            ("stderr", "stderr_bytes", 2**54, 2**54 + 1, 2**54 + 2),
            ("started_at", "started_at", stamp, stamp + 1, None),
            ("finished_at", "finished_at", stamp, stamp + 1, None),
        )
        for column, field, low, middle, high in fields:
            with self.subTest(column=column):
                rows, error = gui._rows(
                    gui.Response(
                        {
                            "commands": [
                                {"id": "10", field: middle},
                                {"id": "z", field: middle},
                                {"id": "y", field: high},
                                {"id": "11", field: low},
                            ]
                        }
                    ),
                    "commands",
                )
                self.assertFalse(error)
                self.assertEqual(
                    [row.key for row in gui._sort_commands(rows, column, False)],
                    ["11", "10", "z", "y"],
                )
                self.assertEqual(
                    [row.key for row in gui._sort_commands(rows, column, True)],
                    ["y", "10", "z", "11"],
                )

    def test_command_rows_display_local_times_and_missing_finish(self) -> None:
        stamp = 1_767_323_045_678
        expected = f"{datetime.fromtimestamp(stamp // 1000):%Y-%m-%d %H:%M:%S}"
        commands = gui.Response(
            {"commands": [{"id": "1", "started_at": stamp, "finished_at": None}]}
        )
        rows, error = gui._rows(commands, "commands")
        self.assertFalse(error)
        self.assertEqual(rows[0].values[-2:], (expected, gui.DASH))

    def test_cpu_usage_displays_latest_and_both_averages(self) -> None:
        self.assertEqual(
            gui._cpu_usage(
                {
                    "usage_percent": 28.5,
                    "usage_5s_percent": 15,
                    "usage_60s_percent": 0,
                }
            ),
            ("28.5%", "5s 15.0%  ·  60s 0.0%", 28.5),
        )
        self.assertEqual(
            gui._cpu_usage({"usage_percent": 101, "usage_5s_percent": float("nan")}),
            (gui.DASH, f"5s {gui.DASH}  ·  60s {gui.DASH}", 0),
        )

    def test_metrics_handle_missing_values(self) -> None:
        self.assertEqual(gui._uptime(90061.5), "1 天 01:01:01")
        self.assertEqual(gui._uptime(float("inf")), gui.DASH)
        self.assertEqual(
            gui._capacity({"total_bytes": 4096, "available_bytes": 1024}),
            ("75%", "已用 3.0 KiB / 4.0 KiB", 75),
        )


if __name__ == "__main__":
    unittest.main()
