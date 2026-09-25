"""Names shared by every part of the client: Result, exceptions, PowerState,
constants and helpers -- the module-level half of the original ct_simple_control.
"""

import enum
import functools
import inspect
import json
import math
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import NewType

# Self-update from GitHub BEFORE anything else happens (see ct_update.py): a
# clone that is behind fast-forwards and this script restarts on the new code.
# Only ever at start-up -- never while a script is controlling hardware.
# CT_NO_AUTO_UPDATE=1 turns it off.
from ct import update as ct_update
from ct.paths import CLIENT_LOG_DIR
ct_update.check_and_update()

# FID: the canonical 0..95 filament id the backend and firmware agree on.
# A NewType, not a plain alias: passing a USER_INDEX where a Fid is required is
# then a type error a checker catches, instead of a wrong-but-legal int that
# only shows up as a filament that mysteriously did not fire. Zero runtime cost.
Fid = NewType("Fid", int)

import requests

# ── exceptions ────────────────────────────────────────────────────────────────
# See "ERROR HANDLING" at the top of this file. These classes exist mainly
# for CTLeaseError, the one thing this client actually raises. The others
# are kept for backward compatibility / advanced use but are not raised by
# any method here by default — every failure comes back as {"ok": False}.

#: SHV run state and stop reason, as the firmware numbers them (RP2350
#: simple_hv_schedule.h). One table for the printer and the decoder.
SHV_STATE_NAMES = {0: "idle", 1: "armed", 2: "running", 3: "complete", 4: "fault"}
SHV_STOP_REASON_NAMES = {0: "none", 1: "complete", 2: "read-back mismatch",
                         3: "inter-pulse timeout", 4: "total timeout",
                         5: "fault", 6: "disarmed"}


