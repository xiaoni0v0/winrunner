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
    from winprocess import Job, JobCancelled, _PdhCpu, health


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

    def test_health_native_metrics(self):
        with (
            patch("winprocess._previous_cpu_times", None),
            patch("winprocess._GetActiveProcessorGroupCount", return_value=1),
        ):
            first = health(self.cwd, 3.5)
            self.assertEqual(first["uptime_seconds"], 3.5)
            self.assertGreater(first["cpu"]["logical_cores"], 0)
            self.assertIsNone(first["cpu"]["usage_percent"])
            for key in ("disk", "memory"):
                self.assertGreater(first[key]["total_bytes"], 0)
                self.assertGreaterEqual(
                    first[key]["total_bytes"], first[key]["available_bytes"]
                )
            time.sleep(0.08)
            usage = health(self.cwd, 4)["cpu"]["usage_percent"]
            self.assertIsNotNone(usage)
            self.assertTrue(0 <= usage <= 100)

    def test_health_multi_group_uses_native_total_pdh_counter(self):
        try:
            sampler = _PdhCpu()
        except (OSError, AttributeError) as error:
            self.skipTest(f"host performance counters unavailable: {error}")
        self.addCleanup(sampler.close)
        with (
            patch("winprocess._pdh_cpu", sampler),
            patch("winprocess._GetActiveProcessorGroupCount", return_value=2),
        ):
            self.assertIsNone(health(self.cwd, 0)["cpu"]["usage_percent"])
            self.assertIsNone(health(self.cwd, 0)["cpu"]["usage_percent"])
            time.sleep(1.05)
            usage = health(self.cwd, 1)["cpu"]["usage_percent"]
            self.assertIsNotNone(usage)
            self.assertTrue(0 <= usage <= 100)
            self.assertEqual(health(self.cwd, 1)["cpu"]["usage_percent"], usage)

    def test_health_unavailable_pdh_does_not_report_partial_usage(self):
        with (
            patch("winprocess._pdh_cpu", None),
            patch("winprocess._pdh_retry_after", 0),
            patch("winprocess._GetActiveProcessorGroupCount", return_value=2),
            patch("winprocess._PdhCpu", side_effect=OSError("unavailable")) as build,
        ):
            self.assertIsNone(health(self.cwd, 0)["cpu"]["usage_percent"])
            self.assertIsNone(health(self.cwd, 0)["cpu"]["usage_percent"])
            build.assert_called_once()


if __name__ == "__main__":
    unittest.main()
