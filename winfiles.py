"""Validate and pin project paths on Windows.

The caller supplies an absolute project directory in the runner-owned data
area on a trusted local volume. Keep the drive mapping and control of the
data directory trusted. Use pinned_entry for browsing: its handle-relative
operations do not follow reparse points even when another process changes one
in place. Windows deny-delete sharing prevents name replacement but does not
prevent in-place edits, so callers of pinned_path must trust writers not to
install reparse points while the caller uses the returned name. Hard links
are ordinary files; this helper is not a sandbox for jobs running with the
runner's privileges.
"""

import ctypes
import errno
import os
import string
from contextlib import ExitStack, contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import BinaryIO, Iterator, cast

if os.name == "nt":
    import msvcrt


class BadPath(ValueError):
    """A path uses invalid syntax or encounters a reparse point."""


_INVALID_CHARS = frozenset('<>:"|?*')
_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}) | {
    prefix + digit for prefix in ("COM", "LPT") for digit in "123456789¹²³"
}


def _validate_component(component: str) -> None:
    if component.endswith((".", " ")) or any(
        c in _INVALID_CHARS or ord(c) < 32 for c in component
    ):
        raise BadPath(f"Invalid path component: {component!r}")
    # Win32 also recognizes device names with an extension or spaces before it.
    if component.partition(".")[0].rstrip(" ").upper() in _DEVICES:
        raise BadPath(f"Reserved Windows device name: {component!r}")


def _relative_parts(relative: str) -> list[str]:
    if not isinstance(relative, str):
        raise BadPath("The path must be a string")
    if relative.startswith(("/", "\\")):
        raise BadPath("The path must be relative to the project")
    parts = []
    for component in relative.replace("\\", "/").split("/"):
        if not component or component == ".":
            continue
        if component == "..":
            raise BadPath("Parent directory components are not permitted")
        _validate_component(component)
        parts.append(component)
    return parts


_FILE_READ_DATA = 0x0001  # FILE_LIST_DIRECTORY for directories
_FILE_READ_ATTRIBUTES = 0x0080
_FILE_SHARE_READ = 0x0001
_FILE_SHARE_WRITE = 0x0002
_FILE_SHARE_DELETE = 0x0004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_ATTRIBUTE_DIRECTORY = 0x0010
_FILE_ATTRIBUTE_DEVICE = 0x0040
_FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
_FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
_FILE_ID_BOTH_DIRECTORY_INFO = 10
_FILE_ID_BOTH_DIRECTORY_RESTART_INFO = 11
_ERROR_NO_MORE_FILES = 18
_ERROR_CANT_RESOLVE_FILENAME = 1921
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

_SYNCHRONIZE = 0x00100000
_FILE_OPEN = 1
_FILE_SYNCHRONOUS_IO_NONALERT = 0x20
_FILE_NON_DIRECTORY_FILE = 0x40
_FILE_OPEN_FOR_BACKUP_INTENT = 0x4000
_OBJ_CASE_INSENSITIVE = 0x40
_OBJ_DONT_REPARSE = 0x1000
_STATUS_DELETE_PENDING = 0xC0000056
_STATUS_FILE_IS_A_DIRECTORY = 0xC00000BA
_STATUS_REPARSE_POINT_ENCOUNTERED = 0xC000050B
_DUPLICATE_SAME_ACCESS = 2


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("attributes", wintypes.DWORD), ("tag", wintypes.DWORD)]


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.USHORT),
        ("maximum_length", wintypes.USHORT),
        ("buffer", wintypes.LPVOID),
    ]


class _ObjectAttributes(ctypes.Structure):
    _fields_ = [
        ("length", wintypes.ULONG),
        ("root_directory", wintypes.HANDLE),
        ("object_name", ctypes.POINTER(_UnicodeString)),
        ("attributes", wintypes.ULONG),
        ("security_descriptor", wintypes.LPVOID),
        ("security_qos", wintypes.LPVOID),
    ]