class Result(dict):
    """A result dict that PRINTS readably and behaves exactly like a dict.

    Every call returns one of these. Nothing about the data changes -- it is a
    dict subclass, so `r["ok"]`, `r.get(...)`, `json.dumps(r)`, `**r` and
    `isinstance(r, dict)` all work as before. Only `repr()` differs, which is
    what a REPL and a bare `print()` use.

    EVERYTHING IS SHOWN. Nothing is folded, summarised away or truncated: these
    results are what you reason about a run from, and a reader cannot know in
    advance which field turns out to matter. What changes is the SHAPE -- one
    field per line, nested structures indented, records in a list grouped one
    labelled line per group (see _ROW_LAYOUTS), a dict of small records as a
    table, a list of sentences one per line -- so a 35-field pulse event is
    scannable instead of a single 2000-character line.

    This IS the printer. There are no format_*/print_* companions: they were
    removed once every result printed readably by itself, so a reader never
    has to know which helper goes with which call. describe() is different --
    it squeezes a result into ONE sentence for a log line or an error message.

    The verdict leads, then the reason, then the rest. That ordering is the
    only editorial judgement here, and it hides nothing.
    """

    #: Shown first, in this order, when present -- the fields that answer
    #: "what happened". Everything else follows in the order the result
    #: carries it, which is usually the order the code that built it chose.
    _LEAD = ("ok", "error", "not_reached", "reason", "filament", "filaments", "state",
             "state_name", "verdict", "fired", "measured_ma", "arrival")
    _WIDTH = 78

    #: How a RECORD in a list is laid out: one labelled line per group, in
    #: order. A pulse event carries ~35 fields, and as one wrapped run of k=v
    #: pairs the answer (emission_ma) sat in the middle of raw ADC counts.
    #: Grouping changes only the layout: a field in no group goes on the final
    #: `other` line, so nothing is dropped. The first group is the record's
    #: identity and is printed on the [n] line itself, unlabelled.
    #:
    #: One layout per KIND of record; a record takes the first layout that
    #: places most of its fields (see _group_record), else no grouping.
    #: Pulse events and pulse-log rows:
    _PULSE_GROUPS = (
        ("", ("id", "filament", "controller", "seq", "tOnUs", "on_us",
              "durationUs", "empty_envelope")),
        ("emission", ("emission_ma", "emission_mams", "diode_ma")),
        ("net", ("plateau_net_ma", "peak_net_ma", "integral_mams",
                 "integral_mams_sigma", "integral_mams_unavailable")),
        ("absolute", ("plateau_ma", "peak_ma", "bg_ma", "bg_sigma_ma",
                      "post_bg_ma")),
        ("counts", ("plateau", "peak", "bg", "post_bg", "integral", "bg_sigma4",
                    "background_n", "background_gap")),
        ("checks", ("background_suspect", "background_pre_post_delta",
                    "background_partial", "background_windowing",
                    "integral_saturated", "duration_saturated",
                    "background_note")),
        ("verify", ("on_mismatch", "hv_stuck_on", "unverified", "read165",
                    "flags")),
        ("heating", ("heat_meas_mA", "heat_target_mA", "heat_meas_unavailable",
                     "heat_target_unavailable")),
    )
    #: Emission-curve points (emission_vs_heating / emission_ramp) and their
    #: per-shot rows:
    _CURVE_GROUPS = (
        ("", ("shot", "seq", "commanded_ma", "usable", "cold", "n_used",
              "n_fired", "n_cold", "empty_envelope")),
        ("emission", ("emission_ma", "emission_mams", "pedestal_ma")),
        ("net", ("net_ma", "net_ma_sd", "charge_mams", "on_us")),
        ("heating", ("heat_mA", "heat_target_mA", "settled_ma",
                     "heat_unavailable")),
        ("resistance", ("r_total_ohm", "r_fil_ohm", "r_ratio", "r_drift_frac",
                        "bus_mV", "vi_current_mA")),
        ("temperature", ("T_K", "residual", "inv_T", "ln_i_over_t2")),
        ("noise", ("bg_ma", "sigma_ma")),
    )
    _ROW_LAYOUTS = (_PULSE_GROUPS, _CURVE_GROUPS)
    _GROUP_LABEL_W = 12
    _GROUP_MIN_FRAC = 0.6
    _GROUP_MIN_FIELDS = 6       # below this one line reads fine ungrouped

    def raw(self) -> dict:
        """The plain dict. `dict(r)` does the same."""
        return dict(self)

    # ---- rendering -------------------------------------------------------
    @staticmethod
    def _scalar(v) -> str:
        if isinstance(v, float):
            return f"{v:.6g}"
        if isinstance(v, str):
            return v                      # unquoted: these are read, not eval'd
        return repr(v)

    @classmethod
    def _is_flat(cls, v) -> bool:
        return not isinstance(v, (dict, list, tuple))

    @classmethod
    def _row(cls, d: dict) -> str:
        """One record of a list-of-dicts, as aligned `k=v` pairs."""
        return "  ".join(f"{k}={cls._scalar(v)}" for k, v in d.items()
                         if not isinstance(v, (dict, list, tuple))) or "(nested)"

    @classmethod
    def _lead_first(cls, flat: dict) -> dict:
        """The same fields, the _LEAD ones (verdict, ok, error...) first."""
        head = {k: flat[k] for k in cls._LEAD if k in flat}
        return {**head, **{k: v for k, v in flat.items() if k not in head}}

    @classmethod
    def _is_table(cls, v: dict) -> bool:
        """A dict keyed by id whose every value is a small flat record -- a
        mosfet_test `results`, a per-controller readback. Printed one row per
        key, like a list of records: a field per line made a 96-filament
        result 670 lines long."""
        return (len(v) >= 2 and all(
            isinstance(x, dict) and x
            and all(not isinstance(y, (dict, list, tuple)) for y in x.values())
            for x in v.values()))

    @classmethod
    def _group_record(cls, flat: dict):
        """[(label, [k=v, ...]), ...] under the first layout that fits this
        record, or None for a record no layout describes, which then keeps the
        plain wrapped layout."""
        for layout in cls._ROW_LAYOUTS:
            g = cls._group_with(layout, flat)
            if g is not None:
                return g
        return None

    @classmethod
    def _group_with(cls, layout, flat: dict):
        placed: set = set()
        groups = []
        for label, keys in layout:
            pairs = [f"{k}={cls._scalar(flat[k])}" for k in keys if k in flat]
            placed.update(k for k in keys if k in flat)
            if pairs:
                groups.append((label, pairs))
        # A layout fits only a record it was written FOR: most of the fields
        # must land in a group. An emission-curve point carries emission_ma
        # and heat_target_mA among twenty others, and the pulse layout put two
        # of them on labelled lines and eighteen under `other` -- worse than
        # not grouping at all.
        if (len(flat) < cls._GROUP_MIN_FIELDS
                or sum(1 for label, _ in groups if label) < 2
                or len(placed) < cls._GROUP_MIN_FRAC * len(flat)):
            return None
        rest = [f"{k}={cls._scalar(x)}" for k, x in flat.items() if k not in placed]
        if rest:
            groups.append(("other", rest))
        if groups[0][0]:
            groups.insert(0, ("", []))
        return groups

    @classmethod
    def _wrap_pairs(cls, pairs: list, first: str, cont: str, out: list) -> None:
        """Lay k=v pairs out after `first`, continuing under `cont`. Wrapped on
        the pair boundaries, never inside one: a pair split across lines is
        unreadable. A single pair wider than the whole line on its own -- a
        free-text `note=` runs to 130 characters -- is wrapped inside itself
        instead, rather than emitted as one long line and called wrapped."""
        # `first` and `cont` are the same width at every call site, so "has
        # this line got any pairs yet" is just "is it longer than its prefix".
        line = first
        for pair in pairs:
            if len(cont) + len(pair) > cls._WIDTH:
                # Starts on the current line if nothing is on it yet, so a
                # group label is never left alone above its only field.
                if len(line) > len(cont):
                    out.append(line.rstrip())
                    line = cont
                parts = textwrap.wrap(
                    pair, width=cls._WIDTH - len(cont),
                    subsequent_indent="    ",
                    break_long_words=False,
                    break_on_hyphens=False)
                out.append(line + parts[0])
                out.extend(cont + part for part in parts[1:])
                line = cont
                continue
            if len(line) > len(cont) and len(line) + len(pair) > cls._WIDTH:
                out.append(line.rstrip())
                line = cont
            line += pair + "  "
        if line.strip():
            out.append(line.rstrip())

    @classmethod
    def _render(cls, key, v, indent: int, out: list) -> None:
        pad = " " * indent
        if isinstance(v, dict):
            if not v:
                out.append(f"{pad}{key}: {{}}")
                return
            if cls._is_table(v):
                out.append(f"{pad}{key}:")
                w = max(len(str(k)) for k in v) + 2
                for k2, rec in v.items():
                    head = f"{pad}  {str(k2) + ':':<{w}}"
                    pairs = [f"{k}={cls._scalar(x)}"
                             for k, x in cls._lead_first(rec).items()]
                    cls._wrap_pairs(pairs, head, " " * len(head), out)
                return
            out.append(f"{pad}{key}:")
            for k2, v2 in v.items():
                cls._render(k2, v2, indent + 2, out)
            return
        if isinstance(v, (list, tuple)):
            if not v:
                out.append(f"{pad}{key}: []")
                return
            if (all(isinstance(x, str) for x in v)
                    and any(len(x) + indent + 4 > cls._WIDTH for x in v)):
                # Sentences: one per line. Joined with ", " they ran together
                # -- the sentences have commas of their own, so where one item
                # ended could not be seen.
                out.append(f"{pad}{key}: [{len(v)} items]")
                for x in v:
                    out.append(textwrap.fill(
                        x, width=cls._WIDTH, initial_indent=f"{pad}  - ",
                        subsequent_indent=f"{pad}    ",
                        break_long_words=False, break_on_hyphens=False))
                return
            if all(cls._is_flat(x) for x in v):
                line = ", ".join(cls._scalar(x) for x in v)
                if len(line) + len(key) + indent <= cls._WIDTH:
                    out.append(f"{pad}{key}: [{line}]")
                else:
                    out.append(f"{pad}{key}: [{len(v)} items]")
                    for chunk in textwrap.wrap(line, width=cls._WIDTH - indent - 2):
                        out.append(f"{pad}  {chunk}")
                return
            out.append(f"{pad}{key}: [{len(v)} items]")
            for n, item in enumerate(v):
                if isinstance(item, dict):
                    head = f"{pad}  [{n}] "
                    cont = " " * len(head)
                    flat = {k: x for k, x in item.items()
                            if not isinstance(x, (dict, list, tuple))}
                    grouped = cls._group_record(flat)
                    if grouped is None:
                        pairs = [f"{k}={cls._scalar(x)}"
                                 for k, x in cls._lead_first(flat).items()]
                        cls._wrap_pairs(pairs, head, cont, out)
                        if not pairs:
                            out.append(head + "(nested)")
                    else:
                        for label, pairs in grouped:
                            if not label:
                                cls._wrap_pairs(pairs, head, cont, out)
                                continue
                            lead = cont + f"{label:<{cls._GROUP_LABEL_W}}"
                            cls._wrap_pairs(pairs, lead, " " * len(lead), out)
                    # Nested containers inside a record still get their own
                    # lines -- a record is not allowed to swallow its children.
                    for k2, v2 in item.items():
                        if isinstance(v2, (dict, list, tuple)) and v2:
                            cls._render(k2, v2, indent + 6, out)
                else:
                    cls._render(f"[{n}]", item, indent + 2, out)
            return
        if isinstance(v, str) and len(v) + len(key) + indent > cls._WIDTH:
            # break_long_words=False so a path or a URL overflows its line
            # instead of being cut in half. A wrapped path cannot be copied,
            # which makes it worse than a long line, not better.
            out.append(textwrap.fill(v, width=cls._WIDTH,
                                     initial_indent=f"{pad}{key}: ",
                                     subsequent_indent=pad + "    ",
                                     break_long_words=False,
                                     break_on_hyphens=False))
            return
        out.append(f"{pad}{key}: {cls._scalar(v)}")

    @classmethod
    def _pulse_table(cls, res: dict) -> list:
        """One line per fired pulse -- WHEN it fired, on WHICH filament, at WHAT
        heating current (the RP2350's snapshot at that instant, measured and
        commanded), and the emission it produced. Those were all in the result,
        but in three places: tOnUs and heat_meas_mA inside `records`, the
        emission inside `measured`, with a 25-line `status` in between -- so
        "what was the heating when this pulse went out" could not be read off a
        print. Emission is paired by ORDER and only when both lists have the
        same length; otherwise the column is left out rather than guessed."""
        recs = res.get("records")
        if not (isinstance(recs, list) and recs and isinstance(recs[0], dict)
                and "heat_meas_mA" in recs[0]):
            return []
        meas = res.get("measured")
        paired = isinstance(meas, list) and len(meas) == len(recs)

        def num(v, fmt="{:.0f}"):
            return "-" if v is None else fmt.format(v)
        rows = [("#", "fil", "t_on ms", "width us", "heat mA", "target", "diff")
                + (("emission mA", "mA*ms") if paired else ()) + ("flags",)]
        for n, r in enumerate(recs):
            m, t = r.get("heat_meas_mA"), r.get("heat_target_mA")
            heat = num(m) if m is not None else f"n/a ({r.get('heat_meas_unavailable') or '?'})"
            flags = [name for name, key in (("ON-MISMATCH", "on_mismatch"),
                                            ("STUCK-ON", "hv_stuck_on"),
                                            ("unverified", "unverified")) if r.get(key)]
            row = (str(n), num(r.get("filament")),
                   num(None if r.get("tOnUs") is None else r["tOnUs"] / 1000.0, "{:.1f}"),
                   num(r.get("durationUs")), heat,
                   num(t) if t is not None else f"n/a ({r.get('heat_target_unavailable') or '?'})",
                   num(m - t, "{:+.0f}") if m is not None and t is not None else "-")
            if paired:
                e = meas[n] if isinstance(meas[n], dict) else {}
                row += (num(e.get("emission_ma"), "{:.3f}"), num(e.get("emission_mams"), "{:.2f}"))
            rows.append(row + (" ".join(flags) or "-",))
        widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
        out = [f"  pulses: [{len(recs)}]  (heat = RP2350 snapshot at the instant each pulse fired)"]
        for r in rows:
            out.append("    " + "  ".join(c.rjust(w) if i < len(r) - 1 else c
                                          for i, (c, w) in enumerate(zip(r, widths))).rstrip())
        return out

    #: With a pulse table shown, these are firmware-level detail: the raw pulse
    #: records (in the table), the per-pulse ADC internals (answer in the
    #: table), the SHV status block (summarised on one `run:` line), the ADC
    #: reference and the schedule-reuse note. Hidden from the PRINT only --
    #: the dict and the call log keep every field; r.full() prints them all.
    _PULSE_DETAIL = ("records", "measured", "status", "ref_mv", "schedule")
    #: Status counters that mean something went wrong -- shown when non-zero.
    _RUN_COUNTERS = ("mismatches", "off_mismatches", "uncounted", "underfed",
                     "rbDropped", "rbStale", "rbSaturated", "unsafeSlots")

    @classmethod
    def _run_line(cls, st) -> list:
        if not isinstance(st, dict) or "state" not in st:
            return []
        state = SHV_STATE_NAMES.get(st.get("state"), st.get("state"))
        why = SHV_STOP_REASON_NAMES.get(st.get("stopReason"), st.get("stopReason"))
        line = f"  run: {state}"
        if why not in (None, "none", state):
            line += f" ({why})"
        if st.get("totalPulsesTarget") is not None:
            line += f", {st.get('totalPulsesDone')}/{st.get('totalPulsesTarget')} pulses"
        bad = [f"{k}={st[k]}" for k in cls._RUN_COUNTERS if st.get(k)]
        return [line] + ([f"  firmware counters: {'  '.join(bad)}"] if bad else [])

    def full(self) -> str:
        """Every field, nothing hidden -- the firmware-level detail included."""
        return self._render_all(brief=False)

    def __repr__(self) -> str:
        return self._render_all(brief=True)

    def _render_all(self, brief: bool) -> str:
        if not self:
            return "Result({})"
        head = "ok" if self.get("ok") else ("FAILED" if "ok" in self else "")
        out: list[str] = []
        done = {"ok"}
        for k in self._LEAD:
            if k in self and k not in done:
                self._render(k, self[k], 2, out)
                done.add(k)
        table = self._pulse_table(self)
        out.extend(table)   # the answer, before the detail
        if table and brief:
            out.extend(self._run_line(self.get("status")))
            done.update(k for k in self._PULSE_DETAIL if k in self)
        for k, v in self.items():
            if k in done:
                continue
            self._render(k, v, 2, out)
            done.add(k)
        if table and brief:
            out.append("  (firmware detail hidden -- r.full() prints every field; the call log keeps them all)")
        return (f"Result({head})" if head else "Result") + ("\n" + "\n".join(out) if out else "")


