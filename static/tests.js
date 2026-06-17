/*
 * Calibration & Test suite (frontend-driven, like the impedance sweep).
 *
 * 1. Filament Resistance   — all → STANDBY (0.8 V), read INA219, R = V/I.
 * 2. Emission short scan    — cold (SLEEP), emission −30 V @ 30 mA, pulse/filament.
 * 3. Focus leak scan        — focus −30 V, emission OFF, pulse/filament, watch emis V/I.
 * 4. Emission current test  — heat each filament, emission −200 V, 1 ms pulse,
 *                             check the per-pulse emission current is in range.
 * 5. Emission current calibration — per filament, sweep heating 1.0→2.5 A (0.25 A
 *                             step, 200 ms settle), record the emission-current
 *                             curve, save to host disk.
 *
 * HV (STM32) routes to the master server-side; per-filament heat/pulse use the
 * filament's own controller from /api/mapping. HV is always torn down in finally.
 */

const tPostJ = async (path, body) => {
  try {
    return await (await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
    })).json();
  } catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
};
const tGetJ = async (path) => { try { return await (await fetch(path)).json(); } catch (e) { return { ok: false, error: String(e) }; } };
const tSleep = (ms) => new Promise((r) => setTimeout(r, ms));
const $t = (id) => document.getElementById(id);

// ---- scaling ----------------------------------------------------------------
const ADS_MV_PER_COUNT = 6144 / 32768;
const HV_FULL_V = { emission: 350, focus: 1000 };
const voltToCount = (chan, magV) => Math.round((Math.abs(magV) / HV_FULL_V[chan] * 5000) / ADS_MV_PER_COUNT);
const EMI_FULL_MA = 85.7;
const emiLimitWiper = (mA) => Math.max(0, Math.min(127, Math.round(mA / EMI_FULL_MA * 127)));
const peakToMa = (peak) => 2 * (peak * 3.1 / 4095 - 0.5 * 1.155) / 6.8 / 8.2 * 1000;

// ---- hardware helpers -------------------------------------------------------
async function loadFilMap() {
  const j = await tGetJ('/api/mapping');
  const map = {};
  (j && j.mapping && j.mapping.filaments || []).forEach((r) => {
    if (r.controller === 0 || r.controller === 1) map[r.filament] = { ctrl: r.controller + 1, ch: r.channel, pos: r.position };
  });
  return map;
}
async function anyRunning() {
  const j = await tGetJ('/api/run-status');
  return Object.values((j && j.controllers) || {}).some((c) => c && c.status && c.status.state === 2);
}
// Resolve a filament's mapped power slot to a human label "P1·CH2.7" so scan
// results show which channel/board a flagged filament drives (the F# alone is
// the global 0-95 index — opaque without the active-list mapping). ch/pos are
// 0-based from /api/mapping; the UI is 1-based (CH1.1 = ch0/pos0).
const filBoard = (m) => (m ? `P${m.ctrl}·CH${m.ch + 1}.${m.pos + 1}` : '?');
const setState = (ctrl, ch, pos, state, arg) =>
  tPostJ('/api/cmd', { controller: ctrl, command: 'CH_SET_POWER_STATE', channel: ch, mux_port: pos, state, arg: arg || 0 });
const firePulse = (ctrl, ch, pos, widthUs) =>
  tPostJ('/api/cmd', { controller: ctrl, command: 'HV_PULSE', channel: ch, bit: pos, width_us: widthUs });
// Per-pulse tests only need the STM32 detector armed (EVT_PULSE over UART), NOT
// the ESP32 continuous SPI ring read — the ring's 20 MHz read competes with WiFi
// and drops the link mid-test. pulse-arm arms the STM32 ADC alone.
const pulseArm = () => tPostJ('/api/adc/pulse-arm', { rate: 1000000 });
const pulseDisarm = () => tPostJ('/api/adc/pulse-disarm', {});
const hvEnable = (chan, on) => tPostJ('/api/stm32/hv-enable', { ch: chan, on });
// Is the channel's HV output currently energised? (hv_status pin level, same
// source the Power-view On/Off tiles use.) Lets a test respect a pre-existing
// HV state instead of unconditionally toggling it.
const hvIsOn = async (chan) => {
  const st = await tGetJ('/api/stm32/hv-status');
  return !!(st && st.ok && (chan === 'focus' ? st.focus_on : st.emission_on));
};
const setEmiLimit = (mA) => tPostJ('/api/stm32/ds3502-set', { ch: 'ei', wiper: emiLimitWiper(mA) });
const readAds = () => tGetJ('/api/stm32/ads1115');
// Read a DS3502 wiper (0-127) — used to capture/restore a pre-existing HV setpoint
// exactly (the measured voltage is too coarse/unreliable to round-trip).
const readWiper = async (ds) => { const j = await tGetJ(`/api/stm32/ds3502?ch=${ds}`); return (j && j.ok && j.wiper != null) ? j.wiper : null; };

