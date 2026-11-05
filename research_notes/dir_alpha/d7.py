"""Drill-down 7: capacity and portfolio. Ladder walk cost per leg from every saved book snapshot, then a simulation of the filtered signal at
several sizes per trade, with walking the book on the way in and on the way out."""
import sys, glob, json, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab, H
L = Lab()
mid, gap, P, fair = L.mid, L.gap, L.P, L.fair
pd.set_option("display.width", 200, "display.max_rows", 300, "display.max_columns", 40)
files = sorted(glob.glob("data/books_history/*.json")) + ["data/books_latest.json"]
TICK = .005
Kmax = 30
depth = {"bid": {}, "ask": {}}
for f in files:
    try:
        d = json.load(open(f))
    except Exception:
        continue
    bk = d.get("books", d) if isinstance(d, dict) else {}
    for ex, b in bk.items():
        if not isinstance(b, dict) or not b.get("bids") or not b.get("asks"): continue
        for side, key in (("bid", "bids"), ("ask", "asks")):
            lv = sorted(b[key], key=lambda x: -x["price"] if key == "bids" else x["price"])
            t = lv[0]["price"]
            arr = np.zeros(Kmax)
            for x in lv:
                k = int(round(abs(x["price"] - t) / TICK))
                if k < Kmax: arr[k] += x["quantity"]
            depth[side].setdefault(ex, []).append(arr)
print("snapshots", len(files), "legs", len(depth["bid"]), "per leg", np.median([len(v) for v in depth["bid"].values()]))
med = {s: {e: np.median(np.array(v), axis=0) for e, v in depth[s].items()} for s in depth}


def walk(ex, side, shares):
    """Average extra price per share over the touch to trade `shares` against this side's median ladder (cents)."""
    d = med[side].get(ex)
    if d is None: return np.nan
    left, cost = shares, 0.0
    for k, q in enumerate(d):
        t = min(left, q); cost += t * k * TICK; left -= t
        if left <= 0: break
    if left > 0: cost += left * Kmax * TICK
    return 100 * cost / shares


rows = []
for ex in med["ask"]:
    if ex not in L.cols: continue
    j = L.cols.index(ex)
    p = np.nanmedian(mid.values[-288:, j])
    if not (.15 <= p <= .85): continue
    for notional in (500, 1000, 2500, 5000, 10000, 20000, 40000):
        q = notional / p
        rows.append(dict(ex=ex, office=L.office[j], notional=notional, ask=walk(ex, "ask", q), bid=walk(ex, "bid", q), touch=med["ask"][ex][0]))
W = pd.DataFrame(rows)
print("\nExtra cost above the touch, buying then selling, cents per share, median legs priced 15-85:")
print((W.assign(rt=W.ask + W.bid).groupby(["office", "notional"]).rt.median().unstack()).round(2).to_string())
print("p75:"); print((W.assign(rt=W.ask + W.bid).groupby(["office", "notional"]).rt.quantile(.75).unstack()).round(2).to_string())
print("median shares at the touch by office", W[W.notional == 500].groupby("office").touch.median().round(0).to_dict())

S = gap - gap.rolling(576, min_periods=288).median()
s = np.sign(S)
both = (L.lag(L.pm, 288) * s >= .01) & (L.lag(L.k, 288) * s >= .01)
flowok = L.rsum(P["sflow"], 288) * s >= -5000
M = (((mid >= .15) & (mid <= .85)) & both & flowok).values.copy()
M[:, L.office == "chamber"] = False
tr = L.trades(S, H["24h"], thr=.02, mask=M, cool=H["24h"])
E = pd.DataFrame(tr, columns=["j", "i", "s", "gross", "net"]); E[["j", "i"]] = E[["j", "i"]].astype(int)
E["ex"] = [L.cols[j] for j in E.j]; E["race"] = L.race[E.j]; E["mid"] = mid.values[E.i, E.j]; E["party"] = L.party[E.j]
E["t"] = L.idx[E.i]
E["dside"] = E.s * np.where(E.party == "D", 1, -1)
res = []
for N in (500, 1000, 2500, 5000, 10000, 20000, 40000):
    pnl = []
    for r in E.itertuples():
        pr = r.mid if r.s > 0 else 1 - r.mid
        sh = N / pr
        # buy YES lifts the ask, sell lifts the bid; buying NO is hitting the YES bid
        c = (walk(r.ex, "ask", sh) + walk(r.ex, "bid", sh)) / 100
        pnl.append((r.t, r.net - c, sh, (r.net - c) * sh, r.race, r.dside * N))
    Q = pd.DataFrame(pnl, columns=["t", "netps", "sh", "usd", "race", "dside"]).dropna()
    Q = Q.sort_values("t")
    # one trade per race at a time: the mirror leg is the same bet
    Q["bin"] = (Q.t // 86400)
    Qr = Q.drop_duplicates(["race", "bin"])
    open_cap = []
    for t in Qr.t:
        o = Qr[(Qr.t <= t) & (Qr.t > t - 86400)]
        open_cap.append((len(o) * N, o.dside.sum()))
    oc = np.array(open_cap)
    day = Qr.groupby("bin").usd.sum()
    res.append(dict(per_trade=N, trades=len(Qr), net_c=100 * Qr.netps.mean(), win=(Qr.usd > 0).mean(), total=Qr.usd.sum(), per_day=day.mean(), worst_day=day.min(), worst_trade=Qr.usd.min(), peak_capital=oc[:, 0].max(), peak_net_D=np.abs(oc[:, 1]).max()))
print("\nPortfolio: every filtered signal, one per race per day, held 24 h, taker both ways, walking the median ladder")
print(pd.DataFrame(res).round(2).to_string(index=False))
print("share of trades buying D-direction:", round((E.dside > 0).mean(), 2))
E.to_pickle("data/research_diralpha/filtered_events.pkl")
