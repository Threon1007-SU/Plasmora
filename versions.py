"""Daily working copies and durable, independently stored revision snapshots."""
from datetime import datetime
from pathlib import Path
import os
import tempfile
from uuid import uuid4

import server


def now():
    return datetime.now().astimezone().isoformat(timespec='microseconds')


def today():
    return now()[:10]


def init_schema(c):
    c.execute('''CREATE TABLE IF NOT EXISTS plasmid_versions (
        id INTEGER PRIMARY KEY, plasmid_id INTEGER NOT NULL REFERENCES library_plasmids(id) ON DELETE CASCADE,
        name TEXT NOT NULL, day TEXT, created_at TEXT NOT NULL, modified_at TEXT NOT NULL,
        path TEXT NOT NULL UNIQUE, snapshot_path TEXT NOT NULL, sha256 TEXT NOT NULL,
        file_size INTEGER NOT NULL, original INTEGER NOT NULL DEFAULT 0, file_mtime_ns INTEGER NOT NULL DEFAULT 0
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_versions_plasmid ON plasmid_versions(plasmid_id,id)')


def enabled(c=None):
    if c is None:
        with server.db() as connection:
            return enabled(connection)
    row = c.execute("SELECT value FROM app_settings WHERE key='daily_versions'").fetchone()
    return bool(row and row[0] == '1')


def copy_checked(source, target, digest, size):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        with Path(source).open('rb') as input_file, target.open('xb') as output:
            created = True
            actual, length = server._copy_with_hash(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        if (actual, length) != (digest, size):
            raise ValueError('文件仍在写入，请稍后重试')
    except Exception:
        if created:
            target.unlink(missing_ok=True)
        raise
    return target


def snapshot(source, digest, size):
    return copy_checked(source, server.STORAGE_ROOT / '.plasmora-history' / f'{uuid4().hex}.dna', digest, size)


def ensure_baseline(c, item_id):
    if c.execute('SELECT 1 FROM plasmid_versions WHERE plasmid_id=?', (item_id,)).fetchone():
        return
    row = c.execute('SELECT * FROM library_plasmids WHERE id=?', (item_id,)).fetchone()
    if not row:
        raise FileNotFoundError('质粒已不存在')
    saved = snapshot(row['storage_path'], row['sha256'], row['file_size'])
    try:
        c.execute('''INSERT INTO plasmid_versions(plasmid_id,name,created_at,modified_at,path,snapshot_path,sha256,file_size,original,file_mtime_ns)
                     VALUES(?,?,?,?,?,?,?,?,1,?)''',
                  (item_id, row['file_name'], row['imported_at'], row['imported_at'], row['storage_path'], str(saved), row['sha256'], row['file_size'], Path(row['storage_path']).stat().st_mtime_ns))
    except Exception:
        saved.unlink(missing_ok=True)
        raise


def register_import(c, item_id):
    if enabled(c):
        ensure_baseline(c, item_id)


def set_enabled(value, progress=None, cancel=None):
    if type(value) is not bool:
        raise ValueError('请选择是否按日生成副本')
    with server.LOCK:
        if value and not enabled():
            result = server.sync_changed_plasmids(progress, cancel)
            if result['errors']:
                raise ValueError('部分质粒无法读取，请先同步仓库后再启用按日副本')
            with server.db() as c:
                rows = c.execute('SELECT id,file_name FROM library_plasmids ORDER BY id').fetchall()
            created = []
            try:
                for index, row in enumerate(rows):
                    server.check_cancel(cancel)
                    server.report_progress(progress, 'versioning', index, len(rows), row['file_name'])
                    with server.db() as c:
                        existed = c.execute('SELECT 1 FROM plasmid_versions WHERE plasmid_id=?', (row['id'],)).fetchone()
                        ensure_baseline(c, row['id'])
                        if not existed:
                            created.append(dict(c.execute('SELECT * FROM plasmid_versions WHERE plasmid_id=?', (row['id'],)).fetchone()))
                server.check_cancel(cancel)
            except Exception:
                with server.db() as c:
                    c.executemany('DELETE FROM plasmid_versions WHERE id=?', [(item['id'],) for item in created])
                for item in created:
                    Path(item['snapshot_path']).unlink(missing_ok=True)
                raise
        if not value:
            server.check_cancel(cancel)
        with server.db() as c:
            c.execute("INSERT INTO app_settings(key,value) VALUES('daily_versions',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('1' if value else '0',))
    return {'enabled': value}


def version_paths(c, item_id=None):
    sql = 'SELECT path,snapshot_path FROM plasmid_versions'
    rows = c.execute(sql + (' WHERE plasmid_id=?' if item_id is not None else ''), (item_id,) if item_id is not None else ()).fetchall()
    return {Path(row[key]) for row in rows for key in ('path', 'snapshot_path')}


def _restore_public(version):
    path = Path(version['path'])
    saved = Path(version['snapshot_path'])
    fd, temporary = tempfile.mkstemp(prefix='.plasmora-history-', suffix='.tmp', dir=path.parent)
    os.close(fd)
    try:
        with saved.open('rb') as source, open(temporary, 'wb') as output:
            digest, size = server._copy_with_hash(source, output)
        if (digest, size) != (version['sha256'], version['file_size']):
            raise ValueError('历史副本校验失败，已停止恢复原文件')
        os.replace(temporary, path)
        with server.db() as c:
            c.execute('UPDATE plasmid_versions SET file_mtime_ns=? WHERE id=?', (path.stat().st_mtime_ns, version['id']))
    finally:
        Path(temporary).unlink(missing_ok=True)


def accept_change(item_id, source, parsed, source_version=None, force_copy=False):
    """Commit the new content before restoring any edited protected public file.

    Snapshots never change in place. A crash before the SQLite commit leaves the
    old snapshot valid; a crash afterwards can safely retry the public-file repair.
    """
    with server.LOCK:
        with server.db() as c:
            library = c.execute('SELECT * FROM library_plasmids WHERE id=?', (item_id,)).fetchone()
            if not library:
                raise FileNotFoundError('质粒已不存在')
            head = c.execute('SELECT * FROM plasmid_versions WHERE plasmid_id=? AND path=?', (item_id, library['storage_path'])).fetchone()
            if not head:
                return Path(source)
            if source_version is None and not force_copy:
                source_version = head
            protected = force_copy or source_version['original'] or source_version['path'] != head['path'] or (enabled(c) and source_version['day'] != today())
            if not force_copy and parsed['sha256'] == source_version['sha256']:
                return Path(source)
            target_version = head if not protected else c.execute(
                'SELECT * FROM plasmid_versions WHERE plasmid_id=? AND day=? AND original=0 ORDER BY id DESC LIMIT 1', (item_id, today())).fetchone()
            # Never overwrite a protected source while making the current-day copy.
            if target_version and source_version and target_version['id'] == source_version['id'] and protected:
                target_version = None
        created, old_snapshot = [], None
        stamp = now()
        try:
            new_snapshot = snapshot(source, parsed['sha256'], parsed['file_size'])
            created.append(new_snapshot)
            if target_version:
                target = Path(target_version['path'])
                if Path(source) != target:
                    staged = copy_checked(new_snapshot, target.with_name(f'.plasmora-{uuid4().hex}.tmp'), parsed['sha256'], parsed['file_size'])
                    os.replace(staged, target)
                old_snapshot = Path(target_version['snapshot_path'])
                name = target_version['name']
            else:
                name = f"{Path(library['file_name']).stem[:120]} ({today()}).dna"
                target = server.STORAGE_ROOT / server.safe_storage_name(name, server.STORAGE_ROOT)
                copy_checked(new_snapshot, target, parsed['sha256'], parsed['file_size'])
                created.append(target)
            with server.db() as c:
                if target_version:
                    c.execute('UPDATE plasmid_versions SET modified_at=?,snapshot_path=?,sha256=?,file_size=?,file_mtime_ns=? WHERE id=?',
                              (stamp, str(new_snapshot), parsed['sha256'], parsed['file_size'], target.stat().st_mtime_ns, target_version['id']))
                else:
                    c.execute('''INSERT INTO plasmid_versions(plasmid_id,name,day,created_at,modified_at,path,snapshot_path,sha256,file_size,file_mtime_ns)
                                 VALUES(?,?,?,?,?,?,?,?,?,?)''',
                              (item_id, name, today(), stamp, stamp, str(target), str(new_snapshot), parsed['sha256'], parsed['file_size'], target.stat().st_mtime_ns))
                c.execute('UPDATE library_plasmids SET storage_path=?,stored_name=? WHERE id=?', (str(target), target.name, item_id))
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        if old_snapshot:
            try:
                old_snapshot.unlink(missing_ok=True)
            except OSError:
                server.LOGGER.warning('旧快照暂时无法清理：%s', old_snapshot.name)
        if protected and source_version and Path(source) == Path(source_version['path']):
            _restore_public(source_version)
        return target


def prepare_edit(item_id):
    with server.LOCK:
        sync_family(item_id)
        if not enabled():
            return server.managed_plasmid_path(item_id)
        with server.db() as c:
            ensure_baseline(c, item_id)
            library = c.execute('SELECT * FROM library_plasmids WHERE id=?', (item_id,)).fetchone()
            head = c.execute('SELECT * FROM plasmid_versions WHERE path=?', (library['storage_path'],)).fetchone()
        if head['day'] == today() and not head['original']:
            return Path(head['path'])
        parsed = server.parse_dna(Path(head['path']))
        path = accept_change(item_id, Path(head['path']), parsed, force_copy=True)
        server.sync_plasmid(item_id, manage_versions=False)
        return path


def sync_path(path):
    with server.LOCK:
        return _sync_path(path)


def _sync_path(path):
    with server.LOCK, server.db() as c:
        version = c.execute('SELECT * FROM plasmid_versions WHERE path=?', (str(path),)).fetchone()
        if not version:
            return False
        library = c.execute('SELECT storage_path FROM library_plasmids WHERE id=?', (version['plasmid_id'],)).fetchone()
    if library['storage_path'] == str(path):
        return server.sync_plasmid_if_changed(version['plasmid_id'])
    stat = Path(path).stat()
    if stat.st_size == version['file_size'] and stat.st_mtime_ns == version['file_mtime_ns']:
        return False
    parsed = server.parse_dna(Path(path))
    if parsed['sha256'] == version['sha256']:
        with server.db() as c:
            c.execute('UPDATE plasmid_versions SET file_mtime_ns=? WHERE id=?', (stat.st_mtime_ns, version['id']))
        return False
    accept_change(version['plasmid_id'], path, parsed, version)
    server.sync_plasmid(version['plasmid_id'], manage_versions=False)
    return True


def sync_family(item_id):
    with server.LOCK:
        with server.db() as c:
            rows = c.execute('SELECT path FROM plasmid_versions WHERE plasmid_id=?', (item_id,)).fetchall()
        changed = False
        for row in rows:
            changed = sync_path(row['path']) or changed
        return server.sync_plasmid_if_changed(item_id) or changed


def stage_migration(c, target_dir, moved_rows, created, journal, progress, cancel):
    mapping = {str(old): Path(new) for new, _, _, old in moved_rows}
    rows = c.execute('SELECT * FROM plasmid_versions').fetchall()
    changes = []
    for index, row in enumerate(rows):
        server.check_cancel(cancel)
        server.report_progress(progress, 'move', index, len(rows), '正在迁移历史副本')
        for key in ('path', 'snapshot_path'):
            old = row[key]
            if old in mapping:
                continue
            folder = target_dir / '.plasmora-history' if key == 'snapshot_path' else target_dir
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / server.safe_storage_name(Path(old).name, folder)
            journal['created'].append(str(target))
            server._write_migration_journal(journal)
            copy_checked(old, target, row['sha256'], row['file_size'])
            created.append(target)
            mapping[old] = target
        changes.append((str(mapping[row['path']]), str(mapping[row['snapshot_path']]), mapping[row['path']].stat().st_mtime_ns, row['id']))
    return changes, {Path(row[key]) for row in rows for key in ('path', 'snapshot_path')}


def list_versions(item_id):
    with server.db() as c:
        library = c.execute('SELECT * FROM library_plasmids WHERE id=?', (item_id,)).fetchone()
        if not library:
            raise FileNotFoundError('质粒已不存在')
        rows = c.execute('SELECT * FROM plasmid_versions WHERE plasmid_id=? ORDER BY modified_at DESC,id DESC', (item_id,)).fetchall()
    if not rows:
        return [{'id': 0, 'name': library['file_name'], 'time': library['imported_at'], 'original': True, 'latest': True, 'size': library['file_size']}]
    return [{'id': r['id'], 'name': r['name'], 'time': r['modified_at'], 'original': bool(r['original']),
             'latest': r['path'] == library['storage_path'], 'size': r['file_size']} for r in rows]


def preview(item_id, version_id):
    if version_id == 0:
        return server.preview_plasmid(item_id)
    with server.LOCK, server.db() as c:
        row = c.execute('SELECT * FROM plasmid_versions WHERE id=? AND plasmid_id=?', (version_id, item_id)).fetchone()
        if not row:
            raise FileNotFoundError('副本已不存在')
        parsed = server.parse_dna(Path(row['snapshot_path']))
        if parsed['sha256'] != row['sha256']:
            raise ValueError('副本校验失败')
    return {'id': item_id, 'versionId': version_id, 'name': row['name'], 'length': len(parsed['sequence']),
            'circular': parsed['circular'], 'features': parsed['features'], 'primers': parsed['primers'],
            'sha256': row['sha256'], 'versionTime': row['modified_at']}


def archive_history(c, item_id, folder):
    library = c.execute('SELECT storage_path FROM library_plasmids WHERE id=?', (item_id,)).fetchone()
    rows = c.execute('SELECT * FROM plasmid_versions WHERE plasmid_id=? ORDER BY id', (item_id,)).fetchall()
    archived = []
    try:
        for row in rows:
            target = folder / f'{uuid4().hex}.dna'
            copy_checked(row['snapshot_path'], target, row['sha256'], row['file_size'])
            item = dict(row)
            item['archive_path'] = str(target)
            item['latest'] = row['path'] == library['storage_path']
            archived.append(item)
    except Exception:
        for row in archived:
            Path(row['archive_path']).unlink(missing_ok=True)
        raise
    return archived


def restore_history(c, item_id, rows, head_path, created):
    for row in rows:
        source = Path(row['archive_path'])
        if (server.LOCAL_DATA / 'Trash').resolve() not in source.resolve().parents:
            raise ValueError('副本回收站路径无效')
        saved = snapshot(source, row['sha256'], row['file_size'])
        created.append(saved)
        target = Path(head_path) if row['latest'] else server.STORAGE_ROOT / server.safe_storage_name(row['name'], server.STORAGE_ROOT)
        if not row['latest']:
            copy_checked(saved, target, row['sha256'], row['file_size'])
            created.append(target)
        c.execute('''INSERT INTO plasmid_versions(plasmid_id,name,day,created_at,modified_at,path,snapshot_path,sha256,file_size,original,file_mtime_ns)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                  (item_id, row['name'], row['day'], row['created_at'], row['modified_at'], str(target), str(saved),
                   row['sha256'], row['file_size'], row['original'], target.stat().st_mtime_ns))


