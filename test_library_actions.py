import json
import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


class LibraryActionsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = root / "repository"
        self.patches = [
            patch.object(server, "LOCAL_DATA", root / "data"),
            patch.object(server, "DB_PATH", root / "data" / "library.sqlite3"),
            patch.object(server, "DEFAULT_STORAGE", self.store),
            patch.object(server, "STORAGE_ROOT", self.store),
            patch.object(server, "SOURCE_ROOT", root),
            patch.object(server, "LEGACY_ROOT", None),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.temp.cleanup)
        server.init_db()
        self.first = self.store / "first.dna"
        self.second = self.store / "second.dna"
        self.first.write_bytes(b"original-one")
        self.second.write_bytes(b"original-two")
        with server.db() as c:
            for name, path in (("first.dna", self.first), ("second.dna", self.second)):
                c.execute("INSERT INTO library_plasmids(file_name,stored_name,storage_path,sha256,file_size,imported_at) VALUES(?,?,?,?,?,?)",
                          (name, name, str(path), hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size, "2026-09-27"))
            c.execute("INSERT INTO groups(name,created_at) VALUES('old group','2026-09-27')")
            c.execute("INSERT INTO plasmid_groups(plasmid_id,group_id) VALUES(1,1)")
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        req = Request(f"http://127.0.0.1:{self.httpd.server_port}{path}", data=payload, method=method,
                      headers={"Content-Type": "application/json"})
        with urlopen(req) as response:
            return json.load(response)

    def test_rename_group_membership_and_delete(self):
        self.assertEqual(self.request("PATCH", "/api/plasmids/1", {"name": "renamed"})["name"], "renamed.dna")
        self.assertEqual(self.first.read_bytes(), b"original-one")
        self.assertEqual(self.request("PATCH", "/api/groups/1", {"name": "new group"})["name"], "new group")
        self.request("POST", "/api/groups/1/members", {"plasmid_ids": [2]})
        items = self.request("GET", "/api/plasmids")["plasmids"]
        self.assertEqual(next(p for p in items if p["id"] == 1)["groupIds"], [])
        self.assertEqual(next(p for p in items if p["id"] == 2)["groups"], ["new group"])
        self.request("DELETE", "/api/plasmids/1")
        self.assertFalse(self.first.exists())
        self.assertTrue(self.second.exists())
        self.assertEqual(len(self.request("GET", "/api/plasmids")["plasmids"]), 1)
        self.request("DELETE", "/api/groups/1")
        self.assertTrue(self.second.exists())
        self.assertEqual(self.request("GET", "/api/plasmids")["plasmids"][0]["groupIds"], [])

    def test_invalid_rename_and_membership_do_not_change_data(self):
        with self.assertRaises(HTTPError):
            self.request("PATCH", "/api/plasmids/1", {"name": "../outside.dna"})
        with self.assertRaises(HTTPError):
            self.request("POST", "/api/groups/1/members", {"plasmid_ids": [999]})
        self.assertEqual(self.request("GET", "/api/plasmids")["plasmids"][0]["groupIds"], [1])
        self.assertTrue(self.first.exists())

    def test_note_and_sort_setting_persist_without_changing_dna(self):
        note = "来源：JH 系列\n待复核测序结果"
        self.assertEqual(self.request("PATCH", "/api/plasmids/1/note", {"note": note})["note"], note)
        self.assertEqual(self.request("GET", "/api/plasmids")["plasmids"][0]["note"], note)
        self.assertEqual(self.first.read_bytes(), b"original-one")
        with self.assertRaises(HTTPError):
            self.request("PATCH", "/api/plasmids/1/note", {"note": "x" * 10001})
        self.assertEqual(self.request("GET", "/api/plasmids")["plasmids"][0]["note"], note)
        self.request("POST", "/api/settings/sort", {"sort": "size_large"})
        self.assertEqual(self.request("GET", "/api/settings/sort")["sort"], "size_large")
        with self.assertRaises(HTTPError):
            self.request("POST", "/api/settings/sort", {"sort": "unknown"})
        self.assertEqual(self.request("GET", "/api/settings/sort")["sort"], "size_large")

    def test_favorite_and_recent_view_persist(self):
        self.request("PATCH", "/api/plasmids/1/favorite", {"favorite": True})
        self.request("POST", "/api/plasmids/1/view", {})
        first = next(item for item in self.request("GET", "/api/plasmids")["plasmids"] if item["id"] == 1)
        self.assertTrue(first["favorite"])
        self.assertTrue(first["lastViewedAt"])
        with self.assertRaises(HTTPError):
            self.request("PATCH", "/api/plasmids/1/favorite", {"favorite": "yes"})
        self.assertTrue(next(item for item in self.request("GET", "/api/plasmids")["plasmids"] if item["id"] == 1)["favorite"])


if __name__ == "__main__":
    unittest.main()
