import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

if os.name == "nt":
    from winrunner import winprocess
    from winrunner.winprocess import (
        CpuSampler,
        Job,
        JobCancelled,
        _CpuHistory,
        _CpuReader,
        _PdhCpu,
        health,
    )


@unittest.skipUnless(os.name == "nt", "requires Windows Job Objects")
class WinProcessTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="winprocess spaced ")
        self.addCleanup(folder.cleanup)
        self.cwd = Path(folder.name)
        self.env = dict(os.environ)
        self.job = self.new_job()
        self.out = bytearray()
        self.err = bytearray()

    def new_job(self):
        job = Job()
        self.addCleanup(job.close)
        return job

    def run_python(self, source, *arguments, **kwargs):
        options = dict(
            cwd=self.cwd, env=self.env, stdout=self.out.extend, stderr=self.err.extend
        )
        options.update(kwargs)
        return self.job.run([sys.executable, "-c", source, *arguments], **options)

    def start_worker(self, function, job=None):
        job = self.job if job is None else job
        result: queue.Queue[int | BaseException] = queue.Queue()

        def work():
            try:
                result.put(function())
            except BaseException as error:
                result.put(error)

        thread = threading.Thread(target=work, daemon=True)
        thread.start()

        def cleanup():
            if thread.is_alive():
                job.cancel()
                thread.join(5)

        self.addCleanup(cleanup)
        return thread, result

    def finish(self, worker, timeout=10):
        thread, result = worker
        thread.join(timeout)
        self.assertFalse(thread.is_alive(), "worker did not complete")
        return result.get_nowait()

    def test_argv_raw_environment_cwd_and_sequential_use(self):
        self.env["WINPROCESS_TEST"] = "unicode \u6d4b\u8bd5"
        arg = 'embedded "quote" \\ with a space\\'
        source = (
            "import os,sys; "
            "sys.stdout.buffer.write((os.getcwd()+'|'+os.environ['WINPROCESS_TEST']+'|'+sys.argv[1]).encode()); "
            "sys.stderr.buffer.write(sys.stdin.buffer.read() or b'EOF'); sys.exit(23)"
        )
        started = []
        self.assertEqual(
            self.run_python(source, arg, on_started=lambda: started.append(True)), 23
        )
        self.assertEqual(started, [True])
        self.assertEqual(
            self.out.decode(), f"{self.cwd}|{self.env['WINPROCESS_TEST']}|{arg}"
        )
        self.assertEqual(self.err, b"EOF")
        self.out.clear()
        raw = subprocess.list2cmdline([sys.executable, "-c", "print('raw', end='')"])
        self.assertEqual(
            self.job.run(
                raw,
                cwd=str(self.cwd),
                env=self.env,
                stdout=self.out.extend,
                stderr=self.err.extend,
                executable=sys.executable,
            ),
            0,
        )
        self.assertEqual(self.out, b"raw")
        self.out.clear()
        cmd = self.env["COMSPEC"]
        shell_command = f'"{cmd}" /d /s /c "echo first^&second"'
        self.assertEqual(
            self.job.run(
                shell_command,
                cwd=self.cwd,
                env=self.env,
                stdout=self.out.extend,
                stderr=self.err.extend,
                executable=cmd,
            ),
            0,
        )
        self.assertEqual(self.out, b"first&second\r\n")

    def test_both_pipes_deliver_bytes_before_process_exits(self):
        gate = self.cwd / "release"
        streamed = [threading.Event(), threading.Event()]
        count = 300_000

        def collect(data, destination, event):
            destination.extend(data)
            if len(destination) == count:
                event.set()

        source = (
            "import os,pathlib,sys,time\n"
            f"os.write(1, b'x'*{count}); os.write(2, b'y'*{count})\n"
            "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.01)\n"
        )
        worker = self.start_worker(
            lambda: self.run_python(
                source,
                str(gate),
                stdout=lambda data: collect(data, self.out, streamed[0]),
                stderr=lambda data: collect(data, self.err, streamed[1]),
            )
        )
        for event in streamed:
            self.assertTrue(event.wait(8), "pipe did not stream until exit")
        self.assertTrue(worker[0].is_alive())
        gate.touch()
        self.assertEqual(self.finish(worker), 0)
        self.assertEqual(self.out, b"x" * count)
        self.assertEqual(self.err, b"y" * count)

    def test_waits_for_descendant_without_inherited_pipes(self):
        gate = self.cwd / "release"
        done = self.cwd / "done"
        started = threading.Event()
        child = (
            "import pathlib,sys,time\n"
            "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.01)\n"
            "pathlib.Path(sys.argv[2]).touch()\n"
        )
        parent = (
            "import subprocess,sys; "
            "subprocess.Popen([sys.executable,'-c',*sys.argv[1:]], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print('child', flush=True); "
            "sys.exit(17)"
        )
        worker = self.start_worker(
            lambda: self.run_python(
                parent, child, str(gate), str(done), stdout=lambda _: started.set()
            )
        )
        self.assertTrue(started.wait(8), self.err)
        worker[0].join(0.25)
        self.assertTrue(
            worker[0].is_alive(), "run returned while descendant was still alive"
        )
        gate.touch()
        self.assertEqual(self.finish(worker), 17)
        self.assertTrue(done.is_file())

    def test_cancellation_terminates_descendant_and_latches(self):
        import ctypes
        from ctypes import wintypes

        started = threading.Event()
        child = "import time; time.sleep(60)"
        parent = (
            "import subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c',sys.argv[1]], stdin=subprocess.DEVNULL, "
            "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); print(p.pid, flush=True); "
            "time.sleep(60)"
        )

        def collect(data):
            self.out.extend(data)
            if b"\n" in self.out:
                started.set()

        worker = self.start_worker(
            lambda: self.run_python(parent, child, stdout=collect)
        )
        self.assertTrue(started.wait(8), self.err)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x00100000, False, int(self.out))
        self.assertTrue(handle, f"OpenProcess failed: {ctypes.get_last_error()}")
        try:
            self.assertEqual(k32.WaitForSingleObject(handle, 0), 258)
            self.job.cancel()
            result = self.finish(worker)
            self.assertIsInstance(result, JobCancelled)
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(k32.WaitForSingleObject(handle, 0), 0)
        finally:
            k32.CloseHandle(handle)
        self.job.cancel()
        with self.assertRaises(JobCancelled):
            self.run_python("raise AssertionError('must not start')")

    def test_cancel_falls_back_to_kill_on_close(self):
        ready = threading.Event()

        def capture(chunk):
            self.out.extend(chunk)
            ready.set()

        worker = self.start_worker(
            lambda: self.run_python(
                "import time; print('ready', flush=True); time.sleep(60)",
                stdout=capture,
            )
        )
        self.assertTrue(ready.wait(8))

        def fail_terminate(*_):
            winprocess.C.set_last_error(5)
            return 0

        with (
            patch.object(
                winprocess, "_TerminateJobObject", side_effect=fail_terminate
            ) as terminate,
            self.assertLogs(winprocess.LOG, "ERROR") as logs,
        ):
            self.job.cancel()
            result = self.finish(worker)
        self.assertEqual(terminate.call_count, 2)
        self.assertTrue(any("trying kill-on-close" in line for line in logs.output))
        self.assertIsInstance(result, JobCancelled)
        self.assertIsNotNone(result.exit_code)
        self.assertIn(b"ready", self.out)
        self.assertIsNone(self.job._handle)

    def test_cancel_before_launch_and_close_are_idempotent(self):
        self.job.cancel()
        self.job.cancel()
        with self.assertRaises(JobCancelled):
            self.run_python("raise AssertionError('must not start')")
        self.job.close()
        self.job.close()
        self.job.cancel()
        with self.assertRaises(JobCancelled):
            self.run_python("raise AssertionError('must not start')")

    def test_failed_launch_does_not_start_or_poison_job(self):
        started = []
        with self.assertRaises(OSError):
            self.job.run(
                ["not-real"],
                cwd=self.cwd,
                env=self.env,
                stdout=self.out.extend,
                stderr=self.err.extend,
                executable=str(self.cwd / "no.exe"),
                on_started=lambda: started.append(True),
            )
        self.assertEqual(started, [])
        self.assertEqual(self.run_python("pass"), 0)

    def test_second_reader_start_failure_never_runs_child(self):
        marker = self.cwd / "ran"
        started = []
        readers = []
        original_start = threading.Thread.start
        failure = RuntimeError("could not start stderr reader")

        def start(thread):
            if thread.name == "winprocess-pipe":
                readers.append(thread)
                if len(readers) == 2:
                    raise failure
            original_start(thread)

        with (
            patch.object(threading.Thread, "start", start),
            patch(
                "winrunner.winprocess._ResumeThread", wraps=winprocess._ResumeThread
            ) as resume,
        ):
            worker = self.start_worker(
                lambda: self.run_python(
                    "import pathlib, sys; pathlib.Path(sys.argv[1]).touch()",
                    str(marker),
                    on_started=lambda: started.append(True),
                )
            )
            self.assertIs(self.finish(worker), failure)
            resume.assert_not_called()
        self.assertFalse(marker.exists())
        self.assertEqual(started, [])
        self.assertEqual(len(readers), 2)
        self.assertFalse(any(reader.is_alive() for reader in readers))
        self.assertEqual(self.run_python("pass"), 0)

    def test_resume_failure_reaps_suspended_child(self):
        def fail_resume(_thread):
            winprocess.C.set_last_error(5)
            return 0xFFFFFFFF

        started = threading.Event()
        with patch("winrunner.winprocess._ResumeThread", side_effect=fail_resume):
            worker = self.start_worker(
                lambda: self.run_python("print('must not run')", on_started=started.set)
            )
            result = self.finish(worker)
        self.assertIsInstance(result, OSError)
        self.assertEqual(result.winerror, 5)
        self.assertFalse(started.is_set())
        self.assertEqual(self.out, b"")
        self.assertEqual(self.run_python("pass"), 0)

    def test_cancel_while_starting_readers_never_resumes_child(self):
        starting, release, started = (threading.Event() for _ in range(3))
        original_start = threading.Thread.start

        def start(thread):
            if thread.name == "winprocess-pipe" and not starting.is_set():
                starting.set()
                if not release.wait(8):
                    raise TimeoutError("test did not release reader setup")
            original_start(thread)

        with (
            patch.object(threading.Thread, "start", start),
            patch(
                "winrunner.winprocess._ResumeThread", wraps=winprocess._ResumeThread
            ) as resume,
        ):
            worker = self.start_worker(
                lambda: self.run_python("print('must not run')", on_started=started.set)
            )
            try:
                self.assertTrue(starting.wait(8))
                with self.assertRaisesRegex(RuntimeError, "Overlapping runs"):
                    self.run_python("pass")
                self.job.cancel()
            finally:
                release.set()
            result = self.finish(worker)
            resume.assert_not_called()
        self.assertIsInstance(result, JobCancelled)
        self.assertEqual(result.exit_code, 1)
        self.assertFalse(started.is_set())
        self.assertEqual(self.out, b"")

    def test_monitor_failure_keeps_primary_error_drains_logs_and_closes_job(self):
        for failed_operation, expected_log in (
            ("_QueryInformationJobObject", "Could not query the failed job"),
            ("_TerminateJobObject", "trying kill-on-close"),
        ):
            with self.subTest(failed_operation=failed_operation):
                self.job = self.new_job()
                self.out.clear()
                self.err.clear()
                stdout_seen, stderr_seen, release_reads = (
                    threading.Event() for _ in range(3)
                )
                failure = RuntimeError("on_started failed")
                gate = self.cwd / f"write-tail-{failed_operation}"
                written = self.cwd / f"written-{failed_operation}"

                def capture(buffer, seen):
                    def collect(data):
                        buffer.extend(data)
                        seen.set()
                        if not release_reads.wait(8):
                            raise TimeoutError("test did not release output reader")

                    return collect

                def started():
                    self.assertTrue(stdout_seen.wait(8))
                    self.assertTrue(stderr_seen.wait(8))
                    gate.touch()
                    deadline = time.monotonic() + 8
                    while not written.exists():
                        if time.monotonic() >= deadline:
                            raise TimeoutError(
                                "child did not write the remaining output"
                            )
                        time.sleep(0.01)
                    self.assertEqual((self.out, self.err), (b"out", b"\xfferr"))
                    raise failure

                def cleanup_failed(*_args):
                    release_reads.set()
                    winprocess.C.set_last_error(5)
                    return 0

                with (
                    patch(
                        f"winrunner.winprocess.{failed_operation}",
                        side_effect=cleanup_failed,
                    ),
                    self.assertLogs(winprocess.LOG, level="ERROR") as diagnostics,
                ):
                    worker = self.start_worker(
                        lambda: self.run_python(
                            "import os, pathlib, sys, time\n"
                            "os.write(1, b'out'); os.write(2, b'\\xfferr')\n"
                            "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(.01)\n"
                            "os.write(1, b'-tail'); os.write(2, b'-tail')\n"
                            "pathlib.Path(sys.argv[2]).touch(); time.sleep(60)\n",
                            str(gate),
                            str(written),
                            stdout=capture(self.out, stdout_seen),
                            stderr=capture(self.err, stderr_seen),
                            on_started=started,
                        )
                    )
                    try:
                        self.assertIs(self.finish(worker), failure)
                    finally:
                        release_reads.set()
                self.assertTrue(
                    any(expected_log in line for line in diagnostics.output)
                )
                self.assertEqual((self.out, self.err), (b"out-tail", b"\xfferr-tail"))
                with self.assertRaises(JobCancelled):
                    self.run_python("pass")

    def test_double_cleanup_failure_keeps_work_pending_until_stop_succeeds(self):
        failure = RuntimeError("could not resume")
        close_attempted = threading.Event()
        close_handle = winprocess._CloseHandle
        job_handle = self.job._handle

        def fail_to_close_job(handle):
            if handle == job_handle:
                close_attempted.set()
                winprocess.C.set_last_error(5)
                return 0
            return close_handle(handle)

        with (
            patch("winrunner.winprocess._ResumeThread", side_effect=failure),
            patch("winrunner.winprocess._TerminateJobObject", return_value=0),
            patch("winrunner.winprocess._CloseHandle", side_effect=fail_to_close_job),
            self.assertLogs(winprocess.LOG, level="ERROR") as diagnostics,
        ):
            worker = self.start_worker(lambda: self.run_python("pass"))
            self.assertTrue(close_attempted.wait(3))
            worker[0].join(0.25)
            self.assertTrue(worker[0].is_alive())
        self.assertTrue(
            any("trying kill-on-close" in line for line in diagnostics.output)
        )
        self.assertTrue(
            any("Could not stop the failed job" in line for line in diagnostics.output)
        )
        # The handle is retained, so retry can still terminate the suspended child.
        self.job.close()
        self.assertIs(self.finish(worker, timeout=3), failure)
        with self.assertRaises(JobCancelled):
            self.run_python("pass")

    def test_concurrent_jobs_have_independent_streams(self):
        gate = self.cwd / "release"
        started = threading.Event()
        source = (
            "import os,pathlib,sys,time\n"
            "os.write(1, sys.argv[1].encode()); os.write(2, sys.argv[1].encode())\n"
            "while len(sys.argv)>2 and not pathlib.Path(sys.argv[2]).exists(): time.sleep(.01)\n"
        )
        barrier = threading.Barrier(2)

        def first():
            barrier.wait(8)
            return self.run_python(source, "slow", str(gate), on_started=started.set)

        other = self.new_job()
        out, err = bytearray(), bytearray()

        def second():
            barrier.wait(8)
            return other.run(
                [sys.executable, "-c", source, "fast"],
                cwd=self.cwd,
                env=self.env,
                stdout=out.extend,
                stderr=err.extend,
            )

        slow = self.start_worker(first)
        fast = self.start_worker(second, other)
        self.assertTrue(started.wait(8))
        self.assertEqual(self.finish(fast), 0)
        self.assertEqual((out, err), (b"fast", b"fast"))
        gate.touch()
        self.assertEqual(self.finish(slow), 0)
        self.assertEqual((self.out, self.err), (b"slow", b"slow"))


