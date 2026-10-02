"""Exercise the actual installer code using isolated names and dummy files."""
import ctypes
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from desktop import DesktopApi
from installation import InstallerShutdown, ShutdownSignal


def find_compiler():
    found = shutil.which("ISCC.exe")
    if found:
        return found
    for env, subfolder in (("LOCALAPPDATA", "Programs"), ("PROGRAMFILES", ""), ("PROGRAMFILES(X86)", "")):
        root = Path(os.environ.get(env, ".")) / subfolder
        for version in ("Inno Setup 7", "Inno Setup 6"):
            candidate = root / version / "ISCC.exe"
            if candidate.is_file():
                return str(candidate)
    return None


COMPILER = find_compiler() if os.name == "nt" else None


@unittest.skipUnless(COMPILER, "Inno Setup required for isolated installer integration")
class InstallerPreflightTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="plasmora-setup-test-")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.target = root / "app" / "Plasmora.exe"
        self.target.parent.mkdir()
        self.target.write_bytes(b"existing application")
        source = root / "replacement.bin"
        source.write_bytes(b"updated application")
        token = uuid.uuid4().hex
        self.mutex_name = "Local\\PlasmoraTestRunning-" + token
        self.guard_name = "Local\\PlasmoraTestInstalling-" + token
        self.event_name = "Local\\PlasmoraTestExit-" + token
        code = Path("installer.iss").read_text(encoding="utf-8").split("[Code]", 1)[1]
        code = code.replace("Local\\PlasmoraDesktopMutex", self.mutex_name)
        code = code.replace("Local\\PlasmoraInstallInProgress", self.guard_name)
        code = code.replace("Local\\PlasmoraInstallerShutdown", self.event_name)
        script = root / "fixture.iss"
        script.write_text(f'''#define AppExeName "Plasmora.exe"
[Setup]
AppId=PlasmoraTest-{token}
AppName=Plasmora Test
AppVersion=0.0.0
DefaultDirName={self.target.parent}
PrivilegesRequired=lowest
OutputDir={root}
OutputBaseFilename=fixture
Uninstallable=no
CreateUninstallRegKey=no
CloseApplications=no
RestartApplications=no
[Files]
Source: "{source}"; DestDir: "{{app}}"; DestName: "Plasmora.exe"; Flags: ignoreversion
[Code]
{code}
''', encoding="utf-8-sig")
        built = subprocess.run([COMPILER, "/Q", str(script)], capture_output=True, text=True, errors="replace")
        self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        self.command = [str(root / "fixture.exe"), "/SP-", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"]
        self.kernel = ctypes.windll.kernel32
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        self.kernel.CreateMutexW.restype = ctypes.c_void_p
        self.kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        self.kernel.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
        self.kernel.OpenMutexW.restype = ctypes.c_void_p

    def run_setup(self):
        return subprocess.run(self.command, timeout=20, capture_output=True).returncode

    def test_legacy_running_app_blocks_before_overwriting(self):
        mutex = self.kernel.CreateMutexW(None, False, self.mutex_name)
        self.assertTrue(mutex)
        try:
            self.assertNotEqual(self.run_setup(), 0)
            self.assertEqual(self.target.read_bytes(), b"existing application")
        finally:
            self.kernel.CloseHandle(mutex)

    def test_file_still_locked_after_mutex_release_blocks_install(self):
        self.kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                           ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
        self.kernel.CreateFileW.restype = ctypes.c_void_p
        handle = self.kernel.CreateFileW(str(self.target), 0x80000000, 1, None, 3, 128, None)
        self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
        try:
            self.assertNotEqual(self.run_setup(), 0)
            self.assertEqual(self.target.read_bytes(), b"existing application")
        finally:
            self.kernel.CloseHandle(handle)

    def test_available_files_install_normally(self):
        self.assertEqual(self.run_setup(), 0)
        self.assertEqual(self.target.read_bytes(), b"updated application")

    def test_cooperative_shutdown_waits_for_task_before_copying(self):
        mutex = self.kernel.CreateMutexW(None, False, self.mutex_name)
        exited = threading.Event()
        api = DesktopApi()
        api._window = SimpleNamespace(evaluate_js=lambda script: {"ready": True})

        def quit_app(behavior):
            self.kernel.CloseHandle(mutex)
            exited.set()

        api._tray_controller = SimpleNamespace(perform=quit_app)

        def installing():
            handle = self.kernel.OpenMutexW(0x100000, False, self.guard_name)
            if not handle:
                return False
            self.kernel.CloseHandle(handle)
            return True

        monitor = InstallerShutdown(api, lambda: ShutdownSignal(self.event_name), installing)
        self.addCleanup(monitor.stop)
        api._operation_lock.acquire()
        monitor.start()
        process = subprocess.Popen(self.command)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        try:
            self.assertTrue(api._update_requested.wait(5))
            self.assertFalse(exited.is_set())
            self.assertEqual(self.target.read_bytes(), b"existing application")
        finally:
            api._operation_lock.release()
        try:
            self.assertEqual(process.wait(timeout=15), 0)
            self.assertTrue(exited.is_set())
            self.assertEqual(self.target.read_bytes(), b"updated application")
        finally:
            if not exited.is_set():
                self.kernel.CloseHandle(mutex)


if __name__ == "__main__":
    unittest.main()
