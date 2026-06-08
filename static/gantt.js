/*
 * Schedule Gantt — the bound schedule as filament-rows × trigger-timeline.
 *
 * Each filament (Y) gets a horizontal ACTIVE bar over the trigger span it's
 * heated (from the derived heating plan), with emission pulses as ticks inside
 * it. The gap between a bar's start and its first tick IS the settle lead; the
 * tail past the last tick is the hold. A vertical playhead marks the live
 * trigger. Scroll to zoom (around the cursor), drag to pan, double-click resets.
 */

const N = 96;

export class ScheduleGantt {
  static HEIGHT = 240; // fixed canvas height (css px)

  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.dpr = Math.max(1, window.devicePixelRatio || 1);
    this.state = null;
    this.hover = null;
    this._len = 0;
    this.v0 = 0;          // view start (trigger)
    this.vSpan = null;    // visible span (triggers); null = uninitialized
    this._pan = null;
    new ResizeObserver(() => this._resize()).observe(canvas.parentElement);
    canvas.addEventListener('mousemove', (e) => this._onMove(e));
    canvas.addEventListener('mousedown', (e) => this._onDown(e));
    window.addEventListener('mouseup', () => this._onUp());
    canvas.addEventListener('mouseleave', () => { if (!this._pan) { this.hover = null; this.draw(); } });
    canvas.addEventListener('wheel', (e) => this._onWheel(e), { passive: false });
    canvas.addEventListener('dblclick', () => { this.v0 = 0; this.vSpan = this._len; this.draw(); });
    this._resize();
  }

  update(state) {
    if (this.vSpan == null || this._len !== state.len) { this.v0 = 0; this.vSpan = state.len; } // reset on length change
    this._len = state.len;
    this.state = state;
    this._clampView();
    this.draw();
  }

  _resize() {
    const w = Math.max(280, this.canvas.parentElement.getBoundingClientRect().width);
    const h = ScheduleGantt.HEIGHT;
    if (this.cssW === w && this.cssH === h) return;
    this.canvas.width = Math.round(w * this.dpr);
    this.canvas.height = Math.round(h * this.dpr);
    this.canvas.style.width = w + 'px';
    this.canvas.style.height = h + 'px';
    this.cssW = w; this.cssH = h;
    this.draw();
  }

  get _m() { return { l: 30, r: 8, t: 6, b: 16 }; }
  get _plotW() { const m = this._m; return this.cssW - m.l - m.r; }
  _x(t) { return this._m.l + ((t - this.v0) / this.vSpan) * this._plotW; }
  _t(px) { return this.v0 + ((px - this._m.l) / this._plotW) * this.vSpan; }
  _y(f) { const m = this._m; return m.t + (f / N) * (this.cssH - m.t - m.b); }
  get _rowH() { const m = this._m; return (this.cssH - m.t - m.b) / N; }

  _clampView() {
    const len = this._len || 1;
    const minSpan = Math.min(20, len);
    this.vSpan = Math.max(minSpan, Math.min(this.vSpan || len, len));
    this.v0 = Math.max(0, Math.min(this.v0, len - this.vSpan));
  }

  _onWheel(e) {
    if (!this.state) return;
    e.preventDefault();
    const mx = e.clientX - this.canvas.getBoundingClientRect().left;
    const tAt = this._t(mx);
    this.vSpan *= e.deltaY < 0 ? 0.82 : 1 / 0.82;
    this._clampView();
    this.v0 = tAt - ((mx - this._m.l) / this._plotW) * this.vSpan; // keep cursor trigger fixed
    this._clampView();
    this.draw();
  }

  _onDown(e) {
    if (!this.state) return;
    const mx = e.clientX - this.canvas.getBoundingClientRect().left;
    this._pan = { mx, v0: this.v0 };
    this.canvas.style.cursor = 'grabbing';
  }
  _onUp() { if (this._pan) { this._pan = null; this.canvas.style.cursor = ''; } }

  _onMove(e) {
    if (!this.state) return;
    const rect = this.canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    if (this._pan) {
      this.v0 = this._pan.v0 - ((mx - this._pan.mx) / this._plotW) * this.vSpan;
      this._clampView();
      this.draw();
      return;
    }
    const m = this._m;
    const f = Math.floor((my - m.t) / this._rowH);
    const t = Math.round(this._t(mx));
    this.hover = (f >= 0 && f < N && mx >= m.l && t >= 0 && t <= this._len) ? { f, t, mx } : null;
    this.draw();
  }

  draw() {
    const ctx = this.ctx;
    ctx.save();
    ctx.scale(this.dpr, this.dpr);
    ctx.clearRect(0, 0, this.cssW, this.cssH);
    ctx.fillStyle = '#0a1016';
    ctx.fillRect(0, 0, this.cssW, this.cssH);
    if (!this.state) { ctx.restore(); return; }

    const s = this.state, len = s.len, rowH = this._rowH, m = this._m;
    const x0 = m.l, x1 = this.cssW - m.r;
    const clampX = (x) => Math.max(x0, Math.min(x1, x));

    // controller bands (P2 = 48-95)
    ctx.fillStyle = 'rgba(120,170,210,0.06)';
    ctx.fillRect(x0, this._y(48), x1 - x0, this._y(96) - this._y(48));

    // ACTIVE bars per filament (clipped to the view)
    ctx.fillStyle = 'rgba(255,93,93,0.32)';
    for (let f = 0; f < N; f++) {
      const iv = s.intervals.get(f);
      if (!iv) continue;
      const y = this._y(f) + rowH * 0.15, bh = rowH * 0.7;
      const seg = (a, b) => { const xa = clampX(this._x(a)), xb = clampX(this._x(b)); if (xb > xa) ctx.fillRect(xa, y, xb - xa, bh); };
      if (iv.promote <= iv.demote) seg(iv.promote, iv.demote);
      else { seg(iv.promote, len); seg(0, iv.demote); }
    }

    // emission bursts (one bar per burst, width = its pulse count)
    ctx.fillStyle = 'rgba(255,205,120,0.95)';
    for (const r of s.emissions) {
      const x = this._x(r.trigger);
      if (x > x1) continue;
      const w = Math.max(0.6, this._x(r.trigger + (r.burstLen || 1)) - x);
      if (x + w < x0) continue;
      ctx.fillRect(Math.max(x0, x), this._y(r.filament) + rowH * 0.15, w, rowH * 0.7);
    }

    // filament axis labels (every 24)
    ctx.fillStyle = 'rgba(160,185,195,0.65)';
    ctx.font = '9px var(--mono, monospace)';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    for (let f = 0; f < N; f += 24) ctx.fillText(String(f), m.l - 4, this._y(f) + rowH / 2);
    ctx.textAlign = 'left'; ctx.fillStyle = 'rgba(120,170,210,0.6)';
    ctx.fillText('P2', m.l + 2, this._y(48) + 7);

    // trigger axis (visible range)
    ctx.fillStyle = 'rgba(160,185,195,0.55)'; ctx.textAlign = 'center'; ctx.textBaseline = 'top';
    for (let k = 0; k <= 4; k++) {
      const t = Math.round(this.v0 + this.vSpan * k / 4);
      ctx.fillText(String(t), Math.max(x0 + 8, Math.min(x1 - 8, this._x(t))), this.cssH - m.b + 3);
    }
    // zoom readout
    if (this.vSpan < len) {
      ctx.textAlign = 'right'; ctx.fillStyle = 'rgba(150,170,182,0.6)';
      ctx.fillText(`${Math.round(this.v0)}–${Math.round(this.v0 + this.vSpan)} / ${len}`, x1, this.cssH - m.b + 3);
    }

    // playhead (only if in view)
    if (s.liveSeq >= this.v0 && s.liveSeq <= this.v0 + this.vSpan) {
      const px = this._x(s.liveSeq);
      ctx.strokeStyle = 'rgba(255,255,255,0.8)'; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(px, m.t); ctx.lineTo(px, this.cssH - m.b); ctx.stroke();
    }

    // hover crosshair + label
    if (this.hover) {
      const { f, t } = this.hover;
      ctx.strokeStyle = 'rgba(255,255,255,0.25)'; ctx.lineWidth = 1;
      ctx.strokeRect(x0, this._y(f), x1 - x0, rowH);
      const iv = s.intervals.get(f);
      const on = iv && (iv.promote <= iv.demote ? (t >= iv.promote && t < iv.demote) : (t >= iv.promote || t < iv.demote));
      const lbl = `fil ${f} · trig ${t} · ${on ? 'ACTIVE' : 'idle'}`;
      ctx.font = '10px var(--mono, monospace)'; ctx.textAlign = 'left'; ctx.textBaseline = 'top';
      const lw = ctx.measureText(lbl).width + 10;
      const lx = Math.min(this.hover.mx + 6, x1 - lw);
      ctx.fillStyle = 'rgba(10,16,22,0.85)'; ctx.fillRect(lx, m.t + 2, lw, 15);
      ctx.fillStyle = '#dfeaee'; ctx.fillText(lbl, lx + 5, m.t + 5);
    }
    ctx.restore();
  }
}
