# Multi-Source CT Control GUI

Browser GUI for the 96-source ring CT. Layout: a full-width **Heating Gantt**
on top, then three columns — the **filament-ring monitor** (geometry), the
**Scan Schedule** (emission + derived heating plan), and the **hardware control
modules**. Two ESP32 bridges → RP2350B controllers; hardware I/O goes through
`backend.py` `/api/*` (and simulated telemetry drops into `ingestTelemetry()`).

## View modes (Live / Plan / Debug)

A selector on the geometry card switches what the ring reflects:

- **Plan** (default) — edit the scan + schedule and Play it (simulated). The hot
  band, schedule, and Gantt are all driven by the plan.
- **Live** — read-only; reflects the actual gantry position + INA219 telemetry.
- **Debug** — pick any filament to set power state / heating / OCP / HV / pulses.
  Right-click the V·I or mAs rings (or the **Debug…** button) to open the editor.

## Direct manipulation (on the ring)

- drag a **filament** → rotate the gantry (the whole assembly rocks together);
- drag the **collimator block** or **detector** → move the collimator+detector ring;
- drag in the inner **beam** region → switch the collimated (active) filament;
- hover any filament for its live readout; **⟲ gantry 0°** resets the rock.

## Filament-ring monitor

Each filament is a spectrum glyph on the source ring:

- **Marker color = PowerState** (firmware enum): Stop=1, Sleep=2, Standby=3,
  Idle=4, Active=5.
- **Outward bars** = live **voltage** (teal) and **current** (amber) from the
  per-board INA219 (`ChGetIna219` 0x24 → `bus_mV`, `current_mA`).
- **Inward bar** = **accumulated mAs** (violet). The firmware does *not* track
  mAs, so it is integrated host-side from current × dwell during firing.

Hover any filament to inspect it in the **Selected Filament** card. Telemetry is
simulated until the bridges are wired; real data drops into
`window.ingestTelemetry([{index, state, bus_mV, current_mA}])` unchanged.

## Power controllers (two ESP32 bridges)

Two power controllers, each an ESP32 bridge serving the RP2350B protocol on TCP
:3333 and the STM32 transparent bridge on :80 (`/stm32`):

| Controller | Filaments | Offset | Channels × boards |
|------------|-----------|--------|-------------------|
| Power 1    | 0–47      | 0      | 6 of 8 ch × 8     |
| Power 2    | 48–95     | 48     | 6 of 8 ch × 8     |

Mapping: `filament i → P{1|2} · CH{1–6}.{1–8}`, i.e. `channel = (i%48)//8`,
`board = (i%48)%8`. The **offset** is what ties a global filament index to a
bridge; it is editable per controller.

In the **Power Controllers** card: **Scan :3333** sweeps the LAN / `CTPower`
AP for hosts exposing TCP :3333 and fills the host fields; **Connect** opens
each bridge. Two heartbeat badges per controller track liveness — **RP2350 ♥**
(periodic PING through the bridge) and **STM32 ♥** (HTTP `/stm32` age). Badges
go green/pulsing when fresh (<3 s), amber when stale, red when dead.

The backend (`backend.py`) holds two independent `TcpProtocolClient`s — reused
from `../wifi_gui/net_protocol.py` — and exposes:

