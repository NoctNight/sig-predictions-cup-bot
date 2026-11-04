"""Decision plots for the overnight study. Plain-word labels, one question per panel, glossary on the image.
 dir_alpha_exit.png: when to sell (x = hours held)
 dir_alpha_size.png: how big (x = dollars per trade)"""
import json, sys, numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
INK, MUTED, GRID, SURF = "#2b2b28", "#6f6e68", "#e7e6e1", "#fcfcfb"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
plt.rcParams.update({"font.family": "Helvetica", "font.size": 11, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": .8, "figure.facecolor": SURF, "axes.facecolor": SURF})
T = json.load(open("data/research_diralpha/exit_tables.json"))
S, F = pd.DataFrame(T["slow"]), pd.DataFrame(T["fast"])
fig, ax = plt.subplots(3, 1, figsize=(10, 11.5), sharex=True, gridspec_kw=dict(hspace=.18))
fig.suptitle("When to sell a lag trade", x=.08, ha="left", fontsize=17, fontweight="bold", y=.965)
lab = {"fast": "Fast: both venues jumped in the last 30 min (30 trades)", "slow": "Slow: gap built up over a day (56 trades)"}
for d, c, k in ((F, BLUE, "fast"), (S, ORANGE, "slow")):
    ax[0].plot(d.hours, d.right_pct, color=c, lw=2, marker="o", ms=5, label=lab[k])
    ax[1].plot(d.hours, d.net_if_sold, color=c, lw=2, marker="o", ms=5)
    ax[1].fill_between(d.hours, d.net_if_sold - d.se, d.net_if_sold + d.se, color=c, alpha=.13, lw=0)
    ax[2].plot(d.hours, d.per_hour.clip(lower=-.1), color=c, lw=2, marker="o", ms=5)
ax[0].set_ylabel("Price moved our way\n(% of trades)"); ax[0].set_ylim(40, 100)
ax[0].set_title("How often is the signal right?", loc="left", fontsize=12.5, fontweight="bold")
ax[0].legend(frameon=False, loc="lower right", fontsize=10.5)
ax[1].axhline(0, color=MUTED, lw=1)
ax[1].set_ylabel("Profit if sold now\n(cents a share, after spread)")
ax[1].set_title("How much have we made by each hour?", loc="left", fontsize=12.5, fontweight="bold")
ax[1].text(5, 3.05, "Fast: 1.9c by 4 h, 2.9c by 24 h", fontsize=10, color=MUTED)
ax[1].text(25, .45, "Slow: nothing for 6 h, 2.2c by 36 h", fontsize=10, color=MUTED)
ax[2].axhline(0, color=MUTED, lw=1)
ax[2].set_ylabel("Profit per hour held\n(cents a share)"); ax[2].set_xlabel("Hours since we bought")
ax[2].set_title("How fast does the money come in?", loc="left", fontsize=12.5, fontweight="bold")
ax[2].set_xticks([0, 4, 8, 12, 16, 20, 24, 30, 36, 48]); ax[2].set_xlim(0, 49)
g = ("Glossary\nSpread: the gap between what buyers pay and sellers receive; every figure here already pays it.\n"
     "Band: one standard error either side. Backtest on 21-30 Oct under the audit's strict rules, before the 0.4c selection haircut.\n"
     "Lag trade: Polymarket and Kalshi moved, SIG has not finished following.")
fig.text(.08, .022, g, fontsize=9.5, color=MUTED, va="bottom", linespacing=1.4)
fig.subplots_adjust(top=.915, bottom=.125, left=.11, right=.97)
fig.savefig("research_notes/figures/dir_alpha_exit.png", dpi=150)
# size
L = Lab()
Es = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); Ef = pd.read_pickle("data/research_diralpha/strict_fast.pkl")
Bv, Av = L.B, L.A


def book(N, haircut, c1k, CAP=80000):
    cand = sorted([(r.e, r.x, r.j, r.s, r.net) for r in Es.itertuples()] + [(r.e, r.x, r.j, r.s, r.net) for r in Ef.itertuples()])
    op, used, tot, n = [], 0.0, 0.0, 0
    for e, x, j, sg, net in cand:
        keep = []
        for p in op:
            if p[0] <= e: used -= N
            else: keep.append(p)
        op = keep
        if any(p[1] == L.race[j] for p in op) or used + N > CAP: continue
        sh = N / (Av[e, j] if sg > 0 else 1 - Bv[e, j])
        tot += (net - haircut - 2 * c1k * sh / 1e5) * sh; n += 1
        op.append((x, L.race[j])); used += N
    return tot / 8.5 * 3.9, n


X = np.array([500, 1000, 1500, 2000, 2500, 3000, 4000, 5000, 6000, 7500, 10000, 12500, 15000, 20000])
fig, ax = plt.subplots(3, 1, figsize=(10, 11.5), sharex=True, gridspec_kw=dict(hspace=.18))
fig.suptitle("How big each lag trade should be", x=.08, ha="left", fontsize=17, fontweight="bold", y=.965)
shares = X / .5
ax[0].plot(X, .02 * shares / 1000, color=BLUE, lw=2, label="Cost used here, fitted to our own orders")
ow = pd.DataFrame(dict(x=[1000, 3800, 5200], y=[0, .14, .25]))
ax[0].scatter(ow.x, ow.y, color=BLUE, s=55, zorder=3, edgecolor=SURF, lw=1.5, label="Our real orders (median)")
ax[0].scatter([18500], [.75], color=BLUE, s=55, zorder=3, marker="s", edgecolor=SURF, lw=1.5, label="Our 37,000-share bursts, 3 min")
house = np.array([0, .2, .32, .38, .45, .5, .6, .73, .8, .95, 1.12, 1.25, 1.35, 1.58])
ax[0].plot(X, house, color=ORANGE, lw=2, ls="--", label="If the ladder never refilled (House seats)")
ax[0].set_ylabel("Extra cost each way\n(cents a share)"); ax[0].set_ylim(0, 1.7)
ax[0].set_title("What does size cost?", loc="left", fontsize=12.5, fontweight="bold")
ax[0].legend(frameon=False, loc="upper left", fontsize=10)
c = 2 * .02 * shares / 1000
ax[1].plot(X, 1.6 - c, color=BLUE, lw=2, label="Backtest edge 1.6c (strict rules)")
ax[1].plot(X, 1.2 - c, color=ORANGE, lw=2, label="Backtest after the selection haircut, 1.2c")
ax[1].plot(X, .6 - c, color=AQUA, lw=2, label="Unseen days 0.6c, live catch-up trades 0.7c")
ax[1].axhline(0, color=MUTED, lw=1); ax[1].set_ylim(-1.2, 1.8)
ax[1].set_ylabel("Profit per share\nafter costs (cents)")
ax[1].set_title("What is left per share?", loc="left", fontsize=12.5, fontweight="bold")
ax[1].legend(frameon=False, loc="upper right", fontsize=10)
a = np.array([book(int(x), 0, .02)[0] for x in X]); b = np.array([book(int(x), .004, .02)[0] for x in X]); e = np.array([book(int(x), .010, .02)[0] for x in X])
ax[2].plot(X, a, color=BLUE, lw=2, label="Backtest edge, our cost curve")
ax[2].plot(X, b, color=ORANGE, lw=2, label="After the selection haircut")
ax[2].plot(X, e, color=AQUA, lw=2, label="Using the edge from unseen days (0.6c)")
ax[2].axhline(0, color=MUTED, lw=1); ax[2].set_ylim(-5000, 6500)
ax[2].set_xscale("log"); ax[2].set_xticks([500, 1000, 2500, 5000, 10000, 20000]); ax[2].set_xticklabels(["$500", "$1k", "$2.5k", "$5k", "$10k", "$20k"]); ax[2].minorticks_off()
ax[2].set_xlabel("Dollars per trade (log scale)")
ax[2].set_ylabel("Expected profit to 3 Nov\n($, 3.9 days of signals)")
ax[2].set_title("What does the whole book earn?", loc="left", fontsize=12.5, fontweight="bold")
ax[2].legend(frameon=False, loc="lower left", fontsize=10)
g = ("Glossary\nTouch: the best price on offer. Ladder: the market maker's stacked orders behind it.\n"
     "Selection haircut: 0.4c off because the rule was picked after looking at the data. Unseen days: 3-20 Oct, not used to find the rule.\n"
     "Book: every fast and slow signal, one bet per race, at most $80k in use. The top two panels assume 50c a share.")
fig.text(.08, .022, g, fontsize=9.5, color=MUTED, va="bottom", linespacing=1.4)
fig.subplots_adjust(top=.915, bottom=.135, left=.11, right=.97)
fig.savefig("research_notes/figures/dir_alpha_size.png", dpi=150)
print("per $ table", pd.DataFrame(dict(x=X, edge=a.round(0), haircut=b.round(0), doubled=e.round(0))).to_string(index=False))
