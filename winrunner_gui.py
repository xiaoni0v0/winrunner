"""Local WinRunner desktop console. Run beside config.py with Python 3.10+."""

from __future__ import annotations

import base64
import codecs
import math
import re
import signal
import threading
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from queue import Empty, SimpleQueue
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import TYPE_CHECKING, Literal

from runner import APIError
from winrunner_desktop import DesktopRunner, ProjectSelection, Submission

if TYPE_CHECKING:
    from winrunner_gui_files import FileBrowser
    from winrunner_gui_forms import FormKind, SubmitForm

INTEGRATED_REFRESH_MS = 850
REOPEN_MS = 450
BACKEND_EXIT_MS = 850
LOG_PAGE = 32768
LOG_OVERLAP = 3  # Longest UTF-8 scalar minus one byte.
LOG_READ = LOG_PAGE + 2 * LOG_OVERLAP
STREAMS = ("stdout", "stderr")

BG = "#0e1520"
PANEL = "#172332"
HEADER = "#131e2b"
INSET = "#101a27"
RAISED = "#213249"
HOVER = "#2b4058"
BORDER = "#2c3d51"
INK = "#edf4fa"
MUTED = "#a5b6c8"
TEAL = "#65e0cd"
BLUE = "#92bdff"
GREEN = "#8de3b6"
AMBER = "#ffcf89"
RED = "#ffabb4"
FONT = "Microsoft YaHei UI"
MONO = "Consolas"
DASH = "—"
MIN_WIDTH, MIN_HEIGHT = 870, 700
STATUS_COLORS = {"active": BLUE, "success": GREEN, "warning": AMBER, "error": RED}
METRIC_COLORS = (TEAL, BLUE, "#c0a9fa", AMBER)

STATUSES = {
    "cloning": ("克隆中", "active"),
    "deleting": ("删除中", "active"),
    "delete_failed": ("删除失败", "error"),
    "idle": ("空闲", "neutral"),
    "preparing": ("准备中", "active"),
    "running": ("运行中", "active"),
    "cancelling": ("取消中", "warning"),
    "succeeded": ("成功", "success"),
    "failed": ("失败", "error"),
    "cancelled": ("已取消", "warning"),
}
PHASES = {
    "reset": "重置工作树",
    "validate": "校验参数",
    "fetch": "更新远端引用",
    "resolve": "解析提交",
    "checkout": "检出",
    "launch": "启动",
    "run": "执行",
}
COMMAND_COLUMNS = (
    ("id", "ID", 55, False),
    ("mode", "Shell", 88, True),
    ("status", "状态", 66, True),
    ("phase", "阶段", 96, True),
    ("exit", "退出码", 60, False),
    ("stdout", "stdout", 76, True),
    ("stderr", "stderr", 76, True),
    ("started_at", "开始时间", 144, False),
    ("finished_at", "结束时间", 144, False),
)

JsonObject = dict[str, object]
Row = tuple[str, tuple[str, ...], str]
Column = tuple[str, str, int, bool]


@dataclass(frozen=True)
class Response:
    data: JsonObject | None = None
    error: str = ""
    status: int = 200


@dataclass(frozen=True)
class Target:
    project: str | None
    command: str | None = None  # None refers to the project's Action.


@dataclass(frozen=True)
class View:
    project: str | None = None
    target: Target | None = None
    stdout: int = 0
    stderr: int = 0
    revision: int = 0
    superseded: threading.Event = field(
        default_factory=threading.Event, compare=False, repr=False
    )

    def offsets(self) -> dict[str, int]:
        return {"stdout": self.stdout, "stderr": self.stderr}


@dataclass(frozen=True)
class ViewData:
    view: View
    action: Response | None = None
    commands: Response | None = None
    detail: Response | None = None


@dataclass(frozen=True)
class Snapshot:
    health: Response
    projects: Response | None = None
    commands: Response | None = None
    note: str = ""
    selected: ViewData | None = None


def read_view(
    view: View,
    list_commands: Callable[[str], Response],
    get_task: Callable[[Target, dict[str, int] | None], Response],
) -> ViewData:
    action = commands = detail = None
    if view.project is not None and not view.superseded.is_set():
        if view.target != Target(view.project):
            action = get_task(Target(view.project), None)
        if not view.superseded.is_set():
            commands = list_commands(view.project)
    if view.target is not None and not view.superseded.is_set():
        detail = get_task(
            view.target,
            {
                stream: max(0, start - LOG_OVERLAP)
                for stream, start in view.offsets().items()
            },
        )
        if view.target.command is None:
            action = detail
    return ViewData(view, action, commands, detail)


def _poll_snapshot(
    snapshot: Callable[[], Snapshot],
    stop: threading.Event,
    results: SimpleQueue[Snapshot],
) -> None:
    if stop.is_set():
        return
    try:
        result = snapshot()
    except Exception:
        # Like HTTP polling, show an unavailable state and let the next refresh retry.
        result = Snapshot(Response(error="无法读取本机 Runner 状态"))
    if not stop.is_set():
        results.put(result)


def _object(value: object) -> JsonObject:
    return value if isinstance(value, dict) else {}


def _text(value: object, fallback: str = DASH) -> str:
    return value if isinstance(value, str) and value else fallback


def _number(value: object) -> int | float | None:
    if type(value) is int and 0 <= value <= 2**64 - 1:
        return value
    if isinstance(value, float) and math.isfinite(value) and 0 <= value <= 2**64 - 1:
        return value
    return None


def _size(value: object) -> str:
    size = _number(value)
    if size is None:
        return DASH
    scaled = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        if scaled < 1024 or unit == "EiB":
            return f"{scaled:,.0f} B" if unit == "B" else f"{scaled:,.1f} {unit}"
        scaled /= 1024
    return DASH


def _uptime(value: object) -> str:
    seconds = _number(value)
    if seconds is None:
        return DASH
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    minutes, seconds = divmod(rest, 60)
    clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{days} 天 {clock}" if days else clock


def _capacity(data: JsonObject) -> tuple[str, str, float]:
    total = _number(data.get("total_bytes"))
    free = _number(data.get("available_bytes"))
    if not total or free is None or free > total:
        return DASH, "用量暂不可用", 0
    used = total - free
    percent = 100 * used / total
    return f"{percent:.0f}%", f"已用 {_size(used)} / {_size(total)}", percent


def _cpu_usage(data: JsonObject) -> tuple[str, str, float]:
    def percent(key: str) -> int | float | None:
        value = _number(data.get(key))
        return value if value is not None and value <= 100 else None

    def label(value: int | float | None) -> str:
        return f"{value:.1f}%" if value is not None else DASH

    latest = percent("usage_percent")
    short = label(percent("usage_5s_percent"))
    long = label(percent("usage_60s_percent"))
    return label(latest), f"5s {short}  ·  60s {long}", latest or 0


def _local_time(value: object) -> str:
    if type(value) is int:
        try:
            return f"{datetime.fromtimestamp(value // 1000):%Y-%m-%d %H:%M:%S}"
        except (ValueError, OverflowError, OSError):
            pass
    return DASH


