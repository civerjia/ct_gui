/*
 * Direct power control — boards matrix (and, later, HV grid + ShV schedule).
 *
 * All commands target ONE selected controller (the "target" selector under the
 * divider). Board/HV/TPS/INA commands are built server-side by the WiFi GUI's
 * build_command_payload and proxied via POST /api/power-cmd {controller, command, …}.
 */

import { calApi } from './tests.js';

const $p = (id) => document.getElementById(id);
// Always resolve to an object with `ok` — a network/parse failure becomes
// {ok:false,error} instead of a thrown rejection, so callers that show a
// "… in progress" line always get to replace it with the result or the error.
const postJ = async (path, body) => {
  try {
    return await (await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
    })).json();
  } catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
};

let pwTarget = 1;                       // selected controller (1 or 2) — boards/HV-grid
// The STM32 (HV monitor/setpoints + ADC waveform/pulses) lives only on the MASTER
// controller; those calls route there regardless of the selected target.
const masterId = () => window.ctMaster || 1;
const masterConnected = () => !!connectedSet[masterId()];
const POLL_MS = 1000;                    // board snapshot + HV grid refresh period
const REFRESH_S = (POLL_MS / 1000).toFixed(0) + ' s';
const powerCmd = (command, extra) => postJ('/api/power-cmd', { controller: pwTarget, command, ...extra });

// ---- boards matrix ----------------------------------------------------------
// 64 placeholder boards so the 8×8 grid always renders, even disconnected.
function emptyBoards() {
  const out = [];
  for (let ch = 0; ch < 8; ch++) for (let mux = 0; mux < 8; mux++) {
    out.push({
      channel: ch, mux_port: mux, label: `CH${ch + 1}.${mux + 1}`,
      present: false, tps_present: false, ina_present: false,
      iso_enabled: false, tps_enabled: false, tps_fault: false, hv_overcurrent: false,
      bus_mV: 0, current_mA: 0,
    });
  }
  return out;
}
let boardCache = emptyBoards();         // 64 board objects from /api/board-snapshot
let boardPrimary = { channel: 0, mux_port: 0 };

// TPS register read-back: OCP threshold (reg 0x02) + VOUT_SR (reg 0x03).
const OCP_DELAY_LABELS = ['128 µs', '3.07 ms', '6.14 ms', '12.3 ms'];
const SLEW_LABELS = ['1.25', '2.5', '5', '10'];
const OCP_MA_PER_CODE = 0.5 / 0.015;     // 0.5 mV LSB / 0.015 Ω sense ≈ 33.3 mA/code
const V_MAX_MV = 15000;                   // kCurrentLoopMaxMv
const OCP_MAX_MA = Math.round(0x7F * OCP_MA_PER_CODE);  // ~4233 mA (IOUT_LIMIT ceiling)
const I_MAX_MA = OCP_MAX_MA;              // current capability = hardware ceiling 4233 mA

// power-state ladder (CH_SET_POWER_STATE arg = mA for Idle/Active, mV for Voltage)
const STATE_STANDBY = 3, STATE_IDLE = 4, STATE_ACTIVE = 5, STATE_VOLTAGE = 6;
let bmState = STATE_IDLE;
function reflectStateArg() {
  document.querySelectorAll('#bmStateSeg button').forEach((b) => b.classList.toggle('active', +b.dataset.state === bmState));
  const row = document.getElementById('bmStateArgRow'), unit = document.getElementById('bmArgUnit');
  if (!row) return;
  // hides ONLY the value field (an inline label); the Apply button stays visible
  // so Stop/Sleep/Standby (no setpoint) can still be applied.
  const arg = document.getElementById('bmStateArg');
  // current is clamped to the hardware capability (4233 mA). OCP is a separate,
  // overwritable setting — NOT a limit on the current field.
  if (bmState === STATE_VOLTAGE) { row.style.display = ''; unit.textContent = 'Voltage (mV)'; if (arg) arg.max = V_MAX_MV; }
  else if (bmState === STATE_IDLE || bmState === STATE_ACTIVE) { row.style.display = ''; unit.textContent = 'Current (mA)'; if (arg) arg.max = I_MAX_MA; }
  else { row.style.display = 'none'; }
  // impedance sweep is only meaningful in Voltage mode
  const z = document.getElementById('bmZSection');
  if (z) z.style.display = bmState === STATE_VOLTAGE ? '' : 'none';
}
// Batch power-state selector (applies to every selected board), independent of
// the single-board one above.
let bmBatchState = STATE_IDLE;
function reflectBatchStateArg() {
  document.querySelectorAll('#bmBatchStateSeg button').forEach((b) => b.classList.toggle('active', +b.dataset.bstate === bmBatchState));
  const row = document.getElementById('bmBatchStateArgRow'), unit = document.getElementById('bmBatchArgUnit'), arg = document.getElementById('bmBatchStateArg');
  if (!row) return;
  if (bmBatchState === STATE_VOLTAGE) { row.style.display = ''; unit.textContent = 'Voltage (mV)'; if (arg) arg.max = V_MAX_MV; }
  else if (bmBatchState === STATE_IDLE || bmBatchState === STATE_ACTIVE) { row.style.display = ''; unit.textContent = 'Current (mA)'; if (arg) arg.max = I_MAX_MA; }
  else { row.style.display = 'none'; }
}
const boardSel = new Set(['0.0']);      // multi-select "ch.mux" keys
const bKey = (b) => `${b.channel}.${b.mux_port}`;
const keyTo = (k) => { const [c, m] = k.split('.').map(Number); return { channel: c, mux_port: m }; };

function boardMask() {
  const mask = [0, 0, 0, 0, 0, 0, 0, 0];
  for (const k of boardSel) { const { channel, mux_port } = keyTo(k); mask[channel] |= (1 << mux_port); }
  return mask;
}

// Channel-enable mask mirror. Single source of truth = window.ctChannelMask,
// shared with the I²C-section control in app.js. Masked-off channels are
// dimmed + non-selectable in the grid and skipped by the select-all helpers.
if (window.ctChannelMask == null) window.ctChannelMask = 0x3F;
const chEnabled = (ch) => !!((window.ctChannelMask >> ch) & 1);

// Drop any selected boards (and move the single-board anchor off) channels the
// mask just disabled, so batch ops never reach a masked-off channel.
function pruneMaskedSelection() {
  for (const k of [...boardSel]) if (!chEnabled(keyTo(k).channel)) boardSel.delete(k);
  if (!chEnabled(boardPrimary.channel)) {
    let ch = 0; while (ch < 8 && !chEnabled(ch)) ch++;
    boardPrimary = { channel: ch < 8 ? ch : 0, mux_port: 0 };
  }
}

// 8-bit channel-enable toggles. Rendered into both the Boards card (#bmMaskBits)
// and the HV grid (#hvMaskBits) — both mirror the shared window.ctChannelMask.
function renderMaskBits(containerId) {
  const el = $p(containerId); if (!el) return;
  el.innerHTML = '';
  for (let ch = 0; ch < 8; ch++) {
    const b = document.createElement('button');
    b.className = 'mask-bit' + (chEnabled(ch) ? ' on' : '');
    b.textContent = ch + 1;
    b.title = `Channel ${ch + 1} ${chEnabled(ch) ? 'enabled' : 'disabled'}`;
    b.addEventListener('click', () => {
      window.ctChannelMask ^= (1 << ch);
      // ctMaskChanged (app.js) re-renders every mirror and the I²C control;
      // fall back to a local refresh if app.js hasn't wired it yet.
      if (window.ctMaskChanged) window.ctMaskChanged();
      else { renderMaskBits(containerId); }
    });
    el.appendChild(b);
  }
}
function renderBoardMaskBits() { renderMaskBits('bmMaskBits'); }

// app.js calls this after any mask change (from any control) to keep the Boards
// card + HV grid mirrors and their grid grey-outs in sync.
window.ctRenderBoardMask = () => {
  pruneMaskedSelection(); renderBoardMaskBits(); renderBoardGrid();
  pruneHvMaskedSelection(); renderMaskBits('hvMaskBits'); renderHvGrid();
};

const BOARDS_HTML = `
  <div class="legend bm-legend">
    <span class="legend-item"><span class="dot on">P</span> present</span>
    <span class="legend-item"><span class="dot on">I</span> ISO</span>
    <span class="legend-item"><span class="dot on">T</span> TPS</span>
    <span class="legend-item"><span class="dot fault">F</span> fault</span>
    <span class="legend-item"><span class="hv-badge on">HV</span> HV current</span>
    <span class="legend-item"><span class="dot absent">·</span> absent</span>
    <span class="hint">Shift-click = block · Ctrl/Cmd-click = toggle</span>
  </div>
  <div class="row compact i2c-mask bm-mask" title="Host poll set — only enabled channels are read for INA219 V/I (and selectable). Set applies it host-side; enable CH7 to start polling it. The firmware always scans all 8 regardless. Shared with the I²C section below.">
    <span class="hint">channels enabled</span>
    <span id="bmMaskBits" class="i2c-mask-bits"></span>
    <button id="bmMaskGetBtn" class="xs mask-btn">Get</button>
    <button id="bmMaskSetBtn" class="xs mask-btn">Set</button>
  </div>
  <div class="row compact selection-toolbar">
    <span id="bmSelSummary" class="summary">1 selected</span>
    <button id="bmSelPresent" class="xs">All present</button>
    <button id="bmSelAll" class="xs" title="Select every board on enabled channels">All enabled</button>
    <button id="bmSelClear" class="xs">Clear</button>
  </div>
  <div id="bmGrid" class="board-grid"></div>

  <div class="batch-box">
    <div class="block-title">Batch (selected boards)</div>
    <div class="bm-btn-row">
      <button id="bmIsoOn" class="xs">ISO On</button>
      <button id="bmIsoOff" class="xs">ISO Off</button>
      <button id="bmTpsOn" class="xs">TPS On</button>
      <button id="bmTpsOff" class="xs">TPS Off</button>
      <button id="bmReadIna" class="xs">Read INA</button>
    </div>
    <div class="bm-set-row">
      <label class="numlabel">TPS mV <input id="bmTpsMv" type="number" min="0" max="15000" value="800" /></label>
      <button id="bmSetV" class="xs">Set V</button>
    </div>
    <div class="bm-set-row">
      <label class="numlabel">OCP mA <input id="bmOcp" type="number" min="0" max="4233" value="3200" /></label>
      <button id="bmSetOcp" class="xs">Set OCP</button>
    </div>
    <div class="block-title" style="margin-top:6px">Power state <span class="hint">Idle/Active → mA · Voltage → mV · applies to all selected</span></div>
    <div class="seg6" id="bmBatchStateSeg">
      <button data-bstate="1">Stop</button><button data-bstate="2">Sleep</button><button data-bstate="3">Standby</button>
      <button data-bstate="4">Idle</button><button data-bstate="5">Active</button><button data-bstate="6">Voltage</button>
    </div>
    <div class="bm-set-row">
      <label class="numlabel" id="bmBatchStateArgRow"><span id="bmBatchArgUnit">Current (mA)</span> <input id="bmBatchStateArg" type="number" min="0" value="1000" /></label>
      <button id="bmBatchApplyState" class="xs quick">Apply state (sel)</button>
    </div>
  </div>

  <div class="batch-box">
    <div class="block-title">Single board <span id="bmOneLabel" class="hint">CH1.1</span></div>
    <div class="bm-badges">
      <span>Present <span id="bmbPresent" class="b-badge">·</span></span>
      <span>TPS <span id="bmbTps" class="b-badge">·</span></span>
      <span>ISO <span id="bmbIso" class="b-badge">·</span></span>
      <span>Fault <span id="bmbFault" class="b-badge">·</span></span>
    </div>
    <div class="detail-top">
      <div class="metric"><span class="metric-label">Bus V</span><span id="bmOneV" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Current</span><span id="bmOneI" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Power</span><span id="bmOneP" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Load R</span><span id="bmOneR" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Temp · R/R0</span><span id="bmOneT" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">OCP</span><span id="bmOcpRead" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">OCP start</span><span id="bmStartOcpMetric" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">OCP delay</span><span id="bmDelayRead" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Slew</span><span id="bmSlewRead" class="metric-value">—</span></div>
    </div>
    <div class="bm-set-row" title="Filament resistance at room temperature (Ω). Temp is estimated from R(T)/R0 = −0.524 + 4.66e-3·T + 2.84e-7·T².">
      <label class="numlabel">R0 room Ω <input id="bmZR0" type="number" min="0.001" step="0.01" value="0.257" /></label>
      <span class="hint">used for the Temp estimate (K)</span>
    </div>
    <div class="bm-btn-row">
      <button id="bmOneIsoOn" class="xs">ISO On</button>
      <button id="bmOneIsoOff" class="xs">ISO Off</button>
      <button id="bmOneTpsOn" class="xs">TPS On</button>
      <button id="bmOneTpsOff" class="xs">TPS Off</button>
      <button id="bmOneReadIna" class="xs">Read INA</button>
    </div>
    <div class="block-title">Power state <span class="hint">Idle/Active → mA · Voltage → mV</span></div>
    <div class="seg6" id="bmStateSeg">
      <button data-state="1">Stop</button><button data-state="2">Sleep</button><button data-state="3">Standby</button>
      <button data-state="4">Idle</button><button data-state="5">Active</button><button data-state="6">Voltage</button>
    </div>
    <div class="bm-set-row">
      <label class="numlabel" id="bmStateArgRow"><span id="bmArgUnit">Current (mA)</span> <input id="bmStateArg" type="number" min="0" value="1000" /></label>
      <button id="bmApplyState" class="xs quick">Apply state</button>
    </div>
    <div class="bm-set-row">
      <label class="numlabel">OCP mA <input id="bmOneOcp" type="number" min="0" max="4233" value="1500" /></label>
      <button id="bmOneSetOcp" class="xs">Set OCP</button>
    </div>
    <div class="bm-set-row">
      <label class="numlabel">OCP delay
        <select id="bmOneOcpDelay"><option value="0">128 µs</option><option value="1">3.07 ms</option><option value="2">6.14 ms</option><option value="3">12.3 ms</option></select></label>
      <label class="numlabel">slew mV/µs
        <select id="bmOneSlew"><option value="0">1.25</option><option value="1" selected>2.5</option><option value="2">5</option><option value="3">10</option></select></label>
      <button id="bmOneVoutSr" class="xs">Apply VOUT_SR</button>
    </div>
    <div class="bm-set-row" title="ChStartupOcp 0x37 — OCP current floor applied at IDLE/ACTIVE turn-on. Global across the controller's channels.">
      <label class="numlabel">Startup OCP mA <input id="bmStartOcp" type="number" min="0" max="4233" value="4000" /></label>
      <button id="bmStartOcpSet" class="xs">Set</button>
      <button id="bmStartOcpGet" class="xs">Get</button>
      <span id="bmStartOcpRead" class="hint">—</span>
    </div>

    <div id="bmZSection">
      <div class="block-title">Impedance sweep</div>
      <div class="bm-set-row" title="Voltage-mode sweep from 0.8 V to End V in Step increments. Dwells at each step for thermal equilibrium, reads INA219, then fits + plots.">
        <label class="numlabel">end V <input id="bmZEndV" type="number" min="0.9" max="15" step="0.1" value="1.5" /></label>
        <label class="numlabel">step V <input id="bmZStepV" type="number" min="0.01" max="1" step="0.05" value="0.1" /></label>
        <label class="numlabel">dwell s <input id="bmZDwell" type="number" min="0.1" max="20" step="0.1" value="3" /></label>
        <button id="bmZMeasure" class="xs quick">Sweep</button>
      </div>
      <canvas id="bmZCanvas" class="adc-plot" width="320" height="170"></canvas>
      <div id="bmZFormula" class="z-formula"></div>
      <div id="bmZResult" class="summary"></div>
    </div>
  </div>
  <div id="bmStatus" class="summary"></div>`;

const bmMsg = (m) => { const e = $p('bmStatus'); if (e) e.textContent = m; };

