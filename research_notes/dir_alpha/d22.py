"""Drill-down 22: why did the paper test take 6 new slow entries in 3 h against 6.6 a day in the strict backtest?
 A paper-style triggers per day on the panel (forward-filled history) against strict (real polls only)
 B how many of the paper entries sit next to our own fills (own footprint, wind-down from 04:00 UTC)."""
import sys, json, sqlite3, numpy as np, pandas as pd
sys.path.insert(0, "research_notes/dir_alpha")
from lab import Lab
L = Lab(); P = L.P
pd.set_option("display.width", 200, "display.max_rows", 200, "display.max_columns", 30)
print("panel ends", pd.Timestamp(L.idx[-1], unit="s", tz="UTC"))
gap = L.gap; mid = L.mid
S = gap - gap.rolling(576, min_periods=288).median(); s = np.sign(S)
d24p = L.pm - L.pm.shift(288); d24k = L.k - L.k.shift(288)
M = (S.abs() >= .02) & (mid >= .15) & (mid <= .85) & (d24p * s >= .01) & (d24k * s >= .01) & (L.rsum(P["sflow"], 288) * s >= -5000)
M = M.values.copy(); M[:, L.office == "chamber"] = False
ev = []; last = {}
for t in range(M.shape[0]):
    for j in sorted(np.flatnonzero(M[t]), key=lambda q: -abs(S.values[t, q])):
        r = L.race[j]
        if t - last.get(r, -10**9) < 288: continue
        last[r] = t; ev.append((t, j))
E_ = pd.DataFrame(ev, columns=["t", "j"]); E_["day"] = L.day[E_.t.values]
strict = pd.read_pickle("data/research_diralpha/strict_slow.pkl"); strict["day"] = L.day[strict.t.values]
print(pd.DataFrame(dict(paper_style=E_.groupby("day").size(), strict=strict.groupby("day").size())).fillna(0).astype(int).to_string())
print("legs with |S|>=2c right now (panel end):", int((S.iloc[-1].abs() >= .02).sum()), "of", S.shape[1])
print("share of leg-bins with |S|>=2c by day:"); print(((S.abs() >= .02).mean(axis=1)).groupby(L.day).mean().round(3).to_string())
db = sqlite3.connect("file:data/sigbot.sqlite?mode=ro", uri=True)
f = pd.read_sql_query("select exchange_id,side,price,quantity,filled_at,purpose from fills where filled_at>='2026-10-31'", db)
f["ts"] = pd.to_datetime(f.filled_at, format="ISO8601", utc=True)
print("fills today", len(f)); print(f.groupby("purpose").quantity.agg(["size", "sum"]).to_string())
pp = [json.loads(x) for x in open("data/research_diralpha/paper.jsonl")]
for e in pp:
    t = pd.Timestamp(e["t"], unit="s", tz="UTC")
    w = f[(f.exchange_id == e["ex"]) & (f.ts >= t - pd.Timedelta(hours=6)) & (f.ts <= t)]
    print(e.get("rule", "slow"), e["race"][:22], e["utc"], "s", e["s"], "dpm %.1f dk %.1f flow %.0f" % (100 * e.get("dpm", 0), 100 * e.get("dk", 0), e.get("flow", 0)), "our fills 6h before:", len(w), w.quantity.sum(), w.purpose.value_counts().to_dict())
