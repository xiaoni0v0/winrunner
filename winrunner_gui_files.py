"""Modeless project file browser for the local WinRunner dashboard."""

from __future__ import annotations

import re
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from queue import Empty, SimpleQueue
from tkinter import filedialog, ttk
from typing import Literal

import winrunner_gui as gui
from runner import APIError

Browse = Callable[[str, str, int, threading.Event], dict]
Save = Callable[[str, str, Path, str | None, threading.Event], int]
POLL_MS = 35
_RANGE = re.compile(r"bytes=(?:[0-9]+-[0-9]*|-[0-9]+)", re.I | re.ASCII)


@dataclass(frozen=True)
class _Entry:
    name: str
    kind: str
    size: int | None


@dataclass(frozen=True)
class _Page:
    path: str
    entries: tuple[_Entry, ...]
    next_offset: int | None = None


@dataclass(frozen=True)
class _Job:
    run: Callable[[threading.Event], _Page | int]
    # A browse job carries its cursor history; a save job has no offset.
    offsets: tuple[int, ...] = ()
    path_input: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass(frozen=True)
class _Result:
    value: _Page | int | None = None
    error: str = ""


def _load_page(
    browse: Browse, project: str, path: str, offset: int, cancel: threading.Event
) -> _Page:
    data = browse(project, path, offset, cancel)
    relative = data["path"]
    if "entries" not in data:
        raise APIError(
            400, "not_a_directory", "这是文件。请转到所在目录，选中文件后下载。"
        )
    entries = tuple(
        _Entry(item["name"], item["type"], item["size_bytes"])
        for item in data["entries"]
    )
    return _Page(relative, entries, data.get("next_entry_offset"))


def _run_job(job: _Job, results: SimpleQueue[_Result]) -> None:
    # The thread only receives callables, plain data, and synchronization objects.
    try:
        result = (
            _Result() if job.cancel.is_set() else _Result(value=job.run(job.cancel))
        )
    except APIError as exc:
        result = _Result(error=f"{exc.status} {exc.code}：{exc}")
    except Exception:
        result = _Result(error="发生意外错误")
    results.put(result)


def _display(text: str) -> str:
    # Windows directory entries can contain unpaired UTF-16 surrogates.
    # Keep the original names for backend calls, but pass safe strings to Tk.
    return text.encode("utf-8", "backslashreplace").decode().replace("\0", "\\x00")


