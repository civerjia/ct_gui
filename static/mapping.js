/*
 * Filament → power mapping editor (firmware ACTIVE-LIST model).
 *
 * Each global filament 0-95 is driven by exactly one power controller. A
 * controller packs its filaments into power slots 0-63 (slot k = channel k>>3 /
 * position k&7) in ascending filament order; the host downloads that as the
 * 64-byte ShvSetActiveList. Default = alternating groups of N. This panel edits
 * the per-filament controller assignment and uploads the active list.
 */

const mapApi = async (path, opts) => (await fetch(path, opts)).json();
const mapPost = (body) => mapApi('/api/mapping', {
  method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
});

let mapAssign = new Array(96).fill(null);   // 0 = Power 1, 1 = Power 2, null = unassigned
let mapGroupSize = 12;
let mapRows = {};                            // last server slot/ch/pos detail per filament

function renderMapGrid() {
  const grid = document.getElementById('mapGrid'); if (!grid) return;
  grid.innerHTML = '';
  for (let f = 0; f < 96; f++) {
    const c = mapAssign[f];
    const cell = document.createElement('button');
    cell.className = 'map-cell ' + (c === 0 ? 'p1' : c === 1 ? 'p2' : 'none');
    cell.textContent = f;
    const r = mapRows[f];
    const slotTxt = (r && r.slot != null) ? ` · slot ${r.slot} (CH${r.channel + 1}.${r.position + 1})` : '';
    cell.title = `Filament ${f} → ${c === 0 ? 'Power 1' : c === 1 ? 'Power 2' : 'unassigned'}${slotTxt}\nClick: P1 → P2 → unassigned`;
    cell.addEventListener('click', () => {
      mapAssign[f] = c === 0 ? 1 : c === 1 ? null : 0;   // cycle P1 → P2 → unassigned
      mapRows = {};                                       // slots are stale until Apply recomputes
      renderMapGrid(); updateMapCounts();
    });
    grid.appendChild(cell);
  }
}

function updateMapCounts() {
  const p1 = mapAssign.filter((c) => c === 0).length;
  const p2 = mapAssign.filter((c) => c === 1).length;
  const none = mapAssign.filter((c) => c == null).length;
  const el = document.getElementById('mapCounts');
  if (el) el.textContent = `P1 ${p1} · P2 ${p2}${none ? ' · ' + none + ' unassigned' : ''}`;
  const st = document.getElementById('mapStatus');
  if (st && !st.dataset.busy) {
    const over = [p1 > 64 ? 'P1>64' : '', p2 > 64 ? 'P2>64' : ''].filter(Boolean);
    st.textContent = over.length ? `⚠ ${over.join(' ')} — exceeds 64 power slots; extra filaments can't fire.` : '';
    st.className = 'summary' + (over.length ? ' bad' : '');
  }
}

function ingestMapping(meta) {
  if (!meta) return;
  mapGroupSize = meta.group_size || mapGroupSize;
  const g = document.getElementById('mapGroup'); if (g) g.value = mapGroupSize;
  mapAssign = new Array(96).fill(null);
  mapRows = {};
  (meta.filaments || []).forEach((r) => {
    mapAssign[r.filament] = (r.controller === 0 || r.controller === 1) ? r.controller : null;
    mapRows[r.filament] = r;
  });
  window.ctFilamentController = (f) => mapAssign[f];   // expose for the ring / other modules
  renderMapGrid(); updateMapCounts();
}

export async function initMapping() {
  const altBtn = document.getElementById('mapAltBtn');
  const applyBtn = document.getElementById('mapApplyBtn');
  const groupInp = document.getElementById('mapGroup');
  if (altBtn) altBtn.addEventListener('click', () => {
    const g = Math.max(1, Math.min(96, parseInt(groupInp.value, 10) || 12));
    mapGroupSize = g;
    for (let f = 0; f < 96; f++) mapAssign[f] = Math.floor(f / g) % 2;   // 0=P1, 1=P2
    mapRows = {}; renderMapGrid(); updateMapCounts();
  });
  if (applyBtn) applyBtn.addEventListener('click', async () => {
    const st = document.getElementById('mapStatus');
    if (st) { st.dataset.busy = '1'; st.className = 'summary'; st.textContent = 'Applying + uploading active list…'; }
    let res;
    try { res = await mapPost({ assignment: mapAssign, group_size: mapGroupSize, upload: true }); }
    catch (e) { if (st) { delete st.dataset.busy; st.className = 'summary bad'; st.textContent = 'Apply failed: ' + e; } return; }
    if (res && res.mapping) ingestMapping(res.mapping);
    const up = Object.entries((res && res.uploaded) || {}).map(([k, r]) => `P${k}: ${r.ok ? '✓' : (r.error || '✗')}`);
    if (st) {
      delete st.dataset.busy;
      st.className = 'summary' + (res && res.ok ? '' : ' bad');
      st.textContent = (res && res.ok ? 'Saved' : 'Save failed')
        + (up.length ? ' — uploaded ' + up.join(' · ') : ' (no controller connected — saved on host)');
    }
  });
  try {
    const j = await mapApi('/api/mapping');
    if (j && j.mapping) ingestMapping(j.mapping);
  } catch { /* leave default grid */ }
}
