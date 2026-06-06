/*
 * CT GUI — scan state machine + per-filament power/telemetry model + wiring.
 *
 * Two scan modes (the motion controls only drive the animation):
 *   Stationary: gantry fixed. Fire the 35 covered filaments, then step the
 *     collimator+detector to the next filament center. 96 ring steps.
 *   Precision: at each collimator position sweep the gantry across ±max in N
 *     steps, firing all 35 at every angle; at an extreme advance the collimator
 *     and reverse the sweep (boustrophedon).
 *
 * Power model mirrors the RP2350B firmware PowerState enum
 *   (Stop=1, Sleep=2, Standby=3, Idle=4, Active=5). V/I come from INA219
 *   (bus_mV / current_mA). mAs is NOT in firmware — we integrate it host-side.
 * Until the two ESP32 bridges are wired, telemetry is simulated so the ring is
 * live; real /api telemetry will drop into ingestTelemetry() unchanged.
 */

import { CT, mod as ctMod, filamentBaseAngle as ctFilamentBaseAngle } from './ct/constants.js';
import { CTGeometry } from './ct/renderer.js';

const $ = (id) => document.getElementById(id);
const HALF = (CT.COVERAGE - 1) / 2; // 17
const N = CT.N_FILAMENTS;

// ---- firmware PowerState mirror --------------------------------------------
const STATE = { STOP: 1, SLEEP: 2, STANDBY: 3, IDLE: 4, ACTIVE: 5 };
const STATE_NAME = { 1: 'Stop', 2: 'Sleep', 3: 'Standby', 4: 'Idle', 5: 'Active' };
const STATE_COLOR = {
  1: '#3b444c', // Stop    — dark, fully off
  2: '#4d6fae', // Sleep   — cool blue, INA only
  3: '#c79a3a', // Standby — amber, TPS biased
  4: '#3fb6a0', // Idle    — teal, detection-ready (0.8 V)
  5: '#ff5d5d', // Active  — red, firing at operating mV
};
const V_MAX = 5000;   // mV full-scale for the outward voltage bar
const I_MAX = 1500;   // mA full-scale for the outward current bar
const SHOT_S = 0.0005; // demo dwell per fired filament (s) for mAs integration
const PREHEAT = 5;     // filaments ahead of the current one held Active to pre-heat

// ---- hardware topology ------------------------------------------------------
// 2 power controllers (ESP32 bridges). 6 of 8 channels used per controller,
// 8 boards/channel = 48 filaments each. Filaments 0-47 -> power1, 48-95 -> power2.
const CH_USED = 6;
function filamentToHw(i) {
  const controller = i < 48 ? 1 : 2;
  const local = i % 48;
  const channel = Math.floor(local / 8); // 0..5
  const mux = local % 8;                  // 0..7
  return { controller, channel, mux, label: `P${controller} · CH${channel + 1}.${mux + 1}` };
}

// ---- per-filament telemetry -------------------------------------------------
const filaments = Array.from({ length: N }, () => ({
  state: STATE.STOP, voltage_mV: 0, current_mA: 0, mAs: 0,
}));

const state = {
  mode: 'stationary',
  collimatorCenter: 0,
  windowPos: HALF,
  collimatorDir: +1,    // +1 CCW collimator ring step, -1 CW
  filamentDir: +1,      // +1 CCW gantry rotation, -1 CW
  gantryMax: 10,
  gantrySteps: 5,
  gantryIndex: 0,
  sweepDir: +1,
  gantryAngle: 0,
  ringStep: 0,
  get activeFilament() {
    return ctMod(this.collimatorCenter - HALF + this.windowPos, N);
  },
};

let geo;
let playTimer = null;
let hoverFil = -1; // filament under the cursor, or -1

// ---- gantry angle list ------------------------------------------------------
function gantryAngles() {
  const n = 2 * state.gantrySteps + 1, out = [];
  for (let k = 0; k < n; k++) out.push(-state.gantryMax + 2 * state.gantryMax * (k / (n - 1)));
  return out;
}
function applyGantryFromIndex() {
  if (state.mode !== 'precision') return;
  const a = gantryAngles();
  state.gantryIndex = Math.min(state.gantryIndex, a.length - 1);
  state.gantryAngle = a[state.gantryIndex];
}

function windowIndices() {
  const out = [];
  for (let k = -HALF; k <= HALF; k++) out.push(ctMod(state.collimatorCenter + k, N));
  return out;
}