| Route | Method | Purpose |
|-------|--------|---------|
| `/api/scan` | GET | hosts with :3333 open (`{host, name, controller_responsive}`) |
| `/api/status` | GET | both controllers: connected, host, offset, RP2350/STM32 heartbeat ages |
| `/api/connect` | POST | `{controller, host, offset}` → open a bridge |
| `/api/disconnect` | POST | `{controller}` → close a bridge |
| `/api/offset` | POST | `{controller, offset}` → retarget filament mapping |
| `/api/cmd` | POST | `{controller, command, …}` → one RP2350B command on a board |
| `/api/schedule` | POST | stage the bound schedule (emission rows + heating deltas) |
| `/api/download` | POST | translate the bound schedule (logical 0–95) → per-controller frames and download to all connected controllers |
| `/api/arm` | POST | `{repeats}` → `ShvArm` both controllers (they wait for SyncIn) |
| `/api/disarm` | POST | `ShvDisarm` both → participants return to IDLE |
| `/api/run-status` | GET | poll `ShvGetStatus 0x79` per controller (shared `totalPulsesDone` playhead + live filament + state/fault) |
| `/api/telemetry` | GET | Live ring data: batch INA219 V/I per controller (mapped to filament 0–95) + live firing filament; INA sweep skipped while a controller is firing |
| `/api/present` | POST | I2C presence scan (`CH_GET_PRESENT 0x25`): which mux/TPS/INA/IO chips respond, per controller |
| `/api/selftest` | POST | TCA9554 toggle self-test (`0x60`) + presence; refused while a controller is running |
| `/api/mux-reset` | POST | pulse the TCA9548A reset line (`CH_RESET_MUX 0x5F`) and re-detect (power-cutting) |
| `/api/trigger` | POST | `{count}` → bench test: pulse SyncIn via the ESP32 `/sync/fire` |
| `/api/geometry` | GET | machine geometry constants |
| `/api/raw` | POST | `{controller, type, payload, flags, timeout}` → any protocol frame, verbatim |
| `/api/shv` | POST | `{controller, op, …}` → any Simple-HV-schedule opcode (`ShvSetActiveList`…`ShvHeatGetInfo` 0x70–0x7E, plus `ShvFaultPolicy` 0x81 / `ShvTriggerDelay` 0x82) on one controller |
| `/api/cached-currents` | GET | paged bulk read of `ChGetCachedCurrents` 0x3A per controller — **no I2C**, safe to poll mid-run |
| `/api/filament-currents` | GET | same 0x3A read, merged/keyed by global filament 0–95 across both controllers |
| `/api/hv-diag165` | POST | `{controller, channel, test_byte, settle_ms}` → `HvDiag165` 0x7F, raw 74HC165 write/read-back diagnostic |
| `/api/hv-shift-hz` | POST | `{controller, hz}` → `HvSetShiftHz` 0x80, sets the 165-readback bit-bang SCK frequency (set-only) |
| `/api/lock` | GET/POST | the cooperative write lease (see *Sharing the bench*) |
| `/api/clients` | GET | who else has called this API recently |

## Sharing the bench (several programs, one bridge)

The ESP32 bridge is **single-client**: `tcp_bridge.cpp` accepts one socket on
:3333 and hard-rejects every other connect, so only one process can ever own a
controller. `backend.py` is that process — it owns both bridge sockets,
serializes every frame on the link's request lock and routes responses by seq —
so **any number of programs share the hardware by speaking HTTP to this API**
instead of grabbing :3333 for themselves.

- Works the same for programs **on this machine** (a notebook, a bench script,
  a second browser tab — all hitting `127.0.0.1:8770`) and for programs on
  **other machines**: the server binds all interfaces (`CT_GUI_HOST=127.0.0.1`
  to keep it local) and answers **CORS**, so tools elsewhere on the LAN and
  pages served from another origin can call it too.
- **Reads are always free.** Writes interleave frame-by-frame and are free too.
- A program that needs a stretch of *uninterrupted* time (schedule download,
  calibration sweep, an armed run) takes the **lease**: while it is held, only
  the holder may write — everyone else gets `409` plus who holds it. Read
  commands (`CH_GET_*`, `HV_GET_*`, `ShvGet*`, presence/diagnosis scans) stay
  allowed throughout: a lease reserves the right to *change* the hardware, not
  to look at it. The lease always expires (default 30 s, renewable, 600 s max),
  so a client that dies mid-run can never wedge the bench.
- Identify yourself with an `X-CT-Client: <name>` header (or a `client` field in
  the body). Taking the lease **requires** a name and the name must be unique
  per process (append a PID): two unnamed programs on the same machine would
  otherwise share their address-derived identity and could renew, release, or
  write past each other's lease. Only the holder can release it (`steal: true`
  overrides — for when a program died without releasing and you don't want to
  wait out its TTL). `GET /api/clients` lists everyone currently on the API,
  `GET /api/lock` the current holder; the GUI shows a 🔒 badge while somebody
  else holds it.

```python
import os, requests
API = "http://127.0.0.1:8770"                      # or http://bench-host:8770
ME = {"X-CT-Client": f"sweep.py#{os.getpid()}"}    # unique per process

requests.post(f"{API}/api/lock", json={"action": "acquire", "ttl": 60,
                                       "note": "impedance sweep"}, headers=ME)
try:
    requests.post(f"{API}/api/cmd", headers=ME, json={           # named command
        "controller": 1, "command": "CH_SET_POWER_STATE", "channel": 0,
        "mux_port": 0, "state": 4, "arg16": 1200})
    requests.post(f"{API}/api/raw", headers=ME, json={           # or any raw frame
        "controller": 1, "type": "0x79", "payload": ""})
finally:
    requests.post(f"{API}/api/lock", json={"action": "release"}, headers=ME)
```

