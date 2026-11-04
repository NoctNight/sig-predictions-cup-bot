"""Chamber plan sizes on the live book. Read-only. Walks data/books_latest.json (touch sizes refreshed from the quotes table), then for each size:
 entry and exit cost, profit if sold by 3 Nov with the gap unchanged or closing as in October, profit held to the result under several Senate chances, net D after.
 Writes figures/dir_alpha_chamber_plan.png and data/research_diralpha/chamber/plan_table.pkl."""
import json, sqlite3, numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
pd.set_option("display.width", 200, "display.max_columns", 30)
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
B = json.load(open("data/books_latest.json")); B = B.get("books", B)
for ex in ("840", "842"):
    q = db.execute("select best_bid,best_ask,bid_qty,ask_qty from quotes where exchange_id=? order by ts desc limit 1", (ex,)).fetchone()
    print(ex, "latest top of book", q)
    a = [l for l in B[ex]["asks"] if l["price"] > q[1]] + [dict(price=q[1], quantity=q[3])]
    b = [l for l in B[ex]["bids"] if l["price"] < q[0]] + [dict(price=q[0], quantity=q[2])]
    B[ex] = dict(asks=sorted(a, key=lambda x: x["price"]), bids=sorted(b, key=lambda x: -x["price"]))


def walk(lv, n):
    """Average price of n shares down a ladder."""
    left, c = n, 0.0
    for l in lv:
        t = min(left, l["quantity"]); c += t * l["price"]; left -= t
        if left <= 0: break
    return c / (n - left) if n > left else np.nan


f = {r[0]: r[1:] for r in db.execute("select exchange_id,fair,pm,kalshi from venue_fair where ts>(select max(ts)-60 from venue_fair) and exchange_id in ('840','842')")}
fH, fS = f["840"][0], f["842"][0]
mH = .5 * (B["840"]["bids"][0]["price"] + B["840"]["asks"][0]["price"]); mS = .5 * (B["842"]["bids"][0]["price"] + B["842"]["asks"][0]["price"])
gapH, gapS = fH - mH, mS - fS
print("fair House", fH, "mid", mH, "gap", gapH, "| fair Senate", fS, "mid", mS, "gap", gapS)
CLOSE = 1 - 8.7 / 13                     # October fit: pair gap 13c to 8.7c by 3 Nov
NET_D = 4957.0
X = np.array([2500, 5000, 7500, 10000, 15000, 20000, 30000, 37000, 40000])
rows = []
for n in X:
    n = float(n)
    sE = walk(B["842"]["bids"], n); sX = walk(B["842"]["asks"], n)    # short: sell YES (buy NO at 1 - bid), buy back YES
    hE = walk(B["840"]["asks"], n); hX = walk(B["840"]["bids"], n)
    r = dict(n=n, s_cash=n * (1 - sE), s_rt_c=100 * (sX - sE), s_flat=n * (sE - sX), s_close=n * (gapS * CLOSE + sE - sX),
             h_cash=n * hE, h_rt_c=100 * (hE - hX), h_flat=n * ((hX - mH) - (hE - mH)), h_close=n * (gapH * CLOSE + (hX - hE)),
             net_s=NET_D - n * (1 - sE), net_h=NET_D + n * hE, net_p=NET_D + n * hE - n * (1 - sE))
    for pS, lab in ((.57, "ddhq"), (fS, "mkt"), (.764, "silver")):
        r["s_" + lab] = n * (sE - pS)
    r["h_mkt"] = n * (fH - hE)
    rows.append(r)
T = pd.DataFrame(rows); T.to_pickle("data/research_diralpha/chamber/plan_table.pkl")
print(T.round(1).to_string(index=False))
INK, MUTED, GRID, SURF = "#2b2b28", "#6f6e68", "#e7e6e1", "#fcfcfb"
BLUE, ORANGE, AQUA = "#2a78d6", "#eb6834", "#1baf7a"
plt.rcParams.update({"font.family": "Helvetica", "font.size": 11, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED, "ytick.color": MUTED, "text.color": INK, "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": .8, "figure.facecolor": SURF, "axes.facecolor": SURF})
fig, ax = plt.subplots(3, 1, figsize=(10, 11.5), sharex=True, gridspec_kw=dict(hspace=.18))
fig.suptitle("How big a chamber trade, and what it earns", x=.08, ha="left", fontsize=17, fontweight="bold", y=.965)
x = T.n
ax[0].plot(x, T.s_rt_c, color=ORANGE, lw=2, marker="o", ms=5, label="Short Senate D, one pass through today's book")
ax[0].plot(x, T.h_rt_c, color=BLUE, lw=2, marker="o", ms=5, label="Long House D, one pass through today's book")
ax[0].set_ylabel("In and out cost\n(cents a share)"); ax[0].set_ylim(bottom=0)
ax[0].set_title("What does it cost to get in and out?", loc="left", fontsize=12.5, fontweight="bold")
ax[0].legend(frameon=False, loc="upper left", fontsize=10)
ax[1].plot(x, T.s_close, color=ORANGE, lw=2, label="Senate short, gap closes a third")
ax[1].plot(x, T.s_flat, color=ORANGE, lw=2, ls="--", label="Senate short, gap stays")
ax[1].plot(x, T.h_close, color=BLUE, lw=2, label="House long, gap closes a third")
ax[1].plot(x, T.h_flat, color=BLUE, lw=2, ls="--", label="House long, gap stays")
ax[1].axhline(0, color=MUTED, lw=1); ax[1].set_ylabel("Profit if sold by 3 Nov ($)")
ax[1].set_title("Sold before the punt: small either way", loc="left", fontsize=12.5, fontweight="bold")
ax[1].legend(frameon=False, loc="lower left", fontsize=10)
ax[2].plot(x, T.h_mkt, color=BLUE, lw=2, label="House long, market chance")
ax[2].plot(x, T.s_ddhq, color=ORANGE, lw=2, ls=":", label="Senate short, D chance 57% (Decision Desk)")
ax[2].plot(x, T.s_mkt, color=ORANGE, lw=2, label=f"Senate short, D chance {100*fS:.0f}% (markets)")
ax[2].plot(x, T.s_silver, color=ORANGE, lw=2, ls="--", label="Senate short, D chance 76% (Silver Bulletin)")
ax[2].axhline(0, color=MUTED, lw=1); ax[2].set_ylabel("Expected profit if held\nto the result ($)")
ax[2].set_title("Held to the result: more money, but it is not cash for the punt", loc="left", fontsize=12.5, fontweight="bold")
ax[2].legend(frameon=False, loc="upper left", fontsize=10)
ax[2].set_xscale("log"); ax[2].set_xticks([2500, 5000, 10000, 20000, 40000]); ax[2].set_xticklabels(["2.5k", "5k", "10k", "20k", "40k"]); ax[2].minorticks_off()
ax[2].set_xlabel("Shares (log scale)")
g = ("Glossary\nGap: SIG's price against the Polymarket and Kalshi average. \n"
     "Gap closes a third: the October fit on these legs, from 3 episodes. Gap stays: what other legs did in late October.\n"
     "Short Senate D: buy NO at about 26c. Long House D: buy YES at 86c. Book of about 14:00 UTC with touch sizes from 14:55 UTC.\n"
     "One pass assumes no refill, so clipping over hours should cost less. Results are in play money, before any effect on the punt.")
fig.text(.08, .022, g, fontsize=9.5, color=MUTED, va="bottom", linespacing=1.4)
fig.subplots_adjust(top=.915, bottom=.14, left=.11, right=.97)
fig.savefig("research_notes/figures/dir_alpha_chamber_plan.png", dpi=150)
