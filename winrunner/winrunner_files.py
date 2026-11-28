"""Shared project file responses and local, atomic GUI exports."""

import ctypes
import json
import os
import re
import secrets
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from pathlib import Path
from threading import Event
from typing import BinaryIO

from . import winfiles
from .runner import APIError, Runner, invalid, not_found

_BYTE_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)\Z", re.I)
_PAGE_LIMIT = 512 * 1024
_CHUNK_SIZE = 64 * 1024


def range_bounds(header: str | None, size: int) -> tuple[int, int]:
    """Return the start and exclusive end of a single HTTP-style byte range."""
    if header is None:
        return 0, size
    match = _BYTE_RANGE.fullmatch(header.strip())
    if match and size:
        a, b = match.groups()
        try:
            if a:
                start = int(a)
                end = min(int(b) + 1, size) if b else size
                if 0 <= start < end:
                    return start, end
            elif b and int(b):
                return max(0, size - int(b)), size
        except ValueError:
            pass
    raise APIError(416, "range_not_satisfiable", "Expected one satisfiable byte range")


def directory_listing(
    entry: winfiles._PinnedEntry,
    relative: str,
    *,
    offset: int | None = None,
    limit: int = 100,
) -> dict:
    items = sorted(
        entry.list_entries(), key=lambda item: (item["name"].casefold(), item["name"])
    )
    path = winfiles.normalized_relative(relative)
    if offset is None:
        return {"path": path, "entries": items}

    page: list[dict] = []
    result: dict = {"path": path, "entries": page, "next_entry_offset": None}
    # Match HTTP reply(): ASCII escaping, commas, and the cursor count on the wire.
    body_bytes = len(json.dumps(result, separators=(",", ":"), ensure_ascii=True))
    if body_bytes > _PAGE_LIMIT:
        raise APIError(
            413, "response_too_large", "Directory path exceeds the page size limit"
        )
    for index, item in enumerate(items[offset : offset + limit], offset + 1):
        item_bytes = len(json.dumps(item, separators=(",", ":"), ensure_ascii=True)) + (
            1 if page else 0
        )
        cursor = index if index < len(items) else None
        cursor_delta = len(json.dumps(cursor)) - len("null")
        if body_bytes + item_bytes + cursor_delta > _PAGE_LIMIT:
            if not page:
                raise APIError(
                    413,
                    "response_too_large",
                    "Directory entry exceeds the page size limit",
                )
            break
        page.append(item)
        body_bytes += item_bytes
        result["next_entry_offset"] = cursor
    return result


def _check_cancelled(cancelled: Event) -> None:
    if cancelled.is_set():
        raise APIError(409, "cancelled", "File operation cancelled")


@contextmanager
def _project_io(cancelled: Event) -> Iterator[None]:
    """Map project reads like the HTTP file route, with useful GUI diagnostics."""
    try:
        yield
    except (winfiles.BadPath, OSError) as exc:
        _check_cancelled(cancelled)
        if isinstance(exc, winfiles.BadPath):
            raise invalid(str(exc)) from exc
        if getattr(exc, "winerror", None) in (32, 33):
            raise APIError(409, "file_busy", "File or directory is in use") from exc
        if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
            raise not_found("File or directory not found") from exc
        raise APIError(
            500, "file_read_failed", f"Could not read project file: {exc}"
        ) from exc


def browse(
    runner: Runner,
    name: str,
    relative: str,
    offset: int,
    cancelled: Event,
    *,
    expected_scope: str | None = None,
) -> dict:
    _check_cancelled(cancelled)
    if type(offset) is not int or offset < 0:
        raise invalid("entry_offset must be a non-negative integer")
    with (
        _project_io(cancelled),
        runner.file_entry(
            name, relative, cancel_read=cancelled.set, expected_scope=expected_scope
        ) as entry,
    ):
        _check_cancelled(cancelled)
        if entry.is_dir:
            result = directory_listing(entry, relative, offset=offset)
        else:
            with entry.open_file() as file:
                result = {
                    "path": winfiles.normalized_relative(relative),
                    "size_bytes": os.fstat(file.fileno()).st_size,
                }
    _check_cancelled(cancelled)
    return result


