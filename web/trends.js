/*
 * Supply & vacuum trend: Spellman V / I, emission V / I (ADS1115) and the
 * vacuum, one strip each on a shared time axis (different units -> separate
 * y-axes, never one dual-axis plot). Data from /api/series: every reading as
 * the backend received it (Spellman broadcasts, pressure points, ADS reads),
 * fetched incrementally (4x a second while the card is open, once a second
 * when folded). Drawn only while the card is open, on the BACKEND's clock: the
 * points are stamped there, and a browser clock 1 s behind it (the rig PC,
 * 2026-10-28) put the newest second past the right edge.
 *
 * Zoom and pan over the last 24 h (the user, 2026-11-11): wheel zooms around
 * the cursor, drag pans, double-click goes back to live. The live hour is the
 * incremental feed above; anything older or wider comes from
 * /api/series?from=&to=&buckets= (raw readings when zoomed in, a min/max
 * envelope zoomed out -- a spike survives either).
 */
import { $ } from './dom.js';
import { postJ } from './net.js';

const KEEP_S = 3600;
const RANGE_S = 24 * 3600, MIN_SPAN_S = 20;
const TICKS = [5, 10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600];
const STRIPS = [
  { key: 'sp_kv', label: 'Spellman V', unit: 'kV', color: '#2f6db5', fmt: (v) => v.toFixed(2) },
  { key: 'sp_ma', label: 'Spellman I', unit: 'mA', color: '#7a4fb3', fmt: (v) => v.toFixed(3) },
  { key: 'em_v', label: 'Emission V', unit: 'V', color: '#1f8a70', fmt: (v) => v.toFixed(1) },
  { key: 'em_i', label: 'Emission I', unit: 'mA', color: '#c46a1b', fmt: (v) => v.toFixed(2) },
  { key: 'vac', label: 'Vacuum', unit: 'Torr', color: '#56636b', fmt: (v) => v.toExponential(2), log: true, limit: null },   // limit: the backend's
  { key: 'gmc_cpm', label: 'Radiation', unit: 'cpm', color: '#a3342b', fmt: (v) => v.toFixed(0) },   // GMC-300S, control2
];
const STRIP_H = 58, GAP = 6, AXIS_H = 18, LEFT = 84, RIGHT = 8;

