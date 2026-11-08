"""Tests for providers/ninjutso.py. No hardware is needed.

The fake Sora V2 8K dongle has the collections a real one (1915:AE8C) shows,
minus the keyboard one, and answers the feature report exchange:

    05 15 00 00 01 00 ... out  ->  05 15 00 00 01 00 00 00 00 <level> <chg> <full> <online> back

The fake Sora V3 receiver (093A:EB02) has the four vendor collections the real
one shows on interface 2 (0xFF00, two 0xFF01 and 0xFF02) and answers report 6 on
the second 0xFF01 one only, echoing the command in byte 1:

    06 <cmd> 00 00 01 00 ... out  ->  06 <cmd> 00 00 00 00 01 00 <value> ... back

Run from the repository root:

    python -m unittest discover -s tests
"""
import os
import sys
import time
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from providers import ninjutso as N  # noqa: E402

REPORT = 32
V3_REPLY_LEN = 65                    # what hidapi returns for a 64-byte read


class FakeVendorCollection:
    """Sora V2. answer: None = never answers (asleep/off); else (level, charging, full, online)."""

    def __init__(self, answer=(99, 0, 0, 1)):
        self.answer = answer
        self.sent = []
        self.opened = 0
        self.gets = 0

    def reply(self):
        self.gets += 1
        if not self.sent or self.answer is None:
            return [0x00] * REPORT
        level, chg, full, online = self.answer
        return ([N.REPORT_ID, 0x15, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00,
                 level, chg, full, online] + [0x00] * (REPORT - 13))

    def answer_report(self, report_id, n):
        if report_id != N.REPORT_ID:
            raise OSError("read error")        # what a collection without that report id says
        assert n == REPORT, n
        return self.reply()


class FakeV3Collection:
    """Sora V3. dead = never answers report 6, like the 0xFF00/0xFF02 collections."""

    def __init__(self, level=81, charging=0, online=1, dead=False, answer=True):
        self.level, self.charging, self.online = level, charging, online
        self.dead = dead
        self.answer = answer
        self.sent = []
        self.opened = 0

    def answer_report(self, report_id, n):
        assert report_id == N.V3_REPORT_ID and n == N.V3_READ_LEN, (report_id, n)
        if self.dead or not self.sent or not self.answer:
            raise OSError("read error")
        cmd = self.sent[-1][1]
        value = {N.V3_CMD_ONLINE: self.online,
                 N.V3_CMD_BATTERY: self.level,
                 N.V3_CMD_CHARGING: self.charging}.get(cmd)
        if value is None:
            raise OSError("read error")
        return ([N.V3_REPORT_ID, cmd, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, value,
                 0x16, 0xAE] + [0x00] * (V3_REPLY_LEN - 11))


def fake_device_class(cols):
    class FakeDevice:
        def open_path(self, path):
            self.c = cols[path]
            self.c.opened += 1

        def send_feature_report(self, data):
            self.c.sent.append(list(data))
            return len(data)

        def get_feature_report(self, report_id, n):
            return self.c.answer_report(report_id, n)

        def close(self):
            pass
    return FakeDevice


def sora_entries(pid=0xAE8C, prefix=b"dongle", product="Ninjutso Sora V2 8K"):
    # the collections of a Sora V2 8K dongle in a real hid.enumerate() result
    shape = [(0, 0x0001, 0x0002), (1, 0x0001, 0x0006), (1, 0x000C, 0x0001),
             (1, 0xFFA0, 0x0001)]
    return [{"vendor_id": N.NINJUTSO_VID, "product_id": pid, "interface_number": i,
             "usage_page": p, "usage": u, "path": prefix + b"-%d-%04x" % (i, p),
             "product_string": product, "manufacturer_string": "Ninjutso"}
            for i, p, u in shape]


def v3_entries(pid=0xEB02, product="Ninjutso Sora V3"):
    # the four vendor collections of a Sora V3 8K receiver, all on interface 2:
    # 0xFF00, 0xFF01 (twice - a 16-byte and a 64-byte feature report) and 0xFF02
    shape = [(0x0001, 0x0080), (0x0001, 0x0006), (0x000C, 0x0001),
             (0xFF00, 0x0001), (0xFF01, 0x0001), (0xFF01, 0x0001), (0xFF02, 0x0001)]
    return [{"vendor_id": N.PIXART_VID, "product_id": pid, "interface_number": 2,
             "usage_page": p, "usage": u,
             "path": b"v3-%04x-%d-%04x-%02x" % (pid, i, p, u),
             "product_string": product, "manufacturer_string": "Ninjutso Inc."}
            for i, (p, u) in enumerate(shape)]


V3_ANSWER_INDEX = 5                  # the second 0xFF01 collection, as on the hardware


