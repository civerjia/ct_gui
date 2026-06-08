/*
 * Power-controller connection panel.
 *
 * Scans the LAN for ESP32 bridges exposing TCP :3333, connects up to two of
 * them (each with a filament-index offset — 0 for fil 0-47, 48 for 48-95), and
 * polls /api/status to drive the per-controller RP2350 + STM32 heartbeat
 * badges. All hardware I/O lives in the backend; this just talks to /api/*.
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
    scanHint.textContent = 'Scanning for :3333 …';
    try {
      const { results } = await api('/api/scan');
      datalist.innerHTML = '';
      for (const r of results) {
        const o = document.createElement('option');
        o.value = r.host;
        o.label = (r.name ? r.name + ' · ' : '') + (r.controller_responsive ? 'RP2350 ✓' : 'port open');
        datalist.appendChild(o);
      }
      scanHint.textContent = results.length
        ? `Found ${results.length} bridge(s) on :3333 — pick a host below.`
        : 'No :3333 hosts found. Check the ESP32 is powered and on this LAN/AP.';
      // auto-fill blank host fields with discovered IPs, in order
      results.forEach((r, i) => {
        const h = cards[i] && cards[i].querySelector('[data-host]');
        if (h && !h.value) h.value = r.host;
      });
    } catch (e) {
      scanHint.textContent = 'Scan failed: ' + e;
    } finally {
      scanBtn.disabled = false;
    }
  });

  for (const card of cards) {
    const cid = parseInt(card.dataset.ctrl, 10);
    const btn = card.querySelector('[data-connect]');
    const host = card.querySelector('[data-host]');
    const offset = card.querySelector('[data-offset]');
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        if (connected[cid]) {
          await post('/api/disconnect', { controller: cid });
        } else {
          const res = await post('/api/connect', {
            controller: cid,
            host: host.value.trim(),
            offset: parseInt(offset.value, 10) || 0,
          });
          if (!res.ok) scanHint.textContent = `Power ${cid}: ${res.error}`;
        }
      } finally {
        btn.disabled = false;
        refresh();
      }
    });
    offset.addEventListener('change', () => {
      if (connected[cid]) post('/api/offset', { controller: cid, offset: parseInt(offset.value, 10) || 0 });
    });
  }

  async function refresh() {
    let st;
    try { st = await api('/api/status'); } catch { return; }
    for (const card of cards) {
      const c = st.controllers[card.dataset.ctrl];
      if (!c) continue;
      connected[card.dataset.ctrl] = c.connected;
      card.querySelector('[data-dot]').className = 'dot ' + (c.connected ? 'online' : 'offline');
      const btn = card.querySelector('[data-connect]');
      btn.textContent = c.connected ? 'Disconnect' : 'Connect';
      btn.classList.toggle('quick', !c.connected);
      const rp = card.querySelector('[data-hb="rp"]');
      const stm = card.querySelector('[data-hb="stm"]');
      if (!c.connected) {
        rp.className = 'hb idle';
        stm.className = 'hb idle';
      } else {
        rp.className = 'hb ' + hbClass(c.rp2350.age_ms, c.rp2350.age_ms != null);
        stm.className = 'hb ' + hbClass(c.stm32.age_ms, c.stm32.ever_seen);
      }
    }
  }

  setInterval(refresh, 1500);
  refresh();

  // expose for other modules that map a filament index -> controller/offset
  return { status: () => api('/api/status') };
}