The GUI plays by the same rules: each tab has its own client id and takes the
lease around a schedule download and a Cal & Test run (`static/client.js`).

## Logs

`logs/backend.log`, rotated daily (`backend.log.YYYY-MM-DD`).

**Audit: one line per command.** Every POST that changes something is logged
with who sent it (the client id), a bounded summary of the request, and the
outcome — `-> ok` at INFO, `-> FAILED: <reason>` at WARNING. Writes refused by
the lease are logged too. Grep for them:

```bash
grep AUDIT logs/backend.log                  # everything that was commanded
grep "AUDIT.*FAILED" logs/backend.log        # everything that did not work
grep "AUDIT liuxing" logs/backend.log        # one client's commands
```

```
AUDIT test-script POST /api/filament-state {controller: 1, filament: 81, state: 5, arg: 1700} -> FAILED: filament 81 may not go to ACTIVE: …
AUDIT other-client POST /api/filament-state {…} -> FAILED: another client holds the write lease: test-script
AUDIT test-script POST /api/filament-state {…} -> FAILED: …   (+3 identical before this, not logged)
```

Not audited, because they would bury the commands: POSTs that only read
(`verify-schedule`, `present`, `diagnosis`, `/api/shv` status/pulse_log/…,
fault_policy and trigger_delay without a value), and heartbeats (lease renew,
safety keepalive, ready-renew). GETs are never audited — every GUI poll is a
GET. The same request from the same client within 5 s is counted instead of
re-logged, and the count rides on the next line that is logged.

**Controller outages** are logged on the transitions: `bridge DOWN` with the
error, `bridge RECONNECTED … after N s down (M attempts)`, and `bridge still
down` every 10 minutes in between. (It used to log every 1 s retry — 17–22k
lines a day with a controller switched off, 99.7% of the file.)

The audit records **what was commanded**. The full results — measured
currents, pulse events, everything a script printed — are in the client's own
log, below.

## Real-hardware bound schedule (download · arm · trigger · monitor)

The firmware executes the bound schedule **autonomously** (PIO/ISR, sub-µs);
the host's job is config-download + passive monitoring, never per-pulse driving.
`backend.py` is the **translation/planning layer** (heating doc §9): the GUI works
in logical filament **0–95**, the firmware in global **0–127** = `controller*64 +
channel*8 + position` (offset 0 / 64, default first-6-channels mask `0x3F`).

The **Hardware run** panel (schedule card):

- **Download** — builds the plan from the GUI's bound schedule and pushes, per
  connected controller: offset (`ShvSetOffset 0x70`), channel mask (`0x34`),
  per-filament IDLE/ACTIVE currents (`ChFilamentCurrents 0x39`), config (`0x75`),
  the **full global emission table to *both*** (`ShvSetEntries 0x73`, chunked), and
  **each controller's half** of the heating deltas (`ShvHeatSetEntries 0x7D`).
- **Arm / Disarm** — `ShvArm 0x77` (`repeats`) / `ShvDisarm 0x78`.
- **Fire SyncIn** — bench trigger: the ESP32 pulses SyncIn (`/sync/fire`). When the
  ESP32 isn't driving, leave SyncIn hi-Z for an external pulse source.
- **Monitor** — polls `ShvGetStatus 0x79`. Because both controllers fire the same
  full table (each fires its scope, *counts* the rest), `totalPulsesDone` is an
  identical **global cursor** → the ring playhead; `filamentIndex` is the live
  firing filament; `state`/`faultFilament` surface completion/abort. In **Live**
  view the ring tracks the firmware cursor.
- **Fault policy** — `ShvFaultPolicy 0x81` sets two independent stop/continue
  switches *before* arming: a board CC/OCP fault, and a 74HC165 read-back
  mismatch (which under "continue" is force-written and still fires at its
  intended width). Under "continue" the run's `stop_reason` never trips —
  the only record is `mismatch_count` + the `faulted_slots` bitmask, both
  reset on each fresh arm. `ShvTriggerDelay 0x82` offsets SyncIn→fire by a
  fixed µs (PIO precision mode only; `applies=0` means the live fire path
  can't honour it, so a set can't silently do nothing).
- Live currents during a run are read via `ChGetCachedCurrents 0x3A`
  (`/api/cached-currents`, `/api/filament-currents`) — the CC loop's
  already-measured values, **no I2C**, so polling it doesn't stall pulses
  the way an INA219 sweep would.

## Scan schedule + bound heating plan