class PulseLog(list):
    """shv_pulse_log()'s records -- still a plain list (index it, len() it,
    json.dump it), but it PRINTS as the one-line-per-pulse table: time,
    filament, width, heating current at that instant (measured / commanded),
    flags. A schedule's pulses used to print as a raw list of dicts, the
    heating current one field among fifteen per pulse."""

    def __repr__(self) -> str:
        if not self:
            return "PulseLog([])"
        table = Result._pulse_table({"records": list(self)})
        return "\n".join(["PulseLog"] + table) if table else list.__repr__(self)

    def raw(self) -> list:
        """The plain list."""
        return list(self)



class CTError(Exception):
    """Base error class. Not raised by default — kept for compatibility."""

class CTConnectionError(CTError):
    """Backend unreachable. Not raised by default — a connection failure
    comes back as {"ok": False, "connection_error": True, "error": ...}."""

class CTLeaseError(CTError):
    """Write lease held by another client. Raised ONLY by acquire_lease()
    (and the `lease()` context manager) — see ERROR HANDLING above."""

class CTTimeoutError(CTError):
    """Not raised by default — fire_single_pulse's poll timeout comes back
    as {"ok": False, "timeout": True, ...} instead."""

# ── SHV schedule state constants (ShvGetStatus.state) ────────────────────────

