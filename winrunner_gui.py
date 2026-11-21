"""Read-only local WinRunner dashboard. Run beside config.py with Python 3.10+."""

from __future__ import annotations

import http.client
import json
import math
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from queue import Empty, SimpleQueue
from socket import SHUT_RDWR
from tkinter import ttk

HOST = "127.0.0.1"
ROUTES = ("/health", "/projects", "/commands")
REQUEST_SECONDS = 2.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
REFRESH_MS = 5000
INTEGRATED_REFRESH_MS = 850
BACKEND_EXIT_MS = 850

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
PHASES = {"launch": "启动", "run": "执行"}

JsonObject = dict[str, object]
Row = tuple[str, tuple[str, ...], str]
Column = tuple[str, str, int, bool]


@dataclass(frozen=True)
class Response:
    data: JsonObject | None = None
    error: str = ""


@dataclass(frozen=True)
class Snapshot:
    health: Response
    projects: Response | None = None
    commands: Response | None = None
    note: str = ""


def _get(port: int, token: str, route: str) -> Response:
    if route not in ROUTES:
        raise ValueError("Not a dashboard route")
    # No URL opener, proxy discovery, or redirect handling. Each request gets its
    # own connection; a socket deadline also bounds a response that trickles in.
    conn = http.client.HTTPConnection(HOST, port, timeout=REQUEST_SECONDS)
    deadline = time.monotonic() + REQUEST_SECONDS
    timer: threading.Timer | None = None
    try:
        conn.connect()
        sock = conn.sock
        if sock is None:
            return Response(error="无法连接本机 Runner")

        def expire() -> None:
            try:
                # Hold the socket even if HTTPConnection has handed it to the response.
                sock.shutdown(SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(max(0, deadline - time.monotonic()), expire)
        timer.daemon = True
        timer.start()
        conn.request(
            "GET",
            route,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "Connection": "close",
            },
        )
        with conn.getresponse() as reply:
            if reply.status == 401:
                return Response(error="认证失败，请检查本地配置")
            if reply.status == 503:
                return Response(error="Runner 忙或正在退出，请稍后重试")
            if reply.status != 200:
                return Response(error=f"请求未成功（HTTP {reply.status}）")
            body = reply.read(MAX_RESPONSE_BYTES + 1)
        if time.monotonic() >= deadline:
            return Response(error="本机 Runner 响应超时")
        if len(body) > MAX_RESPONSE_BYTES:
            return Response(error="Runner 响应过大")
        data = json.loads(body)
        if isinstance(data, dict):
            return Response(data=data)
        return Response(error="Runner 返回了无效数据")
    except ConnectionRefusedError:
        return Response(error="无法连接本机 Runner")
    except TimeoutError:
        return Response(error="本机 Runner 响应超时")
    except (http.client.HTTPException, OSError):
        if time.monotonic() >= deadline:
            return Response(error="本机 Runner 响应超时")
        return Response(error="与本机 Runner 通信失败")
    except (ValueError, RecursionError):
        return Response(error="Runner 返回了无效数据，或本地配置无效")
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


def _poll(
    port: int, token: str, stop: threading.Event, results: SimpleQueue[Snapshot]
) -> None:
    health = _get(port, token, "/health")
    if stop.is_set():
        return
    if health.data is None:
        results.put(Snapshot(health))
        return
    projects = _get(port, token, "/projects")
    if stop.is_set():
        return
    commands = _get(port, token, "/commands")
    if stop.is_set():
        return
    # Confirm both lists came from the same Runner generation. A restart can
    # reuse command IDs, so never show lists that straddle it.
    confirmed = _get(port, token, "/health")
    if stop.is_set():
        return
    if confirmed.data is None:
        results.put(Snapshot(confirmed))
    elif health.data.get("run_id") != confirmed.data.get("run_id"):
        unavailable = Response(error="Runner 已重启，等待刷新")
        results.put(Snapshot(confirmed, unavailable, unavailable, unavailable.error))
    else:
        results.put(Snapshot(health, projects, commands))


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
        return [], "等待 Runner 连接"
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
        if not key or key in seen:
            return [], "Runner 返回了无效列表"
        seen.add(key)
        status = _text(entry.get("status"))
        label, tag = STATUSES.get(status, (status, "neutral"))
        values: tuple[str, ...]
        if collection == "projects":
            values = (key, label, _text(entry.get("repo_url")))
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


