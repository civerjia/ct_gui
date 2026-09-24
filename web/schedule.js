/*
 * Virtualized schedule table — shared by the Emission and Heating views.
 *
 * Columns are configurable via setColumns(headers, cellFn). Only the rows in
 * view are rendered (the rest are a scroll spacer), so an 8192-row schedule
 * stays smooth. setActive() highlights/scrolls the live row.
 */

const ROW_H = 24;

export function initScheduleTable({ body, head, onRowClick }) {
  body.classList.add('sch-body');
  const spacer = document.createElement('div');
  spacer.className = 'sch-spacer';
  const layer = document.createElement('div');
  layer.className = 'sch-layer';
  body.append(spacer, layer);

  let rows = [];
  let active = -1;
  let cell = (r) => [r.seq, r.filament, r.pulses, `${r.duration} µs`];

  function renderVisible() {
    const top = body.scrollTop, h = body.clientHeight || 440;
    const first = Math.max(0, Math.floor(top / ROW_H) - 4);
    const last = Math.min(rows.length, Math.ceil((top + h) / ROW_H) + 4);
    layer.textContent = '';
    const frag = document.createDocumentFragment();
    for (let i = first; i < last; i++) {
      const r = rows[i];
      const el = document.createElement('div');
      el.className = 'sch-row' + (i === active ? ' active' : '');
      el.style.top = (i * ROW_H) + 'px';
      el.innerHTML = cell(r).map((c) => `<span>${c}</span>`).join('');
      el.addEventListener('click', () => onRowClick && onRowClick(r));
      frag.appendChild(el);
    }
    layer.appendChild(frag);
  }

  body.addEventListener('scroll', renderVisible);

  return {
    setColumns(headers, cellFn) {
      if (head) head.innerHTML = headers.map((h) => `<span>${h}</span>`).join('');
      cell = cellFn;
      renderVisible();
    },
    setRows(r) {
      rows = r;
      spacer.style.height = (rows.length * ROW_H) + 'px';
      renderVisible();
    },
    setActive(row) {
      active = row;
      if (row >= 0 && rows.length) {
        const t = row * ROW_H, b = t + ROW_H;
        if (t < body.scrollTop) body.scrollTop = t;
        else if (b > body.scrollTop + body.clientHeight) body.scrollTop = b - body.clientHeight;
      }
      renderVisible();
    },
  };
}