function renderBoardGrid() {
  const grid = $p('bmGrid'); if (!grid) return;
  grid.innerHTML = '';
  let present = 0, tps = 0, iso = 0, fault = 0;
  for (const b of boardCache) {
    if (b.present) present++; if (b.tps_enabled) tps++; if (b.iso_enabled) iso++; if (b.tps_fault) fault++;
    const tile = document.createElement('div');
    const cls = b.tps_fault ? 'fault' : b.present ? 'present' : 'absent';
    const k = bKey(b);
    tile.className = 'status-tile ' + cls + (boardSel.has(k) ? ' selected' : '')
      + (chEnabled(b.channel) ? '' : ' masked')
      + (b.channel === boardPrimary.channel && b.mux_port === boardPrimary.mux_port ? ' active' : '');
    const dot = (on, ch, fa) => `<span class="dot ${fa ? 'fault' : on ? 'on' : 'off'}">${ch}</span>`;
    const hv = `<span class="hv-badge ${b.hv_overcurrent ? 'on' : 'off'}" title="HV current ${b.hv_overcurrent ? 'sensed (>1 mA)' : 'none'}">HV</span>`;
    tile.innerHTML = hv
      + `<span class="tile-title">${b.label}</span>`
      + `<span class="tile-dots">${dot(b.present, 'P')}${dot(b.iso_enabled, 'I')}${dot(b.tps_enabled, 'T', b.tps_fault)}${dot(b.tps_fault, 'F', b.tps_fault)}</span>`
      + `<span class="tile-measure">${b.present
          ? `<span>${(b.bus_mV / 1000).toFixed(2)} V</span><span>${b.current_mA} mA</span>`
          : '<span>—</span>'}</span>`;
    tile.addEventListener('click', (e) => {
      if (!chEnabled(b.channel)) return;                 // masked-off channel: non-selectable
      if (e.shiftKey) {
        // Rectangular block from the anchor (boardPrimary) to this cell —
        // selects every enabled board in channels [r0..r1] × ports [c0..c1].
        const r0 = Math.min(boardPrimary.channel, b.channel), r1 = Math.max(boardPrimary.channel, b.channel);
        const c0 = Math.min(boardPrimary.mux_port, b.mux_port), c1 = Math.max(boardPrimary.mux_port, b.mux_port);
        boardSel.clear();
        for (let c = r0; c <= r1; c++) { if (!chEnabled(c)) continue; for (let m = c0; m <= c1; m++) boardSel.add(`${c}.${m}`); }
        renderBoardGrid();                               // anchor stays put so the block can be re-dragged
      } else if (e.ctrlKey || e.metaKey) {
        if (boardSel.has(k)) boardSel.delete(k); else boardSel.add(k);
        boardPrimary = { channel: b.channel, mux_port: b.mux_port };   // move anchor for the next shift-block
        renderBoardGrid();
      } else {
        boardSel.clear(); boardSel.add(k); boardPrimary = { channel: b.channel, mux_port: b.mux_port };
        renderBoardGrid(); renderOneBoard(); readTpsRegs(); readStartupOcp();   // read OCP/delay/slew + startup OCP
      }
    });
    grid.appendChild(tile);
  }
  $p('bmSelSummary').textContent = `${boardSel.size} selected · ${present}P ${tps}T ${iso}I ${fault}F`;
}

function renderOneBoard() {
  const b = boardCache.find((x) => x.channel === boardPrimary.channel && x.mux_port === boardPrimary.mux_port);
  $p('bmOneLabel').textContent = b ? b.label : `CH${boardPrimary.channel + 1}.${boardPrimary.mux_port + 1}`;
  const badge = (id, on, fault) => {
    const e = $p(id); if (!e) return;
    e.className = 'b-badge ' + (fault ? 'fault' : on ? 'on' : 'off');
    e.textContent = fault ? '!' : on ? '✓' : '·';
  };
  badge('bmbPresent', b && b.present);
  badge('bmbTps', b && b.tps_enabled);
  badge('bmbIso', b && b.iso_enabled);
  badge('bmbFault', b && b.tps_fault, b && b.tps_fault);
  const v = (b && b.present) ? b.bus_mV : 0, i = (b && b.present) ? b.current_mA : 0;
  $p('bmOneV').textContent = (b && b.present) ? (v / 1000).toFixed(3) + ' V' : '—';
  $p('bmOneI').textContent = (b && b.present) ? i + ' mA' : '—';
  const P = (b && b.present && i) ? (v * i / 1e6) : null;                                       // P = V·I (W)
  $p('bmOneP').textContent = P != null ? P.toFixed(2) + ' W' : '—';
  // Below 0.1 W the V/I resistance (and the temperature derived from it) is
  // dominated by sense noise/offset, so report Load R and Temp as invalid.
  const R = (P != null && P >= 0.1) ? (v / i) : null;                                          // R = V/I (Ω)
  $p('bmOneR').textContent = R != null ? R.toFixed(2) + ' Ω' : '—';
  const R0room = +$p('bmZR0').value;
  const T = R != null ? estimateTempK(R, R0room) : null;
  const ratio = (R != null && R0room > 0) ? R / R0room : null;
  $p('bmOneT').textContent = T != null ? `${Math.round(T)} K · ${ratio.toFixed(1)}` : '—';
}

// Tungsten filament temperature from R(T)/R0 = -0.524 + 4.66e-3·T + 2.84e-7·T²
// (T in K). Solve the quadratic for the positive root; R0 = room-temp resistance.
function estimateTempK(R, R0room) {
  if (!(R > 0) || !(R0room > 0)) return null;
  const ratio = R / R0room;
  const A = 2.84e-7, B = 4.66e-3, C = -0.524 - ratio;
  const disc = B * B - 4 * A * C;
  if (disc < 0) return null;
  const T = (-B + Math.sqrt(disc)) / (2 * A);
  return T > 0 ? T : null;
}

// Read the primary board's OCP threshold (reg 0x02) + VOUT_SR (reg 0x03) and
// display the OCP current, OCP delay, and slew rate the chip is actually using.
// Polled in the 2 s loop (silent=true → no "…" flash). On a failed/empty read we
// DON'T fall back to 0 (which would falsely show "off") — keep the last value
// while polling, or show "—" on an explicit (non-silent) read.
let tpsBusy = false;
async function readTpsRegs(silent) {
  if (!boardTargetConnected()) { for (const id of ['bmOcpRead', 'bmDelayRead', 'bmSlewRead']) $p(id).textContent = '—'; return; }
  if (tpsBusy) return;                  // never overlap (selection click vs poll)
  tpsBusy = true;
  try {
    if (!silent) for (const id of ['bmOcpRead', 'bmDelayRead', 'bmSlewRead']) { const e = $p(id); if (e) e.textContent = '…'; }
    const t = single();
    const ocp = await powerCmd('CH_READ_TPS_REGISTER', { ...t, reg: 0x02, width_bytes: 1 });
    const sr = await powerCmd('CH_READ_TPS_REGISTER', { ...t, reg: 0x03, width_bytes: 1 });
    const od = ocp.response && ocp.response.decoded, sd = sr.response && sr.response.decoded;
    if (od && od.value != null) {
      const ov = od.value & 0xFF;
      $p('bmOcpRead').textContent = (ov & 0x80) ? Math.round((ov & 0x7F) * OCP_MA_PER_CODE) + ' mA' : 'off';
    } else if (!silent) { $p('bmOcpRead').textContent = '—'; }
    if (sd && sd.value != null) {
      const sv = sd.value & 0xFF;
      $p('bmDelayRead').textContent = OCP_DELAY_LABELS[(sv >> 4) & 0x03];
      $p('bmSlewRead').textContent = SLEW_LABELS[sv & 0x03] + ' mV/µs';
    } else if (!silent) { $p('bmDelayRead').textContent = '—'; $p('bmSlewRead').textContent = '—'; }
  } finally { tpsBusy = false; }
}

// ---- filament impedance sweep ----------------------------------------------
// Voltage-mode scan 0.8→1.5 V (0.1 V step); dwell at each step for thermal
// equilibrium, read INA219, then least-squares fit V = a·I² + R0 and plot.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let zPoints = [], zFit = null;

// the fit model, typeset as MathML (native, offline LaTeX-quality). Symbolic
// when a/R0 are null, otherwise with the fitted values substituted.
function zMathML(a, R0) {
  const A = (a == null) ? '<mi>a</mi>' : `<mn>${a.toExponential(3)}</mn>`;
  const R = (R0 == null) ? '<msub><mi>R</mi><mn>0</mn></msub>' : `<mn>${R0.toFixed(3)}</mn>`;
  return `<math display="block"><mrow><mi>V</mi><mo>=</mo><mrow><mo>(</mo>`
    + `${A}<mo>&#8290;</mo><msup><mi>I</mi><mn>2</mn></msup><mo>+</mo>${R}<mo>)</mo></mrow>`
    + `<mo>&#8290;</mo><mi>I</mi></mrow></math>`;
}

function drawZ() {
  const c = $p('bmZCanvas'); if (!c) return;
  const ctx = c.getContext('2d'), W = c.width, H = c.height, pad = 26;
  ctx.clearRect(0, 0, W, H); ctx.fillStyle = '#070b0e'; ctx.fillRect(0, 0, W, H);
  if (!zPoints.length) return;
  const iMax = Math.max(...zPoints.map((p) => p.i)) * 1.1 || 1;
  const vMax = Math.max(...zPoints.map((p) => p.v)) * 1.1 || 1;
  const X = (i) => pad + (i / iMax) * (W - pad - 6);
  const Y = (v) => H - pad - (v / vMax) * (H - pad - 6);
  ctx.strokeStyle = '#243038'; ctx.beginPath(); ctx.moveTo(pad, 6); ctx.lineTo(pad, H - pad); ctx.lineTo(W - 6, H - pad); ctx.stroke();
  ctx.fillStyle = '#8b97a0'; ctx.font = '9px monospace'; ctx.fillText('I (A)', W - 34, H - 8); ctx.fillText('V', 6, 14);
  if (zFit) {   // fitted curve V = (a·I² + R0)·I
    ctx.strokeStyle = '#f2c14e'; ctx.lineWidth = 1.2; ctx.beginPath();
    for (let n = 0; n <= 60; n++) { const i = (n / 60) * iMax, v = (zFit.a * i * i + zFit.R0) * i; n ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)); }
    ctx.stroke();
  }
  ctx.fillStyle = '#3fb6a0';
  for (const p of zPoints) { ctx.beginPath(); ctx.arc(X(p.i), Y(p.v), 3, 0, 2 * Math.PI); ctx.fill(); }
}

async function measureImpedance() {
  if (!boardTargetConnected()) { $p('bmZResult').textContent = `Power ${pwTarget} not connected`; return; }
  const ch = boardPrimary.channel, mux = boardPrimary.mux_port;
  const dwell = Math.max(100, Math.round((+$p('bmZDwell').value || 3) * 1000));   // seconds → ms
  const startMv = 800;
  const endMv = Math.min(V_MAX_MV, Math.max(startMv + 100, Math.round((+$p('bmZEndV').value || 1.5) * 1000)));
  const stepMv = Math.min(1000, Math.max(10, Math.round((+$p('bmZStepV').value || 0.1) * 1000)));   // step ≤ 1 V
  const setV = (mv) => postJ('/api/cmd', { controller: pwTarget, command: 'CH_SET_POWER_STATE', channel: ch, mux_port: mux, state: STATE_VOLTAGE, arg: mv });
  zPoints = []; zFit = null;
  $p('bmZMeasure').disabled = true;
  try {
    for (let mv = startMv; mv <= endMv; mv += stepMv) {
      await setV(mv);
      $p('bmZResult').textContent = `sweeping ${mv} mV… (dwell ${(dwell / 1000).toFixed(1)} s)`;
      await sleep(dwell);
      const r = await powerCmd('CH_GET_INA219', { target: 'single', channel: ch, mux_port: mux });
      const d = r.response && r.response.decoded;
      if (d && d.present && d.current_mA > 0) { zPoints.push({ v: d.bus_mV / 1000, i: d.current_mA / 1000 }); drawZ(); }
    }
  } finally {
    // leave the board in Voltage mode at 0.8 V after the sweep
    await postJ('/api/cmd', { controller: pwTarget, command: 'CH_SET_POWER_STATE', channel: ch, mux_port: mux, state: STATE_VOLTAGE, arg: 800 });
    $p('bmZMeasure').disabled = false;
  }
  if (zPoints.length < 3) { $p('bmZResult').textContent = `only ${zPoints.length} valid point(s) — enable ISO+TPS and check the board`; drawZ(); return; }
  // least-squares V = a·I² + R0 : basis [I², 1]
  // V = (a·I² + R0)·I = a·I³ + R0·I  → R = V/I = a·I² + R0 (R0 = cold resistance).
  // LSQ basis [I³, I]: [sI6 sI4; sI4 sI2][a;R0] = [sI3V; sIV].
  let sI6 = 0, sI4 = 0, sI2 = 0, sI3V = 0, sIV = 0; const n = zPoints.length;
  for (const p of zPoints) { const i = p.i, i2 = i * i, i3 = i2 * i; sI6 += i3 * i3; sI4 += i2 * i2; sI2 += i2; sI3V += i3 * p.v; sIV += i * p.v; }
  const det = sI6 * sI2 - sI4 * sI4;
  let a = det ? (sI3V * sI2 - sI4 * sIV) / det : 0;
  let R0 = det ? (sI6 * sIV - sI4 * sI3V) / det : 0;
  if (R0 < 0) { R0 = 0; a = sI6 ? sI3V / sI6 : 0; }   // constrain R₀ ≥ 0, refit a
  zFit = { a, R0 };
  drawZ();
  $p('bmZFormula').innerHTML = zMathML(a, R0);
  $p('bmZResult').textContent = `R₀ ≈ ${R0.toFixed(3)} Ω · ${n} pts`;
}

// ChStartupOcp (0x37): host-configurable startup OCP current floor, applied at
// IDLE/ACTIVE turn-on. Global across the controller's channels. Firmware-ahead
// of build_command_payload, so routed via /api/cmd.
async function readStartupOcp() {
  if (!boardTargetConnected()) { $p('bmStartOcpRead').textContent = '—'; $p('bmStartOcpMetric').textContent = '—'; return; }
  const j = await postJ('/api/cmd', { controller: pwTarget, command: 'CH_STARTUP_OCP' });
  const raw = j.response && j.response.raw;
  if (raw && raw[0] === 0 && raw.length >= 3) {
    const ma = raw[1] | (raw[2] << 8);
    $p('bmStartOcpRead').textContent = ma + ' mA';
    $p('bmStartOcpMetric').textContent = ma + ' mA';
    $p('bmStartOcp').value = ma;
  } else { $p('bmStartOcpRead').textContent = '—'; $p('bmStartOcpMetric').textContent = '—'; }
}

// fast=true → lightweight V/I-only read (?vi=1): MERGE the live bus_mV/current_mA/
// present into the existing cache instead of replacing it, so the numbers update
// at ~1 Hz without paying for the two bitmap round-trips (enable/fault/mux presence
// change rarely and come from the periodic full refresh). fast=false → full snapshot.
let boardGen = 0;   // bumped on every refresh + controller switch; stale responses drop
async function refreshBoards(fast) {
  const conn = boardTargetConnected();
  if (!conn) { boardCache = emptyBoards(); renderBoardGrid(); renderOneBoard(); bmMsg(`Power ${pwTarget} not connected.`); return; }
  const gen = ++boardGen, target = pwTarget;   // this call supersedes any in-flight one
  let j;
  try { j = await (await fetch(`/api/board-snapshot?controller=${target}${fast ? '&vi=1' : ''}`)).json(); } catch { return; }
  // Drop the response if a newer refresh started or the user switched controllers
  // mid-flight — otherwise stale (e.g. Power 1) data lands after the switch to Power 2.
  if (gen !== boardGen || target !== pwTarget) return;
  if (!j.ok) { if (!fast) { boardCache = emptyBoards(); renderBoardGrid(); bmMsg(j.error || 'snapshot failed'); } return; }
  const rows = (j.boards && j.boards.length) ? j.boards : emptyBoards();
  if (fast && boardCache && boardCache.length === rows.length) {
    // merge only the volatile fields; keep bitmap-derived flags from the last full read
    const by = new Map(rows.map((b) => [`${b.channel}.${b.mux_port}`, b]));
    for (const b of boardCache) {
      const n = by.get(`${b.channel}.${b.mux_port}`);
      if (n) { b.bus_mV = n.bus_mV; b.current_mA = n.current_mA; b.present = n.present; b.ina_present = n.ina_present; }
    }
  } else {
    boardCache = rows;
  }
  renderBoardGrid(); renderOneBoard();
  bmMsg(`Power ${pwTarget} — ${boardCache.filter((b) => b.present).length}/64 present · INA219 refresh ${REFRESH_S}.`);
}
window.ctRefreshBoards = refreshBoards;

let connectedSet = {};
function boardTargetConnected() { return !!connectedSet[pwTarget]; }

const single = () => ({ target: 'single', channel: boardPrimary.channel, mux_port: boardPrimary.mux_port });
const batchExtra = () => ({ board_mask: boardMask() });

