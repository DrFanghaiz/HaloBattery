"""Ninjutso mice: the Sora V2 behind its 8K dongle (1915:AE8C) or on the cable
(1915:AE12), and the Sora V3 behind its PixArt receiver (093A:EB02 and siblings).

Sora V2 - a 32-byte HID *feature* report, id 5, on the vendor collection (usage
page 0xFFA0, usage 0x0001), the exchange Ninjutso's own web driver makes
(reverse-engineered by Fan4Metal/mouse_tray, MIT - mouse_tray/drivers/vendor/
ninjutso.py, and its predecessor Fan4Metal/Sora_tray):

    request   feature report 5, 32 bytes: 05 15 00 00 01 00 ...
    reply     feature report 5, 32 bytes: 05 15 00 00 01 00 00 00 00 <level>
                                          <charging> <full> <online>

  byte 9  level in percent
  byte 10 charging
  byte 11 fully charged
  byte 12 the mouse is awake behind the dongle (0: asleep, level 0 as well)

Nothing else is ever written to the mouse. The reply comes back in about 90 ms,
which is the delay the reference waits before reading it, and the request is
repeated for every other attempt. The report is 32 bytes even though the
collection declares a 74-byte feature report: a 74-byte buffer is refused by
hidapi/Windows (ERROR_INVALID_PARAMETER, Win32 87) on this device.

Sora V3 - a PixArt chipset and a different protocol, taken from Ninjutso's web
driver itself (ninjaforce.ninjutso.cn, /_nuxt/DAqgT4Sq.js): feature report 6,
15-byte payloads, one command per exchange, answered on the 0xFF01 collection by
a report that echoes the command in byte 1:

    request   feature report 6, 16 bytes: 06 <cmd> 00 00 01 00 ...
    reply     feature report 6, 64 bytes: 06 <cmd> 00 00 00 00 01 00 <value>

  cmd 19  the receiver sees the mouse awake (byte 8 == 1)
  cmd 18  level in percent (byte 8)
  cmd 17  charging (byte 8 != 0)
  cmd 21  firmware version, bytes 9..11 - not read here

The web driver's own tables put the V3 receivers at 0xEB02/0xE010 (8K),
0xEB01/0xE020 and 0xEB03/0xE021, all on PixArt's vendor id 0x093A.

The receiver answers while the mouse sleeps too (online = 0, level 0 - as the
reference treats it), so a sleeping mouse keeps its last level, greyed out, for
ASLEEP_KEEP seconds and the app polls at once when new input wakes it.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

try:
    import hid
except ImportError:              # pragma: no cover
    hid = None

from . import hidlist
from .base import DeviceStatus, Provider, hexdump, log

NINJUTSO_VID = 0x1915

# product id -> connection. AE8C is the 8K dongle confirmed on hardware; the other
# three are the ids Ninjutso used for other Sora V2 revisions (AE1C wireless and AE11
# on the cable, both from mouse_tray) and the wired id Windows still had in its
# registry on the machine this was written on.
PIDS = {
    0xAE8C: "dongle",
    0xAE1C: "dongle",
    0xAE12: "cable",
    0xAE11: "cable",
}
NAME = "Ninjutso Sora V2"

USAGE_PAGE = 0xFFA0              # the vendor collection, on interface 1 here
REPORT_ID = 5
REPORT_LEN = 32                  # despite the 74-byte feature report the collection declares
QUERY = [REPORT_ID, 0x15, 0x00, 0x00, 0x01]

REPLY_DELAY = 0.09               # s, the delay the reference driver waits
REPLY_TRIES = 3
ASLEEP_KEEP = 300                # s, as in the other receiver mice

# --- Sora V3 (PixArt) ---------------------------------------------------------
PIXART_VID = 0x093A

# product id -> connection, straight from the web driver's tables: it lists the
# Sora V3 as three variants (0xEB02/0xE010, 0xEB01/0xE020, 0xEB03/0xE021), each a
# 8K receiver id and the id the mouse itself takes on the cable. Both halves of the
# first variant are confirmed on hardware - 0xEB02 with the receiver alone and
# 0xE010 as soon as the mouse was plugged in, charging reported on that one.
PIXART_PIDS = {
    0xEB02: "8k receiver",
    0xEB01: "8k receiver",
    0xEB03: "8k receiver",
    0xE010: "cable",
    0xE020: "cable",
    0xE021: "cable",
}
V3_NAME = "Ninjutso Sora V3"

# the vendor collections; only the 64-byte 0xFF01 one answers report 6 on the
# hardware seen so far, so the pages are tried in that order
PIXART_PAGES = (0xFF01, 0xFF00, 0xFF02)
V3_REPORT_ID = 6
V3_PAYLOAD = 15                  # bytes the web driver sends, report id excluded
V3_CMD_ONLINE = 19
V3_CMD_BATTERY = 18
V3_CMD_CHARGING = 17
V3_TRIES = 4
V3_DELAY = 0.03                  # s, as in the web driver
V3_DELAY_LATER = 0.06
V3_READ_LEN = 64                 # the reply is 65 bytes: id + 64

Reading = Tuple[int, bool]
V3Reading = Tuple[int, bool, bool]


def looks_like_sora(d: dict) -> bool:
    """A collection this module has no id for: the vendor ids are chip vendors',
    so only the strings may say it is a Ninjutso mouse."""
    text = " ".join(str(d.get(k) or "") for k in ("product_string", "manufacturer_string"))
    text = text.lower()
    return "ninjutso" in text or "sora" in text


def parse_report(resp) -> Optional[Reading]:
    """level, charging out of the 32-byte Sora V2 reply - None when it is not one.

    The report id is expected at index 0, as hidapi returns it; a buffer without
    the id (the 0x15 first) is accepted too, at the previous indices.
    """
    r = list(resp or [])
    if len(r) < 12:
        return None
    i = 1 if r[0] == REPORT_ID else 0
    if len(r) < i + 12 or r[i] != 0x15:
        return None
    level, charging, full, online = r[i + 8], r[i + 9], r[i + 10], r[i + 11]
    if not online or not 0 < level <= 100:
        return None                  # asleep (or off): no level, keep the last one
    return level, bool(charging or full)


def parse_v3(resp, cmd: int) -> Optional[int]:
    """The value byte of a Sora V3 reply - None when it is not the answer to cmd.

    The reply echoes the command in byte 1 and carries the value in byte 8; a
    buffer without the report id (the command first) is accepted too, at the
    previous indices.
    """
    r = list(resp or [])
    if not r:
        return None
    i = 0 if r[0] == V3_REPORT_ID else -1
    if len(r) < i + 9 or r[i + 1] != cmd:
        return None
    return r[i + 8]


class NinjutsoProvider(Provider):
    name = "ninjutso"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, bool, float]] = None
        self._last_v3: Optional[Tuple[int, bool, float]] = None

    # --- Sora V2 (Nordic) ---
    def _read(self, path) -> Optional[Reading]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            req = QUERY + [0x00] * (REPORT_LEN - len(QUERY))
            try:
                dev.send_feature_report(req)
            except (OSError, ValueError) as e:
                # the dongle also answers a bare get on this unit: try that rather
                # than give up on a firmware that refuses the write
                self._diag.append(f"    send: {e} (reading back anyway)")
            for i in range(REPLY_TRIES):
                time.sleep(REPLY_DELAY if i == 0 else 0.05)
                try:
                    resp = dev.get_feature_report(REPORT_ID, REPORT_LEN)
                except (OSError, ValueError) as e:
                    self._diag.append(f"    get: {e}")
                    continue
                res = parse_report(resp)
                if res is not None:
                    self._diag.append(f"    reply: {hexdump(resp, 14)}")
                    return res
            self._diag.append("    no 0x15 reply (mouse asleep, off, or another tool holds it)")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _poll_v2(self) -> List[DeviceStatus]:
        try:
            infos = hidlist.enumerate(NINJUTSO_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(ninjutso): %s", e)
            return []

        groups: Dict[int, List[dict]] = {}
        for d in infos:
            if d.get("usage_page") != USAGE_PAGE:
                continue
            pid = d["product_id"]
            if pid in PIDS or looks_like_sora(d):
                groups.setdefault(pid, []).append(d)
        if not groups:
            return []

        reading: Optional[Reading] = None
        # the cable first: it charges, and the dongle has nothing to add then
        for pid in sorted(groups, key=lambda p: PIDS.get(p) != "cable"):
            conn = PIDS.get(pid, "dongle")
            entries = groups[pid]
            label = (entries[0].get("product_string") or "").strip()
            self._diag.append(f"[Ninjutso] pid={pid:04x} ({conn}) '{label or NAME}'")
            if pid not in PIDS:
                self._diag.append("  not a known Sora V2 id; the strings say Ninjutso, "
                                  "trying the Sora V2 exchange")
            got = None
            for d in entries:
                self._diag.append(f"  iface={d.get('interface_number')} usage="
                                  f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
                got = self._read(d["path"])
                if got is not None:
                    break
            if got is not None:
                reading = got
                break

        if reading is not None:
            level, charging = reading
            self._last = (level, charging, time.time())
            return [DeviceStatus("ninjutso", NAME, level, charging, True, "ninjutso",
                                 kind="mouse")]
        if self._last and time.time() - self._last[2] < ASLEEP_KEEP:
            level, charging, _ = self._last
            self._diag.append("  asleep: keeping the last level greyed until it answers"
                              " (or new input wakes it)")
            return [DeviceStatus("ninjutso", NAME, level, charging, False, "ninjutso",
                                 kind="mouse", wake_on_input=True)]
        return []

    # --- Sora V3 (PixArt) ---
    def _v3_transact(self, dev, cmd: int) -> Optional[int]:
        """One web-driver command: send report 6, read the reply that echoes cmd."""
        payload = [cmd, 0x00, 0x00, 0x01] + [0x00] * (V3_PAYLOAD - 4)
        buf = [V3_REPORT_ID] + payload
        delay = V3_DELAY
        for _ in range(V3_TRIES):
            try:
                dev.send_feature_report(buf)
            except (OSError, ValueError) as e:
                self._diag.append(f"    send({cmd}): {e}")
                return None
            time.sleep(delay)
            delay = V3_DELAY_LATER
            try:
                resp = dev.get_feature_report(V3_REPORT_ID, V3_READ_LEN)
            except (OSError, ValueError) as e:
                self._diag.append(f"    get({cmd}): {e}")
                continue
            value = parse_v3(resp, cmd)
            if value is not None:
                self._diag.append(f"    cmd {cmd}: {hexdump(resp, 12)}")
                return value
        return None

    def _read_v3(self, path) -> Optional[V3Reading]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            online = self._v3_transact(dev, V3_CMD_ONLINE)
            if online is None:
                return None
            if online == 0:
                # asleep: the receiver still answers, but withholds the level
                # (cmd 18 and 17 come back as 06 00 ...), so do not ask for it.
                self._diag.append("    asleep (online=0)")
                return 0, False, False
            level = self._v3_transact(dev, V3_CMD_BATTERY)
            if level is None:
                return None
            charging = self._v3_transact(dev, V3_CMD_CHARGING) or 0
            return level, bool(charging), bool(online)
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _poll_v3(self) -> List[DeviceStatus]:
        try:
            infos = hidlist.enumerate(PIXART_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(ninjutso v3): %s", e)
            return []

        groups: Dict[int, List[dict]] = {}
        for d in infos:
            if d.get("usage_page") not in PIXART_PAGES:
                continue
            pid = d["product_id"]
            if pid in PIXART_PIDS or looks_like_sora(d):
                groups.setdefault(pid, []).append(d)
        if not groups:
            return []

        reading: Optional[V3Reading] = None
        # the cable first: it charges, and the receiver has nothing to add then
        for pid in sorted(groups, key=lambda p: PIXART_PIDS.get(p) != "cable"):
            entries = groups[pid]
            conn = PIXART_PIDS.get(pid, "receiver")
            label = (entries[0].get("product_string") or "").strip()
            self._diag.append(f"[Ninjutso V3] pid={pid:04x} ({conn}) '{label or V3_NAME}'")
            if pid not in PIXART_PIDS:
                self._diag.append("  not a known receiver id; the strings say Ninjutso, "
                                  "trying the Sora V3 commands")
            got = None
            for d in entries:
                self._diag.append(f"  iface={d.get('interface_number')} usage="
                                  f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
                got = self._read_v3(d["path"])
                if got is not None:
                    break
            if got is not None:
                reading = got
                break

        if reading is not None:
            level, charging, online = reading
            if online and 0 < level <= 100:
                self._last_v3 = (level, charging, time.time())
                return [DeviceStatus("ninjutso-v3", V3_NAME, level, charging, True,
                                     "ninjutso", kind="mouse")]
            self._diag.append("  mouse not awake (online=0) or a level out of range: "
                              "keeping the last level greyed")
        if self._last_v3 and time.time() - self._last_v3[2] < ASLEEP_KEEP:
            level, charging, _ = self._last_v3
            return [DeviceStatus("ninjutso-v3", V3_NAME, level, charging, False,
                                 "ninjutso", kind="mouse", wake_on_input=True)]
        return []

    # --- provider ---
    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        return self._poll_v2() + self._poll_v3()

    def diagnostics(self) -> List[str]:
        return list(self._diag)