def v3_cols(entries, **kw):
    cols = {e["path"]: FakeV3Collection(dead=True) for e in entries}
    cols[entries[V3_ANSWER_INDEX]["path"]] = FakeV3Collection(**kw)
    return cols


class ParseReportTest(unittest.TestCase):
    def test_the_reply_seen_on_hardware(self):
        raw = [0x05, 0x15, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x63,
               0x00, 0x00, 0x01] + [0x00] * 19
        self.assertEqual(N.parse_report(raw), (99, False))

    def test_charging_and_fully_charged(self):
        base = [0x05, 0x15, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]
        self.assertEqual(N.parse_report(base + [0x41, 0x01, 0x00, 0x01] + [0] * 19), (65, True))
        self.assertEqual(N.parse_report(base + [0x64, 0x00, 0x01, 0x01] + [0] * 19), (100, True))

    def test_a_buffer_without_the_report_id_is_read_too(self):
        raw = [0x15, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x50,
               0x00, 0x00, 0x01] + [0x00] * 20
        self.assertEqual(N.parse_report(raw), (80, False))

    def test_no_reply_and_other_commands_are_refused(self):
        self.assertIsNone(N.parse_report(None))
        self.assertEqual(N.parse_report([]), None)
        self.assertEqual(N.parse_report([0x05, 0x16] + [0x00] * 30), None)
        self.assertEqual(N.parse_report([0x05, 0x15] + [0x00] * 5), None)

    def test_a_sleeping_mouse_and_a_zero_level_are_no_reading(self):
        base = [0x05, 0x15, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]
        self.assertIsNone(N.parse_report(base + [0x00, 0x00, 0x00, 0x00] + [0] * 19))
        self.assertIsNone(N.parse_report(base + [0x00, 0x00, 0x00, 0x01] + [0] * 19))

    def test_a_level_out_of_range_is_refused(self):
        base = [0x05, 0x15, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00]
        self.assertIsNone(N.parse_report(base + [0x65, 0x00, 0x00, 0x01] + [0] * 19))


class ParseV3Test(unittest.TestCase):
    def test_the_replies_seen_on_hardware(self):
        online = [0x06, 0x13, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x16, 0xAE]
        self.assertEqual(N.parse_v3(online, N.V3_CMD_ONLINE), 1)
        battery = [0x06, 0x12, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x51, 0x16, 0xAE]
        self.assertEqual(N.parse_v3(battery, N.V3_CMD_BATTERY), 0x51)
        charging = [0x06, 0x11, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x16, 0xAE]
        self.assertEqual(N.parse_v3(charging, N.V3_CMD_CHARGING), 0)

    def test_an_answer_to_another_command_is_refused(self):
        battery = [0x06, 0x12, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x51]
        self.assertIsNone(N.parse_v3(battery, N.V3_CMD_ONLINE))
        self.assertIsNone(N.parse_v3(battery, N.V3_CMD_CHARGING))

    def test_a_buffer_without_the_report_id_is_read_too(self):
        raw = [0x12, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x51]
        self.assertEqual(N.parse_v3(raw, N.V3_CMD_BATTERY), 0x51)

    def test_short_and_empty_buffers_are_refused(self):
        self.assertIsNone(N.parse_v3(None, N.V3_CMD_BATTERY))
        self.assertIsNone(N.parse_v3([], N.V3_CMD_BATTERY))
        self.assertIsNone(N.parse_v3([0x06, 0x12, 0x00], N.V3_CMD_BATTERY))
        self.assertIsNone(N.parse_v3([0x00] * 9, N.V3_CMD_BATTERY))

    def test_the_asleep_replies_seen_on_hardware(self):
        # asleep, the receiver still echoes cmd 19 but reports 0 ...
        online = [0x06, 0x13, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x16, 0xAE]
        self.assertEqual(N.parse_v3(online, N.V3_CMD_ONLINE), 0)
        # ... and answers the other two with a bare echo and no value
        empty = [0x06, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]
        self.assertIsNone(N.parse_v3(empty, N.V3_CMD_BATTERY))
        self.assertIsNone(N.parse_v3(empty, N.V3_CMD_CHARGING))


class PollBase(unittest.TestCase):
    def setUp(self):
        self._saved = (N.hid, N.hidlist, N.time)
        N.time = types.SimpleNamespace(time=time.time, sleep=lambda s: None)
        self.provider = N.NinjutsoProvider()

    def tearDown(self):
        N.hid, N.hidlist, N.time = self._saved

    def poll(self, entries, cols):
        N.hid = types.SimpleNamespace(device=fake_device_class(cols))
        N.hidlist = types.SimpleNamespace(enumerate=lambda vid=0, pid=0: list(entries))
        return self.provider.poll()

    def cols(self, entries, answer=(99, 0, 0, 1)):
        return {e["path"]: FakeVendorCollection(answer) for e in entries}


