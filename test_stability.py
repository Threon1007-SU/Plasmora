import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server


def dna_bytes(sequence, feature):
    cookie = b"\x09" + (14).to_bytes(4, "big") + b"SnapGene" + b"\x00" * 6
    sequence_packet = b"\x00" + (len(sequence) + 1).to_bytes(4, "big") + b"\x01" + sequence
    xml = f'<Features><Feature name="{feature}" type="CDS"><Segment range="1-{len(sequence)}"/></Feature></Features>'.encode()
    feature_packet = b"\x0a" + len(xml).to_bytes(4, "big") + xml
    return cookie + sequence_packet + feature_packet


class StabilityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "repository"
        for name, value in (
            ("LOCAL_DATA", self.root / "data"),
            ("DB_PATH", self.root / "data" / "library.sqlite3"),
            ("DEFAULT_STORAGE", self.store),
            ("STORAGE_ROOT", self.store),
            ("SOURCE_ROOT", self.root),
            ("LEGACY_ROOT", None),
        ):
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        server.init_db()

    def import_file(self, name, feature):
        source = self.root / "source" / name
        source.parent.mkdir(exist_ok=True)
        source.write_bytes(dna_bytes(b"ATGCATGC", feature))
        return server.import_one(source)

    def test_external_edit_updates_search_tags_and_hash(self):
        item = self.import_file("target.dna", "IP3R1")
        path = server.managed_plasmid_path(item["id"])
        original = server.get_plasmids()[0]
        self.assertIn("IP3R1", original["tags"])
        path.write_bytes(dna_bytes(b"ATGCATGCA", "ITPR1"))
        result = server.sync_changed_plasmids()
        self.assertEqual(result["updated"], [item["id"]])
        self.assertEqual(result["errors"], [])
        changed = server.get_plasmids()[0]
        self.assertIn("ITPR1", changed["tags"])
        self.assertNotIn("IP3R1", changed["tags"])
        self.assertNotEqual(changed["sha256"], original["sha256"])
        self.assertEqual(server.sync_changed_plasmids()["updated"], [])

    def test_delete_and_restore_preserve_metadata_and_file(self):
        item = self.import_file("target.dna", "ITPR1")
        with server.db() as c:
            c.execute("UPDATE library_plasmids SET note='lab note',favorite=1 WHERE id=?", (item["id"],))
            group_id = c.execute("INSERT INTO groups(name,created_at) VALUES('signal','2026-09-30')").lastrowid
            c.execute("INSERT INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)", (item["id"], group_id))
        content = server.managed_plasmid_path(item["id"]).read_bytes()
        deleted = server.delete_plasmid(item["id"])
        self.assertEqual(server.get_plasmids(), [])
        self.assertEqual(len(server.get_trash()), 1)
        restored = server.restore_trash(deleted["trashId"])
        record = server.get_plasmids()[0]
        self.assertEqual(restored["id"], item["id"])
        self.assertEqual(record["note"], "lab note")
        self.assertTrue(record["favorite"])
        self.assertEqual(record["groups"], ["signal"])
        self.assertEqual(record["tags"], ["ITPR1"])
        self.assertEqual(server.managed_plasmid_path(item["id"]).read_bytes(), content)
        self.assertEqual(server.get_trash(), [])

    def test_cancelled_migration_keeps_original_files_and_paths(self):
        first = self.import_file("first.dna", "A")
        second = self.import_file("second.dna", "B")
        original_paths = [server.managed_plasmid_path(i["id"]) for i in (first, second)]
        target = self.root / "new-repository"
        cancel = lambda: any(target.glob("*.dna"))
        with self.assertRaises(server.OperationCancelled):
            server.set_storage_directory(target, cancel=cancel)
        self.assertEqual(server.STORAGE_ROOT, self.store)
        self.assertEqual([server.managed_plasmid_path(i["id"]) for i in (first, second)], original_paths)
        self.assertFalse(any(target.glob("*.dna")))
        self.assertTrue(all(path.exists() for path in original_paths))

    def test_restore_creates_rollback_backup(self):
        item = self.import_file("target.dna", "A")
        backup = self.root / "first.plasmora"
        server.backup_library(backup)
        server.update_plasmid_note(item["id"], "changed later")
        server.restore_backup(backup)
        self.assertEqual(server.get_plasmids()[0]["note"], "")
        rollback = server.list_rollback_backups()
        self.assertEqual(len(rollback), 1)
        server.restore_backup(server.LOCAL_DATA / "rollback" / rollback[0]["name"])
        self.assertEqual(server.get_plasmids()[0]["note"], "changed later")

    def test_cancelled_export_keeps_existing_zip(self):
        first = self.import_file("first.dna", "A")
        second = self.import_file("second.dna", "B")
        destination = self.root / "export.zip"
        destination.write_bytes(b"previous export")
        with self.assertRaises(server.OperationCancelled):
            server.export_plasmids([first["id"], second["id"]], destination,
                                   cancel=lambda: True)
        self.assertEqual(destination.read_bytes(), b"previous export")
        self.assertEqual(list(self.root.glob(".plasmora-archive-*")), [])


if __name__ == "__main__":
    unittest.main()
