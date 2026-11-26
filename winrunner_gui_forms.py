"""Desktop forms for creating projects and submitting tasks."""

from __future__ import annotations

import json
import math
import re
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import Literal

import config
from winrunner_gui import (
    BG,
    BORDER,
    FONT,
    INK,
    INSET,
    MONO,
    MUTED,
    RED,
    TEAL,
    FitLabel,
    _label,
)

FormKind = Literal["project", "action", "command"]


class SubmitForm(tk.Toplevel):
    def __init__(
        self,
        parent: tk.Tk,
        kind: FormKind,
        title: str,
        description: str,
        submit: Callable[[dict[str, object], str], None],
    ) -> None:
        super().__init__(parent)
        self.kind = kind
        self.submit = submit
        self.busy = False
        self.title(title)
        self.transient(parent)
        self.configure(bg=BG)
        available_width = max(1, self.winfo_screenwidth() - 24)
        self.minsize(min(530, available_width), 1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self._fields: dict[str, tk.StringVar] = {}
        self._editors: list[ttk.Entry | ttk.Combobox | tk.Text] = []
        first: ttk.Entry | ttk.Combobox | tk.Text

        head = tk.Frame(self, bg=BG, padx=14, pady=10)
        head.grid(row=0, column=0, sticky="ew")
        _label(head, title, bg=BG, size=13, bold=True).pack(fill="x")
        _label(head, description, bg=BG, fg=MUTED, size=9, wraplength=415).pack(
            fill="x"
        )
        form = tk.Frame(self, bg=BG, padx=14)
        form.grid(row=1, column=0, sticky="nsew")
        form.columnconfigure(1, weight=1)

        if kind == "project":
            first = self._field(form, 0, "name", "项目名")
            self._field(form, 1, "repo_url", "仓库 HTTPS 地址")
            first.bind("<Return>", self._submit)
        elif kind == "action":
            first = self._field(form, 0, "commit", "提交哈希或前缀")
            self._field(
                form, 1, "interpreter", "解释器", choices=tuple(config.INTERPRETERS)
            )
            self._field(form, 2, "script", "脚本相对路径")
            self._field(form, 3, "args", "参数（JSON 数组）")
            _label(
                form,
                '例如 ["--name", "两个词"]；留空不传该字段。',
                bg=BG,
                fg=MUTED,
                size=8,
            ).grid(row=4, column=1, sticky="w")
        else:
            self._field(form, 0, "mode", "Shell", choices=("PowerShell", "CMD"))
            _label(form, "命令正文", bg=BG).grid(
                row=1, column=0, columnspan=2, sticky="w"
            )
            command_frame = tk.Frame(form, bg=BG)
            command_frame.grid(
                row=2, column=0, columnspan=2, sticky="nsew", pady=(0, 6)
            )
            form.rowconfigure(2, weight=1)
            command_frame.columnconfigure(0, weight=1)
            command_frame.rowconfigure(0, weight=1)
            self.command_text = tk.Text(
                command_frame,
                width=48,
                height=8,
                wrap="char",
                undo=True,
                font=(MONO, 10),
                bg=INSET,
                fg=INK,
                insertbackground=INK,
                padx=8,
                pady=5,
                relief="flat",
                highlightthickness=1,
                highlightbackground=BORDER,
                highlightcolor=TEAL,
            )
            self.command_text.grid(row=0, column=0, sticky="nsew")
            scrollbar = ttk.Scrollbar(
                command_frame, orient="vertical", command=self.command_text.yview
            )
            scrollbar.grid(row=0, column=1, sticky="ns")
            self.command_text.configure(yscrollcommand=scrollbar.set)
            self.command_text.bind("<Control-Return>", self._submit)
            self._editors.append(self.command_text)
            first = self.command_text

        if kind != "project":
            self.options = tk.Frame(self, bg=BG, padx=14)
            self.options.grid(row=3, column=0, sticky="ew")
            self.options.columnconfigure(1, weight=1)
            self._field(self.options, 0, "timeout", "等待响应（秒）", default="0")
            self._field(self.options, 1, "key", "幂等键（可选）")
            _label(
                self.options,
                "0 表示立即返回；留空使用服务默认值。",
                bg=BG,
                fg=MUTED,
                size=8,
            ).grid(row=2, column=1, sticky="w")
            self.options.grid_remove()
            self.options_button = ttk.Button(
                self, text="进阶选项 ▸", command=self._toggle_options
            )
            self.options_button.grid(row=2, column=0, sticky="w", padx=14, pady=(5, 0))
            self._expanded = False

        footer = tk.Frame(self, bg=BG, padx=14, pady=10)
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        self.feedback = FitLabel(footer, "", bg=BG, size=9, fg=RED)
        self.feedback.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.submit_button = ttk.Button(
            footer, text="创建" if kind == "project" else "提交", command=self._submit
        )
        self.submit_button.grid(row=0, column=1, padx=(0, 5))
        ttk.Button(footer, text="关闭", command=self.destroy).grid(row=0, column=2)
        self.bind("<Escape>", lambda _: self.destroy())
        self.bind("<Control-Return>", self._submit)
        first.focus_set()

    def _field(
        self,
        parent: tk.Frame,
        row: int,
        key: str,
        title: str,
        *,
        default: str = "",
        choices: tuple[str, ...] | None = None,
    ) -> ttk.Entry | ttk.Combobox:
        _label(parent, title, bg=BG).grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=4
        )
        var = self._fields[key] = tk.StringVar(
            self, value=choices[0] if choices else default
        )
        editor: ttk.Entry | ttk.Combobox
        if choices is None:
            editor = ttk.Entry(parent, textvariable=var, width=36, font=(FONT, 10))
        else:
            editor = ttk.Combobox(
                parent, textvariable=var, values=choices, state="readonly", width=34
            )
        editor.grid(row=row, column=1, sticky="ew", pady=4)
        self._editors.append(editor)
        return editor

    def _toggle_options(self) -> None:
        self._expanded = not self._expanded
        if self._expanded:
            self.options.grid()
        else:
            self.options.grid_remove()
        self.options_button.configure(
            text="进阶选项 ▾" if self._expanded else "进阶选项 ▸"
        )

    def values(self) -> tuple[dict[str, object], str]:
        values = {name: value.get() for name, value in self._fields.items()}
        if self.kind == "project":
            name, repo = values["name"].strip(), values["repo_url"].strip()
            if not name or not repo:
                raise ValueError("请填写项目名和仓库地址。")
            return {"name": name, "repo_url": repo}, ""
        key = values["key"].strip()
        if key and not re.fullmatch(r"[A-Za-z0-9._~-]{1,128}", key):
            raise ValueError("幂等键须为 1–128 个 ASCII 字母、数字或 ._~-")
        result: dict[str, object] = {}
        if values["timeout"].strip():
            try:
                timeout = json.loads(values["timeout"])
                if (
                    type(timeout) not in (int, float)
                    or not math.isfinite(timeout)
                    or timeout < 0
                ):
                    raise ValueError
            except (ValueError, OverflowError, RecursionError):
                raise ValueError("等待时间须为非负有限秒数。") from None
            result["timeout"] = timeout
        if self.kind == "command":
            result["mode"] = "cmd" if values["mode"] == "CMD" else "ps"
            result["command"] = self.command_text.get("1.0", "end-1c")
            if not str(result["command"]).strip():
                raise ValueError("请输入命令。")
        else:
            result.update(
                commit=values["commit"].strip(),
                interpreter=values["interpreter"],
                script=values["script"],
            )
            try:
                args = json.loads(values["args"].strip() or "[]")
                if not isinstance(args, list) or not all(
                    isinstance(arg, str) for arg in args
                ):
                    raise ValueError
            except (ValueError, RecursionError):
                raise ValueError("参数须是 JSON 字符串数组。") from None
            if values["args"].strip():
                result["args"] = args
        return result, key

    def _submit(self, _event: tk.Event | None = None) -> str:
        if not self.busy:
            try:
                values, key = self.values()
            except ValueError as exc:
                self.feedback.set_text(str(exc))
            else:
                self.submit(values, key)
        return "break"

    def working(self, busy: bool, message: str = "") -> None:
        self.busy = busy
        for editor in self._editors:
            if isinstance(editor, ttk.Combobox):
                editor.configure(state="disabled" if busy else "readonly")
            else:
                editor.configure(state="disabled" if busy else "normal")
        self.submit_button.state(["disabled" if busy else "!disabled"])
        self.feedback.configure(fg=MUTED if busy else RED)
        self.feedback.set_text(message)