// ---- HV setpoint LUT --------------------------------------------------------
// The firmware closed loop (hv_set_target) walks the DS3502 toward an ADS target
// and drifts / never settles reliably. Instead we calibrate once: sweep the
// wiper, read the measured ADS voltage at each step, and store a wiper→V LUT.
// Setting a voltage then becomes a stable, repeatable direct wiper write — no
// loop, no drift. The LUT lives on the host (backend /api/hv-lut), keyed by the
// master controller + channel.
const HV_V_CH = {
  emission: { ds: 'ev', ads: 'emiss_v' },
  focus:    { ds: 'fv', ads: 'focus_v' },
};
const lutCache = { emission: null, focus: null };          // {points:[{wiper,v}], max_mag, ts}
const lutLoad = async (chan) => {
  const j = await tGetJ(`/api/hv-lut?chan=${chan}`);
  lutCache[chan] = (j && j.ok && Array.isArray(j.points) && j.points.length >= 2) ? j : null;
  return lutCache[chan];
};
const lutSave = (chan, points, maxMag) => tPostJ('/api/hv-lut/save', { chan, points, max_mag: maxMag });
// Direct DS3502 wiper write (loop bypassed). One quick retry on the busy-bus 502.
async function dsWrite(ch, wiper) {
  let j = await tPostJ('/api/stm32/ds3502-set', { ch, wiper: Math.max(0, Math.min(127, Math.round(wiper))) });
  if (!j.ok && j.status === 502) { await tSleep(500); j = await tPostJ('/api/stm32/ds3502-set', { ch, wiper: Math.max(0, Math.min(127, Math.round(wiper))) }); }
  return j;
}
// Interpolate a target magnitude (V, positive) → wiper using the cached LUT.
// Points are (wiper, signed-V); we work in magnitudes which rise with the wiper.
// Returns {wiper, expectV (signed), clamped}. null if no LUT.
function lutWiperForV(chan, magV) {
  const lut = lutCache[chan]; if (!lut) return null;
  const sign = (lut.points.find((p) => p.v !== 0) || { v: -1 }).v < 0 ? -1 : 1;
  const pts = lut.points.map((p) => ({ w: p.wiper, m: Math.abs(p.v) }))
    .sort((a, b) => a.m - b.m);                              // by magnitude, ascending
  const T = Math.abs(magV);
  if (T <= pts[0].m) return { wiper: pts[0].w, expectV: sign * pts[0].m, clamped: T < pts[0].m && pts[0].m > 0 };
  const last = pts[pts.length - 1];
  if (T >= last.m) return { wiper: last.w, expectV: sign * last.m, clamped: T > last.m };
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i], b = pts[i + 1];
    if (T >= a.m && T <= b.m) {
      const f = (b.m === a.m) ? 0 : (T - a.m) / (b.m - a.m);
      return { wiper: Math.round(a.w + f * (b.w - a.w)), expectV: sign * T, clamped: false };
    }
  }
  return { wiper: last.w, expectV: sign * last.m, clamped: true };
}
// Linear wiper estimate, used as a fallback when there is no usable LUT point
// (no calibration, or the request is outside the LUT's calibrated range):
// wiper ≈ |V| / full-scale × 127. Approximate — gets HV into the right ballpark.
const hardcodedWiper = (chan, magV) => Math.max(0, Math.min(127, Math.round(Math.abs(magV) / HV_FULL_V[chan] * 127)));
// Set a channel to a target magnitude. Prefers the calibrated LUT; falls back to
// the hardcoded linear formula when the LUT is missing or the request is out of
// its range, so a missing/partial calibration never blocks a test. Returns
// {ok, wiper, expectV, method} — method 'lut' | 'formula(no-LUT)' | 'formula(out-of-LUT)'.
async function lutSetV(chan, magV) {
  if (!lutCache[chan]) await lutLoad(chan);
  const hit = lutWiperForV(chan, magV);
  if (hit && !hit.clamped) {
    const j = await dsWrite(HV_V_CH[chan].ds, hit.wiper);
    return { ...j, ok: j.ok, wiper: hit.wiper, expectV: hit.expectV, method: 'lut' };
  }
  const w = hardcodedWiper(chan, magV);
  const j = await dsWrite(HV_V_CH[chan].ds, w);
  return { ...j, ok: j.ok, wiper: w, expectV: -Math.abs(magV), method: hit ? 'formula(out-of-LUT)' : 'formula(no-LUT)' };
}
// Safe teardown: zero the voltage wiper (replaces the old hv-clear-target).
const lutZeroV = (chan) => dsWrite(HV_V_CH[chan].ds, 0);
// Read the measured channel voltage (signed V), averaged over a few samples.
async function lutMeasV(chan, samples = 3) {
  let sum = 0, n = 0;
  for (let i = 0; i < samples; i++) {
    const a = await readAds();
    const v = a && a.ok ? a[HV_V_CH[chan].ads] : null;
    if (v != null) { sum += +v; n++; }
    await tSleep(80);
  }
  return n ? sum / n : null;
}
// Sweep the wiper in `steps` even increments across 0..127, read the measured
// voltage at each, and build + save the LUT. Requires HV already enabled on the
// channel (we refuse to silently energise). onStep(i, total, wiper, v) for UI.
async function lutCalibrate(chan, opts = {}) {
  const steps = Math.max(2, opts.steps || 10);
  const settleMs = opts.settleMs || 700;
  const onStep = opts.onStep || (() => {});
  const isAbort = opts.isAbort || (() => false);
  const st = await tGetJ(`/api/stm32/hv-status`);
  const on = st && st.ok && (chan === 'focus' ? st.focus_on : st.emission_on);
  if (!on) return { ok: false, error: `enable ${chan} HV first (sweep won't energise it)` };
  const wipers = [];
  for (let i = 0; i <= steps; i++) wipers.push(Math.round((i / steps) * 127));
  const points = [];
  try {
    for (let i = 0; i < wipers.length; i++) {
      if (isAbort()) return { ok: false, error: 'aborted', points };
      const w = wipers[i];
      const dw = await dsWrite(HV_V_CH[chan].ds, w);
      if (!dw.ok) return { ok: false, error: `wiper ${w} write failed: ${dw.error || dw.status}`, points };
      await tSleep(settleMs);
      const v = await lutMeasV(chan);
      points.push({ wiper: w, v: v == null ? 0 : +v.toFixed(2) });
      onStep(i + 1, wipers.length, w, v);
    }
  } finally {
    await lutZeroV(chan);                                    // leave it safe
  }
  const maxMag = points.length ? Math.max(...points.map((p) => Math.abs(p.v))) : 0;
  if (maxMag < 1) return { ok: false, error: 'calibration captured no voltage (all readings ~0) — check HV enable / ADS wiring', points };
  const save = await lutSave(chan, points, +maxMag.toFixed(1));
  if (save.ok) { lutCache[chan] = { points, max_mag: +maxMag.toFixed(1), ts: save.ts }; }
  return { ok: save.ok, error: save.error, points, max_mag: +maxMag.toFixed(1) };
}

// Set a channel via the LUT and let it settle (replaces the closed-loop wait).
// Set HV (LUT or formula fallback) and let it settle. Returns true if the wiper
// write succeeded (via either path) — callers MUST check this before enabling HV
// or firing, so a failed DS3502 write never leaves HV at an unknown setpoint.
async function setHvAndWait(chan, magV, timeoutMs = 8000) {
  const r = await lutSetV(chan, magV);
  if (!r.ok) { tMsg(`HV ${chan} not set — DS3502 wiper write failed (${r.error || r.status || '?'}).`, 'bad'); return false; }
  if (r.method !== 'lut') {
    tMsg(`⚠ ${chan} −${Math.abs(magV)} V set via formula (wiper ${r.wiper}) — ${r.method.includes('no-LUT') ? 'no LUT; Calibrate for accuracy' : 'outside LUT range'}.`, 'bad');
  }
  await tSleep(Math.min(timeoutMs, 600));                    // direct wiper settles fast
  return true;
}
async function pulseCursor() { const j = await tGetJ('/api/pulse-events?since=0'); return (j && j.last_id) || 0; }
async function fireAndMeasure(cur, ctrl, ch, pos, widthUs) {
  await firePulse(ctrl, ch, pos, widthUs);
  // Poll until a NEW event (id > cursor) lands rather than a single fixed wait —
  // a slow EVT_PULSE would otherwise read 0 mA and look like "no emission".
  const settle = Math.max(60, widthUs / 1000 + 120);
  const deadline = Date.now() + settle + 400;
  let evs = [];
  do {
    await tSleep(60);
    const j = await tGetJ(`/api/pulse-events?since=${cur.id}`);
    evs = (j && j.events) || [];
    if (evs.length) { cur.id = (j.last_id != null) ? j.last_id : evs[evs.length - 1].id; break; }
  } while (Date.now() < deadline);
  if (!evs.length) return null;
  const e = evs[evs.length - 1];
  // Per-pulse summary carries raw ADC counts: peak (highest), plateau (steady
  // "high"), bg (background/baseline). peakToMa is affine, so (peak − bg) → net =
  // real emission current with the dark/background level subtracted out. mA is the
  // background-subtracted net (what every verdict should use), clamped ≥ 0.
  const peakMa = peakToMa(e.peak);
  const bgMa = (e.bg != null) ? peakToMa(e.bg) : 0;
  const plateauMa = (e.plateau != null) ? peakToMa(e.plateau) : peakMa;
  return {
    peak: e.peak, plateau: e.plateau, bg: e.bg,
    mA: Math.max(0, peakMa - bgMa),
    peakMa, plateauMa, bgMa, netMa: peakMa - bgMa,
  };
}

