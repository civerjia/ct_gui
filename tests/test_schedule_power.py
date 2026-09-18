#!/usr/bin/env python3
"""Schedule bench test for ONE power controller.

Uses the designed active-list mapping (NOT hand-built filament=slot):
  - read the device active list  -> slot k -> filament (the mapping's assignment)
  - fireable = filaments the active list assigns to PRESENT slots
  - build emission (those filaments, in slot order) + rolling ACTIVE heat plan
  - download -> verify -> arm -> fire the ESP32 trigger train -> read back

All reads retry until valid (the WiFi bridge drops frames).

Usage: python3 test_schedule_power.py [--controller 1] [--host 192.168.8.137]
                                      [--pulses 3] [--idle 1000] [--active 1500]
"""
import json, os, urllib.request, time, argparse

B = 'http://127.0.0.1:8770'
# Who we are on the shared API (backend.py owns the single-client bridge, so the
# GUI and every bench script go through it) — shows up in GET /api/clients.
ME = f'test_schedule_power#{os.getpid()}'

def _raw(p, b=None, timeout=12):
    try:
        req = urllib.request.Request(B + p,
              data=(json.dumps(b).encode() if b is not None else None),
              headers={'Content-Type': 'application/json', 'X-CT-Client': ME})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except Exception as e:
        return {'error': str(e)[:60]}

def connect(cid, host):
    for _ in range(6):
        if _raw('/api/connect', {'controller': cid, 'host': host}).get('ok'):
            return True
        time.sleep(1.5)
    return False

def rq(cid, host, p, b=None, valid=None, tries=6):
    """Request with reconnect + validity retry. `valid(resp)->bool` gates success."""
    for _ in range(tries):
        r = _raw(p, b)
        if 'not connected' in str(r.get('error', '')):
            connect(cid, host); time.sleep(0.4); continue
        if valid is None or valid(r):
            return r
        time.sleep(0.4)
    return r