SHV_IDLE     = 0
SHV_ARMED    = 1
SHV_RUNNING  = 2
SHV_COMPLETE = 3
SHV_FAULT    = 4

# ── power state constants ─────────────────────────────────────────────────────

class PowerState(enum.IntEnum):
    """The power ladder, as an enum rather than six loose integers.

    IntEnum, not Enum: these travel over the wire and through JSON as plain
    numbers and an IntEnum member IS that number -- `PowerState.SLEEP == 2` is
    True and json.dumps emits `2`. So the module-level STOP/SLEEP/... below are
    these same members, every existing caller keeps working, and nothing on the
    protocol changes.

    Nothing should be spelled as a bare digit again. A state written as `2` in
    a script, a config or a log line is one nobody can check without going to
    find the table.
    """
    STOP = 1
    SLEEP = 2
    STANDBY = 3
    IDLE = 4
    ACTIVE = 5
    VOLTAGE = 6

    @property
    def energising(self) -> bool:
        """True if this state puts power ON the filament. STANDBY counts: it
        enables the output at the firmware's 0.8 V floor (~0.9 A into a real
        filament). STOP and SLEEP leave it off."""
        return self >= PowerState.STANDBY

    def __str__(self) -> str:
        return f"{self.name}({self.value})"


STOP    = PowerState.STOP
SLEEP   = PowerState.SLEEP
STANDBY = PowerState.STANDBY
IDLE    = PowerState.IDLE
ACTIVE  = PowerState.ACTIVE
VOLTAGE = PowerState.VOLTAGE