@unittest.skipUnless(os.name == "nt", "requires Windows performance counters")
class CpuSamplerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(prefix="winprocess CPU ")
        self.addCleanup(folder.cleanup)
        self.cwd = Path(folder.name)
        self.sampler = CpuSampler()
        self.addCleanup(self.sampler.close)

    def test_health_background_single_group_native_metrics(self):
        first = health(self.cwd, 3.5, self.sampler)
        self.assertEqual(first["uptime_seconds"], 3.5)
        self.assertGreater(first["cpu"]["logical_cores"], 0)
        for name in ("usage_percent", "usage_5s_percent", "usage_60s_percent"):
            self.assertIsNone(first["cpu"][name])
        for key in ("disk", "memory"):
            self.assertGreater(first[key]["total_bytes"], 0)
            self.assertGreaterEqual(
                first[key]["total_bytes"], first[key]["available_bytes"]
            )
        with patch(
            "winrunner.winprocess._GetActiveProcessorGroupCount", return_value=1
        ):
            try:
                self.sampler.start()
                # Wait without calling health: no request establishes the baseline.
                deadline = time.monotonic() + 4
                while None in self.sampler.snapshot() and time.monotonic() < deadline:
                    time.sleep(0.02)
                cpu = health(self.cwd, 4, self.sampler)["cpu"]
                for name in ("usage_percent", "usage_5s_percent", "usage_60s_percent"):
                    self.assertIsNotNone(cpu[name], name)
                    self.assertTrue(0 <= cpu[name] <= 100, name)
            finally:
                self.sampler.close()

    def test_health_does_not_sample_or_wait_for_native_read(self):
        entered, release = threading.Event(), threading.Event()
        workers = []

        def sample(_now):
            workers.append(threading.current_thread())
            entered.set()
            release.wait(5)
            return None

        with patch("winrunner.winprocess._CpuReader") as build:
            reader = build.return_value
            reader.sample.side_effect = sample
            try:
                self.sampler.start()
                self.sampler.start()
                self.assertTrue(entered.wait(5))
                first = health(self.cwd, 0, self.sampler)["cpu"]
                self.assertEqual(health(self.cwd, 0, self.sampler)["cpu"], first)
                self.assertIsNone(first["usage_percent"])
                self.assertEqual(reader.sample.call_count, 1)
            finally:
                release.set()
                self.sampler.close()
            self.sampler.close()
            build.assert_called_once()
            reader.close.assert_called_once()
            self.assertFalse(workers[0].is_alive())

    def test_windows_use_elapsed_time_and_clip_partial_intervals(self):
        history = _CpuHistory()
        self.assertEqual(history.record(0, None), (None, None, None))
        self.assertEqual(history.record(2, 20), (20, 20, 20))
        current, short, long = history.record(6, 80)
        self.assertEqual(current, 80)
        self.assertEqual(short, (1 * 20 + 4 * 80) / 5)
        self.assertEqual(long, (2 * 20 + 4 * 80) / 6)
        current, short, long = history.record(61, None)
        self.assertIsNone(current)
        self.assertIsNone(short)  # The gap is not counted as 0% or held at 80%.
        self.assertEqual(long, (1 * 20 + 4 * 80) / 5)
        self.assertEqual(history.record(66, None), (None, None, None))
        self.assertEqual(history.record(67, 0), (0, 0, 0))

    def test_native_pdh_total_counter(self):
        try:
            pdh = _PdhCpu()
        except (OSError, AttributeError) as error:
            self.skipTest(f"host performance counters unavailable: {error}")
        self.addCleanup(pdh.close)
        self.assertIsNone(pdh.sample())
        time.sleep(1.0)
        usage = pdh.sample()
        assert usage is not None
        self.assertTrue(0 <= usage <= 100)

    def test_multi_group_selects_pdh_instead_of_single_group(self):
        reader = _CpuReader()
        self.addCleanup(reader.close)
        with (
            patch("winrunner.winprocess._GetActiveProcessorGroupCount", return_value=2),
            patch("winrunner.winprocess._GetSystemTimes") as single_group,
            patch("winrunner.winprocess._PdhCpu") as build,
        ):
            pdh = build.return_value
            pdh.sample.side_effect = [None, 34.5]
            self.assertIsNone(reader.sample(0))
            self.assertEqual(reader.sample(1), 34.5)
            single_group.assert_not_called()
            build.assert_called_once()
            reader.close()
            pdh.close.assert_called_once()

    def test_unavailable_pdh_does_not_report_partial_usage(self):
        reader = _CpuReader()
        self.addCleanup(reader.close)
        with (
            patch("winrunner.winprocess._GetActiveProcessorGroupCount", return_value=2),
            patch("winrunner.winprocess._GetSystemTimes") as single_group,
            patch(
                "winrunner.winprocess._PdhCpu", side_effect=OSError("unavailable")
            ) as build,
        ):
            self.assertIsNone(reader.sample(0))
            self.assertIsNone(reader.sample(59))
            build.assert_called_once()
            self.assertIsNone(reader.sample(60))
            self.assertEqual(build.call_count, 2)
            single_group.assert_not_called()


if __name__ == "__main__":
    unittest.main()
