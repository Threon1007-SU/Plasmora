import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import server
from repository_watch import RepositoryWatcher
from test_stability import dna_bytes


def primer_dna(name='Test-F', sequence='ggATGCATGC'):
    xml = f'''<Primers><HybridizationParams minContinuousMatchLen="4" minMeltingTemperature="40"/>
    <Primer name="{name}" sequence="{sequence}" description="&lt;b&gt;实验引物&lt;/b&gt;">
      <BindingSite location="0-7" boundStrand="0" annealedBases="ATGCATGC" meltingTemperature="52"/>
      <BindingSite location="0-7" boundStrand="0" annealedBases="ATGCATGC" meltingTemperature="52" simplified="1"/>
      <BindingSite location="4-7" boundStrand="1" annealedBases="ATGC" meltingTemperature="43.5"/>
      <BindingSite location="5-1" boundStrand="1" annealedBases="ATGCA" meltingTemperature="45"/>
      <BindingSite location="1-2" annealedBases="TG" meltingTemperature="10"/>
    </Primer><Primer name="Unbound-R" sequence="NNATG"/></Primers>'''.encode()
    return dna_bytes(b'ATGCATGC', 'Feature-X') + b'\x05' + len(xml).to_bytes(4, 'big') + xml


class PrimerTest(unittest.TestCase):
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
        server.init_db()

    def import_primer(self):
        source = self.root / 'construct.dna'
        source.write_bytes(primer_dna())
        return server.import_one(source)['id']

    def test_read_full_sequence_ranges_and_unbound_primers(self):
        item_id = self.import_primer()
        primers = server.preview_plasmid(item_id)['primers']
        self.assertEqual(len(primers), 2)
        primer = primers[0]
        self.assertEqual(primer['sequence'], 'ggATGCATGC')  # Includes 5-prime tail and preserves case.
        self.assertEqual(primer['length'], 10)
        self.assertEqual(primer['gcPercent'], 60)
        self.assertEqual(primer['description'], '实验引物')
        self.assertEqual([(site['start'], site['end'], site['strand']) for site in primer['bindingSites']],
                         [(1, 8, 1), (5, 8, -1), (6, 2, -1)])
        self.assertEqual(primer['bindingSites'][1]['meltingTemperature'], 43.5)
        self.assertEqual(primers[1]['bindingSites'], [])
        self.assertIsNone(primers[1]['gcPercent'])

    def test_name_index_is_independent_and_list_never_opens_originals(self):
        self.import_primer()
        with patch.object(server, 'parse_dna', side_effect=AssertionError('List parsed a file')), \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('List read a file')):
            item = server.get_plasmids()[0]
        self.assertEqual(item['primerNames'], ['Test-F', 'Unbound-R'])
        self.assertTrue(item['primerIndexed'])
        self.assertNotIn('Test-F', item['tags'])
        self.assertIn('Feature-X', item['tags'])

    def test_external_edit_removes_old_names_and_no_primer_file_is_empty(self):
        item_id = self.import_primer()
        path = server.managed_plasmid_path(item_id)
        path.write_bytes(primer_dna('New-F'))
        self.assertTrue(server.sync_plasmid_if_changed(item_id))
        self.assertEqual(server.get_plasmids()[0]['primerNames'], ['New-F', 'Unbound-R'])
        path.write_bytes(dna_bytes(b'ATGCATGC', 'Feature-X'))
        self.assertTrue(server.sync_plasmid_if_changed(item_id))
        self.assertEqual(server.preview_plasmid(item_id)['primers'], [])
        self.assertEqual(server.get_plasmids()[0]['primerNames'], [])

    def test_legacy_database_migrates_without_parsing_then_backfills_once(self):
        item_id = self.import_primer()
        with server.db() as c:
            c.execute('DROP TABLE plasmid_primer_names')
            c.execute('ALTER TABLE library_plasmids DROP COLUMN primer_indexed')
        with patch.object(server, 'parse_dna', side_effect=AssertionError('Startup parsed a file')):
            server.init_db()
            self.assertFalse(server.get_plasmids()[0]['primerIndexed'])
        monitor = RepositoryWatcher(debounce=0)
        monitor._queue_recheck()
        with patch.object(server, 'parse_dna', wraps=server.parse_dna) as parser:
            monitor.process_pending(now=float('inf'))
            self.assertEqual(monitor.take_updates()['updated'], [item_id])
            self.assertEqual(server.get_plasmids()[0]['primerNames'], ['Test-F', 'Unbound-R'])
            monitor._queue_recheck()
            monitor.process_pending(now=float('inf'))
            self.assertEqual(monitor.take_updates()['updated'], [])
            parser.assert_called_once()

    def test_replace_trash_and_backup_keep_correct_index(self):
        item_id = self.import_primer()
        source = self.root / 'construct.dna'
        source.write_bytes(primer_dna('Replacement-F'))
        server.import_one(source, on_conflict='replace', existing_id=item_id)
        self.assertEqual(server.get_plasmids()[0]['primerNames'], ['Replacement-F', 'Unbound-R'])
        deleted = server.delete_plasmid(item_id)
        server.restore_trash(deleted['trashId'])
        self.assertEqual(server.get_plasmids()[0]['primerNames'], ['Replacement-F', 'Unbound-R'])
        backup = self.root / 'backup.plasmora'
        server.backup_library(backup)
        server.restore_backup(backup)
        self.assertEqual(server.get_plasmids()[0]['primerNames'], ['Replacement-F', 'Unbound-R'])

    def test_old_backup_restores_with_pending_index_and_can_be_backfilled(self):
        item_id = self.import_primer()
        backup = self.root / 'current.plasmora'
        server.backup_library(backup)
        legacy = self.root / 'legacy.plasmora'
        snapshot = self.root / 'old.sqlite3'
        with zipfile.ZipFile(backup) as archive:
            snapshot.write_bytes(archive.read('library.sqlite3'))
            with closing(sqlite3.connect(snapshot)) as c:
                c.execute('DROP TABLE plasmid_primer_names')
                c.execute('ALTER TABLE library_plasmids DROP COLUMN primer_indexed')
                c.commit()
            db_content = snapshot.read_bytes()
            manifest = json.loads(archive.read('manifest.json'))
            manifest['dbSha256'] = hashlib.sha256(db_content).hexdigest()
            with zipfile.ZipFile(legacy, 'w') as output:
                for name in archive.namelist():
                    content = db_content if name == 'library.sqlite3' else json.dumps(manifest).encode() if name == 'manifest.json' else archive.read(name)
                    output.writestr(name, content)
        server.restore_backup(legacy)
        self.assertFalse(server.get_plasmids()[0]['primerIndexed'])
        self.assertTrue(server.sync_plasmid_if_changed(item_id))
        self.assertEqual(server.get_plasmids()[0]['primerNames'], ['Test-F', 'Unbound-R'])


if __name__ == '__main__':
    unittest.main()
