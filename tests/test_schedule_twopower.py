#!/usr/bin/env python3
"""Two-power schedule test.

The schedule is GLOBAL: both controllers hold the full emission list and advance
on the SAME trigger; each fires the filaments its active list maps, counts the
rest. This builds the full schedule from BOTH controllers' active-list mappings
(present+alive slots only), downloads to both, arms both, auto-detects the
trigger topology (shared vs per-power), fires the train, and verifies each power
fired its own filaments.

Usage: python3 test_schedule_twopower.py [--pulses 3] [--idle 1000] [--active 1500]
"""
import json, os, urllib.request, time, argparse

B = 'http://127.0.0.1:8770'
HOSTS = {1: '192.168.8.137', 2: '192.168.8.203'}   # API index -> host
# Who we are on the shared API (backend.py owns the single-client bridge, so the
# GUI and every bench script go through it) — shows up in GET /api/clients.
ME = f'test_schedule_twopower#{os.getpid()}'

def _raw(p, b=None, timeout=12):
    try:
        req = urllib.request.Request(B + p,
              data=(json.dumps(b).encode() if b is not None else None),
              headers={'Content-Type': 'application/json', 'X-CT-Client': ME})
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except Exception as e:
        return {'error': str(e)[:60]}

def connect(cid):
    for _ in range(6):
        if _raw('/api/connect', {'controller': cid, 'host': HOSTS[cid]}).get('ok'):
            return True
        time.sleep(1.5)
    return False

def rq(cid, p, b=None, valid=None, tries=6):
    for _ in range(tries):
        r = _raw(p, b)
        if 'not connected' in str(r.get('error', '')):
            connect(cid); time.sleep(0.4); continue
        if valid is None or valid(r):
            return r
        time.sleep(0.4)
    return r

def shv(cid, op, extra=None, valid=None):
    body = {'controller': cid, 'op': op}
    if extra: body.update(extra)
    return rq(cid, '/api/shv', body, valid=valid)