def _rows(response: Response | None, collection: str) -> tuple[list[Row], str]:
    if response is None:
        return [], "等待 Runner 状态"
    if response.data is None:
        return [], response.error
    records = response.data.get(collection)
    if not isinstance(records, list):
        return [], "Runner 返回了无效列表"
    rows: list[Row] = []
    seen: set[str] = set()
    for record in records:
        entry = _object(record)
        key = _text(entry.get("name" if collection == "projects" else "id"), "")
        pattern = (
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
            if collection == "projects"
            else r"[0-9a-z]+"
        )
        if not re.fullmatch(pattern, key) or key in seen:
            return [], "Runner 返回了无效列表"
        seen.add(key)
        status = _text(entry.get("status"))
        label, tag = STATUSES.get(status, (status, "neutral"))
        values: tuple[str, ...]
        if collection == "projects":
            values = (key, label)
        else:
            mode = _text(entry.get("mode"))
            phase = _text(entry.get("phase"))
            exit_code = entry.get("exit_code")
            values = (
                key,
                {"cmd": "CMD", "ps": "PowerShell"}.get(mode, mode),
                label,
                PHASES.get(phase, phase),
                str(exit_code) if type(exit_code) is int else DASH,
                _size(entry.get("stdout_bytes")),
                _size(entry.get("stderr_bytes")),
                _local_time(entry.get("started_at")),
                _local_time(entry.get("finished_at")),
            )
        rows.append((key, values, tag))
    if collection == "projects":
        rows.sort(key=lambda row: row[0].casefold())
    else:
        # The API lists commands in insertion order. Keep IDs as opaque strings.
        rows.reverse()
    return rows, ""


def _label(
    parent: tk.Misc,
    text: str,
    *,
    bg: str = PANEL,
    fg: str = INK,
    size: int = 10,
    bold: bool = False,
    wraplength: int = 0,
) -> tk.Label:
    return tk.Label(
        parent,
        text=text,
        bg=bg,
        fg=fg,
        anchor="w",
        wraplength=wraplength,
        font=(FONT, size, "bold" if bold else "normal"),
    )


def _ellipsize(text: str, width: int, measure: Callable[[str], int]) -> str:
    if measure(text) <= width:
        return text
    if measure("…") > width:
        return ""
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if measure(text[:mid] + "…") <= width:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + "…"


class FitLabel(tk.Label):
    """Keep data-driven text from setting the minimum size of its container."""

    def __init__(
        self,
        parent: tk.Misc,
        text: str,
        *,
        bg: str = PANEL,
        fg: str = INK,
        size: int = 10,
        bold: bool = False,
        family: str = FONT,
    ) -> None:
        self._font = tkfont.Font(
            root=parent, family=family, size=size, weight="bold" if bold else "normal"
        )
        super().__init__(
            parent,
            width=1,
            padx=0,
            pady=0,
            borderwidth=0,
            highlightthickness=0,
            anchor="w",
            bg=bg,
            fg=fg,
            font=self._font,
        )
        self._text = ""
        self.set_text(text)
        self.bind("<Configure>", self._fit)

    def set_text(self, text: str) -> None:
        # Labels are single-line surfaces; full command/log text lives in Text.
        text = text.encode("utf-8", "backslashreplace").decode().replace("\0", "\\x00")
        text = re.sub(r"[\r\n\t]+", " ", text)
        if text != self._text:
            self._text = text
            self._fit()

    def _fit(self, _event: tk.Event | None = None) -> None:
        visible = _ellipsize(self._text, self.winfo_width(), self._font.measure)
        if visible != self.cget("text"):
            self.configure(text=visible)


class Metric(tk.Frame):
    def __init__(
        self, parent: tk.Misc, title: str, accent: str, bar: bool = True
    ) -> None:
        super().__init__(
            parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1
        )
        tk.Frame(self, bg=accent, height=2).pack(fill="x")
        body = tk.Frame(self, bg=PANEL, padx=10, pady=5)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        self.title = FitLabel(body, title, fg=MUTED, size=9, bold=True)
        self.title.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.value = FitLabel(body, DASH, size=12, bold=True, family=MONO)
        self.detail = FitLabel(body, DASH, fg=MUTED, size=8)
        if bar:
            self.value.configure(anchor="e", width=7)
            self.value.grid(row=0, column=1, sticky="ew")
            self.detail.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(1, 4))
        else:
            self.title.grid_configure(columnspan=2)
            self.value.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(1, 0))
        self.bar = ttk.Progressbar(
            body, style=f"{accent}.Horizontal.TProgressbar", maximum=100
        )
        if bar:
            self.bar.grid(row=2, column=0, columnspan=2, sticky="ew")

    def update_value(self, value: str, detail: str, percent: float = 0) -> None:
        self.value.set_text(value)
        self.detail.set_text(detail)
        self.bar.configure(value=percent)


class StatusTable(tk.Frame):
    def __init__(
        self, parent: tk.Misc, title: str, columns: tuple[Column, ...]
    ) -> None:
        super().__init__(parent, bg=PANEL, padx=10, pady=6)
        header = self.header = tk.Frame(self, bg=PANEL)
        header.pack(fill="x", pady=(0, 4))
        header.columnconfigure(1, weight=1)
        _label(header, title, size=10, bold=True).grid(row=0, column=0, sticky="w")
        self.summary = FitLabel(header, "等待刷新…", fg=MUTED, size=8)
        self.summary.configure(anchor="e")
        self.summary.grid(row=0, column=1, padx=(6, 0), sticky="ew")
        body = tk.Frame(self, bg=PANEL)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            body,
            columns=[column[0] for column in columns],
            show="headings",
            selectmode="browse",
            height=1,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        cell_font = tkfont.Font(root=self, family=FONT, size=10)
        examples = {
            "mode": "PowerShell",
            "status": "删除失败",
            "phase": "更新远端引用",
            "stdout": "1,024.0 MiB",
            "stderr": "1,024.0 MiB",
            "started_at": "2026-01-02 03:04:05",
            "finished_at": "2026-01-02 03:04:05",
        }
        for key, label, width, stretch in columns:
            anchor: Literal["e", "w"] = (
                "e" if key in {"exit", "stdout", "stderr"} else "w"
            )
            width = max(width, cell_font.measure(examples.get(key, label)) + 16)
            self.tree.heading(key, text=label, anchor=anchor)
            self.tree.column(
                key, width=width, minwidth=width, stretch=stretch, anchor=anchor
            )
        for tag, color in STATUS_COLORS.items():
            self.tree.tag_configure(tag, foreground=color)
        self.tree.tag_configure("stripe", background=HEADER)
        if columns == COMMAND_COLUMNS:
            self.horizontal = ttk.Scrollbar(
                body, orient="horizontal", command=self.tree.xview
            )
            self.horizontal.grid(row=1, column=0, sticky="ew")
            self.horizontal.grid_remove()
            self.tree.configure(xscrollcommand=self._scroll_x)
        self.empty_message = FitLabel(self.tree, "等待刷新…", fg=MUTED)
        self.empty_message.configure(anchor="center")
        self._place_empty()
        self._rendered: list[Row] = []

    def _scroll_x(self, first: float, last: float) -> None:
        self.horizontal.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.horizontal.grid_remove()
        else:
            self.horizontal.grid()

    def _place_empty(self) -> None:
        self.empty_message.place(relx=0.5, rely=0.62, relwidth=0.9, anchor="center")

    def show_rows(
        self, rows: list[Row], error: str = "", *, reset: bool = False
    ) -> None:
        if error and not reset and self._rendered:
            self.summary.set_text(f"暂不可用 · 保留上次 {len(self._rendered)} 条")
            self.summary.configure(fg=AMBER)
            return
        if reset or rows != self._rendered:
            old = self.tree.get_children()
            ids = {key for key, _, _ in rows}
            for key in old:
                if reset or key not in ids:
                    self.tree.delete(key)
            for index, (key, values, status) in enumerate(rows):
                tags = (status, "stripe") if index % 2 else (status,)
                if self.tree.exists(key):
                    self.tree.item(key, values=values, tags=tags)
                    if self.tree.index(key) != index:
                        self.tree.move(key, "", index)
                else:
                    self.tree.insert("", index, iid=key, values=values, tags=tags)
            self._rendered = rows
        self.summary.set_text("不可用" if error else f"{len(rows)} 条记录")
        self.summary.configure(fg=AMBER if error else MUTED)
        if rows:
            self.empty_message.place_forget()
        else:
            self.empty_message.set_text(error or "暂无记录")
            self._place_empty()