# Derived: two hand-maintained copies of one ladder is how one ends up wrong.
_STATE_NAMES = {int(s): s.name for s in PowerState}
_FAULT_NAMES = {0: "none", 1: "open", 2: "OCP/SCP"}


# ── client ────────────────────────────────────────────────────────────────────


# Everything above, for `from ._base import *` in the client's modules --
# underscore names included: the methods use private helpers and constants.
# A LITERAL list, not computed: editors (Pylance/pyright) read __all__
# statically, and a computed one left every star-imported helper
# "not defined" -- goto definition stopped working. tests/test_star_exports.py
# fails if this falls out of step with the module's globals.
__all__ = [
    "ACTIVE", "CLIENT_LOG_DIR", "CTConnectionError", "CTError", "CTLeaseError",
    "CTTimeoutError", "Fid", "IDLE", "NewType", "Path", "PowerState", "PulseLog",
    "Result", "SHV_ARMED", "SHV_COMPLETE", "SHV_FAULT", "SHV_IDLE", "SHV_RUNNING",
    "SHV_STATE_NAMES", "SHV_STOP_REASON_NAMES", "SLEEP", "STANDBY", "STOP", "VOLTAGE",
    "_FAULT_NAMES", "_STATE_NAMES", "contextmanager", "ct_update", "enum", "functools",
    "inspect", "json", "math", "requests", "sys", "textwrap", "threading", "time",
]