// VOUT_SR (TPS reg 0x03): read-modify-write OCP_DELAY[5:4] + slew[1:0] on one
// board. Register write is single-board only, so batch loops this per board.
async function voutSrOne({ channel, mux_port }, ocp, sr) {
  const rd = await powerCmd('CH_READ_TPS_REGISTER', { target: 'single', channel, mux_port, reg: 0x03, width_bytes: 1 });
  const cur = ((rd.response && rd.response.decoded && rd.response.decoded.value) || 0) & 0xFF;
  const next = (cur & ~0x33) | ((ocp << 4) & 0x30) | (sr & 0x03);
  const j = await powerCmd('CH_WRITE_TPS_REGISTER', { target: 'single', channel, mux_port, reg: 0x03, width_bytes: 1, value: next });
  return j.ok;
}

function wireBoards() {
  $p('boardsCard').innerHTML = BOARDS_HTML;
  // anchorFromSel: park the shift-block anchor on the lowest selected cell (or the
  // first enabled board) so a later shift-click starts from a real, visible origin.
  const anchorFromSel = () => {
    const ks = [...boardSel].sort();
    if (ks.length) { boardPrimary = keyTo(ks[0]); return; }
    let c = 0; while (c < 8 && !chEnabled(c)) c++;
    boardPrimary = { channel: c < 8 ? c : 0, mux_port: 0 };
  };
  $p('bmSelPresent').onclick = () => { boardSel.clear(); boardCache.forEach((b) => b.present && chEnabled(b.channel) && boardSel.add(bKey(b))); anchorFromSel(); renderBoardGrid(); };
  $p('bmSelAll').onclick = () => { boardSel.clear(); for (let c = 0; c < 8; c++) { if (!chEnabled(c)) continue; for (let m = 0; m < 8; m++) boardSel.add(`${c}.${m}`); } anchorFromSel(); renderBoardGrid(); };
  $p('bmSelClear').onclick = () => { boardSel.clear(); anchorFromSel(); renderBoardGrid(); };
  // Channel-enable mask (mirror of the I²C-section control). Get = present scan
  // (its response reflects channel_mask back via app.js); Set = push the mask.
  renderBoardMaskBits();
  $p('bmMaskGetBtn').onclick = () => { if (window.ctI2cGetMask) window.ctI2cGetMask(); else refreshBoards(); };
  $p('bmMaskSetBtn').onclick = async () => {
    if (window.ctI2cSetMask) { window.ctI2cSetMask(); return; }
    const j = await postJ('/api/channel-mask', { mask: window.ctChannelMask });
    bmMsg(j.ok === false ? (j.error || 'set mask failed') : 'channel mask set');
  };
  const run = async (p) => { const j = await p; bmMsg(j.ok ? 'ok' : (j.error || 'failed')); refreshBoards(true); };
  $p('bmIsoOn').onclick = () => run(powerCmd('CH_SET_ISO_ENABLE', { ...batchExtra(), enable: true }));
  $p('bmIsoOff').onclick = () => run(powerCmd('CH_SET_ISO_ENABLE', { ...batchExtra(), enable: false }));
  $p('bmTpsOn').onclick = () => run(powerCmd('CH_SET_TPS_ENABLE', { ...batchExtra(), enable: true }));
  $p('bmTpsOff').onclick = () => run(powerCmd('CH_SET_TPS_ENABLE', { ...batchExtra(), enable: false }));
  $p('bmReadIna').onclick = () => run(powerCmd('CH_GET_INA219', { ...batchExtra(), page_start: 0, max_entries: 64 }));
  $p('bmSetV').onclick = () => run(powerCmd('CH_SET_TPS_VOLTAGE', { ...batchExtra(), millivolts: Math.min(V_MAX_MV, +$p('bmTpsMv').value), enable_after_set: true }));
  $p('bmSetOcp').onclick = () => run(powerCmd('CH_SET_TPS_OCP_THRESHOLD', { ...batchExtra(), threshold_mA: Math.min(OCP_MAX_MA, +$p('bmOcp').value) }));
  $p('bmOneIsoOn').onclick = () => run(powerCmd('CH_SET_ISO_ENABLE', { ...single(), enable: true }));
  $p('bmOneIsoOff').onclick = () => run(powerCmd('CH_SET_ISO_ENABLE', { ...single(), enable: false }));
  $p('bmOneTpsOn').onclick = () => run(powerCmd('CH_SET_TPS_ENABLE', { ...single(), enable: true }));
  $p('bmOneTpsOff').onclick = () => run(powerCmd('CH_SET_TPS_ENABLE', { ...single(), enable: false }));
  $p('bmOneReadIna').onclick = () => run(powerCmd('CH_GET_INA219', single()));
  $p('bmOneSetOcp').onclick = async () => { await run(powerCmd('CH_SET_TPS_OCP_THRESHOLD', { ...single(), threshold_mA: Math.min(OCP_MAX_MA, +$p('bmOneOcp').value) })); readTpsRegs(); };
  // Power state (CH_SET_POWER_STATE 0x35) — routed via /api/cmd (firmware-ahead
  // of build_command_payload). Idle/Active carry mA; Voltage carries mV.
  document.querySelectorAll('#bmStateSeg button').forEach((b) =>
    b.addEventListener('click', () => {
      bmState = +b.dataset.state;
      // each state gets its own sensible default — don't carry Active's current into Idle
      const arg = $p('bmStateArg');
      if (arg) {
        if (bmState === STATE_IDLE) arg.value = 1000;
        else if (bmState === STATE_ACTIVE) arg.value = 3000;
        else if (bmState === STATE_VOLTAGE) arg.value = 800;
      }
      reflectStateArg();
    }));
  $p('bmApplyState').onclick = async () => {
    const cap = bmState === STATE_VOLTAGE ? V_MAX_MV : I_MAX_MA;
    const arg = (bmState === STATE_IDLE || bmState === STATE_ACTIVE || bmState === STATE_VOLTAGE)
      ? Math.min(cap, Math.max(0, +$p('bmStateArg').value)) : 0;
    const j = await postJ('/api/cmd', { controller: pwTarget, command: 'CH_SET_POWER_STATE', channel: boardPrimary.channel, mux_port: boardPrimary.mux_port, state: bmState, arg });
    bmMsg(j.ok ? `state ${bmState}${arg ? ' @ ' + arg : ''} set` : (j.error || 'state failed'));
    refreshBoards(true);
  };
  reflectStateArg();
  // Batch power state — applies the chosen state to every selected board (loops
  // CH_SET_POWER_STATE per board; 0x35 has no board-mask form).
  document.querySelectorAll('#bmBatchStateSeg button').forEach((b) =>
    b.addEventListener('click', () => {
      bmBatchState = +b.dataset.bstate;
      const arg = $p('bmBatchStateArg');
      if (arg) {
        if (bmBatchState === STATE_IDLE) arg.value = 1000;
        else if (bmBatchState === STATE_ACTIVE) arg.value = 3000;
        else if (bmBatchState === STATE_VOLTAGE) arg.value = 800;
      }
      reflectBatchStateArg();
    }));
  $p('bmBatchApplyState').onclick = async () => {
    const keys = [...boardSel];
    if (!keys.length) { bmMsg('no boards selected'); return; }
    const energising = bmBatchState === STATE_IDLE || bmBatchState === STATE_ACTIVE || bmBatchState === STATE_VOLTAGE;
    const cap = bmBatchState === STATE_VOLTAGE ? V_MAX_MV : I_MAX_MA;
    const arg = energising ? Math.min(cap, Math.max(0, +$p('bmBatchStateArg').value)) : 0;
    // Energising many boards at once can draw a lot of current — confirm.
    if (energising && keys.length > 1 &&
        !confirm(`Apply state ${bmBatchState}${arg ? ' @ ' + arg + (bmBatchState === STATE_VOLTAGE ? ' mV' : ' mA') : ''} to ${keys.length} boards on P${pwTarget}?`)) return;
    const btn = $p('bmBatchApplyState'); if (btn) btn.disabled = true;
    let okN = 0; const fails = [];
    for (const k of keys) {
      const { channel, mux_port } = keyTo(k);
      const j = await postJ('/api/cmd', { controller: pwTarget, command: 'CH_SET_POWER_STATE', channel, mux_port, state: bmBatchState, arg });
      if (j.ok) okN++; else fails.push(`CH${channel + 1}.${mux_port + 1}`);
      bmMsg(`applying state ${bmBatchState}… ${okN}/${keys.length}`);
    }
    if (btn) btn.disabled = false;
    bmMsg(fails.length ? `state ${bmBatchState}: ${okN}/${keys.length} ok · failed ${fails.join(' ')}`
      : `state ${bmBatchState}${arg ? ' @ ' + arg : ''} → ${okN} board(s)`);
    refreshBoards(true);
  };
  reflectBatchStateArg();
  $p('bmOneVoutSr').onclick = async () => {
    const ocp = (+$p('bmOneOcpDelay').value) & 0x03, sr = (+$p('bmOneSlew').value) & 0x03;
    const okv = await voutSrOne({ channel: boardPrimary.channel, mux_port: boardPrimary.mux_port }, ocp, sr);
    bmMsg(okv ? `VOUT_SR ← OCP delay ${ocp}, slew ${sr}` : 'VOUT_SR failed');
    readTpsRegs();
  };
  $p('bmStartOcpSet').onclick = async () => {
    const j = await postJ('/api/cmd', { controller: pwTarget, command: 'CH_STARTUP_OCP', set: true, threshold_mA: Math.min(OCP_MAX_MA, +$p('bmStartOcp').value) });
    bmMsg(j.ok ? 'startup OCP set' : (j.error || 'startup OCP failed')); readStartupOcp();
  };
  $p('bmStartOcpGet').onclick = readStartupOcp;
  $p('bmZMeasure').onclick = measureImpedance;
  $p('bmZR0').addEventListener('input', renderOneBoard);   // recompute Temp on R₀ change
  $p('bmZFormula').innerHTML = zMathML(null, null);   // symbolic until a sweep fits it
  renderBoardGrid();
}

// ---- HV card: grid + setpoint + monitor -------------------------------------
let hvDesired = [0, 0, 0, 0, 0, 0, 0, 0];
let hvFeedback = [0, 0, 0, 0, 0, 0, 0, 0];
const hvSel = new Set(['0.0']);
let hvPrimary = { channel: 0, bit: 0 };   // shift-block anchor (like boardPrimary)
let hvTest = null;   // Map "ch.b" → bool pass, from the last Toggle test switch-verify run
let hvTestAbort = false;   // set by the Quit button to stop a running Toggle test
const hvBit = (m, ch, b) => (m[ch] >> b) & 1;
// Drop selected HV bits (and move the anchor off) channels the mask just disabled.
function pruneHvMaskedSelection() {
  for (const k of [...hvSel]) if (!chEnabled(keyTo(k).channel)) hvSel.delete(k);
  if (!chEnabled(hvPrimary.channel)) {
    let ch = 0; while (ch < 8 && !chEnabled(ch)) ch++;
    hvPrimary = { channel: ch < 8 ? ch : 0, bit: 0 };
  }
}

const HV_HTML = `
  <div class="batch-box">
    <div class="block-title">HV grid <button id="hvMonitor" class="xs">Monitor: ON</button> <span class="hint">click=toggle · shift/ctrl=multi</span></div>
    <div class="legend bm-legend">
      <span class="legend-item"><span class="dot on">1</span> on+verified</span>
      <span class="legend-item"><span class="dot off">0</span> off</span>
      <span class="legend-item"><span class="dot fault">!</span> mismatch</span>
      <span class="legend-item"><span class="hv-test pass">✓</span>/<span class="hv-test fail">✗</span> switch test</span>
      <span class="hint">click=toggle · shift=block · ctrl=multi</span>
    </div>
    <div class="row compact i2c-mask bm-mask" title="Host poll set — disabled channels are dimmed, non-selectable, and skipped by the Toggle test. Shared with the Boards matrix + I²C section. (Firmware always scans all 8; this is a host-side filter.)">
      <span class="hint">channels enabled</span>
      <span id="hvMaskBits" class="i2c-mask-bits"></span>
      <button id="hvMaskGetBtn" class="xs mask-btn">Get</button>
      <button id="hvMaskSetBtn" class="xs mask-btn">Set</button>
    </div>
    <div class="hv-row">
      <button id="hvSelTest" class="xs" title="HV switch toggle test — auto-tests EVERY bit on all ENABLED channels: toggle each switch ON with HV_SET_BIT verify → firmware reads the switch feedback back → ✓ pass / ✗ FAIL (dead/non-actuating switch), then back OFF. Masked channels are skipped. Run with HV voltage at 0 (this exercises the switch, not emission). (Tests the SWITCH; use I²C diagnostics 'Chip test' for the expander chip.)">Toggle test</button>
      <button id="hvSelTestStop" class="xs danger" title="Abort the running Toggle test (finishes the current switch, then stops)." disabled>Quit</button>
      <button id="hvTestClear" class="xs" title="Clear the Toggle test ✓/✗ marks from the grid.">Clear</button>
    </div>
    <div id="hvGrid" class="board-grid"></div>
    <div class="hv-row">
      <button id="hvSelOn" class="xs">Sel ON</button>
      <button id="hvSelOff" class="xs">Sel OFF</button>
      <button id="hvAllOff" class="xs">All OFF</button>
      <button id="hvRefresh" class="xs">Refresh</button>
    </div>
    <div class="hv-row">
      <label class="numlabel">Pulse µs <input id="hvPulseUs" type="number" min="1" value="100" /></label>
      <button id="hvPulse" class="xs">Fire pulse</button>
    </div>
    <div id="hvStatus" class="summary"></div>
  </div>

  <div class="batch-box">
    <div class="block-title">HV setpoints <span class="hint">— LUT wiper (no drift)</span></div>
    <div class="bm-set-row hv-set" title="Enter the magnitude — 70 sets −70 V (the output is negative). Set V maps the target through the calibrated LUT to a DS3502 wiper and writes it directly (no closed loop, no drift). Cal sweeps the wiper 0→127 and reads the measured V at each step to (re)build the LUT — enable Emission HV first.">
      <label class="numlabel"><span class="cap">Emission −V</span><input id="emVset" type="number" min="0" max="350" value="0" /></label>
      <span id="emVmeas" class="pot-est">—</span>
      <button id="emVsetBtn" class="xs">Set V</button>
      <button id="emVcalBtn" class="xs">Cal</button>
    </div>
    <div class="bm-set-row hv-set" title="Enter the magnitude — 70 sets −70 V (the output is negative). Set V maps the target through the calibrated LUT to a DS3502 wiper and writes it directly (no closed loop, no drift). Cal sweeps the wiper 0→127 and reads the measured V at each step to (re)build the LUT — enable Focus HV first.">
      <label class="numlabel"><span class="cap">Focus −V</span><input id="focVset" type="number" min="0" max="495" value="0" /></label>
      <span id="focVmeas" class="pot-est">—</span>
      <button id="focVsetBtn" class="xs">Set V</button>
      <button id="focVcalBtn" class="xs">Cal</button>
    </div>
    <div class="bm-set-row hv-set">
      <label class="numlabel"><span class="cap">Emission I</span><input id="dsEi" type="number" min="0" max="127" value="0" /></label>
      <span id="dsEiEst" class="pot-est">~0 mA</span>
      <button id="dsSet" class="xs">Set I</button>
      <button id="dsRead" class="xs">Read</button>
    </div>
    <div class="block-title" style="margin-top:6px">Direct wiper <span class="hint">— raw DS3502 (bypasses the closed loop)</span></div>
    <div class="bm-set-row hv-set" title="Write the DS3502 wiper directly (0–127), bypassing the closed loop — to isolate firmware-loop vs GUI/hardware. Clr the closed loop first or it will overwrite this. Watch the monitor's Emission V.">
      <label class="numlabel"><span class="cap">Em-V wiper</span><input id="evWiper" type="number" min="0" max="127" value="0" /></label>
      <span id="evWiperEst" class="pot-est">—</span>
      <button id="evWiperSet" class="xs">Set</button>
      <button id="evWiperGet" class="xs">Read</button>
    </div>
    <div class="bm-set-row hv-set" title="Write the DS3502 wiper directly (0–127), bypassing the closed loop. Clr the focus closed loop first. Watch the monitor's Focus V.">
      <label class="numlabel"><span class="cap">Foc-V wiper</span><input id="fvWiper" type="number" min="0" max="127" value="0" /></label>
      <span id="fvWiperEst" class="pot-est">—</span>
      <button id="fvWiperSet" class="xs">Set</button>
      <button id="fvWiperGet" class="xs">Read</button>
    </div>
    <div class="hv-row">
      <button id="hvEnEm" class="xs hv-en off">Emission: ?</button>
      <button id="hvEnFoc" class="xs hv-en off">Focus: ?</button>
    </div>
    <div id="dsStatus" class="summary"></div>
  </div>

  <div class="batch-box">
    <div class="block-title">HV monitor <span class="hint">— ADS1115</span></div>
    <div class="row wrap">
      <button id="adsRead" class="xs">Read</button>
      <label class="chk"><input id="adsAuto" type="checkbox" checked /> Auto 2 Hz</label>
    </div>
    <div class="detail-top">
      <div class="metric"><span class="metric-label">1.2V ref</span><span id="adsRef" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Focus V</span><span id="adsFocus" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Emission I</span><span id="adsEmI" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Emission V</span><span id="adsEmV" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Emission HV</span><span id="hvEmStatus" class="metric-value">—</span></div>
      <div class="metric"><span class="metric-label">Focus HV</span><span id="hvFocStatus" class="metric-value">—</span></div>
    </div>
    <div id="adsStatus" class="summary"></div>
  </div>

  <div class="batch-box sb-test">
    <div class="block-title" title="Runs a calibration/test on ONE chosen board — pick the Power controller and channel.position below. Per-board version of the Calibration & Test suite.">Single-board Cal &amp; Test <span class="hint">ⓘ</span></div>
    <div class="row compact sb-target">
      <span class="hint">Power</span>
      <div class="seg sm" id="sbCtrlSeg">
        <button class="seg-btn active" data-sbctrl="1">P1</button>
        <button class="seg-btn" data-sbctrl="2">P2</button>
      </div>
      <label class="numlabel">CH<select id="sbCh">${[1, 2, 3, 4, 5, 6, 7, 8].map((n) => `<option>${n}</option>`).join('')}</select></label>
      <span class="sb-dot">.</span>
      <label class="numlabel">pos<select id="sbPos">${[1, 2, 3, 4, 5, 6, 7, 8].map((n) => `<option>${n}</option>`).join('')}</select></label>
    </div>
    <div class="seg sm" id="sbSeg">
      <button class="seg-btn active" data-sb="2" title="Emission current — fire one pulse on the selected board and read the per-pulse measurement (net Ie = peak − bg). Set heating current + emission voltage manually first.">Emis I</button>
      <button class="seg-btn" data-sb="3" title="Emission calibration — sweep heating voltage (mV), read per-pulse emission current at each step, plot + save the curve. Set emission V manually first.">Calib</button>
      <button class="seg-btn" data-sb="4" title="Impedance sweep — voltage sweep, plots V-I, fit R₀">Imped</button>
    </div>
    <div class="sb-params" id="sbP2">
      <label class="numlabel">pulse µs<input id="sb2Width" type="number" min="1" max="100000" value="1000" /></label>
      <span class="hint">set heat mA + emission V manually first</span>
    </div>
    <div class="sb-params" id="sbP3" hidden>
      <label class="numlabel">from mV<input id="sb3From" type="number" min="0" max="15000" step="100" value="800" /></label>
      <label class="numlabel">to mV<input id="sb3To" type="number" min="0" max="15000" step="100" value="3000" /></label>
      <label class="numlabel">step mV<input id="sb3Step" type="number" min="10" max="5000" step="50" value="100" /></label>
      <label class="numlabel">settle ms<input id="sb3Settle" type="number" min="0" max="5000" value="200" /></label>
      <span class="hint">set emission V manually first</span>
    </div>
    <div class="sb-params" id="sbP4" hidden>
      <label class="numlabel">end mV<input id="sb4End" type="number" min="900" max="15000" step="50" value="1500" /></label>
      <label class="numlabel">step mV<input id="sb4Step" type="number" min="20" max="2000" step="20" value="100" /></label>
      <label class="numlabel">dwell s<input id="sb4Dwell" type="number" min="0.1" max="20" step="0.1" value="1.5" /></label>
    </div>
    <div class="hv-row">
      <button id="sbRun" class="xs quick">Run</button>
      <button id="sbAbort" class="xs" disabled>Abort</button>
    </div>
    <canvas id="sbPlot" class="adc-plot" width="320" height="140" hidden></canvas>
    <div id="sbResult" class="summary"></div>
  </div>`;

