/*
 * Direct power control — boards matrix (and, later, HV grid + ShV schedule).
 *
 * All commands target ONE selected controller (the "target" selector under the
 * divider). Board/HV/TPS/INA commands are built server-side by the WiFi GUI's
 * build_command_payload and proxied via POST /api/power-cmd {controller, command, …}.
 */

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
const POLL_MS = 2000;                    // board snapshot + HV grid refresh period
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
const STATE_IDLE = 4, STATE_ACTIVE = 5, STATE_VOLTAGE = 6;
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
const boardSel = new Set(['0.0']);      // multi-select "ch.mux" keys
const bKey = (b) => `${b.channel}.${b.mux_port}`;
const keyTo = (k) => { const [c, m] = k.split('.').map(Number); return { channel: c, mux_port: m }; };

function boardMask() {
  const mask = [0, 0, 0, 0, 0, 0, 0, 0];
  for (const k of boardSel) { const { channel, mux_port } = keyTo(k); mask[channel] |= (1 << mux_port); }
  return mask;
}

const BOARDS_HTML = `
  <div class="legend bm-legend">
    <span class="legend-item"><span class="dot on">P</span> present</span>
    <span class="legend-item"><span class="dot on">I</span> ISO</span>
    <span class="legend-item"><span class="dot on">T</span> TPS</span>
    <span class="legend-item"><span class="dot fault">F</span> fault</span>
    <span class="legend-item"><span class="hv-badge on">HV</span> HV current</span>
    <span class="legend-item"><span class="dot absent">·</span> absent</span>
    <span class="hint">Shift/Ctrl-click = multi-select</span>
  </div>
  <div class="row compact selection-toolbar">
    <span id="bmSelSummary" class="summary">1 selected</span>
    <button id="bmSelPresent" class="xs">All present</button>
    <button id="bmSelAll" class="xs">All 64</button>
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
      + (b.channel === boardPrimary.channel && b.mux_port === boardPrimary.mux_port ? ' active' : '');
    const dot = (on, ch, fa) => `<span class="dot ${fa ? 'fault' : on ? 'on' : 'off'}">${ch}</span>`;
    const hv = `<span class="hv-badge ${b.hv_overcurrent ? 'on' : 'off'}" title="HV current ${b.hv_overcurrent ? 'sensed (>1 mA)' : 'none'}">HV</span>`;
    tile.innerHTML = hv
      + `<span class="tile-title">${b.label}</span>`
      + `<span class="tile-dots">${dot(b.present, 'P')}${dot(b.iso_enabled, 'I')}${dot(b.tps_enabled, 'T', b.tps_fault)}${dot(b.tps_fault, 'F', b.tps_fault)}</span>`
      + `<span class="tile-measure">${b.present ? (b.bus_mV / 1000).toFixed(2) + 'V ' + b.current_mA + 'mA' : '—'}</span>`;
    tile.addEventListener('click', (e) => {
      if (e.shiftKey || e.ctrlKey || e.metaKey) {
        if (boardSel.has(k)) boardSel.delete(k); else boardSel.add(k);
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
async function readTpsRegs() {
  if (!boardTargetConnected()) { for (const id of ['bmOcpRead', 'bmDelayRead', 'bmSlewRead']) $p(id).textContent = '—'; return; }
  for (const id of ['bmOcpRead', 'bmDelayRead', 'bmSlewRead']) { const e = $p(id); if (e) e.textContent = '…'; }
  const t = single();
  const ocp = await powerCmd('CH_READ_TPS_REGISTER', { ...t, reg: 0x02, width_bytes: 1 });
  const sr = await powerCmd('CH_READ_TPS_REGISTER', { ...t, reg: 0x03, width_bytes: 1 });
  const ov = ((ocp.response && ocp.response.decoded && ocp.response.decoded.value) || 0) & 0xFF;
  const sv = ((sr.response && sr.response.decoded && sr.response.decoded.value) || 0) & 0xFF;
  const ocpEnabled = !!(ov & 0x80);
  $p('bmOcpRead').textContent = ocpEnabled ? Math.round((ov & 0x7F) * OCP_MA_PER_CODE) + ' mA' : 'off';
  $p('bmDelayRead').textContent = OCP_DELAY_LABELS[(sv >> 4) & 0x03];
  $p('bmSlewRead').textContent = SLEW_LABELS[sv & 0x03] + ' mV/µs';
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

async function refreshBoards() {
  const conn = boardTargetConnected();
  if (!conn) { boardCache = emptyBoards(); renderBoardGrid(); renderOneBoard(); bmMsg(`Power ${pwTarget} not connected.`); return; }
  let j;
  try { j = await (await fetch(`/api/board-snapshot?controller=${pwTarget}`)).json(); } catch { return; }
  if (!j.ok) { boardCache = emptyBoards(); renderBoardGrid(); bmMsg(j.error || 'snapshot failed'); return; }
  boardCache = (j.boards && j.boards.length) ? j.boards : emptyBoards();
  renderBoardGrid(); renderOneBoard();
  bmMsg(`Power ${pwTarget} — ${boardCache.filter((b) => b.present).length}/64 present · INA219 refresh ${REFRESH_S}.`);
}

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
  $p('bmSelPresent').onclick = () => { boardSel.clear(); boardCache.forEach((b) => b.present && boardSel.add(bKey(b))); renderBoardGrid(); };
  $p('bmSelAll').onclick = () => { boardSel.clear(); for (let c = 0; c < 8; c++) for (let m = 0; m < 8; m++) boardSel.add(`${c}.${m}`); renderBoardGrid(); };
  $p('bmSelClear').onclick = () => { boardSel.clear(); renderBoardGrid(); };
  const run = async (p) => { const j = await p; bmMsg(j.ok ? 'ok' : (j.error || 'failed')); refreshBoards(); };
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
    refreshBoards();
  };
  reflectStateArg();
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
const hvBit = (m, ch, b) => (m[ch] >> b) & 1;

const HV_HTML = `
  <div class="batch-box">
    <div class="block-title">HV grid <button id="hvMonitor" class="xs">Monitor: ON</button> <span class="hint">click=toggle · shift/ctrl=multi</span></div>
    <div class="legend bm-legend">
      <span class="legend-item"><span class="dot on">1</span> on+verified</span>
      <span class="legend-item"><span class="dot off">0</span> off</span>
      <span class="legend-item"><span class="dot fault">!</span> mismatch</span>
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
    <div class="block-title">HV setpoints <span class="hint">— closed-loop voltage</span></div>
    <div class="bm-set-row hv-set" title="Enter the magnitude — 70 sets −70 V (the output is negative).">
      <label class="numlabel"><span class="cap">Emission −V</span><input id="emVset" type="number" min="0" max="350" value="0" /></label>
      <span id="emVmeas" class="pot-est">—</span>
      <button id="emVsetBtn" class="xs">Set</button>
      <button id="emVclrBtn" class="xs">Clr</button>
    </div>
    <div class="bm-set-row hv-set" title="Enter the magnitude — 70 sets −70 V (the output is negative).">
      <label class="numlabel"><span class="cap">Focus −V</span><input id="focVset" type="number" min="0" max="495" value="0" /></label>
      <span id="focVmeas" class="pot-est">—</span>
      <button id="focVsetBtn" class="xs">Set</button>
      <button id="focVclrBtn" class="xs">Clr</button>
    </div>
    <div class="bm-set-row hv-set">
      <label class="numlabel"><span class="cap">Emission I</span><input id="dsEi" type="number" min="0" max="127" value="0" /></label>
      <span id="dsEiEst" class="pot-est">~0 mA</span>
      <button id="dsSet" class="xs">Set I</button>
      <button id="dsRead" class="xs">Read</button>
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
  </div>`;

function renderHvGrid() {
  const grid = $p('hvGrid'); if (!grid) return;
  grid.innerHTML = '';
  let on = 0, mm = 0;
  for (let ch = 0; ch < 8; ch++) for (let b = 0; b < 8; b++) {
    const d = hvBit(hvDesired, ch, b), f = hvBit(hvFeedback, ch, b), mis = d !== f;
    if (d) on++; if (mis) mm++;
    const k = `${ch}.${b}`;
    const tile = document.createElement('div');
    tile.className = 'status-tile ' + (mis ? 'fault' : d ? 'present' : 'absent') + (hvSel.has(k) ? ' selected' : '');
    tile.innerHTML = `<span class="tile-title">C${ch + 1}.${b + 1}</span><span class="hv-state">${mis ? '!' : d}</span>`;
    tile.title = `CH${ch + 1} bit ${b + 1} — desired ${d}, feedback ${f}`;
    tile.addEventListener('click', (e) => {
      if (e.shiftKey || e.ctrlKey || e.metaKey) { hvSel.has(k) ? hvSel.delete(k) : hvSel.add(k); renderHvGrid(); }
      else { hvSetBit(ch, b, !d); }
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

function wireHv() {
  $p('hvCard').innerHTML = HV_HTML;
  renderHvGrid();
  $p('hvSelOn').onclick = () => hvSelSet(true);
  $p('hvSelOff').onclick = () => hvSelSet(false);
  $p('hvAllOff').onclick = async () => { for (let c = 0; c < 8; c++) for (let b = 0; b < 8; b++) if (hvBit(hvDesired, c, b)) await powerCmd('HV_SET_BIT', { channel: c, bit: b, value: false, verify: true }); refreshHv(); };
  $p('hvRefresh').onclick = () => refreshHv(true);
  $p('hvMonitor').onclick = () => {
    hvMonitorOn = !hvMonitorOn;
    $p('hvMonitor').textContent = `Monitor: ${hvMonitorOn ? 'ON' : 'OFF'}`;
    $p('hvMonitor').classList.toggle('off', !hvMonitorOn);
    if (hvMonitorOn) refreshHv(true);
  };
  $p('hvPulse').onclick = async () => { const j = await powerCmd('HV_PULSE', { hv_mask: hvSelMask(), width_us: +$p('hvPulseUs').value, verify_mode: 0 }); $p('hvStatus').textContent = j.ok ? 'pulse fired' : (j.error || 'pulse failed'); refreshHv(); };
  // Closed-loop voltage: Emission/Focus target entered in volts → ADS counts.
  $p('emVsetBtn').onclick = () => hvSetTarget('emission', 'emVset');
  $p('emVclrBtn').onclick = () => hvClearTarget('emission');
  $p('focVsetBtn').onclick = () => hvSetTarget('focus', 'focVset');
  $p('focVclrBtn').onclick = () => hvClearTarget('focus');
  // Emission I — DS3502 wiper (no closed-loop for current)
  $p('dsEi').addEventListener('input', updatePotEsts);
  updatePotEsts();
  $p('dsSet').onclick = async () => {
    if (!hvConnGuard()) return;
    hvFb('Em-I: setting …');
    const j = await postJ('/api/stm32/ds3502-set', { controller: masterId(), ch: 'ei', wiper: +$p('dsEi').value });
    hvFb(j.ok ? `Em-I wiper ${+$p('dsEi').value} set` : `Em-I ${hvErr(j)}`, j.ok ? 'ok' : 'bad');
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
  // Enable buttons = the COMMANDED state. Click flips it instantly (one click),
  // and we only undo the flip if the command itself fails. The ACTUAL pin state
  // is shown separately in the monitor's "Emission HV / Focus HV" tiles, so the
  // button is never yanked back under you by the background poll. readHvStatus()
  // syncs the button to reality once, on (re)connect / target switch.
  const hvEnClick = async (chan, id, label) => {
    if (!hvConnGuard()) return;
    const next = $p(id).dataset.on !== '1';
    setHvEnBtn(id, label, next);               // flip immediately — no waiting for the round-trip
    hvFb(`${label}: turning ${next ? 'ON' : 'OFF'} …`);
    const j = await postJ('/api/stm32/hv-enable', { controller: masterId(), ch: chan, on: next });
    if (!j.ok) setHvEnBtn(id, label, !next);   // command failed → undo the flip
    hvFb(j.ok ? `${label} ${next ? 'ON' : 'OFF'}` : `${label} ${hvErr(j)}`, j.ok ? 'ok' : 'bad');
  };
  $p('hvEnEm').onclick = () => hvEnClick('emission', 'hvEnEm', 'Emission');
  $p('hvEnFoc').onclick = () => hvEnClick('focus', 'hvEnFoc', 'Focus');
  setHvEnBtn('hvEnEm', 'Emission', false);   // start OFF (boot-safe)
  setHvEnBtn('hvEnFoc', 'Focus', false);
  // ADS1115 monitor + DS3502 wiper readout — same 2 Hz auto-poll, on by default
  let adsTimer = null;
  const tick = () => { adsRead(); pollHvLoop(); };
  const adsAutoApply = (on) => { clearInterval(adsTimer); adsTimer = on ? setInterval(tick, 500) : null; };
  $p('adsRead').onclick = tick;
  $p('adsAuto').onchange = (e) => adsAutoApply(e.target.checked);
  adsAutoApply($p('adsAuto').checked);
}
// DS3502 wiper (0–127) → estimated HV output (HV design doc / ds3502 scaling memo).
const POT_EST = { ev: { full: -350, unit: 'V' }, ei: { full: 85.7, unit: 'mA' }, fv: { full: -495, unit: 'V' } };
function potEstText(ch, wiper) {
  const s = POT_EST[ch];
  const w = Number.isFinite(wiper) ? Math.min(127, Math.max(0, wiper)) : 0;
  return `~${((w / 127) * s.full).toFixed(s.unit === 'mA' ? 1 : 0)} ${s.unit}`;
}
function updatePotEsts() { $p('dsEiEst').textContent = potEstText('ei', +$p('dsEi').value); }

// closed-loop voltage target: volts → ADS1115 counts (PGA ±6.144 V → 0.1875
// mV/count; ADS scaling: emission 5000 mV → −350 V, focus 5000 mV → −1000 V).
const ADS_MV_PER_COUNT = 6144 / 32768;
const HV_ADS_FULL_V = { emission: 350, focus: 1000 };   // |HV| at ADS 5000 mV
// input is the magnitude (70 → −70 V); ADS count is always positive.
const hvVoltToCount = (chan, mag) => Math.round((Math.abs(mag) / HV_ADS_FULL_V[chan] * 5000) / ADS_MV_PER_COUNT);
// inverse: ADS counts → signed HV volts (negative), matching config_portal's scaling.
const countsToVolts = (chan, counts) => Math.round(-(counts * ADS_MV_PER_COUNT) / 5000 * HV_ADS_FULL_V[chan]);
// prominent, immediate feedback for every HV-setpoint action
function hvFb(msg, kind) { const e = $p('dsStatus'); if (e) { e.textContent = msg; e.className = 'summary hv-fb ' + (kind || ''); } }
function hvConnGuard() {
  if (masterConnected()) return true;
  hvFb(`Master (Power ${masterId()}) not connected — Scan & Connect it first.`, 'bad'); return false;
}
const hvErr = (j) => `${j.error || j.message || 'failed'}${j.status ? ' [HTTP ' + j.status + ']' : ''}`;

async function hvSetTarget(chan, inputId) {
  if (!hvConnGuard()) return;
  const mag = Math.abs(+$p(inputId).value);
  const target = hvVoltToCount(chan, mag);
  hvFb(`${chan}: setting −${mag} V …`);
  const j = await postJ('/api/stm32/hv-set-target', { controller: masterId(), chan, target, tol: 8, max_step: 2 });
  // The command only ARMS the loop; the STM32 then walks the wiper toward target
  // over time. pollHvLoop() (2 Hz) shows the live convergence / at-target /
  // unreachable state in emVmeas/focVmeas — kick it once now for immediacy.
  hvFb(j.ok ? `${chan} closed-loop → −${mag} V (${target} cnt)` : `set-target ${hvErr(j)}`, j.ok ? 'ok' : 'bad');
  if (j.ok) pollHvLoop();
}
async function hvClearTarget(chan) {
  if (!hvConnGuard()) return;
  hvFb(`${chan}: clearing …`);
  const j = await postJ('/api/stm32/hv-clear-target', { controller: masterId(), chan });
  // Loop goes inactive; pollHvLoop() will reflect "loop off". Wiper stays put.
  hvFb(j.ok ? `${chan} closed-loop off` : `clear ${hvErr(j)}`, j.ok ? 'ok' : 'bad');
  if (j.ok) pollHvLoop();
}
// target voltage → DS3502 wiper (the closed-loop reference the HV module regulates to)
const vToWiper = (ch, v) => Math.min(127, Math.max(0, Math.round((v / POT_EST[ch].full) * 127)));
const wiperToV = (ch, w) => Math.round((w / 127) * POT_EST[ch].full);

let adsBusy = false;
// Blank every monitor field. Called on any failed read so stale HV numbers are
// never left on screen looking live — a safety monitor must read '—', not lie.
function blankAds(msg, kind) {
  for (const id of ['adsRef', 'adsFocus', 'adsEmI', 'adsEmV']) { const e = $p(id); if (e) e.textContent = '—'; }
  for (const id of ['hvEmStatus', 'hvFocStatus']) { const e = $p(id); if (e) { e.textContent = '—'; e.className = 'metric-value'; } }
  const s = $p('adsStatus'); if (s) { s.textContent = msg; s.className = 'summary ' + (kind || ''); }
}
async function adsRead() {
  if (adsBusy || !masterConnected()) return;   // never overlap / flood the bridge (STM32 on master)
  adsBusy = true;
  try {
    const j = await (await fetch(`/api/stm32/ads1115?controller=${masterId()}`)).json();
    if (!j.ok) { blankAds(j.error || 'read failed', 'bad'); return; }
    const f = (v, u) => (v == null ? '—' : (+v).toFixed(2) + u);
    $p('adsRef').textContent = f(j.ref_mv, ' mV');
    $p('adsFocus').textContent = f(j.focus_v, ' V');
    $p('adsEmI').textContent = f(j.emiss_i_ma, ' mA');
    $p('adsEmV').textContent = f(j.emiss_v, ' V');
    // actual HV pin status from the ADS1115 safety flags (bit3=emission, bit4=focus)
    const fl = j.flags || 0;
    const stat = (id, on) => { const e = $p(id); if (e) { e.textContent = on ? 'ON' : 'OFF'; e.className = 'metric-value ' + (on ? 'st-on' : 'st-off'); } };
    stat('hvEmStatus', !!(fl & 0x08));
    stat('hvFocStatus', !!(fl & 0x10));
    // name which safety flag tripped instead of a vague "alert/diag"
    const warn = [j.ads1115_alert && 'ADS alert', j.amc3301_diag && 'AMC diag'].filter(Boolean).join(' · ');
    const s = $p('adsStatus'); if (s) { s.textContent = warn ? '⚠ ' + warn : 'ok'; s.className = 'summary' + (warn ? ' bad' : ''); }
  } catch (e) {
    blankAds('monitor read error — link down?', 'bad');   // surface it; don't silently keep stale values
  } finally { adsBusy = false; }
}

// Live closed-loop status for the two voltage channels — same 2 Hz cadence as
// the ADS monitor. Grounded in the STM32 hv_control state machine (active /
// at_target / hw_limit, plus the wiper it has written and the target). NOTE: the
// actual output voltage is the ADS monitor (adsEmV / adsFocus) — the wiper alone
// does NOT equal the output. A wound-up wiper of 127 with HV off reads ~0 V, not
// −350 V, which is why showing "~−350 V" from the wiper was wrong.
let loopBusy = false;
async function getTarget(chan) {
  try { const r = await (await fetch(`/api/stm32/hv-target?controller=${masterId()}&chan=${chan}`)).json(); return r.ok ? r : null; } catch { return null; }
}
// Compact readout (must fit inline before the Set/Clr buttons) + full detail in
// the hover tooltip. JSON fields (config_portal /stm32/hv_get_target): target,
// last_adc, wiper, active, at_target, hw_limit. `wiper`/`hw_limit` need the
// updated ESP32 firmware — if absent the device still runs the old build.
function hvLoopText(chan, t) {
  if (!t) return { txt: '—', title: '' };
  if (t.wiper == null) return { txt: '⚠ old fw', title: 'Reflash the ESP32 — old firmware: no wiper field and off-by-one flag parsing.' };
  const v = countsToVolts(chan, t.target), w = t.wiper;
  if (t.hw_limit)  return { txt: `⚠ w${w} unreach`, title: `Target ${v} V unreachable — wiper saturated at ${w}, loop auto-disabled. Enable HV, then set.` };
  if (!t.active)   return { txt: `off w${w}`,       title: `Closed loop off. Wiper at ${w}.` };
  if (t.at_target) return { txt: `✓ ${v}V w${w}`,   title: `At target ${v} V (wiper ${w}).` };
  return { txt: `${v}V w${w}…`, title: `Converging to ${v} V (wiper ${w})…` };
}
async function pollHvLoop() {
  if (loopBusy) return;
  const set = (id, r) => { const e = $p(id); if (e) { e.textContent = r.txt; e.title = r.title; } };
  if (!masterConnected()) { set('emVmeas', { txt: '—', title: '' }); set('focVmeas', { txt: '—', title: '' }); return; }
  loopBusy = true;
  try {
    const [em, fo] = await Promise.all([getTarget('emission'), getTarget('focus')]);
    set('emVmeas', hvLoopText('emission', em));
    set('focVmeas', hvLoopText('focus', fo));
  } finally { loopBusy = false; }
}

// Emission/Focus enable buttons reflect the actual pin state (like the HV grid).
function setHvEnBtn(id, label, on) {
  const b = $p(id); if (!b) return;
  b.dataset.on = on ? '1' : '0';
  // color = state: red = energized, green = off/safe, gray = unknown. Click toggles.
  b.innerHTML = `<span class="hv-en-dot">●</span> ${label}`;
  b.className = 'xs hv-en ' + (on == null ? 'unk' : on ? 'on' : 'off');
  b.title = on == null ? `${label}: unknown` : `${label}: ${on ? 'ON (energized)' : 'OFF'} — click to toggle`;
}
async function readHvStatus() {
  if (!masterConnected()) { setHvEnBtn('hvEnEm', 'Emission', null); setHvEnBtn('hvEnFoc', 'Focus', null); return; }
  let j;
  try { j = await (await fetch(`/api/stm32/hv-status?controller=${masterId()}`)).json(); }
  catch { setHvEnBtn('hvEnEm', 'Emission', null); setHvEnBtn('hvEnFoc', 'Focus', null); return; }
  setHvEnBtn('hvEnEm', 'Emission', j.ok ? j.emission_on : null);
  setHvEnBtn('hvEnFoc', 'Focus', j.ok ? j.focus_on : null);
}

// ---- Emission & Schedule card: waveform + per-pulse + ShV -------------------
const EMI_HTML = `
  <div class="batch-box">
    <div class="block-title">Emission current waveform <span class="hint">— STM32 ADC (SPI ring)</span></div>
    <div class="row wrap">
      <label class="numlabel">samples <input id="wfN" type="number" min="64" max="32768" value="2048" /></label>
      <label class="numlabel">kSPS <input id="wfRate" type="number" min="1" max="1000" value="1000" /></label>
      <button id="wfCapture" class="xs">Capture</button>
      <button id="wfTrig" class="xs" title="Continuous → trigger → retrieve: fire SyncOut and pull the full-rate window around the trigger from the running ring. Requires Live.">Trig ▶</button>
      <label class="chk" title="Continuous rolling capture from the STM32 ADC ring. While live the same stream also feeds per-pulse measurements."><input id="wfLive" type="checkbox" /> Live</label>
    </div>
    <canvas id="wfCanvas" class="adc-plot" width="600" height="160"></canvas>
    <div id="wfStatus" class="summary">no capture yet</div>
  </div>

  <div class="batch-box">
    <div class="block-title">Per-pulse measurements <span class="hint">— STM32 1 MSPS · events flow while Live (or a Capture) runs</span></div>
    <div class="row wrap">
      <button id="pulseStream" class="xs">Stream</button>
      <button id="pulseClear" class="xs">Clear</button>
      <span id="pulseSummary" class="hint">no events</span>
    </div>
    <div class="pulse-wrap"><table class="pulse-table"><thead><tr><th>#</th><th>t µs</th><th>ON µs</th><th>peak</th><th>bg±σ</th><th>∫</th></tr></thead><tbody id="pulseBody"></tbody></table></div>
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

