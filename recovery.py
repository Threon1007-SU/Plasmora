"""Preserve a damaged repository before restoring an independently valid backup.

Recovery ZIPs keep the unfiltered index and readable bytes, including damaged
DNA files. They are data preservation packs, not complete backups.
"""
from contextlib import closing
from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import tempfile
import zipfile

import server


def capture_repository(destination, reason, progress=None, cancel=None):
    with server.LOCK, tempfile.TemporaryDirectory(prefix='plasmora-preserve-') as work:
        server.check_cancel(cancel)
        snapshot_path = Path(work) / 'library.sqlite3'
        server._snapshot_database(snapshot_path)
        paths = {}
        def add(path, role):
            paths.setdefault(str(Path(path)), []).append(role)
        with closing(sqlite3.connect(snapshot_path)) as snapshot:
            snapshot.row_factory = sqlite3.Row
            for row in snapshot.execute('SELECT id,file_name,storage_path,sha256,file_size FROM library_plasmids'):
                add(row['storage_path'], {'kind': 'current', 'plasmidId': row['id'], 'name': row['file_name'],
                                         'sha256': row['sha256'], 'size': row['file_size']})
            for row in snapshot.execute('SELECT * FROM plasmid_versions'):
                for key, kind in (('path', 'history'), ('snapshot_path', 'snapshot')):
                    add(row[key], {'kind': kind, 'plasmidId': row['plasmid_id'], 'versionId': row['id'],
                                   'name': row['name'], 'sha256': row['sha256'], 'size': row['file_size']})
        destination, temporary = server._temporary_archive_path(destination)
        manifest = {'format': 'PlasmoraRecovery', 'version': 1, 'complete': False,
                    'createdAt': datetime.now().isoformat(timespec='seconds'), 'reason': str(reason),
                    'dbSha256': hashlib.sha256(snapshot_path.read_bytes()).hexdigest(), 'files': [], 'unavailable': []}
        try:
            with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.write(snapshot_path, 'library.sqlite3')
                for index, (name, roles) in enumerate(paths.items(), 1):
                    server.check_cancel(cancel)
                    server.report_progress(progress, 'preserve', index-1, len(paths), '正在保全原仓库可读取文件')
                    path = Path(name)
                    # Only source opening errors are recoverable. Destination
                    # write failures must abort, never permit destructive cleanup.
                    try:
                        before = path.stat()
                        source = path.open('rb')
                    except OSError as error:
                        manifest['unavailable'].append({'path': name, 'roles': roles, 'error': str(error)})
                        continue
                    entry = f'files/{index:06d}.dna'
                    with source, archive.open(entry, 'w') as output:
                        digest, size = server._copy_with_hash(source, output)
                    after = path.stat()
                    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns or size != after.st_size:
                        raise ValueError('原仓库文件仍在写入，已停止恢复，请稍后重试')
                    manifest['files'].append({'entry': entry, 'path': name, 'roles': roles,
                                              'sha256': digest, 'size': size,
                                              'matchesIndex': all((digest, size) == (r['sha256'], r['size']) for r in roles)})
                archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2))
                archive.writestr('README.txt',
                    '原仓库不完整，此保全包不能作为完整备份直接恢复。\n'
                    'library.sqlite3 保留全部原索引（含分组、备注和缺失记录）。\n'
                    'files 保存所有可读取的受管理文件原始字节，包括损坏或与索引不一致的文件。\n'
                    'manifest.json 记录文件名、原路径、质粒/副本编号、校验值及无法读取的文件。\n'
                    '可按清单提取有效 .dna 文件并重新导入；恢复时旧文件未被清理。\n')
            server.check_cancel(cancel)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {'path': str(destination), 'incomplete': True, 'readableCount': len(manifest['files']),
            'unavailableCount': len(manifest['unavailable'])}
