from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
import zipfile

import recovery
import server
import versions
import test_stability


class DamagedRepositoryRecoveryTest(unittest.TestCase):
    def setUp(self):
        test_stability.StabilityTest.setUp(self)
        logger = patch.object(server, 'LOGGER')
        logger.start()
        self.addCleanup(logger.stop)

    import_file = test_stability.StabilityTest.import_file

    def create_backup(self, history=False):
        self.item = self.import_file('target.dna', 'Saved-A')
        if history:
            versions.set_enabled(True)
            versions.prepare_edit(self.item['id'])
        self.backup = self.root / 'valid.plasmora'
        server.backup_library(self.backup)
        self.survivor = self.import_file('survivor.dna', 'New-B')
        server.update_plasmid_note(self.survivor['id'], 'keep this note')
        self.survivor_path = server.managed_plasmid_path(self.survivor['id'])
        self.survivor_bytes = self.survivor_path.read_bytes()
        return server.managed_plasmid_path(self.item['id'])

    def check_pack(self, result):
        self.assertTrue(result['ok'])
        self.assertTrue(result['rollbackIncomplete'])
        self.assertEqual(server.get_plasmids()[0]['name'], 'target.dna')
        self.assertEqual(server.get_plasmids()[0]['tags'], ['Saved-A'])
        self.assertEqual(len(server.get_plasmids()), 1)
        self.assertEqual(self.survivor_path.read_bytes(), self.survivor_bytes)
        pack = Path(result['rollbackPath'])
        with zipfile.ZipFile(pack) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            self.assertFalse(manifest['complete'])
            self.assertEqual(manifest['format'], 'PlasmoraRecovery')
            self.assertEqual(hashlib.sha256(archive.read('library.sqlite3')).hexdigest(), manifest['dbSha256'])
            for entry in manifest['files']:
                data = archive.read(entry['entry'])
                self.assertEqual(hashlib.sha256(data).hexdigest(), entry['sha256'])
                self.assertEqual(len(data), entry['size'])
            original_db = self.root / 'preserved.sqlite3'
            original_db.write_bytes(archive.read('library.sqlite3'))
            with closing(sqlite3.connect(original_db)) as c:
                self.assertEqual(c.execute('SELECT note FROM library_plasmids WHERE id=?', (self.survivor['id'],)).fetchone()[0], 'keep this note')
                self.assertEqual(c.execute('SELECT COUNT(*) FROM library_plasmids').fetchone()[0], 2)
        self.assertTrue(server.list_rollback_backups()[0]['incomplete'])
        return manifest

    def test_missing_current_file_can_restore_and_keeps_readable_state(self):
        current = self.create_backup()
        current.unlink()
        result = server.restore_backup(self.backup)
        manifest = self.check_pack(result)
        self.assertEqual(result['unavailableFileCount'], 1)
        self.assertEqual(manifest['unavailable'][0]['path'], str(current))

    def test_corrupt_current_bytes_are_preserved_before_restore(self):
        current = self.create_backup()
        broken = b'broken external save\x00'
        current.write_bytes(broken)
        result = server.restore_backup(self.backup)
        manifest = self.check_pack(result)
        entry = next(e for e in manifest['files'] if e['path'] == str(current))
        with zipfile.ZipFile(result['rollbackPath']) as archive:
            self.assertEqual(archive.read(entry['entry']), broken)
        self.assertFalse(entry['matchesIndex'])
        self.assertEqual(current.read_bytes(), broken)

    def test_missing_history_public_file_restores_full_history(self):
        self.create_backup(history=True)
        with server.db() as c:
            original = dict(c.execute('SELECT * FROM plasmid_versions WHERE plasmid_id=? AND original=1', (self.item['id'],)).fetchone())
        Path(original['path']).unlink()
        result = server.restore_backup(self.backup)
        manifest = self.check_pack(result)
        self.assertTrue(any(e['path'] == original['path'] for e in manifest['unavailable']))
        self.assertEqual(len(versions.list_versions(self.item['id'])), 2)
        for version in versions.list_versions(self.item['id']):
            self.assertEqual(versions.preview(self.item['id'], version['id'])['features'][0]['name'], 'Saved-A')

    def test_missing_history_snapshot_still_preserves_public_copy(self):
        self.create_backup(history=True)
        with server.db() as c:
            original = dict(c.execute('SELECT * FROM plasmid_versions WHERE plasmid_id=? AND original=1', (self.item['id'],)).fetchone())
        Path(original['snapshot_path']).unlink()
        result = server.restore_backup(self.backup)
        manifest = self.check_pack(result)
        self.assertTrue(any(e['path'] == original['snapshot_path'] for e in manifest['unavailable']))
        self.assertTrue(any(e['path'] == original['path'] for e in manifest['files']))
        self.assertEqual(len(versions.list_versions(self.item['id'])), 2)

    def test_cancelled_preservation_keeps_current_data_and_cleans_temporary_archive(self):
        current = self.create_backup()
        current.unlink()
        before = server.get_plasmids()
        cancel_requested = False
        def progress(update):
            nonlocal cancel_requested
            if update['phase'] == 'preserve':
                cancel_requested = True
        with self.assertRaises(server.OperationCancelled):
            server.restore_backup(self.backup, progress=progress, cancel=lambda: cancel_requested)
        self.assertEqual(server.get_plasmids(), before)
        self.assertEqual(self.survivor_path.read_bytes(), self.survivor_bytes)
        self.assertEqual(server.list_rollback_backups(), [])
        self.assertEqual(list((server.LOCAL_DATA / 'rollback').glob('.plasmora-archive-*')), [])

    def test_failed_preservation_never_replaces_database(self):
        current = self.create_backup()
        current.unlink()
        before = server.get_plasmids()
        with patch.object(recovery.os, 'replace', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                server.restore_backup(self.backup)
        self.assertEqual(server.get_plasmids(), before)
        self.assertEqual(self.survivor_path.read_bytes(), self.survivor_bytes)
        self.assertEqual(server.list_rollback_backups(), [])

    def test_database_replace_failure_preserves_current_state_and_safety_pack(self):
        current = self.create_backup()
        current.unlink()
        before = server.get_plasmids()
        replace = os.replace
        def fail_db_replace(source, target):
            if Path(target) == server.DB_PATH:
                raise OSError('database locked')
            return replace(source, target)
        with patch.object(server.os, 'replace', side_effect=fail_db_replace):
            with self.assertRaises(OSError):
                server.restore_backup(self.backup)
        self.assertEqual(server.get_plasmids(), before)
        self.assertEqual(self.survivor_path.read_bytes(), self.survivor_bytes)
        self.assertEqual(len(server.list_rollback_backups()), 1)
        self.assertEqual(list(self.store.glob('Plasmora-*.dna')), [])

    def test_invalid_input_is_rejected_before_preserving_or_replacing(self):
        current = self.create_backup()
        current.unlink()
        before = server.get_plasmids()
        invalid = self.root / 'invalid.plasmora'
        invalid.write_bytes(b'not a backup')
        with self.assertRaises(zipfile.BadZipFile):
            server.restore_backup(invalid)
        self.assertEqual(server.get_plasmids(), before)
        self.assertEqual(server.list_rollback_backups(), [])

    def test_desktop_opens_only_local_recovery_packs(self):
        import desktop
        current = self.create_backup()
        current.unlink()
        result = server.restore_backup(self.backup)
        api = desktop.DesktopApi()
        name = Path(result['rollbackPath']).name
        with patch.object(desktop.os, 'startfile', create=True) as launch:
            self.assertTrue(api.open_recovery_archive(name)['ok'])
            self.assertEqual(Path(launch.call_args.args[0]).resolve(), Path(result['rollbackPath']).resolve())
            launch.reset_mock()
            for invalid in ('../'+name, 'Before-restore-missing.recovery.zip', 'wrong.zip'):
                self.assertIn('error', api.open_recovery_archive(invalid))
            launch.assert_not_called()
