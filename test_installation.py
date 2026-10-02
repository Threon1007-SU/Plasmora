import os
import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

from desktop import DesktopApi
from installation import InstallerShutdown, ShutdownSignal


class TestSignal:
    def __init__(self):
        self.event = threading.Event()
        self.closed = False

    def wait(self, milliseconds):
        received = self.event.wait(0.02)
        if received:
            self.event.clear()
        return received

    def close(self):
        self.closed = True


class InstallerShutdownTest(unittest.TestCase):
    def setUp(self):
        self.api = DesktopApi()
        self.quit = threading.Event()
        self.api._tray_controller = SimpleNamespace(perform=lambda behavior: self.quit.set())
        self.api._window = SimpleNamespace(evaluate_js=Mock(return_value={"ready": True}))
        self.signal = TestSignal()
        self.active = True
        self.monitor = InstallerShutdown(self.api, lambda: self.signal, lambda: self.active)
        self.addCleanup(self.monitor.stop)

    def test_update_waits_for_running_operation_then_quits(self):
        self.api._operation_lock.acquire()
        try:
            self.monitor.start()
            self.signal.event.set()
            self.assertTrue(self.api._update_requested.wait(2))
            self.assertFalse(self.quit.wait(0.1))
            self.api._window.evaluate_js.assert_not_called()
        finally:
            self.api._operation_lock.release()
        self.assertTrue(self.quit.wait(2))
        self.monitor.stop()
        self.assertTrue(self.signal.closed)

    def test_cancelled_setup_restores_running_program(self):
        self.api._pending_import = {"awaiting": "conflict"}
        restored = threading.Event()
        self.api._window.evaluate_js.side_effect = lambda script: restored.set()
        self.monitor.start()
        self.signal.event.set()
        self.assertTrue(self.api._update_requested.wait(2))
        self.assertFalse(self.quit.is_set())
        self.active = False
        self.assertTrue(restored.wait(2))
        self.assertFalse(self.api._update_requested.is_set())
        self.assertIsNotNone(self.api._pending_import)
        self.assertFalse(self.quit.is_set())

    def test_failed_note_save_requires_retry_without_discarding(self):
        attempted = threading.Event()

        def prepare(script, callback=None):
            attempted.set()
            return {"ready": False, "failed": True}

        self.api._window.evaluate_js.side_effect = prepare
        self.monitor.start()
        self.signal.event.set()
        self.assertTrue(attempted.wait(2))
        self.assertFalse(self.quit.wait(0.1))
        self.assertEqual(self.api._window.evaluate_js.call_count, 1)
        self.api._window.evaluate_js.side_effect = None
        self.api._window.evaluate_js.return_value = {"ready": True}
        self.signal.event.set()
        self.assertTrue(self.quit.wait(2))

    def test_async_note_save_must_resolve_before_quitting(self):
        called = threading.Event()
        callbacks = []

        def evaluate(script, callback=None):
            callbacks.append(callback)
            called.set()
            return True  # pywebview's immediate Promise acknowledgement

        self.api._window.evaluate_js.side_effect = evaluate
        self.monitor.start()
        self.signal.event.set()
        self.assertTrue(called.wait(2))
        self.assertFalse(self.quit.is_set())
        callbacks[0]({"ready": True})
        self.assertTrue(self.quit.wait(2))

    def test_new_task_is_rejected_while_existing_import_can_finish(self):
        self.api._update_requested.set()
        with self.assertRaisesRegex(ValueError, "准备更新"):
            self.api._run_operation("backup", lambda progress, cancel: self.fail("Started during update"))
        self.assertIn("error", self.api._begin_import([], None))
        self.api._pending_import = {"awaiting": "conflict"}
        self.assertFalse(self.api.ready_for_update())
        self.api._pending_import = None
        self.assertTrue(self.api.ready_for_update())

    @unittest.skipUnless(os.name == "nt", "Windows update IPC")
    def test_native_shutdown_signal_is_consumed_once(self):
        signal = ShutdownSignal("Local\\PlasmoraTestShutdown-" + uuid.uuid4().hex)
        self.addCleanup(signal.close)
        kernel = signal._kernel
        kernel.SetEvent.argtypes = [__import__("ctypes").c_void_p]
        self.assertFalse(signal.wait(0))
        self.assertTrue(kernel.SetEvent(signal._handle))
        self.assertTrue(signal.wait(100))
        self.assertFalse(signal.wait(0))


if __name__ == "__main__":
    unittest.main()
