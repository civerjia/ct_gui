# HV guard

The HV guard takes the HV down when current flows that should not: a grid switch that will not turn off, or a Spellman arc or over-current. Current flowing *during* a pulse is not judged.

When the guard trips:

- every grid switch is forced open;
- emission and focus are switched off at the STM32;
- heating drops (STOP or IDLE, see the table below);
- the guard latches, so HV ON is refused until a person clears it with `POST /api/hv-guard {"clear": true}` or the GUI button.

## 1. The emission rail: what the loads draw

The baseline is not learned. It is computed from the loads on the emission rail. The diode and LED drops below were measured with a meter on its diode range (2026-10-29).

| load | where | parts | drop |
|---|---|---|---|
| per channel board (12 on the rig) | rail → board | 4 × 51 kΩ + 1N4007 + green LED (NCD0805G1) | 0.6 + 2.3 = 2.9 V |
| per sub-board whose MOSFET conducts | rail → sub-board | 100 kΩ + 1N4007 + red LED (NCD0805R1) | 0.6 + 1.75 = 2.35 V |

```
expected  = offset + 12 × (|V| − 2.9) / 204 kΩ
one switch on        =   (|V| − 2.35) / 100 kΩ        (1.95 mA at 197.8 V)
```

The offset is what the ADS reads with the rail off (about 0.10 mA). It is learned while the rail is off.

Measured 2026-10-29:

| rail | measured | model |
|---|---|---|
| 197.8 V | 11.60 mA | 11.56 mA |
| 17.2 V | 0.88 mA | 0.94 mA |
| 4.8 V | 0.23 mA | 0.21 mA |

Focus on or off makes no difference.

### Why the baseline is not learned

- A learned baseline cannot see a switch that was already on when the rail came up: it learns the fault as normal. This happened at 08:31 and 08:39 on 2026-10-27.
- The STM32's learned baseline wandered by 115 counts (about 2 switches) in one morning.

### When it acts

The check runs only between pulses. That means no sim train or its tail, no switch the host closed, and no external-trigger run armed. With the ADS read at about 1 Hz:

| reading for 2 s | action |
|---|---|
| over the model by more than half a switch | Force every switch open. If the current comes back down, it is cleared (a 595 flipped). If it is still over, a MOSFET nothing can open: HV off, filaments hot within 15 s to STOP, latched. |
| under the model by more than half a board | Warning once: a board load is missing, or `CT_HV_LOAD_BOARDS` is wrong. |

### The STM32's fast guard

The STM32 guard (firmware `hv_guard.c`) still trips in milliseconds on a rise over its own learned baseline: 36 counts for 5 ms.

The backend then:

1. forces every switch open;
2. drops hot filaments to IDLE;
3. puts the HV back on;
4. judges by the ADS against the same model.

Back within half a switch means recovered. Still over means latched.

More than 3 recoveries in 10 min latches. **A trip that comes within about 4 s after a Spellman arc is the arc's.** It recovers and does not count toward that limit. On 2026-10-29 between 11:03 and 11:09, five arcs tripped the STM32 and the third one latched the rig.

## 2. The Spellman (read-only)

control2's `spellman_backend` broadcasts kV, mA and GetStatus about 4 times a second. Nothing is ever sent to the supply.

| event | what it looks like | action |
|---|---|---|
| ordinary arc | one reading (sometimes two) with the kV down by more than 10 % and 3 kV and/or an mA spike, then back to 95 % | Recorded in `logs/arcs.jsonl` and in the backend log. The supply recovers by itself, so nothing is switched. |
| big arc | the kV falls in one step, and a reading taken at least 5 s later is still under 50 % (a stale reading never counts: `spellman_backend` pauses its monitor while its conditioning handles an arc, which caused a false trip at 11:36 on 2026-10-29; with no reading for 30 s the event is recorded as unknown) | Trip, latched: switches open, HV off, hot filaments to IDLE (IDLE keeps the vacuum). Cleared by hand. |
| over-current | between pulses, the mA is over its baseline by `spellman_trip_ma` for more than 1 s | Trip, latched: switches open, HV off, filaments hot within 15 s to STOP. |

A ramp-down (about 1 kV per reading) is never an arc. The GetStatus arc bit counts on its rising edge only.

The mA baseline is the median of quiet readings over the last 60 s, with arcs and pulses left out. It is capped at 0.2 mA, so a fault present from the start is not learned as normal.

`spellman_trip_ma` defaults to 0.3 mA (`CT_SPELLMAN_TRIP_MA`). It can be set with `POST /api/hv-guard {"spellman_trip_ma": x}`, and it is kept across restarts.

Replay of 2026-10-29 11:01–11:12 (60–65 kV): 22 arcs, all ordinary, no over-current.

## 3. What to read

`GET /api/hv-guard` returns:

- `load_model`: measured, expected, excess, `switches_on`;
- `spellman`: baseline, threshold, arcs in the last 10 min, the last arc;
- `tripped`: the full record and the message.

The GUI's HV guard tile shows the arc count. Its tooltip shows the model and the Spellman baseline.

## 4. Trend data on disk

Every plotted reading goes to disk: Spellman kV and mA, vacuum, emission V and I.

| file | content | kept |
|---|---|---|
| `logs/series/raw_<date>T<hour>.csv` | `unix_t,name,value`, full rate, about 13 rows/s | `CT_SERIES_RAW_HOURS`, default 6 h |
| `logs/series/summary_<date>.csv` | `minute,name,n,min,max,mean` | for good |

When a raw hour falls outside the kept window, it is compacted into the summary and deleted. A restart reads the last hour back into the plot.
