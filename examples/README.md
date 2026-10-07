# Examples

## `external_trigger.py` — fire on an external trigger, and know when to send it

```bash
python3 external_trigger.py --host 192.168.8.165 -f 8                  # one pulse
python3 external_trigger.py --host 192.168.8.165 -f 8 --pulses 3        # three
```

Heats filament F up the ladder (STOP → SLEEP → STANDBY → IDLE → ACTIVE), turns
HV on, then arms with `trigger="ext"`. It prints **READY** from the `on_armed`
callback at the moment everything is armed — send the external trigger then;
an edge sent earlier is lost. Always ends with HV off and filament F at STOP.
The callback and its timing rules are documented in `fire_single_pulse`'s
docstring ("EXTERNAL TRIGGER").

## `fire_steps.py` — fire_single_pulse's steps called one by one

```bash
python3 fire_steps.py --host 192.168.8.218 -f 16 55 70
python3 fire_steps.py --host 192.168.8.218 -f 16 --trigger ext
```

`fire_single_pulse()` is `shot_prepare` → `shot_measure_arm` (optional) →
`shot_arm` → `shot_trigger` → `shot_wait` → `shot_records` → `shot_measured`
(optional). Each takes and returns the SHOT dict; a failed step returns it with
`ok=False` and every later step passes it through, and `shot_abort(shot)`
disarms whatever was armed (use it in a `finally`). This script prepares and
arms at IDLE, raises ACTIVE right before the trigger and goes back to IDLE
before reading the results, and prints how long each filament was at ACTIVE.
For the one-call version of the same order:
`fire_single_pulse(..., active_ma=2700, idle_ma=1300)`. Both are shown, with
liuxing_api.py's settings, in `../liuxing_api_example.py` (`MODE`).

## `step_fire_schedule.py` — a short schedule, one filament per trigger, heated by the firmware

```bash
python3 step_fire_schedule.py --host 192.168.8.218 -f 93 12 40                 # ext trigger
python3 step_fire_schedule.py --host 192.168.8.218 -f 93 12 40 --trigger sim --gap-s 10
```

Downloads ONE schedule (one entry per filament, in the order given) plus a
heating table: each filament goes ACTIVE right after the previous one's pulse
and back to IDLE right after its own, so the gap between two edges is its
heating time. The first filament is brought to ACTIVE before arming (the table
only runs after the first edge); the last is idled afterwards (its IDLE row is
never reached). Prints, per pulse, the heating current at the moment it fired
and the measured emission. While the run is RUNNING the backend refuses
`idle_one`/`active_one` — the heating table is the only way to change a current
mid-run. Always ends disarmed, detector off, HV off if it turned it on, and
every filament it touched at STOP.

## `walkthrough.py` — the whole API, in runnable sections

```bash
python3 walkthrough.py --list                    # what the sections are
python3 walkthrough.py                           # READ-ONLY sections (safe)
python3 walkthrough.py --only 8                  # just the schedule builder
python3 walkthrough.py --energise -f 27          # + heating and measurement
python3 walkthrough.py --energise --fire -f 27   # + firing pulses
```

Needs `backend.py` running with a controller connected.

| # | section | does |
|---|---|---|
| 1 | Connect, status, error convention | read-only |
| 2 | Three index spaces (SITE / FID / USER_INDEX) | read-only |
| 3 | Dead mask | read-only |
| 4 | Slew rates, OCP, fault policy, trigger delay | read-only |
| 5 | Telemetry: cached vs live, board faults, thermal history | read-only |
| 6 | Heating ladder + batch verify | **energises** |
| 7 | Resistance screen + impedance sweep | **energises** |
| 8 | Schedule: geometry → emission → heating → validate → gantt | read-only (pure computation) |
| 9 | Fire a pulse and measure it | **fires** |
| 10 | Recovery after a killed script | read-only |

Nothing energises without `--energise`, nothing fires without `--fire`. A
section whose permission is missing is skipped **loudly**, never silently
downgraded. `-f/--filament` has no default on purpose — which board carries a
load is a property of your bench, and picking one for you is how a script ends
up heating something unexpected. Section 5 finds one.

### Two things worth copying out of it

**The signal handler.** `energised()` guarantees a STOP on exception and on
Ctrl-C, but a plain `SIGTERM` terminates the process without running any
`finally`. That is not hypothetical: a harness timeout killed a script during
this work and left a filament at 880 mA. Put this in any bench script that
might be killed:

```python
for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(s, lambda n, f: (_ for _ in ()).throw(KeyboardInterrupt()))
```

`SIGKILL` cannot be caught by anything in the process; only a backend-side
watchdog could cover that, and there isn't one yet.

**Batch verification.** Use `wait_for_currents({f: mA, ...})`, not
`wait_for_current()` per filament. The cached read is a bulk command — it
returns every board whether you ask for one or ninety-six. Measured on 14
filaments: **6.5 s / 40 backend calls batched, against 85.9 s / 409
sequential**, identical verdicts.
