# Tests, self-tests and calibration — tutorial

Every hardware check and calibration the controllers offer, from Python, with
a complete example for each, what a healthy result looks like, and how to read
a bad one. The GUI's **Calibration & Test** panel runs several of the same
tests; this is the scripted side.

Outputs marked **(real)** were captured on this bench; everything else shows
the shape of the result, never invented numbers.

- [0. Before you start](#0-before-you-start)
- [1. Which test answers which question](#1-which-test-answers-which-question)
- [2. Recommended order](#2-recommended-order)
- [3. Read-only checks (safe any time, even during a run)](#3-read-only-checks)
- [4. Checks that drive pins or switches (HV must be off)](#4-checks-that-drive-pins-or-switches)
- [5. The emission path: MOSFET R_eq sweep (HV on, filaments cold)](#5-the-emission-path-mosfet-r_eq-sweep)
- [6. Calibration](#6-calibration)
- [7. Schedule checks](#7-schedule-checks)
- [8. USB console (the RP2350's own commands)](#8-usb-console)
- [9. Symptom → test](#9-symptom--test)

---

## 0. Before you start

```python
from ct_simple_control import CTClient

ct = CTClient("192.168.8.218", client_id="bench-check")   # the BACKEND's IP (its banner)
print(ct.status())            # both controllers "connected": True?
```

Things that apply to every call below:

- **Results print short.** `print(r)` shows the answer; firmware-level detail
  is left out and the print says so. `print(r.full())` shows every field, and
  the call log (`logs/client/*.jsonl` on the machine running the script) keeps
  the whole result regardless. In code, `r["..."]` works on every field.
- **Failures are results, not exceptions.** `r["ok"]` is False and
  `r["error"]` / `r["problems"]` say why. A test that could not measure says
  "unmeasured" / "inconclusive" / `None` — never a plausible-looking 0.
- **Per controller.** The diagnostics cover every connected controller, or one
  if you pass `controller=1`/`2`. Results are under
  `r["controllers"]["1"]`, `["2"]`.
- **Anything that changes hardware needs the lease** — wrap it in
  `with ct.lease(note="..."):`. Reads never need it.
- **Channels 7 and 8 are not used**; the diagnostics look at CH1–CH6 by default.

---

## 1. Which test answers which question

| Question | Method | Touches hardware? | Time |
|---|---|---|---|
| Is a board lost / a channel dark right now? | `board_health()` | no (read-only) | ~1 s |
| Is an I²C bus wedged, or are the chips just silent? | `i2c_stats()` | no | ~1 s |
| Which chips answer on each board? | `chip_health()` | reads I²C | ~1 s |
| Does each chip answer *registers*, or only its address? | `diagnosis()` | reads I²C | ~0.3 s/controller |
| Are the RP2350's own GPIOs healthy? | `pin_report()` | no (samples 50 ms) | <1 s |
| Do the HV-chain output pins actually move? | `pin_probe()` | **drives 7 pins** | <1 s |
| Do the I/O expanders toggle? | `self_test()` | **toggles expander pins** | seconds |
| Does every HV grid switch close and open? | `hv_switch_test()` | **switches the grid** — HV at 0 | ~1 min |
| Is the 165 read-back chain intact on one channel? | `read_hv_diag165()` | writes a test byte | <1 s |
| Which filaments are physically there? | `present_filaments()` | **SLEEPs every board** | seconds |
| Does each emission path conduct, with the right resistance? | `mosfet_sweep()` | **HV on**, filaments cold | minutes |
| What is each filament's cold resistance R₀? | `sweep_filament_impedance()` | **energises** one at a time | ~1 min/filament |
| How much emission per heating current? | `emission_ramp()` / `emission_vs_heating()` | **HV on + ACTIVE** | s / tens of s |
| Did the schedule download intact? | `verify_schedule(plan)` | reads | <1 s |
| What actually fired, at what heating? | `scan_report()` / `shv_pulse_log()` | reads | <1 s |

---

## 2. Recommended order

**After swapping any hardware** (an RP2350, a control board, a cable) — cheapest
first, stop at the first failure:

```python
print(ct.pin_report())        # RP2350 GPIOs, read-only
with ct.lease(note="bring-up: pin probe"):
    print(ct.pin_probe())     # the HV-chain pins really move
print(ct.i2c_stats(clear=True))   # zero the counters for a clean window
print(ct.diagnosis())         # every chip, every board
print(ct.i2c_stats())         # what the diagnosis cost the buses
with ct.lease(note="bring-up: HV switches"):
    print(ct.hv_switch_test(controller=1, channel_mask=0x3F))
```

**Start of the day** (read-only, safe with anything running):

```python
print(ct.board_health())
print(ct.i2c_stats())
```

**Before a scan:** `board_health()`, then (with HV planned for the scan)
`mosfet_sweep()` on the filaments you will fire, then `verify_schedule(plan)`
after `download(plan)`.

**When something looks wrong:** [section 9](#9-symptom--test).

---

## 3. Read-only checks

Safe at any time — including while a schedule is armed or running, except
`pin_report()` which the firmware refuses (Busy) during a run.

### board_health() — lost boards and dark channels

The RP2350 tracks boards that stop answering (**LOST**: ≥ 2 failed visits over
≥ 1 s, probed on a backoff 0.25 → 5 s, restored to their *commanded* state when
they answer — an ACTIVE board goes back through a converged IDLE first) and
channels whose mux stops answering (**DARK**). This reads that state. No I²C.

```python
r = ct.board_health()
print(r)
lost = r["controllers"]["1"]["lost"]          # e.g. ["CH1.7", "CH2.7"]
```

**(real)** — a controller with nothing lost:

```
Result(ok)
  controllers:
    2:
      ok: True
      lost: []
      dark: []
      channels:
        CH1: state=ok  boards_ok=8  lost=-  recovering=-  dark_for_ms=None
             times_dark=0
        ...
        CH6: state=ok  boards_ok=8  lost=-  recovering=-  dark_for_ms=None
             times_dark=0
      attention: {}
  (detail hidden -- r.full() prints every field; the call log keeps them all)
```

How to read it:
- `lost` — boards not answering now. An **empty slot** that was commanded on
  (e.g. by a whole-controller STANDBY) shows here forever, and that is correct.
- `attention` — only boards with something to say: lost now, recovering, or
  lost at some point since boot (`lost_count`, `recover_count`).
- `times_dark` > 0 — the channel's mux dropped out and came back. Check that
  channel's control cable and supply.
- Boards at STOP are never "lost": their INA is unpowered there on purpose.

### i2c_stats() — is the bus wedged, or are the chips silent?

The RP2350's per-channel I²C counters (the console's `i2cstat`). No I²C.

```python
print(ct.i2c_stats())             # since boot / last clear
ct.i2c_stats(clear=True)          # read, then zero: start a clean window
# ... do the thing you suspect ...
print(ct.i2c_stats())             # exactly what it cost
```

**(real)**:

```
        CH1: timeouts=0  nacks=73  recoveries=0  bus_clears=0  sda_stuck=0
             retry_fails=0  nak_streak=0  max_nak_streak=2  last_ok_ms_ago=14
             bad_ina_reads=0  last_timeout_ms_ago=None  last_timeout_addr=None
             bus=pio
```

| Pattern | Meaning |
|---|---|
| `timeouts`, `recoveries`, `bus_clears` climbing | the **bus wedged** (a slave held SDA) |
| `sda_stuck` > 0 | SDA still low after the unwedge — **hardware** |
| `nak_streak` growing and never resetting, `timeouts` 0 | the channel's **chips stopped answering** (cable, their supply) — the "dark channel" signature |
| only `nacks` | normal — every probe of an empty slot is a NAK |
| `bad_ina_reads` > 0 | corrupted INA219 transfers, discarded (seen on C1) |

### chip_health() — which chips answer

```python
print(ct.chip_health())
```

A presence scan per board. **Not** the same as `present_filaments()`: a board
can answer I²C here and still be unusable as a filament.

### diagnosis() — does each chip really work?

```python
r = ct.diagnosis()
print(r)
```

Each chip on each board lands in **op** (operational) / **reg** (answers
register reads) / **addr** (ACKs its address only) / **missing**. Three things
that look like faults and are not:

- `hv_io_state` never better than `addr` on every channel — the 0x23 expander
  is **not fitted** on these boards.
- `tps_state`/`ina_state` = `addr` with the filaments at **STOP** — those chips
  sit behind the isolated rail, which is off at STOP. SLEEP the boards first.
- `tps`/`ina` never reach `op` — by design; `reg` is their ceiling.

A **whole channel** reading `missing` (mux and all expanders) is a dark
channel: see `i2c_stats()` to tell a wedged bus from silent chips.

### pin_report() — the RP2350's own GPIOs

Every GPIO the firmware uses: owner, levels, and a verdict. Read-only, ~50 ms.

```python
r = ct.pin_report(controller=1)
print(r)
```

**(real)** — healthy:

```
Result(ok)
  controllers:
    2:
      ok: True
      summary: 41 pins, 0 FAIL, 0 WARN
      fail: []
      warn: []
      inputs:
        MISO: input: high 0/50, 0 edges
        READY_IN: input: high 0/50, 0 edges
        SYNC_IN: input: high 0/50, 0 edges
  (detail hidden -- r.full() prints every field; the call log keeps them all)
```

- **FAIL** — a driven output whose pad reads the other level (*held from
  outside*: the 2026-09-24 GP31/SCK fault), an I²C line stuck, a pin on the
  wrong owner, pad isolation on.
- **WARN** — an interrupt line held asserted the whole window.
- **A pass here does not clear a pin.** A driver that died at the level it is
  driven to reads fine. That is what `pin_probe()` is for.

---

## 4. Checks that drive pins or switches

**HV must be off.** Take the lease. The firmware refuses these (Busy) while a
schedule is armed or running.

### pin_probe() — do the HV-chain pins actually move?

Drives SCK, MOSI, LOAD_N, CLKINH, S0–S2 high and low and times each edge, then
clocks the 165 read-back chain 64 bits. The LATCH pins are never touched, so
**no grid switch can move**.

```python
with ct.lease(note="pin probe"):
    r = ct.pin_probe(controller=1)
print(r)
```

**(real)** — healthy chip, control board unplugged:

```
      pins:
        SCK:    verdict=OK  gpio=31  rise=1 us  fall=0 us  note=None
        MOSI:   verdict=OK  gpio=25  rise=0 us  fall=0 us  note=None
        ...
      chain_165: 0000000000000000000000000000000000000000000000000000000000000000
      chain_transitions: 0
      chain_note: MISO never moved (stuck, or clock/load not reaching the
          chips; expected with the control board unplugged)
```

- Every edge should be 0–2 µs. `>20000 us` = the pin **never followed** (dead
  driver or held from outside); a slow edge names its load.
- The 165 clock-out reads whichever channel S0–S2 currently address, and with
  every grid switch off all its inputs are equal — so a **constant stream is
  normal in circuit** (on C1, 2026-09-25: all 1s, S0–S2 at CH8). It only proves
  something out of circuit. The real in-circuit chain check is
  `read_hv_diag165()` / `hv_switch_test()` (HV off).

**(real)** — C1 in circuit, power on:

```
        SCK:    verdict=OK  gpio=31  rise=1 us  fall=1 us  note=None
        ...
      chain_165: 1111111111111111111111111111111111111111111111111111111111111111
      chain_transitions: 0
```

### self_test() — do the I/O expanders toggle?

```python
with ct.lease(note="expander self-test"):
    print(ct.self_test())
```

Toggles the TCA9554 expander outputs and reads them back, plus a chip scan.
Refused on a controller running a schedule (reported per controller).

### hv_switch_test() — does every HV grid switch close and open?

**HV voltage at 0.** It really switches every grid MOSFET on and off.

```python
with ct.lease(note="HV switch test"):
    r = ct.hv_switch_test(controller=1, channel_mask=0x3F)   # CH1-CH6
print(r)
print(r["dead"], r["inconclusive"])
```

- `channel_mask`: `0x3F` = CH1–6. The default tests all 8 (7 and 8 unused).
- Four outcomes per switch: **pass**; **dead** (never actuated); **stuck_on**
  (closed but would not open — worse than dead: the grid stays connected);
  **inconclusive** (the two read-backs disagreed — a marginal switch).

### read_hv_diag165(controller, channel) — the 165 chain of one channel

```python
print(ct.read_hv_diag165(controller=1, channel=1))   # CH2
```

Writes `test_byte` (0x55), reads it back twice, clears. `r1`/`r2` should equal
`test_byte`, `r0`/`r3` zero. A mismatch that is stable = wiring/alignment; one
that changes between `r1` and `r2` = timing/noise.

### present_filaments() — which filaments are physically there

```python
with ct.lease(note="presence scan"):
    present = set(ct.present_filaments())
print(sorted(present))
# To mark everything absent as dead (this REPLACES the dead mask):
# ct.set_dead(sorted(set(range(96)) - present), reason="not fitted")
```

SLEEPs every board to power the presence rail, re-scans, **leaves them at
SLEEP**. Slow; run once at setup. Returns `[]` on failure — check
`ct.status()` if it comes back empty.

---

## 5. The emission path: MOSFET R_eq sweep

Every filament **cold** (SLEEP: the isolated rail on, no heating), so the only
current path is the sub-board's two diodes and its 100 kΩ resistor through the
grid MOSFET: I = (V − Vf) / R. One voltage cannot tell a healthy path from a
front-end offset (a −0.4 mA offset called good MOSFETs dead), so the rail is
stepped through **n ≥ 3 voltages** and each filament's I–V points are fitted
to a line: the **slope gives R_eq**; an offset only moves the intercept.

```python
with ct.lease(note="MOSFET R_eq sweep"):
    r = ct.mosfet_sweep([0, 1, 2, 3], v_start=30, v_step=10, n=4, limit_ma=30)   # 30/40/50/60 V
print(r)          # leave out the list to sweep every live filament (minutes)
```

- **Emission must be OFF before you start** — the sweep sets its own
  voltages and refuses to write over a rail someone left on.
- **The rail is read back at every step**; the sweep stops at the first step
  more than 10 % off. At a 30 mA limit this bench's rail follows to 60 V and
  stops near 65 V — keep the sweep at or below 60 V until that is explained.
- The rail comes down **first** on teardown, then the filaments go to STOP.
- Only filaments on **connected** controllers are tested.

Verdicts per filament (nominal 100 kΩ, ±35 %):

| verdict | meaning |
|---|---|
| `pass` | conducts, R_eq within tolerance, r² ≥ 0.9 |
| `dead` | slope ≤ 0, R_eq > 5× nominal, or the top current < 30 % of expected — **not conducting** |
| `odd` | conducts, but R_eq is off or the fit is poor |
| `unmeasured` | fewer than 3 usable points |

`r["results"][f]["points"]` has the raw `[V, mA]` pairs.

**(real)**, 50 V, 30 mA limit — a healthy path reads ~0.50 mA (expected
0.47 mA); F0/F8/F76 read about −1.5 mA at every voltage, which the fit flags
rather than calling them dead.

The GUI's **Calibration & Test → 4 · Emission path R_eq** runs the same sweep
with a live plot and saves `calibration/emission_path_req_<time>.json`.

`mosfet_test(emission_v=..., limit_ma=30)` is the single-voltage version —
quicker, but it cannot see through an offset. Prefer the sweep.

---

## 6. Calibration

All calibration records go to the **backend machine's** `calibration/`
folder (JSON + CSV), whichever machine the script runs on.

### Cold resistance R₀ — sweep_filament_impedance()

```python
with ct.lease(note="R0 sweep"):
    r = ct.sweep_filament_impedance([1, 2, 3], cool_s=60)
print(r)
print({f: x["R0_ohm"] for f, x in r["results"].items()})
```

Holds VOLTAGE mode, steps up, reads the INA219 at each step, fits
V = a·I³ + R₀·I. One filament energised at a time, STOPped before the next.

- **R₀ is cold only if the filament was cold.** Pass `cool_s` (wait before
  sweeping). The sweep always returns to the start voltage and compares:
  drift marks `r0_is_cold: False` — the number is still returned, never
  labelled cold.
- Slow: roughly `dwell_s × steps` per filament. Pass a subset.
- `save=True` (default) writes `impedance_sweep_<time>.json/.csv`.
- A curve that cannot be fitted gives `R0_ohm: None`, verdict `no_fit` —
  never 0 Ω (which would read as a short).

### HV voltage LUT (GUI)

The **Cal** button next to **Emission −V** (or **Focus −V**) on the HV card
(also on the "HV Control & Monitor" card): sweeps that channel's DS3502 wiper
0 → 127, reads the rail at each step, and stores the wiper → volts table the
backend then uses for every **Set V** and every `set_emission_v()` /
`set_focus_v()`. Run it after changing anything in the HV supply. Turn that
channel's HV on first — the sweep refuses to energise it for you — and it
zeroes the wiper when done. (GUI only; there is no client method.)

**The emission current limit.** At 200 V use a 55 mA limit, as below: on this
bench a 30 mA limit holds the rail at about 65 V (it reached 197.8 V at 55 mA).

### Emission pedestal — measure_emission_pedestal()

The non-emission part of a pulse's current (the diode path), measured at a
heating too low to emit. **HV must already be on at the voltage you will
measure the curve at** — the pedestal scales with the rail.

```python
ct.set_emission_i(55); ct.set_emission_v(200); ct.enable_emission(True)
try:
    with ct.lease(note="pedestal"):
        p = ct.measure_emission_pedestal(1, heat_ma=1400)
    print(p)          # pedestal_ma, width_independent, temperature_independent
finally:
    ct.enable_emission(False)
```

`pedestal_ma` is `None` when the checks did not hold (not steady, or not
temperature-independent) — never a quietly wrong number.

### Emission vs heating — quick: emission_ramp()

Is this filament emitting, and roughly how much. One ramp through the CC
loop, seconds at ACTIVE. No temperature.

```python
ct.set_emission_i(55); ct.set_emission_v(200); ct.enable_emission(True)
try:
    with ct.lease(note="emission ramp F1"):
        r = ct.emission_ramp(1, from_ma=1500, to_ma=2800)
    print(r)          # span_ma, gap_max_ma, one point per shot
finally:
    ct.enable_emission(False)
```

`span_ma` / `gap_max_ma` report what the ramp actually covered — a ramp that
finished early bunches every shot at the top and shows as a large gap.

### Emission vs heating — precise: emission_vs_heating() + fit_richardson()

Steps ACTIVE through setpoints, fires `pulses_per_point` shots at each, and
measures V and I with the shots, so every point has a resistance and a
temperature. Tens of seconds at firing current.

```python
ct.set_emission_i(55); ct.set_emission_v(200); ct.enable_emission(True)
try:
    with ct.lease(note="emission curve F1"):
        r = ct.emission_vs_heating(1, start_ma=2500, max_ma=2800, step_ma=100,
                                   save_as="emission_curve")
    print(r)
    f = ct.fit_richardson(r)
    print(f)          # work_function_eV, r_lead_ohm, trustworthy, warnings
finally:
    ct.enable_emission(False)
```

- It walks the full ladder (STOP → SLEEP → STANDBY → IDLE → ACTIVE), steps
  **upward** only, never above `max_ma`, and leaves the filament at `end_state`
  (STOP) whatever happens.
- The x-axis is `heat_mA` — the firmware's snapshot of the heating current **at
  the instant each pulse fired** — not the commanded value. Shots that fired
  more than 20 % below target are dropped from the point's mean (`n_cold`).
- `fit_richardson()`: read `trustworthy` and `warnings` first, then
  `r_lead_plateau_ohm` (a wide plateau = R_lead is not determined by this
  data). A work function outside 1.5–6 eV means the model does not fit.
- Several filaments into one file: `ct.save_emission_curves("run1", {1: r1, 2: r2})`.

---

## 7. Schedule checks

A complete, safe run of the path: a one-shot plan with no heating steps,
downloaded and verified (nothing fires), then one COLD shot (filament at
SLEEP, HV off — a dry run of the switching), and the records of it.

```python
# One emission row: filament 1 fires at trigger 0. no_heat: no ACTIVE steps.
plan = ct.build_scan_plan([{"filament": 1, "trigger": 0}], no_heat=[1])
print(ct.download(plan))
v = ct.verify_schedule(plan)          # counts + CRC read back from every controller
print(v)

cursor = ct.pulse_cursor()            # "since" for the report below
with ct.lease(note="one cold shot"):
    ct.stop_one(1, verify=True)
    ct.sleep_one(1, verify=True)      # SLEEP: the iso rail on, no heating
    r = ct.fire_single_pulse(1, trigger="sim", measure=True)
    ct.stop_one(1, verify=True)
print(r)                              # one line per pulse: time, heating at that instant
print(ct.shv_pulse_log())             # the same, from the RP2350's own log
print(ct.scan_report(since=cursor, plan=plan))
```

- `verify_schedule(plan)` after `download(plan)`, before arming — catches a
  partial or corrupted transfer before anything fires.
- `fire_single_pulse` downloads its own one-shot schedule, arms, triggers and
  measures. In a real scan the filament goes up the ladder to ACTIVE first;
  here it stays at SLEEP, so the heating column reads the standby level.
- `shv_pulse_log()` prints one line per pulse: when it fired, the filament,
  the width, and the **heating current at that instant** (measured / target /
  diff), plus switch-verify flags.
- `scan_report()` puts the three records side by side (what fired, the run's
  counters, what the STM32 measured); the disagreements are the findings.
  `fired_cold` lists pulses that landed below their heating setpoint.

---

## 8. USB console

The RP2350's USB serial console still has commands the API does not wrap.
Connect at any baud (it is USB CDC):

```bash
python3 -m serial.tools.miniterm /dev/cu.usbmodemXXXX 115200
```

**Make sure it is the controller you think it is**: two RP2350s can be on USB
at once and the port names move whenever one is replugged. SLEEP one filament
of the target controller through the backend and check which chip's `ccstat`
shows it.

| Command | What | API equivalent |
|---|---|---|
| `health` | lost / dark + power-job internals | `board_health()` (+ internals only here) |
| `i2cstat` / `i2cstat clear` | bus counters | `i2c_stats()` |
| `pinreport` / `pinprobe` | GPIO health | `pin_report()` / `pin_probe()` |
| `ccstat` | per-board power state, CC mode, V/I, revives | — |
| `safecheck <ch> <bit>` | why a board is refused for firing | — |
| `i2creinit <ch>` | rebuild one channel's I²C state machine | — |
| `scan` | presence scan (**forces IsoPower on**) | `chip_health()` (read-only) |
| `test hv` / `test i2c` / `test mux` | firmware self-tests | `hv_switch_test()` / `diagnosis()` / `self_test()` |
| `shvtest` | self-contained PIO schedule run | — |
| `bootsel` | reboot into the UF2 bootloader (flashing) | — |

Avoid `tps 4500` and `test` on a live rig: they energise outputs.

---

## 9. Symptom → test

| Symptom | Run | Then |
|---|---|---|
| A whole channel vanished from the GUI | `board_health()` | DARK → `i2c_stats()`: timeouts = bus, NAK streak only = cable/supply |
| One board shows absent while on | `board_health()` | LOST: it is being probed and restored; check that board's cable |
| Commands "did not land" on some boards | `i2c_stats(clear=True)`, repeat the command, `i2c_stats()` | many NAKs under load = marginal bus |
| HV "did not turn on / off" on pulses | `hv_switch_test()` | dead / stuck_on / inconclusive per switch |
| Every switch on a controller dead | `pin_report()` then `pin_probe()` | a pin that will not follow = RP2350 or its SCK/latch line |
| Emission ~0 on one filament | `mosfet_sweep([f])` | dead = MOSFET/path; pass = look at the filament |
| Emission lower than yesterday | `emission_ramp(f)` | compare with the saved curve |
| Filament resistance changed | `sweep_filament_impedance([f], cool_s=60)` | R₀ vs its earlier record |
| Schedule fired but data looks off | `shv_pulse_log()`, `scan_report()` | heating at each pulse, fired_cold, counters |
