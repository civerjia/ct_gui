# Firing filaments: scan_view and fire_single_pulse — design

**Status: proposal, nothing built.** The last section lists what must be
decided before code changes.

## 1. Goal

- **Run `scan_view` at scale.** Fire a list of filaments in order, the next
  ones already heating while one fires, every filament measured, every
  filament back at IDLE. `fire_single_pulse` is the same thing for one
  filament.
- **VOLTAGE mode is the main heating mode, for speed.** A voltage step
  arrives faster than the current loop converges.
- **Avoid every pitfall already hit on this rig.** Section 4 lists each one
  and what in this design prevents it. That section is the point of this
  document; the rest follows from it.

## 2. Design rules

1. **Every argument is named.** No positional arguments.
2. **One choice = one argument whose value says which.** For example,
   `heating=ct.Voltage(...)` or `ct.Current(...)`, and `trigger=ct.Sim(...)`
   or `ct.Ext(...)`. "Both" cannot be written.
3. **What only one choice uses lives inside it.** For example,
   `max_voltage_drop_mv` lives in `ct.Voltage`, the sim spacing in `ct.Sim`,
   and the sampling in `ct.Measure`.
4. **One name, one meaning**, in both functions.
5. **Nothing hidden.** Everything that decides how a shot fires is an argument
   of the shot, written and read back by the shot. Board state set earlier by
   another call does not count.
6. **Every shot is measured.**
7. **Physical things are named physically.** Safety state (dead filaments,
   stuck switches) is kept in FIDs. The user numbering is translated ONCE,
   at the start of the call, with one mapping snapshot used for everything
   in that call.

`ct.Sim`, `ct.Ext`, `ct.Voltage`, `ct.Current` and `ct.Measure` take named
arguments only and return a plain dict that carries its kind. A misspelt name
fails at once, and the dict passes the remote proxy (`on_armed` inside it is
still called back).

## 2a. Words (agreed 2026-10-27)

| word | means |
|---|---|
| **arrived** | VOLTAGE: the LOAD voltage is within ±150 mV of `active_mv`. Current: the current loop has converged at `active_ma` (not capped). |
| **`settle_ms`** | a wait counted from the FIRST arrival. It is never restarted while the setpoint is unchanged. |
| **HOT** | can emit: `settle_ms` has passed since the first arrival, and the heating has not been interrupted since. Only a HOT filament fires. |
| **interrupted** | an output fault (OCP = short, open), the board lost, the filament left its heating state, or (VOLTAGE) more than `max_voltage_drop_mv` below target on 3 reads in a row. Wobble is not an interruption. |
| **settled** | stable. Feedback only, never a gate. |

What is done TODAY differs in one place:

| | today | this design |
|---|---|---|
| fire_single_pulse | waits for "settled": the heating must hold arrival for `settle_ms` CONTINUOUSLY, and every exit restarts the clock (VOLTAGE and current alike) | HOT, as above |
| scan_view | HOT, as above (VOLTAGE) | HOT, as above |
| current heating | restarts on every exit (fire_single_pulse) | HOT, as above: from the first arrival, like VOLTAGE (decided, Q11) |

## 3. The calls

