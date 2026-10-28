#!/usr/bin/env python3
"""Draw the timing figures the API reference shows (docs/img/*.svg).

    python3 scripts/make_doc_figures.py

Plain SVG, no dependencies. The docstrings link them as ../img/<name>.svg
(relative to docs/api/ct/client.html); scripts/make_api_docs.sh copies
docs/img into docs/api/img. The SVGs are committed so the README on GitHub
shows them too. Edit the numbers here, not the SVGs.

Colour = what happens when that timeout runs out:
    STOP  -- the whole run stops (red)
    SKIP  -- that filament is skipped, the run goes on (amber)
    WAIT  -- a wait, not a timeout (slate)
"""
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "docs" / "img"

FONT = ('system-ui,-apple-system,&quot;Segoe UI&quot;,Roboto,&quot;Helvetica Neue&quot;,'
        'Arial,sans-serif')
MONO = 'SFMono-Regular,Menlo,Monaco,Consolas,monospace'
INK, MUTED, RULE, BAND = "#1e293b", "#64748b", "#cbd5e1", "#e2e8f0"
TRACE, PULSE, WAITFILL = "#0f766e", "#334155", "#f1f5f9"
STOP, SKIP, WAIT = "#b91c1c", "#b45309", "#475569"


class Svg:
    def __init__(self, w, h, title):
        self.w, self.h, self.parts = w, h, []
        self.title = title

    def add(self, s):
        self.parts.append(s)

    def text(self, x, y, s, size=15, color=INK, anchor="start", weight=400, mono=False):
        fam = MONO if mono else FONT
        s = str(s).replace("<", "&lt;").replace(">", "&gt;")
        self.add(f'<text x="{x}" y="{y}" font-family="{fam}" font-size="{size}" '
                 f'font-weight="{weight}" fill="{color}" text-anchor="{anchor}">{s}</text>')

    def line(self, x1, y1, x2, y2, color=RULE, width=1, dash=None):
        d = f' stroke-dasharray="{dash}"' if dash else ""
        self.add(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" '
                 f'stroke-width="{width}"{d}/>')

    def rect(self, x, y, w, h, fill, rx=0, stroke=None):
        s = f' stroke="{stroke}"' if stroke else ""
        self.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}"{s}/>')

    def path(self, d, color=TRACE, width=2.2):
        self.add(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="{width}" '
                 f'stroke-linejoin="round" stroke-linecap="round"/>')

    def event(self, x, y_top, y_bot, label, sub=None):
        """A dashed vertical marker with its label on top."""
        self.line(x, y_top + 8, x, y_bot, RULE, 1, "4 4")
        self.text(x, y_top - 4, label, 14.5, INK, "middle", 600)
        if sub:
            self.text(x, y_top + 9, sub, 13.5, MUTED, "middle")

    def span(self, x1, x2, y, label, color, note=None, note_side="right"):
        """A timeout drawn as a bar with end ticks; label above, note beside."""
        self.line(x1, y, x2, y, color, 3)
        self.line(x1, y - 7, x1, y + 7, color, 2)
        self.line(x2, y - 7, x2, y + 7, color, 2)
        self.add(f'<path d="M{x2 - 9},{y - 5} L{x2 - 1},{y} L{x2 - 9},{y + 5}" fill="none" '
                 f'stroke="{color}" stroke-width="2"/>')
        self.text((x1 + x2) / 2, y - 10, label, 15, color, "middle", 600, mono=True)
        if note:
            if note_side == "right":
                self.text(x2 + 12, y + 4, note, 14, MUTED)
            else:
                self.text(x1 - 12, y + 4, note, 14, MUTED, "end")

    def legend(self, y, items):
        x = 24
        for color, label in items:
            self.line(x, y, x + 26, y, color, 3)
            self.text(x + 34, y + 4, label, 14, MUTED)
            x += 34 + 7.6 * len(label) + 26

    def save(self, name):
        OUT.mkdir(parents=True, exist_ok=True)
        body = "\n  ".join(self.parts)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
               f'viewBox="0 0 {self.w} {self.h}" role="img" aria-label="{self.title}">\n'
               f'  <title>{self.title}</title>\n'
               f'  <rect width="{self.w}" height="{self.h}" fill="#ffffff"/>\n  {body}\n</svg>\n')
        (OUT / name).write_text(svg)
        print(f"-> docs/img/{name}")


