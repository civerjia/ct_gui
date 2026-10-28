# scan_view timing — paper design (2026-10-28)

Status: **design only, nothing built.** It replaces the timeout section of
`scan_view_design.md` once agreed. Open questions are at the end; every one
needs an answer before code.

Goal: as few timing knobs as possible, each with one meaning, the same name
everywhere it means the same thing.

## 1. What the user sets

| name | meaning | default | when it runs out |
|---|---|---|---|
| `settle_ms` | after a filament FIRST arrives (load voltage within ±150 mV), wait this long; then it is HOT and may fire. Not restarted by a wobble. | required | — (a wait, not a timeout) |
| `heat_timeout_s` | **the longest a filament may stay heated in one go**: from the VOLTAGE command until it is back at IDLE. Hard: timed by the backend, never renewed. | Q4 | that filament back to IDLE; what it had not done is skipped; the run goes on (Q1) |
| `trigger_timeout_ms` | the longest gap between two consecutive edges of the SAME filament, timed by the RP2350 from the filament's first edge | sim: 2 × `pulse_interval_ms` + 100; ext: Q7 | Q2 |
| `max_on_ms` | firmware cap on one pulse; `width_us` above it and the ARM is refused, nothing fires | Q3 | — (checked before anything fires) |
| `link_lost_timeout_s` | how long a link (§4) may be down before the run stops | 5 s; nobody needs to change it | the run stops |

Pulse settings (not timing knobs): `num_pulses`, `width_us`, `pulse_interval_ms`
(sim: the edge spacing the library sends; ext: not used).

## 2. Removed, and what takes their place

| removed | why it is no longer needed |
|---|---|
| `heat_timeout_s` as "wait for the first arrival" | inside the heating window: not HOT by the end of the window = that heating failed |
| `shot_wait_timeout_s` | inside the heating window: pulses not all in by its end = the rest skipped |
| `on_armed_timeout_s` | `on_armed` still running when its filament's window ends: the user's code is stuck; the next filament cannot be called → STOP |
| `no_pulse_timeout_s` | every filament ends within its window, so the run cannot stall. Fixed rule instead: 2 filaments in a row with no edge at all → STOP (the trigger source is dead) |
| `total_ms` (parameter) | computed: number of filaments × `heat_timeout_s` + 30 s, written to the firmware as its last backstop |
| backend timed-VOLTAGE length | = `heat_timeout_s`, no longer "predicted wait + margin, renewed" |

## 3. The workflow

Each step happens once, in this order. A filament's heating window opens at
step 6 and closes at step 13 at the latest.

**Before anything is touched (refused with a reason, nothing changed):**

1. Every filament has its own voltage; none is dead; HV (emission and focus)
   is on; HV guard not tripped; vacuum within its limit.
2. `width_us` ≤ `max_on_ms`; widths fit the firmware's u16 µs (≤ 65 535).
3. sim: `trigger_timeout_ms` > `pulse_interval_ms`.
4. The window can hold one filament's work: `heat_timeout_s` ≥ typical heat-up
   + `settle_ms` + (`num_pulses` − 1) × spacing + margin. Otherwise refused as
   "heat_timeout_s too short for settle_ms + pulses" (Q5).

**Setup (all at IDLE):**

5. Download ONE gated schedule (every filament of the list, in order) to every
   controller that fires or frames a pulse; read it back; set `settle_ms` on
   the firmware; arm the STM32 measurement; ARM.

**Per filament, strictly in list order:**

6. **Raise** it to VOLTAGE, early enough to be HOT by its turn
   (`preheat_ahead`). Its window starts; the backend's hard timer is set to
   `heat_timeout_s`.
7. **HOT:** the RP2350 reports first arrival + `settle_ms`, not interrupted
   (output fault, lost board, a drop of more than `max_voltage_drop_mv`).