```python
# scan_view, VOLTAGE heating, sim trigger
r = ct.scan_view(
    filaments=[0, 1, 2],
    heating=ct.Voltage(
        active_mv={0: 7688, 1: 7698, 2: 8228},  # each filament's calibrated voltage
        idle_ma=1200,                           # back to this when it is done
        settle_ms=1000,                         # HOT = this long after the FIRST arrival
        heat_timeout_s=10,                      # longest it may stay heated, raise -> IDLE
        max_voltage_drop_mv=500,                # a drop this big (3 reads) = interrupted
    ),
    num_pulses=10,
    width_us=7000,
    trigger_delay_us=3000,                      # each pulse fires this long after its edge
    trigger=ct.Sim(
        pulse_interval_ms=100,                  # the train's edge spacing
        trigger_timeout_ms=None,                # None = 2 x 100 + 100 = 300
        on_armed=None,
    ),
    measure=ct.Measure(rate_hz=1_000_000, bg_gap_us=100.0, bg_window_us=50.0),
    preheat_ahead="auto",
    preheat_margin_s=0.3,
    max_hot=6,
    link_lost_timeout_s=5.0,
)

# scan_view, VOLTAGE heating, ext trigger (the detector sends one edge per filament)
r = ct.scan_view(
    filaments=[0, 1, 2],
    heating=ct.Voltage(active_mv={0: 7688, 1: 7698, 2: 8228}, idle_ma=1200,
                       settle_ms=1000, heat_timeout_s=10, max_voltage_drop_mv=500),
    num_pulses=1,
    width_us=30000,
    trigger_delay_us=3000,
    trigger=ct.Ext(
        on_armed=on_armed,                      # on_armed(f): start the detector
        trigger_timeout_ms=None,                # gap between edges: none with 1 pulse
    ),
    measure=ct.Measure(rate_hz=1_000_000, bg_gap_us=100.0, bg_window_us=50.0),
    preheat_ahead="auto",
    preheat_margin_s=0.3,
    max_hot=6,
    link_lost_timeout_s=5.0,
)

# fire_single_pulse: the same arguments for one filament
r = ct.fire_single_pulse(
    filament=16,
    heating=ct.Voltage(active_mv=7688, idle_ma=1200, settle_ms=1000, heat_timeout_s=10,
                       max_voltage_drop_mv=500),
    num_pulses=1,
    width_us=30000,
    trigger_delay_us=3000,
    trigger=ct.Ext(on_armed=on_armed, trigger_timeout_ms=None),
    measure=ct.Measure(rate_hz=1_000_000, bg_gap_us=100.0, bg_window_us=50.0),
    controller=None,
    link_lost_timeout_s=5.0,
)
```

`fire_single_pulse` with a heating is `scan_view` with one filament: the same
steps, gating, window and failures. It also takes `ct.Current(active_ma=...)`
(ACTIVE through the current loop), and `heating=None` for tests that set the
filament's state themselves: then the schedule is not gated and there is no
window.

## 4. Pitfalls already hit, and what prevents each

Each row is something that went wrong on this rig. "Status" says whether it
is fixed today; "this design" says what keeps it from coming back, or from
happening at scan scale.

### 4.1 Heating in VOLTAGE mode

| pitfall | cause | status | this design |
|---|---|---|---|
| Re-setting a voltage dropped the output to 0.8 V, then ramped back up (2026-07) | the OFF→ON test did not count VOLTAGE as on, so it re-seeded | fixed (firmware) | a filament is raised ONCE per turn; nothing re-sets its voltage mid-turn |
| Load voltage below target by the cable drop | VOLTAGE was open loop on the converter | fixed: closed loop on the measured load voltage, c9bb6df | "arrived" = load voltage within ±150 mV |
| One wild INA reading moved the voltage loop (a 500 mV jump; a voltage change whose current did not follow) | corrupted reads; P2 CH1/CH2 have 10–25× the other channels' bad reads | fixed: held until the next read agrees, 3386f0f / 5553c9a | HOT is interrupted only by a drop on **3** reads; one bad read never interrupts, and never resets `settle_ms` |
| VOLTAGE → IDLE overshot to ~0.45 A, then climbed back slowly; the filament cooled under IDLE | the current loop restarted unseeded and cold | fixed: seeded from the measured filament, warm restart, cc9e776 | — |
| …but on P2 CH1/CH2 a filament was still at 424–883 mA after 37–40 s at IDLE, so the next view refused it "not warm yet" | bad INA reads on those channels (suspected) | open (hardware) | a "not warm yet" refusal skips that filament for THIS view only; the next view tries it again (done, 4b0c0d9) |
| A voltage above the cap is never reached | the rig's cap is **9.5 V** (the user's setting, 2026-10-27), kept by the backend | by design | the cap is READ from every controller at set-up; `active_mv` above it is refused before anything is heated |
| After an RP2350 restart its cap is the firmware default, 11 V, until the backend's keeper writes 9.5 V back — and the keeper skips controllers that are armed or running | the cap is board state; the keeper waits for the end of a run | **open** | the run compares each controller's cap (with its heartbeat) during the run; a cap different from the one read at set-up → STOP |
| Over-current cuts the converter OFF (IOUT_LIMIT is a cut-out, not a limiter) | with the cap at 9.5 V a normal filament cannot draw that much: an OCP trip means a short or a fault | by design of the part | an OCP trip = "interrupted" (output fault): that filament → SLEEP, skipped, the run goes on |
| A VOLTAGE row in the firmware heating table spun forever | the firmware table cannot run VOLTAGE | fixed: skipped, 3cbf555 | all heating is commanded by the host (backend), never by the firmware table |

### 4.2 Holding the heat: the backend's own timers

