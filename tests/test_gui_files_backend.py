"""Windows desktop file service tests with real Runner worktrees and local I/O."""

import ctypes
import io
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
import unittest
from unittest.mock import patch

from winrunner import runner
from winrunner import winfiles
from winrunner import winrunner_files
from winrunner.winrunner_files import browse, save_bytes, save_file

_PROJECT_BODY = b'{"repo_url":"https://example.invalid/repo.git"}'
_PAYLOAD = bytes(range(256)) * 1024


class _LocalRepository:
    def __init__(self, cache, worktree, url, job, base):
        self.cache, self.worktree = cache, worktree

    def create(self):
        self.cache.mkdir()
        self.worktree.mkdir()


@unittest.skipUnless(os.name == "nt", "Windows Runner and file handles required")
class GUIFileBackendTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name).resolve()
        self.exports = self.scratch / "exports"
        self.exports.mkdir()
        self.destination = self.exports / "download.bin"
        self.model = runner.Runner(self.scratch / "data")
        self.addCleanup(self.model.close)
        repo_patch = patch.object(runner, "Repository", _LocalRepository)
        repo_patch.start()
        self.addCleanup(repo_patch.stop)
        self.model.create("Demo", _PROJECT_BODY)
        self.project = self.model.command_project("Demo")
        self.source = self.project.worktree / "artifact.bin"
        self.source.write_bytes(_PAYLOAD)
        self.cancelled = Event()

    def browse(self, relative="", offset=0, **kwargs):
        return browse(self.model, "demo", relative, offset, self.cancelled, **kwargs)

    def save(
        self, relative="artifact.bin", destination=None, range_header=None, **kwargs
    ):
        return save_file(
            self.model,
            "demo",
            relative,
            self.destination if destination is None else destination,
            range_header,
            self.cancelled,
            **kwargs,
        )

    @contextmanager
    def expect_error(self, status, code):
        with self.assertRaises(runner.APIError) as caught:
            yield
        self.assertEqual(
            (caught.exception.status, caught.exception.code), (status, code)
        )

    def assert_original_output(self):
        self.assertEqual(self.destination.read_bytes(), b"original")
        self.assertEqual(list(self.exports.iterdir()), [self.destination])
        self.assertEqual(self.project.readers, {})

    def junction(self, link, target):
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if result.returncode:
            self.skipTest(f"Cannot make a directory junction: {result.stderr!r}")
        self.addCleanup(lambda: link.rmdir() if link.exists() else None)

    def test_browse_pinned_directory_and_file_metadata(self):
        folder = self.project.worktree / "sub"
        folder.mkdir()
        names = [f"Entry{i:03}" for i in range(105)]
        for name in reversed(names):
            (folder / name).touch()
        (folder / "z-folder").mkdir()

        self.assertEqual(self.browse(".")["path"], "")
        self.assertEqual(
            self.browse(".\\sub\\"),
            {
                "path": "sub",
                "entries": [
                    {"name": name, "type": "file", "size_bytes": 0}
                    for name in names[:100]
                ],
                "next_entry_offset": 100,
            },
        )
        self.assertEqual(
            self.browse("sub", 100),
            {
                "path": "sub",
                "entries": [
                    {"name": name, "type": "file", "size_bytes": 0}
                    for name in names[100:]
                ]
                + [{"name": "z-folder", "type": "directory", "size_bytes": None}],
                "next_entry_offset": None,
            },
        )
        self.assertEqual(
            self.browse("./artifact.bin", expected_scope=self.project.command_scope),
            {"path": "artifact.bin", "size_bytes": len(_PAYLOAD)},
        )
        for operation in (self.browse, self.save):
            with self.expect_error(409, "project_changed"):
                operation(expected_scope="stale")
        self.assertFalse(self.destination.exists())

    def test_save_full_bounded_open_ended_and_suffix_ranges(self):
        for header, expected in (
            (None, _PAYLOAD),
            ("bytes=2-5", _PAYLOAD[2:6]),
            (" Bytes=250- ", _PAYLOAD[250:]),
            ("bytes=-8", _PAYLOAD[-8:]),
            ("bytes=262140-500000", _PAYLOAD[262140:]),
        ):
            with self.subTest(header=header):
                self.assertEqual(self.save(range_header=header), len(expected))
                self.assertEqual(self.destination.read_bytes(), expected)
                self.assertEqual(list(self.exports.iterdir()), [self.destination])
        empty = self.source.with_name("empty")
        empty.touch()
        self.assertEqual(self.save("empty"), 0)
        self.assertEqual(self.destination.read_bytes(), b"")

    def test_bad_requests_missing_paths_and_busy_sources(self):
        self.destination.write_bytes(b"original")
        for operation in (self.browse, self.save):
            for relative, status, code in (
                ("../other", 400, "invalid_request"),
                ("artifact.bin:alternate", 400, "invalid_request"),
                ("absent", 404, "not_found"),
                ("artifact.bin/not-a-directory", 404, "not_found"),
            ):
                with self.subTest(operation=operation.__name__, relative=relative):
                    with self.expect_error(status, code):
                        operation(relative)
        with self.expect_error(400, "invalid_request"):
            self.browse(offset=-1)
        with self.expect_error(400, "invalid_request"):
            self.save(".")
        for header in ("bytes=1-0", "bytes=-0", "bytes=0-1,2-3", "bytes=262144-"):
            with (
                self.subTest(header=header),
                self.expect_error(416, "range_not_satisfiable"),
            ):
                self.save(range_header=header)

        handle = winfiles._create_file(
            str(self.source), 0x40000000, 0, None, 3, 0, None
        )
        if handle == winfiles._INVALID_HANDLE_VALUE:
            self.fail(str(ctypes.WinError(ctypes.get_last_error())))
        try:
            for operation in (self.browse, self.save):
                with self.expect_error(409, "file_busy"):
                    operation("artifact.bin")
        finally:
            winfiles._close_handle(handle)
        self.assert_original_output()

    def test_guards_runner_data_and_parent_junctions(self):
        self.model.create("Other", _PROJECT_BODY)
        other = self.model.command_project("Other")
        protected = other.worktree / "notes"
        protected.write_bytes(b"unchanged")
        cache_file = other.cache / "metadata"
        root_alias = self.scratch / "root-alias"
        self.junction(root_alias, self.model.base)
        final_junction = self.exports / "cache-junction"
        self.junction(final_junction, other.cache)
        out_alias = self.scratch / "out-alias"
        self.junction(out_alias, self.exports)

        for path in (
            self.model.base / ".lock",
            self.model.base,
            self.source,
            protected,
            cache_file,
            root_alias / "projects" / "Other" / protected.name,
            root_alias / ".lock",
            final_junction,
        ):
            with self.subTest(path=path), self.expect_error(400, "invalid_request"):
                self.save(destination=path)
        self.assertEqual(self.source.read_bytes(), _PAYLOAD)
        self.assertEqual(protected.read_bytes(), b"unchanged")
        self.assertEqual(list(other.cache.iterdir()), [])
        self.assertTrue(final_junction.exists())
        self.assertEqual(
            self.save(destination=out_alias / self.destination.name), len(_PAYLOAD)
        )
        self.assertEqual(self.destination.read_bytes(), _PAYLOAD)
        self.assertEqual(
            set(self.exports.iterdir()), {self.destination, final_junction}
        )

    def test_substituted_drive_cannot_alias_runner_data(self):
        drive = next(
            (f"{letter}:" for letter in "ZYXWVUT" if not Path(f"{letter}:/").exists()),
            None,
        )
        if drive is None:
            self.skipTest("No free drive letter for SUBST")
        result = subprocess.run(
            ["subst.exe", drive, str(self.model.base)], capture_output=True
        )
        if result.returncode:
            self.skipTest(f"Cannot create a SUBST drive: {result.stderr!r}")
        self.addCleanup(subprocess.run, ["subst.exe", drive, "/D"], check=True)
        with self.expect_error(400, "invalid_request"):
            self.save(destination=Path(f"{drive}/cache/forbidden"))
        self.assertFalse((self.model.caches_dir / "forbidden").exists())

    def test_alias_retarget_during_save_does_not_enter_runner_data(self):
        alias = self.scratch / "selected-folder"
        self.junction(alias, self.exports)
        protected = self.model.caches_dir / self.destination.name
        protected.write_bytes(b"protected")

        def chunks():
            alias.rmdir()
            self.junction(alias, self.model.caches_dir)
            yield b"bytes for the originally selected folder"

        count = save_bytes(
            self.model, alias / self.destination.name, chunks(), self.cancelled
        )
        self.assertEqual(count, len(b"bytes for the originally selected folder"))
        self.assertEqual(
            self.destination.read_bytes(), b"bytes for the originally selected folder"
        )
        self.assertEqual(protected.read_bytes(), b"protected")
        self.assertEqual(
            set(self.model.caches_dir.iterdir()), {self.project.cache, protected}
        )

    def test_racing_final_junction_is_not_followed(self):
        rename = winrunner_files._rename
        protected = self.project.worktree / "protected"
        protected.write_bytes(b"unchanged")

        def race(file, parent, destination):
            self.junction(destination, self.project.worktree)
            rename(file, parent, destination)

        with patch.object(winrunner_files, "_rename", side_effect=race):
            with self.expect_error(500, "save_failed"):
                self.save()
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(protected.read_bytes(), b"unchanged")
        self.assertEqual(list(self.exports.iterdir()), [self.destination])

    def test_guards_final_file_symlink_and_does_not_follow_renamed_leaf(self):
        target = self.project.worktree / "target"
        target.write_bytes(b"protected")
        try:
            self.destination.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"No file symlink support: {exc}")
        with self.expect_error(400, "invalid_request"):
            self.save()
        self.assertTrue(self.destination.is_symlink())
        self.assertEqual(target.read_bytes(), b"protected")

        # Introduce the symlink after final validation so the native rename
        # itself must preserve the target, whether it succeeds or fails.
        self.destination.unlink()
        rename = winrunner_files._rename

        def race(file, parent, destination):
            destination.symlink_to(target)
            return rename(file, parent, destination)

        with patch.object(winrunner_files, "_rename", side_effect=race):
            try:
                self.save()
            except runner.APIError as exc:
                self.assertEqual(exc.code, "save_failed")
        self.assertEqual(target.read_bytes(), b"protected")
        self.assertEqual(list(self.exports.iterdir()), [self.destination])

    def test_local_save_and_source_errors_leave_existing_destination_intact(self):
        self.destination.write_bytes(b"original")
        with self.assertRaisesRegex(runner.APIError, "Could not save to") as caught:
            self.save(destination=self.exports / "missing" / "output")
        self.assertEqual(caught.exception.code, "save_failed")
        with patch(
            "winrunner.winrunner_files.os.fsync", side_effect=OSError("disk is full")
        ):
            with self.assertRaisesRegex(runner.APIError, "disk is full") as caught:
                self.save()
        self.assertEqual(caught.exception.code, "save_failed")
        self.assert_original_output()

        def broken_chunks():
            yield b"new data"
            raise RuntimeError("source iterator failed")

        with self.assertRaisesRegex(RuntimeError, "source iterator failed"):
            save_bytes(self.model, self.destination, broken_chunks(), self.cancelled)
        self.assert_original_output()
        self.assertEqual(
            save_bytes(
                self.model, self.destination, [b"one", b"", b"two"], self.cancelled
            ),
            6,
        )
        self.assertEqual(self.destination.read_bytes(), b"onetwo")

    def test_premature_eof_fails_but_short_reads_can_complete(self):
        self.destination.write_bytes(b"original")
        open_file = winfiles._PinnedEntry.open_file

        @contextmanager
        def short_reads(entry):
            with open_file(entry) as file:
                read = file.read
                with patch.object(
                    file, "read", side_effect=lambda count: read(min(count, 3))
                ):
                    yield file

        @contextmanager
        def interrupted_reads(entry):
            with open_file(entry) as file:
                with patch.object(file, "read", side_effect=[b"123", b""]):
                    yield file

        with patch.object(winfiles._PinnedEntry, "open_file", interrupted_reads):
            with self.expect_error(500, "file_read_failed"):
                self.save()
        self.assert_original_output()
        with patch.object(winfiles._PinnedEntry, "open_file", short_reads):
            self.assertEqual(self.save(range_header="bytes=0-7"), 8)
        self.assertEqual(self.destination.read_bytes(), _PAYLOAD[:8])

    def test_pre_cancel_and_cancel_from_runner_project_deletion(self):
        self.destination.write_bytes(b"original")
        self.cancelled.set()
        for operation in (self.browse, self.save):
            with self.expect_error(409, "cancelled"):
                operation()
        self.assert_original_output()
        self.cancelled.clear()

        entered = Event()
        open_file = winfiles._PinnedEntry.open_file

        @contextmanager
        def paused_reads(entry):
            with open_file(entry) as file:
                read = file.read

                def step(count):
                    if file.tell():
                        entered.set()
                        if not self.cancelled.wait(3):
                            raise AssertionError(
                                "Runner did not cancel its file reader"
                            )
                    return read(count)

                with patch.object(file, "read", side_effect=step):
                    yield file

        with (
            ThreadPoolExecutor(max_workers=1) as pool,
            patch.object(winfiles._PinnedEntry, "open_file", paused_reads),
        ):
            saving = pool.submit(self.save)
            self.assertTrue(entered.wait(3), "saving did not reach the second chunk")
            self.model.delete_project("demo")
            with self.expect_error(409, "cancelled"):
                saving.result(timeout=3)
        self.assertTrue(self.cancelled.is_set())
        self.assertFalse(self.project.worktree.exists())
        self.assert_original_output()

    def temp_name(self, destination=None):
        """Return the one private temporary export name beside the destination."""
        destination = self.destination if destination is None else destination
        names = [path.name for path in self.exports.iterdir() if path != destination]
        self.assertEqual(len(names), 1, names)
        return names[0]

    def test_failed_temp_deletion_is_reported_and_keeps_the_final_file(self):
        self.destination.write_bytes(b"original")

        def broken_chunks():
            yield b"partial bytes"
            raise RuntimeError("source iterator failed")

        # A discard that fails used to be silent and could hide a leaked
        # temporary file. Report it, still release every handle, and keep the
        # original export error authoritative.
        with (
            patch.object(
                winrunner_files,
                "_discard_temp",
                side_effect=OSError("cannot discard temporary export"),
            ),
            self.assertLogs(winrunner_files.LOG, level="ERROR") as logs,
        ):
            with self.assertRaisesRegex(RuntimeError, "source iterator failed"):
                save_bytes(
                    self.model, self.destination, broken_chunks(), self.cancelled
                )
        self.assertIn("temporary export", "\n".join(logs.output))
        self.assertIn(self.destination.name, "\n".join(logs.output))
        self.assertEqual(self.destination.read_bytes(), b"original")
        # Handles were released despite the failed delete, so the leaked
        # private name can be removed and nothing else was touched.
        leftover = self.exports / self.temp_name()
        leftover.unlink()
        self.assert_original_output()

    def test_cleanup_diagnostics_never_replace_the_original_error(self):
        self.destination.write_bytes(b"original")
        fail = RuntimeError("source iterator failed")
        closed = []

        def broken_chunks():
            yield b"partial bytes"
            raise fail

        original_close = winrunner_files._close_temp

        def tracking_close(file, fd, handle, destination, *, discard):
            original_close(file, fd, handle, destination, discard=discard)
            closed.append(fd)

        with (
            patch.object(
                winrunner_files,
                "_discard_temp",
                side_effect=OSError("cannot discard temporary export"),
            ),
            patch.object(winrunner_files, "_close_temp", side_effect=tracking_close),
            self.assertLogs(winrunner_files.LOG, level="ERROR") as logs,
        ):
            with self.assertRaises(RuntimeError) as caught:
                save_bytes(
                    self.model, self.destination, broken_chunks(), self.cancelled
                )
        self.assertIs(caught.exception, fail)
        self.assertEqual(len(closed), 1)
        self.assertIn("temporary export", "\n".join(logs.output))
        self.assertEqual(self.destination.read_bytes(), b"original")
        (self.exports / self.temp_name()).unlink()
        self.assert_original_output()

    def test_file_close_failure_still_closes_the_descriptor_and_reports(self):
        # Exercise the cleanup helper directly: only the file object fails, so
        # the descriptor must still be released and the leak reported.
        class BrokenFile(io.BytesIO):
            def close(self) -> None:
                raise OSError("close failed")

        with winfiles._walk(self.exports, "", relative_handles=True) as (_, parent, _):
            handle = winrunner_files._open_temp(parent, self.destination)
            fd = winrunner_files.msvcrt.open_osfhandle(
                handle, os.O_WRONLY | os.O_BINARY | os.O_NOINHERIT
            )
            with self.assertLogs(winrunner_files.LOG, level="ERROR") as logs:
                winrunner_files._close_temp(
                    BrokenFile(), fd, handle, self.destination, discard=False
                )
            self.assertIn(str(self.destination), "\n".join(logs.output))
            with self.assertRaises(OSError):
                os.fstat(fd)
            # Cleanup only ever touched the private temporary name.
            self.assertFalse(self.destination.exists())
            self.assertEqual(len(list(self.exports.iterdir())), 1)

    def test_committed_export_succeeds_when_cleanup_discards_nothing(self):
        # The normal path: a committed export never leaves a temporary name and
        # never logs a cleanup complaint.
        with patch.object(winrunner_files, "_discard_temp") as discard:
            self.assertEqual(
                save_bytes(self.model, self.destination, [b"payload"], self.cancelled),
                7,
            )
        discard.assert_not_called()
        self.assertEqual(self.destination.read_bytes(), b"payload")
        self.assertEqual(list(self.exports.iterdir()), [self.destination])
        self.assertEqual(self.project.readers, {})


if __name__ == "__main__":
    unittest.main()
