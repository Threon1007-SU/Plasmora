from __future__ import annotations

import threading
import os
import json
import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

import webview
from webview.dom import DOMEventHandler

import server


class DesktopApi:
    def __init__(self):
        self._window = None
        self._pending_restore = None
        self._pending_import = None
        self._import_lock = threading.RLock()
        self._tray_controller = None

    def start_file_drag(self, plasmid_id):
        """Offer the managed DNA file to Explorer as a copy-only shell drag."""
        staging_dir = None
        try:
            source = server.managed_plasmid_path(int(plasmid_id))
            with server.db() as c:
                row = c.execute("SELECT file_name FROM library_plasmids WHERE id=?", (int(plasmid_id),)).fetchone()
            if not row:
                raise FileNotFoundError("仓库中已没有这条质粒记录")
            staging_dir = Path(tempfile.mkdtemp(prefix="plasmora-drag-"))
            path = staging_dir / Path(row["file_name"]).name
            try:
                os.link(source, path)
            except OSError:
                shutil.copy2(source, path)
            import clr
            clr.AddReference("System.Windows.Forms")
            from System import Array, String, Action
            from System.Windows.Forms import DataObject, DataFormats, DragDropEffects

            data = DataObject()
            data.SetData(DataFormats.FileDrop, Array[String]([str(path)]))
            native = self._window.native
            result = []

            def drag():
                result.append(native.DoDragDrop(data, DragDropEffects.Copy))

            native.Invoke(Action(drag))
            cleanup = threading.Timer(300, shutil.rmtree, args=(staging_dir,), kwargs={"ignore_errors": True})
            cleanup.daemon = True
            cleanup.start()
            return {"ok": True, "copied": bool(result and result[0] == DragDropEffects.Copy)}
        except Exception as exc:
            if staging_dir:
                shutil.rmtree(staging_dir, ignore_errors=True)
            return {"error": f"无法拖出质粒：{exc}"}

    def resolve_close(self, behavior, remember=False):
        if behavior not in {"tray", "quit"}:
            return {"error": "请选择关闭方式"}
        if remember:
            server.set_close_behavior(behavior)
        if self._tray_controller:
            self._tray_controller.perform(behavior)
        return {"ok": True}

    def choose_storage_directory(self):
        folders = self._window.create_file_dialog(
            webview.FileDialog.FOLDER,
            directory=str(server.STORAGE_ROOT),
            allow_multiple=False,
        )
        if not folders:
            return {"cancelled": True}
        try:
            result = server.set_storage_directory(folders[0])
            return {"cancelled": False, **result, "count": len(server.get_plasmids())}
        except Exception as exc:
            return {"cancelled": False, "error": str(exc)}

    def import_plasmids(self):
        if self._pending_import is not None:
            return {"error": "请先处理当前的同名文件选择"}
        paths = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            directory=str(server.STORAGE_ROOT),
            allow_multiple=True,
            file_types=("SnapGene DNA (*.dna)",),
        )
        if not paths:
            return {"cancelled": True}
        return self._begin_import(paths, None)

    def begin_dropped_import(self, paths, group_id=None):
        return self._begin_import(paths, group_id)

    def _begin_import(self, paths, group_id):
        with self._import_lock:
            if self._pending_import is not None:
                return {"error": "请先处理当前的同名文件选择"}
            if group_id is not None:
                if type(group_id) is not int or group_id <= 0:
                    return {"error": "目标分组无效"}
                with server.db() as c:
                    if not c.execute("SELECT 1 FROM groups WHERE id=?", (group_id,)).fetchone():
                        return {"error": "目标分组不存在"}
            self._pending_import = {"paths": [str(path) for path in paths], "groupId": group_id,
                                    "index": 0, "imported": [], "duplicates": [], "skipped": [],
                                    "replaced": [], "errors": []}
            return self._advance_import()

    def _record_import(self, result):
        key = "duplicates" if result.get("duplicate") else "skipped" if result.get("skipped") else "replaced" if result.get("replaced") else "imported"
        self._pending_import[key].append(result)

    def _finish_import(self, cancelled_remaining=False):
        session = self._pending_import
        self._pending_import = None
        results = session["imported"] + session["duplicates"] + session["skipped"] + session["replaced"]
        return {"imported": session["imported"], "duplicates": session["duplicates"],
                "skipped": session["skipped"], "replaced": session["replaced"],
                "errors": session["errors"], "grouped": sum(item["grouped"] for item in results),
                "groupId": session["groupId"], "plasmids": server.get_plasmids(),
                "cancelledRemaining": cancelled_remaining}

    def _advance_import(self, action=None, existing_id=None):
        session = self._pending_import
        if action is not None:
            path = session["paths"][session["index"]]
            try:
                self._record_import(server.import_one(path, session["groupId"], action, existing_id))
            except Exception as exc:
                return {"error": str(exc)}
            session["index"] += 1
        while session["index"] < len(session["paths"]):
            path = session["paths"][session["index"]]
            try:
                conflict = server.inspect_import_conflict(path)
                if conflict:
                    return {"pending": True, "conflict": conflict,
                            "position": session["index"] + 1, "total": len(session["paths"])}
                self._record_import(server.import_one(path, session["groupId"]))
            except Exception as exc:
                session["errors"].append({"file": Path(path).name, "error": str(exc)})
            session["index"] += 1
        return self._finish_import()

    def resolve_import(self, action, existing_id=None):
        with self._import_lock:
            if self._pending_import is None:
                return {"error": "没有待处理的同名文件"}
            if action not in {"copy", "keep_existing", "replace"}:
                return {"error": "请选择同名文件的处理方式"}
            return self._advance_import(action, existing_id)

    def cancel_import(self):
        with self._import_lock:
            if self._pending_import is None:
                return {"cancelled": True}
            return self._finish_import(cancelled_remaining=True)

    def backup_library(self):
        selected = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(Path.home() / "Documents"),
            save_filename=f"Plasmora-备份-{datetime.now():%Y%m%d-%H%M}.plasmora",
            file_types=("Plasmora 备份 (*.plasmora)",),
        )
        if not selected:
            return {"cancelled": True}
        try:
            return server.backup_library(selected[0])
        except Exception as exc:
            return {"error": str(exc)}

    def choose_backup_for_restore(self):
        self._pending_restore = None
        selected = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            directory=str(Path.home() / "Documents"),
            allow_multiple=False,
            file_types=("Plasmora 备份 (*.plasmora)",),
        )
        if not selected:
            return {"cancelled": True}
        try:
            info = server.inspect_backup(selected[0])
            self._pending_restore = info["path"]
            return info
        except Exception as exc:
            return {"error": str(exc)}

    def restore_backup(self):
        if not self._pending_restore:
            return {"error": "请先选择并检查备份文件"}
        try:
            result = server.restore_backup(self._pending_restore)
            self._pending_restore = None
            return result
        except Exception as exc:
            return {"error": str(exc)}

    def export_plasmids(self, item_ids):
        if not isinstance(item_ids, list) or not item_ids:
            return {"error": "请至少选择一个质粒"}
        selected = self._window.create_file_dialog(
            webview.FileDialog.SAVE,
            directory=str(Path.home() / "Documents"),
            save_filename=f"Plasmora-质粒导出-{datetime.now():%Y%m%d-%H%M}.zip",
            file_types=("ZIP 压缩包 (*.zip)",),
        )
        if not selected:
            return {"cancelled": True}
        try:
            return server.export_plasmids(item_ids, selected[0])
        except Exception as exc:
            return {"error": str(exc)}

    def export_plasmid(self, plasmid_id):
        try:
            with server.db() as c:
                row = c.execute("SELECT file_name FROM library_plasmids WHERE id=?", (int(plasmid_id),)).fetchone()
            if not row:
                return {"error": "仓库中已没有这条质粒记录"}
            selected = self._window.create_file_dialog(
                webview.FileDialog.SAVE,
                directory=str(Path.home() / "Documents"),
                save_filename=row["file_name"],
                file_types=("SnapGene DNA (*.dna)",),
            )
            if not selected:
                return {"cancelled": True}
            return server.export_one_plasmid(int(plasmid_id), selected[0])
        except Exception as exc:
            return {"error": str(exc)}

    def open_in_snapgene(self, plasmid_id):
        try:
            path = server.managed_plasmid_path(int(plasmid_id))
            executable = find_snapgene()
            if executable:
                subprocess.Popen([str(executable), str(path)])
                return {"ok": True, "method": "snapgene"}
            os.startfile(str(path))
            return {"ok": True, "method": "association"}
        except Exception as exc:
            return {"error": f"无法打开质粒：{exc}"}


