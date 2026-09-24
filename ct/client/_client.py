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
        #     ct.simulate_sync(count=1)          # fires through the master
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

from ._base import *  # noqa: F401,F403 -- this module IS ct_simple_control:
#   every shared name stays reachable here, as it was in the single file
from ._power import _PowerMixin
from ._hv import _HvMixin
from ._schedule import _ScheduleMixin
from ._measure import _MeasureMixin
from ._emission import _EmissionMixin
from ._diagnostics import _DiagnosticsMixin
from ._decode import _DecodeMixin


class CTClient(_PowerMixin, _HvMixin, _ScheduleMixin, _MeasureMixin, _EmissionMixin, _DiagnosticsMixin, _DecodeMixin):
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
        record: bool = True,          # append every call and its FULL result
                                       # to logs/client/ct_client_<date>.jsonl
                                       # -- see _record_call(). False = off
        record_dir: str | None = None,  # where; None = <repo>/logs/client (ct.paths)
        keepalive: bool = True,       # renew the backend's dead-man watchdog
                                       # in the background for as long as this
                                       # client is alive and has energised
                                       # something. See _ensure_keepalive();
                                       # False means you renew it yourself, or
                                       # accept the fallback firing mid-run.
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
        self.record = bool(record)
        self.record_dir = (Path(record_dir) if record_dir is not None
                           else CLIENT_LOG_DIR)
        self._record_lock = threading.Lock()
        self._record_last: dict = {}      # method -> [signature, monotonic, suppressed]
        self._record_warned = False
        self.timeout = timeout
        self._s = requests.Session()
        # Dead-man keepalive state. Armed lazily by _ensure_keepalive() the
        # first time this client energises anything, then left running: it is
        # one small POST every few seconds, and the alternative is remembering
        # to renew in every long-running flow.
        self._keepalive_enabled = bool(keepalive)
        self._keepalive_lock = threading.Lock()
        self._keepalive_stop = None
        # Result of the most recent session() teardown; None until one runs.
        self.last_teardown = None
        self.lease_lost = None       # set by lease() if a renewal found it taken
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
        # The BACKEND is the authority; these are this client's SNAPSHOT of it,
        # taken once on first use. None = not fetched yet (lazy, so constructing
        # a CTClient still costs no network round trip).
        self._order: dict[int, int] | None = None
        self._order_rev: dict[int, int] = {}
        self._order_epoch: str | None = None
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
        # Client/backend version check, once, on the first request (lazy for
        # the same reason as the filament order: constructing a CTClient costs
        # no round trip). The backend is long-lived and does not update itself
        # while running, so a freshly updated script can meet an older one.
        self._version_checked = False

    def _check_backend_version(self) -> None:
        """Warn once if this client and the backend run different commits."""
        self._version_checked = True
        try:
            theirs = self._s.get(self.base + "/api/version", timeout=2.0).json()
        except Exception:
            return          # unreachable or an old backend without /api/version
        mine = ct_update.version()
        # By CONTENT (tree hash), not commit: the published repo's commits are
        # rewritten copies of the development repo's, same code, other hashes.
        if mine.get("tree") and theirs.get("tree") and mine["tree"] != theirs["tree"]:
            print(f"[ct_simple_control] version mismatch: this client is "
                  f"{mine['commit']} (code {mine['tree']}), the backend at {self.base} "
                  f"is {theirs.get('commit')} (code {theirs['tree']}) -- update and "
                  f"restart backend.py.", file=sys.stderr)

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
            return Result(data)
        return Result({"ok": r.ok, "data": data})

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

    # ── the call record (logs/client/*.jsonl) ─────────────────────────────────
    # backend.log audits what was COMMANDED. The results -- measured currents,
    # pulse events, verdicts, everything computed here in the client -- only
    # ever existed in a script's stdout. Every public call now appends one JSON
    # line with its arguments and its complete result, so a run can be read
    # back afterwards:
    #
    #     import json; from ct_simple_control import Result
    #     for line in open("logs/client/ct_client_2026-09-23.jsonl"):
    #         rec = json.loads(line)
    #         print(rec["t"], rec["method"], Result(rec["result"] or {}))

    RECORD_DEDUP_S = 5.0     # same method, args and result within this: counted, not re-written

    @property
    def record_path(self) -> Path:
        """Today's record file (it rolls over at midnight, local time)."""
        return self.record_dir / f"ct_client_{time.strftime('%Y-%m-%d')}.jsonl"

    def _record_call(self, method: str, args, kwargs, t0: float,
                     out=None, exc: BaseException | None = None) -> None:
        """Append one call to the record. Never raises: a record that cannot
        be written must not take the call it describes down with it -- it
        warns once on stderr instead."""
        if not self.record:
            return
        try:
            call = {"method": method, "args": list(args), "kwargs": dict(kwargs)}
            if exc is not None:
                body = {"exception": f"{type(exc).__name__}: {exc}"}
            else:
                body = {"ok": out.get("ok") if isinstance(out, dict) else None,
                        "result": out}
            sig = json.dumps({**call, **body}, default=repr, sort_keys=True,
                             ensure_ascii=False)
            now = time.monotonic()
            with self._record_lock:
                last = self._record_last.get(method)
                if last and last[0] == sig and now - last[1] < self.RECORD_DEDUP_S:
                    last[1] = now
                    last[2] += 1
                    return
                repeats = last[2] if (last and last[0] == sig) else 0
                self._record_last[method] = [sig, now, 0]
                rec = {"t": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t0))
                            + f".{int((t0 % 1) * 1000):03d}",
                       "client": self.client_id, "backend": self.base,
                       **call, "duration_s": round(time.time() - t0, 3), **body}
                if repeats:
                    rec["identical_before_not_recorded"] = repeats
                self.record_dir.mkdir(parents=True, exist_ok=True)
                with open(self.record_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, default=repr, ensure_ascii=False) + "\n")
        except Exception as e:     # noqa: BLE001 -- see the docstring
            if not self._record_warned:
                self._record_warned = True
                print(f"[ct_simple_control] call record not written ({e}); "
                      f"further failures are silent. record=False turns it off.",
                      file=sys.stderr)

    def _post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        if not self._version_checked:
            self._check_backend_version()
        r = self._one_post(path, body, timeout)
        attempt = 0
        while not r.get("ok") and self._is_transient(r) and attempt < self.max_retries:
            time.sleep(self.retry_delay_s * (2 ** attempt))
            attempt += 1
            r = self._one_post(path, body, timeout)
        if attempt:
            r["retries"] = attempt
        # Wrapped HERE, not only in _parse(): the refusals and connection
        # errors built locally never touch _parse, and those are exactly the
        # results a reader is squinting at.
        return r if isinstance(r, Result) else Result(r)

    def _get(self, path: str, timeout: float | None = None) -> dict:
        if not self._version_checked:
            self._check_backend_version()
        r = self._one_get(path, timeout)
        attempt = 0
        while not r.get("ok") and self._is_transient(r) and attempt < self.max_retries:
            time.sleep(self.retry_delay_s * (2 ** attempt))
            attempt += 1
            r = self._one_get(path, timeout)
        if attempt:
            r["retries"] = attempt
        return r if isinstance(r, Result) else Result(r)

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

    # ── the backend machine's logs, over the LAN ─────────────────────────────
    # backend.py may run on another computer. These read ITS logs/ directory:
    # backend.log (+ dated roll-overs) and the call records of scripts that
    # ran on THAT machine (client/ct_client_<date>.jsonl). A script running
    # here writes its own records here -- see record_path. Read-only; no lease.

    def list_logs(self) -> dict:
        """Log files on the backend's machine.

            ct.list_logs()   ->  {"ok", "log_dir", "files": [{"path", "size", "mtime"}]}

        Pass a "path" from here to read_log()."""
        return self._get("/api/logs", timeout=10.0)

    def read_log(self, path: str = "backend.log",   # as listed by list_logs()
                 tail: int = 200,                    # last N lines (after grep), <= 20000
                 grep: str | None = None) -> dict:   # keep only lines containing this
        """Read one log file on the backend's machine: its last `tail` lines.

            ct.read_log()                                         # backend.log, last 200
            ct.read_log(grep="WARNING", tail=50)                  # its warnings
            ct.read_log("client/ct_client_2026-09-24.jsonl", tail=20)
            ct.read_log("client/ct_client_2026-09-24.jsonl", grep='"fire_single_pulse"')

        Returns {"ok", "path", "size", "lines", "matched", "returned"}. A .jsonl
        file also gets "records": each line parsed (a line that is not JSON
        stays as its text under {"unparsed": ...}) -- so a record's result can
        be read the same way as a live one: Result(rec["result"]).

        Only the last 8 MB of a file are scanned; "scanned_from_byte" > 0 says
        older content was not searched."""
        from urllib.parse import urlencode
        q = {"path": path, "tail": int(tail)}
        if grep:
            q["grep"] = grep
        r = self._get("/api/logs/read?" + urlencode(q), timeout=30.0)
        if r.get("ok") and str(r.get("path", "")).endswith(".jsonl"):
            recs = []
            for ln in r.get("lines") or []:
                try:
                    recs.append(json.loads(ln))
                except ValueError:
                    recs.append({"unparsed": ln})
            r["records"] = recs
        return r

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
        boards and return the filaments found, in YOUR numbering
        (USER_INDEX), same as every other read on this client.

        The wire carries FIDs; they are translated here, which is the whole
        reason this does not just return the response. Handing FIDs back
        would break the documented use below in the worst possible way:
        set_dead() takes USER_INDEX and maps outbound, so feeding it FIDs
        maps them a second time and marks a DIFFERENT set of filaments dead
        -- silently, and only once a non-identity order is installed, so it
        tests clean on the bench that has no remapping.

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
        return [self._user_index_of(int(f)) for f in (r.get("present") or [])]

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
        # The backend clamps ttl to LOCK_TTL_MAX_S (600) silently. A script that
        # asked for an hour and got ten minutes would find the lease gone
        # mid-run with nothing having said so, so surface the granted value and
        # flag the difference rather than echoing the request back.
        granted = (r.get("lock") or {}).get("expires_in_s")
        if r.get("ok") and granted is not None and float(granted) < float(ttl) - 1.0:
            r["ttl_requested_s"] = float(ttl)
            r["ttl_granted_s"] = float(granted)
            r["ttl_clamped"] = True
            r["note_ttl"] = (f"asked for {float(ttl):.0f} s, granted "
                             f"{float(granted):.0f} s (backend maximum) — renew "
                             f"before it expires or the lease drops mid-run")
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
        it outlives the script -- and it now does: its dead-man watchdog walks
        an unattended ACTIVE filament back to SLEEP and opens every HV grid
        MOSFET (see safety()). It never turns the emission/focus rails off. The lease still de-energises nothing; it only gates writes.

        While this block is open a keepalive runs in the background, because
        the watchdog is deliberately renewed by COMMANDS and not by reads: code
        that legitimately sits at ACTIVE for a minute without commanding
        anything -- a settle, a long measurement -- would otherwise be walked
        back mid-run by the very thing protecting it.
        """
        stop_keepalive = self._start_keepalive(filaments)
        body_exc = None
        try:
            yield self
        except BaseException as exc:
            body_exc = exc
            raise
        finally:
            stop_keepalive()
            # Deliberately not conditional on success, and each filament is
            # attempted even if an earlier one errors: the whole point is that
            # this path runs when something has already gone wrong.
            #
            # And each result is CHECKED. This was `try: stop_one() except
            # Exception: pass` -- dead code (stop_one returns, it does not
            # raise) wrapped around a discarded return value, so a filament
            # that would not stop read exactly like one that had. Two ways to
            # fail, both reported: the STOP never landed (ok:False), or it
            # landed and the current did not come down (verify's heating.ok
            # False) -- the second is the dangerous one, because everything
            # upstream believes the filament is off.
            failed = {}
            for f in filaments:
                name = f"filament {int(f)}"
                try:
                    r = self.stop_one(int(f), verify=verify)
                except Exception as exc:          # defensive: should not raise
                    failed[name] = f"raised {type(exc).__name__}: {exc}"
                    continue
                if not r.get("ok"):
                    failed[name] = f"STOP did not land — {r.get('error') or 'no reason given'}"
                    continue
                h = r.get("heating")
                if verify and isinstance(h, dict) and not h.get("ok"):
                    failed[name] = (f"STOP landed but the current did not come down "
                                    f"({h.get('measured_ma')} mA, arrival="
                                    f"{h.get('arrival')}) — it may still be heating")
            self._report_teardown("energised()", failed,
                                  [f"filament {int(f)}" for f in filaments], body_exc)

    @contextmanager
    def lease(self, ttl: float = 60.0, note: str = ""):
        """Hold the bench's write lease for the whole `with` block.

            with ct.lease(note="emission sweep"):
                ct.download(plan); ct.arm_all(); ct.simulate_sync(count=N)

        WHAT IT IS FOR. Several programs share the bench through one backend --
        the GUI, this script, somebody else's script. Without a lease their
        WRITES interleave command by command: nothing stops the GUI from
        changing a filament's state, re-downloading a table or disarming in
        the middle of your sequence. While you hold the lease, every other
        client's write is refused (it gets "another client holds the write
        lease: <you>"); reads are never refused, so the GUI keeps monitoring.

        It is cooperative and gates only OTHERS. It does not make anything
        safer by itself, stop anything on exit, or slow your own calls --
        energised()/session() are what de-energise.

        Raises CTLeaseError on enter if another client holds it. Always
        releases on exit, including on error.

        RENEWED IN THE BACKGROUND for as long as the block runs, every ttl/3.
        `ttl` is therefore how long the lease outlives THIS PROCESS if it dies
        without releasing -- not how long the block may take. It used to be the
        latter: a 15-minute run under `lease(ttl=120)` lost its lease after two
        minutes and ran the rest unprotected without a word. If a renewal finds
        the lease taken (another client used steal), that is printed to stderr
        and `self.lease_lost` is set; the block is not interrupted.
        """
        self.acquire_lease(ttl=ttl, note=note)
        self.lease_lost = None
        stop = threading.Event()
        period = max(1.0, float(ttl) / 3.0)

        def _renew():
            while not stop.wait(period):
                r = self._post("/api/lock", {"action": "acquire", "ttl": ttl, "note": note})
                if r.get("ok") or r.get("connection_error"):
                    # A backend that cannot be reached cannot hand the lease
                    # to anyone else either; the next renewal will tell.
                    continue
                snap = r.get("lock") or {}
                self.lease_lost = Result({"at": time.strftime("%H:%M:%S"),
                                          "holder": snap.get("owner"),
                                          "error": r.get("error")})
                print(f"ct.lease: LOST the write lease to '{snap.get('owner')}' "
                      f"— from now on other clients can write to the bench "
                      f"while this block runs", file=sys.stderr, flush=True)
                return

        t = threading.Thread(target=_renew, name="ct_lease_renew", daemon=True)
        t.start()
        try:
            yield self
        finally:
            stop.set()
            t.join(timeout=5.0)
            self.release_lease()

    # ── session — guaranteed safe teardown ────────────────────────────────────

    def _report_teardown(self, who: str, failed: dict, steps, body_exc) -> None:
        """Record a teardown's outcome, and make a failure impossible to miss.

        Used by session() and energised(). A context manager cannot return
        anything and this client does not raise, so a failure goes three ways:
        stderr (always visible in a script's output), `self.last_teardown` (for
        code that checks), and -- when the with-block is already raising -- a
        note on THAT exception rather than a replacement for it, so the
        original error is still the one seen first.
        """
        self.last_teardown = Result({"ok": not failed, "by": who,
                                     "failed": failed, "steps": list(steps)})
        if not failed:
            return
        msg = (f"ct.{who} teardown FAILED — hardware may still be energised:\n"
               + "\n".join(f"  {n}: {e}" for n, e in failed.items()))
        print(msg, file=sys.stderr, flush=True)
        if body_exc is not None and hasattr(body_exc, "add_note"):
            body_exc.add_note(msg)

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
        # A context manager cannot return anything, and this client does not
        # raise (see ERROR HANDLING), so a failed teardown step used to vanish
        # completely: `try: fn() except Exception: pass`, where the except was
        # dead code (these calls return ok:False, they do not raise) and the
        # return value -- the only place the failure shows -- was discarded. An
        # HV rail that would not turn off looked exactly like one that did.
        #
        # Now every step's result is checked, and a failure is reported three
        # ways, because each one alone can be missed: printed to stderr (always
        # visible in a script's output), kept on `self.last_teardown` (for code
        # that wants to check), and -- if the with-block is already raising --
        # attached to THAT exception as a note rather than replacing it, so the
        # original error is still the one you see first.
        body_exc = None
        try:
            yield self
        except BaseException as exc:
            body_exc = exc
            raise
        finally:
            if cleanup:
                steps = (("emission HV off", lambda: self.enable_emission(False)),
                         ("focus HV off",    lambda: self.enable_focus(False)),
                         ("HV grid clear",   lambda: self.hv_grid_clear_all()),
                         ("STOP all",        lambda: self.stop_all()))
                failed = {}
                for name, fn in steps:
                    try:
                        r = fn()
                    except Exception as exc:     # defensive: should not raise
                        failed[name] = f"raised {type(exc).__name__}: {exc}"
                        continue
                    if isinstance(r, dict) and not r.get("ok"):
                        failed[name] = r.get("error") or "returned ok:False with no reason"
                self._report_teardown("session()", failed,
                                      [n for n, _ in steps], body_exc)

    # ── dead-man safety watchdog ─────────────────────────────────────────────
    # The backend walks an unattended ACTIVE filament back and drops the HV
    # rails on its own -- it outlives the client, which is the only place that
    # protection can live. See safety() for the rules; the one that shapes
    # everything else is that COMMANDS renew the timer and READS do not, so the
    # GUI polling in another window cannot hold a filament at firing current.

    #: How often _start_keepalive() renews. A third of the tightest default
    #: (HV, 10 s) so two renewals can be lost to a slow link and the timer
    #: still does not expire under a caller that is very much alive.
    _KEEPALIVE_PERIOD_S = 3.0

    def safety(self) -> dict:
        """Read the backend's dead-man watchdog.

            {"ok", "enabled", "active_timeout_s", "active_fallback",
             "hv_timeout_s",
             "active_filaments": {fid: {"idle_for_s", "never_commanded"}},
             "grid_closed": [fid, ...],   # grid MOSFETs possibly closed
             "hv_idle_for_s": float | None, # since the last HV grid command
             "events": [ ... what it has actually had to do ... ]}

        `never_commanded: True` means this backend has seen no command for a
        filament it believes is ACTIVE -- normally because it restarted while
        the filament was already hot. That is counted as expired, not as fresh:
        it is exactly the case the watchdog exists for.

        `events` empty is the healthy answer. A non-empty one is a record of
        the hardware being walked back without anyone asking, which is worth
        reading after a run that ended badly.
        """
        return self._get("/api/safety", timeout=5.0)

    def safety_config(self, enabled: bool | None = None,
                      active_timeout_s: float | None = None,
                      active_fallback=None,          # PowerState, name or number
                      hv_timeout_s: float | None = None) -> dict:
        """Change the watchdog's rules. Returns the same shape as safety(),
        plus "changed".

        Defaults: ACTIVE falls back to SLEEP after 30 s without a command; a
        closed HV grid MOSFET with no HV command for 10 s gets every MOSFET
        opened (clear-all). The emission/focus rails are never touched.

        `active_fallback` takes a PowerState, its name, or its number -- all
        three of these are the same call, and the first is the one to write:

            ct.safety_config(active_fallback=STOP)
            ct.safety_config(active_fallback="stop")
            ct.safety_config(active_fallback=1)

        It must be a DE-ENERGISING state (STOP or SLEEP) and the backend
        refuses anything else -- a watchdog that fired from one energised state
        into another would be firing into a second hazard. The result carries
        `active_fallback_name` beside the number so a caller never has to keep
        its own copy of the ladder.

        A zero or negative timeout is refused too: switching the watchdog off
        goes through `enabled=False`, so that turning off the thing that
        protects an unattended filament is a visible decision in the log rather
        than a number someone set to 0.
        """
        body: dict = {}
        if enabled is not None:
            body["enabled"] = bool(enabled)
        if active_timeout_s is not None:
            body["active_timeout_s"] = float(active_timeout_s)
        if active_fallback is not None:
            # A PowerState, its name, or its number -- passed through as given
            # so the backend does the parsing and owns the error message, which
            # keeps one definition of what a legal fallback is.
            body["active_fallback"] = (active_fallback.name
                                       if isinstance(active_fallback, PowerState)
                                       else active_fallback)
        if hv_timeout_s is not None:
            body["hv_timeout_s"] = float(hv_timeout_s)
        return self._post("/api/safety", body, timeout=5.0)

    def keepalive(self, filaments=None) -> dict:
        """Renew the watchdog without commanding anything.

        For code that is legitimately holding ACTIVE or HV grid MOSFETs while
        doing its own work -- a settle, a long fit, a measurement between
        shots. `filaments` limits it to those (USER_INDEX); None renews every
        filament the backend is tracking, and the HV grid timer either way.

        energised() runs one of these in the background for you, which is the
        right place for it. Call this directly only when holding a state
        outside that block.
        """
        body: dict = {"keepalive": True}
        if filaments is not None:
            body["filaments"] = [int(self._fid_of(f)) for f in filaments]
        return self._post("/api/safety", body, timeout=5.0)

    def _start_keepalive(self, filaments=None):
        """Renew in the background until the returned callable is invoked.

        Never raises and never blocks the caller: a keepalive that could take
        the run down with it would be worse than the timeout it prevents. A
        failed renewal is simply not a renewal -- if the backend really is
        unreachable, the watchdog firing is the correct outcome.
        """
        stop = threading.Event()
        fids = None
        if filaments:
            try:
                fids = [int(self._fid_of(f)) for f in filaments]
            except Exception:
                fids = None

        def _run():
            while not stop.wait(self._KEEPALIVE_PERIOD_S):
                body: dict = {"keepalive": True}
                if fids:
                    body["filaments"] = fids
                try:
                    self._post("/api/safety", body, timeout=3.0)
                except Exception:
                    pass
        t = threading.Thread(target=_run, name="ct_keepalive", daemon=True)
        t.start()

        def _stop():
            stop.set()
            t.join(timeout=1.0)
        return _stop

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
    # -- How far the PRE-pulse background may sit from the POST-pulse one, in
    #    units of the background's own sigma, before it is called suspect. 3 is
    #    the usual "outside the noise" line; the two windows measure the same
    #    physical baseline, so on a clean pulse they agree to within noise.
    _BG_DELTA_SIGMA = 3.0

    # -- Background measurement, SYMMETRIC around the envelope. ONE pair, in
    #    MICROSECONDS; at 1 MSPS one microsecond is one sample.
    #
    #      <- gap 200 -><- win 50 ->| envelope |<- win 50 -><- gap 200 ->
    #            (pre: SUBTRACTED)                 (post: reference)
    #
    #    Pre and post deliberately share the numbers: same front end, same
    #    disturbance next to each edge, so no reason to differ -- and one pair
    #    is half the wire and half the ways to get it wrong.
    _BG_GAP_US = 200.0
    _BG_WINDOW_US = 50.0
    #    Quiet time every pulse needs around it for BOTH backgrounds to exist:
    #    gap + window on each side. Fire tighter than this and one pulse's
    #    background is measured over its neighbour's tail -- which does not
    #    error, it just biases the charge.
    MIN_INTER_PULSE_US = 2 * (200 + 50)

    _READY_TTL_FLOOR_MS = 60000     # never below the firmware's own default
    _READY_TTL_GAP_FACTOR = 4       # x inter_pulse_ms; room for a late pulse
                                     # without waiting a whole extra cycle to
                                     # reclaim a genuinely dead arm

    @classmethod
    def identity_order(cls) -> list[int]:
        """The no-swap order: [0, 1, 2, ..., 95]. Start from this, change the
        entries you need, and pass the whole list to set_filament_order()."""
        return list(range(cls.FILAMENT_COUNT))

    @property
    def filament_order(self) -> dict[int, int]:
        """This client's USER_INDEX -> FID table (sparse: only entries that
        differ from identity). Read-only -- assign via set_filament_order().

        SNAPSHOT, taken once, deliberately NOT re-polled. Every index this
        client sends or receives is crossed through it, so a table that changed
        halfway through a loop would send some commands under the old lens and
        some under the new, with nothing in any result saying which. A script's
        numbering has to hold still for the length of the script.

        So: a new CTClient inherits whatever order the backend currently holds;
        an already-running one keeps the order it started with. Call
        reload_filament_order() to deliberately re-sync, and
        filament_order_status() to see whether you have drifted from the
        backend.
        """
        if self._order is None:
            self._adopt_order(self._get("/api/filament-order", timeout=10.0))
        return self._order

    def _adopt_order(self, snap: dict) -> None:
        """Install a backend snapshot as this client's order."""
        if not snap.get("ok"):
            # Identity is the only safe fallback: it is the one mapping that
            # cannot send a command to a filament other than the one named. An
            # order we failed to read must not be GUESSED -- but record that we
            # never got one, so filament_order_status() can say so rather than
            # reporting a confident identity.
            self._order, self._order_rev, self._order_epoch = {}, {}, None
            return
        seq = list(snap.get("order") or range(self.FILAMENT_COUNT))
        self._order = {i: int(v) for i, v in enumerate(seq) if int(v) != i}
        self._order_rev = {v: k for k, v in self._order.items()}
        self._order_epoch = snap.get("epoch")
        # The dead cache holds USER_INDEX values translated under the PREVIOUS
        # order, so it now names the wrong filaments. Drop it rather than
        # translate.
        self._dead_fetched_at = 0.0

    def reload_filament_order(self) -> list[int]:
        """Re-read the order from the backend and adopt it, discarding this
        client's snapshot. Returns the new 96-entry list.

        The deliberate version of what the property will not do on its own --
        call it at a point where you know no partially-issued operation is in
        flight, not inside a loop.
        """
        self._adopt_order(self._get("/api/filament-order", timeout=10.0))
        return self.get_filament_order()

    def filament_order_status(self) -> dict:
        """Compare this client's snapshot against the backend's live order.

        Returns {"ok", "in_sync", "client": [96], "backend": [96],
        "epoch_client", "epoch_backend", "backend_restarted", "set_by",
        "note"}. Never raises.

        `backend_restarted` True means the backend has been restarted since
        this client took its snapshot: the order is held in memory only and a
        restart forgets it, so the backend is back at identity while this
        client is still crossing indices through the old table. Nothing will
        error -- this client stays self-consistent -- but the NEXT script to
        start will get identity, so re-apply the order if it still reflects the
        hardware.
        """
        live = self._get("/api/filament-order", timeout=10.0)
        mine = self.get_filament_order()
        if not live.get("ok"):
            return {"ok": False, "error": live.get("error"), "in_sync": None,
                    "client": mine, "backend": None,
                    "epoch_client": self._order_epoch, "epoch_backend": None,
                    "backend_restarted": None,
                    "note": "could not read the backend's order"}
        theirs = list(live.get("order") or [])
        restarted = (self._order_epoch is not None
                     and live.get("epoch") != self._order_epoch)
        in_sync = theirs == mine
        if in_sync:
            note = "client and backend agree"
        elif restarted:
            note = ("the backend restarted and forgot the order (it is held in "
                    "memory only, by design). This client still uses its own "
                    "snapshot and stays consistent, but the next script to "
                    "start will get identity — re-apply with "
                    "set_filament_order() if it still matches the hardware.")
        else:
            note = ("another client changed the order after this one took its "
                    "snapshot. This client keeps its own until "
                    "reload_filament_order().")
        return {"ok": True, "in_sync": in_sync, "client": mine, "backend": theirs,
                "epoch_client": self._order_epoch, "epoch_backend": live.get("epoch"),
                "backend_restarted": restarted, "set_by": live.get("set_by"),
                "note": note}

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
            self._push_order(None)
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
        self._push_order(seq)

    def _push_order(self, seq) -> None:
        """Send the order to the backend and adopt what it echoes back.

        Adopting the ECHO rather than the local copy is the point: the backend
        is the authority, so this client ends up using exactly what the next
        script will read, epoch included. If the write fails the local table is
        left ALONE -- silently continuing under a mapping the backend does not
        have is how two scripts end up disagreeing about which filament is which.
        """
        r = self._post("/api/filament-order",
                       {"order": list(seq) if seq is not None else None},
                       timeout=10.0)
        if not r.get("ok"):
            raise CTError(f"set_filament_order: the backend rejected it: "
                          f"{r.get('error')}. The local order is unchanged.")
        self._adopt_order(r)

    def get_filament_order(self) -> list[int]:
        """The current mapping as an explicit 96-entry list, order[i] = the
        FID that USER_INDEX i refers to. Identity when no remapping is
        set, so this always round-trips through set_filament_order()."""
        order = self.filament_order     # bound once: property, may fetch
        return [order.get(i, i) for i in range(self.FILAMENT_COUNT)]

    @property
    def _swap_active(self) -> bool:
        """Whether any USER_INDEX differs from its FID. Exists so that the only
        places `filament_order` itself is read are the two crossing functions
        and its own setter/getter -- which makes the boundary rule something a
        grep can check, not just a convention."""
        return bool(self.filament_order)   # property; cached after first use

    def _fid_of(self, filament: int) -> Fid:
        """USER_INDEX -> FID. The only outbound crossing (identity with no swap)."""
        f = int(filament)
        order = self.filament_order      # bound once, not read twice
        return Fid(order.get(f, f) if order else f)

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



