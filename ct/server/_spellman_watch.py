"""backend: the Spellman 60 kV supply's arcs and over-current, from its
read-only stream. Pure logic -- no I/O, no hardware; the HV guard acts on
what it returns.

control2's spellman_backend broadcasts GetStatus / GetkVMonitor /
GetmAMonitor about 4 times a second (measured 2026-10-29: 0.22 s median).
Nothing is ever sent to the supply: other programs drive it.

What it looks like (2026-10-29 11:01-11:12, 60-65 kV, 22 arcs in 11 min):
  ordinary arc   ONE reading with the kV at 2-40 kV and/or the mA at
                 0.5-2.4 mA, the next reading back at the set voltage. The
                 supply recovers by itself, so does this: it is recorded,
                 nothing is switched.
  big arc        the kV falls in one step and does NOT come back within
                 BIG_ARC_S: HV guard trip, latched, a person clears it (the
                 user, 2026-10-29).
  over-current   the mA over mean + k sigma of its quiet readings for more than HOLD_S
                 while nothing is pulsing: something is emitting that should
                 not be (a grid switch on, a hot filament) -- HV guard trip.
A ramp-down is not an arc: an arc opens only on a drop of more than 10 %
AND ARC_KV_MIN_DROP kV between two consecutive readings.

The mA baseline is learned all the time from quiet readings (no pulse, no
arc) over BASE_WINDOW_S, never above BASE_CAP_MA: a fault present from the
start may not be learned as normal.
"""
from __future__ import annotations

import collections
import statistics
import threading

ARC_KV_FRAC = 0.90       # a reading under this fraction of the one before = an arc starts ...
ARC_KV_MIN_DROP = 3.0    # ... and at least this many kV under it (a ramp near 0 kV
                         # falls 10 % per step; the 10-29 arcs fell 20-63 kV)
ARC_BACK_FRAC = 0.95     # back to this fraction of the kV before it = recovered
BIG_ARC_KV_FRAC = 0.50   # still under this fraction after BIG_ARC_S = big arc
BIG_ARC_S = 5.0          # judged on a kV reading taken at least this long after the drop --
                         # never on a stale one: spellman_backend's monitor pauses while
                         # its conditioning handles an arc (2026-10-29 11:36, a false trip)
NO_READING_S = 30.0      # no kV reading this long after the drop: closed as unknown, no trip
ARC_MA = 1.0             # one quiet reading this far over the baseline = an arc (kV dip missed)
KV_ON = 5.0              # under this the supply is off: no arcs to see
HOLD_S = 1.0             # over-current: over the threshold this long
GAP_S = 2.0              # readings further apart than this are not "continuous"
BASE_WINDOW_S = 60.0
BASE_CAP_MA = 0.2        # the learned baseline is never taken above this
# Over-current threshold = mean + k * sigma of the quiet readings over
# BASE_WINDOW_S. Measured 2026-10-29 at 60-65 kV: mean 0.034-0.037 mA, sigma
# 0.036-0.043 mA in every 60 s window (readings in 0.0098 mA steps, skewed:
# median 0.02, p99 0.13 -- 3 sigma is about the 1 % point, hence k = 5).
TRIP_SIGMA_DEFAULT = 5.0
TRIP_SIGMA_RANGE = (2.0, 20.0)
SIGMA_FLOOR_MA = 0.02    # ~2 steps: all-zero readings (low kV) must not make sigma 0
SIGMA_CAP_MA = 0.1       # a noisy fault may not widen the threshold past this
MIN_BASE_N = 20          # fewer quiet readings: the floor sigma, baseline 0
ARCS_KEPT = 500

# GetStatus: 17 bits, in the order control2's spellman.html labels them.
STATUS_NAMES = ("power_on", "hv_on", "arc", "interlock_closed", "over_current", "over_power",
                "over_voltage", "voltage_control", "system_fault", "regulation_error",
                "current_control", "over_temperature", "power_control", "ac_fault",
                "remote_mode", "lvps_fault", "hv_inhibit")
# The ones that mean something went wrong (recorded on every change).
ALARMS = ("arc", "over_current", "over_power", "over_voltage", "system_fault",
          "regulation_error", "over_temperature", "ac_fault", "lvps_fault", "hv_inhibit")
STATUS_FRESH_S = 3.0     # a GetStatus older than this says nothing about now
STABLE_S = 10.0          # "stable again": this long with no arc, no alarm, HV on, kV steady
STABLE_KV_SPREAD = 0.02  # ... kV within this fraction over that time