def clean_trash_history(metadata):
    for row in metadata.get('versions', []):
        path = Path(row['archive_path'])
        if (server.LOCAL_DATA / 'Trash').resolve() not in path.resolve().parents:
            raise ValueError('副本回收站路径无效')
        path.unlink(missing_ok=True)


def backup_history(snapshot_db, archive, manifest, progress=None, cancel=None):
    rows = snapshot_db.execute('SELECT id,snapshot_path,sha256,file_size FROM plasmid_versions ORDER BY id').fetchall()
    manifest['versions'] = []
    for index, row in enumerate(rows):
        server.check_cancel(cancel)
        server.report_progress(progress, 'backup', index, len(rows), '正在备份历史副本')
        with Path(row['snapshot_path']).open('rb') as source, archive.open(f"versions/{row['id']}.dna", 'w') as output:
            digest, size = server._copy_with_hash(source, output)
        if (digest, size) != (row['sha256'], row['file_size']):
            raise ValueError('历史副本校验失败，已停止备份')
        manifest['versions'].append({'id': row['id'], 'sha256': digest, 'size': size})


def verify_history(snapshot_db, archive, manifest, work, extract, cancel=None):
    exists = snapshot_db.execute("SELECT 1 FROM sqlite_master WHERE name='plasmid_versions' AND type='table'").fetchone()
    rows = snapshot_db.execute('SELECT * FROM plasmid_versions ORDER BY id').fetchall() if exists else []
    files = manifest.get('versions', [])
    if not isinstance(files, list) or len(files) != len(rows):
        raise ValueError('备份副本清单与数据库不一致')
    by_id = {entry['id']: entry for entry in files}
    if len(by_id) != len(rows) or set(by_id) != {row['id'] for row in rows}:
        raise ValueError('备份副本清单无效')
    for row in rows:
        server.check_cancel(cancel)
        entry = by_id[row['id']]
        if (entry['sha256'], entry['size']) != (row['sha256'], row['file_size']):
            raise ValueError('备份副本校验信息不一致')
        path = Path(work) / f"version-{row['id']}.dna"
        with archive.open(f"versions/{row['id']}.dna") as source:
            if extract:
                with path.open('xb') as output:
                    digest, size = server._copy_with_hash(source, output)
            else:
                digest, size = server._copy_with_hash(source)
        if (digest, size) != (row['sha256'], row['file_size']):
            raise ValueError('备份副本内容校验失败')


def restore_backup_history(c, work, head_map, created):
    init_schema(c)
    rows = c.execute('SELECT * FROM plasmid_versions ORDER BY id').fetchall()
    for row in rows:
        # Restore works with an ordinary sqlite connection using tuple rows too.
        item = dict(zip([column[1] for column in c.execute('PRAGMA table_info(plasmid_versions)')], row))
        source = Path(work) / f"version-{item['id']}.dna"
        saved = snapshot(source, item['sha256'], item['file_size'])
        created.append(saved)
        previous_head, new_head = head_map[item['plasmid_id']]
        target = new_head if item['path'] == previous_head else server.STORAGE_ROOT / server.safe_storage_name(item['name'], server.STORAGE_ROOT)
        if target != new_head:
            copy_checked(saved, target, item['sha256'], item['file_size'])
            created.append(target)
        c.execute('UPDATE plasmid_versions SET path=?,snapshot_path=?,file_mtime_ns=? WHERE id=?',
                  (str(target), str(saved), target.stat().st_mtime_ns, item['id']))
