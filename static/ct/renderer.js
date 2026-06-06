/*
 * CTGeometry — the renderer core.
 *
 * Owns the canvas, the world↔screen transform, the low-level drawing
 * primitives, and the machine-geometry helpers. The actual scene (filaments,
 * collimator, detector, beam, cues, HUD) lives in ./scene/* modules; draw()
 * just composes them in z-order. Each scene module is a pure function of the
 * renderer instance, so they share the primitives without inheritance.
 */

import { CT, D2R, mod, filamentBaseAngle } from './constants.js';
import { drawBackground } from './scene/background.js';
import { drawSpectrum } from './scene/spectrum.js';
import { drawCollimator } from './scene/collimator.js';
import { drawDetectorRing, drawFOV, drawDetector } from './scene/detector.js';
import { drawBeam } from './scene/beam.js';
import { drawRotationCues } from './scene/cues.js';
import { drawAxisLabels } from './scene/axes.js';
import { drawHud } from './scene/hud.js';

export class CTGeometry {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.dpr = Math.max(1, window.devicePixelRatio || 1);
    this.opts = { beam: true, indices: false, wedge: true, detGrid: true };
    this.state = null;
    new ResizeObserver(() => this._resize()).observe(canvas.parentElement);
    this._resize();
  }

  setOptions(o) { Object.assign(this.opts, o); this.draw(); }
  update(state) { this.state = state; this.draw(); }

  // radii of the two spectrum bands (mm)
  _bands() {
    const masBase = CT.R_SOURCE + CT.MAS_GAP;
    const masTop = masBase + CT.MAS_LEN;
    const viBase = masTop + CT.BAND_GAP;
    const viTop = viBase + CT.VI_LEN;
    return { masBase, masTop, viBase, viTop, outer: viTop };
  }

  _resize() {
    const box = this.canvas.parentElement.getBoundingClientRect();
    const w = Math.max(320, box.width), h = Math.max(320, box.height);
    this.canvas.width = Math.round(w * this.dpr);
    this.canvas.height = Math.round(h * this.dpr);
    this.canvas.style.width = w + 'px';
    this.canvas.style.height = h + 'px';
    this.cssW = w; this.cssH = h;
    const worldR = this._bands().outer + 20;
    this.scale = Math.min(w, h) / (2 * worldR);
    this.draw();
  }

  // world (mm) -> screen (css px); y flipped
  _x(x) { return this.cssW / 2 + x * this.scale; }
  _y(y) { return this.cssH / 2 - y * this.scale; }
  _s(v) { return v * this.scale; }
  get _cx() { return this.cssW / 2; }
  get _cy() { return this.cssH / 2; }

  draw() {
    const ctx = this.ctx;
    ctx.save();
    ctx.scale(this.dpr, this.dpr);
    ctx.clearRect(0, 0, this.cssW, this.cssH);

    drawBackground(this);
    drawDetectorRing(this);
    drawFOV(this);
    if (this.state) {
      drawCollimator(this);
      drawDetector(this);
      drawBeam(this);
      drawSpectrum(this);
      drawRotationCues(this);
    }
    this._ring(CT.R_SOURCE, 'rgba(150,170,180,0.28)', 1.2);
    drawAxisLabels(this);
    if (this.state) drawHud(this);
    ctx.restore();
  }

  // --- primitives -------------------------------------------------------------
  _rrect(x, y, w, h, r) {
    const ctx = this.ctx;
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  _ring(r, color, lw) {
    const ctx = this.ctx;
    ctx.strokeStyle = color; ctx.lineWidth = lw;
    ctx.beginPath(); ctx.arc(this._cx, this._cy, this._s(r), 0, 2 * Math.PI); ctx.stroke();
  }
  _annulusStroke(r, d0, d1) {
    const ctx = this.ctx;
    ctx.beginPath();
    ctx.arc(this._cx, this._cy, this._s(r), -d0 * D2R, -d1 * D2R, true);
    ctx.stroke();
  }
  _annulus(rIn, rOut, d0, d1) {
    const ctx = this.ctx;
    ctx.beginPath();
    ctx.arc(this._cx, this._cy, this._s(rOut), -d0 * D2R, -d1 * D2R, true);
    ctx.arc(this._cx, this._cy, this._s(rIn), -d1 * D2R, -d0 * D2R, false);
    ctx.closePath();
  }

  // radial bar centered at tangential offset `off` (mm), r0->r1, optional glow
  _radialBar(angDeg, r0, r1, halfW, off, fill, glow) {
    const ctx = this.ctx;
    const a = angDeg * D2R, ca = Math.cos(a), sa = Math.sin(a);
    const tx = -sa, ty = ca;
    const pt = (r, s) => [this._x(r * ca + s * tx), this._y(r * sa + s * ty)];
    const [ax, ay] = pt(r0, off - halfW);
    const [bx, by] = pt(r0, off + halfW);
    const [cx, cy] = pt(r1, off + halfW);
    const [dx, dy] = pt(r1, off - halfW);
    if (glow) { ctx.shadowColor = glow; ctx.shadowBlur = 10; }
    ctx.fillStyle = fill;
    ctx.beginPath();
    ctx.moveTo(ax, ay); ctx.lineTo(bx, by); ctx.lineTo(cx, cy); ctx.lineTo(dx, dy);
    ctx.closePath(); ctx.fill();
    ctx.shadowBlur = 0;
  }

  // --- machine-geometry helpers ----------------------------------------------
  _filamentPos(i) {
    const a = filamentBaseAngle(i) * D2R;
    return { x: CT.R_SOURCE * Math.cos(a), y: CT.R_SOURCE * Math.sin(a) };
  }
  _windowIndices() {
    const half = (CT.COVERAGE - 1) / 2, c = this.state.collimatorCenter, out = [];
    for (let k = -half; k <= half; k++) out.push(mod(c + k, CT.N_FILAMENTS));
    return out;
  }
  _collimatorAngle() { return filamentBaseAngle(this.state.collimatorCenter); }
  _detectorAngle() { return this._collimatorAngle() + 180; }
  _detectorCenter() {
    const a = this._detectorAngle() * D2R;
    return { x: CT.R_DETECTOR * Math.cos(a), y: CT.R_DETECTOR * Math.sin(a) };
  }

  // map a canvas px to the nearest filament index, or -1 if outside the rings
  hitTest(cssX, cssY) {
    if (!this.state) return -1;
    const wx = (cssX - this._cx) / this.scale, wy = -(cssY - this._cy) / this.scale;
    const r = Math.hypot(wx, wy);
    if (r < CT.R_SOURCE - 6 || r > this._bands().outer + 6) return -1;
    const ang = Math.atan2(wy, wx) / D2R;
    let best = -1, bestd = 1e9;
    for (let i = 0; i < CT.N_FILAMENTS; i++) {
      const d = Math.abs(((filamentBaseAngle(i) - ang + 540) % 360) - 180);
      if (d < bestd) { bestd = d; best = i; }
    }
    return bestd <= CT.STEP_DEG / 2 + 0.3 ? best : -1;
  }
}