// ---- run lifecycle ----------------------------------------------------------
let testBusy = false, abortFlag = false;
const tMsg = (m, cls) => { const e = $t('testStatus'); if (e) { e.textContent = m; e.className = 'summary' + (cls ? ' ' + cls : ''); } };
function setRunning(on) {
  testBusy = on;
  // Pause the GUI's background pollers so they don't pile load on the master
  // ESP32 while a (HV-energising, ring-streaming) test runs.
  window.ctTestRunning = on;
  document.querySelectorAll('.test-run').forEach((b) => { b.disabled = on; });
  const ab = $t('testAbort'); if (ab) ab.disabled = !on;
}
async function guard(needHv) {
  if (testBusy) return false;
  if (await anyRunning()) { tMsg('A schedule is running — disarm before testing.', 'bad'); return false; }
  if (needHv && !confirm('This test ENERGISES HV and fires pulses. Continue?')) return false;
  return true;
}
async function runTest(fn, needHv) {
  if (!(await guard(needHv))) return;
  abortFlag = false; setRunning(true);
  try { await fn(); }
  catch (e) { tMsg('Test error: ' + ((e && e.message) || e), 'bad'); }
  finally { setRunning(false); }
}

// ---- plots ------------------------------------------------------------------
const BAR_COLOR = { ok: '#3fb6a0', short: '#ff5d5d', open: '#f2c14e', leak: '#b07ce8', skip: '#5a6470', none: '#2a343c' };
function drawBars(canvasId, items, opt) {
  const c = $t(canvasId); if (!c) return;
  const W = c.width = c.clientWidth || 680, H = c.height = 132, ctx = c.getContext('2d');
  ctx.clearRect(0, 0, W, H); ctx.fillStyle = '#070b0e'; ctx.fillRect(0, 0, W, H);
  const padL = 38, padB = 16, padT = 8;
  const finite = items.filter((b) => isFinite(b.value)).map((b) => b.value);
  const yMax = opt.yMax || Math.max(0.001, ...finite) * 1.15 || 1;
  const x0 = padL, y0 = H - padB, plotW = W - padL - 6, plotH = H - padB - padT;
  ctx.strokeStyle = '#243038'; ctx.beginPath(); ctx.moveTo(x0, padT); ctx.lineTo(x0, y0); ctx.lineTo(W - 4, y0); ctx.stroke();
  ctx.fillStyle = '#8b97a0'; ctx.font = '9px monospace'; ctx.textAlign = 'right';
  for (let t = 0; t <= 2; t++) {
    const v = yMax * t / 2, y = y0 - (v / yMax) * plotH;
    ctx.fillText(opt.fmt ? opt.fmt(v) : v.toFixed(2), x0 - 3, y + 3);
    if (t) { ctx.strokeStyle = '#141c22'; ctx.beginPath(); ctx.moveTo(x0, y); ctx.lineTo(W - 4, y); ctx.stroke(); }
  }
  ctx.textAlign = 'left'; ctx.fillText(opt.yLabel || '', x0 + 2, padT + 8);
  const bw = plotW / 96;
  for (const b of items) {
    const open = !isFinite(b.value), v = open ? yMax : Math.min(b.value, yMax);
    const h = Math.max(open || b.cls === 'short' ? 3 : 1, (v / yMax) * plotH);
    ctx.fillStyle = BAR_COLOR[b.cls] || BAR_COLOR.none;
    ctx.fillRect(x0 + b.f * bw + 0.5, y0 - h, Math.max(1, bw - 0.6), h);
  }
  ctx.fillStyle = '#566069'; ctx.textAlign = 'center';
  for (let f = 0; f <= 95; f += 12) ctx.fillText(String(f), x0 + (f + 0.5) * bw, H - 5);
}
// line plot for a live x/y curve (calibration: Ie vs heat A; impedance: V vs I)
function drawCurve(canvasId, pts, opt) {
  const c = $t(canvasId); if (!c) return;
  const W = c.width = c.clientWidth || 680, H = c.height = 132, ctx = c.getContext('2d');
  ctx.clearRect(0, 0, W, H); ctx.fillStyle = '#070b0e'; ctx.fillRect(0, 0, W, H);
  const padL = 38, padB = 16, padT = 8, x0 = padL, y0 = H - padB, plotW = W - padL - 6, plotH = H - padB - padT;
  const xMax = opt.xMax || Math.max(0.001, ...pts.map((p) => p.x)) * 1.05;
  const yMax = Math.max(0.001, ...pts.map((p) => p.y)) * 1.15;
  ctx.strokeStyle = '#243038'; ctx.beginPath(); ctx.moveTo(x0, padT); ctx.lineTo(x0, y0); ctx.lineTo(W - 4, y0); ctx.stroke();
  ctx.fillStyle = '#8b97a0'; ctx.font = '9px monospace';
  ctx.textAlign = 'right'; ctx.fillText(yMax.toFixed(yMax < 10 ? 2 : 0), x0 - 3, padT + 6); ctx.fillText('0', x0 - 3, y0);
  ctx.textAlign = 'left'; ctx.fillText(opt.yLabel || '', x0 + 2, padT + 8);
  ctx.textAlign = 'center'; ctx.fillText(xMax.toFixed(2) + ' ' + (opt.xUnit || 'A'), W - 22, H - 5);
  const X = (x) => x0 + (x / xMax) * plotW, Y = (y) => y0 - (y / yMax) * plotH;
  ctx.strokeStyle = '#3fb6a0'; ctx.lineWidth = 1.4; ctx.beginPath();
  pts.forEach((p, i) => { i ? ctx.lineTo(X(p.x), Y(p.y)) : ctx.moveTo(X(p.x), Y(p.y)); });
  ctx.stroke();
  ctx.fillStyle = '#f2c14e';
  for (const p of pts) { ctx.beginPath(); ctx.arc(X(p.x), Y(p.y), 2.5, 0, 7); ctx.fill(); }
}