class PollTest(PollBase):

    def test_dongle(self):
        entries = sora_entries()
        res = self.poll(entries, self.cols(entries))
        self.assertEqual([(r.key, r.name, r.level, r.charging, r.online, r.kind)
                          for r in res],
                         [("ninjutso", "Ninjutso Sora V2", 99, False, True, "mouse")])

    def test_only_the_vendor_collection_gets_the_request(self):
        entries = sora_entries()
        cols = self.cols(entries)
        self.poll(entries, cols)
        touched = [n for n, e in enumerate(entries) if cols[e["path"]].opened]
        self.assertEqual(touched, [3])                    # usage page 0xFFA0 only
        self.assertEqual(cols[entries[3]["path"]].sent,
                         [[N.REPORT_ID, 0x15, 0x00, 0x00, 0x01] + [0x00] * 27])

    def test_the_report_is_32_bytes_whatever_the_collection_declares(self):
        entries = sora_entries()
        cols = self.cols(entries)
        self.poll(entries, cols)
        self.assertEqual(len(cols[entries[3]["path"]].sent[0]), 32)

    def test_no_vendor_collection_sends_nothing(self):
        entries = [e for e in sora_entries() if e["usage_page"] != 0xFFA0]
        cols = self.cols(entries)
        self.assertEqual(self.poll(entries, cols), [])
        self.assertTrue(all(not c.opened for c in cols.values()))

    def test_charging(self):
        entries = sora_entries()
        res = self.poll(entries, self.cols(entries, answer=(64, 1, 0, 1)))
        self.assertEqual([(r.level, r.charging) for r in res], [(64, True)])

    def test_cable_is_asked_first_and_wins(self):
        dongle = sora_entries()
        cable = sora_entries(0xAE12, b"cable")
        cols = {**self.cols(dongle, answer=(0, 0, 0, 0)),
                **self.cols(cable, answer=(88, 1, 0, 1))}
        res = self.poll(dongle + cable, cols)
        self.assertEqual([(r.level, r.charging) for r in res], [(88, True)])
        self.assertFalse(any(c.sent for p, c in cols.items() if p.startswith(b"dongle")))

    def test_sleeping_mouse_keeps_its_last_level_greyed(self):
        entries = sora_entries()
        cols = self.cols(entries)
        self.poll(entries, cols)
        for c in cols.values():
            c.answer = None
        res = self.poll(entries, cols)
        self.assertEqual([(r.level, r.online, r.wake_on_input) for r in res],
                         [(99, False, True)])
        self.provider._last = (99, False, time.time() - N.ASLEEP_KEEP)
        self.assertEqual(self.poll(entries, cols), [])

    def test_other_pids_are_ignored(self):
        e = [dict(sora_entries()[3], product_id=0x1234, product_string="Nordic thing",
                  manufacturer_string="")]
        cols = self.cols(e)
        self.assertEqual(self.poll(e, cols), [])
        self.assertEqual(cols[e[0]["path"]].opened, 0)

    def test_an_unknown_pid_whose_strings_say_ninjutso_is_tried(self):
        e = [dict(sora_entries()[3], product_id=0xAE99)]
        res = self.poll(e, self.cols(e))
        self.assertEqual([(r.name, r.level) for r in res],
                         [("Ninjutso Sora V2", 99)])

    def test_a_dongle_that_never_answers_shows_nothing(self):
        entries = sora_entries()
        res = self.poll(entries, self.cols(entries, answer=None))
        self.assertEqual(res, [])
        self.assertTrue(any("no 0x15 reply" in line for line in self.provider.diagnostics()))