The **Scan Schedule** is the materialized scan (one burst row per firing); the
geometry IS the schedule's current row, so Play/Step/row-click stay in sync.
The timeline is **pulse-indexed**: each burst spans its filament's pulse count,
so `trigger` = cumulative pulses (not 1 per burst).

The **heating plan** is *derived* from the emission schedule (the doc's bound
schedule, Approach B): every filament rests at IDLE and is promoted to ACTIVE a
**T_settle** lead before its window, held **T_hold** ms after its last pulse,
then demoted. The plan is **settle-checked** (every emission reaches stable
ACTIVE ≥ T_settle earlier) and the per-filament idle/active currents ride in the
heating deltas (`arg16`) — so they download with the schedule.

Three views: **Gantt** (full-width, filament × pulse-trigger, ACTIVE bars +
emission bursts + live playhead; scroll = zoom, drag = pan, dbl-click = reset),
**Emission** list, and **Heating** list (Trigger · Filament · → State · Current).
**Save to host** persists per-filament settings (localStorage); **Upload** sends
the bound schedule (emission rows + heating deltas) to the controller.

## Debug power panel (real RP2350B commands)

In Debug, the editor maps each control to a firmware command
(`docs/power_state_and_cc.md`) on the owning controller:

- **Power state** ladder (Stop/Sleep/Standby/Idle/Active/Voltage) →
  `CH_SET_POWER_STATE` 0x35. Idle/Active carry the **heating current** (mA, the
  closed-loop CC target); Voltage carries mV. `CH_GET_POWER_STATE` 0x36 reads
  state + fault on open.
- **OCP** → `CH_SET_TPS_OCP_THRESHOLD` 0x28 · **Heating I (measured)** →
  `CH_GET_INA219` 0x24 · **DC HV** read/toggle → `HV_GET_ALL_BYTES` 0x13 /
  `HV_SET_BIT` 0x10 · **Fire pulse** → `HV_PULSE` 0x16.
- **165 shift-chain bench diagnostics** (signal-integrity work, no HV/heating
  involved): `HvDiag165` 0x7F writes/reads back a test byte on one channel's
  74HC165 chain to separate a write fault from a read-back fault
  (`/api/hv-diag165`); `HvSetShiftHz` 0x80 sets that chain's bit-bang SCK
  frequency (100 Hz–2 MHz, set-only, survives until reboot — for a
  spike-free waveform on long cables) (`/api/hv-shift-hz`).

Frames are built in `backend.py` (the firmware protocol is ahead of the WiFi
GUI's `net_protocol`) and proxied via `POST /api/cmd {controller, command, …}`.

## Machine geometry

- **96 X-ray filaments** equally spaced on the **source ring (341 mm dia)**.
  Filament 0 sits on **+y**; index increases clockwise (Δ = 3.75°).
- **Collimator + detector** ride a second **ring (280 mm dia)**, pointing along
  the collimator-center filament's radius on the opposite side.
- The **collimator window** covers **35 filaments**, centered on one filament.
- The **detector** is a flat **256×256 PCD panel**, 0.1 mm pixels (25.6 mm wide),
  tangent to the detector ring and facing the source.
- The **gantry** (source ring) can rock ±~10°; the detector ring does not move
  with it.

## Modes

1. **Stationary** — gantry fixed. Fire the 35 covered filaments, then step the
   collimator+detector to the next filament center. 96 ring steps (CW or CCW).
2. **Precision** — at each collimator position, sweep the gantry across ±max in
   N steps, firing all 35 filaments at every gantry angle. On reaching an
   extreme, advance the collimator and reverse the sweep (boustrophedon).

## Run

```bash
python3 tools/ct_gui/backend.py
```

Then open <http://127.0.0.1:8770> (other machines: `http://<this-host>:8770`).

Every input field remembers the last value you typed or picked
(`static/sticky.js` → localStorage) and restores it on load, so a reload or a
rebuilt card never resets the bench setup. Fields marked `data-nostick` are
deliberately not remembered: the **Override** safety gate and the two
live-capture checkboxes (**Auto 2 Hz**, **Live**), which would otherwise start
hardware traffic by themselves at page load. `ctSticky.clear()` in the console
forgets everything.

- **Play / Step / Reset** drive the scan; **Space** toggles play, **→** steps.
- The collimator/active-filament/gantry sliders scrub the geometry directly.
- Toggles under the canvas show the beam fan, filament indices, collimator
  wedge, and detector pixel ticks.

Stdlib only — no third-party dependencies.