class Dashboard:
    def __init__(
        self,
        root: tk.Tk,
        port: int | None = None,
        token: str | None = None,
        *,
        snapshot: Callable[[], Snapshot] | None = None,
        on_close: Callable[[], None] | None = None,
        is_running: Callable[[], bool] | None = None,
    ) -> None:
        self.root = root
        self._stop = threading.Event()
        self._results: SimpleQueue[Snapshot] = SimpleQueue()
        self._after_id: str | None = None
        self._busy = False
        self._backend_exited = False
        self._run_id = DASH
        self._on_close = on_close
        self._is_running = is_running

        if snapshot is None:
            if port is None or token is None:
                raise TypeError("Standalone dashboard requires a port and token")
            self._poll = partial(_poll, port, token, self._stop, self._results)
            self._refresh_ms = REFRESH_MS
            subtitle = f"只读仪表盘  ·  {HOST}:{port}"
            initial_notice = "正在向本机 Runner 请求状态…"
        else:
            self._poll = partial(_poll_snapshot, snapshot, self._stop, self._results)
            self._refresh_ms = INTEGRATED_REFRESH_MS
            subtitle = "只读仪表盘  ·  内置监控"
            initial_notice = "正在读取本机 Runner 状态…"

        root.title("WinRunner · 本机监控")
        root.geometry("1020x710")
        root.minsize(860, 580)
        root.configure(bg=BG)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<F5>", lambda _event: self.refresh())

        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(
            "Treeview",
            background=WHITE,
            fieldbackground=WHITE,
            foreground=INK,
            borderwidth=0,
            rowheight=28,
            font=(FONT, 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#e9f0f8",
            foreground=INK,
            relief="flat",
            padding=(6, 5),
            font=(FONT, 9, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", "#dbeafe")],
            foreground=[("selected", INK)],
        )
        style.configure(
            "Refresh.TButton",
            padding=(15, 7),
            font=(FONT, 10, "bold"),
            background=ACCENT,
            foreground=WHITE,
            bordercolor=ACCENT,
        )
        style.map(
            "Refresh.TButton",
            background=[("disabled", "#375472"), ("active", "#1d56ba")],
            foreground=[("disabled", "#dce7f4")],
        )
        style.configure(
            "Health.Horizontal.TProgressbar",
            background=ACCENT,
            troughcolor="#e3edf9",
            borderwidth=0,
            thickness=6,
        )

        hero = tk.Frame(root, bg=NAVY, padx=24, pady=16)
        hero.grid(row=0, column=0, sticky="ew")
        heading = tk.Frame(hero, bg=NAVY)
        heading.pack(side="left")
        _label(
            heading, "WinRunner  本机监控", bg=NAVY, fg=WHITE, size=21, bold=True
        ).pack(fill="x")
        _label(heading, subtitle, bg=NAVY, fg="#a9c0da", size=9).pack(fill="x")
        self.refresh_button = ttk.Button(
            hero, text="立即刷新  F5", style="Refresh.TButton", command=self.refresh
        )
        self.refresh_button.pack(side="right")
        self.connection = _label(hero, "● 正在连接", bg=NAVY, fg="#e7c07b", bold=True)
        self.connection.pack(side="right", padx=(0, 16))

        content = tk.Frame(root, bg=BG, padx=22, pady=12)
        content.grid(row=1, column=0, sticky="nsew")
        content.columnconfigure(0, weight=1)
        content.rowconfigure(3, weight=2, minsize=110)
        content.rowconfigure(4, weight=3, minsize=125)
        _label(content, "主机概览", bg=BG, size=13, bold=True).grid(
            row=0, column=0, sticky="w", pady=(0, 7)
        )
        cards = tk.Frame(content, bg=BG)
        cards.grid(row=1, column=0, sticky="ew")
        names = ("CPU 使用率", "物理内存", "工作区磁盘", "Runner 运行时长")
        metrics = [Metric(cards, name, bar=i < 3) for i, name in enumerate(names)]
        self.cpu, self.memory, self.disk, self.uptime = metrics
        for index, metric in enumerate(metrics):
            cards.columnconfigure(index, weight=1, uniform="metrics")
            metric.grid(
                row=0, column=index, sticky="nsew", padx=(0, 9 if index < 3 else 0)
            )
        self.run = _label(content, f"Runner ID  {DASH}", bg=BG, fg=MUTED, size=9)
        self.run.grid(row=2, column=0, sticky="ew", pady=(4, 8))

        self.projects = StatusTable(
            content,
            "项目 / Projects",
            (
                ("name", "项目名称", 160, True),
                ("status", "状态", 105, False),
                ("repository", "仓库地址", 300, True),
            ),
        )
        self.projects.grid(row=3, column=0, sticky="nsew", pady=(0, 9))
        self.commands = StatusTable(
            content,
            "临时命令 / Commands",
            (
                ("id", "命令 ID", 100, True),
                ("mode", "模式", 105, True),
                ("status", "状态", 105, True),
                ("phase", "阶段", 75, False),
                ("exit", "退出码", 75, False),
                ("stdout", "标准输出", 90, False),
                ("stderr", "标准错误", 90, False),
            ),
        )
        self.commands.grid(row=4, column=0, sticky="nsew")

        footer = tk.Frame(root, bg="#e2eaf4", padx=22, pady=6)
        footer.grid(row=2, column=0, sticky="ew")
        self.notice = _label(footer, initial_notice, bg="#e2eaf4", fg=MUTED, size=9)
        self.notice.pack(side="left")
        _label(footer, "自动刷新  ·  只读取状态", bg="#e2eaf4", fg=MUTED, size=9).pack(
            side="right"
        )
        self._after_id = root.after_idle(self.refresh)

    def _check_backend(self) -> bool:
        if self._backend_exited:
            return False
        if self._is_running is None or self._is_running():
            return True
        self._backend_exited = True
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        self.refresh_button.state(["disabled"])
        offline = Response(error="本机 Runner 已停止")
        self._show(Snapshot(offline, offline, offline))
        self._after_id = self.root.after(BACKEND_EXIT_MS, self.close)
        return False

    def refresh(self) -> None:
        if self._stop.is_set() or not self._check_backend() or self._busy:
            return
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
        self._busy = True
        self.refresh_button.state(["disabled"])
        threading.Thread(
            target=self._poll,
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
        self.refresh_button.state(["!disabled"])
        self._after_id = self.root.after(self._refresh_ms, self.refresh)

    def _show(self, snapshot: Snapshot) -> None:
        health = snapshot.health.data
        if health is None:
            status = "已停止" if self._backend_exited else "未连接"
            self.connection.configure(text=f"● {status}", fg="#f5c386")
            self.notice.configure(text=snapshot.health.error)
            self.cpu.title.configure(text="CPU 使用率")
            self.cpu.update_value(*_cpu_usage({}))
            detail = "Runner 已停止" if self._backend_exited else "等待 Runner 连接"
            for metric in (self.memory, self.disk, self.uptime):
                metric.update_value(DASH, detail)
            run_id = DASH
        else:
            cpu = _object(health.get("cpu"))
            cores = _number(cpu.get("logical_cores"))
            title = f"CPU 使用率 · {cores:g} 核" if cores else "CPU 使用率"
            self.cpu.title.configure(text=title)
            self.cpu.update_value(*_cpu_usage(cpu))
            self.memory.update_value(*_capacity(_object(health.get("memory"))))
            self.disk.update_value(*_capacity(_object(health.get("disk"))))
            self.uptime.update_value(
                _uptime(health.get("uptime_seconds")), "当前服务进程"
            )
            run_id = _text(health.get("run_id"))
            self.connection.configure(text="● 已连接", fg="#82e0b9")
            self.notice.configure(
                text=snapshot.note or f"上次刷新  {datetime.now():%H:%M:%S}"
            )
        self.run.configure(text=f"Runner ID  {run_id}")
        reset = self._run_id != run_id
        self._run_id = run_id
        self.projects.show_rows(*_rows(snapshot.projects, "projects"), reset=reset)
        self.commands.show_rows(*_rows(snapshot.commands, "commands"), reset=reset)

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self._after_id is not None:
            self.root.after_cancel(self._after_id)
            self._after_id = None
        try:
            if not self._backend_exited and self._on_close is not None:
                self._on_close()
        finally:
            self.root.destroy()


def run_integrated(
    snapshot: Callable[[], Snapshot],
    on_close: Callable[[], None],
    is_running: Callable[[], bool],
) -> None:
    """Run Tk on the main thread; only snapshot collection runs in a worker.

    The liveness check and user-close notification run on the Tk thread and
    must not block. Backend exit closes the dashboard without a notification.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("The integrated dashboard must run on the main thread")
    root = tk.Tk()
    Dashboard(root, snapshot=snapshot, on_close=on_close, is_running=is_running)
    root.mainloop()


def main() -> None:
    # Import the adjacent settings without generating a new config bytecode file.
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        from config import PORT, TOKEN
    finally:
        sys.dont_write_bytecode = previous
    root = tk.Tk()
    Dashboard(root, PORT, TOKEN)
    root.mainloop()


if __name__ == "__main__":
    main()
