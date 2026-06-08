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
import { initControllers } from './controllers.js';
import { initScheduleTable } from './schedule.js';
import { ScheduleGantt } from './gantt.js';

const $ = (id) => document.getElementById(id);
const setStatus = (msg) => { $('statusBar').textContent = msg; };
const HALF = (CT.COVERAGE - 1) / 2; // 17
const N = CT.N_FILAMENTS;

// ---- firmware PowerState mirror --------------------------------------------
const STATE = { STOP: 1, SLEEP: 2, STANDBY: 3, IDLE: 4, ACTIVE: 5, VOLTAGE: 6 };
const STATE_NAME = { 1: 'Stop', 2: 'Sleep', 3: 'Standby', 4: 'Idle', 5: 'Active', 6: 'Voltage' };
const STATE_COLOR = {
  1: '#3b444c', // Stop    — dark, fully off
  2: '#4d6fae', // Sleep   — cool blue, INA only
  3: '#c79a3a', // Standby — amber, 0.8 V detection
  4: '#3fb6a0', // Idle    — teal, ~1 A closed-loop CC
  5: '#ff5d5d', // Active  — red, ~3 A closed-loop CC
  6: '#9a6ed2', // Voltage — violet, manual mV
};
const FAULT_NAME = { 0: 'none', 1: 'open', 2: 'OCP' };
const V_MAX = 5000;   // mV full-scale for the outward voltage bar
const I_MAX = 3500;   // mA full-scale for the outward current bar (heating ≤ ~3 A)
const SHOT_S = 0.0005; // demo dwell per fired filament (s) for mAs integration

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

// ---- per-filament telemetry + per-filament pulse/heating plan ---------------
const DEFAULT_PULSES = 1;
const DEFAULT_DUR_US = 1000; // 1 ms, shown in µs
const filaments = Array.from({ length: N }, () => ({
  state: STATE.STOP, voltage_mV: 0, current_mA: 0, mAs: 0,
  pulses: DEFAULT_PULSES, durationUs: DEFAULT_DUR_US,
  idleA: 1, activeA: 3,      // cathode heating currents (A)
  ocp: 4000, dcHv: false, // debug: per-board OCP (mA, firmware default 4 A), DC HV bit
}));

// Heating plan — bound to the emission schedule. Per the firmware design
// (docs/heating_schedule_design.md): all filaments rest at IDLE; each is
// promoted to ACTIVE T_settle (a trigger lead) before its emission window and
// demoted after, with a per-controller power cap. idle/active currents are the
// per-filament defaults; the rest are plan-wide.
// confirmed system constants (docs/heating_schedule_design.md v2), not GUI inputs.
// holdMs = stay ACTIVE this long after the last pulse before demoting to IDLE.
const heatingSchedule = { tSettleMs: 1000, rotationMs: 30000, holdMs: 200 };

// view mode: 'live' (hardware position + sensors), 'plan' (edit + play sim),
// 'debug' (per-filament heating / debug pulses, right-click to edit power)
let viewMode = 'plan';

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


// ---- power model ------------------------------------------------------------
// During a scan the collimator-covered 35 sit at Idle by default. The current
// filament fires (Active) and the next PREHEAT filaments in scan order are also
// held Active to pre-heat their cathodes; already-scanned filaments fall back to
// Idle. Everything outside the window sleeps. Real telemetry replaces this in
// ingestTelemetry().
function refreshStates() {
  // Only the Plan view simulates. Live/Debug reflect actual per-filament data.
  if (viewMode !== 'plan') return;
  // Bound-schedule heating: every filament rests at IDLE (warm pool); the
  // derived plan promotes the hot band (collimator window + T_settle lead) to
  // ACTIVE at the current pulse trigger. The firing filament emits.
  const t = currentTrigger();
  for (let i = 0; i < N; i++) {
    const f = filaments[i];
    if (isActiveAt(i, t)) {
      f.state = STATE.ACTIVE;
      f.voltage_mV = 4500 + (Math.random() - 0.5) * 120;
      f.current_mA = f.activeA * 1000 + (Math.random() - 0.5) * 80;
    } else {
      f.state = STATE.IDLE;       // warm pool — ready to promote
      f.voltage_mV = 1500 + (Math.random() - 0.5) * 40;
      f.current_mA = f.idleA * 1000 + (Math.random() - 0.5) * 40;
    }
  }
}

