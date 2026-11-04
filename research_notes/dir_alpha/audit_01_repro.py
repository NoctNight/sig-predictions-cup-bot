"""1. Reproduce the 97 trades without lab.py. 2. Look-ahead probes: execution delay, flow cut at the entry poll, signal lag."""
import sys, pickle; sys.path.insert(0, "research_notes/dir_alpha")
import numpy as np, pandas as pd
import audit_common as c
G = c.Grid()
sf = G.flow(); M = G.masks(sf)
d = G.trades(M["full"])
d.to_pickle(c.CACHE + "/base.pkl")
def rep(n, d):
    day = d.groupby("day").net.mean()
    print(f"{n:42s} n {len(d):4d} races {d.race.nunique():3d} net {100*d.net.mean():6.2f}c gross {100*d.gross.mean():6.2f}c win {100*(d.net>0).mean():4.0f}% days+ {(day>0).sum()}/{len(day)} worst {100*d.net.min():.1f}")
rep("independent rebuild, rule as written", d)
print(d.groupby("day").net.agg(["size", lambda x: 100*x.mean()]).round(2).T.to_string())
# compare with the study's own panel (data only, no lab import)
P = pickle.load(open("data/research_diralpha/panel.pkl", "rb"))
print("panel vs rebuild cols equal:", list(P["sig_bid"].columns) == list(P["sig_bid"].columns), "shape", P["sig_bid"].shape, G.bid.shape)
x = P["sig_bid"].reindex(index=G.idx, columns=G.cols); y = G.P["sig_bid"]
print("sig_bid cells differ:", int(((x-y).abs() > 1e-9).sum().sum()), "of", int(y.notna().sum().sum()))
e = pd.read_pickle("data/research_diralpha/d4.pkl")
print(e[(e.rule=="both+flow")&(e.hold=="24h")&(e.th==.02)&(e.office=="all")][["n","net","win","train","test","worst"]].round(2).to_string())
# delays
for dl in (0, 1, 2, 3, 6, 12, 36):
    rep(f"fill delay {dl*5} min after signal bin", G.trades(M["full"], delay=dl))
# flow cut: only trades whose executed_at <= poll ts of the entry row, and which had been ingested by then
tp = G.tape
print("tape ingest lag (s): ", (tp.ingested_at - tp.ts).describe(percentiles=[.5,.9,.99,.999]).round(1).to_dict())
print("share ingested later than 5 min:", round(((tp.ingested_at-tp.ts) > 300).mean(), 4), " later than 1 h:", round(((tp.ingested_at-tp.ts) > 3600).mean(), 4))
print("aggressor mix", tp.aggressor.fillna("null").value_counts(normalize=True).round(3).to_dict())
# as-known flow: at entry time use only prints ingested before the entry poll time
tt = tp.sort_values("ts")
def asknown(ex, t0):
    a = by[ex]; m = (a.ts > t0 - 86400) & (a.ts <= t0) & (a.ingested_at <= t0)
    return a.sf[m].sum()
by = {k: v for k, v in tp.groupby("exchange_id")}
polls = G.P["ts"].values
# rerun with flow as-known: enumerate candidates under no flow filter then apply flow test in order
base = M["base"] & M["both"]
S = G.S48.values
kept = np.zeros_like(base)
for j in range(base.shape[1]):
    ex = G.cols[j]
    if ex not in by: kept[:, j] = base[:, j]; continue
    for i in np.flatnonzero(base[:, j] & (np.abs(S[:, j]) >= c.TH)):
        kept[i, j] = asknown(ex, polls[i, j]) * np.sign(S[i, j]) >= -5000
d2 = G.trades(kept); rep("flow as known at entry poll", d2)
rep("no flow filter (both only)", G.trades(M["base"] & M["both"]))
rep("no both, flow only", G.trades(M["base"] & M["flowok"]))
rep("neither (band + no chamber)", G.trades(M["base"]))
for nm, fld in (("aggr null share at trades", None),):
    pass
# flips: reversed-sign placebo of the flow filter