def shv(cid, host, op, extra=None, valid=None):
    body = {'controller': cid, 'op': op}
    if extra: body.update(extra)
    return rq(cid, host, '/api/shv', body, valid=valid)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--controller', type=int, default=1)
    ap.add_argument('--host', default='192.168.8.137')
    ap.add_argument('--pulses', type=int, default=3)
    ap.add_argument('--idle', type=int, default=1000)
    ap.add_argument('--active', type=int, default=1500)
    a = ap.parse_args()
    cid, host, NP = a.controller, a.host, a.pulses

    connect(cid, host)

    # 1) active list (Power->Filament), retry until a real 64-byte list arrives
    al = shv(cid, host, 'get_active_list',
             valid=lambda r: isinstance(r.get('list'), list) and len(r['list']) == 64
                             and any(x != 0xFF for x in r['list'])).get('list', [])
    assigned = {k: al[k] for k in range(64) if al[k] != 0xFF}
    print('boards assigned to this power (active-list): %d' % len(assigned))

    # 2) pre-heat all -> IDLE, then read presence (retry until boards arrive)
    rq(cid, host, '/api/cmd', {'controller': cid, 'command': 'CH_SET_POWER_STATE',
                               'board_mask': [255] * 8, 'state': 4, 'arg': a.idle})
    time.sleep(7)
    bs = rq(cid, host, '/api/board-snapshot?controller=%d' % cid,
            valid=lambda r: len(r.get('boards', [])) > 0).get('boards', [])
    present = {b['channel'] * 8 + b['mux_port'] for b in bs
               if b.get('present') and (b.get('current_mA') or 0) > 300}

    # 3) fireable = assigned AND present, in slot order
    slots = sorted(s for s in assigned if s in present)
    fils = [assigned[s] for s in slots]
    print('present+alive: %d ; fireable (assigned & present): %d' % (len(present), len(fils)))
    if not fils:
        print('nothing fireable — aborting'); return

    # 4) build plan through the mapping's filaments
    emission = [{'filament': f, 'numPulses': NP, 'widthUs': 1000} for f in fils]
    heating = []
    for k, f in enumerate(fils):
        heating.append({'filament': f, 'triggerIndex': NP * k,      'state': 5, 'milliamps': a.active})
        heating.append({'filament': f, 'triggerIndex': NP * k + NP, 'state': 4, 'milliamps': a.idle})
    currents = {str(f): {'idle_mA': a.idle, 'active_mA': a.active} for f in fils}
    plan = {'emission': emission, 'heating': heating, 'currents': currents,
            'config': {'interPulseMs': 10000, 'maxOnMs': 40, 'totalMs': 600000, 'triggerEdge': 0},
            'repeats': 1}

    # 5) download + VERIFY entries+heat+config all landed. One download call with a
    # LONG timeout (it blocks for the whole pipelined transfer; a short client timeout
    # cuts it mid-stream and only part loads). Retry the WHOLE download if incomplete.
    want_e, want_h = len(fils), 2 * len(fils)
    ent = heat = cfg_ok = 0
    for attempt in range(4):
        dl = _raw('/api/download', {'plan': plan}, timeout=90)
        if 'not connected' in str(dl.get('error', '')):
            connect(cid, host); continue
        ti = shv(cid, host, 'table_info', valid=lambda r: r.get('entryCount') is not None)
        hi = shv(cid, host, 'heat_info', valid=lambda r: r.get('heatCount') is not None)
        gc = shv(cid, host, 'get_config', valid=lambda r: r.get('ok'))
        ent, heat = ti.get('entryCount') or 0, hi.get('heatCount') or 0
        cfg_ok = 1 if gc.get('interPulseMs') == 10000 else 0
        print('download attempt %d: ok=%s entries=%s heat=%s interPulseMs=%s (want %d/%d/10000)'
              % (attempt + 1, dl.get('ok'), ent, heat, gc.get('interPulseMs'), want_e, want_h))
        if ent == want_e and heat == want_h and cfg_ok:
            break
        time.sleep(1.0)
    if not (ent == want_e and cfg_ok):
        print('download incomplete (entries=%d/%d config=%s) — aborting' % (ent, want_e, cfg_ok)); return

    # 6) reset to Idle (a prior run may have left Fault/Complete -> arm StateConflict), then arm
    shv(cid, host, 'disarm', valid=lambda r: r.get('ok'))
    am = shv(cid, host, 'arm', {'repeats': 1}, valid=lambda r: 'reject' in r or r.get('ok'))
    print('ARM:', am)
    if am.get('reject'):
        print('armed rejected — dead board in schedule; aborting'); return

    # 7) fire the trigger train until Complete
    target = len(fils) * NP
    def status():
        s = shv(cid, host, 'status', valid=lambda r: r.get('status', {}).get('state') is not None).get('status', {})
        return s.get('state'), s.get('entryIndex'), s.get('totalPulsesDone')
    print('firing %d triggers...' % target)
    for i in range(target + 20):
        rq(cid, host, '/api/sync/fire', {'controller': cid})
        if (i + 1) % 25 == 0:
            st, ei, done = status()
            print('  +%d: state=%s entry=%s done=%s/%s' % (i + 1, st, ei, done, target))
            if st == 3: break
        time.sleep(0.05)

    # 8) read back
    time.sleep(0.4)
    s = shv(cid, host, 'status', valid=lambda r: r.get('status', {}).get('state') is not None).get('status', {})
    pl = shv(cid, host, 'pulse_log', valid=lambda r: 'records' in r)
    fired = sorted({r['filament'] for r in pl.get('records', [])})
    print('FINAL state=%s done=%s/%s' % (s.get('state'), s.get('totalPulsesDone'), s.get('totalPulsesTarget')))
    print('distinct filaments FIRED=%d of %d fireable' % (len(fired), len(fils)))
    missing = [f for f in fils if f not in fired]
    if missing:
        print('NOT fired:', missing)

if __name__ == '__main__':
    main()