def status(cid):
    s = shv(cid, 'status', valid=lambda r: r.get('status', {}).get('state') is not None).get('status', {})
    return s

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pulses', type=int, default=3)
    ap.add_argument('--idle', type=int, default=1000)
    ap.add_argument('--active', type=int, default=1500)
    a = ap.parse_args()
    NP = a.pulses

    for cid in (1, 2):
        connect(cid)

    # 1) filaments come from the HOST mapping (same source the download uses to route
    # heat + currents), NOT the device active list — otherwise heat/currents filter
    # to the wrong controller and drop. cid (API 1/2) == host controller (0/1) + 1.
    mp = _raw('/api/mapping').get('mapping', {})
    rows = mp.get('filaments') or []
    host_slot = {int(x['filament']): (x.get('channel'), x.get('position'))
                 for x in rows if x.get('controller') is not None}
    host_ctrl = {int(x['filament']): int(x['controller']) for x in rows if x.get('controller') is not None}
    for cid in (1, 2):
        rq(cid, '/api/cmd', {'controller': cid, 'command': 'CH_SET_POWER_STATE',
                             'board_mask': [255] * 8, 'state': 4, 'arg': a.idle})
    time.sleep(7)
    fireable = {}   # cid -> [filaments], in slot order
    for cid in (1, 2):
        bs = rq(cid, '/api/board-snapshot?controller=%d' % cid,
                valid=lambda r: len(r.get('boards', [])) > 0).get('boards', [])
        present = {b['channel'] * 8 + b['mux_port'] for b in bs
                   if b.get('present') and (b.get('current_mA') or 0) > 300}
        mine = [(host_slot[f][0] * 8 + host_slot[f][1], f) for f in host_slot
                if host_ctrl[f] == cid - 1]
        fireable[cid] = [f for slot, f in sorted(mine) if slot in present]
        print('Power%d: host-mapped=%d  present+alive fireable=%d'
              % (cid, sum(1 for f in host_ctrl if host_ctrl[f] == cid - 1), len(fireable[cid])))

    # 2) GLOBAL emission = union of both controllers' filaments, sorted (firing order).
    #    Each filament is unique to one controller (active-list assignment).
    owner = {}
    for cid in (1, 2):
        for f in fireable[cid]:
            owner[f] = cid
    gfils = sorted(owner)
    print('global emission entries=%d (P1=%d + P2=%d)'
          % (len(gfils), len(fireable[1]), len(fireable[2])))
    if not gfils:
        print('nothing fireable — aborting'); return

    emission = [{'filament': f, 'numPulses': NP, 'widthUs': 1000} for f in gfils]
    heating = []
    for k, f in enumerate(gfils):
        heating.append({'filament': f, 'triggerIndex': NP * k,      'state': 5, 'milliamps': a.active})
        heating.append({'filament': f, 'triggerIndex': NP * k + NP, 'state': 4, 'milliamps': a.idle})
    currents = {str(f): {'idle_mA': a.idle, 'active_mA': a.active} for f in gfils}
    plan = {'emission': emission, 'heating': heating, 'currents': currents,
            'config': {'interPulseMs': 60000, 'maxOnMs': 40, 'totalMs': 600000, 'triggerEdge': 0},
            'repeats': 1}

    # 3) DISARM both to Idle FIRST — a Fault/Running schedule rejects all table-write
    # frames (CLEAR/SET_ENTRIES/SET_CONFIG), so the download silently fails on it.
    for cid in (1, 2):
        shv(cid, 'disarm', valid=lambda r: r.get('ok'))
    # download to BOTH (one call fans out), verify each controller's table loaded.
    # Long timeout: one bridge can be slow (~seconds/frame) but still complete.
    want_e = len(gfils)
    for attempt in range(4):
        dl = _raw('/api/download', {'plan': plan}, timeout=300)
        oks = {}
        for cid in (1, 2):
            ti = shv(cid, 'table_info', valid=lambda r: r.get('entryCount') is not None)
            gc = shv(cid, 'get_config', valid=lambda r: r.get('ok'))
            oks[cid] = (ti.get('entryCount') or 0, gc.get('interPulseMs'))
        print('download attempt %d: P1=%s P2=%s (want entries=%d cfg=60000)'
              % (attempt + 1, oks[1], oks[2], want_e))
        if all(oks[c][0] == want_e and oks[c][1] == 60000 for c in (1, 2)):
            break
        time.sleep(1.0)
    if not all(oks[c][0] == want_e and oks[c][1] == 60000 for c in (1, 2)):
        print('download incomplete on one controller — aborting'); return

    # 4) reset to Idle + arm BOTH
    for cid in (1, 2):
        shv(cid, 'disarm', valid=lambda r: r.get('ok'))
    arms = {cid: shv(cid, 'arm', {'repeats': 1}, valid=lambda r: 'reject' in r) for cid in (1, 2)}
    print('ARM: P1=%s P2=%s' % (arms[1], arms[2]))
    if any(arms[c].get('reject') for c in (1, 2)):
        print('arm rejected on a controller — aborting'); return

    # 5) auto-detect trigger topology: fire ONE controller's SyncOut, see if both advance
    d1_before, d2_before = status(1).get('totalPulsesDone'), status(2).get('totalPulsesDone')
    rq(1, '/api/sync/fire', {'controller': 1})
    time.sleep(0.3)
    d1_after, d2_after = status(1).get('totalPulsesDone'), status(2).get('totalPulsesDone')
    shared = (d2_after or 0) > (d2_before or 0)
    print('topology probe: P1 %s->%s  P2 %s->%s  => %s'
          % (d1_before, d1_after, d2_before, d2_after, 'SHARED (one SyncOut clocks both)' if shared else 'PER-POWER (fire both)'))

    # 6) fire the rest of the train
    target = want_e * NP
    fired_by_probe = 1
    def clock():
        rq(1, '/api/sync/fire', {'controller': 1})
        if not shared:
            rq(2, '/api/sync/fire', {'controller': 2})
    print('firing to target=%d...' % target)
    for i in range(fired_by_probe, target + 20):
        clock()
        if (i + 1) % 25 == 0:
            s1, s2 = status(1), status(2)
            print('  +%d: P1 state=%s done=%s | P2 state=%s done=%s'
                  % (i + 1, s1.get('state'), s1.get('totalPulsesDone'), s2.get('state'), s2.get('totalPulsesDone')))
            if s1.get('state') == 3 and s2.get('state') == 3:
                break
        time.sleep(0.05)

    # 7) verify each power fired its own filaments
    time.sleep(0.4)
    for cid in (1, 2):
        s = status(cid)
        pl = shv(cid, 'pulse_log', valid=lambda r: 'records' in r)
        fired = sorted({r['filament'] for r in pl.get('records', [])})
        exp = set(fireable[cid])
        hit = [f for f in fired if f in exp]
        print('Power%d FINAL state=%s done=%s/%s | fired-own=%d of %d expected'
              % (cid, s.get('state'), s.get('totalPulsesDone'), s.get('totalPulsesTarget'), len(hit), len(exp)))

if __name__ == '__main__':
    main()
