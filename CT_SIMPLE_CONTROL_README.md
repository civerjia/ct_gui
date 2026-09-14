# ct_simple_control — CT Power Controller API

A thin Python HTTP client for the CT power-controller backend. Third-party
programs import this module to drive pre-heat, HV setpoints, readback, and
single-filament HV pulses without touching the GUI.

## Requirements

```
pip install requests
```

Python 3.10+ required (uses `X | Y` type union syntax).

---

## Error handling — read this first

**This client drives real HV and heating hardware. Almost NOTHING in it
raises an exception.** Every method returns a dict with an `"ok"` key (and
an `"error"` key on failure) — check `"ok"` yourself. A dead filament hit in
a loop, a board that's temporarily absent, a transient UART hiccup talking
to the STM32 — none of these raise. They come back as `{"ok": False, ...}`
so your loop can log it and move on to the next filament, instead of your
whole script crashing and leaving HV energized or a filament heating with
no cleanup.

```python
r = ct.active_one(6, current_ma=2900)
if not r["ok"]:
    print(f"filament 6 failed: {r.get('error')}")   # continues, doesn't crash
```

**The one exception**: `acquire_lease()` (and the `with ct.lease():`
context manager) *does* raise `CTLeaseError` if another client already
holds the write lock. Proceeding without it risks two scripts fighting
over the same hardware at once, which genuinely is unsafe — so this one
call fails loudly on purpose rather than silently continuing. It's a
single, deliberate call site — easy to wrap once at the top of your script:

```python
from ct_simple_control import CTClient, CTLeaseError

try:
    ct.acquire_lease(ttl=120, note="my test run")
except CTLeaseError as e:
    print(f"someone else has the bench: {e}")
    raise SystemExit(1)
```

**For belt-and-suspenders safety**, wrap your script body in
`with ct.session():` — it guarantees HV gets disabled and every filament
gets stopped when the block exits, even if your *own* code raises an
unrelated exception (a bug, a `Ctrl+C`, anything):

```python
with ct.session():
    ct.active_one(5, current_ma=2900)
    ct.enable_emission(True)
    ...  # if this raises, HV still gets shut off safely on the way out
```

