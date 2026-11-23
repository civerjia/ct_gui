/*
 * Filaments card: every filament by PHYSICAL number (FID) -- where it sits,
 * whether the backend refuses to power it (dead, with the reason), and a
 * free-text note. The dead list is the backend's enforced mask
 * (/api/dead-fids); notes are bookkeeping (/api/filament-notes) and decide
 * nothing. Both are stored on the backend's disk and shared with scripts
 * (ct.set_dead / ct.set_filament_note).
 */

import { getJ, postJ } from './net.js';
import { state } from './state.js';

const N = 96;
const REFRESH_MS = 15000;     // a file read on the backend, no hardware

let dead = {};                // fid -> {reason, by, at}
let notes = {};               // fid -> {note, by, at}
let where = {};               // fid -> "P1 CH1.1"
let ringOf = {};              // fid -> ring position
let maxChars = 2000;
let readOnly = null;          // why notes cannot be written, or null
let filter = 'all';
let query = '';
const rowMsg = {};            // fid -> {text, kind} shown under the note
const marking = new Set();    // fids whose "mark dead" reason box is open

const $f = (id) => document.getElementById(id);
const esc = (x) => String(x == null ? '' : x).replace(/[&<>"]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const when = (iso) => (iso ? String(iso).replace('T', ' ').slice(0, 16) : '');
const msg = (text, kind) => { const e = $f('filmMsg'); if (e) { e.textContent = text; e.className = 'summary ' + (kind || ''); } };

const CARD_HTML = `
  <div class="row compact film-tools">
    <div class="seg sm" id="filmFilter">
      <button class="seg-btn active" data-f="all">All</button>
      <button class="seg-btn" data-f="dead">Dead</button>
      <button class="seg-btn" data-f="notes">With notes</button>
    </div>
    <input id="filmSearch" class="film-search" type="search" placeholder="search number, place, note, reason" data-nostick />
  </div>
  <div class="film-table-wrap">
    <table class="film-table">
      <thead><tr>
        <th title="Physical filament number (FID) -- the hardware index scripts and the backend use">#</th>
        <th title="Position on the ring (filament order)">ring</th>
        <th title="Controller, channel and board position">where</th>
        <th title="Dead = the backend refuses to power it, for every script and this GUI">dead</th>
        <th>note</th>
      </tr></thead>
      <tbody id="filmBody"></tbody>
    </table>
  </div>
  <div id="filmMsg" class="summary"></div>`;

function matches(f) {
  if (filter === 'dead' && !dead[f]) return false;
  if (filter === 'notes' && !notes[f]) return false;
  if (!query) return true;
  const hay = [String(f), where[f], dead[f] && dead[f].reason, notes[f] && notes[f].note]
    .filter(Boolean).join(' ').toLowerCase();
  return hay.includes(query);
}

function deadCell(f) {
  const d = dead[f];
  if (d) {
    return `<span class="film-dead-chip" title="${esc(d.reason)}">DEAD</span>`
      + `<span class="film-why">${esc(d.reason) || '(no reason)'}</span>`
      + `<span class="film-by">${esc(d.by)} · ${esc(when(d.at))}</span>`
      + `<button class="xs film-clear" data-fid="${f}" title="Allow filament ${f} to be powered again">Clear</button>`;
  }
  if (marking.has(f)) {
    return `<input class="film-reason" data-fid="${f}" type="text" placeholder="reason (required)" data-nostick />`
      + `<button class="xs danger film-mark-ok" data-fid="${f}">Mark dead</button>`
      + `<button class="xs film-mark-cancel" data-fid="${f}">Cancel</button>`;
  }
  return `<button class="xs film-mark" data-fid="${f}" title="Refuse power to filament ${f}, everywhere">Mark dead…</button>`;
}

function render() {
  const body = $f('filmBody'); if (!body) return;
  const rows = [];
  for (let f = 0; f < N; f++) {
    if (!matches(f)) continue;
    const n = notes[f], m = rowMsg[f];
    rows.push(`<tr class="${dead[f] ? 'is-dead' : ''}" data-fid="${f}">`
      + `<td class="film-id">${f}</td>`
      + `<td class="film-ring">${ringOf[f] == null ? '—' : ringOf[f]}</td>`
      + `<td class="film-where">${esc(where[f] || 'unassigned')}</td>`
      + `<td class="film-dead">${deadCell(f)}</td>`
      + `<td class="film-note"><textarea rows="1" class="film-note-in" data-fid="${f}" maxlength="${maxChars}"`
      + `${readOnly ? ' disabled' : ''} placeholder="note" data-nostick>${esc(n ? n.note : '')}</textarea>`
      + `<div class="film-note-meta ${m ? m.kind : ''}">${m ? esc(m.text) : n ? `${esc(n.by)} · ${esc(when(n.at))}` : ''}</div></td>`
      + '</tr>');
  }
  body.innerHTML = rows.join('') || `<tr><td colspan="5" class="hint">no filament matches</td></tr>`;
  const nd = Object.keys(dead).length, nn = Object.keys(notes).length;
  const c = $f('filmCounts'); if (c) c.textContent = `${nd} dead · ${nn} with notes`;
  if (readOnly) msg(`Notes are read-only: ${readOnly}`, 'bad');
}

async function load() {
  const [d, n, m, o] = await Promise.all([getJ('/api/dead-fids'), getJ('/api/filament-notes'),
    getJ('/api/mapping'), getJ('/api/filament-order')]);
  if (d && d.ok) dead = d.dead || {};
  if (n && n.ok) { notes = n.notes || {}; maxChars = n.max_chars || maxChars; readOnly = n.read_only || null; }
  if (m && m.ok) {
    where = {};
    ((m.mapping && m.mapping.filaments) || []).forEach((r) => {
      if (r.controller === 0 || r.controller === 1) where[r.filament] = `P${r.controller + 1} CH${r.channel + 1}.${r.position + 1}`;
    });
  }
  if (o && o.ok && Array.isArray(o.order)) { ringOf = {}; o.order.forEach((fid, ring) => { ringOf[fid] = ring; }); }
  if (!(d && d.ok) || !(n && n.ok)) msg(`Could not read ${!(d && d.ok) ? 'the dead list' : 'the notes'} from the backend.`, 'bad');
  render();
}

// A periodic refresh must not wipe what someone is typing.
function editing() {
  const a = document.activeElement;
  return !!(a && a.closest && a.closest('#filmBody')) || marking.size > 0;
}

async function saveNote(el) {
  const f = +el.dataset.fid, text = el.value.trim(), old = notes[f] ? notes[f].note : '';
  if (text === old) return;
  rowMsg[f] = { text: 'saving…', kind: '' }; el.nextElementSibling.textContent = 'saving…';
  const r = await postJ('/api/filament-notes', { fid: f, note: text });
  if (r && r.ok) {
    if (r.note) notes[f] = r.note; else delete notes[f];
    delete rowMsg[f];
    el.nextElementSibling.className = 'film-note-meta ok';
    el.nextElementSibling.textContent = r.note ? `saved · ${r.note.by} · ${when(r.note.at)}` : 'note deleted';
  } else {
    rowMsg[f] = { text: `not saved: ${(r && r.error) || 'no reply'}`, kind: 'bad' };
    el.nextElementSibling.className = 'film-note-meta bad';
    el.nextElementSibling.textContent = rowMsg[f].text;
  }
}

async function markDead(f) {
  const box = document.querySelector(`.film-reason[data-fid="${f}"]`);
  const reason = box ? box.value.trim() : '';
  if (!reason) { msg(`Filament ${f}: give a reason -- nothing clears a dead entry on its own, so it has to say why.`, 'bad'); return; }
  const r = await postJ('/api/dead-fids', { op: 'add', fids: [f], reason });
  if (r && r.ok) { marking.delete(f); msg(`Filament ${f} marked dead -- it will be refused power.`, 'ok'); }
  else msg(`Filament ${f} not marked: ${(r && r.error) || 'no reply'}`, 'bad');
  await load();
  if (state.syncBackendDead) state.syncBackendDead();
}

async function clearDead(f) {
  if (!confirm(`Clear filament ${f} from the dead list?\n\nEvery script and this GUI may then power it again. `
    + 'A script that sets the dead list at start-up will put it back.')) return;
  const r = await postJ('/api/dead-fids', { op: 'remove', fids: [f] });
  msg(r && r.ok ? `Filament ${f} cleared -- it may be powered again.` : `Not cleared: ${(r && r.error) || 'no reply'}`,
    r && r.ok ? 'ok' : 'bad');
  await load();
  if (state.syncBackendDead) state.syncBackendDead();
}

export function initFilaments() {
  const host = $f('filamentsCard'); if (!host) return;
  host.innerHTML = CARD_HTML;
  document.querySelectorAll('#filmFilter .seg-btn').forEach((b) => b.addEventListener('click', () => {
    filter = b.dataset.f;
    document.querySelectorAll('#filmFilter .seg-btn').forEach((x) => x.classList.toggle('active', x === b));
    render();
  }));
  $f('filmSearch').addEventListener('input', (e) => { query = e.target.value.trim().toLowerCase(); render(); });
  const body = $f('filmBody');
  body.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    const f = +b.dataset.fid;
    if (b.classList.contains('film-mark')) {
      marking.add(f); render();
      const box = document.querySelector(`.film-reason[data-fid="${f}"]`); if (box) box.focus();
    } else if (b.classList.contains('film-mark-cancel')) { marking.delete(f); render(); }
    else if (b.classList.contains('film-mark-ok')) markDead(f);
    else if (b.classList.contains('film-clear')) clearDead(f);
  });
  body.addEventListener('keydown', (e) => {
    if (e.target.classList.contains('film-reason') && e.key === 'Enter') markDead(+e.target.dataset.fid);
    if (e.target.classList.contains('film-note-in') && e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); e.target.blur(); }
  });
  // focusout bubbles (blur does not): save when leaving a note box
  body.addEventListener('focusout', (e) => { if (e.target.classList.contains('film-note-in')) saveNote(e.target); });
  load();
  setInterval(() => { if (!editing() && !document.hidden) load(); }, REFRESH_MS);
}