// DS3502 writes share the STM32 I2C bus with the HV voltage closed-loop. Right
// after a Set V (emission/focus), that loop is actively walking its own wiper, so
// an immediate manual DS3502 write returns HTTP 502 (UART/I2C busy) until the loop
// settles (a few seconds). Retry with backoff instead of surfacing the transient.
async function ds3502SetRetry(ch, wiper, label) {
  let j;
  for (let i = 0; i < 8; i++) {
    j = await postJ('/api/stm32/ds3502-set', { controller: masterId(), ch, wiper });
    if (j.ok || j.status !== 502) return j;     // only retry the busy-bus 502
    hvFb(`${label}: HV loop busy on I²C, retrying ${i + 1}/8…`);
    await sleep(600);
  }
  return j;
}

function renderHvGrid() {
  const grid = $p('hvGrid'); if (!grid) return;
  grid.innerHTML = '';
  let on = 0, mm = 0;
  for (let ch = 0; ch < 8; ch++) for (let b = 0; b < 8; b++) {
    const d = hvBit(hvDesired, ch, b), f = hvBit(hvFeedback, ch, b), mis = d !== f;
    if (d) on++; if (mis && chEnabled(ch)) mm++;
    const k = `${ch}.${b}`;
    const tile = document.createElement('div');
    const tested = hvTest && hvTest.has(k);
    const mark = tested ? hvTest.get(k) : undefined;     // true=pass · false=real fail · 'err'=inconclusive
    const testPass = mark === true, testErr = mark === 'err';
    const glyph = testPass ? '✓' : testErr ? '⚠' : '✗';
    const word = testPass ? 'OK' : testErr ? 'INCONCLUSIVE (link busy/timeout — re-run)' : 'FAILED';
    tile.className = 'status-tile ' + (mis ? 'fault' : d ? 'present' : 'absent') + (hvSel.has(k) ? ' selected' : '')
      + (chEnabled(ch) ? '' : ' masked')
      + (mark === false ? ' test-fail' : testErr ? ' test-err' : '');
    tile.innerHTML = `<span class="tile-title">C${ch + 1}.${b + 1}</span><span class="hv-state">${mis ? '!' : d}</span>`
      + (tested ? `<span class="hv-test ${testPass ? 'pass' : testErr ? 'err' : 'fail'}" title="switch verify ${word}">${glyph}</span>` : '');
    tile.title = `CH${ch + 1} bit ${b + 1} — desired ${d}, feedback ${f}`
      + (tested ? ` · switch test ${testPass ? 'PASS' : testErr ? 'INCONCLUSIVE' : 'FAIL'}` : '');
    tile.addEventListener('click', (e) => {
      if (!chEnabled(ch)) return;                        // masked channel: non-interactive
      if (e.shiftKey) {
        // rectangular block from the anchor to this cell (enabled channels only)
        const r0 = Math.min(hvPrimary.channel, ch), r1 = Math.max(hvPrimary.channel, ch);
        const c0 = Math.min(hvPrimary.bit, b), c1 = Math.max(hvPrimary.bit, b);
        hvSel.clear();
        for (let c = r0; c <= r1; c++) { if (!chEnabled(c)) continue; for (let m = c0; m <= c1; m++) hvSel.add(`${c}.${m}`); }
        renderHvGrid();
      } else if (e.ctrlKey || e.metaKey) {
        hvSel.has(k) ? hvSel.delete(k) : hvSel.add(k);
        hvPrimary = { channel: ch, bit: b };
        renderHvGrid();
      } else {
        hvPrimary = { channel: ch, bit: b };
        hvSetBit(ch, b, !d);                             // plain click still actuates the switch
      }
    });
    grid.appendChild(tile);
  }
  const s = $p('hvStatus'); if (s) s.textContent = `${on}/64 on · ${mm} mismatch${hvMonitorOn ? ' · refresh ' + REFRESH_S : ' · monitor off'}`;
}

// Monitor gates the periodic HV feedback read — OFF = no SCK/LOAD activity from
// the GUI (so a scope sees a quiet bus). A manual Refresh still forces a read.
let hvMonitorOn = true;
async function refreshHv(force) {
  if (!boardTargetConnected() || (!hvMonitorOn && !force)) { return; }
  let j; try { j = await (await fetch(`/api/hv-snapshot?controller=${pwTarget}`)).json(); } catch { return; }
  if (j.ok) { hvDesired = j.desired; hvFeedback = j.feedback; renderHvGrid(); }
}
async function hvSetBit(ch, b, val) {
  await powerCmd('HV_SET_BIT', { channel: ch, bit: b, value: val, verify: true });
  refreshHv();
}
function hvSelMask() { const m = [0, 0, 0, 0, 0, 0, 0, 0]; for (const k of hvSel) { const [c, b] = k.split('.').map(Number); m[c] |= (1 << b); } return m; }
async function hvSelSet(val) {
  for (const k of hvSel) { const [c, b] = k.split('.').map(Number); await powerCmd('HV_SET_BIT', { channel: c, bit: b, value: val, verify: true }); }
  refreshHv();
}
// Pause/resume the target controller's background PING. The toggle test fires
// ~2 round-trips per switch on the shared bridge socket; without this they'd
// queue behind the 1 Hz PING (and its slow replies), so per-bit latency jitters.
const pollPause = (paused) => postJ('/api/poll-pause', { controller: pwTarget, paused });

// A transient (retryable) failure is a transport timeout or the device-busy
// mailbox status — NOT a real VERIFY_FAIL. VERIFY_FAIL means the switch didn't
// actuate (a genuine dead chip), which must be marked ✗ immediately, never
// retried away. No decoded status ⇒ the request timed out at the transport.
function hvCmdTransient(r) {
  if (r.ok) return false;
  const st = r.response && r.response.status;
  if (!st) return true;            // no status frame ⇒ transport timeout/error
  return st === 'BUSY';            // device mailbox momentarily busy
}

// Retry a power command ONLY on the transient busy/timeout (up to 4×, 200 ms
// apart). A real device status (VERIFY_FAIL, etc.) returns on the first try.
async function powerCmdRetry(command, extra) {
  let r;
  for (let i = 0; i < 4; i++) {
    r = await powerCmd(command, extra);
    if (!hvCmdTransient(r)) break;
    await sleep(200);
  }
  return r;
}

// Deterministic switch read. The firmware's own HV_SET_BIT verify reads the 165
// switch-SENSE line the instant after latching the 595, racing the switch's
// physical settling — so a marginal switch verifies differently run-to-run. We
// can't add the settle in firmware (it lives in the core-1 verify chain on a
// tight stack), so we do it on the HOST: force-write the bit (write-mode 2 — no
// firmware verify, no fault/clear), wait HV_SETTLE_MS, then force a FRESH 165
// read and fetch the byte. Returns the read-back bit (0/1), or null on a
// transport error (→ inconclusive).
const HV_SETTLE_MS = 12;
async function hvForceReadBit(c, b, value) {
  const w = await powerCmdRetry('HV_SET_BIT', { channel: c, bit: b, value, force: true });
  if (!w.ok) return null;
  await sleep(HV_SETTLE_MS);                              // host-side settle the firmware lacks
  const rf = await powerCmdRetry('HV_REFRESH_FEEDBACK', { channel: c });  // fresh 165 read
  if (!rf.ok) return null;
  const g = await powerCmdRetry('HV_GET_ALL_BYTES', {});
  const fb = g.ok && g.response && g.response.decoded && g.response.decoded.feedback;
  if (!fb) return null;
  return (fb[c] >> b) & 1;
}

// Switch-verify test: auto-test EVERY bit on all ENABLED channels. For each
// switch we force it ON and read the 165 sense back TWICE after a host settle
// (hvForceReadBit), then restore it OFF. A switch is ✓ only if both settled
// reads say "actuated", ✗ if both say "not actuated" (a real dead switch), and
// ⚠ inconclusive if the two reads disagree (genuinely flaky/marginal) or a
// transport error prevented a read. The PING is paused for the run. No firmware
// change — the settle that the firmware verify lacks is applied host-side.
async function hvSwitchTest() {
  if (!boardTargetConnected()) { $p('hvStatus').textContent = 'connect the target controller first'; return; }
  const keys = [];
  for (let c = 0; c < 8; c++) { if (!chEnabled(c)) continue; for (let b = 0; b < 8; b++) keys.push(`${c}.${b}`); }
  if (!keys.length) { $p('hvStatus').textContent = 'all channels masked off — enable a channel first'; return; }
  const nch = keys.length / 8;
  if (!confirm(`Toggle test exercises every HV switch on ${nch} enabled channel(s) of P${pwTarget} (${keys.length} switches, ON→settled read-back→OFF). Best run with HV voltage at 0. Continue?`)) return;
  hvTest = new Map();
  hvTestAbort = false;
  const btn = $p('hvSelTest'); if (btn) btn.disabled = true;
  const stop = $p('hvSelTestStop'); if (stop) stop.disabled = false;
  const fails = [], inconc = [];
  let aborted = false;
  await pollPause(true);
  try {
    let i = 0;
    for (const k of keys) {
      if (hvTestAbort) { aborted = true; break; }
      if (i++ % 8 === 0) pollPause(true);                  // re-arm the auto-expiring pause on long runs
      const [c, b] = k.split('.').map(Number);
      $p('hvStatus').textContent = `toggle test CH${c + 1}.${b + 1}… (${hvTest.size + 1}/${keys.length})`;
      const s1 = await hvForceReadBit(c, b, true);       // settled read #1
      const s2 = await hvForceReadBit(c, b, true);       // settled read #2 (must agree)
      await hvForceReadBit(c, b, false);                 // restore off
      let mark;                                          // true=pass · false=real dead · 'err'=inconclusive
      if (s1 === null || s2 === null) { mark = 'err'; inconc.push(`CH${c + 1}.${b + 1}`); }
      else if (s1 === 1 && s2 === 1) mark = true;        // consistently actuated
      else if (s1 === 0 && s2 === 0) { mark = false; fails.push(`CH${c + 1}.${b + 1}`); }  // consistently dead
      else { mark = 'err'; inconc.push(`CH${c + 1}.${b + 1}`); }   // reads disagree → flaky/marginal
      hvTest.set(k, mark);
      renderHvGrid();
    }
  } finally {
    await pollPause(false);                              // always resume the PING
  }
  if (btn) btn.disabled = false;
  if (stop) stop.disabled = true;
  await refreshHv(true);
  const done = hvTest.size;
  const ok = done - fails.length - inconc.length;
  const parts = [];
  if (fails.length) parts.push(`FAILED ${fails.join(' ')}`);
  if (inconc.length) parts.push(`INCONCLUSIVE ${inconc.join(' ')} (link busy or flaky switch — re-run)`);
  $p('hvStatus').textContent = aborted
    ? `toggle test ABORTED at ${done}/${keys.length}${parts.length ? ' · ' + parts.join(' · ') : ''}`
    : parts.length
      ? `toggle test: ${ok}/${done} ok · ${parts.join(' · ')}`
      : `toggle test: all ${keys.length} switch(es) verified ✓`;
}

