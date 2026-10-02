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
    if (ctl[other].connected && ctl[other].host === body.host) r = { ok: false, error: 'already connected on the other slot' };
    else { ctl[body.controller] = { connected: true, host: body.host }; r = { ok: true }; }
  } else if (path === '/api/disconnect') { ctl[body.controller] = { connected: false, host: null }; r = { ok: true }; }
  else if (path === '/api/master') { master = body.controller; r = { master }; }
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

// Scenario 1: P1 connected to B (STM32), then rescan [A, B]
await reset(); scanResults = [{ host: A, name: 'a', controller_responsive: true }, { host: B, name: 'b', controller_responsive: true }];
cards[0].host.value = B; await cards[0].btn.fire('click'); await tick();
await ids.scanBtn.fire('click'); await tick();
expect('rescan with P1 connected keeps the other IP in P2', cards[1].host.value === A && cards[0].host.value === B);

// Scenario 2: scan fills P1=A, P2=B; user connects P2 (B, STM32) first, P1 not connected
await reset(); await ids.scanBtn.fire('click'); await tick();
const before = show();
await cards[1].btn.fire('click'); await tick(); await tick();
expect('connect STM32 board in P2 first: moved to P1, A kept in P2 (not connected)', cards[0].host.value === B && ctl[1].connected && ctl[1].host === B && cards[1].host.value === A && !ctl[2].connected);
console.log('   (before connect: ' + before + ')');

// Scenario 3: both connected with STM32 in P2 -> swapped, both stay connected
await reset(); cards[0].host.value = A; cards[1].host.value = B;
await cards[0].btn.fire('click'); await tick();
await ids.scanBtn.fire('click'); await tick();   // re-enable auto-arrange (new scan)
cards[1].host.value = B; await cards[1].btn.fire('click'); await tick(); await tick();
expect('both connected, STM32 in P2: swapped, both still connected', ctl[1].host === B && ctl[2].host === A && cards[0].host.value === B && cards[1].host.value === A);

// Scenario 4: plain first scan, nothing connected
await reset(); await ids.scanBtn.fire('click'); await tick();
expect('first scan fills P1=A, P2=B', cards[0].host.value === A && cards[1].host.value === B);
// Scenario 5: scan, then connect P1 = A (no STM32) -> P2 still shows B
await reset(); await ids.scanBtn.fire('click'); await tick();
await cards[0].btn.fire('click'); await tick();
expect('scan, connect P1 (no STM32): P2 still shows B', ctl[1].host === A && cards[1].host.value === B);
// Scenario 6: scan, user swaps fields, connects P1 = B (STM32) -> P2 still shows A
await reset(); await ids.scanBtn.fire('click'); await tick();
cards[0].host.value = B; cards[1].host.value = A;
await cards[0].btn.fire('click'); await tick();
expect('connect P1 = STM32 board: P2 still shows A', ctl[1].host === B && cards[1].host.value === A);
process.exit(results.every(([, ok]) => ok) ? 0 : 1);
