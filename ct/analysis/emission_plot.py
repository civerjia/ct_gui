"""Plot an emission_vs_heating() run, and leave the figures on disk with it.

    python3 -m ct.analysis.emission_plot calibration/richardson_20260922_140130.json
    python3 -m ct.analysis.emission_plot --r-lead 0.20 --latest   (from the repository root)

The figures are part of the RECORD, not a viewer: they are written next to the
JSON/CSV the run already produced, at the same basename, so a result and its
plots stay together and a later reader does not have to re-derive them.

Every figure carries its own conditions -- emission voltage, pulse width, the
assumed lead and cold resistances, the pedestal that was subtracted. A plot
without those is not a record of anything: the same filament plotted at a
different assumed R_lead is a different curve, and nothing in the picture would
say which one it is.

matplotlib is an OPTIONAL dependency of this repo (backend.py and
ct_simple_control.py stay stdlib-only). Only this module needs it.
"""
from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

try:
    import matplotlib
    matplotlib.use("Agg")           # headless: this runs on the bench machine
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
except ImportError:                  # pragma: no cover - reported, not raised
    plt = None

from ct.client import CTClient
from ct.paths import CALIB_DIR


# Palette from the subject: the filament's own incandescence over the measured
# range. Point colour IS the measured temperature, so it carries data.
EMBER_LO, EMBER_HI = (0.925, 0.376, 0.094), (1.0, 0.698, 0.329)
COOL = "#5b7183"
DROPPED = "#b4aaa0"
WARN = "#9a6b12"
INK = "#1b1613"
FAINT = "#8d857c"


def ember(t_k: float, lo: float = 1980.0, hi: float = 2360.0):
    """Blackbody-ish colour for a filament temperature."""
    u = max(0.0, min(1.0, (float(t_k) - lo) / (hi - lo)))
    return tuple(a + u * (b - a) for a, b in zip(EMBER_LO, EMBER_HI))


def _style(ax, xlabel, ylabel, title=None):
    ax.set_xlabel(xlabel, fontsize=9)
    ax.set_ylabel(ylabel, fontsize=9)
    if title:
        ax.set_title(title, fontsize=10, loc="left", color=INK, pad=8)
    ax.grid(True, lw=0.5, color="#e3ddd5", zorder=0)
    ax.set_axisbelow(True)
    ax.tick_params(labelsize=8.5, color="#c9c1b8")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color("#c9c1b8")


def _conditions(result: dict, r_lead_ohm: float, r_cold_ohm: float) -> str:
    p = result.get("params") or {}
    ped = result.get("pedestal_ma")
    bits = [f"filament {result.get('filament')}"]
    if p.get("emission_v") is not None:
        bits.append(f"Vem −{abs(p['emission_v']):.0f} V")
    if p.get("width_us"):
        bits.append(f"{p['width_us']} µs")
    if p.get("pulses_per_point"):
        bits.append(f"{p['pulses_per_point']}×/point")
    if ped is not None:
        bits.append(f"pedestal −{ped:.3f} mA")
    bits.append(f"R_lead {r_lead_ohm:.3f} Ω")
    bits.append(f"R_cold {r_cold_ohm:.3f} Ω")
    if result.get("ref_mv"):
        bits.append(f"ref {result['ref_mv']:.1f} mV")
    return "  ·  ".join(bits)


# ── the panels ───────────────────────────────────────────────────────────────