8. **Its turn** (the previous filament's pulses are all counted): release its
   row on every armed controller.
9. **`on_armed(f)`** is called. trigger="ext": your code starts your edges.
10. **Edges:** sim — the library sends `num_pulses` edges `pulse_interval_ms`
    apart; ext — yours. Release → first edge is not timed by the firmware.
11. **Pulses:** the firmware fires one `width_us` pulse per edge, only for the
    released row, only while that filament is HOT. Any other edge is dropped
    and counted. From the first edge, each gap must be < `trigger_timeout_ms`.
12. **Measurement:** the host reads the STM32 events while pulses arrive
    (the ESP32 keeps only the last 128).
13. **Done:** all its pulses counted → it goes back to IDLE as soon as the
    next filament is released. Its window closes.

**End (whatever ended it):**

14. Disarm; every filament to IDLE (a faulted one to SLEEP); `settle_ms` back
    to 0; read the pulse logs; match measurements to filaments.

```mermaid
sequenceDiagram
    participant H as host (scan_view, in the backend)
    participant B as backend timer
    participant R as RP2350
    participant U as your on_armed
    H->>R: 6. VOLTAGE f
    H->>B: window for f = heat_timeout_s
    R-->>H: 7. f HOT (arrival + settle_ms)
    H->>R: 8. release row f
    H->>U: 9. on_armed(f)
    U-->>R: 10. edges (or the library's, sim)
    R->>R: 11. pulse per edge; gap < trigger_timeout_ms
    R-->>H: 13. all pulses counted
    H->>R: f to IDLE (once the next is released)
    B-->>R: window ends first: f to IDLE, rest skipped
```

## 4. "Link": which one

A run depends on six links. Only some are worth a timeout.

| | link | carries during a run | lost: what happens | detected by |
|---|---|---|---|---|
| L1 | your script ↔ backend (HTTP, remote session) | the `on_armed` call-back | the run lives in the backend; it cannot call `on_armed` | session pings (3 s), reaper 15 s |
| L2 | backend ↔ ESP32, per controller (TCP over WiFi) | release, VOLTAGE / IDLE commands, status | **no IDLE can be sent: a heated filament stays heated** | `connected` (socket open) — what `link_lost_timeout_s` watches today |
| L3 | ESP32 ↔ RP2350, per controller (UART 460 800) | the same commands, one hop further | same as L2, but L2 still looks "connected" | RP2350 heartbeat age (`rp_age_ms`) — **not watched today** |
| L4 | ESP32 ↔ STM32 on the master (UART 921 600) | per-pulse measurements | measurements lost; firing unaffected | STM32 LINK_LOST, event ids jump |
| L5 | RP2350 ↔ boards (I²C, per channel) | arrival, HOT, heating | that filament's heating interrupted | the HOT/interrupted flags; port lost |
| L6 | SyncOut → SyncIn chain, envelope to the STM32 (wires) | the edges themselves | edges missing | `trigger_timeout_ms`; envelope count ≠ pulses |

Proposal:
- `link_lost_timeout_s` watches **L2 and L3**: these carry the commands that
  bring a filament back to IDLE. Today only L2 is watched.
- L4 loses measurements only: recorded, not a stop. L5 is per filament (the
  filament is skipped, §3 step 7). L6 is `trigger_timeout_ms`. L1: Q6.
- **Firmware gap:** the RP2350 has no fallback of its own. If L2 or L3 stays
  down, nothing drops a heated filament: the backend's timer fires but its
  IDLE cannot arrive (Q8).

## 5. Names across the API

| meaning | scan_view | fire_single_pulse | low level (`voltage_one` / `active_one`) |
|---|---|---|---|
| longest one heating may last | `heat_timeout_s` (new meaning) | `heat_timeout_s` (changes from "wait for arrival") | `active_s` (unchanged) |
| wait after the first arrival | `settle_ms` (from the FIRST arrival) | `settle_ms` (held continuously) — Q9 | — |
| gap between edges | `trigger_timeout_ms` | `trigger_timeout_ms` | — |
| firmware pulse cap | `max_on_ms` | `max_on_ms` | — |

combined_backend passes today, all of which have to follow:
- to `fire_single_pulse`: `max_on_ms`, `total_ms`, `shot_wait_timeout_s`,
  `heat_timeout_s`, `settle_ms`;
- to `scan_view`: `trigger_timeout_ms`, `max_on_ms`, `total_ms`,
  `shot_wait_timeout_s`, `heat_timeout_s`, `link_lost_timeout_s`,
  `preheat_margin_s`.

It also sets the backend's dead-man to `active_timeout_s=4.0` (a heated
filament nobody renews for 4 s falls back), which a long window must not trip.

## 6. Open questions

- **Q1** The window runs out while a filament is HOT but still waiting for its
  turn (the one before it took long). Back to IDLE and **re-heat it at its
  turn** (no data lost; a new window) — or skip it?
- **Q2** `trigger_timeout_ms` runs out: today the firmware faults the WHOLE run
  (InterPulseTimeout → STOP). Keep that — or skip only the rest of that
  filament (needs a firmware change: the timeout ends the row, not the run)?
- **Q3** `max_on_ms` default: the firmware's own 40 ms, or the user always
  sets it?
- **Q4** `heat_timeout_s` default (10 s fits today's 4 V test: ~3 s heat-up +
  1 s settle + 20 pulses × 26 ms, with margin)?
- **Q5** Step 4: refuse a window that cannot hold one filament's work, or only
  warn?
- **Q6** L1 lost (your script gone) with trigger="ext": stop the run at once,
  or let the current filament finish?
- **Q7** ext default for `trigger_timeout_ms` (today 3000 ms) — or required
  with ext, since only you know your edge spacing?
- **Q8** RP2350 firmware: a timed VOLTAGE of its own (the board drops to IDLE
  by itself after `heat_timeout_s`), so a heated filament is safe even with L2
  or L3 down. Firmware change, test board first.
- **Q9** `settle_ms` in fire_single_pulse is "held continuously"; in scan_view
  "from the first arrival". Make them the same (which one)?