// ---- power model ------------------------------------------------------------
// During a scan the collimator-covered 35 sit at Idle by default. The current
// filament fires (Active) and the next PREHEAT filaments in scan order are also
// held Active to pre-heat their cathodes; already-scanned filaments fall back to
// Idle. Everything outside the window sleeps. Real telemetry replaces this in
// ingestTelemetry().
function refreshStates() {
  const covered = new Set(windowIndices());
  const active = state.activeFilament;
  // active + pre-heat lookahead band (current scan position .. +PREHEAT),
  // wrapping periodically WITHIN the 35-filament window: at the last few
  // covered filaments the lookahead pre-heats the window's initial ones.
  const heatBand = new Set();
  for (let k = 0; k <= PREHEAT; k++) {
    const wp = (state.windowPos + k) % CT.COVERAGE;
    heatBand.add(ctMod(state.collimatorCenter - HALF + wp, N));
  }
  for (let i = 0; i < N; i++) {
    const f = filaments[i];
    if (heatBand.has(i)) {
      f.state = STATE.ACTIVE;
      const firing = i === active;
      f.voltage_mV = 4500 + (Math.random() - 0.5) * 120;
      f.current_mA = (firing ? 1150 : 360) + (Math.random() - 0.5) * (firing ? 180 : 60);
    } else if (covered.has(i)) {
      f.state = STATE.IDLE;       // detection-ready 0.8 V
      f.voltage_mV = 800 + (Math.random() - 0.5) * 30;
      f.current_mA = 6 + Math.random() * 4;
    } else {
      f.state = STATE.SLEEP;
      f.voltage_mV = 0;
      f.current_mA = 0;
    }
  }
}

// integrate mAs for the filament that just fired
function fireActive() {
  const f = filaments[state.activeFilament];
  f.mAs += (1150 / 1000) * SHOT_S * 1000; // mA * s -> mAs (scaled for demo visibility)
}

function maxMAs() {
  let m = 0;
  for (const f of filaments) if (f.mAs > m) m = f.mAs;
  return Math.max(1e-6, m);
}

// hook for real telemetry later: ingestTelemetry([{index, state, bus_mV, current_mA}])
function ingestTelemetry(rows) {
  for (const r of rows) {
    const f = filaments[r.index];
    if (!f) continue;
    if (r.state != null) f.state = r.state;
    if (r.bus_mV != null) f.voltage_mV = r.bus_mV;
    if (r.current_mA != null) f.current_mA = r.current_mA;
  }
  sync();
}
window.ingestTelemetry = ingestTelemetry;

// ---- scan stepping ----------------------------------------------------------
function advance() {
  state.windowPos++;
  if (state.windowPos >= CT.COVERAGE) {
    state.windowPos = 0;
    if (state.mode === 'stationary') {
      stepRing();
    } else {
      const angles = gantryAngles();
      const next = state.gantryIndex + state.sweepDir;
      if (next < 0 || next >= angles.length) { state.sweepDir *= -1; stepRing(); }
      else state.gantryIndex = next;
      applyGantryFromIndex();
    }
  }
  fireActive();
  sync();
}
function stepRing() {
  state.collimatorCenter = ctMod(state.collimatorCenter + state.collimatorDir, N);
  state.ringStep = ctMod(state.ringStep + 1, N);
}
function reset() {
  stopPlay();
  state.windowPos = HALF;
  state.gantryIndex = state.filamentDir > 0 ? 0 : 2 * state.gantrySteps;
  state.sweepDir = state.filamentDir;
  state.ringStep = 0;
  if (state.mode === 'precision') applyGantryFromIndex();
  else state.gantryAngle = parseFloat($('gantrySlider').value) || 0;
  for (const f of filaments) { f.mAs = 0; f.state = STATE.STOP; f.voltage_mV = 0; f.current_mA = 0; }
  sync();
}

// ---- derived readouts -------------------------------------------------------
function detectorCenterAngle() { return ctFilamentBaseAngle(state.collimatorCenter) + 180; }
function sourceToDetectorMM() {
  const fa = (ctFilamentBaseAngle(state.activeFilament) + state.gantryAngle) * Math.PI / 180;
  const da = detectorCenterAngle() * Math.PI / 180;
  const sx = CT.R_SOURCE * Math.cos(fa), sy = CT.R_SOURCE * Math.sin(fa);
  const dx = CT.R_DETECTOR * Math.cos(da), dy = CT.R_DETECTOR * Math.sin(da);
  return Math.hypot(sx - dx, sy - dy);
}