See [`session()`](#connection) below for exactly what it tears down.

**Automatic retry.** `backend.py` almost always answers with HTTP 200 even
when the ESP32↔STM32 or ESP32↔RP2350 hop underneath it failed — the real
error (a UART timeout, an ESP32-reported "Bad Gateway" from *its* proxy to
the STM32, a dropped frame) comes back embedded in that 200 response as
`{"ok": False, "error": "..."}`, not as an HTTP-level failure. So every
request here automatically retries up to `ct.max_retries` times (default
`2`, i.e. 3 attempts total) with a short, doubling backoff — but **only**
for failures that look like a one-off communication hiccup. A dead
filament, a bad argument, a held lease, a rejected arm — none of those are
retried, since retrying a deterministic rejection just wastes time instead
of surfacing the real problem. A response that got retried carries a
`"retries"` key with the count:

```python
r = ct.read_ads_all()
if r.get("retries"):
    print(f"took {r['retries']} retries to succeed")
```

Tune or disable it per instance:

```python
ct.max_retries = 0        # disable retry entirely
ct.retry_delay_s = 0.5    # base delay between attempts (doubles each retry)
```

---

## Architecture

```
Your script          GUI (browser)
     │                    │
     └──── HTTP ──────────┘
                  │
            backend.py          ← owns ALL hardware logic
                  │
            ESP32 (WiFi TCP)
                  │
         RP2350b + STM32G431   ← actual hardware
```

The backend process (`backend.py`) runs on the host machine, binds to the
ESP32 over TCP, and exposes an HTTP API on port **8770** (default).
Both the GUI and your script call the same API simultaneously — no hardware
access happens in the client.

---

## Starting the backend

```bash
cd tools/ct_gui
python3 backend.py
```

This prints something like:

```
CT GUI server listening on http://0.0.0.0:8770
  open http://127.0.0.1:8770 · shared API on http://192.168.50.112:8770
```

**There are two completely different IP addresses in this whole setup —
don't mix them up:**

| | What it is | Where it's used |
|---|---|---|
| **ESP32's IP** (e.g. `192.168.50.173`) | The WiFi bridge board's own address on your network | Only in the `/api/connect` call below, so **backend.py** knows which ESP32 to talk to |
| **backend.py's IP** (e.g. `192.168.50.112`, the "shared API" line above) | The machine running `backend.py` | What **your script's `CTClient(host=...)`** connects to |

Of the three addresses backend.py prints:
- `0.0.0.0` — just the *bind* address (means "all network interfaces").
  **Never** use this as a value to connect to.
- `127.0.0.1` — use this (or `"localhost"`, `CTClient`'s default) when
  your script runs on the **same machine** as `backend.py`.
- The "shared API" IP (`192.168.50.112` above) — use this when your
  script runs on a **different machine** on the network. This is
  `backend.py`'s own address, copied from its startup banner each time.

Once the backend is running, connect it to the ESP32 (one-time per
backend restart) — either through the GUI at `http://localhost:8770`
(connect the controller to the ESP32's IP there), or via the API:

```bash
curl -X POST http://localhost:8770/api/connect \
     -H "Content-Type: application/json" \
     -d '{"controller": 1, "host": "192.168.50.173"}'
```

`"host"` here is the **ESP32's** IP — different from the `CTClient(host=...)`
your script will use below, which is `backend.py`'s IP.

---

## Quick start

```python
from ct_simple_control import CTClient

ct = CTClient("localhost", port=8770, client_id="my-script")

# Connect controller 1 to the ESP32 if the GUI hasn't already
if not ct.status()["controllers"]["1"]["connected"]:
    r = ct.connect(1, "192.168.50.173")   # the ESP32's own IP
    if not r["ok"]:
        print(f"connect failed: {r['error']}")

# ── Dead filaments ───────────────────────────────────────────────────────
# A "dead" filament is one you've marked as physically damaged, missing, or
# otherwise not to be touched — e.g. a burnt-out emitter, an unseated board,
# or a known-bad HV switch. Once marked, EVERY batch call below (stop_all,
# sleep_all, standby_all, idle_all, active_all, hv_grid_set_all, ...)
# silently skips it — you never have to remember to exclude it yourself.
# Any call that targets ONE specific filament (active_one, idle_one,
# fire_single_pulse, hv_grid_set, ...) returns {"ok": False, "dead": True}
# immediately instead — it does NOT raise (see "Error handling" above) — so
# a script iterating every filament in a loop just skips it and moves on.
ct.set_dead([6, 26, 73])   # or: ct.set_dead(set(range(96)) - set(ct.present_filaments()))

# with ct.session(): guarantees HV/heating gets torn down on exit even if
# something below raises unexpectedly — see "Error handling" above.
with ct.session():
    # ── Startup safety ladder (batch control) ───────────────────────────
    # A common startup sequence: walk every populated filament DOWN through
    # the safe states first, in case anything was left mid-ladder from a
    # previous run — then bring the whole bench up to STANDBY as a
    # known-clean baseline.
    ct.stop_all()      # HV off, heating off — the safe resting state
    ct.sleep_all()     # low-power resting state, one step above STOP
    ct.standby_all()   # powered, not heating — ready state before pre-heating

    # ── Batch control: pre-heat several filaments at once ───────────────
    ct.idle_all(filaments=[0, 1], currents={0: 1500, 1: 1500})   # filaments= scopes it to just these two

    # ── Single-filament control: promote exactly ONE to fire ────────────
    # active_one (like idle_one, stop_one, sleep_one, standby_one) uses the
    # RP2350's dedicated single-board wire format — not a batch call with
    # one item in it. Use these whenever you're targeting exactly one
    # filament. Never raises — check "ok":
    r = ct.active_one(filament=0, current_ma=2900)
    if not r["ok"]:
        print(f"active_one failed: {r.get('error')}")

    # Set HV
    ct.set_emission_v(30)     # −30 V emission (magnitude, backend applies LUT)
    ct.set_focus_v(200)       # −200 V focus
    ct.set_emission_i(10)     # 10 mA emission current reference

    # Enable HV output
    ct.enable_emission(True)

    # Read back — these return None on failure (e.g. STM32 momentarily
    # unreachable over UART — a real, recoverable condition, not a crash):
    v = ct.read_emission_v()
    if v is not None:
        print(f"emission: {v} V")

    # Fire one HV pulse on filament 0 — two ways to trigger it:

    # (a) trigger="sim": the ESP32 generates the SyncIn edge itself. No
    #     external wiring needed — use this for bench testing.
    result = ct.fire_single_pulse(
        filament=0,
        num_pulses=1,
        width_us=1000,       # 1 ms pulse
        controller=1,
        trigger="sim",
    )
    if result["ok"]:
        print(result)  # {"ok": True, "fired": 1, "records": [...], "status": {...}}
    else:
        print(f"fire failed: {result['error']}")

    # (b) trigger="ext": arms the schedule, then just WAITS (polling) for a
    #     real electrical edge on the RP2350's SyncIn pin — from a gantry
    #     encoder, a bench pulse generator, a manual trigger button, or an
    #     upstream controller's chained SyncOut. This call does not
    #     generate anything itself; make sure whatever supplies the real
    #     edge is ready to fire before (or shortly after) calling this,
    #     within timeout_s.
    result = ct.fire_single_pulse(
        filament=0,
        num_pulses=1,
        width_us=1000,
        controller=1,
        trigger="ext",
        timeout_s=30.0,       # give the external source enough time to fire
    )
    print(result)

    # Tear down (also happens automatically at the end of the `with`
    # block above, even on error — this is just the explicit happy path)
    ct.enable_emission(False)
    ct.stop_all()
```

---

## Multi-client / GUI co-existence

The GUI and your script share the same backend and can run simultaneously.

**Reads** (telemetry, HV readbacks, status polls) are always allowed and
never blocked — the GUI's live view keeps updating even while your script runs.

**Writes** are coordinated by a **lease**. Take a lease around any critical
multi-step sequence to prevent the GUI from interfering mid-way:

```python
with ct.lease(ttl=60, note="auto scan"):
    # GUI write buttons are blocked while this block runs
    ct.shv_arm(1)
    result = ct.fire_single_pulse(filament=0, ...)
# lease released — GUI resumes full control
```

Without a lease, writes from the script and GUI are serialized at the UART
level (safe, no corruption) but have no semantic ordering guarantee. For a
simple pre-heat followed by a GUI-driven scan, no lease is needed:

```python
ct.idle_all()          # pre-heat via script
ct.active_one(5, 2900)
# hand off — operator clicks Arm in the GUI
```

---

## API reference

### Constructor

**`CTClient(host="localhost", port=8770, client_id="ct_simple_control", timeout=5.0)`**
— Create a client bound to one backend instance. Creating a `CTClient` does
NOT connect to hardware by itself — the backend must separately connect a
controller to the ESP32 bridge (via the GUI, or `ct.connect(...)` — see
[Connection](#connection) right below).

```python
from ct_simple_control import CTClient

# host is backend.py's own "shared API" IP (its startup banner) — NOT the
# ESP32's IP. Only needed when your script runs on a different machine;
# same-machine scripts can just use CTClient() (defaults to localhost:8770).
ct = CTClient(host="192.168.50.112", port=8770,
              client_id="soak-test-script", timeout=5.0)
```

- `host` / `port`: where the **backend** (not the ESP32) is listening —
  see [Starting the backend](#starting-the-backend) for how to read this
  off its startup banner.
- `client_id`: identifies this script in the backend's client list and
  lease log — pick something descriptive so operators can tell your script
  apart from the GUI or other scripts (see `GET /api/clients`).
- `timeout`: default per-request timeout in seconds; some calls
  (`fire_single_pulse`, `idle_all`, `hv_grid_set_all`) override this with
  a longer built-in timeout since they take longer on real hardware.

### Connection

Binding a controller to the ESP32 bridge is separate from creating a
`CTClient` — do this once per backend process lifetime (it survives until
the backend restarts, or `disconnect()` is called). Skip this section
entirely if the GUI (or another script) already connected the controller
you need — `status()` tells you either way.

**`connect(controller, host)`** — Bind `controller` (1 or 2) to the ESP32
bridge at `host`. `host` here is the **ESP32's own IP** (e.g.
`192.168.50.173`) — different from `CTClient`'s own `host` argument, which
is `backend.py`'s address (see [Constructor](#constructor) above; don't mix
these up). Does not raise — check `"ok"`; `False` if the ESP32 is
unreachable or its single TCP client slot is already held by someone else.

```python
r = ct.connect(1, "192.168.50.173")
if not r["ok"]:
    print(f"connect failed: {r['error']}")
```

**`disconnect(controller)`** — Release the bridge connection, freeing the
ESP32's single-client TCP slot so another host (or the GUI) can connect.

```python
ct.disconnect(1)
```

**`status()`** — Check what's currently connected before doing anything
else. Good first call in any script.

```python
s = ct.status()
print(s)
# {"controllers": {"1": {"connected": True, "host": "192.168.50.173", ...},
#                   "2": {"connected": False, "host": None, ...}},
#  "master": 1, "lock": {"held": False, ...}, "you": "ct_simple_control"}
if not s["controllers"]["1"]["connected"]:
    ct.connect(1, "192.168.50.173")
```

**`present_filaments()`** — Live-scan every connected controller for
physically-present boards and return the global filament indices found.
**Slow** (a few seconds per controller — it sleeps every board to power the
presence-sense rail, then re-scans I2C) and leaves touched boards at SLEEP
afterward, so run it once at setup, not in a polling loop. This pairs
naturally with the [dead mask](#dead-mask) — auto-populate it from what's
actually plugged in instead of hand-maintaining a list:

```python
present = set(ct.present_filaments())
ct.set_dead(set(range(96)) - present)   # everything NOT physically present is dead
print(f"{len(present)}/96 filaments present; dead mask: {sorted(ct.dead)[:10]}...")
```

### Active-list mapping

Which global filament (0–95) sits at which physical power slot — the
host-owned "active-list" model every other method's filament→board
resolution is built on. Every filament is assigned to controller 1, 2, or
left unassigned; within a controller, filaments pack into power slots
(`channel*8 + position`) in **ascending filament order** — this model does
not support an arbitrary custom slot order, only which controller a
filament lands on. Changing this mapping is a structural change to the
whole rig's board layout — everything downstream (currents cache, HV grid
addressing, schedules) depends on it, so `download()` again after changing
it, before firing anything.

**`get_mapping()`** — Read the current mapping.

```python
m = ct.get_mapping()
mp = m["mapping"]
print(mp["counts"])          # {"1": 48, "2": 48} — filaments per controller
print(mp["group_size"])      # alternating-group size (default layout)
print(mp["overflow"])        # {"1": [...], "2": [...]} — assigned past slot 63, can't fire
for row in mp["filaments"][:3]:
    print(row)
    # {"filament": 0, "controller": 0, "slot": 0, "channel": 0, "position": 0}
    # controller is 0-based (0 or 1) here, or None if unassigned
```

**`set_mapping(assignment=None, group_size=None, skip_channels=None, upload=True)`**
— Edit the mapping. Pass any combination of:

- `assignment`: `list[96]` of `0`/`1`/`None` — which controller (0-based)
  each global filament belongs to.
- `group_size`: reset to the default alternating-group pattern instead of a
  custom assignment.
- `skip_channels`: `{"1": [chan_idx, ...], "2": [...]}` (0-indexed channels
  to treat as broken/empty — filaments skip over them when packing into
  slots) — or a flat list applied to both controllers.
- `upload` (default `True`): also push the new active-list + channel mask
  to every connected controller immediately.

Invalidates the host-side currents-download cache automatically (remapping
moves filaments between boards, so cached idle/active mA per filament are
no longer meaningful) — the next `download()` re-sends them.

```python
# Mark channel index 4 (CH5) as broken on controller 1 — filaments skip
# over it and pack into the remaining good channels instead
ct.set_mapping(skip_channels={"1": [4]})

# Reset to a plain 8-filament alternating group size
ct.set_mapping(group_size=8)

# Fully custom: filaments 0-47 -> controller 1, 48-95 -> controller 2
ct.set_mapping(assignment=[0]*48 + [1]*48)
```

**`filament_to_board(filament)`** — Forward lookup: global filament index
→ physical board location. Returns `None` if unassigned or overflowed past
the usable slots (see `get_mapping()`'s `"overflow"`).

```python
b = ct.filament_to_board(5)
print(b)   # {"controller": 1, "channel": 0, "position": 5, "slot": 5}
```

**`board_to_filament(controller, channel, position)`** — Reverse lookup:
physical board location → global filament index. Returns `None` if that
slot is unassigned.

```python
fil = ct.board_to_filament(1, 0, 5)
print(fil)   # 5
```

### Filament order swap

A **purely client-side** index remap — distinct from
[active-list mapping](#active-list-mapping) above, which is the *backend's*
hardware mapping. This is a simple software-level swap for when boards
were physically wired in a different order than you'd naturally number
them — e.g. filament 5 is actually the board wired where you'd expect
filament 8 to be. It never touches the backend or the RP2350's own
mapping; it's just a translation table this `CTClient` instance applies
transparently.

**Once set, keep using your own (logical) numbering everywhere** — the
dead mask, single-filament calls, batch calls, reads, `fire_single_pulse`,
everything. Every method transparently translates your logical index to
the physical one right before talking to hardware, and translates any
filament-indexed data in the *response* back to logical before handing it
to you. You never need to translate anything yourself.

**`set_filament_order(order)`** — Define the swap.

```python
# filament 5 is physically wired where the hardware calls filament 8:
ct.set_filament_order({5: 8})

ct.active_one(5, current_ma=2900)      # actually commands physical filament 8
ct.read_filament_current(5)            # actually reads physical filament 8,
                                        # returned to you keyed as "filament 5"
ct.fire_single_pulse(filament=5)       # fires physical filament 8
```

`order` can also be a list where `order[i]` is the physical index for
logical filament `i`:

```python
order = list(range(96))
order[5] = 8   # only this one entry differs from identity
ct.set_filament_order(order)
```

Pass `None` (or `{}`) to clear back to identity (no swap):

```python
ct.set_filament_order(None)
```

**`get_filament_order()`** — Read the current swap (`{}` = identity, no swap).

```python
print(ct.get_filament_order())   # {5: 8}
```

**What's covered**: every filament-taking method — `stop_one`/`sleep_one`/
`standby_one`/`idle_one`/`active_one`/`voltage_one`, `stop_all`/`sleep_all`/
`standby_all`/`idle_all`/`active_all`/`voltage_all` (including `currents`/
`millivolts` dict keys), `hv_grid_set`/
`hv_grid_set_all`/`hv_grid_off_all`/`hv_grid_status`, `get_ocp_threshold_one`/
`set_ocp_threshold_one`/`_all`, `read_board_status`,
`read_filament_current`/`read_filament_currents`,
`read_filament_voltage`/`read_filament_voltages`,
`filament_to_board`/`board_to_filament`, `fire_single_pulse`, and the
low-level `shv_set_entry`/`shv_status`/`shv_pulse_log`. The dead mask
(`set_dead`/`add_dead`/`remove_dead`) always operates in **your own logical
numbering**, independent of any swap.

```python
ct.set_filament_order({5: 8})
ct.set_dead([6])   # blocks LOGICAL filament 6, unaffected by the 5->8 swap

# Verify the round-trip:
board = ct.filament_to_board(5)              # physical 8's board location
back  = ct.board_to_filament(board["controller"], board["channel"], board["position"])
print(back)   # 5 -- reverse-translated back to your logical number
```

### Lease

Coordinates write access between your script and the GUI (or other scripts).
See [Multi-client / GUI co-existence](#multi-client--gui-co-existence) for
the full explanation of when a lease is needed.

**`acquire_lease(ttl=60.0, note="")`** — Take the exclusive write lock for up
to `ttl` seconds. **This is the one method in this client that raises by
default** — `CTLeaseError` if someone else already holds it. See
[Error handling](#error-handling--read-this-first) above for why.

```python
ct.acquire_lease(ttl=120, note="overnight soak test")
```

**`release_lease()`** — Release the lease early. Safe to call even if you
don't currently hold it (no-op).

```python
ct.release_lease()
```

**`renew_lease(ttl=60.0)`** — Extend the lease before it expires, e.g. inside
a long-running loop.

```python
import time
ct.acquire_lease(ttl=30)
for i in range(100):
    ct.active_one(i, 2900)
    time.sleep(1)
    ct.renew_lease(ttl=30)   # keep the lease alive for the next iteration
ct.release_lease()
```

**`with ct.lease(ttl=60.0, note=""):`** — Context manager: acquires on enter,
always releases on exit (even if an exception is raised inside the block).
This is the recommended way to use the lease.

```python
with ct.lease(ttl=60, note="firing sequence"):
    ct.shv_arm(1)
    result = ct.fire_single_pulse(filament=5)
# lease released automatically here, even on error
```

### Session — guaranteed safe teardown

`with ct.session():` wraps your script body and guarantees a safe teardown
runs on exit — even if your own code raises an unrelated exception (a bug,
a `Ctrl+C`, anything) partway through. It does NOT suppress the exception;
it re-raises after cleanup so you still see what broke.

Teardown, in order (each step attempted independently — one failing
doesn't block the rest): disable emission HV, disable focus HV, instantly
clear the HV grid ([`hv_grid_clear_all()`](#hv-grid-switch-force-toggle)),
then STOP every populated filament.

This is on top of — not instead of — the fact that this client's methods
already don't raise by default (see
[Error handling](#error-handling--read-this-first)). Use it as extra
insurance for the whole script:

```python
with ct.session():
    ct.active_one(5, 2900)
    ct.enable_emission(True)
    ...  # if this raises, HV still gets shut off safely on the way out
# HV disabled, grid cleared, all filaments stopped — guaranteed, even on error
```

`cleanup=False` skips the teardown (e.g. if you're deliberately leaving HV
on across multiple `with ct.session():` blocks and handling shutdown
yourself):

```python
with ct.session(cleanup=False):
    ...
```

### Dead mask

Block specific filaments from **all** heating and HV pulse operations.
Set once at startup; every subsequent batch call silently skips them, and
`active_one` / `fire_single_pulse` / other single-filament calls return
`{"ok": False, "dead": True, ...}` immediately if asked to operate on a
dead filament — they do NOT raise (see
[Error handling](#error-handling--read-this-first)).

```python
ct.set_dead([3, 7, 12, 55])   # replace the entire dead mask
ct.add_dead(20, 21)            # add individual filaments
ct.remove_dead(7)              # un-block a filament
print(ct.dead)                 # {3, 12, 20, 21, 55}
```

### Power state

Every filament sits in one of five states: STOP → SLEEP → STANDBY → IDLE →
ACTIVE, in increasing order of readiness (ACTIVE is the only state that
actually heats to firing current). There is also a sixth, out-of-ladder
state, **VOLTAGE** — a fixed-voltage hold for bench/calibration use, see
`voltage_one`/`voltage_all` below.

**There are two distinct APIs, and they are NOT interchangeable:**

| | Batch (`*_all`) | Single (`*_one`) |
|---|---|---|
| Targets | every populated board, or a list | exactly one filament |
| Wire format | RP2350's masked multi-board frame (one frame per channel group) | RP2350's dedicated single-board frame |
| Endpoint | `/api/filament-prep` | `/api/filament-state` |
| Soft per-board failure | returned in `"failed": [...]`, batch call still succeeds overall | returned as `{"ok": False, ...}`, no exception |
| Dead filament in the target set | silently skipped | `stop_one`/etc. return `{"ok": False, "dead": True, ...}` immediately, no exception |

Passing `filaments=[5]` to a `*_all` method technically works (it's a
1-element batch), but it still goes through the masked multi-board wire
format meant for groups — **use the dedicated `*_one` method whenever
you're targeting exactly one filament.** It's a different, simpler frame
on the wire, and its response shape is honest about targeting one board
instead of reusing the batch response shape for a batch of one.

#### Single filament — `*_one`

**Setting a state only confirms the RP2350 accepted the command — it says
nothing about whether the filament actually got there.** A board can be
absent, faulted, or thermally slow, and the command would still return
`ok: True`. Every `*_one` method below takes an optional `verify=True` to
get REAL feedback: it polls the measured heating current afterward and
merges the result under `result["heating"]`.

**`stop_one(filament, verify=False, timeout_s=5.0)`** /
**`sleep_one(filament, verify=False, timeout_s=5.0)`** /
**`standby_one(filament, verify=False, timeout_s=5.0)`**
— Set one filament to STOP / SLEEP / STANDBY. Never raises: returns
`{"ok": False, "dead": True, ...}` if the filament is dead, or
`{"ok": False, "error": ...}` if it has no board mapping or the board
simply didn't ACK (e.g. temporarily unseated) — always check `"ok"`.
`verify=True` polls until the measured current drops to ~0 mA (confirms
it actually stopped heating).

```python
ct.stop_one(5)
ct.sleep_one(5)
ct.standby_one(5)

# With real feedback:
r = ct.stop_one(5, verify=True)
print(r["heating"])   # {"ok": True, "measured_ma": 0.0, "elapsed_s": 0.2, ...}
```

**`idle_one(filament, current_ma, verify=False, tolerance_ma=150.0, timeout_s=5.0)`**
— Idle exactly one filament at `current_ma` mA (warm pool, ready to promote
to ACTIVE quickly).

```python
ct.idle_one(5, current_ma=1500)

# With real feedback — confirms it actually reached 1500 mA (±150 mA),
# not just that the command was accepted:
r = ct.idle_one(5, current_ma=1500, verify=True, timeout_s=5.0)
print(r["heating"])
# {"ok": True, "filament": 5, "target_ma": 1500.0, "measured_ma": 1487.0,
#  "elapsed_s": 1.4, "present": True, "cc_mode": 1}
if not r["heating"]["ok"]:
    print(f"WARNING: filament 5 only reached {r['heating']['measured_ma']} mA "
          f"of {r['heating']['target_ma']} mA target — board absent or faulted?")
```

**`active_one(filament, current_ma, verify=False, tolerance_ma=150.0, timeout_s=5.0)`**
— Promote exactly one filament to ACTIVE at `current_ma` mA — the state
that actually fires HV-ready. Same `verify=True` feedback shape as
`idle_one`.

```python
ct.active_one(5, current_ma=2900)

r = ct.active_one(5, current_ma=2900, verify=True, timeout_s=5.0)
if not r["heating"]["ok"]:
    raise RuntimeError(f"filament 5 not at firing current: {r['heating']}")
```

**`voltage_one(filament, millivolts, verify=False, timeout_s=5.0)`** — Drive
exactly one filament to manual **VOLTAGE** mode (PowerState 6) at
`millivolts` mV — a fixed voltage hold, NOT current-regulated (unlike
`idle_one`/`active_one`'s closed CC loop). Mostly for bench/calibration use
(probing a point on the load curve) rather than normal heating control.
Firmware clamps the target to 0.8–15 V (800–15000 mV); out-of-range values
are rejected here before any frame is sent.

```python
ct.voltage_one(5, millivolts=5000)   # hold filament 5's board at 5.0 V

# verify=True has no current target to poll (unlike idle_one/active_one) —
# it instead polls until the board reports cc_mode==0 (voltage), confirming
# it actually entered voltage-regulation rather than a fault:
r = ct.voltage_one(5, millivolts=5000, verify=True, timeout_s=5.0)
print(r["heating"])
# {"ok": True, "filament": 5, "cc_mode": 0, "elapsed_s": 0.4, "present": True}
```

**`wait_for_current(filament, target_ma, tolerance_ma=150.0, timeout_s=5.0, poll_interval_s=0.2)`**
— The primitive `verify=True` uses internally. Call it directly if you want
to check status separately from the command (e.g. re-check later, or check
without re-commanding anything). Never raises — always returns a dict;
check `["ok"]` yourself.

```python
status = ct.wait_for_current(5, target_ma=1500, tolerance_ma=150, timeout_s=5.0)
print(status)
# {"ok": bool, "filament": 5, "target_ma": 1500.0, "measured_ma": ...,
#  "elapsed_s": ..., "present": bool, "cc_mode": int}
```

A typical single-filament cycle with real feedback at each step — nothing
here raises, so a bad filament just gets logged and skipped instead of
killing the whole loop (wrap the whole thing in `with ct.session():` for
guaranteed teardown too — see [Session](#session--guaranteed-safe-teardown)):

```python
r = ct.idle_one(5, current_ma=1500, verify=True, timeout_s=5.0)
if not r["ok"] or not r["heating"]["ok"]:
    print(f"filament 5 didn't reach idle, skipping: {r}")
else:
    r = ct.active_one(5, current_ma=2900, verify=True, timeout_s=5.0)
    if not r["ok"] or not r["heating"]["ok"]:
        print(f"filament 5 didn't reach active, skipping: {r}")
    else:
        # ... fire_single_pulse(filament=5) ...
        pass

ct.idle_one(5, current_ma=1500)   # demote back to warm pool
# ... or ...
ct.stop_one(5)                     # fully de-energize when done with it
```

#### Batch — `*_all`

All batch methods accept `filaments=None` (every populated board minus the
dead mask) or an explicit list of 0–95 filament indices — dead ones are
always silently removed.

**`stop_all(filaments=None)`** — Fully de-energize a batch: HV off, heating
off. The safe resting state; always call this when you're done.

```python
ct.stop_all()                    # every populated filament
ct.stop_all(filaments=[5, 6, 7]) # just these three
```

**`sleep_all(filaments=None)`** — Low-power resting state for a batch, one
step above STOP. Rarely used directly; mostly a transitional state in the
ladder.

```python
ct.sleep_all()
```

**`standby_all(filaments=None)`** — Powered but not heating, for a batch.
Use between scans when you want to keep boards ready without drawing idle
current.

```python
ct.standby_all(filaments=[0, 1, 2, 3])
```

**`idle_all(filaments=None, currents=None, default_ma=0)`** — Warm pool for
a BATCH of filaments at once. See the dedicated section below.

**`active_all(filaments=None, currents=None, default_ma=0)`** — Promote a
BATCH of filaments to ACTIVE at once. Same `currents` / `default_ma` shape
as `idle_all`. Less common than firing filaments one at a time via
`active_one` — mainly useful for multi-filament group tests.

```python
ct.active_all(filaments=[0, 1, 2], default_ma=2900)
```

**`voltage_all(filaments=None, millivolts=None, default_mv=800)`** — Drive a
BATCH of filaments to manual VOLTAGE mode at once. Same
`millivolts`/`default_mv` shape as `idle_all`'s `currents`/`default_ma`,
except `default_mv` defaults to **800** (the firmware's own STANDBY floor),
not 0 — 0 mV is below the firmware's 0.8–15 V clamp, so unlike
`idle_all`/`active_all` there's no "silently does nothing" footgun from
omitting it.

```python
ct.voltage_all(filaments=[0, 1, 2], default_mv=5000)
```

#### `idle_all` — changing idle current

`currents` maps `{filament_index: mA}` for explicit per-filament targets.
Any filament not listed in `currents` gets `default_ma` instead — there is
**no firmware-side default**, so a filament with neither an entry in
`currents` nor a `default_ma` idles at **0 mA** (i.e. no heat).

```python
# 1. Idle EVERY populated filament at the same current
ct.idle_all(default_ma=1500)

# 2. Idle every filament at 1500 mA, except a few overridden individually
ct.idle_all(default_ma=1500, currents={5: 1300, 6: 1300, 40: 1800})
# filaments 5, 6 -> 1300 mA; filament 40 -> 1800 mA; everyone else -> 1500 mA

# 3. Idle only a specific subset, all at the same current
ct.idle_all(filaments=[0, 1, 2, 3], default_ma=1500)

# 4. Idle a subset with per-filament currents from a lookup table
IDLE_MA = {0: 1500, 1: 1600, 2: 1400, 3: 1500}
ct.idle_all(filaments=list(IDLE_MA), currents=IDLE_MA)

# 5. Ramp one filament's idle current up in steps (e.g. thermal soak test)
import time
for ma in (500, 1000, 1500, 2000, 2500):
    ct.idle_all(filaments=[7], currents={7: ma})
    time.sleep(2)

# 6. CAUTION: calling idle_all() with no arguments idles every filament
#    at 0 mA — it will NOT warm anything up. Always pass default_ma or a
#    full currents dict when the intent is to actually heat.
```

`active_all` takes the same `currents` / `default_ma` shape for the ACTIVE
state. Dead-masked filaments are always stripped from both `filaments` and
`currents` before the request is sent — see [Dead mask](#dead-mask).

### Board heating status

Distinct from [`read_filament_currents()`](#filament-heating-current)
above: that reads the CACHED CC-loop current (no I2C, cheap, bulk). This
reads the RP2350's own **last-commanded PowerState and fault kind** for
ONE board directly (a single-board I2C round-trip) — use it when you need
to know the actual state/fault, not just the measured current.

**`read_board_status(filament)`** — Never raises; check `"ok"`.

```python
st = ct.read_board_status(5)
print(st)
# {"ok": True, "filament": 5, "controller": 1, "channel": 0, "mux_port": 5,
#  "state": 1, "state_name": "STOP", "fault": 0, "fault_name": "none"}
if st["ok"] and st["fault"] != 0:
    print(f"filament 5 fault: {st['fault_name']}")
```

`state`: 1=STOP, 2=SLEEP, 3=STANDBY, 4=IDLE, 5=ACTIVE, 6=VOLTAGE.
`fault`: 0=none, 1=open filament, 2=OCP/SCP.

### OCP protection

**Two distinct, unrelated OCP mechanisms exist in this firmware** — don't
conflate them:

| | Per-board threshold | Global startup floor |
|---|---|---|
| Scope | ONE board's TPS55289 | Whole controller (RP2350), not per-board |
| What it is | The "real" steady-state OCP trip current | A two-stage floor: STARTUP (tolerates cold-inrush) then STEADY ~2 s later |
| Method | `get_ocp_threshold_one`/`set_ocp_threshold_one`/`_all` | `get_ocp_startup`/`set_ocp_startup` |

**Not exposed**: the ~2 s timing delay between STARTUP and STEADY, and the
TPS55289's internal deglitch-bit settings, are compiled-in firmware
constants — there's no UART command to change them, only the two current
thresholds. If you need the delay itself tunable, that requires a firmware
change (a new UART opcode) first.

**`get_ocp_threshold_one(filament)`** — Read back ONE board's *currently
configured* TPS55289 OCP trip current — a real hardware register read, not
just "whatever you last called `set_ocp_threshold_one` with" (catches a
threshold set by another client, or one that predates this process).
`CH_SET_TPS_OCP_THRESHOLD` is SET-only in firmware; this decodes the raw
TPS55289 register instead (single-board I2C read — a fine one-off check,
don't loop this to poll many boards).

```python
r = ct.get_ocp_threshold_one(5)
print(r)   # {"ok": True, "filament": 5, "controller": 1, "channel": 0,
           #  "mux_port": 0, "enabled": True, "threshold_ma": 3200}
if not r["enabled"]:
    print("OCP protection is currently OFF for filament 5")
```

**`set_ocp_threshold_one(filament, threshold_ma)`** — Set ONE board's
TPS55289 OCP trip current. No dead-mask guard (OCP is a protection
setting, not a heating/HV action) — call it even on a dead filament if you
want to lower its trip point.

```python
ct.set_ocp_threshold_one(5, 3200)
```

**`set_ocp_threshold_all(filaments=None, threshold_ma=0)`** — Set a BATCH
of boards' OCP trip current. No native batch opcode exists for this — the
backend loops one frame per board (same pattern as the batch power-state
calls).

```python
ct.set_ocp_threshold_all(threshold_ma=3200)                     # every populated board
ct.set_ocp_threshold_all(filaments=[5, 10, 15], threshold_ma=3200)
```

**`get_ocp_startup(controller=1)`** / **`set_ocp_startup(startup_ma, steady_ma=None, controller=1)`**
— Read/write the global two-stage floor. `steady_ma` is optional on set —
omit it to leave the steady threshold unchanged and only update startup.

```python
r = ct.get_ocp_startup(1)
print(r)   # {"ok": True, "controller": 1, "startup_ma": 3400, "steady_ma": 3200}

ct.set_ocp_startup(startup_ma=4000, steady_ma=3000, controller=1)
```

### HV grid switch (Force toggle)

Directly toggle a filament's HV isolation relay, bypassing the normal
power-state ladder — the same operation as the GUI's HV grid tiles with the
**Force** checkbox checked. Useful for bench debugging (e.g. energising one
board manually) or clearing a switch stuck in the wrong state.

`force=True` (the default) uses writeMode=2 — it bypasses the firmware's
fault/verify checks, matching the GUI's Force checkbox. Set `force=False`
to require the firmware's own verify pass instead (writeMode=1).

**As with every batch call, dead-masked filaments are always excluded** —
`hv_grid_set` returns `{"ok": False, "dead": True, ...}` immediately if the
target is dead (never raises), and `hv_grid_set_all` / `hv_grid_off_all`
silently strip dead filaments from the batch before sending the request.

```python
ct.set_dead([3, 7, 12])   # these are physically damaged / removed boards

# Toggle ONE filament's switch ON (returns {"ok": False, "dead": True} if
# filament 3 is dead — never raises)
ct.hv_grid_set(10, on=True, force=True)

# Toggle a batch ON — filament 12 is silently skipped (dead)
ct.hv_grid_set_all(filaments=[10, 11, 12], on=True)

# Turn OFF every populated board (skips the whole dead mask automatically)
ct.hv_grid_off_all()

# Turn off only a subset
ct.hv_grid_off_all(filaments=[10, 11])

# Read back actual switch state (desired vs. feedback sense line — a
# mismatch flags a stuck/dead switch)
status = ct.hv_grid_status(controller=1)
for fil, s in status["filaments"].items():
    if s["desired"] != s["feedback"]:
        print(f"filament {fil}: switch mismatch! desired={s['desired']} feedback={s['feedback']}")
```

| Method | Description |
|--------|-------------|
| `hv_grid_set(filament, on, force=True)` | Toggle one filament's switch; `{"ok": False, "dead": True}` if dead |
| `hv_grid_set_all(filaments=None, on=False, force=True)` | Toggle a batch, excluding dead mask |
| `hv_grid_off_all(filaments=None, force=True)` | Convenience for `hv_grid_set_all(on=False)` |
| `hv_grid_status(controller=1)` | Read `{filament: {desired, feedback}}` for every populated board |
| `hv_grid_clear_all()` | Instant hardware clear of EVERY channel (see below) |

#### `hv_grid_clear_all` — instant hardware clear

Zeros **every** HV grid output on **every** connected controller at once,
using the 74HC595 shift register's hardware `/SRCLR` clear pin — an
asynchronous clear that bypasses the normal per-bit shift-and-latch write
path entirely. This is the fastest possible way to kill all HV grid outputs,
and it's a whole-chain hardware operation: **the dead mask does not apply**
here, since there's no per-filament targeting — every channel on every
board goes to 0 regardless of which filaments are marked dead.

Use it as an emergency "kill everything now": before walking away from the
bench, after an unexpected fault, or whenever you want a known-clean
starting point before re-arming.

```python
ct.hv_grid_clear_all()
print(ct.hv_grid_status(1))   # every filament: desired=False, feedback=False
```

```python
r = ct.hv_grid_clear_all()
print(r)   # {"ok": True, "results": {"1": {"ok": True}, "2": {"ok": True}}}
```

### SHV run policy & HV bit-bang diagnostics

**`get_fault_policy(controller=1)`** / **`set_fault_policy(controller=1, board=None, mismatch=None)`**
— Two independent stop/continue switches for a run that hits trouble:
`board` (0=stop, 1=continue on a CC/OCP hardware fault) and `mismatch`
(0=stop, 1=continue on an HC165 read-back mismatch). `set_fault_policy`
leaves either policy unchanged if you omit it. Both calls also report
which boards actually faulted — the only record of that under a
"continue" policy.

```python
ct.set_fault_policy(1, board=1, mismatch=0)   # continue past CC/OCP faults,
                                               # but still stop on a mismatch
r = ct.get_fault_policy(1)
print(r)
# {"ok": True, "board": 1, "mismatch": 0, "mismatchCount": 0,
#  "faultedSlots": [...], "faultedFilaments": [...]}   # your logical numbering
```

**`get_trigger_delay(controller=1)`** / **`set_trigger_delay(delay_us, controller=1)`**
— A small, deliberate offset (µs, uint16, 0–65535) between the SyncIn
trigger edge and the RP2350 actually firing. **Always check `"applies"`**:
a set that the live fire path (e.g. PIO precision mode) can't currently
honour still returns `"ok": True` (the setting was stored) but
`"applies": False` — meaning it won't actually change anything when
pulses fire, without you being able to tell unless you check this field.

```python
r = ct.set_trigger_delay(500, controller=1)   # 500 µs
print(r)   # {"ok": True, "delayUs": 500, "applies": True}
if not r["applies"]:
    print("WARNING: delay set but not honoured by the live fire path")
```

**`read_hv_diag165(controller=1, channel=0, test_byte=0x55, settle_ms=5)`**
— Raw HC165 shift-register readback diagnostic for one channel: writes
`test_byte`, reads it back twice, then clears. Safe at any time (doesn't
touch HV/heating) — a bench/signal-integrity check, not something you'd
call during normal operation.

```python
r = ct.read_hv_diag165(1, channel=0, test_byte=0x55, settle_ms=5)
print(r)
# {"ok": True, "channel": 0, "test_byte": 85, "r0": 0, "r1": 85, "r2": 85, "r3": 0}
# r0/r3 should read 0 (baseline/final clear); r1/r2 should match test_byte
# — a mismatch flags a shift-chain or cabling problem on that channel.
```

**`set_hv_shift_hz(hz, controller=1)`** — Set the HC165 readback bit-bang
SCK frequency (Hz) — for signal-integrity testing on long cables (e.g. drop
it to 1 kHz to see a spike-free waveform on a scope). **SET-only**: there's
no separate "read current value" request — the firmware always requires a
fresh value and echoes back the ACTUAL frequency now in effect (clamped
100 Hz–2 MHz, so what you ask for and what you get may differ slightly).
Survives until the next reboot.

```python
r = ct.set_hv_shift_hz(1000, controller=1)
print(r)   # {"ok": True, "controller": 1, "actualHz": 1000}
```

### HV set

The backend loads the calibrated wiper→voltage LUT (built via the GUI's
Calibrate sweep) and interpolates the DS3502 wiper for you — you always pass
a **magnitude** (positive number); the actual rail is negative. If no LUT has
been calibrated yet, the backend falls back to a linear approximation so the
call still does something reasonable.

Every method returns `{"ok", "wiper", "expect_v"/"expect_ma", "method"}`,
where `method` is `"lut"` (interpolated from calibration), `"lut(clamped)"`
(target was outside the calibrated range, clamped to the nearest edge), or
`"linear(no-lut)"` (no calibration exists — used the hardware full-scale
constant instead).

**`set_emission_v(volts)`** — Set the emission HV magnitude.

```python
r = ct.set_emission_v(30)   # target −30 V
print(r)   # {"ok": True, "wiper": 41, "expect_v": -29.8, "method": "lut"}
```

**`set_focus_v(volts)`** — Set the focus HV magnitude.

```python
r = ct.set_focus_v(150)     # target −150 V
print(r)   # {"ok": True, "wiper": 38, "expect_v": -150.0, "method": "lut"}
```

**`set_emission_i(ma)`** — Set the emission current reference (0–85.7 mA).
This channel has no calibrated LUT — it is always a linear DS3502 scale.

```python
r = ct.set_emission_i(10)   # target 10 mA
print(r)   # {"ok": True, "wiper": 15, "expect_ma": 10.12}
```

### HV readback (ADS1115)

Live measured values from the STM32's ADS1115 ADC — use these to confirm
the setpoints above actually landed, or to monitor HV during a run.

**`read_ads_all()`** — Read all four channels in one call (most efficient
if you need more than one value).

```python
a = ct.read_ads_all()
print(a["emiss_v"], a["emiss_i_ma"], a["focus_v"], a["ref_mv"])
# -29.8 9.94 -149.6 1200.1
print(a["codes"])   # [int×4] raw ADS1115 counts, for debugging
```

**`read_emission_v()`** — Measured emission voltage (V, negative). Single-
value convenience wrapper around `read_ads_all()`.

```python
v = ct.read_emission_v()
print(f"emission: {v} V")
```

**`read_emission_i()`** — Measured emission **beam** current (mA), off the
shared ADS1115 bus. This is NOT a per-filament heating current — it's
whichever filament currently has its [HV grid switch](#hv-grid-switch-force-toggle)
on. For a filament's own cathode HEATING current, see
[Filament heating current](#filament-heating-current) below.

```python
i = ct.read_emission_i()
print(f"emission beam current: {i} mA")
```

**`read_focus_v()`** — Measured focus voltage (V, negative).

```python
v = ct.read_focus_v()
print(f"focus: {v} V")
```

### Filament heating current

Distinct from `read_emission_i()` above: that reads the shared emission
**beam** current off the ADS1115. These read each filament's own **cathode
heating** current from the CC loop (INA219, cached — no I2C on the
firmware side, so it's safe to poll even while a schedule is running).
Use them to confirm `idle_one()`/`active_one()` actually landed at the
current you commanded, or to check a filament's state before firing it.

**`read_filament_currents(filaments=None)`** — Bulk read of every
populated filament's measured heating current.

```python
all_currents = ct.read_filament_currents()
for fil, c in all_currents.items():
    print(f"fil {fil}: {c['current_mA']} mA (target {c['target_mA']} mA, "
          f"present={c['present']})")

# Or filter to just a few:
subset = ct.read_filament_currents([0, 1, 2])
```

Each entry: `{"current_mA", "target_mA", "present", "cc_mode"}`.
`cc_mode`: `0`=voltage, `1`=current (Idle/Active regulating), `2`/`3`=fault.
`present` is inferred from whether the CC loop is actively regulating that
board — not a live I2C presence scan (see
[`present_filaments()`](#connection) for that).

**`read_filament_current(filament)`** — Convenience wrapper for one
filament. Returns `0.0` if it isn't present/regulated.

```python
ma = ct.read_filament_current(5)
print(f"filament 5 heating current: {ma} mA")
```

A typical use: confirm a filament actually reached its idle target before
promoting it to active:

```python
import time

ct.idle_one(5, current_ma=1500)
time.sleep(2)   # let the CC loop settle
measured = ct.read_filament_current(5)
if abs(measured - 1500) > 100:   # mA tolerance
    raise RuntimeError(f"filament 5 didn't reach idle target: {measured} mA")
ct.active_one(5, current_ma=2900)
```

### Filament board voltage

**`read_filament_voltages(filaments=None)`** — Bulk read of every populated
filament's measured board voltage (mV) **and** current (mA) together.
Unlike `read_filament_currents()` above (CC-loop cached current, no I2C,
always safe to poll), this is a real INA219 **I2C sweep** — the backend
skips it automatically while a schedule is firing (I2C would stall pulses)
and falls back to the cached current for that window: mid-run, entries
come back with `"bus_mV": 0` and `"cached": True` — voltage genuinely
isn't available then; call again once the run completes.

```python
v = ct.read_filament_voltages()
for fil, row in v.items():
    print(f"fil {fil}: {row['bus_mV']} mV, {row['current_mA']} mA "
          f"(present={row['present']}, cached={row.get('cached', False)})")
```

**`read_filament_voltage(filament)`** — Convenience wrapper for one
filament. Returns `None` (not `0.0`) if the filament isn't present, or if
voltage isn't available right now (mid-run) — distinct from a genuine 0 mV
reading.

```python
mv = ct.read_filament_voltage(5)
if mv is None:
    print("filament 5 not present, or voltage unavailable mid-run")
else:
    print(f"filament 5: {mv} mV")
```

### HV enable

**This is NOT the same concept as "arming" a schedule.** `shv_arm` /
`fire_single_pulse` arm the RP2350's PULSE SCHEDULE — whether it's ready to
fire a brief, timed pulse on a SyncIn trigger. `enable_emission` /
`enable_focus` are a completely separate master power switch on the STM32
board — whether the HV rail itself is even powered at all. Nothing (pulsed
or continuous) can appear on the emission/focus bus unless the relevant
`enable_*` is `True`, regardless of the SHV schedule's armed state.

**`enable_emission(on: bool)`** — Turn the emission HV rail on or off. This
is an **absolute set, not a toggle**: `on=True` always commands ON and
`on=False` always commands OFF, regardless of whatever state it was in
before the call — matches the GUI's explicit **Emission ON** / **Emission
OFF** buttons and the wire-level `/stm32/hv_enable` request, which both
take an absolute `on` value.
`set_emission_v` only sets the DS3502 wiper (the target voltage) — it does
NOT energize the rail by itself; `enable_emission(True)` is the actual
power switch.

```python
ct.enable_emission(True)    # energize
...
ct.enable_emission(False)   # de-energize (always do this when finished)
```

**`enable_focus(on: bool)`** — Turn the focus HV rail on or off. Same
relationship to `set_focus_v` as above.

```python
ct.enable_focus(True)
ct.enable_focus(False)
```

**`hv_status()`** — Read the current HV enable/fault state.

```python
s = ct.hv_status()
print(s)   # {"emission_on": True, "focus_on": False,
           #  "ads1115_alert": False, "amc3301_diag": False}
if s["ads1115_alert"]:
    print("WARNING: ADS1115 alert flag set")
```

#### DC HV toggle — continuous current test (not a pulse)

A different bench-test shape than `fire_single_pulse`: instead of a brief,
scheduled pulse fired via the SHV schedule engine, this routes HV
*continuously* to one filament's board and reads the *steady-state* DC
current straight off the ADS1115 — useful for bench calibration or
verifying a board's HV switch and current path without going through the
pulse-schedule machinery at all.

The flow: [route HV to the filament's board](#hv-grid-switch-force-toggle)
→ wait for the switch to settle → [read the DC current](#hv-readback-ads1115)
→ switch it back off. `enable_emission` must already be `True` (and a
target voltage set) before this does anything — `hv_grid_set` only decides
*which* filament sees the already-live rail, exactly like a physical
mosfet/relay gating power to one board.

```python
import time

ct.set_emission_v(30)
ct.enable_emission(True)
time.sleep(0.3)   # let the rail settle

# Toggle the filament's HV grid switch ON — this is the per-filament
# "emission mosfet" that routes the live rail to board #5 specifically.
ct.hv_grid_set(5, on=True, force=True)
time.sleep(0.3)   # let the switch settle before reading

# Read the steady-state DC current (ADS1115), not a per-pulse measurement.
dc_ma = ct.read_emission_i()
print(f"filament 5 DC emission current: {dc_ma} mA")

# Switch it back off before moving to the next filament or tearing down.
ct.hv_grid_set(5, on=False, force=True)
time.sleep(0.1)

ct.enable_emission(False)   # de-energize the rail when fully done
```

To sweep several filaments, toggle one at a time — never grid more than one
on at once unless you specifically intend to sum their currents on the
shared bus:

```python
results = {}
ct.set_emission_v(30)
ct.enable_emission(True)
time.sleep(0.3)

for fil in [5, 6, 7]:
    ct.hv_grid_set(fil, on=True, force=True)
    time.sleep(0.3)
    results[fil] = ct.read_emission_i()
    ct.hv_grid_set(fil, on=False, force=True)
    time.sleep(0.1)

ct.enable_emission(False)
print(results)   # {5: 8.4, 6: 0.1, 7: 9.1}  (fil 6's switch may be
                  # unseated/faulty here — near-zero current with the
                  # switch commanded ON is worth investigating)
```

### Schedule download

Loading a schedule onto the RP2350 is a multi-frame transfer (active-list
mapping, channel mask, timing config, the emission table itself, plus an
optional heat table) that has to land completely and correctly before arming
means anything. `download()` is the **same reliable, pipelined path the GUI
itself uses** — per-frame retries, currents caching, and a CRC you can check
with `verify_schedule()`. `fire_single_pulse` uses these two internally; you
rarely need to call them directly, but they're here for custom sequences.

**`download(plan, timeout=30.0)`** — Send a schedule plan to every connected
controller. The full emission list goes to *every* connected controller —
each RP2350 only fires the entries for filaments its own active-list
mapping actually owns, so this is safe even in a single-controller setup.

```python
plan = {
    # triggerEdge 0=rising, 1=falling — must match whatever actually drives
    # SyncIn (see shv_set_config below for the full explanation and the
    # failure mode when it's wrong: nothing fires, the run just times out).
    "config": {"interPulseMs": 3000, "maxOnMs": 40, "totalMs": 15000, "triggerEdge": 0},
    "emission": [{"filament": 5, "numPulses": 1, "widthUs": 1000}],
    "heating": [],   # empty — no pre-heat automation needed for an ad-hoc pulse
}
r = ct.download(plan)
print(r)   # {"ok": True, "results": [{"controller": 0, "ok": True, "emit": 1,
           #                           "heat": 0, "frames": 6, ...}]}
```

**`verify_schedule(plan)`** — Read the emission/heat table counts and CRC
back from every connected controller and confirm they match `plan`. Call
this after `download()`, before arming, to catch a corrupted or partial
transfer before firing anything.

```python
v = ct.verify_schedule(plan)
print(v)   # {"ok": True, "results": {"1": {"match": True, "emit": 1,
           #                                "crc": 418803015, ...}}}
if not v["ok"]:
    raise RuntimeError("schedule did not land correctly — do not arm")
```

Multi-filament plan (several entries in one schedule):

```python
plan = {
    "config": {"interPulseMs": 2000, "maxOnMs": 40, "totalMs": 30000,
               "triggerEdge": 0},   # 0=rising — see download() above
    "emission": [
        {"filament": 5,  "numPulses": 1, "widthUs": 1000},
        {"filament": 10, "numPulses": 1, "widthUs": 1000},
        {"filament": 15, "numPulses": 2, "widthUs": 800},
    ],
    "heating": [],
}
ct.download(plan)
ct.verify_schedule(plan)
ct.shv_arm(1, repeats=1)
```

### Single-filament HV pulse

**High-level** (recommended):

```python
result = ct.fire_single_pulse(
    filament=0,          # 0–95
    num_pulses=1,
    width_us=1000,       # pulse width in µs
    inter_pulse_ms=3000, # min gap between SyncIn edges
    max_on_ms=40,        # firmware safety guard (ms)
    total_ms=15000,      # schedule timeout (ms)
    controller=1,        # 1 or 2
    trigger="sim",       # "sim" = ESP32 SyncIn; "ext" = external edge
    timeout_s=15.0,
    verify=True,         # confirm the download landed before arming
)
```

Internally: `disarm → download() → verify_schedule() → arm → trigger → poll`
— it builds a one-entry plan from your arguments and runs it through the
same [download](#schedule-download) path described above, not a hand-rolled
sequence of individual SHV commands.

#### Arguments, one at a time

**`filament`** (required, int, 0–95) — the global filament index to fire.
Resolved server-side to a physical (controller, channel, position) via the
active-list mapping. Returns `{"ok": False, "dead": True, ...}`
immediately if the filament is in your dead mask — does not raise.

```python
ct.fire_single_pulse(filament=42)
```

**`num_pulses`** (default `1`) — how many pulses fire in this one burst.
Each pulse is `width_us` long, spaced `inter_pulse_ms` apart. Use more than
1 to fire a repeated burst on the same filament in one schedule.

```python
ct.fire_single_pulse(filament=0, num_pulses=1)   # single shot
ct.fire_single_pulse(filament=0, num_pulses=5)   # 5-pulse burst
```

**`width_us`** (default `1000`) — the HV-on duration of each pulse, in
microseconds. Must stay under the firmware's `max_on_ms` safety guard
(1000 µs = 1 ms here, comfortably under the 40 ms default). **Wire-format
limit: a uint16 field, 0–65535 µs** (~65.5 ms) — a value outside that range
is rejected client-side with a clear error before any network call.

```python
ct.fire_single_pulse(filament=0, width_us=500)    # 0.5 ms pulse
ct.fire_single_pulse(filament=0, width_us=2000)   # 2 ms pulse
```

**`inter_pulse_ms`** (default `3000`) — the minimum time between successive
SyncIn edges, in milliseconds. With `trigger="sim"` this also sets the
spacing of the pulses the ESP32 generates for you; with `trigger="ext"` it's
just a firmware-side minimum guard against edges arriving too close
together.

```python
ct.fire_single_pulse(filament=0, num_pulses=3, inter_pulse_ms=500)
# 3 pulses, ≥500 ms apart
```

**`max_on_ms`** (default `40`) — firmware safety guard, checked at **arm
time** (not a live runtime cutoff mid-pulse): the RP2350 compares
`width_us` against `max_on_ms × 1000` for every entry in the schedule, and
if any pulse would exceed it, `arm()` itself is rejected — the schedule
never starts firing at all. Keep `width_us` comfortably under this value.

**Wire-format limit: a uint16 field, 0–65535 ms** (~65.5 s). A value
outside that range is rejected client-side immediately, with a clear
`{"ok": False, "error": "max_on_ms=<value> out of range (0-65535)"}` —
before it would otherwise reach the backend's own wire encoding, which
raises a much less readable `OverflowError` for the same case.

```python
ct.fire_single_pulse(filament=0, width_us=1000, max_on_ms=40)  # 1 ms << 40 ms, safe
```

**`total_ms`** (default `15000`) — the overall schedule timeout on the
RP2350 itself, in milliseconds. Must be long enough to cover
`num_pulses * inter_pulse_ms` plus margin, or the firmware will time out
the schedule before all pulses fire. Wire-format field is uint32
(0–4294967295 ms) — effectively unbounded for any real schedule, but still
checked and rejected client-side if you somehow pass something outside
that (same for `inter_pulse_ms`).

```python
# 10 pulses at 3000 ms apart needs >= 30000 ms; give it margin
ct.fire_single_pulse(filament=0, num_pulses=10, inter_pulse_ms=3000,
                     total_ms=40000)
```

**`controller`** (default `None`) — which RP2350's **schedule engine** to
arm/trigger/poll. `None` (the default) **auto-infers it from `filament`**
via [`filament_to_board()`](#active-list-mapping) — you don't need to pass
this at all in the common case.

Why it's a separate parameter instead of always being implicit like
`active_one`/`idle_one`/`hv_grid_set`: those commands are single-board and
fully filament-scoped, so the backend resolves the board and routes there
for you. `fire_single_pulse` underneath calls `download()`/`shv_arm()`/
`shv_disarm()`/`shv_status()`/`shv_pulse_log()`/`simulate_sync()` — commands
to a **whole controller's schedule engine** (which can hold entries for
many filaments at once), not to one board. For this single-filament
wrapper, "which engine to arm" is almost always just "whichever controller
owns this filament", so that's the default — but you can still override it
explicitly if you need to (e.g. deliberately targeting a different chained
controller).

**Passing the wrong explicit controller is a real footgun**: `download()`
always reaches every connected controller (harmless), but arming the WRONG
one means the controller that actually owns the board never gets
triggered — the pulse silently never fires, with no direct error pointing
at the mismatch (you'd just see `shv_status` never leave `armed`, or a
timeout). Let auto-inference handle it unless you have a specific reason
not to.

```python
ct.fire_single_pulse(filament=50)              # controller inferred automatically
ct.fire_single_pulse(filament=50, controller=2) # explicit override
```

**`trigger`** (default `"sim"`) — `"sim"` or `"ext"`; see the dedicated
section right below for the full explanation and examples of each.

**`timeout_s`** (default `15.0`) — how long, in seconds, the Python call
itself polls before giving up and raising `CTTimeoutError`. This is a
**client-side** poll timeout, separate from `total_ms` (the firmware's own
schedule timeout). Set it comfortably above `total_ms / 1000` so the
firmware gets to report COMPLETE/FAULT before Python gives up waiting.

```python
# total_ms=40000 (40 s) -> give the client poll a bit more headroom
ct.fire_single_pulse(filament=0, num_pulses=10, inter_pulse_ms=3000,
                     total_ms=40000, timeout_s=45.0)
```

**`verify`** (default `True`) — after `download()`, call `verify_schedule()`
and return `{"ok": False, "error": ...}` (without arming) if the table
didn't land correctly — never raises. Costs one extra round-trip; set
`False` only if you're firing rapidly and have already confirmed the link
is reliable.

```python
ct.fire_single_pulse(filament=0, verify=False)   # skip the post-download check
```

**`reuse`** (default `False`) — skip the (expensive, ~6-frame) `download()`
round-trip entirely and go straight to a cheap `verify_schedule()` check
(~2 frames, no data transfer) when **both** of these hold:

1. This exact plan (filament, `num_pulses`, `width_us`, timing config) is
   the last one **this client instance** successfully downloaded to this
   controller — a cheap, local, no-network pre-filter.
2. A **fresh table CRC** (the firmware-computed checksum from
   `ShvGetTableInfo`, read via `verify_schedule()`) still matches the CRC
   this client recorded right after its own last successful write.

That second check is real content verification, not just an entry-count
check — `verify_schedule()`'s own `"match"` field only compares counts (no
CRC in that logic), which would miss a *different* client overwriting the
table with a same-sized-but-different schedule. Comparing the CRC directly
closes that gap, without needing to know the RP2350's CRC algorithm — we
just remember what CRC *our own* write produced and check it's still there.

If either check fails, `reuse` falls back to a full `download()` +
`verify()` automatically and self-heals for next time — it never trades
correctness for speed, only skips work when it's confident. Measured
speedup on a repeat call with an unchanged plan: **~65× faster**
(download-bound calls took ~2–5 s in testing; a reused call took ~0.05 s).

```python
with ct.lease(ttl=60, note="repeated fire"):
    for i in range(20):
        r = ct.fire_single_pulse(filament=5, width_us=1000, reuse=True)
        print(i, r["ok"])
        # first call: full download (captures a CRC baseline). Every call
        # after: skips straight to a CRC check + arm, since the content on
        # the RP2350 is confirmed unchanged from what we last wrote.
```

**Still hold the lease anyway.** The CRC check protects you even without
one — it was specifically verified against another client downloading a
*different* same-entry-count schedule mid-sequence, and it correctly
detected the change and fell back to a full re-download. But the lease is
still the better first line of defense: it blocks other clients' writes
outright at the backend level, rather than relying on catching the change
after the fact. Use both.

**Return value** — a dict, always; never raises:

```python
# success:
{
    "ok": True,          # whether the fired-pulse log confirms this filament actually fired
    "fired": 1,           # count of matching pulse-log records
    "records": [...],     # [{"filament": 0, "seq": 0, "tOnUs": ..., "durationUs": 998, "flags": 0}]
    "status": {...},      # the final shv_status() snapshot (state, elapsedMs, totalPulsesDone, ...)
}
# failure (dead filament, download/verify/arm failure, SHV fault, or timeout)
# — "fired"/"records"/"status" are still present so you can destructure the
# same shape either way:
{
    "ok": False,
    "error": "...",      # human-readable reason
    "fired": 0, "records": [], "status": {},
    "dead": True,        # present only if the filament was in the dead mask
    "timeout": True,     # present only if the poll timed out
}
```

#### `trigger` — `"sim"` vs. `"ext"`

The RP2350 always fires on a **SyncIn** electrical edge — `fire_single_pulse`
downloads the schedule and arms it either way; `trigger` only controls
**where that edge comes from**.

**`trigger="sim"`** (the default) — after arming, the call additionally
tells the ESP32 firmware to generate the SyncIn pulse(s) itself (a
loopback/simulate path, no external wiring needed). Use this for bench
testing with nothing hooked up to the SyncIn line.

```python
result = ct.fire_single_pulse(filament=0, trigger="sim")
```

**`trigger="ext"`** — the call arms the schedule and then just **polls**
`shv_status()` until it completes; it does **not** generate any pulse
itself. A real electrical edge must appear on the RP2350's SyncIn hardware
pin from somewhere outside this API — typically the actual scan hardware
(a gantry rotary encoder pulse, a bench pulse/function generator, a manual
trigger button, or the chained `SyncOut` of an upstream controller).
Your script's job is only to arm in time and then wait; the physical
trigger is out of the Python API's control entirely.

```python
# Arm and wait for a real external SyncIn edge (e.g. someone presses a
# hardware trigger button, or an encoder pulse arrives from the gantry).
# fire_single_pulse blocks (polling) until the RP2350 reports COMPLETE,
# FAULT, or timeout_s elapses.
result = ct.fire_single_pulse(
    filament=0,
    trigger="ext",
    timeout_s=30.0,     # give the external source enough time to fire
)
print(result)  # {"ok": True, "fired": 1, "records": [...], "status": {...}}
```

If you need to confirm the schedule is armed and WAITING before the
external edge arrives (e.g. to signal a separate trigger system "go
ahead now"), build it with [`download()`](#schedule-download) — the real
transfer path, same as `fire_single_pulse` uses internally — instead of
`fire_single_pulse`'s all-in-one wrapper:

```python
import time

ct.shv_disarm(1)
plan = {
    "config": {"interPulseMs": 3000, "maxOnMs": 40, "totalMs": 30000,
               "triggerEdge": 0},   # 0=rising — see download() above
    "emission": [{"filament": 0, "numPulses": 1, "widthUs": 1000}],
    "heating": [],
}
ct.download(plan)
ct.verify_schedule(plan)
ct.shv_arm(1, repeats=1)

print("armed — waiting for external SyncIn edge")
# ... signal your external trigger system to fire now ...

deadline = time.monotonic() + 30.0
while time.monotonic() < deadline:
    st = ct.shv_status(1)
    if st["state"] == 3:      # COMPLETE
        print("fired:", ct.shv_pulse_log(1))
        break
    if st["state"] == 4:      # FAULT
        print("fault:", st)
        break
    time.sleep(0.05)
```

#### Controlling exactly *when* the simulated trigger fires

`fire_single_pulse(trigger="sim")` triggers immediately, right after
arming — you don't get to choose the moment. If you need to decide *when*
the pulse fires (a fixed delay, a sensor check, a keypress, coordination
with something else), split arming from triggering yourself: arm, do
whatever you need in between, then call
[`simulate_sync()`](#syncin-simulate-standalone) at the exact moment you
choose.

**What `simulate_sync()` actually does:** it tells the ESP32 firmware to
toggle its SyncIn output pin — an electrical edge wired directly into the
RP2350. If the RP2350 is armed, it's sitting in an interrupt handler
waiting for exactly that edge; the instant it sees it, the pulse fires.
It's a "generate one trigger edge right now" call, nothing more.

```python
import time
from ct_simple_control import CTClient

ct = CTClient()

# 1. Build the plan, then download + verify it (once)
plan = {
    "config": {"interPulseMs": 3000, "maxOnMs": 40, "totalMs": 60000,
               "triggerEdge": 0},   # 0=rising — see download() above
    "emission": [{"filament": 5, "numPulses": 1, "widthUs": 1000}],
    "heating": [],
}
ct.shv_disarm(1)
ct.download(plan)
ct.verify_schedule(plan)

# 2. Arm — RP2350 now WAITS for a SyncIn edge. Nothing fires yet.
ct.shv_arm(1, repeats=1)
print("armed — waiting. Nothing fires until we call simulate_sync().")

# 3. YOUR code decides when — a fixed delay here, but this could be a
#    sensor reading, a keypress, a condition check, anything:
time.sleep(2)
print("2 seconds elapsed — triggering now")

# 4. Trigger — THIS call is the exact moment the pulse fires
ct.simulate_sync(count=1, controller=1)

# 5. Poll for the result
deadline = time.monotonic() + 10
while time.monotonic() < deadline:
    st = ct.shv_status(1)
    if st["state"] in (3, 4):   # 3=complete, 4=fault
        print("done:", st)
        break
    time.sleep(0.05)

ct.stop_all()
```

**Low-level SHV ops** — raw single-command building blocks. **Prefer
[`download()`](#schedule-download) for loading an actual schedule** — these
bypass its retry logic and CRC verification, so hand-calling them on a
slow or lossy link is easy to get subtly wrong (a dropped frame here fails
silently). They're mainly useful for tweaking one thing (e.g. just the
timing config) without a full re-download, or for inspecting/clearing state:

**`shv_clear(controller=1)`** — Clear the schedule table on the RP2350.

```python
ct.shv_clear(controller=1)
```

**`shv_push_active_list(controller=1)`** — Push the power-slot → filament
mapping so the RP2350 knows which physical board each schedule entry
refers to.

```python
ct.shv_push_active_list(controller=1)
```

**`shv_set_entry(controller, filament, num_pulses=1, width_us=1000)`** —
Write ONE schedule entry directly (no retry, no CRC check). For anything
beyond a single quick tweak, use `download()` with a full `plan["emission"]`
list instead.

```python
ct.shv_set_entry(1, filament=5, num_pulses=1, width_us=1000)
```

**`shv_set_config(controller, inter_pulse_ms=3000, max_on_ms=40, total_ms=30000, trigger_edge=0)`**
— Update just the timing config without touching the emission/heat tables.
All four are enforced by the RP2350 itself, independent of this Python
process:

- **`inter_pulse_ms`** — a **runtime watchdog** while Armed/Running: if no
  new SyncIn trigger arrives within this many ms of the last one, the
  RP2350 faults the **whole schedule** (`shv_status()["stopReason"]` comes
  back `"inter-pulse timeout"`) — a late trigger isn't skipped or ignored,
  the run just stops. The 3000 ms default here is workable for a real
  trigger source; the library's own default of 30000 ms (30 s, see
  `fire_single_pulse`) is sized instead for slow *manual* bench triggers.
- **`max_on_ms`** — checked **once, at `shv_arm()` time**, against every
  entry already in the table: if *any* entry's `width_us` exceeds it,
  `arm()` itself is **rejected** (`reject` code `WidthTooLarge`, `2`) and
  **nothing fires at all** — it is not a runtime cutoff that would
  truncate a pulse already in flight. If arm keeps getting rejected,
  either shrink the offending entry's width or raise this.
- **`total_ms`** — the RP2350's own deadline for the whole armed
  schedule, arm to last pulse; exceeding it faults the run
  (`stopReason` `"total timeout"`) **independent of whether any Python
  process is even watching**. This is a completely separate clock from
  `fire_single_pulse`'s `timeout_s`, which lives in *this* process and
  only bounds how long *it* polls over HTTP — see `fire_single_pulse`'s
  docstring ("total_ms vs timeout_s") for the full two-clocks explanation
  and why `timeout_s` should be set a bit larger than `total_ms / 1000`.
- **`trigger_edge`** — `0` = rising, `1` = falling: which SyncIn edge
  fires the schedule. Match this to whatever actually drives SyncIn (the
  ESP32 bridge's own Sync I/O "ext edge" setting, or an external pulse
  source) — get this wrong and every real trigger is invisible to the
  engine, so you'll just sit Armed until `inter_pulse_ms` above faults the
  run with nothing having fired. `fire_single_pulse` hardcodes this to `0`
  (rising) and doesn't expose it — use `shv_set_config` directly if you
  need falling-edge.

```python
ct.shv_set_config(1, inter_pulse_ms=3000, max_on_ms=40, total_ms=15000, trigger_edge=0)
```

**`shv_arm(controller=1, repeats=1)`** — Arm the schedule. Returns
immediately; the RP2350 then waits for a SyncIn edge before actually
firing. Never raises: check `result["ok"]`; a rejection (e.g. a targeted
filament's ISO switch isn't enabled — idle/active it first) comes back as
`{"ok": False, "reject": <code>}`.

`repeats` is how many times the RP2350 **auto-loops the whole downloaded
table** before completing — seamlessly, on the firmware side: it does
**not** wait for a fresh external "start the next repeat" signal, SyncIn
triggers just keep driving individual pulses as usual and the engine
re-stages entry 0 once the table's last entry finishes. `repeats=0` fires
the table exactly once (same as `1`). `shv_status()["totalPulsesDone"]`
counts across **all** repeats, not per-loop — divide by the table's
per-loop pulse count if you need to know which repeat is currently running.

```python
r = ct.shv_arm(1, repeats=1)
if not r["ok"]:
    print(f"arm rejected, code {r.get('reject')}")
```

**`shv_disarm(controller=1)`** — Cancel an armed or running schedule.
Safe to call at any time, including when already idle.

```python
ct.shv_disarm(1)
```

**`shv_status(controller=1)`** — Read the current schedule state.

```python
st = ct.shv_status(1)
print(st)
# {"state": 2, "filamentIndex": 5, "totalPulsesDone": 1,
#  "totalPulsesTarget": 1, "elapsedMs": 42, ...}
# state: 0=idle 1=armed 2=running 3=complete 4=fault
```

**`shv_pulse_log(controller=1, start=0)`** — Fetch fired-pulse records
after a run, to confirm what actually happened on the hardware.

```python
log = ct.shv_pulse_log(1)
for rec in log:
    print(rec)   # {"filament": 5, "seq": 0, "tOnUs": ..., "durationUs": 998, "flags": 0}
```

Full manual sequence, equivalent to what `fire_single_pulse` does
internally (using [`download()`](#schedule-download) — the real transfer
path — not the raw `shv_set_entry`/`shv_set_config` calls above):

```python
ct.shv_disarm(1)
plan = {
    "config": {"interPulseMs": 3000, "maxOnMs": 40, "totalMs": 15000,
               "triggerEdge": 0},   # 0=rising — see download() above
    "emission": [{"filament": 0, "numPulses": 1, "widthUs": 1000}],
    "heating": [],
}
ct.download(plan)
ct.verify_schedule(plan)
ct.shv_arm(1, repeats=1)
ct.simulate_sync(count=1, controller=1)   # ESP32 generates the SyncIn edge
status = ct.shv_status(1)
log    = ct.shv_pulse_log(1)
ct.shv_disarm(1)
```

### SyncIn simulate (standalone)

`fire_single_pulse(trigger="sim")` uses this internally for a single burst —
these are the same three calls exposed directly, for when you want to start,
monitor, or stop a simulated SyncIn train independently of the high-level
wrapper (e.g. driving a longer sequence, or generating edges for a custom
`download()`-based sequence like the one just above).

**`simulate_sync(count=1, interval_ms=None, duration_s=None, controller=1, expect=None, active_ma=2900)`**
— Tell the ESP32 to generate `count` SyncIn pulses. Fires from the
head-of-chain controller; the RP2350 chain propagates the edge onward if a
second power unit is chained.

- Pass **either** `interval_ms` (fixed gap between pulses) **or**
  `duration_s` (spread `count` pulses evenly across that many seconds) —
  not both. Omit both and pulses fire back-to-back with no delay.
- `expect` (optional list of filament indices) and `active_ma` seed the
  run-report's expected-filament tracking, same as a real scan — leave them
  out for ad-hoc single-pulse testing.
- Never raises: if a simulation is already running, returns
  `{"ok": False, "error": "..."}` — call `simulate_sync_stop()` first.

```python
ct.simulate_sync(count=1, controller=1)                       # one pulse, no delay
ct.simulate_sync(count=5, interval_ms=200, controller=1)       # 5 pulses, 200 ms apart
ct.simulate_sync(count=10, duration_s=3.0, controller=1)       # 10 pulses spread over 3 s
```

**`simulate_sync_stop()`** — Stop an in-progress simulated train early.
Safe to call even when nothing is running.

```python
ct.simulate_sync_stop()
```

**`simulate_sync_status()`** — Poll progress while a simulated train runs.

```python
import time
ct.simulate_sync(count=20, interval_ms=500, controller=1)
while True:
    st = ct.simulate_sync_status()
    print(f"{st['fired']}/{st['count']} fired")
    if not st["running"]:
        break
    time.sleep(0.5)
```

### STM32 per-pulse HV current measurement

Everything above (`fire_single_pulse`, `hv_grid_set`, ...) controls **when
and which** HV switch fires. None of it tells you how much current
actually flowed — that's a completely separate measurement subsystem: the
STM32G431 sitting on the **master** controller's link runs a hardware
`pulse_detector` that measures every REAL rise/fall edge on the
emission-current line directly (no amplitude threshold, no guessed
timing) once armed. There is exactly **one** detector, on the master —
none of these methods take a `controller` argument, unlike every
board/HV method above.

**Shared with the GUI**: `pulse_arm()`/`pulse_disarm()` hit the *same*
backend endpoints as the GUI's "Stream" button and "Record measurement"
card, which the backend reference-counts — arming here while a GUI tab
already has Stream or Record running just **joins** that arm (your
`rate_hz` is ignored if you weren't the first arm-er); disarming here
only actually disarms the hardware once nothing else still wants it
armed. Safe to run this script alongside an open GUI tab.

**`pulse_arm(rate_hz=1000000)`** / **`pulse_disarm()`** — Arm/release the
detector. Arming doesn't measure anything by itself — it just gets the
STM32 ADC streaming and the edge-triggered detector waiting for a real
pulse.

```python
ct.pulse_arm(1000000)
# ... fire pulses some other way (e.g. hv_grid_set / the GUI) ...
ct.pulse_disarm()
```

**`pulse_events(since=0)`** — Poll new measured events (`id > since`).
Each event: `{"id", "t_us", "on_us", "peak", "plateau", "bg",
"bg_sigma4", "integral", "recv_ms"}` — `peak`/`plateau`/`bg` are **raw
ADC counts**, not mA; convert with `pulse_ma()`/`pulse_events_ma()`
below, never by hand. Persist the returned `last_id` and pass it back as
`since` to get only the delta next time. Pass a deliberately huge
`since` (e.g. `2_000_000_000`) to get zero events back but still learn
the *current* `last_id` — the same trick the GUI's own "Clear" button
uses to reset its cursor without walking the whole history.

**`get_ads1115_ref_mv()`** — The external differential circuit's LIVE
reference voltage (mV), off the ADS1115's "1.2V ref" channel. Nominally
~1.2 V, but it drifts board-to-board and with temperature — treating it
as a fixed constant is exactly what overstated measured current by ~35%
in the GUI's own history before this was fixed there. `pulse_ma()` needs
a live reading of this for an accurate conversion.

**`pulse_ma(raw, ref_mv=None)`** — Convert one raw ADC count to emission
current (mA). Formula: `V = raw*3.3/4095; Ie = 2*(V - 0.5*ref_v) /
R_sense / G_amc` (A→mA), with `R_sense=4.7 Ω`, `G_amc=8.2` (AMC3301's
fixed gain — both live as class constants, `_PULSE_R_SENSE_OHM`/
`_PULSE_AMC3301_GAIN`, change them there only) — the STM32's *own*
internal ADC scale, not an ESP32-ADC constant (those numbers belong to a
different chip entirely). Omitting `ref_mv` fetches one live reading itself
(an extra HTTP round trip) —
fine for one-off conversions, but fetch it once and reuse it for a batch
instead (see `pulse_events_ma()`).

**`pulse_events_ma(since=0)`** — Like `pulse_events()`, but every event
also gets `peak_ma`/`plateau_ma`/`bg_ma` fields, converted with ONE live
reference reading shared across the whole batch. Returns `{"ok",
"events": [...], "last_id", "ref_mv"}`.

```python
ct.pulse_arm(1000000)
since = ct.pulse_events(2_000_000_000)["last_id"]   # cursor, no history
ct.hv_grid_set(5, on=True); time.sleep(0.01); ct.hv_grid_set(5, on=False)
r = ct.pulse_events_ma(since)
for e in r["events"]:
    print(f"peak {e.get('peak_ma')} mA, plateau {e.get('plateau_ma')} mA "
          f"(ref {r['ref_mv']} mV)")
ct.pulse_disarm()
```

**`measure_pulse_current(filament, num_pulses=1, width_us=1000, rate_hz=1000000, **fire_single_pulse_kwargs)`**
— The convenience wrapper for the whole workflow above: arms the
detector, fires `num_pulses` on `filament` via `fire_single_pulse` (the
precise, PIO-timed schedule-engine path — see its own docstring for
every parameter besides `rate_hz`), correlates the detector's measured
events for exactly those pulses (polling with a short grace period past
when the fire completes — the detector reacts in real time, so this is
normally near-instant), and always releases its own claim on the
detector arm afterward.

Returns `{"ok", "fired": <fire_single_pulse's own result>, "measured":
[<pulse_events_ma() events for just this fire>], "ref_mv"}`. `"ok"` is
True only if BOTH the fire succeeded AND every fired pulse got a
matching measured event — a fire that "succeeds" with zero/partial
measured events (detector wasn't really armed, STM32 link dropped
mid-run) comes back `ok=False`, so a silent measurement gap can't look
like success.

```python
r = ct.measure_pulse_current(filament=5, num_pulses=3, width_us=1000)
print(ct.describe(r))
# Measured 3 pulse(s), peak mA: 5.83, 5.79, 5.85 (ref 1227.4 mV)
if not r["ok"]:
    print("fire or measurement failed:", r["fired"].get("error"))
```

### Human-readable results

Every method above returns a plain dict — convenient for scripting, but not
something you'd want to eyeball in a log.

**`describe(result)`** — Turn any result dict this client returns into one
short English sentence, for a `print()`/log line instead of dumping raw
JSON. Best-effort: it recognizes a result **shape** (which keys are
present), not which method produced it — so it works on a dict you've
stashed/reloaded too — and falls back to a short generic ok/error summary
for anything it doesn't recognize. Never raises.

```python
print(ct.describe(ct.active_one(5, current_ma=2900, verify=True)))
# "Filament 5 commanded to ACTIVE (2900 mA); verify: reached 2890.0 mA (target 2900.0 mA) in 1.4s."

print(ct.describe(ct.stop_one(7)))
# "Filament 7 commanded to STOP."

print(ct.describe(ct.hv_grid_set_all(on=True)))
# "Applied to 48 filament(s)."

print(ct.describe(ct.fire_single_pulse(5, num_pulses=3)))
# "Fired 3 pulse(s), schedule complete (stop reason: complete), 3 total pulses done, elapsed 118 ms."

print(ct.describe({"ok": False, "error": "controller not connected"}))
# "Failed: controller not connected."
```

### Exceptions

See [Error handling](#error-handling--read-this-first) at the top of this
doc for the full picture. Short version: **almost nothing here raises.**
Every method returns `{"ok": bool, "error": str, ...}` — check `"ok"`.

| Class | Raised by | When |
|---|---|---|
| `CTLeaseError` | `acquire_lease()` / `with ct.lease():` | Another client already holds the write lock. **The one deliberate exception** — see below. |
| `CTError` | nothing, by default | Base class, kept for compatibility. Not raised by any method here. |
| `CTConnectionError` | nothing, by default | Kept for compatibility. A connection failure now comes back as `{"ok": False, "connection_error": True, "error": ...}` instead of raising. |
| `CTTimeoutError` | nothing, by default | Kept for compatibility. `fire_single_pulse`'s poll timeout now comes back as `{"ok": False, "timeout": True, ...}` instead of raising. |

```python
from ct_simple_control import CTClient, CTLeaseError

ct = CTClient()

try:
    ct.acquire_lease(ttl=30, note="my test")
except CTLeaseError as e:
    print(f"someone else is driving: {e}")
    raise SystemExit(1)

# everything else: check "ok", don't try/except
r = ct.set_emission_v(30)
if not r["ok"]:
    print(f"HV set failed: {r['error']}")

r = ct.fire_single_pulse(filament=5, timeout_s=5)
if not r["ok"]:
    reason = "timed out" if r.get("timeout") else r.get("error")
    print(f"pulse did not complete: {reason}")

ct.release_lease()
```

For extra insurance beyond the non-raising design (e.g. protecting against
a bug in your *own* code, or a `Ctrl+C`), wrap your script in
[`with ct.session():`](#session--guaranteed-safe-teardown) — it guarantees
HV/heating gets torn down on the way out no matter what happens inside.

---

## Pre-heat workflow example

A loop over many filaments is exactly the case where a raising API would
be dangerous — one dead or momentarily-absent filament would otherwise
crash the whole run with HV still on. This example checks `"ok"` at every
step and keeps going; `with ct.session():` is the safety net underneath
that in case anything still goes wrong.

```python
from ct_simple_control import CTClient
import time

ct = CTClient("localhost", port=8770, client_id="preheat-script")

# Known-bad boards on this bench — silently skipped by every batch call,
# and every single-filament call below returns {"ok": False, "dead": True}
# for these instead of doing anything.
ct.set_dead([6, 26, 73])

IDLE_CURRENTS  = {i: 1500 for i in range(48)}  # mA per filament — typical idle hold
ACTIVE_CURRENT = 2900                           # mA for the firing filament

with ct.session():                              # guaranteed HV/heat teardown on exit
    with ct.lease(ttl=300, note="preheat run"):  # the one call that DOES raise

        # 1. Ramp these 48 filaments to idle (warm pool) — filaments= scopes
        #    the batch to exactly the keys in IDLE_CURRENTS, so nothing
        #    outside that set gets silently idled at the 0 mA default.
        #    Dead filaments in the set are silently skipped.
        r = ct.idle_all(filaments=list(IDLE_CURRENTS), currents=IDLE_CURRENTS)
        if not r["ok"]:
            print(f"idle_all reported failures: {r.get('failed')}")
        time.sleep(5)   # allow heating to settle

        # 2. Set HV setpoints (LUT-based, backend owns the calculation)
        ct.set_emission_v(30)    # −30 V
        ct.set_focus_v(150)      # −150 V
        ct.set_emission_i(10)    # 10 mA

        # 3. Enable HV
        ct.enable_emission(True)
        ct.enable_focus(True)

        # 4. Fire filaments one by one — every step checked, nothing raises,
        #    a bad filament just gets logged and the loop moves on
        for fil in range(48):
            r = ct.active_one(fil, ACTIVE_CURRENT)
            if not r["ok"]:
                print(f"fil {fil}: active_one failed ({r.get('error')}), skipping")
                continue
            time.sleep(0.1)   # allow active current to settle

            result = ct.fire_single_pulse(filament=fil, width_us=1000)
            if not result["ok"]:
                print(f"fil {fil}: fire failed — {result.get('error')}")
            else:
                dur = result['records'][0]['durationUs'] if result['records'] else '?'
                print(f"fil {fil}: fired={result['fired']} duration={dur} µs")

            ct.idle_one(fil, current_ma=1500)  # demote back to idle — single-filament API

        # 5. Tear down (also happens automatically via session() above,
        #    even on error — this is just the explicit happy path)
        ct.enable_emission(False)
        ct.enable_focus(False)
        ct.hv_grid_clear_all()   # instant hardware clear
        ct.stop_all()
```