def panel_raw(ax, result):
    """Net current and emission against heating current, with the pedestal."""
    pts = [p for p in result["points"] if p.get("heat_mA") is not None]
    x = [p["heat_mA"] for p in pts]
    ped = result.get("pedestal_ma")
    sd = ((result.get("pedestal") or {}).get("sd_ma")
          if isinstance(result.get("pedestal"), dict) else None)
    if ped is not None:
        if sd:
            ax.axhspan(ped - 3 * sd, ped + 3 * sd, color=EMBER_HI, alpha=0.22, zorder=1)
        ax.axhline(ped, color=EMBER_HI, lw=1.3, ls="--", zorder=2)
        ax.annotate(f"pedestal {ped:.3f} mA", (min(x), ped), xytext=(4, 5),
                    textcoords="offset points", fontsize=8, color=FAINT)
    net = [p.get("net_ma") for p in pts]
    if any(v is not None for v in net):
        ax.plot(x, net, "-o", color=COOL, lw=1.4, ms=3.4, zorder=3,
                label="net (pedestal included)")
    emis = [p.get("emission_ma") for p in pts]
    if any(v is not None for v in emis):
        ax.plot(x, emis, "-o", color=EMBER_LO, lw=2.0, ms=4.6, zorder=4,
                label="emission (pedestal removed)")
    _style(ax, "heating current at pulse (mA)", "current (mA)",
           "1 · the raw curve, and the floor under it")
    ax.legend(fontsize=8, frameon=False, loc="upper left")


def panel_vs_t(ax, result, fit):
    """Emission against temperature, log y. Dropped points hollow."""
    if not fit.get("ok"):
        ax.text(.5, .5, "no fit — " + (fit.get("warnings") or ["?"])[0][:60],
                ha="center", va="center", fontsize=9, color=WARN, wrap=True)
        _style(ax, "filament temperature (K)", "emission current (mA)",
               "2 · emission against temperature")
        return
    used = {p["heat_mA"]: p for p in fit["points"]}
    kept = sorted(fit["points"], key=lambda p: p["T_K"])
    ax.plot([p["T_K"] for p in kept], [p["emission_ma"] for p in kept],
            "-", color=EMBER_LO, lw=1.6, alpha=.55, zorder=2)
    for p in kept:
        ax.plot(p["T_K"], p["emission_ma"], "o", ms=6,
                color=ember(p["T_K"]), zorder=4)
    # Dropped points still get drawn -- hollow, at the temperature the same
    # reduction gives them. Hiding them would make the sweep look shorter than
    # it was, and the reason they were dropped is the interesting part.
    for p in result["points"]:
        if p.get("heat_mA") in used or p.get("emission_ma") is None:
            continue
        t = _temperature_of(p, fit)
        if t is None or p["emission_ma"] <= 0:
            continue
        ax.plot(t, p["emission_ma"], "o", ms=5, mfc="none",
                mec=DROPPED, mew=1.4, zorder=3)
    ax.set_yscale("log")
    _style(ax, "filament temperature (K)", "emission current (mA)",
           "2 · emission against temperature")
    # The "dropped" key only appears when something WAS dropped -- a legend
    # entry for an absent class reads as "these exist somewhere on the plot".
    handles = [Line2D([], [], marker="o", ls="", color=EMBER_LO, ms=6, label="fitted")]
    if fit.get("dropped"):
        handles.append(Line2D([], [], marker="o", ls="", mfc="none", mec=DROPPED,
                              mew=1.4, ms=5, label="dropped — below the pedestal noise"))
    ax.legend(handles=handles, fontsize=8, frameon=False, loc="upper left")


def _temperature_of(point, fit):
    """The temperature this point would have under the fit's own reduction."""
    if not point.get("r_total_ohm"):
        return None
    r_fil = point["r_total_ohm"] - fit["r_lead_ohm"]
    if r_fil <= 0:
        return None
    return CTClient.tungsten_temperature(r_fil / fit["r_cold_ohm"])