| pitfall | cause | status | this design |
|---|---|---|---|
| **During an armed or running schedule, the backend renews the dead-man for every filament and DEFERS every timed-heating deadline** ("held_for_run", "timed_active_deferred") | written so a run is never interrupted by a stale timer | **open — found 2026-10-28** | the heating window is NOT deferred for the run's own filaments: when `heat_timeout_s` is up, the backend drops that filament to IDLE even while the schedule is armed (Q1). Without this, a pre-heated filament can stay at VOLTAGE until `total_ms`, i.e. minutes |
| The timed VOLTAGE of a waiting filament was renewed as its wait grew, with no upper limit | "predicted wait + margin, renewed" | today | one hard window per raise, never renewed (§6) |
| combined_backend sets `active_timeout_s=4.0` (the auto test 6): a heated filament nobody commands for 4 s falls back | the dead-man | today | inside a run the window is the limit; outside a run the dead-man still applies |

### 4.3 Firing: edges, delay, timing

| pitfall | cause | status | this design |
|---|---|---|---|
| The trigger delay is board state set by a separate call; an RP2350 restart zeroes it, and the shot fires 3 ms early with nobody told | `set_trigger_delay()` is not part of the shot | today | `trigger_delay_us` is a **required argument**, written to every controller at set-up and read back; the ARM is refused while they differ |
| An edge during the delay of the previous one loses that pulse while both are counted | one hardware alarm serves every edge; a new edge re-aims it | firmware, unchecked | spacing must be > `trigger_delay_us` + `width_us`: sim refused before starting; ext Q4 |
| `trigger_timeout_ms` counted the host's own work between release and the first edge; 60 ms stopped the run at the second filament | the firmware timed it from the release | fixed: from the first edge, 766874d | `trigger_timeout_ms` = the gap between edges only |
| A gap limit equal to the spacing faulted after one pulse | every edge raced its own deadline | refused today | `trigger_timeout_ms` > spacing, refused otherwise |
| sim: "already running" right after a filament; the next one was skipped | the backend sees a train's end only on its 300 ms poll | host retries 2 s, cb70fa6 | the backend asks the ESP32 directly (Q6) |
| sim: a train runs on its own; if a filament ends early, the rest of its train fires the NEXT filament | nothing stops the train | **open** | a filament that ends before its train: stop the train and confirm it stopped before the next row is released; every STOP stops it too |
| ext: an edge before the filament is hot fired a cold filament | fire_single_pulse's schedule is not gated | today | gated schedule in both functions: a pulse fires only on a released row whose filament is HOT |
| First pulse after ARM latched 0 (2.3% of pulse 1) | unknown; the byte waits in the 595 for the whole ARM→trigger wait (~3 s) | open | the gated run stages a filament's first pulse at its RELEASE, after HOT, so the wait shrinks to `on_armed` + the first edge |

### 4.4 Two controllers

| pitfall | cause | status | this design |
|---|---|---|---|
| sim edges skipped the trigger delay on C1 while C2 applied it; C2's pulses fell outside the window | the sim path bypassed the delay | fixed (G6) | the delay is applied by each board on both paths |
| C2 missed sim edges | C1 forwarded the live level of a 5 µs pulse that was already over | fixed (G7): forwards latched edges | — |
| An edge between the two ARMs counted on one board only: the boards an entry apart for the whole run | arm order | fixed: C2 armed first, C1 (forwards) last | kept |
| The two boards' trigger delays differ after one restarts | board state | ARM refused on mismatch | the shot writes the delay to both (§4.3) |

### 4.5 Measurement

| pitfall | cause | status | this design |
|---|---|---|---|
| A 660-pulse view got the LAST 128 events and gave them to the FIRST filaments | the ESP32 keeps 128 events; read once at the end | fixed: read during the run, 0768ab2 | a missing event is "missing", never another pulse's |
| "Measured 0 of 1" | the STM32 sent no event | open | reported per pulse; the run goes on |
| A shot with measurement off is useless | — | — | `measure=` is required; a measurement that cannot be armed refuses the shot before anything is heated |
| Absolute mA is suspect on the current board (VREF+ floating, front end saturating ~842 counts) | hardware | new board coming | the per-pulse event is kept raw too |

### 4.6 HV switch read-back (165) and switches

The 165 read-back is ALWAYS taken (PIO firing): every pulse reads the switch
byte once while ON and once after OFF, into that pulse's record.

