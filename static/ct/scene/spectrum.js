/*
 * Filament-ring spectrum.
 *
 * Each filament is a glyph: a state-colored marker on the source ring, plus two
 * concentric bands growing outward — inner = accumulated mAs (violet), outer =
 * voltage (teal) + current (amber) side by side. Dim baseline stubs keep the
 * ring reading full even when most filaments are asleep.
 */

import { CT, D2R, clamp01, filamentBaseAngle } from '../constants.js';

export function drawSpectrum(r) {
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
    const a = filamentBaseAngle(i);
    const isActive = i === s.activeFilament;
    const isHover = i === s.hover;

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

    // hover highlight: brighten the slot
    if (isHover) {
      ctx.strokeStyle = 'rgba(255,255,255,0.5)'; ctx.lineWidth = 1;
      r._annulusStroke((masBase + viTop) / 2, a - CT.STEP_DEG / 2, a + CT.STEP_DEG / 2);
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

    if (r.opts.indices && (i % 4 === 0 || isActive || i === s.collimatorCenter)) {
      const lr = viTop + 9, ar = a * D2R;
      ctx.fillStyle = isActive ? '#ff9a9a' : 'rgba(160,185,195,0.6)';
      ctx.font = '9px var(--mono, monospace)';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
      ctx.fillText(String(i), r._x(lr * Math.cos(ar)), r._y(lr * Math.sin(ar)));
    }
  }

  // band labels near +x
  ctx.fillStyle = 'rgba(63,182,160,0.7)'; ctx.font = '9px var(--mono, monospace)';
  ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
  ctx.fillText('V·I', r._x(viTop + 4), r._cy);
  ctx.fillStyle = 'rgba(154,110,210,0.75)';
  ctx.fillText('mAs', r._x(masTop + 3), r._cy + 12);
}
