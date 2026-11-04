"""Decision plot: do the 90-92c favourites catch up with the outside fair before the result? Run after the study scripts."""
import json, os
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
OUT = "data/research_near_settlement"
INK, MUTED, GRID, SURF = "#2b2b28", "#6f6e68", "#e7e6e1", "#fcfcfb"
BLUE, ORANGE, GREEN = "#2a78d6", "#eb6834", "#1baf7a"
plt.rcParams.update({"font.family": "Helvetica", "font.size": 11, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": .8, "figure.facecolor": SURF, "axes.facecolor": SURF})
X = lambda d: -np.sqrt(np.asarray(d, float))      # days before the result, stretched near the end so the last 3 days can be read
TICKS = [14, 10, 7, 5, 3, 2, 1, .25, 0]
fig, ax = plt.subplots(4, 1, figsize=(10, 15.2), sharex=True, gridspec_kw=dict(hspace=.2, height_ratios=[1, .9, 1, 1]))
fig.suptitle("Do the 90-92c favourites catch up before the result?", x=.08, ha="left", fontsize=17, fontweight="bold", y=.975)

# A volatility
T = pd.read_pickle(f"{OUT}/q1_tab_election.pkl")
for band, col, lab in (("skewed 80-95c", BLUE, "Favourites at 80-95c"), ("mid 50-65c", MUTED, "Close races at 50-65c")):
    q = T[(T.band == band) & (T.day >= 1)].sort_values("day", ascending=False)
    x = q.day - .5
    ax[0].plot(X(x), q.vol_c, color=col, lw=2, marker="o", ms=5, label=lab + " (US elections)")
    ax[0].fill_between(X(x), q.lo, q.hi, color=col, alpha=.13, lw=0)
s = T[(T.band == "skewed 80-95c") & (T.day == 0)].iloc[0]
ax[0].plot(X(.0), s.vol_c, color=BLUE, marker="o", ms=8, ls="none")
ax[0].text(X(.0) - .06, s.vol_c, f"Favourites on result night: {s.vol_c:.0f}c in 12 hours", fontsize=10, color=MUTED, ha="right", va="center")
ax[0].set_ylabel("Price swing\n(cents per day)"); ax[0].set_ylim(0, 14)
ax[0].set_title("Do prices get more volatile near the result? Yes, in the last day", loc="left", fontsize=12.5, fontweight="bold")
ax[0].legend(frameon=False, loc="upper left", fontsize=10.5, ncol=2)

# B drift of favourites
Q = pd.read_pickle(f"{OUT}/q2_tab_election.pkl")
q = Q[(Q.band == "85-95c") & (Q.anchor == 10)].sort_values("tau", ascending=False)
n = int(q.n.max())
xs = np.r_[10, q.tau]
ys = np.r_[0, q.closed_all]
lo = np.r_[0, 100 * q.lo / q.dist]; hi = np.r_[0, 100 * q.hi / q.dist]
ax[1].plot(X(xs), ys, color=BLUE, lw=2, marker="o", ms=5, label=f"US election favourites at 85-95c on day 10, all of which went on to win ({n} markets)")
ax[1].fill_between(X(xs), lo, hi, color=BLUE, alpha=.13, lw=0)
O = pd.read_pickle(f"{OUT}/q2_tab_others.pkl")
o = O[(O.band == "85-95c") & (O.anchor == 10)].sort_values("tau", ascending=False)
xo = np.r_[10, o.tau]
ax[1].plot(X(xo), np.r_[0, o.closed_all], color=GREEN, lw=2, marker="o", ms=5, label=f"The same in other events: Fed, awards, foreign elections ({int(o.n.max())} markets)")
ax[1].fill_between(X(xo), np.r_[0, 100 * o.lo / o.dist], np.r_[0, 100 * o.hi / o.dist], color=GREEN, alpha=.13, lw=0)
ax[1].axhline(0, color=MUTED, lw=1)
ax[1].set_ylabel("Way to 1.00 covered\n(% of the distance)"); ax[1].set_ylim(-20, 75)
ax[1].set_title("Do winners climb early? In other events yes, in US elections not until the result", loc="left", fontsize=12.5, fontweight="bold")
ax[1].legend(frameon=False, loc="upper left", fontsize=10.5)

# C closure
R = pd.read_pickle(f"{OUT}/q3_election.pkl")
BK = {"T-14 to T-10": 12, "T-10 to T-7": 8.5, "T-7 to T-5": 6, "T-5 to T-3": 4, "T-3 to T-2": 2.5, "T-2 to T-1": 1.5, "T-1 to T-6h": .6}
q = R[(R.band == "skewed 80-96c") & (R.min_gap == .01) & (R.h == 24) & R.bucket.isin(BK)].assign(d=lambda z: z.bucket.map(BK)).sort_values("d", ascending=False)
ax[2].plot(X(q.d), q.close, color=BLUE, lw=2, marker="o", ms=5, label="Polymarket against Kalshi, US elections")
ax[2].fill_between(X(q.d), q.lo, q.hi, color=BLUE, alpha=.13, lw=0)
if os.path.exists(f"{OUT}/q3_others.pkl"):
    o = pd.read_pickle(f"{OUT}/q3_others.pkl")
    o = o[(o.band == "skewed 80-96c") & (o.min_gap == .01) & (o.h == 24) & o.bucket.isin(BK)].assign(d=lambda z: z.bucket.map(BK)).sort_values("d", ascending=False)
    if len(o) >= 3:
        ax[2].plot(X(o.d), o.close, color=GREEN, lw=2, marker="o", ms=5, label="Polymarket against Kalshi, other events (Fed, awards, foreign elections)")
        ax[2].fill_between(X(o.d), o.lo, o.hi, color=GREEN, alpha=.13, lw=0)
S = pd.DataFrame(json.load(open(f"{OUT}/sig_closure.json"))["fav_24"]["table"])
S["d"] = S.window.map({"21-23 Oct": 11.5, "24-25": 9.5, "26-27": 7.5, "28-29": 5.5, "30-31": 3.5})
ax[2].plot(X(S.d), S.close, color=ORANGE, lw=2, marker="o", ms=5, label="SIG against the outside fair, this month")
ax[2].fill_between(X(S.d), S.lo, S.hi, color=ORANGE, alpha=.13, lw=0)
ax[2].text(X(S.d.iloc[-1]) - .03, 12, "One day of buys", fontsize=10, color=MUTED, ha="center")
ax[2].axvline(X(3), color=MUTED, lw=1, ls=":")
ax[2].text(X(3) + .04, 2, "SIG has no history past here", fontsize=10, color=MUTED)
ax[2].set_ylabel("Gap closed in 24 hours\n(%)"); ax[2].set_ylim(0, 100)
ax[2].set_title("Do gaps close faster near the result? Fed and awards yes, elections a little, SIG no", loc="left", fontsize=12.5, fontweight="bold")
ax[2].legend(frameon=False, loc="upper left", fontsize=10.5)

# D exit
E = pd.read_pickle(f"{OUT}/exit_scenarios.pkl")
Hd = pd.read_pickle(f"{OUT}/exit_hold.pkl")
I = json.load(open(f"{OUT}/exit_inputs.json"))
px = [1, .25]
low, cen, up, top, real = E.iloc[0], E.iloc[1], E.iloc[2], E.iloc[3], E.iloc[4]
ax[3].plot(X(px), [cen.sold_2nov, cen.sold_3nov], color=BLUE, lw=2, marker="o", ms=7, label="Sold at SIG's bid: SIG's rate since 24 Oct with history's speed-up")
ax[3].plot(X(px), [up.sold_2nov, up.sold_3nov], color=BLUE, lw=2, marker="o", ms=7, ls="--", label="Sold at SIG's bid: SIG's rate since 21 Oct with history's speed-up")
ax[3].fill_between(X(px), [low.sold_2nov, low.sold_3nov], [top.sold_2nov, top.sold_3nov], color=BLUE, alpha=.13, lw=0, label="Range, from no speed-up to the top of history's")
h = Hd.iloc[0]
ax[3].errorbar(X(0), h.per_share, yerr=[[h.per_share - Hd.iloc[1].per_share], [Hd.iloc[2].per_share - h.per_share]], color=ORANGE, marker="o", ms=8, lw=2, capsize=4, label=f"Held to the result: +{h.per_share:.1f}c at the outside fair (no cash for the 18:00 ET punt)")
ax[3].axhline(0, color=MUTED, lw=1)
ax[3].set_ylabel("Profit per share\n(cents, after spread)"); ax[3].set_ylim(-3, 11)
ax[3].set_title("Which exit earns most? Holding, if the cash can wait; selling earns about nothing", loc="left", fontsize=12.5, fontweight="bold")
ax[3].legend(frameon=False, loc="upper left", fontsize=10.5)
ax[3].set_xticks(X(TICKS)); ax[3].set_xticklabels([("Result" if t == 0 else ("6 h" if t == .25 else str(t))) for t in TICKS])
ax[3].set_xlabel("Days before the first polls close (18:00 ET on election day)")
ax[3].set_xlim(X(14.4), X(0) + .08)
g = ("Glossary\nPrice swing: realised volatility, the square root of the sum of squared 4-hour price moves in a day, averaged across markets.\n"
     "Gap: outside fair minus SIG's mid, or one venue minus the other. Outside fair: Polymarket and Kalshi. Spread: buyers' price minus sellers' price.\n"
     "Way to 1.00 covered: the favourite's rise as a share of what it had left to go. Band: one standard error across races, so markets in one race count once.\n"
     f"Sales assume {I['sides']} live sides, bought at {100*I['ask']:.1f}c, spread {100*I['spread']:.1f}c, fair {100*I['fair']:.1f}c. Gaps in the third chart are 1c+ and open at least 6 hours; SIG's are 4c+.")
fig.text(.08, .016, g, fontsize=9.3, color=MUTED, va="bottom", linespacing=1.4)
fig.subplots_adjust(top=.94, bottom=.118, left=.11, right=.97)
fig.savefig("research_notes/figures/near_settlement.png", dpi=150)
