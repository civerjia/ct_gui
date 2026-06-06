// X-ray beam fan from the active filament to the detector panel, plus a center
// ray and a source glow. Needs r._detEdges (set by drawDetector) to aim.

export function drawBeam(r) {
  if (!r.opts.beam || !r._detEdges) return;
  const ctx = r.ctx;
  const src = r._filamentPos(r.state.activeFilament);
  const { e0, e1, c } = r._detEdges;

  const grad = ctx.createLinearGradient(r._x(src.x), r._y(src.y), r._x(c.x), r._y(c.y));
  grad.addColorStop(0, 'rgba(255,110,110,0.30)');
  grad.addColorStop(0.5, 'rgba(255,140,120,0.12)');
  grad.addColorStop(1, 'rgba(120,180,255,0.08)');
  ctx.fillStyle = grad;
  ctx.beginPath();
  ctx.moveTo(r._x(src.x), r._y(src.y));
  ctx.lineTo(r._x(e0.x), r._y(e0.y)); ctx.lineTo(r._x(e1.x), r._y(e1.y));
  ctx.closePath(); ctx.fill();

  ctx.strokeStyle = 'rgba(255,150,150,0.75)'; ctx.lineWidth = 1.2;
  ctx.beginPath(); ctx.moveTo(r._x(src.x), r._y(src.y)); ctx.lineTo(r._x(c.x), r._y(c.y)); ctx.stroke();

  const gl = ctx.createRadialGradient(r._x(src.x), r._y(src.y), 1, r._x(src.x), r._y(src.y), r._s(8));
  gl.addColorStop(0, 'rgba(255,120,120,0.55)'); gl.addColorStop(1, 'rgba(255,120,120,0)');
  ctx.fillStyle = gl;
  ctx.beginPath(); ctx.arc(r._x(src.x), r._y(src.y), r._s(8), 0, 2 * Math.PI); ctx.fill();
}
