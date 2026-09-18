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
        self._dead_cache: set[int] = set()   # USER_INDEX; see the `dead` property
        self._dead_fetched_at = 0.0
        self._dead_stale = False
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
        # Whether this run has asked the backend what is already loaded. The
        # backend outlives the script, so a FRESH process can reuse a table a
        # previous run downloaded -- before this, every new process started
        # blank and paid for the download again even though the hardware
        # already held exactly the right table. Fetched once, lazily: the
        # answer only changes when WE download, and this client is the one
        # doing that.
        self._loaded_fetched = False

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
    def energised(self, *filaments: int, verify: bool = True):
        """Context manager: whatever happens inside, these filaments get STOPped
        on the way out — normal return, exception, or Ctrl-C.

        Use this around ANY code that heats a filament. Relying on a STOP at the
        end of the script is not enough: a bug anywhere in between skips it and
        leaves the filament at full power indefinitely. That is not
        hypothetical — a diagnostic here raised AttributeError one line before
        its stop_one() and left a filament ramping to 2.9 A at 10.5 V for
        several minutes before anyone noticed.

            with ct.energised(7):
                ct.active_one(7, 2900, verify=True)
                ...                      # a crash here still stops filament 7

        What this does NOT cover: the process being killed outright (SIGKILL),
        or the machine dying. Only the backend can protect against that, since
        it outlives the script — there is no such watchdog yet, and the lease
        (which does self-expire) only gates writes, it de-energises nothing.
        """
        try:
            yield self
        finally:
            # Deliberately not conditional on success, and each filament is
            # attempted even if an earlier one errors: the whole point is that
            # this path runs when something has already gone wrong.
            for f in filaments:
                try:
                    self.stop_one(int(f), verify=verify)
                except Exception:
                    pass

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
    # The mask lives in the BACKEND now, not here. It used to die with the
    # script, so the next run had to re-declare every entry -- and, worse, it
    # was only honoured by clients that filtered, while backend.py would carry
    # out an energise command for a dead filament from anywhere else.
    #
    # It changes rarely and is read constantly, so it is cached for
    # _DEAD_TTL_S and refreshed on every write. A stale cache here is safe by
    # construction: the backend enforces the mask itself, so the worst a stale
    # read does is send a request the backend then refuses and reports.


    @property
    def dead(self) -> frozenset[int]:
        """Filaments that must not be energised, in YOUR numbering (USER_INDEX).

        A frozenset, deliberately: `ct.dead.add(5)` used to appear to work and
        would now silently fail to reach the backend, so it raises instead.
        Use add_dead()/remove_dead()/set_dead().
        """
        self._refresh_dead()
        return frozenset(self._dead_cache)

    # Consecutive mode 2/3 reads before a fault is believed. A startup-inrush
    # OCP that recovers on the next revive can flash the fault bits, so one
    # sighting is not a verdict. At the default 0.1 s poll interval this is a
    # ~0.3 s window -- long enough to ride out a transient, short enough that a
    # real fault still ends the wait immediately rather than at timeout.

    def _refresh_dead(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and self._dead_fetched_at and (now - self._dead_fetched_at) < self._DEAD_TTL_S:
            return
        r = self._get("/api/dead-fids", timeout=5.0)
        if not r.get("ok"):
            # Keep the last known mask. Falling back to an EMPTY set would read
            # as "nothing is dead" and let this client happily target a filament
            # someone disabled -- a failed read must not become a permissive
            # answer. The backend still enforces regardless of what we think.
            self._dead_stale = True
            return
        # The backend stores FIDs; translate into this script's numbering so
        # `ct.dead` matches the indices the caller passes everywhere else.
        self._dead_cache = {self._user_index_of(int(f)) for f in (r.get("dead") or {})}
        self._dead_fetched_at = now
        self._dead_stale = False

    def _write_dead(self, op: str, filaments, reason: str | None) -> dict:
        fids = [int(self._fid_of(f)) for f in filaments]
        body = {"op": op, "fids": fids}
        if op != "remove":
            # Not fabricated when absent: the backend requires a non-empty
            # reason because an entry nothing clears automatically has to say
            # why, and inventing a plausible cause here would be worse than
            # recording that nobody wrote one down.
            body["reason"] = reason or f"(no reason recorded — set by {self.client_id})"
        r = self._post("/api/dead-fids", body, timeout=10.0)
        self._refresh_dead(force=True)
        return r

    def set_dead(self, filaments, reason: str | None = None) -> dict:
        """Replace the dead mask with the given filament indices (0–95).

        "Dead" means THIS FILAMENT MUST NOT BE ENERGISED. The board it sits on
        may be perfectly fine -- the filament is the faulty part. It is your
        decision, never something inferred: nothing clears an entry on its own,
        and in particular not a board dropping out of presence (presence is
        about the board, this is about the filament).

        Dead filaments are skipped in every batch power-state call and listed
        in "dead_skipped". Any SINGLE-filament call (active_one, idle_one,
        fire_single_pulse, hv_grid_set, ...) returns
        {"ok": False, "dead": True, ...} -- it does NOT raise.

        Enforcement is in the backend, so this survives the script exiting and
        applies to every client, not just this one. Repaired a filament? Call
        remove_dead() -- it is meant to be changeable, just rarely changed.

        `reason` is recorded with who and when. Pass a real one: these entries
        outlive the session, and "why is 55 disabled" is the question nobody
        can answer later without it.

        Example:
            ct.set_dead([3, 7, 12, 55], reason="burnt emitters, 2026-09 bench")
        """
        return self._write_dead("set", filaments, reason)

    def add_dead(self, *filaments: int, reason: str | None = None) -> dict:
        """Add filaments to the dead mask. Existing entries keep their original
        provenance rather than being overwritten with this call's reason."""
        return self._write_dead("add", filaments, reason)

    def remove_dead(self, *filaments: int) -> dict:
        """Un-block filaments -- the repaired-it path. No reason needed: the
        record of why it was blocked goes away with the entry."""
        return self._write_dead("remove", filaments, None)

    def dead_details(self) -> dict:
        """The dead mask with provenance: {USER_INDEX: {reason, by, at}}."""
        r = self._get("/api/dead-fids", timeout=5.0)
        if not r.get("ok"):
            return r
        return {"ok": True, "dead": {self._user_index_of(int(f)): v
                                     for f, v in (r.get("dead") or {}).items()},
                "stale": False}

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
        dead = self.dead   # bound once: `self.dead` is a property, and inside a
                           # comprehension it would be re-evaluated per element
        survivors = [f for f in base if f not in dead]   # dead mask is in USER_INDEX space
        return self._fids_of(survivors)                      # then cross to FID

    def _live_user_indices(self, filaments=None) -> list[int]:
        """Same dead-mask filtering as _live(), but the result stays in
        USER_INDEX space instead of being crossed to FID.

        _live() exists to build a wire payload, so it ends with _fids_of().
        Anything that iterates the survivors and calls other client methods
        with them (which all take USER_INDEX) needs this one instead -- using
        _live()'s return as a loop variable silently applies the filament-order
        swap twice.
        """
        base = ([int(f) for f in filaments] if filaments is not None
                else list(range(self.FILAMENT_COUNT)))
        dead = self.dead   # bound once -- property; see _live()
        return [f for f in base if f not in dead]

    # PowerState values that put power ON the filament (STANDBY enables the
    # output at the firmware's 0.8 V floor, so it counts). Mirrors the backend's
    # ENERGISING_STATES -- the two must agree, or the client refuses something
    # the backend would have allowed, which is how a dead filament ends up
    # impossible to turn OFF.

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

    # ── Constants ────────────────────────────────────────────────────────
    # All of them, in one place. They used to sit next to whichever method
    # happened to need them, spread over several thousand lines, so there was
    # no way to see what existed or to check one against another -- and two of
    # these (the saturation markers, the ACTIVE floor) only make sense read
    # together with their neighbours.
    #
    # Names are unchanged, including the leading underscores, so nothing that
    # referenced them had to move with them.

    FILAMENT_COUNT = 96      # USER_INDEX filaments 0..95

    # -- Wire limits. Exceeding these is rejected HERE rather than silently
    #    truncated on the wire: `int(x) & 0xFF` once turned a request for 300
    #    pulses into 44.
    _U8_MAX = 255            # numPulses is a single byte on the wire
    _U16_MAX = 65535
    _U32_MAX = 4294967295

    # -- ADC -> emission current, the STM32's own scale (NOT an ESP32 ADC
    #    constant -- those belong to a different chip). mA = k*raw + c, and
    #    pulse_ma() builds both from these two plus a LIVE reference reading.
    _PULSE_R_SENSE_OHM = 4.7      # shunt
    _PULSE_AMC3301_GAIN = 8.2     # AMC3301 fixed gain

    # -- Saturation markers the STM32 sends instead of a value it cannot hold.
    #    `integral` is SIGNED, so both ends are markers, and both are
    #    REACHABLE: it accumulates over the firmware's internal u32 sample
    #    count, which duration_samples' u16 does not bound -- at full scale
    #    INT32 is about 520k samples, ~0.5 s at 1 MSPS.
    _INTEGRAL_SAT_HI = 2_147_483_647     # INT32_MAX
    _INTEGRAL_SAT_LO = -2_147_483_648    # INT32_MIN
    #    duration_samples is truncated to this on the way out, so the envelope
    #    was AT LEAST this long. It saturates independently of the integral: a
    #    pulse can have a good charge and an unusable width, which is why this
    #    does not invalidate integral_mams (that never uses the duration).
    _DURATION_SATURATED = 0xFFFF

    # -- Power-state ladder. STOP/SLEEP leave the output off; STANDBY enables
    #    it at the firmware's 0.8 V floor and is therefore NOT a no-power state
    #    (measured: 2.1 A of inrush decaying to ~885 mA). Guards block
    #    energising and never de-energising -- refusing STOP would leave a
    #    faulty filament with no way to be turned off.
    _ENERGISING_STATES = frozenset({STANDBY, IDLE, ACTIVE, VOLTAGE})

    # -- Poll/confirm timings.
    _DEAD_TTL_S = 5.0        # dead-mask cache; refreshed on every write
    #    Consecutive mode 2/3 reads before a fault is believed. A startup-inrush
    #    OCP that recovers on the next revive can flash the fault bits, so one
    #    sighting is not a verdict; ~0.3 s at the default poll interval.
    _FAULT_CONFIRM_READS = 3

    # The firmware's DEFAULTS, which are NOT the ceilings (below 2000,
    # above/warm 5000). below/above are the COLD-start rates and sit well under
    # their ceiling on purpose -- fast cold slew is what trips OCP on the
    # cold-inrush. Only `warm` is set near its own. Recorded here because
    # "reset to defaults" is exactly where reaching for the ceiling looks right
    # and is not: doing that once here made cold starts 5x/10x faster.
    SLEW_DEFAULTS = {"below_mV_per_s": 400, "above_mV_per_s": 1000,
                     "warm_mV_per_s": 2800}
    SLEW_CEILINGS = {"below_mV_per_s": 2000, "above_mV_per_s": 5000,
                     "warm_mV_per_s": 5000}

    # -- Test/measurement flow defaults. These mirror the GUI's Calibration &
    #    Test tab input boxes one-for-one, so a run from here and a run from
    #    the browser are the same measurement; see measure_filament_resistance()
    #    and sweep_filament_impedance().
    #    R thresholds are per-flow on purpose and NOT interchangeable: the
    #    STANDBY test divides one V by one I at the 0.8 V floor, where the
    #    numerator is a couple of hundred mV of slack; the sweep fits a line
    #    through a whole V-I curve and resolves a genuinely smaller R.
    _T1_SETTLE_S = 3.0        # hold at STANDBY before reading; thermal settle
    _T1_SHORT_OHM = 0.05      # R below this (or a TPS fault) = SHORT
    _T1_OPEN_MA = 10.0        # current below this at 0.8 V = OPEN
    _T6_START_MV = 800        # the firmware's own voltage floor; below it the
                              # regulator will not start, so a sweep cannot
                              # begin lower no matter what is asked for
    _T6_END_MV = 1500
    _T6_STEP_MV = 100
    _T6_DWELL_S = 1.5         # per step, for thermal equilibrium
    _T6_SHORT_OHM = 0.02      # fitted R0 below this = SHORT
    _T6_MIN_FIT_POINTS = 3    # 2 unknowns (a, R0); fewer cannot be fitted
    #    Above this, the I and I³ columns of the fit are too alike over the
    #    sampled currents to split R₀ from the self-heating term -- see
    #    fit_cold_resistance(). Sweeps that fit properly measure ~0.85; the
    #    bench load that does not measures 0.9916.
    _T6_MAX_COLLINEARITY = 0.95
    #    How far R at the sweep's opening voltage may drift by the time the
    #    sweep returns to it before R₀ stops being a COLD resistance. This is a
    #    repeatability bound, not a physical constant: 5% on R is well inside
    #    what separates filaments from each other, and well outside INA219
    #    quantisation at these currents.
    _T6_HYSTERESIS_TOL = 0.05

    # -- ready_relay arm TTL, host side. The ESP32 auto-disarms the pulse-relay
    #    after this long with NO relayed edge, to reclaim an arm left behind by a
    #    killed script. Relayed pulses renew it in firmware, so the only thing
    #    the host has to size is the GAP between pulses -- a schedule sparser
    #    than the TTL is indistinguishable from an abandoned arm.
    _READY_TTL_FLOOR_MS = 60000     # never below the firmware's own default
    _READY_TTL_GAP_FACTOR = 4       # x inter_pulse_ms; room for a late pulse
                                     # without waiting a whole extra cycle to
                                     # reclaim a genuinely dead arm

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
            self._dead_fetched_at = 0.0   # same reason as below
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
        # The dead cache holds USER_INDEX values translated under the PREVIOUS
        # order, so it now names the wrong filaments. Drop it rather than
        # translate: re-fetching costs one request, and a mask that quietly
        # points at the wrong indices is the failure this whole naming pass
        # exists to prevent.
        self._dead_fetched_at = 0.0

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

    def _reindex_response(self, r: dict,
                          keys=("applied", "failed", "ladder_blocked")) -> dict:
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
        dead = self.dead   # bound once — property, see _live()
        dead_skipped = [f for f in requested if f in dead]
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
            alive = {int(k): v for k, v in currents.items() if int(k) not in dead}
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
        # Block energising, never de-energising: STOP/SLEEP on a dead filament
        # must go through, or marking one dead would leave it with no way to be
        # turned off -- the opposite of the point. Same rule as the backend's.
        if state in self._ENERGISING_STATES and self._is_dead(filament):
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
                  "elapsed_s": float, "present": bool, "cc_mode": int,
                  "faulted": bool}   # True = gave up EARLY because the CC
                                     # loop reported this channel faulted,
                                     # with "error" naming it. Distinct from
                                     # ok=False after a full timeout, which
                                     # means it was still trying.
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
        # Deliberately `start`, not 0: the first struggling check happens 2 s in,
        # not immediately. The bit LATCHES while the output stays down, so a
        # filament that was struggling in an earlier run still reads struggling
        # before this attempt has driven it at all. Checking at t=0 would refuse
        # a repaired load forever -- refuse, never drive, never clear, refuse.
        # The delay gives the firmware a revive pass to clear it.
        last_struggle_check = start
        # Deliberately `start - 1e9`, not `start`: the FIRST iteration should be
        # allowed to take the live read (that is the one that answers a stale
        # cache immediately); the rate limit is only about the ones after it.
        last_live_read = start - 1e9
        fault_streak = 0            # consecutive mode 2/3 reads; see below
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
                # Rate-limited, NOT once per poll. This is a live INA219 I2C mux
                # sweep -- the most expensive read in this client -- and it fires
                # exactly when the cache has nothing, which for an absent board
                # is every single iteration for the whole timeout. Two costs, and
                # the second is the bad one: measured, polling a live sweep slows
                # a CC ramp by ~20%, so on a filament that is merely SLOW this
                # fallback was making it slower while waiting for it. For an
                # absent board it is pure waste -- the sweep reports present=False
                # and the guard below rejects the result every time.
                #
                # Once a second is enough to catch the case this exists for (a
                # cache that is genuinely stale while the board is fine).
                if time.monotonic() - last_live_read < max(poll_interval_s, 1.0):
                    live = {}
                else:
                    last_live_read = time.monotonic()
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
            # The CC loop's OWN verdict, when it has one. cc_mode 2/3 means the
            # firmware faulted this channel -- it is not going to arrive, and
            # continuing to poll just burns the timeout and then reports a
            # generic "did not reach target" that reads identically to a
            # thermally slow filament or an unreachable setpoint.
            #
            # This covers an OPEN filament and a genuine OCP trip. It does NOT
            # cover a SHORT, and that is not an oversight here but a property of
            # the firmware: mode 2/3 is only ever set behind a
            # `feedbackMv >= 2000` guard, and a shorted output cannot reach 2 V
            # -- that is what shorted means. A short therefore sits in mode 1
            # indefinitely while the guardian keeps reviving the collapsed
            # output. Measured on this bench by the RP2350 session: a shorted
            # board commanded Idle 1200 mA held mode 1, measMv 0, measMa 1-2 for
            # minutes. So never read "mode 1 and current not rising" as healthy
            # -- see the struggling[] check below, which is what catches it.
            #
            # There is also still no positive "arrived" signal, so ok= below
            # compares a POLLED sample against the target. That comparison
            # belongs in the loop, not here: a trip between two polls is
            # invisible to this host. Requested on the RP2350 side (they hold
            # tpsCcConverged_/tpsCcCapped_ internally, unreported).
            # ARRIVAL: the firmware's own answer, which is the whole point --
            # comparing polled samples here was always the wrong place for the
            # judgement. The loop sees every sample; this host sees one every
            # 50-100 ms across a shared link, and (measured) polling live INA
            # reads to watch a ramp slows the ramp by ~20%.
            #
            #   settled -> it arrived. Believe it over any comparison here.
            #   capped  -> pinned at the voltage cap, target NOT reached. This
            #              answers what a timeout could not: "cannot arrive at
            #              this cap", not "still on its way". Stop; waiting
            #              longer cannot help unless the load or cap changes.
            arrival = data.get("arrival")
            if arrival == "settled" and abs(float(target_ma)) > tolerance_ma:
                return {"ok": True, "filament": int(filament),
                        "target_ma": float(target_ma), "measured_ma": measured,
                        "measured_valid": valid, "measured_from": source,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False)),
                        "cc_mode": data.get("cc_mode"), "arrival": arrival,
                        "faulted": False}
            if arrival == "capped" and abs(float(target_ma)) > tolerance_ma:
                return {"ok": False, "filament": int(filament),
                        "target_ma": float(target_ma), "measured_ma": measured,
                        "measured_valid": valid, "measured_from": source,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False)),
                        "cc_mode": data.get("cc_mode"), "arrival": arrival,
                        "faulted": False, "capped": True,
                        "error": f"CC loop is CAPPED — pinned at the voltage cap "
                                 f"with {target_ma} mA unreached (holding "
                                 f"{measured} mA). Not a fault and not slow: "
                                 f"unreachable at this cap. Raise the cap or "
                                 f"change the load; waiting will not help."}
            # CANNOT START -- the only signal that catches a SHORT. A short
            # never sets the fault bits (they need feedbackMv >= 2000, which a
            # short cannot reach) and its arrival bits read "ramping" forever,
            # so on a shorted board every check above says "still on its way"
            # and this would burn the full timeout. Measured on CH2.8: arrival
            # "ramping", mode 1, 1 mA, struggling set ~6.5 s in.
            #
            # Checked on a slow cadence of its own: it is a TPS register read,
            # not something to poll at the loop rate, and the bit needs a few
            # seconds of failed revives to appear anyway.
            if (abs(float(target_ma)) > tolerance_ma
                    and time.monotonic() - last_struggle_check >= 2.0):
                last_struggle_check = time.monotonic()
                sr = self._get("/api/tps-struggling", timeout=5.0)
                rows = (sr.get("struggling") or {}) if sr.get("ok") else {}
                fid = int(self._fid_of(filament))
                for _cid, fids in rows.items():
                    # None = old firmware with no mask. Absent, not empty: it
                    # must not read as "nothing is struggling".
                    if fids and fid in fids:
                        return {"ok": False, "filament": int(filament),
                                "target_ma": float(target_ma),
                                "measured_ma": measured, "measured_valid": valid,
                                "measured_from": source,
                                "elapsed_s": time.monotonic() - start,
                                "present": bool(data.get("present", False)),
                                "cc_mode": data.get("cc_mode"),
                                "arrival": data.get("arrival"),
                                "faulted": False, "cannot_start": True,
                                "error": "the firmware cannot get this output "
                                         "started (TPS 'struggling': 3+ failed "
                                         "revives of a collapsed output). A SHORT "
                                         "looks exactly like this — it never sets "
                                         "the fault bits and its arrival stays "
                                         "'ramping', so nothing else here catches "
                                         "it. Check the load before retrying."}
            # A fault must be CONFIRMED before it ends the wait. A board that
            # trips OCP on the startup inrush and comes up on the next revive is
            # a NORMAL outcome, not a failure -- and it can flash FaultOcp while
            # that is happening. Returning on the first sighting would report a
            # hard fault for a board that recovered a moment later, which is the
            # same single-sample mistake as trusting one current reading.
            #
            # `struggling` is the firmware's own confirmed version of this (3
            # consecutive failed revives) and needs no debounce here; the mode
            # bits are instantaneous, so they do.
            cc_mode = data.get("cc_mode")
            fault_streak = fault_streak + 1 if cc_mode in (2, 3) else 0
            if fault_streak >= self._FAULT_CONFIRM_READS and abs(float(target_ma)) > tolerance_ma:
                return {"ok": False, "filament": int(filament),
                        "target_ma": float(target_ma), "measured_ma": measured,
                        "measured_valid": valid, "measured_from": source,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False)),
                        # `arrival` was missing from THIS return only, so a
                        # faulted result came back without a key the docstring
                        # promises and every other path supplies -- a caller
                        # reading r["arrival"] got None and could not tell
                        # "firmware reports no arrival" from "this return
                        # forgot to include it".
                        "cc_mode": cc_mode, "arrival": data.get("arrival"),
                        "faulted": True,
                        "error": f"CC loop reports this channel FAULTED (cc_mode "
                                 f"{cc_mode}: 2=open filament, 3=OCP/SCP) on "
                                 f"{fault_streak} consecutive reads — not a "
                                 f"transient startup trip, which recovers on the "
                                 f"next revive. Not ramping toward {target_ma} mA."}
            # The polled comparison is now only a FALLBACK, for firmware that
            # does not report arrival. When arrival IS reported it is
            # authoritative: "ramping" means the loop says it has not arrived,
            # and a host-side sample that happens to land inside the tolerance
            # band must not override that. Measured: commanding IDLE 1500 on a
            # cold filament, the inrush passes DOWN through 1569 mA within
            # 0.4 s, so the comparison declared success while the loop was
            # still ramping -- the exact false success this arrival bit exists
            # to remove.
            if arrival is not None:
                ok = False          # settled/capped already returned above
            else:
                ok = valid and abs(measured - target_ma) <= tolerance_ma
            if ok or time.monotonic() >= deadline:
                return {"ok": ok, "filament": int(filament), "target_ma": float(target_ma),
                        "measured_ma": measured, "measured_valid": valid,
                        "measured_from": source,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False)),
                        "cc_mode": data.get("cc_mode", 0),
                        "arrival": data.get("arrival"), "faulted": False}
            time.sleep(poll_interval_s)

    @staticmethod
    def _poll_intervals(first_s: float, cap_s: float, factor: float = 1.6):
        """Sleep durations for a wait loop: responsive at first, then backing
        off geometrically to `cap_s`.

        A fixed short sleep is not a poll rate. Measured on this bench, idle:
        SHV_GET_STATUS is 15 ms and the ESP32's /pulse_events is 65 ms, so a
        `sleep(0.05)` loop is not "20 Hz" -- it is back-to-back requests with a
        gap smaller than the round trip, i.e. as fast as the link will go. That
        matters because both of those loops run WHILE the thing they are
        watching is happening: the SHV poll shares the single RP2350 link with
        the schedule that is firing and with the 20 fps telemetry push, and
        /pulse_events is served by the ESP32's config_portal, which is
        single-threaded with the tcp_bridge relay carrying those very pulses
        (200-300 ms per request under load) -- so polling for pulse events
        slows the path the pulse events arrive on.

        Backing off keeps the first few checks fast (a fault or an immediate
        completion is still caught at once) while a wait that turns out to be
        long costs a request every `cap_s` instead of continuously.
        """
        delay = first_s
        while True:
            yield delay
            delay = min(cap_s, delay * factor)

    def wait_for_currents(self, targets: dict,        # {filament: target mA}
                          tolerance_ma: float = 150.0,
                          timeout_s: float = 10.0,
                          poll_interval_s: float = 0.2) -> dict:
        """Wait for MANY filaments to reach their targets, in ONE polling loop.

        Same rules and the same per-filament result shape as
        wait_for_current() -- firmware `arrival` is authoritative, faults are
        debounced over _FAULT_CONFIRM_READS reads, and `struggling` catches a
        short -- but one bulk read per tick covers the whole batch instead of
        one loop per filament.

        Use this whenever more than one filament is being brought up. The
        cached read is a BULK command: it returns every board's current whether
        you asked for one or ninety-six, so verifying a 35-filament heating
        step one filament at a time costs 35x the link traffic for exactly the
        same data. The struggling check is shared too -- one /api/tps-struggling
        every 2 s for the batch, not one per filament.

        Zero targets are REFUSED here. Confirming ~0 mA needs the live-INA219
        and power-state fallbacks (a stopped board drops its rail, so no
        current reading exists any more -- see wait_for_current()), and those
        are per-board reads that would put back exactly the traffic this
        exists to remove. Use stop_one(verify=True) for those.

        Returns {"ok": all arrived, "results": {filament: <wait_for_current
        shape>}, "pending": [...], "elapsed_s", "polls"}.
        """
        want = {int(f): float(ma) for f, ma in (targets or {}).items()}
        zero = sorted(f for f, ma in want.items() if abs(ma) <= tolerance_ma)
        if zero:
            return {"ok": False, "results": {}, "pending": sorted(want),
                    "elapsed_s": 0.0, "polls": 0,
                    "error": f"filament(s) {zero} have a ~0 mA target; a stop "
                             f"cannot be confirmed by the bulk cached read "
                             f"(the rail drops and the reading disappears). "
                             f"Use stop_one(verify=True) for those."}
        live = {f: ma for f, ma in want.items() if not self._is_dead(f)}
        results: dict[int, dict] = {f: self._dead_result(f) for f in want
                                    if self._is_dead(f)}
        if not live:
            return {"ok": False, "results": results, "pending": [],
                    "elapsed_s": 0.0, "polls": 0,
                    "error": "every requested filament is in the dead mask"}

        start = time.monotonic()
        deadline = start + timeout_s
        last_struggle_check = start
        streaks = {f: 0 for f in live}
        pending = set(live)
        polls = 0

        def finish(f, data, ok, **extra):
            raw = data.get("current_mA")
            return {"ok": ok, "filament": f, "target_ma": live[f],
                    "measured_ma": float(raw) if raw is not None else 0.0,
                    "measured_valid": raw is not None, "measured_from": "cached",
                    "elapsed_s": time.monotonic() - start,
                    "present": bool(data.get("present", False)),
                    "cc_mode": data.get("cc_mode"),
                    "arrival": data.get("arrival"), "faulted": False, **extra}

        while pending:
            rows = self.read_filament_current_cached(sorted(pending))
            polls += 1
            # One struggling read for the whole batch, on its own slow cadence:
            # it is a TPS register read, and the bit needs a few seconds of
            # failed revives to appear at all.
            struggling: set = set()
            if time.monotonic() - last_struggle_check >= 2.0:
                last_struggle_check = time.monotonic()
                sr = self._get("/api/tps-struggling", timeout=5.0)
                if sr.get("ok"):
                    fid_to_user = {int(self._fid_of(f)): f for f in pending}
                    for _cid, fids in (sr.get("struggling") or {}).items():
                        for fid in (fids or []):      # None = no mask (old fw)
                            if fid in fid_to_user:
                                struggling.add(fid_to_user[fid])
            for f in sorted(pending):
                data = rows.get(f) or {}
                arrival = data.get("arrival")
                if arrival == "settled":
                    results[f] = finish(f, data, True); pending.discard(f); continue
                if arrival == "capped":
                    results[f] = finish(f, data, False, capped=True,
                        error=f"CC loop is CAPPED — pinned at the voltage cap "
                              f"with {live[f]} mA unreached. Unreachable at this "
                              f"cap; waiting will not help.")
                    pending.discard(f); continue
                if f in struggling:
                    results[f] = finish(f, data, False, cannot_start=True,
                        error="the firmware cannot get this output started (TPS "
                              "'struggling'). A SHORT looks exactly like this.")
                    pending.discard(f); continue
                cc_mode = data.get("cc_mode")
                streaks[f] = streaks[f] + 1 if cc_mode in (2, 3) else 0
                if streaks[f] >= self._FAULT_CONFIRM_READS:
                    results[f] = finish(f, data, False, faulted=True,
                        error=f"CC loop reports this channel FAULTED (cc_mode "
                              f"{cc_mode}) on {streaks[f]} consecutive reads.")
                    pending.discard(f); continue
                # Fallback for firmware with no arrival bits, same as the
                # single-filament version: compare the sample only when the
                # loop has given no verdict of its own.
                raw = data.get("current_mA")
                if arrival is None and raw is not None \
                        and abs(float(raw) - live[f]) <= tolerance_ma:
                    results[f] = finish(f, data, True); pending.discard(f)
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(poll_interval_s)

        if pending:
            # ONE bulk read for every timed-out filament, not one each -- doing
            # it per filament here would reinstate exactly the N-fold traffic
            # this method exists to remove, on the timeout path where the link
            # is already the likeliest suspect.
            final = self.read_filament_current_cached(sorted(pending))
            for f in sorted(pending):
                results[f] = finish(f, final.get(f) or {}, False,
                                    error=f"did not reach {live[f]} mA within "
                                          f"{timeout_s} s")
        return {"ok": all(r.get("ok") for r in results.values()),
                "results": results, "pending": sorted(pending),
                "elapsed_s": time.monotonic() - start, "polls": polls}

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
                    verify: bool = False,      # confirm the board reports STANDBY
                                                # and report what it actually draws
                    timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """STANDBY a single filament. Returns {"ok": False, "dead": True, ...}
        if the filament is dead — does not raise.

        verify=True confirms the board reports STANDBY and reports the current
        it is actually drawing, under result["standby"].

        It does NOT check the current against a target, because STANDBY has no
        current target: it holds the firmware's 0.8 V floor, and what flows is
        whatever the filament's resistance allows. Measured on a cold filament
        here: 2.1 A of inrush, decaying to ~885 mA steady by ~5 s at 0.78 V.
        This used to verify against a target of 0 mA and report "reached, 0.0 mA"
        while about 2 A was flowing — true only in the sense that it confirmed
        the STATE, and actively misleading about the current. Read
        result["standby"]["current_mA"], and wait ~5 s before calling a STANDBY
        current abnormal.
        """
        r = self._state_one(filament, STANDBY, 0, "standby_one")
        if verify and not r.get("dead"):
            st = self.read_board_status(filament)
            live = (self.read_filament_vi_live(filament).get(int(filament)) or {})
            in_standby = st.get("ok") and st.get("state") == STANDBY
            r = {**r, "standby": {
                "ok": bool(in_standby),
                "state": st.get("state"),
                "current_mA": live.get("current_mA"),
                "bus_mV": live.get("bus_mV"),
                "note": ("STANDBY has no current target — this is what it draws, "
                         "not a pass/fail. Inrush decays for ~5 s."),
            }}
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
                                                   # REQUIRES the filament to be
                                                   # at IDLE already -- see below
                   verify: bool = False,          # poll real measured current
                                                   # after commanding -- see below
                   tolerance_ma: float = 150.0,   # only used if verify=True --
                                                   # passed straight to
                                                   # wait_for_current()
                   timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """Promote a single filament to ACTIVE at `current_ma` mA.

        The filament MUST already be at IDLE. Going straight to ACTIVE is not
        allowed: full firing current into a cold filament damages it, and a
        filament that fails inside the vacuum cannot be repaired. Measured on a
        simulated load, ACTIVE from cold collapsed the output to 0 V for ~10 s
        before the firmware revived it — that is the mechanism, and it is not
        something to confirm on a real filament.

        Walk the ladder instead, and let IDLE SETTLE before promoting (its
        voltage keeps climbing for ~15 s from cold; ACTIVE from a settled IDLE
        takes ~3 s, from an unsettled one it starts far lower and takes longer):

            ct.sleep_one(f); ct.standby_one(f)
            ct.idle_one(f, 1500, verify=True, timeout_s=30)
            ...                                  # let it settle
            ct.active_one(f, 2900, verify=True)

        Returns {"ok": False, "ladder_blocked": True, "error": ...} if the
        filament is not at IDLE, or if this backend does not KNOW its state
        (after a reconnect — unknown refuses rather than allows). Returns
        {"ok": False, "dead": True, ...} if the filament is dead. Never raises.

        verify=True: same real-current feedback as idle_one(verify=True),
        merged under result["heating"].
        """
        # The backend enforces the ladder; this mirrors it so the reason is
        # clear without a round trip, and so a script gets the same answer
        # whether or not the backend is reachable. ACTIVE may only be entered
        # from IDLE: going straight to firing current damages the filament, and
        # in vacuum that damage is unrepairable.
        r = self._state_one(filament, ACTIVE, int(current_ma), "active_one")
        if r.get("ladder_blocked"):
            return r
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
        # on=False is de-energising -- allowed for a dead filament, same reason
        # as STOP above. Refusing it would leave a faulty filament's grid switch
        # closed with no way to open it.
        if on and self._is_dead(filament):
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
        dead = self.dead   # bound once — property, see _live()
        dead_skipped = [] if requested is None else [f for f in requested if f in dead]
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


    def get_slew_rates(self, controller: int = 1) -> dict:
        """Read the three voltage-ramp slew rates, in mV/s.

        Returns {"ok", "below_mV_per_s", "above_mV_per_s", "warm_mV_per_s"}:
        `below` applies under 2 V, `above` above 2 V from cold, `warm` above 2 V
        on a warm restart (the IDLE<->ACTIVE transition a scan actually uses).

        The configured number IS the real instantaneous dV/dt -- no conversion,
        and nothing to scale for display. (An earlier firmware halved it: the
        ramp's step clock was reset on every target increase, and the CC loop
        re-arms a higher target every 20 ms, so 10 ms of accumulated step credit
        was discarded each time -- the ramp stepped on a 20 ms cadence while
        sizing each step for 10 ms. It scaled linearly, so it looked like a
        clean 0.44 constant. It was a bug and it is fixed.)

        A MEASURED full IDLE->ACTIVE transition averages BELOW the setting, and
        that is correct rather than a discrepancy: peak dV/dt never exceeds the
        configured rate, median runs ~91% of it, and the CC loop's fine trim at
        the operating point is deliberately slow.
        """
        r = self._post("/api/cmd", {"controller": int(controller),
                                    "command": "CH_SLEW_RATE"}, timeout=10.0)
        raw = ((r.get("response") or {}).get("raw")) if r.get("ok") else None
        if not raw or len(raw) < 7 or raw[0] != 0:
            return {"ok": False, "error": r.get("error") or "bad CH_SLEW_RATE response"}
        le = lambda o: raw[o] | (raw[o + 1] << 8)
        return {"ok": True, "below_mV_per_s": le(1), "above_mV_per_s": le(3),
                "warm_mV_per_s": le(5)}

    def set_slew_rates(self, below_mV_per_s: int, above_mV_per_s: int,
                       warm_mV_per_s: int, controller: int = 1) -> dict:
        """Set all three slew rates (mV/s). See get_slew_rates for what each is.

        Out-of-range CLAMPS rather than failing (ceilings: below 2000,
        above/warm 5000), so the return is the values actually IN FORCE, not
        what you asked for -- check them, and check `clamped`.

        THE CEILINGS ARE NOT THE DEFAULTS; use SLEW_DEFAULTS for that.
        """
        # Clamp to the u16 wire range here. The firmware clamps to its own
        # ceilings, but a value over 65535 would fail to serialise and the call
        # would error instead of clamping, contradicting the contract the
        # read-back is built on.
        w = lambda v: max(0, min(65535, int(v)))
        asked = (w(below_mV_per_s), w(above_mV_per_s), w(warm_mV_per_s))
        r = self._post("/api/cmd", {"controller": int(controller),
                                    "command": "CH_SLEW_RATE",
                                    "below_mV_per_s": asked[0],
                                    "above_mV_per_s": asked[1],
                                    "warm_mV_per_s": asked[2]}, timeout=10.0)
        raw = ((r.get("response") or {}).get("raw")) if r.get("ok") else None
        if not raw or len(raw) < 7 or raw[0] != 0:
            return {"ok": False, "error": r.get("error") or "bad CH_SLEW_RATE response"}
        le = lambda o: raw[o] | (raw[o + 1] << 8)
        got = (le(1), le(3), le(5))
        return {"ok": True, "below_mV_per_s": got[0], "above_mV_per_s": got[1],
                "warm_mV_per_s": got[2],
                # Compared against what was actually SENT (post-u16 clamp), so a
                # request of 99999 reports clamped rather than being measured
                # against a number that never reached the wire.
                "clamped": got != asked}

    def get_fault_policy(self, controller: int = 1) -> dict:
        """Read the per-run fault policy: two INDEPENDENT stop/continue
        switches for a run that hits trouble.

        - "board": what to do on a CC/OCP hardware fault (0=stop the run,
          1=continue, logging it).
        - "mismatch": what to do on an HC165 shift-register read-back
          mismatch (0=stop, 1=continue).

        Also reports which boards have faulted — the only record of that under
        a "continue" policy. NOTE it is CUMULATIVE: `arm` does not clear it, so
        it answers "which filaments have ever faulted since this controller came
        up", not "which faulted this run". (Verified: the list is identical
        before and after a run with no faults.) The per-run counters --
        mismatches/uncounted/underfed/triggerEdges -- ARE zeroed by arm, and
        shv_status's `faultFilament` is the single filament that stopped the
        current run. Three different questions.

        With unpopulated slots on the bench you almost certainly want board=1:
        the default stops the whole run at the first fault, and an empty slot
        promoted to ACTIVE is an open circuit and faults by definition.
        Measured on a 16-pulse scan with 14 empty slots: board=0 gave
        done=2/16 (Fault), board=1 gave 16/16.

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
                # The CC loop's OWN verdict on whether it got there:
                # "ramping" | "settled" | "capped" | None (not trustworthy).
                # Only the 0x3A cached source carries it.
                "arrival": raw.get("arrival"),
                "cc_mode_raw": raw.get("cc_mode_raw"),   # undecoded byte, for diagnosis
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

        DO NOT POLL THIS TO WATCH A RAMP. It is a LIVE INA219 read over the same
        I2C the CC loop uses to rewrite its setpoint, so sampling it slows the
        thing being sampled. Measured on one IDLE->ACTIVE transition: 3.61 s
        while polling this at 4 Hz, 3.28 s polling the zero-I2C cache, and
        3.0 s not polling at all -- a 20% observer effect that is easy to
        mistake for the loop being slow. Use read_filament_current_cached()
        (0x3A, costs the RP2350 no I2C), or command, sleep, and read once.
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

    def _adopt_loaded_schedule(self) -> None:
        """Seed the reuse pre-filter from the BACKEND's record of what is
        already in each controller's table.

        The backend keeps running between script runs, so it knows a table a
        previous process downloaded. Without this, a fresh process reuses
        nothing -- the second run of a script re-downloads a table the hardware
        already holds, which is the cost this whole fast path exists to remove.

        It only seeds the CHEAP pre-filter. The safety check is unchanged: the
        live CRC is still re-read and compared before any download is skipped,
        so an out-of-date hint costs one extra verify, never a wrong schedule.
        Fetched once per run; a failure just leaves the pre-filter empty, which
        degrades to the old always-download behaviour.
        """
        if self._loaded_fetched:
            return
        self._loaded_fetched = True
        r = self._get("/api/loaded-schedule", timeout=5.0)
        if not r.get("ok"):
            return
        for cid_s, row in (r.get("loaded") or {}).items():
            try:
                cid = int(cid_s)
            except (TypeError, ValueError):
                continue
            plan, crc = row.get("plan"), row.get("crc")
            # The backend stores the WIRE plan (FID space); the comparison in
            # fire_single_pulse is against a wire plan too, so no crossing here.
            # A row without a CRC is not usable as a pre-filter seed: the CRC is
            # what the reuse check compares, and seeding a plan with no CRC
            # would make `controller in self._last_crc` fail anyway.
            if isinstance(plan, dict) and crc is not None:
                self._last_plan[cid] = plan
                self._last_crc[cid] = int(crc)

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
        dead = self.dead   # bound once — property, see _live()
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
                if f in dead:
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
                if f in dead:
                    if f not in dead_skipped:
                        dead_skipped.append(f)
                    continue
                kept_cur[self._fid_of(f)] = v
            out["currents"] = kept_cur
        return out, sorted(dead_skipped)

    # ── Building a full scan plan ────────────────────────────────────────
    # download() takes a plan; it does not build one. The GUI's builder lives
    # in JavaScript (buildSchedule / planHeating / buildPlan in static/app.js),
    # so until now a script had to hand-assemble the dict and re-derive the
    # heating window from reading that JS. build_scan_plan() is that algorithm
    # in Python, so the two produce the same plan for the same inputs.

    # Ring geometry, from the GUI's ct/constants.js. A scan walks a collimator
    # window around a ring of filaments; these say how big the ring and the
    # window are.
    N_FILAMENTS = 96
    COLLIMATOR_COVERAGE = 35      # filaments under the collimator at once
    MAX_SCHEDULE_ROWS = 8192      # firmware schedule cap

    def build_scan_schedule(self, *,
                            mode: str = "stationary",   # or "precision"
                            collimator_center: int = 0,
                            collimator_dir: int = +1,   # +1 CCW ring step, -1 CW
                            filament_dir: int = +1,     # gantry sweep direction
                            gantry_max_deg: float = 10.0,
                            gantry_steps: int = 5,
                            pulses: int = 1,            # burst length per filament
                            width_us: int = 1000,
                            ring_order=None,            # ring position -> filament
                            skip=(),                    # filaments that fire nothing
                            skip_dead: bool = True,
                            max_rows: int | None = None) -> dict:
        """Generate the emission table for a full scan from the ring geometry.

        This is the GUI's buildSchedule/stepScan, which had no API equivalent --
        build_scan_plan() takes an emission list, it does not produce one. Feed
        the "emission" from here straight into build_scan_plan().

        The scan walks a collimator window (COLLIMATOR_COVERAGE filaments wide)
        around the ring. Each step fires the filament at the current position in
        that window; when the window is exhausted it either steps the collimator
        round by one (mode="stationary") or advances the gantry to its next
        angle and only steps the collimator when the gantry reverses at an end
        (mode="precision"). The scan ends when the collimator has been all the
        way round -- N_FILAMENTS ring steps.

        `ring_order` maps a RING POSITION to a filament. It is NOT the client's
        filament_order (USER_INDEX -> FID); that one is applied later, on the
        wire. Conflating them would silently reorder the scan geometry.

        `skip_dead` leaves out filaments in the backend dead mask, which is why
        this is an instance method rather than a static one.

        Returns {"emission", "rows", "truncated", "ring_steps", "triggers"}.
        Precision mode produces a LOT of rows -- roughly
        N * (2*gantry_steps+1) * COVERAGE -- so `truncated` is not an edge case
        there, and it is reported rather than left for you to notice the scan
        ends early.
        """
        if mode not in ("stationary", "precision"):
            raise ValueError("mode must be 'stationary' or 'precision'")
        n = self.N_FILAMENTS
        coverage = self.COLLIMATOR_COVERAGE
        half = (coverage - 1) // 2
        cap = self.MAX_SCHEDULE_ROWS if max_rows is None else int(max_rows)

        steps = max(0, int(gantry_steps))
        n_ang = 2 * steps + 1
        angles = [-gantry_max_deg + 2 * gantry_max_deg * (k / (n_ang - 1))
                  if n_ang > 1 else 0.0 for k in range(n_ang)]

        excluded = {int(f) for f in skip}
        if skip_dead:
            excluded |= set(self.dead)

        window_pos = 0
        coll = int(collimator_center) % n
        g_idx = 0 if filament_dir > 0 else n_ang - 1
        sweep = 1 if filament_dir > 0 else -1
        ring_step = 0
        gantry = angles[g_idx] if mode == "precision" else 0.0

        rows, trig, truncated = [], 0, False
        while ring_step < n:
            if len(rows) >= cap:
                truncated = True
                break
            pos = (coll - half + window_pos) % n
            fil = pos if ring_order is None else int(ring_order[pos])
            if fil not in excluded:
                burst = max(1, int(pulses))
                rows.append({"seq": len(rows), "trigger": trig, "burstLen": burst,
                             "filament": int(fil), "widthUs": int(width_us),
                             "coll": coll, "gantry": gantry,
                             "windowPos": window_pos, "ringStep": ring_step})
                trig += burst
            # stepScan
            window_pos += 1
            if window_pos >= coverage:
                window_pos = 0
                if mode == "stationary":
                    coll = (coll + collimator_dir) % n
                    ring_step += 1
                else:
                    nxt = g_idx + sweep
                    if nxt < 0 or nxt >= n_ang:
                        sweep = -sweep
                        coll = (coll + collimator_dir) % n
                        ring_step += 1
                    else:
                        g_idx = nxt
                    gantry = angles[g_idx]
        return {"emission": rows, "rows": len(rows), "truncated": truncated,
                "ring_steps": ring_step, "triggers": trig,
                "excluded": sorted(excluded)}

    @staticmethod
    def _peak_for_lead(runs, length: int, lead: int, hold: int) -> int:
        """Peak filaments ACTIVE at once for a given pre-heat lead.

        Cyclic difference sweep, monotonic non-decreasing in `lead` -- which is
        what lets the caller binary-search it.
        """
        diff = [0] * (length + 1)
        for first_start, last_end in runs:
            promote = (first_start - lead) % length
            demote = (last_end + hold) % length
            span = (demote - promote) % length or length
            if promote + span <= length:
                diff[promote] += 1
                diff[promote + span] -= 1
            else:
                diff[promote] += 1
                diff[length] -= 1
                diff[0] += 1
                diff[promote + span - length] -= 1
        cur = peak = 0
        for t in range(length):
            cur += diff[t]
            peak = max(peak, cur)
        return peak

    def build_scan_plan(self,
                        emission,              # [{"filament", "trigger",
                                               #   "burstLen"?, "widthUs"?}]
                                               # in trigger order
                        active_count: int = 3,  # peak filaments ACTIVE at once
                        idle_ma: int = 1500,
                        active_ma: int = 2950,
                        rotation_ms: int = 0,   # whole-scan wall time; only
                                                # shapes the config timeouts
                        hold_ms: int = 0,       # stay ACTIVE this long past a
                                                # filament's last pulse
                        width_us: int = 1000,   # default per-entry width
                        repeats: int = 1,
                        no_heat=()) -> dict:    # fire but never heat these
        """Build a full scan plan — the same shape the GUI downloads.

        Returns {"config", "emission", "heating", "currents"} ready for
        download(). USER_INDEX throughout; download() crosses to FID.

        The heating window is the part worth not rewriting by hand. Each
        filament is promoted to ACTIVE some triggers BEFORE its first pulse and
        demoted after its last, and the lead is chosen by binary search as the
        LARGEST one whose peak concurrent-ACTIVE count still fits
        `active_count` -- pre-heat as early as the power budget allows, not a
        fixed number of steps. A filament's window is the arc complementary to
        its largest dark gap, so a filament that fires in two bursts is held
        ACTIVE across the short gap and dropped across the long one.

        `rotation_ms` only feeds the config timeouts (interPulseMs, totalMs);
        the actual pacing comes from the trigger source. Leave it 0 and the
        firmware minimums apply.

        Does NOT pre-heat anything. The deltas run during the schedule; the
        filaments still have to be brought up before arm or arm skips them --
        see fire_single_pulse's note.
        """
        rows = [dict(e) for e in emission]
        if not rows:
            raise ValueError("build_scan_plan: emission is empty")
        for r in rows:
            r.setdefault("burstLen", 1)
            r.setdefault("widthUs", width_us)
        length = max(int(r["trigger"]) + int(r["burstLen"]) for r in rows)
        if active_count < 1:
            raise ValueError("build_scan_plan: active_count must be >= 1")

        pulse_ms = (rotation_ms / length) if (rotation_ms and length) else 0
        hold_bursts = max(1, -(-hold_ms // pulse_ms)) if pulse_ms else 1
        hold_bursts = int(hold_bursts)

        # Each filament's run = the arc complementary to its largest dark gap.
        skip = {int(f) for f in no_heat}
        bursts: dict = {}
        for r in rows:
            f = int(r["filament"])
            if f in skip:
                continue
            bursts.setdefault(f, []).append(
                (int(r["trigger"]), int(r["trigger"]) + int(r["burstLen"])))
        runs, run_fil = [], []
        for f, bs in bursts.items():
            bs.sort()
            gap_at, max_gap = 0, -1
            for i, (_s, e) in enumerate(bs):
                nxt = bs[(i + 1) % len(bs)][0]
                gap = (nxt - e) % length
                if gap > max_gap:
                    max_gap, gap_at = gap, i
            runs.append((bs[(gap_at + 1) % len(bs)][0], bs[gap_at][1]))
            run_fil.append(f)

        lo, hi, lead = 0, length, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._peak_for_lead(runs, length, mid, hold_bursts) <= active_count:
                lead, lo = mid, mid + 1
            else:
                hi = mid - 1

        heating = []
        for (first_start, last_end), f in zip(runs, run_fil):
            heating.append({"filament": f, "triggerIndex": (first_start - lead) % length,
                            "state": ACTIVE, "milliamps": int(active_ma)})
            heating.append({"filament": f, "triggerIndex": (last_end + hold_bursts) % length,
                            "state": IDLE, "milliamps": int(idle_ma)})
        heating.sort(key=lambda d: d["triggerIndex"])

        max_width = max(int(r["widthUs"]) for r in rows)
        return {
            "config": {
                # maxOnMs MUST exceed the widest pulse or arm rejects
                # WidthTooLarge; the other two scale with the scan so a slow
                # run does not trip InterPulseTimeout / TotalTimeout mid-scan.
                "interPulseMs": max(3000, int(-(-pulse_ms * 4 // 1)) if pulse_ms else 0),
                "maxOnMs": max(40, -(-max_width // 1000) + 1),
                "totalMs": max(60000, int(-(-rotation_ms * repeats * 2 // 1)) if rotation_ms else 0),
                "triggerEdge": 0,
            },
            "emission": [{"filament": int(r["filament"]), "numPulses": int(r["burstLen"]),
                          "widthUs": int(r["widthUs"])} for r in rows],
            "heating": heating,
            "currents": {int(f): {"idle_mA": int(idle_ma), "active_mA": int(active_ma)}
                         for f in bursts},
            "_lead_triggers": lead,      # diagnostics, ignored by download()
            "_hold_triggers": hold_bursts,
            "_peak_active": self._peak_for_lead(runs, length, lead, hold_bursts),
        }

    def scan_report(self, controller: int = 1, since: int | None = None,
                    plan: dict | None = None) -> dict:
        """Assemble a post-run report from everything the hardware recorded.

        Three independent sources, which is the point -- each can be complete
        while another is not, and the disagreements are the findings:

          RP2350 pulse log   what FIRED: filament, trigger seq, measured width,
                             and the per-pulse 165 verification
          RP2350 status      the run's counters: done/rbIrqs/edges, uncounted,
                             underfed, unsafeSlots, and the ring health
          STM32 events       what was MEASURED per pulse: envelope width and
                             emission current/charge

        `since` is the pulse-event cursor taken BEFORE the run (pulse_cursor());
        without it the STM32 half covers whatever is still in the ring, which
        may include an earlier run. `plan` is optional and only used to say
        which filaments were expected.

        `heating_at_pulse` is the firmware's snapshot of the filament's heating
        current AT THE INSTANT each pulse fired, which is the number that makes
        an emission reading interpretable -- a shot on a filament that had not
        reached current is not comparable with one on a hot filament. The host
        could never supply it: the ACTIVE window is a few triggers wide, one
        poll round is ~250 ms, and sampling hard enough to align perturbs the
        ramp being sampled. `fired_cold` picks out the pulses that landed below
        their setpoint.
        """
        st = self.shv_status(controller) or {}
        logs = self.shv_pulse_log(controller) or []
        ev = self.pulse_events_ma(since if since is not None else 0)
        events = ev.get("events") or []

        fired = {}
        for r in logs:
            fired[r.get("filament")] = fired.get(r.get("filament"), 0) + 1
        expected = None
        if plan:
            expected = {}
            for e in (plan.get("emission") or []):
                f = int(e["filament"])
                expected[f] = expected.get(f, 0) + int(e.get("numPulses", 1))

        # Anything that makes the run untrustworthy, named rather than left for
        # the reader to notice in a table of counters.
        problems = []
        done, irq = st.get("totalPulsesDone"), st.get("rbIrqs")
        if done is not None and irq is not None and done != irq:
            problems.append(f"totalPulsesDone {done} != rbIrqs {irq} — the host's "
                            f"bookkeeping disagrees with what the hardware fired")
        if st.get("unsafeSlots"):
            u = st["unsafeSlots"]
            slots = [i for i in range(64) if (u >> i) & 1]
            problems.append(f"arm SKIPPED power slots {slots} as unsafe — those "
                            f"filaments did not fire even though the run looks normal")
        if st.get("uncounted"):
            problems.append(f"uncounted={st['uncounted']} — pulses fired that the "
                            f"edge counter missed"
                            + ("" if not st.get("rbSaturated") else
                               " (rbSaturated>0, so this is a LOWER BOUND)"))
        if st.get("underfed"):
            problems.append(f"underfed={st['underfed']} — triggers arrived with "
                            f"nothing staged")
        if st.get("off_mismatches"):
            problems.append(f"off_mismatches={st['off_mismatches']} — THE HV DID "
                            f"NOT TURN OFF on that many pulses")
        if st.get("rbDropped"):
            problems.append(f"rbDropped={st['rbDropped']} — read-back ring "
                            f"overran, verification data was lost")
        stuck = sorted({r["filament"] for r in logs if r.get("hv_stuck_on")})
        if stuck:
            problems.append(f"HV did not turn off on filament(s) {stuck}")
        mism = sorted({r["filament"] for r in logs if r.get("on_mismatch")})
        if mism:
            problems.append(f"read-back did not match the commanded byte on "
                            f"filament(s) {mism}")
        dropped_dead = []
        if expected:
            # Filaments the dead mask removed are EXPECTED to be missing -- the
            # plan was built before the filter ran. Reporting them as a
            # shortfall turns a guard doing its job into an alarm, which is
            # exactly the failure mode this report exists to avoid.
            dead = self.dead
            dropped_dead = sorted(f for f in expected if f in dead)
            short = {f: (n, fired.get(f, 0)) for f, n in expected.items()
                     if f not in dead and fired.get(f, 0) != n}
            if short:
                problems.append(f"fired count differs from the plan for "
                                f"{ {f: f'{g}/{w}' for f, (w, g) in short.items()} }")
        if len(events) != len(logs):
            problems.append(f"{len(logs)} pulses fired but the STM32 measured "
                            f"{len(events)} — measurement is incomplete, so the "
                            f"per-pulse currents do not cover every pulse")

        # HEATING AT PULSE TIME -- the firmware's snapshot, now that it records
        # one. The finding this exists for is a pulse that landed on a filament
        # that had not reached current: commanded 1500 mA, drawing 1 mA. That is
        # a REAL reading from a powered board with an open filament, not a
        # sentinel and not an error, and it is the most important row in a
        # report when it appears.
        cold = []
        heat_unknown = []
        for r in logs:
            m, t = r.get("heat_meas_mA"), r.get("heat_target_mA")
            if m is None:
                heat_unknown.append((r.get("filament"),
                                     r.get("heat_meas_unavailable")))
                continue
            if t is None or t <= 0:
                continue
            if m < t * 0.8:      # 20% short of the setpoint it was told to hold
                cold.append({"filament": r.get("filament"), "seq": r.get("seq"),
                             "meas_mA": m, "target_mA": t,
                             "pct": round(100.0 * m / t, 1)})
        if cold:
            worst = min(cold, key=lambda c: c["pct"])
            problems.append(
                f"{len(cold)} pulse(s) fired on a filament BELOW its heating "
                f"setpoint — worst: filament {worst['filament']} at "
                f"{worst['meas_mA']} mA against {worst['target_mA']} mA "
                f"({worst['pct']}%). The shot happened before the filament was "
                f"hot, so its emission reading is not comparable with the rest.")
        if heat_unknown:
            reasons = sorted({w for _f, w in heat_unknown if w})
            problems.append(
                f"{len(heat_unknown)} pulse(s) have no heating snapshot "
                f"({', '.join(reasons) or 'unknown'}) — those rows cannot be "
                f"compared against the others")

        unverified = sorted({r["filament"] for r in logs if r.get("unverified")})
        widths = [e.get("on_us") for e in events if e.get("on_us") is not None]
        charges = [e.get("integral_mams") for e in events
                   if e.get("integral_mams") is not None]
        return {
            "ok": not problems,
            "problems": problems,
            "fired_pulses": len(logs),
            "fired_by_filament": dict(sorted(fired.items())),
            "expected_by_filament": expected,
            # Named, not silently subtracted: they were in the plan and did not
            # fire, and the reader should see WHY rather than wonder.
            "dropped_dead": dropped_dead,
            "measured_pulses": len(events),
            "unverified_filaments": unverified,   # fired, but no read-back evidence
            "width_us": ({"min": min(widths), "max": max(widths),
                          "n": len(widths)} if widths else None),
            "charge_mams": ({"min": min(charges), "max": max(charges),
                             "n": len(charges)} if charges else None),
            # The firmware's snapshot at fire time. `cold` is the finding:
            # pulses that landed before the filament reached its setpoint.
            "heating_at_pulse": [
                {"filament": r.get("filament"), "seq": r.get("seq"),
                 "meas_mA": r.get("heat_meas_mA"), "target_mA": r.get("heat_target_mA"),
                 "unavailable": r.get("heat_meas_unavailable")}
                for r in logs],
            "fired_cold": cold,
            "status": st,
            "pulses": logs,
            "events": events,
        }

    @staticmethod
    def is_active_at(plan: dict, filament: int, trigger: int) -> bool:
        """Was `filament` ACTIVE at trigger `trigger`, per this plan?

        Answers it from the plan's own deltas rather than from a live read, so
        it works before the run and cannot be perturbed by asking. The window
        wraps: a filament promoted near the end of the timeline is ACTIVE
        through the wrap into the start.
        """
        iv = CTClient.heating_windows(plan).get(int(filament))
        if not iv:
            return False
        length = iv["length"]
        span = (iv["demote"] - iv["promote"]) % length or length
        return (int(trigger) - iv["promote"]) % length < span

    @staticmethod
    def heating_windows(plan: dict) -> dict:
        """Per-filament ACTIVE window, as {filament: {promote, demote, length}}.

        This is the data a Gantt chart draws: when each filament comes up and
        goes back down, on the trigger timeline. Derived from the plan's heating
        deltas, so it describes what WILL happen rather than what a poll caught.
        """
        length = 0
        for e in (plan.get("emission") or []):
            length = max(length, int(e.get("numPulses", 1)))
        # The timeline length is the total trigger count, which for an emission
        # list in trigger order is the sum of the burst lengths.
        length = sum(int(e.get("numPulses", 1)) for e in (plan.get("emission") or [])) or 1
        out: dict = {}
        for d in (plan.get("heating") or []):
            f = int(d["filament"])
            slot = out.setdefault(f, {"promote": None, "demote": None, "length": length})
            if int(d["state"]) == ACTIVE:
                slot["promote"] = int(d["triggerIndex"])
            elif int(d["state"]) == IDLE:
                slot["demote"] = int(d["triggerIndex"])
        return {f: v for f, v in out.items()
                if v["promote"] is not None and v["demote"] is not None}

    def gantt(self, plan: dict, width: int = 72) -> str:
        """Render the schedule as text — the terminal form of the GUI's Gantt.

        One row per filament: `#` where it fires, `=` where it is held ACTIVE,
        and blank where it is off. The point is to see the OVERLAP: how many
        filaments are hot at once, and whether each one is up before its own
        pulse. A count of concurrently-ACTIVE filaments runs underneath.
        """
        emission = plan.get("emission") or []
        if not emission:
            return "(empty plan)"
        length = sum(int(e.get("numPulses", 1)) for e in emission)
        windows = self.heating_windows(plan)
        fires: dict = {}
        t = 0
        for e in emission:
            n = int(e.get("numPulses", 1))
            fires.setdefault(int(e["filament"]), set()).update(range(t, t + n))
            t += n
        scale = max(1, -(-length // width))     # triggers per column
        cols = -(-length // scale)
        lines = [f"trigger 0..{length - 1}"
                 + (f"  ({scale} per column)" if scale > 1 else "")]
        concurrent = [0] * cols
        for f in sorted(set(list(fires) + list(windows))):
            row = []
            for c in range(cols):
                span = range(c * scale, min(length, (c + 1) * scale))
                if any(tt in fires.get(f, ()) for tt in span):
                    row.append("#")
                elif any(self.is_active_at(plan, f, tt) for tt in span):
                    row.append("=")
                    concurrent[c] += 1
                else:
                    row.append(" ")
            # '#' columns are ACTIVE too -- count them, but draw the pulse.
            for c in range(cols):
                span = range(c * scale, min(length, (c + 1) * scale))
                if row[c] == "#" and any(self.is_active_at(plan, f, tt) for tt in span):
                    concurrent[c] += 1
            lines.append(f"  fil {f:>3} |{''.join(row)}|")
        peak = max(concurrent) if concurrent else 0
        lines.append(f"  ACTIVE   |{''.join(str(min(9, c)) if c else '.' for c in concurrent)}|"
                     f"  peak {peak}")
        lines.append("  legend: # pulse   = held ACTIVE   digits = concurrent ACTIVE")
        return "\n".join(lines)

    def validate_plan(self, plan: dict, rotation_ms: int,
                      t_settle_ms: float = 0.0) -> dict:
        """Is every filament ACTIVE long enough before it fires?

        The only check that matters on a scan plan: the pre-heat lead has to be
        at least the filament's settling time, or pulses land on a filament that
        has not reached operating current. `rotation_ms` converts the lead from
        triggers into milliseconds -- the plan itself is in triggers and knows
        nothing about wall time.

        Returns {"ok", "lead_triggers", "lead_ms", "t_settle_ms", "peak_active",
        "problems"}. ok is False when the lead is short, which is a REAL
        finding: build_scan_plan picks the largest lead the concurrency budget
        allows, so a short one means the budget cannot buy enough pre-heat and
        the answer is a higher active_count or a slower rotation, not a retry.
        """
        length = sum(int(e.get("numPulses", 1)) for e in (plan.get("emission") or [])) or 1
        lead = plan.get("_lead_triggers")
        windows = self.heating_windows(plan)
        if lead is None:
            # Not built here -- recover the lead from the first filament's own
            # window rather than refusing to answer.
            leads = []
            t = 0
            for e in (plan.get("emission") or []):
                f = int(e["filament"])
                if f in windows:
                    leads.append((t - windows[f]["promote"]) % length)
                t += int(e.get("numPulses", 1))
            lead = min(leads) if leads else 0
        pulse_ms = rotation_ms / length if (rotation_ms and length) else 0
        lead_ms = lead * pulse_ms
        peak = max((sum(1 for f in windows if self.is_active_at(plan, f, t))
                    for t in range(length)), default=0)
        problems = []
        if t_settle_ms and lead_ms + 1e-6 < t_settle_ms:
            problems.append(
                f"pre-heat lead is {lead_ms:.0f} ms ({lead} triggers) but the "
                f"filament needs {t_settle_ms:.0f} ms to settle — pulses will "
                f"land on filaments that have not reached operating current. "
                f"Raise active_count (buys a longer lead) or slow the rotation.")
        if not rotation_ms and t_settle_ms:
            problems.append("rotation_ms is 0, so the lead cannot be converted "
                            "to milliseconds and the settle check did not run")
        return {"ok": not problems, "lead_triggers": lead, "lead_ms": lead_ms,
                "t_settle_ms": t_settle_ms, "peak_active": peak,
                "problems": problems}

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
                # The WIRE plan (FID space), not the USER_INDEX one we were
                # handed. The hardware holds FIDs, the backend records FIDs, and
                # the reuse check compares against what the hardware holds -- so
                # caching the caller's own numbering here would mismatch the
                # moment a filament_order swap is active, and would make two
                # scripts with different orders but the SAME physical schedule
                # each think the other's table was stale.
                self._last_plan[cid + 1] = wire_plan
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
        wire_plan, dead_skipped = self._plan_to_fids(plan)
        r = self._post("/api/verify-schedule", {"plan": wire_plan}, timeout=10.0)
        # SAY that entries were dropped. Without this a dead filament in the
        # plan makes the counts differ from what the CALLER built -- they built
        # 32 heating entries, 30 were verified -- and the only visible symptom
        # is match=False, which reads as a transfer failure. The dead mask is
        # backend-held and shared, so the entry may have been marked by someone
        # else entirely; nothing in the caller's own code would hint at it.
        if dead_skipped:
            r = {**r, "dead_skipped": sorted(dead_skipped),
                 "note": (f"{len(dead_skipped)} filament(s) were dropped from the plan "
                          f"as dead before it was sent: {sorted(dead_skipped)}. The "
                          f"counts below are for what was ACTUALLY downloaded, which "
                          f"is smaller than what you built -- that is the dead mask "
                          f"working, not a transfer problem. ct.dead_details() says "
                          f"who marked them and why.")}
        return r

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
        """Fired pulse records, ALL of them, paging until the log is exhausted.

        Each record: {filament, seq, tOnUs, durationUs, flags, read165,
        on_mismatch, hv_stuck_on, unverified, heat_meas_mA, heat_target_mA} --
        the last two being the filament's heating current at the instant that
        pulse fired, or None with a *_unavailable reason.

        PAGES, and the page size is not something to assume. The firmware sizes
        a page to fit the link (~242 deliverable bytes), so it shrank from 32 to
        14 records when the record grew from 12 to 16 bytes -- and an oversized
        frame is DROPPED SILENTLY, so a caller that assumed the old size would
        have seen a run simply stop reporting past ~19 pulses. This loops on
        what each page actually returned, against the total the firmware states.

        Returns [] on failure (never raises).
        """
        out: list[dict] = []
        idx = int(start)
        total = None
        for _ in range(512):     # bound: 4096-record log / smallest sane page
            r = self._shv(controller, {"op": "pulse_log", "start": idx})
            page = r.get("records") or []
            if total is None:
                total = r.get("total")
            if not page:
                break            # empty page = nothing further, whatever total says
            out.extend(page)
            idx += len(page)
            if total is not None and len(out) >= int(total):
                break
        for rec in out:
            if "filament" in rec:
                rec["filament"] = self._user_index_of(rec["filament"])
        return out

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

        IT DOES NOT HEAT THE FILAMENT. The schedule it builds carries an EMPTY
        heating table and this method calls nothing in the power-state ladder,
        so the filament stays in whatever state you left it in. What runs end
        to end here is the SCHEDULE path (download -> verify -> arm -> trigger
        -> read back, plus the detector when measure=True), not the whole
        operation.

        Bring the filament up yourself first. At MINIMUM its isolated rail must
        be on, or arm silently SKIPS it -- sleep_one() is enough for that, and
        the result then carries skipped_unsafe. For a real emission measurement
        it has to be at operating current:

            with ct.energised(f):
                ct.sleep_one(f); ct.standby_one(f)
                ct.idle_one(f, 1500, verify=True, timeout_s=30)
                ct.active_one(f, 2950, verify=True)
                r = ct.fire_single_pulse(f, width_us=1000, measure=True)


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
        # Size the relay's abandonment TTL from THIS run rather than taking the
        # firmware default. The firmware renews the TTL on every relayed edge,
        # so an active run cannot be reclaimed -- but the renewal is driven by
        # PULSES, so a gap wider than the TTL still looks abandoned. The binding
        # gap is inter_pulse_ms; give it room, and never go below the firmware
        # default. Also covers the head of the run, before the first pulse.
        arm_ttl_ms = max(self._READY_TTL_FLOOR_MS,
                         int(inter_pulse_ms) * self._READY_TTL_GAP_FACTOR,
                         int(timeout_s * 1000))
        arm = self.ready_arm(rate_hz, ttl_ms=arm_ttl_ms,
                             post_bg_gap_us=post_bg_gap_us,
                             post_bg_n_us=post_bg_n_us)
        if not arm.get("ok"):
            hint = ""
            if "already armed" in str(arm.get("error", "")):
                st = self.ready_status()
                hint = (" — the relay is already armed. The usual cause is a "
                        "previous run that was KILLED between arming and its "
                        "cleanup (the disarm is in a finally, so an exception is "
                        "fine; SIGKILL is not). If nothing else is using it, "
                        "clear it with ct.ready_disarm(). Not stolen "
                        "automatically: the relay is a single global resource "
                        "with no owner recorded, so another client could be "
                        f"mid-run. Current: {st}")
            return {"ok": False, "fired": 0, "records": [], "status": {},
                    "measured": [], "ref_mv": None,
                    "error": f"detector arm failed, nothing fired: "
                             f"{arm.get('error')}{hint}"}
        try:
            # Take the cursor BEFORE firing so we collect only what THIS fire
            # produces and never a stale backlog. Via pulse_cursor() rather
            # than a huge `since`: that shortcut saturates at 2**31-1 in the
            # ESP32's query parsing, and past that id it would stop excluding
            # old events silently. See pulse_cursor().
            since = self.pulse_cursor()
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
        # Backed off rather than hammered: this endpoint is served by the
        # ESP32's config_portal, which shares loop() with the tcp_bridge relay
        # carrying these very pulses (measured 65 ms per request idle,
        # 200-300 ms under load). A tight loop here steals loop() time from the
        # path the events arrive on, so polling harder makes them arrive later.
        naps = self._poll_intervals(0.05, 0.3)
        while len(measured) < want and time.monotonic() < deadline:
            ev = self.pulse_events_ma(cursor)
            if ev.get("ok"):
                ref_mv = ev.get("ref_mv", ref_mv)
                if ev.get("events"):
                    measured.extend(ev["events"])
                    cursor = ev["events"][-1]["id"]
                    naps = self._poll_intervals(0.05, 0.3)   # events flowing: re-arm fast
            if len(measured) < want:
                time.sleep(next(naps))
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
            # plan in USER_INDEX; _last_plan holds the FID form and the reuse
            # so shv_pulse_log()'s re-keying still lines up with the
            # "fired"/"records" filter below.
            "emission": [{"filament": int(filament), "numPulses": int(num_pulses),
                         "widthUs": int(width_us)}],
            "heating": [],
        }

        skip_download = False
        # Why this call did or did not re-download, reported in the result. The
        # fast path is only useful if a caller can SEE it working: wall-clock
        # time cannot distinguish "reused the table" from "the download happened
        # to be cheap", and a silent fast path is one nobody can tell has
        # regressed. Values: "downloaded:first-seen" | "downloaded:plan-changed"
        # | "downloaded:crc-mismatch" | "reused:crc-confirmed" | "reuse-not-requested".
        reuse_note = "reuse-not-requested"
        if reuse:
            self._adopt_loaded_schedule()
        # Compare in FID space -- see the note in download(). _plan_to_fids also
        # drops dead filaments, so what is compared is exactly what would be
        # written, not what was asked for.
        reuse_wire, _reuse_dead = self._plan_to_fids(plan) if reuse else ({}, [])
        if reuse and self._last_plan.get(controller) != reuse_wire:
            reuse_note = ("downloaded:first-seen" if controller not in self._last_plan
                          else "downloaded:plan-changed")
        elif reuse and controller not in self._last_crc:
            reuse_note = "downloaded:no-crc-baseline"
        if reuse and self._last_plan.get(controller) == reuse_wire and controller in self._last_crc:
            # Cheap local pre-filter passed (plan unchanged from what WE last
            # wrote) — now confirm against the ACTUAL hardware CRC, not just
            # entry count, so a different actor's same-size schedule can't
            # slip past undetected. See fire_single_pulse's docstring.
            v = self.verify_schedule(plan)
            row = (v.get("results") or {}).get(str(controller)) or {}
            if (row.get("match") and row.get("crc") is not None
                    and row.get("crc") == self._last_crc.get(controller)):
                skip_download = True   # content confirmed byte-identical — go straight to arm
                reuse_note = "reused:crc-confirmed"
            else:
                reuse_note = "downloaded:crc-mismatch"

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
                    "arm_reject": arm_r.get("reject"),
                    "fired": 0, "records": [], "status": {}, "schedule": reuse_note}

        # A SUCCESSFUL arm can still have silently dropped this filament. Under
        # the CONTINUE fault policy, arm skips a filament that fails its safety
        # gate (IsoOff -- the board's isolated 12 V rail is off), returns
        # reject 0, and runs the rest. The envelope still fires for the counted
        # trigger so the pulse index stays aligned, so the detector records a
        # pulse and every other field looks like a normal shot -- while no HV
        # ever reached the filament.
        #
        # Measured: filament 5 at STOP, arm reject 0, unsafeSlots 0x20 (slot 5),
        # and a measured pulse event. That result was indistinguishable from a
        # real one without this check. SLEEP (iso on) gives unsafeSlots 0.
        st_after = self.shv_status(controller)
        unsafe = st_after.get("unsafeSlots")
        site = self.filament_to_board(filament)
        slot = (site or {}).get("slot")
        if unsafe and slot is not None and (unsafe >> int(slot)) & 1:
            self.shv_disarm(controller)
            return {"ok": False, "fired": 0, "records": [], "status": st_after,
                    "schedule": reuse_note, "skipped_unsafe": True,
                    "error": f"arm accepted the schedule but SKIPPED filament "
                             f"{filament} (power slot {slot}) as unsafe — its "
                             f"board's isolated 12 V rail is off, so no HV would "
                             f"reach it. The run would still fire the envelope "
                             f"for the counted trigger, so this would otherwise "
                             f"look like a successful shot. Bring the rail up "
                             f"(sleep_one({filament}) is enough — it enables iso "
                             f"without heating current) and fire again."}

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
        # This loop runs WHILE the schedule is firing, on the same single
        # RP2350 link that is carrying the run and the 20 fps telemetry push.
        # A flat sleep(0.05) was ~15 requests/s of pure contention (the status
        # round trip is 15 ms, so the sleep was smaller than the request).
        # Fast at first so an immediate fault or a 1-pulse completion is still
        # seen at once, then backing off to 2 requests/s.
        naps = self._poll_intervals(0.05, 0.5)
        while time.monotonic() < deadline:
            st = self.shv_status(controller)
            state = st.get("state", SHV_IDLE)
            if state == SHV_FAULT:
                return {"ok": False,
                        "error": f"SHV fault on controller {controller}: "
                                f"filament {st.get('faultFilament')}, reason "
                                f"{self._SHV_STOP_REASON_NAMES.get(st.get('stopReason'), st.get('stopReason'))}"
                                f" ({st.get('stopReason')})",
                        "fired": 0, "records": [], "status": st, "schedule": reuse_note}
            if state == SHV_COMPLETE:
                logs = self.shv_pulse_log(controller)
                fired = [r for r in logs if r.get("filament") == filament]
                out = {"ok": bool(fired), "fired": len(fired),
                       "records": fired, "status": st, "schedule": reuse_note}
                # HV DID NOT TURN OFF (flags bit 0x02): the OFF read-back came
                # back non-zero. This is the only pulse-log flag that is about
                # the PULSE rather than about the verification of it, and it is
                # the one that matters -- a switch that stayed closed leaves HV
                # on the filament after the pulse. Surfaced at the top level
                # because it was previously invisible: `flags` was a raw byte
                # nobody decoded, so this condition could occur and be reported
                # as a perfectly successful shot.
                stuck = [r.get("filament") for r in fired if r.get("hv_stuck_on")]
                if stuck:
                    out["hv_stuck_on"] = sorted(set(stuck))
                    out["ok"] = False
                    out["error"] = (f"HV DID NOT TURN OFF after the pulse on "
                                    f"filament(s) {sorted(set(stuck))} — the OFF "
                                    f"read-back was non-zero, so the grid switch "
                                    f"may still be closed. Check before firing "
                                    f"again.")
                # Unverified (0x04) is NOT a failure: the pulse fired, the
                # firmware just has no read-back evidence about it. Reported so
                # a caller can tell "verified good" from "no evidence", which
                # the ok flag alone cannot.
                unver = [r.get("filament") for r in fired if r.get("unverified")]
                if unver:
                    out["unverified"] = sorted(set(unver))
                return out
            time.sleep(next(naps))

        self.shv_disarm(controller)
        return {"ok": False, "timeout": True,
                "error": f"timed out after {timeout_s} s (state={state})",
                "fired": 0, "records": [], "status": {}, "schedule": reuse_note}

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
                  ttl_ms: int | None = None,
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
        # Omitted (not 0) when unspecified: the ESP32 uses its own default, and
        # ttl_ms=0 explicitly DISABLES the auto-disarm -- sending 0 to mean
        # "unspecified" would remove the recovery this exists for.
        if ttl_ms is not None:
            body["ttl_ms"] = int(ttl_ms)
        # The wire wants SAMPLES; callers here think in microseconds like every
        # other timing argument in this client, so convert at the boundary using
        # the rate actually being armed. Omitted (not 0) when unspecified -- 0 is
        # a real value on the wire meaning "do not measure the post-pulse
        # background at all", so it must not double as "caller said nothing".
        for key, us in (("post_bg_gap", post_bg_gap_us), ("post_bg_n", post_bg_n_us)):
            if us is not None:
                body[key] = max(0, int(round(float(us) * rate_hz / 1_000_000)))
        return self._post("/api/adc/ready-arm", body)

    def recover(self, stop_heating: bool = False) -> dict:
        """Clear state left behind by an operation that did not finish.

        A killed script, a Ctrl-C, or a backend that exited before its cleanup
        leaves the pulse-envelope relay armed, the STM32 CS claimed, or a
        schedule armed — and every later run then fails with "already armed" or
        an arm reject that reads like a hardware fault. `try/finally` and
        energised() cannot help: they need the process to still be alive.

        Returns {"ok", "was_stuck": bool, "found": {...}, "cleared": [...]}.
        `found` is reported whether or not anything needed clearing, so a
        recurring leak is visible instead of being quietly fixed each time.

        Does NOT de-energise filaments unless stop_heating=True: heat is not
        what gets a later run stuck, and stopping it could interrupt somebody
        else's legitimate run. Energised filaments are listed either way.

        The ESP32 also reclaims an abandoned arm on its own after a timeout
        (60 s by default) — this is the immediate version of that, for when you
        do not want to wait. ready_status()["ttl_expiries"] counts how many
        arms the timeout has had to reclaim; non-zero means some caller's
        cleanup is not running.
        """
        return self._post("/api/recover", {"stop_heating": bool(stop_heating)},
                          timeout=30.0)

    def ready_status(self) -> dict:
        """Whether the pulse-envelope relay is armed, and how many edges it has
        relayed. Useful when an arm is refused as "already armed" -- there is no
        owner recorded, so this is all there is to go on."""
        return self._post("/api/adc/ready-status", {}, timeout=5.0)

    def ready_renew(self) -> dict:
        """Push the relay's auto-disarm deadline out by its TTL.

        Only needed for a run that outlasts the TTL (60 s by default) — an
        ordinary fire finishes well inside it. The timeout exists to reclaim an
        ABANDONED arm, so renewing is the exception, not a keepalive you are
        expected to run."""
        return self._post("/api/adc/ready-renew", {}, timeout=5.0)

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

    def pulse_cursor(self) -> int:
        """Where the pulse log is RIGHT NOW, as a `since` value for later.

        Read this before firing, then pass it to pulse_events() afterwards to
        get only your own events:

            cursor = ct.pulse_cursor()
            ...                                # fire
            r = ct.pulse_events(cursor)

        Implemented as pulse_events(0) and taking `last_id` from the reply,
        which every reply carries regardless of `since`. That costs one
        transfer of the ring (at most 128 events) and is correct for the whole
        id range, forever.

        The obvious alternative -- pass a huge `since` so nothing can be newer,
        get zero events and a truthful last_id -- has a CEILING and is the
        reason this method exists. `pulse_id` is a uint32, but the ESP32 parses
        the query parameter through Arduino's String::toInt(), which returns a
        SIGNED long: values above 2**31-1 saturate rather than wrap (verified
        on the wire -- since=2**32 returns count 0, where a wrap to 0 would have
        returned the whole ring). So no cursor above 2,147,483,647 can be
        expressed at all, and once pulse_id passes that the trick silently stops
        excluding old events instead of failing. At a scan a minute that is
        centuries away; at 1000 pulses/s it is 23 days, and the id only resets
        when the ESP32 reboots.

        Returns 0 if the read fails -- which means "from the beginning", the
        safe direction: you see extra events rather than silently missing yours.
        """
        r = self.pulse_events(0)
        return int(r.get("last_id") or 0) if r.get("ok") else 0

    def pulse_events(self, since: int = 0) -> dict:
        """Poll STM32-measured pulse events NEWER than `since`.

        WHAT `since` IS. The ESP32 keeps a rolling log of measured pulses, each
        with a monotonically increasing `id`, and that log KEEPS GROWING -- it
        is not cleared when you fire. `since` is a cursor into it: you get back
        only events whose id is greater than the number you pass. Without it
        every poll hands you the whole backlog, including pulses from a run an
        hour ago, with no way to tell which ones were yours.

        HOW TO USE IT. Read the cursor BEFORE firing, then poll with it after:

            since = ct.pulse_cursor()      # where the log is now
            ...                            # fire
            r = ct.pulse_events(since)     # only yours

        Then keep `r["last_id"]` and pass it as the next `since`.

        Do not reach for a huge `since` to read the cursor -- see
        pulse_cursor() for why that has a ceiling this does not.

        `since=0` (the default) means "everything still in the log", which is
        rarely what you want. fire_single_pulse(measure=True) does all of this
        for you; this is the manual form for when you fire some other way.

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
        Returns {"ok", "events": [...], "last_id": int}."""
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
        # A BENCH WITH NO ANALOG FRONT END READS ZERO, AND ZERO CONVERTS TO
        # ABOUT -32 mA. The lab test board's STM32 is a bare board: no DS3502,
        # no ADS1115, no AMC3301. There, `i2c_present` is 0x80 (probe valid,
        # all four absent), get_ads1115_ref_mv() returns None, adc_window reads
        # min=0 max=0 with ZERO variance, and every peak/plateau/bg/post_bg is
        # 0 -- so every current here is pulse_ma(0), and integral_mams is 0.0
        # with background_flat True. None of that is a fault to chase; the
        # parts are absent, not broken and not switched off. Emission-current
        # MAGNITUDES can only be measured on a populated board.
        if ref_mv is None:
            live = self.get_ads1115_ref_mv()
            ref_mv = live if live is not None else 1227.0   # last-known-good fallback
        v = raw * 3.3 / 4095 - 0.5 * (ref_mv / 1000.0)
        return 2 * v / self._PULSE_R_SENSE_OHM / self._PULSE_AMC3301_GAIN * 1000

    # Saturation markers the STM32 reports instead of a value it cannot hold.
    # integral is SIGNED, so both ends are markers, and both are REACHABLE: the
    # integral accumulates over the STM32's internal u32 sample count, which is
    # NOT bounded by duration_samples' u16 -- at full scale it hits INT32 in
    # about 520k samples (~0.5 s at 1 MSPS).
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

    # ── Test & measurement flows ──────────────────────────────────────────────
    # Ports of the GUI's "Calibration & Test" tab, so a flow can be run from a
    # script instead of a browser tab that has to stay open. Same thresholds,
    # same order of operations, same numbers -- see each method for where it
    # deliberately differs.
    #
    # Only the two flows that need nothing but the INA219 are here. The other
    # four (emission short scan, focus leak scan, emission current, emission
    # calibration) all need the DS3502s and the ADS1115 to set and read an HV
    # operating point, which the lab board does not have fitted -- they can be
    # written, but not verified, on this bench, so they are not yet written.

    def read_board_faults(self, filaments=None) -> dict:
        """Per-filament TPS55289 fault / HV-overcurrent flags, keyed by
        USER_INDEX. Returns {} on failure (never raises).

        Each flag is True, False, or **None**. None means the firmware marked
        that field's read as invalid (its `*_valid` twin is clear -- a missing
        or faulty chip, or a board built without it), so the flag is UNKNOWN,
        not "no fault". The GUI's test 1 reads the raw `tps_fault` bit without
        consulting its validity twin, which silently turns every unreadable
        board into a passing one; this is that same data with the hole left
        visible.

        Returns {user_index: {"tps_fault", "hv_overcurrent", "present",
        "controller", "channel", "position"}}.
        """
        want = self._want_filaments(filaments)
        mapping = self.get_mapping().get("mapping") or {}
        site_to_user: dict[tuple, int] = {}
        for row in mapping.get("filaments") or []:
            fid = row.get("filament")
            if fid is None or row.get("slot") is None or row.get("controller") is None:
                continue
            site_to_user[(row["controller"] + 1, row["channel"], row["position"])] = \
                self._user_index_of(fid)
        out: dict[int, dict] = {}
        for cid in sorted({site[0] for site in site_to_user}):
            r = self._get(f"/api/board-snapshot?controller={cid}", timeout=20.0)
            if not r.get("ok"):
                continue
            for b in r.get("boards") or []:
                user_index = site_to_user.get((cid, b.get("channel"), b.get("mux_port")))
                if user_index is None:
                    continue
                out[user_index] = {
                    "index": user_index, "controller": cid,
                    "channel": b.get("channel"), "position": b.get("mux_port"),
                    "present": bool(b.get("present")),
                    "tps_fault": (bool(b.get("tps_fault"))
                                  if b.get("tps_fault_valid") else None),
                    "hv_overcurrent": (bool(b.get("hv_overcurrent"))
                                       if b.get("hv_overcurrent_valid") else None),
                }
        if want is not None:
            keep = set(want)
            out = {k: v for k, v in out.items() if k in keep}
        return out

    def _thermal_history_raw(self) -> dict:
        """The raw /api/thermal-history response, so callers can tell an EMPTY
        history (nothing commanded yet) apart from an ABSENT one (a backend too
        old to have the endpoint). Both leave thermal_history() returning {},
        and only one of them is fixed by waiting."""
        return self._get("/api/thermal-history", timeout=10.0)

    def thermal_history(self, filaments=None) -> dict:
        """How long each filament has been de-energised, keyed by USER_INDEX.

        A filament that was just run is still hot, and hot tungsten reads a
        substantially higher resistance than cold tungsten — so "resistance"
        without a thermal precondition is not a repeatable number. This is the
        precondition, read from the backend (which outlives any one script and
        therefore remembers what the previous script left hot).

        Returns {user_index: {"state", "energising", "since_command_s",
        "cold_for_s"}}. `cold_for_s` is None while the filament is still
        energised. A filament MISSING from the result is unknown, not cold —
        the backend only knows what it commanded, so a fresh backend or a
        controller reconnect erases the history. Do not substitute 0 or
        infinity for a missing entry; treat it as "must cool it yourself".
        """
        want = self._want_filaments(filaments)
        r = self._thermal_history_raw()
        out = {}
        for fid_s, row in (r.get("filaments") or {}).items():
            user_index = self._user_index_of(int(fid_s))
            out[user_index] = {**row, "index": user_index}
        if want is not None:
            keep = set(want)
            out = {k: v for k, v in out.items() if k in keep}
        return out

    def cool_down(self, filaments=None, *, cool_s: float,
                  poll_s: float = 2.0, progress=None) -> dict:
        """De-energise `filaments` and wait until every one of them has been off
        for at least `cool_s` seconds. Returns once the precondition holds.

        Credits time already served: a filament the backend says has been at
        STOP for 300 s does not get another wait. Only filaments that are
        actually energised (or whose history is unknown) are commanded to STOP,
        precisely so that a filament already cooling does not have its clock
        reset by a redundant STOP.

        There is no default for `cool_s` anywhere in this client and there
        should not be: the right value is the thermal time constant of the real
        filament assembly in its vacuum envelope, which is a property of the
        production rig and cannot be inferred from the bench's dummy loads.
        Measure it once on the real hardware (sweep_filament_impedance()'s
        `hysteresis` output is the instrument for that) and pass that.

        Returns {"ok", "waited_s", "already_cold_s", "stopped": [...],
        "unknown": [...]}.
        """
        say = progress or (lambda _msg: None)
        targets = self._live_user_indices(filaments)
        if not targets:
            return {"ok": True, "waited_s": 0.0, "already_cold_s": None,
                    "stopped": [], "unknown": []}
        hist = self.thermal_history(targets)
        unknown = [f for f in targets if f not in hist]
        hot = [f for f in targets if (hist.get(f) or {}).get("energising")]
        to_stop = sorted(set(hot) | set(unknown))
        if to_stop:
            say(f"STOP on {len(to_stop)} filament(s) before cooling…")
            self.stop_all(to_stop)
        hist = self.thermal_history(targets)
        # The weakest link sets the wait: one filament that was just running
        # makes the whole batch's measurement warm, not just its own row.
        served = [float((hist.get(f) or {}).get("cold_for_s") or 0.0)
                  for f in targets]
        already = min(served) if served else 0.0
        wait = max(0.0, float(cool_s) - already)
        if wait > 0:
            say(f"cooling {wait:.0f} s (already off {already:.0f} s)…")
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
        return {"ok": True, "waited_s": wait, "already_cold_s": already,
                "stopped": to_stop, "unknown": unknown}

    def _thermal_precondition(self, targets, cool_s, say) -> dict:
        """Shared preamble for the measurement flows: optionally establish, and
        always REPORT, the cold-start precondition. Never silently asserts the
        filament was cold."""
        if cool_s is not None:
            cooled = self.cool_down(targets, cool_s=cool_s, progress=say)
        else:
            cooled = None
        raw = self._thermal_history_raw()
        hist = self.thermal_history(targets)
        known = [float(hist[f]["cold_for_s"]) for f in targets
                 if f in hist and hist[f].get("cold_for_s") is not None]
        unknown = [f for f in targets if f not in hist
                   or hist[f].get("cold_for_s") is None]
        coldest = min(known) if known else None
        if not raw.get("ok"):
            # No endpoint (older backend) or the call failed. Report it as what
            # it is -- the precondition is UNVERIFIABLE, which is not the same
            # as unmet, and must not read as met either.
            return {"required_s": cool_s, "coldest_off_s": None,
                    "unknown_history": list(targets), "met": None,
                    # Truncated: a 404's body is a full HTML error page, and an
                    # unabridged one buries every other line of the report.
                    "note": f"thermal history unavailable: "
                            f"{str(raw.get('error') or 'backend did not answer')[:120]}",
                    "cool_down": cooled}
        if cool_s is None:
            note = ("no cool_s requested — R is whatever temperature these "
                    "filaments happen to be at")
            met = None
        elif unknown:
            note = (f"{len(unknown)} filament(s) have no usable cooling history; "
                    f"cannot confirm the cold start")
            met = None
        else:
            met = coldest is not None and coldest >= float(cool_s)
            note = (f"off for at least {coldest:.0f} s" if met else
                    f"only {coldest:.0f} s off, wanted {float(cool_s):.0f} s")
        return {"required_s": cool_s, "coldest_off_s": coldest,
                "unknown_history": unknown, "met": met, "note": note,
                "cool_down": cooled}

    def measure_filament_resistance(self, filaments=None, *,
                                    settle_s: float | None = None,
                                    short_ohm: float | None = None,
                                    open_ma: float | None = None,
                                    cool_s: float | None = None,
                                    progress=None) -> dict:
        """TEST 1 -- filament resistance at the 0.8 V STANDBY floor.

        Drives every live filament (or just `filaments`) to STANDBY, holds for
        `settle_s`, then takes ONE live INA219 V+I pair per board and reports
        R = V/I with a short/open verdict. Cheap and quick -- this is the
        go/no-go screen you run before anything else; sweep_filament_impedance()
        is the careful version.

        STANDBY is genuinely energised (the firmware's 0.8 V floor, ~0.9 A into
        a real filament), so the whole flow runs inside energised() and every
        touched filament is STOPped on the way out -- including on exception or
        Ctrl-C.

        A per-filament STANDBY failure does NOT abort the run: those filaments
        are reported as `standby_fail` and the rest are still measured. Only a
        STANDBY that applied to nothing at all aborts.

        ## This R is NOT a cold resistance

        It is R at the filament's temperature after `settle_s` at the 0.8 V
        floor, and 0.8 V into a real filament is ~0.7 W, so the filament is
        heating for the whole settle. Run this twice back to back and the
        second run reads higher. That is fine for what this test is -- a
        short/open screen, where the thresholds are orders of magnitude away
        from the drift -- but the number must not be recorded as a filament's
        cold resistance, and two runs' numbers are only comparable if both
        started from the same temperature. For a cold resistance, use
        sweep_filament_impedance(), which extrapolates to zero power.

        `cool_s`: de-energise and wait this long before measuring, so runs
        start from a comparable temperature; None (default) skips the wait. The
        thermal state is REPORTED either way, under "thermal" -- there is no
        invented default here, because the right value is a property of the
        real filament assembly. See cool_down().

        settle_s/short_ohm/open_ma default to the GUI's own box values
        (_T1_SETTLE_S / _T1_SHORT_OHM / _T1_OPEN_MA).
        progress: optional callable(str) for a live line; None = silent.

        Returns {"ok", "pass", "results": {user_index: {...}}, "counts",
        "flagged", "thresholds", "thermal"}. Per filament: "R_ohm" (None when
        no current flowed -- an absent measurement, not a fabricated 0 or
        infinity), "bus_mV", "current_mA", "tps_fault", "verdict"
        ("ok"/"short"/"open"/"standby_fail"), "reason".
        """
        settle_s = self._T1_SETTLE_S if settle_s is None else float(settle_s)
        short_ohm = self._T1_SHORT_OHM if short_ohm is None else float(short_ohm)
        open_ma = self._T1_OPEN_MA if open_ma is None else float(open_ma)
        say = progress or (lambda _msg: None)
        thresholds = {"settle_s": settle_s, "short_ohm": short_ohm, "open_ma": open_ma}

        targets = self._live_user_indices(filaments)
        if not targets:
            return {"ok": False, "error": "no live filaments to measure",
                    "results": {}, "thresholds": thresholds}

        thermal = self._thermal_precondition(targets, cool_s, say)
        with self.energised(*targets):
            say(f"STANDBY on {len(targets)} filament(s)…")
            prep = self.standby_all(targets)
            failed = {int(f) for f in (prep.get("failed") or [])}
            applied = int(prep.get("applied") or 0)
            if applied == 0:
                return {"ok": False,
                        "error": prep.get("error") or "STANDBY applied to nothing",
                        "prep": prep, "results": {}, "thresholds": thresholds,
                        "thermal": thermal}
            say(f"settling {settle_s:.1f} s at STANDBY…")
            time.sleep(settle_s)
            say("reading INA219 V/I…")
            vi = self.read_filament_vi_live(targets)
            faults = self.read_board_faults(targets)

        results: dict[int, dict] = {}
        for f in targets:
            entry = vi.get(f) or {}
            fault = (faults.get(f) or {}).get("tps_fault")
            row = {"index": f, "bus_mV": entry.get("bus_mV"),
                   "current_mA": entry.get("current_mA"),
                   "present": bool(entry.get("present")),
                   "tps_fault": fault, "R_ohm": None,
                   "verdict": None, "reason": None}
            if f in failed:
                row["verdict"], row["reason"] = "standby_fail", "STANDBY was refused"
                results[f] = row
                continue
            if not row["present"]:
                row["verdict"], row["reason"] = "absent", "board not present"
                results[f] = row
                continue
            mA, mV = row["current_mA"], row["bus_mV"]
            if mA is None or mV is None:
                # The live read fell back to cache (a schedule is firing) or the
                # board answered nothing. No pair, no resistance -- do not
                # divide a real voltage by a missing current.
                row["verdict"], row["reason"] = "no_reading", "no live V/I pair"
                results[f] = row
                continue
            # R is left None rather than infinity when no current flows: the
            # verdict already says "open", and an infinity here would not
            # survive a round trip through JSON.
            if mA > 0:
                row["R_ohm"] = (mV / 1000.0) / (mA / 1000.0)
            if fault:
                row["verdict"], row["reason"] = "short", "TPS55289 fault flag set"
            elif row["R_ohm"] is not None and row["R_ohm"] < short_ohm:
                row["verdict"] = "short"
                row["reason"] = f"R {row['R_ohm']:.3f} Ω < {short_ohm} Ω"
            elif mA < open_ma:
                row["verdict"] = "open"
                row["reason"] = f"{mA:.0f} mA < {open_ma} mA at {mV:.0f} mV"
            else:
                row["verdict"] = "ok"
            if fault is None and row["verdict"] == "ok":
                # Passed on R alone; the fault bit could not be read, so say so
                # rather than letting the pass imply it was checked.
                row["reason"] = "TPS fault flag unreadable — verdict from R only"
            results[f] = row

        counts: dict[str, int] = {}
        for row in results.values():
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        flagged = [f"F{row['index']}: {row['verdict'].upper()} ({row['reason']})"
                   for row in results.values()
                   if row["verdict"] not in ("ok", "absent")]
        return {"ok": True, "pass": not flagged, "results": results,
                "counts": counts, "flagged": flagged, "thresholds": thresholds,
                "thermal": thermal,
                "dead_skipped": prep.get("dead_skipped") or []}

    @staticmethod
    def fit_cold_resistance(curve) -> dict:
        """Least-squares fit of a filament V-I curve to V = a·I³ + R₀·I, i.e.
        R(I) = a·I² + R₀ -- so R₀ is the cold (zero-current) resistance and `a`
        is the self-heating coefficient.

        curve: [{"v": volts, "i": amps}, ...]; points with a missing v or i are
        skipped.

        ALWAYS returns a dict -- never a bare number and never None:

            {"R0_ohm": float|None, "a": float|None, "points": int,
             "collinearity": float|None, "r0_pinned": bool,
             "rms_residual_frac": float|None, "reason": str|None}

        `R0_ohm` is None whenever the fit could not resolve it, and `reason`
        says which of the four ways it failed. Nothing here ever reports an
        unresolved R₀ as 0 Ω, because downstream that number is compared
        against a short threshold and a fabricated zero reads as a dead short.

        ## Where this deliberately differs from the GUI's fitR0()

        The maths is identical (verified bit-for-bit against the original JS
        across eight curves, including its R₀ < 0 branch). Two of its outputs
        are not carried over, because both are unresolved fits wearing a
        number:

        1. **Singular normal equations** (every point at the same current --
           what an empty board pinned at the voltage floor produces). fitR0()
           returns `{R0: 0, a: 0}`. Here: R₀ None, reason "same current".
        2. **Ill-conditioned fit.** Over a narrow current range I and I³ are
           nearly the same shape, so the split between R₀ and `a` is not
           identifiable and R₀ lands anywhere -- usually negative, which then
           trips fitR0()'s R₀ < 0 branch and pins it to exactly 0. Measured on
           this bench: a load holding 0.99-1.11 A across an 0.88-1.46 V sweep,
           R = 0.89-1.31 Ω throughout, was fitted as R₀ = 0 Ω and would have
           been reported SHORT. `collinearity` (= Σi⁴² / Σi⁶·Σi², in [0,1],
           1 = indistinguishable) was 0.9916 there against 0.85 for sweeps that
           fit properly, so _T6_MAX_COLLINEARITY sits between them.

        A pinned R₀ = 0 from the surviving R₀ < 0 branch is kept, since it is
        the GUI's documented behaviour, but it is flagged `r0_pinned` so the
        caller can refuse to call it a short.
        """
        pts = [p for p in (curve or [])
               if p.get("i") is not None and p.get("v") is not None]
        out = {"R0_ohm": None, "a": None, "points": len(pts),
               "collinearity": None, "r0_pinned": False,
               "rms_residual_frac": None, "reason": None}
        if len(pts) < CTClient._T6_MIN_FIT_POINTS:
            out["reason"] = (f"only {len(pts)} usable point(s), need "
                             f"{CTClient._T6_MIN_FIT_POINTS}")
            return out
        sI6 = sI4 = sI2 = sI3V = sIV = 0.0
        for p in pts:
            i = float(p["i"]); v = float(p["v"])
            i2 = i * i; i3 = i2 * i
            sI6 += i3 * i3; sI4 += i2 * i2; sI2 += i2
            sI3V += i3 * v; sIV += i * v
        det = sI6 * sI2 - sI4 * sI4
        if not det or not (sI6 * sI2):
            out["collinearity"] = 1.0
            out["reason"] = "every point at the same current — R₀ not resolvable"
            return out
        # Cauchy-Schwarz bounds this at 1; it reaches 1 exactly when I and I³
        # are proportional over the sampled currents, i.e. when the sweep never
        # moved the current.
        out["collinearity"] = sI4 * sI4 / (sI6 * sI2)
        if out["collinearity"] >= CTClient._T6_MAX_COLLINEARITY:
            currents = [float(p["i"]) for p in pts]
            span = (max(currents) - min(currents)) / max(currents) * 100
            out["reason"] = (f"current moved only {span:.0f}% over the sweep "
                             f"(collinearity {out['collinearity']:.4f} ≥ "
                             f"{CTClient._T6_MAX_COLLINEARITY}) — R₀ and the I² "
                             f"term are not separable")
            return out
        a = (sI3V * sI2 - sI4 * sIV) / det
        r0 = (sI6 * sIV - sI4 * sI3V) / det
        if r0 < 0:
            r0 = 0.0
            a = (sI3V / sI6) if sI6 else 0.0
            out["r0_pinned"] = True
            out["reason"] = "fitted R₀ was negative — pinned to 0, not measured"
        out["R0_ohm"], out["a"] = r0, a
        # How well the model actually describes this load, as an RMS residual
        # relative to V. Reported, NOT gated on: a threshold would need a
        # population of real filaments to calibrate, and there isn't one on this
        # bench. It exists because R₀ alone looks equally authoritative whether
        # the curve is a tungsten filament or something the a·I²+R₀ form does
        # not fit at all -- the bench's current-limited dummy load fits to 22%
        # and still yields a tidy-looking R₀.
        ss = 0.0
        for p in pts:
            i = float(p["i"]); v = float(p["v"])
            ss += ((a * i * i * i + r0 * i) - v) ** 2 / (v * v) if v else 0.0
        out["rms_residual_frac"] = (ss / len(pts)) ** 0.5
        return out

    def sweep_filament_impedance(self, filaments=None, *,
                                 start_mv: int | None = None,
                                 end_mv: int | None = None,
                                 step_mv: int | None = None,
                                 dwell_s: float | None = None,
                                 short_ohm: float | None = None,
                                 cool_s: float | None = None,
                                 hysteresis_tol: float | None = None,
                                 save: bool = True,
                                 progress=None) -> dict:
        """TEST 6 -- per-filament impedance sweep, fitted to a cold resistance.

        For each live filament in turn: hold VOLTAGE mode at start_mv, dwell,
        read the INA219, step up by step_mv, repeat to end_mv, STOP, then fit
        the collected V-I curve with fit_cold_resistance(). One filament is
        energised at a time, and it is STOPped before the next one starts.

        SLOW -- one filament's sweep is roughly
        `dwell_s * (1 + (end_mv - start_mv) // step_mv)` seconds, so all 96 at
        the defaults is well over an hour. Pass `filaments` to sweep a subset.

        Unlike measure_filament_resistance(), which divides one V by one I at a
        single operating point, this fits a whole curve, so it separates the
        cold resistance R₀ from the self-heating term -- the number you want
        when comparing filaments to each other.

        ## R₀ is a cold resistance only if the filament was actually cold

        R₀ is the fit's extrapolation to zero dissipated power, so it equals the
        room-temperature resistance only when the sweep both STARTS at ambient
        and stays in thermal equilibrium throughout. A filament that ran
        recently is still hot and reads high; one swept faster than it can shed
        heat climbs during the sweep. Neither shows up in the fit -- both just
        move R₀, and the GUI's version reports the result as a cold resistance
        either way.

        Two independent guards, because they fail differently:

        - `cool_s` (default None = no wait): de-energise and wait this long
          before sweeping, crediting time already served. Establishes the start
          condition. Reported under "thermal".
        - The **return point** (always taken): after the last step the sweep
          goes back to `start_mv`, re-measures, and compares R with the opening
          point. This TESTS equilibrium instead of assuming it, and needs no
          knowledge of the filament's thermal constant. Drift beyond
          `hysteresis_tol` (default _T6_HYSTERESIS_TOL) marks R₀ not-cold.

        Per filament, `r0_is_cold` is True / False / None (unverified), with
        `cold_note` saying why, and the run-level `r0_not_cold` lists every
        filament whose R₀ fitted but is not a cold resistance. A not-cold R₀ is
        still returned -- it is a real measurement at an unknown temperature --
        but it is never silently labelled as cold.

        start_mv is clamped up to the firmware's 0.8 V floor: below it the
        regulator does not start at all, so a lower request would silently
        collect points that are all the same voltage.

        save=True writes the curves and fits to the backend's calibration
        directory as `impedance_sweep_<timestamp>.json` + `.csv`.
        progress: optional callable(str); None = silent.

        Returns {"ok", "results": {user_index: {"R0_ohm", "a", "verdict",
        "curve": [{"v","i","mv_set"}], "points"}}, "params", "counts",
        "flagged", "saved"}. A filament whose curve could not be fitted gets
        "R0_ohm": None and verdict "no_fit" -- never a placeholder number.
        """
        start_mv = self._T6_START_MV if start_mv is None else int(start_mv)
        end_mv = self._T6_END_MV if end_mv is None else int(end_mv)
        step_mv = self._T6_STEP_MV if step_mv is None else int(step_mv)
        dwell_s = self._T6_DWELL_S if dwell_s is None else float(dwell_s)
        short_ohm = self._T6_SHORT_OHM if short_ohm is None else float(short_ohm)
        hysteresis_tol = (self._T6_HYSTERESIS_TOL if hysteresis_tol is None
                          else float(hysteresis_tol))
        say = progress or (lambda _msg: None)

        start_mv = max(self._T6_START_MV, start_mv)
        if step_mv <= 0:
            return {"ok": False, "error": f"step_mv={step_mv} must be positive",
                    "results": {}}
        if end_mv < start_mv:
            return {"ok": False,
                    "error": f"end_mv={end_mv} is below start_mv={start_mv}",
                    "results": {}}
        steps = list(range(start_mv, end_mv + 1, step_mv))
        params = {"start_mv": start_mv, "end_mv": end_mv, "step_mv": step_mv,
                  "dwell_s": dwell_s, "short_ohm": short_ohm,
                  "cool_s": cool_s, "hysteresis_tol": hysteresis_tol,
                  "steps_per_filament": len(steps)}

        targets = self._live_user_indices(filaments)
        if not targets:
            return {"ok": False, "error": "no live filaments to sweep",
                    "results": {}, "params": params}

        thermal = self._thermal_precondition(targets, cool_s, say)
        results: dict[int, dict] = {}
        # One energised() around the whole run, not one per filament: if the
        # loop dies partway through, the filament being swept AND any earlier
        # one whose STOP did not land both still get stopped.
        with self.energised(*targets):
            for n, f in enumerate(targets, 1):
                curve = []
                for mv in steps:
                    say(f"F{f} ({n}/{len(targets)}) @ {mv} mV…")
                    r = self.voltage_one(f, mv)
                    if not r.get("ok"):
                        curve.append({"mv_set": mv, "v": None, "i": None,
                                      "error": r.get("error") or "set failed"})
                        continue
                    time.sleep(dwell_s)
                    entry = self.read_filament_vi_live([f]).get(f) or {}
                    mA, mV = entry.get("current_mA"), entry.get("bus_mV")
                    if entry.get("present") and mA is not None and mV is not None and mA > 0:
                        curve.append({"mv_set": mv, "v": mV / 1000.0, "i": mA / 1000.0})
                    else:
                        # Kept in the curve with v/i None so the record shows the
                        # step was attempted; fit_cold_resistance() skips it.
                        curve.append({"mv_set": mv, "v": None, "i": None})
                # Return to the FIRST voltage and re-measure. If the filament is
                # at the same temperature as when the sweep opened, this reads
                # the same R; if the sweep heated it faster than it could shed
                # the heat, it reads higher, and by how much. This is the only
                # thing here that TESTS the fit's premise rather than assuming
                # it -- and unlike a cool-down time, it needs no prior knowledge
                # of the filament's thermal constant, so it works on any rig.
                hysteresis = None
                first = next((p for p in curve if p.get("i")), None)
                if first is not None:
                    say(f"F{f}: return to {steps[0]} mV for the hysteresis check…")
                    rr = self.voltage_one(f, steps[0])
                    if rr.get("ok"):
                        time.sleep(dwell_s)
                        e = self.read_filament_vi_live([f]).get(f) or {}
                        mA, mV = e.get("current_mA"), e.get("bus_mV")
                        if e.get("present") and mA and mV and mA > 0:
                            r_open = first["v"] / first["i"]
                            r_back = (mV / 1000.0) / (mA / 1000.0)
                            hysteresis = {
                                "mv_set": steps[0], "v": mV / 1000.0,
                                "i": mA / 1000.0,
                                "r_open_ohm": r_open, "r_return_ohm": r_back,
                                # Positive = came back hotter than it started.
                                "drift_frac": (r_back - r_open) / r_open if r_open else None,
                            }
                self.stop_one(f)
                fit = self.fit_cold_resistance(curve)
                row = {"index": f, "curve": curve, "points": fit["points"],
                       "hysteresis": hysteresis,
                       "R0_ohm": fit["R0_ohm"], "a": fit["a"],
                       "collinearity": fit["collinearity"],
                       "r0_pinned": fit["r0_pinned"],
                       "rms_residual_frac": fit["rms_residual_frac"],
                       "verdict": "no_fit", "reason": fit["reason"]}
                if fit["R0_ohm"] is None:
                    pass                    # reason already explains which way
                elif fit["r0_pinned"]:
                    # R₀ = 0 here means "the fit wanted a negative one", not a
                    # measured 0 Ω. Calling that SHORT is the false alarm this
                    # whole path exists to avoid.
                    row["verdict"] = "no_fit"
                elif fit["R0_ohm"] < short_ohm:
                    row["verdict"] = "short"
                    row["reason"] = f"R₀ {fit['R0_ohm']:.4f} Ω < {short_ohm} Ω"
                else:
                    row["verdict"], row["reason"] = "ok", None
                # Whether the R0 that came out is a COLD resistance is a
                # separate question from whether the fit converged, and is
                # tracked separately so neither can stand in for the other.
                drift = (hysteresis or {}).get("drift_frac")
                if row["R0_ohm"] is None:
                    row["r0_is_cold"] = None
                elif drift is None:
                    row["r0_is_cold"] = None
                    row["cold_note"] = "no return point — cold start unverified"
                elif abs(drift) > hysteresis_tol:
                    row["r0_is_cold"] = False
                    row["cold_note"] = (
                        f"R at {steps[0]} mV drifted {drift * 100:+.1f}% over the "
                        f"sweep (tol ±{hysteresis_tol * 100:.0f}%) — the filament "
                        f"did not stay at one temperature, so R₀ is not a cold "
                        f"resistance")
                elif thermal["met"] is False:
                    row["r0_is_cold"] = False
                    row["cold_note"] = f"warm start: {thermal['note']}"
                elif thermal["met"] is None:
                    row["r0_is_cold"] = None
                    row["cold_note"] = f"cold start unconfirmed: {thermal['note']}"
                else:
                    row["r0_is_cold"] = True
                    row["cold_note"] = None
                results[f] = row
                say(f"F{f}: " + (f"R₀ = {row['R0_ohm']:.4f} Ω"
                                 if row["R0_ohm"] is not None else "no fit"))

        counts: dict[str, int] = {}
        for row in results.values():
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        flagged = [f"F{row['index']}: {row['verdict'].upper()} ({row['reason']})"
                   for row in results.values() if row["verdict"] != "ok"]
        not_cold = [row["index"] for row in results.values()
                    if row.get("R0_ohm") is not None and row.get("r0_is_cold") is not True]
        out = {"ok": True, "pass": not flagged, "results": results,
               "params": params, "counts": counts, "flagged": flagged,
               "thermal": thermal, "r0_not_cold": sorted(not_cold),
               "saved": None}
        if save:
            out["saved"] = self.save_calibration("impedance_sweep", {
                "params": params,
                "curves": {str(k): v["curve"] for k, v in results.items()},
                "r0": {str(k): v["R0_ohm"] for k, v in results.items()},
                "a": {str(k): v["a"] for k, v in results.items()},
                # Saved alongside R0 on purpose: a stored cold resistance with
                # no record of whether the filament was cold is not a
                # calibration, it is a number.
                "r0_is_cold": {str(k): v.get("r0_is_cold") for k, v in results.items()},
                "hysteresis": {str(k): v.get("hysteresis") for k, v in results.items()},
                "thermal": thermal,
            })
        return out

    def save_calibration(self, name: str, data: dict) -> dict:
        """Write a calibration/measurement record to the backend's host disk as
        `<name>_<timestamp>.json` plus a flat `.csv` of `data["curves"]`.

        The file lands next to backend.py (its `calibration/` directory), NOT
        next to the calling script -- the backend is what owns the disk here.
        `name` is sanitised by the backend to [A-Za-z0-9._-].

        Returns {"ok", "json": path, "csv": path, "filaments"}; never raises.
        """
        return self._post("/api/calibration/save",
                          {"name": str(name), "data": data}, timeout=20.0)

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
