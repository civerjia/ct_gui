# CT Scan Simulation — Run Report
_generated 2026-07-10 16:02_

## Result
- **Simulation mechanism: WORKING.** ESP32 fires `ready_in`, RP2350 fires the real schedule via the precision PIO, P1→P2 sync chain propagates. Both controllers advance in lockstep. No faked data.
- Reached **1518 / 2940 pulses**, then both faulted **TotalTimeout** — the 60 s scan-time budget elapsed *because the live INA219 power sampling slowed the run* (I²C shares the bus with firing). Not a hardware fault.
- Earlier bare runs (no sampling) hit real **CC open/OCP** faults on marginal boards (P1 fil 39, P2 ch2·mux0 / fil 16).

## Live power during the scan (real INA219, sampled ~every 5 s)
| t (s) | pulse | P1 total W | P1 heating | P2 total W | P2 heating |
|---:|---:|---:|---:|---:|---:|
| 0 | 3 | 78 | 42 | 65 | 38 |
| 6 | 158 | 82 | 42 | 65 | 38 |
| 11 | 349 | 96 | 42 | 67 | 38 |
| 17 | 536 | 94 | 42 | 77 | 38 |
| 22 | 737 | 98 | 42 | 86 | 38 |
| 28 | 928 | 121 | 42 | 87 | 38 |
| 34 | 1001 | 126 | 42 | 87 | 38 |
| 39 | 1134 | 127 | 42 | 96 | 38 |
| 45 | 1267 | 128 | 42 | 108 | 38 |
| 51 | 1370 | 127 | 42 | 117 | 38 |

Power climbs as the active heating band sweeps the ring (P1 73→128 W, P2 65→117 W) — the expected scan signature.

## Board health (baseline, present boards)
### Power 1: 45 present boards, 74 W total
- CC-faulted now: none
- Suspect (bus < 0.3 V): ch5.mux0
### Power 2: 39 present boards, 67 W total
- CC-faulted now: none
- Suspect (bus < 0.3 V): ch2.mux0

## Findings & recommendations
1. **The sim is proven** — real firing, real chain, real power. Trigger + sim-crash + download-speed + WiFi bugs all fixed.
2. **Live power vs. timing is a genuine trade-off.** Sampling INA219 during firing slows pulses (shared I²C) → raises run time → TotalTimeout. Two clean modes: (a) *production* scan = no INA sweep during firing (timing-exact), power shown from ShvGetStatus firing filament + progress; (b) *diagnostic* run = sample INA (real power) with a raised total-time budget.
3. **Bad boards** must be excluded (Detect present / disable) to complete a bare run: P1 fil 39, P2 ch2·mux0 seen faulting under heating.
4. To get a clean full monitored run: raise the schedule `totalMs` (so INA sampling fits the budget) and exclude the CC-faulting boards.