def find_snapgene():
    candidates = []
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            for key_path in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\SnapGene.exe",):
                try:
                    with winreg.OpenKey(hive, key_path) as key:
                        candidates.append(Path(winreg.QueryValueEx(key, "")[0]))
                except OSError:
                    pass
    except ImportError:
        pass
    for env_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(env_name)
        if root:
            candidates.extend((Path(root) / "SnapGene" / "SnapGene.exe", Path(root) / "Programs" / "SnapGene" / "SnapGene.exe"))
    on_path = shutil.which("SnapGene.exe")
    if on_path:
        candidates.append(Path(on_path))
    return next((path for path in candidates if path.is_file()), None)


def bind_file_drop(window, api):
    def on_drop(event):
        files = event.get("dataTransfer", {}).get("files", [])
        paths = [item.get("pywebviewFullPath") for item in files]
        paths = [path for path in paths if path]
        if not paths:
            if files:
                window.evaluate_js('window.plasmoraDropCompleted({"error":"无法读取拖入文件的本机路径，请从资源管理器拖入 .dna 文件"})')
            return
        try:
            group_id = window.evaluate_js("window.plasmoraDropGroupId ?? null")
            result = api.begin_dropped_import(paths, group_id)
        except Exception as exc:
            result = {"error": str(exc)}
        window.evaluate_js(f"window.plasmoraDropCompleted({json.dumps(result, ensure_ascii=True)})")

    window.dom.document.events.drop += DOMEventHandler(on_drop, True, True)


