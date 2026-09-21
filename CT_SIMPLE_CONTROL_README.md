# ct_simple_control — CT Power Controller guide

A thin Python HTTP client for the CT power-controller backend. Third-party
programs import this module to drive pre-heat, HV setpoints, readback,
schedules, and HV pulses without touching the GUI.

## This is a guide, not the API reference

**The API reference is generated from the docstrings:**

```
tools/ct_gui/make_api_docs.sh      # -> docs/api/ct_simple_control.html
```

The docstrings are the reference — they carry the measured numbers, the failure
modes and the "do not do this" notes, and they are what you see in an editor
anyway. Keeping a second copy here meant the two drifting apart, so this
document sticks to what a reference cannot give you: which calls belong
together, which order to make them in, and the traps that only show up when you
put them in sequence.

The generated output is not committed (it is derived and it churns); run the
script.

## Requirements

```
pip install requests      # runtime
pip install pdoc          # only to regenerate the API reference
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

## Runnable examples

Before reading further: `examples/walkthrough.py` is this whole API as ten
runnable, individually selectable sections, verified against the bench.

```bash
python3 examples/walkthrough.py --list                    # what the sections are
python3 examples/walkthrough.py                           # READ-ONLY sections
python3 examples/walkthrough.py --only 8                  # just the schedule builder
python3 examples/walkthrough.py --energise -f 27          # + heating/measurement
python3 examples/walkthrough.py --energise --fire -f 27   # + firing pulses
```

Nothing energises without `--energise`, nothing fires without `--fire`, and a
section whose permission is missing is skipped loudly rather than silently
downgraded. See [`examples/README.md`](examples/README.md).

Hardware bring-up and reliability scripts live in
[`tests/`](tests/README.md) — those are bench tests against real hardware, not
a unit-test suite.

## Quick start

```python
from ct_simple_control import CTClient

ct = CTClient("localhost", port=8770, client_id="my-script")

# Connect controller 1 to the ESP32 if the GUI hasn't already
if not ct.status().get("controllers", {}).get("1", {}).get("connected"):
    r = ct.connect(1, "192.168.50.173")   # the ESP32's own IP
    if not r["ok"]:
        print(f"connect failed: {r['error']}")

# ── Dead filaments ───────────────────────────────────────────────────────
# A "dead" filament is one that MUST NOT BE ENERGISED — e.g. a burnt-out
# emitter or a known-bad HV switch. The board it sits on may be perfectly
# fine; the filament is the faulty part.
#
# It lives in the BACKEND, so it outlives your script and applies to every
# client — the GUI and a bare curl are stopped by it too, not just scripts
# that remember to filter. Nothing clears an entry automatically, and in
# particular not a board dropping out of presence: presence is about the
# board, this is about the filament. Repaired one? remove_dead() — it is
# meant to be changeable, just rarely changed.
# Once marked, EVERY batch call below (stop_all,
# sleep_all, standby_all, idle_all, active_all, hv_grid_set_all, ...)
# silently skips it — you never have to remember to exclude it yourself.
# Any call that targets ONE specific filament (active_one, idle_one,
# fire_single_pulse, hv_grid_set, ...) returns {"ok": False, "dead": True}
# immediately instead — it does NOT raise (see "Error handling" above) — so
# a script iterating every filament in a loop just skips it and moves on.
ct.set_dead([6, 26, 73], reason="burnt emitters, 2026-09 bench")

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
    result = ct.fire_single_pulse(filament=0, num_pulses=1, width_us=1000)
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
if not s.get("controllers", {}).get("1", {}).get("connected"):
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
# Everything not physically present, marked as do-not-energise. NOTE this is
# YOU deciding to disable those slots, not an automatic link: the entries
# persist and will NOT clear themselves when a board is re-seated, because
# dead is about the filament and presence is about the board. Re-seated a
# board? remove_dead() those indices.
ct.set_dead(set(range(96)) - present,
            reason="no board present at scan time")
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

**`set_filament_order(order)`** — Define the mapping, explicitly.

`order` is a sequence of **exactly 96** integers: `order[i]` is the physical
filament that your logical filament `i` refers to. The whole table is stated,
not a diff.

```python
order = CTClient.identity_order()     # [0, 1, 2, ..., 95]
order[5], order[8] = 8, 5             # state BOTH directions yourself
ct.set_filament_order(order)

ct.active_one(5, current_ma=2900)      # actually commands physical filament 8
ct.active_one(8, current_ma=2900)      # ...and this one commands physical 5
ct.read_filament_current(5)            # actually reads physical filament 8,
                                        # returned to you keyed as "filament 5"
ct.fire_single_pulse(filament=5)       # fires physical filament 8
```

**It must be one-to-one, and that is checked.** Every filament `0..95` has to
appear exactly once. The mapping has to be reversible: this client translates
your indices to physical ones on the way out and back to yours on the way in,
and that round trip is only unambiguous if no two logical filaments claim the
same physical board. A `ValueError` names the offending entries:

```python
bad = CTClient.identity_order()
bad[5] = 8                     # one-way: physical 8 now claimed by 5 AND by 8
ct.set_filament_order(bad)
# ValueError: mapping is not one-to-one — physical 8 claimed by both
#             logical 5 and 8. Every filament 0..95 must appear exactly once...
```

Wrong length, a value outside `0..95`, and duplicates are each rejected with
the specific entries listed. Any permutation is legal, not just pairwise
swaps — a 3-cycle (`1→2→3→1`) is one-to-one and accepted.

> **Partial mappings are no longer accepted.** The old dict form (`{5: 8}`,
> applied in both directions for you) is rejected with a message pointing at
> the list form. That convenience is exactly what made a table uncheckable:
> the entries you left out are the ones a conflict would hide. Passing a dict
> now raises; passing `None` or `[]` still clears to identity.

