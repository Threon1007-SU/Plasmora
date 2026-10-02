"""Cooperative installer shutdown; never force-terminate a running task."""
from __future__ import annotations

import ctypes
import os
import threading

import server

INSTALL_MUTEX = "Local\\PlasmoraInstallInProgress"
SHUTDOWN_EVENT = "Local\\PlasmoraInstallerShutdown"


def installation_in_progress():
    if os.name != "nt":
        return False
    kernel = ctypes.windll.kernel32
    kernel.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.OpenMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenMutexW(0x100000, False, INSTALL_MUTEX)
    if not handle:
        return False
    kernel.CloseHandle(handle)
    return True


class ShutdownSignal:
    def __init__(self, name=SHUTDOWN_EVENT):
        self._kernel = ctypes.windll.kernel32
        self._kernel.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_bool, ctypes.c_wchar_p]
        self._kernel.CreateEventW.restype = ctypes.c_void_p
        self._kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._kernel.WaitForSingleObject.restype = ctypes.c_uint32
        self._kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self._handle = self._kernel.CreateEventW(None, False, False, name)
        if not self._handle:
            raise ctypes.WinError()

    def wait(self, milliseconds):
        result = self._kernel.WaitForSingleObject(self._handle, milliseconds)
        if result == 0xFFFFFFFF:
            raise ctypes.WinError()
        return result == 0

    def close(self):
        if self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


class InstallerShutdown:
    def __init__(self, api, signal_factory=ShutdownSignal, installing=installation_in_progress):
        self._api = api
        self._signal_factory = signal_factory
        self._installing = installing
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="plasmora-installer-exit", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self):
        signal = None
        requested = False
        failed = False
        try:
            signal = self._signal_factory()
            while not self._stop.is_set():
                if signal.wait(500):
                    requested = self._installing()
                    failed = False
                if not requested:
                    continue
                if not self._installing():
                    # Cancelling setup returns the running program to normal use.
                    self._api._update_requested.clear()
                    self._api._window.evaluate_js("window.plasmoraCancelUpdate?.()")
                    requested = False
                    continue
                self._api._update_requested.set()
                if failed or not self._api.ready_for_update():
                    continue
                ready = self._api._window.evaluate_js(
                    "window.plasmoraPrepareForUpdate ? window.plasmoraPrepareForUpdate() : ({ready:false})")
                if not ready or not ready.get("ready"):
                    failed = bool(ready and ready.get("failed"))
                    continue
                if not self._installing():
                    continue
                watcher = self._api._repository_watcher
                if watcher:
                    watcher.stop()
                    if watcher._thread and watcher._thread.is_alive():
                        continue
                if not self._installing():
                    if watcher:
                        watcher.start()
                    continue
                self._api._tray_controller.perform("quit")
                return
        except Exception:
            server.LOGGER.exception("安装更新的安全退出失败")
            self._api._update_requested.clear()
            try:
                self._api._window.evaluate_js("window.plasmoraCancelUpdate?.()")
            except Exception:
                pass
        finally:
            if signal:
                signal.close()
