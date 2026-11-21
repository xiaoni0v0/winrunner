"""HTTP contract tests for opt-in directory pagination."""

from contextlib import nullcontext
import http.client
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock

from winrunner import HTTPHandler, HTTPServer

TOKEN = "test-bearer-token"
FILES = "/projects/demo/files"


class QuietHandler(HTTPHandler):
    def log_message(self, *args):
        pass


class DirectoryPaginationTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), QuietHandler)
        self.addCleanup(self.server.server_close)
        self.server.runner = Mock()
        self.server.token = TOKEN.encode("ascii")
        self.entry = Mock(is_dir=True)
        self.server.runner.file_entry.return_value = nullcontext(self.entry)
        worker = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        worker.start()
        self.addCleanup(worker.join, 5)
        self.addCleanup(self.server.shutdown)

    def list_names(self, names):
        self.entry.list_entries.return_value = [
            {"name": name, "type": "file", "size_bytes": 1} for name in names
        ]

    def request(self, route, **headers):
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )
        try:
            connection.request(
                "GET", route, headers={"Authorization": f"Bearer {TOKEN}", **headers}
            )
            response = connection.getresponse()
            data = response.read()
            if response.headers.get_content_type() == "application/json":
                data = json.loads(data)
            return response.status, data, response.headers
        finally:
            connection.close()

    def assert_page(self, query, expected_names, next_offset, path=""):
        status, page, _ = self.request(f"{FILES}?{query}")
        self.assertEqual(status, 200)
        self.assertEqual(
            page,
            {
                "path": path,
                "entries": [
                    {"name": name, "type": "file", "size_bytes": 1}
                    for name in expected_names
                ],
                "next_entry_offset": next_offset,
            },
        )

    def test_legacy_listing_stays_unpaged(self):
        names = [f"item{index:04}" for index in range(105)]
        self.list_names(reversed(names))
        status, listing, _ = self.request(f"{FILES}?path=.")
        self.assertEqual(status, 200)
        self.assertEqual(set(listing), {"path", "entries"})
        self.assertEqual(listing["path"], "")
        self.assertEqual([entry["name"] for entry in listing["entries"]], names)

    def test_either_parameter_opts_in_with_default_for_other(self):
        names = [f"item{index:04}" for index in range(105)]
        self.list_names(reversed(names))
        self.assert_page("entry_offset=1", names[1:101], 101)
        self.assert_page("entry_limit=2", names[:2], 2)
        self.assert_page("entry_offset=101", names[101:], None)

    def test_casefold_order_and_original_name_break_ties_across_pages(self):
        names = ["z", "strasse", "b", "Straße", "STRASSE", "a"]
        self.list_names(names)
        self.assert_page(
            "path=sub%5Cdir&entry_limit=3", ["a", "b", "STRASSE"], 3, "sub/dir"
        )
        self.list_names(reversed(names))
        self.assert_page(
            "entry_offset=3&entry_limit=3", ["Straße", "strasse", "z"], None
        )

    def test_page_respects_wire_byte_limit_and_continues_without_skipping(self):
        names = [f"{index:04}" + "😀" * 125 for index in range(1000)]
        self.list_names(reversed(names))
        paths = ["深" * 100 for _ in range(3)]
        query = "path=" + "%2F".join("%E6%B7%B1" * len(part) for part in paths)
        collected: list[str] = []
        while len(collected) < len(names):
            offset = len(collected)
            status, page, headers = self.request(
                f"{FILES}?{query}&entry_offset={offset}&entry_limit=1000"
            )
            self.assertEqual(status, 200)
            self.assertLessEqual(int(headers["Content-Length"]), 512 * 1024)
            self.assertEqual(page["path"], "/".join(paths))
            self.assertTrue(page["entries"])
            collected.extend(entry["name"] for entry in page["entries"])
            if len(collected) < len(names):
                self.assertEqual(page["next_entry_offset"], len(collected))
            else:
                self.assertIsNone(page["next_entry_offset"])
        self.assertEqual(collected, names)

    def test_empty_and_out_of_bounds_pages(self):
        self.list_names([])
        self.assert_page("entry_limit=1000", [], None)
        self.list_names(["a"])
        self.assert_page("entry_offset=1", [], None)
        self.assert_page("entry_offset=100000000000000000000", [], None)

    def test_reject_invalid_values_duplicate_and_unknown_parameters(self):
        for key in ("entry_offset", "entry_limit"):
            for value in ("", "-1", "%2B1", "1.5", "one", "%EF%BC%91", "1%20"):
                with self.subTest(key=key, value=value):
                    status, body, _ = self.request(f"{FILES}?{key}={value}")
                    self.assertEqual(status, 400)
                    self.assertEqual(body["error"]["code"], "invalid_request")
                    self.assertIn(key, body["error"]["message"])
        for value in ("0", "1001"):
            with self.subTest(entry_limit=value):
                status, body, _ = self.request(f"{FILES}?entry_limit={value}")
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_request")
                self.assertIn("entry_limit", body["error"]["message"])
        for query in ("entry_offset=0&entry_offset=1", "other=0"):
            with self.subTest(query=query):
                status, body, _ = self.request(f"{FILES}?{query}")
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_request")
        self.entry.list_entries.assert_not_called()

    def test_file_rejects_pagination_and_keeps_byte_ranges(self):
        self.entry.is_dir = False
        route = f"{FILES}?path=artifact.txt"
        for query in ("entry_offset=0", "entry_limit=100"):
            with self.subTest(query=query):
                status, body, _ = self.request(f"{route}&{query}", Range="bytes=2-5")
                self.assertEqual(status, 400)
                self.assertEqual(body["error"]["code"], "invalid_request")
                self.assertIn("only for directories", body["error"]["message"])
        self.entry.open_file.assert_not_called()

        with tempfile.TemporaryFile() as file:
            file.write(b"0123456789")
            file.flush()
            self.entry.open_file.return_value = nullcontext(file)
            status, data, headers = self.request(route, Range="bytes=2-5")
            self.assertEqual((status, data), (206, b"2345"))
            self.assertEqual(headers["Content-Range"], "bytes 2-5/10")


if __name__ == "__main__":
    unittest.main()