class TrayController:
    def __init__(self, window):
        self.window = window
        self._allow_close = False
        self._tray = None
        self._menu = None
        self._handlers = []
        window.events.closing += self.on_closing

    def install(self):
        from System import Action
        from System.Windows.Forms import NotifyIcon, ContextMenuStrip, ToolStripMenuItem

        def create():
            native = self.window.native
            menu = ContextMenuStrip()
            open_item = ToolStripMenuItem("打开 Plasmora")
            quit_item = ToolStripMenuItem("退出 Plasmora")
            open_handler = lambda sender, args: self.show()
            quit_handler = lambda sender, args: self.perform("quit")
            open_item.Click += open_handler
            quit_item.Click += quit_handler
            menu.Items.Add(open_item)
            menu.Items.Add(quit_item)
            tray = NotifyIcon()
            tray.Icon = native.Icon
            tray.Text = "Plasmora"
            tray.ContextMenuStrip = menu
            tray.DoubleClick += open_handler
            tray.Visible = True
            self._tray, self._menu = tray, menu
            self._handlers = [open_handler, quit_handler]

        self.window.native.Invoke(Action(create))

    def on_closing(self):
        if self._allow_close:
            return True
        behavior = server.get_close_behavior()
        if behavior == "ask":
            def prompt():
                try:
                    self.window.evaluate_js("window.plasmoraShowCloseChoice()")
                except Exception:
                    pass
            threading.Thread(target=prompt, name="plasmora-close-prompt", daemon=True).start()
        else:
            self.perform(behavior)
        return False

    def show(self):
        from System import Action
        from System.Windows.Forms import FormWindowState
        def open_window():
            native = self.window.native
            native.Show()
            native.WindowState = FormWindowState.Normal
            native.Activate()
        self.window.native.BeginInvoke(Action(open_window))

    def perform(self, behavior):
        from System import Action
        def apply():
            native = self.window.native
            if behavior == "tray":
                native.Hide()
            else:
                self._allow_close = True
                if self._tray:
                    self._tray.Visible = False
                    self._tray.Dispose()
                if self._menu:
                    self._menu.Dispose()
                native.Close()
        self.window.native.BeginInvoke(Action(apply))


def main():
    # A previous session may have exited before its temporary drag source expired.
    for folder in Path(tempfile.gettempdir()).glob("plasmora-drag-*"):
        try:
            if folder.is_dir() and time.time() - folder.stat().st_mtime > 86400:
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass
    httpd = server.run_server()
    threading.Thread(target=httpd.serve_forever, name="plasmid-local-api", daemon=True).start()
    api = DesktopApi()
    window = webview.create_window(
        "Plasmora",
        f"http://127.0.0.1:{httpd.server_address[1]}",
        js_api=api,
        width=1420,
        height=920,
        min_size=(860, 620),
        background_color="#0b1322",
        text_select=True,
    )
    api._window = window
    tray = TrayController(window)
    api._tray_controller = tray
    def on_ready():
        bind_file_drop(window, api)
        tray.install()
    try:
        webview.start(on_ready, debug=False)
    finally:
        httpd.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
