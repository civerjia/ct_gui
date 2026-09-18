# Examples

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
