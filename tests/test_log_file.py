"""Tests for the log file setup in halo_battery.pyw. No tray, no hardware.

A read-only %APPDATA% (a locked-down profile, or the app started with a restricted
token) used to kill start-up with a traceback out of the logging module:

    PermissionError: [Errno 13] Permission denied: '...\\HaloBattery\\halo_battery.log'

The app now runs without a log file instead.

Run from the repository root:

    python -m unittest discover -s tests
"""
import importlib.util
import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The app writes its log and settings to %APPDATA%\HaloBattery when it loads. Point it
# at a temporary folder, so the tests never touch the real log or settings.
_real_appdata = os.environ.get("APPDATA")
os.environ["APPDATA"] = tempfile.mkdtemp(prefix="halo_battery_test_")
try:
    spec = importlib.util.spec_from_file_location("halo_battery", os.path.join(ROOT, "halo_battery.pyw"))
    hb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hb)
finally:
    if _real_appdata is None:
        os.environ.pop("APPDATA", None)
    else:
        os.environ["APPDATA"] = _real_appdata


class LogFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = tmp.name
        self.path = os.path.join(self.folder, "halo_battery.log")

    def test_handler_writes_to_the_file(self):
        handler = hb._log_file_handler(self.path)
        self.assertIsNotNone(handler)
        self.addCleanup(handler.close)
        logger = logging.getLogger("halo_battery_test_writes")
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        logger.info("hello from the test")
        with open(self.path, encoding="utf-8") as f:
            self.assertIn("hello from the test", f.read())

    def test_handler_rotates_like_the_app(self):
        handler = hb._log_file_handler(self.path)
        self.addCleanup(handler.close)
        self.assertEqual(handler.maxBytes, 512_000)
        self.assertEqual(handler.backupCount, 1)

    def test_handler_is_none_when_the_file_cannot_be_opened(self):
        with mock.patch.object(hb, "RotatingFileHandler",
                               side_effect=PermissionError(13, "denied")):
            self.assertIsNone(hb._log_file_handler(self.path))

    def test_handler_is_none_for_a_folder_that_does_not_exist(self):
        self.assertIsNone(hb._log_file_handler(os.path.join(self.folder, "missing", "log.txt")))

    def test_a_dead_thread_is_written_to_the_log(self):
        """The tray window is built in a thread: when Windows denies it, say why."""
        import types

        def boom():
            raise PermissionError(5, "denied")

        try:
            boom()
        except PermissionError:
            exc = sys.exc_info()
        args = types.SimpleNamespace(exc_type=exc[0], exc_value=exc[1], exc_traceback=exc[2],
                                     thread=types.SimpleNamespace(name="Dummy-1"))
        with self.assertLogs(hb.log, level="WARNING") as cm:
            hb._log_thread_exception(args)
        text = "\n".join(cm.output)
        self.assertIn("Dummy-1", text)
        self.assertIn("PermissionError", text)
        self.assertIn("denied", text)

    def test_a_system_exit_in_a_thread_is_not_logged(self):
        import types
        args = types.SimpleNamespace(exc_type=SystemExit, exc_value=SystemExit(0),
                                     exc_traceback=None,
                                     thread=types.SimpleNamespace(name="Dummy-2"))
        with mock.patch.object(hb.log, "warning") as warning:
            hb._log_thread_exception(args)
        warning.assert_not_called()

    def test_start_up_survives_an_unwritable_data_folder(self):
        """No settings folder and no log file: the app still loads, with a null handler."""
        with mock.patch.object(os, "makedirs", side_effect=PermissionError(5, "denied")), \
                mock.patch.object(logging.handlers, "RotatingFileHandler",
                                  side_effect=PermissionError(13, "denied")):
            spec = importlib.util.spec_from_file_location(
                "halo_battery_no_log", os.path.join(ROOT, "halo_battery.pyw"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        handlers = module.log.handlers
        self.assertTrue(handlers, "the log needs a handler, even when it is a null one")
        self.assertTrue(any(isinstance(h, logging.NullHandler) for h in handlers))


if __name__ == "__main__":
    unittest.main()
