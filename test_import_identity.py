import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import server


def dna_bytes(sequence):
    cookie = b"\x09" + (14).to_bytes(4, "big") + b"SnapGene" + b"\x00" * 6
    packet = b"\x00" + (len(sequence) + 1).to_bytes(4, "big") + b"\x01" + sequence
    return cookie + packet


class ImportIdentityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
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

    def source(self, folder, name, content):
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_duplicate_requires_matching_name_and_content(self):
        content = dna_bytes(b"ATGC")
        first = server.import_one(self.source("input-1", "a.dna", content))
        other_name = server.import_one(self.source("input-2", "b.dna", content))
        exact_repeat = server.import_one(self.source("input-3", "a.dna", content))
        other_content = server.import_one(self.source("input-4", "a.dna", dna_bytes(b"ATGA")))

        self.assertFalse(first["duplicate"])
        self.assertFalse(other_name["duplicate"])
        self.assertTrue(exact_repeat["duplicate"])
        self.assertEqual(exact_repeat["id"], first["id"])
        self.assertFalse(other_content["duplicate"])
        self.assertEqual(len(server.get_plasmids()), 3)
        self.assertEqual(len({server.managed_plasmid_path(item["id"]) for item in server.get_plasmids()}), 3)

        with self.assertRaisesRegex(ValueError, "同名且内容相同"):
            server.rename_plasmid(other_name["id"], "a.dna")
        server.rename_plasmid(other_content["id"], "b.dna")
        self.assertEqual(len(server.get_plasmids()), 3)

    def test_reimport_after_rename_keeps_separate_managed_files(self):
        content = dna_bytes(b"ATGC")
        first = server.import_one(self.source("input-1", "original.dna", content))
        server.rename_plasmid(first["id"], "renamed.dna")
        second = server.import_one(self.source("input-2", "original.dna", content))
        first_path = server.managed_plasmid_path(first["id"])
        second_path = server.managed_plasmid_path(second["id"])
        self.assertNotEqual(first_path, second_path)
        server.delete_plasmid(first["id"])
        self.assertTrue(second_path.is_file())
        self.assertEqual(second_path.read_bytes(), content)

    def test_drop_into_group_imports_and_classifies_existing_copy(self):
        content = dna_bytes(b"ATGC")
        with server.db() as c:
            group_id = c.execute("INSERT INTO groups(name,created_at) VALUES('JH','2026-09-29')").lastrowid
        source = self.source("input", "sample.dna", content)
        imported = server.import_files([source], group_id)
        self.assertEqual(len(imported["imported"]), 1)
        self.assertEqual(imported["grouped"], 1)
        self.assertEqual(server.get_plasmids()[0]["groupIds"], [group_id])

        with server.db() as c:
            c.execute("DELETE FROM plasmid_groups WHERE group_id=?", (group_id,))
        repeated = server.import_files([source], group_id)
        self.assertEqual(len(repeated["duplicates"]), 1)
        self.assertEqual(repeated["grouped"], 1)
        self.assertEqual(server.get_plasmids()[0]["groupIds"], [group_id])
        self.assertEqual(server.import_files([source], group_id)["grouped"], 0)

        with self.assertRaises(ValueError):
            server.import_files([source], -1)
        missing = server.import_files([source], 999)
        self.assertEqual(len(missing["errors"]), 1)
        self.assertEqual(len(server.get_plasmids()), 1)

    def test_same_name_decisions_mark_old_and_new_and_preserve_metadata(self):
        first_content = dna_bytes(b"ATGC")
        first = server.import_one(self.source("input-1", "same.dna", first_content))
        with server.db() as c:
            c.execute("UPDATE library_plasmids SET note='keep this note',favorite=1 WHERE id=?", (first["id"],))
            c.execute("INSERT INTO plasmid_tags(plasmid_id,tag,tag_kind) VALUES(?,'old feature','feature')", (first["id"],))
            group_id = c.execute("INSERT INTO groups(name,created_at) VALUES('JH','2026-09-29')").lastrowid
            c.execute("INSERT INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)", (first["id"], group_id))

        incoming = self.source("input-2", "same.dna", dna_bytes(b"ATGA"))
        conflict = server.inspect_import_conflict(incoming)
        self.assertEqual(conflict["incoming"]["name"], "same.dna")
        self.assertEqual(conflict["existing"][0]["id"], first["id"])
        self.assertNotEqual(conflict["incoming"]["sha256"], conflict["existing"][0]["sha256"])
        copied = server.import_one(incoming, on_conflict="copy")
        self.assertEqual(copied["name"], "same (1).dna")
        self.assertEqual(server.import_one(self.source("input-3", "same.dna", dna_bytes(b"ATGT")))["name"], "same (2).dna")

        newer = self.source("input-4", "same.dna", dna_bytes(b"ATGG"))
        kept = server.import_one(newer, on_conflict="keep_existing", existing_id=first["id"])
        self.assertTrue(kept["skipped"])
        self.assertEqual(server.managed_plasmid_path(first["id"]).read_bytes(), first_content)
        with self.assertRaises(ValueError):
            server.import_one(newer, on_conflict="replace", existing_id=999)

        replaced = server.import_one(newer, on_conflict="replace", existing_id=first["id"])
        self.assertTrue(replaced["replaced"])
        self.assertEqual(server.managed_plasmid_path(first["id"]).read_bytes(), newer.read_bytes())
        record = next(item for item in server.get_plasmids() if item["id"] == first["id"])
        self.assertEqual(record["note"], "keep this note")
        self.assertTrue(record["favorite"])
        self.assertEqual(record["groupIds"], [group_id])
        self.assertEqual(record["tags"], [])
        self.assertIsNone(server.inspect_import_conflict(newer))

    def test_existing_database_migrates_without_losing_memberships(self):
        server.DB_PATH.unlink()
        content = dna_bytes(b"ATGC")
        old_file = self.store / "old.dna"
        old_file.write_bytes(content)
        with closing(sqlite3.connect(server.DB_PATH)) as c, c:
            c.executescript("""
                CREATE TABLE library_plasmids (
                    id INTEGER PRIMARY KEY, file_name TEXT NOT NULL, stored_name TEXT NOT NULL,
                    storage_path TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
                    file_size INTEGER NOT NULL, imported_at TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '', favorite INTEGER NOT NULL DEFAULT 0,
                    last_viewed_at TEXT
                );
                CREATE TABLE plasmid_tags (
                    plasmid_id INTEGER NOT NULL REFERENCES library_plasmids(id) ON DELETE CASCADE,
                    tag TEXT NOT NULL COLLATE NOCASE, tag_kind TEXT NOT NULL,
                    PRIMARY KEY (plasmid_id,tag,tag_kind)
                );
                CREATE TABLE groups (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
                CREATE TABLE plasmid_groups (
                    plasmid_id INTEGER NOT NULL REFERENCES library_plasmids(id) ON DELETE CASCADE,
                    group_id INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                    PRIMARY KEY (plasmid_id,group_id)
                );
            """)
            c.execute("INSERT INTO library_plasmids VALUES(1,'old.dna','old.dna',?,?,?,?,?,?,?)",
                      (str(old_file), hashlib.sha256(content).hexdigest(), len(content), "2026-09-28", "saved note", 1, None))
            c.execute("INSERT INTO plasmid_tags VALUES(1,'test feature','feature')")
            c.execute("INSERT INTO groups VALUES(1,'my group','2026-09-28')")
            c.execute("INSERT INTO plasmid_groups VALUES(1,1)")
        server.init_db()
        original = server.get_plasmids()[0]
        self.assertEqual(original["note"], "saved note")
        self.assertTrue(original["favorite"])
        self.assertEqual(original["groups"], ["my group"])
        self.assertEqual(original["tags"], ["test feature"])
        added = server.import_one(self.source("input", "new-name.dna", content))
        self.assertFalse(added["duplicate"])
        with server.db() as c:
            self.assertEqual(c.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