LEGEND = [(STOP, "runs out: the whole run STOPS"),
          (SKIP, "runs out: this filament is skipped, the run goes on"),
          (WAIT, "a wait, not a timeout")]


def heating_panel(s, x_raise, x_arrive, x_end, x_idle, top, band_note=True,
                  wobble=None, depth=92):
    """IDLE -> VOLTAGE trace with the +-150 mV arrival band. The trace enters
    the band exactly at x_arrive (that IS the arrival)."""
    y_t, y_idle = top + 30, top + depth
    s.rect(60, y_t - 9, s.w - 120, 18, BAND)
    if band_note:
        s.text(66, y_t - 13, "ACTIVE_MV &#177; 150 mV (arrived)", 13.5, MUTED)
    s.text(54, y_idle + 4, "IDLE", 13.5, MUTED, "end")
    d = f"M60,{y_idle} L{x_raise},{y_idle} "
    d += (f"C{x_raise + 80},{y_idle} {x_arrive - 60},{y_t + 30} {x_arrive},{y_t + 9} "
          f"C{x_arrive + 14},{y_t + 5} {x_arrive + 26},{y_t + 1} {x_arrive + 44},{y_t + 1} ")
    if wobble:
        w1, w2 = wobble
        d += (f"L{w1 - 20},{y_t + 1} C{w1},{y_t + 2} {w1 + 4},{y_t + 26} {w1 + 20},{y_t + 26} "
              f"L{w2 - 20},{y_t + 26} C{w2 - 4},{y_t + 26} {w2},{y_t + 1} {w2 + 20},{y_t} ")
    d += f"L{x_end},{y_t} "
    if x_idle:
        d += f"C{x_end + 40},{y_t} {x_idle - 30},{y_idle} {x_idle},{y_idle} L{s.w - 60},{y_idle}"
    else:
        d += f"L{s.w - 60},{y_t}"
    s.path(d)
    return y_t, y_idle


def fig_turn():
    s = Svg(940, 410, "scan_view: one filament's turn and its timeouts")
    x_raise, x_arr, x_hot, x_rel, x_ret, x_done = 110, 280, 400, 520, 690, 840
    top = 44
    s.rect(x_hot, top + 8, x_rel - x_hot, 250, WAITFILL)
    s.text((x_hot + x_rel) / 2, top + 120, "waits for", 14, MUTED, "middle")
    s.text((x_hot + x_rel) / 2, top + 136, "its turn", 14, MUTED, "middle")
    heating_panel(s, x_raise, x_arr, x_done + 20, x_done + 70, top)
    s.text(s.w - 30, top + 108, "IDLE once the next", 13.5, MUTED, "end")
    s.text(s.w - 30, top + 123, "one is released", 13.5, MUTED, "end")
    # pulses
    yp = top + 128
    for i in range(9):
        s.rect(x_ret + 6 + i * 17, yp, 7, 16, PULSE, 1)
    s.text(x_ret + 6 + 4.5 * 17, yp + 32, "its num_pulses pulses", 13.5, MUTED, "middle")
    for x, lab in ((x_raise, "raise to VOLTAGE"), (x_arr, "first arrival"), (x_hot, "HOT"),
                   (x_rel, "release + on_armed(A)"), (x_done, "all pulses counted")):
        s.event(x, top, 356, lab)
    s.span(x_raise, x_arr, 200 + 26, "heat_timeout_s", SKIP, None)
    s.span(x_arr, x_hot, 200 + 26, "settle_ms", WAIT, None)
    s.span(x_rel, x_ret, 246 + 26, "on_armed_timeout_s", STOP, None)
    s.text((x_rel + x_ret) / 2, 246 + 26 + 18, "until on_armed RETURNS", 13.5, MUTED, "middle")
    s.span(x_rel, x_done, 306 + 26, "shot_wait_timeout_s", SKIP, None)
    s.text((x_rel + x_done) / 2, 306 + 26 + 18, "until ALL its pulses are counted; else the rest "
           "of A is skipped", 13.5, MUTED, "middle")
    s.legend(392, LEGEND)
    s.save("scan_view_turn.svg")