class _IoStatusBlock(ctypes.Structure):
    # The first field is a union of an NTSTATUS and a pointer.
    _fields_ = [
        ("status_or_pointer", ctypes.c_void_p),
        ("information", ctypes.c_size_t),
    ]


class _DirectoryInfo(ctypes.Structure):
    _fields_ = [
        ("next_entry_offset", wintypes.DWORD),
        ("file_index", wintypes.DWORD),
        ("creation_time", ctypes.c_int64),
        ("last_access_time", ctypes.c_int64),
        ("last_write_time", ctypes.c_int64),
        ("change_time", ctypes.c_int64),
        ("file_size", ctypes.c_int64),
        ("allocation_size", ctypes.c_int64),
        ("attributes", wintypes.DWORD),
        ("name_bytes", wintypes.DWORD),
        ("ea_size", wintypes.DWORD),
        ("short_name_length", ctypes.c_char),
        ("short_name", wintypes.WCHAR * 12),
        ("file_id", ctypes.c_int64),
        ("name", wintypes.WCHAR),  # beginning of a variable-length name
    ]


if os.name == "nt":
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _create_file.restype = wintypes.HANDLE

    _get_file_info = _kernel32.GetFileInformationByHandleEx
    _get_file_info.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    _get_file_info.restype = wintypes.BOOL

    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = (wintypes.HANDLE,)
    _close_handle.restype = wintypes.BOOL

    _get_process = _kernel32.GetCurrentProcess
    _get_process.argtypes = ()
    _get_process.restype = wintypes.HANDLE

    _duplicate_handle = _kernel32.DuplicateHandle
    _duplicate_handle.argtypes = (
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    )
    _duplicate_handle.restype = wintypes.BOOL

    _ntdll = ctypes.WinDLL("ntdll")
    _nt_create_file = _ntdll.NtCreateFile
    _nt_create_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.DWORD,
        ctypes.POINTER(_ObjectAttributes),
        ctypes.POINTER(_IoStatusBlock),
        wintypes.LPVOID,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.LPVOID,
        wintypes.ULONG,
    )
    _nt_create_file.restype = wintypes.LONG

    _ntstatus_to_error = _ntdll.RtlNtStatusToDosError
    _ntstatus_to_error.argtypes = (wintypes.LONG,)
    _ntstatus_to_error.restype = wintypes.ULONG


def _winerror(path: Path, code: int | None = None) -> OSError:
    if code is None:
        code = ctypes.get_last_error()
    return OSError(0, ctypes.FormatError(code).strip(), str(path), code)