function wireHv() {
  $p('hvCard').innerHTML = HV_HTML;
  renderHvGrid();
  // channel-enable mask mirror (shared window.ctChannelMask) — same control as
  // the Boards matrix; masked channels are greyed out + skipped by Toggle test.
  renderMaskBits('hvMaskBits');
  $p('hvMaskGetBtn').onclick = () => { if (window.ctI2cGetMask) window.ctI2cGetMask(); else refreshHv(true); };
  $p('hvMaskSetBtn').onclick = async () => {
    if (window.ctI2cSetMask) { window.ctI2cSetMask(); return; }
    const j = await postJ('/api/channel-mask', { mask: window.ctChannelMask });
    $p('hvStatus').textContent = j.ok === false ? (j.error || 'set mask failed') : 'channel mask set';
  };
  $p('hvSelOn').onclick = () => hvSelSet(true);
  $p('hvSelOff').onclick = () => hvSelSet(false);
  $p('hvSelTest').onclick = () => hvSwitchTest();
  $p('hvSelTestStop').onclick = () => { hvTestAbort = true; $p('hvStatus').textContent = 'aborting toggle test…'; };
  $p('hvTestClear').onclick = () => { hvTest = null; renderHvGrid(); $p('hvStatus').textContent = 'toggle test marks cleared'; };
  // All OFF: drive every bit OFF unconditionally — do NOT trust the (possibly
  // stale, monitor-off) hvDesired cache, or a switch that's actually ON could be
  // skipped and left energized.
  $p('hvAllOff').onclick = async () => { for (let c = 0; c < 8; c++) for (let b = 0; b < 8; b++) await powerCmd('HV_SET_BIT', { channel: c, bit: b, value: false, verify: true }); refreshHv(true); };
  $p('hvRefresh').onclick = () => refreshHv(true);
  $p('hvMonitor').onclick = () => {
    hvMonitorOn = !hvMonitorOn;
    $p('hvMonitor').textContent = `Monitor: ${hvMonitorOn ? 'ON' : 'OFF'}`;
    $p('hvMonitor').classList.toggle('off', !hvMonitorOn);
    if (hvMonitorOn) refreshHv(true);
  };
  $p('hvPulse').onclick = async () => { const j = await powerCmd('HV_PULSE', { hv_mask: hvSelMask(), width_us: +$p('hvPulseUs').value, verify_mode: 0 }); $p('hvStatus').textContent = j.ok ? 'pulse fired' : (j.error || 'pulse failed'); refreshHv(); };
  // LUT voltage: Emission/Focus target (V) → wiper via the calibrated LUT, then
  // written directly to the DS3502 (no closed loop, no drift). Cal (re)builds it.
  $p('emVsetBtn').onclick = () => hvLutSet('emission', 'emVset');
  $p('emVcalBtn').onclick = () => hvLutCalibrate('emission');
  $p('focVsetBtn').onclick = () => hvLutSet('focus', 'focVset');
  $p('focVcalBtn').onclick = () => hvLutCalibrate('focus');
  // Emission I — DS3502 wiper (no closed-loop for current)
  $p('dsEi').addEventListener('input', updatePotEsts);
  updatePotEsts();
  $p('dsSet').onclick = async () => {
    if (!hvConnGuard()) return;
    const w = +$p('dsEi').value;
    hvFb('Em-I: setting …');
    const j = await ds3502SetRetry('ei', w, 'Em-I');
    hvFb(j.ok ? `Em-I wiper ${w} set` : `Em-I ${hvErr(j)}`, j.ok ? 'ok' : 'bad');
  };
  $p('dsRead').onclick = async () => {
    if (!hvConnGuard()) return;
    hvFb('reading …');
    let j;
    try { j = await (await fetch(`/api/stm32/ds3502?controller=${masterId()}&ch=ei`)).json(); }
    catch (e) { hvFb(`Em-I read error — ${String((e && e.message) || e)}`, 'bad'); return; }
    if (j.ok && j.wiper != null) { $p('dsEi').value = j.wiper; updatePotEsts(); }
    hvFb(j.ok ? `Em-I wiper = ${j.wiper}` : `read ${hvErr(j)}`, j.ok ? 'ok' : 'bad');
  };
  // Direct DS3502 wiper for the V channels — writes the wiper raw, bypassing the
  // closed loop (Clr the loop first or it overwrites). Isolates firmware-loop vs
  // GUI/hardware: set a wiper, watch the monitor's Emission/Focus V respond.
  const wireWiper = (chan, inId, estId, setId, getId, label) => {
    const est = () => { const e = $p(estId); if (e) e.textContent = potEstText(chan, +$p(inId).value); };
    $p(inId).addEventListener('input', est); est();
    $p(setId).onclick = async () => {
      if (!hvConnGuard()) return;
      hvFb(`${label}: wiper ${+$p(inId).value} …`);
      const j = await ds3502SetRetry(chan, +$p(inId).value, label);
      hvFb(j.ok ? `${label} wiper ${+$p(inId).value} set (loop bypassed)` : `${label} ${hvErr(j)}`, j.ok ? 'ok' : 'bad');
    };
    $p(getId).onclick = async () => {
      if (!hvConnGuard()) return;
      let j; try { j = await (await fetch(`/api/stm32/ds3502?controller=${masterId()}&ch=${chan}`)).json(); }
      catch (e) { hvFb(`${label} read error — ${String((e && e.message) || e)}`, 'bad'); return; }
      if (j.ok && j.wiper != null) { $p(inId).value = j.wiper; est(); }
      hvFb(j.ok ? `${label} wiper = ${j.wiper}` : `read ${hvErr(j)}`, j.ok ? 'ok' : 'bad');
    };
  };
  wireWiper('ev', 'evWiper', 'evWiperEst', 'evWiperSet', 'evWiperGet', 'Em-V');
  wireWiper('fv', 'fvWiper', 'fvWiperEst', 'fvWiperSet', 'fvWiperGet', 'Foc-V');
  // Enable buttons = the COMMANDED state (sticky): a click flips instantly, sends
  // hv-enable, and only reverts if the command FAILS. The 2 Hz poll must NOT yank
  // them — otherwise the next click recomputes its direction from the (lagging)
  // real pin and you lose control of the toggle. The ACTUAL live pin level is the
  // monitor's "Emission HV / Focus HV" tiles (H·On / L·Off). readHvStatus(true)
  // syncs the buttons to reality once, on (re)connect / target-switch.
  const hvEnClick = async (chan, id, label) => {
    if (!hvConnGuard()) return;
    const next = $p(id).dataset.on !== '1';
    setHvEnBtn(id, label, next);               // flip immediately — no waiting for the round-trip
    hvFb(`${label}: turning ${next ? 'ON' : 'OFF'} …`);
    // The hv_enable command shares the single STM32 UART with the 2 Hz ads/hv_status
    // poll; a click that lands while the STM32 is mid-transaction misses the firmware's
    // 250 ms ACK window and the device returns HTTP 502 (UART busy). Retry the transient
    // busy-502 (same pattern as ds3502SetRetry) instead of surfacing it to the user.
    let j;
    for (let i = 0; i < 4; i++) {
      j = await postJ('/api/stm32/hv-enable', { controller: masterId(), ch: chan, on: next });
      if (j.ok || j.status !== 502) break;     // only retry the busy-bus 502
      hvFb(`${label}: STM32 link busy, retrying ${i + 1}/4…`);
      await sleep(250);
    }
    if (!j.ok) setHvEnBtn(id, label, !next);   // command still failed → undo the flip
    hvFb(j.ok ? `${label} ${next ? 'ON' : 'OFF'}` : `${label} ${hvErr(j)}`, j.ok ? 'ok' : 'bad');
  };
  $p('hvEnEm').onclick = () => hvEnClick('emission', 'hvEnEm', 'Emission');
  $p('hvEnFoc').onclick = () => hvEnClick('focus', 'hvEnFoc', 'Focus');
  // Mirror HV control+monitor (foldable card below Hardware Run): same master-STM32
  // handlers; its monitor/button values are kept in sync via the *2-suffixed IDs in
  // setHvPin / setHvEnBtn / adsRead / pollHvLoop / hvFb. Skips cleanly if absent.
  if ($p('emVsetBtn2')) $p('emVsetBtn2').onclick = () => hvLutSet('emission', 'emVset2');
  if ($p('emVcalBtn2')) $p('emVcalBtn2').onclick = () => hvLutCalibrate('emission');
  if ($p('focVsetBtn2')) $p('focVsetBtn2').onclick = () => hvLutSet('focus', 'focVset2');
  if ($p('focVcalBtn2')) $p('focVcalBtn2').onclick = () => hvLutCalibrate('focus');
  if ($p('hvEnEm2')) $p('hvEnEm2').onclick = () => hvEnClick('emission', 'hvEnEm', 'Emission');
  if ($p('hvEnFoc2')) $p('hvEnFoc2').onclick = () => hvEnClick('focus', 'hvEnFoc', 'Focus');
  setHvEnBtn('hvEnEm', 'Emission', false);   // start OFF (boot-safe) — updates primary + mirror
  setHvEnBtn('hvEnFoc', 'Focus', false);
  // ADS1115 monitor + DS3502 wiper readout — same 2 Hz auto-poll, on by default
  let adsTimer = null;
  const tick = () => { adsRead(); readHvStatus(false); };   // tiles live; buttons stay on the commanded state (poll must not yank a toggle)
  // Load both HV LUTs once at wire time and show their status next to Set V.
  refreshLutStatus('emission'); refreshLutStatus('focus');
  const adsAutoApply = (on) => { clearInterval(adsTimer); adsTimer = on ? setInterval(tick, 500) : null; };
  $p('adsRead').onclick = tick;
  $p('adsAuto').onchange = (e) => adsAutoApply(e.target.checked);
  adsAutoApply($p('adsAuto').checked);
  wireSingleBoardTests();
}

// ---- single-board calibration & test (selected board on the target controller)
// Per-board mirror of the all-filament suite (tests.js); reuses calApi primitives.
let sbBusy = false, sbAbort = false, sbSel = 2, sbCtrl = 1;
const sbMsg = (m) => { const e = $p('sbResult'); if (e) e.textContent = m; };
function sbSetRunning(on) { sbBusy = on; $p('sbRun').disabled = on; $p('sbAbort').disabled = !on; }
async function sbWaitHv(chan, magV, timeoutMs = 8000) {
  // LUT setpoint (no closed loop): map V→wiper, write it, let it settle. Returns
  // false only if there's no LUT or the target is outside its calibrated range.
  const r = await calApi.lutSetV(chan, magV);
  if (!r.ok) { sbMsg(`HV ${chan}: ${r.error || 'set failed'} — calibrate the LUT first.`); return false; }
  await calApi.tSleep(Math.min(timeoutMs, 600));
  return !r.clamped;
}
async function sbFilamentIndex(ctrl, ch, pos) {
  const m = await calApi.loadFilMap();
  for (const [f, b] of Object.entries(m)) if (b.ctrl === ctrl && b.ch === ch && b.pos === pos) return +f;
  return null;
}
async function sbInaR(ctrl, ch, pos) {
  const r = await calApi.inaSingle(ctrl, ch, pos), d = r && r.response && r.response.decoded;
  if (!d || !d.present) return null;
  const mA = d.current_mA || 0, V = (d.bus_mV || 0) / 1000;
  return { V, mA, R: mA > 0 ? V / (mA / 1000) : Infinity };
}
// Gate primitive: poll `read()` until the value is STEADY (plateaued) — `need`
// consecutive samples within `band` of each other — THEN judge it against target.
// The old gates returned ready on the first sample to cross the line, which fires
// mid-ramp (HV still climbing, filament still heating). Steadiness adapts the wait
// to the real ramp and distinguishes three outcomes:
//   ok:true,  steady:true  → settled AT/above target → safe to fire
//   ok:false, steady:true  → plateaued BELOW target (supply/regulator can't reach it)
//   ok:false, steady:false → never settled before timeout (still moving)
async function sbWaitSteady(read, target, { tol, band, need = 3, pollMs = 250, timeoutMs = 12000 }) {
  const t0 = Date.now();
  let last = NaN, prev = NaN, stable = 0;
  while (Date.now() - t0 < timeoutMs) {
    if (sbAbort) return { ok: false, steady: false, v: last };
    const v = await read();
    if (v != null && isFinite(v)) {
      last = v;
      stable = (isFinite(prev) && Math.abs(v - prev) <= band) ? stable + 1 : 0;
      prev = v;
      if (stable >= need) return { ok: v >= target - tol, steady: true, v };
    }
    await calApi.tSleep(pollMs);
  }
  return { ok: false, steady: false, v: last };
}
// Gate: emission HV (ADS1115 readback) must settle STEADY at target before firing.
// HV ramps can take several seconds — give it room rather than a tight timeout.
async function sbWaitEmV(targetV) {
  return sbWaitSteady(async () => {
    const j = await calApi.readAds();
    return (j && j.ok && j.emiss_v != null) ? Math.abs(+j.emiss_v) : null;
  }, targetV, { tol: Math.max(8, targetV * 0.05), band: Math.max(3, targetV * 0.02), timeoutMs: 15000 });
}
// Gate: heating current (filament INA219) must settle STEADY at the commanded target.
async function sbWaitHeat(ctrl, ch, pos, targetMa) {
  const r = await sbWaitSteady(async () => {
    const x = await sbInaR(ctrl, ch, pos);
    return x ? x.mA : null;
  }, targetMa, { tol: Math.max(15, targetMa * 0.05), band: Math.max(8, targetMa * 0.03), timeoutMs: 10000 });
  return { ...r, mA: r.v };   // callers read .mA
}

// Emission HV is set by DIRECT DS3502 wiper (via the calibrated LUT), not a closed
// loop. The old firmware closed loop drifted and left the wiper parked on teardown,
// so the next run started mid-range and diverged → emission V never reached → no
// pulse. A LUT-derived wiper is deterministic every click and we zero it on teardown.
async function sbEmission(ctrl, ch, pos) {
  // Minimal: the user sets heating current + emission voltage MANUALLY beforehand.
  // This just arms the detector (it free-runs), fires one pulse on the selected
  // board, and reports the per-pulse measurement. No HV/heat setup, no gating.
  const w = +$p('sb2Width').value || 1000;
  await calApi.pulseArm();                       // ensure the STM32 detector is running
  const cur = { id: await calApi.pulseCursor() };
  sbMsg(`firing ${w} µs HV pulse on CH${ch + 1}.${pos + 1}…`);
  const r = await calApi.fireAndMeasure(cur, ctrl, ch, pos, w);
  sbMsg(r
    ? `net Ie ${r.netMa.toFixed(1)} mA · peak ${r.peakMa.toFixed(1)} − bg ${r.bgMa.toFixed(1)} · high ${r.plateauMa.toFixed(1)} mA`
    : 'no pulse measured (no EVT_PULSE — set heat + emission V, or arm the detector)');
}

async function sbCalib(ctrl, ch, pos) {
  // Sweep the filament HEATING VOLTAGE (mV) and read the per-pulse emission
  // current at each step. Emission voltage is set MANUALLY by the user, not here.
  const from = +$p('sb3From').value || 800, to = +$p('sb3To').value || 3000, step = Math.max(1, +$p('sb3Step').value || 100);
  const settle = +$p('sb3Settle').value || 200;
  const fil = await sbFilamentIndex(ctrl, ch, pos);
  if (fil == null) { sbMsg('board not mapped to a filament — cannot save curve'); return; }
  const levels = []; for (let mv = from; mv <= to + 1e-6; mv += step) levels.push(Math.round(mv));
  const pts = [], curve = [], cur = { id: await calApi.pulseCursor() };
  await calApi.pulseArm();
  try {
    for (let i = 0; i < levels.length; i++) {
      if (sbAbort) break;
      const mv = levels[i];
      sbMsg(`F${fil} @ ${mv} mV (${i + 1}/${levels.length})…`);
      await calApi.setState(ctrl, ch, pos, 6, mv);                 // VOLTAGE mode = manual heating voltage
      await calApi.tSleep(settle);
      const r = await calApi.fireAndMeasure(cur, ctrl, ch, pos, 1000), mA = r ? r.netMa : 0;
      pts.push({ x: mv, y: Math.max(0, mA) });
      curve.push({ heatMv: mv, mA: +mA.toFixed(2), peak: r ? r.peak : null });
      calApi.drawCurve('sbPlot', pts, { xMax: to, yLabel: `Ie (mA) · F${fil}`, xUnit: 'mV' });
    }
  } finally {
    await calApi.setState(ctrl, ch, pos, 1, 0);                    // heating OFF (STOP)
  }
  if (curve.length) {
    const s = await calApi.saveCalibration('emission_calibration_sb', { params: { fromMv: from, toMv: to, stepMv: step, settleMs: settle, board: `P${ctrl}.CH${ch + 1}.${pos + 1}` }, curves: { [fil]: curve } });
    sbMsg(s.ok ? `✓ saved F${fil} curve — ${s.csv}` : `save failed: ${s.error || ''}`);
  }
}

async function sbImpedance(ctrl, ch, pos) {
  const startMv = 800, endMv = Math.round(+$p('sb4End').value || 1500);
  const stepMv = Math.max(1, Math.round(+$p('sb4Step').value || 100)), dwell = Math.max(100, (+$p('sb4Dwell').value || 1.5) * 1000);
  const fil = await sbFilamentIndex(ctrl, ch, pos);
  const pts = [], curve = [];
  try {
    for (let mv = startMv; mv <= endMv && !sbAbort; mv += stepMv) {
      sbMsg(`sweep ${mv} mV…`);
      await calApi.setState(ctrl, ch, pos, 6, mv); await calApi.tSleep(dwell);
      const r = await sbInaR(ctrl, ch, pos);
      if (r && r.mA > 0) { pts.push({ x: r.mA, y: r.V * 1000 }); curve.push({ v: +r.V.toFixed(4), i: +(r.mA / 1000).toFixed(4) }); calApi.drawCurve('sbPlot', pts, { yLabel: 'mV', xUnit: 'mA' }); }
    }
    await calApi.setState(ctrl, ch, pos, 2, 0);
  } finally { await calApi.setState(ctrl, ch, pos, 2, 0); }
  const R0 = calApi.fitR0(curve);
  sbMsg(R0 == null ? `${curve.length} pts — need ≥3 to fit` : `R₀ = ${R0.toFixed(3)} Ω (${curve.length} pts)`);
  if (curve.length >= 3 && fil != null) await calApi.saveCalibration('impedance_sweep_sb', { params: { startMv, endMv, stepMv, dwellMs: dwell }, curves: { [fil]: curve }, r0: { [fil]: R0 } });
}

