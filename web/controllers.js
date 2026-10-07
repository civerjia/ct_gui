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
    scanHint.textContent = 'Finding and connecting the controllers …';
    try {
      // The BACKEND scans and connects (STM32 board -> Power 1); the cards
      // only show the result. Deduplicated by device there already.
      const res = await post('/api/auto-connect', {});
      if (res && res.ok === false && res.error) { scanHint.textContent = res.error; return; }
      const found = (res && res.found) || [];
      allFoundHosts = found.map((r) => ({
        host: r.host,
        name: r.name,
        label: (r.name ? r.name + ' · ' : '') + (r.has_stm32 ? 'STM32 (master)' : 'no STM32')
          + (r.busy ? ' · held by another client' : ''),
      }));
      await refresh();

      // Fill only the cards that are NOT connected, and only with hosts no
      // connected card already holds. Filling every card in scan order used to
      // put a connected bridge's IP into the free card -- which the duplicate
      // rule in updateHostLists() then wiped, so the other bridge's IP vanished
      // from both cards.
      const taken = new Set();
      for (const card of cards) {
        const h = card.querySelector('[data-host]');
        if (connected[card.dataset.ctrl] && h && h.value) taken.add(h.value.trim());
      }
      const free = allFoundHosts.map((r) => r.host).filter((x) => !taken.has(x));
      let k = 0;
      for (const card of cards) {
        if (connected[card.dataset.ctrl]) continue;
        const h = card.querySelector('[data-host]');
        if (h && free[k]) h.value = free[k++];
      }

      // Rebuild datalists with the fresh results (each excludes the other slot).
      updateHostLists();

      if (!found.length) {
        scanHint.textContent = 'No bridge found. Join the CTPower-XXXXXX AP or check the ESP32 is powered, then scan again.';
      } else {
        const conn = Object.entries((res && res.connected) || {}).map(([c, h]) => `Power ${c} → ${h}`);
        const busy = found.filter((r) => r.busy).map((r) => r.host);
        const fail = Object.entries((res && res.failed) || {}).map(([c, e]) => `Power ${c}: ${e}`);
        scanHint.textContent = `Found ${found.length} bridge(s)`
          + (conn.length ? ` — connected ${conn.join(', ')}` : ' — nothing new to connect')
          + (busy.length ? ` · held by another client: ${busy.join(', ')}` : '')
          + (fail.length ? ` · failed: ${fail.join('; ')}` : '');
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
  // Returns true if it swapped the slots (the caller must then re-read the
  // status: the one it holds describes the arrangement BEFORE the swap).
  async function autoArrangeByStm32(st) {
    if (userPinnedMaster || hasAutoArranged) return false;
    const c1 = st.controllers['1'];
    const c2 = st.controllers['2'];
    const c1HasStm = c1 && c1.connected && c1.stm32 && c1.stm32.ever_seen;
    const c2HasStm = c2 && c2.connected && c2.stm32 && c2.stm32.ever_seen;
    if (!c1HasStm && !c2HasStm) return false;   // STM32 not visible on either yet — wait
    hasAutoArranged = true;
    let swapped = false;
    if (c2HasStm && !c1HasStm) {
      swapped = true;
      // STM32 is on P2 → disconnect both, swap IP slots, reconnect
      const host2 = c2.host;
      const c1Conn = !!(c1 && c1.connected);
      // Slot 1's IP moves to slot 2 -- the connected one, or, when slot 1 is
      // not connected, whatever the scan or the user put in its field. It used
      // to be dropped in that case, so the other bridge's IP disappeared.
      const typed1 = ((hostInputOf(1) || {}).value || '').trim();
      const host1 = c1Conn ? c1.host : (typed1 && typed1 !== host2 ? typed1 : null);
      if (c2.connected) await post('/api/disconnect', { controller: 2 });
      if (c1Conn) await post('/api/disconnect', { controller: 1 });
      // Swap the IP fields first so the UI reflects the new arrangement
      const h1 = hostInputOf(1); if (h1) h1.value = host2;
      const h2 = hostInputOf(2); if (h2) h2.value = host1 || '';
      await post('/api/connect', { controller: 1, host: host2 });
      // Reconnect slot 2 only if it was connected before; otherwise leave its
      // IP in the field for the user to connect.
      if (c1Conn && host1) await post('/api/connect', { controller: 2, host: host1 });
    }
    // Master is always slot 1 (now guaranteed to hold the STM32 bridge)
    if (master !== 1) {
      const res = await post('/api/master', { controller: 1 });
      if (res && res.master) master = res.master;
    }
    return swapped;
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

  // One refresh at a time. The 1.5 s timer and the Scan/Conn handlers both call
  // it; two overlapping runs could write one run's pre-swap status over the
  // other's post-swap result (and the duplicate rule then wiped an IP).
  let refreshing = null;
  function refresh() {
    if (!refreshing) refreshing = doRefresh().finally(() => { refreshing = null; });
    return refreshing;
  }

  async function doRefresh() {
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
    // After a swap, the status read above is stale: using it would write the
    // pre-swap host back into the cards (and the duplicate rule would then wipe
    // the other bridge's IP). Read it again.
    if (await autoArrangeByStm32(st)) {
      try { st = await api('/api/status'); } catch { return; }
      if (st.master) master = st.master;
    }
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
      const chip = hbGroup.querySelector('[data-link]');
      if (chip) renderLinkChip(chip, c, cid);
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

  // WiFi signal + recent latency of one bridge. Two latencies on purpose: the
  // ESP32 alone (its HTTP) and the RP2350 behind it -- slow ESP32 too = the
  // WiFi; ESP32 fast but RP2350 slow = the controller / its UART.
  function renderLinkChip(chip, c, cid) {
    const L = c.link || {};
    const rssi = L.rssi_dbm, esp = L.esp32_http_avg_ms, rp = L.rp2350_avg_ms;
    const failed = (L.esp32_http_failed || 0) + (L.rp2350_timeouts || 0);
    if (!c.connected && rssi == null && esp == null) {
      chip.className = 'link-chip idle'; chip.textContent = '—';
      chip.title = `Power ${cid} — not connected`; return;
    }
    const sig = rssi == null ? 'unknown' : rssi >= -60 ? 'good' : rssi >= -70 ? 'fair' : 'poor';
    const slow = (esp != null && esp > 200) || (rp != null && rp > 200) || failed > 0;
    const bad = sig === 'poor' || (L.esp32_http_requests && L.esp32_http_failed >= L.esp32_http_requests / 2);
    chip.className = 'link-chip ' + (bad ? 'bad' : slow || sig === 'fair' ? 'warn' : sig === 'unknown' ? 'idle' : 'ok');
    const ms = (v) => (v == null ? '—' : `${Math.round(v)} ms`);
    chip.textContent = `${rssi == null ? '? dBm' : rssi + ' dBm'} · ${ms(esp)}`;
    chip.title = [
      `Power ${cid} link, last ${L.window_s || 60} s`,
      `WiFi signal: ${rssi == null ? (L.wifi_error || 'unknown') : `${rssi} dBm (${sig})`}`
        + (L.channel != null ? ` · ch ${L.channel}` : '') + (L.bandwidth ? ` · ${L.bandwidth}` : '')
        + (L.power_save && L.power_save !== 'none' ? ` · power save ${L.power_save}` : ''),
      `ESP32 itself (HTTP): avg ${ms(esp)}, p95 ${ms(L.esp32_http_p95_ms)}, ${L.esp32_http_failed || 0} of ${L.esp32_http_requests || 0} failed`,
      `RP2350 behind it: avg ${ms(rp)}, p95 ${ms(L.rp2350_p95_ms)}, ${L.rp2350_timeouts || 0} of ${L.rp2350_requests || 0} timed out`,
      'Both slow = the WiFi. ESP32 fast, RP2350 slow = the controller or its UART.',
    ].join('\n');
  }

  setInterval(refresh, 1500);
  refresh();

  return { status: () => api('/api/status') };
}