def status_flags(bits, names=ALARMS) -> list[str]:
    """Names of the bits set in a GetStatus list (alarms only by default)."""
    if not isinstance(bits, (list, tuple)):
        return []
    return [n for i, n in enumerate(STATUS_NAMES) if n in names and i < len(bits) and bits[i]]


def status_bit(bits, name):
    """True / False, or None when the list is too short to say."""
    i = STATUS_NAMES.index(name)
    return bool(bits[i]) if isinstance(bits, (list, tuple)) and i < len(bits) else None


class SpellmanWatch:
    """Feed it readings in arrival order (monotonic seconds); each feed
    returns the events it closed: dicts with "kind" "arc" | "big_arc" |
    "over_current". Thread-safe."""

    def __init__(self, trip_sigma: float = TRIP_SIGMA_DEFAULT):
        self.trip_sigma = float(trip_sigma)
        self._lock = threading.Lock()
        self._base = collections.deque()          # (t, mA) quiet, no arc
        self._arcs = collections.deque(maxlen=ARCS_KEPT)
        self._arc = None                          # the open arc, or None
        self._kv = None                           # (t, kV) last reading
        self._ma = None                           # (t, mA) last reading
        self._over_since = None
        self._over_fired = False
        self._last_ma_t = None
        self._status_flags = []
        self._hv_on = None                        # (t, bool) the supply's HV Ray On bit
        self._kv_hist = collections.deque()       # (t, kV), the last STABLE_S * 2
        self._last_bad_status_t = None            # last GetStatus with an alarm or HV off

    # ── readings ────────────────────────────────────────────────────────────
    def feed_kv(self, t: float, kv: float) -> list[dict]:
        with self._lock:
            prev = self._kv
            self._kv = (t, kv)
            self._kv_hist.append((t, kv))
            while self._kv_hist and t - self._kv_hist[0][0] > 2 * STABLE_S:
                self._kv_hist.popleft()
            out = self._advance(t)
            if self._arc is not None:
                a = self._arc
                a["min_kv"] = min(a["min_kv"], kv)
                if kv >= ARC_BACK_FRAC * a["kv_before"]:
                    out.append(self._close(t, "arc"))
                return out
            if prev is not None and prev[1] >= KV_ON and kv < ARC_KV_FRAC * prev[1] \
                    and prev[1] - kv >= ARC_KV_MIN_DROP and t - prev[0] <= GAP_S:
                self._open(t, "kv", kv_before=prev[1], min_kv=kv)
            if kv < KV_ON:
                self._base.clear()                # off: a new baseline when it is back
            return out

    def feed_ma(self, t: float, ma: float, quiet: bool) -> list[dict]:
        with self._lock:
            gap = self._last_ma_t is not None and t - self._last_ma_t > GAP_S
            self._last_ma_t = t
            self._ma = (t, ma)                    # first: it is what closes an mA spike
            out = self._advance(t)
            base, sigma = self._stats(t)
            thr = base + self.trip_sigma * sigma
            if self._arc is not None:
                self._arc["peak_ma"] = max(self._arc["peak_ma"], ma)
                self._over_since, self._over_fired = None, False   # an arc's mA is the arc's
                return out
            if not quiet or gap:
                self._over_since, self._over_fired = None, False   # a pulse is not over-current
                if not quiet:
                    return out
            if ma - base >= ARC_MA and self._over_since is None and self._kv is not None \
                    and self._kv[1] >= KV_ON:
                # One spike: an arc whose kV dip fell between readings. If it
                # stays up it is over-current instead (the next reading
                # closes the arc only once the mA is back).
                self._open(t, "ma", kv_before=self._kv[1], min_kv=self._kv[1], peak_ma=ma)
                return out
            if ma > thr:
                if self._over_since is None:
                    self._over_since = t
                if t - self._over_since >= HOLD_S and not self._over_fired:
                    self._over_fired = True
                    out.append({"kind": "over_current", "t": t, "ma": round(ma, 4),
                                "baseline_ma": round(base, 4), "sigma_ma": round(sigma, 4),
                                "k": self.trip_sigma, "threshold_ma": round(thr, 4),
                                "over_s": round(t - self._over_since, 2),
                                "kv": None if self._kv is None else round(self._kv[1], 2)})
                return out
            self._over_since, self._over_fired = None, False
            self._base.append((t, ma))
            while self._base and t - self._base[0][0] > BASE_WINDOW_S:
                self._base.popleft()
            return out

    def feed_status(self, t: float, bits) -> list[dict]:
        flags = status_flags(bits)
        with self._lock:
            out = self._advance(t)
            was = self._status_flags
            rose = "arc" in flags and "arc" not in was    # an edge, not a level
            self._status_flags = flags
            hv = status_bit(bits, "hv_on")
            hv_was = None if self._hv_on is None else self._hv_on[1]
            if hv is not None:
                self._hv_on = (t, hv)
            if flags or hv is False:
                self._last_bad_status_t = t
            kv = None if self._kv is None else round(self._kv[1], 2)
            if flags != was or (hv is not None and hv_was is not None and hv != hv_was):
                out.append({"kind": "status", "t": t, "set": [f for f in flags if f not in was],
                            "cleared": [f for f in was if f not in flags], "alarms": flags,
                            "hv_on": hv, "kv": kv})
            if "system_fault" in flags and "system_fault" not in was:
                # The supply says it faulted: act now, whatever the kV says.
                out.append({"kind": "system_fault", "t": t, "alarms": flags, "hv_on": hv, "kv": kv,
                            "kv_before": None if self._arc is None else round(self._arc["kv_before"], 2)})
            if self._arc is not None:
                for f in flags:
                    if f not in self._arc["status"]:
                        self._arc["status"].append(f)
            elif rose and self._kv is not None and self._kv[1] >= KV_ON:
                self._open(t, "status", kv_before=self._kv[1], min_kv=self._kv[1])
                self._arc["status"] = flags
            return out

    def feed_arc_flag(self, t: float) -> list[dict]:
        """spellman_backend's conditioning says "arc" (its own status read):
        an arc, nothing more -- not a GetStatus, so no HV On / alarm change."""
        with self._lock:
            out = self._advance(t)
            if self._arc is None and self._kv is not None and self._kv[1] >= KV_ON:
                self._open(t, "status", kv_before=self._kv[1], min_kv=self._kv[1])
                self._arc["status"] = ["arc"]
            return out

    def poll(self, now: float) -> list[dict]:
        """Close what time alone closes (a big arc with the readings stopped)."""
        with self._lock:
            return self._advance(now)

    # ── internals (lock held) ───────────────────────────────────────────────
    def _open(self, t, via, kv_before, min_kv, peak_ma=None):
        self._arc = {"t0": t, "via": via, "kv_before": kv_before, "min_kv": min_kv,
                     "peak_ma": peak_ma if peak_ma is not None else (self._ma[1] if self._ma else 0.0),
                     "status": []}

    def _close(self, t, kind, unknown=None):
        a, self._arc = self._arc, None
        ev = {"kind": kind, "t": a["t0"], "via": a["via"], "kv_before": round(a["kv_before"], 2),
              "min_kv": round(a["min_kv"], 2), "peak_ma": round(a["peak_ma"], 4),
              "status": a["status"]}
        if unknown:
            ev["unknown"] = unknown
        if kind == "arc":
            ev["recovered_s"] = round(t - a["t0"], 2)
            kv_now = self._kv[1] if self._kv else None
            if kv_now is not None and kv_now < ARC_BACK_FRAC * a["kv_before"]:
                ev["settled_kv"] = round(kv_now, 2)
        else:
            ev["down_s"] = round(t - a["t0"], 2)
            ev["kv_now"] = None if self._kv is None else round(self._kv[1], 2)
        self._arcs.append(ev)
        return ev

    def _advance(self, t):
        a = self._arc
        if a is None:
            return []
        if a["via"] == "ma" and self._ma is not None and self._ma[0] > a["t0"]:
            # The reading after the spike: back under -> it was an arc.
            if self._ma[1] - self._baseline(t) < ARC_MA:
                return [self._close(t, "arc")]
            # Still up: not a spike. Hand it to the over-current check.
            self._arc = None
            self._over_since = a["t0"]
            return []
        if a["via"] == "status" and self._kv is not None and self._kv[0] > a["t0"] \
                and self._kv[1] >= ARC_BACK_FRAC * a["kv_before"]:
            return [self._close(t, "arc")]
        if self._kv is None or self._kv[0] - a["t0"] < BIG_ARC_S:
            if t - a["t0"] >= NO_READING_S:      # the readings stopped: cannot tell
                return [self._close(t, "arc", unknown="no kV reading after the drop")]
            return []
        if self._kv[1] < BIG_ARC_KV_FRAC * a["kv_before"]:
            # Down and staying down. The supply's own HV Ray On bit, fresh,
            # off and no fault: someone switched it off -- not an arc.
            if (self._hv_on is not None and self._hv_on[1] is False and t - self._hv_on[0] <= STATUS_FRESH_S
                    and "system_fault" not in a["status"] and "arc" not in a["status"]):
                return [self._close(t, "hv_off")]
            return [self._close(t, "big_arc")]
        return [self._close(t, "arc")]            # came part way back and stayed: recorded, settled_kv

    def _stats(self, t):
        """(baseline mean, sigma) of the quiet readings, both bounded."""
        vals = [m for tt, m in self._base if t - tt <= BASE_WINDOW_S]
        if len(vals) < MIN_BASE_N:
            return (min(statistics.mean(vals), BASE_CAP_MA) if vals else 0.0), SIGMA_FLOOR_MA
        return (min(statistics.mean(vals), BASE_CAP_MA),
                min(max(statistics.pstdev(vals), SIGMA_FLOOR_MA), SIGMA_CAP_MA))

    def _baseline(self, t):
        return self._stats(t)[0]

    # ── queries ─────────────────────────────────────────────────────────────
    def arc_near(self, t_from: float, t_to: float):
        """The first arc that started in [t_from, t_to] (open or closed), or None."""
        with self._lock:
            if self._arc is not None and t_from <= self._arc["t0"] <= t_to:
                return dict(self._arc, kind="arc (open)")
            for ev in self._arcs:
                if t_from <= ev["t"] <= t_to:
                    return dict(ev)
        return None

    def stable(self, now: float, hold_s: float = STABLE_S):
        """None if the supply has been stable for hold_s (no arc open or within
        hold_s, no alarm bit, HV Ray On, kV readings covering the whole time
        within STABLE_KV_SPREAD), else why not (a short text)."""
        with self._lock:
            if self._arc is not None:
                return "an arc is still open"
            if self._status_flags:
                return "alarm bit(s) set: " + ", ".join(self._status_flags)
            if self._hv_on is None or now - self._hv_on[0] > STATUS_FRESH_S:
                return "no fresh GetStatus"
            if not self._hv_on[1]:
                return "the Spellman HV is off"
            if self._last_bad_status_t is not None and now - self._last_bad_status_t < hold_s:
                return f"alarm / HV off {now - self._last_bad_status_t:.1f} s ago"
            if self._arcs and now - self._arcs[-1]["t"] < hold_s:
                return f"an arc {now - self._arcs[-1]['t']:.1f} s ago"
            kvs = [(t, kv) for t, kv in self._kv_hist if now - t <= hold_s]
            if not kvs or kvs[0][0] > now - hold_s + 1.0 or now - kvs[-1][0] > GAP_S:
                return f"kV readings do not cover the last {hold_s:g} s"
            lo, hi = min(kv for _, kv in kvs), max(kv for _, kv in kvs)
            if lo < KV_ON:
                return f"kV {lo:.1f} (supply not up)"
            if hi - lo > STABLE_KV_SPREAD * hi:
                return f"kV not steady ({lo:.1f}-{hi:.1f})"
            return None

    def reset_over(self):
        with self._lock:
            self._over_since, self._over_fired = None, False

    def snapshot(self, now: float) -> dict:
        with self._lock:
            arcs = [ev for ev in self._arcs if now - ev["t"] <= 600.0]
            base, sigma = self._stats(now)
            return {"baseline_ma": round(base, 4), "sigma_ma": round(sigma, 4), "k": self.trip_sigma,
                    "threshold_ma": round(base + self.trip_sigma * sigma, 4),
                    "hold_s": HOLD_S, "over_s": None if self._over_since is None
                    else round(now - self._over_since, 2),
                    "arc_open": self._arc is not None, "alarms": list(self._status_flags),
                    "hv_on": None if self._hv_on is None else self._hv_on[1],
                    "arcs_10min": sum(1 for ev in arcs if ev["kind"] == "arc"),
                    "big_arcs_10min": sum(1 for ev in arcs if ev["kind"] == "big_arc"),
                    "last_arc": dict(self._arcs[-1], age_s=round(now - self._arcs[-1]["t"], 1))
                    if self._arcs else None}
