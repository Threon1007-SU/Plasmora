import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import server


class SynonymClusterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in (
            ("LOCAL_DATA", root / "data"),
            ("DB_PATH", root / "data" / "library.sqlite3"),
            ("DEFAULT_STORAGE", root / "repository"),
            ("STORAGE_ROOT", root / "repository"),
            ("SOURCE_ROOT", root),
            ("LEGACY_ROOT", None),
        ):
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)

    def start_server(self):
        self.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, method, path, body=None):
        import json
        from urllib.request import Request, urlopen

        payload = json.dumps(body).encode() if body is not None else None
        request = Request(f"http://127.0.0.1:{self.httpd.server_port}{path}", data=payload, method=method,
                          headers={"Content-Type": "application/json"})
        with urlopen(request) as response:
            return json.load(response)

    def test_create_edit_merge_delete(self):
        from urllib.error import HTTPError

        server.init_db()
        self.start_server()
        initial = self.request("GET", "/api/synonym-clusters")
        self.assertEqual([set(item["terms"]) for item in initial], [{"ITPR1", "IP3R1"}])

        first_id = self.request("POST", "/api/synonym-clusters", {"terms": ["Gene A", "Alias A"]})["id"]
        second_id = self.request("POST", "/api/synonym-clusters", {"terms": ["Gene B", "Alias B"]})["id"]
        self.request("PATCH", f"/api/synonym-clusters/{first_id}", {"terms": ["Gene A", "Alias A", "Gene B"]})
        clusters = self.request("GET", "/api/synonym-clusters")
        merged = next(item for item in clusters if item["id"] == first_id)
        self.assertEqual(set(merged["terms"]), {"Gene A", "Alias A", "Gene B", "Alias B"})
        self.assertNotIn(second_id, [item["id"] for item in clusters])

        self.request("PATCH", f"/api/synonym-clusters/{first_id}", {"terms": ["Gene A", "Alias B"]})
        clusters = self.request("GET", "/api/synonym-clusters")
        self.assertEqual(set(next(item for item in clusters if item["id"] == first_id)["terms"]), {"Gene A", "Alias B"})
        with self.assertRaises(HTTPError):
            self.request("PATCH", f"/api/synonym-clusters/{first_id}", {"terms": ["Only one"]})
        self.assertEqual(set(next(item for item in self.request("GET", "/api/synonym-clusters") if item["id"] == first_id)["terms"]), {"Gene A", "Alias B"})
        self.request("DELETE", f"/api/synonym-clusters/{first_id}")
        self.assertEqual(len(self.request("GET", "/api/synonym-clusters")), 1)

    def test_old_pairs_migrate_as_connected_cluster(self):
        server.LOCAL_DATA.mkdir(parents=True)
        with closing(sqlite3.connect(server.DB_PATH)) as connection:
            with connection as c:
                c.execute("CREATE TABLE aliases (id INTEGER PRIMARY KEY, canonical TEXT NOT NULL, alias TEXT NOT NULL UNIQUE COLLATE NOCASE)")
                c.executemany("INSERT INTO aliases(canonical,alias) VALUES(?,?)", [
                    ("ITPR1", "ITPR1"), ("ITPR1", "IP3R1"), ("IP3R1", "IP3 receptor"),
                    ("Gene A", "Alias A"), ("Alias A", "Alias B"),
                ])
        server.init_db()
        clusters = [set(item["terms"]) for item in server.get_synonym_clusters()]
        self.assertIn({"ITPR1", "IP3R1", "IP3 receptor"}, clusters)
        self.assertIn({"Gene A", "Alias A", "Alias B"}, clusters)
        with server.db() as c:
            self.assertFalse(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='aliases'").fetchone())
        server.init_db()
        self.assertEqual(len(server.get_synonym_clusters()), 2)

    def test_existing_plasmid_table_gets_note_column(self):
        server.LOCAL_DATA.mkdir(parents=True)
        with closing(sqlite3.connect(server.DB_PATH)) as connection:
            with connection as c:
                c.execute("CREATE TABLE library_plasmids (id INTEGER PRIMARY KEY, file_name TEXT NOT NULL, stored_name TEXT NOT NULL, storage_path TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE, file_size INTEGER NOT NULL, imported_at TEXT NOT NULL)")
                c.execute("INSERT INTO library_plasmids(file_name,stored_name,storage_path,sha256,file_size,imported_at) VALUES('old.dna','old.dna','old.dna','hash',10,'2026-09-27')")
        server.init_db()
        items = server.get_plasmids()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "old.dna")
        self.assertEqual(items[0]["note"], "")
        self.assertFalse(items[0]["favorite"])
        self.assertIsNone(items[0]["lastViewedAt"])


if __name__ == "__main__":
    unittest.main()
