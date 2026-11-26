"""Local desktop operations on the same Runner and replay cache as HTTP."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING

import config
from runner import APIError, Runner, action_body, command_body, invalid
from winfiles import BadPath, normalized_relative

if TYPE_CHECKING:
    from winrunner import StartReplay


@dataclass(frozen=True)
class ProjectSelection:
    name: str
    scope: str


@dataclass(frozen=True)
class Submission:
    status: int
    data: dict[str, object]


def _body(values: dict[str, object]) -> bytes:
    raw = json.dumps(values, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    encoded = raw.encode("utf-8")
    if len(encoded) > config.MAX_REQUEST_BYTES:
        raise APIError(413, "body_too_large", "Request body is too large")
    return encoded


class DesktopRunner:
    def __init__(self, runner: Runner, replay: StartReplay) -> None:
        self.runner = runner
        self.replay = replay

    def create(self, name: str, values: dict[str, object]) -> dict:
        raw = _body(values)
        return self.runner.create(name, raw, include_scope=True)

    def delete_project(self, project: ProjectSelection) -> None:
        self.runner.delete_project(project.name, expected_scope=project.scope)

    def start_action(
        self, project: ProjectSelection, values: dict[str, object], key: str
    ) -> Submission:
        raw = _body(values)

        def validate() -> None:
            # Fresh form mistakes must not reset an existing Action's worktree.
            _, _, script, _, _ = action_body(raw)
            try:
                if not normalized_relative(script):
                    raise BadPath("script must be a nonempty relative path")
            except BadPath as exc:
                raise invalid(str(exc)) from exc

        def start() -> tuple[int, dict]:
            return self.runner.start_action(
                project.name, raw, accepted_at, expected_scope=project.scope
            )

        accepted_at = time.monotonic()
        if not key:
            validate()
            return Submission(*start())
        return Submission(
            *self.replay.submit(
                f"action:{project.name.lower()}",
                key,
                raw,
                start,
                log_errors=True,
                validate_new=validate,
            )
        )

    def cancel_action(self, project: ProjectSelection, action_id: str) -> None:
        self.runner.cancel_action(
            project.name, expected_scope=project.scope, expected_action=action_id
        )

    def start_command(
        self, project: ProjectSelection | None, values: dict[str, object], key: str
    ) -> Submission:
        raw = _body(values)
        selected = (
            self.runner.command_project(project.name, expected_scope=project.scope)
            if project
            else None
        )

        def validate() -> None:
            command_body(raw)

        def start() -> tuple[int, dict]:
            return self.runner.start_command(raw, accepted_at, project=selected)

        accepted_at = time.monotonic()
        if not key:
            validate()
            return Submission(*start())
        return Submission(
            *self.replay.submit(
                selected.command_scope if selected else "command",
                key,
                raw,
                start,
                log_errors=True,
                validate_new=validate,
            )
        )

    def delete_command(self, project: str | None, command: str) -> None:
        # Command IDs are never reused within a project name in this Runner run.
        self.runner.delete_command(command, name=project)

    def browse(
        self, project: ProjectSelection, relative: str, offset: int, cancelled: Event
    ) -> dict:
        from winrunner_files import browse

        return browse(
            self.runner,
            project.name,
            relative,
            offset,
            cancelled,
            expected_scope=project.scope,
        )

    def save_file(
        self,
        project: ProjectSelection,
        relative: str,
        destination: Path,
        range_header: str | None,
        cancelled: Event,
    ) -> int:
        from winrunner_files import save_file

        return save_file(
            self.runner,
            project.name,
            relative,
            destination,
            range_header,
            cancelled,
            expected_scope=project.scope,
        )

    def save_log(
        self,
        project: ProjectSelection | None,
        command: str | None,
        action_id: str | None,
        stream: str,
        destination: Path,
        cancelled: Event,
    ) -> int:
        from winrunner_files import save_bytes

        def get(offsets: dict[str, int] | None = None) -> dict:
            if command is not None:
                return self.runner.get_command(
                    command, offsets, name=project.name if project else None
                )
            assert project is not None and action_id is not None
            return self.runner.get_action(
                project.name,
                offsets,
                expected_scope=project.scope,
                expected_action=action_id,
            )

        size = get()[f"{stream}_bytes"]

        def chunks() -> Iterator[bytes]:
            offset = 0
            while offset < size and not cancelled.is_set():
                piece = base64.b64decode(get({stream: offset})[stream]["data_base64"])
                if not piece:
                    raise APIError(
                        409, "log_changed", "The task log is no longer available"
                    )
                piece = piece[: size - offset]
                offset += len(piece)
                yield piece

        return save_bytes(self.runner, destination, chunks(), cancelled)
