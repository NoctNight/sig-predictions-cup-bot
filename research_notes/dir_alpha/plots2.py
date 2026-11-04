"""Decision plots for the chamber trade.
 dir_alpha_chamber.png: is it real, does it close (x = date)
 dir_alpha_chamber_size.png: how big (x = shares)"""
import sqlite3, numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt, matplotlib.dates as md
INK, MUTED, GRID, SURF = "#2b2b28", "#6f6e68", "#e7e6e1", "#fcfcfb"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
plt.rcParams.update({"font.family": "Helvetica", "font.size": 11, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": .8, "figure.facecolor": SURF, "axes.facecolor": SURF})
D = pd.read_pickle("data/research_diralpha/chamber/gaps.pkl")
fig, ax = plt.subplots(3, 1, figsize=(10, 11.5), sharex=True, gridspec_kw=dict(hspace=.2))
fig.suptitle("SIG's House and Senate control prices against Polymarket", x=.08, ha="left", fontsize=16, fontweight="bold", y=.965)
for a_, sg, pg, nm in ((ax[0], "sig_h", "pm_h", "Democrats win the House"), (ax[1], "sig_s", "pm_s", "Democrats win the Senate")):
    a_.plot(D.index, 100 * D[pg], color=BLUE, lw=2, label="Polymarket (real money)")
    a_.plot(D.index, 100 * D[sg], color=ORANGE, lw=2, label="SIG")
    a_.set_ylabel("Price (cents)"); a_.set_title(nm, loc="left", fontsize=12.5, fontweight="bold")
ax[0].legend(frameon=False, loc="upper center", fontsize=10.5, ncol=2)
ax[0].text(D.index[-1], 83.2, "Last point: SIG 86c, Polymarket 93.5c", fontsize=10, color=MUTED, ha="right")
ax[1].text(D.index[-90], 75.2, "Last point: SIG 75c, Polymarket 70.5c", fontsize=10, color=MUTED, ha="right")
ax[2].plot(D.index, 100 * D.gp, color=AQUA, lw=2)
ax[2].axhline(0, color=MUTED, lw=1); ax[2].axhline(5.1, color=MUTED, lw=1, ls="--")
ax[2].text(D.index[300], 10.6, "Dashed line: fitted long-run level 5c. Mean since 1 Oct 3.6c.", fontsize=10, color=MUTED)
ax[2].set_ylabel("Gap on the pair (cents)")
ax[2].set_title("How wide is the pair gap, and has it closed before?", loc="left", fontsize=12.5, fontweight="bold")
ax[2].text(D.index[-1], 15.5, f"13c at {D.index[-1].strftime('%-d %b %H:%M')} UTC", fontsize=10, color=MUTED, ha="right")
ax[2].xaxis.set_major_locator(md.DayLocator(interval=3)); ax[2].xaxis.set_major_formatter(md.DateFormatter("%-d Oct")); ax[2].set_xlabel("Date (UTC)")
g = ("Glossary\nPair: buy House D and short Senate D. It costs about 1.11 in cash, pays 2 if D win the House only, 1 if both or neither, 0 if R House and D Senate.\n"
     "Gap on the pair: what Polymarket says the pair is worth above SIG's price. SIG prices before 11 Oct are daily closes.\n"
     "Wide gaps have narrowed before (3 episodes, about 6c in 87 h), but a quick exit costs 2.4-3.4c. On SIG's other legs such gaps did not close at all.")
fig.text(.08, .022, g, fontsize=9.5, color=MUTED, va="bottom", linespacing=1.4)
fig.subplots_adjust(top=.915, bottom=.125, left=.1, right=.97)
fig.savefig("research_notes/figures/dir_alpha_chamber.png", dpi=150)
L = pd.read_pickle("data/research_diralpha/chamber_ladder.pkl")
T = pd.read_pickle("data/research_diralpha/chamber_table.pkl")
P = dict(DD=.6985, DR=.235, RR=.06, RD=.0065)
X = L.q.values
hp, sp = L.hD_avg.values, 1 - L.sD_bid_avg.values
ev_h = X * ((P["DD"] + P["DR"]) - hp); ev_s = X * ((P["DR"] + P["RR"]) - sp); ev_p = ev_h + ev_s
fig, ax = plt.subplots(3, 1, figsize=(10, 11.5), sharex=True, gridspec_kw=dict(hspace=.2))
fig.suptitle("How big a chamber trade could be, held to the election result", x=.08, ha="left", fontsize=16, fontweight="bold", y=.965)
ax[0].plot(X, 100 * (hp - .86), color=BLUE, lw=2, marker="o", ms=5, label="Buy House D: 570k shares for sale at 86c")
ax[0].plot(X, 100 * (L.sD_bid_avg.values[0] - L.sD_bid_avg.values + (.75 - L.sD_bid_avg.values[0])), color=ORANGE, lw=2, marker="o", ms=5, label="Sell Senate D into the book")
ax[0].set_ylabel("Extra cost per share\nabove the touch (cents)"); ax[0].set_title("What does size cost on the way in?", loc="left", fontsize=12.5, fontweight="bold"); ax[0].legend(frameon=False, loc="upper left", fontsize=10.5)
ax[1].plot(X, ev_h, color=BLUE, lw=2, marker="o", ms=5, label="House D only (needs a cap exception)")
ax[1].plot(X, ev_s, color=ORANGE, lw=2, marker="o", ms=5, label="Senate D short only (fits the cap to about 37k shares)")
ax[1].plot(X, ev_p, color=AQUA, lw=2, marker="o", ms=5, label="Both, equal shares (needs a cap exception)")
ax[1].set_ylabel("Expected profit at the\nresult ($)"); ax[1].set_title("What is it worth if the real-money prices are right?", loc="left", fontsize=12.5, fontweight="bold"); ax[1].legend(frameon=False, loc="upper left", fontsize=10.5)
loss_h = X * hp; loss_s = X * sp; loss_p = X * (hp + sp - 1)
ax[2].plot(X, loss_h, color=BLUE, lw=2, marker="o", ms=5, label="House D only: 7% chance")
ax[2].plot(X, loss_s, color=ORANGE, lw=2, marker="o", ms=5, label="Senate D short only: 70% chance")
ax[2].plot(X, loss_p, color=AQUA, lw=2, marker="o", ms=5, label="Both: 76% chance (plus a 0.6% chance of losing all the cash)")
ax[2].set_ylabel("Loss if it goes wrong ($)"); ax[2].set_xlabel("Shares")
ax[2].set_title("What is the usual way to lose, and how much?", loc="left", fontsize=12.5, fontweight="bold"); ax[2].legend(frameon=False, loc="upper left", fontsize=10.5)
ax[2].xaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda x, _: f"{int(x/1000)}k"))
g = ("Glossary\nTouch: the best price on offer. Book read at 03:56 UTC on 31 Oct; a live read is needed before any order.\n"
     "Chances come from Polymarket: D House D Senate 70%, D House R Senate 23.5%, R sweep 6%, R House D Senate 0.6%. Silver Bulletin puts Senate D at 76%.\n"
     "Cap: the 5,000 partisan limit, in notional. Room is about 300 D-side, 9,700 R-side. An equal-share pair adds 61c of net D a share.")
fig.text(.08, .022, g, fontsize=9.5, color=MUTED, va="bottom", linespacing=1.4)
fig.subplots_adjust(top=.915, bottom=.125, left=.11, right=.97)
fig.savefig("research_notes/figures/dir_alpha_chamber_size.png", dpi=150)
print(pd.DataFrame(dict(X=X, ev_h=ev_h.round(), ev_s=ev_s.round(), ev_p=ev_p.round(), loss_h=loss_h.round(), loss_s=loss_s.round(), loss_p=loss_p.round())).to_string(index=False))
