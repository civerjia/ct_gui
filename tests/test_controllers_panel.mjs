// Regression test for the controller panel (web/controllers.js): a scan or an
// auto-swap must never make the other bridge's IP disappear (2026-10-02).
// Fake DOM + fake backend, no browser needed:   node tests/test_controllers_panel.mjs
// Fake DOM + fake backend for web/controllers.js. Usage: node ctrl_sim.mjs <path-to-controllers.js>
const target = process.argv[2] || new URL('../web/controllers.js', import.meta.url).href;
function el(extra = {}) {
  const ls = {};
  return Object.assign({ value: '', textContent: '', innerHTML: '', title: '', className: '', hidden: false, disabled: false,
    dataset: {}, _ls: ls, children: [],
    addEventListener(ev, fn) { (ls[ev] ||= []).push(fn); },
    async fire(ev) { for (const f of ls[ev] || []) await f(); },
    appendChild(c) { this.children.push(c); },
    classList: { toggle() {}, remove() {}, add() {} },
    querySelector() { return null; } }, extra);
}
const cards = [1, 2].map((n) => {
  const host = el(), btn = el(), ident = el();
  return el({ dataset: { ctrl: String(n) }, host, btn,
    querySelector(sel) { return sel === '[data-host]' ? host : sel === '[data-connect]' ? btn : sel === '[data-ident]' ? ident : null; } });
});
const hb = {}; for (const n of [1, 2]) { const parts = { '[data-dot]': el(), '[data-master]': el(), '[data-hb="rp"]': el(), '[data-hb="stm"]': el() };
  hb[n] = el({ querySelector: (s) => parts[s] || null }); }
const ids = { scanBtn: el(), scanHint: el(), apiLockBadge: el(), bridgeHosts1: el(), bridgeHosts2: el() };
globalThis.document = {
  getElementById: (id) => ids[id],
  querySelectorAll: () => cards,
  querySelector: (s) => { const m = s.match(/data-ctrl="(\d)"/); return m ? hb[m[1]] : null; },
  createElement: () => el(),
};
let REFRESH = null;
globalThis.setInterval = (fn) => { REFRESH = fn; return 0; };
// ---- fake backend ----
const STM = new Set(['10.0.0.2']);       // B carries the STM32
let scanResults = [];
const ctl = { 1: { connected: false, host: null }, 2: { connected: false, host: null } };
let master = 1;
const status = () => ({ master, lock: {}, you: 'gui', controllers: Object.fromEntries([1, 2].map((n) => [String(n), {
  connected: ctl[n].connected, host: ctl[n].host, bridge_name: '',
  stm32: { ever_seen: ctl[n].connected && STM.has(ctl[n].host), age_ms: 100 }, rp2350: { age_ms: 100 } }])) });
globalThis.fetch = async (path, opts) => {
  const body = opts && opts.body ? JSON.parse(opts.body) : {};
  let r;
  if (path === '/api/status') r = status();
  else if (path === '/api/scan') r = { results: scanResults };
  else if (path === '/api/connect') {
    const other = body.controller === 1 ? 2 : 1;
    if (!body.host) r = { ok: false, error: 'no host' };   // as the real backend
    else if (ctl[other].connected && ctl[other].host === body.host) r = { ok: false, error: 'already connected as Power ' + other };
    else { ctl[body.controller] = { connected: true, host: body.host }; r = { ok: true }; }
  } else if (path === '/api/disconnect') { ctl[body.controller] = { connected: false, host: null }; r = { ok: true }; }
  else if (path === '/api/master') { master = body.controller; r = { master }; }
  else if (path === '/api/auto-connect') {
    // as the backend: free slots only, STM32 board -> Power 1, busy bridges skipped
    const found = scanResults.map((x) => ({ host: x.host, name: x.name, has_stm32: STM.has(x.host), busy: !!x.busy }));
    const mine = new Set([1, 2].filter((n) => ctl[n].connected).map((n) => ctl[n].host));
    const queue = found.filter((b) => !mine.has(b.host) && !b.busy);
    const connected = {};
    for (const n of [1, 2]) {
      if (ctl[n].connected || !queue.length) continue;
      const pick = (n === 1 ? queue.find((b) => b.has_stm32) : queue.find((b) => !b.has_stm32)) || queue[0];
      queue.splice(queue.indexOf(pick), 1);
      ctl[n] = { connected: true, host: pick.host }; connected[n] = pick.host;
    }
    r = { ok: true, found, connected, failed: {} };
  }
  else r = {};
  return { json: async () => r };
};
const { initControllers } = await import(target);
initControllers();
const sleep = () => new Promise((res) => setTimeout(res, 20));
const tick = async () => { await sleep(); for (let i = 0; i < 3; i++) { await REFRESH(); await sleep(); } };
await tick();
const A = '10.0.0.1', B = '10.0.0.2';
const show = () => `P1=[${cards[0].host.value}]${ctl[1].connected ? '(conn ' + ctl[1].host + ')' : ''}  P2=[${cards[1].host.value}]${ctl[2].connected ? '(conn ' + ctl[2].host + ')' : ''}`;
const results = [];
const expect = (name, ok) => { results.push([name, ok]); console.log((ok ? 'PASS ' : 'FAIL ') + name + '   ' + show()); };
const reset = async () => { ctl[1] = { connected: false, host: null }; ctl[2] = { connected: false, host: null }; await tick(); for (const c of cards) c.host.value = ''; };