// least-squares fit V = (a·I² + R0)·I  → R0 = cold resistance (Ω). Reused from
// power.js measureImpedance: basis [I³, I], curve = [{v(volts), i(amps)}].
function fitR0(curve) {
  if (!curve || curve.length < 3) return null;
  let sI6 = 0, sI4 = 0, sI2 = 0, sI3V = 0, sIV = 0;
  for (const p of curve) { const i = p.i, i2 = i * i, i3 = i2 * i; sI6 += i3 * i3; sI4 += i2 * i2; sI2 += i2; sI3V += i3 * p.v; sIV += i * p.v; }
  const det = sI6 * sI2 - sI4 * sI4;
  let a = det ? (sI3V * sI2 - sI4 * sIV) / det : 0;
  let R0 = det ? (sI6 * sIV - sI4 * sI3V) / det : 0;
  if (R0 < 0) { R0 = 0; a = sI6 ? sI3V / sI6 : 0; }
  return R0;
}

// =========================================================================
// 1 — Filament resistance
// =========================================================================
// ---- unified test-result renderer -------------------------------------------
// Every test reports the same way: a verdict badge (PASS/FAIL/DONE) + count
// chips + an optional note + a flagged-item list. opts = { title, pass:
// true|false|null (null = neutral DONE), counts:[{n,label,bad?}], note?,
// flagged?:[str] }.
function testResult(elId, opts) {
  const el = $t(elId); if (!el) return;
  const badge = opts.pass == null ? '<span class="tr-badge done">DONE</span>'
    : opts.pass ? '<span class="tr-badge ok">PASS</span>' : '<span class="tr-badge fail">FAIL</span>';
  const counts = (opts.counts || []).map((c) =>
    `<span class="tr-count${c.bad ? ' bad' : ''}">${c.n} ${c.label}</span>`).join('');
  const flag = (opts.flagged && opts.flagged.length)
    ? `<div class="tr-flagged">${opts.flagged.slice(0, 30).join(' · ')}${opts.flagged.length > 30 ? ` · +${opts.flagged.length - 30} more` : ''}</div>`
    : '';
  el.className = 'summary tr' + (opts.pass === false ? ' bad' : '');
  el.innerHTML = `<div class="tr-head">${badge} <b>${opts.title}</b> ${counts}</div>`
    + (opts.note ? `<div class="tr-note">${opts.note}</div>` : '') + flag;
}

async function test1() {
  const settle = Math.max(0, parseInt($t('t1Settle').value, 10) || 3) * 1000;
  const shortR = parseFloat($t('t1Short').value) || 0.05, openMa = parseFloat($t('t1OpenMa').value) || 10;
  tMsg('Setting all filaments to STANDBY (0.8 V)…');
  const p = await tPostJ('/api/filament-prep', { state: 3 });
  // STANDBY can fail per-filament when a power channel is bad. Don't abort the
  // whole test — skip the failed filaments, flag them in the report, and measure
  // the rest. Only bail if NOTHING could be put to standby (no link / all bad).
  const prepFailed = new Set((p.failed || []).map(Number));
  const applied = p.applied != null ? p.applied
    : (p.results ? Object.values(p.results).reduce((a, r) => a + (r.applied || 0), 0) : 0);
  if (applied === 0) {
    const why = p.error || (p.results ? Object.values(p.results).map((r) => r.error).filter(Boolean).join('; ') : '') || 'check power';
    tMsg(`Standby failed on all filaments — ${why}.`, 'bad'); return;
  }
  if (prepFailed.size) tMsg(`⚠ Standby failed on ${prepFailed.size} filament(s) — skipping, will report. Settling…`, 'bad');
  for (let s = settle; s > 0 && !abortFlag; s -= 500) { tMsg(`Settling at standby… ${(s / 1000).toFixed(1)} s`); await tSleep(Math.min(500, s)); }
  if (abortFlag) { tMsg('Aborted.'); return; }
  tMsg('Reading INA219 V/I…');
  const fmap = await loadFilMap(); const items = []; const bad = []; const skipped = []; let okN = 0;
  for (const cid of [1, 2]) {
    const j = await tGetJ(`/api/board-snapshot?controller=${cid}`); if (!j.ok) continue;
    const byBoard = {}; (j.boards || []).forEach((b) => { byBoard[`${b.channel}.${b.mux_port}`] = b; });
    for (let f = 0; f < 96; f++) {
      const m = fmap[f]; if (!m || m.ctrl !== cid) continue;
      if (prepFailed.has(f)) { items.push({ f, value: 0, cls: 'skip' }); skipped.push(`F${f}`); continue; }
      const b = byBoard[`${m.ch}.${m.pos}`]; if (!b || !b.present) continue;
      const mA = b.current_mA || 0, V = (b.bus_mV || 0) / 1000, R = mA > 0 ? V / (mA / 1000) : Infinity;
      let cls;
      if (b.tps_fault || R < shortR) { cls = 'short'; bad.push(`F${f}: SHORT`); }
      else if (mA < openMa) { cls = 'open'; bad.push(`F${f}: OPEN`); }
      else { cls = 'ok'; okN++; }
      items.push({ f, value: cls === 'short' ? (isFinite(R) ? R : 0) : R, cls });
    }
  }
  drawBars('t1Plot', items, { yLabel: 'R (Ω)', yMax: 1.0, fmt: (v) => v.toFixed(2) });
  const shortN = bad.filter((s) => s.includes('SHORT')).length;
  const openN = bad.filter((s) => s.includes('OPEN')).length;
  testResult('t1Result', {
    title: 'Filament resistance',
    pass: bad.length === 0 && skipped.length === 0,
    counts: [{ n: okN, label: 'ok' }, { n: shortN, label: 'short', bad: shortN > 0 },
      { n: openN, label: 'open', bad: openN > 0 }, { n: skipped.length, label: 'standby-fail', bad: skipped.length > 0 }],
    flagged: bad.concat(skipped.map((s) => `${s} standby-fail`)),
  });
  tMsg(`Resistance done — ${okN} ok, ${bad.length} flagged, ${skipped.length} standby-fail (skipped).`, (bad.length || skipped.length) ? 'bad' : '');
}

