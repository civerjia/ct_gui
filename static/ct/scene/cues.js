// Rotation cues — collimator-ring step direction arrow on the detector side,
// and the gantry rock indicator (fixed to the gantry home, +y).

import { CT, D2R } from '../constants.js';

function arrowHead(r, wx, wy, headingRad, color) {
  const ctx = r.ctx;
  const sx = r._x(wx), sy = r._y(wy), sh = -headingRad, len = 7, spread = 0.5;
  ctx.fillStyle = color;
  ctx.beginPath();
  ctx.moveTo(sx, sy);
  ctx.lineTo(sx - len * Math.cos(sh - spread), sy - len * Math.sin(sh - spread));
  ctx.lineTo(sx - len * Math.cos(sh + spread), sy - len * Math.sin(sh + spread));
  ctx.closePath(); ctx.fill();
}

function curvedArrow(r, radius, d0, d1, color, dir) {
  const ctx = r.ctx;
  ctx.strokeStyle = color; ctx.lineWidth = 2;
  r._annulusStroke(radius, d0, d1);
  const tip = d1 * D2R;
  arrowHead(r, radius * Math.cos(tip), radius * Math.sin(tip), tip + (dir > 0 ? Math.PI / 2 : -Math.PI / 2), color);
}

// fixed gantry reference: filament-0 home at +y (90°). The gantry rock is a
// rotation about this frame, so the indicator lives here regardless of where
// the collimator/detector are.
const GANTRY_REF = 90;

export function drawRotationCues(r) {
  const ctx = r.ctx;

  // collimator-ring step direction (moves with the detector/collimator)
  const dir = r.state.collimatorDir || 1;
  curvedArrow(r, CT.R_DETECTOR * 0.78, r._detectorAngle() - 14 * dir, r._detectorAngle() + 14 * dir, 'rgba(120,170,210,0.8)', dir);

  // gantry rock indicator — bound to the gantry frame (fixed at +y); sits well
  // outside the filament index labels (which are at outerR + 9).
  const g = r.state.gantryAngle || 0;
  const maxA = r.state.gantryMax || 10;
  const rad = r._outerR() + 20;
  const radialTick = (deg, len, col, lw) => {
    const a = deg * D2R;
    ctx.strokeStyle = col; ctx.lineWidth = lw;
    ctx.beginPath();
    ctx.moveTo(r._x((rad - len) * Math.cos(a)), r._y((rad - len) * Math.sin(a)));
    ctx.lineTo(r._x((rad + len) * Math.cos(a)), r._y((rad + len) * Math.sin(a)));
    ctx.stroke();
  };
  // range arc + end ticks
  ctx.strokeStyle = 'rgba(154,167,176,0.45)'; ctx.lineWidth = 2;
  r._annulusStroke(rad, GANTRY_REF - maxA, GANTRY_REF + maxA);
  radialTick(GANTRY_REF - maxA, 3, 'rgba(154,167,176,0.55)', 1);
  radialTick(GANTRY_REF + maxA, 3, 'rgba(154,167,176,0.55)', 1);
  // 0° tick (gantry home)
  radialTick(GANTRY_REF, 5, 'rgba(214,224,228,0.9)', 1.6);
  // live marker at home + gantry angle
  const cur = (GANTRY_REF + g) * D2R;
  ctx.fillStyle = '#ffd27a';
  ctx.beginPath(); ctx.arc(r._x(rad * Math.cos(cur)), r._y(rad * Math.sin(cur)), 3.2, 0, 2 * Math.PI); ctx.fill();

  // gantry angle readout — in world space above the arc, with a clear gap so it
  // never lands on the indicator/marker
  ctx.fillStyle = 'rgba(225,234,238,0.92)';
  ctx.font = '700 11px var(--mono, monospace)';
  ctx.textAlign = 'center'; ctx.textBaseline = 'bottom';
  ctx.fillText(`gantry ${g >= 0 ? '+' : ''}${g.toFixed(1)}°`, r._x(0), r._y(rad + 13));
}
