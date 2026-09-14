#!/usr/bin/env python3
"""74HC165 readback diagnostic for the RP2350b HV shift-register chain.

Talks DIRECTLY to the RP2350b over its USB-CDC serial port (no WiFi bridge).

Uses HvSetChannelByte (0x11) with writeMode=2 (FORCE) — writes testByte to
the 595, reads 165 feedback, and returns WITHOUT the safety auto-clear that
fires on any mismatch in normal mode. This lets us see what the 165 actually
reads after a write, regardless of whether it matches.

Response: [status(0), channel(1), desired(2), feedback(3)] — 4 bytes on Ok.

Usage:
  python3 test_hv_diag165.py [--port /dev/cu.usbmodem11101] [--baud 460800]
                             [--channel all|0-7]

Requires: pip install pyserial
"""
import argparse, sys, time, struct

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed. Run: pip install pyserial")
    sys.exit(1)

# ── Frame codec ───────────────────────────────────────────────────────────────
SOF0, SOF1       = 0xA5, 0x5A
VERSION          = 0x01
FLAG_ACK_REQUIRED = 1 << 0
FLAG_IS_RESPONSE  = 1 << 1

def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc

def encode_frame(ftype: int, payload: bytes, seq: int = 1) -> bytes:
    flags = FLAG_ACK_REQUIRED
    header = bytes([VERSION, ftype, flags, seq,
                    len(payload) & 0xFF, (len(payload) >> 8) & 0xFF])
    crc = _crc16(header + payload)
    return bytes([SOF0, SOF1]) + header + payload + struct.pack("<H", crc)

def parse_frames(buf: bytearray) -> list:
    out = []
    while True:
        if len(buf) < 8: break
        idx = -1
        for i in range(len(buf) - 1):
            if buf[i] == SOF0 and buf[i+1] == SOF1:
                idx = i; break
        if idx < 0: buf.clear(); break
        if idx > 0: del buf[:idx]
        if len(buf) < 8: break
        ver, ftype, flags, seq = buf[2], buf[3], buf[4], buf[5]
        length = buf[6] | (buf[7] << 8)
        if length > 512: del buf[:2]; continue
        total = 2 + 6 + length + 2
        if len(buf) < total: break
        payload = bytes(buf[8:8+length])
        crc_got = struct.unpack_from("<H", buf, 8+length)[0]
        crc_exp = _crc16(bytes(buf[2:8+length]))
        del buf[:total]
        if crc_got == crc_exp:
            out.append((ver, ftype, flags, seq, payload))
    return out

# ── Transport ─────────────────────────────────────────────────────────────────
HV_SET_CHANNEL_BYTE = 0x11   # [channel, value, writeMode] → [status, ch, desired, feedback]
WRITE_MODE_FORCE    = 2      # skip verify auto-clear; return actual 165 feedback

STATUS_NAMES = {0x00:"Ok", 0x05:"BadLength", 0x06:"BadArgument",
                0x07:"InvalidChannel", 0x0A:"NotReady", 0x0C:"VerifyFail",
                0x0D:"FaultActive", 0x0E:"Unsupported", 0x11:"StateConflict"}

def send_one(ser: serial.Serial, buf: bytearray, ftype: int, payload: bytes,
             timeout: float = 2.0) -> bytes | None:
    ser.reset_input_buffer()
    buf.clear()
    ser.write(encode_frame(ftype, payload))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        chunk = ser.read(ser.in_waiting or 1)
        if chunk:
            buf.extend(chunk)
        for _ver, ft, flags, _seq, pl in parse_frames(buf):
            if ft == ftype and (flags & FLAG_IS_RESPONSE):
                return pl
        time.sleep(0.005)
    return None

# ── Transport ─────────────────────────────────────────────────────────────────
HV_SET_SHIFT_HZ = 0x80   # [hz: uint32 LE] → [status, actual_hz: uint32 LE]

def set_shift_hz(ser: serial.Serial, buf: bytearray, hz: int) -> tuple:
    """Set 165 readback SCK frequency. Returns (actual_hz, error_str)."""
    payload = struct.pack("<I", hz)
    pl = send_one(ser, buf, HV_SET_SHIFT_HZ, payload, timeout=2.0)
    if pl is None or len(pl) == 0:
        return None, "no response"
    status = pl[0]
    if status != 0x00:
        return None, f"status={STATUS_NAMES.get(status, f'0x{status:02X}')}"
    if len(pl) < 5:
        return None, f"short response ({len(pl)}B)"
    actual = struct.unpack_from("<I", pl, 1)[0]
    return actual, None

# ── Test patterns ─────────────────────────────────────────────────────────────
TEST_PATTERNS = [0x01, 0x80, 0x55, 0xAA, 0xFF]

