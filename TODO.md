# TODO — ct_gui / ct_simple_control

Deferred work: known, understood, and not on the main line yet. Each entry says
what is wrong, what the fix is, and what is still undecided.

## VOLTAGE mode: verify does not verify (2026-09-23)

**What is wrong.** `voltage_one(verify=True)` waits for `cc_mode == 0` and
nothing else. The firmware sets `cc_mode` to 0 (`kTpsCcModeVoltage`) for
every state that is not regulating current — STOP, SLEEP and STANDBY too
(`channel_controller.cpp`, `setPowerState`). So the verify passes for a
filament that never left STOP, and it never looks at the voltage.
`voltage_all` has no verify at all; it is the only `*_all` without one.

**The fix, `voltage_one` (no firmware change).** Confirmed only when all
three hold:

1. EN on and the output enable read back on — `GET /api/tps-status`, the
   same bulk read `standby_all(verify=True)` uses;
2. `cc_mode == 0` — not regulating current;
3. the live INA219 bus voltage within a tolerance of the target — one live
   read of one board is fine. The tolerance has to allow for the sense
   resistor and lead drop between the TPS output and the INA219, which grows
   with current.

**The fix, `voltage_all` — undecided.** 1 and 2 are bulk reads. 3 has no bulk
form: the live voltage is a per-board I2C read, and looping it over a batch
is the per-board polling that starves the shared link.

- (a) confirm 1 and 2 only, and say in the result that the voltage was not
  checked;
- (b) firmware: have the CC loop cache the INA219 bus voltage beside the
  current, and return it in the bulk cached read (0x3A). Then `voltage_all`
  checks the value in bulk, and every cached read — `idle_all`'s verify
  included — can carry voltage and resistance. Preferred, but a firmware
  change on both RP2350s.

## GUI debug page: warn before powering a dead filament (2026-09-23)

**Decided:** the GUI's Power debug page may operate a dead filament — it is a
debug tool, and a repaired board has to be exercised before it is unmarked —
but it must **warn** first.

**What is wrong now.** The debug page sends `CH_SET_POWER_STATE` straight
through `/api/cmd` (`static/power.js` — the single-board setter around line
712 and the board-mask batch around 733). That path bypasses
`prep_filaments` and `/api/filament-state`, so none of the dead rules apply:
not the refusal of energising states, and not the SLEEP → STOP substitution
(`7b846a5`). Nothing tells the operator the board is marked dead.

**To do.**

- Before sending any state that powers the board (SLEEP and above) to a
  filament in the dead mask, show a confirmation naming the filament and the
  reason it was marked dead (`GET /api/dead-fids` carries it). For the
  board-mask batch, list every dead filament in the mask.
- STOP needs no warning — turning a dead filament off is always fine.
- Keep `/api/cmd` itself permissive: the debug path is meant to bypass the
  guards. The warning belongs in the GUI, where a person is present to answer
  it; scripts use `prep_filaments`, which enforces.

## RP2350 power plane: open items after the concurrency work (2026-09-23)

State: the CC loop and ramp run concurrently on all 8 channels (RP2350
`1e08f92`, architecture.md §4.5 #11). `tests/test_idle_all_concurrency.py`
passes 3/3 on firmware `8ed468a`: 93/93 filaments at IDLE, 0 I2C timeouts,
0 command failures. Still open:

- **Controller 1 I2C integrity.** C1 (never C2, same firmware and load)
  intermittently returns corrupted reads: TPS MODE read-backs that differ
  (VerifyFailed), INA readings like -4010 mA at 8256 mV on a filament at ~1 A.
  Firmware now contains it (one counted OE retry; implausible reads treated as
  spikes; open latch needs two agreeing reads) but the cause is hardware-side:
  check C1's I2C cables, pull-ups and routing. A 100 kHz build on C1 would tell
  whether margin is the issue. `ccstat` shows spikes / oeRetries / ccLatches /
  pwrFails per board; `i2cstat` shows timeouts split async/sync.
- **Batch commands: done.** 0x23 bulk status (RP2350 `9ab5d49`) and masked
  0x35 STANDBY/IDLE/ACTIVE/VOLTAGE all run concurrently on the eight channels.
  Open: on C1, a batch command to boards whose outputs are already ON has
  failed its first mux read-back (rc 2) on CH2/CH4/CH6 even after 2 retries --
  but not in the last run. `ccstat` now prints `muxMiss: rb=... cached|full-select
  step=...` for such a failure: a read-back of another port's bit means a stale
  mux cache (firmware), no answer / garbage means the bus.
- **C1 corrupts INA reads -- pattern known (2026-09-23).** Every recorded bad
  read (19 in one IDLE run, all C1, none on C2) is ONE bit 0->1 in the top 3
  bits of the FIRST data byte (0x05A5 -> 0x85A5, 0x061A -> 0x461A, bus 0x0628
  -> 0x4628); the low byte is always right. Those are the first bits the INA
  drives after the address ACK: a timing-margin problem at the start of the
  read data phase on C1's wiring, not random noise. Firmware discards reads
  outside the CC loop's bounds and keeps the raw bytes (`i2cstat`: `bad INA
  reads`) -- but a flip in bits 8-12 (a few hundred mA) passes those bounds
  unseen, so filtering is not a fix. Fix the timing (hardware, or the PIO
  sample point). A 100 kHz comparison was proposed and declined.
- **Board monitor (ring + matrix, one reader).** RP2350 idle monitor reads
  every non-CC board at 2 Hz in the power job's idle time (0x3D, no I2C for
  the host); off during a run, when I2C queries are refused with Busy and
  the cache stays readable with true ages. Backend: one thread per controller
  (`MONITOR_*` in backend.py). Known: STOP boards read not-present (their INA
  is on the iso rail).
- **C1 CH2 went dark once, cause unknown.** 2026-09-23, after a rig-wide
  IDLE on firmware `c57aa5e`+pacing: mux and all three expanders NAK'd every
  probe (1.1M NAKs, 0 timeouts, 0 stuck lines) until a reboot; its filaments
  (8-11, 24-27) were unreadable, so their off state could not be confirmed
  until then. Not reproduced in 4 runs since. If it recurs: `i2cstat` (SM +
  pad snapshot, `nakStreak` climbing) BEFORE anything else, then
  `i2creinit 2` -- answers again = the SM was wedged (firmware); still NAKs =
  the board/cable. Firmware never re-inits on its own yet; a stuck channel
  cannot turn its filaments off, so an auto-reinit on a long mux NAK streak
  is the likely fix once the snapshot says what broke.
- **Warm-start idle times near the test bound.** Back-to-back runs (filaments
  still hot) reach 9.6-9.7 s max against the 10 s bound: SLEEP -> IDLE uses the
  cold slew rate. Decide whether warm starts should use the warm rate, or the
  test should cool between runs.
