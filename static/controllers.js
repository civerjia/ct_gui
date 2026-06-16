/*
 * Power-controller connection panel.
 *
 * Scans the LAN for ESP32 bridges exposing TCP :3333, connects up to two of
 * them, lets the user pick the MASTER (the bridge that carries the STM32 — all
 * STM32/HV/ADC commands route there), and polls /api/status to drive the
 * per-controller RP2350 + STM32 heartbeat badges. The filament→power mapping is
 * the host active-list (Mapping card). All hardware I/O lives in the backend.
 */

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
  const datalist = document.getElementById('bridgeHosts');
  const cards = [...document.querySelectorAll('.pc')];
  const connected = {};

  scanBtn.addEventListener('click', async () => {
    scanBtn.disabled = true;
    userPinnedMaster = false;   // fresh scan → re-detect the master from STM32 presence
    scanHint.textContent = 'Scanning LAN + 192.168.4.0/24 …';
    try {
      const { results } = await api('/api/scan');
      // responsive controllers first, then port-open — so the best host wins.
      const found = (results || []).slice().sort(
        (a, b) => (b.controller_responsive ? 1 : 0) - (a.controller_responsive ? 1 : 0));
      datalist.innerHTML = '';
      for (const r of found) {
        const o = document.createElement('option');
        o.value = r.host;
        o.label = (r.name ? r.name + ' · ' : '') + (r.controller_responsive ? 'RP2350 ✓' : 'port open');
        datalist.appendChild(o);
      }
      // Assign discovered IPs to the cards (OVERWRITE — the defaults are only
      // valid on the AP and were masking real LAN IPs). Distinct host per card.
      cards.forEach((card, i) => {
        const h = card.querySelector('[data-host]');
        if (h && found[i]) h.value = found[i].host;
      });
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
  // The master carries the STM32 — auto-detected from which connected bridge sees
  // an STM32 (c.stm32.ever_seen). Clicking a master badge PINS a manual choice so
  // auto-detect stops overriding it (until the next scan).
  let userPinnedMaster = false;
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
          if (!res.ok) scanHint.textContent = `Power ${cid}: ${res.error}`;
        }
      } finally {
        btn.disabled = false;
        refresh();
      }
    });
    // master badge — pick which bridge carries the STM32 (all STM32/ADC commands route there)
    const mb = card.querySelector('[data-master]');
    if (mb) mb.addEventListener('click', async () => {
      userPinnedMaster = true;               // manual override — stop auto-detect
      const res = await post('/api/master', { controller: cid });
      if (res && res.master) master = res.master;
      refresh();
    });
  }

  // Auto-pick the master from STM32 presence: if exactly ONE connected bridge sees
  // an STM32, make it master. If both or neither do, leave the current choice for
  // the user. Never overrides a manual pin.
  async function autoDetectMaster(st) {
    if (userPinnedMaster) return;
    const withStm = cards
      .map((c) => parseInt(c.dataset.ctrl, 10))
      .filter((cid) => { const c = st.controllers[String(cid)]; return c && c.connected && c.stm32 && c.stm32.ever_seen; });
    if (withStm.length === 1 && withStm[0] !== master) {
      const res = await post('/api/master', { controller: withStm[0] });
      if (res && res.master) master = res.master;
    }
  }

  async function refresh() {
    let st;
    try { st = await api('/api/status'); } catch { return; }
    if (st.master) master = st.master;
    await autoDetectMaster(st);
    for (const card of cards) {
      const cid = parseInt(card.dataset.ctrl, 10);
      const c = st.controllers[card.dataset.ctrl];
      if (!c) continue;
      connected[card.dataset.ctrl] = c.connected;
      // Restore the host field from the backend's live state — the connection
      // outlives a page refresh, so the IP must reappear when still connected.
      if (c.connected && c.host) {
        const h = card.querySelector('[data-host]');
        if (h && h.value !== c.host) h.value = c.host;
      }
      card.querySelector('[data-dot]').className = 'dot ' + (c.connected ? 'online' : 'offline');
      const btn = card.querySelector('[data-connect]');
      btn.textContent = c.connected ? 'Disconnect' : 'Connect';
      btn.classList.toggle('quick', !c.connected);

      const ident = card.querySelector('[data-ident]');
      if (ident) ident.textContent = c.connected && c.bridge_name ? c.bridge_name : '';

      // master badge: highlight the active master; the badge is always clickable
      const mb = card.querySelector('[data-master]');
      if (mb) mb.classList.toggle('active', cid === master);

      const rp = card.querySelector('[data-hb="rp"]');
      const stm = card.querySelector('[data-hb="stm"]');
      // STM32 badge reflects ACTUAL presence on THIS bridge (c.stm32.ever_seen) —
      // that's what picks the master. A bridge with no STM32 shows a dim/idle badge.
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
    window.ctMaster = master;   // expose for power.js STM32/ADC routing
  }

  setInterval(refresh, 1500);
  refresh();

  // expose for other modules that map a filament index -> controller/offset
  return { status: () => api('/api/status') };
}
