import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
import versions
from repository_watch import RepositoryWatcher
from test_primers import primer_dna
from test_stability import dna_bytes


class DailyVersionsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in (
            ('LOCAL_DATA', self.root / 'data'), ('DB_PATH', self.root / 'data' / 'library.sqlite3'),
            ('DEFAULT_STORAGE', self.root / 'repository'), ('STORAGE_ROOT', self.root / 'repository'),
            ('SOURCE_ROOT', self.root), ('LEGACY_ROOT', None),
        ):
            mocked = patch.object(server, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.day = '2026-10-08'
        date = patch.object(versions, 'now', side_effect=lambda: self.day+'T12:00:00+08:00')
        date.start()
        self.addCleanup(date.stop)
        server.init_db()
        source = self.root / 'construct.dna'
        self.original_bytes = primer_dna('Original-F')
        source.write_bytes(self.original_bytes)
        self.item_id = server.import_one(source)['id']
        self.original = server.managed_plasmid_path(self.item_id)

    def test_default_off_then_same_day_edit_and_next_day_open(self):
        self.assertFalse(versions.enabled())
        self.assertEqual(versions.prepare_edit(self.item_id), self.original)
        versions.set_enabled(True)
        working = versions.prepare_edit(self.item_id)
        self.assertNotEqual(working, self.original)
        self.assertEqual(working.read_bytes(), self.original_bytes)
        working.write_bytes(primer_dna('Day1-F'))
        server.sync_plasmid_if_changed(self.item_id)
        self.assertEqual(versions.prepare_edit(self.item_id), working)
        self.assertEqual(len(versions.list_versions(self.item_id)), 2)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertEqual(server.get_plasmids()[0]['primerNames'], ['Day1-F', 'Unbound-R'])
        self.day = '2026-10-09'
        next_day = versions.prepare_edit(self.item_id)
        self.assertNotEqual(next_day, working)
        self.assertEqual(next_day.read_bytes(), working.read_bytes())
        self.assertEqual(len(versions.list_versions(self.item_id)), 3)

    def test_cross_midnight_save_without_reopening_preserves_yesterday(self):
        versions.set_enabled(True)
        yesterday = versions.prepare_edit(self.item_id)
        yesterday.write_bytes(primer_dna('Yesterday-F'))
        server.sync_plasmid_if_changed(self.item_id)
        saved = yesterday.read_bytes()
        self.day = '2026-10-09'
        yesterday.write_bytes(primer_dna('Today-F'))  # Editor still has yesterday's path open.
        server.sync_plasmid_if_changed(self.item_id)
        latest = server.managed_plasmid_path(self.item_id)
        self.assertNotEqual(latest, yesterday)
        self.assertEqual(yesterday.read_bytes(), saved)
        self.assertEqual(server.preview_plasmid(self.item_id)['primers'][0]['name'], 'Today-F')
        old = next(v for v in versions.list_versions(self.item_id) if '2026-10-08' in v['name'])
        self.assertEqual(versions.preview(self.item_id, old['id'])['primers'][0]['name'], 'Yesterday-F')

    def test_already_open_original_and_historical_save_are_captured_by_watcher(self):
        versions.set_enabled(True)
        self.original.write_bytes(primer_dna('First-save-F'))
        monitor = RepositoryWatcher(debounce=0)
        monitor.queue_path(self.original)
        monitor.process_pending(now=float('inf'))
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertEqual(monitor.take_updates()['updated'], [self.item_id])
        self.assertEqual(server.get_plasmids()[0]['primerNames'], ['First-save-F', 'Unbound-R'])
        self.original.write_bytes(primer_dna('Second-save-F'))
        monitor.queue_path(self.original)  # Original is no longer the library's current head.
        monitor.process_pending(now=float('inf'))
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertEqual(server.preview_plasmid(self.item_id)['primers'][0]['name'], 'Second-save-F')
        self.assertEqual(len(versions.list_versions(self.item_id)), 2)

    def test_disabled_setting_keeps_history_and_edits_latest_without_daily_split(self):
        versions.set_enabled(True)
        latest = versions.prepare_edit(self.item_id)
        versions.set_enabled(False)
        self.day = '2026-10-09'
        self.assertEqual(versions.prepare_edit(self.item_id), latest)
        latest.write_bytes(primer_dna('Disabled-F'))
        server.sync_plasmid_if_changed(self.item_id)
        self.assertEqual(len(versions.list_versions(self.item_id)), 2)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)

    def test_import_replacement_and_trash_restore_preserve_entire_family(self):
        versions.set_enabled(True)
        versions.prepare_edit(self.item_id)
        source = self.root / 'construct.dna'
        source.write_bytes(primer_dna('Replacement-F'))
        server.import_one(source, on_conflict='replace', existing_id=self.item_id)
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        self.assertEqual(server.get_plasmids()[0]['primerNames'], ['Replacement-F', 'Unbound-R'])
        before = versions.list_versions(self.item_id)
        trash = server.delete_plasmid(self.item_id)
        self.assertEqual(server.get_plasmids(), [])
        restored = server.restore_trash(trash['trashId'])['id']
        self.assertEqual(len(versions.list_versions(restored)), len(before))
        original = next(v for v in versions.list_versions(restored) if v['original'])
        self.assertEqual(versions.preview(restored, original['id'])['primers'][0]['name'], 'Original-F')

    def test_backup_restore_and_storage_migration_include_every_revision(self):
        versions.set_enabled(True)
        working = versions.prepare_edit(self.item_id)
        working.write_bytes(primer_dna('Day1-F'))
        server.sync_plasmid_if_changed(self.item_id)
        self.day = '2026-10-09'
        versions.prepare_edit(self.item_id).write_bytes(primer_dna('Day2-F'))
        server.sync_plasmid_if_changed(self.item_id)
        backup = self.root / 'family.plasmora'
        server.backup_library(backup)
        self.assertEqual(server.inspect_backup(backup)['count'], 1)
        server.restore_backup(backup)
        self.assertEqual(len(versions.list_versions(self.item_id)), 3)
        new_root = self.root / 'migrated'
        server.set_storage_directory(new_root)
        with server.db() as c:
            paths = versions.version_paths(c)
        self.assertTrue(all(new_root.resolve() in p.resolve().parents and p.is_file() for p in paths))
        names = [versions.preview(self.item_id, v['id'])['primers'][0]['name'] for v in versions.list_versions(self.item_id)]
        self.assertCountEqual(names, ['Original-F', 'Day1-F', 'Day2-F'])

    def test_failed_snapshot_and_cancelled_enable_leave_setting_off_and_original_safe(self):
        with patch.object(versions, 'snapshot', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                versions.set_enabled(True)
        self.assertFalse(versions.enabled())
        self.assertEqual(self.original.read_bytes(), self.original_bytes)
        with self.assertRaises(server.OperationCancelled):
            versions.set_enabled(True, cancel=lambda: True)
        self.assertFalse(versions.enabled())

    def test_index_only_listing_and_version_preview_do_not_modify_main_index(self):
        versions.set_enabled(True)
        working = versions.prepare_edit(self.item_id)
        working.write_bytes(primer_dna('Newest-F'))
        server.sync_plasmid_if_changed(self.item_id)
        with patch.object(server, 'parse_dna', side_effect=AssertionError('List read an original')):
            listed = versions.list_versions(self.item_id)
        original = next(v for v in listed if v['original'])
        versions.preview(self.item_id, original['id'])
        self.assertEqual(server.get_plasmids()[0]['primerNames'], ['Newest-F', 'Unbound-R'])

    def test_cancelled_enable_rolls_back_partially_created_baselines(self):
        second = self.root / 'second.dna'
        second.write_bytes(primer_dna('Second-F'))
        server.import_one(second)
        def cancel_after_first_snapshot():
            with server.db() as c:
                return c.execute('SELECT COUNT(*) FROM plasmid_versions').fetchone()[0] > 0
        with self.assertRaises(server.OperationCancelled):
            versions.set_enabled(True, cancel=cancel_after_first_snapshot)
        self.assertFalse(versions.enabled())
        with server.db() as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM plasmid_versions').fetchone()[0], 0)
        self.assertEqual(list((server.STORAGE_ROOT / '.plasmora-history').glob('*.dna')), [])
        self.assertEqual(versions.prepare_edit(self.item_id), self.original)

    def test_corrupt_history_backup_is_rejected_before_replacing_repository(self):
        import json
        import zipfile
        versions.set_enabled(True)
        latest = versions.prepare_edit(self.item_id)
        latest.write_bytes(primer_dna('Latest-F'))
        server.sync_plasmid_if_changed(self.item_id)
        backup = self.root / 'valid.plasmora'
        server.backup_library(backup)
        corrupt = self.root / 'corrupt.plasmora'
        with zipfile.ZipFile(backup) as source, zipfile.ZipFile(corrupt, 'w') as target:
            manifest = json.loads(source.read('manifest.json'))
            broken = f"versions/{manifest['versions'][0]['id']}.dna"
            for item in source.infolist():
                target.writestr(item, b'corrupt-history' if item.filename == broken else source.read(item.filename))
        with self.assertRaises(ValueError):
            server.restore_backup(corrupt)
        self.assertEqual(server.managed_plasmid_path(self.item_id), latest)
        self.assertEqual(server.preview_plasmid(self.item_id)['primers'][0]['name'], 'Latest-F')
        self.assertEqual(len(versions.list_versions(self.item_id)), 2)

    def test_copy_race_never_deletes_existing_destination(self):
        target = self.root / 'exists.dna'
        target.write_bytes(b'keep')
        with self.assertRaises(FileExistsError):
            versions.copy_checked(self.original, target, 'invalid', 1)
        self.assertEqual(target.read_bytes(), b'keep')

    def test_snapgene_opens_current_daily_copy_instead_of_original(self):
        import desktop
        versions.set_enabled(True)
        api = desktop.DesktopApi()
        with patch.object(desktop, 'find_snapgene', return_value=self.root / 'SnapGene.exe'), \
             patch.object(desktop.subprocess, 'Popen') as launch:
            self.assertTrue(api.open_in_snapgene(self.item_id)['ok'])
            first = Path(launch.call_args.args[0][1])
            self.assertNotEqual(first, self.original)
            self.day = '2026-10-09'
            self.assertTrue(api.open_in_snapgene(self.item_id)['ok'])
            latest = Path(launch.call_args.args[0][1])
            self.assertNotEqual(latest, first)
            self.assertEqual(latest, server.managed_plasmid_path(self.item_id))

    def test_cancelled_history_migration_keeps_original_paths(self):
        versions.set_enabled(True)
        versions.prepare_edit(self.item_id)
        with server.db() as c:
            before = versions.version_paths(c)
        target = self.root / 'cancelled-move'
        with self.assertRaises(server.OperationCancelled):
            server.set_storage_directory(target, cancel=lambda: (target / '.plasmora-history').is_dir())
        with server.db() as c:
            self.assertEqual(versions.version_paths(c), before)
        self.assertTrue(all(path.is_file() for path in before))

    def test_snapgene_opens_selected_history_and_saved_edits_preserve_history(self):
        import desktop
        versions.set_enabled(True)
        yesterday = versions.prepare_edit(self.item_id)
        yesterday.write_bytes(primer_dna('Yesterday-F'))
        server.sync_plasmid_if_changed(self.item_id)
        revision = next(v for v in versions.list_versions(self.item_id) if v['latest'])
        self.day = '2026-10-09'
        latest = versions.prepare_edit(self.item_id)
        api = desktop.DesktopApi()
        with patch.object(desktop, 'find_snapgene', return_value=self.root / 'SnapGene.exe'), \
             patch.object(desktop.subprocess, 'Popen') as launch:
            self.assertTrue(api.open_in_snapgene(self.item_id, revision['id'])['ok'])
            self.assertEqual(Path(launch.call_args.args[0][1]), yesterday)
            self.assertEqual(server.managed_plasmid_path(self.item_id), latest)
        yesterday.write_bytes(primer_dna('Edit-from-history-F'))
        versions.sync_path(yesterday)
        self.assertEqual(server.preview_plasmid(self.item_id)['primers'][0]['name'], 'Edit-from-history-F')
        self.assertEqual(versions.preview(self.item_id, revision['id'])['primers'][0]['name'], 'Yesterday-F')
        self.assertEqual(server.parse_dna(yesterday)['primers'][0]['name'], 'Yesterday-F')

    def test_selected_history_validates_family_and_association_uses_selected_path(self):
        import desktop
        api = desktop.DesktopApi()
        with patch.object(desktop, 'find_snapgene', return_value=None), patch.object(desktop.os, 'startfile', create=True) as launch:
            self.assertTrue(api.open_in_snapgene(self.item_id, 0)['ok'])
            self.assertEqual(Path(launch.call_args.args[0]), self.original)
        versions.set_enabled(True)
        original = versions.list_versions(self.item_id)[0]
        versions.prepare_edit(self.item_id)
        other = self.root / 'other.dna'
        other.write_bytes(primer_dna('Other-F'))
        other_id = server.import_one(other)['id']
        with patch.object(desktop, 'find_snapgene', return_value=None), patch.object(desktop.os, 'startfile', create=True) as launch:
            self.assertTrue(api.open_in_snapgene(self.item_id, original['id'])['ok'])
            self.assertEqual(Path(launch.call_args.args[0]), self.original)
            launch.reset_mock()
            self.assertIn('error', api.open_in_snapgene(other_id, original['id']))
            self.assertIn('error', api.open_in_snapgene(self.item_id, 0))
            self.assertIn('error', api.open_in_snapgene(self.item_id, 999999))
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
