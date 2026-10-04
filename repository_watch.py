"""Watch managed originals without rescanning on window focus."""
from __future__ import annotations

import os
import threading
import time

import server


def path_key(path):
    return os.path.normcase(os.path.realpath(path))


class WindowsDirectoryWatch:
    def __init__(self, root, on_change, on_error):
        import clr
        clr.AddReference("System")
        from System.IO import FileSystemWatcher, NotifyFilters

        self._watcher = FileSystemWatcher(str(root), "*.dna")
        self._watcher.NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.Size
        self._watcher.IncludeSubdirectories = False
        self._watcher.InternalBufferSize = 16384

        def changed(sender, event):
            on_change(str(event.FullPath))

        def renamed(sender, event):
            on_change(str(event.OldFullPath))
            on_change(str(event.FullPath))

        def error(sender, event):
            on_error(str(event.GetException()))

        # Keep Python delegates alive for the lifetime of the .NET watcher.
        self._handlers = (changed, renamed, error)
        self._watcher.Changed += changed
        self._watcher.Created += changed
        self._watcher.Deleted += changed
        self._watcher.Renamed += renamed
        self._watcher.Error += error
        self._watcher.EnableRaisingEvents = True

    def close(self):
        self._watcher.EnableRaisingEvents = False
        self._watcher.Dispose()


class RepositoryWatcher:
    def __init__(self, factory=WindowsDirectoryWatch, debounce=1.5):
        self._factory = factory
        self._debounce = debounce
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pending = {}
        self._updated = set()
        self._errors = {}
        self._reported = {}
        self._root = None
        self._watch = None
        self._thread = None
        self._restart = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="plasmora-file-watch", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def queue_path(self, path):
        key = path_key(path)
        with self._lock:
            self._pending[key] = (time.monotonic() + self._debounce, 0)
        self._wake.set()

    def _queue_recheck(self):
        # Only on startup, changing repository, or lost file notifications.
        with server.LOCK, server.db() as c:
            paths = c.execute("SELECT storage_path FROM library_plasmids").fetchall()
        for row in paths:
            self.queue_path(row["storage_path"])

    def _watch_error(self, message):
        server.LOGGER.warning("仓库文件监听中断：%s", message)
        with self._lock:
            self._restart = True
        self._wake.set()

    def _report_error(self, key, error):
        with self._lock:
            if self._reported.get(key) != error["error"]:
                self._errors[key] = error
                self._reported[key] = error["error"]

    def take_updates(self):
        with self._lock:
            result = {"updated": sorted(self._updated), "errors": list(self._errors.values())}
            self._updated.clear()
            self._errors.clear()
            return result

    def process_pending(self, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            due = {key: entry for key, entry in self._pending.items() if entry[0] <= now}
            for key in due:
                del self._pending[key]
        if not due or self._stop.is_set():
            return
        # Read the index once per changed batch; untouched files are not accessed.
        # Release the repository lock between files so a legacy index backfill
        # does not block preview/import requests for the entire batch.
        with server.LOCK, server.db() as c:
            rows = c.execute("SELECT id,file_name,storage_path FROM library_plasmids").fetchall()
        targets = {path_key(row["storage_path"]): row for row in rows}
        for key, (_, attempts) in due.items():
            with server.LOCK:
                if self._stop.is_set():
                    break
                row = targets.get(key)
                if not row:
                    continue  # Unmanaged drops and deleted records are not imported here.
                try:
                    changed = server.sync_plasmid_if_changed(row["id"])
                    with self._lock:
                        self._reported.pop(key, None)
                        self._errors.pop(key, None)
                        if changed:
                            self._updated.add(row["id"])
                except Exception as exc:
                    if attempts < 3:
                        # SnapGene may lock, truncate, or atomically replace a file while saving.
                        with self._lock:
                            self._pending.setdefault(key, (time.monotonic() + 2 ** (attempts + 1), attempts + 1))
                    else:
                        server.LOGGER.warning("后台同步失败：%s：%s", row["file_name"], exc)
                        self._report_error(key, {"id": row["id"], "name": row["file_name"], "error": str(exc)})

    def _run(self):
        try:
            while not self._stop.is_set():
                root = path_key(server.STORAGE_ROOT)
                with self._lock:
                    restart = self._restart
                    self._restart = False
                if root != self._root or restart or self._watch is None:
                    if self._watch:
                        self._watch.close()
                        self._watch = None
                    try:
                        self._watch = self._factory(server.STORAGE_ROOT, self.queue_path, self._watch_error)
                        self._root = root
                        with self._lock:
                            self._pending.clear()
                            self._reported.pop("watcher", None)
                        self._queue_recheck()
                    except Exception as exc:
                        server.LOGGER.exception("无法监听仓库文件")
                        self._report_error("watcher", {"name": "仓库监听", "error": str(exc)})
                        self._stop.wait(5)
                        continue
                self.process_pending()
                self._wake.wait(0.5)
                self._wake.clear()
        finally:
            if self._watch:
                self._watch.close()
                self._watch = None