// Scenario 2 (run first: a fresh page, auto-arrange not yet used): scan fills P1=A, P2=B; user connects P2 (B, STM32) first, P1 not connected
await reset(); cards[0].host.value = A; cards[1].host.value = B;
const before = show();
await cards[1].btn.fire('click'); await tick(); await tick();
expect('connect STM32 board in P2 first: moved to P1, A kept in P2 (not connected)', cards[0].host.value === B && ctl[1].connected && ctl[1].host === B && cards[1].host.value === A && !ctl[2].connected);
console.log('   (before connect: ' + before + ')');

// Scenario 1: P1 connected to B (STM32), then rescan [A, B]
await reset(); scanResults = [{ host: A, name: 'a', controller_responsive: true }, { host: B, name: 'b', controller_responsive: true }];
cards[0].host.value = B; await cards[0].btn.fire('click'); await tick();
await ids.scanBtn.fire('click'); await tick();
expect('Scan with P1 connected: the other bridge is connected as P2', cards[1].host.value === A && cards[0].host.value === B && ctl[2].host === A);

// Scenario 3: P1 connected by hand to A (no STM32), then Scan: the backend
// connects B (STM32) as P2, and the panel swaps them so P1 is the STM32 board
await reset(); cards[0].host.value = A;
await cards[0].btn.fire('click'); await tick();
await ids.scanBtn.fire('click'); await tick(); await tick();
expect('P1 by hand (no STM32) + Scan: swapped, STM32 board P1, both connected', ctl[1].host === B && ctl[2].host === A && cards[0].host.value === B && cards[1].host.value === A);

// Scenario 4: plain first scan, nothing connected
await reset(); await ids.scanBtn.fire('click'); await tick();
expect('first Scan: STM32 board connected as P1, the other as P2', cards[0].host.value === B && cards[1].host.value === A && ctl[1].host === B && ctl[2].host === A);
// Scenario 5: one bridge held by another client -> not connected, shown in the hint
await reset(); scanResults = [{ host: A, name: 'a', busy: true }, { host: B, name: 'b' }];
await ids.scanBtn.fire('click'); await tick();
expect('busy bridge skipped and reported; free card shows it for the user', ctl[1].host === B && !ctl[2].connected
  && /held by another client/.test(ids.scanHint.textContent) && cards[1].host.value === A);
scanResults = [{ host: A, name: 'a' }, { host: B, name: 'b' }];
// Scenario 7: the same IP typed into both cards, both Conn clicked -> the second
// is refused and its card cleared; the two cards never both show it once one connects
await reset(); cards[0].host.value = A; cards[1].host.value = A;
await cards[0].btn.fire('click'); await tick();
await cards[1].btn.fire('click'); await tick();
expect('same IP in both cards: only one connects, other card cleared',
  ctl[1].host === A && !ctl[2].connected && cards[1].host.value !== A && /no host|already/.test(ids.scanHint.textContent));
process.exit(results.every(([, ok]) => ok) ? 0 : 1);
