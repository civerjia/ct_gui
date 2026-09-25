# Tests, self-tests and calibration — tutorial

Every hardware check and calibration the controllers offer, from Python. **Each
example is complete: copy one block into a file (or a Python prompt) and run
it.** The only line you may need to change is the backend address.

The GUI's **Calibration & Test** panel runs several of the same tests; this is
the scripted side. Outputs marked **(real)** were captured on this bench on
2026-09-25; nothing shown is invented.

**What each example does to the hardware** — read the tag before running:

| Tag | Means |
|---|---|
| 🟢 **read-only** | reads counters and caches; safe any time, even during a schedule run |
| 🟡 **drives pins / switches** | actuates pins, expanders or grid switches; **HV must be off**; takes the lease |
| 🟠 **HV on, filaments cold** | turns the emission rail on (≤ 60 V here); filaments stay at SLEEP (no heating) |
| 🔴 **heats a filament** | heats to IDLE / ACTIVE (firing current). **Only with the bench owner's go-ahead, for that filament, that current, that time.** |

Contents

- [0. Setup](#0-setup)
- [1. Which test answers which question](#1-which-test-answers-which-question)
- [2. Recommended order](#2-recommended-order)
- [3. 🟢 Read-only checks](#3-read-only-checks)
- [4. 🟡 Pins, expanders, switches (HV off)](#4-pins-expanders-switches)
- [5. 🟠 The emission path: MOSFET R_eq sweep](#5-the-emission-path-mosfet-r_eq-sweep)
- [6. Calibration (🔴 heats)](#6-calibration)
- [7. 🟠 Schedule checks](#7-schedule-checks)
- [8. USB console](#8-usb-console)
- [9. Symptom → test](#9-symptom--test)
- [10. Known issues](#10-known-issues)

---

## 0. Setup

```bash
pip install -r requirements.txt       # once, in the ct_gui folder
```

Every example starts with these two lines. Put **your backend's** address in
the first one — the IP the backend prints in its banner (`shared API on
http://<ip>:8770`):

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")
print(ct.status())            # both controllers "connected": True?
```

If a controller shows `connected: False` (e.g. right after the backend
restarted), connect it once — here C1 at .242 and C2 at .203:

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")
print(ct.connect(1, "192.168.8.242"))
print(ct.connect(2, "192.168.8.203"))
```

How results work, for every call below:

- **`print(r)` shows the answer.** Detail is left out and the print says so
  (`detail hidden ...`). `print(r.full())` shows every field; the call log
  (`logs/client/*.jsonl` on the machine running the script) keeps the whole
  result anyway. In code, every field is there: `r["..."]`.
- **Failures are results, not exceptions.** `r["ok"]` is False and `r["error"]`
  / `r["problems"]` say why. A test that could not measure says so
  (`unmeasured`, `inconclusive`, `None`) — never a plausible-looking 0.
- **Per controller.** Diagnostics cover every connected controller, or one with
  `controller=1` / `2`; results sit under `r["controllers"]["1"]`.
- **Anything that changes hardware takes the lease** (`with ct.lease(...)`), so
  nobody else's command lands in the middle. Reads never need it.
- Channels 7 and 8 are not used; the diagnostics look at CH1–CH6.

---

## 1. Which test answers which question

| Question | Method | Tag | Time |
|---|---|---|---|
| Is a board lost / a channel dark right now? | `board_health()` | 🟢 | ~1 s |
| Is an I²C bus wedged, or are the chips just silent? | `i2c_stats()` | 🟢 | ~1 s |
| Which chips answer on each board? | `chip_health()` | 🟢 | ~1 s |
| Does each chip answer registers, or only its address? | `diagnosis()` | 🟢 | < 1 s |
| Are the RP2350's own GPIOs healthy? | `pin_report()` | 🟢 | < 1 s |
| Do the HV-chain output pins actually move? | `pin_probe()` | 🟡 | < 1 s |
| Do the I/O expanders toggle? | `self_test()` | 🟡 | ~1 s |
| Does every HV grid switch close and open? | `hv_switch_test()` | 🟡 | ~25 s |
| Is the 165 read-back chain intact on a channel? | `read_hv_diag165()` | 🟡 | < 1 s |
| Which filaments are physically there? | `present_filaments()` | 🟡 (SLEEPs boards) | ~3 s |
| Does each emission path conduct, with the right R? | `mosfet_sweep()` | 🟠 | ~20 s per filament |
| Cold resistance R₀ of a filament | `sweep_filament_impedance()` | 🔴 (voltage sweep) | ~40 s per filament |
| Emission per heating current | `emission_ramp()` / `emission_vs_heating()` | 🔴 ACTIVE | 15 s / ~50 s |
| Did the schedule download intact? | `verify_schedule()` | 🟢 | < 1 s |
| What fired, at what heating? | `shv_pulse_log()` / `scan_report()` | 🟢 | < 1 s |

---

## 2. Recommended order

**After swapping hardware** (an RP2350, a control board, a cable) — cheapest
first; stop at the first failure. 🟡, HV off:

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

print(ct.pin_report())                 # RP2350 GPIOs, read-only
with ct.lease(note="bring-up"):
    print(ct.pin_probe())              # the HV-chain pins really move
    ct.i2c_stats(clear=True)           # zero the counters: a clean window
    print(ct.diagnosis())              # every chip on every board
    print(ct.i2c_stats())              # what the diagnosis cost the buses
    print(ct.hv_switch_test(controller=1, channel_mask=0x3F))
```

**Start of the day** 🟢 (safe with anything running):

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

print(ct.board_health())
print(ct.i2c_stats())
```

**Before a scan:** `board_health()`, then `mosfet_sweep()` on the filaments you
will fire, then `verify_schedule(plan)` after `download(plan)` ([section 7](#7-schedule-checks)).

---

## 3. Read-only checks

🟢 Safe any time, including during a schedule run — except `pin_report()`,
which the firmware refuses (Busy) while a schedule is armed or running.

### board_health() — lost boards and dark channels

The RP2350 marks a board **LOST** when it fails ≥ 2 visits over ≥ 1 s: its
control work is suspended and it is probed on a backoff (0.25 → 5 s). When it
answers again it is brought back to the state it was **commanded** (an ACTIVE
board through a converged IDLE first). A channel whose mux stops answering is
**DARK**: only the mux is probed until it returns. Boards at STOP are never
lost — their INA is unpowered there on purpose.

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

r = ct.board_health()
print(r)
for c, h in r["controllers"].items():       # one entry per connected controller
    print(f"C{c} lost: {h['lost']}  dark: {h['dark']}")
```

**(real)**:

```
Result(ok)
  controllers:
    1:
      ok: True
      lost: []
      dark: []
      channels:
        CH1: state=ok  boards_ok=8  lost=-  recovering=-  dark_for_ms=None
             times_dark=1
        CH2: state=ok  boards_ok=8  lost=-  recovering=-  dark_for_ms=None
             times_dark=0
        ...
      attention: {}
```

- `lost` — boards not answering **now**. An empty slot that was commanded on
  (a whole-controller STANDBY) sits here, and that is correct.
- `attention` — only boards with something to say (lost now, recovering, or a
  history of it).
- `times_dark` > 0 — that channel's mux dropped out and came back on its own.
  Check its control cable and supply.

### i2c_stats() — is the bus wedged, or are the chips silent?

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

print(ct.i2c_stats())               # since boot / the last clear
ct.i2c_stats(clear=True)            # read, then zero: start a clean window
# ... do the thing you suspect here ...
print(ct.i2c_stats())               # exactly what it cost
```

**(real)**:

```
        CH1: timeouts=0  nacks=142  recoveries=0  bus_clears=0  sda_stuck=0
             retry_fails=0  nak_streak=1  max_nak_streak=2  last_ok_ms_ago=24
             bad_ina_reads=0  last_timeout_ms_ago=None  last_timeout_addr=None
             bus=pio
```

| Pattern | Meaning |
|---|---|
| `timeouts` / `recoveries` / `bus_clears` climbing | the **bus wedged** (a slave held SDA) |
| `sda_stuck` > 0 | SDA still low after the unwedge — **hardware** |
| `nak_streak` growing and never resetting, `timeouts` 0 | the channel's **chips stopped answering** (cable, supply) — the dark-channel signature |
| only `nacks` | normal — every probe of an empty slot is a NAK |
| `bad_ina_reads` > 0 | corrupted INA219 transfers, discarded |

### chip_health() — which chips answer

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

print(ct.chip_health())
```

Not the same as `present_filaments()`: a board can answer I²C and still be
unusable as a filament.

### diagnosis() — does each chip really work?

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

print(ct.diagnosis())
```

Each chip lands in **op** / **reg** / **addr** / **missing**. Three things that
look like faults and are not: `hv_io_state` never better than `addr` (that
expander is not fitted); `tps`/`ina` at `addr` with the filaments at STOP (their
isolated rail is off — SLEEP them first); `tps`/`ina` never `op` (`reg` is their
ceiling). A whole channel `missing` is a dark channel — see `i2c_stats()`.

### pin_report() — the RP2350's own GPIOs

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

print(ct.pin_report())
```

**(real)**:

```
      ok: True
      summary: 41 pins, 0 FAIL, 0 WARN
      fail: []
      warn: []
      inputs:
        MISO: input: high 50/50, 0 edges
        READY_IN: input: high 0/50, 0 edges
        SYNC_IN: input: high 0/50, 0 edges
```

**FAIL** = a driven output whose pad reads the other level (held from outside —
the 2026-09-24 GP31/SCK fault), a stuck I²C line, a wrong owner, pad isolation.
**A pass here does not clear a pin**: a driver that died at the level it is
driven to reads fine. That is what `pin_probe()` is for.

---

## 4. Pins, expanders, switches

🟡 **HV must be off.** These take the lease and are refused (Busy) while a
schedule is armed or running.

### pin_probe() — do the HV-chain pins move?

Drives SCK, MOSI, LOAD_N, CLKINH, S0–S2 high and low and times each edge, then
clocks the 165 read-back chain. The LATCH pins are never touched: **no grid
switch can move.**

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

with ct.lease(note="pin probe"):
    print(ct.pin_probe())
```

**(real)**, C1 in circuit:

```
        SCK:    verdict=OK  gpio=31  rise=1 us  fall=1 us  note=None
        MOSI:   verdict=OK  gpio=25  rise=0 us  fall=0 us  note=None
        ...
      chain_165: 1111111111111111111111111111111111111111111111111111111111111111
      chain_transitions: 0
```

Every edge should be 0–2 µs; `>20000 us` = the pin never followed. The 165
clock-out reads whichever channel S0–S2 address, and with every switch off its
inputs are all equal, so **a constant stream is normal in circuit**. The real
in-circuit chain test is `read_hv_diag165()` / `hv_switch_test()`.

### self_test() — do the I/O expanders toggle?

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

with ct.lease(note="expander self-test"):
    print(ct.self_test())
```

### hv_switch_test() — does every HV grid switch close and open?

**HV voltage at 0.** It really switches every grid MOSFET on and off.

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

with ct.lease(note="HV switch test"):
    r = ct.hv_switch_test(controller=1, channel_mask=0x3F)    # CH1-CH6
print(r)
```

**(real)**, C1:

```
Result(ok)
  pass: True
  stuck_on: []
  dead: []
  inconclusive: []
  counts:
    pass: 48
  channels: [1, 2, 3, 4, 5, 6]
  restored_off: 48/48
```

Per switch: **pass**; **dead** (never actuated); **stuck_on** (closed, would not
open — the grid stays connected, worse than dead); **inconclusive** (the two
read-backs disagreed — marginal). `r["results"]` has every switch.

### read_hv_diag165() — the 165 chain of one channel

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

with ct.lease(note="165 diag"):
    print(ct.read_hv_diag165(controller=1, channel=1))       # CH2
```

Writes `test_byte` (0x55), reads it back twice, clears. `r1`/`r2` should equal
the test byte and `r0`/`r3` zero. A stable wrong value = wiring/alignment; one
that changes between `r1` and `r2` = timing/noise.

### present_filaments() — which filaments are there

SLEEPs every board to power the presence rail, re-scans, and leaves them at
SLEEP (no heating); the example puts them back to STOP.

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

with ct.lease(note="presence scan"):
    present = sorted(ct.present_filaments())
    print(present)
    print(ct.stop_all(verify=True))      # back to STOP
```

To mark everything absent as dead (**this replaces the dead mask**):
`ct.set_dead(sorted(set(range(96)) - set(present)), reason="not fitted")`.

---

## 5. The emission path: MOSFET R_eq sweep

🟠 **HV on (≤ 60 V), filaments cold.** Every filament tested is at SLEEP: the
isolated rail on, no heating. The only current path is the sub-board's two
diodes and its 100 kΩ resistor through the grid MOSFET: I = (V − Vf) / R. One
voltage cannot tell a healthy path from an offset, so the rail is stepped
through **n ≥ 3 voltages** and each filament's points are fitted: the **slope
gives R_eq**, an offset only moves the intercept.

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

with ct.lease(note="MOSFET R_eq sweep"):
    r = ct.mosfet_sweep([0, 1, 2, 3], v_start=30, v_step=10, n=4, limit_ma=30)   # 30/40/50/60 V
print(r)
```

Leave the list out to sweep every live filament (minutes). **(real)**:

```
  volts: [30, 40, 50, 60]
  rail_v: [-29.5, -40, -50.3, -60.6]
    1:
      points: [[29.5, 0.279], [40, 0.404], [50.3, 0.474], [60.6, 0.5853]]
      r_kohm: 104.7
      r2: 0.99
      verdict: pass
    0:
      points: [[29.5, -1.3175], [40, -1.1987], [50.3, -1.617], [60.6, -1.9933]]
      verdict: dead
      note: not conducting
```

| verdict | meaning |
|---|---|
| `pass` | conducts, R_eq within ±35 % of 100 kΩ, r² ≥ 0.9 |
| `dead` | slope ≤ 0, R_eq > 5× nominal, or the top current < 30 % of expected |
| `odd` | conducts, but R_eq is off or the fit is poor |
| `unmeasured` | fewer than 3 usable points |

- **Emission must be OFF before you start** — the sweep sets its own voltages.
- The rail is **read back at every step**; the sweep stops at a step more than
  10 % off. At a 30 mA limit this bench's rail follows to 60 V and stops near
  65 V — stay at or below 60 V with `limit_ma=30`.
- On teardown the rail goes **off first**, then the filaments go to STOP.
- A **negative** current like F0's above is not a real current — see
  [known issues](#10-known-issues).

The GUI's **Calibration & Test → 4 · Emission path R_eq** runs the same sweep.

---

## 6. Calibration

Records land in the **backend machine's** `calibration/` folder (JSON + CSV).

**🔴 Every example in this section heats a filament. Run one only after the
bench owner has agreed to that filament, that current and that time.** They
walk the power ladder themselves (STOP → SLEEP → STANDBY → IDLE → ACTIVE) and
leave the filament at STOP whatever happens.

### Cold resistance R₀ — sweep_filament_impedance()

🔴 VOLTAGE mode, 0.8 → 1.5 V by default (about 1 A through a filament), one
filament at a time.

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

with ct.lease(note="R0 sweep"):
    r = ct.sweep_filament_impedance([1], cool_s=60)
print(r)
print(r["results"][1]["R0_ohm"], r["results"][1]["verdict"], r["results"][1].get("reason"))
```

**(real)** — on this bench the defaults do **not** give a fit:

```
      curve: [8 items]
        [0] mv_set=800  v=0.78  i=1.025
        ...
        [7] mv_set=1500  v=1.468  i=0.998
      hysteresis:
        r_open_ohm: 0.760976
        r_return_ohm: 1.32677
        drift_frac: 0.743506
      R0_ohm: None
      verdict: no_fit
      reason: current moved only 8% over the sweep (collinearity 0.9963 ≥
          0.95) — R₀ and the I² term are not separable
```

The filament heats as the voltage rises, so the current barely moves (~1 A
throughout) and the return point reads 74 % higher: the steps are shorter
than the filament's thermal settling. The function says so and gives no number
rather than a wrong one. Parameters that fit on this bench are not established
yet ([known issues](#10-known-issues)).

### HV voltage LUT (GUI only)

The **Cal** button next to **Emission −V** (or **Focus −V**) on the HV card:
sweeps that channel's DS3502 wiper 0 → 127, reads the rail at each step, and
stores the wiper → volts table every **Set V** / `set_emission_v()` uses. Turn
that channel's HV on first (the sweep will not energise it) — it zeroes the
wiper when done. Rerun after changing anything in the HV supply.

### Emission current limit

At 200 V use a **55 mA** limit: on this bench a 30 mA limit holds the emission
rail at about 65 V; at 55 mA it reaches 197.8 V.

### Emission pedestal — measure_emission_pedestal()

🔴 Heats to 1400 mA (below emission) plus a STANDBY check. HV must be on at the
voltage the curve will be measured at — the pedestal scales with the rail.

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

ct.set_emission_i(55); ct.set_emission_v(200); ct.enable_emission(True)
try:
    with ct.lease(note="pedestal F1"):
        p = ct.measure_emission_pedestal(1, heat_ma=1400)
    print(p)
finally:
    ct.enable_emission(False)
```

**(real)**: `pedestal_ma: 1.994`, `temperature_independent: True` — matching
the calculated diode path (200 − 2.82) V / 100 kΩ = 1.97 mA.

### Emission vs heating — quick: emission_ramp()

🔴 **ACTIVE**, one ramp through the CC loop up to `to_ma`, a few seconds.

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

ct.set_emission_i(55); ct.set_emission_v(200); ct.enable_emission(True)
try:
    with ct.lease(note="emission ramp F1"):
        r = ct.emission_ramp(1, from_ma=1500, to_ma=2800)
    print(r)
finally:
    ct.enable_emission(False)
```

Each shot reports the heating current **at the instant it fired**. `span_ma` /
`gap_max_ma` say what the ramp actually covered.

### Emission vs heating — precise: emission_vs_heating() + fit_richardson()

🔴 **ACTIVE for tens of seconds** (~43 s for the four points below).

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

ct.set_emission_i(55); ct.set_emission_v(200); ct.enable_emission(True)
try:
    with ct.lease(note="emission curve F1"):
        r = ct.emission_vs_heating(1, start_ma=2500, max_ma=2800, step_ma=100,
                                   save_as="emission_curve")
    print(r)
    print(ct.fit_richardson(r))
finally:
    ct.enable_emission(False)
```

**(real)**, F1:

```
    [0] commanded_ma=2500  usable=True  n_used=3  n_fired=3  n_cold=0
        emission    emission_ma=2.621  emission_mams=2.6114  pedestal_ma=1.951
        heating     heat_mA=2469  heat_target_mA=2500  settled_ma=2472
    [1] commanded_ma=2600  usable=True  n_used=3  n_fired=3  n_cold=0
        emission    emission_ma=4.754  emission_mams=4.7592  pedestal_ma=1.951
        heating     heat_mA=2568  heat_target_mA=2600  settled_ma=2584
```

- The x-axis is `heat_mA`, the firmware's snapshot at each pulse — not the
  commanded value. Shots more than 20 % under target are dropped (`n_cold`);
  shots whose path did not conduct are dropped too (`n_not_conducted`).
- `fit_richardson()`: read `trustworthy` and `warnings` first. With a narrow
  heating range it says the lead resistance is not determined (it did here) —
  widen the range before quoting a work function.

---

## 7. Schedule checks

🟠 A complete, safe run of the path: a one-shot plan with **no heating
steps**, downloaded and verified (nothing fires), then one **cold** shot
(filament at SLEEP, HV off — a dry run of the switching) and its records.

```python
from ct_simple_control import CTClient
ct = CTClient("192.168.8.218", client_id="tutorial")

plan = ct.build_scan_plan([{"filament": 1, "trigger": 0}], no_heat=[1])
print(ct.download(plan))
print(ct.verify_schedule(plan))       # counts + CRC read back from every controller

cursor = ct.pulse_cursor()
with ct.lease(note="one cold shot"):
    ct.stop_one(1, verify=True)
    ct.sleep_one(1, verify=True)      # iso rail on, no heating
    r = ct.fire_single_pulse(1, trigger="sim", measure=True)
    ct.stop_one(1, verify=True)
print(r)
print(ct.shv_pulse_log())
print(ct.scan_report(since=cursor, plan=plan))
```

- `verify_schedule(plan)` after `download(plan)`, before arming, catches a
  partial or corrupted transfer before anything fires. A timed-out read is
  retried; a real mismatch never is.
- `print(r)` / `print(ct.shv_pulse_log())` give one line per pulse: when it
  fired, the filament, the width, the **heating current at that instant**
  (measured / target / diff), the emission, and flags (`ON-MISMATCH`,
  `STUCK-ON`, `NO-CURRENT`). **(real)**, from a shot at ACTIVE:

  ```
    pulses: [1]  (heat = RP2350 snapshot at the instant each pulse fired)
      #  fil  t_on ms  width us  heat mA  target  diff  emission mA  mA*ms  flags
      0    7      0.0      6000     2569    2600   -31        3.697  22.23  -
  ```
- `scan_report()` puts what fired, the run's counters and what the STM32
  measured side by side; `fired_cold` lists pulses below their heating setpoint.

---

## 8. USB console

The RP2350's USB serial console has a few commands the API does not wrap.

```bash
python3 -m serial.tools.miniterm /dev/cu.usbmodemXXXX 115200    # Windows: COMx
```

**Make sure it is the controller you think it is** — two RP2350s can be on USB
at once and the port names move whenever one is replugged: SLEEP one filament
of that controller through the backend and check which chip's `ccstat` shows it.

| Command | What | API equivalent |
|---|---|---|
| `health` | lost / dark + the power job's internals | `board_health()` |
| `i2cstat` / `i2cstat clear` | bus counters | `i2c_stats()` |
| `pinreport` / `pinprobe` | GPIO health | `pin_report()` / `pin_probe()` |
| `ccstat` | per-board power state, CC mode, V/I | — |
| `safecheck <ch> <bit>` | why a board is refused for firing | — |
| `i2creinit <ch>` | rebuild one channel's I²C state machine | — |
| `bootsel` | reboot into the UF2 bootloader | — |

Avoid `tps 4500`, `scan` and `test` on a live rig: they energise outputs.

---

## 9. Symptom → test

| Symptom | Run | Then |
|---|---|---|
| A whole channel vanished from the GUI | `board_health()` | DARK → `i2c_stats()`: timeouts = bus; NAK streak only = cable/supply |
| One board absent while on | `board_health()` | LOST: being probed and restored; check its cable |
| Commands "did not land" on some boards | `i2c_stats(clear=True)`, repeat, `i2c_stats()` | NAKs under load = marginal bus |
| HV "did not turn on / off" on pulses | `hv_switch_test()` | dead / stuck_on / inconclusive per switch |
| Every switch on a controller dead | `pin_report()`, `pin_probe()` | a pin that will not follow = RP2350 or its line |
| Emission ~0 on one filament | `mosfet_sweep([f])` | dead = the path; pass = look at the filament |
| Schedule fired but data looks off | `shv_pulse_log()`, `scan_report()` | heating at each pulse, fired_cold, counters |

---

## 10. Known issues

Open as of 2026-09-25; each is a measurement or test limitation, reported
by the code rather than hidden:

- **Negative pulse currents are never real.** Two sources:
  1. *Fixed:* a pulse whose switch did not close used to report
     `emission_ma` = 0 − the calculated diode current (−1.95 mA at 200 V). It now
     reports `path_conducted: False` and no emission (`NO-CURRENT` in the table).
  2. *Not fixed yet:* the **first filament fired on each channel** (CHx.1) can
     saturate the current front end with a large edge spike; the plateau then
     reads the ADC floor and the net current comes out ≈ −1.5 mA (F0 above).
     The MOSFET sweep calls such a filament `dead` — treat a CHx.1 `dead` with
     negative points as **unmeasured**, not as a failed MOSFET.
- **R₀ sweep** does not fit on this bench with its defaults (the current does
  not move enough; the filament is not in thermal equilibrium per step).
- **Emission rail at 30 mA** stops near 65 V; use ≤ 60 V there, 55 mA at 200 V.
- **Focus leak scan (GUI test 3)** reads the one shared emission-rail voltage
  against a fixed threshold with no baseline, so a leak that is present with
  every switch open flags **every** filament. Take its result as "there is a
  leak", not "these filaments leak".
