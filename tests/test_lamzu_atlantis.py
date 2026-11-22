"""LAMZU Atlantis Mini (Champion Edition) tests, with real captured frames.

The Atlantis Mini is CompX hardware: receiver 3554:F50D, cable 3554:F50F. The
frames below are byte-for-byte what the real mouse returned:

    receiver  08 04 00 00 00 02 5f 00 0f ec 00 00 00 00 00 00 ed   95 %, 4076 mV
    cable     08 04 00 00 00 02 5f 01 10 6d 00 00 00 00 00 00 6a   95 %, 4205 mV, charging

No hardware is needed; the HID layer is faked.
"""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import compx as C  # noqa: E402
from providers import lamzu as L  # noqa: E402

WIRELESS_REPLY = bytes.fromhex("08 04 00 00 00 02 5f 00 0f ec 00 00 00 00 00 00 ed")
CABLE_REPLY = bytes.fromhex("08 04 00 00 00 02 5f 01 10 6d 00 00 00 00 00 00 6a")


class FakeCollection:
    def __init__(self, replies=(), fail_open=False):
        self.replies = [list(r) for r in replies]
        self.fail_open = fail_open
        self.opened = 0
        self.sent = []
        self.queue = []

    def open_path(self, path):
        self.opened += 1
        if self.fail_open:
            raise OSError("access denied")

    def write(self, data):
        self.sent.append(list(data))
        self.queue = [list(r) for r in self.replies]
        return len(data)

    def read(self, n, timeout=0):
        return self.queue.pop(0) if self.queue else b""

    def close(self):
        pass


def fake_device_class(cols):
    class FakeDevice:
        def open_path(self, path):
            self.c = cols[path]
            self.c.open_path(path)

        def write(self, data):
            return self.c.write(data)

        def read(self, n, timeout=0):
            return self.c.read(n, timeout)

        def close(self):
            self.c.close()
    return FakeDevice


def atlantis_entries(pid=0xF50D, prefix=b"atlantis", product="LAMZU Atlantis MINI Champion"):
    shape = [(0, 0x0001, 0x0006), (1, 0x0001, 0x0080), (1, 0x000C, 0x0001),
             (1, 0xFF02, 0x0002), (1, 0xFF03, 0x0000), (1, 0xFF04, 0x0002),
             (1, 0xFF05, 0x0000), (2, 0x0001, 0x0002)]
    return [{"vendor_id": L.COMPX_VID, "product_id": pid, "interface_number": i,
             "usage_page": p, "usage": u, "path": prefix + b"-%d-%04x-%d" % (i, p, n),
             "product_string": product}
            for n, (i, p, u) in enumerate(shape)]


class AtlantisTests(unittest.TestCase):
    def setUp(self):
        self._saved = (L.hid, L.hidlist, L.time, L.compx_output_length)
        L.time = types.SimpleNamespace(time=time.time, sleep=lambda s: None)
        L.compx_output_length = lambda path: None

    def tearDown(self):
        L.hid, L.hidlist, L.time, L.compx_output_length = self._saved

    def poll(self, entries, cols):
        L.hid = types.SimpleNamespace(device=fake_device_class(cols))
        L.hidlist = types.SimpleNamespace(
            enumerate=lambda vid=0, pids=None: list(entries) if vid == L.COMPX_VID else [])
        return L.LamzuAtlantisProvider().poll()

    def cols(self, entries, **kw):
        out = {e["path"]: FakeCollection() for e in entries}
        out[entries[3]["path"]] = FakeCollection(**kw)      # 0xFF02:0002 is the control collection
        return out

    def test_real_frames_parse(self):
        self.assertEqual(C.parse_power(WIRELESS_REPLY), (95, False))
        self.assertEqual(C.parse_power(CABLE_REPLY), (95, True))
        self.assertEqual(C.voltage_mv(WIRELESS_REPLY), 4076)
        self.assertEqual(C.voltage_mv(CABLE_REPLY), 4205)

    def test_receiver_is_named_atlantis(self):
        entries = atlantis_entries(0xF50D, b"receiver", "LAMZU 1K Champion Receiver")
        found = self.poll(entries, self.cols(entries, replies=(WIRELESS_REPLY,)))
        self.assertEqual([(s.name, s.level, s.charging, s.online) for s in found],
                         [("LAMZU Atlantis Mini (1K receiver)", 95, False, True)])

    def test_cable_is_named_atlantis_not_thorn(self):
        entries = atlantis_entries(0xF50F, b"cable", "LAMZU Atlantis MINI Champion")
        found = self.poll(entries, self.cols(entries, replies=(CABLE_REPLY,)))
        self.assertEqual([(s.name, s.level, s.charging) for s in found],
                         [("LAMZU Atlantis Mini (wired)", 95, True)])

    def test_thorn_cable_keeps_its_name(self):
        entries = atlantis_entries(0xF50F, b"cable", "LAMZU THORN")
        found = self.poll(entries, self.cols(entries, replies=(CABLE_REPLY,)))
        self.assertEqual([s.name for s in found], ["LAMZU Thorn (wired)"])


if __name__ == "__main__":
    unittest.main()
