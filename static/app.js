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
import { initPower } from './power.js';
import { initMapping } from './mapping.js';
import { initTests } from './tests.js';

const $ = (id) => document.getElementById(id);
const setStatus = (msg) => { $('statusBar').textContent = msg; };
let HALF = (CT.COVERAGE - 1) / 2; // window half-width (# active = 2·HALF+1)
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
// Scan-schedule UI shows V/A (firmware payloads stay mV/mA). Trim trailing zeros.
const maToA = (ma) => (ma / 1000).toFixed(3).replace(/\.?0+$/, '') + ' A';
const mvToV = (mv) => (mv / 1000).toFixed(3).replace(/\.?0+$/, '') + ' V';
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

// Filament -> board id via the live ACTIVE-LIST mapping (mapping.js), not the
// fixed wiring above. Each controller packs its assigned filaments into slots
// 0-63 in ascending filament order; slot k -> channel k>>3 (0-7), position k&7
// (0-7). Derived from the per-filament controller assignment so it stays correct
// mid-edit (before Apply repopulates the server slot detail). Returns null when
// unassigned or overflowing the 64 slots (can't fire). Shape matches filamentToHw.
function filamentToBoard(i) {
  const get = window.ctFilamentController;
  if (!get) return null;
  const c = get(i);
  if (c !== 0 && c !== 1) return null;        // unassigned
  let slot = 0;
  for (let g = 0; g < i; g++) if (get(g) === c) slot++;
  if (slot > 63) return null;                 // beyond the 64 power slots
  return { controller: c + 1, channel: slot >> 3, mux: slot & 7 };
}

// ---- per-filament telemetry + per-filament pulse/heating plan ---------------
const DEFAULT_PULSES = 1;
const DEFAULT_DUR_US = 1000; // 1 ms, shown in µs
const filaments = Array.from({ length: N }, () => ({
  state: STATE.STOP, voltage_mV: 0, current_mA: 0, mAs: 0,
  pulses: DEFAULT_PULSES, durationUs: DEFAULT_DUR_US,
  idleA: 1, activeA: 3,      // cathode heating currents (A)
  ocp: 4000, dcHv: false, // debug: per-board OCP (mA, firmware default 4 A), DC HV bit
  dead: false,            // disabled filament — omitted from the schedule (§8.4)
}));

// Heating plan — bound to the emission schedule. Per the firmware design
// (docs/heating_schedule_design.md): all filaments rest at IDLE; each is
// promoted to ACTIVE T_settle (a trigger lead) before its emission window and
// demoted after, with a per-controller power cap. idle/active currents are the
// per-filament defaults; the rest are plan-wide.
// holdMs = stay ACTIVE this long after the last pulse before demoting to IDLE.
// activeCount = max filaments allowed ACTIVE at once (the hot-band cap).
const heatingSchedule = { tSettleMs: 1000, rotationMs: 30000, holdMs: 200, activeCount: 40 };
// per-filament power for the PLAN total-power estimate (W). Live mode uses the
// real measured P = V·I instead.
const powerEst = { idleW: 1.8, activeW: 32 };