# ── every public method returns a Result ────────────────────────────────────
# The syntactic sugar, applied once here rather than as a decorator repeated on
# 130 methods. Only the RETURN VALUE is touched, and only when it is a plain
# dict: context managers (energised/lease/session), floats, lists and strings
# pass through untouched, so nothing about how the client is used changes.
#
# Done at class level because results are built in dozens of places -- every
# local refusal, every early return, every parsed reply. Wrapping only the HTTP
# funnels left exactly the wrong half readable: the refusals a caller is most
# likely to be squinting at never go near them.
#: Call depth per thread, so only the call the SCRIPT made is recorded, not
#: the dozens it makes internally (one fire_single_pulse polls shv_status
#: throughout the shot).
_CALL_DEPTH = threading.local()


def _as_result(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        depth = getattr(_CALL_DEPTH, "n", 0)
        _CALL_DEPTH.n = depth + 1
        t0 = time.time()
        try:
            out = fn(*args, **kwargs)
        except BaseException as exc:
            if depth == 0 and args and isinstance(args[0], CTClient):
                args[0]._record_call(fn.__name__, args[1:], kwargs, t0, exc=exc)
            raise
        finally:
            _CALL_DEPTH.n = depth
        if isinstance(out, dict) and not isinstance(out, Result):
            out = Result(out)
        if depth == 0 and args and isinstance(args[0], CTClient):
            args[0]._record_call(fn.__name__, args[1:], kwargs, t0, out=out)
        return out
    return wrapper


# Over the whole MRO, not vars(CTClient): most methods live in the mixins now,
# and vars() of the class alone would skip them -- silently, no Result and no
# call record. The first definition along the MRO wins, as attribute lookup
# does; the wrapped copy is set on CTClient, the mixins stay undecorated.
_seen: set = set()
for _cls in CTClient.__mro__:
    if _cls is object:
        continue
    for _name, _fn in list(vars(_cls).items()):
        if _name in _seen:
            continue
        _seen.add(_name)
        # Public, plain functions only. staticmethod/classmethod/property objects
        # are not functions here, so they are skipped -- which is what we want:
        # they return numbers, and `filament_order` is a property whose access
        # must not be routed through a wrapper.
        if _name.startswith("_") or not inspect.isfunction(_fn):
            continue
        setattr(CTClient, _name, _as_result(_fn))
del _seen, _cls
del _name, _fn


# A few methods in the mixins name CTClient itself (CTClient.identity_order(),
# CTClient.heating_windows(), the fit constants). It is defined only here,
# after them, so bind it into their modules; they look it up at call time.
from . import _power, _hv, _schedule, _measure, _emission, _diagnostics, _decode  # noqa: E402
for _m in (_power, _hv, _schedule, _measure, _emission, _diagnostics, _decode):
    _m.CTClient = CTClient
del _m
