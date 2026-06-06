// Instrument backdrop — deep radial gradient behind everything.

export function drawBackground(r) {
  const ctx = r.ctx;
  const g = ctx.createRadialGradient(r._cx, r._cy, 8, r._cx, r._cy, Math.max(r.cssW, r.cssH) / 1.25);
  g.addColorStop(0, '#0f1820');
  g.addColorStop(0.62, '#0a1016');
  g.addColorStop(1, '#05080b');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, r.cssW, r.cssH);
}
