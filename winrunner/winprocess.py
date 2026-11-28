"""Windows Job Object process runner (Windows 10 / Server 2016 or newer)."""

from __future__ import annotations

from collections import deque
import ctypes as C
from ctypes import wintypes as W
from contextlib import ExitStack, contextmanager, suppress
import errno
import logging
import math
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from typing import Callable, cast

LOG = logging.getLogger(__name__)


class JobCancelled(Exception):
    """The job was cancelled; ``exit_code`` is set if a process was reaped."""

    def __init__(
        self, message: str = "Job was cancelled", *, exit_code: int | None = None
    ):
        super().__init__(message)
        self.exit_code = exit_code


class _BasicLimits(C.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", C.c_int64),
        ("PerJobUserTimeLimit", C.c_int64),
        ("LimitFlags", W.DWORD),
        ("MinimumWorkingSetSize", C.c_size_t),
        ("MaximumWorkingSetSize", C.c_size_t),
        ("ActiveProcessLimit", W.DWORD),
        ("Affinity", C.c_size_t),
        ("PriorityClass", W.DWORD),
        ("SchedulingClass", W.DWORD),
    ]


class _ExtendedLimits(C.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", C.c_uint64 * 6),
        ("ProcessMemoryLimit", C.c_size_t),
        ("JobMemoryLimit", C.c_size_t),
        ("PeakProcessMemoryUsed", C.c_size_t),
        ("PeakJobMemoryUsed", C.c_size_t),
    ]


class _Accounting(C.Structure):
    _fields_ = [
        ("TotalUserTime", C.c_int64),
        ("TotalKernelTime", C.c_int64),
        ("ThisPeriodTotalUserTime", C.c_int64),
        ("ThisPeriodTotalKernelTime", C.c_int64),
        ("TotalPageFaultCount", W.DWORD),
        ("TotalProcesses", W.DWORD),
        ("ActiveProcesses", W.DWORD),
        ("TotalTerminatedProcesses", W.DWORD),
    ]


class _StartupInfo(C.Structure):
    _fields_ = [
        ("cb", W.DWORD),
        ("lpReserved", W.LPWSTR),
        ("lpDesktop", W.LPWSTR),
        ("lpTitle", W.LPWSTR),
        ("dwX", W.DWORD),
        ("dwY", W.DWORD),
        ("dwXSize", W.DWORD),
        ("dwYSize", W.DWORD),
        ("dwXCountChars", W.DWORD),
        ("dwYCountChars", W.DWORD),
        ("dwFillAttribute", W.DWORD),
        ("dwFlags", W.DWORD),
        ("wShowWindow", W.WORD),
        ("cbReserved2", W.WORD),
        ("lpReserved2", C.c_void_p),
        ("hStdInput", W.HANDLE),
        ("hStdOutput", W.HANDLE),
        ("hStdError", W.HANDLE),
    ]


