// Axis label — marks +y / filament 0 at the top of the ring.

export function drawAxisLabels(r) {
  const ctx = r.ctx, rad = r._bands().outer + 11;
  ctx.fillStyle = 'rgba(150,175,188,0.4)'; ctx.font = '10px var(--mono, monospace)';
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText('+y · fil 0', r._cx, r._y(rad));
}
