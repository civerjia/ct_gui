"""ct_simple_control.py — thin HTTP client for the CT power-controller backend.

The backend process (backend.py) owns all hardware binding and business logic.
This module is a convenience wrapper around its HTTP API for third-party scripts.

ERROR HANDLING — READ THIS FIRST:
    Almost NOTHING in this client raises an exception. Every method returns a
    dict with an "ok" key (and an "error" key on failure) — check "ok"
    yourself. This is deliberate: this module drives real HV and heating
    hardware, and a script that crashes mid-run on an uncaught exception can
    leave a filament heating or HV energized with no cleanup. A dead
    filament in a loop, a momentarily-absent board, a transient UART
    hiccup — none of these raise. They come back as {"ok": False, ...} so
    your loop can log it and move on to the next filament.

    The ONE exception: acquire_lease() (and the `with ct.lease():` context
    manager) DOES raise CTLeaseError if another client already holds the
    write lock — proceeding without it risks two scripts fighting over the
    same hardware, which genuinely is unsafe. Wrap that one call if you want
    to handle contention yourself; everything else just returns a dict.

    For belt-and-suspenders safety on top of that, wrap your script body in
    `with ct.session():` — it guarantees HV is disabled and every filament
    is stopped on exit, even if your OWN code raises an unrelated exception
    (a bug, a KeyboardInterrupt, whatever) partway through. See session().

    RETRY: every request automatically retries up to CTClient.max_retries
    times (default 2, so 3 attempts total) with a short backoff, but ONLY
    for failures that look like a one-off communication hiccup (a UART
    timeout, an ESP32<->STM32 "Bad Gateway", a dropped TCP frame) — never
    for deterministic rejections (dead filament, bad argument, lease held,
    arm rejected) that won't change on retry. A response that got retried
    carries a "retries" key with the count. Tune it per instance:
        ct.max_retries = 0        # disable retry entirely
        ct.retry_delay_s = 0.5    # base delay between attempts (doubles each time)

WHICH host/port TO CONNECT TO:
    backend.py listens on port 8770 by default and prints something like:

        CT GUI server listening on http://0.0.0.0:8770
          open http://127.0.0.1:8770 · shared API on http://192.168.50.112:8770

    - "0.0.0.0" is just the BIND address (means "all interfaces") — never
      pass this as your client's host.
    - If your script runs on the SAME machine as backend.py, use
      "localhost" or "127.0.0.1" (CTClient's default).
    - If your script runs on a DIFFERENT machine on the network, use the
      "shared API" IP printed at startup (e.g. "192.168.50.112" above) —
      that is backend.py's LAN address, NOT the ESP32's IP.
    - The port is always 8770 in both cases unless CT_GUI_PORT was set
      when backend.py was started.

Usage:
    from ct_simple_control import CTClient

    # Same machine as backend.py:
    ct = CTClient()   # defaults to host="localhost", port=8770

    # Different machine — use backend.py's own LAN IP (from its startup
    # banner), NOT the ESP32's IP:
    ct = CTClient(host="192.168.50.112", port=8770)

    # If the GUI hasn't already connected a controller, do it here — host
    # is the ESP32's own IP, separate from CTClient's host above:
    if not ct.status()["controllers"]["1"]["connected"]:
        r = ct.connect(1, "192.168.50.173")
        if not r["ok"]:
            print(f"connect failed: {r['error']}")

    # A "dead" filament is one you mark as physically damaged, missing, or
    # otherwise off-limits (burnt-out emitter, unseated board, bad switch).
    # Once marked, EVERY batch call (stop_all, sleep_all, idle_all, ...)
    # silently skips it. Any SINGLE-filament call (active_one, idle_one,
    # fire_single_pulse, ...) returns {"ok": False, "dead": True, ...}
    # immediately if targeted at it — it does NOT raise.
    ct.set_dead([6, 26, 73])

    # session() guarantees HV/heating gets torn down on exit even if
    # something below raises unexpectedly (see ERROR HANDLING above).
    with ct.session():
      with ct.lease(ttl=120, note="auto test"):   # the one call that DOES raise
        # Startup safety ladder — walk every populated filament down through
        # the safe states first, then bring the bench to a known baseline:
        ct.stop_all()      # HV off, heating off
        ct.sleep_all()     # low-power resting state
        ct.standby_all()   # powered, not heating — ready before pre-heat

        # Batch control: pre-heat several filaments to idle (warm pool).
        # 1500 mA is a typical IDLE hold current — well below the ~2900 mA
        # ACTIVE firing current, so the CC loop isn't riding at the firing
        # setpoint while just idling:
        ct.idle_all(filaments=[5, 6, 7], currents={5: 1500, 7: 1500})
        # (filament 6 is dead — silently skipped even though it's listed)

        # Single-filament control: promote exactly ONE to ACTIVE (firing
        # current). Uses the RP2350's dedicated single-board wire format,
        # not a batch call.
        #
        # A bare state-setting call only confirms the RP2350 ACCEPTED the
        # command — not that the filament actually reached that current
        # (the board could be absent/faulted). Pass verify=True to poll the
        # real measured current and get honest feedback — never raises:
        r = ct.active_one(5, current_ma=2900, verify=True, timeout_s=5.0)
        if not r["ok"]:
            print(f"active_one command failed: {r.get('error')}")
        elif not r["heating"]["ok"]:
            print(f"WARNING: filament 5 not at target — {r['heating']}")

        # set_emission_v() only sets the DS3502 wiper (the TARGET voltage).
        # enable_emission() is the separate master switch that actually
        # powers the emission HV rail on the STM32 board — nothing is
        # energized until both have been called. This is NOT the SHV
        # schedule "arm" (shv_arm/fire_single_pulse's internal arm step) —
        # that's a different concept on the RP2350 controlling whether a
        # pulse schedule is ready to trigger. enable_emission is unrelated
        # to arming: it must be True before ANY HV (pulsed or continuous)
        # can appear on the bus at all.
        ct.set_emission_v(30)        # backend loads LUT and writes DS3502
        ct.enable_emission(True)
        time.sleep(0.3)              # let the rail settle before reading
        v = ct.read_emission_v()     # None on failure — always check
        if v is not None:
            print(v)

        # ── Pulsed HV fire (SHV schedule) — two ways to trigger it ────────
        # fire_single_pulse internally does: disarm -> download() (the
        # real reliable transfer path, same one the GUI uses) ->
        # verify_schedule() (CRC-checks the table landed correctly) ->
        # arm -> trigger -> poll for COMPLETE/FAULT. It is NOT a hand-rolled
        # sequence of individual SHV commands — see download()/
        # verify_schedule() below if you need to build a custom multi-
        # filament schedule instead of firing just one. Never raises;
        # check result["ok"].

        # trigger="sim": after arming, also asks the ESP32 to generate the
        # SyncIn edge itself (internally posts to /api/sync/simulate) — no
        # external wiring needed. Equivalent standalone call, if you want to
        # trigger it yourself instead of letting fire_single_pulse do it:
        #     ct.simulate_sync(count=1, controller=1)
        result = ct.fire_single_pulse(filament=5, num_pulses=1, width_us=1000,
                                      trigger="sim")
        if not result["ok"]:
            print(f"fire_single_pulse failed: {result.get('error')}")

        # trigger="ext": arms the schedule, then just polls for a REAL
        # electrical edge on the RP2350's SyncIn pin from somewhere outside
        # this API (gantry encoder, bench pulse generator, manual trigger
        # button, or a chained upstream controller's SyncOut). This call
        # does not generate anything itself.
        # result = ct.fire_single_pulse(filament=5, num_pulses=1, width_us=1000,
        #                               trigger="ext", timeout_s=30.0)

        # ── DC HV toggle (continuous, not pulsed) ──────────────────────────
        # A separate bench-test flow: instead of a brief scheduled pulse,
        # route HV continuously to ONE filament's board via its grid switch
        # (hv_grid_set — the per-filament "emission mosfet"), read the
        # steady-state DC current off the ADS1115, then switch it back off.
        # Waits between each step let the switch settle and give the
        # ADS1115 time to complete a fresh conversion before reading.
        ct.hv_grid_set(5, on=True, force=True)   # route HV to filament 5
        time.sleep(0.3)                           # let the switch settle
        dc_ma = ct.read_emission_i()              # None on failure
        print(f"filament 5 DC emission current: {dc_ma} mA")
        ct.hv_grid_set(5, on=False, force=True)  # switch it back off
        time.sleep(0.1)

        ct.enable_emission(False)
        ct.stop_all()
      # lease released here even on error
    # session() teardown (HV off, all filaments stopped) runs here even on error

Dependencies: pip install requests
"""

import math
import time
from contextlib import contextmanager
from typing import NewType

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

STOP    = 1
SLEEP   = 2
STANDBY = 3
IDLE    = 4
ACTIVE  = 5
VOLTAGE = 6

_STATE_NAMES = {1: "STOP", 2: "SLEEP", 3: "STANDBY", 4: "IDLE", 5: "ACTIVE", 6: "VOLTAGE"}
_FAULT_NAMES = {0: "none", 1: "open", 2: "OCP/SCP"}


# ── client ────────────────────────────────────────────────────────────────────