// =========================================================================
// 2 — Emission short scan (no heating)
// =========================================================================
async function test2() {
  const magV = Math.abs(parseFloat($t('t2V').value) || 30), limMa = parseFloat($t('t2Lim').value) || 30;
  const widthUs = parseInt($t('t2Width').value, 10) || 100000, thr = parseFloat($t('t2Thr').value) || 5;
  const fmap = await loadFilMap(), fils = Object.keys(fmap).map(Number).sort((a, b) => a - b);
  const items = [], shorts = [], cur = { id: await pulseCursor() };
  // Respect a pre-existing emission state. If HV is already energised, capture
  // its setpoint, leave it ON, and restore that setpoint at the end; if it was
  // off, enable it for the sweep and return it to off after. Either way the
  // sweep itself drives −${magV} V while it runs.
  const emWasOn = await hvIsOn('emission');
  let priorWiper = null;
  if (emWasOn) priorWiper = await readWiper('ev');   // capture the exact wiper to restore
  try {
    tMsg('All filaments → SLEEP (no heating)…');
    const p = await tPostJ('/api/filament-prep', { state: 2 });
    if (!p.ok) { tMsg('Sleep failed: ' + (p.error || ''), 'bad'); return; }
    tMsg(`Emission → −${magV} V @ ${limMa} mA${emWasOn ? ' (was ON — staying on)' : ''}…`);
    if (!(await setHvAndWait('emission', magV))) return;   // wiper write failed → abort (finally tears down)
    await setEmiLimit(limMa);
    if (!emWasOn) await hvEnable('emission', true);
    await pulseArm(); await tSleep(150);
    for (let i = 0; i < fils.length; i++) {
      if (abortFlag) { tMsg('Aborted.'); break; }
      const f = fils[i], m = fmap[f];
      tMsg(`Emission short scan: ${i + 1}/${fils.length} (F${f} · ${filBoard(m)})…`);
      const r = await fireAndMeasure(cur, m.ctrl, m.ch, m.pos, widthUs), mA = r ? r.mA : 0, isShort = mA > thr;
      items.push({ f, value: Math.max(0, mA), cls: isShort ? 'short' : 'ok' });
      if (isShort) shorts.push(`F${f} (${filBoard(m)}): ${mA.toFixed(1)} mA`);
      drawBars('t2Plot', items, { yLabel: 'Ie (mA)', yMax: Math.max(limMa, thr * 2), fmt: (v) => v.toFixed(0) });
    }
    testResult('t2Result', {
      title: 'Emission short scan', pass: shorts.length === 0,
      counts: [{ n: fils.length, label: 'tested' }, { n: shorts.length, label: 'short', bad: shorts.length > 0 }],
      flagged: shorts,
    });
    tMsg(`Emission short scan done — ${shorts.length ? shorts.length + ' short' : 'all green'}.`, shorts.length ? 'bad' : '');
  } finally {
    if (emWasOn) {
      // Leave emission energised (the caller owns it); restore its exact prior
      // wiper so the scan's −30 V doesn't silently linger.
      if (priorWiper != null) await dsWrite('ev', priorWiper);
    } else {
      await hvEnable('emission', false); await lutZeroV('emission');
    }
    await pulseDisarm();
  }
}

// =========================================================================
// 3 — Focus leak scan (monitor emission V and I)
// =========================================================================
async function test3() {
  const magV = Math.abs(parseFloat($t('t3V').value) || 30), widthUs = parseInt($t('t3Width').value, 10) || 100000;
  const iThr = parseFloat($t('t3IThr').value) || 2, vThr = parseFloat($t('t3VThr').value) || 5;
  const fmap = await loadFilMap(), fils = Object.keys(fmap).map(Number).sort((a, b) => a - b);
  const items = [], leaks = [], cur = { id: await pulseCursor() }; let maxV = 0;
  try {
    tMsg('Emission OFF, all → SLEEP…');
    await hvEnable('emission', false); await lutZeroV('emission');
    const p = await tPostJ('/api/filament-prep', { state: 2 });
    if (!p.ok) { tMsg('Sleep failed: ' + (p.error || ''), 'bad'); return; }
    tMsg(`Focus → −${magV} V (emission stays OFF)…`);
    if (!(await setHvAndWait('focus', magV))) return;
    await hvEnable('focus', true);
    await pulseArm(); await tSleep(150);
    for (let i = 0; i < fils.length; i++) {
      if (abortFlag) { tMsg('Aborted.'); break; }
      const f = fils[i], m = fmap[f];
      tMsg(`Focus leak scan: ${i + 1}/${fils.length} (F${f})…`);
      const r = await fireAndMeasure(cur, m.ctrl, m.ch, m.pos, widthUs), ads = await readAds();
      const iMa = r ? r.mA : 0;
      const vEm = (ads && ads.ok && ads.emiss_v != null) ? Math.abs(ads.emiss_v) : 0;
      const iAds = (ads && ads.ok && ads.emiss_i_ma != null) ? Math.abs(ads.emiss_i_ma) : 0;
      maxV = Math.max(maxV, vEm);
      const leak = iMa > iThr || vEm > vThr || iAds > iThr;
      items.push({ f, value: Math.max(iMa, iAds), cls: leak ? 'leak' : 'ok' });
      if (leak) leaks.push(`F${f}: Ie ${Math.max(iMa, iAds).toFixed(1)} mA · Vem ${vEm.toFixed(1)} V`);
      drawBars('t3Plot', items, { yLabel: 'Ie (mA)', yMax: Math.max(iThr * 4, 10), fmt: (v) => v.toFixed(0) });
    }
    testResult('t3Result', {
      title: 'Focus leak scan', pass: leaks.length === 0,
      counts: [{ n: fils.length, label: 'tested' }, { n: leaks.length, label: 'leak', bad: leaks.length > 0 }],
      note: `peak Vem ${maxV.toFixed(1)} V`, flagged: leaks,
    });
    tMsg(`Focus leak scan done — ${leaks.length ? leaks.length + ' leak' : 'no leak'}.`, leaks.length ? 'bad' : '');
  } finally { await hvEnable('focus', false); await lutZeroV('focus'); await pulseDisarm(); }
}

