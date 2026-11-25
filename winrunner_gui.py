"""Read-only local WinRunner dashboard. Run beside config.py with Python 3.10+."""

from __future__ import annotations

import base64
import codecs
import math
import re
import signal
import threading
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from queue import Empty, SimpleQueue
from tkinter import ttk
from typing import Literal

INTEGRATED_REFRESH_MS = 850
REOPEN_MS = 450
BACKEND_EXIT_MS = 850
LOG_PAGE = 32768
LOG_OVERLAP = 3  # Longest UTF-8 scalar minus one byte.
LOG_READ = LOG_PAGE + 2 * LOG_OVERLAP
STREAMS = ("stdout", "stderr")

BG = "#eef3f8"
WHITE = "#ffffff"
INK = "#15253a"
MUTED = "#52647a"
NAVY = "#12253c"
ACCENT = "#2466d8"
GREEN = "#166348"
AMBER = "#885110"
RED = "#aa323e"
FONT = "Microsoft YaHei UI"
DASH = "—"

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
    ("mode", "Shell", 100, True),
    ("status", "状态", 87, True),
    ("phase", "阶段", 65, True),
    ("exit", "退出码", 62, False),
    ("stdout", "stdout", 80, True),
    ("stderr", "stderr", 80, True),
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
    bg: str = WHITE,
    fg: str = INK,
    size: int = 10,
    bold: bool = False,
) -> tk.Label:
    return tk.Label(
        parent,
        text=text,
        bg=bg,
        fg=fg,
        anchor="w",
        font=(FONT, size, "bold" if bold else "normal"),
    )


class Metric(tk.Frame):
    def __init__(self, parent: tk.Misc, title: str, bar: bool = True) -> None:
        super().__init__(parent, bg=WHITE, padx=15, pady=10)
        self.title = _label(self, title, fg=MUTED, bold=True)
        self.title.pack(fill="x")
        self.value = _label(self, DASH, size=21, bold=True)
        self.value.pack(fill="x", pady=(3, 0))
        self.detail = _label(self, DASH, fg=MUTED, size=9)
        self.detail.pack(fill="x", pady=(0, 8))
        self.bar = ttk.Progressbar(
            self, style="Health.Horizontal.TProgressbar", maximum=100
        )
        if bar:
            self.bar.pack(fill="x")

    def update_value(self, value: str, detail: str, percent: float = 0) -> None:
        self.value.configure(text=value)
        self.detail.configure(text=detail)
        self.bar.configure(value=percent)


