"""Physical Windows tray metrics and explicit-size HICON loading.

Keep tray sizing separate from the renderer's default export/notification size.
Like the icon lab, use the taskbar window's DPI rather than the caller's DPI.
"""
import ctypes
from ctypes import wintypes
import io
import sys


_cached_u32 = None


def _user32():
    global _cached_u32
    if _cached_u32 is None:
        u32 = ctypes.WinDLL("user32", use_last_error=True)
        for name, argtypes, restype in (
            ("FindWindowW", [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND),
            ("GetDpiForWindow", [wintypes.HWND], wintypes.UINT),
            ("GetSystemMetricsForDpi", [ctypes.c_int, wintypes.UINT], ctypes.c_int),
            ("CreateIconFromResourceEx", [
                ctypes.c_char_p, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, wintypes.UINT,
            ], wintypes.HANDLE),
            ("DestroyIcon", [wintypes.HANDLE], wintypes.BOOL),
        ):
            try:
                fn = getattr(u32, name)
                fn.argtypes = argtypes
                fn.restype = restype
            except AttributeError:
                pass
        _cached_u32 = u32
    return _cached_u32


def tray_pixels() -> int:
    """Return the taskbar's small icon width in physical pixels (16 elsewhere)."""
    if sys.platform != "win32":
        # HaloBattery targets Windows; this is a safe test/import fallback,
        # not a native menu-bar size recommendation for other platforms.
        return 16
    dpi = 96
    try:
        user32 = _user32()
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if hwnd:
            try:
                dpi = user32.GetDpiForWindow(hwnd) or 96
            except (AttributeError, OSError):
                pass
        try:
            pixels = user32.GetSystemMetricsForDpi(49, dpi)  # SM_CXSMICON
            if pixels > 0:
                return pixels
        except (AttributeError, OSError):
            pass
    except (AttributeError, OSError):
        pass
    return max(1, round(16 * dpi / 96))


def load_icon_handle(image):
    """Load exactly the physical pixels supplied by the renderer in memory.

    The caller owns the resulting HICON and must eventually DestroyIcon it.
    Uses CreateIconFromResourceEx with an in-memory PNG buffer so tray icon
    updates (especially charging animation frames) never touch the filesystem.
    """
    width, height = image.size
    if not (1 <= width <= 256 and 1 <= height <= 256):
        raise ValueError("ICO dimensions must be between 1 and 256 pixels")
    user32 = _user32()
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    data = buf.getvalue()
    handle = user32.CreateIconFromResourceEx(data, len(data), True, 0x00030000, width, height, 0)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def destroy_icon_handle(handle) -> None:
    """Release an owned HICON without truncating a 64-bit Windows handle."""
    if not handle:
        return
    user32 = _user32()
    user32.DestroyIcon.argtypes = [wintypes.HANDLE]
    user32.DestroyIcon.restype = wintypes.BOOL
    user32.DestroyIcon(handle)
