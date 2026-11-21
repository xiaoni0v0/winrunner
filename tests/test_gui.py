"""Exercise the dashboard without launching Tk or listening on a port."""

import threading
import unittest
from queue import SimpleQueue
from unittest.mock import MagicMock, call, patch

import winrunner_gui as gui


class HttpClientTests(unittest.TestCase):
    @patch("winrunner_gui.http.client.HTTPConnection")
    def test_only_gets_allowlisted_routes_on_loopback(self, factory: MagicMock) -> None:
        conn = factory.return_value
        reply = conn.getresponse.return_value.__enter__.return_value
        reply.status = 200
        reply.read.return_value = b'{"run_id":"test-run"}'
        response = gui._get(18080, "test-token", "/health")
        self.assertEqual(response.data, {"run_id": "test-run"})
        factory.assert_called_once_with("127.0.0.1", 18080, timeout=gui.REQUEST_SECONDS)
        conn.request.assert_called_once_with(
            "GET",
            "/health",
            headers={
                "Authorization": "Bearer test-token",
                "Accept": "application/json",
                "Connection": "close",
            },
        )
        with self.assertRaises(ValueError):
            gui._get(18080, "test-token", "http://example.test/health")
        self.assertEqual(factory.call_count, 1)
        conn.close.assert_called_once()

    @patch("winrunner_gui.http.client.HTTPConnection")
    def test_rejects_redirect_without_following_or_reading(
        self, factory: MagicMock
    ) -> None:
        conn = factory.return_value
        reply = conn.getresponse.return_value.__enter__.return_value
        reply.status = 302
        response = gui._get(18080, "test-token", "/health")
        self.assertIsNone(response.data)
        self.assertIn("HTTP 302", response.error)
        self.assertEqual(conn.request.call_count, 1)
        reply.read.assert_not_called()
        reply.getheader.assert_not_called()
        conn.close.assert_called_once()

    @patch("winrunner_gui.http.client.HTTPConnection")
    def test_does_not_print_transport_error_details(self, factory: MagicMock) -> None:
        factory.return_value.connect.side_effect = OSError("test-token")
        response = gui._get(18080, "test-token", "/health")
        self.assertIsNone(response.data)
        self.assertTrue(response.error)
        self.assertNotIn("test-token", response.error)
        factory.return_value.close.assert_called_once()


class PollTests(unittest.TestCase):
    @patch("winrunner_gui._get")
    def test_offline_skips_all_further_requests(self, getter: MagicMock) -> None:
        offline = gui.Response(error="offline")
        getter.return_value = offline
        results: SimpleQueue[gui.Snapshot] = SimpleQueue()
        gui._poll(18080, "test-token", threading.Event(), results)
        self.assertEqual(results.get_nowait(), gui.Snapshot(offline))
        getter.assert_called_once_with(18080, "test-token", "/health")

    @patch("winrunner_gui._get")
    def test_stop_during_request_suppresses_results(self, getter: MagicMock) -> None:
        stop = threading.Event()

        def get(_port: int, _token: str, _route: str) -> gui.Response:
            stop.set()
            return gui.Response({"run_id": "old"})

        getter.side_effect = get
        results: SimpleQueue[gui.Snapshot] = SimpleQueue()
        gui._poll(18080, "test-token", stop, results)
        self.assertTrue(results.empty())
        getter.assert_called_once()

    @patch("winrunner_gui._get")
    def test_runner_restart_discards_lists_from_mixed_generation(
        self, getter: MagicMock
    ) -> None:
        first = gui.Response({"run_id": "old"})
        latest = gui.Response({"run_id": "new"})
        getter.side_effect = [
            first,
            gui.Response({"projects": []}),
            gui.Response({"commands": [{"id": "z"}]}),
            latest,
        ]
        results: SimpleQueue[gui.Snapshot] = SimpleQueue()
        gui._poll(18080, "test-token", threading.Event(), results)
        snapshot = results.get_nowait()
        self.assertEqual(snapshot.health, latest)
        self.assertTrue(snapshot.note)
        self.assertEqual(gui._rows(snapshot.projects, "projects"), ([], snapshot.note))
        self.assertEqual(gui._rows(snapshot.commands, "commands"), ([], snapshot.note))
        self.assertEqual(
            getter.call_args_list,
            [
                call(18080, "test-token", route)
                for route in (
                    "/health",
                    "/projects",
                    "/commands",
                    "/health",
                )
            ],
        )

    @patch("winrunner_gui._get")
    def test_same_generation_keeps_healthy_section_and_partial_error(
        self, getter: MagicMock
    ) -> None:
        health = gui.Response({"run_id": "same", "uptime_seconds": 20})
        projects = gui.Response({"projects": []})
        busy = gui.Response(error="busy")
        getter.side_effect = [health, projects, busy, gui.Response({"run_id": "same"})]
        results: SimpleQueue[gui.Snapshot] = SimpleQueue()
        gui._poll(18080, "test-token", threading.Event(), results)
        self.assertEqual(results.get_nowait(), gui.Snapshot(health, projects, busy))


class IntegratedTests(unittest.TestCase):
    @staticmethod
    def dashboard(snapshot, on_close, is_running):
        root = MagicMock()
        with (
            patch.object(gui.tk, "Frame"),
            patch.object(gui, "_label", side_effect=lambda *a, **kw: MagicMock()),
            patch.object(gui.ttk, "Style"),
            patch.object(gui.ttk, "Button"),
            patch.object(gui, "Metric"),
            patch.object(gui, "StatusTable"),
        ):
            dashboard = gui.Dashboard(
                root, snapshot=snapshot, on_close=on_close, is_running=is_running
            )
        return dashboard

    @patch.object(gui, "_get")
    def test_integrated_poll_uses_worker_and_queue(self, getter: MagicMock) -> None:
        sample = gui.Snapshot(gui.Response({"run_id": "integrated"}))
        worker_threads = []

        def snapshot() -> gui.Snapshot:
            worker_threads.append(threading.current_thread())
            return sample

        on_close = MagicMock()
        dashboard = self.dashboard(snapshot, on_close, lambda: True)
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
        on_close.assert_called_once_with()
        dashboard.root.destroy.assert_called_once_with()

    def test_backend_exit_closes_without_notification_during_a_poll(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        alive = threading.Event()
        alive.set()

        def snapshot() -> gui.Snapshot:
            entered.set()
            release.wait(timeout=2)
            return gui.Snapshot(gui.Response({"run_id": "old"}))

        on_close = MagicMock()
        dashboard = self.dashboard(snapshot, on_close, alive.is_set)
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
            on_close.assert_not_called()
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
        snapshot, on_close, is_running = MagicMock(), MagicMock(), MagicMock()
        gui.run_integrated(snapshot, on_close, is_running)
        dashboard.assert_called_once_with(
            tk_factory.return_value,
            snapshot=snapshot,
            on_close=on_close,
            is_running=is_running,
        )
        tk_factory.return_value.mainloop.assert_called_once_with()


class DisplayTests(unittest.TestCase):
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