// integrate mAs for one fired filament
function fireFilament(i) {
  filaments[i].mAs += (1150 / 1000) * SHOT_S * 1000; // mA·s -> mAs (scaled for demo)
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

// ---- scan schedule ----------------------------------------------------------
// The schedule is the materialized full scan (one row per firing). The geometry
// IS the schedule's current row, so Play/Step/row-click all keep them in sync.
const MAX_ROWS = 8192;            // firmware schedule cap
let schedule = [];
let totalTriggers = 0;           // total SyncIn pulses in one rotation
let liveSeq = 0;
// the pulse-trigger position of the current scan row
function currentTrigger() { return schedule[liveSeq] ? schedule[liveSeq].trigger : 0; }
let scheduleTruncated = false;
let scheduleTable = null;

function gantryAnglesCfg(gantryMax, gantrySteps) {
  const n = 2 * gantrySteps + 1, out = [];
  for (let k = 0; k < n; k++) out.push(-gantryMax + 2 * gantryMax * (k / (n - 1)));
  return out;
}

// advance a sim state by one firing — identical logic to the live scan
function stepScan(st, cfg, angles) {
  st.windowPos++;
  if (st.windowPos >= CT.COVERAGE) {
    st.windowPos = 0;
    if (cfg.mode === 'stationary') {
      st.collimatorCenter = ctMod(st.collimatorCenter + cfg.collimatorDir, N);
      st.ringStep++;
    } else {
      const next = st.gantryIndex + st.sweepDir;
      if (next < 0 || next >= angles.length) {
        st.sweepDir *= -1;
        st.collimatorCenter = ctMod(st.collimatorCenter + cfg.collimatorDir, N);
        st.ringStep++;
      } else st.gantryIndex = next;
      st.gantryAngle = angles[st.gantryIndex];
    }
  }
}

// generate the full scan from the current geometry as origin (capped at 8192)
function buildSchedule() {
  const cfg = { mode: state.mode, collimatorDir: state.collimatorDir };
  const angles = gantryAnglesCfg(state.gantryMax, state.gantrySteps);
  const startIdx = state.filamentDir > 0 ? 0 : angles.length - 1;
  const sim = {
    windowPos: 0,
    collimatorCenter: state.collimatorCenter,
    gantryIndex: startIdx,
    sweepDir: state.filamentDir,
    ringStep: 0,
    gantryAngle: state.mode === 'precision' ? angles[startIdx] : (state.gantryAngle || 0),
  };
  const rows = [];
  scheduleTruncated = false;
  let trig = 0; // pulse-indexed trigger timeline (a burst spans `pulses` triggers)
  while (sim.ringStep < N) {
    if (rows.length >= MAX_ROWS) { scheduleTruncated = true; break; }
    const fil = ctMod(sim.collimatorCenter - HALF + sim.windowPos, N);
    const burstLen = Math.max(1, filaments[fil].pulses);
    rows.push({
      seq: rows.length,
      trigger: trig,                        // start pulse of this burst
      burstLen,                             // # pulses in this burst
      filament: fil,
      pulses: burstLen,
      duration: filaments[fil].durationUs,  // per-filament duration (µs)
      coll: sim.collimatorCenter, gantry: sim.gantryAngle,
      windowPos: sim.windowPos, ringStep: sim.ringStep,
    });
    trig += burstLen;
    stepScan(sim, cfg, angles);
  }
  totalTriggers = trig;
  return rows;
}

function updateScheduleHeader() {
  const lbl = $('schGanttLabel'); if (lbl) lbl.textContent = `${schedule.length} emit · ${heatRows.length} heat`;
  if (scheduleView === 'heating') { $('schCount').textContent = `${heatRows.length} state changes`; return; }
  $('schCount').textContent = scheduleTruncated
    ? `${schedule.length} / 8192 (capped)` : `${schedule.length} emission rows`;
}

let scheduleView = 'emission';
let gantt = null;

function rebuildSchedule() {
  schedule = buildSchedule();
  planHeating();           // derive + validate the bound heating layer
  applyScheduleView();     // (re)populate the active view
  updateScheduleHeader();
  gotoSeq(0, false);
}

function applyScheduleView() {
  if (!scheduleTable) return;
  if (scheduleView === 'heating') {
    scheduleTable.setColumns(['Trigger', 'Filament', '→ State', 'Current'],
      (r) => [r.seq, r.filament, STATE_NAME[r.state], r.arg + ' mA']);
    scheduleTable.setRows(heatRows);
  } else {
    scheduleTable.setColumns(['Seq Idx', 'Filament Idx', '# Pulses', 'Pulse Duration'],
      (r) => [r.seq, r.filament, r.pulses, r.duration + ' µs']);
    scheduleTable.setRows(schedule);
  }
  updateTableActive();
}

// the top Gantt is always live (not a toggle view). X axis = pulse triggers.
function drawGantt() {
  if (!gantt || !heatingPlan) return;
  gantt.update({ len: heatingPlan.len, intervals: heatingPlan.intervals, emissions: schedule, liveSeq: currentTrigger() });
}

// reflect the live trigger: top Gantt playhead + the middle list highlight
function updateTableActive() {
  drawGantt();
  if (!scheduleTable) return;
  if (scheduleView === 'heating') {
    const t = currentTrigger();
    let idx = -1;
    for (let i = 0; i < heatRows.length; i++) { if (heatRows[i].seq <= t) idx = i; else break; }
    scheduleTable.setActive(idx);
  } else {
    scheduleTable.setActive(liveSeq);
  }
}

function setScheduleView(view) {
  scheduleView = view;
  document.querySelectorAll('#schViewSeg .seg-btn').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  applyScheduleView();
  updateScheduleHeader();
}

// ---- bound heating plan (derived from the emission schedule) ----------------
// One bound schedule: each filament is promoted ACTIVE a trigger-lead before its
// emission run and demoted IDLE after (cyclic), then validated for power/settle.
let heatingPlan = null;

function planHeating() {
  const len = totalTriggers;   // timeline is in pulses, not bursts
  if (!schedule.length || !len) { heatingPlan = null; renderValidation(); return; }
  const pulseMs = heatingSchedule.rotationMs / len;
  const leadBursts = Math.max(1, Math.ceil(heatingSchedule.tSettleMs / pulseMs));
  const holdBursts = Math.max(1, Math.ceil(heatingSchedule.holdMs / pulseMs));

  // emission bursts per filament, in pulse-trigger units {start, end}
  const burstsByFil = new Map();
  for (const r of schedule) {
    let a = burstsByFil.get(r.filament);
    if (!a) burstsByFil.set(r.filament, (a = []));
    a.push({ start: r.trigger, end: r.trigger + r.burstLen });
  }
  // each filament's window run = the arc complementary to its largest dark gap
  const intervals = new Map();
  const deltas = [];
  for (const [fil, bursts] of burstsByFil) {
    bursts.sort((a, b) => a.start - b.start);
    let maxGap = -1, gapAt = 0;
    for (let i = 0; i < bursts.length; i++) {
      const next = bursts[(i + 1) % bursts.length];
      const gap = ctMod(next.start - bursts[i].end, len); // dark pulses between bursts
      if (gap > maxGap) { maxGap = gap; gapAt = i; }
    }
    const lastEnd = bursts[gapAt].end;                  // end of the run (last pulse)
    const firstStart = bursts[(gapAt + 1) % bursts.length].start; // start of the run
    const promote = ctMod(firstStart - leadBursts, len);
    const demote = ctMod(lastEnd + holdBursts, len);    // hold ACTIVE holdMs after the last pulse
    intervals.set(fil, { promote, demote });
    // arg16 = CC target current (mA) carried in the heating delta -> downloaded
    deltas.push({ seq: promote, filament: fil, state: STATE.ACTIVE, arg: Math.round(filaments[fil].activeA * 1000) });
    deltas.push({ seq: demote, filament: fil, state: STATE.IDLE, arg: Math.round(filaments[fil].idleA * 1000) });
  }
  deltas.sort((a, b) => a.seq - b.seq);
  heatingPlan = { len, leadBursts, holdBursts, intervals, deltas };
  buildHeatRows();
  validatePlan();
}

// display rows for the Heating view (the state-change timeline)
let heatRows = [];
function buildHeatRows() {
  heatRows = (heatingPlan ? heatingPlan.deltas : []).map((d) => ({
    seq: d.seq, filament: d.filament, state: d.state, arg: d.arg,
  }));
}

// active if the live trigger sits inside the filament's [promote, demote) arc
function isActiveAt(fil, t) {
  const iv = heatingPlan && heatingPlan.intervals.get(fil);
  if (!iv) return false;
  const len = heatingPlan.len;
  const span = ctMod(iv.demote - iv.promote, len) || len;
  return ctMod(t - iv.promote, len) < span;
}

// the only validation that matters: is every emission preceded by its filament
// reaching ACTIVE ≥ T_settle earlier? We promote `leadBursts` before each run,
// so the worst-case actual lead = min over filaments. It fails only if T_settle
// can't fit in a rotation, or a filament's emission run leaves no room.
function validatePlan() {
  const len = heatingPlan.len, lead = heatingPlan.leadBursts;
  const burstMs = heatingSchedule.rotationMs / len;
  let minLead = Infinity, worst = -1;
  for (const [fil, iv] of heatingPlan.intervals) {
    // actual ACTIVE-before-first-emission = the promote→firstEmission distance,
    // capped by the filament's own active span (can't exceed its run length).
    const span = ctMod(iv.demote - iv.promote, len) || len;
    const actual = Math.min(lead, span);
    if (actual < minLead) { minLead = actual; worst = fil; }
  }
  if (!isFinite(minLead)) minLead = 0;
  const actualMs = minLead * burstMs;
  const ok = lead < len && actualMs + 1e-6 >= heatingSchedule.tSettleMs;
  heatingPlan.validation = { lead, minLead, actualMs, worst, burstMs, ok };
  renderValidation();
}

function renderValidation() {
  const el = $('planVerdict');
  if (!el) return;
  const v = heatingPlan && heatingPlan.validation;
  if (!v) { el.textContent = ''; el.className = 'plan-verdict'; return; }
  const angles = (heatingPlan.len ? v.lead / heatingPlan.len * N : 0).toFixed(1); // 96 angles / rotation
  el.className = 'plan-verdict ' + (v.ok ? 'ok' : 'bad');
  el.title = v.ok
    ? 'Each filament reaches stable ACTIVE at least T settle before its first pulse.'
    : `Filament ${v.worst} can't reach T settle of ACTIVE before firing — shorten T settle or slow the rotation.`;
  el.innerHTML =
    `<b>${v.ok ? '✓ settle OK' : '✗ settle FAILS'}</b> · turn each filament ACTIVE ` +
    `<b>${v.lead}</b> triggers (~${angles} angles) before its window` +
    (v.ok ? `.` : ` — short on filament ${v.worst}.`);
}

// move the geometry to schedule[seq]; fire its filament when stepping
function gotoSeq(seq, fire) {
  if (!schedule.length) return;
  liveSeq = ctMod(seq, schedule.length);
  const row = schedule[liveSeq];
  state.collimatorCenter = row.coll;
  state.gantryAngle = row.gantry;
  state.windowPos = row.windowPos;
  state.ringStep = row.ringStep;
  if (fire) fireFilament(row.filament);
  updateTableActive();
  sync();
}

// after a manual geometry change, re-point the live row (or rebuild if off-plan)
function resyncSchedule() {
  for (let i = 0; i < schedule.length; i++) {
    const r = schedule[i];
    if (r.coll === state.collimatorCenter && r.windowPos === state.windowPos &&
        Math.abs(r.gantry - state.gantryAngle) < 0.01) {
      liveSeq = i;
      updateTableActive(); // moves the Gantt playhead + list highlight together
      return;
    }
  }
  rebuildSchedule();
}

function advance() {
  if (!schedule.length) { rebuildSchedule(); return; }
  gotoSeq(liveSeq + 1, true);
}

function reset() {
  stopPlay();
  for (const f of filaments) { f.mAs = 0; f.state = STATE.STOP; f.voltage_mV = 0; f.current_mA = 0; }
  rebuildSchedule();
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
let selFil = 0; // filament currently shown in the detail card
function renderDetail(i, isHover) {
  selFil = i;
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
  // per-filament plan inputs (don't clobber a field being typed in)
  const ae = document.activeElement;
  if (ae !== $('selFilPulses')) $('selFilPulses').value = f.pulses;
  if (ae !== $('selFilDur')) $('selFilDur').value = f.durationUs;
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

// ---- view mode (live / plan / debug) ---------------------------------------
function setViewMode(mode) {
  viewMode = mode;
  stopPlay();
  hideHeatPopup();
  document.body.setAttribute('data-view', mode);
  document.querySelectorAll('#viewModeSeg .seg-btn').forEach((b) =>
    b.classList.toggle('active', b.dataset.view === mode));
  // only Plan can simulate-and-play and edit the motion/schedule
  const plan = mode === 'plan';
  for (const id of ['playBtn', 'stepBtn', 'resetBtn']) $(id).disabled = !plan;
  for (const id of ['schApplyBtn', 'schSyncBtn']) $(id).disabled = !plan;
  setStatus(mode === 'live'
    ? 'Live — reflects the actual gantry position and INA219 sensor data from the controllers.'
    : mode === 'debug'
      ? 'Debug — pick any filament to set heating / fire pulses. Right-click the V·I or mAs rings to edit heating power.'
      : 'Plan — edit the scan schedule and Play it (simulated).');
  if (plan) refreshStates();
  sync();
}

// ---- debug power/pulse editor: wired to the real RP2350B commands ----------
// Each field is a separate command (heating V = CH_SET_TPS_VOLTAGE, OCP =
// CH_SET_TPS_OCP_THRESHOLD, heating I is measured via CH_GET_INA219, DC HV =
// HV bit, Fire = HV_PULSE). Filament index -> (controller, channel, mux).
let heatTarget = -1;

async function cmd(fil, command, extra) {
  const hw = filamentToHw(fil);
  const body = { controller: hw.controller, command, target: 'single', channel: hw.channel, mux_port: hw.mux, ...extra };
  const r = await fetch('/api/cmd', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  return r.json();
}
const heatMsg = (m) => { $('heatStatus').textContent = m; };
function highlightState(st) {
  document.querySelectorAll('#heatStateSeg button').forEach((b) => b.classList.toggle('active', +b.dataset.state === st));
}

// Set PowerState — the firmware closed-loop driver. IDLE/ACTIVE carry the
// heating-current (mA) target, VOLTAGE carries a manual mV; others arg=0.
async function dbgSetState(i, st) {
  const f = filaments[i];
  let arg = 0;
  if (st === STATE.IDLE || st === STATE.ACTIVE) arg = Math.max(0, parseInt($('heatI').value, 10) || 0);
  else if (st === STATE.VOLTAGE) arg = Math.max(0, parseInt($('heatV').value, 10) || 0);
  f.state = st;
  if (st === STATE.IDLE) f.current_mA = arg || f.idleA * 1000;
  else if (st === STATE.ACTIVE) f.current_mA = arg || f.activeA * 1000;
  else if (st === STATE.VOLTAGE) { f.voltage_mV = arg; f.current_mA = 0; }
  else if (st === STATE.STANDBY) { f.voltage_mV = 800; f.current_mA = 0; }
  else { f.voltage_mV = 0; f.current_mA = 0; }
  highlightState(st); sync();
  const j = await cmd(i, 'CH_SET_POWER_STATE', { state: st, arg });
  const unit = st === STATE.VOLTAGE ? ' mV' : (st === STATE.IDLE || st === STATE.ACTIVE) ? ' mA' : '';
  heatMsg(j.ok ? `${STATE_NAME[st]}${arg ? ' @ ' + arg + unit : ''} set.` : `State: ${j.error}`);
}
async function dbgSetOcp(i) {
  const ma = Math.max(0, parseInt($('heatOcp').value, 10) || 0);
  filaments[i].ocp = ma;
  const j = await cmd(i, 'CH_SET_TPS_OCP_THRESHOLD', { threshold_mA: ma });
  heatMsg(j.ok ? `OCP set to ${ma} mA.` : `OCP: ${j.error}`);
}
async function dbgReadIna(i) {
  const j = await cmd(i, 'CH_GET_INA219', {});
  const d = j.ok && j.response && j.response.decoded;
  if (d) {
    filaments[i].current_mA = d.current_mA; filaments[i].voltage_mV = d.bus_mV; sync();
    $('heatImeas').textContent = `${d.current_mA} mA`;
    $('heatVmeas').textContent = `${d.bus_mV} mV`;
    heatMsg(`INA219: ${d.bus_mV} mV / ${d.current_mA} mA.`);
  } else heatMsg(`Read: ${j.error || 'no data'}`);
}
async function dbgReadState(i) {
  const j = await cmd(i, 'CH_GET_POWER_STATE', {});
  const raw = j.ok && j.response && (j.response.raw || (j.response.decoded && j.response.decoded.raw));
  if (raw && raw.length >= 5) {
    const st = raw[3], fault = raw[4];
    filaments[i].state = st; highlightState(st); sync();
    $('heatStateNow').textContent = `${STATE_NAME[st] || st} · fault ${FAULT_NAME[fault] || fault}`;
  } else $('heatStateNow').textContent = j.ok ? '— · fault —' : `(${j.error})`;
}
async function dbgFire(i) {
  const f = filaments[i];
  f.durationUs = Math.max(1, parseInt($('heatPw').value, 10) || 1);
  const hw = filamentToHw(i);
  const j = await cmd(i, 'HV_PULSE', { channel: hw.channel, bit: hw.mux, width_us: f.durationUs, verify_mode: 0 });
  if (j.ok) { f.mAs += f.activeA * (f.durationUs / 1e6) * 1000; sync(); }
  heatMsg(j.ok ? `Pulsed ${f.durationUs} µs.` : `Fire: ${j.error}`);
}
async function dbgReadHv(i) {
  const hw = filamentToHw(i);
  const j = await cmd(i, 'HV_GET_ALL_BYTES', {});
  const d = j.ok && j.response && j.response.decoded;
  if (d && d.feedback) { setHvButton(i, !!(d.feedback[hw.channel] & (1 << hw.mux))); }
  else { setHvButton(i, null); if (!j.ok) heatMsg(`HV: ${j.error}`); }
}
async function dbgToggleHv(i) {
  const next = !filaments[i].dcHv;
  const hw = filamentToHw(i);
  const j = await cmd(i, 'HV_SET_BIT', { channel: hw.channel, bit: hw.mux, value: next, verify: true });
  if (j.ok) setHvButton(i, next);
  heatMsg(j.ok ? `DC HV ${next ? 'ON' : 'off'}.` : `HV: ${j.error}`);
}
function setHvButton(i, on) {
  filaments[i].dcHv = !!on;
  const b = $('heatHv');
  b.className = 'sm hv-btn ' + (on == null ? 'unknown' : on ? 'on' : 'off');
  b.textContent = on == null ? 'read DC HV status…' : on ? 'DC HV ON — click to turn off' : 'DC HV off — click to turn on';
}

function showHeatPopup(clientX, clientY, fil) {
  heatTarget = fil;
  const f = filaments[fil], hw = filamentToHw(fil);
  $('heatFil').textContent = fil;
  $('heatAddr').textContent = hw.label;
  $('heatI').value = f.current_mA ? Math.round(f.current_mA) : f.activeA * 1000;
  $('heatV').value = f.voltage_mV > 0 ? Math.round(f.voltage_mV) : 800;
  $('heatOcp').value = f.ocp;
  $('heatPw').value = f.durationUs;
  $('heatImeas').textContent = f.current_mA ? `${f.current_mA.toFixed(0)} mA` : '— mA';
  $('heatVmeas').textContent = f.voltage_mV ? `${f.voltage_mV.toFixed(0)} mV` : '— mV';
  $('heatStateNow').textContent = `${STATE_NAME[f.state]} · fault —`;
  highlightState(f.state);
  setHvButton(fil, f.dcHv);
  heatMsg('');
  const p = $('heatPopup');
  p.hidden = false;
  const w = p.offsetWidth || 250, h = p.offsetHeight || 380;
  p.style.left = Math.max(8, Math.min(clientX, window.innerWidth - w - 8)) + 'px';
  p.style.top = Math.max(8, Math.min(clientY, window.innerHeight - h - 8)) + 'px';
  dbgReadState(fil); // pull live power-state + fault
  dbgReadHv(fil);    // and DC HV status
}
function hideHeatPopup() { $('heatPopup').hidden = true; heatTarget = -1; }

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

// ---- direct manipulation on the plot ---------------------------------------
// drag the beam      -> switch the collimated (active) filament
// drag a filament    -> rotate the gantry
// drag collimator/det-> move the detector (rotate the collimator+detector ring)
const STEP = 360 / N;
const WIN_HALF_DEG = HALF * STEP; // 63.75° half-span of the collimator window
let drag = null;
const angDist = (a, b) => Math.abs(((a - b + 540) % 360) - 180);
// The whole assembly rocks by gantryAngle, so a screen angle maps back to a
// filament by removing that offset first.
const filFromAngle = (angDeg) => ctMod(Math.round((90 - (angDeg - state.gantryAngle)) / STEP), N);

// Decide what the click grabbed. Collimator and detector are bounded to their
// actual footprints so a click elsewhere doesn't accidentally move the ring.
function dragModeAt(p) {
  const Rs = CT.R_SOURCE, Rd = CT.R_DETECTOR;
  const collA = ctFilamentBaseAngle(state.collimatorCenter) + state.gantryAngle; // rocked
  // collimator block: just inside the source ring, within the 35-window arc
  if (p.r >= Rs * 0.86 && p.r < Rs * 0.985 && angDist(p.ang, collA) <= WIN_HALF_DEG + 2) return 'collimator';
  // detector panel: near the detector ring, opposite the collimator center
  if (Math.abs(p.r - Rd) <= 22 && angDist(p.ang, collA + 180) <= 16) return 'detector';
  // filament ring + spectrum bars (outside the ring): rotate the gantry
  if (p.r >= Rs * 0.985) return 'gantry';
  // inner region along the beam: switch the collimated (active) filament
  return 'beam';
}

function applyDrag(p) {
  if (!drag) return;
  if (drag.mode === 'gantry') {
    const delta = ((p.ang - drag.startAng + 540) % 360) - 180;
    state.gantryAngle = Math.max(-state.gantryMax, Math.min(state.gantryMax, drag.startGantry + delta));
    if (state.mode === 'stationary') $('gantrySlider').value = state.gantryAngle.toFixed(2);
  } else if (drag.mode === 'collimator') {
    state.collimatorCenter = filFromAngle(p.ang);          // collimator follows the cursor
  } else if (drag.mode === 'detector') {
    state.collimatorCenter = filFromAngle(p.ang + 180);    // detector is opposite the center
  } else { // beam -> nearest covered filament (try both sides of iso-center)
    const base = state.collimatorCenter - HALF;
    let wp = ctMod(filFromAngle(p.ang) - base, N);
    if (wp > CT.COVERAGE - 1) {
      const wp2 = ctMod(filFromAngle(p.ang + 180) - base, N);
      if (wp2 <= CT.COVERAGE - 1) wp = wp2;
    }
    if (wp <= CT.COVERAGE - 1) state.windowPos = wp;
  }
  sync();
}

function attachPlotDrag(cv) {
  const at = (e) => {
    const rect = cv.getBoundingClientRect();
    return geo.screenToWorld(e.clientX - rect.left, e.clientY - rect.top);
  };
  cv.addEventListener('mousedown', (e) => {
    if (e.button !== 0 || viewMode === 'live') return; // live is hardware-driven
    stopPlay();
    const p = at(e);
    drag = { mode: dragModeAt(p) };
    if (drag.mode === 'gantry') { drag.startAng = p.ang; drag.startGantry = state.gantryAngle; }
    applyDrag(p);
    cv.style.cursor = 'grabbing';
    e.preventDefault();
  });
  // Debug: right-click the V·I / mAs ring region to edit heating power
  cv.addEventListener('contextmenu', (e) => {
    if (viewMode !== 'debug') return;
    const p = at(e);
    if (p.r < CT.R_SOURCE || p.r > geo._bands().outer + 4) return; // only the ring bands
    const i = geo.hitTest(e.clientX - cv.getBoundingClientRect().left, e.clientY - cv.getBoundingClientRect().top);
    const fil = i >= 0 ? i : filFromAngle(p.ang);
    e.preventDefault();
    showHeatPopup(e.clientX, e.clientY, fil);
  });
  cv.addEventListener('mousemove', (e) => {
    const rect = cv.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    if (drag) { applyDrag(geo.screenToWorld(mx, my)); return; }
    const i = geo.hitTest(mx, my);
    if (i !== hoverFil) { hoverFil = i; sync(); }
    cv.style.cursor = i >= 0 ? 'grab' : 'crosshair';
  });
  window.addEventListener('mouseup', () => { if (drag) { drag = null; cv.style.cursor = 'crosshair'; resyncSchedule(); } });
  cv.addEventListener('mouseleave', () => { if (!drag && hoverFil !== -1) { hoverFil = -1; sync(); } });
}

function resetGantry() {
  state.gantryAngle = 0;
  state.gantryIndex = state.filamentDir > 0 ? 0 : 2 * state.gantrySteps;
  state.sweepDir = state.filamentDir;
  $('gantrySlider').value = '0.00';
  rebuildSchedule();
}

// ---- wiring -----------------------------------------------------------------
// ---- per-filament settings list (collapsed, lazy-built) ---------------------
let rebuildTimer = null;
function scheduleRebuildDebounced() {
  if (rebuildTimer) clearTimeout(rebuildTimer);
  rebuildTimer = setTimeout(() => { rebuildTimer = null; rebuildSchedule(); }, 250);
}
function buildFilList() {
  const el = $('filList');
  if (el.dataset.built) return;
  el.dataset.built = '1';
  const frag = document.createDocumentFragment();
  for (let i = 0; i < N; i++) {
    const f = filaments[i];
    const row = document.createElement('div');
    row.className = 'fil-row';
    row.innerHTML = `<span>${i}</span>` +
      `<input data-i="${i}" data-k="pulses" type="number" min="1" value="${f.pulses}">` +
      `<input data-i="${i}" data-k="durationUs" type="number" min="1" value="${f.durationUs}">` +
      `<input data-i="${i}" data-k="idleA" type="number" min="0" step="0.1" value="${f.idleA}">` +
      `<input data-i="${i}" data-k="activeA" type="number" min="0" step="0.1" value="${f.activeA}">`;
    frag.appendChild(row);
  }
  el.appendChild(frag);
  el.addEventListener('input', (e) => {
    const inp = e.target;
    if (inp.tagName !== 'INPUT') return;
    const i = +inp.dataset.i, k = inp.dataset.k;
    const intK = (k === 'pulses' || k === 'durationUs');
    filaments[i][k] = intK ? Math.max(1, parseInt(inp.value, 10) || 1) : Math.max(0, parseFloat(inp.value) || 0);
    scheduleRebuildDebounced();
  });
}
function refreshFilList() {
  const el = $('filList');
  if (!el.dataset.built) return;
  el.querySelectorAll('input').forEach((inp) => { inp.value = filaments[+inp.dataset.i][inp.dataset.k]; });
}

// host-side persistence of the per-filament plan (pulses / duration / currents)
const FIL_STORE = 'ct_fil_settings';
function saveFilSettings() {
  const data = filaments.map((f) => ({ pulses: f.pulses, durationUs: f.durationUs, idleA: f.idleA, activeA: f.activeA }));
  try { localStorage.setItem(FIL_STORE, JSON.stringify(data)); $('schStatus').textContent = 'Saved per-filament settings to host.'; }
  catch (e) { $('schStatus').textContent = 'Save failed: ' + e; }
}
function loadFilSettings(quiet) {
  let data;
  try { data = JSON.parse(localStorage.getItem(FIL_STORE) || 'null'); } catch { data = null; }
  if (!data) { if (!quiet) $('schStatus').textContent = 'No saved settings on host.'; return false; }
  data.forEach((d, i) => { if (filaments[i]) Object.assign(filaments[i], d); });
  refreshFilList(); rebuildSchedule();
  if (!quiet) $('schStatus').textContent = 'Loaded per-filament settings from host.';
  return true;
}

async function uploadSchedule() {
  const st = $('schStatus');
  st.textContent = `Uploading ${schedule.length} rows…`;
  try {
    const rows = schedule.map((r) => ({
      seq: r.seq, filament: r.filament, pulses: r.pulses, duration: r.duration,
      idleA: filaments[r.filament].idleA, activeA: filaments[r.filament].activeA,
    }));
    // bound schedule: emission rows + derived heating deltas (promote/demote)
    const heating = heatingPlan ? heatingPlan.deltas.map((d) => ({ seq: d.seq, filament: d.filament, state: d.state, arg: d.arg })) : [];
    const v = heatingPlan && heatingPlan.validation;
    if (v && !v.ok && !confirm('Settle time is NOT satisfied for this schedule. Upload anyway?')) { st.textContent = 'Upload cancelled (settle not satisfied).'; return; }
    const res = await fetch('/api/schedule', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rows, heating, settle: { tSettleMs: heatingSchedule.tSettleMs, leadBursts: heatingPlan && heatingPlan.leadBursts } }),
    });
    const j = await res.json();
    st.textContent = j.ok ? `Staged ${j.rows} emission + ${heating.length} heating deltas.` : `Upload failed: ${j.error || '?'}`;
  } catch (e) {
    st.textContent = 'Upload failed: ' + e;
  }
}

function init() {
  geo = new CTGeometry($('ctCanvas'));
  initControllers();

  // scan schedule
  scheduleTable = initScheduleTable({
    body: $('schBody'), head: $('schHead'),
    onRowClick: (row) => { stopPlay(); gotoSeq(row.seq, false); },
  });
  gantt = new ScheduleGantt($('schGantt'));
  document.querySelectorAll('#schViewSeg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => setScheduleView(b.dataset.view)));
  $('filSaveBtn').addEventListener('click', saveFilSettings);
  $('filLoadBtn').addEventListener('click', () => loadFilSettings(false));
  $('schPulses').addEventListener('input', (e) => {
    scheduleDefaults.pulses = Math.max(1, parseInt(e.target.value, 10) || 1); rebuildSchedule();
  });
  $('schDuration').addEventListener('input', (e) => {
    scheduleDefaults.duration = Math.max(1, parseInt(e.target.value, 10) || 1); rebuildSchedule();
  });
  $('schSyncBtn').addEventListener('click', () => rebuildSchedule());
  $('schUploadBtn').addEventListener('click', uploadSchedule);
  $('schApplyBtn').addEventListener('click', () => {
    const pulses = Math.max(1, parseInt($('schPulses').value, 10) || 1);
    const dur = Math.max(1, parseInt($('schDuration').value, 10) || 1);
    const idleA = Math.max(0, parseFloat($('hsIdleA').value) || 0);
    const activeA = Math.max(0, parseFloat($('hsActiveA').value) || 0);
    for (const f of filaments) { f.pulses = pulses; f.durationUs = dur; f.idleA = idleA; f.activeA = activeA; }
    refreshFilList(); rebuildSchedule();
  });

  $('filListDetails').addEventListener('toggle', (e) => { if (e.target.open) buildFilList(); });

  // heating-plan parameters: re-derive the bound heating layer on change
  const onPlanParam = (id, key) => $(id).addEventListener('input', (e) => {
    heatingSchedule[key] = Math.max(0, parseInt(e.target.value, 10) || 0);
    planHeating(); applyScheduleView(); updateScheduleHeader(); sync();
  });
  onPlanParam('hsSettle', 'tSettleMs');
  onPlanParam('hsHold', 'holdMs');
  onPlanParam('hsRot', 'rotationMs');

  // view mode (live / plan / debug)
  document.querySelectorAll('#viewModeSeg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => setViewMode(b.dataset.view)));

  // per-filament pulse / duration / heating (Selected Filament card)
  $('selFilPulses').addEventListener('input', (e) => {
    filaments[selFil].pulses = Math.max(1, parseInt(e.target.value, 10) || 1); refreshFilList(); rebuildSchedule();
  });
  $('selFilDur').addEventListener('input', (e) => {
    filaments[selFil].durationUs = Math.max(1, parseInt(e.target.value, 10) || 1); refreshFilList(); rebuildSchedule();
  });
  $('selFilDebugBtn').addEventListener('click', () => {
    const b = $('selFilDebugBtn').getBoundingClientRect();
    showHeatPopup(b.left - 200, b.bottom + 6, selFil);
  });

  // debug popup — power-state ladder + per-field commands
  document.querySelectorAll('#heatStateSeg button').forEach((b) =>
    b.addEventListener('click', () => { if (heatTarget >= 0) dbgSetState(heatTarget, +b.dataset.state); }));
  $('heatSetOcp').addEventListener('click', () => { if (heatTarget >= 0) dbgSetOcp(heatTarget); });
  $('heatReadI').addEventListener('click', () => { if (heatTarget >= 0) dbgReadIna(heatTarget); });
  $('heatPulse').addEventListener('click', () => { if (heatTarget >= 0) dbgFire(heatTarget); });
  $('heatHv').addEventListener('click', () => { if (heatTarget >= 0) dbgToggleHv(heatTarget); });
  $('heatClose').addEventListener('click', hideHeatPopup);
  document.addEventListener('mousedown', (e) => {
    const p = $('heatPopup');
    if (!p.hidden && !p.contains(e.target) && e.target.id !== 'ctCanvas') hideHeatPopup();
  });

  document.querySelectorAll('#modeSeg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => setMode(b.dataset.mode)));

  $('collimatorSlider').addEventListener('input', (e) => {
    state.collimatorCenter = parseInt(e.target.value, 10); resyncSchedule(); sync();
  });
  $('dirCcwBtn').addEventListener('click', () => { setCollimatorDir(+1); rebuildSchedule(); });
  $('dirCwBtn').addEventListener('click', () => { setCollimatorDir(-1); rebuildSchedule(); });
  $('filCcwBtn').addEventListener('click', () => { setFilamentDir(+1); rebuildSchedule(); });
  $('filCwBtn').addEventListener('click', () => { setFilamentDir(-1); rebuildSchedule(); });

  $('windowSlider').addEventListener('input', (e) => {
    state.windowPos = parseInt(e.target.value, 10); resyncSchedule(); sync();
  });

  $('gantrySlider').addEventListener('input', (e) => {
    if (state.mode === 'stationary') { state.gantryAngle = parseFloat(e.target.value); rebuildSchedule(); }
  });
  $('gantryMaxInput').addEventListener('input', (e) => {
    state.gantryMax = Math.max(1, parseFloat(e.target.value) || 10);
    $('gantrySlider').min = -state.gantryMax;
    $('gantrySlider').max = state.gantryMax;
    rebuildSchedule();
  });
  $('gantryStepsInput').addEventListener('input', (e) => {
    state.gantrySteps = Math.max(1, parseInt(e.target.value, 10) || 5);
    rebuildSchedule();
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

  // direct manipulation + hover on the plot
  attachPlotDrag($('ctCanvas'));
  $('resetGantryBtn').addEventListener('click', resetGantry);

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || viewMode !== 'plan') return;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    else if (e.code === 'ArrowRight') { e.preventDefault(); stopPlay(); advance(); }
  });

  $('gantrySlider').min = -state.gantryMax;
  $('gantrySlider').max = state.gantryMax;
  document.body.setAttribute('data-view', 'plan');
  if (!loadFilSettings(true)) rebuildSchedule(); // restore host settings, else fresh build
}

document.addEventListener('DOMContentLoaded', init);