// ---- render -----------------------------------------------------------------
function sync() {
  refreshStates();
  geo.update({
    collimatorCenter: state.collimatorCenter,
    activeFilament: state.activeFilament,
    gantryAngle: state.gantryAngle,
    gantryMax: state.gantryMax,
    collimatorDir: state.collimatorDir,
    filamentDir: state.filamentDir,
    filaments, vMax: V_MAX, iMax: I_MAX, mAsMax: maxMAs(),
    stateColor: STATE_COLOR,
    hover: hoverFil,
  });

  $('collimatorSlider').value = state.collimatorCenter;
  $('collimatorVal').textContent = state.collimatorCenter;
  $('windowSlider').value = state.windowPos;
  $('activeVal').textContent = state.activeFilament;
  $('gantrySlider').value = state.gantryAngle.toFixed(2);
  $('gantryVal').textContent = state.gantryAngle.toFixed(2) + '°';
  $('windowRange').textContent =
    `${ctMod(state.collimatorCenter - HALF, N)}…${ctMod(state.collimatorCenter + HALF, N)}`;

  const famAngle = ctFilamentBaseAngle(state.activeFilament) + state.gantryAngle;
  $('roActive').textContent = state.activeFilament;
  $('roAngle').textContent = famAngle.toFixed(2) + '°';
  $('roColl').textContent = state.collimatorCenter;
  $('roDet').textContent = ((detectorCenterAngle() + 540) % 360 - 180).toFixed(2) + '°';
  $('roGantry').textContent = state.gantryAngle.toFixed(2) + '°';
  $('roScan').textContent = `${state.windowPos + 1} / ${CT.COVERAGE}`;
  $('roRing').textContent = `${state.ringStep + 1} / ${N}`;
  $('roSdd').textContent = sourceToDetectorMM().toFixed(1) + ' mm';

  $('geoSummary').textContent = `filament ${state.activeFilament} @ ${famAngle.toFixed(1)}°`;
  renderDetail(hoverFil >= 0 ? hoverFil : state.activeFilament, hoverFil >= 0);

  $('modeBadge').textContent = 'Mode: ' + (state.mode === 'precision' ? 'Precision' : 'Stationary');
  const pct = Math.round((state.ringStep / N) * 100);
  const pb = $('progressBadge');
  if (playTimer) { pb.textContent = `Scanning… ring ${pct}%`; pb.className = 'badge heartbeat-alive'; }
  else { pb.textContent = `ring ${pct}%`; pb.className = 'badge heartbeat-idle'; }
}

// selected/hovered filament detail card
function renderDetail(i, isHover) {
  const f = filaments[i];
  const hw = filamentToHw(i);
  $('selFilTitle').textContent = isHover ? `Filament ${i} (hover)` : `Filament ${i} (active)`;
  $('selFilHw').textContent = hw.label;
  const sc = STATE_COLOR[f.state];
  const sb = $('selFilState');
  sb.textContent = STATE_NAME[f.state];
  sb.style.background = sc + '33';
  sb.style.color = sc;
  $('selFilV').textContent = (f.voltage_mV / 1000).toFixed(3) + ' V';
  $('selFilI').textContent = f.current_mA.toFixed(1) + ' mA';
  $('selFilMas').textContent = f.mAs.toFixed(3) + ' mAs';
}

// ---- play loop --------------------------------------------------------------
function startPlay() {
  if (playTimer) return;
  const fps = parseInt($('speedInput').value, 10) || 12;
  playTimer = setInterval(advance, 1000 / fps);
  $('playBtn').textContent = '⏸ Pause';
  $('playBtn').classList.add('playing');
  sync();
}
function stopPlay() {
  if (!playTimer) return;
  clearInterval(playTimer); playTimer = null;
  $('playBtn').textContent = '▶ Play';
  $('playBtn').classList.remove('playing');
  sync();
}
function togglePlay() { playTimer ? stopPlay() : startPlay(); }

// ---- mode -------------------------------------------------------------------
function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll('#modeSeg .seg-btn').forEach((b) =>
    b.classList.toggle('active', b.dataset.mode === mode));
  const precision = mode === 'precision';
  $('gantryBlock').classList.toggle('mode-precision', precision);
  $('gantrySlider').disabled = precision;
  $('modeHelp').textContent = precision
    ? 'Sweep the gantry across ±max in N steps, firing all 35 filaments at each angle; ' +
      'on reaching an extreme, advance the collimator and reverse the sweep.'
    : 'Gantry fixed. Scan the 35 covered filaments, then step the collimator+detector ' +
      'to the next filament center — 96 steps around the ring.';
  reset();
}

