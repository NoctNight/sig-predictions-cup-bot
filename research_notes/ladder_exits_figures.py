"""Decision figure for ladder_exits_study.py (research_notes/figures/ladder_exits_decision.png)."""
from __future__ import annotations

import math
import os
import textwrap

import numpy as np

FIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "research_notes", "figures")
DELTAS = [3, 2, 1, 0, -1, -2, -3, -4, -6]


def _header(ax, title, subtitle=None):
    ax.axis("off")
    ax.text(0.5, 1.0, title, ha="center", va="top", fontsize=15, fontweight="bold", transform=ax.transAxes)
    if subtitle:
        ax.annotate(subtitle, xy=(0.5, 1.0), xycoords="axes fraction", xytext=(0, -24), textcoords="offset points",
                    ha="center", va="top", fontsize=11, color="0.25")


def _glossary(ax, lines, ncol=2, width=62, fontsize=9):
    from matplotlib.patches import FancyBboxPatch
    ax.axis("off")
    ax.add_patch(FancyBboxPatch((0, 0), 1, 1, boxstyle="round,pad=0,rounding_size=0.01", transform=ax.transAxes,
                                facecolor="#f6f6f2", edgecolor="0.6", lw=0.8, clip_on=False))
    ax.text(0.01, 0.97, "Glossary", ha="left", va="top", fontsize=fontsize + 1, fontweight="bold", transform=ax.transAxes)
    wrapped = ["\n".join(textwrap.wrap(f"{t}: {e}", width=width, subsequent_indent="    ")) for t, e in lines]
    per = math.ceil(len(wrapped) / ncol)
    for c in range(ncol):
        ax.text(0.01 + c / ncol, 0.88, "\n".join(wrapped[c * per:(c + 1) * per]), ha="left", va="top",
                fontsize=fontsize, linespacing=1.35, transform=ax.transAxes)