// =========================================================================
// 4 — Emission current test (heat each filament, check the range)
// =========================================================================
async function test4() {
  // heating current entered in A (scan units); firmware CC target is mA.
  const heatA = parseFloat($t('t4Heat').value) || 2.6, heatMa = Math.round(heatA * 1000);
  const emV = Math.abs(parseFloat($t('t4V').value) || 200);
  const settle = Math.max(0, parseInt($t('t4Settle').value, 10) || 1500);
  const widthUs = parseInt($t('t4Width').value, 10) || 1000;
  const nMin = parseFloat($t('t4Min').value) || 2, nMax = parseFloat($t('t4Max').value) || 40;
  const fmap = await loadFilMap(), fils = Object.keys(fmap).map(Number).sort((a, b) => a - b);
  const items = [], bad = [], cur = { id: await pulseCursor() }; let cer = null;
  try {
    tMsg(`Emission → −${emV} V…`);
    if (!(await setHvAndWait('emission', emV))) return;
    await hvEnable('emission', true);
    await pulseArm(); await tSleep(150);
    for (let i = 0; i < fils.length; i++) {
      if (abortFlag) { tMsg('Aborted.'); break; }
      const f = fils[i], m = fmap[f]; cer = m;
      tMsg(`Emission current test: ${i + 1}/${fils.length} (F${f}) heating ${heatA} A…`);
      await setState(m.ctrl, m.ch, m.pos, 5, heatMa);     // ACTIVE
      await tSleep(settle);
      if (abortFlag) { await setState(m.ctrl, m.ch, m.pos, 2, 0); break; }
      const r = await fireAndMeasure(cur, m.ctrl, m.ch, m.pos, widthUs), mA = r ? r.mA : 0;
      await setState(m.ctrl, m.ch, m.pos, 2, 0);          // STOP before next
      const cls = (mA >= nMin && mA <= nMax) ? 'ok' : (mA < nMin ? 'open' : 'short');
      items.push({ f, value: Math.max(0, mA), cls });
      if (cls !== 'ok') bad.push(`F${f}: ${mA.toFixed(1)} mA`);
      drawBars('t4Plot', items, { yLabel: 'Ie (mA)', yMax: Math.max(nMax * 1.5, 60), fmt: (v) => v.toFixed(0) });
    }
    testResult('t4Result', {
      title: 'Emission current', pass: bad.length === 0,
      counts: [{ n: fils.length, label: 'tested' }, { n: fils.length - bad.length, label: 'in-range' }, { n: bad.length, label: 'out', bad: bad.length > 0 }],
      note: `range ${nMin}–${nMax} mA`, flagged: bad,
    });
    tMsg(`Emission current test done — ${bad.length ? bad.length + ' out of range' : 'all in range'}.`, bad.length ? 'bad' : '');
  } finally {
    if (cer) await setState(cer.ctrl, cer.ch, cer.pos, 2, 0);
    await hvEnable('emission', false); await lutZeroV('emission'); await pulseDisarm();
  }
}

// =========================================================================
// 5 — Emission current calibration (heat sweep per filament → curve → disk)
// =========================================================================
async function test5() {
  const fromA = parseFloat($t('t5From').value) || 1.0, toA = parseFloat($t('t5To').value) || 2.5;
  const stepA = Math.max(0.001, parseFloat($t('t5Step').value) || 0.25), settle = Math.max(0, parseInt($t('t5Settle').value, 10) || 200);
  const emV = Math.abs(parseFloat($t('t5V').value) || 200), widthUs = parseInt($t('t5Width').value, 10) || 1000;
  const nLevels = Math.max(1, Math.round((toA - fromA) / stepA) + 1);   // integer index avoids float drift dropping the top level
  const levels = []; for (let k = 0; k < nLevels; k++) levels.push(+(fromA + k * stepA).toFixed(3));
  const fmap = await loadFilMap(), fils = Object.keys(fmap).map(Number).sort((a, b) => a - b);
  const curves = {}, cur = { id: await pulseCursor() }; let cer = null;
  const params = { fromA, toA, stepA, settleMs: settle, emissionV: -emV, pulseUs: widthUs, levels };
  try {
    tMsg(`Calibration — emission → −${emV} V (${fils.length} filaments × ${levels.length} levels)…`);
    if (!(await setHvAndWait('emission', emV))) return;
    await hvEnable('emission', true);
    await pulseArm(); await tSleep(150);
    for (let i = 0; i < fils.length; i++) {
      if (abortFlag) { tMsg('Aborted — saving partial…'); break; }
      const f = fils[i], m = fmap[f]; cer = m; const pts = []; curves[f] = [];
      for (const a of levels) {
        if (abortFlag) break;
        tMsg(`Calibration: F${f} (${i + 1}/${fils.length}) @ ${a.toFixed(2)} A…`);
        await setState(m.ctrl, m.ch, m.pos, 5, Math.round(a * 1000));   // ACTIVE @ a amps
        await tSleep(settle);
        const r = await fireAndMeasure(cur, m.ctrl, m.ch, m.pos, widthUs), mA = r ? r.mA : 0;
        curves[f].push({ heatA: a, mA: +mA.toFixed(2), peak: r ? r.peak : null });
        pts.push({ x: a, y: Math.max(0, mA) });
        drawCurve('t5Plot', pts, { xMax: toA, yLabel: `Ie (mA) · F${f}` });
      }
      await setState(m.ctrl, m.ch, m.pos, 2, 0);   // STOP before the next filament
    }
  } finally {
    if (cer) await setState(cer.ctrl, cer.ch, cer.pos, 2, 0);
    await hvEnable('emission', false); await lutZeroV('emission'); await pulseDisarm();
  }
  // persist whatever we collected (full or partial)
  if (Object.keys(curves).length) {
    const save = await tPostJ('/api/calibration/save', { name: 'emission_calibration', data: { params, curves } });
    testResult('t5Result', {
      title: 'Emission calibration', pass: save.ok ? null : false,
      counts: [{ n: save.filaments != null ? save.filaments : Object.keys(curves).length, label: 'curves' }],
      note: save.ok ? `saved · ${save.csv || 'disk'}` : `save failed: ${save.error || ''}`,
    });
    tMsg(`Calibration done — ${Object.keys(curves).length} filaments ${save.ok ? 'saved to disk' : 'NOT saved'}.`, save.ok ? '' : 'bad');
  } else { tMsg('Calibration produced no data.', 'bad'); }
}

