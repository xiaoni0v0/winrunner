"""Focused file-browser behavior tests with a display, not a live backend."""

import threading
import time
import tkinter as tk
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import winrunner_gui as gui
from runner import APIError
from winrunner_gui_files import Browse, FileBrowser, Save


def listing(path: str, *entries: tuple[str, str, int | None], nxt=None) -> dict:
    return {
        "path": path,
        "entries": [
            {"name": name, "type": kind, "size_bytes": size}
            for name, kind, size in entries
        ],
        "next_entry_offset": nxt,
    }


def set_entry(entry: tk.Entry, value: str) -> None:
    entry.delete(0, tk.END)
    entry.insert(0, value)


class FileBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.addCleanup(self.destroy_root)
        gui._apply_theme(self.root)
        self._tk_errors: list[BaseException] = []
        self.root.report_callback_exception = (  # type: ignore[method-assign]
            lambda _kind, exc, _traceback: self._tk_errors.append(exc)
        )

    def destroy_root(self) -> None:
        if self.root.children or self.root.tk.call("info", "commands", "."):
            self.root.destroy()

    def wait_for(self, predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            self.assertEqual(self._tk_errors, [])
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for the file browser")

    def browser(self, browse: Browse, save: Save = lambda *_: 0) -> FileBrowser:
        browser = FileBrowser(self.root, "Demo", browse, save)
        self.addCleanup(browser.close)
        return browser

    def ready(self, browser: FileBrowser) -> None:
        self.wait_for(lambda: not browser._busy())

    def select(self, browser: FileBrowser, name: str) -> None:
        row = next(
            row
            for row in browser._tree.get_children()
            if browser._tree.set(row, "name") == name
        )
        browser._tree.selection_set(row)
        self.root.update()

    def test_modeless_directory_navigation_and_cursor_history(self) -> None:
        calls: list[tuple[str, str, int, int]] = []
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def browse(project, path, offset, cancel):
            calls.append((project, path, offset, threading.get_ident()))
            if path == "":
                entered.set()
                release.wait(timeout=3)
                return listing("", ("sub", "directory", None), ("link", "other", None))
            if offset == 0:
                return listing("sub", ("readme.txt", "file", 1024), nxt=13)
            if offset == 13:
                return listing("sub", ("source.py", "file", 3), nxt=24)
            return listing("sub", ("tail.csv", "file", 17))

        browser = self.browser(browse)
        owner_callback = threading.Event()
        self.root.after_idle(owner_callback.set)
        self.wait_for(lambda: entered.is_set() and owner_callback.is_set())
        self.assertEqual(str(browser.transient()), str(self.root))
        self.assertIsNone(browser.grab_current())
        self.assertTrue(browser._refresh_button.instate(["disabled"]))
        self.assertTrue(browser._download_button.instate(["disabled"]))
        self.assertIn("正在读取", browser._status.cget("text"))
        release.set()
        self.ready(browser)
        self.assertTrue(browser._up_button.instate(["disabled"]))
        self.select(browser, "link")
        self.assertTrue(browser._open_button.instate(["disabled"]))
        self.select(browser, "sub")
        browser._open_button.invoke()
        self.ready(browser)
        self.assertEqual(browser._path_entry.get(), "sub")
        self.assertTrue(browser._previous_button.instate(["disabled"]))
        browser._next_button.invoke()
        browser._next_button.invoke()  # Disabled while the cursor's page is loading.
        self.ready(browser)
        self.assertIn("第 2 页", browser._page_label.cget("text"))
        browser._next_button.invoke()
        self.ready(browser)
        self.assertTrue(browser._next_button.instate(["disabled"]))
        browser._previous_button.invoke()
        self.ready(browser)
        self.assertIn("第 2 页", browser._page_label.cget("text"))
        browser._refresh_button.invoke()
        self.ready(browser)
        self.select(browser, "readme.txt")
        browser._up_button.invoke()
        self.ready(browser)
        self.select(browser, "sub")
        browser._open_button.invoke()
        self.ready(browser)
        browser._root_button.invoke()
        self.ready(browser)
        self.assertEqual(browser._path_entry.get(), "")
        self.assertEqual(
            [(project, path, offset) for project, path, offset, _ in calls],
            [
                ("Demo", path, offset)
                for path, offset in (
                    ("", 0),
                    ("sub", 0),
                    ("sub", 13),
                    ("sub", 24),
                    ("sub", 13),
                    ("sub", 0),
                    ("", 0),
                    ("sub", 0),
                    ("", 0),
                )
            ],
        )
        self.assertTrue(all(worker != threading.get_ident() for *_, worker in calls))

    def test_file_selection_stays_in_directory_and_allows_download(self) -> None:
        saves: list[tuple[str, str, Path, str | None, int]] = []
        paths: list[str] = []

        def browse(project, path, offset, cancel):
            paths.append(path)
            if path == "":
                return listing("", ("sub", "directory", None))
            if path == "sub":
                return listing("sub", ("log.txt", "file", 536), ("keep.txt", "file", 1))
            if path == r"sub\log.txt":
                return {"path": "sub/log.txt", "size_bytes": 536}
            raise APIError(404, "not_found", "No such project file")

        def save(project, path, destination, range_value, cancel):
            saves.append(
                (project, path, destination, range_value, threading.get_ident())
            )
            if range_value == "bytes=999-":
                raise APIError(
                    416, "range_not_satisfiable", "Expected one satisfiable byte range"
                )
            return 16 if range_value else 536

        browser = self.browser(browse, save)
        self.ready(browser)
        self.select(browser, "sub")
        browser._open_button.invoke()
        self.ready(browser)
        self.select(browser, "log.txt")
        tree = browser._tree
        row = tree.selection()[0]
        self.assertEqual(tree.set(row, "size"), "536 B")
        self.assertTrue(browser._open_button.instate(["disabled"]))
        browser._open_button.invoke()
        tree.focus_force()
        tree.event_generate("<Return>")
        box = tree.bbox(row)
        assert box
        x, y, _, height = box
        for _ in range(2):
            tree.event_generate("<ButtonPress-1>", x=x + 5, y=y + height // 2)
            tree.event_generate("<ButtonRelease-1>", x=x + 5, y=y + height // 2)
        self.root.update()
        self.assertEqual(paths, ["", "sub"])
        self.assertEqual(tree.selection(), (row,))
        self.assertEqual(browser._location.cget("text"), "当前路径：sub")
        self.assertTrue(browser._download_button.instate(["!disabled"]))

        set_entry(browser._path_entry, r"sub\log.txt")
        browser._go_button.invoke()
        self.ready(browser)
        self.assertIn("not_a_directory", browser._status.cget("text"))
        self.assertEqual(browser._location.cget("text"), "当前路径：sub")
        self.assertEqual(tree.selection(), (row,))
        self.assertEqual(len(tree.get_children()), 2)

        dest = Path("chosen log.txt")
        with patch(
            "winrunner_gui_files.filedialog.asksaveasfilename", return_value=str(dest)
        ) as picker:
            set_entry(browser._range_entry, "not a range")
            browser._range_button.invoke()
            picker.assert_not_called()
            self.assertIn("bytes=0-1023", browser._status.cget("text"))
            browser._download_button.invoke()  # Full mode ignores the range field.
            self.ready(browser)
            self.assertEqual(picker.call_args.kwargs["initialfile"], "log.txt")
            self.assertIs(picker.call_args.kwargs["parent"], browser)
            self.assertIn("536 字节", browser._status.cget("text"))
            set_entry(browser._range_entry, "bytes=-16")
            browser._range_button.invoke()
            self.ready(browser)
            self.assertEqual(picker.call_args.kwargs["initialfile"], "log.txt.part")
            self.assertIn("16 字节", browser._status.cget("text"))
            set_entry(browser._range_entry, "bytes=999-")
            browser._range_button.invoke()
            self.ready(browser)
            self.assertIn("416 range_not_satisfiable", browser._status.cget("text"))
            self.assertIn(
                "Expected one satisfiable byte range", browser._status.cget("text")
            )
            picker.return_value = ""
            browser._download_button.invoke()

        self.assertEqual(
            [
                (project, path, destination, value)
                for project, path, destination, value, _ in saves
            ],
            [
                ("Demo", "sub/log.txt", dest, value)
                for value in (None, "bytes=-16", "bytes=999-")
            ],
        )
        self.assertTrue(all(worker != threading.get_ident() for *_, worker in saves))
        set_entry(browser._path_entry, "missing.txt")
        browser._go_button.invoke()
        self.ready(browser)
        self.assertIn("404 not_found", browser._status.cget("text"))
        self.assertIn("No such project file", browser._status.cget("text"))
        self.assertEqual(browser._location.cget("text"), "当前路径：sub")
        self.assertTrue(browser._download_button.instate(["!disabled"]))

    def test_superseded_result_is_ignored_and_only_latest_path_runs(self) -> None:
        paths: list[str] = []
        old_started, release_old = threading.Event(), threading.Event()
        new_started, release_new = threading.Event(), threading.Event()
        events: dict[str, threading.Event] = {}
        self.addCleanup(release_old.set)
        self.addCleanup(release_new.set)

        def browse(project, path, offset, cancel):
            paths.append(path)
            events[path] = cancel
            if path == "old":
                old_started.set()
                release_old.wait(timeout=3)
            elif path == "new":
                new_started.set()
                release_new.wait(timeout=3)
            return listing(path, (path or "root", "directory", None))

        browser = self.browser(browse)
        self.ready(browser)
        set_entry(browser._path_entry, "old")
        browser._go_button.invoke()
        self.assertTrue(old_started.wait(timeout=2))
        for path in ("intermediate", "new"):
            set_entry(browser._path_entry, path)
            browser._go_button.invoke()
        self.assertTrue(events["old"].is_set())
        release_old.set()
        self.wait_for(new_started.is_set)
        self.assertEqual(browser._location.cget("text"), "当前路径：.")
        self.assertEqual(browser._path_entry.get(), "new")
        release_new.set()
        self.ready(browser)
        self.assertEqual(paths, ["", "old", "new"])
        self.assertEqual(browser._location.cget("text"), "当前路径：new")
        self.assertFalse(events["new"].is_set())

    def test_thread_start_failure_is_reported_and_can_retry(self) -> None:
        with patch.object(
            threading.Thread, "start", side_effect=RuntimeError("no worker")
        ):
            browser = self.browser(lambda *_: listing(""))
        self.assertIn("无法启动后台线程", browser._status.cget("text"))
        self.assertTrue(browser._refresh_button.instate(["!disabled"]))
        self.assertIsNone(browser._after_id)
        browser._refresh_button.invoke()
        self.ready(browser)
        self.assertIn("没有条目", browser._status.cget("text"))

    def test_layout_fits_minimum_width_with_larger_scaling(self) -> None:
        self.root.tk.call("tk", "scaling", 2.0)
        gui._apply_theme(self.root)
        browser = self.browser(lambda *_: listing(""))
        width, height = browser.minsize()
        browser.geometry(f"{width}x{height}")
        self.ready(browser)
        if not browser.winfo_ismapped():
            self.skipTest("File-browser window was not mapped")
        for widget in (browser._go_button, browser._range_button, browser._status):
            with self.subTest(widget=widget):
                self.assertTrue(widget.winfo_ismapped())
                self.assertLessEqual(
                    widget.winfo_rootx() + widget.winfo_width(),
                    browser.winfo_rootx() + browser.winfo_width(),
                )
                self.assertLessEqual(
                    widget.winfo_rooty() + widget.winfo_height(),
                    browser.winfo_rooty() + browser.winfo_height(),
                )

    def test_close_cancels_browse_and_removes_timer(self) -> None:
        started, stopped = threading.Event(), threading.Event()

        def browse(project, path, offset, cancel):
            started.set()
            if cancel.wait(timeout=3):
                stopped.set()
            return listing("")

        browser = self.browser(browse)
        self.assertTrue(started.wait(timeout=2))
        timer = browser._after_id
        self.assertIsNotNone(timer)
        browser.close()
        browser.close()
        self.assertTrue(stopped.wait(timeout=2))
        self.assertNotIn(timer, self.root.tk.call("after", "info"))
        self.assertTrue(self.root.winfo_exists())
        self.root.update()
        self.assertEqual(self._tk_errors, [])

    def test_owner_teardown_cancels_download(self) -> None:
        started, stopped = threading.Event(), threading.Event()

        def save(project, path, destination, range_value, cancel):
            started.set()
            if cancel.wait(timeout=3):
                stopped.set()
            return 1

        browser = self.browser(lambda *_: listing("", ("file", "file", 1)), save)
        self.ready(browser)
        self.select(browser, "file")
        with patch(
            "winrunner_gui_files.filedialog.asksaveasfilename", return_value="dest"
        ):
            browser._download_button.invoke()
        self.assertTrue(started.wait(timeout=2))
        self.assertTrue(browser._go_button.instate(["disabled"]))
        self.assertIn("正在下载", browser._status.cget("text"))
        timer = browser._after_id
        self.assertIsNotNone(timer)
        self.root.destroy()
        self.assertTrue(stopped.wait(timeout=2))
        self.assertNotIn(timer, self.root.tk.call("after", "info"))
        self.assertTrue(browser._closed)

    def test_window_closed_while_save_picker_is_active(self) -> None:
        self.unexpected_save = False

        def save(*args):
            self.unexpected_save = True
            return 0

        browser = self.browser(lambda *_: listing("", ("file.txt", "file", 1)), save)
        self.ready(browser)
        self.select(browser, "file.txt")

        def close_during_picker(**kwargs):
            browser.close()
            return "dest"

        with patch(
            "winrunner_gui_files.filedialog.asksaveasfilename",
            side_effect=close_during_picker,
        ):
            browser._download_button.invoke()
        self.assertTrue(browser._closed)
        self.assertIsNone(browser._running)
        self.assertFalse(self.unexpected_save)
