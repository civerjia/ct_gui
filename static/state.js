/*
 * Shared cross-module state. Was 13 ad hoc `window.ct*` globals — one module
 * writes, another reads, with no single place listing what's actually on
 * the bus. One object now; same read/write pattern (mutate a field, call a
 * stashed callback), just an explicit import instead of an implicit global.
 */
export const state = {
  master: null,             // masterId() (controllers.js) -> power.js
  connected: null,          // {1,2} connected map (controllers.js) -> app.js
  channelMask: 0x3F,        // shared host-side channel-enable mask (app.js <-> power.js)
  maskChanged: null,        // fn (app.js) <- power.js — re-render every mask mirror
  filamentController: null, // fn: filament idx -> controller id (mapping.js) -> app.js
  refreshRunGate: null,     // fn (app.js) <- controllers.js
  invalidateRun: null,      // fn (app.js) <- controllers.js
  redrawGantt: null,        // fn (app.js) <- power.js
  i2cGetMask: null,         // fn (app.js) <- power.js
  i2cSetMask: null,         // fn (app.js) <- power.js
  renderBoardMask: null,    // fn (power.js) <- app.js
  refreshBoards: null,      // fn (power.js) <- app.js
  syncHvButtons: null,      // fn (power.js) <- tests.js
  testRunning: false,       // shared busy flag (power.js/tests.js)
  scheduleRunning: false,   // a REAL hardware schedule is armed/running (app.js's
                             // runMonitorTimer/pollRunStatus) -> power.js; distinct
                             // from testRunning (Cal & Test owns the master, not a
                             // scan). Used to gate the fast board-poll rate: fine to
                             // go fast normally, but a real run already gets its own
                             // dedicated status polling and firing shouldn't also
                             // compete with a fast INA sweep for the link.
};