// view mode: 'live' (hardware position + sensors), 'plan' (edit + play sim),
// 'debug' (per-filament heating / debug pulses, right-click to edit power)
let viewMode = 'live';

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
  // Deterministic design values (NOT fake telemetry): the planned operating
  // point implied by the per-filament currents + configured power.
  const t = currentTrigger();
  for (let i = 0; i < N; i++) {
    const f = filaments[i];
    if (f.dead) { f.state = STATE.STOP; f.voltage_mV = 0; f.current_mA = 0; continue; }
    if (isActiveAt(i, t)) {
      f.state = STATE.ACTIVE;
      f.current_mA = f.activeA * 1000;
      f.voltage_mV = f.activeA > 0 ? powerEst.activeW / f.activeA * 1000 : 0; // V·I = activeW
    } else {
      f.state = STATE.IDLE;       // warm pool — ready to promote
      f.current_mA = f.idleA * 1000;
      f.voltage_mV = f.idleA > 0 ? powerEst.idleW / f.idleA * 1000 : 0;       // V·I = idleW
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
    if (!filaments[fil].dead) { // dead filaments fire nothing — a known gap
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
    }
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
  if (typeof invalidateRun === 'function') invalidateRun();   // a changed schedule must be re-downloaded
}

function applyScheduleView() {
  if (!scheduleTable) return;
  if (scheduleView === 'heating') {
    scheduleTable.setColumns(['Trigger', 'Filament', '→ State', 'Current'],
      (r) => [r.seq, r.filament, STATE_NAME[r.state], maToA(r.arg)]);
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

// peak filaments ACTIVE at once for a given pre-heat lead (cyclic diff sweep).
// Monotonic non-decreasing in `lead`, so we can binary-search a target peak.
function peakForLead(runs, len, lead, hold) {
  const diff = new Array(len + 1).fill(0);
  for (const run of runs) {
    const promote = ctMod(run.firstStart - lead, len);
    const demote = ctMod(run.lastEnd + hold, len);
    const span = ctMod(demote - promote, len) || len;
    if (promote + span <= len) { diff[promote]++; diff[promote + span]--; }
    else { diff[promote]++; diff[len]--; diff[0]++; diff[promote + span - len]--; }
  }
  let cur = 0, peak = 0;
  for (let t = 0; t < len; t++) { cur += diff[t]; if (cur > peak) peak = cur; }
  return peak;
}

function planHeating() {
  const len = totalTriggers;   // timeline is in pulses, not bursts
  if (!schedule.length || !len) { heatingPlan = null; renderValidation(); return; }
  const pulseMs = heatingSchedule.rotationMs / len;
  const triggersPerAngle = len / N;
  const holdBursts = Math.max(1, Math.ceil(heatingSchedule.holdMs / pulseMs));

  // emission bursts per filament, in pulse-trigger units {start, end}
  const burstsByFil = new Map();
  for (const r of schedule) {
    let a = burstsByFil.get(r.filament);
    if (!a) burstsByFil.set(r.filament, (a = []));
    a.push({ start: r.trigger, end: r.trigger + r.burstLen });
  }
  // each filament's window run = the arc complementary to its largest dark gap
  // (lead-independent: just the first/last pulse of the run)
  const runs = [];
  for (const [fil, bursts] of burstsByFil) {
    bursts.sort((a, b) => a.start - b.start);
    let maxGap = -1, gapAt = 0;
    for (let i = 0; i < bursts.length; i++) {
      const next = bursts[(i + 1) % bursts.length];
      const gap = ctMod(next.start - bursts[i].end, len); // dark pulses between bursts
      if (gap > maxGap) { maxGap = gap; gapAt = i; }
    }
    runs.push({
      fil,
      lastEnd: bursts[gapAt].end,                          // end of run (last pulse)
      firstStart: bursts[(gapAt + 1) % bursts.length].start, // start of run
    });
  }

  // Solve for the pre-heat lead so the ACTUAL peak ACTIVE count lands on
  // `# active`: binary-search the largest lead whose computed peak ≤ # active.
  let lo = 0, hi = len, leadBursts = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (peakForLead(runs, len, mid, holdBursts) <= heatingSchedule.activeCount) { leadBursts = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  const leadAngles = leadBursts / triggersPerAngle;

  const intervals = new Map();
  const deltas = [];
  for (const run of runs) {
    const promote = ctMod(run.firstStart - leadBursts, len);
    const demote = ctMod(run.lastEnd + holdBursts, len); // hold ACTIVE holdMs after last pulse
    intervals.set(run.fil, { promote, demote });
    // arg16 = CC target current (mA) carried in the heating delta -> downloaded
    deltas.push({ seq: promote, filament: run.fil, state: STATE.ACTIVE, arg: Math.round(filaments[run.fil].activeA * 1000) });
    deltas.push({ seq: demote, filament: run.fil, state: STATE.IDLE, arg: Math.round(filaments[run.fil].idleA * 1000) });
  }
  deltas.sort((a, b) => a.seq - b.seq);
  heatingPlan = { len, leadBursts, leadAngles, holdBursts, runs, intervals, deltas };
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
  const len = heatingPlan.len;
  const pulseMs = heatingSchedule.rotationMs / len;
  const leadMs = heatingPlan.leadBursts * pulseMs;        // pre-heat time before firing
  const settleOk = leadMs + 1e-6 >= heatingSchedule.tSettleMs;

  // the actual peak ACTIVE at once for the chosen lead (== what the ring shows)
  const peak = peakForLead(heatingPlan.runs, len, heatingPlan.leadBursts, heatingPlan.holdBursts);

  // decompose the REAL peak into window + lead + hold so the breakdown always
  // adds up: window is the collimator coverage, hold the post-fire tail, and the
  // pre-heat lead is whatever remains.
  const msPerAngle = heatingSchedule.rotationMs / N;
  const holdAngles = Math.max(1, Math.ceil(heatingSchedule.holdMs / msPerAngle));
  const windowN = Math.min(CT.COVERAGE, peak);
  const holdN = Math.max(0, Math.min(holdAngles, peak - windowN));
  const leadN = peak - windowN - holdN;   // window + lead + hold === peak by construction

  heatingPlan.validation = { leadMs, peak, windowN, leadN, holdN, settleOk, ok: settleOk };
  renderValidation();
}

function renderValidation() {
  const el = $('planVerdict');
  if (!el) return;
  const v = heatingPlan && heatingPlan.validation;
  if (!v) { el.textContent = ''; el.className = 'plan-verdict'; return; }
  el.className = 'plan-verdict ' + (v.ok ? 'ok' : 'bad');
  el.title = '# active is the target hot-band size. The pre-heat lead is solved so the actual ' +
    'peak ACTIVE count lands on it = collimator window + filaments pre-heated ahead + post-fire hold tail.';
  const tgt = v.peak === heatingSchedule.activeCount ? '' : ` (target ${heatingSchedule.activeCount})`;
  el.innerHTML =
    `<div>Hot band <b>${v.peak}</b> ACTIVE${tgt} = ${v.windowN} window + ${v.leadN} lead + ${v.holdN} hold.</div>` +
    `<div><b>${v.settleOk ? '✓ settle OK' : '✗ settle short'}</b> — pre-heat ` +
    `<b>${Math.round(v.leadMs)} ms</b> ${v.settleOk ? '≥' : '<'} ${heatingSchedule.tSettleMs} ms T settle` +
    (v.settleOk ? '.' : ' — raise # active or rotation time.') + `</div>`;
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
    hwMap: filamentToBoard,   // filament idx -> active-list {controller, channel, mux} for the HUD board-id readout
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
  renderPower();

  $('modeBadge').textContent = 'Mode: ' + (state.mode === 'precision' ? 'Precision' : 'Stationary');
  const pct = Math.round((state.ringStep / N) * 100);
  const pb = $('progressBadge');
  if (playTimer) { pb.textContent = `Scanning… ring ${pct}%`; pb.className = 'badge heartbeat-alive'; }
  else { pb.textContent = `ring ${pct}%`; pb.className = 'badge heartbeat-idle'; }
}

// total heating power, split by ACTIVE vs IDLE.
//  · Plan  — ESTIMATE: per-filament configured power (powerEst.idleW/activeW)
//            × the number of filaments in each state.
//  · Live  — MEASURED: real P = V·I (voltage_mV × current_mA / 1e6), present only.
function renderPower() {
  let active = 0, idle = 0;
  const live = viewMode === 'live';
  for (const f of filaments) {
    if (!f || f.dead) continue;
    if (f.state === STATE.ACTIVE) active += live ? (f.voltage_mV * f.current_mA) / 1e6 : powerEst.activeW;
    else if (f.state === STATE.IDLE) idle += live ? (f.voltage_mV * f.current_mA) / 1e6 : powerEst.idleW;
  }
  const fmt = (w) => (w >= 1000 ? (w / 1000).toFixed(2) + ' kW' : w.toFixed(1) + ' W');
  $('pwTotal').textContent = fmt(active + idle);
  $('pwActive').textContent = fmt(active);
  $('pwIdle').textContent = fmt(idle);
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
      : 'Plan — edit the scan schedule and Play it. Right-click a filament to disable/enable it.');
  if (plan) refreshStates();
  else clearLiveData();   // plan placeholder V/I must NOT carry into live/debug
  if (mode === 'live') startLiveTelemetry(); else stopLiveTelemetry();
  sync();
}

// blank all telemetry-derived fields — real hardware data fills them in Live,
// per-board reads in Debug. Prevents Plan's design values from lingering.
function clearLiveData() {
  for (const f of filaments) {
    if (f.dead) continue;
    f.state = STATE.STOP; f.voltage_mV = 0; f.current_mA = 0;
  }
}

// ---- live telemetry: drive the ring from real hardware ----------------------
// Batch INA219 V/I per controller + the live firing filament from ShvGetStatus.
// There is no batch power-state read, so each filament's state is INFERRED from
// its measured heating current (relative to its configured idle/active target).
let liveTelemetryTimer = null;
function startLiveTelemetry() {
  if (liveTelemetryTimer) return;
  liveTelemetryTimer = setInterval(pollTelemetry, 1000);
  pollTelemetry();
}
function stopLiveTelemetry() { if (liveTelemetryTimer) { clearInterval(liveTelemetryTimer); liveTelemetryTimer = null; } }

function inferState(f, mA, present) {
  if (!present) return STATE.STOP;
  const idle = f.idleA * 1000, act = f.activeA * 1000;
  if (mA >= (idle + act) / 2) return STATE.ACTIVE;
  if (mA >= idle * 0.4) return STATE.IDLE;
  return STATE.STOP;
}

async function pollTelemetry() {
  if (viewMode !== 'live') return;
  let data;
  try { data = await (await fetch('/api/telemetry')).json(); } catch { return; }
  const tele = data.telemetry || [];
  const running = Object.values(data.run || {}).some((s) => s && s.state === 2);
  if (!tele.length && !running) { setStatus('Live — no telemetry (controllers disconnected or bridge slot busy).'); return; }
  const present = tele.filter((t) => t.present).length;
  const rows = tele.map((t) => {
    const f = filaments[t.index]; if (!f) return null;
    return { index: t.index, state: inferState(f, t.current_mA, t.present), bus_mV: t.bus_mV, current_mA: t.current_mA };
  }).filter(Boolean);
  // the firmware-reported firing filament(s) are authoritative ACTIVE
  const firing = data.firing || [];
  for (const fi of firing) { const r = rows.find((x) => x.index === fi); if (r) r.state = STATE.ACTIVE; }
  // NOTE: do not assign state.activeFilament — it's a derived getter (no setter),
  // and writing it throws in strict mode (ES module), aborting the poll. The
  // firing rows are already marked ACTIVE above; the live active pointer is
  // driven through geometry by gotoSeq()/pollRunStatus().
  ingestTelemetry(rows);   // calls sync()
  setStatus(present === 0 && !running
    ? `Live — connected, but 0/${tele.length} boards present (no filament daughter-boards detected). Run I2C Self-Test to check the controller chips.`
    : `Live — ${present}/${tele.length} boards present${firing.length ? `, firing ${firing.join(',')}` : ''}.`);
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
  // Active/Idle expose the CC current; Voltage exposes the manual mV.
  $('heatIField').hidden = !(st === STATE.IDLE || st === STATE.ACTIVE);
  $('heatVField').hidden = st !== STATE.VOLTAGE;
}

// Set PowerState — the firmware closed-loop driver. IDLE/ACTIVE carry the
// heating-current (mA) target, VOLTAGE carries a manual mV; others arg=0.
async function dbgSetState(i, st) {
  const f = filaments[i];
  // UI is in A / V; the firmware arg is mA (Idle/Active) or mV (Voltage).
  let arg = 0;
  if (st === STATE.IDLE || st === STATE.ACTIVE) arg = Math.round(Math.max(0, parseFloat($('heatI').value) || 0) * 1000);
  else if (st === STATE.VOLTAGE) arg = Math.round(Math.max(0, parseFloat($('heatV').value) || 0) * 1000);
  f.state = st;
  if (st === STATE.IDLE) f.current_mA = arg || f.idleA * 1000;
  else if (st === STATE.ACTIVE) f.current_mA = arg || f.activeA * 1000;
  else if (st === STATE.VOLTAGE) { f.voltage_mV = arg; f.current_mA = 0; }
  else if (st === STATE.STANDBY) { f.voltage_mV = 800; f.current_mA = 0; }
  else { f.voltage_mV = 0; f.current_mA = 0; }
  highlightState(st); sync();
  const j = await cmd(i, 'CH_SET_POWER_STATE', { state: st, arg });
  const argTxt = st === STATE.VOLTAGE ? mvToV(arg) : (st === STATE.IDLE || st === STATE.ACTIVE) ? maToA(arg) : '';
  heatMsg(j.ok ? `${STATE_NAME[st]}${arg ? ' @ ' + argTxt : ''} set.` : `State: ${j.error}`);
}
// "Set" the CC heating current: re-issue the power state carrying heatI. Keep
// Idle if already Idle, else apply as Active.
async function dbgSetI(i) {
  await dbgSetState(i, filaments[i].state === STATE.IDLE ? STATE.IDLE : STATE.ACTIVE);
}
// "Set" the manual voltage: switch the channel to Voltage state with heatV.
async function dbgSetV(i) { await dbgSetState(i, STATE.VOLTAGE); }
async function dbgSetOcp(i) {
  const ma = Math.round(Math.max(0, parseFloat($('heatOcp').value) || 0) * 1000);   // A → mA
  filaments[i].ocp = ma;
  const j = await cmd(i, 'CH_SET_TPS_OCP_THRESHOLD', { threshold_mA: ma });
  heatMsg(j.ok ? `OCP set to ${maToA(ma)}.` : `OCP: ${j.error}`);
}
// INA219 is polled continuously while the popup is open (quiet = no status line).
async function dbgReadIna(i, quiet) {
  const j = await cmd(i, 'CH_GET_INA219', {});
  const d = j.ok && j.response && j.response.decoded;
  if (d && d.present) {
    filaments[i].current_mA = d.current_mA; filaments[i].voltage_mV = d.bus_mV; sync();
    $('heatImeas').textContent = maToA(d.current_mA);
    $('heatVmeas').textContent = mvToV(d.bus_mV);
    if (!quiet) heatMsg(`INA219: ${mvToV(d.bus_mV)} / ${maToA(d.current_mA)}.`);
  } else if (d && !d.present) {
    $('heatImeas').textContent = 'absent'; $('heatVmeas').textContent = '—';
    if (!quiet) heatMsg('INA219 not present on this board (no daughter-board?).');
  } else if (!quiet) heatMsg(`Read: ${j.error || 'no data'}`);
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
async function dbgReadHv(i, quiet) {
  const hw = filamentToHw(i);
  const j = await cmd(i, 'HV_GET_ALL_BYTES', {});
  const d = j.ok && j.response && j.response.decoded;
  if (d && d.feedback) { setHvButton(i, !!(d.feedback[hw.channel] & (1 << hw.mux))); }
  else { setHvButton(i, null); if (!j.ok && !quiet) heatMsg(`HV: ${j.error}`); }
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
  b.textContent = on == null ? 'DC HV …' : on ? 'DC HV ON — click to turn off' : 'DC HV off — click to turn on';
}

function showHeatPopup(clientX, clientY, fil) {
  heatTarget = fil;
  const f = filaments[fil], hw = filamentToHw(fil);
  $('heatFil').textContent = fil;
  $('heatAddr').textContent = hw.label;
  // inputs in A / V (model stays mV/mA)
  $('heatI').value = +((f.current_mA ? f.current_mA / 1000 : f.activeA)).toFixed(2);
  $('heatV').value = +((f.voltage_mV > 0 ? f.voltage_mV / 1000 : 0.8)).toFixed(2);
  $('heatOcp').value = +(f.ocp / 1000).toFixed(2);
  $('heatPw').value = f.durationUs;
  $('heatImeas').textContent = f.current_mA ? maToA(f.current_mA) : '— A';
  $('heatVmeas').textContent = f.voltage_mV ? mvToV(f.voltage_mV) : '— V';
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
  // INA219 + DC HV auto-poll while the popup is open (like wifi_gui).
  if (heatPollTimer) clearInterval(heatPollTimer);
  const poll = () => { if (heatTarget >= 0) { dbgReadIna(heatTarget, true); dbgReadHv(heatTarget, true); } };
  poll();
  heatPollTimer = setInterval(poll, 1000);
}
let heatPollTimer = null;
function hideHeatPopup() {
  $('heatPopup').hidden = true; heatTarget = -1;
  if (heatPollTimer) { clearInterval(heatPollTimer); heatPollTimer = null; }
}

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
let WIN_HALF_DEG = HALF * STEP; // half-span of the collimator window (deg)
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
  // Right-click a filament: Plan = disable/enable (dead), Debug = power editor.
  cv.addEventListener('contextmenu', (e) => {
    if (viewMode === 'live') return;
    const p = at(e);
    if (p.r < CT.R_SOURCE - 8 || p.r > geo._bands().outer + 4) return; // on/near the ring
    const rect = cv.getBoundingClientRect();
    const i = geo.hitTest(e.clientX - rect.left, e.clientY - rect.top);
    const fil = i >= 0 ? i : filFromAngle(p.ang);
    e.preventDefault();
    if (viewMode === 'debug') showHeatPopup(e.clientX, e.clientY, fil);
    else toggleDead(fil); // plan mode
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

// Collimator coverage = how many filaments the collimator covers (geometry).
// Kept odd so the window centers on a filament. Re-derives scan + geometry.
function setCoverage(n) {
  HALF = Math.round((Math.max(1, Math.min(N - 1, n | 0)) - 1) / 2); // nearest odd
  CT.COVERAGE = 2 * HALF + 1;
  WIN_HALF_DEG = HALF * STEP;
  $('covInput').value = CT.COVERAGE;
  $('windowSlider').max = CT.COVERAGE - 1;
  rebuildSchedule();
}

// Plan mode: disable/enable a filament. Dead filaments are omitted from the
// emission schedule (and so from the heating plan) — a known projection gap.
async function detectPresent() {
  setStatus('Enabling isolation power & scanning board presence (boards → Sleep)…');
  try {
    const j = await (await fetch('/api/present-filaments')).json();
    const present = new Set(j.present || []);
    if (!present.size) { setStatus('No present boards detected — check the controller connection / I²C (nothing changed).'); return; }
    // Full sync: the scan is authoritative — present → live, absent → dead. This
    // links the detected hardware to the plan (✕ marks) and the schedule.
    let alive = 0, dead = 0;
    for (let i = 0; i < N; i++) {
      const shouldDie = !present.has(i);
      if (filaments[i].dead !== shouldDie) {
        filaments[i].dead = shouldDie;
        if (shouldDie) { filaments[i].state = STATE.STOP; filaments[i].mAs = 0; }
      }
      filaments[i].dead ? dead++ : alive++;
    }
    rebuildSchedule();   // → gotoSeq → sync redraws the plan (✕); schedule excludes dead
    setStatus(`Detected ${present.size} present board(s) → ${alive} live / ${dead} dead. Plan + schedule updated. (Manually disable any present-but-bad board.)`);
  } catch (e) { setStatus('Detect present failed: ' + e); }
}

function toggleDead(i) {
  filaments[i].dead = !filaments[i].dead;
  if (filaments[i].dead) { filaments[i].state = STATE.STOP; filaments[i].mAs = 0; }
  rebuildSchedule();
  setStatus(`Filament ${i} ${filaments[i].dead ? 'disabled (dead) — omitted from the schedule' : 'enabled'}.`);
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
  const data = filaments.map((f) => ({ pulses: f.pulses, durationUs: f.durationUs, idleA: f.idleA, activeA: f.activeA, dead: f.dead }));
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

// ---- real-hardware bound schedule: download / arm / trigger / monitor -------
const hwMsg = (m) => { const e = $('hwRunStatus'); if (e) e.innerHTML = m; };
const postJSON = async (path, body) => (await fetch(path, {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
})).json();

// ---- guided run-sequence gating --------------------------------------------
// Each step unlocks the next; later actions stay disabled until prerequisites
// pass. Hard gates use only state app.js fully owns — schedule (scanReady),
// operator ack (hwChecked), and runState (download/verify/prep/arm) — so they
// can't be wrong. Connection is a live indicator chip fed by controllers.js.
const runState = { hwChecked: false, downloaded: false, verified: false, prepActive: false, armed: false, override: false };
const scanReady = () => schedule.length > 0;
const settleOk = () => !!(heatingPlan && heatingPlan.validation && heatingPlan.validation.ok);
const hwConnected = () => { const c = window.ctConnected || {}; return !!(c[1] || c[2]); };
function setGate(id, ok, text) {
  const e = $(id); if (!e) return;
  e.classList.toggle('ok', !!ok);
  if (text != null && e.childNodes[0]) e.childNodes[0].nodeValue = text;
}
function enableBtn(id, on) { const e = $(id); if (e) e.disabled = !on; }
function lockStep(id, locked) { const e = $(id); if (e) e.classList.toggle('locked', !!locked); }
function refreshRunGate() {
  const conn = hwConnected();
  setGate('hwGateConn', conn, conn ? '① Connected ' : '① Connect ');
  setGate('hwGateHw', runState.hwChecked, runState.hwChecked ? '② Hardware OK ' : '② Hardware ');
  setGate('hwGateSched', scanReady(), scanReady() ? `③ Schedule ${schedule.length}${settleOk() ? '' : ' ⚠'} ` : '③ Schedule ');
  // Verify is the gate for Arm, NOT Download. The schedule may already be in the
  // firmware from a prior session — verify it matches the plan, then arm. Download
  // is only needed when Verify fails (or you changed the plan, which clears
  // `verified` via invalidateRun). Prep is power-states only — independent of both.
  // Override unlocks every step regardless of order (testing / bring-up). It only
  // relaxes the GUI gate — the firmware still enforces its own arm safety.
  const ovr = !!runState.override;
  const canDownload = ovr || scanReady();
  const canVerify = ovr || scanReady();
  const canPrep = ovr || scanReady();
  const canArm = ovr || (runState.verified && runState.hwChecked && scanReady());
  enableBtn('hwDownloadBtn', canDownload);
  enableBtn('hwVerifyBtn', canVerify);
  ['hwPrepStop', 'hwPrepSleep', 'hwPrepStandby', 'hwPrepIdle', 'hwPrepActive'].forEach((id) => enableBtn(id, canPrep));
  enableBtn('hwArmBtn', canArm && !runState.armed);
  // Disarm is an abort/quit — always available when connected, even after a
  // rejected arm or an out-of-band armed state, so there's always a way out.
  enableBtn('hwDisarmBtn', hwConnected());
  enableBtn('hwTrigBtn', ovr || runState.armed);
  lockStep('hwStepDownload', !canDownload);
  lockStep('hwStepPrep', !canPrep);
  lockStep('hwStepArm', !(canArm || runState.armed));
  lockStep('hwStepTrig', !(ovr || runState.armed));
  // baseline arm-state badge (pollRunStatus refines it to Running/Complete/Fault)
  if (!runState.armed) setArmState('idle', 'idle');
  else if ($('hwArmState') && /idle/.test($('hwArmState').className)) setArmState('● armed', 'armed');
}
function setArmState(label, cls) {
  const e = $('hwArmState'); if (!e) return;
  e.textContent = label;
  e.className = 'hw-armstate ' + cls;
}
window.ctRefreshRunGate = refreshRunGate;
// A changed/rebuilt schedule (or lost connection) invalidates a prior download.
function invalidateRun() { runState.downloaded = runState.verified = runState.prepActive = runState.armed = false; refreshRunGate(); }

// Translate the GUI's bound schedule into the host plan (logical filament 0-95;
// the backend maps to global 0-127 + per-controller split).
function buildPlan() {
  const emission = schedule.map((r) => ({
    filament: r.filament, numPulses: r.burstLen, widthUs: Math.round(r.duration),
  }));
  const heating = (heatingPlan ? heatingPlan.deltas : []).map((d) => ({
    filament: d.filament, triggerIndex: d.seq, state: d.state, milliamps: d.arg,
  }));
  const currents = {};
  filaments.forEach((f, i) => {
    if (f.dead) return;
    currents[i] = { idle_mA: Math.round(f.idleA * 1000), active_mA: Math.round(f.activeA * 1000) };
  });
  // Derive the firmware config from the actual schedule instead of hardcoding:
  //  - maxOnMs MUST exceed the widest pulse or arm() rejects WidthTooLarge.
  //  - interPulseMs / totalMs scale with the rotation so a long/slow scan doesn't
  //    trip InterPulseTimeout / TotalTimeout mid-run.
  //  - triggerEdge 0 = rising; match this to the target's Sync I/O 'Ext edge'.
  const maxWidthUs = emission.reduce((m, e) => Math.max(m, e.widthUs), 0);
  const rotationMs = heatingSchedule.rotationMs || 0;
  const pulses = totalTriggers || emission.reduce((s, e) => s + e.numPulses, 0) || 1;
  const pulseMs = rotationMs ? rotationMs / pulses : 0;
  const config = {
    interPulseMs: Math.max(3000, Math.ceil(pulseMs * 4)),
    maxOnMs: Math.max(40, Math.ceil(maxWidthUs / 1000) + 1),
    totalMs: Math.max(60000, Math.ceil(rotationMs * 2)),
    triggerEdge: 0,
  };
  return { emission, heating, currents, config };
}

async function hwDownload() {
  if (!schedule.length) { hwMsg('No schedule to download — build one first.'); return; }
  const v = heatingPlan && heatingPlan.validation;
  if (v && !v.ok && !confirm('Settle time is NOT satisfied. Download anyway?')) { hwMsg('Download cancelled.'); return; }
  hwMsg(`Downloading ${schedule.length} emission + ${heatRows.length} heating…`);
  // /api/download blocks for the whole transfer, so poll a live progress endpoint
  // (separate request, runs concurrently) to show phase + frames done while it runs.
  let progDone = false;   // guards against a late poll overwriting the final result
  let progTimer = setInterval(async () => {
    if (progDone) return;
    try {
      const p = await (await fetch('/api/download-progress')).json();
      if (progDone) return;   // re-check: the download may have finished mid-fetch
      const parts = Object.entries(p.controllers || {})
        .map(([k, s]) => `P${k} ${s.phase} ${s.done}/${s.total}`);
      if (parts.length) hwMsg(`Downloading… ${parts.join(' · ')}`);
    } catch { /* keep the bar alive */ }
  }, 400);
  try {
    const j = await postJSON('/api/download', { plan: buildPlan() });
    progDone = true; clearInterval(progTimer); progTimer = null;
    if (!j.ok && j.error) { hwMsg(`Download failed: ${j.error}`); return; }
    const s = (ms) => ((ms || 0) / 1000).toFixed(1) + 's';
    const parts = (j.results || []).map((r) => {
      const t = r.timing || {};
      const perf = (t.total && r.frames) ? ` · ${Math.round(t.total / r.frames)} ms/frame` : '';
      const breakdown = t.total != null
        ? `<br>&nbsp;&nbsp;<span class="hint">${r.frames} frames in ${s(t.total)}${perf}${r.fails ? ' · ' + r.fails + ' failed' : ''}</span>`
        : '';
      return `P${r.controller + 1}: ${r.ok ? '✓' : '✗'} ${r.emit} emit / ${r.heat} heat${breakdown}`;
    });
    hwMsg(`${j.ok ? '✓ Downloaded' : '✗ Partial'} — ${parts.join('<br>')}`);
    runState.downloaded = !!j.ok; runState.verified = false; runState.armed = false; refreshRunGate();
  } catch (e) { hwMsg('Download failed: ' + e); }
  finally { progDone = true; if (progTimer) clearInterval(progTimer); }
}

// Read the schedule back out of firmware (ShvGetTableInfo 0x74 + ShvHeatGetInfo
// 0x7E per controller) and compare entry counts to the loaded plan — this is the
// answer to "did the download actually land in firmware?".
async function hwVerify() {
  if (!schedule.length) { hwMsg('No plan loaded to verify against — build & download one first.'); return; }
  hwMsg('Verifying firmware tables…');
  try {
    const j = await postJSON('/api/verify-schedule', { plan: buildPlan() });
    if (!j.ok && j.error) { hwMsg(`Verify failed: ${j.error}`); return; }
    const parts = Object.entries(j.results || {}).map(([k, r]) =>
      r.error ? `P${k}: ${r.error}`
        : `P${k}: ${r.match ? '✓' : '✗'} emit ${r.emit}/${r.emitExpected} · heat ${r.heat}/${r.heatExpected}`
          + (r.crc != null ? ` · crc 0x${(r.crc >>> 0).toString(16).toUpperCase()}` : ''));
    hwMsg(`${j.ok ? '✓ Firmware matches plan' : '✗ Mismatch — re-download'} — ${parts.join(' · ')}`);
    runState.verified = !!j.ok; refreshRunGate();
  } catch (e) { hwMsg('Verify failed: ' + e); }
}

async function hwArm() {
  const repeats = Math.max(1, parseInt($('hwRepeats').value, 10) || 1);
  if (!runState.override && !runState.prepActive && !confirm('Pre-heat (Active first batch) has not been run — the lead filaments may fire cold. Arm anyway?')) return;
  // The firmware arm gate requires every participating filament at PowerState
  // >= Sleep (isolated-12V on); a board left in Stop => IsoOff. For emission-only
  // runs the firmware won't auto-idle (it only does so when a heating stream is
  // loaded), so bring the schedule's filaments to Idle here first. Idempotent —
  // Idle on an already-warm board is a no-op.
  const parts0 = [...new Set(schedule.map((r) => r.filament).filter((f) => f != null && f !== 255))];
  if (parts0.length) {
    hwMsg(`Idling ${parts0.length} participating filament(s) (arm prerequisite)…`);
    try {
      const jp = await postJSON('/api/filament-prep', { state: STATE.IDLE, filaments: parts0 });
      if (!jp.ok) hwMsg(`⚠ Idle prep partial (applied ${jp.applied || 0}/${parts0.length}) — arming anyway…`);
    } catch (e) { hwMsg('Idle prep failed: ' + e + ' — aborting arm.'); return; }
  }
  hwMsg('Arming…');
  try {
    const j = await postJSON('/api/arm', { repeats });
    const parts = Object.entries(j.results || {}).map(([k, r]) => `P${k}: ${r.ok ? 'armed' : (r.error || 'reject ' + (SHV_REJECT[r.reject] || r.reject))}`);
    runState.armed = !!j.ok; refreshRunGate();
    if (j.ok) {
      hwMsg(`✓ Armed (×${repeats}) — ${parts.join(' · ')} — waiting for SyncIn.`);
      setViewMode('live'); startRunMonitor();   // watch the run: plot tracks the firing filament
    } else {
      // it's NOT armed/waiting — surface why, with a fix hint for the common rejects
      const reasons = Object.values(j.results || {}).map((r) => r.reject);
      const hint = reasons.includes(6) ? ' — a participating filament is still in Stop (its isolated-12V is off). Run Prep → Idle first.'
        : reasons.includes(4) ? ' — TPS/HV supply is disabled; enable it.'
        : reasons.includes(3) ? ' — schedule table is empty; download first.'
        : reasons.includes(5) ? ' — TPS fault; clear it then retry.'
        : reasons.includes(7) ? ' — controller not ready.'
        : reasons.includes(8) ? ' — state conflict; Disarm then retry.' : '';
      hwMsg(`✗ Arm rejected — ${parts.join(' · ')}${hint}`);
    }
  } catch (e) { hwMsg('Arm failed: ' + e); }
}

async function hwDisarm() {
  try { await postJSON('/api/disarm', {}); hwMsg('Disarmed — participants → IDLE.'); }
  catch (e) { hwMsg('Disarm failed: ' + e); }
  runState.armed = false; refreshRunGate();
  stopRunMonitor();
}

async function hwTrigger() {
  const count = Math.max(1, parseInt($('hwTrigCount').value, 10) || 1);
  try {
    const j = await postJSON('/api/trigger', { count });
    hwMsg(`Fired ${j.fired}/${count} SyncIn pulse(s)${j.ok ? '.' : ' — ' + ((j.last && j.last.message) || j.error || 'stalled')}`);
  } catch (e) { hwMsg('Trigger failed: ' + e); }
}

// ---- CT-scan prep ladder: batch PowerState across BOTH controllers ----------
// Stop → Sleep → Standby → Idle warms every filament up to the schedule's rest
// state; "Active first batch" then pre-heats the cold-start band so the lead
// filaments are already hot when the first trigger fires.
async function hwPrep(state, label, opts) {
  hwMsg(`⏳ ${label} — applying…`);   // explicit in-progress (present continuous)
  try {
    const j = await postJSON('/api/filament-prep', { state, ...(opts || {}) });
    if (!j.ok && j.error) { hwMsg(`✗ ${label} failed — ${j.error}`); return; }
    const parts = Object.entries(j.results || {}).map(([k, r]) => {
      const total = r.total != null ? r.total : (r.applied || 0) + (r.failed ? r.failed.length : 0);
      const nf = r.failed ? r.failed.length : 0;
      return `P${k}: ${r.applied || 0}/${total}` + (nf ? ` · ${nf} failed (${r.failed.slice(0, 6).join(',')})` : '');
    });
    hwMsg(`${j.ok ? '✓ done' : '⚠ partial'} — ${label}: ${parts.join(' · ')}`);
    if (j.ok && state === STATE.ACTIVE) { runState.prepActive = true; refreshRunGate(); }
  } catch (e) { hwMsg(`✗ ${label} failed — ` + e); }
}
// per-filament CC current (mA) so Idle/Active land on the right setpoint
function prepCurrents(field) {
  const out = {};
  filaments.forEach((f, i) => { if (!f.dead) out[i] = Math.round(f[field] * 1000); });
  return out;
}
// the schedule's cold-start band = filaments the heating plan has ACTIVE at trigger 0
function firstBatchFilaments() {
  if (!heatingPlan) return null;
  const fils = [];
  for (let f = 0; f < N; f++) if (isActiveAt(f, 0)) fils.push(f);
  return fils;
}

// Poll ShvGetStatus: totalPulsesDone → schedule playhead, firmware filament/state.
let runMonitorTimer = null;
const SHV_STATE_NAME = { 0: 'Idle', 1: 'Armed', 2: 'Running', 3: 'Complete', 4: 'Fault' };
const SHV_REJECT = ['None (armed)', 'IndexOutOfWindow', 'WidthTooLarge', 'EmptyTable', 'TpsDisabled', 'TpsFault', 'IsoOff', 'NotReady', 'StateConflict'];
function startRunMonitor() {
  if (runMonitorTimer) return;
  runMonitorTimer = setInterval(pollRunStatus, 500);
  pollRunStatus();
}
function stopRunMonitor() { if (runMonitorTimer) { clearInterval(runMonitorTimer); runMonitorTimer = null; } }

async function pollRunStatus() {
  let data;
  try { data = await (await fetch('/api/run-status')).json(); } catch { return; }
  const ctrls = data.controllers || {};
  let cursor = null, anyRunning = false, fault = null, statePieces = [], firingFil = null;
  let anyArmed = false, anyComplete = false;
  for (const [k, c] of Object.entries(ctrls)) {
    if (!c.connected || !c.status) continue;
    const s = c.status;
    if (cursor == null) cursor = s.totalPulsesDone;
    if (s.state === 1) anyArmed = true;
    if (s.state === 2) anyRunning = true;
    if (s.state === 3) anyComplete = true;
    if (s.state === 4) fault = { ctrl: k, fil: s.faultFilament };
    // firmware-reported live firing filament (255 = none/idle)
    if (s.filamentIndex != null && s.filamentIndex !== 255) firingFil = s.filamentIndex;
    const fil = (s.filamentIndex == null || s.filamentIndex === 255) ? '—' : s.filamentIndex;
    statePieces.push(`P${k}: ${SHV_STATE_NAME[s.state] || s.state} · firing fil ${fil} · pulse ${s.totalPulsesDone}/${s.totalPulsesTarget || '?'}`);
  }
  // ---- link the live firing filament to the CT plot (live view) ----
  // Position the geometry to the firmware-reported firing filament so the ring
  // highlights exactly what's firing; fall back to the pulse cursor's row.
  if (viewMode === 'live' && schedule.length) {
    let idx = -1;
    if (cursor != null) { for (let i = 0; i < schedule.length; i++) { if (schedule[i].trigger <= cursor) idx = i; else break; } }
    if (firingFil != null && (idx < 0 || schedule[idx].filament !== firingFil)) {
      const j = schedule.findIndex((r) => r.filament === firingFil);
      if (j >= 0) idx = j;
    }
    if (idx >= 0) { gotoSeq(idx, false); updateTableActive(); }
  }
  // arm-state badge (firmware-authoritative)
  if (fault) setArmState(`✗ fault (fil ${fault.fil})`, 'fault');
  else if (anyRunning) setArmState('● running', 'running');
  else if (anyArmed) setArmState('● armed', 'armed');
  else if (anyComplete) setArmState('✓ complete', 'complete');
  else setArmState('idle', 'idle');
  const f = fault ? ` — ⚠ FAULT P${fault.ctrl} fil ${fault.fil}` : '';
  hwMsg(`${statePieces.join(' · ') || 'no controller'}${f}`);
  if (!anyRunning && !fault && runMonitorTimer && cursor != null) {
    // schedule complete / idle — keep polling slowly only while armed
  }
}

// ---- I2C self-test / channel mask / diagnosis (ported from wifi_gui) ---------
const i2cMsg = (m) => { const e = $('i2cResult'); if (e) e.innerHTML = m; };

// channel enable mask — 8 toggle bits (default 0x3F = first 6 channels).
// Single source of truth lives on window so the Boards-matrix card mirror
// (power.js) stays in sync with this I²C-section control.
if (window.ctChannelMask == null) window.ctChannelMask = 0x3F;
function buildMaskBits() {
  const el = $('i2cMaskBits'); if (!el) return;
  el.innerHTML = '';
  for (let ch = 0; ch < 8; ch++) {
    const b = document.createElement('button');
    b.className = 'mask-bit' + ((window.ctChannelMask >> ch) & 1 ? ' on' : '');
    b.textContent = ch + 1;
    b.title = `Channel ${ch + 1} ${(window.ctChannelMask >> ch) & 1 ? 'enabled' : 'disabled'}`;
    b.addEventListener('click', () => { window.ctChannelMask ^= (1 << ch); maskChanged(); });
    el.appendChild(b);
  }
}
// Re-render this control AND the Boards-matrix mirror (+ its grid grey-out) so
// a toggle in either place is reflected in both.
function maskChanged() { buildMaskBits(); if (window.ctRenderBoardMask) window.ctRenderBoardMask(); }
window.ctMaskChanged = maskChanged;
function reflectMask(mask) { if (mask != null) { window.ctChannelMask = mask & 0xFF; maskChanged(); } }

// Per-controller diagnostic caches, keyed by controller id (string). Holds the
// raw present masks, the deep-diagnosis map, the TCA9554 register read, and the
// pin-toggle self-test — each action merges into here, then we re-render.
const i2cState = {};
const i2cCtx = (cid) => (i2cState[cid] ||= {});

const STATE_RANK = { op: 3, reg: 2, addr: 1, missing: 0, na: -1 };
const TCA_READ_MAP = {
  enable_io: { readKey: 'enable', testKey: 'enable_toggle', kind: 'out' },
  fault_io:  { readKey: 'fault',  testKey: 'fault_live',   kind: 'in' },
  iso_io:    { readKey: 'iso',    testKey: 'iso_toggle',   kind: 'out' },
  hv_io:     { readKey: 'hv',     testKey: 'hv_live',      kind: 'in' },
};
function chipDotForState(s) {
  switch (s) {
    case 'op':      return { cls: 'chip-cell op',      glyph: '●' };
    case 'reg':     return { cls: 'chip-cell ok',      glyph: '◉' };
    case 'addr':    return { cls: 'chip-cell addr',    glyph: '◐' };
    case 'missing': return { cls: 'chip-cell missing', glyph: '○' };
    default:        return { cls: 'chip-cell na',      glyph: '·' };
  }
}
// mux + 4× TCA9554 are channel-scoped (one chip per channel): collapse the 8
// per-port diagnosis results into the best state seen. Fall back to the present
// mask (addr-level) when no deep scan is cached.
function channelChipState(ctx, channel, chipKey, maskByte) {
  if (ctx.diag) {
    let best = 'missing';
    for (let p = 0; p < 8; p++) {
      const e = ctx.diag.get(`${channel}.${p}`);
      const s = e ? e[`${chipKey}_state`] : 'missing';
      if ((STATE_RANK[s] ?? -1) > (STATE_RANK[best] ?? -1)) best = s;
    }
    return best;
  }
  return (maskByte || 0) !== 0 ? 'addr' : 'missing';
}
function boardChipState(ctx, channel, port, chipKey, maskByte) {
  if (ctx.diag) {
    const e = ctx.diag.get(`${channel}.${port}`);
    return e ? e[`${chipKey}_state`] : 'missing';
  }
  return ((maskByte || 0) & (1 << port)) ? 'addr' : 'missing';
}
// TPS + INA are the only per-board chips → the true board-presence signal.
function boardPresence(ctx, channel, port, tpsByte, inaByte) {
  const tps = boardChipState(ctx, channel, port, 'tps', tpsByte);
  const ina = boardChipState(ctx, channel, port, 'ina', inaByte);
  const at = tps !== 'missing' && tps !== 'na', ai = ina !== 'missing' && ina !== 'na';
  if (!at && !ai) return 'empty';
  return (at && ai) ? 'ok' : 'fault';
}
// 3-row mini-table (status / dir / level) for one TCA9554 across its 8 boards.
// Driven by the full register dump (CH_READ_TCA9554 0x61) when available:
//   status ← per-pin I²C ACK (✓ read OK / ✗ NACK), else diagnosis dot (0x2E)
//   dir    ← config register (1=Input, 0=Output); warn if an output pin reads I
//   level  ← input register (live GPIO) for inputs, output latch for outputs
function tcaBitCell(ctx, channel, chipKey, maskByte) {
  const map = TCA_READ_MAP[chipKey];
  const rch = map && ctx.read && ctx.read[channel] && ctx.read[channel].chips && ctx.read[channel].chips[map.readKey];
  const COL = { ok: '#2c7a6b', fail: '#c0392b', warn: '#b8860b', dim: '#9aa0a6', mute: '#bdbdbd' };
  // Pin-toggle self-test (CH_TCA9554_SELF_TEST 0x60): per-chip polarity round-trip
  // = expander alive on I²C. map.readKey ('enable'/'fault'/'iso'/'hv') indexes the
  // per-channel result. Shown as a "pol ✓/✗" header above the per-pin rows so a
  // dead expander (e.g. a non-responding HV chip) is obvious.
  let polRow = '';
  if (ctx.test && map) {
    const t = ctx.test.get(channel);
    const pass = t ? t[map.readKey] : undefined;
    const g = pass === true ? 'pol ✓' : pass === false ? 'pol ✗' : 'pol ·';
    const pc = pass === true ? COL.ok : pass === false ? COL.fail : COL.mute;
    const tip = pass === true ? 'polarity round-trip OK — expander alive on I²C'
      : pass === false ? 'polarity round-trip FAILED — dead/unresponsive expander chip'
      : 'self-test not run (Pin toggle)';
    polRow = `<div class="tca-pol" style="color:${pc}" title="${tip}">${g}</div>`;
  }
  const sRow = [], dRow = [], lRow = [];
  for (let b = 0; b < 8; b++) {
    const tip = [`CH${channel + 1} board ${b + 1} · ${map ? map.readKey : chipKey}`];
    let dir = '·', dc = COL.mute, lvl = '·', lc = COL.mute, status = '·', sc = COL.mute;
    const readOk = !!rch && (rch.ok || 0) !== 0;
    const readNack = !!rch && (rch.ok || 0) === 0;
    if (readOk) {
      const ok = rch.ok, c = (rch.config >> b) & 1, o = (rch.output >> b) & 1, i = (rch.input >> b) & 1;
      if (ok & 0x01) { dir = c ? 'I' : 'O'; dc = (map.kind === 'out' && c === 1) ? COL.warn : COL.dim; }
      const bitVal = map.kind === 'out' ? ((ok & 0x04) ? o : null) : ((ok & 0x02) ? i : null);
      if (bitVal !== null) { lvl = bitVal ? 'H' : 'L'; lc = bitVal ? COL.ok : COL.dim; }
      status = '✓'; sc = COL.ok;
      tip.push(`dir=${dir} pin=${(ok & 0x02) ? (i ? 'H' : 'L') : '?'} drive=${(ok & 0x04) ? (o ? 'H' : 'L') : '?'}`);
    } else if (readNack) {
      status = '✗'; sc = COL.fail; tip.push('no I²C response (NACK)');
    } else {
      const s = boardChipState(ctx, channel, b, chipKey, maskByte);
      status = chipDotForState(s).glyph;
      sc = (s === 'op' || s === 'reg') ? COL.ok : s === 'addr' ? COL.warn : s === 'missing' ? COL.fail : COL.mute;
      tip.push(`diagnosis: ${s} (Read TCA9554 for live regs)`);
    }
    const t = tip.join(' · ');
    sRow.push(`<td style="color:${sc}" title="${t}">${status}</td>`);
    dRow.push(`<td style="color:${dc}" title="${t}">${dir}</td>`);
    lRow.push(`<td style="color:${lc};font-weight:bold" title="${t}">${lvl}</td>`);
  }
  return polRow + `<table class="tca-bits"><tbody><tr>${sRow.join('')}</tr><tr>${dRow.join('')}</tr><tr>${lRow.join('')}</tr></tbody></table>`;
}
function chipHealthTable(cid, ctx) {
  const pm = ctx.masks || {};
  const M = (k) => pm[k] || [0, 0, 0, 0, 0, 0, 0, 0];
  const mux = M('mux_present_mask'), en = M('enable_io_present_mask'), ft = M('fault_io_present_mask');
  const iso = M('iso_io_present_mask'), hv = M('hv_io_present_mask'), tps = M('tps_present_mask'), ina = M('ina_present_mask');
  const chanMaskOf = { enable_io: en, fault_io: ft, iso_io: iso, hv_io: hv };
  const goodRank = ctx.diag ? STATE_RANK.reg : STATE_RANK.addr;
  let channelsGood = 0, boardsPresent = 0, boardFaults = 0;
  const boardCell = (ch, chipKey, byte) => {
    const parts = [];
    for (let p = 0; p < 8; p++) {
      if (boardPresence(ctx, ch, p, tps[ch], ina[ch]) === 'empty') { parts.push('<span class="chip-cell missing" title="no board">○</span>'); continue; }
      const st = boardChipState(ctx, ch, p, chipKey, byte), { cls, glyph } = chipDotForState(st);
      parts.push(`<span class="${cls}" title="port ${p + 1}: ${chipKey} ${st}">${glyph}</span>`);
    }
    return `<span class="chip-row">${parts.join('')}</span>`;
  };
  let rows = '';
  for (let ch = 0; ch < 8; ch++) {
    const muxState = channelChipState(ctx, ch, 'mux', mux[ch]);
    const muxLive = muxState !== 'missing' && muxState !== 'na';
    let channelOk = (STATE_RANK[muxState] ?? -1) >= goodRank;
    let tca = '';
    for (const k of ['enable_io', 'fault_io', 'iso_io', 'hv_io']) {
      tca += `<td>${tcaBitCell(ctx, ch, k, chanMaskOf[k][ch])}</td>`;
      if ((STATE_RANK[channelChipState(ctx, ch, k, chanMaskOf[k][ch])] ?? -1) < goodRank) channelOk = false;
    }
    if (channelOk) channelsGood++;
    const muxTag = muxLive ? '<span class="summary">✓</span>' : '<span class="bad">✗</span>';
    const tpsCell = muxLive ? boardCell(ch, 'tps', tps[ch]) : '<span class="chip-cell na" title="mux dead">·</span>';
    const inaCell = muxLive ? boardCell(ch, 'ina', ina[ch]) : '<span class="chip-cell na" title="mux dead">·</span>';
    if (muxLive) for (let p = 0; p < 8; p++) { const pr = boardPresence(ctx, ch, p, tps[ch], ina[ch]); if (pr === 'ok') boardsPresent++; else if (pr === 'fault') { boardsPresent++; boardFaults++; } }
    rows += `<tr><td><b>CH${ch + 1}</b></td><td>${muxTag} ${chipDotForState(muxState).glyph}</td>${tca}<td>${tpsCell}</td><td>${inaCell}</td></tr>`;
  }
  const err = ctx.present_error || ctx.error || ctx.selftest_error || ctx.tca_error;
  const maskTag = ctx.channel_mask != null ? `mask 0x${ctx.channel_mask.toString(16).toUpperCase().padStart(2, '0')}` : '';
  const summary = err ? `<span class="bad">${err}</span>`
    : `<span class="summary">channels good ${channelsGood}/8 · boards ${boardsPresent}/64 · faults ${boardFaults}</span>`;
  return `<div class="i2c-ctrl"><b>Power ${cid}</b> <span class="hint">${maskTag}</span> ${summary}`
    + `<div class="table-wrap"><table class="data-table chip-health"><thead><tr>`
    + '<th>Ch</th><th>mux</th><th>enable</th><th>fault</th><th>ISO</th><th>HV</th><th>TPS</th><th>INA</th>'
    + `</tr></thead><tbody>${rows}</tbody></table></div></div>`;
}
function renderI2C() {
  const host = $('i2cHealthHost'); if (!host) return;
  const cids = Object.keys(i2cState);
  host.innerHTML = cids.length ? cids.map((cid) => chipHealthTable(cid, i2cState[cid])).join('') : '';
}
// merge an /api endpoint's {controllers:{cid:{...}}} response into the caches
function mergeI2C(data) {
  const ctrls = (data && data.controllers) || {};
  const keys = Object.keys(ctrls);
  if (!keys.length) { i2cMsg('No controller connected.'); return false; }
  for (const k of keys) {
    const c = ctrls[k], ctx = i2cCtx(k);
    if (c.channel_mask != null) { ctx.channel_mask = c.channel_mask; reflectMask(c.channel_mask); }
    if (c.present_masks) { ctx.masks = c.present_masks; ctx.present_error = null; }
    if (c.present_error) ctx.present_error = c.present_error;
    if (c.error) ctx.error = c.error;
    if (c.diagnosis_bits) ctx.diag = new Map(c.diagnosis_bits.map((e) => [`${e.channel}.${e.mux_port}`, e]));
    if (c.tca9554_channels) { ctx.read = c.tca9554_channels; ctx.tca_error = null; }
    if (c.tca9554_error) ctx.tca_error = c.tca9554_error;
    if (c.selftest_chips) ctx.test = new Map(c.selftest_chips.map((r) => [r.channel, r]));   // channel → {enable,fault,iso,hv} chip-alive pass/fail
    if (c.selftest_method) ctx.test_method = c.selftest_method;   // 'polarity' (0x60) | 'read_ack' (0x61 fallback)
    if (c.selftest_error) ctx.selftest_error = c.selftest_error;
  }
  renderI2C();
  return true;
}

async function i2cPresent() {
  i2cMsg('Scanning I²C bus…');
  try { if (mergeI2C(await postJSON('/api/present', {}))) i2cMsg('I²C scan done. <b>Run diagnosis</b> + <b>Read TCA9554</b> for per-chip detail.'); }
  catch (e) { i2cMsg('Scan failed: ' + e); }
}
async function i2cDiagnose() {
  i2cMsg('Running deep I²C diagnosis… ~300 ms');
  try { if (mergeI2C(await postJSON('/api/diagnosis', {}))) i2cMsg('Diagnosis complete — per-chip states shown.'); }
  catch (e) { i2cMsg('Diagnosis failed: ' + e); }
}
async function i2cTcaRead() {
  i2cMsg('Reading TCA9554 registers (per channel, 0x61)…');
  try { if (mergeI2C(await postJSON('/api/tca9554-read', {}))) i2cMsg('TCA9554 read — status/dir/level rows updated (config/input/output regs).'); }
  catch (e) { i2cMsg('TCA9554 read failed: ' + e); }
}
async function i2cSelftest() {
  i2cMsg('Running TCA9554 self-test…');
  try {
    const j = await postJSON('/api/selftest', {});
    if (mergeI2C(j)) {
      const fellBack = Object.values(j.controllers || {}).some((c) => c && c.selftest_method === 'read_ack');
      const via = fellBack ? ' <span class="hint">(via 0x61 read-ACK — firmware lacks the 0x60 polarity test; reflash the RP2350b for the stronger write+read test)</span>' : '';
      i2cMsg('Self-test complete — per-chip <b>pol ✓/✗</b> in the matrix (✗ = dead/unresponsive expander).' + via);
    }
  } catch (e) { i2cMsg('Self-test failed: ' + e); }
}
async function i2cSetMask() {
  try {
    const j = await postJSON('/api/channel-mask', { mask: window.ctChannelMask });
    const en = [];
    for (let c = 0; c < 8; c++) if ((window.ctChannelMask >> c) & 1) en.push('CH' + (c + 1));
    i2cMsg('Polling ' + (en.join(', ') || 'no channels') + ' (host scan set, 0x'
      + (window.ctChannelMask & 0xFF).toString(16).toUpperCase().padStart(2, '0') + '). Refreshing boards…');
    if (window.ctRefreshBoards) window.ctRefreshBoards();
  } catch (e) { i2cMsg('Set mask failed: ' + e); }
}
async function i2cMuxReset() {
  if (!confirm('Reset all TCA9548A muxes? This pulses the shared reset line and CUTS POWER to every board (outputs drop low). Continue?')) return;
  i2cMsg('Resetting muxes…');
  try {
    const j = await postJSON('/api/mux-reset', {});
    for (const k of Object.keys(i2cState)) { i2cState[k].diag = null; i2cState[k].read = null; i2cState[k].test = null; }  // stale after reset
    const parts = Object.entries(j.controllers || {}).map(([k, r]) => `P${k}: ${r.ok ? '✓ reset' : (r.error || '✗')}`);
    i2cMsg('Mux reset — ' + (parts.join(' · ') || 'no controller') + '. Re-scanning…');
    i2cPresent();
  } catch (e) { i2cMsg('Mux reset failed: ' + e); }
}

function init() {
  geo = new CTGeometry($('ctCanvas'));
  initControllers();
  initPower();
  initMapping();
  initTests();

  // scan schedule
  scheduleTable = initScheduleTable({
    body: $('schBody'), head: $('schHead'),
    onRowClick: (row) => { stopPlay(); gotoSeq(row.seq, false); },
  });
  gantt = new ScheduleGantt($('schGantt'));
  window.ctRedrawGantt = () => { try { gantt._resize(); drawGantt(); } catch (e) {} };
  document.querySelectorAll('#schViewSeg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => setScheduleView(b.dataset.view)));
  $('filSaveBtn').addEventListener('click', saveFilSettings);
  $('filLoadBtn').addEventListener('click', () => loadFilSettings(false));
  $('schDetectPresent').addEventListener('click', detectPresent);
  // schPulses / schDuration are committed to every filament by the Apply button
  // below (no live `input` handler — the old one referenced an undefined
  // `scheduleDefaults` and threw on every keystroke).
  $('schSyncBtn').addEventListener('click', () => rebuildSchedule());
  $('schUploadBtn').addEventListener('click', uploadSchedule);
  buildMaskBits();
  $('i2cPresentBtn').addEventListener('click', i2cPresent);
  $('i2cDiagnoseBtn').addEventListener('click', i2cDiagnose);
  $('i2cTcaReadBtn').addEventListener('click', i2cTcaRead);
  $('i2cSelftestBtn').addEventListener('click', i2cSelftest);
  $('i2cMuxResetBtn').addEventListener('click', i2cMuxReset);
  $('i2cMaskGetBtn').addEventListener('click', i2cPresent);
  $('i2cMaskSetBtn').addEventListener('click', i2cSetMask);
  // Let the Boards-matrix card (power.js) reuse the same get (present scan,
  // which reflects channel_mask back) and set handlers.
  window.ctI2cGetMask = i2cPresent;
  window.ctI2cSetMask = i2cSetMask;
  $('hwDownloadBtn').addEventListener('click', hwDownload);
  $('hwVerifyBtn').addEventListener('click', hwVerify);
  $('hwArmBtn').addEventListener('click', hwArm);
  $('hwDisarmBtn').addEventListener('click', hwDisarm);
  $('hwTrigBtn').addEventListener('click', hwTrigger);
  $('hwCheckBtn').addEventListener('click', () => { runState.hwChecked = !runState.hwChecked; refreshRunGate(); });
  $('hwOverride').addEventListener('change', (e) => {
    runState.override = e.target.checked;
    const note = $('hwOverrideNote'); if (note) note.hidden = !runState.override;
    $('hwOverrideLbl').classList.toggle('active', runState.override);
    refreshRunGate();
  });
  refreshRunGate();   // initial gate/lock state
  $('hwPrepStop').addEventListener('click', () => hwPrep(STATE.STOP, 'Stop all'));
  $('hwPrepSleep').addEventListener('click', () => hwPrep(STATE.SLEEP, 'Sleep all'));
  $('hwPrepStandby').addEventListener('click', () => hwPrep(STATE.STANDBY, 'Standby all'));
  $('hwPrepIdle').addEventListener('click', () => hwPrep(STATE.IDLE, 'Idle all', { currents: prepCurrents('idleA') }));
  $('hwPrepActive').addEventListener('click', () => {
    const fils = firstBatchFilaments();
    if (!fils || !fils.length) { hwMsg('No heating plan — build a schedule first.'); return; }
    hwPrep(STATE.ACTIVE, `Active first batch (${fils.length})`, { filaments: fils, currents: prepCurrents('activeA') });
  });
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
  $('hsActive').addEventListener('input', (e) => {
    heatingSchedule.activeCount = Math.max(1, parseInt(e.target.value, 10) || 40);
    planHeating(); applyScheduleView(); updateScheduleHeader(); sync();
  });
  const onPowerW = (id, key) => $(id).addEventListener('input', (e) => {
    powerEst[key] = Math.max(0, parseFloat(e.target.value) || 0); sync();
  });
  onPowerW('pwActiveW', 'activeW');
  onPowerW('pwIdleW', 'idleW');

  // view mode (live / plan / debug)
  document.querySelectorAll('#viewModeSeg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => setViewMode(b.dataset.view)));

  // debug popup — power-state ladder + per-field commands
  document.querySelectorAll('#heatStateSeg button').forEach((b) =>
    b.addEventListener('click', () => { if (heatTarget >= 0) dbgSetState(heatTarget, +b.dataset.state); }));
  $('heatSetI').addEventListener('click', () => { if (heatTarget >= 0) dbgSetI(heatTarget); });
  $('heatSetV').addEventListener('click', () => { if (heatTarget >= 0) dbgSetV(heatTarget); });
  $('heatSetOcp').addEventListener('click', () => { if (heatTarget >= 0) dbgSetOcp(heatTarget); });
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
  $('covInput').addEventListener('change', (e) => setCoverage(parseInt(e.target.value, 10) || 35));

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
  if (!loadFilSettings(true)) rebuildSchedule(); // restore host settings, else fresh build
  setViewMode('live');  // start on real hardware — no simulated data until Plan is chosen
}

document.addEventListener('DOMContentLoaded', init);
