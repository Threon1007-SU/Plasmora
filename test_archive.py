import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import server


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.store = root / "repository"
        for name, value in (
            ("LOCAL_DATA", root / "data"),
            ("DB_PATH", root / "data" / "library.sqlite3"),
            ("DEFAULT_STORAGE", self.store),
            ("STORAGE_ROOT", self.store),
            ("SOURCE_ROOT", root),
            ("LEGACY_ROOT", None),
        ):
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        server.init_db()
        self.files = []
        with server.db() as c:
            c.execute("INSERT INTO groups(name,created_at) VALUES('质粒组','2026-09-28')")
            for index, content in enumerate((b"first plasmid", b"second plasmid"), 1):
                path = self.store / f"plasmid-{index}.dna"
                path.write_bytes(content)
                self.files.append(path)
                digest = hashlib.sha256(content).hexdigest()
                c.execute("INSERT INTO library_plasmids(id,file_name,stored_name,storage_path,sha256,file_size,imported_at,note,favorite,last_viewed_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (index, path.name, path.name, str(path), digest, len(content), "2026-09-28", f"实验备注 {index}", index == 1, "2026-09-28T08:00:00" if index == 1 else None))
                c.execute("INSERT INTO plasmid_tags(plasmid_id,tag,tag_kind) VALUES(?,?,?)", (index, f"Feature {index}", "feature"))
            c.execute("INSERT INTO plasmid_groups(plasmid_id,group_id) VALUES(1,1)")

    def test_backup_export_restore_round_trip(self):
        target = Path(self.temp.name) / "backup.plasmora"
        self.assertEqual(server.backup_library(target)["count"], 2)
        self.assertEqual(server.inspect_backup(target)["count"], 2)

        exported = Path(self.temp.name) / "selected.zip"
        self.assertEqual(server.export_plasmids([1], exported)["count"], 1)
        with zipfile.ZipFile(exported) as archive:
            self.assertEqual(archive.read("质粒/plasmid-1.dna"), b"first plasmid")
            self.assertNotIn("质粒/plasmid-2.dna", archive.namelist())
            rows = list(csv.DictReader(io.StringIO(archive.read("质粒清单.csv").decode("utf-8-sig"))))
            self.assertEqual(rows[0]["备注"], "实验备注 1")
            self.assertEqual(rows[0]["所属分组"], "质粒组")

        with server.db() as c:
            c.execute("UPDATE library_plasmids SET note='changed',favorite=0 WHERE id=1")
            c.execute("DELETE FROM library_plasmids WHERE id=2")
            c.execute("DELETE FROM groups")
        self.assertEqual(server.restore_backup(target)["count"], 2)
        records = server.get_plasmids()
        self.assertEqual(len(records), 2)
        first = next(item for item in records if item["id"] == 1)
        self.assertEqual(first["note"], "实验备注 1")
        self.assertTrue(first["favorite"])
        self.assertEqual(first["groups"], ["质粒组"])
        self.assertEqual(first["tags"], ["Feature 1"])
        self.assertEqual(len(server.get_synonym_clusters()), 1)
        self.assertEqual(server.managed_plasmid_path(1).read_bytes(), b"first plasmid")
        self.assertEqual(server.managed_plasmid_path(2).read_bytes(), b"second plasmid")

    def test_corrupt_backup_and_failed_swap_preserve_current_library(self):
        target = Path(self.temp.name) / "backup.plasmora"
        server.backup_library(target)
        corrupt = Path(self.temp.name) / "corrupt.plasmora"
        with zipfile.ZipFile(target) as source, zipfile.ZipFile(corrupt, "w") as output:
            for name in source.namelist():
                content = source.read(name)
                if name == "manifest.json":
                    manifest = json.loads(content)
                    manifest["dbSha256"] = "0" * 64
                    content = json.dumps(manifest).encode()
                output.writestr(name, content)
        with self.assertRaises(ValueError):
            server.inspect_backup(corrupt)
        with self.assertRaises(ValueError):
            server.restore_backup(corrupt)
        self.assertEqual(len(server.get_plasmids()), 2)
        self.assertTrue(all(path.exists() for path in self.files))

        with patch.object(server.os, "replace", side_effect=OSError("simulated database swap failure")):
            with self.assertRaises(OSError):
                server.restore_backup(target)
        self.assertEqual(len(server.get_plasmids()), 2)
        self.assertEqual(sorted(path.name for path in self.store.iterdir()), ["plasmid-1.dna", "plasmid-2.dna"])

    def test_single_file_export_preserves_managed_copy(self):
        destination = Path(self.temp.name) / "exported.dna"
        result = server.export_one_plasmid(1, destination)
        self.assertTrue(os.path.samefile(result["path"], destination))
        self.assertEqual(destination.read_bytes(), b"first plasmid")
        self.assertEqual(self.files[0].read_bytes(), b"first plasmid")
        with self.assertRaisesRegex(ValueError, "不能覆盖仓库"):
            server.export_one_plasmid(1, self.files[1])
        self.assertEqual(self.files[1].read_bytes(), b"second plasmid")
        self.files[0].write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "不是可识别|不一致"):
            server.export_one_plasmid(1, destination)
        self.assertEqual(destination.read_bytes(), b"first plasmid")


if __name__ == "__main__":
    unittest.main()