// =========================================================================
// 6 — Impedance sweep per filament (reuses power.js measureImpedance + fit)
// =========================================================================
async function test6() {
  const startMv = 800;
  const endMv = Math.round(Math.min(15, Math.max(0.9, parseFloat($t('t6End').value) || 1.5)) * 1000);
  const stepMv = Math.round(Math.min(2, Math.max(0.02, parseFloat($t('t6Step').value) || 0.1)) * 1000);
  const dwell = Math.max(100, Math.round((parseFloat($t('t6Dwell').value) || 1.5) * 1000));
  const shortR = parseFloat($t('t6Short').value) || 0.02;
  const fmap = await loadFilMap(), fils = Object.keys(fmap).map(Number).sort((a, b) => a - b);
  const curves = {}, r0s = {}, bars = []; let done = 0;
  const params = { startMv, endMv, stepMv, dwellMs: dwell };
  try {
    for (let i = 0; i < fils.length; i++) {
      if (abortFlag) { tMsg('Aborted — saving partial…'); break; }
      const f = fils[i], m = fmap[f]; const pts = []; curves[f] = [];
      for (let mv = startMv; mv <= endMv && !abortFlag; mv += stepMv) {
        tMsg(`Impedance: F${f} (${i + 1}/${fils.length}) @ ${mv} mV…`);
        await setState(m.ctrl, m.ch, m.pos, 6, mv);               // VOLTAGE mode
        await tSleep(dwell);
        const r = await tPostJ('/api/cmd', { controller: m.ctrl, command: 'CH_GET_INA219', channel: m.ch, mux_port: m.pos });
        const d = r && r.response && r.response.decoded;
        if (d && d.present && d.current_mA > 0) {
          const v = d.bus_mV / 1000, iA = d.current_mA / 1000;
          pts.push({ x: iA, y: v }); curves[f].push({ v: +v.toFixed(4), i: +iA.toFixed(4) });
          drawCurve('t6Curve', pts, { yLabel: `V · F${f}`, xUnit: 'A' });
        }
      }
      await setState(m.ctrl, m.ch, m.pos, 2, 0);                   // STOP before next
      const R0 = fitR0(curves[f]); r0s[f] = R0; done++;
      bars.push({ f, value: R0 == null ? Infinity : R0, cls: R0 == null ? 'open' : (R0 < shortR ? 'short' : 'ok') });
      drawBars('t6Plot', bars, { yLabel: 'R₀ (Ω)', yMax: 0.6, fmt: (v) => v.toFixed(2) });
    }
  } finally {
    for (const f of Object.keys(curves).map(Number)) { const m = fmap[f]; if (m) await setState(m.ctrl, m.ch, m.pos, 2, 0); }
  }
  if (done) {
    const save = await tPostJ('/api/calibration/save', { name: 'impedance_sweep', data: { params, curves, r0: r0s } });
    const r0vals = Object.values(r0s).filter((x) => x != null);
    const lo = r0vals.length ? Math.min(...r0vals) : 0, hi = r0vals.length ? Math.max(...r0vals) : 0;
    testResult('t6Result', {
      title: 'Impedance sweep', pass: save.ok ? null : false,
      counts: [{ n: done, label: 'swept' }],
      note: `R₀ ${lo.toFixed(3)}–${hi.toFixed(3)} Ω${save.ok ? (save.csv ? ' · ' + save.csv : ' · saved') : ' · save failed'}`,
    });
    tMsg(`Impedance sweep done — ${done} filaments ${save.ok ? 'saved to disk' : 'NOT saved'}.`, save.ok ? '' : 'bad');
  } else { tMsg('Impedance sweep produced no data.', 'bad'); }
}