If only ONE of the two filaments is really misplaced, this is the wrong tool —
you would be remapping a second filament that was fine, and on the HV path
that means energising a board you didn't name. Fix the active-list
[mapping](#active-list-mapping) instead; that's the layer that describes which
physical board a filament index means.

Pass `None` (or `[]`, or `{}`) to clear back to identity:

```python
ct.set_filament_order(None)
```

**`get_filament_order()`** — the current mapping as an explicit 96-entry list.

```python
print(ct.get_filament_order())   # [0, 1, 2, 3, 4, 8, 6, 7, 5, 9, ...]
```

It always round-trips: whatever this returns is accepted by
`set_filament_order()` unchanged, and it is the identity list when no
remapping is set.

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
(`set_dead`/`add_dead`/`remove_dead`) always operates in **your own USER_INDEX
numbering**, independent of any swap.

```python
ct.set_filament_order({5: 8})
ct.set_dead([6], reason="...")   # blocks YOUR filament 6; stored as its FID

# Verify the round-trip:
board = ct.filament_to_board(5)              # physical 8's board location
back  = ct.board_to_filament(board["controller"], board["channel"], board["position"])
print(back)   # 5 -- reverse-translated back to your logical number
```

#### The backend holds it, and forgets it on restart

The order used to be a CTClient attribute, so every script silently started at
identity. The **backend** holds it now: a new client inherits whatever the last
one set, for as long as the backend stays up.

**Deliberately not persisted to disk**, unlike the dead mask. The two look
alike and must not be treated alike — `dead` is a property of the HARDWARE (a
burnt filament is still burnt after a restart), while the order is a property
of a SESSION's convention. Reloading a stale permutation into a rig whose
backplane has since been rewired sends every command to the wrong filament, and
nothing announces it: every index stays in range and every call succeeds.
Identity is the only honest default for a backend that just started.

The backend only REMEMBERS it — every endpoint still speaks FID, and this
client still does the crossing.

**Your snapshot does not move.** The client reads the order once and does not
re-poll. Every index it sends or receives is crossed through that table, so a
table that changed halfway through a loop would issue some commands under the
old lens and some under the new, with nothing in any result saying which.

- **`reload_filament_order()`** — deliberately re-sync, at a point you know no
  partially-issued operation is in flight.
- **`filament_order_status()`** — compare your snapshot against the backend's
  live order. `backend_restarted=True` means the backend forgot (memory only,
  by design): this client stays self-consistent, but the NEXT script to start
  gets identity, so re-apply if the order still reflects the hardware. The
  epoch it compares changes on every backend start, so "the backend forgot" is
  **detected**, not inferred from the order happening to look like identity —
  which is also what a deliberate clear looks like.

Validation runs on both sides. A rejected write leaves your local table alone:
continuing under a mapping the backend does not have is how two scripts end up
disagreeing about which filament is which.

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
for i in range(96):          # filaments are 0-95
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

#### What expires on its own, and what it actually does

Three things here self-expire so a killed script cannot wedge the bench. It is
worth knowing exactly what each one does on expiry, because none of them does
what people usually assume:

| mechanism | where | default | on expiry |
|---|---|---|---|
| **Lease** (`/api/lock`) | backend | 30 s (max 600) | **Releases the write lock. Sends no hardware command.** |
| **`ready_relay` arm TTL** | ESP32 firmware | 60 s (`kDefaultArmTtlMs`) | `disarm()` — stops relaying the pulse envelope, releases the STM32 CS claim |
| **poll-pause** | backend | 15 s | Background PING polling **resumes** |

**None of them de-energises a filament.** There is no watchdog anywhere that
turns heating off — which is exactly why a killed script can leave a filament
powered (see the `energised()` warning under *Waiting on state*). Restating
because it cuts both ways: an expiry can never *interrupt* your heating, and it
can never *protect* you either.

Read the failure direction of each before worrying about it:

- **Lease** — the risk is the opposite of "it turned my stuff off": if a long
  operation stops renewing, another client may begin writing while you are
  mid-run. Reads are never gated at all (every `GET`/`READ` command, plus
  presence/diagnosis/verify-schedule, stay allowed while someone holds it — a
  lease reserves the right to *change* the hardware, not to *look* at it). Use
  `with ct.lease(ttl=...)`, or `renew_lease()` inside a long loop.
- **`ready_relay` TTL** — the only one that actively does something, and its
  blast radius is just *pulse measurement*: it does not fire, power, or stop
  anything, so the worst case is a later pulse going unmeasured. It exists
  because an abandoned arm makes **every subsequent arm fail** with "already
  armed" — observed after an interrupted script. For a run legitimately longer
  than the TTL, pass a larger `ttl_ms` or call `ready_renew()`. Check
  `ready_status()["ttl_expiries"]`: non-zero means somebody's cleanup is not
  running.
- **poll-pause** — fails safe in the direction of doing *more* work, not less;
  at worst background pings resume during a long operation.

A backend restart additionally clears `LAST_POWER_STATE`, so
[`thermal_history()`](#resistance-is-meaningless-without-a-temperature) reports
every filament as **unknown** (not cold) until something is commanded again. The
dead mask is persisted to disk and survives.

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
ct.set_dead([3, 7, 12, 55], reason="burnt emitters")  # replace the whole mask
ct.add_dead(20, 21, reason="HV switch stuck closed")  # add individual ones
ct.remove_dead(7)                                     # repaired — un-block it
ct.dead                        # frozenset of YOUR indices (backend-held)
ct.dead_details()              # ... with {reason, by, at} for each
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
| Soft per-board failure | returned in `"failed": [...]`, **and the batch `"ok"` goes `False`** | returned as `{"ok": False, ...}`, no exception |
| Dead filament in the target set | skipped, and listed in `"dead_skipped": [...]` | `stop_one`/etc. return `{"ok": False, "dead": True, ...}` immediately, no exception |

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
get REAL feedback, merged under `result["heating"]`.

#### What `verify=True` actually checks

`verify` used to compare host-polled current against the target. That was the
wrong place for the judgement and produced false successes: commanding
`idle_one(f, 1500)` on a cold filament, the inrush passes **down** through
1569 mA within 0.4 s, so the comparison declared arrival while the loop was
still ramping. The CC loop sees every sample; this host sees one every
50-100 ms across a shared link.

The firmware now reports its own verdict, and `verify` uses that. Four outcomes,
each meaning something different:

| `result["heating"]` | meaning | what to do |
|---|---|---|
| `ok: True`, `arrival: "settled"` | the loop reports it arrived and is stable | proceed |
| `cannot_start: True` | the firmware cannot bring the output up at all | **check the load — a SHORT looks exactly like this** |
| `capped: True`, `arrival: "capped"` | pinned at the voltage cap with the target unreached | raise the cap or change the load; waiting cannot help |
| `faulted: True` | open filament, or a real OCP, confirmed over consecutive reads | investigate the board |
| `ok: False` with none of the above | still ramping when the timeout expired | give it longer — see the timings below |

Three of those could not be told apart before: everything that was not success
came back as a generic "did not reach target" after burning the full timeout.

> ⚠️ **`cannot_start` is the only signal that catches a short.** A shorted board
> never sets the fault bits — those need `feedbackMv >= 2000` and a short cannot
> reach 2 V — and its arrival bits read `"ramping"` indefinitely. Measured on a
> deliberately shorted board: mode 1, arrival `"ramping"`, 1 mA, for minutes.
> It comes from the firmware's `struggling` flag (3 consecutive failed revives
> of a collapsed output), which is checked from 2 s into the wait — the flag
> latches while the output stays down, so checking at t=0 would refuse a
> repaired load forever.

> ⚠️ **A startup OCP that recovers is NORMAL and is not reported as a fault.**
> A board can trip on the inrush and come up on the next revive; the fault bits
> can flash while that happens. `faulted` requires three consecutive reads, so a
> transient does not end the wait.

#### Timings, and one trap that will cost you 20%

Measured on a simulated filament load, so treat them as shape rather than spec:

| | time |
|---|---|
| cold → IDLE 1500 mA settled | ~14 s |
| settled IDLE → ACTIVE 2950 mA | ~3.0 s |
| cold → ACTIVE directly | **refused — see below** |

`idle_one`'s default `timeout_s=5.0` is **not enough for a cold start**. Pass
`timeout_s=30`.

> ⚠️ **Do not watch a ramp with live V/I reads.** `read_filament_vi_live` is a
> live INA219 read over the same I2C the CC loop uses to rewrite its setpoint,
> so polling it *slows the ramp you are measuring*. Measured on the same ramp:
> 3.61 s while polling live at 4 Hz, 3.28 s polling the zero-I2C cache, **3.0 s
> not polling at all** — a 20% observer effect. Use
> `read_filament_current_cached`, or command, sleep, and read once.

#### ACTIVE may only be entered from IDLE

`active_one` is refused unless the filament is **already settled at IDLE**.
Going straight to firing current damages the filament, and a filament that fails
inside the vacuum cannot be repaired. Measured on a simulated load, ACTIVE from
cold produced 2067 mA of inrush and then collapsed the output to 0 V for ~10 s
before the firmware revived it.

Refused with `{"ok": False, "ladder_blocked": True, "error": ...}` when the
filament is not at IDLE, when it was *commanded* to IDLE but the loop reports it
never got there, or when this backend does not KNOW its state (after a
reconnect — unknown refuses rather than allows). The RP2350 firmware refuses the
transition too, so this is a second line rather than the only one.

ACTIVE below **1500 mA** (the idle operating current) is also refused, on the
live paths and inside a downloaded schedule's heating deltas alike — promoting
to ACTIVE must not lower the current. IDLE is separately clamped to 2 A by the
firmware, and schedules may not use VOLTAGE mode at all.

    ct.sleep_one(f); ct.standby_one(f)
    ct.idle_one(f, 1500, verify=True, timeout_s=30)
    ct.active_one(f, 2950, verify=True)

Wrap anything that heats in `ct.energised()` so a crash cannot leave it on:

```python
with ct.energised(7):
    ct.idle_one(7, 1500, verify=True, timeout_s=30)
    ct.active_one(7, 2950, verify=True)
    ...                      # an exception here still STOPs filament 7
```

It covers a normal return, an exception, and Ctrl-C. It does **not** cover the
process being killed outright — nothing does yet.

**`stop_one(filament, verify=False, timeout_s=5.0)`** /
**`sleep_one(filament, verify=False, timeout_s=5.0)`** /
**`standby_one(filament, verify=False, timeout_s=5.0)`**
— Set one filament to STOP / SLEEP / STANDBY. Never raises: returns
`{"ok": False, "dead": True, ...}` if the filament is dead, or
`{"ok": False, "error": ...}` if it has no board mapping or the board
simply didn't ACK (e.g. temporarily unseated) — always check `"ok"`.
For `stop_one`/`sleep_one`, `verify=True` polls until the measured current
drops to ~0 mA (confirms it actually stopped heating).

`standby_one(verify=True)` is different: STANDBY has **no current target** — it
holds the firmware's 0.8 V floor and draws whatever the filament's resistance
allows (measured: 2.1 A of inrush decaying to ~885 mA by ~5 s at 0.78 V). So it
confirms the board actually reports STANDBY and reports the current as an
observation, not a pass/fail, under `result["standby"]`. Wait ~5 s before
calling a STANDBY current abnormal.

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
#  "measured_valid": True, "elapsed_s": 1.4, "present": True, "cc_mode": 1}
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
#  "measured_valid": bool, "elapsed_s": ..., "present": bool, "cc_mode": int}
```

`measured_valid` is `False` when the board never returned a live measurement
(the RP2350 flags its cached current as stale). `measured_ma` is then `0.0`
filler, and `ok` is forced `False` — so a stale reading can't masquerade as a
real 0 mA and make `stop_one(verify=True)` report a success it never saw.

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

### Slew rates

**`get_slew_rates(controller=1)`** / **`set_slew_rates(below, above, warm,
controller=1)`** — how fast the CC loop may ramp the output, in mV/s. Three
bands: `below`/`above` are the COLD-start rates, `warm` applies once the
filament is hot.

⚠ **The ceilings are NOT the defaults.**

| | below | above | warm |
|---|---|---|---|
| `SLEW_DEFAULTS` | 400 | 1000 | 2800 |
| `SLEW_CEILINGS` | 2000 | 5000 | 5000 |

Cold slew sits far below its ceiling deliberately — fast cold ramping is what
trips OCP on the cold inrush. "Resetting to defaults" by writing the ceilings
once made cold starts 5×/10× faster. To restore: `ct.set_slew_rates(
**CTClient.SLEW_DEFAULTS)`.

Below 400 mV/s is **clamped up** to it and the clamp is reported in
`floored_to_min`. The firmware's own floor is 1 mV/s and it installs that
silently, which is not "unlimited" but "ramp 10 V in about three hours" — a
`set_slew_rates(0, 0, 0)` returned ok=True and left this bench at 1/1/1 until a
readback caught it. Above the ceilings it still clamps, reported separately via
`clamped`, so the two causes never merge.

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
if r.get("ok") and not r["enabled"]:   # no "enabled" key on a failed read
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

> ⚠️ **This is the CONTINUOUS DC path, and it is an alternative to pulsing —
> not a step before it.** Closing this switch leaves HV routed to the board
> until you open it again. `fire_single_pulse` drives the *same* switch as a
> brief scheduled pulse and must own it for the duration, so the grid has to be
> **off** when you pulse. Either route DC with `hv_grid_set` and read the
> steady-state current, or leave the grid off and fire a pulse — never both on
> the same filament. `fire_single_pulse` refuses (and fires nothing) if the
> target's grid switch is already closed.

**As with every batch call, dead-masked filaments are always excluded** —
`hv_grid_set` returns `{"ok": False, "dead": True, ...}` immediately if the
target is dead (never raises), and `hv_grid_set_all` / `hv_grid_off_all`
silently strip dead filaments from the batch before sending the request.

```python
ct.set_dead([3, 7, 12], reason="damaged emitters")   # must not be energised

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

**It also DISARMS the schedule engine.** The call POSTs `/api/disarm`,
which sends `SHV_DISARM` to every connected controller — so calling it
during an armed or running schedule kills that run, not just the grid
outputs. That is what you want from an emergency stop, but it means this
is not a "clear the grid and carry on" operation. `session()` teardown
uses it for exactly this reason.

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
#  "faultedSlots": [...], "faultedFilaments": [...]}   # your USER_INDEX numbering
```

> ⚠️ **On a bench with unpopulated slots you almost certainly want
> `board=1`.** The default (`board=0`, stop) ends the whole run at the FIRST
> filament that faults, and an empty slot promoted to ACTIVE faults by
> definition — it is an open circuit. Measured on a 16-pulse scan with 14 empty
> slots: `done=2/16`, `stopReason=5` (Fault), `faultFilament=3`. The same
> schedule with `board=1` completed `16/16` and recorded the offenders in
> `faultedFilaments`.
>
> This only bites when a heating delta promotes a filament **during** the run —
> a schedule whose deltas all sit at trigger 0 never exposes it, which is why
> a sliding-window plan can fail where a simpler one passes.
>
> An open circuit does not fault instantly: `FaultOpen` needs the output voltage
> to climb past ~2 V with no current, so each board gets there at its own time
> (measured on six empty slots at IDLE 1500 mA: two still healthy at 4 s, all
> six faulted by 10 s). So a single snapshot showing only *some* slots faulted
> is a timing artefact, not a difference between the slots.

> ⚠️ **`faultedFilaments` is CUMULATIVE, not per-run.** `arm` does not clear it,
> unlike `mismatches`/`uncounted`/`underfed`/`triggerEdges`, which it zeroes. A
> clean run leaves the previous run's entries in place (verified: the list was
> identical before and after a run with no faults), so it answers "which
> filaments have ever faulted since the controller came up", not "which faulted
> this run". `faultFilament` in `shv_status` is the single filament that stopped
> the current run — a different question again.

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

> **Which of these two do I want?** They are not a currents/voltages pair —
> they are two different firmware commands with different costs, and the names
> now say so. Prefer the capability-led names; the old ones still work.
>
> | need | call | voltage? | safe while a schedule fires? |
> |---|---|---|---|
> | a current, cheaply | `read_filament_current_cached()` | no — always `None` | **yes** (no I2C) |
> | a voltage, or a live V+I pair | `read_filament_vi_live()` | yes | **no** (does an I2C mux sweep) |
>
> `read_filament_currents()` and `read_filament_voltages()` are kept as aliases
> for the two above, so existing scripts are unchanged. The old names implied a
> symmetry that does not exist: the cached read has no voltage and never will,
> because there is no voltage field in its firmware response.

**`read_filament_current_cached(filaments=None)`** (alias:
`read_filament_currents`) — Measured heating current for
one filament or many, same call. **Current only**, and safe to poll mid-run.

```python
all_currents = ct.read_filament_currents()          # every populated filament
for fil, c in all_currents.items():
    print(f"fil {fil}: {c['current_mA']} mA (target {c['target_mA']} mA, "
          f"present={c['present']})")

subset = ct.read_filament_currents([0, 1, 2])       # a few
one    = ct.read_filament_currents(5)               # one — bare int is fine
one    = ct.read_filament_currents([5])             # identical to the above
```

**Single and bulk are different firmware commands, not one read filtered two
ways.** Ask for exactly one filament and it goes out as a single small frame
(`CH_GET_CACHED_CURRENTS` `FLAG_SINGLE`) to only that filament's controller —
that's what makes a per-filament poll cheap. Ask for several (or all) and you
get the paged bulk sweep across every used channel, because looping the
single read over many boards would flood the one shared bridge link. The
return shape is identical either way, so you never branch on which one ran.

Each entry: `{"current_mA", "target_mA", "present", "cc_mode"}`.
`cc_mode`: `0`=voltage, `1`=current (Idle/Active regulating), `2`/`3`=fault.
`current_mA` is `None` — not `0` — when the board's cached reading isn't a
live measurement yet; guard it with `is not None` rather than treating it as
0 mA. `present` is inferred from whether the CC loop is actively regulating
that board — not a live I2C presence scan (see
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

**`read_filament_vi_live(filaments=None)`** (alias: `read_filament_voltages`)
— Bulk read of every populated
filament's measured board voltage (mV) **and** current (mA) together. **The only
source of a voltage**, and **not** safe to poll while a schedule is firing.
Unlike `read_filament_currents()` above (CC-loop cached current, no I2C,
always safe to poll), this is a real INA219 **I2C sweep** — it requests
`/api/telemetry?live=1`, because the plain telemetry read is the no-I2C
cached one and carries **no bus voltage at all**. The backend refuses the
sweep while a schedule is firing (I2C would stall pulses) and falls back to
the cached read for that window: mid-run, entries come back with
`"bus_mV": 0` and `"cached": True` — voltage genuinely isn't available
then; call again once the run completes.

```python
v = ct.read_filament_voltages()
for fil, row in v.items():
    print(f"fil {fil}: {row['bus_mV']} mV, {row['current_mA']} mA "
          f"(present={row['present']}, cached={row.get('cached', False)})")
```

**Reading one filament's V and I together** — take both from a single
`read_filament_vi_live()` entry. It accepts a bare int:

```python
d = ct.read_filament_vi_live(7)[7]
# {"index": 7, "present": True, "bus_mV": 1048.0, "current_mA": 1073.0,
#  "target_mA": None, "cc_mode": None, "source": "live", "valid": True}
if d["valid"]:
    print(f"R = {d['bus_mV'] / d['current_mA']:.3f} ohm")
```

⚠️ **Do not build a V/I pair by calling `read_filament_voltage()` and
`read_filament_current()` together.** They deliberately read different
sources — the voltage from the live INA219 (the only command that has one),
the current from the CC-loop cache — so you get two numbers from two commands
sampled at two different instants. While a filament's resistance is still
moving with temperature that is a real difference, not jitter: measured 92 mA
apart mid-ramp, against 1–7 mA once settled. One `read_filament_vi_live()`
entry gives you both from one INA219 conversion, so `bus_mV / current_mA` is a
meaningful resistance.

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

### Building a schedule

Nothing produced an emission table before these existed — a script either
hand-wrote the rows or read `buildSchedule`/`stepScan` out of `app.js`.

```python
sch  = ct.build_scan_schedule(mode="stationary", collimator_center=0)
plan = ct.build_scan_plan(sch["emission"], active_count=35, rotation_ms=360000)
v    = ct.validate_plan(plan, rotation_ms=360000, t_settle_ms=2000)
print(ct.gantt(plan, width=72))
```

**`build_scan_schedule(...)`** — ring geometry → emission rows. A faithful port
of the GUI's `buildSchedule`/`stepScan`, verified field-by-field against the
original JS under node across four parameter sets.

⚠ `ring_order` maps a RING POSITION to a filament. It is **not** the client's
`filament_order` (USER_INDEX → FID), which is applied later, on the wire.
Conflating them silently reorders the scan geometry while every index still
looks valid.

⚠ `truncated` is returned, not left implicit. Precision mode generates roughly
`N × (2·steps+1) × coverage` rows, so hitting the firmware's 8192 cap is the
**normal** case there — and a scan that quietly ends at ring step 21 of 96
reads exactly like one that ran fine.

**`build_scan_plan(emission, active_count, rotation_ms)`** — emission rows →
heating deltas + timing config, holding `active_count` filaments hot at once.

**`validate_plan(plan, rotation_ms, t_settle_ms)`** — is the pre-heat lead long
enough? This is the public view of the plan's timing: read `lead_ms`,
`lead_triggers` and `peak_active` from here, **not** from the plan dict's own
`_lead_triggers`/`_peak_active`, which are underscore-prefixed internals.

**`gantt(plan, width=72)`** — a text overlap chart. **`heating_windows(plan)`**
and **`is_active_at(plan, filament, trigger)`** answer "was F7 hot when trigger
42 fired?" without re-deriving the plan.

All four refuse a plan with no emission rows. An empty plan makes every count
compare 0 against 0, so `validate_plan`, `verify_schedule` and `download` all
used to pass vacuously — and `verify_schedule` runs immediately before arming,
so "nothing to check" was being reported as "checked".

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
itself polls before giving up and returning
`{"ok": False, "timeout": True, ...}` (it does **not** raise — see the
exceptions table). This is a
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
    if st.get("state") == 3:  # COMPLETE (shv_status returns {} on a failed read)
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
    if st.get("state") in (3, 4):   # 3=complete, 4=fault ({} on a failed read)
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
  trigger source; the 30000 ms (30 s) default of `shv_set_config`'s **`total_ms`** is sized
  instead for slow *manual* bench triggers. (`fire_single_pulse`'s own
  `inter_pulse_ms` default is 3000, same as here — not 30000.)
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

Diagnostics worth reading after a run. All are **per run** — `arm` zeroes them,
so two identical runs both reporting 1 is not accumulation:

| field | meaning |
|---|---|
| `triggerEdges` | trigger edges the firmware counted |
| `uncounted` | pulses the PIO fired but the edge-count ISR missed. **Exact only when `rbSaturated == 0`**, otherwise a lower bound |
| `underfed` | triggers that arrived with nothing staged |
| `mismatches` | pulses whose 165 read-back disagreed with the commanded byte — **see the warning below** |
| `rbSaturated` | times the 165 read-back FIFO was full when a pulse was accounted |
| `off_mismatches` | pulses whose OFF read-back was non-zero — **the HV did not turn off**. The per-run count of the pulse log's `hv_stuck_on` |
| `unsafeSlots` | bitmap of power slots `arm` SKIPPED as unsafe. **Non-zero means those filaments did not fire even though the run looks normal** |

Two more, and they are **cumulative** rather than per-run — see the warning below:

| field | meaning |
|---|---|
| `rbDropped` | the read-back ring OVERRAN. Real data loss, the CPU fell behind |
| `rbStale` | samples discarded because their pulse was already reported unverified — the pairing **recovering as designed**, about two per unverified pulse |

`rbIrqs` (per-run, like the first group) counts the pulses the **hardware**
actually fired:

> ✅ **The invariant worth asserting after a run is `totalPulsesDone == rbIrqs`**
> — the host's bookkeeping agreeing with what the hardware did.
>
> It is **not** `done == triggerEdges == rbIrqs`. `triggerEdges` counts edges the
> ISR *saw*, and on the real pin-trigger path an edge arriving mid-pulse is
> counted and fires nothing. So **`triggerEdges >= done` is normal** and means
> the trigger source is running faster than width + recovery — not that pulses
> were lost.
>
> ⚠️ **And that excess cannot be seen from here.** `simulate_sync()` uses the
> simulated-trigger path, which discards an edge arriving mid-pulse *before*
> counting it. Equal `done`/`triggerEdges` out of a simulated run is therefore
> not evidence that a real pin-triggered run would be equal too — the same
> over-triggering reports differently depending on which trigger path fired it.

> ⚠️ **`rbStale > 0` with `rbDropped == 0` is a HEALTHY run**, not a fault. It is
> the recovery mechanism working: a late pulse costs one `unverified` pulse and
> the stale samples behind it are thrown away instead of being handed to the
> next pulse. Only `rbDropped` is data loss.

> ⚠️ **Not every counter resets on `arm`.** `triggerEdges` / `uncounted` /
> `underfed` / `mismatches` / `off_mismatches` are **per-run** — `arm` zeroes
> them. `rbStale`, `rbDropped` and `faultedFilaments` are **cumulative** and
> survive it. Take a reading before and after and use the delta: dividing a
> cumulative `rbStale` by one run's `unverified` count produces a ratio that
> grows every run and looks exactly like a defect (measured 4.7–6.7 that way,
> versus the ~2.0 the deltas actually give).

> ⚠️ **A non-zero `mismatches` is a real verify failure** — a pulse whose 165
> read-back did not match the byte that was commanded. There is exactly ONE
> read-back per pulse (post-ON, mid-pulse), so there is no second read to fall
> back on.
>
> It briefly meant something else: for one firmware revision a cross-channel
> schedule reported one mismatch per channel boundary, because the channel
> select moved before the read-back had been taken. That is fixed at the source,
> and a cross-channel table now reads 0 (measured: 0 / 3 / 15 crossings, all 0).
> If you ever see `mismatches` tracking the number of channel boundaries again,
> that is the regression — not a property of crossing channels.

**`shv_pulse_log(controller=1, start=0)`** — Fetch fired-pulse records
after a run, to confirm what actually happened on the hardware.

```python
log = ct.shv_pulse_log(1)
for rec in log:
    print(rec)
# {"filament": 5, "seq": 0, "tOnUs": ..., "durationUs": 998, "flags": 0,
#  "read165": 32, "on_mismatch": False, "hv_stuck_on": False, "unverified": False}
```

`flags` is a bitfield, decoded into the three booleans above. They are not
equally serious:

| bit | field | meaning |
|---|---|---|
| `0x01` | `on_mismatch` | the ON read-back did not equal the commanded byte |
| `0x02` | **`hv_stuck_on`** | **the OFF read-back was non-zero — the HV did not turn off** |
| `0x04` | `unverified` | a read-back was unavailable: the pulse fired, the firmware has no evidence either way |

> ⚠️ **`hv_stuck_on` is the one that is about the PULSE.** The other two are
> about the *verification* of it. A non-zero OFF read-back means the grid switch
> may still be closed with HV on the filament after the pulse, so
> `fire_single_pulse` fails on it and names the filament. It was invisible
> before: `flags` was a raw byte nobody decoded, so this could occur and be
> reported as a fully successful shot.

`unverified` does **not** clear `ok` — the pulse fired, and the firmware's own
mismatch counter deliberately skips it. It is reported separately so you can
tell "verified good" from "no evidence", which `ok` alone cannot express.

`read165` is the 165 read-back (expected value: `1 << position` for that
filament). It is `None`, not a number, when the read-back was unavailable — the
firmware writes a `0xEE` sentinel there and its bits mean nothing.

#### The heating current at the moment each pulse fired

Every pulse record carries `heat_meas_mA` (what the filament was actually
drawing) and `heat_target_mA` (what it had been commanded to hold), sampled by
the firmware **at the instant the pulse fired**.

This is what makes an emission reading interpretable. A shot on a filament that
had not reached current is not comparable with one on a hot filament, and
without this you cannot tell them apart — the run reports 16/16 fired either
way.

> **The host cannot supply this, which is why the firmware does.** A filament's
> ACTIVE window is a few triggers wide and one host poll round is ~250 ms, so
> host sampling cannot be aligned to pulses — and sampling hard enough to try
> perturbs the ramp being measured (measured: a live-INA poll stretches
> IDLE→ACTIVE by 20%). The firmware reads only the CC loop's cached value and
> never touches I2C on the firing path.

Both fields are `None` when there is no reading, with a `*_unavailable` reason
rather than a number — `0` is a legal current and must never stand for unknown:

| reason | meaning |
|---|---|
| `no_answer` | the board did not answer / is not present |
| `no_live_sample` | never measured, or the sample was older than the firmware's 6 s freshness limit |
| `not_current_mode` | `heat_target_mA` only — the board is not current-regulated, so there is no setpoint |

> ⚠️ **A small number is not a sentinel.** `1 mA` against a `1500 mA` target is a
> *real* reading from a powered board whose filament is open — it is exactly the
> "fired before it was hot" case this field exists to surface, and
> `scan_report()` flags it as `fired_cold`. On a bench of empty slots every row
> looks like this, which is correct.

> ⚠️ **The pulse log PAGES, and the page size is not fixed.** It shrank from 32
> records to 14 when the record grew from 12 to 16 bytes, because a page has to
> fit the link's ~242 deliverable bytes — and an oversized frame is dropped
> *silently*, so a reader that assumed the old size would have seen a run stop
> reporting past ~19 pulses, i.e. exactly on the long runs. `shv_pulse_log()`
> loops on what each page actually returned; do not assume a size if you read
> the endpoint directly.

> **A healthy run has all three false on every pulse.** Two firmware defects on
> this path were fixed after being found here, and the shape of each is worth
> keeping as a regression signature:
>
> - **`on_mismatch` cascading to the end of a table** was an unframed read-back
>   FIFO whose consumer popped two entries per pulse and outran the producer.
>   One pulse reported `unverified`, consumed nothing, and every pulse after it
>   read the *previous* pulse's bit — permanently, because nothing re-anchored
>   the pairing. If this shape ever returns, the framing is broken.
> - **Scattered `unverified`** was the CPU's pulse accounting running on the
>   trigger clock while the read-backs do not exist until one pulse WIDTH later.
>   Fixed by accounting a pulse only once its samples exist.
>
> Throughout both, the pulses themselves were always correct — durations,
> envelope measurements, `done` and `uncounted` were unaffected. Only the
> verification was lost, which is exactly why decoding these flags matters: a
> run that looks perfect by every other measure can have no verification behind
> it.

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

#### The normal way: `fire_single_pulse(..., measure=True)`

> ⚠️ **It does not heat the filament.** The schedule it builds carries an empty
> heating table and it calls nothing in the power-state ladder — the filament
> stays in whatever state you left it in. What runs end to end is the *schedule*
> path, not the whole operation.
>
> At **minimum** the filament's isolated rail must be on, or `arm` silently
> SKIPS it and the run still looks normal (`sleep_one()` is enough; the result
> then carries `skipped_unsafe`). For a real emission measurement it has to be
> at operating current — walk the ladder first, as in the example below.

**Don't fire and measure as two steps.** Firing and measuring are separate
subsystems but a single operation: a pulse you fired without measuring tells
you almost nothing, and arming the detector *after* firing has already missed
it. One flag runs the whole flow:

> ⚠️ **Do NOT call `hv_grid_set(f, on=True)` before pulsing.** That is the
> *continuous DC* path — it closes the filament's grid switch and leaves HV
> routed to it. `fire_single_pulse` drives that same switch as a brief pulse,
> and the schedule must own it. The two are **alternatives, not steps**: pulse
> with the grid off, or route DC with `hv_grid_set` and don't pulse.
>
> **Nothing in the client enforces this** — an earlier client-side guard was
> removed because it was bypassable, checked the wrong surface (the firmware's
> `beginRun` rewrites the whole channel byte), and could brick the main firing
> path after a fault. The schedule owns the switch; keeping DC off before a
> pulse is yours to get right.

```python
from ct_simple_control import CTClient

ct = CTClient("192.168.8.214")
with ct.lease(ttl=120, note="pulse measurement"):
    ct.idle_one(5, current_ma=1500, verify=True)      # get the filament hot
    # grid stays OFF — the schedule owns the switch for the duration of the pulse

    r = ct.fire_single_pulse(5, num_pulses=3, width_us=1000, measure=True)

    print(ct.describe(r))
    # Measured 3 pulse(s), peak mA: 5.83, 5.79, 5.85 (ref 1227.4 mV)

    if r["ok"]:
        for e in r["measured"]:
            # post_bg_ma is ABSENT (not zero) when the post-pulse window
            # wasn't measured — so read it with .get(), not e["post_bg_ma"]
            print(f"{e['on_us']} us  peak {e['peak_ma']:.2f} mA  "
                  f"post {e.get('post_bg_ma', 'not measured')}  "
                  f"plateau {e['plateau_ma']:.2f} mA "
                  f"background {e['bg_ma']:.2f} mA")
    else:
        print("not trustworthy:", r.get("error"))

    ct.stop_one(5, verify=True)
```

That call arms the detector, notes where the event stream is, fires, collects
exactly the events *this* fire produced, and releases the detector — including
if the fire fails or times out.

**What `measure=True` adds to the result**

| key | meaning |
|---|---|
| `measured` | one event per pulse: `on_us`, `peak_ma`, `plateau_ma`, `bg_ma`, `post_bg_ma`, `id` |
| `ref_mv` | the live reference reading actually used for the mA conversion |
| `ok` | stricter — True only if the fire succeeded **and** every fired pulse produced a measured event |

**Tuning the background** — the background is measured **symmetrically** around
the envelope, from ONE pair of numbers applied to both sides: settle for
`bg_gap_us`, then average `bg_window_us`.

```
    <- gap 200 -><- win 50 ->| envelope |<- win 50 -><- gap 200 ->
          (pre: SUBTRACTED)                 (post: reference)
```

Pre and post share the numbers on purpose — same front end, same disturbance
beside each edge — and one pair is half the wire and half the ways to get it
wrong. Defaults 200 µs / 50 µs.

```python
r = ct.fire_single_pulse(25, num_pulses=3, width_us=1000, measure=True,
                         bg_gap_us=400,       # let it settle longer
                         bg_window_us=100)    # then average 100 µs
```

**Why the gap matters most on the PRE side.** The charge is
`integral = Σx − (F−R)·mean_bg`, so an error in the **pre**-pulse background
biases the charge in proportion to pulse width — 1 LSB of background error on a
1000-sample pulse is 1000 LSB·samples of integral error, arriving as a
perfectly plausible number. The pre side used to have **no gap at all** and a
shorter window than the post side (20 vs 50): the less accurate average was the
one being subtracted. While the STM32 processed samples in coarse batches, edges
landed late and the window sat well before the real edge by accident; once edges
were placed at the exact sample, the window began abutting the edge and picking
up whatever leads the envelope.

If `post_bg` comes back looking like the tail of the pulse rather than a settled
level, the gap is too short. `bg_window_us=0` turns the measurement off, and
`post_bg` then reports `None` — **not** `0`, which would be a legal post-pulse
current.

**Minimum pulse spacing.** gap + window on each side is 500 µs of quiet per
pulse, so `MIN_INTER_PULSE_US` is derived, not chosen.
`fire_single_pulse(measure=True)` **refuses** a tighter interval: firing closer
measures one pulse's background over its neighbour's tail, which produces no
error and no flag, just a biased charge. Only enforced when `measure=True` —
firing faster is legitimate when nobody is integrating.

**Limits.** window 1–1024 samples, gap 0–3072, and their sum ≤ 4096 (the STM32's
raw-sample history). Both the ESP32 and the STM32 **refuse** out-of-range values
rather than clamping, so a request for 2000 can never silently average 1024.

**Knowing whether it worked** — three fields, all on every event:

| field | meaning |
|---|---|
| `background_n` | samples that actually backed the mean. Short of the request only when history is short (fresh reset, rate change, ADC restart). **`0` = no background at all** |
| `background_gap` | gap actually left before the rise. Equal to your request = the pre-gap took effect |
| `background_windowing` | whether this firmware does exact windowing at all (`fw_build ≥ 0x00030000`) |

`background_n == 0` **refuses the charge** (`integral_mams` None,
`integral_mams_unavailable="no_background"`). Without a background, `integral`
degenerates to the raw in-envelope sum with nothing subtracted — a large,
entirely plausible number that is not a charge.

`background_n` short of the request sets `background_partial`. It is `None`, not
`False`, on firmware that does not report the field: unknown must not read as
complete.

⚠ **Check `background_n == 0` before `bg_sigma4 == 0`.** When there is no
background, sigma is 0 too — but that is "no background", not "flat input".
Deciding on sigma first files a missing background as a dead front end and sends
someone to check an analog path that is fine.

This was verified on hardware: three pulses, `background_n = 50` and
`background_gap = 200` on every one, exactly as requested.

These are microseconds here and samples on the wire; the client converts using
the `rate_hz` it is arming. They are sent on **every** arm, because the STM32
loses them on reset — leaving that to a one-time setup would mean a reset
silently reverts to "not measured" without the host noticing.

That stricter `ok` is the point. A fire that "worked" while the detector saw
nothing — link down, detector not really armed, events dropped — reports
`ok=False` rather than letting a silent measurement gap look like success. And
if the detector cannot be armed at all, **nothing is fired**: `measure=True`
never leaves you guessing whether HV went out.

Requires the STM32 link to be up. If it isn't, you get the refusal, with the
reason:

```python
r = ct.fire_single_pulse(5, num_pulses=1, measure=True)
# {"ok": False, "fired": 0, "measured": [], "ref_mv": None,
#  "error": "detector arm failed, nothing fired: HTTP 502: hsadc_config failed (UART)"}
```

#### The pieces, if you need them separately

Everything below is what `measure=True` does for you. Reach for it only when
you need to measure pulses this client didn't fire (an external trigger
source, or the GUI firing), or to hold one arm across many fires.

**Shared with the GUI**: `pulse_arm()`/`pulse_disarm()` hit the *same*
backend endpoints as the GUI's "Stream" button and "Record measurement"
card, which the backend reference-counts — arming here while a GUI tab
already has Stream or Record running just **joins** that arm (your
`rate_hz` is ignored if you weren't the first arm-er); disarming here
only actually disarms the hardware once nothing else still wants it
armed. Safe to run this script alongside an open GUI tab.

**`ready_arm(rate_hz=1000000, n_samples=2000, bg_gap_us=None, bg_window_us=None)`**
/ **`ready_disarm()`** — Arm/release the pulse-envelope **relay** *and* the STM32
detector inside it. This is what `fire_single_pulse(measure=True)` uses, and
what you want if you are arming by hand and intend to measure.

> The relay is the part that makes measurement work at all. The STM32 times each
> pulse from the real edge on its PA4 pin, and that pin only moves while the
> ESP32 is mirroring the RP2350's pulse-envelope output onto it. Arm only the
> detector (`pulse_arm` below) and PA4 never moves — the detector sits there
> sampling and never sees a pulse start, so a fire measures **nothing** while
> everything else looks healthy.

```python
ct.ready_arm(bg_gap_us=200, bg_window_us=50)   # relay + detector
try:
    ...                                              # fire from elsewhere
finally:
    ct.ready_disarm()
```

**`pulse_arm(rate_hz=1000000)`** / **`pulse_disarm()`** — Arm/release the
detector. Arming doesn't measure anything by itself — it just gets the
STM32 ADC streaming and the edge-triggered detector waiting for a real
pulse.

```python
ct.pulse_arm(1000000)
# ... fire pulses some other way (the GUI's pulse controls, a schedule run) ...
ct.pulse_disarm()
```

> ⚠️ **`pulse_arm` alone measures nothing, and `hv_grid_set` cannot trigger it.**
> The detector is *envelope-gated only* — it starts on a real edge at the
> STM32's PA4 pin, relayed from the RP2350's pulse envelope, and there is no
> amplitude self-trigger any more. `hv_grid_set` routes DC and never moves PA4,
> so arming and toggling the grid yields **zero events while everything looks
> healthy**. Either use `fire_single_pulse(..., measure=True)`, which arms the
> relay for you, or pair `pulse_arm` with `ready_arm` and fire a real pulse.

**`pulse_events(since=0)`** — Poll measured events newer than `since`.

`since` is a **cursor**. The ESP32 keeps a rolling log of measured pulses with
monotonically increasing `id`s, and it keeps growing — firing does not clear it.
Passing `since` returns only events newer than that id; without it you get the
whole backlog, including pulses from someone else's run, with no way to tell
which were yours.

```python
since = ct.pulse_cursor()      # where the log is right now
...                            # fire
r = ct.pulse_events(since)     # only your events
```

Keep `r["last_id"]` and pass it as the next `since`. `since=0` means "everything
still in the log", which is rarely what you want.

> ⚠️ **Do not read the cursor by passing a huge `since`.** The obvious trick —
> pass a number nothing can exceed, get zero events and a truthful `last_id` —
> has a ceiling. `pulse_id` is a `uint32`, but the ESP32 parses the query
> parameter through Arduino's `String::toInt()`, which returns a **signed**
> long: anything above `2**31-1` **saturates** rather than wrapping. Verified on
> the wire — `since=2**32` returns 0 events, where a wrap to 0 would have
> returned the whole ring.
>
> So no cursor above **2,147,483,647** can be expressed, and once `pulse_id`
> passes that the trick stops excluding old events *silently* instead of
> failing. At one scan a minute that is centuries away; at 1000 pulses/s it is
> 23 days, and the id only resets when the ESP32 reboots.
> `pulse_cursor()` reads `last_id` out of an ordinary reply instead, which is
> correct for the whole range.

| field | meaning |
|---|---|
| `id` | monotonic event id — pass it back as the next `since` |
| `t_us` | STM32 timestamp of the pulse start |
| `on_us` | **measured** pulse width, from the real envelope on the STM32's PA4 pin — not the width you commanded. Compare the two; they should agree closely |
| `peak` | highest sample inside the pulse |
| `plateau` | mean raw code over `[rise + margin, fall)`, margin = 0 → whole envelope. **`None` when not measured** |
| `bg` | background **before** the pulse — an exact window `[R−gap−win+1, R−gap]`, no longer a rolling accumulator |
| `post_bg` | mean **after** the pulse — the STM32 waits ~50 µs to settle, then averages ~50 µs. **`None` when not measured** (see below) |
| `bg_sigma4` | 4× the background σ (σ = `bg_sigma4/4`) |
| `integral` | background-subtracted sum over the pulse, raw counts: `round(Σ(sample − bg))`. **Signed** — see below |
| `empty_envelope` | `True` when `on_us == 0` — a PA4 glitch, not a pulse. See below |
| `rate_hz` | the rate this pulse was **actually** sampled at. **`None` on firmware too old to report it** |
| `recv_ms` | host receive time |

> ⚠️ **`post_bg` is `None`, not `0`, when it wasn't measured.** That happens
> when the post-pulse window is configured to 0 samples, or when the next pulse
> arrives before even one sample could be taken. `0` is a perfectly legal
> post-pulse current, so the two must not look alike — guard with
> `is not None`, never `if e["post_bg"]:`.

#### How each number is derived

The STM32's own `pulse_measurement.md` (in the STM32G431ADC repo root) is the
authoritative definition; this is the host-side summary. Envelope = samples
`R+1 … F`, where `R`/`F` are the rising/falling PA4 edges, each placed within
~1 sample of the real edge.

| field | how the STM32 computes it | background subtracted? |
|---|---|---|
| `t_us` (`rise_sample`) | `R`, sample index **since boot** | — |
| `integral` | `round(Σ x − (F−R)·Σbg/n)` using the **exact** background mean | **yes**, exact mean |
| `on_us` (`duration_samples`) | `F − R` | — |
| `peak` | `max x` over the envelope | no — raw code |
| `bg` | `floor(Σbg / n)` over the `bg_window` samples before `R` | — |
| `bg_sigma4` | `floor(4·√(n·Σbg² − (Σbg)²) / n)`; σ = value/4, **population** σ | — |
| `plateau` | `floor(mean x)` over `R+m+1 … F`, m = `plateau_margin` | no — raw code |
| `post_bg` | `floor(mean x)` over `F+g+1 … F+g+k` | no — raw code |

Because `peak`/`plateau`/`bg`/`post_bg` are **absolute raw codes**, converting
them needs the full affine map — which is what `pulse_ma()` applies. `integral`
already has the background removed, so only the slope applies, which is why
`integral_mams` and `integral_mams_sigma` use `k` alone.

`0xFFFF` is the not-measured sentinel for `peak`, `plateau` and `post_bg` — not
a valid 12-bit code, so it can never collide with a real sample. The ESP32 turns
it into `null` for all three in one place, and the Python layer converts only
non-`None` fields. `integral` has no such sentinel (`0` is one of its legitimate
values), which is why the duration test carries that one.

Parameters this client actually sends (`PULSE_CFG`, re-sent on every arm — a
reset reverts the STM32 to its own defaults, which happen to match):

| parameter | value sent | changeable from here |
|---|---|---|
| `bg_window` | 50 samples | yes — `bg_window_us` (**both** sides) |
| `bg_gap` | 200 samples | yes — `bg_gap_us` (**both** sides) |
| `plateau_margin` | 0 (plateau = whole envelope) | no — firmware constant |
| `post_bg_gap` | 200 samples | same `bg_gap_us` — not separately settable |
| `post_bg_n` | 50 samples | same `bg_window_us` — not separately settable |

Four behaviours worth knowing before you trust a number:

- **`t_us` wraps.** It is a free-running sample counter, so it rolls over every
  2³² samples — about **71.6 minutes at 1 MSPS**. Differencing two `t_us` across
  a wrap gives a large negative or nonsense gap; use `id` for ordering, and
  `recv_ms` for wall-clock.
- **A glitch on PA4 arrives as a normal-looking event.** If a rise and a fall
  land on the same sample, the STM32 commits a complete event with
  `on_us = 0`, `integral = 0`, and `peak = 0` — where that `peak` is the
  field's *initial value*, never a measurement. Converted naively, `0` counts
  becomes a confident **≈ −32 mA** and the integral a legal **`0.0`** charge.
  This client flags them `empty_envelope: True` and withholds
  `peak_ma`/`plateau_ma`/`integral_mams`; `bg` and `post_bg` are real
  measurements on these events and still convert. `on_us == 0` is the reliable
  test — and with `plateau_margin > 0` the corresponding test is
  `on_us <= margin`, still on the duration, never on `plateau`.
  Note that the STM32's `edge_rise_abandoned` / `edge_fall_ignored` counters do
  **not** count these: a rise and fall that pair up on one sample is a complete
  envelope as far as the firmware is concerned. Both counters reading zero
  therefore says nothing about whether glitch events occurred.
- **`plateau` is `None` when its range is empty** (envelope shorter than
  `plateau_margin`) on firmware carrying the `measure_flags` capability; older
  firmware reports `peak` there instead, unflagged. **Don't test
  `plateau == peak` to catch that** — a genuinely flat pulse has
  `floor(mean) == max`, so the test fires on the *cleanest* data, not the
  broken data. With the margin at 0 the empty case needs
  `duration_samples <= 0`, which a real pulse never produces.
- **The background can be stale.** The window holds the last 20 samples *before
  the rise*; if the previous pulse ended less than 20 samples earlier, it still
  contains samples from before **that** pulse. Back-to-back firing quietly
  degrades `bg`, and with it `integral` and σ.
- **Two different backgrounds are in play.** `integral` subtracts the *exact*
  mean `Σbg/n`; a hand-computed `plateau − bg` uses the *rounded* `bg`, so the
  two disagree by up to one ADC code. Prefer `integral_mams` where it matters.

> ⚠️ **`on_us` and `integral` are SAMPLE COUNTS, and the rate is not a
> constant.** The STM32's timer runs at 170 MHz ÷ an integer divider, so the
> achieved rate rarely equals the one you requested, and it can change between
> pulses — which is why each event carries its own `rate_hz`. Use that field,
> never an assumed 1 MSPS: charge and duration scale 1:1 with it, so a guessed
> rate produces a wrong answer wearing the right units.

`peak`/`plateau`/`bg`/`post_bg` are **raw
ADC counts**, not mA; convert with `pulse_ma()`/`pulse_events_ma()`
below, never by hand. Persist the returned `last_id` and pass it back as
`since` to get only the delta next time, and use `pulse_cursor()` to read the
cursor before a run — not a large `since`, which saturates at `2**31-1` in the
ESP32's query parsing (see the warning above).

**`get_ads1115_ref_mv()`** — The external differential circuit's LIVE
reference voltage (mV), off the ADS1115's "1.2V ref" channel. Nominally
~1.2 V, but it drifts board-to-board and with temperature — treating it
as a fixed constant is exactly what overstated measured current by ~35%
in the GUI's own history before this was fixed there. `pulse_ma()` needs
a live reading of this for an accurate conversion.

> ⚠️ **A board with no analog front end reads zero, and zero converts to about
> −32 mA.** The lab test board's STM32 is a *bare* board — no DS3502, no
> ADS1115, no AMC3301. On it `i2c_present` is `0x80` (probe valid, all four
> absent), `get_ads1115_ref_mv()` returns `None`, `adc_window` reads
> `min=0 max=0` with **zero variance**, every `peak`/`plateau`/`bg`/`post_bg`
> is 0, and `integral_mams` is `0.0` with `background_flat: True`.
>
> None of that is a fault to diagnose — **the parts are absent, not broken and
> not switched off**, and no supply will change it. Everything digital still
> works there (heating, schedules, firing, envelope widths, sentinels); only
> emission-current *magnitudes* need a populated board.

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
also gets `peak_ma`/`plateau_ma`/`bg_ma`/`post_bg_ma` fields, converted with ONE live
reference reading shared across the whole batch. Returns `{"ok",
"events": [...], "last_id", "ref_mv"}`.

It also adds the **charge** per event, so you don't have to know the sample
rate or the ADC scale yourself:

| field | meaning |
|---|---|
| `integral_mams` | charge in **mA·ms** = `slope × integral / rate_hz`. `None` when it can't be computed |
| `integral_mams_unavailable` | present only when the above is `None`: `"rate_unknown"`, `"integral_clamped"`, `"integral_form_unknown"`, or `"saturated"` — see below |
| `integral_saturated` | `True` when `integral` hit `INT32_MAX`/`INT32_MIN` (charge unusable) |
| `duration_saturated` | `True` when `on_us` hit `65535` (width is a lower bound; charge still valid, σ is `None`) |
| `integral_mams_sigma` | the scatter in that charge from background noise alone (`σ√N`). **`None` when `bg_sigma4 == 0`** |
| `background_flat` | `True` when `bg_sigma4 == 0` — the background window had zero spread, i.e. a stuck or unpowered input |

> ⚠️ **Charge is refused, not approximated, when the STM32 is the wrong
> firmware.** `integral` changed meaning without changing shape — it used to be
> clamped at zero per sample, which biases weak pulses **upward** (measured:
> ~57% of a 976 µs reading was clamp bias, not signal). Same offset, same width,
> nothing in the value to tell them apart. So the STM32's `GET_INFO` capability
> bit decides: `"integral_clamped"` means it is the old form and the conversion
> is refused; `"integral_form_unknown"` means it never answered, which is *not*
> the same as knowing it is old. Both give `None`, never a number.
>
> The ESP32 relays this once per response as `integral_signed`
> (`true`/`false`/`null`) on `/api/pulse-events`. **All of this arithmetic runs
> host-side** — the firmware only forwards raw integers.

> ⚠️ **A negative charge is a legal result, not a fault.** `integral` is a
> *signed* sum with no per-sample clamp, so pure noise sums to about zero and a
> pulse dimmer than the background it was measured against lands below it.
> Don't floor these at zero or treat them as errors — doing so reintroduces
> exactly the upward bias the signed accumulation was changed to remove.

> ⚠️ **`bg_sigma4 == 0` disables that test rather than passing it.** A live
> 12-bit front end always has *some* background spread, so zero means the input
> is stuck or unpowered — not that the measurement is noise-free. Reported as
> `background_flat: True` with `integral_mams_sigma: None`, because a σ of `0`
> would make `|charge| > σ` true for **any** charge: the one guard against
> over-reading a weak signal would silently stop guarding. Found on hardware
> with the analog front end unpowered — every sample `0`, `bg_sigma4` `0`.

> ⚠️ **Compare `|integral_mams|` against `integral_mams_sigma` before believing a
> small value.** A charge smaller than its own σ has not been distinguished
> from noise, and roughly a third of pure-noise pulses land outside ±1σ by
> chance. The σ field exists so you can make that call from the data instead of
> eyeballing the magnitude.

> ⚠️ **`integral_saturated` exists because the firmware clamps without setting
> any flag.** A saturated `integral` is indistinguishable from a real one by
> value alone, so `integral_mams` is `None` there rather than a number that
> looks like an unusually large — but plausible — charge. This is reachable, not
> theoretical: the integral accumulates over the STM32's *internal* `u32` sample
> count, which is **not** bounded by `duration_samples`' `u16`, so at full scale
> it hits `INT32_MAX` in roughly 520k samples (~0.5 s at 1 MSPS).
>
> `-1` is **not** treated as saturation, even though the pre-signed firmware
> used `0xFFFFFFFF` as its marker (which reads as `-1` once parsed signed). With
> the clamp gone, `-1` is an ordinary noise result — far too common to discard.
> Mixing the two firmware generations is ruled out by flashing both sides
> together, not by guessing from the value.

> ⚠️ **`duration_saturated` is separate from `integral_saturated`.** `on_us` is
> truncated to `65535`, so a longer envelope reports a width that is only a
> *lower bound* — but its **charge is still valid**, since `integral_mams` never
> uses the duration. What is lost is `integral_mams_sigma`, which needs the real
> `N`: it is `None` there rather than computed from `65535`, which would
> understate the scatter on exactly the longest pulses.

```python
since = ct.pulse_cursor()                           # cursor, no history

# fire_single_pulse(measure=True) arms the detector AND the envelope relay,
# fires, and tears both down -- the grid stays off, which is the point.
ct.fire_single_pulse(5, width_us=1000, measure=True)

r = ct.pulse_events_ma(since)
for e in r["events"]:
    print(f"peak {e.get('peak_ma')} mA, plateau {e.get('plateau_ma')} mA "
          f"(ref {r['ref_mv']} mV)")
    mas = e.get("integral_mams")
    if mas is None:
        print(f"  charge unavailable: {e.get('integral_mams_unavailable')}")
    else:
        sigma = e.get("integral_mams_sigma")
        flag = "" if sigma is None or abs(mas) > sigma else "  <- within noise"
        print(f"  charge {mas} mA*ms  (sigma {sigma} mA*ms"
              f" @ {e['rate_hz']} Hz){flag}")
```

**`measure_pulse_current(filament, num_pulses=1, width_us=1000, rate_hz=1000000, ...)`**
— Identical work to `fire_single_pulse(..., measure=True)`, which is the
recommended call. This differs only in **shape**: it nests the fire result
under `"fired"` instead of merging it, which is handy when you want to log the
two halves apart.

```python
r = ct.measure_pulse_current(filament=5, num_pulses=3, width_us=1000)
print(ct.describe(r))
# Measured 3 pulse(s), peak mA: 5.83, 5.79, 5.85 (ref 1227.4 mV)
if not r["ok"]:
    print("fire or measurement failed:", r["fired"].get("error"))
```

Returns `{"ok", "fired": <the full fire_single_pulse result>, "measured": [...],
"ref_mv"}`. Same arming, correlation and strict-`ok` rules as `measure=True`;
it takes the same `fire_single_pulse` parameters.

### Waiting on state — what is polled, and what that costs

There is no push path for "this filament arrived". The firmware *knows* — the
CC loop sets `arrival` (ramping / settled / capped) in the 0x3A cached read —
but the host has to ask. The one push channel that exists (`EVENT_TELEMETRY`
mode 2, 20 fps) deliberately **cannot** be used for this: its entries are
`(channel, port, bus_mV, current_mA)` with *no validity flag*, so a failed read
arrives as `0 mA` and a stale value arrives looking live. It is advisory, for
the live view only. Verification goes through 0x3A, which carries the flags.

So every wait here is host-side polling of a **single, shared** link. Measured
round trips on this bench, idle:

| polled endpoint | transport | median RTT |
|---|---|---|
| cached current (bulk 0x3A) | RP2350 link | 39 ms |
| `shv_status` | RP2350 link | 15 ms |
| `/api/tps-struggling` | RP2350 link | 68 ms |
| `/api/pulse-events` | **ESP32 HTTP** | 65 ms (200–300 ms under load) |

Two consequences that are easy to get wrong:

**A short fixed sleep is not a poll rate.** `sleep(0.05)` around a 15 ms or
65 ms request is not "20 Hz" — it is back-to-back requests with a gap smaller
than the round trip. Both wait loops that run *while the thing they watch is
happening* now back off geometrically (`_poll_intervals`), staying fast for the
first few checks and then settling to ~2 req/s:

- the schedule-completion wait shares the link with the firing run itself and
  with the 20 fps telemetry push;
- `/api/pulse-events` is served by the ESP32's `config_portal`, which is
  single-threaded with the `tcp_bridge` relay carrying those very pulses — so
  polling harder for pulse events makes them arrive *later*.

**Verify in batches, not one filament at a time.** The cached read is a bulk
command: it returns every board whether you ask for one or ninety-six. Use
**`wait_for_currents({filament: mA, ...})`** — one polling loop, one bulk read
per tick, one shared `struggling` check every 2 s — instead of calling
`wait_for_current()` per filament, which pays the same bulk read N times over.
Same rules and the same per-filament result shape.

`wait_for_currents()` refuses ~0 mA targets: confirming a stop needs the
live-INA219 and power-state fallbacks (stopping drops the rail, so the current
reading ceases to exist), and those are per-board reads that would put back the
traffic this removes. Use `stop_one(verify=True)` for those.

> ⚠ `energised()` does not survive `SIGKILL`/`SIGTERM` — the `finally` never
> runs. Observed here: a test script killed by a harness timeout left a filament
> at 880 mA. For any script that may be killed, install a handler that turns the
> signal into an exception so the context manager can do its job:
> ```python
> for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
>     signal.signal(s, lambda n, f: (_ for _ in ()).throw(KeyboardInterrupt()))
> ```
> A `SIGKILL` still cannot be caught by anything in this process — only a
> backend-side watchdog could cover that, and there isn't one yet.

### Board self-test and I²C diagnostics

"Is the hardware wired up and answering" — a different question from "is the
filament good". All return `{controller: {...}}` keyed 1/2, covering only
CONNECTED controllers.

| method | what it reads |
|---|---|
| `chip_health()` | presence scan — which chips answer |
| `diagnosis()` | per board+chip: **op / reg / addr / missing** |
| `read_tca9554()` | expander register dump + the per-read ACK flag |
| `self_test()` | TCA9554 toggle test — **drives pins** |
| `read_board_faults()` | per-filament TPS fault / HV-overcurrent, `None` when the validity twin says the read failed |

**`diagnosis()` is the one that earns its keep.** `chip_health()` only checks
the address ACK, so it happily reports `mux: 16, iso_io: 16` for a board where
nothing works. `addr` means the chip ACKs its address and will not talk
registers — and a board-wide `addr` result means **the control cable is
unplugged**, not that the chips are dead. That cost a working RP2350 a
near-replacement here before the distinction was read correctly.

`self_test()` is the only one that is not read-only. The backend refuses it on
a controller running a schedule and says so in `selftest_error` rather than
disarming for you.

### HV switch toggle test

**`hv_switch_test(controller=1, channel_mask=None)`** — force every HV grid
switch ON, read the 165 sense back twice, restore OFF. Tests the **switch**,
not emission; run with the HV voltage at 0. `channel_mask` takes `None` (all
8), a bitmask (`0b11` = CH1+CH2), or a list (`[0, 1]`).

Three outcomes, not two — a switch that reads back inconsistently is neither a
pass nor a failure, and collapsing it either way loses the one thing worth
knowing:

| verdict | meaning |
|---|---|
| `pass` | both reads returned 1 |
| `dead` | both reads returned 0 |
| `inconclusive` | the reads disagreed, or one never arrived |

Retries follow the same rule: a transport timeout or a busy mailbox is retried,
**`VERIFY_FAIL` never is** — retrying the failure the test exists to find would
turn a dead switch into a passing one. Every bit is restored OFF whatever the
reads said, and `restored_off` is reported as a count rather than assumed.

That third verdict paid for itself immediately: a wrong endpoint in the first
draft made all 8 switches come back `inconclusive` with `restored_off 0/8` on a
bench whose switches were fine. With only pass/fail, a tooling bug would have
read as 8 dead switches.

### Test & measurement flows

Ports of the GUI's "Calibration & Test" tab, runnable from a script instead of
a browser tab that has to stay open. Only the two flows that need nothing but
the INA219 exist so far — the other four need the DS3502s and ADS1115, which
are not fitted on the lab board, so they could be written but not verified.

**`measure_filament_resistance(filaments=None, settle_s=, short_ohm=, open_ma=,
cool_s=, progress=)`** — STANDBY every filament, settle, read one V+I pair
each, report `R = V/I` with a short/open verdict. The quick go/no-go screen.

**`sweep_filament_impedance(filaments=None, start_mv=, end_mv=, step_mv=,
dwell_s=, short_ohm=, cool_s=, hysteresis_tol=, save=, progress=)`** — per
filament, step the voltage up in VOLTAGE mode, dwell at each step, and fit the
V-I curve to `R(I) = a·I² + R₀`. Slow; saves curves and fits to the backend's
`calibration/` directory.

Both run inside `energised()`, so every filament is STOPped on the way out —
including on an exception or Ctrl-C.

#### Resistance is meaningless without a temperature

A filament that ran recently is still hot, and hot tungsten reads a much higher
resistance than cold. `R₀` is the fit's extrapolation to zero dissipated power,
so it is the room-temperature resistance only if the sweep both **starts** at
ambient and **stays in equilibrium** at every step. Neither condition shows up
in the fit — both just move R₀. Measured on this bench's dummy load, three runs
that all reported `verdict: ok`:

| start | dwell | R₀ | return-point drift | residual |
|---|---|---|---|---|
| 60 s off | 1.5 s | 0.192 Ω | **+51.1%** | 18.3% |
| straight after the previous sweep | 1.5 s | 0.473 Ω | **+16.8%** | 3.5% |
| 90 s off | 6.0 s | 0.481 Ω | +4.8% | 4.9% |

**R₀ swung 2.5× across runs that all succeeded**, and both variables do it:

- *Start temperature.* Rows 1 and 2 differ only in that — 0.192 vs 0.473 Ω.
- *Dwell too short.* Rows 1 and 3 both start cold. At 1.5 s the filament lags
  the voltage, so the low-current points read colder than equilibrium and the
  extrapolation is pulled **down** — toward a false SHORT. Row 3's 4.9%
  residual says the `a·I²+R₀` model fits once each point has settled; row 1's
  18.3% says it does not, and its R₀ is an artifact.

The only outputs that separate the trustworthy run from the other two are the
drift and the residual — R₀ alone looks equally authoritative in all three.
Which is the practical procedure: **raise `dwell_s` until the drift falls
inside tolerance**, then trust R₀. Two independent guards:

- **`cool_s`** — de-energise and wait this long first, crediting time already
  served (`cool_down()` does it standalone). There is deliberately **no default
  value** anywhere: the right number is the thermal time constant of the real
  filament in its vacuum envelope, which cannot be inferred from a bench dummy
  load. Establishes the start condition; reported under `"thermal"`.
- **The return point** — always taken. After the last step the sweep goes back
  to `start_mv` and re-measures. Same temperature as it opened ⇒ same R. This
  *tests* the premise instead of assuming it, and needs no prior knowledge of
  the filament, so it works on any rig. Drift past `hysteresis_tol` (default 5%)
  marks R₀ not-cold.

Per filament: `r0_is_cold` is `True` / `False` / `None` (unverified) with a
`cold_note`; run-level `r0_not_cold` lists every filament whose R₀ fitted but
is not a cold resistance. A not-cold R₀ is still returned — it is a real
measurement at an unknown temperature — but it is never labelled as cold, and
`r0_is_cold` is saved into the calibration file next to R₀.

`measure_filament_resistance()`'s R is **never** a cold resistance: 0.8 V into a
real filament is ~0.7 W, so it is heating throughout the settle. That is fine
for a short/open screen, where the thresholds are orders of magnitude from the
drift, but do not record it as a filament's cold resistance.

**`thermal_history(filaments=None)`** — how long each filament has been
de-energised, from the backend (which outlives any one script, so it remembers
what the *previous* script left hot). A filament missing from the result is
**unknown, not cold** — a fresh backend or a controller reconnect erases the
history. Needs backend `/api/thermal-history`; against an older backend the
flows report the precondition as unverifiable rather than failing or, worse,
assuming it held.

#### Where these deliberately differ from the GUI

Three of `fitR0()`'s outputs are unresolved fits wearing a number, and all
three feed a `R₀ < short_ohm` comparison, so each one is a potential false
SHORT:

1. **Singular fit** (every point at the same current — an empty board pinned at
   the voltage floor). GUI: `{R0: 0, a: 0}`. Here: `R0_ohm: None`.
2. **Ill-conditioned fit.** Over a narrow current range `I` and `I³` are nearly
   the same shape and the split between R₀ and `a` is not identifiable; R₀
   usually lands negative, which trips the GUI's `R0 < 0` branch and pins it to
   exactly 0. Observed on this bench: a load holding 0.99–1.11 A across an
   0.88–1.46 V sweep, R = 0.89–1.31 Ω throughout, fitted as R₀ = 0 Ω →
   reported SHORT. `collinearity` (0 … 1) was 0.9916 there vs ~0.85 for sweeps
   that fit properly, so the threshold sits between them at 0.95.
3. **Pinned R₀ = 0** from the surviving `R₀ < 0` branch is kept but flagged
   `r0_pinned`, and never called a short.

`fit_cold_resistance()` always returns a dict and never a bare number — R₀ is
`None` whenever it could not be resolved, with `reason` saying which way. Where
both resolve, it is bit-for-bit identical to the JS (verified across eight
curves, including the `R₀ < 0` branch). It also reports `rms_residual_frac`, how
well the model actually describes the load — reported, not gated on, since
calibrating a threshold needs a population of real filaments.

`tps_fault` is read with its `tps_fault_valid` twin: unreadable becomes `None`,
not "no fault". The GUI's test 1 reads the raw bit, which silently turns every
unreadable board into a passing one.

### Recovery and saving records

**`recover(stop_heating=False)`** — clear state a killed operation left behind:
a relay still armed, a schedule still loaded, filaments still energised.
`stop_heating=True` also de-energises what it finds.

A failed STOP is **reported, not swallowed**: `stop_heating_ok` and
`stop_heating_errors` say which controllers could not be stopped, with a
top-level error naming them. This is the call made BECAUSE something is already
wrong, so reporting the energised filaments it found while hiding that it could
not stop them would be the worst combination available.

**`save_calibration(name, data)`** — write a record to the backend's
`calibration/` directory as `<name>_<timestamp>.json` plus a flat `.csv` of
`data["curves"]`. The file lands next to `backend.py`, **not** next to the
calling script: the backend owns the disk here.

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
# "Failed: controller not connected"   (no trailing period on this one)
```

### Exceptions

See [Error handling](#error-handling--read-this-first) at the top of this
doc for the full picture. Short version: **almost nothing here raises.**
Every method returns `{"ok": bool, "error": str, ...}` — check `"ok"`.

| Class | Raised by | When |
|---|---|---|
| `CTLeaseError` | `acquire_lease()` / `with ct.lease():` | Another client already holds the write lock. **The one deliberate exception on the hardware path** — see below. |
| `CTError` | nothing, by default | Base class, kept for compatibility. Not raised by any method here. |
| `CTConnectionError` | nothing, by default | Kept for compatibility. A connection failure now comes back as `{"ok": False, "connection_error": True, "error": ...}` instead of raising. |
| `ValueError` | `set_filament_order()` | Raised when the 96-entry order isn't a valid one-to-one mapping — wrong length, a value outside `0..95`, or duplicates — and when a dict is passed instead of the explicit list. The message names the offending entries. Validate input you build from user data. |
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
ct.set_dead([6, 26, 73], reason="burnt emitters")

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