def _pin(path: Path) -> int:
    # A metadata-only handle may not participate in Windows share checks.
    # Ask for READ_DATA so omitting FILE_SHARE_DELETE actually protects the name.
    handle = _create_file(
        str(path),
        _FILE_READ_DATA | _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        raise _winerror(path)
    return handle


def _pin_child(parent: int, path: Path, name: str, *, file_leaf: bool = False) -> int:
    # Open the single name relative to the *directory object*, rather than
    # resolving a new absolute path through possibly mutated ancestors.
    encoded = name.encode("utf-16-le", errors="surrogatepass")
    if len(encoded) > 65532:
        raise OSError(errno.ENAMETOOLONG, "Path component too long", str(path))
    buffer = ctypes.create_string_buffer(encoded + b"\0\0")
    nt_name = _UnicodeString(len(encoded), len(encoded) + 2, ctypes.addressof(buffer))
    obj = _ObjectAttributes(
        ctypes.sizeof(_ObjectAttributes),
        parent,
        ctypes.pointer(nt_name),
        _OBJ_CASE_INSENSITIVE | _OBJ_DONT_REPARSE,
        None,
        None,
    )
    handle = wintypes.HANDLE()
    options = (
        _FILE_SYNCHRONOUS_IO_NONALERT
        | _FILE_OPEN_FOR_BACKUP_INTENT
        | _FILE_FLAG_OPEN_REPARSE_POINT
    )
    # Let the kernel distinguish files from directories in the open itself.
    # A directory is never opened with delete sharing, even temporarily.
    for share_delete in (True, False) if file_leaf else (False,):
        share = _FILE_SHARE_READ | _FILE_SHARE_WRITE
        create_options = options
        if share_delete:
            share |= _FILE_SHARE_DELETE
            create_options |= _FILE_NON_DIRECTORY_FILE
        status = _nt_create_file(
            ctypes.byref(handle),
            _FILE_READ_DATA | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
            ctypes.byref(obj),
            ctypes.byref(_IoStatusBlock()),
            None,
            0,
            share,
            _FILE_OPEN,
            create_options,
            None,
            0,
        )
        if status >= 0:
            # A successful NtCreateFile fills the output handle.
            return cast(int, handle.value)
        if not share_delete or (status & 0xFFFFFFFF) != _STATUS_FILE_IS_A_DIRECTORY:
            break
        # Retry without delete sharing to pin a directory leaf (or a new file
        # if the name changed after the first open).

    ntstatus = status & 0xFFFFFFFF
    if ntstatus == _STATUS_DELETE_PENDING:
        # Legacy deletion leaves the name visible until the last handle is
        # closed, but another reader cannot open it. Preserve real ACL errors.
        raise FileNotFoundError(
            errno.ENOENT, "File or directory is pending deletion", str(path)
        )
    code = _ntstatus_to_error(status)
    if (
        ntstatus == _STATUS_REPARSE_POINT_ENCOUNTERED
        or code == _ERROR_CANT_RESOLVE_FILENAME
    ):
        raise BadPath(f"Reparse point not permitted: {name!r}")
    raise _winerror(path, code)


def _check_handle(stack: ExitStack, handle: int, path: Path, *, directory: bool) -> int:
    stack.callback(_close_handle, handle)
    info = _FileAttributeTagInfo()
    if not _get_file_info(
        handle, _FILE_ATTRIBUTE_TAG_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
    ):
        raise _winerror(path)
    if info.attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise BadPath(f"Reparse point not permitted: {path.name!r}")
    if directory and not info.attributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(path))
    return info.attributes


@contextmanager
def _walk(
    root: Path, relative: str, *, relative_handles: bool
) -> Iterator[tuple[Path, int, int]]:
    relative_parts = _relative_parts(relative)
    if os.name != "nt":
        raise OSError(errno.ENOTSUP, "Project path pinning requires Windows")

    root = Path(root)
    if (
        not root.is_absolute()
        or len(root.drive) != 2
        or root.drive[0] not in string.ascii_letters
        or root.drive[1:] != ":"
    ):
        raise BadPath("The project root must be an absolute local drive path")
    root_parts = root.parts[1:]
    for component in root_parts:
        _validate_component(component)

    parts = (*root_parts, *relative_parts)
    with ExitStack() as stack:
        path = Path(root.anchor)
        handle = _pin(path)
        attributes = _check_handle(stack, handle, path, directory=True)
        for index, component in enumerate(parts):
            path /= component
            directory = index < len(root_parts) or index + 1 < len(parts)
            if relative_handles:
                handle = _pin_child(handle, path, component, file_leaf=not directory)
            else:
                handle = _pin(path)
            attributes = _check_handle(stack, handle, path, directory=directory)
        yield path, handle, attributes


@contextmanager
def pinned_path(root: Path, relative: str) -> Iterator[Path]:
    """Yield an absolute file or directory path while its names are pinned.

    Accept either separator, and use an empty path or '.' to pin the root.
    The drive root and all intervening directories, including the runner's
    data directory and the project root, are opened and checked in order.
    Reopening the resulting name assumes trusted writers (see module docs).
    Use pinned_entry for downloads and directory listings instead.
    """
    with _walk(root, relative, relative_handles=False) as (path, _, _):
        yield path


