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

Then open the GUI in a browser (`http://localhost:8770`) and connect the
controllers to the ESP32 IP address, **or** connect via the API:

```bash
curl -X POST http://localhost:8770/api/connect \
     -H "Content-Type: application/json" \
     -d '{"controller": 1, "host": "192.168.x.x"}'
```

---

## Quick start

```python
from ct_simple_control import CTClient, CTError, CTTimeoutError

ct = CTClient("localhost", port=8770, client_id="my-script")

# Pre-heat all filaments to idle (warm pool)
ct.idle_all(currents={0: 2500, 1: 2500})   # filament→mA

# Promote one filament to active
ct.active_one(filament=0, current_ma=2900)

# Set HV
ct.set_emission_v(30)     # −30 V emission (magnitude, backend applies LUT)
ct.set_focus_v(200)       # −200 V focus
ct.set_emission_i(10)     # 10 mA emission current reference

# Enable HV output
ct.enable_emission(True)

# Read back
print(ct.read_emission_v())   # V (negative)
print(ct.read_emission_i())   # mA
print(ct.read_focus_v())      # V (negative)

# Fire one HV pulse on filament 0
result = ct.fire_single_pulse(
    filament=0,
    num_pulses=1,
    width_us=1000,       # 1 ms pulse
    controller=1,
    trigger="sim",       # ESP32 generates the SyncIn edge
)
print(result)  # {"ok": True, "fired": 1, "records": [...], "status": {...}}

# Tear down
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

```python
CTClient(host="localhost", port=8770, client_id="ct_simple_control", timeout=5.0)
```

`client_id` identifies your script in the backend's client list and lease log.

### Lease

| Method | Description |
|--------|-------------|
| `acquire_lease(ttl=60, note="")` | Take exclusive write lock (raises `CTLeaseError` if already held) |
| `release_lease()` | Release early (safe to call if not held) |
| `renew_lease(ttl=60)` | Extend before expiry |
| `with ct.lease(ttl, note):` | Context manager — auto-releases on exit |

### Power state

All methods accept `filaments=None` (all populated boards) or a list of
0–95 filament indices. `currents` is `{filament_index: mA}`.

| Method | State | Notes |
|--------|-------|-------|
| `stop_all(filaments=None)` | STOP | HV off, heating off |
| `sleep_all(filaments=None)` | SLEEP | |
| `standby_all(filaments=None)` | STANDBY | |
| `idle_all(filaments=None, currents=None)` | IDLE | warm pool |
| `active_one(filament, current_ma)` | ACTIVE | single filament |
| `active_all(filaments=None, currents=None)` | ACTIVE | |

### HV set

Backend handles LUT lookup and DS3502 wiper write.
Pass the **magnitude** (positive number); outputs are negative rail.

| Method | Description |
|--------|-------------|
| `set_emission_v(volts)` | Set emission HV — uses calibrated LUT, falls back to linear |
| `set_focus_v(volts)` | Set focus HV — uses calibrated LUT, falls back to linear |
| `set_emission_i(ma)` | Set emission current reference (0–85.7 mA, linear) |

Returns `{"ok", "wiper", "expect_v"/"expect_ma", "method"}`.
`method` is `"lut"`, `"lut(clamped)"`, or `"linear(no-lut)"`.

### HV readback (ADS1115)

| Method | Returns |
|--------|---------|
| `read_emission_v()` | float V (negative) |
| `read_emission_i()` | float mA |
| `read_focus_v()` | float V (negative) |
| `read_ads_all()` | dict with all four channels + raw codes |

### HV enable

| Method | Description |
|--------|-------------|
| `enable_emission(on: bool)` | Enable / disable emission HV output |
| `enable_focus(on: bool)` | Enable / disable focus HV output |
| `hv_status()` | `{emission_on, focus_on, ads1115_alert, amc3301_diag}` |

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
)
```

**Low-level** (for custom sequences):

```python
ct.shv_clear(controller=1)
ct.shv_push_active_list(controller=1)
ct.shv_set_entry(controller=1, filament=0, num_pulses=1, width_us=1000)
ct.shv_set_config(controller=1, inter_pulse_ms=3000, max_on_ms=40, total_ms=15000)
ct.shv_arm(controller=1, repeats=1)
# ... wait for external SyncIn edge ...
status = ct.shv_status(controller=1)
log    = ct.shv_pulse_log(controller=1)
ct.shv_disarm(controller=1)
```

### Exceptions

| Exception | When |
|-----------|------|
| `CTError` | Base class; operation failed |
| `CTConnectionError` | Cannot reach the backend |
| `CTLeaseError` | Lease held by another client |
| `CTTimeoutError` | `fire_single_pulse` polling timed out |

---

## Pre-heat workflow example

```python
from ct_simple_control import CTClient
import time

ct = CTClient("localhost", port=8770, client_id="preheat-script")

IDLE_CURRENTS  = {i: 2500 for i in range(48)}  # mA per filament
ACTIVE_CURRENT = 2900                           # mA for the firing filament

# 1. Ramp all filaments to idle (warm pool)
ct.idle_all(currents=IDLE_CURRENTS)
time.sleep(5)   # allow heating to settle

# 2. Set HV setpoints (LUT-based, backend owns the calculation)
ct.set_emission_v(30)    # −30 V
ct.set_focus_v(150)      # −150 V
ct.set_emission_i(10)    # 10 mA

# 3. Enable HV
ct.enable_emission(True)
ct.enable_focus(True)

# 4. Fire filaments one by one
for fil in range(48):
    ct.active_one(fil, ACTIVE_CURRENT)
    time.sleep(0.1)   # allow active current to settle

    with ct.lease(ttl=10):
        result = ct.fire_single_pulse(filament=fil, width_us=1000)
        print(f"fil {fil}: fired={result['fired']} "
              f"duration={result['records'][0]['durationUs'] if result['records'] else '?'} µs")

    ct.idle_all(filaments=[fil], currents={fil: 2500})  # demote back to idle

# 5. Tear down
ct.enable_emission(False)
ct.enable_focus(False)
ct.stop_all()
```