let pulseList = [], pulseSince = 0, pulseTimer = null, wfTimer = null;

function drawWaveform(samples, rate, trigIdx) {
  const c = $p('wfCanvas'); if (!c || !samples.length) return;
  const ctx = c.getContext('2d'), W = c.width, H = c.height;
  ctx.clearRect(0, 0, W, H); ctx.fillStyle = '#070b0e'; ctx.fillRect(0, 0, W, H);
  let lo = Infinity, hi = -Infinity;
  for (const s of samples) { if (s < lo) lo = s; if (s > hi) hi = s; }
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
async function wfTrigCapture() {
  if (wfBusy) return;
  if (!wfLive) { $p('wfStatus').textContent = 'turn Live on first (the ring must be running)'; return; }
  wfBusy = true;
  try {
    // Window must fit the ring (pre+post+1 ≤ cap); clamp the requested count.
    const total = Math.min(+$p('wfN').value, WF_RING_CAP - 1);
    const pre = Math.max(0, Math.round(total * 0.25)), post = Math.max(1, total - pre);
    const clamp = (+$p('wfN').value > WF_RING_CAP - 1) ? ' (clamped to ring)' : '';
    $p('wfStatus').textContent = `triggering — pre ${pre} / post ${post}${clamp}…`;
    const j = await postJ('/api/adc/ring-window', { controller: pwTarget, pre, post, fire: true });
    if (!j.ok) { $p('wfStatus').textContent = `trigger ${j.error || j.message || 'failed'}`; return; }
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
function renderPulses() {
  const body = $p('pulseBody'); if (!body) return;
  body.innerHTML = pulseList.slice(-50).map((p) =>
    `<tr><td>${p.id}</td><td>${p.t_us}</td><td>${p.on_us}</td><td>${p.peak}</td><td>${p.bg}±${(p.bg_sigma4 / 4).toFixed(0)}</td><td>${p.integral}</td></tr>`).join('');
  $p('pulseSummary').textContent = `${pulseList.length} events`;
}
async function pulseTick() {
  if (!masterConnected()) return;
  let j; try { j = await (await fetch(`/api/pulse-events?controller=${masterId()}&since=${pulseSince}`)).json(); } catch { return; }
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

function shvShow(j) {
  let t;
  if (j && j.status) {
    const s = j.status;
    t = `state ${nm(SHV_STATE, s.state)} · stop ${nm(SHV_STOP, s.stopReason)}\n`
      + `entry ${s.entryIndex}/${s.entryCount} · filament ${s.filamentIndex === 255 ? '—' : s.filamentIndex} · pulses ${s.totalPulsesDone}/${s.totalPulsesTarget}\n`
      + `elapsed ${s.elapsedMs} ms` + (s.faultFilament !== 255 ? ` · fault fil ${s.faultFilament}` : '');
  } else if (j && j.reject != null && j.results) {            // capability test (0xFFFF = 165 verify mismatch)
    t = `reject: ${nm(SHV_REJECT, j.reject)}\n` + j.results.map((r) => `  bit ${r.bit}: ${r.measuredUs === 0xFFFF ? 'verify MISMATCH' : 'measured ' + r.measuredUs + ' µs'}`).join('\n');
  } else if (j && j.reject != null) {                          // arm
    t = `${j.ok ? '✓ armed' : '✗'} — reject: ${nm(SHV_REJECT, j.reject)}`;
  } else if (j && j.records) {                                 // pulse log
    t = `${j.records.length}/${j.total} records\n` + j.records.map((r) => `  fil ${r.filament} seq ${r.seq} t ${r.tOnUs}µs dur ${r.durationUs}µs${r.flags ? ' flags 0x' + r.flags.toString(16) : ''}`).join('\n');
  } else if (j && j.entryCount != null) {                      // table info
    t = `entries ${j.entryCount} · crc 0x${(j.crc >>> 0).toString(16).toUpperCase()}`;
  } else if (j && j.interPulseMs != null) {                    // get config
    t = `inter ${j.interPulseMs} ms · maxOn ${j.maxOnMs} ms · total ${j.totalMs} ms · edge ${j.triggerEdge ? 'falling' : 'rising'}`;
  } else if (j && j.mapped != null) {                          // active-list read
    t = `active list: ${j.mapped}/64 power slots mapped`;
  } else {
    t = j && j.ok ? 'ok' : (j && j.error) ? j.error : JSON.stringify(j);
  }
  $p('shvResult').textContent = t;
}
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
  $p('wfTrig').onclick = wfTrigCapture;
  $p('wfLive').onchange = (e) => wfSetLive(e.target.checked);
  $p('pulseStream').onclick = () => {
    if (pulseTimer) { clearInterval(pulseTimer); pulseTimer = null; $p('pulseStream').classList.remove('danger'); }
    else { pulseTimer = setInterval(pulseTick, 500); $p('pulseStream').classList.add('danger'); pulseTick(); }
  };
  $p('pulseClear').onclick = () => { pulseList = []; pulseSince = 0; renderPulses(); };
  wireRecord();
  $p('shvPushList').onclick = async () => shvShow(await shv('push_active_list'));
  $p('shvGetList').onclick = async () => shvShow(await shv('get_active_list'));
  $p('shvSetCfg').onclick = async () => shvShow(await shv('set_config', { interPulseMs: +$p('shvInter').value, maxOnMs: +$p('shvMaxOn').value, totalMs: +$p('shvTotal').value, triggerEdge: 0 }));
  $p('shvUpload').onclick = async () => shvShow(await shv('set_entries', { entries: parseShvEntries() }));
  $p('shvClear').onclick = async () => shvShow(await shv('clear_table'));
  $p('shvInfo').onclick = async () => shvShow(await shv('table_info'));
  $p('shvArm').onclick = async () => shvShow(await shv('arm', { repeats: 1 }));
  $p('shvDisarm').onclick = async () => shvShow(await shv('disarm'));
  $p('shvStatus').onclick = async () => shvShow(await shv('status'));
  $p('shvLog').onclick = async () => shvShow(await shv('pulse_log', { start: 0 }));
  // capability test (ShvCapabilityTest 0x7B) — FIRES HV
  $p('shvCapRun').onclick = async () => {
    const pairs = $p('shvCapPairs').value.trim().split(/\s+/).filter(Boolean).map((s) => {
      const [bit, width] = s.split(',').map(Number); return { bit, width };
    });
    shvShow(await shv('capability', { channel: +$p('shvCapCh').value, pairs }));
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
    b.addEventListener('click', () => {
      pwTarget = +b.dataset.ctrl;
      document.querySelectorAll('#pwTargetSeg .seg-btn').forEach((x) => x.classList.toggle('active', x === b));
      updateTargetStatus(); refreshBoards(); refreshHv(); readHvStatus(); readTpsRegs(); readStartupOcp();
    }));

  // poll connection state + board snapshot
  setInterval(pollPower, POLL_MS);
  pollPower();
}

let pollBusy = false;
let prevConn = false;
async function pollPower() {
  if (pollBusy) return;          // never overlap — the board sweep is slow
  pollBusy = true;
  try {
    const st = await (await fetch('/api/status')).json();
    connectedSet = {};
    for (const [k, c] of Object.entries(st.controllers || {})) connectedSet[+k] = c.connected;
    updateTargetStatus();
    await refreshBoards();
    await refreshHv();
    // Sync the enable buttons to the real pin state ONCE on (re)connect — NOT
    // every poll, which would fight the user's click and need a second press.
    const conn = boardTargetConnected();
    if (conn && !prevConn) await readHvStatus();
    prevConn = conn;
  } catch { /* keep last */ } finally { pollBusy = false; }
}

function updateTargetStatus() {
  const el = $p('pwTargetStatus');
  if (el) el.textContent = boardTargetConnected() ? `Power ${pwTarget} connected` : `Power ${pwTarget} — not connected`;
}
