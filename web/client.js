/*
 * Client identity + the cooperative write lease.
 *
 * The ESP32 bridge is single-client, so backend.py owns the :3333 sockets and
 * every program on the bench — this GUI, bench scripts, another laptop's
 * browser — shares the hardware through its HTTP API. Reads are always free and
 * writes interleave frame-by-frame, so the only thing that needs coordinating
 * is a stretch of UNINTERRUPTED time: a schedule download, a calibration sweep,
 * an armed run. That is the lease (`/api/lock`).
 *
 * This module gives the tab a stable identity (sent as `X-CT-Client` on every
 * /api call, so the backend can tell holders apart and /api/clients can show
 * who else is on the bench) and wraps long exclusive operations in `hold()`,
 * which acquires the lease, renews it while the work runs, and always releases.
 * The lease expires on its own, so a closed tab never wedges the bench.
 */

const LEASE_TTL_S = 30;
const RENEW_MS = 10_000;

// Per-tab id: two tabs of the same GUI are two clients (they can hold the lease
// independently), but a reload keeps the same identity so a lease survives it.
function makeId() {
  let id = sessionStorage.getItem('ctgui.clientId');
  if (!id) {
    id = 'gui-' + Math.random().toString(16).slice(2, 8);
    sessionStorage.setItem('ctgui.clientId', id);
  }
  return id;
}

export const clientId = makeId();

// Backend-liveness banner. Track consecutive network-level failures across ALL
// /api/* calls. HTTP error responses (4xx/5xx) count as alive — only a rejected
// promise (connection refused / backend process dead) increments the streak.
let _failStreak = 0;
const _FAIL_THRESHOLD = 2;   // show banner after this many consecutive failures
function _onApiOk() {
  if (_failStreak === 0) return;
  _failStreak = 0;
  const b = document.getElementById('backendBanner');
  if (b) b.hidden = true;
}
function _onApiFail() {
  _failStreak++;
  if (_failStreak >= _FAIL_THRESHOLD) {
    const b = document.getElementById('backendBanner');
    if (b) b.hidden = false;
  }
}

// Stamp every same-origin /api/* call with our identity. Done once here rather
// than at ~200 call sites (and it covers any fetch added later).
const rawFetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  const url = typeof input === 'string' ? input : (input && input.url) || '';
  if (!url.startsWith('/api/')) return rawFetch(input, init);
  const opts = { ...(init || {}) };
  const headers = new Headers(opts.headers || (typeof input === 'object' ? input.headers : undefined));
  headers.set('X-CT-Client', clientId);
  opts.headers = headers;
  return rawFetch(input, opts).then(
    (resp) => { _onApiOk(); return resp; },
    (err)  => { _onApiFail(); throw err; }
  );
};

const post = (body) => fetch('/api/lock', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
}).then((r) => r.json());

export const lock = {
  id: clientId,
  status: () => fetch('/api/lock').then((r) => r.json()),
  acquire: (note, ttl = LEASE_TTL_S) => post({ action: 'acquire', note, ttl }),
  release: () => post({ action: 'release' }),
  /**
   * Run `fn` holding the exclusive-write lease: other programs' writes are
   * refused (409) for its duration, ours are not. Renewed while it runs and
   * released however it ends. If another client already holds the lease the
   * work still runs — the backend will refuse the writes and the operation
   * reports that itself, which is better than silently doing nothing.
   */
  async hold(note, fn) {
    const got = await post({ action: 'acquire', note, ttl: LEASE_TTL_S }).catch(() => null);
    const mine = !!(got && got.ok);
    const timer = mine ? setInterval(() => post({ action: 'renew', note, ttl: LEASE_TTL_S }), RENEW_MS) : null;
    try {
      return await fn(mine, got && got.lock);
    } finally {
      if (timer) clearInterval(timer);
      if (mine) await post({ action: 'release' }).catch(() => {});
    }
  },
};

window.ctClient = { id: clientId, lock };