function setCollimatorDir(d) {
  state.collimatorDir = d;
  $('dirCcwBtn').classList.toggle('active', d === +1);
  $('dirCwBtn').classList.toggle('active', d === -1);
  sync();
}
function setFilamentDir(d) {
  state.filamentDir = d;
  $('filCcwBtn').classList.toggle('active', d === +1);
  $('filCwBtn').classList.toggle('active', d === -1);
  if (state.mode === 'precision') {
    state.sweepDir = d;
    state.gantryIndex = d > 0 ? 0 : 2 * state.gantrySteps;
    applyGantryFromIndex();
  }
  sync();
}

// ---- wiring -----------------------------------------------------------------
function init() {
  geo = new CTGeometry($('ctCanvas'));

  document.querySelectorAll('#modeSeg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => setMode(b.dataset.mode)));

  $('collimatorSlider').addEventListener('input', (e) => {
    state.collimatorCenter = parseInt(e.target.value, 10); sync();
  });
  $('dirCcwBtn').addEventListener('click', () => setCollimatorDir(+1));
  $('dirCwBtn').addEventListener('click', () => setCollimatorDir(-1));
  $('filCcwBtn').addEventListener('click', () => setFilamentDir(+1));
  $('filCwBtn').addEventListener('click', () => setFilamentDir(-1));

  $('windowSlider').addEventListener('input', (e) => {
    state.windowPos = parseInt(e.target.value, 10); sync();
  });

  $('gantrySlider').addEventListener('input', (e) => {
    if (state.mode === 'stationary') { state.gantryAngle = parseFloat(e.target.value); sync(); }
  });
  $('gantryMaxInput').addEventListener('input', (e) => {
    state.gantryMax = Math.max(1, parseFloat(e.target.value) || 10);
    $('gantrySlider').min = -state.gantryMax;
    $('gantrySlider').max = state.gantryMax;
    if (state.mode === 'precision') { state.gantryIndex = 0; applyGantryFromIndex(); }
    sync();
  });
  $('gantryStepsInput').addEventListener('input', (e) => {
    state.gantrySteps = Math.max(1, parseInt(e.target.value, 10) || 5);
    if (state.mode === 'precision') { state.gantryIndex = 0; applyGantryFromIndex(); }
    sync();
  });

  $('playBtn').addEventListener('click', togglePlay);
  $('stepBtn').addEventListener('click', () => { stopPlay(); advance(); });
  $('resetBtn').addEventListener('click', reset);
  $('speedInput').addEventListener('input', (e) => {
    $('speedVal').textContent = `${e.target.value} fil/s`;
    if (playTimer) { stopPlay(); startPlay(); }
  });

  // geometry diameters (mm) — rescale the canvas on change
  $('sourceDiaInput').addEventListener('input', (e) => {
    const d = parseFloat(e.target.value);
    if (d > 0) { CT.R_SOURCE = d / 2; geo._resize(); sync(); }
  });
  $('detDiaInput').addEventListener('input', (e) => {
    const d = parseFloat(e.target.value);
    if (d > 0) { CT.R_DETECTOR = d / 2; geo._resize(); sync(); }
  });

  $('showBeamChk').addEventListener('change', (e) => geo.setOptions({ beam: e.target.checked }));
  $('showIndexChk').addEventListener('change', (e) => geo.setOptions({ indices: e.target.checked }));
  $('showWindowChk').addEventListener('change', (e) => geo.setOptions({ wedge: e.target.checked }));
  $('showDetGridChk').addEventListener('change', (e) => geo.setOptions({ detGrid: e.target.checked }));

  // hover hit-test on the ring
  const cv = $('ctCanvas');
  cv.addEventListener('mousemove', (e) => {
    const rect = cv.getBoundingClientRect();
    const i = geo.hitTest(e.clientX - rect.left, e.clientY - rect.top);
    if (i !== hoverFil) { hoverFil = i; sync(); }
  });
  cv.addEventListener('mouseleave', () => { if (hoverFil !== -1) { hoverFil = -1; sync(); } });

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT') return;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    else if (e.code === 'ArrowRight') { e.preventDefault(); stopPlay(); advance(); }
  });

  $('gantrySlider').min = -state.gantryMax;
  $('gantrySlider').max = state.gantryMax;
  sync();
}

document.addEventListener('DOMContentLoaded', init);
