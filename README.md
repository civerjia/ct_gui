# Multi-Source CT Control GUI

Browser GUI for the 96-source ring CT. The left panel is a **filament-ring
monitor** (geometry + live per-filament telemetry); the right column holds the
**hardware control modules**. The motion controls (collapsed under the ring)
only drive the on-screen animation. Hardware control (two ESP32 bridges →
RP2350B controllers) grafts onto `backend.py` `/api/*` and `ingestTelemetry()`.

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

## Power topology

Two power controllers (two ESP32 bridges on TCP :3333):

| Controller | Filaments | Channels × boards |
|------------|-----------|-------------------|
| Power 1    | 0–47      | 6 of 8 ch × 8     |
| Power 2    | 48–95     | 6 of 8 ch × 8     |

Mapping: `filament i → P{1|2} · CH{1–6}.{1–8}`, i.e. `channel = (i%48)//8`,
`board = (i%48)%8`.

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

Then open <http://127.0.0.1:8770>.

- **Play / Step / Reset** drive the scan; **Space** toggles play, **→** steps.
- The collimator/active-filament/gantry sliders scrub the geometry directly.
- Toggles under the canvas show the beam fan, filament indices, collimator
  wedge, and detector pixel ticks.

Stdlib only — no third-party dependencies.
