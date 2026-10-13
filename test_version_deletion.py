from pathlib import Path
import unittest
from unittest.mock import patch

import server
import versions
import test_versions
from test_primers import primer_dna


class VersionDeletionTest(unittest.TestCase):
    setUp = test_versions.DailyVersionsTest.setUp

    def build_history(self):
        versions.set_enabled(True)
        yesterday = versions.prepare_edit(self.item_id)
        yesterday.write_bytes(primer_dna('Yesterday-F'))
        server.sync_plasmid_if_changed(self.item_id)
        self.day = '2026-10-09'
        latest = versions.prepare_edit(self.item_id)
        latest.write_bytes(primer_dna('Latest-F'))
        server.sync_plasmid_if_changed(self.item_id)
        with server.db() as c:
            server.update_plasmid_note(self.item_id, 'keep note')
            c.execute('UPDATE library_plasmids SET favorite=1 WHERE id=?', (self.item_id,))
            group = c.execute("INSERT INTO groups(name,created_at) VALUES('group','2026-10-08')").lastrowid
            c.execute('INSERT INTO plasmid_groups(plasmid_id,group_id) VALUES(?,?)', (self.item_id, group))
            return [dict(row) for row in c.execute('SELECT * FROM plasmid_versions WHERE plasmid_id=? ORDER BY id', (self.item_id,))]

    def test_delete_historical_copy_preserves_head_and_restores_individually(self):
        rows = self.build_history()
        head = server.managed_plasmid_path(self.item_id)
        deleted = versions.delete_version(self.item_id, rows[1]['id'])
        self.assertFalse(deleted['latestChanged'])
        self.assertEqual(server.managed_plasmid_path(self.item_id), head)
        self.assertFalse(Path(rows[1]['path']).exists())
        self.assertFalse(Path(rows[1]['snapshot_path']).exists())
        self.assertEqual(len(versions.list_versions(self.item_id)), 2)
        self.assertEqual(server.get_trash()[0]['reason'], 'version_deleted')
        restored = server.restore_trash(deleted['trashId'])
        self.assertEqual(restored['kind'], 'version')
        self.assertEqual(versions.preview(self.item_id, restored['versionId'])['primers'][0]['name'], 'Yesterday-F')
        self.assertEqual(server.managed_plasmid_path(self.item_id), head)
        self.assertEqual(len(server.get_plasmids()), 1)
        self.assertEqual(server.get_trash(), [])
        record = server.get_plasmids()[0]
        self.assertEqual(record['note'], 'keep note')
        self.assertTrue(record['favorite'])
        self.assertEqual(record['groups'], ['group'])

    def test_delete_latest_reindexes_previous_copy_and_snapgene_uses_it(self):
        import desktop
        rows = self.build_history()
        deleted = versions.delete_version(self.item_id, rows[2]['id'])
        self.assertTrue(deleted['latestChanged'])
        self.assertEqual(server.managed_plasmid_path(self.item_id), Path(rows[1]['path']))
        self.assertCountEqual(server.get_plasmids()[0]['primerNames'], ['Yesterday-F', 'Unbound-R'])
        self.assertEqual(server.preview_plasmid(self.item_id)['primers'][0]['name'], 'Yesterday-F')
        versions.set_enabled(False)
        with patch.object(desktop, 'find_snapgene', return_value=self.root/'SnapGene.exe'), patch.object(desktop.subprocess, 'Popen') as launch:
            self.assertTrue(desktop.DesktopApi().open_in_snapgene(self.item_id)['ok'])
            self.assertEqual(Path(launch.call_args.args[0][1]), Path(rows[1]['path']))
        backup = self.root/'after-delete.plasmora'
        server.backup_library(backup)
        server.restore_backup(backup)
        self.assertEqual(len(versions.list_versions(self.item_id)), 2)

    def test_delete_original_allowed_but_last_version_is_protected(self):
        rows = self.build_history()
        versions.delete_version(self.item_id, rows[0]['id'])
        versions.delete_version(self.item_id, rows[1]['id'])
        with self.assertRaisesRegex(ValueError, '至少保留一个'):
            versions.delete_version(self.item_id, rows[2]['id'])
        self.assertEqual(len(versions.list_versions(self.item_id)), 1)
        self.assertEqual(server.preview_plasmid(self.item_id)['primers'][0]['name'], 'Latest-F')

    def test_archive_failure_rolls_back_head_and_keeps_files(self):
        rows = self.build_history()
        before = server.get_plasmids()
        files = {Path(row[key]): Path(row[key]).read_bytes() for row in rows for key in ('path','snapshot_path')}
        with patch.object(versions, 'copy_checked', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                versions.delete_version(self.item_id, rows[2]['id'])
        self.assertEqual(server.get_plasmids(), before)
        self.assertEqual(len(versions.list_versions(self.item_id)), 3)
        self.assertEqual(server.get_trash(), [])
        self.assertEqual({path: path.read_bytes() for path in files}, files)

    def test_invalid_family_and_duplicate_rollback_are_rejected(self):
        rows = self.build_history()
        folder = self.root/'other';folder.mkdir()
        source = folder/'construct.dna';source.write_bytes(self.original_bytes)
        other = server.import_one(source)['id']
        server.rename_plasmid(other, 'construct.dna')
        with self.assertRaises(FileNotFoundError):
            versions.delete_version(other, rows[1]['id'])
        # Delete the middle version so head would roll back to the same-name original.
        versions.delete_version(self.item_id, rows[1]['id'])
        with self.assertRaisesRegex(ValueError, '先重命名'):
            versions.delete_version(self.item_id, rows[2]['id'])
        self.assertCountEqual(next(p for p in server.get_plasmids() if p['id']==self.item_id)['primerNames'], ['Latest-F','Unbound-R'])

    def test_trash_restore_waits_for_original_family_and_rejects_reused_id(self):
        rows = self.build_history()
        deleted = versions.delete_version(self.item_id, rows[1]['id'])
        family = server.delete_plasmid(self.item_id)
        with self.assertRaisesRegex(ValueError, '先从回收站恢复'):
            server.restore_trash(deleted['trashId'])
        server.restore_trash(family['trashId'])
        restored = server.restore_trash(deleted['trashId'])
        self.assertEqual(restored['id'], self.item_id)
        deleted = versions.delete_version(self.item_id, restored['versionId'])
        server.delete_plasmid(self.item_id)
        other = self.root/'unrelated.dna';other.write_bytes(primer_dna('Other-F'))
        self.assertEqual(server.import_one(other)['id'], self.item_id)
        with self.assertRaisesRegex(ValueError, '对应质粒已不存在'):
            server.restore_trash(deleted['trashId'])

    def test_purge_version_trash_only_removes_archived_copy(self):
        rows = self.build_history()
        deleted = versions.delete_version(self.item_id, rows[1]['id'])
        server.purge_trash(deleted['trashId'])
        self.assertEqual(server.get_trash(), [])
        self.assertEqual(len(versions.list_versions(self.item_id)), 2)
        self.assertEqual(server.preview_plasmid(self.item_id)['primers'][0]['name'], 'Latest-F')