| pitfall | cause | status | this design |
|---|---|---|---|
| "HV did not turn on": the ON read-back ≠ commanded; it often reads the neighbour bit | the 165 is flaky and reads upstream of the gate resistor | hardware | ON mismatch is recorded on the pulse (`on_mismatch`), the run goes on; whether it fired is judged by the measured current (Q8) |
| A switch did not open after a pulse | MOS stuck, or the OFF frame lost | the OFF read-back ≠ 0 is counted per pulse | STOP at once |
| A pulse whose read-back sample was dropped | the read-back FIFO | flagged "unverified" | recorded; the run goes on |
| Read-back OK but no current | cold-soldered 33 Ω gate resistors | hardware | a pulse with no current is flagged on that filament |
| OFF frames lost on a slow link left 4 switches closed for 3 h | an unconfirmed OFF was ignored | fixed: `force_grid_off` | every STOP and every end of run: disarm, then all grids forced open and confirmed |
| The firmware `fault_policy` (on a board fault: stop the run / continue; on a read-back mismatch: stop / continue) is board state, set by a separate call and kept until changed | hidden state, like the trigger delay. Its mismatch switch only acts on the old bit-bang path, not PIO | today | the run writes the policy it needs at set-up and reads it back (rule 5): a board fault = that filament interrupted and skipped, never the whole run; a read-back mismatch = recorded |

### 4.7 Numbering

| pitfall | cause | status | this design |
|---|---|---|---|
| One filament heated, the next fired (27 shots, 10-08) | the call that heated and the call that fired used two different filament orders | fixed: one mapping, `X-CT-Order` | heat and fire happen in ONE call that takes ONE mapping snapshot |
| `set_dead` after `set_filament_order` freed broken boards | the dead mask was translated through the order | fixed: dead mask is physical | rule 7 |
| The GUI sent ring positions as FIDs and addressed the wrong board | two numberings | fixed (GUI) | the call takes the user's numbering only; the result reports user number, FID and board for every row |
| An extra key in `active_mv` was silently ignored (a typo like 95 for 59) | scan_views passes one dict for every view | today | refused; scan_views gives each view only its own (Q5) |

### 4.8 Links, backend, other programs

| pitfall | cause | status | this design |
|---|---|---|---|
| WiFi TCP stalls of 1–2.5 s on both ESP32s | the link | 64 KB ring on P1 | `link_lost_timeout_s` default 5 s rides them out |
| The ESP32's web server is single-threaded; 200–300 ms a request under load | its design | — | during a run: one sim request per filament, no other ESP32 web traffic from the run |
| Looping single-board reads starved the current loop and gave false "stuck" results | one shared link | rule | during a run: bulk / cached reads only (CH_GET_HOT, cached currents, pushed telemetry) |
| Only the backend→ESP32 socket is watched; an RP2350 behind it can be silent while it looks connected | `connected` = socket open | **open** | watch the RP2350 heartbeat too (L3) |
| With the link down, nothing drops a heated filament: the RP2350 has no fallback | firmware | **open** | firmware: a timed VOLTAGE of its own (Q2) |
| A second open copy of the combined page sent `idle_one` and cancelled the first page's next fire | broadcast results acted on by every page | fixed in the page (ownership flags) | the run lives in the backend under one lease; nothing outside it may change its filaments (only de-energising requests pass the lease) |
| A client NEWER than the backend was told to restart itself | the version check compared only "different" | fixed, 61672eb | — |
| The backend updated itself mid-session | auto-update when quiet | waits for no lease, no run | the run holds the lease for its whole length |

## 5. Every parameter

### 5.1 Top level