function wireSingleBoardTests() {
  document.querySelectorAll('#sbCtrlSeg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => {
      sbCtrl = +b.dataset.sbctrl;
      document.querySelectorAll('#sbCtrlSeg .seg-btn').forEach((x) => x.classList.toggle('active', x === b));
    }));
  document.querySelectorAll('#sbSeg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => {
      sbSel = +b.dataset.sb;
      document.querySelectorAll('#sbSeg .seg-btn').forEach((x) => x.classList.toggle('active', x === b));
      for (let i = 2; i <= 4; i++) { const p = $p('sbP' + i); if (p) p.hidden = i !== sbSel; }
      $p('sbPlot').hidden = sbSel === 2;   // Emis I is a single reading — no plot
    }));
  $p('sbAbort').onclick = () => { sbAbort = true; sbMsg('Aborting…'); };
  $p('sbRun').onclick = async () => {
    if (sbBusy) return;
    if (!connectedSet[sbCtrl]) { sbMsg(`Power ${sbCtrl} not connected.`); return; }
    if (await calApi.anyRunning()) { sbMsg('A schedule is running — disarm first.'); return; }
    const ctrl = sbCtrl, ch = (+$p('sbCh').value || 1) - 1, pos = (+$p('sbPos').value || 1) - 1;
    const fn = { 2: sbEmission, 3: sbCalib, 4: sbImpedance }[sbSel];
    const needHv = sbSel === 3;   // Emis I just fires (user sets HV/heat manually); Calib energises
    if (needHv && !confirm('This test ENERGISES HV and fires pulses on the selected board. Continue?')) return;
    sbAbort = false; sbSetRunning(true);
    // Pause the GUI's background pollers (board snapshot, ADS, HV) so they don't
    // pile concurrent load on the master ESP32 while the test streams + fires.
    window.ctTestRunning = true;
    try { await fn(ctrl, ch, pos); }
    catch (e) { sbMsg('error: ' + ((e && e.message) || e)); }
    finally { window.ctTestRunning = false; sbSetRunning(false); }
  };
}
// DS3502 wiper (0–127) → estimated HV output (HV design doc / ds3502 scaling memo).
const POT_EST = { ev: { full: -350, unit: 'V' }, ei: { full: 85.7, unit: 'mA' }, fv: { full: -495, unit: 'V' } };
function potEstText(ch, wiper) {
  const s = POT_EST[ch];
  const w = Number.isFinite(wiper) ? Math.min(127, Math.max(0, wiper)) : 0;
  return `~${((w / 127) * s.full).toFixed(s.unit === 'mA' ? 1 : 0)} ${s.unit}`;
}
function updatePotEsts() { $p('dsEiEst').textContent = potEstText('ei', +$p('dsEi').value); }

// prominent, immediate feedback for every HV-setpoint action
function hvFb(msg, kind) { for (const e of [$p('dsStatus'), $p('dsStatus2')]) if (e) { e.textContent = msg; e.className = 'summary hv-fb ' + (kind || ''); } }
function hvConnGuard() {
  if (masterConnected()) return true;
  hvFb(`Master (Power ${masterId()}) not connected — Scan & Connect it first.`, 'bad'); return false;
}
const hvErr = (j) => `${j.error || j.message || 'failed'}${j.status ? ' [HTTP ' + j.status + ']' : ''}`;

// ---- HV setpoint via LUT (host-side; replaces the firmware closed loop) -----
// The STM32 closed loop walked the DS3502 toward an ADS target and drifted /
// never settled. Instead: Cal calibrates a wiper→measured-V LUT once, and Set V
// interpolates it to a wiper that's written directly. Stable, repeatable, no drift.
async function hvLutSet(chan, inputId) {
  if (!hvConnGuard()) return;
  const mag = Math.abs(+$p(inputId).value);
  if (!calApi.lutGet(chan)) await calApi.lutLoad(chan);
  if (!calApi.lutGet(chan)) { hvFb(`${chan}: no LUT — press Cal to build it (enable HV first)`, 'bad'); return; }
  hvFb(`${chan}: setting −${mag} V …`);
  const r = await calApi.lutSetV(chan, mag);
  if (!r.ok) { hvFb(`${chan} set ${hvErr(r)}`, 'bad'); return; }
  const warn = r.clamped ? ' ⚠ outside LUT range (clamped)' : '';
  hvFb(`${chan} → −${mag} V · wiper ${r.wiper} (≈${Math.round(r.expectV)} V)${warn}`, r.clamped ? 'bad' : 'ok');
  refreshLutStatus(chan);
}
// Cal: sweep the wiper 0→127 in 10 steps, read measured V at each, save the LUT.
// Requires HV already on (the sweep won't silently energise); confirms first.
let hvCalBusy = false;
async function hvLutCalibrate(chan) {
  if (!hvConnGuard() || hvCalBusy) return;
  if (!confirm(`Calibrate the ${chan} HV LUT?\n\nThis sweeps the DS3502 wiper 0→127 with HV ENERGISED and records the measured voltage at each step. Enable ${chan} HV first — the sweep will not energise it for you. The wiper is zeroed when done.`)) return;
  hvCalBusy = true; window.ctTestRunning = true;   // pause background pollers during the sweep
  try {
    hvFb(`${chan}: calibrating LUT …`);
    const r = await calApi.lutCalibrate(chan, {
      steps: 10, settleMs: 700,
      onStep: (i, n, w, v) => hvFb(`${chan} cal ${i}/${n}: wiper ${w} → ${v == null ? '—' : v.toFixed(1) + ' V'}`),
    });
    if (!r.ok) { hvFb(`${chan} cal ${hvErr(r)}`, 'bad'); return; }
    hvFb(`${chan} LUT saved — ${r.points.length} pts, range ≤ −${r.max_mag} V`, 'ok');
    refreshLutStatus(chan);
  } finally { hvCalBusy = false; window.ctTestRunning = false; }
}
// Inline LUT status next to Set V (emVmeas/focVmeas + mirror). Loads on demand.
async function refreshLutStatus(chan) {
  const id = chan === 'focus' ? 'focVmeas' : 'emVmeas';
  let lut = calApi.lutGet(chan);
  if (!lut) lut = await calApi.lutLoad(chan);
  const txt = lut ? `LUT ${lut.points.length}pt ≤−${lut.max_mag}V` : 'no LUT — Cal';
  const title = lut
    ? `Calibrated ${lut.ts || ''} · ${lut.points.length} points, max −${lut.max_mag} V. Set V interpolates this LUT.`
    : 'No LUT yet — enable HV, then press Cal to build the wiper→voltage table.';
  for (const e of [$p(id), $p(id + '2')]) if (e) { e.textContent = txt; e.title = title; }
}

let adsBusy = false;
// Blank every monitor field. Called on any failed read so stale HV numbers are
// never left on screen looking live — a safety monitor must read '—', not lie.
function blankAds(msg, kind) {
  for (const id of ['adsRef', 'adsFocus', 'adsEmI', 'adsEmV']) for (const e of [$p(id), $p(id + '2')]) if (e) e.textContent = '—';
  for (const id of ['ctHvEmV', 'ctHvEmI', 'ctHvFocV']) { const e = $p(id); if (e) e.textContent = '—'; }
  // hvEmStatus/hvFocStatus are owned by readHvStatus() (hv_status pin level) — not blanked here.
  const s = $p('adsStatus'); if (s) { s.textContent = msg; s.className = 'summary ' + (kind || ''); }
}
async function adsRead() {
  if (window.ctTestRunning) return;             // a Cal & Test owns the master — don't add load
  if (adsBusy || !masterConnected()) return;   // never overlap / flood the bridge (STM32 on master)
  adsBusy = true;
  try {
    const j = await (await fetch(`/api/stm32/ads1115?controller=${masterId()}`)).json();
    if (!j.ok) { blankAds(j.error || 'read failed', 'bad'); return; }
    const f = (v, u) => (v == null ? '—' : (+v).toFixed(2) + u);
    const setM = (id, v) => { for (const e of [$p(id), $p(id + '2')]) if (e) e.textContent = v; };   // primary + mirror
    setM('adsRef', f(j.ref_mv, ' mV'));
    setM('adsFocus', f(j.focus_v, ' V'));
    setM('adsEmI', f(j.emiss_i_ma, ' mA'));
    setM('adsEmV', f(j.emiss_v, ' V'));
    // mirror onto the CT-geometry overlay (top-right of the plot)
    const co = (id, v) => { const e = $p(id); if (e) e.textContent = v; };
    co('ctHvEmV', f(j.emiss_v, ' V')); co('ctHvEmI', f(j.emiss_i_ma, ' mA')); co('ctHvFocV', f(j.focus_v, ' V'));
    // NOTE: the Emission/Focus pin On/Off tiles are driven by readHvStatus()
    // (hv_status pin level), NOT the ADS1115 safety flags — those had emission/
    // focus polarity written wrong in firmware.
    // name which safety flag tripped instead of a vague "alert/diag"
    const warn = [j.ads1115_alert && 'ADS alert', j.amc3301_diag && 'AMC diag'].filter(Boolean).join(' · ');
    const s = $p('adsStatus'); if (s) { s.textContent = warn ? '⚠ ' + warn : 'ok'; s.className = 'summary' + (warn ? ' bad' : ''); }
  } catch (e) {
    blankAds('monitor read error — link down?', 'bad');   // surface it; don't silently keep stale values
  } finally { adsBusy = false; }
}


// Emission/Focus enable buttons = the COMMANDED (believed) state, sticky — the
// click toggle. The live REAL pin level is the "Emission HV / Focus HV" monitor
// tiles (setHvPin, H·On / L·Off). Button label: On / Off / — (unknown).
function setHvEnBtn(id, label, on) {
  const tag = on == null ? '—' : on ? 'On' : 'Off';
  for (const b of [$p(id), $p(id + '2')]) {   // primary + mirror panel (below Hardware Run)
    if (!b) continue;
    b.dataset.on = on ? '1' : '0';
    // color = state: red = commanded ON, green = OFF/safe, gray = unknown. Click toggles.
    b.innerHTML = `<span class="hv-en-dot">●</span> ${label} ${tag}`;
    b.className = 'xs hv-en ' + (on == null ? 'unk' : on ? 'on' : 'off');
    b.title = on == null ? `${label}: unknown` : `${label}: commanded ${on ? 'ON (energized)' : 'OFF'} — click to toggle (real pin level is in the HV monitor)`;
  }
}
// HV monitor tile = the ACTUAL emission/focus pin level read by the STM32
// (hv_status emission_on/focus_on). H → On, L → Off. This is the trustworthy
// source: the ADS1115 safety-flag bits had emission/focus polarity written wrong
// in firmware, so the tiles are driven from here — the same field the Emission /
// Focus buttons command — instead of the ADS flags. on==null ⇒ unknown ('—').
function setHvPin(id, on) {
  for (const e of [$p(id), $p(id + '2')]) {   // primary + mirror panel
    if (!e) continue;
    if (on == null) { e.textContent = '—'; e.className = 'metric-value'; continue; }
    e.textContent = on ? 'H · On' : 'L · Off';            // raw pin level + meaning
    e.className = 'metric-value ' + (on ? 'st-on' : 'st-off');
  }
}
// hv_status emission_on/focus_on are now correct in firmware (the earlier reversed
// polarity is fixed), so we consume them straight. If a future firmware regresses,
// flip HV_STATUS_POLARITY_REVERSED to true to invert here again.
const HV_STATUS_POLARITY_REVERSED = false;
const hvFix = (v) => (v == null ? null : (HV_STATUS_POLARITY_REVERSED ? !v : v));
// Reads hv_status and updates the monitor tiles every call. syncButtons=true ALSO
// pulls the enable BUTTONS to the real pin state — done only on (re)connect /
// target-switch; the 2 Hz monitor poll passes false so it never yanks a button
// out from under the user's click.
async function readHvStatus(syncButtons) {
  const blank = () => {
    setHvPin('hvEmStatus', null); setHvPin('hvFocStatus', null);
    if (syncButtons) { setHvEnBtn('hvEnEm', 'Emission', null); setHvEnBtn('hvEnFoc', 'Focus', null); }
  };
  if (!masterConnected()) { blank(); return; }
  let j;
  try { j = await (await fetch(`/api/stm32/hv-status?controller=${masterId()}`)).json(); }
  catch { blank(); return; }
  const em = hvFix(j.ok ? j.emission_on : null), fo = hvFix(j.ok ? j.focus_on : null);
  setHvPin('hvEmStatus', em); setHvPin('hvFocStatus', fo);
  if (syncButtons) { setHvEnBtn('hvEnEm', 'Emission', em); setHvEnBtn('hvEnFoc', 'Focus', fo); }
}

