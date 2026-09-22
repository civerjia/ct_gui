# Bench tests

Hardware bring-up and reliability scripts. These are **bench tests against real
hardware**, not unit tests — there is no `pytest` suite here, each one is run by
hand with `backend.py` up and a controller connected.

```bash
python3 tests/test_pulse_train.py --help
```

Run them from anywhere: `_path.py` puts `tools/ct_gui` on `sys.path`, so
`from ct_simple_control import CTClient` resolves no matter the working
directory. Import it first:

```python
import _path  # noqa: F401
from ct_simple_control import CTClient
```

| script | what it exercises |
|---|---|
| `test_api.py` | end-to-end API: mapping → HV → heating ladder → fire → measure, with PASS/FAIL per step and a non-zero exit on failure |
| `test_hv_diag165.py` | 74HC165 read-back of the RP2350b HV shift-register chain |
| `test_pulse_sweep.py` | voltage sweep via the STM32 pulse detector's on-chip summary |
| `test_pulse_width_sweep.py` | pulse-width sweep at fixed voltage; known-timing mode |
| `test_pulse_detect_sweep.py` | WIDTH_US × GAP_US, firing a real 4-pulse train each time |
| `test_pulse_train.py` | multi-pulse train with a real off-time gap between pulses |
| `test_pulse_train_sweep.py` | reliability sweep of the confirmed pulse-train sequence |
| `test_pulse_multientry_sweep.py` | multi-entry (alternating filament) reliability sweep |
| `test_schedule_power.py` | schedule bench test, one power controller |
| `test_schedule_twopower.py` | schedule bench test, two power controllers |
| `test_liuxing.py` | — |

`test_api.py` is the one to run first — it is the only script here that
*checks* rather than prints, so "did the rig come up correctly" has a one-line
answer:

```bash
python3 tests/test_api.py                    # default: USER_INDEX 8, one 5 ms pulse
python3 tests/test_api.py --filaments 8,9,10 # several
python3 tests/test_api.py --skip-hv          # mapping + ladder + detector, no HV
python3 tests/test_api.py --scan-presence    # refuse absent boards up front (slow)
```

⚠ `--filaments` takes **USER_INDEX**, your own numbering, not the FID on the
wire. Under the liuxing order `--filaments 0` addresses FID 8 (a CH2 board) —
a different filament from `--filaments 8`, which is FID 0 on CH1.1.

For a guided tour of the API rather than a stress test, see
[`../examples/walkthrough.py`](../examples/walkthrough.py).

⚠ These energise filaments. `energised()` does not survive `SIGTERM` — see the
signal-handler note in the examples README before running one from a harness
that can time out.