| parameter | function | meaning | default / range |
|---|---|---|---|
| `filaments` / `filament` | scan_view / fire_single_pulse | fired strictly in this order / the one filament | required; no repeats, none dead |
| `heating` | both | §5.2 | required |
| `num_pulses` | both | pulses per filament = edges per filament | 1..255 |
| `width_us` | both | the HV pulse width (RP2350 PIO) | 1..65 535 µs |
| `trigger_delay_us` | both | each pulse fires this long after its edge (both paths, both boards); written and read back by the shot | required, 0..65 535 µs |
| `trigger` | both | §5.3 | required |
| `measure` | both | `ct.Measure(rate_hz, bg_gap_us, bg_window_us)`: background averaged over `bg_window_us`, taken `bg_gap_us` before and after each pulse | required |
| `preheat_ahead`, `preheat_margin_s`, `max_hot` | scan_view | when to raise each filament so it is HOT about when its turn comes; at most `max_hot` heating at once | `"auto"`, 0.3 s, 6 |
| `controller` | fire_single_pulse | the firing controller (`None` = the filament's own board) | `None` |
| `link_lost_timeout_s` | both | a command link (L2 or L3) down this long → STOP | 5.0 |

### 5.2 `heating=`

| | `ct.Voltage(...)` — main | `ct.Current(...)` | `None` |
|---|---|---|---|
| for | both | fire_single_pulse | fire_single_pulse |
| heats with | VOLTAGE, closed loop on the load voltage | ACTIVE, current loop | the state as is: whatever the caller set before the call |
| value | `active_mv`: scan_view `{filament: mV}`, exactly the listed filaments; fire_single_pulse one number; ≤ the voltage cap read from the controllers (9.5 V on the rig) | `active_ma` | — |
| "arrived" | §2a | §2a | — |
| `idle_ma` | back to this when done | same | — |
| `settle_ms` | §2a: from the FIRST arrival | the same (Q11) | — |
| `max_voltage_drop_mv` | a drop this far below `active_mv` on 3 reads = interrupted. Default 500 | — | — |
| `heat_timeout_s` | the longest it may stay heated in one go; backend-timed, never renewed, **not deferred by the run**. Counted from the heating command (the raise) until it is back at IDLE: ALL heating, including a filament that never arrives (decided, Q10) | same | — |
| interrupted by | output fault (OCP, open, short), board lost, the drop above | output fault, board lost | — |

### 5.3 `trigger=`

| | `ct.Sim(...)` | `ct.Ext(...)` |
|---|---|---|
| edges from | the master ESP32's hardware timer, one train per filament | your hardware, into the master's SyncIn |
| `on_armed` | optional `on_armed(f)`; the train starts after it returns | required `on_armed(f)`: start your source and return |
| `pulse_interval_ms` | the train's spacing; > (`trigger_delay_us` + `width_us`)/1000, ≥ 0.5 ms; default 500 | — (Q4) |
| `trigger_timeout_ms` | the longest wait for the NEXT edge. The first one is counted from the moment the train has started (the ESP32 accepted it); every later one from the previous edge. Runs out → **STOP the run**. Default 2 × spacing + 100 | the same: the first edge counted from `on_armed` returning (your source started), the later ones from the previous edge. Runs out → **STOP the run**. Required (only you know how long your source takes) |

## 6. Timing

![sim timing: the parameters on the timeline, the request and poll delays, the leftover train](img/timing_sim.svg)

One pulse:

```
edge ──(trigger_delay_us)──> pulse ON ──(width_us)──> OFF ──> next edge, after (delay + width)
```

The workflow. Each step happens once, in order.

1. **Checks.** Nothing is touched; on failure the call is refused with a
   reason.
   - Arguments: every name, every kind, every key; the heating dict
     matches `filaments` exactly; none dead; `active_mv` ≤ the cap read from
     every controller (9.5 V on the rig); the spacing
     rules in §5.3.
   - HV (emission and focus) is on, the HV guard is not tripped, and the
     vacuum is within its limit.
   - The window holds one filament's work (below).
2. **Set-up.** All filaments are at IDLE.
   - One mapping snapshot for the whole call.
   - Write `trigger_delay_us` to every controller and read it back.
   - Download ONE gated schedule and read it back.
   - Write `settle_ms` and `max_voltage_drop_mv`.
   - Arm the STM32 measurement (or refuse), then ARM: C2 first, C1 last.
3. **Per filament f, in order:**
   1. **Raise** f to VOLTAGE `active_mv[f]`, early enough to be HOT by its
      turn. The window opens: the backend drops f to IDLE at
      `heat_timeout_s`, armed or not.
   2. **HOT:** first arrival + `settle_ms`, not interrupted.
   3. **Its turn:** release f's row on every controller.
   4. **`on_armed(f)`** is called.
   5. **Edges:** sim, one train request; ext, your source.
   6. **Pulses**, `trigger_delay_us` after each edge. The host reads the
      measurement events as they arrive.
   7. **Done:** all pulses counted. f goes to IDLE once the next filament is
      released.
   - **Ended early** (window, skip): stop the sim train and confirm it, then
     f goes to IDLE, then the next row is released.
4. **End**, whatever ended it.
   - Stop any sim train and disarm.
   - Force every grid open and confirm.
   - Every heated filament goes to IDLE (a faulted one to SLEEP).
   - Set `settle_ms` back to 0.
   - Read the pulse logs and match the measurements by event id.

![three filaments, normal run](img/timing_normal.svg)

What has to fit in the window, from the raise:

```
VOLTAGE heat-up to first arrival   from the filament's history (filament_perf)
+ settle_ms
+ wait for its turn                ~0 with preheat_ahead="auto"
+ on_armed                         ext: until the detector is ready
+ sim: train request               0.07-0.3 s
+ (num_pulses - 1) x spacing + trigger_delay_us + width_us
```

## 7. When something goes wrong

![what ends each failure](img/timing_failures.svg)

| failure | caught by | result |
|---|---|---|
| never gets HOT | the window | IDLE, skipped, the run goes on |
| interrupted (OCP, open, short, board lost, voltage drop) | the RP2350 HOT flags | a faulted output → SLEEP, otherwise IDLE; skipped; the run goes on |
| "not warm yet" when raised | the backend's ladder guard | skipped in this view only |
| HOT, its turn comes late | the window | IDLE, re-heated at its turn (Q3) |
| `on_armed` hangs | the window, with `on_armed` still running | STOP |
| no edge comes (ext source dead, sim train never started) | `trigger_timeout_ms` from when edges should start (host) | STOP — at the first filament, not after heating the rest |
| edges stop half way | `trigger_timeout_ms` between edges (firmware) | STOP |
| ON read-back mismatch, a pulse with no current | the pulse log, the measurement | recorded on that filament; the run goes on |
| measurement event missing | event ids | "missing" on that pulse |
| SHV fault, MOS stuck on, HV guard, vacuum, HV off | checked every 0.5 s | STOP |
| link L2 or L3 down > `link_lost_timeout_s` | the host | STOP |

STOP = stop the sim train, disarm, force the grids open, every heated filament
to IDLE (a faulted one to SLEEP), `stopped` says why.

## 8. Removed and renamed

| today | becomes |
|---|---|
| positional arguments | all named |
| `set_trigger_delay()` before the shot | `trigger_delay_us=` in the shot |
| `trigger="sim"/"ext"`, `on_armed`, `inter_pulse_ms` / `pulse_interval_ms`, `trigger_timeout_ms` | `trigger=ct.Sim(...)` / `ct.Ext(...)` |
| `active_mv` / `active_ma`, `idle_ma`, `settle_ms`, `heat_timeout_s`, `max_voltage_drop_mv` | `heating=ct.Voltage(...)` / `ct.Current(...)` / `None` |
| `measure=True`, `rate_hz`, `bg_gap_us`, `bg_window_us` | `measure=ct.Measure(...)`, required |
| `heat_timeout_s` = the wait for the first arrival | the longest heating, raise → IDLE |
| `settle_ms` "held continuously" (fire_single_pulse) | from the first arrival, in both |
| `on_armed()` (fire_single_pulse) | `on_armed(f)` in both |
| `shot_wait_timeout_s`, `timeout_s`, `on_armed_timeout_s`, `no_pulse_timeout_s`, `total_ms`, `max_on_ms` | removed: the window, or computed |
| `idle_one` around each shot | not needed: every filament starts and ends at IDLE |

Callers that change with it: the auto test program, combined_backend (its
`CT_PULSE_*` / `CT_SCAN_*` whitelists, its delay box), the emission tests and
`measure_pulse_current` (`heating=None`, `on_armed(f)`).

## 9. To decide

| | question | proposal |
|---|---|---|
| Q1 | the run's own filaments: their heating window ends while the schedule is armed. Today the backend defers it until the run ends | the window is never deferred for the run's own filaments |
| Q2 | RP2350 drops a heated filament by itself after `heat_timeout_s` if no IDLE comes (L2/L3 down) | yes; firmware change, test board first |
| Q3 | a filament HOT but still waiting when its window ends | back to IDLE, re-heated at its turn |
| Q4 | ext: spacing > delay + width and ≥ 0.5 ms need your spacing | `ct.Ext(expected_interval_ms=...)`, optional, used only for these checks |
| Q5 | extra keys in the heating dict | refused; scan_views passes each view only its own |
| Q6 | sim "already running" right after a filament | the backend asks the ESP32 directly |
| Q7 | `trigger_timeout_ms` runs out | decided: STOP the run. It is the safety backstop; a dead trigger must stop the run at the first filament |
| Q8 | ON read-back mismatch | record and go on; the measured current is the truth |
| Q9 | `heat_timeout_s` default | none: required |
| Q10 | `heat_timeout_s` counted from | decided: the raise → back at IDLE, all heating |
| Q11 | `settle_ms` with current heating | decided: from the first arrival, like VOLTAGE |