def fig_edges():
    s = Svg(940, 400, "scan_view: the edges of one filament")
    x_rel, x_arm = 90, 170
    edges = [300, 460, 620, 780]
    w = 42
    yp = 70
    s.line(60, yp + 34, s.w - 50, yp + 34, RULE, 1)
    for e in edges:
        s.rect(e, yp, w, 34, PULSE, 2)
    s.event(x_rel, 30, 350, "release")
    s.event(x_arm, 30, 350, "on_armed(A)")
    for i, e in enumerate(edges[:3]):
        s.event(e, 30, 350, f"edge {i + 1}")
    s.span(x_rel, edges[0], 150, "not timed by the firmware", WAIT,
           None)
    s.text((x_rel + edges[0]) / 2, 172, "(the host's shot_wait_timeout_s covers it)", 13.5,
           MUTED, "middle")
    s.span(edges[0], edges[0] + w, 214, "width_us", WAIT, "&#8804; max_on_ms (checked at ARM)")
    s.span(edges[0], edges[1], 262, "gap", WAIT,
           None)
    s.text((edges[0] + edges[1]) / 2, 284, "sim: pulse_interval_ms  |  ext: your source", 13.5,
           MUTED, "middle")
    # the deadline window after edge 2
    dl = edges[1] + 230
    s.span(edges[1], dl, 330, "trigger_timeout_ms", STOP,
           "next edge before this", )
    s.text(dl + 12, 346, "(restarts at every edge)", 13.5, MUTED)
    s.legend(384, LEGEND)
    s.save("scan_view_edges.svg")


def fig_settle():
    s = Svg(940, 420, "settle_ms: fire_single_pulse vs scan_view")
    x_raise, x_arr, w1, w2 = 100, 250, 400, 470
    S = 230
    top = 40
    y_t, y_idle = heating_panel(s, x_raise, x_arr, s.w - 60, None, top, wobble=(w1, w2),
                                depth=150)
    s.text(w2 + 34, y_t + 30, "&#8592; a wobble out of the band", 13.5, MUTED)
    y_drop = y_t + 70
    s.line(60, y_drop, s.w - 60, y_drop, STOP, 1, "6 4")
    s.text(s.w - 60, y_drop + 16, "target &#8722; max_voltage_drop_mv: below this on 3 reads = "
                                  "interrupted (scan_view only)", 13.5, STOP, "end")
    s.event(x_raise, top - 6, 390, "VOLTAGE")
    s.event(x_arr, top - 6, 390, "first arrival")
    yA, yB = 270, 370
    s.text(24, yA - 34, "fire_single_pulse(settle_ms=...): held CONTINUOUSLY", 15, INK, weight=600)
    s.span(x_arr, w1 + 10, yA, "clock", WAIT, None)
    s.text(w1 + 22, yA + 4, "restarts", 14, STOP, weight=600)
    s.span(w2 + 10, w2 + 10 + S, yA, "settle_ms", WAIT, "settled: on_armed + trigger")
    s.text(24, yB - 34, "scan_view(settle_ms=...): HOT, from the FIRST arrival", 15, INK,
           weight=600)
    s.span(x_arr, x_arr + S, yB, "settle_ms", WAIT, "HOT: may fire (the wobble did not restart it)")
    s.save("settle_ms.svg")


def fig_single():
    s = Svg(940, 280, "fire_single_pulse: ARM, triggers and their limits")
    x_arm = 90
    trig = [330, 470, 610, 750]
    yp = 60
    s.line(60, yp + 30, s.w - 50, yp + 30, RULE, 1)
    for t in trig:
        s.rect(t, yp, 30, 30, PULSE, 2)
    s.event(x_arm, 30, 236, "ARM")
    for i, t in enumerate(trig[:3]):
        s.event(t, 30, 236, f"trigger {i + 1}")
    s.span(x_arm, trig[0], 136, "wait for trigger 1", WAIT, None)
    s.text((x_arm + trig[0]) / 2, 156, "(Armed: only total_ms limits it)", 13.5, MUTED,
           "middle")
    s.span(trig[0], trig[1], 136, "gap 1", STOP, None)
    s.span(trig[1], trig[2], 136, "gap 2", STOP, None)
    s.text((trig[0] + trig[2]) / 2, 174, "each gap < trigger_timeout_ms (sim: gap = "
                                         "pulse_interval_ms)", 13.5, MUTED, "middle")
    s.span(x_arm, trig[3] + 30, 222, "total_ms", STOP, "ARM to the last pulse")
    s.legend(264, LEGEND[:1] + LEGEND[2:])
    s.save("fire_single_pulse_triggers.svg")


if __name__ == "__main__":
    fig_turn()
    fig_edges()
    fig_settle()
    fig_single()
