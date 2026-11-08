"""Tests for the LAMZU Atlantis Mini part of providers/lamzu.py. No hardware needed.

The Atlantis Mini Champion Edition is Compx hardware (3554:F50D receiver, 3554:F50F
on its cable) and answers the 17-byte frame of providers/pulsar.py instead of the
Maya X feature-report exchange. The two replies below are byte-for-byte what the real
mouse returned on this machine:

    receiver  08 04 00 00 00 02 5f 00 0f ec 00 00 00 00 00 00 ed   95 %, 4076 mV
    cable     08 04 00 00 00 02 5f 01 10 6d 00 00 00 00 00 00 6a   95 %, 4205 mV, charging

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import lamzu as L  # noqa: E402
from providers import pulsar as P  # noqa: E402

WIRELESS_REPLY = bytes.fromhex("08 04 00 00 00 02 5f 00 0f ec 00 00 00 00 00 00 ed")
CABLE_REPLY = bytes.fromhex("08 04 00 00 00 02 5f 01 10 6d 00 00 00 00 00 00 6a")
REQUEST = [0x08, 0x04] + [0x00] * 14 + [0x49]          # 17 bytes, checksum 0x55-0x0c


class FakeCollection:
    """One HID collection. `replies` is what a read returns after a write."""

    def __init__(self, replies=(WIRELESS_REPLY,), fail_open=False, fail_write=False,
                 feature=None):
        self.replies = [list(r) for r in replies]
        self.fail_open = fail_open
        self.fail_write = fail_write
        self.feature = feature                 # what get_feature_report returns
        self.opened = 0
        self.sent = []
        self.features_sent = []
        self.queue = []


def fake_device_class(cols):
    class FakeDevice:
        def open_path(self, path):
            self.c = cols[path]
            if self.c.fail_open:
                raise OSError("access denied")
            self.c.opened += 1

        def write(self, data):
            if self.c.fail_write:
                raise OSError("write failed")
            self.c.sent.append(list(data))
            self.c.queue = [list(r) for r in self.c.replies]
            return len(data)

        def read(self, n, timeout=0):
            return self.c.queue.pop(0) if self.c.queue else []

        def send_feature_report(self, data):
            self.c.features_sent.append(list(data))
            return len(data)

        def get_feature_report(self, report_id, n):
            return list(self.c.feature or [0x00] * 65)

        def close(self):
            pass
    return FakeDevice


def atlantis_entries(pid=L.ATLANTIS_RECEIVER, prefix=b"atlantis"):
    # the eight collections of the real mouse, as dev_send.py caps reported them
    shape = [(0, 0x0001, 0x0006), (1, 0x0001, 0x0080), (1, 0x000C, 0x0001),
             (1, 0xFF02, 0x0002), (1, 0xFF03, 0x0000), (1, 0xFF04, 0x0002),
             (1, 0xFF05, 0x0000), (2, 0x0001, 0x0002)]
    name = ("LAMZU 1K Champion Receiver" if pid == L.ATLANTIS_RECEIVER
            else "LAMZU Atlantis MINI Champion")
    return [{"product_id": pid, "interface_number": i, "usage_page": p, "usage": u,
             "path": prefix + b"-%d-%04x-%d" % (i, p, n), "product_string": name}
            for n, (i, p, u) in enumerate(shape)]


def maya_entries(pid=L.DONGLE, prefix=b"maya"):
    shape = [(1, 0xFFA0, 0x01), (1, 0xFFFF, 0x01), (2, 0xFFFF, 0x00)]
    return [{"product_id": pid, "interface_number": i, "usage_page": p, "usage": u,
             "path": prefix + b"-%d-%04x-%d" % (i, p, n),
             "product_string": "LAMZU Maya X 8K Dongle"}
            for n, (i, p, u) in enumerate(shape)]


class CompxBase(unittest.TestCase):
    def setUp(self):
        self._saved = (L.hid, L.hidlist, L.time, L.output_length)
        L.time = types.SimpleNamespace(time=time.time, sleep=lambda s: None)
        L.output_length = lambda path: None          # Windows is not asked in tests
        self.provider = L.LamzuProvider()

    def tearDown(self):
        L.hid, L.hidlist, L.time, L.output_length = self._saved

    def poll(self, entries, cols):
        L.hid = types.SimpleNamespace(device=fake_device_class(cols))
        L.hidlist = types.SimpleNamespace(enumerate=lambda vid=0, pid=0: list(entries))
        return self.provider.poll()

    def cols(self, entries, **kw):
        # only the 0xFF02 collection answers; the rest are inert
        out = {}
        for e in entries:
            if (e["usage_page"], e["usage"]) == L.ATLANTIS_CONTROL:
                out[e["path"]] = FakeCollection(**kw)
            else:
                out[e["path"]] = FakeCollection(replies=())
        return out


class ReceiverTest(CompxBase):
    def test_receiver_reports_the_real_frames(self):
        entries = atlantis_entries()
        res = self.poll(entries, self.cols(entries))
        self.assertEqual([(r.key, r.name, r.level, r.charging, r.online, r.kind) for r in res],
                         [(L.ATLANTIS_KEY, L.ATLANTIS_NAME, 95, False, True, "mouse")])

    def test_the_raw_bytes_from_the_mouse_parse(self):
        self.assertEqual(P.parse_power(WIRELESS_REPLY), (95, False))
        self.assertEqual(P.parse_power(CABLE_REPLY), (95, True))
        self.assertEqual(P.voltage_mv(WIRELESS_REPLY), 4076)
        self.assertEqual(P.voltage_mv(CABLE_REPLY), 4205)

    def test_only_the_control_collection_gets_the_frame(self):
        entries = atlantis_entries()
        cols = self.cols(entries)
        self.poll(entries, cols)
        touched = [e["path"] for e in entries if cols[e["path"]].opened]
        self.assertEqual(touched, [e["path"] for e in entries
                                   if (e["usage_page"], e["usage"]) == L.ATLANTIS_CONTROL])
        self.assertEqual(cols[touched[0]].sent, [REQUEST])
        self.assertEqual(len(REQUEST), 17)

    def test_cable_charges(self):
        entries = atlantis_entries(L.ATLANTIS_WIRED, b"cable")
        res = self.poll(entries, self.cols(entries, replies=(CABLE_REPLY,)))
        self.assertEqual([(r.level, r.charging) for r in res], [(95, True)])

    def test_cable_is_asked_first_and_wins(self):
        wired = atlantis_entries(L.ATLANTIS_WIRED, b"cable")
        receiver = atlantis_entries(L.ATLANTIS_RECEIVER, b"receiver")
        cols = {**self.cols(wired, replies=(CABLE_REPLY,)), **self.cols(receiver)}
        res = self.poll(wired + receiver, cols)
        self.assertEqual([(r.level, r.charging) for r in res], [(95, True)])
        self.assertFalse(any(c.sent for p, c in cols.items() if p.startswith(b"receiver")))

    def test_maya_x_and_atlantis_can_both_be_present(self):
        atl = atlantis_entries()
        maya = maya_entries()
        cols = self.cols(atl)
        for e in maya:
            cols[e["path"]] = FakeCollection(replies=[], feature=[
                0x00, 0xA1, 0x00, 0x02, 0x02, 0x00, 0x83, 0x00, 0x51] + [0x00] * 56)
        res = self.poll(atl + maya, cols)
        self.assertEqual(sorted((r.key, r.level, r.charging, r.source) for r in res),
                         sorted([(L.ATLANTIS_KEY, 95, False, "lamzu"),
                                 ("lamzu", 81, False, "lamzu")]))

    def test_other_compx_pids_are_ignored(self):
        entries = [dict(e, product_id=0xF508) for e in atlantis_entries()]
        cols = self.cols(entries)
        self.assertEqual(self.poll(entries, cols), [])
        self.assertTrue(all(not c.opened for c in cols.values()))


class SleepAndFailureTest(CompxBase):
    def test_sleeping_mouse_keeps_its_last_value_greyed(self):
        entries = atlantis_entries()
        cols = self.cols(entries)
        self.poll(entries, cols)
        cols[entries[3]["path"]].replies = []           # it stops answering
        res = self.poll(entries, cols)
        self.assertEqual([(r.level, r.charging, r.online) for r in res], [(95, False, False)])
        self.provider._atlantis = (95, False, time.time() - L.ASLEEP_KEEP)
        self.assertEqual(self.poll(entries, cols), [])

    def test_unplugged_receiver_drops_the_device_and_its_memory(self):
        entries = atlantis_entries()
        self.poll(entries, self.cols(entries))
        res = self.poll([], {})
        self.assertEqual(res, [])
        self.assertIsNone(self.provider._atlantis)

    def test_no_reply_at_all_is_not_an_error(self):
        entries = atlantis_entries()
        self.assertEqual(self.poll(entries, self.cols(entries, replies=())), [])

    def test_open_failing_is_not_an_error(self):
        entries = atlantis_entries()
        self.assertEqual(self.poll(entries, self.cols(entries, fail_open=True)), [])

    def test_write_failing_is_not_an_error(self):
        entries = atlantis_entries()
        self.assertEqual(self.poll(entries, self.cols(entries, fail_write=True)), [])
        self.assertTrue(any("query error" in line for line in self.provider.diagnostics()))

    def test_no_vendor_collection_sends_nothing(self):
        entries = [e for e in atlantis_entries() if e["usage_page"] < 0xFF00]
        cols = {e["path"]: FakeCollection(replies=()) for e in entries}
        self.assertEqual(self.poll(entries, cols), [])
        self.assertTrue(all(not c.opened for c in cols.values()))

    def test_a_collection_that_cannot_take_17_bytes_is_skipped(self):
        entries = atlantis_entries()
        cols = self.cols(entries)
        L.output_length = lambda path: {entries[3]["path"]: 16,
                                        entries[4]["path"]: 17}.get(path)
        # 0xFF02 refuses the frame, 0xFF03 can take it: the second one is used
        self.assertEqual(self.poll(entries, cols), [])
        self.assertEqual(cols[entries[3]["path"]].sent, [])
        self.assertEqual(cols[entries[4]["path"]].sent, [REQUEST])

    def test_a_bad_checksum_is_ignored(self):
        broken = bytearray(WIRELESS_REPLY)
        broken[16] ^= 0xFF
        self.assertIsNone(P.parse_power(bytes(broken)))
        entries = atlantis_entries()
        self.assertEqual(self.poll(entries, self.cols(entries, replies=(bytes(broken),))), [])

    def test_a_power_event_is_ignored(self):
        event = bytes.fromhex("080a000000000000000000000000000043")
        self.assertIsNone(P.parse_power(event))
        entries = atlantis_entries()
        self.assertEqual(self.poll(entries, self.cols(entries, replies=(event,))), [])

    def test_an_impossible_level_is_ignored(self):
        bad = bytearray(WIRELESS_REPLY)
        bad[6] = 0x7F
        bad[16] = (0x55 - sum(bad[:16])) % 256
        self.assertIsNone(P.parse_power(bytes(bad)))


if __name__ == "__main__":
    unittest.main()
