"""Regression tests for Windows project path containment."""

import ctypes
import os
import struct
import sys
import tempfile
import unittest
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from unittest.mock import patch

import winfiles
from winfiles import BadPath, pinned_entry, pinned_path


@unittest.skipUnless(os.name == "nt", "Windows handles required")
class WinFilesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name)
        self.root = self.parent / "project"
        self.root.mkdir()

    def test_invalid_names(self):
        for name in (
            "/escape", "\\escape", "C:drive-relative", "C:/drive-absolute", "\\\\server\\share",
            "\\\\?\\C:\\foo", "../escape", "dir\\..\\escape", "./../escape",
            "dir/con.txt", "NUL .txt", "dir/Com².log", "LPT9", "CONIN$", "aux",
            "dir/stream:ads", "dir/trailing.", "trailing /child", "file\0name", "f*",
        ):
            with self.subTest(name=name), self.assertRaises(BadPath):
                with pinned_entry(self.root, name):
                    self.fail("unexpectedly pinned an invalid name")

    def test_file_open_and_pins(self):
        git_dir = self.root / ".git"
        git_dir.mkdir()
        target = git_dir / "-note with spaces \N{GREEK CAPITAL LETTER OMEGA}"
        target.write_bytes(b"0123456789")
        for pin in (pinned_path, pinned_entry):
            with self.subTest(pin=pin), pin(self.root, f".git\\{target.name}") as value:
                if pin is pinned_entry:
                    self.assertEqual(value.path, target)
                    self.assertFalse(value.is_dir)
                    with value.open_file() as file:
                        self.assertEqual(os.fstat(file.fileno()).st_size, 10)
                        file.seek(3)
                        self.assertEqual(file.read(3), b"345")
                    with value.open_file() as file:
                        self.assertEqual(file.read(1), b"0")
                else:
                    self.assertEqual(value, target)
                    self.assertEqual(value.read_bytes(), b"0123456789")
                pinned_names = (self.parent, self.root, git_dir)
                if pin is pinned_path:
                    pinned_names += (target,)
                for path in pinned_names:
                    with self.assertRaises(PermissionError):
                        path.rename(path.with_name(path.name + "-renamed"))
        with pinned_entry(self.root, ".git") as entry:
            self.assertTrue(entry.is_dir)
            with self.assertRaises(PermissionError):
                git_dir.rename(git_dir.with_name("moved"))
        target.rename(target.with_name("renamed"))

    @contextmanager
    def native_handle(self, path, access, share):
        handle = winfiles._create_file(str(path), access, share, None, 3, 0, None)
        if handle == winfiles._INVALID_HANDLE_VALUE:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield handle
        finally:
            winfiles._close_handle(handle)

    def set_file_info(self, handle, info_class, info):
        setter = winfiles._kernel32.SetFileInformationByHandle
        setter.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
        setter.restype = wintypes.BOOL
        if not setter(handle, info_class, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())

    def test_download_survives_rename_and_unlink_with_shared_writer(self):
        target, rotated = self.root / "artifact", self.root / "rotated"
        old = b"original payload" * 4096
        target.write_bytes(old)
        # Rotate with a writer that permits deletion (Python's builtin open
        # does not share delete on Windows; the reader cannot change that).
        with self.native_handle(target, 0x40000000, 7):  # GENERIC_WRITE, share all
            with pinned_entry(self.root, target.name) as entry, entry.open_file() as file:
                self.assertEqual(file.read(8), old[:8])
                target.rename(rotated)
                target.write_bytes(b"new file at the original name")
                self.assertEqual(file.read(), old[8:])
                rotated.unlink()
                file.seek(0)
                self.assertEqual(file.read(), old)
        self.assertFalse(rotated.exists())
        self.assertEqual(target.read_bytes(), b"new file at the original name")

    def test_download_survives_atomic_replacement(self):
        if sys.getwindowsversion().build < 16299:
            self.skipTest("FileRenameInfoEx requires Windows 10 version 1709 or later")
        target, replacement = self.root / "artifact", self.root / "replacement"
        old, new = b"original payload" * 4096, b"new payload"
        target.write_bytes(old)
        replacement.write_bytes(new)
        target_name_bytes = len(str(target).encode("utf-16-le"))

        class FileRenameInfo(ctypes.Structure):
            _fields_ = [
                ("flags", wintypes.DWORD), ("root_directory", wintypes.HANDLE),
                ("name_bytes", wintypes.DWORD), ("name", wintypes.WCHAR * (target_name_bytes // 2 + 1)),
            ]

        # FileRenameInfoEx with REPLACE_IF_EXISTS | POSIX_SEMANTICS. This is
        # one atomic replacement, not an unlink followed by a rename. Legacy
        # MoveFileEx (os.replace) can reject any open destination on Windows.
        rename_info = FileRenameInfo(3, None, target_name_bytes, str(target))
        with pinned_entry(self.root, target.name) as entry, entry.open_file() as file:
            self.assertEqual(file.read(8), old[:8])
            with self.native_handle(replacement, 0x10000, 7) as handle:  # DELETE access
                self.set_file_info(handle, 22, rename_info)
            self.assertFalse(replacement.exists())
            with pinned_entry(self.root, target.name) as current, current.open_file() as current_file:
                self.assertEqual(current_file.read(), new)
            self.assertEqual(os.fstat(file.fileno()).st_size, len(old))
            self.assertEqual(file.read(), old[8:])

    def test_delete_pending_is_not_available_to_new_downloads(self):
        target = self.root / "artifact"
        target.write_bytes(b"old payload")
        with pinned_entry(self.root, target.name) as entry, entry.open_file() as file:
            with self.native_handle(target, 0x10000, 7) as handle:  # DELETE access
                self.set_file_info(handle, 4, wintypes.BOOL(True))  # legacy FileDispositionInfo
            with self.assertRaises(FileNotFoundError):
                with pinned_entry(self.root, target.name):
                    self.fail("unexpectedly opened a new handle to a delete-pending file")
            self.assertEqual(file.read(), b"old payload")
        self.assertFalse(target.exists())

    def test_download_with_writer_and_exclusive_sharing_violation(self):
        target = self.root / "artifact"
        with target.open("wb") as writer:
            writer.write(b"payload")
            writer.flush()
            with pinned_entry(self.root, target.name) as entry, entry.open_file() as file:
                self.assertEqual(file.read(), b"payload")
        with self.native_handle(target, 0x40000000, 0):  # GENERIC_WRITE, exclusive
            with self.assertRaises(OSError) as error:
                with pinned_entry(self.root, target.name):
                    self.fail("unexpectedly opened an exclusively held file")
            self.assertEqual(error.exception.winerror, 32)

    def test_directory_inventory_and_missing(self):
        (self.root / ".git").mkdir()
        # Force the handle-based listing to query multiple buffers.
        for i in range(650):
            (self.root / f"file {i:04d}").write_bytes(b"abc")
        for spelling in ("", "."):
            with pinned_entry(self.root, spelling) as entry:
                self.assertTrue(entry.is_dir)
                self.assertEqual(entry.path, self.root)
                entries = entry.list_entries()
                self.assertEqual(len(entries), 651)
                by_name = {item["name"]: item for item in entries}
                self.assertEqual(by_name[".git"]["type"], "directory")
                self.assertIsNone(by_name[".git"]["size_bytes"])
                for i in range(650):
                    name = f"file {i:04d}"
                    self.assertEqual(by_name[name], {"name": name, "type": "file", "size_bytes": 3})
                self.assertEqual(entry.list_entries(), entries)
        with self.assertRaises(FileNotFoundError):
            with pinned_entry(self.root, "missing/path"):
                self.fail("unexpectedly pinned a missing file")

    def test_reparse_rejection_and_in_place_conversion(self):
        outside = self.parent / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"secret string")
        leaf = self.root / "leaf"
        leaf.mkdir()

        # A junction uses a mount-point reparse tag and normally needs no symlink
        # privilege. Use a temporary, ordinary local volume; skip if unavailable.
        io_control = winfiles._kernel32.DeviceIoControl
        io_control.argtypes = (
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID,
        )
        io_control.restype = wintypes.BOOL
        flags = winfiles._FILE_FLAG_BACKUP_SEMANTICS | winfiles._FILE_FLAG_OPEN_REPARSE_POINT
        handle = winfiles._create_file(str(leaf), 0x100, 7, None, 3, flags, None)
        if handle == winfiles._INVALID_HANDLE_VALUE:
            self.skipTest(f"Cannot open temporary junction candidate: WinError {ctypes.get_last_error()}")
        self.addCleanup(winfiles._close_handle, handle)
        sub = ("\\??\\" + str(outside)).encode("utf-16-le")
        visible = str(outside).encode("utf-16-le")
        names = struct.pack("<HHHH", 0, len(sub), len(sub) + 2, len(visible)) + sub + b"\0\0" + visible + b"\0\0"
        mount_point = struct.pack("<IHH", 0xA0000003, len(names), 0) + names

        junction_present = False

        def make_junction():
            nonlocal junction_present
            if not io_control(
                handle, 0x900A4, mount_point, len(mount_point), None, 0,
                ctypes.byref(wintypes.DWORD()), None,
            ):
                self.skipTest(f"Cannot install junction: WinError {ctypes.get_last_error()}")
            junction_present = True

        def clear_junction():
            nonlocal junction_present
            if junction_present:
                data = struct.pack("<IHH", 0xA0000003, 0, 0)
                self.assertTrue(io_control(
                    handle, 0x900AC, data, len(data), None, 0, ctypes.byref(wintypes.DWORD()), None,
                ))
                junction_present = False

        self.addCleanup(clear_junction)
        with pinned_entry(self.root, "leaf") as entry:
            self.assertEqual(entry.list_entries(), [])
            make_junction()
            self.assertEqual(list(leaf.iterdir()), [leaf / "secret.txt"])
            self.assertEqual(entry.list_entries(), [])
        with pinned_entry(self.root, "") as entry:
            self.assertEqual(entry.list_entries(), [{"name": "leaf", "type": "other", "size_bytes": None}])
        for pin in (pinned_path, pinned_entry):
            for root, name in ((self.root, "leaf"), (self.root, "leaf/secret.txt"), (leaf, ""), (leaf / "project", "")):
                with self.subTest(root=root, name=name, pin=pin), self.assertRaises(BadPath):
                    with pin(root, name):
                        self.fail("unexpectedly followed the junction")

        clear_junction()
        make_junction_into = winfiles._pin_child

        def replace_parent_before_open(parent, path, name, *, file_leaf=False):
            if name == "secret.txt":
                make_junction()
            return make_junction_into(parent, path, name, file_leaf=file_leaf)

        with patch.object(winfiles, "_pin_child", replace_parent_before_open):
            with self.assertRaises((BadPath, FileNotFoundError)):
                with pinned_entry(self.root, "leaf/secret.txt"):
                    self.fail("unexpectedly followed a junction created after checking its parent")


if __name__ == "__main__":
    unittest.main()
