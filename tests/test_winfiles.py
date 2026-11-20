"""Regression tests for Windows project path containment."""

import ctypes
import os
import struct
import tempfile
import unittest
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
                for path in (self.parent, self.root, git_dir, target):
                    with self.assertRaises(PermissionError):
                        path.rename(path.with_name(path.name + "-renamed"))
        target.rename(target.with_name("renamed"))

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

        def replace_parent_before_open(parent, path, name):
            if name == "secret.txt":
                make_junction()
            return make_junction_into(parent, path, name)

        with patch.object(winfiles, "_pin_child", replace_parent_before_open):
            with self.assertRaises((BadPath, FileNotFoundError)):
                with pinned_entry(self.root, "leaf/secret.txt"):
                    self.fail("unexpectedly followed a junction created after checking its parent")


if __name__ == "__main__":
    unittest.main()