def run_force_write(ser, buf, channel: int, test_byte: int):
    """Write testByte via writeMode=2 (force). Returns (desired, feedback) or None."""
    payload = bytes([channel & 0xFF, test_byte & 0xFF, WRITE_MODE_FORCE])
    pl = send_one(ser, buf, HV_SET_CHANNEL_BYTE, payload, timeout=2.0)
    if pl is None or len(pl) == 0:
        return None, None, "no response"
    status = pl[0]
    if status != 0x00:
        return None, None, f"status={STATUS_NAMES.get(status, f'0x{status:02X}')}"
    if len(pl) < 4:
        return None, None, f"short response ({len(pl)}B)"
    return pl[2], pl[3], None   # desired, feedback, error

def manual_mode(ser, buf, channel: int):
    """Interactive: type a hex byte, script writes it and reads back 165."""
    print(f"Manual mode — ch{channel}. Type hex byte (e.g. 0x55 or AA), blank to re-read, 'q' to quit.")
    last_val = None
    while True:
        try:
            line = input(f"  ch{channel} > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if line.lower() in ("q", "quit", "exit"):
            break
        if line == "":
            # re-read with same value (or 0x00 if none written yet)
            val = last_val if last_val is not None else 0
        else:
            try:
                val = int(line, 16)
                if not 0 <= val <= 255:
                    print("  Value must be 0x00–0xFF")
                    continue
            except ValueError:
                print(f"  Invalid hex: {line!r}")
                continue

        desired, feedback, err = run_force_write(ser, buf, channel, val)
        last_val = val
        if err:
            print(f"  ERROR: {err}")
        else:
            bits = f"{desired:08b}"
            ok = desired == feedback
            tag = "OK" if ok else "MISMATCH"
            print(f"  wrote=0x{desired:02X} ({bits})  165_read=0x{feedback:02X} ({feedback:08b})  [{tag}]")

    # Clear on exit
    print("  Clearing (writing 0x00)...")
    run_force_write(ser, buf, channel, 0x00)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/cu.usbmodem11101")
    ap.add_argument("--baud", type=int, default=460800)
    ap.add_argument("--channel", default="all", help="0-7 or 'all'")
    ap.add_argument("--patterns", default="",
                    help="comma-separated hex bytes e.g. 0x55,0xAA")
    ap.add_argument("--manual", action="store_true",
                    help="interactive mode: type a hex byte, read back 165; --channel must be a single channel")
    ap.add_argument("--sclk", type=int, default=None, metavar="HZ",
                    help="set SCK frequency before testing (e.g. 1000 for 1 kHz); range 100–2000000")
    a = ap.parse_args()

    if a.manual and a.channel == "all":
        print("ERROR: --manual requires a specific --channel (e.g. --channel 0)")
        sys.exit(1)
    channels = list(range(8)) if a.channel == "all" else [int(a.channel)]
    patterns = ([int(p, 16) for p in a.patterns.split(",") if p.strip()]
                if a.patterns else TEST_PATTERNS)

    print(f"Opening {a.port} @ {a.baud} baud...")
    try:
        ser = serial.Serial(a.port, a.baud, timeout=0.1)
    except serial.SerialException as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    time.sleep(0.5)
    ser.reset_input_buffer()
    buf = bytearray()

    # Quick PING to confirm link
    pl = send_one(ser, buf, 0x01, struct.pack("<I", 0xDEAD), timeout=2.0)
    if pl is None or (pl and pl[0] != 0x00):
        print(f"PING failed ({pl.hex() if pl else 'timeout'}) — wrong port?")
        ser.close(); sys.exit(1)
    print(f"Link OK (uptime {struct.unpack_from('<I', pl, 5)[0] if pl and len(pl)>=9 else '?'} ms)\n")

    if a.sclk is not None:
        actual, err = set_shift_hz(ser, buf, a.sclk)
        if err:
            print(f"ERROR setting SCK: {err}")
            ser.close(); sys.exit(1)
        print(f"SCK set to {actual} Hz\n")

    if a.manual:
        manual_mode(ser, buf, channels[0])
        ser.close()
        return

    fails = total = 0

    for ch in channels:
        for pat in patterns:
            desired, feedback, err = run_force_write(ser, buf, ch, pat)
            if err:
                print(f"  ch{ch} pat=0x{pat:02X}  ERROR: {err}  [FAIL]")
                fails += 1
            else:
                ok = (feedback == pat)
                tag = "PASS" if ok else "FAIL"
                note = "" if ok else f"  <- 165 read 0x{feedback:02X}, want 0x{pat:02X}"
                print(f"  ch{ch} pat=0x{pat:02X}  desired=0x{desired:02X} feedback=0x{feedback:02X}  [{tag}]{note}")
                if not ok:
                    fails += 1
            total += 1
        print()

    ser.close()

    print("=" * 60)
    if fails == 0:
        print(f"ALL {total} tests PASSED")
    else:
        print(f"{fails}/{total} FAILED")
        if fails == total:
            print("165 cannot read any pattern — hardware issue in 165/MISO chain")
        else:
            print("Partial failures — check per-channel wiring or timing")

if __name__ == "__main__":
    main()
