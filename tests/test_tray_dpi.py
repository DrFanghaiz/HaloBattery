"""Tray DPI/HICON tests with mocked Windows calls; no desktop or hardware access."""
import io
from types import SimpleNamespace
import unittest
from unittest import mock

from PIL import Image

from halo import tray_dpi


def user32(dpi=120, metric=20, hwnd=0x100000001):
    return SimpleNamespace(FindWindowW=mock.Mock(return_value=hwnd),
                           GetDpiForWindow=mock.Mock(return_value=dpi),
                           GetSystemMetricsForDpi=mock.Mock(return_value=metric),
                           CreateIconFromResourceEx=mock.Mock(), DestroyIcon=mock.Mock())


class TrayPixelsTests(unittest.TestCase):
    def test_taskbar_dpi_at_supported_scales(self):
        for dpi, pixels in ((96, 16), (120, 20), (144, 24), (192, 32)):
            with self.subTest(dpi=dpi):
                native = user32(dpi, pixels)
                with mock.patch.object(tray_dpi.sys, "platform", "win32"), \
                        mock.patch.object(tray_dpi, "_user32", return_value=native):
                    self.assertEqual(tray_dpi.tray_pixels(), pixels)
                native.FindWindowW.assert_called_once_with("Shell_TrayWnd", None)
                native.GetDpiForWindow.assert_called_once_with(0x100000001)
                native.GetSystemMetricsForDpi.assert_called_once_with(49, dpi)

    def test_metric_is_authoritative_instead_of_assuming_scale(self):
        native = user32(120, 24)
        with mock.patch.object(tray_dpi.sys, "platform", "win32"), \
                mock.patch.object(tray_dpi, "_user32", return_value=native):
            self.assertEqual(tray_dpi.tray_pixels(), 24)

    def test_missing_or_failed_metrics_use_taskbar_dpi(self):
        for failure in (AttributeError, OSError):
            with self.subTest(failure=failure):
                native = user32(144)
                native.GetSystemMetricsForDpi.side_effect = failure
                with mock.patch.object(tray_dpi.sys, "platform", "win32"), \
                        mock.patch.object(tray_dpi, "_user32", return_value=native):
                    self.assertEqual(tray_dpi.tray_pixels(), 24)

    def test_zero_metric_uses_taskbar_dpi(self):
        native = user32(120, 0)
        with mock.patch.object(tray_dpi.sys, "platform", "win32"), \
                mock.patch.object(tray_dpi, "_user32", return_value=native):
            self.assertEqual(tray_dpi.tray_pixels(), 20)

    def test_no_taskbar_or_unknown_dpi_uses_96(self):
        for hwnd, dpi in ((0, 144), (1, 0)):
            native = user32(dpi, 16, hwnd)
            with mock.patch.object(tray_dpi.sys, "platform", "win32"), \
                    mock.patch.object(tray_dpi, "_user32", return_value=native):
                self.assertEqual(tray_dpi.tray_pixels(), 16)
            native.GetSystemMetricsForDpi.assert_called_once_with(49, 96)

    def test_missing_dpi_api_uses_96(self):
        native = user32(metric=16)
        del native.GetDpiForWindow
        with mock.patch.object(tray_dpi.sys, "platform", "win32"), \
                mock.patch.object(tray_dpi, "_user32", return_value=native):
            self.assertEqual(tray_dpi.tray_pixels(), 16)
        native.GetSystemMetricsForDpi.assert_called_once_with(49, 96)

    def test_missing_user32_or_non_windows_is_safe(self):
        with mock.patch.object(tray_dpi.sys, "platform", "win32"), \
                mock.patch.object(tray_dpi, "_user32", side_effect=OSError):
            self.assertEqual(tray_dpi.tray_pixels(), 16)
        with mock.patch.object(tray_dpi.sys, "platform", "linux"), \
                mock.patch.object(tray_dpi, "_user32") as native:
            self.assertEqual(tray_dpi.tray_pixels(), 16)
        native.assert_not_called()


class IconHandleTests(unittest.TestCase):
    def test_single_actual_size_frame_and_explicit_load_dimensions(self):
        for size in (16, 20, 24, 32, 40, 48, 64):
            with self.subTest(size=size):
                native = user32()
                handle = 0x100000001

                def create(data, length, is_icon, version, width, height, flags):
                    self.assertEqual(length, len(data))
                    with Image.open(io.BytesIO(data)) as png:
                        self.assertEqual(png.format, "PNG")
                        self.assertEqual(png.size, (size, size))
                    self.assertEqual((is_icon, version, width, height, flags),
                                     (True, 0x00030000, size, size, 0))
                    return handle

                native.CreateIconFromResourceEx.side_effect = create
                with mock.patch.object(tray_dpi, "_user32", return_value=native):
                    result = tray_dpi.load_icon_handle(Image.new("RGBA", (size, size)))
                self.assertEqual(result, handle)

    def test_save_failure_propagates_without_native_call(self):
        native = user32()
        image = mock.Mock(size=(20, 20))
        image.save.side_effect = OSError("save failed")
        with mock.patch.object(tray_dpi, "_user32", return_value=native):
            with self.assertRaisesRegex(OSError, "save failed"):
                tray_dpi.load_icon_handle(image)
        native.CreateIconFromResourceEx.assert_not_called()

    def test_load_failure_raises(self):
        native = user32()
        native.CreateIconFromResourceEx.return_value = 0
        with mock.patch.object(tray_dpi, "_user32", return_value=native), \
                mock.patch.object(tray_dpi.ctypes, "get_last_error", return_value=5,
                                  create=True), \
                mock.patch.object(tray_dpi.ctypes, "WinError", return_value=OSError("load failed"),
                                  create=True):
            with self.assertRaisesRegex(OSError, "load failed"):
                tray_dpi.load_icon_handle(Image.new("RGBA", (20, 20)))

    def test_invalid_ico_size_is_rejected_before_native_calls(self):
        for size in ((0, 20), (20, 0), (257, 257)):
            with self.subTest(size=size), mock.patch.object(tray_dpi, "_user32") as native:
                with self.assertRaises(ValueError):
                    tray_dpi.load_icon_handle(mock.Mock(size=size))
                native.assert_not_called()

    def test_destroy_preserves_pointer_width_and_ignores_empty_handle(self):
        native = user32()
        with mock.patch.object(tray_dpi, "_user32", return_value=native):
            tray_dpi.destroy_icon_handle(0x100000001)
            tray_dpi.destroy_icon_handle(None)
        native.DestroyIcon.assert_called_once_with(0x100000001)
        self.assertEqual(native.DestroyIcon.argtypes, [tray_dpi.wintypes.HANDLE])
        self.assertEqual(native.DestroyIcon.restype, tray_dpi.wintypes.BOOL)


if __name__ == "__main__":
    unittest.main()