class StatusTable(tk.Frame):
    def __init__(
        self, parent: tk.Misc, title: str, columns: tuple[Column, ...]
    ) -> None:
        super().__init__(parent, bg=WHITE, padx=14, pady=10)
        header = tk.Frame(self, bg=WHITE)
        header.pack(fill="x", pady=(0, 7))
        _label(header, title, size=12, bold=True).pack(side="left")
        self.summary = _label(header, "等待刷新…", fg=MUTED, size=9)
        self.summary.pack(side="right")
        body = tk.Frame(self, bg=WHITE)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            body,
            columns=[column[0] for column in columns],
            show="headings",
            selectmode="browse",
            height=3,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        for key, label, width, stretch in columns:
            self.tree.heading(key, text=label, anchor="w")
            self.tree.column(
                key, width=width, minwidth=width, stretch=stretch, anchor="w"
            )
        for tag, color in (
            ("active", ACCENT),
            ("success", GREEN),
            ("warning", AMBER),
            ("error", RED),
        ):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.tag_configure("stripe", background="#f4f7fb")
        self.empty_message = _label(self.tree, "等待刷新…", fg=MUTED)
        self.empty_message.place(relx=0.5, rely=0.62, anchor="center")
        self._rendered: list[Row] = []

    def show_rows(
        self, rows: list[Row], error: str = "", *, reset: bool = False
    ) -> None:
        if error and not reset and self._rendered:
            self.summary.configure(
                text=f"暂不可用 · 保留上次 {len(self._rendered)} 条", fg=AMBER
            )
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
        self.summary.configure(
            text="不可用" if error else f"{len(rows)} 条记录",
            fg=AMBER if error else MUTED,
        )
        if rows:
            self.empty_message.place_forget()
        else:
            self.empty_message.configure(text=error or "暂无记录")
            self.empty_message.place(relx=0.5, rely=0.62, anchor="center")


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
        super().__init__(parent, bg=WHITE)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.text = tk.Text(
            self,
            height=5,
            width=1,
            wrap=wrap,
            state="disabled",
            bg="#f5f8fc",
            fg=INK,
            relief="flat",
            padx=9,
            pady=6,
            font=("Consolas", 10),
            selectbackground="#c4ddff",
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
    ):
        super().__init__(parent, bg=WHITE, padx=14, pady=10)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        header = tk.Frame(self, bg=WHITE)
        header.grid(row=0, column=0, sticky="ew")
        self.title = _label(header, "任务详情", bold=True, size=12)
        self.title.pack(side="left", fill="x", expand=True)
        ttk.Button(header, text="收起详情", command=close).pack(side="right")
        self.meta = _label(self, "", fg=MUTED, size=9)
        self.meta.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self.tabs = ttk.Notebook(self)
        self.tabs.grid(row=2, column=0, sticky="nsew")
        self.input = ReadOnlyText(self.tabs, wrap="char")
        self.tabs.add(self.input, text="命令原文")
        self.outputs: dict[str, ReadOnlyText] = {}
        self.ranges: dict[str, tk.Label] = {}
        self.buttons: dict[str, tuple[ttk.Button, ttk.Button]] = {}
        for stream in STREAMS:
            tab = tk.Frame(self.tabs, bg=WHITE, padx=6, pady=5)
            self.tabs.add(tab, text=stream)
            nav = tk.Frame(tab, bg=WHITE)
            nav.pack(fill="x", pady=(0, 4))
            self.ranges[stream] = _label(nav, "", fg=MUTED, size=9)
            self.ranges[stream].pack(side="left")
            prev = ttk.Button(nav, text="上一页", command=partial(page, stream, -1))
            nxt = ttk.Button(nav, text="下一页", command=partial(page, stream, 1))
            nxt.pack(side="right")
            prev.pack(side="right", padx=4)
            self.buttons[stream] = (prev, nxt)
            self.outputs[stream] = ReadOnlyText(tab)
            self.outputs[stream].pack(fill="both", expand=True)
        self.diagnostics = ReadOnlyText(self.tabs, wrap="word")
        self.tabs.add(self.diagnostics, text="诊断")

    def open(self, target: Target) -> None:
        scope = target.project or "全局"
        title = (
            f"{scope}  /  Action"
            if target.command is None
            else f"{scope}  /  命令 #{target.command}"
        )
        self.title.configure(text=title)
        if target.command is None:
            self.tabs.hide(self.input)
            self.tabs.select(self.outputs["stdout"].master)
        else:
            self.tabs.add(self.input, text="命令原文")
            self.tabs.select(self.input)
        self.show(View(), None)

    def show(self, view: View, result: Response | None) -> None:
        data = result.data if result and result.data else {}
        exit_code = data.get("exit_code")
        mode = {"cmd": "CMD", "ps": "PowerShell"}.get(_text(data.get("mode")))
        meta = f"{_status(data)}  ·  {_phase(data)}  ·  退出码 {exit_code if type(exit_code) is int else DASH}"
        self.meta.configure(
            text=(
                (f"{mode}  ·  " if mode else "") + meta
                if data
                else (result.error if result else "正在读取…")
            )
        )
        self.input.show(_text(data.get("command"), ""))
        error = _object(data.get("error"))
        self.tabs.tab(self.diagnostics, text="诊断 · 有错误" if error else "诊断")
        diag = []
        if error:
            diag.append(f"{_text(error.get('code'))}\n{_text(error.get('message'))}")
        if data.get("commit"):
            diag.append(f"提交：{data['commit']}")
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
            self.ranges[stream].configure(
                text=(f"字节 {start:,}–{end:,} / {total:,}" if total else "暂无输出")
            )
            prev, nxt = self.buttons[stream]
            prev.state(["!disabled" if data and start else "disabled"])
            nxt.state(["!disabled" if end < total else "disabled"])


