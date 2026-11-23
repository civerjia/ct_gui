/*
 * Bake-out card: the BACKEND's bake-out job (/api/bake). Every filament rests
 * at its CC IDLE and takes a turn at a fixed voltage, one at a time, around the
 * ring, paced by the vacuum. It runs in the backend: closing this page does
 * not stop it; Stop does (never refused, whoever holds the lease).
 */

import { getJ, postJ } from './net.js';

const POLL_MS = 2000;
const $b = (id) => document.getElementById(id);
const esc = (x) => String(x == null ? '' : x).replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const torr = (v) => (v == null ? '—' : Number(v).toExponential(2));
const msg = (text, kind) => { const e = $b('bakeMsg'); if (e) { e.textContent = text; e.className = 'summary ' + (kind || ''); } };

const CARD_HTML = `
  <div class="bake-form">
    <label class="numlabel" title="Voltage each filament is held at during its turn">V <input id="bakeV" type="number" min="0.8" max="15" step="0.1" value="5" data-nostick /></label>
    <label class="numlabel" title="Seconds at that voltage per turn">hold s <input id="bakeHold" type="number" min="1" max="60" step="0.5" value="5" data-nostick /></label>
    <label class="numlabel" title="CC IDLE current every filament rests at between turns">idle A <input id="bakeIdle" type="number" min="0.1" max="2" step="0.05" value="1" data-nostick /></label>
    <label class="numlabel" title="Rounds of the ring; 0 = until Stop">cycles <input id="bakeCycles" type="number" min="0" step="1" value="0" data-nostick /></label>
    <label class="numlabel bake-skip" title="Physical filament numbers (FID) to leave out, e.g. 26 40. Dead filaments are always skipped.">skip <input id="bakeSkip" type="text" placeholder="e.g. 26" data-nostick /></label>
  </div>
  <details class="bake-adv">
    <summary>Vacuum pacing <span class="hint">— empty = 0.5 / 0.4 / 0.8 × the limit</span></summary>
    <div class="bake-form">
      <label class="numlabel" title="Before a turn: wait while the pressure is above this (Torr)">pause above <input id="bakePause" type="text" placeholder="auto" data-nostick /></label>
      <label class="numlabel" title="…and go on once it is under this (Torr)">resume below <input id="bakeResume" type="text" placeholder="auto" data-nostick /></label>
      <label class="numlabel" title="During a turn: above this the filament goes back to IDLE at once (Torr)">cut above <input id="bakeCut" type="text" placeholder="auto" data-nostick /></label>
    </div>
  </details>
  <div class="row compact bake-actions">
    <button id="bakeStart" class="xs quick">Start bake-out</button>
    <button id="bakeStop" class="xs danger">Stop</button>
    <span id="bakeState" class="bake-state">—</span>
  </div>
  <div id="bakeCycle" class="summary"></div>
  <div class="film-table-wrap bake-table-wrap">
    <table class="film-table">
      <thead><tr><th>time</th><th>cycle</th><th>#</th><th>ok</th><th>mA</th><th>vacuum before → peak</th><th>rise</th><th></th></tr></thead>
      <tbody id="bakeRows"></tbody>
    </table>
  </div>
  <div id="bakeMsg" class="summary"></div>`;

function torrInput(id) {
  const t = $b(id).value.trim();
  if (!t) return undefined;
  const v = Number(t);
  if (!(v > 0)) throw new Error(`${id.replace('bake', '')}: "${t}" is not a pressure in Torr`);
  return v;
}

