/*
 * Shared DOM helpers. Was independently reinvented as `$`/`$p`/`$t` in
 * app.js/power.js/tests.js and as six near-identical "set status text"
 * one-liners (tMsg/setStatus/heatMsg/hwMsg/bmMsg/sbMsg/shvMsg/i2cMsg) —
 * consolidated here so there's one implementation to fix/extend.
 */

export const $ = (id) => document.getElementById(id);

// Sets a status/message element's text. Most callers want plain text
// (XSS-safe by default); a few status lines are built from internally
// generated HTML fragments (e.g. `<br>`-joined per-controller results) — pass
// {html:true} for those, explicitly, instead of silently choosing innerHTML.
// {cls} optionally replaces the element's className (some status lines style
// themselves per-outcome, e.g. tMsg's 'summary ok'/'summary fail').
export function setMsg(id, text, opts) {
  const e = $(id);
  if (!e) return;
  if (opts && opts.html) e.innerHTML = text; else e.textContent = text;
  if (opts && opts.cls !== undefined) e.className = opts.cls;
}

// Live-clamps every <input type="number"> under `root` that declares a min
// and/or max: red-highlights it (.num-invalid) while the typed value is
// out of range or non-numeric, and snaps it back into range on blur.
// HTML's min/max attributes alone do NOT stop an out-of-range value being
// typed and read via .value -- they only affect the spinner arrows -- so
// without this every plain number input across the app (Pulse µs, HV
// setpoints, timeouts, sweep params, ...) would let through a value no
// caller ever validates. Idempotent (safe to call again after a re-render)
// via a data-clamped marker, since some cards re-render their own inputs.
export function clampNumberInputs(root) {
  if (!root) return;
  root.querySelectorAll('input[type="number"]').forEach((inp) => {
    if (inp.dataset.clamped) return;
    const hasMin = inp.min !== '', hasMax = inp.max !== '';
    if (!hasMin && !hasMax) return;               // nothing declared to clamp against
    inp.dataset.clamped = '1';
    const lo = hasMin ? +inp.min : -Infinity, hi = hasMax ? +inp.max : Infinity;
    const invalid = () => { const n = +inp.value; return inp.value === '' || !Number.isFinite(n) || n < lo || n > hi; };
    inp.addEventListener('input', () => inp.classList.toggle('num-invalid', invalid()));
    inp.addEventListener('blur', () => {
      let n = +inp.value;
      if (!Number.isFinite(n)) n = hasMin ? lo : 0;
      inp.value = Math.min(hi, Math.max(lo, n));
      inp.classList.remove('num-invalid');
    });
  });
}
