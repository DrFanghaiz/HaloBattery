"""Tests for the tray window pystray builds (halo_battery.pyw). No tray, no hardware.

pystray opts in to WM_TASKBARCREATED with ChangeWindowMessageFilterEx right after it
creates the window. Windows answers that with "access denied" when the app runs with a
restricted token, the tray thread died with a traceback, and the app had no tray icon at
all. The app now logs the refusal and keeps the window.

pystray also drops the answer of Shell_NotifyIcon, so an icon Windows refused to add left
nothing in the log either; that is logged now too.

Run from the repository root:

    python -m unittest discover -s tests
"""
import importlib.util
import os
import sys
import tempfile
import types
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


class FakeBase:
    """Stands in for pystray.Icon: only the private methods TrayIcon overrides exist."""


def fake_win32(filter_error=None, hwnd=4242, hinstance=99):
    calls = types.SimpleNamespace(create=[], filter=[])

    def CreateWindowEx(*args):
        calls.create.append(args)
        return hwnd

    def ChangeWindowMessageFilterEx(*args):
        calls.filter.append(args)
        if filter_error is not None:
            raise filter_error
        return True

    module = types.SimpleNamespace(
        CreateWindowEx=CreateWindowEx,
        ChangeWindowMessageFilterEx=ChangeWindowMessageFilterEx,
        GetModuleHandle=lambda name: hinstance,
        WS_POPUP=0x80000000,
        WM_TASKBARCREATED=0x0313,
        MSGFLT_ALLOW=1,
        calls=calls,
    )
    return module


def fake_notify(answer=1, sent=None):
    class NOTIFYICONDATAW(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    def Shell_NotifyIcon(code, data):
        if sent is not None:
            sent.append((code, data))
        return answer

    return types.SimpleNamespace(Shell_NotifyIcon=Shell_NotifyIcon,
                                 NOTIFYICONDATAW=NOTIFYICONDATAW, NIM_ADD=0)


class TrayWindowTest(unittest.TestCase):
    def make_icon(self):
        cls = hb._make_tray_class(FakeBase)
        return cls.__new__(cls), cls

    def test_the_window_is_a_popup_of_the_registered_class(self):
        icon, cls = self.make_icon()
        win32 = fake_win32()
        with mock.patch("pystray._util.win32", win32):
            hwnd = cls._create_window(icon, 1234)
        self.assertEqual(hwnd, 4242)
        self.assertEqual(win32.calls.create,
                         [(0, 1234, None, 0x80000000, 0, 0, 0, 0, 0, None, 99, None)])
        self.assertEqual(win32.calls.filter, [(4242, 0x0313, 1, None)])

    def test_a_refused_message_filter_keeps_the_window(self):
        """The regression: access denied here used to kill the whole tray thread."""
        icon, cls = self.make_icon()
        win32 = fake_win32(filter_error=PermissionError(5, "access denied"))
        with mock.patch("pystray._util.win32", win32):
            with self.assertLogs(hb.log, level="WARNING") as cm:
                hwnd = cls._create_window(icon, 1234)
                second = cls._create_window(icon, 1234)     # the menu window, same refusal
        self.assertEqual(hwnd, 4242)
        self.assertEqual(second, 4242)
        text = "\n".join(cm.output)
        self.assertIn("window message filter", text)
        self.assertIn("access denied", text)
        self.assertEqual(len(cm.output), 1, "one warning, not one per window")

    def test_the_filter_is_still_asked_for(self):
        """No silent change of behaviour: the call is made whenever Windows allows it."""
        icon, cls = self.make_icon()
        win32 = fake_win32()
        with mock.patch("pystray._util.win32", win32):
            cls._create_window(icon, 1234)
        self.assertEqual(len(win32.calls.filter), 1)


class TrayMessageTest(unittest.TestCase):
    """pystray drops the answer of Shell_NotifyIcon: a refused icon left no trace."""

    def make_icon(self):
        cls = hb._make_tray_class(FakeBase)
        icon = cls.__new__(cls)
        icon._hb_uid = 7
        icon._hwnd = 4242
        return icon, cls

    def test_a_refused_icon_is_logged_once_with_the_error(self):
        sent = []

        def notify(code, data):
            sent.append((code, dict(data)))
            return 0, PermissionError(5, "access denied")

        icon, cls = self.make_icon()
        with mock.patch("ctypes.sizeof", lambda t: 8), \
                mock.patch("pystray._util.win32", fake_notify()), \
                mock.patch.object(hb, "_shell_notify", notify):
            with self.assertLogs(hb.log, level="WARNING") as cm:
                cls._message(icon, 0, 0x1)
                cls._message(icon, 0, 0x1)
        self.assertEqual(len(cm.output), 1, "one warning, not one per message")
        text = "\n".join(cm.output)
        self.assertIn("refused", text)
        self.assertIn("access denied", text)
        self.assertEqual(sent[0][1]["uID"], 7)
        self.assertEqual(sent[0][1]["hWnd"], 4242)

    def test_a_visible_icon_is_not_logged(self):
        sent = []
        icon, cls = self.make_icon()
        with mock.patch("ctypes.sizeof", lambda t: 8), \
                mock.patch("pystray._util.win32", fake_notify()), \
                mock.patch.object(hb, "_shell_notify",
                                  lambda code, data: sent.append(code) or (1, None)), \
                mock.patch.object(hb.log, "warning") as warning:
            cls._message(icon, 0, 0x1)
        warning.assert_not_called()
        self.assertEqual(sent, [0])


if __name__ == "__main__":
    unittest.main()