// ---- markup (descriptions live in the title tooltip, not on-screen) ---------
const TESTS_HTML = `
  <div class="seg sm test-seg" id="testSeg">
    <button class="seg-btn active" data-test="1" title="Filament Resistance">Resist</button>
    <button class="seg-btn" data-test="2" title="Emission short scan">E-short</button>
    <button class="seg-btn" data-test="3" title="Focus leak scan">Focus</button>
    <button class="seg-btn" data-test="4" title="Emission current test">Emis I</button>
    <button class="seg-btn" data-test="5" title="Emission current calibration">Calib</button>
    <button class="seg-btn" data-test="6" title="Impedance sweep (per filament)">Imped</button>
  </div>
  <div class="test-block show" id="testBlock1">
    <div class="block-title" title="Drives every filament to STANDBY (0.8 V), settles, then reads each board's INA219 V/I and computes R = V/I. Near-zero R or an OCP trip = short; no current = open.">1 · Filament Resistance <span class="hint">ⓘ</span></div>
    <div class="test-params">
      <label class="numlabel">settle s<input id="t1Settle" type="number" min="0" max="60" value="3" /></label>
      <label class="numlabel">short Ω<input id="t1Short" type="number" min="0" step="0.01" value="0.05" /></label>
      <label class="numlabel">open mA<input id="t1OpenMa" type="number" min="0" value="10" /></label>
      <button class="xs quick test-run" id="t1Run">Run</button>
    </div>
    <canvas id="t1Plot" class="test-plot"></canvas>
    <div class="test-legend"><span><i class="sw ok"></i>normal</span><span><i class="sw short"></i>short</span><span><i class="sw open"></i>open</span></div>
    <div id="t1Result" class="summary"></div>
  </div>

  <div class="test-block" id="testBlock2">
    <div class="block-title" title="With every filament cold (SLEEP), sets a low emission voltage at a tight current limit and pulses each filament. A cold filament draws almost nothing — real emission current = short.">2 · Emission short scan <span class="hint">— no heating · ⓘ</span></div>
    <div class="test-params">
      <label class="numlabel">emis −V<input id="t2V" type="number" min="0" max="350" value="30" /></label>
      <label class="numlabel">limit mA<input id="t2Lim" type="number" min="0" max="85" value="30" /></label>
      <label class="numlabel">pulse µs<input id="t2Width" type="number" min="1" max="1000000" value="100000" /></label>
      <label class="numlabel">short mA<input id="t2Thr" type="number" min="0" value="5" /></label>
      <button class="xs quick test-run" id="t2Run">Run</button>
    </div>
    <canvas id="t2Plot" class="test-plot"></canvas>
    <div class="test-legend"><span><i class="sw ok"></i>ok</span><span><i class="sw short"></i>short</span></div>
    <div id="t2Result" class="summary"></div>
  </div>

  <div class="test-block" id="testBlock3">
    <div class="block-title" title="Energises the focus rail with emission OFF, all filaments SLEEP, and pulses each filament while monitoring emission V and I (ADS1115 + per-pulse). Emission V/I that deviates = focus leaking across.">3 · Focus leak scan <span class="hint">ⓘ</span></div>
    <div class="test-params">
      <label class="numlabel">focus −V<input id="t3V" type="number" min="0" max="1000" value="30" /></label>
      <label class="numlabel">pulse µs<input id="t3Width" type="number" min="1" max="1000000" value="100000" /></label>
      <label class="numlabel">leak mA<input id="t3IThr" type="number" min="0" value="2" /></label>
      <label class="numlabel">leak V<input id="t3VThr" type="number" min="0" value="5" /></label>
      <button class="xs quick test-run" id="t3Run">Run</button>
    </div>
    <canvas id="t3Plot" class="test-plot"></canvas>
    <div class="test-legend"><span><i class="sw ok"></i>ok</span><span><i class="sw leak"></i>leak</span></div>
    <div id="t3Result" class="summary"></div>
  </div>

  <div class="test-block" id="testBlock4">
    <div class="block-title" title="Heats each filament (one at a time) to the heat current, sets emission −200 V, fires a 1 ms pulse, and checks the per-pulse emission current is inside the normal window. Out-of-range = low (amber) / high (red).">4 · Emission current test <span class="hint">ⓘ</span></div>
    <div class="test-params">
      <label class="numlabel">heat A<input id="t4Heat" type="number" min="0" max="4" step="0.05" value="2.6" /></label>
      <label class="numlabel">emis −V<input id="t4V" type="number" min="0" max="350" value="200" /></label>
      <label class="numlabel">settle ms<input id="t4Settle" type="number" min="0" max="10000" value="1500" /></label>
      <label class="numlabel">pulse µs<input id="t4Width" type="number" min="1" max="100000" value="1000" /></label>
      <label class="numlabel">norm mA<input id="t4Min" type="number" min="0" value="2" /></label>
      <label class="numlabel">– max<input id="t4Max" type="number" min="0" value="40" /></label>
      <button class="xs quick test-run" id="t4Run">Run</button>
    </div>
    <canvas id="t4Plot" class="test-plot"></canvas>
    <div class="test-legend"><span><i class="sw ok"></i>in range</span><span><i class="sw open"></i>low</span><span><i class="sw short"></i>high</span></div>
    <div id="t4Result" class="summary"></div>
  </div>

  <div class="test-block" id="testBlock5">
    <div class="block-title" title="LONG. Per filament, sweeps the heating current from→to in steps (200 ms settle each), fires a pulse at each level, and records the emission-current curve. Slow. Saves all curves to the host disk (JSON + CSV).">5 · Emission current calibration <span class="hint">ⓘ</span></div>
    <div class="test-params">
      <label class="numlabel">from A<input id="t5From" type="number" min="0" max="4" step="0.05" value="1.0" /></label>
      <label class="numlabel">to A<input id="t5To" type="number" min="0" max="4" step="0.05" value="2.5" /></label>
      <label class="numlabel">step A<input id="t5Step" type="number" min="0.05" max="2" step="0.05" value="0.25" /></label>
      <label class="numlabel">settle ms<input id="t5Settle" type="number" min="0" max="5000" value="200" /></label>
      <label class="numlabel">emis −V<input id="t5V" type="number" min="0" max="350" value="200" /></label>
      <label class="numlabel">pulse µs<input id="t5Width" type="number" min="1" max="100000" value="1000" /></label>
      <button class="xs quick test-run" id="t5Run">Run</button>
    </div>
    <canvas id="t5Plot" class="test-plot"></canvas>
    <div class="hint">Live curve = the filament being calibrated. Full set is written to <code>tools/ct_gui/calibration/</code>.</div>
    <div id="t5Result" class="summary"></div>
  </div>

  <div class="test-block" id="testBlock6">
    <div class="block-title" title="Per filament, voltage-mode sweep from 0.8 V to End V (dwell at each step for thermal equilibrium), reads INA219, and least-squares fits V = a·I² + R₀ to get the cold resistance R₀. Reuses the single-board impedance sweep. Slow. Saves V-I curves + R₀ to disk.">6 · Impedance sweep <span class="hint">ⓘ</span></div>
    <div class="test-params">
      <label class="numlabel">end V<input id="t6End" type="number" min="0.9" max="15" step="0.1" value="1.5" /></label>
      <label class="numlabel">step V<input id="t6Step" type="number" min="0.02" max="2" step="0.05" value="0.1" /></label>
      <label class="numlabel">dwell s<input id="t6Dwell" type="number" min="0.1" max="20" step="0.1" value="1.5" /></label>
      <label class="numlabel">short Ω<input id="t6Short" type="number" min="0" step="0.01" value="0.02" /></label>
      <button class="xs quick test-run" id="t6Run">Run</button>
    </div>
    <canvas id="t6Curve" class="test-plot" title="Live V-I curve of the filament being swept."></canvas>
    <canvas id="t6Plot" class="test-plot" title="Fitted cold resistance R₀ per filament."></canvas>
    <div class="test-legend"><span><i class="sw ok"></i>R₀ ok</span><span><i class="sw short"></i>short</span><span><i class="sw open"></i>no fit</span></div>
    <div id="t6Result" class="summary"></div>
  </div>

  <div class="row compact test-foot">
    <button class="xs" id="testAbort" disabled title="Stop after the current step and tear down HV (calibration saves the partial set).">Abort</button>
    <span id="testStatus" class="summary"></span>
  </div>`;

function showTest(n) {
  for (let i = 1; i <= 6; i++) { const b = $t('testBlock' + i); if (b) b.classList.toggle('show', i === n); }
  document.querySelectorAll('#testSeg .seg-btn').forEach((x) => x.classList.toggle('active', +x.dataset.test === n));
}

// Reusable primitives for the single-board Cal & Test panel (power.js). Stateless
// (no shared abort) — the single-board caller runs its own short loops.
export const calApi = {
  tSleep, voltToCount, peakToMa, emiLimitWiper, fitR0, drawCurve, drawBars,
  setState, firePulse, pulseArm, pulseDisarm, hvEnable, setEmiLimit, readAds,
  pulseCursor, fireAndMeasure, loadFilMap,
  inaSingle: (ctrl, ch, pos) => tPostJ('/api/cmd', { controller: ctrl, command: 'CH_GET_INA219', channel: ch, mux_port: pos }),
  // HV setpoint via host LUT (replaces the unstable firmware closed loop).
  lutLoad, lutSave, lutSetV, lutZeroV, lutCalibrate, lutWiperForV,
  lutGet: (chan) => lutCache[chan],
  setHvAndWait,
  saveCalibration: (name, data) => tPostJ('/api/calibration/save', { name, data }),
  anyRunning,
};

export function initTests() {
  const host = $t('testsCard'); if (!host) return;
  host.innerHTML = TESTS_HTML;
  document.querySelectorAll('#testSeg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => showTest(+b.dataset.test)));
  showTest(1);
  $t('t1Run').onclick = () => runTest(test1, false);
  $t('t2Run').onclick = () => runTest(test2, true);
  $t('t3Run').onclick = () => runTest(test3, true);
  $t('t4Run').onclick = () => runTest(test4, true);
  $t('t5Run').onclick = () => runTest(test5, true);
  $t('t6Run').onclick = () => runTest(test6, false);   // voltage-mode, no HV
  $t('testAbort').onclick = () => { abortFlag = true; tMsg('Aborting after the current step…'); };
}
