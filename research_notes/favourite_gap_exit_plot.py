"""Decision plot: should 90-92c favourites be sold by 2 Nov or held to the result? Run after favourite_gap_exit_study.py."""
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
INK, MUTED, GRID, SURF = "#2b2b28", "#6f6e68", "#e7e6e1", "#fcfcfb"
BLUE, ORANGE = "#2a78d6", "#eb6834"
plt.rcParams.update({"font.family": "Helvetica", "font.size": 11, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": .8, "figure.facecolor": SURF, "axes.facecolor": SURF})
T = pd.read_pickle("data/research_diralpha/fav_gap_tab.pkl")
E = pd.read_pickle("data/research_diralpha/fav_gap.pkl")
gap0 = 100 * E.gap0.mean()
fig, ax = plt.subplots(3, 1, figsize=(10, 11.5), sharex=True, gridspec_kw=dict(hspace=.2))
fig.suptitle("Sell the favourites by 2 Nov, or hold them to the result?", x=.08, ha="left", fontsize=17, fontweight="bold", y=.965)
ax[0].plot(T.h, T.net, color=BLUE, lw=2, marker="o", ms=5, label=f"Sold at SIG's bid after that many hours ({len(E)} past trades)")
ax[0].fill_between(T.h, T.net - T.se, T.net + T.se, color=BLUE, alpha=.13, lw=0)
ax[0].axhline(gap0, color=ORANGE, lw=2, ls="--", label=f"Held to the result: +{gap0:.1f}c at the outside fair")
ax[0].axhline(0, color=MUTED, lw=1)
ax[0].axvspan(36, 48, color=GRID, alpha=.7, lw=0)
ax[0].text(37, 3.4, "Sale on 2 Nov", fontsize=10, color=MUTED)
ax[0].set_ylabel("Profit per share\n(cents, after spread)"); ax[0].set_ylim(-1, 6.3)
ax[0].set_title("How much of the edge do we keep?", loc="left", fontsize=12.5, fontweight="bold")
ax[0].legend(frameon=False, loc="center right", fontsize=10.5)
ax[1].plot(T.h, T.gap, color=BLUE, lw=2, marker="o", ms=5)
ax[1].axhline(gap0, color=MUTED, lw=1, ls=":")
ax[1].text(70, gap0 + .08, f"Gap when bought: {gap0:.1f}c", fontsize=10, color=MUTED)
ax[1].set_ylabel("Gap still open\n(cents)"); ax[1].set_ylim(3.5, 6)
ax[1].set_title("Does SIG catch up with Polymarket and Kalshi?", loc="left", fontsize=12.5, fontweight="bold")
ax[1].text(5, 3.75, f"After 4 days only {T.close.iloc[-1]:.0f}% of the gap has closed", fontsize=10, color=MUTED)
n, fund = 22100, 705
pnl = T.net / 100 * n - fund
ax[2].plot(T.h, pnl, color=BLUE, lw=2, marker="o", ms=5, label="Sold after that many hours")
ax[2].axhline(gap0 / 100 * n - fund, color=ORANGE, lw=2, ls="--", label=f"Held to the result: +${gap0 / 100 * n - fund:,.0f}")
ax[2].axhline(0, color=MUTED, lw=1)
ax[2].axvspan(36, 48, color=GRID, alpha=.7, lw=0)
ax[2].set_ylabel("Expected profit\n($)"); ax[2].set_xlabel("Hours held")
ax[2].set_title("What does moving $20k out of the MN-05 sets earn?", loc="left", fontsize=12.5, fontweight="bold")
ax[2].legend(frameon=False, loc="center right", fontsize=10.5)
ax[2].set_xticks([6, 12, 24, 48, 72, 96]); ax[2].set_xlim(0, 100)
g = ("Glossary\nEdge or gap: outside fair (Polymarket and Kalshi) minus SIG's price. Spread: what buyers pay minus what sellers receive; every figure already pays it.\n"
     f"Band: one standard error across races. Trades: 21-31 Oct, sides at 88-93c, 4c+ under outside fair, one per market per day, {E.race.nunique()} races.\n"
     "Moving \\$20k: sell MN-05 sets at about 96.7c a set (\\$705 given up), buy about 22,100 shares at 90.5c. Held to the result assumes the outside fair is right.")
fig.text(.08, .022, g, fontsize=9.3, color=MUTED, va="bottom", linespacing=1.4)
fig.subplots_adjust(top=.915, bottom=.125, left=.11, right=.97)
fig.savefig("research_notes/figures/favourite_gap_exit.png", dpi=150)
print(pnl.round(0).tolist(), round(gap0 / 100 * n - fund))