def save_file(
    runner: Runner,
    name: str,
    relative: str,
    destination: Path,
    range_header: str | None,
    cancelled: Event,
    *,
    expected_scope: str | None = None,
) -> int:
    _check_cancelled(cancelled)
    with (
        _project_io(cancelled),
        runner.file_entry(
            name, relative, cancel_read=cancelled.set, expected_scope=expected_scope
        ) as entry,
    ):
        _check_cancelled(cancelled)
        if entry.is_dir:
            raise invalid("Select a file to save, not a directory")
        with entry.open_file() as file:
            start, stop = range_bounds(range_header, os.fstat(file.fileno()).st_size)
            file.seek(start)
            return save_bytes(
                runner,
                destination,
                _read_chunks(file, stop - start, cancelled),
                cancelled,
            )


def _read_chunks(file: BinaryIO, remaining: int, cancelled: Event) -> Iterator[bytes]:
    while remaining:
        chunk = file.read(min(remaining, _CHUNK_SIZE))
        _check_cancelled(cancelled)
        if not chunk:
            raise APIError(
                500,
                "file_read_failed",
                "Project file ended before the requested bytes were read",
            )
        remaining -= len(chunk)
        yield chunk


if os.name == "nt":
    import msvcrt

    _set_file_info = winfiles._kernel32.SetFileInformationByHandle
    _set_file_info.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _set_file_info.restype = wintypes.BOOL

    _nt_set_file_info = winfiles._ntdll.NtSetInformationFile
    _nt_set_file_info.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(winfiles._IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        ctypes.c_int,
    )
    _nt_set_file_info.restype = wintypes.LONG

    _final_path = winfiles._kernel32.GetFinalPathNameByHandleW
    _final_path.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    _final_path.restype = wintypes.DWORD

_FILE_WRITE_DATA = 0x0002
_DELETE = 0x10000
_FILE_CREATE = 2
_FILE_RENAME_INFORMATION = 10  # Native information class
_FILE_DISPOSITION_INFO = 4  # Win32 information class


class _RenameInfo(ctypes.Structure):
    _fields_ = [
        ("replace", wintypes.BOOL),
        ("parent", wintypes.HANDLE),
        ("name_bytes", wintypes.DWORD),
        ("name", wintypes.WCHAR),
    ]


def _rename(file: BinaryIO, parent: int, destination: Path) -> None:
    name = destination.name.encode("utf-16-le", errors="surrogatepass")
    # The final component is a directory entry to replace, not a link to follow.
    info = _RenameInfo(True, parent, len(name))
    ctypes.resize(info, ctypes.sizeof(info) + len(name))
    ctypes.memmove(ctypes.addressof(info) + _RenameInfo.name.offset, name, len(name))
    status = _nt_set_file_info(
        msvcrt.get_osfhandle(file.fileno()),
        ctypes.byref(winfiles._IoStatusBlock()),
        ctypes.byref(info),
        ctypes.sizeof(info),
        _FILE_RENAME_INFORMATION,
    )
    if status < 0:
        raise winfiles._winerror(destination, winfiles._ntstatus_to_error(status))


def _open_temp(parent: int, destination: Path) -> int:
    # Create a new name below the pinned directory object rather than traversing
    # the path again. Keep the file private until it is renamed or discarded.
    while True:
        name = f".winrunner-{secrets.token_hex(16)}.tmp"
        name_bytes = name.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(name_bytes + b"\0\0")
        nt_name = winfiles._UnicodeString(
            len(name_bytes), len(buffer), ctypes.addressof(buffer)
        )
        obj = winfiles._ObjectAttributes(
            ctypes.sizeof(winfiles._ObjectAttributes),
            parent,
            ctypes.pointer(nt_name),
            winfiles._OBJ_CASE_INSENSITIVE | winfiles._OBJ_DONT_REPARSE,
            None,
            None,
        )
        handle = wintypes.HANDLE()
        status = winfiles._nt_create_file(
            ctypes.byref(handle),
            _FILE_WRITE_DATA | _DELETE | winfiles._SYNCHRONIZE,
            ctypes.byref(obj),
            ctypes.byref(winfiles._IoStatusBlock()),
            None,
            0,
            0,
            _FILE_CREATE,
            winfiles._FILE_NON_DIRECTORY_FILE | winfiles._FILE_SYNCHRONOUS_IO_NONALERT,
            None,
            0,
        )
        if status >= 0 and handle.value is not None:
            return handle.value
        code = winfiles._ntstatus_to_error(status)
        if code not in (80, 183):  # ERROR_FILE_EXISTS, ERROR_ALREADY_EXISTS
            raise winfiles._winerror(destination, code)


@contextmanager
def _destination_io(destination: Path) -> Iterator[None]:
    # Never relabel an exception from a caller's chunk iterator as a save error.
    try:
        yield
    except (OSError, ValueError, RuntimeError) as exc:
        raise APIError(
            500, "save_failed", f"Could not save to {destination}: {exc}"
        ) from exc


