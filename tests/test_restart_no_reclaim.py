#!/usr/bin/env python3
"""A restart must not take the bridges back from the backend it starts.

2026-10-12: on Windows the old process stays alive waiting for the new one
(ct.update.restart_in_place), and its daemon threads kept running. Its
auto-connect loop reconnected both bridges right after _do_restart released
them; each bridge takes ONE client, so the new backend never got a controller.
Separately, the new process's "reconnect what the old one had" called
connect() again on links auto-connect had already opened, dropping them.
Offline: links, the scan and the relaunch are fakes.

    python3 tests/test_restart_no_reclaim.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

import ct.server._server as srv  # noqa: E402
from ct import update as ct_update  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeClient:
    def __init__(self, connected):
        self.connected = connected


class FakeLink:
    def __init__(self, name, host, connected):
        self.name, self.host = name, host
        self.client = FakeClient(connected)
        self.events = []

    def connect(self, host):
        self.events.append(("connect", host, srv._STOPPING.is_set()))
        self.host = host
        self.client.connected = True

    def disconnect(self):
        self.events.append(("disconnect", srv._STOPPING.is_set()))
        self.host = None
        self.client.connected = False


orig = dict(controllers=srv.CONTROLLERS, scan=srv.scan_bridges, rip=ct_update.restart_in_place)
try:
    # 1. _do_restart: the stop flag is up BEFORE the bridges are released.
    srv._STOPPING.clear()
    links = {1: FakeLink("Power 1", "192.168.8.157", True), 2: FakeLink("Power 2", "192.168.8.242", True)}
    srv.CONTROLLERS = links
    relaunched = []
    ct_update.restart_in_place = lambda: relaunched.append(True)

    class Srv:
        def server_close(self):
            pass
    srv._do_restart({1: "192.168.8.157", 2: "192.168.8.242"}, Srv())
    check("restart: every link released with the stop flag already set",
          all(l.events == [("disconnect", True)] for l in links.values()),
          {k: l.events for k, l in links.items()})
    check("...then relaunched", relaunched == [True])

    # 2. With the flag set, auto-connect does not scan or connect anything.
    scanned = []
    srv.scan_bridges = lambda: (scanned.append(True), [{"host": "192.168.8.157", "busy": False,
                                                        "has_stm32": True}])[1]
    r = srv.auto_connect()
    check("stopping: auto_connect refuses, no scan, slots stay free",
          not r["ok"] and not scanned and all(not l.host for l in links.values()), r)
    os.environ.pop("CT_NO_AUTO_CONNECT", None)
    srv._auto_connect_loop()          # must RETURN (not loop forever) once stopping
    check("stopping: the auto-connect loop exits", True)

    # 3. New process: a link auto-connect already opened is NOT reconnected.
    srv._STOPPING.clear()
    links = {1: FakeLink("Power 1", "192.168.8.157", True), 2: FakeLink("Power 2", None, False)}
    srv.CONTROLLERS = links
    os.environ["CT_RECONNECT"] = "1=192.168.8.157,2=192.168.8.242"
    srv._reconnect_from_env()
    check("reconnect: already-connected Power 1 left alone", links[1].events == [], links[1].events)
    check("reconnect: free Power 2 connected", links[2].events == [("connect", "192.168.8.242", False)],
          links[2].events)
finally:
    srv.CONTROLLERS = orig["controllers"]
    srv.scan_bridges = orig["scan"]
    ct_update.restart_in_place = orig["rip"]
    srv._STOPPING.clear()

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