def panel_richardson(ax, fits, highlight):
    """ln(I/T^2) vs 1/T at several lead resistances -- all equally straight."""
    cols = [COOL, EMBER_LO, WARN]
    keys = sorted(fits)[:: max(1, (len(fits) - 1) // 2)][:3] or sorted(fits)
    for i, k in enumerate(keys):
        f = fits[k]
        if not f.get("ok"):
            continue
        xs = [p["inv_T"] * 1e4 for p in f["points"]]
        ys = [p["ln_i_over_t2"] for p in f["points"]]
        n = len(xs)
        mx, my = sum(xs) / n, sum(ys) / n
        sxx = sum((v - mx) ** 2 for v in xs)
        m = sum((a - mx) * (b - my) for a, b in zip(xs, ys)) / sxx
        c = my - m * mx
        lo, hi = min(xs) - .03 * (max(xs) - min(xs)), max(xs) + .03 * (max(xs) - min(xs))
        lw = 2.2 if abs(f["r_lead_ohm"] - highlight) < 1e-9 else 1.3
        ax.plot([lo, hi], [m * lo + c, m * hi + c], "-", color=cols[i % 3],
                lw=lw, alpha=.85, zorder=2)
        ax.plot(xs, ys, "o", ms=4.6, color=cols[i % 3], zorder=3,
                label=f"R_lead {f['r_lead_ohm']:.2f} Ω · φ {f['work_function_eV']:.3f} eV"
                      f" · r² {f['r_squared']:.5f}")
    _style(ax, "10⁴ / T  (K⁻¹)", "ln( I / T² )",
           "3 · Richardson–Dushman — the same data at three lead resistances")
    ax.legend(fontsize=8, frameon=False, loc="upper right")


def panel_sensitivity(ax, fits):
    """Work function and fit quality against the ASSUMED lead resistance."""
    ok = [fits[k] for k in sorted(fits) if fits[k].get("ok")]
    if len(ok) < 2:
        ax.text(.5, .5, "not enough fits to show the sensitivity",
                ha="center", va="center", fontsize=9, color=WARN)
        _style(ax, "assumed R_lead (Ω)", "work function (eV)", "4 · what the fit cannot see")
        return
    rl = [f["r_lead_ohm"] for f in ok]
    ax.plot(rl, [f["work_function_eV"] for f in ok], "-o", color=EMBER_LO, lw=2, ms=4.5)
    _style(ax, "assumed R_lead (Ω)", "work function (eV)",
           "4 · what the fit cannot see")
    ax2 = ax.twinx()
    r2 = [f["r_squared"] for f in ok]
    ax2.plot(rl, r2, "--", color=COOL, lw=1.4)
    # The r^2 axis is deliberately NOT autoscaled to the data alone: the whole
    # finding is that it barely moves, and an axis that zooms into the 5th
    # decimal would draw that as a dramatic slope.
    span = max(r2) - min(r2)
    mid = (max(r2) + min(r2)) / 2
    ax2.set_ylim(mid - max(span, 2e-4) * 3, mid + max(span, 2e-4) * 3)
    ax2.set_ylabel("r²", fontsize=9, color=COOL)
    ax2.tick_params(labelsize=8, colors=COOL)
    ax2.spines["top"].set_visible(False)
    ax.annotate(f"r² spans only {min(r2):.5f}–{max(r2):.5f}\nacross the whole range",
                (0.5, 0.06), xycoords="axes fraction", ha="center",
                fontsize=8, color=FAINT)


def panel_residuals(ax, fit):
    if not fit.get("ok"):
        ax.text(.5, .5, "no fit", ha="center", va="center", fontsize=9, color=WARN)
        _style(ax, "filament temperature (K)", "residual in ln( I / T² )", "5 · residuals")
        return
    ax.axhline(0, color="#c9c1b8", lw=1.2, zorder=1)
    for p in fit["points"]:
        ax.plot([p["T_K"], p["T_K"]], [0, p["residual"]], "-",
                color=ember(p["T_K"]), lw=1.3, alpha=.6, zorder=2)
        ax.plot(p["T_K"], p["residual"], "o", ms=5, color=ember(p["T_K"]), zorder=3)
    _style(ax, "filament temperature (K)", "residual in ln( I / T² )",
           f"5 · residuals at R_lead {fit['r_lead_ohm']:.2f} Ω")


def panel_summary(ax, result, fit):
    """The numbers, in the figure. A plot that needs its caption to be read is
    not a record."""
    ax.axis("off")
    if not fit.get("ok"):
        ax.text(0, 1, "FIT FAILED", fontsize=11, weight="bold", color=WARN, va="top")
        ax.text(0, .88, "\n".join(f"• {w}" for w in (fit.get("warnings") or []))[:600],
                fontsize=8, color=INK, va="top", wrap=True)
        return
    temps = [p["T_K"] for p in fit["points"]]
    s = fit.get("sensitivity_to_r_lead") or {}
    rows = [
        ("temperature", f"{min(temps):.0f} – {max(temps):.0f} K"),
        ("work function", f"{fit['work_function_eV']:.3f} eV"),
        ("A_eff", f"{fit['richardson_a_eff_ma_per_k2']:.4g} mA/K²"),
        ("r²", f"{fit['r_squared']:.5f}  ({fit['n_points']} points)"),
        ("dropped", f"{len(fit.get('dropped') or [])} below the pedestal noise"),
    ]
    if s:
        rows.append(("per +0.1 Ω lead",
                     f"φ {s['d_work_function_eV_per_ohm'] * .1:+.3f} eV, "
                     f"T {s['d_mean_T_K_per_ohm'] * .1:+.0f} K"))
    y = 1.0
    ax.text(0, y, "6 · result", fontsize=10, color=INK, va="top")
    y -= .13
    for k, v in rows:
        ax.text(0, y, k, fontsize=8.5, color=FAINT, va="top")
        ax.text(.42, y, v, fontsize=9.5, color=INK, va="top", family="monospace")
        y -= .105
    y -= .03
    # Wrapped by hand: matplotlib's wrap=True measures against the FIGURE, not
    # this axes, so a long warning ran off the right edge of the page instead
    # of folding. A warning that leaves the paper is not a warning.
    for w in (fit.get("warnings") or [])[:3]:
        lines = textwrap.wrap("⚠ " + w, width=64)[:4]
        for ln in lines:
            ax.text(0, y, ln, fontsize=7.5, color=WARN, va="top", family="monospace")
            y -= .045
        y -= .025


# ── the figure ───────────────────────────────────────────────────────────────

def plot_emission_run(result: dict, out_base, r_lead_ohm: float = 0.20,
                      r_cold_ohm: float = 0.257, r_lead_scan=(0.0, 0.1, 0.2, 0.3, 0.4),
                      min_snr: float = 5.0, formats=("png", "pdf")) -> dict:
    """Render one emission_vs_heating() result. Returns {"paths": [...], "fit": {...}}.

    `result` is the dict emission_vs_heating() returns, or one filament's slice
    of a saved sweep (see load_saved()). `out_base` is a path WITHOUT suffix;
    the figure is written at that basename in each requested format.
    """
    if plt is None:
        return {"ok": False, "paths": [],
                "error": "matplotlib is not installed (pip install matplotlib) — "
                         "it is an optional dependency, only this module needs it"}
    ct = CTClient()                     # offline: no request is made by the fit
    fits = {}
    for rl in r_lead_scan:
        f = ct.fit_richardson(result, r_cold_ohm=r_cold_ohm, r_lead_ohm=rl,
                              min_snr=min_snr)
        f.setdefault("r_lead_ohm", rl)
        f.setdefault("r_cold_ohm", r_cold_ohm)
        fits[f"{rl:.2f}"] = f
    main = ct.fit_richardson(result, r_cold_ohm=r_cold_ohm,
                             r_lead_ohm=r_lead_ohm, min_snr=min_snr)
    main.setdefault("r_lead_ohm", r_lead_ohm)
    main.setdefault("r_cold_ohm", r_cold_ohm)

    fig, axes = plt.subplots(3, 2, figsize=(12.5, 13.5))
    fig.patch.set_facecolor("white")
    panel_raw(axes[0][0], result)
    panel_vs_t(axes[0][1], result, main)
    panel_richardson(axes[1][0], fits, r_lead_ohm)
    panel_sensitivity(axes[1][1], fits)
    panel_residuals(axes[2][0], main)
    panel_summary(axes[2][1], result, main)

    fig.suptitle("Thermionic emission vs heating current", fontsize=14,
                 x=0.012, ha="left", y=0.992, color=INK)
    fig.text(0.012, 0.972, _conditions(result, r_lead_ohm, r_cold_ohm),
             fontsize=8.5, color=FAINT, family="monospace")
    fig.tight_layout(rect=(0, 0, 1, 0.962))

    out_base = Path(out_base)
    out_base.parent.mkdir(parents=True, exist_ok=True)
    paths = []
    for ext in formats:
        p = out_base.with_suffix("." + ext)
        fig.savefig(p, dpi=160, facecolor="white")
        paths.append(str(p))
    plt.close(fig)
    return {"ok": True, "paths": paths, "fit": main}


def load_saved(path) -> dict:
    """Read a save_emission_curves() JSON back into {filament: result}.

    The saved shape splits points and per-shot detail into separate sections
    for the CSV's sake; this puts them back together so the plotting code sees
    the same dict emission_vs_heating() returned.
    """
    d = json.loads(Path(path).read_text())
    out = {}
    for fil, curve in (d.get("curves") or {}).items():
        meta = (d.get("per_filament") or {}).get(fil) or {}
        shots = (d.get("pulses") or {}).get(fil) or []
        by_cmd = {}
        for s in shots:
            by_cmd.setdefault(s.get("commanded_ma"), []).append(s)
        pts = [{**p, "pulses": by_cmd.get(p.get("commanded_ma"), [])} for p in curve]
        params = d.get("params") or {}
        # The pedestal block (sigma included) is restored from the saved
        # metadata, so an offline replay reduces to the SAME numbers the live
        # run reported. Older files predate it and carry only the value; the
        # fit then falls back to each point's own scatter, which shifts the
        # significance cut -- flagged rather than silently accepted.
        ped = meta.get("pedestal")
        out[int(fil)] = {
            "filament": int(fil), "points": pts, "params": params,
            "pedestal_ma": meta.get("pedestal_ma",
                                    pts[0].get("pedestal_ma") if pts else None),
            "pedestal": ped,
            "pedestal_sigma_known": bool(isinstance(ped, dict) and ped.get("sd_ma")),
            "ok": meta.get("ok"), "problems": meta.get("problems"),
            "active_s": meta.get("active_s"), "end_state": meta.get("end_state"),
            "ref_mv": meta.get("ref_mv", params.get("ref_mv")),
        }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("json", nargs="?", help="a save_emission_curves() JSON")
    ap.add_argument("--latest", action="store_true",
                    help="use the newest JSON in calibration/ instead")
    ap.add_argument("--dir", default=str(CALIB_DIR))   # <repo>/calibration, from any cwd
    ap.add_argument("--r-lead", type=float, default=0.20,
                    help="assumed lead resistance, Ω (default: 0.20). The "
                         "emission curve cannot determine this — take it from "
                         "the I-V measurement")
    ap.add_argument("--r-cold", type=float, default=0.257,
                    help="filament resistance at 293 K, Ω (default: 0.257)")
    ap.add_argument("--min-snr", type=float, default=5.0)
    ap.add_argument("--formats", default="png,pdf")
    args = ap.parse_args(argv)

    if plt is None:
        print("matplotlib is not installed — pip install matplotlib")
        return 2
    path = args.json
    if args.latest or not path:
        cands = sorted(Path(args.dir).glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not cands:
            print(f"no JSON found in {args.dir}/")
            return 2
        path = cands[-1]
    runs = load_saved(path)
    if not runs:
        print(f"{path} carries no curves")
        return 2
    base = Path(path).with_suffix("")
    rc = 0
    for fil, result in sorted(runs.items()):
        out = plot_emission_run(
            result, f"{base}_f{fil}", r_lead_ohm=args.r_lead,
            r_cold_ohm=args.r_cold, min_snr=args.min_snr,
            formats=tuple(x.strip() for x in args.formats.split(",") if x.strip()))
        if not out["ok"]:
            print(f"filament {fil}: {out['error']}")
            rc = 1
            continue
        f = out["fit"]
        if f.get("ok"):
            t = [p["T_K"] for p in f["points"]]
            print(f"filament {fil}: T {min(t):.0f}–{max(t):.0f} K, "
                  f"φ {f['work_function_eV']:.3f} eV, r² {f['r_squared']:.5f}, "
                  f"{f['n_points']} points")
        else:
            print(f"filament {fil}: fit failed — "
                  f"{(f.get('warnings') or ['?'])[0][:90]}")
            rc = 1
        for p in out["paths"]:
            print(f"    {p}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
