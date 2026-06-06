/*
 * Compact on-canvas readout for the current (active) filament:
 * index, angle, V, I, R (=V/I), P (=V·I), accumulated mAs — packed in two
 * columns with a state-colored accent bar.
 */

import { filamentBaseAngle } from '../constants.js';

export function drawHud(r) {
  const ctx = r.ctx, s = r.state;
  const f = (s.filaments && s.filaments[s.activeFilament]) || { state: 1, voltage_mV: 0, current_mA: 0, mAs: 0 };
  const ang = filamentBaseAngle(s.activeFilament) + (s.gantryAngle || 0);
  const V = f.voltage_mV / 1000;                   // V
  const I = f.current_mA;                           // mA
  const R = I > 0.5 ? f.voltage_mV / I : null;      // mV/mA = Ω
  const P = (f.voltage_mV * f.current_mA) / 1e6;    // W
  const muted = 'rgba(150,170,182,0.6)';
  const neutral = 'rgba(195,208,214,0.95)';
  const sc = (s.stateColor && s.stateColor[f.state]) || '#888';

  const x = 10, y = 10, w = 150, pad = 8, lh = 14;
  const h = pad * 2 + lh * 4;
  ctx.fillStyle = 'rgba(8,13,18,0.74)';
  r._rrect(x, y, w, h, 7); ctx.fill();
  ctx.strokeStyle = 'rgba(120,150,162,0.28)'; ctx.lineWidth = 1; ctx.stroke();
  ctx.fillStyle = sc; r._rrect(x, y, 3, h, 1.5); ctx.fill();

  ctx.textBaseline = 'middle';
  const colL = x + pad, colR = x + pad + 74;
  let yy = y + pad + lh / 2;

  // label + value, packed tight (value sits right after the label)
  const kv = (cx, label, val, col) => {
    ctx.font = '10.5px var(--mono, monospace)';
    ctx.textAlign = 'left'; ctx.fillStyle = muted;
    ctx.fillText(label, cx, yy);
    const lw = ctx.measureText(label).width;
    ctx.fillStyle = col; ctx.fillText(val, cx + lw + 5, yy);
  };

  ctx.font = '700 10.5px var(--mono, monospace)';
  ctx.textAlign = 'left'; ctx.fillStyle = sc;
  ctx.fillText(`FIL ${s.activeFilament}`, colL, yy);
  kv(colR, 'θ', ang.toFixed(1) + '°', neutral); yy += lh;
  kv(colL, 'V', V.toFixed(2) + ' V', '#5fd0bb');
  kv(colR, 'I', I.toFixed(0) + ' mA', '#f2c14e'); yy += lh;
  kv(colL, 'R', R == null ? '—' : R.toFixed(1) + ' Ω', neutral);
  kv(colR, 'P', P.toFixed(2) + ' W', neutral); yy += lh;
  kv(colL, 'mAs', f.mAs.toFixed(3), '#c48cff');
}
