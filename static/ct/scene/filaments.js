/*
 * Filament-ring spectrum.
 *
 * Per filament: a state-colored marker on the source ring (color = firmware
 * PowerState) plus two outward bands — inner mAs (violet), outer V+I (teal/
 * amber). Dim baseline stubs keep the ring reading full. Index labels every 24
 * filaments (0/24/48/72); the "indices" option adds one every 4. No band text
 * labels (the colors are explained in the page legend).
 */

import { CT, D2R, clamp01 } from '../constants.js';

const LABEL_EVERY = CT.N_FILAMENTS / 4; // 24

export function drawFilaments(r) {
  const s = r.state, ctx = r.ctx;
  const covered = new Set(r._windowIndices());
  const fil = s.filaments || [];
  const { masBase, masTop, viBase, viTop } = r._bands();

  // band guide rings (baseline + full-scale)
  r._ring(masBase, 'rgba(154,110,210,0.20)', 1);
  r._ring(masTop, 'rgba(154,110,210,0.07)', 1);
  r._ring(viBase, 'rgba(120,150,162,0.22)', 1);
  r._ring(viTop, 'rgba(120,150,162,0.07)', 1);

  const offV = -3.1, offI = 3.1, hw = 2.0, hwM = 2.5;

  for (let i = 0; i < CT.N_FILAMENTS; i++) {
    const f = fil[i] || { state: 1, voltage_mV: 0, current_mA: 0, mAs: 0 };
    const a = r._filamentAngle(i); // rocks with the gantry
    const isActive = i === s.activeFilament;
    const isHover = i === s.hover;

    // dead/disabled filament: a hollow gray ✕ on the ring, no bars
    if (f.dead) {
      const p = r._filamentPos(i), sx = r._x(p.x), sy = r._y(p.y), d = 3;
      ctx.strokeStyle = 'rgba(120,130,138,0.85)'; ctx.lineWidth = 1.3;
      ctx.beginPath();
      ctx.moveTo(sx - d, sy - d); ctx.lineTo(sx + d, sy + d);
      ctx.moveTo(sx + d, sy - d); ctx.lineTo(sx - d, sy + d);
      ctx.stroke();
      if (isHover) { ctx.strokeStyle = 'rgba(255,255,255,0.5)'; r._annulusStroke(masBase, a - CT.STEP_DEG / 2, a + CT.STEP_DEG / 2); }
      continue;
    }

    // dim baseline stubs so the ring always reads full
    r._radialBar(a, viBase, viBase + 2, hw, offV, 'rgba(120,150,162,0.16)');
    r._radialBar(a, viBase, viBase + 2, hw, offI, 'rgba(120,150,162,0.16)');
    r._radialBar(a, masBase, masBase + 2, hwM, 0, 'rgba(154,110,210,0.16)');

    // value bars (grow outward)
    const vFrac = clamp01(f.voltage_mV / s.vMax);
    const iFrac = clamp01(f.current_mA / s.iMax);
    const mFrac = clamp01(f.mAs / s.mAsMax);
    const glowV = isActive ? 'rgba(63,182,160,0.9)' : null;
    const glowI = isActive ? 'rgba(242,193,78,0.9)' : null;
    if (vFrac > 0.004) r._radialBar(a, viBase, viBase + CT.VI_LEN * vFrac, hw, offV, '#3fb6a0', glowV);
    if (iFrac > 0.004) r._radialBar(a, viBase, viBase + CT.VI_LEN * iFrac, hw, offI, '#f2c14e', glowI);
    if (mFrac > 0.004) r._radialBar(a, masBase, masBase + CT.MAS_LEN * mFrac, hwM, 0,
      isActive ? '#c48cff' : '#9a6ed2');

    // hover indicators: the slot arc (between the rings) plus a second arc on
    // the mAs-ring side, close to the filament
    if (isHover) {
      ctx.strokeStyle = 'rgba(255,255,255,0.6)'; ctx.lineWidth = 1.4;
      r._annulusStroke((masBase + viTop) / 2, a - CT.STEP_DEG / 2, a + CT.STEP_DEG / 2);
      r._annulusStroke(masBase, a - CT.STEP_DEG / 2, a + CT.STEP_DEG / 2);
    }

    // state marker on the source ring
    const color = (s.stateColor && s.stateColor[f.state]) || '#888';
    const p = r._filamentPos(i);
    const sx = r._x(p.x), sy = r._y(p.y);
    const rad = isActive ? 4.4 : 3;
    if (isActive) { ctx.shadowColor = 'rgba(255,93,93,0.9)'; ctx.shadowBlur = 14; }
    ctx.fillStyle = color;
    ctx.beginPath(); ctx.arc(sx, sy, rad, 0, 2 * Math.PI); ctx.fill();
    ctx.shadowBlur = 0;
    if (covered.has(i) && !isActive) { ctx.strokeStyle = 'rgba(242,193,78,0.7)'; ctx.lineWidth = 1; ctx.stroke(); }

    // index labels every 24 (bold), plus every-4 when the option is on
    const label = (i % LABEL_EVERY === 0) || (r.opts.indices && i % 4 === 0) || isActive;
    if (label) {
      const lr = viTop + 9, ar = a * D2R;
      ctx.fillStyle = isActive ? '#ff9a9a' : 'rgba(170,190,200,0.7)';
      ctx.font = (i % LABEL_EVERY === 0 ? '700 ' : '') + '9px var(--mono, monospace)';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(String(i), r._x(lr * Math.cos(ar)), r._y(lr * Math.sin(ar)));
    }
  }
}
