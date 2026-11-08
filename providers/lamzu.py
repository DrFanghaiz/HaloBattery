"""LAMZU Maya X on its 8K dongle (373E:001E) or on the cable (373E:001C).

Source: Sheroune/lamzu-battery-monitory (MIT), and its Linux port in
Quitsoon/gnome-shell-extension-lamzu-battery (MIT); anany1702/lamzu-ctl (MIT) reads
it the same way ("Aurora", the newer revision). The exchange is the one the WLmouse
and G-Wolves providers use - the same firmware family - on the vendor collection
(usage page 0xFFFF) of interface 2:

    request   feature report 0, 64 bytes: 00 00 02 02 00 83 00 ...
    reply     feature report 0:           a1 00 02 02 00 83 <charging> <battery %>

Nothing else is ever written to the mouse. The request and the reply parser are
the ones in wlmouse.py. The dongle cannot tell a sleeping mouse from one that is
switched off, so a silent mouse keeps its last level, greyed out, for a while.

Only the Maya X ids are listed for that exchange: they are the ones the sources and a
diagnostics report from a real Maya X 8K dongle agree on. Other Lamzu mice use other
vendor ids (0x3554, 0x37B0) and, according to lamzu-ctl, other protocols.

The Atlantis Mini Champion Edition is one of those: it is Compx hardware (its receiver
and its cable both report manufacturer "compx" under vendor id 0x3554), so it speaks
the 17-byte Compx frame of providers/pulsar.py instead, on the 0xFF02:0x0002
collection of interface 1:

    request   17 bytes, output report on that collection: 08 04 00 ... 00 <checksum>
    reply     read back on the same handle:               08 04 00 00 00 02 <level>
                                                          <charging> <mV hi> <mV lo> ...

Both transports answer it - the 1K "Champion" receiver (3554:F50D) and the mouse on its
cable (3554:F50F) - and byte 7 is 1 while the mouse charges, which the Maya X exchange
cannot show without its cable. Verified on this machine: receiver 95 % / 4076 mV / not
charging, cable 95 % / 4205 mV / charging. The frame, the checksum and the reply
parser are imported from pulsar.py so the two files cannot drift apart.
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
from .pulsar import (FLUSH_TIMEOUT_MS, PAYLOAD_LEN, READ_ATTEMPTS, READ_TIMEOUT_MS,
                     make_request, output_length, parse_power, voltage_mv)
from .wlmouse import QUERY, parse_feature

LAMZU_VID = 0x373E
DONGLE = 0x001E
WIRED = 0x001C
PIDS = {DONGLE: "dongle", WIRED: "cable"}
NAME = "LAMZU Maya X"

# The Atlantis Mini Champion Edition is Compx hardware under the same vendor id the
# Pulsar/ATK/VXE mice use, so both of its ids are claimed here.
COMPX_VID = 0x3554
ATLANTIS_WIRED = 0xF50F          # "LAMZU Atlantis MINI Champion" on its cable
ATLANTIS_RECEIVER = 0xF50D       # "LAMZU 1K Champion Receiver"
ATLANTIS_PIDS = {ATLANTIS_WIRED: "cable", ATLANTIS_RECEIVER: "receiver"}
ATLANTIS_KEY = "lamzu-atlantis"
ATLANTIS_NAME = "LAMZU Atlantis Mini"
ATLANTIS_CONTROL = (0xFF02, 0x0002)   # the collection the 17-byte frame goes to

INTERFACE = 2                    # the vendor collection is on interface 2 ...
USAGE_PAGE = 0xFFFF              # ... with usage page 0xFFFF

REPLY_TRIES = 10                 # get the reply up to 10 times, 50 ms apart
ASLEEP_KEEP = 300                # s, as in the WLmouse and G-Wolves providers

Reading = Tuple[int, bool]


def vendor_collection(ifaces: List[dict]) -> Optional[dict]:
    """The one collection the request may go to: interface 2, usage page 0xFFFF."""
    for d in ifaces:
        if d.get("interface_number") == INTERFACE and d.get("usage_page") == USAGE_PAGE:
            return d
    return None


def compx_collection(ifaces: List[dict]) -> Optional[dict]:
    """The collection the 17-byte Compx frame may go to.

    0xFF02:0x0002 is the one the Atlantis answers on, so it is tried first. Other
    vendor collections of the same device are only used when Windows does not know
    their output length or it fits the frame: a collection that cannot carry 17
    bytes refuses the write, which looks exactly like a mouse that is off."""
    vendor = [d for d in ifaces if (d.get("usage_page") or 0) >= 0xFF00]
    vendor.sort(key=lambda d: (d.get("usage_page"), d.get("usage")) != ATLANTIS_CONTROL)
    for d in vendor:
        if output_length(d["path"]) in (None, PAYLOAD_LEN):
            return d
    return None


class LamzuProvider(Provider):
    name = "lamzu"

    def __init__(self):
        self._diag: List[str] = []
        self._last: Optional[Tuple[int, bool, float]] = None
        self._atlantis: Optional[Tuple[int, bool, float]] = None

    def _read(self, path: bytes) -> Optional[Reading]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            try:
                dev.send_feature_report([0x00] + QUERY + [0x00] * (64 - len(QUERY)))
            except (OSError, ValueError) as e:
                self._diag.append(f"    send: {e}")
                return None
            for _ in range(REPLY_TRIES):
                time.sleep(0.05)
                try:
                    resp = dev.get_feature_report(0, 65)
                except (OSError, ValueError):
                    resp = None
                batt, chg = parse_feature(resp)
                if batt is not None:
                    self._diag.append(f"    reply: {hexdump(resp, 12)}")
                    return batt, bool(chg)
            self._diag.append("    no a1 reply to request 0x83 (mouse asleep or off)")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def poll(self) -> List[DeviceStatus]:
        self._diag = []
        if hid is None:
            return []
        return self._poll_maya() + self._poll_atlantis()

    def _poll_maya(self) -> List[DeviceStatus]:
        try:
            infos = hidlist.enumerate(LAMZU_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(lamzu): %s", e)
            return []
        groups: Dict[int, List[dict]] = {}
        for d in infos:
            if d["product_id"] in PIDS:
                groups.setdefault(d["product_id"], []).append(d)
        if not groups:
            return []

        readings: List[Reading] = []
        # the cable first: it charges, and the dongle has nothing to add then
        for pid in sorted(groups, key=lambda p: p != WIRED):
            self._diag.append(f"[Lamzu] pid={pid:04x} ({PIDS[pid]}) "
                              f"'{(groups[pid][0].get('product_string') or '').strip()}'")
            d = vendor_collection(groups[pid])
            if d is None:
                self._diag.append(f"  no interface {INTERFACE} collection with usage page "
                                  f"{USAGE_PAGE:04x}: nothing sent")
                continue
            self._diag.append(f"  iface={d.get('interface_number')} usage="
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
            res = self._read(d["path"])
            if res is not None:
                readings.append(res)
                break

        key = "lamzu"
        if readings:
            batt, chg = readings[0]
            self._last = (batt, chg, time.time())
            return [DeviceStatus(key, NAME, batt, chg, True, "lamzu", kind="mouse")]
        if self._last and time.time() - self._last[2] < ASLEEP_KEEP:
            return [DeviceStatus(key, NAME, self._last[0], self._last[1], False, "lamzu",
                                 kind="mouse")]
        return []

    def diagnostics(self) -> List[str]:
        return list(self._diag)

    # -- the Atlantis Mini: Compx 17-byte frames instead of a feature report ------

    def _query_compx(self, path: bytes) -> Optional[List[int]]:
        dev = hid.device()
        try:
            dev.open_path(path)
        except (OSError, IOError) as e:
            self._diag.append(f"    open: {e}")
            return None
        try:
            self._drain(dev)
            dev.write(make_request())
            for attempt in range(READ_ATTEMPTS):
                r = dev.read(PAYLOAD_LEN, READ_TIMEOUT_MS)
                if not r:
                    break
                self._diag.append(f"    reply {attempt + 1}: {hexdump(r)}")
                if parse_power(r) is not None:
                    return list(r)
                time.sleep(0.02)
            self._diag.append("    no power reply (events and short frames are ignored)")
            return None
        except (OSError, IOError, ValueError) as e:
            self._diag.append(f"    query error: {e}")
            return None
        finally:
            try:
                dev.close()
            except Exception:
                pass

    def _drain(self, dev) -> None:
        for _ in range(4):
            if not dev.read(PAYLOAD_LEN, FLUSH_TIMEOUT_MS):
                return

    def _poll_atlantis(self) -> List[DeviceStatus]:
        try:
            infos = hidlist.enumerate(COMPX_VID)
        except Exception as e:  # pragma: no cover
            log.warning("hid.enumerate(%04x): %s", COMPX_VID, e)
            return []
        present = False
        out: List[DeviceStatus] = []
        # the cable first: it charges, and the receiver adds nothing while it is in
        for pid in (ATLANTIS_WIRED, ATLANTIS_RECEIVER):
            mine = [d for d in infos if d.get("product_id") == pid]
            if not mine:
                continue
            present = True
            self._diag.append(f"[LAMZU Atlantis] pid={COMPX_VID:04x}:{pid:04x} "
                              f"({ATLANTIS_PIDS[pid]}) "
                              f"'{(mine[0].get('product_string') or '').strip()}'")
            d = compx_collection(mine)
            if d is None:
                self._diag.append("  no vendor collection that takes a 17-byte frame: "
                                  "nothing sent")
                continue
            self._diag.append(f"  iface={d.get('interface_number')} usage="
                              f"{d.get('usage_page', 0):04x}:{d.get('usage', 0):04x}")
            reply = self._query_compx(d["path"])
            parsed = parse_power(reply)
            if parsed is None:
                if not reply:
                    self._diag.append("    no reply (mouse asleep or switched off)")
                continue
            level, charging = parsed
            mv = voltage_mv(reply)
            if mv:
                self._diag.append(f"    {mv} mV")
            self._atlantis = (level, charging, time.time())
            out.append(DeviceStatus(ATLANTIS_KEY, ATLANTIS_NAME, level, charging, True,
                                    "lamzu", kind="mouse"))
            break
        if out:
            return out
        if not present:
            self._atlantis = None
            return []
        if self._atlantis and time.time() - self._atlantis[2] < ASLEEP_KEEP:
            level, charging, _ = self._atlantis
            return [DeviceStatus(ATLANTIS_KEY, ATLANTIS_NAME, level, charging, False,
                                 "lamzu", kind="mouse")]
        return []