export function initTrends() {
  const card = $('trendCard'), canvas = $('trendCanvas');
  if (!card || !canvas) return;
  const data = Object.fromEntries(STRIPS.map((s) => [s.key, []]));
  let last = 0, windowS = 120, hoverX = null;
  let clockOffset = 0;   // backend time - browser time, s (from each reply's `now`)
  // The view: live (right edge = now) or parked with its right edge at endT.
  let follow = true, endT = 0;
  let vacEdited = false;   // the limit input holds a value not yet Set
  let rng = null, rngBusy = false, rngWant = null, rngAt = 0;   // /api/series?from= reply
  const nowT = () => Date.now() / 1000 + clockOffset;
  const view = () => {
    const t1 = follow ? nowT() : endT;
    return [t1 - windowS, t1];
  };
  const plotW = () => (canvas.clientWidth || 340) - LEFT - RIGHT;

  const seg = $('trendWindow'), viewLbl = $('trendView');
  const markSeg = () => {
    if (seg) seg.querySelectorAll('button').forEach((x) => x.classList.toggle('active', follow && +x.dataset.s === windowS));
    const hm = (t) => new Date(t * 1000).toTimeString().slice(0, 5);
    if (viewLbl) viewLbl.textContent = follow ? 'live' : `${hm(endT - windowS)}–${hm(endT)}`;
  };
  // Clamp a view to [now - 24 h, now]; a right edge at now is live again.
  function setView(t0, t1) {
    const now = nowT();
    let span = Math.min(RANGE_S, Math.max(MIN_SPAN_S, t1 - t0));
    t1 = t0 + span;
    if (t1 >= now - Math.max(1, span * 0.01)) { follow = true; windowS = span; markSeg(); draw(); return; }
    if (t0 < now - RANGE_S) { t0 = now - RANGE_S; t1 = t0 + span; }
    follow = false; windowS = span; endT = t1;
    markSeg(); draw();
  }
  if (seg) seg.querySelectorAll('button').forEach((b) => {
    b.onclick = () => { follow = true; windowS = +b.dataset.s; markSeg(); draw(); };
  });
  canvas.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = canvas.getBoundingClientRect(), [t0, t1] = view();
    const fx = Math.min(1, Math.max(0, (e.clientX - r.left - LEFT) / plotW()));
    const k = Math.exp(Math.max(-1, Math.min(1, e.deltaY / 300)));   // down = out
    const span = Math.min(RANGE_S, Math.max(MIN_SPAN_S, windowS * k));
    const tc = t0 + fx * (t1 - t0);
    if (follow && k < 1) { windowS = span; markSeg(); draw(); return; }   // live stays live zooming in
    setView(tc - fx * span, tc - fx * span + span);
  }, { passive: false });
  let drag = null;
  canvas.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    drag = { x: e.clientX, t1: view()[1], moved: false };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener('pointermove', (e) => {
    const r = canvas.getBoundingClientRect(); hoverX = e.clientX - r.left;
    if (drag) {
      const dt = ((e.clientX - drag.x) / plotW()) * windowS;
      if (Math.abs(e.clientX - drag.x) > 2) drag.moved = true;
      if (drag.moved) { setView(drag.t1 - dt - windowS, drag.t1 - dt); return; }
    }
    draw();
  });
  const endDrag = () => { drag = null; canvas.style.cursor = ''; };
  canvas.addEventListener('pointerup', endDrag);
  canvas.addEventListener('pointercancel', endDrag);
  canvas.addEventListener('dblclick', () => { follow = true; markSeg(); draw(); });
  canvas.addEventListener('mouseleave', () => { hoverX = null; draw(); });

  // Older or wider than the live hour: ask the backend for the view, about
  // one bucket per pixel. One request at a time; the newest view wins.
  function liveCovers(t0) {
    const a = data.sp_kv.length ? data.sp_kv : data.em_i;
    return windowS <= KEEP_S && a.length && a[0][0] <= t0 + 5;
  }
  function needRange(t0, t1) {
    if (liveCovers(t0)) return false;
    if (!rng || rngWant) return !rngWant;
    const tol = windowS * 0.05, span = rng.to - rng.from;
    if (span > windowS * 2.5 || span < windowS * 0.6) return true;            // resolution
    if (t0 < rng.from - tol) return true;
    if (follow) return Date.now() - rngAt > 15000;                           // live: refresh the old part
    return t1 > rng.to + tol;
  }
  async function fetchRange(t0, t1) {
    if (rngBusy) { rngWant = [t0, t1]; return; }
    rngBusy = true;
    try {
      // a margin each side so a small pan does not refetch
      const m = windowS * 0.5, a = t0 - m, b = t1 + m;
      const n = Math.round(plotW() * (b - a) / windowS);
      const r = await (await fetch(`/api/series?from=${a.toFixed(1)}&to=${b.toFixed(1)}&buckets=${Math.max(50, n)}`)).json();
      if (r.ok) { rng = r; rngAt = Date.now(); }
    } catch { /* keep the last; the next draw asks again */ }
    rngBusy = false;
    if (rngWant) { const w = rngWant; rngWant = null; if (needRange(...w)) fetchRange(...w); }
    draw();
  }

  async function fetchNew() {
    try {
      const t0 = Date.now() / 1000;
      const r = await (await fetch(`/api/series?since=${last}&max_points=4000`)).json();
      if (!r.ok) return;
      if (typeof r.now === 'number') clockOffset = r.now - (t0 + Date.now() / 1000) / 2;
      if (typeof r.vacuum_limit_torr === 'number') {
        STRIPS[4].limit = r.vacuum_limit_torr;
        // Not while someone is typing a new limit: clicking Set blurs the
        // input first, and a refresh landing in that gap put the old value
        // back -- Set then sent the old limit (2026-11-11, "takes several clicks").
        const inp = $('vacLimit');
        if (inp && !vacEdited && document.activeElement !== inp) inp.value = r.vacuum_limit_torr.toExponential(1);
      }
      for (const s of STRIPS) {
        const pts = (r.series || {})[s.key] || [];
        if (pts.length) data[s.key].push(...pts);
        const cut = r.now - KEEP_S;
        const a = data[s.key];
        let i = 0; while (i < a.length && a[i][0] < cut) i++;
        if (i) a.splice(0, i);
      }
      last = Math.max(last, ...STRIPS.map((s) => { const a = data[s.key]; return a.length ? a[a.length - 1][0] : 0; }));
    } catch { /* keep the last data; try again next tick */ }
  }

  function draw() {
    if (!card.open) return;
    const dpr = Math.max(1, window.devicePixelRatio || 1);
    const W = canvas.clientWidth || 340, H = STRIPS.length * (STRIP_H + GAP) + AXIS_H;
    canvas.style.height = H + 'px';
    canvas.width = W * dpr; canvas.height = H * dpr;
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const [t0, t1] = view(), pw = W - LEFT - RIGHT, tNow = nowT();
    const useLive = liveCovers(t0);
    if (!useLive && needRange(t0, t1)) fetchRange(t0, t1);
    const gapS = Math.max(10, windowS / 30, 3 * (useLive || !rng ? 0 : rng.bucket_s || 0));
    // the points of one strip in view: the live feed, or the range reply
    // (live mode: plus the live points newer than it)
    const ptsOf = (key) => {
      const live = data[key];
      if (useLive) return live.filter((p) => p[0] >= t0 - 1 && p[0] <= t1 + 1);
      const old = ((rng && rng.series) || {})[key] || [];
      const end = old.length ? old[old.length - 1][0] : (rng ? rng.from : t0);
      const out = old.filter((p) => p[0] >= t0 - 1 && p[0] <= t1 + 1);
      if (follow) for (const p of live) if (p[0] > end && p[0] >= t0 - 1) out.push(p);
      return out;
    };
    const xOf = (t) => LEFT + ((t - t0) / windowS) * pw;
    ctx.font = '10px ui-monospace, Menlo, monospace';
    const hoverT = hoverX != null && hoverX >= LEFT ? t0 + ((hoverX - LEFT) / pw) * windowS : null;

    STRIPS.forEach((s, k) => {
      const y0 = k * (STRIP_H + GAP), pts = ptsOf(s.key);
      // frame + label + latest value (text in ink, the line carries the colour)
      ctx.strokeStyle = '#e3e0d6'; ctx.lineWidth = 1;
      ctx.strokeRect(LEFT + 0.5, y0 + 0.5, pw - 1, STRIP_H - 1);
      ctx.fillStyle = '#3c4348'; ctx.textAlign = 'left';
      ctx.fillText(s.label, 2, y0 + 11);
      const latest = data[s.key].length ? data[s.key][data[s.key].length - 1] : null;
      ctx.fillStyle = latest ? '#1d2327' : '#9aa3a8';
      ctx.fillText(latest && tNow - latest[0] < 15 ? `${s.fmt(latest[1])} ${s.unit}` : 'no data', 2, y0 + 24);
      if (!pts.length) return;
      const f = s.log ? (v) => Math.log10(Math.max(v, 1e-12)) : (v) => v;
      let lo = Infinity, hi = -Infinity;
      for (const p of pts) { const v = f(p[1]); if (v < lo) lo = v; if (v > hi) hi = v; }
      if (s.limit) { hi = Math.max(hi, f(s.limit)); }
      if (hi - lo < (s.log ? 0.2 : 1e-6)) { const m = (hi + lo) / 2, d = s.log ? 0.1 : Math.max(Math.abs(m) * 0.01, 1e-3); lo = m - d; hi = m + d; }
      const pad = (hi - lo) * 0.08; lo -= pad; hi += pad;
      const yOf = (v) => y0 + STRIP_H - 3 - ((f(v) - lo) / (hi - lo)) * (STRIP_H - 6);
      // y range (muted, inside the plot's right edge -- the left column is
      // the label and the latest value)
      // Decimals from the span, not the size: -197.6..-198.0 must not both read "-198".
      const dec = Math.max(0, Math.min(4, Math.ceil(-Math.log10(Math.max(hi - lo - 2 * pad, 1e-9))) + 1));
      const lab = (x) => (s.log ? Math.pow(10, x).toExponential(1) : x.toFixed(dec));
      if (s.limit) {
        const yl = yOf(s.limit);
        ctx.strokeStyle = '#d0453d'; ctx.setLineDash([4, 3]);
        ctx.beginPath(); ctx.moveTo(LEFT, yl); ctx.lineTo(LEFT + pw, yl); ctx.stroke(); ctx.setLineDash([]);
      }
      ctx.save();
      ctx.beginPath(); ctx.rect(LEFT, y0, pw, STRIP_H); ctx.clip();
      ctx.strokeStyle = s.color; ctx.lineWidth = 1.5; ctx.lineJoin = 'round';
      ctx.beginPath();
      let prev = null;
      for (const p of pts) {
        const x = xOf(p[0]), y = yOf(p[1]);
        if (prev && p[0] - prev > gapS) ctx.moveTo(x, y);   // a gap is not a line
        else if (prev == null) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        prev = p[0];
      }
      ctx.stroke();
      ctx.restore();
      // range labels on top of the line, on a backing box
      ctx.textAlign = 'right';
      for (const [txt, ty] of [[lab(hi - pad), y0 + 10], [lab(lo + pad), y0 + STRIP_H - 4]]) {
        const tw = ctx.measureText(txt).width;
        ctx.fillStyle = 'rgba(252,251,247,0.85)';
        ctx.fillRect(LEFT + pw - 5 - tw, ty - 9, tw + 4, 11);
        ctx.fillStyle = '#8b949a';
        ctx.fillText(txt, LEFT + pw - 3, ty);
      }
      if (hoverT != null) {
        let best = null;
        for (const p of pts) if (!best || Math.abs(p[0] - hoverT) < Math.abs(best[0] - hoverT)) best = p;
        if (best && Math.abs(best[0] - hoverT) < Math.max(3, windowS / 60)) {
          const x = xOf(best[0]), y = yOf(best[1]);
          ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(x, y, 3, 0, 2 * Math.PI); ctx.fill();
          ctx.fillStyle = '#1d2327'; ctx.textAlign = x > LEFT + pw - 90 ? 'right' : 'left';
          ctx.fillText(`${s.fmt(best[1])} ${s.unit}`, x + (ctx.textAlign === 'left' ? 6 : -6), y0 + 11);
        }
      }
    });
    // time axis
    const ya = STRIPS.length * (STRIP_H + GAP) + 12;
    ctx.fillStyle = '#8b949a'; ctx.textAlign = 'center';
    const step = TICKS.find((st) => windowS / st <= Math.max(3, pw / 90)) || TICKS[TICKS.length - 1];
    // ticks on local wall-clock boundaries (an hour tick at :00, not UTC-offset)
    const tz = -new Date(t0 * 1000).getTimezoneOffset() * 60;
    for (let s = Math.ceil((t0 + tz) / step) * step - tz; s <= t1; s += step) {
      const x = xOf(s);
      if (x < LEFT + 22 || x > LEFT + pw - 22) continue;     // would be clipped
      ctx.fillText(new Date(s * 1000).toTimeString().slice(0, step < 60 ? 8 : 5), x, ya);
    }
    if (hoverT != null && !drag) {
      ctx.strokeStyle = 'rgba(29,35,39,0.35)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(hoverX + 0.5, 0); ctx.lineTo(hoverX + 0.5, ya - 10); ctx.stroke();
      ctx.fillStyle = '#1d2327'; ctx.fillText(new Date(hoverT * 1000).toTimeString().slice(0, 8), hoverX, ya);
    }
  }

  const setBtn = $('vacLimitSet'), msg = $('vacLimitMsg'), vacInp = $('vacLimit');
  if (vacInp) {
    vacInp.addEventListener('input', () => { vacEdited = true; });
    vacInp.addEventListener('keydown', (e) => { if (e.key === 'Enter' && setBtn) setBtn.click(); });
  }
  if (setBtn) setBtn.onclick = async () => {
    const v = parseFloat((vacInp || {}).value);
    if (!(v > 0)) { if (msg) msg.textContent = 'enter a pressure in Torr, e.g. 2.5e-5'; return; }
    const r = await postJ('/api/vacuum', { limit_torr: v });
    if (msg) msg.textContent = r.ok ? `limit ${r.limit_torr.toExponential(1)} Torr (was ${r.was.toExponential(1)})`
                                    : (r.error || 'not changed');
    if (r.ok) {
      vacEdited = false;
      if (vacInp) vacInp.value = r.limit_torr.toExponential(1);
      STRIPS[4].limit = r.limit_torr; draw();
    }
  };
  markSeg();
  card.addEventListener('toggle', draw);
  window.addEventListener('resize', draw);
  const loop = async () => { await fetchNew(); draw(); setTimeout(loop, card.open ? 250 : 1000); };
  loop();
}