def _protect_root(path: Path, root: Path) -> None:
    if path.is_relative_to(root):
        raise invalid("Choose a destination outside the WinRunner data directory")


def _volume_path(handle: int, path: Path) -> Path:
    # Use a volume GUID, not a drive letter, to also catch SUBST drive aliases.
    volume_name_guid = 1
    buffer = ctypes.create_unicode_buffer(512)
    length = _final_path(handle, buffer, len(buffer), volume_name_guid)
    if length >= len(buffer):
        buffer = ctypes.create_unicode_buffer(length + 1)
        length = _final_path(handle, buffer, len(buffer), volume_name_guid)
    if not length or length >= len(buffer):
        raise winfiles._winerror(path)
    return Path(buffer.value)


def _check_leaf(parent: int, destination: Path) -> None:
    try:
        with ExitStack() as stack:
            handle = winfiles._pin_child(
                parent, destination, destination.name, file_leaf=True
            )
            attributes = winfiles._check_handle(
                stack, handle, destination, directory=False
            )
            if attributes & winfiles._FILE_ATTRIBUTE_DIRECTORY:
                raise invalid("Choose a file as the save destination, not a directory")
    except FileNotFoundError:
        pass
    except winfiles.BadPath as exc:
        raise invalid(
            "Choose a save destination that is not a link or reparse point"
        ) from exc


def _discard_temp(handle: int) -> None:
    # Discard through the handle: never unlink a rediscovered path.
    delete = wintypes.BOOL(True)
    _set_file_info(
        handle, _FILE_DISPOSITION_INFO, ctypes.byref(delete), ctypes.sizeof(delete)
    )


@contextmanager
def _temp_file(parent: int, destination: Path) -> Iterator[BinaryIO]:
    with _destination_io(destination):
        handle = _open_temp(parent, destination)
    fd = None
    file = None
    committed = False
    try:
        with _destination_io(destination):
            fd = msvcrt.open_osfhandle(
                handle, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT
            )
            file = os.fdopen(fd, "wb", buffering=0, closefd=False)
        yield file
        committed = True
    finally:
        if not committed:
            _discard_temp(handle)
        try:
            if file is not None:
                file.close()
        finally:
            if fd is None:
                winfiles._close_handle(handle)
            else:
                os.close(fd)


@contextmanager
def _pin_destination(runner: Runner, destination: Path) -> Iterator[int]:
    if os.name != "nt":
        raise APIError(500, "save_failed", "Local file export requires Windows")
    with ExitStack() as stack:
        with _destination_io(destination):
            if not destination.is_absolute() or not destination.name:
                raise invalid("Choose an absolute local file as the save destination")
            try:
                winfiles._validate_component(destination.name)
            except winfiles.BadPath as exc:
                raise invalid(str(exc)) from exc
            _protect_root(destination, runner.base)
            parent_path = destination.parent.resolve(strict=True)
            _protect_root(parent_path / destination.name, runner.base)
            # Follow the chosen parent aliases once; use the no-reparse walk
            # to open the result, then do all mutations relative to that handle.
            _, parent, _ = stack.enter_context(
                winfiles._walk(parent_path, "", relative_handles=True)
            )
            _, root, _ = stack.enter_context(
                winfiles._walk(runner.base, "", relative_handles=True)
            )
            _protect_root(
                _volume_path(parent, parent_path), _volume_path(root, runner.base)
            )
            _check_leaf(parent, destination)
        yield parent


def save_bytes(
    runner: Runner, destination: Path, chunks: Iterable[bytes], cancelled: Event
) -> int:
    """Stream bytes and replace a local file only after all chunks are saved."""
    _check_cancelled(cancelled)
    count = 0
    with _pin_destination(runner, destination) as parent:
        _check_cancelled(cancelled)
        with _temp_file(parent, destination) as file:
            iterator = iter(chunks)
            while True:
                _check_cancelled(cancelled)
                try:
                    chunk = next(iterator)
                except StopIteration:
                    break
                pending = memoryview(chunk)
                with _destination_io(destination):
                    while pending:
                        _check_cancelled(cancelled)
                        written = file.write(pending)
                        if not written:
                            raise OSError("Temporary file write made no progress")
                        count += written
                        pending = pending[written:]
            with _destination_io(destination):
                _check_cancelled(cancelled)
                os.fsync(file.fileno())
                _check_leaf(parent, destination)
                _check_cancelled(cancelled)
                _rename(file, parent, destination)
    return count