// ---- Emission & Schedule card: waveform + per-pulse + ShV -------------------
const EMI_HTML = `
  <div class="batch-box">
    <div class="block-title">Emission current waveform <span class="hint">— STM32 ADC · Capture=PSRAM shot · Live/Fire/GP40=ring</span></div>
    <div class="row wrap">
      <label class="numlabel">samples <input id="wfN" type="number" min="64" max="32768" value="2048" /></label>
      <label class="numlabel">kSPS <input id="wfRate" type="number" min="1" max="1000" value="1000" /></label>
      <button id="wfCapture" class="xs">Capture</button>
      <button id="wfTrig" class="xs" title="ESP32 fires GP37 now and pulls the full-rate window around it from the running ring (debug). Requires Live.">Fire ▶</button>
      <button id="wfTrigGp40" class="xs" title="Wait for the next external GP40 edge (RP2350-echoed fire) and pull the fire-correlated window from the ring. Requires Live.">GP40 ▶</button>
      <label class="chk" title="Continuous rolling capture from the STM32 ADC ring. While live the same stream also feeds per-pulse measurements."><input id="wfLive" type="checkbox" /> Live</label>
    </div>
    <canvas id="wfCanvas" class="adc-plot" width="600" height="160"></canvas>
    <div id="wfStats" class="summary"></div>
    <div id="wfStatus" class="summary">no capture yet</div>
  </div>

  <div class="batch-box">
    <div class="block-title">Per-pulse measurements <span class="hint">— STM32 1 MSPS · Stream arms the ADC; one event per detected pulse</span></div>
    <div class="row wrap">
      <button id="pulseStream" class="xs">Stream</button>
      <button id="pulseClear" class="xs">Clear</button>
      <span id="pulseSummary" class="hint">no events</span>
    </div>
    <div class="row compact"><input id="pulseSlider" type="range" min="0" max="0" value="0" title="Scroll through the pulse history (drag right = newest = auto-follow)" style="flex:1" /></div>
    <div class="pulse-wrap"><table class="pulse-table"><thead><tr><th>#</th><th>t µs</th><th>ON µs</th><th>peak mA</th><th>plat mA</th><th>bg±σ mA</th><th>∫ mA·µs</th></tr></thead><tbody id="pulseBody"></tbody></table></div>
  </div>

  <div class="batch-box">
    <div class="block-title">Fire-correlated pulses <span class="hint">— ESP32 Mode 2 · one summary per GP40 edge (RING_PULSE, TCP 3334)</span></div>
    <div class="row wrap" title="Arms the ESP32 to measure the ring window around every external GP40 fire edge (baseline/peak/width/integral). Needs the ring — turn Live on. Distinct from the STM32 detector above; pick one to avoid double-counting.">
      <label class="numlabel">pre <input id="rpPre" type="number" min="0" max="16000" value="256" /></label>
      <label class="numlabel">post <input id="rpPost" type="number" min="1" max="16000" value="2048" /></label>
      <label class="numlabel">thresh <input id="rpThresh" type="number" min="0" value="100" /></label>
      <label class="numlabel">report <select id="rpReport"><option value="1">summary</option><option value="2">raw</option><option value="3">both</option></select></label>
      <button id="rpArm" class="xs">Arm</button>
      <button id="rpDisarm" class="xs" disabled>Disarm</button>
    </div>
    <div id="rpStatus" class="summary">idle — turn Live on (ring), then Arm; events arrive per GP40 fire</div>
    <div class="pulse-wrap"><table class="pulse-table"><thead><tr><th>seq</th><th>t µs</th><th>base mA</th><th>peak mA</th><th>peak@</th><th>width</th><th>∫ mA·µs</th></tr></thead><tbody id="rpBody"></tbody></table></div>
  </div>

  <div class="batch-box">
    <div class="block-title">Record measurement <span class="hint">— raw ADC waveform + per-pulse → host files</span></div>
    <div class="row wrap" title="Records the raw STM32 ADC samples (ring tap → .bin) AND the per-pulse measurements (→ .csv) to host files for the whole session. Use decim to fit the WiFi link if you see drops.">
      <label class="numlabel">kSPS <input id="recRate" type="number" min="1" max="1000" value="1000" /></label>
      <label class="numlabel">decim <input id="recDecim" type="number" min="1" max="2048" value="1" /></label>
      <button id="recStart" class="xs quick">● Record</button>
      <button id="recStop" class="xs" disabled>Stop</button>
    </div>
    <div id="recStatus" class="summary">idle</div>
    <div id="recFiles" class="summary"></div>
  </div>

  <details class="batch-box" id="syncDetails">
    <summary class="block-title">ESP32 Sync I/O <span class="hint">— trigger lines</span></summary>
    <div id="syncLines" class="summary">lines: —</div>
    <div class="bm-set-row">
      <label class="numlabel">SyncOut edge <select id="syncOutEdge"><option value="rising">rising</option><option value="falling">falling</option></select></label>
      <label class="numlabel">width µs <input id="syncWidth" type="number" min="0" max="100000" value="5" /></label>
    </div>
    <div class="bm-set-row">
      <label class="numlabel">Ready active <select id="syncReady"><option value="high">high</option><option value="low">low</option></select></label>
      <label class="numlabel">Ext edge <select id="syncExtEdge"><option value="rising">rising</option><option value="falling">falling</option></select></label>
    </div>
    <div class="hv-row">
      <button id="syncConfig" class="xs">Apply</button>
      <button id="syncFire" class="xs quick">Fire SyncOut</button>
      <button id="syncAbort" class="xs">Abort</button>
    </div>
    <div id="syncStatus" class="summary"></div>
  </details>

  <details class="batch-box" open>
    <summary class="block-title">Simple HV schedule <span class="hint">— ShV 0x70–0x7B</span></summary>

    <div class="shv-group">
      <div class="shv-grouplabel">Active list <span class="hint">— mapping → power slots</span></div>
      <div class="hv-row">
        <button id="shvPushList" class="xs" title="Push the host filament→power mapping (64-byte ShvSetActiveList) to this target controller. Edit it in the Filament → Power card.">Push mapping</button>
        <button id="shvGetList" class="xs" title="Read this controller's active list and count how many of its 64 power slots map to a filament.">Read</button>
      </div>
    </div>

    <div class="shv-group">
      <div class="shv-grouplabel">Config — timeouts</div>
      <div class="bm-set-row">
        <label class="numlabel">inter ms <input id="shvInter" type="number" value="3000" /></label>
        <label class="numlabel">maxOn ms <input id="shvMaxOn" type="number" value="40" /></label>
      </div>
      <div class="bm-set-row">
        <label class="numlabel">total ms <input id="shvTotal" type="number" value="60000" /></label>
        <label class="numlabel" title="Edge of the SyncIn trigger the schedule fires on. Match this to the Sync I/O 'Ext edge'.">trig edge
          <select id="shvTrigEdge"><option value="rising">rising</option><option value="falling">falling</option></select></label>
        <button id="shvSetCfg" class="xs">Set cfg</button>
      </div>
    </div>

    <div class="shv-group">
      <div class="shv-grouplabel">Table <span class="hint">— filament,numPulses,widthUs per line</span></div>
      <textarea id="shvEntries" rows="3" class="shv-entries">0,5,1000</textarea>
      <div class="hv-row">
        <button id="shvUpload" class="xs">Upload</button>
        <button id="shvClear" class="xs">Clear</button>
        <button id="shvInfo" class="xs">Info</button>
        <button id="shvHeatInfo" class="xs">Heat info</button>
      </div>
    </div>

    <div class="shv-group">
      <div class="shv-grouplabel">Run <span class="hint">— fires on SyncIn</span></div>
      <div class="hv-row">
        <button id="shvArm" class="xs quick">Arm</button>
        <button id="shvDisarm" class="xs">Disarm</button>
        <button id="shvStatus" class="xs">Status</button>
        <button id="shvLog" class="xs">Log</button>
      </div>
    </div>

    <div class="shv-group">
      <div class="shv-grouplabel shv-warn">⚠ Capability test — FIRES HV</div>
      <div class="bm-set-row">
        <label class="numlabel">ch <input id="shvCapCh" type="number" min="0" max="7" value="0" /></label>
        <label class="numlabel">bit,µs pairs <input id="shvCapPairs" type="text" value="0,500 1,1500" title="space-separated bit,widthUs" /></label>
        <button id="shvCapRun" class="xs">Run</button>
      </div>
    </div>

    <pre id="shvResult" class="summary shv-result"></pre>
  </details>`;

let pulseList = [], pulseSince = 0, pulseTimer = null, wfTimer = null, pulseArmed = false;

