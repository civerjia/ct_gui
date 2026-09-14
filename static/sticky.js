/*
 * Sticky inputs — every field remembers the last value the user typed/picked.
 *
 * The GUI is one page whose "pages" are show/hide panels (test tabs, view-mode
 * segments, the Power 1/2 target) and cards that are rebuilt from template HTML
 * (power.js / tests.js). Values survive a panel switch, but a browser reload —
 * or a card that re-renders — starts every field back at its markup default.
 * This module persists them to localStorage and puts them back on load.
 *
 * How it works: ONE delegated listener pair on document (capture phase, so a
 * handler that stops propagation can't hide the edit from us) writes every
 * change into a single JSON blob; a MutationObserver restores fields that are
 * injected later (the async mapping card, any card re-render). Restored fields
 * fire `input` + `change` so the wiring that derives state from them (geometry,
 * scan summary, pot estimates) reacts exactly as if the user had typed it.
 *
 * Opting out: put `data-nostick` on a field (or any ancestor). Used for the
 * safety Override gate and the two live-capture checkboxes, which would
 * otherwise start hardware traffic on their own the moment the page loads.
 * `data-nostick-events` restores the value but skips the synthetic events.
 */

const STORE_KEY = 'ctgui.inputs.v1';
const SAVE_DEBOUNCE_MS = 250;

const SKIP_TYPES = new Set(['password', 'file', 'hidden', 'submit', 'button', 'reset', 'image']);

let store = {};
let saveTimer = null;
const restored = new WeakSet();

function load() {
  try { store = JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {}; }
  catch { store = {}; }
}

function flush() {
  saveTimer = null;
  try { localStorage.setItem(STORE_KEY, JSON.stringify(store)); }
  catch { /* private mode / quota — sticky values are a convenience, never fatal */ }
}

function scheduleFlush() {
  if (saveTimer) return;
  saveTimer = setTimeout(flush, SAVE_DEBOUNCE_MS);
}

// Stable key for a field. Radio groups share one key (their `name`), everything
// else prefers the id the rest of the GUI already looks it up by.
function keyOf(el) {
  if (el.type === 'radio') return el.name ? 'radio:' + el.name : null;
  return el.id || el.name || null;
}

function eligible(el) {
  if (!el || !(el instanceof Element)) return false;
  const tag = el.tagName;
  if (tag !== 'INPUT' && tag !== 'SELECT' && tag !== 'TEXTAREA') return false;
  if (tag === 'INPUT' && SKIP_TYPES.has(el.type)) return false;
  if (el.closest('[data-nostick]')) return false;
  return !!keyOf(el);
}

function readValue(el) {
  if (el.type === 'checkbox') return !!el.checked;
  if (el.type === 'radio') return el.checked ? el.value : undefined;   // only the chosen one writes
  if (el.tagName === 'SELECT' && el.multiple) return [...el.selectedOptions].map((o) => o.value);
  return el.value;
}

function save(el) {
  const key = keyOf(el);
  if (!key) return;
  const v = readValue(el);
  if (v === undefined) return;
  store[key] = v;
  scheduleFlush();
}

function applyValue(el, v) {
  if (el.type === 'checkbox') { if (el.checked === !!v) return false; el.checked = !!v; return true; }
  if (el.type === 'radio') { const want = el.value === v; if (el.checked === want) return false; el.checked = want; return true; }
  if (el.tagName === 'SELECT' && el.multiple) {
    const set = new Set(Array.isArray(v) ? v : [v]);
    let changed = false;
    for (const o of el.options) { if (o.selected !== set.has(o.value)) { o.selected = set.has(o.value); changed = true; } }
    return changed;
  }
  if (el.tagName === 'SELECT' && ![...el.options].some((o) => o.value === v)) return false;  // option list moved on
  if (el.value === v) return false;
  el.value = v;
  return true;
}

function restore(el) {
  if (restored.has(el) || !eligible(el)) return;
  restored.add(el);
  const key = keyOf(el);
  if (!(key in store)) return;
  if (!applyValue(el, store[key])) return;
  if (el.hasAttribute('data-nostick-events')) return;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
}

function restoreWithin(root) {
  if (!root) return;
  if (root.nodeType === Node.ELEMENT_NODE && eligible(root)) restore(root);
  if (root.querySelectorAll) for (const el of root.querySelectorAll('input, select, textarea')) restore(el);
}

/**
 * Wire persistence for every field on the page, now and later.
 * Call once, after the cards that build their own markup have mounted.
 */
export function initSticky(root = document) {
  load();

  const onEdit = (e) => { if (eligible(e.target)) save(e.target); };
  document.addEventListener('input', onEdit, true);
  document.addEventListener('change', onEdit, true);
  // A pending debounce must not be lost when the tab goes away.
  window.addEventListener('pagehide', () => { if (saveTimer) { clearTimeout(saveTimer); flush(); } });

  restoreWithin(root);

  // Cards rendered after init (the async mapping card, any re-render) get their
  // saved values as soon as they land in the DOM.
  const obs = new MutationObserver((records) => {
    for (const r of records) for (const n of r.addedNodes) restoreWithin(n);
  });
  obs.observe(document.body, { childList: true, subtree: true });

  window.ctSticky = {
    dump: () => ({ ...store }),
    forget: (key) => { delete store[key]; flush(); },
    clear: () => { store = {}; flush(); },
  };
}
