"""Drill-down 9: price impact of size. Does the market maker move its quote when lifted again and again? Three looks:
 1 refill study rows (touch fully taken: refilled at the same price, or moved away)
 2 our own taker runs: walked price against time, by run size, using fills and the tape
 3 panel regression: mid change after a 5-min bin against signed flow, by office and size bucket, with the outside move as a control."""
import json, sqlite3, sys, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 40)
d = json.load(open("data/research_overnight/refill_study.json"))
R = pd.DataFrame(d["rows"])
R = R[R.res.map(lambda x: isinstance(x, (list, tuple)))]
R["what"] = R.res.map(lambda x: x[0]); R["sec"] = R.res.map(lambda x: x[1])
R["bk"] = pd.cut(R.frac, [0, .25, .5, .99, 1.0, 100])
print("1. refill study: what happened after a print took this share of the touch")
print(pd.crosstab(R.bk, R.what, normalize="index").round(2).to_string()); print(R.bk.value_counts().sort_index().to_string())
print("seconds to refill, full touch taken:", R[(R.frac >= 1) & (R.what.str.contains("refill"))].sec.describe().round(1).to_dict())
L = Lab()
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
tp = pd.read_pickle("data/research_diralpha/tape.pkl")
tp = tp[tp.aggressor.isin(["buy", "sell"])].sort_values("ts")
tp["q"] = tp.quantity.abs()
tp["sgn"] = np.where(tp.aggressor == "buy", 1, -1)
# runs: same leg, same side, gaps under 60 s
rows = []
for ex, g in tp[tp.ts >= L.idx[0]].groupby("exchange_id"):
    if ex not in L.cols: continue
    j = L.cols.index(ex)
    t, q, s, p = g.ts.values, g.q.values, g.sgn.values, g.price.values
    st = 0
    for i in range(1, len(t) + 1):
        if i == len(t) or s[i] != s[st] or t[i] - t[i - 1] > 60:
            sh = q[st:i].sum()
            if sh >= 1000 and 0.1 <= p[st] <= .9:
                rows.append(dict(ex=ex, j=j, t0=t[st], t1=t[i - 1], s=s[st], sh=sh, prints=i - st, p0=p[st], p1=p[i - 1], office=L.office[j], walked=s[st] * (p[i - 1] - p[st])))
            st = i
Rn = pd.DataFrame(rows)
print("\n2. taker runs on legs priced 10-90:", len(Rn))
mid = L.mid.values; fair = L.fair.values; idx = L.idx
bin_ = np.searchsorted(idx, Rn.t0.values) - 1
for h, n in ((1, "5m"), (3, "15m"), (12, "1h"), (48, "4h")):
    ok = (bin_ >= 0) & (bin_ + h + 1 < len(idx))
    a = np.full(len(Rn), np.nan); o = np.full(len(Rn), np.nan)
    a[ok] = Rn.s.values[ok] * (mid[bin_[ok] + h + 1, Rn.j.values[ok]] - mid[bin_[ok], Rn.j.values[ok]])
    o[ok] = Rn.s.values[ok] * (fair[bin_[ok] + h + 1, Rn.j.values[ok]] - fair[bin_[ok], Rn.j.values[ok]])
    Rn["d" + n] = 100 * a; Rn["o" + n] = 100 * o
Rn["size"] = pd.cut(Rn.sh, [999, 2000, 5000, 10000, 20000, 50000, 1e7])
t = Rn.groupby(["office", "size"], observed=True).agg(n=("sh", "size"), shares=("sh", "median"), prints=("prints", "median"), secs=("t1", lambda x: 0), walked=("walked", lambda x: 100 * x.mean()), d5m=("d5m", "mean"), d15m=("d15m", "mean"), d1h=("d1h", "mean"), d4h=("d4h", "mean"), o1h=("o1h", "mean")).round(2)
print(t.drop(columns="secs").to_string())
Rn["ex_out_1h"] = Rn.d1h - Rn.o1h
print("run mid move after 1 h net of the outside move, cents:"); print(Rn.groupby("size", observed=True).ex_out_1h.agg(["size", "mean", "median"]).round(2).to_string())
# 3 regression on bins
m = L.mid; f = L.fair
fl = L.P["sflow"]
res = []
for off in ("house", "governor", "senate", "chamber"):
    c = L.office == off
    for h, n in ((1, "same bin to +5m"), (12, "to +1h"), (48, "to +4h")):
        y = (m.shift(-h) - m.shift(1)).loc[:, c].values.ravel()
        x = fl.loc[:, c].values.ravel() / 1000
        z = (f.shift(-h) - f.shift(1)).loc[:, c].values.ravel()
        ok = np.isfinite(y) & np.isfinite(x) & np.isfinite(z) & (np.abs(x) >= .1)
        X = np.c_[np.ones(ok.sum()), x[ok], z[ok]]
        b = np.linalg.lstsq(X, y[ok], rcond=None)[0]
        res.append(dict(office=off, horizon=n, n=int(ok.sum()), cents_per_1k_shares=100 * b[1], outside_beta=b[2]))
print("\n3. regression: lasting price move per 1,000 shares of net flow, outside move controlled"); print(pd.DataFrame(res).round(3).to_string(index=False))
# our own orders: sequence of large taker exits
fl_ = pd.read_sql_query("select filled_at,exchange_id,price,quantity,purpose from fills where purpose in ('raise','exit','catchup','catchup_exit','shock','shock_exit') order by filled_at", db)
fl_["ts"] = (pd.to_datetime(fl_.filled_at, format="ISO8601", utc=True) - pd.Timestamp("1970-01-01", tz="UTC")).dt.total_seconds()
print("\nour taker fills", len(fl_), "median size", fl_.quantity.abs().median())
