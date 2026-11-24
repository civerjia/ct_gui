/*
 * Data heartbeat: one pill per data source in the header, so it is plain
 * whether the numbers on the page are really moving.
 *
 *   ● Boards 0.4 s     ● Vacuum 0.9 s     ● Spellman 1.1 s     ● HV monitor 0.6 s
 *
 * The age is the age of the DATA (the backend's / firmware's own timestamp),
 * not of the page's last request: a page that polls happily while the backend
 * serves the same stale snapshot still turns amber, then red. The dot pulses
 * once per genuinely NEW sample. Green under FRESH_S, amber under STALE_S,
 * red beyond (or when there is no reading at all).
 */

const FRESH_S = 3;     // a sample younger than this reads as live
const STALE_S = 10;    // older than this, or none: red
const RENDER_MS = 250;

const ORDER = [
  ['boards', 'Boards', 'Heating V / I of every board: the firmware board cache the backend monitor reads'],
  ['vac', 'Vacuum', 'The Lesker gauge (raspberrypi5), via the backend'],
  ['spellman', 'Spellman', 'Spellman kV / mA, via the Spellman backend'],
  ['hvmon', 'HV monitor', 'Emission / focus monitor (STM32 ADS1115) on the master controller'],
];

const streams = {};   // name -> {dataAt (ms, page clock) | null, seenAt, pulse}

/** A poll for `name` came back. ageMs = how old the newest sample is
 *  (null / undefined = the poll had no reading). */
export function beat(name, ageMs) {
  const now = performance.now();
  const s = streams[name] || (streams[name] = { dataAt: null, seenAt: 0, pulse: false });
  s.seenAt = now;
  if (ageMs == null || !Number.isFinite(ageMs)) { s.dataAt = null; return; }
  const dataAt = now - Math.max(0, ageMs);
  if (s.dataAt == null || dataAt > s.dataAt + 100) s.pulse = true;   // a newer sample
  s.dataAt = s.dataAt == null ? dataAt : Math.max(s.dataAt, dataAt);
}

function render() {
  const bar = document.getElementById('liveBar'); if (!bar) return;
  const now = performance.now();
  for (const [name] of ORDER) {
    const pill = bar.querySelector(`[data-stream="${name}"]`); if (!pill) continue;
    const s = streams[name];
    const age = s && s.dataAt != null ? (now - s.dataAt) / 1000 : null;
    const cls = age == null ? 'bad' : age < FRESH_S ? 'ok' : age < STALE_S ? 'warn' : 'bad';
    pill.className = `live-pill ${cls}`;
    pill.querySelector('.live-age').textContent =
      age == null ? (s ? 'no data' : '—') : age < 60 ? `${age.toFixed(1)} s` : `${Math.round(age / 60)} min`;
    if (s && s.pulse) {
      s.pulse = false;
      const dot = pill.querySelector('.live-dot');
      dot.classList.remove('beat'); void dot.offsetWidth; dot.classList.add('beat');   // restart
    }
  }
}

export function initLiveness() {
  const bar = document.getElementById('liveBar'); if (!bar) return;
  bar.innerHTML = ORDER.map(([name, label, title]) =>
    `<span class="live-pill bad" data-stream="${name}" title="${title}. Age of the newest sample; the dot blinks on each new one.">`
    + `<i class="live-dot"></i>${label} <b class="live-age">—</b></span>`).join('');
  setInterval(render, RENDER_MS);
}
