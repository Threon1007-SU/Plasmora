import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from repository_watch import RepositoryWatcher, WindowsDirectoryWatch, path_key
from test_stability import dna_bytes


class RepositoryWatchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in (
            ("LOCAL_DATA", self.root / "data"),
            ("DB_PATH", self.root / "data" / "library.sqlite3"),
            ("DEFAULT_STORAGE", self.root / "repository"),
            ("STORAGE_ROOT", self.root / "repository"),
            ("SOURCE_ROOT", self.root),
            ("LEGACY_ROOT", None),
        ):
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        server.init_db()
        self.monitor = RepositoryWatcher()

    def import_file(self, name, feature="Old"):
        source = self.root / name
        source.write_bytes(dna_bytes(b"ATGCATGC", feature))
        item = server.import_one(source)
        return item["id"], server.managed_plasmid_path(item["id"])

    def test_debounced_edit_checks_only_changed_original(self):
        item_id, path = self.import_file("target.dna")
        self.import_file("untouched.dna")
        path.write_bytes(dna_bytes(b"ATGCATGCA", "New"))
        with patch("repository_watch.time.monotonic", return_value=10):
            self.monitor.queue_path(path)
            self.monitor.queue_path(path)  # One save can produce many notifications.
        with patch.object(server, "sync_plasmid_if_changed", wraps=server.sync_plasmid_if_changed) as sync:
            self.monitor.process_pending(now=11)
            sync.assert_not_called()
            self.monitor.process_pending(now=12)
            sync.assert_called_once_with(item_id)
        self.assertEqual(self.monitor.take_updates(), {"updated": [item_id], "errors": []})
        self.assertIn("New", server.get_plasmids()[0]["tags"])
        self.assertEqual(self.monitor.take_updates(), {"updated": [], "errors": []})
        self.monitor.queue_path(path)
        self.monitor.process_pending(now=float("inf"))
        self.assertEqual(self.monitor.take_updates(), {"updated": [], "errors": []})

    def test_temporary_save_failure_retries_without_prompt(self):
        item_id, path = self.import_file("target.dna")
        path.write_bytes(b"partially saved")
        self.monitor.queue_path(path)
        self.monitor.process_pending(now=float("inf"))
        self.assertEqual(self.monitor.take_updates(), {"updated": [], "errors": []})
        self.assertIn("Old", server.get_plasmids()[0]["tags"])
        path.write_bytes(dna_bytes(b"ATGCATGCA", "New"))
        self.monitor.process_pending(now=float("inf"))
        self.assertEqual(self.monitor.take_updates(), {"updated": [item_id], "errors": []})

    def test_persistent_error_reported_once_and_unmanaged_files_ignored(self):
        _, path = self.import_file("target.dna")
        path.unlink()
        self.monitor.queue_path(path)
        self.monitor.queue_path(self.root / "not-imported.dna")
        for _ in range(4):
            self.monitor.process_pending(now=float("inf"))
        self.assertEqual(len(self.monitor.take_updates()["errors"]), 1)
        self.monitor.queue_path(path)
        for _ in range(4):
            self.monitor.process_pending(now=float("inf"))
        self.assertEqual(self.monitor.take_updates(), {"updated": [], "errors": []})

    def test_startup_and_migration_rebind_without_reparsing_unchanged_files(self):
        self.import_file("target.dna")
        ready = threading.Event()
        watches = []

        class FakeWatch:
            def __init__(self, root, changed, error):
                self.root, self.changed, self.error, self.closed = root, changed, error, False
                watches.append(self)
                ready.set()

            def close(self):
                self.closed = True

        monitor = RepositoryWatcher(factory=FakeWatch, debounce=0)
        self.addCleanup(monitor.stop)
        with patch.object(server, "parse_dna", wraps=server.parse_dna) as parse:
            monitor.start()
            self.assertTrue(ready.wait(5))
            ready.clear()
            server.set_storage_directory(self.root / "migrated")
            self.assertTrue(ready.wait(5))
            self.assertEqual(path_key(watches[-1].root), path_key(server.STORAGE_ROOT))
            self.assertTrue(watches[0].closed)
            monitor.stop()
            parse.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows native file notifications")
    def test_native_atomic_save_triggers_targeted_update(self):
        item_id, path = self.import_file("target.dna")
        notified = threading.Event()

        def changed(event_path):
            self.monitor.queue_path(event_path)
            if path_key(event_path) == path_key(path):
                notified.set()

        watch = WindowsDirectoryWatch(server.STORAGE_ROOT, changed, lambda message: None)
        self.addCleanup(watch.close)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(dna_bytes(b"ATGCATGCA", "New"))
        os.replace(temporary, path)
        self.assertTrue(notified.wait(5), "Windows did not report the saved original")
        self.monitor.process_pending(now=float("inf"))
        self.assertEqual(self.monitor.take_updates(), {"updated": [item_id], "errors": []})

    def test_stale_parse_cannot_record_newer_file_timestamp(self):
        item_id, path = self.import_file("target.dna")
        stale = server.parse_dna(path)
        path.write_bytes(dna_bytes(b"ATGCATGCA", "New"))
        with self.assertRaisesRegex(ValueError, "仍在写入"):
            server.sync_plasmid(item_id, stale)
        self.assertIn("Old", server.get_plasmids()[0]["tags"])


if __name__ == "__main__":
    unittest.main()