class FileBrowser(tk.Toplevel):
    """Browse one project's files without blocking or changing its owner window."""

    def __init__(self, parent: tk.Tk, project: str, browse: Browse, save: Save) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("The file browser must run on the main thread")
        super().__init__(parent, bg=gui.BG)
        self.project = project
        self._browse = browse
        self._save = save
        self._page: _Page | None = None
        self._offsets: tuple[int, ...] = (0,)
        self._rows: dict[str, _Entry] = {}
        self._results: SimpleQueue[_Result] = SimpleQueue()
        self._running: _Job | None = None
        self._pending: _Job | None = None
        self._after_id: str | None = None
        self._picking_destination = False
        self._closed = False

        self.title(f"WinRunner · 项目文件 · {_display(project)}")
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Destroy>", self._on_destroy)
        self._build()
        self._browse_to("")

    @staticmethod
    def _entry(parent: tk.Misc) -> tk.Entry:
        return tk.Entry(
            parent,
            width=10,
            font=(gui.MONO, 10),
            bg=gui.INSET,
            fg=gui.INK,
            insertbackground=gui.INK,
            disabledbackground=gui.HEADER,
            disabledforeground=gui.MUTED,
            selectbackground=gui.HOVER,
            selectforeground=gui.INK,
            relief="flat",
            highlightthickness=1,
            highlightbackground=gui.BORDER,
            highlightcolor=gui.TEAL,
        )

    def _build(self) -> None:
        scale = max(1.0, float(self.tk.call("tk", "scaling")) / 1.33)
        screen_width = max(1, self.winfo_screenwidth() - 24)
        screen_height = max(1, self.winfo_screenheight() - 75)
        self.geometry(
            f"{min(round(740 * scale), screen_width)}x"
            f"{min(round(510 * scale), screen_height)}"
        )
        self.minsize(
            min(round(520 * scale), screen_width),
            min(round(385 * scale), screen_height),
        )
        panel = tk.Frame(self, bg=gui.PANEL, padx=12, pady=10)
        panel.pack(fill="both", expand=True, padx=10, pady=10)
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(3, weight=1)

        self._location = gui._label(panel, "尚未加载目录", fg=gui.TEAL, bold=True)
        self._location.configure(width=1)
        self._location.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        navigation = tk.Frame(panel, bg=gui.PANEL)
        navigation.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self._root_button = ttk.Button(navigation, text="根目录", command=self._go_root)
        self._up_button = ttk.Button(navigation, text="上一级", command=self._up)
        self._refresh_button = ttk.Button(
            navigation, text="刷新", command=self._refresh
        )
        self._open_button = ttk.Button(navigation, text="进入目录", command=self._open)
        for button in (
            self._root_button,
            self._up_button,
            self._refresh_button,
            self._open_button,
        ):
            button.pack(side="left", padx=(0, 6))

        path_bar = tk.Frame(panel, bg=gui.PANEL)
        path_bar.grid(row=2, column=0, sticky="ew", pady=(0, 9))
        path_bar.columnconfigure(1, weight=1)
        gui._label(path_bar, "项目相对目录（留空为根目录）", fg=gui.MUTED).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 3)
        )
        self._path_entry = self._entry(path_bar)
        self._path_entry.grid(row=1, column=0, columnspan=2, sticky="ew", ipady=3)
        self._path_entry.bind("<Return>", self._go)
        self._go_button = ttk.Button(path_bar, text="转到", command=self._go)
        self._go_button.grid(row=1, column=2, padx=(6, 0))

        table = tk.Frame(panel, bg=gui.PANEL)
        table.grid(row=3, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self._tree = ttk.Treeview(
            table,
            columns=("name", "type", "size"),
            show="headings",
            selectmode="browse",
            height=1,
        )
        self._tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self._tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=scroll.set)
        for name, title, width in (
            ("name", "名称", 240),
            ("type", "类型", 90),
            ("size", "大小", 128),
        ):
            anchor: Literal["e", "w"] = "e" if name == "size" else "w"
            self._tree.heading(name, text=title, anchor=anchor)
            self._tree.column(name, width=width, anchor=anchor, stretch=name == "name")
        self._tree.tag_configure("stripe", background=gui.HEADER)
        self._tree.tag_configure("directory", foreground=gui.TEAL)
        self._tree.tag_configure("other", foreground=gui.MUTED)
        self._tree.bind("<<TreeviewSelect>>", self._sync_controls)
        self._tree.bind("<Double-1>", self._open_clicked)
        self._tree.bind("<Return>", self._open)

        paging = tk.Frame(panel, bg=gui.PANEL)
        paging.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self._previous_button = ttk.Button(
            paging, text="上一页", command=self._previous
        )
        self._next_button = ttk.Button(paging, text="下一页", command=self._next)
        self._previous_button.pack(side="left", padx=(0, 6))
        self._next_button.pack(side="left", padx=(0, 8))
        self._page_label = gui._label(paging, "", fg=gui.MUTED)
        self._page_label.configure(width=1)
        self._page_label.pack(side="left", fill="x", expand=True)

        downloads = tk.Frame(panel, bg=gui.PANEL)
        downloads.grid(row=5, column=0, sticky="ew", pady=(8, 0))
        downloads.columnconfigure(1, weight=1)
        gui._label(downloads, "字节范围（如 bytes=0-1023）", fg=gui.MUTED).grid(
            row=0, column=1, columnspan=3, sticky="w", pady=(0, 3)
        )
        self._download_button = ttk.Button(
            downloads, text="下载全部", command=self._download
        )
        self._download_button.grid(row=1, column=0, padx=(0, 12))
        self._range_entry = self._entry(downloads)
        self._range_entry.grid(row=1, column=1, sticky="ew", ipady=3)
        self._range_button = ttk.Button(
            downloads, text="下载范围", command=partial(self._download, ranged=True)
        )
        self._range_button.grid(row=1, column=2, padx=(6, 0))
        ttk.Button(downloads, text="关闭", command=self.close).grid(
            row=1, column=3, padx=(8, 0)
        )

        self._status = gui._label(panel, "", fg=gui.MUTED)
        self._status.configure(justify="left", wraplength=530)
        self._status.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        panel.bind(
            "<Configure>",
            lambda event: self._status.configure(wraplength=max(1, event.width - 24)),
        )
        self._path_entry.focus_set()

    def _selected(self) -> _Entry | None:
        selection = self._tree.selection()
        return self._rows.get(selection[0]) if selection else None

    def _busy(self) -> bool:
        return self._closed or self._picking_destination or self._running is not None

    def _sync_controls(self, _event: tk.Event | None = None) -> None:
        if self._closed:
            return
        idle = not self._busy()
        page = self._page
        selected = self._selected()
        file_selected = idle and selected is not None and selected.kind == "file"
        go = not self._picking_destination and (
            self._running is None or bool(self._running.offsets)
        )
        states = (
            (self._root_button, idle and (page is None or bool(page.path))),
            (self._up_button, idle and page is not None and bool(page.path)),
            (self._refresh_button, idle),
            (
                self._open_button,
                idle and selected is not None and selected.kind == "directory",
            ),
            (self._previous_button, idle and len(self._offsets) > 1),
            (
                self._next_button,
                idle and page is not None and page.next_offset is not None,
            ),
            (self._download_button, file_selected),
            (self._range_button, file_selected),
            (self._go_button, go),
        )
        for button, enabled in states:
            button.state(["!disabled" if enabled else "disabled"])
        self._tree.state(["!disabled" if idle else "disabled"])
        self._path_entry.configure(state="normal" if go else "disabled")
        self._range_entry.configure(state="normal" if file_selected else "disabled")

    def _set_status(self, text: str, color: str = gui.MUTED) -> None:
        self._status.configure(text=_display(text), fg=color)

    def _go_root(self) -> None:
        if not self._busy():
            self._browse_to("")

    def _up(self) -> None:
        if not self._busy() and self._page is not None and self._page.path:
            self._browse_to(self._page.path.rpartition("/")[0])

    def _refresh(self) -> None:
        if not self._busy():
            self._browse_to(self._page.path if self._page is not None else "")

    def _go(self, _event: tk.Event | None = None) -> None:
        if (
            not self._closed
            and not self._picking_destination
            and (self._running is None or self._running.offsets)
        ):
            self._browse_to(self._path_entry.get())

    def _entry_path(self, entry: _Entry) -> str:
        assert self._page is not None
        return "/".join(part for part in (self._page.path, entry.name) if part)

    def _open(self, _event: tk.Event | None = None) -> str:
        if not self._busy():
            entry = self._selected()
            if entry is not None and entry.kind == "directory":
                self._browse_to(self._entry_path(entry))
        return "break"

    def _open_clicked(self, event: tk.Event) -> str:
        if not self._busy() and self._tree.identify_region(event.x, event.y) == "cell":
            row = self._tree.identify_row(event.y)
            if row:
                self._tree.selection_set(row)
                self._open()
        return "break"

    def _previous(self) -> None:
        if not self._busy() and self._page is not None and len(self._offsets) > 1:
            self._browse_to(self._page.path, self._offsets[:-1])

    def _next(self) -> None:
        if not self._busy() and self._page is not None:
            offset = self._page.next_offset
            if offset is not None:
                self._browse_to(self._page.path, (*self._offsets, offset))

    def _browse_to(self, path: str, offsets: tuple[int, ...] = (0,)) -> None:
        self._set_status("正在读取项目文件…", gui.BLUE)
        self._submit(
            _Job(
                partial(_load_page, self._browse, self.project, path, offsets[-1]),
                offsets,
                self._path_entry.get(),
            )
        )

    def _download(self, *, ranged: bool = False) -> None:
        if self._busy():
            return
        entry = self._selected()
        if entry is None or entry.kind != "file":
            return
        range_value = self._range_entry.get().strip() if ranged else None
        if range_value is not None and not _RANGE.fullmatch(range_value):
            self._set_status(
                "请输入单个字节范围，例如 bytes=0-1023、bytes=1024- 或 bytes=-512。",
                gui.RED,
            )
            self._range_entry.focus_set()
            return
        path = self._entry_path(entry)
        self._picking_destination = True
        self._sync_controls()
        try:
            selected = filedialog.asksaveasfilename(
                parent=self,
                title="保存字节范围" if ranged else "下载项目文件",
                initialfile=_display(entry.name + (".part" if ranged else "")),
            )
        except tk.TclError:
            if not self._closed:
                self._set_status("无法打开文件保存对话框", gui.RED)
            return
        finally:
            self._picking_destination = False
            self._sync_controls()
        # The native picker can run a nested event loop in which the owner closes.
        if self._closed or not selected:
            return
        self._set_status("正在下载…关闭此窗口可取消。", gui.BLUE)
        self._submit(
            _Job(partial(self._save, self.project, path, Path(selected), range_value))
        )

    def _submit(self, job: _Job) -> None:
        # Serialize backend calls. While cancellation takes effect, keep only
        # the latest typed-path request rather than starting more threads.
        if self._running is not None:
            self._running.cancel.set()
            if self._pending is not None:
                self._pending.cancel.set()
            self._pending = job
        else:
            self._start(job)

    def _start(self, job: _Job) -> None:
        self._running = job
        self._sync_controls()
        try:
            threading.Thread(
                target=_run_job, args=(job, self._results), daemon=True
            ).start()
        except RuntimeError:
            self._running = None
            job.cancel.set()
            self._show_error(job, "无法启动后台线程")
            self._sync_controls()
        else:
            self._after_id = self.after(POLL_MS, self._receive)

    def _show_error(self, job: _Job, error: str) -> None:
        label = "读取失败" if job.offsets else "下载失败"
        self._set_status(f"{label}（{error}）", gui.RED)

    def _receive(self) -> None:
        self._after_id = None
        job = self._running
        if self._closed or job is None:
            return
        try:
            result = self._results.get_nowait()
        except Empty:
            self._after_id = self.after(POLL_MS, self._receive)
            return
        self._running = None
        if self._pending is not None:
            pending, self._pending = self._pending, None
            self._start(pending)
            return
        if result.error:
            self._show_error(job, result.error)
        elif isinstance(result.value, _Page):
            self._show_page(result.value, job)
        elif isinstance(result.value, int):
            self._set_status(f"下载完成 · {result.value:,} 字节", gui.GREEN)
        self._sync_controls()

    def _show_page(self, page: _Page, job: _Job) -> None:
        self._page = page
        self._offsets = job.offsets
        if self._path_entry.get() == job.path_input:
            self._path_entry.delete(0, tk.END)
            self._path_entry.insert(0, _display(page.path))
        self._location.configure(text=_display(f"当前路径：{page.path or '.'}"))
        self._tree.delete(*self._tree.get_children())
        self._rows.clear()
        types = {"directory": "目录", "file": "文件", "other": "其他（不可打开）"}
        for index, entry in enumerate(page.entries):
            row = self._tree.insert(
                "",
                "end",
                values=(
                    _display(entry.name),
                    types.get(entry.kind, types["other"]),
                    gui._size(entry.size),
                ),
                tags=(entry.kind, "stripe") if index % 2 else (entry.kind,),
            )
            self._rows[row] = entry
        self._tree.yview_moveto(0)
        self._page_label.configure(
            text=f"第 {len(self._offsets)} 页 · 本页 {len(page.entries)} 项"
        )
        self._set_status(
            "双击目录进入，选中文件后可下载。" if page.entries else "没有条目。"
        )

    def _stop(self) -> None:
        self._closed = True
        if self._running is not None:
            self._running.cancel.set()
            self._running = None
        if self._pending is not None:
            self._pending.cancel.set()
            self._pending = None
        if self._after_id is not None:
            self.after_cancel(self._after_id)
            self._after_id = None

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self and not self._closed:
            # Covers Tcl-side destruction in addition to Tkinter parent teardown.
            self._stop()

    def close(self) -> None:
        self.destroy()

    def destroy(self) -> None:
        if not self._closed:
            self._stop()
            super().destroy()
