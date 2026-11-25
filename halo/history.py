"""Battery history: how fast each device drains, and roughly how much use it has left.

Every poll feeds each device's reading in here. Time only counts while the device is
awake and on battery: a mouse that is asleep, a headset that is switched off or a PC
that is suspended adds nothing, so the estimate is "hours of use left", not hours on
the clock. A long gap between two readings (the PC slept, the app was closed) counts
as MAX_GAP at most.

The history starts over when the device charges: seen charging, or its level jumps up
by RESET_RISE or more (charged while the app was not running, or a fresh battery). The
drain rate is a least-squares fit of the level against the time used since then, and
no estimate is given until the device has been used for MIN_SPAN and dropped MIN_DROP
percentage points, so the first reading after a charge does not produce a wild guess.

The history is kept in %APPDATA%\\HaloBattery\\history.json, so an estimate survives a
restart of the app or of Windows.
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

MAX_GAP = 600.0          # s: the most one gap between two readings may add
MIN_SPAN = 3600.0        # s of use since the charge before an estimate is given
MIN_DROP = 5             # percentage points dropped since the charge, likewise
RESET_RISE = 3           # a rise this large without "charging" means it was charged
MAX_SAMPLES = 400        # per device; the oldest are dropped
MAX_POINTS = 288         # per device: 24 h at one point every 5 minutes
POINT_EVERY = 300.0      # s between feature points
FORGET_AFTER = 60 * 86400.0   # a device not seen for this long is forgotten
SAVE_EVERY = 300.0       # s between writes of history.json


def format_left(seconds: float) -> str:
    """12600 -> 'about 4 h of use left'.

    Zero (or a negative from a bad reading) means the battery is empty: saying
    "less than 1 h of use left" for 0% would be wrong.
    """
    if seconds <= 0:
        return "no use left"
    hours = seconds / 3600.0
    if hours < 1:
        return "less than 1 h of use left"
    if hours < 48:
        return f"about {round(hours)} h of use left"
    return f"about {round(hours / 24)} days of use left"


class History:
    def __init__(self, path: Optional[str] = None):
        self.path = path
        # key -> {"use": s used since the charge, "last": wall time of the last counted
        #         reading or None, "seen": wall time, "samples": [[use, level], ...]}
        self.devices: Dict[str, dict] = {}
        self.dirty = False
        self._saved = 0.0

    # ---------------- storage
    def load(self) -> None:
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        now = time.time()
        devices = data.get("devices") if isinstance(data, dict) else None
        for key, d in (devices.items() if isinstance(devices, dict) else ()):
            try:
                samples = [[float(u), int(l)] for u, l in d.get("samples", [])][-MAX_SAMPLES:]
                points = []
                for p in d.get("points", []) if isinstance(d.get("points"), list) else []:
                    if not isinstance(p, list) or len(p) < 5:
                        continue
                    points.append([float(p[0]), int(p[1]), int(p[2]), int(p[3]),
                                   None if p[4] is None else float(p[4])])
                points = points[-MAX_POINTS:]
                entry = {"use": float(d.get("use", 0.0)), "last": None,
                         "seen": float(d.get("seen", now)), "samples": samples,
                         "points": points, "model": str(d.get("model") or ""),
                         "source": str(d.get("source") or ""),
                         "transport": str(d.get("transport") or "")}
            except (AttributeError, OverflowError, TypeError, ValueError):
                continue                      # a damaged entry: start that device over
            if now - entry["seen"] < FORGET_AFTER:
                self.devices[str(key)] = entry

    def save(self, force: bool = False) -> None:
        """Write history.json when something changed, at most every SAVE_EVERY
        seconds unless forced (on exit)."""
        if not self.path or not self.dirty:
            return
        now = time.time()
        if not force and now - self._saved < SAVE_EVERY:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"devices": self.devices}, f, separators=(",", ":"))
            os.replace(tmp, self.path)
            self.dirty = False
            self._saved = now
        except OSError:
            pass

    # ---------------- readings
    def record(self, key: str, level: Optional[int], charging: bool, online: bool,
               now: float, coarse: bool = False, *, model: str = "", source: str = "",
               transport: str = "", idle: Optional[float] = None) -> None:
        """One poll's reading of one device. `coarse`: the device only reports rough
        steps (XInput, Switch controllers), too rough to measure a rate from.

        The optional metadata and `idle` populate the feature points used by a
        future model; the rate estimate itself still comes from `samples`.
        """
        if coarse or level is None:
            return
        d = self.devices.get(key)
        if d is None:
            d = self.devices[key] = {"use": 0.0, "last": None, "seen": now,
                                     "samples": [], "points": [],
                                     "model": model, "source": source, "transport": transport}
        for name, value in (("model", model), ("source", source), ("transport", transport)):
            if value and d.get(name) != value:
                d[name] = value
                self.dirty = True
        d["seen"] = now
        points = d.setdefault("points", [])
        if not points or now - points[-1][0] >= POINT_EVERY:
            points.append([float(now), int(level), int(bool(charging)),
                           int(bool(online)), None if idle is None else round(float(idle), 1)])
            del points[:-MAX_POINTS]
            self.dirty = True
        if charging:
            if d["samples"] or d["use"]:
                self._reset(d)
            d["last"] = None
            return
        if not online:
            d["last"] = None                  # asleep: the time until it wakes does not count
            return
        if d["last"] is not None:
            d["use"] += min(max(now - d["last"], 0.0), MAX_GAP)
            # the use time is part of the estimate (a level that sits still slows the
            # rate down), so a restart must keep it too, not only the samples
            self.dirty = True
        d["last"] = now
        samples = d["samples"]
        if samples and level >= samples[-1][1] + RESET_RISE:
            self._reset(d)                    # charged somewhere the app did not see
            samples = d["samples"]
        if not samples or level < samples[-1][1]:
            samples.append([d["use"], level])  # a small rise is jitter: not a new sample
            del samples[:-MAX_SAMPLES]
            self.dirty = True

    def _reset(self, d: dict) -> None:
        d["use"] = 0.0
        d["samples"] = []
        self.dirty = True

    # ---------------- estimate
    def _points(self, key: str):
        d = self.devices.get(key)
        if d is None or len(d["samples"]) < 2:
            return None
        pts = list(d["samples"])
        # the time since the last drop counts too: a level that sits still for hours
        # slows the rate down instead of keeping the rate of the last drop
        last_level = pts[-1][1]
        if d["use"] > pts[-1][0]:
            pts.append([d["use"], last_level])
        return pts

    def rate(self, key: str) -> Optional[float]:
        """Percentage points used per second of use, or None when not known yet."""
        pts = self._points(key)
        if pts is None:
            return None
        span = pts[-1][0] - pts[0][0]
        if span < MIN_SPAN or pts[0][1] - pts[-1][1] < MIN_DROP:
            return None
        n = len(pts)
        mean_t = sum(p[0] for p in pts) / n
        mean_l = sum(p[1] for p in pts) / n
        var = sum((p[0] - mean_t) ** 2 for p in pts)
        if var <= 0:
            return None
        slope = sum((p[0] - mean_t) * (p[1] - mean_l) for p in pts) / var
        return -slope if slope < 0 else None

    def rate_bounds(self, key: str):
        """(low, high) percentage-points/second from the sample intervals.

        This is a deliberately simple robust range: the 25th and 75th
        percentiles of the observed per-interval drain rates. It is used only
        for the tooltip range; `rate()` stays the central regression.
        """
        pts = self._points(key)
        if pts is None:
            return None
        span = pts[-1][0] - pts[0][0]
        if span < MIN_SPAN or pts[0][1] - pts[-1][1] < MIN_DROP:
            return None
        rates = []
        for (t0, l0), (t1, l1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt <= 0:
                continue
            if l0 > l1:
                rates.append((l0 - l1) / dt)
        if len(rates) < 2:
            return None
        rates.sort()
        lo = rates[max(0, int(len(rates) * 0.25) - 1)]
        hi = rates[min(len(rates) - 1, int(len(rates) * 0.75))]
        # If the level has been sitting still since the last drop for longer than the
        # slow bound would allow, cap the slow bound at 1 % / tail_dt so the range
        # reflects the observed hold just like rate() does, and always bracket rate().
        if len(pts) >= 2 and pts[-1][1] == pts[-2][1]:
            tail_dt = pts[-1][0] - pts[-2][0]
            if tail_dt > 0:
                lo = min(lo, 1.0 / tail_dt)
        central = self.rate(key)
        if central is not None and central > 0:
            lo = min(lo, central)
            hi = max(hi, central)
        if lo <= 0 or hi <= 0:
            return None
        return lo, hi

    def seconds_left(self, key: str, level: Optional[int]) -> Optional[float]:
        r = self.rate(key)
        if r is None or level is None:
            return None
        return level / r

    def seconds_left_range(self, key: str, level: Optional[int]):
        """(low, high) seconds left from the robust interval range, or None."""
        bounds = self.rate_bounds(key)
        if bounds is None or level is None:
            return None
        lo_rate, hi_rate = bounds
        return level / hi_rate, level / lo_rate

    def report(self) -> List[str]:
        """Lines for the diagnostics report."""
        lines = []
        for key, d in sorted(self.devices.items()):
            r = self.rate(key)
            s = d["samples"]
            est = (f"{r * 3600:.2f} %/h of use" if r is not None else "no estimate yet")
            lines.append(f"[{key}] {len(s)} sample(s), {d['use'] / 3600:.1f} h of use since "
                         f"the last charge, {est}")
        return lines or ["(no history yet)"]
