import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import desktop
import server
from test_import_identity import dna_bytes


class DesktopImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
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
        server.init_db()
        self.api = desktop.DesktopApi()

    def source(self, folder, name, sequence):
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(dna_bytes(sequence))
        return path

    def test_batch_pauses_for_name_conflict_then_continues(self):
        first = self.source("one", "sample.dna", b"ATGC")
        second = self.source("two", "sample.dna", b"ATGA")
        third = self.source("three", "other.dna", b"ATGT")
        pending = self.api.begin_dropped_import([first, second, third])
        self.assertTrue(pending["pending"])
        self.assertEqual((pending["position"], pending["total"]), (2, 3))
        self.assertEqual(pending["conflict"]["existing"][0]["name"], "sample.dna")
        self.assertIn("error", self.api.resolve_import("replace", 999))
        self.assertIn("error", self.api.begin_dropped_import([third]))
        result = self.api.resolve_import("copy")
        self.assertEqual(len(result["imported"]), 3)
        self.assertEqual([item["name"] for item in result["plasmids"]], ["other.dna", "sample (1).dna", "sample.dna"])

    def test_cancel_keeps_already_imported_files(self):
        first = self.source("one", "sample.dna", b"ATGC")
        second = self.source("two", "sample.dna", b"ATGA")
        self.assertTrue(self.api.begin_dropped_import([first, second])["pending"])
        result = self.api.cancel_import()
        self.assertTrue(result["cancelledRemaining"])
        self.assertEqual(len(result["imported"]), 1)
        self.assertEqual(len(server.get_plasmids()), 1)
        self.assertFalse(self.api.begin_dropped_import([self.source("three", "other.dna", b"ATGT")]).get("pending", False))


if __name__ == "__main__":
    unittest.main()