def _status(data: JsonObject) -> str:
    status = _text(data.get("status"))
    return STATUSES.get(status, (status, "neutral"))[0]


def _phase(data: JsonObject) -> str:
    phase = _text(data.get("phase"))
    return PHASES.get(phase, phase)


def _find(response: Response | None, collection: str, key: str) -> JsonObject:
    records = response.data.get(collection) if response and response.data else None
    field = "name" if collection == "projects" else "id"
    if isinstance(records, list):
        for record in records:
            item = _object(record)
            if item.get(field) == key:
                return item
    return {}


def _utf8_boundary(raw: bytes, point: int, *, live: bool) -> int:
    """Assign a character spanning two pages to the page where it begins."""
    for lead in range(point - 1, max(-1, point - LOG_OVERLAP - 1), -1):
        byte = raw[lead]
        if byte & 0xC0 == 0x80:
            continue
        width = (
            2
            if 0xC2 <= byte <= 0xDF
            else 3 if 0xE0 <= byte <= 0xEF else 4 if 0xF0 <= byte <= 0xF4 else 0
        )
        end = lead + width
        if point < end and (end <= len(raw) or live):
            try:
                codecs.getincrementaldecoder("utf-8")().decode(
                    raw[lead:end], final=not live
                )
            except UnicodeDecodeError:
                break
            return min(end, len(raw))
        break
    return point


def _decode_log(data: JsonObject, stream: str, start: int = 0) -> str:
    encoded = _text(_object(data.get(stream)).get("data_base64"), "")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError:
        return "[日志数据无效]"
    prefix = min(start, LOG_OVERLAP, len(raw))
    page_end = min(len(raw), prefix + LOG_PAGE)
    live = data.get("status") in ("preparing", "running", "cancelling")
    page = raw[
        _utf8_boundary(raw, prefix, live=live) : _utf8_boundary(
            raw, page_end, live=live
        )
    ]
    try:
        return codecs.getincrementaldecoder("utf-8")().decode(page, final=not live)
    except UnicodeDecodeError:
        # CMD and Windows native tools may use the console OEM code page.
        if data.get("mode") == "cmd":
            return raw[prefix:page_end].decode("oem", errors="replace")
        return codecs.getincrementaldecoder("utf-8")(errors="replace").decode(
            page, final=not live
        )


class ReadOnlyText(tk.Frame):
    def __init__(
        self, parent: tk.Misc, *, wrap: Literal["none", "char", "word"] = "none"
    ) -> None:
        super().__init__(parent, bg=PANEL)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text = tk.Text(
            self,
            height=2,
            width=1,
            wrap=wrap,
            state="disabled",
            bg=INSET,
            fg=INK,
            relief="flat",
            padx=9,
            pady=6,
            font=(MONO, 10),
            selectbackground=HOVER,
            selectforeground=INK,
            inactiveselectbackground=RAISED,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=TEAL,
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=scroll.set)
        if wrap == "none":
            horizontal = ttk.Scrollbar(
                self, orient="horizontal", command=self.text.xview
            )
            horizontal.grid(row=1, column=0, sticky="ew")
            self.text.configure(xscrollcommand=horizontal.set)
        self.value = ""

    def show(self, value: str) -> None:
        if value == self.value:
            return
        old = self.value
        self.value = value
        self.text.configure(state="normal")
        append = bool(old) and value.startswith(old)
        if not append:
            self.text.delete("1.0", "end")
        text = value[len(old) :] if append else value
        # Represent unpaired JSON surrogates and NULs without passing them to Tcl.
        self.text.insert(
            "end",
            text.encode("utf-8", "backslashreplace").decode().replace("\0", "\\x00"),
        )
        self.text.configure(state="disabled")


class TaskDetail(tk.Frame):
    def __init__(
        self,
        parent: tk.Misc,
        close: Callable[[], None],
        page: Callable[[str, int], None],
        save: Callable[[str], None] | None = None,
    ):
        super().__init__(parent, bg=PANEL, padx=14, pady=6)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        header = tk.Frame(self, bg=PANEL)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        self.title = FitLabel(header, "任务详情", bold=True, size=11)
        self.title.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(header, text="收起详情", command=close).grid(row=0, column=1)
        self.meta = FitLabel(self, "", fg=MUTED, size=9)
        self.meta.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.times = FitLabel(self, "", fg=MUTED, size=9)
        self.times.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        self.tabs = ttk.Notebook(self)
        self.tabs.grid(row=3, column=0, sticky="nsew")
        self.input = ReadOnlyText(self.tabs, wrap="char")
        self.tabs.add(self.input, text="命令原文")
        self.outputs: dict[str, ReadOnlyText] = {}
        self.ranges: dict[str, FitLabel] = {}
        self.buttons: dict[str, tuple[ttk.Button, ttk.Button]] = {}
        self.save_buttons: dict[str, ttk.Button] = {}
        for stream in STREAMS:
            tab = tk.Frame(self.tabs, bg=PANEL, padx=6, pady=5)
            self.tabs.add(tab, text=stream)
            nav = tk.Frame(tab, bg=PANEL)
            nav.pack(fill="x", pady=(0, 4))
            nav.columnconfigure(0, weight=1)
            self.ranges[stream] = FitLabel(nav, "", fg=MUTED, size=9, family=MONO)
            self.ranges[stream].grid(row=0, column=0, sticky="ew", padx=(0, 8))
            prev = ttk.Button(nav, text="上一页", command=partial(page, stream, -1))
            nxt = ttk.Button(nav, text="下一页", command=partial(page, stream, 1))
            prev.grid(row=0, column=1, padx=(0, 4))
            nxt.grid(row=0, column=2)
            self.buttons[stream] = (prev, nxt)
            if save is not None:
                button = ttk.Button(
                    nav, text="保存原始日志…", command=partial(save, stream)
                )
                button.grid(row=0, column=3, padx=(4, 0))
                self.save_buttons[stream] = button
            self.outputs[stream] = ReadOnlyText(tab)
            self.outputs[stream].pack(fill="both", expand=True)
        self.diagnostics = ReadOnlyText(self.tabs, wrap="word")
        self.tabs.add(self.diagnostics, text="诊断")

    def open(self, target: Target, *, keep_tab: bool = False) -> None:
        scope = target.project or "全局"
        title = (
            f"{scope}  /  Action"
            if target.command is None
            else f"{scope}  /  命令 #{target.command}"
        )
        self.title.set_text(title)
        if target.command is None:
            self.tabs.hide(self.input)
            self.tabs.select(self.outputs["stdout"].master)
        else:
            self.tabs.add(self.input, text="命令原文")
            if not keep_tab:
                self.tabs.select(self.input)
        self.show(View(), None)

    def show(self, view: View, result: Response | None) -> None:
        data = result.data if result and result.data else {}
        exit_code = data.get("exit_code")
        mode = {"cmd": "CMD", "ps": "PowerShell"}.get(_text(data.get("mode")))
        meta = f"{_status(data)}  ·  {_phase(data)}  ·  退出码 {exit_code if type(exit_code) is int else DASH}"
        self.meta.set_text(
            (
                (f"{mode}  ·  " if mode else "") + meta
                if data
                else (result.error if result else "正在读取…")
            )
        )
        self.times.set_text(
            f"开始：{_local_time(data.get('started_at'))}  ·  结束：{_local_time(data.get('finished_at'))}"
            if data
            else ""
        )
        self.input.show(_text(data.get("command"), ""))
        error = _object(data.get("error"))
        self.tabs.tab(self.diagnostics, text="诊断 · 有错误" if error else "诊断")
        diag = []
        if error:
            diag.append(f"{_text(error.get('code'))}\n{_text(error.get('message'))}")
        if data.get("commit"):
            diag.append(f"提交：{data['commit']}")
        if data.get("action_id"):
            diag.append(f"Action ID：{data['action_id']}")
        if data.get("output_source"):
            diag.append(f"输出来源：{data['output_source']}")
        self.diagnostics.show("\n\n".join(diag) or ("暂无诊断" if data else ""))
        for stream, start in view.offsets().items():
            total = data.get(f"{stream}_bytes")
            total = total if type(total) is int else 0
            end = max(start, min(total, start + LOG_PAGE))
            self.outputs[stream].show(_decode_log(data, stream, start))
            self.tabs.tab(
                self.outputs[stream].master,
                text=f"{stream}  {_size(data.get(f'{stream}_bytes'))}",
            )
            self.ranges[stream].set_text(
                (f"字节 {start:,}–{end:,} / {total:,}" if total else "暂无输出")
            )
            prev, nxt = self.buttons[stream]
            prev.state(["!disabled" if data and start else "disabled"])
            nxt.state(["!disabled" if end < total else "disabled"])
            if stream in self.save_buttons:
                self.save_buttons[stream].state(["!disabled" if data else "disabled"])


