import tempfile
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import desktop
import server
from test_import_identity import dna_bytes


class DesktopSettingsTest(unittest.TestCase):
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

    def test_close_behavior_persists_and_validates(self):
        self.assertEqual(server.get_close_behavior(), "ask")
        server.set_close_behavior("tray")
        self.assertEqual(server.get_close_behavior(), "tray")
        server.set_close_behavior("quit")
        self.assertEqual(server.get_close_behavior(), "quit")
        with self.assertRaises(ValueError):
            server.set_close_behavior("unknown")
        self.assertEqual(server.get_close_behavior(), "quit")

    def test_native_file_drag_uses_display_name_and_copy_effect(self):
        source = self.root / "incoming" / "Original.dna"
        source.parent.mkdir()
        source.write_bytes(dna_bytes(b"ATGC"))
        item_id = server.import_one(source)["id"]
        server.rename_plasmid(item_id, "Display name.dna")

        class FakeNative:
            def __init__(self):
                self.files = None
                self.allowed = None

            def Invoke(self, callback):
                callback()

            def DoDragDrop(self, data, allowed):
                from System.Windows.Forms import DataFormats, DragDropEffects
                self.files = list(data.GetData(DataFormats.FileDrop))
                self.allowed = allowed
                return DragDropEffects.Copy

        native = FakeNative()
        api = desktop.DesktopApi()
        api._window = SimpleNamespace(native=native)
        result = api.start_file_drag(item_id)
        self.assertEqual(result, {"ok": True, "copied": True})
        self.assertEqual(native.allowed.ToString(), "Copy")
        self.assertEqual(len(native.files), 1)
        self.addCleanup(shutil.rmtree, Path(native.files[0]).parent, ignore_errors=True)
        self.assertEqual(Path(native.files[0]).name, "Display name.dna")
        self.assertEqual(Path(native.files[0]).read_bytes(), source.read_bytes())
        self.assertNotEqual(Path(native.files[0]), server.managed_plasmid_path(item_id))


if __name__ == "__main__":
    unittest.main()
