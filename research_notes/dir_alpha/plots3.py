"""Decision plot: does the lag rule hold on days it was not found on? dir_alpha_oos.png"""
import json, numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
INK, MUTED, GRID, SURF = "#2b2b28", "#6f6e68", "#e7e6e1", "#fcfcfb"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
plt.rcParams.update({"font.family": "Helvetica", "font.size": 11, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": .8, "figure.facecolor": SURF, "axes.facecolor": SURF})
P = pd.read_pickle("data/research_diralpha/oos/paths.pkl")
T = json.load(open("data/research_diralpha/exit_tables.json")); Bs, Bf = pd.DataFrame(T["slow"]), pd.DataFrame(T["fast"])
fig, ax = plt.subplots(3, 1, figsize=(10, 11.5), gridspec_kw=dict(hspace=.3))
fig.suptitle("The lag rule on 3-20 Oct, days it was not found on", x=.08, ha="left", fontsize=17, fontweight="bold", y=.965)
ax[0].plot(P["fast"].hours, P["fast"].net, color=BLUE, lw=2, marker="o", ms=5, label="Fast, unseen days (25 trades)")
ax[0].plot(P["slow"].hours, P["slow"].net, color=ORANGE, lw=2, marker="o", ms=5, label="Slow, unseen days (77 trades)")
ax[0].plot(Bf.hours, Bf.net_if_sold, color=BLUE, lw=1.2, ls="--", label="Fast, backtest")
ax[0].plot(Bs.hours, Bs.net_if_sold, color=ORANGE, lw=1.2, ls="--", label="Slow, backtest")
ax[0].axhline(0, color=MUTED, lw=1); ax[0].set_xticks([0, 4, 8, 12, 16, 20, 24, 30, 36, 48]); ax[0].set_xlim(0, 49)
ax[0].set_xlabel("Hours since we bought"); ax[0].set_ylabel("Profit if sold now\n(cents a share)")
ax[0].set_title("When to sell: sell fast trades by about 8 h, slow ones by about 20 h", loc="left", fontsize=12.5, fontweight="bold"); ax[0].set_ylim(-1.3, 4.2); ax[0].legend(frameon=False, loc="upper center", fontsize=10, ncol=2)
w = .36
def bars(a, labs, x1, x2, n1, n2, title):
    i = np.arange(len(labs))
    a.bar(i - w / 2, x1, w, color=ORANGE, alpha=.45, label="Backtest", edgecolor=SURF, linewidth=2)
    a.bar(i + w / 2, x2, w, color=ORANGE, label="Unseen days", edgecolor=SURF, linewidth=2)
    for k in range(len(labs)):
        a.text(k - w / 2, max(x1[k], 0) + .06, f"{n1[k]}", ha="center", fontsize=9.5, color=MUTED); a.text(k + w / 2, max(x2[k], 0) + .06, f"{n2[k]}", ha="center", fontsize=9.5, color=MUTED)
    a.set_xticks(i); a.set_xticklabels(labs); a.axhline(0, color=MUTED, lw=1); a.set_ylabel("Profit (cents a share)"); a.set_title(title, loc="left", fontsize=12.5, fontweight="bold"); a.set_ylim(top=max(x1) * 1.3); a.legend(frameon=False, loc="upper right", fontsize=10, ncol=2)
bars(ax[1], ["0-1 points", "2 points", "3+ points"], [.5, 2.2, 3.5], [.19, .45, 1.25], [29, 21, 6], [51, 23, 3], "Which signals: more conviction points, more profit (slow rule)")
bars(ax[2], ["Other trades", "Started 2c+ on the right side of the line"], [1.27, 1.71], [-.01, .87], [28, 28], [44, 40], "Which trades: starting on the right side helps, as a tilt (within-day t 1.3)")
g = ("Glossary\nConviction points: one each for no public polls, toss-up rating, leg unchanged 2 h, signal in the upper two thirds. Numbers above bars are trades.\n"
     "The line: where the market maker usually prices a leg. Right side: the leg is cheap against it and the signal says buy, or rich and it says sell.\n"
     "Unseen days: SIG last trades, Polymarket, Kalshi; no taker flow filter; 0.75c assumed for the spread.\nPolls and ratings are today's, which flatters the points slightly.\n"
     "Backtest: 21-30 Oct under the audit's strict rules, before any haircut. Placebo on the same unseen days: other legs +0.15c before spread, against +1.1c.")
fig.text(.08, .022, g, fontsize=9.5, color=MUTED, va="bottom", linespacing=1.4)
fig.subplots_adjust(top=.915, bottom=.155, left=.11, right=.97)
fig.savefig("research_notes/figures/dir_alpha_oos.png", dpi=150)
X = pd.read_pickle("research_notes/dir_alpha/lr_sets.pkl"); A, B = X["A2"], X["C"]      # the stated rule, 3-20 Oct; book mid signal and true book, 21-30 Oct (line_review.md)
bk = [(.03, .035), (.035, .045), (.045, .10)]; labs = ["3-3.5c from the line", "3.5-4.5c", "4.5c or more"]
f = lambda E: [100 * E[(E.res >= a) & (E.res < b)].n.mean() for a, b in bk]; cnt = lambda E: [int(((E.res >= a) & (E.res < b)).sum()) for a, b in bk]
fig, ax = plt.subplots(3, 1, figsize=(10, 11.5), gridspec_kw=dict(hspace=.3))
fig.suptitle("Prices far from the market maker's usual line, held 72 h", x=.08, ha="left", fontsize=17, fontweight="bold", y=.965)
def b2(a, labs, x1, x2, n1, n2, title, l1, l2, c1=ORANGE, c2=BLUE):
    i = np.arange(len(labs))
    a.bar(i - w / 2, x1, w, color=c1, label=l1, edgecolor=SURF, linewidth=2); a.bar(i + w / 2, x2, w, color=c2, label=l2, edgecolor=SURF, linewidth=2)
    for k in range(len(labs)):
        a.text(k - w / 2, max(x1[k], 0) + .05, f"{n1[k]}", ha="center", fontsize=9.5, color=MUTED); a.text(k + w / 2, max(x2[k], 0) + .05, f"{n2[k]}", ha="center", fontsize=9.5, color=MUTED)
    a.set_xticks(i); a.set_xticklabels(labs); a.axhline(0, color=MUTED, lw=1); a.set_ylabel("Profit at 72 h\n(cents a share)"); a.set_title(title, loc="left", fontsize=12.5, fontweight="bold"); a.set_ylim(top=max(max(x1), max(x2)) * 1.3); a.legend(frameon=False, loc="upper left", fontsize=10, ncol=2)
b2(ax[0], labs, f(A), f(B), cnt(A), cnt(B), "Does distance matter? A little", "3-20 Oct, last trades", "21-30 Oct, true book")
b2(ax[1], ["SIG cheap, buy it", "SIG rich, sell it"], [100 * A[~A.rich].n.mean(), 100 * A[A.rich].n.mean()], [100 * B[~B.rich].n.mean(), 100 * B[B.rich].n.mean()], [int((~A.rich).sum()), int(A.rich.sum())], [int((~B.rich).sum()), int(B.rich.sum())], "Does it work both ways? Yes", "3-20 Oct, last trades", "21-30 Oct, true book")
T_ = pd.concat([A.assign(p="a"), B.assign(p="b")]); T_["d"] = pd.to_datetime("2026-" + T_.day, format="%Y-%m-%d"); T_["blk"] = ((T_.d - pd.Timestamp("2026-10-03")).dt.days // 3)
g_ = T_.groupby("blk").agg(net=("n", lambda v: 100 * v.mean()), n=("n", "size"), first=("d", "min")).reset_index()
ax[2].bar(g_.blk, g_.net, .7, color=[ORANGE if d < pd.Timestamp("2026-10-21") else BLUE for d in g_["first"]], edgecolor=SURF, linewidth=2)
for r in g_.itertuples(): ax[2].text(r.blk, max(r.net, 0) + .06, f"{r.n}", ha="center", fontsize=9.5, color=MUTED)
ax[2].set_xticks(g_.blk); ax[2].set_xticklabels([x.strftime("%-d %b") for x in pd.Timestamp("2026-10-03") + pd.to_timedelta(g_.blk.values * 3, "D")]); ax[2].axhline(0, color=MUTED, lw=1)
ax[2].set_xlabel("Three days from the date shown"); ax[2].set_ylabel("Profit at 72 h\n(cents a share)"); ax[2].set_title("Is it still working? The latest entries made about nothing", loc="left", fontsize=12.5, fontweight="bold")
g = ("Glossary\nThe line: SIG's market maker prices about 0.93 of the way from 50c to outside. Distance is SIG's price minus that line, fitted across all legs.\n"
     "Numbers above bars are trades, one bet per leg per 72 h, chamber legs left out. True book: bought at the ask, sold at the bid.\n"
     "Last trades: 0.75c assumed for spread. These are at the touch: 5,000 shares costs 0.25-0.7c more, which takes most of the profit.")
fig.text(.08, .022, g, fontsize=9.5, color=MUTED, va="bottom", linespacing=1.4)
fig.subplots_adjust(top=.915, bottom=.125, left=.11, right=.97)
fig.savefig("research_notes/figures/dir_alpha_level.png", dpi=150)
