/*
 * Power-controller connection panel.
 *
 * Scans the LAN for ESP32 bridges exposing TCP :3333, connects up to two of
 * them, lets the user pick the MASTER (the bridge that carries the STM32 — all
 * STM32/HV/ADC commands route there), and polls /api/status to drive the
 * per-controller RP2350 + STM32 heartbeat badges. The filament→power mapping is
 * the host active-list (Mapping card). All hardware I/O lives in the backend.
 */

import { state } from './state.js';

const api = async (path, opts) => (await fetch(path, opts)).json();
const post = (path, body) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
});

// heartbeat freshness -> badge class
function hbClass(ageMs, everSeen) {
  if (!everSeen || ageMs == null) return 'idle';
  if (ageMs < 3000) return 'alive';
  if (ageMs < 10000) return 'stale';
  return 'dead';
}

export function initControllers() {
  const scanBtn = document.getElementById('scanBtn');
  const scanHint = document.getElementById('scanHint');
  const lockBadge = document.getElementById('apiLockBadge');
  const dl1 = document.getElementById('bridgeHosts1');
  const dl2 = document.getElementById('bridgeHosts2');
  const cards = [...document.querySelectorAll('.pc')];
  const connected = {};

  // All hosts found by the last scan (deduped by identity). Used to rebuild
  // per-slot datalists that exclude the other slot's current selection.
  let allFoundHosts = [];   // [{ host, name, label }]

  function hostInputOf(cid) {
    const card = cards.find((c) => parseInt(c.dataset.ctrl, 10) === cid);
    return card ? card.querySelector('[data-host]') : null;
  }

  // Rebuild each slot's datalist to exclude whatever the OTHER slot has typed,
  // and clear a slot's value if it duplicates a CONNECTED slot.
  function updateHostLists() {
    // Collect which IPs are currently connected (backend has confirmed them).
    const connectedIps = new Set();
    for (const card of cards) {
      const cid = String(parseInt(card.dataset.ctrl, 10));
      if (connected[cid]) {
        const h = card.querySelector('[data-host]');
        if (h && h.value) connectedIps.add(h.value.trim());
      }
    }
    // For each unconnected slot: if its value is already taken by a connected
    // slot, wipe it so the user sees the field is free for a different IP.
    for (const card of cards) {
      const cid = String(parseInt(card.dataset.ctrl, 10));
      if (!connected[cid]) {
        const h = card.querySelector('[data-host]');
        if (h && h.value && connectedIps.has(h.value.trim())) h.value = '';
      }
    }
    const v1 = (hostInputOf(1) || {}).value || '';
    const v2 = (hostInputOf(2) || {}).value || '';
    [
      { dl: dl1, exclude: v2 },
      { dl: dl2, exclude: v1 },
    ].forEach(({ dl, exclude }) => {
      dl.innerHTML = '';
      for (const r of allFoundHosts) {
        if (r.host === exclude) continue;
        const o = document.createElement('option');
        o.value = r.host;
        o.label = r.label;
        dl.appendChild(o);
      }
    });
  }

  // Listen for manual typing in either IP field to keep the other list fresh.
  cards.forEach((card) => {
    const h = card.querySelector('[data-host]');
    if (h) h.addEventListener('input', updateHostLists);
  });

  scanBtn.addEventListener('click', async () => {
    scanBtn.disabled = true;
    userPinnedMaster = false;   // fresh scan → re-detect master from STM32 presence
    hasAutoArranged = false;    // allow slot swap to run again after a new scan
    scanHint.textContent = 'Scanning LAN + 192.168.4.0/24 …';
    try {
      const { results } = await api('/api/scan');
      // responsive controllers first, then port-open — so the best host wins.
      const found = (results || []).slice().sort(
        (a, b) => (b.controller_responsive ? 1 : 0) - (a.controller_responsive ? 1 : 0));

      // Dedup by identity (name = AP SSID / MAC) so a bridge answering at
      // both its LAN IP and 192.168.4.1 doesn't fill both slots.
      const seenId = new Set();
      allFoundHosts = [];
      for (const r of found) {
        const id = r.name || r.host;
        if (seenId.has(id)) continue;
        seenId.add(id);
        allFoundHosts.push({
          host: r.host,
          name: r.name,
          label: (r.name ? r.name + ' · ' : '') + (r.controller_responsive ? 'RP2350 ✓' : 'port open'),
        });
      }

      // Assign distinct IPs to the cards (first found → P1, second → P2).
      const hosts = allFoundHosts.map((r) => r.host);
      cards.forEach((card, i) => {
        const h = card.querySelector('[data-host]');
        if (h && hosts[i]) h.value = hosts[i];
      });

      // Rebuild datalists with the fresh results (each excludes the other slot).
      updateHostLists();

      if (!found.length) {
        scanHint.textContent = 'No bridge found. Join the CTPower-XXXXXX AP or check the ESP32 is powered, then scan again.';
      } else {
        const live = found.filter((r) => r.controller_responsive).length;
        scanHint.textContent = found.length === 1
          ? `Found ${found[0].name || found[0].host}${found[0].controller_responsive ? ' — controller alive' : ' — controller silent'} @ ${found[0].host}.`
          : `Found ${found.length} bridge(s), ${live} with a live controller — filled the cards (adjust if needed).`;
      }
    } catch (e) {
      scanHint.textContent = 'Scan failed: ' + e;
    } finally {
      scanBtn.disabled = false;
    }
  });

  let master = 1;
  let userPinnedMaster = false;   // true after manual M-badge click; cleared on scan
  let hasAutoArranged = false;    // prevent repeated swaps; cleared on scan

  // If the STM32 bridge ended up in slot 2, swap slots so it becomes slot 1
  // (Power 1 = master is the invariant). Runs once per scan/session.
  async function autoArrangeByStm32(st) {
    if (userPinnedMaster || hasAutoArranged) return;
    const c1 = st.controllers['1'];
    const c2 = st.controllers['2'];
    const c1HasStm = c1 && c1.connected && c1.stm32 && c1.stm32.ever_seen;
    const c2HasStm = c2 && c2.connected && c2.stm32 && c2.stm32.ever_seen;
    if (!c1HasStm && !c2HasStm) return;   // STM32 not visible on either yet — wait
    hasAutoArranged = true;
    if (c2HasStm && !c1HasStm) {
      // STM32 is on P2 → disconnect both, swap IP slots, reconnect
      const host2 = c2.host;
      const host1 = c1 && c1.connected ? c1.host : null;
      if (c2.connected) await post('/api/disconnect', { controller: 2 });
      if (c1 && c1.connected) await post('/api/disconnect', { controller: 1 });
      // Swap the IP fields first so the UI reflects the new arrangement
      const h1 = hostInputOf(1); if (h1) h1.value = host2;
      const h2 = hostInputOf(2); if (h2) h2.value = host1 || '';
      await post('/api/connect', { controller: 1, host: host2 });
      if (host1) await post('/api/connect', { controller: 2, host: host1 });
    }
    // Master is always slot 1 (now guaranteed to hold the STM32 bridge)
    if (master !== 1) {
      const res = await post('/api/master', { controller: 1 });
      if (res && res.master) master = res.master;
    }
  }

  for (const card of cards) {
    const cid = parseInt(card.dataset.ctrl, 10);
    const btn = card.querySelector('[data-connect]');
    const host = card.querySelector('[data-host]');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        if (connected[cid]) {
          await post('/api/disconnect', { controller: cid });
        } else {
          const res = await post('/api/connect', { controller: cid, host: host.value.trim() });
          if (!res.ok) { scanHint.textContent = `Power ${cid}: ${res.error}`; return; }
        }
        await refresh();
        updateHostLists();
      } finally {
        btn.disabled = false;
      }
    });
    // master badge lives in the pc-hb header group — look it up there
    const hbGroup = document.querySelector(`.pc-hb[data-ctrl="${cid}"]`);
    const mb = hbGroup && hbGroup.querySelector('[data-master]');
    if (mb) mb.addEventListener('click', async () => {
      userPinnedMaster = true;    // manual override — stop auto-detect until next scan
      const res = await post('/api/master', { controller: cid });
      if (res && res.master) master = res.master;
      refresh();
    });
  }

  async function refresh() {
    let st;
    try { st = await api('/api/status'); } catch { return; }
    if (st.master) master = st.master;
    // Somebody else on the shared API is holding the write lease — say so, so a
    // refused write reads as "the bench is busy", not as a broken GUI.
    if (lockBadge) {
      const lk = st.lock || {};
      const other = lk.held && lk.owner !== st.you;
      lockBadge.hidden = !other;
      if (other) {
        lockBadge.textContent = `🔒 ${lk.owner} is driving the bench`
          + (lk.note ? ` — ${lk.note}` : '') + ` · ${Math.ceil(lk.expires_in_s)}s left`;
      }
    }
    await autoArrangeByStm32(st);
    for (const card of cards) {
      const cid = parseInt(card.dataset.ctrl, 10);
      const c = st.controllers[card.dataset.ctrl];
      if (!c) continue;
      const wasConn = connected[card.dataset.ctrl];
      connected[card.dataset.ctrl] = c.connected;
      if (wasConn && !c.connected && state.invalidateRun) state.invalidateRun();
      // Restore the host field from the backend's live state — the connection
      // outlives a page refresh, so the IP must reappear when still connected.
      if (c.connected && c.host) {
        const h = card.querySelector('[data-host]');
        if (h && h.value !== c.host) h.value = c.host;
      }
      const btn = card.querySelector('[data-connect]');
      btn.textContent = c.connected ? 'Disc' : 'Conn';
      btn.title = c.connected ? 'Disconnect this bridge' : 'Connect to this bridge';
      btn.classList.toggle('quick', !c.connected);

      const ident = card.querySelector('[data-ident]');
      if (ident) ident.textContent = c.connected && c.bridge_name ? c.bridge_name : '';

      const hbGroup = document.querySelector(`.pc-hb[data-ctrl="${card.dataset.ctrl}"]`);
      // dot and master badge now live in the header hbGroup, not in the .pc card
      const dot = hbGroup && hbGroup.querySelector('[data-dot]');
      if (dot) dot.className = 'dot ' + (c.connected ? 'online' : 'offline');
      const mb = hbGroup && hbGroup.querySelector('[data-master]');
      if (mb) mb.classList.toggle('active', cid === master);
      const rp = hbGroup && hbGroup.querySelector('[data-hb="rp"]');
      const stm = hbGroup && hbGroup.querySelector('[data-hb="stm"]');
      if (!rp || !stm) continue;
      const hasStm = c.connected && c.stm32 && c.stm32.ever_seen;
      stm.classList.remove('na');
      stm.title = hasStm
        ? 'STM32 detected on this bridge (HTTP /stm32 age)' + (cid === master ? ' — master' : '')
        : 'No STM32 seen on this bridge';
      if (!c.connected) {
        rp.className = 'hb idle';
        stm.className = 'hb idle';
      } else {
        rp.className = 'hb ' + hbClass(c.rp2350.age_ms, c.rp2350.age_ms != null);
        stm.className = 'hb ' + hbClass(c.stm32.age_ms, c.stm32.ever_seen);
      }
    }
    state.master = master;
    state.connected = { 1: !!connected['1'], 2: !!connected['2'] };
    if (state.refreshRunGate) state.refreshRunGate();
    // Keep datalists and field values consistent on every tick — clears a slot
    // that shows an IP already claimed by a connected slot.
    updateHostLists();
  }

  setInterval(refresh, 1500);
  refresh();

  return { status: () => api('/api/status') };
}
