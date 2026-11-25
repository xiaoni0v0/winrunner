"""Exercise the dashboard without launching Tk or listening on a port."""

import base64
import signal
import threading
import unittest
from queue import SimpleQueue
from unittest.mock import MagicMock, call, patch

import winrunner_gui as gui


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
        with (
            patch.object(gui.tk, "Frame"),
            patch.object(gui, "_label", side_effect=lambda *a, **kw: MagicMock()),
            patch.object(gui.ttk, "Style"),
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
            dashboard.connection.configure.assert_called_once_with(
                text="● 已停止", fg="#f5c386"
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
        self.assertEqual([row[0] for row in rows], ["10", "z"])
        self.assertEqual(rows[0][1][4], "0")
        self.assertEqual(rows[1][1][1], "PowerShell")

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