class _PinnedEntry:
    def __init__(self, path: Path, handle: int, attributes: int):
        self.path = path
        self.is_dir = bool(attributes & _FILE_ATTRIBUTE_DIRECTORY)
        self._regular = not attributes & (
            _FILE_ATTRIBUTE_DIRECTORY | _FILE_ATTRIBUTE_DEVICE
        )
        self._handle: int | None = handle

    def _require_handle(self) -> int:
        if self._handle is None:
            raise ValueError("The pinned entry is no longer active")
        return self._handle

    def open_file(self) -> BinaryIO:
        """Open a seekable binary file; use one stream per entry at a time."""
        handle = self._require_handle()
        if self.is_dir:
            raise IsADirectoryError(errno.EISDIR, "Is a directory", str(self.path))
        if not self._regular:
            raise BadPath("The path does not identify a regular file")

        duplicate = wintypes.HANDLE()
        proc = _get_process()
        if not _duplicate_handle(
            proc,
            handle,
            proc,
            ctypes.byref(duplicate),
            0,
            False,
            _DUPLICATE_SAME_ACCESS,
        ):
            raise _winerror(self.path)
        # A successful DuplicateHandle fills the output handle.
        duplicate_handle = cast(int, duplicate.value)
        try:
            fd = msvcrt.open_osfhandle(
                duplicate_handle, os.O_RDONLY | os.O_BINARY | os.O_NOINHERIT
            )
        except BaseException:
            _close_handle(duplicate_handle)
            raise
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            return os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise

    def list_entries(self) -> list[dict]:
        """List directly from the directory handle, without following its entries."""
        handle = self._require_handle()
        if not self.is_dir:
            raise NotADirectoryError(errno.ENOTDIR, "Not a directory", str(self.path))
        buffer = ctypes.create_string_buffer(64 * 1024)
        info_class = _FILE_ID_BOTH_DIRECTORY_RESTART_INFO
        entries = []
        while _get_file_info(handle, info_class, buffer, ctypes.sizeof(buffer)):
            offset = 0
            while True:
                record = _DirectoryInfo.from_buffer(buffer, offset)
                start = offset + _DirectoryInfo.name.offset
                # ctypes c_char arrays return bytes when sliced.
                raw_name = cast(bytes, buffer[start : start + record.name_bytes])
                name = raw_name.decode("utf-16-le", errors="surrogatepass")
                if name not in (".", ".."):
                    if record.attributes & (
                        _FILE_ATTRIBUTE_REPARSE_POINT | _FILE_ATTRIBUTE_DEVICE
                    ):
                        kind, size = "other", None
                    elif record.attributes & _FILE_ATTRIBUTE_DIRECTORY:
                        kind, size = "directory", None
                    else:
                        kind, size = "file", record.file_size
                    entries.append({"name": name, "type": kind, "size_bytes": size})
                if not record.next_entry_offset:
                    break
                offset += record.next_entry_offset
            info_class = _FILE_ID_BOTH_DIRECTORY_INFO
        code = ctypes.get_last_error()
        if code != _ERROR_NO_MORE_FILES:
            raise _winerror(self.path, code)
        return entries


@contextmanager
def pinned_entry(root: Path, relative: str) -> Iterator[_PinnedEntry]:
    """Yield checked file/dir access that never reopens the entry by path.

    The opened regular file stays readable after rename, deletion, or an
    atomic replacement using Windows POSIX rename semantics. Legacy Windows
    replacement (including os.replace) can still reject an open target.
    This pins the file object, not its contents against in-place writes.
    Directory handles continue to deny delete sharing.
    """
    with _walk(root, relative, relative_handles=True) as (path, handle, attributes):
        entry = _PinnedEntry(path, handle, attributes)
        try:
            yield entry
        finally:
            entry._handle = None