def figure(d):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    x = np.array([-v for v in DELTAS], float)            # ticks better than the race's usual exit cost
    fig = plt.figure(figsize=(13, 27), constrained_layout=True)
    gs = fig.add_gridspec(7, 1, height_ratios=[0.35, 3, 2.4, 3.4, 3, 0.3, 1.45])
    _header(fig.add_subplot(gs[0]), "Selling arb sets in steps at better prices, or all now?",
            "Exit cost of a set vs the race's usual level; 2-leg races (top three panels) and our three big sets (bottom)")

    # A. chance of reaching each level
    ax = fig.add_subplot(gs[1])
    hz = [("real", 1, "#9ecae1"), ("real", 6, "#4292c6"), ("real", 24, "#08519c"), ("syn", 72, "#fd8d3c"), ("syn", 240, "#a63603")]
    for src, h, col in hz:
        for half, ls in (("IS", ":"), ("OOS", "-")):
            k = f"{half}|2|{h}"
            blk = d["hit_real" if src == "real" else "hit_syn"].get(k)
            if not blk:
                continue
            p = np.array([blk["p"][str(v)][0] for v in DELTAS], float)
            se = np.array([blk["p"][str(v)][1] for v in DELTAS], float)
            lab = (f"within {h} h" if h < 48 else f"within {h // 24} days (simulated)") + (" - training day" if half == "IS" else " - test day")
            ax.plot(x, p, ls=ls, color=col, marker="o" if half == "OOS" else None, ms=4, label=lab)
            if half == "OOS":
                ax.fill_between(x, p - 2 * se, p + 2 * se, color=col, alpha=0.12, lw=0)
    ax.set_title("A. How likely is the exit cost to reach a level better than usual?", loc="left", fontweight="bold")
    ax.set_ylabel("Share of start times when the level\nwas reached in time")
    ax.set_ylim(0, 1.02)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8.5, ncol=2, loc="lower left")

    # B. median wait
    ax = fig.add_subplot(gs[2], sharex=ax)
    for src, h, col in hz:
        for half, ls in (("IS", ":"), ("OOS", "-")):
            blk = d["hit_real" if src == "real" else "hit_syn"].get(f"{half}|2|{h}")
            if not blk:
                continue
            w = np.array([blk["wait"][str(v)] if blk["wait"][str(v)] is not None else np.nan for v in DELTAS], float)
            ax.plot(x, w, ls=ls, color=col, marker="o" if half == "OOS" else None, ms=4)
    ax.set_yscale("log")
    ax.set_title("B. When it is reached, how long does it take? (median; same lines as A)", loc="left", fontweight="bold")
    ax.set_ylabel("Hours until the level\nis first reached")
    ax.grid(alpha=0.3, which="both")

    # C. 2-leg pool, real paths: single level vs selling in clips from now
    ax = fig.add_subplot(gs[3], sharex=ax)
    for h, col in ((6, "#74c476"), (12, "#006d2c")):
        for half, ls in (("IS", ":"), ("OOS", "-")):
            blk = d["pool"].get(f"{half}|{h}")
            if not blk:
                continue
            m = np.array([blk[f"single level {v:+d}"]["mean"] for v in DELTAS], float)
            t = np.array([blk[f"single level {v:+d}"]["t"] for v in DELTAS], float)
            n = np.array([blk[f"single level {v:+d}"]["n_races"] for v in DELTAS], float)
            p5 = np.array([blk[f"single level {v:+d}"]["p5"] for v in DELTAS], float)
            se = np.where(np.isfinite(t) & (t != 0), np.abs(m / np.where(t == 0, 1, t)), np.nan)
            ax.plot(x, m, ls=ls, color=col, marker="o" if half == "OOS" else None, ms=4,
                    label=f"sell everything left after {h} h; {'training day' if half == 'IS' else 'test day'} (average over races)")
            if half == "OOS":
                ax.fill_between(x, m - 2 * se, m + 2 * se, color=col, alpha=0.15, lw=0)
                ax.plot(x, p5, ls="--", lw=0.9, color=col, label=f"after {h} h, test day: worst 5% of start times")
            if half == "OOS" and h == 12:
                dm = blk["all now (one clip)"]["mean"]
                ax.axhline(dm, color="0.3", lw=1, ls="-.", label=f"sell all 2,000 at once now (test day): {dm:+.2f}")
    ps = d.get("pool_syn", {})
    sd = [1, 0, -1, -2, -4]
    for half, ls in (("IS", ":"), ("OOS", "-")):
        blk = ps.get(f"{half}|240")
        if not blk:
            continue
        m = np.array([blk[f"single level {v:+d}"]["mean"] for v in sd], float)
        t = np.array([blk[f"single level {v:+d}"]["t"] for v in sd], float)
        p5 = np.array([blk[f"single level {v:+d}"]["p5"] for v in sd], float)
        xs = np.array([-v for v in sd], float)
        ax.plot(xs, m, ls=ls, color="#a63603", marker="s" if half == "OOS" else None, ms=4,
                label=f"sell everything left after 10 days (simulated); {'training-day' if half == 'IS' else 'test-day'} price swings")
        if half == "OOS":
            ax.plot(xs, p5, ls="--", lw=0.9, color="#a63603", label="after 10 days (simulated), test-day swings: worst 5% of paths")
            for xi, mi, blk_v in zip(xs, m, sd):
                ax.annotate(f"{blk[f'single level {blk_v:+d}']['time_h']:.0f} h", (xi, mi), xytext=(0, 6), textcoords="offset points",
                            ha="center", fontsize=7.5, color="#a63603")
    ax.axhline(0, color="0.5", lw=0.8)
    ax.set_title("C. 2-leg races, 2,000 sets: gain from waiting for a level, vs selling 500-set clips every 30 min from now",
                 loc="left", fontweight="bold")
    ax.set_ylabel("Better than selling in clips now\n(ticks per set)")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8.5, loc="lower left")

    # D. our three positions, simulated to the 3 Nov deadline (10 days), clips of 2,000
    ax = fig.add_subplot(gs[4], sharex=ax)
    cols = {"Rhode Island Governor": "#6a51a3", "MN-05 House race": "#d94801", "Hawaii Governor": "#238b45"}
    held = {"Rhode Island Governor": "12,304 NO sets", "MN-05 House race": "21,162 YES sets", "Hawaii Governor": "7,658 YES sets"}
    for r, col in cols.items():
        for half, ls in (("IS", ":"), ("OOS", "-")):
            blk = d["targets"].get(f"{r}|{half}|240|2000")
            if not blk:
                continue
            m = np.array([blk[f"single level {v:+d}"]["gain"] for v in DELTAS], float)
            p5 = np.array([blk[f"single level {v:+d}"]["gain_p5"] for v in DELTAS], float)
            ax.plot(x, m, ls=ls, color=col, marker="o" if half == "OOS" else None, ms=4,
                    label=f"{r} ({held[r]}), {'training-day' if half == 'IS' else 'test-day'} price swings")
            if half == "OOS":
                ax.fill_between(x, p5, m, color=col, alpha=0.10, lw=0)
                ax.axhline(-blk["dump_vs_base"], color=col, lw=1, ls="-.")
    ax.axhline(0, color="0.5", lw=0.8)
    ax.set_title("D. Our sets, simulated 10 days to 3 Nov: waiting for a level vs 2,000-set clips every 30 min from now",
                 loc="left", fontweight="bold")
    ax.set_ylabel("Better than selling in clips now\n(ticks per set)")
    ax.set_xlabel("Target exit level: ticks better than the race's usual exit cost (its median over the last 24 h); "
                  "0 = usual, negative = accept worse")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8.5, loc="lower left")

    ax = fig.add_subplot(gs[5])
    ax.axis("off")
    ax.text(0, 0.5, "Line styles: dotted = training day (22 Oct 12:00 - 23 Oct 12:00 UTC); solid with dots = test day (23 Oct 12:00 - "
            "24 Oct 13:00 UTC). Shaded: 95% band of the average (A, C); in D, the area between the average and the worst 5%. "
            "Dash-dot (D): selling the whole position at once now, per race colour. Brown labels in C: average hours until a set is sold (per set).", fontsize=9, va="center", wrap=True)

    _glossary(fig.add_subplot(gs[6]), [
        ("arb set", "one contract on every outcome of a race; a NO set pays (legs - 1), a YES set pays 1 at settlement"),
        ("exit cost", "what selling one set back gives up vs its payout, at the best prices on offer (the touch), in ticks"),
        ("tick", "0.005, the smallest price step"),
        ("usual level", "the race's median exit cost over the last 24 hours; levels move about 2 ticks a day (3-leg: 4-6)"),
        ("clip", "a part of the position sold in one go; bigger clips also walk deeper into the book and cost more"),
        ("waiting for a level", "sell a clip only when the exit cost is at or below the level, at most one clip per 30 min; "
                                "whatever is left is sold at once at the end"),
        ("simulated days", "6-hour stretches of the race's own price swings strung together, plus day-to-day level moves "
                           "taken from other races over 14-23 Oct"),
        ("worst 5%", "the 5th percentile of the outcome over start times (C) or simulated paths (D)"),
        ("training / test day", "the rule shape is read off the training day and checked on the test day"),
    ])
    os.makedirs(FIG, exist_ok=True)
    out = os.path.join(FIG, "ladder_exits_decision.png")
    fig.savefig(out, dpi=100)
    print("wrote", out)
