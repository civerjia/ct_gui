/*
 * Shared fetch helpers. Was independently reinvented three times
 * (postJ in power.js, tPostJ+tGetJ in tests.js, postJSON in app.js) with a
 * real behavioral gap: app.js's copy had no try/catch, so a network failure
 * (bridge drops mid-request) threw an unhandled rejection instead of
 * resolving to {ok:false,error} like the other two — every one of its ~15
 * call sites relied on an outer try/catch to paper over that. One
 * implementation now, always resolves to an object with `ok`.
 */

export const postJ = async (path, body) => {
  try {
    return await (await fetch(path, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
    })).json();
  } catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
};

export const getJ = async (path) => {
  try { return await (await fetch(path)).json(); }
  catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
};

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