def _apply_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")
    row_height = tkfont.Font(root=root, family=FONT, size=10).metrics("linespace") + 10
    style.configure(
        "Treeview",
        background=PANEL,
        fieldbackground=PANEL,
        foreground=INK,
        borderwidth=0,
        rowheight=row_height,
        font=(FONT, 10),
    )
    style.map(
        "Treeview", background=[("selected", HOVER)], foreground=[("selected", INK)]
    )
    style.configure(
        "Treeview.Heading",
        background=RAISED,
        foreground=MUTED,
        bordercolor=RAISED,
        lightcolor=RAISED,
        darkcolor=RAISED,
        relief="flat",
        padding=(7, 5),
        font=(FONT, 9, "bold"),
    )
    style.map("Treeview.Heading", background=[("active", RAISED)])
    style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 3))
    style.configure(
        "TNotebook.Tab",
        background=HEADER,
        foreground=MUTED,
        bordercolor=BORDER,
        lightcolor=HEADER,
        darkcolor=HEADER,
        padding=(12, 5),
        font=(FONT, 9, "bold"),
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", RAISED), ("active", HOVER)],
        foreground=[("selected", TEAL), ("active", INK)],
        lightcolor=[("selected", RAISED)],
        darkcolor=[("selected", RAISED)],
    )
    style.configure(
        "TButton",
        background=RAISED,
        foreground=INK,
        bordercolor=BORDER,
        lightcolor=RAISED,
        darkcolor=RAISED,
        focuscolor=TEAL,
        padding=(8, 3),
        font=(FONT, 9, "bold"),
        relief="flat",
    )
    style.map(
        "TButton",
        background=[("disabled", HEADER), ("pressed", BORDER), ("active", HOVER)],
        foreground=[("disabled", MUTED)],
        lightcolor=[("active", HOVER)],
        darkcolor=[("active", HOVER)],
    )
    for direction in ("Vertical", "Horizontal"):
        name = f"{direction}.TScrollbar"
        style.configure(
            name,
            background=BORDER,
            troughcolor=INSET,
            arrowcolor=MUTED,
            bordercolor=INSET,
            lightcolor=BORDER,
            darkcolor=BORDER,
            arrowsize=11,
        )
        style.map(name, background=[("active", HOVER), ("pressed", HOVER)])
    for accent in METRIC_COLORS[:3]:
        style.configure(
            f"{accent}.Horizontal.TProgressbar",
            background=accent,
            troughcolor=INSET,
            lightcolor=accent,
            darkcolor=accent,
            bordercolor=INSET,
            thickness=5,
        )


@dataclass(frozen=True)
class OperationResult:
    key: tuple[str, ...]
    value: object = None
    error: str = ""


def _run_operation(
    work: Callable[[], object],
    key: tuple[str, ...],
    results: SimpleQueue[OperationResult],
    stop: threading.Event,
) -> None:
    if stop.is_set():
        return
    try:
        result = OperationResult(key, work())
    except APIError as exc:
        result = OperationResult(key, error=f"{exc.code}\n{exc}")
    except ValueError as exc:  # Path or form validation.
        result = OperationResult(key, error=str(exc))
    except OSError as exc:
        result = OperationResult(key, error=str(exc))
    except Exception:
        import logging

        logging.getLogger("winrunner").exception("Desktop operation failed: %s", key[0])
        result = OperationResult(key, error="内部错误，请查看服务日志。")
    if not stop.is_set():
        results.put(result)