class PollV3Test(PollBase):
    def test_receiver(self):
        entries = v3_entries()
        res = self.poll(entries, v3_cols(entries))
        self.assertEqual([(r.key, r.name, r.level, r.charging, r.online, r.kind)
                          for r in res],
                         [("ninjutso-v3", "Ninjutso Sora V3", 81, False, True, "mouse")])

    def test_the_commands_the_web_driver_sends(self):
        entries = v3_entries()
        cols = v3_cols(entries)
        self.poll(entries, cols)
        answer = cols[entries[V3_ANSWER_INDEX]["path"]]
        self.assertEqual(answer.sent,
                         [[N.V3_REPORT_ID, N.V3_CMD_ONLINE, 0x00, 0x00, 0x01] + [0x00] * 11,
                          [N.V3_REPORT_ID, N.V3_CMD_BATTERY, 0x00, 0x00, 0x01] + [0x00] * 11,
                          [N.V3_REPORT_ID, N.V3_CMD_CHARGING, 0x00, 0x00, 0x01] + [0x00] * 11])

    def test_only_the_vendor_pages_are_opened(self):
        entries = v3_entries()
        cols = v3_cols(entries)
        self.poll(entries, cols)
        touched = [n for n, e in enumerate(entries) if cols[e["path"]].opened]
        self.assertEqual(touched, [3, 4, 5])   # the mouse/keyboard collections never
        self.assertFalse(cols[entries[6]["path"]].opened)   # asked: stops at the answer

    def test_only_the_answering_collection_gets_the_three_commands(self):
        entries = v3_entries()
        cols = v3_cols(entries)
        self.poll(entries, cols)
        written = [n for n, e in enumerate(entries) if cols[e["path"]].sent]
        self.assertEqual(written, [3, 4, 5])   # 0xFF00 and the first 0xFF01 are tried first
        self.assertTrue(cols[entries[V3_ANSWER_INDEX]["path"]].sent)
        self.assertEqual(len(cols[entries[V3_ANSWER_INDEX]["path"]].sent), 3)

    def test_charging(self):
        entries = v3_entries()
        res = self.poll(entries, v3_cols(entries, charging=1))
        self.assertEqual([(r.level, r.charging) for r in res], [(81, True)])

    def test_the_charging_byte_seen_on_hardware_is_2(self):
        # the cable replied 06 11 00 00 00 00 01 00 02 while the mouse charged
        entries = v3_entries(pid=0xE010)
        res = self.poll(entries, v3_cols(entries, charging=2))
        self.assertEqual([(r.level, r.charging) for r in res], [(81, True)])

    def test_the_wired_id_is_read(self):
        entries = v3_entries(pid=0xE010)
        res = self.poll(entries, v3_cols(entries))
        self.assertEqual([(r.key, r.level) for r in res], [("ninjutso-v3", 81)])

    def test_cable_is_asked_first_and_wins(self):
        receiver = v3_entries()
        cable = v3_entries(pid=0xE010)
        cols = {**v3_cols(receiver, level=50), **v3_cols(cable, level=64, charging=2)}
        res = self.poll(receiver + cable, cols)
        self.assertEqual([(r.level, r.charging) for r in res], [(64, True)])
        self.assertFalse(any(c.sent for p, c in cols.items() if p.startswith(b"v3-eb02")))

    def test_a_sleeping_mouse_is_not_asked_for_its_level(self):
        entries = v3_entries()
        cols = v3_cols(entries)
        self.poll(entries, cols)                        # seen awake at 81 %
        asleep = FakeV3Collection(online=0, level=0)
        cols[entries[V3_ANSWER_INDEX]["path"]] = asleep
        res = self.poll(entries, cols)
        self.assertEqual([(r.level, r.online, r.wake_on_input) for r in res],
                         [(81, False, True)])
        self.assertEqual([cmd[1] for cmd in asleep.sent], [N.V3_CMD_ONLINE])
        self.assertTrue(any("asleep" in line for line in self.provider.diagnostics()))

    def test_a_mouse_out_of_range_keeps_the_last_level(self):
        entries = v3_entries()
        cols = v3_cols(entries)
        self.poll(entries, cols)
        cols[entries[V3_ANSWER_INDEX]["path"]] = FakeV3Collection(online=0, level=0)
        res = self.poll(entries, cols)
        self.assertEqual([(r.level, r.online, r.wake_on_input) for r in res],
                         [(81, False, True)])
        self.provider._last_v3 = (81, False, time.time() - N.ASLEEP_KEEP)
        self.assertEqual(self.poll(entries, cols), [])

    def test_both_mice_at_once(self):
        v2, v3 = sora_entries(), v3_entries()
        cols = {**self.cols(v2), **v3_cols(v3)}
        res = self.poll(v2 + v3, cols)
        self.assertEqual([(r.key, r.level) for r in res],
                         [("ninjutso", 99), ("ninjutso-v3", 81)])

    def test_a_receiver_that_never_answers_shows_nothing(self):
        entries = v3_entries()
        cols = {e["path"]: FakeV3Collection(dead=True) for e in entries}
        self.assertEqual(self.poll(entries, cols), [])
        self.assertTrue(any("read error" in line for line in self.provider.diagnostics()))

    def test_an_unknown_receiver_id_whose_strings_say_ninjutso_is_tried(self):
        entries = v3_entries(pid=0xEB99)
        res = self.poll(entries, v3_cols(entries))
        self.assertEqual([(r.name, r.level) for r in res], [("Ninjutso Sora V3", 81)])

    def test_an_unknown_receiver_id_without_the_strings_is_ignored(self):
        entries = [dict(e, product_id=0xEB99) for e in v3_entries()]
        for e in entries:
            e["product_string"], e["manufacturer_string"] = "PixArt thing", ""
        cols = v3_cols(entries)
        self.assertEqual(self.poll(entries, cols), [])
        self.assertTrue(all(not c.opened for c in cols.values()))


if __name__ == "__main__":
    unittest.main()