// Emission current from a raw ADC count (wifi_gui conversion):
//   V = raw·3.1/4095 (ESP32 ADC_ATTEN_12);  Ie = 2·(V − 0.5·1.155)/6.8/8.2 A → mA.
const emissionMa = (raw) => 2 * (raw * 3.1 / 4095 - 0.5 * 1.155) / 6.8 / 8.2 * 1000;
// mA per ADC count (slope only) — use for deviations (σ) and bg-relative sums
// (integral) where the constant offset cancels.
const EMI_MA_PER_COUNT = 2 * (3.1 / 4095) / 6.8 / 8.2 * 1000;
function drawWaveform(samples, rate, trigIdx) {
  const c = $p('wfCanvas'); if (!c || !samples.length) return;
  const ctx = c.getContext('2d'), W = c.width, H = c.height;
  ctx.clearRect(0, 0, W, H); ctx.fillStyle = '#070b0e'; ctx.fillRect(0, 0, W, H);
  // ---- emission-current stats (wifi_gui conversion) ----
  // V = raw·3.1/4095 (ESP32 ADC_ATTEN_12);  Ie = 2·(V − 0.5·1.155)/6.8/8.2 A → mA.
  let lo = Infinity, hi = -Infinity, sum = 0, sum2 = 0;
  for (const s of samples) { if (s < lo) lo = s; if (s > hi) hi = s; sum += s; sum2 += s * s; }
  const n = samples.length;
  const mean = sum / n;
  const sigma = Math.sqrt(Math.max(0, sum2 / n - mean * mean));
  const avgMa = emissionMa(mean), sigMa = sigma * EMI_MA_PER_COUNT, curMa = emissionMa(samples[n - 1]);
  const se = $p('wfStats');
  if (se) se.innerHTML = `current <b>${curMa.toFixed(2)}</b> mA · average <b>${avgMa.toFixed(2)}</b> mA · σ <b>${sigMa.toFixed(2)}</b> mA`;
  const span = Math.max(1, hi - lo);
  // trigger marker (vertical amber line at the trigger sample)
  if (trigIdx != null && trigIdx >= 0 && trigIdx < samples.length) {
    const tx = (trigIdx / (samples.length - 1)) * W;
    ctx.strokeStyle = '#f2c14e'; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(tx, 0); ctx.lineTo(tx, H); ctx.stroke(); ctx.setLineDash([]);
  }
  ctx.strokeStyle = '#3fb6a0'; ctx.lineWidth = 1; ctx.beginPath();
  for (let i = 0; i < samples.length; i++) {
    const x = (i / (samples.length - 1)) * W;
    const y = H - ((samples[i] - lo) / span) * (H - 8) - 4;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
  const tg = trigIdx != null ? ` · trig@${trigIdx}` : '';
  $p('wfStatus').textContent = `${samples.length} samples · ${lo}–${hi} raw${rate ? ' · ' + rate + ' Hz' : ''}${tg}`;
}
let wfBusy = false;
async function wfCapture() {
  if (wfBusy) return;                                   // shots are synchronous — never overlap
  if (!masterConnected()) { $p('wfStatus').textContent = `Master (Power ${masterId()}) not connected`; return; }
  wfBusy = true;
  try { await wfCaptureInner(); } finally { wfBusy = false; }
}
async function wfCaptureInner() {
  const n = +$p('wfN').value;
  const rate = Math.max(1, +$p('wfRate').value || 1000) * 1000;   // kSPS → SPS
  $p('wfStatus').textContent = `capturing ${n} @ ${rate / 1000} kSPS…`;
  // Bounded STM32 ADC shot over SPI (the only ADC) — blocks ~n/fs s server-side.
  let j;
  try { j = await (await fetch(`/api/adc/spi-shot?controller=${masterId()}&n=${n}&rate=${rate}`)).json(); }
  catch (e) { $p('wfStatus').textContent = `capture error — ${String((e && e.message) || e)}`; return; }
  if (!j.ok) { $p('wfStatus').textContent = j.error || 'capture failed'; return; }
  drawWaveform(j.samples || [], j.rate_hz);
}
// ---- live rolling waveform from the continuous STM32 ADC ring ---------------
let wfLive = false;
async function wfLiveTick() {
  if (wfBusy) return;
  wfBusy = true;
  try {
    const n = +$p('wfN').value;
    const j = await (await fetch(`/api/adc/ring-peek?controller=${pwTarget}&n=${n}`)).json();
    if (j.ok) drawWaveform(j.samples || [], j.rate_hz);
    else $p('wfStatus').textContent = j.error || 'ring peek failed';
  } catch (e) { /* keep the live loop alive */ } finally { wfBusy = false; }
}
async function wfSetLive(on) {
  const cb = $p('wfLive');
  if (on) {
    const rate = Math.max(1, +$p('wfRate').value || 1000) * 1000;
    $p('wfStatus').textContent = 'starting ring…';
    const j = await postJ('/api/adc/ring-start', { controller: pwTarget, rate });
    if (!j.ok) { if (cb) cb.checked = false; $p('wfStatus').textContent = `ring start failed — ${j.error || j.message || ''}`; return; }
    wfLive = true;
    clearInterval(wfTimer); wfTimer = setInterval(wfLiveTick, 250);   // 4 Hz rolling
    $p('wfStatus').textContent = `live @ ${rate / 1000} kSPS — rolling (also feeds per-pulse)`;
  } else {
    wfLive = false;
    clearInterval(wfTimer); wfTimer = null;
    await postJ('/api/adc/ring-stop', { controller: pwTarget });
    $p('wfStatus').textContent = 'live stopped';
  }
}
// Continuous → trigger → retrieve: fire SyncOut and pull the trigger-aligned
// full-rate window out of the running ring. Needs Live (the ring) running.
const WF_RING_CAP = 16384;   // adc_spi kRingSamples — pre+post+1 must fit the ring
// src: 'fire' = ESP32 pulses GP37 now (debug); 'gp40' = wait for the external
// RP2350-echoed edge (fire-correlated). Both pull the window from the live ring.
async function wfTrigCapture(src) {
  if (wfBusy) return;
  if (!wfLive) { $p('wfStatus').textContent = 'turn Live on first (the ring must be running)'; return; }
  wfBusy = true;
  try {
    // Window must fit the ring (pre+post+1 ≤ cap); clamp the requested count.
    const total = Math.min(+$p('wfN').value, WF_RING_CAP - 1);
    const pre = Math.max(0, Math.round(total * 0.25)), post = Math.max(1, total - pre);
    const clamp = (+$p('wfN').value > WF_RING_CAP - 1) ? ' (clamped to ring)' : '';
    $p('wfStatus').textContent = src === 'gp40'
      ? `waiting for GP40 edge — pre ${pre} / post ${post}${clamp}…`
      : `firing — pre ${pre} / post ${post}${clamp}…`;
    const j = await postJ('/api/adc/ring-window', { controller: pwTarget, pre, post, src });
    if (!j.ok) { $p('wfStatus').textContent = `${src === 'gp40' ? 'GP40' : 'trigger'} ${j.error || j.message || 'failed'}`; return; }
    drawWaveform(j.samples || [], j.rate_hz, j.pre);
  } finally { wfBusy = false; }
}
// ---- measurement recorder: raw ADC waveform + per-pulse → host files --------
let recTimer = null;
async function recPoll() {
  let j; try { j = await (await fetch('/api/record/status')).json(); } catch { return; }
  if (!j.ok || !j.recording) { if (!j.recording) $p('recStatus').textContent = 'idle'; return; }
  const M = (j.adc_samples || 0) / 1e6;
  const obs = j.rate_hz_obs ? `${Math.round(j.rate_hz_obs / 1000)} kSPS` : '—';
  const drop = (j.drops || j.missed_packets) ? ` · ⚠ drops ${j.drops}/miss ${j.missed_packets}` : '';
  $p('recStatus').textContent = `● REC ${j.duration_s || 0}s · ADC ${M.toFixed(2)}M samp @ ${obs} · pulses ${j.pulse_events}${drop}`;
}
// ---- ESP32 fire-correlated per-pulse (Mode 2, RING_PULSE over TCP 3334) -----
let rpList = [], rpSince = 0, rpTimer = null, rpArmed = false;
function rpRender() {
  const body = $p('rpBody'); if (!body) return;
  body.innerHTML = rpList.slice(-50).map((e) =>
    `<tr><td>${e.seq}</td><td>${e.t_edge_us}</td>`
    + `<td>${emissionMa(e.baseline).toFixed(2)}</td>`
    + `<td>${emissionMa(e.peak).toFixed(2)}</td>`
    + `<td>${e.peak_index}</td><td>${e.width}</td>`
    + `<td>${(e.integral * EMI_MA_PER_COUNT).toFixed(1)}</td></tr>`).join('');
}
async function rpTick() {
  let j; try { j = await (await fetch(`/api/ringpulse/events?since=${rpSince}`)).json(); } catch { return; }
  if (!j.ok) return;
  if (j.events && j.events.length) {
    rpList.push(...j.events); if (rpList.length > 4096) rpList = rpList.slice(-4096);
    rpSince = j.events[j.events.length - 1].eid; rpRender();
  }
  $p('rpStatus').textContent = `${rpArmed ? '● armed' : 'idle'} · ${j.connected ? '3334 connected' : 'disconnected'} · ${rpList.length} events`;
}
function wireRingPulse() {
  $p('rpArm').onclick = async () => {
    $p('rpStatus').textContent = 'arming…';
    const j = await postJ('/api/ringpulse/arm', { controller: pwTarget, rate: 1000000,
      pre: +$p('rpPre').value, post: +$p('rpPost').value, thresh: +$p('rpThresh').value, report: +$p('rpReport').value });
    if (!j.ok) { $p('rpStatus').textContent = `arm ${j.error || j.message || 'failed'}`; return; }
    rpArmed = true; $p('rpArm').disabled = true; $p('rpDisarm').disabled = false;
    clearInterval(rpTimer); rpTimer = setInterval(rpTick, 500); rpTick();
  };
  $p('rpDisarm').onclick = async () => {
    await postJ('/api/ringpulse/disarm', { controller: pwTarget });
    rpArmed = false; $p('rpArm').disabled = false; $p('rpDisarm').disabled = true;
    clearInterval(rpTimer); rpTimer = null; $p('rpStatus').textContent = 'disarmed';
  };
}
function wireRecord() {
  $p('recStart').onclick = async () => {
    const rate = Math.max(1, +$p('recRate').value || 1000) * 1000;
    const decim = Math.max(1, +$p('recDecim').value || 1);
    $p('recStatus').textContent = 'starting…';
    const j = await postJ('/api/record/start', { controller: pwTarget, rate, decim });
    if (!j.ok) { $p('recStatus').textContent = `start failed — ${j.error || ''}`; return; }
    $p('recStart').disabled = true; $p('recStop').disabled = false; $p('recStart').classList.add('danger');
    $p('recFiles').textContent = `recording → ${j.adc_file} · ${j.pulse_file}`;
    clearInterval(recTimer); recTimer = setInterval(recPoll, 1000); recPoll();
  };
  $p('recStop').onclick = async () => {
    const j = await postJ('/api/record/stop', { controller: pwTarget });
    clearInterval(recTimer); recTimer = null;
    $p('recStart').disabled = false; $p('recStop').disabled = true; $p('recStart').classList.remove('danger');
    if (j.ok) {
      $p('recStatus').textContent = `saved · ADC ${(((j.adc_samples || 0)) / 1e6).toFixed(2)}M samp · ${j.pulse_events || 0} pulses`;
      const dl = (f) => `<a href="/api/record/download?file=${f}" download>${f}</a>`;
      $p('recFiles').innerHTML = j.adc_file ? `${dl(j.adc_file)} · ${dl(j.pulse_file)}` : '';
    } else { $p('recStatus').textContent = j.error || 'stop failed'; }
  };
}
const PULSE_WIN = 50;        // rows shown at once
let pulseFollow = true;      // auto-track the newest pulses (slider pinned right)
function renderPulses() {
  const body = $p('pulseBody'); if (!body) return;
  const sl = $p('pulseSlider'), n = pulseList.length;
  const maxOff = Math.max(0, n - PULSE_WIN);
  if (sl) {
    sl.max = maxOff;
    if (pulseFollow) sl.value = maxOff;       // pinned to newest while following
  }
  const off = sl ? Math.min(+sl.value, maxOff) : maxOff;
  const rows = pulseList.slice(off, off + PULSE_WIN);
  // ADC-count fields → emission mA. peak/plateau/bg are absolute (emissionMa);
  // σ (bg_sigma4/4) and integral (Σ of sample−bg) are bg-relative → slope only.
  body.innerHTML = rows.map((p) =>
    `<tr><td>${p.id}</td><td>${p.t_us}</td><td>${p.on_us}</td>`
    + `<td>${emissionMa(p.peak).toFixed(2)}</td>`
    + `<td>${p.plateau ? emissionMa(p.plateau).toFixed(2) : '—'}</td>`
    + `<td>${emissionMa(p.bg).toFixed(2)}±${((p.bg_sigma4 / 4) * EMI_MA_PER_COUNT).toFixed(2)}</td>`
    + `<td>${(p.integral * EMI_MA_PER_COUNT).toFixed(1)}</td></tr>`).join('');
  const span = rows.length ? `${off + 1}–${off + rows.length}` : '0';
  $p('pulseSummary').textContent = `${n} events · showing ${span}${pulseFollow ? ' · live' : ' · held'}`;
}
async function pulseTick() {
  if (!masterConnected()) { $p('pulseSummary').textContent = `master (Power ${masterId()}) not connected`; return; }
  let j; try { j = await (await fetch(`/api/pulse-events?controller=${masterId()}&since=${pulseSince}`)).json(); } catch { return; }
  if (j && j.ok === false) { $p('pulseSummary').textContent = `pulse read failed — ${j.error || j.message || ''}`; return; }
  if (j.ok && j.events && j.events.length) {
    pulseList.push(...j.events); if (pulseList.length > 4096) pulseList = pulseList.slice(-4096);
    pulseSince = j.events[j.events.length - 1].id; renderPulses();
  }
}
const shv = (op, extra) => postJ('/api/shv', { controller: pwTarget, op, ...extra });
// names from the current firmware (simple_hv_schedule.h enums)
const SHV_STATE = ['Idle', 'Armed', 'Running', 'Complete', 'Fault'];
const SHV_STOP = ['None', 'Complete', 'Mismatch', 'InterPulseTimeout', 'TotalTimeout', 'Fault', 'Disarmed'];
const SHV_REJECT = ['None (armed)', 'IndexOutOfWindow', 'WidthTooLarge', 'EmptyTable', 'TpsDisabled', 'TpsFault', 'IsoOff', 'NotReady', 'StateConflict'];
const nm = (arr, i) => (i == null ? '?' : (arr[i] || i));

const shvMsg = (t) => { const e = $p('shvResult'); if (e) e.textContent = t; };
function shvShow(j, label) {
  const lab = label || 'op';
  let t;
  if (j && j.status) {
    const s = j.status;
    t = `state ${nm(SHV_STATE, s.state)} · stop ${nm(SHV_STOP, s.stopReason)}\n`
      + `entry ${s.entryIndex}/${s.entryCount} · filament ${s.filamentIndex === 255 ? '—' : s.filamentIndex} · pulses ${s.totalPulsesDone}/${s.totalPulsesTarget}\n`
      + `elapsed ${s.elapsedMs} ms` + (s.faultFilament !== 255 ? ` · fault fil ${s.faultFilament}` : '');
  } else if (j && j.reject != null && j.results) {            // capability test (0xFFFF = 165 verify mismatch)
    t = `reject: ${nm(SHV_REJECT, j.reject)}\n` + j.results.map((r) => `  bit ${r.bit}: ${r.measuredUs === 0xFFFF ? 'verify MISMATCH' : 'measured ' + r.measuredUs + ' µs'}`).join('\n');
  } else if (j && j.reject != null) {                          // arm
    t = `${j.ok ? '✓ armed' : '✗ arm rejected'} — ${nm(SHV_REJECT, j.reject)}`;
  } else if (j && j.records) {                                 // pulse log
    t = `${j.records.length}/${j.total} records\n` + j.records.map((r) => `  fil ${r.filament} seq ${r.seq} t ${r.tOnUs}µs dur ${r.durationUs}µs${r.flags ? ' flags 0x' + r.flags.toString(16) : ''}`).join('\n');
  } else if (j && j.entryCount != null) {                      // table info
    t = `✓ table: ${j.entryCount} entries · crc 0x${(j.crc >>> 0).toString(16).toUpperCase()}`;
  } else if (j && j.heatCount != null) {                       // heat-table info
    t = `✓ heat entries ${j.heatCount}/${j.maxHeatEntries}`;
  } else if (j && j.interPulseMs != null) {                    // get config
    t = `inter ${j.interPulseMs} ms · maxOn ${j.maxOnMs} ms · total ${j.totalMs} ms · edge ${j.triggerEdge ? 'falling' : 'rising'}`;
  } else if (j && j.mapped != null) {                          // active-list read
    t = `✓ active list: ${j.mapped}/64 power slots mapped`;
  } else if (j && j.count != null) {                           // set_entries (upload)
    t = `${j.ok ? '✓' : '✗'} ${lab} — ${j.count} entr${j.count === 1 ? 'y' : 'ies'}`;
  } else {
    t = (j && j.ok) ? `✓ ${lab}` : `✗ ${lab} failed${j && j.error ? ': ' + j.error : ''}`;
  }
  $p('shvResult').textContent = t;
}
// In-progress + labelled result wrapper so every ShV button gives feedback.
async function shvDo(label, op, extra) { shvMsg(label + '…'); shvShow(await shv(op, extra), label); }
function parseShvEntries() {
  return $p('shvEntries').value.split('\n').map((l) => l.trim()).filter(Boolean).map((l) => {
    const [filament, numPulses, width] = l.split(',').map(Number);
    return { filament, numPulses, width };
  });
}

function wireEmission() {
  $p('emissionCard').innerHTML = EMI_HTML;
  // Capture: a single frame — peek the live ring if running, else a bounded shot.
  $p('wfCapture').onclick = () => (wfLive ? wfLiveTick() : wfCapture());
  $p('wfTrig').onclick = () => wfTrigCapture('fire');
  $p('wfTrigGp40').onclick = () => wfTrigCapture('gp40');
  $p('wfLive').onchange = (e) => wfSetLive(e.target.checked);
  $p('pulseStream').onclick = async () => {
    if (pulseTimer) {
      clearInterval(pulseTimer); pulseTimer = null; $p('pulseStream').classList.remove('danger');
      // If WE armed the STM32 (and Live doesn't own the ring), disarm it.
      if (pulseArmed && !wfLive) { await postJ('/api/adc/pulse-disarm', { controller: pwTarget }); pulseArmed = false; }
      return;
    }
    // The STM32 only emits EVT_PULSE while its ADC is armed. Use pulse-arm
    // (detector only, no ESP32 SPI ring read → no WiFi-load) unless Live already
    // owns the ring (which arms the STM32 anyway).
    if (!wfLive) {
      $p('pulseSummary').textContent = 'arming STM32 detector…';
      let j = await postJ('/api/adc/pulse-arm', { controller: pwTarget, rate: 1000000 });
      // A leftover ring (Live waveform / recorder) makes the detector-only arm
      // fail with 409 "ring running" — stop the ring and retry once.
      if (!j.ok && (j.status === 409 || /ring/i.test(`${j.message || ''} ${j.error || ''}`))) {
        await postJ('/api/adc/ring-stop', { controller: pwTarget });
        j = await postJ('/api/adc/pulse-arm', { controller: pwTarget, rate: 1000000 });
      }
      if (!j.ok) { $p('pulseSummary').textContent = `can't arm STM32 — ${j.error || j.message || 'arm failed'}`; return; }
      pulseArmed = true;
    }
    pulseTimer = setInterval(pulseTick, 500); $p('pulseStream').classList.add('danger'); pulseTick();
    if (!pulseList.length) $p('pulseSummary').textContent = 'armed — waiting for pulses…';
  };
  $p('pulseClear').onclick = () => { pulseList = []; pulseSince = 0; pulseFollow = true; renderPulses(); };
  $p('pulseSlider').addEventListener('input', () => {
    const sl = $p('pulseSlider');
    pulseFollow = (+sl.value >= +sl.max);   // dragged to the far right ⇒ resume live-follow
    renderPulses();
  });
  wireRecord();
  wireRingPulse();
  $p('shvPushList').onclick = () => shvDo('mapping pushed', 'push_active_list');
  $p('shvGetList').onclick = () => shvDo('read active list', 'get_active_list');
  $p('shvSetCfg').onclick = () => shvDo('config set', 'set_config', {
    interPulseMs: +$p('shvInter').value, maxOnMs: +$p('shvMaxOn').value, totalMs: +$p('shvTotal').value,
    triggerEdge: $p('shvTrigEdge').value === 'falling' ? 1 : 0 });
  $p('shvUpload').onclick = () => shvDo('uploaded', 'set_entries', { entries: parseShvEntries() });
  $p('shvClear').onclick = () => shvDo('table cleared', 'clear_table');
  $p('shvInfo').onclick = () => shvDo('table info', 'table_info');
  $p('shvHeatInfo').onclick = () => shvDo('heat info', 'heat_info');
  $p('shvArm').onclick = () => shvDo('armed', 'arm', { repeats: 1 });
  $p('shvDisarm').onclick = () => shvDo('disarmed', 'disarm');
  $p('shvStatus').onclick = () => shvDo('status', 'status');
  $p('shvLog').onclick = () => shvDo('pulse log', 'pulse_log', { start: 0 });
  // capability test (ShvCapabilityTest 0x7B) — FIRES HV
  $p('shvCapRun').onclick = () => {
    const pairs = $p('shvCapPairs').value.trim().split(/\s+/).filter(Boolean).map((s) => {
      const [bit, width] = s.split(',').map(Number); return { bit, width };
    });
    shvDo('capability test', 'capability', { channel: +$p('shvCapCh').value, pairs });
  };

  // ESP32 Sync I/O
  $p('syncConfig').onclick = async () => {
    const j = await postJ('/api/sync/config', { controller: pwTarget,
      sync_out_edge: $p('syncOutEdge').value, sync_out_width_us: +$p('syncWidth').value,
      ready_active: $p('syncReady').value, ext_trig_edge: $p('syncExtEdge').value });
    $p('syncStatus').textContent = j.ok ? 'config applied' : (j.error || 'config failed'); syncRefresh();
  };
  $p('syncFire').onclick = async () => {
    const j = await postJ('/api/sync/fire', { controller: pwTarget });
    $p('syncStatus').textContent = j.ok ? `fired SyncOut${j.message ? ' (' + j.message + ')' : ''}` : (j.error || j.message || 'fire failed');
  };
  $p('syncAbort').onclick = async () => { const j = await postJ('/api/sync/abort', { controller: pwTarget }); $p('syncStatus').textContent = j.ok ? 'aborted' : (j.error || 'abort failed'); };
  // poll the line status at 1 Hz only while the section is open
  let syncTimer = null;
  $p('syncDetails').addEventListener('toggle', (e) => {
    clearInterval(syncTimer); syncTimer = null;
    if (e.target.open) { syncRefresh(); syncTimer = setInterval(syncRefresh, 1000); }
  });
}

async function syncRefresh() {
  if (!boardTargetConnected()) { $p('syncLines').textContent = 'lines: not connected'; return; }
  let d; try { d = await (await fetch(`/api/sync/status?controller=${pwTarget}`)).json(); } catch { return; }
  const L = d.lines || d;
  $p('syncLines').textContent = d.ok === false ? `lines: ${d.error || '?'}`
    : `SyncOut ${L.sync_out} · SyncIn ${L.sync_in} · ReadyOut ${L.ready_out} · ReadyIn ${L.ready_in} · out ${d.sync_out_edge}/${d.sync_out_width_us}µs · ext ${d.ext_trig_edge}`;
}

// ---- target selector + gantt fold + lifecycle -------------------------------
export function initPower() {
  wireBoards();
  wireHv();
  wireEmission();

  // fold/unfold heating gantt — when opened, the canvas needs a (re)draw
  const gd = $p('ganttDetails');
  if (gd) gd.addEventListener('toggle', () => { if (gd.open && window.ctRedrawGantt) window.ctRedrawGantt(); });

  // target-controller selector
  document.querySelectorAll('#pwTargetSeg .seg-btn').forEach((b) =>
    b.addEventListener('click', () => selectController(+b.dataset.ctrl, b)));

  // poll connection state + board snapshot
  setInterval(pollPower, POLL_MS);
  pollPower();
}

// Controller switch: clear the matrix immediately (so the click visibly registers),
// bump boardGen to drop any in-flight Power-1 response, force the next periodic poll
// to be a full snapshot, then refresh the boards FIRST (awaited) so the matrix fills
// fast, before the heavier HV/TPS reads queue behind it on the single-client link.
async function selectController(n, btn) {
  if (n === pwTarget && btn && btn.classList.contains('active')) return;
  pwTarget = n;
  document.querySelectorAll('#pwTargetSeg .seg-btn').forEach((x) => x.classList.toggle('active', x === btn));
  hvTest = null;               // stale switch-test marks belong to the previous controller
  boardGen++;                  // supersede any in-flight refresh for the old controller
  boardCache = emptyBoards(); renderBoardGrid(); renderOneBoard();
  pollTick = 0;                // next periodic poll does a full snapshot
  updateTargetStatus();
  if (pollBusy) return;        // a sweep is running; it'll target the new controller on its next tick
  pollBusy = true;
  try {
    await refreshBoards(false);   // full snapshot first → matrix fills immediately
    await refreshHv(true);
    await readHvStatus(true);
    await readTpsRegs(true);
    await readStartupOcp();
  } catch { /* keep last */ } finally { pollBusy = false; }
}

let pollBusy = false;
let prevConn = false;
let pollTick = 0;
const POLL_PHASES = 5;
// True only when the matrix is actually on screen. When it's not (different tab /
// backgrounded), skip its reads so the single-client link stays free for the CT
// geometry live view. Robust check: checkVisibility() where available, else box size.
function powerPanelVisible() {
  if (document.hidden) return false;
  const el = document.getElementById('bmGrid');
  if (!el) return true;
  if (el.checkVisibility) return el.checkVisibility({ visibilityProperty: true, contentVisibilityAuto: true });
  const r = el.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}
// One poll per second. INA219 V/I is the live operational signal, so EVERY tick
// does the cheap V/I refresh and the matrix numbers stay current. The only other
// periodic reads are the presence/enable/fault snapshot (phase 0) and the HV grid
// (phase 2) — both change rarely and are spread onto their own ticks so no tick
// holds the link long enough to stall the V/I refresh. TPS OCP/delay/slew are
// debug config that don't change during a run, so they are NOT polled — they're
// read on demand only (selecting a board / switching controllers).
async function pollPower() {
  if (pollBusy) return;          // never overlap — reads serialize on the single link
  if (window.ctTestRunning) { updateTargetStatus(); return; }   // pause during a Cal & Test
  if (document.hidden) return;   // tab backgrounded → nothing to draw
  pollBusy = true;
  try {
    const st = await (await fetch('/api/status')).json();   // cheap: local connection flags, no bridge round-trip
    connectedSet = {};
    for (const [k, c] of Object.entries(st.controllers || {})) connectedSet[+k] = c.connected;
    updateTargetStatus();
    const conn = boardTargetConnected();
    if (!conn) { prevConn = false; return; }
    if (!prevConn) await readHvStatus(true);   // sync enable buttons ONCE on (re)connect
    prevConn = conn;
    if (!powerPanelVisible()) return;          // matrix off-screen → yield the link to geometry
    const phase = pollTick++ % POLL_PHASES;
    await refreshBoards(phase !== 0);          // phase 0 = full snapshot; every other tick = fast V/I merge
    if (phase === 2) await refreshHv();        // HV grid on its own tick (bits change on action, not continuously)
  } catch { /* keep last */ } finally { pollBusy = false; }
}

function updateTargetStatus() {
  const el = $p('pwTargetStatus');
  if (el) el.textContent = boardTargetConnected() ? `Power ${pwTarget} connected` : `Power ${pwTarget} — not connected`;
}
