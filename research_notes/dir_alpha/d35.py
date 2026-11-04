"""Drill-down 35. A walls: an offer or bid at the touch of 100k+ shares and at least 10x that leg's usual touch, from saved books. Does the price move through it, away from it, or stay?
 B scenario table for the chamber trades at live book prices, with Kalshi's four-way prices as the scenario weights, plus what exit before results costs."""
import glob, json, os, sqlite3, numpy as np, pandas as pd
pd.set_option("display.width", 220, "display.max_rows", 200, "display.max_columns", 30)
fs = sorted(glob.glob("data/books_history/books_*.json"))
snap = []
for f in fs:
    ts = pd.Timestamp(os.path.basename(f)[6:-5].replace("Z", ""), tz="UTC").timestamp()
    try: B = json.load(open(f))
    except Exception: continue
    B = B.get("books", B)
    for ex, b in B.items():
        try:
            a = sorted(b["asks"], key=lambda x: x["price"]); d = sorted(b["bids"], key=lambda x: -x["price"])
            if not a or not d: continue
            snap.append((ts, ex, d[0]["price"], a[0]["price"], d[0]["quantity"], a[0]["quantity"], a[1]["quantity"] if len(a) > 1 else 0, d[1]["quantity"] if len(d) > 1 else 0))
        except Exception: pass
S = pd.DataFrame(snap, columns=["ts", "ex", "b", "a", "bq", "aq", "a2", "b2"]).sort_values(["ex", "ts"])
print("snapshots", S.ts.nunique(), "from", pd.Timestamp(S.ts.min(), unit="s"), "rows", len(S))
S["m"] = (S.b + S.a) / 2
med = S.groupby("ex")[["aq", "bq"]].transform("median")
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
v = pd.read_sql_query("select ts,exchange_id ex,fair,sig_bid,sig_ask from venue_fair where exchange_id is not null and ts>?", db, params=(float(S.ts.min()) - 600,))
v["mid"] = (v.sig_bid + v.sig_ask) / 2; v = v.dropna(subset=["mid"]).sort_values("ts")
paths = {e: g for e, g in v.groupby("ex")}
def later(ex, t, h):
    g = paths.get(ex)
    if g is None: return np.nan, np.nan
    i = np.searchsorted(g.ts.values, t + h)
    if i >= len(g) or g.ts.values[i] - (t + h) > 1800: return np.nan, np.nan
    return g.mid.values[i], g.fair.values[i]
rows = []
for r, ma, mb in zip(S.itertuples(), med.aq.values, med.bq.values):
    for side, q, mq in (("offer", r.aq, ma), ("bid", r.bq, mb)):
        if q >= 50000 and q >= 10 * max(mq, 1) and .1 <= r.m <= .9:
            s = 1.0 if side == "offer" else -1.0
            x = dict(ex=r.ex, ts=r.ts, side=side, q=q, m=r.m)
            for h, nm in ((4, "4h"), (24, "24h"), (48, "48h")):
                ml, fl = later(r.ex, r.ts, h * 3600); x["through_" + nm] = s * (ml - r.m)
            rows.append(x)
W = pd.DataFrame(rows).sort_values("ts").groupby(["ex", "side"]).head(3)
print("wall events", len(W), "legs", W.ex.nunique())
if len(W): print(W.groupby("side")[["through_4h", "through_24h", "through_48h"]].agg(["count", "mean"]).multiply([1, 100, 1, 100, 1, 100]).round(2).to_string())
# B scenarios
K = dict(DD=.6985, DR=.235, RR=.06, RD=.0065); tot = sum(K.values()); P = {k: v / tot for k, v in K.items()}   # Polymarket: chamber legs and balance-of-power; Kalshi combo legs run about 2c rich (chamber_review.md)
print("\nscenario weights", {k: round(v, 3) for k, v in P.items()}, "P(D House)", round(P["DD"] + P["DR"], 3), "P(D Senate)", round(P["DD"] + P["RD"], 3))
pay = dict(hD=dict(DD=1, DR=1, RD=0, RR=0), sNO=dict(DD=0, RD=0, DR=1, RR=1))
R = pd.read_pickle("data/research_diralpha/chamber_ladder.pkl").set_index("q")
def line(lab, qh, qs, ph, ps):
    cost = qh * ph + qs * ps
    pl = {k: qh * pay["hD"][k] + qs * pay["sNO"][k] - cost for k in P}
    ev = sum(P[k] * pl[k] for k in P); lose = sum(P[k] for k in P if pl[k] < 0)
    sd = np.sqrt(sum(P[k] * (pl[k] - ev) ** 2 for k in P))
    nd = .86 * qh - (1 - .75) * qs
    return dict(trade=lab, house_shares=qh, senate_no=qs, cash=round(cost), EV=round(ev), EV_pct=round(100 * ev / cost, 1), P_lose=round(lose, 3), DD=round(pl["DD"]), DR=round(pl["DR"]), RR=round(pl["RR"]), RD=round(pl["RD"]), sd=round(sd), netD_change=round(nd))
out = []
for q in (10000, 20000, 40000, 60000):
    r = R.loc[q]
    out.append(line(f"pair {q//1000}k", q, q, r.hD_avg, 1 - r.sD_bid_avg))
for q in (10000, 20000, 40000, 60000, 100000):
    r = R.loc[q]; out.append(line(f"House D only {q//1000}k", q, 0, r.hD_avg, 0))
for q in (10000, 20000, 40000, 60000):
    r = R.loc[q]; out.append(line(f"Senate short only {q//1000}k", 0, q, 0, 1 - r.sD_bid_avg))
T = pd.DataFrame(out); print(T.to_string(index=False)); T.to_pickle("data/research_diralpha/chamber_table.pkl")
B = json.load(open("data/books_latest.json"))
def walk(lv, q):
    got = c = 0
    for l in lv:
        t = min(q - got, l["quantity"]); c += t * l["price"]; got += t
        if got >= q: break
    return c / got
print("\nexit before results at today's book: House D sold into bids, Senate short bought back at asks")
for q in (10000, 20000, 40000, 60000):
    hx = walk(sorted(B["840"]["bids"], key=lambda x: -x["price"]), q); sx = walk(sorted(B["842"]["asks"], key=lambda x: x["price"]), q)
    r = R.loc[q]
    print(f"{q:6d}: House out at {hx:.4f} (round trip {100*(hx-r.hD_avg):.2f}c), Senate back at {sx:.4f} (round trip {100*(r.sD_bid_avg-sx):.2f}c), pair round trip ${q*((hx-r.hD_avg)+(r.sD_bid_avg-sx)):.0f}")
