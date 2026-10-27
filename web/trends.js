/*
 * Supply & vacuum trend: Spellman V / I, emission V / I (ADS1115) and the
 * vacuum, one strip each on a shared time axis (different units -> separate
 * y-axes, never one dual-axis plot). Data from /api/series: every reading as
 * the backend received it (Spellman broadcasts, pressure points, ADS reads),
 * fetched incrementally once a second. Drawn only while the card is open.
 */
import { $ } from './dom.js';
import { postJ } from './net.js';

const KEEP_S = 3600;
const STRIPS = [
  { key: 'sp_kv', label: 'Spellman V', unit: 'kV', color: '#2f6db5', fmt: (v) => v.toFixed(2) },
  { key: 'sp_ma', label: 'Spellman I', unit: 'mA', color: '#7a4fb3', fmt: (v) => v.toFixed(3) },
  { key: 'em_v', label: 'Emission V', unit: 'V', color: '#1f8a70', fmt: (v) => v.toFixed(1) },
  { key: 'em_i', label: 'Emission I', unit: 'mA', color: '#c46a1b', fmt: (v) => v.toFixed(2) },
  { key: 'vac', label: 'Vacuum', unit: 'Torr', color: '#56636b', fmt: (v) => v.toExponential(2), log: true, limit: null },   // limit: the backend's
];
const STRIP_H = 58, GAP = 6, AXIS_H = 18, LEFT = 84, RIGHT = 8;

export function initTrends() {
  const card = $('trendCard'), canvas = $('trendCanvas');
  if (!card || !canvas) return;
  const data = Object.fromEntries(STRIPS.map((s) => [s.key, []]));
  let last = 0, windowS = 120, hoverX = null;

  const seg = $('trendWindow');
  if (seg) seg.querySelectorAll('button').forEach((b) => {
    b.onclick = () => {
      seg.querySelectorAll('button').forEach((x) => x.classList.toggle('active', x === b));
      windowS = +b.dataset.s;
      draw();
    };
  });
  canvas.addEventListener('mousemove', (e) => { const r = canvas.getBoundingClientRect(); hoverX = e.clientX - r.left; draw(); });
  canvas.addEventListener('mouseleave', () => { hoverX = null; draw(); });

  async function fetchNew() {
    try {
      const r = await (await fetch(`/api/series?since=${last}&max_points=4000`)).json();
      if (!r.ok) return;
      if (typeof r.vacuum_limit_torr === 'number') {
        STRIPS[4].limit = r.vacuum_limit_torr;
        const inp = $('vacLimit');
        if (inp && document.activeElement !== inp) inp.value = r.vacuum_limit_torr.toExponential(1);
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
    const t1 = Date.now() / 1000, t0 = t1 - windowS, pw = W - LEFT - RIGHT;
    const xOf = (t) => LEFT + ((t - t0) / windowS) * pw;
    ctx.font = '10px ui-monospace, Menlo, monospace';
    const hoverT = hoverX != null && hoverX >= LEFT ? t0 + ((hoverX - LEFT) / pw) * windowS : null;

    STRIPS.forEach((s, k) => {
      const y0 = k * (STRIP_H + GAP), pts = data[s.key].filter((p) => p[0] >= t0 - 1);
      // frame + label + latest value (text in ink, the line carries the colour)
      ctx.strokeStyle = '#e3e0d6'; ctx.lineWidth = 1;
      ctx.strokeRect(LEFT + 0.5, y0 + 0.5, pw - 1, STRIP_H - 1);
      ctx.fillStyle = '#3c4348'; ctx.textAlign = 'left';
      ctx.fillText(s.label, 2, y0 + 11);
      const latest = data[s.key].length ? data[s.key][data[s.key].length - 1] : null;
      ctx.fillStyle = latest ? '#1d2327' : '#9aa3a8';
      ctx.fillText(latest && t1 - latest[0] < 15 ? `${s.fmt(latest[1])} ${s.unit}` : 'no data', 2, y0 + 24);
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
        if (prev && p[0] - prev > Math.max(10, windowS / 30)) ctx.moveTo(x, y);   // a gap is not a line
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
    const step = windowS <= 120 ? 30 : windowS <= 600 ? 120 : 900;
    for (let s = Math.ceil(t0 / step) * step; s <= t1; s += step) {
      const x = xOf(s);
      if (x < LEFT + 22 || x > LEFT + pw - 22) continue;     // would be clipped
      ctx.fillText(new Date(s * 1000).toTimeString().slice(0, windowS <= 600 ? 8 : 5), x, ya);
    }
    if (hoverT != null) {
      ctx.strokeStyle = 'rgba(29,35,39,0.35)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(hoverX + 0.5, 0); ctx.lineTo(hoverX + 0.5, ya - 10); ctx.stroke();
      ctx.fillStyle = '#1d2327'; ctx.fillText(new Date(hoverT * 1000).toTimeString().slice(0, 8), hoverX, ya);
    }
  }

  const setBtn = $('vacLimitSet'), msg = $('vacLimitMsg');
  if (setBtn) setBtn.onclick = async () => {
    const v = parseFloat(($('vacLimit') || {}).value);
    if (!(v > 0)) { if (msg) msg.textContent = 'enter a pressure in Torr, e.g. 2.5e-5'; return; }
    const r = await postJ('/api/vacuum', { limit_torr: v });
    if (msg) msg.textContent = r.ok ? `limit ${r.limit_torr.toExponential(1)} Torr (was ${r.was.toExponential(1)})`
                                    : (r.error || 'not changed');
    if (r.ok) { STRIPS[4].limit = r.limit_torr; draw(); }
  };
  card.addEventListener('toggle', draw);
  window.addEventListener('resize', draw);
  const loop = async () => { await fetchNew(); draw(); setTimeout(loop, 1000); };
  loop();
}
