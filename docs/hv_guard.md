# HV guard

The HV guard takes the HV down when current flows that should not: a grid switch that will not turn off, or a Spellman arc or over-current.

Only two things need a person, and they stay latched until one clears them:

- a MOSFET that stays on;
- Spellman current high for a long time.

Arcs are normal transients, including big ones and a System Fault. The HV comes down, then goes back on by itself as soon as the arc is over (Spellman stable 1 s). Stable means no arc, no alarm bit and no HV-off read in that time, HV Ray On, and the kV steady within 2 %.

The vacuum interlock is unchanged: it stays latched. Current flowing *during* a pulse is not judged.

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

There is **no latch by count**: every trip is checked by current, and only "still over with every switch forced open" (a MOSFET that stays on) latches. More than 3 flips in 10 min adds a warning about the 595s. This changed on 2026-10-29: at 12:51:12 a 4th trip in 10 min latched unchecked. A trip that comes within about 4 s after a Spellman arc is the arc's, and is not counted as a flip. An STM32 trip while the emission rail is already off (for example a latch left over from before a restart) is not checked and does not latch: nothing can conduct, so the STM32 latch is cleared and the HV stays off. Whoever turns it on next is watched by the load model.

## 2. The Spellman (read-only)

control2's `spellman_backend` broadcasts kV, mA and GetStatus about 4 times a second. Nothing is ever sent to the supply.

| event | what it looks like | action |
|---|---|---|
| ordinary arc | the GetStatus Arc bit rises (fallback: a kV drop of more than 10 % and 3 kV between two readings) and the kV is back to 95 % within 2 s. Every ordinary arc on 2026-10-29 was back in 0.2–0.65 s. A single mA spike alone is not an arc, since a normal pulse makes one too. | Recorded in `logs/arcs.jsonl` and in the backend log. Nothing is switched. |
| big arc (auto-recovers) | the kV is **not back within 2 s**: low, climbing, or no readings at all (`spellman_backend` pauses its monitor while its conditioning handles an arc, about 7.8 s), or System Fault. HV Ray On stays 1 through these, so GetStatus cannot tell one. 12:24:24: 61.5 → 0.3 kV, 7.8 s without readings, ramp back by 12:24:41; a shot fired at 38 kV during the ramp. | Trip: switches open, HV off, hot filaments to IDLE. Back on by itself as soon as the arc is over (Spellman stable 1 s). Heating stays at IDLE. If the STM32 tripped on the arc, its check waits until the arc is decided. |
| over-current | between pulses, the mA is over mean + 5σ of its quiet readings for more than 1 s | Trip, latched: switches open, HV off, filaments hot within 15 s to STOP. |

### GetStatus

Its 17 bits, in the order of control2's `spellman.html`:

| bit | meaning | bit | meaning |
|---|---|---|---|
| 0 | Power On | 9 | Regulation Error |
| **1** | **HV Ray On** | 10 | Current Control Mode |
| **2** | **Arc** | 11 | Over Temperature |
| 3 | Interlock Closed | 12 | Power Control Mode |
| 4 | Over Current | 13 | AC Fault |
| 5 | Over Power | 14 | Local/Remote Mode |
| 6 | Over Voltage | 15 | LVPS Fault |
| 7 | Voltage Control Mode | 16 | HV Inhibit |
| **8** | **System Fault** | | |

- **System Fault** rising: trip at once, whatever the kV reads. Switches are opened, HV goes off, hot filaments go to IDLE. Once the supply is stable again for 1 s, the rails that were on come back by themselves. Heating stays at IDLE. If the STM32 tripped on the same arc, its own current check restores the rails instead.
- **Arc** rising: an arc. Only the edge counts; the bit can stay set across several reads.
- **HV Ray On**: the kV not back within 2 s, with this bit 0 (read within 3 s) and neither Arc nor System Fault, means somebody switched the supply off. That is recorded as `hv_off`, with no trip. During an arc, even a big one, the bit stays 1.
- Every change of an alarm bit or of HV Ray On is written to `logs/arcs.jsonl` as `kind: status`. The alarm bits are Arc, Over Current/Power/Voltage, System Fault, Regulation Error, Over Temperature, AC/LVPS Fault and HV Inhibit.

A ramp-down (about 1 kV per reading) is never an arc. The GetStatus arc bit counts on its rising edge only.

The mA baseline is the median of quiet readings over the last 60 s, with arcs and pulses left out. It is capped at 0.2 mA, so a fault present from the start is not learned as normal.

The over-current threshold is statistical: **mean + k·σ** of the quiet readings over the last 60 s. Quiet means no arc and no pulse.

- k defaults to 5. Set it with `CT_SPELLMAN_TRIP_SIGMA` or `POST /api/hv-guard {"spellman_trip_sigma": k}` (range 2–20); it is kept across restarts.
- Measured 2026-10-29 at 60–65 kV: mean 0.034–0.037 mA, σ 0.036–0.043 mA in every 60 s window, so the threshold is about 0.24 mA.
- The readings come in 0.0098 mA steps and are skewed (median 0.02 mA, p99 0.13 mA). 3σ is about the 1 % point, so k = 3 would be too tight.
- σ has a floor of 0.02 mA, so all-zero readings at low kV cannot make the threshold zero, and a cap of 0.1 mA. The mean is capped at 0.2 mA.
- Over readings are never learned. A current already high when the watch starts trips after 1 s.

Replayed on 1 h 20 min of real readings: no false over-current.

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

## 5. Shots while the HV is off

While the guard has the HV down, it **refuses to heat any filament to ACTIVE or VOLTAGE**.

- A shot then would measure 0 mA, and a tuner reads that as "heat more". This happened on 2026-10-29 between 13:17 and 14:51: `liuxing_api.py` raised ten filaments, and #86 reached 9563 mV, past the 9.5 V cap. From then on every #86 shot was refused.
- The refusal names the trip, and says whether the HV comes back by itself or needs a person.

`fire_single_pulse(measure=True)` handles the HV-off case on the client side:

| situation | what happens |
|---|---|
| guard tripped: a big arc (HV comes back by itself) or a latch that needs a person | refuses at once (`hv_off: True`), not waited out: the ~15 s outlasts the heating (2026-10-30). `scan_view` stops on it the same way |
| small arc (kV back within 2 s) | never trips the guard: ignored, logged in `arcs.jsonl` |
| guard acted during the shot (counted by `trips` in `GET /api/hv-guard`) | returns `ok=False`, `hv_off: True` |

The emission and focus rails themselves are the operator's: neither `fire_single_pulse` nor `scan_view` checks or switches them (2026-10-30). With them off a measured pulse reads ~0 mA.