class CTClient:
    """Thin HTTP client for the CT backend. All hardware logic lives server-side.

    See the module-level "ERROR HANDLING" docstring above: methods here
    return {"ok": False, ...} on failure rather than raising, with the sole
    exception of acquire_lease()/lease().
    """

    # ── Filament index spaces ────────────────────────────────────────────
    # THREE spaces exist, and only two words used to name them -- with
    # "physical" meaning both #2 and #1 depending on the sentence, which is
    # how a dead filament once got blamed on the backend (see _dead_result).
    # Each space now has exactly one name, and they are deliberately NOT
    # symmetrical-looking: misreading one for the other has to be visible.
    #
    #   1. SITE       (controller, channel, position) -- where the wire
    #                 physically is. Authority: the harness. Also `slot`
    #                 (0-63), the firmware's power-slot index.
    #   2. FID        0..95 canonical filament id. Authority: the backend's
    #                 MAPPING and the firmware active-list. NEVER "physical".
    #   3. USER_INDEX 0..95 what the SCRIPT calls it, after filament_order.
    #                 Authority: this client only. NEVER "logical".
    #
    # Crossings go through exactly two functions -- _fid_of() outbound and
    # _user_index_of() inbound. `filament_order` must not be touched anywhere
    # else; that is mechanically checkable and the point of the rule.
    #
    # Public API takes and returns USER_INDEX (plain ints), because that is
    # what scripts already pass. Conversion happens at the boundary, and
    # everything past it carries Fid.

    def __init__(
        self,
        host: str = "localhost",      # backend.py's address — SAME machine:
                                       # "localhost" (default). DIFFERENT
                                       # machine: backend.py's own "shared
                                       # API" IP from its startup banner.
                                       # NEVER the ESP32's IP — see docstring.
        port: int = 8770,             # backend.py's port (its default; only
                                       # differs if CT_GUI_PORT was set there)
        client_id: str = "ct_simple_control",  # identifies THIS script in the
                                                # backend's client list/lease
                                                # log — pick something
                                                # descriptive per script
        timeout: float = 5.0,         # default per-request timeout (s); some
                                       # calls (fire_single_pulse, idle_all,
                                       # hv_grid_set_all, present_filaments)
                                       # override this with a longer built-in
                                       # timeout since they take longer
    ):
        """Connect to a running backend.py instance.

        host: "localhost" (default) if this script runs on the SAME
              machine as backend.py. If running from a DIFFERENT machine,
              use the "shared API" IP backend.py printed at startup —
              e.g. "CT GUI server listening on http://0.0.0.0:8770 /
              shared API on http://192.168.50.112:8770" means pass
              host="192.168.50.112" here. NEVER pass "0.0.0.0" — that is
              only a bind address, not something to connect to. This is
              backend.py's own LAN address, NOT the ESP32's IP.
        port: 8770 by default, matching backend.py's default
              (overridable there via the CT_GUI_PORT environment variable).
        """
        self.base = f"http://{host}:{port}"
        self.client_id = client_id
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers.update({
            "X-CT-Client": client_id,
            "Content-Type": "application/json",
        })
        # Dead mask — filaments blocked from all heating and HV pulse operations.
        # Set once with set_dead(); automatically applied to every batch call.
        self.dead: set[int] = set()
        # Software-level USER_INDEX -> FID swap — see set_filament_order().
        self.filament_order: dict[int, int] = {}
        self._order_rev: dict[int, int] = {}     # FID -> USER_INDEX, see _user_index_of
        # Auto-retry for TRANSIENT failures only — see _is_transient() below.
        # Does NOT retry deterministic rejections (dead filament, bad state,
        # lease held, arm rejected) since retrying those just wastes time;
        # only retries things that plausibly succeed on a second try (a
        # dropped UART frame, a momentary ESP32<->STM32 hiccup, a request
        # timeout). Override per-instance if needed: ct.max_retries = 0.
        self.max_retries = 2       # extra attempts beyond the first (3 total)
        self.retry_delay_s = 0.25  # base delay; doubles each retry
        # Per-controller record of the last plan THIS client successfully
        # downloaded, for fire_single_pulse(reuse=True) — see its docstring.
        # _last_plan is a cheap LOCAL pre-filter (skip the network entirely
        # if we already know the plan changed); _last_crc is the actual
        # safety check — the firmware-computed table CRC (from
        # ShvGetTableInfo, returned by verify_schedule() but NOT used in its
        # own "match" field, which only compares entry counts) captured
        # right after OUR OWN successful write. On reuse, we re-read the
        # CRC fresh and compare it to what we recorded — if it still
        # matches, the table content is byte-identical to what we wrote,
        # regardless of who (if anyone) touched it since. This is real
        # content verification, not just "we think nothing changed."
        self._last_plan: dict[int, dict] = {}
        self._last_crc: dict[int, int] = {}

    # ── HTTP helpers ──────────────────────────────────────────────────────────
    # Never raise. Any failure — connection refused, timeout, HTTP error, bad
    # JSON — comes back as a plain {"ok": False, "error": "..."} dict so every
    # method built on top of these is automatically non-raising too.
    #
    # RETRY: backend.py almost always answers with HTTP 200 even when the
    # ESP32<->STM32 or ESP32<->RP2350 hop underneath failed — the real error
    # (a UART timeout, an ESP32-reported "502 Bad Gateway" from ITS proxy to
    # the STM32, a dropped frame) comes back as {"ok": False, "error": "..."}
    # inside that 200 response, not as an HTTP-level failure. So retry can't
    # just watch the status code — _is_transient() below inspects the parsed
    # body's "error" text for known transient-communication signatures.

    _TRANSIENT_MARKERS = (
        "bad gateway", " 502", " 503", " 504",
        "timed out", "timeout",
        "uart", "tcp send failed",
        "connection reset", "connection refused", "connection aborted",
        "broken pipe",
    )
    # Deliberately NOT included: "not connected" (a controller that was
    # never connect()'d stays not-connected no matter how many times you
    # ask — retrying wastes time instead of surfacing the real fix), and
    # anything matching lease/dead-mask/arm-rejection wording, which are
    # all deterministic and won't change on retry.

    def _is_transient(self, resp: dict) -> bool:
        """True if `resp` looks like a one-off communication hiccup worth
        retrying (dropped UART frame, momentary ESP32<->STM32 timeout, TCP
        reset) rather than a deterministic rejection (dead filament, bad
        argument, lease held, arm rejected) that won't change on retry."""
        if resp.get("connection_error") or resp.get("timeout"):
            return True
        err = str(resp.get("error") or "").lower()
        return any(marker in err for marker in self._TRANSIENT_MARKERS)

    def _parse(self, r, path: str) -> dict:
        # Deliberately does NOT call r.raise_for_status() first: the backend
        # sends a meaningful JSON body ({"ok": False, "error": ...}) on many
        # non-2xx statuses (409 lease conflict, 502 hardware-unreachable,
        # etc.) — raising on status would discard that body and replace it
        # with a generic "409 Client Error" string, losing the real reason.
        # So: always try to parse JSON first; only fall back to a generic
        # message built from the raw status if the body genuinely isn't JSON.
        try:
            data = r.json()
        except ValueError:
            return {"ok": False,
                    "error": f"{path} failed: HTTP {r.status_code} {r.reason} — "
                            f"{r.text[:200]}"}
        if isinstance(data, dict):
            data.setdefault("ok", r.ok)
            return data
        return {"ok": r.ok, "data": data}

    def _one_post(self, path: str, body: dict, timeout: float | None) -> dict:
        try:
            r = self._s.post(self.base + path, json=body,
                             timeout=timeout or self.timeout)
        except requests.ConnectionError as exc:
            return {"ok": False, "connection_error": True,
                    "error": f"cannot reach backend {self.base}: {exc}"}
        except requests.Timeout as exc:
            return {"ok": False, "timeout": True, "error": f"request timed out: {exc}"}
        except Exception as exc:
            return {"ok": False, "error": f"POST {path} failed: {exc}"}
        return self._parse(r, path)

    def _one_get(self, path: str, timeout: float | None) -> dict:
        try:
            r = self._s.get(self.base + path, timeout=timeout or self.timeout)
        except requests.ConnectionError as exc:
            return {"ok": False, "connection_error": True,
                    "error": f"cannot reach backend {self.base}: {exc}"}
        except requests.Timeout as exc:
            return {"ok": False, "timeout": True, "error": f"request timed out: {exc}"}
        except Exception as exc:
            return {"ok": False, "error": f"GET {path} failed: {exc}"}
        return self._parse(r, path)

    def _post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        r = self._one_post(path, body, timeout)
        attempt = 0
        while not r.get("ok") and self._is_transient(r) and attempt < self.max_retries:
            time.sleep(self.retry_delay_s * (2 ** attempt))
            attempt += 1
            r = self._one_post(path, body, timeout)
        if attempt:
            r["retries"] = attempt
        return r

    def _get(self, path: str, timeout: float | None = None) -> dict:
        r = self._one_get(path, timeout)
        attempt = 0
        while not r.get("ok") and self._is_transient(r) and attempt < self.max_retries:
            time.sleep(self.retry_delay_s * (2 ** attempt))
            attempt += 1
            r = self._one_get(path, timeout)
        if attempt:
            r["retries"] = attempt
        return r

    # ── connection ────────────────────────────────────────────────────────────
    # A CTClient talking to a running backend.py does NOT imply a controller is
    # bound to hardware yet — the backend must separately connect to the ESP32
    # bridge (either through the GUI, or these calls). Do this once per backend
    # process lifetime (it survives until the backend is restarted or you call
    # disconnect()).

    def connect(self, controller: int, host: str) -> dict:
        """Bind `controller` (1 or 2) to the ESP32 bridge at `host` (the
        ESP32's own IP — NOT the backend's). Does not raise; check "ok" —
        False if the ESP32 is unreachable or its bridge slot is already
        held by another client.

        Returns {"ok", "status": {...}} — status includes rp2350/stm32
        link health once connected.
        """
        return self._post("/api/connect", {"controller": int(controller), "host": str(host)},
                          timeout=15.0)

    def disconnect(self, controller: int) -> dict:
        """Release `controller`'s bridge connection, freeing the ESP32's
        single-client TCP slot for another host to use."""
        return self._post("/api/disconnect", {"controller": int(controller)})

    def status(self) -> dict:
        """Read overall backend status: which controllers are connected, the
        current master, and the lease state.

        Returns {"controllers": {"1": {...}, "2": {...}}, "master": int,
        "lock": {...}, "you": str}. On failure, returns {"ok": False, ...}
        instead — check for the "controllers" key before indexing into it.
        """
        return self._get("/api/status")

    def present_filaments(self) -> list[int]:
        """Live-scan every connected controller for physically-present
        boards and return the global filament indices found.

        SLOW (several seconds per controller — it sleeps every board to
        power the presence-sense rail, then re-scans I2C) and leaves
        touched boards at SLEEP afterward. Not for polling; run it once at
        setup to auto-populate the dead mask. Returns [] on failure (never
        raises) — an empty scan looks the same as "nothing connected", so
        check ct.status() first if you get an unexpectedly empty list:

            present = set(ct.present_filaments())
            ct.set_dead(set(range(96)) - present)
        """
        r = self._get("/api/present-filaments", timeout=30.0)
        return list(r.get("present") or [])

    # ── active-list mapping (filament <-> power slot) ─────────────────────────
    # Which global filament (0-95) sits at which power slot — the
    # host-owned "active-list" model. Every filament is assigned to controller
    # 1, 2, or unassigned; within a controller, filaments pack into power
    # slots (channel*8+position) in ascending filament order. This mapping is
    # what push_active_list()/download() actually push to the RP2350 as the
    # ShvSetActiveList table, and it's what every filament_to_board() lookup
    # elsewhere in this client is built on. Editable, but changing it is a
    # structural change to the whole rig's board layout — everything
    # downstream (currents cache, HV grid addressing, schedules) depends on
    # it, so re-download() after changing it before firing anything.

    def get_mapping(self) -> dict:
        """Read the current filament<->power-slot mapping.

        Returns {"ok", "mapping": {
            "group_size": int,
            "counts": {"1": int, "2": int},           # filaments per controller
            "overflow": {"1": [...], "2": [...]},     # assigned but past slot 63 — can't fire
            "channel_mask": {"1": int, "2": int},      # bitmask of channels in use
            "skip_channels": {"1": [...], "2": [...]}, # channels deliberately left empty
            "filaments": [{"filament", "controller", "slot", "channel", "position"}, ...],
        }}
        `controller` in each row is 0-based (0 or 1) or None if unassigned;
        `slot`/`channel`/`position` are None if unassigned or overflowed.
        """
        return self._get("/api/mapping")

    def set_mapping(self, assignment=None,       # list[96] of 0/1/None -- which
                                                  # controller each filament
                                                  # belongs to (0-based); see below
                    group_size: int | None = None,  # reset to the default
                                                     # alternating pattern instead
                                                     # of a custom assignment
                    skip_channels=None,           # channels to leave empty
                                                   # (broken boards) -- see below
                    upload: bool = True) -> dict:  # also push to hardware now
                                                    # (False = stage host-side only)
        """Edit the filament<->power-slot mapping. Pass ANY combination:

        assignment: list[96] of 0/1/None — which controller (0-based) each
            global filament belongs to. Within a controller, filaments
            always pack into slots in ascending filament-index order — this
            model does not support an arbitrary custom slot order, only
            which controller a filament lands on.
        group_size: resets to the default alternating-group pattern instead
            of a custom assignment (groups of this many filaments alternate
            controller 0/1). Ignored if `assignment` is also given, unless
            you pass both to set a custom assignment AND remember the group
            size that produced it.
        skip_channels: {"1": [chan_idx, ...], "2": [...]} (0-indexed channels
            to treat as broken/empty — filaments skip over them when
            packing into slots) — or a flat list applied to both controllers.
        upload: also push the new SHV active-list + channel mask to every
            connected controller immediately (default True). Set False to
            stage the change host-side only, e.g. before both controllers
            are connected yet.

        Invalidates the host-side currents-download cache (remapping moves
        filaments between boards, so cached idle/active mA per filament are
        no longer meaningful) — the next download() re-sends them.

        Returns {"ok", "mapping": {...same shape as get_mapping()...},
        "uploaded": {"1": {"ok": bool}, "2": {...}}} (uploaded present only
        if upload=True).
        """
        body: dict = {"upload": bool(upload)}
        if assignment is not None:
            body["assignment"] = list(assignment)
        if group_size is not None:
            body["group_size"] = int(group_size)
        if skip_channels is not None:
            body["skip_channels"] = skip_channels
        return self._post("/api/mapping", body, timeout=15.0)

    def filament_to_board(self, filament: int) -> dict | None:
        """Forward lookup: global filament index (0-95) -> SITE (controller/channel/position). Returns None if the filament is unassigned or overflowed
        past the usable slots (can't fire — see get_mapping()'s "overflow").

        Returns {"controller": 1|2, "channel": 0-7, "position": 0-7, "slot": 0-63}.
        """
        # get_mapping() reflects the BACKEND's hardware active-list, which
        # knows nothing about the client-side filament-order swap — look up
        # the FID -- what the backend actually calls this filament.
        fid = self._fid_of(filament)
        m = self.get_mapping().get("mapping") or {}
        for row in m.get("filaments") or []:
            if row.get("filament") == fid:
                if row.get("controller") is None or row.get("slot") is None:
                    return None
                return {"controller": row["controller"] + 1, "channel": row["channel"],
                        "position": row["position"], "slot": row["slot"]}
        return None

    def board_to_filament(self,
                          controller: int,  # 1 or 2 (matches CTClient's
                                             # convention elsewhere; 1-based)
                          channel: int,     # 0-7
                          position: int) -> int | None:  # 0-7 (aka "mux_port")
        """Reverse lookup: SITE (controller/channel/position) -> global filament index.

        controller: 1 or 2. channel/position: 0-7. Returns None if that
        slot is unassigned.
        """
        m = self.get_mapping().get("mapping") or {}
        for row in m.get("filaments") or []:
            if (row.get("controller") == controller - 1 and row.get("channel") == channel
                and row.get("position") == position):
                fid = row.get("filament")
                # translate back so you always see YOUR (USER_INDEX) numbering
                return self._user_index_of(fid) if fid is not None else None
        return None

    # ── lease ─────────────────────────────────────────────────────────────────
    # acquire_lease() / lease() are the ONE place in this client that raises by
    # default — see ERROR HANDLING at the top of this file for why.

    def acquire_lease(self, ttl: float = 60.0, note: str = "") -> dict:
        """Acquire an exclusive write lock (blocks other GUI writes while held).

        Raises CTLeaseError if another client already holds it — proceeding
        without the lease could mean two scripts fighting over the same
        hardware, which genuinely is unsafe, so this fails loudly rather
        than silently continuing. A connection failure to the backend
        itself does NOT raise (matches every other method) — check the
        returned dict's "ok" for that case instead.

        Returns the raw lease response dict on success.
        """
        r = self._post("/api/lock", {"action": "acquire", "ttl": ttl, "note": note})
        if not r.get("ok") and not r.get("connection_error"):
            snap = r.get("lock") or {}
            raise CTLeaseError(
                f"Lease held by '{snap.get('owner')}' "
                f"({snap.get('expires_in_s', '?')} s remaining)"
            )
        return r

    def release_lease(self) -> dict:
        """Release the lease. Safe to call when not held."""
        return self._post("/api/lock", {"action": "release"})

    def renew_lease(self, ttl: float = 60.0) -> dict:
        """Extend the current lease before it expires."""
        return self._post("/api/lock", {"action": "acquire", "ttl": ttl})

    @contextmanager
    def lease(self, ttl: float = 60.0, note: str = ""):
        """Context manager: acquire lease on enter, release on exit.

        Raises CTLeaseError on enter if another client holds it — see
        acquire_lease(). Always releases on exit, including on error.
        """
        self.acquire_lease(ttl=ttl, note=note)
        try:
            yield self
        finally:
            self.release_lease()

    # ── session — guaranteed safe teardown ────────────────────────────────────

    @contextmanager
    def session(self, cleanup: bool = True):
        """Guarantees a safe teardown runs on exit from the `with` block —
        even if your own code raises an unrelated exception partway through
        (a bug, a KeyboardInterrupt, anything). Does NOT suppress the
        exception; it re-raises after cleanup so you still see what broke.

        Teardown (when cleanup=True, the default): disable emission HV,
        disable focus HV, instantly clear the HV grid, then STOP every
        populated filament. Each step is attempted independently — one
        failing doesn't block the rest.

        Use this to wrap your script's whole body as extra insurance on
        top of this client's normal non-raising behavior (see ERROR
        HANDLING at the top of this file) — belt and suspenders:

            with ct.session():
                ct.active_one(5, 2900)
                ct.enable_emission(True)
                ...  # if this raises, HV still gets shut off safely
        """
        try:
            yield self
        finally:
            if cleanup:
                for fn in (lambda: self.enable_emission(False),
                          lambda: self.enable_focus(False),
                          lambda: self.hv_grid_clear_all(),
                          lambda: self.stop_all()):
                    try:
                        fn()
                    except Exception:
                        pass

    # ── dead mask ─────────────────────────────────────────────────────────────

    def set_dead(self, filaments) -> None:
        """Replace the dead mask with the given filament indices (0–95).

        Dead filaments are silently skipped in every batch power-state call.
        Any SINGLE-filament call (active_one, idle_one, fire_single_pulse,
        hv_grid_set, ...) returns {"ok": False, "dead": True, ...}
        immediately if targeted at one — it does NOT raise.

        Example:
            ct.set_dead([3, 7, 12, 55])
        """
        self.dead = {int(f) for f in filaments}

    def add_dead(self, *filaments: int) -> None:
        """Add filaments to the dead mask."""
        self.dead.update(int(f) for f in filaments)

    def remove_dead(self, *filaments: int) -> None:
        """Remove filaments from the dead mask."""
        self.dead.difference_update(filaments)

    def _live(self, filaments=None) -> list[int] | None:
        """Return the filament list with dead filaments removed and the
        filament-order swap applied (USER_INDEX -> FID).

        If no dead mask/swap is set and filaments is None, returns None so
        the backend applies the command to all populated boards (most
        efficient) — a pure permutation swap doesn't change the SET of
        "every filament", so this shortcut stays valid even with a swap set.
        """
        if not self.dead and not self._swap_active and filaments is None:
            return None
        base = list(filaments) if filaments is not None else list(range(self.FILAMENT_COUNT))
        survivors = [f for f in base if f not in self.dead]   # dead mask is in USER_INDEX space
        return self._fids_of(survivors)                      # then cross to FID

    def _is_dead(self, filament: int) -> bool:
        """True if filament is in the dead mask."""
        return filament in self.dead

    def _dead_result(self, filament: int, extra: dict | None = None) -> dict:
        """The standard soft-failure dict returned for a dead-filament target."""
        out = {"ok": False, "dead": True, "filament": int(filament),
               "error": f"filament {filament} is in the dead mask"}
        if extra:
            out.update(extra)
        return out

    # ── filament order swap (software-level, client-side) ────────────────────
    # Purely client-side index remap — never touches the backend's hardware
    # active-list mapping (see get_mapping()/set_mapping() for that). Use this
    # to correct for boards being physically wired in a different order than
    # you'd naturally number them: after set_filament_order(), you keep using
    # YOUR OWN (USER_INDEX) numbering everywhere — dead mask, single-filament
    # calls, batch calls, reads — and every one of them transparently talks
    # to the FID (actually-wired filament) underneath. You never need to
    # translate anything yourself.

    FILAMENT_COUNT = 96      # USER_INDEX filaments 0..95

    @classmethod
    def identity_order(cls) -> list[int]:
        """The no-swap order: [0, 1, 2, ..., 95]. Start from this, change the
        entries you need, and pass the whole list to set_filament_order()."""
        return list(range(cls.FILAMENT_COUNT))

    def set_filament_order(self, order) -> None:
        """Define the USER_INDEX -> FID mapping EXPLICITLY.

        order: a sequence of exactly 96 integers, where order[i] is the
        FID that your USER_INDEX i refers to. Pass None
        or an empty sequence to clear back to identity (no remapping).

        The whole table is stated, not a diff. The previous "only list what
        differs" forms are gone on purpose: a partial mapping cannot be
        checked for validity, because the entries you left out are exactly
        the ones a conflict would hide. Writing all 96 makes the mapping
        checkable, and it is checked -- see below.

        MUST BE ONE-TO-ONE. Every filament 0..95 has to appear exactly once,
        so the mapping is a true permutation and is reversible: this client
        crosses your indices to FIDs on the way out and back to
        yours on the way in, and that round trip only works if no two USER_INDEX
        values claim the same FID. Raises ValueError naming the
        offending entries otherwise -- a length that isn't 96, a value outside
        0..95, or any duplicate.

        Example -- filaments 5 and 8 are physically swapped on the backplane:
            order = CTClient.identity_order()
            order[5], order[8] = 8, 5        # state BOTH directions yourself
            ct.set_filament_order(order)

            ct.active_one(5, 2900)           # commands FID 8
            ct.read_filament_current(5)      # reads FID 8,
                                              # returned keyed as "filament 5"

        Note you now write both directions explicitly. The old dict form
        applied {5: 8} in both directions for you; that convenience is what
        made a partial table ambiguous, so the table says what it means now.
        """
        n = self.FILAMENT_COUNT
        if order is None or (hasattr(order, "__len__") and len(order) == 0):
            self.filament_order = {}
            self._order_rev = {}
            return
        if isinstance(order, dict):
            raise ValueError(
                "set_filament_order: pass an explicit sequence of "
                f"{n} indices, not a dict. Start from CTClient.identity_order() "
                "and assign the entries you need (both directions of a swap). "
                "A partial mapping can't be validated, which is why it's no "
                "longer accepted; pass None or [] to clear back to identity.")
        try:
            seq = [int(v) for v in order]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"set_filament_order: order must be a sequence "
                             f"of {n} integers ({exc})") from None
        if len(seq) != n:
            raise ValueError(
                f"set_filament_order: expected exactly {n} entries "
                f"(one per filament 0..{n - 1}), got {len(seq)}")
        bad = [(i, v) for i, v in enumerate(seq) if not (0 <= v < n)]
        if bad:
            raise ValueError(
                f"set_filament_order: value(s) outside 0..{n - 1}: "
                + ", ".join(f"order[{i}]={v}" for i, v in bad[:8])
                + (f" (and {len(bad) - 8} more)" if len(bad) > 8 else ""))
        seen: dict[int, int] = {}
        dupes: list[str] = []
        for i, v in enumerate(seq):
            if v in seen:
                dupes.append(f"FID {v} claimed by both USER_INDEX {seen[v]} and {i}")
            else:
                seen[v] = i
            
        if dupes:
            raise ValueError(
                "set_filament_order: mapping is not one-to-one — "
                + "; ".join(dupes[:6])
                + (f" (and {len(dupes) - 6} more)" if len(dupes) > 6 else "")
                + ". Every filament 0..%d must appear exactly once, or "
                  "translating results back to your numbering is ambiguous." % (n - 1))
        # Store only the entries that actually differ: _fid_of/_user_index_of short-
        # circuit on an empty table, so identity stays free.
        self.filament_order = {i: v for i, v in enumerate(seq) if v != i}
        # Built here, not on lookup: set_filament_order() already proved the
        # mapping is a permutation, so the inverse is well-defined and total.
        self._order_rev = {v: k for k, v in self.filament_order.items()}

    def get_filament_order(self) -> list[int]:
        """The current mapping as an explicit 96-entry list, order[i] = the
        FID that USER_INDEX i refers to. Identity when no remapping is
        set, so this always round-trips through set_filament_order()."""
        return [self.filament_order.get(i, i) for i in range(self.FILAMENT_COUNT)]

    @property
    def _swap_active(self) -> bool:
        """Whether any USER_INDEX differs from its FID. Exists so that the only
        places `filament_order` itself is read are the two crossing functions
        and its own setter/getter -- which makes the boundary rule something a
        grep can check, not just a convention."""
        return bool(self.filament_order)

    def _fid_of(self, filament: int) -> Fid:
        """USER_INDEX -> FID. The only outbound crossing (identity with no swap)."""
        f = int(filament)
        return Fid(self.filament_order.get(f, f) if self.filament_order else f)

    def _fids_of(self, filaments) -> list[Fid]:
        """Translate a list of USER_INDEX values to FIDs."""
        return [self._fid_of(f) for f in filaments]

    def _fid_keys(self, d: dict) -> dict:
        """Translate a {filament: value} dict's KEYS from USER_INDEX to FID."""
        return {self._fid_of(k): v for k, v in d.items()}

    def _user_index_of(self, fid: int) -> int:
        """FID -> USER_INDEX. The only inbound crossing — re-keys read results
        so a script always sees its OWN numbering back.

        Uses a reverse map built once in set_filament_order(): this runs per
        filament inside result loops, and the linear scan it replaces was
        O(len(order)) on every single lookup.
        """
        if not self._order_rev:
            return int(fid)
        return self._order_rev.get(int(fid), int(fid))

    def _reindex_response(self, r: dict, keys=("applied", "failed")) -> dict:
        """Re-key filament-index LISTS in a batch response from FID
        back to USER_INDEX — both at the top level and inside any
        per-controller "results" sub-dict. Only touches keys whose value is
        actually a list (some responses use "applied" as a plain COUNT, not
        a list of indices — those are left untouched). Every batch method
        that returns filament-index lists runs its response through this."""
        for k in keys:
            if isinstance(r.get(k), list):
                r[k] = [self._user_index_of(f) for f in r[k]]
        for row in (r.get("results") or {}).values():
            if isinstance(row, dict):
                for k in keys:
                    if isinstance(row.get(k), list):
                        row[k] = [self._user_index_of(f) for f in row[k]]
        return r

    # ── power state ───────────────────────────────────────────────────────────

    def _prep(self, state: int, filaments=None,
              currents: dict | None = None, arg: int = 0) -> dict:
        # USER_INDEX values this call actually asked for that the dead mask
        # drops BEFORE anything is sent — _live()'s filtering is invisible
        # to the caller otherwise. A filament silently vanishing here (e.g.
        # because a filament_order swap happens to route a DEAD USER_INDEX
        # onto an otherwise-fine FID) looked exactly like
        # a backend bug until this was surfaced -- see the "why was 25
        # skipped" investigation this traced back to set_dead().
        requested = [int(f) for f in filaments] if filaments is not None else list(range(96))
        dead_skipped = [f for f in requested if f in self.dead]
        live = self._live(filaments)
        if live is not None and len(live) == 0:
            return {"ok": True, "applied": 0, "failed": [], "skipped_dead": True,
                    "dead_skipped": dead_skipped}
        body: dict = {"state": state, "arg": arg}
        if live is not None:
            body["filaments"] = live
        if currents:
            # strip dead filaments (checked on USER_INDEX keys), then cross
            # the survivors' keys to FID for the wire
            alive = {int(k): v for k, v in currents.items() if int(k) not in self.dead}
            body["currents"] = {str(self._fid_of(k)): int(v) for k, v in alive.items()}
        # "excluded" (top level) + "touched"/"not_this_controller"/"unslotted"
        # (per-controller, inside "results") are filament-index lists too --
        # re-key them back to USER_INDEX the same as applied/failed, or a swap
        # would leak FIDs into what is supposed to be an all-USER_INDEX
        # response.
        r = self._reindex_response(
            self._post("/api/filament-prep", body, timeout=20.0),
            keys=("applied", "failed", "excluded", "touched", "not_this_controller", "unslotted"))
        if dead_skipped:
            r["dead_skipped"] = dead_skipped
        return r

    def stop_all(self, filaments=None) -> dict:
        """STOP a BATCH of filaments (all populated boards, or `filaments`),
        excluding the dead mask. For exactly one filament, use stop_one()."""
        return self._prep(STOP, filaments)

    def sleep_all(self, filaments=None) -> dict:
        """SLEEP a BATCH of filaments, excluding the dead mask.
        For exactly one filament, use sleep_one()."""
        return self._prep(SLEEP, filaments)

    def standby_all(self, filaments=None) -> dict:
        """STANDBY a BATCH of filaments, excluding the dead mask.
        For exactly one filament, use standby_one()."""
        return self._prep(STANDBY, filaments)

    def idle_all(self,
                filaments=None,               # None = every populated board
                                               # (minus dead mask); or an
                                               # explicit list of indices
                currents: dict | None = None,   # {filament: mA} -- explicit
                                                 # PER-FILAMENT override
                default_ma: float = 0) -> dict:  # mA for any filament NOT in
                                                  # `currents` above -- ⚠ NO
                                                  # firmware-side default: a
                                                  # filament with neither an
                                                  # entry here nor in
                                                  # `currents` idles at 0 mA
        """IDLE a BATCH of filaments, excluding the dead mask.
        For exactly one filament, use idle_one() instead — it's clearer and
        avoids the "everyone else falls back to default_ma" footgun below.

        currents: {filament: mA} — per-filament idle current override.
        default_ma: current used for any filament NOT listed in `currents`
        (including every filament, if `currents` is omitted entirely).
        There is no firmware-side default — omitting both leaves every
        filament idling at 0 mA.
        """
        return self._prep(IDLE, filaments, currents, arg=int(default_ma))

    def active_all(self,
                   filaments=None,               # None = every populated
                                                  # board (minus dead mask)
                   currents: dict | None = None,   # {filament: mA} -- explicit
                                                    # PER-FILAMENT override
                   default_ma: float = 0) -> dict:  # mA for any filament NOT
                                                     # in `currents` -- same
                                                     # "no default" footgun as
                                                     # idle_all, see above
        """ACTIVE a BATCH of filaments, excluding the dead mask.
        For exactly one filament, use active_one() instead.

        currents: {filament: mA} — per-filament active current override.
        default_ma: current used for any filament NOT listed in `currents`.
        """
        return self._prep(ACTIVE, filaments, currents, arg=int(default_ma))

    def voltage_all(self,
                    filaments=None,               # None = every populated
                                                   # board (minus dead mask)
                    millivolts: dict | None = None,  # {filament: mV} --
                                                      # explicit PER-FILAMENT
                                                      # target-voltage override
                    default_mv: float = 800) -> dict:  # mV for any filament
                                                        # NOT in `millivolts` --
                                                        # defaults to 800 (the
                                                        # firmware's own
                                                        # STANDBY floor), NOT 0
                                                        # like idle_all/
                                                        # active_all's
                                                        # default_ma, because 0
                                                        # is below the
                                                        # firmware's clamp
        """Drive a BATCH of filaments to manual VOLTAGE mode (PowerState 6)
        at a fixed mV, excluding the dead mask. For exactly one filament,
        use voltage_one() instead.

        VOLTAGE is a fixed-voltage hold, NOT current-regulated (unlike
        IDLE/ACTIVE's closed CC loop) — mostly for bench/calibration use
        (e.g. probing an arbitrary point on the load curve) rather than
        normal heating control.

        millivolts: {filament: mV} — per-filament target-voltage override.
        default_mv: mV used for any filament NOT listed in `millivolts`
        (including every filament, if `millivolts` is omitted entirely).
        Firmware clamps every value to 0.8-15 V (800-15000 mV) regardless
        of what's requested here.
        """
        return self._prep(VOLTAGE, filaments, millivolts, arg=int(default_mv))

    # ── power state — single filament ─────────────────────────────────────────
    # These use the RP2350's own single-board CH_SET_POWER_STATE wire format
    # (one filament, one frame) — NOT the batch-with-one-item path the *_all
    # methods above take even when given a 1-element filaments= list. Prefer
    # these whenever you're operating on exactly one filament.
    #
    # None of these raise. A dead filament, an unmapped filament, a
    # disconnected controller, or a board that simply didn't ACK all come
    # back as {"ok": False, "error": "...", ...} — check "ok" yourself.

    def _state_one(self, filament: int, state: int, arg: int, op: str) -> dict:
        if self._is_dead(filament):   # dead mask is USER_INDEX, which is what the caller passed
            return self._dead_result(filament)
        r = self._post("/api/filament-state",
                       {"filament": self._fid_of(filament), "state": state, "arg": int(arg)})
        # The board-didn't-ACK soft failure carries no "error" message on the
        # wire — fill one in so a printed/logged result is never just "None".
        if not r.get("ok") and not r.get("error"):
            r["error"] = "board did not ACK (absent, unseated, or faulted?)"
        r["filament"] = int(filament)   # always echo back YOUR (USER_INDEX) number, not the FID
        return r

    def wait_for_current(self, filament: int,
                         target_ma: float,             # the current you commanded
                                                        # (idle_one/active_one's
                                                        # current_ma) — what we're
                                                        # waiting to see measured
                         tolerance_ma: float = 150.0,   # how close counts as "there"
                                                         # (CC loop settles near, not
                                                         # exactly at, the target)
                         timeout_s: float = 5.0,        # give up and return
                                                         # ok=False after this long
                         poll_interval_s: float = 0.2) -> dict:  # how often to
                                                                  # re-check while waiting
        """Poll the REAL measured heating current until it settles within
        `tolerance_ma` of `target_ma`, or `timeout_s` elapses.

        A command like idle_one()/active_one() only confirms the RP2350
        accepted the command — it says nothing about whether the CC loop
        actually got the filament there (a board could be absent, faulted,
        thermally slow, or the target could simply be unreachable). This
        polls read_filament_currents() (no I2C, cheap) to give you the real
        answer. Never raises — check the returned "ok".

        Returns: {"ok": bool,          # reached target within tolerance
                  "filament": int, "target_ma": float, "measured_ma": float,
                  "measured_valid": bool,   # False = NO source could give a
                                            # live measurement; measured_ma is
                                            # 0.0 filler and "ok" is False
                  "measured_from": "cached" | "ina219" | "power_state",
                                            # which evidence answered. See the
                                            # note below on "power_state".
                                            # The CC cache stops being maintained
                                            # once the loop isn't regulating (i.e.
                                            # after stop/sleep), so a 0 mA target
                                            # is normally confirmed via ina219
                  "elapsed_s": float, "present": bool, "cc_mode": int}
                  cc_mode: 0=voltage 1=current(regulating) 2/3=fault.
        A ~0 mA TARGET IS CONFIRMED BY POWER STATE, NOT BY CURRENT, and it has
        to be: stopping a board drops its rail, so the INA presence probe stops
        answering for it and no current reading exists any more. Waiting for a
        measured 0 would therefore never succeed. Such a result carries
        measured_from="power_state" and measured_valid=False -- it is a
        confirmation, explicitly not a reading, and describe() says so.

        Known limit, and why it is the acceptable direction: once the rail is
        down this cannot tell a real stopped board from a board that was never
        there (read_board_status reports STOP for both, and the only probe that
        distinguishes them is slow and leaves boards at SLEEP). So stop_one() on
        an absent filament reports success. The dangerous direction -- calling a
        still-heating board stopped -- cannot happen: the state comes from the
        firmware's own per-board read, and a board being driven reports
        IDLE/ACTIVE, not STOP. Verified on hardware.
        """
        start = time.monotonic()
        deadline = start + timeout_s
        data: dict = {}
        while True:
            data = self.read_filament_current_cached(filament).get(int(filament), {})
            # current_mA is None (key PRESENT, value None) when the RP2350's
            # cached reading isn't a live measurement yet -- so .get(...,0)
            # never fires and float(None) would raise. None also must not
            # count as a measured 0, or stop_one(verify=True) (target 0,
            # tolerance 50) would report success off a missing reading.
            raw = data.get("current_mA")
            valid = raw is not None
            source = "cached"
            if not valid:
                # The CC cache has no live measurement. That is NORMAL and
                # permanent for a stop/sleep/standby target: once the loop is no
                # longer regulating the port it stops maintaining a current, so
                # polling the cache alone can never confirm 0 mA and this call
                # would burn its full timeout and report "did NOT reach" for a
                # filament that stopped correctly (measured on hardware: 8.7 s to
                # a wrong answer). The live INA219 read CAN still see it. Fall
                # back to it -- it costs an I2C sweep, but only on the iteration
                # where the cache has nothing, and a stop confirms on the first
                # one. Skipped automatically mid-run: the backend refuses the
                # sweep while a schedule fires and answers cached (cached=True),
                # which we do not accept as a measurement.
                # A STOPPED board cannot be confirmed by CURRENT at all, and
                # that is structural, not a flake. Stopping removes the board's
                # rail, so the live INA presence probe reports present=False --
                # indistinguishable from a board that was never there, which is
                # exactly why the `present` guard below exists. Result: a
                # stop/sleep/standby verification could never succeed; measured
                # here, stop_one(verify=True) burned its full timeout and said
                # "did NOT reach 0.0 mA" for a filament that had stopped
                # correctly. (One earlier run passed only because the rail had
                # not collapsed yet -- timing luck, not confirmation.)
                #
                # For a ~0 mA target the honest instrument is the board's own
                # power state: if the CC loop is no longer driving it and the
                # board reports a non-heating state, it is not heating. That is
                # reported as measured_from="power_state" with
                # measured_valid=False, so it can never be mistaken for a
                # measured zero -- confirmation, but explicitly not a reading.
                if abs(float(target_ma)) <= tolerance_ma:
                    st = self.read_board_status(filament)
                    if st.get("ok") and st.get("state") in (STOP, SLEEP, STANDBY):
                        return {"ok": True, "filament": int(filament),
                                "target_ma": float(target_ma), "measured_ma": 0.0,
                                "measured_valid": False, "measured_from": "power_state",
                                # NOT "state_name": that key is read_board_status's
                                # shape and describe() matches it first, which made a
                                # standalone wait_for_current result render as a board
                                # status line instead of a heating one.
                                "power_state": st.get("state_name"),
                                "elapsed_s": time.monotonic() - start,
                                "present": bool(data.get("present", False)),
                                "cc_mode": data.get("cc_mode", 0),
                                "note": (f"not heating — board reports "
                                         f"{st.get('state_name')}; no current reading is "
                                         f"available once the rail is down, so this is "
                                         f"confirmed by power state, not measured")}
                live = self.read_filament_vi_live([filament]).get(int(filament), {})
                # `present` is NOT optional here. The INA sweep reports a board it
                # could not find as 0 mA with present=False, so accepting any
                # non-None value re-opens the exact false-success this method
                # exists to close -- an absent board "confirmed" at 0 mA. That
                # regression was introduced by this very fallback and caught in
                # test; the guard is the only thing separating "measured 0" from
                # "nothing there to measure".
                if live and not live.get("cached") and live.get("present"):
                    lraw = live.get("current_mA")
                    if lraw is not None:
                        raw, valid, source = lraw, True, "ina219"
            measured = float(raw) if valid else 0.0
            ok = valid and abs(measured - target_ma) <= tolerance_ma
            if ok or time.monotonic() >= deadline:
                return {"ok": ok, "filament": int(filament), "target_ma": float(target_ma),
                        "measured_ma": measured, "measured_valid": valid,
                        "measured_from": source,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False)),
                        "cc_mode": data.get("cc_mode", 0)}
            time.sleep(poll_interval_s)

    def stop_one(self, filament: int,
                verify: bool = False,      # confirm current drops to ~0 mA
                                            # afterward (real feedback, see docstring)
                timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """STOP a single filament. Returns {"ok": False, "dead": True, ...}
        if the filament is dead — does not raise.

        verify=True: poll the measured current down to ~0 mA afterward and
        merge that feedback under result["heating"] — confirms the filament
        actually stopped heating, not just that the command was accepted.
        """
        r = self._state_one(filament, STOP, 0, "stop_one")
        if verify and not r.get("dead"):
            r = {**r, "heating": self.wait_for_current(filament, 0, tolerance_ma=50,
                                                       timeout_s=timeout_s)}
        return r

    def sleep_one(self, filament: int,
                 verify: bool = False,      # confirm current drops to ~0 mA
                                             # afterward (real feedback, see docstring)
                 timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """SLEEP a single filament. Returns {"ok": False, "dead": True, ...}
        if the filament is dead — does not raise.

        verify=True: same real-current feedback as stop_one(verify=True).
        """
        r = self._state_one(filament, SLEEP, 0, "sleep_one")
        if verify and not r.get("dead"):
            r = {**r, "heating": self.wait_for_current(filament, 0, tolerance_ma=50,
                                                       timeout_s=timeout_s)}
        return r

    def standby_one(self, filament: int,
                    verify: bool = False,      # confirm current drops to ~0 mA
                                                # afterward (real feedback, see docstring)
                    timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """STANDBY a single filament. Returns {"ok": False, "dead": True, ...}
        if the filament is dead — does not raise.

        verify=True: same real-current feedback as stop_one(verify=True).
        """
        r = self._state_one(filament, STANDBY, 0, "standby_one")
        if verify and not r.get("dead"):
            r = {**r, "heating": self.wait_for_current(filament, 0, tolerance_ma=50,
                                                       timeout_s=timeout_s)}
        return r

    def idle_one(self, filament: int,
                current_ma: float,             # target IDLE (warm-pool) hold
                                                # current in mA -- typically
                                                # well below the ACTIVE firing
                                                # current, e.g. 1500 vs 2900
                verify: bool = False,          # poll real measured current
                                                # after commanding -- see below
                tolerance_ma: float = 150.0,   # only used if verify=True --
                                                # passed straight to
                                                # wait_for_current()
                timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """IDLE a single filament at `current_ma` mA. Returns
        {"ok": False, "dead": True, ...} if the filament is dead — does
        not raise.

        verify=True: after commanding, poll the REAL measured current until
        it settles within `tolerance_ma` of `current_ma` (or `timeout_s`
        elapses) and merge that feedback under result["heating"] — so you
        know the filament actually reached the current you asked for, not
        just that the command was accepted:
            {"ok": bool, "measured_ma": float, "elapsed_s": float,
             "present": bool, "cc_mode": int}
        """
        r = self._state_one(filament, IDLE, int(current_ma), "idle_one")
        if verify and not r.get("dead"):
            r = {**r, "heating": self.wait_for_current(filament, current_ma,
                                                       tolerance_ma, timeout_s)}
        return r

    def active_one(self, filament: int,
                   current_ma: float,             # target ACTIVE (firing) current
                                                   # in mA -- the real operating
                                                   # current, e.g. 2900
                   verify: bool = False,          # poll real measured current
                                                   # after commanding -- see below
                   tolerance_ma: float = 150.0,   # only used if verify=True --
                                                   # passed straight to
                                                   # wait_for_current()
                   timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """Promote a single filament to ACTIVE at `current_ma` mA. Returns
        {"ok": False, "dead": True, ...} if the filament is dead — does
        not raise.

        verify=True: same real-current feedback as idle_one(verify=True),
        merged under result["heating"].
        """
        r = self._state_one(filament, ACTIVE, int(current_ma), "active_one")
        if verify and not r.get("dead"):
            r = {**r, "heating": self.wait_for_current(filament, current_ma,
                                                       tolerance_ma, timeout_s)}
        return r

    def voltage_one(self, filament: int,
                    millivolts: float,             # target voltage in mV --
                                                    # firmware clamps to
                                                    # 0.8-15 V (800-15000);
                                                    # out-of-range is
                                                    # rejected HERE before
                                                    # any frame is sent
                    verify: bool = False,          # confirm the board
                                                    # actually entered
                                                    # voltage-regulation mode
                                                    # afterward -- see below
                    timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """Drive a single filament to manual VOLTAGE mode (PowerState 6) at
        `millivolts` mV -- a fixed voltage hold, NOT current-regulated
        (unlike IDLE/ACTIVE, which hold a commanded mA via the closed CC
        loop). Mostly for bench/calibration use (e.g. probing an arbitrary
        point on the load curve) rather than normal heating control.

        Returns {"ok": False, "dead": True, ...} if the filament is dead —
        does not raise. Returns {"ok": False, "error": "..."} without
        touching hardware if `millivolts` is outside the firmware's
        0.8-15 V clamp.

        verify=True: there's no current target to poll here (unlike
        idle_one/active_one's wait_for_current) — instead this polls the
        same no-I2C cached read until the board reports cc_mode==0
        (voltage), or `timeout_s` elapses, and merges that under
        result["heating"]:
            {"ok": bool, "cc_mode": int, "elapsed_s": float, "present": bool}
        cc_mode: 0=voltage (expected here), 1=current, 2/3=fault.
        """
        mv = int(millivolts)
        if mv < 800 or mv > 15000:
            return {"ok": False, "filament": int(filament),
                    "error": f"millivolts={mv} out of range 800-15000 (firmware clamps to 0.8-15 V)"}
        r = self._state_one(filament, VOLTAGE, mv, "voltage_one")
        if verify and not r.get("dead"):
            r = {**r, "heating": self._wait_for_voltage_mode(filament, timeout_s)}
        return r

    def _wait_for_voltage_mode(self, filament: int, timeout_s: float,
                               poll_interval_s: float = 0.2) -> dict:
        """Poll read_filament_currents() until cc_mode reports 0 (voltage),
        or timeout_s elapses. Same no-I2C cached read as wait_for_current();
        used by voltage_one(verify=True), which has no mA target to wait on."""
        start = time.monotonic()
        deadline = start + timeout_s
        data: dict = {}
        while True:
            data = self.read_filament_current_cached(filament).get(int(filament), {})
            cc_mode = int(data.get("cc_mode", -1))
            ok = cc_mode == 0
            if ok or time.monotonic() >= deadline:
                return {"ok": ok, "filament": int(filament), "cc_mode": cc_mode,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False))}
            time.sleep(poll_interval_s)

    # ── Board heating status (single-board, direct I2C) ──────────────────────
    # Distinct from read_filament_currents() above: that reads the CACHED
    # CC-loop current (no I2C, cheap, bulk). This reads the RP2350's own
    # last-commanded PowerState + fault kind for ONE board directly
    # (CH_GET_POWER_STATE, single-board I2C round-trip) — use it when you
    # need to know the actual state/fault, not just the measured current.

    def read_board_status(self, filament: int) -> dict:
        """Single-board heating status: last-commanded PowerState and fault
        kind for one filament, read directly from the RP2350 (not cached).

        Returns {"ok", "filament", "controller", "channel", "mux_port",
        "state": 1-6, "state_name": "STOP".."VOLTAGE",
        "fault": 0/1/2, "fault_name": "none"/"open"/"OCP/SCP"}.
        Never raises — check "ok".
        """
        r = self._get(f"/api/filament-status?filament={self._fid_of(filament)}")
        if r.get("ok"):
            r["state_name"] = _STATE_NAMES.get(r.get("state"))
            r["fault_name"] = _FAULT_NAMES.get(r.get("fault"))
        r["filament"] = int(filament)   # always echo back YOUR (USER_INDEX) number
        return r

    # ── OCP protection ─────────────────────────────────────────────────────────
    # Two DISTINCT, unrelated OCP mechanisms in this firmware:
    #   1. Per-board TPS55289 IOUT_LIMIT — the "real" steady-state OCP trip
    #      current for one board's HV supply. Settable per filament; no
    #      native batch opcode, so set_ocp_threshold_all() loops per-board.
    #   2. A GLOBAL per-controller two-stage floor (CH_STARTUP_OCP): a
    #      STARTUP threshold that tolerates the cold-inrush transient on
    #      turn-on, and a STEADY threshold applied ~2 s later. This is ONE
    #      pair of values for the whole controller (RP2350) — NOT per-board.
    # NOTE: the actual timing delay between STARTUP and STEADY (~2 s), and
    # the TPS55289's internal deglitch-bit settings, are compiled-in firmware
    # constants with no UART command to change them — only the two CURRENT
    # thresholds are settable, not the delay itself. If you need the delay
    # tunable, that requires a firmware change (a new UART opcode) first.

    def get_ocp_threshold_one(self, filament: int) -> dict:
        """Read back ONE board's currently configured TPS55289 steady-state
        OCP trip current (mA) — a real hardware register read, not just
        "whatever you last called set_ocp_threshold_one with" (catches a
        threshold set by another client, or one that predates this
        process). set_ocp_threshold_one/_all are SET-only in firmware
        (CH_SET_TPS_OCP_THRESHOLD 0x28 has no matching "get" opcode); this
        decodes the raw TPS55289 register instead (single-board I2C read
        — a fine one-off check, don't loop this to poll many boards).

        Returns {"ok", "filament", "controller", "channel", "mux_port",
        "enabled": bool, "threshold_ma": int}. `enabled` False means OCP
        protection is currently OFF for this board (threshold_ma reads 0
        in that case, not a real 0 mA trip point). Returns
        {"ok": False, "error": ...} if the filament has no board mapping.
        """
        r = self._get(f"/api/ocp-threshold?filament={self._fid_of(filament)}")
        r["filament"] = int(filament)   # always echo back YOUR (USER_INDEX) number
        return r

    def set_ocp_threshold_one(self, filament: int, threshold_ma: int) -> dict:
        """Set ONE board's TPS55289 steady-state OCP trip current (mA).

        No dead-mask guard here — OCP is a protection setting, not a
        heating/HV action, so it's not gated the same way; call this even
        on a filament you've marked dead if you specifically want to lower
        its trip point. Returns {"ok": False, "error": ...} if the filament
        has no board mapping.
        """
        r = self.set_ocp_threshold_all(filaments=[filament], threshold_ma=threshold_ma)
        # Confirm POSITIVELY that this filament was applied, rather than merely
        # checking it isn't in `failed`. A filament that was silently skipped --
        # unslotted, or on a controller that isn't connected -- appears in NEITHER
        # list, so the old absence-of-failure test returned ok:True for a
        # protection threshold that was never written.
        if int(filament) not in [int(x) for x in (r.get("applied") or [])]:
            reason = ("filament is dead-masked" if self._is_dead(filament)
                      else "no board mapping, or its controller is not connected")
            r = {**r, "ok": False,
                 "error": r.get("error") or
                          f"OCP threshold NOT written for filament {int(filament)} ({reason})"}
        return r

    def set_ocp_threshold_all(self,
                              filaments=None,      # None = every populated
                                                    # board. NOT dead-filtered --
                                                    # OCP is protection, see
                                                    # set_ocp_threshold_one
                              threshold_ma: int = 0) -> dict:  # per-board OCP
                                                                # trip current
        """Set the TPS55289 steady-state OCP trip current (mA) for a BATCH
        of filaments (all populated boards, or `filaments`). No native batch
        opcode exists for this — the backend loops one frame per board.

        Returns {"ok", "results": {controller: {...}}, "applied": [...], "failed": [...]}.
        """
        body: dict = {"threshold_ma": int(threshold_ma)}
        if filaments is not None:
            body["filaments"] = self._fids_of(filaments)
        return self._reindex_response(self._post("/api/ocp-threshold", body, timeout=30.0),
                                    keys=("applied", "failed", "excluded", "touched",
                                            "not_this_controller", "unslotted",
                                            "mismatched", "unstable"))

    def get_ocp_startup(self, controller: int = 1) -> dict:
        """Read the global per-controller two-stage OCP floor.

        Returns {"ok", "controller", "startup_ma", "steady_ma"}.
        """
        return self._get(f"/api/ocp-startup?controller={int(controller)}")

    def set_ocp_startup(self,
                        startup_ma: int,               # trip current (mA) that
                                                        # tolerates the cold-
                                                        # inrush transient right
                                                        # at turn-on
                        steady_ma: int | None = None,   # trip current (mA)
                                                         # applied ~2s later, once
                                                         # inrush has settled;
                                                         # None = leave unchanged
                        controller: int = 1) -> dict:   # 1 or 2 -- this is a
                                                         # WHOLE-CONTROLLER
                                                         # setting, not per-board
        """Set the global per-controller two-stage OCP floor (NOT per-board
        — see the note above). `steady_ma` is optional; omit to leave the
        steady threshold unchanged and only update the startup one.

        Returns {"ok", "controller", "startup_ma", "steady_ma"} (the values
        now in effect, read back from the same response).
        """
        body: dict = {"controller": int(controller), "startup_ma": int(startup_ma)}
        if steady_ma is not None:
            body["steady_ma"] = int(steady_ma)
        return self._post("/api/ocp-startup", body)

    # ── HV grid switch (ISO relay) — Force toggle ────────────────────────────

    def hv_grid_set(self, filament: int, on: bool, force: bool = True) -> dict:
        """Toggle ONE filament's HV isolation switch. Returns
        {"ok": False, "dead": True, ...} if the filament is dead — does
        not raise.

        force=True (default) uses writeMode=2 — bypasses the firmware's
        fault/verify checks, matching the GUI's "Force" checkbox. Use when
        the switch feedback is unreliable or the filament is known-shorted.
        force=False uses verify mode (writeMode=1) instead.

        Returns {"ok", "applied": [filament] or [], "failed": [...]}.
        """
        if self._is_dead(filament):
            return self._dead_result(filament)
        r = self._post("/api/hv-grid", {"filaments": [self._fid_of(filament)],
                                        "on": bool(on), "force": bool(force)})
        return self._reindex_response(r, keys=("applied", "failed", "excluded", "touched",
                                            "not_this_controller", "unslotted",
                                            "mismatched", "unstable"))

    def hv_grid_set_all(self,
                        filaments=None,     # None = every populated board
                                             # (minus dead mask)
                        on: bool = False,   # switch state to command
                        force: bool = True) -> dict:  # bypass firmware fault/
                                                       # verify checks (True) vs
                                                       # require verify (False)
        """Toggle the HV isolation switch for all (or listed) filaments.

        Dead-masked filaments are always stripped before the request is sent
        — see set_dead()/add_dead(). filaments=None targets every populated
        board (minus dead). force=True bypasses firmware fault/verify checks.

        Returns {"ok", "results": {controller: {...}}, "applied": [...], "failed": [...]}.
        """
        # dead_skipped is computed on USER_INDEX BEFORE _live() crosses to FID,
        # so it reads back in YOUR numbering. Without it, hv_grid_off_all([5]) with
        # 5 dead-masked returned a bare ok:True while the grid switch stayed ON --
        # a success-shaped no-op on an HV path.
        requested = None if filaments is None else [int(f) for f in filaments]
        dead_skipped = [] if requested is None else [f for f in requested if f in self.dead]
        live = self._live(filaments)   # already crossed to FID
        if live is not None and len(live) == 0:
            # ok:False -- the caller named filaments and NONE were commanded.
            return {"ok": False, "applied": [], "failed": [], "skipped_dead": True,
                    "dead_skipped": dead_skipped, "requested": requested,
                    "error": "every requested filament is dead-masked — nothing sent"}
        body: dict = {"on": bool(on), "force": bool(force)}
        if live is not None:
            body["filaments"] = live
        r = self._reindex_response(self._post("/api/hv-grid", body, timeout=20.0),
                                 keys=("applied", "failed", "excluded", "touched",
                                            "not_this_controller", "unslotted",
                                            "mismatched", "unstable"))
        if dead_skipped:
            r = {**r, "dead_skipped": dead_skipped}
        return r

    def hv_grid_off_all(self, filaments=None, force: bool = True) -> dict:
        """Convenience: turn OFF the HV isolation switch for all (or listed)
        filaments, excluding the dead mask. Same as hv_grid_set_all(on=False)."""
        return self.hv_grid_set_all(filaments, on=False, force=force)

    def hv_grid_status(self, controller: int = 1) -> dict:
        """Read per-filament HV-grid switch state on one controller.

        Returns {"ok", "filaments": {str(filament): {"desired": bool, "feedback": bool}}}.
        `desired` is the last commanded state; `feedback` is the switch's own
        sense line (a mismatch flags a stuck/dead switch).
        """
        r = self._get(f"/api/hv-grid-status?controller={controller}")
        # Backend replies keyed by FID (as a string) -- re-key to
        # USER_INDEX so this always matches YOUR numbering.
        if isinstance(r.get("filaments"), dict):
            r["filaments"] = {str(self._user_index_of(int(k))): v
                              for k, v in r["filaments"].items()}
        return r

    def hv_grid_clear_all(self) -> dict:
        """Instantly zero EVERY HV grid output on every connected controller.

        Uses the hardware 74HC595 /SRCLR clear pin (async shift-register
        clear) — bypasses the normal per-bit shift-and-latch write path
        entirely, so it is the fastest possible way to kill all HV grid
        outputs at once. This is a whole-chain hardware operation, not a
        per-filament one: the dead mask does NOT apply here — every
        channel is zeroed regardless of which filaments are marked dead.

        Use this as an emergency "kill everything now" — e.g. before
        walking away from the bench, or if a switch is behaving
        unexpectedly and you want a known-clean starting point. Also used
        internally by session()'s teardown.

        Returns {"ok", "results": {controller: {"ok": bool}}}.
        """
        return self._post("/api/disarm", {})

    # ── SHV run policy & HV bit-bang diagnostics ──────────────────────────────

    def get_fault_policy(self, controller: int = 1) -> dict:
        """Read the per-run fault policy: two INDEPENDENT stop/continue
        switches for a run that hits trouble.

        - "board": what to do on a CC/OCP hardware fault (0=stop the run,
          1=continue, logging it).
        - "mismatch": what to do on an HC165 shift-register read-back
          mismatch (0=stop, 1=continue).

        Also reports which boards actually faulted during the current/most
        recent run — the only record of that under a "continue" policy.

        Returns {"ok", "board": 0|1, "mismatch": 0|1, "mismatchCount": int,
        "faultedSlots": [raw slot ints, 8*channel+position],
        "faultedFilaments": [global filament indices, your USER_INDEX numbering]}.
        """
        r = self._shv(controller, {"op": "fault_policy"})
        if r.get("ok") and "faultedFilaments" in r:
            r["faultedFilaments"] = [self._user_index_of(f) for f in r["faultedFilaments"]]
        return r

    def set_fault_policy(self, controller: int = 1,
                         board: int | None = None,       # 0=stop the run,
                                                          # 1=continue, on a
                                                          # CC/OCP hardware fault
                         mismatch: int | None = None) -> dict:  # 0=stop, 1=continue,
                                                                  # on an HC165
                                                                  # read-back mismatch
        """Set one or both fault policies before arming a run. Omit either
        argument to leave that policy unchanged. Returns the same shape as
        get_fault_policy() (the values now in effect)."""
        body: dict = {"op": "fault_policy"}
        if board is not None:
            body["board"] = int(board)
        if mismatch is not None:
            body["mismatch"] = int(mismatch)
        r = self._shv(controller, body)
        if r.get("ok") and "faultedFilaments" in r:
            r["faultedFilaments"] = [self._user_index_of(f) for f in r["faultedFilaments"]]
        return r

    def get_trigger_delay(self, controller: int = 1) -> dict:
        """Read the SyncIn->fire trigger delay (µs) — a small, deliberate
        offset between the trigger edge and the RP2350 actually firing.

        Returns {"ok", "delayUs": int, "applies": bool}. `applies` is False
        when the live fire path (e.g. PIO precision mode) can't currently
        honour a nonzero delay — check it after a set(), since a value that
        "applies"=False for is silently not taking effect on real hardware.
        """
        return self._shv(controller, {"op": "trigger_delay"})

    def set_trigger_delay(self, delay_us: int, controller: int = 1) -> dict:
        """Set the SyncIn->fire trigger delay (µs); uint16, 0-65535.

        Returns {"ok", "delayUs": int, "applies": bool} — ALWAYS check
        "applies": a set that isn't honoured by the live fire path still
        returns "ok": True (the SETTING was stored) but "applies": False
        (it won't actually change when pulses fire) — this call can't
        silently do nothing without you being able to tell.
        """
        err = self._range_error("delay_us", int(delay_us), self._U16_MAX)
        if err:
            return {"ok": False, "error": err}
        return self._shv(controller, {"op": "trigger_delay", "delay_us": int(delay_us)})

    def read_hv_diag165(self, controller: int = 1,
                        channel: int = 0,          # 0-7, which HV channel's
                                                    # 165 shift register to test
                        test_byte: int = 0x55,     # bit pattern written and
                                                    # read back (bench diagnostic
                                                    # only — no HV needs to be on)
                        settle_ms: int = 5) -> dict:  # wait between write and
                                                        # read-back, 0-100
        """Raw HC165 shift-register readback diagnostic for one channel —
        writes `test_byte`, reads it back twice, and clears. Safe at any
        time (doesn't touch HV/heating) — a bench/signal-integrity check,
        not something you'd call during normal operation.

        Returns {"ok", "channel", "test_byte", "r0", "r1", "r2", "r3"}:
        r0 = baseline after clearAll, r1 = first read after the raw write,
        r2 = repeat read (repeatability check), r3 = read after final clear.
        All four should read back as `test_byte` (r0/r3 as 0) if the shift
        chain and cabling are healthy.
        """
        return self._post("/api/hv-diag165", {
            "controller": int(controller), "channel": int(channel),
            "test_byte": int(test_byte), "settle_ms": int(settle_ms),
        }, timeout=5.0)

    def set_hv_shift_hz(self, hz: int, controller: int = 1) -> dict:
        """Set the HC165 readback bit-bang SCK frequency (Hz) — for
        signal-integrity testing on long cables (e.g. drop it to 1 kHz to
        see a spike-free waveform on a scope). SET-ONLY: there is no
        separate "read current value" request — the firmware always
        requires a fresh value and echoes back the ACTUAL frequency now in
        effect (clamped 100 Hz-2 MHz, so what you asked for and what you
        get may differ slightly). Survives until the next reboot.

        Returns {"ok", "controller", "actualHz"}.
        """
        return self._post("/api/hv-shift-hz", {"controller": int(controller), "hz": int(hz)})

    # ── HV voltage / current set ──────────────────────────────────────────────

    def set_emission_v(self, volts: float) -> dict:
        """Set emission HV to |volts| V (output is negative).

        Backend loads the calibrated LUT, interpolates the DS3502 wiper, and
        writes it. Falls back to a linear approximation when no LUT is saved.
        Returns {"ok", "wiper", "expect_v", "method"}.
        """
        return self._post("/api/hv/set-v", {"chan": "emission", "volts": abs(volts)})

    def set_focus_v(self, volts: float) -> dict:
        """Set focus HV to |volts| V (output is negative).

        Returns {"ok", "wiper", "expect_v", "method"}.
        """
        return self._post("/api/hv/set-v", {"chan": "focus", "volts": abs(volts)})

    def set_emission_i(self, ma: float) -> dict:
        """Set emission current reference (0–85.7 mA). Linear DS3502 scale.

        Returns {"ok", "wiper", "expect_ma"}.
        """
        return self._post("/api/hv/set-i", {"ma": abs(ma)})

    # ── HV readback (ADS1115) ─────────────────────────────────────────────────

    def read_ads_all(self) -> dict:
        """Read all four ADS1115 channels.

        Keys on success: emiss_v (V), emiss_i_ma (mA), focus_v (V), ref_mv (mV),
        codes ([int×4] raw counts), mv ([float×4] pin voltages). On failure
        (e.g. STM32 unreachable — this happens; it's a live UART link, not a
        guarantee) returns {"ok": False, "error": ...} instead — check "ok"
        before indexing into this, or use the read_emission_v()/etc.
        wrappers below which already do that for you.
        """
        return self._get("/api/stm32/ads1115")

    def read_emission_v(self) -> float | None:
        """Measured emission voltage (V, negative). Returns None on failure
        (e.g. STM32 unreachable) — always check for None before using."""
        r = self.read_ads_all()
        return float(r["emiss_v"]) if r.get("ok") else None

    def read_emission_i(self) -> float | None:
        """Measured emission BEAM current (mA) — the ADS1115 reading on the
        shared emission bus. This is NOT a per-filament heating current;
        see read_filament_current()/read_filament_currents() for that.
        Returns None on failure — always check for None before using."""
        r = self.read_ads_all()
        return float(r["emiss_i_ma"]) if r.get("ok") else None

    def read_focus_v(self) -> float | None:
        """Measured focus voltage (V, negative). Returns None on failure —
        always check for None before using."""
        r = self.read_ads_all()
        return float(r["focus_v"]) if r.get("ok") else None

    # ── Filament heating current (CC loop, per-filament) ──────────────────────
    # Distinct from read_emission_i() above: that reads the shared emission
    # BEAM current off the ADS1115. These read each filament's own CATHODE
    # HEATING current from the CC loop (INA219, cached — no I2C, safe to poll
    # even mid-run) — use them to confirm idle_one()/active_one() actually
    # landed at the current you commanded.

    # ══ FILAMENT READS — WHICH ONE DO I WANT? ════════════════════════════════
    #
    #   I want to...                          | use
    #   --------------------------------------+-------------------------------
    #   check a filament reached its commanded | read_filament_current_cached()
    #   current, or watch currents WHILE a     |   (cheap, no I2C, run-safe)
    #   schedule is firing                     |
    #   --------------------------------------+-------------------------------
    #   know a filament's VOLTAGE, or get a    | read_filament_vi_live()
    #   matched V+I pair (e.g. to compute      |   (real I2C read; do NOT call
    #   resistance)                            |    while a schedule fires)
    #   --------------------------------------+-------------------------------
    #   ...just one filament, both values      | read_filament_vi_live(f)[f]
    #   --------------------------------------+-------------------------------
    #   ...just one filament, one number       | read_filament_current(f) /
    #                                          | read_filament_voltage(f)
    #
    # THE TRAP THIS TABLE EXISTS TO PREVENT: the cached read has NO voltage --
    # not "0 V", none at all, because the firmware command behind it carries no
    # voltage field. And the two single-value helpers read DIFFERENT sources, so
    # pairing them gives you V and I sampled by different commands at different
    # instants (measured 92 mA apart on a warming filament). For a pair, always
    # take BOTH from one read_filament_vi_live() entry.
    #
    # ── One shape, one failure convention ────────────────────────────────────
    # Every reader below takes `int | list | None` (None = all), returns
    # {filament: entry} with the SAME keys regardless of source, and encodes
    # "no reading" as None -- never as 0. A value this layer did not get from
    # hardware is absent, not zero; that rule is the one every bug in this file
    # has come down to.

    _ENTRY_KEYS = ("index", "present", "bus_mV", "current_mA", "target_mA",
                   "cc_mode", "source", "valid", "unavailable", "cached")

    @staticmethod
    def _want_filaments(filaments):
        """`int | list | None` -> `list[int] | None`. Accepting a bare int used
        to work on one reader and raise TypeError on its sibling."""
        if filaments is None:
            return None
        if isinstance(filaments, (int, float)) and not isinstance(filaments, bool):
            return [int(filaments)]
        return [int(f) for f in filaments]

    @staticmethod
    def _entry(raw: dict, user_index: int, source: str) -> dict:
        """Normalise one backend entry to the common shape.

        Fields the source cannot supply are None, not 0 -- the live read has no
        target_mA/cc_mode, the cached read has no bus_mV (its firmware command
        carries no voltage at all). `valid` says whether a usable measurement
        came back; a board that isn't present never reports numbers, because the
        INA sweep reports a board it cannot find as a tidy 0 mA."""
        present = bool(raw.get("present"))
        # Cached: current_mA is already None when stale/unavailable. Live: it is
        # 0 for an absent board, which is exactly the fabricated value this
        # normalisation exists to remove.
        mA = raw.get("current_mA")
        mV = raw.get("bus_mV")
        if not present:
            mA = mV = None
        valid = mA is not None or mV is not None
        return {"index": user_index, "present": present,
                "bus_mV": float(mV) if mV is not None else None,
                "current_mA": float(mA) if mA is not None else None,
                "target_mA": (float(raw["target_mA"])
                              if raw.get("target_mA") is not None else None),
                "cc_mode": raw.get("cc_mode"),
                "source": source, "valid": valid,
                # Always present on BOTH sources, so a caller never has to know
                # which one answered to know which keys exist. `cached` is not
                # redundant with source=="cached": the LIVE read falls back to
                # cached data while a schedule is firing (the I2C sweep would
                # stall pulses), so source=="live" with cached=True means "you
                # asked for a live read and did not get one".
                "unavailable": bool(raw.get("unavailable", not present)),
                "cached": bool(raw.get("cached", source == "cached"))}

    def read_filament_current_cached(self, filaments=None) -> dict:
        """USE THIS TO: confirm filaments reached their commanded current, and
        to watch heating current while a schedule is firing.

        CURRENT ONLY, from the CC loop's cache. SAFE while a schedule fires.

        No voltage: the firmware command behind this returns currents and
        nothing else, so `bus_mV` is always None here — use read_filament_vi_live()
        if you need a voltage. No I2C either, which is the point: this is the
        read you can poll at speed, and the only one that is safe to call while
        a schedule is firing (the live read does a mux select and would stall
        pulses).

        Read ONE filament or MANY with the same call.

        filaments:
            None          -> every populated filament (bulk/paged sweep)
            5             -> just filament 5   (SINGLE-board command)
            [5]           -> same as 5         (SINGLE-board command)
            [0, 1, 2]     -> those three       (bulk sweep, filtered)

        Single and bulk are DIFFERENT firmware commands, not the same read
        filtered two ways: one filament goes out as a single small frame to
        only that filament's controller (0x3A FLAG_SINGLE), which is what
        makes a per-filament poll like wait_for_current() cheap. Asking for
        several always uses the paged bulk sweep — looping the single read
        over many boards would flood the shared bridge link. The returned
        shape is identical either way, so you never branch on which ran.

        Not auto-filtered by the dead mask — this is a read, and you may
        still want to see a dead filament's last-known current.

        Returns {filament_index: {"current_mA", "target_mA", "present",
        "cc_mode"}}. cc_mode: 0=voltage, 1=current (Idle/Active), 2/3=fault.
        current_mA is None when the board's cached reading isn't a live
        measurement — guard with `is not None`, don't treat it as 0 mA.
        Returns {} on failure (never raises).

        NO VOLTAGE HERE. The underlying firmware command returns currents and
        nothing else — there is no bus-voltage field in its response — so
        `bus_mV` comes back None rather than a made-up number. Use
        read_filament_voltages() for a voltage; the live INA219 read is the only
        source that has one.
        """
        want = self._want_filaments(filaments)

        if want is not None and len(want) == 1:
            r = self._get(f"/api/filament-currents?filament={self._fid_of(want[0])}")
        else:
            r = self._get("/api/filament-currents")
        # The backend replies keyed by FID (it has no
        # concept of the client-side swap) -- re-key to USER_INDEX so the
        # result always matches YOUR numbering, then filter on that. Also
        # fix up the "index" field INSIDE each entry (a raw copy of the key,
        # left as FID by the backend) so it agrees with the outer key.
        raw = {int(k): v for k, v in (r.get("filaments") or {}).items()}
        out = {}
        for k, v in raw.items():
            user_index = self._user_index_of(k)
            out[user_index] = self._entry(v, user_index, "cached") if isinstance(v, dict) else v
        if want is not None:
            keep = set(want)
            out = {k: v for k, v in out.items() if k in keep}
        return out

    def read_filament_current(self, filament: int) -> float | None:
        """USE THIS TO: read one filament's heating current as a plain number,
        when you don't need to know WHY a read came back empty.

        Measured heating current (mA) for ONE filament. Returns **None** (not
        0.0) if the filament isn't present/regulated by the CC loop, OR if the
        read itself failed — arithmetic on the result then raises loudly
        instead of silently continuing with a fabricated zero. This still
        can't distinguish those two cases; use
        read_filament_current_cached() directly if you need to tell them apart.

        Reads the CC-loop CACHE. If you also want the voltage, do NOT pair this
        with read_filament_voltage() -- that one reads live INA219, so the two
        come from different commands at different instants. Use
        read_filament_vi_live(filament) for a matched pair -- it takes both
        from one conversion."""
        raw = self.read_filament_current_cached(filament).get(int(filament), {}).get("current_mA")
        return float(raw) if raw is not None else None

    def read_filament_vi_live(self, filaments=None) -> dict:
        """USE THIS TO: measure board voltage, or get matched V+I pairs (e.g.
        to compute resistance). NOT for polling during a run -- it does real
        I2C and would stall pulses; use read_filament_current_cached() there.

        VOLTAGE AND CURRENT, live from the INA219. NOT safe mid-run.

        This is the only source of a board voltage — the cached read has none.
        It does a real I2C mux sweep, so the backend refuses it while a schedule
        is firing (it would stall pulses) and answers from the cache instead;
        those entries come back "cached": True with no usable voltage. For a
        current you can poll safely at any time, use read_filament_current_cached().

        Bulk read of every populated filament's measured board voltage
        (mV) AND current (mA) together. Unlike read_filament_currents()
        (CC-loop CACHED currents, no I2C, safe mid-run), this is a real
        INA219 I2C sweep — the backend SKIPS it automatically while a
        schedule is firing (I2C would stall pulses) and falls back to the
        no-I2C cached current for that window: during a run, entries come
        back with "bus_mV": 0 and "cached": True — voltage genuinely isn't
        available then; call again after the run completes for a real
        reading.

        filaments: optional list to filter the result to just these indices.

        Returns {filament_index: {"bus_mV", "current_mA", "present",
        "cached"}}. Returns {} on failure (never raises).
        """
        # ?live=1 is REQUIRED for a voltage: /api/telemetry defaults to the
        # no-I2C cached read, which carries no bus_mV at all (every entry comes
        # back bus_mV=0, cached=True). Without this flag every filament here
        # reads 0 mV and read_filament_voltage() returns None for all of them.
        want = self._want_filaments(filaments)
        r = self._get("/api/telemetry?live=1")
        raw = {int(row["index"]): row for row in (r.get("telemetry") or [])
              if isinstance(row, dict) and "index" in row}
        out = {}
        for k, v in raw.items():
            user_index = self._user_index_of(k)
            out[user_index] = self._entry(v, user_index, "live")
        if want is not None:
            keep = set(want)
            out = {k: v for k, v in out.items() if k in keep}
        return out

    def read_filament_voltage(self, filament: int) -> float | None:
        """USE THIS TO: read one filament's board voltage as a plain number.
        If you also want its current, use read_filament_vi_live(filament)
        instead — it takes both from one conversion.

        Measured board voltage (mV) for ONE filament. Returns None if
        the filament isn't present, or if voltage isn't available right
        now (mid-run — see read_filament_vi_live()) — distinct from 0.0,
        which is a real (if unusual) reading. Use read_filament_vi_live()
        directly if you need present/cached separated from a genuine 0 mV.

        If you also want the current, use read_filament_vi_live(filament) rather than pairing
        this with read_filament_current(): that one reads the CC-loop cache, so
        the two values would come from different commands at different
        instants."""
        data = self.read_filament_vi_live([filament]).get(int(filament), {})
        if not data.get("present") or data.get("cached"):
            return None
        return float(data.get("bus_mV", 0))

    def read_filament_currents(self, filaments=None) -> dict:
        """Deprecated alias for read_filament_current_cached().

        Same behaviour, clearer name. Cached CC-loop CURRENT ONLY (no voltage
        exists in that firmware response), no I2C, safe to poll mid-run."""
        return self.read_filament_current_cached(filaments)

    def read_filament_voltages(self, filaments=None) -> dict:
        """Deprecated alias for read_filament_vi_live().

        Same behaviour, clearer name. LIVE INA219 read of voltage AND current;
        does I2C, so it is NOT safe to poll while a schedule is firing."""
        return self.read_filament_vi_live(filaments)

    # ── HV enable / disable ───────────────────────────────────────────────────

    def enable_emission(self, on: bool) -> dict:
        """Enable or disable the emission HV output."""
        return self._post("/api/stm32/hv-enable", {"ch": "emission", "on": on})

    def enable_focus(self, on: bool) -> dict:
        """Enable or disable the focus HV output."""
        return self._post("/api/stm32/hv-enable", {"ch": "focus", "on": on})

    def hv_status(self) -> dict:
        """HV pin states: {emission_on, focus_on, ads1115_alert, amc3301_diag}.
        On failure returns {"ok": False, ...} instead — those keys will be
        missing, so check "ok" before reading them."""
        return self._get("/api/stm32/hv-status")

    # ── SHV schedule — download (the real transfer path) ─────────────────────
    # This is the SAME reliable, pipelined download the GUI itself uses to get
    # a schedule onto the RP2350 — not a hand-assembled sequence of individual
    # SHV ops. It handles per-frame retries, currents caching, and CRC-checked
    # verification. ALWAYS use download()/verify_schedule() to load a schedule;
    # the individual shv_clear/shv_set_entry/etc. calls further below are raw
    # building blocks for advanced/custom sequences only — see the warning
    # on that section before reaching for them.

    def _plan_to_fids(self, plan: dict) -> tuple[dict, list]:
        """Translate a schedule plan's filament indices USER_INDEX -> FID and
        drop dead-masked entries. Returns (translated_plan, dead_skipped).

        This is the ONE place a plan crosses the USER_INDEX/FID boundary. It
        used to be nowhere: download()/verify_schedule() put plan indices on the
        wire raw while every other filament-taking method went through _fid_of(),
        so with a swap active `active_one(5)` heated FID 8 while a plan
        naming 5 scheduled FID 5 -- the schedule fired a different, unheated
        filament than the one just pre-heated. fire_single_pulse compensated by
        pre-translating its own plan; that compensation is now REMOVED (it would
        translate twice here, and a symmetric swap would map straight back to the
        original). Build plans in USER_INDEX; this crosses them to FID.

        Dead entries are dropped rather than sent, matching _live()/_prep(), and
        reported so the drop is never silent."""
        dead_skipped: list[int] = []
        out = dict(plan)
        for key in ("emission", "heating"):
            rows = plan.get(key)
            if not isinstance(rows, list):
                continue
            kept = []
            for row in rows:
                if not isinstance(row, dict) or "filament" not in row:
                    kept.append(row)
                    continue
                f = int(row["filament"])
                if f in self.dead:
                    if f not in dead_skipped:
                        dead_skipped.append(f)
                    continue
                kept.append({**row, "filament": self._fid_of(f)})
            out[key] = kept
        cur = plan.get("currents")
        if isinstance(cur, dict):
            kept_cur = {}
            for k, v in cur.items():
                f = int(k)
                if f in self.dead:
                    if f not in dead_skipped:
                        dead_skipped.append(f)
                    continue
                kept_cur[self._fid_of(f)] = v
            out["currents"] = kept_cur
        return out, sorted(dead_skipped)

    def download(self, plan: dict,       # {"config", "emission", "heating"?,
                                          # "currents"?} -- see the shape below
                timeout: float = 30.0) -> dict:  # generous default; a full
                                                  # multi-filament schedule with
                                                  # currents can take a while
        """Download a schedule plan to every connected controller.

        plan: {
            "config": {"interPulseMs", "maxOnMs", "totalMs", "triggerEdge"},
            "emission": [{"filament", "numPulses", "widthUs"}, ...],
            "heating": [{"filament", "triggerIndex", "state", "milliamps"}, ...],  # optional
            "currents": {filament: {"idle_mA", "active_mA"}},  # optional
        }

        The full emission list is sent to every connected controller — each
        RP2350 only fires the entries for filaments its own active-list map
        actually owns, so this is safe even when only one controller is
        involved. Downloads to both connected controllers if both are up.

        Plan filament indices are USER_INDEX (your numbering) -- they are crossed
        through filament_order and dead-filtered on the way out by _plan_to_fids().
        Any dead-masked entry is dropped and reported back as "dead_skipped";
        if that would leave nothing to fire, the download is refused outright
        rather than writing an empty emission table.

        Returns {"ok", "results": [...]} — one result dict per controller, plus
        "dead_skipped": [...] whenever the dead mask removed something.
        """
        wire_plan, dead_skipped = self._plan_to_fids(plan)
        if isinstance(plan.get("emission"), list) and plan["emission"] and not wire_plan["emission"]:
            return {"ok": False, "results": [], "dead_skipped": dead_skipped,
                    "error": "every emission entry is dead-masked — nothing to download"}
        r = self._post("/api/download", {"plan": wire_plan}, timeout=timeout)
        if dead_skipped:
            r = {**r, "dead_skipped": dead_skipped}
        # Keep fire_single_pulse(reuse=True)'s per-controller cache honest even
        # when download() is called directly (bypassing fire_single_pulse): a
        # successful write updates what we believe is on that controller now;
        # a failed one CLEARS both cache entries rather than leaving a stale
        # belief about a possibly-partial write. _last_crc is deliberately
        # NOT set here (only fire_single_pulse's own verify_schedule() call
        # populates it) — a direct download() with no matching verify leaves
        # reuse's crc check unable to confirm anything, which correctly
        # forces a full download on the next fire_single_pulse(reuse=True)
        # instead of trusting a crc we never actually observed.
        for row in (r.get("results") or []):
            cid = row.get("controller")
            if cid is None:
                continue
            if row.get("ok"):
                self._last_plan[cid + 1] = plan
            else:
                self._last_plan.pop(cid + 1, None)
            self._last_crc.pop(cid + 1, None)
        return r

    def verify_schedule(self, plan: dict) -> dict:
        """Read the emission/heat table counts + CRC back from every
        connected controller and confirm they match `plan`. Call this after
        download() and before arming, to catch a corrupted/partial transfer
        before firing anything.

        Takes the SAME USER_INDEX plan you gave download() -- it is crossed
        identically here (_plan_to_fids), so the CRC compared against the hardware
        is computed over the bytes that were actually written. Passing a plan
        that download() dead-filtered is fine: this filters it the same way.

        Returns {"ok", "results": {controller: {"match": bool, ...}}}.
        """
        wire_plan, _dead = self._plan_to_fids(plan)
        return self._post("/api/verify-schedule", {"plan": wire_plan}, timeout=10.0)

    # ── SHV schedule — low-level ──────────────────────────────────────────────
    # RAW single-op building blocks, useful for advanced/custom sequences (e.g.
    # tweaking just the timing config without a full re-download). For loading
    # an actual schedule, use download() above instead — it is the reliable,
    # retry-capable, CRC-verifiable path; calling clear/set_entries/set_config
    # by hand here bypasses all of that and is easy to get subtly wrong on a
    # slow or lossy link.
    #
    # WIRE-FORMAT LIMITS: these fields are fixed-width integers on the wire to
    # the RP2350 — passing a value outside its range doesn't get clamped or
    # rounded anywhere (backend.py, firmware); the backend's own int->bytes
    # encoding just raises OverflowError, which surfaces as a not-very-useful
    # {"ok": False, "error": "int too big to convert"}. The methods below
    # check first and return a clear, specific error instead.
    #   maxOnMs, widthUs        -> uint16: 0-65535 (ms / µs respectively)
    #   interPulseMs, totalMs   -> uint32: 0-4294967295 (ms) — effectively
    #                              unbounded for any real schedule
    #   numPulses                -> uint8: 0-255 — NOT validated here; the
    #                              backend silently truncates via `& 0xFF`
    #                              (e.g. 300 becomes 44) rather than
    #                              rejecting it, so keep this one <= 255
    #                              yourself.

    _U8_MAX = 255          # numPulses is a single byte on the wire
    _U16_MAX = 65535
    _U32_MAX = 4294967295

    def _range_error(self, name: str, value: int, max_value: int) -> str | None:
        """None if `value` fits [0, max_value]; else a ready-to-return error string."""
        if not (0 <= value <= max_value):
            return f"{name}={value} out of range (0-{max_value})"
        return None

    def _shv(self, controller: int, body: dict, timeout: float | None = None) -> dict:
        return self._post("/api/shv", {"controller": controller, **body}, timeout)

    def shv_clear(self, controller: int = 1) -> dict:
        return self._shv(controller, {"op": "clear_table"})

    def shv_push_active_list(self, controller: int = 1) -> dict:
        return self._shv(controller, {"op": "push_active_list"})

    def shv_set_entry(self, controller: int, filament: int,
                      num_pulses: int = 1,   # pulses in this entry's burst;
                                              # uint8 on the wire (0-255),
                                              # REJECTED here if out of range
                                              # (the backend encodes it &0xFF,
                                              # so 300 would silently become 44)
                      width_us: int = 1000) -> dict:  # pulse width (µs);
                                                       # uint16, 0-65535,
                                                       # rejected here if out
                                                       # of range
        err = (self._range_error("num_pulses", int(num_pulses), self._U8_MAX)
               or self._range_error("width_us", int(width_us), self._U16_MAX))
        if err:
            return {"ok": False, "error": err}
        return self._shv(controller, {
            "op": "set_entries",
            "entries": [{"filament": self._fid_of(filament),
                         "numPulses": int(num_pulses),
                         "width": int(width_us)}],
        })

    def shv_set_config(self, controller: int,
                       inter_pulse_ms: int = 3000,  # min gap between SyncIn
                                                     # edges (ms); uint32, huge
                                                     # legal range
                       max_on_ms: int = 40,          # arm-time safety gate
                                                      # (ms); uint16, 0-65535 --
                                                      # see fire_single_pulse's
                                                      # docstring for the full
                                                      # "checked at arm, not
                                                      # runtime" explanation
                       total_ms: int = 30000,  # FIRMWARE's own schedule
                                                # deadline (ms); uint32
                       trigger_edge: int = 0) -> dict:  # 0=rising, 1=falling —
                                                         # which SyncIn edge
                                                         # fires the schedule
        """Set the SHV schedule engine's timing config. All three timeouts
        are FIRMWARE-side (the RP2350 enforces them itself, independent of
        this Python process):

            inter_pulse_ms — a RUNTIME watchdog while Armed/Running: if no
                new SyncIn trigger arrives within this many ms of the last
                one, the RP2350 faults the WHOLE schedule
                (stopReason=InterPulseTimeout, shv_status()["stopReason"])
                — it does not skip/ignore the late trigger, the run stops.
                Default 30000 ms (30 s) is sized for slow manual bench
                triggers; tighten it once you're firing on a real cadence
                so a genuinely stuck run is caught quickly instead of
                sitting "Armed" for 30 s.
            max_on_ms — checked ONCE, at arm() time, against every entry's
                width_us already in the table: if ANY entry exceeds it,
                arm() itself is REJECTED (WidthTooLarge) and nothing fires
                at all — it is NOT a runtime cutoff that would truncate an
                in-flight pulse. Fix the offending entry or raise
                max_on_ms and re-arm.
            total_ms — the RP2350's own deadline for the WHOLE armed
                schedule, arm to last pulse; exceeding it faults the run
                (stopReason=TotalTimeout) independent of whether any Python
                process is even watching. This is a SEPARATE timeout from
                fire_single_pulse's `timeout_s` (that one lives in THIS
                process and only bounds how long it polls over HTTP before
                giving up) — see fire_single_pulse's docstring, "total_ms
                vs timeout_s", for the full two-clocks explanation and why
                timeout_s should be set a bit larger than total_ms/1000.
            trigger_edge — which SyncIn edge the schedule fires on. Match
                this to whatever actually drives SyncIn (the ESP32's own
                Sync I/O "Ext edge" setting, or an external source) — a
                mismatch means every real trigger is invisible to the
                engine and inter_pulse_ms's watchdog above will eventually
                fault the run with nothing having fired.

        max_on_ms is a firmware uint16 field — legal range 0-65535 ms
        (~65.5 s). Out-of-range values are rejected HERE with a clear
        {"ok": False, "error": ...}, before ever reaching the network."""
        err = (self._range_error("max_on_ms", int(max_on_ms), self._U16_MAX)
               or self._range_error("inter_pulse_ms", int(inter_pulse_ms), self._U32_MAX)
               or self._range_error("total_ms", int(total_ms), self._U32_MAX))
        if err:
            return {"ok": False, "error": err}
        return self._shv(controller, {
            "op": "set_config",
            "interPulseMs": int(inter_pulse_ms),
            "maxOnMs": int(max_on_ms),
            "totalMs": int(total_ms),
            "triggerEdge": 1 if int(trigger_edge) else 0,
        })

    def shv_arm(self,
               controller: int = 1,   # which RP2350's schedule engine to arm
               repeats: int = 1) -> dict:  # how many times to loop the whole
                                            # downloaded schedule before
                                            # auto-completing
        """Arm the SHV schedule. Returns immediately; firing waits for a
        SyncIn edge. Does NOT raise on rejection — check result["ok"]; a
        rejection includes result["reject"] (a numeric firmware reject
        code — e.g. IsoOff if the target filament's board isn't
        present/enabled).

        `repeats` — how many times the RP2350 auto-loops the WHOLE
        downloaded table before completing. The loop is seamless on the
        firmware side: it does NOT need a fresh external "start the next
        repeat" signal — SyncIn triggers keep driving individual PULSES as
        usual, and the engine itself re-stages entry 0 once the table's
        last entry finishes. `repeats=0` is treated the same as 1 (fires
        the table exactly once). `shv_status()["totalPulsesDone"]` counts
        across ALL repeats, not per-loop, so divide by the table's
        per-loop pulse count if you need to know which repeat you're in."""
        return self._shv(controller, {"op": "arm", "repeats": int(repeats)})

    def shv_disarm(self, controller: int = 1) -> dict:
        return self._shv(controller, {"op": "disarm"})

    def shv_status(self, controller: int = 1) -> dict:
        """SHV status: {state, filamentIndex, totalPulsesDone, elapsedMs, …}.
        Returns {} on failure (never raises)."""
        st = self._shv(controller, {"op": "status"}).get("status") or {}
        # filamentIndex/faultFilament come back as FID (0xFF/255 = none)
        # -- re-key to USER_INDEX so they match YOUR numbering.
        for key in ("filamentIndex", "faultFilament"):
            if key in st and st[key] not in (None, 0xFF, 255):
                st[key] = self._user_index_of(st[key])
        return st

    def shv_pulse_log(self,
                      controller: int = 1,   # which RP2350 to read the log from
                      start: int = 0) -> list[dict]:  # log index to start from
                                                       # (paginate through a
                                                       # long run's history)
        """Fired pulse records: [{filament, seq, tOnUs, durationUs, flags}, …].
        Returns [] on failure (never raises)."""
        records = self._shv(controller, {"op": "pulse_log",
                                         "start": int(start)}).get("records") or []
        for rec in records:
            if "filament" in rec:
                rec["filament"] = self._user_index_of(rec["filament"])
        return records

    # ── SyncIn simulate (ESP32-generated trigger pulses) ──────────────────────
    # fire_single_pulse(trigger="sim") uses this internally for a single burst.
    # These standalone methods let you start/stop/monitor a simulated SyncIn
    # train directly — e.g. to drive a longer/independent test sequence, or to
    # generate the SyncIn edges for a hand-built low-level SHV sequence.

    def simulate_sync(self,
                      count: int = 1,                # how many SyncIn edges
                                                       # to generate
                      interval_ms: float | None = None,  # fixed gap between
                                                          # pulses -- pick
                                                          # EITHER this OR
                                                          # duration_s, not both
                      duration_s: float | None = None,   # spread `count` pulses
                                                          # evenly across this
                                                          # many seconds instead
                      controller: int = 1,            # which RP2350 fires the
                                                       # edge (chain propagates
                                                       # it onward if chained)
                      expect=None,                    # optional [filament, ...]
                                                       # to seed the run-report's
                                                       # expected-filament
                                                       # tracking, like a real scan
                      active_ma: int = 2900) -> dict:  # paired with `expect` --
                                                        # the ACTIVE current the
                                                        # run-report expects
        """Start the ESP32 generating `count` SyncIn pulses.

        Fires from the head-of-chain controller (`controller`); the RP2350
        chain propagates the edge to the other power unit if chained.

        Pass EITHER `interval_ms` (fixed gap between pulses) OR `duration_s`
        (spread `count` pulses evenly across this many seconds) — not both.
        If neither is given, pulses fire back-to-back with no delay.

        `expect` (optional list of filament indices) and `active_ma` seed
        the run-report's expected-filament tracking, same as a real scan.

        Does not raise — if a simulation is already running, returns
        {"ok": False, "error": "..."}; call simulate_sync_stop() first.

        Returns {"ok", "count", "interval_ms", "controller"}.
        """
        body: dict = {"count": int(count), "controller": int(controller),
                     "active_mA": int(active_ma)}
        if interval_ms is not None:
            body["interval_ms"] = float(interval_ms)
        elif duration_s is not None:
            body["duration_s"] = float(duration_s)
        if expect is not None:
            body["expect"] = list(expect)
        return self._post("/api/sync/simulate", body, timeout=10.0)

    def simulate_sync_stop(self) -> dict:
        """Stop an in-progress simulated SyncIn train early. Safe to call
        even if nothing is running (no-op)."""
        return self._post("/api/sync/simulate-stop", {})

    def simulate_sync_status(self) -> dict:
        """Read simulate-sync progress.

        Returns {"ok", "running": bool, "fired": int, "count": int,
        "stop": bool, "controller": int|None}.
        """
        return self._get("/api/sync/simulate-status")

    # ── SHV schedule — single-filament pulse (high-level) ────────────────────

    def fire_single_pulse(
        self,
        filament: int,
        num_pulses: int = 1,
        width_us: int = 1000,
        inter_pulse_ms: int = 3000,
        max_on_ms: int = 40,
        total_ms: int = 15000,       # RP2350 FIRMWARE's own schedule timeout (ms)
                                      # — see docstring, "total_ms vs timeout_s"
        controller: int | None = None,   # None = auto-infer from `filament` via
                                          # the active-list mapping — see docstring,
                                          # "why controller exists at all"
        trigger: str = "sim",
        timeout_s: float = 15.0,     # PYTHON CLIENT's polling timeout (seconds)
                                      # — see docstring, "total_ms vs timeout_s"
        verify: bool = True,
        reuse: bool = False,   # skip re-download if unchanged since your last
                                # call — see docstring, "reuse — skipping the
                                # download when nothing changed"; OFF by
                                # default because it has a real, documented
                                # safety gap (see the docstring) — opt in only
                                # when you understand it.
        measure: bool = False,   # ALSO measure the HV current of each
                                  # pulse on the STM32 detector -- see
                                  # "measure" in the docstring
        rate_hz: int = 1000000,  # detector ADC sample rate; only used
                                  # when measure=True
        post_bg_gap_us: float | None = None,  # measure=True only: wait this long
                                               # after the pulse ends before
                                               # sampling the post-pulse level
        post_bg_n_us: float | None = None,    # measure=True only: then average
                                               # over this long. None = firmware
                                               # default (50 us / 50 us)
    ) -> dict:
        """Download a one-entry schedule, arm it, fire, and verify.

        Internally: disarm -> download() (the real reliable transfer path,
        same one the GUI uses) -> verify_schedule() -> arm -> trigger -> poll.
        Does NOT raise at any step — every failure mode (dead filament,
        download failure, verify mismatch, arm rejection, SHV fault, poll
        timeout) comes back as {"ok": False, "error": "...", ...}; check
        "ok" yourself. Best-effort disarms the schedule before returning on
        any failure path, so a failed fire doesn't leave it armed.

        `total_ms` vs `timeout_s` — two DIFFERENT timeouts, on two DIFFERENT
        machines, watching two DIFFERENT things:

            total_ms   -> lives on the RP2350. Downloaded as part of the
                          schedule config. The FIRMWARE's own deadline for
                          the whole armed schedule (from arm to the last
                          pulse) — if exceeded, the RP2350 itself declares
                          the schedule timed out/faulted, independent of
                          whether Python is even still watching.
            timeout_s  -> lives in THIS Python process. How long the local
                          while-loop below keeps polling shv_status() over
                          HTTP before giving up and returning
                          {"ok": False, "timeout": True, ...} on its own —
                          independent of what the RP2350 is doing. Python
                          could give up on a schedule that's still running
                          fine on the hardware, or keep polling a schedule
                          the RP2350 already abandoned.

        Rule of thumb: timeout_s should be a bit LARGER than total_ms/1000,
        so Python doesn't give up right before the firmware would have
        reported COMPLETE/FAULT on its own — e.g. total_ms=40000 (40 s)
        pairs with timeout_s=45.0, not timeout_s=15.0 (the default, sized
        for total_ms's own 15000 ms default).

        Why `controller` exists at all, and why it's separate from
        `filament`: every OTHER single-filament method in this client
        (active_one, idle_one, hv_grid_set, ...) takes ONLY a filament index
        — the backend resolves which controller/board that filament lives
        on via the active-list mapping (see filament_to_board()) and routes
        the command there for you. fire_single_pulse can't be fully
        filament-scoped the same way, because `download()`/`shv_arm()`/
        `shv_disarm()`/`shv_status()`/`shv_pulse_log()`/`simulate_sync()`
        are commands to a CONTROLLER'S SCHEDULE ENGINE (one whole RP2350's
        armed/running state), not to one board — a controller's schedule
        can hold entries for many filaments at once, and arming/triggering
        operates on the ENGINE, not a single filament within it. For this
        single-filament convenience wrapper, "which engine to arm" is
        almost always just "whichever controller owns this filament" — so
        `controller=None` (the default) resolves it automatically via
        filament_to_board(). Pass an explicit `controller` only if you need
        to override that (e.g. deliberately targeting a different chained
        controller for some reason). Passing the wrong explicit controller
        here is a real footgun: the schedule downloads fine either way
        (download() always reaches every connected controller), but arming
        the WRONG controller's engine means the RIGHT controller — the one
        that actually owns the board — never gets triggered, and the pulse
        silently never fires.

        `reuse` — skipping the download when nothing changed: downloading
        the schedule (active-list, mask, config, emission table) costs
        several UART round-trips (~6 frames) every single call, even when
        you're firing the SAME filament with the SAME pulse settings
        repeatedly (measured ~65x slower than a reused call). `reuse=True`
        skips straight to a cheap verify_schedule() check (~2 frames, no
        data transfer) instead of a full re-download WHEN both of these
        hold:
            1. This exact plan (filament/num_pulses/width_us/timing config)
               is the last one THIS CLIENT successfully downloaded to this
               controller (tracked in-memory per CTClient instance) — a
               cheap LOCAL pre-filter, no network call needed to fail this.
            2. A FRESH verify_schedule() call's returned table CRC (the
               firmware-computed checksum from ShvGetTableInfo) still
               matches the CRC we recorded right after OUR OWN last
               successful write. This is REAL content verification, not
               just an entry-count check — verify_schedule()'s own "match"
               field only compares counts (no CRC in that logic), which
               would miss a different actor overwriting the table with a
               same-sized-but-different schedule; comparing the CRC
               ourselves closes that gap without needing to know the
               RP2350's CRC algorithm.
        If EITHER check fails, it falls back to a full download()+verify()
        automatically — reuse never trades correctness for speed, only
        skips work when it's confident enough to, and self-heals the next
        time it's called.

        Still recommended: hold the lease for the whole sequence (e.g.
        `with ct.lease():` around repeated fire_single_pulse(reuse=True)
        calls) so writes from OTHER clients are blocked outright at the
        backend level — the CRC check is a strong second line of defense,
        not a replacement for that.

        Args:
            filament:       0–95 global filament index.
            num_pulses:     pulses in the burst.
            width_us:       pulse width (µs).
            inter_pulse_ms: minimum gap between SyncIn edges (ms) — a
                            RUNTIME watchdog: no trigger within this long
                            faults the WHOLE run (stopReason=
                            InterPulseTimeout), it does not skip a late one.
            max_on_ms:      firmware safety guard, checked ONCE at arm()
                            against width_us — NOT a runtime cutoff. If
                            width_us exceeds it, arm() itself is rejected
                            (WidthTooLarge) and nothing fires; it never
                            truncates an in-flight pulse.
            total_ms:       FIRMWARE schedule timeout (ms) — see above.
            controller:     1, 2, or None (default) to auto-infer from
                            `filament` via the active-list mapping — see
                            "why controller exists at all" above.
            trigger:        "sim" — ESP32 generates SyncIn pulse(s);
                            "ext" — caller supplies the external SyncIn edge.
                            Either way this fires on the RISING edge —
                            triggerEdge is hardcoded 0 here (unlike
                            shv_set_config's `trigger_edge`, which this
                            convenience wrapper doesn't expose). Use
                            download()+shv_set_config(trigger_edge=1)+
                            shv_arm() directly if you need falling-edge.
            timeout_s:      CLIENT polling timeout (seconds) — see above.
            verify:         confirm the downloaded table's entry count/CRC
                            match before arming (recommended; costs one
                            extra round-trip). Ignored (always effectively
                            True) when `reuse` actually skips the download,
                            since that path already runs verify_schedule()
                            as its own gate.
            reuse:          skip re-download when unchanged — see above.
                            Default False; opt in only under a held lease.

        Returns:
            {"ok": bool, "fired": int, "records": [...], "status": {...},
             "error": str}   # "error" present only when "ok" is False


        measure=True -- ALSO measure the current of every pulse
        ------------------------------------------------------
        Firing and measuring are two different subsystems: the RP2350
        decides WHEN HV fires, and a pulse detector on the STM32 measures
        how much current actually flowed. This flag runs both as one
        operation, which is almost always what you want -- a fired pulse
        you did not measure tells you very little.

            r = ct.fire_single_pulse(5, num_pulses=3, width_us=1000,
                                     measure=True)
            if r["ok"]:
                for e in r["measured"]:
                    print(e["peak_ma"], "mA peak,", e["plateau_ma"], "mA plateau")

        It arms the detector, notes where the event stream is, fires,
        collects exactly the events this fire produced, and releases the
        detector again -- including if the fire raises or times out.

        POST-PULSE BACKGROUND. Each event's post_bg is measured by waiting
        post_bg_gap_us after the envelope ends (for the analog front end to
        settle) and then averaging over post_bg_n_us. Both default to the
        firmware's 50 us / 50 us; pass your own when that doesn't fit this
        board. If post_bg comes back looking like the tail of the pulse rather
        than a settled level, the gap is too short. Setting post_bg_n_us=0
        disables the measurement, and post_bg then reports None -- NOT 0, which
        would be a legal post-pulse current.

            r = ct.fire_single_pulse(5, num_pulses=3, width_us=1000,
                                     measure=True,
                                     post_bg_gap_us=200,   # let it settle longer
                                     post_bg_n_us=100)

        With measure=True the result gains:
            "measured": [ ... ]   one event per pulse, each with peak_ma /
                                  plateau_ma / bg_ma (see pulse_events_ma)
            "ref_mv":    float    the live reference reading actually used
        and "ok" becomes stricter: it is True only if the fire succeeded AND
        every fired pulse produced a measured event. A fire that "worked"
        while the detector saw nothing -- link down, detector not really
        armed, events dropped -- reports ok=False rather than letting a
        silent measurement gap look like success.

        Requires the STM32 link to be up; there is exactly ONE detector and
        it lives on the master controller. If it cannot be armed, nothing is
        fired at all and the error says so, so measure=True never leaves you
        guessing whether HV went out.
        """
        if not measure:
            return self._fire_core(
                filament, num_pulses=num_pulses, width_us=width_us,
                inter_pulse_ms=inter_pulse_ms, max_on_ms=max_on_ms,
                total_ms=total_ms, controller=controller, trigger=trigger,
                timeout_s=timeout_s, verify=verify, reuse=reuse)

        # Arm BEFORE firing -- a detector armed afterwards has already missed
        # the pulses. If it can't arm we fire nothing: silently firing HV that
        # nobody is measuring is the opposite of what measure=True asked for.
        #
        # This arms the RELAY, not just the detector. The STM32 times each pulse
        # from the real envelope on its PA4 pin, and PA4 only moves while the
        # ESP32 is mirroring the RP2350's pulse signal onto it. pulse_arm() alone
        # arms the detector and leaves the relay off, so the detector sits there
        # sampling and never sees a pulse start: measured on hardware, a 3-pulse
        # fire came back "0 of 3 measured" while the RP2350 fired correctly and
        # the STM32 took 7.2M samples. With the relay armed the same fire gives
        # 3 events whose measured widths (1009/1002/1000 us) match the commanded
        # 1000 us. See pulse_arm()'s note for the detector-only form.
        arm = self.ready_arm(rate_hz, post_bg_gap_us=post_bg_gap_us,
                             post_bg_n_us=post_bg_n_us)
        if not arm.get("ok"):
            return {"ok": False, "fired": 0, "records": [], "status": {},
                    "measured": [], "ref_mv": None,
                    "error": f"detector arm failed, nothing fired: {arm.get('error')}"}
        try:
            # "Huge since" returns no events but a true current cursor, so we
            # collect only what THIS fire produces and never a stale backlog.
            since = self.pulse_events(2_000_000_000).get("last_id", 0)
            fired = self._fire_core(
                filament, num_pulses=num_pulses, width_us=width_us,
                inter_pulse_ms=inter_pulse_ms, max_on_ms=max_on_ms,
                total_ms=total_ms, controller=controller, trigger=trigger,
                timeout_s=timeout_s, verify=verify, reuse=reuse)
            measured, ref_mv = self._collect_pulse_events(since, int(num_pulses))
            out = {**fired, "measured": measured, "ref_mv": ref_mv,
                   "ok": bool(fired.get("ok")) and len(measured) >= int(num_pulses)}
            if not out["ok"] and fired.get("ok") and not out.get("error"):
                out["error"] = (f"fired {fired.get('fired')} pulse(s) but the detector "
                                f"reported {len(measured)} of {num_pulses} — measurement "
                                f"incomplete, so the result is not trustworthy")
            return out
        finally:
            self.ready_disarm()

    def _collect_pulse_events(self, since: int, want: int,
                              grace_s: float = 3.0) -> tuple[list, float | None]:
        """Collect `want` detector events newer than `since`. The detector is
        real-time, so these normally arrive immediately after the fire returns;
        the grace period is for a delayed/dropped event, not expected lag.
        Returns whatever it got -- the CALLER decides that a short count is a
        failure, so this can't quietly paper over one."""
        measured: list = []
        ref_mv = None
        deadline = time.monotonic() + grace_s
        cursor = since
        while len(measured) < want and time.monotonic() < deadline:
            ev = self.pulse_events_ma(cursor)
            if ev.get("ok"):
                ref_mv = ev.get("ref_mv", ref_mv)
                if ev.get("events"):
                    measured.extend(ev["events"])
                    cursor = ev["events"][-1]["id"]
            if len(measured) < want:
                time.sleep(0.05)
        return measured, ref_mv

    def _fire_core(
        self,
        filament: int,
        num_pulses: int = 1,
        width_us: int = 1000,
        inter_pulse_ms: int = 3000,
        max_on_ms: int = 40,
        total_ms: int = 15000,       # RP2350 FIRMWARE's own schedule timeout (ms)
                                      # — see docstring, "total_ms vs timeout_s"
        controller: int | None = None,   # None = auto-infer from `filament` via
                                          # the active-list mapping — see docstring,
                                          # "why controller exists at all"
        trigger: str = "sim",
        timeout_s: float = 15.0,     # PYTHON CLIENT's polling timeout (seconds)
                                      # — see docstring, "total_ms vs timeout_s"
        verify: bool = True,
        reuse: bool = False,   # skip re-download if unchanged since your last
                                # call — see docstring, "reuse — skipping the
                                # download when nothing changed"; OFF by
                                # default because it has a real, documented
                                # safety gap (see the docstring) — opt in only
                                # when you understand it.
    ) -> dict:
        """Fire one schedule entry and wait for it. The body of
        fire_single_pulse() -- see that method for the full contract;
        this exists only so the public method can wrap it with the
        detector arm/correlate step without duplicating any of it."""
        if self._is_dead(filament):
            return self._dead_result(filament, {"fired": 0, "records": [], "status": {}})

        if controller is None:
            board = self.filament_to_board(filament)
            if board is None:
                return {"ok": False,
                        "error": f"filament {filament} has no board (unassigned or "
                                f"overflowed past the usable slots) — can't infer "
                                f"controller; pass one explicitly if you meant this",
                        "fired": 0, "records": [], "status": {}}
            controller = board["controller"]

        # max_on_ms/width_us are uint16 fields (0-65535), inter_pulse_ms/
        # total_ms are uint32 — see the wire-format note above shv_set_config.
        err = (self._range_error("num_pulses", int(num_pulses), self._U8_MAX)
               or self._range_error("max_on_ms", int(max_on_ms), self._U16_MAX)
               or self._range_error("width_us", int(width_us), self._U16_MAX)
               or self._range_error("inter_pulse_ms", int(inter_pulse_ms), self._U32_MAX)
               or self._range_error("total_ms", int(total_ms), self._U32_MAX))
        if err:
            return {"ok": False, "error": err, "fired": 0, "records": [], "status": {}}

        self.shv_disarm(controller)   # cheap (1 frame); resets engine state to
                                       # Idle WITHOUT touching the schedule table
                                       # — safe to call unconditionally even when
                                       # reuse is about to skip the download.

        plan = {
            # With trigger="sim" THIS CLIENT drives both the trigger and the
            # watchdog that fires if a trigger is late. Setting them to the same
            # value makes the simulated edge race its own deadline: measured on
            # hardware, inter_pulse_ms=300 lost that race on every attempt (2, 3
            # and 4 pulses all faulted with stopReason=inter-pulse timeout after
            # exactly one pulse) while 200 and 500 passed every time. The spacing
            # is the physically meaningful number, so keep the sim at what the
            # caller asked for and give the WATCHDOG room instead. An external
            # trigger is the caller's to time, so it keeps the value as given.
            "config": {"interPulseMs": (int(inter_pulse_ms) * 2 + 100
                                        if trigger == "sim" else int(inter_pulse_ms)),
                       "maxOnMs": int(max_on_ms),
                       "totalMs": int(total_ms), "triggerEdge": 0},
            # USER_INDEX here -- download()/verify_schedule() cross to FID
            # via _plan_to_fids(). This used to call _fid_of() itself, back
            # when download() sent plans raw; doing both would translate twice
            # (a symmetric swap maps straight back to the original). Keep the
            # plan in USER_INDEX so _last_plan's reuse comparison is too, and
            # so shv_pulse_log()'s re-keying still lines up with the
            # "fired"/"records" filter below.
            "emission": [{"filament": int(filament), "numPulses": int(num_pulses),
                         "widthUs": int(width_us)}],
            "heating": [],
        }

        skip_download = False
        if reuse and self._last_plan.get(controller) == plan and controller in self._last_crc:
            # Cheap local pre-filter passed (plan unchanged from what WE last
            # wrote) — now confirm against the ACTUAL hardware CRC, not just
            # entry count, so a different actor's same-size schedule can't
            # slip past undetected. See fire_single_pulse's docstring.
            v = self.verify_schedule(plan)
            row = (v.get("results") or {}).get(str(controller)) or {}
            if (row.get("match") and row.get("crc") is not None
                    and row.get("crc") == self._last_crc.get(controller)):
                skip_download = True   # content confirmed byte-identical — go straight to arm

        if not skip_download:
            dl = self.download(plan)   # updates self._last_plan[controller]; clears any stale crc
            if not dl.get("ok"):
                return {"ok": False, "error": f"download failed: {dl.get('error', dl)}",
                        "fired": 0, "records": [], "status": {}}

            # Always verify when reuse is requested — even if verify=False —
            # since reuse's whole safety mechanism depends on having a fresh
            # CRC baseline to compare against on the NEXT call.
            if verify or reuse:
                v = self.verify_schedule(plan)
                if not v.get("ok"):
                    return {"ok": False, "error": f"schedule verify mismatch after download: {v}",
                            "fired": 0, "records": [], "status": {}}
                if reuse:
                    row = (v.get("results") or {}).get(str(controller)) or {}
                    if row.get("crc") is not None:
                        self._last_crc[controller] = row["crc"]

        arm_r = self.shv_arm(controller, repeats=1)
        if not arm_r.get("ok"):
            return {"ok": False, "error": f"arm rejected (code {arm_r.get('reject')}) — "
                                          "check active list and filament power states",
                    "fired": 0, "records": [], "status": {}}

        if trigger == "sim":
            r = self._post("/api/sync/simulate", {
                "count": int(num_pulses),
                "interval_ms": float(max(inter_pulse_ms, 10)),
                "controller": int(controller),
            }, timeout=10.0)
            if not r.get("ok"):
                self.shv_disarm(controller)
                return {"ok": False, "error": f"could not start SyncIn simulation: "
                                              f"{r.get('error', r)}",
                        "fired": 0, "records": [], "status": {}}

        deadline = time.monotonic() + timeout_s
        state = SHV_IDLE
        while time.monotonic() < deadline:
            st = self.shv_status(controller)
            state = st.get("state", SHV_IDLE)
            if state == SHV_FAULT:
                return {"ok": False,
                        "error": f"SHV fault on controller {controller}: "
                                f"filament {st.get('faultFilament')}, reason "
                                f"{self._SHV_STOP_REASON_NAMES.get(st.get('stopReason'), st.get('stopReason'))}"
                                f" ({st.get('stopReason')})",
                        "fired": 0, "records": [], "status": st}
            if state == SHV_COMPLETE:
                logs = self.shv_pulse_log(controller)
                fired = [r for r in logs if r.get("filament") == filament]
                return {"ok": bool(fired), "fired": len(fired),
                        "records": fired, "status": st}
            time.sleep(0.05)

        self.shv_disarm(controller)
        return {"ok": False, "timeout": True,
                "error": f"timed out after {timeout_s} s (state={state})",
                "fired": 0, "records": [], "status": {}}

    # ── STM32 per-pulse HV CURRENT measurement ──────────────────────────────
    # Everything above (fire_single_pulse, hv_grid_set, ...) controls WHEN/
    # WHICH HV switch fires. None of it tells you how much current actually
    # flowed -- that's a SEPARATE measurement subsystem: the STM32G431 sitting
    # on the MASTER controller's link runs a hardware pulse_detector that
    # measures every REAL rise/fall edge on the emission-current line directly
    # (no amplitude threshold, no guessed timing) once armed. There is exactly
    # ONE detector, on the master -- no `controller` parameter on any of these,
    # unlike every board/HV method above.
    #
    # Shared with the GUI: pulse_arm()/pulse_disarm() hit the SAME
    # /api/adc/pulse-arm|disarm endpoints as the GUI's "Stream" button and
    # "Record measurement" card, which backend.py reference-counts
    # server-side -- arming here while a GUI tab already has Stream or Record
    # running just JOINS that arm (doesn't reset it, and your requested
    # rate_hz is ignored if you're not the first arm-er); disarming here only
    # actually disarms the hardware once nothing else still wants it armed.
    # Safe to run this script alongside an open GUI tab; just don't assume
    # you got the rate_hz you asked for if something else armed it first.

    def ready_arm(self, rate_hz: int = 1000000, n_samples: int = 2000,
                  post_bg_gap_us: float | None = None,
                  post_bg_n_us: float | None = None) -> dict:
        """Arm the pulse-envelope RELAY plus the STM32 detector inside it.

        post_bg_gap_us / post_bg_n_us tune the POST-PULSE background window: the
        STM32 waits `gap` after the envelope ends for the signal to settle, then
        averages `n` to produce each event's post_bg. Both are in MICROSECONDS
        here (converted to samples at rate_hz on the way out). Leave them None
        to use the firmware defaults (50 us / 50 us). The right gap depends on
        how long this board's analog front end takes to settle -- if post_bg
        still looks like the tail of the pulse rather than a settled level,
        raise the gap. post_bg_n_us=0 turns the measurement off, and events then
        report post_bg as None rather than 0.


        This is the one you want when you intend to MEASURE fired pulses, and it
        is what fire_single_pulse(measure=True) uses. The STM32 times each pulse
        from the real edge on its PA4 pin; that pin is driven by the ESP32
        mirroring the RP2350's own pulse-envelope output. Arm only the detector
        (pulse_arm) and PA4 never moves, so a fire measures nothing at all --
        the detector reports zero events while everything else looks healthy.
        """
        body: dict = {"rate": int(rate_hz), "n_samples": int(n_samples)}
        # The wire wants SAMPLES; callers here think in microseconds like every
        # other timing argument in this client, so convert at the boundary using
        # the rate actually being armed. Omitted (not 0) when unspecified -- 0 is
        # a real value on the wire meaning "do not measure the post-pulse
        # background at all", so it must not double as "caller said nothing".
        for key, us in (("post_bg_gap", post_bg_gap_us), ("post_bg_n", post_bg_n_us)):
            if us is not None:
                body[key] = max(0, int(round(float(us) * rate_hz / 1_000_000)))
        return self._post("/api/adc/ready-arm", body)

    def ready_disarm(self) -> dict:
        """Stop relaying pulse envelopes and release the STM32 CS claim."""
        return self._post("/api/adc/ready-disarm", {})

    def pulse_arm(self, rate_hz: int = 1000000) -> dict:
        """Arm the STM32 per-pulse current detector. Nothing is measured
        until pulses actually fire -- this just gets the STM32's ADC
        streaming and its hardware edge-triggered detector armed and
        waiting. Reference-counted with the GUI's Stream/Record (see the
        section comment above) -- safe to call even if a GUI tab already
        has one of those running; check result["shared"]/["other_users"]
        if you need to know whether you got your own arm or joined one."""
        return self._post("/api/adc/pulse-arm", {"rate": int(rate_hz)}, timeout=10.0)

    def pulse_disarm(self) -> dict:
        """Release this script's claim on the shared detector arm. Only
        actually disarms the STM32 if the GUI isn't also using it right
        now (see the section comment above) -- a result with
        {"shared": True, "still_armed_for": [...]} means it's still armed
        for someone else, which is NOT an error, just information."""
        return self._post("/api/adc/pulse-disarm", {}, timeout=5.0)

    def pulse_events(self, since: int = 0) -> dict:
        """Poll new STM32-measured pulse events with id > `since`.

        Each event:
            "id"        monotonic event id (use as the next `since`)
            "t_us"      STM32 sample index at the pulse start (R). Free-running
                        since boot, so it WRAPS every 2**32 samples -- about
                        71.6 minutes at 1 MSPS. Differencing two of these
                        across a wrap gives nonsense: order by "id", and use
                        "recv_ms" for wall-clock.
            "on_us"     MEASURED pulse width, from the real envelope on the
                        STM32's PA4 pin -- not the commanded width. Compare it
                        against what you asked for; they should agree closely.
            "peak"      highest raw code inside the pulse. **None when not
                        measured** (empty envelope), on firmware with the
                        measure_flags capability. Older firmware reports the
                        field's initial 0 there, which converts to a confident
                        ~-32 mA -- so "empty_envelope" below, derived from
                        on_us, stays the authoritative test and this null is
                        the second layer.
            "plateau"   mean raw code over [rise + plateau_margin, fall),
                        margin = 0 here so it is the whole envelope. **None
                        when not measured** (empty range), on firmware with the
                        measure_flags capability. Older firmware reports "peak"
                        instead, with no flag.
                        Do NOT try to detect that by testing plateau == peak: a
                        genuinely flat pulse has floor(mean) == max, so the test
                        misfires on the CLEANEST data. With margin at 0 the
                        empty case needs duration_samples <= 0 -- a zero-length
                        envelope -- which a real pulse never produces.
            "bg"        floor of the mean over the 20 samples BEFORE the
                        rise. If the previous pulse ended fewer than 20 samples
                        ago, this still holds samples from before THAT pulse --
                        back-to-back firing quietly degrades bg, and with it
                        integral and sigma.
            "post_bg"   mean AFTER the pulse: the STM32 waits ~50 us for the
                        signal to settle, then averages ~50 us. **None when it
                        was not measured** -- either the window is configured
                        to 0 samples, or the next pulse arrived before even one
                        sample could be taken. None, not 0: 0 is a perfectly
                        legal post-pulse current and the two must not look
                        alike. Guard with `is not None`.
            "bg_sigma4" 4x the background sigma (sigma = bg_sigma4/4)
            "integral"  background-subtracted sum over the pulse, in raw
                        counts: round(Sigma(x) - (F-R)*Sigma_bg/n), using the
                        EXACT background mean -- not the rounded "bg" above, so
                        it does not carry that field's up-to-1-code error.
                        SIGNED -- pure noise
                        sums to about zero and a pulse dimmer than its own
                        background is legitimately negative. Use
                        pulse_events_ma()'s "integral_mams" rather than scaling
                        it by hand -- the rate that conversion needs is
                        "rate_hz" below, not the one you asked for.
            "empty_envelope"  True when on_us == 0: a rise and a fall landed
                        on the SAME sample, which is a PA4 glitch, not a pulse
                        -- and the STM32 still commits a full, normal-looking
                        event for it. In that event peak is its INITIAL value
                        (0), integral is 0, and plateau is empty; none are
                        measurements. peak_ma/plateau_ma/integral_mams are
                        therefore omitted or None on such events (0 counts would
                        otherwise convert to a confident ~-32 mA, and the
                        integral to a legal 0.0 charge). bg and post_bg stay
                        valid -- bg is snapshotted at the rise and post_bg is
                        measured normally -- so those still convert.
                        Discard these events, or keep them explicitly as
                        glitches; do not average them in.
            "rate_hz"   the rate this pulse was ACTUALLY sampled at, reported
                        by the STM32 per event. Its timer runs at
                        170 MHz / an integer divider, so the achieved rate
                        rarely equals the requested one, and it can change
                        between pulses. **None on firmware too old to report
                        it.** on_us/integral and every sample-count parameter
                        are in samples of THIS rate -- so with it None, they
                        cannot be turned into time or charge at all.
            "recv_ms"   host receive time

        peak/plateau/bg/post_bg are RAW ADC
        counts, not mA; convert with pulse_ma()/pulse_events_ma() below,
        never by hand (the correct conversion needs a LIVE reference
        reading, not a fixed constant -- see pulse_ma's docstring).
        pulse_events_ma() additionally adds, per event:
            "integral_mams"  charge in mA*ms, = slope * integral / rate_hz.
                        **None** when it could not be computed; see
                        "integral_mams_unavailable" for which reason.
            "integral_mams_unavailable"  present only when integral_mams is
                        None, naming which fact was missing:
                          "rate_unknown"  old firmware sent no sample rate, so
                              there is nothing to divide by (assuming 1 MSPS
                              would scale the answer by whatever the real rate
                              turned out to be).
                          "integral_clamped"  the STM32 reports the OLD
                              clamp-at-zero integral, which is biased upward on
                              weak pulses. Converting it would hand back a
                              number that is wrong by an amount nothing in the
                              data reveals. Flash both sides.
                          "integral_form_unknown"  the STM32 never answered
                              GET_INFO, so which of the two forms it sends is
                              unknown -- distinct from knowing it is old.
                          "empty_envelope"  on_us == 0, so integral is 0
                              because nothing was integrated -- not because the
                              charge was zero.
                          "saturated"  integral hit INT32_MAX/INT32_MIN.
            "integral_saturated"  True when integral hit 0xFFFFFFFF or
                        duration hit 0xFFFF. The firmware clamps WITHOUT
                        setting any flag, so a saturated reading cannot be
                        told from a real one by value -- hence None above.
            "integral_mams_sigma"  the scatter in that charge from background
                        noise alone: sigma*sqrt(N) converted the same way.
                        A charge smaller than its own sigma has NOT been
                        distinguished from noise -- compare the two before
                        believing a small value, and expect roughly a third of
                        pure-noise pulses to land outside +/-1 sigma.
                        **None when bg_sigma4 is 0** (flagged
                        "background_flat"): a live front end always has some
                        spread, so zero means the input is stuck or unpowered.
                        Reporting 0 would make the |charge| > sigma test pass
                        for anything -- the guard would silently stop guarding.
        Returns {"ok", "events": [...], "last_id": int}: persist `last_id`
        and pass it back as `since` on your next call to get only the
        delta. Pass an intentionally huge `since` (e.g. 2_000_000_000) to
        get zero events back but still learn the CURRENT last_id -- the
        same trick the GUI's own "Clear" button uses to reset its cursor
        without walking the whole history."""
        return self._get(f"/api/pulse-events?since={int(since)}", timeout=5.0)

    def get_ads1115_ref_mv(self) -> float | None:
        """Live ADS1115 "1.2V ref" channel reading (mV) — the external
        differential circuit's ACTUAL reference right now (nominally
        ~1.2V, but drifts board-to-board and with temperature — treating
        it as a fixed constant is exactly what overstated current by
        ~35% before this was fixed in the GUI). Needed by pulse_ma() for
        an accurate conversion. Returns None if the read failed (master
        not connected, etc.) — pulse_ma() falls back to a fixed ~1.2V
        then, same as before this existed."""
        r = self._get("/api/stm32/ads1115", timeout=3.0)
        return r.get("ref_mv") if r.get("ok") else None

    # R_sense/gain live ONLY here — pulse_events_ma() below calls this
    # rather than re-deriving the formula, so there is exactly one place
    # to correct if either constant changes (this exact formula was
    # independently duplicated 3+ times across this repo before and
    # drifted out of sync once already — see the JS side's tests.js).
    _PULSE_R_SENSE_OHM = 4.7
    _PULSE_AMC3301_GAIN = 8.2

    def pulse_ma(self, raw: float, ref_mv: float | None = None) -> float:
        """Convert one raw STM32 ADC count (a pulse_events() peak/plateau/
        bg field) to emission current in mA.

        The STM32's OWN internal 12-bit ADC (PA0/ADC1_IN1, via the AMC3301
        isolation amp) samples V = raw*3.3/4095; Ie = 2*(V - 0.5*ref_v) /
        R_sense / G_amc A -> mA, with R_sense/G_amc from the class
        constants above. This is NOT the formula an ESP32-ADC-scale
        constant (3.1 V / 6.8 ohm) would give you — those numbers belong
        to a different ADC entirely and overstated current by ~35% when
        they were still in use here.

        `ref_mv` is the external differential circuit's LIVE reference —
        pass a reading from get_ads1115_ref_mv() for an accurate result.
        Omit it and this fetches one live reading itself (one extra HTTP
        round trip per call — fetch it ONCE and reuse it across a batch
        instead, e.g. via pulse_events_ma(), rather than calling this
        directly in a loop)."""
        if ref_mv is None:
            live = self.get_ads1115_ref_mv()
            ref_mv = live if live is not None else 1200.0   # last-known-good fallback
        v = raw * 3.3 / 4095 - 0.5 * (ref_mv / 1000.0)
        return 2 * v / self._PULSE_R_SENSE_OHM / self._PULSE_AMC3301_GAIN * 1000

    # Saturation markers the STM32 reports instead of a value it cannot hold.
    # integral is SIGNED, so both ends are markers, and both are REACHABLE: the
    # integral accumulates over the STM32's internal u32 sample count, which is
    # NOT bounded by duration_samples' u16 -- at full scale it hits INT32 in
    # about 520k samples (~0.5 s at 1 MSPS).
    _INTEGRAL_SAT_HI = 2_147_483_647     # INT32_MAX
    _INTEGRAL_SAT_LO = -2_147_483_648    # INT32_MIN
    # duration_samples is truncated to this on the way out, so the envelope was
    # AT LEAST this long. It saturates independently of the integral: a pulse
    # can have a perfectly good charge and an unusable width, so this must not
    # invalidate integral_mams (which never uses the duration).
    #
    # The pre-signed firmware used 0xFFFFFFFF as ITS integral marker, which
    # reads as -1 once parsed signed. That is deliberately NOT treated as
    # saturation: -1 is an ordinary integral now that the clamp is gone (pure
    # noise sums to about zero), and mixing the two firmware generations is
    # ruled out by flashing both sides together.
    _DURATION_SATURATED = 0xFFFF
    def _add_charge(self, e: dict, ref_mv: float,
                    integral_signed: bool | None = None) -> None:
        """Add integral_mams (charge, mA*ms) and its scatter to one event.

        integral is round(Sigma(sample - background)) over the pulse -- signed,
        with no per-sample clamp -- so the conversion's large offset cancels and
        charge is just slope * integral / rate.

        A NEGATIVE integral is a legal result, not an error: with the clamp gone
        pure noise sums to about zero, and a pulse dimmer than the background it
        was measured against lands below it. Do not treat negatives as faults or
        floor them at zero.

        integral_mams_sigma comes with it: sigma*sqrt(N) worth of charge, the
        scatter you would see from background noise alone over this pulse
        length. A charge smaller than its own sigma has not been distinguished
        from noise.
        """
        raw = e.get("integral")
        dur = e.get("on_us")
        # The rate comes from the EVENT, not from the rate we requested and not
        # from a constant. The STM32 reports what its timer actually achieved
        # (170 MHz / an integer divider, so rarely exactly the requested value),
        # and it can change between pulses. null => firmware too old to say.
        # Charge scales 1:1 with it, so an assumed rate is a wrong answer
        # wearing the right units: no rate, no mAs.
        rate = e.get("rate_hz")
        if raw is None:
            return
        # Zero-length envelope: integral is 0 because nothing was integrated,
        # not because the charge was zero. See the empty_envelope comment in
        # pulse_events_ma().
        if e.get("empty_envelope"):
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "empty_envelope"
            return
        # integral changed MEANING without changing shape: it used to be clamped
        # at zero per sample, which biases weak pulses upward (measured: ~57% of
        # a 976 us reading). Same offset, same width, no way to tell from the
        # value -- so the STM32's capability bit decides, relayed by the ESP32.
        # False => refuse rather than convert; None => it never answered, which
        # is UNKNOWN and must not decay into an assumption either way.
        if integral_signed is False:
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "integral_clamped"
            return
        if integral_signed is None:
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "integral_form_unknown"
            return
        if not rate:
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "rate_unknown"
            return
        if raw >= self._INTEGRAL_SAT_HI or raw <= self._INTEGRAL_SAT_LO:
            # The STM32 clamps rather than wrapping, and reports no flag -- so a
            # saturated reading is indistinguishable from a real one by value
            # alone. None, not the clamped number.
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "saturated"
            e["integral_saturated"] = True
            return
        e["integral_saturated"] = False
        slope = self.pulse_ma(1, ref_mv) - self.pulse_ma(0, ref_mv)   # mA per count
        # 9 decimals, not 6: a 1 ms pulse of a few mA is ~2e-4 mA*ms and its
        # sigma ~1e-5, which 6 decimals would flatten to one significant digit
        # and print as -0.0 for any small negative. Charge here spans several
        # orders of magnitude, so keep the resolution and normalise negative
        # zero away -- "-0.0 mA*ms" reads as a sign error rather than a number.
        def _q(v: float) -> float:
            r = round(v, 9)
            return 0.0 if r == 0 else r
        # rate is in Hz, so slope*integral/rate is mA*s; x1000 -> mA*ms, which
        # is the scale these pulses actually live at (a 1 ms pulse of a few mA
        # is ~0.2 mA*ms, vs 0.0002 mA*s -- four leading zeros of nothing).
        e["integral_mams"] = _q(slope * raw * 1000.0 / rate)
        # Random error, not bias: summing N samples of noise with sigma each
        # gives a spread of about sigma*sqrt(N). (The old clamped integral
        # needed a BIAS estimate instead -- it could only ever accumulate
        # upward. Signed accumulation centres on zero, so what is left is
        # scatter, and scatter is what tells you whether a small charge is
        # real.) Compare |integral_mams| against this.
        # sigma needs N, and a truncated duration is a LOWER BOUND on N, not N.
        # Using 65535 there would understate the scatter on exactly the longest
        # pulses -- the ones most likely to have accumulated a big integral. No
        # N, no sigma.
        sigma4 = e.get("bg_sigma4")
        # sigma4 == 0 means the background window had ZERO spread. A live 12-bit
        # front end always has some, so this says the input is stuck or
        # unpowered -- not that the measurement is noise-free. It must not
        # become a threshold: the documented test is |charge| > sigma, and with
        # sigma 0 that passes for ANY charge, turning the one guard against
        # over-reading a weak signal into a rubber stamp. Found on hardware with
        # the analog front end unpowered (every sample 0, sigma4 0).
        if sigma4 == 0:
            e["background_flat"] = True
            e["integral_mams_sigma"] = None
        elif dur is not None and dur >= self._DURATION_SATURATED:
            e["duration_saturated"] = True
            e["integral_mams_sigma"] = None
        elif sigma4 is not None and dur:
            e["duration_saturated"] = False
            e["integral_mams_sigma"] = _q(
                slope * (sigma4 / 4.0) * math.sqrt(dur) * 1000.0 / rate)

    def pulse_events_ma(self, since: int = 0) -> dict:
        """Like pulse_events(), but every event also gets peak_ma/
        plateau_ma/bg_ma/post_bg_ma fields, converted with ONE live ADS1115
        reference reading shared across the whole batch — cheaper and
        more internally consistent than calling pulse_ma() per-event
        (each of which would otherwise fetch its own live reading).
        Returns {"ok", "events": [...], "last_id", "ref_mv": <the reading
        actually used, or None if that read failed and the ~1.2V fallback
        was used instead>}.

        A field that was not measured stays None and gets NO _ma companion --
        post_bg_ma is simply absent on such an event, rather than carrying a
        converted stand-in. Check `"post_bg_ma" in event`, or guard on
        `event["post_bg"] is not None`."""
        r = self.pulse_events(since)
        if not r.get("ok"):
            return r
        ref_mv = self.get_ads1115_ref_mv()
        # Resolve the fallback ONCE here and always pass a real number to
        # pulse_ma() below — passing None would make IT fetch its own live
        # reading per event, defeating the one-shared-reading point of
        # this method entirely.
        resolved_ref_mv = ref_mv if ref_mv is not None else 1200.0
        for e in r.get("events", []):
            # A zero-length envelope is a PA4 glitch, not a pulse: a rise and a
            # fall landing on the same sample still commit a full event. In it,
            # peak_adc is its INITIAL value (0), integral is 0 and plateau is
            # empty -- none of them measurements. Converting 0 counts yields
            # about -32 mA, a confident-looking current that was never measured,
            # and integral would report a legal 0.0 charge. bg and post_bg ARE
            # real (bg is snapshotted at the rise, post_bg measured normally),
            # so they still convert.
            empty = e.get("on_us") == 0
            e["empty_envelope"] = empty
            keys = ("bg", "post_bg") if empty else ("peak", "plateau", "bg", "post_bg")
            # The `is not None` guard is what keeps "not measured" (null) from
            # being converted into a plausible mA value.
            for key in keys:
                if e.get(key) is not None:
                    e[f"{key}_ma"] = round(self.pulse_ma(e[key], resolved_ref_mv), 3)
            self._add_charge(e, resolved_ref_mv, r.get("integral_signed"))
        r["ref_mv"] = ref_mv
        return r

    def measure_pulse_current(
        self,
        filament: int,
        num_pulses: int = 1,
        width_us: int = 1000,
        rate_hz: int = 1000000,
        inter_pulse_ms: int = 3000,
        max_on_ms: int = 40,
        total_ms: int = 15000,
        controller: int | None = None,
        trigger: str = "sim",
        timeout_s: float = 15.0,
        verify: bool = True,
        reuse: bool = False,
        post_bg_gap_us: float | None = None,
        post_bg_n_us: float | None = None,
    ) -> dict:
        """Fire and measure, returning the two halves separately.

        Identical work to fire_single_pulse(..., measure=True) -- which is
        now the recommended call, since firing and measuring belong to the
        same operation and keeping them in one function stops anyone firing
        HV they forgot to measure. This wrapper differs only in SHAPE: it
        nests the fire result under "fired" instead of merging it, which is
        handy when you want to log the two halves apart.

            {"ok":       both fired AND every pulse measured,
             "fired":    the full fire_single_pulse result dict,
             "measured": [one event per pulse, peak_ma/plateau_ma/bg_ma],
             "ref_mv":   the live reference reading actually used}

        See fire_single_pulse's "measure=True" section for the arming and
        correlation rules, why a partial measurement reports ok=False, and what
        post_bg_gap_us/post_bg_n_us do -- they are forwarded unchanged, so this
        wrapper can do everything the call it wraps can.
        """
        r = self.fire_single_pulse(
            filament, num_pulses=num_pulses, width_us=width_us,
            inter_pulse_ms=inter_pulse_ms, max_on_ms=max_on_ms,
            total_ms=total_ms, controller=controller, trigger=trigger,
            timeout_s=timeout_s, verify=verify, reuse=reuse,
            measure=True, rate_hz=rate_hz,
            post_bg_gap_us=post_bg_gap_us, post_bg_n_us=post_bg_n_us)
        fired = {k: v for k, v in r.items() if k not in ("measured", "ref_mv")}
        return {"ok": bool(r.get("ok")), "fired": fired,
                "measured": r.get("measured") or [], "ref_mv": r.get("ref_mv")}

    # ── Human-readable result decoding ────────────────────────────────────────
    # Every method above returns a plain dict -- convenient for scripting, but
    # not something you'd want to eyeball in a log. describe() turns any of
    # those dicts into one short English sentence, for a print()/log line
    # instead of dumping raw JSON. Best-effort: it recognizes a result SHAPE
    # (which keys are present), not which method produced it -- so it works
    # on a dict you've stashed/reloaded too -- and falls back to a short
    # generic ok/error summary for anything it doesn't recognize. Never
    # raises: an unrecognized dict still gets the generic summary, and a
    # non-dict input is just str()'d.

    _SHV_STATE_NAMES = {0: "idle", 1: "armed", 2: "running",
                        3: "complete", 4: "fault"}
    _SHV_STOP_REASON_NAMES = {0: "none", 1: "complete", 2: "read-back mismatch",
                              3: "inter-pulse timeout", 4: "total timeout",
                              5: "fault", 6: "disarmed"}

    def describe(self, result: dict) -> str:
        """One short English sentence describing any result dict this
        client returns -- e.g. `print(ct.describe(ct.active_one(5, 2900)))`
        instead of printing the raw dict. See the section comment above for
        what it recognizes. Never raises."""
        if not isinstance(result, dict):
            return str(result)
        try:
            return self._describe(result)
        except Exception as exc:   # a formatting bug here should never break a log line
            return f"(describe() failed: {exc}) {result}"

    def _describe_skips(self, r: dict) -> str:
        """The clause naming filaments that were DROPPED rather than commanded.

        Without this, describe() reported "Applied to 2 filament(s)." for a call
        that was asked for three -- the dead_skipped/excluded keys were sitting
        right there in the dict and never surfaced. A summary line that silently
        omits what it skipped is the same defect the keys were added to fix, one
        layer up. Returns "" when nothing was dropped, so callers can append it
        unconditionally."""
        parts = []
        dead = r.get("dead_skipped") or []
        if dead:
            parts.append(f"{len(dead)} dead-skipped: {dead}")
        exc = r.get("excluded") or []
        if exc:
            parts.append(f"{len(exc)} excluded (no board / controller offline): {exc}")
        uns = r.get("unslotted") or []
        if uns:
            parts.append(f"{len(uns)} unslotted: {uns}")
        # Not a skip -- these WERE commanded, but the 74HC165 read-back disagrees
        # with what was asked for, so the bit did not land. Surfaced here because
        # it forces ok:False and would otherwise be invisible in the summary line.
        mis = r.get("mismatched") or []
        if mis:
            parts.append(f"{len(mis)} read-back MISMATCH (did not land): {mis}")
        # Distinct from mismatched: two reads disagreed with each OTHER, so the
        # bit's state is unknown rather than known-bad. Named differently on
        # purpose -- "unknown" and "failed" are different things to act on.
        unst = r.get("unstable") or []
        if unst:
            parts.append(f"{len(unst)} UNCONFIRMED (165 read unstable, state unknown): {unst}")
        return (", " + ", ".join(parts)) if parts else ""

    def _describe_heating(self, h: dict) -> str:
        """One clause describing a verify=True sub-result -- either
        wait_for_current()'s shape ("measured_ma") or
        _wait_for_voltage_mode()'s ("cc_mode") -- with no trailing period,
        for embedding inline. Empty string if `h` isn't one of those."""
        if not isinstance(h, dict):
            return ""
        if "measured_ma" in h:
            # Never say "reached N mA" for something that was not measured. A
            # stop is confirmed from the board's POWER STATE (no current reading
            # exists once the rail is down -- see wait_for_current), and
            # reporting that as a measured zero would be claiming evidence we
            # do not have, in the summary line people actually read.
            if h.get("measured_from") == "power_state":
                return (f"not heating — board reports {h.get('power_state')} "
                        f"(confirmed by power state, not measured) "
                        f"in {h.get('elapsed_s', 0):.1f}s")
            verb = "reached" if h.get("ok") else "did NOT reach"
            src = f" [{h['measured_from']}]" if h.get("measured_from") else ""
            return (f"{verb} {h.get('measured_ma')} mA{src} "
                    f"(target {h.get('target_ma')} mA) in {h.get('elapsed_s', 0):.1f}s")
        if "cc_mode" in h:
            verb = "entered voltage-regulation mode" if h.get("ok") else "did NOT enter voltage mode"
            return f"{verb} (cc_mode={h.get('cc_mode')}) in {h.get('elapsed_s', 0):.1f}s"
        return ""

    def _describe(self, r: dict) -> str:
        ok = r.get("ok")

        # dead-filament shortcut (stop_one/idle_one/hv_grid_set/fire_single_pulse/...)
        if r.get("dead"):
            return f"Filament {r.get('filament')} is marked dead — no command sent."

        # Fire+measure results, in EITHER shape. fire_single_pulse(measure=True)
        # merges the fire in, so "fired" is a pulse COUNT and "error" is at the
        # top level; measure_pulse_current() nests the whole fire result under
        # "fired" instead. Both are checked BEFORE fire_single_pulse's own shape
        # below, since all three carry a "fired" key. Reading an int "fired" as
        # a dict is how this branch used to report "unknown error" while the
        # actual reason was sitting right there in r["error"].
        if "measured" in r and "fired" in r:
            nested = r["fired"] if isinstance(r.get("fired"), dict) else None
            fire_ok = nested.get("ok") if nested is not None else (r.get("fired") or 0) > 0
            why = (nested or r).get("error") or r.get("error")
            label = "measure_pulse_current" if nested is not None else "fire_single_pulse(measure=True)"
            if not fire_ok:
                return f"{label}: fire failed — {why or 'unknown error'}"
            n = len(r.get("measured") or [])
            if not ok:
                return (f"{label}: fired but only {n} pulse(s) measured "
                        f"(detector gap?){' — ' + why if why else ''} "
                        f"— ref {r.get('ref_mv')} mV")
            mas = [e.get("peak_ma") for e in r["measured"] if e.get("peak_ma") is not None]
            peaks = ", ".join(f"{m:.2f}" for m in mas) if mas else "?"
            return f"Measured {n} pulse(s), peak mA: {peaks} (ref {r.get('ref_mv')} mV)"

        # fire_single_pulse(): {"ok","fired","records","status",...}
        if "fired" in r and "records" in r and "status" in r:
            if r.get("timeout"):
                return f"fire_single_pulse timed out: {r.get('error')}"
            if not ok:
                return f"fire_single_pulse failed: {r.get('error', 'unknown error')}"
            st = r.get("status") or {}
            state = self._SHV_STATE_NAMES.get(st.get("state"), st.get("state"))
            reason = self._SHV_STOP_REASON_NAMES.get(st.get("stopReason"), st.get("stopReason"))
            return (f"Fired {r.get('fired')} pulse(s), schedule {state} "
                    f"(stop reason: {reason}), {st.get('totalPulsesDone', '?')} "
                    f"total pulses done, elapsed {st.get('elapsedMs', '?')} ms.")

        # read_board_status(): has state_name/fault_name
        if "state_name" in r or "fault_name" in r:
            if not ok:
                return (f"read_board_status failed for filament {r.get('filament')}: "
                        f"{r.get('error', 'unknown error')}")
            loc = f"ctrl {r.get('controller')} ch{r.get('channel')}.{r.get('mux_port')}"
            return (f"Filament {r.get('filament')} ({loc}): state={r.get('state_name')}, "
                    f"fault={r.get('fault_name')}.")

        # shv_status(), or its "status" sub-dict on its own
        if "stopReason" in r or ("state" in r and "entryCount" in r):
            state = self._SHV_STATE_NAMES.get(r.get("state"), r.get("state"))
            reason = self._SHV_STOP_REASON_NAMES.get(r.get("stopReason"), r.get("stopReason"))
            fil = r.get("filamentIndex")
            fil_txt = f", live filament {fil}" if fil not in (None, 0xFF, 255) else ""
            return (f"SHV engine: {state} (stop reason: {reason}){fil_txt}, "
                    f"{r.get('totalPulsesDone', '?')}/{r.get('totalPulsesTarget', '?')} "
                    f"pulses done, entry {r.get('entryIndex', '?')}/{r.get('entryCount', '?')}, "
                    f"elapsed {r.get('elapsedMs', '?')} ms.")

        # enable_emission()/enable_focus(): {"ok","ch","on"}
        if "ch" in r and "on" in r and "controller" not in r:
            if not ok:
                return f"HV enable failed for {r.get('ch')}: {r.get('error', 'unknown error')}"
            return f"{str(r.get('ch')).capitalize()} HV commanded {'ON' if r.get('on') else 'OFF'}."

        # hv_status(): {"emission_on","focus_on",...}
        if "emission_on" in r or "focus_on" in r:
            if not ok:
                return f"hv_status read failed: {r.get('error', 'unknown error')}"
            tag = lambda v: "unknown" if v is None else ("ON" if v else "OFF")
            return (f"Emission HV: {tag(r.get('emission_on'))}; Focus HV: {tag(r.get('focus_on'))}; "
                    f"ADS alert: {bool(r.get('ads1115_alert'))}; AMC diag: {bool(r.get('amc3301_diag'))}.")

        # get_fault_policy()/set_fault_policy(): {"board","mismatch","mismatchCount","faultedFilaments"}
        if "mismatchCount" in r and "faultedFilaments" in r:
            pol = lambda v: "continue" if v else "stop"
            fils = r.get("faultedFilaments") or []
            fils_txt = f", faulted: {fils}" if fils else ""
            return (f"Fault policy: board={pol(r.get('board'))}, mismatch={pol(r.get('mismatch'))}; "
                    f"{r.get('mismatchCount', 0)} read-back mismatch(es) this run{fils_txt}.")

        # get_ocp_threshold_one(): {"enabled","threshold_ma",...}
        if "threshold_ma" in r and "enabled" in r:
            if not ok:
                return (f"OCP threshold read failed for filament {r.get('filament')}: "
                        f"{r.get('error', 'unknown error')}")
            state = f"{r.get('threshold_ma')} mA" if r.get("enabled") else "OFF (protection disabled)"
            return f"Filament {r.get('filament')} OCP threshold: {state}."

        # get_trigger_delay()/set_trigger_delay(): {"delayUs","applies"}
        if "delayUs" in r:
            applies = "applies" if r.get("applies") else "does NOT apply (live fire path can't honour it)"
            return f"Trigger delay: {r.get('delayUs')} µs, {applies}."

        # acquire_lease()/release_lease()/renew_lease(): {"lock": {...}}
        if "lock" in r:
            lock = r.get("lock") or {}
            if not ok:
                return (f"Lease unavailable — held by '{lock.get('owner')}' "
                        f"({lock.get('expires_in_s', '?')} s remaining).")
            return (f"Lease held by '{lock.get('owner')}'"
                    + (f", {lock.get('expires_in_s')} s remaining." if lock else " (released)."))

        # wait_for_current()/_wait_for_voltage_mode() called standalone (not nested
        # under "heating"), or a *_one() result with an embedded "heating" sub-result
        direct = self._describe_heating(r)
        if direct:
            prefix = f"Filament {r.get('filament')}: " if "filament" in r else ""
            return prefix + direct + "."

        # stop_all/sleep_all/standby_all/idle_all/active_all/voltage_all():
        # {"ok","applied": int, "failed": [...]}
        if "applied" in r and "failed" in r and isinstance(r.get("applied"), int):
            if r.get("skipped_dead"):
                dead = r.get("dead_skipped") or []
                which = f" ({dead})" if dead else ""
                return f"No live filaments in the requested set (all dead-masked{which}) — nothing sent."
            failed = r.get("failed") or []
            fail_txt = f", {len(failed)} failed: {failed}" if failed else ""
            return f"Applied to {r.get('applied')} filament(s){fail_txt}{self._describe_skips(r)}."

        # hv_grid_set()/hv_grid_set_all()/set_ocp_threshold_all(): "applied" is a
        # list of filaments here, not a count
        if "applied" in r and "failed" in r:
            if r.get("skipped_dead"):
                dead = r.get("dead_skipped") or []
                which = f" ({dead})" if dead else ""
                return f"Nothing sent — every requested filament is dead-masked{which}."
            applied = r.get("applied")
            n = len(applied) if isinstance(applied, list) else applied
            failed = r.get("failed") or []
            fail_txt = f", {len(failed)} failed: {failed}" if failed else ""
            return f"Applied to {n} filament(s){fail_txt}{self._describe_skips(r)}."

        # stop_one/sleep_one/standby_one/idle_one/active_one/voltage_one() via
        # _state_one(): {"ok","filament","state": 1-6,"arg", ...}
        if "filament" in r and isinstance(r.get("state"), int) and 1 <= r["state"] <= 6:
            state_name = _STATE_NAMES.get(r["state"], r["state"])
            base = f"Filament {r.get('filament')} commanded to {state_name}"
            arg = r.get("arg")
            if arg:
                unit = "mV" if r["state"] == VOLTAGE else "mA"
                base += f" ({arg} {unit})"
            if not ok:
                base += f" — FAILED: {r.get('error', 'board did not ACK')}"
            heating_txt = self._describe_heating(r.get("heating")) if "heating" in r else ""
            if heating_txt:
                base += f"; verify: {heating_txt}"
            return base + "."

        # bulk per-filament table with no top-level "ok" (read_filament_currents())
        if "ok" not in r and r and all(isinstance(v, dict) for v in r.values()):
            present = sum(1 for v in r.values() if v.get("present"))
            return f"{len(r)} filament(s) in result, {present} reported present."

        # generic fallback
        if ok is True:
            extra = {k: v for k, v in r.items() if k != "ok"}
            return f"OK ({extra})." if extra else "OK."
        if ok is False:
            # "no_device" is not a failure to retry -- the chip the command
            # needs is not on the bus. Saying "Failed: ..." for it sends the
            # reader looking for a fault in something that is simply absent.
            if r.get("reason") == "no_device":
                return f"Chip not present: {r.get('error', 'the required chip is absent')}"
            return f"Failed: {r.get('error', 'unknown error')}"
        return str(r)
