// Rotation cues — collimator-ring step direction arrow on the detector side,
// and the gantry ±max rock arc with a direction arrowhead + live position mark.

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

export function drawRotationCues(r) {
  const ctx = r.ctx;
  const cAng = r._collimatorAngle();
  const dir = r.state.collimatorDir || 1;
  curvedArrow(r, CT.R_DETECTOR * 0.78, r._detectorAngle() - 14 * dir, r._detectorAngle() + 14 * dir, 'rgba(120,170,210,0.8)', dir);

  const maxA = r.state.gantryMax || 10;
  const rad = r._bands().outer + 13;
  ctx.strokeStyle = 'rgba(154,167,176,0.4)'; ctx.lineWidth = 2;
  r._annulusStroke(rad, cAng - maxA, cAng + maxA);

  const fdir = r.state.filamentDir || 1;
  const tip = (cAng + maxA * fdir) * D2R;
  arrowHead(r, rad * Math.cos(tip), rad * Math.sin(tip), tip + (fdir > 0 ? Math.PI / 2 : -Math.PI / 2), 'rgba(200,210,215,0.8)');

  const cur = (cAng + r.state.gantryAngle) * D2R;
  ctx.fillStyle = '#e3ecf0';
  ctx.beginPath(); ctx.arc(r._x(rad * Math.cos(cur)), r._y(rad * Math.sin(cur)), 2.6, 0, 2 * Math.PI); ctx.fill();
}
