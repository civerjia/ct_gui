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