class _StartupInfoEx(C.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", C.c_void_p)]


class _ProcessInformation(C.Structure):
    _fields_ = [
        ("hProcess", W.HANDLE),
        ("hThread", W.HANDLE),
        ("dwProcessId", W.DWORD),
        ("dwThreadId", W.DWORD),
    ]


class _MemoryStatus(C.Structure):
    _fields_ = [
        ("dwLength", W.DWORD),
        ("dwMemoryLoad", W.DWORD),
        ("ullTotalPhys", C.c_uint64),
        ("ullAvailPhys", C.c_uint64),
        ("ullTotalPageFile", C.c_uint64),
        ("ullAvailPageFile", C.c_uint64),
        ("ullTotalVirtual", C.c_uint64),
        ("ullAvailVirtual", C.c_uint64),
        ("ullAvailExtendedVirtual", C.c_uint64),
    ]


_kernel32 = C.WinDLL("kernel32", use_last_error=True)


def _native(name, result, *arguments, library=_kernel32):
    function = getattr(library, name)
    function.restype = result
    function.argtypes = arguments
    return function


_P = C.POINTER
_PTR = C.c_void_p
_H = W.HANDLE
_D = W.DWORD
_B = W.BOOL
_SZ = C.c_size_t
_CreateJobObject = _native("CreateJobObjectW", _H, _PTR, W.LPCWSTR)
_SetInformationJobObject = _native("SetInformationJobObject", _B, _H, C.c_int, _PTR, _D)
_QueryInformationJobObject = _native(
    "QueryInformationJobObject", _B, _H, C.c_int, _PTR, _D, _P(_D)
)
_TerminateJobObject = _native("TerminateJobObject", _B, _H, W.UINT)
_CreatePipe = _native("CreatePipe", _B, _P(_H), _P(_H), _PTR, _D)
_CreateFile = _native("CreateFileW", _H, W.LPCWSTR, _D, _D, _PTR, _D, _D, _H)
_CloseHandle = _native("CloseHandle", _B, _H)
_GetCurrentProcess = _native("GetCurrentProcess", _H)
_DuplicateHandle = _native("DuplicateHandle", _B, _H, _H, _H, _P(_H), _D, _B, _D)
_InitializeAttributes = _native(
    "InitializeProcThreadAttributeList", _B, _PTR, _D, _D, _P(_SZ)
)
_UpdateAttribute = _native(
    "UpdateProcThreadAttribute", _B, _PTR, _D, _SZ, _PTR, _SZ, _PTR, _P(_SZ)
)
_DeleteAttributes = _native("DeleteProcThreadAttributeList", None, _PTR)
_CreateProcess = _native(
    "CreateProcessW",
    _B,
    W.LPCWSTR,
    W.LPWSTR,
    _PTR,
    _PTR,
    _B,
    _D,
    _PTR,
    W.LPCWSTR,
    _P(_StartupInfoEx),
    _P(_ProcessInformation),
)
_ReadFile = _native("ReadFile", _B, _H, _PTR, _D, _P(_D), _PTR)
_ResumeThread = _native("ResumeThread", _D, _H)
_WaitForSingleObject = _native("WaitForSingleObject", _D, _H, _D)
_GetExitCodeProcess = _native("GetExitCodeProcess", _B, _H, _P(_D))
_GetSystemTimes = _native("GetSystemTimes", _B, *([_P(W.FILETIME)] * 3))
_GetActiveProcessorGroupCount = _native("GetActiveProcessorGroupCount", W.WORD)
_GetActiveProcessorCount = _native("GetActiveProcessorCount", _D, W.WORD)
_GlobalMemoryStatusEx = _native("GlobalMemoryStatusEx", _B, _P(_MemoryStatus))

_JOB_BASIC_ACCOUNTING = 1
_JOB_EXTENDED_LIMIT = 9
_JOB_KILL_ON_CLOSE = 0x2000
_JOB_LIST = 0x0002000D
_HANDLE_LIST = 0x00020002
_STARTF_USESTDHANDLES = 0x100
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_SUSPENDED = 0x4
_CREATE_UNICODE_ENVIRONMENT = 0x400
_CREATE_NO_WINDOW = 0x08000000
_DUPLICATE_SAME_ACCESS = 2
_INVALID_HANDLE_VALUE = C.c_void_p(-1).value
_ERROR_INSUFFICIENT_BUFFER = 122
_ERROR_BROKEN_PIPE = 109
_ERROR_HANDLE_EOF = 38
_ERROR_NO_DATA = 232
_INFINITE = 0xFFFFFFFF
_CANCEL_EXIT_CODE = 1


def _check(result):
    if not result:
        raise C.WinError(C.get_last_error())
    return result


def _inheritable(handle: int, temporary: ExitStack) -> int:
    duplicate = _H()
    current = _GetCurrentProcess()
    _check(
        _DuplicateHandle(
            current,
            handle,
            current,
            C.byref(duplicate),
            0,
            True,
            _DUPLICATE_SAME_ACCESS,
        )
    )
    # The successful call fills the output handle.
    duplicate_handle = cast(int, duplicate.value)
    temporary.callback(_CloseHandle, duplicate_handle)
    return duplicate_handle


class _PipeReader:
    """Transfer ownership to the reader when it begins execution."""

    def __init__(self, handle: int):
        self._handle: int | None = handle
        self._lock = threading.Lock()

    def take(self) -> int | None:
        with self._lock:
            handle, self._handle = self._handle, None
            return handle

    def close(self):
        # CloseHandle can block behind a synchronous ReadFile. Once a reader
        # takes the pipe, only that thread may close it, even if we abandon it.
        handle = self.take()
        if handle is not None:
            _CloseHandle(handle)


def _output_pipe(resources: ExitStack, temporary: ExitStack) -> tuple[_PipeReader, int]:
    reader, writer = _H(), _H()
    _check(_CreatePipe(C.byref(reader), C.byref(writer), None, 0))
    # The successful call fills both output handles.
    read_handle, write_handle = cast(int, reader.value), cast(int, writer.value)
    try:
        try:
            pipe = _PipeReader(read_handle)
            resources.callback(pipe.close)
        except BaseException:
            _CloseHandle(read_handle)
            raise
        return pipe, _inheritable(write_handle, temporary)
    finally:
        _CloseHandle(write_handle)


def _prepare(argv, env, cwd, executable):
    command = argv if isinstance(argv, str) else subprocess.list2cmdline(argv)
    directory = os.fspath(cwd)
    if not command or any(
        "\0" in text for text in (command, directory, executable or "")
    ):
        raise OSError(errno.EINVAL, "Empty command or NUL in process arguments")
    entries = []
    for key, value in sorted(env.items(), key=lambda pair: pair[0].upper()):
        # Windows also uses hidden environment entries such as '=C:=C:\\dir'.
        if not key or "=" in key[1:] or key == "=" or "\0" in key or "\0" in value:
            raise OSError(errno.EINVAL, "Invalid process environment entry")
        entries.append(f"{key}={value}")
    # create_unicode_buffer contributes the final NUL; an empty block still needs two.
    environment = C.create_unicode_buffer("\0".join(entries) + "\0")
    return C.create_unicode_buffer(command), environment, directory


def _launch(job, resources, command, environment, directory, executable):
    with ExitStack() as temporary:
        size = _SZ()
        # The API sizes the list by failing with ERROR_INSUFFICIENT_BUFFER.
        if (
            _InitializeAttributes(None, 2, 0, C.byref(size))
            or C.get_last_error() != _ERROR_INSUFFICIENT_BUFFER
        ):
            raise C.WinError(C.get_last_error())
        attributes = C.create_string_buffer(size.value)
        _check(_InitializeAttributes(attributes, 2, 0, C.byref(size)))
        temporary.callback(_DeleteAttributes, attributes)

        # Unnamed pipes start non-inheritable. Only the three child standard
        # handles are marked inheritable, briefly, for the explicit allowlist.
        # Concurrent launches through this module each use an allowlist; do not
        # mix them with blanket handle inheritance elsewhere in the runner.
        out_read, out_write = _output_pipe(resources, temporary)
        err_read, err_write = _output_pipe(resources, temporary)
        nul = _CreateFile("NUL", 0x80000000, 3, None, 3, 0, None)
        if nul == _INVALID_HANDLE_VALUE:
            raise C.WinError(C.get_last_error())
        try:
            child_stdin = _inheritable(nul, temporary)
        finally:
            _CloseHandle(nul)
        handles = (_H * 3)(child_stdin, out_write, err_write)
        jobs = (_H * 1)(job)
        _check(
            _UpdateAttribute(
                attributes, 0, _HANDLE_LIST, handles, C.sizeof(handles), None, None
            )
        )
        _check(
            _UpdateAttribute(attributes, 0, _JOB_LIST, jobs, C.sizeof(jobs), None, None)
        )

        startup = _StartupInfoEx()
        startup.StartupInfo.cb = C.sizeof(startup)
        startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = child_stdin
        startup.StartupInfo.hStdOutput = out_write
        startup.StartupInfo.hStdError = err_write
        startup.lpAttributeList = C.addressof(attributes)
        process = _ProcessInformation()
        # The kernel attaches the job during creation. Keep it suspended until
        # both pipe readers are started so a setup failure cannot run user code.
        _check(
            _CreateProcess(
                executable,
                command,
                None,
                None,
                True,
                _EXTENDED_STARTUPINFO_PRESENT
                | _CREATE_SUSPENDED
                | _CREATE_UNICODE_ENVIRONMENT
                | _CREATE_NO_WINDOW,
                environment,
                directory,
                C.byref(startup),
                C.byref(process),
            )
        )
        resources.callback(_CloseHandle, process.hProcess)
        resources.callback(_CloseHandle, process.hThread)
        return process.hProcess, process.hThread, out_read, err_read


def _drain(pipe, callback, failures, buffer, count, abandoned):
    handle = pipe.take()
    if handle is None:
        return
    try:
        while not abandoned.is_set():
            if not _ReadFile(handle, buffer, len(buffer), C.byref(count), None):
                error = C.get_last_error()
                if error in (_ERROR_BROKEN_PIPE, _ERROR_HANDLE_EOF, _ERROR_NO_DATA):
                    return
                raise C.WinError(error)
            if count.value and not abandoned.is_set():
                callback(buffer[: count.value])
    except BaseException as error:
        failures.put(error)
    finally:
        with _cleanup_step("Could not close a job output pipe"):
            _check(_CloseHandle(handle))


def _raise_reader_failure(failures):
    try:
        error = failures.get_nowait()
    except queue.Empty:
        return
    raise error


def _wait_empty(has_processes, failures=None):
    # Completion-port messages are not guaranteed. Query the active count
    # through the owner so cancellation can safely close the job handle.
    accounting = _Accounting()
    while True:
        if failures is not None:
            _raise_reader_failure(failures)
        if not has_processes(accounting):
            return
        time.sleep(0.015)


@contextmanager
def _cleanup_step(message):
    try:
        yield
    except BaseException:
        # Neither cleanup nor a faulty logging handler may replace the error.
        with suppress(BaseException):
            LOG.exception(message)


def _monitor(
    has_processes,
    process,
    out_read,
    err_read,
    stdout,
    stderr,
    resume,
    abort,
    on_started,
):
    readers: list[threading.Thread] = []
    abandoned = None
    try:
        failures: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
        abandoned = threading.Event()
        for handle, callback in ((out_read, stdout), (err_read, stderr)):
            reader = threading.Thread(
                target=_drain,
                args=(
                    handle,
                    callback,
                    failures,
                    C.create_string_buffer(65536),
                    _D(),
                    abandoned,
                ),
                name="winprocess-pipe",
                daemon=True,
            )
            readers.append(reader)
            reader.start()
        _raise_reader_failure(failures)
        if resume() and on_started is not None:
            on_started()
        _wait_empty(has_processes, failures)
        for reader in readers:
            reader.join()
        _raise_reader_failure(failures)
        if _WaitForSingleObject(process, _INFINITE) != 0:
            raise C.WinError(C.get_last_error())
        code = _D()
        _check(_GetExitCodeProcess(process, C.byref(code)))
        return code.value
    except BaseException:
        # Stop the writers before joining readers. Even if accounting fails,
        # drain the buffers through the original log callbacks before raising.
        accounting = None
        with _cleanup_step("Could not stop the failed job"):
            accounting = abort()
        if accounting:
            accounting = None
            with _cleanup_step("Could not query the failed job during cleanup"):
                _wait_empty(has_processes)
                accounting = True
            if accounting is None:
                # An unqueryable job cannot be safely reused. Kill on close
                # rather than treating a termination request as confirmation.
                with _cleanup_step("Could not close the unqueryable job"):
                    abort(close=True)
                    accounting = False
        if accounting is None:
            # Both termination and kill-on-close failed. Never publish a
            # finished task or forget this handle while members may be alive.
            # The owning Runner bounds its waits and can retry cancellation.
            if abandoned is not None:
                abandoned.set()
            pending = _Accounting()
            while True:
                try:
                    if not has_processes(pending):
                        break
                except OSError:
                    # A later cancellation can still close the job handle.
                    pass
                time.sleep(0.05)
        for reader in readers:
            with _cleanup_step("Could not join a job output reader"):
                if reader.ident is not None:
                    reader.join()
        with _cleanup_step("Could not reap the failed job's process"):
            if _WaitForSingleObject(process, _INFINITE) != 0:
                raise C.WinError(C.get_last_error())
        raise


class Job:
    """Own a non-inheritable job handle across a sequence of process runs.

    ``cancel`` latches immediately and requests termination of every member.
    ``run`` does not return until members have exited and both pipes are drained.
    Output callbacks run concurrently in two reader threads and must return promptly.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._cancelled = False
        self._closed = False
        self._active = False
        handle = _check(_CreateJobObject(None, None))
        try:
            limit = _ExtendedLimits()
            limit.BasicLimitInformation.LimitFlags = _JOB_KILL_ON_CLOSE
            _check(
                _SetInformationJobObject(
                    handle, _JOB_EXTENDED_LIMIT, C.byref(limit), C.sizeof(limit)
                )
            )
        except BaseException:
            _CloseHandle(handle)
            raise
        self._handle = handle

    def _check_available(self):
        if self._cancelled or self._closed:
            raise JobCancelled("Job is cancelled or closed")
        if self._active:
            raise RuntimeError("Overlapping runs on one Job are not supported")

    def _close_handle(self):
        if self._handle is not None:
            _check(_CloseHandle(self._handle))
            self._handle = None

    def _has_processes(self, accounting) -> bool:
        with self._lock:
            if self._handle is None:
                # The fallback requested termination by closing the last handle.
                # The monitor still reaps the process and drains its pipes.
                return False
            _check(
                _QueryInformationJobObject(
                    self._handle,
                    _JOB_BASIC_ACCOUNTING,
                    C.byref(accounting),
                    C.sizeof(accounting),
                    None,
                )
            )
            return bool(accounting.ActiveProcesses)

    def _terminate(self) -> bool:
        # Called under _lock; a live handle says the monitor can still query it.
        if self._handle is None:
            return False
        with _cleanup_step("Could not terminate the job; retrying"):
            _check(_TerminateJobObject(self._handle, _CANCEL_EXIT_CODE))
            return True
        with _cleanup_step(
            "Could not terminate the job on retry; trying kill-on-close"
        ):
            _check(_TerminateJobObject(self._handle, _CANCEL_EXIT_CODE))
            return True
        self._closed = True
        self._close_handle()
        return False

    def _resume(self, thread) -> bool:
        with self._lock:
            if self._cancelled:
                # Do not resume if cancellation raced pipe setup.
                self._terminate()
                return False
            if _ResumeThread(thread) == _INFINITE:
                raise C.WinError(C.get_last_error())
            return True

    def _abort(self, *, close: bool = False) -> bool:
        with self._lock:
            if close:
                self._closed = True
                self._close_handle()
                return False
            return self._terminate()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._active:
                self._terminate()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._cancelled = True
            if self._active:
                self._terminate()
            else:
                self._close_handle()

    def run(
        self,
        argv: list[str] | str,
        *,
        cwd: Path | str,
        env: dict[str, str],
        stdout: Callable[[bytes], None],
        stderr: Callable[[bytes], None],
        executable: str | None = None,
        on_started: Callable[[], None] | None = None,
    ) -> int:
        with self._lock:
            self._check_available()
        command, environment, directory = _prepare(argv, env, cwd, executable)
        with ExitStack() as resources:
            with self._lock:
                self._check_available()
                job = self._handle
                process, thread, out_read, err_read = _launch(
                    job, resources, command, environment, directory, executable
                )
                self._active = True
            try:
                code = _monitor(
                    self._has_processes,
                    process,
                    out_read,
                    err_read,
                    stdout,
                    stderr,
                    lambda: self._resume(thread),
                    self._abort,
                    on_started,
                )
            finally:
                with self._lock:
                    self._active = False
                    cancelled = self._cancelled
                    if self._closed:
                        with _cleanup_step(
                            "Could not close the job during final cleanup"
                        ):
                            self._close_handle()
            if cancelled:
                raise JobCancelled("Job was cancelled", exit_code=code)
            return code


class _PdhValue(C.Structure):
    # PDH_FMT_COUNTERVALUE: status followed by the double member of its union.
    _fields_ = [("status", _D), ("value", C.c_double)]


class _PdhCpu:
    """Sample the system-wide counter when GetSystemTimes sees only one group."""

    def __init__(self):
        pdh = C.WinDLL("pdh.dll")
        op = _native("PdhOpenQueryW", W.LONG, W.LPCWSTR, _SZ, _P(_H), library=pdh)
        add = _native(
            "PdhAddEnglishCounterW", W.LONG, _H, W.LPCWSTR, _SZ, _P(_H), library=pdh
        )
        self._close = _native("PdhCloseQuery", W.LONG, _H, library=pdh)
        self._collect = _native("PdhCollectQueryData", W.LONG, _H, library=pdh)
        self._format = _native(
            "PdhGetFormattedCounterValue",
            W.LONG,
            _H,
            _D,
            _P(_D),
            _P(_PdhValue),
            library=pdh,
        )
        self.query, self.counter = _H(), _H()
        self._primed = False
        if op(None, 0, C.byref(self.query)):
            raise OSError("Could not open Windows performance query")
        if add(
            self.query,
            r"\Processor Information(_Total)\% Processor Time",
            0,
            C.byref(self.counter),
        ):
            self.close()
            raise OSError("System-wide Windows processor counter unavailable")

    def close(self) -> None:
        self._close(self.query)

    def sample(self) -> float | None:
        # PDH rate counters need two successful collections. The sampler
        # schedules them about one second apart, independent of health polls.
        if self._collect(self.query):
            self._primed = False
            return None
        if not self._primed:
            self._primed = True
            return None
        value = _PdhValue()
        if (
            not self._format(
                self.counter, 0x200, None, C.byref(value)
            )  # PDH_FMT_DOUBLE
            and value.status in (0, 1)
            and math.isfinite(value.value)
        ):
            return min(100.0, max(0.0, value.value))
        return None


def _filetime(value: W.FILETIME) -> int:
    return (value.dwHighDateTime << 32) | value.dwLowDateTime


class _CpuReader:
    """Own the native counters and baselines for one sampling thread."""

    def __init__(self) -> None:
        self._previous_times: tuple[int, int] | None = None
        self._pdh: _PdhCpu | None = None
        self._pdh_retry_after = 0.0

    def close(self) -> None:
        if self._pdh is not None:
            self._pdh.close()
            self._pdh = None

    def sample(self, now: float) -> float | None:
        if _GetActiveProcessorGroupCount() > 1:
            self._previous_times = None
            if self._pdh is None:
                if now < self._pdh_retry_after:
                    return None
                try:
                    self._pdh = _PdhCpu()
                except (OSError, AttributeError):
                    # Do not fall back to misleading primary-group-only data.
                    self._pdh_retry_after = now + 60.0
                    return None
            return self._pdh.sample()

        self.close()
        self._pdh_retry_after = 0.0
        idle, kernel, user = W.FILETIME(), W.FILETIME(), W.FILETIME()
        if not _GetSystemTimes(C.byref(idle), C.byref(kernel), C.byref(user)):
            self._previous_times = None
            return None
        sample = (_filetime(idle), _filetime(kernel) + _filetime(user))
        previous, self._previous_times = self._previous_times, sample
        if previous is None:
            return None
        elapsed = sample[1] - previous[1]
        idle_elapsed = sample[0] - previous[0]
        if elapsed <= 0 or idle_elapsed < 0:
            return None
        return min(100.0, max(0.0, 100.0 * (elapsed - idle_elapsed) / elapsed))


class _CpuHistory:
    """Time-weighted means of valid intervals, clipped to each window."""

    def __init__(self) -> None:
        self._intervals: deque[tuple[float, float, float]] = deque()
        self._last_time: float | None = None

    def record(
        self, now: float, usage: float | None
    ) -> tuple[float | None, float | None, float | None]:
        previous, self._last_time = self._last_time, now
        if previous is not None and now > previous and usage is not None:
            self._intervals.append((previous, now, usage))
        while self._intervals and self._intervals[0][1] <= now - 60.0:
            self._intervals.popleft()
        return usage, self._average(now - 5.0), self._average(now - 60.0)

    def _average(self, cutoff: float) -> float | None:
        total = duration = 0.0
        for start, end, usage in reversed(self._intervals):
            if end <= cutoff:
                break
            weight = end - max(start, cutoff)
            total += usage * weight
            duration += weight
        return total / duration if duration else None


class CpuSampler:
    """Collect CPU usage in one background thread about once per second.

    Health reads only the last published snapshot. A failed read makes the
    latest value null; rolling means exclude missing intervals and use all
    available history within each window, including during warm-up.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._usage: tuple[float | None, float | None, float | None] = (
            None,
            None,
            None,
        )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is None and not self._stop.is_set():
                thread = threading.Thread(
                    target=self._run, name="winprocess-cpu", daemon=True
                )
                thread.start()
                self._thread = thread

    def close(self) -> None:
        with self._lifecycle_lock:
            self._stop.set()
            if self._thread is not None:
                self._thread.join()
                self._thread = None

    def snapshot(self) -> tuple[float | None, float | None, float | None]:
        with self._snapshot_lock:
            return self._usage

    def _run(self) -> None:
        reader, history = _CpuReader(), _CpuHistory()
        try:
            while not self._stop.is_set():
                usage = reader.sample(time.monotonic())
                snapshot = history.record(time.monotonic(), usage)
                with self._snapshot_lock:
                    self._usage = snapshot
                if self._stop.wait(1.0):
                    break
        finally:
            reader.close()


def health(workspace: Path, uptime_seconds: float, sampler: CpuSampler) -> dict:
    """Report Windows host utilization and capacity for the workspace volume."""
    memory = _MemoryStatus()
    memory.dwLength = C.sizeof(memory)
    _check(_GlobalMemoryStatusEx(C.byref(memory)))
    disk = shutil.disk_usage(workspace)
    usage, usage_5s, usage_60s = sampler.snapshot()
    return {
        "cpu": {
            "logical_cores": _GetActiveProcessorCount(0xFFFF) or os.cpu_count() or 1,
            "usage_percent": usage,
            "usage_5s_percent": usage_5s,
            "usage_60s_percent": usage_60s,
        },
        "memory": {
            "total_bytes": memory.ullTotalPhys,
            "available_bytes": memory.ullAvailPhys,
        },
        "disk": {"total_bytes": disk.total, "available_bytes": disk.free},
        "uptime_seconds": uptime_seconds,
    }
