// Collimator — a physical block INSIDE the source ring, over the 35-filament
// window, with one slit per covered filament; the active slit glows open.

import { CT, filamentBaseAngle } from '../constants.js';

export function drawCollimator(r) {
  if (!r.opts.wedge) return;
  const ctx = r.ctx;
  const half = (CT.COVERAGE - 1) / 2;
  const cAng = r._collimatorAngle();
  const d0 = cAng - half * CT.STEP_DEG, d1 = cAng + half * CT.STEP_DEG;
  const rIn = CT.R_SOURCE * 0.895, rOut = CT.R_SOURCE * 0.965;

  // body
  r._annulus(rIn, rOut, d0, d1);
  const grad = ctx.createRadialGradient(r._cx, r._cy, r._s(rIn), r._cx, r._cy, r._s(rOut));
  grad.addColorStop(0, 'rgba(46,58,66,0.95)');
  grad.addColorStop(1, 'rgba(28,38,45,0.95)');
  ctx.fillStyle = grad; ctx.fill();
  ctx.lineWidth = 1; ctx.strokeStyle = 'rgba(242,193,78,0.5)'; ctx.stroke();

  // slits — one per covered filament; the active slit glows open
  for (const i of r._windowIndices()) {
    const a = filamentBaseAngle(i) * Math.PI / 180;
    const isActive = i === r.state.activeFilament;
    ctx.strokeStyle = isActive ? 'rgba(255,150,150,0.95)' : 'rgba(10,16,22,0.85)';
    ctx.lineWidth = isActive ? 1.8 : 0.7;
    ctx.beginPath();
    ctx.moveTo(r._x(rIn * Math.cos(a)), r._y(rIn * Math.sin(a)));
    ctx.lineTo(r._x(rOut * Math.cos(a)), r._y(rOut * Math.sin(a)));
    ctx.stroke();
  }
}
