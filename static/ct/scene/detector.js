// Detector ring guide, the field-of-view marker, and the 256×256 PCD panel.
// drawDetector caches its panel edges on r._detEdges for the beam to aim at.

import { CT, D2R } from '../constants.js';

export function drawDetectorRing(r) {
  const ctx = r.ctx;
  ctx.strokeStyle = 'rgba(91,155,213,0.18)';
  ctx.setLineDash([3, 6]); ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(r._cx, r._cy, r._s(CT.R_DETECTOR), 0, 2 * Math.PI); ctx.stroke();
  ctx.setLineDash([]);
}

export function drawFOV(r) {
  const ctx = r.ctx, rad = r._s(CT.R_FOV);
  const fov = ctx.createRadialGradient(r._cx, r._cy, 2, r._cx, r._cy, rad);
  fov.addColorStop(0, 'rgba(63,182,160,0.08)');
  fov.addColorStop(1, 'rgba(63,182,160,0)');
  ctx.fillStyle = fov;
  ctx.beginPath(); ctx.arc(r._cx, r._cy, rad, 0, 2 * Math.PI); ctx.fill();
  ctx.setLineDash([2, 4]); ctx.strokeStyle = 'rgba(63,182,160,0.32)'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(r._cx, r._cy, rad, 0, 2 * Math.PI); ctx.stroke();
  ctx.setLineDash([]);
  ctx.strokeStyle = 'rgba(180,200,210,0.35)'; ctx.lineWidth = 1;
  const c = r._s(5);
  ctx.beginPath();
  ctx.moveTo(r._cx - c, r._cy); ctx.lineTo(r._cx + c, r._cy);
  ctx.moveTo(r._cx, r._cy - c); ctx.lineTo(r._cx, r._cy + c);
  ctx.stroke();
}

export function drawDetector(r) {
  const ctx = r.ctx;
  const c = r._detectorCenter(), a = r._detectorAngle() * D2R;
  const tx = -Math.sin(a), ty = Math.cos(a), nx = Math.cos(a), ny = Math.sin(a);
  const hw = CT.DET_WIDTH / 2;
  const e0 = { x: c.x - tx * hw, y: c.y - ty * hw }, e1 = { x: c.x + tx * hw, y: c.y + ty * hw };

  // housing
  const hwH = hw + 4, depth = 9;
  const h0 = { x: c.x - tx * hwH, y: c.y - ty * hwH }, h1 = { x: c.x + tx * hwH, y: c.y + ty * hwH };
  ctx.fillStyle = 'rgba(34,46,56,0.95)'; ctx.strokeStyle = 'rgba(120,150,165,0.4)'; ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(r._x(h0.x), r._y(h0.y)); ctx.lineTo(r._x(h1.x), r._y(h1.y));
  ctx.lineTo(r._x(h1.x + nx * depth), r._y(h1.y + ny * depth));
  ctx.lineTo(r._x(h0.x + nx * depth), r._y(h0.y + ny * depth));
  ctx.closePath(); ctx.fill(); ctx.stroke();

  // active PCD surface (faces source)
  const th = 3;
  const grad = ctx.createLinearGradient(r._x(e0.x), r._y(e0.y), r._x(e1.x), r._y(e1.y));
  grad.addColorStop(0, '#3f7fb8'); grad.addColorStop(0.5, '#6fb0e6'); grad.addColorStop(1, '#3f7fb8');
  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.moveTo(r._x(e0.x), r._y(e0.y)); ctx.lineTo(r._x(e1.x), r._y(e1.y));
  ctx.lineTo(r._x(e1.x + nx * th), r._y(e1.y + ny * th));
  ctx.lineTo(r._x(e0.x + nx * th), r._y(e0.y + ny * th));
  ctx.closePath(); ctx.fill();
  ctx.lineWidth = 1; ctx.strokeStyle = '#bcdcf6'; ctx.stroke();

  if (r.opts.detGrid) {
    ctx.strokeStyle = 'rgba(12,22,30,0.5)'; ctx.lineWidth = 0.5; ctx.beginPath();
    const ticks = 32;
    for (let k = 0; k <= ticks; k++) {
      const fr = k / ticks, px = e0.x + (e1.x - e0.x) * fr, py = e0.y + (e1.y - e0.y) * fr;
      ctx.moveTo(r._x(px), r._y(py)); ctx.lineTo(r._x(px + nx * th), r._y(py + ny * th));
    }
    ctx.stroke();
  }

  ctx.fillStyle = 'rgba(180,210,240,0.85)'; ctx.font = '9px var(--mono, monospace)';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  const lr = CT.R_DETECTOR + depth + 8;
  ctx.fillText('256×256 PCD', r._x(lr * Math.cos(a)), r._y(lr * Math.sin(a)));
  r._detEdges = { e0, e1, c };
}