class Dashboard:
    def __init__(
        self,
        root: tk.Tk,
        *,
        snapshot: Callable[[View], Snapshot],
        is_running: Callable[[], bool],
        desktop: DesktopRunner | None = None,
    ) -> None:
        self.root = root
        self.desktop = desktop
        self._operation_results: SimpleQueue[OperationResult] = SimpleQueue()
        self._operations: dict[tuple[str, ...], Callable[[OperationResult], None]] = {}
        self._operation_after: str | None = None
        self._exports: dict[tuple[str, ...], threading.Thread] = {}
        self._forms: dict[tuple[str, ...], SubmitForm] = {}
        self._browsers: dict[str, FileBrowser] = {}
        self._action_summary: JsonObject = {}
        self._selected_scope: str | None = None
        self._pending_selection: (
            tuple[View, str | None, str | None, str | None] | None
        ) = None
        self._refresh_requested = False
        self._flash: tuple[str, float] = ("", 0)
        self._stop = threading.Event()
        self._results: SimpleQueue[Snapshot] = SimpleQueue()
        self._after_id: str | None = None
        self._reopen_id: str | None = None
        self._busy = False
        self._backend_exited = False
        self._run_id = DASH
        self._is_running = is_running
        self._view = View()
        self._action_id: str | None = None
        self._detail_revision: int | None = None
        self._snapshot = Snapshot(Response())

        stop, results = self._stop, self._results

        def poll(view: View) -> None:
            _poll_snapshot(partial(snapshot, view), stop, results)

        self._poll = poll

        root.title("WinRunner · 控制台")
        # Heights set in pixels need extra room when Tk renders larger fonts.
        scale = float(root.tk.call("tk", "scaling"))
        available_width = max(1, root.winfo_screenwidth() - 24)
        available_height = max(1, root.winfo_screenheight() - 75)
        min_height = min(
            available_height, MIN_HEIGHT + round(210 * max(0, scale - 1.33))
        )
        root.minsize(min(available_width, MIN_WIDTH), min_height)
        root.geometry(
            f"{min(1120, available_width)}x{min(available_height, max(780, min_height))}"
        )
        root.configure(bg=BG)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        root.protocol("WM_DELETE_WINDOW", self._window_close)

        _apply_theme(root)

        hero = tk.Frame(root, bg=HEADER, padx=16, pady=6)
        hero.grid(row=0, column=0, sticky="ew")
        hero.columnconfigure(1, weight=1)
        mark = tk.Label(
            hero,
            text="WR",
            bg=TEAL,
            fg=BG,
            font=(MONO, 13, "bold"),
            padx=10,
            pady=7,
            borderwidth=0,
        )
        mark.grid(row=0, column=0, padx=(0, 11))
        heading = tk.Frame(hero, bg=HEADER)
        heading.grid(row=0, column=1, sticky="w")
        _label(heading, "WinRunner", bg=HEADER, size=16, bold=True).pack(side="left")
        _label(heading, "本机控制台", bg=HEADER, fg=MUTED, size=9).pack(
            side="left", padx=(10, 0)
        )
        self.connection = _label(
            hero, "● 正在读取", bg=RAISED, fg=AMBER, size=9, bold=True
        )
        self.connection.configure(padx=9, pady=4)
        self.connection.grid(row=0, column=2)

        cards = tk.Frame(root, bg=BG)
        cards.grid(row=1, column=0, sticky="ew", padx=14, pady=6)
        names = ("CPU 使用率", "物理内存", "工作区磁盘", "Runner 运行时长")
        metrics = [
            Metric(cards, name, accent, bar=i < 3)
            for i, (name, accent) in enumerate(zip(names, METRIC_COLORS))
        ]
        self.cpu, self.memory, self.disk, self.uptime = metrics
        for index, metric in enumerate(metrics):
            cards.columnconfigure(index, weight=1, uniform="metrics")
            metric.grid(
                row=0, column=index, sticky="nsew", padx=(0, 7 if index < 3 else 0)
            )

        workspace = tk.Frame(root, bg=BG)
        workspace.grid(row=2, column=0, sticky="nsew", padx=14)
        workspace.columnconfigure(0, weight=1)
        # Share the actual space with the detail, not just the extra space
        # left over after each widget's requested height.
        workspace.rowconfigure(0, weight=3, uniform="workspace")
        self.tabs = ttk.Notebook(workspace)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        projects = tk.Frame(self.tabs, bg=BG)
        self.tabs.add(projects, text="项目")
        self.projects = StatusTable(
            projects,
            "项目列表",
            (("name", "名称", 115, True), ("status", "状态", 76, False)),
        )
        projects.columnconfigure(1, weight=1)
        projects.rowconfigure(0, weight=1)
        self.projects.grid(row=0, column=0, sticky="ns", padx=(0, 8))
        self.projects.tree.bind("<<TreeviewSelect>>", self._select_project)
        self.controls: dict[str, ttk.Button] = {}
        if desktop is not None:
            project_tools = tk.Frame(projects, bg=PANEL, padx=8, pady=5)
            project_tools.grid(row=1, column=0, sticky="ew", padx=(0, 8))
            self._buttons(
                project_tools,
                ("create", "创建项目", self._create_project),
                ("delete_project", "删除项目", self._delete_project),
            )

        project = tk.Frame(projects, bg=PANEL)
        project.grid(row=0, column=1, rowspan=2, sticky="nsew")
        project.columnconfigure(0, weight=1)
        project.rowconfigure(2, weight=1)
        intro = tk.Frame(project, bg=PANEL, padx=12, pady=6)
        intro.grid(row=0, column=0, sticky="ew")
        intro.columnconfigure(0, weight=1)
        self.project_title = FitLabel(intro, "选择左侧项目", bold=True, size=11)
        self.project_title.grid(row=0, column=0, sticky="ew")
        self.repo = FitLabel(intro, "", fg=MUTED, size=9)
        self.repo.grid(row=1, column=0, sticky="ew")
        action = tk.Frame(project, bg=INSET, padx=8, pady=3)
        action.grid(row=1, column=0, padx=12, sticky="ew")
        action.columnconfigure(0, weight=1)
        self.action_button = ttk.Button(
            action, text="查看 Action", command=self._select_action
        )
        self.action_button.grid(row=0, column=1)
        self.action_text = FitLabel(action, "Action  ·  未选择项目", bg=INSET, size=9)
        self.action_text.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        self.project_commands = StatusTable(project, "项目命令", COMMAND_COLUMNS)
        self.project_commands.grid(row=2, column=0, sticky="nsew")

        self.commands = StatusTable(self.tabs, "全局命令", COMMAND_COLUMNS)
        self.tabs.add(self.commands, text="全局命令")
        if desktop is not None:
            project_tools = tk.Frame(intro, bg=PANEL)
            project_tools.grid(row=0, column=1, rowspan=2, sticky="e", padx=(6, 0))
            self._buttons(
                project_tools,
                ("files", "项目文件", self._browse_files),
                ("copy_repo", "复制仓库地址", self._copy_repo),
            )
            action_tools = tk.Frame(action, bg=INSET)
            action_tools.grid(row=0, column=2)
            self._buttons(
                action_tools,
                ("run_action", "运行 Action", self._start_action),
                ("cancel_action", "取消 Action", self._cancel_action),
            )
            for scope, table in (
                ("project", self.project_commands),
                ("global", self.commands),
            ):
                command_tools = tk.Frame(table.header, bg=PANEL)
                command_tools.grid(row=0, column=2, padx=(8, 0))
                self._buttons(
                    command_tools,
                    (f"{scope}_submit", "提交命令", self._start_command),
                    (f"{scope}_delete", "删除命令", self._delete_command),
                )
        self.project_commands.tree.bind("<<TreeviewSelect>>", self._select_command)
        self.commands.tree.bind("<<TreeviewSelect>>", self._select_command)
        self.tabs.bind("<<NotebookTabChanged>>", self._switch_tab)
        self.detail = TaskDetail(
            workspace,
            self._collapse,
            self._page,
            self._save_log if desktop is not None else None,
        )
        self.detail.grid(row=1, column=0, sticky="nsew", pady=(7, 0))
        self.detail.grid_remove()
        self.workspace = workspace
        self._update_project(None)

        footer = tk.Frame(root, bg=BG, padx=14, pady=6)
        footer.grid(row=3, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.notice = FitLabel(footer, "正在读取状态…", bg=BG, fg=MUTED, size=8)
        self.notice.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        _label(footer, "Ctrl+C 停止服务", bg=BG, fg=MUTED, size=8).grid(
            row=0, column=1, padx=(0, 12)
        )
        self.run = FitLabel(footer, "", bg=BG, fg=MUTED, size=8, family=MONO)
        self.run.configure(anchor="e")
        footer.columnconfigure(2, weight=1)
        self.run.grid(row=0, column=2, sticky="ew")
        self._after_id = root.after_idle(self.refresh)

    def _buttons(
        self, parent: tk.Frame, *items: tuple[str, str, Callable[[], None]]
    ) -> None:
        for key, label, callback in items:
            button = self.controls[key] = ttk.Button(
                parent, text=label, command=callback
            )
            button.pack(side="left", padx=(0, 5))

    def _project_selection(self) -> ProjectSelection | None:
        name = self._view.project
        if name is None or self._global() or self._selected(self.projects) != name:
            return None
        return (
            ProjectSelection(name, self._selected_scope)
            if self._selected_scope
            else None
        )

    def _update_controls(self) -> None:
        if not self.controls:
            return
        project = self._project_selection()
        name = project.name if project else ""
        status = _find(self._snapshot.projects, "projects", name).get("status")
        action_status = self._action_summary.get("status")
        project_id = self._selected(self.project_commands)
        global_id = self._selected(self.commands)
        enabled = {
            "create": True,
            "delete_project": project is not None
            and status != "deleting"
            and ("delete_project", name) not in self._operations,
            "run_action": project is not None
            and status not in ("cloning", "deleting", "delete_failed"),
            "cancel_action": project is not None
            and action_status in ("preparing", "running")
            and bool(self._action_summary.get("action_id"))
            and ("cancel_action", name) not in self._operations,
            "files": project is not None
            and status not in ("cloning", "deleting", "delete_failed"),
            "copy_repo": project is not None,
            "project_submit": project is not None and status != "deleting",
            "project_delete": project is not None
            and project_id is not None
            and ("delete_command", name, project_id) not in self._operations,
            "global_submit": True,
            "global_delete": global_id is not None
            and ("delete_command", "", global_id) not in self._operations,
        }
        for key, button in self.controls.items():
            button.state(
                [
                    (
                        "!disabled"
                        if enabled[key] and not self._backend_exited
                        else "disabled"
                    )
                ]
            )

    def _operate(
        self,
        key: tuple[str, ...],
        work: Callable[[], object],
        done: Callable[[OperationResult], None],
    ) -> bool:
        if key in self._operations or self._stop.is_set() or not self._check_backend():
            return False
        self._operations[key] = done
        worker = threading.Thread(
            target=_run_operation,
            args=(work, key, self._operation_results, self._stop),
            daemon=True,
            name=f"winrunner-desktop-{key[0]}",
        )
        try:
            worker.start()
        except RuntimeError:
            self._operations.pop(key)
            messagebox.showerror("操作未启动", "无法启动后台操作。", parent=self.root)
            return False
        if key[0] == "save_log":
            self._exports[key] = worker
        if self._operation_after is None:
            self._operation_after = self.root.after(50, self._receive_operations)
        self._update_controls()
        return True

    def _receive_operations(self) -> None:
        self._operation_after = None
        if self._stop.is_set() or not self._check_backend():
            return
        while True:
            try:
                result = self._operation_results.get_nowait()
            except Empty:
                break
            done = self._operations.pop(result.key)
            self._exports.pop(result.key, None)
            self._refresh_requested = True
            self._schedule(0)
            self._update_controls()
            try:
                done(result)
            except Exception as exc:
                self.root.report_callback_exception(type(exc), exc, exc.__traceback__)
            if self._stop.is_set():
                return
        if self._operations and self._operation_after is None:
            self._operation_after = self.root.after(50, self._receive_operations)

    def _report(self, message: str) -> None:
        self._flash = (message, time.monotonic() + 8)
        self.notice.set_text(message)

    def _complete(self, result: OperationResult, success: str) -> None:
        if result.error:
            messagebox.showerror("操作未完成", result.error, parent=self.root)
        else:
            self._report(success)

    def _form(
        self,
        key: tuple[str, ...],
        kind: FormKind,
        title: str,
        description: str,
        work: Callable[[JsonObject, str], Callable[[], object]],
        success: Callable[[object], None],
        confirm: str = "",
    ) -> None:
        from winrunner_gui_forms import SubmitForm

        previous = self._forms.get(key)
        if previous is not None and previous.winfo_exists():
            previous.lift()
            return

        def receive(result: OperationResult) -> None:
            value = result.value
            error = result.error
            if isinstance(value, Submission) and value.status >= 400:
                info = _object(value.data.get("error"))
                error = f"{_text(info.get('code'))}\n{_text(info.get('message'))}"
            shown = bool(dialog.winfo_exists())
            if error:
                if shown:
                    dialog.working(False)
                messagebox.showerror(
                    "操作未完成", error, parent=dialog if shown else self.root
                )
            else:
                if shown:
                    dialog.destroy()
                success(value)

        def submit(values: JsonObject, replay_key: str) -> None:
            if confirm and not messagebox.askyesno(
                title, confirm, icon="warning", default="no", parent=dialog
            ):
                return
            # A closed form can be reopened for another request while the old
            # one still waits. Keep the two completions bound to their forms.
            operation = (*key, str(dialog))
            if self._operate(operation, work(values, replay_key), receive):
                dialog.working(True, "后台处理中；关闭此窗不会取消任务。")

        dialog = self._forms[key] = SubmitForm(
            self.root, kind, title, description, submit
        )

        def forget(event: tk.Event) -> None:
            if event.widget is dialog and self._forms.get(key) is dialog:
                del self._forms[key]

        dialog.bind("<Destroy>", forget, add="+")

    def _create_project(self) -> None:
        desktop = self.desktop
        if desktop is None:
            return
        origin = self._view

        def work(values: JsonObject, _key: str) -> Callable[[], object]:
            return partial(
                desktop.create, str(values["name"]), {"repo_url": values["repo_url"]}
            )

        def created(value: object) -> None:
            name = _text(_object(value).get("name"))
            self._report(f"项目 {name} 已创建")
            if self._view is origin and not self._global():
                scope = _text(_object(value).get("scope"), "")
                if scope:
                    self._pending_selection = (origin, name, None, scope)

        self._form(
            ("create",),
            "project",
            "创建项目",
            "从公开 HTTPS Git 仓库克隆并检出默认分支。",
            work,
            created,
        )

    def _delete_project(self) -> None:
        desktop, project = self.desktop, self._project_selection()
        if desktop is None or project is None:
            return
        if messagebox.askyesno(
            "删除项目",
            f"删除项目 {project.name}？\n\n这会停止该项目的克隆、Action 和命令，"
            "中断下载，并删除工作树、Git 缓存、记录和日志。",
            icon="warning",
            default="no",
            parent=self.root,
        ):
            self._operate(
                ("delete_project", project.name),
                partial(desktop.delete_project, project),
                lambda result: self._complete(result, f"项目 {project.name} 已删除"),
            )

    def _submitted(
        self,
        value: object,
        project: str | None,
        *,
        action: bool,
        origin: View,
    ) -> None:
        if not isinstance(value, Submission):
            return
        identifier = _text(value.data.get("id"), "")
        title = (
            f"{project} / Action"
            if action
            else f"{project or '全局'} / 命令 #{identifier}"
        )
        self._report(f"{title} · {_status(value.data)}")
        if self._view is origin and self._global() == (project is None):
            action_id = _text(value.data.get("action_id"), "") if action else None
            self._pending_selection = (
                origin,
                project,
                "" if action else identifier,
                action_id,
            )

    def _start_action(self) -> None:
        desktop, project = self.desktop, self._project_selection()
        if desktop is None or project is None:
            return
        origin = self._view
        self._form(
            ("start_action", project.scope),
            "action",
            f"运行 Action · {project.name}",
            "从远端可达的指定提交检出工作树，再用白名单中的解释器运行脚本。",
            lambda values, key: partial(desktop.start_action, project, values, key),
            partial(self._submitted, project=project.name, action=True, origin=origin),
            "新一轮 Action 会替换上一轮日志和记录并重置工作树；旧文件和下载将不可用。"
            "正在运行的项目命令不会被停止。继续吗？",
        )

    def _cancel_action(self) -> None:
        desktop, project = self.desktop, self._project_selection()
        action_id = self._action_summary.get("action_id")
        if desktop is None or project is None or not isinstance(action_id, str):
            return
        if messagebox.askyesno(
            "取消 Action",
            f"停止 {project.name} 的当前 Action？\n已保留的日志和可用文件不会被删除。",
            icon="warning",
            default="no",
            parent=self.root,
        ):
            self._operate(
                ("cancel_action", project.name),
                partial(desktop.cancel_action, project, action_id),
                lambda result: self._complete(
                    result, f"{project.name} 的 Action 已停止"
                ),
            )

    def _start_command(self) -> None:
        desktop, project = self.desktop, self._project_selection()
        if desktop is None or (not self._global() and project is None):
            return
        name = project.name if project else None
        origin = self._view
        self._form(
            ("start_command", project.scope if project else ""),
            "command",
            f"提交命令 · {name or '全局'}",
            "在当前项目工作树中运行。" if project else "从 Runner 账号的用户目录运行。",
            lambda values, key: partial(desktop.start_command, project, values, key),
            partial(self._submitted, project=name, action=False, origin=origin),
        )

    def _delete_command(self) -> None:
        desktop = self.desktop
        global_tab = self._global()
        project = self._project_selection()
        command = self._selected(self.commands if global_tab else self.project_commands)
        if desktop is None or command is None or (not global_tab and project is None):
            return
        name = project.name if project else None
        if messagebox.askyesno(
            "删除命令",
            f"删除 {name or '全局'}的命令 #{command}？\n\n"
            "这会停止仍在运行的进程，删除该命令的记录和日志，不撤销它对文件的改动。",
            icon="warning",
            default="no",
            parent=self.root,
        ):
            self._operate(
                ("delete_command", name or "", command),
                partial(desktop.delete_command, name, command),
                lambda result: self._complete(
                    result, f"{name or '全局'} / 命令 #{command} 已删除"
                ),
            )

    def _copy_repo(self) -> None:
        project = self._project_selection()
        if project is not None:
            url = _find(self._snapshot.projects, "projects", project.name).get(
                "repo_url"
            )
            if isinstance(url, str) and url:
                self.root.clipboard_clear()
                self.root.clipboard_append(url)
                self._report("已复制仓库地址")

    def _browse_files(self) -> None:
        from winrunner_gui_files import FileBrowser

        desktop, project = self.desktop, self._project_selection()
        if desktop is None or project is None:
            return
        previous = self._browsers.get(project.scope)
        if previous is not None and previous.winfo_exists():
            previous.lift()
            previous.focus_set()
            return
        browser = self._browsers[project.scope] = FileBrowser(
            self.root,
            project.name,
            lambda _name, relative, offset, cancel: desktop.browse(
                project, relative, offset, cancel
            ),
            lambda _name, relative, path, bounds, cancel: desktop.save_file(
                project, relative, path, bounds, cancel
            ),
        )

        def forget(event: tk.Event) -> None:
            if event.widget is browser and self._browsers.get(project.scope) is browser:
                del self._browsers[project.scope]

        browser.bind("<Destroy>", forget, add="+")

    def _save_log(self, stream: str) -> None:
        desktop, target, project = (
            self.desktop,
            self._view.target,
            self._project_selection(),
        )
        if (
            desktop is None
            or target is None
            or self._detail_revision != self._view.revision
        ):
            return
        action_id = self._action_id if target.command is None else None
        if target.project and (not project or project.name != target.project):
            return
        if target.command is None and action_id is None:
            return
        key = (
            "save_log",
            target.project or "",
            target.command or action_id or "",
            stream,
        )
        if key in self._operations:
            return
        try:
            destination = filedialog.asksaveasfilename(
                parent=self.root,
                title=f"保存原始 {stream}",
                initialfile=f"{target.project or 'global'}-{target.command or 'action'}-{stream}.log",
            )
        except tk.TclError:
            if not self._stop.is_set():
                messagebox.showerror(
                    "无法保存日志", "无法打开文件保存窗口。", parent=self.root
                )
            return
        if destination and self._operate(
            key,
            partial(
                desktop.save_log,
                project,
                target.command,
                action_id,
                stream,
                Path(destination),
                self._stop,
            ),
            lambda result: self._complete(
                result, f"已保存 {stream} · {_size(result.value)}"
            ),
        ):
            self._report(f"正在保存 {stream}…")

    @staticmethod
    def _selected(table: StatusTable) -> str | None:
        items = table.tree.selection()
        return items[0] if items else None

    def _global(self) -> bool:
        return self.tabs.select() == str(self.commands)

    def _select_project(self, _event: tk.Event) -> None:
        if not self._global():
            self._switch_tab()

    def _switch_tab(self, _event: tk.Event | None = None) -> None:
        project = None if self._global() else self._selected(self.projects)
        if self._view.project != project:
            self._clear_command_selection()
            self._set_view(project, None)
            self._update_project(None)
        elif (
            self._view.target is not None
            and (self._view.target.project is None) != self._global()
        ):
            self._collapse()
        self._update_controls()

    def _select_action(self) -> None:
        if self._view.project is not None:
            self._clear_command_selection()
            self._set_view(self._view.project, Target(self._view.project))

    def _select_command(self, event: tk.Event) -> None:
        table = self.commands if self._global() else self.project_commands
        if event.widget is not table.tree:
            return
        key = self._selected(table)
        if key is not None:
            self._set_view(self._view.project, Target(self._view.project, key))
        elif self._view.target is not None and self._view.target.command is not None:
            self._collapse()
        self._update_controls()

    def _clear_command_selection(self) -> None:
        for table in (self.commands, self.project_commands):
            selection = table.tree.selection()
            if selection:
                table.tree.selection_remove(*selection)

    def _collapse(self) -> None:
        self._clear_command_selection()
        self._set_view(self._view.project, None)

    def _set_view(self, project: str | None, target: Target | None) -> None:
        old = self._view
        if (old.project, old.target) == (project, target):
            return
        old.superseded.set()
        self._view = View(project, target, revision=old.revision + 1)
        if project != old.project:
            record = (
                _find(self._snapshot.projects, "projects", project) if project else {}
            )
            self._selected_scope = _text(record.get("scope"), "") or None
        self._action_id = None
        if target is None:
            self.detail.show(View(), None)
            self.detail.grid_remove()
            self.workspace.rowconfigure(1, weight=0, uniform="")
        else:
            self.detail.open(target, keep_tab=old.target is not None)
            self.detail.grid()
            self.workspace.rowconfigure(1, weight=2, uniform="workspace")
        self._schedule(0)

    def _page(self, stream: str, direction: int) -> None:
        view = self._view
        if view.target is None or self._detail_revision != view.revision:
            return
        offsets = view.offsets()
        offsets[stream] = max(0, offsets[stream] + direction * LOG_PAGE)
        view.superseded.set()
        self._view = View(
            view.project,
            view.target,
            offsets["stdout"],
            offsets["stderr"],
            view.revision + 1,
        )
        self._schedule(0)

    def _window_close(self) -> None:
        if not self._is_running():
            self.close()
        elif self._reopen_id is None:
            self.root.withdraw()
            self._reopen_id = self.root.after(REOPEN_MS, self._reopen)

    def _reopen(self) -> None:
        self._reopen_id = None
        if not self._stop.is_set() and self._check_backend():
            self.root.deiconify()

    def _check_backend(self) -> bool:
        if self._backend_exited:
            return False
        if self._is_running():
            return True
        self._backend_exited = True
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        self.connection.configure(text="● 已停止", fg=AMBER)
        self._after_id = self.root.after(BACKEND_EXIT_MS, self.close)
        return False

    def _schedule(self, delay: int) -> None:
        if self._busy or self._stop.is_set() or not self._check_backend():
            return
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        self._after_id = self.root.after(delay, self.refresh)

    def refresh(self) -> None:
        self._after_id = None
        if self._stop.is_set() or not self._check_backend() or self._busy:
            return
        self._busy = True
        self._refresh_requested = False
        self._poll_revision = self._view.revision
        threading.Thread(
            target=self._poll,
            args=(self._view,),
            daemon=True,
            name="winrunner-dashboard-poll",
        ).start()
        self._after_id = self.root.after(50, self._receive)

    def _receive(self) -> None:
        self._after_id = None
        if self._stop.is_set() or not self._check_backend():
            return
        try:
            snapshot = self._results.get_nowait()
        except Empty:
            self._after_id = self.root.after(50, self._receive)
            return
        try:
            self._show(snapshot)
        finally:
            self._busy = False
            self._schedule(
                0
                if self._refresh_requested or self._poll_revision != self._view.revision
                else INTEGRATED_REFRESH_MS
            )

    def _update_project(self, data: ViewData | None) -> None:
        name = self._view.project
        project = _find(self._snapshot.projects, "projects", name) if name else {}
        self.project_title.set_text(
            f"{name}  ·  {_status(project)}" if name else "选择左侧项目"
        )
        self.repo.set_text(_text(project.get("repo_url"), ""))
        action = data.action if data else None
        task = action.data if action and action.data else {}
        self._action_summary = task
        if task:
            message = f"{_status(task)}  ·  {_phase(task)}"
            code = _object(task.get("error")).get("code")
            if isinstance(code, str):
                message += f"  ·  {code}"
        else:
            message = (
                "未选择项目"
                if not name
                else (
                    "正在读取…"
                    if action is None
                    else "尚未提交" if action.status == 404 else action.error
                )
            )
        self.action_text.set_text(f"Action  ·  {message}")
        tag = STATUSES.get(_text(task.get("status")), ("", "neutral"))[1]
        self.action_text.configure(fg=STATUS_COLORS.get(tag, MUTED))
        self.action_button.state(["!disabled" if task else "disabled"])
        self.project_commands.show_rows(
            *(
                _rows(data.commands, "commands")
                if data
                else ([], "正在读取项目…" if name else "选择项目后显示")
            ),
            reset=data is None,
        )

    def _show(self, snapshot: Snapshot) -> None:
        name = self._view.project
        if (
            name
            and self._selected_scope
            and snapshot.projects
            and snapshot.projects.data is not None
        ):
            new_scope = _find(snapshot.projects, "projects", name).get("scope")
            if self._selected_scope != new_scope:
                # A same-name replacement must not inherit the old selection.
                self.projects.tree.selection_remove(name)
                self._clear_command_selection()
                self._set_view(None, None)
                self._update_project(None)
        self._snapshot = snapshot
        health = snapshot.health.data
        if health is None:
            self.connection.configure(text="● 状态不可用", fg=AMBER)
            self.notice.set_text(snapshot.health.error)
            self.cpu.update_value(*_cpu_usage({}))
            for metric in (self.memory, self.disk, self.uptime):
                metric.update_value(DASH, "等待 Runner 状态")
            run_id = DASH
        else:
            cpu = _object(health.get("cpu"))
            cores = _number(cpu.get("logical_cores"))
            self.cpu.title.set_text(
                f"CPU 使用率 · {cores:g} 核" if cores else "CPU 使用率"
            )
            self.cpu.update_value(*_cpu_usage(cpu))
            self.memory.update_value(*_capacity(_object(health.get("memory"))))
            self.disk.update_value(*_capacity(_object(health.get("disk"))))
            self.uptime.update_value(
                _uptime(health.get("uptime_seconds")), "当前服务进程"
            )
            run_id = _text(health.get("run_id"))
            self.connection.configure(text="● 服务运行中", fg=GREEN)
            self.notice.set_text(
                self._flash[0]
                if self._flash[1] > time.monotonic()
                else snapshot.note or f"自动刷新  ·  {datetime.now():%H:%M:%S}"
            )
        self.run.set_text(f"Runner ID  {run_id}")
        reset = run_id != DASH and self._run_id != run_id
        if reset and self._run_id != DASH:
            self._collapse()
            self._set_view(None, None)
        if run_id != DASH:
            self._run_id = run_id
        self.projects.show_rows(*_rows(snapshot.projects, "projects"), reset=reset)
        self.commands.show_rows(*_rows(snapshot.commands, "commands"), reset=reset)
        view = self._view
        selected = snapshot.selected
        if selected is not None and selected.view == view:
            self._update_project(selected)
            if view.target is not None:
                result = selected.detail
                action_id = (
                    _text(result.data.get("action_id"), "")
                    if result and result.data and view.target.command is None
                    else ""
                )
                replaced = action_id and self._action_id not in (None, action_id)
                if action_id:
                    self._action_id = action_id
                if (replaced or (result and result.status == 400)) and (
                    view.stdout or view.stderr
                ):
                    # The Action slot was replaced; never use old byte offsets.
                    self._view = View(
                        view.project, view.target, revision=view.revision + 1
                    )
                    self.detail.show(self._view, None)
                else:
                    self.detail.show(view, result)
                    self._detail_revision = (
                        view.revision if result and result.data else None
                    )
        elif reset:
            self._update_project(None)
        self._select_result(selected)
        self._update_controls()

    def _select_result(self, selected: ViewData | None) -> None:
        if self._pending_selection is None:
            return
        origin, project, command, generation = self._pending_selection
        if self._view is not origin or self._global() != (project is None):
            self._pending_selection = None
            return
        key: str | None
        if command is None and project is not None:
            record = _find(self._snapshot.projects, "projects", project)
            if record.get("scope") != generation:
                if not self._refresh_requested:
                    self._pending_selection = None
                return
            table, key = self.projects, project
        elif command == "":
            if selected is not None and selected.view == self._view:
                task = selected.action.data if selected.action else None
                if task and task.get("action_id") == generation:
                    self._pending_selection = None
                    self._select_action()
                elif not self._refresh_requested:
                    self._pending_selection = None
            return
        else:
            table = self.commands if project is None else self.project_commands
            key = command
        if key is not None and table.tree.exists(key):
            self._pending_selection = None
            table.tree.selection_set(key)
            table.tree.see(key)
        elif not self._refresh_requested and (
            project is None or selected and selected.view == self._view
        ):
            # A replay can reference a command or project that no longer exists.
            self._pending_selection = None

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        for after_id in (self._after_id, self._reopen_id, self._operation_after):
            if after_id is not None:
                self.root.after_cancel(after_id)
        self.root.destroy()
        # Let cancelling exports discard their partial files without waiting on
        # task submissions, which can still be waiting for Runner.shutdown.
        deadline = time.monotonic() + 1
        for worker in self._exports.values():
            worker.join(max(0, deadline - time.monotonic()))


def run_integrated(
    snapshot: Callable[[View], Snapshot],
    is_running: Callable[[], bool],
    *,
    desktop: DesktopRunner | None = None,
) -> None:
    """Keep Tk on the main thread and the HTTP server alive across window closes."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("The integrated dashboard must run on the main thread")
    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        # A signal can arrive during a Tk call; only change a flag here.
        stopping = True

    previous = signal.signal(signal.SIGINT, request_stop)
    try:
        root = tk.Tk()
        try:
            dashboard = Dashboard(
                root,
                snapshot=snapshot,
                is_running=lambda: not stopping and is_running(),
                desktop=desktop,
            )
        except BaseException:
            root.destroy()
            raise
        try:
            root.mainloop()
        finally:
            if not dashboard._stop.is_set():
                dashboard.close()
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(
        "Start the desktop console with: python winrunner.py (without --nogui)"
    )