async function start() {
  let body;
  try {
    const skip = $b('bakeSkip').value.trim().split(/[\s,]+/).filter(Boolean).map(Number);
    if (skip.some((x) => !Number.isInteger(x))) throw new Error('skip: whole filament numbers, e.g. "26 40"');
    body = {
      action: 'start', mv: Math.round(Number($b('bakeV').value) * 1000), hold_s: Number($b('bakeHold').value),
      idle_ma: Math.round(Number($b('bakeIdle').value) * 1000), cycles: parseInt($b('bakeCycles').value, 10) || 0,
      skip, pause_above: torrInput('bakePause'), resume_below: torrInput('bakeResume'), cut_above: torrInput('bakeCut'),
    };
  } catch (e) { msg(e.message, 'bad'); return; }
  const r = await postJ('/api/bake', body);
  msg(r && r.ok ? `Started — log ${r.log}` : `Not started: ${(r && r.error) || 'no reply'}`, r && r.ok ? 'ok' : 'bad');
  poll();
}

async function stop() {
  const r = await postJ('/api/bake', { action: 'stop' });
  msg(r && r.ok ? (r.running ? 'Stopping — the current filament goes back to IDLE.' : 'Nothing was running.')
    : `Stop failed: ${(r && r.error) || 'no reply'}`, r && r.ok ? 'ok' : 'bad');
  poll();
}

function render(s) {
  const st = $b('bakeState'); if (!st) return;
  const vac = s.vacuum || {};
  let line;
  if (s.running) {
    line = `<b class="bake-on">running</b> · cycle ${s.cycle} · ${s.turns} turns`
      + (s.current != null ? ` · baking #${s.current}` : '')
      + (s.waiting ? ` · <b class="bake-wait">waiting for the vacuum</b> since ${esc(s.waiting.since)} (resume below ${torr(s.waiting.resume_below)})` : '')
      + (s.stop ? ' · stopping…' : '');
  } else {
    line = s.started ? `stopped · ${esc(s.error ? 'error: ' + s.error : s.stopped_by || '')} · ${esc(s.finished || '')}` : 'not running';
  }
  st.innerHTML = `${line} · vacuum ${torr(vac.pressure_torr)} Torr (limit ${torr(vac.limit_torr)})`;
  $b('bakeStart').disabled = !!s.running;
  $b('bakeStop').disabled = !s.running;
  const c = (s.cycles || [])[s.cycles.length - 1];
  $b('bakeCycle').textContent = c
    ? `cycle ${c.cycle} (${c.finished}): ${c.ok}/${c.turns} ok, peak ${torr(c.vac_peak)} Torr`
      + (c.cut_short.length ? `, cut short ${c.cut_short.join(', ')}` : '')
      + (c.failed.length ? `, failed ${c.failed.join(', ')}` : '')
      + ` · gassiest: ${c.gassiest.map((g) => `#${g.fid} +${torr(g.rise)}`).join(', ')}`
    : (s.params ? `${s.params.mv} mV for ${s.params.hold_s} s, IDLE ${s.params.idle_ma} mA; pause above ${torr(s.params.pause_above)}, cut above ${torr(s.params.cut_above)}` : '');
  const rows = (s.recent || []).slice(-12).reverse();
  $b('bakeRows').innerHTML = rows.map((r) => `<tr class="${r.ok ? '' : 'is-dead'}">`
    + `<td>${esc(r.time)}</td><td>${r.cycle}</td><td class="film-id">${r.fid}</td><td>${r.ok ? '✓' : '✗'}</td>`
    + `<td>${r.mA == null ? '—' : r.mA}</td><td>${torr(r.vac_before)} → ${torr(r.vac_peak)}</td>`
    + `<td>${torr(r.rise)}</td><td>${r.cut_short ? 'cut short' : ''} ${esc(r.error)}</td></tr>`).join('')
    || '<tr><td colspan="8" class="hint">no turns yet</td></tr>';
}

async function poll() {
  const s = await getJ('/api/bake');
  if (s && s.ok) render(s);
}

export function initBake() {
  const host = $b('bakeCard'); if (!host) return;
  host.innerHTML = CARD_HTML;
  $b('bakeStart').onclick = start;
  $b('bakeStop').onclick = stop;
  poll();
  setInterval(() => {
    const card = host.closest('details');
    if (!document.hidden && (!card || card.open)) poll();
  }, POLL_MS);
}