class Dashboard:
    def __init__(
        self,
        root: tk.Tk,
        *,
        snapshot: Callable[[View], Snapshot],
        is_running: Callable[[], bool],
    ) -> None:
        self.root = root
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

        root.title("WinRunner · 本机监控")
        root.geometry("1120x780")
        root.minsize(870, 640)
        root.configure(bg=BG)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)
        root.protocol("WM_DELETE_WINDOW", self._window_close)

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=WHITE,
            fieldbackground=WHITE,
            foreground=INK,
            borderwidth=0,
            rowheight=27,
            font=(FONT, 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#e9f0f8",
            foreground=INK,
            relief="flat",
            padding=(5, 4),
            font=(FONT, 9, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", "#dbeafe")],
            foreground=[("selected", INK)],
        )
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(12, 5), font=(FONT, 9))
        style.map("TNotebook.Tab", background=[("selected", WHITE)])
        style.configure("TButton", padding=(7, 3), font=(FONT, 9))
        style.configure(
            "Health.Horizontal.TProgressbar",
            background=ACCENT,
            troughcolor="#e3edf9",
            borderwidth=0,
            thickness=4,
        )

        hero = tk.Frame(root, bg=NAVY, padx=19, pady=11)
        hero.grid(row=0, column=0, sticky="ew")
        heading = tk.Frame(hero, bg=NAVY)
        heading.pack(side="left")
        _label(heading, "WinRunner", bg=NAVY, fg=WHITE, size=18, bold=True).pack(
            fill="x"
        )
        _label(
            heading,
            "内置监控  ·  在控制台按 Ctrl+C 停止服务",
            bg=NAVY,
            fg="#a9c0da",
            size=9,
        ).pack(fill="x")
        self.connection = _label(hero, "● 正在读取", bg=NAVY, fg="#e7c07b", bold=True)
        self.connection.pack(side="right")

        cards = tk.Frame(root, bg=BG)
        cards.grid(row=1, column=0, sticky="ew", padx=14, pady=9)
        names = ("CPU 使用率", "物理内存", "工作区磁盘", "Runner 运行时长")
        metrics = [Metric(cards, name, bar=i < 3) for i, name in enumerate(names)]
        self.cpu, self.memory, self.disk, self.uptime = metrics
        for index, metric in enumerate(metrics):
            cards.columnconfigure(index, weight=1, uniform="metrics")
            metric.grid(
                row=0, column=index, sticky="nsew", padx=(0, 7 if index < 3 else 0)
            )

        workspace = tk.Frame(root, bg=BG)
        workspace.grid(row=2, column=0, sticky="nsew", padx=14)
        workspace.columnconfigure(0, weight=1)
        workspace.rowconfigure(0, weight=3)
        self.tabs = ttk.Notebook(workspace)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        projects = tk.Frame(self.tabs, bg=BG)
        self.tabs.add(projects, text="项目")
        self.projects = StatusTable(
            projects,
            "项目列表",
            (("name", "名称", 115, True), ("status", "状态", 76, False)),
        )
        self.projects.pack(side="left", fill="y", padx=(0, 7))
        self.projects.tree.bind("<<TreeviewSelect>>", self._select_project)

        project = tk.Frame(projects, bg=WHITE)
        project.pack(side="left", fill="both", expand=True)
        intro = tk.Frame(project, bg=WHITE, padx=14, pady=8)
        intro.pack(fill="x")
        self.project_title = _label(intro, "选择左侧项目", bold=True, size=12)
        self.project_title.pack(fill="x")
        self.repo = _label(intro, "", fg=MUTED, size=9)
        self.repo.pack(fill="x")
        # Keep long repository URLs from pushing task columns off-screen.
        self.repo.configure(width=1)
        action = tk.Frame(project, bg="#eef4fc", padx=9, pady=5)
        action.pack(fill="x", padx=14)
        self.action_button = ttk.Button(
            action, text="查看 Action", command=self._select_action
        )
        self.action_button.pack(side="right")
        self.action_text = _label(action, "Action  ·  未选择项目", bg="#eef4fc", size=9)
        self.action_text.pack(side="left", fill="x", expand=True)
        self.project_commands = StatusTable(project, "项目命令", COMMAND_COLUMNS)
        self.project_commands.pack(fill="both", expand=True)

        self.commands = StatusTable(self.tabs, "全局命令", COMMAND_COLUMNS)
        self.tabs.add(self.commands, text="全局命令")
        self.project_commands.tree.bind("<<TreeviewSelect>>", self._select_command)
        self.commands.tree.bind("<<TreeviewSelect>>", self._select_command)
        self.tabs.bind("<<NotebookTabChanged>>", self._switch_tab)
        self.detail = TaskDetail(workspace, self._collapse, self._page)
        self.detail.grid(row=1, column=0, sticky="nsew", pady=(7, 0))
        self.detail.grid_remove()
        self.workspace = workspace
        self._update_project(None)

        footer = tk.Frame(root, bg=BG, padx=14, pady=5)
        footer.grid(row=3, column=0, sticky="ew")
        self.notice = _label(footer, "正在读取状态…", bg=BG, fg=MUTED, size=9)
        self.notice.pack(side="left")
        self.run = _label(footer, "", bg=BG, fg=MUTED, size=9)
        self.run.pack(side="right")
        self._after_id = root.after_idle(self.refresh)

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
        self._action_id = None
        if target is None:
            self.detail.show(View(), None)
            self.detail.grid_remove()
            self.workspace.rowconfigure(1, weight=0)
        else:
            self.detail.open(target)
            self.detail.grid()
            self.workspace.rowconfigure(1, weight=2)
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
        self.connection.configure(text="● 已停止", fg="#f5c386")
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
        self._show(snapshot)
        self._busy = False
        self._schedule(
            INTEGRATED_REFRESH_MS if self._poll_revision == self._view.revision else 0
        )

    def _update_project(self, data: ViewData | None) -> None:
        name = self._view.project
        project = _find(self._snapshot.projects, "projects", name) if name else {}
        self.project_title.configure(
            text=f"{name}  ·  {_status(project)}" if name else "选择左侧项目"
        )
        self.repo.configure(text=_text(project.get("repo_url"), ""))
        action = data.action if data else None
        task = action.data if action and action.data else {}
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
        self.action_text.configure(text=f"Action  ·  {message}")
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
        self._snapshot = snapshot
        health = snapshot.health.data
        if health is None:
            self.connection.configure(text="● 状态不可用", fg="#f5c386")
            self.notice.configure(text=snapshot.health.error)
            self.cpu.update_value(*_cpu_usage({}))
            for metric in (self.memory, self.disk, self.uptime):
                metric.update_value(DASH, "等待 Runner 状态")
            run_id = DASH
        else:
            cpu = _object(health.get("cpu"))
            cores = _number(cpu.get("logical_cores"))
            self.cpu.title.configure(
                text=f"CPU 使用率 · {cores:g} 核" if cores else "CPU 使用率"
            )
            self.cpu.update_value(*_cpu_usage(cpu))
            self.memory.update_value(*_capacity(_object(health.get("memory"))))
            self.disk.update_value(*_capacity(_object(health.get("disk"))))
            self.uptime.update_value(
                _uptime(health.get("uptime_seconds")), "当前服务进程"
            )
            run_id = _text(health.get("run_id"))
            self.connection.configure(text="● 服务运行中", fg="#82e0b9")
            self.notice.configure(
                text=snapshot.note or f"自动刷新  ·  {datetime.now():%H:%M:%S}"
            )
        self.run.configure(text=f"只读  ·  Runner ID  {run_id}")
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

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        for after_id in (self._after_id, self._reopen_id):
            if after_id is not None:
                self.root.after_cancel(after_id)
        self.root.destroy()


def run_integrated(
    snapshot: Callable[[View], Snapshot], is_running: Callable[[], bool]
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
        "Start local monitoring with: python winrunner.py (without --nogui)"
    )
